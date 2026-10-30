"""Conservative public account observations; no authenticated account operations.

Only explicit visible suspension notices can add restrictions. Script strings,
generic errors and failed requests cannot. Readability never proves permission
to publish. Evidence is short and redacted; restrictions are never auto-cleared.
"""
from html.parser import HTMLParser
import re
import threading
import time
import urllib.error
import urllib.request

import store

CLASSIFIER_VERSION = 1
MAX_RESPONSE_BYTES = 1024 * 1024
EVIDENCE_FILE = 'account_evidence.json'
_LOCK = threading.RLock()
_SUSPENSION = re.compile(r'^(?:this )?account (?:is |has been )?suspended[.!]?$', re.I)
_RULES_NOTICE = re.compile(r'\b(?:x|twitter) suspends accounts (?:that|which) violate (?:the )?(?:x|twitter) rules\b', re.I)
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
_IGNORE = {'script', 'style', 'template', 'noscript', 'svg', 'head', 'title'}
_STRUCTURAL = {'html', 'body', 'main', 'section', 'div', 'h1', 'h2', 'h3', 'form'}


def _clean(value, limit=300):
    text = ' '.join(str(value or '').split())
    text = re.sub(r'https?://\S+', '[url]', text, flags=re.I)
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[email]', text)
    text = re.sub(r'(?i)\b(auth_token|ct0|password|access_token|authorization|cookie|bearer)\b\s*[:=]?\s*["\']?[^\s,;"\']+', r'\1=[redacted]', text)
    text = re.sub(r'\b[A-Za-z0-9_-]{32,}\b', '[redacted]', text)
    return text[:limit]


