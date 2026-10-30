r"""
browser_login.py — harvest X session cookies with a REAL browser.

Why: twikit's HTTP login is dead against current X. The login endpoint answers
403 with a Cloudflare interstitial for password logins (and before that, a
logged-out page carries no ondemand.s manifest, so the transaction id can't even
be built). A genuine browser gets through where raw HTTP does not.

This drives Camoufox (anti-detect Firefox) through the normal login form on each
account's OWN residential proxy exit, handles the username-confirmation and TOTP
steps, then reads `auth_token` + `ct0` out of the browser jar and writes a normal
cookie-based accounts.txt line. After this runs once per account, everything else
in the tool is cookie-based — which is what X actually tolerates.

Input format (same as add_accounts.py):
    email:email_password:x_password:TOTP_SECRET:https://x.com/<handle>

    python browser_login.py batch.txt --limit 1     # prove it on one first
    python browser_login.py batch.txt               # then the rest
    python browser_login.py batch.txt --headful     # watch it (needs a display)
"""
import os
import sys
import random
import asyncio
import argparse

import pyotp
import twitter_poster as tp
from add_accounts import parse_batch, line_for
from camoufox.async_api import AsyncCamoufox

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

LOGIN_URL = 'https://x.com/i/flow/login'


def proxy_dict(username, cfg, proxies, attempt=0):
    """Camoufox wants {server,username,password}, not a URL."""
    url = tp.resolve_proxy({'username': username}, cfg, proxies, attempt)
    if not url:
        return None
    rest = url.split('://', 1)[1]
    creds, host = rest.rsplit('@', 1)
    user, pw = creds.split(':', 1)
    return {'server': f'http://{host}', 'username': user, 'password': pw}


async def _type(page, selector, value, delay=60):
    el = await page.wait_for_selector(selector, timeout=25000)
    await el.click()
    await el.type(value, delay=delay)          # keystroke timing matters


async def _next(page):
    """Click whatever advances the flow (Next / Log in)."""
    for sel in ('button[data-testid="ocfEnterTextNextButton"]',
                'button[data-testid="LoginForm_Login_Button"]',
                'div[role="button"]:has-text("Next")',
                'div[role="button"]:has-text("Log in")'):
        try:
            b = await page.query_selector(sel)
            if b and await b.is_enabled():
                await b.click()
                return True
        except Exception:
            continue
    try:                                        # last resort: submit with Enter
        await page.keyboard.press('Enter')
        return True
    except Exception:
        return False


async def login_one(rec, cfg, proxies, headless=True, attempt=0):
    """Returns ((auth_token, ct0), None) or (None, reason)."""
    u = rec['username']
    px = proxy_dict(u, cfg, proxies, attempt)
    try:
        async with AsyncCamoufox(headless=headless, os='windows', humanize=True,
                                 geoip=True, proxy=px) as browser:
            page = await browser.new_page()
            await page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(random.randint(1800, 3200))

            # 1) username
            await _type(page, 'input[autocomplete="username"]', u)
            await _next(page)
            await page.wait_for_timeout(random.randint(1600, 2600))

            # 2) X often interrupts with "enter your phone or email" — feed the email
            try:
                probe = await page.query_selector('input[data-testid="ocfEnterTextTextInput"]')
                if probe:
                    await probe.type(rec['email'], delay=55)
                    await _next(page)
                    await page.wait_for_timeout(random.randint(1500, 2400))
            except Exception:
                pass

            # 3) password
            await _type(page, 'input[name="password"]', rec['password'])
            await _next(page)
            await page.wait_for_timeout(random.randint(2600, 4200))

            # 4) TOTP, if asked
            if rec.get('totp'):
                try:
                    otp_in = await page.query_selector('input[data-testid="ocfEnterTextTextInput"]')
                    if otp_in:
                        code = pyotp.TOTP(rec['totp'].replace(' ', '')).now()
                        await otp_in.type(code, delay=90)
                        await _next(page)
                        await page.wait_for_timeout(random.randint(2600, 4200))
                except Exception:
                    pass

            # 5) read the jar
            for _ in range(12):
                jar = {c['name']: c['value'] for c in await browser.contexts[0].cookies()}
                if jar.get('auth_token') and jar.get('ct0'):
                    return (jar['auth_token'], jar['ct0']), None
                await page.wait_for_timeout(1500)

            url = page.url
            body = (await page.content())[:200].lower()
            if 'arkose' in body or 'captcha' in body:
                return None, 'captcha challenge'
            return None, f'no cookies (stuck at {url[:60]})'
    except Exception as e:
        return None, f'{type(e).__name__}: {str(e)[:90]}'


async def main(path, limit, headless, gap, tries):
    recs = parse_batch(path)
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    existing = {a['username'].lower()
                for a in tp.load_accounts(include_benched=True)}   # dedupe vs ALL lines
    todo = [r for r in recs if r['username'].lower() not in existing]
    print(f'{len(recs)} parsed, {len(recs) - len(todo)} already present, {len(todo)} to do')
    if limit:
        todo = todo[:limit]
    print(f'attempting {len(todo)} (headless={headless})\n', flush=True)

    ok = fail = 0
    for i, rec in enumerate(todo, 1):
        creds = err = None
        for attempt in range(tries):
            creds, err = await login_one(rec, cfg, proxies, headless, attempt)
            if creds:
                break
            print(f'    · attempt {attempt + 1} failed: {err}', flush=True)
        if creds:
            at, ct0 = creds
            with open(tp.ACCOUNTS_FILE, 'a', encoding='utf-8') as fh:
                fh.write(line_for(rec, at, ct0) + '\n')
            ok += 1
            print(f'  ✓ [{i}/{len(todo)}] @{rec["username"]} logged in + added', flush=True)
        else:
            fail += 1
            print(f'  ✗ [{i}/{len(todo)}] @{rec["username"]} FAILED — {err}', flush=True)
        if i < len(todo):
            await asyncio.sleep(random.uniform(*gap))
    print(f'\nadded {ok}, failed {fail}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Harvest X cookies via a real browser')
    ap.add_argument('batch')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--headful', action='store_true', help='show the browser')
    ap.add_argument('--tries', type=int, default=2)
    ap.add_argument('--gap', type=float, nargs=2, default=(8, 20))
    a = ap.parse_args()
    asyncio.run(main(a.batch, a.limit, not a.headful, tuple(a.gap), a.tries))
