r"""
account_ages.py — compare the fleet by ACCOUNT VINTAGE (X signup year).

The bank is bought in batches of different ages (aged 2011-era handles, fresh
2024-era ones, …). Age is the one property that plausibly changes both halves of
the economics, and until now nothing recorded it:

  REACH     does an old handle get more impressions per post?
  SURVIVAL  does it get suspended slower? (accounts are the consumable here —
            a cohort that reaches 2x but dies 3x faster is the worse buy)

    python account_ages.py --fetch     # fill created_at into accounts_meta.json
    python account_ages.py             # print the cohort report
    python account_ages.py --json

`--fetch` asks each account for its OWN profile through its OWN proxy (same
pattern as stats.py) — one request per account, so nothing gets hammered.
"""
import sys
import json
import time
import asyncio
import argparse
import statistics
from datetime import datetime, timezone

import twitter_poster as tp
import stats
import store

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


# Signup dates live in their OWN file, not accounts_meta.json. The stats poller
# does a read-modify-write of the whole meta blob every ~2 min; a one-off writer
# that merges into the same file loses the race and silently drops its work
# (first attempt at this wrote 99 accounts and had 96 of them clobbered seconds
# later). created_at never changes, so it doesn't belong in a rotating file.
AGES_FILE = 'account_created.json'


def _ages_path():
    import os
    return os.path.join(store.DATA_DIR, AGES_FILE)


def read_ages():
    """{username: 'Wed Aug 27 12:00:00 +0000 2011'} — plus anything the stats
    poller happened to record in accounts_meta.json."""
    ages = dict(store.read_json(_ages_path(), {}) or {})
    for u, m in (store.read_meta() or {}).items():
        if m.get('created_at') and u not in ages:
            ages[u] = m['created_at']
    return ages


def _parse_created(s):
    """X serves 'Wed Aug 27 12:00:00 +0000 2011'."""
    if not s:
        return None
    try:
        return datetime.strptime(s, '%a %b %d %H:%M:%S %z %Y')
    except (ValueError, TypeError):
        try:                                   # already ISO (hand-filled)
            return datetime.fromisoformat(str(s))
        except ValueError:
            return None


async def fetch_ages(refetch=False, concurrency=8):
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    accounts = tp.load_accounts(include_benched=True)
    ages = read_ages()
    todo = [a for a in accounts if refetch or not ages.get(a['username'])]
    print(f'{len(accounts)} account(s), {len(todo)} need created_at', flush=True)
    sem = asyncio.Semaphore(concurrency)
    got, failed = [], []

    async def one(acct):
        u = acct['username']
        async with sem:
            c = stats._client_for(acct, cfg, proxies)
            if not c:
                failed.append((u, 'no-cookies'))
                return
            try:
                usr = await c.get_user_by_screen_name(u)
            except Exception as e:
                failed.append((u, type(e).__name__))
                return
            created = getattr(usr, 'created_at', None)
            if not created:
                failed.append((u, 'no-created_at'))
                return
            ages[u] = created
            got.append((u, created))

    await asyncio.gather(*(one(a) for a in todo))
    # re-read before writing: another --fetch may have landed while we were out
    merged = dict(read_ages())
    merged.update(ages)
    store.write_json(_ages_path(), merged)
    print(f'got {len(got)}, failed {len(failed)}, on file {len(merged)}')
    for u, why in failed[:20]:
        print(f'  ✗ @{u}: {why}')
    return len(got), len(failed)


