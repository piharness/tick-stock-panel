"""成交额层与换手率层在 2024–25 分道：两个「流动性」口径选的不是同一批票。

fact：width_frontier 的逐年层溢价两个口径同向到 2023，2024/2025 劈开——
成交额 +393/+327，换手率 −79/−71。若两者都在测同一件事（低关注度），不该分裂。

机制不需要新数据就能写出来：
    成交额(元) ≡ 收盘价 × 成交量 ≡ 换手率 × 流通市值
所以按成交额分层 = 按「换手率 × 规模」的**乘积**分层；换手率(量/流通股本)与
价格、规模都无关。两者不是同一个量的两种度量，是复合排序 vs 单变量排序。
若是这样，「层溢价是流动性/关注度」这句话就得降级成「是规模与换手的一个组合」，
而 2024–25 的分裂方向会告诉我们哪一轴在最近两年说话。

三个检验：
 A 成分倾斜：两个层的成员在 (规模, 价格, 换手率, 成交额) 上的中位分位。
   倾斜不同，分裂就有了不需要 t 检验的解释。
 B 规模 × 换手率 五五网格。（原脚本只有 规模 × 成交额，缺的就是这一格。）
 C 固定换手率看成交额：同一换手率五分位内按成交额再分五档。由恒等式
   amount = rate × mcap，固定 rate 时 amount ∝ mcap，**所以这一列量的是规模，
   不是价格**。价格不是第三根轴：它在 mcap 里已经被吸收。
 D 价格 × 成交额，只作旁证：与 B 同构，用来确认没有漏掉独立的价格轴。
输出：data/.scratch/liquidity_identity.json
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
import probe_width_frontier as W  # noqa: E402

OUT = P.OUT
H = 60
K = 5
FRAC = 0.2


def frame() -> pl.DataFrame:
    cq = (pl.scan_parquet(P.DUMP).select(["thscode", "date_ms", "close_price"])
          .with_columns(pl.from_epoch(pl.col("date_ms") + 8 * 3600 * 1000,
                                     time_unit="ms").dt.date().alias("date"))
          .select(["thscode", "date", "close_price"]).collect())
    inst = pl.read_parquet(P.ROOT / "data/instruments/instruments.parquet").select(
        ["symbol", "float_shares"]).rename({"symbol": "thscode"})
    fwd = f"fwd{H}"
    return (P.load_panel()
            .join(cq, on=["thscode", "date"], how="left")
            .join(inst, on="thscode", how="left")
            .filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                    & (pl.col("turnover") > 0) & (pl.col("close_price") > 0)
                    & (pl.col("float_shares") > 0))
            .with_columns(
                (pl.col("close_price") * pl.col("float_shares")).alias("mcap"),
                (pl.col("volume") / pl.col("float_shares")).alias("rate")))


def qexpr(col: str, k: int = K) -> pl.Expr:
    return (((pl.col(col).rank("ordinal").over("date") - 1)
             / pl.col(col).count().over("date") * k).floor().clip(0, k - 1)
            .cast(pl.Int8))


def pct_expr(col: str) -> pl.Expr:
    return ((pl.col(col).rank("ordinal").over("date") - 1)
            / (pl.col(col).count().over("date") - 1))


def tilt(d: pl.DataFrame) -> dict:
    """每层成员在四个变量上的中位分位，再对日期取均值。"""
    x = d.with_columns(
        pct_expr("mcap").alias("p_mcap"), pct_expr("close_price").alias("p_price"),
        pct_expr("rate").alias("p_rate"), pct_expr("turnover").alias("p_amt"),
        ((pl.col("turnover").rank("ordinal").over("date") - 1)
         / pl.col("turnover").count().over("date")).alias("r_amt"),
        ((pl.col("rate").rank("ordinal").over("date") - 1)
         / pl.col("rate").count().over("date")).alias("r_rate"),
    )
    cols = ["p_mcap", "p_price", "p_rate", "p_amt"]
    out = {}
    for name, cond in (("成交额层", pl.col("r_amt") < FRAC),
                       ("换手率层", pl.col("r_rate") < FRAC)):
        g = (x.filter(cond).group_by("date")
             .agg([pl.col(c).median().alias(c) for c in cols])
             .select([pl.col(c).mean().alias(c) for c in cols]))
        out[name] = {c: round(float(g[c][0]), 4) for c in cols}
    return out


def overlap(d: pl.DataFrame) -> dict:
    """两个层的逐日 Jaccard 与「成交额层有多大比例落在换手率层里」。

    若两者真是同一个东西的两种量法，Jaccard 应接近 1；若只是相关，则在 0.2–0.4。
    """
    x = d.with_columns(
        ((pl.col("turnover").rank("ordinal").over("date") - 1)
         / pl.col("turnover").count().over("date")).alias("ra"),
        ((pl.col("rate").rank("ordinal").over("date") - 1)
         / pl.col("rate").count().over("date")).alias("rr"))
    g = (x.group_by("date").agg(
        ((pl.col("ra") < FRAC) & (pl.col("rr") < FRAC)).sum().alias("both"),
        (pl.col("ra") < FRAC).sum().alias("na"),
        (pl.col("rr") < FRAC).sum().alias("nr"))
        .with_columns((pl.col("both") / (pl.col("na") + pl.col("nr") - pl.col("both"))).alias("j")))
    return {"jaccard_mean": round(float(g["j"].mean()), 4),
            "jaccard_median": round(float(g["j"].median()), 4),
            "share_of_amount_layer_in_rate_layer": round(float((g["both"] / g["na"]).mean()), 4)}


def grid(d: pl.DataFrame, row: str, col: str, since=None) -> tuple[list[list[float]], int, str]:
    fwd = f"fwd{H}"
    x = d if since is None else d.filter(pl.col("date") >= since)
    g = (x.with_columns(qexpr(row).alias("r"), qexpr(col).alias("c"))
         .group_by(["date", "r", "c"]).agg(pl.col(fwd).mean().alias("mu"))
         .group_by(["r", "c"]).agg((pl.col("mu").mean() * 1e4).alias("bp")))
    mat = [[float(g.filter((pl.col("r") == i) & (pl.col("c") == j))["bp"][0])
            for j in range(K)] for i in range(K)]
    # 只存起点不存终点，读者会把 1.5 年读成 2 年（2025-01 起只有 383 个决策日）。
    return mat, int(x["date"].n_unique()), str(x["date"].max())


def row_slopes(mat: list[list[float]]) -> list[float]:
    """每行对列（五分位）的 OLS 斜率，bp/档。

    「杀伤只在小盘端」是一句可证伪的话，但产物里没有字段能证伪它，
    于是它活了三轮。斜率进产物，这句话才有对手。
    """
    out = []
    for row in mat:
        out.append(float(np.polyfit(np.arange(K, dtype=float),
                                    np.asarray(row, dtype=float), 1)[0]))
    return out


def show(tag: str, mat: list[list[float]], rows: str, cols: str) -> None:
    print(f"\n=== {tag}   行={rows}(0 最低)  列={cols}(0 最低) ===")
    for i, row in enumerate(mat):
        print(f"  {i}  " + "  ".join(f"{v:+8.1f}" for v in row))
    print(f"  行极差 " + " ".join(f"{i}:{max(r)-min(r):+7.1f}" for i, r in enumerate(mat)))
    print(f"  列极差 " + " ".join(f"{j}:{max(r[j] for r in mat)-min(r[j] for r in mat):+7.1f}"
                                for j in range(len(mat[0]))))


def main() -> None:
    d = frame()
    print(f"面板 {d.height:,} 行  {d['date'].min()} → {d['date'].max()}")

    ratio = (d.select((pl.col("turnover") / (pl.col("close_price") * pl.col("volume")))
                      .median())).item()
    print(f"成交额 ÷ (收盘价×成交量) 中位 = {ratio:.4f}"
          f"  → 成交额确实含价格水平（100 表示 volume 以手计）")

    til = tilt(d)
    print("\n=== A 成分倾斜（成员在当日全池的中位分位；1=最高）===")
    for k, v in til.items():
        print(f"  {k} 规模 {v['p_mcap']:.3f}  价格 {v['p_price']:.3f}  "
              f"换手率 {v['p_rate']:.3f}  成交额 {v['p_amt']:.3f}")
    ov = overlap(d)
    print(f"  两层重叠：Jaccard 均值 {ov['jaccard_mean']:.3f}；"
          f"成交额层成员有 {ov['share_of_amount_layer_in_rate_layer']:.1%} 也在换手率层")

    g_sr, sr_days, sr_until = grid(d, "mcap", "rate")
    show("B 规模 × 换手率  H=60 全年", g_sr, "规模", "换手率")
    print("  每档斜率(对换手率五分位) "
          + "  ".join(f"S{i}={v:+.1f}" for i, v in enumerate(row_slopes(g_sr))))
    g_sr_rc, sr_rc_days, sr_rc_until = grid(d, "mcap", "rate", since=date(2024, 1, 1))
    show("B' 规模 × 换手率  2024 起", g_sr_rc, "规模", "换手率")
    print("  每档斜率(对换手率五分位) "
          + "  ".join(f"S{i}={v:+.1f}" for i, v in enumerate(row_slopes(g_sr_rc))))

    g_ra, ra_days, ra_until = grid(d, "rate", "turnover")
    show("C 换手率 × 成交额  H=60 全年（固定换手率时 成交额 ∝ 流通市值 ⇒ 列 = 规模）",
         g_ra, "换手率", "成交额")
    g_ra_rc, ra_rc_days, ra_rc_until = grid(d, "rate", "turnover", since=date(2024, 1, 1))
    show("C' 换手率 × 成交额  2024 起", g_ra_rc, "换手率", "成交额")

    g_pa, pa_days, pa_until = grid(d, "close_price", "turnover")
    show("D 价格 × 成交额  H=60 全年（价格不是独立轴，与 B 同构，仅作旁证）",
         g_pa, "价格", "成交额")

    rep = {"h": H, "k": K, "frac": FRAC,
           "amount_over_price_volume_median": float(ratio),
           "tilt": til, "overlap": ov,
           "definitions": {
               "tilt": "层成员在当日全池的中位分位（1=最高），再对日期取均值",
               "jaccard_mean": "两层逐日 Jaccard 的时序均值；1 才是「同一个东西」",
               "slope_per_quintile_bp": "同一行内 bp 对列五分位的 OLS 斜率，bp/档",
               "size_x_rate": "行=规模五分位(0 最小)，列=换手率五分位(0 最低)",
               "rate_x_amount": "行=换手率，列=成交额。固定换手率时成交额 ∝ 流通市值",
               "price_x_amount": "行=价格。旁证，价格不是独立轴"},
           "size_x_rate": {"mat": g_sr, "slope_per_quintile_bp": row_slopes(g_sr),
                           "since": None, "until": sr_until, "days": sr_days},
           "size_x_rate_since2024": {"mat": g_sr_rc,
                                     "slope_per_quintile_bp": row_slopes(g_sr_rc),
                                     "since": "2024-01-01", "until": sr_rc_until,
                                     "days": sr_rc_days},
           "rate_x_amount": {"mat": g_ra, "slope_per_quintile_bp": row_slopes(g_ra),
                             "since": None, "until": ra_until, "days": ra_days},
           "rate_x_amount_since2024": {"mat": g_ra_rc,
                                       "slope_per_quintile_bp": row_slopes(g_ra_rc),
                                       "since": "2024-01-01", "until": ra_rc_until,
                                       "days": ra_rc_days},
           "price_x_amount": {"mat": g_pa, "slope_per_quintile_bp": row_slopes(g_pa),
                              "since": None, "until": pa_until, "days": pa_days}}
    (OUT / "liquidity_identity.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/liquidity_identity.json")


if __name__ == "__main__":
    sys.exit(main())
