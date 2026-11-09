#!/usr/bin/env python3
"""YHTIS V5 每日正式流程（GitHub Actions，標準函式庫即可執行）。

順序：同步官方日行情 → 資料品質檢查 → V5 研究分數 → 通過才發布 TOP 10。
  1. 同步：daily_screen/history.csv 中比滾動行情庫更新的交易日，逐日向 TWSE/TPEx 官方重新抓取，
     OHLCV 用 daily_screen/screen.py 的同一個 parse_payload 解析，並要求與 history.csv 逐筆完全一致；
     另取官方「漲跌」換算參考價（ref_price = 收盤 − 漲跌），用來辨識除權息。
  2. 品質檢查：與 v5_history/audit_v2.py 相同規則（最近 250 個交易日）。
  3. V5：compute／研究分數直接沿用 v5/v5_early_warning.py，不修改公式、權重或門檻；
     輸入價格先依參考價做除權息還原（backtest_v5b.py 規則 C），20 日均成交值 < 5,000 萬只標記、不排除。
  4. 發布閘門：行情庫最新日＝daily_screen 最新日、交易日無缺漏、合格率 >= 80% 且 >= 100 檔，
     才輸出 TOP 10；否則頁面只顯示「今日不發布」。
不讀寫 Golden Entry、Stage1、Stage2、AI 分數或首頁。任何錯誤都在寫檔前中止，已發布檔案保持原狀。

用法：python3 v5/v5_live.py [--offline]
  --offline  不連網，只用現有滾動行情庫重算（測試用；行情庫須已含最新日）。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import io
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLL = ROOT / 'v5_history/rolling'
FIRST_SEEN = ROOT / 'v5_history/first_seen.json'
HISTORY = ROOT / 'daily_screen/history.csv'
LATEST = ROOT / 'daily_screen/latest.json'
OUT_JSON = ROOT / 'v5/early_warning_latest.json'
OUT_HTML = ROOT / 'v5/index.html'
TZ = timezone(timedelta(hours=8))

KEEP_DAYS = 260        # 滾動行情庫保留交易日數
AUDIT_WINDOW = 250     # 品質檢查窗口（同 audit_v2.py）
AUDIT_MIN_BARS = 120   # 同 audit_v2.py --min-bars 預設
LOOKBACK = 120         # V5 計算讀取的交易日數（同 v5_early_warning_store.py）
MAX_GAP_WEEKDAYS = 130  # 最多自動補抓約半年
TPEX_ODDLOT_FROM = '2026-10-19'  # daily_screen 上櫃成交量自此日起含零股（與官方 dailyQuotes 全量相同）
FETCH_PAUSE = 3        # 每個交易日之間暫停秒數（避免官方網站限流）
LOW_LIQ_VALUE20 = 5e7  # 20 日平均成交值低於 5,000 萬元：標記流動性低（不排除；見 backtest_v5b D）
FIELDS = ['date', 'market', 'code', 'name', 'open', 'high', 'low', 'close', 'ref_price', 'volume', 'value', 'source', 'source_sha256']
STOCK = re.compile(r'[1-9]\d{3}')
ORDER = ['missing_history', 'stale', 'insufficient_bars', 'unit_mismatch', 'bad_ohlc', 'unexplained_jump', 'ok']


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orig = load_module('v5_original', ROOT / 'v5/v5_early_warning.py')
screen = load_module('daily_screen_screen', ROOT / 'daily_screen/screen.py')


class LiveError(RuntimeError):
    pass


def f(x):
    try:
        return float(x) if x not in ('', None) else None
    except ValueError:
        return None


# ---------- 1. 同步官方日行情 ----------

def strip_html(x):
    return re.sub(r'<[^>]*>', '', str(x)).replace(',', '').strip()


def extra_fields(raw, market):
    """從同一份官方原始回應取 參考價 與 成交金額。回傳 code -> (ref_price, value)。"""
    doc = json.loads(raw)
    out = {}
    for t in doc.get('tables', []):
        if not isinstance(t, dict):
            continue
        title = str(t.get('title', ''))
        fields = [screen.normalized_header(x) for x in t.get('fields', [])]
        if market == 'TWSE':
            if '每日收盤行情' not in title or '證券代號' not in fields:
                continue
            ix = {k: fields.index(k) for k in ('證券代號', '收盤價', '漲跌(+/-)', '漲跌價差', '成交金額')}
        else:
            if '上櫃股票' not in title or '管理股票' in title or '定價交易' in title or '代號' not in fields:
                continue
            ix = {k: fields.index(k) for k in ('代號', '收盤', '漲跌', '成交金額(元)')}
        for arr in t.get('data', []):
            code = str(arr[ix['證券代號' if market == 'TWSE' else '代號']]).strip()
            if not screen.is_stock(code):
                continue
            close = screen.value(arr[ix['收盤價' if market == 'TWSE' else '收盤']])
            if market == 'TWSE':
                amt = screen.value(strip_html(arr[ix['漲跌價差']]))
                sign_html = str(arr[ix['漲跌(+/-)']])
                sign = 1.0 if ('color:red' in sign_html or '+' in strip_html(sign_html)) else (
                    -1.0 if ('color:green' in sign_html or '-' in strip_html(sign_html)) else 0.0)
                chg = None if amt is None else (amt * sign if sign else (0.0 if amt == 0 else None))
                val = screen.value(arr[ix['成交金額']])
            else:
                chg = screen.value(strip_html(arr[ix['漲跌']]))
                val = screen.value(arr[ix['成交金額(元)']])
            ref = round(close - chg, 4) if (close is not None and chg is not None) else None
            out[code] = (ref, val)
    return out


def fetch_day(day, hist_rows):
    """抓取一個交易日的 TWSE+TPEx。hist_rows 不為 None 時，與 daily_screen/history.csv 逐筆比對。
    官方明確回覆「無資料」（休市）時回傳 None。"""
    rows = {}
    for market in ('TWSE', 'TPEx'):
        errs = []
        got = None
        closed = True
        for url in screen.urls(market, day):
            try:
                raw = screen.fetch(url)
                got = (screen.parse_payload(raw, market, day), extra_fields(raw, market))
                break
            except screen.NoSession as e:
                errs.append('NoSession:' + str(e)[:80])
            except Exception as e:  # 換下一個官方網址
                closed = False
                errs.append(type(e).__name__ + ':' + str(e)[:120])
        if got is None:
            if closed and market == 'TWSE' and hist_rows is None:
                return None
            raise LiveError(f'FETCH_FAILED {market} {day}: ' + ' | '.join(errs))
        base, extra = got
        src = 'TWSE MI_INDEX' if market == 'TWSE' else 'TPEx dailyQuotes'
        for code, r in base.items():
            ref, val = extra.get(code, (None, None))
            rows[code] = dict(date=day, market=market, code=code, name=r['Name'], open=r['Open'], high=r['High'], low=r['Low'],
                              close=r['Close'], ref_price=ref, volume=r['Volume'], value=val, source=src,
                              source_sha256=r['SourceSHA256'])
    if sum(1 for r in rows.values() if r['ref_price'] is not None) < 0.9 * len(rows):
        raise LiveError(f'REF_PRICE_COVERAGE_LOW {day}')
    if hist_rows is None:
        return rows
    # 與 daily_screen 已驗證的同日資料逐筆比對（同一套解析，必須完全一致）
    h = {r['Code']: r for r in hist_rows}
    if set(h) != set(rows):
        raise LiveError(f'CODESET_MISMATCH {day}: history={len(h)} fetched={len(rows)}')
    for code, r in rows.items():
        for k_live, k_hist in (('open', 'Open'), ('high', 'High'), ('low', 'Low'), ('close', 'Close'), ('volume', 'Volume')):
            fv, hv = float(r[k_live]), float(h[code][k_hist])
            if abs(fv - hv) > 1e-6:
                # daily_screen 在 TPEX_ODDLOT_FROM 之前的上櫃成交量只含整股（1000 股倍數）；官方含零股，必 >= 該值
                if (k_live == 'volume' and r['market'] == 'TPEx' and day < TPEX_ODDLOT_FROM
                        and hv % 1000 == 0 and fv >= hv):
                    continue
                raise LiveError(f'HISTORY_MISMATCH {day} {code} {k_live}: fetched={r[k_live]} history={h[code][k_hist]}')
    return rows


def weekdays_between(a, b):
    """a < d < b 的平日（休市日由官方「無資料」回覆判斷）。"""
    d = datetime.strptime(a, '%Y-%m-%d').date() + timedelta(days=1)
    end = datetime.strptime(b, '%Y-%m-%d').date()
    out = []
    while d < end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def ref_coverage(rows):
    return sum(1 for r in rows if r.get('ref_price')) / max(1, len(rows))


def day_bytes(rows):
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=FIELDS, lineterminator='\r\n')
    w.writeheader()
    for c in sorted(rows):
        w.writerow({k: ('' if rows[c][k] is None else rows[c][k]) for k in FIELDS})
    return gzip.compress(s.getvalue().encode('utf-8'), mtime=0)


def read_day(path):
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


def load_history_by_day():
    if not HISTORY.is_file():
        raise LiveError('DAILY_SCREEN_HISTORY_MISSING')
    by = defaultdict(list)
    with HISTORY.open(encoding='utf-8-sig', newline='') as fh:
        for r in csv.DictReader(fh):
            by[r['Date']].append(r)
    return by


# ---------- 2. 品質檢查（同 audit_v2.py） ----------

def audit(days, day_rows, universe, first_seen):
    window = days[-AUDIT_WINDOW:]
    day_idx = {d: i for i, d in enumerate(sorted(set(first_seen.values()) | set(days)))}
    bars = defaultdict(list)
    for d in window:
        for r in day_rows[d]:
            if r['code'] in universe:
                bars[r['code']].append(r)
    out = {}
    for c in universe:
        b = bars.get(c, [])
        notes = []
        reason = 'ok'
        if not b:
            reason = 'missing_history'
        else:
            last = b[-1]['date']
            n_valid = sum(1 for r in b if f(r['close']) and f(r['volume']) is not None)
            bad = sum(1 for r in b if None in (f(r['open']), f(r['high']), f(r['low']), f(r['close'])) or not (
                f(r['low']) <= min(f(r['open']), f(r['close'])) <= max(f(r['open']), f(r['close'])) <= f(r['high'])))
            jumps = exr = ipo = noref = 0
            prev = None
            fs = day_idx.get(first_seen.get(c), 0)
            for r in b:
                cl, ref = f(r['close']), f(r['ref_price'])
                if prev and cl:
                    if abs(cl / prev - 1) > 0.105:
                        if day_idx[r['date']] - fs < 5:
                            ipo += 1
                        elif ref is None:
                            noref += 1
                        elif abs(ref - prev) / prev > 0.001:
                            exr += 1
                        else:
                            jumps += 1
                prev = cl or prev
            unitbad = 0
            for r in b:
                v, val, lo, hi = f(r['volume']), f(r.get('value')), f(r['low']), f(r['high'])
                if v and val and lo and hi and not (lo * 0.98 <= val / v <= hi * 1.02):
                    if not (0.5 <= (val / v) / f(r['close']) <= 2):
                        unitbad += 1
            gap = len([d for d in window if d > last])
            if gap > 0:
                reason = 'stale'
            elif n_valid < AUDIT_MIN_BARS:
                reason = 'insufficient_bars'
            elif unitbad:
                reason = 'unit_mismatch'
            elif bad:
                reason = 'bad_ohlc'
            elif jumps:
                reason = 'unexplained_jump'
            if noref:
                notes.append(f'jump_without_ref_price={noref}')
        out[c] = (reason, len(b), ';'.join(notes))
    return out


# ---------- 3. V5（公式沿用原版） ----------

def v5(days, day_rows, universe, eod, audit_res):
    look = days[-LOOKBACK:]
    bars = defaultdict(list)
    rawbars = defaultdict(list)
    cumf = {}
    exdates = defaultdict(list)
    for d in look:
        for r in day_rows[d]:
            c = r['code']
            if c not in universe:
                continue
            o, h, l, cl, v = (orig.number(r[k]) for k in ('open', 'high', 'low', 'close', 'volume'))
            # 除權息還原：參考價 ≠ 昨收時，因子 = 參考價 / 昨收；之後價格除以累積因子（指標只看比例，與 backtest_v5b C 相同）
            if rawbars[c] and r['ref_price']:
                prev = rawbars[c][-1]['收盤']
                ref = orig.number(r['ref_price'])
                if prev and ref and abs(ref / prev - 1) > 0.001:
                    cumf[c] = cumf.get(c, 1.0) * min(5.0, max(0.2, ref / prev))
                    exdates[c].append(d)
            k = cumf.get(c, 1.0)
            rawbars[c].append({'日期': d, '收盤': cl, '成交量': v})
            bars[c].append({'日期': d, '開盤': o / k if o else o, '最高': h / k if h else h, '最低': l / k if l else l,
                            '收盤': cl / k if cl else cl, '成交量': v})
    asof = days[-1]
    stats = Counter()
    vetted = []
    for code in sorted(universe):
        s = eod[code]
        reason = audit_res[code][0]
        if reason != 'ok':
            stats[f'audit_{reason}'] += 1
            continue
        raw = bars.get(code, [])
        if len(raw) < orig.MIN_BARS:
            stats['short_history'] += 1
            continue
        if raw[-1]['日期'] != asof:
            stats['stale_history'] += 1
            continue
        if any(not orig.valid_bar(b) for b in raw):
            stats['invalid_bar_in_window'] += 1
            continue
        rb = rawbars[code]
        if abs(rb[-1]['收盤'] - float(s['Close'])) > 0.011 or abs(rb[-1]['成交量'] - float(s['Volume'])) > 1:
            stats['eod_mismatch'] += 1
            continue
        try:
            m = orig.compute(raw)
        except (ValueError, ZeroDivisionError, statistics.StatisticsError):
            stats['compute_error'] += 1
            continue
        val20 = statistics.mean(x['收盤'] * x['成交量'] for x in rb[-20:])
        vetted.append({'Code': code, 'Name': s['Name'], 'Market': s['Market'], 'Date': asof, 'Close': float(s['Close']),
                       'AvgValue20': round(val20), 'LowLiquidity': val20 < LOW_LIQ_VALUE20,
                       'ExRightsDates': exdates.get(code, []), **m})
    n = len(universe)
    cov = len(vetted) / n if n else 0
    ok = cov >= orig.MIN_COVERAGE and len(vetted) >= 100
    cands = []
    if ok:
        med5 = statistics.median(x['Return5Pct'] for x in vetted)
        med20 = statistics.median(x['Return20Pct'] for x in vetted)
        for v in vetted:
            v['RS5vsMedian'] = round(v['Return5Pct'] - med5, 3)
            v['RS20vsMedian'] = round(v['Return20Pct'] - med20, 3)
            score = ((v['RS5vsMedian'] > 0) * 2 + (v['RS20vsMedian'] > 0) * 2 + int(v['OBV10Rising']) * 2 + int(v['BBWContracting']) * 2
                     + int(v['JTurningUp']) + int(v['NearPrior20High']) + int(v['EMA9Above20']))
            v['ResearchScore'] = score
            v['EligibleResearch'] = score >= 7 and v['VolumeRatio5'] is not None and v['VolumeRatio5'] >= 1
        cands = sorted((v for v in vetted if v['EligibleResearch']),
                       key=lambda v: (-v['ResearchScore'], -v['RS20vsMedian'], -v['RS5vsMedian'], v['Code']))[:10]
    return vetted, cands, stats, cov, ok


# ---------- 4. 網頁 ----------

BACKTEST = [  # v5/backtest_v5b.py 規則 C（還原價）逐日事件回測；每筆扣成本 0.785%，對照為同日合格股等權平均
    ('20 日', '2019–2022 訓練', '+1.24%', '+0.59%', '2.64'),
    ('20 日', '2023–2024 驗證', '+1.20%', '+0.41%', '2.35'),
    ('20 日', '2025–2026 保留', '+1.12%', '+0.84%', '1.10'),
    ('60 日', '2019–2022 訓練', '+4.98%', '+1.51%', '2.55'),
    ('60 日', '2023–2024 驗證', '+5.01%', '+1.40%', '3.24'),
    ('60 日', '2025–2026 保留', '+5.68%', '+3.04%', '1.87'),
    ('5 日', '三段期間', '約 -0.4%', '約 0%', '<0.8'),
]


def esc(x):
    return str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def render(rep):
    pub = rep['Status'] == 'PUBLISHED'
    def table_rows(cands):
        return ''.join(
        f"<tr><td>{i}</td><td>{esc(c['Code'])}</td><td>{esc(c['Name'])}</td><td>{esc(c['Market'])}</td><td>{c['Close']:g}</td>"
        f"<td><b>{c['ResearchScore']}</b></td><td>{c['RS5vsMedian']:+.2f}</td><td>{c['RS20vsMedian']:+.2f}</td>"
        f"<td>{c['VolumeRatio5']:.2f}</td><td>{c['AvgValue20'] / 1e4:,.0f}{' <span class=low>低</span>' if c['LowLiquidity'] else ''}</td><td>{c['BBWvsRecentMedian']}</td>"
        f"<td>{'是' if c['OBV10Rising'] else '—'}</td><td>{'是' if c['JTurningUp'] else '—'}</td>"
        f"<td>{'是' if c['NearPrior20High'] else '—'}</td><td>{'是' if c['EMA9Above20'] else '—'}</td></tr>"
        for i, c in enumerate(cands, 1))
    rows = table_rows(rep.get('CandidatesLiquid', []))
    rows_low = table_rows(rep.get('CandidatesLowLiquidity', []))
    bt = ''.join(f'<tr><td>{a}</td><td>{b}</td><td>{c}</td><td><b>{d}</b></td><td>{e}</td></tr>' for a, b, c, d, e in BACKTEST)
    if pub:
        main = (f"<section><h2>V5 研究名單</h2>"
                "<p>研究分 = RS5 高於中位數 2＋RS20 高於中位數 2＋OBV 10 日上升 2＋布林帶寬收斂 2＋J 值低檔轉上 1＋接近前 20 日高 1＋EMA9 在 EMA20 上 1；"
                "研究分 ≥ 7 且量比 ≥ 1 才列入。價格已依官方參考價做除權息還原。<b>「低」＝20 日平均成交值不到 5,000 萬</b>，成交較少、價差可能較大。</p>"
                + "<h3>成交值夠大（20 日均 ≥ 5,000 萬）前 10</h3>" + ("<div class=scroll><table><tr><th>名次</th><th>代號</th><th>名稱</th><th>市場</th><th>收盤</th><th>研究分</th><th>RS5</th><th>RS20</th><th>量比</th><th>20日均成交值(萬)</th><th>帶寬比</th><th>OBV升</th><th>J轉上</th><th>近20日高</th><th>EMA9&gt;20</th></tr>" + rows + "</table></div>" if rows else "<p><b>今日沒有符合條件的股票，不硬湊。</b></p>")
                + "<h3>成交值小（研究參考）前 10</h3><p>買賣價差大、不易成交；近兩年同類股票 60 日上漲機率約 37%。</p>" + ("<div class=scroll><table><tr><th>名次</th><th>代號</th><th>名稱</th><th>市場</th><th>收盤</th><th>研究分</th><th>RS5</th><th>RS20</th><th>量比</th><th>20日均成交值(萬)</th><th>帶寬比</th><th>OBV升</th><th>J轉上</th><th>近20日高</th><th>EMA9&gt;20</th></tr>" + rows_low + "</table></div>" if rows_low else "<p>無。</p>")
                + "<p>原始 TOP 10（不分成交值、與回測同一套排序）保留在 <a href=early_warning_latest.json>early_warning_latest.json</a> 的 Candidates。</p></section>")
    else:
        main = (f"<section class=block><h2>⛔ 今日不發布 TOP 10</h2><p>{esc(rep.get('BlockReason', '資料檢查未通過'))}</p>"
                "<p>資料不完整時不產生名單，以免把舊資料或錯誤資料當成最新訊號。</p></section>")
    ex = '、'.join(f'{esc(k)} {v}' for k, v in sorted(rep['Exclusions'].items())) or '無'
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YHTIS V5 研究名單</title><style>
body{{font:15px -apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif;max-width:1150px;margin:24px auto;padding:0 16px;background:#f7f9fc;color:#172338}}
h1{{margin-bottom:4px}}p{{color:#526175;line-height:1.65}}section{{background:#fff;border:1px solid #dde6f0;border-radius:12px;padding:16px;margin:18px 0}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;border-bottom:1px solid #edf1f5;padding:8px 6px;white-space:nowrap}}
th{{background:#f2f6fb}}.scroll{{overflow:auto}}.tag{{display:inline-block;border-radius:20px;padding:4px 10px;font-weight:600}}
.ok{{background:#e5f6ea;color:#1d6b35}}.bad{{background:#fde8e8;color:#9b1c1c}}.warning{{background:#fff6e5;border-left:4px solid #cb901e;padding:10px 14px}}
.block{{border:2px solid #e0a3a3;background:#fff7f7}}.kpi{{display:flex;gap:12px;flex-wrap:wrap}}.kpi div{{background:#f2f6fb;border-radius:10px;padding:10px 14px}}
.kpi b{{display:block;font-size:20px;color:#172338}}.low{{background:#fde8e8;color:#9b1c1c;border-radius:6px;padding:1px 6px;font-size:12px}}</style></head><body>
<h1>YHTIS V5 研究名單</h1>
<p>官方行情日期 <b>{rep['AsOfDate']}</b>　<span class="tag {'ok' if pub else 'bad'}">{'資料檢查通過' if pub else '資料檢查未通過'}</span>　產生時間（台北）{esc(rep['GeneratedTaipei'])}</p>
<div class=warning><b>研究觀察名單，不是買進訊號。</b>不是 Golden Entry、Stage1 或 Stage2，沒有進場點、停損或勝率保證。回測顯示這套分數適合 20～60 日波段觀察，5 日短線沒有優勢。</div>
<section><div class=kpi><div>合格股票<b>{rep['ValidatedStocks']} / {rep['UniverseTotal']}</b></div><div>合格率<b>{rep['CoveragePct']}%</b></div>
<div>行情庫交易日<b>{rep['Data']['RollingDays']}</b></div><div>品質檢查通過<b>{rep['Audit']['ok']}</b></div></div>
<p>排除原因：{ex}；近 120 日有除權息、已做價格還原 {rep['ExRightsAdjustedStocks']} 檔。</p></section>
{main}
<section><h2>歷史回測（2019～2026，逐日事件）</h2><p>每天收盤選出 TOP 10，隔日開盤買、持有期滿開盤賣，扣成本 0.785%；超額報酬是相對同日所有合格股等權平均。t 值約 2 以上才較能排除運氣。這是事件平均，不是資金投組績效，也還沒計算最大回撤。<b>若只保留 20 日均成交值 ≥ 5,000 萬的股票，2019～2024 的超額報酬就不顯著（t 0.5～1.5）</b>，代表優勢主要來自成交較少的股票。</p>
<div class=scroll><table><tr><th>持有</th><th>期間</th><th>每筆淨報酬</th><th>超額報酬</th><th>t 值</th></tr>{bt}</table></div></section>
<p>資料來源：TWSE MI_INDEX、TPEx dailyQuotes 官方逐日行情（以官方參考價還原除權息；TPEx 成交量含零股）。每個交易日由 GitHub Actions 同步、檢查、計算；任何一步失敗都不更新名單。</p>
<p><a href="../">← 返回 YHTIS Dashboard</a>　<a href="early_warning_latest.json">原始資料 JSON</a></p>
</body></html>
"""


