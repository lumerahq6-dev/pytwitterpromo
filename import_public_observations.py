"""Import reviewed, redacted browser observations without making requests."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys

import account_evidence
import store


def main(path):
    observations = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(observations, dict) or len(observations) > 50:
        raise ValueError('Expected a bounded set of reviewed public observations')
    for username, observation in observations.items():
        if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', username):
            raise ValueError('Invalid account identifier')
        if (not isinstance(observation, dict) or observation.get('source') != 'public_browser'
                or not isinstance(observation.get('observed_at'), int)
                or observation.get('status') not in ('suspended', 'readable')):
            raise ValueError('Invalid reviewed public observation')
        if observation['status'] == 'suspended' and not account_evidence.summarize_account(None, observation)['confirmed']:
            raise ValueError('Suspension requires a recorded visible platform notice')
    latest = account_evidence.read_evidence()
    selected = {u: e for u, e in observations.items()
                if (latest.get(u, {}).get('observed_at') or 0) < e['observed_at']}
    if not selected:
        print('All observations already recorded or superseded.')
        return
    target = Path(store.DATA_DIR).resolve()
    backup = target.parent / '.public-observation-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True)
    for filename in (store.BENCH_FILE, account_evidence.EVIDENCE_FILE):
        source = target / filename
        if source.is_file():
            shutil.copy2(source, backup / filename)
    newly = sum(int(account_evidence.bench_from_evidence(u, e)) for u, e in selected.items())
    print(f'Recorded {len(selected)} public observations; {newly} newly restricted accounts. Backup: {backup}')


if __name__ == '__main__':
    main(sys.argv[1])
