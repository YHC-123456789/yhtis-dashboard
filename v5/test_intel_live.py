#!/usr/bin/env python3
"""綜合判讀檢查。

  python3 v5/test_intel_live.py --verify-only   發布前檢查 v5/intel_latest.json
  python3 v5/test_intel_live.py --selftest      以模擬官方網站跑完整流程：
      月營收抓取＋恆等式、重大訊息欄位比對、RSS／鉅亨解析、欄位不明不發布、營收數字錯置不寫入。
"""
from __future__ import annotations

import gzip
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(msg):
    print('INTEL VERIFY FAIL:', msg, file=sys.stderr)
    sys.exit(1)


def verify(root=ROOT):
    rep = json.loads((root / 'v5/intel_latest.json').read_text(encoding='utf-8'))
    idx = json.loads((root / 'v5_history/rolling/index.json').read_text(encoding='utf-8'))
    if rep['AsOfDate'] != idx['Last']:
        fail('綜合判讀日期與行情庫不一致')
    m = rep['Market']
    if not m.get('N') or m['N'] < 1500:
        fail('市場廣度樣本過少')
    if not 0 <= m['UpPct'] <= 100 or not 0 <= m['AboveMA20Pct'] <= 100:
        fail('廣度比例超出範圍')
    for k in ('S1', 'S2'):
        if len(rep[k]) > 8:
            fail(f'{k} 超過 8 檔')
        for s in rep[k]:
            if s['Val20'] < 0.5:
                fail(f'{k} {s["Code"]} 成交值未達 5,000 萬')
            if abs(s['StopRef'] - s['Close'] * 0.92) > 0.02:
                fail(f'{k} {s["Code"]} 停損價錯誤')
            if k == 'S2' and s['NearHigh'] < 94.99:
                fail(f'S2 {s["Code"]} 未接近 52 週高')
        key = 'R20' if k == 'S1' else 'R60'
        vals = [s[key] for s in rep[k]]
        if vals != sorted(vals, reverse=True):
            fail(f'{k} 排序錯誤')
    cb = rep.get('Combo')
    if cb:
        p = [(x['P60'], x['M60']) for x in cb['Top']]
        if [x[0] for x in p] != sorted((x[0] for x in p), reverse=True):
            fail('多因子排行未依 60 日上漲機率排序')
        if any(x['Val20'] < 0.5 or not 0 <= x['P60'] <= 100 for x in cb['Top']):
            fail('多因子排行數值異常')
    dt = rep.get('DayTrade')
    if dt:
        for x in dt['Top'] + dt['LimitUpHigh']:
            if x['Val20'] < 3 or not (0 <= x['Win'] <= 100 and 0 <= x['Up2'] <= 100):
                fail(f'當沖統計異常 {x["Code"]}')
        if any(not x['Pattern'].startswith('漲停・收高') for x in dt['LimitUpHigh']):
            fail('漲停收高清單混入其他型態')
    for h in rep['Headlines']:
        if not h['Link'].startswith('http'):
            fail('新聞連結格式錯誤')
    print('INTEL VERIFY OK', rep['AsOfDate'], '市場', m['State'], 'S1', rep['S1Count'], 'S2', rep['S2Count'],
          '營收', rep['Revenue']['LatestYM'], '來源', list(rep['Sources']))


ANN_TW = [{'出表日期': '1151109', '發言日期': '1151109', '發言時間': '173512', '公司代號': '2330', '公司名稱': '台積電',
           '主旨 ': '本公司取得重大訂單', '符合條款': '第51款', '事實發生日': '1151109', '說明': 'x'},
          {'出表日期': '1151109', '發言日期': '1151109', '發言時間': '180000', '公司代號': '2303', '公司名稱': '聯電',
           '主旨 ': '公告本公司董事會決議減資', '符合條款': '第11款', '事實發生日': '1151109', '說明': 'x'}]
ANN_OTC = [{'Date': '1151109', 'SecuritiesCompanyCode': '6488', 'CompanyName': '環球晶', 'SpeechDate': '1151109', 'SpeechTime': '090000',
            'Subject': '公告本公司股票注意交易資訊'}]
