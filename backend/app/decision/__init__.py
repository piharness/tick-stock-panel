"""决策账本 (Decision Ledger) — 从 Capital-Console 移植的「可证伪」那一半。

## 这个包只搬一件事：不变量，不搬模块

`D:\\DSH\\Capital-Console` 有 22 个模块、12k 行，其中真正不可替代的不是
`book.py` / `orders.py` / `web.py`（本仓库的 `strategy/paper.py` 更完整：T+1、
涨跌停排队、除权、多账户、append-only 成交台账），而是它的六条不变量：

1. **预登记**：决策在结果之前被冻结，冻结内容的哈希写进记录，事后不可改；
2. **三态**：PASS / TRIGGERED / **UNKNOWN**——"读不到"不许悄悄变成"没触发"；
3. **接线才算数**：一条没有代码读它的规则，与不存在没有区别；
4. **可证伪的验收**：预测必须跑赢常数基线（Brier skill > 0），否则不算信息；
5. **失败关闭**：状态坏了就拒绝给结论，而不是带着坏状态给一个好看的结论；
6. **只追加**：修复以新记录表达，从不改写历史。

因此本包不引入它的 `config/*.yaml`、junction 工作区、`capital_cny` 真钱账户、
Web 控制台与 kernel 信念库（那 38 个指标绑定的是另一个研究项目的数据）。

## 落在 TSP 里的位置

    strategy run / 人工选股
            │
            ▼
    ledger  ── 冻结决策卡（内容哈希 = 承诺）
            │
            ▼
    gates   ── K1 亏损 / K2 假设破坏 / K3 记录完整 / K4 数据新鲜（三态）
            │
            ▼
    settle  ── 到期按真实 bar 结算（限价撮合口径与 paper 一致）
            │
            ▼
    score   ── Brier / log-loss / 超额 LCB，对比常数基线
            │
            ▼
    pwin    ── 用已结算的卡反哺下一张卡的 p_win（账本自举，天然 walk-forward）

数据落 `data/user_data/decision/`，与 paper 账户同源同盘，不新增第二套真相。

**注意**：本 `__init__` 刻意不 re-export `settle` / `score` / `prior` 等函数——
它们与子模块同名，re-export 会让 `from app.decision import settle` 拿到函数而不是
模块（本项目已经因为这类隐式遮蔽栽过一次）。请显式 `from app.decision import settle`
取模块，或 `from app.decision.settle import score` 取函数。
"""
from __future__ import annotations

__all__ = ["bars", "gates", "ledger", "pwin", "settle"]
