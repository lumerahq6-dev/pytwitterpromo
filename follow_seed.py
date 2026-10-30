r"""
follow_seed.py — give an account that follows NOBODY a following list before it posts.

Why: 107 of the 396 accounts in the bank have following == 0. An account with
zero follows, zero followers and a fresh porn clip is the cheapest shell shape
there is, and tonight's cut of the dead had following==0 surviving 1/7 (14%) vs
24/36 (67%) for following>=1.

⚠️ That signal is WEAK — p=0.0154 raw, ~0.09 after correcting for the six cuts
tested, and the earlier larger-sample run got p=0.605 with the sign reversed. So
this is cheap insurance, not a proven fix. It is built to be measurable: every
seeding is recorded in data/follow_seed.json with a timestamp, so survival of
seeded vs unseeded accounts can be cut later.

Design notes (same reasoning as communities.py):
  * Seeding is LAZY — it happens on the account's own next post, not in a burst
    when the account is imported. 107 brand-new accounts all following the same
    person inside one minute is precisely the coordinated-inauthenticity shape we
    just proved is what kills this fleet. Spread across each account's own
    schedule, it's just... a new account following someone.
  * For the same reason the follow set is NOT identical fleet-wide. The operator
    asked for Elon Musk, so @elonmusk is always followed; 1-2 extra picks from a
    pool of huge mainstream accounts go with it, so no two accounts emit the same
    follow-graph fingerprint.
  * Only accounts at following == 0 are touched, read LIVE from the API rather
    than from the (nightly, stale) accounts_meta.json.
  * Every account is checked at most once — the result is cached either way, so
    an account already following someone costs exactly one GET, ever.
  * Best-effort ALWAYS. A failure here must never fail the post.
"""
import os
import json
import random
import asyncio
import threading

import store

_LOCK = threading.Lock()
SEEDED = 'follow_seed.json'      # per-account outcome
IDS = 'follow_seed_ids.json'     # handle -> numeric user id, resolved once fleet-wide
MAX_TRIES = 3                    # follow attempted but didn't land
MAX_CHECK_FAILS = 25             # couldn't even read the following count

# _attempt_post only re-mints a stale ct0 in its EXCEPT handler, so the first
# _do_post of a session routinely runs on a dead ct0 and every authenticated
# read 403s. The retry re-enters _do_post with a fresh ct0 and we seed there.
# That means check failures are normal and must not exhaust the budget the way
# a real failure does -- hence the separate, much larger counter.


def _path(name):
    return os.path.join(store.DATA_DIR, name)


def _load(name):
    return store.read_json(_path(name), {})


def _save(name, d):
    os.makedirs(store.DATA_DIR, exist_ok=True)
    store.write_json(_path(name), d)


def state(username):
    return (_load(SEEDED).get(username) or {})


def is_done(username):
    return bool(state(username).get('done'))


def _record(username, **fields):
    with _LOCK:
        d = _load(SEEDED)
        rec = d.setdefault(username, {})
        rec.update(fields)
        rec['ts'] = store.now()
        _save(SEEDED, d)


def targets(username, cfg):
    """The handles this account should follow: the primary plus a per-account
    random tail, so the fleet doesn't share one follow-graph signature."""
    primary = (cfg.get('follow_seed_primary') or 'elonmusk').lstrip('@')
    pool = [h.lstrip('@') for h in (cfg.get('follow_seed_extra_pool') or [])
            if h.lstrip('@').lower() != primary.lower()]
    lo = int(cfg.get('follow_seed_extra_min', 1))
    hi = int(cfg.get('follow_seed_extra_max', 2))
    n = random.randint(min(lo, hi), max(lo, hi)) if pool else 0
    rng = random.Random(username.lower())        # stable per account
    extra = rng.sample(pool, min(n, len(pool))) if n > 0 else []
    return [primary] + extra


