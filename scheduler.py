r"""
PORNYARD TWITTER PROMO — scheduler (3x/day per account).

Long-running loop, mirrors redgif_scheduler.py. Each account gets `posts_per_day`
posts spread across the configured time windows. Within a window the exact minute
is chosen deterministically per (account, day, window) so a restart never
double-posts and accounts don't all fire at the same minute.

State (which windows already posted today) is persisted to _state.json, so you
can kill/restart the process freely.

Usage:
    python scheduler.py            # DRY RUN — logs the plan + what it *would* post
    python scheduler.py --live     # actually post

Run it under whatever keeps it alive (nohup / a systemd unit / Windows Task
Scheduler "at log on"). It is single-instance (a .lock file guards against two
copies running at once).
"""
import os
import sys
import json
import time
import random
import asyncio
import argparse
import subprocess
from datetime import datetime, date
from zoneinfo import ZoneInfo

import twitter_poster as tp
import posting_safety
import stats

# --- arm test (10-23): hold caption constant, vary ONLY the watermark ---
_ARMS = {
    "M": ("/root/x_bank_top500", "/root/sfw_pool_clean500"),  # 10-23: flipped to CLEAN (marked arm was killing accts)
    "C": ("/root/x_bank_top500", "/root/sfw_pool_clean500"),                                # clean no-mark
}
def _arm_cfg(cfg, user, log=None):
    import hashlib
    arm = "M" if int(hashlib.md5(user.encode()).hexdigest(), 16) % 2 == 0 else "C"
    md, sd = _ARMS[arm]
    c = dict(cfg); c["media_dir"] = md; c["sfw_media_dir"] = sd
    if log: log(f"  @{user} ARM={arm} media={md}")
    return c
# --- end arm test ---

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, '_state.json')
LOCK_FILE = os.path.join(HERE, '_scheduler.lock')


def log(msg):
    print(f'[{datetime.now().strftime("%m-%d %H:%M:%S")}] {msg}', flush=True)


# --------------------------------------------------------------------------- #
# State                                                                       #
# --------------------------------------------------------------------------- #
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def prune_state(state, keep_date):
    """Drop everything except today's record per account."""
    for user, days in list(state.items()):
        state[user] = {k: v for k, v in days.items() if k == keep_date}


# --------------------------------------------------------------------------- #
# Schedule maths                                                              #
# --------------------------------------------------------------------------- #
def hhmm(minute):
    minute %= 1440
    return f'{minute // 60:02d}:{minute % 60:02d}'


def daily_targets(username, day_str, cfg):
    """The day's post-times (minutes-from-midnight) for one account.

    `posts_per_day` posts spaced `interval_hours` apart (default 3 x 8h), with a
    per-account random anchor so every account sits at a different point on the
    clock — they each post on their own 8h cadence but never in lockstep, so it
    doesn't look like a coordinated burst.
    """
    ppd = int(cfg.get('posts_per_day', 3))
    interval = int(cfg.get('interval_hours', 8)) * 60
    rng = random.Random(f'{username}|{day_str}|anchor')
    # Full-day anchor. Posts are taken mod 1440, so for a schedule that tiles the
    # day (3 x 8h) drawing from [0,1440) is distributionally identical to
    # [0,interval). But when ppd * interval < 1440 (e.g. 1/day) only the full
    # range spreads the fleet over all 24h instead of bunching every account
    # into the first 8.
    anchor = rng.randint(0, 1439)
    return sorted((anchor + i * interval) % 1440 for i in range(ppd))


def log_plan(accounts, cfg, tz):
    today = datetime.now(tz).strftime('%Y-%m-%d')
    ppd = int(cfg.get('posts_per_day', 3))
    iv = int(cfg.get('interval_hours', 8))
    log(f'today ({today}) plan — {len(accounts)} account(s) x {ppd} posts, ~{iv}h apart:')
    for a in accounts:
        times = [hhmm(m) for m in daily_targets(a['username'], today, cfg)]
        log(f'  @{a["username"]}: {", ".join(times)} {tz.key}')


# --------------------------------------------------------------------------- #
# Single instance                                                             #
# --------------------------------------------------------------------------- #
def single_instance():
    if os.path.exists(LOCK_FILE):
        try:
            pid = int(open(LOCK_FILE).read().strip())
            if sys.platform.startswith('win'):
                out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                     capture_output=True, text=True).stdout
                alive = str(pid) in out
            else:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                log(f'another scheduler already running (PID {pid}) — exiting')
                sys.exit(0)
        except ValueError:
            pass
    open(LOCK_FILE, 'w').write(str(os.getpid()))


# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #
def run(live):
    # Operator kill switch. Flip PUBLISHING_ENABLED in posting_safety.py
    # to stop every publisher cleanly (this replaced a gate that referenced
    # the module without it existing, so it raised NameError instead).
    if live and not posting_safety.enabled():
        log(posting_safety.block_reason())
        return 2
    single_instance()
    cfg = tp.load_config()
    accounts = tp.load_accounts()
    tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))

    log(f'SCHEDULER start — {"LIVE" if live else "DRY RUN"} — '
        f'{len(accounts)} account(s), {cfg.get("posts_per_day")} posts/day, tz={tz.key}')
    if not live:
        log('DRY RUN: nothing will actually be posted (pass --live to post)')
    log_plan(accounts, cfg, tz)

    state = load_state()
    last_plan_day = None
    last_roster = None

    while True:
        # Re-read config + accounts every tick so edits to accounts.txt (add/remove,
        # e.g. from the dashboard) are picked up LIVE without a restart.
        cfg = tp.load_config()
        accounts = tp.load_accounts()
        proxies = tp.load_proxies()
        tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
        interval = int(cfg.get('check_interval_seconds', 300))
        grace = int(cfg.get('catchup_grace_minutes', 120))

        now = datetime.now(tz)
        day_str = now.strftime('%Y-%m-%d')
        now_min = now.hour * 60 + now.minute

        roster = tuple(sorted(a['username'] for a in accounts))
        if roster != last_roster and last_roster is not None:
            log(f'roster changed: now {len(roster)} account(s)')
            log_plan(accounts, cfg, tz)
        last_roster = roster

        if day_str != last_plan_day:
            prune_state(state, day_str)
            save_state(state)
            if last_plan_day is not None:        # new day rolled over while running
                log(f'--- new day {day_str} ---')
                log_plan(accounts, cfg, tz)
            last_plan_day = day_str

        # A pass costs (stagger x due count). With a fixed 30-120s stagger
        # and a large roster that exceeds the grace window, so accounts late
        # in the list had their slots expire before the loop reached them --
        # 119 slots were dropped that way on 09-17. Size the stagger to the
        # actual workload so the pass finishes inside the grace window.
        _due = 0
        for _a in accounts:
            _p = state.setdefault(_a['username'], {}).setdefault(day_str, [])
            for _i, _t in enumerate(daily_targets(_a['username'], day_str, cfg)):
                if _i not in _p and _t <= now_min <= _t + grace:
                    _due += 1
        _bud = float(cfg.get("stagger_grace_budget", 0.55))
        _per = (grace * 60 * _bud / _due) if _due else None
        if _due:
            _fl = int(cfg.get('stagger_floor_seconds', 10))
            _lo = max(_fl, min(int(cfg.get('min_seconds_between_accounts', 30)),
                               int(_per)))
            _hi = max(_lo, min(int(cfg.get('max_seconds_between_accounts', 120)),
                               int(_per * 1.5)))
            log(f'tick: {_due} slot(s) due — stagger {_lo}-{_hi}s '
                f'(pass ~{int(_due * (_lo + _hi) / 2 / 60)}min, grace {grace}min)')

        for acct in accounts:
            user = acct['username']
            posted = state.setdefault(user, {}).setdefault(day_str, [])
            targets = daily_targets(user, day_str, cfg)
            for i, target in enumerate(targets):
                if i in posted:
                    continue
                # the pass takes real time, so re-read the clock rather
                # than judging every slot against a now_min captured at
                # the top of the tick
                _n2 = datetime.now(tz)
                now_min = _n2.hour * 60 + _n2.minute
                if now_min < target:
                    continue
                if now_min > target + grace:
                    # slot long gone (e.g. scheduler started late) — skip, don't burst
                    posted.append(i)
                    save_state(state)
                    log(f'  @{user} slot {i + 1} ({hhmm(target)}) missed by >{grace}m — skipping today')
                    continue

                log(f'>>> @{user} due for post {i + 1}/{len(targets)} (target {hhmm(target)})')
                try:
                    ok, detail = asyncio.run(
                        tp.post_for_account(acct, cfg, proxies, dry_run=not live))
                except Exception as e:
                    ok, detail = False, f'crash:{type(e).__name__}:{e}'
                    log(f'  post crashed: {e}')

                if ok:
                    posted.append(i)
                    save_state(state)
                else:
                    log(f'  @{user} slot {i + 1} not marked done (detail={detail}) — will retry next tick')

                # stagger accounts so they don't all post in the same minute
                lo = int(cfg.get('min_seconds_between_accounts', 30))
                hi = int(cfg.get('max_seconds_between_accounts', 120))
                if _per is not None:
                    # shrink (never below the floor) so the pass fits the
                    # grace window; a big roster must not starve itself
                    floor = int(cfg.get('stagger_floor_seconds', 10))
                    lo = max(floor, min(lo, int(_per)))
                    hi = max(lo, min(hi, int(_per * 1.5)))
                if live:
                    time.sleep(random.randint(min(lo, hi), max(lo, hi)))

        # refresh a slice of view/follower stats for the dashboard
        if cfg.get('stats_enabled', True):
            try:
                asyncio.run(stats.poll_async(accounts, cfg, proxies, log=log))
            except Exception as e:
                log(f'stats poll error: {type(e).__name__}: {e}')

        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description='Pornyard Twitter promo scheduler (3x/day per account)')
    ap.add_argument('--live', action='store_true', help='actually post (default is a dry run)')
    args = ap.parse_args()
    try:
        run(args.live)
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == '__main__':
    main()
