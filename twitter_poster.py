r"""
PORNYARD TWITTER PROMO — core poster.

Posts a fixed promo tweet to a bunch of pre-made X/Twitter accounts. These
accounts are NOT API apps (no consumer key / access token), they come with the
browser login cookies (auth_token + ct0) and a 2FA TOTP secret, so we drive
Twitter's internal web API via `twikit` instead of the official v2 API.

Account line format (one per line in accounts.txt), ':'-separated:

    username:password:email:email_password:CT0:AUTH_TOKEN:TOTP_SECRET

Fields after the email-password are auto-classified by shape (so order between
ct0 / auth_token / totp doesn't actually matter):
    * 40-char hex          -> auth_token   (the cookie that logs you in)
    * longer hex           -> ct0          (CSRF cookie)
    * 16+ char A-Z2-7      -> totp_secret  (2FA, used only if cookies are dead)

Usage:
    python twitter_poster.py --test                  # parse accounts + show the tweet, no network
    python twitter_poster.py --once                  # DRY RUN: log what each account *would* post
    python twitter_poster.py --once --live           # actually post once to EVERY account
    python twitter_poster.py --once --live --account ashwiin___   # one account only

Nothing is posted unless you pass --live. Cookies are tried first; if they're
dead the account is re-logged-in with username/password/totp and the refreshed
cookies are cached under cookies/<username>.json.
"""
import os
import re
import sys
import json
import random
import hashlib
import asyncio
import argparse
import subprocess
from datetime import datetime

import store
import media_ledger
import communities
import follow_seed
import ramp

from twikit import Client
from twikit.errors import (
    Unauthorized, DuplicateTweet, AccountLocked, AccountSuspended,
    TooManyRequests, BadRequest, Forbidden, NotFound, TwitterException,
)

# --- twikit transaction-id fix ------------------------------------------- #
# twikit 2.3.3 (latest) can't parse X's CURRENT webpack manifest: the format
# flipped from `"ondemand.s":"<hash>"` to `<chunkId>:"ondemand.s"` with the
# content hash stored separately as `<chunkId>:"<hash>"`. Its get_indices()
# therefore finds no ondemand file and every create_tweet/login dies with
# "Couldn't get KEY_BYTE indices". We patch get_indices to read the new layout
# (falling back to the old one). Everything downstream of it is unchanged.
from twikit.x_client_transaction import transaction as _xct


ONDEMAND_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'data', 'ondemand_hash.txt')


def read_ondemand_cache():
    try:
        v = open(ONDEMAND_CACHE, encoding='utf-8').read().strip()
        return v or None
    except OSError:
        return None


def write_ondemand_cache(h):
    try:
        os.makedirs(os.path.dirname(ONDEMAND_CACHE), exist_ok=True)
        with open(ONDEMAND_CACHE, 'w', encoding='utf-8') as f:
            f.write(h)
    except OSError:
        pass


def scrape_ondemand_hash(page):
    """Pull the ondemand.s content hash out of an x.com page, either format."""
    m = re.search(r'(\d+):"ondemand\.s"', page)          # chunk id for ondemand.s
    if m:
        cid = m.group(1)
        h = re.search(r'(?<![0-9])' + re.escape(cid) + r':"([0-9a-f]{4,})"', page)
        if h:
            return h.group(1)                            # the id -> hash mapping
    old = _xct.ON_DEMAND_FILE_REGEX.search(page)         # old-format fallback
    return old.group(1) if old else None


async def _patched_get_indices(self, home_page_response, session, headers):
    response = self.validate_response(home_page_response) or self.home_page_response
    page = str(response)
    on_demand_hash = scrape_ondemand_hash(page)
    if on_demand_hash:
        write_ondemand_cache(on_demand_hash)
    else:
        # The LOGGED-OUT x.com shell (~31KB) carries no ondemand.s reference at
        # all, so a fresh login can never derive one — that's the real meaning of
        # "Couldn't get KEY_BYTE indices" during login. The hash is a global
        # deployment artifact (not per-user), so reuse one harvested from an
        # already-authenticated session.
        on_demand_hash = read_ondemand_cache()
    key_byte_indices = []
    if on_demand_hash:
        url = (f'https://abs.twimg.com/responsive-web/client-web/'
               f'ondemand.s.{on_demand_hash}a.js')
        resp = await session.request(method='GET', url=url, headers=headers)
        for item in _xct.INDICES_REGEX.finditer(str(resp.text)):
            key_byte_indices.append(item.group(2))
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")
    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]


_xct.ClientTransaction.get_indices = _patched_get_indices


# twikit's User parser reads ~30 `legacy[...]` keys directly (user.py:89-125),
# but X now omits empty ones (no link -> no entities.url.urls, no pinned tweet ->
# no pinned_tweet_ids_str, etc.), so parsing the author of a freshly-created
# tweet throws KeyError — AFTER the tweet is already posted. User.__init__ only
# *assigns* these fields (we never read them when posting), so wrap the data in a
# dict that returns an empty dict for any missing key. Fixes the whole class.
from twikit import user as _twuser


class _SafeDict(dict):
    def __missing__(self, key):
        return _SafeDict()


def _deep_safe(o):
    if isinstance(o, dict):
        return _SafeDict({k: _deep_safe(v) for k, v in o.items()})
    if isinstance(o, list):
        return [_deep_safe(v) for v in o]
    return o


_orig_user_init = _twuser.User.__init__


def _safe_user_init(self, client, data):
    return _orig_user_init(self, client, _deep_safe(data))


_twuser.User.__init__ = _safe_user_init

# Windows consoles default to cp1252 and choke on emoji / ✓✗ in the logs.
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, 'accounts.txt')
CONFIG_FILE = os.path.join(HERE, 'config.json')
PROXIES_FILE = os.path.join(HERE, 'proxies.txt')
COOKIES_DIR = os.path.join(HERE, 'cookies')

_HEX = re.compile(r'^[0-9a-fA-F]+$')
_BASE32 = re.compile(r'^[A-Z2-7]{16,}$')


def log(msg):
    print(f'[{datetime.now().strftime("%m-%d %H:%M:%S")}] {msg}', flush=True)