RSS = '''<?xml version="1.0"?><rss><channel><item><title>國安基金進場護盤 台股大漲 - 經濟日報</title><link>https://example.com/a</link>
<pubDate>Mon, 09 Nov 2026 05:00:00 GMT</pubDate><source url="https://money.udn.com">經濟日報</source></item>
<item><title><![CDATA[台積電法說會釋利多（2330）]]></title><link>https://example.com/b</link><pubDate>Mon, 09 Nov 2026 06:00:00 GMT</pubDate><source url="x">工商時報</source></item>
</channel></rss>'''
CNYES = {'items': {'data': [{'newsId': 123, 'title': '關稅疑慮再起 電子股承壓', 'publishAt': 1794200000}]}}


def run(tmp, site, today, extra=''):
    code = f"""
import sys, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('intel', {str(tmp / 'v5/intel_live.py')!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.FETCH_PAUSE = 0
S = Path({str(site)!r})
def fetch(url):
    {extra}
    if 'mopsov' in url:
        mk = 'sii' if '/sii/' in url else 'otc'
        p = S / f'rev_{{mk}}.html'
        if not p.exists(): raise m.SourceFail('HTTP 404')
        return p.read_bytes()
    if 't187ap04_L' in url: return (S / 'ann_tw.json').read_bytes()
    if 'mopsfin_t187ap04_O' in url or 'mopsfe_t187ap04_O' in url: raise m.SourceFail('HTTP 404')
    if 't187ap04_O' in url: return (S / 'ann_otc.json').read_bytes()
    if 'news.google' in url: return (S / 'rss.xml').read_bytes()
    if 'cnyes' in url: return (S / 'cnyes.json').read_bytes()
    raise m.SourceFail('unknown ' + url)
m.screen.fetch = fetch
sys.exit(m.main(['--today', {today!r}]))
"""
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)


