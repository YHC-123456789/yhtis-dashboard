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
import importlib.util
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
            if c['Date'] != rep['AsOfDate'] or c['ResearchScore'] < 7 or c['VolumeRatio5'] < 1:
                fail(f"候選不符合規則 {c['Code']}")
    else:
        if rep['Candidates']:
            fail('未通過檢查卻輸出候選')
    dt = json.loads((root / 'v5/daytrade_watch.json').read_text(encoding='utf-8'))
    if dt['AsOfDate'] != rep['AsOfDate'] or dt['Count'] != len(dt['Stocks']):
        fail('當沖觀察名單日期或筆數不一致')
    sc = json.loads((root / 'v5/scores_latest.json').read_text(encoding='utf-8'))
    if sc['AsOfDate'] != rep['AsOfDate'] or sc['Status'] != rep['Status']:
        fail('全市場分數檔與 V5 不一致')
    if rep['Status'] == 'PUBLISHED':
        top = [r[0] for r in sc['Rows'] if r[6]][:10]
        if top != [c['Code'] for c in rep['Candidates']] or len(sc['Rows']) != rep['ValidatedStocks']:
            fail('全市場分數排序與 TOP 10 不一致')
    idx = json.loads((root / 'v5_history/rolling/index.json').read_text(encoding='utf-8'))
    if idx['Days'] != days or idx['Last'] != rep['AsOfDate']:
        fail('行情庫索引與實際檔案不一致')
    eod = json.loads((root / 'output/official_market_eod_latest.json').read_text(encoding='utf-8'))
    if eod['Date'] > rep['AsOfDate'] or eod['Status'] != 'OFFICIAL_DATED_SNAPSHOT_VALIDATED' or len(eod['Stocks']) < 1700:
        fail('官方收盤行情檔異常')
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


def read_gz(p):
    with gzip.open(p, 'rt', encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def build_fake_site(pristine, fake, days):
    """以原始行情庫為官方網站的內容：每個交易日一組 TWSE/TPEx 回應；缺參考價的日期以前一日收盤補上（視為無除權息）。"""
    fake.mkdir(parents=True, exist_ok=True)
    allp = sorted(pristine.glob('*.csv.gz'))
    names = [p.name[:10] for p in allp]
    for d in days:
        i = names.index(d)
        rows = read_gz(allp[i])
        if sum(1 for x in rows if x['ref_price']) < 0.9 * len(rows) and i > 0:
            prev = {x['code']: x['close'] for x in read_gz(allp[i - 1])}
            for x in rows:
                if not x['ref_price'] and x['code'] in prev:
                    x['ref_price'] = prev[x['code']]
        tw, tp = payloads(rows, d)
        (fake / f'{d}_tw.json').write_bytes(tw)
        (fake / f'{d}_tp.json').write_bytes(tp)


def run_sim(tmp, fake, override=None):
    """在 tmp 複本中，以假的官方網站（依網址日期回應）執行 v5_live.main。"""
    code = f"""
import sys, re, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('v5_live', {str(tmp / 'v5/v5_live.py')!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.FETCH_PAUSE = 0
FAKE = Path({str(fake)!r}); OVR = {override!r}
def fetch(url):
    if 'twse' in url:
        x = re.search(r'date=(\\d{{8}})', url).group(1); d = f'{{x[:4]}}-{{x[4:6]}}-{{x[6:]}}'; k = 'tw'
    else:
        x = re.search(r'date=(\\d{{4}})%2F(\\d{{2}})%2F(\\d{{2}})', url)
        if not x: raise m.screen.SourceError('legacy url not served')
        d = '-'.join(x.groups()); k = 'tp'
    if OVR and OVR[0] == d and OVR[1] == k: return Path(OVR[2]).read_bytes()
    p = FAKE / f'{{d}}_{{k}}.json'
    if not p.exists(): return '{{"stat":"很抱歉，沒有符合條件的資料!"}}'.encode()
    return p.read_bytes()
m.screen.fetch = fetch
try:
    sys.exit(m.main([]))
except Exception as e:
    print('ABORT', type(e).__name__, e); sys.exit(2)
"""
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)


def same_ohlcv(a_rows, b_rows, label):
    a = {x['code']: x for x in a_rows}
    b = {x['code']: x for x in b_rows}
    if set(a) != set(b):
        fail(f'{label} 股票清單不同')
    for c, x in a.items():
        for k in ('date', 'market', 'name', 'open', 'high', 'low', 'close', 'volume'):
            if x[k] != b[c][k]:
                fail(f'{label} {c} {k} 不同：{x[k]} vs {b[c][k]}')
        if x['ref_price'] and b[c]['ref_price'] and abs(float(x['ref_price']) - float(b[c]['ref_price'])) > 1e-6:
            fail(f'{label} {c} ref_price 不同')


def fresh(td, name):
    tmp = Path(td) / name
    for d in ('v5', 'v5_history', 'daily_screen'):
        shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp / 'output').mkdir()
    shutil.copy2(ROOT / 'output/official_market_eod_latest.json', tmp / 'output/official_market_eod_latest.json')
    return tmp