# ---------- 5. 網站其他分頁需要的資料 ----------

EOD_OUT = ROOT / 'output/official_market_eod_latest.json'
DAYTRADE_OUT = ROOT / 'v5/daytrade_watch.json'
SCORES_OUT = ROOT / 'v5/scores_latest.json'
ROLL_INDEX = ROLL / 'index.json'
DT_PRICE = (50, 150)       # 與 tw_screener/daytrade_scan.py 相同
DT_RANGE = (0.02, 0.04)
DT_MIN_VALUE20 = 3e8


def eod_payload(day, rows):
    """首頁「官方收盤行情」用的檔案（格式同既有 output/official_market_eod_latest.json；只含 4 碼普通股）。"""
    stocks = [{'Code': r['code'], 'Name': r['name'], 'Close': f"{float(r['close']):.2f}", 'Volume': int(float(r['volume'])),
               'Market': r['market']} for r in sorted(rows, key=lambda r: (r['market'] != 'TWSE', r['code']))]
    c = Counter(r['market'] for r in rows)
    return {'Date': day, 'Status': 'OFFICIAL_DATED_SNAPSHOT_VALIDATED', 'TWSE': c['TWSE'], 'TPEx': c['TPEx'], 'Count': len(stocks),
            'Sources': {'TWSE': 'TWSE MI_INDEX ' + day, 'TPEx': 'TPEx dailyQuotes ' + day},
            'TPExVolumeScope': 'TPEx dailyQuotes incl. odd lots（與 daily_screen 2026-10-19 起相同）',
            'Scope': '4-digit ordinary stocks only（不含 ETF）', 'Generator': 'v5/v5_live.py（與 daily_screen/history.csv 逐筆比對）',
            'AIRecomputed': False, 'AdjCloseVerified': False, 'Stocks': stocks}


