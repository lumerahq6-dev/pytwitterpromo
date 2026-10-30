r"""
bank_creatives.py — pull the LIVE pornyard Redgif omegle-win bank down to local
Twitter-ready clips, so the promo poster attaches authentic OmeTV/monkey.app UI
video (the Monkeygod format) instead of generic previews.

Source of truth = R2 `data/redgif_bank/live_bank.json` (the same bank the site
serves) minus `data/redgif_bank/dead_slugs.json`. Each live slug is resolved via
the Redgifs API (needs a free temp bearer), the HD mp4 is downloaded, trimmed to
<=30s and re-encoded Twitter-safe (H.264/yuv420p + faststart), and cached under
creatives_ready/<slug>.mp4 with a thumbnail. Once local, a clip is permanent —
it keeps working on Twitter even after the redgifs copy is banned/410s.

The clips keep their burned-in `ometvwins.xyz` funnel bar as-is (operator's own
domain). The Twitter funnel itself is the account BIO link — captions carry NO
link (see config.json).

    python bank_creatives.py --refresh            # sync new live-bank clips
    python bank_creatives.py --refresh --limit 10 # cap this run
    python bank_creatives.py --list               # show local creatives

Enable/disable/delete + a "Refresh from bank" button are exposed in the
dashboard "Creatives" tab; this module is the engine behind them.
"""
import os
import re
import sys
import json
import time
import subprocess
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CREATIVES_DIR = os.path.join(HERE, 'creatives_ready')
THUMBS_DIR = os.path.join(CREATIVES_DIR, 'thumbs')
MANIFEST = os.path.join(HERE, 'creatives.json')

# rclone: prefer the known install, fall back to PATH
RCLONE = r'C:\Users\lard\bin\rclone.exe'
if not os.path.exists(RCLONE):
    RCLONE = 'rclone'

LIVE_BANK_KEY = 'r2:tbw-media/data/redgif_bank/live_bank.json'
DEAD_SLUGS_KEY = 'r2:tbw-media/data/redgif_bank/dead_slugs.json'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

MAX_SECONDS = 30      # Twitter best practice: 15-30s fully watched > long w/ drop-off
MAX_WIDTH = 1080


def log(msg):
    print(f'[bank] {msg}', flush=True)


# --------------------------------------------------------------------------- #
# manifest                                                                    #
# --------------------------------------------------------------------------- #
def load_manifest():
    if not os.path.exists(MANIFEST):
        return {}
    try:
        with open(MANIFEST, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(m):
    tmp = MANIFEST + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, MANIFEST)


# --------------------------------------------------------------------------- #
# redgifs + r2 helpers                                                        #
# --------------------------------------------------------------------------- #
def _http(url, tok=None, binary=False, timeout=40):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Referer': 'https://www.redgifs.com/'})
    if tok:
        req.add_header('Authorization', 'Bearer ' + tok)
    r = urllib.request.urlopen(req, timeout=timeout)
    return r.read() if binary else json.loads(r.read())


def redgifs_token():
    return _http('https://api.redgifs.com/v2/auth/temporary')['token']


def _rclone_cat(key):
    r = subprocess.run([RCLONE, 'cat', key], capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout.decode('utf-8'))
    except Exception:
        return None


def fetch_bank_slugs():
    """Live bank slugs (minus dead), newest-first-ish. Falls back to server.js."""
    bank = _rclone_cat(LIVE_BANK_KEY)
    slugs = list(bank.get('slugs', [])) if isinstance(bank, dict) else []
    if not slugs:  # fallback: parse server.js fallback block
        try:
            srv = open(os.path.join(HERE, '..', 'server.js'), encoding='utf-8').read()
            m = re.search(r"    // BEGIN_REDGIF_FALLBACK_SLUGS(.*?)    // END_REDGIF_FALLBACK_SLUGS", srv, re.S)
            if m:
                slugs = re.findall(r"'([A-Za-z0-9_-]+)'", m.group(1))
        except Exception:
            pass
    dead = _rclone_cat(DEAD_SLUGS_KEY)
    dead = set(dead) if isinstance(dead, list) else set()
    return [s for s in slugs if s not in dead], len(slugs), len(dead)


