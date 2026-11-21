"""「等第 3/4 次托底信号再入场」——这个条件在数据里是否有增量信息。

命题（用户版本）
  托底不是一次到位，而是一段过程。第 1、2 次信号只是「试探」，等到第 3、4 次才入场，
  因为「历史不会简单重复，但会惊人相似」。等价说法：段内序号 k 越大，后续收益越好。

三个必须先讲清的口径问题
  1. 「第 k 次」不是未来函数：段起点与 k 都只依赖已经发生的信号（gap 阈值向前看）。
     但它是**事后才知道段何时结束**的——入场后你并不知道还有没有第 k+1 次。
  2. 「k≥3」与「时间后段」高度共线。一段托底里 k 越大必然日期越晚，
     所以任何 k 的效应都必须与「段内时间后移」比较，否则测的是日历。
  3. 重叠：同段内相邻信号间隔仅数日，fwd250 几乎完全重叠。
     ⇒ 推断单位必须是「段」或「段内配对」，不能按信号条数算 t。

检验设计
  A 描述：段数、每段信号次数分布、每段起止。样本够不够看一眼就知道。
  B 按 k 分组：fwd20/60/120/250 与 250 日 MDD。
  C 段内配对：同段内 (k>=3) − (k<=2)，时间效应被段内差分消掉。段级 bootstrap。
  D 负控（关键）：段内「晚期随机日」——取与 k>=3 信号等量、落在该段时间区间后段的日期，
     看其 fwd 与 k>=3 信号是否有差别。若相同，则 k 无信息，只是「买在下跌的后半程」。
  E 置换检验：同段内随机打乱 k 标签 2000 次，真实差值落在哪。
  F 跨轮次（段序号）：第 1/2/3/4+ 轮托底各自的收益，检验「越晚的轮次越好」是否只是时间趋势。
  G 不依赖 ETF 的长样本版（1991-2026，纯大跌代理），因为 ETF 信号只能从 2012 起。

用法  python analysis/_diag_trough_sequence.py [--write|--check]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
IDX = DATA / "backfill_20261117" / "index_daily_full.parquet"
ETF = DATA / "backfill_20261117" / "etf_daily_full.parquet"
OUT = ROOT / "analysis" / "_diag_trough_sequence_result.json"

SH = "000001.SH"
IDX300 = "000300.SH"
ETF300 = "510300.SH"

BIG_DOWN = -0.015      # 信号的大跌阈值（与 national_team 一致）
GAPS = (10, 20, 40)    # 聚簇间隔，主结果用 20
HORIZONS = (20, 60, 120, 250)
RNG_SEED = 20261121

# 长样本版的纯大跌阈值（不含 ETF 条件）
PLAIN_TH = -0.025


def load() -> dict:
    idx = (
        pl.scan_parquet(IDX)
        .filter(pl.col("symbol").is_in([SH, IDX300]))
        .select(["symbol", "date", "close"])
        .collect()
    )
    etf = (
        pl.scan_parquet(ETF)
        .filter(pl.col("symbol") == ETF300)
        .select(["date", "close"])
        .collect()
    )
    sh = idx.filter(pl.col("symbol") == SH).select(["date", "close"]).sort("date").rename({"close": "c_sh"})
    i3 = idx.filter(pl.col("symbol") == IDX300).select(["date", "close"]).sort("date").rename({"close": "c_idx"})
    j = (
        sh.join(i3, on="date", how="inner")
        .join(etf.rename({"close": "c_etf"}), on="date", how="inner")
        .sort("date")
        .drop_nulls()
    )
    # 长样本：只有 SH 与 300
    jl = sh.join(i3, on="date", how="inner").sort("date").drop_nulls()
    return {"j": j, "jl": jl, "sh_only": sh}


def build(j: pl.DataFrame, use_etf: bool = True) -> dict:
    c_etf = j["c_etf"].to_numpy() if use_etf else None
    c_idx = j["c_idx"].to_numpy()
    c_sh = j["c_sh"].to_numpy()
    n = len(c_idx)
    r_sh = np.full(n, np.nan)
    r_sh[1:] = c_sh[1:] / c_sh[:-1] - 1.0
    if use_etf:
        ex = np.full(n, np.nan)
        r_e = np.full(n, np.nan)
        r_e[1:] = c_etf[1:] / c_etf[:-1] - 1.0
        r_i = np.full(n, np.nan)
        r_i[1:] = c_idx[1:] / c_idx[:-1] - 1.0
        ex[1:] = r_e[1:] - r_i[1:]
        ex5 = np.full(n, np.nan)
        for k in range(n):
            w = ex[max(0, k - 4) : k + 1]
            if np.isfinite(w).sum() >= 3:
                ex5[k] = float(np.nansum(w))
        sig = np.where(np.isfinite(r_sh) & (r_sh <= BIG_DOWN) & np.isfinite(ex5) & (ex5 > 0))[0]
    else:
        sig = np.where(np.isfinite(r_sh) & (r_sh <= PLAIN_TH))[0]
    return {"dates": j["date"].to_numpy(), "c_idx": c_idx, "r_sh": r_sh, "sig": sig}


def fwd(c: np.ndarray, k: int, h: int) -> float:
    """从 k+1 收盘买入，持有 h 日，收益（小数）。不足样本返回 nan。"""
    t0, t1 = k + 1, k + 1 + h
    if t1 >= len(c):
        return float("nan")
    a, b = c[t0], c[t1]
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
        return float("nan")
    return float(b / a - 1.0)


def mdd(c: np.ndarray, k: int, h: int = 250) -> float:
    t0, t1 = k + 1, min(k + 1 + h, len(c))
    if t1 - t0 < 20:
        return float("nan")
    seg = c[t0:t1]
    seg = seg[np.isfinite(seg)]
    if seg.size < 20 or seg[0] <= 0:
        return float("nan")
    run = np.fmax.accumulate(seg)
    return float(np.nanmin(seg / run - 1.0))


def cluster(sig: np.ndarray, gap: int) -> list[list[int]]:
    segs: list[list[int]] = []
    cur: list[int] = []
    for k in sig:
        if cur and int(k) - cur[-1] > gap:
            segs.append(cur)
            cur = []
        cur.append(int(k))
    if cur:
        segs.append(cur)
    return segs


def ci(v: np.ndarray) -> tuple[float, float]:
    if v.size < 2:
        return (float("nan"), float("nan"))
    se = v.std(ddof=1) / math.sqrt(v.size)
    return (float(v.mean() - 1.96 * se), float(v.mean() + 1.96 * se))


def part_a_segments(j: pl.DataFrame, sig: np.ndarray, gap: int) -> dict:
    segs = cluster(sig, gap)
    dates = j["date"].to_numpy()
    rows = []
    for s in segs:
        rows.append(
            {
                "start": str(dates[s[0]]),
                "end": str(dates[s[-1]]),
                "n_sig": len(s),
                "span_td": int(s[-1] - s[0]),
            }
        )
    dist = {}
    for r in rows:
        dist[str(r["n_sig"])] = dist.get(str(r["n_sig"]), 0) + 1
    return {
        "gap": gap,
        "n_segments": len(segs),
        "n_signals": len(sig),
        "size_dist": dict(sorted(dist.items(), key=lambda x: int(x[0]))),
        "segments": rows,
        "n_seg_with_3plus": sum(1 for r in rows if r["n_sig"] >= 3),
    }


def part_b_by_k(data: dict, sig: np.ndarray, segs: list[list[int]], h: int) -> list[dict]:
    c = data["c_idx"]
    rows = []
    flat = []  # (k, 段id, 信号索引)
    for si, s in enumerate(segs):
        for rank, k in enumerate(s, start=1):
            flat.append((rank, si, k))
    for label, pred in (
        ("k=1", lambda r: r == 1),
        ("k=2", lambda r: r == 2),
        ("k=3", lambda r: r == 3),
        ("k>=4", lambda r: r >= 4),
        ("k<=2", lambda r: r <= 2),
        ("k>=3", lambda r: r >= 3),
        ("全部", lambda r: True),
    ):
        ks = [k for r, _, k in flat if pred(r)]
        v = np.array([fwd(c, k, h) for k in ks])
        v = v[np.isfinite(v)]
        if v.size < 5:
            rows.append({"group": label, "n": int(v.size), "note": "样本不足"})
            continue
        lo, hi = ci(v)
        rows.append(
            {
                "group": label,
                "n": int(v.size),
                "n_seg": len({si for r, si, _ in flat if pred(r)}),
                "mean_pct": round(float(v.mean()) * 100, 2),
                "med_pct": round(float(np.median(v)) * 100, 2),
                "win": round(float((v > 0).mean()), 3),
                "ci95_pct": [round(lo * 100, 2), round(hi * 100, 2)],
            }
        )
    return rows


def part_c_paired(data: dict, segs: list[list[int]], h: int, thresh: int = 3) -> dict:
    """段内配对：(k>=thresh) − (k<thresh)；以段为独立单位。"""
    c = data["c_idx"]
    diffs = []
    detail = []
    for si, s in enumerate(segs):
        early = [fwd(c, k, h) for k, r in zip(s, range(1, len(s) + 1)) if r < thresh]
        late = [fwd(c, k, h) for k, r in zip(s, range(1, len(s) + 1)) if r >= thresh]
        early = [x for x in early if np.isfinite(x)]
        late = [x for x in late if np.isfinite(x)]
        if not early or not late:
            continue
        d = float(np.mean(late) - np.mean(early))
        diffs.append(d)
        detail.append({"seg": si, "n_early": len(early), "n_late": len(late), "diff_pct": round(d * 100, 2)})
    v = np.array(diffs)
    if v.size < 3:
        return {"h": h, "n_seg": int(v.size), "verdict": "样本不足（可配对段 < 3）"}
    lo, hi = ci(v)
    return {
        "h": h,
        "n_seg": int(v.size),
        "diff_pct": round(float(v.mean()) * 100, 2),
        "t": round(float(v.mean() / (v.std(ddof=1) / math.sqrt(v.size))), 2),
        "ci95_pct": [round(lo * 100, 2), round(hi * 100, 2)],
        "n_seg_positive": int((v > 0).sum()),
        "detail": detail,
    }


def part_d_negctrl(data: dict, segs: list[list[int]], h: int, thresh: int = 3,
                   reps: int = 50, seed: int = 7) -> dict:
    """负控：段内「后半程的非信号日」——若 k>=3 的优势只是「买在段的后半程」，这些日子应当一样好。

    设计要点（第一版踩过）：随机池**必须排除信号日本身**。
    段往往只有几个交易日跨度，若池子与信号日重叠，抽出来的「随机日」就是信号日本身，
    差值被结构性地压成 0，负控会假通过。这里池子 = 段内晚于第 thresh-1 个信号、且不是信号的交易日。
    """
    c = data["c_idx"]
    rng = np.random.default_rng(seed)
    real, fake = [], []
    for s in segs:
        late = [k for k, r in zip(s, range(1, len(s) + 1)) if r >= thresh]
        if not late:
            continue
        v = np.array([fwd(c, k, h) for k in late])
        v = v[np.isfinite(v)]
        if v.size == 0:
            continue
        real.append(float(v.mean()))
        sigset = set(s)
        start = s[thresh - 2] + 1
        pool = [t for t in range(start, s[-1] + 1) if t not in sigset]
        if len(pool) < len(late):
            pool = [t for t in range(s[0], s[-1] + 1) if t not in sigset]
        if len(pool) < len(late):
            continue
        acc = []
        for _ in range(reps):
            pick = rng.choice(pool, size=len(late), replace=False)
            vv = np.array([fwd(c, int(k), h) for k in pick])
            vv = vv[np.isfinite(vv)]
            if vv.size:
                acc.append(float(vv.mean()))
        if acc:
            fake.append(float(np.mean(acc)))
    if len(real) < 3 or len(fake) < 3:
        return {"h": h, "verdict": "样本不足"}
    a, b = np.array(real), np.array(fake)
    d = a - b
    se = d.std(ddof=1) / math.sqrt(d.size) if d.size > 1 else float("nan")
    return {
        "h": h,
        "n_seg": int(a.size),
        "real_pct": round(float(a.mean()) * 100, 2),
        "fake_late_random_pct": round(float(b.mean()) * 100, 2),
        "gap_pct": round(float(d.mean()) * 100, 2),
        "gap_t": round(float(d.mean() / se), 2) if np.isfinite(se) and se > 0 else None,
        "n_seg_gap_positive": int((d > 0).sum()),
        "note": "real=k>=3 信号（段内等权）；fake=同段后半程的非信号日、等量、重复 50 次取均值。"
                "gap 接近 0 ⇒ k 无增量信息，效应只是「买得更晚」",
    }


def part_e_permute(data: dict, segs: list[list[int]], h: int, thresh: int = 3,
                   n_iter: int = 2000, seed: int = RNG_SEED) -> dict:
    """同段内随机打乱 k 标签：真实 (k>=3 − k<3) 差值落在置换分布的哪个分位。

    口径必须与 part_c 一致（段内先平均、再跨段平均）。第一版用逐条信号平均，
    结果与 part_c 符号相反——那不是矛盾，是段间权重差异造成的 Simpson 悖论。
    """
    c = data["c_idx"]
    use = [s for s in segs if len(s) >= thresh]
    if len(use) < 3:
        return {"h": h, "verdict": "可配对段不足"}
    fw = [[fwd(c, k, h) for k in s] for s in use]

    def stat(ranks: list[np.ndarray]) -> float:
        ds = []
        for f, r in zip(fw, ranks):
            fv, ok = np.array(f, dtype=float), None
            ok = np.isfinite(fv)
            late = fv[ok & (r >= thresh)]
            early = fv[ok & (r < thresh)]
            if late.size == 0 or early.size == 0:
                continue
            ds.append(float(late.mean() - early.mean()))
        return float(np.mean(ds)) if ds else float("nan")

    base = [np.arange(1, len(s) + 1) for s in use]
    real = stat(base)
    rng = np.random.default_rng(seed)
    null = []
    for _ in range(n_iter):
        rp = [rng.permutation(r) for r in base]
        v = stat(rp)
        if np.isfinite(v):
            null.append(v)
    null = np.array(null)
    p = float((np.abs(null) >= abs(real)).mean()) if null.size else float("nan")
    return {
        "h": h,
        "real_diff_pct": round(real * 100, 2),
        "null_mean_pct": round(float(null.mean()) * 100, 2) if null.size else None,
        "null_sd_pct": round(float(null.std(ddof=1)) * 100, 2) if null.size > 1 else None,
        "p_two_sided": round(p, 4) if np.isfinite(p) else None,
        "n_iter": n_iter,
        "n_seg": len(use),
        "note": "段内等权口径（与 C 一致）；p 大 ⇒ 段内顺序与随机排序无法区分",
    }


def part_f_rounds(data: dict, sig: np.ndarray, h: int, big_gap: int = 250) -> dict:
    """「第几轮托底」：用年度级间隔聚簇（gap=250 交易日），这才是用户说的「第 3/4 次托底」。

    第一版用 gap=20 的段序号做这个维度，结果 53 段里 50 段都落进「第4轮+」——
    短聚簇把「轮」切得太碎，「第几轮」这个轴等于不存在。
    每个轮次报两个口径：轮内全部信号的均值，以及轮内第一个信号（代表点，无重叠）。
    """
    c = data["c_idx"]
    dates = data["dates"]
    rounds = cluster(sig, big_gap)
    rows = []
    for label, pred in (("第1轮", lambda r: r == 1), ("第2轮", lambda r: r == 2),
                        ("第3轮", lambda r: r == 3), ("第4轮+", lambda r: r >= 4)):
        v_all, v_first, spans = [], [], []
        for ri, s in enumerate(rounds, start=1):
            if not pred(ri):
                continue
            xs = [fwd(c, k, h) for k in s]
            xs = [x for x in xs if np.isfinite(x)]
            if xs:
                v_all.append(float(np.mean(xs)))
            x0 = fwd(c, s[0], h)
            if np.isfinite(x0):
                v_first.append(x0)
            spans.append(f"{dates[s[0]]}..{dates[s[-1]]}")
        if len(v_all) < 2:
            rows.append({"round": label, "n_rounds": len(v_all), "note": "样本不足"})
            continue
        a, b = np.array(v_all), np.array(v_first)
        rows.append({
            "round": label,
            "n_rounds": int(a.size),
            "n_sig": int(sum(len(s) for ri, s in enumerate(rounds, start=1) if pred(ri))),
            "all_mean_pct": round(float(a.mean()) * 100, 2),
            "all_win": round(float((a > 0).mean()), 3),
            "first_sig_mean_pct": round(float(b.mean()) * 100, 2) if b.size else None,
            "first_sig_win": round(float((b > 0).mean()), 3) if b.size else None,
            "rounds": spans,
        })
    return {"big_gap": big_gap, "h": h, "n_rounds_total": len(rounds), "groups": rows,
            "all_round_spans": [f"{dates[s[0]]}..{dates[s[-1]]}({len(s)})" for s in rounds]}


def part_i_segment_detail(data: dict, segs: list[list[int]], h: int, thresh: int = 3) -> list[dict]:
    """逐段明细：+6pct 到底是哪几段贡献的，以及 k>=3 的入场价相对段末低多少（事后信息，仅用于解释）。"""
    c = data["c_idx"]
    dates = data["dates"]
    rows = []
    for si, s in enumerate(segs):
        if len(s) < thresh:
            continue
        early = [fwd(c, k, h) for k, r in zip(s, range(1, len(s) + 1)) if r < thresh]
        late_idx = [k for k, r in zip(s, range(1, len(s) + 1)) if r >= thresh]
        late = [fwd(c, k, h) for k in late_idx]
        early = [x for x in early if np.isfinite(x)]
        late = [x for x in late if np.isfinite(x)]
        if not early or not late:
            continue
        # 段内最低收盘（事后），看 k>=3 的入场价离它多远
        lo = float(np.nanmin(c[s[0] : s[-1] + 1]))
        px_first = float(c[s[0]])
        px_late = float(np.mean([c[k] for k in late_idx]))
        rows.append({
            "start": str(dates[s[0]]),
            "end": str(dates[s[-1]]),
            "n_sig": len(s),
            "diff_pct": round((float(np.mean(late)) - float(np.mean(early))) * 100, 2),
            "first_entry_vs_seg_low_pct": round((px_first / lo - 1.0) * 100, 2),
            "late_entry_vs_seg_low_pct": round((px_late / lo - 1.0) * 100, 2),
        })
    pos = sum(1 for r in rows if r["diff_pct"] > 0)
    return {"h": h, "n_seg": len(rows), "n_positive": pos, "rows": rows,
            "note": "first/late_entry_vs_seg_low = 入场价高于段内最低收盘的百分比（事后口径，只用于判断机制）"}


def part_j_year(data: dict, sig: np.ndarray, h: int) -> list[dict]:
    """按自然年分组：用户说的「第 3/4 次托底」在现实里对应 2015/2018/2022/2024 这些年份。"""
    c = data["c_idx"]
    dates = data["dates"]
    years = np.array([int(str(x)[:4]) for x in dates])
    rows = []
    for y in sorted(set(years[sig].tolist())):
        v = [fwd(c, k, h) for k in sig if years[k] == y]
        v = [x for x in v if np.isfinite(x)]
        if len(v) < 3:
            rows.append({"year": y, "n": len(v), "note": "样本不足"})
            continue
        a = np.array(v)
        rows.append({"year": y, "n": int(a.size),
                     "mean_pct": round(float(a.mean()) * 100, 2),
                     "med_pct": round(float(np.median(a)) * 100, 2),
                     "win": round(float((a > 0).mean()), 3)})
    return rows
def part_h_relative_position(data: dict, segs: list[list[int]], h: int) -> dict:
    """把「第几次信号」翻译成「段内位置」：相对段起点的跌幅 与 时间进度。

    这是负控的正面版本：若效应来自「跌得更多」而不是「次数」，按跌幅分桶应当复现同样的单调性。
    """
    c = data["c_idx"]
    recs = []
    for s in segs:
        if len(s) < 2:
            continue  # 单信号段没有「段内位置」，混进来会把 Q1 灌满
        c0 = c[s[0]]
        if not np.isfinite(c0) or c0 <= 0:
            continue
        span = max(s[-1] - s[0], 1)
        for k in s:
            x = fwd(c, k, h)
            if not np.isfinite(x) or not np.isfinite(c[k]):
                continue
            recs.append((float(c[k] / c0 - 1.0), float((k - s[0]) / span), x))
    if len(recs) < 30:
        return {"h": h, "verdict": "样本不足"}
    arr = np.array(recs)
    dd, prog, y = arr[:, 0], arr[:, 1], arr[:, 2]
    rows = []
    for name, x in (("相对段起点跌幅", dd), ("段内时间进度", prog)):
        qs = np.quantile(x, [0.25, 0.5, 0.75])
        edges = [-np.inf, qs[0], qs[1], qs[2], np.inf]
        blk = []
        for i in range(4):
            m = (x > edges[i]) & (x <= edges[i + 1])
            if m.sum() < 10:
                blk.append({"q": f"Q{i+1}", "n": int(m.sum()), "note": "样本不足"})
                continue
            yy = y[m]
            blk.append({"q": f"Q{i+1}", "n": int(m.sum()),
                        "x_mean": round(float(x[m].mean()) * (100 if name.endswith("跌幅") else 1), 2),
                        "fwd_mean_pct": round(float(yy.mean()) * 100, 2),
                        "fwd_med_pct": round(float(np.median(yy)) * 100, 2),
                        "win": round(float((yy > 0).mean()), 3)})
        rows.append({"axis": name, "buckets": blk})
    return {"h": h, "n": int(len(recs)), "axes": rows,
            "note": "若「相对段起点跌幅」桶单调且「段内时间进度」不单调，则效应来自价格而非次数"}


def part_g_long(plain: dict, gap: int, h: int) -> dict:
    """长样本（1991-2026，无 ETF 条件）：纯大跌序列的 k 效应。控制「早期样本波动更大」的干扰。"""
    segs = cluster(plain["sig"], gap)
    c = plain["c_idx"]
    dates = plain["dates"]
    rows = []
    for label, pred in (("k=1", lambda r: r == 1), ("k=2", lambda r: r == 2),
                        ("k=3", lambda r: r == 3), ("k>=4", lambda r: r >= 4)):
        v = []
        for s in segs:
            for k, rk in zip(s, range(1, len(s) + 1)):
                if pred(rk):
                    x = fwd(c, k, h)
                    if np.isfinite(x):
                        v.append(x)
        if len(v) < 10:
            rows.append({"group": label, "n": len(v), "note": "样本不足"})
            continue
        v = np.array(v)
        lo, hi = ci(v)
        rows.append({"group": label, "n": int(v.size), "mean_pct": round(float(v.mean()) * 100, 2),
                     "med_pct": round(float(np.median(v)) * 100, 2), "win": round(float((v > 0).mean()), 3),
                     "ci95_pct": [round(lo * 100, 2), round(hi * 100, 2)]})
    return {"gap": gap, "h": h, "n_segments": len(segs), "n_signals": len(plain["sig"]), "groups": rows}


def main() -> dict:
    w = "--write" in sys.argv
    chk = "--check" in sys.argv
    d = load()
    j = d["j"]
    data = build(j, use_etf=True)
    sig = data["sig"]

    gaps = {}
    for g in GAPS:
        segs = cluster(sig, g)
        gaps[str(g)] = part_a_segments(j, sig, g)

    GAP = 20
    segs = cluster(sig, GAP)
    out = {
        "generated": "2026-11-21",
        "signal_def": f"上证日收益 <= {BIG_DOWN:.1%} 且 沪深300ETF 5日相对指数超额 > 0",
        "sample": [str(j["date"][0]), str(j["date"][-1])],
        "A_segments": gaps,
        "B_by_k": {str(h): part_b_by_k(data, sig, segs, h) for h in HORIZONS},
        "C_paired": {str(h): part_c_paired(data, segs, h) for h in HORIZONS},
        "D_negctrl_late_random": {str(h): part_d_negctrl(data, segs, h) for h in HORIZONS},
        "E_permute": {str(h): part_e_permute(data, segs, h) for h in (60, 250)},
        "F_rounds": {str(h): part_f_rounds(data, sig, h) for h in (60, 250)},
        "H_relative_position": {str(h): part_h_relative_position(data, segs, h) for h in (60, 250)},
        "I_segment_detail": {str(h): part_i_segment_detail(data, segs, h) for h in (250,)},
        "J_year": {str(h): part_j_year(data, sig, h) for h in (60, 250)},
        # C 的 gap 敏感性：段的定义换一个阈值，结论是否还成立
        "C_by_gap": {
            str(g): {str(h): part_c_paired(data, cluster(sig, g), h) for h in (60, 250)}
            for g in GAPS
        },
    }

    pl_ = build(d["jl"], use_etf=False)
    out["G_long_sample"] = {str(h): part_g_long(pl_, GAP, h) for h in (60, 250)}
    out["G_signal_def"] = f"上证日收益 <= {PLAIN_TH:.1%}（无 ETF 条件，1991-2026）"

    # 描述性补充：k>=3 的信号在时间上分布在哪
    dates = data["dates"]
    late_dates = []
    for s in segs:
        for k, r in zip(s, range(1, len(s) + 1)):
            if r >= 3:
                late_dates.append(str(dates[k]))
    out["H_late_signal_dates"] = late_dates
    out["H_n_late"] = len(late_dates)

    if w:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if chk:
        if not OUT.exists():
            print("[check] 缺基线，请先 --write", file=sys.stderr)
            raise SystemExit(2)
        old = json.loads(OUT.read_text(encoding="utf-8"))
        pairs = [
            ("C_paired.250.diff_pct", out["C_paired"]["250"].get("diff_pct"), old["C_paired"]["250"].get("diff_pct")),
            ("D.250.gap_pct", out["D_negctrl_late_random"]["250"].get("gap_pct"),
             old["D_negctrl_late_random"]["250"].get("gap_pct")),
            ("B.250.k3.mean", out["B_by_k"]["250"][2].get("mean_pct"), old["B_by_k"]["250"][2].get("mean_pct")),
        ]
        bad = []
        for name, a, b in pairs:
            if a is None or b is None:
                continue
            rel = abs(a - b) / max(abs(b), 1e-9)
            flag = "OK" if rel <= 0.05 else "DRIFT"
            print(f"[check] {name:<22} now={a:>8.2f} base={b:>8.2f} rel={rel:>6.2%} {flag}")
            if rel > 0.05:
                bad.append(name)
        raise SystemExit(1 if bad else 0)
    return out


def report(out: dict) -> None:
    print(f"信号: {out['signal_def']}")
    print(f"样本: {out['sample'][0]} .. {out['sample'][1]}")
    a = out["A_segments"]
    for g, blk in a.items():
        print(f"\n-- gap={g}: {blk['n_segments']} 段 / {blk['n_signals']} 个信号；"
              f"含 3+ 信号的段 {blk['n_seg_with_3plus']} 个；段长分布 {blk['size_dist']}")
    blk = a["20"]
    print("\n每段明细（gap=20）:")
    for r in blk["segments"]:
        print(f"  {r['start']} .. {r['end']}  {r['n_sig']} 次  跨度 {r['span_td']} 交易日")

    for h in HORIZONS:
        print(f"\n== B 按段内序号 k 分组，持有 {h} 日 ==")
        print(f'{"组":<8}{"N":>5}{"段数":>5}{"均值%":>9}{"中位%":>9}{"胜率":>7}  CI95')
        for r in out["B_by_k"][str(h)]:
            if "mean_pct" not in r:
                print(f'{r["group"]:<8}{r["n"]:>5}  样本不足')
                continue
            print(f'{r["group"]:<8}{r["n"]:>5}{r["n_seg"]:>5}{r["mean_pct"]:>9.2f}{r["med_pct"]:>9.2f}'
                  f'{r["win"]:>7.1%}  [{r["ci95_pct"][0]:>7.2f},{r["ci95_pct"][1]:>7.2f}]')

    print("\n== C 段内配对 (k>=3) − (k<=2) ==")
    for h in HORIZONS:
        r = out["C_paired"][str(h)]
        if "diff_pct" not in r:
            print(f'  h={h:>3}: {r.get("verdict")}')
            continue
        print(f'  h={h:>3}: 差 {r["diff_pct"]:>6.2f}pct  t={r["t"]:>6.2f}  '
              f'CI[{r["ci95_pct"][0]:>7.2f},{r["ci95_pct"][1]:>7.2f}]  '
              f'段数 {r["n_seg"]}  正 {r["n_seg_positive"]}/{r["n_seg"]}')

    print("\n== D 负控：k>=3 信号 vs 同段后半程的【非信号日】 ==")
    for h in HORIZONS:
        r = out["D_negctrl_late_random"][str(h)]
        if "real_pct" not in r:
            print(f'  h={h:>3}: {r.get("verdict")}')
            continue
        print(f'  h={h:>3}: 真实 {r["real_pct"]:>6.2f}%  随机 {r["fake_late_random_pct"]:>6.2f}%  '
              f'差 {r["gap_pct"]:>6.2f}pct  t={r["gap_t"]}  正 {r["n_seg_gap_positive"]}/{r["n_seg"]}')

    print("\n== E 同段内置换 k 标签（段内等权） ==")
    for h, r in out["E_permute"].items():
        if r.get("p_two_sided") is None:
            print(f'  h={h}: {r.get("verdict")}')
            continue
        print(f'  h={h}: 真实差 {r["real_diff_pct"]:>6.2f}pct  零分布 {r["null_mean_pct"]:>6.2f}%'
              f'±{r["null_sd_pct"]:>5.2f}  p={r["p_two_sided"]}  段数 {r["n_seg"]}')

    print("\n== F 第几轮托底（gap=250 交易日聚簇） ==")
    for h, blk in out["F_rounds"].items():
        print(f'  -- 持有 {h} 日：共 {blk["n_rounds_total"]} 轮')
        for r in blk["groups"]:
            if "all_mean_pct" not in r:
                print(f'     {r["round"]:<6} 轮数={r["n_rounds"]} 样本不足')
                continue
            print(f'     {r["round"]:<6} 轮数={r["n_rounds"]:>2} 信号={r["n_sig"]:>3} '
                  f'轮内均值 {r["all_mean_pct"]:>7.2f}% 胜率 {r["all_win"]:>5.1%} | '
                  f'首信号 {r["first_sig_mean_pct"]:>7.2f}% 胜率 {r["first_sig_win"]:>5.1%}')
    print("  轮次区间（gap=250）:", " ; ".join(out["F_rounds"]["250"]["all_round_spans"]))

    print("\n== H 把「第几次」翻译成「跌多少 / 什么时候」 ==")
    for h, blk in out["H_relative_position"].items():
        if "axes" not in blk:
            print(f'  h={h}: {blk.get("verdict")}')
            continue
        print(f'  -- 持有 {h} 日（N={blk["n"]}）')
        for ax in blk["axes"]:
            print(f'     [{ax["axis"]}]')
            for b in ax["buckets"]:
                if "fwd_mean_pct" not in b:
                    print(f'       {b["q"]} n={b["n"]} 样本不足')
                    continue
                print(f'       {b["q"]} n={b["n"]:>3} 自变量均值 {b["x_mean"]:>7.2f}  '
                      f'fwd {b["fwd_mean_pct"]:>7.2f}% 中位 {b["fwd_med_pct"]:>7.2f}% 胜率 {b["win"]:>5.1%}')

    print(f"\n== G 长样本（{out['G_signal_def']}） ==")
    for h, blk in out["G_long_sample"].items():
        print(f'  -- 持有 {h} 日：{blk["n_segments"]} 段 / {blk["n_signals"]} 信号')
        for r in blk["groups"]:
            if "mean_pct" not in r:
                print(f'     {r["group"]:<6} N={r["n"]:>4} 样本不足')
                continue
            print(f'     {r["group"]:<6} N={r["n"]:>4} 均值 {r["mean_pct"]:>7.2f}% 中位 {r["med_pct"]:>7.2f}% '
                  f'胜率 {r["win"]:>5.1%}  CI[{r["ci95_pct"][0]:>7.2f},{r["ci95_pct"][1]:>7.2f}]')

    print(f'\n== H k>=3 的信号日期（共 {out["H_n_late"]} 个） ==')
    print("  " + ", ".join(out["H_late_signal_dates"]))

    print("\n== I 逐段明细（含 3+ 信号的 21 段，h=250） ==")
    blk = out["I_segment_detail"]["250"]
    print(f'  正贡献 {blk["n_positive"]}/{blk["n_seg"]} 段')
    print(f'  {"段起点":<12}{"段末":<12}{"次":>3}{"差%":>8}{"首入场距段低%":>14}{"第3次起距段低%":>16}')
    for r in blk["rows"]:
        print(f'  {r["start"]:<12}{r["end"]:<12}{r["n_sig"]:>3}{r["diff_pct"]:>8.2f}'
              f'{r["first_entry_vs_seg_low_pct"]:>14.2f}{r["late_entry_vs_seg_low_pct"]:>16.2f}')

    print("\n== J 按自然年 ==")
    for h, rows in out["J_year"].items():
        print(f'  -- 持有 {h} 日')
        for r in rows:
            if "mean_pct" not in r:
                continue
            print(f'     {r["year"]}  n={r["n"]:>3}  均值 {r["mean_pct"]:>7.2f}%  中位 {r["med_pct"]:>7.2f}%  胜率 {r["win"]:>5.1%}')

    print("\n== C 的 gap 敏感性（段定义换阈值） ==")
    for g, byh in out["C_by_gap"].items():
        for h, r in byh.items():
            if "diff_pct" not in r:
                print(f'  gap={g:>2} h={h:>3}: {r.get("verdict")}')
                continue
            print(f'  gap={g:>2} h={h:>3}: 差 {r["diff_pct"]:>6.2f}pct t={r["t"]:>6.2f} '
                  f'段数 {r["n_seg"]:>2} 正 {r["n_seg_positive"]}/{r["n_seg"]}')


if __name__ == "__main__":
    res = main()
    report(res)
