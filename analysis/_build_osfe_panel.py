"""构建 OS/FE 状态面板（PIT 全样本：含退市、含 ST 不剔除）。

关键口径（报告必带）：
  - 样本池 = `kline_daily_enriched`（qfq，5,582 只，全部活到最新）+ 
             `backfill_20261117/stock_gap_daily.parquet`（raw，307 只含退市/合并/改名）。
            缺口股 `price_basis='raw'`：无分红记录，除权日显示为假跌，方向已知
            （`docs/delisted-daily-union.md`）——只压低收益，不制造假信号。
  - **不做任何基于「今天是否 ST」的过滤**（这是上一轮面板 6.9% 未来信息的来源）。
    ST 只作为 PIT 标记位 `is_st`，用于分层与稳健性，不用于剔除。
  - 观测日 = 全局交易日索引 rn 每 5 天一个（确定性锚点，非随机），
    且该股已有 ≥250 个交易日历史（次新股排除，指数起始日不等）。
  - 标签用 rn 精确定位（不数行数天）：pump = max(close[t+1..t+60])/close[t]-1 ≥ 阈值；
    窗口不完整（退市/长期停牌跨过 t+60）记 NaN，并在结果里报告截断率。

产物：data/_osfe/panel.parquet
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _osfe_lib import assert_kernel_equivalence, chip_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_osfe"
START = "2013-01-01"
EVAL_START = "2016-01-01"
EVAL_STEP = 5
MIN_HIST = 250
FWD = 60


def load_prices() -> pl.DataFrame:
    con = duckdb.connect()
    con.execute("PRAGMA threads=8")
    main = con.execute(f"""
        select symbol, date, open, high, low, close, volume::double as volume,
               amount::double as amount, turnover_rate, raw_close, 'main' as src
        from read_parquet('{DATA}/kline_daily_enriched/date=*/part.parquet')
        where date >= DATE '{START}' and close > 0 and volume > 0
    """).df()
    # 缺口股无 turnover_rate：用历史流通股本（unlimited_shares，按公告日 as-of）换算。
    # 主表自检：turnover_rate ≈ volume(手)×10000/流通股（median 比值 98.7 ⇒ 单位正确）。
    gap = con.execute(f"""
        with f as (
            select symbol, end_date, unlimited_shares
            from read_parquet('{DATA}/backfill_20261117/fin_shares.parquet')
            where unlimited_shares > 0
        ), g as (
            select symbol, date, open, high, low, close, volume::double as volume,
                   amount::double as amount
            from read_parquet('{DATA}/backfill_20261117/stock_gap_daily.parquet')
            where date >= DATE '{START}' and close > 0 and volume > 0
        )
        select g.symbol, g.date, g.open, g.high, g.low, g.close, g.volume, g.amount,
               least(volume * 10000.0 / f.unlimited_shares, 100.0) as turnover_rate,
               g.close as raw_close, 'gap' as src
        from g asof left join f
          on g.symbol = f.symbol and g.date >= f.end_date
    """).df()
    df = pl.concat([pl.from_pandas(main), pl.from_pandas(gap)], how="vertical")
    return df.with_columns(pl.col("date").cast(pl.Date)).sort(["symbol", "date"])


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    px = load_prices()
    print(f"prices rows={px.height:,} symbols={px['symbol'].n_unique():,} "
          f"gap_rows={px.filter(pl.col('src') == 'gap').height:,} "
          f"dates={px['date'].n_unique():,} [{time.time()-t0:.0f}s]", flush=True)

    # 硬闸门：向量化路径必须与生产内核等价
    eq = assert_kernel_equivalence(
        px.filter(pl.col("date").dt.year() == 2019), n_cases=60
    )
    print(f"kernel_equivalence OK {eq}", flush=True)

    cal = px.select("date").unique().sort("date").with_row_index("rn")
    px = px.join(cal, on="date", how="left")

    # 行业 / 板块 / ST（PIT 标记，不用于剔除）
    ind = pl.read_parquet(str(DATA / "ext_data/ext_hy_ths/part.parquet")).select(
        "symbol",
        pl.col("所属同花顺行业").str.split("-").list.first().alias("industry"),
    )
    px = px.join(ind, on="symbol", how="left")
    # 板块用前缀表达式（map_elements 在 1,600 万行上会慢到不可用）
    code = pl.col("symbol").str.slice(0, 3)
    px = px.with_columns(
        pl.when(code.is_in(["688", "689"])).then(pl.lit("star"))
        .when(code.is_in(["300", "301", "302"])).then(pl.lit("chinext"))
        .when(code.is_in(["920", "830", "831", "832", "833", "834", "835", "836",
                          "837", "838", "839", "870", "871", "872", "873", "430"]))
        .then(pl.lit("bse"))
        .otherwise(pl.lit("main")).alias("board")
    )

    st_path = DATA / "_st_hist/st_days.parquet"
    cov_path = DATA / "_st_hist/st_covered.parquet"
    if st_path.exists():
        st = pl.read_parquet(str(st_path)).with_columns(pl.lit(True).alias("is_st"))
        cov = pl.read_parquet(str(cov_path)).with_columns(pl.lit(True).alias("st_known"))
        px = px.join(st, on=["symbol", "date"], how="left").join(cov, on="symbol", how="left")
        px = px.with_columns(
            pl.col("is_st").fill_null(False),
            pl.col("st_known").fill_null(False),
        )
        print(f"st joined: known={px.filter(pl.col('st_known')).height:,} "
              f"st_rows={px.filter(pl.col('is_st')).height:,}", flush=True)
    else:
        px = px.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_st"),
                             pl.lit(False).alias("st_known"))

    # ---- 逐股滚动特征（按该股自身交易日序列，停牌日不占行）----
    px = px.with_columns(
        pl.col("close").shift(1).over("symbol").alias("_c1"),
    )
    px = px.with_columns(
        (pl.col("close") / pl.col("_c1") - 1).alias("ret1"),
    )
    px = px.with_columns(
        pl.col("amount").rolling_mean(20).over("symbol").alias("adv20"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("to20"),
        pl.col("turnover_rate").rolling_mean(120).over("symbol").alias("to120"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).rolling_mean(20)
            .over("symbol").alias("amp20"),
        (pl.col("amount") / (pl.col("turnover_rate") / 100.0)).rolling_median(20)
            .over("symbol").alias("mcap_f"),
        pl.col("ret1").abs().rolling_mean(20).over("symbol").alias("absret20"),
        pl.col("close").shift(20).over("symbol").alias("_c20"),
        pl.col("close").shift(60).over("symbol").alias("_c60"),
        pl.col("close").rolling_max(250).over("symbol").alias("_hi250"),
        pl.int_range(pl.len()).over("symbol").alias("hist_n"),
        pl.col("rn").last().over("symbol").alias("last_rn"),
    )
    px = px.with_columns(
        (pl.col("close") / pl.col("_c20") - 1).alias("r20"),
        (pl.col("close") / pl.col("_c60") - 1).alias("r60"),
        (pl.col("close") / pl.col("_hi250") - 1).alias("dist_hi250"),
    ).drop(["_c20", "_c60"])

    # ---- 候选状态族（预注册 `docs/state-family-screen-prereg.md` 第 2 节穷举）----
    # 全部 PIT：只用 t 及之前的数据；窗口只取 5/20/60/250 四档，不搜索
    px = px.with_columns(
        pl.col("close").shift(5).over("symbol").alias("_c5"),
        pl.col("close").shift(250).over("symbol").alias("_c250"),
        pl.col("close").rolling_mean(20).over("symbol").alias("_ma20"),
        pl.col("close").rolling_mean(60).over("symbol").alias("_ma60"),
        pl.col("close").rolling_mean(120).over("symbol").alias("_ma120"),
        pl.col("close").rolling_mean(250).over("symbol").alias("_ma250"),
        pl.col("high").rolling_max(60).over("symbol").alias("_hi60"),
        pl.col("low").rolling_min(250).over("symbol").alias("_lo250"),
        pl.col("low").rolling_min(120).over("symbol").alias("_lo120"),
        pl.col("low").rolling_min(200).over("symbol").alias("_lo200"),
        pl.col("low").rolling_min(300).over("symbol").alias("_lo300"),
        pl.when(pl.col("ret1") > 0).then(pl.col("volume")).otherwise(0.0)
            .alias("_upvol"),
    )
    px = px.with_columns(
        (pl.col("close") / pl.col("_c5") - 1).alias("r5"),
        (pl.col("close") / pl.col("_c250") - 1).alias("r250"),
        (pl.col("close") / pl.col("_ma20") - 1).alias("dist_ma20"),
        (pl.col("close") / pl.col("_ma60") - 1).alias("dist_ma60"),
        (pl.col("close") / pl.col("_ma120") - 1).alias("dist_ma120"),
        (pl.col("close") / pl.col("_ma250") - 1).alias("dist_ma250"),
        (pl.col("close") / pl.col("_hi60") - 1).alias("dist_hi60"),
        (pl.col("close") / pl.col("_lo250") - 1).alias("dist_lo250"),
        (pl.col("close") / pl.col("_lo120") - 1).alias("dist_lo120"),
        (pl.col("close") / pl.col("_lo200") - 1).alias("dist_lo200"),
        (pl.col("close") / pl.col("_lo300") - 1).alias("dist_lo300"),
        ((pl.col("close") - pl.col("_lo250"))
         / (pl.col("_hi250") - pl.col("_lo250")).clip(lower_bound=1e-9))
            .alias("pos_range250"),
    ).drop(["_c5", "_c250", "_ma20", "_ma60", "_ma120", "_ma250", "_hi60",
            "_lo250", "_lo120", "_lo200", "_lo300"])
    px = px.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("close")).rolling_mean(60)
            .over("symbol").alias("amp60"),
        pl.col("ret1").rolling_std(20).over("symbol").alias("vol20"),
        pl.col("ret1").rolling_std(60).over("symbol").alias("vol60"),
        pl.col("ret1").rolling_skew(20).over("symbol").alias("skew20"),
        ((pl.col("close") - pl.col("open")).abs()
         / (pl.col("high") - pl.col("low")).clip(lower_bound=1e-9))
            .rolling_mean(20).over("symbol").alias("body20"),
        ((pl.col("open") / pl.col("_c1") - 1).abs()).rolling_mean(20)
            .over("symbol").alias("gap20"),
        (pl.col("_upvol").rolling_sum(20).over("symbol")
         / pl.col("volume").rolling_sum(20).over("symbol").clip(lower_bound=1e-9))
            .alias("updn_vol20"),
        (pl.col("ret1") > 0).cast(pl.Float64).rolling_mean(20)
            .over("symbol").alias("updays20"),
        (pl.col("ret1") >= 0.095).cast(pl.Float64).rolling_sum(60)
            .over("symbol").alias("n_bigup60"),
        pl.col("turnover_rate").rolling_mean(5).over("symbol").alias("to5"),
        pl.col("turnover_rate").rolling_mean(60).over("symbol").alias("to60"),
        pl.col("turnover_rate").rolling_std(20).over("symbol").alias("to_std20"),
        pl.col("amount").rolling_mean(250).over("symbol").alias("adv250"),
    )
    px = px.with_columns(
        (pl.col("to5") / pl.col("to60").clip(lower_bound=1e-9) - 1).alias("to_trend"),
        (pl.col("to_std20") / pl.col("to20").clip(lower_bound=1e-9)).alias("turnover_cv20"),
        (pl.col("adv20") / pl.col("adv250").clip(lower_bound=1e-9)).alias("amt_share20"),
    ).drop(["_c1", "_upvol", "_hi250", "to_std20", "adv250"])

    # ---- 筹码状态（逐股向量化，numpy 分组，避免 5,889 次 polars group_by）----
    ev_start = cal.filter(pl.col("date") >= pl.lit(EVAL_START).str.to_date())["rn"].min()
    ev_mask = px.select(
        (pl.col("rn") >= ev_start) & (pl.col("rn") % EVAL_STEP == 0)
        & (pl.col("hist_n") >= MIN_HIST)
    ).to_series().to_numpy()
    px = px.sort(["symbol", "date"])
    sym_a = px["symbol"].to_numpy()
    brk = np.flatnonzero(sym_a[1:] != sym_a[:-1]) + 1
    starts = np.r_[0, brk]
    ends = np.r_[brk, len(sym_a)]
    ev_pos = np.flatnonzero(ev_mask)
    date_a = px["date"].to_numpy()
    c_a, h_a, l_a = (px[c].to_numpy().astype(float) for c in ("close", "high", "low"))
    v_a, t_a = px["volume"].to_numpy().astype(float), px["turnover_rate"].to_numpy().astype(float)

    out = {k: [] for k in ("symbol", "date", "os", "os20", "fe", "fe20",
                           "profit", "near_mass", "cost_gap")}
    n_ev = 0
    for i in range(len(starts)):
        s, e = starts[i], ends[i]
        g0 = np.searchsorted(ev_pos, s)
        g1 = np.searchsorted(ev_pos, e)
        if g1 <= g0:
            continue
        idx = ev_pos[g0:g1] - s
        st = chip_state(c_a[s:e], h_a[s:e], l_a[s:e], v_a[s:e], t_a[s:e], idx)
        m = g1 - g0
        out["symbol"].append(np.repeat(sym_a[s], m))
        out["date"].append(date_a[ev_pos[g0:g1]])
        for k, v in st.items():
            out[k].append(v)
        n_ev += m
    chip = pl.DataFrame({
        "symbol": np.concatenate(out["symbol"]),
        "date": np.concatenate(out["date"]),
        **{k: np.concatenate(out[k]) for k in out if k not in ("symbol", "date")},
    })
    print(f"chip rows={chip.height:,} (expected {n_ev:,}) [{time.time()-t0:.0f}s]", flush=True)

    panel = px.filter(pl.Series(ev_mask)).drop(
        ["turnover_rate", "volume", "amount", "open"]
    ).join(chip, on=["symbol", "date"], how="left")

    # ---- 前向标签（rn 精确定位，稠密矩阵一次算完）----
    uniq, inv = np.unique(sym_a, return_inverse=True)
    all_syms = list(uniq)
    rn_min, rn_max = int(cal["rn"].min()), int(cal["rn"].max())
    sii, rii = inv, px["rn"].to_numpy() - rn_min
    shape = (len(all_syms), rn_max - rn_min + 1)
    mat = np.full(shape, np.nan, dtype=np.float64)
    mat[sii, rii] = c_a
    mat_o = np.full(shape, np.nan, dtype=np.float64)
    mat_o[sii, rii] = px["open"].to_numpy().astype(float)

    def fwd_path(h: int):
        """未来 h 日的 (最高收盘, 最低收盘, 第 h 日收盘, 次日开盘)。"""
        fmax = np.full(shape, np.nan)
        fmin = np.full(shape, np.nan)
        acc = np.full(shape, np.nan)
        accm = np.full(shape, np.nan)
        for k in range(1, h + 1):
            r = np.roll(mat, -k, axis=1)
            acc = np.fmax(acc, r)
            accm = np.fmin(accm, r)
        fmax[:, :-h] = acc[:, :-h]
        fmin[:, :-h] = accm[:, :-h]
        end = np.full(shape, np.nan)
        end[:, :-h] = np.roll(mat, -h, axis=1)[:, :-h]
        nxt = np.full(shape, np.nan)
        nxt[:, :-1] = np.roll(mat_o, -1, axis=1)[:, :-1]
        cnt = np.zeros(shape)
        for k in range(1, h + 1):
            cnt[:, :-k] += np.isfinite(np.roll(mat, -k, axis=1))[:, :-k]
        return fmax, fmin, end, nxt, cnt

    fmax40, _, end40, _, cnt40 = fwd_path(40)
    fmax60, fmin60, end60, nxt_open, cnt60 = fwd_path(FWD)
    fmax80, _, end80, _, cnt80 = fwd_path(80)

    def fwd_max(m: np.ndarray, h: int) -> np.ndarray:
        out = np.full(shape, np.nan)
        acc_ = np.full(shape, np.nan)
        for k in range(1, h + 1):
            acc_ = np.fmax(acc_, np.roll(m, -k, axis=1))
        out[:, :-h] = acc_[:, :-h]
        return out

    # 「快拉」：未来 60 日内**起点不早于 t** 的 20 日窗口涨幅 ≥ 阈值。
    # 旧写法 max_{h≤60} C[t+h]/C[t+h−20]−1 会把信号日之前已发生的涨幅算进来，
    # 等于把「已经在涨」当成「将被拉升」。这里限制窗口起点 a∈[0,40]。
    g20 = fwd_max(mat, 20) / mat - 1.0
    acc = g20.copy()
    for k in range(1, FWD - 20 + 1):
        acc = np.fmax(acc, np.roll(g20, -k, axis=1))
    fastc = np.full(shape, np.nan)
    fastc[:, :-FWD] = acc[:, :-FWD]

    # 窗口完整性：该股最后有效 rn 必须 ≥ t+h，且窗口内至少 2/3 个有效价
    valid = np.isfinite(mat)
    last_valid = np.where(valid.any(axis=1),
                          valid.shape[1] - 1 - np.argmax(valid[:, ::-1], axis=1), -1)

    panel = panel.with_columns(
        pl.col("rn").cast(pl.Int64),
        pl.col("hist_n").cast(pl.Int64),
        pl.col("last_rn").cast(pl.Int64),
    )
    pi = np.searchsorted(np.array(all_syms), panel["symbol"].to_numpy())
    pr = panel["rn"].to_numpy() - rn_min
    rn_arr, last_arr = panel["rn"].to_numpy(), panel["last_rn"].to_numpy()

    def ok_h(h, cnt):
        return (last_arr >= rn_arr + h) & (cnt[pi, pr] >= int(0.66 * h))

    full = ok_h(FWD, cnt60)
    full40, full80 = ok_h(40, cnt40), ok_h(80, cnt80)
    c0 = panel["close"].to_numpy()
    fmax = np.where(full, fmax60[pi, pr] / c0 - 1.0, np.nan)
    fast = np.where(full, fastc[pi, pr], np.nan)
    ret60 = np.where(full, end60[pi, pr] / c0 - 1.0, np.nan)
    ret60_oc = np.where(full, end60[pi, pr] / nxt_open[pi, pr] - 1.0, np.nan)
    min60 = np.where(full, fmin60[pi, pr] / c0 - 1.0, np.nan)
    ret40 = np.where(full40, end40[pi, pr] / c0 - 1.0, np.nan)
    mx40 = np.where(full40, fmax40[pi, pr] / c0 - 1.0, np.nan)
    ret80 = np.where(full80, end80[pi, pr] / c0 - 1.0, np.nan)
    mx80 = np.where(full80, fmax80[pi, pr] / c0 - 1.0, np.nan)

    panel = panel.with_columns(
        pl.Series("fwd_max60", fmax),
        pl.Series("fwd_fast", fast),
        pl.Series("fwd_ret60", ret60),
        pl.Series("fwd_ret60_oc", ret60_oc),
        pl.Series("fwd_min60", min60),
        pl.Series("fwd_ret40", ret40),
        pl.Series("fwd_max40", mx40),
        pl.Series("fwd_ret80", ret80),
        pl.Series("fwd_max80", mx80),
        pl.Series("full_window", full),
        pl.Series("fwd_nobs", cnt60[pi, pr]),
    ).with_columns(
        (pl.col("fwd_max60") >= 0.30).cast(pl.Int8).alias("pump30"),
        (pl.col("fwd_max60") >= 0.20).cast(pl.Int8).alias("pump20"),
        (pl.col("fwd_max60") >= 0.50).cast(pl.Int8).alias("pump50"),
        (pl.col("fwd_fast") >= 0.30).cast(pl.Int8).alias("fast30"),
        (pl.col("fwd_max40") >= 0.30).cast(pl.Int8).alias("pump30_40"),
        (pl.col("fwd_max80") >= 0.30).cast(pl.Int8).alias("pump30_80"),
    )

    panel.write_parquet(OUT / "panel.parquet")
    print(f"panel={panel.height:,} cols={len(panel.columns)} → {OUT/'panel.parquet'} "
          f"[{time.time()-t0:.0f}s]", flush=True)
    print(panel.select(
        pl.len().alias("n"), pl.col("os").is_not_null().sum().alias("os_ok"),
        pl.col("fwd_max60").is_not_null().sum().alias("lbl_ok"),
        pl.col("full_window").mean().alias("full_win_share"),
    ).to_pandas().to_string())


if __name__ == "__main__":
    main()
