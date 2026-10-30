r"""
mint_ct0.py — give freshly-bought accounts their missing `ct0` cookie.

AccsMarket (and most bulk sellers) ship `auth_token` but NO `ct0`. twikit needs
both: with only auth_token, X answers v11 endpoints with a bogus 404
("Sorry, that page does not exist") and preflight/posting fail for no obvious
reason. X hands out a fresh ct0 to any request that presents a valid auth_token,
so we just ask for one.

For each account missing a ct0 this fetches https://x.com/home through that
account's own proxy exit and reads the Set-Cookie:
  * `ct0`  -> written back into accounts.txt (the CSRF token twikit needs)
  * `twid` -> `u=<user_id>`, which also dates the account (see AGE_MARKS) — a
    free way to check a seller's "registered in 2011" claim actually holds.
No cookie returned at all = the auth_token is dead; the account is reported
DEAD and left alone (never silently dropped).

    python mint_ct0.py            # mint for every account missing a ct0
    python mint_ct0.py --check    # report only, don't touch accounts.txt
    python mint_ct0.py --all      # re-mint even for accounts that have one
"""
import os
import re
import sys
import time
import argparse
import subprocess

import twitter_poster as tp

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# Rough Twitter snowflake-era boundaries: sequential user ids -> signup year.
# Only the low ids matter to us (aged accounts), so approximate is fine.
AGE_MARKS = [
    (20_000_000, '2008 or earlier'), (100_000_000, '2009'), (200_000_000, '2010'),
    (400_000_000, '2011'), (900_000_000, '2012'), (1_600_000_000, '2013'),
    (3_000_000_000, '2014-2015'),
]


def approx_year(uid):
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return '?'
    if uid > 1_000_000_000_000:      # snowflake ids (2013+), not sequential
        return '2013+ (snowflake)'
    for bound, label in AGE_MARKS:
        if uid < bound:
            return label
    return '2015+'


def fetch_cookies(acct, cfg, proxies, attempt=0):
    """Hit x.com/home with the auth_token; return dict of Set-Cookie values."""
    proxy = tp.resolve_proxy(acct, cfg, proxies, attempt)
    jar = os.path.join(tp.HERE, f'_ct0jar_{acct["username"]}.txt')
    cmd = ['curl', '-s', '--max-time', '45', '-A', UA,
           '-c', jar, '-b', f'auth_token={acct["auth_token"]}',
           'https://x.com/home', '-o', os.devnull]
    if proxy:
        cmd[1:1] = ['-x', proxy]
    out = {}
    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
        if os.path.exists(jar):
            for line in open(jar, encoding='utf-8', errors='ignore'):
                parts = line.split('\t')
                if len(parts) >= 7:
                    out[parts[5].strip()] = parts[6].strip()
    except Exception:
        pass
    finally:
        try:
            os.remove(jar)
        except OSError:
            pass
    return out


def rewrite_line(raw, acct, ct0):
    """Rebuild one accounts.txt line with the ct0 inserted, order-canonical."""
    fields = [acct['username'], acct.get('password', ''), acct.get('email', ''),
              acct.get('email_password', ''), ct0, acct['auth_token']]
    if acct.get('totp'):
        fields.append(acct['totp'])
    return ':'.join(fields)


def main(check_only=False, do_all=False, tries=2):
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    accounts = tp.load_accounts()

    todo = [a for a in accounts if do_all or not a['ct0']]
    print(f'{len(accounts)} account(s) loaded, {len(todo)} need a ct0\n', flush=True)
    if not todo:
        return

    results = {}
    for acct in todo:
        u = acct['username']
        got = {}
        for attempt in range(tries):          # a flaky exit can drop the response
            got = fetch_cookies(acct, cfg, proxies, attempt)
            if got.get('ct0'):
                break
        ct0, twid = got.get('ct0'), got.get('twid', '')
        uid = re.sub(r'^u%3D|^u=', '', twid) if twid else ''
        if ct0:
            results[u] = ct0
            print(f'  ✓ {u:<16} ct0 ok ({len(ct0)} chars)  uid={uid or "?"}  '
                  f'~{approx_year(uid)}', flush=True)
        else:
            print(f'  ✗ {u:<16} DEAD — no cookie returned (auth_token expired/banned)',
                  flush=True)
        time.sleep(1)

    print(f'\nalive: {len(results)}/{len(todo)}')
    if check_only:
        print('(--check: accounts.txt not modified)')
        return
    if not results:
        return

    # rewrite only the lines we minted for; leave comments/others byte-identical
    path = tp.ACCOUNTS_FILE
    lines = open(path, encoding='utf-8').read().splitlines()
    out, n = [], 0
    for raw in lines:
        s = raw.strip()
        if s and not s.startswith('#'):
            try:
                a = tp.parse_account(raw)
            except ValueError:
                a = None
            if a and a['username'] in results:
                out.append(rewrite_line(raw, a, results[a['username']]))
                n += 1
                continue
        out.append(raw)
    tmp = path + '.tmp'
    open(tmp, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    os.replace(tmp, path)
    print(f'accounts.txt updated ({n} line(s) got a ct0)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Mint missing ct0 cookies for bought accounts')
    ap.add_argument('--check', action='store_true', help="report only, don't write")
    ap.add_argument('--all', action='store_true', help='re-mint even if a ct0 exists')
    args = ap.parse_args()
    main(check_only=args.check, do_all=args.all)
