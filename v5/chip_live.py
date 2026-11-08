#!/usr/bin/env python3
"""YHTIS 籌碼雷達每日資料（GitHub Actions，標準函式庫即可執行）。

來源（全部為官方公開資料）：
  三大法人（日）：TWSE T86、TPEx 三大法人買賣明細
  融資融券（日）：TWSE MI_MARGN、TPEx 融資融券餘額
  千張大戶（週）：TDCC 集保戶股權分散表（opendata 1-5）
防錯（欄位對應錯誤時一定會中止該來源，不發布錯誤數字）：
  - 回應日期必須等於要求的交易日；筆數下限。
  - 三大法人：外資 + 投信 + 自營商 必須等於 三大法人合計（>= 98% 的股票）。
  - 融資券：前日餘額 + 增加 - 減少 必須等於 今日餘額（>= 95% 的股票）。
  - TDCC：各級距比例合計約 100%。
每個來源、每個交易日獨立；失敗只影響該來源，下次執行自動補抓（最近 20 個交易日）。
輸出：v5/chip_latest.json；歷史：v5_history/chip/{inst,margin,tdcc}/日期.json.gz（保留 60 份）。
不讀寫 Golden Entry、Stage1、Stage2、AI 分數。
用法：python3 v5/chip_live.py [--offline]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import io
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHIP = ROOT / 'v5_history/chip'
ROLL_INDEX = ROOT / 'v5_history/rolling/index.json'
ROLL = ROOT / 'v5_history/rolling'
OUT = ROOT / 'v5/chip_latest.json'
TZ = timezone(timedelta(hours=8))
BACKFILL_DAYS = 20
KEEP = 60
FETCH_PAUSE = 3
MAX_REQUESTS = 90
MIN_ROWS = {'TWSE': 600, 'TPEx': 400}
STOCK = re.compile(r'[1-9]\d{3}')

spec = importlib.util.spec_from_file_location('daily_screen_screen', ROOT / 'daily_screen/screen.py')
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)


class ChipError(RuntimeError):
    pass


class NotPublished(ChipError):
    pass


REQUESTS = 0


def get(url):
    global REQUESTS
    if REQUESTS >= MAX_REQUESTS:
        raise ChipError('MAX_REQUESTS')
    if REQUESTS:
        time.sleep(FETCH_PAUSE)
    REQUESTS += 1
    return screen.fetch(url)


def hdr(x):
    return re.sub(r'<[^>]*>', '', str(x)).replace(' ', '').replace('　', '').strip()


def n(x):
    v = screen.value(re.sub(r'<[^>]*>', '', str(x)))
    return v


def doc_tables(doc):
    """官方 JSON 有兩種形狀：頂層 fields/data，或 tables[]。"""
    out = []
    if isinstance(doc.get('fields'), list) and isinstance(doc.get('data'), list):
        out.append(([hdr(f) for f in doc['fields']], doc['data'], str(doc.get('title', ''))))
    for t in doc.get('tables') or []:
        if isinstance(t, dict) and isinstance(t.get('fields'), list) and isinstance(t.get('data'), list):
            out.append(([hdr(f) for f in t['fields']], t['data'], str(t.get('title', ''))))
    return out


def load_json(raw, day):
    try:
        doc = json.loads(raw)
    except Exception as e:
        raise ChipError('JSON_INVALID') from e
    stat = str(doc.get('stat', ''))
    if '沒有符合' in stat or (stat and stat.lower() != 'ok'):
        raise NotPublished('NO_DATA ' + stat[:60])
    d = str(doc.get('date', '')).replace('/', '').replace('-', '')
    if d and d != day.replace('-', ''):
        raise ChipError(f'DATE_MISMATCH {d}')
    return doc


# ---------- 三大法人 ----------

def inst_urls(market, day):
    dt = datetime.strptime(day, '%Y-%m-%d')
    if market == 'TWSE':
        q = urllib.parse.urlencode({'date': dt.strftime('%Y%m%d'), 'selectType': 'ALLBUT0999', 'response': 'json'})
        return [f'https://www.twse.com.tw/rwd/zh/fund/T86?{q}', f'https://www.twse.com.tw/fund/T86?{q}']
    q = urllib.parse.urlencode({'type': 'Daily', 'sect': 'EW', 'date': dt.strftime('%Y/%m/%d'), 'response': 'json'})
    return [f'https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?{q}']


def map_inst(fields):
    def find(pred):
        c = [i for i, f in enumerate(fields) if pred(f)]
        return c[0] if c else None
    code = find(lambda f: f in ('證券代號', '代號'))
    f_ex = find(lambda f: '買賣超' in f and '外' in f and '不含外資自營商' in f)
    f_dl = find(lambda f: '買賣超' in f and f.startswith('外資自營商'))
    trust = find(lambda f: '買賣超' in f and f.startswith('投信'))
    dealer = find(lambda f: '買賣超' in f and f.startswith('自營商') and '自行' not in f and '避險' not in f)
    total = find(lambda f: '買賣超' in f and '三大法人' in f)
    if None in (code, f_ex, trust, dealer, total):
        # 櫃買新版固定 24 欄（與 tw_screener/inst.py 已驗證的位置相同）：
        # 代號,名稱, 7 組(買進,賣出,買賣超)=外陸資(不含自營)/外資自營商/外資及陸資合計/投信/自營商(自行)/自營商(避險)/自營商合計, 三大法人合計
        if len(fields) == 24 and fields[0] in ('代號', '證券代號'):
            return dict(code=0, f_ex=4, f_dl=7, trust=13, dealer=22, total=23)
        return None
    return dict(code=code, f_ex=f_ex, f_dl=f_dl, trust=trust, dealer=dealer, total=total)


def parse_inst(raw, market, day):
    doc = load_json(raw, day)
    for fields, data, _ in doc_tables(doc):
        m = map_inst(fields)
        if not m:
            continue
        out, ok, tot = {}, 0, 0
        for r in data:
            if len(r) != len(fields):
                raise ChipError('BAD_ROW_LENGTH')
            c = str(r[m['code']]).strip()
            if not STOCK.fullmatch(c):
                continue
            v = [n(r[m[k]]) if m[k] is not None else 0.0 for k in ('f_ex', 'f_dl', 'trust', 'dealer', 'total')]
            if any(x is None for x in v):
                continue
            fx, fd, tr, de, to = v
            tot += 1
            ok += abs((fx + fd + tr + de) - to) < 1.5
            out[c] = [round((fx + fd) / 1000), round(tr / 1000), round(de / 1000), round(to / 1000)]
        if len(out) < MIN_ROWS[market]:
            raise ChipError(f'INST_ROWS_LOW {market} {len(out)}')
        if ok < 0.98 * tot:
            raise ChipError(f'INST_IDENTITY_FAIL {market} {ok}/{tot}（欄位對應錯誤，停止）')
        return out
    raise ChipError(f'INST_TABLE_NOT_FOUND {market}')


# ---------- 融資融券 ----------

def margin_urls(market, day):
    dt = datetime.strptime(day, '%Y-%m-%d')
    if market == 'TWSE':
        q = urllib.parse.urlencode({'date': dt.strftime('%Y%m%d'), 'selectType': 'ALL', 'response': 'json'})
        return [f'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?{q}', f'https://www.twse.com.tw/exchangeReport/MI_MARGN?{q}']
    q = urllib.parse.urlencode({'date': dt.strftime('%Y/%m/%d'), 'response': 'json'})
    return [f'https://www.tpex.org.tw/www/zh-tw/margin/balance?{q}']


def map_margin(fields):
    """回傳 (code, 資:[買,賣,償,前,今], 券:[買,賣,償,前,今])。"""
    idx = {f: [i for i, x in enumerate(fields) if x == f] for f in set(fields)}
    code = (idx.get('代號') or idx.get('股票代號') or idx.get('證券代號') or [None])[0]
    if code is None:
        return None
    # TWSE：兩組重複欄名（買進、賣出、現金償還/現券償還、前日餘額、今日餘額）
    if len(idx.get('今日餘額', [])) == 2 and len(idx.get('前日餘額', [])) == 2 and len(idx.get('買進', [])) == 2:
        mg = [idx['買進'][0], idx['賣出'][0], (idx.get('現金償還') or [None])[0], idx['前日餘額'][0], idx['今日餘額'][0]]
        sh = [idx['買進'][1], idx['賣出'][1], (idx.get('現券償還') or [None])[0], idx['前日餘額'][1], idx['今日餘額'][1]]
        if None in mg or None in sh:
            return None
        return code, mg, sh

    # TPEx：資買、資賣、現償、前資餘額、資餘額；券買、券賣、券償、前券餘額、券餘額
    def one(pred):
        c = [i for i, f in enumerate(fields) if pred(f)]
        return c[0] if c else None
    mg = [one(lambda f: f.startswith('資買')), one(lambda f: f.startswith('資賣')), one(lambda f: f.startswith('現償')),
          one(lambda f: f.startswith('前資餘額')), one(lambda f: f.startswith('資餘額'))]
    sh = [one(lambda f: f.startswith('券買')), one(lambda f: f.startswith('券賣')), one(lambda f: f.startswith('券償')),
          one(lambda f: f.startswith('前券餘額')), one(lambda f: f.startswith('券餘額'))]
    if None in mg or None in sh:
        # 櫃買融資券固定欄位（與 tw_screener/bigwave/margin.py 已驗證的位置相同）
        if '資餘額' in fields and len(fields) >= 19:
            return code, [3, 4, 5, 2, 6], [12, 11, 13, 10, 14]
        return None
    return code, mg, sh


def parse_margin(raw, market, day):
    doc = load_json(raw, day)
    for fields, data, _ in doc_tables(doc):
        m = map_margin(fields)
        if not m:
            continue
        code, mg, sh = m
        out, ok_m, ok_s, tot = {}, 0, 0, 0
        for r in data:
            if len(r) != len(fields):
                continue
            c = str(r[code]).strip()
            if not STOCK.fullmatch(c):
                continue
            a = [n(r[i]) for i in mg]
            b = [n(r[i]) for i in sh]
            if any(x is None for x in a + b):
                continue
            tot += 1
            ok_m += abs(a[3] + a[0] - a[1] - a[2] - a[4]) < 0.5      # 融資：前 + 買 - 賣 - 現償 = 今
            ok_s += abs(b[3] + b[1] - b[0] - b[2] - b[4]) < 0.5      # 融券：前 + 賣 - 買 - 現償 = 今
            out[c] = [round(a[4]), round(a[4] - a[3]), round(b[4]), round(b[4] - b[3])]  # 單位：張
        if len(out) < MIN_ROWS[market] * 0.6:
            raise ChipError(f'MARGIN_ROWS_LOW {market} {len(out)}')
        if ok_m < 0.95 * tot or ok_s < 0.95 * tot:
            raise ChipError(f'MARGIN_IDENTITY_FAIL {market} 資{ok_m}/券{ok_s}/{tot}（欄位對應錯誤，停止）')
        return out
    raise ChipError(f'MARGIN_TABLE_NOT_FOUND {market}')


# ---------- TDCC 千張大戶（週） ----------

TDCC_URL = 'https://opendata.tdcc.com.tw/getOD.ashx?id=1-5'


def parse_tdcc(raw):
    for enc in ('utf-8-sig', 'cp950'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ChipError('TDCC_ENCODING')
    rows = list(csv.reader(io.StringIO(text)))
    head = [hdr(x) for x in rows[0]]
    try:
        i_date, i_code, i_lv, i_pct = (head.index('資料日期'), head.index('證券代號'), head.index('持股分級'),
                                       next(i for i, h in enumerate(head) if h.startswith('占集保庫存數比例')))
        i_ppl = head.index('人數')
    except (ValueError, StopIteration):
        raise ChipError('TDCC_HEADER ' + ','.join(head))
    dates = set()
    lv = {}
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        c = r[i_code].strip()
        if not STOCK.fullmatch(c):
            continue
        dates.add(r[i_date].strip())
        try:
            level, pct, ppl = int(r[i_lv]), float(r[i_pct]), int(float(r[i_ppl]))
        except ValueError:
            continue
        lv.setdefault(c, {})[level] = (pct, ppl)
    if len(dates) != 1:
        raise ChipError(f'TDCC_DATES {sorted(dates)[:3]}')
    d = dates.pop()
    out, ok = {}, 0
    for c, m in lv.items():
        s = sum(m[k][0] for k in range(1, 16) if k in m)
        ok += 99 <= s <= 101
        if 15 in m:
            out[c] = [round(m[15][0], 2), m[15][1]]  # 千張以上：比例%、人數
    if len(out) < 1500 or ok < 0.95 * len(lv):
        raise ChipError(f'TDCC_CHECK_FAIL rows={len(out)} sum_ok={ok}/{len(lv)}')
    return f'{d[:4]}-{d[4:6]}-{d[6:8]}', out


# ---------- 存取 ----------

def path(kind, day):
    return CHIP / kind / f'{day}.json.gz'


def save(kind, day, data):
    p = path(kind, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.new')
    tmp.write_bytes(gzip.compress(json.dumps(data, separators=(',', ':')).encode(), mtime=0))
    tmp.replace(p)


def load(kind, day):
    return json.loads(gzip.decompress(path(kind, day).read_bytes()))


def have(kind):
    return sorted(p.name[:10] for p in (CHIP / kind).glob('*.json.gz')) if (CHIP / kind).is_dir() else []


def prune(kind):
    for d in have(kind)[:-KEEP]:
        path(kind, d).unlink()


def fetch_both(kind, day):
    urls, parse = (inst_urls, parse_inst) if kind == 'inst' else (margin_urls, parse_margin)
    out = {}
    for market in ('TWSE', 'TPEx'):
        errs = []
        got = None
        for u in urls(market, day):
            try:
                got = parse(get(u), market, day)
                break
            except NotPublished as e:
                errs.append('未公布:' + str(e)[:60])
            except Exception as e:
                errs.append(type(e).__name__ + ':' + str(e)[:140])
        if got is None:
            raise ChipError(f'{kind} {market} {day}: ' + ' | '.join(errs))
        out.update(got)
    return out


def names():
    idx = json.loads(ROLL_INDEX.read_text(encoding='utf-8'))
    d = idx['Last']
    with gzip.open(ROLL / f'{d}.csv.gz', 'rt', encoding='utf-8') as fh:
        return d, idx['Days'], {r['code']: (r['name'], r['market'], float(r['close'])) for r in csv.DictReader(fh)}


def streak(series):
    s = 0
    for v in reversed(series):
        if v > 0 and s >= 0:
            s += 1
        elif v < 0 and s <= 0:
            s -= 1
        else:
            break
    return s


def build(asof, nm):
    status = {}
    inst_days = [d for d in have('inst') if d <= asof]
    mg_days = [d for d in have('margin') if d <= asof]
    td_days = have('tdcc')
    stocks = {c: {'Name': v[0], 'Market': v[1], 'Close': v[2]} for c, v in nm.items() if STOCK.fullmatch(c)}
    if inst_days:
        last = inst_days[-1]
        hist = [load('inst', d) for d in inst_days[-20:]]
        for c, s in stocks.items():
            seq = [h.get(c) for h in hist]
            cur = seq[-1]
            if not cur:
                continue
            f_ser = [x[0] if x else 0 for x in seq]
            t_ser = [x[1] if x else 0 for x in seq]
            s.update({'Foreign': cur[0], 'Trust': cur[1], 'Dealer': cur[2], 'Inst3': cur[3],
                      'Foreign5': sum(f_ser[-5:]), 'Trust5': sum(t_ser[-5:]), 'Inst3_5': sum((x[3] if x else 0) for x in seq[-5:]),
                      'ForeignStreak': streak(f_ser), 'TrustStreak': streak(t_ser)})
        status['inst'] = {'Date': last, 'Days': len(inst_days), 'Fresh': last == asof}
    if mg_days:
        last = mg_days[-1]
        m = load('margin', last)
        for c, s in stocks.items():
            if c in m:
                s.update({'Margin': m[c][0], 'MarginChg': m[c][1], 'Short': m[c][2], 'ShortChg': m[c][3]})
        status['margin'] = {'Date': last, 'Fresh': last == asof}
    if td_days:
        last = td_days[-1]
        cur = load('tdcc', last)
        prev = load('tdcc', td_days[-2]) if len(td_days) > 1 else {}
        for c, s in stocks.items():
            if c in cur:
                s['Big1000Pct'] = cur[c][0]
                s['Big1000People'] = cur[c][1]
                if c in prev:
                    s['Big1000Chg'] = round(cur[c][0] - prev[c][0], 2)
        status['tdcc'] = {'Date': last, 'PrevDate': td_days[-2] if len(td_days) > 1 else None}
    return status, stocks


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    a = ap.parse_args(argv)
    asof, days, nm = names()
    log = []
    if not a.offline:
        targets = days[-BACKFILL_DAYS:]
        for kind in ('inst', 'margin'):
            missing = [d for d in targets if d not in set(have(kind))]
            for d in reversed(missing):  # 先抓最新日
                try:
                    save(kind, d, fetch_both(kind, d))
                    log.append(f'{kind} {d} OK')
                except ChipError as e:
                    log.append(f'{kind} {d} 未取得：{e}')
                    if 'MAX_REQUESTS' in str(e):
                        break
        try:
            td_date, td = parse_tdcc(get(TDCC_URL))
            if td_date not in have('tdcc'):
                save('tdcc', td_date, td)
                log.append(f'tdcc {td_date} OK')
            else:
                log.append(f'tdcc {td_date} 已有')
        except Exception as e:
            log.append(f'tdcc 未取得：{type(e).__name__}: {str(e)[:140]}')
        for k in ('inst', 'margin', 'tdcc'):
            prune(k)
    status, stocks = build(asof, nm)
    rep = {'AsOfDate': asof, 'GeneratedTaipei': datetime.now(TZ).isoformat(timespec='seconds'), 'Sources': status,
           'Units': '三大法人、融資券：張；千張大戶：占集保庫存比例 %',
           'Note': '官方公開資料；各來源日期分別揭示。非買進訊號。', 'Log': log, 'Stocks': stocks}
    if OUT.is_file():
        try:
            prev = json.loads(OUT.read_text(encoding='utf-8'))
            if prev.get('Sources') == rep['Sources'] and prev.get('Stocks') == rep['Stocks'] and prev.get('AsOfDate') == asof:
                print('CHIP_NO_CHANGE', json.dumps(status, ensure_ascii=False))
                for x in log:
                    print(x)
                return 0
        except ValueError:
            pass
    tmp = OUT.with_name(OUT.name + '.new')
    tmp.write_text(json.dumps(rep, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    tmp.replace(OUT)
    for x in log:
        print(x)
    print('籌碼狀態', json.dumps(status, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('YHTIS CHIP ABORT（已發布檔案未更動）: ' + type(e).__name__ + ': ' + str(e), file=sys.stderr)
        sys.exit(2)
