"""沉默事件研究（匹配基准版）：跌入 silent 的因果效应，用小盘/低流动性匹配对照。

核心问题（可证伪）：silent 的 +606bp 超额，是「低关注度本身有因果 alpha」，
还是「silent 只是小盘 + 低流动性的投影」？

方法：事件研究 + 倾向得分匹配（PSM）做差分（DiD）。
  1. 事件 = 某票首次「跌入 silent」（前一日非 silent，当日 silent）。
  2. 对每个事件票，在**同一日**找一个「倾向得分」最接近、但**未进入 silent** 的对照票。
     倾向得分用事件日**前**可得的特征估计（成交额分位、换手率分位、20 日动量、20 日波动率）——
     这些都不需要时点市值，正是本方法被选中的原因。
  3. 差分 CAR = 事件票 CAR − 对照票 CAR。若「沉默有因果」→ 差分 CAR 在事件后持续为正；
     若「沉默只是投影」→ 差分 CAR 在事件后趋近 0（小盘/低流动性溢价被对照票抵消）。

为什么上一版（全市场中位数基准）是错的：
  silent 票成交额中位数只有全市场的 12%，是典型小盘。「相对全市场中位数」的累计超额
  天然被小盘效应逐日漂移放大（实测 +2904bp/60 日，荒谬），那不是在测「沉默」，是在重测
  「小盘跑赢大盘」。匹配基准把这一层消掉，剩下的才是「沉默」的边际。

口径（对齐 citizenship / probe_decile_net）：
  - 等级 = 当日横截面三轴分位（amount / turnover 的 ordinal rank），与 citizenship 同规则；
  - 动量 = 20 日累计对数收益（前复权 close）；波动率 = 20 日对数收益 std；
  - 倾向得分 = 逻辑回归（手写，scipy 不在 venv），特征标准化后拟合；
  - 匹配 = 同日 1:1 最近邻（倾向得分欧氏距离），不重复使用对照票（不放回）；
  - 差分 CAR 的标准误用 block bootstrap（块长 5，事件间近似独立）。

输出：data/.scratch/silent_event_study.json
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
LOOKBACK = 20  # 动量/波动率回看窗口


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
    cols = ["symbol", "date", "close", "amount", "turnover_rate"]
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
    """加逐日收益、动量、波动率、分位特征（全部只用到当日及以前信息，无前视）。"""
    # 对数收益
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("log_ret")
    )
    # 20 日动量（累计对数收益）与波动率（std），都用过去 20 日（含当日）
    df = df.with_columns(
        pl.col("log_ret").rolling_sum(window_size=LOOKBACK, min_periods=LOOKBACK)
        .over("symbol").alias("mom"),
        pl.col("log_ret").rolling_std(window_size=LOOKBACK, min_periods=LOOKBACK)
        .over("symbol").alias("vol"),
    )
    # 当日横截面分位（成交额、换手率），0~1
    df = df.with_columns([
        ((pl.col("amount").rank("ordinal").over("date") - 1)
         / (pl.col("amount").count().over("date") - 1).clip(lower_bound=1)).alias("p_amt"),
        ((pl.col("turnover_rate").rank("ordinal").over("date") - 1)
         / (pl.col("turnover_rate").count().over("date") - 1).clip(lower_bound=1)).alias("p_turn"),
    ])
    return df


def find_events(df: pl.DataFrame) -> pl.DataFrame:
    """首次「跌入 silent」事件。要求事件日前后各有 PRE/POST 数据。"""
    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))
    entry = df.filter(
        (pl.col("level") == "silent") & (pl.col("prev_level") != "silent")
        & pl.col("prev_level").is_not_null()
    )
    events = entry.sort("date").group_by("symbol").first()
    # 特征（事件日前一日，即 prev 的特征，严格无前视）取事件日 t=0 当日的特征作为匹配变量
    # 注意：匹配特征必须在事件日当日收盘可知（等级也是当日收盘算的），故用当日 p_amt/p_turn/mom/vol
    return events.select(["symbol", "date", "prev_level"])


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _logreg_fit(X: np.ndarray, y: np.ndarray, iters: int = 200, lr: float = 0.1) -> np.ndarray:
    """手写逻辑回归（梯度下降），返回系数。scipy 不在 venv。"""
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
    """同日 1:1 最近邻匹配（倾向得分），返回 (event_symbol, date, ctrl_symbol)。

    倾向得分：logit(进入 silent) ~ [p_amt, p_turn, mom, vol]（标准化）。

    关键（防前视）：匹配特征必须用**事件日前一日（t-1）**的特征，而不是事件日当日。
    理由：silent 的定义本身就是「成交额后 10% 且换手率后 10%」，所以事件日当日的
    p_amt/p_turn 已被锁死在 <0.1，用它们匹配 = 「用结果匹配结果」，会把 +488bp 的
    结论抬成假阳性。用 t-1 特征，票在那一刻还没沉默、分位还是「正常」的，才能匹配到
    「本来相似、一只后来沉默、另一只没有」的可比对象。

    对照池 = 事件日当日 level != silent 且 prev_level != silent（「稳定非沉默」）。
    对照的匹配特征同样取 t-1（即对照票在 t-1 的分位/动量/波动率）。
    """
    # 事件票的 t-1 特征（用 shift 取前一日；对事件票，t-1 即 prev 日）
    feat_cols = ["p_amt", "p_turn", "mom", "vol"]
    prev = df.with_columns([
        pl.col(c).shift(1).over("symbol").alias(f"pre_{c}") for c in feat_cols
    ]).select(["symbol", "date"] + [f"pre_{c}" for c in feat_cols])

    ev = events.join(prev, on=["symbol", "date"], how="inner")
    # 事件票的特征列名统一回 feat_cols
    ev = ev.rename({f"pre_{c}": c for c in feat_cols})

    # 对照池：当日非 silent 且前一日也非 silent；特征取 t-1
    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))
    ctrl_pool = df.filter(
        (pl.col("level") != "silent") & (pl.col("prev_level") != "silent")
        & pl.col("prev_level").is_not_null()
    ).select(["symbol", "date", "level"])
    ctrl_pool = ctrl_pool.join(prev, on=["symbol", "date"], how="inner")
    ctrl_pool = ctrl_pool.rename({f"pre_{c}": c for c in feat_cols})

    # 剔除特征为 null 的行（t-1 缺失，如上市首日）
    ev = ev.drop_nulls(subset=feat_cols)
    ctrl_pool = ctrl_pool.drop_nulls(subset=feat_cols)

    # 特征标准化（用事件+对照并集算 mean/std，避免只用事件组）
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

    # 逻辑回归：y=1 表示「事件票」（进入 silent），X 是标准化特征
    X_all = np.column_stack([all_rows[f].to_numpy() for f in feats])
    y_all = np.concatenate([
        np.ones(ev.height), np.zeros(ctrl_pool.height),
    ])
    w = _logreg_fit(X_all, y_all)
    ps = _sigmoid(X_all @ w)
    ev_ps = ps[: ev.height]
    ctrl_ps = ps[ev.height:]

    # 同日匹配：向量化。按日期分组，每个日期内用 numpy 算事件×对照的 |Δps|，
    # 逐事件贪心取最近、不放回（对照票同一天只能配一个事件）。
    ev = ev.with_columns(pl.Series("ps", ev_ps))
    ctrl_pool = ctrl_pool.with_columns(pl.Series("ps", ctrl_ps))

    # 只保留「有事件」的日期（对照池从 1458 万行砍到事件日），再按日期分区。
    event_dates = ev["date"].unique()
    ctrl_pool = ctrl_pool.filter(pl.col("date").is_in(event_dates))

    # partition_by 按日期切分，避免 to_dicts 全量遍历
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
        dist = np.abs(e_pss[:, None] - c_pss[None, :])  # (n_event, n_ctrl)
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
                "ps_event": float(e_pss[i]),
                "ps_ctrl": float(c_pss[chosen]),
            })
    return pl.DataFrame(matches)


def build_diff_car(df: pl.DataFrame, matches: pl.DataFrame) -> pl.DataFrame:
    """对每对 (event, ctrl)，取事件日前后窗口的逐日超额收益，做差分。

    逐日超额 = 个股日收益 − 当日全市场中位数（两边同减，差分时中位数项抵消，
    真正起作用的正是「事件票 vs 对照票」的差）。
    """
    # 逐日收益与全市场中位数
    df = df.with_columns(
        (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1.0).alias("ret1")
    )
    med = df.group_by("date").agg(pl.col("ret1").median().alias("med"))
    df = df.join(med, on="date").with_columns(
        (pl.col("ret1") - pl.col("med")).alias("exc1")
    ).drop("med")
    df = df.filter(pl.col("exc1").is_not_null())

    # symbol -> (dates, exc1)：只对匹配涉及到的 symbol 建索引，避免 5580 次全表 filter
    need_syms = set(matches["event_symbol"].to_list()) | set(matches["ctrl_symbol"].to_list())
    need = df.filter(pl.col("symbol").is_in(list(need_syms)))
    sym_data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym in need["symbol"].unique().to_list():
        seg = need.filter(pl.col("symbol") == sym).sort("date")
        sym_data[sym] = (seg["date"].to_numpy(), seg["exc1"].to_numpy())

    rows = []
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
        for t in range(-PRE, POST + 1):
            diff = float(eexc[epos + t] - cexc[cpos + t])
            rows.append({"pair_id": len(rows) // (PRE + POST + 1), "t": t, "diff": diff})
    if not rows:
        return pl.DataFrame(schema={"pair_id": pl.Int64, "t": pl.Int64, "diff": pl.Float64})
    return pl.DataFrame(rows)


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    print(f"  {df.height:,} 行  {df['date'].min()} → {df['date'].max()}", flush=True)
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
    )
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
    diff = build_diff_car(df, matches)
    print(f"  差分样本 {diff.height:,} 行", flush=True)

    # 每对累计差分
    diff = diff.sort(["pair_id", "t"]).with_columns(
        pl.col("diff").cum_sum().over("pair_id").alias("car")
    )
    # 每个 t 的平均差分 CAR
    avg = diff.group_by("t").agg(
        pl.col("diff").mean().alias("mean_diff"),
        pl.col("car").mean().alias("mean_car"),
        pl.col("diff").len().alias("n"),
    ).sort("t")

    car_rows = []
    for t in avg["t"].to_list():
        seg = diff.filter(pl.col("t") == t)
        car_val = float(seg["car"].mean())
        se = block_se(seg["car"].to_numpy(), block=5)
        car_rows.append({
            "t": int(t),
            "mean_diff_bp": float(avg.filter(pl.col("t") == t)["mean_diff"][0] * 1e4),
            "car_bp": float(car_val * 1e4),
            "car_se_bp": float(se * 1e4),
            "n_pairs": int(seg.height),
        })

    # 关键读数
    base = diff.filter(pl.col("t") == 0)["car"].mean()
    pre = diff.filter(pl.col("t") == -1)["car"].mean()
    post = diff.filter(pl.col("t") == POST)["car"].mean()

    report = {
        "n_events": int(events.height),
        "n_matched": int(matches.height),
        "window": {"pre": PRE, "post": POST},
        "car_series": car_rows,
        "pre_car_bp": float(pre * 1e4),
        "pre_car_se_bp": float(block_se(diff.filter(pl.col("t") == -1)["car"].to_numpy(), 5) * 1e4),
        "post_car_bp": float(post * 1e4),
        "post_car_se_bp": float(block_se(diff.filter(pl.col("t") == POST)["car"].to_numpy(), 5) * 1e4),
        "post_inc_bp": float((post - base) * 1e4),
    }

    print("\n=== 匹配基准下，跌入 silent 的差分累计超额（CAR, bp）===", flush=True)
    print("  t     ΔCAR(bp)  n_pairs")
    for r in car_rows:
        if r["t"] in (-PRE, -10, -1, 0, 1, 10, 20, 40, POST):
            print(f"  {r['t']:>4d}  {r['car_bp']:+8.1f}   {r['n_pairs']:,}")

    print("\n=== 因果方向判读（差分基准，小盘/低流动性已抵消）===", flush=True)
    pre_bp = report["pre_car_bp"]
    post_bp = report["post_car_bp"]
    post_inc = report["post_inc_bp"]
    print(f"  事件前差分 CAR(-{PRE}..-1) = {pre_bp:+.1f} bp")
    print(f"  事件后差分 CAR(+1..+{POST}) = {post_bp:+.1f} bp")
    print(f"  事件后增量（相对事件日）= {post_inc:+.1f} bp")
    print()
    if abs(post_inc) < 50 and abs(pre_bp) < 50:
        print("  → 差分 CAR 接近 0：silent 超额被小盘/低流动性**完全解释**，")
        print("     「沉默」本身没有独立 alpha（支持「沉默是投影，非因果」）。", flush=True)
    elif post_inc > 100:
        print("  → 事件后差分持续为正：沉默本身有独立边际 alpha。", flush=True)
    else:
        print("  → 结果介于两者之间，需进一步拆解（见报告）。", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "silent_event_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/silent_event_study.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
