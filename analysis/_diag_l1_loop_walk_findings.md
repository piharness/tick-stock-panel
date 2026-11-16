# L1 回路演练：从 candidates() 到 settle()，断点逐个落地

> 脚本 `analysis/_diag_l1_loop_walk.py`
> 产物 `analysis/_diag_l1_loop_walk_open_only_result.json`（正确口径）
> 　　 `analysis/_diag_l1_loop_walk_auto_result.json`（默认路径）
> 窗口 2026-11-05 .. 2026-11-11 五个决策日（entry 2026-11-06 .. 2026-11-12）
> 隔离：临时 data_dir + junction，`data/user_data/decision` 至今**仍不存在**

---

## 0. 结论

**回路是活的，但接上去会错四处。** 现有代码能在历史窗口上完整走完
「候选 → 卡 → 结算 → 收益」，且收益口径与研究侧逐位一致（max 0.005bp）。
但默认路径会造出**另一个策略**，冷启动会**一张卡都不发**，而卡**永远不会变成订单**。

> 2026-11-13 12:45：B2（口径）与 B5（盘中快照守卫）已修并回归，数字逐位不变。
> 2026-11-13 13:05：B1 已做（第 7 节），账本第一次有 10 张卡 / 200 行结算；
> 第一次读数是 `median` skill 为负 —— n_eff 只有 10，还看不出结论。
> 顺带查出并修了第三个口径缺陷：median 基准的入场价（第 8 节）。
> 2026-11-13 13:35：B4 已做（第 9 节）。真实价格预演：底仓 700 股不变、
> 买 700 @22.28 / 卖 700 @23.10，回合闭合。默认开关仍是关。
> B3（L1 接 producer）与 B6（cohort 覆盖边界）留在原地。

| 环节 | 状态 | 依据 |
|---|---|---|
| `candidates()` → 20 只带限价清单 | 通 | 5/5 天各出 20 只 |
| `produce_card()` → 卡 | 通（但见 B1/B2） | 5/5 `status=created`，`gate=GO` |
| `settle()` → 结算行 | 通 | 100 行，0 pending |
| 收益口径 = 研究口径 | **通** | max\|diff\| = **0.005bp**，0/42 行超 0.01bp |
| 卡 → 纸面订单 | **不存在** | `create_order` 三个调用点均不从 ledger 出发 |
| `p_win` 有证据 | **无** | `source=prior`、`prior_weight=1.0`（100% 常数 0.50） |

---

## 1. 对账表：两个口径并列

`fill_mode` 不是一个开关，是**两个不同的策略**（`settle._fill` docstring 原话）。
同一批 picks、同一批日子，只改这一格：

| 指标 | `open_only`（L1 口径） | `limit`（自动路径默认） | 差 |
|---|---:|---:|---:|
| 成交笔数 | **42** / 100 | **70** / 100 | +28（+66.7%） |
| 逐笔均值 | **+118.70bp** | +108.18bp | −10.5bp（−8.9%） |
| 与研究的 max\|diff\| | **0.005bp** | 915.31bp | — |
| 超 0.01bp 的行 | **0** | 28 / 70（40%） | — |
| `fill_basis` 分布 | OPEN 42 / OPEN_ONLY 58 | OPEN 42 / LIMIT 28 / 空 30 | — |

**关键读数**：`limit` 多出来的 28 笔**全部**是 `OPEN` 失败后被「盘中回落触及限价」
补上的。它们恰好是 R5 说的「高开走弱」那批，方向上与 L1 相反。混进账本后
策略看起来更"稳"（成交更多、均值更平），但那是另一个策略。

逐日（`open_only`）：

| as_of | entry | 名单 | 成交 | 均值 bp |
|---|---|---:|---:|---:|
| 2026-11-05 | 11-06 | 20 | 14 | +294.5 |
| 2026-11-06 | 11-09 | 20 | 8 | +342.0 |
| 2026-11-09 | 11-10 | 20 | 4 | +494.7 |
| 2026-11-10 | 11-11 | 20 | 7 | +146.1 |
| 2026-11-11 | 11-12 | 20 | 9 | **−541.7** |
| 合计 | — | 100 | **42** | **+118.70** |

42 笔 / 5 天 = **8.4 只/日**，与研究的 8.71 只/日（2,752 ÷ 316）同量级。
总均值 +118.70bp 与 R5 记录的**逐笔均值 +118.9bp** 相差 0.2bp —— 这是
生产管线对研究结论的一次独立复现，不是自证。

