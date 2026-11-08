#!/usr/bin/env python3
"""籌碼雷達檢查。

  python3 v5/test_chip_live.py --verify-only   發布前檢查 v5/chip_latest.json
  python3 v5/test_chip_live.py --selftest      以模擬官方網站（官方欄位格式、符合恆等式的數值）跑完整流程：
      正常抓取＋補抓、千張大戶週變化、欄位錯置被擋下、尚未公布時保留舊資料。
"""
from __future__ import annotations

import gzip
import csv
import importlib.util
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(msg):
    print('CHIP VERIFY FAIL:', msg, file=sys.stderr)
    sys.exit(1)


def verify(root=ROOT):
    p = root / 'v5/chip_latest.json'
    rep = json.loads(p.read_text(encoding='utf-8'))
    idx = json.loads((root / 'v5_history/rolling/index.json').read_text(encoding='utf-8'))
    if rep['AsOfDate'] != idx['Last']:
        fail('籌碼檔日期與行情庫不一致')
    for k, v in rep['Sources'].items():
        if k != 'tdcc' and v['Date'] > rep['AsOfDate']:
            fail(f'{k} 日期晚於行情日')
    if len(rep['Stocks']) < 1700:
        fail('股票數過少')
    print('CHIP VERIFY OK', rep['AsOfDate'], {k: v['Date'] for k, v in rep['Sources'].items()})


def fmt(v):
    return f'{int(v):,}'


T86 = ['證券代號', '證券名稱', '外陸資買進股數(不含外資自營商)', '外陸資賣出股數(不含外資自營商)', '外陸資買賣超股數(不含外資自營商)',
       '外資自營商買進股數', '外資自營商賣出股數', '外資自營商買賣超股數', '投信買進股數', '投信賣出股數', '投信買賣超股數', '自營商買賣超股數',
       '自營商買進股數(自行買賣)', '自營商賣出股數(自行買賣)', '自營商買賣超股數(自行買賣)', '自營商買進股數(避險)', '自營商賣出股數(避險)',
       '自營商買賣超股數(避險)', '三大法人買賣超股數']
MG = ['代號', '名稱', '買進', '賣出', '現金償還', '前日餘額', '今日餘額', '次一營業日限額', '買進', '賣出', '現券償還', '前日餘額', '今日餘額',
      '次一營業日限額', '資券互抵', '註記']


def build_site(site, days, codes, seed=1):
    """每個交易日四份回應＋TDCC 一份 CSV。數值隨機但符合官方恆等式。"""
    rnd = random.Random(seed)
    site.mkdir(parents=True, exist_ok=True)
    bal = {c: [rnd.randint(100, 50000), rnd.randint(0, 3000)] for c in codes}
    for d in days:
        ymd = d.replace('-', '')
        tw, tp, mtw, mtp = [], [], [], []
        for c, mk in codes.items():
            f, t, de = (rnd.randint(-5000, 5000) * 1000 for _ in range(3))
            if mk == 'TWSE':
                tw.append([c, '名', '0', '0', fmt(f), '0', '0', '0', '0', '0', fmt(t), fmt(de), '0', '0', fmt(de), '0', '0', '0', fmt(f + t + de)])
            else:
                tp.append([c, '名', '0', '0', fmt(f), '0', '0', '0', '0', '0', fmt(f), '0', '0', fmt(t), '0', '0', fmt(de), '0', '0', '0', '0', '0',
                           fmt(de), fmt(f + t + de)])
            mb, ms, mr = rnd.randint(0, 500), rnd.randint(0, 500), rnd.randint(0, 20)
            sb, ss, sr = rnd.randint(0, 50), rnd.randint(0, 50), rnd.randint(0, 5)
            p, q = bal[c]
            nm, nq = max(0, p + mb - ms - mr), max(0, q + ss - sb - sr)
            ms = p + mb - mr - nm
            sb = q + ss - sr - nq
            bal[c] = [nm, nq]
            if mk == 'TWSE':
                mtw.append([c, '名', fmt(mb), fmt(ms), fmt(mr), fmt(p), fmt(nm), '0', fmt(sb), fmt(ss), fmt(sr), fmt(q), fmt(nq), '0', '0', ''])
            else:
                mtp.append([c, '名', fmt(p), fmt(mb), fmt(ms), fmt(mr), fmt(nm), '0', '0', '0', fmt(q), fmt(ss), fmt(sb), fmt(sr), fmt(nq), '0', '0', '0', '0', ''])
        (site / f'inst_TWSE_{d}.json').write_text(json.dumps({'stat': 'OK', 'date': ymd, 'fields': T86, 'data': tw}, ensure_ascii=False))
        (site / f'inst_TPEx_{d}.json').write_text(json.dumps({'stat': 'ok', 'date': ymd, 'tables': [
            {'fields': ['代號', '名稱'] + [f'c{i}' for i in range(22)], 'data': tp}]}, ensure_ascii=False))
        (site / f'margin_TWSE_{d}.json').write_text(json.dumps({'stat': 'OK', 'date': ymd, 'tables': [
            {'fields': ['項目'], 'data': [['x']]}, {'fields': MG, 'data': mtw}]}, ensure_ascii=False))
        (site / f'margin_TPEx_{d}.json').write_text(json.dumps({'stat': 'ok', 'date': ymd, 'tables': [
            {'fields': ['代號', '名稱', '前資餘額', 'a', 'b', 'c', '資餘額'] + [f'z{i}' for i in range(13)], 'data': mtp}]}, ensure_ascii=False))
    lines = ['資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%']
    td = days[-1].replace('-', '')
    for c in codes:
        big = round(rnd.uniform(20, 80), 2)
        rest = 100 - big
        for lv in range(1, 15):
            lines.append(f'{td},{c},{lv},10,1000,{rest / 14:.4f}')
        lines.append(f'{td},{c},15,{rnd.randint(1, 50)},1000,{big}')
        lines.append(f'{td},{c},17,100,1000,100.00')
    (site / 'tdcc.csv').write_text('\n'.join(lines), encoding='utf-8-sig')


