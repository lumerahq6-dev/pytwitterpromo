r"""
Stats poller — fetches view/like/etc counts for recent tweets and each account's
follower numbers, and appends a totals snapshot for the dashboard chart.

Each account's tweets/profile are fetched with THAT ACCOUNT'S OWN session: you
can always see your own (even adult-labeled) tweets ungated, whereas a shared
"reader" account gets a gated/None result for sensitive posts. Clients are
cached per account. The scheduler calls poll_async() once per tick; each call
refreshes only a small slice so it never blocks posting or hammers the API.
View counts read None on brand-new tweets and become numbers once impressions
accrue.
"""
import asyncio
import os

import twitter_poster as tp  # applies twikit patches on import
import store
from twikit import Client

_clients = {}
_clients_loop = None      # the event loop the cached clients were built on


def _sync_loop():
    """Drop cached clients when the event loop changed.

    The scheduler calls `asyncio.run(poll_async(...))` once per tick, and
    asyncio.run creates a NEW loop and CLOSES it on return. A twikit Client
    holds an httpx.AsyncClient bound to the loop it was built on, so reusing the
    cache on the next tick raises 'Event loop is closed' for every account —
    stats then silently stop updating until the process restarts (which is why
    they appeared to work right after each restart and then plateau).
    """
    global _clients_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _clients_loop is not loop:
        _clients.clear()
        _clients_loop = loop


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _session(acct, cfg, proxies, attempt=0, fresh=False):
    """Warm client for acct. fresh=True forces a NEW client on a rotated exit
    and replaces any cached one. Prefers the poster's saved cookie jar (it holds
    a recently reminted ct0) over the account's stored, possibly-stale ct0."""
    u = acct['username']
    if not fresh and u in _clients:
        return _clients[u]
    cp = tp.cookies_path(acct)
    if not acct.get('auth_token') and not os.path.exists(cp):
        return None
    c = Client(language=cfg.get('language', 'en-US'),
               proxy=tp.resolve_proxy(acct, cfg, proxies, attempt),
               user_agent=(cfg.get('user_agent') or None),
               timeout=float(cfg.get('http_timeout_seconds', 60)))
    if os.path.exists(cp):
        try:
            c.load_cookies(cp)
            _clients[u] = c
            return c
        except Exception:
            pass
    ck = {'auth_token': acct['auth_token']}
    if acct.get('ct0'):
        ck['ct0'] = acct['ct0']
    c.set_cookies(ck)
    _clients[u] = c
    return c


# Back-compat alias: external callers used _client_for.
def _client_for(acct, cfg, proxies):
    return _session(acct, cfg, proxies)


async def _with_retry(acct, cfg, proxies, uname, fn, log, tries=3):
    """Run fn(client); on stale-ct0 (403/353), twikit ClientTransaction
    bootstrap failure, or a transport error, remint ct0 AND rotate to a fresh
    exit, then retry. Without this the cached client kept a dead ct0 and every
    fetch 403'd forever, so metrics never updated."""
    last = None
    for attempt in range(tries):
        c = _session(acct, cfg, proxies, attempt, fresh=(attempt > 0))
        if not c:
            return None
        try:
            return await fn(c)
        except Exception as e:
            last = e
            try:
                if tp._remint_ct0(c, acct, cfg, proxies, attempt):
                    c.save_cookies(tp.cookies_path(acct))
            except Exception:
                pass
            await asyncio.sleep(1.5)
    if last:
        raise last
    return None


def reset_clients():
    _clients.clear()


