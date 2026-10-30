r"""
add_accounts.py — bulk-add password+TOTP accounts (no cookies supplied).

Some sellers ship `auth_token` (use mint_ct0.py for those). Others ship only a
password + a TOTP secret, which means an actual LOGIN is required to mint a
session. Input format, one per line:

    email:email_password:x_password:TOTP_SECRET:https://x.com/<handle>

The handle comes from the profile URL. For each account this logs in through
that account's OWN proxy exit (rotating exits on failure), then writes a normal
accounts.txt line WITH the harvested auth_token + ct0 so every later run is
cookie-based — X blocks/locks repeated password logins, so we only ever want to
do this once per account.

Accounts that fail to log in are reported and SKIPPED (never written half-formed,
never silently dropped). Re-running only attempts the ones not already present.

    python add_accounts.py batch.txt              # log in + add all
    python add_accounts.py batch.txt --limit 2    # try a couple first
    python add_accounts.py batch.txt --parse-only # just show what it parsed
"""
import os
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


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')


def harvest_ondemand(cfg, proxies):
    """Grab X's ondemand.s hash using an ALREADY-authenticated account.

    A logged-out x.com page has no ondemand.s reference, so twikit can't build a
    transaction id and every login dies with "Couldn't get KEY_BYTE indices".
    The hash is global (a deploy artifact), so one logged-in fetch unblocks all
    the logins.
    """
    import subprocess
    for acct in tp.load_accounts():
        if not acct.get('auth_token'):
            continue
        proxy = tp.resolve_proxy(acct, cfg, proxies, 0)
        cmd = ['curl', '-s', '--max-time', '45', '-A', UA,
               '-b', f'auth_token={acct["auth_token"]}', 'https://x.com/home']
        if proxy:
            cmd[1:1] = ['-x', proxy]
        try:
            page = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=90).stdout
        except Exception:
            continue
        h = tp.scrape_ondemand_hash(page or '')
        if h:
            tp.write_ondemand_cache(h)
            print(f'  ondemand hash harvested from @{acct["username"]}: {h}')
            return h
    print('  ! could not harvest an ondemand hash — logins will fail')
    return None


def parse_batch(path):
    out = []
    for raw in open(path, encoding='utf-8'):
        raw = raw.strip()
        if not raw or raw.startswith('#'):
            continue
        parts = raw.split(':')
        if len(parts) < 5:
            print(f'  ! unparseable: {raw[:50]}')
            continue
        email, email_pw, x_pw, totp = parts[0], parts[1], parts[2], parts[3]
        url = ':'.join(parts[4:])                    # url contains a colon
        handle = url.rstrip('/').rsplit('/', 1)[-1].lstrip('@')
        if not handle:
            print(f'  ! no handle in: {raw[:50]}')
            continue
        out.append({'username': handle, 'password': x_pw, 'email': email,
                    'email_password': email_pw, 'totp': totp})
    return out


async def login_one(rec, cfg, proxies, tries=3):
    """Log in and return (auth_token, ct0) or (None, reason)."""
    for attempt in range(tries):
        proxy = tp.resolve_proxy({'username': rec['username']}, cfg, proxies, attempt)
        client = Client(language=cfg.get('language', 'en-US'), proxy=proxy,
                        timeout=float(cfg.get('http_timeout_seconds', 60)))
        try:
            await client.login(
                auth_info_1=rec['username'],
                auth_info_2=rec['email'] or None,
                password=rec['password'],
                totp_secret=rec['totp'] or None,
            )
            ck = client.get_cookies()
            at, ct0 = ck.get('auth_token'), ck.get('ct0')
            if at and ct0:
                os.makedirs(tp.COOKIES_DIR, exist_ok=True)
                client.save_cookies(tp.cookies_path(rec))
                return (at, ct0), None
            return None, 'login ok but no cookies returned'
        except Exception as e:
            reason = f'{type(e).__name__}: {str(e)[:90]}'
            if attempt < tries - 1:
                await asyncio.sleep(random.uniform(2, 5))
                continue
            return None, reason
    return None, 'exhausted'


def line_for(rec, at, ct0):
    return ':'.join([rec['username'], rec['password'], rec['email'],
                     rec['email_password'], ct0, at, rec['totp']])


async def main(path, limit, parse_only, gap):
    recs = parse_batch(path)
    print(f'parsed {len(recs)} account(s)')
    if parse_only:
        for r in recs:
            print(f"  {r['username']:<18} pw={r['password']:<14} totp={r['totp'][:6]}… {r['email']}")
        return

    cfg = tp.load_config()
    proxies = tp.load_proxies()
    if not tp.read_ondemand_cache():
        harvest_ondemand(cfg, proxies)
    existing = {a['username'].lower()
                for a in tp.load_accounts(include_benched=True)}   # dedupe vs ALL lines
    todo = [r for r in recs if r['username'].lower() not in existing]
    print(f'{len(recs) - len(todo)} already present, {len(todo)} to add\n')
    if limit:
        todo = todo[:limit]

    ok = fail = 0
    for i, rec in enumerate(todo, 1):
        u = rec['username']
        creds, err = await login_one(rec, cfg, proxies)
        if creds:
            at, ct0 = creds
            with open(tp.ACCOUNTS_FILE, 'a', encoding='utf-8') as fh:
                fh.write(line_for(rec, at, ct0) + '\n')
            ok += 1
            print(f'  ✓ [{i}/{len(todo)}] @{u} logged in + added', flush=True)
        else:
            fail += 1
            print(f'  ✗ [{i}/{len(todo)}] @{u} FAILED — {err}', flush=True)
        if i < len(todo):
            await asyncio.sleep(random.uniform(*gap))

    print(f'\nadded {ok}, failed {fail}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Bulk-add password+TOTP X accounts')
    ap.add_argument('batch', help='file of email:emailpw:xpw:totp:profile_url lines')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--parse-only', action='store_true')
    ap.add_argument('--gap', type=float, nargs=2, default=(4, 10),
                    help='min/max seconds between logins')
    args = ap.parse_args()
    asyncio.run(main(args.batch, args.limit, args.parse_only, tuple(args.gap)))
