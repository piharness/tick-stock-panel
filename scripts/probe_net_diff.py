"""净收益口径：把「胜率 pp」换成「每笔 bp」，再问样本够不够。

§9.28 的 15.9–25.7pp 是**窗口 vs 全历史**的 MDE，回答「近端有没有增量」。
它不是经济问题，也不能和成本比较：pp 是胜率的单位，成本是成交额的 bp。
本脚本用 `probe_window.py` 写出的连续 `exc`（对基准中心的 20 日超额收益）
直接算经济口径：

  1. **水平**：每笔 20 日超额收益的均值（bp），对 0 和成本做单侧检验；
  2. **配对**：同日两个策略的每笔超额差（bp），共同行情被消掉；
  3. **分辨率**：两种口径的 MDE(80%)，与 19bp 的往返成本并排；
  4. **位移**：同一批 picks 换中心后差多少（必须是一个常数）；
  5. **块长**：同一条判定在 B=20/40/60/80 上重算，报告 200bp 那道门是不是
     块长的函数。**只报告，不改判定**——§4 固定 B=40，本节扫的是它周围。
  6. **确认段预算**：把同一台设备放到 250 天窗上跑，与 `se∝n^-1/2` 外推并排。
     两者之差不是噪声，是估计器在 `b/n=0.16` 处的自偏置（§6.3）。

## 基准中心（v5）

`--basis` 选中心，与 `probe_window.py` 的文件名一一对应：

    median  全样本中位数（v1–v4 口径）。**不是**零假设中心：随机抽 20 只的
            期望是均值，减中位数会给每个多头组合 +195.7bp 的常数。
    mean    全样本等权均值。唯一满足 E[exc | 无能力] = 0 的中心。
    mcap    流通市值加权均值。随机组合相对它带规模溢价。

成本假设（`backend/app/services/backtest.py` 的默认值 + 现行税率）：

    佣金 2bp/边 × 2 + 滑点 5bp/边 × 2 + 卖出印花 5bp = 19bp/往返

这个数才是「最小有意义抬升」的锚：净收益要为正，毛超额必须先越过它。
它**不是**从 0.001 Brier 开方来的 3.2pp——那个数没有认领人。

⚠️ 两条边界：

- `exc` 是 20 日**重叠**收益，逐日序列的有效样本约 n/20 个块。HAC(lag=40)
  在长记忆下会偏窄，所以判定用 40 天块重抽的 5% 分位，HAC 只作对照。
- 四个策略的股票池不同，配对差主要是「换了股票」，不是方法优劣；配对表
  的用途是把差分的 bp 分辨率钉住，不是排名。
- 缓存里一旦出现确认段决策日（≥ 2026-10-09），本脚本**拒绝运行**：判定读的是
  缓存，`--nmax` 只决定打印几行，不决定算过什么。唯一出口是
  `--include-confirmation`，它会往 `docs/decision-read-log.jsonl` 记账。
  见 `decision_gate.py` 与预注册 §5。

用法：
    python scripts/probe_net_diff.py [--nmax=325] [--basis=median|mean|mcap]
                                     [--include-confirmation]
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import math
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HITS = ROOT / "data" / ".probe_per_day.json"
#: `--basis` 选基准中心；文件名与 `probe_window.py` 一一对应（median 是旧名）。
BASIS = next((a.split("=", 1)[1] for a in sys.argv[1:]
              if a.startswith("--basis=")), "median")
if BASIS not in ("median", "mean", "mcap"):
    raise SystemExit(f"--basis 必须是 median/mean/mcap，收到 {BASIS!r}")
_CACHE = {b: ROOT / "data" / (".probe_per_day_exc.json" if b == "median"
                              else f".probe_per_day_exc_{b}.json")
          for b in ("median", "mean", "mcap")}
EXC = _CACHE[BASIS]

#: 往返成本，bp。改这里必须同时改 `docs/decision-target-prereg.md`。
COST_BP = 19.0
#: §4 的分辨率门，bp。**只作用于「区间跨过成本线」这一格**：方向已经用
#: 区间定下来之后，再要求正方向的 MDE 就是范畴错误（预注册 §4.1）。
MDE_GATE = 200.0
W, BLOCK, REPS, SEED = 60, 40, 1000, 0
#: 块长扫描的取值。`BLOCK=40` 是**选定的**（20 日重叠收益的记忆约等于持有期，
#: 块长取其两倍），不是估出来的。扫描回答的是另一个问题：200bp 那道门的位置
#: 会不会随块长动——会动，就说明 §4 的判定是块长的函数而不是数据的功能。
BLOCK_SCAN = (20, 40, 60, 80)
#: 循环块重抽至少要这么多块，**块长扫描表**里的那一列才够格被引用。n=326 时
#: B≤40 满足（17/9 块），B=60/80 只剩 6/5 块，`b/n` 到 0.25——那里 se 被低估、
#: 区间比标称窄，判定不能引用。
#:
#: **它管的是「在 B=20/40/60/80 之间挑一列」，不管确认段（v11 更正）。** 两个问题
#: 的失败模式不同：扫描时是「这一列能不能和别的列比」；确认段只有 `n=250`、`B=40`
#: 一格，要问的是「这台设备在这个长度上还准不准」，而那由 cohort 直接量（§6.4
#: 表二：0.88–0.99）。把 8 借过去否决确认段，等于用 A 问题的经验常数回答 B 问题。
#: 并且 `n=326` 的四列 nb 是 17/9/6/5——**没有一列等于 7**，所以这个常数的取值在
#: 现有任何表上都是 0 效果，只在 `n=250` 上留下一行警告。改它是措辞，不是定标。
MIN_BLOCKS = 8
#: 确认段的 cohort 定标：`probe_cohort_check.py` 表二，`n=250`、`B=40` 的四档
#: 块重抽 se / cohort se 中位数区间。0.88 是 low（最窄），0.99 是 high。
#: 它替代 `MIN_BLOCKS` 做确认段的引用口径：区间宽度上界乘 `1/0.88 = 1.14`，
#: 意思是「MDE 与 200bp 的距离小于 14% 时，FAIL 与 UNDERPOWERED 不可区分」。
#: 这是**带**，不是改正系数——偏窄是策略相关的（0.88–0.99），不是一个常数。
CONFIRM_CALIB = (0.88, 0.99)
#: v13b（§12.10 预注册，先于实现钉死）：确认段跨线格的等效门，**单点定义**。
#: 测量 MDE ≤ 176 ⟺ cohort 改正后的 MDE ≤ 200（0.88 是 n=250/B=40 定标带的
#: 下端，最坏情况收紧）。只允许进入 §4 确认段判定（`gate=CONFIRM_GATE`）；
#: 探索段诊断（n=326 主表、扫描、预算、校准、cohort）保持默认门 200——
#: 0.88 是 n=250 的定标量，用到 326 上是错误方向的保守（§6.4：比值 0.99–1.01）。
CONFIRM_GATE = MDE_GATE * CONFIRM_CALIB[0]
#: 确认段的样本量（§4/§5：250 天，2026-10-09 起）。本节只做预算，
#: 不读确认数据——滚窗用的是探索段内部的历史窗，产物里仍没有确认段决策日。
CONFIRM_N = 250
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ppd = _load("probe_paired_diff", "scripts/probe_paired_diff.py")
se_hac = ppd.se_hac
p_two_sided = ppd.p_two_sided
#: 确认段闸门。判定只能经过它，不能另开一条读缓存的路径。
GATE = _load("decision_gate", "scripts/decision_gate.py")


def block_dist(x: list[float], block: int = BLOCK, reps: int = REPS,
               seed: int = SEED) -> list[float]:
    """循环块重抽下**均值**的分布。返回整个分布而不是一个 sd：
    5% 分位比 `mean − 1.645·se` 更诚实，均值分布本身是偏的。"""
    n = len(x)
    rng = random.Random(seed)
    nb = math.ceil(n / block)
    out = []
    for _ in range(reps):
        idx: list[int] = []
        for _ in range(nb):
            s = rng.randrange(n)
            idx.extend((s + k) % n for k in range(block))
        z = [x[i] for i in idx[:n]]
        out.append(sum(z) / n)
    return out


def q(vals: list[float], p: float) -> float:
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[i]


def p_one(t: float) -> float:
    """单侧 p。用 |t|：负向结果不该返回 0.998。"""
    return 0.5 * math.erfc(abs(t) / math.sqrt(2))


def classify(net_lo: float, net_hi: float, mde: float,
             gate: float = MDE_GATE) -> str:
    """§4 的判定表。**四格必须互斥且穷尽**——v5 的表不是。

    v5 的写法是「net_lo>0 即 ECON+，否则 MDE>200 即 UNRESOLVED」，于是
    「区间整段在成本线下方」这一格无处可去：high_turnover 净 LCB −819bp、
    p=0.000，仍被判成 UNRESOLVED，和一个分辨率不足的零结果同名。这是分区
    有洞，不是措辞问题。

    正解是先把**方向**用区间定下来（两侧都是 95% 陈述），再对剩下的
    「跨线」一格问**分辨率**。MDE 的定义是「能检出的最小**正**效应」，
    拿它去判负向主张是范畴错误，所以门槛只挂在跨线这一格上。
    """
    if net_lo > 0:
        return "ECON+"        # 整段在成本线上方：正向主张成立（待 Holm）
    if net_hi < 0:
        return "ECON−"        # 整段在成本线下方：负向主张成立（待 Holm）
    return "FAIL" if mde <= gate else "UNDERPOWERED"


def evaluate_series(x: list[float], reps: int = REPS, block: int = BLOCK,
                    seed: int = SEED, gate: float = MDE_GATE) -> dict:
    """§4 的判定核心：均值、块重抽 5% 分位、MDE、单侧 p、判定。

    抽出来是为了让**安慰剂走同一条代码路径**。安慰剂若用另一套判定，
    就只是在复述预期，不是证据。

    `gate` 是跨线格的分辨率门，默认裸门 `MDE_GATE`：五条探索路径
    （主表、扫描、预算、校准、cohort）不得传别的值——§12.10 的边界是
    探索段诊断逐位不变。确认段执行入口（当它被写出时）必须传
    `gate=CONFIRM_GATE`，且这是读取确认数据的唯一合法判定路径。
    """
    mu = sum(x) / len(x)
    dist = block_dist(x, block=block, reps=reps, seed=seed)
    sd_b = statistics.stdev(dist)
    lo, hi = q(dist, 0.05), q(dist, 0.95)
    net_lo, net_hi = lo - COST_BP, hi - COST_BP
    mde = 2.486 * sd_b
    verdict = classify(net_lo, net_hi, mde, gate=gate)
    # 方向性 p：正向主张的零假设是「净收益 ≤ 0」，负向反之。
    # 用重抽分布直接数，不用正态近似——这个分布是偏的。
    p = (sum(1 for v in dist if v <= COST_BP) if mu > COST_BP
         else sum(1 for v in dist if v >= COST_BP)) / len(dist)
    return {"mu": mu, "sd_b": sd_b, "lo": lo, "hi": hi, "mde": mde,
            "net_lo": net_lo, "net_hi": net_hi, "verdict": verdict, "p": p}


def decide(verdict: str, holm_p: float, alpha: float = 0.05) -> str:
    """把「经济方向」乘上「多重比较」得到 §4 的最终标签。

    方向由区间给出，显著性由 Holm 给出。两者分开报，是因为它们会分叉：
    区间整段在成本线上方但 Holm 没过，既不是 PASS 也不是 FAIL。
    """
    if verdict == "ECON+":
        return "PASS" if holm_p < alpha else "ECON+ n.s."
    if verdict == "ECON−":
        return "EXCLUDED" if holm_p < alpha else "ECON− n.s."
    return verdict


def holm(ps: list[float]) -> list[float]:
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj = [0.0] * len(ps)
    prev = 0.0
    for k, i in enumerate(order):
        prev = max(prev, min(1.0, ps[i] * (len(ps) - k)))
        adj[i] = prev
    return adj


def main() -> None:
    if not EXC.exists():
        raise SystemExit(f"缺少 {EXC}；先跑 `python scripts/probe_window.py "
                         f"--rebuild --basis={BASIS}`")
    hits = json.loads(HITS.read_text(encoding="utf-8"))
    exc = json.loads(EXC.read_text(encoding="utf-8"))
    days = sorted(set.intersection(*(set(v) for v in exc.values())))
    if NMAX:
        days = days[:NMAX]
    GATE.require_exploration(days, "probe_net_diff", sys.argv,
                             allow="--include-confirmation" in sys.argv,
                             artifacts=[EXC])
    n = len(days)
    m = len(next(iter(exc.values()))[days[0]])
    print(f"样本 {n} 个决策日（{days[0]} … {days[-1]}），{m} picks/日，H=20")
    print(f"成本 {COST_BP:.0f}bp/往返 = 佣金 2bp×2 + 滑点 5bp×2 + 卖出印花 5bp")
    print(f"基准中心 {BASIS}"
          + ("（随机组合的期望值，零假设中心）" if BASIS == "mean" else
             "（**不是**零假设中心，见 §9.33）" if BASIS == "median" else
             "（市值加权，规模溢价会进超额）"))
    print("判定 §4.1：方向由块重抽 5%/95% 分位定，分辨率门只作用于跨线格；"
          "PASS/EXCLUDED 另需 Holm<0.05\n")

    ser: dict[str, list[float]] = {}
    rows = []
    for name, per_day in exc.items():
        x = [sum(per_day[d]) / len(per_day[d]) * 1e4 for d in days]
        ser[name] = x
        r = evaluate_series(x)
        med = statistics.median(x)
        xs = sorted(x)
        trim = sum(xs[16:-16]) / (n - 32) if n > 40 else r["mu"]
        drop5 = sum(xs[:-5]) / (n - 5)
        rows.append((name, r["mu"], med, trim, drop5, se_hac(x), r["sd_b"],
                     r["mde"], r["net_lo"], r["verdict"], r["p"]))

    # Holm（4 个策略）
    adj = holm([r[-1] for r in rows])

    head = (f"{'策略':<15}{'均值':>8}{'中位':>8}{'截尾':>8}{'去最好5':>9}"
            f"{'se_HAC':>8}{'se_块':>7}{'MDE_块':>8}{'净LCB':>8}{'MDE/成本':>9}"
            f"{'p':>7}{'Holm':>7}  判定")
    print("一、策略级：每笔 20 日超额收益（bp）")
    print(head)
    for i, (name, mu, med, trim, drop5, sd_h, sd_b, mde, net_lo,
            verdict, p) in enumerate(rows):
        print(f"{name:<15}{mu:>8.1f}{med:>8.1f}{trim:>8.1f}{drop5:>9.1f}"
              f"{sd_h:>8.1f}{sd_b:>7.1f}{mde:>8.1f}{net_lo:>8.1f}"
              f"{mde / COST_BP:>9.1f}{p:>7.3f}{adj[i]:>7.3f}"
              f"  {decide(verdict, adj[i])}")

    print("\n二、配对差：同日两策略的每笔超额差（bp）")
    print(f"{'对照':<28}{'Δ':>8}{'se_HAC':>8}{'se_块':>8}{'t_块':>7}"
          f"{'MDE_块':>8}{'MDE/成本':>9}{'p':>8}")
    ps = []
    for a, b in itertools.combinations(ser, 2):
        d = [x - y for x, y in zip(ser[a], ser[b])]
        mu = sum(d) / n
        sd_h = se_hac(d)
        dist = block_dist(d)
        sd_b = statistics.stdev(dist)
        t = mu / sd_b
        ps.append(p_two_sided(t))
        print(f"{a[:13] + '−' + b[:13]:<28}{mu:>8.1f}{sd_h:>8.1f}{sd_b:>8.1f}"
              f"{t:>7.2f}{2.486 * sd_b:>8.1f}{2.486 * sd_b / COST_BP:>9.1f}"
              f"{p_two_sided(t):>8.4f}")
    print(f"\n名义 p<0.05：{sum(p < 0.05 for p in ps)}/6；"
          f"Bonferroni p<0.0083：{sum(p < 0.05 / 6 for p in ps)}/6")

    print("\n三、和 §9.28 的胜率口径对照")
    print("15.9–25.7pp 是「窗口 vs 全历史」的 MDE，分母是两个估计量之差；")
    print("本表是「水平 vs 0」的 MDE，分母是均值本身。两者不能互相换算，")
    print("也不能拿 3.2pp（0.001 Brier 的开方）当成本：成本是 19bp，")
    print("它对应的是「毛超额要越过多少」，与胜率 pp 无关。")

    shift_table(days)
    block_table(ser)
    budget_table(ser)


def scan_blocks(ser: dict[str, list[float]], blocks=BLOCK_SCAN,
                reps: int = REPS, seed: int = SEED) -> list[dict]:
    """同一批日序列、只换块长：均值不变，分位与 MDE 变。

    抽成函数是为了让它可被测试重算，而不是只存在于打印里。每格都走
    `evaluate_series()`，与 §4 的判定同一条代码路径——扫描若自带一套判定，
    就只是在复述预期。
    """
    out = []
    for name, x in ser.items():
        out.append({"strategy": name, "cells": [
            {"block": b, **evaluate_series(x, reps=reps, block=b, seed=seed)}
            for b in blocks]})
    return out


def scan_summary(rows: list[dict], n: int, gate: float = MDE_GATE) -> list[str]:
    """把扫描读成三句话：可读列里谁恒定、谁在门附近翻转、哪些列不能读。

    **只统计 nb≥MIN_BLOCKS 的列**。把不可读的列也算进「翻转」，等于用估计器
    的偏差去描述数据——那正是这张表要防的读法。
    """
    steady, flips, split = [], [], []
    for r in rows:
        cells = [c for c in r["cells"] if -(-n // c["block"]) >= MIN_BLOCKS]
        vs = [c["verdict"] for c in cells]
        if len(set(vs)) == 1:
            steady.append(f"{r['strategy']}（{vs[0]}）")
        else:
            flips.append(f"{r['strategy']}（" + "→".join(
                f"B={c['block']}:{c['verdict']}" for c in cells) + "）")
        mdes = [c["mde"] for c in cells]
        if min(mdes) <= gate < max(mdes):
            split.append(f"{r['strategy']}（{min(mdes):.0f}…{max(mdes):.0f}）")
    out = ["可读列（nb≥%d）里判定恒定：%s" % (MIN_BLOCKS,
                                              "、".join(steady) or "无"),
           "可读列里判定翻转：" + ("、".join(flips) or "无")]
    if split:
        out.append(f"200bp 门被块长切开：{'、'.join(split)}——那两档的判定是参数选择，不是数据")
    bad = [c["block"] for r in rows for c in r["cells"]
           if -(-n // c["block"]) < MIN_BLOCKS]
    if bad:
        out.append(f"！列 nb<{MIN_BLOCKS}（{sorted(set(bad))}）越过 b/n≤0.2 的适用边界，"
                   "se 不可信（合成序列实测低估 1/3，见 test_large_blocks_underestimate）："
                   "这两列的判定不引用")
    return out


def block_table(ser: dict[str, list[float]], blocks=BLOCK_SCAN) -> None:
    """块长敏感性：**只报告**，§4 的判定仍固定 B=40。

    读法不是「MDE 随块长单调上升」——实测不是。n=326 时 B=80 只剩 5 个块，
    `b/n=0.25` 已经越过循环块重抽的适用边界：重抽均值贴着全样本均值，se 塌下去
    （reversal 84.7→56.9），而 high 因为带趋势反而升到 155.3。**同一列上方向都
    不一致**，这本身就是不可读的证据——于是长块给出更窄的区间、更多 ECON−，
    与「加长块更保守」的直觉相反。这张表要暴露的正是这件事：判定对 B 的依赖，
    有一半来自估计器而不是数据。
    """
    n = len(next(iter(ser.values())))
    rows = scan_blocks(ser, blocks=blocks)
    print(f"\n五、块长敏感性（§4 判定固定 B={BLOCK}；本表只报告，不改判定）")
    print(f"{'策略':<15}" + "".join(
        f"{'B=%d(nb=%d)%s' % (b, -(-n // b), '!' if -(-n // b) < MIN_BLOCKS else ''):>19}"
        for b in blocks))
    for r in rows:
        print(f"{r['strategy']:<15}" + "".join(
            f"{c['mde']:>9.1f} {c['verdict']:<9}" for c in r["cells"]))
    print()
    for line in scan_summary(rows, n):
        print(line)


def rolling_se(x: list[float], block: int = BLOCK, window: int = CONFIRM_N,
               step: int = 10, reps: int = REPS,
               seed: int = SEED) -> list[float]:
    """把**同一台设备**放到 `window` 天的历史窗上跑，量它各段会报多少 se。

    为什么不在 326 天的 se 上乘 `sqrt(326/250)`：n 掉到 250、B=40 时块数从
    9 掉到 7，`b/n` 从 0.12 升到 0.16，§6.2 已经证明偏置随 `b/n` 走。外推
    假设「偏置不变」，滚窗不假设——它直接量。
    """
    return [statistics.stdev(block_dist(x[s:s + window], block=block,
                                       reps=reps, seed=seed))
            for s in range(0, len(x) - window + 1, step)]


def budget_row(x: list[float], block: int = BLOCK, target: int = CONFIRM_N,
               reps: int = REPS, seed: int = SEED) -> dict:
    """一档策略在确认段的三种读数：现样本、开方外推、250 天滚窗实测。

    `mde_proj` 是 §6.1 现在用的约定；`mde_meas` 是同一台设备在 250 天上
    真正会打印的数。两者都不是「真值」：前者假设偏置不变，后者把这个偏置
    一起量了进去。并排报，是因为它们回答的是两个问题——「能分辨多少」与
    「会打印多少」。
    """
    r = evaluate_series(x, block=block, reps=reps, seed=seed)
    se_meas = statistics.median(rolling_se(x, block=block, window=target,
                                           reps=reps, seed=seed))
    return {"n": len(x), "mu": r["mu"], "se": r["sd_b"], "mde": r["mde"],
            "mde_proj": r["mde"] * math.sqrt(len(x) / target),
            "se_meas": se_meas, "mde_meas": 2.486 * se_meas,
            "nb": -(-target // block)}


def confirm_width_bound() -> float:
    """确认段区间宽度的上界倍数：`1 / 0.88 = 1.14`。

    取四档里最窄的那一档（low），所以它是**保守**的：high 的比值是 0.99，
    乘以 1.14 会高估它的宽度。宁可高估，是因为这个数的用途只有一个——
    判断「打印出来的 MDE 离 200bp 够不够远」，而那不是一张需要精确的表。
    """
    return 1.0 / CONFIRM_CALIB[0]


def gate_tag(r: dict, bound: float | None = None) -> str:
    """「这一档离 200bp 有多远」——带上 cohort 的宽度带，不只比两个点。

    带宽是**单边**的：块重抽的 se 只可能偏窄（§6.4 表二，比值 0.88–0.99），
    所以真值只会在打印值的上方，下界直接用打印值、上界乘 `1/0.88`。
    往两边对称地撑开会把「已知方向的偏窄」写成「不知道方向的不确定」。
    """
    b = confirm_width_bound() if bound is None else bound
    lo = min(r["mde_proj"], r["mde_meas"])
    hi = max(r["mde_proj"], r["mde_meas"]) * b
    if hi <= MDE_GATE:
        return "门内"
    if lo > MDE_GATE:
        return "门外"
    return "刀锋"


def budget_table(ser: dict[str, list[float]], target: int = CONFIRM_N) -> None:
    """确认段 250 天的分辨率：外推与实测差多少，差在哪。

    §6.1 的 249 / 306 是 **n=325 + 开方外推** 的单一约定。这里把约定本身
    变成一行读数：`实测/外推` 不是误差项，是估计器在更小的 n 上偏得更狠的
    量。若两者加上宽度带之后跨过 200bp，那一档在确认段就是不可判定的——
    与数据无关。
    """
    n = len(next(iter(ser.values())))
    rows = {k: budget_row(x, target=target) for k, x in ser.items()}
    print(f"\n六、确认段 {target} 天的分辨率（§4 判定仍 B={BLOCK}；只报告）")
    print(f"{'策略':<15}{'MDE@%d' % n:>9}{'外推@%d' % target:>11}"
          f"{'实测@%d' % target:>11}{'实测/外推':>10}{'nb':>4}{'门':>8}")
    split = []
    for k, r in rows.items():
        gap = r["mde_meas"] / r["mde_proj"] - 1
        tag = gate_tag(r)
        if tag == "刀锋":
            split.append(k)
        print(f"{k:<15}{r['mde']:>9.1f}{r['mde_proj']:>11.1f}"
              f"{r['mde_meas']:>11.1f}{gap:>+9.1%}{r['nb']:>4}{tag:>8}")
    gaps = [r["mde_meas"] / r["mde_proj"] - 1 for r in rows.values()]
    print(f"\n实测/外推 {min(gaps):+.0%} … {max(gaps):+.0%}：**不是常数**，"
          "但也不是一个可改正的偏置——5～7 个块上，se 自身的跨样本 sd 就有"
          "18–20%（§6.3 的合成标定），这个量级就是读数误差本身。")
    print(f"\n确认段 nb={-(-target // BLOCK)}（§6.3 记过这个数）。v11 起 `MIN_BLOCKS` "
          "只管块长扫描表，不再否决这一格：")
    print(f"定标改由 cohort 给（§6.4 表二，B={BLOCK} 的块 se 是 cohort 的 "
          f"{CONFIRM_CALIB[0]:.2f}–{CONFIRM_CALIB[1]:.2f}），所以区间宽度的上界乘 "
          f"1/{CONFIRM_CALIB[0]:.2f}={confirm_width_bound():.2f}——")
    print(f"MDE 与 {MDE_GATE:.0f}bp 的距离小于 {confirm_width_bound() - 1:.0%} 时，"
          "FAIL 与 UNDERPOWERED 不可区分；其余照 §4 出标签。")
    if split:
        print(f"！{'、'.join(split)} 的宽度带跨过 200bp 两侧："
              "那一档在确认段不可判定，与数据无关。")
    else:
        print(f"加上 {confirm_width_bound():.2f} 的宽度带之后，四档仍在 200bp 的同侧："
              "判定不依赖选哪种算法。")


def shift_table(days: list[str]) -> None:
    """同一批 picks、只换中心：位移必须是**一个常数**，与策略无关。

    这是 §9.33 的核心读数。若位移随策略变，说明换的不只是中心（例如
    picks 也重排了）；若位移是常数，则中位数口径的整张表只是均值口径的
    平移副本，任何「显著 vs 0」都只是在检验那个常数。
    """
    avail = {b: p for b, p in _CACHE.items() if p.exists()}
    if len(avail) < 2:
        print("\n四、换基准中心的位移：只有一种中心的缓存，跳过"
              "（`probe_window.py --rebuild --basis=...`）")
        return
    data = {b: json.loads(p.read_text(encoding="utf-8")) for b, p in avail.items()}
    names = [k for k in data[BASIS] if all(k in d for d in data.values())]
    common = [d for d in days if all(d in data[b][names[0]] for b in data)]
    print(f"\n四、换基准中心的位移（同一批 picks，{len(common)} 天，bp）")
    print(f"{'策略':<15}" + "".join(f"{b:>9}" for b in avail)
          + f"{'Δ(median−mean)':>16}{'Δ(mean−mcap)':>14}")
    deltas = []
    for name in names:
        mu = {b: sum(sum(data[b][name][d]) / len(data[b][name][d]) * 1e4
                     for d in common) / len(common) for b in avail}
        dm = mu["median"] - mu["mean"] if "median" in mu and "mean" in mu else float("nan")
        dk = mu["mean"] - mu["mcap"] if "mcap" in mu and "mean" in mu else float("nan")
        deltas.append(dm)
        print(f"{name:<15}" + "".join(f"{mu[b]:>9.1f}" for b in avail)
              + f"{dm:>+16.1f}{dk:>+14.1f}")
    ok = [d for d in deltas if d == d]
    if len(ok) > 1:
        spread = max(ok) - min(ok)
        print(f"\n位移的离散度 {spread:.2f}bp（应为 0）："
              + ("✓ 常数位移，中位数表 = 均值表 + 常数"
                 if spread < 0.2 else
                 "✗ 非常数——换中心时 picks 也动了，读数不可比"))


if __name__ == "__main__":
    main()
