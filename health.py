r"""Public account observations and historical activity report.

Checks use one unauthenticated public request per account. Ambiguous pages stay
unknown, and readable profiles do not prove permission to publish. This command
never posts, changes cookies, retries proxy exits, or clears restrictions.

    python health.py                 # human-readable report
    python health.py --json          # machine-readable
"""
import os
import re
import sys
import json
import time
import asyncio
import argparse
import statistics
import urllib.request
from datetime import datetime

import twitter_poster as tp
import store
import media_ledger
import account_evidence
import bank_creatives as bc

from twikit import Client
from twikit.client.v11 import Endpoint
from twikit.errors import (Unauthorized, Forbidden, NotFound,
                           AccountSuspended, AccountLocked)

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')


def public_status(username, cfg, proxies, tries=2):
    """One public read with saved evidence; no cookie use or proxy retries."""
    proxy = tp.resolve_proxy({'username': username, 'auth_token': None}, cfg, proxies, 0)
    evidence = account_evidence.fetch_public_evidence(username, proxy=proxy)
    account_evidence.persist_evidence(username, evidence)
    return {
        'suspended': 'SUSPENDED',
        'readable': 'PUBLIC-READABLE (publishing unverified)',
        'not_found': 'NOT-FOUND',
        'login_required': 'LOGIN-REQUIRED',
        'rate_limited': 'RATE-LIMITED',
        'unavailable': 'UNAVAILABLE',
    }.get(evidence['status'], 'UNKNOWN')


async def check_account(acct, cfg, proxies, tries=2):
    """Public status is independent of stored cookies and publishing rights."""
    return public_status(acct['username'], cfg, proxies)


LIFECYCLE = 'account_lifecycle.json'


def _lc_path():
    return os.path.join(store.DATA_DIR, LIFECYCLE)


def update_lifecycle(statuses, posts):
    """Retain restriction history until explicit, verified account recovery.

    OK-VERIFIED is reserved for an operator-confirmed recovery; ordinary public
    visibility and successful session reads never produce that status here.
    Updating this history does not remove an account from the bench.
    """
    lc = store.read_json(_lc_path(), {})
    now = store.now()
    first_post = {}
    for p in posts:
        a = p.get('account')
        if a:
            first_post[a] = min(first_post.get(a, p.get('ts', now)), p.get('ts', now))
    for u, st in statuses.items():
        rec = lc.setdefault(u, {'first_seen': now})
        if u in first_post:
            rec['first_post'] = first_post[u]
        dead = st.startswith(('SUSPENDED', 'LOCKED'))
        if dead and not rec.get('died'):
            rec['died'] = now
            rec['died_status'] = st
        # Readable profiles and transient errors cannot verify reinstatement.
        # OK-VERIFIED is an explicit operator-verified recovery status.
        if st == 'OK-VERIFIED' and rec.get('died'):
            rec.pop('died', None)
            rec.pop('died_status', None)
        rec['last_status'] = st
        rec['last_seen'] = now
    store.write_json(_lc_path(), lc)
    return lc


def sync_bench(statuses):
    """Persist restrictions without ever automatically restoring eligibility.

    Profile reads and account settings do not verify enforcement resolution.
    The second return list is retained for report compatibility and stays empty.
    """
    benched = store.read_benched()
    observations = account_evidence.read_evidence()
    newly, revived = [], []
    for u, st in statuses.items():
        dead = st.startswith('SUSPENDED') or st.startswith('LOCKED')
        evidence = observations.get(u, {})
        if st == 'SUSPENDED' and evidence.get('status') == 'suspended':
            if account_evidence.bench_from_evidence(u, evidence):
                newly.append(u)
            continue
        if dead and u not in benched:
            store.bench_account(u, st, source='health')
            newly.append(u)
    return newly, revived


def survival_summary(lc):
    """Historical detections only; absence of a record is not verified survival."""
    now = store.now()
    alive = [u for u, r in lc.items() if not r.get('died')]
    dead = [u for u, r in lc.items() if r.get('died')]
    lifespans = []
    for u, r in lc.items():
        start = r.get('first_post') or r.get('first_seen')
        end = r.get('died') or now
        if start:
            lifespans.append((end - start) / 86400.0)
    total = len(lc) or 1
    return {
        'tracked': len(lc), 'without_recorded_restriction': len(alive),
        'recorded_restrictions': len(dead),
        'without_recorded_restriction_pct': round(100.0 * len(alive) / total, 1),
        'restriction_names': sorted(dead),
        'interpretation': 'Historical flags include unverified legacy detections; no current eligibility conclusion.',
        'median_age_days': round(statistics.median(lifespans), 2) if lifespans else None,
        'median_interval_to_detection_days': (
            round(statistics.median([
                ((lc[u].get('died') or now) - (lc[u].get('first_post') or lc[u].get('first_seen', now))) / 86400.0
                for u in dead]), 2) if dead else None),
    }


