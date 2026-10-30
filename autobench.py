"""Record public evidence and restrict only explicit suspension notices.

Generic errors, login/age gates, network failures and partial documents cannot
establish suspension. Existing restrictions remain for manual review. This CLI
does not enable any timer or publishing job. --dry-run performs public reads
without persisting changes. No authenticated session sweep is supported.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import time

import account_evidence
import store
import twitter_poster as tp

PROBE_BYTES = account_evidence.MAX_RESPONSE_BYTES


def log(msg):
    print('[%s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg), flush=True)


def _public_evidence(username, cfg, proxies):
    proxy = tp.resolve_proxy({'username': username, 'auth_token': None}, cfg, proxies, 0)
    return account_evidence.fetch_public_evidence(username, proxy=proxy, max_bytes=PROBE_BYTES)


def public_status_cheap(username, cfg, proxies, tries=2):
    """Compatibility result from one request; tries is intentionally ignored."""
    evidence = _public_evidence(username, cfg, proxies)
    account_evidence.persist_evidence(username, evidence)
    return {'readable': 'ALIVE', 'not_found': 'NOT-FOUND',
            'login_required': 'LOGIN-REQUIRED', 'rate_limited': 'RATE-LIMITED',
            'unavailable': 'UNAVAILABLE'}.get(evidence['status'], evidence['status'].upper())


def session_dead(acct, cfg, proxies, tries=2):
    """Legacy sweep disabled: session errors cannot prove suspension."""
    return False


def recent_posters(minutes):
    cutoff = time.time() - minutes * 60
    seen = {}
    for row in store.read_posts():
        if row.get('account') and row.get('ts', 0) >= cutoff:
            seen[row['account']] = max(seen.get(row['account'], 0), row['ts'])
    return [name for name, _ in sorted(seen.items(), key=lambda item: -item[1])]


def main():
    ap = argparse.ArgumentParser(description='Record public account evidence conservatively')
    ap.add_argument('--recent', type=float, metavar='MIN')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--sessions', action='store_true', help='legacy option; session sweeps are disabled')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if args.sessions:
        log('Session sweeps are disabled. Authentication failures do not prove suspension.')
        return 0
    cfg = tp.load_config()
    proxies = tp.load_proxies()
    active = {a['username'] for a in tp.load_accounts()}
    targets = sorted(active) if args.all else [u for u in recent_posters(args.recent or 90) if u in active]
    if not targets:
        log('No matching accounts to check.')
        return 0

    def check(username):
        try:
            return username, _public_evidence(username, cfg, proxies)
        except Exception:
            return username, account_evidence.classify_public_response('', http_status=None, network_error=True)

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as executor:
        results = list(executor.map(check, targets))
    newly = 0
    for username, evidence in results:
        if args.dry_run:
            log('@%s: %s (%s)' % (username, evidence['status'], evidence['reason']))
        else:
            newly += int(account_evidence.bench_from_evidence(username, evidence))
    counts = Counter(e['status'] for _, e in results)
    log('Observed %s; new confirmed restrictions: %d. Readability does not verify publishing eligibility.' %
        (dict(counts), newly))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
