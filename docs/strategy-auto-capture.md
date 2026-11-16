# 策略自动捕获（strategy auto-capture）

> 需求来源：给策略池增加「自动捕获」——从淘县/淘股吧等社区检索战法，批量生成策略，
> 自动过审计门后入池，全链自动化，池本体迁到后端持久化、可回查。
>
> 本文是设计与验收契约，不写实现细节。实现代码另见各模块。

---

## 实现进度（2026-11-15）

| 段 | 模块 | 状态 |
| :-- | :-- | :-- |
| 策略池持久化 | `app/services/strategy_pool.py` + `app/api/strategy_pool.py` | ✅ 完成，9 测试 |
| S4 横截面 alpha | `app/services/cross_section_alpha.py` | ✅ 完成，8 测试（含端到端判据对） |
| 全链编排 (S1~S5) | `app/services/strategy_capture.py` + `app/api/strategy_capture.py` | ✅ 完成，6 测试 + 容器实测 |
| 前端迁移 | `useStrategyPool` → 后端 API + 采集入口 UI | ✅ 完成，tsc/build/vitest 通过 |

### 容器实测（2026-11-15，`--build` 重建后）

已 `docker compose up -d --build` 实测通过：

- `eval_protocol` 已提升为后端模块 `app/strategy/eval_protocol.py`（进镜像，消除 `analysis/` 动态路径依赖）。
- `/api/strategy-pool` GET/PUT/history 全通，落盘文件宿主机可见（持久化真实生效）。
- `/api/strategy-capture` 提交→后台线程→AI 生成→策略落盘→采集产物三件套→回测子进程 spawn，全链 S1~S4 真实跑通。

---

### 全链已接通的路径（S1~S5）

`POST /api/strategy-capture`（提交文本）→ 后台线程跑 `run_capture`：

1. S1 原文落 `data/community_capture/{uuid}/source.txt`
2. S2 `AIStrategyGenerator.generate` → 策略代码（ast 校验）
3. S2.5 口径校验：非日线策略（timeframes 含 1m）fail-fast 拒绝
4. S3 落 `data/strategies/ai/{ai_xxx}.py`（research_only 草稿）
5. S4 回测（max_positions=10000 逼近全信号）→ `trades` 提取信号 → `compute_signal_alpha` + `compute_capacity_curve` + `build_claim`
6. S5 `eval_protocol.evaluate` → **PASS 自动 publish + `strategy_pool.add(capture_pass)`；REJECT 归档**

产物三件套（source.txt / strategy.py / verdict.json）同一 uuid 贯穿，可追溯。

> **口径约束**：自动捕获只支持**日线策略**（`timeframes=['1d']`）。横截面 alpha 链是
> `open_t1→close_h` 的日线口径；分钟策略（minute_filter/1m）无法进入该链，且分钟回测
> panel 缺 `consecutive_limit_ups` 等日线列会 ColumnNotFoundError。提示词硬约束 + S2.5
> 后校验双保险，防止 LLM 把「开盘后 N 分钟」误生成分钟策略。

### 关键决策（已与用户确认）

**PASS 后自动 publish（research_only=False）+ 入池**，五道门 PASS 取代"人点发布"。
自动发布动作显式记录在 `verdict.provenance.auto_publish=true` + `strategy_pool_history`
（action=capture_pass），可回滚、可追溯。

---

### S4 已固化的判据（最重要）

`cross_section_alpha.compute_signal_alpha` 实现了三种基准口径的横截面超额：
- `market`：扣同日全市场等权（不控维度，G3 会 `unmatched_benchmark` 拒）
- `bucket`：同涨幅箱（控 `pct`）
- `board_bucket`：同板块 + 同涨幅箱（控 `board` + `pct`，对应审计补记的正确口径）

端到端判据对（`test_cross_section_alpha.py`）：
1. **自动推导不完整的 Claim 必被 REJECT**（缺 `unfillable_share` / `holding_days` /
   `capacity_annual_pct` → `unfillable_share_missing` 等）。
2. **口径齐全的正对照必 PASS**（G1~G5 全绿）——防止"恒拒绝"伪自动化。

这两个测试合起来证明五道门"会分辨"，而不是"只会拒绝"或"只会放行"。

### S4 的诚实边界（三项已补两项）

`build_claim` 现在支持传入补齐项，并自动推导 `holding_days`：

- `holding_days`：✅ 已自动从 `(entry, exit)` 推导（复用 `eval_protocol.holding_days_between`），
  保证与成交价口径自洽（`open_t1→close_t2 = 1`）。