def selftest():
    before_top = [c['Code'] for c in json.loads((ROOT / 'v5/early_warning_latest.json').read_text(encoding='utf-8'))['Candidates']]
    pristine = ROOT / 'v5_history/rolling'
    names = sorted(p.name[:10] for p in pristine.glob('*.csv.gz'))
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / 'fake'
        build_fake_site(pristine, fake, names[-45:])

        # 1) 正常同步一個新交易日；同時補齊 history 窗口內缺參考價的日期
        tmp = fresh(td, 'r1'); roll = tmp / 'v5_history/rolling'
        day = names[-1]
        (roll / f'{day}.csv.gz').unlink()
        r = run_sim(tmp, fake)
        if r.returncode != 0:
            fail('模擬同步失敗：' + r.stdout + r.stderr)
        print('  ' + ' / '.join(x for x in r.stdout.splitlines() if x.startswith(('同步', '補參考價')))[:200])
        for d in names[-25:]:
            got = read_gz(roll / f'{d}.csv.gz')
            same_ohlcv(read_gz(pristine / f'{d}.csv.gz'), got, d)
            if sum(1 for x in got if x['ref_price']) < 0.9 * len(got):
                fail(f'{d} 參考價仍未補齊')
        top = [c['Code'] for c in json.loads((tmp / 'v5/early_warning_latest.json').read_text(encoding='utf-8'))['Candidates']]
        if top != before_top:
            fail(f'TOP 10 改變 {top} vs {before_top}')
        verify(tmp)
        eod = json.loads((tmp / 'output/official_market_eod_latest.json').read_text(encoding='utf-8'))
        if eod['Date'] != day or eod['Count'] != len(read_gz(pristine / f'{day}.csv.gz')):
            fail('同步後官方收盤行情檔未更新')
        print('  同步新交易日＋補參考價：OHLCV 逐筆相同、參考價全部補齊、TOP 10 不變')

        # 1b) 參考價、成交金額解析
        spec = importlib.util.spec_from_file_location('v5_live_t', tmp / 'v5/v5_live.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        for p in sorted(pristine.glob('*.csv.gz'))[::-1]:
            rr = read_gz(p)
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
                    fail(f"{d2} {x['code']} 參考價／成交金額解析錯誤")
                n += 1
        print(f'  參考價與成交金額解析（{d2}，{n} 檔）：全部相同')

        # 2) 官方資料與 daily_screen 不一致 → 中止、不改檔
        tmp = fresh(td, 'r2'); roll = tmp / 'v5_history/rolling'
        (roll / f'{day}.csv.gz').unlink()
        bad = json.loads((fake / f'{day}_tw.json').read_bytes())
        row = bad['tables'][1]['data'][0]
        row[6] = str(float(row[6].replace(',', '')) + 1); row[8] = str(float(row[8].replace(',', '')) + 1)
        (Path(td) / 'bad.json').write_bytes(json.dumps(bad, ensure_ascii=False).encode())
        snap = snapshot(tmp)
        r = run_sim(tmp, fake, (day, 'tw', str(Path(td) / 'bad.json')))
        if r.returncode == 0 or 'HISTORY_MISMATCH' not in r.stdout or snapshot(tmp) != snap:
            fail('資料不一致時沒有正確中止：' + r.stdout)
        print('  官方資料與 daily_screen 不一致：中止，檔案未改動')

        # 3) 行情庫落後 40 個交易日（超過 daily_screen 30 日窗口）→ 自動向官方補抓
        tmp = fresh(td, 'r3'); roll = tmp / 'v5_history/rolling'
        for d in names[-40:]:
            (roll / f'{d}.csv.gz').unlink()
        r = run_sim(tmp, fake)
        if r.returncode != 0:
            fail('長期缺漏補抓失敗：' + r.stdout + r.stderr)
        for d in names[-40:]:
            same_ohlcv(read_gz(pristine / f'{d}.csv.gz'), read_gz(roll / f'{d}.csv.gz'), d)
        rep = json.loads((tmp / 'v5/early_warning_latest.json').read_text(encoding='utf-8'))
        if [c['Code'] for c in rep['Candidates']] != before_top:
            fail('補抓後 TOP 10 改變')
        print(f"  落後 40 個交易日：補抓 {len(rep['Data']['BackfilledDays'])} 日、休市 {len(rep['Data']['ClosedDays'])} 日，資料逐筆相同、TOP 10 不變")

        # 4) 落後太久（超過約半年）→ 中止、不改檔
        tmp = fresh(td, 'r4'); roll = tmp / 'v5_history/rolling'
        for d in names[-200:]:
            (roll / f'{d}.csv.gz').unlink()
        snap = snapshot(tmp)
        r = run_sim(tmp, fake)
        if r.returncode == 0 or 'ROLLING_GAP_TOO_LONG' not in r.stdout or snapshot(tmp) != snap:
            fail('落後太久時沒有正確中止：' + r.stdout)
        print('  落後超過半年：中止，檔案未改動')
    print('V5 SELFTEST OK')


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest()
    else:
        verify()
