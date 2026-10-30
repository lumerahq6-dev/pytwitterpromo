r"""
PREFLIGHT — validate every account before scheduling.

For each account (through its own proxy): confirm the cookies still authenticate,
report the @handle, and if the account is PROTECTED flip it public (promo tweets
on a protected account are invisible). Flags dead cookies / suspended / locked
accounts so they can be pulled before the scheduler wastes posts on them.

    python preflight.py            # check + auto-unprotect
    python preflight.py --no-fix   # check only, don't change anything
"""
import sys
import asyncio
import argparse

import twitter_poster as tp  # applies the twikit patches on import
from twikit import Client
from twikit.client.v11 import Endpoint
from twikit.errors import (Unauthorized, Forbidden, NotFound,
                           AccountSuspended, AccountLocked)


async def check_account(acct, cfg, proxies, fix=True, tries=2):
    user = acct['username']
    if not acct['auth_token']:
        return user, 'NO-COOKIES (login-only; X blocks fresh logins — add cookies)'

    data = None
    last = 'UNKNOWN'
    # X rotates ct0 constantly; a stale one fails as a misleading 404. Refresh and
    # retry (also gets a fresh proxy exit, which clears transient 522s).
    for attempt in range(tries):
        proxy = tp.resolve_proxy(acct, cfg, proxies, attempt)
        client = Client(language=cfg.get('language', 'en-US'), proxy=proxy)
        cookies = {'auth_token': acct['auth_token']}
        if acct['ct0']:
            cookies['ct0'] = acct['ct0']
        client.set_cookies(cookies)
        try:
            data, _ = await client.get(Endpoint.SETTINGS, headers=client._base_headers)
            break
        except (AccountSuspended,):
            return user, 'SUSPENDED'
        except (AccountLocked,):
            return user, 'LOCKED'
        except (Unauthorized, Forbidden, NotFound) as e:
            if attempt < tries - 1 and tp._remint_ct0(client, acct, cfg, proxies, attempt):
                last = 'STALE-CT0'
                continue
            return user, ('BAD-COOKIES (re-auth needed)'
                          if isinstance(e, Unauthorized) else f'{type(e).__name__} (stale ct0 / blocked?)')
        except Exception as e:
            last = f'ERROR {type(e).__name__}: {str(e)[:80]}'
    if data is None:
        return user, last

    sn = data.get('screen_name') or user
    protected = data.get('protected')
    if protected and fix:
        try:
            await client.post(Endpoint.SETTINGS, params={'protected': 'false'},
                              headers=client._base_headers)
            return user, f'OK  (@{sn})  was PROTECTED -> set public'
        except Exception as e:
            return user, f'PROTECTED, unprotect FAILED: {type(e).__name__}'
    return user, f'OK  (@{sn})  protected={protected}'


async def main(fix):
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    accounts = tp.load_accounts()
    print(f'preflight: {len(accounts)} account(s), fix={fix}\n', flush=True)
    results = []
    for acct in accounts:
        user, status = await check_account(acct, cfg, proxies, fix=fix)
        print(f'  {user:<18} {status}', flush=True)
        results.append((user, status))
    ok = sum(1 for _, s in results if s.startswith('OK'))
    bad = [u for u, s in results if not s.startswith('OK')]
    print(f'\nusable: {ok}/{len(results)}')
    if bad:
        print('needs attention: ' + ', '.join(bad))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-fix', action='store_true', help="don't unprotect, just report")
    args = ap.parse_args()
    asyncio.run(main(fix=not args.no_fix))
