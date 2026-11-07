"""中段十分位的正净值，是「动量」还是「安静/低换手」？——双向排序归因。

上一轮的十分位网格在 H=60 出现 5 个净值为正的格子（D3-D7，+10~+19bp/60日，
t≈3.2~3.9）。但那些格子的中位成交额同时是全场最低（0.45亿，对比 D1 0.81亿、
D10 2.53亿）。|动量| 与换手正相关，所以「中段动量」天然等于「最安静的股票」。
若把换手先分层，动量的驼峰还在——那是新东西；消失——那就只是已知的低换手溢价。

本脚本只做这件事：同一面板、同一前向，mom 五分位 × 换手三分位，逐格超额与 t。

口径：t 的块长 = 2×H（H 期重叠收益的自相关必须落在块内，否则 t 系统性虚高）。
输出：data/.scratch/hump_attribution.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_decile_net as P  # noqa: E402

OUT = P.OUT
COST_BP = P.COST_BP


def grid(df: pl.DataFrame, h: int, by: str, n_mom: int = 5, n_turn: int = 3) -> dict:
    """by='amt' 按成交额（元）分层；by='rate' 按换手率 volume/float_shares 分层。

    这两个不是同一个因子：成交额 ≈ 规模×换手率，是规模代理；换手率是关注度。
    生产 probe_mde_matrix 的 turn = volume/float_shares 是后者。
    """
    fwd = f"fwd{h}"
    d = df.filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                  & (pl.col("turnover") > 0) & pl.col("rate").is_not_null()
                  & (pl.col("rate") > 0))
    key = "turnover" if by == "amt" else "rate"
    d = d.with_columns(
        ((pl.col(key).rank("ordinal").over("date") - 1)
         / pl.col(key).count().over("date") * n_turn)
        .floor().clip(0, n_turn - 1).cast(pl.Int8).alias("tbin"))
    d = d.with_columns(
        ((pl.col("mom").rank("ordinal").over(["date", "tbin"]) - 1)
         / pl.col("mom").count().over(["date", "tbin"]) * n_mom)
        .floor().clip(0, n_mom - 1).cast(pl.Int8).alias("mbin"))
    base = d.group_by("date").agg(pl.col(fwd).mean().alias("base"))
    g = (d.group_by(["date", "tbin", "mbin"])
         .agg(pl.col(fwd).mean().alias("mu"), pl.col(fwd).len().alias("n"))
         .join(base, on="date")
         .with_columns((pl.col("mu") - pl.col("base")).alias("exc")))
    cells = {}
    for tb in range(n_turn):
        for mb in range(n_mom):
            seg = g.filter((pl.col("tbin") == tb) & (pl.col("mbin") == mb)).sort("date")
            x = seg["exc"].to_numpy()
            if x.size < 100:
                continue
            mu_bp = 1e4 * x.mean()
            cells[f"t{tb}_m{mb}"] = {
                "gross_bp": float(mu_bp),
                "t": float(mu_bp / (1e4 * P.block_se(x, block=2 * h))),
                "net_bp": float(mu_bp - COST_BP),
            }
    return cells


def main() -> None:
    df = P.load_panel()
    inst = pl.read_parquet(P.ROOT / "data/instruments/instruments.parquet").select(
        ["symbol", "float_shares"])
    df = df.join(inst.rename({"symbol": "thscode"}), on="thscode", how="left").with_columns(
        (pl.col("volume") / pl.col("float_shares")).alias("rate"))

    report = {}
    for by in ("amt", "rate"):
        for h in (20, 60):
            cells = grid(df, h, by)
            report[f"{by}_H{h}"] = cells
            tag = "成交额(元)=规模代理" if by == "amt" else "换手率=volume/float_shares"
            print(f"\n=== 按{tag}分层  H={h}  mom 五分位×换手三分位，超额 bp / t（块长 {2*h}）===")
            for tb in range(3):
                line = []
                for mb in range(5):
                    c = cells.get(f"t{tb}_m{mb}")
                    line.append(f"{c['gross_bp']:+7.1f}({c['t']:+5.2f})" if c else "     n/a      ")
                print(f"  T{tb+1} " + "  ".join(line))
            for tb in range(3):
                vals = [cells[f"t{tb}_m{mb}"]["gross_bp"] for mb in range(5) if f"t{tb}_m{mb}" in cells]
                if vals:
                    print(f"     T{tb+1} 内动量极差 {max(vals)-min(vals):+7.1f}bp"
                          f"  层均值 {sum(vals)/len(vals):+7.1f}bp")
    (OUT / "hump_attribution.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/hump_attribution.json")


if __name__ == "__main__":
    main()
