r"""
set_bio.py — put the funnel link in every account's profile.

This is THE conversion step. Captions carry no link on purpose (link-in-body is
heavily suppressed), so the profile link is the only path from a post to the
site — every view and bookmark before this is set converts nothing. It is also
retroactive: people reach the profile from old posts, so setting it late still
captures traffic already earned.

The clickable link goes in the profile `url` field (what @kapandr does), NOT in
the bio text. Bio text is spintax'd so 49 accounts don't ship a byte-identical
description, which is the profile-level version of duplicate-copy detection.

Uses v1.1 account/update_profile.json directly (twikit has no wrapper), through
each account's own proxy exit, staggered.

    python set_bio.py --check         # show current url/bio, change nothing
    python set_bio.py                 # set url + bio on every account
    python set_bio.py --only user1,user2
    python set_bio.py --url https://pornyard.xyz --no-desc   # link only
"""
import re
import sys
import time
import random
import asyncio
import argparse

import twitter_poster as tp
from twikit import Client

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

UPDATE_PROFILE = 'https://api.x.com/1.1/account/update_profile.json'
VERIFY = 'https://api.x.com/1.1/account/verify_credentials.json'

DEFAULT_URL = 'https://pornyard.xyz'

# Spintax bio. Kept short, adult-industry plain, no hedging, and deliberately
# WITHOUT the bare domain in the text — the url field already carries the link,
# and repeating a bare .xyz in copy is what got the Redgifs accounts flagged.
DESC_SPINTAX = (
    '{18+|18+ only|18+ 🔞} · '
    '{daily clips|new clips daily|fresh clips daily|clips daily} '
    '{👇|⬇️|👇🏼|below}'
)


def spin(s):
    while True:
        m = re.search(r'\{([^{}]*)\}', s)
        if not m:
            return s
        s = s[:m.start()] + random.choice(m.group(1).split('|')) + s[m.end():]


def _client(acct, cfg, proxies, attempt=0):
    c = Client(language=cfg.get('language', 'en-US'),
               proxy=tp.resolve_proxy(acct, cfg, proxies, attempt),
               timeout=float(cfg.get('http_timeout_seconds', 60)))
    ck = {'auth_token': acct['auth_token']}
    if acct.get('ct0'):
        ck['ct0'] = acct['ct0']
    c.set_cookies(ck)
    return c


async def read_profile(acct, cfg, proxies, attempt=0):
    c = _client(acct, cfg, proxies, attempt)
    r = await c.get(VERIFY, headers=c._base_headers)
    d = r[0] if isinstance(r, tuple) else r
    ent = ((d.get('entities') or {}).get('url') or {}).get('urls') or []
    return {'url': (ent[0].get('expanded_url') if ent else (d.get('url') or '')),
            'description': d.get('description') or '',
            'name': d.get('name') or ''}


async def set_profile(acct, cfg, proxies, url, desc, tries=3):
    """Set url (+ optional description). Retries on a fresh exit / stale ct0."""
    last = 'unknown'
    for attempt in range(tries):
        c = _client(acct, cfg, proxies, attempt)
        params = {'url': url}
        if desc is not None:
            params['description'] = desc
        try:
            await c.post(UPDATE_PROFILE, params=params, headers=c._base_headers)
            return True, 'ok'
        except Exception as e:
            last = f'{type(e).__name__}: {str(e)[:70]}'
            # a rotated ct0 shows up as 401/403/404 — refresh and retry
            tp._remint_ct0(c, acct, cfg, proxies, attempt)
    return False, last


async def main(args):
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    accounts = tp.load_accounts()
    if args.only:
        want = {u.strip().lstrip('@').lower() for u in args.only.split(',')}
        accounts = [a for a in accounts if a['username'].lower() in want]

    print(f'{len(accounts)} account(s)\n', flush=True)
    ok = bad = 0
    for i, acct in enumerate(accounts):
        u = acct['username']
        if args.check:
            try:
                p = await read_profile(acct, cfg, proxies)
                print(f'  {u:<18} url={p["url"] or "(none)":<28} bio={p["description"][:34]!r}',
                      flush=True)
                ok += 1
            except Exception as e:
                print(f'  {u:<18} ERR {type(e).__name__}: {str(e)[:50]}', flush=True)
                bad += 1
            continue

        desc = None if args.no_desc else spin(DESC_SPINTAX)
        good, msg = await set_profile(acct, cfg, proxies, args.url, desc)
        if good:
            ok += 1
            print(f'  ✓ {u:<18} url set{"" if desc is None else "  bio=" + desc!r}', flush=True)
        else:
            bad += 1
            print(f'  ✗ {u:<18} {msg}', flush=True)
        if i < len(accounts) - 1:
            # 49 identical profile edits in one burst is a fingerprint; spread them
            time.sleep(random.randint(args.min_gap, args.max_gap))

    print(f'\ndone: {ok} ok, {bad} failed')
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Set the funnel link on every account profile')
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--check', action='store_true', help='report current profiles, change nothing')
    ap.add_argument('--no-desc', action='store_true', help='set the link only, leave bio text alone')
    ap.add_argument('--only', help='comma-separated usernames')
    ap.add_argument('--min-gap', type=int, default=8)
    ap.add_argument('--max-gap', type=int, default=25)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)))