def build_report(cfg, accounts, statuses):
    tz_now = int(time.time())
    posts = store.read_posts()
    metrics = store.read_metrics()

    # --- per-account activity -------------------------------------------- #
    # Reach must only describe the CURRENT fleet's RECENT posts. The posts log is
    # append-only and still holds the 2026-06 run from accounts that are now dead
    # and were ghosted at ~1 view — averaging those in makes every report scream
    # "GHOSTED" forever and would mask a real ghosting later.
    # benched accounts are listed in the table but must not skew the numbers:
    # their tweets are gone with the handle, so they'd drag the reach median and
    # inflate the clip-pool requirement for a fleet that no longer posts.
    active = [a for a in accounts if not a.get('benched')]
    live = {a['username'] for a in active}
    window = int(cfg.get('reach_window_days', 14)) * 86400
    # a brand-new post has no impressions yet; ignore anything too fresh to judge
    min_age = int(cfg.get('reach_min_age_hours', 6)) * 3600

    last_post, counts, views = {}, {}, []
    for p in posts:
        a = p.get('account')
        counts[a] = counts.get(a, 0) + 1
        last_post[a] = max(last_post.get(a, 0), p.get('ts', 0))
        age = tz_now - p.get('ts', 0)
        if a not in live or age > window or age < min_age:
            continue
        m = metrics.get(p.get('tweet_id'))
        if m and m.get('views') is not None:
            views.append(m['views'])

    # --- clip pool -------------------------------------------------------- #
    counts_c = bc.counts()
    # Clips consumed inside one reuse window = accounts x posts/day x window-days.
    # Leaving posts_per_day out of this (as an earlier version did) understates the
    # requirement by exactly that factor and reports a starved pool as healthy.
    gap_days = max(1, int(cfg.get('media_reuse_gap_hours', 48)) / 24)
    per_day = max(1, int(cfg.get('posts_per_day', 1)))
    needed = int(len(active) * per_day * gap_days)
    pool_ok = counts_c['enabled'] >= needed

    lc = update_lifecycle(statuses, posts)
    newly, revived = sync_bench(statuses)
    return {
        'ts': tz_now,
        'survival': survival_summary(lc),
        'bench': {'benched_now': newly, 'revived': revived,
                  'total': len(store.read_benched())},
        'accounts': [{
            'username': a['username'],
            'status': statuses[a['username']],
            'benched': bool(a.get('benched')),
            'posts': counts.get(a['username'], 0),
            'last_post_ts': last_post.get(a['username']),
            'days_since_post': (round((tz_now - last_post[a['username']]) / 86400, 1)
                                if last_post.get(a['username']) else None),
        } for a in accounts],
        'pool': {'enabled': counts_c['enabled'], 'total': counts_c['total'],
                 'needed': needed, 'ok': pool_ok, 'per_day': per_day,
                 'accounts': len(active),
                 'reuse_gap_hours': cfg.get('media_reuse_gap_hours', 48)},
        'reach': {
            'posts_measured': len(views),
            'median_views': round(statistics.median(views), 1) if views else None,
            'max_views': max(views) if views else None,
            'window_days': int(cfg.get('reach_window_days', 14)),
        },
    }


