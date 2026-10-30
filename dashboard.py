r"""
Dashboard — local web panel for the promo bot.

    python dashboard.py            # http://127.0.0.1:8787

Read-only on the bot's data files (posts/metrics/totals/profiles + scheduler
state) plus add/remove accounts (which the scheduler picks up live). Binds to
127.0.0.1 only — it can edit accounts.txt, so it must not be exposed.
"""
import os
import sys
import json
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import twitter_poster as tp
import scheduler as sch
import store
import posting_safety
import account_evidence
import bank_creatives as bc


# --------------------------------------------------------------------------- #
# Creatives — Redgif-bank clip management (background refresh)                 #
# --------------------------------------------------------------------------- #
_refresh = {'running': False, 'msg': '', 'ts': 0}
_refresh_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Gainers — who/what gained the most views in a window                         #
# --------------------------------------------------------------------------- #
PERIODS = {'1h': 3600, '4h': 14400, '24h': 86400, '7d': 604800}
_hist_cache = {'key': None, 'rows': []}


def _history():
    """Parsed history, cached on (mtime,size) — the file gets big and the UI
    polls often, so re-parsing it every request would peg the CPU."""
    p = os.path.join(store.DATA_DIR, 'metrics_history.jsonl')
    try:
        st = os.stat(p)
        key = (st.st_mtime, st.st_size)
    except OSError:
        return []
    if _hist_cache['key'] != key:
        _hist_cache['rows'] = store.read_jsonl(p)
        _hist_cache['key'] = key
    return _hist_cache['rows']


def gainers(period='24h', limit=25):
    secs = PERIODS.get(period, 86400)
    now_ts = store.now()
    cutoff = now_ts - secs
    rows = _history()

    # Baseline = the LAST snapshot at or BEFORE the cutoff. Using the first
    # snapshot *after* the cutoff (an earlier version did) both under-measures
    # the window and silently drops every tweet that has no snapshot inside it —
    # which is why short windows read "+0" while views were obviously climbing.
    base, earliest, last = {}, {}, {}
    for r in rows:
        tid, ts = r.get('i'), r.get('ts', 0)
        if not tid:
            continue
        if ts <= cutoff and (tid not in base or ts > base[tid]['ts']):
            base[tid] = r
        if tid not in earliest or ts < earliest[tid]['ts']:
            earliest[tid] = r
        if tid not in last or ts > last[tid]['ts']:
            last[tid] = r

    metrics = store.read_metrics()
    posts = {str(p.get('tweet_id')): p for p in store.read_posts()}
    out = []
    for tid in set(list(last) + [str(p.get('tweet_id')) for p in posts.values()]):
        p = posts.get(tid, {})
        hi = last.get(tid)
        cur = metrics.get(tid) or {}
        v_now = cur.get('views') if cur.get('views') is not None else (hi or {}).get('v', 0)
        k_now = cur.get('bookmarks') if cur.get('bookmarks') is not None else (hi or {}).get('k', 0)
        if v_now is None and k_now is None:
            continue

        if (p.get('ts') or 0) >= cutoff:
            v0 = k0 = 0          # posted inside the window: all of it is gain
        elif tid in base:
            v0, k0 = base[tid].get('v', 0), base[tid].get('k', 0)
        elif tid in earliest:
            v0, k0 = earliest[tid].get('v', 0), earliest[tid].get('k', 0)
        else:
            continue             # no history and not new — nothing to diff

        dv = max(0, (v_now or 0) - v0)
        dk = max(0, (k_now or 0) - k0)
        if dv <= 0 and dk <= 0:
            continue
        clip = (p.get('media') or '').rsplit('.mp4', 1)[0] or None
        out.append({
            'tweet_id': tid, 'account': p.get('account'), 'url': p.get('url'),
            'clip': clip, 'text': p.get('text'), 'ts': p.get('ts'),
            'views_now': v_now, 'gain_views': dv,
            'bookmarks_now': k_now, 'gain_bookmarks': dk,
            'rate_per_hr': round(dv / (secs / 3600.0), 1),
        })
    out.sort(key=lambda x: -x['gain_views'])

    # same deltas rolled up per CLIP — the actionable view, since a clip that
    # gains across several accounts is a creative that works, not a fluke post
    by_clip = {}
    for g in out:
        if not g['clip']:
            continue
        c = by_clip.setdefault(g['clip'], {'clip': g['clip'], 'gain_views': 0,
                                           'gain_bookmarks': 0, 'posts': 0,
                                           'accounts': set(), 'top_gain': -1,
                                           'top_url': None, 'top_account': None})
        c['gain_views'] += g['gain_views']
        c['gain_bookmarks'] += g['gain_bookmarks']
        c['posts'] += 1
        c['accounts'].add(g['account'])
        if g['gain_views'] > c['top_gain']:      # best single post for this clip
            c['top_gain'] = g['gain_views']
            c['top_url'] = g.get('url')
            c['top_account'] = g.get('account')
    clips = sorted(by_clip.values(), key=lambda c: -c['gain_views'])[:limit]
    for c in clips:
        c['accounts'] = len(c['accounts'])
        c['rate_per_hr'] = round(c['gain_views'] / (secs / 3600.0), 1)
        # Only advertise a video link the dashboard can actually serve.
        # /api/creatives/video reads CREATIVES_DIR, so check exactly there —
        # a link that 404s is worse than no link.
        _vp = os.path.join(bc.CREATIVES_DIR, os.path.basename(c['clip']) + '.mp4')
        c['has_video'] = os.path.exists(_vp) or os.path.exists(_vp + '.disabled')

    return {'period': period, 'window_hours': round(secs / 3600, 1),
            'posts': out[:limit], 'clips': clips,
            'total_gain': sum(g['gain_views'] for g in out),
            'tracked': len(out),
            'history_rows': len(rows)}


