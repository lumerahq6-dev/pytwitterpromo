r"""
media_ledger.py — hand every account a DIFFERENT clip.

Why this exists: `pick_media()` picks at random, so with a fleet the same video
can go out from several accounts on the same day. Identical media across
accounts is the clearest "these are one operator" signal there is — it's what
gets a whole cohort actioned together, not just one account. So clip choice has
to be coordinated, not random.

Rules, in order:
  1. Never hand out a clip another account posted inside `reuse_gap_hours`
     (default 48h) — that's the hard uniqueness guarantee.
  2. Prefer clips this account has NEVER posted (an account repeating itself
     looks like a bot loop).
  3. Break ties by least-recently-used globally, then fewest total uses.
If rule 1 leaves nothing (bank too small for the fleet), it degrades to the
globally least-recently-used clip and says so loudly — the fix is more clips,
and silently double-posting would be worse than a warning.

A claim is recorded at UPLOAD time, not post time: two accounts posting
concurrently must not race onto the same clip.

  data/media_ledger.json = {"<slug>": {"last_ts": int, "uses": int,
                                        "by": {"<account>": last_ts}}}
"""
import os
import json
import time
import threading

import store

_LOCK = threading.Lock()
LEDGER = 'media_ledger.json'


def _path():
    return os.path.join(store.DATA_DIR, LEDGER)


def _load():
    return store.read_json(_path(), {})


def _save(led):
    os.makedirs(store.DATA_DIR, exist_ok=True)
    store.write_json(_path(), led)


def claim(account, candidates, reuse_gap_hours=48, log=None):
    """Pick + atomically claim the best clip for `account`.

    `candidates` = list of clip basenames currently enabled. Returns the chosen
    basename, or None if there are no candidates at all.
    """
    if not candidates:
        return None
    now = int(time.time())
    gap = max(0, int(reuse_gap_hours)) * 3600

    with _LOCK:
        led = _load()

        def rec(c):
            return led.get(c) or {'last_ts': 0, 'uses': 0, 'by': {}}

        # 1) hard filter: not used by ANYONE inside the gap
        fresh = [c for c in candidates if now - rec(c)['last_ts'] >= gap]
        pool, degraded = fresh, False
        if not pool:
            pool, degraded = list(candidates), True
            if log:
                log(f'  ⚠ media: every clip was used within {reuse_gap_hours}h '
                    f'({len(candidates)} clips / fleet too small) — reusing the '
                    f'oldest. ADD MORE CLIPS to keep posts unique.')

        # 2) prefer clips this account has never posted
        never = [c for c in pool if account not in rec(c)['by']]
        if never:
            pool = never

        # 3) least-recently-used globally, then fewest uses, then name (stable)
        pool.sort(key=lambda c: (rec(c)['last_ts'], rec(c)['uses'], c))
        chosen = pool[0]

        r = rec(chosen)
        r['last_ts'] = now
        r['uses'] = int(r.get('uses', 0)) + 1
        r.setdefault('by', {})[account] = now
        led[chosen] = r
        _save(led)

    if log and not degraded:
        log(f'  media: {chosen} (unique — unused by the fleet for {reuse_gap_hours}h)')
    return chosen


def release(account, clip):
    """Undo a claim when the upload failed, so the clip isn't burned for 48h."""
    if not clip:
        return
    with _LOCK:
        led = _load()
        r = led.get(clip)
        if not r:
            return
        r['uses'] = max(0, int(r.get('uses', 1)) - 1)
        by = r.get('by', {})
        by.pop(account, None)
        r['by'] = by
        # roll last_ts back to the newest remaining use (0 if now unused)
        r['last_ts'] = max(by.values()) if by else 0
        led[clip] = r
        _save(led)


def stats(candidates=None):
    led = _load()
    now = int(time.time())
    out = []
    for clip, r in led.items():
        out.append({
            'clip': clip,
            'uses': r.get('uses', 0),
            'last_ts': r.get('last_ts', 0),
            'hours_ago': round((now - r.get('last_ts', 0)) / 3600, 1) if r.get('last_ts') else None,
            'accounts': sorted((r.get('by') or {}).keys()),
        })
    out.sort(key=lambda x: -(x['last_ts'] or 0))
    if candidates is not None:
        known = {o['clip'] for o in out}
        for c in candidates:
            if c not in known:
                out.append({'clip': c, 'uses': 0, 'last_ts': 0,
                            'hours_ago': None, 'accounts': []})
    return out
