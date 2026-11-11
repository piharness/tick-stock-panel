"""L1 量级门的可红检验：121.5bp 是「选股」还是「缺口 + 一段牛市」？

触发问题（2026-11-11）：`_diag_l1_annualize.py` 把回合收益拆成
漂移 9.7 + 缺口 47.2 + 动量溢价 64.5 = 121.5bp。但这个拆法有两处没被检验：

  A. 64.5 是「主动 vs 换手十分位匹配基准」，而基准本身就是在**缺口池内**
     按选股器的十分位分布加权出来的。若选股器只是集中到高 i1 的十分位，
     64.5 是加权造成的，不是标的身份造成的。
  B. 47.2 用的是 `bench - uni`，bench 已带选股器的十分位权重，所以它同时
     包含「缺口效应」和「十分位构成」两段，不能整体记在缺口头上。

本脚本做三件事，全部本地日线、只读：

  1. 三种口径重算「任何低开 1% 的股票都能拿到多少」：
     (a) 缺口池等权        (b) 缺口池按选股器十分位加权（= annualize 的 bench）
     (c) §3 的全市场缺口桶主动（换手十分位匹配、不分选股器）
     三者并排才知道 47.2 里有多少是缺口、多少是构成。
  2. 两个安慰剂（各 200 次重抽，seed 20261111）：
     P-name: 每天从当日缺口池随机抽同样只数（打掉标的身份，保留日期与缺口）
     P-date: 保留当日缺口过滤，把名单换成另一随机交易日的 mom20 名单（打掉日期配对）
     判据是**可红的**：若 P-name 的 95% 区间覆盖 121.5，则 64.5 的「选股溢价」不成立；
     若 P-date 均值 >= 80bp，则名单与日期的配对不承重，§9.4 的分解要改写。
  3. 集中度：按季度与按单日贡献，看 121.5 是不是少数几天扛的。

预注册判据（跑之前写死）：
  R1  P-name 均值应落在缺口池口径附近（±10bp），否则基准构造有偏。
  R2  P-date >= 80bp => 「选股器 + 日期配对」不承重 => 分解作废，改报全市场缺口桶。
  R3  单季贡献超过 60% => 年化数不能当期望，只能当该季度的实现值。

ASCII 输出（GBK 控制台）。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_base_t0 import leg  # noqa: E402
from _diag_gaprev import N_DECILE  # noqa: E402

SEED = 20261111
GAP_MAX = -0.01
N_REP = 200
OUT = Path(__file__).resolve().parent / "_diag_l1_placebo_result.json"


def main() -> None:
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    d_map = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    c, o = mats["close"], mats["open"]
    tradable = np.isfinite(c) & (c > 0) & np.isfinite(o) & (o > 0)
    C2 = leg(mats, "c2")
    I1 = leg(mats, "i1")
    picks = ap.pick_daily(df, "mom20", ascending=False).select("date", "symbols").to_dicts()

    # ---- 逐日：缺口池、选股、十分位权重、各种口径 ----
    rows = []          # dict per usable day
    for r in picks:
        i0 = d_map[str(r["date"])]
        if i0 + 2 >= T:
            continue
        sel = np.array([s_map[x] for x in r["symbols"]], dtype=np.int64)
        c0 = c[i0]
        pool = tradable[i0] & np.isfinite(C2[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
        if pool.sum() < 200:
            continue
        lim = c0 * (1.0 - abs(GAP_MAX))
        fill = pool & tradable[i0 + 1] & (o[i0 + 1] <= lim) & (lim > 0)
        good = sel[fill[sel]]
        if good.size < 3:
            continue
        pool_idx = np.flatnonzero(pool)
        tv = turn[i0][pool_idx]
        order = np.argsort(tv, kind="stable")
        rk = np.empty(pool_idx.size, dtype=np.int64)
        rk[order] = np.arange(pool_idx.size)
        dec_full = np.full(S, -1, dtype=np.int64)
        dec_full[pool_idx] = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
        fpool = pool_idx[fill[pool_idx]]
        if fpool.size < 50:
            continue
        dsel = dec_full[good]
        cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
        if cnt.sum() <= 0:
            continue
        w = cnt / cnt.sum()
        rows.append(dict(
            i0=i0, dsel=dates[i0 + 1], good=good, fpool=fpool,
            dec_full=dec_full, w=w, sel=sel, pool_idx=pool_idx, cnt=cnt,
        ))

    n_day = len(rows)

    def day_cells(r) -> np.ndarray:
        v = I1[r["i0"]][r["good"]]
        return v[np.isfinite(v)]

    # (a) 缺口池等权 / (b) 按选股器十分位加权 / (c) 全市场缺口桶主动
    uni_gap, w_gap, act_bucket, raw_pick, dec_shift = [], [], [], [], []
    used = []
    for r in rows:
        fv = I1[r["i0"]][r["fpool"]]
        fv = fv[np.isfinite(fv)]
        if fv.size < 50:
            continue
        used.append(r)
        uni_gap.append(float(fv.mean()))
        pm = np.full(N_DECILE, np.nan)
        for d in range(N_DECILE):
            m = r["dec_full"][r["fpool"]] == d
            if m.sum() >= 5:
                v = fv[m]
                if v.size >= 5:
                    pm[d] = float(v.mean())
        ok = np.isfinite(pm) & (r["w"] > 0)
        w_gap.append(float(np.dot(r["w"][ok] / r["w"][ok].sum(), pm[ok])) if ok.any() else np.nan)
        # (c) 全市场十分位匹配基准（不分缺口）：池内每十分位的均值
        pmall = np.full(N_DECILE, np.nan)
        for d in range(N_DECILE):
            m = r["dec_full"][r["pool_idx"]] == d
            if m.sum() >= 5:
                v = I1[r["i0"]][r["pool_idx"][m]]
                v = v[np.isfinite(v)]
                if v.size >= 5:
                    pmall[d] = float(v.mean())
        ok2 = np.isfinite(pmall)
        act_bucket.append(float(np.dot(r["w"][ok2] / r["w"][ok2].sum(), pmall[ok2]))
                          if ok2.any() else np.nan)
        cv = day_cells(r)
        raw_pick.append(float(cv.mean()))
        dec_shift.append(r["w"])

    a = np.array(uni_gap) * 1e4
    b = np.array(w_gap) * 1e4
    cc = np.array(act_bucket) * 1e4
    pk = np.array(raw_pick) * 1e4
    m = np.isfinite(a) & np.isfinite(b) & np.isfinite(cc) & np.isfinite(pk)
    a, b, cc, pk = a[m], b[m], cc[m], pk[m]
    used = [r for r, keep in zip(used, m) if keep]
    n_day = a.size

    # ---- 安慰剂 P-name：当日缺口池随机抽同样只数 ----
    counts = np.array([day_cells(r).size for r in used])
    pname = np.empty(N_REP)
    for k in range(N_REP):
        v = []
        for j, r in enumerate(used):
            fv = I1[r["i0"]][r["fpool"]]
            fv = fv[np.isfinite(fv)]
            if fv.size == 0:
                continue
            take = fv[rng.integers(0, fv.size, size=min(counts[j], fv.size))]
            v.append(float(take.mean()))
        pname[k] = float(np.mean(v)) * 1e4

    # ---- 安慰剂 P-date：保留当日缺口过滤，名单换成另一随机交易日的 mom20 ----
    pdate = np.empty(N_REP)
    pdate_n = np.empty(N_REP)
    for k in range(N_REP):
        v, nd = [], 0
        for r in used:
            other = used[int(rng.integers(0, len(used)))]
            sel2 = other["sel"]
            c0 = c[r["i0"]][sel2]
            g2 = sel2[tradable[r["i0"] + 1][sel2] &
                      (c0 > 0) &
                      (o[r["i0"] + 1][sel2] <= c0 * (1.0 - abs(GAP_MAX)))]
            if g2.size < 3:
                continue
            cv = I1[r["i0"]][g2]
            cv = cv[np.isfinite(cv)]
            if cv.size == 0:
                continue
            v.append(float(cv.mean()))
            nd += 1
        pdate[k] = (float(np.mean(v)) * 1e4) if v else np.nan
        pdate_n[k] = nd

    # ---- 选股溢价的配对检验：d_d = 当日实选 - 当日缺口池等权（同一批日子，日效应相消）----
    diff = pk - a
    diff = diff[np.isfinite(diff)]
    n_d = diff.size
    sdd = float(diff.std(ddof=1))
    nb = int(math.ceil(n_d / 20))
    bs = rng.integers(0, n_d - 20 + 1, size=(2000, nb))
    bidx = (bs[:, :, None] + np.arange(20)[None, None, :]).reshape(2000, -1)[:, :n_d]
    dm = diff[bidx].mean(axis=1)

    # ---- 集中度 ----
    order = np.argsort(-pk)
    top10 = int(round(n_day * 0.10))
    share_top10 = float(pk[order[:top10]].sum() / pk.sum())
    qtr = {}
    for v, r in zip(pk, used):
        q = str(r["dsel"])[:7]
        qtr.setdefault(q, []).append(float(v))
    qtr_mean = {k: round(float(np.mean(v)), 1) for k, v in sorted(qtr.items())}
    tot = sum(float(np.sum(v)) for v in qtr.values())
    qtr_share = {k: round(float(np.sum(v)) / tot, 3) for k, v in sorted(qtr.items())}

    wmean = np.mean(np.array(dec_shift), axis=0)
    out = {
        "seed": SEED,
        "n_days": int(n_day),
        "definitions": {
            "gap_pool_unweighted_bp": round(float(a.mean()), 1),
            "gap_pool_pick_decile_weighted_bp": round(float(b.mean()), 1),
            "full_pool_decile_matched_bp": round(float(cc.mean()), 1),
            "picks_raw_bp": round(float(pk.mean()), 1),
        },
        "accounting": {
            "note": "annualize 的 bench 是 b；未加权的缺口效应是 a;"
                    "『缺口反转 47.2』= b - uni(9.7)，其中缺口本身是 a - uni",
            "gap_effect_unweighted_bp": round(float(a.mean()) - 9.7, 1),
            "gap_effect_weighted_bp": round(float(b.mean()) - 9.7, 1),
            "decile_composition_bp": round(float(b.mean()) - float(a.mean()), 1),
            "bucket_active_bp": round(float(cc.mean()), 1),
        },
        "selection_premium_paired": {
            "note": "d_d = 当日实选均值 - 当日缺口池等权均值；同一批日子，市场日效应相消",
            "mean_bp": round(float(diff.mean()), 1),
            "t_iid": round(float(diff.mean() / (sdd / math.sqrt(n_d))), 2),
            "ci95_block20_bp": [round(float(np.percentile(dm, 2.5)), 1),
                                round(float(np.percentile(dm, 97.5)), 1)],
            "share_days_positive": round(float((diff > 0).mean()), 3),
        },
        "placebo_pname": {
            "mean_bp": round(float(pname.mean()), 1),
            "ci95_bp": [round(float(np.percentile(pname, 2.5)), 1),
                        round(float(np.percentile(pname, 97.5)), 1)],
            "share_ge_80": round(float((pname >= 80).mean()), 3),
            "share_ge_121": round(float((pname >= pk.mean()).mean()), 3),
        },
        "placebo_pdate": {
            "mean_bp": round(float(np.nanmean(pdate)), 1),
            "ci95_bp": [round(float(np.nanpercentile(pdate, 2.5)), 1),
                        round(float(np.nanpercentile(pdate, 97.5)), 1)],
            "days_kept_median": float(np.median(pdate_n)),
            "share_ge_80": round(float((pdate >= 80).mean()), 3),
            "share_ge_121": round(float((pdate >= pk.mean()).mean()), 3),
        },
        "concentration": {
            "top10pct_days_share_of_sum": round(share_top10, 3),
            "quarter_mean_bp": qtr_mean,
            "quarter_share_of_sum": qtr_share,
        },
        "pick_decile_weights": [round(float(x), 4) for x in wmean],
        "elapsed_s": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"days {n_day}")
    for k, v in out["definitions"].items():
        print(f"  {k:34s} {v}")
    print("accounting:")
    for k, v in out["accounting"].items():
        print(f"  {k:26s} {v}")
    sp = out["selection_premium_paired"]
    print(f"paired  mean {sp['mean_bp']}bp t {sp['t_iid']} CI {sp['ci95_block20_bp']} "
          f"pos_days {sp['share_days_positive']}")
    print(f"P-name  mean {out['placebo_pname']['mean_bp']}bp "
          f"CI {out['placebo_pname']['ci95_bp']} >=80 {out['placebo_pname']['share_ge_80']}")
    print(f"P-date  mean {out['placebo_pdate']['mean_bp']}bp "
          f"CI {out['placebo_pdate']['ci95_bp']} >=80 {out['placebo_pdate']['share_ge_80']} "
          f"days {out['placebo_pdate']['days_kept_median']}")
    print(f"top10% days share {share_top10:.3f}")
    print(f"quarters {qtr_mean}")
    print(f"elapsed {out['elapsed_s']}s")


if __name__ == "__main__":
    main()