def run(tmp, site, extra=''):
    code = f"""
import sys, re, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('chip', {str(tmp / 'v5/chip_live.py')!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.FETCH_PAUSE = 0
S = Path({str(site)!r})
def fetch(url):
    if 'tdcc' in url: return (S / 'tdcc.csv').read_bytes()
    kind = 'inst' if ('T86' in url or 'insti' in url) else 'margin'
    mk = 'TWSE' if 'twse' in url else 'TPEx'
    x = re.search(r'date=(\\d{{4}})(?:%2F)?(\\d{{2}})(?:%2F)?(\\d{{2}})', url)
    d = '-'.join(x.groups())
    p = S / f'{{kind}}_{{mk}}_{{d}}.json'
    {extra}
    if not p.exists(): return '{{"stat":"很抱歉，沒有符合條件的資料!"}}'.encode()
    return p.read_bytes()
m.screen.fetch = fetch
sys.exit(m.main([]))
"""
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)


def selftest():
    idx = json.loads((ROOT / 'v5_history/rolling/index.json').read_text(encoding='utf-8'))
    days = idx['Days'][-20:]
    with gzip.open(ROOT / f"v5_history/rolling/{idx['Last']}.csv.gz", 'rt', encoding='utf-8') as fh:
        codes = {r['code']: r['market'] for r in csv.DictReader(fh)}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / 'repo'
        for d in ('v5', 'daily_screen'):
            shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns('__pycache__'))
        (tmp / 'v5_history/rolling').mkdir(parents=True)
        shutil.copy2(ROOT / 'v5_history/rolling/index.json', tmp / 'v5_history/rolling/index.json')
        shutil.copy2(ROOT / f"v5_history/rolling/{idx['Last']}.csv.gz", tmp / f"v5_history/rolling/{idx['Last']}.csv.gz")
        shutil.rmtree(tmp / 'v5_history/chip', ignore_errors=True)
        site = Path(td) / 'site'
        # 最新日的融資券「尚未公布」
        build_site(site, days, codes)
        (site / f'margin_TWSE_{days[-1]}.json').unlink()
        r = run(tmp, site)
        if r.returncode != 0:
            fail('模擬執行失敗：' + r.stdout + r.stderr)
        rep = json.loads((tmp / 'v5/chip_latest.json').read_text(encoding='utf-8'))
        src = rep['Sources']
        if src['inst']['Date'] != days[-1] or src['inst']['Days'] != 20:
            fail(f'三大法人應補齊 20 日：{src}')
        if src['margin']['Date'] != days[-2] or src['margin']['Fresh']:
            fail(f'融資券最新日未公布時應顯示前一日並標示非最新：{src}')
        if 'tdcc' not in src:
            fail('千張大戶未取得')
        s = next(v for v in rep['Stocks'].values() if 'Trust' in v)
        for k in ('Foreign', 'Trust', 'Dealer', 'Inst3', 'Trust5', 'TrustStreak', 'Margin', 'MarginChg', 'Big1000Pct'):
            if k not in s:
                fail(f'欄位缺少 {k}')
        print(f"  正常抓取：三大法人 20 日、融資券最新日未公布→顯示 {src['margin']['Date']}、千張大戶 {src['tdcc']['Date']}")
        # 第二次執行：融資券補上；其他不重抓
        build_site(site, days, codes)
        r = run(tmp, site)
        rep = json.loads((tmp / 'v5/chip_latest.json').read_text(encoding='utf-8'))
        if r.returncode != 0 or rep['Sources']['margin']['Date'] != days[-1] or sum('inst' in x and 'OK' in x for x in rep['Log']) != 0:
            fail('第二次執行應只補抓融資券：' + r.stdout)
        print('  第二次執行：只補抓遺漏的融資券，三大法人不重抓')
        # 欄位錯置：投信與自營商互換 → 不寫入該日
        shutil.rmtree(tmp / 'v5_history/chip/inst')
        for d in days:
            p = site / f'inst_TWSE_{d}.json'
            j = json.loads(p.read_text())
            for row in j['data']:
                row[10], row[11] = row[11], row[10] + '1'
            p.write_text(json.dumps(j, ensure_ascii=False))
        r = run(tmp, site)
        if (tmp / 'v5_history/chip/inst').exists() and any((tmp / 'v5_history/chip/inst').iterdir()):
            fail('欄位錯置仍寫入三大法人資料')
        if 'INST_IDENTITY_FAIL' not in r.stdout:
            fail('欄位錯置未被偵測：' + r.stdout[:300])
        rep = json.loads((tmp / 'v5/chip_latest.json').read_text(encoding='utf-8'))
        if 'inst' in rep['Sources'] or any('Trust' in v for v in rep['Stocks'].values()):
            fail('欄位錯置後仍輸出三大法人數字')
        print('  三大法人欄位錯置：恆等式不成立，該來源不發布；融資券與千張大戶不受影響')
    print('CHIP SELFTEST OK')


if __name__ == '__main__':
    selftest() if '--selftest' in sys.argv else verify()
