#!/usr/bin/env python3
"""V5 每日流程檢查。

  python3 v5/test_v5_live.py --verify-only   發布前檢查輸出（GitHub Actions 用）
  python3 v5/test_v5_live.py --selftest      在暫存複本中模擬「同步一個新交易日」：
      把滾動行情庫最後一天移除，用該日資料組出與官方相同格式的 TWSE/TPEx 回應，
      跑完整流程，確認重建的當日資料（含參考價、成交金額）與原檔逐筆相同、TOP 10 不變，
      並確認資料不一致、交易日缺漏時會中止且不改動任何檔案。
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(msg):
    print('V5 VERIFY FAIL:', msg, file=sys.stderr)
    sys.exit(1)


def verify(root=ROOT):
    rep = json.loads((root / 'v5/early_warning_latest.json').read_text(encoding='utf-8'))
    latest = json.loads((root / 'daily_screen/latest.json').read_text(encoding='utf-8'))
    if rep['AsOfDate'] != latest['AsOfDate']:
        fail(f"日期不一致 V5={rep['AsOfDate']} daily_screen={latest['AsOfDate']}")
    days = sorted(p.name[:10] for p in (root / 'v5_history/rolling').glob('*.csv.gz'))
    if not days or days[-1] != rep['AsOfDate']:
        fail('滾動行情庫最新日與 V5 日期不一致')
    if len(days) > 260:
        fail('滾動行情庫超過 260 天')
    if rep['Status'] == 'PUBLISHED':
        if rep['CoveragePct'] < rep['MinimumCoveragePct'] or rep['ValidatedStocks'] < 100:
            fail('合格率不足卻標示 PUBLISHED')
        if len(rep['Candidates']) > 10:
            fail('TOP 10 超過 10 檔')
        for c in rep['Candidates']:
            if c['Date'] != rep['AsOfDate'] or c['ResearchScore'] < 7 or c['VolumeRatio5'] < 1 or c['ExRightsWithin60']:
                fail(f"候選不符合規則 {c['Code']}")
    else:
        if rep['Candidates']:
            fail('未通過檢查卻輸出候選')
    html = (root / 'v5/index.html').read_text(encoding='utf-8')
    if rep['AsOfDate'] not in html:
        fail('網頁日期與資料不一致')
    if rep['Status'] != 'PUBLISHED' and '今日不發布' not in html:
        fail('未通過檢查但網頁沒有標示不發布')
    print('V5 VERIFY OK', rep['AsOfDate'], rep['Status'], f"{rep['ValidatedStocks']}/{rep['UniverseTotal']}", len(rep['Candidates']))


def fmt(x):
    v = float(x)
    return f'{v:,.2f}' if v != int(v) else f'{int(v):,}'


def payloads(rows, day):
    ymd = day.replace('-', '')
    tw, tp = [], []
    for r in rows:
        close = float(r['close'])
        ref = float(r['ref_price']) if r['ref_price'] else None
        chg = None if ref is None else round(close - ref, 4)
        val = fmt(r['value']) if r['value'] else '--'
        if r['market'] == 'TWSE':
            sign = '' if chg in (None, 0) else ('<p style= color:red>+</p>' if chg > 0 else '<p style= color:green>-</p>')
            tw.append([r['code'], r['name'], fmt(r['volume']), '1', val, r['open'], r['high'], r['low'], r['close'], sign,
                       ('0.00' if chg is None else f'{abs(chg):.2f}') if chg is not None else '--', '', '', '', '', '0'])
        else:
            tp.append([r['code'], r['name'], r['close'], ('---' if chg is None else f'{chg:+.2f}' if chg else '0.00'), r['open'],
                       r['high'], r['low'], '', fmt(r['volume']), val, '1'])
    twse = {'stat': 'OK', 'date': ymd, 'tables': [
        {'title': '價格指數', 'fields': ['指數'], 'data': [['x']]},
        {'title': f'{ymd} 每日收盤行情(全部(不含權證、牛熊證))', 'fields': [
            '證券代號', '證券名稱', '成交股數', '成交筆數', '成交金額', '開盤價', '最高價', '最低價', '收盤價', '漲跌(+/-)', '漲跌價差',
            '最後揭示買價', '最後揭示買量', '最後揭示賣價', '最後揭示賣量', '本益比'], 'data': tw}]}
    tpex = {'stat': 'ok', 'date': ymd, 'tables': [
        {'title': '上櫃股票行情', 'fields': ['代號', '名稱', '收盤', '漲跌', '開盤', '最高', '最低', '均價', '成交股數', '成交金額(元)', '成交筆數'],
         'data': tp}]}
    return json.dumps(twse, ensure_ascii=False).encode(), json.dumps(tpex, ensure_ascii=False).encode()


def snapshot(root):
    h = {}
    for p in sorted((root / 'v5').glob('*')) + sorted((root / 'v5_history').rglob('*')):
        if p.is_file() and '__pycache__' not in str(p):
            h[str(p.relative_to(root))] = hashlib.md5(p.read_bytes()).hexdigest()
    return h


def run_sim(tmp, tw, tp, history_patch=None):
    """在 tmp 複本中，以假的 fetch 執行 v5_live.main。"""
    code = f"""
