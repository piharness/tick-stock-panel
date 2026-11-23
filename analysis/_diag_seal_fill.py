"""§8.1 成交率代理检验: 首触封单额/成交额 能否区分封住与炸板.

问题(analysis/_diag_board_shape_findings.md §8.1): 打板可成交子集期望为负
(2 板 −1.14%, 17/17 年为负), 属于「成交概率与结果负相关」的选择偏差。
若「封单额/成交额」在封住组显著高于炸板组, 成交概率就有可观测代理,
条件化打板才值得再回测; 否则打板排除结论维持, 不启用任何板上买规则。

数据(2026-11 起由 depth_service 逐轮落盘):
  depth5_touch  当日触板轨迹(每轮: fetched_at, symbol, bid1_vol, ...)
  depth5        收盘定版快照(sealed_up = 收盘真封, 交叉校验用)
  kline_daily_enriched  amount / raw_close / close(前复权) / S-1 日 → lim, 板高

口径:
  触板事件 = depth5_touch 中出现的 (date, symbol) — 触板即入轨迹(逐轮轮询,
    首触时间精度 = 轮询间隔 10~120s); 首触轮 = 当日最早 fetched_at
  封单额 = 首触轮 bid1_vol × lim(S), lim 由 S-1 日 raw_close 按板块/ST 规则生成
    (bid1_vol 单位若为手而非股, 比值整体等比缩放, 组间比较与秩检验不受影响)
  板高 board_try = S-1 日 consecutive_limit_ups + 1, 只分析 ≥2(二板及以上)
  封住组 = S 日收盘涨停(raw_close ≥ lim−0.005); 炸板组 = 触板但收盘未涨停
  T+1 gap = open_adj(T+1) / (lim × close_adj/close_raw) − 1, 与诊断脚本 r_lim_gap1 同口径
检验:
  ① 封住组 vs 炸板组 的首触 封单额/成交额 — Mann-Whitney U(正态近似, 并列校正)
  ② 封住组内 ratio 与 T+1 gap 的 Spearman 相关(封单厚度预测次日溢价)
  ③ 按板高分层的组间中位数差
无 scipy, 秩和检验 numpy 手写; 事件数 <30 时只报分布不报 p。

用法: backend/.venv/Scripts/python.exe analysis/_diag_seal_fill.py
输出: stdout 摘要 + data/.scratch/seal_fill.json
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date as _date
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.price_limits import polars_limit_price, polars_price_limit_pct  # noqa: E402

DATA = ROOT / "data"
TOUCH_DIR = DATA / "depth5_touch"
DEPTH5_DIR = DATA / "depth5"
ENRICHED = DATA / "kline_daily_enriched"
OUT = DATA / ".scratch" / "seal_fill.json"
MIN_BOARD = 2
MIN_EVENTS_FOR_TEST = 30


def enriched_dates() -> list[str]:
    return sorted(
        p.name[5:] for p in ENRICHED.iterdir()
        if p.name.startswith("date=") and (p / "part.parquet").exists()
    )


def _dates_with(dir_: Path) -> list[str]:
    if not dir_.exists():
        return []
    return sorted(
        p.name[5:] for p in dir_.iterdir()
        if p.name.startswith("date=") and (p / "part.parquet").exists()
    )


def _read_enriched(d: str, cols: list[str]) -> pl.DataFrame | None:
    p = ENRICHED / f"date={d}" / "part.parquet"
    if not p.exists():
        return None
    return pl.read_parquet(p, columns=cols)


def _rankdata(v: np.ndarray) -> np.ndarray:
    """平均秩(并列取平均), 等价 scipy.stats.rankdata。"""
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v), dtype=float)
    sv = v[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def mannwhitney(a: np.ndarray, b: np.ndarray) -> dict | None:
    """Mann-Whitney U, 正态近似(含并列校正)。z>0 表示 a 偏大; 返回单侧 p。"""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None
    allv = np.concatenate([a, b])
    n = len(allv)
    ranks = _rankdata(allv)
    u1 = float(ranks[:n1].sum()) - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    _, counts = np.unique(allv, return_counts=True)
    tie_term = float((counts ** 3 - counts).sum() / (n * (n - 1))) if n > 1 else 0.0
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - tie_term))
    if sigma == 0:
        return None
    z = (u1 - mu) / sigma
    return {"u1": round(u1, 1), "z": round(z, 3),
            "p_one_sided": round(0.5 * math.erfc(abs(z) / math.sqrt(2)), 5)}


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 3 or len(x) != len(y):
        return None
    rx, ry = _rankdata(x), _rankdata(y)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return round(float(np.corrcoef(rx, ry)[0, 1]), 3)


def _finalize_sealed(d: str) -> dict[str, bool | None]:
    p = DEPTH5_DIR / f"date={d}" / "part.parquet"
    if not p.exists():
        return {}
    return {r["symbol"]: r["sealed_up"]
            for r in pl.read_parquet(p).to_dicts()}


def build_events(dates: list[str]) -> pl.DataFrame:
    """跨日期拼接触板事件表: 首触封单额 / 板高 / 是否封住 / T+1 gap。"""
    rows: list[dict] = []
    idx = {d: i for i, d in enumerate(dates)}
    for d in _dates_with(TOUCH_DIR):
        touch = pl.read_parquet(
            TOUCH_DIR / f"date={d}" / "part.parquet"
        ).filter(pl.col("status") == "limit_up")
        if touch.is_empty():
            continue
        first = (
            touch.sort("fetched_at")
            .group_by("symbol")
            .agg([
                pl.col("fetched_at").first().alias("first_ts"),
                pl.col("bid1_vol").first().alias("first_bid1_vol"),
                pl.len().alias("rounds"),
                pl.col("bid1_vol").last().alias("last_bid1_vol"),
            ])
        )
        cur = _read_enriched(d, ["symbol", "amount", "raw_close", "close"])
        prev = _read_enriched(dates[idx[d] - 1], ["symbol", "raw_close",
                                                  "consecutive_limit_ups"]) \
            if idx.get(d, 0) > 0 else None
        if cur is None or prev is None:
            continue
        prev = prev.select(
            (pl.col("consecutive_limit_ups").fill_null(0) + 1).alias("board_try"),
            pl.col("raw_close").alias("prev_raw"),
            "symbol",
        )
        df = first.join(cur, on="symbol", how="inner").join(prev, on="symbol", how="inner")
        df = df.with_columns(pl.lit(_date.fromisoformat(d)).alias("_d")).with_columns(
            polars_price_limit_pct(pl.col("symbol"), pl.col("_d"), pl.lit(False)).alias("lim_pct")
        ).with_columns(
            polars_limit_price(pl.col("prev_raw"), pl.col("lim_pct"), up=True).alias("lim")
        ).filter(
            (pl.col("lim") > 0) & (pl.col("amount") > 0)
        ).with_columns([
            (pl.col("raw_close") >= pl.col("lim") - 0.005).alias("lu"),
            (pl.col("first_bid1_vol") * pl.col("lim") / pl.col("amount")).alias("ratio"),
            (pl.col("close") / pl.col("raw_close")).alias("adj"),
        ]).with_columns(
            (pl.col("lim") * pl.col("adj")).alias("lim_adj")
        )
        # T+1 gap: 次交易日分区的前复权 open
        nd = dates[idx[d] + 1] if idx[d] + 1 < len(dates) else None
        nxt = _read_enriched(nd, ["symbol", "open"]) if nd else None
        if nxt is not None:
            df = df.join(
                nxt.rename({"open": "open_next"}).select(["symbol", "open_next"]),
                on="symbol", how="left",
            ).with_columns(
                (pl.col("open_next") / pl.col("lim_adj") - 1).alias("gap1")
            )
        else:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("gap1"))
        sealed_final = _finalize_sealed(d)
        rows.extend(
            {
                "date": d,
                "symbol": r["symbol"],
                "board_try": int(r["board_try"]),
                "ratio": float(r["ratio"]),
                "rounds": int(r["rounds"]),
                "last_bid1_vol": int(r["last_bid1_vol"]),
                "lu": bool(r["lu"]),
                "sealed_final": sealed_final.get(r["symbol"]),
                "gap1": r["gap1"],
            }
            for r in df.select(
                ["symbol", "board_try", "ratio", "rounds", "last_bid1_vol",
                 "lu", "gap1"]
            ).to_dicts()
        )
    return pl.DataFrame(rows, schema={
        "date": pl.Utf8, "symbol": pl.Utf8, "board_try": pl.Int64,
        "ratio": pl.Float64, "rounds": pl.Int64, "last_bid1_vol": pl.Int64,
        "lu": pl.Boolean, "sealed_final": pl.Boolean, "gap1": pl.Float64,
    })


def _fallback_finalize_stats(out: dict) -> None:
    """无触板轨迹时的降级视图: 收盘定版 封单量/成交额。
    只有封住组(炸板股不进定版), 仅验证数据链路是否活着, 不构成检验。"""
    stats = []
    for d in _dates_with(DEPTH5_DIR):
        dep = pl.read_parquet(DEPTH5_DIR / f"date={d}" / "part.parquet").filter(
            (pl.col("status") == "limit_up") & (pl.col("bid1_vol") > 0)
        )
        cur = _read_enriched(d, ["symbol", "amount"])
        if dep.is_empty() or cur is None:
            continue
        j = dep.join(cur, on="symbol", how="inner").with_columns(
            (pl.col("bid1_vol") / pl.col("amount")).alias("vol_ratio")  # 单位混合, 只看链路
        )
        if j.height:
            stats.append({"date": d, "n": j.height,
                          "median_vol_ratio": round(float(j["vol_ratio"].median()), 4)})
    if stats:
        print("\n[降级视图] 收盘定版 封单量/成交额(单位混合, 仅验链路, 非检验):")
        for s in stats:
            print(f"  {s['date']}: n={s['n']} median={s['median_vol_ratio']}")
        out["fallback_finalize"] = stats
    else:
        print("[降级视图] depth5 定版亦无可用数据。")


def main() -> None:
    dates = enriched_dates()
    touch_dates = _dates_with(TOUCH_DIR)
    print(f"depth5_touch 日期数: {len(touch_dates)} {touch_dates[:5]}{'...' if len(touch_dates) > 5 else ''}")

    out: dict = {"touch_dates": touch_dates}
    if not touch_dates:
        print("触板轨迹为空(收集尚未开始)。depth5_touch 由 depth_service 盘中轮询逐轮")
        print("落盘, 交易时段外的 boot_check 补跑不写入; 预期下一个交易日 15:02 定版后")
        print("出现首个文件, 累计 20~60 个交易日后本检验才有统计意义。")
        _fallback_finalize_stats(out)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    ev = build_events(dates).filter(pl.col("board_try") >= MIN_BOARD)
    out["n_events"] = ev.height
    sealed = ev.filter(pl.col("lu"))
    broken = ev.filter(~pl.col("lu"))
    print(f"\n触板事件(板高≥{MIN_BOARD}): {ev.height}  封住 {sealed.height} / 炸板 {broken.height}")

    for tag, sub in (("封住组", sealed), ("炸板组", broken)):
        v = sub["ratio"].drop_nulls().to_numpy()
        if len(v):
            print(f"{tag}: n={len(v)} mean={np.mean(v):.4f} median={np.median(v):.4f} "
                  f"p25={np.quantile(v, .25):.4f} p75={np.quantile(v, .75):.4f}")
        else:
            print(f"{tag}: n=0")

    va = sealed["ratio"].drop_nulls().to_numpy()
    vb = broken["ratio"].drop_nulls().to_numpy()
    mw = mannwhitney(va, vb) if (len(va) + len(vb)) >= MIN_EVENTS_FOR_TEST else None
    if mw:
        print(f"Mann-Whitney 封住>炸板: U={mw['u1']} z={mw['z']} 单侧p={mw['p_one_sided']}")
        verdict = "成立" if (mw["z"] > 0 and mw["p_one_sided"] < 0.05) else "不成立"
        print(f"§8.1 判据(封单额/成交额 区分封住与炸板): {verdict}")
    else:
        print(f"事件数不足 {MIN_EVENTS_FOR_TEST}, 暂不做秩检验 — 继续落盘 20~60 个交易日。")
    out.update({
        "sealed_n": sealed.height, "broken_n": broken.height,
        "sealed_ratio_median": float(np.median(va)) if len(va) else None,
        "broken_ratio_median": float(np.median(vb)) if len(vb) else None,
        "mannwhitney": mw,
    })

    sg = sealed.filter(pl.col("gap1").is_not_null())
    if sg.height >= 3:
        rho = spearman(sg["ratio"].to_numpy(), sg["gap1"].to_numpy())
        print(f"封住组内 Spearman(ratio, T+1 gap) = {rho}  n={sg.height}")
        out["spearman_ratio_gap"] = rho

    by_board: dict[int, dict] = {}
    for b in sorted({int(x) for x in ev["board_try"].to_list()}):
        s = ev.filter((pl.col("board_try") == b) & pl.col("lu"))["ratio"].drop_nulls()
        k = ev.filter((pl.col("board_try") == b) & ~pl.col("lu"))["ratio"].drop_nulls()
        by_board[b] = {
            "sealed_median": round(float(s.median()), 4) if s.len() else None,
            "broken_median": round(float(k.median()), 4) if k.len() else None,
        }
        print(f"板高 {b}: 封住中位 {by_board[b]['sealed_median']} / 炸板中位 {by_board[b]['broken_median']}")
    out["by_board"] = by_board

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
