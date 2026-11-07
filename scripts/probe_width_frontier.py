"""宽度 N 的 (alpha, t, 容量) 前沿：一个指数 λ 判「形态有没有救」。

上一轮把下一步定义成「N ∈ {20..500} 扫 H=60 低流动性净 alpha，看能不能转正」。
这个设计有一个会给出假阳性的漏洞：**基准选错**。

同一只「低换手 TOP20」篮子，相对**全池等权**的层溢价见本产物
`turnover_H60.layer_bp`（上一轮口头报过一个数，已不再在正文里复述——
散文里的数和产物里的数一旦分叉，谁也不知道该信哪个）。
相对**同层随机篮子**却只有几十 bp。差额不是动量，是「低流动性/小市值」这件事
本身——它宽而稳，抬高 N 不会让它消失，反而因分散化把 t 抬得更高。
于是「N=200 净值转正」会稳稳成立，而它证明的是「小市值在 A 股有溢价」，
不是「动量可用」。按那个结论去建 200 只票的组合，是做了一次风格暴露。

所以本脚本对每个 N 同时报两条基准：
  vs_pool   = 组合 − 当日全池等权        （产品相对基准的收益；含流动性溢价）
  vs_bucket = 组合 − 当日同层等权        （**动量自身的贡献**；这才是信号）
并且报第三条：bucket − pool（层溢价本身），它就是 287.7 与几十 bp 的差。

判定不用某个格子的正负，用**两条曲线**：
  1) 净 alpha 越过 0 的宽度 = breakeven_n（在 log N 上线性插值）。它是产品的
     最大可行宽度：再宽就只买到「层的中段」，超额被摊薄到成本以下。
  2) 衰减指数 λ：alpha(N) ∝ N^-λ。
     ⚠ λ 只在 N ≤ 100 时可信。层内中位 862 只，N=500 已占 58%，此时无论有没有
     信号，子集均值都会回归层均值——那是池子边界，不是衰减。早先一版用全区间
     拟合得到 λ≈1 并据此说「效应全在榜首一只」，这个推断是错的：曲线在 N=10→50
     是平的（+143→+124），一只票撑不起这一段。

容量同时算：AUM_max(N) = N × 20% × ADV_中位数(N)，即每只票不超过自身日成交额的 20%。

口径与 probe_decile_net 完全一致（同一面板缓存、同一前视、同一块长 2H）。
输出：data/.scratch/width_frontier.json
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_decile_net as P  # noqa: E402

OUT = P.OUT
COST_BP = P.COST_BP
NS = (10, 20, 50, 100, 200, 500)
PARTICIPATION = 0.20          # 单票下单额 / 日成交额
H = 60                        # 主口径；H=20 只作附注


def build_bucket(df: pl.DataFrame, key: str, h: int, frac: float = 0.2) -> pl.DataFrame:
    """取 key 的最底层 frac，层内按 mom 降序排名，返回逐日组合均值/层均值/ADV。"""
    fwd = f"fwd{h}"
    d = df.filter(
        pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
        & (pl.col("turnover") > 0) & pl.col(key).is_not_null() & (pl.col(key) > 0)
    ).with_columns(
        (((pl.col(key).rank("ordinal").over("date") - 1)
          / pl.col(key).count().over("date")) < frac).alias("in_b")
    )
    d = d.filter(pl.col("in_b")).with_columns(
        pl.col("mom").rank("ordinal", descending=True).over("date").alias("rk"),
        pl.col("mom").rank("ordinal", descending=False).over("date").alias("rk_lo"),
    )
    aggs = [
        pl.col(fwd).mean().alias("bucket_mu"),
        pl.col(fwd).median().alias("bucket_med"),
        # 去掉右尾 1% 后的层均值：均值−缩尾均值 就是「一只十倍股」的贡献。
        # ⚠ 这是**单边上尾**缩尾，名字里的 w99 只指上界。对称口径见 bucket_w01w99。
        pl.col(fwd).clip(upper_bound=pl.col(fwd).quantile(0.99)).mean().alias("bucket_w99"),
        pl.col(fwd).clip(lower_bound=pl.col(fwd).quantile(0.01),
                         upper_bound=pl.col(fwd).quantile(0.99)).mean().alias("bucket_w01w99"),
        pl.col("turnover").median().alias("bucket_adv"),
        pl.len().alias("bucket_n"),
        # 层内多空（不可做空，只用于诊断动量本身有没有方向）
        (pl.col(fwd).filter(pl.col("rk") <= 20).mean()
         - pl.col(fwd).filter(pl.col("rk_lo") <= 20).mean()).alias("ls20"),
    ]
    for n in NS:
        aggs.append(pl.col(fwd).filter(pl.col("rk") <= n).mean().alias(f"mu{n}"))
        aggs.append(pl.col(fwd).filter(pl.col("rk") <= n).median().alias(f"med{n}"))
        aggs.append(pl.col("turnover").filter(pl.col("rk") <= n).median().alias(f"adv{n}"))
    return d.group_by("date").agg(aggs).sort("date")


def pool_mean(df: pl.DataFrame, h: int) -> pl.DataFrame:
    """全池基准。除了均值还要给中位与缩尾均值——否则「层缩尾 vs 池不缩尾」是
    不对称比较：A 股 60 日收益本身极度右偏。

    三个统计量必须分清，混一个就会算错 57bp（2026-11-08 更正）：
      pool_mu               逐日截面均值，再对日期取**均值**
      pool_med              逐日截面中位，再对日期取**均值**（参与恒等式的那个）
      pool_med_of_daily_med 逐日截面中位，再对日期取**中位**（稳健，但不闭合）
    `pool_mean_minus_median×1e4 = mean(pool_mu) − mean(pool_med)`，两边的
    时间聚合都必须是均值。初稿把「日度池中位的中位」写进了这句恒等式旁边，
    与产物差 57bp——产物没错，是注释把两种时间聚合当成了同一个数。"""
    fwd = f"fwd{h}"
    return (df.filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                      & (pl.col("turnover") > 0))
            .group_by("date").agg(pl.col(fwd).mean().alias("pool_mu"),
                                  pl.col(fwd).median().alias("pool_med"),
                                  pl.col(fwd).clip(
                                      upper_bound=pl.col(fwd).quantile(0.99)
                                  ).mean().alias("pool_w99"),
                                  pl.col(fwd).clip(
                                      lower_bound=pl.col(fwd).quantile(0.01),
                                      upper_bound=pl.col(fwd).quantile(0.99)
                                  ).mean().alias("pool_w01w99"),
                                  pl.len().alias("pool_n")).sort("date"))


def max_t_bootstrap(mat: np.ndarray, se: np.ndarray, block: int = 120,
                    reps: int = 1000, seed: int = 0) -> np.ndarray:
    """整张网格的 max|t| 零分布（White reality check 的块自助版）。

    逐格报 t 会把「挑了 24 格里最好的那个」藏起来。同一组重采样日期同时施加到
    所有格子，格间的相关（不同 N 高度重叠）自动进入零分布，不必另做校正。
    各格先中心化，用原样本的 se 标准化——标准做法，不重估 se 是为了让
    1000 次重采样在秒级完成。
    """
    x = mat - mat.mean(axis=1, keepdims=True)
    c, t = x.shape
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(t / block))
    out = np.empty(reps)
    ar = np.arange(block)
    for i in range(reps):
        starts = rng.integers(0, t, nb)
        idx = ((starts[:, None] + ar[None, :]) % t).ravel()[:t]
        out[i] = np.max(np.abs(x[:, idx].mean(axis=1)) / se)
    return out


def mom_shape(df: pl.DataFrame, h: int = 60, k: int = 5) -> dict:
    """层内：有符号动量 vs |动量|，谁才是单调的那个。

    若单调的是 |mom|，则「动量」只是「最近动得大」的别名，而 MAX/IVOL 与它同形
    就不是「同一个流动性代理」，而是同一个 |20 日涨跌| 的三种写法。
    """
    fwd = f"fwd{h}"
    d = (df.filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                   & (pl.col("turnover") > 0))
         .with_columns((((pl.col("turnover").rank("ordinal").over("date") - 1)
                         / pl.col("turnover").count().over("date")) < 0.2).alias("in_b"))
         .filter(pl.col("in_b"))
         .with_columns(pl.col("mom").abs().alias("amom")))

    def q(col: str) -> pl.Expr:
        return (((pl.col(col).rank("ordinal").over("date") - 1)
                 / pl.col(col).count().over("date") * k).floor().clip(0, k - 1)
                .cast(pl.Int8))

    out = {}
    for name, col in (("signed", "mom"), ("abs", "amom")):
        g = (d.with_columns(q(col).alias("b"))
             .group_by(["date", "b"]).agg(pl.col(fwd).mean().alias("mu"))
             .group_by("b").agg((pl.col("mu").mean() * 1e4).alias("bp")).sort("b"))
        out[name] = [float(v) for v in g["bp"].to_list()]
    return out


def size_turnover_grid(h: int = 60, k: int = 5,
                       since=None) -> tuple[list[list[float]], list[float], int, str]:
    """规模 × 成交额 双向排序。上一轮把层溢价归给「规模/流动性」，但它没做双向。

    规模 = close × float_shares，**用当日收盘 × 当前股本快照**——不是 PIT 总股本，
    所以它是「量级排序」而不是精确市值；只用于分层，不用于回归。

    since 用于子样本复核：股本快照漂移随时间累积，越早的年份越不准，所以
    「近两年」是这个网格唯一可信的窗口。
    """
    cq = (pl.scan_parquet(P.DUMP).select(["thscode", "date_ms", "close_price"])
          .with_columns(pl.from_epoch(pl.col("date_ms") + 8 * 3600 * 1000,
                                      time_unit="ms").dt.date().alias("date"))
          .select(["thscode", "date", "close_price"]).collect())
    inst = pl.read_parquet(P.ROOT / "data/instruments/instruments.parquet").select(
        ["symbol", "float_shares"])
    fwd = f"fwd{h}"
    d = (P.load_panel()
         .join(cq, on=["thscode", "date"], how="left")
         .join(inst.rename({"symbol": "thscode"}), on="thscode", how="left")
         .with_columns((pl.col("close_price") * pl.col("float_shares")).alias("mcap"))
         .filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                 & (pl.col("turnover") > 0) & (pl.col("mcap") > 0)))
    if since is not None:
        d = d.filter(pl.col("date") >= since)

    def q(col: str) -> pl.Expr:
        return (((pl.col(col).rank("ordinal").over("date") - 1)
                 / pl.col(col).count().over("date") * k).floor().clip(0, k - 1)
                .cast(pl.Int8))

    d = d.with_columns(q("mcap").alias("sz"), q("turnover").alias("tq"))
    g = (d.group_by(["date", "sz", "tq"])
         .agg(pl.col(fwd).mean().alias("mu"))
         .group_by(["sz", "tq"]).agg((pl.col("mu").mean() * 1e4).alias("bp")))
    mat = [[float(g.filter((pl.col("sz") == i) & (pl.col("tq") == j))["bp"][0])
            for j in range(k)] for i in range(k)]
    s = (d.group_by(["date", "sz"]).agg(pl.col(fwd).mean().alias("mu"))
         .group_by("sz").agg((pl.col("mu").mean() * 1e4).alias("bp")).sort("sz"))
    days = int(d["date"].n_unique())
    until = str(d["date"].max())
    return mat, [float(v) for v in s["bp"].to_list()], days, until


def stat(x: np.ndarray, block: int) -> tuple[float, float]:
    mu = 1e4 * float(np.nanmean(x))
    if np.isfinite(x).sum() <= 30:
        return mu, float("nan")
    se = P.block_se(x[np.isfinite(x)], block=block)
    return mu, float(mu / (1e4 * se))


def decay_lambda(ns: list[int], alphas: list[float], max_n: int = 100) -> float:
    """log alpha ~ -λ log N 的最小二乘斜率，只在 alpha>0 且 N ≤ max_n 的点上拟合。

    max_n 不是调参：超过它，子集已占满该层的一大块，均值向层均值回归是与信号
    无关的机械效应，混进来会把 λ 推向 1。
    """
    xs, ys = [], []
    for n, a in zip(ns, alphas):
        if a > 0 and n <= max_n:
            xs.append(np.log(n))
            ys.append(np.log(a))
    if len(xs) < 2:
        return float("nan")
    xs, ys = np.array(xs), np.array(ys)
    return float(-np.polyfit(xs, ys, 1)[0])


def breakeven_n(ns: list[int], alphas: list[float], cost: float) -> float:
    """毛 alpha 跌破成本的宽度（log N 线性插值）。返回 inf 表示全区间未跌破。"""
    for (n0, a0), (n1, a1) in zip(zip(ns, alphas), zip(ns[1:], alphas[1:])):
        if a0 >= cost > a1:
            w = (a0 - cost) / (a0 - a1)
            return float(np.exp(np.log(n0) + w * (np.log(n1) - np.log(n0))))
    return float("inf")


def yearly(dates, x: np.ndarray) -> dict:
    yrs = np.array([int(str(d)[:4]) for d in dates])
    out = {}
    for y in sorted(set(yrs.tolist())):
        m = yrs == y
        if m.sum() >= 20:
            out[str(y)] = round(1e4 * float(np.nanmean(x[m])), 1)
    return out


def row_slopes(mat: list[list[float]]) -> list[float]:
    """每一行（规模档）对列（成交额五分位）的最小二乘斜率，bp/档。

    存在的理由：文字里「换手率杀伤只在小盘端」这类断言，没有任何产物字段能证伪，
    于是它可以一直活着。斜率进产物，断言才可核。
    """
    out = []
    for row in mat:
        x = np.arange(len(row), dtype=float)
        out.append(float(np.polyfit(x, np.asarray(row, dtype=float), 1)[0]))
    return out


def main() -> None:
    df = P.load_panel()
    inst = pl.read_parquet(P.ROOT / "data/instruments/instruments.parquet").select(
        ["symbol", "float_shares"])
    df = df.join(inst.rename({"symbol": "thscode"}), on="thscode", how="left").with_columns(
        (pl.col("volume") / pl.col("float_shares")).alias("rate"))
    print(f"面板 {df.height:,} 行")

    report: dict = {"cost_bp": COST_BP, "participation": PARTICIPATION,
                    "ns": list(NS), "frac": 0.2,
                    "definitions": {
                        "layer_bp": "层均值 − 池均值；层 = 当日 turnover（或 rate）最低 20%",
                        "layer_med_bp": "层的中位 − 池的中位（两边都是逐日再对日期取均值）",
                        "layer_w99_bp": "层与池都只 clip 上界 99% ——**单边上尾**，不是双侧缩尾",
                        "layer_w01w99_bp": "层与池都按 1%/99% 双侧缩尾（2026-11-08 补）",
                        "win_daily": "逐日 layer>0 的占比。日度重叠，**不是**单次持有胜率",
                        "block_win": "layer 序列切成不重叠的 h 个决策日一块，块均值>0 的占比（仍在回报空间重叠）",
                        "stride_win": "每 h 个决策日取一日，取到的 fwd_h 互不相交。**这个才是单次持有胜率**",
                        "pool_med_of_daily_med_bp": "日度池中位的时序中位。与 pool_mean_minus_median 不闭合，勿混用",
                        "slope_per_quintile_bp": "同一规模档内，bp 对成交额五分位的 OLS 斜率（0→4）"}}
    mt: list[tuple[str, np.ndarray]] = []
    for h in (H, 20):
        pm = pool_mean(df, h)
        for key in ("turnover", "rate"):
            g = build_bucket(df, key, h).join(pm, on="date", how="inner")
            g = g.with_columns(
                *[(pl.col(f"mu{n}") - pl.col("pool_mu")).alias(f"vs_pool{n}") for n in NS],
                *[(pl.col(f"mu{n}") - pl.col("bucket_mu")).alias(f"vs_bucket{n}") for n in NS],
                (pl.col("bucket_mu") - pl.col("pool_mu")).alias("layer"),
                (pl.col("bucket_w99") - pl.col("pool_w99")).alias("layer_w99"),
                (pl.col("bucket_w01w99") - pl.col("pool_w01w99")).alias("layer_w01w99"),
                (pl.col("bucket_med") - pl.col("pool_med")).alias("layer_med"),
                (pl.col("bucket_mu") - pl.col("bucket_med")).alias("tail"),
            )
            dates = g["date"].to_numpy()
            block = 2 * h
            tag = "成交额(元)" if key == "turnover" else "换手率"
            print(f"\n=== {tag} 最底层 20%   H={h}   {g.height} 个决策日   "
                  f"层内 {g['bucket_n'].median():.0f} 只 ===")
            print("   N   组合   层溢价  动量(vs层)  t(动量)  t(vs池)  中位ADV   容量(20%ADV)")
            rows = []
            for n in NS:
                vp = g[f"vs_pool{n}"].to_numpy()
                vb = g[f"vs_bucket{n}"].to_numpy()
                mu_p = 1e4 * float(np.nanmean(vp))
                mu_b = 1e4 * float(np.nanmean(vb))
                _, t_b = stat(vb, block)
                _, t_p = stat(vp, block)
                adv = float(np.nanmedian(g[f"adv{n}"].to_numpy()))
                aum = n * PARTICIPATION * adv
                med = 1e4 * float(np.nanmean(g[f"med{n}"].to_numpy()))
                rows.append({
                    "n": n, "gross_vs_pool_bp": mu_p, "net_vs_pool_bp": mu_p - COST_BP,
                    "t_vs_pool": t_p, "gross_vs_bucket_bp": mu_b,
                    "net_vs_bucket_bp": mu_b - COST_BP, "t_vs_bucket": t_b,
                    "median_fwd_bp": med,
                    "adv_median_yi": adv / 1e8, "aum_max_yi": aum / 1e8,
                    "aum_max_wan": aum / 1e4,
                })
                print(f"  {n:4d} {1e4*float(np.nanmean(g[f'mu{n}'].to_numpy())):+8.1f} "
                      f"{1e4*float(np.nanmean(g['layer'].to_numpy())):+8.1f} "
                      f"{mu_b:+9.1f} {t_b:+7.2f} {t_p:+7.2f} "
                      f"{adv/1e8:7.3f}亿 {aum/1e8:8.2f}亿")
            lam_b = decay_lambda(list(NS), [r["gross_vs_bucket_bp"] for r in rows])
            lam_p = decay_lambda(list(NS), [r["gross_vs_pool_bp"] for r in rows])
            if h == H:
                for n in NS:
                    mt.append((f"{key}/N{n}/vs层", g[f"vs_bucket{n}"].to_numpy()))
                    mt.append((f"{key}/N{n}/vs池", g[f"vs_pool{n}"].to_numpy()))
            be_b = breakeven_n(list(NS), [r["gross_vs_bucket_bp"] for r in rows], COST_BP)
            layer_bp, layer_t = stat(g["layer"].to_numpy(), block)
            ls_bp, ls_t = stat(g["ls20"].to_numpy(), block)
            w99_bp, w99_t = stat(g["layer_w99"].to_numpy(), block)
            w2_bp, w2_t = stat(g["layer_w01w99"].to_numpy(), block)
            med_bp, med_t = stat(g["layer_med"].to_numpy(), block)
            tail_bp = 1e4 * float(np.nanmean(g["tail"].to_numpy()))
            pool_tail = 1e4 * float(np.nanmean((g["pool_mu"] - g["pool_med"]).to_numpy()))
            # 三个池统计量分开落盘：混用时间聚合是初稿的 57bp 口径错。
            pool_mu_bp = 1e4 * float(np.nanmean(g["pool_mu"].to_numpy()))
            pool_med_bp = 1e4 * float(np.nanmean(g["pool_med"].to_numpy()))
            pool_med_of_med_bp = 1e4 * float(np.nanmedian(g["pool_med"].to_numpy()))
            bucket_mu_bp = 1e4 * float(np.nanmean(g["bucket_mu"].to_numpy()))
            bucket_med_bp = 1e4 * float(np.nanmean(g["bucket_med"].to_numpy()))
            bucket_med_of_med_bp = 1e4 * float(np.nanmedian(g["bucket_med"].to_numpy()))
            # 胜率两种口径：逐日（重叠，会被 60 日重叠放大）与不重叠块。
            lay = g["layer"].to_numpy()
            fin = lay[np.isfinite(lay)]
            win_daily = float((fin > 0).mean())
            nblk = len(fin) // h
            blk = fin[:nblk * h].reshape(nblk, h).mean(axis=1) if nblk else np.array([])
            win_block = float((blk > 0).mean()) if nblk else float("nan")
            # 真·不重叠：每 h 个决策日取一日，取到的那个 fwd_h 与下一个不相交。
            # 这和「分块取均值」是两个量（79% vs 67%），混淆会把单次持有胜率抬高 12 个点。
            stride = fin[::h]
            print(f"  层溢价 {layer_bp:+.1f}bp (t={layer_t:+.2f})   "
                  f"中位口径 {med_bp:+.1f}bp (t={med_t:+.2f})   "
                  f"上尾99%缩尾 {w99_bp:+.1f}bp (t={w99_t:+.2f})   "
                  f"双侧1/99缩尾 {w2_bp:+.1f}bp (t={w2_t:+.2f})")
            print(f"  胜率 逐日 {win_daily:.2%}（重叠）；分块均值 {win_block:.2%} "
                  f"(n={nblk}，最差块 {1e4*blk.min():+.1f}bp)；"
                  f"每 {h} 日取一日 {float((stride > 0).mean()):.2%} "
                  f"(n={len(stride)}，最差 {1e4*stride.min():+.1f}bp)")
            print(f"  池 均值 {pool_mu_bp:+.1f} / 中位(均值口径) {pool_med_bp:+.1f} / "
                  f"中位(中位口径) {pool_med_of_med_bp:+.1f}"
                  f"（恒等式只用前者：{pool_mu_bp - pool_med_bp:+.1f}）")
            print(f"  层内多空20/20 {ls_bp:+.1f}bp (t={ls_t:+.2f})；"
                  f"层均值−层中位 {tail_bp:+.1f}bp，全池自身 {pool_tail:+.1f}bp"
                  f"（60 日收益本来右偏，不是层独有）")
            print(f"  λ(vs池)={lam_p:.2f}   λ(vs层,N≤100)={lam_b:.2f}   "
                  f"净 alpha 跌破 0 的宽度 N*={be_b:.0f}")
            report[f"{key}_H{h}"] = {
                "rows": rows, "layer_bp": layer_bp, "layer_t": layer_t,
                "layer_med_bp": med_bp, "layer_med_t": med_t,
                "layer_w99_bp": w99_bp, "layer_w99_t": w99_t,
                "layer_w01w99_bp": w2_bp, "layer_w01w99_t": w2_t,
                "bucket_mean_minus_median_bp": tail_bp,
                "pool_mean_minus_median_bp": pool_tail,
                "pool_mu_bp": pool_mu_bp, "pool_med_bp": pool_med_bp,
                "pool_med_of_daily_med_bp": pool_med_of_med_bp,
                "bucket_mu_bp": bucket_mu_bp, "bucket_med_bp": bucket_med_bp,
                "bucket_med_of_daily_med_bp": bucket_med_of_med_bp,
                "win_daily": win_daily,
                "block_win": {"h": h, "n_blocks": int(nblk),
                              "win_rate": win_block,
                              "worst_bp": float(1e4 * blk.min()) if nblk else float("nan"),
                              "best_bp": float(1e4 * blk.max()) if nblk else float("nan"),
                              "median_bp": float(1e4 * np.median(blk)) if nblk else float("nan")},
                # 单次持有胜率只有这一个口径算数：每 h 日取一日，取到的 fwd_h 互不相交。
                "stride_win": {"h": h, "n": int(len(stride)),
                               "win_rate": float((stride > 0).mean()),
                               "worst_bp": float(1e4 * stride.min()),
                               "best_bp": float(1e4 * stride.max())},
                "window": {"start": str(dates[0]), "end": str(dates[-1]),
                           "days": int(g.height), "horizon": h,
                           "block_bootstrap": block, "seed": 0, "reps": 1000},
                "ls20_bp": ls_bp, "ls20_t": ls_t,
                "lambda_vs_pool": lam_p, "lambda_vs_bucket_n100": lam_b,
                "breakeven_n": be_b,
                "bucket_n_med": float(g["bucket_n"].median()),
                "yearly_layer": yearly(dates, g["layer"].to_numpy()),
                "yearly_layer_w99": yearly(dates, g["layer_w99"].to_numpy()),
                "yearly_vs_pool20": yearly(dates, g["vs_pool20"].to_numpy()),
                "yearly_vs_bucket20": yearly(dates, g["vs_bucket20"].to_numpy()),
                "yearly_vs_bucket100": yearly(dates, g["vs_bucket100"].to_numpy()),
                "days": int(g.height),
            }

    OUT.mkdir(parents=True, exist_ok=True)
    # 整张 H=60 网格（2 口径 × 6 宽度 × 2 基准 = 24 格）的 max|t| 零分布
    names = [n for n, _ in mt]
    mat = np.vstack([x for _, x in mt])
    se = np.array([P.block_se(x, 2 * H) for _, x in mt])
    t_obs = np.abs(mat.mean(axis=1) / se)
    null = max_t_bootstrap(mat, se, block=2 * H)
    p_fwer = float((1 + (null >= t_obs.max()).sum()) / (len(null) + 1))
    order = np.argsort(-t_obs)[:3]
    print(f"\n=== 24 格 max|t| 现实性检验（块长 {2*H}，{len(null)} 次）===")
    for i in order:
        print(f"  {names[i]:24s} t={t_obs[i]:.2f}  净 "
              f"{1e4*float(mat[i].mean())-COST_BP:+.1f}bp")
    print(f"  网格内最好一格的 p（族系误差，已含格间相关）= {p_fwer:.3f}"
          f"   零分布 95% 分位 t = {np.quantile(null, 0.95):.2f}")
    report["max_t"] = {"best": names[int(order[0])], "best_t": float(t_obs.max()),
                       "p_fwer": p_fwer,
                       "null_q95": float(np.quantile(null, 0.95)),
                       "raw_best_net_bp": float(1e4 * mat[int(order[0])].mean() - COST_BP)}

    # 「动量这一族整族是噪声」这句话，检验的是 12 个 vs层 格，不是 24 格。
    # 24 格里 12 个是 vs池（含层溢价，t 高达 4.85），混进来会把零分布抬到 2.76，
    # 于是任何动量格都「不显著」——那是被同族的另一个假设污染的判据。
    # 族系必须按假设划分：H0(动量=0) 只有 vs层 这 12 格。
    sel = [i for i, n in enumerate(names) if n.endswith("vs层")]
    null_b = max_t_bootstrap(mat[sel], se[sel], block=2 * H)
    t_b = t_obs[sel]
    p_b = float((1 + (null_b >= t_b.max()).sum()) / (len(null_b) + 1))
    print(f"  仅 12 个动量格(vs层)：max t={t_b.max():.2f} "
          f"({names[sel[int(np.argmax(t_b))]]})  零分布95%={np.quantile(null_b, 0.95):.2f} "
          f" p={p_b:.3f}")
    report["max_t_momentum_only"] = {
        "n_cells": len(sel), "best": names[sel[int(np.argmax(t_b))]],
        "best_t": float(t_b.max()), "p_fwer": p_b,
        "null_q95": float(np.quantile(null_b, 0.95)),
        "raw_best_net_bp": float(1e4 * mat[sel[int(np.argmax(t_b))]].mean() - COST_BP),
    }

    mat_st, sz_only, days_st, until_st = size_turnover_grid(H)
    print("\n=== 规模 × 成交额（H=60，等权 fwd bp）  行=规模五分位(0 最小)，列=成交额五分位(0 最低) ===")
    for i, row in enumerate(mat_st):
        print(f"  S{i}  " + "  ".join(f"{v:+8.1f}" for v in row))
    print("  规模单独 " + "  ".join(f"S{i}={v:+.1f}" for i, v in enumerate(sz_only)))
    print(f"  规模极差 {max(sz_only)-min(sz_only):.1f}bp；"
          f"成交额极差（最小规模行）{max(mat_st[0])-min(mat_st[0]):.1f}bp；"
          f"（最大规模行）{max(mat_st[-1])-min(mat_st[-1]):.1f}bp")
    report["size_turnover_H60"] = {"mat": mat_st, "size_only": sz_only,
                                   "slope_per_quintile_bp": row_slopes(mat_st),
                                   "since": None, "until": until_st, "days": days_st}
    print("  每档斜率(对成交额五分位) "
          + "  ".join(f"S{i}={v:+.1f}" for i, v in enumerate(row_slopes(mat_st))))

    # 子样本：股本快照漂移随时间累积，越早越不可信。两个起点都存，因为结论对起点敏感
    # （2024-01 起 S0 行 +1020，2025-01 起 +711——同一形状，不同水平）。
    for tag, since in (("2024_01", date(2024, 1, 1)), ("2025_01", date(2025, 1, 1))):
        mat_rc, sz_rc, days_rc, until_rc = size_turnover_grid(H, since=since)
        print(f"\n=== 规模 × 成交额（{tag[:4]}-{tag[5:]} 起；股本快照漂移最小）"
              f"   {days_rc} 个决策日 → {until_rc} ===")
        for i, row in enumerate(mat_rc):
            print(f"  S{i}  " + "  ".join(f"{v:+8.1f}" for v in row))
        print("  规模单独 " + "  ".join(f"S{i}={v:+.1f}" for i, v in enumerate(sz_rc)))
        print("  每档斜率(对成交额五分位) "
              + "  ".join(f"S{i}={v:+.1f}" for i, v in enumerate(row_slopes(mat_rc))))
        report[f"size_turnover_H60_since_{tag}"] = {
            "mat": mat_rc, "size_only": sz_rc,
            "slope_per_quintile_bp": row_slopes(mat_rc),
            # 只存起点是不够的：2025-01 起只有 383 个决策日 ≈ 1.5 年，
            # 把它读成「近两年」会高估样本长度 33%。
            "since": since.isoformat(), "until": until_rc, "days": days_rc}

    shape = mom_shape(df, H)
    print("\n=== 低换手层内：五分位的 fwd60 均值（bp） ===")
    print("  有符号 mom " + "  ".join(f"{v:+7.1f}" for v in shape["signed"])
          + f"   极差 {max(shape['signed'])-min(shape['signed']):.1f}")
    print("  |mom|      " + "  ".join(f"{v:+7.1f}" for v in shape["abs"])
          + f"   极差 {max(shape['abs'])-min(shape['abs']):.1f}")
    report["mom_shape_H60"] = shape

    (OUT / "width_frontier.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/width_frontier.json")


if __name__ == "__main__":
    sys.exit(main())
