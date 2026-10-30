"""Automatic replacement of restricted accounts is disabled.

Account restrictions require recovery through X, not another reserve account.
The reserve files are preserved and are never opened by this entry point.
"""
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry', action='store_true', help='show why refill is disabled')
    parser.parse_args()
    print('Automatic account refill is disabled. ' + posting_safety.block_reason())
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
