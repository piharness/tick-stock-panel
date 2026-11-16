# 主动 Alpha 搜索

> 状态：**已实现研究内核与 CLI**（`backend/app/services/alpha_search.py`、`scripts/alpha_search.py`）。
> 尚未接入面板页面、周度调度和策略发布；当前只产出研究候选与台账，不写用户策略目录。

## 1. 它解决什么问题

现有挖掘（`docs/mining.md`）在**已有因子目录**上做组合搜索：选哪些因子、权重多少、外层样本外是否成立。它回答的是"已知信号怎么组合"，不回答"有没有新信号"。

主动 Alpha 搜索补的是前一层：**先提出新的可检验假设，再用两段数据验证**。它不是让模型自由写代码，也不是遗传规划；搜索空间是预先写死的模板网格，试验次数可数，否则多重检验无法校正。

与挖掘的分工：

| | 挖掘（mining） | 主动 Alpha 搜索（alpha_search） |
| --- | --- | --- |
| 输入 | 已有因子目录 + 用户选择的策略 | 模板网格 × 种子因子 × 窗口 |
| 搜索对象 | 因子组合与权重 | 新的 DSL 公式 |
| 验证 | 嵌套 walk-forward（训练/测试多折） | 发现期筛选 + 独立确认期一次读取 |
| 输出 | 可发布的策略候选 | 研究候选（pending），不自动发布 |
| 多重检验 | 候选级晋升门槛 | 批量 BH-FDR + 确认期 BH + DSR 诊断 |

## 2. 预注册的搜索族

生成逻辑在 `generate_candidates()`，不读行情数据，纯确定性：

- **种子**：20 个价量/流动性/收益形态因子（`momentum_*`、`rsi_14`、`macd_hist_pct`、`boll_position`、`annual_vol_20d`、`atr_pct`、`vol_ratio_5d`、`turnover_z_60d`、`amihud_20d`、`max_ret_20d`、`log_float_mv` 等）。财务因子暂不进入 v1：公告日口径下覆盖率低，与价量因子 IC 不可直接比。
- **窗口**：5 / 20 / 60 / 120 个交易日。
- **单因子模板**（输出统一做截面 `rank`）：
  - `rank(ts_delta(s, w))`：因子变化
  - `rank(ts_rank(s, w))`：时序分位
  - `rank(decay_linear(s, w))`：线性衰减平滑
  - `rank(s / ts_std(s, w))`：波动调整
  - `rank(s - ts_mean(s, w))`：偏离自身均值
- **交互模板**（只取经济上能讲通的 10 对，不做全组合）：
  - `rank(ts_corr(a, b, w))`：共动
  - `rank(ts_mean(a, w) - ts_mean(b, w))`：平滑价差

默认 360 条公式（5 个单因子模板 × 4 窗口 × 15 个种子 + 2 个交互模板 × 3 窗口 × 10 对）。生成后按模板轮转截断，避免前几个模板吃掉预算；同一条公式只保留一次。

`preregistration.json` 在读取行情前写入，包含配置、家族哈希、数据指纹和完整公式清单。家族哈希 = 排序后 canonical 公式的 SHA-256；同一份清单重复运行得到同一个哈希。

## 3. 两段验证

| 段 | 用途 | 是否参与选择 |
| --- | --- | --- |
| 发现期（discovery） | 批量检验全部公式，算 IC / ICIR / NW t / BH q / 覆盖率 / 换手率 | 是：方向、门槛、排序 |
| 确认期（confirmation） | 只对发现期幸存者跑一次，检查同向、幅度、t、q、覆盖率、换手率 | 否：读完才用于判定 |

默认窗口（可用 CLI 参数覆盖）：

```text
发现期：2025-09-01 .. 2026-04-30（159 个交易日）
确认期：2026-05-01 .. 2026-11-06（126 个交易日）
```

两段严格不重叠；配置校验强制 `discovery_end < confirmation_start`，每段至少 60 天。公式只引用过去数据（DSL 的 `ts_delay`/`ts_delta` 禁止负 shift），截面算子只作用于同日。

