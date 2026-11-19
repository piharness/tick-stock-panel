# 全样本日K：把退市股接进查询路径

> 目标不是「多一份数据」，是让任何横截面统计不再默认「所有股票都活到今天」。

## 1. 缺的是什么

`kline_daily_enriched` 是**按今天在册名单回溯填充**的行情库：5,573 只标的全部
活到最新交易日，没有一只在 2025 年及以前停止交易。名录 `instruments` 有 5,893 只，
差的 312 只：

| 类别 | 只数 | 例子 |
|---|---|---|
| 含「退」 | 154 | 康得退、中弘退、国华退 |
| ST | 100 | *ST卓然、*ST清越 |
| 吸收合并/改名 | 58 | 美的电器、招商地产、小天鹅Ａ、宏源证券 |

其中 306 只有日K，落在 `data/backfill_20261117/stock_gap_daily.parquet`
（1,193,364 行 / 1990-12-19 ~ 2026-10-27 / 原始价）。剩下 6 只是维表残留旧代码
（`000022 深赤湾Ａ` 已换码、`002525 胜景山河` IPO 被否从未上市）。

## 2. 为什么不能塞进分区

`kline_daily_enriched/date=*/` 里多一个 parquet 文件，会让同一天被读两遍，
**且读取器不报错** —— 本仓已知事故。所以只能显式 union，不能合并文件。

## 3. 口径：两套视图，不能互相替代

| 视图 | 主表行 | 缺口股行 | 用途 |
|---|---|---|---|
| `kline_stock_all` | `price_basis='qfq'`（前复权） | `price_basis='raw'` | 默认入口；日内比值不受基准影响 |
| `kline_stock_all_raw` | `price_basis='raw'` | `price_basis='raw'` | 严格同口径 + 剔除权日的复检 |

Python 侧对应四个方法（`app.tickflow.repository.KlineRepository`）：

```python
repo.get_daily_all(symbol, start, end)          # 单票，混合口径
repo.get_daily_batch_all(symbols, start, end)   # 批量，混合口径
repo.get_daily_all_raw(symbol, start, end)      # 单票，统一原始价
repo.get_daily_batch_all_raw(symbols, start, end)
```

返回 10 列：`symbol, date, open, high, low, close, volume, amount, src, price_basis`。
`src ∈ {main, gap}`，`price_basis ∈ {qfq, raw}`。

### 为什么缺口股只能是 raw

退市股没有分红/送转记录：东财 `RPT_SHAREBONUS_DET` 对 306 只全部返回空，
`adj_factor` 覆盖 **0/306**。没有除权事件就算不出前复权。

代价方向固定：除权日会显示成假跌，**压低**退市股收益。推论（来自
`data/backfill_bs_minute/NEXT.md`）：若复检结论仍是负的，缺分红只会让它更负；
若发现**正的**效应，那是被低估的（稳健）。

**绝不伪造一个"看起来统一"的复权 close** —— 那会让除权日变成无人察觉的假跌，
比缺失更贵。

## 4. 正确用法

```sql
-- 全市场等权日收益（含退市股），日历必须裁到主表覆盖区间
WITH cal AS (
  SELECT date, row_number() OVER (ORDER BY date) rn
  FROM (SELECT DISTINCT date FROM kline_stock_all WHERE src = 'main')
),
p AS (
  SELECT symbol, date, close,
         lead(close) OVER (PARTITION BY symbol ORDER BY date) nclose,
         lead(date)  OVER (PARTITION BY symbol ORDER BY date) ndate
  FROM kline_stock_all WHERE close > 0
)
SELECT avg(p.nclose / p.close - 1)
FROM p JOIN cal c ON p.date = c.date JOIN cal cn ON p.ndate = cn.date
WHERE c.rn + 1 = cn.rn;
```

两条硬约束：

1. **`date` 与 `lag(date)` 必须日历相邻**（`rn` 差 1）。退市整理期长期停牌后
   一次性复牌，按行 `lag()` 会把累计跌幅记成单日 −90%。
2. **日历以主表为准**。`kline_stock_all` 的日期到 1990 年，但 2005 年前只有
   缺口股、没有主表 —— 直接用全样本日历做横截面，早期每一天的「前 20 名」
   全是退市股，结果不可比。

## 5. 已知边界

### 5.1 DuckDB `union_by_name` + 窗口函数会崩

`kline_enriched` 视图（`union_by_name=true`）一旦参与 `UNION ALL` 且外层带
窗口函数，DuckDB 稳定抛 `TProtocolException: Invalid data`（threads=1/2/4/8
均复现；去掉 `union_by_name` 立刻正常）。

所以两张全样本视图都**直读 parquet**，不引用 `kline_enriched`。
同理，做窗口分析时**不要**用 `kline_enriched`，用 `kline_stock_all`。

这是既有缺陷，不是本次引入：`SELECT ... lag() ... FROM kline_enriched` 在本改动
之前同样会崩。生产链路走 Polars（`compute_all`），不受影响。

### 5.2 性能

单票跨 117 个交易日要开 117 个分区文件，约 **60~100 ms**（`get_daily` 的
7 ms 是因为命中了内存里的 300 天 enriched 缓存）。这是分区粒度的物理代价，
不是实现问题。

**不要把 `get_daily_all` 放进行情轮询/前端热路径** —— 它面向研究，不面向逐 tick。

### 5.3 缺口股没有的列

`turnover_rate`、`consecutive_limit_ups/downs`、`quote_ts` 在缺口股行上是 NULL。
不填：换手率要时点流通股本（`fin_shares` 有，但按 as-of 前推是另一件事），
连板数依赖涨跌停规则，而「5% 涨跌幅指纹」实测 precision .448 / recall .423，
不能用来造数据。

## 6. 验收

`analysis/_verify_l1_survivorship.py` 是独立基准（直读 parquet，不走视图）：

| 口径 | 决策日 | 笔数 | 日等权 | t |
|---|---|---|---|---|
| 基线（仅现存股） | 5,291 | 105,441 | +94.74 bp | 26.6 |
| 补全（+306 退市股） | 5,311 | 105,898 | **+95.61 bp** | 26.9 |
| `kline_stock_all`（本改动） | 5,312 | 105,918 | **+95.56 bp** | 26.9 |
| `kline_stock_all_raw` | 5,312 | 105,957 | +94.40 bp | 26.6 |

视图与独立脚本差 0.05 bp（1 个交易日的数据边界），口径一致。

顺带证伪了一条旧假设：脚本 docstring 写「退市股永远不会进入 mom20 前 20」，
实测 **9,085 次**（占 8.6%）。L1 免疫不是因为退市股进不来，是因为它们进来后
收益被均值吸收。

## 7. 测试

`backend/tests/test_delisted_source.py`（7 例）：

- `price_basis` 区分 qfq/raw，主表 close 不被降级
- `raw_open = open × raw_close / close` 精确可逆，且落在当日原始高低之间
- union 后 `(symbol, date)` 零重复（双计是 union 最贵的失败模式）
- 补全包缺失时优雅退化为只读主表
- 日期窗口与列裁剪生效

跑法：

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_delisted_source.py -q
```
