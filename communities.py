r"""
communities.py — route a share of each account's posts into X Communities.

Why bother: correctly-labelled adult content is excluded from For You, from
logged-out search and from Explore, so the plain timeline only reaches people
via hashtags and reposts. Communities have their OWN timeline, which that
exclusion doesn't touch — it's a second distribution channel, not a nicer
version of the first.

Design notes:
  * Each account is pinned to ONE community by a stable hash of its username, so
    it doesn't hop around (an account posting into five communities reads as a
    spam bot). Assignment is weighted, so the bigger/better-fit community gets
    proportionally more of the fleet.
  * Joins happen LAZILY, on an account's first community post. That spreads 49
    joins across days of normal posting instead of 49 identical joins in one
    hour, which is the coordination signal we're trying not to emit.
  * Membership is cached in data/communities_joined.json so we don't re-join on
    every post.
"""
import os
import json
import hashlib
import threading

import store

_LOCK = threading.Lock()
JOINED = 'communities_joined.json'


def _path():
    return os.path.join(store.DATA_DIR, JOINED)


def _load():
    return store.read_json(_path(), {})


def _save(d):
    os.makedirs(store.DATA_DIR, exist_ok=True)
    store.write_json(_path(), d)


def assign(username, cfg):
    """Stable, weighted community assignment for one account. None if disabled."""
    comms = cfg.get('communities') or []
    comms = [c for c in comms if c.get('id') and c.get('enabled', True)]
    if not comms:
        return None
    # expand by weight, then pick by a stable hash of the username
    pool = []
    for c in comms:
        pool.extend([c] * max(1, int(c.get('weight', 1))))
    h = int(hashlib.sha1(username.lower().encode()).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def is_joined(username, cid):
    return cid in (_load().get(username) or [])


def mark_joined(username, cid):
    with _LOCK:
        d = _load()
        lst = d.setdefault(username, [])
        if cid not in lst:
            lst.append(cid)
            _save(d)


async def ensure_joined(client, username, cid, log=None):
    """Join once, then remember. Returns True if the account is a member."""
    if is_joined(username, cid):
        return True
    try:
        cm = await client.join_community(cid)
        mark_joined(username, cid)
        if log:
            log(f'  @{username}: joined community {getattr(cm, "name", cid)}')
        return True
    except Exception as e:
        msg = str(e)[:90]
        # already a member -> treat as success and stop retrying every post
        if 'already' in msg.lower():
            mark_joined(username, cid)
            return True
        if log:
            log(f'  @{username}: community join failed: {type(e).__name__}: {msg}')
        return False


def stats():
    d = _load()
    by = {}
    for u, cids in d.items():
        for c in cids:
            by.setdefault(c, []).append(u)
    return {'accounts_joined': len(d), 'by_community': {k: len(v) for k, v in by.items()}}
