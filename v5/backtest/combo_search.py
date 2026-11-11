#!/usr/bin/env python3
"""多因子組合 → 上漲機率表（規則事先固定）。
因子（收盤時已知）：NH 收盤>=52週高95%；REV 營收轉強（75日內有效，同 S2）；TR 趨勢 收盤>MA20>MA60；
RS 60日漲幅>同日中位數；LU 前20日內漲停過；SQ 布林帶寬<近60日中位數×0.85。
宇宙：20日均成交值>=5,000萬、上市滿260日。結果：隔日開盤買，持有 20／60 日（開盤出），扣成本 0.485%。
每種組合（2^6=64）分期統計；建表 2019–2024，檢驗 2025–2026。"""
import importlib.util, os, glob, gc, json
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('b', os.path.join(HERE, 'backtest_v5b.py')); b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
TW = os.path.expanduser('~/mnt/tw_screener/data'); COST = 0.00485
a = b.load()
for c in ['open', 'high', 'low', 'close', 'ref_price', 'volume']: a[c] = a[c].astype('float32')
key = a.code; g = a.groupby('code', observed=True, sort=False)
pc = g.close.shift(1); ref = a.ref_price.where(a.ref_price.notna(), pc); rf = a.ref_price / pc
f = rf.where(a.ref_price.notna() & (pc > 0) & ((rf - 1).abs() > 0.001), 1.0).clip(0.2, 5)
cumf = f.groupby(key, observed=True).cumprod()
a['O'] = (a.open / cumf).astype('float32'); a['C'] = (a.close / cumf).astype('float32'); Hh = (a.high / cumf).astype('float32'); a['Hd'] = Hh; a['Ld'] = (a.low / cumf).astype('float32')
lu = (a.close / ref - 1) >= 0.095
del pc, rf, f, cumf; gc.collect()
T = lambda s, fn: s.groupby(key, observed=True).transform(fn)
a['NH'] = a.C >= 0.95 * T(Hh, lambda s: s.rolling(252, min_periods=200).max()); del Hh
ma20 = T(a.C, lambda s: s.rolling(20, min_periods=20).mean()); ma60 = T(a.C, lambda s: s.rolling(60, min_periods=60).mean())
a['TR'] = (a.C > ma20) & (ma20 > ma60)
sd = T(a.C, lambda s: s.rolling(20, min_periods=20).std()); bbw = (4 * sd / ma20).astype('float32'); del sd, ma20, ma60
a['SQ'] = bbw < 0.85 * T(bbw, lambda s: s.rolling(60, min_periods=40).median()); del bbw
a['LU'] = T(lu.astype('float32'), lambda s: s.rolling(20, min_periods=1).sum()) > 0; del lu
a['r60'] = (a.C / a.C.groupby(key, observed=True).shift(60) - 1).astype('float32')
a['val20'] = T((a.close * a.volume).astype('float32'), lambda s: s.rolling(20, min_periods=20).mean())
a['nbar'] = g.cumcount() + 1
O1 = g.O.shift(-1); a['gapok'] = (g.open.shift(-1) / a.close - 1) < 0.095
for H in (20, 60): a[f'f{H}'] = (g.O.shift(-1 - H) / O1 - 1 - COST).astype('float32')
del O1; gc.collect()
a['code'] = a.code.astype(str)
rv = pd.concat([pd.read_csv(p, dtype={'code': str, 'ym': str}, usecols=['ym', 'code', 'rev', 'yoy']) for p in glob.glob(os.path.join(TW, 'revenue', '*.csv'))])
rv['rev'] = pd.to_numeric(rv.rev, errors='coerce'); rv['yoy'] = pd.to_numeric(rv.yoy, errors='coerce')
rv = rv.dropna(subset=['rev']).drop_duplicates(['code', 'ym']).sort_values(['code', 'ym'])
gg = rv.groupby('code'); rv['y3'] = gg.yoy.transform(lambda s: s.rolling(3, min_periods=3).mean())
rv['REV'] = (rv.y3 > rv.groupby('code').y3.shift(3)) & (rv.rev >= gg.rev.transform(lambda s: s.rolling(12, min_periods=12).max()))
rv['reff'] = pd.to_datetime(rv.ym, format='%Y%m') + pd.offsets.MonthBegin(1) + pd.Timedelta(days=10)
a = a.sort_values('date')
a = pd.merge_asof(a, rv[['code', 'reff', 'REV']].sort_values('reff'), left_on='date', right_on='reff', by='code', direction='backward', tolerance=pd.Timedelta(days=75))
a['REV'] = a.REV.astype('boolean').fillna(False).astype(bool); del rv; gc.collect()

