# pytwitterpromo

A scheduler that posts one unique video per account per day across a pool of X
(Twitter) accounts, with per-account residential proxying, cookie/TOTP auth, a
warm-up ramp, caption/hashtag generation, and a local status dashboard.

## Layout

| file | role |
|------|------|
| `scheduler.py` | the loop: plans the day, calls the poster per account |
| `twitter_poster.py` | core posting: auth, media upload, caption build, proxy rotation |
| `caption_gen.py` | caption / hashtag / emoji generation |
| `ramp.py` | per-account warm-up gating (first-post rules) |
| `follow_seed.py` | warm-up follow actions |
| `stats.py` / `stats_runner.py` | metrics collection (views/likes/etc.) |
| `health.py` / `account_evidence.py` / `account_ages.py` | account liveness & audit |
| `autobench.py` / `refill_accounts.py` / `add_accounts.py` | pool management |
| `bank_creatives.py` / `media_ledger.py` | media bank + de-dup ledger |
| `store.py` | JSON state persistence |
| `dashboard.py` | local Flask-style status dashboard |
| `official_publisher.py` | optional official-API publishing path |
| `mint_ct0.py` / `browser_login.py` | cookie/ct0 acquisition |

## Setup

1. `python -m venv .venv && . .venv/bin/activate`
2. `pip install twikit httpx pyotp pillow flask`  (see imports; no pinned manifest shipped)
3. Copy `config.example.json` to `config.json` and fill in your own values
   (proxy template, media dir, caption pools, etc.).
4. Create `accounts.txt`, one account per line:
   `username:password:email:emailpass:auth_token:totp_seed`
5. `python scheduler.py --live`

## Note on what is NOT in this repo

All credentials and runtime state were intentionally excluded:
accounts, cookies, proxies, the real `config.json`, collected metrics, and the
media bank. `config.example.json` has every secret value replaced with a
placeholder. Bring your own accounts, proxies, and media.