def account_detail(username):
    """Everything the drawer needs for one account: profile, its posts with full
    metrics, and the clip each post used."""
    username = (username or '').lstrip('@')
    accounts = {a['username']: a for a in tp.load_accounts(include_benched=True)}
    acct = accounts.get(username)
    if acct is None:
        # tolerate a case difference between the panel and accounts.txt
        _ci = {k.lower(): v for k, v in accounts.items()}
        acct = _ci.get(username.lower())
        if acct is not None:
            username = acct['username']
    if acct is None:
        # STANDALONE TEST ARMS (2026-10-26): test cohorts run outside the fleet
        # write to posts.jsonl but are deliberately never added to accounts.txt
        # (so the scheduler cannot contaminate an arm). The drawer used to 404 on
        # every one of them. Resolve any handle that has posts; 404 only if none.
        _seen = {}
        for _p in store.read_posts():
            _a = _p.get('account') or ''
            if _a:
                _seen.setdefault(_a.lower(), _a)
        _real = _seen.get(username.lower())
        if not _real:
            return None
        username = _real
        acct = {}
    cfg = tp.load_config()
    meta = store.read_meta().get(username, {})
    metrics = store.read_metrics()

    posts = []
    for p in sorted((x for x in store.read_posts() if x.get('account') == username),
                    key=lambda x: -x.get('ts', 0)):
        m = metrics.get(str(p.get('tweet_id'))) or {}
        views = m.get('views') or 0
        likes = m.get('likes') or 0
        bm = m.get('bookmarks') or 0
        clip = (p.get('media') or '').rsplit('.mp4', 1)[0] or None
        posts.append({
            'ts': p.get('ts'), 'tweet_id': str(p.get('tweet_id')),
            'url': p.get('url'), 'text': p.get('text'), 'media': p.get('media'),
            'clip': clip,
            'views': m.get('views'), 'likes': m.get('likes'),
            'retweets': m.get('retweets'), 'replies': m.get('replies'),
            'quotes': m.get('quotes'), 'bookmarks': m.get('bookmarks'),
            'metrics_ts': m.get('ts'),
            # bookmark:like is the signal that actually predicts a banger
            'bm_ratio': round(100.0 * bm / likes, 1) if likes else None,
            'eng_rate': round(100.0 * (likes + bm) / views, 2) if views else None,
        })

    tot = {k: sum((p[k] or 0) for p in posts)
           for k in ('views', 'likes', 'retweets', 'replies', 'bookmarks')}
    return {
        'username': username,
        'name': meta.get('name'),
        'followers': meta.get('followers'), 'following': meta.get('following'),
        'statuses': meta.get('statuses'), 'meta_ts': meta.get('ts'),
        'has_cookies': bool(acct.get('auth_token')),
        'benched': acct.get('benched'),
        'restriction': account_evidence.summarize_account(acct.get('benched'), account_evidence.read_evidence().get(username)),
        'evidence': account_evidence.read_evidence().get(username),
        'publishing': posting_safety.status(),
        'proxy_sessid': tp.account_sessid(username),
        'posts': posts, 'totals': tot,
        'profile_url': f'https://x.com/{username}',
    }


# --- one-off "post now" for a single account ------------------------------- #
_posting = {}          # username -> {'running': bool, 'msg': str}


def start_post_now(username):
    if not posting_safety.enabled():
        return False, posting_safety.block_reason()
    username = (username or '').lstrip('@')
    accounts = {a['username']: a for a in tp.load_accounts(include_benched=True)}
    if username not in accounts:
        return False, f'@{username} not in accounts.txt'
    bench = accounts[username].get('benched')
    if bench:
        return False, f'@{username} is benched ({bench.get("reason", "suspended")}) — not posting'
    st = _posting.get(username)
    if st and st.get('running'):
        return False, 'already posting'
    _posting[username] = {'running': True, 'msg': 'posting…'}

    def run():
        try:
            cfg = tp.load_config()
            proxies = tp.load_proxies()
            acct = {a['username']: a for a in tp.load_accounts()}[username]
            ok, detail = asyncio.run(
                tp.post_for_account(acct, cfg, proxies, dry_run=False))
            _posting[username] = {'running': False,
                                  'msg': ('✓ ' if ok else '✗ ') + detail}
        except Exception as e:
            _posting[username] = {'running': False,
                                  'msg': f'✗ {type(e).__name__}: {e}'}

    threading.Thread(target=run, daemon=True).start()
    return True, 'posting started'


def start_refresh(limit=None):
    with _refresh_lock:
        if _refresh['running']:
            return False, 'refresh already running'
        _refresh['running'] = True
        _refresh['msg'] = 'syncing from bank…'

    def run():
        try:
            r = bc.sync_bank(limit=limit)
            _refresh['msg'] = r.get('msg', 'done')
        except Exception as e:
            _refresh['msg'] = f'{type(e).__name__}: {e}'
        finally:
            _refresh['running'] = False
            _refresh['ts'] = store.now()

    threading.Thread(target=run, daemon=True).start()
    return True, 'refresh started'

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = tp.ACCOUNTS_FILE


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #
def _totals_at(totals, target_ts):
    """The totals snapshot with the largest ts <= target_ts (else earliest)."""
    older = [t for t in totals if t.get('ts', 0) <= target_ts]
    if older:
        return older[-1]
    return totals[0] if totals else None


def _window_delta(totals, latest, seconds, key='views'):
    if not latest:
        return 0
    base = _totals_at(totals, latest['ts'] - seconds)
    if not base:
        return 0
    return max(0, latest.get(key, 0) - base.get(key, 0))


