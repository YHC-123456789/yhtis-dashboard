#!/usr/bin/env python3
"""YHTIS 綜合判讀：月營收、消息面、市場廣度、S1／S2 名單、紙上追蹤。

只用標準函式庫。每個來源獨立：抓不到或檢查不過，只影響該來源，其他照常輸出，並在檔案中標明各來源日期。

來源
  月營收：公開資訊觀測站靜態彙總頁 t21sc03（上市 sii／上櫃 otc，與 tw_screener/bigwave/revenue.py 相同格式）。
          檢查：營收、去年同月營收與年增率需互相吻合（≥90% 檔）。
  重大訊息：證交所 OpenAPI t187ap04_L、櫃買 OpenAPI（欄位名稱以關鍵字比對，找不到欄位即不發布並記錄欄位名）。
  新聞標題：Google 新聞 RSS、鉅亨網新聞列表（只存標題、來源、連結、時間）。
  市場廣度：v5_history/rolling 官方行情（自行計算）。

S1／S2 定義與 v5/portfolio_sim.py、exit_test.py、risk_sim.py 回測完全相同：
  REV  = 近 3 個月年增率平均 > 3 個月前的同一數值，且當月營收 ≥ 近 12 個月最高；生效日 = 次月 11 日。
  S1   = REV 生效 7 日內 ＋ 前 5 個交易日（不含當日）漲停過；依前 20 日漲幅排序。
  S2   = REV（生效 75 日內）＋ 收盤 ≥ 近 252 日最高價的 95%；依前 60 日漲幅排序。
  共同 = 近 20 日平均成交值 ≥ 5,000 萬、行情庫內至少 252 個交易日（回測為上市滿 260 日，行情庫只存 260 日故以 252 近似）。
  出場 = 停損 -8%（盤中觸及；跳空則開盤價），不設停利，最長 120 個交易日。部位 = 每筆風險 1% ÷ 8% ＝ 資金 12.5%，最多 8 檔。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import importlib.util
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
ROLL = ROOT / 'v5_history/rolling'
REVD = ROOT / 'v5_history/revenue'
NEWSD = ROOT / 'v5_history/news'
PAPER = ROOT / 'v5_history/paper/signals.csv'
OUT = ROOT / 'v5/intel_latest.json'
CHIP = ROOT / 'v5/chip_latest.json'
TZ = timezone(timedelta(hours=8))
STOCK = re.compile(r'[1-9]\d{3}')
FETCH_PAUSE = 2
KEEP_REV_MONTHS = 18
KEEP_NEWS_DAYS = 30
TOPN = 8
STOP = 0.08
RISK = 0.01
MIN_VAL20 = 5e7
MIN_BARS = 252
MAXH = 120
DT_STATS = ROOT / 'v5/backtest/dt_stats.json'
COMBO = ROOT / 'v5/backtest/combo_prob.json'
FACTOR_NAMES = {'NH': '近52週高', 'REV': '營收轉強', 'TR': '多頭趨勢', 'RS': '強於大盤', 'LU': '近20日漲停', 'SQ': '波動收斂'}
DT_MIN_VAL20 = 3e8  # 當沖名單：20 日均成交值 >= 3 億


def dt_bucket(r, clv, vr):
    R = '<-5%' if r < -0.05 else '-5~-2%' if r < -0.02 else '-2~0%' if r < 0 else '0~2%' if r < 0.02 else '2~5%' if r < 0.05 else '5~9.5%' if r < 0.095 else '漲停'
    P = '收低' if clv <= 0.33 else '收中' if clv <= 0.67 else '收高'
    V = '量縮' if vr <= 1 else '量增' if vr <= 2 else '爆量'
    return f'{R}|{P}|{V}'


def dt_merge(c):
    nb, nc = c['n_build'] or 0, c['n_check'] or 0
    w = lambda k: round(((c[k + '_build'] or 0) * nb + (c[k + '_check'] or 0) * nc) / (nb + nc) * 100, 1)
    return {'N': int(nb + nc), 'Win': w('win'), 'Mean': w('mean'), 'Up2': w('h2'), 'Dn2': w('l2'),
            'MeanBuild': round(c['mean_build'] * 100, 2), 'MeanCheck': round(c['mean_check'] * 100, 2) if c['mean_check'] is not None else None}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


screen = load_module('yhtis_screen_for_intel', ROOT / 'daily_screen/screen.py')
_last = [0.0]


def get(url):
    wait = FETCH_PAUSE - (time.time() - _last[0])
    if wait > 0 and _last[0]:
        time.sleep(wait)
    try:
        return screen.fetch(url)
    finally:
        _last[0] = time.time()


def num(s):
    s = str(s).strip().replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None


def atomic_write(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.new')
    tmp.write_bytes(data)
    tmp.replace(p)


# ---------------- 行情庫 ----------------

def load_rolling(asof=None):
    idx = json.loads((ROLL / 'index.json').read_text(encoding='utf-8'))
    days = [d for d in idx['Days'] if not asof or d <= asof]
    per = {}
    names = {}
    for d in days:
        with gzip.open(ROLL / f'{d}.csv.gz', 'rt', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                c = r['code']
                if not STOCK.fullmatch(c):
                    continue
                o, h, l, cl, v = (num(r[k]) for k in ('open', 'high', 'low', 'close', 'volume'))
                if not cl or cl <= 0:
                    continue
                per.setdefault(c, []).append((d, o or cl, h or cl, l or cl, cl, num(r['ref_price']), v or 0.0))
                names[c] = (r['name'], r['market'])
    return days[-1], days, per, names


def features(rows):
    """回測同口徑：除權息還原（ref/前收），漲停以 ref（缺則前收）判斷。"""
    cum = 1.0
    O, H, L, C, LU, VAL = [], [], [], [], [], []
    prev = None
    for (d, o, h, l, c, ref, v) in rows:
        if prev:
            if ref and prev > 0:
                f = ref / prev
                if abs(f - 1) > 0.001:
                    cum *= min(5.0, max(0.2, f))
            base = ref if ref else prev
            LU.append(c / base - 1 >= 0.095)
        else:
            LU.append(False)
        O.append(o / cum); H.append(h / cum); L.append(l / cum); C.append(c / cum)
        VAL.append(c * v)
        prev = c
    return O, H, L, C, LU, VAL


# ---------------- 月營收 ----------------

REV_ROW = re.compile(r"<tr align=right><td align=center>([0-9A-Za-z]+)</td><td align=left>(.*?)</td>((?:<td[^>]*>[^<]*</td>){8})", re.I | re.S)
REV_IND = re.compile(r"產業別：(.*?)</th>")
REV_CELL = re.compile(r"<td[^>]*>([^<]*)</td>", re.I)


class SourceFail(Exception):
    pass


def parse_revenue(text, ym, market):
    toks = sorted([(m.start(), 'I', m) for m in REV_IND.finditer(text)] + [(m.start(), 'R', m) for m in REV_ROW.finditer(text)], key=lambda x: x[0])
    ind, rows, checked, ok = '', [], 0, 0
    for _, k, m in toks:
        if k == 'I':
            ind = m.group(1).strip()
            continue
        v = [num(x) for x in REV_CELL.findall(m.group(3))]
        if len(v) != 8 or v[0] is None:
            continue
        rev, rev_ly, yoy = v[0], v[2], v[4]
        if rev_ly and rev_ly > 0 and yoy is not None:
            checked += 1
            ok += abs((rev / rev_ly - 1) * 100 - yoy) < 0.6
        rows.append({'ym': str(ym), 'market': market, 'code': m.group(1), 'name': re.sub(r'\s+', '', m.group(2)), 'industry': ind,
                     'rev': rev, 'yoy': '' if yoy is None else yoy})
    if len(rows) < 100:
        raise SourceFail(f'REV_TOO_FEW {market} {ym} rows={len(rows)}')
    if checked and ok < 0.9 * checked:
        raise SourceFail(f'REV_IDENTITY_FAIL {market} {ym} ok={ok}/{checked}')
    return rows


def rev_months_have():
    return sorted(p.name[:6] for p in REVD.glob('*.csv.gz'))


def ym_add(ym, k):
    y, m = int(ym[:4]), int(ym[4:6]) + k
    while m <= 0:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    return f'{y}{m:02d}'


def fetch_revenue(today: date, log):
    """上個月：每月 1～15 日每天重抓（公司陸續申報），之後只在缺檔時抓；更早的月份只補缺檔。"""
    have = set(rev_months_have())
    last = ym_add(f'{today.year}{today.month:02d}', -1)
    todo = []
    for k in range(0, 3):
        ym = ym_add(last, -k)
        if ym not in have or (k == 0 and today.day <= 15):
            todo.append(ym)
    for ym in todo:
        rows = []
        try:
            for mk in ('sii', 'otc'):
                y, m = int(ym[:4]), int(ym[4:6])
                raw = get(f'https://mopsov.twse.com.tw/nas/t21/{mk}/t21sc03_{y - 1911}_{m}_0.html')
                rows += parse_revenue(raw.decode('big5', errors='replace'), ym, mk)
        except Exception as e:
            log.append(f'營收 {ym} 未取得：{type(e).__name__}: {str(e)[:150]}')
            continue
        p = REVD / f'{ym}.csv.gz'
        old = 0
        if p.exists():
            with gzip.open(p, 'rt', encoding='utf-8') as fh:
                old = sum(1 for _ in csv.DictReader(fh))
        if len(rows) < old:
            log.append(f'營收 {ym} 本次 {len(rows)} 檔少於已存 {old} 檔，保留舊檔')
            continue
        buf = []
        w = csv.DictWriter(_Lines(buf), fieldnames=['ym', 'market', 'code', 'name', 'industry', 'rev', 'yoy'])
        w.writeheader(); w.writerows(rows)
        atomic_write(p, gzip.compress(''.join(buf).encode('utf-8'), mtime=0))
        log.append(f'營收 {ym} OK {len(rows)} 檔')
    for ym in rev_months_have()[:-KEEP_REV_MONTHS]:
        (REVD / f'{ym}.csv.gz').unlink()


class _Lines:
    def __init__(self, buf):
        self.buf = buf

    def write(self, s):
        self.buf.append(s)


def load_revenue():
    rev = {}
    meta = {}
    for ym in rev_months_have():
        with gzip.open(REVD / f'{ym}.csv.gz', 'rt', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                rv, yy = num(r['rev']), num(r['yoy'])
                if rv is None:
                    continue
                rev.setdefault(r['code'], {})[ym] = (rv, yy)
                meta[r['code']] = (r['name'], r['industry'])
    return rev, meta


def rev_flags(series):
    """回傳 {ym: (REV, yoy, y3)}；月份需連續（缺月即不判斷，比回測略嚴）。"""
    out = {}
    for ym, (rv, yy) in series.items():
        y3m = [series.get(ym_add(ym, -k)) for k in range(3)]
        y3p = [series.get(ym_add(ym, -k)) for k in range(3, 6)]
        last12 = [series.get(ym_add(ym, -k)) for k in range(12)]
        if any(x is None or x[1] is None for x in y3m + y3p) or any(x is None for x in last12):
            out[ym] = (None, yy, None)
            continue
        y3 = sum(x[1] for x in y3m) / 3
        y3o = sum(x[1] for x in y3p) / 3
        out[ym] = (bool(y3 > y3o and rv >= max(x[0] for x in last12)), yy, round(y3, 2))
    return out


def reff(ym):
    y, m = int(ym[:4]), int(ym[4:6])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(y, m, 11)


# ---------------- 消息面 ----------------

POLICY = {
    '政策支撐': ['國安基金', '護盤', '限空', '禁空', '平盤以下不得', '降息', '降準', '四大基金', '勞退基金進場', '穩定股市'],
    '風險事件': ['關稅', '升息', '斷頭', '追繳', '熔斷', '戰爭', '衝突', '飛彈', '崩跌', '重挫', '暴跌', '制裁', '衰退', '違約'],
    '央行／利率': ['央行', '聯準會', 'Fed', 'FOMC', '利率決策'],
}
ANN_POS = re.compile(r'取得.{0,12}(訂單|合約)|簽訂|得標|創.{0,4}新高|調升|上修|擴產|新廠|庫藏股|現金股利|策略聯盟|技術授權|通過.{0,8}認證|獲利成長|轉虧為盈')
ANN_NEG = re.compile(r'虧損|衰退|減資|違約|停工|停產|火災|爆炸|裁罰|罰鍰|下修|跳票|退票|訴訟|資遣|掏空|重編|無法表示意見|保留意見|終止.{0,6}(合約|合作)')
ANN_RISK = re.compile(r'注意交易|處置|變更交易方法|全額交割|暫停交易')


def tone(subject):
    if ANN_RISK.search(subject):
        return '注意'
    p, n = bool(ANN_POS.search(subject)), bool(ANN_NEG.search(subject))
    return '正面' if p and not n else '負面' if n and not p else '中性'


def roc_date(s):
    s = re.sub(r'\D', '', str(s))
    if len(s) == 7:
        return f'{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}'
    if len(s) == 8:
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return ''


def pick(keys, *pats):
    for pat in pats:
        for k in keys:
            if re.search(pat, k.replace(' ', ''), re.I):
                return k
    return None


ANN_URLS = [('TWSE', 'https://openapi.twse.com.tw/v1/opendata/t187ap04_L'),
            ('TPEx', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O'),  # 櫃買 OpenAPI 公司資料一律 mopsfin_ 前綴（t187ap03/05 已實測）
            ('TPEx', 'https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap04_O'),
            ('TPEx', 'https://www.tpex.org.tw/openapi/v1/t187ap04_O')]


def parse_announcements(raw, market):
    data = json.loads(raw.decode('utf-8-sig'))
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise SourceFail(f'ANN_EMPTY {market}')
    keys = list(data[0].keys())
    kc = pick(keys, r'公司代號', r'CompanyCode', r'SecuritiesCompanyCode', r'^Code$', r'代號')
    ks = pick(keys, r'主旨', r'Subject', r'標題', r'Title')
    kd = pick(keys, r'發言日期', r'SpeechDate', r'^Date$', r'出表日期', r'日期')
    kt = pick(keys, r'發言時間', r'SpeechTime', r'時間', r'Time')
    if not (kc and ks and kd):
        raise SourceFail(f'ANN_FIELDS {market} 欄位={keys[:12]}')
    out = []
    for r in data:
        c = str(r.get(kc, '')).strip()
        sub = re.sub(r'\s+', ' ', str(r.get(ks, ''))).strip()
        d = roc_date(r.get(kd, ''))
        if not STOCK.fullmatch(c) or not sub or not d:
            continue
        t = re.sub(r'\D', '', str(r.get(kt, ''))) if kt else ''
        out.append({'Date': d, 'Time': f'{t[:2]}:{t[2:4]}' if len(t) >= 4 else '', 'Code': c, 'Market': market,
                    'Subject': sub[:160], 'Tone': tone(sub)})
    if not out:
        raise SourceFail(f'ANN_NO_ROWS {market} 欄位={keys[:12]}')
    return out


NEWS_URLS = [
    ('Google 新聞', 'https://news.google.com/rss/search?q=' + quote('台股 when:2d') + '&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'),
    ('Google 新聞', 'https://news.google.com/rss/search?q=' + quote('(國安基金 OR 央行 OR 限空 OR 關稅) 股市 when:2d') + '&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'),
    ('鉅亨網', 'https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=60'),
]


def parse_rss(raw):
    t = raw.decode('utf-8', errors='replace')
    items = re.findall(r'<item>(.*?)</item>', t, re.S)
    out = []
    for it in items:
        def tag(n):
            m = re.search(rf'<{n}[^>]*>(.*?)</{n}>', it, re.S)
            return html.unescape(re.sub(r'<!\[CDATA\[|\]\]>', '', m.group(1))).strip() if m else ''
        title, link, pub, src = tag('title'), tag('link'), tag('pubDate'), tag('source')
        if src and title.endswith(' - ' + src):
            title = title[:-len(src) - 3]
        try:
            ts = parsedate_to_datetime(pub).astimezone(TZ).isoformat(timespec='minutes')
        except Exception:
            ts = ''
        if title and link.startswith('http'):
            out.append({'Title': title[:140], 'Link': link, 'Source': src, 'Time': ts})
    if not out:
        raise SourceFail('RSS_NO_ITEMS')
    return out


def parse_cnyes(raw):
    data = json.loads(raw.decode('utf-8'))
    lst = None

    def walk(x, depth=0):
        nonlocal lst
        if lst is not None or depth > 4:
            return
        if isinstance(x, list) and x and isinstance(x[0], dict) and 'title' in x[0]:
            lst = x
            return
        if isinstance(x, dict):
            for v in x.values():
                walk(v, depth + 1)
    walk(data)
    if not lst:
        raise SourceFail('CNYES_NO_LIST')
    out = []
    for r in lst:
        nid, title = r.get('newsId'), str(r.get('title', '')).strip()
        ts = r.get('publishAt')
        try:
            ts = datetime.fromtimestamp(int(ts), TZ).isoformat(timespec='minutes')
        except Exception:
            ts = ''
        if nid and title:
            out.append({'Title': html.unescape(title)[:140], 'Link': f'https://news.cnyes.com/news/id/{nid}', 'Source': '鉅亨網', 'Time': ts})
    if not out:
        raise SourceFail('CNYES_NO_ITEMS')
    return out


def tag_headline(h, name_index):
    tags = [k for k, words in POLICY.items() if any(w.lower() in h['Title'].lower() for w in words)]
    codes = sorted({c for nm, c in name_index if nm in h['Title']} | set(re.findall(r'[（(]([1-9]\d{3})[)）]', h['Title'])))
    return tags, codes[:6]


def news_path(d):
    return NEWSD / f'{d}.json.gz'


def load_news(d):
    p = news_path(d)
    return json.loads(gzip.decompress(p.read_bytes())) if p.exists() else {'Announcements': [], 'Headlines': []}


def fetch_news(asof_now: str, log, status):
    store = load_news(asof_now)
    ann, n_ok = [], 0
    got_market = set()
    for market, url in ANN_URLS:
        if market in got_market:
            continue
        try:
            ann += parse_announcements(get(url), market)
            got_market.add(market)
            n_ok += 1
        except Exception as e:
            log.append(f'重大訊息 {market} 未取得：{type(e).__name__}: {str(e)[:200]}')
    if ann:
        seen = {(a['Code'], a['Date'], a['Subject']) for a in store['Announcements']}
        store['Announcements'] += [a for a in ann if (a['Code'], a['Date'], a['Subject']) not in seen]
        status['announcements'] = {'Markets': sorted(got_market), 'Fetched': len(ann)}
        log.append(f'重大訊息 OK {len(ann)} 則（{"、".join(sorted(got_market))}）')
    heads = []
    for src, url in NEWS_URLS:
        try:
            raw = get(url)
            heads += parse_cnyes(raw) if 'cnyes' in url else parse_rss(raw)
        except Exception as e:
            log.append(f'新聞 {src} 未取得：{type(e).__name__}: {str(e)[:160]}')
    if heads:
        seen = {h['Title'] for h in store['Headlines']}
        for h in heads:
            if h['Title'] not in seen:
                store['Headlines'].append(h)
                seen.add(h['Title'])
        status['headlines'] = {'Fetched': len(heads)}
        log.append(f'新聞標題 OK {len(heads)} 則')
    if ann or heads:
        atomic_write(news_path(asof_now), gzip.compress(json.dumps(store, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    for p in sorted(NEWSD.glob('*.json.gz'))[:-KEEP_NEWS_DAYS]:
        p.unlink()


# ---------------- 紙上追蹤 ----------------

PAPER_FIELDS = ['date', 'strategy', 'rank', 'code', 'name', 'close']


def paper_record(asof, lists):
    rows = []
    if PAPER.exists():
        with PAPER.open(encoding='utf-8') as fh:
            rows = list(csv.DictReader(fh))
    rows = [r for r in rows if r['date'] != asof]
    for strat, lst in lists.items():
        for i, s in enumerate(lst, 1):
            rows.append({'date': asof, 'strategy': strat, 'rank': i, 'code': s['Code'], 'name': s['Name'], 'close': s['Close']})
    buf = []
    w = csv.DictWriter(_Lines(buf), fieldnames=PAPER_FIELDS)
    w.writeheader(); w.writerows(sorted(rows, key=lambda r: (r['date'], r['strategy'], int(r['rank']))))
    atomic_write(PAPER, ''.join(buf).encode('utf-8'))
    return rows


def paper_eval(rows, days, feat):
    """隔日開盤買（漲 ≥9.5% 視為買不到）、盤中觸及 -8% 停損（跳空以開盤）、最長 120 日；同策略同股 20 日內只算第一次。"""
    pos = {d: i for i, d in enumerate(days)}
    out = {}
    for strat in ('S1', 'S2'):
        last_sig = {}
        trades = []
        for r in sorted((x for x in rows if x['strategy'] == strat), key=lambda x: x['date']):
            i = pos.get(r['date'])
            c = r['code']
            if i is None or c not in feat:
                continue
            if c in last_sig and i - last_sig[c] < 20:
                continue
            last_sig[c] = i
            F = feat[c]
            di = F['idx'].get(r['date'])
            t = {'Date': r['date'], 'Code': c, 'Name': r['name']}
            if di is None or di + 1 >= len(F['O']):
                t['Status'] = '待進場'
                trades.append(t)
                continue
            e = di + 1
            if F['O'][e] / F['C'][di] - 1 >= 0.095:
                t['Status'] = '開盤漲停附近買不到'
                trades.append(t)
                continue
            px = F['O'][e]
            stop = px * (1 - STOP)
            t.update({'Entry': F['dates'][e]})
            res = None
            for k in range(e, min(e + MAXH, len(F['O']))):
                if F['L'][k] <= stop:
                    res = (min(F['O'][k], stop) / px - 1, F['dates'][k], '停損')
                    break
            if res is None and e + MAXH < len(F['O']):
                res = (F['O'][e + MAXH] / px - 1, F['dates'][e + MAXH], '滿 120 日')
            if res:
                t.update({'Status': res[2], 'Exit': res[1], 'Ret': round(res[0] * 100, 2)})
            else:
                t.update({'Status': '持有中', 'Ret': round((F['C'][-1] / px - 1) * 100, 2)})
            trades.append(t)
        done = [t for t in trades if t['Status'] in ('停損', '滿 120 日')]
        held = [t for t in trades if t['Status'] == '持有中']
        out[strat] = {'Signals': len(trades), 'Closed': len(done), 'Holding': len(held),
                      'ClosedWin': sum(t['Ret'] > 0 for t in done),
                      'ClosedAvg': round(sum(t['Ret'] for t in done) / len(done), 2) if done else None,
                      'Trades': trades[-40:]}
    return out


# ---------------- 主流程 ----------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--today', help='測試用：台北日期 YYYY-MM-DD')
    ap.add_argument('--asof', help='測試用：只用此日以前的行情（不寫紙上追蹤）')
    a = ap.parse_args(argv)
    now = datetime.now(TZ)
    today = date.fromisoformat(a.today) if a.today else now.date()
    log, status = [], {}
    asof, days, per, names = load_rolling(a.asof)
    if not a.offline:
        fetch_revenue(today, log)
        fetch_news(today.isoformat(), log, status)

    # 價格特徵與廣度
    feat = {}
    dpos = {d: i for i, d in enumerate(days)}
    breadth = {d: [0, 0, 0, 0, 0, 0] for d in days}  # up, down, n, above_ma20, n_ma, limit_up
    for c, rows in per.items():
        O, H, L, C, LU, VAL = features(rows)
        dts = [r[0] for r in rows]
        feat[c] = {'dates': dts, 'idx': {d: i for i, d in enumerate(dts)}, 'O': O, 'H': H, 'L': L, 'C': C, 'LU': LU, 'VAL': VAL}
        for i in range(1, len(rows)):
            d = dts[i]
            j = dpos.get(d)
            if not j or dts[i - 1] != days[j - 1]:
                continue
            ch = C[i] / C[i - 1] - 1
            b = breadth[d]
            b[2] += 1
            b[0] += ch > 0.0005
            b[1] += ch < -0.0005
            b[5] += LU[i]
            if i >= 19:
                b[4] += 1
                b[3] += C[i] > sum(C[i - 19:i + 1]) / 20
    up_series = [(d, b[0] / b[2]) for d, b in breadth.items() if b[2] > 500]
    ma_series = [(d, b[3] / b[4]) for d, b in breadth.items() if b[4] > 500]
    bt = breadth[asof]

    def pct_rank(series, v):
        vals = [x for _, x in series]
        return round(sum(x <= v for x in vals) / len(vals) * 100, 1) if vals else None
    up_pct = bt[0] / bt[2] if bt[2] else None
    ma_pct = bt[3] / bt[4] if bt[4] else None
    ew = None
    rets = []
    for c, F in feat.items():
        i = F['idx'].get(asof)
        if i and F['dates'][i - 1] == days[-2]:
            rets.append(F['C'][i] / F['C'][i - 1] - 1)
    if rets:
        ew = round(sum(rets) / len(rets) * 100, 2)

    # 營收旗標
    rev, rev_meta = load_revenue()
    flags = {c: rev_flags(s) for c, s in rev.items()}
    asof_d = date.fromisoformat(asof)
    rev_now = {}
    for c, fl in flags.items():
        eff = [(reff(ym), ym) for ym in fl if reff(ym) <= asof_d]
        if not eff:
            continue
        rd, ym = max(eff)
        age = (asof_d - rd).days
        if age > 75:
            continue
        r, yy, y3 = fl[ym]
        rev_now[c] = {'YM': ym, 'REV': r, 'REVW': bool(r and age <= 7), 'Yoy': yy, 'Y3': y3, 'Eff': rd.isoformat()}
    latest_ym = rev_months_have()[-1] if rev_months_have() else None

    # S1／S2
    s1, s2 = [], []
    for c, F in feat.items():
        i = F['idx'].get(asof)
        if i is None or i != len(F['C']) - 1 or len(F['C']) < MIN_BARS:
            continue
        val20 = sum(F['VAL'][-20:]) / 20
        if val20 < MIN_VAL20:
            continue
        rv = rev_now.get(c)
        if not rv or not rv['REV']:
            continue
        C = F['C']
        r20 = C[-1] / C[-21] - 1 if len(C) > 20 else None
        r60 = C[-1] / C[-61] - 1 if len(C) > 60 else None
        hi = max(F['H'][-252:])
        nh = C[-1] >= 0.95 * hi
        lu5 = any(F['LU'][-6:-1])
        close = per[c][-1][4]
        item = {'Code': c, 'Name': names[c][0], 'Market': names[c][1], 'Close': close, 'StopRef': round(close * (1 - STOP), 2),
                'R20': round(r20 * 100, 1) if r20 is not None else None, 'R60': round(r60 * 100, 1) if r60 is not None else None,
                'Val20': round(val20 / 1e8, 2), 'RevYM': rv['YM'], 'Yoy': rv['Yoy'], 'Y3': rv['Y3'], 'NearHigh': round(C[-1] / hi * 100, 1)}
        if rv['REVW'] and lu5 and r20 is not None:
            s1.append(item)
        if nh and r60 is not None:
            s2.append(item)
    s1.sort(key=lambda x: (-x['R20'], x['Code']))
    s2.sort(key=lambda x: (-x['R60'], x['Code']))
    s1_all, s2_all = len(s1), len(s2)
    s1, s2 = s1[:TOPN], s2[:TOPN]

    # 消息面整理（近 5 個自然日）
    name_index = [(v[0], c) for c, v in names.items() if len(v[0]) >= 3]
    ann_all, heads_all = [], []
    for k in range(5):
        st = load_news((today - timedelta(days=k)).isoformat())
        ann_all += st['Announcements']
        heads_all += st['Headlines']
    seen = set()
    heads = []
    for h in sorted(heads_all, key=lambda x: x.get('Time') or '', reverse=True):
        if h['Title'] in seen:
            continue
        seen.add(h['Title'])
        tg, codes = tag_headline(h, name_index)
        heads.append({**h, 'Tags': tg, 'Codes': codes})
    heads = heads[:120]
    policy = {}
    for h in heads:
        for t in h['Tags']:
            policy.setdefault(t, []).append(h['Title'])
    ann_all.sort(key=lambda x: (x['Date'], x['Time']), reverse=True)
    for x in ann_all:
        x['Name'] = names.get(x['Code'], (x.get('Name', ''),))[0]

    chip = {}
    try:
        cj = json.loads(CHIP.read_text(encoding='utf-8'))
        chip = cj.get('Stocks', {})
    except Exception:
        pass

    def chip_brief(c):
        s = chip.get(c) or {}
        return {k: s[k] for k in ('Inst3_5', 'Trust5', 'Foreign5', 'TrustStreak', 'MarginChg', 'Big1000Chg') if k in s}

    for lst in (s1, s2):
        for s in lst:
            s['Chip'] = chip_brief(s['Code'])
            s['Ann'] = [{k: x[k] for k in ('Date', 'Subject', 'Tone')} for x in ann_all if x['Code'] == s['Code']][:3]
            s['News'] = [h['Title'] for h in heads if s['Code'] in h['Codes']][:3]

    stocks = {}
    for c in names:
        e = {}
        if c in rev_now:
            e['Rev'] = rev_now[c]
        a_ = [{k: x[k] for k in ('Date', 'Time', 'Subject', 'Tone')} for x in ann_all if x['Code'] == c][:5]
        if a_:
            e['Ann'] = a_
        n_ = [{k: h[k] for k in ('Title', 'Link', 'Source', 'Time')} for h in heads if c in h['Codes']][:5]
        if n_:
            e['News'] = n_
        if e:
            stocks[c] = e

    # 多因子組合上漲機率（表由 v5/backtest/combo_prob.py 以 2019–2026 官方日資料建立）
    combo = None
    try:
        CB = json.loads(COMBO.read_text(encoding='utf-8'))
        F_ = CB['factors']
        elig = {}
        for c, F in feat.items():
            if F['dates'][-1] != asof or len(F['C']) < MIN_BARS:
                continue
            val20 = sum(F['VAL'][-20:]) / 20
            if val20 < MIN_VAL20:
                continue
            C = F['C']
            if len(C) < 121:
                continue
            ma = lambda k, n: sum(C[k - n + 1:k + 1]) / n
            ma20, ma60 = ma(len(C) - 1, 20), ma(len(C) - 1, 60)
            bb = []
            for k in range(len(C) - 60, len(C)):
                w = C[k - 19:k + 1]
                m = sum(w) / 20
                sd = (sum((v - m) ** 2 for v in w) / 19) ** 0.5
                bb.append(4 * sd / m)
            med = sorted(bb)[len(bb) // 2] if len(bb) % 2 else sum(sorted(bb)[len(bb) // 2 - 1:len(bb) // 2 + 1]) / 2
            rv = rev_now.get(c)
            elig[c] = {'NH': C[-1] >= 0.95 * max(F['H'][-252:]), 'REV': bool(rv and rv['REV']), 'TR': C[-1] > ma20 > ma60,
                       'r60': C[-1] / C[-61] - 1, 'LU': any(F['LU'][-20:]), 'SQ': bb[-1] < 0.85 * med, 'val20': val20}
        if elig:
            rs = sorted(v['r60'] for v in elig.values())
            n = len(rs)
            med60 = rs[n // 2] if n % 2 else (rs[n // 2 - 1] + rs[n // 2]) / 2
            rows_c = []
            for c, v in elig.items():
                v['RS'] = v['r60'] > med60
                cell = sum((1 << i) for i, f in enumerate(F_) if v[f])
                st = CB['cells'].get(str(cell))
                if not st or (st.get('n60_build') or 0) < 500:
                    continue
                nb, nc = st['n60_build'] or 0, st.get('n60_check') or 0
                cmb = lambda k: ((st[k + '_build'] or 0) * nb + (st.get(k + '_check') or 0) * nc) / (nb + nc)
                item = {'Code': c, 'Name': names[c][0], 'Close': per[c][-1][4], 'Factors': [FACTOR_NAMES[f] for f in F_ if v[f]],
                        'P20': round(cmb('up20') * 100, 1), 'P60': round(cmb('up60') * 100, 1), 'M20': round(cmb('m20') * 100, 1), 'M60': round(cmb('m60') * 100, 1),
                        'P60Check': round(st['up60_check'] * 100, 1) if st.get('up60_check') is not None else None,
                        'N': int(nb + nc), 'R60': round(v['r60'] * 100, 1), 'Val20': round(v['val20'] / 1e8, 2), 'StopRef': round(per[c][-1][4] * (1 - STOP), 2)}
                rows_c.append(item)
            rows_c.sort(key=lambda x: (-x['P60'], -x['M60'], -x['R60'], x['Code']))
            for i, x in enumerate(rows_c, 1):
                x['Rank'] = i
            base = CB['base']
            combo = {'Top': rows_c[:30], 'Count': len(rows_c), 'All': {x['Code']: {k: x[k] for k in ('Rank', 'P20', 'P60', 'M60', 'Factors', 'N')} for x in rows_c},
                     'Base': {'P20': round((base['build'][0] + base['check'][0]) / 2 * 100, 1), 'P60': round((base['build'][2] + base['check'][2]) / 2 * 100, 1)},
                     'Rule': '6 個因子（近52週高、營收轉強、多頭趨勢、強於大盤、近20日漲停、波動收斂）的 64 種組合，各自統計 2019–2026 隔日開盤買、持有 20／60 日（扣成本 0.485%）的上漲比例；宇宙為 20 日均成交值 ≥ 5,000 萬。',
                     'Validation': '用 2019–2024 建表、2025–2026 檢驗：組合排名相關 0.72；建表排名前 20% 的組合，檢驗期 60 日上漲比例 57%、平均 +10.3%，後 20% 為 46%、+5.9%。同一股票連續多日重複計入，實際獨立樣本較少；機率是同組合的歷史頻率，不是個股保證。'}
    except Exception as e:
        log.append(f'多因子排行未產生：{type(e).__name__}: {str(e)[:120]}')

    # 當沖統計（隔日 開盤買→收盤賣，扣 0.33%；型態表由 v5/backtest/dt_stats.py 以 2019–2026 官方日資料建立）
    daytrade = None
    try:
        DS = json.loads(DT_STATS.read_text(encoding='utf-8'))
        cells = DS['cells']
        rows_dt = []
        for c, rows in per.items():
            if rows[-1][0] != asof or len(rows) < 21:
                continue
            d, o, h, l, cl, ref, v = rows[-1]
            base = ref or rows[-2][4]
            vm = sum(r[6] for r in rows[-21:-1]) / 20
            val20 = sum(r[4] * r[6] for r in rows[-20:]) / 20
            if not base or h <= l or vm <= 0 or val20 < DT_MIN_VAL20:
                continue
            r_, clv, vr = cl / base - 1, (cl - l) / (h - l), v / vm
            k = dt_bucket(r_, clv, vr)
            if k not in cells:
                continue
            st = dt_merge(cells[k])
            rows_dt.append({'Code': c, 'Name': names[c][0], 'Close': cl, 'Chg': round(r_ * 100, 2), 'VolRatio': round(vr, 2),
                            'Val20': round(val20 / 1e8, 1), 'Pattern': k.replace('|', '・'), **st})
        rows_dt.sort(key=lambda x: (-x['Up2'], -x['Mean'], x['Code']))
        mk = None
        if up_pct is not None:
            mb = '<20%' if up_pct < 0.2 else '20-40%' if up_pct < 0.4 else '40-60%' if up_pct < 0.6 else '60-80%' if up_pct < 0.8 else '>=80%'
            mv = DS['market'][mb]
            mk = {'Bucket': mb, 'Win': round(mv['win'] * 100, 1), 'Mean': round(mv['mean'] * 100, 2), 'Up2': round(mv['h2'] * 100, 1),
                  'Dn2': round(mv['l2'] * 100, 1), 'N': int(mv['n'])}
        short = [x for x in rows_dt if x['Pattern'].startswith('漲停・收高') and x['Pattern'].split('・')[2] in ('量增', '爆量')]
        daytrade = {'Market': mk, 'Top': rows_dt[:12], 'Count': len(rows_dt), 'LimitUpHigh': short[:12],
                    'Rule': '隔日開盤買、收盤賣，扣成本 0.33%；統計 2019–2026 同型態（今日漲跌×收盤位置×量比）、20 日均成交值 ≥ 1 億的歷史結果。',
                    'Finding': '50 種型態扣成本後沒有任何一種「開盤買、收盤賣」兩段期間都賺；全市場平均約 -0.6%／筆，收紅機率約 36%。'
                               '唯一兩段都為正的是反方向：今日漲停、收在高點、帶量的股票，隔日開盤先賣、收盤買回，平均約 +0.3%～+0.5%（未計借券成本與限空措施）。'}
    except Exception as e:
        log.append(f'當沖統計未產生：{type(e).__name__}: {str(e)[:120]}')

    # 市場狀態（透明規則，未回測）
    if ma_pct is None:
        state = '資料不足'
    elif ma_pct < 0.30:
        state = '偏弱'
    elif ma_pct >= 0.60:
        state = '偏多'
    else:
        state = '中性'

    window = None
    if latest_ym:
        rd = reff(latest_ym)
        window = {'RevYM': latest_ym, 'From': rd.isoformat(), 'To': (rd + timedelta(days=7)).isoformat(), 'Active': 0 <= (asof_d - rd).days <= 7}
    paper_rows = paper_record(asof, {'S1': s1, 'S2': s2}) if not a.asof else []
    paper = paper_eval(paper_rows, days, feat)
    ann_today = [x for x in ann_all if x['Date'] == asof or x['Date'] == today.isoformat()]

    rep = {
        'AsOfDate': asof, 'GeneratedTaipei': now.isoformat(timespec='seconds'), 'Sources': status,
        'Market': {'State': state, 'StateRule': '站上 20 日均線股票比例：<30% 偏弱、30～60% 中性、≥60% 偏多（描述用，未回測）',
                   'UpPct': round(up_pct * 100, 1) if up_pct is not None else None, 'UpPctRank': pct_rank(up_series, up_pct) if up_pct is not None else None,
                   'AboveMA20Pct': round(ma_pct * 100, 1) if ma_pct is not None else None, 'AboveMA20Rank': pct_rank(ma_series, ma_pct) if ma_pct is not None else None,
                   'Up': bt[0], 'Down': bt[1], 'N': bt[2], 'LimitUp': bt[5], 'EqualWeightRet': ew, 'RankDays': len(up_series),
                   'Series': [[d, round(u * 100, 1)] for d, u in up_series[-60:]],
                   'Policy': {k: v[:6] for k, v in policy.items()}},
        'Revenue': {'LatestYM': latest_ym, 'Months': len(rev_months_have()), 'S1Window': window, 'Flagged': sum(1 for v in rev_now.values() if v['REV'])},
        'Rules': {'S1': '營收剛轉強（近 3 月年增率平均上升＋營收創 12 月新高，次月 11 日起 7 日內）＋前 5 日漲停過；依 20 日漲幅排序',
                  'S2': '營收轉強（75 日內有效）＋收盤 ≥ 52 週高 95%；依 60 日漲幅排序',
                  'Common': '20 日均成交值 ≥ 5,000 萬；隔日開盤買，開盤漲 ≥9.5% 不買；停損 -8%（盤中觸及），不設停利，最長 120 日；每筆風險 1% → 部位約資金 12.5%，最多 8 檔',
                  'Backtest': '2019～2026 回測（扣成本 0.485%）：S1 年化約 +44%、最大回撤約 -40%；S2 年化約 +24～29%、回撤約 -32%；勝率約 20～26%，靠少數大賺。S1 由大量搜尋挑出，需前瞻驗證。'},
        'S1': s1, 'S2': s2, 'S1Count': s1_all, 'S2Count': s2_all,
        'Announcements': ann_today[:200], 'Headlines': heads[:60],
        'Paper': paper,
        'DayTrade': daytrade,
        'Combo': combo,
        'Note': '消息面為關鍵字判讀，未經回測，僅供參考；名單非保證獲利。',
        'Log': log, 'Stocks': stocks,
    }
    atomic_write(OUT, (json.dumps(rep, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
    for x in log:
        print(x)
    print(f'INTEL OK {asof} 市場={state} 上漲比例={rep["Market"]["UpPct"]}%(排名 {rep["Market"]["UpPctRank"]}) '
          f'S1={s1_all} S2={s2_all} 營收最新={latest_ym} 重大訊息={len(ann_today)} 新聞={len(heads)}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('YHTIS INTEL ABORT（已發布檔案未更動）: ' + type(e).__name__ + ': ' + str(e), file=sys.stderr)
        sys.exit(2)
