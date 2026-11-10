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
a['O'] = (a.open / cumf).astype('float32'); a['C'] = (a.close / cumf).astype('float32'); Hh = (a.high / cumf).astype('float32')
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
x = a[(a.date >= '2019-01-01') & (a.nbar >= 260) & (a.val20 >= 5e7) & a.gapok & a.r60.notna()][['date', 'NH', 'REV', 'TR', 'r60', 'LU', 'SQ', 'f20', 'f60']].copy(); del a; gc.collect()
x['RS'] = x.r60 > x.groupby('date').r60.transform('median')
F = ['NH', 'REV', 'TR', 'RS', 'LU', 'SQ']
x['cell'] = sum(x[f].astype(int) * (1 << i) for i, f in enumerate(F))
x['per'] = np.where(x.date < '2025-01-01', 'build', 'check')
def st(y, H):
    z = y[f'f{H}'].dropna(); return len(z), (z > 0).mean() if len(z) else np.nan, z.mean() if len(z) else np.nan
rows = []
for (c, p), y in x.groupby(['cell', 'per']):
    n20, u20, m20 = st(y, 20); n60, u60, m60 = st(y, 60)
    rows.append(dict(cell=int(c), per=p, n=n20, up20=u20, m20=m20, n60=n60, up60=u60, m60=m60))
r = pd.DataFrame(rows).pivot(index='cell', columns='per')
r.columns = [f'{a}_{b}' for a, b in r.columns]
r['name'] = [('+'.join(f for i, f in enumerate(F) if c >> i & 1) or '(無)') for c in r.index]
base = {p: st(y, 20)[1:] + st(y, 60)[1:] for p, y in x.groupby('per')}
print('基準（全體）', {p: [round(v, 4) for v in vals] for p, vals in base.items()})
ok = r[(r.n_build >= 2000)].copy()
ok = ok.sort_values('up60_build', ascending=False)
pd.set_option('display.width', 250)
cols = ['name', 'n_build', 'up20_build', 'up60_build', 'm60_build', 'n_check', 'up20_check', 'up60_check', 'm60_check']
print(ok[cols].round(3).head(15).to_string()); print(ok[cols].round(3).tail(6).to_string())
spearmanr = lambda a, b: (pd.Series(a.values).rank().corr(pd.Series(b.values).rank()),)
oc = ok.dropna(subset=['up60_check']); oc = oc[oc.n_check >= 300]
print('排名一致性（建表 vs 檢驗）up60 spearman', round(spearmanr(oc.up60_build, oc.up60_check)[0], 3), 'm60', round(spearmanr(oc.m60_build, oc.m60_check)[0], 3), 'cells', len(oc))
q = oc.sort_values('up60_build'); k = len(q) // 5
for nm, part in (('前20%組合', q.tail(k)), ('後20%組合', q.head(k))):
    w = part.n_check; print(nm, '檢驗期 up60 %.3f m60 %.4f up20 %.3f' % ((part.up60_check * w).sum() / w.sum(), (part.m60_check * w).sum() / w.sum(), (part.up20_check * w).sum() / w.sum()), int(w.sum()))
for f_ in F:
    for p, y in x.groupby('per'):
        print(f_, p, 'with %.3f/%.4f without %.3f/%.4f' % ((y[y[f_]].f60 > 0).mean(), y[y[f_]].f60.mean(), (y[~y[f_]].f60 > 0).mean(), y[~y[f_]].f60.mean()))
out = {'factors': F, 'cells': {int(c): {k: (None if pd.isna(v) else (round(float(v), 4) if not isinstance(v, str) else v)) for k, v in row.items()} for c, row in r.iterrows()},
       'base': {p: [round(float(v), 4) for v in vals] for p, vals in base.items()}}
json.dump(out, open(os.path.join(HERE, 'combo_prob.json'), 'w'), ensure_ascii=False)