> 单日 11-11 的 −541.7bp 是 5 天里唯一的负日。样本只有 9 笔，不构成结论；
> 它同时说明「+118bp」不是每天都成立。

---

## 2. 六个断点

### B1 冷启动死锁 —— 今天接线，明天仍然 0 张卡

`decision_auto_allow_prior` 生产配置是 `false`。复现（`--no-prior`）：

```
status=skipped_no_evidence
策略 l1_gap_reversal 没有可用的历史结算样本（n=0 < 8）：只有先验，没有证据。
```

建卡要 `n ≥ 8`，`n` 只能来自已结算的卡，而空账本永远结算不出第一张。
这是一条**闭环的等待**。报错文本自己写了出路（补登历史卡 或 `allow_prior=true`），
但两条都在自动路径之外。

### B2 `produce_card` 没有 `fill_mode` 参数（已修）

`ledger.build_card(fill_mode=...)` 有，`api/decision.py` 的显式建卡有，
**中间这一层没有**。于是每天自动发出的卡恒为 `limit`，而 L1 必须 `open_only`。
代价见第 1 节：−8.9% 均值、40% 样本不可比。

这不是"漏了个参数"：它意味着**自动路径从来没有能力表达 L1 的成交假设**。

### B3 L1 没有接进生产者

`decision_auto_strategy_id = "trend_breakout"`（内置种子，止损无止盈），
而 L1 只通过 `GET /api/decision/l1/candidates` 出清单，**没有任何建卡路径**。
配置指向的那条腿在 347 天实测里是 −31.7bp（动量 20 日），
证据最硬的那条腿（+118.7bp、t 4.2）反而在账本外。

### B4 卡 → 订单：没有任何桥

`paper.create_order` 只有三个调用点：手动 API、`custom/paper_execution`、
监控规则（`paper_auto`）。**没有一个读 `decision` 账本。**
所以即使账本里躺着卡，也不会有仓位。纸面账户 8 天 0 单不是"没人盯"，
是**没有代码能把卡变成订单**。

### B5 盘中快照可以被结算成收盘价（已修）

`data/kline_daily_enriched/date=2026-11-13/` 有 5,571 行、字段齐全、
`(date,symbol)` 无重复 —— 所有浅层判据都绿。但它的 `quote_ts` 是
**10:42:47**（盘中）。`horizon=0` 时 exit == entry，所以：

```
as_of=2026-11-12  →  entry=2026-11-13  →  用 10:42 的价格算"收盘收益"
```

而结算行一旦写下就被 `done` 键固化。本脚本的守卫已拦下这一支（实测
`guard_rejected_intraday`），但**生产链路没有这道守卫**。

顺带：`date=2026-11-09` 与 `date=2026-11-10` 的 `quote_ts` **整列为 null**
（5572/5571 行全空）。所以「用 quote_ts 判收盘」不能只做两态 ——
null 既可能是"缺失"也可能是"没记"。正确判据是**日期先于数据**：
过去的日子直接放行，只有"今天"才需要 quote_ts 作证，它缺席时只能 UNKNOWN。

### B6 研究侧覆盖不到要用的那 20 天

`cohort_cells(require_next=True)` 要求 `fwd = close.shift(-20)` 有限。
实测：研究 cohort 2,816 行 / 321 日，**最新只到 2026-10-19**，而数据到 11-12。

这不是缺陷，是那条前视过滤的定义。但后果必须写明：**实盘要用的最后 20 个
交易日，研究口径一天都判不了**。所以「研究 vs 生产」的对账在这 20 天里
只能靠自建对照（本脚本做的：同一批 picks、同一根 bar 的复权 open→close），
不能引用 `cohort_cells`。

---

## 3. 三条不能省的边界

1. **样本只有 5 天 / 42 笔**。它证明口径对齐，不证明策略可交易。
   R5 的容量、库存、制度期限制一条都没被这次演练解除。
2. **`p_win` 是 100% 先验**（常数 0.50）。这批卡的 Brier 分数衡量的是先验，
   不是策略。在 `n_eff` 起来之前，账本的 `skill` 字段没有信息量。
3. **`allow_prior=true` 是我显式打开的**，生产是 `false`。第 1 节的数字来自
   打开后的路径，B1 演示的是关掉后的路径。两者不是同一次运行。

---

## 4. 补丁已落地（B2、B5）

2026-11-13 12:45 修完，两处都是守卫性质，不碰生产数据。