SCORE_FIELDS = ['Code', 'Name', 'Market', 'Close', 'Score', 'Rank', 'Eligible', 'RS5', 'RS20', 'OBV10Rising', 'BBWContracting', 'BBWRatio',
                'JTurningUp', 'J', 'NearPrior20High', 'EMA9Above20', 'VolumeRatio5', 'AvgValue20', 'LowLiquidity', 'Return5Pct', 'Return20Pct']


def scores_payload(rep, vetted, ok):
    """全部合格股票的 V5 研究分（供網站個股條件與綜合排行；與 TOP 10 同一套計算、同一排序）。"""
    if not ok:
        return {'AsOfDate': rep['AsOfDate'], 'Status': rep['Status'], 'Fields': SCORE_FIELDS, 'Rows': []}
    order = sorted(vetted, key=lambda v: (-v['ResearchScore'], -v['RS20vsMedian'], -v['RS5vsMedian'], v['Code']))
    rows = [[v['Code'], v['Name'], v['Market'], v['Close'], v['ResearchScore'], i, v['EligibleResearch'], v['RS5vsMedian'], v['RS20vsMedian'],
             v['OBV10Rising'], v['BBWContracting'], v['BBWvsRecentMedian'], v['JTurningUp'], v['J'], v['NearPrior20High'], v['EMA9Above20'],
             v['VolumeRatio5'], v['AvgValue20'], v['LowLiquidity'], v['Return5Pct'], v['Return20Pct']] for i, v in enumerate(order, 1)]
    return {'AsOfDate': rep['AsOfDate'], 'Status': rep['Status'], 'Fields': SCORE_FIELDS, 'Universe': len(rows),
            'Rule': '研究分 = RS5>中位數 2 + RS20>中位數 2 + OBV10 上升 2 + 布林帶寬<0.85×近 20 日中位數 2 + J 低檔轉上 1 + 接近前 20 日高 1 + EMA9>EMA20 1；Eligible = 研究分>=7 且量比>=1',
            'Rows': rows}