def build_state():
    cfg = tp.load_config()
    publishing = posting_safety.status()
    # benched accounts stay in the panel — you want to SEE that 12 handles got
    # suspended, not have them vanish — but they're flagged and dropped from the
    # schedule, since nothing will ever post for them again.
    accounts = tp.load_accounts(include_benched=True)
    tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
    posts = store.read_posts()
    metrics = store.read_metrics()
    meta = store.read_meta()
    totals = sorted(store.read_totals(), key=lambda t: t.get('ts', 0))
    sched_state = sch.load_state()
    evidence_by_account = account_evidence.read_evidence()

    now = datetime.now(tz)
    now_ts = store.now()
    now_min = now.hour * 60 + now.minute
    today = now.strftime('%Y-%m-%d')

    # per-account post counts + views/likes (from latest metrics)
    by_acct = {}
    posts_today_by = {}
    for p in posts:
        a = p.get('account')
        d = by_acct.setdefault(a, {'posts': 0, 'views': 0, 'likes': 0,
                                   'retweets': 0, 'bookmarks': 0})
        d['posts'] += 1
        if datetime.fromtimestamp(p.get('ts', 0), tz).strftime('%Y-%m-%d') == today:
            posts_today_by[a] = posts_today_by.get(a, 0) + 1
        m = metrics.get(p.get('tweet_id'))
        if m:
            d['views'] += m.get('views', 0)
            d['likes'] += m.get('likes', 0)
            d['retweets'] += m.get('retweets', 0)
            d['bookmarks'] += m.get('bookmarks', 0)

    # schedule: next post time per account
    ppd = int(cfg.get('posts_per_day', 3))

    def next_for(user):
        targets = sch.daily_targets(user, today, cfg)
        posted = set(sched_state.get(user, {}).get(today, []))
        for i, m in enumerate(targets):
            if i not in posted and m >= now_min:
                return m, (m - now_min)            # later today
        tmrw_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        tmrw = sch.daily_targets(user, tmrw_str, cfg)
        m = min(tmrw) if tmrw else 0
        return m, (1440 - now_min) + m

    accounts_out = []
    for a in accounts:
        u = a['username']
        m = meta.get(u, {})
        agg = by_acct.get(u, {})
        nxt_min, nxt_in = next_for(u)
        posted_today = posts_today_by.get(u, 0)   # real posts today, from the log
        bench = a.get('benched')
        accounts_out.append({
            'username': u,
            'benched': bool(bench),
            'restriction': account_evidence.summarize_account(bench, evidence_by_account.get(u)),
            'evidence': evidence_by_account.get(u),
            'bench_reason': (bench or {}).get('reason'),
            'bench_ts': (bench or {}).get('ts'),
            'name': m.get('name'),
            'followers': m.get('followers'),
            'following': m.get('following'),
            'statuses': m.get('statuses'),
            'has_cookies': bool(a.get('auth_token')),
            'posts': agg.get('posts', 0),
            'views': agg.get('views', 0),
            'likes': agg.get('likes', 0),
            'bookmarks': agg.get('bookmarks', 0),
            'posted_today': posted_today,
            'per_day': ppd,
            'next_min': None if publishing['blocked'] else nxt_min,
            'next_label': 'Paused' if publishing['blocked'] else sch.hhmm(nxt_min),
            'next_in_min': None if publishing['blocked'] else nxt_in,
        })

    # posts list (newest first) joined with metrics
    posts_out = []
    for p in sorted(posts, key=lambda x: x.get('ts', 0), reverse=True)[:400]:
        m = metrics.get(p.get('tweet_id'), {})
        posts_out.append({
            'ts': p.get('ts'), 'account': p.get('account'), 'url': p.get('url'),
            'text': p.get('text'), 'media': p.get('media'),
            'views': m.get('views'), 'likes': m.get('likes'),
            'retweets': m.get('retweets'), 'replies': m.get('replies'),
            'bookmarks': m.get('bookmarks'),
        })

    latest = totals[-1] if totals else None
    total_views = latest['views'] if latest else sum(m.get('views', 0) for m in metrics.values())
    total_likes = latest['likes'] if latest else sum(m.get('likes', 0) for m in metrics.values())
    total_bm = sum(m.get('bookmarks', 0) for m in metrics.values())
    posts_today = len([p for p in posts
                       if datetime.fromtimestamp(p.get('ts', 0), tz).strftime('%Y-%m-%d') == today])

    # chart: views series for last 7 days, downsampled
    week = now_ts - 7 * 86400
    series = [t for t in totals if t.get('ts', 0) >= week]
    if len(series) > 300:
        step = len(series) // 300 + 1
        series = series[::step]
    chart = [{'ts': t['ts'], 'views': t.get('views', 0), 'likes': t.get('likes', 0)} for t in series]

    best = sorted(accounts_out, key=lambda a: a['views'], reverse=True)[:10]
    # "what posts next" must only list accounts that will actually post
    upcoming = sorted((a for a in accounts_out if not a['benched'] and not publishing['blocked']),
                      key=lambda a: a['next_in_min'])[:12]

    return {
        'now': now_ts,
        'publishing': publishing,
        'tz': tz.key,
        'kpis': {
            'accounts': len([a for a in accounts_out if not a['benched']]),
            'accounts_benched': len([a for a in accounts_out if a['benched']]),
            'accounts_total': len(accounts_out),
            'restrictions_confirmed': sum(a['restriction']['confirmed'] for a in accounts_out),
            'restrictions_unverified': sum(a['benched'] and not a['restriction']['confirmed'] for a in accounts_out),
            'total_posts': len(posts),
            'posts_today': posts_today,
            'total_views': total_views,
            'total_likes': total_likes,
            'total_bookmarks': total_bm,
            'views_10m': _window_delta(totals, latest, 600),
            'views_1h': _window_delta(totals, latest, 3600),
            'views_24h': _window_delta(totals, latest, 86400),
            'views_7d': _window_delta(totals, latest, 7 * 86400),
        },
        'accounts': accounts_out,
        'posts': posts_out,
        'best': best,
        'upcoming': upcoming,
        'chart': chart,
        'stats_ready': bool(metrics),
    }


# --------------------------------------------------------------------------- #
# accounts.txt add / remove                                                   #
# --------------------------------------------------------------------------- #
def _read_lines():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
        return f.read().splitlines()


def _write_lines(lines):
    tmp = ACCOUNTS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, ACCOUNTS_FILE)


def add_account(line):
    line = (line or '').strip()
    if not line:
        return False, 'empty line'
    acct = tp.parse_account(line)          # raises ValueError if unparseable
    if not acct:
        return False, 'could not parse'
    # include benched: a suspended handle is still IN accounts.txt, so adding it
    # again would silently create a duplicate line
    existing = {a['username'].lower() for a in tp.load_accounts(include_benched=True)}
    if acct['username'].lower() in existing:
        return False, f'@{acct["username"]} already in the bank'
    lines = _read_lines()
    lines.append(line)
    _write_lines(lines)
    return True, f'added @{acct["username"]}'


