"""Single kill switch for everything that publishes.

Set PUBLISHING_ENABLED = False to stop the scheduler, the dashboard's post-now
button and the account refill, cleanly and immediately.
"""

PUBLISHING_ENABLED = True
DISABLED_REASON = ('Publishing is disabled in posting_safety.py '
                   '(set PUBLISHING_ENABLED = True to resume).')


def enabled():
    return bool(PUBLISHING_ENABLED)


def blocked():
    return not enabled()


def block_reason():
    return '' if enabled() else DISABLED_REASON


def status():
    # dashboard.py reads status()['blocked'] and status()['reason']
    return {'blocked': blocked(), 'enabled': enabled(), 'reason': block_reason()}
