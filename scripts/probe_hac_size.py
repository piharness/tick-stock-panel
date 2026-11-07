"""`hac_lcb` 的判定错误率：把「结构不变量」换成 size 表。

## 为什么需要这个脚本

`test_decision_pwin.py` 里的验收全是结构不变量（单调、截断、标注一致）。
它们全部成立而判定器仍然可以系统性错：`significance=PASS` 的错误率没有被
任何一条测试覆盖。本脚本直接量这个数：

    H0（真实改善 = 0）下 P(lcb > 0) 应当 ≈ 5%（单侧）

而且分两个口径报：

- `size`：全部重复里 `lcb > 0` 的比例；
- `size|PASS`：只统计 `n_eff >= MIN_NEFF` 的那些重复——**这才是面板真的会
  印出 `PASS` 的那批样本上的错误率**。分母没到门槛时判定是 `UNKNOWN`，
  错不到读者眼前。

## 已知的偏差来源

`hac_lcb` 用 Bartlett 核、带宽恒等于 `lag+1 = H`。Bartlett 权重
`w_k = 1 - k/H` 在 k=H-1 处只剩 `1/H`，而 MA(H-1) 的 `γ_{H-1}` 仍有
`γ_0/(H)` 的量级。对等权 MA(q)（`γ_k = γ_0·(q+1-k)/(q+1)`，正是重叠 H 日
收益的自协方差形状）概率极限是

    LRV_hat → [2/3 + 1/(3h²)]·LRV = 267/400·LRV = 0.6675·LRV   (h = q+1 = 20)

于是 `se` 恒为真值的 `sqrt(267/400) ≈ 0.817` 倍，**不随 n 收敛**；
`n_eff = n·(se_iid/se)²` 被同一份偏差放大 1.5 倍。

> 旧版这里写的是 `(2/3 + 1/(q+1))·LRV`（= 0.717），与两行后的 `sqrt(2/3)`
> 自相矛盾：前者给 0.717，后者给 0.667，而这支核的实际闭式是
> `2/3 + 1/(3h²)`。手推式把尾项当成了 O(1/h)：实际
> `Σ_{k<h}(1−k/h)(h−k) = (h−1)h(2h−1)/6`，除 `h²` 后余项只有
> `1/(3h²)`（h=20 时 1/1200，不是 1/20）。

## 本脚本比对的估计量

1. `bartlett_q`   —— 现状：Bartlett，b = q+1，分母 n
2. `trunc_q`      —— 截断（矩形）核，b = q，分母 n-k：LRV 无偏，但方差大
3. `struct_H`     —— 结构性：重叠 H 日 ⇒ `LRV = H·γ0`，只估 γ0 一个标量
4. `bartlett_n13` —— Bartlett，b ≈ 1.1447·n^(1/3)（随 n 增长，一致）

## floor：两支核的收底不同，这里是第六处同型缺陷的产地

`settle.hac_lcb` 的两个分支 floor 不同：Bartlett 收 `max(lr, 0)`，截断收
`max(lr, γ0)`（重叠 H 日序列上 `LRV ≥ γ0` 是模型内硬下界）。本脚本的
`_evaluate` 曾经对四条估计量统一按 `max(lr, 0)` 收底，于是 `trunc_q` 在
`iid` 列上算的**不是生产行为**：`lr` 有 **58–69%** 的抽样落在 `γ0` 以下——注意
`lr < γ0` 严格弱于 `lr < 0`，`LRV<=0%` 只有 1.0–2.6%，从那一列看不出这件事。
生产会把这些抽样抬回 `γ0`，后果是方向性的：旧读数 `se` 偏低 12%、size
8.4–9.7%，生产实际是 `se` **偏高** 4–6%、size **3.3–4.7%**——结论从「这支核
在 iid 上过度拒绝」翻成「略保守」。

因此本表的两列是诊断而不是行：`floor%` 是 floor 的绑定率，`size@floor0%` 是
同一批抽样上只收 0 的读数。**旧口径留在列里、不进新行**——行会被当作「这支
核的 size」引用，列只被当作诊断读；而 `trunc_q` 这一行必须报生产值，否则它
和 `test_lrv_is_floored_at_gamma0`（生产截断核 `se ≥ se_iid`）当场矛盾。

## 三个 DGP

- `ma`    ：等权 MA(q)，记忆恰为 H-1（代码假定的模型）
- `drift` ：MA(q) + AR(1) ρ=0.995 的慢漂移（记忆**超过** H-1）
- `iid`   ：无重叠（H=1 的退化情形，作为对照）

`drift` 是关键的证伪列：任何固定带宽 b=H 的核在这里都会崩，包括截断核。
「换核」只修好了代码假定成立的那一列。

用法：

    uv run python scripts/probe_hac_size.py

退出码非零 ⇔ **生产截断核**（`trunc_q`）在 `ma` 列上条件 size 超 8%，且该格的
分母 `PASS_n >= 100`。CI 门禁可以只跑 `--reps 500`。

> 旧版这里写的是「有估计量在 ma 列上超过 8%」——那条契约**永远不可能满足**：
> `bad` 把 `bartlett_q` 与 `bartlett_n13` 也算进去了，而这两支是**证伪列**，
> 它们超 8% 正是本表要展示的事实（`test_decision_hac_size.py` 还断言旧核
> size ≥ 7%）。一个恒红的门禁等于没有门禁。判红的对象只能是**会上线的那支**；
> 分母不足的格（n=250/326 的 `PASS_n` 只有 66/83）只印不判，否则判的是噪声。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

Z90 = 1.645
Q = 19          # lag = H-1，H=20
MIN_NEFF = 30
HAC_MIN_RATIO = 3
#: 门禁只判生产截断核，且要求条件 rate 的分母够读。见模块 docstring 的「用法」。
GATED = ("trunc_q",)
MIN_GATE_N = 100


def _ma_equal(n: int, reps: int, q: int, rng: np.random.Generator) -> np.ndarray:
    """等权 MA(q)，Var=1、LRV=q+1（重叠 H 日收益的形状）。"""
    e = rng.standard_normal((reps, n + q))
    c = np.cumsum(e, axis=1)
    out = (c[:, q:] - np.concatenate([np.zeros((reps, 1)), c[:, :-q - 1]], axis=1))
    return out / math.sqrt(q + 1)


def _ma_drift(n: int, reps: int, q: int, rho: float,
              rng: np.random.Generator) -> np.ndarray:
    """MA(q) + AR(1) 慢漂移。H0 下均值仍为 0，但 LRV 由低频主导。"""
    base = _ma_equal(n, reps, q, rng)
    z = np.empty((reps, n))
    z[:, 0] = rng.standard_normal(reps)
    for t in range(1, n):
        z[:, t] = rho * z[:, t - 1] + math.sqrt(1 - rho ** 2) * rng.standard_normal(reps)
    return base + z


def _lrv_bartlett(x: np.ndarray, lag: int) -> np.ndarray:
    """逐行 Bartlett HAC（分母 n，权重 `1−k/(lag+1)`，与 `settle.hac_lcb` 逐位一致）。

    `lag` 必须传**自相关阶数**，不是「带宽」：`settle.hac_lcb(values, lag)` 的
    循环走到 k=lag、权重 `1−k/(lag+1)`。传 `Q+1` 会多算一阶且把权重压成
    `1−k/(lag+2)`，闭式随之从 267/400 漂到 0.683，那一列就不再是生产核。
    """
    reps, n = x.shape
    d = x - x.mean(axis=1, keepdims=True)
    g0 = (d * d).mean(axis=1)
    lr = g0.copy()
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        lr += 2.0 * w * (d[:, :-k] * d[:, k:]).sum(axis=1) / n
    return lr


def _lrv_trunc(x: np.ndarray, b: int) -> np.ndarray:
    """矩形（截断）核：w_k ≡ 1，分母 n-k。LRV 无偏，但不保证非负。"""
    reps, n = x.shape
    d = x - x.mean(axis=1, keepdims=True)
    g0 = (d * d).mean(axis=1)
    lr = g0.copy()
    for k in range(1, min(b, n - 1) + 1):
        gk = (d[:, :-k] * d[:, k:]).sum(axis=1) / (n - k)
        lr += 2.0 * gk
    return lr


def _lrv_struct(x: np.ndarray, h: int) -> np.ndarray:
    """重叠 H 日序列：LRV = H·γ0（形状已知，只估 γ0）。"""
    d = x - x.mean(axis=1, keepdims=True)
    return h * (d * d).mean(axis=1)


def _se(lr: np.ndarray, g0: np.ndarray, n: int, kernel: str) -> np.ndarray:
    """生产 `se` 的唯一映射：`sqrt(max(lr, floor)/n)`，**floor 随核变**。

    `kernel="none"` 给没有生产对应物的估计量（`struct_H`）：它的 LRV 构造上
    ≥ γ0，不存在收底问题。四支估计量里只有这一支走 `none`。
    """
    if kernel == "truncated":
        lr = np.maximum(lr, g0)
    elif kernel == "bartlett":
        lr = np.maximum(lr, 0.0)
    elif kernel != "none":
        raise ValueError(f"kernel 必须是 truncated/bartlett/none，收到 {kernel!r}")
    return np.sqrt(lr / n)


def _neff(n: int, se_iid: np.ndarray, se: np.ndarray) -> np.ndarray:
    """与 `settle.effective_sample` 同一个构造（向量化），`PASS%` 的分母靠它。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(se > 0, se_iid / se, np.inf)
    # n < 3(lag+1) 时退回 n//(lag+1)——那一支不看 se，与生产同构。
    return np.where(n < HAC_MIN_RATIO * (Q + 1), max(1, n // (Q + 1)),
                    np.clip(np.rint(n * ratio ** 2), 1, n))


def _evaluate(name: str, lrv: np.ndarray, x: np.ndarray, lrv_true: float,
              kernel: str) -> dict:
    n = x.shape[1]
    mean = x.mean(axis=1)
    g0 = x.var(axis=1)
    se = _se(lrv, g0, n, kernel)
    se_iid = np.sqrt(g0 / n)
    se0 = np.sqrt(np.maximum(lrv, 0.0) / n)      # 只收 0 的诊断口径（旧读数）
    lcb = mean - Z90 * se
    pass_lcb = lcb > 0
    n_eff = _neff(n, se_iid, se)
    gate = n_eff >= MIN_NEFF
    floor = g0 if kernel == "truncated" else np.zeros_like(g0)
    return {
        "est": name,
        "n": n,
        "reps": int(x.shape[0]),
        "se/true": float(np.mean(se[se > 0]) / math.sqrt(lrv_true / n)),
        "size%": 100 * float(pass_lcb.mean()),
        "floor%": 100 * float((lrv < floor).mean()),
        #: 同一批抽样、floor 只收 0 的读数。等于 `size%` 当 floor 从不绑定。
        "size@floor0%": 100 * float((mean - Z90 * se0 > 0).mean()),
        "PASS%": 100 * float(gate.mean()),
        "PASS_n": int(gate.sum()),
        "size|PASS%": 100 * float(pass_lcb[gate].mean()) if gate.any() else float("nan"),
        "LRV<=0%": 100 * float((lrv <= 0).mean()),
    }


def _assert_matches_settle() -> tuple[float, int, int]:
    """本地「LRV → se」全链路必须与 `settle.hac_lcb` 逐位一致——这是护栏。

    比的是 **`se`**，不是 `se²·n`。第六处同型缺陷的全部原因在这里：旧护栏把
    生产的 `se` 反解成 LRV 再比自己算的 LRV，而 `se²·n` 只在 floor 不绑定时
    才等于 LRV。于是「比得过」这个要求本身把探测向量限死在 floor 不触发的
    那一支（等权 MA(19)）——**缺陷被向量的选法排除在断言之外**。旧 docstring
    里写着「±1 向量在这里会合理地红」，红的就是 floor；散文记着两支核收底
    不同，断言结构上却看不见它。

    同型缺陷在整个仓库里出现过六次（`Q+1`、`2/3+1/H`、`n−k`、`sqrt(2/3)`、
    `n−k` 的第二个副本、本文件的 floor），全部来自「从邻支复制一行」。

    现在分两层，各自比对自己的对象：
    - **核层**：等权 MA(19)，两支核都不收底，比的是权重与分母；
    - **floor 层**：交替 ±1 加 6 条短 AR 抽样（φ ∈ ±0.8/±0.5/±0.2），`lr`
      落到 γ0 以下甚至为负，两支核的收底都必须与生产逐位一致。

    返回 `(最大相对偏差, floor 绑定次数, 配对数)`。**绑定次数是护栏自己的
    覆盖率**：它掉到 0 就说明这组向量又只看得见核、看不见 floor。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.decision.settle import hac_lcb

    rng = np.random.default_rng(4242)
    probes = [np.convolve(rng.normal(size=345), np.ones(Q + 1), mode="valid"),
              np.tile([1.0, -1.0], 40)]
    for phi in (-0.8, -0.5, -0.2, 0.2, 0.5, 0.8):
        e = rng.standard_normal(80)
        y = np.empty(80)
        y[0] = e[0]
        for t in range(1, 80):
            y[t] = phi * y[t - 1] + e[t]
        probes.append(y)

    worst, bind = 0.0, 0
    for kernel, fn in (("truncated", _lrv_trunc), ("bartlett", _lrv_bartlett)):
        for i, v in enumerate(probes):
            lr = fn(v[None, :], Q)
            se_local = float(_se(lr, np.array([v.var()]), v.size, kernel)[0])
            se_prod = hac_lcb(v.tolist(), Q, kernel=kernel)[1]
            dev = abs(se_local - se_prod) / max(1e-12, abs(se_prod))
            assert dev <= 1e-9, (
                f"{kernel} 的 se 与 settle.hac_lcb 不一致：探测向量 {i}，"
                f"本地 {se_local:.12f} / 生产 {se_prod:.12f}")
            worst = max(worst, dev)
            floor_const = v.var() if kernel == "truncated" else 0.0
            if float(lr[0]) < floor_const:
                bind += 1
    pairs = 2 * len(probes)
    print(f"保真 本地 se ≡ settle.hac_lcb（2 核 × {len(probes)} 向量，"
          f"最大相对偏差 {worst:.2e}；floor 绑定 {bind}/{pairs}）\n")
    return worst, bind, pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261105)
    ap.add_argument("--out", type=Path,
                    default=Path("data/.scratch/hac_size.json"),
                    help="size 表落盘路径（默认在 data/ 下，被 .gitignore 覆盖）")
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)
    print(f"q={Q}  Z90={Z90}  reps={args.reps}  MIN_NEFF={MIN_NEFF}\n")
    prod_dev, prod_bind, prod_pairs = _assert_matches_settle()
    bad = 0
    dump: list[dict] = []
    def four_rows(x: np.ndarray, lrv_true: float) -> list[dict]:
        """一格（一个 n）的四条估计量。`kernel` 决定 floor，不能统一传一个。"""
        n = x.shape[1]
        b13 = max(1, round(1.1447 * n ** (1 / 3)))
        # Q，不是 Q+1：settle.hac_lcb 的 lag=H-1、权重 1−k/(lag+1)=1−k/20
        return [
            _evaluate("bartlett_q", _lrv_bartlett(x, Q), x, lrv_true, "bartlett"),
            _evaluate("trunc_q", _lrv_trunc(x, Q), x, lrv_true, "truncated"),
            _evaluate("struct_H", _lrv_struct(x, Q + 1), x, lrv_true, "none"),
            _evaluate(f"bartlett_n13(b={b13})", _lrv_bartlett(x, b13), x, lrv_true,
                      "bartlett"),
        ]

    def emit(dgp: str, rows: list[dict], lrv_true: float) -> None:
        nonlocal bad
        print(f"{'est':<20}{'n':>6}{'se/true':>9}{'size%':>8}{'floor%':>9}"
              f"{'PASS%':>7}{'size|PASS%':>12}{'LRV<=0%':>9}")
        for r in rows:
            print(f"{r['est']:<20}{r['n']:>6}{r['se/true']:>9.3f}{r['size%']:>8.1f}"
                  f"{r['floor%']:>9.1f}{r['PASS%']:>7.1f}{r['size|PASS%']:>12.1f}"
                  f"{r['LRV<=0%']:>9.1f}")
            if (dgp == "ma" and r["est"] in GATED and r["PASS_n"] >= MIN_GATE_N
                    and r["size|PASS%"] > 8):
                bad += 1
            dump.append({**r, "dgp": dgp, "lrv_true": lrv_true})
        print()

    for dgp, gen, lrv_true in (
        ("ma   ", lambda n: _ma_equal(n, args.reps, Q, rng), float(Q + 1)),
        ("drift", lambda n: _ma_drift(n, args.reps, Q, 0.995, rng),
         (Q + 1) + (1 + 0.995) / (1 - 0.995)),
        ("iid  ", lambda n: rng.standard_normal((args.reps, n)), 1.0),
    ):
        print(f"=== DGP {dgp}  (真 LRV={lrv_true:.1f}) ===")
        rows = []
        for n in (250, 600, 1000):
            rows += four_rows(gen(n), lrv_true)
        emit(dgp.strip(), rows, lrv_true)

    # 326 = 面板的决策日数，用来堵住「250 与 600 之间是空的」这个缺口：条件读数
    # （`size|PASS`）恰在这段从 2% 的 PASS 率跳到 100%。**必须用独立 rng**：
    # 三个 DGP 共用上面那个 `rng`，把 326 塞进元组会让 drift/iid 的抽样流整体
    # 平移——实测 24 行按 MC 噪声改变，而 drift 列正是本表的证伪列。
    print("=== DGP ma/drift/iid @ n=326（独立 rng，不影响上面 36 行） ===")
    rng326 = np.random.default_rng(args.seed + 326)
    for dgp, x, lrv_true in (
        ("ma", _ma_equal(326, args.reps, Q, rng326), float(Q + 1)),
        ("drift", _ma_drift(326, args.reps, Q, 0.995, rng326),
         (Q + 1) + (1 + 0.995) / (1 - 0.995)),
        ("iid", rng326.standard_normal((args.reps, 326)), 1.0),
    ):
        print(f"--- {dgp} ---")
        emit(dgp, four_rows(x, lrv_true), lrv_true)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"args": {"reps": args.reps, "seed": args.seed, "q": Q, "z90": Z90,
                  "min_neff": MIN_NEFF},
         #: 护栏的覆盖率一并落盘：`prod_check_bind` 掉到 0 = 这组向量看不见 floor。
         "prod_check_rel": prod_dev,
         "prod_check_bind": prod_bind,
         "prod_check_pairs": prod_pairs,
         "rows": dump},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"产物 → {args.out}")
    print(f"门禁：{GATED[0]} 在 ma 列、PASS_n>={MIN_GATE_N} 的格里 size|PASS>8% 的个数"
          f" = {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