def daytrade_watch(days, day_rows, universe, eod):
    """盤後產生的隔日當沖觀察名單（規則同 daytrade_scan.py；不是盤中訊號、未回測當沖績效）。"""
    look = days[-26:]
    ser = defaultdict(list)
    for d in look:
        for r in day_rows[d]:
            if r['code'] in universe:
                ser[r['code']].append(r)
    out = []
    for c, b in ser.items():
        if len(b) < 21 or b[-1]['date'] != days[-1]:
            continue
        try:
            rng, rets = [], []
            for i in range(1, len(b)):
                pc = float(b[i - 1]['close'])
                ref = f(b[i]['ref_price']) or pc
                rng.append((float(b[i]['high']) - float(b[i]['low'])) / ref)
                rets.append(float(b[i]['close']) / ref - 1)
            r20 = statistics.mean(rng[-20:])
            v20 = statistics.mean(float(x['close']) * float(x['volume']) for x in b[-20:])
            close = float(b[-1]['close'])
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if not (DT_PRICE[0] <= close <= DT_PRICE[1] and DT_RANGE[0] <= r20 <= DT_RANGE[1] and v20 >= DT_MIN_VALUE20):
            continue
        if any(abs(x) >= 0.095 for x in rets[-5:]):
            continue
        out.append({'Code': c, 'Name': eod[c]['Name'], 'Market': eod[c]['Market'], 'Close': close,
                    'DayPct': round(rets[-1] * 100, 2), 'DayRangePct': round(rng[-1] * 100, 2), 'AvgRange20Pct': round(r20 * 100, 2),
                    'MaxRange20Pct': round(max(rng[-20:]) * 100, 1), 'AvgValue20': round(v20), 'LotCost': round(close * 1000)})
    out.sort(key=lambda x: -x['AvgValue20'])
    return {'AsOfDate': days[-1], 'Rule': '收盤 50～150 元、20 日平均振幅 2%～4%、20 日平均成交值 ≥ 3 億；排除近 5 日有接近漲跌停（±9.5%）者',
            'Note': '盤後觀察名單，不是盤中訊號；當沖績效未回測。下單前請確認處置股、注意股與最新新聞。', 'Count': len(out), 'Stocks': out}


