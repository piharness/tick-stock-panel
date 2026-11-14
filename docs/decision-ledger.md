# 决策账本：Capital-Console 融合说明

> 状态：已实现（`backend/app/decision/`、`/api/decision/*`、`/decisions` 页面）
> 判定原则：**搬不变量，不搬模块**。本文记录哪些留下、哪些丢掉、以及为什么。

## 0. 一句话结论

`Capital-Console` 的价值不在它的 22 个模块，而在它用代码强制执行的六条不变量；
`tick-stock-panel` 已经有更好的数据层、执行层和界面，缺的正是这六条。
所以融合 = **把不变量接进 TSP 的现有数据与账本**，而不是把两个仓库拼在一起。

## 1. 两个系统各自的真实缺口

| | 强 | 弱 |
|---|---|---|
| **TSP** | 数据管线（Polars/分区/插件化）、策略与回测、paper 撮合（T+1/涨跌停/除权/多账户）、3000+ 测试、React 界面、开放 API | 能算"回测里赚了多少"，答不出"**我下过的判断，到期后比常数预测强吗**"；没有预登记，没有三态门禁，没有校准结算 |
| **Capital-Console** | 预登记 + 内容哈希、K1–K4 三态门禁、Brier 校准与常数基线、失败关闭、只追加账本 | 自带一套 `Data/` 与 `config/` 工作区、junction 单源、真钱 `capital_cny` 账户、独立 Web 控制台、Python 3.12 + PowerShell 启动器；`book/orders/tickets/broker` 与 TSP 的 `strategy/paper.py` 功能重叠且更弱 |

结论：**重叠的部分丢 CC 的，缺失的部分搬 CC 的**。

## 2. 菁 / 芜 判定表

| Capital-Console | 判定 | 在 TSP 的落点 / 理由 |
|---|---|---|
| `guard.py` 三态门禁 | **菁 → 移植** | `app/decision/gates.py`。三态是整套体系的地基：没有 UNKNOWN，"读不到"就会变成"没触发" |
| `calibration.py` 结算 + Brier | **菁 → 移植** | `app/decision/settle.py`。改成 Polars + TSP 分区，去掉 pandas 与 `Data/` 硬编码 |
| `policy.py` 承诺/观测二分 | **菁 → 降级保留** | TSP 无 Principal 私钥，不冒充签名。承诺 = 内容哈希（card_id），观测 = gates 单独哈希 |
| `preflight.py` 失败关闭 | **菁 → 语义保留** | 不新起服务，改为「门禁 UNKNOWN 不放行 + 建卡遇 STOP 返回 409」 |
| `kernel/` 38 指标 / 22 结论 | **芜（对本仓库）** | 绑定另一个研究项目的数据与口径。只借"结论必须带 falsifier"的 schema 思想，不搬数据 |
| `book.py` / `orders.py` / `tickets.py` / `broker.py` | **芜（重复）** | TSP `strategy/paper.py` 已覆盖且更强（T+1、涨跌停排队、除权、多账户、append-only 台账） |
| `web.py` + `web/index.html`（1452 行） | **芜（重复）** | 改由 React `/decisions` 页面承接，复用全站主题与组件 |
| `config/*.yaml` + junction 工作区 | **芜（第二套真相）** | 全部落 `data/user_data/decision/`，与 paper 账户同盘同源 |
| `capital_cny` 真钱账户 | **芜（越界）** | TSP 明确不做资金通道；K1 改用 paper 账户回撤 |
| `STATE.json` / `TASK.md` / `redteam/` | **芜（流程）** | 那是单人研究项目的交接装置，不是产品代码 |
| `simulate.py` / `panel.py` / `sizing.py` | **暂不移植** | 与 `backtest/position_sizing.py`、`robust_optimizer.py` 职责重叠，先验证账本闭环再谈组合优化 |
| `consensus.py`（ARM/CONFIRM/ENTER） | **暂不移植** | 是特定策略腿的状态机；账本层应先于策略层落地 |

## 3. 移植过来的六条不变量

1. **预登记**：卡在结果之前封存，`sha256(承诺内容)` 即 `card_id`。改一个限价 = 另一张卡，不是"改过的旧卡"。
2. **三态**：`PASS / TRIGGERED / UNKNOWN`。`strict=True` 时 UNKNOWN 也阻断。
3. **接线才算数**：`gates.py` 里出现的每条规则都真的读数据；读不到返回 UNKNOWN，不返回 PASS。
4. **可证伪**：`Brier skill = 1 - brier / brier_const`。基线的取法决定这条路走不走得通
   （见第 9 节）：`abs` 用**展开 climatology**——第 d 天只用严格早于 d 的已结算样本估常数
   （含 κ=20 收缩），决策时点可知，不含未来信息；`median` 用精确的 `0.5`。
   两个都不是样本内命中率：对任意常数 `p`，`mean((p−y)²) ≥ mean((ȳ−y)²)`，
   拿 `ȳ` 做分母会让任何常数预测器的 BSS 恒 ≤ 0——那是恒等式，不是检验。
5. **失败关闭**：建卡时门禁为 STOP ⇒ HTTP 409；确需补登历史卡必须 `allow_stop=true` + ≥10 字理由，理由随卡存档。
6. **只追加**：卡文件不覆盖（同 id 返回 `created: false`），结算写 `settlements.jsonl`；没有删除端点。

## 4. 口径（改这些之前先改测试）

| 项 | 口径 | 理由 |
|---|---|---|
| 限价撮合 | 不复权价。开盘 ≤ 限价按开盘；否则最低 ≤ 限价按限价；否则 `NOT_FILLED` 留痕 | 与 `strategy/paper.py` 一致，且"看得见买不到"本身是策略的一部分 |
| `raw_open` | `open / (close / raw_close)` | enriched 只落 `raw_close/raw_high/raw_low`；同一日复权因子对 OHLC 一致 |
| 收益 | 复权价：`exit_close / (fill_price × f_entry) - 1` | 除权日不产生假跌幅 |
| 超额 | 同区间基准（默认 `000300.SH`，`kline_index_enriched`） | 不比基准的收益不是 alpha |
| **选股超额** | **`ret - 同日全市场收益中位数`** | **基线精确 50%，同日 pick 近似独立；剥离 beta** |
| 到期日无行情 | 仍 `PENDING`，不拿旧价假装结算 | 停牌不是"没发生" |
| 结论门槛（`significance`） | 已结算 `n < MIN_N` ⇒ `INSUFFICIENT`；`n_eff < MIN_NEFF` 或不可靠 ⇒ 不显著 | 小样本的 Brier 只是噪声 |
| **判决（`verdict`）** | `n < MIN_N` ⇒ `INSUFFICIENT`；`n_eff < MIN_NEFF`（或不可靠）⇒ `UNKNOWN`；`skill_lcb > 0` ⇒ `BEATS_CONSTANT`；`skill_lcb ≤ 0` 且 `skill > 0` ⇒ `NOT_ESTABLISHED`；`skill < 0` ⇒ `KILL`；`skill == 0` ⇒ `NO_INFORMATION` | **与 `significance` 同轴**——点估计为正而下界为负不是「有戏」，点估计为负才是「已证伪」，恰好等于基线是「换预测器」。三者下一步不同，不能共用一个名字 |
| `verdict` 的唯一产地 | `settle.verdict_of()`；总分与 `by_source` 分组都调它 | 分组另写一份 `skill > 0`，会让 `n_eff` 最小的一组替整份账本下结论 |

## 5. 与 Capital-Console 的有意差异

| CC 的做法 | 本实现 | 为什么改 |
|---|---|---|
| 无决策文件 ⇒ K3 TRIGGERED | 空账本 ⇒ K3 PASS | TSP 用户可能从不使用账本；"未声称决策"不是"记录造假" |
| 签名（Principal 私钥） | 内容哈希 | 没有密钥就不要假装有；哈希能证明"未被改写"，不能证明"谁写的"，如实标注 |
| `capital_cny` 真钱账户 | paper 账户 | TSP 不做资金通道 |
| 门禁 STOP 时 `decide` 直接不出卡 | API 409，可显式越权并留痕 | 补登历史卡做校准是合法需求，但必须留下可读理由 |
| `asym2.pit.freshness` 外部新鲜度 | 日K分区目录 | 不引入第二个数据真相；没有数据的那天就不是交易日 |

## 6. 验收标准（可证伪）

- [x] 改动任一承诺字段 ⇒ `card_id` 变化；事后改卡 ⇒ `verify_card` 返回 False（测试 `test_verify_detects_post_seal_edit`）
- [x] 门禁在数据过期时判 `TRIGGERED` 而非 PASS（`test_gate_stops_when_data_is_stale`）
- [x] 缺 instruments 时 K2 判 `UNKNOWN`，`strict=True` 下阻断（`test_gate_k2_unknown_when_instruments_missing`）
- [x] 同一批卡重复结算不重复写入（`test_settle_is_idempotent`）
- [x] 复权因子 2 的场景下收益为 +10% 而非 +120%（`test_adjusted_return_removes_exrights_jump`）
- [x] 常数预测（`p ≡ 0.5`、`median` 口径）的 skill = 0、判 `NO_INFORMATION`；`n < MIN_N` 判 `INSUFFICIENT`
- [x] `verdict` 与 `significance` 同轴：点估计再高，`n_eff < MIN_NEFF` 也只能是 `UNKNOWN`
      （`test_high_skill_below_min_neff_cannot_beat_constant`）
- [x] 六态是分区：互斥、穷尽、每格可达（`test_six_verdicts_are_all_reachable`）
- [x] 分组判决走同一个谓词，不许比总分更乐观（`test_breakdown_verdict_shares_the_same_predicate`）
- [x] 旧判据装回去必须让新用例变红——否则那条断言是恒真的
      （`test_old_predicate_would_have_said_beats`）
- [x] 真实数据只读验证：门禁 0.01s、常数基线 0.06s（6 个采样日 / 33,090 个观测，p0 = 35.69%）

**已补上（2026-11-07 更新）**：「真实策略信号 → 卡」的自动生产者已实现——
`app/decision/producer.py`（0cdf8d3）接入盘后管道（`daily_pipeline` Step 2.9，
软失败不阻断）与 `POST /api/decision/produce` 手动触发；路径 = 结算到期卡 →
跑策略取 picks（复用 `StrategyEngine.run` 唯一执行入口）→ `auto_pwin` 反推 →
门禁 → 封卡。默认关（`decision_auto` 偏好，须显式开启）：自动给不会真实执行
的策略发卡会污染校准，发卡对象必须显式选择。
`p_win` 的自动来源已实现（`app/decision/pwin.py`，账本自举），但**它只在 `median`
口径下成立**——见第 9 节。`abs` 口径下这条路在数学上走不通，不是工程量问题。

## 7. 非目标

- 不接券商、不做实盘、不提供资金通道（与 TSP 既有边界一致）。
- 不做组合优化 / Kelly 仓位（`sizing.py` 暂不移植）。
- 不引入 `config/*.yaml` 或第二套数据目录。
- 不追求"每个策略都自动出卡"——先让账本可信，再谈自动化。

## 8. 下一步（按代价排序）

1. ~~**p_win 的真实来源**~~ → 已实现 `pwin.py`（账本自举 + 样本外重放），
   但**必须先读第 9 节**：`abs` 口径下这条路结构性地走不通。
   样本外重放的可见性口径已于 **9.8** 修正（原来偷看了未结算的样本）。
2. ~~**策略 → 卡的生产者**~~ → 已实现 `app/decision/producer.py`（0cdf8d3）：
   盘后管道 Step 2.9 + `POST /api/decision/produce` 手动触发；门禁 STOP 只跳过
   不越权（定时任务无 `allow_stop` 特权），无证据不编 p_win（`skipped_no_evidence`）。
   默认关，须显式开启 `decision_auto` 偏好。
3. **组合层**：把 `sizing.py` 的 E[log] 仓位接到 `paper` 的下单金额上，但必须先有
   `n ≥ 8` 且 `significant=true` 的校准证据。

---

## 9. p_win 的来源：这个问题必须先定口径，再定方法

> 本节记录六次实测的结论。全部脚本在 `scripts/probe_*.py` / `verify_pwin_e2e.py`，
> 数据为 `data/kline_daily_enriched`（365 个交易日，2025-05-07 .. 2026-11-04）。

### 9.1 先说结论

**"用哪个历史命中率、带多少样本"这个问法假设了 p_win 有唯一正确答案。
实际上答案由计分口径决定，而两个口径给出的结论相反。**

| | `abs`（当前默认） | `median`（新增） |
|---|---|---|
| 因变量 | `ret > 0` | `ret > 同日全市场中位数` |
| 常数基线 | 展开 climatology（决策时点可知，§9.10 修正） | 精确 `0.5`（无未来信息） |
| 有效样本 | 365 天 / 20 日 horizon ≈ **18 个不重叠窗口** | ≈ pick 数（ICC ≈ 0） |
| 基线估计误差 | 展开估计，随历史长度收敛 | 0 |
| 历史胜率能否当 p_win | 能，但增量极小（§9.10） | 能，但增量极小（§9.10） |

> §9.10 推翻了本节原先「abs 走不通、median 走通」的二分。**两个口径的
> `verdict` 都是相对 0.5 的产物**，而 0.5 不是任何一个策略的基础胜率。
> 换成展开基线后，两个口径给出同一个结论：`NO_INCREMENT`。

同一批 386 条真实结算（40 个决策日、高换手策略）：

```
[abs]    n=386  hit=17.36%  skill=-7.00%   lcb=-21.22%  NO_INFORMATION
[median] n=386  hit=19.95%  skill=+33.16%  lcb=+17.23%  BEATS_CONSTANT
```

不是数据打架，是两个问题：**赚没赚** vs **选得准不准**。

### 9.2 六条实测

| # | 主张 | 实测 | 脚本 |
|---|---|---|---|
| 1 | `p0` 不是常数，是**采样窗口的函数** | 同一段数据：`sample=6` ⇒ **35.69%**（与旧记录逐位一致）、`sample=12`（代码默认）⇒ **42.03%**、`sample=24` ⇒ **48.16%**。跨度 12.5 个百分点，只因为取了几个点 | `probe_p0_drift.py` |
| 2 | `p0` 的精度被高估 31 倍 | 345 个起点的日间 sd = **21.37%**；naive SE 0.037% vs 按日聚类 1.15% | `probe_p0_precision.py` |
| 3 | 绝对收益的有效样本是「天」不是「观测」 | 3.3 万个观测只有 **18 个不重叠窗口**；要拿到 ±2% 需要 **36 年** | `probe_p0_precision.py` |
| 4 | 超额口径下 pick 近似独立 | 同日 20 个 pick 的日胜率 sd = 16.96% vs 独立假设 11.18%… 随机组 11.13%，**ICC ≈ 0** | `probe_excess_outcome.py` |
| 5 | 历史胜率在强信号上样本外有效，弱信号上翻转 | 高换手：训练 +12.06% → 验证 **+23.12%**；20 日动量：+4.45% → **-4.19%** | `probe_window.py` |
| 6 | 窗口长度不敏感 | W = 40 / 80 / 160 / 320 / 全历史，skill 差异 **< 1 个百分点** | `probe_window.py` |

第 1 条要展开说，因为它解释了旧文档里的 35.69%：

```
sample=6   p0 = 35.69%  ± 16.91%  (95%)   ← 旧记录（6 采样日 / 33,090 观测）
sample=12  p0 = 42.03%  ± 11.96%  (95%)   ← 当前代码默认值
sample=24  p0 = 48.16%  ±  9.27%  (95%)
```

那 6 个采样点自身的 p 值是 `[20.1%, 26.3%, 70.5%, 51.5%, 30.3%, 15.4%]`——
**同一年里，20 日上涨占比从 15% 到 70%**。用 6 个这样的点去估"市场基础胜率"，
95% 区间宽达 ±17%，比"有信息"和"没信息"之间的差距还大。

`prior()` 现在会一并返回 `p0_se` 与 `p0_range`，前端显示成 `42.03% ±11.96%`。
`sample` / `step` 是任意参数，`p0` 随之漂移，所以它**不能承担"常数基线"的角色**——
这也正是 `median` 口径存在的理由：那里的基线是精确的 0.5，不需要估。

第 5 条是最重要的：**它说明 p_win 的来源本身需要被审计**。动量策略的历史胜率
不是"算错了"，而是"对未来没有预测力"。账本必须能说出这句话。

### 9.3 样本内常数基线为什么不能用（数学，不是数据）

> 原标题是「为什么 `abs` 口径下历史胜率注定失败」。§9.10 证明这个标题把
> 「基线错了」误读成了「口径走不通」——错的是基线，不是 `abs`。

`abs` 原来的基线是 `brier_const = mean((hit - y)²)`，其中 `hit` 是**该样本**的
命中率。对任意常数 `p`：

```
p = hit   ⇒ brier = brier_const  ⇒ skill = 0
p ≠ hit   ⇒ brier > brier_const  ⇒ skill < 0
```

而历史胜率**只能是常数**（每个策略一个数）。所以：

> **样本内常数基线 + 常数 p_win ⇒ skill ≤ 0，恒成立。**

这段数学没错，但它证明的是一个**恒等式**，不是发现：`hit` 是使平方误差最小
的常数，任何别的常数都打不赢它。拿它当分母，检验就变成了同义反复。

修正（§9.10）是把分母换成**展开 climatology**——只用决策时点之前已结算的
样本估常数。换完之后 `abs` 不再恒 ≤ 0，但同时暴露出 `median` 口径的同一个
问题：`0.5` 只是「随机选股」的基准，不是这个策略的基础胜率（实测 27.9%）。

**两个口径的 `verdict` 都只回答「比 0.5 好多少」，不回答「有没有预测力」。**
后者需要另一个检验：`verdict_incremental`。

### 9.4 p_win 的实现：账本自举

`app/decision/pwin.py`：

```
第 N 张卡的 p_win  ←  前 N-1 张已封存并结算的卡
```

- **冷启动**：`n < 8` ⇒ `p_win = None`，`source="prior"`，不编造数字；
- **收缩**：`p = (hits + κ·0.5) / (n + κ)`，`κ = 20`（等价 20 个虚拟样本），**不调优**；
- **元校准**：把历史按决策日重放，第 i 天只用第 i 天之前的样本估 p，
  算 Brier 改善的单侧 95% 下界。`lcb > 0` ⇒ `RELIABLE`，否则 `UNRELIABLE`；
- **失败关闭**：`suggest(require_reliable=True)` 在 `UNRELIABLE` 时抛错，
  API 返回 **409**。宁可不出卡，也不用一个已被证伪的概率污染账本。

这不是"外部数据源"，而是账本自己的历史。好处是天然 walk-forward：没有任何未来
信息能进入 p_win。

### 9.5 新增接口

| 端点 | 用途 |
|---|---|
| `GET /api/decision/pwin?strategy_id=&horizon=&basis=&window=&kappa=` | 某策略当前的 p_win 及其样本外证据 |
| `POST /api/decision/cards` `auto_pwin=true` | 用账本历史自动填 p_win；不可靠则 409 |
| `GET /api/decision/score?basis=median` | 超额口径计分 |
| `GET /api/decision/overview` | 同时返回 `score`（abs）与 `score_median` |

结算记录新增三个字段（向后兼容，旧记录读不到即为 `None`）：

```
median_ret    同区间全市场收益中位数
excess_med    ret - median_ret
outcome_med   1 if ret > median_ret else 0
```

### 9.6 验收标准（已通过）

- [x] 冷启动 `n < 8` ⇒ `p_win = None`，`source="prior"`，不返回数字
- [x] 8 个全胜样本 ⇒ `p_win = 18/28`，不是 1.0（`test_shrinkage_pulls_toward_prior`）
- [x] 稳定 70% 胜率 ⇒ `RELIABLE`，`oos_lcb > 0`
- [x] 前 20 天全胜、后 20 天全负 ⇒ `UNRELIABLE`，`oos_lcb ≤ 0`
- [x] 样本外重放不偷看当天结果（`test_walk_forward_uses_only_past`）
- [x] `median` 口径的 `brier_const` 恰为 0.25，不是样本命中率
- [x] 单日 20 个 pick ⇒ `days=1`，`skill_lcb=None`，`significant=False`
- [x] 旧记录缺 `outcome_med` 时 `abs` 口径照常可算（向后兼容）
- [x] 真实数据端到端：40 张卡 / 386 条结算 / `outcome_med` 覆盖 99%，
      `median` 口径 skill +33.16%（lcb +17.23%，HAC 修正后），`abs` 口径 -1.44%

### 9.7 这一节没有解决的问题

1. **`abs` 口径怎么办**：要么承认它只能回答"赚没赚"、不参与"有没有信息"的判断；
   要么给 `p_win` 加标的级区分度（需要因子暴露模型）。当前选择前者。
2. **非平稳**：动量策略的符号翻转说明"策略胜率"不是稳定参数。`UNRELIABLE`
   能识别它，但不能预测下一次翻转。这是统计的边界，不是实现的缺口。
3. **冷启动期**：前 8 张卡没有 p_win，只能用 prior。这是诚实的代价。

### 9.8 口径修正：重放必须只用**已结算**的样本（2026-11-04）

#### 问题

`walk_forward` 原来的回看是 `days[i-window:i]`——按**决策日**往前数。
但一张卡的出场日是 `as_of + 1 + horizon`（次日入场 + 持有 horizon 天）。
于是决策日 `D` 的 p 用到了 `D-1` 的胜率，而那个结果要等 20 个交易日之后才出来。

模块注释当时写着「没有任何未来信息能进入 p_win」，代码违反了这句话。
`scripts/probe_pwin_source.py` 的预登记口径反而是对的（`cutoff = pos[t] - HORIZON`），
库函数没有照它实现——**文档、探针、实现三者不一致时，以预登记为准**。

#### 修复

```python
exit.date < D              # 优先：结算记录里的真实出场日（精确）
或  D - d > horizon + 1    # 回退：没有 exit.date 时按决策日间隔保守判定
```

一天里只要还有一笔没结算完，这一天就整体不算可见（取 `max(exit.date)`）。

#### 实测（同一批 386 条结算 / 39 个决策日 / horizon=20）

| | 可评估决策日 | Brier 改善 | 单侧下界 | skill | 判定 |
|---|---|---|---|---|---|
| 修复前 | 37 | +0.08614 | +0.06506 | +34.45% | RELIABLE |
| 修复后 | **17** | **+0.12325** | **+0.09533** | **+49.30%** | RELIABLE |

**判定没变，但数字全变了。** 而且方向和直觉相反：修复后改善**更大**。
原因是修复前把冷启动期 `p≈0.5` 的那些日子也算进了评估，把均值拉低了。
所以「泄漏导致乐观」这个说法在这里是错的——泄漏的真实危害不是偏大或偏小，
而是**这个数字无法用决策当时已知的信息复现**。

这一点值得记住：如果当初用「修复后数字变大」来自证修复正确，那就又犯了
"用结果解释方法"的错。修复的依据是定义，不是它让指标变好看。

#### 附带的结构性结论

39 个决策日、horizon 20 ⇒ 只有 **17 天**可评估（其余 22 天的结果还没结算）。
`2 × horizon + 1` 是"有第一天可评估"的下界：要认证一个 20 日策略，
账本至少要有 **41 个决策日**，否则连"样本外"都凑不出来。
这条约束直接决定第 8 节第 2 步（策略 → 卡的生产者）的验收节奏：
**自动化跑满 41 个交易日后，p_win 的可靠性才有第一次可读的结论。**

#### 新增验证

- `test_walk_forward_cannot_certify_before_first_settlement`：20 天账本 + horizon 20 ⇒ `INSUFFICIENT`
- `test_walk_forward_honours_exit_date`：有 `exit.date` 时按真实出场日判定，不按间隔
- `test_walk_forward_does_not_see_the_regime_it_is_evaluating`：制度翻转后 p 仍停在旧制度
- `scripts/probe_pwin_availability.py`：并排跑「旧口径」与「预登记口径」，量化差异

三条测试都在「把可见性判定强制为 True」时失败——不是装饰性断言。
`reason` 现在写明「N 个可评估决策日」，`GET /pwin` 与建卡响应新增 `oos_days`。

### 9.9 结算必须记录 p_win 的出处

结算流水原来只存 `p_win` 这个数字，不存它从哪来。于是事后无法区分三种样本：

| 来源 | 含义 | 该不该算进「校准」 |
|---|---|---|
| `ledger` | 账本自举 | 该 |
| `prior` | 冷启动常数 0.5 | 不该——它的 Brier 恒等于基线，天然 skill=0 |
| `None` | 人手填的 / 旧记录 | 不该混入，至少要看清楚占多少 |

新增三个字段（旧记录读不到即为 `None`，向后兼容）：

```
p_win_source     "ledger" / "prior" / None
p_win_n          估这个 p 时用了多少已结算样本
p_win_verdict    当时的元校准结论（RELIABLE / UNRELIABLE / INSUFFICIENT）
```

`score()` 同时返回 `by_source` 分组。实测同一批 386 条：

```
[median] 合并      n=386  skill +33.16%  BEATS_CONSTANT
         ledger   n=376  skill +34.04%  BEATS_CONSTANT
         prior    n=10   skill   0.00%  NO_INFORMATION
```

这组数字差得不多（prior 只占 10 条），但它证明分组是通的。更重要的是
**方向**：如果冷启动期很长、或有人手工填了一批乐观的 p_win，合并分数会
把两种完全不同的证据混成一个无法解释的数。分组的代价是几行代码，
不分组则意味着「校准结论」可以被一段没有信息的历史稀释。

`scripts/verify_pwin_e2e.py` 现在也像 API 一样把出处写进 `forecast.pwin`，
否则它自己造出来的账本永远是 `unknown`——验证脚本绕过 API 时，必须补上
API 做的同一件事，否则验证的是另一条路径。

### 9.10 `RELIABLE` 是基线的产物，不是 p_win 的能力（2026-11-05）

#### 问题

§9.8 修好了可见性，但没检查**基线**。`walk_forward` 的 `bc` 用的是
`(prior - y)²`，而 `_prior_of()` 对两个口径都返回 `0.5`。于是当 p 接近常数时：

```
skill = 1 - p(1-p)/0.25 = 1 - 4p(1-p)
```

命中率 15% ⇒ **+49%**，28% ⇒ **+19%**。这个数字和「预测未来」无关，只和
「命中率离 50% 有多远」有关。§9.8 实测的 +49.30% 正好落在这个闭式解上
（该评估窗口命中率约 15% ⇒ 上限 0.1225，实测 0.12325）。

#### 四预测器对照（`scripts/probe_pwin_information.py`）

343 个决策日 / 3430 条观测，同一批样本上打分：

| 预测器 | abs Brier | 相对 0.5（下界） | 相对展开基线（下界） |
|---|---|---|---|
| A 近端 60 日胜率（现行 p_win） | 0.2002 | **+0.04979 (+0.04271)** | **+0.00156 (+0.00050)** |
| B 展开全历史胜率 | 0.2018 | — | — |
| C 常数 0.5（现行基线） | 0.2500 | — | — |
| D 全样本命中率（神谕常数） | 0.2004 | — | — |

`median` 口径同向且更极端：A−C `+0.04684 (+0.04042)`，A−B `−0.00026 (−0.00150)`。

三件事，其中第 2 条已被 §9.11 推翻：

1. **A 打赢 C 是必然的**（C 是错的基线）；
2. ~~A 打不赢 B（近端胜率不含额外信息）~~ —— 上表 A−B 的点估计是
   **+0.00156**，方向与这句话相反。这个符号与 `walk_forward` 的 −0.00548
   冲突，根因是两处 `window` 语义不同（§9.11）。口径统一后 A−B 为
   **+0.00155**，但 iid 下界 +0.00050 经重叠修正后变成 **−0.00078**。
   正确表述是「与 0 无法区分」，既不是打赢也不是打不赢；
3. **A 打不赢 D**——`A−D = +0.00015`，但下界 `−0.00146`。A 连「事后最优
   常数」都赢不了，说明它不只是「在估基础胜率」，而是「估得比事后平均
   还差一点」。

顺带确认两个口径是同一个变量：abs 命中率 27.87%、median 27.90%，
因变量一致率 **93.56%**；20 日全市场收益中位数均值 **−1.01%**，
阈值几乎重合。§9.1 说「不是数据打架，是两个问题」——数据上它们是同一个问题，
结论相反**全部来自基线**（一个用神谕常数，一个用 0.5）。

#### 修正：三个基线，两个判定

`settle.score()` 现在分开报告三个基线：

| 字段 | 含义 | 用途 |
|---|---|---|
| `brier_random` | `p = 0.5` | 无信息基准（median 口径的「随机选股」） |
| `brier_clim` | 展开 climatology | **诚实基线**：决策时点可知的最优常数 |
| `brier_insample` | 样本内命中率 | 事后最优，恒 ≥ 任何常数预测器，**只做诊断** |

`abs` 口径的 `brier_const` 改成 `brier_clim`；`median` 口径保持 `0.25`
（0.5 是定义性基准，不是估计值）。

`pwin.walk_forward()` 新增第二套判定，与现行 `verdict` **并列而非替代**：

```
verdict              这个 p 比「无信息常数 0.5」好吗？      → RELIABLE / UNRELIABLE
verdict_incremental  这个 p 比「同样诚实的历史常数」好吗？  → INCREMENTAL / MARGINAL / NO_INCREMENT
```

`MARGINAL` 是必须的第三档：`lcb > 0` 只说明「可分辨」，不说明「有用」。
实测 `abs` + `window=250` 给出改善 `+0.00011`、下界 `+0.00003`——若只按符号
判定会得到「显著但等于零」的 INCREMENTAL。`MIN_INCREMENTAL_EFFECT = 0.001`
（预测误差降低 0.1 个百分点）是这一档的门槛。

#### 一个必须写下来的数学事实

**不随决策日变化的预测器，相对展开基线的 Brier 改善恒 ≤ 0。**

因为常数的分辨率（resolution）恒为 0，Murphy 分解给出
`BSS_clim = (RES − REL)/UNC ≤ 0`。这有两个后果，必须分开说：

1. `estimate()` 返回的 `p_win` 是一个**标量**，它相对 climatology 的增量
   结构性地为 0。所以「卡上那个数字」本身不含超出「历史平均胜率」的信息。
2. `walk_forward` 里的 p 是**逐日重估**的，理论上可以赢 climatology。
   实测两个窗口都为负（−0.00237 / −0.00055）。**§9.11 推翻了本节的解释**：
   这个负号里有一块是「滚动窗口样本更少 ⇒ 方差更大」的机械项，不是信息量
   的度量。所以只能说「这批数据没能显示增量」，不能说「这条路走不通」。

因此「把基线换成 B 之后 RELIABLE 会变 UNRELIABLE」这个预期只对了一半：
它确实会变，但变的原因是**当前这个时变方式不含信息**，不是「这批数据
没有信息」。若把它当成唯一的 gate，「胜率稳定在 70%」这种明显有用的情形
也会被判死（`test_constant_predictor_has_no_increment_but_can_be_reliable`
钉住了这个反例）。

#### 实测（`scripts/probe_pwin_verdicts.py`，343 决策日 / 3430 观测）

| basis | window | skill vs 0.5 | skill vs clim | verdict | incremental |
|---|---|---|---|---|---|
| median | 60 | +0.1785 | **−0.0117** | RELIABLE | NO_INCREMENT |
| median | 250 | +0.1858 | **−0.0027** | RELIABLE | NO_INCREMENT |
| abs | 60 | +0.1707 | **−0.0271** | RELIABLE | NO_INCREMENT |
| abs | 250 | +0.1930 | +0.0005 | RELIABLE | **MARGINAL** |

`verdict_incremental` 对 `window` 敏感：窗口越接近全历史，p 与展开基线越像，
增量越接近 0。判断「近端是否含远端没有的信息」时，必须让 `window` 明显小于
历史长度——默认 250 在 343 天的账本上已经接近全历史。
**§9.16 修正**：窗口一旦覆盖全部已可见决策日，增量不是「接近 0」而是**恒等 0**
（两个估计量是同一个），此时检验没有功效，结论是 `NOT_TESTABLE` 而不是
`NO_INCREMENT`。

#### `days` / `oos_days` 的语义拆分

`walk_forward` 的 `days` 曾在失败分支是「总决策日」、在成功分支是「可评估日」，
而 `estimate()` 又把 `wf["days"]` 透传成 `oos_days`，于是「账本规模」被当成
「样本外天数」显示。现在：

- `days` **恒为**总决策日；
- `oos_days` **恒为**可评估决策日（失败时为 `0`，不再是 `None`）；
- `estimate()` 透传 `wf["oos_days"]`。

#### 验收

- [x] `test_days_and_oos_days_are_distinct`：20 天账本 + horizon 20 ⇒ `days=20, oos_days=0`
- [x] `test_constant_predictor_has_no_increment_but_can_be_reliable`：稳定 70% ⇒ `RELIABLE × NO_INCREMENT`（§9.16 把窗口从 250 收到 20：原来的断言是恒等式，不是结论）
- [x] `test_increment_detected_when_regime_drifts_and_window_is_short`：制度翻转 + 短窗口 ⇒ `INCREMENTAL`
- [x] `test_marginal_verdict_when_increment_is_significant_but_tiny`：`+0.00011/+0.00003` ⇒ `MARGINAL`
- [x] `test_score_abs_baseline_is_expanding_not_insample`：p=hit 时 `skill > 0`，旧实现恒为 0
- [x] 真实行情：`median` 口径两个 window 都返回 `NO_INCREMENT`（§9.8 的 `RELIABLE` 未被推翻，但不再被当作「有信息」的证据）

#### 这一节没有解决的

1. **结构性上限**：365 交易日 ÷ 20 日 horizon = **18 个不重叠窗口**。
   p=0.28 时标准误 10.6%，95% 区间 ±20.7%；要收到 ±5% 需要 309 个窗口 ≈ 26 年。
   「41 个决策日就有第一次可读结论」是对的，但**可读 ≠ 有功效**。
2. **重叠窗口的自相关**：`_daily_clustered_lcb` 只按决策日聚类，校正了同日
   横截面相关，**没有**校正 horizon 重叠导致的跨日自相关。正确做法是
   block bootstrap（block ≥ horizon）或 Newey-West。当前下界仍然偏乐观。
3. **出路只有一条**：把 per-strategy 标量换成 pooled 截面模型
   （全标的 × 全策略，n 从 3430 起），预测 20 日收益分布的某个分位，
   按 CRPS 打分，`p_win = P(ret > 0)` 作为派生量。账本退回审计与漂移监控。
   在那之前，`p_win` 只能当「一个比 0.5 好、但没有区分度的常数」使用。

### 9.11 两个口径错误互相抵消，§9.10 的结论要退回「测不出」（2026-11-05）

§9.10 的 A/B 对照跑在 `probe_pwin_information.py` 上，而 `walk_forward` 的
结论跑在 `pwin.py` 上。这两个实现**对同一个参数名 `window` 的理解不同**，
加上下界算法低估了标准误，于是两边给出符号相反的 A−B。逐个说。

#### 错误一：`window` 在 `estimate()` 与 `walk_forward()` 里不是同一个东西

- `estimate()`：取「最近 `window` 个**已结算**决策日」；
- `walk_forward()`（旧）：取「最近 `window` 个**日历**决策日，再过滤掉未结算的」。

持有 20 日的卡，最近 21 天都不可见。所以 `window=60` 在 walk-forward 里
实际只用到约 39 天（≈390 条观测），而 `estimate()` 的 `window=60` 是 60 天
（≈600 条）。后果有两层：

1. walk-forward 验证的**不是** `estimate()` 输出的那个预测器——同一个参数，
   两个模型；
2. `window ≤ horizon` 时一天都评估不出来。`window=20, horizon=20` 返回
   `oos_days=0`，而 `estimate(window=20)` 正常给出数字。

§9.10「A 打不赢 B」正压在这个歧义上：那个 +0.00156 用的是 60 个已结算日，
而 `walk_forward` 的 −0.00548 用的是 39 个日历日。**符号是口径造成的，不是
数据造成的。**

修法：`walk_forward` 也按「最近 `window` 个已结算决策日」取近端。改完后两个
脚本的 A−B 逐位一致（`+0.00155` vs `+0.00156`，差异来自 1 个可评估日的边界）。

#### 错误二：逐日观测不是独立样本

逐日改善量是 MA(horizon−1)：相邻决策日的收益重叠 19 天。实测自相关
lag1 = **0.65–0.84**，lag20 仍有 −0.05…+0.03。按 iid 算，标准误被低估
2.2–2.7 倍。

修法：`settle.hac_lcb()`，Newey-West（Bartlett 核），`lag = horizon - 1`，
`pwin._mean_se_lcb()` 与 `settle._daily_clustered_lcb()` 共用。新增：

```
n_eff    等效独立样本数 = n · (se_iid / se_hac)²     （321 天 ⇒ 44–66）
hac_lag  滞后阶数 = horizon - 1
```

#### 两个错误方向相反，只修一个会得到相反的假结论

| basis | window | 改善（旧→新） | 旧判定（日历窗口+iid） | 只修窗口（已结算+iid） | 两处都修（+HAC） | n_eff |
|---|---|---|---|---|---|---|
| median | 60 | −0.00237 → −0.00026 | NO_INCREMENT | NO_INCREMENT（−0.00150） | **NO_INCREMENT**（−0.00302） | 65 |
| median | 250 | −0.00055 → −0.00034 | NO_INCREMENT | NO_INCREMENT（−0.00046） | **NO_INCREMENT**（−0.00065） | 44 |
| abs | 60 | −0.00548 → **+0.00155** | NO_INCREMENT | **INCREMENTAL**（+0.00050） | **NO_INCREMENT**（−0.00078） | 66 |
| abs | 250 | +0.00011 → +0.00008 | **MARGINAL**（+0.00003） | NO_INCREMENT（+0.00001） | **NO_INCREMENT**（−0.00010） | 44 |

`abs/60` 这一行是关键：只修窗口，会得到一个「显著为正」的 INCREMENTAL；
只修标准误，会得到一个「显著为负」的 NO_INCREMENT。两个错误一个压低了
点估计、一个抬高了显著性，**互相掩护**。§9.10 的 `MARGINAL` 就是它们
刚好抵消到门槛附近的产物。

改完后全表四个配置都是 `NO_INCREMENT`，且下界全为负。
`MIN_INCREMENTAL_EFFECT = 0.001` 从来没有承重：`abs/60` 的改善
（+0.00155）已经越过 0.001，卡住它的是下界，不是门槛。

#### 更硬的一条：这个检验对滚动估计量有内置负偏

对任意一天，`d_i = (c_i − y)² − (p_i − y)² = (c_i − p_i)(c_i + p_i − 2y)`。
对 y 取期望（`E[y] = μ`）：

```
E[d_i] = (c_i − μ)² − (p_i − μ)²
E[d]   = [Var(c) + bias²(c)] − [Var(p) + bias²(p)]
```

`p` 只用 `window` 天、`c` 用全部历史，**`Var(p) > Var(c)` 是构造性的**。
所以任何 trailing-window 估计量相对展开基线都自带机械负偏，量级
≈ `Var(p) − Var(c)`，与「有没有信息」无关。

`scripts/probe_pwin_variance.py` 实测：

| basis | window | sd(p) | sd(c) | 机械项 | 实测改善 | 残差=信号 |
|---|---|---|---|---|---|---|
| median | 60 | 0.0470 | 0.0234 | −0.00208 | −0.00026 | +0.00182 |
| median | 250 | 0.0243 | 0.0234 | −0.000065 | −0.00034 | −0.00028 |
| abs | 60 | 0.0409 | 0.0151 | −0.00121 | +0.00155 | **+0.00276** |
| abs | 250 | 0.0155 | 0.0151 | −0.000002 | +0.00008 | +0.00008 |

**残差才是信号通道**：它等于 `d_i` 减掉机械项，若近端真的多含信息，应当
系统性为正。对它单独做 HAC 检验：

| basis | window | 残差 | HAC 下界 | t |
|---|---|---|---|---|
| median | 60 | +0.00182 | −0.00087 | 1.11 |
| median | 250 | −0.00028 | −0.00054 | −1.71 |
| abs | 60 | **+0.00276** | **+0.00013** | **1.73** |
| abs | 250 | +0.00008 | −0.00009 | 0.76 |

所以准确的说法不是「近端不含信息」，而是：

> 近端窗口可能有信息（`abs/60` 的残差 t=1.73），但它的量级（+0.0028）
> 和估计量自身的方差惩罚（−0.0012）同阶，净效应被抵消到测不出。

而且本节只测了 4 个配置（2 个口径 × 2 个窗口），其中只有 1 个 t>1.645；
纯噪声下出现至少一个的概率约 1−0.95⁴ ≈ 19%。**这条不能当作发现，
只能当作「不能排除」。**

#### 对两个待定问题的回答

1. **不能让 `suggest(require_reliable=True)` 接受 `NO_INCREMENT` 作为拒绝条件。**
   对滚动估计量它恒为负（机械项），接上就是永久停机；而且停机的理由
   （「没有增量」）与真实原因（「这个检验对这个预测器有内置负偏」）不是
   一回事。`verdict` 也不适合当 gate——它是「基础胜率离 50% 有多远」的
   单调函数，命中率 28% 的策略无条件通过，命中率 52% 的策略无条件拒绝。
2. **0.001 不是问题所在。** 修完口径后没有任何配置能靠它区分。

#### 如果还要继续这条路，检验该怎么改

要检测「近端是否多含信息」，必须让两个预测器的**估计方差相等**，
否则方差惩罚会盖过信号。可用的做法：

- 同窗口长度、不同信息集：`p1` 用最近 60 个已结算日，`p2` 用「随机抽 60 个
  已结算日」或「60 天前的那 60 天」——两者方差同阶，差值才是信息；
- 或者直接对残差序列做检验（本节表格），把机械项当已知量减掉。

#### 仍然没解决

- HAC 修正二阶矩，没有修正「p 与 c 由同一批 y 构造」导致的估计相关；
  残差的方差仍被低估。
- `n` 接近 `lag` 时（端到端账本只有 17 个可评估日 / 20 日持有期）NW 本身
  不可靠，下界仍偏乐观。这种账本的真实独立样本可能是 1–2 个，唯一诚实的
  读数是 `n_eff`。
- `n_eff` 44–66 说明账本的信息量比「321 天」少一个数量级。任何需要**条件**
  预测（按日、按标的、按制度）的模型都撑不起这个样本量——见 §9.12。

### 9.12 账本的职责边界：它是审计工具，不是训练集（未实现，下一步）

§9.11 把信息量算清楚了：321 个可评估日只等于 **44–66** 个独立样本；
真实账本（39 天 / 17 个可评估日 / 20 日持有期）只有 **1–2** 个。
经验上，可辨识的参数个数约为 `n_eff / 10`。

由此得到一条硬约束：

> **账本只够估一个标量。** 任何「按日、按标的、按制度」的条件模型，
> 都不能在这个样本上训练或验证。

这直接否掉了 §9.10 提的第 8 条出路（pooled 截面 + 分位数 + CRPS）作为
**账本**的延伸——那不是升级，是把模型容量加到数据撑不住的地方。

正确的分工是两层，各有各的样本：

| 层 | 数据 | 方法 | 评价 |
|---|---|---|---|
| 条件模型 | 全市场横截面（百万级 stock-day） | purged K-fold + embargo，防重叠泄漏 | CRPS / pinball，不是 Brier |
| 账本 | 实盘决策卡（几十条） | 只做审计与漂移 | 与回测分布的偏离，不是「有没有 skill」 |

账本要回答的问题因此从「这个 p_win 准不准」变成：

1. **审计**：实盘 p_win 的 Brier 是否落在回测分布内（按 horizon 分档）；
2. **漂移**：条件模型的实盘表现是否系统性偏离回测（CUSUM on daily Brier）。

这两件事在几十条样本上是**可做**的——因为它们检验的是一个已知分布的偏离，
不是一个未知效应的存在。

#### 验收标准（可证伪）

1. 账本页不再显示「39 天」而不显示 `n_eff`；~~`n_eff < 30` 时只显示「样本不足」，
   不给任何 skill 数字~~ —— **§9.13 修正**：真实账本的 `n_eff` 就是 1，隐藏数字
   会造成永久空白。改为「数字保留 + 降级为不可判定」。
2. 漂移检测的回放实验：向历史序列注入已知的 Brier 恶化（+0.02），
   CUSUM 必须在 ≤ 20 个决策日内报警，年化误报 ≤ 1 次。
3. 条件模型的 CRPS 在 purged 时序 CV 上同时优于两个基线：常数命中率、
   同日横截面中位数。
4. 实盘 Brier 与回测 Brier 之差超过 2 个 HAC 标准误 ⇒ 判「回测不可信」，
   冻结该策略的自动建卡。

#### 第一步

把 `n_eff` 显示到决策页与 `/api/decision/pwin` 的展示层。这是零风险的
一步，而且它会立刻改变读数：现在的「39 天」会被读成「39 个样本」，
实际只有 1–2 个。

### 9.13 `n_eff` 是分母，不是开关（2026-11-05）

§9.12 的验收标准第 1 条写着「`n_eff < 30` 时只显示「样本不足」，不给任何
skill 数字」。这条与 §9.11 拒绝 `NO_INCREMENT` gate 的理由是**同一个错误**，
只是换了一层：

- §9.11：对滚动估计量 `verdict_incremental` 恒为负 ⇒ 接成 gate 就是永久停机；
- §9.12：真实账本的 `n_eff` 只有 1–2 ⇒ 按 `< 30` 隐藏 skill 就是永久空白。

两者都拿**一个对目标数据结构性不成立的判据**做二值开关。区别只是前者让
结论恒为「否」，后者让读数恒为「无」。界面上「无」比「否」更糟：用户会去
别处找同一个数字（顶部 skill、`/api/decision/pwin`、探针输出），而那些地方
没有「不可判定」的语境。

正确的形态是把它当**分母**，而不是开关：

1. 每一处 skill 旁边都要有它，否则 skill 没有分母；
2. 它决定结论的强度（`significant` 降级），不决定数字的存亡；
3. 它自己不可靠时要标注来源（上界 / 估计值）。

这与三态门禁同构：`UNKNOWN` 不是 `PASS`，也不是不显示——是显式的未知。

#### 顺带修掉的一个硬 bug

`n_eff = n·(se_iid/se)²` 原来没有上界。`se < se_iid`（负自相关或纯噪声）时
它会给出比 `n` 还大的值：实测 n=17、lag=19 的 MA(19) 序列给出 **55**。
「等效独立样本数超过观测数」没有意义。

更根本的是 NW 在这个区间不可用。同一个 MA(19) 过程抽 5 次，方差膨胀在
**0.56–1.26** 之间跳（真实值约 2–3），`n_eff` 在 11–55 之间跳。`hac_lcb`
的文档已经警告过「n 接近 lag 时 NW 不可靠」，但 `n_eff` 仍然照算。

修法（`settle.effective_sample`）：

```
n < 3·(lag+1)  ->  n_eff = max(1, n // (lag+1))，reliable=False
否则            ->  n_eff = min(n, round(n·(se_iid/se)²))，reliable=True
```

第二行是上界修正；第一行是**退回构造性量**：不重叠窗口数不需要估计，
是唯一诚实的读数。

| 账本 | oos_days | lag | 修前 n_eff | 修后 n_eff | reliable |
|---|---|---|---|---|---|
| 探针（321 天） | 321 | 19 | 65 / 44 / 66 / 44 | **不变** | True |
| 真实账本（39 天） | 17 | 19 | 11–55（抽样跳动） | **1** | False |

真实账本的 `n_eff` 因此从「§9.11 推断的 1–2」变成代码输出。修前修后探针的
四个配置逐位不变（65/44/66/44），说明这个修正只影响它该影响的区间。

#### 两个连带的行为变化

1. `settle.score()` 新增 `n_eff` / `hac_lag` / `n_eff_reliable`；
   `significant` 增加 `and n_eff_reliable`——`skill_lcb` 是读数，
   `significant` 是结论，两者的失败模式不一样。30 天 / 20 日持有期的账本
   下界仍为正，但 `n_eff=1`，不再标「显著」。
2. `pwin.walk_forward()` / `estimate()` / `POST /cards` 的 `pwin` 出处
   同步透传 `n_eff_reliable`。

#### 验收

- [x] `test_effective_sample_caps_at_observations`：公式给 400，截到 100
- [x] `test_effective_sample_uses_windows_when_lag_dominates`：17 天 / lag 19
      ⇒ `(1, False)`；60 天 ⇒ `(15, True)`
- [x] `test_walk_forward_reports_n_eff_reliability`：100 天 ⇒ `True`，
      30 天 ⇒ `(1, False)`
- [x] `test_score_does_not_claim_significance_without_effective_sample`：
      下界 > 0 但 `significant=False`
- [x] 决策页显示「有效样本 N（lag L）」；`n_eff < 30` 或 `reliable=False` 时
      skill 降级为「不可判定」并说明上界来源（`Decision.test.tsx`）
- [x] 探针四个配置的 `n_eff` 修前修后逐位一致（65/44/66/44）

#### 端到端实测：`reliable=True` 与 `n_eff=1` 并存

`scripts/verify_pwin_e2e.py`（40 张卡 / 386 条结算 / 39 个决策日 / 17 个可评估日）：

```
[median] skill=+0.3316  lcb=+0.1723  sig=False   n_eff=1  lag=19  reliable=False
[abs]    skill=-0.0144  lcb=-0.0493  sig=False   n_eff=1  lag=19  reliable=False
estimate: verdict=RELIABLE  reliable=True  n_eff=1  n_eff_reliable=False
```

`sig=False` 是本节改动生效（旧实现给 `True`）。但 `estimate` 仍返回
`reliable=True`——因为 `reliable` 只回答「lcb > 0」，不回答「这个 lcb 可不可信」。
两个字段并列不是矛盾，是分工：

| 字段 | 回答 | 在 n_eff=1 时 |
|---|---|---|
| `reliable` | lcb > 0 吗 | True（数字本身为正） |
| `n_eff_reliable` | 这个 lcb 可信吗 | False |

后果：`suggest(require_reliable=True)` 会在这份账本上放行建卡。这不是 bug，
是「`verdict` 不该当 gate」的又一个证据（§9.11 第 1 条）。修法不是给它加
`n_eff` 门槛——那会让真实账本永久停机，与 §9.12 的错误同构——而是把 gate
从「拒绝建卡」改成「建卡 + 在卡上标注」。

#### 仍然没解决

- `3·(lag+1)` 是经验阈值，不是推导出来的。更严格的做法是 Andrews(1991) 的
  数据驱动带宽 + 预白化，或 block bootstrap。当前选它是因为它**可解释**：
  低于这个比值时退回的量（不重叠窗口数）有明确含义，而 NW 的估计没有。
- `n_eff` 仍然是**检验配置**的属性，不是账本的属性：同一批 321 天数据，
  `window=60` 给 65、`window=250` 给 44。页面上显示的是当前配置下的值，
  切换 `window` 会变。这一点必须随数字一起说，否则它会被当成账本的固有属性。
- 两个口径的 `n_eff` 同源（都来自 `score()` 的逐日序列），但因变量不同，
  数值不同。页面上并列显示，不能互相替代。

### 9.14 阈值可以决定「信不信」，不能决定「做不做」（2026-11-05）

§9.13 留下两件事：一个结论（`estimate` 在 `n_eff=1` 时仍返回 `reliable=True`，
`suggest()` 因此放行建卡）和一个没修的字段（`significant` 仍是二值）。本节把
前者升级成一条可执行的界线，把后者拆开。

#### 一、三次同构，一个闭环

| 节 | gate 的输入 | 被 gate 的行为 | 后果 |
|---|---|---|---|
| §9.11 | `verdict_incremental`（滚动窗口算出） | 建卡 | 卡不再增长 ⇒ 判据不变 ⇒ 永久停机 |
| §9.12 | `n_eff`（决策日算出） | 隐藏 skill 数字 | 没人再产卡 ⇒ `n_eff` 不变 ⇒ 永久空白 |
| §六 | `reliable`（结算记录算出） | 拒绝建卡 | 没有新结算 ⇒ `reliable` 不变 ⇒ 永久 409 |

§六 那一行已经拆掉：`reliable` 降级为标签，gate 换成 `allow_prior`（§9.15）。
拆的过程恰好证明了这条形式：只要 gate 的输入还是 `reliable`，无论怎么调阈值都
解不开；换成一个「声明一句就能满足」的开关，闭环才断。

三者不是「阈值选错了」。换成 `n_eff>=30`、`reliable`、更严格的 Andrews 带宽
都一样——**不动点在闭环里，不在判据里**。共同形式是：

> gate 的输入，由被 gate 的行为所产生的数据算出。

由此得到一条比「分母不是开关」更可操作的界线：

> **阈值可以决定「信不信」，不能决定「做不做」。**

允许 `n_eff < 30 ⇒ UNKNOWN` 这样的门槛存在，只要它的作用域是**标签**。
一旦作用域是**行为**（隐藏数字、拒绝建卡），就重新变成 §9.12。

#### 二、`significant` 拆成三个字段

旧实现 `significant = lcb > 0 and n_eff_reliable` 把读数和可判定性 AND 在一起，
`False` 同时表示「测过了，下界不为正」和「根本测不了」。这两件事的后续动作
相反：前者该改策略，后者该继续攒样本。

| 字段 | 回答 | 真实账本（n_eff=1, lcb=+0.17） |
|---|---|---|
| `skill_lcb` | 下界是多少（读数） | +0.1723 |
| `skill_lcb_positive` | 下界为正吗（读数） | `True` |
| `significance` | 这个下界能不能当结论（结论） | `UNKNOWN` |
| `significant` | 等价于 `significance == "PASS"`（兼容字段） | `False` |

**没有把 `significant` 降格成纯读数。** 那样做会让 `n_eff=1` 的账本重新输出
`significant=true`，而所有读这个字段的旧调用方（探针、下游脚本、将来的自动化）
都会把它读成「显著」。统计工具里，保守的错误（把可判定读成不可判定）比激进的
错误（把不可判定读成显著）便宜得多。所以：新增读数键，保留结论键的语义。

`MIN_NEFF=30` 在这里**只喂标签**：不隐藏数字、不放行也不拦截任何请求。这是它
和 §9.12 那条验收标准的唯一区别，也是它能活下来的原因。

#### 三、`n_eff` 原来是两把尺子

同一个键名，两条不同的残差序列：

| 出处 | 残差序列 |
|---|---|
| `settle.score()` | `(b0-y)² - (p-y)²`，`b0` 是 0.5（median）或展开 climatology（abs） |
| `pwin.walk_forward()` | `(p_clim-y)² - (p-y)²`，`p_clim` 是**按结算可见性**展开的收缩胜率 |

自协方差不同 ⇒ `se` 不同 ⇒ `n_eff` 不同。现在两边都落在 `n < 3(lag+1)` 的
退化分支里（都是 1），看不出差别；**账本一旦越过 60 个可评估日，卡上的
「有效样本」和 `/api/decision/pwin` 的就会分叉**，而且是两个都叫 `n_eff` 的数字。

这正是 §9.8 抓过的「一个键两种含义」，只是换了字段。修法是拆名：

```
walk_forward: n_eff                  # 对应 verdict（相对常数先验）
              n_eff_incremental      # 对应 verdict_incremental（相对展开基线）
```

`estimate()` 与 `POST /cards` 的 `pwin` 出处同步透传两个键。`n_eff` 的语义因此
从「增量检验的有效样本」变成「verdict 的有效样本」——数值在退化区间里不变
（都是 1），所以这次改动**可证不改变任何读数**，只改变它指向哪条序列。

#### 四、顺带修正的三处表述

1. **`n // (lag+1)` 不是上界**，是 MA(H−1) 下 `n_eff` 的**模型推断值**
   （长程方差 ≈ H·γ0 ⇒ `n_eff ≈ n/H`）。说成上界会让人以为真值只会更小；
   负自相关时真值更大。经验的是那个 `3`，不是这个值。
2. **`n_eff` 挂在 skill 上，不挂在账本上**。页面上从「有效样本」改为
   「skill 的有效样本」——它随 `window` 变，不是账本的固有属性。
3. 页面的 `weak` 不再自己算 `n_eff < 30 || !reliable`，改读 `significance`。
   阈值只在后端定义一处，前端不复制。

#### 五、实测

`scripts/verify_pwin_e2e.py`（40 张卡 / 390 条结算 / 39 个决策日 / 17 个可评估日）：

```
[abs]    skill=-0.0144  lcb=-0.0493  sig=UNKNOWN  lcb_positive=False  significant=False
[median] skill=+0.3316  lcb=+0.1723  sig=UNKNOWN  lcb_positive=True   significant=False
         n_eff=1  lag=19  reliable=False
estimate: reliable=True  verdict=RELIABLE  significance=UNKNOWN
          n_eff=1  n_eff_incremental=1  n_eff_reliable=False
```

`lcb_positive=True` 而 `significance=UNKNOWN` 是这次改动要守的形状：读数没被
改动，结论被如实降级。

#### 验收

- [x] `test_significance_of_three_states`：PASS / FAIL / UNKNOWN 的判定边界
- [x] `test_score_does_not_claim_significance_without_effective_sample`：
      `n_eff=1` 且 `lcb>0` ⇒ `skill_lcb_positive=True`、`significance=UNKNOWN`、
      `significant=False`
- [x] `test_score_pass_and_fail_both_require_a_denominator`：样本撑得住时符号才
      决定 PASS / FAIL
- [x] `test_walk_forward_keeps_the_two_n_eff_apart`：两个键都在，不共用
- [x] `test_estimate_significance_is_unknown_when_sample_is_short`：
      冷启动 / 短账本 ⇒ UNKNOWN，不是 FAIL
- [x] 前端 `Decision.test.tsx`：`PASS` 打 `✓`、`UNKNOWN` 打 `?`、`FAIL` 不打符号
- [x] 探针四个口径的输出与 §9.13 逐位一致（`n_eff=1`、`lcb=+0.1723`）

#### 没做的

1. ~~`require_reliable` → `require_evidence`~~ —— 已落地，见 §9.15。字段名最终是
   三态 `allow_prior`，不是 `require_evidence`：gate 的对象是「有没有证据」，
   而 `allow_prior=true` 是「这次就用先验」的显式声明，比
   `require_evidence=false` 少一层否定。
2. **把收缩推广到 skill**（贝叶斯化）。对 skill 取以 0 为中心、强度 κ 的
   skeptical prior，报后验均值 + 可信区间，不报 `lcb > 0`。`n_eff=1` 时后验
   ≈ 先验，区间自动变宽——「数据少」表现为「区间宽」，而不是「标签 UNKNOWN」。
   这才是分母概念的完全体，属于 §9.16 的体量。

#### 仍然没解决

- `MIN_NEFF=30` 是经验值。它让值的切换点（`n=3(lag+1)`，`n_eff` 从 2 跳到 15）
  落在标签仍为 UNKNOWN 的区间里，但两者不是精确对齐的：如果 `n_eff` 在
  `n=65` 就跨过 30，用户仍会看到一次「值跳了、标签也跳了」。要彻底消除，
  得让标签带宽（`4(lag+1)`）和值的切换点错开，而那需要把 `n` 和 `lag` 一起
  传进 `significance_of`——多一层耦合，当前不值得。
- `MIN_NEFF` 只约束 `skill_lcb` 的结论。`verdict_incremental` 的结论（
  `INCREMENTAL` / `MARGINAL` / `NO_INCREMENT`）仍然只看 `lcb` 符号，没有
  对应的三态。它现在不是 gate（§9.11），所以危害小；但它和 `significance`
  不对称，读的人会以为两个结论的可信度是同一档。（§9.16 补了 `NOT_TESTABLE`
  一档，但 `n_eff` 仍不参与判定，这个不对称只解决了一半。）

### 9.15 `allow_prior`：gate 的对象是「有没有证据」（2026-11-05）

§9.14 没做的第 1 条当天落地。落地过程中第一版写法被证伪，值得单独记下来：
**换字段名不等于换语义**，而 `bool` 承担不了三态。

#### 一、第一版写法会静默反转

第一版：新增 `allow_prior: bool = False`，保留 `require_reliable`，两者都传时
`allow_prior` 优先。看起来旧客户端不受影响。实测（Pydantic 2.13.4）：

```
M(require_reliable=False).model_fields_set  ==  set()
M(require_reliable=False).model_dump()      ==  {'allow_prior': False}
```

`require_reliable` 在新模型里已不存在，Pydantic 默认 `extra="ignore"`，**未知
字段被静默丢弃，不报错**。于是旧客户端只传 `require_reliable=false` 时，
`allow_prior` 取默认 `False` 并胜出 ⇒ 还是 409。同一处静默反转，换了个入口。

根因不是优先级写错，是 `bool` 分不清「没传」和「传了 false」。这个字段必须
三态。

#### 二、最终形态：三态 + 单点解析

```python
allow_prior: bool | None = None      # None = 未指定
require_reliable: bool = True        # 保留原默认；它映射到的正是「失败关闭」

def _resolve_allow_prior(req) -> bool:
    if req.allow_prior is not None:
        if req.allow_prior is False and req.require_reliable is False:
            raise HTTPException(400, "allow_prior=false 与 require_reliable=false 相互矛盾")
        return req.allow_prior
    return not req.require_reliable   # 旧字段唯一的映射方向
```

`require_reliable` 不需要三态：默认 `True` 映射到 `False`，与「未指定」同解。

| 请求 | 解析结果 | 行为 |
|---|---|---|
| 不传 | `False` | 无证据 → 409；有证据（哪怕 unreliable）→ 200 + 标注 |
| `allow_prior=true` | `True` | 冷启动用先验建卡，`source=prior` |
| `require_reliable=false`（旧） | `True` | 等价于 `allow_prior=true` |
| `require_reliable=true`（旧，含默认） | `False` | 只拒「无证据」，不再拒「证据不可靠」 |
| 两个都传 false | — | 400，不静默择一 |

两个字段只在 `_resolve_allow_prior` 里读。散落读取是这类改动的标准死法：
一处读了 `req.require_reliable`，优先级就白设计了。

#### 三、这会改变默认请求的行为，不是纯改名

旧的 `require_reliable=true` 连 unreliable 账本一起拒；新的 `allow_prior=false`
只拒「无证据」。同一份 unreliable 账本，昨天 409，今天 200。这是 §9.14 一的
意图，但必须承认它是行为变更，所以两条旧测试是**翻转**，不是回滚：

| 旧测试 | 新断言 |
|---|---|
| `test_auto_pwin_fails_closed_when_unreliable` | 200 + `verdict=UNRELIABLE` + `significance=UNKNOWN` |
| `test_suggest_fails_closed_when_unreliable` | 不再抛；`p_win is not None`，结论降级 |

`suggest()` 第二个 `raise` **整段删除**，不是改名。留着它，`allow_prior` 对
unreliable 分支就是空操作——那正是「改了名、没改行为」的形态。

`allow_prior=false` 仍然会拒冷启动。这看着像同一个死锁，但不是：闭环的定义是
「gate 的输入由被 gate 的行为产生」，而这里 gate 的输入是**一句声明**——
`allow_prior=true` 永远可满足，不需要任何新数据。区别在可满足性，不在严格性。

#### 四、`allow_prior=false` 保证不了「没有先验」

它只保证 `p_win is not None`，即 `n >= MIN_N=8`。真实账本 `n=386 / hits=77`：

| 量 | 值 |
|---|---|
| `p_win` = (77+10)/406 | 0.214286 |
| `prior_weight` = 20/406（按观测数） | 0.049261 |
| `n_eff=1` ⇒ `prior_weight_effective` = 20/21 | 0.952381 |
| 按 `n_eff` 收缩 (77/386×1+10)/21 | 0.485690 |

那张卡 `source="ledger"`、`allow_prior=false` 放行，但它的 p_win 有 95% 是先验。
把分母从 `n` 换成 `n_eff`，p_win 从 0.214286 挪到 0.485690——**0.271404 的位移
全部来自把 386 条相关观测当成独立样本**。这正是 gate 声称要拒绝的事，只是
`source` 标签把它遮住了。

**但不能把它做成 gate。** `prior_weight_effective > 0.5 ⇒ 拒绝` 又是「gate 的
输入由被 gate 的行为产生」（建卡 → 结算 → `n_eff` 涨），§9.14 一的闭环。所以它
只是标签：两个权重进 `estimate()` 的返回、进 `forecast.pwin`、进承诺哈希。
事后无法把「95% 是先验」改写成别的。

两个都要给。只给 `prior_weight`（4.9%）会把 386 条重叠观测读成 386 个独立样本；
只给 `prior_weight_effective`（95.2%）又看不出差在哪里。差值本身是信息。

`prior_weight_effective` 的分母是 `walk_forward` 的 `n_eff`——那是**计分序列**的
等效独立样本数，不是收缩本身的自由度。两者在 MA(H−1) 下同阶（都约 `n/H`），
拿它当代理是保守的；但它随 `window` 变，不是账本的固有属性，读数时必须带这个
限定。`n_eff=None`（样本不够、没跑 walk-forward）时为 `None`：「没测」既不是
「全先验」也不是「全证据」。

#### 五、顺带修掉的两处出处撒谎

1. `auto_pwin=true` 但每个 pick 都自带 `p_win` 时，旧实现照样写一块
   `source=ledger` 的出处——把人工填的数字记成账本算的。现在没有要补的 pick
   就不写出处。
2. 部分 pick 缺 p_win 时，出处加 `filled: [symbols]`，标明哪几个是这次补的。
   否则同一张卡上「账本算的」和「人填的」无法区分。

#### 验收

- [x] `test_prior_weights_differ_by_denominator`：386/1 ⇒ 4.9% 对 95.2%
- [x] `test_estimate_carries_prior_weight_of_the_number_it_returns`
- [x] `test_suggest_returns_unreliable_ledger_as_a_label`
- [x] `test_suggest_allow_prior_falls_back_on_cold_start`：纯先验 ⇒ 权重 1.0/1.0
- [x] `test_allow_prior_true_builds_card_on_cold_start`：声明即可放行
- [x] `test_allow_prior_false_conflicts_with_legacy_false`：矛盾 400
- [x] `test_legacy_require_reliable_false_maps_to_allow_prior`：旧字段映射方向
- [x] `test_auto_pwin_with_all_p_win_given_claims_no_ledger_provenance`
- [x] 三个 decision 测试文件 95 passed（基线 90）
- [x] `verify_pwin_e2e.py`：`n=386 hits=77 p_win=0.214286`、
      `prior_weight=0.049261`、`prior_weight_effective=0.952381` 与本节逐位一致

#### 仍然没解决

- **标签只到卡，没到分数视图。** `settlements.jsonl` 记了 `p_win_source`，没记
  两个权重，所以 `score()["by_source"]` 仍把「95% 是先验」的卡和「证据为主」的
  卡一起算进 `ledger` 组（实测该组 `n=376`、`skill=+0.3404`）。要修得先决定
  「按权重分桶」还是「给每组报权重均值」——那是分组口径问题，不是加一个字段
  能解决的，留给下一节。
- **收缩用 `n` 而不是 `n_eff` 是单位错误。** κ 的单位是「独立样本」，`n` 的
  单位是「观测」。修它会把 p_win 从 0.214 挪到 0.486，属于 §9.16，必须先用
  探针拿外部证据，不能顺手改。
  **探针（`scripts/probe_shrink_unit.py`）推翻了「单位错误」这个读法**：κ 换算到
  等效单位后两个权重恒等，95.2% 是标签的换算错误、不是 `p_win` 的分母错误。
  探针账本上 `p(n)=0.186` 落在 [0.105, 0.235] 内，`p(n_eff)=0.470` 落在区间外
  （见 §9.16 的实测表）。方向是改标签、不动分母，留给 §9.17。
- `allow_prior` 这个名字仍有歧义：有证据时先验永远参与收缩，这个开关管不着。
  它 gate 的只是「没有任何证据时是否允许用先验常数」。改名
  `allow_prior_fallback` 会更准，但当前只有 `_resolve_allow_prior` 一处读它，
  先靠 docstring 收口。

### 9.16 恒等零不是结论：`verdict_incremental` 的第四态（2026-11-05）

`probe_shrink_unit.py --days 40` 在 40 个决策日 / 400 条观测的账本上跑默认配置，
[D] 段原本输出：

```
window=250 对 40 个决策日：improvement_vs_clim=+0.000000  lcb=0.0  verdict=NO_INCREMENT
```

读起来是「近端窗口不比全历史多含信息」。它不是。它是「近端窗口**就是**全历史」。

#### 一、恒等式

设第 i 天已可见的决策日集合 `V_i`（已结算、出场日早于当天）。两个估计量是

```
p_i        = shrink(近端 window 个可见日)
p_clim,i   = shrink(全部 V_i)
```

`window >= |V_i|` 时 `V_i[-window:] ≡ V_i` ⇒ `p_i ≡ p_clim,i` ⇒ 逐日改善
`(p_clim − y)² − (p − y)² ≡ 0` ⇒ 均值 0、标准误 0、下界 0。

那个 `+0.000000 / 0.0` 不是「测出来是 0」，是**恒等式**。两个估计量吃的是同一批
样本；比较它们等于拿尺子量自己，得到的 0 与「两者一样好」无关。

#### 二、它为什么不能报成 NO_INCREMENT

- 「没有增量」是一个**关于数据的结论**：近端与远端不同，且近端没赢。
- 这里近端与远端**相同**，所以「谁更好」这个问题从来没有被问过。

后续动作相反：前者该改策略，后者该改 `window`。合成一个 `NO_INCREMENT` 时，
读的人只会看到「否」——而界面上的「否」比「未知」更容易被当成定论（§9.14 二）。

默认配置正好落在这个区间：真实账本 39 个决策日、17 个可评估日，
`DEFAULT_WINDOW = 250`。

#### 三、修法：先判「能不能测」，再判「测出了什么」

`walk_forward` 记一个 `truncated_any`：至少有一个被评估日满足 `|V_i| > window`。

| `truncated_any` | `verdict_incremental` | `improvement_vs_clim` / `skill_vs_clim` / `skill_vs_clim_lcb` / `n_eff_incremental` |
|---|---|---|
| `True` | INCREMENTAL / MARGINAL / NO_INCREMENT | 数值 |
| `False` | **NOT_TESTABLE** | 全为 `None` |
| `None`（没有可评估日） | INSUFFICIENT | 全为 `None` |

撤数字与给状态是同一件事的两半。只改状态、留着 `+0.000000`，读者仍会读数字；
只删数字、不改状态，`None` 会被当成「没算」。两个都要。

`window <= 0`（不限窗口）永远不置位 ⇒ 永远 `NOT_TESTABLE`：它不是「截断到 0 天」，
是「没有截断这个概念」。

判据是「没有一个被评估日被截断」，**不是**「`window ≥ oos_days`」。后者在边界上
会漏判：`|V_i|` 数的是**所有**已可见日，`oos_days` 只数**被评估**的日子。22 个
决策日 / horizon=1 时 `oos_days=20`，而最后一天能看到 21 天 ⇒ `window=20` 仍然
截断（检验可做），`window=21` 才恒等（`NOT_TESTABLE`）。差一天，状态就不同。

#### 实测（同一份 40 决策日 / 400 观测 / horizon 20 / median 口径的账本）

| 配置 | `truncated_any` | `improvement_vs_clim` | `skill_vs_clim_lcb` | `verdict_incremental` |
|---|---|---|---|---|
| `window=250`（默认） | False | — | — | **NOT_TESTABLE** |
| `window=10` | True | −0.005283 | −0.0735 | NO_INCREMENT |

第二行才是「测过、没有增量」。第一行现在不报数字。

#### 两条旧测试必须改写，不是被删除

它们把恒等式当成了结论：

| 测试 | 旧断言 | 新断言 | 为什么 |
|---|---|---|---|
| `test_constant_predictor_has_no_increment_but_can_be_reliable` | `window=250`，改善 `== 0` | `window=20`（真的截断），改善 `≤ 0`（实测 −0.000021） | 「常数预测器没有分辨率」是一个**估计**出来的很小的负数，不是恒等式 |
| `test_window_counts_settled_days_not_calendar_days` | `window=10000`，改善 `== 0` | `NOT_TESTABLE` + `improvement_vs_clim is None` | 主张（window 数已结算日）不变，末尾那个 0 换成了状态 |

第一条改窗口之后，`verdict` 仍是 RELIABLE、`verdict_incremental` 仍是
NO_INCREMENT——原测试的主张保住了，只是证据不再来自恒等式。

#### 与 §9.13 / §9.14 / §六 的关系

那三次是「判据对目标数据结构性不成立，却被接成**开关**」；这一次判据没有接成
开关，它接成了**结论**。所以修法不同：不是加门槛、不是降级标签，而是承认这个
配置下**没有对照**，并把状态说出来。

四次错误的共同点只有一句：**输出的强度超过了证据的强度。**

#### 验收

- [x] `test_increment_is_not_testable_when_window_covers_every_day`：`DEFAULT_WINDOW`
      在 40 日账本上 ⇒ `NOT_TESTABLE`，四个数值字段全 `None`，`reason` 不含
      「多含信息」；`estimate()` 同步透传
- [x] `test_increment_is_testable_once_the_window_truncates`：同一账本
      `window=10` ⇒ 有数值，不是 `NOT_TESTABLE`
- [x] `test_not_testable_is_a_state_not_a_zero`：`0/0` 的两种来源映射到两个状态
- [x] `test_constant_predictor_has_no_increment_but_can_be_reliable` 改写（窗口 20）
- [x] `test_window_counts_settled_days_not_calendar_days` 改写（wide ⇒ `NOT_TESTABLE`）
- [x] 三个 decision 测试文件 98 passed（基线 95，新增 3）
- [x] `verify_pwin_e2e.py` 逐位不变：`n=386 hits=77 p_win=0.214286`、
      `prior_weight=0.049261`、`prior_weight_effective=0.952381`、`reliable=True`、
      `significance=UNKNOWN`；新增 `truncated_any=False`、
      `verdict_incremental=NOT_TESTABLE`、`skill_vs_clim=None`
- [x] `probe_shrink_unit.py --days 40`：[D] 段输出 `NOT_TESTABLE`，`improvement_vs_clim=—`

#### 为什么 `truncated_any` 没进卡（`truncated_days` 进了）

卡的 `forecast.pwin` 已经封存了 `window` / `days` / `oos_days` /
`verdict_incremental`，标志可由这四者推出（`window >= days` ⇒ 不可能截断）。
加字段会改封存 schema 的形状，换不来新信息。`/api/decision/pwin` 的实时返回里
带这个字段，因为那里没有卡上的上下文。

`truncated_days` 是例外，它推不出来：同一天能不能看到样本取决于那天的出场日，
`days` / `oos_days` 都不含这个信息。而它恰好是 §9.17 里唯一能衡量结论分量的数，
所以进卡。

#### 仍然没解决

1. **κ 的单位（§9.19）。** 这一节没动 `p_win`、`kappa`、`prior_weight_effective`。
   探针只够否掉一个读法，不够定一个新口径：非重叠 20 日窗口给出 0.155 / 0.185，
   `n_eff=2`，命中率 90% 区间 [0.105, 0.235]；`p(n)=0.186` 在区间内，
   `p(n_eff)=0.470` 在区间外。所以「换成 `n_eff`」会让估计变差，该改的是
   `prior_weight_effective` 的**标签**——但改标签要连带改 `settlements.jsonl` 里
   字段的含义，必须单独写一节。
2. **`verdict_incremental` 仍然不看 `n_eff`。** 现在能区分「测不了」，但「测得出来、
   而分母是 1」仍然报 INCREMENTAL / NO_INCREMENT。§9.14 的三态只覆盖 `verdict`，
   这个不对称还在。§9.17 补上了「这个结论比了几天」（`truncated_days` 进结论句），
   但没有把它接成开关——那会变成 §9.13 警告过的「把分母当闸门」。
3. **`by_source` 的分组键（§9.15 遗留）。** 39 日账本里 40 张卡全部是 `UNKNOWN`，
   按 provenance 分组只会得到「先验穿着账本的外衣」。分组键该是封存时的
   `significance`，但那要先把字段写进 `settlements.jsonl`，与第 1 条同一批改动。

> **第 2 条已由 §9.21 补上。** 补的不是第五态，是第二个检验自己的
> `significance_incremental`：`INCREMENTAL × UNKNOWN` = 改善过门槛、但有效样本
> 不足 30。第 1、3 条仍开着。

### 9.17 布尔量量不出功效：`truncated_days`（2026-11-05）

§9.16 把「零天被截断」判成 `NOT_TESTABLE`，判据是 `truncated_any`。它是个布尔量：
`True` 只保证**至少一天**被截断。一天能撑起什么结论，上一节没有问。

#### 一、反例：同一份账本，只改 `window`

账本：30 个决策日 18/20 命中，接 30 个决策日 2/20 命中（90% → 10% 的制度翻转），
`horizon=1`，1200 条观测，57 个可评估日。数据一个字不改，只扫 `window`：

| `window` | 截断日/评估日 | `improvement_vs_clim` | `skill_vs_clim_lcb` | `n_eff_inc` | `verdict_incremental` |
|---|---|---|---|---|---|
| 5 | 52/57 | +0.141318 | +0.386809 | 57 | INCREMENTAL |
| 12 | 45/57 | +0.115852 | +0.317726 | 57 | INCREMENTAL |
| 20 | 37/57 | +0.085577 | +0.231231 | 57 | INCREMENTAL |
| 28 | 29/57 | +0.055395 | +0.143621 | 57 | INCREMENTAL |
| 40 | 17/57 | +0.017918 | +0.039038 | 57 | INCREMENTAL |
| 44 | 13/57 | +0.010192 | +0.019620 | 57 | INCREMENTAL |
| 53 | 4/57 | +0.001009 | +0.000464 | 57 | MARGINAL |
| 54 | 3/57 | +0.000598 | −0.000002 | 57 | NO_INCREMENT |
| 56 | 1/57 | +0.000097 | −0.000222 | 57 | NO_INCREMENT |
| 57 | 0/57 | — | — | — | NOT_TESTABLE |

> **这张表被 §9.18 改过一版，§9.20 已撤回。** 中间那版把滞后改成
> `window-1`，下界普遍低 2–3 倍，转折点从 `window=54` 提前到 `window=25`。
> 那个改动的前提（`r1(d)=+0.98` 是零假设下的自相关）是错的，读数已恢复为
> `lag=horizon-1`。本节的结论不受影响：`window=12` 有增量、`window=56` 没有。

90% → 10% 是这份账本里最大的信号，`window=12` 时下界 +0.1014；同一个信号在
`window=56` 时报 `NO_INCREMENT`。变的不是数据，是「窗口碰巧绑定了几天」。

#### 二、为什么 1 天不能替 57 天说话

逐日改善 `d_i = (p_clim,i − y_i)² − (p_i − y_i)²`。窗口没有绑定的那天
`p_i ≡ p_clim,i` ⇒ `d_i ≡ 0`。这些 0 是**真的**，不是缺失：那天两个预测器确实
相同，说它改善了 0 是对的。

但均值是全部评估日的平均，于是真实效应被摊成 `f·δ`，`f = k/n`。`window=56`
时 `mean = δ/57`，`se` 却仍按 57 天算（只有 1 个非零观测，方差也只剩那一个），
`lcb` 必然落在 0 附近——**结论由 `f` 决定，不由数据决定**。

`NO_INCREMENT` 的旧句子是「近端窗口不比全历史多含信息」。这是一句关于信息量的
结论，需要多天才成立；`k=1` 时它只证明了「最后一天近端赢了一点，摊薄之后不够」。
后续动作也相反：前者该改策略，后者该改 `window`——和 §9.16 的分歧一模一样，
只是位置从 `k=0` 挪到了 `k=1`。

#### 三、修法：不设阈值，让句子和证据等长

没有引入「`f ≥ 50%` 才许报结论」这类阈值。那是把 §9.14 的毛病换个位置再犯一次：
阈值决定信不信，不能决定做不做。做的是两件事：

1. `truncated_days` 单独成字段，与 `oos_days` 一起读；
2. 三个数值结论的句子末尾附覆盖度，并与 `n_eff` 合进同一个括号，例如
   `（等效独立样本 3；只有 3/57 天被截断，其余 54 天两个估计量恒等、改善恒为 0）`。
   （原文这里是「等效独立样本 57」——那正是 §9.22 修掉的自相矛盾：分母不能
   超过真正比过的天数。）

数字全部保留：`mean` 是真的，撤掉它等于把「这个 window 效果很小」这个事实藏起来。
`NOT_TESTABLE` 分支不变——`k=0` 时没有对照，数字仍然全撤。两节的界线是
「没有数据」与「数据很少」，不是「够不够多」。

#### 四、`n_eff_incremental` 在这个区间读的是什么

`horizon=1 ⇒ lag=0 ⇒ n_eff ≡ n`。所以 `window=56` 时报「等效独立样本 57」，
而承载比较的只有 1 天。它不是算错：没有重叠就没有可校正的自相关，`n_eff` 只是
把 `n` 抄了一遍。但把它印在结论里，读者会以为这个判断建立在 57 个独立样本上。

要它有意义，得让它数「非零贡献的天数」；可那样它就和 `skill_vs_clim_lcb` 的
分母不一致了（那个 `lcb` 是在全部 `n` 天上算的），又是一次「一个键两种含义」。
所以这一版不改 `n_eff`：让 `truncated_days` 承担「证据有几天」，让 `n_eff`
继续承担「这 n 天值几个独立样本」。两个问题不挤进一个字段。

> **§9.22 修正。** 这个折中在分母上站不住：`n_eff ≡ n` 不只是「把 n 抄了
> 一遍」，它会在 6 个对照日上报 57、在 1 个对照日上报 256，于是
> `significance_incremental` 在 1 天证据上给出 `FAIL`。修法不是把 `n_eff`
> 改成「非零天数」（那确实会和 `lcb` 的分母打架），而是给它加一条上界：
> `min(n_eff, truncated_days // (lag+1))`。`mean` 与 `lcb` 不变，被截的只有
> 「这个下界有没有分母」这个判断。

> **§9.18 曾推翻这段，§9.20 又把 §9.18 推翻了。** 中间的诊断是「重叠有两种，
> `lag=horizon-1` 漏掉了估计量重叠（共用 `w-1` 天）」。这个诊断错在判据：
> 它用 `r1(d)=+0.98` 证明「存在自相关」，而那个 0.98 来自**确定性账本**与
> **制度翻转**，不是零假设下的依赖。平稳 + 抽样的同一份账本上 `r1(d)=-0.10`，
> `lag=0` 的误报率 4.5%（n=57）、0%（n=200）。结论回到原样：`n_eff=n` 是
> **正确读数**，不是把 `n` 抄了一遍；需要脚注的是「只有 k 天承载证据」，
> 那已经由 `truncated_days` 承担。

#### 五、验收

- [x] `test_one_truncated_day_cannot_speak_for_the_whole_ledger`：最大截断窗口下
      `truncated_days == 1`、结论 `NO_INCREMENT`、句子含 `1/`、不含「多含信息」；
      同一份数据 `window=5` ⇒ `truncated_days > 50`、`INCREMENTAL`
- [x] `test_not_testable_is_a_state_not_a_zero` 的 `NO_INCREMENT` 断言改为
      「没测出增量」+ 覆盖度（原断言钉的是被改掉的那句话）
- [x] 三个 decision 测试文件全通过
- [x] `verify_pwin_e2e.py`：`n=381 hits=77 p_win=0.216958`、
      `prior_weight=0.049875`、`prior_weight_effective=0.952381`、`reliable=True`、
      `significance=UNKNOWN`、`truncated_any=False`、
      `verdict_incremental=NOT_TESTABLE`、`skill_vs_clim=None`；新增
      `truncated_days=0`
- [x] `probe_shrink_unit.py --days 40` 的 [D] 段逐字不变
- [x] `scripts/probe_truncation_power.py`：上表可复现

> **这两条 `[x]` 的写法有问题，§9.18 已改。** 原来写的是「逐位不变
> `n=386 … p_win=0.214286`」，而这组字面量当天下午就变成 381/0.216958——
> 不是代码回归，是 `data/kline_daily_enriched/date=2026-11-05/part.parquet`
> 在 15:56 落盘，多了一个交易日，`[-40:]` 的决策日窗口整体右移一格。
> **一个随本地行情漂移的字面量不能当回归保证**：它分不清「代码变了」和
> 「数据变了」。要么断言关系（`truncated_days == 0`、`verdict_incremental ==
> NOT_TESTABLE`、`skill_vs_clim is None`），要么把快照日期写进断言旁边。

#### 仍然没解决

- **`truncated_days` 只是标签，不是开关。** 它进了结论句和卡上的出处，但不参与
  判定。要不要在 `k/n` 过低时降级成 `NOT_TESTABLE`，是下一节的口径问题——
  一旦接成阈值，就又回到「阈值决定做不做」。
- **`n_eff` 在 `lag=0` 时恒等于 `n`。** 这一节没有修，因为修它会与 `lcb` 的分母
  脱钩。真正需要的是把「覆盖度」与「等效独立样本」当成两个量分别报告，
  而不是让一个字段同时回答。

### 9.18 滞后阶数读错了对象：`window` 造的重叠（2026-11-05）

> **本节已撤回，见 §9.20。** 诊断的前提是「同一份账本上 `r1(d)=+0.98`
> 说明零假设下存在估计量自相关」。那份账本同时是**确定性**的（每天恰好
> 18/20 或 2/20 命中）和**有制度翻转**的，两个条件各自都能把 `r1` 推到 1；
> 平稳 + 二项抽样的账本上 `r1(d)=-0.10`。`lag=window-1` 的唯一效果是在
> n=57 时把功效从 100% 打到 0%。以下保留原文，作为「判据选错会得到什么」
> 的记录——它的推理链是完整的，错在第一块砖。

§9.17 把「零天」的边界修成了「一天」，并留下一条判断：`n_eff=57` 只是把 `n`
抄了一遍，因为它落在退化情形（只有 1 天被截断）。这条判断低估了问题的范围。

#### 一、`n_eff=57` 不是边界情形，是每一行

同一份账本（90% → 10% 翻转，`horizon=1`，57 个可评估日），对每个 `window`
的逐日改善序列直接调 `effective_sample(n, se_iid, se, lag=0)`：

| `window` | 5 | 12 | 20 | 28 | 36 | 40 | 44 | 48 | 50 | 52 | 53 | 54 | 55 | 56 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `n_eff` | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 |

一个在 14 个配置上完全不变的数，不可能是对「这些观测值几个独立样本」的估计。
所以这不是「`k=1` 时退化」：`window=12` 有 45 天参与比较，报的仍然是 57。
§9.17 用 `truncated_days` 给这句话补了脚注，但被补的那句话本身是错的。

#### 二、两种重叠，一个键

`lag = horizon - 1` 的理由是「持有 H 天的卡，相邻决策日的**结果**重叠 H-1 天」。
重叠有两种，它只覆盖第一种：

| 重叠 | 来源 | 长度 | 是否进 `lag` |
|---|---|---|---|
| 结果重叠 | 相邻决策日持有的交易日 | `horizon - 1` | 是 |
| 估计量重叠 | 两个估计量共用的已结算决策日 | `window - 1` | **否** |

第 i 天的 `p` 用已结算日 `[i-w, i-1]`，第 i+1 天用 `[i-w+1, i]`，共用 `w-1` 天。
逐日改善因此是 MA(w-1)，与持有期无关。`horizon=1` 时 `lag=0`，NW 修正整个
被关掉，`effective_sample` 直接 `return n`——**「没有可校正的自相关」和
「假定自相关为零」被写成了同一个返回值**。

实测这条序列的 lag-1 自相关：

| `window` | 12 | 20 | 40 | 53 |
|---|---|---|---|---|
| `r1` | +0.983 | +0.982 | +0.925 | +0.673 |
| `r3` | +0.913 | +0.921 | +0.763 | +0.133 |

`lag=0` 的前提（`r1≈0`）在每一行都不成立。

#### 三、改法：换滞后的来源，不换 `n_eff` 的定义

`walk_forward` 里增量序列单独取滞后：

    lag_inc = max(horizon - 1, min(window, n) - 1)

`min(window, n)` 是「窗口不会比序列长」这个上限；窗口盖住全部可评估日时该检验
已判 `NOT_TESTABLE`，不进入这条路径。只改增量这一条：`daily` 比的是常数 0.5，
它的对照不由 `window` 制造，持有期重叠仍是主项。两条序列各有各的滞后，各自
单独存字段（`hac_lag` / `hac_lag_incremental`）。

**`n_eff` 的公式一个字没改**，仍是 `n·(se_iid/se)²`，仍与 `skill_vs_clim_lcb`
共用同一个 `se`。§9.17 担心的「改 `n_eff` 会与 `lcb` 的分母脱钩」没有发生：
脱钩的是 `lag`，不是 `n_eff`。把滞后改对，两个数自动回到同一分母上。

另加一处：`n_eff_reliable=False` 时结论句标注「NW 不可用，取不重叠窗口数」。
`1` 和「估出来是 1」是两件事，前者是 `n // (lag+1)`。两个检验都加——第一个
检验的 `reason` 原本也把 `等效独立样本 1` 写成估计值（真实账本就是这种情形）。

#### 四、修正前后的同一张表

| `window` | 截断日/评估日 | `lag` 旧→新 | `n_eff` 旧→新 | `skill_vs_clim_lcb` 旧→新 | 结论 旧→新 |
|---|---|---|---|---|---|
| 5 | 52/57 | 0→4 | 57→12 | +0.3868→+0.2439 | INCREMENTAL→INCREMENTAL |
| 12 | 45/57 | 0→11 | 57→6 | +0.3177→+0.1014 | INCREMENTAL→INCREMENTAL |
| 20 | 37/57 | 0→19 | 57→2 | +0.2312→+0.0207 | INCREMENTAL→INCREMENTAL |
| 28 | 29/57 | 0→27 | 57→2 | +0.1436→−0.0105 | INCREMENTAL→**NO_INCREMENT** |
| 40 | 17/57 | 0→39 | 57→1 | +0.0390→−0.0132 | INCREMENTAL→**NO_INCREMENT** |
| 44 | 13/57 | 0→43 | 57→1 | +0.0196→−0.0090 | INCREMENTAL→**NO_INCREMENT** |
| 53 | 4/57 | 0→52 | 57→1 | +0.0005→−0.0011 | MARGINAL→**NO_INCREMENT** |
| 56 | 1/57 | 0→55 | 57→1 | −0.0002→−0.0001 | NO_INCREMENT→NO_INCREMENT |

三件事因此变了性质：

1. **转折点从 `window=54` 提前到 `window=25`。** 逐窗口比对 1..56：`window=24..53`
   这 30 个配置里，旧口径全部报 INCREMENTAL 或 MARGINAL，新口径 29 个降为
   NO_INCREMENT、1 个（`window=24`）降为 MARGINAL。旧表里 `window=44` 报
   INCREMENTAL、下界 +0.0196；修正后是 −0.0090，**符号反了**。那不是一个
   「边界上的一两天」，是一段 30 个窗口宽的假阳性区间。
2. **`n_eff` 开始随配置变**：12 → 6 → 2 → 1。它现在和 `truncated_days`
   说同一件事的两个方面（几天承载证据 / 这几天值几个独立样本），不再互相打脸。
3. **`window=5` 的 INCREMENTAL 站得住**（+0.2439）。修正没有把结论一律打成
   NO_INCREMENT——它只打掉了靠 iid 假设撑起来的那一段。

#### 五、验收

- [x] `test_rolling_window_overlap_is_not_iid`：`window=12` 时 `hac_lag == 0`
      而 `hac_lag_incremental == 11`，`n_eff_incremental < oos_days/3`，结论仍
      INCREMENTAL；`window=40` 时 `hac_lag_incremental == 39`、
      `n_eff_incremental_reliable is False`、结论 NO_INCREMENT、句子含「NW 不可用」
      **（该测试已删除，见 §9.20——它钉住的是一个把功效打没的配置）**
- [x] 三个 decision 测试文件全通过（新增 1 条）
- [x] `verify_pwin_e2e.py` 除 `reason` 多一句 NW 标注外逐位不变：真实账本
      `horizon=20`，`hac_lag` 与 `hac_lag_incremental` 都是 19（持有期主导），
      `n_eff=1`、`verdict_incremental=NOT_TESTABLE` 不变；新增字段
      `hac_lag_incremental=19`
- [x] `probe_shrink_unit.py --days 40` 的 [D] 段逐字不变
- [x] `scripts/probe_incremental_lag.py`：上两表可复现

#### 仍然没解决

- **`window=20` 报 INCREMENTAL，而 `n_eff=2` 且 `n_eff_reliable=False`。**
  增量侧没有 `significance` 那样的三态结论：`verdict` 只读 `lcb` 的符号，
  而那个 `lcb` 的 `se` 是在 NW 边界上估的。句子已经带了标注，但只读结论标签
  的人仍会被误导。补一个 `significance_incremental` 是下一步，不是这一节。
- **`lag_inc` 是上界，不是估计值。** 取 `window-1` 覆盖的是「最多能重叠多少天」；
  非零改善只集中在 `k` 个相邻日时，真实依赖长度短于它。Bartlett 权重让两者
  差别不大（`window=53` 时 lag=1 与 lag=10 给出同号下界），但这是运气，不是保证。
- **`by_state` 的阻塞点没动**：`verify_pwin_e2e.py` 建卡时 `forecast={"pwin": …}`
  的键列表里仍没有 `significance`（约 128–131 行），那份账本的 `by_state`
  第一份证据会是空的。
- **`settle.score` 的滞后仍只按持有期。** 它计分的 `p_win` 本身是滚动估计，
  同一族重叠还在，但卡上没有记 `window`（建卡出处里只有 `n` / `days` / `kappa`），
  事后算不出那个滞后。要修得先让 `window` 进出处。
- **e2e 的字面量不是回归保证。** 见 §9.17 第五节的注：它随 `data/` 漂移。

### 9.20 判据选错了：`r1` 不是 HAC 的对象（2026-11-05）

§9.18 用「逐日改善的 `r1=+0.98`」证明 `lag=0` 不成立，把滞后改成 `window-1`。
推理链每一步都对，错在第一块砖：**它量的是账本，不是零假设。**

#### 一、HAC 校的是均值的方差，判据只有尺寸和功效

`se` 的正确性不由「自相关有多长」回答，由两件事回答：

- **尺寸**：没有增量时，`lcb > 0` 的频率应 ≤ 5%；
- **功效**：真有增量时，`lcb > 0` 的频率。

自相关只是输入，而输入取决于你拿哪份账本去量。§9.18 的账本同时满足两个
抬高 `r1` 的条件，且都不是零假设的性质。

#### 二、四种账本上的 `r1`（`window=12`，n≈59）

| 账本 | `r1(d)` | `r2(d)` | 说明 |
|---|---|---|---|
| 平稳 + 确定性（每天恰好 6/20） | +0.964 | +0.921 | `y` 是常数，只剩 `δ` 漂移 |
| **平稳 + 二项抽样** | **−0.100** | −0.186 | **零假设的真实形态** |
| 翻转 + 确定性（§9.18 用的那份） | +0.984 | +0.954 | 两个条件叠加 |
| 翻转 + 二项抽样 | +0.929 | +0.882 | 有信号，也有噪声 |

`r1` 跟的是「确定性」和「信号」。第一行说明：只要每天命中数相同，`d_i` 就
只剩 `δ_i = p_clim − p` 的缓慢漂移，`r1` 必然接近 1——**与 `window` 无关，
与重叠也无关**。第三行只是把制度翻转再叠上去。

第四行是另一件事：翻转让 `d` 持续为正，所以 `r1` 高。但那份持续性是**要检测
的信号**，不是要校正的噪声。用备择的依赖去定零假设的方差，等于把信号算进
误差里。

#### 三、尺寸与功效（`w=40`，各 200 次重复）

| 场景 | n | `lag=0` 拒绝率 | `lag=w−1` 拒绝率 |
|---|---|---|---|
| H0 平稳 0.3 | 57 | 4.5% | 0.0% |
| H0 平稳 0.3 | 199 | 0.0% | 0.0% |
| H1 翻转 0.9→0.1 | 57 | **100.0%** | **0.0%** |
| H1 翻转 0.9→0.1 | 199 | 100.0% | 100.0% |

`lag=0` 的尺寸本来就对（4.5%，不超 5%）；`lag=w−1` 没有改善它，却在这个
项目真实样本量（n=57，真实账本 n=17）上把功效清零。n=199 时两者都能拒绝，
所以这不是「`w−1` 永远错」，是「**它在样本量与账本同量级时必然错**」。

`n_eff` 也因此不是问题：平稳 + 抽样下它等于 `n` 是**正确读数**（这里说的是
`d` 全都有对照的情形；被窗口截断出来的恒等零另算，见 §9.22）。§9.18 说
「一个在 14 个配置上完全不变的数不可能是估计」——那个推理在「配置应当改变真值」时成立，而这里真值确实不随 `window` 变。

#### 四、撤回

- `walk_forward` 的 `lag_inc` 退回 `lag`（`horizon-1`）；
- `hac_lag_incremental` 字段**保留**，值等于 `hac_lag`。不合并字段，是因为
  下游按「每条序列各有自己的滞后」读过它——一次错误的滞后修正需要留下位置，
  否则下次还会有人在同一个键上改；
- 删除 `test_rolling_window_overlap_is_not_iid`，换成
  `test_incremental_lag_is_the_hold_overlap_not_the_window`：H=1 时断言
  `hac_lag_incremental == 0` 且 `n_eff_incremental == oos_days`，H=20 时两个
  滞后都是 19。旧实现给 11 / 39 与 6 / 1，必失败；
  （§9.22 把 `== oos_days` 改成 `== truncated_days`：`oos_days` 里有一部分是
  恒等零，不能进分母。）
- `scripts/probe_incremental_lag.py` 重写：[A] 四种账本的 `r1`、[B] 尺寸与
  功效、[C] §9.18 的翻转表复现。§9.17 那张表恢复为 `lag=0` 读数。

#### 五、顺带修掉的一处：被吞掉的测试

`test_rolling_window_overlap_is_not_iid` 插入时吃掉了下一个函数的 `def` 行，
第 616 行的 docstring 与三条断言变成了前一个函数里的死代码——`99 passed`
里有**一条根本没被收集**。恢复为
`test_median_basis_keeps_clim_as_a_separate_baseline` 后是 `100 passed`。

这不是格式问题：那条断言钉的是「median 口径的 `brier_clim` 与常数 0.25
分开报告」，正是 §9.3 那条恒等式的守卫。它可以被静默删掉一次，就能被删第二次。

#### 六、验收

- [x] 三个 decision 测试文件 `100 passed`（99 + 恢复的那条）
- [x] `probe_incremental_lag.py` 三张表可复现
- [x] `probe_truncation_power.py` 的表与 §9.17 恢复后一致
- [x] 真实账本（`horizon=20`）不受影响：`hac_lag` 与 `hac_lag_incremental`
      本来就都是 19，`n_eff=1`、`NOT_TESTABLE` 不变

#### 仍然没解决

- ~~**`significance_incremental` 仍然缺。** 这一节撤回的是滞后，不是那个不对称：
  `verdict_incremental` 只读符号，没有「测不了」这一档。§9.17 列的下一步成立。~~
  已落地，见 §9.21。
- **`lag=0` 的 4.5% 是单点估计。** 200 次重复的蒙特卡洛标准误约 1.5 个百分点，
  在 5% 附近分辨率不够；要定这个数得 2000 次以上。
- **探针只对 H=1 做了功效表。** H≥2 时两种滞后在大样本上同量级，差别被结果
  重叠盖住；这一段没有单独的功效读数。
- **`settle.score` 的 `window` 仍未进出处**，所以它那一族的滞后事后算不出来。
- **§9.19 是空号。** §9.16 的「仍然没解决」把 κ 的单位预留给 §9.19，那一节
  还没写；本节按 §9.20 编号是为了不占那个号。读者从 §9.18 翻到 §9.20 会看到
  一个洞，那是真的。

### 9.21 增量检验的可辨识性：`significance_incremental`（2026-11-05）

§9.20 撤回的是滞后，不是 §9.17 留下的那个不对称：第一个检验有
`verdict × significance` 两维（符号 / 有没有分母），第二个检验只有 `verdict`。
`lcb > 0` 只说明「这批样本里近端赢了」，不说明「这个赢法可复现」。

#### 一、反例：下界 +0.02069，等效独立样本 1

账本：30 个决策日 18/20 命中，接 30 个决策日 2/20 命中（90% → 10%），
`window=5`。数据一个字不改，只把持有期从 1 天换成 20 天：

| 字段 | `horizon=1` | `horizon=20` |
|---|---|---|
| `oos_days` | 57 | 38 |
| `hac_lag_incremental` | 0 | 19 |
| `n_eff_incremental` | 57 | **1**（`n // 20`，`reliable=False`）|
| `improvement_vs_clim` | +0.14132 | +0.12023 |
| `skill_vs_clim_lcb` | +0.10726 | +0.02069 |
| `verdict_incremental` | INCREMENTAL | INCREMENTAL |
| `significance_incremental` | PASS | **UNKNOWN** |

> §9.22 之后 H=1 那一行的 `n_eff_incremental` 是 **52**（对照天数），不是 57；
> `PASS` 不变。H=20 那一行本来就是 1。差别不在结论，在分母的来源。

H=20 时旧句子是：「相对展开基线改善 +0.12023，下界 +0.02069 > 0：
**近端窗口含有远端没有的信息**（等效独立样本 1，NW 不可用，取不重叠窗口数；
只有 33/38 天被截断…）」。括号里已经写了「等效独立样本 1」，可句子本身是
结论式的——读者读的是句子，不是括号。

这正是 §9.17 自己定下的界线被越过：句子必须和证据等长。`n_eff=1` 时，
`+0.02069` 的下界没有分母，能支持的只有「方向为正」。

#### 二、为什么不给 `verdict_incremental` 加第五态

这里的理由曾经是「两种状态的补救方式相反」：`NOT_TESTABLE` 改 `window`
（配置），样本不够就攒样本（数据）。**这个理由被 §六自己的数据推翻了**——
真实账本（17 个可评估日 / H=20）的 `NOT_TESTABLE` 只能靠攒到约 600 天才解除，
和「样本不够」是同一条路。两个障碍经常同时成立，补救方式不是可靠的判据。

真正的区别在**有没有数字**：`NOT_TESTABLE` 时 `p ≡ p_clim`，改善、下界、`n_eff`
全是恒等式的产物，必须撤掉（§9.17）；样本不够时数字是真的，只是没有分母，
撤掉会把「测过、不可判定」伪装成「没测过」。一个状态既要撤数字又要留数字，
就是两个状态。所以状态留在 `verdict`，可辨识性进 `significance`——
与第一个检验的 `verdict × significance` 同构：`RELIABLE × UNKNOWN` 早就是
合法组合（真实账本此刻就是），`INCREMENTAL × UNKNOWN` 没有理由不行。

#### 三、修法

`_incremental_verdict` 返回 `(verdict, reason, significance)`，第三个值直接调
`settle.significance_of(lcb, n_eff, n_eff_reliable)`——与第一个检验同一个函数、
同一个 `MIN_NEFF=30`。`UNKNOWN` 时三个分支共用一句同构的话：只报符号，不报
结论（「近端含有远端没有的信息」和「没测出增量」都需要分母，一个都没有）。

`walk_forward` 把 `sig_inc` 写进 `significance_incremental`，并在「窗口盖住
全部已可见日」的分支里把它一并撤成 `UNKNOWN`——撤数字和撤状态写在同一个分支，
分开写的地方迟早只改一半。`_insufficient` 与 `estimate` 的冷启动分支同样返回
`UNKNOWN`：没测不是没通过。

#### 四、顺手修掉：探针的判据单位和生产不同

`probe_incremental_lag.py` 的 [C] 表自己折算结论，用的是
`e0[2] / brier_clim < MIN_INCREMENTAL_EFFECT`——**相对**下界对**绝对**门槛。
`MIN_INCREMENTAL_EFFECT = 0.001` 是 Brier 单位，除以 `brier_clim≈0.28` 之后
同一个改善小 3.6 倍，于是 `window=51/52` 被报成 INCREMENTAL，生产报 MARGINAL。
探针和被探的东西分叉，比没有探针更糟：它会把「判据变了」误报成「数据变了」。

改成 `_verdict_of(lcb_abs)`，并在 `lag=0` 那一列断言它与
`wf["verdict_incremental"]` 逐位相等。表随之变了：51/52 从 INCREMENTAL 变
MARGINAL。翻转计数也变了——旧文案写死「28 个窗口从 INCREMENTAL 变
NO_INCREMENT」，按修正后的单位重算是 **26**（「掉出 INCREMENTAL」、含 MARGINAL
则是 27）。那个 28 确实是单位错误的产物：用 `lcb / brier_clim` 折算，同一份
账本正好给出 28。

#### 五、iid 对照：§9.17 的结论不是确定性账本的怪癖

§9.20 的教训是「确定性账本量出来的东西不一定是零假设的性质」。§9.17 的覆盖度
结论建立在同一份确定性账本上（每天恰好 18/20），所以它也需要对照。
`probe_truncation_power.py` 现在对同一批 `window` 再跑 60 份二项抽样账本
（每天按 0.9 / 0.1 抽 20 个结果）：

| `window` | 截断/评估 | 确定性账本 | iid 分布 |
|---|---|---|---|
| 5 | 52/57 | INCREMENTAL | INCREMENTAL 100% |
| 20 | 37/57 | INCREMENTAL | INCREMENTAL 100% |
| 40 | 17/57 | INCREMENTAL | INCREMENTAL 100% |
| 50 | 7/57 | INCREMENTAL | INCREMENTAL 53% / MARGINAL 47% |
| 51 | 6/57 | MARGINAL | MARGINAL 100% |
| 54 | 3/57 | NO_INCREMENT | MARGINAL 48% / NO_INCREMENT 52% |
| 56 | 1/57 | NO_INCREMENT | NO_INCREMENT 100% |

12/12 个窗口里，确定性账本的结论是 iid 分布的众数（含并列）。`window=50` 与
`54` 本来就在门槛附近，落在哪一边不重要；重要的是**翻转点跟着覆盖度走，
不跟着账本有没有噪声走**。

#### 六、`NOT_TESTABLE` 的药方：改配置还是攒样本

这一节差点以一句错药方收尾。原句是：

> window=250 ≥ 最多 17 个已可见决策日 …… **要让这个检验可判定，window 必须小于可评估日数。**

它把 `NOT_TESTABLE` 的两个可能障碍当成了一个：

1. `window` 盖住全部已可见日 ⇒ 两个估计量恒等（**配置**，改 window 才能开始判定）；
2. 已可见日本身不够 ⇒ 就算 window=1，`n_eff` 也到不了 30（**样本**，只能攒）。

在 e2e 账本（39 个决策日 / 17 个可评估日 / H=20；行数随 `data/` 漂移）上把 window 逐个缩下去：

| `window` | 截断/评估 | `improvement_vs_clim` | `n_eff` | `verdict_incremental` | `significance_incremental` |
|---|---|---|---|---|---|
| 250 | 0/17 | —（撤数字）| — | NOT_TESTABLE | UNKNOWN |
| 20 | 0/17 | —（撤数字）| — | NOT_TESTABLE | UNKNOWN |
| 12 | 5/17 | +0.00001 | 1 | NO_INCREMENT | UNKNOWN |
| 8 | 9/17 | −0.008432 | 1 | NO_INCREMENT | UNKNOWN |
| 4 | 13/17 | −0.035012 | 1 | NO_INCREMENT | UNKNOWN |
| 1 | 16/17 | −0.070247 | 1 | NO_INCREMENT | UNKNOWN |

**按原句做，状态从 `NOT_TESTABLE` 变成了 `NO_INCREMENT`，`n_eff` 还是 1。**
这不是「解除」，是把「没测」换成一个看起来像结论的词——而且换出来的那个词
更假：它读起来像「近端确实没有增量」，实际是 16 天的噪声除出来的符号。
`significance_incremental` 仍然是 UNKNOWN，但读者先看到的是 verdict。

修法：`_not_testable_remedy(days, hac_lag)` 先算再开药方。判据是构造值
`n // (lag+1)`（MA(H−1) ⇒ `n_eff ≈ n/H`）：

- `max(1, days // (lag+1)) >= MIN_NEFF` ⇒ window 是唯一障碍，原句成立；
- 否则 ⇒ 句子改写成「把 window 缩到 1 也只能让检验跑起来：17 个可评估日、
  MA(19) ⇒ 等效独立样本约 1 < 30，结论仍然不可判定。要让它可判定，按 MA(H−1)
  的构造值推算需要约 600 个可评估日（30×20），不是改配置能解开的」。

这个判据只在 `NOT_TESTABLE` 分支跑，不 gate 任何东西；它改的是句子，不是数字。
同一份 40 天账本换成 H=1（38 个可评估日、`lag=0`）时，`30 ≤ 38`，句子回到
原样——差别不在数据，在 `H`。

**600 是模型值，不是测出来的门槛。** 它来自 `30 × H`，前提是 `n_eff ≈ n/H`。
单条路径上的 NW 读数抖得很厉害：一条 290 天回放里 267 个可评估日给出
`n_eff=22`（约 0.065–0.09/天，未截断读数外推到 30 只要 350–390 天），
而 78 天给 16、98 天给 9——同一族路径，比值差一倍。所以句子里的「约」不能
去掉，也不能把 600 当成「过了就一定能判定」的承诺；它只回答「现在缩 window
有没有用」。§9.22 的截断按同一条 MA(H−1) 模型收紧分母，门槛仍按 600 表述；
350–390 是未截断的 NW 读数，不作为门槛。

#### 七、验收

- [x] 三个 decision 测试文件 `102 passed`（101 + §六新增的一条）
- [x] 三态各钉一条：H=20 ⇒ `INCREMENTAL × UNKNOWN`；H=1 ⇒ `INCREMENTAL × PASS`；
      窗口盖住全历史 ⇒ `NOT_TESTABLE × UNKNOWN`
- [x] `NOT_TESTABLE` 的句子按 `n // (lag+1)` 是否够 30 分流：H=20 指向样本
      （「不是改配置能解开的」），H=1 指向 window。两条都钉了断言，
      另加一条反证：window=1 让状态变成 `NO_INCREMENT`，`n_eff` 仍是 1。
- [x] `estimate` 与 `walk_forward` 报同一个 `significance_incremental`，
      且它随卡封存（`forecast.pwin`）——接口与卡不能各说一句话
- [x] e2e（`n=386 hits=79`，随 `data/` 漂移）：`verdict_incremental=NOT_TESTABLE`、
      `significance_incremental=UNKNOWN`，句子点名 600 个可评估日
- [x] `probe_incremental_lag.py` [C] 与生产逐位一致（带断言）
- [x] `probe_truncation_power.py` iid 对照 12/12

#### 仍然没解决

- **真实账本上这个字段恒为 UNKNOWN，而且不是改配置能解除的。** `horizon=20`、
  `oos_days=17` ⇒ `NOT_TESTABLE`；window 缩到 1 也只是把标签换成
  `NO_INCREMENT`，`n_eff` 仍是 1（§六的表）。`n_eff` 要过 30，按 MA(H−1) 的
  构造值推算需要约 600 个可评估日（实测比值更高时会少一些，见 §六末的注）。
  这一节是给账本长大以后准备的。
- **`verdict_incremental` 与 `significance_incremental` 前端都没显示。**
  两个字段进了 `forecast.pwin` 封存，但决策页只渲染 `significance`（评分卡的）。
  封存解决的是「事后不能改写」，没有解决「读者当下看得到」——而 §9.17 的
  出发点正是「读者读的是句子」。
- **`suggest()` 有 `window`，HTTP 层没透传。** 签名是
  `window: int = DEFAULT_WINDOW`（`pwin.py`），`api/decision.py` 的建卡入口
  没有把这个入参接出去。现在这是对的：在 17 个可评估日上开放 window 只会
  生产 `NO_INCREMENT`。账本过 600 天之后才需要它——届时补的是 API 参数，
  不是函数签名。
- **`MIN_NEFF=30` 没有对增量序列单独标定。** 它借自水平序列（§9.14），在这里
  只决定标签、不决定任何数字；但「30 对差值序列是否偏严」没有读数。
- **`n_eff_incremental_reliable` 没进卡。** 卡上有 `significance_incremental`
  （结论）和 `n_eff_incremental`（数），但读不出那个数是不是构造值。按 §9.16
  的规则它属于「能由状态推出」（`UNKNOWN` 就是它的读数），所以没加。
- **`settle.score` 的 `window` 仍未进出处**（§9.20 遗留）。

### 9.22 `n_eff_incremental` 的上限：证据越少，分母越大（2026-11-05）

§9.21 修的是句子（`significance_incremental` 这个标签），这一节修的是那个
标签的**分母**。只修句子不够：状态机里还有一个洞，能让那句「600 天才可判定」
和它自己的数字互相否定，而且在默认配置下就会命中。

#### 一、反例：1 个对照日 = 256 个独立样本

逐日改善是 `d_i = (p_clim,i - y_i)² - (p_i - y_i)²`。窗口没有截断第 i 天时
`p_i == p_clim,i`，于是 `d_i == 0`——**恒等零**，不是抽样噪声，是窗口配置的
恒等式：给它任何一份账本，这些天都恒等于 0。

它们留在序列里是对的（`mean` 要覆盖全部评估日，见 §9.17），但 NW 把它们
当成独立观测：方差被零稀释 ⇒ `se ≈ se_iid` ⇒ `n_eff = n·(se_iid/se)² ≈ n`。
`n_eff` 于是随「没比过的天数」一起长。

构造序列 `d = [0]*255 + [0.02]*k`：

| 非零日 `k` | `n_eff` | `lcb` | `significance` |
|---|---|---|---|
| 1 | **256** | −0.00005 | FAIL |
| 2 | 132 | −0.00010 | FAIL |
| 6 | 48 | −0.00025 | FAIL |

真实尺度上（278 个决策日、256 个可评估日、常数预测器、默认 `window=250`、
6 天对照）：`n_eff=57`、`significance_incremental=FAIL`。另一条真实数据回放
（290 个决策日、267 个可评估日、默认 `window=250`、17 天对照）给 `n_eff=54`；
把 window 依次推到 252 / 254 / 255，`n_eff` 是 79 / 267 / 250——
**分母一路涨到等于可评估日数，对照天数却在减少**。而 §9.21 §六说这个判定
要 600 天。两句话不能同时为真，先到的是后者——而且方向相反：账本越短，
越容易拿到一个「没有增量」的结论。

#### 二、修法：分母截到对照天数

`_identifiable_sample(n_eff, truncated_days, lag)`，上限是
`max(1, truncated_days // (lag+1))`。

- **不能只截到 `truncated_days`。** 对照日按决策日连续排列（`visible` 单调
  增长，一旦超过 window 就一直超过），持有 H 天时相邻两天重叠 H−1 天，
  独立样本至多 `k/H`。256 天账本上 `k=36`、NW 给 17，截到 36 仍会把 1.8 个
  独立样本说成 36 个。
- **更弱的截法连方向都保不住。** `n_eff_nw` 随 window **非单调**：
  57 → 35 → 26 → 22 → 19 → 18 → 17（`k=6,11,16,21,26,31,36`）。取
  `min(n_eff, k)` 之后序列先升后降，而「证据越多、分母越大」正是要守的东西。
- **`k // (lag+1)` 不是新模型。** 它与 `effective_sample` 的退化值、
  `_not_testable_remedy` 的推算是同一条 MA(H−1)：长程方差 ≈ H·γ0 ⇒
  `n_eff ≈ n/H`。H=1 时它退化为 `k`，所以 `horizon=1` 的账本行为不变。

截断只改 `n_eff_incremental` 与 `significance_incremental`，不改
`improvement_vs_clim` / `lcb` / `verdict_incremental`：符号和量级仍然是全部
评估日上的读数，只有「这个读数有没有分母」变了。也不翻转
`n_eff_incremental_reliable`——它回答的是「NW 可不可用」，不是「有没有被截」；
`effective_sample` 里的 `min(n, …)` 同样不翻转它。截断后的值读作**上界**。

判定函数自己也守这条不变量：`_incremental_verdict` 在算 `significance_of`
之前再截一次。只靠在 `walk_forward` 传对参数，下一个调用点就会绕过去。

#### 三、验收

- [x] `truncated_days=1` ⇒ `n_eff_incremental ≤ 1`、`significance_incremental != FAIL`
- [x] 同样本下 `n_eff` 随 `truncated_days` 单调不减（6/21/41/61/81/101 天）
- [x] `n_eff_incremental ≤ truncated_days // (lag+1)` 写成断言
- [x] 278 个决策日 / 256 个可评估日 / 默认 window：`FAIL` → `UNKNOWN`，
      `n_eff` 57 → 1，句子仍写「6/256 天被截断」
- [x] 真实 290 天回放（267 个可评估日 / 17 天对照）：`FAIL` → `UNKNOWN`，
      `n_eff` 54 → 1；同一条回放上 window=250/252/254/255 的旧读数是
      54/79/267/250，新读数全是 1
- [x] 三个 decision 测试文件 `107 passed`（102 + 5）
- [x] e2e 不变：真实账本 17 个可评估日仍是 `NOT_TESTABLE × UNKNOWN`

#### 仍然没解决

- **`improvement_vs_clim` 仍被恒等零摊薄。** 256 天里只有 6 天承载证据，
  均值是 6 天的效应除以 256。分母修好了，均值还没有：读者看到 `+0.00004`
  仍可能读成「平均每天改善 0.00004」，而不是「6 天里有改善，其余 250 天
  没有对照」。正解是只在对照片段上算均值与 NW，另立
  `improvement_on_contrast_days`；那要改字段语义，单独做。
- **`settle.score` 没有 `window`**，不存在「`p ≡ p_clim`」的构造，所以没有
  这个机制；但它的 `n_eff` 仍可能在低方差序列上被高估，没有单独标定。

### 9.23 核选错了：`se` 的偏差在分子上，且不随 n 收敛（2026-11-05）

§9.22 修的是分母（`n_eff_incremental` 的截断上限）。这一节修的是**分子**：
`se` 本身系统性偏小，`PASS` 的错误率因此是标称值的两倍左右。四条验收
（单调、截断、标注一致、`107 passed`）全部成立也发现不了它——它们约束的是
顺序与文案，不约束尺度。

#### 一、反例：带宽固定成模型记忆长度

`hac_lcb` 原本用 Bartlett 核，带宽恒等于 `lag+1 = H`。持有 `H` 天的卡，
逐日改善是等权 MA(H−1)：`γ_k = γ_0·(H−k)/H`（正是重叠 H 日收益的自协方差
形状）。Bartlett 权重 `1 - k/H` 在这个形状上的概率极限是

    LRV_hat → (2/3 + 1/H)·LRV

也就是 `se` 恒为真值的 `sqrt(2/3) = 0.817` 倍。**它不随 n 收敛**：带宽固定、
模型记忆固定，加多少样本都补不上那一阶被压掉的协方差。用发布代码实测
（等权 MA(19)、`H0` 真值 0、400 次重复）：

| n | 核 | `se`/真值 | `P(lcb>0)` | `n_eff` 均值 |
|---|---|---|---|---|
| 250 | bartlett | 0.755 | **11.0%** | 20.6 |
| 250 | truncated | 0.911 | 7.8% | 14.8 |
| 600 | bartlett | 0.778 | **8.8%** | 47.4 |
| 600 | truncated | 0.942 | 6.0% | 33.0 |
| 1000 | bartlett | 0.799 | **7.8%** | 77.1 |
| 1000 | truncated | 0.972 | 4.5% | 52.6 |

`n_eff = n·(se_iid/se)²` 被同一份偏差放大 1.44 倍（47.4 → 33.0）。
这就是「同一个门槛 30，两种含义」的来源：旧读数下 30 约等于 20 个真独立样本。

#### 二、修法：截断核 + `LRV ≥ γ0`

`LRV = γ_0 + 2Σ_{k=1..lag} γ̂_k`，`γ̂_k` 的分母取 `n-k`；`lag` 不是「取够
大」，它就是重叠长度，所以末阶不能被权重压掉。另外 `LRV` 下界取 `γ_0`：
重叠序列在 `k < H` 上 `γ_k ≥ 0`，`LRV ≥ γ_0` 是模型内的硬下界；顺带挡住
`LRV ≤ 0 ⇒ se = 0 ⇒ lcb = mean` 这个「零误差」退化（iid 压力测试里
n=250 有 2.5% 的重复踩到）。

**不是「让带宽随 n 增长」。** `b ≈ 1.1447·n^(1/3)` 在 n=1000 只到 11，
远小于 H−1=19，size 反而更差：n=600 给 14.3%、n=1000 给 12.3%。
MA(19) 要求 `b ≥ 19`，而 `n^(1/3)` 要到 n≈4600 才够——这正是
「600 天可判定」与「H=20」不兼容的地方。

**不在 `pwin` 里单独修。** `settle._daily_clustered_lcb` 走同一个函数，
`settle.score` 的 `skill_lcb` 因此有同一个洞，而它连 `reliable` 标注都没有。
换核在 `hac_lcb` 里做，两条路径一起变。

#### 三、验收

- [x] `size` 测试进 CI：等权 MA(19)、n=600、400 次重复，`P(lcb>0) ≤ 8%`
- [x] 旧核偏差写成断言：`se`/真值 = `sqrt(2/3) ± 0.04`，且 size ≥ 7%
      （默认值被改回去时两条一起红）
- [x] `LRV ≥ γ0`：交替正负序列上 `se ≥ se_iid`
- [x] **已知边界写成断言**：漂移 DGP（MA(19)+AR(1) ρ=0.995）下
      truncated / bartlett 的 size 都 > 20%。这条测试断言的是「没修好」
- [x] 真实账本回放（344 个决策日 / 322 个可评估日）四个切片的 verdict 全不变，
      `n_eff` 52/54/49/50 → 44/49/45/49
- [x] 句子补上截断前的读数：`等效独立样本 3（按对照天数折算；NW 读出 32）`
- [x] 句子补上未被摊薄的均值：`322 天摊薄；只看有对照的 72 天是 +0.00039`
- [x] 决策四个测试文件 `113 passed`

#### 换核之后，仍有四处没解决

- **漂移。** 真实账本的逐日改善可以有几个月量级的持续期（战术胜率有制度性
  高低）。此时真 `LRV` 远大于 `H·γ0`，而任何带宽 `b ≤ H` 的核都看不见它：
  同一条 DGP 上三种核的 size 是 28–37%，`n_eff` 却照样报得出 30 以上。
  「换核把 size 修到 5%」只对「记忆恰为 H−1」成立。
- **真实账本上偏差没有模型那么大。** 322 天回放里 `LRV_bart/LRV_trunc = 0.907`，
  `n_eff` 只掉 10%（54 → 49），不是 1.44 倍。原因是实际 `γ_k` 衰减比三角快
  （`n_eff = 322/49 ⇒ H_eff ≈ 6.6`，不是 20）。所以「按 1.5 倍修正历史读数」
  会过度保守；2/3 是**模型内**的上界，不是实测值。
- **单位混用没修（§9.24 已修）。** `truncated_days` 数决策日，`lag+1` 数交易日。每 3 个
  交易日发一张卡时上限低估 3 倍（`scripts/probe_neff_units.py`：16 vs 48），
  `_not_testable_remedy` 的「约 630 个可评估日」同样多报 3 倍。门禁想要的
  恰是稀疏发卡，所以这条路只会越走越错。
- **`reliable` 仍然答非所问。** 它回答「NW 可不可用」，句子拿它回答「这个数
  是不是构造值」。回放四个切片全是 cap 值（13 = 262//21、3 = 72//21）配
  `reliable=True`；现在只在句子里补了原读数，字段没拆。正解是
  `n_eff_source ∈ {nw, fallback, contrast_cap}`，三态三句话。

#### 更正（2026-11-07）：上面「一、」的闭式推错了

本节正文保留原样（带日期的历史记录），只更正算式——**结论不变**，错的是
`(2/3 + 1/H)` 这个手推式。

「一、」里写

    LRV_hat → (2/3 + 1/H)·LRV

`1/H` 应当是 `1/(3H²)`。Bartlett 权重 `1−k/H` 作用在 `γ_k = γ₀(H−k)/H` 上：

    LRV_hat → [H + 2Σ_{k=1..H−1}(1−k/H)(H−k)] / H² · LRV
            = (2/3 + 1/(3H²))·LRV = 267/400·LRV = 0.6675·LRV    (H = 20)

手推式把余项当成了 `O(1/H)`：实际 `Σ_{k<H}(1−k/H)(H−k) = (H−1)H(2H−1)/6`，
除 `H²` 后只剩 `1/(3H²)`（H=20 时是 1/1200，不是 1/20）。

同一段紧接着写的 `sqrt(2/3) = 0.817` 因此与本式**自相矛盾**：`2/3 + 1/H` 给 LRV
0.717，而 `sqrt(2/3)` 隐含的 LRV 是 2/3 = 0.667。正确对应是 267/400 →
`sqrt(267/400) = 0.81697`；`sqrt(2/3) = 0.81650` 只是 `H → ∞` 的连续极限（差在
第四位小数，所以那个数读起来「没错」，但它是极限，不是这支核在 H=20 的值）。

已按 267/400 修正：`settle.py:373–384` 的核注释、`test_decision_hac_size.py`
的 docstring 与断言常量。本节「三、验收」第 2 条写的是 `sqrt(2/3) ± 0.04`，
测试里的常量现为 `sqrt(267/400)`（容差 0.04 未动，行为不变）。实测读数见
`scripts/probe_hac_size.py`：`bartlett_q` 在 `ma` 列上的 size 是 8.6–12.2%
（无条件口径，n=1000 / 600 / 250，reps=2000），本轮补的 n=326 是 11.5%。

### 9.24 单位：分子数决策日，分母数交易日（2026-11-05）

§9.22 把 `n_eff_incremental` 截到「对照日数」，§9.23 换掉了 `se` 的核。
两节都默认 `k // (lag+1)` 里两个数是同一个单位。它们不是：

    truncated_days  决策日（walk_forward 每评估一天加一）
    lag + 1         交易日（= horizon_sessions）

只有「每个交易日都发卡」时两者相等——而那正好是门禁**不**想要的那种节奏。
发卡越稀疏，这个上限低估得越多，且方向单调：**你越按它的要求做，它错得越狠。**

#### 一、反例

同一份账本、同一个交易日历，只改发卡间隔（`scripts/probe_neff_units.py`，
H=21、window=60、420 个决策日、每张卡 20 个 pick、命中数 iid）：

| 节奏 | 对照日 k | 交易日跨度 | 旧上限 | 新上限 | 真上限 |
|---|---|---|---|---|---|
| daily | 337 | 337 | 16 | 16 | 16 |
| sparse×3 | 337 | 1009 | 16 | **48** | 48 |
| sparse×5 | 337 | 1681 | 16 | **80** | 80 |

第二个出口是 `NOT_TESTABLE` 的补救句。门槛 `MIN_NEFF × (lag+1)` 的单位是
**交易日**（30 个独立块 × H 天），与发卡节奏无关；旧句子把它当成「可评估日」
印出来，于是 daily 和 sparse×3 拿到同一句「约 630 个可评估日」，而 sparse
真正要的是 210 张卡。

#### 二、修法：给 `walk_forward` 一个交易日历

`walk_forward(..., sessions=...)`（`estimate` / `suggest` 透传，API 两处调用点
都传 `bars.sessions(data_dir)`——否则 `/pwin` 与建卡会按两种单位报同一个数）。

- `_trading_span` 用二分求首末对照日之间的交易日数，取代「决策日数」；
  日历缺失、越界、或跨度小于决策日数（日历与账本对不上）时返回 `None`。
- `_identifiable_sample(..., trading_days=)`：跨度 ≥ 决策日数才采信，否则退回
  旧口径。**退回只会在稀疏账本上低估，不会高估**——缺日历是「没算对」，不是
  「放宽门槛」，所以不需要拦截请求。
- 结果里加两个字段：`truncated_trading_days`（这段对照日跨了几个交易日）与
  `n_eff_cap_unit ∈ {trading_days, decision_days, None}`。读到 `decision_days`
  就意味着这个分母可能低估；一天都没截断时是 `None`（没算 ≠ 按决策日算）。
- `_not_testable_remedy`：门槛写成交易日，再用实测节奏折回卡数——
  `需要约 600 个交易日（按当前每 3.0 个交易日一张卡，约 201 个可评估日）`。
- 判据函数 `_incremental_verdict` 也接这两个跨度，不只靠 `walk_forward`
  传对参数：只修调用点，下个调用点就会绕过去。

#### 三、验收

- [x] 三种节奏的上限 = `交易日跨度 // H`（16 / 48 / 80），旧读数与 `step` 无关
- [x] 逐日发卡时有无日历**逐位相同**（12 个字段全等）：真实回放 322 个可评估日、
      `median/abs × window=60/250` 四个切片 `n_eff_incremental` 全是 13/13/3/3，
      `verdict_incremental` 全是 `NO_INCREMENT × UNKNOWN`——**换单位没有翻动任何结论**
- [x] 真实行情上改成每 3 个交易日一张卡：`w=30` 上限 3 → 11、`w=60` 2 → 6；
      `w=250` 的补救句从「600 个可评估日」变成「600 个交易日 ≈ 201 张卡」
- [x] 无日历时 `n_eff_cap_unit="decision_days"`，读数与改动前逐位相同
- [x] 新增 `tests/test_decision_neff_units.py`（7 条）；把上限公式改回决策日，
      其中 3 条立刻红
- [x] 决策五个测试文件 `120 passed`（113 + 7）

#### 四、这个修法没有解决什么

- **门槛没有变松，只是换了单位。** `n_eff` 由「经过的交易日 ÷ H」决定，
  不由卡数决定：稀疏发卡不减少单位时间里的证据，只减少卡数。`H=20`、每年
  250 个交易日 ⇒ 12.5 个独立块／年，要到 30 仍是 2.4 年。所以单位修对之后，
  面板上那个判定**依然在两年内出不了结论**——该动的是 H，或者把它撤下来。
  修单位只是让这句话第一次说得准确。
- **`window` 仍以决策日计数。** `sparse×3` 的 `window=60` 覆盖 180 个交易日，
  与 daily 的 60 天不是同一段时间。这是**配置语义**（§9.16 特意选的），不是
  统计量算错；要统一得改 API 契约，不在本节范围内。
- **`reliable` 仍答非所问**（§9.23 最后一条）：回放四个切片全是 cap 值配
  `reliable=True`。`n_eff_source ∈ {nw, fallback, contrast_cap}` 没做。
- **`improvement_vs_clim` 的均值仍被恒等零摊薄**（§9.22）：句子已补上
  「只看有对照的 k 天」的折算，字段本身没拆。

### 9.25 「30 块」是结构问题，不是时间问题：H 扫描（2026-11-05）

§9.24 的处方是「把 H 降到 8 以下」。那条处方默认 H 是可以为了功效随便拧的
参数。H 是策略的**持有期**：改 H 就是改预测任务（5 日胜率能不能预测，与 20 日
胜率能不能预测是两个命题），不是把同一件事量得更准。所以本节对每个 H 都用
真实行情**重新结算一遍**，不是把同一份账本除以不同的 H
（`scripts/probe_horizon_budget.py`）。

#### 一、30 块要多少日历

`n_eff = 交易日跨度 // H`，所以 30 块要 `30·H` 个**被截断**的交易日。窗口
覆盖全部可见日时两个估计量恒等、一天对照都没有，预热期 `w` 不进证据：

    T ≥ 30·H + w

| H | 30·H | 一年（250 个交易日）够吗 |
|---|---|---|
| 20 | 600 | 不可能，窗口再短也不行 |
| 10 | 300 | 不可能 |
| 5 | 150 | 够，还剩 100 天留给窗口 |

一年预算能容纳的最大 H = `250/30` = 8，且窗口必须为 0。**默认 `window=250`
就是一年的预热期**：§9.24 的「2.4 年」没含这段，从零部署要 3.4 年。

从**当前账本**（322 个可评估日，`w=60` 已有 262 个对照日）算，还差：

| H | window | 需要 T=30H+w | 已有 | 还差 |
|---|---|---|---|---|
| 20 | 60 | 660 | 322 | 338 天 ≈ **1.4 年** |
| 20 | 250 | 850 | 322 | 528 天 ≈ 2.1 年 |
| 10 | 60 | 360 | 342 | 18 天 ≈ 1 个月 |
| 10 | 250 | 550 | 342 | 208 天 ≈ 10 个月 |
| 5 | 60 | 210 | 352 | 0，已越过（58 块） |

这张表把「等两年」换成了一个可执行的实验：**H=10、`window=60` 的卡再发
18 个交易日就能判定**，代价是换持有期——那是产品决定，不是统计决定。

#### 二、真实账本（366 个交易日，2025-05-07 … 2026-11-05）

| H | window | 可评估 | 对照日 | 块数 | `n_eff` | 改善 | `se` | `verdict × significance` |
|---|---|---|---|---|---|---|---|---|
| 20 | 60 | 322 | 262 | 13 | 13 | −1.77e−04 | 1.72e−03 | `NO_INCREMENT × UNKNOWN` |
| 20 | 250 | 322 | 72 | 3 | 3 | −3.55e−04 | 2.25e−04 | `NO_INCREMENT × UNKNOWN` |
| 10 | 60 | 342 | 282 | 28 | 28 | −1.56e−03 | 1.75e−03 | `NO_INCREMENT × UNKNOWN` |
| 10 | 250 | 342 | 92 | 9 | 9 | −3.04e−04 | 2.04e−04 | `NO_INCREMENT × UNKNOWN` |
| 5 | 60 | 352 | 292 | 58 | 58 | −9.28e−04 | 9.47e−04 | `NO_INCREMENT × **FAIL**` |
| 5 | 250 | 352 | 102 | 20 | 20 | −4.10e−05 | 8.86e−05 | `NO_INCREMENT × UNKNOWN` |

三条读数：

- **H=20 上这份数据最多 18 块**（366 ÷ 20）。30 块不是「还没攒到」，是这份
  历史**取不到**：再发 338 个交易日（1.4 年，`w=60`）才够。
- 对照日 `72 = 322 − 250` 就是预热期的直接读数：默认窗口下第一年一天对照
  都没有，`verdict_incremental` 只能是 `NOT_TESTABLE`。
- 只有 H=5、`window=60` 越过 30 块（58 块），此时 `significance_incremental`
  从 `UNKNOWN` 翻成 **`FAIL`**——它是唯一走到过判定的一次。

#### 三、越过门槛之后看到什么

H=5、`window=60`：改善 −9.28e−04，单侧 95% 区间 `[−2.49e−03, +6.29e−04]`；
换 `basis=abs` 是 −1.53e−03、`[−3.09e−03, …]`。区间跨 0 ⇒ 不是「证明更差」，
是「认证不了增量」。H=20 的 `abs` 口径是唯一正号（+1.66e−03，下界 −1.09e−03），
13 块，同样不显著。

**这两个字必须分清**：`significance_incremental=FAIL` 的语义是「测过了，
下界不为正」（`settle.significance_of`），不是「增量为负已被证明」。

#### 四、新字段 `se_incremental`

把「不可判定」翻成「还差多久」只有一条路：门限 `z·se/√B`。而当时唯一的入口
是 `skill_vs_clim_lcb`——4 位小数的**比值**，真实账本上落在 ±0.0004 的格子里，
反解出的 `se` 误差可达 25%，正好把「还差两年」和「还差二十年」混成一个数。
所以这个字段不是附赠品，它是这句翻译的分子。测试断言的是关系
（`lcb = mean − 1.645·se` 与判定同号），不是数字。

#### 五、没解决的

- **H=5 的 FAIL 不是 H=20 的结论。** 它们检验的是两个不同的预测任务。可用的
  直接证据只有：四个 H×basis 组合里三个点估计为负、一个为正且 13 块。
- **窗口仍按决策日计数**（§9.16 的配置语义），预算表里的 `w` 因此也是决策日。
- **面板还没用上这个结论。** 判定要么接受「H=20 在其生命周期内不出结论」并
  撤下这一栏，要么只在 H≤8 的卡上展示。改哪一边是产品决定，不在本节范围。

### 9.26 `verdict_incremental` 在平稳过程里恒为 FAIL（2026-11-05）

§9.25 的落点是「H=10 + `window=60` 再发 18 个交易日就能判定」。那条推导只算了
**什么时候可判定**（`n_eff ≥ 30`），没算**可判定的那一刻会不会过**。本节先给
被检量的代数，再用发布代码在已知真值的 DGP 上跑功效，最后回真实账本对方向。

结论先行：**它不是功效不够，是被检量的期望恒为负。** 加交易日只会把
`UNKNOWN` 换成 `FAIL`，H=5/`w=60` 那 58 块给出 FAIL 不是策略差，是这个代数式的
必然结果。

#### 一、被检量的代数

逐日逐 pick 的增量改善是

    d_i = (p_clim - y)^2 - (p - y)^2

其中 `p` 来自最近 `window` 个已结算决策日，`p_clim` 来自全部已可见决策日。把
两者写成「真值 + 估计误差」`p = p* + e_w`、`p_clim = p* + e_c`，对 `y` 取期望：

    (p_clim - p)(p_clim + p - 2y) = (e_c - e_w)(e_c + e_w) = e_c² - e_w²

所以

    E[改善] = σ_c² - σ_w²

**与真实胜率 `p*` 无关**，只与两个估计量的方差有关。而窗口估计量用的样本更少
（`window` 个决策日 vs 全部），`σ_w² > σ_c²` 恒成立 ⇒ `E[改善] < 0` 恒成立。

更要紧的是极限行为：`T → ∞` 时 `σ_c² → 0`，而

    σ_w² ≈ n_w·p(1-p) / (n_w + kappa)²

**停在常数上**——窗口长度不变，它的方差就不会变小。于是改善量收敛到负常数，
不是收敛到 0。平稳过程里 `P(PASS) → 0`，且**加数据使它更负**（`σ_c²` 变小）。

读法：这一栏实际测的是「两个估计量谁更噪」，不是「近端有没有增量信息」。

#### 二、发布代码上的功效（`scripts/probe_incremental_null.py`）

DGP：`DAYS` 个决策日 ×每日 10 picks，`H=10`、`window=60`、`kappa=20`、
`basis=median`（`p* = 0.5` 是精确零假设）。每格 `reps` 次独立重抽，直接调
`pwin.walk_forward`，不复制公式。

| 场景 | 解析 `σ_c²-σ_w²` | 实测均值 | 正值占比 | 可判定率 | PASS 率 |
|---|---|---|---|---|---|
| 平稳，360 日（reps=40） | −3.22e−04 | −1.85e−04 | 15% | 0% | **0%** |
| 平稳，900 日（reps=10） | −3.63e−04 | −2.60e−04 | 0% | 100% | **0%** |

900 日那行是关键：`n_eff` 已经过门槛（可判定率 100%），真胜率**恰好等于**
0.5，判定仍然 10/10 都是 FAIL。实测比解析值轻 30%，因为 `σ_c²` 沿路径递减
（早期几天样本少、偏差小），解析值取的是末端值。

变点场景（900 日，最后 120 天把真胜率抬到 `0.5 + a`）：

| a（真实胜率抬升） | 实测改善 | PASS 率 |
|---|---|---|
| 0.02 | −2.21e−04 | 0% |
| 0.05 | −6.23e−05 | 0% |
| 0.10 | +6.25e−04 | 0% |
| 0.20 | +3.15e−03 | 40% |
| 0.30 | +7.41e−03 | 100% |

即：**要出 PASS，胜率得从 50% 持续抬到 80%。** 50% 功率点落在 `a ≈ 0.21`
（在 0.20 的 40% 与 0.30 的 100% 之间插值）。

#### 三、门槛比自己的检出下限低一个数量级

`MIN_INCREMENTAL_EFFECT = 0.001`。改善量按 `a²` 折算，材料性门槛对应

    a = √0.001 ≈ 3.2pp

而 900 日（`n_eff ≈ 57`）的检出下限约 **21pp**。**门槛（3.2pp）比自己的检出
下限（21pp）低 6–7 倍**，所以判定只会在 `UNKNOWN` 与 `FAIL` 之间摆动，`PASS`
是名义上存在的状态：`MIN_NEFF=30` 保证「可判定」，不保证「可检出」，两件事
在 §9.21–§9.25 里一直被当成一件。

#### 四、真实账本只能当方向证据

§9.25 表里六个格子全为负，与代数同号。但幅度对不上：`H=20/w=250` 实测
−3.55e−04，该格解析值约 −2.4e−05（`n_w ≈ 2250` picks、`n_c ≈ 2900`），差一个
量级。说明真实账本里「估计量方差差」与「近期真的变差」混在一起，**这个检验
分不开两者**。所以真实数据只支持方向，不支持定量。

#### 五、§9.25 的 18 天计划因此作废

它回答的是「什么时候可判定」，不是「可判定时能不能过」。按上表，判定的那
一刻最可能的输出是 `NO_INCREMENT × PASS`，也就是 **FAIL**。H=5/`w=60` 用
58 块演示过一次，不是运气。

#### 六、出路（都没实现）

1. **偏差校正**：把被检量换成 `mean(d) - (σ_c² - σ_w²)`，零假设回到 0。改动
   只在一处（`_mean_se_lcb` 的调用点），但 `improvement_vs_clim` 的语义变了，
   旧卡上的数不可比。
2. **换个问题**：这一栏实际在问「近端和全史一样吗」，那就改名成变点检测
   （CUSUM / Page），按变点检验给零分布，而不是按 skill 给门槛。
3. **撤栏**：`verdict`（相对常数先验）是良定义的检验，`significance` 也良定义；
   撤掉增量这一栏，面板少一个永远红着的灯。
4. 若保留又不校正，`MIN_INCREMENTAL_EFFECT` 必须抬到 MDE 之上，否则是装饰。

#### 七、验收

- [x] 解析式与 DGP 实测同号、同量级（−3.63e−04 vs −2.60e−04）
- [x] 平稳 DGP 在可判定率 100% 时 PASS 率 0/10（900 日）
- [x] 变点 DGP 给出功效曲线（`a=0.20` → 40%，`a=0.30` → 100%）
- [x] 真实账本六格符号全负，与代数同向
- [x] 偏差校正 / 改名 / 撤栏：三选一 → §9.27 用同一判据把前两个否掉了
- [ ] `estimate()` 与 `/pwin` 仍未透传 `se_incremental`——但按本节，**先不要**
      把它接成「还差 N 天」倒计时：那个 N 是到达 FAIL 的日期

#### 八、没解决的

- **真实账本的偏差项没有独立估计**：要分离「方差差」与「真的变差」，得拿到
  逐日 `p` 与 `p_clim` 序列；`walk_forward` 没有把它们暴露出来。
- **组内相关没有进代数**：DGP 里同日 picks 独立，真实同日 picks 同涨同跌。
  它放大 `σ`，不改变 `E[d]` 的符号，但会改功效曲线的位置。
- **全量测试的 E 块仍未定位**（§9.25 遗留）。本轮跑到 35% 时已有 8 failed /
  4 errors，与「4 failed」的读数不一致；`TMP`/`TEMP` 被指到仓库内是这轮的
  变量，不能据此判断 HEAD 基线。

### 9.27 三个乘数：门槛、路径平均、设计效应（2026-11-05）

§9.26 证到 `E[d] = σ_c² - σ_w² < 0`，但留下的三条出路都默认「换掉被检量或门槛
就能救」。本节把四个统计量放在**同一条判据**下（假警固定 5% 时的 MDE），结论是：

**不是统计量的错。** 门槛、路径平均的稀释、持有期重叠的设计效应，三个乘数乘在
一起，把 `PASS` 推到了 20pp 的胜率抬升之外。换哪个统计量都搬不动 20pp。

#### 一、判据

    MDE(80%) = 让检出率达到 80% 所需的最小真实胜率抬升 a

每个统计量的门槛取它自己在**平稳 DGP 上的 95 分位**（size 由构造固定为 5%），
所以列间可比的是信息量，不是谁的门槛更松。四个被告：

| 代号 | 被检量 | 出处 |
|---|---|---|
| A | `mean(d)`，`d = (p_clim-y)² - (p-y)²` | 现行实现 |
| B | `mean(d - (σ_c²-σ_w²))`，逐日 plug-in | §9.26 出路 1 |
| C | window vs 窗口之前的计数，两比例 z | 经典同质性检验 |
| D | Page CUSUM，逐日标准化命中数 | §9.26 出路 2 |

A/B 是**全程路径平均**（现行口径），C/D 是**末端读数**。这个差别本身是结果。

自检：自建重放与 `pwin.walk_forward` 逐位对齐（900 日账本，`improvement_vs_clim`
差 5e-7，可评估日 888 对 888）。

#### 二、结果（`scripts/probe_incremental_power.py`）

DGP：900 决策日 × 10 picks，`H=10`、`window=60`、`kappa=20`、`p*=0.5`，
变点在最后 120 天。`overlap` 让持有期共享行情冲击（`ρ` 是行情因子占 latent 的
方差份额），代价是相邻决策日的结果相关——**这才是真实账本的形状**。

| DGP | 600 条 pick ≈ 几个独立观测 | 信息下限 | A | B | C | D |
|---|---|---|---|---|---|---|
| `iid`（ρ=0） | 600 | 5pp | 8pp | 8pp | 8pp | 10pp |
| `overlap` ρ=0.3 | 31（设计效应 19×） | 23pp | >30pp | >30pp | 30pp | >30pp |
| `overlap` ρ=0.6 | 13（设计效应 46×） | >30pp | >30pp | >30pp | >30pp | >30pp |

三张表的 size 行都是 5%（构造保证），所以表里比的是功率。

两条对 §9.26 出路的直接判决：

1. **A 与 B 几乎逐格相同**（唯一差异是 `a=0.02` 处 15% 对 14%，来自 plug-in
   方差的抽样噪声）。偏差校正是把被检量平移一个常数，而门槛校准也平移同一个
   常数——两者是同一个检验。**出路 1 买不到任何功效**，它买到的是「零假设回到
   0」这个说法本身，不是检出能力。
2. **D 从未优于 C**，ρ=0.6 时 28% 对 78%。CUSUM 的零分布是**路径最大值**，
   在强自相关下比末端两比例宽得多；改名成变点检测是换了个更钝的尺子。
   **出路 2 被实测否掉**，不是「没试过」。

#### 三、三个乘数

以 `iid`/900 日那一格为例（`se_incremental = 2.17e-4`）：

| 乘数 | 实测 | 后果 |
|---|---|---|
| 门槛 | `mde = 0.001 + 2.486·se = 0.00154`，是门槛的 **1.5 倍** | 判定要求真实改善 ≥ 0.00154 |
| 路径平均稀释 | 888 个评估日里只有 120 天带信号（**7.4 倍**） | 单日改善被摊成 1/7.4 |
| 设计效应 | 600 条 pick ≈ 13–31 个独立观测（**19–46 倍**） | `se` 比掷币公式大 4–7 倍 |

三者相乘，就是实测的功效曲线：`a=0.10` → PASS 0%，`a=0.20` → 16%，`a=0.30` → 100%。
**要过门槛，胜率得从 50% 持续抬到 80%**——与 §9.26 的读数一致，但现在知道它由
三个可分离的原因构成，而不是一个「代数式恒为负」。

**§9.26 的「门槛比检出下限低 6–7 倍」在 Brier 单位上其实是 1.5 倍。** 那个 6–7
是 pp 单位上的比（3.2pp 对 21pp），而 pp 与 Brier 的换算要除以「变点覆盖的账本
比例」再开方。把两个单位混在一起会高估门槛的罪过：**真正把 PASS 推远的是稀释和
设计效应，门槛只是第三名。**

#### 四、判决

- **出路 1（偏差校正）**：功效零增益（实测逐格相同），只是把 0 从 −3e−4 挪回 0。
  要做也可以，但它不解决任何问题——**否**。
- **出路 2（改名成 CUSUM）**：在三种 DGP 下都不优于末端两比例，强重叠下显著更差。
  **否。**
- **出路 3（撤栏）**：唯一与证据一致的方向。`verdict`（相对常数先验）是良定义的
  检验，撤掉增量这一栏，面板少一个永远红着的灯。

落地分两步：本轮只做**分辨率**（`mde_incremental` + 句子），不动判定语义——
删字段要改 35 处断言、且 `verdict_incremental` 是封进卡片的字段（改值会改旧卡的
可读性），那是独立一轮的事。

#### 五、本轮改了什么

- `pwin.MIN_INCREMENTAL_EFFECT` 旁新增 `_MDE_Z = 1.645 + 0.842` 与推导。
- `walk_forward` / `estimate` 新增 `mde_incremental = 门槛 + 2.486·se`；检验没跑
  时为 `None`（**不是**「等于门槛」）。
- 两个 `NO_INCREMENT` 分支的句子都带上分辨率：
  「要让下界过 0.001，真实改善需 ≥ 0.001539（门槛的 1.5 倍），低于它的增量这个
  样本分辨不出来」。样本不足（`INCREMENTAL × UNKNOWN`）那一支也印——那里既没
  分母也没分辨率，读者手上只剩一个负号。
- 新增 `scripts/probe_incremental_power.py`（四统计量 + 两 DGP + MDE）。
- 新增 5 条测试：同源、未跑时为 `None`、句子里有分辨率、随账本增长单调下降、
  `estimate()` 透传。

**没有改**：`verdict_incremental` / `significance_incremental` 的三态语义、
门槛值、`MIN_NEFF`。

#### 六、验收

- [x] A 与 B 的检出率逐格相同（三张表）
- [x] D 不优于 C（三种 DGP），ρ=0.6 时 28% 对 78%
- [x] 设计效应实测 19.3 / 45.5（不是假设值）
- [x] `mde_incremental` 与 `se_incremental` 同源（测试钉住）
- [x] 检验没跑时 `mde_incremental is None`
- [x] `NO_INCREMENT` 的句子自带分辨率（测试钉住）
- [x] 决策相关测试 128 passed（`-k decision`；其中新增 5 条）
- [ ] 撤栏（删 `verdict_incremental` / `significance_incremental`）未做

#### 七、没解决的

- **真实账本的 DEFF 没测过**。要测它只需要逐日命中数与 pick 数，现在
  `walk_forward` 不暴露这两个序列。这是把本节从「模拟」推到「诊断」的唯一缺口。
- **重叠模型是参数化的**（ρ=0.3/0.6 两档），真实值未知。ρ 越小结论越轻：ρ=0 时
  8pp 就够，ρ=0.6 时 30pp 都不够。所以「要不要撤栏」在 ρ<0.2 的世界里可以缓一缓。
- **`mde_incremental` 是路径平均尺度的数**，不是 pp。换算成胜率抬升要除以
  「变点覆盖的账本比例」再开方；同一个 0.00154 在全程变点下是 4pp，在末 1/7
  变点下是 15pp 起。报字段时不换算，正是不想把两个单位再混一次。
- 全量测试的 E 块仍未定位（§9.25 遗留）。


### 9.28 独立复核：6.9 是真的，但它不是「零分布形状」的全部（2026-11-05）

§9.27 的表格有一个方法论缺口：每个统计量的门槛都取**它自己在同一 DGP 上的
95 分位**，于是 size 被构造固定为 5%，表里只剩功效。它回答「谁更灵敏」，
不回答三件更要紧的事：

1. 零分布的**刻度**对不对（上线时门槛是常数，不是每格现算的）；
2. 那个 6.9 是 DGP 的产物还是账本的性质；
3. 修正 CUSUM 的标准化能不能救回功效。

本节用 `scripts/probe_statistic_null.py`（三 DGP × 七统计量 × 名义门槛）和
`scripts/probe_real_deff.py`（真实账本的矩）独立复核。**结论方向没变，但
归因和数字变了三处，其中一处推翻 §9.27 的措辞。**

> 同日第二轮：MDE 的**单位**（γ0 被写成 p(1−p)）、**分母**（vs 其余 还是
> vs 全历史）与**地板**（w 固定时 n→∞ 的极限）三处再改，第二节与末段已按
> 新读数重写；公式本身的验收见第九节，配对表的计数见第十节。
>
> 样本固定：本节所有读数对应 **n=325（2025-06-05 … 2026-09-30）**。缓存会
> 随行情同步增长（复算时已 326 天），复跑要加 `--nmax=325`，否则 momentum
> 的 15.9 会变成 16.0、low 的 25.7 会变成 26.1。可复现性是这一节的前提。

#### 一、6.9 复现了，而且不是模拟的产物

把门槛换成**常数**（iid 零分布的 95 分位，等价于上线时定死一条线）：

| DGP | z 的零分布 sd | 门槛 5% 时的实际 size |
|---|---|---|
| `iid` | 0.97（=1，自检通过） | 5% |
| `overlap` ρ=0.6 | **6.79** | **44%** |
| `surrogate`（真实账本分块重抽） | **5.98** | **43%** |

`surrogate` 不假设任何机制：把真实账本的日胜率按 40 天一块循环重抽，保留
实测自相关，其它一概不管。它给出 5.98，与参数化的 6.79 只差 12%。**所以
「sd 不是 1」不是 ρ=0.6 这个选择的产物**——真实序列本身就坐在那一档。

这条读数的后果比 §9.27 写的更硬：`verdict_incremental` 用的不是 z，但
**任何拿 iid 二项当分母的统计量**（包括面板上任何「命中率 ±1.96·se」的
展品）在这个账本上的实际误报率是 5% 的**九倍**。不是「偏保守」，是反的。

#### 二、真实账本的 DEFF：从假设变成读数

`scripts/probe_real_deff.py` 读 `data/.probe_per_day.json`（真实行情重建的
325 个决策日 × 20 picks × H=20，四个策略）。

**先定分母。** 旧版把 `h=n−w` 传进 `mde_two_prop`，算的是窗口对**其余**
n−w 天；而 `walk_forward` 的 `p_clim` 是**全部**可见日，包含窗口本身。
两者的差值差一个因子：`D_全 = (1−w/n)·D_其余`，方差差 `(1−w/n)²`。所以
同一份读数有两个口径，句子必须写清用哪个：

- `vs 其余`（`h=n−w`）：旧版和 `mde_two_prop` 实际算的那个；
- `vs 全历史`（`p_clim` 的定义）：前值 ×`(1−w/n)`，w=60、n=325 时 ×0.815。

| 策略 | 胜率 | 日 sd | acf lag1 | DEFF(w=60) | n_eff | MDE vs 其余 | MDE vs 全历史 | 块重抽(vs 其余) |
|---|---|---|---|---|---|---|---|---|
| momentum_20 | 0.384 | 0.1875 | 0.82 | 9.2 | 6.5 | 19.5 | **15.9** | 19.9 |
| reversal_20 | 0.468 | 0.1694 | 0.68 | 4.6 | 13.1 | 12.7 | **10.3** | 16.6 |
| low_turnover | 0.519 | 0.2616 | 0.88 | 10.8 | 5.5 | 31.6 | **25.7** | 31.1 |
| high_turnover | 0.286 | 0.1499 | 0.72 | 8.3 | 7.2 | 15.2 | **12.4** | 14.0 |

三个读数：

- **37–60pp 是单位错，不是读数。** 旧公式把 `v=p(1−p)`（逐 pick 方差）
  当成 `γ0`（日胜率方差），等于把 w 天当成 w 条独立 pick：漏了每天 m 条的
  稀释（`/m`）和日内相关（`DEFF(1)`），两项合起来把隐含 se 退化成**单日**
  sd（momentum 0.1905 vs 实测 0.1875）。修正后是 **10.3–25.7pp
  （vs 全历史）/ 12.7–31.6pp（vs 其余）**；旧值高估 2.3–3.7×（前者）
  或 1.9–3.0×（后者）。结论不变：仍是 3.2pp 目标的 5–8 倍。
- **`window=120` 的「全部改善」是分母的算术，不是发现。** 两种口径下
  w=120 的 MDE 都低于 w=60（vs 其余 13.9/9.1/26.6/12.3 vs
  19.5/12.7/31.6/15.2），**4/4，没有反转**——会反转的是 DEFF 本身：
  low 10.8→12.0、high 8.3→8.7，这两档的序列并没有变得更可估，改善全部
  来自 w 翻倍。而 `vs 全历史` 的 MDE 对 w **单调递减**：w→n 时
  `(1−w/n)→0`，MDE→0。
  也就是说 w=200、w=250 会「更好」，那只是窗口吞掉了对照。同一账本在
  `vs 其余` 下才有内点（四个策略都在 120–160 天附近最低）。所以「w 应当等于 120」不成立，
  §9.27 把「窗口太短」记到设计效应头上这笔账也要撤——窗口该由样本外的
  净收益或预注册的对照定义来选，不由 MDE 选。
- **「平台 DEFF 远大于 acf」也不成立。** 同口径比较（分块 DEFF 对
  acf×日内膨胀）：B=20 时比值 0.77–1.47，B=60 时 1.29–1.85。旧文写的
  「1.3–1.4×」是对 B≥20 那条**仍在上升**的曲线取平均得到的，而 B≥40
  只有 4–8 块，χ² 区间跨 2–3 倍。能说的只有「分块曲线到 B=80 仍在上
  升，含真实漂移」；幅度不可判定，见第五节。

> **复核补记（同日，n=325）。** B=20 的「0.77–1.47」复现不出来。按本脚本
> 的口径（连续块、块均值方差 ÷ `v/(B·m)`、acf 侧乘 `γ0·m/v`）重算，四个
> 策略是：
>
> | B | momentum | reversal | low | high |
> |---|---|---|---|---|
> | 20 | 0.89 | 0.94 | 0.92 | 0.96 |
> | 40 | 1.36 | 0.62 | 0.98 | 1.51 |
> | 60 | 1.85 | 1.38 | 1.29 | 1.35 |
>
> **符号不稳定出现在 B=40（0.62–1.51），不在 B=20。** B=20 时四个都低于 1，
> B=60 时四个都高于 1，交叉点在 B≈40。结论不变（曲线到 B=80 仍上升、
> 幅度不可判定），但归因要从「B=20 就正负不定」改成「短块一致低估、
> 长块一致高估，交叉点在 40 附近」。B=60 那一档逐位复现。

#### 三、CUSUM：修好刻度只买到 validity，买不到 power

`probe_incremental_power.py` 的 `D` 除以逐日方差 γ0，而路径方差是 n·LRV
= n·γ0·DEFF。于是门槛被放大 √DEFF 倍。新增 `D_lrv`（除以截断核 LRV）：

| | 名义门槛 size | MDE(80%) |
|---|---|---|
| D（γ0） | **92–97%** | >30pp |
| D（LRV） | **2–4%** | >30pp |

**这不是「换了个更钝的尺子」，是尺子的刻度错了。** 修好之后 size 回到 5%
以下（略保守），但 MDE 一格没动——因为约束不在刻度，在「对 780 个候选变点
取最大值」这个动作：多重比较惩罚随路径长度增长，而 C 只比一个位置。

**但 §9.27 说「D 从未优于 C」也过头了。** 变点位置一变，排序就翻：

| DGP | 变点位置 | C 检出 | D 检出 |
|---|---|---|---|
| overlap | 末端 120 天 | 44% | **14%** |
| overlap | 中段（400 起） | 18% | **51%** |
| surrogate | 末端 120 天 | 40% | **6%** |
| surrogate | 中段（400 起） | 18% | **28%** |

C 是「末端 vs 历史」的**匹配检验**：变点在末端时它就该赢。D 是「路径上某处」
的检验：变点不在末端时它才赢。两者不是钝与利，是**问的不是同一个问题**。
账本要问的是前者（「这个 window 还有没有增量」），所以选 C 是对的；但理由
要改成「C 与问题匹配」，不是「C 更利」。

#### 四、分块长度必须等于相关长度，不是持有期

| 统计量 | size（overlap） | size（surrogate） |
|---|---|---|
| E：按 H=10 分块 | 14% | 15% |
| E40：按 40 分块 | **4%** | **7%** |

按 H 分块只消掉持有期重叠，消不掉更慢的那一层（实测 acf 到 lag 40 仍有
0.1 量级）。**「不重叠块 = 独立观测」这个等式只在块长 ≥ 相关长度时成立。**
§9.27 的「600 picks ≈ 6 块」用的是 H=10 分块，那个 6 是下界；按 40 天分块
是 15 块，但每块内部仍相关——真实 n_eff 见第二节，5.5–13。

#### 五、这一节没能解决的，以及为什么它不改变结论

- **DEFF 含真实漂移，所以它是这份序列的方差，不是「保守假设」。** 零假设
  要求 p 不随时间变，而真实序列的 p 确实在变。观测到的自相关无法区分
  「重叠/同股票」与「边际在漂」，账本里也没有持仓明细或重复实验能分开。
  把漂移拆掉会得到一个更小的 DEFF，但那等于换零假设（允许边际漂移），
  不是同一个检验。所以第二节的 15.9–25.7pp 是这份账本在现行定义下的刻度，
  不能再当成「可以打折的上界」。
- **`surrogate` 的 A 不可比。** 分块重抽保的是日级序列，重抽不出逐 pick 的
  离散性，所以 Brier 那一列在 surrogate 行留空（打印 `—`），不硬凑。
- **没做**：把 `daily_hits` / `daily_picks` 接进 `/pwin` 的响应。字段已在
  `walk_forward(with_daily=True)` 上，但 `estimate()` 不透传——一张卡存
  300–900 个整数会撑大契约，而唯一的下游是离线诊断。

#### 六、本轮改了什么

- 新增 `scripts/probe_statistic_null.py`：三 DGP（iid / overlap / 真实分块
  重抽）× 七统计量 × **常数**名义门槛，另加变点位置与窗口扫描。
- 新增 `scripts/probe_real_deff.py`：真实账本的 acf、分块方差曲线、DEFF、
  n_eff 与 MDE，四个策略逐条打印。
- `pwin.walk_forward` 新增 `with_daily`：返回 `daily_hits` / `daily_picks`
  （与 `evaluated` 同序）。默认 `None`，`estimate()` 不透传。
- `probe_incremental_power.py` 的 CUSUM 新增 `scale="lrv"`，表格加 `D_lrv`
  一列；两个口径并排，刻度差异可读。
- 新增 2 条测试：逐日序列与 `oos_days` 同长、默认不返回；样本不足时为 `None`。

同日复算（第二轮，单位错之后）：

- `probe_real_deff.py`：`mde_two_prop` 改吃 `γ0 = Var(日胜率)`，`v=p(1−p)`
  只保留给 DEFF 的基线；新增 `block_sd()`（40 天循环块、固定种子）与
  「vs 其余 / vs 全历史」双列。
- 新增 `probe_mde_truth.py`：AR(1) 已知 DGP，γ0 与 acf 闭式，20k 次对比
  解析 se 与模拟 se，两个口径各一条判定带。
- 新增 `probe_paired_diff.py`：6 对策略的逐日差分，HAC（截断核、lag=40、
  LRV≥γ0）与 40 天块重抽两种 se 并排，附带名义/ Bonferroni 两行计数。

#### 七、验收

- [x] iid 下 z 的 sd = 0.97（自检），名义 5% 的实际 size = 5%
- [x] 重叠下 z 的 sd = 6.79，真实分块重抽下 = 5.98（相差 12%，不是参数产物）
- [x] 真实账本 DEFF(w=60) = 4.6–10.8；MDE = **15.9–25.7pp（vs 全历史）**
      / 19.5–31.6pp（vs 其余）；块重抽 14.0–31.1pp，解析/重抽 0.76–1.08
- [x] `window=120` 在两种口径下都低于 `window=60`，但 `vs 全历史` 对 w
      单调（w→n 时 →0）⇒ **不作为选窗依据**，原验收条废除
- [x] CUSUM 换 LRV 后 size 从 92–97% 回到 2–4%，MDE 不变
- [x] 变点在中段时 D 优于 C（51% vs 18%），在末端时反之（14% vs 44%）
- [x] 按 40 分块的 size（4–7%）优于按 H 分块（14–15%）
- [x] 已知 DGP 蒙特卡洛：解析/真值 = 1.007（ρ=0.68）/ 1.006（ρ=0.82），
      两个口径都在 [0.95, 1.05]；`test_decision_mde_truth.py` 4 条钉住
- [x] 配对差分：名义 p<0.05 为 **5/6**（HAC 与块重抽一致），Bonferroni
      p<0.0083 为 **3/6**，恰好是 |t|>3.5 的三对
- [x] `pytest -k decision` 全绿（新增 2 + 4 条）

#### 八、对 §9.27 的三处更正

1. **「D 从未优于 C」→「D 在末端变点上劣于 C，在中段变点上优于 C」。**
   前者是匹配检验的必然，后者是扫描统计量的主场。理由要写成「问题匹配」。
2. **「6–7 倍」的更正只说对了一半。** §9.27 说那是 pp 与 Brier 的单位混用，
   对的；但**真正把 PASS 推远的是设计效应**。不过实测值比 §9.27 用的
   「>30pp」**更小**，不是更大：15.9–25.7pp（vs 全历史）——方向上 §9.27
   是保守的。真正的高估不在这条，在 MDE 的单位（见第二节）。
   门槛的 1.5 倍在三者里仍然排第三。
3. **「600 picks ≈ 13–31 个独立观测」在真实账本上是 5.5–13 个（按天），
   折算成 picks 是 110–260 条。** 数量级对，但 §9.27 那个 13 是模拟值，
   不是读数——现在有读数了。

**判决不变：撤栏。** 但依据从「模拟显示 ρ=0.6 时 30pp 都不够」换成
「真实账本四个策略的 MDE 是 **15.9–25.7pp（vs 全历史）**，是 3.2pp 目标的
5–8 倍」。§9.25 那条「再攒样本就能判定」的路线，在 H=20 上**没有解**，
而且理由比原文更硬：

- `vs 其余` 口径下 MDE 有地板：w=60 固定、n→∞ 时收敛到 18.2 / 11.6 /
  27.6 / 13.9pp。当前 19.5 只是「h 还不够大」带来的一点点加成，攒到无限
  历史也只降到 18.2。
- `vs 全历史` 口径下方向是**反的**：MDE 随 n **上升**，momentum 从 325 天的
  15.9 → 5,000 天的 18.1 → 上确界 18.2。因为 `p_clim` 含窗口本身，历史越长，
  窗口在对照里的权重越小，`(1−w/n)` 越接近 1。所以「再攒历史」不会让这个
  数变小，只会让它变大。
- 原文的「~7.9 万个决策日（约 300 年）」和按 `vs 全历史` 反平方缩放得到的
  「3,400–21,000 天（13–83 年）」都是把一条**上升**曲线当下降曲线解出来的。
  方程 `MDE=3.2pp` 在这里唯一的解是 n≈62–66，即 w=60 时对照样本只剩
  2–6 天——那里差值本身趋于 0，不是「攒够了」。

这不是时间问题，是这个问题在这个账本上不可回答。要让它可回答，得换
估计量（配对差分，见第十节）或换问题（净收益、预注册目标），不是加样本。

#### 九、MDE 公式的验收：已知 DGP，不是账本

第二节的四个数是**样本矩代进公式**得到的，公式本身一直没被单独验过。
拟议的验收条是「解析/块重抽 ∈[0.85,1.15]」，这条在 reversal 上会
直接失败（12.7/16.6 = 0.76），但失败的不是公式：n=325、L=40 只有 8 块，
bootstrap sd 自身的相对误差约 27%，±15% 的带子比估计量的噪声还窄。
把实现细节当验收标准，得到的是「块长选得好不好」，不是「公式对不对」。

换成已知真值的 DGP：AR(1)，`γ0=1`、`ρ^k` 都是闭式，n=325、w=60、20k 次，
两个口径各测一次。解析式仍是 `γ0(DEFF_w/w + DEFF_h/h)`，`vs 全历史`
再乘 `(1−w/n)`：

| ρ | 口径 | 解析 se | 模拟 se | 解析/真值 |
|---|---|---|---|---|
| 0.68 | vs 其余 | 0.3216 | 0.3195 | **1.007** |
| 0.68 | vs 全历史 | 0.2622 | 0.2605 | **1.007** |
| 0.82 | vs 其余 | 0.4381 | 0.4352 | **1.006** |
| 0.82 | vs 全历史 | 0.3572 | 0.3549 | **1.006** |

三件事被钉住：

- **公式代数是对的。** 真实账本的 0.76 是估计噪声（8 块）或长程依赖，
  不是公式错。判定带 `[0.95,1.05]` 放在 MC 上，`test_decision_mde_truth.py`
  用 4,000 次就能稳定通过（sd 的相对误差约 1.1%）。
- **单位错会被同一个带子抓住。** 把 `γ0` 换回 `p(1−p)`，se 被放大
  `sqrt(v/γ0)=2.6` 倍（momentum：0.2366/0.0352），比值直接落到 2.6。
  这条写成回归测试，改回去就红。
- **一个没验的边界**：解析式忽略了窗口与「其余」的协方差。AR(1) 下这项
  只有方差的约 1%（ρ=0.82、w=60 时 0.0016 vs 0.154），所以 1.006 成立；
  换成长记忆或强趋势 DGP 时这项不再可忽略，公式要重推。这不是现在的账本。

#### 十、配对差分：名义 5/6，校正后 3/6

`probe_paired_diff.py` 把 6 对策略的逐日胜率差做成表。两条 se 并排：
HAC（截断核、lag=40、`LRV≥γ0`，与 `settle.hac_lcb` 同约定）和 40 天
循环块重抽（固定种子）：

| 对照 | Δ(pp) | se_HAC | se_块 | t_HAC | p_HAC | Bonferroni |
|---|---|---|---|---|---|---|
| momentum − reversal | −8.37 | 3.50 | 4.03 | −2.39 | 0.0166 | 否 |
| momentum − low | −13.43 | 5.31 | 6.70 | −2.53 | 0.0114 | 否 |
| momentum − high | +9.86 | 2.77 | 2.86 | 3.56 | 0.0004 | **是** |
| reversal − low | −5.06 | 2.89 | 6.02 | −1.75 | 0.0796 | 否 |
| reversal − high | +18.23 | 1.76 | 2.84 | 10.36 | <0.0001 | **是** |
| low − high | +23.29 | 5.67 | 5.67 | 4.11 | <0.0001 | **是** |

**「4 对过关」在任何一种规则下都复现不出来。** 名义 p<0.05 是 5/6
（HAC 与块重抽都给 5），按 6 次 Bonferroni（p<0.0083）是 3/6，恰好是
|t|>3.5 的那三对。三对的 Δ 分别是 9.9 / 18.2 / 23.3pp，另两对贴线
（p=0.017/0.011），换块重抽后走到 0.038/0.045——仍在 0.05 内，
但已经没有余量，任何一次多重比较校正都会把它们拿掉。

两条 se 的差也不是固定的 0.1–0.5pp：本表是 0.0–3.2pp，随 HAC 带宽在
L=20…108 之间移动。reversal−high 上 L=40 给 1.76、块给 2.84，t 从 10.4
掉到 6.4——差 1.1pp 就够改一个结论的措辞。

**配对买到的是分母，不是排名。** 与「两个水平各自 HAC 再相加」相比，
配对只把 se 压到 0.96–1.78 倍；看起来的 3–4 倍来自把「策略间差分」和
「窗口对历史」两个不同估计量放在一起比。这张表能支撑的是「配对差分的
se 在 1.8–6.7pp」，不能支撑「A 比 B 好」——`low` 与 `high` 的 universe
几乎不重叠，23pp 的差是换了股票，不是方法优劣。

**仍未做**：删 `verdict_incremental` / `significance_incremental`（91 处引用、
0 前端）。没有 gate，所以这不是统计问题，是信息价值问题。3.2pp 这个目标
已经换成 `docs/decision-target-prereg.md` 里的两个锚：**成本 19bp（经济门槛）
与 200bp（下一段样本的可确认效应）**；0.001 Brier 只保留为预测质量门槛，
不再用于样本量。在有人认领之前，字段留着。

#### 十一、换估计量：净收益口径把「无解」变成「差 10–31 倍」（2026-11-05 追加）

第九、十节仍在胜率口径里。胜率不是经济目标：`0.001` Brier 的开方是 3.2pp，
而成本是 19bp/往返——单位不同，也没有换算关系。`probe_window.py` 现在同时
写连续 `exc`（对截面中位数的 20 日超额），`probe_net_diff.py` 据此算每笔 bp：

| 策略 | 均值(bp) | se_块 | MDE(80%) | 净 LCB | MDE/成本 | p | Holm | 判定 |
|---|---|---|---|---|---|---|---|---|
| momentum | +43.2 | 236.7 | 588 | −329 | 31.0 | 0.457 | 0.457 | UNRESOLVED |
| reversal | +122.0 | 81.4 | 202 | −29 | 10.7 | 0.100 | 0.200 | UNRESOLVED |
| low_turnover | +185.2 | 86.4 | 215 | +27 | 11.3 | 0.027 | 0.081 | ECON+（校正后不成立） |
| high_turnover | −405.5 | 148.9 | 370 | −661 | 19.5 | 0.002 | 0.008 | ECON− |

成本 19bp = 佣金 2bp×2 + 滑点 5bp×2 + 卖出印花 5bp；块重抽 B=40、5% 分位
单侧，p 由重抽分布直接数（不用正态近似，这个分布是偏的）。

**但上表的基准是全样本中位数，而被选中的 20 只并不在成交额中位**：
momentum 0.90、reversal 0.59、low 0.48、high 0.93。把基准换成**同一成交额
五分位内的中位数**，同一批 picks 的读数变成：

| 策略 | 成交额分位 | vs 全样本 | se | vs 同层 | se | t(同层) |
|---|---|---|---|---|---|---|
| momentum | 0.90 | +43.2 | 236.7 | **+192.8** | 157.7 | 1.22 |
| reversal | 0.59 | +122.0 | 81.4 | **+194.5** | 81.0 | 2.40 |
| low_turnover | 0.48 | +185.2 | 86.4 | **+212.1** | 97.6 | 2.17 |
| high_turnover | 0.93 | −405.5 | 148.9 | **−251.3** | 115.1 | −2.18 |

**基准一换，四个读数位移 27–155bp，符号不变、幅度和显著性都变**：high 的
−405 缩到 −251（t 从 −2.72 到 −2.18），reversal / low 从 t=1.50 / 2.14
升到 2.40 / 2.17，momentum 的 +43 变成 +193（仍不显著）。位移量与 MDE
同阶，说明**效应量本身对基准选择敏感**——「5–8 倍目标」这类幅度陈述
不能只报一个基准。

五个读数：

- **分辨率是成本的 10–31 倍。** 20 只/日、20 日重叠，325 天只有约 16 个
  独立块。要分辨「刚过成本」的 19bp，reversal 需约 3.7 万天、momentum
  约 31 万天；要确认观测到的 122bp，reversal 累计约 890 天。**同一份账本，
  「确认大效应」可行，「确认刚过成本的效应」不可行**——这两件事必须
  分开说，pre-reg 的 19bp/200bp 就是这个区分。
- **唯一在 Holm 校正后站得住的是 high_turnover 的负号，但它不是「信号
  好坏」。** 全样本 −405（t=−2.72、Holm 0.008），同层 −251（t=−2.18、
  边缘）；幅度掉 38%，其中一部分是流动性 beta，剩下的仍可能被行业/
  波动率解释。reversal / low 在同层基准下也到 t=2.40 / 2.17，但主基准下
  p=0.10 / 0.027、Holm 0.081——**事后换基准换不出正结论**。
- **胜率与净收益排序相反。** momentum 胜率 38.4%，每笔均值却是 +43bp：
  赢的次数少、赢得大。去最好 5 天后均值 −4bp，所以它不可信；但这条
  足以说明**用胜率 MDE 选策略会选错**——它测的不是盈亏。
- **配对差的 MDE 更大（316–699bp），不是更小。** 同名配对消掉了共同
  行情，但两个策略的残差差本身很噪；名义 3/6、Bonferroni 2/6。跨池
  配对仍是「换股票」，不能读成方法优劣。
- **样本必须固定。** 重算缓存后是 326 天（多了 2026-10-08），同一份代码
  给出 16.0/10.4/26.1/12.4，与表里的 15.9/10.3/25.7/12.4 差 0.1–0.4。
  三个 probe 已加 `--nmax=325`，本节读数用它复现。

经济目标见 `docs/decision-target-prereg.md`：19bp 是经济门槛，200bp 是
下一段样本的可确认效应。两者相差 10 倍这件事本身就是结论——这份账本
能证明「很大的效应」，不能证明「刚过成本的效应」。**「撤栏」的依据由此
从「胜率 MDE 是 3.2pp 的 5–8 倍」换成两条：净收益 MDE 是 19bp 成本的
10–31 倍；同一批 picks 换个基准就位移 27–155bp。前者说明分辨率不够，
后者说明效应量对基准敏感——两条都不依赖 Brier 换算。**

本轮新增/修改：

- `probe_window.py`：同时写 `.probe_per_day_exc.json`（连续 `exc`），加
  `--rebuild`；两份缓存的日序与 picks 一一对应。
- 新增 `probe_net_diff.py`：水平/配对/成本三张表，块重抽 5% 分位 + Holm。
- 新增 `probe_selection_tilt.py`：成交额分位与同层中位数基准。
- `probe_real_deff.py` / `probe_paired_diff.py`：加 `--nmax`，样本可固定；
  前者的「DEFF 是上界、MDE 偏悲观」改成「这是这份序列的方差，拆漂移等于
  换零假设」——与第五节一致，原措辞是同一处逻辑倒置的另一半。
- 新增 `docs/decision-target-prereg.md`：19bp / 200bp / 250 天 / Holm。
- 新增 5 条测试（`test_decision_net_diff.py`）：成本、`_MDE_Z`、块重抽、
  分位、单侧 p 的符号。

---

### 9.29 全截面 IC：宽度买到 1.1–3.2 倍，而且它换了一个对象（2026-11-05）

§9.28 的结论是「20 只/日、20 日重叠，325 天只相当于十几个独立块」（设计推算
n/20≈16）。本节把 n_eff 实测出来，是 25–35——比推算宽松，但仍是三位数天内只有
两位数独立块。自然的下一步是「把扔掉的 99% 截面捡回来」：既然每天只用 20 只，
全市场 5,500 只的秩相关应该把 MDE 压下去一个量级。本节按同一批决策日
（`--nmax=325`）、同一份 `kline_daily_enriched` 把它做出来，结论有三条，其中
一条推翻了 §9.28 的一半。

#### 一、IC 的读数：t=3.64 是真的，但它不是 bp

| 策略 | IC | se_HAC | se_块 | t | MDE_IC | 层内 IC | 层内 t |
|---|---|---|---|---|---|---|---|
| momentum | −0.0535 | 0.0260 | 0.0289 | −1.85 | 0.0717 | −0.0361 | −1.48 |
| reversal | +0.0535 | 0.0260 | 0.0289 | +1.85 | 0.0717 | +0.0361 | +1.48 |
| low_turnover | +0.1111 | 0.0221 | 0.0306 | +3.64 | 0.0760 | +0.0791 | +3.28 |
| high_turnover | −0.1111 | 0.0221 | 0.0306 | −3.64 | 0.0760 | −0.0791 | −3.28 |

N≈5,419 只/日，σ₂₀=1,330bp，z̄_top20=1.73。「层内 IC」是成交额五分位**内**的
秩相关按样本量加权——§9.28 的「同层中位数基准」在 5,500 只上的高分辨率版本：
控制规模/流动性后 turnover 仍有 t=3.28，动量降到 t=−1.48。

先记三条口径事实：

- **reversal 与 momentum、high 与 low 的 IC 逐位互为相反数**。四个策略是
  **两个信号 × 两个方向**，不是四个独立假设。Holm 按 4 次算是保守的；按 2 个
  信号算是 0.0003（turnover）与 0.064（动量，不显著）。
- 层内相关只掉 29%（0.111→0.079），比 20 只口径的同层位移小得多。**§9.28 说的
  「选股坐在成交额 0.93 分位」解释不了 turnover 效应的大头。**
- IC 的 MDE 是 0.072–0.076（IC 单位）。**它不能写成 bp**，理由见第四节。

#### 二、宽度确实压 se —— 但压的是另一个泛函

同一批决策日、同一目标（每笔 20 日 bp），四个估计量并排：

| 策略 | 20 只（产品） | top10% vs 中位数 | 十分位多空 | IC×σ×z̄（秩映射） |
|---|---|---|---|---|
| momentum | +43.2 / 236.7 / MDE 588 | +145.7 / 176.6 / 439 | −137.0 / 234.0 / 582 | −114.5 / 73.5 / 183 |
| reversal | +122.0 / 81.4 / 202 | **+180.1 / 43.5 / 108** | −23.7 / 200.3 / 498 | +114.5 / 73.5 / 183 |
| low_turnover | +185.2 / 86.4 / 215 | **+209.8 / 46.6 / 116** | +194.6 / 176.8 / 439 | +242.5 / 77.2 / 192 |
| high_turnover | −405.5 / 148.9 / 370 | **−9.4 / 121.6 / 302** | −615.3 / 174.4 / 434 | −242.5 / 77.2 / 192 |

（均值 / se_块 / MDE，bp。）

**宽度是有效的**：同口径 top10% 把 reversal 的 MDE 从 202 压到 108（t 1.50→4.14）、
low 从 215 压到 116（t 2.14→4.51）。这是这份账本第一次把这两个策略推到 t>4。
但同一张表里，high_turnover 从 −405 变成 **−9.4（t=−0.08）**。

原因在配对尾差（同日 `top20 − top10%`）：

| 策略 | 尾差(bp) | se_块 | t |
|---|---|---|---|
| momentum | −102.6 | 100.2 | −1.02 |
| reversal | −58.1 | 56.0 | −1.04 |
| low_turnover | −24.6 | 57.7 | −0.43 |
| high_turnover | **−396.0** | 107.0 | **−3.70** |

四个都为负：**极端 0.4% 系统性差于同方向顶层 10%**；high 的差是 −396bp，t=−3.70，
是这张表里唯一显著的。也就是说 high_turnover 的 −405bp 不是「高换手这个因子」
的效应，是「换手率最高那 20 只」的效应——同一方向铺到 540 只就归零。
单日核对过（2026-06-01：top20 +183bp、top10% +1,337bp），不是实现错。

#### 三、IC→bp 的换算率不是常数

把实际 bp 对 IC 做无截距回归，斜率就是这份账本的隐含换算率；理论上它应等于
σ·z̄=2,295bp。四个策略各估一次：

| 策略 | 实际 bp/IC | σ·z̄ | 比值 | corr(bp,IC) | R² |
|---|---|---|---|---|---|
| momentum | 3,911 | 2,295 | 1.70 | 0.54 | 0.30 |
| reversal | 1,318 | 2,295 | 0.57 | 0.36 | 0.13 |
| low_turnover | 1,841 | 2,295 | 0.80 | 0.68 | 0.47 |
| high_turnover | 3,027 | 2,295 | 1.32 | 0.60 | 0.35 |

比值 0.57–1.70，R² 0.13–0.47。**IC 与「20 只等权均值」不是同一个泛函**，一个刻度
常数折不过去。最刺眼的是 momentum：IC=−0.0535 而实际 top-20 是 +43bp，符号相反。
分位形状解释了这件事：

```
momentum 十分位（bp，去当日截面均值，1=最弱 10=最强）
  −16  −2   9  15  18  21  18   5  −19  −50
```

倒 U，峰在第 6 层，最高一层是最差的一层。20 日动量不是单调信号；IC 测的是
整条秩关系（受第 10 层拖累而变负），top-20 测的是极端尾部（+43bp，t=0.18）。
两个都对，都不是 bp 意义上的「动量有效」。

#### 四、结论：50bp 这条路关了

预注册 §10 把全截面 IC 写成「唯一可能把 MDE 从 200bp 压到 50bp 的设计改动」。
**这条不成立，而且不是差一点：**

1. **IC 口径的 MDE 是 0.072–0.076**，换算成 bp 需要第三节那个不稳定比值，
   折出来的 183–192bp 是刻度巧合，不是分辨率。
2. **同口径宽度（top10%）的 MDE 是 108–302bp**，比 20 只好 1.1–1.9 倍，
   但仍离 50bp 差 2–6 倍；且它估的是「顶层 10%」，不是产品持有的极端 20 只
   （high 上两者差 396bp，t=−3.70）。
3. **时间序列才是约束**：等价独立日数 n_eff 只有 25（momentum/reversal）/
   35（turnover），325/25=13，即每个独立块相当于 13 天——比 20 日持有期还短，
   说明重叠不是唯一的自相关来源，但足以让 325 天只剩 25 个独立块。宽度
   压掉的是横截面的特定风险，压不掉因子路径的日间波动——后者占方差的大头
   （日内 sd/只 2,616bp，除 √20 是 585bp，而实测 se 只有 237bp，说明 20 只
   高度同向，有效宽度远小于 20；再铺宽到 540 只，se 也只从 237 降到 177）。

把下一段 250 天（合计 575）按 se∝n^(−1/2) 外推：

| 策略 | 20 只 MDE@575 | top10% MDE@575 | 确认 200bp 所需天数（20 只 / top10%） |
|---|---|---|---|
| momentum | 442 | 330 | 2,812 / 1,566 |
| reversal | 152 | 81 | 333 / 95 |
| low_turnover | 161 | 87 | 375 / 109 |
| high_turnover | 278 | 227 | 1,114 / 742 |

**pass/fail 的分布不随估计量变**：575 天上只有 reversal / low 能进 200bp 以内，
momentum / high 不能。§6 的功效预算因此是稳健的——它不依赖 20 只这个口径。
而 19bp 那一档，20 只口径要 3.7 万–31 万天，top10% 要 1.1 万–17 万天：
**「刚过成本」在任何宽度下都不可确认**，这句结论比 §9.28 更硬。

#### 五、被推翻的那一半：high_turnover 是尾部，不是因子

§9.28 写「唯一在 Holm 校正后站得住的是 high_turnover 的负号」。现在这句要拆开：

- **长边（low_turnover）是稳的**：20 只 +185、top10% +210、尾差 −25（t=−0.43），
  层内 IC 0.079（t=3.28）。宽度、基准、层内三重检查下符号和量级都不变。
- **短边（high_turnover）只在极端尾部成立**：20 只 −405（t=−2.72）、top10% −9.4
  （t=−0.08）、尾差 −396（t=−3.70）。铺宽就归零，说明它不是「高换手」的因子
  收益，是「换手率尖峰那 20 只」的路径。这类标的常伴涨停/连板，20 日后的
  反转不能外推到「所有高换手股」。

所以「撤 high_turnover 栏」的依据要改写：不是「高换手是负 alpha」，而是
**「这一栏的负号只存在于它自己那 20 只构成的尾部里，不可外推、不可复制」**。
对产品决策这不改变动作（仍然是撤），但改变了理由，也改变了它对其他标的的
推广能力——这点必须写进对外表述。

#### 六、为什么「改构造」不是答案

分位表容易被读成「峰在第 8 层，所以该买第 8 层」。三处核对后这个读法不成立：

- **对 low_turnover，位置不是瓶颈**：尾差只有 −24.6bp（t=−0.43），极端 20 只与
  整层没有可分辨的差别；真正的差距是宽度（se 86 vs 47）。
- **移动 20 只的位置不会降 se**：仍是 20 只，MDE 仍约 215bp。要拿到 116bp 必须
  铺到 540 只，那已经不是「每天 20 只」的产品，是另一个产品。
- **200bp 门槛本来就能达到**：575 天时 20 只口径是 161bp（§四表）。不需要为了
  分辨率改产品。

所以这条不进下一步，只记一条负面结果：**在 20 只这个形态下，215bp 是 MDE 的
地板**——宽度换不到（换的是泛函）、位置换不到（尾差不显著）、估计量换不到
（换算率不稳）。要更好只能改产品形态，而预注册的 200bp 门槛并不要求改。

#### 七、本轮改了什么

- 新增 `scripts/probe_cs_ic.py`：全截面秩 IC、层内 IC、四个估计量、换算率、
  尾差、块长四档、十分位形状、天数预算。`--nmax=325` 与 `--rebuild`，缓存
  `data/.probe_cs_ic.json`；决策日列表从 `.probe_per_day_exc.json` 读，
  缺日直接报错（不静默跳过）。
- 修正一处自造错误：`n_eff` 初版写成 `n·(sd/se)²`，把 n 乘了两次，输出
  8,218（>n）。正确式是 `(sd/se)²`，为 25/25/35/35。**这个错不会报错**，
  只会给出一个「独立日数比天数还多」的数；已加注释与测试。
- 新增 6 条契约测试（`test_decision_cs_ic.py`）：成本外常数 `MDE_Z`、标准化
  秩的 ddof、z̄ 的解析值 1.726（正态分数会给 2.6，差 50%）、秩相关端点、
  层内相关的单调端点、缓存日序与 `probe_net_diff` 同源。
- `pytest -k decision`：**145 passed**（139 + 6）。

#### 八、验收

- 325 天逐位：20 只口径 +43.2 / +122.0 / +185.2 / −405.5，与 §9.28 一致；
  IC −0.0535 / +0.0535 / +0.1111 / −0.1111（互为镜像，符合构造）。
- 层内 IC 与 `probe_selection_tilt.py` 方向一致（turnover 仍显著、动量不显著）。
- 尾差在单日（2026-06-01）独立核对：top20 +183bp、top10% +1,337bp，符号与
  量级一致。

### 9.30 `verdict_incremental` 的 size 与 power：它不是功效低，是对效应量非单调（2026-11-05）

§9.26 用发布代码跑过平稳与变点两个 DGP，§9.27 又把 `mde_incremental` 接进
了句子。但那份证据有三个缺口，使它只能当方向证据，不能当验收读数：

1. 零效应只有 iid 一种。真实账本的 `y` 有 19 天持有期重叠，iid 零分布会把
   `se` 低估 3–5 倍，于是 size 看起来很干净，其实只是没造出真实的自相关。
2. 「目标效应」从来没有写成一个可核对的数。§9.26 的注释把
   `mde_incremental` 开方读成「3.2pp 的胜率抬升」，这个读法没有在任何实验里
   对上过。
3. PASS 率只在 2–3 个 a 上取过点。如果检出力对 a 非单调，单点读数会把倒 U
   的峰误读成单调。

本节用 `scripts/probe_incremental_size.py` 直接调 `pwin.walk_forward` 补上
这三条：800 个决策日 × 20 picks、H=20、window=60（`n_eff≈35`，刚过
`MIN_NEFF=30`）、末 120 天抬升、每个格子 40–100 次重复。

#### 一、零效应覆盖率：三种零分布都是 0%，但原因不是同一个

| 零分布 | PASS | 95% CI | 可判定 | 改善 | mde | se |
|---|---|---|---|---|---|---|
| iid | 0.0% | 0–3.7% | 100% | −1.40e−4 | 0.00127 | 1.10e−4 |
| ma20 | 0.0% | 0–3.7% | 100% | −1.72e−3 | 0.00425 | 1.31e−3 |
| block | 0.0% | 0–3.7% | 100% | −4.47e−3 | 0.00881 | 3.14e−3 |

- **iid**：门槛 `0.001` 是零分布 `se=1.1e−4` 的 **9 倍**。它不是「检验严格」，
  是门槛远在噪声之外，任何样本量下都过不了。
- **ma20**（共同因子为 20 日移动平均，只有重叠、没有因子持续性）：门槛是
  `se=1.31e−3` 的 **0.76 倍**，已经在噪声里；但 `E[d]=σ_c²−σ_w²<0` 把
  `lcb` 压住，仍然过不了。真实账本的日命中 acf(1)=0.68–0.88（§9.28），
  比 ma20 的 0.37 更高，所以 ma20 是「自相关」这一维的下界。
- **block**（真实行情账本 40 天循环块自助）：门槛是 `se=3.14e−3` 的
  **0.32 倍**，同样在噪声里。它的改善是 −4.47e−3，因为块自助保留了账本
  自己的负漂移（momentum 胜率 38.4%），漂移把 `lcb` 推到门槛以下。
  **所以 block 的 0% 不能读成「size 受控」**——它是被漂移压住的，不是被
  检验挡住的。

一句话：0% 的 size 不是同一个事实的三种证据，是三种不同的失败方式。

#### 二、目标效应功效：倒 U，峰值 30%，a=0.5 时归零

| a（末段真实抬升） | iid PASS | ma20 PASS | block PASS | 改善(iid) | mde(iid) | se(iid) |
|---|---|---|---|---|---|---|
| 0.05 | 0% | 0% | 0% | +7.2e−5 | 0.00149 | 2.0e−4 |
| 0.10 | 10% | 0% | 0% | +7.1e−4 | 0.00239 | 5.6e−4 |
| 0.15 | 22% | 2% | 0% | +1.8e−3 | 0.00398 | 1.2e−3 |
| **0.20** | **30%** | 6% | 0% | +3.3e−3 | 0.00625 | 2.1e−3 |
| **0.25** | **30%** | 8% | 2% | +5.2e−3 | 0.00907 | 3.2e−3 |
| **0.30** | 16% | **12%** | 2% | +7.4e−3 | 0.01247 | 4.6e−3 |
| 0.40 | 0% | 2% | **6%** | +1.3e−2 | 0.02133 | 8.2e−3 |
| 0.50 | 0% | 0% | 0% | +2.1e−2 | 0.03286 | 1.3e−2 |

三条读数，每一条都反直觉：

1. **峰值只有 30%（iid）/ 12%（ma20）/ 6%（block）**，而名义功效应该是 80%。
   这不是样本不够——800 天、`n_eff=35`、可判定率 100%。
2. **三条曲线都是倒 U**：iid 峰在 0.20–0.25，ma20 在 0.30，block 在 0.40。
   a 从峰值加到 0.50（真实胜率从 75% 抬到 100%），PASS 率全部掉回 0%。
3. **改善本身是单调上升的**（+5.2e−3 → +2.1e−2），但 `se` 涨得更快
   （3.2e−3 → 1.3e−2）。`lcb = mean − 1.645·se` 因此变负。

#### 三、为什么必然倒 U

`d_i = δ_i·(δ_i + 2(p*−y_i))`，其中 `δ_i = p_clim,i − p_i`。分两种日子：

    变点后：δ ≈ a(1−share)  ⇒  E[d | 变点后] = δ² = a²(1−share)²
    变点前：δ ≈ 0           ⇒  d ≈ 0
    全样本：E[d] ≈ share·a²(1−share)²           （∝ a²）

`share` 是变点后样本占可评估日的比例（本节 120/778≈0.15）。`se(d)` 的主导项
是 `2δ·(p*−y)`，所以它也与 `δ` 同阶，即 `se ∝ a`——比例常数由 share 与有效
样本决定，这里不写死（本节实测 se 从 2.0e−4 涨到 1.3e−2，跨两个数量级）。
于是

    lcb(a) ≈ A·a² − B·a − 1.645·c,   A = share(1−share)²,  B ∝ 1/√n_eff

**`a²` 的系数是常数，`a` 的系数也是常数，门槛 `0.001` 还是常数。** 这是一条
开口向上的抛物线减一条直线：在 `a = B/(2A)` 处取最大，之后必然下降。峰值
位置随 `share` 与 `n_eff` 移动（iid 在 0.20–0.25、ma20 在 0.30、block 在
0.40），但**下降本身不随任何参数消失**。

这解释了 30% 峰值：它不是系统性功效，是 `se` 分布左尾里那些偶然很小的格子；
也解释了为什么加大 a 反而归零——**不是因为效应太小才测不出，是因为效应变大
时噪声按同阶变大**。

这也是 §9.27 的「三个乘数」只对了一半的原因：门槛 1.5×、路径平均 7.4×、
设计效应 19–46× 都是真的，但即使把三个乘数全部消掉，倒 U 仍然在——它是
`d_i` 这个统计量的形状，不是样本量的形状。

#### 四、`mde_incremental` 开方读成胜率：差 30 倍，这个读法要撤回

零效应下 `mde_incremental = 0.00127`，开方（减门槛后）得 **1.65pp**。
如果这个读法成立，a=0.05（5pp）就该有可观功效；实测 0%，a=0.20 才 30%，
a80 在 50pp 以上都到不了。**差 30 倍以上。**

原因有两层：

1. **刻度**：`mde_incremental` 是 `d` 的 MDE，单位是 Brier²。把它开方只在
   `E[d]=a²` 时才是胜率；实际 `E[d]=share·a²(1−share)²`，所以正确的零阶反解是
   `a = √(mde / (share(1−share)²))`，share=0.15 时是 10.8pp，不是 3.6pp。
2. **正反馈**：`mde_incremental` 用的是**当前观测到的** `se`，而 `se ∝ a`。
   效应一大，`se` 跟着涨，MDE 也涨。这一步没有不动点以外的闭式解——要迭代，
   而迭代的结果就是本节测出的倒 U。

所以：

- §9.26 的「门槛 0.001 对应 3.2pp」、§9.27 的「可检出下限约 20pp」都要
  改成「**在特定 share 与 se 下的等效值**」，不能当通用换算。
- `mde_incremental` 这个字段本身没有错（它确实是 `d` 的分辨率），但它的
  句子读法错了。卡上印「这个样本能分辨 0.0032 的改善」时，读者会以为
  「0.0032 的 Brier 改善」≈「3.2pp 胜率」，这两个不是一回事；而且那个
  0.0032 只在「效应小到 se 不变」时成立。

#### 五、结论：这一栏该撤，理由从「功效不够」升级为「刻度错了」

§9.27 说「撤栏是三选一的产品决定」，当时依据是功效不足。本节把依据换成
更硬的一条：

- **size=0 不是优点**：三种零分布用三种不同的方式失败，其中两种是门槛落在
  噪声里、被负偏压住。一个 size=0 且 power 峰值 30% 的检验，不提供信息。
- **power 非单调**：真实增量越大，越检不出。任何「等样本够了就能判」的计划
  都建立在单调性上，而这里没有。
- **刻度不可解释**：`mde_incremental` 开方差 30 倍，句子里的「3.2pp」是错的。

三条合起来，`verdict_incremental` / `significance_incremental` 不是「暂时
不可判定」，是「判定了也不该信」。**建议删除这两个字段**（连同 `estimate()`
透传、API 两个键、前端一栏、35 处断言）。这是破坏性变更，影响面清单见下，
等产品确认后执行：

```
backend/app/decision/pwin.py        walk_forward 返回 2 键 + 4 个分支文案
backend/app/decision/pwin.py        estimate() 透传 2 键
backend/app/api/decision.py         2 处字段
frontend/src/pages/Decision.tsx     1 栏
backend/tests/test_decision_pwin.py  ~30 条断言
backend/tests/test_decision_api.py   ~5 条
```

保留 `improvement_vs_clim` / `se_incremental` / `truncated_days`：它们是
**描述**（近端与全历史的 Brier 差是多少、分母有多大），不是**判定**。
描述可以留，判定要撤。

#### 六、边界

- `block` 用的是 325 天探索样本循环到 800 天，块会重复，长程结构是造出来的。
  它只回答「这份账本的结构下读数是多少」，不能反证真实账本有增量。
- `ma20` 的日命中 acf(1)=0.37，低于真实账本的 0.68–0.88，所以它是自相关
  这一维的下界；真实结构只会让 `se` 更大、power 更低。
- a 的定义是「末 120 天相对前 680 天的绝对胜率抬升」，不是「全样本平均抬升」。
  换段长会改 `share`，从而改 `Δ`；本节的倒 U 结论对段长不敏感（只要
  `0<share<1`），但具体峰值位置会移动。

#### 七、本轮改了什么

- 新增 `scripts/probe_incremental_size.py`：三种零分布、八档变点、Wilson
  区间、`a80` 插值、MDE 开方对照。
- 新增 7 条契约测试（`test_decision_probe_size.py`）：ma20 的边际校准
  （√1.05·Φ⁻¹(p)，不是 2(p−0.5)）、acf 衰减与 20 日断崖、block 的 bump 是
  绝对抬升不是翻转率、pick 数不变、`_interp80` 插值、倒 U 形状、Wilson 端点。
- `pytest -k decision`：**157 passed**（145 + 12，含 §9.31 的 5 条）。
### 9.31 行业×规模中性化：low_turnover 掉了一半，但 t 反而升了（2026-11-05）

> ⚠️ **§9.32 已推翻本节的产品结论**：本节所有 bp 读数都减的是**格内中位数**，
> 而 `fwd` 右偏，均值−中位数 ≈ 195bp（大盘格 92bp、中盘格 208bp）。换成
> 与组合 P&L 同中心的格内均值后 low_turnover 是 **+7.0bp（t=0.14）**，不是
> +99。本节的**统计描述**（行业分布、格子集中度、回退率、快照前视方向）
> 仍然有效；凡涉及效应量、「行业内仍有 +110」「对外表述按 +99」的句子，
> 以 §9.32 为准。

§9.29 把所有基准都只按成交额分层。成交额是规模/流动性的代理，不是行业。
低换手策略选出来的 20 只如果集中在银行、电力、港口、公路这些「天生低换手」
的板块，那 +185bp 里就混着行业 beta——它对「换手率是不是信号」一点证据都
没有。预注册 §2 把这条写成硬条件：**换成行业×规模双重分层前，任何对外表述
都不成立**。本节就是那一层，也是账本里最后一个没被证伪的稳健性缺口。

#### 一、五个基准，逐步加控制

同一批 325 个决策日、同一批 picks（20 只等权、H=20），只换基准：

| 策略 | 全样本 | 成交额5 | 行业 | 行业×规模 | 回归残差 | MDE(全) | MDE(行×规) |
|---|---|---|---|---|---|---|---|
| momentum | +43.2 (0.18) | +192.8 (1.22) | +138.1 (0.77) | +138.7 (0.86) | +65.8 (0.41) | 588 | 403 |
| reversal | +122.0 (1.50) | +194.5 (2.40) | +128.8 (1.74) | +135.5 (1.85) | +133.1 (1.86) | 202 | 182 |
| low_turnover | +185.2 (2.14) | +212.1 (2.17) | +110.0 (2.66) | **+99.0 (2.30)** | +147.6 (1.96) | 215 | **107** |
| high_turnover | −405.5 (−2.72) | −251.3 (−2.18) | −333.9 (−2.73) | −324.0 (−2.70) | −384.9 (−3.08) | 370 | 299 |

（均值 bp，括号内 t；se 用 40 天循环块重抽。行业=同花顺一级 90 个，
规模=流通市值三分位，格内 <8 只退回行业内中位数。回归残差 =
`fwd ~ 行业哑变量 + log(流通市值)` 的残差，再减当日中位数。）

先记三条口径事实：

- **四个策略在五个基准下全部同号**。预注册 §2 的「双基准同号」条件满足，
  而且是在比它更细的两层上满足的。
- **残差与格内基准不是同一个中心**。OLS 残差以均值为中心，`fwd` 右偏
  （均值 − 中位数 ≈ +200bp），不减中位数会让残差列系统性低 150–200bp，
  看起来像「控制后归零」。本表已减，这一条修之前 low 的残差是 −2.5bp。
- **t 不能跨列比大小**。行业基准的 t 从 2.14 升到 2.66，但均值从 +185 掉到
  +110——t 上升完全来自分母（se 86→41），不是分子。看到 t 变大就说「更显著」
  是这张表最容易犯的错。

#### 二、low_turnover：行业解释 41%，规模几乎不解释

+185.2 → +110.0 是行业，−75.2bp（−41%）；+110.0 → +99.0 是规模，−11bp（−6%）。
所以「低换手溢价」里有四成是「选了哪个行业」，不是「选了什么股」：

```
low_turnover 的行业分布（325 天平均持仓只数占比）
  银行 19.9%  电力 9.9%  石油加工贸易 7.3%  港口航运 6.8%
  公路铁路运输 5.6%  保险 5.5%  煤炭开采加工 5.2%  通信服务 3.3%
```

前七名合计 60%，全部是低估值/高股息/低换手的成熟行业。这是行业 beta 的
直接读数，比任何 p 值都直观。

但**行业内仍然有 +110bp（t=2.66）**：同一个行业里，换手率低的那 20 只仍然
跑赢同行业中位数。所以「换手率是信号」这句话在控制行业后仍然成立，只是
量级要按 110bp 报，不是 185bp。

#### 三、精度红利：MDE 从 215 掉到 107，而且不用换持仓

行业基准把 se 从 86.4 压到 41.4（−52%），MDE 从 215 降到 107。§9.29 第六节
写「20 只形态下 215bp 是 MDE 的地板」，**这句要限定为「相对全市场基准的
地板」**：换成同行业基准，同一批 20 只、同一个持有期，分辨率翻倍。

机制不是样本变多，是基准变稳：全样本中位数每天随行业轮动漂，同行业中位数
不漂。日序列的方差因此小一半，`se ∝ sd` 跟着小一半。

这条**不能**拿来当主口径。预注册 §2 写死了主基准是全样本中位数，理由正是
防止「哪个基准 t 高用哪个」。行业基准的用途是敏感性：它证明效应不是行业
beta 造出来的，不证明效应比 185bp 更大。而且它换了一个问题——「同行业内
选股是否有效」与「这个组合是否跑赢大盘」是两件事，后者才是产品口径。

附带一个可执行的读数：如果产品的业绩口径愿意写成「同行业超额」，200bp 的
确认门槛在 **325 天就已经达到**（MDE 107），不必等 575 天（161）。这是基准
选择带来的，不是样本带来的。

#### 四、momentum 与 reversal 在行业中性后几乎重合

+138.7 (t=0.86) vs +135.5 (t=1.85)，差 3bp。全样本下它们是 +43 vs +122，
差 79bp。也就是说：

**「动量」与「反转」在 20 日尺度上的差别，主要是行业暴露，不是时序方向。**

这与 §9.29 的倒 U 分位形状是同一件事的两面：20 日动量的 IC 为负（−0.054）
而 top-20 为正（+43），因为最高一层最差；行业内中性化把行业那一段拿掉后，
两个方向的读数都回到 +136 附近，谁也没有方向性。所以「momentum 是反转的
镜像」这个观察在**信号层**成立（IC 逐位相反），在**产品层**不成立（收益
不一样），而在**行业中性层**又成立（又一样了）。三层各是一个对象。

#### 五、high_turnover 不变：仍是尾部，不是行业

−405.5 → −324.0（t=−2.70），只掉 81bp（20%）。行业中性化没有救它，也没有
推翻它：结合 §9.29 的 top10% 归零（−9.4）与尾差 −396（t=−3.70），结论仍是
「这一栏的负号只存在于它自己那 20 只构成的尾部里」。行业不是它的解释，
宽度才是——而宽度一铺，它就没了。

#### 六、口径与前视：两个快照，方向已知

- 行业分类是 **2026-11-05 的同花顺一级快照**（5,579 只），不是历史版本。
  回测跨 2025-06 起，期间的重组/改名/新股归类都没有历史。
- 流通股本是同一快照；`float_mv = raw_close_t × float_shares` 只有股本是
  前视，价格是当日的。
- 两个偏差都让**中性化变弱**：用错的行业去控制，残差里会留下本可控掉的
  暴露，于是「控制后仍有超额」更容易成立。所以本节的**否定性结论是稳的**
  （行业确实吃掉了 75bp），**肯定性结论可能偏高**（+110 可能还含未控尽的
  行业）。

#### 七、本轮改了什么

- 新增 `scripts/probe_neutral.py`：五个基准、格子集中度、回退率、行业分布、
  规模分位，`--nmax=325` 与 §9.28/§9.29 同源同序。
- 新增 5 条契约测试（`test_decision_neutral.py`）：五个基准的减法恒等、
  格内中位数只在本格、小格子回退、FWL 残差只剩噪声、残差中位数为 0。
- `pytest -k decision`：**157 passed**（145 + 12，含 §9.30 的 7 条）。

#### 八、验收

- 全样本列逐位复现 §9.28：+43.2 / +122.0 / +185.2 / −405.5。
- ~~四个策略在五个基准下同号；low 的行业内 t=2.66 是四列里最高的。~~
  §9.32：五个基准都是中位数（同一个泛函的五个精度），同号不构成独立确认；
  均值口径下 low 的行业内读数是 +4.4bp（t=0.10）。
- 格子集中度：最大格 21%（low），远低于「picks 自己决定基准」的 60% 线；
  回退率 12%，90×3 的切分没有过细。
- 规模分位：low 0.79（大盘）、high 0.46（中盘）、mom 0.66、rev 0.45——
  与「低换手=大盘价值」的先验一致。

### 9.32 中位数基准不是 P&L 基准：随机组合也「跑赢」194bp（2026-11-05）

§9.31 的结论是「控制行业与规模后 low_turnover 仍有 +99bp」。那句话建立在
一个从未被检验的假设上：**减掉截面中位数的剩余，是选股能力**。本节用一个
安慰剂把这个假设拆掉。结论先说：+99bp 里的绝大部分不是能力，是偏度；
换成与组合 P&L 同中心的均值基准，low_turnover 是 **+7.0bp（t=0.14）**。

#### 一、安慰剂：没有任何信号，也「跑赢」194bp

`scripts/probe_bench_placebo.py`：325 个决策日，每天从同一可交易池里**均匀
随机**抽 20 只（无排序、无信号），每日重复 20 次取均值，按日序列做 40 天
循环块重抽。

| 口径 | 均值 | se | t |
|---|---|---|---|
| 减截面中位数 | **+194.0** | 33.8 | **5.75** |
| 减截面均值 | −1.6 | 2.9 | −0.56 |

随机组合减中位数，`t=5.75`。同一个组合减均值，`t=−0.56`。

**截面均值 − 中位数 = +195.7bp/日**，这就是那个偏移的全部来源。`fwd` 右偏
（20 日收益的截面分布有长右尾），而任何**多头**组合的 `mean(fwd_picks)` 都
落在截面均值附近；减去中位数，等于把「均值 − 中位数」这个常数收进超额。
它与选股无关，与信号无关，只与「你是不是只做多」有关。

#### 二、偏移不是常数 195，等于**那一个基准格**的均值−中位数

同一批 picks，只换基准中心，读数差如下（bp）：

| 策略 | 全样本 | 行业×规模 | 回归残差 | 格内中心偏移 |
|---|---|---|---|---|
| | med → mean | med → mean | med → mean | |
| momentum | +43.2 → **−152.5** | +138.7 → **−68.9** | +65.8 → −84.3 | 207.6 |
| reversal | +122.0 → **−73.7** | +135.5 → **−16.0** | +133.1 → −17.1 | 151.5 |
| low_turnover | +185.2 → **−10.5** | +99.0 → **+7.0** | +147.6 → **−2.5** | 92.0 |
| high_turnover | −405.5 → **−601.1** | −324.0 → **−522.4** | −384.9 → −535.0 | 198.4 |

偏移大小由 picks 落在哪类格子决定：low 选的是大盘（规模分位 0.79），大盘
收益分布相对对称，偏差 92bp；momentum 偏中盘，偏差 208bp。所以「+99 掉到 0」
不是巧合，是 92bp 的偏移被拿掉之后剩下的 7bp。

复现：`probe_neutral.py --basis=mean`（`--basis=median` 与 §9.31 逐位一致：
+43.2 / +122.0 / +185.2 / −405.5）。

#### 三、按均值中心重读：四个策略没有一个是正的

- **low_turnover +7.0bp（t=0.14）**，OLS 残差 −2.5bp（t=−0.03）。两套控制
  模型（饱和格内均值 vs 行业哑变量+log 市值）给出同一个零。**「低换手溢价」
  在控制行业与规模后不存在**；+185 是「跑赢中位数」，不是「跑赢平均持仓」。
- **momentum −68.9、reversal −16.0**：两个方向都是负的，且不可区分。
  §9.31 第四节「中性化后几乎重合」在均值中心下仍然成立，但共同值从 +136
  变成 −69 与 −16，不是「都有效」。
- **high_turnover −522.4（t=−4.65）**：唯一的强读数，而且比中位数口径更负。
  换手率信号的真实形态是**高换手在扣掉同格均值后大幅跑输**，不是「低换手
  跑赢」。对只做多的产品，这条只能当排除规则用。

「四个策略在五个基准下全部同号」这句话在 §9.31 里是真的，但五个基准全是
中位数——同一个泛函的五个精度，不是五个独立检验。换成均值中心，三个为负、
一个为零，同号性消失。

#### 四、预注册的问题不在 MDE，在估计量

预注册 §2 把估计量写成 `exc_i = fwd_i − median_j(fwd_j)`，§4 又用
`net = mean(exc) − 19bp > 0` 当 PASS 条件。两句话拼起来意味着：

    随机组合：net = 194.0 − 19 = +175bp，t = 5.2，MDE = 84bp

**按 §4 的字面规则，一个零信号的随机组合会拿到 PASS**（主基准净值为正、
Holm 显著、MDE ≤ 200bp）。所以 §4 判不出能力，它判的是「这个组合是不是
多头」。§9.31 第三节「MDE 107 ⇒ 200bp 门槛已达到」也跟着失效：107 是那个
错误估计量的分辨率，不是经济效应的分辨率；均值中心的格内 se 是 51.5，
对应 MDE ≈ 128bp，而观测值是 +7.0bp。

§2 自己写过「vs 截面中位数是选择收益，不是 alpha」，但 §4 仍然拿它当 P&L
检验。**这是 §9.30 那个单位混淆往上再挪一层**：那里混的是 Brier 与 pp，
这里混的是「跑赢典型股票」（命中率口径，中位数是正确中心）与「跑赢平均
持仓」（P&L 口径，均值是正确中心）。

#### 五、哪一半不受影响

`p_win` 的 `median` 口径**不受影响**：它的因变量是 `outcome_med = 1[fwd >
截面中位数]`，是一个 50/50 的分类目标，中位数就是它的自然中心，偏度不进
标签。受影响的只有「用 bp 报超额」的那条线：§9.28–§9.31 的 bp 读数、预注册
§2/§4/§6 的效应量与 MDE、以及一切「对外表述按 +99bp」的措辞。

#### 六、复现与验收

- `probe_neutral.py --basis=median` 逐位复现 §9.31；`--basis=mean` 给出上表
  右列。两个口径共用同一份数据、同一批 picks、同一个块重抽。
- `probe_bench_placebo.py --nmax=325 --draws=20 --seed=0` 给出 +194.0（t=5.75）
  与 −1.6（t=−0.56）。换 seed 或 draws，前者不动，后者仍应在 ±2 内。
- 判定：中位数基准的安慰剂 |t| 大而均值基准的 |t| 小 ⇒ 基准中心错了；
  两者都接近 0 ⇒ 本节的推理被证伪。

#### 七、下一步（按信息增益）

1. 预注册 §2/§4 的估计量换成均值中心，并在**读取确认段之前**记入变更记录；
   两个中心同时报，中位数只作命中率诊断。
2. 确认段的 PASS 规则在换中心之前不具备判别力，不应执行。
3. §9.31 的六条产品结论按第四节撤回，代码层面的 `verdict_incremental` 清理
   顺延——它要改的文档理由有三条刚刚被证伪，先改口径再删代码。

### 9.33 基准中心的校准：200 只零能力组合全部被判成 ECON+（2026-11-05）

§9.32 给出的是一个**点估计**（随机组合减中位数 +194bp）。本节把两件事做实：
换中心后的 §4 全流程判定，以及零能力被判成 ECON+ 的**频率**。

结论先说：**中位数口径下 200/200 只零能力组合全部 ECON+，且 100% 满足
MDE≤200；均值口径 0/200。** §4 在中位数口径下不是「可能」被零能力通过，
是必然。

§9.32 的数值全部复现（+195.7bp 的偏移、PASS 可达），本节改的是它的两处
**措辞与机制**：偏移在**全样本层**是严格常数（第一节），以及零能力的 MDE
比策略更小（第五节）。

#### 一、换中心只平移一个常数：+195.7bp，离散度 0.00

`probe_window.py --rebuild --basis=mean|mcap` 重算 picks 的 `exc`，
`probe_net_diff.py --basis=...` 走同一条判定链。同一批 picks、325 天：

| 策略 | median | mean | mcap | Δ(med−mean) | Δ(mean−mcap) |
|---|---|---|---|---|---|
| momentum | +43.2 | **−152.5** | −147.1 | +195.7 | −5.4 |
| reversal | +122.0 | **−73.7** | −68.2 | +195.7 | −5.4 |
| low_turnover | +185.2 | **−10.5** | **−5.1** | +195.7 | −5.4 |
| high_turnover | −405.5 | **−601.1** | −595.7 | +195.7 | −5.4 |

位移的离散度 **0.00bp**（`probe_net_diff.py` 第四节自己算这个离散度，>0.2 就报 ✗）。
这比 §9.32 的说法更硬：在**全样本**这一层，偏移是严格常数，中位数口径的整张
表就是均值口径的平移副本。§9.32 说「偏移不是常数 195」指的是**格内**层
（92–208bp，随格子变），两句话不矛盾，但要标明是哪一层。

市值加权与等权的差只有 −5.4bp：这段样本里等权与市值加权的 20 日收益几乎一样，
所以「买得起的市场组合」与「等权均值」在这里不构成分歧。**这是样本的性质，
不是一般结论**——换一段小盘占优的行情，两者会分开。

#### 二、配对差与中心无关 ⇒ 账本的相对结论全部存活

常数在相减时消掉，所以配对表在三个中心下逐位相同：

| 对照 | Δ | se_块 | t | p |
|---|---|---|---|---|
| low_turnover − high_turnover | +590.6 | 190.0 | **3.11** | 0.0019 |
| reversal − high_turnover | +527.5 | 142.0 | **3.71** | 0.0002 |
| momentum − high_turnover | +448.6 | 185.2 | 2.42 | 0.0154 |

这是账本里唯一**不需要重算**的一类数。要撤回的只有「A 相对 0 有多少」，
不是「A 比 B 好多少」。

#### 三、§4 全流程：十二格里没有一格能 PASS

净 LCB 与 Holm 并列（判定列只是区间条件；§4 的 PASS 还要 Holm<0.05 与
MDE≤200）：

| 策略 | median | mean | mcap |
|---|---|---|---|
| momentum | 43.2｜LCB −328.7｜.457 | −152.5｜−476.4｜.486 | −147.1｜−445.2｜.594 |
| reversal | 122.0｜−28.5｜.200 | −73.7｜−233.2｜.486 | −68.2｜−295.5｜.594 |
| low_turnover | 185.2｜**+26.9**｜.081 | −10.5｜−206.4｜.486 | −5.1｜−230.4｜.594 |
| high_turnover | −405.5｜−660.5｜.008 | −601.1｜−819.2｜.000 | −595.7｜−826.6｜.000 |

**按 §4 的字面规则，十二格全部是 UNRESOLVED**：MDE 是 202–588，没有一格
≤ 200；low_turnover 在 median 下 Holm 0.081 也过不了。

这一条很重要，它改变了处置方向：**§4 的 PASS 门槛从来没有被跨过**。偏移不是
从 PASS 那道门进来的，是从**「ECON+ / 对外表述」这条侧门**进来的——§9.31
写「对外表述只能按 +99bp 报」，用的是区间条件，不是 PASS。所以要修的是
估计量的中心与措辞，不是阈值。

#### 四、安慰剂校准：单只组合，200 只，100% / 0% / 0%

`probe_basis_calibration.py --nmax=325 --draws=200 --reps=1000`：每天从同一
可交易池均匀抽 20 只，**每只组合每日只抽一次**；200 只 × 325 天；判定用
`probe_net_diff.evaluate_series`，与上面的策略表同一条代码路径。

| 中心 | 偏移 | 组合均值 | ECON+ 率 | MDE 中位 | 同时过 MDE≤200 |
|---|---|---|---|---|---|
| median | +195.7 | +196.0 | **100.0%** | 91 | **100.0%** |
| mean | +0.0 | +0.4 | **0.0%** | 37 | 0.0% |
| mcap | +5.4 | +5.8 | 0.0% | 144 | 0.0% |

偏移必须等于组合均值——随机组合的期望就是这个常数，与信号无关。两者对上了
（+195.7 vs +196.0），说明这不是抽样波动。

**为什么必须用单只而不是日平均**：§9.32 的「每日 20 次取均值」se 是 33.8，
本节单只组合 se 是 36.6，几乎相同。也就是说日序列的方差**不由抽签噪声决定**，
而由「均值 − 中位数」这个偏移在日间的波动决定。所以 §9.32 的构造没问题，
它的结论也不需要修正；区别只是当时只有一个点估计，现在有 200 只的频率。

#### 五、机制：为什么零能力的 MDE 反而更小

这是本节最反直觉的读数，也是必须写下来的原因。

**`MDE≤200` 这道门，四个策略一个都过不了，200 只零能力组合全部过得了。**

| | MDE 范围 |
|---|---|
| 四个策略（三个中心 × 十二格） | 202–588 |
| 零能力组合（median / mean / mcap 的中位） | 91 / 37 / 144 |

原因不是安慰剂更「干净」，而是**它每天重抽**：随机组合的日序列近似独立
（今天的 20 只与明天无关），而策略的 picks 高度持续（低换手股明天还是低换手），
40 天块重抽下有效样本小得多。持续性推高 se，se 推高 MDE，MDE 卡住判定。

所以 §4 的实际筛选方向是反的：**它筛选「序列平稳」，不筛选「有超额」**。
一个每天乱买、从不重复的组合，比一个坚持同一套规则的策略更容易过门。

这条也解释了为什么 §9.31 会写出「200bp 门槛在 325 天已达到」：那句话成立，
但它成立的代价是把基准换成格内中位数——偏移最大的口径。

#### 六、§4 的两条要求互相打架

- **分辨率**要细口径：20 只 / 20 日 / 325 天下，只有**格内**口径的 MDE 进得了
  200（low 128bp；全样本均值口径 268bp，全样本中位数 215bp）。
- **中心**要等权均值：只有它满足 E[exc | 无能力] = 0。
- 而**格内中位数**恰好是偏移最大的那个口径（low 92bp、momentum 208bp）。

两条要求指向不同口径，且**能过 MDE 门槛的那个口径偏移最大**。这不是巧合：
格内中位数把「同格里有几只大涨」直接变成超额，格子越细，这个成分越大。

出路是**格内均值**：MDE 128 ≤ 200，中心无偏，读数 +7.0bp（se 51.5，t=0.14）。
要 PASS 需毛超额 > 19 + 1.645×51.5 = **103.7bp**，观测 7.0bp，差 97bp。
所以 low_turnover 在正确口径下是**探索样本上可判定的 FAIL**，不是 UNRESOLVED：
分辨率够，效应不在。（**不是** §4 的确认判定——预注册 §5 禁止拿这 325 天做
确认，见 §9.34 第五节。）

**这个结论不依赖选哪个均值口径**：全样本 −10.5、成交额五分位 −12.9、行业
+4.4、格内 +7.0、回归残差 −2.5，五档全部 ≤ +7。选格内只是为了让 MDE 进
200，不是为了挑一个好看的数字。

#### 七、处置

1. **撤回**：§9.28–§9.31 一切「vs 中位数」的 bp 读数；预注册 §1/§2/§6 的
   效应量与 MDE；「对外表述按 +99bp」。
2. **保留**：配对差（第二节）、`p_win`（中位数是 50/50 分类的自然中心）、
   §9.31 的行业/规模集中度诊断（与中心无关）。
3. **唯一存活的强读数**：high_turnover 在三个中心下都是 −522…−601bp
   （t = −4.2…−4.7）。它只能当**排除规则**用，不能当「低换手溢价」的反面。
4. **代码清理继续压住**：要改的六节文档里三节的数字刚被证伪，先改口径。

#### 八、复现与验收

```bash
python scripts/probe_window.py --rebuild --basis=mean
python scripts/probe_window.py --rebuild --basis=mcap
python scripts/probe_net_diff.py --nmax=325 --basis=median   # 逐位复现 §9.31
python scripts/probe_net_diff.py --nmax=325 --basis=mean
python scripts/probe_basis_calibration.py --nmax=325 --draws=200 --reps=1000
```

可证伪的验收条件：

- `--basis=mean` 与 `--basis=median` 的位移必须是 **+195.7 ± 0.1bp 的常数**，
  且对四个策略都相同（脚本自己算离散度并断言 <0.2）。若随策略变 ⇒ picks
  在换中心时动了，本节不可比。
- 安慰剂的 ECON+ 率在 median 下必须是 **100%**（200/200），在 mean 下必须是
  **0%**（0/200）。换 seed 或 draws，前者应恒为 100%、后者应 ≤ 5%。
  若 median 的率掉到 50% 以下 ⇒ 偏移没有 195bp 那么大，本节结论被证伪。
- 安慰剂的 MDE 中位必须 < 策略的 MDE（37–144 vs 202–588）；若反过来，说明
  「持续性」这个解释错了。
- `pytest -k decision`：**165 passed**（158 + 本节 7 条）。

#### 九、下一步

1. 预注册 v5：主口径换成**行业×规模格内均值**，median 降为命中率诊断；
   全样本均值 / 成交额五分位均值 / 市值加权作为敏感性，四档同号才算同号。
2. 这一步必须在**第一次读取确认段之前**落定（§11 的处理意见已经写了这条）。
3. 不再在 325 天上找「更好的基准」：五档均值口径全部 ≤ +7bp，换基准换不出
   效应，只能换来分辨率。

### 9.34 确认段闸门：一句「没读」值多少钱（2026-11-05）

§9.33 的处置全部落在文字上：「换中心发生在读取任何确认段结果之前」。这句话
无法被证伪，因此也无法被信任。本节把它拆成三件可复核的事，并顺手发现：
**漏洞不是过去的某次操作，是明天开盘后的第一次例行重建。**

#### 一、漏洞按时钟打开，不按人的意图打开

`probe_window.build()` 的决策日边界是 `len(cal)-HORIZON`，**没有日期上限**。
2026-11-05 有 366 个交易日（索引 0…365），决策日可取 `i ∈ [20, 345]`，
共 326 天，末日 2026-10-08。再落一根 2026-11-06 的 bar，索引变 0…366，
`i = 346`（2026-10-09）立刻进入可算范围——第 327 个决策日，确认段的第一天。

从那一刻起，`python scripts/probe_window.py --rebuild` 会把确认段写进三个
`exc` 缓存和胜率缓存，**不打印任何提示**；而 `probe_net_diff.py --nmax=325`
只是决定打印 325 行，不决定算过什么。也就是说：即使一个人完全诚实，只要
他第二天照常重建一次，`git status` 里就会多出一份含确认段数值的文件。

#### 二、签字时能证明什么

`scripts/decision_gate.py audit`（2026-11-05T23:38+08:00）：

| 项 | 值 |
|---|---|
| 价格日历 | 366 天（2025-05-07 … 2026-11-05） |
| 可算决策日 | 326，末日 2026-10-08 |
| 其中确认段 | **0**（2026-10-09 的 t+20 要 2026-11-06 收盘才齐备） |
| 4 个缓存的确认段决策日 | **0 / 0 / 0 / 0** |

第二行是这个节的关键：**签字时不存在可读的确认段结果**。这不是「我读了但
没看」，是分母为零。任何「你 10 月 9 日之后改了口径，一定偷看过」的质疑，
在这一行面前要么给出反例（某个产物里有 ≥ 2026-10-09 的决策日），要么撤回。

一个必须写明的口径：确认段的采样单位是**决策日**，不是价格日历。
2026-10-08 的决策要用 2026-11-05 的收盘价，那是它的 20 日收益。把价格日历
也截到 10-08，会把第 326 天丢掉，样本从 326 变 325——那是另一种错。

#### 三、闸门做了什么，没做什么

| 机制 | 管什么 |
|---|---|
| `cap_calendar()` | 决策日截到 2026-10-08，保留 t+20 所需价格 |
| `require_exploration()` | 缓存/日历含确认日则拒绝运行；`--include-confirmation` 才放行 |
| `log_read()` | 放行时追加一行，带上一行哈希；`verify_log()` 查改写与删行 |
| `seal()` | 冻结 4 个脚本 + 4 个缓存的哈希，写进 `docs/decision-seal.json` |

**管不住的**：有人直接 `scan_parquet` 打印一个数。那只能按预注册 §5.1 定义
为「读取」，靠日志为空 + 封签未漂移来反证，不能靠代码阻止。把这条写进文档
比假装闸门完备有用——闸门的作用不是让人读不到，是让「读过」留痕。

`cap_calendar()` 当天重跑中位数口径，与原缓存**逐字节相同**（`cmp` 通过，
326 天、末日 10-08）：截断没有改动任何已有样本，只关掉了明天会开的那扇门。

#### 四、封签为什么不能覆盖文档

`seal_sha256` 要写进预注册 §12。若把预注册本身哈希进去，改一次文档就改一次
哈希，改一次哈希又要改文档——循环，永远对不上。所以封签只覆盖**代码与产物**，
文档只负责「引用」这个哈希。这条不是实现细节：它决定了封签能证明的是
「分析代码没动过」，不是「文档没动过」。后者由 git 负责。

#### 五、措辞更正：FAIL 是探索样本的标签

§9.33 第六节写「low_turnover 在正确口径下是一个可判定的 FAIL」。这句话在
**探索样本**上成立（+7.0bp，se 51.5，MDE 128 ≤ 200），但它不是 §4 的确认
判定：预注册 §5 明文规定这 325 天只能定样本量。确认段的 §4 至今一格未执行，
四个策略没有任何一个拥有 PASS/FAIL 的确认结论。把探索诊断写成 FAIL，等于
把「分辨率够、效应不在」偷换成「预注册的检验拒绝了它」——后者没有发生。

#### 六、未执行：v5 之下，确认段能改变什么决定

把 §4 的 MDE 门代进 575 天（325 + 250）的预算。只用**已验证的均值全样本
MDE**（515 / 218 / 268 / 328，`probe_net_diff --basis=mean` 实跑），按
`se ∝ n^{-1/2}` 外推——这是近似，不是承诺：

| 策略 | MDE@325 | MDE@575 | 575 天能出 §4 判定？ | 探索读数 |
|---|---|---|---|---|
| momentum | 515 | 387 | **否**——要 se 再降 6.6 倍（≈2,150 天） | −152.5 |
| reversal | 218 | 164 | 是 | −73.7 |
| low_turnover | 268 | 202 | 是，但只因主基准换到格内（128 → 约 96） | −10.5（格内 +7.0） |
| high_turnover | 328 | 247 | 否——负向结论另说，见候选 3 | −601.1 |

（其余三档的格内 MDE 没有按均值口径报过，不外推；§6 表里那几列仍是中位数
遗留，不能拿来填这张表。）

所以 250 天确认段的**预期信息增益是「把两个接近零的读数确认一遍」**：
能出判定的只有 reversal 与 low，而两者在探索样本上是 −73.7 与 +7.0。
momentum 即使真实效应 +200bp 也永远过不了这道门——不是样本不够，是规则把
「pick 持续」当成了「分辨率不足」，而持续性恰恰是这套策略的定义特征。
high 的 −601bp 是账本里最强的读数，却在字面上被判 UNRESOLVED。

三个候选，**均未采纳**（改 §4 必须在第一次读取之前，现在仍然合法）：

1. **把配对差 low−high 升为共同主终点。** 它中心不变（§9.33 第二节），
   325 天已 t=3.11、p=0.0019、LCB +278bp，是账本里唯一现在就站得住的经济
   陈述，而且正好是产品要做的选择（买低换手还是高换手）。代价：Holm 族
   从 4 个假设变成 5 个，阈值不变。
2. **把 MDE 从 PASS 门槛降为必报诊断。** MDE 不控制第一类错误（Holm 与
   LCB 控制），只筛精度；当精度与持续性负相关时，它系统性偏向高换手、
   低持续的组合——200 只零能力组合 100% 过门（§9.33 第五节）是这个方向的
   极端。降级后四个策略在 575 天都能出判定，代价是 PASS 会带着很宽的区间
   发布。
3. **把「已排除」从 PASS/FAIL 里独立出来。** 这是三条里最像 bug 的一条：
   §4 用**正向** MDE 去判**负向**结论是否可判定。MDE 的定义是「能检出的
   最小正效应」，把它套在负向主张上是范畴错误——high_turnover 的
   t=−4.65、净 LCB −819bp，`evaluate_series` 判 ECON−，而 §4 的字面规则
   （MDE>200 即 UNRESOLVED，即使 p<0.05）判 UNRESOLVED。两个都叫
   UNRESOLVED 的东西不是同一件事，这个命名撞车本身也该修。修法不改任何
   阈值：`ECON− 且 Holm<0.05` 即「已排除」，MDE 门只对 `ECON+` 与 `FAIL`
   适用。

三条都改「能得出什么结论」，不改「怎么算」。留给下一次签字决定。
### 9.35 判定表不是分区：v6 的三项修补与一项拒绝（2026-11-05）

§9.34 第六节留了三个候选。本节把闸门的验收实验**提前**做完，又顺手查出
两处 v5 没看见的洞，并对候选 ① 给出了否定的理由——理由是决策集合，不是统计。

#### 一、闸门的验收实验不必等到明天

§9.34 的验收标准是「明日 11-06 落盘后 `--rebuild` 仍必须是 326 天」。
这一天不必等：把真实日历后面接上 301 个合成交易日，逐个重算
`cap_calendar()` + `decision_days()`：

| bar 末日 | 日历长度 | 截断后 | 决策日 | 末日 |
|---|---|---|---|---|
| 2026-11-06 | 367 | 366 | 326 | 2026-10-08 |
| 2026-11-09 | 368 | 366 | 326 | 2026-10-08 |
| 2027-12-31 | 667 | 366 | 326 | 2026-10-08 |

301 个交易日的决策日集合只有一个元素 `{326, 2026-10-08}`。**闸门不是
「明天能挡住」，是对整个确认期恒等。** 这比一天的抽查强一档：单日通过可能
只是边界没走到。

同时补做 §9.34 只做了一半的检查：拿真数据跑一次 `--rebuild`，与封签前的
缓存逐字节比对——`.probe_per_day.json` 与 `.probe_per_day_exc.json` 均
`cmp` 通过，重建后 `verify-seal` 仍未漂移。也就是说**日常重建不会自己
把封签弄坏**，封签一旦漂移，原因只剩「改代码」或「历史数据被重算」。

（一个必须写下来的反例边界：若数据源回补/复权重算，探索日的 `exc` 会变，
封签会漂。那不是误报，是封签在替你发现「同一段历史不再是同一段历史」。）

#### 二、封签没有对 import 闭合

`probe_net_diff.py` 的配对 p 值与 se_HAC 来自 `probe_paired_diff.py`：

```python
ppd = _load("probe_paired_diff", "scripts/probe_paired_diff.py")
se_hac, p_two_sided = ppd.se_hac, ppd.p_two_sided
```

而 `SEALED` 里只有 `probe_window / probe_net_diff / probe_basis_calibration /
decision_gate`。改一行 `p_two_sided` 就能换掉整张配对表，`verify-seal` 不会响。
受影响的不只是表：候选 ① 的全部论据（Δ=+590.6、p=0.0019、LCB +278）就是
这条未封签路径算出来的。

修法不是往清单里补一个文件名，是让清单**可被检查**：
`test_seal_is_closed_over_loaded_scripts` 解析被封签脚本里所有
`_load(..., "scripts/*.py")`，任何不在 `SEALED` 里的目标直接变红。这条测试
在这次修补时立刻抓到了 `probe_paired_diff`。

#### 三、重签会抹掉上一版（已修）

`seal()` 只写一份 `decision-seal.json`。重签 = 覆盖，于是「改到哈希对得上
为止」这条路是敞开的。现在每次签字追加一行到
`docs/decision-seal-history.jsonl`，`prev` 存**上一版的封签哈希**（不是链哈希
——它要回答「我接在哪一次签字后面」），`verify-seal` 额外校验历史末行与当前
封签一致。把旧封签原样放回来时，文件哈希全部对得上，只有历史链会响。

#### 四、§6 的 575 天是合并口径，§5 说的是 250 天

§6 表头 `MDE@575（+250 天）` 把 325 探索日与 250 确认日加在一起算。但 §4 说
判定「对确认样本执行」，§5 说那 325 天「只能用于定样本量」。合并等于让 56%
的确认样本来自已经看过的数据。

`se ∝ n^{-1/2}`，325→250 是 1.14 倍 MDE，方向与「再攒 250 天分辨率变好」相反：

| 策略 | MDE@325 | 合并 575（作废） | 确认 250 |
|---|---|---|---|
| momentum | 515 | 387 | **587** |
| reversal | 218 | 164 | **249** |
| low（全样本） | 268 | 202 | **306** |
| low（格内，主基准） | 128 | 96 | **146** |
| high | 328 | 247 | **374** |

结论翻转：按 §4/§5 字面执行，250 天只能给 low 一个可判定结论，reversal 从
「能判定」变成 249——正好在门外。§6.1 已按此重写。

> **v12 更正**：上表第三列的 `146` 是 `128 × √(325/250)` 的**外推**，不是 250
> 天上的读数。格内序列落盘后重算：窗中位 **122.7**，逐窗 cohort 改正后
> **128.9–141.8**（中位 134.7）。结论不变（仍在 200 内，余量 58bp），但这个数
> 从此不能当读数引用。见预注册 §6.6 与 `backend/tests/test_decision_neutral_series.py`。

#### 五、为什么拒绝候选 ①（把 low−high 升为共同主终点）

数字是好的：Δ=+590.6、t=3.11、LCB +278、p=0.0019，而且中心是常数、配对
消掉了共同行情。拒绝的理由不在统计，在**决策集合**：

1. **它是排名，不是收益。** 两腿的绝对净区间：low `[−206.4, +152.9]`、
   high `[−819.2, −390.4]`。「low 比 high 好」与「low 赚钱」不同，上线门槛
   是后者。
2. **只要「一个都不上」还在选项里，绝对检验支配排名检验。** 四个策略的净
   收益点估计全为负（−171.5 / −92.7 / −29.5 / −620.1），点估计上的支配选项
   是 `ship nothing`。排名只在「必须挑一个」时才是决策相关的估计量——那是
   产品约束，应该由产品方明确写下来，而不是由预注册替它假设。
3. **配对差对成本免疫，而成本恰恰是它的主要内容。** 两腿都减同一个 19bp，
   差分里成本项直接消掉。这张表**结构上**看不见「高换手腿成交成本远高于
   低换手腿」，而那正是这个价差的主要来源。把一个对唯一重要成本项免疫的
   量放到结论位置，是 v4→v5 那次偏移的同一个形状，只是换了门。

因此配对表继续报、继续标探索性，但不进 Holm 族、不产生 PASS。要让它进 §4，
需同时满足：`ship nothing` 移出决策集合，且成本改成随换手率变化并重跑。

候选 ② （MDE 降为诊断）也不必做：③ 落地后，负向结论走 `net_hi<0`，不再被
MDE 门卡住；而 MDE 对**跨线**那一格仍然是对的过滤器——它挡的是「拿噪声的
形状当结论」，不是「负向结论」。

#### 六、v6 改了什么

| 文件 | 改动 |
|---|---|
| `scripts/probe_net_diff.py` | `classify()` 四格分区；`decide()` 把方向与 Holm 分开；`MDE_GATE=200` 只作用于跨线格 |
| `scripts/decision_gate.py` | `SEALED` 加入 `probe_paired_diff.py`；封签历史哈希链；`verify-seal` 查历史末行 |
| `backend/tests/test_decision_verdict_table.py` | 新，5 条：分区穷尽、门只管跨线格、Holm 对称、真实读数重放 |
| `backend/tests/test_decision_gate.py` | +5 条：`_load` 闭合、重签保历史、换回旧封签可查、删行可查、**活封签进 CI**（`test_live_seal_verifies`） |
| `docs/decision-target-prereg.md` | §4 重写为四格；§4.1/4.2 写明理由；§6.1 更正分析样本 |

325 天探索样本在新旧规则下的均值、分位、MDE、p **逐位相同**，只有判定标签
变化：high_turnover 从 `UNRESOLVED` 变成 `EXCLUDED`。这是本节唯一想改变的
东西——让账本里最强的读数可以被引用。

③ 的原表述是「MDE 门只对 ECON+ 与 FAIL 适用」。实现时把它从 PASS 里也拿掉了：`net_lo > 0` 本身已经是方向性结论（区间下界高于成本），再要求 MDE ≤ 200 等于要同一个检验同时证明两件强度不同的事。要回退，只需在 `decide()` 的 `ECON+` 分支加 `and mde <= MDE_GATE` 一行，阈值不用动。

### 9.36 生产入口的两个接缝缺陷（2026-11-06，独立评审 P1/P2）

模块内部全绿（243 个测试、封签链 11 行、读数逐位复现）之后，独立评审在
**模块之间的组合路径**上发现两个缺陷。两者都在产品层（`backend/app/`），
不在封签范围内（`SEALED` 只含 `scripts/` 下的分析代码）——修复不重签、
不动预注册，§4–§6 的统计结论不受影响。

#### 一、P1：补登历史卡的 p_win 看见未来（未来函数）

`POST /api/decision/cards` 允许 `as_of` 传过去日期（补登是合法需求，§3.5），
但 `_resolve_picks()` 把 `load_settlements()` 的**全部**记录喂给 `suggest()`。
实测（评审探针，真实落盘无 mock）：as_of=days[0] 的补登请求，账本无结算时
409；待 8 条出场日晚于该日的结算落盘后，**同一请求变成 200，p_win=0.643，
`source.n=8/hits=8`，全部来自决策日之后才存在的结果**，并随卡封存标注
`source=ledger`。`walk_forward()` 内部的 `_visible` 是正确的——破的是生产入口，
而它没有任何测试。

修法：`_exit_visible(row, as_of)`——出场日**严格早于**建卡日才可见
（§9.8 口径），缺 `exit.date` 的行 fail-closed 排除（未成交行没有 exit，
代价是少数已知结果不入样；保守方向与账本「读不到即 UNKNOWN」一致）。
`GET /pwin` 是 as-of-now 诊断，不过滤——两个入口回答不同的问题。
回归测试三条：补登必须 409、出场日 == as_of 同样不可见（边界严格）、
可见样本必须仍进 suggest()（防反向错修）。

#### 二、P2：并发 settle 产生重复结算行

`settle()` 自称幂等（顺序调用下 `done` 集合成立），但「读 settlements →
算记录 → append」三步之间没有锁：并发调用读到同一份空 `done` 集合并各自
append 同 key 行。实测 8 线程 → 8 行同键记录，`score()` 把 1 个真实样本
计成 8 个。API 是同步端点（线程池执行），UI「结算到期」连点即可触发。

修法：`_SETTLE_LOCK` 串行化整段读-算-写（函数体抽到 `_settle_locked`）。
持锁跨过行情读取，结算批处理因此不并行——有意的取舍：串行时第二个调用
在锁内重读 `done` 后空转，比各自完整重算再写重复行便宜且正确。
回归测试 `test_decision_settle_concurrency.py`：8 线程带屏障并发后
唯一键数 == 写入行数（无锁实测必红）。

#### 三、已知限制（诚实清单，不假装这些不存在）

| 限制 | 现状 | 风险 |
|---|---|---|
| settle 锁仅进程内 | 多 worker 部署跨进程仍竞争 | 需文件锁；当前单进程部署不构成风险 |
| K3 看不见损坏的卡文件 | ~~`load_cards` 静默跳过 JSON 解析失败的行~~ 已修（§9.36 之后的 K3 修复） | 见 §9.44 一 |
| `excess_lcb` 用 iid 标准误 | 按日聚类、跨日重叠的逐笔超额上反保守 | 仅是展示读数，不进门禁；待加标注 |
| `_median_ret` 的 None 被幂等固化 | 落盘时全市场分区不足 100 只，该样本 median 覆盖永久丢失 | `median_covered` 有报告无告警 → §9.44 起由 `ledger_health` 报 `degraded` |
| `as_of` 不校验是交易日 | 非交易日容错通过，entry 取下一个交易日 | 口径不严格，无实际危害 |
| 结算收益依赖 `enriched.close` 的新鲜度 | 窗口内含除权事件、而该事件尚未并入复权序列时，收益整段丢掉分红 | 行一经写入即被 `done` 键固化；见 §9.44 四 |
| `load_settlements` 静默跳过坏行 | ~~`except: continue`~~ 已修（§9.44 一） | `settle` 重算重写、`score` 少数一条，两处都不报错 |

#### 四、教训

两个缺陷都不在任何一个模块内部，而在接缝上：模块测试全绿，生产组合路径
（补登 × auto_pwin、并发 settle）无人覆盖。验证深度早已足够——同一批改动
经历过实现轮之外的四层复核——缺的是广度。**下次评审的触发条件应该是新
攻击面（新入口、新组合路径），而不是把旧结论再确认一遍。**

### 9.37 探针的第六处同型缺陷：floor 只在一支核上复刻（2026-11-07，独立复核）

#### 一、缺陷

`scripts/probe_hac_size.py` 的 `_evaluate` 对四条估计量统一按 `se = sqrt(max(lr, 0)/n)`
收底，而生产 `hac_lcb` 的截断分支收的是 `max(lr, γ0)`（重叠 H 日序列上 `LRV ≥ γ0`
是模型内硬下界，§9.23 与 `settle.hac_lcb` 的 docstring）。差异只落在
`trunc_q × iid` 一列：`lr < γ0` 的抽样占 **57.7–69.0%**，而表里的诊断列 `LRV ≤ 0`
只有 **1.0–2.6%**——从那一列完全看不出 floor 在起作用。

| n | `size%`（旧 → 生产） | `se/true`（旧 → 生产） | `floor%` |
|---|---|---|---|
| 250 | 9.65 → **4.30** | 0.879 → **1.054** | 69.0 |
| 326 | 8.35 → **4.45** | 0.904 → **1.056** | 64.3 |
| 600 | 5.60 → **3.30** | 0.952 → **1.049** | 60.1 |
| 1000 | 6.05 → **4.65** | 0.970 → **1.041** | 57.7 |

方向反转：从「截断核在 iid 上 `se` 偏低 12%、过度拒绝」变成「偏高 4–6%、略保守」。
而 `backend/tests/test_decision_hac_size.py::test_lrv_is_floored_at_gamma0` 明文断言
生产截断核 `se ≥ se_iid`——**旧读数在报告一个本仓库的测试禁止其存在的现象**。

#### 二、为什么护栏没抓住（本节的重点）

旧护栏比的是 LRV：`want = se²·n`、`got = 本地核`。但 `se²·n` **只在 floor 不绑定时**
才等于 LRV。于是「比得过」这个要求本身把探测向量限死在 MA(19)——**缺陷被向量的选法
排除在断言之外**。同一段 docstring 里还写着「±1 向量在这里会合理地红」，红的正是
floor；散文记着两支核收底不同，断言结构上看不见它。

这不是「向量挑得不好」，是**护栏的主语错了**：它比的是中间量，而下游消费的是 `se`。
主语一错，向量集就不再是自由选择的——凡能让两者分开的向量都会红，剩下能过的只有
两者重合的那一支。同一种错误在这里出现两次：先选了看不见缺陷的量，再为「看不见」
写了一段辩护。

#### 三、口径裁定（三条）

1. **行 = 生产可执行的估计量，非生产读数进列。** `trunc_q` 这一行必须报生产值，否则
   它与测试的直接断言公开矛盾，读者只能相信先读到的那一份。非生产口径降为
   `size@floor0%`（同一批抽样上只收 0）与 `floor%`（绑定率）两列：**行会被当结论
   引用，列只被当诊断读。**
2. **「旧行逐位不动」降格为「爆炸半径声明 + 逐位复验」。** 它是一次变更的爆炸半径
   证明，不是永久冻结；把它扩成永久冻结，等于把产物钉在它最不正确的那一版上。
   本轮事前声明：只动 `trunc_q × iid` 4 行 × 3 字段（`se/true`、`size%`、`size|PASS%`）
   = 12 个字段。事后逐键复验：**其余 44 行 0 字段变化**，`PASS%`/`PASS_n` 也一字未动。
3. **改数的合法性由「符号是否由代码决定」判定，不由「看起来对不对」判定。**
   `max(lr, γ0) ≥ max(lr, 0)` 逐样本成立 ⇒ `se` 必升、`size` 必降，对任何一批抽样
   都成立，**自由度为零**。四个格子逐一验过，无一例外。这次修法让生产核显得更好看
   ——**正因为如此，它必须写进档，而不能悄悄改**。反过来，一个让结论变难看的「修正」
   若给不出符号，那它就不是修正，是调参。

#### 四、信息保留

`size@floor0%` 在 **48 行上与旧 `size%` 逐位相同**（含 ma/drift 的 44 行），旧读数因此
在新产物里可复算，不必翻备份。备份 `hac_size.before13f.json` /
`probe_hac_size.before13f.py` 仍在 `data/.scratch/`。

#### 五、护栏重做（两层，不跑模拟）

- **核层**：等权 MA(19)，两支核都不收底，比权重与分母；
- **floor 层**：交替 ±1 加 6 条短 AR（φ ∈ ±0.8/±0.5/±0.2），比 `se` 本身；
- **覆盖率落盘** `prod_check_bind` = **6/16**——它掉到 0 就说明这组向量又只看得见核；
  另落 `prod_check_pairs`，与 `prod_check_rel`（9.3e-16）一起构成护栏的自述。

一条副产品：**Bartlett 的 `max(lr, 0)` 是死代码。** `lr = d'Wd/n`，`W` 是 Bartlett 核的
Toeplitz 矩阵、半正定 ⇒ `lr ≥ 0` 恒成立（2 万条随机差分序列 + p∈[2,200] 正弦扫描，
最小比值 0.0017，从未为负）。所以「统一 floor」当初看起来无害——**唯一会绑定的那一支，
恰好就是被漏掉的那支**。

补 4 条测试（`test_decision_hac_size.py`，不依赖模拟）：两核 `se` 逐位一致（带覆盖
断言；Bartlett 那条反过来钉「不绑定」）、向量化 `n_eff` ≡ `settle.effective_sample`、
截断核在 58 个向量上 `se ≥ se_iid`。`pytest -k decision` **263 → 267**（0 失败 0 错误
0 跳过，junit 复算）。

#### 六、附带一处：门禁的契约从未成立

改这个文件时发现的另一处，与本轮缺陷无关但同族：模块 docstring 写「退出码非零 ⇒ 有
估计量在 ma 列上超过 8%，CI 门禁可以只跑 `--reps 500`」，而 `bad` 把 `bartlett_q` 与
`bartlett_n13` 一起算进去了。这两支是**证伪列**——它们超 8% 正是本表要展示的事实，
`test_decision_hac_size.py` 还专门断言旧核 size ≥ 7%。所以这个退出码**从来就是 1**，
从 Bartlett 第一次被量出来那天起。一个恒红的门禁等于没有门禁。

改法：门禁只判 `trunc_q`（会上线的那支），且要求该格 `PASS_n >= 100`（n=250 与 326 的
条件 reader 只有 66 / 83 个分母，判的是噪声）。现在 `ma × trunc_q` 可读的两格是
7.57%（n=600）与 4.80%（n=1000），退出码 **0**；日志末尾把判红的格数印出来。读数不变
（重跑逐行相同，`prod_check_rel` 仍是 9.3e-16）。

#### 七、教训

六处缺陷同一个形状：**从邻支复制一行**。散文拦不住第七处，但这次暴露的是更具体的一条：
**护栏必须比对下游真正消费的量**。比中间量等于把「哪些输入能比得过」交给缺陷去决定——
它会把探测向量赶到自己看不见的地方，而护栏的 docstring 还会替它辩护。下一个护栏先问
一句：「这个断言的主语，是表里印的那个数吗？」

遗留：探针仍然保有一份向量化核副本（为速度，40 万次调用不可能走纯 Python），护栏是它
与生产之间唯一的连接点；`struct_H` 与 `bartlett_n13` 没有生产对应物，不受护栏覆盖。

### 9.38 第七处同型缺陷：探针的 HAC 不是生产的 HAC（2026-11-07，13h）

#### 一、缺陷

`scripts/probe_pwin_variance.py` 自带一份 `_hac_lcb`：Bartlett 权重
`1−k/(lag+1)`、`γ_k` 分母取 `n`、floor `max(lr, 1e-18)`。生产
`settle.hac_lcb` 的默认核是**截断**：无权重、分母 `n−k`、floor `max(lr, γ₀)`。
前两条让旧实现偏窄，第三条在这批序列上不绑定（`LRV/γ₀ = 5.22–11.43`），
但**推不出「旧实现恒偏窄」**：实测（`data/.scratch/pwin_variance.json` 的
`summary.legacy_gap_*`）8 条序列里 7 条偏窄 **0.7–17.5%**、**1 条反而偏宽
10.1%**（`resid median/60`：`γ_20 = −0.044`，截断核给末阶全权重反而压小 LRV）。
初稿写的「小 20–25%」两处都错——量级（最大 17.5%）与方向（有反向）。而这个数
被 **5 处** docstring 引用成实测值（`pwin.py` 32 / 247、`settle.py` 398 / 660、
`test_decision_excess_hac.py` 146）。

#### 二、实测：**两条序列，不是一条**

增量改善序列 `d`（本节的主角，`_series` 直接算的那条）：

| basis | window | 收窄倍数（旧 Bartlett → 生产截断） | `LRV/γ₀` | `n_eff` |
|---|---|---|---|---|
| median | 60 | 2.270 → **2.285** | 5.22 | 61.9 |
| median | 250 | 2.760 → **3.188** | 10.16 | 31.8 |
| abs | 60 | 2.301 → **2.631** | 6.92 | 46.6 |
| abs | 250 | 2.878 → **3.381** | 11.43 | 28.3 |

verdict 序列（`walk_forward` 的 `n_eff` 所属的那条，逐日 Brier 改善相对常数）：
收窄 **2.567–2.709**，`n_eff` **44–49**（由 `oos_days/n_eff` 反推，产物
`verdict_rows`）。

（323 个可评估日 / 20 日持有期，决策日 345。产物 `data/.scratch/pwin_variance.json`，
两个 `summary` 块分开给。）

历史章节 §9.11 记的「lag1 0.65–0.84、2.2–2.7 倍、44–66」是 Bartlett 口径下的旧读数，
与本次实测（0.66–0.87、2.57–2.71、44–49）不一致；**引用请用本文与产物，§9.11 保留
原样作为当时的记录**——账本不改历史，只在这里标出哪一套是当前口径。

**第二个发现比第一个严重：文档里的数从来没有出处。** 旧 Bartlett 的实测范围是
**2.27–2.88**，而文档写「2.5–3.2」与「2.4–3.2」——两个区间都对不上，任何一份产物里
都复算不出来。`n_eff` 也一样：`44–66` 里的 44 复现得出来（44.0），66 复现不出来
（4 个组合是 44–49）。所以这不是「数错了 20%」，是**这些数从未被任何落盘产物锚定**，
每次重写都靠记忆。修法因此不只是换数，是让数有出处。

#### 二·补、「替换过度」的自纠（本轮第二次犯同一个错）

第一版改法把 `pwin.py:32` 的「321 天 ⇒ 44–66」直接换成了增量序列的「28–62」。
**那是错的**：44–66 说的是 verdict 序列的 `n_eff`，28–62 是增量序列的，两条序列的
自协方差不同，数值不能互相代用。verdict 的实测是 **44–49**——原来的 44 一直是对的，
错的只有上界 66。

这个错误和本节要修的缺陷是**同一个形状**：把一条序列上的读数贴到另一条序列上。
区别只在于，13e/13f 贴错的是「核」，这次贴错的是「序列」。发现方式是加一条
`verdict_rows` 落盘——**只要两条序列各有自己的产物，混用就会在 diff 里现形**。
这也是本轮唯一一条不需要靠「想清楚」就成立的防线。

#### 三、方向：哪一条是定理，哪一条只是经验

复核意见里写「生产走截断核，同 lag 下 LRV 更大，所以那个区间偏低」。**这句只在
MA(H−1) 的 `γ_k ≥ 0 (k < H)` 下成立**，真实序列不满足——`median/60` 的 `lag20 = −0.044`。
截断给末阶全权重、Bartlett 只给 5%，末阶为负时截断反而更小。这就是为什么
`median/60` 只升 0.65%（2.2703 → 2.2851），而 `abs/250` 升 17.5%。

可以当定理用的只有 floor 那一项：`max(lr, γ₀) ≥ max(lr, 0)` 逐样本成立。但在这
4 条序列上 **floor 一律不绑定**（`LRV/γ₀ = 5.22–11.43`，绑定当且仅当 = 1），
它贡献为 0。

所以本轮改数的合法性**不来自方向论证，来自实测**：4 行逐一重算，两次独立运行逐位相同。
把「生产核更大」写成定理会在下一批数据上翻车——**符号不是由代码决定的，就别声称它是**。

顺带纠正一个直觉：改完之后，旧区间「2.5–3.2」既不是被整体调高、也不是调低——verdict
序列（2.57–2.71）**落在旧区间里**，增量序列（2.29–3.38）两端都超出。它错的地方不是
大小，是把两条序列当成了一条，然后凭记忆记了一个中间值。

#### 三·补、第三次自纠：「小 20–25%」自己也没有出处

「三条差异同向叠加 ⇒ 旧实现恒偏窄」是**符号论证**，不是实测——而上一节刚说完
「符号不是由代码决定的，就别声称它是」。把旧实现逐字复刻成一个只用于诊断的
化石函数（`probe_pwin_variance._legacy_se`）后逐条量：

| 序列 | 生产 − 旧实现 | | 序列 | 生产 − 旧实现 |
|---|---|---|---|---|
| d median/60 | +0.7% | | resid median/60 | **−10.1%** |
| d median/250 | +15.5% | | resid median/250 | +5.4% |
| d abs/60 | +14.4% | | resid abs/60 | +16.4% |
| d abs/250 | +17.5% | | resid abs/250 | +17.0% |

7 条偏窄 0.7–17.5%、1 条偏宽 10.1%。所以「小 20–25%」不是「约等于」，是**没有一条
序列对得上**：上界 17.5% 已经越过 20%，方向在 `resid median/60` 上反了。落盘
`summary.legacy_gap_min/max/narrower/wider`，逐行 `d_se_gap_legacy_pct` /
`resid_se_gap_legacy_pct`。

发现路径不是重读，是新加的溯源检查（§9.39）第一次运行就把这一行列进了待核清单——
**它抓到的第一个数，是本轮自己写的第三个无出处的数**。

#### 四、修法

1. **删掉本地实现**，`_hac_stats` 直接调 `settle.hac_lcb`；旧核降为诊断列
   `d_se_infl_bartlett`，用来复算改口径之前的读数（13f 的「非生产读数进列」）。
2. **落盘** `data/.scratch/pwin_variance.json`：`summary`（增量序列的
   `se_infl_min/max`、`neff_min/max`、旧核同名两项）与 `verdict_rows`（verdict 序列的
   `n_eff` / `se_infl`）**分成两块**——混用两个块就是二·补里那个错误。`rows[]` 每行带
   `d_se_infl`、`d_se_infl_bartlett`、`d_lr_over_g0`（= infl²，同时回答 floor 绑没绑）、
   `d_neff`、四项 `acf`。docstring 的数只从这里抄。
3. **5 处 docstring**：讲 `n_eff`/verdict 的两处（`pwin.py` 32 / 928）写 **2.57–2.71
   倍、44–49**；讲通用或 excess 的三处（`settle.py` 398 / 660、测试 docstring）写覆盖
   两条序列的 **2.3–3.4 倍**。顺手修一个同源错误：`pwin.py` 的 `lag1 0.70–0.83`
   实测是 **0.66–0.87**，同样没有出处。

#### 五、护栏（三条，都不跑数据）

- `test_probe_delegates_hac_to_production`：探针**默认路径**与
  `settle.hac_lcb` 逐位相等（`lcb` 与 `infl` 都比），诊断核另比一条。
  只比显式传参抓不到「探针默认核被改回 bartlett」。
- `test_production_default_kernel_is_truncated`：MA(19) 上默认核的 `se` 必须比
  Bartlett 宽 5% 以上。这条堵上一条的盲区——**两边一起改回 Bartlett 时，逐位相等照过**。
- `test_probe_artifact_matches_its_own_summary`：产物自洽（`summary` == `rows` 的
  min/max；`d_lr_over_g0 == infl²`）。产物不在时 skip（CI / 新克隆）。

无牙齿检查（实测，不是推断）：把探针默认核改成 bartlett ⇒ **1 failed**；再把
`settle.hac_lcb` 的默认核改成 bartlett ⇒ **2 failed**；两处改回 ⇒ 7 passed。
`pytest -k decision` **267 → 270**（0 失败 0 错误 0 跳过，junit 复算）。

#### 六、教训

第七处的形状和前六处一样：**从邻支复制一行**。多的这一层是——探针复制的是
**更保守的**那一支，于是它报出来的缺陷比真实的小，读起来还像是「已经量过了」。

所以补一条硬规矩，给下一个写探针的人：**docstring 里的实测数字必须能 index 到一份
落盘产物**（文件 + 键名 + 何时生成）。数不是「记得对」，是「能复算」。这一条比
「不要复制估计量」更可执行——复制与否有争议，「能不能复算」没有。

#### 七、遗留

- `_hac_stats` 的 `NaN` 分支（生产返回 `None`，即 `n < 2`）没有测试覆盖，当前调用点
  `n = 323`。
- `data/.scratch/probe_excl_rule.py:209` 仍写「2.5–3.2」，那份是一次性脚本、不在跟踪
  范围，未改。
- 旧实现与生产 Bartlett 的差异只剩 floor（`1e-18` vs `0`），这 4 条序列上不绑定，
  所以「2.27–2.88」可以当作生产 Bartlett 的复算值。

### 9.39 把「实测」钉到产物上：断言行溯源检查（2026-11-07，13i）

#### 一、为什么散文不够

上一节把教训写成一条硬规矩（「docstring 里的实测数字必须能 index 到产物」），两小时后
它就被自己违反了：本节初稿写「旧实现的 `se` 比生产小 20–25%」——这个数同样没有出处，
实测是 **7/8 条偏窄 0.7–17.5%、1/8 条偏宽 10.1%**。规律很稳定：错的不是记忆，是「记」
这个动作。拦得住它的不是嘱咐，是「拿不出产物就红」。

#### 二、规则（两条，都不跑数据）

**R1 覆盖。** 断言行 = 含 `实测`/`复算`/`落盘`/`测得` 的 docstring 或连续 `#:` 注释块里，
含**读数形态**（小数、`%`/`bp`/`倍`/`×`、整数区间）的那一行。每条必须二选一：锚定到
一份落盘产物，或进封存名单。改数会同时触发 `STALE`（旧行没了）与 `UNREGISTERED`
（新行没出处），两条并排就是一份 diff。

**R2 完整性。** 锚定条目必须（a）该行仍在文件里，（b）`claimed` 值出现在该行文本里
（否则登记表与 docstring 已经不同步），（c）产物存在时逐项比对（偏差超容差即
`VALUE_MISMATCH`），（d）产物欠缺时记 `SKIP`，**不假装通过**。

粒度选**行**不选段落，身份用**归一化文本**：错字不换身份，改数必换身份。段落的唯一
作用是关键词闸门——没有 `实测` 之类的设计说明不该被拉进来。

> **13l 更正：上面这句「错字不换身份」当时是假的。** 实现是 `sha(整行归一化文本)`，
> 加一个「了」就换身份，与改一个数完全同形。身份已改成**读数向量**，见 §9.40。
> 原文保留，因为「散文比代码走得快」正是这一节要记的事。
>
> **13m 更正：上面那套「读数形态」词法只决定**哪一行算断言行**，不决定身份。**
> 13l 把身份也换成了同一套词法，于是裸整数（`323 个可评估日`）改了不换身份——
> 94 行里 60 行有这种哑数字，占全部数字的 41.9%（13n 复算；初稿写 62 行 /
> 43.2%——62 来自变异脚本的前缀重复，43.2% 来自那个从未存在过的 136，见
> §9.41 八）。身份已改成**全部数字**（只剔
> §章节号与 v版本号），见 §9.41。同一个病第三次：**性质写进散文，没写进断言。**

#### 三、今天的状态：94 = 12 锚定 + 82 封存

| 锚定覆盖 | 产物 | 键 |
|---|---|---|
| 两条序列的收窄倍数 / `n_eff` / `lag1` | `data/.scratch/pwin_variance.json` | `summary.se_infl_*`、`verdict_se_infl_*`、`verdict_neff_*`、`rows[].acf.1` |
| 旧实现与生产的差 | 同上 | `summary.legacy_gap_*`、`legacy_narrow_*`、`legacy_wider_abs` |
| 旧核的过度拒绝率 | `data/.scratch/hac_size.json` | `rows[est=bartlett_q,n=…,dgp=ma].size%` |

**82 条封存是债，不是成绩。** 它只保证这些数「不能再悄悄变」，不保证它们对。优先该锚的：
`pwin.py` 的 `0.817` / `1.5 倍`（闭式，缺的是把 `267/400` 的推演落盘）、`settle.py` 的
`21.4%` / `11.1%` / `ICC≈0`、`probe_net_diff` 的 `122.7` / `148.8`。

> **14g 补：总债现在是 `82 + 106 = 188`。** 第二个 106 属于另一个口径
> （`analysis/*.py`，见 §9.50）——它不在这 94 条里，也不是这张表管的数。两个口径
> 各自一份登记表，别把它们并成一份来读。

#### 四、它抓到的第一个数是本轮的第三个数

见 §9.38 三·补。补一句落点：`_legacy_se` 被保留成一个**标注为化石**的诊断函数
（只喂 `*_se_gap_legacy_pct`，不再算任何下界），否则那三个数连「差多少」都复算不了。

#### 五、变异实测（它到底会不会红）

在真实仓库上改登记表，不改代码：

| 变异 | 结果 |
|---|---|
| 把 `summary.verdict_se_infl_min` 的 `claimed` 从 2.57 改成 2.60 | `REGISTRY_DRIFT` + `VALUE_MISMATCH`（2 条） |
| 从 `frozen` 里删掉一条 | `UNREGISTERED`（1 条） |
| 删掉一条锚定条目 | `UNREGISTERED`（1 条） |

还有一次不是设计出来的验证：本文件初稿的两条测试 docstring 里写了「实测 0.42」，第一次
全量运行就被它自己抓了——**扫描没有白名单豁免**，写检查的人也在射程内。

#### 六、边界与遗留

- **抓不到语义混用**：把增量序列的 `28–62` 贴到 verdict 上，数值仍在容差内就不会红。
  那一层只能靠产物分块（`summary` / `verdict_rows`），这也是为什么 §9.38 二·补 值得单独记。
- **抓不到没写「实测」的段落**：库常数与闭式推导（`267/400`、`2/3`）不在射程内。
- **`--rebaseline` 是逃生门**：它挡「悄悄改」，不挡「故意改」。
- **不扫 `docs/*.md`**：这份账本本身有 4000 行读数，扫进来会是另一个量级的工程，且账本
  的写法是「保留历史 + 更正」，与「行文本即身份」冲突。下一轮再议。
- **不进 `decision_gate.py`**：它在 `SEALED` 里（`SEALED` 含 `Path(__file__).resolve()`），
  加子命令就得重签封签。检查走独立脚本 + pytest，进 `-k decision` 一样是门禁。
- `pytest -k decision` **270 → 277**（0 失败 0 错误 0 跳过，junit `13j` 复算）。

### 9.40 溯源检查的第二处自相矛盾：身份是整行文本，正文却写「错字不换身份」（2026-11-07，13l）

#### 一、缺陷：又一次「散文说得比代码多」

§9.39 二 写着「粒度选行不选段落，身份用归一化文本：**错字不换身份**，改数必换身份」。
实现是 `sha(_norm(整行文本))`。§9.40 开工时先在临时仓库上量了一次：

| 变异 | 13i 实现的结果 |
|---|---|
| 原样 | ✓ 绿 |
| `收窄 0.42 倍` → `收窄了 0.42 倍`（加一个「了」） | `STALE` + `UNREGISTERED` |
| `0.42` → `0.39` | `STALE` + `UNREGISTERED` |

两行**完全同形**。也就是说「按行做粒度」只解决了段落级漂移，错字照样要重核，只是把
摩擦从段落推到了行内。而 §9.39 五 的三条「有牙齿」测试只覆盖改数，**没有一条覆盖
改字**——所以那句声称从来没有被任何测试钉住。这是本轮第二次同类错误（第一次是
§9.38 三·补 的「小 20–25%」）：**性质写进散文、没写进断言**，就等于没写。

#### 二、修法：身份 = 读数向量

- `fingerprint(text)` 只哈希 `canonical_readings(text)`——该行读数 token 的有序序列，
  并抹掉排版差（`3 倍` / `3倍`、`44–49` / `44-49`、全半角空格）。
- 改字、补标点、改措辞 ⇒ 向量不变 ⇒ 同身份 ⇒ **不红**；改任一位数 ⇒ 换身份 ⇒
  `STALE`/`STALE_ENTRY` + `UNREGISTERED` 并排。
- `Config(globs, claim_keywords)`：`root` 与 `registry` 本来就注入得进，缺的是**口径**。
  临时仓库现在可以长成 `src/*.py`，不必假装自己是本仓库（`test_custom_globs_...`）。
- 读数词法与结构性百分数**不**做成旋钮：那是规则本身，改规则要改代码并重封。

#### 三、代价要摆在明处：只改字的语义翻转不再红

「7 条**偏窄**」改成「7 条**偏宽**」，数值不动 ⇒ 硬检查绿。这不是疏忽，是身份的必然
代价。所以加 `PROSE_DRIFT` **提示**（不判失败、不影响退出码、不参与 `ok`）：
键命中但文本不同 ⇒ 报相似度与位置，让人看得见。`--rebaseline` 只同步封存项，
锚定项的 `text` 要手工同步。

**别把「绿」读成「语义没变」**：绿只保证「数没动、且数有出处」。方向上的是非，
仍然靠 §9.38 三 那条规矩（方向不是定理）与评审。

#### 四、撞键必须记账：一句锚定、一句封存是合法的

身份只看数，于是「同一文件两行写着同一组数」会撞键——本仓库现在有 **2 组**
（13j 的读数向量口径下是 6 组）。
其中 `probe_pwin_variance.py` 的模块 docstring 与 `_hac_stats` 都写「7 条偏窄
0.7–17.5%、1 条偏宽 10.1%」，一句锚定、一句封存，**是真的两句话说了同一组数**，
不该判「二选一」。所以：

- 计数**按指纹合并**：登记 n 条就得出现 n 次；少一次 `STALE`/`STALE_ENTRY`，
  多一次 `UNREGISTERED`。塌缩成一条会让「删掉其中一行」静默通过。
- 核锚定项用 `_match_text` 挑回**它自己那一行**（先找完全相同的文本，再退到最相似），
  否则会拿隔壁那行过审（读数相同、写法不同时假通过）。
- `--rebaseline` 在这条上翻过车：初版按 key 去重，把同指纹的独立封存行直接丢了
  （82 → 81，同时多出 1 条 `UNREGISTERED`）。改成**按次数抵扣锚定占用的名额**。

#### 五、变异实测（新的三条测试会不会红）

| 变异 | 结果 |
|---|---|
| `fingerprint` 退回整行文本哈希 | 3 条红（粒度 2 条 + `PROSE_DRIFT` 1 条） |
| 同指纹塌缩成一条（`_group` 只留首个） | 2 条红（含真实仓库覆盖测试 94/12/82） |

测试 **7 → 14** 条，`pytest -k decision` **277 → 284**（junit `13l`，0 失败 0 错误
0 跳过）。真实仓库读数不变：**94 = 12 锚定 + 82 封存**，只是 82 条 `sha` 全部按
读数向量重算（登记表 diff = 82 行 `sha` + `_doc`）。

#### 六、边界（与 §9.39 六 并列，不重复的部分）

- **语义混用**仍抓不到（增量序列的 `28–62` 贴到 verdict 上照样过）。
- **只改字的语义翻转**只出 `PROSE_DRIFT`，不判红——见三。
- **`PROSE_DRIFT` 会积累**：改完措辞不 rebaseline，提示会一直挂着。它是提示，
  不是债；但也别让它在输出里长到没人看。
- 仍**不扫 `docs/*.md`**、仍**不进 `decision_gate.py`**（`SEALED` 含
  `Path(__file__).resolve()`，加子命令要重签封签）。
- 遗留未动：`summary.verdict_se_infl_*` 的 2.567/2.709 是由取整后的 `n_eff` 反推，
  真实粒度 ±0.02–0.03——docstring 写三位小数仍是假精度，下一轮让 `walk_forward`
  直接回 `se`/`se_iid`（§9.39 七 第 1 条，未变）。

### 9.41 第三处自相矛盾：身份只认「读数形态」，正文却写「改任何一个数必换」（2026-11-07，13m）

#### 一、这次不是读代码发现的，是数出来的

上一轮（§9.40）结尾写：「改任何一个数 ⇒ 向量变 ⇒ 红」。同一段的实现里
`fingerprint()` 用的是 `readings()`——那是**成员资格**（哪一行算断言行）的词法，
它刻意不收裸整数（`250 天`、`20 日` 是定义）。于是把 94 条断言行里的每一个数字
逐个改末位、再问「身份变没变」，得到：

| 口径 | 改了不换身份 | 占全部数字 | 涉及行 |
|---|---|---|---|
| 13j（读数向量） | 132 / 315 | 41.9% | 60 / 94 |
| 13m（数字向量） | 13 / 315 | 4.1% | 11 / 94 |

132 的构成：**116 个裸整数** + 12 个章节号 + 3 个日期数字 + 1 个版本号。
13 的构成：12 个章节号 + 1 个版本号——**只剩文档坐标**。也就是说，改动前
「`323 个可评估日` 改成 `324`」和「`0.42` 改成 `0.39`」不是一回事，前者全绿。

复算方式：`scan()` 出 94 行 → 对每行每个 `\d+(?:\.\d+)?` 改末位 → 比 `fingerprint`。
不需要跑任何探针，改一次函数就能重算。

> **13n 复核（2026-11-07）：本节初稿的 136 / 62 是错的，上表已改成 132 / 60。**
> 用 `b4322c8` 的完整树 + 它自己的 `scan()` 重跑，七种变异口径无一得到 136；
> 135/62 是变异脚本「前缀重复」的产物。逐 token 账见本节**八**。

#### 二、根因不是「漏了一种数」，是两件事挤进一个正则

`READING` 同时回答两个问题：**这行算不算断言行**、**这行变了没有**。两个问题的
正确答案方向相反：

- **成员资格要窄**：只认小数 / 带量纲 / 区间，否则每行都进名单，名单长到没人读；
- **身份要宽**：行里每个数都该承重，否则改数不红。

为 A 收窄，必然把 B 弄瞎。13m 拆开：`readings()` 只管成员资格（**断言行仍是
94 条，一行不差**），`digits()` 管身份——剔掉**文档坐标**（`§9.39`、`v13c`）后的
**全部**数字，按出现顺序。日期**不豁免**：「2026-11-05 的快照」是对数据新鲜度的
声称，改日期该重核。

豁免项是**白名单**，且由断言钉住：`test_every_digit_in_the_repo_is_identity_bearing`
里有一支 `unexplained` 分支——少掉的数字必须被 §/v 恰好解释掉，多剔一种写法就红。
静默放宽身份正是这一轮要堵的东西，所以这里不许「顺手多剔一点」。

#### 三、同场修掉的三个洞

1. **`_VERSION` 只认带小数点的版本号**：`\bv\d+(?:\.\d+)+` 匹配 `v6.1`，不匹配
   `v13c`，于是 `v13c` 的 `13` 留在身份里，升版本会**假红**。改成
   `\bv\d+(?:\.\d+)*[a-z]?\b`。
2. **`frozen[].sha` 从来没被读过**。归组按 `text` 现算，`sha` 只是写下来摆着：
   手改 `text` 不重签，条目「看起来还在」，检查照样绿。现在 `sha` 与现算不符即
   `REGISTRY_DRIFT`（`test_frozen_sha_must_match_its_own_text`）。
3. **`--rebaseline` 的预留逻辑**。改这一版时它真咬了我一次：第一次跑完 **82 → 81**，
   因为「同一指纹下、一句锚定一句封存」被**重复预留**——精确匹配那条占位之后，
   同一个 entry 的兜底轮又把隔壁那条占了，于是封存行凭空消失，报的是
   `UNREGISTERED`。改成两轮：精确匹配过的不许再占第二行。
   `test_rebaseline_keeps_anchor_and_seal_on_their_own_lines` 专钉这条，包括
   **二次重签幂等**——不幂等就是名单在两态之间摆。

#### 四、代价：重签 82 条，条数不变

| 项 | 数值 |
|---|---|
| 断言行 | **94**（`scan` 集合与改动前逐行相同） |
| 锚定 / 封存 | **12 / 82**（不变） |
| 登记表 diff | **67 行** = 66 条 `sha` 重签 + `_doc`；16 条不动（它们的读数向量本来就等于数字向量） |
| 撞键组 | **6 → 2**（老的 4 组是收窄词法造的假撞键：两行其实只有「3 倍」相同，其余整数不同） |
| 剩余 2 组 | `probe_neutral.py` 的 +185bp 两句；`probe_pwin_variance.py` 一锚一封那对（§9.40 四） |
| `--rebaseline` 二次运行 | md5 不变（幂等） |

#### 五、验证

| 项 | 结果 |
|---|---|
| 测试 | 14 → **20** 条（+6） |
| `pytest -k decision` | 284 → **290 passed**（junit `13m`，0 失败 0 错误 0 跳过） |
| mtime | junit 13:13:52 **晚于**三个被测文件（13:11:10 / 13:12:18 / 13:12:30） |
| 门禁 | `verify-seal` ✓ / `audit` ✓（6 产物 326 决策日，确认段 0 天）/ `verify-log` ✓（0 行）/ checker ✓（94 = 12 + 82，0 失败） |

变异（每一次都临时改坏、跑完还原）：

| 变异 | 杀红 |
|---|---|
| `digits()` 退回 `readings()`（13j 口径） | **7** 条（含真仓库覆盖 94/12/82） |
| `fingerprint` 只哈希第一个数字 | **5** 条 |
| `identity_text` 顺手把日期也剔了 | **4** 条（含 `unexplained` 分支） |
| `rebaseline` 重复预留锚定行 | **1** 条（专钉那条） |

#### 六、边界（更新，与 §9.39 六 / §9.40 六 并列）

- 仍**抓不到语义混用**：同文件内两行数字序列相同即可互换（`probe_neutral.py`
  那对 +185bp 就在射程内）。
- 仍**抓不到只改字的方向翻转**：只出 `PROSE_DRIFT`，不判红。
- 仍**不扫 `docs/*.md`**、仍**不进 `decision_gate.py`**（`SEALED`）。
- 新增代价：**日期承重**。数据快照日期变了要重签（以前静默通过）。这是故意的——
  日期是对「哪天的数据」的声称——但换数据的日子会比以前多一次 rebaseline。
- 新增代价：改动**结构性整数**（`250 天`、`20 日持有期`）也要重签。以前这类改动
  静默。对的定义改动本来就该过一次评审，所以记在账上、不设豁免。

#### 七、下一步（按值排序，均未动）

1. `walk_forward` 直接回 `se`/`se_iid`，把 `2.567/2.709` 从「由取整 `n_eff` 反推」
   变成「测量」，并把锚定容差放到真实粒度（±0.02–0.03）。（§9.39 七 第 1 条，未变）
2. 锚定优先级：`pwin.py` 的 `0.817`/`1.5 倍`、`settle.py` 的 `21.4%`/`11.1%`、
   `probe_net_diff` 的 `122.7`/`148.8`。
3. K3（`ledger.load_cards` 静默跳过损坏卡），`--basis` 记录次之。

#### 八、13n 复核：136 没有出处，135 是脚本 bug

本节初稿写的是 **136 / 315（43.2%）、62 / 94**。13n 用 `b4322c8` 的**完整树**
（`git archive` 导出，不是工作区）+ 它自己的 `check_number_provenance.py` 重跑，
得到的是 **132 / 315（41.9%）、60 / 94**。差额不是「有 4 个 token 没找到」，是
136 从来没有被任何口径产生过：

| 变异口径 | 盲数字 | 备注 |
|---|---|---|
| 正确替换 token（`text[:start] + bump + text[end:]`） | **132** | 与当前产物一致；60/94 行 |
| 前缀重复（`text[:end-1] + bump + text[end:]`） | 135 | 多出的数字没有小数点，被 `readings()` 吞掉；62/94 行 |
| 全域替换（`str.replace`，行内所有同形子串） | 110 | 顺手改坏行内别的数字：22 个翻红、0 个翻盲；53/94 行 |
| 首个替换（`str.replace(..., 1)`） | 125 | 可能改到更早的同形子串：10 红 3 盲；59/94 行 |
| 首位数 / 不取模 / 数值 +1 | 132 / 132 / 132 | 盲集合与「正确替换」**逐 token 相同**；60/94 行 |

七行现在都有脚本：`scripts/verify_identity_mutation.py` 用 `git archive` 只导出
`b4322c8` 的扫描范围，加载**那个提交自带**的 `scan()` / `fingerprint()`，一次跑完
七个口径，与上表逐位比对后落 `data/.scratch/identity_mutation.json`
（`matches_ledger` 字段），不一致即非零退出。七个 `mode` 的机械定义写在脚本
docstring 里，读者能照着重实现。复现器此前放在 `data/.scratch/`，而 `.gitignore`
的 `data/**` 把它挡在版本控制外——**账本引用的脚本，克隆仓库的人根本拿不到**；
这次移进 `scripts/`，`data/.scratch/` 只留可再生的产物。

复算给出比「没有一个是 136」更强的结论：**首位数 / 不取模 / 数值 +1 三者的盲
集合与「正确替换」逐 token 相同**——132 不依赖 bump 怎么写，它就是 `readings()`
看不见的那批坐标（116 裸整数 + 12 章节号 + 3 日期分段 + 1 版本号）。而 110 / 125
不是「更宽松的 132」，是两种**改错位置**：全域替换改的是所有同形子串，连别的数字
一起改坏，于是 22 个本来不承重的 token 翻红；首个替换只改第一个同形子串，可能根本
没碰到被考察的那个 token，于是 10 红 3 盲。三个「怎么 bump」的口径同一个答案、
两个「怎么定位」的口径两个方向偏移——**132 这个量级只有第一种改法能测**。

135 的机制值得单独记：`0.50` 被改成 `0.50.51` 后，`READING` 只认 `\d+\.\d+`，
多出来的 `51` 不算读数，于是整行被误判成「改了不换身份」。这类残渣共 **4 个
token（净 +3：另有 1 个日期 token 被弄坏）**，其中 2 行原本不盲，所以行数
60→62。**「改成本身」和「改出残渣」都会污染计数**——这是同一类错，只是后者
更隐蔽。136 最可能是把 135 记成 136，再倒推「120 个裸整数」（136−16）；表里的
62 也是 bug 版的 62。逐 token 账（含每个口径相对 correct 的差异集
`to_red_rows` / `to_blind_rows`）在 `data/.scratch/identity_mutation.json`，
复现器 `scripts/verify_identity_mutation.py`。

结论：**94 行 / 315 个数字 = 294 承重 + 13 文档坐标 + 8 行首列表编号**
（13q 把列表编号剔出身份之前是 302 + 13；315 这个分母没变），其中旧身份有
**132 个哑数字、涉及 60 行**。原文的 136/62 保留在此作为「散文比代码走得快」
的第四次记录，但不再作为读数引用。

#### 九、13n：符号并进身份（`+`/`−`/`±` 是数字的一部分）

**缺陷**：`digits()` 只取值、不取符号。`实测验证段 −4.19%` 把 `−` 改成 `+`，
`fingerprint` 不变，`check()` 返回 `ok=True`，只留一条 `PROSE_DRIFT`。符号翻转
在这仓库不是排版问题：§9 半部历史都是符号翻车，连「符号翻转」四个字都写在一条
断言行里。

**修法**：`digits()` 对每个数字看前一位；前一位是 `-`/`+`/`±` 且**再前一位不是
数字**时，把符号并进 token。两条排除同时挡住日期连字符（`2026-11-05`）、数字
区间（`44-49`）、markdown 项目符号（`- 250`，符号与数字之间有空格）。`−`/`－`
/`﹣` 与 ASCII `-` 归一，`±` 保持独立。`1e-05` 的指数符号算身份（前一位是 `e`，
不是数字），所以 `1e-05` 改成 `1e+05` 会红。

**代价**：14 行 token 向量变化，其中 13 行在 `frozen`（重签 13 个 `sha`；`entries`
按 text 现算，不用动），1 行是锚定（`pwin.py:248` 的 `horizon-1`）。重签后
`check()` 仍是 94 = 12 + 82、0 失败。已知缺口：`1 - 4p` / `1-4p` 这类**二元**
减号不被当成符号（前者隔空格、后者前一位是数字），它们是公式不是读数；要堵
得引入连接符编码，会把 94 行全部换身份，代价大于收益，留给评审。

**验证**：`test_sign_is_part_of_identity`（翻转必换身份；日期 / 区间 / 项目符号
必不换；减号变体等价）+ `test_sign_flip_in_a_real_file_turns_red`（端到端
`STALE`+`UNREGISTERED`）。

#### 十、13p：`SKIP` 不能算通过——锚定层在克隆上从未生效

同一个 `data/**` 挡了两次。13o 修的是**脚本**（复现器搬进 `scripts/`），这一节
是**产物**：`entries` 的 12 条锚定、36 项 `checks` 全部指向
`data/.scratch/pwin_variance.json`（4 571 B）与 `data/.scratch/hac_size.json`
（15 117 B），而 `git ls-files data` 是 **0 个文件**。

于是干净树上（只放 63 个受控 `.py` + 登记表，没有 `data/`）发生过一次这样的
判定：

```
断言行 94：已锚定 12，已封存 82，失败 0，跳过 36，文字漂移 0
判定：✓ 每个读数都有出处或已封存          ← 退出码 0
```

`已锚定 12` 是**登记条数**，不是核对过的数；`跳过 36` 只占一行字，不进 `ok`。
也就是说：整套溯源里唯一的「数会随数据腐烂、每次重跑重算」那一半，在别人的
机器上**等于零**，而检查器给出的是一句无条件的 ✓。`verify_seal` 早就把
`rel 不存在` 当漂移；同一份脚本，两条口径，必须对齐。

**改法**（三件，互不重叠）：

1. `Report` 拆出 `checks` / `verified`：`checks` 是走到「与产物比对」的项数，
   `verified = checks − len(skipped)`。`anchored` 保留原义（登记条数），但报告
   首行改成 `已锚定 12（登记）…｜逐项核对 36/36`，克隆上是 `0/36（36 项产物
   不在）`——两个数并排，`12` 再也冒充不了「核对过」。
2. `main()` 退出码分三档：`0` 全绿 / `1` 有失败 / `2` **没有失败但有锚定项
   无法核对**。`2` 不与 `1` 混用：「没验」和「验出问题」是两件事。要在这类
   环境里跑通，必须显式 `--allow-skip`，且判定行降级为 `△`。`Report.ok` 不变
   （`check()` 仍可被别的检查组合），只是命令行不假装。
3. `.gitignore` 只放行这两个 JSON（`!data/.scratch/` + `data/.scratch/*` +
   两条按文件名的否定），`data/` 其余内容仍是运行时数据、仍不入库。放行后
   克隆拿到 19 688 B 的**证据**，36 项核对真的会跑。

**为什么是「提交证据」而不是「克隆后重跑探针」**：两个产物由
`probe_pwin_variance.py` / `probe_hac_size.py` 从 `backend/data/*.parquet` 算出，
而那些 parquet 同样不入库、且需要外部行情源。克隆方**物理上无法重算**，所以
证据只能随仓库走——这与「`data/` 是用户运行时数据」不冲突：`.scratch` 里的这
两个文件不是数据，是这个仓库的**结论快照**。

**验证**（现跑）：真实仓库 `逐项核对 36/36`、`0` 失败、退出码 `0`；同一棵干净树
`0/36`、退出码 `2`、判定行给出 `--allow-skip` 提示；加 `--allow-skip` 回 `0` 且
判定为 `△`。`test_decision_number_provenance.py` **25 passed**（新增
`test_missing_artifact_blocks_the_gate`、`test_evidence_artifacts_are_shippable`，
后者用 `git check-ignore -q` 钉住「两个证据文件不许再被忽略」——否则这条债会
原样复发）。身份、登记表、封签**一字未动**：`94 = 12 + 82`、0 失败、13 个
`sha` 无需重签。

#### 十一、13q：两处「数字不是数」，以及射程的分母

**一、行首列表编号进了身份。** `1. ` / `3) ` / `（2）` 是排版，13p 之前它们是
`digits()` 的一部分。后果不只是「吵」：给一个 docstring 列表在**中间**插一条，
后面每一条都换身份，**8 行同时 `STALE` + `UNREGISTERED`**。人会直接
`--rebaseline`，于是同一批里真正变了的读数也被一起重签——**假红把真红洗掉了**，
这比假红本身贵。修法：`_LIST_MARK` 在 `identity_text()` 里剥掉，`(?![0-9])`
保证 `2.57–2.71` 不被吃成 `57–2.71`。8 个 `sha` 重签；`94 = 12 + 82` 不变；
94 行里的承重 token **302 → 294**（`315` 这个 token 分母没动：8 个编号仍在
`DIGITS` 里，只是不再承重）。

**二、`claimed` 是子串匹配，不是数字匹配。** 旧实现 `f"{claimed:g}" in text`，
两个方向都错，而错的方向不一样：

- **假绿，活的**：`claimed=1` 在 `0.7–17.5%` 上命中。把「1 条偏宽」改成
  「2 条偏宽」、登记表仍写 1，R2(b) 照样过。`entries` 里 `legacy_gap_wider=1`
  今天就走这条路——单个数字的声称值几乎无法被这条检查证伪。
- **假红，休眠**：`claimed=-4.19` 拼出 ASCII `-4.19`，正文是 U+2212 `−4.19`，
  同一个数判成漂移。休眠的原因是 `entries` 里负声称值 **0 条**，不是它对了。

改成按 `digits()` 切 token 比 `(符号, 数值)`：缺符号 = 正，`−`/`－`/`﹣` 已折叠，
`±` 不冒充单值。它顺带把 `1` 与 `17.5` 分开——这是子串匹配永远做不到的。

**三、射程有了分母。** `numbered_lines()` 数 claim 段落里**带身份**的行 =
**284**；`scan()` 的 94 是它的读数形态子集，差 **190**。报告第二行：

```
扫描口径：claim 段落带数字行 284｜其中读数形态 94｜未纳入 190（整数/公式/日期，按定义不是读数）
```

理由和 13p 是同一条：**不给分母的 ✓ 就是 SKIP**。`已锚定 12` 曾在 36 项全部
跳过时照旧打印；`断言行 94` 也会在 284 条带数字的行里只覆盖三分之一时照旧打印。
两者都真，但都不是读者会默认的那个意思。

顺带钉一个统计单位：claim 段落里**带 `\d` 的行**也正好是 **315 行**，和 94 行里
的 **315 个 token** 是两个不同的东西。这轮差点又混用一次，写在这里免得下次踩。

**四、为什么没做「`[旋钮]` 标记 + 298 行重登记」。** 三个实测：

1. **298 这个数本身不对**。claim 段落带 `\d` 的行 315，其中 17 行只含 `§`/`v`
   坐标，298 行带身份；再剔掉 14 行只有列表编号的，要登记的是 **284**。298 是
   「没剥编号」的口径，+204 也跟着变成 +190。
2. **豁免会变成常态**。190 条新增里，读数形态（小数 / 非结构量纲 / 区间）的有
   **0 条**——它们全是整数、公式、日期、`95%` 这类定义（把 `§`/`v` 坐标剥掉
   再问 `readings()`，仍是 0 条）。要标 `[旋钮]` 的就是全部 **190 条**，占 284
   的三分之二。三分之二的行都带豁免标记，等于没有标记：评审无法区分「标对了」
   和「标错了」，而误标的方向永远是**假绿**。
3. **行内标记是永久的、看不见的**。`--rebaseline` 至少把增删打出来、进 diff；
   `[旋钮]` 一旦写上，这行就**永远**不在射程里，以后改它不红、也不留痕。两个
   逃生门并存时，**弱的那个赢**。

所以本轮只修仪器，不动登记范围。要不要把 284 全收，是另一个决定。

**（14:40 复核：初稿这句写「`frozen` 82 → 272，其中约 177 条是定义/公式」，
177 撤回——它和 272 不在同一个单位上，凑不满。** 272 能复算：284 − 12 锚定
= 82 封存 + 190 新增。190 条里剥掉 `§`/`v` 后仍有读数形态的是 **0** 条，所以
「非读数」就是 190，不是 177；140 是这 190 条折成的身份组数，也不是 177。
`82 + 177 = 259`，和同一句里的 272 差 13。**要么报条数，要么报组数，别混用。**）

**真正的代价不在条数，在撞键。** 登记表按（文件, 身份）计数，同身份的行合并成
一组，「登记几条就得出现几次」。两种口径实测：

| 口径 | 行数 | 身份组 | 撞键组 | 涉及行 | 最大一组 |
|---|---|---|---|---|---|
| 今天的 94 条断言行 | 94 | 92 | 2 | 4 | 2 |
| 全收的 284 条带身份行 | 284 | **231** | **34** | **87** | **7** |

190 条里 31 个是撞键组、涉及 81 行，最大一组 **7 行全在 `pwin.py`，身份就是
单个 `0`**（`冷启动（n = 0）`、`lcb > 0 -> RELIABLE`、`lcb <= 0 -> UNRELIABLE`…）。
后果正是本节一开头花 8 次重签消掉的那个病：**改其中一行，同组另外 6 行一起被
报 `STALE`**。假红把真红洗掉，规模从 8 行变成 87 行。所以全收不是「多签 190 次」
的代价，是把信噪比倒过来——`0` / `1` / `20` 这类整数在散文里几乎无法被身份区分。

结论：能接受的解法仍然只有「全收 + `--rebaseline` 当唯一逃生门」，但按上表它
换来的不是「任何数字变了都留痕」，而是「任何 `0`/`1` 变了，七行一起喊」。

**五、验证**（现跑）：本文件 **28 passed**（25 → 28）；`-k decision`
**299/0/0/0**（`junit_13q`，70.3s）；四道门全绿——封签未漂移 / 6 产物 326 决策日、
末日 2026-10-08 / read-log 0 行 / 溯源 `94 = 12 + 82`、`逐项核对 36/36`、
`扫描口径 … 284｜94｜190`；变异复现器 **1 passed**（`132/315` 逐位不变：列表
编号原本就在身份里，不在哑集合里，所以这一修不碰 §9.41 八 的那张表）。

**六、同一个数的两种单位（21 / 18），别当成矛盾。** 本轮累计重签 `sha`
**21 条**（13n 的 13 + 13q 的 8，按 `frozen` 下标逐条比），而 `git diff` 上
只数出 **18 个旧值消失**。差额来自同身份：82 条 `frozen` 只有 77 个不同的
`sha`（5 对），被改的 21 行落在 **19** 个不同的旧值上——`pwin.py:104` 与
`test_decision_pwin.py:548` 一对、`probe_neutral.py:1` 与 `:7` 一对，各自
同时改；而 `probe_neutral.py:49` 那个旧值还留在另一条**没改**的行上。19 − 1
= 18。两个数都对，差的是「逐条」与「去重」——和「315 行 vs 315 token」
（§9.41 十一 三）是同一类坑：**报一个数的时候，连单位一起报。**

### 9.42 报告比产物多走了半步：三处口径错、一处不可复现、一处被低估（2026-11-08，13r/13t）

#### 一、这一轮的对象是「对报告的复核」，不是报告

一份独立复核把 A 组（宽度前沿 / 流动性身份）逐位核到了产物上，结论是
「数基本全对，但两处没保存；门禁全绿但和这次改动正交；真正会推翻结论的是
生存偏差与功效」。逐条复核这份复核，结果分四类：

| 复核的断言 | 产物/代码 | 判定 |
|---|---|---|
| 12 格 max t=2.15、零分布 95% 分位 2.54、族系 p=0.160 | `max_t_momentum_only` | ✓ 逐位 |
| 2025-01 起 710.6→−269.5、367.1→+436.1 | `since_2025_01.mat[0]` / `[4]` | ✓ |
| 2024-01 起 1020.2→−185.8、315.4→+448.0 | `since_2024_01` | ✓ |
| 倾斜 0.210/0.417/0.231/0.100 与 0.695/0.498/0.100/0.247 | `tilt` | ✓ |
| Jaccard 0.289、落入率 44.7% | `overlap` | ✓ |
| N=500 毛 +239.6 / 净 +201.6 / t=4.43 / 容量 16.4 亿 | `turnover_H60.rows[5]` | ✓ |
| N*=217、同层 +4.2 / −33.8 / t=0.32 | `breakeven_n=216.5` | ✓ |
| 日度胜率 0.7294 | 新增 `win_daily` | ✓ 值对，原来**没有字段** |
| 不重叠 60 日胜率 67.5%（n=40）、最差块 −418.5 | 新增 `stride_win` | ✓ 值对，原来**没有字段** |
| 双侧 1/99 缩尾 222.2bp、t=4.15 | 新增字段给 227.1 / 4.14 | ✗ 不可复现（见四） |
| 换手率斜率 −502/−510/−508/−386/−231、2024 起 S4 转正 +95 | 产物字段与三种口径都算不出 | ✗ 不可复现（见四） |

**「两处没保存」实际是五处脚本、六份产物。** `git status` 上是
`probe_decile_net` / `probe_width_frontier` / `probe_liquidity_identity` /
`probe_universe_gap` / `probe_hump_attribution` 五个 `??`，被 `.gitignore`
挡住的是 `decile_net` / `width_frontier` / `liquidity_identity` /
`universe_gap` / `survivorship_bound` 五份 JSON 加一份 CSV。复核只点了三个脚本、
两份 JSON——**它自己也少数了**。这不改变结论，但它说明同一类错误不是谁的个人
毛病，而是「用眼睛数版本控制状态」这个动作本身不可靠：应该数 `git status` 的输出。

#### 二、`−246.1` 的真身：不是算错，是三个量挤在一句话里

复核说「`−246.1` 是日度池中位的中位数，不是均值；均值是 `−189.1`；只有
`177.8 − (−189.1) = 366.9` 与 `pool_mean_minus_median_bp` 闭合」。**这一条完全正确**，
而且可以不用重算就证明：若 `pool_med` 真是 −246.1，则
`pool_mu = 366.9 + (−246.1) = 120.8`，与同一句里的 `177.8` 自相矛盾。
产物没错，错在注释把三个统计量写成了一个：

    pool_mu               逐日截面均值 → 对日期取均值        +177.8
    pool_med              逐日截面中位 → 对日期取均值        −189.1   ← 恒等式用它
    pool_med_of_daily_med 逐日截面中位 → 对日期取中位        −246.1   ← 注释引的是它

修法不是改数，是**让三个数各自成为字段**（`pool_mu_bp` / `pool_med_bp` /
`pool_med_of_daily_med_bp`），并加一条测试钉住恒等式在产物内闭合、
后两个字段必须不相等。只把 `−246.1` 改成 `−189.1` 是治标：下次换个人还会再混一次。

复核把「均值」这个词用反了（它说「均值是 −189.1」，而 −189.1 是**中位的时序均值**），
但方向是对的，且它给的闭合关系是对的。**对的结论配错的措辞，仍然要改正措辞**，
否则下一个人会照措辞去别处找 −189.1。

#### 三、`w99` 是单边上尾，`since` 缺终点

- `layer_w99 = clip(upper_bound=q99)`，只削右尾，控制台却印成「双侧缩尾」。
  新增 `layer_w01w99`（层与池各自按 1%/99% 双侧）后：227.4 → 227.1bp，t 4.15 → 4.14。
  **数值几乎不动，所以数值异常永远抓不到这个标签错**——只能靠字段名和定义块。
- `since` 只存起点，于是「2025-01 起」被读成近两年。实际 **383 个决策日、末日
  2026-08-03**，是 1.5 年。现在 `since` / `until` / `days` 三个字段一起落盘，
  测试钉「全样本天数必须多于子样本」。

#### 四、复核的两个「新增数」复现不出来，但它们要杀的断言确实死了

`222.2bp / t=4.15`：我按「层内与池各自 1/99」得 227.1，按「对日度溢价序列截断」
得 234.6，按「用池的 1/99 共同阈值同时截两边」得 217.4。**222.2 落在区间里，
但不等于任何一个可命名的口径**；最接近的是 0.5%/99.5% 共同阈值（222.5），
而复核说的是 1%/99%。同一族口径跨 217–227bp、t 跨 4.14–4.17 ⇒
**结论对口径不敏感，这是可以写进产物的结论；但「222.2」这个数不能进产物。**

斜率 `−502/−510/−508/−386/−231` 与「2024 起 S4 转正 +95」：三种口径都算不出。
按五分位序号：`−249/−201/−154/−78/−34`；按十分位再折回五分位：`−350…`；
按 log(成交额)：`−368/−304/−239/−132/−66`；2024 起 S4 的对应值是
`+32.8`（五分位）与 `+45`（log）。复核的 **方向**（每一档都负、大盘端最平）
在每一种口径下都成立，**形状**（中间档最陡）与**量级**都不成立。

结论：复核要杀的那句话——「换手率杀伤只在小盘端」——确实该杀。产物里现在有
`slope_per_quintile_bp`，这句话以后是可证伪的；而复核给的替代数字本身没有出处，
进了下一份报告就会变成第二个 `−60bp`。

#### 五、生存偏差：不是「修不了」，是「可以定价，但不能补齐」

`universe_gap.json` 早就记了：宇宙 5890 只、dump 5577 只、缺口 313 只，其中
名字带「退」**155 只**、ST 族 100 只、其他 58 只；缺口里 **312/313 本地无任何
价格副本**。复核说「一只退市股都没有、数据本身修不了」——前半句对（dump 里
5571/5577 的最后交易日就是末日，`n_ended_before_max=6`），后半句要分两半：
**补齐修不了（没有价格），定价可以（用真实面板做注入）。**

`probe_survivorship_bound.py` 从真实面板抽 K 只存活标的，随机时点截断，末段 60 日
线性崩到 −L，补回面板重算层溢价。基线 235.4bp，层内中位 862 只。13s 把抽样改成
**确定性 + 配对**（同一次抽签在全部崩盘档复用，`SEED` 真的钉住标的），20 次重复：

| 模式 | K | 末日崩 | Δ 层溢价（均值，p05…p95） |
|---|---|---|---|
| random（保留自身成交额） | 155 | 0% | +0.10（−1.3…+1.8） |
| random | 155 | 100% | −0.75（−3.2…+1.3） |
| forced（成交额×0.05，入层率 93.0%） | 155 | 0% | **−12.27（−16.3…−7.9）** |
| forced | 155 | 40% | −16.78（−20.9…−12.5） |
| forced | 155 | 100% | **−25.62（−30.2…−21.3）** |
| forced | 313 | 100% | −49.86（−52.3…−46.3） |
| **truncate_only（`clip=False`，只截断）** | 155 | 0% | **−9.45（−13.3…−4.9）** |
| truncate_only | 313 | 0% | −17.53（−19.7…−14.5） |

最后两行是 13t 补的配对档：与同 K 的 `forced, crash=0` **共用同一批 chosen**，
只把削平关掉。所以两行之差就是「削平」自己的贡献，不含抽样噪声。

六个读数（13t 把原来的第 1 条拆成 1 与 1b，并补了最后一条）：

1. **−12.27 不是稀释，是「截断 −9.45 + 削平 −2.81」。** crash=0 时斜坡恒为 0，
   但 `np.minimum(v, 0)` 仍把末段 60 行的正收益削平。13t 补了配对档才量出来：
   同一批 chosen、只关掉 clip，K=155 从 −12.27 回到 **−9.45**（削平 −2.81），
   K=313 从 −23.36 回到 **−17.53**（削平 −5.83）。`−9.45 + (−2.81) = −12.26`，
   与 −12.27 逐位闭合。13s 的 docstring 承诺过 `truncate_only` 这一档，代码里却
   没有（`clip=False` 是零调用点的死分支），于是含削平的数被当成稀释读了整整
   一轮。**这是本轮推翻 13s 的第一个数。**
1b. **「41.1% × 全在场锚」这条分解不成立，它对上只是巧合。** K=155 的三个锚：

   | 字段 | 值 | 是什么 |
   |---|---|---|
   | `displacement_identity_bp` | **−28.11** | 按实现 20% 分位推导的解析式 |
   | `all_present_pool_mean_bp` | −29.11 | 全在场·池均值，构造面板后**重算** |
   | `identity_bp` | −35.88 | 一阶参照（层线性增长 + 原成员全留） |

   0.411 × 28.11 = 11.55、0.411 × 29.11 = 11.97，而纯截断实测是 **9.45**：折算
   高估 **2.1bp**。它看起来对上 −12.27，是因为削平又把数压低了 2.81bp——两个
   误差方向相反、恰好相抵。**平均在场率不能代替逐日权重**：在场只在部分决策日
   成立，而层大小固定 20%，Δ 对当日在场数的响应不是线性的。要闭合只能按日算。
1c. **`identity_bp` 不是闭式锚，它算的是另一个实验。** 与 `all_present_pool_mean_bp`
   差 **6.77bp**（K=155），因为实现是 20% 分位——注入 155 只后层从 862 只变
   893 只（不是 1017），原层还有 124 只被挤出，而它们恰是层内成交额最高的一档
   （收益较低，剔掉会抬升剩余层均值）。它假设层线性长到 N+K 且原成员全留，
   所以系统性偏负。产物里三个数并存，`definitions` 已按这个口径重写。
2. **崩盘项只有 −13.4bp。** 配对之后（同一抽签，只换 crash），
   −25.62 − (−12.27) = **−13.35bp**，这才是「退市信息」的价格。上一版三个档位
   各自独立重抽，−25.9 与 −11.9 之差里混着 ±2.5bp 的抽签噪声（见下方「种子」一节）。
3. **`random` 模式恒等于 0。** 155 只分散到全池，按无条件比例进低换手层的只有
   那一份，层均值几乎不动。上一版记成 −1.2 / −2.5bp，比现在的 +0.1 / −0.8 大
   一个量级。**括号的形状是 forced ≫ random，不是两条平行的带。**
4. **「层比池高 57bp」是量纲混用。** 层**均值** 413.20、池**均值** 177.79，
   差 **235.41** = `layer_bp`（层溢价）。57.61 = 235.41 − 177.79，是把「溢价」
   当成「层均值」再去减池均值——和 §9.42 二 的 `−246.1` 同一类：**三个量挤进
   一句话，恒等式闭合所以没人发现。** 稀释的分母是 235.41；用 57.6 会把机制
   解释低估四倍。

**诚实的边界有两条，第二条是本轮新发现的硬边界。**

- 注入保留 donor 的 mom 与成交额，「退市前长期阴跌 + 停牌断点」没建模；155 只也很
  可能不是 2016–2026 退市的全集（若真实全集 300+，上界往 `K=313` 那一行移）。
- **产品口径（层内 mom 前 100）上的偏差，本地不可识别。** 同一注入换成篮子
  （基线 +86.99）：crash=0 → −37.9bp；crash=100% → −58.7bp。但这个数**完全由
  「退市股的 mom 长什么样」决定**：把注入标的的 mom 设成 donor 原值 / 0 / +0.30 /
  −0.30，同一格的 Δ 是 **−42.0 / +6.8 / −174.9 / +14.5**——**符号都不定**。
  机制是排名：低换手层里塞进 155 只 mom 属于全池分布的票，会挤掉层内真正高 mom
  的那 100 只，挤掉多少取决于一个本地没有的数。所以「层溢价上的 −12bp 可以搬到
  篮子 +87bp 上」不是量级错，是**可识别性错**：能搬的是括号，不是数。

**上一版为什么不可复算：`SEED` 是装饰品。** `inject()` 里
`syms = base["thscode"].unique().to_numpy()`，而 polars 的 `unique()` 默认
`maintain_order=False`——**每次调用顺序都不同**。把它喂给 `rng.choice`，
`default_rng(0)` 只钉住了随机数序列，没钉住「从谁里面抽」。实测：同一进程、
同一个 `SEED = 0`、同一格（forced / K=155 / 崩 0%），两次 3 次重复的均值是
**−9.24 与 −12.92bp**。产物里那句 `"seed": 0` 因此不承诺任何复现。

这条把「四个数对不上」从抄写事故改判成**口径事故**：上一版报的 −11.9 / −25.9 /
−68.3 / −1.2 与产物里的 −11.60 / −24.12 / −64.57 / −0.76，**全部落在对方那一次
运行的散布里**（旧产物 forced K=155 崩 0% 的三次重复区间 −14.57…−9.10，崩 100%
是 −25.99…−22.82）。说它「抄了最小值」是个讲得通的故事，但**换一次抽样就能复现
出同样的偏差**，所以那不是根因。修法也不是多跑几次：先 `np.sort` 再抽，再让
崩盘档共用同一签。`test_survivorship_draw_is_deterministic` 钉住前者。

#### 六、溯源的结构性缺口：两个洞必须同时补，单独补任何一个都抓不到本案

复核说「避开四个词，任何数字都能进 docstring」，并建议「把四个词写进新探针」。
量了一下，**这个建议对本案无效**：

| 洞 | 单独补的后果 |
|---|---|
| 词法：裸整数 / 带符号整数不算读数 | `+393/+327/−79/−71` 写成「实测」后仍是 **0 个读数**，该行根本不进名单 |
| 触发：不写 `实测/复算/落盘/测得` 就不扫 | 96 行的名单一条不增 |
| 两者都补 | 本案才进名单；代价是把「带符号整数」加进词法只多 **2 行 / 3 token** |
| 改用「任何带 bp/% 的行都算断言」 | **+132 行（94 → 226，2.4 倍）**，不可接受 |

所以正确的下一步不是「写四个词」，而是「词法加带符号整数 + 探针惯例要求写词」，
两件一起做、一起重签。本轮**只做后者的一半**：把散文里的数换成字段引用
（`+287.7bp` → 指 `turnover_H60.layer_bp`），并让新产物自带 `definitions` 块。
词法不动——它是规则，改规则要重算 94/315/294/284 这套计数并重签，那是独立一轮。

顺带一条本轮的实证：新测试文件里写了句「只报一个『−60bp』」，**60 秒内被自己
的检查器判红**（UNREGISTERED）。机制是活的，缺的是覆盖，不是能力。

13t 又撞了一次，形态不同：在探针 docstring 里写「这一条才是这条链上可独立**复算**
的算术」，整个 module docstring 立刻进射程——`−60bp`、`250 只`、`50%`、
`20% 分位` 等 11 行**引述**全被判未登记，名单从 94 涨到 105。改成不声称「复算」
的措辞后回到 94。**触发词是段落级开关：写它等于替整段的每个数背书，包括引述。**

同一轮还发现 `STRUCTURAL_PCT` 不含 20——测试 docstring 里一句「实现是 20% 分位」
就会被当成读数。这类定义型百分数该不该进白名单是独立一轮的事（改白名单要重算
94/315/294/284），本轮只换措辞。

#### 七、封签射程：产物进、脚本不进，两个清单不互相顶替

复核把这条读成「`survivorship_bound.json` 不在 `decision_gate.ARTIFACTS`，所以
audit 看不见它」。前半句对，后半句对错了门：`ARTIFACTS` 的契约是**扫逐日键、
确认段决策日不许出现**，而这份 JSON 只有标量与汇总表，一个 `YYYY-MM-DD` 键都
没有，塞进去 `audit` 只会多报一行「0 天」——把「清单已闭合」变成一句看着像
检查、其实什么都没扫的话。

真正缺的是**封签**：`SEALED` 回答的是「这些字节有没有被换掉」。所以本轮把产物
并进 `SEALED`（14 → 15 文件：8 个脚本 + 7 份产物），**不进 `ARTIFACTS`**。
`test_survivorship_artifact_is_sealed_but_not_date_scanned` 同时钉住这两条，
防止下一次有人「顺手补齐」把两件事又合成一件——那正是 §9.42 二里
「三个统计量挤在一句话里」的同一类错。

13t 把 `probe_survivorship_bound` 与它的数据层 `probe_decile_net` 也封了进去
（15 → **17** 文件）。13s 的残留风险写的是「改探针 + 重跑会让产物变，变就红」
——这句话漏了「改了探针而产物字节不变」那一半：改 docstring、改一个零调用的
分支、改 `definitions` 的文字，四道门可以一起绿，而产物里的口径已经不描述生成
它的代码。这一轮的三处实证（`truncate_only` 承诺未实现、`synth_in_layer` 指向
空气、「闭式锚」挂错对象）全是这一类。

仍然不封 `probe_width_frontier` / `probe_liquidity_identity`：它们的**产物**不在
`SEALED` 里（没有字节冻结），只封生成器会造出「代码冻了、数还能换」的错觉。
这条链要整体做，留给下一轮——本轮不假装已闭合。

探索层的契约现在是四条：脚本入版本控制、产物入版本控制、产物自带口径字段并有
测试钉着、**判定链上的生成器与数据层进封签**。

#### 八、验证（现跑）

- 四道门全绿：封签未漂移（**17 文件**、历史第 14 行 `581cbc0f…`）；6 产物 326
  决策日、末日 2026-10-08、确认段 0 天；read-log 0 行；溯源 `94 = 12 + 82`、
  `逐项核对 36/36`、`覆盖 284｜94｜190`。
- `test_decision_number_provenance.py` + `test_decision_evidence_artifacts.py`：
  **39 passed**（36 → 39：纯截断档、三个锚、入层率各一条）。
- `-k decision`：**332 / 0 / 0 / 0**（`junit_13t_final`，80.5s；329 → 332）。
- 封签射程是**实测**的，不是声明的：13s 往产物尾巴加一个空格 →
  `verify-seal` 报「已改动（封签后）」；还原 → 绿。13t 补测另一半——改**生成器**
  的 docstring → 同样报红（`probe_survivorship_bound.py 已改动（封签后）`）；
  这一半在 13s 是全绿的，因为它当时不封。加进 `ARTIFACTS` 的写法仍被
  `sealed_but_not_date_scanned` 否掉（两条断言同时成立才算过）。
- 产物逐位对比旧版（`_survivorship_bound.pre13t.json` vs 新）：`injection` 12 行
  × 7 字段 **0 处不一致**，顶部 10 个标量全等，顶层只多一个 `truncate_only`
  键（`injection` 行内另加 `synth_in_layer` 一列）。**只增不改**，可逐位复核。

#### 九、下一步：不花钱的三件先做，PIT 只买 13bp 里的一个分布

**排序按「本地能不能做完」与「会不会改结论」，不按「听起来彻底」。**

1. **把篮子口径标成「不可识别」，而不是给一个数。** 本轮已把 mom 带子落盘
   （`mom_sensitivity`：−42.0 / +6.8 / −174.9 / +14.5）。要收窄它，缺的是
   155 只退市股的 **mom 序列**，不是它们的日线——便宜一个量级。
2. **功效那一半是真阻塞项。** `max_t_momentum_only`：12 格（2 策略 × 6 宽度）的
   argmax 正是 `turnover/N100/vs层`，`best_t = 2.1537`、`null_q95 = 2.5357`、
   `p_fwer = 0.1598`。**这个格子在多重比较下不过线，而 N≤100 就是产品定义。**
   拿 argmax 反推规格，等于把 FWER 要惩罚的选择效应固化成规格：生存偏差 13bp
   顶多把净 49 降到 36，p=0.16 才是否决项。改它要动预注册 §12.14 的判定单位，
   不能顺手改。
3. **PIT 名单排在最后。** 它能买的只有崩盘项 −13.4bp 里那 155 只的收益分布：
   最贵，且不改变任何结论的符号。
4. **把稀释项按日分解，别乘平均在场率。** §五 1b 已证乘法分解不闭合：折算高估
   2.1bp，与削平 2.81bp 方向相反、恰好相抵才让 −11.55/−11.97 看起来对上 −12.27。
   要闭合需要逐日权重 `w_d = 当日在场合成数 / 当日层大小`，产物里还没有这一列。
   它比 PIT 便宜一个量级，且能把「−12.27 到底是什么」钉在日级而不是平均级。

### 9.43 一个 t 塌了三层：跨 N、跨层、跨年（2026-11-08，13v）

#### 一、前提问题：这批数当时在仓库里一个都不存在

上一轮之后，工作区里有三份只活在控制台里的诊断：`|mom|` 是不是动量溢价的主人、
`t` 随 N 上升证明了什么、`abs` 过没过族系门。它们被写进一份报告，其中
「`394.2bp / t=3.12`，族系 `p=0.0155`」被当作「abs 有机制」的证据，
「`abs/500` 的 `+42.7 / t=3.07`」被当作可持有的版本。

复核这四件事：**四条要撤回，两处检验根本没做。** 但撤回之前还有一层前提——
`git grep` 全库（排除 `data/`）对 `394.2|143.3|42.7|0.0155|3.07` 只命中一处
无关的 skill 值。这批数当时只活在 `data/.scratch/_run_*.log`（被 `data/.scratch/*`
挡住）和 `.workbuddy/memory/`（被 `.workbuddy/` 挡住）里。**那是 13r 的病灶原样复发：
引用能克隆，数不能。**

所以这一轮第一件事不是写账本，是让这两份诊断有产地：

| 之前 | 之后 |
|---|---|
| `data/.scratch/diag_sign_vs_abs.py`（docstring：不写任何产物） | `scripts/probe_sign_vs_abs.py` → `data/.scratch/sign_vs_abs.json` |
| `data/.scratch/diag_abs_replicate.py`（同上） | `scripts/probe_abs_replicate.py` → `data/.scratch/abs_replicate.json` |

两份产物与两个生成器**同一次**进封签（`SEALED` 17 → 21）。重跑后 97 行输出与旧日志
**逐位相同**，所以下面引的每个数都有一条路径可以走回去。两份 JSON 没有逐日键，
因此进 `SEALED`、不进 `ARTIFACTS`——两个清单回答两个问题，不许互相顶替（§9.42）。
同一轮里 `_shippable_paths()` 改成从封签派生，`survivorship_bound.json` 那份手写
白名单也一起收了进去；「第四次」是从手写清单进来的。

#### 二、四条撤回

**（1）「t 随 N 上升 ⇒ 结论更强」——对 hi/lo 是机械的，对 abs 不是。**

组合 − 层的逐日超额，其标准差有一个机械下界 `sd ∝ g(N) = sqrt(1/N − 1/M)`。
若 `t(N) ≈ μ(N)/g(N)`，那 `t` 的形状只是 `μ` 的形状乘一个与排名无关的权重：
`sign_vs_abs.json → mechanical.corr_t_over_mu_g` 给 hi `0.9734`、lo `0.9859`。
**abs 是 `0.7658`**——这条不适用于它，它的 `μ` 与 `t` 不同步（`λ` 也差一半）。
这正是 abs 值得单独查的原因，也是它最后塌在别处的原因：不是「t 被分母撑起来的」，
是「这个 t 换一层就没了」。

**（2）「族系 p=0.0155 ⇒ abs 过了 18 格的门」——那个 p 只在成交额层算过。**

同一规则搬到换手率层（`abs_replicate.json → layers.rate.cells`）：

| 层 | 格 | 全样本 | 2017–2023 |
|---|---|---|---|
| 成交额 | `abs/100` | `+144.8 / t=+2.30` | `+53.3 / t=+1.08` |
| 成交额 | `abs/500` | `+42.7 / t=+3.07` | `+16.7 / t=+2.30` |
| 换手率 | `abs/100` | `+73.1 / t=+1.82` | `+50.9 / t=+1.07` |
| 换手率 | `abs/500` | `+16.0 / t=+1.31` | `+1.3 / t=+0.12` |

`abs/500` 的均值从 `+42.7` 掉到 `+16.0`，而 se 只从 14 到 12。**一个只在单一流动性
分层里成立的「机制」，是分层方式的产物。**

**（3）「394.2bp 是效应大小」——它连「多大」都不承载。**

两个独立理由。其一，它不可持有：`W=(-2,-1,0,1,2)` 是五分位加权差，实现里没有任何
组合对应这个权重。其二，`t` 对尺度不变——把 394.2 除以任何常数，`t` 一位不动。
所以「`t=3.12` 且 `394.2bp`」这句里，数字只贡献了「不为零」这一个比特；
有信息量的只有容量与可持有性。写成一句话，读者会以为那是「年化 394bp 的策略」。

**（4）「2024 起更强」——2024 是样本内的分界年，不是样本外。**

`abs_replicate.json → layers.turnover.cells["abs/500"].mean_share_from_2024 = 0.716`：
2024 起的 625 个决策日贡献了均值的七成，而它们只占 2347 个决策日的 27%。
同一格在 2017–2023 是 `+16.7 / t=+2.30`。用同一个样本的右半段去确认左半段的结论，
不是确认，是重述。

#### 三、两处漏做的检验：不是算错，是问漏了

| 漏做的检验 | 补做后的读数 | 后果 |
|---|---|---|
| 换手率层复现 | `abs/500` `+16.0 / t=+1.31`；2017–2023 `+1.3 / t=+0.12` | 「abs 有机制」塌成「成交额层的一个格子」 |
| 2024 前子样本 | `pre2024` 与 `mean_share_from_2024` 两个字段 | 「近年更强」塌成「均值在右半段」 |

第一条值得单独记：族系 18 格里，abs 的 6 格全在成交额层。**族系校正修的是
「同一层里挑了 18 次」，不是「换一层还成不成立」。** 前者是多重比较，后者是外部
效度；一个 p 值只回答前者，而报告用前者回答了后者。

#### 四、235.4 是这一族里唯一没被这四条碰到的数

`width_frontier.json → turnover_H60.layer_bp = 235.405`、`layer_t = 4.2306`；
`yearly_layer` 11 年里 10 年为正，唯一为负的是 2017 的 `−92.8`。它是
「低成交额 20% 层 − 全池」的**毛**溢价（未扣 19bp 单边成本），与层内怎么排无关，
所以四条撤回一条也没碰到它。

**但它和 42.7 不是两个可比的估计，是同一个分解的两段：**

    层 − 池       layer_bp                 235.4    t=4.23
    组合 − 层     abs/500.bp                42.7    t=3.07
    ────────────────────────────────────────────────────
    组合 − 池     两段相加                   ——      毛口径

这个加法在产物里是闭合的：`rows[i].gross_vs_pool_bp` 恒等于
`layer_bp + rows[i].gross_vs_bucket_bp`（N=10 是 `235.4 + 143.3 = 378.7`，
N=500 是 `235.4 + 4.2 = 239.6`，逐位相同）。**把 235.4 和 42.7 并排写成「两个结论」
就是跨对象缝数**——一个是「在哪一层选」，一个是「层内怎么排」。后者不成立
不等于前者不成立，反之亦然。

同一条规矩也适用于两个年度序列：`yearly_layer` 是 10/11（2017 为负），
`yearly_vs_bucket100` 是 9/11（2018、2022 为负）。**两个序列里都有一个 `−92.8`，
但那是两个不同的年份。** 报一个年度数时，连序列名一起报。

#### 五、判据写成测试，不写成散文

判据（预注册 §12.17 第一条）：一个宽度规则算「可采纳」，当且仅当**存在同一个 N**，
使它在两个流动性层同时满足 (a) 全样本 `|t| ≥ 2`、(b) 2017–2023 子样本 `|t| ≥ 2`。

按这个判据，abs 最好的那个 N 的跨层较差 `|t|` 是 `1.07`（换手率 `abs/100` 的
2017–2023）。`test_abs_width_rule_fails_the_cross_layer_and_pre2024_gate`
把它写成断言，从 `abs_replicate.json` 重算，不写死数字——所以改任何一位数、
或删掉一个层，它都会红。**这条判据的两个条件，正是这一轮漏掉的那两个。**

为什么不写进散文：散文里的判据会在下一次争论里被重新解释一遍，而写成断言之后，
谁能通过不取决于谁记得。

#### 六、这一轮不建立什么

1. **不建立「abs 无效」。** 建立的是「按这个判据不可采纳」。成交额层 `abs/500`
   的全样本 `t=3.07`、2017–2023 `t=2.30` 都是真的，它缺的只是跨层那一条。
2. **不建立「换手率层更干净」。** 它两边都更低（`hi/500` 的 `t=0.03`、
   `mean_share_from_2024` 为负），低到分不出「机制不存在」与「这一层没有信号」。
3. **不建立「2024 之后结构变了」。** 份额 `0.716` 说的是均值的来源，不是断点；
   要谈断点需要另一次预注册。
4. **不建立「235.4 是可实现的净收益」。** 它是毛口径的层溢价，容量受层内
   中位成交额约束；净口径与容量见 `width_frontier.json → rows[]`。
5. **不建立「abs 在换手率层不过族系门」。** `abs_replicate.json` 只给逐格 `t`
   与 `pre2024` 的 `t`，**没有在换手率层重跑 18 格的块自助零分布**。所以这一轮
   能说的是「同一条规则在换手率层的边际 t 不过 2」，不能说是「它也没过族系门」。
   要把族系 p 也算到换手率层，需要另一次 `probe_sign_vs_abs.py` 结构的多层版本；
   本轮不做，因为(a1)(a2) 已经足够否掉它——**先用便宜的证据否掉，再谈贵的。**

### 9.44 「0 条」有三种，而账本只会说一句 `[]`（2026-11-08，14a）

#### 一、`load_settlements` 的静默跳过

`ledger.load_settlements` 原来对解析失败的行 `except json.JSONDecodeError: continue`。
后果不是「少一条样本」，而是**两个下游看到两份不同的账本**：

- `settle()` 用它的返回值建 `done` 集合。跳过一行 ⇒ 该 key 不在 done 里 ⇒
  重算并**再写一行同键记录**。从此这笔样本被 `score()` 计两次，而两次都「正常」；
- `score()` 用同一个返回值算分。跳过一行 ⇒ 分母少一条，同样不报错。

同一份文件、两处读错、两处都不响。修法与 `load_cards` 的 K3 修复同构
（`docs` 里那条「K3 看不见损坏的卡文件」至此可以划掉）：解析失败、非对象、
`kind` 非结算记录、**同键重复**，一律抛 `LedgerCorruptError` 并带行号。
重复键也进这一档，是因为 `settle()` 的幂等性保证同 key 只写一次——出现两行
只可能来自写入中断后的重试或多进程并发，而这两种情形下静默收下等于让分数
无声失真。

四个 HTTP 读点（`/overview` `/score` `/pwin` `/settlements`）统一走
`_load_settlements_or_409`。必须包起来的原因很具体：`LedgerCorruptError`
是 `ValueError` 的子类，不包就会被同文件的 `except ValueError -> 400`
误判成「请求写错了」。

`producer.resolve_picks_with_pwin` 也踩同一条：`load_settlements` 在它的
`try` 之外，所以坏账本会一路上抛——但 API 的 `_resolve_picks` 只接
`PickValidationError` / `NoEvidenceError` 两个子类，不接第三个兄弟类。
现在多一个 `except`，并且**话不一样**：账本坏了要修文件，没有证据要攒样本。

#### 二、`ledger_health`：把「0 条」拆成互斥的几种

`GET /api/decision/health`，永不 409——它的全部意义就是「账本坏了也要能问」。

| status | 判据 | 下一动作 |
|---|---|---|
| `never_written` | `<data_dir>/user_data/decision` 不存在 | 还没开始计时，不是计时坏了 |
| `corrupt` | 有行读不出 / 卡文件读不出 | 修文件；在此之前任何统计量都不成立 |
| `degraded` | 全部可读，但有被 `done` 键固化的残缺行 | 修不了，只能承认分母在缩水 |
| `ok` | 其余 | —— |

`degraded` 与 `corrupt` 分开，是因为前者**永远不会自己好**：`settle()` 按
done 集合永久跳过已写 key，一条 `outcome_med=None` 的行是终态。此前这件事
只有 `median_covered < written` 这一个「有报告无告警」的读数，而每一次单独的
读数都是正常的。

本轮动手时的真实读数：`data/user_data/decision` **不存在**，宿主与容器
（`/app/data/user_data/decision`）都没有 ⇒ `status=never_written`。在此之前，
「从未发卡」与「有卡但读不出」在每一个端点上都是同一句「0 条」。

变异验证（`test_decision_ledger_health.py`）：塞一行坏 JSON → `corrupt` 且
`load_settlements` 抛错；删掉 → `ok`。单标的种子日跑一次结算 → `degraded`
（`median_missing=1`），且第二次 `settle()` 写入 0 行——「固化」这件事有断言了。

#### 三、沙盒回放：整条链在真实 parquet 上跑一遍

`scripts/verify_decision_replay.py`（测试 `test_decision_replay_sandbox.py`）。
沙盒是临时目录 + 三个指向真实分区的软链接 + 一个空的 `user_data`，真账本
全程不被触碰。

一次运行的读数（`--json` 落盘）：

| 项 | 值 |
|---|---|
| `cold_start_status` | `skipped_no_evidence` |
| `produce_status` / `pwin_source` | `created` / `prior` |
| `settle.written` / `read_back` | 3 / 3 |
| `settle.median_covered` | 3（= written） |
| 成交价差（3 笔） | 0 bp |
| 收益差（最大） | 0.004956 bp |
| 全市场中位数差 | 0.265275 bp |

第二路径不从 `app.decision.bars` 调任何函数：直接 `pl.read_parquet` 读
`date=<day>/part.parquet`，复权用 `adj_factor/all.parquet` 的**事件表**累乘
（`ret = raw_exit · Π(factor) / fill − 1`），中位数也由不复权价 + 事件表重算。

**它的边界要说清**：它复用列语义，不复用代码。两份实现同时错在同一处才会
同时错，这比单路径强；但它证明的是「与不复权价 + 除权事件自洽」，不是
「与交易所对账」。真正的第二来源是另一个数据源，本地没有。

#### 四、回放顺带测出来的：复权序列的新鲜度

那 0.265275 bp 不是浮点噪声。查下来是**除权事件没有并入
`kline_daily_enriched.close`**：`close` 是「按最新因子折算」的复权价，
一个新事件到达后，事件之前的所有日子都要重新折算；没重算的那些日子，
`close/raw_close` 在除权日不发生阶跃。

    603858.SH  2026-11-05  ex_factor 1.031894（分红 0.48 / 前收 15.53）
    close/raw_close: 2026-11-04 = 0.969092  2026-11-05 = 0.969092  ← 没有阶跃

后果对结算来说是硬的：`_settle_pick` 的收益是
`exit_close / (fill · close_entry/raw_entry)`，两个 `close` 被同一个陈旧因子
除过 ⇒ 因子约掉 ⇒ **这段收益退化成不复权价收益，整段分红被丢掉**。
603858.SH 那一笔丢掉 3.19pp，002142.SZ 1.19pp，窗口内样本的丢幅中位数约 0.6pp。
而结算行一经写入就被 done 键固化，事后无法重算。

暴露面（最近 120 个交易日 4249 个除权事件）：4183 已并入、**24 未并入**、
42 无法判定；24 个全部落在最后两个数据日（11-05 / 11-06），也就是「事件刚到、
下一次重算还没跑」。窗口 2026-10-29…11-05 内是 6/31 未并入。

这不是账本的口径错误，是行情侧的新鲜度滞后。但它在账本上的表现是
**一笔永久错误的结算行**，所以不能只记在数据管道那边。

**这一轮不改它**，原因不是修不了：把 `_settle_pick` 的收益换成
「不复权价 × 事件因子」就能免疫（那样它读的就不再是派生列，而是原始价 +
事件表）。改它等于改被测量的量本身，需要单独一轮：新的收益口径要重新
回答「哪些样本的 `outcome` 会翻面」。回放脚本先把这件事**量出来**
（`adjustment_check` 不计入 `ok`，但与结论一起打印），不让它藏在绿灯里。

> **后续（同日，14c）**：§9.46 已按这里写的修法改掉，并把「哪些 `outcome`
> 会翻面」量到了符号级。上面这段保留为当时的判断——它错的是**排序**，
> 不是修法。错误在「改完之后历史行不会自动重算，所以要单独一轮」这个前提：
> 历史行是零条。


#### 五、这一轮不建立什么

1. **不建立「结算链路正确」。** 建立的是「3 笔、单日、H=5 的窗口上，两条
   独立路径一致到 0.005bp」。样本外推要靠每一天自动发的卡。
2. **不建立「median 口径可用」。** 沙盒里 `median_covered == written` 是因为
   当天全市场分区齐全；生产上的第一个 T+20 仍需按 §9.44 二的 `degraded` 看。
3. **不建立「1bp 阈值是够的」。** 它是本次验收标准，不是统计结论；它挡不住
   §四 那种 3pp 级的偏差（那个偏差来自数据新鲜度，不是来自计算）。
4. **不建立「确认段有进展」。** 确认时钟仍是 1/250（`decision_gate.py audit`）。

### 9.45 `estimate()` 不读落盘的 `p_win`（2026-11-08，14b）

A2 原本的靶子是「`allow_prior` 种下的卡把 `skill_vs_clim` 机械压向 0」，
机制写成「`_select` 不看 `p_win_source` ⇒ 那一段 p_win 恒 0.5 ⇒ 压向 0」。

前半是真的，后半不成立。`estimate()` 只从行里读 `outcome`（`_field(basis)`）
来数 `hits` / `n`，`walk_forward()` 每一天的 `p` 也是由**过去 outcomes**
重新收缩出来的——两处都不读行上的 `p_win`。实测：同一批 outcome，把落盘
`p_win` 从 0.5 改成 0.9，`estimate()` 的返回**逐字节相同**。

压向 0 只发生在 `score()`，而且在那里它是**精确**的：median 口径下
`brier_const` 恒为 0.25，prior 行的 `brier` 也恒为 0.25 ⇒ 该行对 `skill`
的贡献恰好是 0。把落盘 `p_win` 从 0.5 改成 0.9 时，`skill` 只由这组样本的
命中率 `h` 决定：`skill = 3.2h − 2.24`（= 1 − [0.81(1−h) + 0.01h] / 0.25）。
`h=0.5` 时它是 **−0.64**，`h=0.9` 时是 **+0.64**。

所以 −0.64 不是这批样本的实测值，是「`h` 恰为 0.5」这个条件下的代数取值；
原句既没指名是哪批样本，也没写出 `h`，换一批就换一个数。要保留它，就得写成
上面那条含 `h` 的式子，而不是一个不带条件的数。

所以 A2 要改的是**读数**，不是估计：`score()` 的 `by_source` 已经报了
`prior` 的 `n` 与 `days`（分组计分本来就有），缺的是总分的旁边没有一句话
说「这里面有 k 条来自常数预测器」。把 prior 行从 `estimate()` 里剔除是错的
方向——它们的 outcome 是真实结算，属于市场的证据，不属于预测器的证据；
两者的区别要用字段说，不能用删样本表达。

### 9.46 收益口径换到事件表：分红不再依赖派生列的新鲜度（2026-11-08，14c）

#### 一、为什么这一轮先做它，而不是先提交仪器

上一轮的排序是「先提交仪器，再单独一轮改口径」，理由是：「改完之后已有的结算行
不会自动重算，得同时决定要不要把历史行作废重放」。

这个理由的前提是**有历史行**。核实：`data/user_data/decision` 不存在 ⇒ 生产账本
**0 行**；要作废的集合是空集，要重放的集合也是空集。于是这条理由不仅不成立，还
反过来：生产者一旦打开，写进去的行就被 `done` 键永久固化，**迁移成本随时间单调
上升**。零成本窗口只存在于「打开之前」，而现在是打开之前。

仪器那一半不必用一次提交换取：`-k decision` 已是 353 passed / 0 failures /
0 errors / 0 skipped（junit 为准），四道门全绿。先提交的价值是「有一个可回滚点」，
而口径改动的可回滚点由 git 本身提供——分成两次提交即可，不必分成两轮。

#### 二、改的是什么：不是公式，是公式的输入

旧式子（`_settle_pick`）：

     ret = close_exit / (fill · close_entry / raw_entry) − 1

**这个式子在代数上是对的**。设 `close_t = raw_t / Π_{d>t}`（`ex_factor` 的口径，
见 `app/indicators/pipeline.py` 表头），则

     close_exit / close_entry = (raw_exit / raw_entry) · Π_{entry<d≤exit}

即它本来就把窗口内的分红乘了回去。问题在前提：这一步要求两端的 `close` 出自
**同一次因子折算**。而 `close` 是按日增量重算的派生列，事件到达与「受影响标的
全历史重算」之间有滞后；滞后窗口里两端因子相等，`Π` 项被约成 1，分红整段消失。

所以修法是**换输入**，不是换公式：查 `adj_factor/all.parquet` 的事件表，直接算

     ret = raw_exit · Π_{entry<d≤exit} ex_factor / fill − 1

事件表是**输入**（除权公告的落库结果），不是派生列，不受那段滞后影响。

新增 `bars.ex_factor_product(data_dir, symbols, entry, exit) -> dict | None`。
返回 `None` 不是零值，是**「窗口不可判定」**：缺表、空表、表的时间范围盖不住
窗口，三者都是 `None`。这条契约是这一轮唯一容易写错的地方——`None` 当 1.0 用，
就重新引入了一模一样的错误，只是换成更安静的形式（不再有陈旧因子可约，改成
直接把分红当 0）。

`_settle_pick` 的分支顺序变成 `EVENT` → `ADJUSTED` → `RAW`，并把实际口径落盘
（`price_basis` + `window_ex_factor`），结算汇总里加 `return_basis` 计数。降级
不算错，**降级不可见**才算错：`{"EVENT": 20}` 与 `{"ADJUSTED": 20}` 在管道日志里
必须长得不一样。

#### 三、验收：先红，再绿，再变异

**红**：`tests/test_decision_settle_ex_dividend.py` 先把滞后**造出来**（不依赖管道
恰好落后）——复权序列按陈旧口径写死，事件表按真实口径写入，两者自相矛盾。
入场不复权 10.00、出场不复权 9.53、事件因子 10/9.52。旧口径 9.53/10 − 1 =
**−4.70%**（亏），正确口径 9.53·(10/9.52)/10 − 1 = **+10.5bp**（赚）。**符号相反**，
`outcome` 从 0 翻成 1。旧实现下这条测试红在 `price_basis == "ADJUSTED"`。

**绿**：改 `_settle_pick` 后 3 条用例全过。另外两条钉「不知道 ≠ 没有」：无事件表
→ `ADJUSTED` 且 `window_ex_factor is None`；表只覆盖更晚的日期 → 同样降级。

**变异**（两次，都是单点改动）：
- `exit_raw_f * fac` 改成 `exit_raw_f / fac` → 单元测试红，2 条仍绿；
- `if window_fac is not None …` 短路成 `if False …` → **回放新用例红，其余 3 条
  不红**。后者是这一轮最重要的信号：旧的回放测试对这条路径**完全没有感知**，
  默认抽样避开了有事件的标的，0 bp 只证明「没事件的标的算得对」。

**真实数据**（同一 as_of=2026-10-28、H=5、entry 10-29、exit 11-05）：

| 用例 | 窗口内未并入事件 | 收益差（最大） |
|---|---|---|
| 默认抽样（3 只，避开事件标的） | 6/31（全部被避开） | 0.004956 bp |
| `--symbols` 取全部 6 只未并入标的 `--include-events` | 6 只各自 1 个（N=6） | **0.004601 bp** |

这 6 只的漏算幅度（`adjustment_check.unbaked_symbols[].dropped_pct`）从
0.0704pp 到 3.1894pp，跨 45 倍；而收益差从 0.001087bp 到 0.004601bp，只跨
4.2 倍，且不随幅度单调——漏算最大的 603858.SH（3.1894pp）差 **0.002676bp**，
漏算最小的 300170.SZ（0.0704pp）差 **0.004601bp**，恰好最大。所以那个最大值
是数值噪声，不是漏算的量级。这条路径的证据是「6 只全部 < 1bp」，不是某个
最大值的具体大小。

`--include-events` / `--symbols` 就是为这条反向取样加的：均匀抽样抽不到有事件
的标的，而「没抽到」会被读成「没问题」。

> 表中两行是 2026-11-06 数据快照上的一次运行读数。分区前滚、事件并入之后，
> 同一条命令会给出不同的数字——用例断言的是**关系**（差 < 容差、`unbaked > 0`），
> 不是这两个数值。文档引用的是某一次运行的快照，复现时以当次输出为准。


#### 四、`adjust_factor` 挂进沙盒：少挂一张表 = 少测一条路径

`build_sandbox` 原本只软链接三张表。事件表不在沙盒里时，`ex_factor_product`
返回 `None`，结算**如实**降级成 `ADJUSTED` —— 降级本身是对的，错的是回放看不见
它降级了，于是整轮回放测的是即将被替换掉的那条路径。这一条已经写进 `TABLES`
的注释。

#### 五、没修的：`_median_ret` 仍读派生列

`median_ret` / `outcome_med` 还是用 `kline_daily_enriched.close` 算全市场收益
中位数。同一个窗口上的实测偏差 **0.265275 bp**（回放 `max_median_diff_bp`），
来自那 6 只未并入的标的，被 5000+ 只样本的中位数吸收。

没修，理由是量级与代价：分红在单只上的漏算被中位数稀释三个数量级，而修它要给
每个 `(entry, exit)` 对全横截面做一次事件表 join，并重新建立一条第二路径来
验证。**这个 0.265275 bp 现在是 `outcome_med` 阈值上的已知项，不是未知项**——
它比修复前的单笔 3pp 小三个数量级，且不随标的集中度放大。

#### 六、新增的风险与边界

1. **事件表的覆盖起点是 2025-05-07**（9251 行 / 4151 只 / 无重复键 / 无空值）。
   早于它的窗口一律 `None` → 降级 `ADJUSTED`，在 `return_basis` 里计数。降级
   可见，但**不等于正确**。
2. **`ex_factor` 的语义靠上游保证**：它是单事件比值，不是累积因子。若上游改成
   累积口径，这里会静默变成重复连乘。「窗口不可判定 → `None`」挡住的是时间维度，
   挡不住语义维度。可执行的对冲只有一条：`bars.EX_TABLE` 的注释与
   `indicators/pipeline.py` 的表头互相指认，改一边就该红。
3. **历史行仍然是零条**。这次修复之前写入的行数为 0，所以不存在「半份账本两种
   口径」。这个结论**只对当前这个 data_dir 成立**——若容器与宿主的 `data` 不是
   同一份挂载，要先确认容器内的 `/app/data/user_data/decision` 也是空的。

#### 七、这一轮不建立什么

1. **不建立「收益与交易所对账」。** 建立的是「与不复权价 + 除权事件自洽」，
   而且只在单日、3 只、H=5 上。
2. **不建立「median 口径无偏」。** 见五，0.265275 bp 是已知项。
3. **不建立「降级不会发生」。** 事件表被清空、换源、或覆盖起点向前推进时，
   `return_basis` 会立刻出现非 `EVENT` 的计数。
4. **不建立「`outcome` 的翻面规模」。** 本次只在造出来的滞后上验证了符号翻转
   （−4.70% → +10.5bp）；真实账本里有多少条会翻，要等生产者打开后按
   `window_ex_factor ≠ 1` 统计。

#### 八、下一步（按代价排序）

1. 提交 A0（账本四态 + 409）与 14c（事件口径）为两次提交，`-s` 签名。
2. **打开生产者之前**，先手工跑一天 `settle()`，确认 `return_basis == {"EVENT": n}`。
   这一条比任何单元测试都直接：它是唯一能看到「事件表在这个 data_dir 上真的被
   读到」的检查。
3. `_median_ret` 的事件口径（第五节的量级已知，随时可以提优先级）。
4. §9.45 留下的 A2 读数：`score()` 的总分旁边标注「k 条来自常数预测器」。

### 9.47 计分按收益口径分组：降级在消费层可见（2026-11-08，14d）

#### 一、一个字段在管道里活着，在消费端等于不存在

§9.46 给每一行落了 `price_basis`，给结算汇总加了 `return_basis`，理由是
「降级不可见才算错」。那句话只兑现了一半。

`return_basis` 统计的是**本次运行**追加的行；一周前写进去的降级行不会在
今天的汇总里再出现一次。而 `score()` 只按 `outcome` 非空筛样本，不看这一列：
`price_basis` 在全仓的读取点，除 `settle.py` 自己之外是零。于是一条
`ADJUSTED` 行进了 `score()`，与 `EVENT` 行混算 Brier / skill，分数上再也
看不出掺了几条不含分红的样本。

两个字段回答两个不同的问题，不能互相顶替：

| 字段 | 位置 | 对象 | 回答 |
|---|---|---|---|
| `return_basis` | 结算汇总 | 本次运行的增量 | 今天写进去的 n 条里，几条降级了 |
| `by_price_basis` | `score()` | 当前样本集的存量 | 这份分数里，掺了几条降级行 |

#### 二、为什么字段叫 `by_price_basis` 而不是 `by_basis`

`score()` 的返回里**已经有** `basis`，取值是 `abs` / `median`——它选的是因变量
（涨没涨 / 跑没跑赢中位数）。再放一个 `by_basis` 进去，同一个词就指了两件事。
跨对象缝数是这个仓库复发率最高的错误，字段命名不该主动制造一处。

#### 三、实现上只有一处需要小心

`_source_breakdown` 与新的口径分组共用 `_breakdown(settled, basis, column)`。
不共用就得复制那段「abs 口径的分母必须是展开 climatology」的分支，抄错一次
的后果是分组 skill 用了错基线——而错基线的 skill 恒 ≤ 0，看起来像「这组不行」，
不像「代码错了」。

顺带修掉一处**不是本轮引入**的键集漂移：`score()` 的空样本分支缺
`excess_se_hac` / `excess_lcb_hac`，空账本上读这两个字段会 KeyError。它被
「空样本与非空样本键集一致」那条用例抓出来——那条用例原本是为 `by_price_basis`
写的，第一次跑就红了，红在别的地方。

#### 四、验收

- **先红**：3 条新用例在改前全红（两条 KeyError + 一条键集差 2 个字段）。
- **变异**（两次，单点）：
  - `by_price_basis` 的分组列传成 `p_win_source` → 2 红 1 绿；
  - `n_non_event` 的 `!=` 改成 `==` → 1 红 2 绿。
- `-k decision` **360 passed / 0 failures / 0 errors / 0 skipped**（junit 为准）。
- 四道门：封签未漂移、audit 干净、verify-log 0 行、溯源 `94 行 / 284 分母 /
  失败 0`、`36/36`——新用例不含触发词，计数不动。

#### 五、这一轮不建立什么

1. **不建立「分数已按纯净口径算」。** 总分仍是全部样本。那是策略实际交出来的
   成绩；「只在 EVENT 行上算的成绩」是分组里的 `skill`，不是另一个总分。
   要不要把总分改成只算 EVENT，取决于产品问题问的是哪个——现在不做这个决定。
2. **不建立「降级行会被修」。** `done` 键仍然固化它们。本轮让它们**可数**，
   没让它们**可改**。可改需要一条重结算路径，那是另一轮。
3. **不建立「前端会显示」。** 字段已进 `/api/decision/overview` 与 `/score`
   的响应（`score()` 的返回被整块透传），页面没有接。
4. **不建立「`n_non_event` 能指认是哪几行」。** 它只给数量；要指认得读账本。

### 9.48 「不知道」里还要再分一层：等下去会变的那一种不该写进账本（2026-11-08，14e）

#### 一、三处「不可判定」被压成了同一个 `None`

`ex_factor_product` 的契约是「不可判定返回 `None`，不要乘 1」——这条契约是
§9.46 的成果，它把「窗口内没有除权事件」与「不知道有没有」分开了。但 `None`
内部还有三种成因，而调用方只能一律降级：

| 成因 | 等一个交易日会变吗 | 该做什么 |
|---|---|---|
| 缺表 / 空表 | 不会 | 降级并标注 |
| 窗口早于表首 | 不会：表不向后生长 | 降级并标注 |
| 窗口晚于表末 | **会**：同步追上即自愈 | 挂起，不写盘 |

第三种是**同步时序竞争**（bar 先落、事件表后到），也是唯一会在生产者打开
第一天就静默发生的一种。原来的行为是照常降级成 `ADJUSTED` 写盘，然后被
`done` 键永久固化：下一个交易日事件表到了，同一条键再也不会被重算，而那次
算出来的收益**连符号都可能相反**（分红整段消失，§9.46 的第一条用例就是这个
形态）。

分界不是「严重程度」，是**等待是否会改变结论**。三处压成一个 `None`，就没法
按这条分界分流。

#### 二、为什么不一律挂起

「窗口不可判定就别写」听起来更保守，但它会把 `before_start` 也一起冻住：
表首只会往前推（重建、换起算日），窗口一旦落到表首之前，等到天亮也不会变。
一律挂起的直接后果是那些卡**永远不结算**，而 `pending` 只是一个数字。

这不是假设。`test_event_table_that_does_not_cover_the_window_is_not_treated_as_empty`
在 §9.46 就钉住了「只覆盖更晚日期 ⇒ 降级并标注」。把两种方向合并成一条挂起，
这条用例立刻红——它红得对。

#### 三、实现：判据只有一处

`bars.ex_window_state` 返回四种状态之一，`ex_factor_product` 也走它，两处不
各写一遍比较（改一处就分叉）。`settle._settle_pick` 的返回值从
`记录 | None` 改成 `(挂起原因, 记录)`：`pending` 一个计数答不出「为什么还没
结算」，而四种成因要触发的动作相反——未到期什么都不用做，停牌等复牌，
`window_uncovered` 要去看同步。

`settle()` 的汇总因此多了两项：

| 字段 | 回答 |
|---|---|
| `pending_reasons` | 这次没写的 n 条，各是什么原因 |
| `event_coverage` | 表首/表末/行数 vs 日历首/末 + 空洞连段，降级还剩几个入口 |

`event_coverage` 的十个键分三组，各有各的读者：`ex_table_first/last`、`ex_rows`
答「表长什么样」；`calendar_first/last`、`endpoints_span_calendar` 答「端点是否
盖住日历」（**汇总格，不是判据**，真判据是 `ex_window_state` 的逐窗口比较）；
`gap_sessions` / `gap_runs` / `gap_max_run` 答「声明范围内哪几段没有事件行」。
`future_dated_rows` 单独一组：只回答「有没有已公告还没走到的除权日」，健康
状态下它恒大于 0，所以它是读数不是告警。

`event_coverage` 在**零张卡**时也说话——那正是「账本还没开张、先确认事件表
盖住日历」的时刻，也是原来唯一能给出结论的检查（手工跑一天 `settle()` 看
`return_basis`）失效的时刻：0 张卡时 `return_basis` 三个键全 0，任何
`== {"EVENT": n}` 的断言在 `n = 0` 都是空真。

`event_coverage` 的局限要写在字段旁边：表末来自**已公告的除权日**，不是覆盖
终点。同步停摆后，表末要等已公告的那几天走完才会落到日历末日之前，所以这一格
是滞后指标，不是即时信号；即时信号是 `pending_reasons.window_uncovered`。

`gap_*` 三项同样是读数不是告警。A 股除权淡季（1–4 月）里成片的无事件日是正常
形态：把「某段交易日没有任何事件行」接进告警，每个淡季都会连响十几次，而那时
数据是好的。它真正回答的是「21 个空档是 12 个短洞还是一段长黑洞」——这两种
状态要采取的动作相反，而 `gap_sessions` 一个标量分不开。段按**交易日相邻**切，
不是按「日期相差几天」切：同一份空档，两种切法给出 12 段/最长 5 天与 9 段/
最长 6 天两套数，所以只存标量连「这个数是怎么切出来的」都还原不回来。

#### 四、验收

- **先红**：新增用例在改前红（挂起分支短路 ⇒ `written` 为 1 而非 0）。
- **变异**（两次，单点）：
  - 挂起分支短路成 `if False:` → 新用例红，其余 3 绿；
  - 把 `before_start` 也并进挂起 → **既有**用例
    `test_event_table_that_does_not_cover_the_window_is_not_treated_as_empty` 红。
    这一条两边都红，说明分界是承重的，不是装饰。
- `-k decision` **361 passed / 0 failures / 0 errors / 0 skipped**（junit 为准）。
- 四道门：封签未漂移、audit 干净、verify-log 0 行、溯源 `94 行 / 284 分母 /
  失败 0`、`36/36`——新增文本不含触发词，计数不动。

#### 五、这一轮不建立什么

1. **不建立「挂起有期限」。** 真正的永久挂起不是 `window_uncovered`。
   `ex_window_state` 先判 `entry < 表首`、再判 `exit > 表末`（`bars.py:201-204`），
   所以表首一旦越过某张老卡，那张卡落到 `before_start`：**不进
   `pending_reasons`，照常写盘**——`EVENT` 支被 `ex_factor_product` 的 `None`
   挡掉后，`ADJUSTED` / `RAW` / `invalid` 三支都落记录，随后被 `done` 键固化，
   没有重结算路径。这比挂起更坏：挂起至少可见、可等、可数，它三条都不占
   （`invalid` 那一行连 `price_basis` 都没有，不进 `return_basis` 任何一桶）。
   触发它也不需要「滚动窗口重建」：`_merge_adj_factor_store` 在 `all.parquet`
   缺失时只写本次窗口，`extend_history` 在无 `ADJ_FACTOR` 权限时只前扩日历
   不扩事件表——两条日常路径都会把表首推到日历首之后。
   另一类永久挂起在 `entry_missing` / `exit_missing`：`entry` 取的是市场日历上
   `as_of` 之后的第一个交易日，不是该标的复牌后的首个可交易日，停牌入场或
   退市的卡会永远等下去。根因不在 `build_card` 少校验交易日（那只挡 `as_of`
   本身不是交易日），在 `_settle_pick` 用市场日历代替了标的的可交易性。
   带期限的降级是另一轮，且要先定 K。
2. **不建立「`ADJUSTED` / `RAW` 已死」。** 缺表与早于表首两条路仍然降级，
   §9.46 / §9.47 的口径分组照旧有用。
3. **不建立「前端会显示 `pending_reasons`」。** 字段已进 `/api/decision/settle`
   的响应，页面没有接。

### 9.49 回执写侧 + 结算行的读数：账本记读到什么，回执记自己是谁（2026-11-08，14f）

#### 一、两处 delta，一处照原样落地，一处改了归属

`event_gap_*` **不进回执**，进 `event_coverage`。三条理由，按分量排：

1. **它需要日历，而回执写侧在同步层。** 让每条同步链（含 ETF 透传、`extend_history`）
   为了一个读数把日历依赖拖进来，等于把判据和它的输入分开放，两处迟早不同步。
2. **它能随时重算。** 回执的不可替代性在「不可重算」的那部分——表离开时长什么样。
   把可重算的读数冻进只增不改的文件，后来的读者会把它当成**当时**的证据，而它其实
   只是**现在**的读数。
3. **读它的人在看结算报告。** `event_coverage` 与它在同一份响应里，不需要跨表 join。

`run_id` **不进账本**，留在回执里，全串（`20261108T040244Z-1a2b3c4d`）。账本记
`ex_table_first` / `ex_table_last` / `window_ex_events`：**读到了什么**，而不是
**哪次跑的**。理由不是「全串还是前缀」，是方向——账本活得比回执久，指针不能从
永久指向临时；而结算读的本来也不是一次同步，是所有 ≤T 的同步叠加后的状态（每次
全量重写 `all.parquet`，外加每天那个 15 天兜底窗口）。

#### 二、`window_ex_events` 的 `0` 与 `None` 必须分开

`0` 是「窗口判得出、确实没有除权」；`None` 是「窗口判不出」（缺表 / 早于表首 /
晚于表末）。写成同一个值，`price_basis == "EVENT"` 又 `events == 0` 那种行就没法
与「事件表当时没跟上、只是恰好没查到行」区分——而后者正是 §9.46 的起因。

端点仍照记：端点本身可读，只是不足以判定窗口。所以一行 `ADJUSTED` 可以同时有
`ex_table_last = "2026-03-11"` 和 `window_ex_events = None`，这组合是有信息的。

#### 三、`store_created` 锐化 `before_start`，但锐化的不是「相等」

上一轮的说法是「表确由某次已知请求创建时，表首才等于那次 `requested_from`」。
**这句话是错的**：表首是**第一条事件日**，通常晚于请求起点——建表时只拉到
`[requested_from, requested_to]` 内的事件，第一条常常落在窗口中间。

正确形态是 `store_created and requested_from <= table_first`，它证明的是
「**表首之前没有事件**」（这张表只由这一次请求建立，别处没有更早的行），而不是
「表首就是请求起点」。这两句话的差别决定了 `before_start` 能不能被升级成事实：
前者能，后者不能。

#### 四、`scope` 必须由调用方声明，透传层不能替它改口

`scope` 是调用方对「这次是不是全市场清单」的声明，不是从 `len(symbols)` 反推的。
默认 `symbols`：声明不足时读侧不给任何标的记覆盖，这个方向可以补；反过来是凭空
宣称已同步，补不回来。

`index_sync.sync_etf_adj_factor` 是一层透传，它**漏传** `scope` 有两种坏法，方向
相反：写死 `universe` → 调用方只同步了部分 ETF 时回执也宣称全市场已覆盖；写死
`symbols` → ETF 永远没有覆盖，而 ETF 恰恰最需要这个字段（自定义源对 ETF 空返回，
与「该 ETF 无除权」无法区分）。所以它必须穿透，不能在这一层决定。

`failed_out` 是出参，不改返回元组：失败标的不进 `affected`，后者是 enriched 的
重算清单。四个出口各有 `outcome`：

| 出口 | `outcome` | 为什么不能合并 |
|---|---|---|
| 部分批次失败（TickFlow 分块） | `partial` | 成功那批的覆盖要保住，失败那批要点名 |
| 全部批次失败 | `failed` | 与「成功但零行」同样返回 `(0, [])`，只有这里分得开 |
| provider 返回空 | `ok` | 空返回是「这段时间没有除权」的答案，不是失败 |
| 无权限 | `skipped` | 请求没发生；与「成功零行」的动作相反（去授权 vs 什么都不做） |
| 自定义源（扶摇等） | `unverified` | 逐标的完整性不可验证，见下 |

最后一行是本轮唯一「读代码才看出来」的一条：**扶摇把整份 dump 的失败吞成空表**
（`plugins/fuyao/provider.py:737-739` 捕获 `FuyaoError` 后 `return 空表`），而空表
同时就是「这批标的没有除权」的答案——两者在返回值上同形。所以自定义源分支
（含拿到非空数据的分支：前收盘价缺、涨跌停自检剔除的标的会静默缺席）一律记
`unverified`，而它**不在 `COVERING_OUTCOMES` 里**。

这条直接决定了下一步：本机 `preferences.json` 配的正是 `fuyao`，也就是说
**在当前配置下，回执还不能作为覆盖判据**。要换判据，先让 provider 把失败报出来
（或在 `base.get_adj_factors` 上加 `failed_out`），否则换过去的 `window_uncovered`
要么恒假（fail-open，回到 §9.46 的错），要么恒真（所有结算永久挂起）。

自定义源分支没有逐标的失败信息，只能记 `unverified`；这是已知的粗粒度，不是遗漏。

#### 五、验收

- 三个变异各自红在预期用例，都不是「顺手红一片」：
  - `ex_window_readings` 忽略窗口状态（`if True:`）→ `0`/`None` 那条用例红；
  - 空洞分段不按事件日切断（`continue` 替代 `runs.append`）→ 分段用例红；
  - 去掉 `Receipt.covers` 的 `scope != "universe"` 守卫 → 子集 scope 无覆盖用例红。
- 溯源 `94 行 / 284 分母 / 失败 0`，`36/36` 项核对通过——新文本里没有触发词
  （「不重复算成」这种写法会被子串命中成 `复算`，已经改掉）。
- 四道门：封签未漂移、audit 干净、verify-log 0 行、溯源通过。
- `-k decision` **379 passed / 0 failures / 0 errors / 0 skipped**（junit 为准）。
  全量 `pytest tests` 在这台机器上有 25 条与本轮无关的红：其中 6 条因为
  `data/user_data/preferences.json` 把 `daily/adj_factor/minute` 三个 provider
  都指向自定义源，测试桩被绕过；其余是 CRLF、文件锁、cgroup 内存预算一类的
  环境差异。判定方法是拉一个干净 HEAD 检出作基线，逐条比对失败 id，而不是
  看总数。
- 真实数据复算不是靠一段临时脚本：`bars.ex_coverage(data_dir, bars.sessions(data_dir))`
  是生产函数，返回 `ex_rows` / `gap_sessions` / `gap_runs` / `gap_max_run`，
  上面引的读数都能用它一行复现。
- 同步侧集成（`test_adj_factor_sync_integration.py`）**不测参数，测门**：从任务层
  调用真实的 `sync_adj_factor` 写盘，再用 `receipt.window_covered` 读回来。三种
  失效都在跨层时才现形——某个调用点漏传 `scope`（默认 `symbols` 不报错，只是
  永远不给覆盖）、区间口径改了（写侧测试跟着改，读侧看不见）、回执落错文件。
  所以断言包括反向的一半：窗口跨过请求上界时门必须判 False。只会答 True 的判据
  与「没有判据」在测试上一样。
- 静态守卫：`app/**` 里每个 `sync_adj_factor` / `sync_etf_adj_factor` 调用点必须
  显式写 `scope`。`run_now` 的两处与 `extend_history` 的一处要跑完整条盘后管道
  才够得着，行为测试覆盖不到，而漏传是静默的。它只证明「有声明」，值由上面两条
  行为用例守。
- 变异三条，各自红在预期位置：去掉独立同步的 `scope="universe"` → 集成用例与
  静态守卫同时红；`requested_from` 改成取 `end_time` → 两条集成用例红；
  `window_covered` 恒 True → 区间外那条断言红。

#### 六、这一轮不建立什么

1. **不建立「空洞可以触发挂起或降级」。** 空洞是读数：淡季成片无事件是正常形态。
   真正的判据仍是 `ex_window_state` 的端点比较，它回答「表有没有声称覆盖这段」。
2. **不建立「`window_uncovered` 已换判据」。** 回执写侧先攒事实；换判据是下一步，
   而且要把 `scope` 的语义一块搬过去——换成 fail-open 只差一个默认值。
3. **不建立「回执可以反推缺口」。** 回执能证明「问过这一段」，永远不能证明
   「这一段没有事件」。这就是 `before_start` 退不了役的原因。
4. **不建立「全历史同步能解锁 `window_uncovered`」。** `run_adj_factor_sync` 在本地
   还没有任何日K时 `start=None`，而回执的 `requested_from` 只接受日期——填入哨兵
   日期等于把「我问过全部」写成一个具体的谎，所以宁可不写。代价是：独立同步按钮
   跑完，门仍然看不到任何覆盖。今天没有消费方，这是潜在问题；一旦换成回执判据，
   它会变成「看起来像同步没跑」的死结，而重跑多少次都不会变。换判据那一轮必须
   同时给无下界一个表示法，或让该路径把下界钳到表首。

### 9.50 一个口径一份登记表：合并会发明一种合法状态，而它正好吃掉两种真失败（2026-11-11，14g）

起因很窄：`analysis/*.py` 里有 106 条断言行没有任何出处，而默认的三组 glob 一条都不扫。
两条路——**合并**成一张表（加「这次没扫到 = 不在本口径 = 不判」），或者**每个口径一份表**
（`--registry` 早就存在）。选后者，因为合并必须发明一个**合法状态**：「不在本口径」。

那个状态吃掉两种真失败，两者都表现为「这个文件不再产出断言行」：

| 场景（临时仓库 2 条封存、`*.py` 口径） | 未改前 | 合并版 |
|---|---|---|
| 删掉整个文件 | `STALE`，红 | `claims 2→1`、失败 0、**✓** |
| 抹空 docstring 后 `--rebaseline` | 自清，绿 | `STALE` **清不掉**，且留一条幽灵封存 |

第一行是「删文件从红变绿」；第二行是死锁：同一个判据（「这次扫到的文件」）在读侧按
**glob 命中**算、在写侧按**产出行**算，于是抹空之后那条 STALE 既判得出来又清不掉——
罚一次、永久红。这正是用来否决合并方案的那个论证，只是换到了写侧。

#### 三件落地

1. `main()` 加 `claims == 0` 地板：空射程不是通过（glob 写错、目录改名、表与口径配错
   都会扫到 0 行，而「没有 failures ⇒ ✓」照样成立）。退出码 `1`。
2. `docs/number-provenance.analysis.json`：`analysis/*.py` 口径，**106 条全封存、0 锚定**，
   按「新增债务 106」入账。总债 `82 + 106 = 188`。
3. 两条测试：`test_each_registry_covers_its_own_scope` 钉 `(registry, globs)` 配对，
   `test_registry_files_are_in_scope_and_on_disk` 钉射程本身。

默认口径一个字节没动：`94 = 12 + 82`、`逐项核对 36/36`、`284` 分母、`EXIT 0`；
`docs/number-provenance.json` 的 sha256 前 16 位仍是 `236ff902b1487e53`。

#### 数怎么算的（每个数都带产地）

analysis 口径：**106 行 / 242 行分母 / 104 个键 / 2 组撞键 / 16 个产出文件**。行比键多 2，
因为 `analysis/_diag_l1_kstar_condition.py` 有两组同指纹各占 2 行；所以拿默认表去跑
analysis 口径时逐键报的是 **80 条 STALE**（82 条封存 折成 80 个键），不是 82。
那个组合下的失败构成是 `106 UNREGISTERED + 80 STALE + 12 STALE_ENTRY = 198`。

射程是**单层** `analysis/*.py`（73 个文件，今天无子目录 `.py`）。单层 glob 的风险是
「子目录里的证据被静默漏掉」，所以测试里钉了一条：`analysis/` 一旦出现 `*/*.py` 就红。
这不是洁癖——分母藏起来的 ✓ 与 13p 的 SKIP 是同一个东西。

**198 可复算，`104 STALE + 82 UNREGISTERED` 不可复算**——后者把两个桶对调了，又把行数
当成键数。这是本项目第三次「总数对、分项编」，判据是同一条：分项要能被同一份输入重算。

#### 不做的

- **不加 `--min-verified`。** `test_decision_number_provenance.py` 里 `anchored >= 12`
  已经是默认开的门；`entries` 被 rebaseline 吞成 frozen 时 `anchored` 掉到 0，它会红。
  再加一个默认 `None` 的旋钮，唯一效果是把「今天盲」变成「要记得传参」——而新克隆上
  36 项产物一个不在，传 `36` 必红、不传等于没加。
- **不把两份表并回去。** 省下的是一条命令，赔上的是上表第一行。

#### 留下的不变量

**每个登记表的每个 `file` 必须在它自己的 globs 命中内、且存在于盘上；射程缩小的唯一
可见形态是 `claims` 下降。** 这条比任何一次合并都值钱：合并、跳表、rebaseline 都只是
它的特例。它由测试看而不是由检查器看，因为 `--rebaseline` 会把「文件不在射程」这条
债务连着删掉——**写侧永远不能说「这条不算数」**。