| 改动 | 文件 | 内容 |
|---|---|---|
| B2 透传 | `backend/app/decision/producer.py` | `produce_card(fill_mode=...)` 显式透传给 `ledger.build_card`；默认仍是 `limit`（既有卡承诺哈希不变），新口径由调用点显式传 |
| B5 守卫 | `backend/app/decision/bars.py` | 新增 `day_last_quote_ts()`：按分区 mtime 缓存，返回 `HH:MM:SS` 或 None（三态第三态） |
| B5 守卫 | `backend/app/decision/settle.py` | 新增 `PENDING_ENTRY_NOT_CLOSED` 与 `entry_session_state()`；`_settle_pick` 在撮合之前判断，非 `closed` 一律挂起、不写盘 |
| 回归 | `analysis/_diag_l1_loop_walk.py` | 删掉临时绕行函数，改为直接调 `produce_card(fill_mode=...)` |

**回归结果：数字逐位不变。**

| | 补丁前 | 补丁后 |
|---|---:|---:|
| `open_only` 成交 / 均值 | 42 / +118.698bp | **42 / +118.698bp** |
| `open_only` max\|diff\| | 0.005bp | **0.005bp** |
| `limit` 成交 / 均值 | 70 / +108.178bp | **70 / +108.178bp** |
| `limit` max\|diff\| | 915.3053bp | **915.3053bp** |
| `pending_reasons.entry_not_closed` | —（无此键） | **0**（没有误伤） |

差异只在 `pending_reasons` 多了一个恒为 0 的键——这是同一批卡的**可观测性**变多，不是样本变少。

**守卫实测**（`entry_session_state` 逐日）：

| 日期 | `quote_ts` | 判定 |
|---|---|---|
| 2026-11-06 | 17:47:48 | closed |
| 2026-11-09 | None（整列为空） | **closed**（过去的交易日，缺列不算盘中） |
| 2026-11-10 | None（整列为空） | **closed** |
| 2026-11-11 | 16:58:26 | closed |
| 2026-11-12 | 21:10:52 | closed |
| 2026-11-13 | 10:42:47 | **intraday → 拒绝** |

新增测试（`backend/tests/test_decision_producer.py`，3 条断言链）：
`test_produce_card_passes_fill_mode`（透传 + 默认值不变）、
`test_settle_refuses_entry_that_is_not_closed`（unknown / 过去+盘中残留 / 收盘定稿 三支）。

`tests/ -k "decision or l1_cohort or paper"`：545 passed / 16 failed。
16 条全部是**既有红**，逐条归因：封签漂移 7 条（`scripts/probe_*.py`，SEALED 清单
不含本次改的三个文件，故不重签）、`test_l1_cohort_curve` 自身 `NameError: CC`（6 条）、
analysis 侧既有读数债务（3 条，报的是别的文件）。改动前同口径是 19 条，修掉 3 条
（`number_provenance` 报的正是 `bars.py` 与测试 docstring 里我新写的读数）。

---

## 5. 还没修的（只剩 B6）

| 断点 | 为什么这次不动 |
|---|---|
| ~~B1 冷启动~~ | **已完成**（第 7 节）。回放十个决策日，先验只出现在真看不到证据的两天 |
| B3 L1 接 producer | 要新增 `preferences` 字段（`decision_auto_fill_mode`）并改 `scan_picks` 的分支，属于口径决策，不是守卫 |
| B4 卡 → 订单 | 新增 `paper.from_card()`，第一个会动到资金的模块，得先有 B1/B3 的证据链 |
| B6 cohort 覆盖 | `fwd=shift(-20)` 是研究口径的定义，不是缺陷。只能声明边界，不能修 |

---

## 6. 下一步（按"接通"的代价排序）

| # | 动作 | 修哪个断点 | 代价 |
|---|---|---|---|
| 1 | ~~`produce_card` 增 `fill_mode` 并透传~~ | ~~B2~~ | **已完成** |
| 2 | ~~`_settle_pick` 加入 entry 日收盘校验~~ | ~~B5~~ | **已完成** |
| 3 | ~~回填历史卡并结算，让 `n` 自然过 8~~ | ~~B1~~ | **已完成**，见第 7 节 |
| 4 | ~~`paper` 侧新增 `from_card(card_id)`~~ | ~~B4~~ | **已完成**，见第 9 节 |
| 5 | L1 的 producer 入口 + `decision_auto_fill_mode` 配置 | B3 | 新分支 |