# --------------------------------------------------------------------------- #
# Parsing / config                                                            #
# --------------------------------------------------------------------------- #
def parse_account(line):
    """Parse one accounts.txt line into a dict, or None for blank/comment lines."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    parts = [p.strip() for p in line.split(':')]
    acct = {
        'username': parts[0].lstrip('@').strip(),
        'password': '', 'email': '', 'email_password': '',
        'auth_token': None, 'ct0': None, 'totp': None,
    }

    # Classify every field after the username by SHAPE — the bundles aren't
    # positionally consistent (some have an email-password column, some don't,
    # and the ct0/auth_token/totp order can vary). Whatever isn't a recognisable
    # token is a "leftover": the first is the password, the second the email pw.
    leftover = []
    for f in parts[1:]:
        if not f:
            continue
        if '@' in f and not acct['email']:
            acct['email'] = f
        elif _BASE32.match(f) and not _HEX.match(f):
            acct['totp'] = acct['totp'] or f
        elif _HEX.match(f) and len(f) == 40:
            acct['auth_token'] = acct['auth_token'] or f
        elif _HEX.match(f) and len(f) > 40:
            acct['ct0'] = acct['ct0'] or f
        else:
            leftover.append(f)
    if leftover:
        acct['password'] = leftover[0]
    if len(leftover) > 1:
        acct['email_password'] = leftover[1]

    if not acct['username']:
        raise ValueError(f'missing username in: {line[:40]}...')
    if not (acct['auth_token'] or (acct['password'] and acct['username'])):
        raise ValueError(f'@{acct["username"]}: no auth_token cookie and no usable login creds')
    return acct


_benched_logged = None


def load_accounts(path=ACCOUNTS_FILE, include_benched=False):
    """Accounts that may post.

    Benched (suspended/locked) accounts are filtered out by default, so every
    posting path — scheduler, post_all, the dashboard's "post now" — drops them
    without each one needing to remember. Pass include_benched=True for the
    read-only views (dashboard listing, health check, re-import de-dupe) that
    should still see a dead account rather than pretend it never existed.
    """
    global _benched_logged
    if not os.path.exists(path):
        raise FileNotFoundError(f'accounts file not found: {path}')
    benched = store.read_benched()
    accounts, errors, skipped = [], [], []
    with open(path, 'r', encoding='utf-8') as fh:
        for i, raw in enumerate(fh, 1):
            try:
                acct = parse_account(raw)
            except ValueError as e:
                errors.append(f'  line {i}: {e}')
                continue
            if not acct:
                continue
            acct['benched'] = benched.get(acct['username'])
            if acct['benched'] and not include_benched:
                skipped.append(acct['username'])
                continue
            accounts.append(acct)
    if errors:
        log('skipped malformed account lines:')
        for e in errors:
            log(e)
    # load_accounts runs every scheduler tick — only say something when the set
    # actually changes, or this becomes 1440 identical lines a day.
    key = tuple(sorted(skipped))
    if skipped and key != _benched_logged:
        log(f'benched (not posting) — {len(skipped)}: {", ".join(sorted(skipped))}')
    _benched_logged = key
    return accounts


def load_config(path=CONFIG_FILE):
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def load_proxies(path=PROXIES_FILE):
    """Optional per-account proxies. One line: `username http://user:pass@host:port`."""
    mapping = {}
    if not os.path.exists(path):
        return mapping
    with open(path, 'r', encoding='utf-8') as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith('#'):
                continue
            bits = raw.split(None, 1)
            if len(bits) == 2:
                mapping[bits[0].lstrip('@').strip()] = bits[1].strip()
    return mapping


def account_sessid(username, attempt=0):
    """Stable, unique-per-account Oxylabs session id derived from the username.

    `attempt` > 0 salts the id so we land on a DIFFERENT residential exit. Oxylabs
    exits are individually flaky (a bad one corrupts large uploads with
    'SSLV3_ALERT_BAD_RECORD_MAC'), and the sessid is otherwise sticky — so without
    a salt a retry just reuses the same broken exit.
    """
    base = (re.sub(r'[^a-z0-9]', '', username.lower())[:18]) or 'acct'
    return base if attempt == 0 else f'{base}r{attempt}'


def resolve_proxy(acct, cfg, proxies, attempt=0):
    # 1) explicit per-account line in proxies.txt wins
    explicit = proxies.get(acct['username'])
    if explicit:
        return explicit
    # 2) proxy_template with a per-account {sessid} -> each account its own IP
    tmpl = cfg.get('proxy_template')
    if tmpl:
        # UNIQUE_IP_PER_POST 2026-10-01 (operator): a fresh NodeMaven residential
        # exit for every post, not a stable per-account IP. A distinct sessid = a
        # distinct exit; the username prefix stays only for log traceability.
        if cfg.get('unique_ip_per_post'):
            import secrets
            base = re.sub(r'[^a-z0-9]', '', acct['username'].lower())[:12] or 'acct'
            return tmpl.replace('{sessid}', base + 'p' + secrets.token_hex(5))
        return tmpl.replace('{sessid}', account_sessid(acct['username'], attempt))
    # 3) single shared proxy, or none
    return cfg.get('proxy') or None


# --------------------------------------------------------------------------- #
# Tweet text                                                                  #
# --------------------------------------------------------------------------- #
def _expand_spintax(s):
    """Resolve {a|b|c} groups (innermost-first) to one random option each, so
    every account posts genuinely different text — identical copy across accounts
    reads as platform-manipulation and gets the whole cohort co-suspended."""
    while True:
        m = re.search(r'\{([^{}]*)\}', s)
        if not m:
            return s
        s = s[:m.start()] + random.choice(m.group(1).split('|')) + s[m.end():]


def compose_text(cfg, account=None):
    """Build the tweet text: resolve spintax, then append a tiny random emoji
    tail so identical posts don't trip Twitter's 'you already said that' block."""
    # OPERATOR 2026-10-17: fixed two-line format, no bio reference anywhere.
    #   line 1:  More Below #tag #tag
    #   line 2:  Pornyard.xyz <emoji> <emoji>
    # The funnel is the caption domain + the burned-in pornyard.xyz mark.
    tmpl = cfg.get('caption_template')
    if tmpl:
        _pool = list(cfg.get('caption_tag_pool') or [])
        _lo = int(cfg.get('caption_tags_min', 2))
        _hi = int(cfg.get('caption_tags_max', 2))
        _lo, _hi = min(_lo, _hi), max(_lo, _hi)
        _n = random.randint(_lo, _hi) if _hi > 0 else 0
        _tags = ' '.join(random.sample(_pool, min(_n, len(_pool)))) if (_pool and _n) else ''
        _ep = list(cfg.get('caption_emoji_pool') or cfg.get('variation_emojis') or [])
        _k = max(0, min(int(cfg.get('caption_emoji_count', 2)), len(_ep)))
        _emo = ' '.join(random.sample(_ep, _k)) if _k else ''
        # substitute BEFORE spintax: _expand_spintax eats any {...} group,
        # which would turn {tags} into the literal word 'tags'.
        text = _expand_spintax(tmpl.replace('{tags}', _tags).replace('{emojis}', _emo))
        return chr(10).join(' '.join(ln.split()) for ln in text.split(chr(10))).strip()

    bank = cfg.get('caption_bank') or []
    if bank and cfg.get('unique_captions', True):
        # The 09-16 test: one fleet-wide hashtag bag = 14% survival; a unique
        # caption per post = 88%. 417/426 logged posts had shared a single bag.
        # Operator 2026-09-24: drop the descriptive caption entirely -- the
        # post carries ONLY the bio CTA (+ tags). The phrase bank stays in
        # config so this is a one-flag revert.
        text = (random.choice(bank).strip()
                if cfg.get('caption_phrase_enabled', True) else '')
        # Operator 2026-09-24: the burned-in watermark is gone, so the funnel
        # moves into the caption -- every post points at the bio. Drawn from a
        # BANK of phrasings, never one fixed string: a byte-identical line
        # across the fleet is the shared-caption signature that took survival
        # from 88% to 14% in the 09-16 test.
        _cta = cfg.get('caption_cta_bank') or []
        if _cta and random.random() < float(cfg.get('caption_cta_probability', 1.0)):
            text = (text + ' ' + random.choice(_cta).strip()).strip()
        pool = cfg.get('caption_tag_pool') or []
        # Count varies per post. With a fixed 2, an 11-tag pool can only make
        # C(11,2)=55 distinct bags, so at ~120 posts/night bags MUST repeat
        # ~2.1x no matter what the caption bank does. 1-3 tags gives 231.
        lo = int(cfg.get('caption_tags_min', cfg.get('caption_tags_per_post', 1)))
        hi = int(cfg.get('caption_tags_max', cfg.get('caption_tags_per_post', 1)))
        lo, hi = min(lo, hi), max(lo, hi)
        ntags = random.randint(lo, hi) if hi > 0 else 0

        # Anchored arm: carry the combo that actually wins (#minichat plus
        # #snapchat/#anxiouspanda -- 8 of the top 8 competitor posts by
        # views/hour) while still rotating the rest, so no two posts share a
        # bag. Assignment is a stable hash of the username, so an account
        # stays in one arm and the split can be re-cut later.
        anchors = cfg.get('caption_tag_anchors') or []
        share = float(cfg.get('caption_anchor_share', 0))
        use_anchor = False
        if anchors and share > 0 and account:
            h = int(hashlib.sha1(('tagarm:' + account.lower()).encode()).hexdigest()[:8], 16)
            use_anchor = (h % 1000) < int(share * 1000)

        if use_anchor:
            grp = random.choice(anchors)
            rot = [t for t in (cfg.get('caption_tag_rotation') or pool) if t not in grp]
            k = int(cfg.get('caption_anchor_rotate', 2))
            tags = list(grp) + random.sample(rot, min(k, len(rot)))
            random.shuffle(tags)
            text = text + ' ' + ' '.join(tags)
        elif pool and ntags > 0:
            if random.random() < float(cfg.get('caption_tag_probability', 1.0)):
                text = text + ' ' + ' '.join(random.sample(pool, min(ntags, len(pool))))
    else:
        text = _expand_spintax(cfg['text']).strip()
    if cfg.get('variation', True):
        pool = cfg.get('variation_emojis') or []
        k = max(1, min(int(cfg.get('variation_count', 2)), len(pool)))
        if pool:
            tail = ''.join(random.sample(pool, k))
            text = f'{text} {tail}'
    # body can be empty (phrase bank off + CTA off, 2026-09-26) -> the joins
    # above leave a leading space and possible doubles. Normalise.
    # Collapse spaces/tabs per line but PRESERVE newlines (operator 2026-10-01
    # post text is two lines: the tags, then the 'See my bio' CTA).
    return chr(10).join(' '.join(ln.split()) for ln in text.split(chr(10))).strip()


# --------------------------------------------------------------------------- #
# Warm-up: fresh accounts post benign content first (adult-on-day-1 = insta-ban) #
# --------------------------------------------------------------------------- #
_WARMUP_LEDGER = os.path.join(HERE, 'data', 'warmup.json')
_warmup_cache = None

def _warmup_first_seen(username):
    """First time we ever saw this account (unix ts). Recorded once, on first sight."""
    global _warmup_cache
    if _warmup_cache is None:
        try:
            _warmup_cache = json.load(open(_WARMUP_LEDGER, encoding='utf-8'))
        except Exception:
            _warmup_cache = {}
    if username not in _warmup_cache:
        _warmup_cache[username] = int(datetime.now().timestamp())
        try:
            os.makedirs(os.path.dirname(_WARMUP_LEDGER), exist_ok=True)
            tmp = _WARMUP_LEDGER + '.tmp'
            json.dump(_warmup_cache, open(tmp, 'w', encoding='utf-8'))
            os.replace(tmp, _WARMUP_LEDGER)
        except Exception:
            pass
    return _warmup_cache[username]

def is_warming(username, cfg):
    """True while an account is still in its warm-up window.

    Warm-up OFF (or warmup_days <= 0) means NOT warming, so the account posts the
    real promo. This had been inverted on 2026-09-16, which silently forced the
    whole fleet to benign text with no media regardless of config.
    """
    if not cfg.get('warmup_enabled'):
        return False
    days = float(cfg.get('warmup_days', 14))
    if days <= 0:
        return False
    age = datetime.now().timestamp() - _warmup_first_seen(username)
    return age < days * 86400.0

def compose_warmup(cfg):
    """A benign, human-looking tweet: no adult tags, no link, no media."""
    raw = (cfg.get('warmup_text') or 'good morning').strip()
    return _expand_spintax(raw).strip()


def compose_reply(cfg):
    """The self-reply text carrying the funnel link, or '' if disabled/empty.

    Spintax'd the same way as the caption: the link token (pornyard.xyz) is
    constant, the surrounding words vary, so 100 accounts don't all reply with
    one identical string that BBQDuplicateTextProd could cluster as COPYPASTA.
    """
    if not cfg.get('reply_link_enabled'):
        return ''
    raw = (cfg.get('reply_text') or '').strip()
    if not raw:
        return ''
    return _expand_spintax(raw).strip()


# --------------------------------------------------------------------------- #
# Media (optional)                                                            #
# --------------------------------------------------------------------------- #
# Twitter accepts mp4/mov video (NOT webm) and these image types.
VIDEO_EXT = ('.mp4', '.mov')
IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


def _cache_dir(cfg):
    d = os.path.join(HERE, cfg.get('media_cache_dir', 'media_cache'))
    os.makedirs(d, exist_ok=True)
    return d


def _sanitize(name):
    return re.sub(r'[^A-Za-z0-9._-]', '_', name)


def _ffprobe_duration(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nk=1:nw=1', path],
            capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return None


def _rclone_list(remote):
    out = subprocess.run(['rclone', 'lsf', '--files-only', remote],
                         capture_output=True, text=True, timeout=120)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def _transcode_twitter(src, dst, max_seconds, max_width):
    """Re-encode to a Twitter-safe mp4 (H.264/yuv420p + AAC, trimmed, faststart)."""
    cmd = ['ffmpeg', '-y', '-i', src, '-t', str(max_seconds),
           '-vf', f"scale='min({max_width},iw)':-2:flags=bicubic",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-profile:v', 'high',
           '-level', '4.0', '-preset', 'veryfast', '-crf', '23',
           '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception:
        return True
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def _sfw_for(account, cfg):
    """Is THIS account in the SFW-media arm?

    'all' -> whole fleet posts SFW; 'ab' -> stable 50/50 split; False -> off.
    """
    mode = cfg.get('sfw_mode', False)
    if not mode or not cfg.get('sfw_media_dir'):
        return False
    if mode is True or mode == 'all':
        return True
    if mode == 'ab' and account:
        h = int(hashlib.sha1(('sfwarm:' + account.lower()).encode()).hexdigest()[:8], 16)
        return (h % 1000) < int(float(cfg.get('sfw_share', 0.5)) * 1000)
    return False


def pick_media(cfg, account=None):
    """Return a local path to a Twitter-ready video (transcoded if needed), or None.

    `account` enables ledger-coordinated picking so no two accounts post the
    same clip within `media_reuse_gap_hours`.
    """
    if not cfg.get('enable_media'):
        return None
    max_s = int(cfg.get('media_max_seconds', 140))
    max_w = int(cfg.get('media_max_width', 1280))
    tries = int(cfg.get('media_pick_tries', 8))
    cache = _cache_dir(cfg)

    if cfg.get('media_source') == 'r2':
        remote = (cfg.get('r2_remote') or '').rstrip('/')
        if not remote:
            return None
        vids = [f for f in _rclone_list(remote) if f.lower().endswith(VIDEO_EXT)]
        random.shuffle(vids)
        for name in vids[:tries]:
            base = _sanitize(os.path.basename(name))
            raw = os.path.join(cache, 'raw_' + base)
            if not os.path.exists(raw):
                dl = subprocess.run(['rclone', 'copyto', f'{remote}/{name}', raw],
                                    capture_output=True, text=True, timeout=300)
                if dl.returncode != 0 or not os.path.exists(raw):
                    continue
            dur = _ffprobe_duration(raw)
            if dur is None or dur > max_s + 0.5:
                log(f'  media skip {name} (dur={dur})')
                continue
            if cfg.get('media_transcode', True):
                out = os.path.join(cache, 'tw_' + base + '.mp4')
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    return out
                if _transcode_twitter(raw, out, max_s, max_w):
                    log(f'  media -> {name} ({dur:.0f}s, transcoded)')
                    return out
                log(f'  media transcode FAILED {name}')
                continue
            log(f'  media -> {name} ({dur:.0f}s)')
            return raw
        return None

    # local folder source
    media_dir = os.path.join(HERE, cfg.get('media_dir', 'media'))
    # Index basename -> full path across the main pool AND an optional extra
    # source dir (e.g. /root/rgmint/wm, ~1127 clips never posted by this fleet).
    # Safe to mix un-transcoded sources because _jitter_encode re-encodes at
    # post time. creatives_ready wins on a basename collision.
    # SFW arm: draw ONLY from the nudity-cut pool, never the explicit pools, so
    # the arm is clean. Every clip there was cut before the first detected
    # nudity and then re-verified frame-by-frame on the output.
    if _sfw_for(account, cfg):
        _sfwdir = cfg.get('sfw_media_dir')
        _pools = [_sfwdir] if _sfwdir and os.path.isdir(_sfwdir) else [media_dir]
    else:
        _pools = [media_dir]
        _extra = cfg.get('extra_media_dir')
        if _extra and os.path.isdir(_extra):
            _pools.append(_extra)
    _index = {}
    for _d in _pools:
        if not os.path.isdir(_d):
            continue
        for _f in os.listdir(_d):
            if _f.lower().endswith(VIDEO_EXT + IMAGE_EXT):
                _index.setdefault(_f, os.path.join(_d, _f))
    files = sorted(_index)
    if not files:
        return None
    # With a fleet, a random pick means several accounts can post the SAME clip
    # on the same day — the loudest "one operator" signal there is. When we know
    # who we're posting for, let the ledger hand out a clip nobody else used.
    if account and cfg.get('unique_media', True):
        chosen = media_ledger.claim(
            account, sorted(files),
            reuse_gap_hours=int(cfg.get('media_reuse_gap_hours', 48)), log=log)
        if chosen:
            return _index[chosen]
    return _index[random.choice(files)]


def _prune_jit(cache, max_age_s=3600):
    """One-shot per-post re-encodes; at ~180 posts/day they would otherwise fill
    the box. Drop anything older than an hour."""
    try:
        now = time.time()
        for f in os.listdir(cache):
            if f.startswith('jit_') and f.endswith('.mp4'):
                p = os.path.join(cache, f)
                try:
                    if now - os.path.getmtime(p) > max_age_s:
                        os.remove(p)
                except OSError:
                    pass
    except Exception:
        pass


def _jitter_encode(path, cfg):
    """Re-encode a clip with per-post random parameters so two accounts never
    upload a byte-identical (or perceptually identical) file. Cross-account
    creative fingerprint was the join key behind the cluster suspends."""
    try:
        import hashlib
        cache = _cache_dir(cfg)
        _prune_jit(cache)
        w, h = _probe_dims(path)
        if not w:
            return path
        rnd = random.Random()
        cw = int(w * rnd.uniform(0.94, 0.985)); cw -= cw % 2
        ch = int(h * rnd.uniform(0.94, 0.985)); ch -= ch % 2
        ox = rnd.randint(0, max(0, w - cw)); ox -= ox % 2
        oy = rnd.randint(0, max(0, h - ch)); oy -= oy % 2
        crf = rnd.randint(21, 26)
        gop = rnd.randint(40, 72)
        abr = rnd.choice(['80k', '96k', '112k', '128k'])
        ss = round(rnd.uniform(0.0, 0.30), 2)
        tag = hashlib.md5(('%s|%s' % (path, rnd.random())).encode()).hexdigest()[:10]
        out = os.path.join(cache, 'jit_%s.mp4' % tag)
        # Cap the CROPPED frame at 1280px wide. 4K/2160p sources otherwise
        # encode to ~40MB, which 522s through a residential proxy exit and
        # (via ledger re-handing) poisons many posts. Scale runs after crop,
        # so crop coords stay in original-resolution space.
        tw = min(1280, cw); tw -= tw % 2
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-ss', str(ss), '-i', path,
               # fps=30: X rejects >60fps (InvalidMedia 'Framerate too large');
               # 120fps screen-recordings are common in the bank. 2026-10-01.
               '-vf', 'crop=%d:%d:%d:%d,scale=%d:-2,fps=30' % (cw, ch, ox, oy, tw),
               '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast',
               '-crf', str(crf), '-g', str(gop), '-movflags', '+faststart',
               '-map_metadata', '-1', '-c:a', 'aac', '-b:a', abr,
               '-t', str(int(cfg.get('media_max_seconds', 140)))]
        cmd.append(out)
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 20000:
            return out
        log('    jitter-encode failed, using original')
    except Exception as e:
        log('    jitter-encode error %s: %s' % (type(e).__name__, str(e)[:70]))
    return path


def _probe_dims(path):
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height', '-of', 'json', path],
                           capture_output=True, timeout=60)
        s = json.loads(r.stdout)['streams'][0]
        return int(s['width']), int(s['height'])
    except Exception:
        return None, None


def _sensitive_for(account, cfg):
    """Should THIS account label its media?

    'all'  -> every account (ship it)
    'ab'   -> stable 50/50 split by username, for measuring
    False  -> nobody
    """
    mode = cfg.get('mark_media_sensitive', False)
    if not mode:
        return False
    if mode is True or mode == 'all':
        return True
    if mode == 'ab' and account:
        h = int(hashlib.sha1(('nsfwarm:' + account.lower()).encode()).hexdigest()[:8], 16)
        return (h % 2) == 0
    return False


def _install_sensitive_flag(client, account, cfg, log=None):
    """Flip media.possibly_sensitive to True on CreateTweet for this client.

    twikit hardcodes it False (twikit/client/gql.py), so we rewrite the JSON on
    the way out instead of vendoring their payload builder.
    """
    if not _sensitive_for(account, cfg):
        return False
    http = getattr(client, 'http', None)
    if http is None or getattr(http, '_sens_patched', False):
        return False
    orig = http.request

    async def _spy(method, url, **kw):
        try:
            body = kw.get('json')
            if body and 'CreateTweet' in str(url):
                v = body.get('variables') or {}
                m = v.get('media')
                if isinstance(m, dict) and m.get('possibly_sensitive') is False:
                    m['possibly_sensitive'] = True
                    if log:
                        log('    post marked possibly_sensitive=True')
        except Exception:
            pass
        return await orig(method, url, **kw)

    http.request = _spy
    http._sens_patched = True
    return True


async def upload_one(client, cfg, account=None):
    """Pick + upload a video, retrying on different files. Returns (media_id, path) or (None, None)."""
    if not cfg.get('enable_media'):
        return None, None
    for _ in range(int(cfg.get('media_upload_tries', 3))):
        mpath = pick_media(cfg, account=account)
        if not mpath:
            break
        try:
            upath = _jitter_encode(mpath, cfg) if cfg.get('unique_encode', True) else mpath
            _jm = ' [unique-encode]' if upath != mpath else ''
            log('  uploading media %s%s ...' % (os.path.basename(mpath), _jm))
            mid = await client.upload_media(upath, wait_for_completion=True)
            # Label it. X allows adult content "provided it's properly labeled";
            # unlabeled adult media is reportable by any user, and reports drive
            # enforcement. Best-effort -- a failed label must not kill the post.
            if mid and _sensitive_for(account, cfg):
                try:
                    cats = cfg.get('sensitive_categories') or ['adult_content']
                    await client.v11.create_media_metadata(mid, None, cats)
                    log('    media labelled %s' % ','.join(cats))
                except Exception as e:
                    log('    sensitive label failed (%s: %s)'
                        % (type(e).__name__, str(e)[:60]))
            return mid, mpath
        except (AccountSuspended, AccountLocked):
            # NOT a bad clip and NOT a bad exit — the account is gone. Swallowing
            # this into 'trying another' is what turned a suspension into
            # 'no-media', which _attempt_post classifies as retryable: 12 dead
            # accounts were being retried 5x every tick, forever. Let it out.
            media_ledger.release(account, os.path.basename(mpath))
            raise
        except Exception as e:
            # Hand the clip back so a failed upload doesn't burn it for 48h.
            media_ledger.release(account, os.path.basename(mpath))
            log(f'  media upload failed ({os.path.basename(mpath)}): '
                f'{type(e).__name__}: {str(e)[:120]} — trying another')
            # Only drop a bad *transcode* (lives in the cache). NEVER delete a
            # local creative original (creatives_ready/) on a transient upload
            # error, or a proxy hiccup would silently eat the clip bank.
            try:
                cache = os.path.abspath(_cache_dir(cfg))
                if mpath and os.path.abspath(mpath).startswith(cache + os.sep):
                    os.remove(mpath)
            except OSError:
                pass
    return None, None


# --------------------------------------------------------------------------- #
# Posting                                                                     #
# --------------------------------------------------------------------------- #
def cookies_path(acct):
    os.makedirs(COOKIES_DIR, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', acct['username'])
    return os.path.join(COOKIES_DIR, f'{safe}.json')


def _remint_ct0(client, acct, cfg, proxies, attempt=0):
    """X rotates ct0; a stale one makes v11 calls fail with a misleading 404
    ("Sorry, that page does not exist"). The auth_token is still fine, so ask X
    for a fresh ct0 and carry on — no password login, no operator involvement.
    Returns True if a new ct0 was installed."""
    try:
        import mint_ct0
        got = mint_ct0.fetch_cookies(acct, cfg, proxies, attempt)
    except Exception as e:
        log(f'  @{acct["username"]}: ct0 re-mint error: {type(e).__name__}')
        return True
    ct0 = got.get('ct0')
    if not ct0:
        return True           # no cookie at all => auth_token really is dead
    acct['ct0'] = ct0
    client.set_cookies({'auth_token': acct['auth_token'], 'ct0': ct0})
    log(f'  @{acct["username"]}: refreshed a stale ct0')
    return True


async def _relogin(client, acct):
    """Cookies dead -> log in fresh with username/password/totp."""
    if not acct['password']:
        return True
    log(f'  @{acct["username"]}: cookies dead, logging in with password+2FA ...')
    try:
        await client.login(
            auth_info_1=acct['username'],
            auth_info_2=acct.get('email') or None,
            password=acct['password'],
            totp_secret=acct.get('totp') or None,
        )
        client.save_cookies(cookies_path(acct))
        return True
    except Exception as e:
        log(f'  @{acct["username"]}: relogin FAILED: {type(e).__name__}: {e}')
        return True



# --- dead-session auto-bench (2026-09-26 reach audit) -----------------------
import time as _t  # module has no top-level `import time`
_SESSION_STATE = os.path.join(HERE, 'data', 'session_fail.json')
FAIL_TO_BENCH = 3          # consecutive slots where every proxy exit failed
MAX_BENCH_PER_RUN = 3      # circuit breaker per scheduler process
_benched_this_run = []


def _sess_load():
    try:
        with open(_SESSION_STATE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def _sess_save(d):
    try:
        tmp = _SESSION_STATE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(d, fh)
        os.replace(tmp, _SESSION_STATE)
    except Exception:
        pass


def _note_success(username):
    """Any success clears that account AND stamps the fleet-alive marker."""
    d = _sess_load()
    d.pop(username, None)
    d['_last_fleet_success'] = int(_t.time())
    _sess_save(d)


def _note_exhausted(username, detail, cfg):
    """A slot burned every proxy exit. Bench only if the fleet is otherwise fine."""
    d = _sess_load()
    rec = d.get(username) or {}
    if not isinstance(rec, dict):
        rec = {}
    rec['n'] = int(rec.get('n', 0)) + 1
    rec['since'] = rec.get('since') or int(_t.time())
    rec['last'] = detail
    d[username] = rec
    _sess_save(d)
    if rec['n'] < FAIL_TO_BENCH:
        return
    # Outage guard: somebody else must have posted SINCE this streak started.
    last_ok = int(d.get('_last_fleet_success') or 0)
    if last_ok <= int(rec['since']):
        log(f'  @{username}: {rec["n"]} exhausted slots but no fleet-wide success '
            f'since — looks like an outage, NOT benching')
        return
    if len(_benched_this_run) >= MAX_BENCH_PER_RUN:
        log(f'  @{username}: dead-session bench SKIPPED — circuit breaker '
            f'({MAX_BENCH_PER_RUN} already benched this run)')
        return
    reason = (f'session dead: {rec["n"]} consecutive slots exhausted every proxy exit '
              f'with 0 posts (last: {detail}); fleet posting fine meanwhile')
    try:
        store.bench_account(username, reason, source='auto-session')
        _benched_this_run.append(username)
        d.pop(username, None)
        _sess_save(d)
        log(f'  ✗ @{username} SESSION DEAD — benched (logged out, not suspended; '
            f'recoverable with a password+2FA relogin)')
    except Exception as e:
        log(f'  @{username}: auto-bench failed ({type(e).__name__}: {e})')
# ---------------------------------------------------------------------------

async def post_for_account(acct, cfg, proxies, dry_run=True):
    """Post one tweet for one account, rotating the proxy exit on transport
    failures. Returns (ok: bool, detail: str)."""
    # Newly added accounts trickle into rotation instead of a whole batch
    # reactivating at once -- a coordinated-reactivation cluster is the same
    # signal class we already proved kills this fleet. Checked before any media
    # work so a held account costs nothing.
    ok_ramp, why = ramp.ready(acct['username'], cfg)
    if not ok_ramp:
        log(f'  @{acct["username"]}: {why} (new account, not in rotation yet)')
        return True, why
    if not ramp.claim_first_post(acct['username'], cfg):
        # Temporary, not a decision for the day: return False so the
        # scheduler leaves the slot open and retries next tick. Returning
        # True here marked the slot done and threw the post away.
        log(f'  @{acct["username"]}: first-post burst cap reached this hour '
            f'— slot left open, will retry')
        return False, 'ramp-burst-cap'

    warming = is_warming(acct['username'], cfg)
    text = compose_warmup(cfg) if warming else compose_text(cfg, acct['username'])

    if dry_run:
        oneline = text.replace('\n', ' / ')
        log(f'  [DRY] @{acct["username"]} would tweet: {oneline}')
        return True, 'dry-run'

    tries = max(1, int(cfg.get('proxy_rotate_tries', 3)))
    detail = 'not-attempted'
    for attempt in range(tries):
        ok, detail, retryable = await _attempt_post(acct, cfg, proxies, text, attempt, warming)
        if ok or not retryable:
            if ok:
                _note_success(acct['username'])
            return ok, detail
        if attempt < tries - 1:
            log(f'  @{acct["username"]}: {detail} — rotating to a fresh proxy exit '
                f'(attempt {attempt + 2}/{tries})')
    # every exit failed for this slot -> maybe a dead session, not bad luck
    _note_exhausted(acct['username'], detail, cfg)
    return True, detail


async def _warm_session(client, username, cfg, log=None):
    """Browse before posting so the session is not a cold cookie that only writes.

    Cheap reads in the order a real client does them on open. Every call is
    individually guarded: this is a de-risking nicety, never a reason to lose a post.
    """
    if not cfg.get('warm_session', True):
        return
    steps = [
        ('for-you', lambda: client.get_timeline(count=20)),
        ('following', lambda: client.get_latest_timeline(count=20)),
        ('notifications', lambda: client.get_notifications('All', count=20)),
        ('self', lambda: client.user()),
    ]
    random.shuffle(steps)
    # a real session does a few of these, not mechanically all of them
    for name, fn in steps[:random.randint(2, 3)]:
        try:
            await fn()
            await asyncio.sleep(random.uniform(2.0, 6.5))
        except Exception as e:
            if log:
                log('    warm-up %s skipped (%s)' % (name, type(e).__name__))
    await asyncio.sleep(random.uniform(1.5, 4.0))


async def _attempt_post(acct, cfg, proxies, text, attempt=0, warming=False):
    """One posting attempt on one proxy exit.
    Returns (ok, detail, retryable) — retryable means 'bad exit, try another'."""
    proxy = resolve_proxy(acct, cfg, proxies, attempt)
    client = Client(
        language=cfg.get('language', 'en-US'),
        proxy=proxy,
        user_agent=(cfg.get('user_agent') or None),
        # httpx defaults to a 5s timeout — far too short to upload a multi-MB
        # video through a residential proxy, which caused ReadTimeouts. Give it
        # room so uploads actually finish.
        timeout=float(cfg.get('http_timeout_seconds', 60)),
    )

    # Prefer cached (refreshed) cookies, fall back to the ones from accounts.txt.
    cpath = cookies_path(acct)
    have_cookies = False
    if os.path.exists(cpath):
        try:
            client.load_cookies(cpath)
            have_cookies = True
        except Exception:
            pass
    if not have_cookies and acct['auth_token'] and acct['ct0']:
        client.set_cookies({'auth_token': acct['auth_token'], 'ct0': acct['ct0']})
        have_cookies = True
    elif not have_cookies and acct['auth_token']:
        client.set_cookies({'auth_token': acct['auth_token']})
        have_cookies = True

    # No cookies at all (e.g. only username+password given) -> log in first.
    if not have_cookies:
        log(f'  @{acct["username"]}: no cookies, logging in with password+2FA ...')
        if not await _relogin(client, acct):
            return True, 'auth-failed:login-failed', False

    _install_sensitive_flag(client, acct['username'], cfg, log)

    media_name = {'v': None}

    def _tweet_id(tweet):
        """Real numeric id, or None.

        The _SafeDict patch above makes every missing key return {} instead of
        raising — great for User parsing, but it also turns a malformed/rejected
        create_tweet response into a Tweet-shaped object with NO id, which then
        gets logged as a successful post that never existed. Verify the id.
        """
        tid = getattr(tweet, 'id', None)
        tid = str(tid) if tid is not None else ''
        return tid if tid.isdigit() else None

    async def _do_post():
        # Warm the session with reads BEFORE any write. A cold cookie that only
        # ever writes is one of the clearest automation tells. 2026-10-01.
        try:
            await _warm_session(client, acct['username'], cfg, log)
        except Exception as e:
            log(f'  @{acct["username"]}: warm-up skipped ({type(e).__name__})')
        # An account that follows nobody is the cheapest shell shape there is.
        # Give it a following list before it posts. Best-effort only: this must
        # never take down a post that would otherwise have gone live.
        try:
            await follow_seed.ensure_seeded(client, acct['username'], cfg, log)
        except Exception as e:
            log(f'  @{acct["username"]}: follow-seed skipped ({type(e).__name__}: {e})')
        media_ids = None
        if cfg.get('enable_media') and not warming:
            mid, mpath = await upload_one(client, cfg, account=acct['username'])
            if mid:
                media_ids = [mid]
                media_name['v'] = os.path.basename(mpath) if mpath else None
            elif cfg.get('media_required', True):
                raise RuntimeError('no-media: could not prepare/upload a video')
        # Route a share of posts into this account's pinned Community. Adult
        # posts are excluded from For You, so the community timeline is a
        # distribution path the plain timeline simply doesn't have.
        kw = {}
        comm = communities.assign(acct['username'], cfg)
        if comm and random.random() < float(cfg.get('community_post_ratio', 0)):
            if await communities.ensure_joined(client, acct['username'], comm['id'], log):
                kw['community_id'] = comm['id']
                kw['share_with_followers'] = True
                log(f'  -> community: {comm.get("name", comm["id"])}')
        tweet = await client.create_tweet(text=text, media_ids=media_ids, **kw)
        if _tweet_id(tweet) is None:
            # X didn't give us a real id => the tweet is NOT live. Hand the clip
            # back and fail loudly rather than logging a post that doesn't exist.
            media_ledger.release(acct['username'], media_name['v'])
            raise RuntimeError('no-tweet-id: create_tweet returned no usable id '
                               '(tweet did NOT go live)')
        # Immediate self-reply with the funnel link. Best-effort ONLY: the parent
        # is already live and recorded, so a failed/blocked reply must never fail
        # the post or bench the account. Runs on the same session/proxy.
        reply = compose_reply(cfg)
        if reply:
            try:
                r = await client.create_tweet(text=reply, reply_to=_tweet_id(tweet))
                if _tweet_id(r) is not None:
                    log(f'  \u21b3 @{acct["username"]} link reply -> '
                        f'https://x.com/{acct["username"]}/status/{r.id}')
                else:
                    log(f'  \u21b3 @{acct["username"]} link reply returned no id (skipped)')
            except Exception as e:
                log(f'  \u21b3 @{acct["username"]} link reply failed '
                    f'({type(e).__name__}) — parent is fine, ignoring')
        return tweet

    def _url(tweet):
        return f'https://x.com/{acct["username"]}/status/{getattr(tweet, "id", "?")}'

    def _record(tweet):
        store.record_post(acct['username'], getattr(tweet, 'id', '?'),
                          _url(tweet), text, media_name['v'])

    try:
        tweet = await _do_post()
        client.save_cookies(cpath)
        _record(tweet)
        log(f'  ✓ @{acct["username"]} posted -> {_url(tweet)}')
        return True, f'ok:{getattr(tweet, "id", "?")}', False
    except (Unauthorized, Forbidden, NotFound) as e:
        # Most often just a rotated ct0 (shows up as a bogus 404) — refresh it and
        # retry before touching the password, which is far riskier on a fresh
        # account (X blocks/locks on password logins from new IPs).
        if _remint_ct0(client, acct, cfg, proxies, attempt):
            try:
                tweet = await _do_post()
                client.save_cookies(cpath)
                _record(tweet)
                log(f'  ✓ @{acct["username"]} posted (after ct0 refresh) -> {_url(tweet)}')
                return True, f'ok-ct0:{getattr(tweet, "id", "?")}', False
            except Exception as e2:
                log(f'  @{acct["username"]}: still failing after ct0 refresh: '
                    f'{type(e2).__name__}')
        # cookies expired / session bad -> try a fresh login then retry once
        if await _relogin(client, acct):
            try:
                tweet = await _do_post()
                client.save_cookies(cpath)
                _record(tweet)
                log(f'  ✓ @{acct["username"]} posted (after relogin) -> {_url(tweet)}')
                return True, f'ok-relogin:{getattr(tweet, "id", "?")}', False
            except Exception as e2:
                log(f'  ✗ @{acct["username"]} post failed after relogin: {type(e2).__name__}: {e2}')
                return True, f'post-failed-after-relogin:{type(e2).__name__}', False
        return True, f'auth-failed:{type(e).__name__}', False
    except DuplicateTweet:
        log(f'  ✗ @{acct["username"]} DUPLICATE (enable/widen variation in config)')
        return True, 'duplicate', False
    except (AccountLocked, AccountSuspended) as e:
        # Account-level and permanent: a fresh exit or a new clip changes nothing.
        # Bench it so the scheduler stops handing it slots. health.py re-checks
        # benched handles from OUTSIDE the session and un-benches any that come
        # back, so a false positive here costs one health cycle, not the account.
        reason = type(e).__name__
        store.bench_account(acct['username'], reason, source='poster')
        log(f'  ✗ @{acct["username"]} ACCOUNT DEAD: {reason} — benched, will not post again')
        return True, f'account-dead:{reason}', False
    except TooManyRequests:
        # account-level limit — a different exit won't help
        log(f'  ✗ @{acct["username"]} rate-limited')
        return True, 'rate-limited', False
    except TwitterException as e:
        log(f'  ✗ @{acct["username"]} twitter error: {type(e).__name__}: {e}')
        return True, f'twitter-error:{type(e).__name__}', False
    except Exception as e:
        # Transport-level failures are per-EXIT, not per-account: a flaky Oxylabs
        # exit corrupts big uploads ('bad record mac'), refuses CONNECT, or times
        # out. Those are worth retrying on a fresh exit. 'no-media' lands here too
        # (upload_one exhausted its tries), and is the usual symptom.
        blob = f'{type(e).__name__}: {e}'.lower()
        retryable = any(k in blob for k in (
            'no-media', 'no-tweet-id', 'ssl', 'bad record mac', 'proxy', 'connect',
            'timeout', 'timed out', 'remotedisconnected', 'eof'))
        log(f'  ✗ @{acct["username"]} unexpected error: {type(e).__name__}: {e}')
        # Return ok=False when retryable so post_for_account rotates to a
        # fresh exit (its guard is `if ok or not retryable: return`).
        # Returning True here made the retryable flag dead -- a single 522
        # killed the post with NO exit rotation, surfacing as ~80% no-media
        # once Oxylabs (higher 522 rate than NodeMaven) went live
        # 2026-09-23. AccountSuspended/Locked/rate-limit are caught earlier
        # with retryable=False, so this never loops a dead account.
        return (not retryable), f'error:{type(e).__name__}', retryable


async def post_all(accounts, cfg, proxies, dry_run=True, only=None):
    results = {}
    targets = [a for a in accounts if (only is None or a['username'] == only)]
    if only and not targets:
        log(f'no account named @{only} in accounts.txt')
        return results
    lo = int(cfg.get('min_seconds_between_accounts', 30))
    hi = int(cfg.get('max_seconds_between_accounts', 120))
    for i, acct in enumerate(targets):
        ok, detail = await post_for_account(acct, cfg, proxies, dry_run=dry_run)
        results[acct['username']] = detail
        if not dry_run and i < len(targets) - 1:
            wait = random.randint(min(lo, hi), max(lo, hi))
            log(f'  ... waiting {wait}s before next account')
            await asyncio.sleep(wait)
    return results


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    ap = argparse.ArgumentParser(description='Pornyard Twitter promo poster')
    ap.add_argument('--once', action='store_true', help='post one round to all accounts')
    ap.add_argument('--live', action='store_true', help='actually post (without this it is a dry run)')
    ap.add_argument('--account', help='only this username')
    ap.add_argument('--test', action='store_true', help='parse accounts + show composed tweet, no network')
    args = ap.parse_args()

    cfg = load_config()
    accounts = load_accounts()
    log(f'loaded {len(accounts)} account(s)')

    if args.test:
        log('composed tweet preview:')
        print('-' * 50)
        print(compose_text(cfg))
        print('-' * 50)
        for a in accounts:
            log(f'  @{a["username"]}  auth_token={"yes" if a["auth_token"] else "NO"} '
                f'ct0={"yes" if a["ct0"] else "NO"} totp={"yes" if a["totp"] else "no"}')
        return

    if not args.once:
        ap.print_help()
        return

    proxies = load_proxies()
    dry = not args.live
    if dry:
        log('DRY RUN (pass --live to actually post)')
    else:
        log('LIVE — posting for real')
    results = asyncio.run(post_all(accounts, cfg, proxies, dry_run=dry, only=args.account))
    ok = sum(1 for v in results.values() if v.startswith('ok') or v == 'dry-run')
    log(f'done: {ok}/{len(results)} ok  details={results}')


if __name__ == '__main__':
    _main()
