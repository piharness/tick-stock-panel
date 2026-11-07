"""宇宙缺口体检：把十年 dump 里缺席的标的固化成一张可逐行核对的表，并给出偏差上界。

背景：`data/cache/fuyao/daily_k__20261104.parquet` 只有 5577 只，
`data/instruments/instruments.parquet`（as_of 2026-11-05）有 5890 只。
若缺席组是系统性的（退市 / ST / 吸收合并），则「当日合格池等权均值」这个基准被抬高，
仅多组合的超额被压低——**方向和「幸存者偏差让结论好看」相反**，这是先要钉死的事。

本脚本只做三件事，不做数据补齐：
 1. 复算缺口，用可复现的规则分类（名称族，含 S*ST —— 前一轮口径漏了这 4 只，
    导致把 4 只 legacy-ST 记进了「合并/其他」）；
 2. 逐只核对本地是否还有日线，分清「有别的副本」和「真的只有 367 天」；
 3. 给出偏差上界：缺口全体平均相对基准偏离 X bp 时，基准均值被挪动多少 bp。

输出：data/.scratch/universe_gap.json + data/.scratch/universe_gap.csv
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
INST = ROOT / "data/instruments/instruments.parquet"
DUMP10Y = ROOT / "data/cache/fuyao/daily_k__20261104.parquet"
DUMP10D = ROOT / "data/cache/fuyao/daily_k_10d__20261105.parquet"
KLINE = ROOT / "data/kline_daily"
ENRICH = ROOT / "data/kline_daily_enriched"
OUT = ROOT / "data/.scratch"

# 基准对照：上一轮十年形状产物的最好仅多格与固定成本假设
BEST_LONG_BP = 32.4   # _panel10y_shape / _panel10y.json decile_shape_bp[1] (D2)
COST_BP = 38.0        # 19bp/边 × 2


def classify(name: str) -> str:
    """按名称族分类。顺序即优先级，必须显式：S*ST 既不 starts_with('*ST') 也不是普通 ST。"""
    if "退" in name:
        return "delisted"          # 退市整理期 / 已退市
    if name.startswith("*ST"):
        return "star_st"           # *ST
    if name.startswith("S*ST"):
        return "s_star_st"         # 股改未完成的 *ST（前一轮口径的洞）
    if name.startswith(("ST", "SST", "S ST")):
        return "st"
    return "other"


def local_symbols(root: Path) -> set[str]:
    """读一个 date=*/part.parquet 分区仓，返回其覆盖的全部代码。"""
    files = sorted(root.glob("date=*/part.parquet"))
    if not files:
        return set()
    cols = pl.read_parquet_schema(files[0])
    key = "symbol" if "symbol" in cols else "thscode"
    out: set[str] = set()
    for f in files:
        out |= set(pl.read_parquet(f, columns=[key])[key].unique().to_list())
    return out


def main() -> None:
    inst = pl.read_parquet(INST).select(["symbol", "name", "exchange", "listing_date"])
    dump_syms = set(
        pl.scan_parquet(DUMP10Y).select("thscode").unique().collect()["thscode"].to_list()
    )
    inst_syms = set(inst["symbol"].to_list())

    missing = inst.filter(~pl.col("symbol").is_in(list(dump_syms))).with_columns(
        pl.col("name").map_elements(classify, return_dtype=pl.Utf8).alias("cls")
    ).sort(["cls", "symbol"])

    n_inst, n_dump, n_miss = len(inst_syms), len(dump_syms), missing.height

    # ---- 本地是否还有别的副本 ----
    cov_367 = local_symbols(KLINE)
    cov_enr = local_symbols(ENRICH)
    cov_10d = set(pl.scan_parquet(DUMP10D).select("thscode").unique().collect()["thscode"].to_list())

    miss_syms = set(missing["symbol"].to_list())
    hit_367 = miss_syms & cov_367
    hit_enr = miss_syms & cov_enr
    hit_10d = miss_syms & cov_10d

    missing = missing.with_columns(
        pl.col("symbol").is_in(list(hit_367)).alias("in_kline_daily"),
        pl.col("symbol").is_in(list(hit_enr)).alias("in_kline_enriched"),
        pl.col("symbol").is_in(list(hit_10d)).alias("in_fuyao_10d"),
    )

    counts = dict(missing["cls"].value_counts().iter_rows())
    st_total = counts.get("star_st", 0) + counts.get("s_star_st", 0) + counts.get("st", 0)

    # ---- 偏差上界 ----
    # 真实池均值 base_true = (n_o*base_obs + m*mean_M) / (n_o+m)
    # 仅多超额 exc_true = exc_obs - [m/(n_o+m)] * (mean_M - base_obs)
    w = n_miss / (n_inst)
    need_gap = (COST_BP - BEST_LONG_BP) / w   # 要把最好格从 +32.4 抬到打平 38bp 成本，缺口组需低于基准多少
    ladder = []
    for gap in (-50.0, -100.0, -105.4, -200.0, -400.0):
        bias = -w * gap
        ladder.append({
            "assumed_gap_bp": gap,
            "bias_on_long_only_bp": round(bias, 2),
            "best_cell_net_bp": round(BEST_LONG_BP + bias - COST_BP, 2),
        })

    report = {
        "universe": {
            "instruments": n_inst,
            "dump_10y": n_dump,
            "missing": n_miss,
            "extra_in_dump": len(dump_syms - inst_syms),
            "weight_of_gap": round(w, 5),
        },
        "breakdown": counts,
        "breakdown_st_total": st_total,
        "crosscheck_previous_claim": {
            "claimed": {"delisted": 155, "star_st": 72, "st": 24, "other": 62},
            "recomputed": {
                "delisted": counts.get("delisted", 0),
                "star_st": counts.get("star_st", 0),
                "s_star_st": counts.get("s_star_st", 0),
                "st": counts.get("st", 0),
                "other": counts.get("other", 0),
            },
            "note": "总数 313 对；分类错位：前一轮把 4 只 S*ST 记进了「合并/其他」，"
                    "故 *ST 少 4（72→76）、其他多 4（62→58）。",
        },
        "local_coverage": {
            "kline_daily_367d_hit": len(hit_367),
            "kline_enriched_hit": len(hit_enr),
            "fuyao_10d_hit": len(hit_10d),
            "no_local_copy": len(miss_syms - hit_367 - hit_10d),
            "kline_daily_universe": len(cov_367),
        },
        "bias_bound": {
            "assumption": "缺口组全部缺席，且按 instrument 数量等权并入池均值",
            "weight": round(w, 5),
            "required_gap_to_break_even_bp": round(need_gap, 1),
            "ladder": ladder,
        },
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "universe_gap.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    missing.select(["symbol", "name", "exchange", "listing_date", "cls",
                    "in_kline_daily", "in_kline_enriched", "in_fuyao_10d"]).write_csv(
        OUT / "universe_gap.csv")

    print(f"instruments {n_inst}  dump {n_dump}  缺席 {n_miss}  (dump 多出 {len(dump_syms - inst_syms)})")
    print("分类：", counts)
    print(f"本地副本：367天生产缓存命中 {len(hit_367)}  enriched {len(hit_enr)}  "
          f"10日 dump {len(hit_10d)}  完全无副本 {len(miss_syms - hit_367 - hit_10d)}")
    print(f"缺口权重 m/(n+m) = {w:.5f}")
    print(f"要把最好仅多格从 +{BEST_LONG_BP}bp 抬到打平 {COST_BP}bp 成本，"
          f"缺口组须系统性低于基准 {-need_gap:.0f}bp/20日")
    for row in ladder:
        print(f"  缺口 {row['assumed_gap_bp']:+7.1f}bp ⇒ 超额偏 {row['bias_on_long_only_bp']:+6.2f}bp"
              f" ⇒ 最好格净 {row['best_cell_net_bp']:+6.2f}bp")


if __name__ == "__main__":
    main()