账本已经有 10 张卡 / 200 行结算，`n_eff=10`；再跑二十个决策日才有分母。
第 4 步（卡 → 订单）之后才有第一笔真实回合。

---

## 7. B1 已做：账本第一次有内容，也第一次有读数

**动作**：`scripts/backfill_l1_cards.py --apply`，回放 2026-10-29 .. 2026-11-11
十个决策日。这不是「打开 `allow_prior` 就算了」——那样每张卡都是常数。
而是按时间顺序回放，让先验只在**真的看不到证据**的那两天出现。

| as_of | 可见结算 | p_win 来源 | p_win | prior 权重 |
|---|---:|---|---:|---:|
| 2026-10-29 | 0 | prior | 0.5000 | 1.00 |
| 2026-10-30 | 0 | prior | 0.5000 | 1.00 |
| 2026-11-02 | 15 | **ledger** | 0.4571 | 0.57 |
| 2026-11-03 | 27 | ledger | 0.4681 | 0.43 |
| 2026-11-04 | 36 | ledger | 0.4286 | 0.36 |
| 2026-11-05 | 44 | ledger | 0.3906 | 0.31 |
| 2026-11-06 | 55 | ledger | 0.4667 | 0.27 |
| 2026-11-09 | 69 | ledger | 0.4831 | 0.22 |
| 2026-11-10 | 77 | ledger | 0.5155 | 0.21 |
| 2026-11-11 | 81 | ledger | 0.5248 | 0.20 |

**结果**：10 张卡 / 200 行结算（成交 97 / 未成交 103），`verify_card` 全部自洽，
`fill_mode` 全为 `open_only`，`horizon` 全为 0，`backfill=True` 可事后筛除。

### 第一次读数是负面的

| basis | n | 命中 | Brier | 常数 | skill | lcb | n_eff | 结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| median | 97 | 0.5155 | 0.2605 | 0.2500 | **−0.0418** | −0.0856 | 10 | NO_INFORMATION |
| abs | 97 | 0.5155 | 0.2638 | 0.2653 | +0.0055 | −0.0509 | 10 | BEATS_CONSTANT |

`median` 口径（L1 自己的 `pwin_basis`）的 skill 是**负的**：用账本算出的 p_win
反而不如常数 0.5。原因不是策略不行，是**样本还不够**——`n_eff=10`（十个决策日，
持有期让相邻日高度相关），`significance=UNKNOWN` 是设计上的三态，不是失败。

要让它有分母，需要 `n_eff ≥ MIN_NEFF`（约 30 个独立决策日）。按每天一个新决策日，
还要再跑二十个交易日。

**这条读数的价值在于它是负的**：它证明账本不会自动给出好结论。一个从空账本
回填出来的批次，如果第一天就报正 skill，那才是坏事。

---

## 8. 回填顺带查出第三个口径缺陷：median 基准的入场价

`_median_ret(data_dir, entry, exit)` 原来只用 `close`：基准 = 全市场
`close(exit) / close(entry) − 1`。而 L1 的收益是 `close(entry) / open(entry) − 1`。

`horizon_sessions=0` 时 `entry == exit`，于是基准恒为 `close/close − 1 = 0`：

| 日期 | 账本用的基准 | 实际 `median(open→close)` | 差 |
|---|---:|---:|---:|
| 2026-11-06 | 0.0bp | +91.1bp | 91bp |
| 2026-11-10 | 0.0bp | +16.8bp | 17bp |
| 2026-11-12 | 0.0bp | **−164.0bp** | 164bp |

后果：`outcome_med = (ret > median_ret)` 退化成 `(ret > 0)`，**`median` 口径静默
等价于 `abs` 口径**。实测 97/97 行两个因变量完全相同——这是它唯一的破绽，
而破绽只在「两种检验必须给出不同答案」时才显形。

**修法**：`_median_ret` 增 `entry_price` 参数，由 `_settle_pick` 按成交依据传
（`basis=OPEN` ⇒ `open`），结算行写 `median_basis`。修后 97 行里 87 行同、
10 行不同，`mean(median_ret)` 从 0 变成 32.3bp。

> v1 账本（错误口径）保留在 `data/user_data/decision.backfill-v1`，
> 供对比；生产账本用正确口径重建了一次。

### 修完之后才暴露的第二件事

`scripts/verify_decision_replay.py` 是那条链的**独立实现**：它不复用
`app.decision.bars` 的任何一行，从原始 parquet 重算成交价、出场价、收益和
全市场中位数，逐项与主路径比对。改完主路径后它立刻红了：

