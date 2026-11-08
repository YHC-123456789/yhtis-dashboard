#!/usr/bin/env python3
"""V5 回測 第二版：比較四種事先固定的規則（不依結果調參）。
A 原版：未還原價格、無其他排除（= backtest_v5.py）
B 目前上線版：A + 近 60 日有除權息者不列入
C 還原價：指標改用除權息還原價格計算，不再排除除權息股
D 還原價＋流動性：C + 20 日平均成交值 >= 5,000 萬元（與 rev_scan.py 相同門檻）
報酬、成本、基準、期間、統計方法與 backtest_v5.py 相同。基準同為所有合格股（不受流動性門檻影響）。
用法：python3 v5/backtest/backtest_v5b.py → v5/backtest/backtest_v5b.json
"""
import glob, json, os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.path.join(ROOT, os.environ.get('V5_STORE', 'v5_history/store_preview/days'))
COST = 0.00785
HS = (5, 20, 60)
MIN_VAL20 = 5e7
PER = {'train_2019_2022': ('2019-01-01', '2022-12-31'), 'valid_2023_2024': ('2023-01-01', '2024-12-31'), 'holdout_2025_2026': ('2025-01-01', '2026-12-31')}


CACHE = os.path.expanduser('~/v5b_cache.pkl')


def load():
    if os.path.exists(CACHE):
        return pd.read_pickle(CACHE)
    fs = [f for f in sorted(glob.glob(os.path.join(STORE, '*.csv'))) if os.path.basename(f) >= '2018-06-01']
    a = pd.concat([pd.read_csv(f, usecols=['date', 'code', 'open', 'high', 'low', 'close', 'ref_price', 'volume'], dtype={'code': str}) for f in fs], ignore_index=True)
    for c in ['open', 'high', 'low', 'close', 'ref_price', 'volume']:
        a[c] = pd.to_numeric(a[c], errors='coerce').astype('float64')
    a['date'] = pd.to_datetime(a['date'])
    a['code'] = a['code'].astype('category')
    a = a.sort_values(['code', 'date'], kind='stable').reset_index(drop=True)
    a.to_pickle(CACHE)
    return a


def kdj_arrays(h, l, c, codes):
    J = np.full(len(c), np.nan)
    starts = np.r_[0, np.flatnonzero(codes[1:] != codes[:-1]) + 1, len(c)]
    for s, e in zip(starts[:-1], starts[1:]):
        hh = pd.Series(h[s:e]).rolling(9, min_periods=1).max().to_numpy(); ll = pd.Series(l[s:e]).rolling(9, min_periods=1).min().to_numpy()
        k = d = 50.
        for i in range(e - s):
            rsv = (c[s + i] - ll[i]) / (hh[i] - ll[i]) * 100 if hh[i] > ll[i] else 50.
            k = (2 * k + rsv) / 3; d = (2 * d + k) / 3; J[s + i] = 3 * k - 2 * d
    return J


def indicators(a, o, h, l, c):
    """回傳研究分數需要的欄位（公式同 v5_early_warning.compute）。"""
    key = a['code']
    df = pd.DataFrame({'h': h, 'l': l, 'c': c, 'v': a['volume'], 'code': key})
    g = df.groupby('code', observed=True, sort=False)
    pc = g['c'].shift(1)
    out = pd.DataFrame(index=a.index)
    out['ret5'] = df.c / g.c.shift(5) - 1
    out['ret20'] = df.c / g.c.shift(20) - 1
    obv = (np.sign(df.c - pc).fillna(0) * df.v).groupby(key, observed=True).cumsum()
    out['obv10'] = obv - obv.groupby(key, observed=True).shift(10)
    ma = g.c.transform(lambda s: s.rolling(20, min_periods=20).mean()); sd = g.c.transform(lambda s: s.rolling(20, min_periods=20).std(ddof=0))
    bbw = 4 * sd / ma
    out['bbw'] = bbw
    out['bbw_med'] = bbw.groupby(key, observed=True).transform(lambda s: s.rolling(20, min_periods=20).median())
    out['J'] = kdj_arrays(df.h.to_numpy(), df.l.to_numpy(), df.c.to_numpy(), key.cat.codes.to_numpy())
    out['Jp'] = out['J'].groupby(key, observed=True).shift(1)
    h20 = g.h.transform(lambda s: s.shift(1).rolling(20, min_periods=20).max())
    out['near'] = (df.c >= h20 * .97) & (df.c < h20 * 1.02)
    e9 = g.c.transform(lambda s: s.ewm(span=9, adjust=False).mean()); e20 = g.c.transform(lambda s: s.ewm(span=20, adjust=False).mean())
    out['emaup'] = e9 > e20
    return out


def nw_t(m, lag):
    z = m - m.mean(); n = len(z)
    if n <= 3: return None
    var = (z @ z) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * (z[k:] @ z[:-k]) / n
    return round(float(m.mean() / np.sqrt(var / n)), 2) if var > 0 else None


