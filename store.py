r"""
Persistent data store for the promo tool — everything the scheduler writes and
the dashboard reads. Plain JSON/JSONL files under data/ so the whole thing
survives a reboot / restart with full memory.

  data/posts.jsonl        append-only log: one line per tweet posted
  data/metrics_latest.json latest view/like/etc snapshot per tweet_id
  data/totals.jsonl       append-only totals time-series (for the chart + deltas)
  data/accounts_meta.json latest profile (followers/following/...) per account
  data/benched.json       accounts pulled out of the posting rotation (suspended)
"""
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, 'data')


def _p(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def now():
    return int(time.time())


def append_jsonl(path, obj):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj) + '\n')


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# --- posts log ------------------------------------------------------------- #
def record_post(account, tweet_id, url, text, media=None):
    try:
        append_jsonl(_p('posts.jsonl'), {
            'ts': now(), 'account': account, 'tweet_id': str(tweet_id),
            'url': url, 'text': text, 'media': media,
        })
    except Exception:
        pass


def read_posts():
    return read_jsonl(_p('posts.jsonl'))


# --- metrics (latest snapshot per tweet) ----------------------------------- #
def read_metrics():
    return read_json(_p('metrics_latest.json'), {})


def write_metrics(m):
    write_json(_p('metrics_latest.json'), m)


# --- totals time-series ---------------------------------------------------- #
def append_totals(obj):
    append_jsonl(_p('totals.jsonl'), obj)


def read_totals():
    return read_jsonl(_p('totals.jsonl'))


# --- per-tweet metrics history (drives the Gainers tab) -------------------- #
# One row per tweet per 15-min bucket, written only when the numbers actually
# changed. Bucketing matters: the poller runs every ~2min over ~240 tweets, so
# an unthrottled log would add >170k rows/day. 15-min buckets cut that ~8x and
# still resolve a 1-hour delta. Kept to HISTORY_DAYS so this can't become the
# unbounded log that caused the site-wide lag before.
HISTORY_BUCKET_S = 900
HISTORY_DAYS = 8


def _bucket(ts):
    return int(ts) // HISTORY_BUCKET_S * HISTORY_BUCKET_S


def append_metrics_history(rows):
    """rows: iterable of (tweet_id, views, likes, bookmarks). Dedupes per bucket."""
    if not rows:
        return
    path = _p('metrics_history.jsonl')
    b = _bucket(now())
    seen = set()
    if os.path.exists(path):
        # only need this bucket's ids to dedupe; scan the tail, not the file
        try:
            with open(path, 'rb') as f:
                f.seek(max(0, os.path.getsize(path) - 400_000))
                for line in f.read().decode('utf-8', 'ignore').splitlines():
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get('b') == b:
                        seen.add(o.get('i'))
        except Exception:
            pass
    out = []
    for tid, views, likes, bms in rows:
        if str(tid) in seen:
            continue
        out.append({'b': b, 'ts': now(), 'i': str(tid),
                    'v': int(views or 0), 'l': int(likes or 0), 'k': int(bms or 0)})
    if not out:
        return
    with open(path, 'a', encoding='utf-8') as f:
        for o in out:
            f.write(json.dumps(o) + '\n')


def read_metrics_history():
    return read_jsonl(_p('metrics_history.jsonl'))


def trim_metrics_history():
    path = _p('metrics_history.jsonl')
    if not os.path.exists(path):
        return
    cutoff = now() - HISTORY_DAYS * 86400
    rows = [r for r in read_jsonl(path) if r.get('ts', 0) >= cutoff]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    os.replace(tmp, path)


# --- account profiles ------------------------------------------------------ #
def read_meta():
    return read_json(_p('accounts_meta.json'), {})


def write_meta(m):
    write_json(_p('accounts_meta.json'), m)


# --- bench (accounts taken out of the posting rotation) -------------------- #
# A suspended account never posts again, but it costs MORE than a live one: the
# poster burns 5 proxy exits per attempt and, because the failure surfaces as
# 'no-media' (retryable), the scheduler retries it every tick forever. Benching
# is a data-only tombstone — the line stays in accounts.txt so the dashboard,
# lifecycle stats and de-dupe on re-import all still see it.
BENCH_FILE = 'benched.json'


def read_benched():
    """{username: {'ts', 'reason', 'source'}} — accounts that must not post."""
    b = read_json(_p(BENCH_FILE), {})
    return b if isinstance(b, dict) else {}


def bench_account(username, reason, source='poster'):
    """Take an account out of rotation. True if this benched it just now."""
    b = read_benched()
    if username in b:
        return False
    b[username] = {'ts': now(), 'reason': str(reason), 'source': source}
    write_json(_p(BENCH_FILE), b)
    return True


def unbench_account(username):
    """Put an account back in rotation (it came back / operator override)."""
    b = read_benched()
    if b.pop(username, None) is None:
        return False
    write_json(_p(BENCH_FILE), b)
    return True
