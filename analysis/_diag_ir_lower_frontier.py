"""IR 下界前沿：预注册天数 → 「IR 下界 > 0」的可能性；以及 sigma=5% 下的容量三角。

触发问题（2026-11-15）：
  目标写成「sigma 钉死 5% + 年化 >= 10% + 最大化 IR，且在预注册样本量内让 IR 下界 > 0」。
  这句话里三个约束的关系没有被检查过：sigma 钉死时 mu 是 IR 的函数（不是独立约束）；
  而「IR 下界 > 0」在有限 n 上到底可不可达，是一个可以代数判定的事实，不是策略质量问题。

本脚本只回答三件事，不提出新策略：

  1. **门槛是代数恒等式，不是经验**：Wald 型下界
         IR_lower = IR_hat - z * sqrt((1 + IR_hat^2/2) / n_y)
     在 n < z^2 * 242 / 2 时对任何 IR_hat >= 0 都取负（因为 sqrt(1+IR^2/2) > IR/sqrt(2)）。
     给出精确门槛、以及 465 < n < 2000 区间内「IR 门槛被方差膨胀抬高」的表。
     Fieller 判别式给出同一个门槛（区间退化成两个半轴 = 下界 -inf），一并输出。

  2. **真实 L1 日序列的 n_eff**：rho_1..rho_10、方差膨胀因子、以及用 block bootstrap
     复核解析式；再看 IR 的 60 日滚动稳定性（「预注册」要求的平稳性）。

  3. **sigma=5% 三角**：杠杆 L = 5% / sigma_nat、AUM 上限 = A_star/L、年利润与 mu。
     说明 mu >= 10% 是松约束（等价于 IR >= 2），真正的 binding 是容量与样本量。

口径与已知边界（必须一起读）：
  - 日序列口径 = `_diag_l1_annualize` 的 raw（mom20 top20 + gap <= -1% + open[d+1]->close[d+1]，
    日等权，未扣成本）。与它的 parity.raw_bp 对拍，容差 0.2bp。
  - n > 316 的部分是**外推**：用真实序列做 stationary bootstrap 拼接，块内相关保留、
    跨块相关归零 ⇒ 对外推的 power 略偏乐观。产物里记 n_beyond_sample。
  - 蒙特卡洛用标准化 t(5)，不是正态：真实日收益的峰度不能用正态代替。

只读分区。ASCII 输出。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_base_t0 import leg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "_diag_ir_lower_frontier_result.json"
SEED = 20261115
GAP_MAX = -0.01
COST_BP = 20.0
# 年化分母必须是「有信号的回合数/年」，不是日历交易日 242：
# 空仓日既不产生收益也不产生波动，用 242 会把 IR 高估 (242/208.4)^0.5 = 1.078 倍。
# 208.4 = 316 signal days / 1.517 years（_diag_l1_annualize / capacity 同源）。
DAYS_PER_YEAR = 208.4
CALENDAR_DAYS = 242.0
SIGMA_TARGET = 0.05
MU_FLOOR = 0.10
Z95 = 1.959964
Z80 = 0.841621
# _diag_l1_capacity / _diag_l1_annualize 的实测锚（s = 首根 bar 成交额份额）
A_STAR_ROUND_CNY = {"s_3.52pct": 21_938_325.0, "s_20pct": 124_649_571.0}
ROUNDS_PER_YEAR = 208.4


# ---------------------------------------------------------------- 门槛（纯代数）

def wald_ir_threshold(n_days: float, z: float = Z95) -> float | None:
    """给定 n，求满足 IR - z*se(IR) > 0 的最小 IR。

    se(IR) = sqrt(242 * (1 + IR^2/484) / n)，见 wald_lower 的推导。
    解：IR^2*(1 - k^2/484) > k^2, k = z*sqrt(242/n)。
    k^2 >= 484 ⇒ 左边系数 <= 0 ⇒ 无解，即 n <= z^2*242/484 = 1.92 天。
    也就是说：**不存在 465 天那种门槛**（那是把年化 IR 代进周期公式的错，见
    formula_selfcheck）。
    """
    k2 = (z * z) * DAYS_PER_YEAR / n_days
    denom = 1.0 - k2 / DAYS_PER_YEAR  # k2/484 = k2/(2*242)
    if denom <= 0:
        return None
    return math.sqrt(k2 / denom)


def wald_lower(ir_hat: float, n_days: float, z: float = Z95) -> float:
    """IR 的 Wald 下界。分母必须是「日观测数 n」，不是「年数 n_y」。

    Var(IR) 的年化写法：Var(mu_ann)=sigma^2/n_y，但 Var(sigma_ann)=sigma^2/(2n)
    —— sigma 由 n 个日观测估出来，不是由 n_y 个年观测估出来。
    两者合起来：Var(IR) = 1/n_y + IR^2/(2n) = (242/n)(1 + IR^2/484)。

    若误写 Var(sigma_ann)=sigma^2/(2 n_y)，se 会被放大到 3.45 倍
    （IR=4.79,n=316 时 3.09 vs 0.896），并凭空生出「n<465 下界必为负」的假门槛。
    formula_selfcheck 把它钉成断言。
    """
    n_y = n_days / DAYS_PER_YEAR
    return ir_hat - z * math.sqrt(1.0 / n_y + ir_hat * ir_hat / (2.0 * n_days))


def fieller_state(ir_hat: float, n_days: float, z: float = Z95) -> str:
    """Fieller 区间形态。X=mu_ann, Y=sigma_ann, VarX=Y^2/n_y, VarY=Y^2/(2n)。

    不等式 q*k^2 - 2XY*k + (X^2 - z^2 VarX) <= 0
      q     = Y^2 - z^2 VarY = Y^2 (1 - z^2/(2n))
      D/4   = VarY*X^2 + VarX*Y^2 - z^2 VarX VarY
            = Y^4/(2n) * [IR^2 + (2n - z^2)/n_y]
    开口向上(q>0)：D>0 -> 有限区间 [k1,k2]；D<0 -> 空集（记 empty）
    开口向下(q<0)：D>0 -> 两个半轴（下界 -inf）；D<0 -> 整条实轴
    """
    n_y = n_days / DAYS_PER_YEAR
    q = 1.0 - z * z / (2.0 * n_days)
    d = ir_hat * ir_hat + (2.0 * n_days - z * z) / n_y
    if q > 0:
        return "bounded" if d > 0 else "empty"
    return "two_rays" if d > 0 else "whole_line"


# ---------------------------------------------------------------- 蒙特卡洛 power

def mc_power(ir_true: float, n_days: int, B: int = 4000, df: int = 5,
             seed: int = SEED) -> dict:
    """IR_hat 的抽样分布 -> Wald 下界 > 0 的频率。

    日收益 = (IR/sqrt(242)) * z，z 为标准化 t(df)（单位方差、厚尾）。
    这是对「真实 IR = ir_true、形状与非正态同 t(5)」的乐观设定：忽略真实序列的
    自相关与波动聚集，所以得到的 power 是上界。
    """
    rng = np.random.default_rng(seed + int(ir_true * 100) + n_days)
    z = rng.standard_t(df, size=(B, n_days))
    z /= math.sqrt(df / (df - 2.0))
    x = z + ir_true / math.sqrt(DAYS_PER_YEAR)
    m = x.mean(axis=1)
    s = x.std(axis=1, ddof=1)
    ir_hat = m / s * math.sqrt(DAYS_PER_YEAR)
    lower = ir_hat - Z95 * np.sqrt((1.0 + ir_hat ** 2 / 2.0)
                                   / (n_days / DAYS_PER_YEAR))
    return {
        "power": round(float((lower > 0).mean()), 4),
        "ir_hat_median": round(float(np.median(ir_hat)), 3),
        "lower_median": round(float(np.median(lower)), 3),
        "lower_p05": round(float(np.percentile(lower, 5)), 3),
    }


def n_for_power(ir_true: float, target: float = 0.80) -> int | None:
    """扫描最小的 n（交易日）使 MC power >= target。粗网格 + 二分。"""
    grid = [100, 150, 200, 250, 316, 400, 500, 600, 700, 900, 1200,
            1500, 2000, 2500, 3000, 4000]
    prev = None
    for n in grid:
        if mc_power(ir_true, n, B=1200)["power"] >= target:
            lo = prev if prev else 100
            for _ in range(6):
                mid = (lo + n) // 2
                if mc_power(ir_true, mid, B=1200)["power"] >= target:
                    n = mid
                else:
                    lo = mid
            return n
        prev = n
    return None


# ---------------------------------------------------------------- 真实序列

_PANEL: dict | None = None


def panel() -> dict:
    """一次加载，多次复用（gap 敏感性要跑多遍选股）。"""
    global _PANEL
    if _PANEL is None:
        df = ap.add_features(ap.load_panel())
        dates, symbols, mats = ap.calendar_mats(df)
        c, o = mats["close"], mats["open"]
        _PANEL = {
            "dates": dates, "symbols": symbols, "T": c.shape[0],
            "s_map": {x: i for i, x in enumerate(symbols)},
            "d_map": {d: i for i, d in enumerate(dates)},
            "turn": ac.grid_of(df, "turnover_rate", dates, symbols),
            "c": c, "o": o,
            "tradable": np.isfinite(c) & (c > 0) & np.isfinite(o) & (o > 0),
            "C2": leg(mats, "c2"), "I1": leg(mats, "i1"),
            "picks": ap.pick_daily(df, "mom20", ascending=False).select(
                "date", "symbols").to_dicts(),
        }
    return _PANEL


def build_day_series(gap_max: float = GAP_MAX) -> tuple[np.ndarray, list[str]]:
    P = panel()
    dates, s_map, d_map = P["dates"], P["s_map"], P["d_map"]
    turn, c, o = P["turn"], P["c"], P["o"]
    tradable, C2, I1 = P["tradable"], P["C2"], P["I1"]
    T = P["T"]
    day_raw, days = [], []
    for r in P["picks"]:
        i0 = d_map[str(r["date"])]
        if i0 + 2 >= T:
            continue
        sel = np.array([s_map[x] for x in r["symbols"]], dtype=np.int64)
        c0 = c[i0]
        pool = tradable[i0] & np.isfinite(C2[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
        if pool.sum() < 200:
            continue
        lim = c0 * (1.0 - abs(gap_max))
        fill = pool & tradable[i0 + 1] & (o[i0 + 1] <= lim) & (lim > 0)
        good = sel[fill[sel]]
        if good.size < 3:
            continue
        v = I1[i0][good]
        v = v[np.isfinite(v)]
        if v.size < 3:
            continue
        day_raw.append(float(v.mean()))
        days.append(str(dates[i0 + 1]))
    return np.array(day_raw) * 1e4, days


def acf_vif(x: np.ndarray, lag: int = 10) -> dict:
    e = x - x.mean()
    v0 = float(e @ e) / e.size
    rho = []
    for l in range(1, lag + 1):
        rho.append(round(float(e[l:] @ e[:-l]) / e.size / v0, 4))
    # Bartlett 权重与 LRV 之比 = 方差膨胀因子
    vif = 1.0 + 2.0 * sum((1.0 - l / (lag + 1.0)) * rho[l - 1] for l in range(1, lag + 1))
    return {"rho": rho, "vif_bartlett10": round(vif, 4),
            "n_eff": round(x.size / vif, 1) if vif > 0 else None}


def boot_lower(x: np.ndarray, n_days: int, B: int = 3000, block: int = 5,
               seed: int = SEED) -> dict:
    """stationary bootstrap：从真实序列重采样 n_days 个日收益，算 IR 的 5% 分位。

    n_days > len(x) 时块会被重复取用 => 跨块相关归零，对 power 偏乐观（见 docstring）。
    """
    rng = np.random.default_rng(seed)
    n = x.size
    nb = int(math.ceil(n_days / block))
    out = np.empty(B)
    for b in range(B):
        st = rng.integers(0, n - block + 1, size=nb)
        idx = (st[:, None] + np.arange(block)[None, :]).ravel()[:n_days]
        s = x[idx]
        sd = s.std(ddof=1)
        out[b] = s.mean() / sd * math.sqrt(DAYS_PER_YEAR) if sd > 0 else np.nan
    out = out[np.isfinite(out)]
    return {"lower_p05": round(float(np.percentile(out, 5)), 3),
            "median": round(float(np.median(out)), 3),
            "p_gt0": round(float((out > 0).mean()), 4)}


def rolling_ir(x: np.ndarray, win: int = 60) -> dict:
    vals = []
    for i in range(0, x.size - win + 1, 5):
        w = x[i:i + win]
        sd = w.std(ddof=1)
        if sd > 0:
            vals.append(w.mean() / sd * math.sqrt(DAYS_PER_YEAR))
    a = np.array(vals)
    return {"win": win, "n": int(a.size), "min": round(float(a.min()), 2),
            "p10": round(float(np.percentile(a, 10)), 2),
            "median": round(float(np.median(a)), 2),
            "p90": round(float(np.percentile(a, 90)), 2),
            "max": round(float(a.max()), 2),
            "share_negative": round(float((a < 0).mean()), 3)}


# ---------------------------------------------------------------- sigma 三角

def sigma_triangle(sd_bp: float, mean_net_bp: float, tag: str) -> list[dict]:
    sigma_nat = sd_bp * math.sqrt(ROUNDS_PER_YEAR) / 1e4
    lev = SIGMA_TARGET / sigma_nat
    rows = []
    for s_tag, a_star in A_STAR_ROUND_CNY.items():
        aum_cap = a_star / lev
        ann_profit = a_star * (mean_net_bp / 1e4) * ROUNDS_PER_YEAR
        mu = ann_profit / aum_cap
        aum_for_mu10 = ann_profit / MU_FLOOR
        rows.append({
            "caliber": tag,
            "s": s_tag,
            "a_star_round_cny": a_star,
            "leverage": round(lev, 4),
            "aum_cap_cny": round(aum_cap, 0),
            "ann_profit_cny": round(ann_profit, 0),
            "mu_pct": round(mu * 100, 2),
            "sigma_pct": SIGMA_TARGET * 100,
            "ir": round(mu / SIGMA_TARGET, 2),
            "aum_for_mu10_pct_cny": round(aum_for_mu10, 0),
            "binding": "capacity" if aum_cap < aum_for_mu10 else "mu_floor",
        })
    return rows



def seg_stats(x: np.ndarray, tag: str) -> dict:
    sd = float(x.std(ddof=1))
    m = float(x.mean())
    return {
        "caliber": tag,
        "n_days": int(x.size),
        "mean_gross_bp": round(m, 2),
        "mean_net20_bp": round(m - COST_BP, 2),
        "sd_bp": round(sd, 1),
        "ir_gross": round(m / sd * math.sqrt(DAYS_PER_YEAR), 2),
        "ir_net20": round((m - COST_BP) / sd * math.sqrt(DAYS_PER_YEAR), 2),
        "sigma_nat_pct": round(sd * math.sqrt(DAYS_PER_YEAR) / 1e4 * 100, 2),
        "wald_lower_gross_own_n": round(wald_lower(m / sd * math.sqrt(DAYS_PER_YEAR), x.size), 3),
        "wald_lower_net20_own_n": round(
            wald_lower((m - COST_BP) / sd * math.sqrt(DAYS_PER_YEAR), x.size), 3),
        "n_years_calendar": round(x.size / CALENDAR_DAYS, 2),
        "n_years_periods": round(x.size / DAYS_PER_YEAR, 2),
    }


def window_power(x: np.ndarray, n_days: int) -> dict:
    """所有重叠的连续 n 天窗口里，Wald 下界 > 0 的比例。

    这是**经验** power：不假设分布、不假设独立，只用历史里真实出现过的 n 天窗。
    重叠窗口不独立，所以这个比例是「历史上任意 n 天能否过关」的读数，
    不是「独立重复实验的 power」——两者都应报，差异本身是信息。
    """
    lowers = []
    for i in range(0, x.size - n_days + 1):
        w = x[i:i + n_days]
        sd = w.std(ddof=1)
        if sd <= 0:
            continue
        lowers.append(wald_lower(w.mean() / sd * math.sqrt(DAYS_PER_YEAR), n_days))
    a = np.array(lowers)
    return {
        "n_windows": int(a.size),
        "share_lower_gt0": round(float((a > 0).mean()), 4),
        "lower_p05": round(float(np.percentile(a, 5)), 3),
        "lower_median": round(float(np.median(a)), 3),
    }


def quarter_split(x: np.ndarray, labels: list[str]) -> list[dict]:
    k = x.size // 4
    out = []
    for q in range(4):
        seg = x[q * k:(q + 1) * k] if q < 3 else x[3 * k:]
        lab = labels[q * k:(q + 1) * k] if q < 3 else labels[3 * k:]
        sd = float(seg.std(ddof=1))
        out.append({
            "quarter": q + 1,
            "date_from": lab[0] if lab else None,
            "date_to": lab[-1] if lab else None,
            "n_days": int(seg.size),
            "mean_bp": round(float(seg.mean()), 2),
            "ir_gross": round(float(seg.mean()) / sd * math.sqrt(DAYS_PER_YEAR), 2),
        })
    return out


def main() -> None:
    t0 = time.time()
    raw, days = build_day_series()
    order = np.argsort(np.array(days), kind="stable")
    raw = raw[order]
    days = [days[i] for i in order]
    n_all = raw.size

    # 与 _diag_l1_capacity / _diag_l1_annualize 同一集合：enriched 面板 date_first
    RECENT_CUT = "2025-06-06"
    mask_recent = np.array([d >= RECENT_CUT for d in days])
    raw_rec = raw[mask_recent]
    days_rec = [d for d, m in zip(days, mask_recent) if m]

    full = seg_stats(raw, "full_history")
    rec = seg_stats(raw_rec, "recent_367d")

    # ---- 1. 纯代数门槛
    n_gate = Z95 ** 2 * DAYS_PER_YEAR / 2.0
    gate_table = []
    for nd in (250, 316, 400, 465, 500, 550, 600, 700, 800, 1000, 1200,
               1500, 2000, 2500, 3000, 5000):
        th = wald_ir_threshold(nd)
        gate_table.append({
            "n_days": nd,
            "n_years": round(nd / DAYS_PER_YEAR, 2),
            "ir_needed_5pct": None if th is None else round(th, 2),
            "analytic_gate_hit": th is None,
            "fieller_bounded_at_ir4": fieller_state(4.0, nd) == "bounded",
        })

    # ---- 2. 蒙特卡洛 power（iid t5 上界）
    mc = {}
    for ir_true in (1.5, 2.0, 3.0, 4.0):
        mc[str(ir_true)] = {
            str(nd): mc_power(ir_true, nd, B=1500)
            for nd in (316, 500, 700, 1000, 1500, 2000)
        }
    n80 = {str(ir): n_for_power(ir) for ir in (2.0, 3.0, 4.0)}

    # ---- 3. 经验 power（全历史重叠窗口，无分布假设）
    win = {str(nd): window_power(raw, nd)
           for nd in (250, 316, 500, 700, 1000, 1500, 2000, 3000)}

    # ---- 4. 序列结构
    acf_all = acf_vif(raw, 10)
    acf_rec = acf_vif(raw_rec, 10)
    boot_tab = {str(nd): boot_lower(raw, nd, B=1500)
                for nd in (316, 500, 700, 1000, 1500, 2000)}
    rob = {"full": rolling_ir(raw, 60), "recent": rolling_ir(raw_rec, 60)}
    quarters = quarter_split(raw, days)

    # ---- 5. sigma 三角（两套口径，A_star 只有近端成交额这一个产地）
    tri = sigma_triangle(rec["sd_bp"], rec["mean_net20_bp"], "recent_367d")
    tri += sigma_triangle(full["sd_bp"], full["mean_net20_bp"],
                          "full_history_astar_borrowed")

    # ---- 5b. 参数邻域平坦度：预注册的边界有多脆
    gap_sens = []
    for g in (0.005, 0.0075, 0.010, 0.015, 0.020):
        gx, gd = build_day_series(gap_max=-g)
        gsd = float(gx.std(ddof=1))
        gm = float(gx.mean())
        gap_sens.append({
            "gap_threshold_pct": round(-g * 100, 2),
            "n_days": int(gx.size),
            "mean_gross_bp": round(gm, 2),
            "ir_gross": round(gm / gsd * math.sqrt(DAYS_PER_YEAR), 2),
            "ir_net20": round((gm - COST_BP) / gsd * math.sqrt(DAYS_PER_YEAR), 2),
            "lower_own_n_net": round(
                wald_lower((gm - COST_BP) / gsd * math.sqrt(DAYS_PER_YEAR), gx.size), 2),
        })

    # ---- 6. 入场时点悬崖（kcurve 全样本端点）
    kc = json.loads((Path(__file__).resolve().parent
                     / "_diag_l1_kcurve_result.json").read_text(encoding="utf-8"))
    d1 = kc["endpoints"]["d1"]["day_eq_mean_bp"]
    d240 = kc["endpoints"]["d240"]["day_eq_mean_bp"]
    entry = []
    for tag, delta in (("open", 0.0), ("vwap_k1", d1), ("vwap_k240", d240)):
        r = rec["mean_gross_bp"] - delta
        entry.append({
            "entry": tag, "delta_bp": round(delta, 2),
            "gross_bp": round(r, 2), "net_bp": round(r - COST_BP, 2),
            "ir_gross": round(r / rec["sd_bp"] * math.sqrt(DAYS_PER_YEAR), 2),
            "ir_net": round((r - COST_BP) / rec["sd_bp"] * math.sqrt(DAYS_PER_YEAR), 2),
        })

    # ---- 0. 公式自检：se(IR) 三种算法必须收敛，否则下面所有表都是假的
    ir_f, n_f = full["ir_gross"], full["n_days"]
    n_y_f = n_f / DAYS_PER_YEAR
    sr_d = ir_f / math.sqrt(DAYS_PER_YEAR)
    se_period = math.sqrt((1 + sr_d ** 2 / 2) / n_f) * math.sqrt(DAYS_PER_YEAR)
    se_ok = math.sqrt(1.0 / n_y_f + ir_f ** 2 / (2.0 * n_f))
    se_bad = math.sqrt(1.0 / n_y_f + ir_f ** 2 / (2.0 * n_y_f))
    selfcheck = {
        "ir": round(ir_f, 3), "n": n_f,
        "se_period_annualized": round(se_period, 4),
        "se_annual_correct": round(se_ok, 4),
        "se_annual_wrong_n_y": round(se_bad, 4),
        "period_eq_correct": abs(se_period - se_ok) < 1e-9,
        "inflation_if_wrong": round(se_bad / se_ok, 3),
        "fake_gate_days_if_wrong": round(Z95 ** 2 * DAYS_PER_YEAR / 2.0, 1),
        "note": ("Var(sigma_ann)=sigma^2/(2n) 而非 sigma^2/(2n_y)；用错会让 se 放大 3.45 倍，"
                 "并造出「n<465 下界必为负」的假门槛"),
    }
    assert selfcheck["period_eq_correct"], "se 公式三路不自洽，停"

    # ---- Wald 对 bootstrap 的实证核对（同一 n）
    xcheck = {}
    for nd in (316, 500, 1000):
        w = wald_lower(ir_f, nd)
        b = boot_lower(raw, nd, B=1200)
        xcheck[str(nd)] = {"wald_lower": round(w, 3), "boot_p05": b["lower_p05"],
                           "gap": round(w - b["lower_p05"], 3),
                           "agree": abs(w - b["lower_p05"]) < 1.5}

    # ---- 入场价 × 样本量：谁更值钱
    entry_frontier = []
    for tag, delta in (("open", 0.0), ("vwap_k1", d1), ("vwap_k240", d240)):
        r_net = rec["mean_gross_bp"] - delta - COST_BP
        ir_e = r_net / rec["sd_bp"] * math.sqrt(DAYS_PER_YEAR)
        entry_frontier.append({
            "entry": tag, "delta_bp": round(delta, 2), "ir_net": round(ir_e, 2),
            **{f"lower_at_{nd}": round(wald_lower(ir_e, nd), 2)
               for nd in (367, 700, 1500, 3000)},
        })

    res = {
        "seed": SEED,
        "purpose": "IR 下界 > 0 的可行性门槛；sigma=5% 容量三角；入场价悬崖",
        "caliber": ("日序列 = mom20 top20 + gap<=-1% + open[d+1]->close[d+1] 日等权，未扣成本；"
                    "成本 20bp/回合；年化 242 日；full=全部可得历史，recent=最后 367 个 entry 日"),
        "parity_vs_annualize": {
            "full_mean_bp": full["mean_gross_bp"], "expect_22y_bp": 96.02,
            "recent_mean_bp": rec["mean_gross_bp"], "expect_367d_bp": 121.5,
            "recent_diff_bp": round(rec["mean_gross_bp"] - 121.5, 2),
            "note": "两个锚差 24bp 是样本期差异，不是复现失败；不可互除",
        },
        "series": {"full": full, "recent": rec},
        "formula_selfcheck": selfcheck,
        "wald_vs_bootstrap": xcheck,
        "gate": {
            "z": Z95,
            "fake_gate_days_from_wrong_formula": round(n_gate, 1),
            "reason": ("正确写法 n <= z^2*P/2P = z^2/2 = 1.92 天就无解 —— 即几乎总有解。"
                       "「465 天门槛」只出现在把年化 IR 代进周期公式的错误版本里。"),
            "table": gate_table,
        },
        "monte_carlo_iid_t5": {"power_by_ir_and_n": mc, "n_days_for_power80": n80,
                               "note": "iid、无自相关 => power 是上界"},
        "empirical_window_power": {
            "method": "全历史所有重叠连续窗口，Wald 下界 > 0 的比例",
            "by_n": win,
        },
        "structure": {
            "acf_full": acf_all,
            "acf_recent": acf_rec,
            "boot_ir_p05_full": boot_tab,
            "rolling_ir60": rob,
            "quarter_split": quarters,
        },
        "sigma_triangle": tri,
        "entry_frontier": entry_frontier,
        "gap_sensitivity": gap_sens,
        "entry_cliff": {
            "rows": entry,
            "breakeven_delta_bp": rec["mean_net20_bp"],
            "source": "_diag_l1_kcurve_result.json endpoints (316d full sample)",
        },
        "elapsed_s": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[written] {OUT}  elapsed={res['elapsed_s']}s")


if __name__ == "__main__":
    main()