```
max_median_diff_bp = 278.787748   (> tolerance 1.0)
```

原因不是主路径错了，是**第二路径犯了同一个错**：它也拿 `raw_close(entry)`
当基准起点，而成交是 `open(entry)`。两边原来**一致地错**，所以一直是绿的。

这是这一轮最值得记的一课：

> **独立实现能证明「两条路一样」，不能证明「这条路对」。**
> 当两份实现共享同一个错误假设时，一致性反而是最强的假阳性。

修法：`median_from_raw` 增 `entry_price`，按 `fill.basis` 传（`OPEN` ⇒ `open`，
反推 `raw_open = open × raw_close / close`）。修完 `max_median_diff_bp` 落到
`0.000175`，`ok: true`——两条路径在**正确**的口径上一致。

顺带修了 `test_decision_replay_sandbox` 的 4 条断言（它们之前是在错口径上绿的）。

---

## 9. B4 已做：卡 → 订单桥

`paper.create_order` 有三个调用点（手动 API、自定义执行、监控规则），**没有一个
读决策账本**。所以账本能记十张卡两百行结算，纸面账户却零成交。

新增 `backend/app/decision/execute.py`，三件事：

| 部件 | 作用 |
|---|---|
| `paper.create_order(limit_price=...)` | 条件成交价：买单要求撮合价 ≤ 它。**`next_open` 原本无条件按开盘价成交**，会把「高开走弱」一起买进来 |
| `execute.from_card(card_id)` | 把 picks 变成 `next_open` 限价买单；`source=card:{id}` 保证幂等 |
| `paper._close_rounds()` | `settle_day` 撮合完当日买单后，卖出**等量老股**（`lot.date < day`），把回合闭合 |

### 两条硬守卫

1. **历史卡不许执行**：卡里带 `forecast.entry_date`，早于今天就拒绝——否则会拿
   今天的开盘价去成交一个月前的判断。`entry_date` 缺失（周五盘后，下一交易日
   分区还没落盘）不拒绝，记为警告。
2. **买入量由库存约束**：`min(预算折算股数, 可卖老股)`。预演实测过不约束的后果：
   底仓 700 股、按预算买 800 股 ⇒ 平仓时 `avail < qty`，整笔不平，持仓从 700
   变 1500。那不是「少赚」，是策略变成了另一个。

### 真实价格上的端到端预演

底仓在 11-11 开盘建（5 只），11-11 建卡（entry=11-12），11-12 撮合：

```
底仓成交: 5   持仓: 000530:1800 000678:1000 001219:700 002086:4300 002724:1500
下单: 4   跳过(小): 0   失败: 0
11-12 成交: 1   过期(高开): 3   平仓: 1   缺库存: 0
  001219.SZ: buy 700 @ 22.2811 / sell 700 @ 23.0984
最终持仓: 与起始逐只相同（700 → 700）
回合收益: +366.8bp（单笔，不代表策略）
```

**持仓逐只不变**是这一节唯一重要的读数：买卖配对、底仓不被消耗、回合真的闭合了。

### 接线（默认关）

`run_decision_stage` 新增 `decision_auto_execute`（默认 `False`）。开之前必须确认
`decision_auto_strategy_id` 指向真的要执行的那条腿——当前默认值 `trend_breakout`
在三百多天实测里是负的。

顺带修了配置层一个阻塞：`decision_auto_horizon` 的下限原是 1，而 L1 的回合口径
是 0（`ledger.build_card` 明确把 0 定义为「入场日收盘出场」）。钉在 1 会让
「持有 1 天」冒充「回合」——多隔的那一夜实测是负的。

### 它不做什么

**不假装能日内平仓。** 没有底仓时它连买单都不下（`n_skipped_no_inventory`），
不成交也不造一个不可实现的日内交易。所以这里产出的是**执行证据**（成交价、
成交率、涨跌停与高开拒绝），PnL 仍由 `decision.settle` 的回合结算负责。

---

## 10. B3 已做：L1 接进生产者，配置已切换

### 代码

`run_decision_stage` 增一条 L1 分支：`strategy_id == l1_gap_reversal` 时不走
`scan_picks`（L1 的候选不是策略引擎的产物），直接调 `l1_cohort.candidates()`。
分支里两件硬事：

1. **`fill_mode` 被强制成 `open_only`，不受配置影响。** L1 的候选就是「开盘 ≤
   限价才买」，用 `limit` 口径结算等于换策略（多出来的是「高开走弱」）。
