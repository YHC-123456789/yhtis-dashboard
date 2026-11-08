#!/usr/bin/env python3
"""V5 研究分數歷史回測（與 v5_early_warning.py 相同的指標與分數，向量化重算）。
資料：v5_history/store_preview（官方逐日行情；以參考價推算除權息還原因子計算報酬）。
規則：每日收盤後算分數 → TOP 10（score>=7、量比>=1，排序同原版）→ 隔日開盤等權買進 → 持有 H 日後開盤賣出。
成本：來回 0.785%。比較基準：同日所有通過資料檢查股票的等權平均（同樣開盤買、開盤賣、同成本）。
期間：訓練 2019–2022、驗證 2023–2024、保留 2025–2026（分數規則事先固定，未依結果調參）。
用法：python3 v5/backtest/backtest_v5.py → v5/backtest/backtest_v5.json
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.path.join(ROOT, os.environ.get('V5_STORE', 'v5_history/store_preview/days'))
COST = 0.00785
HS = (5, 20, 60)

def load():
    fs = sorted(glob.glob(os.path.join(STORE, '*.csv')))
    fs = [f for f in fs if os.path.basename(f) >= '2018-06-01']
    parts = []
    for f in fs:
        d = pd.read_csv(f, usecols=['date', 'code', 'open', 'high', 'low', 'close', 'ref_price', 'volume'],
                        dtype={'code': str}, engine='c')
        parts.append(d)
    a = pd.concat(parts, ignore_index=True)
    for c in ['open', 'high', 'low', 'close', 'ref_price', 'volume']:
        a[c] = pd.to_numeric(a[c], errors='coerce').astype('float32')
    a['date'] = pd.to_datetime(a['date'])
    a['code'] = a['code'].astype('category')
    return a.sort_values(['code', 'date'], kind='stable').reset_index(drop=True)

def kdj(g):
    h, l, c = g['high'].to_numpy(float), g['low'].to_numpy(float), g['close'].to_numpy(float)
    n = len(c); J = np.full(n, np.nan); k = d = 50.
    hh = pd.Series(h).rolling(9, min_periods=1).max().to_numpy(); ll = pd.Series(l).rolling(9, min_periods=1).min().to_numpy()
    for i in range(n):
        rsv = (c[i] - ll[i]) / (hh[i] - ll[i]) * 100 if hh[i] > ll[i] else 50.
        k = (2 * k + rsv) / 3; d = (2 * d + k) / 3; J[i] = 3 * k - 2 * d
    return pd.Series(J, index=g.index)

def main():
    a = load(); g = a.groupby('code', observed=True, sort=False)
    pc = g['close'].shift(1)
    # 還原因子：參考價/昨收（除權息、減資等）；無參考價視為 1
    f = (a['ref_price'] / pc).where(a['ref_price'].notna() & (pc > 0), 1.0).clip(0.2, 5).astype('float64')
    a['cumf'] = f.groupby(a['code'], observed=True).cumprod()  # 往後累乘；報酬用 open_t2/open_t1 * cumf(t2)/cumf(t1)... 見下
    a['ao'] = a['open'] / a['cumf']  # 將過去價格換算到「未除權」口徑的反向；比值即正確報酬
    a['ret5'] = a['close'] / g['close'].shift(5) - 1
    a['ret20'] = a['close'] / g['close'].shift(20) - 1
    sign = np.sign(a['close'] - pc).fillna(0)
    a['obv'] = (sign * a['volume']).groupby(a['code'], observed=True).cumsum()
    a['obv10'] = a['obv'] - g['obv'].shift(10)
    ma = g['close'].transform(lambda s: s.rolling(20, min_periods=20).mean())
    sd = g['close'].transform(lambda s: s.rolling(20, min_periods=20).std(ddof=0))
    a['bbw'] = 4 * sd / ma
    a['bbw_med'] = a.groupby('code', observed=True)['bbw'].transform(lambda s: s.rolling(20, min_periods=20).median())
    a['J'] = g.apply(kdj, include_groups=False).reset_index(level=0, drop=True).sort_index()
    a['Jp'] = a.groupby('code', observed=True)['J'].shift(1)
    a['vr'] = a['volume'] / g['volume'].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    h20 = g['high'].transform(lambda s: s.shift(1).rolling(20, min_periods=20).max())
    a['near'] = (a['close'] >= h20 * .97) & (a['close'] < h20 * 1.02)
    e9 = g['close'].transform(lambda s: s.ewm(span=9, adjust=False).mean()); e20 = g['close'].transform(lambda s: s.ewm(span=20, adjust=False).mean())
    a['emaup'] = e9 > e20
    a['nbar'] = g.cumcount() + 1
    a['valid'] = (a['nbar'] >= 60) & a[['open', 'high', 'low', 'close']].notna().all(axis=1) & (a['volume'] > 0) & a['bbw_med'].notna()
    # 前瞻報酬（還原）：隔日開盤買、H 日後開盤賣
    o1 = g['ao'].shift(-1)
    for h in HS:
        a[f'r{h}'] = (g['ao'].shift(-(1 + h)) / o1) - 1 - COST
    a['entry_ok'] = (g['open'].shift(-1) / a['close'] - 1) < 0.095  # 開盤近漲停視為買不到
    v = a[a['valid'] & (a['date'] >= '2019-01-01')].copy()
    v['rs5'] = v['ret5'] - v.groupby('date')['ret5'].transform('median')
    v['rs20'] = v['ret20'] - v.groupby('date')['ret20'].transform('median')
    v['score'] = (v.rs5 > 0) * 2 + (v.rs20 > 0) * 2 + (v.obv10 > 0) * 2 + (v.bbw < v.bbw_med * .85) * 2 + ((v.J > v.Jp) & (v.Jp < 50)).astype(int) + v.near.astype(int) + v.emaup.astype(int)
    v['elig'] = (v.score >= 7) & (v.vr >= 1)
    v = v.sort_values(['date', 'score', 'rs20', 'rs5', 'code'], ascending=[True, False, False, False, True])
    top = v[v.elig].groupby('date').head(10)
    top_fill = top[top.entry_ok]
    per = {'train_2019_2022': ('2019-01-01', '2022-12-31'), 'valid_2023_2024': ('2023-01-01', '2024-12-31'), 'holdout_2025_2026': ('2025-01-01', '2026-12-31')}
    out = {'rows': int(len(a)), 'validated_rows_2019+': int(len(v)), 'top10_rows': int(len(top)), 'top10_skipped_limit_up_open': int(len(top) - len(top_fill)), 'results': {}}
    for h in HS:
        base = v[v.entry_ok].groupby('date')[f'r{h}'].mean()
        t = top_fill.groupby('date')[f'r{h}'].mean().dropna()
        ex = (t - base.reindex(t.index)).dropna()
        res = {}
        for name, (s, e) in per.items():
            x = ex[(ex.index >= s) & (ex.index <= e)]; tt = t[(t.index >= s) & (t.index <= e)]
            m = x.groupby(x.index.to_period('M')).mean()
            lag = max(1, h // 21)
            # Newey-West t on monthly
            z = m.values - m.values.mean(); nn = len(z); var = (z @ z) / nn
            for k in range(1, lag + 1):
                var += 2 * (1 - k / (lag + 1)) * (z[k:] @ z[:-k]) / nn
            tstat = float(m.values.mean() / np.sqrt(var / nn)) if nn > 3 and var > 0 else None
            res[name] = dict(days=int(len(x)), top10_net=round(float(tt.mean()), 5), excess=round(float(x.mean()), 5),
                             excess_t=None if tstat is None else round(tstat, 2), hit_vs_market=round(float((x > 0).mean()), 3),
                             top10_win=round(float((top_fill[(top_fill.date >= s) & (top_fill.date <= e)][f'r{h}'] > 0).mean()), 3))
        out['results'][f'H{h}'] = res
    # 與 11/6 輸出一致性抽查
    last = v[v.date == v.date.max()]
    out['last_date'] = str(v.date.max().date())
    out['last_top10'] = top[top.date == v.date.max()][['code', 'score']].astype({'code': str}).values.tolist()
    os.makedirs(os.path.join(ROOT, 'v5/backtest'), exist_ok=True)
    json.dump(out, open(os.path.join(ROOT, 'v5/backtest/backtest_v5.json'), 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