def print_report(rep, stale_days):
    bad, warn = [], []
    print(f'\n=== FLEET HEALTH  {datetime.now().strftime("%Y-%m-%d %H:%M")} ===\n')
    print(f'{"ACCOUNT":<18}{"STATUS":<26}{"POSTS":>6}  LAST POST')
    for a in rep['accounts']:
        d = a['days_since_post']
        last = 'never' if d is None else f'{d}d ago'
        flag = ''
        st = a['status']
        # Only a genuinely unusable account is "needs attention". A rotated ct0
        # self-heals on the next post, and a flaky exit moves between runs —
        # listing those as failures trains you to ignore the report.
        fatal = ('SUSPENDED', 'LOCKED', 'DEAD-COOKIES', 'NO-COOKIES', 'AUTH-REVIEW-REQUIRED')
        if any(st.startswith(f) for f in fatal):
            if a.get('benched'):
                # already out of rotation — costs nothing, so it isn't an alert
                # any more. Keeping it in `bad` would make the daily exit-1 fire
                # forever over accounts that are already handled.
                warn.append(f"@{a['username']}: {st} (benched — not posting)")
                flag = '  <-- benched'
            else:
                bad.append(f"@{a['username']}: {st}")
                flag = '  <-- '
        elif not st.startswith(('OK', 'PUBLIC-READABLE')):
            warn.append(f"@{a['username']}: {st} (requires review; publishing disabled)")
        elif d is not None and d > stale_days:
            warn.append(f"@{a['username']}: silent {d}d")
            flag = '  <-- quiet'
        elif 'PROTECTED' in a['status']:
            warn.append(f"@{a['username']}: protected")
        print(f'{a["username"]:<18}{a["status"]:<26}{a["posts"]:>6}  {last}{flag}')

    p = rep['pool']
    print(f'\nCLIP POOL   {p["enabled"]} enabled / {p["total"]} total  '
          f'(need {p["needed"]} = {p.get("accounts", len(rep["accounts"]))} posting accounts x {p["per_day"]}/day '
          f'x {p["reuse_gap_hours"]}h gap)')
    if not p['ok']:
        bad.append(f'clip pool too small: {p["enabled"]} < {p["needed"]} — posts will repeat')
        print('   ⚠ TOO SMALL — the ledger will start recycling clips (duplicate-media risk).')
        print('     Fix: Creatives tab -> "Refresh from bank", or import more local clips.')

    b = rep.get('bench') or {}
    if b.get('total'):
        print(f'\nBENCH       {b["total"]} account(s) out of the posting rotation')
    if b.get('benched_now'):
        print(f'   + benched this run: {", ".join(b["benched_now"])}')
    if b.get('revived'):
        print(f'   ↩ back in rotation: {", ".join(b["revived"])}')

    s = rep['survival']
    print(f'\nHISTORY     {s["without_recorded_restriction"]}/{s["tracked"]} without a recorded restriction ({s["without_recorded_restriction_pct"]}%)  '
          f'median fleet age {s["median_age_days"]}d')
    print('   ' + s['interpretation'])
    if s['recorded_restrictions']:
        print(f'   recorded restrictions {s["recorded_restrictions"]}: {", ".join(s["restriction_names"][:8])}'
              + (' …' if len(s['restriction_names']) > 8 else ''))
        if s['median_interval_to_detection_days'] is not None:
            print(f'   median interval to restriction detection: {s["median_interval_to_detection_days"]}d '
                  f'— historical detection intervals; not suspension onset')

    r = rep['reach']
    if r['median_views'] is not None:
        print(f'REACH       median {r["median_views"]} views over {r["posts_measured"]} '
              f'posts (last {r["window_days"]}d, current fleet; max {r["max_views"]})')
        if r['posts_measured'] >= 5 and r['median_views'] <= 2:
            warn.append(f'median {r["median_views"]} views — low recorded reach; no enforcement conclusion')
            print('   Low recorded reach alone cannot establish a visibility restriction.')
    else:
        print('REACH       no view data yet (stats poller warms up after a few posts)')

    print()
    if bad:
        print('NEEDS ATTENTION:')
        for b in bad:
            print('  ✗ ' + b)
    if warn:
        print('WATCH:')
        for w in warn:
            print('  · ' + w)
    if not bad and not warn:
        print('✓ all good')
    return 1 if bad else 0


async def main(as_json, stale_days):
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    # Retain restricted accounts in the report; checks never unbench them.
    accounts = tp.load_accounts(include_benched=True)
    statuses = {}
    for a in accounts:
        statuses[a['username']] = await check_account(a, cfg, proxies)
    rep = build_report(cfg, accounts, statuses)
    if as_json:
        print(json.dumps(rep, indent=2))
        healthy = all(x['status'].startswith(('OK', 'PUBLIC-READABLE')) or x.get('benched')
                      for x in rep['accounts']) and rep['pool']['ok']
        return 0 if healthy else 1
    return print_report(rep, stale_days)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Daily fleet health check')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--stale-days', type=float, default=2.0,
                    help='warn if an account has not posted in this many days')
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.json, args.stale_days)))
