r"""
stats_runner.py — poll view/like/bookmark metrics on its own schedule.

Split out of the scheduler because the two jobs compete: the scheduler polls
stats only at the END of a tick, after walking every due account, and each post
takes 10-30s through a residential proxy. With ~107 accounts the tick can run
for many minutes, so stats got polled rarely and irregularly — metrics went 15+
minutes stale and the Gainers history had almost nothing to diff.

Run from a systemd timer instead, so metric freshness is independent of posting
load. The scheduler's own poller should be OFF (`stats_enabled: false`) to avoid
two processes fetching the same tweets.

    python stats_runner.py [--per-tick N]
"""
import sys
import asyncio
import argparse

import twitter_poster as tp
import stats
import store

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def log(msg):
    print(msg, flush=True)


async def _once(per_tick):
    cfg = tp.load_config()
    if per_tick:
        cfg['stats_per_tick'] = per_tick
    proxies = tp.load_proxies()
    accounts = tp.load_accounts()          # re-read so new accounts get picked up
    before = len(store.read_metrics_history())
    await stats.poll_async(accounts, cfg, proxies, log=log)
    after = len(store.read_metrics_history())
    log(f'history rows {before} -> {after}')


async def loop(per_tick, interval):
    """Long-running poller — THIS IS THE CHEAP MODE, don't go back to oneshot.

    Every twikit Client fetches x.com's homepage + JS bundle (~1.5MB) once, to
    derive the x-client-transaction-id. stats.py caches a client per account,
    but that cache is keyed to the event loop — so running this as a systemd
    oneshot (fresh `asyncio.run` per tick) rebuilt every client every tick and
    re-paid that ~1.5MB per account, per run, through a metered residential
    proxy. At a 3-min tick that burned >20GB and exhausted the plan in ~2 days
    (HTTP 402), which silently stopped the whole fleet.

    Staying in one process keeps the loop — and therefore the client cache —
    alive, so the homepage is fetched about once per account per restart.
    """
    log(f'stats loop starting: every {interval}s')
    while True:
        try:
            await _once(per_tick)
        except Exception as e:
            log(f'poll error: {type(e).__name__}: {e}')
        await asyncio.sleep(interval)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Stats poller')
    ap.add_argument('--per-tick', type=int, default=None,
                    help='override stats_per_tick')
    ap.add_argument('--loop', action='store_true',
                    help='run continuously (keeps clients warm — much less proxy traffic)')
    ap.add_argument('--interval', type=int, default=600,
                    help='seconds between polls in --loop mode')
    args = ap.parse_args()
    if args.loop:
        asyncio.run(loop(args.per_tick, args.interval))
    else:
        asyncio.run(_once(args.per_tick))
