# -*- coding: utf-8 -*-
import pandas as pd, numpy as np, pyarrow.parquet as pq
from pathlib import Path

fs = sorted(Path('data/kline_daily').glob('date=*/part.parquet'))
cols = ['symbol', 'date', 'open', 'high', 'low', 'close']
df = pd.concat([pq.ParquetFile(f).read(columns=cols).to_pandas() for f in fs], ignore_index=True)
df['date'] = pd.to_datetime(df['date'])
df = df.sort_values(['symbol', 'date']).reset_index(drop=True)
g = df.groupby('symbol', sort=False)
df['prev'] = g['close'].shift(1)
df['nopen'] = g['open'].shift(-1)
df['ndate'] = g['date'].shift(-1)
ex = pd.read_parquet('data/adj_factor/all.parquet')[['symbol', 'trade_date', 'ex_factor']]
ex['trade_date'] = pd.to_datetime(ex.trade_date)


def m(dates):
    v = ex.set_index(['symbol', 'trade_date'])['ex_factor'].reindex(
        pd.MultiIndex.from_arrays([df.symbol, dates])).to_numpy()
    return np.where(np.isfinite(v), v, 1.0)


df['ap'] = df.prev / m(df.date)
df['ex1'] = m(df.ndate)
ap = df.ap.to_numpy()
hi = df.high.to_numpy()
lo = df.low.to_numpy()
cl = df.close.to_numpy()
L = np.full(len(df), np.nan)
for x in (0.05, 0.10, 0.20, 0.30):
    e = np.round(ap * (1 + x), 2)
    L = np.where(np.isnan(L) & (np.abs(hi - e) <= 0.011), x, L)
df['L'] = L
df['lim'] = np.round(ap * (1 + np.where(np.isnan(L), 0, L)), 2)
df['touch'] = ~np.isnan(L)
df['closed'] = df.touch & (np.abs(cl - df.lim) <= 0.011)
df['win'] = df.touch & ((df.lim - lo) > 0.011)
df['r0'] = df.nopen * df.ex1 / df.lim - 1

z = df[df.win & ~df.closed & df.nopen.notna()]
sz = df[df.win & df.closed & df.nopen.notna()]
print('=== 炸板样本 (n=%d)' % len(z))
for _, r in z.sample(4, random_state=3).iterrows():
    print('%-10s %s adjprev=%.3f L=%.2f 涨停价=%.2f high=%.2f low=%.2f close=%.2f | T+1开=%.2f r0=%+.2f%%'
          % (r.symbol, r.date.date(), r.ap, r['L'], r['lim'], r.high, r.low, r.close, r.nopen, r.r0 * 100))
print('=== 封住样本 (n=%d)' % len(sz))
for _, r in sz.sample(3, random_state=3).iterrows():
    print('%-10s %s adjprev=%.3f L=%.2f 涨停价=%.2f low=%.2f close=%.2f | T+1开=%.2f r0=%+.2f%%'
          % (r.symbol, r.date.date(), r.ap, r['L'], r['lim'], r.low, r.close, r.nopen, r.r0 * 100))
print('炸板占比 %.4f  |  炸板 r0 均值 %.2fbp  封住 r0 均值 %.2fbp  混合 %.2fbp'
      % (len(z) / (len(z) + len(sz)), z.r0.mean() * 1e4, sz.r0.mean() * 1e4,
         (z.r0.sum() + sz.r0.sum()) / (len(z) + len(sz)) * 1e4))