- `capacity_annual_pct`：✅ 已提供 `compute_capacity_curve(data_dir, strategy_id, max_positions=...)`，
  复用 backtest worker 跑多容量回测，返回 `{max_positions → 年化收益%}`。
- `entry_unfillable_share`：⏳ 仍需开板次数（`tdxaidata` GP14，权限未验证），
  采集管道暂以 0.0 传入（`build_claim` 显式标 `auto_derived` 警告"该数为自报"）。

**端到端已验证**：补齐 `capacity_annual_pct` + `entry_unfillable_share` 后，
`build_claim` 产出的 Claim 能真正 PASS（G1~G5 全绿）；不补则必然 REJECT
（`unfillable_share_missing`）。这一对判据固化在 `test_cross_section_alpha.py`。

---

## 0. 结论先行（三句话）

1. **全链可以自动执行，但每一步的 provenance 必须落盘，绝不静默采信。**
2. **五道门（`eval_protocol.py`）是「拒绝器」不是「验证器」：PASS 只等于"没被这套规则否掉"，
   不等于"有 edge"。** 这与本仓已有审计（`docs/youzi-strategy-audit.md`）的立场一致。
3. **社区战法已被本仓数据证伪（追涨族全负），因此自动捕获的预期产出是「大量 REJECT 归档
   + 极少数 PASS」；这个低通过率是本功能的正确行为，不是 bug。**

---

## 1. 全链五段与每段的可信边界

| 段 | 输入 → 输出 | 实现落点 | 可信边界（必须落盘的标注） |
| :-- | :-- | :-- | :-- |
| S1 采集 | 社区文本/粘贴/URL → 规范化战法描述 | `services/community_source.py` | 原文原样保存，来源 URL/时间/作者可追溯 |
| S2 生成 | 战法描述 → Polars 策略 `.py` | 复用 `strategy/ai_generator.py` | AI 可能曲解原意；策略代码与原文并存，可对照 |
| S3 回测 | 策略 → 收益曲线/统计 | 复用 `backtest/engine.py` | 生存偏差/基准/选择机制风险；结果标注口径 |
| S4 效应量 | 回测统计 → `eval_protocol.Claim` | `services/claim_factory.py` | `effect_bp/t/mde/n_tests` 多为**自报/推导值**，非金标准 |
| S5 判分 | `Claim` → `Verdict` | 复用 `analysis/eval_protocol.py` | PASS ≠ 有 edge；REJECT 是默认、安全的行为 |

> S4 是整条链里最不可信的一环：把回测收益曲线自动转成 `Claim` 所需的
> `effect_bp / t_stat / per_day_t / matched_effect_bp / mde_bp / annual_net_pct / n_tests`，
> 目前没有任何现成自动化——`youzi-strategy-audit.md` 里这些数字全是人工写脚本算的。
> 本功能首次把这一步自动化，因此**它产出的 Claim 必须被显式标注为"自动推导，未经人工复核"**。

---

## 2. 数据落地（全部后端持久化、可回查）

| 数据 | 路径 | 格式 | 说明 |
| :-- | :-- | :-- | :-- |
| 策略池（本体） | `data/strategy_pool.json` | `{"ids": [...], "updated_at": ...}` | 取代前端 localStorage `strategy-pool` |
| 池变更历史 | `data/strategy_pool_history/YYYYMMDD-HHMMSS-<uuid>.json` | 追加式 | 每次入池/出池/重排都记一条，可回查 |
| 采集原文 | `data/community_capture/<uuid>/source.txt` | 文本 | 社区原文，不可改 |
| 生成策略 | `data/strategies/ai/` | `.py` | 复用现有 AI 策略目录 |
| 审计产物 | `data/community_capture/<uuid>/verdict.json` | JSON | `Claim` + `Verdict` 全量落盘 |
| 归档（REJECT） | `data/community_capture/<uuid>/` | 目录 | 整个采集单元，含 source/code/verdict |

> 关键设计：**一个采集单元（uuid）贯穿 S1→S5**，所有产物挂在同一目录下，
> 从社区原文到最终判决一条链可追溯。

---

## 3. 前后端契约

### 后端 API（新增）

| 方法 | 路径 | 作用 |
| :-- | :-- | :-- |
| GET | `/api/strategy-pool` | 读取后端池（含历史） |
| PUT | `/api/strategy-pool` | 覆盖池 + 记历史 |
| POST | `/api/strategy-capture` | 提交采集（文本/URL），触发 S1→S5 |
| GET | `/api/strategy-capture/{uuid}` | 查询采集进度与 verdict |
| GET | `/api/strategy-capture` | 列出历史采集记录 |

### 前端

