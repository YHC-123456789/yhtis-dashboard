#!/usr/bin/env python3
"""YHTIS schedule delay-safe market calendar guard. Does not alter trading models.

The official fetcher in screen.py independently verifies the date of both exchanges.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import sys

CLOSED_2026 = {
    '2026-01-01', '2026-02-12', '2026-02-13',
    '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
    '2026-02-27', '2026-04-03', '2026-04-06', '2026-05-01',
    '2026-06-19', '2026-09-25', '2026-09-28', '2026-10-09',
    '2026-10-23', '2026-10-26', '2026-12-25',
}

def decide(now):
    """Use previous Friday for bounded post-midnight delayed Friday schedules.

    This guard is an optimization only, NOT source validation; screen.py verifies
    TWSE and TPEx independently and fails closed on unexpected/missing data.
    """
    now = now.astimezone(ZoneInfo('Asia/Taipei'))
    today = now.date()
    delayed_friday = today.weekday() == 5 and now.hour < 12
    target = today - timedelta(days=1) if delayed_friday else today
    date = target.isoformat()
    closed = target.weekday() >= 5 or date in CLOSED_2026
    reason = ('DELAYED_FRIDAY_RECOVERY' if delayed_friday and not closed else
              'CONFIRMED_CLOSED_DAY' if date in CLOSED_2026 else
              'WEEKEND' if target.weekday() >= 5 else 'RUN_STRICT_MARKET_FETCH')
    return date, reason, closed

def main():
    now = datetime.now(ZoneInfo('Asia/Taipei'))
    day, reason, closed = decide(now)
    print(f'YHTIS_MARKET_DAY={day} STATUS={reason} SKIP={str(closed).lower()}')
    output = os.getenv('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as f:
            f.write(f'skip={str(closed).lower()}\n')
            f.write(f'target_date={day}\n')
    return 0

if __name__ == '__main__':
    sys.exit(main())