# ---------- 主程式 ----------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    a = ap.parse_args(argv)

    latest = json.loads(LATEST.read_text(encoding='utf-8'))
    target = latest['AsOfDate']
    hist = load_history_by_day()
    if max(hist) != target:
        raise LiveError(f'DAILY_SCREEN_INCONSISTENT latest.json={target} history.csv={max(hist)}')

    files = sorted(ROLL.glob('*.csv.gz'))
    if not files:
        raise LiveError('ROLLING_STORE_EMPTY')
    roll_days = [p.name[:10] for p in files]
    roll_last = roll_days[-1]
    new_days = sorted(d for d in hist if d > roll_last)
    if new_days and a.offline:
        raise LiveError(f'OFFLINE_BUT_STORE_BEHIND rolling_last={roll_last} target={target}')
    # 行情庫落後超過 daily_screen 的 30 日窗口：直接向官方逐日補抓（無 history 可比對，仍經 parse_payload 驗證）
    gap_days = weekdays_between(roll_last, min(hist)) if (new_days and min(hist) > roll_last) else []
    if len(gap_days) > MAX_GAP_WEEKDAYS:
        raise LiveError(f'ROLLING_GAP_TOO_LONG rolling_last={roll_last} weekdays={len(gap_days)}（需重新補種子資料）')
    # 缺參考價的既有交易日（例：種子資料中來自 daily_screen 的日期）：在 history 窗口內就重抓補齊
    repair_days = [] if a.offline else [d for d in roll_days if d in hist and ref_coverage(read_day(ROLL / f'{d}.csv.gz')) < 0.5]

    if not new_days and not repair_days and OUT_JSON.is_file():
        prev = json.loads(OUT_JSON.read_text(encoding='utf-8'))
        if prev.get('AsOfDate') == target and prev.get('Data', {}).get('RollingRange', [None, None])[1] == roll_last:
            print(f'NO_NEW_SESSION: V5 已是 {target}，不重寫檔案', flush=True)
            return 0

    fetched = {}
    closed = []
    for i, d in enumerate(gap_days + repair_days + new_days):
        if i:
            time.sleep(FETCH_PAUSE)
        rows = fetch_day(d, hist.get(d))
        if rows is None:
            closed.append(d)
            print(f'休市 {d}（官方無資料）', flush=True)
            continue
        fetched[d] = rows
        tag = '補參考價' if d in repair_days else ('補抓（官方驗證）' if d in gap_days else '同步')
        print(f'{tag} {d}: {len(rows)} 檔' + ('，與 daily_screen 逐筆一致' if d in hist else ''), flush=True)

    day_rows = {}
    keep = sorted(set(roll_days) | set(fetched))[-KEEP_DAYS:]
    for d in keep:
        if d in fetched:
            day_rows[d] = [{k: ('' if v is None else str(v)) for k, v in r.items()} for _, r in sorted(fetched[d].items())]
        else:
            day_rows[d] = read_day(ROLL / f'{d}.csv.gz')
    days = keep
    asof = days[-1]
    if asof != target:
        raise LiveError(f'STORE_NOT_AT_TARGET store={asof} target={target}')
    # 交易日缺漏檢查：daily_screen 近 30 日內的每個交易日都必須在行情庫中
    missing = [d for d in hist if d >= days[0] and d not in set(days)]
    if missing:
        raise LiveError(f'ROLLING_MISSING_DAYS {missing}')

    first_seen = json.loads(FIRST_SEEN.read_text(encoding='utf-8'))
    for d in days:
        for r in day_rows[d]:
            first_seen.setdefault(r['code'], d)

    eod = {r['Code']: r for r in hist[target] if STOCK.fullmatch(r['Code'])}
    universe = set(eod)
    audit_res = audit(days, day_rows, universe, first_seen)
    audit_cnt = Counter(v[0] for v in audit_res.values())
    vetted, cands, stats, cov, ok = v5(days, day_rows, universe, eod, audit_res)

    status = 'PUBLISHED' if ok else 'BLOCKED_INSUFFICIENT_COVERAGE'
    _elig = sorted((x for x in vetted if x.get('EligibleResearch')), key=lambda x: (-x['ResearchScore'], -x['RS20vsMedian'], -x['RS5vsMedian'], x['Code'])) if ok else []
    liq10 = [x for x in _elig if not x['LowLiquidity']][:10]
    low10 = [x for x in _elig if x['LowLiquidity']][:10]
    rep = {
        'AsOfDate': asof, 'Status': status, 'Mode': 'RESEARCH_OBSERVATION_ONLY',
        'GeneratedTaipei': datetime.now(TZ).isoformat(timespec='seconds'),
        'UniverseTotal': len(universe), 'ValidatedStocks': len(vetted), 'CoveragePct': round(100 * cov, 2),
        'MinimumCoveragePct': orig.MIN_COVERAGE * 100, 'Exclusions': dict(sorted(stats.items())),
        'ExRightsAdjustedStocks': sum(bool(v['ExRightsDates']) for v in vetted),
        'Audit': dict(sorted(audit_cnt.items())),
        'AuditNotOk': sorted(({'Code': c, 'Reason': r[0], 'Bars': r[1]} for c, r in audit_res.items() if r[0] != 'ok'),
                             key=lambda x: (ORDER.index(x['Reason']), x['Code'])),
        'Data': {'RollingDays': len(days), 'RollingRange': [days[0], days[-1]], 'SyncedDays': new_days,
                 'RepairedRefDays': repair_days, 'BackfilledDays': sorted(set(gap_days) - set(closed)), 'ClosedDays': closed,
                 'Source': 'TWSE MI_INDEX / TPEx dailyQuotes official daily OHLCV (unadjusted; TPEx volume incl. odd lots)'},
        'Candidates': cands if ok else [],
        'CandidatesLiquid': liq10 if ok else [],
        'CandidatesLowLiquidity': low10 if ok else [],
        'Formula': 'v5/v5_early_warning.py compute()（未修改），輸入為除權息還原價格；研究分 >=7 且量比 >=1；20 日均成交值 < 5,000 萬標記為流動性低（不排除）',
        'Limitations': '研究觀察名單，非買進訊號；非 Golden Entry、Stage1、Stage2。回測僅 20～60 日有小幅超額報酬，且優勢主要來自成交值較小的股票。',
    }
    if not ok:
        rep['BlockReason'] = f'合格率 {rep["CoveragePct"]}%（需 ≥ {orig.MIN_COVERAGE * 100:.0f}% 且至少 100 檔）'

    # 全部算完才寫檔（先寫暫存檔再替換）
    writes = {OUT_JSON: (json.dumps(rep, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
              OUT_HTML: render(rep).encode('utf-8'),
              FIRST_SEEN: (json.dumps(dict(sorted(first_seen.items())), indent=0) + '\n').encode('utf-8')}
    for d, rows in fetched.items():
        writes[ROLL / f'{d}.csv.gz'] = day_bytes(rows)
    writes[SCORES_OUT] = (json.dumps(scores_payload(rep, vetted, ok), ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    writes[ROLL_INDEX] = (json.dumps({'Days': days, 'Last': days[-1]}) + '\n').encode('utf-8')
    writes[DAYTRADE_OUT] = (json.dumps(daytrade_watch(days, day_rows, universe, eod), ensure_ascii=False, indent=1) + '\n').encode('utf-8')
    if target in fetched:  # 只在取得新的官方交易日時更新首頁行情檔
        writes[EOD_OUT] = (json.dumps(eod_payload(target, list(fetched[target].values())), ensure_ascii=False, indent=1) + '\n').encode('utf-8')
    for p, content in writes.items():
        tmp = p.with_name(p.name + '.new')
        tmp.write_bytes(content)
        tmp.replace(p)
    for p in files:
        if p.name[:10] not in set(days):
            p.unlink()

    print(f'官方日期 {asof} | 合格 {len(vetted)}/{len(universe)} ({100 * cov:.1f}%) | 品質檢查 {dict(audit_cnt)} | 狀態 {status}')
    for i, x in enumerate(rep['Candidates'], 1):
        print(f'{i:2d}. {x["Code"]} {x["Name"]} | 研究分 {x["ResearchScore"]} | RS20 {x["RS20vsMedian"]:+.2f} | 量比 {x["VolumeRatio5"]}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('YHTIS V5 ABORT（已發布檔案未更動）: ' + type(e).__name__ + ': ' + str(e), file=sys.stderr)
        sys.exit(2)