def remove_account(username):
    username = (username or '').strip().lstrip('@').lower()
    kept, removed = [], False
    for ln in _read_lines():
        s = ln.strip()
        if not s or s.startswith('#'):
            kept.append(ln)
            continue
        try:
            acct = tp.parse_account(ln)
            if acct and acct['username'].lower() == username:
                removed = True
                continue
        except ValueError:
            pass
        kept.append(ln)
    if removed:
        _write_lines(kept)
        return True, f'removed @{username}'
    return False, f'@{username} not found'


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_video(self, path):
        """Serve an mp4 with Range support — browsers request ranges for <video>,
        and without it playback/seeking silently fails in Chrome."""
        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith('bytes='):
            try:
                s, _, e = rng[6:].partition('-')
                if s:
                    start = int(s)
                if e:
                    end = min(int(e), size - 1)
                partial = start < size
            except ValueError:
                partial = False
        if not partial:
            start, end = 0, size - 1
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with open(path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return          # user scrubbed/closed the player
                remaining -= len(chunk)

    def _send(self, code, body, ctype='application/json'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        # The whole UI is one inline HTML blob, so a cached copy silently keeps
        # serving OLD markup and JS after a deploy — restarts then look like they
        # did nothing. Never cache the page or the API; media still caches fine.
        if 'html' in ctype or 'json' in ctype:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/index'):
            return self._send(200, HTML, 'text/html; charset=utf-8')
        if self.path.startswith('/api/state'):
            try:
                return self._send(200, json.dumps(build_state()))
            except Exception as e:
                return self._send(500, json.dumps({'error': f'{type(e).__name__}: {e}'}))
        if self.path.startswith('/api/gainers'):
            qs = parse_qs(urlparse(self.path).query)
            per = (qs.get('period') or ['24h'])[0]
            try:
                return self._send(200, json.dumps(gainers(per)))
            except Exception as e:
                return self._send(500, json.dumps({'error': f'{type(e).__name__}: {e}'}))
        if self.path.startswith('/api/account'):
            qs = parse_qs(urlparse(self.path).query)
            u = (qs.get('u') or [''])[0]
            try:
                d = account_detail(u)
                if d is None:
                    return self._send(404, json.dumps({'error': 'no such account'}))
                d['posting'] = _posting.get(d['username'], {})
                return self._send(200, json.dumps(d))
            except Exception as e:
                return self._send(500, json.dumps({'error': f'{type(e).__name__}: {e}'}))
        if self.path.startswith('/api/creatives/video'):
            qs = parse_qs(urlparse(self.path).query)
            slug = (qs.get('slug') or [''])[0]
            # only serve out of creatives_ready, and never follow a traversal
            safe = os.path.basename(slug or '')
            p = os.path.join(bc.CREATIVES_DIR, safe + '.mp4')
            if not safe or not os.path.exists(p):
                p2 = p + '.disabled'
                if safe and os.path.exists(p2):
                    p = p2
                else:
                    return self._send(404, b'', 'video/mp4')
            return self._send_video(p)
        if self.path.startswith('/api/creatives/thumb'):
            qs = parse_qs(urlparse(self.path).query)
            slug = (qs.get('slug') or [''])[0]
            p = bc.thumb_path(slug) if slug else None
            if not p:
                return self._send(404, b'', 'image/jpeg')
            with open(p, 'rb') as f:
                return self._send(200, f.read(), 'image/jpeg')
        if self.path.startswith('/api/creatives'):
            try:
                return self._send(200, json.dumps({
                    'creatives': bc.list_creatives(),
                    'counts': bc.counts(),
                    'refresh': _refresh,
                }))
            except Exception as e:
                return self._send(500, json.dumps({'error': f'{type(e).__name__}: {e}'}))
        return self._send(404, json.dumps({'error': 'not found'}))

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            body = json.loads(raw or b'{}')
        except Exception:
            body = {}
        try:
            if self.path == '/api/account/add':
                ok, msg = add_account(body.get('line'))
            elif self.path == '/api/account/remove':
                ok, msg = remove_account(body.get('username'))
            elif self.path == '/api/account/post-now':
                ok, msg = start_post_now(body.get('username'))
            elif self.path == '/api/account/revive':
                ok, msg = False, 'Account review required. Sign in to X and resolve the restriction; this dashboard cannot restore accounts.'
            elif self.path == '/api/creatives/refresh':
                ok, msg = start_refresh(body.get('limit'))
            elif self.path == '/api/creatives/toggle':
                bc.set_enabled(body.get('slug'), bool(body.get('enabled')))
                ok, msg = True, 'updated'
            elif self.path == '/api/creatives/delete':
                bc.delete_creative(body.get('slug'))
                ok, msg = True, 'deleted'
            else:
                return self._send(404, json.dumps({'error': 'not found'}))
            return self._send(200 if ok else 400, json.dumps({'ok': ok, 'msg': msg}))
        except ValueError as e:
            return self._send(400, json.dumps({'ok': False, 'msg': str(e)}))
        except Exception as e:
            return self._send(500, json.dumps({'ok': False, 'msg': f'{type(e).__name__}: {e}'}))


def main():
    cfg = tp.load_config()
    port = int(cfg.get('dashboard_port', 8787))
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'dashboard: http://127.0.0.1:{port}  (Ctrl-C to stop)', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Pornyard Promo — Dashboard</title>
<script defer src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script async src="https://platform.twitter.com/widgets.js"></script>
<style>
:root{--bg:#0b0e14;--card:#141925;--line:#222a3a;--txt:#e6e9ef;--mut:#8a93a6;--acc:#7c5cff;--good:#39d98a;--bad:#ff5c7c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
header h1{font-size:16px;margin:0;font-weight:700}header .dot{width:9px;height:9px;border-radius:50%;background:var(--bad);box-shadow:0 0 8px var(--bad)}
.tabs{display:flex;gap:6px;margin-left:auto}.tabs button{background:none;border:1px solid var(--line);color:var(--mut);padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px}
.tabs button.on{background:var(--acc);border-color:var(--acc);color:#fff}
main{padding:20px;max-width:1200px;margin:0 auto}.tab{display:none}.tab.on{display:block}
.grid{display:grid;gap:14px}.k4{grid-template-columns:repeat(4,1fr)}.k3{grid-template-columns:repeat(3,1fr)}
@media(max-width:820px){.k4,.k3{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi .v{font-size:26px;font-weight:800}.kpi .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.kpi .s{color:var(--good);font-size:12px;margin-top:2px}
h2{font-size:14px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:24px 0 10px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;cursor:pointer}
td.txt{white-space:normal;max-width:360px;color:var(--mut)}a{color:#9db4ff;text-decoration:none}a:hover{text-decoration:underline}
.pill{font-size:11px;padding:2px 7px;border-radius:99px;background:#232b3d;color:var(--mut)}.pill.ok{background:#11331f;color:var(--good)}.pill.no{background:#3a1622;color:var(--bad)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,button.act{font:13px system-ui}
input{background:#0d1119;border:1px solid var(--line);color:var(--txt);padding:9px 11px;border-radius:8px;width:100%}
button.act{background:var(--acc);border:0;color:#fff;padding:9px 14px;border-radius:8px;cursor:pointer}
button.x{background:#3a1622;border:0;color:var(--bad);padding:5px 10px;border-radius:7px;cursor:pointer}
.mut{color:var(--mut)}.right{text-align:right}#msg{font-size:12px}
tr.benched td{background:rgba(127,29,29,.06)}
.restriction-confirmed{color:var(--bad)}
small.mono{font-family:ui-monospace,Consolas,monospace;color:var(--mut)}
.cgrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.cc{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;position:relative}
.cc.off{opacity:.45}
.cc img{width:100%;aspect-ratio:9/12;object-fit:cover;display:block;background:#0d1119}
.cc .meta{padding:7px 9px;font-size:11px;color:var(--mut);display:flex;justify-content:space-between;gap:6px}
.cc .bar{display:flex;gap:6px;padding:0 9px 9px}
.cc .bar button{flex:1;border:0;border-radius:6px;padding:6px 0;font-size:11px;cursor:pointer}
.cc .tg{background:#11331f;color:var(--good)}.cc.off .tg{background:#232b3d;color:var(--mut)}
.cc .dl{background:#3a1622;color:var(--bad);flex:0 0 34px}
.cc .badge{position:absolute;top:6px;left:6px;background:#000a;color:#fff;font-size:10px;padding:2px 6px;border-radius:99px}
/* clickable account rows + detail drawer */
tr.click{cursor:pointer}tr.click:hover{background:#1b2233}
.uname{color:#9db4ff;font-weight:600}
#scrim{position:fixed;inset:0;background:#000a;opacity:0;pointer-events:none;transition:opacity .18s;z-index:20}
#scrim.on{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;right:0;height:100%;width:min(680px,96vw);background:var(--bg);
  border-left:1px solid var(--line);transform:translateX(100%);transition:transform .22s;z-index:21;
  overflow-y:auto;padding:18px 20px 60px}
#drawer.on{transform:none}
#drawer h3{margin:0;font-size:19px}
.dhead{display:flex;align-items:flex-start;gap:12px;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:16px;position:sticky;top:-18px;background:var(--bg);padding-top:4px}
.dstats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:14px 0}
.dstat{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:9px}
.dstat .v{font-size:17px;font-weight:700}.dstat .l{font-size:10px;color:var(--mut);text-transform:uppercase}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:13px;margin-bottom:14px}
.pcard .cap{font-size:14px;margin-bottom:9px}
.pcard video{width:100%;max-height:420px;border-radius:8px;background:#000;display:block}
.mrow{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:10px;text-align:center}
.mrow div{background:#0d1119;border-radius:7px;padding:6px 2px}
.mrow .n{font-weight:700;font-size:14px}.mrow .k{font-size:9px;color:var(--mut);text-transform:uppercase}
.hot{color:var(--good)}
.tw-embed{margin-top:10px}
.btn{background:var(--acc);border:0;color:#fff;padding:8px 13px;border-radius:8px;cursor:pointer;font-size:12px}
.btn.ghost{background:#232b3d;color:var(--txt)}
.btn:disabled{opacity:.5;cursor:default}
.x{background:#3a1622;border:0;color:var(--bad);padding:5px 10px;border-radius:7px;cursor:pointer}

.posting-alert{border:1px solid #aa475b;background:#301921;border-radius:12px;padding:16px;margin-bottom:18px;overflow-wrap:anywhere}
.posting-alert strong{font-size:17px}.posting-alert p{margin:7px 0}.posting-alert a{margin-right:16px;text-decoration:underline}
@media(max-width:600px){header{flex-wrap:wrap;gap:8px;padding:12px}.tabs{width:100%;margin:0;overflow-x:auto}.tabs button{flex:0 0 auto}main{padding:12px}.card{overflow-x:auto}.posting-alert{padding:14px}}
</style></head><body>
<header><span class=dot></span><h1>Pornyard Promo</h1><span class=mut id=sub></span>
<div class=tabs>
 <button class=on data-t=overview>Overview</button>
 <button data-t=accounts>Accounts</button>
 <button data-t=gainers>Top Gainers</button>
 <button data-t=creatives>Creatives</button>
 <button data-t=posts>Posts</button>
 <button data-t=schedule>Schedule</button>
</div></header>
<main>
 <aside class=posting-alert id=posting-alert role=status aria-live=polite hidden>
  <strong>Publishing stopped — account review required</strong>
  <p id=publishing-reason>Automated posting is disabled after reported account suspensions. No posts are scheduled.</p>
  <p>Sign in to the affected account on X to complete recovery or appeal. This dashboard cannot restore suspended accounts.</p>
  <a href="https://help.x.com/en/forms/account-access/appeals" target=_blank rel=noopener>Appeal a suspension ↗</a>
  <a href="https://help.x.com/en/rules-and-policies/x-automation" target=_blank rel=noopener>X automation rules ↗</a>
 </aside>
 <section class="tab on" id=overview>
  <div class="grid k4" id=kpis></div>
  <h2>Views (last 7 days)</h2>
  <div class=card><canvas id=chart height=90></canvas></div>
  <div class="grid k3">
   <div><h2>Top accounts by views</h2><div class=card><table id=best></table></div></div>
   <div style="grid-column:span 2"><h2>Next up</h2><div class=card><table id=upcoming></table></div></div>
  </div>
 </section>
 <section class=tab id=accounts>
  <h2>Add account</h2>
  <div class=card><div class=row>
    <input id=addline placeholder="username:password:email:emailpw:CT0:AUTH_TOKEN:TOTP   (or username:password)">
    <button class=act onclick=addAcct()>Add</button></div>
    <div id=msg class=mut style=margin-top:8px></div></div>
  <h2>Accounts (<span id=acount>0</span>)</h2>
  <p class=mut id=evidence-summary></p>
  <div class=card style=overflow-x:auto><table id=accts></table></div>
 </section>
 <section class=tab id=gainers>
  <h2>Top gainers — views added in the selected window</h2>
  <div class=card>
   <div class=row>
    <span class=mut style=font-size:12px>period</span>
    <div id=periods class=row style=gap:6px>
     <button class="btn ghost per" data-p=1h>1 hour</button>
     <button class="btn ghost per" data-p=4h>4 hours</button>
     <button class="btn per on" data-p=24h>24 hours</button>
     <button class="btn ghost per" data-p=7d>week</button>
    </div>
    <span id=gsum class=mut style="margin-left:auto;font-size:12px"></span>
   </div>
  </div>
  <div class="grid k3" style=margin-top:4px>
   <div style="grid-column:span 3">
     <h2>Top videos <span class=mut style=font-size:12px>— one row per clip, views summed across every post of it · click to play</span></h2>
     <div class=card style=overflow-x:auto><table id=gclips></table></div>
   </div>
  </div>
 </section>
 <section class=tab id=creatives>
  <h2>Creative bank — OmeTV / monkey.app clips from the Redgif bank</h2>
  <div class=card>
   <div class=row>
    <button class=act onclick=refreshCreatives()>↻ Refresh from bank</button>
    <span class=mut>enabled <b id=con>0</b> / <b id=ctot>0</b> · attaches a random <b>enabled</b> clip per post</span>
    <span id=cstatus class=mut style=margin-left:auto></span>
   </div>
   <div class=mut style="margin-top:8px;font-size:12px">Disabled clips are skipped by the poster. Delete removes the local file. Bio link is the funnel — captions carry no link.</div>
  </div>
  <div id=cgrid class=cgrid style=margin-top:14px></div>
 </section>
 <section class=tab id=posts>
  <h2>Posts (<span id=pcount>0</span>)</h2>
  <div class=card style=overflow-x:auto><table id=postlist></table></div>
 </section>
 <section class=tab id=schedule>
  <h2>Schedule</h2>
  <div class=card style=overflow-x:auto><table id=sched></table></div>
 </section>
</main>
<div id=scrim onclick=closeDrawer()></div>
<aside id=drawer></aside>
<script>
const $=s=>document.querySelector(s),fmt=n=>n==null?'–':Intl.NumberFormat().format(n);
const ago=t=>{if(!t)return'';let s=Date.now()/1000-t;if(s<60)return(s|0)+'s';if(s<3600)return(s/60|0)+'m';if(s<86400)return(s/3600|0)+'h';return(s/86400|0)+'d'};
const inmin=m=>{if(m<60)return'in '+m+'m';let h=m/60|0;return'in '+h+'h '+(m%60)+'m'};
let chart;
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));$('#'+b.dataset.t).classList.add('on');});
async function addAcct(){const line=$('#addline').value;const r=await fetch('/api/account/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({line})});const j=await r.json();$('#msg').textContent=j.msg;$('#msg').style.color=j.ok?'var(--good)':'var(--bad)';if(j.ok){$('#addline').value='';load();}}
async function delAcct(u){if(!confirm('Remove @'+u+' from the bank?'))return;const r=await fetch('/api/account/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})});const j=await r.json();$('#msg').textContent=j.msg;load();}
async function revive(u){if(!confirm('Put @'+u+' back in the posting rotation? (only do this if X actually restored it)'))return;const r=await fetch('/api/account/revive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})});const j=await r.json();$('#msg').textContent=j.msg;load();}
function kpi(l,v,s){return `<div class="card kpi"><div class=l>${l}</div><div class=v>${v}</div>${s?`<div class=s>${s}</div>`:''}</div>`}
function render(d){
 const blocked=!d.publishing || d.publishing.blocked;
 {const pa=$('#posting-alert'); if(pa) pa.hidden=!blocked;}
 $('#publishing-reason').textContent=(d.publishing&&d.publishing.reason)||'Publishing is disabled. No posts are scheduled.';
 $('#sub').textContent=`${d.kpis.accounts_total} accounts · ${d.tz}` + (blocked?' · publishing stopped':' · publishing live') + ' · saved metrics';
 $('#kpis').innerHTML=[
   kpi('Total views',fmt(d.kpis.total_views),'+'+fmt(d.kpis.views_24h)+' /24h'),
   kpi('Views /hr',fmt(d.kpis.views_1h),'+'+fmt(d.kpis.views_10m)+' /10m'),
   kpi('Posts',fmt(d.kpis.total_posts),fmt(d.kpis.posts_today)+' today'),
   kpi('Bookmarks',fmt(d.kpis.total_bookmarks),fmt(d.kpis.total_likes)+' likes'),
 ].join('');
 $('#best').innerHTML='<tr><th>Account</th><th class=right>Views</th><th class=right>Posts</th></tr>'+
   d.best.map(a=>`<tr class=click onclick="openAcct('${a.username}')"><td class=uname>@${a.username}</td><td class=right>${fmt(a.views)}</td><td class=right>${a.posts}</td></tr>`).join('');
 $('#upcoming').innerHTML='<tr><th>Account</th><th>Next</th><th class=right>When</th><th class=right>Today</th></tr>'+
   d.upcoming.map(a=>`<tr class=click onclick="openAcct('${a.username}')"><td class=uname>@${a.username}</td><td>${a.next_label}</td><td class=right>${inmin(a.next_in_min)}</td><td class=right>${a.posted_today}/${a.per_day}</td></tr>`).join('');
 if(blocked)$('#upcoming').innerHTML='<tr><td class=mut>No posts scheduled. Publishing is stopped.</td></tr>';
 $('#acount').textContent=d.kpis.accounts_total;
 $('#evidence-summary').textContent=`${d.kpis.restrictions_confirmed} confirmed restrictions · ${d.kpis.restrictions_unverified} historical flags need verification. Public visibility and saved cookies do not confirm permission to publish.`;
 $('#accts').innerHTML='<tr><th>Account</th><th>Name</th><th class=right>Followers</th><th class=right>Views</th><th class=right>Likes</th><th class=right>Bookmarks</th><th class=right>Posts</th><th>Account evidence</th><th></th></tr>'+
   d.accounts.map(a=>`<tr class="click${a.benched?' benched':''}" onclick="openAcct('${a.username}')"><td class=uname>@${a.username}</td><td class=mut>${esc(a.name||'')}</td><td class=right>${fmt(a.followers)}</td><td class=right>${fmt(a.views)}</td><td class=right>${fmt(a.likes)}</td><td class=right>${fmt(a.bookmarks)}</td><td class=right>${a.posts}</td><td><span class="pill ${a.restriction.confirmed?'no':''}" title="${esc(a.restriction.reason)}">${esc(a.restriction.label)}</span></td><td>${a.benched?'<span class=mut>review on X · </span>':''}<button class=x onclick="event.stopPropagation();delAcct('${a.username}')">remove</button></td></tr>`).join('');
 $('#pcount').textContent=d.posts.length;
 $('#postlist').innerHTML='<tr><th>When</th><th>Account</th><th class=right>Views</th><th class=right>Likes</th><th class=right>Bookmarks</th><th class=right>RTs</th><th>Tweet</th><th>Media</th></tr>'+
   d.posts.map(p=>`<tr class=click onclick="openAcct('${p.account}')"><td>${ago(p.ts)} ago</td><td class=uname>@${p.account}</td><td class=right>${fmt(p.views)}</td><td class=right>${fmt(p.likes)}</td><td class=right>${fmt(p.bookmarks)}</td><td class=right>${fmt(p.retweets)}</td><td class=txt><a href="${p.url}" target=_blank onclick=event.stopPropagation()>${(p.text||'').replace(/</g,'&lt;')}</a></td><td><small class=mono>${p.media||''}</small></td></tr>`).join('');
 $('#sched').innerHTML='<tr><th>Account</th><th>Next</th><th class=right>When</th><th class=right>Posted today</th></tr>'+
   d.accounts.filter(a=>!a.benched&&!blocked).sort((a,b)=>a.next_in_min-b.next_in_min).map(a=>`<tr class=click onclick="openAcct('${a.username}')"><td class=uname>@${a.username}</td><td>${a.next_label}</td><td class=right>${inmin(a.next_in_min)}</td><td class=right>${a.posted_today}/${a.per_day}</td></tr>`).join('');
 if(blocked)$('#sched').innerHTML='<tr><td class=mut>Publishing is stopped. No accounts will post automatically.</td></tr>';
 if(typeof Chart==='undefined')return;
 const lab=d.chart.map(c=>new Date(c.ts*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit'}));
 const vv=d.chart.map(c=>c.views);
 if(!chart){chart=new Chart($('#chart'),{type:'line',data:{labels:lab,datasets:[{label:'Views',data:vv,borderColor:'#7c5cff',backgroundColor:'rgba(124,92,255,.15)',fill:true,tension:.3,pointRadius:0,borderWidth:2}]},options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8a93a6',maxTicksLimit:8},grid:{display:false}},y:{ticks:{color:'#8a93a6'},grid:{color:'#222a3a'}}}}});}
 else{chart.data.labels=lab;chart.data.datasets[0].data=vv;chart.update('none');}
}
// ---------- Top Gainers ----------
let gPeriod='24h', gTimer=null;
async function loadGainers(){
 try{
  const d=await (await fetch('/api/gainers?period='+gPeriod)).json();
  if(d.error){$('#gsum').textContent=d.error;return;}
  $('#gsum').textContent=`+${fmt(d.total_gain)} views across ${d.tracked} tracked posts · last ${d.window_hours}h`;
  $('#gclips').innerHTML='<tr><th>Video</th><th class=right>+Views</th><th class=right>/hr</th><th class=right>+Bkmk</th><th class=right>Posts</th></tr>'+
   (d.clips.map(c=>{
      // the whole row plays the clip; has_video is set server-side only when the
      // file really is in CREATIVES_DIR, so we never offer a link that 404s
      const vurl=`/api/creatives/video?slug=${encodeURIComponent(c.clip)}`;
      const nm=c.has_video
        ? `<small class=mono>${c.clip.slice(0,52)}</small>`
        : `<small class=mono>${c.clip.slice(0,52)}</small> <span class=mut style=font-size:11px>(file missing)</span>`;
      return `<tr class=click title="${c.has_video?'play this clip':'no local file for this clip'}"${c.has_video?` onclick="window.open('${vurl}','_blank')"`:''}>
      <td><img src="/api/creatives/thumb?slug=${encodeURIComponent(c.clip)}" style="width:34px;height:44px;object-fit:cover;border-radius:4px;vertical-align:middle;margin-right:6px">${nm}</td>
      <td class="right hot">+${fmt(c.gain_views)}</td>
      <td class=right>${fmt(c.rate_per_hr)}</td>
      <td class=right>${c.gain_bookmarks?'+'+fmt(c.gain_bookmarks):'–'}</td>
      <td class=right>${c.posts}</td></tr>`;}).join('')
    || '<tr><td class=mut colspan=5>no movement in this window yet</td></tr>');
 }catch(e){console.error(e);}
}
document.querySelectorAll('#periods .per').forEach(b=>b.onclick=()=>{
  gPeriod=b.dataset.p;
  document.querySelectorAll('#periods .per').forEach(x=>{x.classList.add('ghost');x.classList.remove('on');});
  b.classList.remove('ghost');b.classList.add('on');
  loadGainers();
});
document.querySelector('.tabs [data-t=gainers]').addEventListener('click',()=>{
  loadGainers();
  if(gTimer)clearInterval(gTimer);
  gTimer=setInterval(()=>{if($('#gainers').classList.contains('on'))loadGainers();},10000);
});

// ---------- account detail drawer ----------
let curAcct=null, drawerTimer=null;
const esc=s=>(s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
function closeDrawer(){$('#drawer').classList.remove('on');$('#scrim').classList.remove('on');
  curAcct=null; if(drawerTimer){clearInterval(drawerTimer);drawerTimer=null;}
  $('#drawer').innerHTML='';}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});
async function openAcct(u){
  curAcct=u;$('#drawer').classList.add('on');$('#scrim').classList.add('on');
  $('#drawer').innerHTML=`<div class=dhead><div style=flex:1><h3>@${esc(u)}</h3>`
    +`<div class=mut style=font-size:12px>loading…</div></div>`
    +`<button class="btn ghost" onclick="closeDrawer()">esc ✕</button></div>`;
  await renderAcct();
  if(drawerTimer)clearInterval(drawerTimer);
  drawerTimer=setInterval(renderAcct,5000);    // live-refresh while open
}
async function renderAcct(){
  if(!curAcct)return;
  let d;try{d=await (await fetch('/api/account?u='+encodeURIComponent(curAcct))).json();}
  catch(e){return;}
  if(!d||d.error){
    const em=esc((d&&d.error)||'could not load');
    const hint=(d&&d.error==='no such account')
      ? 'This handle has no posts on record and is not in accounts.txt.'
      : '';
    $('#drawer').innerHTML=`<div class=dhead><div style=flex:1><h3>@${esc(curAcct)}</h3>`
      +`<div class=mut style=font-size:12px>${em}${hint?' · '+esc(hint):''}</div></div>`
      +`<button class="btn ghost" onclick="closeDrawer()">esc ✕</button></div>`
      +`<div class=row><a class=btn href="https://x.com/${encodeURIComponent(curAcct)}" target=_blank>open on X ↗</a></div>`;
    return;}
  const t=d.totals||{};
  const busy=d.posting&&d.posting.running;
  const blk=!d.publishing || d.publishing.blocked;
  const pmsg=blk?((d.publishing&&d.publishing.reason)||'Publishing is disabled pending account review.'):'';
  const head=`<div class=dhead>
     <div style=flex:1>
       <h3>@${esc(d.username)}</h3>
       <div class=mut style=font-size:12px>${esc(d.name||'')} · ${d.statuses??'–'} tweets ·
         exit <small class=mono>${esc(d.proxy_sessid)}</small></div>
     </div>
     <button class="btn ghost" onclick="closeDrawer()">esc ✕</button>
   </div>
   <div class=row>
     <a class=btn href="${d.profile_url}" target=_blank>open on X ↗</a>
     <button class=btn id=pnow onclick="postNow('${esc(d.username)}')" ${blk?'disabled':''}>
       ${blk?'publishing stopped':'post now'}</button>
     <span class=mut id=pmsg style=font-size:12px>${esc(pmsg)}</span>
   </div>
   <div class=card style="margin-top:8px">
     <b class="${d.restriction.confirmed?'restriction-confirmed':''}">${esc(d.restriction.label)}</b>
     <p class=mut style="font-size:12px">${esc(d.restriction.reason)}</p>
     ${d.evidence?`<p class=mut style="font-size:12px">Latest observation: ${esc(d.evidence.status)} · ${esc(d.evidence.source)} · ${new Date(d.evidence.observed_at*1000).toLocaleString()}<br>${esc(d.evidence.reason)}</p>`:''}
     ${d.benched?`<p class=mut style="font-size:12px">Stored restriction: ${esc(d.benched.reason||'review required')} · ${ago(d.benched.ts)} ago. This account remains paused for review.</p>`:''}
     <p class=mut style="font-size:12px">Metrics below are saved observations. A dash means no data was collected. Public visibility does not confirm permission to publish.</p>
   </div>
   <div class=dstats>
     <div class=dstat><div class=v>${fmt(d.followers)}</div><div class=l>followers</div></div>
     <div class=dstat><div class=v>${fmt(t.views)}</div><div class=l>views</div></div>
     <div class=dstat><div class=v>${fmt(t.likes)}</div><div class=l>likes</div></div>
     <div class=dstat><div class=v>${fmt(t.bookmarks)}</div><div class=l>bookmarks</div></div>
   </div>
   <h2>Posts (${d.posts.length})</h2>`;
  const body=d.posts.map(p=>{
    const vid=p.clip?`<video controls preload=metadata playsinline poster="/api/creatives/thumb?slug=${encodeURIComponent(p.clip)}" src="/api/creatives/video?slug=${encodeURIComponent(p.clip)}"></video>`:'';
    const ratio=p.bm_ratio!=null?`<span class="${p.bm_ratio>=40?'hot':'mut'}" title="bookmark:like — 40%+ is the banger signal">bm/like ${p.bm_ratio}%</span>`:'';
    return `<div class=pcard>
      <div class=cap>${esc(p.text)}</div>
      ${vid}
      <div class=mrow>
        <div><div class=n>${fmt(p.views)}</div><div class=k>views</div></div>
        <div><div class=n>${fmt(p.likes)}</div><div class=k>likes</div></div>
        <div><div class=n>${fmt(p.bookmarks)}</div><div class=k>bookmarks</div></div>
        <div><div class=n>${fmt(p.retweets)}</div><div class=k>reposts</div></div>
        <div><div class=n>${fmt(p.replies)}</div><div class=k>replies</div></div>
      </div>
      <div class=row style="margin-top:9px;font-size:12px">
        <span class=mut>${ago(p.ts)} ago</span>${ratio}
        <a href="${p.url}" target=_blank style=margin-left:auto>view on X ↗</a>
      </div>
      <div class=tw-embed id="emb_${p.tweet_id}"></div>
    </div>`;}).join('') || '<div class=mut>no posts yet</div>';
  $('#drawer').innerHTML=head+body;
  loadEmbeds(d);
}
// Real X embeds when they'll render. Adult-labeled / brand-new posts often refuse
// to embed, so this is additive only — the local clip above is always the source
// of truth for what actually went out.
function loadEmbeds(d){
  if(!window.twttr||!twttr.widgets)return;
  d.posts.slice(0,3).forEach(p=>{
    const el=document.getElementById('emb_'+p.tweet_id);
    if(!el||el.dataset.done)return;
    el.dataset.done=1;
    twttr.widgets.createTweet(p.tweet_id,el,{theme:'dark',width:400,dnt:true})
      .then(node=>{if(!node)el.remove();}).catch(()=>el.remove());
  });
}
async function postNow(u){
  const b=$('#pnow');if(b){b.disabled=true;b.textContent='posting…';}
  await fetch('/api/account/post-now',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:u})});
  setTimeout(renderAcct,1500);
}

async function load(){try{const r=await fetch('/api/state');render(await r.json());}catch(e){console.error(e);}}

// ---- Creatives tab ----
async function loadCreatives(){
 try{const r=await fetch('/api/creatives');const d=await r.json();
  $('#con').textContent=d.counts.enabled;$('#ctot').textContent=d.counts.total;
  const rf=d.refresh||{};$('#cstatus').textContent=rf.running?('⏳ '+(rf.msg||'syncing…')):(rf.msg||'');
  $('#cgrid').innerHTML=d.creatives.map(c=>{
   const dim=(c.w&&c.h)?c.w+'×'+c.h:'';const dur=c.duration!=null?c.duration+'s':'';
   return `<div class="cc ${c.enabled?'':'off'}">
     <img loading=lazy src="/api/creatives/thumb?slug=${c.slug}" alt="">
     ${c.posted?`<span class=badge>posted ${c.posted}×</span>`:''}
     <div class=meta><span>${dur}</span><span>${dim}</span></div>
     <div class=bar>
       <button class=tg onclick="toggleC('${c.slug}',${c.enabled?0:1})">${c.enabled?'enabled':'disabled'}</button>
       <button class=dl onclick="delC('${c.slug}')">✕</button>
     </div></div>`;}).join('')
   || '<div class=mut>no creatives yet — click “Refresh from bank”.</div>';
 }catch(e){console.error(e);}
}
async function toggleC(slug,en){await fetch('/api/creatives/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug,enabled:!!en})});loadCreatives();}
async function delC(slug){if(!confirm('Delete '+slug+'? (local file removed)'))return;await fetch('/api/creatives/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug})});loadCreatives();}
async function refreshCreatives(){$('#cstatus').textContent='⏳ starting…';await fetch('/api/creatives/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});setTimeout(loadCreatives,1200);}

// load creatives when its tab is first opened, and poll while visible
document.querySelector('.tabs [data-t=creatives]').addEventListener('click',loadCreatives);
setInterval(()=>{if($('#creatives').classList.contains('on'))loadCreatives();},8000);

load();setInterval(load,5000);
</script></body></html>"""


if __name__ == '__main__':
    main()