# --------------------------------------------------------------------------- #
# transcode                                                                   #
# --------------------------------------------------------------------------- #
def _transcode(src, dst):
    cmd = ['ffmpeg', '-y', '-i', src, '-t', str(MAX_SECONDS),
           '-vf', f"scale='min({MAX_WIDTH},iw)':-2:flags=bicubic",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-profile:v', 'high',
           '-level', '4.0', '-preset', 'veryfast', '-crf', '23',
           '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception:
        return False
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def _thumb(src, slug):
    os.makedirs(THUMBS_DIR, exist_ok=True)
    out = os.path.join(THUMBS_DIR, slug + '.jpg')
    subprocess.run(['ffmpeg', '-v', 'error', '-ss', '1', '-i', src, '-frames:v', '1',
                    '-vf', 'scale=360:-1', out, '-y'], capture_output=True, timeout=60)
    return out if os.path.exists(out) else None


def _paths(slug):
    """Active + disabled paths for a slug's clip."""
    return (os.path.join(CREATIVES_DIR, slug + '.mp4'),
            os.path.join(CREATIVES_DIR, slug + '.mp4.disabled'))


def import_local(paths, log_fn=log):
    """Add local video files (e.g. hand-picked OmeTV/monkey.app screen recordings)
    to the creative pool, transcoded Twitter-safe like bank clips.

    The fleet needs (accounts x reuse_gap_days) clips to keep every post unique,
    and the live Redgif bank alone is usually too thin for that.
    """
    os.makedirs(CREATIVES_DIR, exist_ok=True)
    man = load_manifest()
    added = 0
    for src in paths:
        if not os.path.exists(src):
            log_fn(f'  ! missing {src}')
            continue
        slug = re.sub(r'[^A-Za-z0-9_-]', '_', os.path.splitext(os.path.basename(src))[0])[:60]
        active, disabled = _paths(slug)
        if os.path.exists(active) or os.path.exists(disabled):
            log_fn(f'  = {slug} already present')
            continue
        if not _transcode(src, active):
            log_fn(f'  ! transcode failed {src}')
            continue
        _thumb(active, slug)
        dur = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=nk=1:nw=1', active],
                             capture_output=True, text=True)
        try:
            d = round(float(dur.stdout.strip()), 1)
        except ValueError:
            d = None
        man[slug] = {'added_ts': int(time.time()), 'duration': d,
                     'w': None, 'h': None, 'enabled': True, 'source': 'local'}
        save_manifest(man)
        added += 1
        log_fn(f'  + {slug} ({d}s, local)')
    log_fn(f'imported {added}')
    return {'ok': True, 'added': added, 'have': len(load_manifest())}


