"""Standalone, opt-in X API publisher. The fleet scheduler does not use this.

Running without --publish only validates local inputs and prints an offline plan.
No retries, cookies, proxy rotation, account replacement, or account recovery.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote

API = 'https://api.x.com'
DATA_DIR = Path(__file__).resolve().parent / 'data'
CHUNK_BYTES = 4 * 1024 * 1024
MAX_VIDEO_BYTES = CHUNK_BYTES * 1000
USERNAME = re.compile(r'[A-Za-z0-9_]{1,15}\Z')
SECRET_KEY = re.compile(r'authorization|cookie|token|secret|password|api[_-]?key', re.I)


class PublishError(RuntimeError):
    def __init__(self, stage, category, http=None, log_failed=False):
        self.stage, self.category, self.http = stage, category, http
        self.log_failed = log_failed
        super().__init__(f'{stage}: {category}' + (f' (HTTP {http})' if http else ''))


def _inputs(text, video):
    if not isinstance(text, str) or (not text.strip() and video is None):
        raise ValueError('Provide text or an MP4 video.')
    if video is None:
        return None, 0
    path = Path(video)
    if path.suffix.lower() != '.mp4' or not path.is_file():
        raise ValueError('Video must be a readable local MP4 file.')
    size = path.stat().st_size
    if not 0 < size <= MAX_VIDEO_BYTES:
        raise ValueError('Video must contain 1 to 4,194,304,000 bytes.')
    return path, size


def preview(text='', video=None, adult=True):
    """No environment access, network, file writes, or credential/content output."""
    _, size = _inputs(text, video)
    return {'mode': 'offline_preview', 'text_characters': len(text),
            'video_bytes': size, 'adult_warning_required': bool(video and adult),
            'will_publish': False}


def _redact(value, secret='', depth=0):
    if depth > 4:
        return '[truncated]'
    if isinstance(value, dict):
        return {_redact(str(k), secret, depth + 1)[:80]: ('[redacted]' if SECRET_KEY.search(str(k)) else
                             _redact(v, secret, depth + 1))
                for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return [_redact(v, secret, depth + 1) for v in value[:5]]
    if isinstance(value, str):
        if secret:
            for variant in sorted({secret, quote(secret, safe=''), unquote(secret)},
                                  key=len, reverse=True):
                if variant:
                    value = value.replace(variant, '[redacted]')
        value = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [redacted]', value)
        value = re.sub(r'(?i)Cookie\s*:[^\r\n]+', 'Cookie: [redacted]', value)
        value = re.sub(r'(?i)((?:auth_token|ct0|access_token|refresh_token|'
                       r'client_secret|password|authorization)\s*[=:]\s*)'
                       r'[^\s,;&]+', r'\1[redacted]', value)
        return value[:1000]
    return value if value is None or isinstance(value, (bool, int, float)) else '[omitted]'


def _codes(body, depth=0):
    found = set()
    if isinstance(body, dict) and depth < 5:
        code = body.get('code')
        if isinstance(code, int) or (isinstance(code, str) and len(code) <= 12 and code.isdigit()):
            found.add(int(code))
        for error in body.get('errors', [])[:12] if isinstance(body.get('errors'), list) else []:
            found.update(_codes(error, depth + 1))
    return found


def _category(http, body):
    codes = _codes(body)
    for code, name in ((64, 'account_suspended'), (326, 'account_locked'),
                       (226, 'automation_flagged'), (344, 'unknown_code_344')):
        if code in codes:
            return name
    return {401: 'authentication_failed', 403: 'forbidden',
            429: 'rate_limited'}.get(http, 'api_error')


class OfficialPublisher:
    """Inject a requests-compatible Session with retries disabled.

    Construction performs no I/O. Calling publish is an explicit live operation.
    """
    def __init__(self, session, token, expected_username, data_dir=DATA_DIR,
                 clock=time.monotonic, sleep=time.sleep, processing_timeout=180,
                 expected_user_id=None):
        self.session, self.token = session, token or ''
        self.username = (expected_username or '').removeprefix('@')
        self.data_dir = Path(data_dir)
        self.clock, self.sleep = clock, sleep
        self.processing_timeout = min(max(float(processing_timeout), 1), 600)
        self.expected_user_id = expected_user_id
        self.user_id = None
        self._hold = None
        self._video_hash = None

    def _fail(self, stage, category, http=None, body=None):
        body = body if isinstance(body, dict) else {}
        record = {'ts': datetime.now(timezone.utc).isoformat(), 'stage': stage,
                  'category': category, 'http': http, 'codes': sorted(_codes(body)),
                  'account': self.username if USERNAME.fullmatch(self.username) else None,
                  'user_id': self.user_id}
        record.update({key: _redact(body[key], self.token)
                       for key in ('type', 'title', 'detail', 'errors', 'message') if key in body})
        log_failed = False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / 'publishing_incidents.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(record, ensure_ascii=True) + '\n')
        except OSError:
            log_failed = True
        raise PublishError(stage, category, http, log_failed)

    def _bench_check(self):
        try:
            with (self.data_dir / 'benched.json').open(encoding='utf-8') as file:
                bench = json.load(file)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self._fail('preflight', 'bench_unreadable')
        if not isinstance(bench, dict):
            self._fail('preflight', 'bench_unreadable')
        if self.username.casefold() in {str(name).removeprefix('@').casefold() for name in bench}:
            self._fail('preflight', 'account_locally_benched')

    def _acquire_hold(self):
        """Exclusive file prevents concurrent runs and survives an interrupted run."""
        path = self.data_dir / 'official_publisher_holds' / (self.username.casefold() + '.json')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as file:
                json.dump({'account': self.username, 'started_at': datetime.now(timezone.utc).isoformat(),
                           'state': 'in_progress_or_interrupted'}, file)
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError:
            self._fail('preflight', 'account_review_hold')
        except OSError:
            self._fail('preflight', 'account_hold_unavailable')
        self._hold = path

    def _request(self, stage, method, path, expected=200, **kwargs):
        try:
            response = self.session.request(method, API + path,
                headers={'Authorization': 'Bearer ' + self.token},
                timeout=(10, 30), allow_redirects=False, **kwargs)
        except Exception:
            # A lost create response may mean the Post exists. Never retry it.
            self._fail(stage, 'create_outcome_unknown' if stage == 'create' else 'transport_error')
        try:
            body = response.json()
        except (ValueError, TypeError):
            self._fail(stage, 'create_outcome_unknown' if stage == 'create' else
                       'invalid_response', response.status_code)
        if not isinstance(body, dict):
            self._fail(stage, 'create_outcome_unknown' if stage == 'create' else
                       'invalid_response', response.status_code)
        if response.status_code != expected or body.get('errors'):
            category = _category(response.status_code, body)
            if (stage == 'create' and category == 'api_error' and
                    not 400 <= response.status_code < 500):
                category = 'create_outcome_unknown'
            self._fail(stage, category, response.status_code, body)
        return body

    def _media(self, path, size, adult):
        body = self._request('initialize', 'POST', '/2/media/upload/initialize', json={
            'media_type': 'video/mp4', 'total_bytes': size, 'media_category': 'tweet_video'})
        media_id = body.get('data', {}).get('id') if isinstance(body.get('data'), dict) else None
        if not isinstance(media_id, str) or not re.fullmatch(r'[0-9]{1,19}', media_id):
            self._fail('initialize', 'invalid_media_id')
        try:
            with path.open('rb') as file:
                sent, index = 0, 0
                digest = hashlib.sha256()
                while chunk := file.read(CHUNK_BYTES):
                    if sent + len(chunk) > size or index >= 1000:
                        self._fail('append', 'video_changed')
                    self._request('append', 'POST', f'/2/media/upload/{media_id}/append',
                        data={'segment_index': str(index)},
                        files={'media': ('chunk.mp4', chunk, 'application/octet-stream')})
                    digest.update(chunk)
                    sent, index = sent + len(chunk), index + 1
                if sent != size:
                    self._fail('append', 'video_changed')
                self._video_hash = digest.hexdigest()
        except OSError:
            self._fail('append', 'video_read_failed')
        body = self._request('finalize', 'POST', f'/2/media/upload/{media_id}/finalize')
        deadline = self.clock() + self.processing_timeout
        awaiting_processing = False
        while True:
            data = body.get('data')
            if not isinstance(data, dict):
                self._fail('processing', 'invalid_response')
            info = data.get('processing_info')
            if info is None and awaiting_processing:
                self._fail('processing', 'processing_status_missing')
            if info is None or (isinstance(info, dict) and info.get('state') == 'succeeded'):
                break
            if not isinstance(info, dict) or info.get('state') not in ('pending', 'in_progress'):
                self._fail('processing', 'media_processing_failed', body={'errors': [info]})
            delay = info.get('check_after_secs', 1)
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 600:
                self._fail('processing', 'invalid_processing_delay')
            delay = max(delay, 1)
            if self.clock() + delay >= deadline:
                self._fail('processing', 'processing_deadline_exceeded')
            self.sleep(delay)
            if self.clock() >= deadline:
                self._fail('processing', 'processing_deadline_exceeded')
            awaiting_processing = True
            body = self._request('status', 'GET', '/2/media/upload',
                                 params={'media_id': media_id, 'command': 'STATUS'})
            if self.clock() >= deadline:
                self._fail('processing', 'processing_deadline_exceeded')
        if adult:
            body = self._request('metadata', 'POST', '/2/media/metadata', json={
                'id': media_id, 'metadata': {'sensitive_media_warning': {'adult_content': True}}})
            data = body.get('data', {})
            metadata = data.get('associated_metadata', {}) if isinstance(data, dict) else {}
            warning = metadata.get('sensitive_media_warning', {}) if isinstance(metadata, dict) else {}
            if (not isinstance(data, dict) or data.get('id') != media_id or
                    not isinstance(warning, dict) or warning.get('adult_content') is not True):
                self._fail('metadata', 'adult_warning_unconfirmed')
        return media_id

    def _receipt(self, tweet_id, text):
        record = {'ts': datetime.now(timezone.utc).isoformat(), 'account': self.username,
                  'user_id': self.user_id, 'tweet_id': tweet_id,
                  'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                  'video_sha256': self._video_hash}
        try:
            with (self.data_dir / 'publishing_receipts.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps(record) + '\n')
                file.flush()
                os.fsync(file.fileno())
        except OSError:
            self._fail('receipt', 'accepted_post_receipt_unavailable',
                       body={'detail': 'Accepted Post ID: ' + tweet_id})

    def publish(self, text='', video=None, adult=True):
        self.user_id, self._video_hash = None, None
        if (not self.token or any(c.isspace() for c in self.token) or
                not USERNAME.fullmatch(self.username) or (self.expected_user_id is not None and
                not re.fullmatch(r'[0-9]{1,19}', str(self.expected_user_id)))):
            self._fail('configuration', 'missing_or_invalid_user_configuration')
        self._bench_check()
        self._acquire_hold()
        retain_hold = True  # An unexpected exception or interruption must fail closed.
        try:
            result = self._publish(text, video, adult)
            retain_hold = False
            return result
        except PublishError as error:
            retain_hold = error.category in {'create_outcome_unknown', 'account_suspended',
                'account_locked', 'automation_flagged', 'unknown_code_344',
                'accepted_post_receipt_unavailable'}
            raise
        finally:
            if not retain_hold:
                try:
                    self._hold.unlink()
                except OSError:
                    # A remaining hold blocks the next invocation; never silently bypass it.
                    self._fail('cleanup', 'account_hold_cleanup_failed')
                self._hold = None

    def _publish(self, text, video, adult):
        try:
            path, size = _inputs(text, video)
        except (OSError, ValueError):
            self._fail('preflight', 'invalid_input')
        me = self._request('identity', 'GET', '/2/users/me').get('data')
        if (not isinstance(me, dict) or not isinstance(me.get('id'), str) or
                not re.fullmatch(r'[0-9]{1,19}', me['id']) or
                not isinstance(me.get('username'), str) or
                me['username'].casefold() != self.username.casefold() or
                (self.expected_user_id is not None and me['id'] != str(self.expected_user_id))):
            self._fail('identity', 'username_mismatch')
        self.user_id = me['id']
        payload = {'text': text}
        if path is not None:
            payload['media'] = {'media_ids': [self._media(path, size, adult)]}
        self._bench_check()
        body = self._request('create', 'POST', '/2/tweets', expected=201, json=payload)
        data = body.get('data')
        if (not isinstance(data, dict) or not isinstance(data.get('id'), str) or
                not re.fullmatch(r'[0-9]{1,19}', data['id'])):
            self._fail('create', 'create_outcome_unknown', http=201)
        self._receipt(data['id'], text)
        return {'id': data['id'], 'url': 'https://x.com/i/status/' + data['id']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text', default='')
    parser.add_argument('--video', type=Path)
    parser.add_argument('--non-sensitive', action='store_true',
                        help='Only for media actually containing no adult content.')
    parser.add_argument('--publish', action='store_true', help='Send one reviewed Post using the official API.')
    args = parser.parse_args(argv)
    if not args.publish:
        try:
            print(json.dumps(preview(args.text, args.video, not args.non_sensitive)))
            return 0
        except (OSError, ValueError):
            print('Offline preview: invalid text or local MP4 input.')
            return 2
    try:
        # Lazy dependency: offline previews and fake-session tests need no requests.
        import requests
        session = requests.Session()
        session.trust_env = False
        session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
        with session:
            publisher = OfficialPublisher(session, os.environ.get('X_USER_ACCESS_TOKEN'),
                                          os.environ.get('X_EXPECTED_USERNAME'),
                                          expected_user_id=os.environ.get('X_EXPECTED_USER_ID'))
            print(json.dumps(publisher.publish(args.text, args.video, not args.non_sensitive)))
        return 0
    except ImportError:
        print('Live publishing requires requests. See OFFICIAL_API_SETUP.md.')
        return 2
    except PublishError as error:
        print(str(error))
        if error.log_failed:
            print('The local incident log could not be written.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