def selftest():
    probe = Path('/mnt/user-data/uploads/tw_screener/out/bigwave/probe2')
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / 'repo'
        for d in ('v5', 'daily_screen', 'v5_history/rolling', 'v5_history/revenue'):
            shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns('__pycache__', 'paper', 'news'))
        site = Path(td) / 'site'
        site.mkdir()
        if (probe / 'mopsov_sii_2023_09.txt').exists():
            for mk in ('sii', 'otc'):
                shutil.copy2(probe / f'mopsov_{mk}_2023_09.txt', site / f'rev_{mk}.html')
        else:  # 沒有官方頁面樣本時，以行情庫內最新月營收產生同格式頁面
            for mk in ('sii', 'otc'):
                rows = []
                with gzip.open(sorted((ROOT / 'v5_history/revenue').glob('*.csv.gz'))[-1], 'rt', encoding='utf-8') as fh:
                    for r in csv.DictReader(fh):
                        if r['market'] == mk:
                            rv = float(r['rev']); yy = float(r['yoy'] or 0); ly = rv / (1 + yy / 100) if yy > -100 else 0
                            cells = ''.join(f'<td>{v:,.2f}</td>' for v in (rv, rv, ly, 0, yy, rv, ly, 0))
                            rows.append(f"<tr align=right><td align=center>{r['code']}</td><td align=left>{r['name']}</td>{cells}")
                (site / f'rev_{mk}.html').write_bytes(('<th>產業別：測試</th>' + ''.join(rows)).encode('big5', errors='replace'))
        (site / 'ann_tw.json').write_text(json.dumps(ANN_TW, ensure_ascii=False), encoding='utf-8')
        (site / 'ann_otc.json').write_text(json.dumps(ANN_OTC, ensure_ascii=False), encoding='utf-8')
        (site / 'rss.xml').write_text(RSS, encoding='utf-8')
        (site / 'cnyes.json').write_text(json.dumps(CNYES, ensure_ascii=False), encoding='utf-8')

        # 1) 正常：11/09（上月=10 月，1～15 日每天重抓）
        r = run(tmp, site, '2026-11-09')
        if r.returncode != 0:
            fail('模擬執行失敗：' + r.stdout + r.stderr)
        rep = json.loads((tmp / 'v5/intel_latest.json').read_text(encoding='utf-8'))
        if not (tmp / 'v5_history/revenue/202610.csv.gz').exists():
            fail('10 月營收未寫入：' + r.stdout)
        src = rep['Sources']
        if src.get('announcements', {}).get('Markets') != ['TPEx', 'TWSE']:
            fail(f'重大訊息兩市場應都取得（上櫃第一個網址失敗後改用第二個）：{src} {r.stdout}')
        tones = {a['Code']: a['Tone'] for a in rep['Announcements']}
        if tones != {'2330': '正面', '2303': '負面', '6488': '注意'}:
            fail(f'重大訊息判讀錯誤：{tones}')
        heads = {h['Title']: h for h in rep['Headlines']}
        if '政策支撐' not in heads['國安基金進場護盤 台股大漲']['Tags'] or heads['國安基金進場護盤 台股大漲']['Source'] != '經濟日報':
            fail('新聞政策標記或來源解析錯誤')
        if '2330' not in heads['台積電法說會釋利多（2330）']['Codes']:
            fail('新聞個股對應錯誤')
        if '風險事件' not in heads['關稅疑慮再起 電子股承壓']['Tags']:
            fail('鉅亨網新聞未解析或未標記')
        if '政策支撐' not in rep['Market']['Policy']:
            fail('市場政策摘要缺少')
        if 'Ann' not in rep['Stocks'].get('2330', {}) or 'News' not in rep['Stocks'].get('2330', {}):
            fail('個股消息未彙整')
        print(f"  正常抓取：營收 10 月寫入、重大訊息 {len(rep['Announcements'])} 則（上櫃備援網址）、新聞 {len(rep['Headlines'])} 則、政策標記 {list(rep['Market']['Policy'])}")

        # 2) 重大訊息欄位不明 → 不發布該來源，其餘照常
        (site / 'ann_tw.json').write_text(json.dumps([{'a': 1, 'b': 2}]), encoding='utf-8')
        shutil.rmtree(tmp / 'v5_history/news')
        r = run(tmp, site, '2026-11-10')
        if r.returncode != 0 or 'ANN_FIELDS TWSE' not in r.stdout:
            fail('欄位不明未被偵測：' + r.stdout)
        rep = json.loads((tmp / 'v5/intel_latest.json').read_text(encoding='utf-8'))
        if any(a['Market'] == 'TWSE' for a in rep['Announcements']):
            fail('欄位不明仍輸出上市重大訊息')
        print('  上市重大訊息欄位不明：記錄欄位名稱、不發布該來源，新聞與營收不受影響')

        # 3) 營收欄位錯置（年增率與營收對不上）→ 不寫入，保留舊檔
        before = (tmp / 'v5_history/revenue/202610.csv.gz').read_bytes()
        for mk in ('sii', 'otc'):
            b = (site / f'rev_{mk}.html').read_bytes().decode('big5', errors='replace')
            import re
            b = re.sub(r'(<tr align=right><td align=center>[0-9A-Za-z]+</td><td align=left>.*?</td>(?:<td[^>]*>[^<]*</td>){4})<td([^>]*)>[^<]*</td>',
                       lambda m_: m_.group(1) + f'<td{m_.group(2)}>99.99</td>', b, flags=re.S | re.I)
            (site / f'rev_{mk}.html').write_bytes(b.encode('big5', errors='replace'))
        r = run(tmp, site, '2026-11-11')
        if 'REV_IDENTITY_FAIL' not in r.stdout:
            fail('營收恆等式錯誤未被偵測：' + r.stdout)
        if (tmp / 'v5_history/revenue/202610.csv.gz').read_bytes() != before:
            fail('營收錯置仍覆寫檔案')
        print('  營收年增率與營收對不上：不寫入，保留前一次正確資料')
        verify(tmp)
    print('INTEL SELFTEST OK')


if __name__ == '__main__':
    selftest() if '--selftest' in sys.argv else verify()