### 门槛（balanced 档）

| 阶段 | 条件 |
| --- | --- |
| 发现 | \|IC\|≥0.02、\|ICIR\|≥0.30、\|t_NW\|≥2.0、BH q≤0.10、覆盖率≥0.70、换手率≤0.90 |
| 确认 | 与发现期同向、\|IC\|≥0.01、\|t_NW\|≥1.5、q≤0.10、覆盖率≥0.70、换手率≤0.90 |
| 新颖性 | 与现有因子目录的最大日均截面秩相关 ≤0.80 |
| 确认期 FDR | q≤0.01 才标记 `confirmed`；0.01<q≤0.10 标 `provisional` |
| DSR | 诊断项：<0.90 时在判定原因中标注，但不单独否决已在确认期复现的候选 |

`exploratory` 放宽（允许 q 缺失），`strict` 收紧（\|IC\|≥0.03、\|ICIR\|≥0.50、\|t\|≥2.5、q≤0.05；确认期 q≤0.01 的 confirmed 门槛不变）。门槛是经验值，需按实际分布校准。

### 为什么 DSR 用 ICIR 而不是多空 Sharpe

分层多空组合在样本短、因子集中时会出现 ±50 的年化 Sharpe 离群值，直接把 Sharpe 方差代入 DSR 会把期望最大 Sharpe 抬到 12+ 年化，任何候选都被判成噪声。这里用日频 `ICIR = mean(IC)/std(IC)` 作为每期 Sharpe 的代理：它与筛选口径同源，方差稳定。方向选择按 2N 次有效试验计（每个公式可做多/做空），候选取 \|IR\|，避免"按发现期符号翻方向"把零分布系统性抬高。

DSR 只作诊断，不是唯一证据；确认期同向和确认期 BH q 仍然是硬条件。原因：确认期是筛选完成后才读取的独立样本，q≤0.01 是"样本外复现"的直接证据；DSR 依赖试验间独立、正态零分布等近似，单独用它否决一个已在独立窗口复现的候选会把门槛变成不可达。DSR 低于 0.90 时，判定原因会显式写出分歧，供人工复核。

## 4. 研究台账与幂等

每次运行写 `data/research/alpha_search/runs/{run_id}/`：

| 文件 | 内容 |
| --- | --- |
| `preregistration.json` | 运行前封存：配置、家族哈希、数据指纹、完整公式清单 |
| `trials.parquet` | 每条公式的发现/确认读数、门槛结果、状态、DSR、新颖性 |
| `report.json` | 汇总计数、候选、全部 trial 行 |
| `report.md` | 人读报告（前 20 条候选 + 未确认原因 + 口径限制） |
| `manifest.json` | 运行状态、耗时、计数、产物路径 |

全局 `candidates.jsonl` 只追加，以 `ledger_id`（公式 + 发现/确认窗口）幂等。重复运行默认跳过台账里已测过的 `(公式, 发现期, 确认期)`；要重测同一窗口必须显式 `--allow-repeat`。这既是研究记忆，也是 DSR trial count 的来源——不记录试验次数的搜索没有可解释的显著性。

## 5. 用法

```bash
cd backend
uv run python ../scripts/alpha_search.py --dry-run          # 只生成公式族并打印哈希
uv run python ../scripts/alpha_search.py --max-formulas 360
uv run python ../scripts/alpha_search.py --profile strict --max-formulas 360
```

常用参数：`--discovery-start/end`、`--confirmation-start/end`、`--output-dir`、`--data-dir`、`--allow-repeat`。

数据读取走 `DataStore → KlineRepository → BacktestEngine.load_panel`，与回测、挖掘同源；公式检验走 `FactorBacktestService.run_batch`，不另写第二套 IC/分层计算。生成因子注册在 `isolated_registry()` 上下文里，退出即丢弃，不污染全局因子目录。

## 6. 首次实测（2026-11-09）