# --------------------------------------------------------------------------- #
# sync                                                                        #
# --------------------------------------------------------------------------- #
def sync_bank(limit=None, log_fn=log):
    os.makedirs(CREATIVES_DIR, exist_ok=True)
    os.makedirs(THUMBS_DIR, exist_ok=True)
    man = load_manifest()

    candidates, total, ndead = fetch_bank_slugs()
    log_fn(f'live bank: {len(candidates)} candidate slugs ({total} in bank, {ndead} dead)')
    if not candidates:
        return {'ok': False, 'msg': 'no live bank slugs (rclone/R2 unreachable?)',
                'added': 0, 'have': len(man)}

    tok = None
    added = skipped = failed = 0
    for slug in candidates:
        active, disabled = _paths(slug)
        if os.path.exists(active) or os.path.exists(disabled):
            skipped += 1
            continue
        if limit is not None and added >= limit:
            break
        try:
            if tok is None:
                tok = redgifs_token()
            meta = _http(f'https://api.redgifs.com/v2/gifs/{slug}', tok)
            g = meta.get('gif', {})
            urls = g.get('urls', {})
            hd = urls.get('hd') or urls.get('sd')
            if not hd:
                failed += 1
                continue
            raw = os.path.join(CREATIVES_DIR, '_raw_' + slug + '.mp4')
            with open(raw, 'wb') as f:
                f.write(_http(hd, tok, binary=True, timeout=90))
            ok = _transcode(raw, active)
            try:
                os.remove(raw)
            except OSError:
                pass
            if not ok:
                failed += 1
                continue
            _thumb(active, slug)
            man[slug] = {
                'added_ts': int(time.time()),
                'duration': round(float(g.get('duration') or 0), 1),
                'w': g.get('width'), 'h': g.get('height'),
                'enabled': True, 'source': 'redgif_bank',
            }
            save_manifest(man)
            added += 1
            log_fn(f'  + {slug} ({man[slug]["duration"]}s {man[slug]["w"]}x{man[slug]["h"]})')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):        # token expired mid-run — refresh once
                tok = None
            failed += 1
        except Exception as e:
            log_fn(f'  ! {slug}: {type(e).__name__}: {str(e)[:80]}')
            failed += 1

    msg = f'added {added}, skipped {skipped} (already local), failed {failed}'
    log_fn(msg)
    return {'ok': True, 'msg': msg, 'added': added, 'skipped': skipped,
            'failed': failed, 'have': len(load_manifest())}


# --------------------------------------------------------------------------- #
# panel helpers                                                               #
# --------------------------------------------------------------------------- #
def _posted_counts():
    """media-basename -> times posted, from the posts log."""
    try:
        import store
        counts = {}
        for p in store.read_posts():
            mediafile = p.get('media')
            if mediafile:
                counts[mediafile] = counts.get(mediafile, 0) + 1
        return counts
    except Exception:
        return {}


def list_creatives():
    man = load_manifest()
    posted = _posted_counts()
    out = []
    for slug, meta in man.items():
        active, disabled = _paths(slug)
        exists = os.path.exists(active) or os.path.exists(disabled)
        enabled = os.path.exists(active)
        out.append({
            'slug': slug,
            'enabled': enabled,
            'exists': exists,
            'duration': meta.get('duration'),
            'w': meta.get('w'), 'h': meta.get('h'),
            'posted': posted.get(slug + '.mp4', 0),
            'added_ts': meta.get('added_ts'),
        })
    out.sort(key=lambda c: (not c['enabled'], -(c['added_ts'] or 0)))
    return out


def set_enabled(slug, enabled):
    active, disabled = _paths(slug)
    if enabled and os.path.exists(disabled):
        os.replace(disabled, active)
    elif not enabled and os.path.exists(active):
        os.replace(active, disabled)
    man = load_manifest()
    if slug in man:
        man[slug]['enabled'] = bool(enabled)
        save_manifest(man)
    return True


def delete_creative(slug):
    active, disabled = _paths(slug)
    for p in (active, disabled, os.path.join(THUMBS_DIR, slug + '.jpg')):
        try:
            os.remove(p)
        except OSError:
            pass
    man = load_manifest()
    man.pop(slug, None)
    save_manifest(man)
    return True


def thumb_path(slug):
    p = os.path.join(THUMBS_DIR, slug + '.jpg')
    return p if os.path.exists(p) else None


def counts():
    man = load_manifest()
    enabled = sum(1 for s in man if os.path.exists(_paths(s)[0]))
    return {'total': len(man), 'enabled': enabled}


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Sync the pornyard Redgif bank to local Twitter creatives')
    ap.add_argument('--refresh', action='store_true', help='download new live-bank clips')
    ap.add_argument('--limit', type=int, default=None, help='max clips to add this run')
    ap.add_argument('--list', action='store_true', help='list local creatives')
    args = ap.parse_args()

    if args.list:
        for c in list_creatives():
            state = 'on ' if c['enabled'] else 'OFF'
            log(f"{state} {c['slug']:40s} {c['duration']}s posted={c['posted']}")
        log(f'total {counts()}')
    elif args.refresh:
        sync_bank(limit=args.limit)
    else:
        ap.print_help()
