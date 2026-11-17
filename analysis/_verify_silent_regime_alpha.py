"""沉默公民的 regime 条件 alpha：牛市/题材轮动期，silent 是否有明显超额？

上一轮结论（_verify_silent_event_study.py）：silent 的 +606bp 超额是小盘+低流动性的
投影，拆掉后事件后差分 CAR ≈ −134bp，全样本平均意义下「沉默无 alpha」。

本脚本攻击上一轮结论的隐藏假设：**「无 alpha」是全样本平均，是否在特定市场状态下反转？**
A 股存在「牛市末期/情绪亢奋期低流动性小盘补涨」（消灭低价股、鸡犬升天）的直觉，
如果真实，silent 的超额应在「牛市 + 高轮动热度」regime 里反转为正。

方法：沿用事件研究 + PSM（防前视）框架，但加一个 regime 交互项 ——
按事件发生当日的市场状态分层，重算差分 CAR，检验 silent 的边际 alpha 是否条件依赖状态。

市场状态标签（严格用当日及以前信息，无前视）：
  mkt_ret  全市场日收益的横截面中位数（当日）
  bull_rank  mkt_ret 在过去 250 交易日的滚动分位（0~1，>0.5 为偏强）—— 滚动窗口保证无前视
  heat      题材轮动强度 = 当日涨停家数 z 分 + 连板高度（当日，情绪是同步信号）
  regime    组合桶：bull(滚动分位>0.7) × heat(涨停数>滚动中位) 的四象限 + 中性

数据：kline_daily_enriched（2005–2026，5310 交易日），与上轮同源。
输出：data/.scratch/silent_regime_alpha.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"

CORE_MIN_AMOUNT = 0.90
ACTIVE_MIN_AMOUNT = 0.50
ACTIVE_MIN_TURNOVER = 0.90
SILENT_MAX_AMOUNT = 0.10
SILENT_MAX_TURNOVER = 0.10

PRE = 20
POST = 60
LOOKBACK = 20
ROLL = 250  # 市场状态滚动分位窗口（约一年）


def block_se(x: np.ndarray, block: int, reps: int = 1000, seed: int = 0) -> float:
    n = len(x)
    if n < block:
        block = max(1, n)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[i] = x[idx.ravel()[:n]].mean()
    return float(means.std(ddof=1))


def load_history() -> pl.DataFrame:
    files = sorted(str(p) for p in TABLE.glob("date=*/part.parquet"))
    cols = ["symbol", "date", "close", "amount", "turnover_rate", "consecutive_limit_ups"]
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )


def assign_level(d: pl.DataFrame) -> pl.DataFrame:
    n = d.height
    if n < 2:
        return d
    a = (pl.col("amount").rank("ordinal") - 1) / (n - 1)
    t = (pl.col("turnover_rate").rank("ordinal") - 1) / (n - 1)
    level = (
        pl.when(a >= CORE_MIN_AMOUNT).then(pl.lit("core"))
        .when((a >= ACTIVE_MIN_AMOUNT) | (t >= ACTIVE_MIN_TURNOVER)).then(pl.lit("active"))
        .when((a < SILENT_MAX_AMOUNT) & (t < SILENT_MAX_TURNOVER)).then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
    )
    return d.with_columns(level.alias("level"))


def enrich(df: pl.DataFrame) -> pl.DataFrame:
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("log_ret")
    )
    df = df.with_columns(
        pl.col("log_ret").rolling_sum(window_size=LOOKBACK, min_periods=LOOKBACK)
        .over("symbol").alias("mom"),
        pl.col("log_ret").rolling_std(window_size=LOOKBACK, min_periods=LOOKBACK)
        .over("symbol").alias("vol"),
    )
    df = df.with_columns([
        ((pl.col("amount").rank("ordinal").over("date") - 1)
         / (pl.col("amount").count().over("date") - 1).clip(lower_bound=1)).alias("p_amt"),
        ((pl.col("turnover_rate").rank("ordinal").over("date") - 1)
         / (pl.col("turnover_rate").count().over("date") - 1).clip(lower_bound=1)).alias("p_turn"),
    ])
    return df


def build_regime(df: pl.DataFrame) -> pl.DataFrame:
    """构造每日市场状态标签（无前视）。

    mkt_ret: 全市场日收益的横截面中位数（当日）。
    bull_rank: mkt_ret 在过去 ROLL 交易日的滚动分位（>0.5 偏强）。
    heat: 题材轮动强度 = 当日涨停家数（consecutive_limit_ups>=1）。
    regime: 四象限桶。
    """
    # 日收益（对数收益，前复权 close）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("log_ret")
    )
    daily = (
        df.group_by("date")
        .agg(
            pl.col("log_ret").median().alias("mkt_ret"),
            (pl.col("consecutive_limit_ups") >= 1).sum().alias("limit_up_count"),
            pl.col("consecutive_limit_ups").max().alias("max_consec"),
        )
        .sort("date")
    )
    # 滚动分位：mkt_ret 在过去 ROLL 日内的分位（用 rolling quantile 近似——
    # 更稳的做法是显式 rolling rank，但 polars 无原生 rolling rank；
    # 这里用「当前值 > 窗口内 (rank-1)/(n-1)」的等价实现太慢，改为：
    # 把 mkt_ret 转成 numpy，逐日算滚动分位，5310 天 O(n*ROLL) 可接受）。
    mkt = daily["mkt_ret"].to_numpy()
    n = len(mkt)
    bull_rank = np.full(n, np.nan)
    heat_med = np.full(n, np.nan)
    lim = daily["limit_up_count"].to_numpy()
    for i in range(n):
        lo = max(0, i - ROLL + 1)
        window = mkt[lo:i + 1]
        if len(window) >= 60:
            bull_rank[i] = float((window < mkt[i]).mean()) + 0.5 * float((window == mkt[i]).sum() - 1) / max(len(window) - 1, 1)
        wl = lim[lo:i + 1]
        if len(wl) >= 60:
            heat_med[i] = float(np.median(wl))
    daily = daily.with_columns(
        pl.Series("bull_rank", bull_rank),
        pl.Series("heat_med", heat_med),
    )
    daily = daily.with_columns(
        (pl.col("limit_up_count") > pl.col("heat_med")).alias("hot")
    )
    daily = daily.with_columns(
        pl.when(pl.col("bull_rank") > 0.7).then(pl.lit("bull"))
        .when(pl.col("bull_rank") < 0.3).then(pl.lit("bear"))
        .otherwise(pl.lit("neutral"))
        .alias("market")
    )
    daily = daily.with_columns(
        (pl.col("market") + "_"
         + pl.when(pl.col("hot")).then(pl.lit("hot")).otherwise(pl.lit("cold"))).alias("regime")
    )
    return daily.select(["date", "mkt_ret", "bull_rank", "limit_up_count", "heat_med",
                         "hot", "market", "regime"])


def find_events(df: pl.DataFrame) -> pl.DataFrame:
    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))
    entry = df.filter(
        (pl.col("level") == "silent") & (pl.col("prev_level") != "silent")
        & pl.col("prev_level").is_not_null()
    )
    events = entry.sort("date").group_by("symbol").first()
    return events.select(["symbol", "date", "prev_level"])


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _logreg_fit(X: np.ndarray, y: np.ndarray, iters: int = 200, lr: float = 0.1) -> np.ndarray:
    n, p = X.shape
    w = np.zeros(p)
    y = y.astype(np.float64)
    for _ in range(iters):
        z = X @ w
        pred = _sigmoid(z)
        grad = X.T @ (pred - y) / n
        w -= lr * grad
    return w


def match_controls(events: pl.DataFrame, df: pl.DataFrame) -> pl.DataFrame:
    feat_cols = ["p_amt", "p_turn", "mom", "vol"]
    prev = df.with_columns([
        pl.col(c).shift(1).over("symbol").alias(f"pre_{c}") for c in feat_cols
    ]).select(["symbol", "date"] + [f"pre_{c}" for c in feat_cols])

    ev = events.join(prev, on=["symbol", "date"], how="inner")
    ev = ev.rename({f"pre_{c}": c for c in feat_cols})

    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))
    ctrl_pool = df.filter(
        (pl.col("level") != "silent") & (pl.col("prev_level") != "silent")
        & pl.col("prev_level").is_not_null()
    ).select(["symbol", "date", "level"])
    ctrl_pool = ctrl_pool.join(prev, on=["symbol", "date"], how="inner")
    ctrl_pool = ctrl_pool.rename({f"pre_{c}": c for c in feat_cols})

    ev = ev.drop_nulls(subset=feat_cols)
    ctrl_pool = ctrl_pool.drop_nulls(subset=feat_cols)

    feats = ["p_amt", "p_turn", "mom", "vol"]
    all_rows = pl.concat([
        ev.select(["date"] + feats),
        ctrl_pool.select(["date"] + feats),
    ])
    mean = {f: float(all_rows[f].mean()) for f in feats}
    std = {f: float(all_rows[f].std()) for f in feats}
    for f in feats:
        if std[f] == 0:
            std[f] = 1.0

    def z(dfx: pl.DataFrame) -> pl.DataFrame:
        for f in feats:
            dfx = dfx.with_columns(((pl.col(f) - mean[f]) / std[f]).alias(f))
        return dfx

    ev = z(ev)
    ctrl_pool = z(ctrl_pool)

    X_all = np.column_stack([all_rows[f].to_numpy() for f in feats])
    y_all = np.concatenate([np.ones(ev.height), np.zeros(ctrl_pool.height)])
    w = _logreg_fit(X_all, y_all)
    ps = _sigmoid(X_all @ w)
    ev_ps = ps[: ev.height]
    ctrl_ps = ps[ev.height:]

    ev = ev.with_columns(pl.Series("ps", ev_ps))
    ctrl_pool = ctrl_pool.with_columns(pl.Series("ps", ctrl_ps))

    event_dates = ev["date"].unique()
    ctrl_pool = ctrl_pool.filter(pl.col("date").is_in(event_dates))

    ev_parts = ev.partition_by("date", maintain_order=True)
    ctrl_parts = ctrl_pool.partition_by("date", maintain_order=True)
    ev_map = {p["date"][0]: p for p in ev_parts}
    ctrl_map = {p["date"][0]: p for p in ctrl_parts}

    matches = []
    for d, ev_part in ev_map.items():
        ctrl_part = ctrl_map.get(d)
        if ctrl_part is None or ctrl_part.is_empty():
            continue
        e_syms = ev_part["symbol"].to_list()
        e_pss = ev_part["ps"].to_numpy()
        c_syms = ctrl_part["symbol"].to_list()
        c_pss = ctrl_part["ps"].to_numpy()
        dist = np.abs(e_pss[:, None] - c_pss[None, :])
        used: set = set()
        for i in range(len(e_syms)):
            order = np.argsort(dist[i])
            chosen = -1
            for j in order:
                jj = int(j)
                if jj not in used:
                    chosen = jj
                    break
            if chosen < 0:
                continue
            used.add(chosen)
            matches.append({
                "event_symbol": e_syms[i],
                "date": d,
                "ctrl_symbol": c_syms[chosen],
            })
    return pl.DataFrame(matches)


def build_diff_car(df: pl.DataFrame, matches: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    df = df.with_columns(
        (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1.0).alias("ret1")
    )
    med = df.group_by("date").agg(pl.col("ret1").median().alias("med"))
    df = df.join(med, on="date").with_columns(
        (pl.col("ret1") - pl.col("med")).alias("exc1")
    ).drop("med")
    df = df.filter(pl.col("exc1").is_not_null())

    need_syms = set(matches["event_symbol"].to_list()) | set(matches["ctrl_symbol"].to_list())
    need = df.filter(pl.col("symbol").is_in(list(need_syms)))
    sym_data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym in need["symbol"].unique().to_list():
        seg = need.filter(pl.col("symbol") == sym).sort("date")
        sym_data[sym] = (seg["date"].to_numpy(), seg["exc1"].to_numpy())

    rows = []
    pair_meta: dict[int, tuple[str, object]] = {}  # pair_id -> (event_symbol, date)
    for m in matches.to_dicts():
        d0 = m["date"]
        esym, csym = m["event_symbol"], m["ctrl_symbol"]
        if esym not in sym_data or csym not in sym_data:
            continue
        edates, eexc = sym_data[esym]
        cdates, cexc = sym_data[csym]
        epos = np.searchsorted(edates, d0)
        cpos = np.searchsorted(cdates, d0)
        if epos >= len(edates) or edates[epos] != d0:
            continue
        if cpos >= len(cdates) or cdates[cpos] != d0:
            continue
        if epos - PRE < 0 or epos + POST >= len(edates):
            continue
        if cpos - PRE < 0 or cpos + POST >= len(cdates):
            continue
        pid = len(rows) // (PRE + POST + 1)
        pair_meta[pid] = (esym, d0)
        for t in range(-PRE, POST + 1):
            diff = float(eexc[epos + t] - cexc[cpos + t])
            rows.append({"pair_id": pid, "t": t, "diff": diff})
    if not rows:
        return pl.DataFrame(schema={"pair_id": pl.Int64, "t": pl.Int64, "diff": pl.Float64}), pl.DataFrame()
    meta_df = pl.DataFrame({
        "pair_id": list(pair_meta.keys()),
        "event_symbol": [v[0] for v in pair_meta.values()],
        "date": [v[1] for v in pair_meta.values()],
    })
    return pl.DataFrame(rows), meta_df


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    print(f"  {df.height:,} 行  {df['date'].min()} → {df['date'].max()}", flush=True)
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
    )

    print("构造市场状态标签…", flush=True)
    regime = build_regime(df)
    print(f"  regime 日数 {regime.height:,}", flush=True)

    print("逐日算等级…", flush=True)
    df = df.group_by("date", maintain_order=True).map_groups(assign_level)
    print("加动量/波动率/分位特征…", flush=True)
    df = enrich(df)

    print("识别跌入 silent 事件…", flush=True)
    events = find_events(df)
    print(f"  事件数 {events.height:,}", flush=True)

    print("倾向得分 + 同日 1:1 匹配…", flush=True)
    matches = match_controls(events, df)
    print(f"  成功匹配 {matches.height:,} 对", flush=True)

    print("算差分 CAR…", flush=True)
    diff, pair_meta = build_diff_car(df, matches)
    diff = diff.sort(["pair_id", "t"]).with_columns(
        pl.col("diff").cum_sum().over("pair_id").alias("car")
    )

    # 事件票的当日动量（前 20 日），join 进 pair_meta，供后续分层诊断
    ev_mom = events.join(
        df.select(["symbol", "date", "mom"]), on=["symbol", "date"], how="inner"
    ).select(["symbol", "date", "mom"]).rename({"symbol": "event_symbol", "mom": "ev_mom"})
    pair_meta = pair_meta.join(ev_mom, on=["event_symbol", "date"], how="left")

    # 把 regime 标签 join 到每个 pair（按事件日，无前视）
    regime_by_date = regime.select(["date", "regime", "market", "hot", "bull_rank", "limit_up_count"])
    pair_regime = pair_meta.join(regime_by_date, on="date", how="left")
    diff = diff.join(
        pair_regime.select(["pair_id", "regime", "market", "hot", "bull_rank", "ev_mom"]),
        on="pair_id", how="left",
    )

    # 落盘中间产物（诊断/后续分层直接读，避免重复全量计算）
    pair_regime.write_parquet(OUT / "silent_regime_pairs.parquet")
    diff.write_parquet(OUT / "silent_regime_diff.parquet")

    # 每个 regime 的事件后差分 CAR
    report = {"n_events": int(events.height), "n_matched": int(matches.height)}
    regime_rows = []
    for reg in sorted(diff["regime"].drop_nulls().unique().to_list()):
        seg = diff.filter(pl.col("regime") == reg)
        post = seg.filter(pl.col("t") == POST)["car"]
        pre = seg.filter(pl.col("t") == -1)["car"]
        if post.len() == 0:
            continue
        post_mean = float(post.mean() * 1e4)
        post_se = float(block_se(post.to_numpy(), 5) * 1e4)
        pre_mean = float(pre.mean() * 1e4)
        n_pairs = int(seg.filter(pl.col("t") == 0).height)
        regime_rows.append({
            "regime": reg,
            "n_pairs": n_pairs,
            "pre_car_bp": round(pre_mean, 1),
            "post_car_bp": round(post_mean, 1),
            "post_car_se_bp": round(post_se, 1),
            "post_t": round(post_mean / post_se, 2) if post_se > 0 else None,
        })
    report["by_regime"] = regime_rows

    # 全样本对照（复现上轮 -134bp）
    post_all = diff.filter(pl.col("t") == POST)["car"]
    report["all_post_car_bp"] = float(post_all.mean() * 1e4)
    report["all_post_car_se_bp"] = float(block_se(post_all.to_numpy(), 5) * 1e4)

    print("\n=== 分市场状态的 silent 事件后差分 CAR（bp，负=跑输对照）===")
    print(f"  {'regime':<18} {'n_pairs':>7} {'pre':>8} {'post':>8} {'se':>7} {'t':>6}")
    for r in regime_rows:
        print(f"  {r['regime']:<18} {r['n_pairs']:>7,} {r['pre_car_bp']:>+8.1f} "
              f"{r['post_car_bp']:>+8.1f} {r['post_car_se_bp']:>7.1f} {str(r['post_t']):>6}")
    print(f"  {'ALL(复现)':<18} {'':>7} {'':>8} {report['all_post_car_bp']:>+8.1f} "
          f"{report['all_post_car_se_bp']:>7.1f}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "silent_regime_alpha.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/silent_regime_alpha.json")


if __name__ == "__main__":
    sys.exit(main())