- 运行：`20261109-124555-d71d9a`，balanced，360 条公式，发现期 159 天 / 确认期 126 天。
- 漏斗：360 生成 → 360 有效检验 → 91 发现通过 → 53 确认通过 → 24 因与现有因子高度相关被拒 → 16 confirmed / 13 provisional → 按 `template+seeds` 聚类后 **11 个独立主题**。
- 最强的两个主题（确认期 BH q 已对 91 个幸存者校正）：
  - `rank(ts_corr(rsi_14, vol_ratio_5d, 5))`：发现 t=-6.96、确认 t=-5.92、确认 q=2.9e-07、与现有因子最大相关 0.31。
  - `rank(ts_corr(boll_position, amount_ratio_5d, 5))`：发现 t=-7.12、确认 t=-5.73、确认 q=4.5e-07、最大相关 0.36。
- 两个主题都是**负 IC**：RSI 与量比同向共振越强，未来收益越低；反向使用（买低共振）才是 alpha 方向。其余主题集中在波动/换手/收益形态的平滑与差分。
- DSR 与确认期证据分歧明显：最强主题 DSR 只有 0.21–0.25，按 360 次搜索校正后发现期证据不足以单独支撑；`confirmed` 的依据是独立确认期的复现。报告对每条 confirmed 都写明 DSR，供人工复核。
- 最高发现 IC 的 `rank(decay_linear(amount_ratio_5d, 60))` 与 `vol_trend_5_60` 秩相关 0.81，被新颖性门槛拒绝——它是旧因子的平滑版，不是新 alpha。
- 这不是"找到 11 个可交易策略"：16 条 confirmed 是研究候选，仍需容量/成本/涨跌停/停牌约束与 paper 账本前瞻验证。系统做到的是"在 360 次搜索后，把能复现的与不能复现的分开"。
- **盲测 caveat**：本次开发过程中已多次查看该确认期，严格意义上它不再是盲测。生产使用应固定一个未来窗口、首次读取后封存，并在台账中记录"已读窗口"，不能反复用同一段确认期调模板。

## 7. 明确不做

- 不自动发布策略、不写 `data/strategies/`、不改用户因子库。
- 不做遗传规划/自由代码生成/LLM 自由写公式；搜索空间必须可数。
- 不接分钟/盘口/财务事件；v1 只做日频价量。
- 不做组合优化、仓位、做空可行性；多空收益只是理论价差。
- 不把确认期结果反馈回模板/阈值选择；确认期只读一次。

## 8. 验收标准

- 同一配置重复运行得到同一 `family_hash`；`preregistration.json` 在行情读取前存在。
- 360 条公式全部可编译；每条 trial 有唯一 `candidate_id`，`trials.parquet` 行数 = 生成数（或明确记录缺失原因）。
- 发现期与确认期无重叠；确认期只对发现期幸存者运行。
- 重复运行默认跳过已测公式；`--allow-repeat` 才重测。
- 任一候选进入 `confirmed` 必须同时满足：发现门槛、确认门槛、同向、新颖性、确认期 BH q≤0.01；DSR 记录并标注，不单独否决。
- 报告明确区分 `confirmed / provisional / rejected`，未确认候选给出可读原因。
- 测试：`uv run pytest tests/test_alpha_search.py -q`（生成、门槛、DSR、配置校验、台账幂等、真实批量链路）。

## 9. 下一步（按信息增益排序）

1. **接入面板与调度**：把 `run_alpha_search` 包成后台任务，复用挖掘的 worker/limiter/SSE，不阻塞请求线程；周度跑一次，结果进研究候选库。
2. **更长确认期与 paper 账本**：确认期扩展到 ≥250 个交易日；provisional 候选进入 `decision` 账本做前瞻结算，用 Brier/收益双重校验。
3. **更强的零分布**：用同日截面置换或 block bootstrap 估计"360 次搜索的最大 IC"分布，替代 DSR 的正态近似。
4. **事件与盘中维度**：把涨停/竞价/盘口事件做成条件模板，而不是继续在日频价量上做变换。
5. **风险中性化**：行业/规模中性化后再算 IC，避免把 beta/小市值暴露当成 alpha。
