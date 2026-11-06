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

    LRV_hat → (2/3 + 1/(q+1))·LRV

于是 `se` 恒为真值的 `sqrt(2/3) ≈ 0.817` 倍，**不随 n 收敛**；
`n_eff = n·(se_iid/se)²` 被同一份偏差放大 1.5 倍。

## 本脚本比对的估计量

1. `bartlett_q`   —— 现状：Bartlett，b = q+1，分母 n
2. `trunc_q`      —— 截断（矩形）核，b = q，分母 n-k：LRV 无偏，但方差大
3. `struct_H`     —— 结构性：重叠 H 日 ⇒ `LRV = H·γ0`，只估 γ0 一个标量
4. `bartlett_n13` —— Bartlett，b ≈ 1.1447·n^(1/3)（随 n 增长，一致）

## 三个 DGP

- `ma`    ：等权 MA(q)，记忆恰为 H-1（代码假定的模型）
- `drift` ：MA(q) + AR(1) ρ=0.995 的慢漂移（记忆**超过** H-1）
- `iid`   ：无重叠（H=1 的退化情形，作为对照）

`drift` 是关键的证伪列：任何固定带宽 b=H 的核在这里都会崩，包括截断核。
「换核」只修好了代码假定成立的那一列。

用法：

    uv run python scripts/probe_hac_size.py

退出码非零 ⇒ 有估计量在 ma 列上超过 8%。CI 门禁可以只跑 `--reps 500`。
"""
from __future__ import annotations

import argparse
import math
import sys

import numpy as np

Z90 = 1.645
Q = 19          # lag = H-1，H=20
MIN_NEFF = 30
HAC_MIN_RATIO = 3


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


def _lrv_bartlett(x: np.ndarray, b: int) -> np.ndarray:
    """逐行 Bartlett HAC（分母 n，与 `settle.hac_lcb` 逐位一致）。"""
    reps, n = x.shape
    d = x - x.mean(axis=1, keepdims=True)
    g0 = (d * d).mean(axis=1)
    lr = g0.copy()
    for k in range(1, min(b, n - 1) + 1):
        w = 1.0 - k / (b + 1.0)
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


def _evaluate(name: str, lrv: np.ndarray, x: np.ndarray, lrv_true: float) -> dict:
    n = x.shape[1]
    mean = x.mean(axis=1)
    g0 = x.var(axis=1)
    se = np.sqrt(np.maximum(lrv, 0.0) / n)
    se_iid = np.sqrt(g0 / n)
    lcb = mean - Z90 * se
    pass_lcb = lcb > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(se > 0, se_iid / se, np.inf)
    # 与 settle.effective_sample 同一个构造：n < 3(lag+1) 时退回 n//(lag+1)。
    n_eff = np.where(n < HAC_MIN_RATIO * (Q + 1), max(1, n // (Q + 1)),
                     np.clip(np.rint(n * ratio ** 2), 1, n))
    gate = n_eff >= MIN_NEFF
    return {
        "est": name,
        "n": n,
        "se/true": float(np.mean(se[se > 0]) / math.sqrt(lrv_true / n)),
        "size%": 100 * float(pass_lcb.mean()),
        "PASS%" : 100 * float(gate.mean()),
        "size|PASS%": 100 * float(pass_lcb[gate].mean()) if gate.any() else float("nan"),
        "LRV<=0%": 100 * float((lrv <= 0).mean()),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261105)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)
    print(f"q={Q}  Z90={Z90}  reps={args.reps}  MIN_NEFF={MIN_NEFF}\n")
    bad = 0
    for dgp, gen, lrv_true in (
        ("ma   ", lambda n: _ma_equal(n, args.reps, Q, rng), float(Q + 1)),
        ("drift", lambda n: _ma_drift(n, args.reps, Q, 0.995, rng),
         (Q + 1) + (1 + 0.995) / (1 - 0.995)),
        ("iid  ", lambda n: rng.standard_normal((args.reps, n)), 1.0),
    ):
        print(f"=== DGP {dgp}  (真 LRV={lrv_true:.1f}) ===")
        rows = []
        for n in (250, 600, 1000):
            x = gen(n)
            rows.append(_evaluate("bartlett_q", _lrv_bartlett(x, Q + 1), x, lrv_true))
            rows.append(_evaluate("trunc_q", _lrv_trunc(x, Q), x, lrv_true))
            rows.append(_evaluate("struct_H", _lrv_struct(x, Q + 1), x, lrv_true))
            b13 = max(1, round(1.1447 * n ** (1 / 3)))
            rows.append(_evaluate(f"bartlett_n13(b={b13})",
                                  _lrv_bartlett(x, b13), x, lrv_true))
        print(f"{'est':<20}{'n':>6}{'se/true':>9}{'size%':>8}{'PASS%':>7}"
              f"{'size|PASS%':>12}{'LRV<=0%':>9}")
        for r in rows:
            print(f"{r['est']:<20}{r['n']:>6}{r['se/true']:>9.3f}{r['size%']:>8.1f}"
                  f"{r['PASS%']:>7.1f}{r['size|PASS%']:>12.1f}{r['LRV<=0%']:>9.1f}")
            if dgp.strip() == "ma" and r["size|PASS%"] > 8:
                bad += 1
        print()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