async def poll_async(accounts, cfg, proxies, log=print):
    _sync_loop()
    now = store.now()
    posts = store.read_posts()
    metrics = store.read_metrics()
    meta = store.read_meta()
    acct_by_name = {a['username']: a for a in accounts}

    # 1) per-tweet metrics — refresh the stalest slice, grouped by account so
    #    each account reads its own (ungated) tweets via its own session.
    iv = int(cfg.get('stats_interval_seconds', 1800))
    per = int(cfg.get('stats_per_tick', 20))
    week = now - 7 * 86400
    cand = [p for p in posts
            if p.get('ts', 0) >= week and p.get('tweet_id') and p.get('account') in acct_by_name]
    cand.sort(key=lambda p: (metrics.get(p['tweet_id']) or {}).get('ts', 0))
    due_by_acct = {}
    total = 0
    for p in cand:
        if total >= per:
            break
        snap = metrics.get(p['tweet_id'])
        if snap and now - snap.get('ts', 0) < iv:
            continue
        due_by_acct.setdefault(p['account'], []).append(str(p['tweet_id']))
        total += 1

    # Poll accounts CONCURRENTLY. Sequentially this cost a fresh client + proxy
    # handshake + twikit's x.com homepage fetch per account (~10-30s each), so a
    # run over ~20 due accounts overran the 3-min timer and metrics stayed ~45min
    # stale. Bounded so we don't open 100 residential tunnels at once.
    polled = 0
    sem = asyncio.Semaphore(int(cfg.get('stats_concurrency', 8)))
    per_acct_timeout = float(cfg.get('stats_account_timeout', 90))

    async def _one(uname, ids):
        nonlocal polled
        async with sem:
            acct = acct_by_name[uname]
            for i in range(0, len(ids), 100):
                chunk = ids[i:i + 100]

                async def _fetch(c, _chunk=chunk):
                    return await asyncio.wait_for(
                        c.get_tweets_by_ids(_chunk), timeout=per_acct_timeout)
                try:
                    tweets = await _with_retry(acct, cfg, proxies, uname, _fetch, log)
                except Exception as e:
                    log(f'  stats: @{uname} tweets -> {type(e).__name__}: {str(e)[:60]}')
                    continue
                for t in (tweets or []):
                    if t is None:           # gated / deleted / unparseable
                        continue
                    metrics[str(t.id)] = {
                        'ts': now, 'account': uname,
                        'views': _int(t.view_count), 'likes': _int(t.favorite_count),
                        'retweets': _int(t.retweet_count), 'replies': _int(t.reply_count),
                        'quotes': _int(t.quote_count), 'bookmarks': _int(t.bookmark_count),
                    }
                    polled += 1

    if due_by_acct:
        await asyncio.gather(*(_one(u, ids) for u, ids in due_by_acct.items()),
                             return_exceptions=True)
    if polled:
        store.write_metrics(metrics)
        # time-series for the Gainers tab (bucketed + deduped inside store)
        try:
            store.append_metrics_history(
                (tid, s.get('views'), s.get('likes'), s.get('bookmarks'))
                for tid, s in metrics.items() if s.get('ts') == now)
            if now % 3600 < int(cfg.get('check_interval_seconds', 60)):
                store.trim_metrics_history()      # ~hourly
        except Exception as e:
            log(f'  stats: history write -> {type(e).__name__}: {str(e)[:60]}')

    # 2) account profiles (followers/following) — own session, stalest first
    miv = int(cfg.get('meta_interval_seconds', 3600))
    mper = int(cfg.get('meta_per_tick', 3))
    unames = sorted(acct_by_name, key=lambda u: (meta.get(u) or {}).get('ts', 0))
    done = 0
    for u in unames:
        if done >= mper:
            break
        m = meta.get(u)
        if m and now - m.get('ts', 0) < miv:
            continue
        acct = acct_by_name[u]

        async def _pf(c, _u=u):
            return await c.get_user_by_screen_name(_u)
        try:
            usr = await _with_retry(acct, cfg, proxies, u, _pf, log)
            if usr is None:
                continue
            meta[u] = {
                'ts': now, 'name': getattr(usr, 'name', None),
                'followers': _int(getattr(usr, 'followers_count', 0)),
                'following': _int(getattr(usr, 'following_count', 0)),
                'statuses': _int(getattr(usr, 'statuses_count', 0)),
                # X signup date — the fleet is bought in batches of different
                # vintages, and age is the thing we can compare cohorts on
                # (aged handles vs fresh ones reach and survive differently).
                'created_at': getattr(usr, 'created_at', None),
            }
            done += 1
        except Exception as e:
            log(f'  stats: @{u} profile -> {type(e).__name__}: {str(e)[:60]}')
    if done:
        store.write_meta(meta)

    # 3) totals snapshot (drives the chart + rolling-window deltas)
    if metrics:
        store.append_totals({
            'ts': now,
            'views': sum(s.get('views', 0) for s in metrics.values()),
            'likes': sum(s.get('likes', 0) for s in metrics.values()),
            'retweets': sum(s.get('retweets', 0) for s in metrics.values()),
            'posts': len(posts),
        })