k = a.code; g = a.groupby(k, observed=True)
pcl = g.C.shift(1); ph = g.Hd.shift(1); pl = g.Ld.shift(1)
up = a.Hd - ph; dn = pl - a.Ld
pdm = up.where((up > dn) & (up > 0), 0.0); ndm = dn.where((dn > up) & (dn > 0), 0.0)
tr = pd.concat([a.Hd - a.Ld, (a.Hd - pcl).abs(), (a.Ld - pcl).abs()], axis=1).max(axis=1)
W = lambda s: s.groupby(k, observed=True).transform(lambda z: z.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
atr = W(tr); pdi = 100 * W(pdm) / atr; ndi = 100 * W(ndm) / atr
dx = 100 * (pdi - ndi).abs() / (pdi + ndi); adx = W(dx)
a['MTM'] = (a.C - g.C.shift(10)) > 0
a['DI'] = pdi > ndi; a['ADX25'] = adx > 25; a['ADXup'] = adx > adx.groupby(k, observed=True).shift(5)
a['ADX40'] = adx > 40
a['DIx'] = a.DI & ~(pdi.groupby(k, observed=True).shift(1) > ndi.groupby(k, observed=True).shift(1))   # +DI 剛上穿 -DI

import itertools
a['DIT'] = a.DI & a.ADX25
el = (a.nbar >= 260) & (a.val20 >= 5e7)
ma20b = a.C > a.C.groupby(k, observed=True).transform(lambda s: s.rolling(20, min_periods=20).mean())
br = a[el].assign(m=ma20b[el]).groupby('date').m.mean()
a['PANIC'] = a.date.map(br) < 0.20
_med = a[el].groupby('date').r60.median(); a['RS'] = (a.r60 > a.date.map(_med)).fillna(False)
x = a[(a.date >= '2019-01-01') & el & a.gapok & a.f60.notna()].copy()
x['x60'] = x.f60 - x.groupby('date').f60.transform('mean')
x['x20'] = x.f20 - x.groupby('date').f20.transform('mean')
FE = {'NH': '近52週高', 'REV': '營收轉強', 'TR': '多頭趨勢', 'RS': '強於大盤', 'LU': '近20日漲停', 'SQ': '波動收斂', 'MTM': 'MTM>0', 'DIT': 'DMI趨勢確認', 'ADXup': 'ADX上升', 'PANIC': '恐慌區'}
B = {f: x[f].fillna(False).to_numpy(bool) for f in FE}
x60 = x.x60.to_numpy(); up = (x.f60 > 0).to_numpy(); x20 = x.x20.to_numpy()
build = (x.date < '2025-01-01').to_numpy(); mon = x.date.dt.to_period('M').astype(str); mi = pd.factorize(mon)[0]; nm_ = mi.max() + 1
def stat(m):
    n = m.sum()
    if n < 3000: return None
    cnt = np.bincount(mi[m], minlength=nm_); sm = np.bincount(mi[m], weights=x60[m], minlength=nm_)
    mm = sm[cnt >= 20] / cnt[cnt >= 20]
    t = mm.mean() / mm.std() * np.sqrt(len(mm)) if len(mm) > 5 else np.nan
    return n, x60[m].mean(), up[m].mean(), t, len(mm), x20[m].mean()
rows = []
for r in range(1, 5):
    for combo in itertools.combinations(FE, r):
        m = np.ones(len(x), bool)
        for f in combo: m &= B[f]
        sb = stat(m & build)
        if sb is None: continue
        sc = stat(m & ~build) if (m & ~build).sum() >= 300 else None
        if sc is None:
            mc = m & ~build; sc = (mc.sum(), x60[mc].mean() if mc.sum() else np.nan, up[mc].mean() if mc.sum() else np.nan, np.nan, 0, np.nan)
        rows.append(('＋'.join(FE[f] for f in combo), r, *sb[:4], sc[0], sc[1], sc[2]))
R = pd.DataFrame(rows, columns=['組合', '條件數', 'n建', '超額60建', '上漲建', 't建', 'n驗', '超額60驗', '上漲驗'])
print('測試組合數', len(R), '｜全體 60日上漲 建/驗', round(up[build].mean(), 3), round(up[~build].mean(), 3))
# 事先固定的挑選規則：建模期 t>=3 且 n>=3000，依建模期 60 日上漲比例排序取前 10
sel = R[(R.t建 >= 3)].sort_values('上漲建', ascending=False).head(10)
pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 60)
f = lambda d: d.assign(**{c: d[c].map(lambda v: f'{v*100:+.2f}%') for c in ('超額60建', '超額60驗')}, **{c: d[c].map(lambda v: f'{v*100:.1f}%') for c in ('上漲建', '上漲驗')}, t建=d.t建.round(1))
print('== 依建模期挑出的前 10 名（之後才看驗證期）=='); print(f(sel).to_string(index=False))
print('== 同規則改依「建模期 60 日超額」排序前 10 =='); print(f(R[R.t建 >= 3].sort_values('超額60建', ascending=False).head(10)).to_string(index=False))
ok = R.dropna(subset=['上漲驗']); ok = ok[ok.n驗 >= 300]
print('建模 vs 驗證 排名相關：上漲', round(ok.上漲建.rank().corr(ok.上漲驗.rank()), 3), '超額', round(ok.超額60建.rank().corr(ok.超額60驗.rank()), 3), '組合數', len(ok))
print('== 對照 ==')
for nm in ('近52週高', '近52週高＋營收轉強', '恐慌區'):
    print(f(R[R.組合 == nm]).to_string(index=False, header=False))
R.to_csv('combo_search.csv', index=False, encoding='utf-8-sig')
