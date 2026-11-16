# 策略自动捕获 · 文件清单与提交建议

> 生成时间：2026-11-15 23:41。本清单用于把「策略自动捕获」功能分文件提交，
> 避免把工作区里他人的未提交改动带进提交（MEMORY #43 教训）。

## 一、纯新增文件（`??`，可直接 `git add`）

| 文件 | 作用 |
| :-- | :-- |
| `backend/app/services/strategy_pool.py` | 策略池后端持久化服务 |
| `backend/app/services/cross_section_alpha.py` | 横截面 alpha 统计链（S4 核心） |
| `backend/app/services/strategy_capture.py` | 全链编排 S1~S5 |
| `backend/app/api/strategy_pool.py` | 策略池 API |
| `backend/app/api/strategy_capture.py` | 采集 API |
| `backend/app/strategy/eval_protocol.py` | 五道门后端化副本（`analysis/eval_protocol.py` 的复制） |
| `backend/tests/test_strategy_pool.py` | 9 测试 |
| `backend/tests/test_cross_section_alpha.py` | 9 测试 |
| `backend/tests/test_strategy_capture.py` | 6 测试 |
| `docs/strategy-auto-capture.md` | 设计与验收契约 |
| `frontend/src/components/screener/StrategyCaptureDialog.tsx` | 采集对话框组件 |

> 这 11 个文件是全新文件，无历史，可安全 `git add` 后一起提交。

## 二、修改的文件（`M`，含他人改动，**必须分开处理**）

### 2.1 纯我的改动，可直接提交

| 文件 | 我的改动行数 | 说明 |
| :-- | :-- | :-- |
| `frontend/src/lib/useStrategyPool.ts` | 66 行 | 纯我改：后端为准 + localStorage 兜底 |
| `frontend/src/pages/Screener.tsx` | 20 行 | 纯我改：加「自动捕获」按钮 + 对话框渲染 |

> 这两个文件经核实 diff **不含他人改动**，可安全提交。

### 2.2 混合他人改动，**不能直接提交**

| 文件 | 我的改动 | 他人改动（需先落库） |
| :-- | :-- | :-- |
| `backend/app/main.py` | 4 行（strategy_pool/capture 的 import + include_router） | 其余 ~133 行（他人未提交） |
| `frontend/src/lib/api.ts` | 6 个 API 方法（strategyPool/Capture） | `DecisionSlate` 系列类型（他人未提交） |

> 这两个文件的 diff 里混有他人的未提交改动。**直接 `git commit -- <path>` 会把他人改动一起带进去**。
> 处理方式（三选一）：
> 1. **等他人改动先落库**，再提交这两个文件的完整 diff；
> 2. 用 `git add -p` 逐 hunk 只暂存我的改动（4 行 / 6 个方法），再提交；
> 3. 若他人改动与我的改动语义独立（确实是），`git add -p` 最干净。

## 三、建议的提交顺序（按依赖从底到顶）

1. **提交 A（后端地基）**：`strategy_pool.py` + `strategy_pool API` + `test_strategy_pool.py` + `eval_protocol.py`
2. **提交 B（S4 统计链）**：`cross_section_alpha.py` + `test_cross_section_alpha.py`
3. **提交 C（全链编排）**：`strategy_capture.py` + `strategy_capture API` + `test_strategy_capture.py` + `docs/strategy-auto-capture.md`
4. **提交 D（前端）**：`useStrategyPool.ts` + `StrategyCaptureDialog.tsx` + `Screener.tsx`（纯我改部分）
5. **提交 E（接线，需 add -p）**：`main.py` 的 4 行 + `api.ts` 的 6 方法（用 `git add -p` 逐 hunk）

## 四、注意事项

- `backend/app/strategy/eval_protocol.py` 是 `analysis/eval_protocol.py` 的**复制**（纯标准库、零依赖）。
  提交时两者内容一致；将来若改协议，两处都要同步（或考虑 `analysis/` 改为 re-export）。
- 本次**未改** `analysis/eval_protocol.py`（它保持原样，分析脚本继续用）。
- 提交前务必 `git diff --stat -- <每个文件>` 核对行数 = 自己写的行数（MEMORY #43）。