async def _resolve(client, handle):
    """handle -> numeric id, cached fleet-wide so we resolve each one once."""
    ids = _load(IDS)
    uid = ids.get(handle.lower())
    if uid:
        return uid
    u = await client.get_user_by_screen_name(handle)
    uid = str(getattr(u, 'id', '') or '')
    if not uid.isdigit():
        return None
    with _LOCK:
        ids = _load(IDS)
        ids[handle.lower()] = uid
        _save(IDS, ids)
    return uid


async def _ensure_public(client, username, me, cfg, log=None):
    """A protected account shows posts only to approved followers, and these
    accounts have ~0 followers -- so it posts into the void. Flip it public.

    Rides on the user object ensure_seeded already fetched, so it costs no extra
    API call. Best-effort: never raises into the post path.

    ⚠️ Every protected account in the 2026-09-17 sweep turned out to be already
    SUSPENDED (X refuses settings writes on those), so this is insurance for a
    future batch that arrives protected-and-alive, not a fix for a live problem.
    """
    if not cfg.get('unprotect_enabled', True):
        return False
    if not bool(getattr(me, 'protected', False)):
        return False
    try:
        from twikit.client.v11 import Endpoint
        headers = dict(client._base_headers)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        await client.post(Endpoint.SETTINGS,
                          data={'protected': 'false'}, headers=headers)
        if log:
            log(f'  @{username}: was PROTECTED (posts reached nobody) -> set public')
        _record(username, unprotected=True)
        return True
    except Exception as e:
        if log:
            log(f'  @{username}: unprotect failed ({type(e).__name__}: {str(e)[:60]})')
        return False


async def ensure_seeded(client, username, cfg, log=None):
    """If this account follows nobody, give it a following list. Returns the
    list of handles followed (empty if nothing was needed or possible).

    Never raises — the caller is about to post and must not be blocked by this.
    """
    if not cfg.get('follow_seed_enabled'):
        return []
    if is_done(username):
        return []
    st = state(username)
    if int(st.get('tries', 0)) >= MAX_TRIES:
        return []
    if int(st.get('check_fails', 0)) >= MAX_CHECK_FAILS:
        return []

    # Fast path: if last night's sweep already saw this account following
    # someone, there is nothing to do and no reason to spend an API call.
    try:
        m = store.read_meta().get(username) or {}
        if isinstance(m.get('following'), int) and m['following'] > 0:
            _record(username, done=True, followed=[],
                    skipped='meta-following-%d' % m['following'])
            return []
    except Exception:
        pass

    try:
        # NOT client.user(): that is get_user_by_id, which 403/404s on every one
        # of these sessions (13/13 measured) while get_user_by_screen_name works
        # on the very same cookies. Posting survives for the same reason.
        me = await client.get_user_by_screen_name(username)
        count = int(getattr(me, 'following_count', 0) or 0)
        await _ensure_public(client, username, me, cfg, log)
    except Exception as e:
        _record(username, check_fails=int(st.get('check_fails', 0)) + 1,
                error='check-failed:%s' % type(e).__name__)
        return []

    if count > 0:
        # Nothing to do, and never worth checking again.
        _record(username, done=True, followed=[], skipped='already-following-%d' % count)
        return []

    want = targets(username, cfg)
    delay = float(cfg.get('follow_seed_delay_seconds', 3))
    done = []
    for h in want:
        try:
            uid = await _resolve(client, h)
            if not uid:
                continue
            await client.follow_user(uid)
            done.append(h)
            if log:
                log(f'  @{username}: followed @{h} (was following 0)')
            if h is not want[-1]:
                await asyncio.sleep(delay * (0.5 + random.random()))
        except Exception as e:
            if log:
                log(f'  @{username}: follow @{h} failed ({type(e).__name__}: {e})')

    if done:
        _record(username, done=True, followed=done, was=0)
    else:
        _record(username, tries=int(st.get('tries', 0)) + 1,
                followed=[], error='no-follow-landed')
    return done
