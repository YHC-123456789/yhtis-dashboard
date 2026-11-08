#!/usr/bin/env python3
"""依 V5 研究分（還原價，規則 C）統計歷史上漲機率。每天、每檔合格股票為一筆；隔日開盤買、H 日後開盤賣、扣成本 0.785%。
上漲機率 = 淨報酬 > 0 的比例；跑贏大盤機率 = 淨報酬 > 同日全部合格股平均的比例。
用法：python3 v5/backtest/prob_by_score.py → v5/backtest/prob_by_score.json"""
import importlib.util, json, os, sys
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('b', os.path.join(HERE, 'backtest_v5b.py')); b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
a = b.load(); g = a.groupby('code', observed=True, sort=False)
pc = g['close'].shift(1); raw_f = a['ref_price'] / pc
f = raw_f.where(a['ref_price'].notna() & (pc > 0) & ((raw_f - 1).abs() > 0.001), 1.0).clip(0.2, 5)
cumf = f.groupby(a['code'], observed=True).cumprod(); ao = a['open'] / cumf
a['val20'] = (a['close'] * a['volume']).groupby(a['code'], observed=True).transform(lambda s: s.rolling(20, min_periods=20).mean())
a['vr'] = a['volume'] / g['volume'].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
a['nbar'] = g.cumcount() + 1
o1 = ao.groupby(a['code'], observed=True).shift(-1)
for h in b.HS:
    a[f'r{h}'] = ao.groupby(a['code'], observed=True).shift(-(1 + h)) / o1 - 1 - b.COST
a['entry_ok'] = (g['open'].shift(-1) / a['close'] - 1) < 0.095
adj = b.indicators(a, a.open / cumf, a.high / cumf, a.low / cumf, a.close / cumf)
valid = (a['nbar'] >= 60) & a[['open', 'high', 'low', 'close']].notna().all(axis=1) & (a['volume'] > 0) & adj['bbw_med'].notna() & (a['date'] >= '2019-01-01')
v = a[valid & a.entry_ok].copy()
s, _, _ = b.score(v, adj)
v['score'] = s.values; v['elig'] = (v.score >= 7) & (v.vr >= 1); v['liq'] = v.val20 >= 5e7
out = {'note': '歷史頻率（2019-01 ~ 2026-11），不是保證；每筆持有期間重疊。', 'groups': {}}
def stats(x, per):
    r = {}
    for h in b.HS:
        y = x[x[f'r{h}'].notna()]
        mk = y.date.map(v[v[f'r{h}'].notna()].groupby('date')[f'r{h}'].mean())
        r[f'H{h}'] = dict(n=int(len(y)), up=round(float((y[f'r{h}'] > 0).mean()), 3), beat=round(float((y[f'r{h}'] > mk).mean()), 3),
                          mean=round(float(y[f'r{h}'].mean()), 4), median=round(float(y[f'r{h}'].median()), 4))
    return r
for per, (s0, e0) in {'all': ('2019-01-01', '2026-12-31'), 'holdout_2025_2026': ('2025-01-01', '2026-12-31')}.items():
    w = v[(v.date >= s0) & (v.date <= e0)]
    G = {'ALL_STOCKS': stats(w, per)}
    for sc in range(0, 12):
        x = w[w.score == sc]
        if len(x) > 500:
            G[f'score_{sc}'] = stats(x, per)
    G['eligible'] = stats(w[w.elig], per); G['eligible_liquid'] = stats(w[w.elig & w.liq], per); G['eligible_illiquid'] = stats(w[w.elig & ~w.liq], per)
    for sc in (7, 8, 9, 10, 11):
        G[f'elig_score_{sc}'] = stats(w[w.elig & (w.score == sc)], per)
        G[f'elig_score_{sc}_liquid'] = stats(w[w.elig & (w.score == sc) & w.liq], per)
    out['groups'][per] = G
json.dump(out, open(os.path.join(HERE, 'prob_by_score.json'), 'w'), ensure_ascii=False, indent=1)
for per in out['groups']:
    print('==', per)
    for k, x in out['groups'][per].items():
        print(f"{k:24s}", ' '.join(f"H{h}: n={x[f'H{h}']['n']:>7} 上漲{x[f'H{h}']['up']:.1%} 贏大盤{x[f'H{h}']['beat']:.1%} 平均{x[f'H{h}']['mean']:+.2%}" for h in (5, 20, 60)))