def score(v, ind):
    x = ind.loc[v.index]
    rs5 = x.ret5 - x.ret5.groupby(v.date).transform('median')
    rs20 = x.ret20 - x.ret20.groupby(v.date).transform('median')
    s = (rs5 > 0) * 2 + (rs20 > 0) * 2 + (x.obv10 > 0) * 2 + (x.bbw < x.bbw_med * .85) * 2 + ((x.J > x.Jp) & (x.Jp < 50)).astype(int) + x.near.astype(int) + x.emaup.astype(int)
    return s, rs5, rs20


def main():
    import sys
    a = load()
    if '--load-only' in sys.argv:
        print('cached', len(a)); return; g = a.groupby('code', observed=True, sort=False)
    pc = g['close'].shift(1)
    raw_f = a['ref_price'] / pc
    f = raw_f.where(a['ref_price'].notna() & (pc > 0) & ((raw_f - 1).abs() > 0.001), 1.0).clip(0.2, 5)
    cumf = f.groupby(a['code'], observed=True).cumprod()
    ao = a['open'] / cumf
    a['exr'] = (f != 1.0)
    a['exr60'] = a['exr'].astype(int).groupby(a['code'], observed=True).transform(lambda s: s.rolling(60, min_periods=1).max()).astype(bool)
    a['val20'] = (a['close'] * a['volume']).groupby(a['code'], observed=True).transform(lambda s: s.rolling(20, min_periods=20).mean())
    a['vr'] = a['volume'] / g['volume'].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    a['nbar'] = g.cumcount() + 1
    o1 = ao.groupby(a['code'], observed=True).shift(-1)
    for h in HS:
        a[f'r{h}'] = ao.groupby(a['code'], observed=True).shift(-(1 + h)) / o1 - 1 - COST
    a['entry_ok'] = (g['open'].shift(-1) / a['close'] - 1) < 0.095
    print('loaded', len(a), flush=True)
    raw = indicators(a, a.open, a.high, a.low, a.close); print('raw ind', flush=True)
    adj = indicators(a, a.open / cumf, a.high / cumf, a.low / cumf, a.close / cumf); print('adj ind', flush=True)
    valid = (a['nbar'] >= 60) & a[['open', 'high', 'low', 'close']].notna().all(axis=1) & (a['volume'] > 0) & raw['bbw_med'].notna() & (a['date'] >= '2019-01-01')
    v = a[valid].copy()
    base = {h: v[v.entry_ok].groupby('date')[f'r{h}'].mean() for h in HS}
    variants = {}
    for name, ind, extra in (('A_original', raw, None), ('B_live_exclude_exr60', raw, 'exr'), ('C_adjusted', adj, None), ('D_adjusted_liquidity', adj, 'liq')):
        s, rs5, rs20 = score(v, ind)
        w = v.assign(score=s, rs5=rs5, rs20=rs20)
        elig = (w.score >= 7) & (w.vr >= 1)
        if extra == 'exr': elig &= ~w.exr60
        if extra == 'liq': elig &= w.val20 >= MIN_VAL20
        w = w[elig].sort_values(['date', 'score', 'rs20', 'rs5', 'code'], ascending=[True, False, False, False, True])
        top = w.groupby('date').head(10); tf = top[top.entry_ok]
        res = {'top10_rows': int(len(top)), 'median_val20_億': round(float(top.val20.median() / 1e8), 2), 'days_with_<10': int((top.groupby('date').size() < 10).sum())}
        for h in HS:
            t = tf.groupby('date')[f'r{h}'].mean().dropna(); ex = (t - base[h].reindex(t.index)).dropna()
            for pn, (s0, e0) in PER.items():
                x = ex[(ex.index >= s0) & (ex.index <= e0)]; tt = t[(t.index >= s0) & (t.index <= e0)]
                m = x.groupby(x.index.to_period('M')).mean().values
                res[f'H{h}_{pn}'] = dict(days=int(len(x)), net=round(float(tt.mean()), 5), excess=round(float(x.mean()), 5), t=nw_t(m, max(1, h // 21)),
                                        hit=round(float((x > 0).mean()), 3), win=round(float((tf[(tf.date >= s0) & (tf.date <= e0)][f'r{h}'] > 0).mean()), 3))
        res['last_top10'] = top[top.date == v.date.max()].code.astype(str).tolist()
        variants[name] = res
        print(name, json.dumps({k: (vv['excess'], vv['t']) for k, vv in res.items() if k.startswith('H')}, ensure_ascii=False), flush=True)
    out = {'last_date': str(v.date.max().date()), 'min_val20': MIN_VAL20, 'variants': variants}
    os.makedirs(os.path.join(ROOT, 'v5/backtest'), exist_ok=True)
    json.dump(out, open(os.path.join(ROOT, 'v5/backtest/backtest_v5b.json'), 'w'), ensure_ascii=False, indent=1)
    print('DONE')


if __name__ == '__main__':
    main()
