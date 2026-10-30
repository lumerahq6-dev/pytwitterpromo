r"""
ramp.py — stagger when a NEWLY ADDED account makes its first post.

Why (and why this is NOT account warming):
  Measured 2026-09-17: fresh seller accounts survived 5/19 = 26% while the
  established fleet ran 53%, identically across both transports. These are
  dormant 2009-2012 accounts that were added and then all posted adult video
  within the same couple of hours.

  That is a coordinated-reactivation cluster, and coordinated inauthenticity is
  the ONE mechanism we actually proved kills this fleet (09-16: 1/7 vs 7/8,
  p=0.0101). Forty dormant accounts waking up together looks exactly like the
  thing the shared caption tag-bag looked like.

  So this is the same class of fix as unique captions and jittered encodes:
  break a fleet-level correlation. It does NOT post benign content, does not
  change what an account posts, and does not delay an account that is already
  in rotation -- the operator's "no warming" rule is about warm-up content, and
  this adds none.

Two levers:
  * each newly-seen account gets a random first-post time spread over
    ramp_days, so a batch trickles in instead of bursting;
  * a fleet-wide cap on how many accounts may make their FIRST post per hour.

Accounts that have ever posted are grandfathered ready, so this can never stall
the existing fleet.
"""
import os
import json
import time
import random
import threading

import store

_LOCK = threading.Lock()
LEDGER = 'account_ramp.json'


def _path():
    return os.path.join(store.DATA_DIR, LEDGER)


def _load():
    return store.read_json(_path(), {})


def _save(d):
    os.makedirs(store.DATA_DIR, exist_ok=True)
    store.write_json(_path(), d)


def seed(usernames, already_posted, cfg):
    """Register accounts we have not seen before.

    already_posted: set of usernames with any post on record -> ready now, so
    turning this on never freezes the accounts already working.
    """
    days = float(cfg.get('ramp_days', 2))
    now = time.time()
    with _LOCK:
        d = _load()
        added = 0
        for u in usernames:
            if u in d:
                continue
            if u in already_posted or days <= 0:
                d[u] = {'ready_at': 0, 'grandfathered': True}
            else:
                d[u] = {'ready_at': now + random.random() * days * 86400.0,
                        'first_seen': int(now)}
                added += 1
        if added:
            _save(d)
        return added


def ready(username, cfg):
    """Is this account allowed to make its first post yet?"""
    if not cfg.get('ramp_enabled', True):
        return True, ''
    rec = _load().get(username)
    if rec is None:
        return True, ''                      # unknown -> never block
    ra = float(rec.get('ready_at') or 0)
    if time.time() >= ra:
        return True, ''
    mins = (ra - time.time()) / 60.0
    return False, 'ramp-hold %.0fmin' % mins


def claim_first_post(username, cfg):
    """Fleet-wide burst cap on FIRST posts. Returns True if this account may
    take one of this hour's slots."""
    cap = int(cfg.get('ramp_max_new_per_hour', 4))
    if cap <= 0:
        return True
    now = time.time()
    with _LOCK:
        d = _load()
        rec = d.get(username) or {}
        if rec.get('posted'):
            return True                      # not a first post any more
        recent = [t for t in (d.get('_firsts') or []) if now - t < 3600]
        if len(recent) >= cap:
            return False
        recent.append(now)
        d['_firsts'] = recent
        rec['posted'] = int(now)
        d[username] = rec
        _save(d)
        return True


def mark_posted(username):
    with _LOCK:
        d = _load()
        rec = d.get(username)
        if rec is not None and not rec.get('posted'):
            rec['posted'] = int(time.time())
            d[username] = rec
            _save(d)


def status(cfg):
    d = _load()
    now = time.time()
    held = [(u, r) for u, r in d.items()
            if u != '_firsts' and not r.get('posted')
            and float(r.get('ready_at') or 0) > now]
    ready_now = [u for u, r in d.items()
                 if u != '_firsts' and not r.get('posted')
                 and float(r.get('ready_at') or 0) <= now]
    return {'tracked': len([k for k in d if k != '_firsts']),
            'held': len(held), 'ready_unposted': len(ready_now),
            'next': min((r.get('ready_at') for _, r in held), default=None)}
