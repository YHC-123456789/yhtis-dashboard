#!/usr/bin/env python3
"""YHTIS publish gate. Exit 0 on known market closure; 1 to run original pipeline.
Only published/confirmed closures may skip. Unrecognized days fall through to
regular TWSE + TPEx strict date and contents validation (fail-closed).
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import sys

# Official TWSE calendar (2026); extraordinary 2026-10-23 typhoon closure
# verified in TWSE/TPEx announcements, 2026-10-22.
CLOSED_2026 = {
    '2026-01-01', '2026-02-12', '2026-02-13',
    '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
    '2026-02-27', '2026-04-03', '2026-04-06', '2026-05-01',
    '2026-06-19', '2026-09-25', '2026-09-28', '2026-10-09',
    '2026-10-23', '2026-10-26', '2026-12-25',
}

def main():
    today = datetime.now(ZoneInfo('Asia/Taipei')).date()
    date = today.isoformat()
    # Restrict this switch to already published 2026 exceptions/regular holidays.
    is_closed = today.weekday() >= 5 or date in CLOSED_2026
    reason = ('CONFIRMED_CLOSED_DAY' if date in CLOSED_2026 else
              'WEEKEND' if today.weekday() >= 5 else 'RUN_STRICT_MARKET_FETCH')
    print(f'YHTIS_MARKET_DAY={date} STATUS={reason} SKIP={str(is_closed).lower()}')
    output = os.getenv('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as f:
            f.write(f'skip={str(is_closed).lower()}\n')
    return 0

if __name__ == '__main__':
    sys.exit(main())