class _VisibleHTML(HTMLParser):
    """Collect visible text, ignoring attributes and non-rendered payloads."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title_parts = []
        self.visible = []
        self.notice_parts = []
        self.headings = []
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        style = re.sub(r'\s+', '', attrs.get('style', '').lower())
        classes = set(attrs.get('class', '').lower().split())
        hidden = (any(x['hidden'] for x in self.stack) or tag in _IGNORE or
                  'hidden' in attrs or 'inert' in attrs or attrs.get('aria-hidden', '').lower() == 'true' or
                  'display:none' in style or 'visibility:hidden' in style or
                  bool(classes & {'hidden', 'sr-only', 'visually-hidden'}))
        entry = {'tag': tag, 'hidden': hidden, 'text': [],
                 'heading': tag in ('h1', 'h2', 'h3') or attrs.get('role') == 'heading',
                 'quoted': tag in ('article', 'blockquote') or any(x['quoted'] for x in self.stack)}
        if tag not in _VOID:
            self.stack.append(entry)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i]['tag'] == tag.lower():
                for entry in self.stack[i:]:
                    text = ' '.join(' '.join(entry['text']).split())
                    if text and not entry['hidden'] and not entry['quoted']:
                        self.blocks.append(text)
                        if entry['heading']:
                            self.headings.append(text)
                del self.stack[i:]
                break

    def handle_data(self, data):
        if any(x['tag'] == 'title' for x in self.stack):
            self.title_parts.append(data)
        if any(x['hidden'] for x in self.stack):
            return
        text = ' '.join(data.split())
        if not text:
            return
        self.visible.append(text)
        if not any(x['quoted'] for x in self.stack):
            self.notice_parts.append(text)
            for entry in self.stack:
                entry['text'].append(text)


def classify_public_response(html, *, username='', http_status=200, truncated=False,
                             source='public_profile', observed_at=None, network_error=False):
    """Classify supplied HTML only; no network requests or persistence."""
    evidence = {'status': 'unknown', 'source': source,
        'observed_at': int(time.time() if observed_at is None else observed_at),
        'http_status': http_status, 'page_title': '', 'reason': 'no_decisive_visible_notice',
        'sample': '', 'classifier_version': CLASSIFIER_VERSION}
    def verdict(status, reason):
        evidence.update(status=status, reason=reason)
        return evidence
    if network_error:
        return verdict('unavailable', 'network_error')
    if http_status == 429:
        return verdict('rate_limited', 'http_429')
    if http_status is not None and http_status >= 500:
        return verdict('unavailable', 'server_error')
    if http_status in (404, 410):
        return verdict('not_found', 'http_not_found')
    if http_status == 401:
        return verdict('login_required', 'http_authentication_required')
    if http_status == 403:
        return verdict('unavailable', 'http_access_denied')
    if truncated or http_status == 206:
        return verdict('unavailable', 'truncated_response')
    if http_status != 200:
        return verdict('unavailable', 'unexpected_http_status')
    if not isinstance(html, str) or not html.strip():
        return verdict('unavailable', 'empty_response')
    parser = _VisibleHTML()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return verdict('unavailable', 'invalid_html')
    evidence['page_title'] = _clean(' '.join(parser.title_parts), 160)
    visible = ' '.join(parser.visible)
    notice = ' '.join(parser.notice_parts)
    evidence['sample'] = _clean(visible)
    if any(x['tag'] in _STRUCTURAL for x in parser.stack):
        return verdict('unavailable', 'incomplete_document')
    if not visible:
        return verdict('unknown', 'no_visible_content')
    headings = [s for s in parser.headings if _SUSPENSION.fullmatch(s)]
    standalone = [s for s in parser.blocks + parser.notice_parts if _SUSPENSION.fullmatch(s)]
    profile_title = bool(username and re.search(r'@' + re.escape(username.lstrip('@')) + r'\b', evidence['page_title'], re.I))
    if standalone and (_RULES_NOTICE.search(notice) or (headings and not profile_title)):
        index = notice.lower().find(standalone[0].lower())
        evidence['sample'] = _clean(notice[max(0, index-30):])
        return verdict('suspended', 'explicit_visible_suspension_notice')
    low = notice.lower().replace('\u2019', "'")
    if re.search(r"\b(?:this |the )?account (?:doesn't|does not) exist\b", low):
        return verdict('not_found', 'visible_account_not_found')
    if re.search(r'age[- ]restricted|verify your age|confirm your age|must be (?:at least )?18|age verification|you must be over 18', low):
        return verdict('login_required', 'age_gate')
    if re.search(r'(?:log|sign) in to (?:continue|view|see|access)|(?:log|sign) in to (?:x|twitter)|join (?:x|twitter) today', low):
        return verdict('login_required', 'login_gate')
    if profile_title and re.search(r'@' + re.escape(username.lstrip('@')) + r'\b', notice, re.I):
        return verdict('readable', 'public_profile_visible_not_publish_eligibility')
    if re.search(r'\b(?:x|twitter)\s*/\s*error\b', evidence['page_title'], re.I):
        return verdict('unknown', 'generic_error_page')
    return evidence


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_public_evidence(username, *, proxy=None, timeout=35, max_bytes=MAX_RESPONSE_BYTES):
    """One bounded unauthenticated public request, without retries or redirects."""
    username = str(username).lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', username):
        result = classify_public_response('', http_status=None, network_error=True)
        result['reason'] = 'invalid_username'
        return result
    limit = min(MAX_RESPONSE_BYTES, max(1, int(max_bytes)))
    handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {})
    opener = urllib.request.build_opener(handler, _NoRedirect())
    request = urllib.request.Request('https://x.com/' + username, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; PublicAccountStatus/1.0)', 'Accept': 'text/html'})
    response = None
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        status = response.getcode()
        payload = response.read(limit + 1)
        declared = response.headers.get('Content-Length')
        truncated = len(payload) > limit
        if declared and str(declared).isdigit() and int(declared) > len(payload):
            truncated = True
        return classify_public_response(payload[:limit].decode('utf-8', 'replace'),
            username=username, http_status=status, truncated=truncated)
    except Exception:
        # Exception text can expose proxy passwords/headers; never retain it.
        return classify_public_response('', http_status=None, network_error=True)
    finally:
        if response is not None:
            response.close()


def _redacted(evidence):
    allowed = ('status', 'source', 'observed_at', 'http_status', 'reason', 'classifier_version')
    safe = {key: evidence.get(key) for key in allowed}
    safe['page_title'] = _clean(evidence.get('page_title'), 160)
    safe['sample'] = _clean(evidence.get('sample'), 300)
    return safe


def persist_evidence(username, evidence):
    """Save the latest redacted observation without changing restrictions."""
    with _LOCK:
        path = store._p(EVIDENCE_FILE)
        records = read_evidence()
        records[username] = _redacted(evidence)
        store.write_json(path, records)


def read_evidence():
    """Latest observations by username; absence means not checked."""
    records = store.read_json(store._p(EVIDENCE_FILE), {})
    return records if isinstance(records, dict) else {}


def _confirmed_notice(evidence):
    if not (isinstance(evidence, dict) and evidence.get('classifier_version') == CLASSIFIER_VERSION and
            evidence.get('status') == 'suspended' and evidence.get('sample')):
        return False
    if evidence.get('source') == 'public_browser':
        # Browser observations may have no HTTP status. Require the recorded
        # visible platform notice, not a generic operator-provided status label.
        return bool(evidence.get('http_status') in (None, 200) and
                    re.search(r'\baccount suspended\b', evidence['sample'], re.I) and
                    _RULES_NOTICE.search(evidence['sample']))
    return (evidence.get('source') == 'public_profile' and
            evidence.get('reason') == 'explicit_visible_suspension_notice' and
            evidence.get('http_status') == 200)


def bench_from_evidence(username, evidence):
    """Add/confirm a restriction only for explicit visible suspension evidence."""
    persist_evidence(username, evidence)
    if not _confirmed_notice(evidence):
        return False
    with _LOCK:
        newly = store.bench_account(username, 'Suspension notice confirmed', source='autobench')
        records = store.read_benched()
        records[username]['evidence'] = _redacted(evidence)
        store.write_json(store._p(store.BENCH_FILE), records)
        return newly


def summarize_bench(record):
    """Never upgrade a legacy heuristic flag into a confirmed suspension."""
    if not record:
        return {'state': 'no_restriction_record', 'label': 'No recorded restriction', 'confirmed': False,
                'reason': 'No restriction record; current account status is unverified.'}
    if record.get('reason') == 'AccountSuspended':
        return {'state': 'suspended', 'label': 'Confirmed suspension', 'confirmed': True,
                'reason': 'X returned the typed AccountSuspended error. The underlying cause is unknown.'}
    if _confirmed_notice(record.get('evidence')):
        return {'state': 'suspended', 'label': 'Suspension notice confirmed', 'confirmed': True,
                'reason': 'An explicit visible suspension notice was recorded on the public profile.'}
    return {'state': 'unverified_restriction', 'label': 'Review required', 'confirmed': False,
            'reason': 'Legacy restriction lacks definitive suspension evidence; manual review is required.'}


def summarize_account(record, evidence=None):
    """Combine latest observations with restriction history without reviving it."""
    restriction = summarize_bench(record)
    if restriction['confirmed']:
        return restriction
    if _confirmed_notice(evidence):
        return summarize_bench({'evidence': evidence})
    if record:
        return restriction
    if not evidence:
        return {'state': 'not_checked', 'label': 'Not checked', 'confirmed': False,
                'reason': 'No current account observation is recorded.'}
    labels = {'readable': 'Publicly readable', 'unknown': 'Status unknown',
              'not_found': 'Profile not found', 'login_required': 'Sign-in or age check required',
              'rate_limited': 'Check rate limited', 'unavailable': 'Check unavailable'}
    status = evidence.get('status', 'unknown')
    return {'state': status if status in labels else 'unknown',
            'label': labels.get(status, 'Status unknown'), 'confirmed': False,
            'reason': 'Public readability does not establish publishing eligibility.' if status == 'readable'
                      else _clean(evidence.get('reason') or 'No definitive account verdict.')}
