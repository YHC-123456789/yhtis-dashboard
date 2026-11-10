#!/usr/bin/env python3
"""當沖統計：今日收盤型態 → 隔日 開盤買/收盤賣。成本 0.33%（當沖手續費折扣＋證交稅減半）。
型態：今日漲跌（相對參考價）× 收盤位置（當日區間）× 量比（今日量/前20日均量）。
另：市場層級 —— 今日全市場上漲家數比例 分組 → 隔日全體等權 開→收。
期間：建表 2019–2024；檢驗 2025–2026（看各格方向是否維持）。流動性：20日均成交值 >= 1 億。"""
import importlib.util, os, gc, json
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('b', os.path.join(HERE, 'backtest_v5b.py')); b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
a = b.load()
for c in ['open', 'high', 'low', 'close', 'ref_price', 'volume']: a[c] = a[c].astype('float32')
key = a.code; g = a.groupby('code', observed=True, sort=False)
pc = g.close.shift(1); ref = a.ref_price.where(a.ref_price.notna(), pc)
a['r'] = (a.close / ref - 1).astype('float32')
rng = (a.high - a.low); a['clv'] = ((a.close - a.low) / rng.where(rng > 0)).astype('float32')
vm = a.volume.groupby(key, observed=True).transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
a['vr'] = (a.volume / vm).astype('float32')
a['val20'] = (a.close * a.volume).groupby(key, observed=True).transform(lambda s: s.rolling(20, min_periods=20).mean()).astype('float32')
O1 = g.open.shift(-1); a['oc'] = (g.close.shift(-1) / O1 - 1 - 0.0033).astype('float32')
a['h2'] = (g.high.shift(-1) / O1 - 1 >= 0.02); a['l2'] = (g.low.shift(-1) / O1 - 1 <= -0.02)
ref1 = g.ref_price.shift(-1).where(g.ref_price.shift(-1).notna(), a.close); a['gap'] = (O1 / ref1 - 1).astype('float32')
a['ok'] = O1.notna() & (O1 > 0) & (a.gap < 0.095) & (a.gap > -0.095)
del pc, ref, rng, vm, O1, ref1; gc.collect()
up = a[a.r.notna()].groupby('date').r.apply(lambda s: (s > 0.0005).mean()); a = a.merge(up.rename('mup'), left_on='date', right_index=True)
x = a[(a.date >= '2019-01-01') & (a.val20 >= 1e8) & a.ok & a.r.notna() & a.clv.notna() & a.vr.notna()].copy(); del a; gc.collect()
x['R'] = pd.cut(x.r, [-1, -0.05, -0.02, 0, 0.02, 0.05, 0.095, 1], labels=['<-5%', '-5~-2%', '-2~0%', '0~2%', '2~5%', '5~9.5%', '漲停'])
x['P'] = pd.cut(x.clv, [-0.01, 0.33, 0.67, 1.01], labels=['收低', '收中', '收高'])
x['V'] = pd.cut(x.vr, [0, 1, 2, 1e9], labels=['量縮', '量增', '爆量'])
x['M'] = pd.cut(x.mup, [0, 0.2, 0.4, 0.6, 0.8, 1.01], labels=['<20%', '20-40%', '40-60%', '60-80%', '>=80%'])
x['per'] = np.where(x.date < '2025-01-01', 'build', 'check')
f = lambda y: pd.Series({'n': len(y), 'win': (y.oc > 0).mean(), 'mean': y.oc.mean(), 'h2': y.h2.mean(), 'l2': y.l2.mean(), 'gap': y.gap.mean()})
pd.set_option('display.width', 220)
print('== 全體 ==');print(x.groupby('per').apply(f).round(4))
print('== 市場上漲家數比例（今日）→ 隔日 ==');print(x.groupby(['M', 'per'], observed=True).apply(f).round(4).unstack('per'))
t = x.groupby(['R', 'P', 'V', 'per'], observed=True).apply(f).unstack('per')
t.columns = [f'{a}_{b}' for a, b in t.columns]; t = t[(t.n_build >= 300)]
t['same'] = np.sign(t.mean_build) == np.sign(t.mean_check)
print('== 型態格（建表 n>=300）：最好/最差 ==')
cols = ['n_build', 'win_build', 'mean_build', 'h2_build', 'n_check', 'win_check', 'mean_check', 'h2_check', 'same']
print(t.sort_values('mean_build', ascending=False)[cols].head(12).round(4).to_string())
print(t.sort_values('mean_build')[cols].head(6).round(4).to_string())
print('cells', len(t), 'positive_build', int((t.mean_build > 0).sum()), 'positive_both', int(((t.mean_build > 0) & (t.mean_check > 0)).sum()), 'same_sign', int(t.same.sum()))
out = {'cells': {f'{r}|{p}|{v}': {k: (None if pd.isna(val) else round(float(val), 4)) for k, val in row.items() if k != 'same'} for (r, p, v), row in t.iterrows()},
       'market': {str(m): {k: round(float(val), 4) for k, val in row.items()} for m, row in x.groupby('M', observed=True).apply(f).iterrows()}}
json.dump(out, open(os.path.join(HERE, 'dt_stats.json'), 'w'), ensure_ascii=False)