def cohorts():
    """Group accounts by signup year and score each group."""
    accounts = tp.load_accounts(include_benched=True)
    ages = read_ages()
    metrics = store.read_metrics()
    posts = store.read_posts()
    lc = store.read_json(f'{store.DATA_DIR}/account_lifecycle.json', {})
    benched = store.read_benched()
    now = store.now()

    views_by_acct, posts_by_acct, first_post, mature = {}, {}, {}, {}
    # A post's views accrue for days, so comparing a cohort that started
    # yesterday against one that started last week measures START DATE, not
    # reach. MIN_AGE drops posts too fresh to have earned their impressions;
    # MAX_AGE keeps both cohorts inside the same maturity window.
    MIN_AGE, MAX_AGE = 6 * 3600, 72 * 3600
    for p in posts:
        a = p.get('account')
        posts_by_acct[a] = posts_by_acct.get(a, 0) + 1
        first_post[a] = min(first_post.get(a, p.get('ts', now)), p.get('ts', now))
        m = metrics.get(str(p.get('tweet_id'))) or {}
        if m.get('views') is None:
            continue
        views_by_acct.setdefault(a, []).append(m['views'])
        if MIN_AGE <= now - p.get('ts', now) <= MAX_AGE:
            mature.setdefault(a, []).append(m['views'])

    rows, unknown = {}, []
    for a in accounts:
        u = a['username']
        created = _parse_created(ages.get(u))
        if not created:
            unknown.append(u)
            continue
        v = views_by_acct.get(u, [])
        dead = u in benched
        start = first_post.get(u)
        died = (lc.get(u) or {}).get('died')
        rows.setdefault(created.year, []).append({
            'username': u,
            'created': created,
            'posts': posts_by_acct.get(u, 0),
            'views': sum(v),
            'per_post': v,
            'mature': mature.get(u, []),
            'dead': dead,
            # how long this account has been EXPOSED to the risk of suspension —
            # a young cohort with 0 deaths may just not have been alive long enough
            'age_days': (now - start) / 86400.0 if start else None,
            'life_days': ((died or now) - start) / 86400.0 if start else None,
        })

    out = []
    for year in sorted(rows):
        g = rows[year]
        per_post = [x for r in g for x in r['per_post']]
        mat = [x for r in g for x in r['mature']]
        ages = [r['age_days'] for r in g if r['age_days'] is not None]
        dead = [r for r in g if r['dead']]
        # account-days survived, so a young cohort's 0 deaths isn't read as safety
        exposure = sum(r['life_days'] for r in g if r['life_days'] is not None)
        top = max(g, key=lambda r: r['views']) if g else None
        out.append({
            'year': year,
            'accounts': len(g),
            'posts': sum(r['posts'] for r in g),
            'measured_posts': len(per_post),
            'views': sum(r['views'] for r in g),
            'median_views': round(statistics.median(per_post), 1) if per_post else None,
            'mean_views': round(statistics.mean(per_post), 1) if per_post else None,
            'max_views': max(per_post) if per_post else None,
            # like-for-like: only posts 6-72h old, so both cohorts are compared at
            # the same maturity
            'mature_n': len(mat),
            'mature_median': round(statistics.median(mat), 1) if mat else None,
            'mature_mean': round(statistics.mean(mat), 1) if mat else None,
            'mature_p90': round(sorted(mat)[int(len(mat) * 0.9)], 1) if mat else None,
            'views_per_account': round(sum(r['views'] for r in g) / len(g), 1),
            'suspended': len(dead),
            'suspended_pct': round(100.0 * len(dead) / len(g), 1),
            'account_days': round(exposure, 1),
            'suspensions_per_100_acct_days': (round(100.0 * len(dead) / exposure, 2)
                                              if exposure else None),
            'median_days_in_fleet': round(statistics.median(ages), 2) if ages else None,
            'best': top['username'] if top else None,
            'best_views': top['views'] if top else None,
        })
    return out, unknown


def print_report(rows, unknown):
    print(f'\n=== FLEET BY SIGNUP YEAR  {datetime.now().strftime("%Y-%m-%d %H:%M")} ===\n')
    if not rows:
        print('no created_at data yet — run: python account_ages.py --fetch')
        return 1
    n = lambda v: 0 if v is None else v
    print('LIKE-FOR-LIKE REACH  (posts 6-72h old only — views accrue for days,')
    print('                      so raw totals just measure who started earlier)')
    print(f'{"YEAR":<6}{"ACCTS":>6}{"POSTS":>7}{"MEASURED":>9}'
          f'{"MED":>7}{"MEAN":>9}{"P90":>8}{"MAX":>10}')
    for r in rows:
        print(f'{r["year"]:<6}{r["accounts"]:>6}{r["posts"]:>7}{r["mature_n"]:>9}'
              f'{n(r["mature_median"]):>7}{n(r["mature_mean"]):>9}'
              f'{n(r["mature_p90"]):>8}{n(r["max_views"]):>10,}')

    print('\nSURVIVAL  (suspensions per 100 account-days — a young cohort with 0')
    print('           deaths may simply not have been alive long enough yet)')
    print(f'{"YEAR":<6}{"ACCTS":>6}{"SUSPENDED":>11}{"ACCT-DAYS":>11}'
          f'{"PER 100":>9}{"IN FLEET":>10}')
    for r in rows:
        print(f'{r["year"]:<6}{r["accounts"]:>6}'
              f'{r["suspended"]:>6} {r["suspended_pct"]:>4}%{r["account_days"]:>11}'
              f'{n(r["suspensions_per_100_acct_days"]):>9}'
              f'{n(r["median_days_in_fleet"]):>9}d')

    print('\nALL-TIME TOTALS  (skewed by outliers — read the median above instead)')
    for r in rows:
        print(f'  {r["year"]}: {r["views"]:>9,} views over {r["measured_posts"]} '
              f'measured posts · best @{r["best"]} ({n(r["best_views"]):,})')
    tot_a = sum(r['accounts'] for r in rows)
    print(f'\n{tot_a} accounts with a known signup date')
    if unknown:
        print(f'{len(unknown)} without created_at (re-run --fetch): '
              + ', '.join(unknown[:10]) + (' …' if len(unknown) > 10 else ''))
    return 0


def main():
    ap = argparse.ArgumentParser(description='Compare fleet cohorts by X signup year')
    ap.add_argument('--fetch', action='store_true', help='fill created_at from X')
    ap.add_argument('--refetch', action='store_true', help='re-fetch even if known')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    if args.fetch or args.refetch:
        asyncio.run(fetch_ages(refetch=args.refetch))
    rows, unknown = cohorts()
    if args.json:
        print(json.dumps({'cohorts': rows, 'unknown': unknown}, indent=2))
        return 0
    return print_report(rows, unknown)


if __name__ == '__main__':
    sys.exit(main())