2. **盘中不许建卡。** `candidates()` 用的是 as_of 当天分区的 `close`，而日线
   分区在盘中也会被实时路径写入。拿「截至此刻」的排名当收盘排名，错名单一旦
   封存就是永久证据。实测（13:45）：

   ```
   status=skipped_not_closed
   as_of=2026-11-13 不是收盘数据（quote_ts=10:42:47 早于 15:00:00，是盘中快照）
   ```

   账本仍 10 张卡、结算 written=0——守卫是拦在**建卡之前**的。

### 配置（已切换到生产）

`data/user_data/preferences.json`（改前已备份 `preferences.bak-20261113-1345.json`）：

| 键 | 改前 | 改后 |
|---|---|---|
| `decision_auto_strategy_id` | `trend_breakout` | **`l1_gap_reversal`** |
| `decision_auto_horizon` | 20 | **0**（回合，不是持有 1 天） |
| `decision_auto_fill_mode` | （无） | **`open_only`** |
| `decision_auto_execute` | （无，默认 false） | **true** |
| `decision_auto_account_id` | `default` | **`acc_mw2bc7suc`** |

### 底仓（已下单）

`seed_inventory` 用 11-12 的 20 只名单、每只 2.5 万，**19 张 `next_open` 买单**
已挂（`688836.SH` 一手 6.5 万，2.5 万买不到一手，被拒）。它们在下一个交易日
开盘成交，成交后才成为可卖的老股。

账户 `acc_mw2bc7suc`：现金 100 万未动（买单还没成交）。账户目录改前已备份。

### rebuild 已完成（2026-11-13 13:58）

服务跑在 Docker 容器里，`docker-compose.yml` 只挂 `./data` 与 `tiers.yaml`，
**代码是烘焙进镜像的**。这就是为什么 `data/logs/*.log` 里「决策账本」出现 0 次：
宿主机上的 Step 2.9 从来没进过容器。

```
docker compose build   → Image tick-stock-panel-app Built
docker compose up -d   → Container TickFlow_Stock_Panel Started
```

容器内实测（`/app/.venv/bin/python`，不是系统 python）：

| 检查 | 结果 |
|---|---|
| `/app/app/decision/execute.py` | 存在，232 行 |
| `producer.py` 里 `_l1_picks` | 命中 2 |
| `skipped_not_closed` | 命中 1 |
| `L1_STRATEGY_ID` | `l1_gap_reversal` |
| `SOURCE_PREFIX` | `card:` |
| 容器内读到 `decision_auto` 配置 | `strategy=l1_gap_reversal, horizon=0, fill_mode=open_only, execute=True, account=acc_mw2bc7suc, allow_prior=False` |
| `daily_pipeline.py` 调 `run_decision_stage` | 第 916 行 |
| 服务健康 | `GET /` → HTTP 200，`Application startup complete` |

> 验证时踩了一个坑：`docker exec ... python` 用的是系统解释器（没有 numpy），
> 应用用的是 `/app/.venv/bin/python`。用错解释器会把「环境缺失」误读成「代码没进去」。

### 生效后的预期时序

| 时点 | 会发生什么 |
|---|---|
| 今天 15:35 | 管道跑到 Step 2.9：建 11-13 的卡。周五盘后下一交易日分区未落盘 ⇒ `entry_date` 为 None，`from_card` 记 warning 后照常下 `next_open` 单 |
| 周一开盘 | 底仓 19 单 + 卡单同批撮合。`lot.date == 周一` 不满足 `< 周一`，所以**周一只买不卖**（`round_unfunded` 会记数） |
| 周二起 | 底仓成为老股，**完整回合开始**（买入 + 卖出等量老股） |

「周一只买不卖」不是缺陷，是「先有库存才有回合」的真实时序。

---

## 11. 复现

```bash
# 正确口径（对账用）
backend/.venv/Scripts/python.exe analysis/_diag_l1_loop_walk.py --fill-mode open_only

# 自动路径默认口径（对照）
backend/.venv/Scripts/python.exe analysis/_diag_l1_loop_walk.py --fill-mode auto

# 冷启动真实行为（生产配置 allow_prior=false）
backend/.venv/Scripts/python.exe analysis/_diag_l1_loop_walk.py --as-of 2026-11-05 --no-prior --no-research

# 盘中快照守卫
backend/.venv/Scripts/python.exe analysis/_diag_l1_loop_walk.py --as-of 2026-11-12 --fill-mode open_only --no-research
```