import sys, importlib.util
sys.path.insert(0, {str(tmp / 'v5')!r})
spec = importlib.util.spec_from_file_location('v5_live', {str(tmp / 'v5/v5_live.py')!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
TW = open({str(tmp / 'tw.json')!r}, 'rb').read(); TP = open({str(tmp / 'tp.json')!r}, 'rb').read()
m.screen.fetch = lambda url: TW if 'twse' in url else TP
try:
    sys.exit(m.main([]))
except Exception as e:
    print('ABORT', type(e).__name__, e); sys.exit(2)
"""
    (tmp / 'tw.json').write_bytes(tw)
    (tmp / 'tp.json').write_bytes(tp)
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)


def selftest():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / 'repo'
        for d in ('v5', 'v5_history', 'daily_screen'):
            shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns('__pycache__'))
        roll = tmp / 'v5_history/rolling'
        last = sorted(roll.glob('*.csv.gz'))[-1]
        day = last.name[:10]
        with gzip.open(last, 'rt', encoding='utf-8') as fh:
            rows = list(csv.DictReader(fh))
        tw, tp = payloads(rows, day)
        before_top = [c['Code'] for c in json.loads((ROOT / 'v5/early_warning_latest.json').read_text(encoding='utf-8'))['Candidates']]

        # 1) 正常同步
        last.unlink()
        r = run_sim(tmp, tw, tp)
        print(r.stdout.strip().splitlines()[0] if r.stdout else r.stderr)
        if r.returncode != 0:
            fail('模擬同步失敗：' + r.stdout + r.stderr)
        with gzip.open(roll / f'{day}.csv.gz', 'rt', encoding='utf-8') as fh:
            got = {x['code']: x for x in csv.DictReader(fh)}
        if set(got) != {x['code'] for x in rows}:
            fail('重建的股票清單不同')
        for x in rows:
            y = got[x['code']]
            for k in ('date', 'market', 'name', 'open', 'high', 'low', 'close', 'volume'):
                if x[k] != y[k]:
                    fail(f"重建 {day} {x['code']} {k} 不同：{x[k]} vs {y[k]}")
            for k in ('ref_price', 'value'):
                if x[k] and abs(float(x[k]) - float(y[k])) > 1e-6:
                    fail(f"重建 {day} {x['code']} {k} 不同：{x[k]} vs {y[k]}")
        top = [c['Code'] for c in json.loads((tmp / 'v5/early_warning_latest.json').read_text(encoding='utf-8'))['Candidates']]
        if top != before_top:
            fail(f'TOP 10 改變 {top} vs {before_top}')
        verify(tmp)
        print('  同步重建當日資料：OHLCV 逐筆相同；TOP 10 不變')

        # 1b) 參考價、成交金額解析：用含這兩欄的日期（官方歷史檔）組回應再解析
        sys.path.insert(0, str(tmp / 'v5'))
        import importlib.util
        spec = importlib.util.spec_from_file_location('v5_live_t', tmp / 'v5/v5_live.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        for p in sorted(roll.glob('*.csv.gz'))[::-1]:
            with gzip.open(p, 'rt', encoding='utf-8') as fh:
                rr = list(csv.DictReader(fh))
            if sum(1 for x in rr if x['ref_price']) > 0.9 * len(rr):
                break
        d2 = p.name[:10]
        t2, p2 = payloads(rr, d2)
        ex = {**m.extra_fields(t2, 'TWSE'), **m.extra_fields(p2, 'TPEx')}
        n = 0
        for x in rr:
            if x['ref_price']:
                ref, val = ex[x['code']]
                if abs(ref - float(x['ref_price'])) > 1e-6 or (x['value'] and abs(val - float(x['value'])) > 1e-6):
                    fail(f"{d2} {x['code']} 參考價／成交金額解析錯誤 {ref},{val} vs {x['ref_price']},{x['value']}")
                n += 1
        print(f'  參考價與成交金額解析（{d2}，{n} 檔）：全部相同')

        # 2) 官方資料與 daily_screen 不一致 → 中止、不改檔
        (roll / f'{day}.csv.gz').unlink()
        snap = snapshot(tmp)
        bad = json.loads(tw)
        bad['tables'][1]['data'][0][8] = str(float(bad['tables'][1]['data'][0][8].replace(',', '')) + 1)
        bad['tables'][1]['data'][0][6] = str(float(bad['tables'][1]['data'][0][6].replace(',', '')) + 1)
        r = run_sim(tmp, json.dumps(bad, ensure_ascii=False).encode(), tp)
        if r.returncode == 0 or 'HISTORY_MISMATCH' not in r.stdout or snapshot(tmp) != snap:
            fail('資料不一致時沒有正確中止：' + r.stdout)
        print('  官方資料與 daily_screen 不一致：中止，檔案未改動')

        # 3) 行情庫缺太多天（daily_screen 30 日窗口接不上）→ 中止、不改檔
        for p in sorted(roll.glob('*.csv.gz'))[-40:]:
            p.unlink()
        snap = snapshot(tmp)
        r = run_sim(tmp, tw, tp)
        if r.returncode == 0 or 'ROLLING_GAP' not in r.stdout or snapshot(tmp) != snap:
            fail('交易日缺漏時沒有正確中止：' + r.stdout)
        print('  行情庫交易日接不上：中止，檔案未改動')
    print('V5 SELFTEST OK')


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest()
    else:
        verify()