- `useStrategyPool` 改为从后端 API 读写（本地 localStorage 作为离线兜底，但后端为准）。
- 新增「自动捕获」入口：输入社区文本/URL → 提交 → 展示 S1~S5 进度与最终 verdict。
- PASS 的策略自动出现在策略池；REJECT 归档可在「捕获历史」里查看原因。

---

## 4. 验收标准（可证伪、可量化）

> 所有验收均在容器 `--build` 后、后端持久化路径上验证，不接受 localStorage 假象。

1. **池迁后端**：清空浏览器 localStorage 后刷新，策略池仍从后端恢复；两台浏览器看到同一份池。
2. **可回查**：连续 3 次改池后，`strategy_pool_history/` 有 3 条记录，每条含变更前后 diff 与时间戳。
3. **采集链完整**：提交一段社区文本后，`community_capture/<uuid>/` 下同时存在
   `source.txt`、策略 `.py`、`verdict.json`，三者可互相追溯（同一 uuid）。
4. **判分真实**：对已知 REJECT 案例（如"打板/首板接力"，本仓审计已证伪），自动捕获产出的
   verdict 必须为 `REJECT`，且 `reasons` 命中 `G1~G5` 中至少一条；对合成正对照（口径齐全）
   必须 `PASS`。**这条防止"恒拒绝"或"恒放行"的伪自动化。**
5. **PASS 才入池**：只有 `decision == "PASS"` 的策略自动进入后端池并触发扫描刷新；
   `REJECT` / `REJECT_ECON` 一律归档不入池。
6. **provenance 落盘**：每个 verdict 的 `provenance` 含来源 URL/时间/作者、生成模型、
   回测口径、以及 `auto_derived: true` 标注（S4 自动推导，未人工复核）。
7. **不静默采信**：S4 推导的 Claim 字段若无法计算（如无成交无法算 effect），必须判
   `REJECT`（`effect_missing`），**不得**跳过五道门或默认放行。
8. **失败隔离**：AI 生成失败/回测失败/判分异常，该采集单元标 `failed` 并落盘错误原因，
   不影响其它单元，不中断整批。

---

## 5. 已知风险与未关闭的洞（诚实标注）

- **S2 语义失真**：AI 把"分歧转一致"这类主观战法转成客观指标时可能失真；靠保留原文对照缓解，不靠 AI 自评。
- **S4 自动推导的统计量**：`mde_bp`、`n_tests`、`matched_effect_bp` 若自动推导，存在口径错配风险；
  已在 `provenance.auto_derived` 显式标注，人工可在 verdict.json 上复核。
- **生存偏差**：回测用存活快照，退市股缺失会高估收益；五道门不拦截这一点（它拦的是口径错误），
  需在 verdict 的 `provenance` 里单独标注 `survivorship_bias: unassessed`。
- **社区内容合规**：采集内容仅作策略研究用途，保留来源，不存储账号密钥；抓取频率需遵守目标站点 robots。

---

## 7. 下一步（按"能改变结论"的程度排）

| 步 | 动作 | 验收 |
| :-- | :-- | :-- |
| 0 | ~~S1/S2 接线~~ ✅ 已接通（`strategy_capture.run_capture` 全链编排） | 一个采集单元（uuid）含 source/code/verdict 三件套可追溯 ✅ |
| 1 | ~~S4 补三项自动计算~~ ✅ 三项全补：`capacity_annual_pct`（`compute_capacity_curve`）、`holding_days`（自动推导）、`entry_unfillable_share`（buyable 口径自动算） | 自动 Claim 能产出「口径齐全」的正对照，端到端 PASS ✅ |
| 2 | ~~S5 接线~~ ✅ 已接通（`run_capture` 内 `evaluate` → `strategy_pool.add(capture_pass)` 或归档） | PASS 入池、REJECT 归档，provenance 落盘 ✅ |
| 3 | ~~前端 `useStrategyPool` 迁后端 API + 采集入口~~ ✅ 完成 | 清 localStorage 后池仍从后端恢复；采集历史可回查 |

## 8. 关键结论（务必先读这条再动代码）

**自动捕获的预期产出是「大量 REJECT + 极少数 PASS」，这是正确行为，不是 bug。**
五道门是拒绝器不是验证器；社区战法已被本仓数据证伪。自动化的价值是"不漏检、可追溯"，
不是"自动发现 alpha"。

---

## 6. 与现有审计纪律的关系

本功能不替代、不绕过 `docs/youzi-strategy-audit.md` 与 `eval_protocol.py`。相反，它把"人工跑审计"
固化为"每次采集自动跑一遍五道门"。**自动化的价值是"不漏检、可追溯"，不是"自动发现 alpha"。**
