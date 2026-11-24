# API 延迟：根因与验收

> 2026-11-24。目标不是「把某个接口调快」，而是找出**一个根因放大成十几个超时**
> 的结构问题，并留下可重复的验收尺子。

## 怎么量的

两把尺子，都在真实运行环境（Windows + Docker Desktop，`data/` 走 bind mount）：

1. **生产账本** —— `app/request_metrics.py` 已按路由记录每次请求的耗时，
   落盘 `data/logs/requests-YYYY-MM-DD.jsonl`。判读：

   ```bash
   python -m app.request_metrics --date 2026-11-24 --top 30
   ```

   `route` 取的是 Starlette 匹配到的模板，不是原始路径，所以能按路由聚合。

2. **门禁脚本** —— `scripts/probe_api_latency.py`：33 个界面只读端点 × N 次采样，
   按「用户是否在等它」分三档预算（500 / 1200 / 8000ms），超预算非零退出。

   ```bash
   python scripts/probe_api_latency.py --runs 5 --json baseline.json
   ```

   改前跑一次、改后跑一次，同一台机器同一份数据 —— 这是仓库 §6.3 对性能改动
   的最低要求。

## 一个根因：全表枚举被放进了读热路径

`app/tickflow/partitions.py` 的 `list_partition_dates()` 对**每个**月分区
`scan_parquet` 读 `date` 列。`kline_daily` 自 2026-11 改成月分区后有 **590 个**
分区，容器内单次 **19.0s**。它在 31 处被调用，而其中若干处位于首屏/轮询路径：

| 调用点 | 症状 |
| --- | --- |
| `KlineRepository.latest_daily_date()` | `/api/kline/minute` 不传 `date` 时先问「最新日K是哪天」→ **18.9s** |
| `api/data.py` 的 `daily` + `adj_factor` 聚合 | `/api/data/status` 冷态 **25.4s**，顶穿前端 30s 超时 |
| `KlineRepository.latest_minute_date*()` | 周末/盘前打开个股图、非交易日回退 → 约 19s |
| 下游消费者（decision / market-recap / case-lab） | 连带 24~47s，见下表 |

**为什么它特别贵**：9p bind mount 上单次文件元数据往返约 0.5~35ms，成本只与
**文件数**成正比、与数据体积无关。`kline_index_daily` 只有 6.1MB，但 257 个分区
也要 2.16s —— 便宜的表一样会被文件数拖死。

### 修法（不改变口径）

优先复用仓库里已有的、声明为唯一取数点的缓存源，而不是再造一份：

- `latest_daily_date()` → `services.trading_days.kline_trading_days()`
  （进程内缓存 + 磁盘快照 + 内容指纹，稳态 0.02s，语义就是「kline_daily 覆盖的交易日」）；
- `api/data.py` 的 `daily` / `adj_factor` → 同一个函数（`_kline_coverage_days`）；
- `latest_minute_date_global()` → 读分区目录名（有分区即有数据，零 parquet 读取）；
- `latest_minute_date(symbol)` → 从最新分区往回探，逐文件确认该标的真有行
  （停牌股会缺行，不能只看目录），上限 15 个分区后回退全表；
- `_fallback_index_quotes_from_daily()` → 只把尾部 6 个分区交给 DuckDB，
  SQL 的窗口逻辑一字未改；尾部没覆盖到全部标的时回退全表（宁可慢不给错价）。

## 第二个根因：状态接口被轮询放大

单次 100ms 的接口被 2s 轮询，比单次 2s 的接口被点一次更贵。账本里一周：

| 端点 | 次数 | 中位耗时 | 真实成本 |
| --- | --- | --- | --- |
| `/api/pipeline/jobs` | 18869 | 133ms | `list_recent(1)` 要重读+重解析 49 个 JSON（549KB） |
| `/api/data/status` | 4099 | 648ms | 冷态 25s，暖态 4ms（TTL 300s 内） |
| `/api/intraday/indices` | 3782 | 142ms | 实时缓存为空时每次 2.16s |
| `/api/maintenance` | 1024 | 871ms | 每次请求调两遍 `list_recent` |

修法：

- `JobStore._job_files_sorted()` 按 `(mtime_ns, size)` 增量解析（`os.scandir`
  1.5ms 取全目录，只重解析变化的文件）。外部进程写入同样能识别 —— 判据是文件
  自身的签名，不是本进程的记账。
- `preferences.load()` 读路径去掉 `mkdir`、两次 `stat` 合成一次。
  **没有**加时间窗口合并：外部改 `preferences.json` 必须立即可见，
  `test_external_file_change_invalidates_cache` 守这条。
- `build_crowding_verdict()` 按数据目录缓存 30 分钟 —— 它对全部本地日线算一个
  静态统计（1530 万行 / 30.8s），下一批分区落盘前结论不会变。

## 改前 / 改后（同一台机器，`127.0.0.1` 直连，避开下面的握手坑）

| 端点 | 改前 | 改后 |
| --- | --- | --- |
| `/api/kline/minute?symbol=...`（不传 date） | 18.9s | 0.21s |
| `/api/data/status`（冷） | 25.4s | 0.57s |
| `/api/data/status`（暖） | 4.5ms | 5.2ms |
| `/api/intraday/indices` | 2.16s | 0.072s |
| `/api/maintenance` | 225ms | 77ms |
| `/api/pipeline/jobs?limit=1` | 112ms | 40ms |
| `/api/settings/preferences` | 103ms | 63ms |
| `/api/decision/score` | 24.7s | 0.09s |
| `/api/stock-analysis/delisting-countdown` | 44s | 0.22s |
| `/api/market-recap/dragon-tiger-verdict` | 44s | 2.7s |
| `/api/case-lab/scan` | 29s | 2.4s |
| `/api/stock-analysis/crowding-verdict` | 30.8s | 首次 30.8s / 之后 ~5ms |

门禁结果：**33 个端点 0 超预算**，中位最差 528ms（`/api/citizenship`）。

## 启动时间：56.5s 里有 50s 在挂载没人查询的视图（2026-11-24 深夜）

一条日志解释了整个启动：

```
21:05:07.991  sync route thread pool: 100
                     ← 56.56s 空白
21:06:04.551  duckdb views registered: 23 statements in 56.48s (workers=8)
```

| 阶段 | 耗时 |
| --- | --- |
| Python 导入 → `Started server process` | 7.86s |
| **视图注册** | **56.56s** |
| 之后全部（调度器 / 能力路由 / 各预热） | < 0.5s |

### 为什么 CREATE VIEW 这么贵

DuckDB 的 `CREATE VIEW` 是**当就绑定**：要把 `<dir>/**/*.parquet` 展开、并读每个
文件的 schema（`union_by_name=true` 逼它读全部）。容器内逐张实测：

| 视图 | 文件数 | 绑定耗时 | 全仓是否有人查询 |
| --- | --- | --- | --- |
| `kline_daily_enriched` | 5316 | **40.0s** | **否**（只在一条注释里出现） |
| `kline_daily` | 5240 | **14.7s** | 是（2 处） |
| `kline_5min` | 536 | 4.1s | 否 |
| `kline_etf_daily` / `etf_enriched` / `index_enriched` | 257 各 | 各 ~2.0s | 否 |
| `kline_index_daily` | 257 | 2.0s | 是（3 处） |
| `kline_minute` | 107 | 0.83s | 是 |
| `dragon_tiger_seats` | 249 | 0.57s | 否 |
| 其余 15 个 | 0~1 | < 0.1s | 多数否 |

统计全仓 `FROM/JOIN <view>` 后，**23 个视图里只有 7 个被真实引用**。也就是说
启动期一半以上的时间在挂载没人查询的表。

顺带修掉一处自相矛盾的注释：`_register_views` 写着"DuckDB 在 CREATE 时就绑定，
不是懒绑定"，而 `refresh_view` 写着"视图懒扫描，创建本身 O(1)" —— 后者是错的，
很可能就是没人质疑这 56s 的原因。

### 试过但无效的方案（都有实测）

- **持久化 DuckDB catalog**（`.duckdb` 文件）：重开 28ms，但首次查询仍要重新绑定
  **43.6s** —— 视图的绑定结果不落盘。
- **`union_by_name=false`**：40.4s → 36.8s，只省 9%（成本在读 footer，不在 union）。
- **`read_parquet(..., schema=...)`**：DuckDB 1.5.3 要求 map 的值是 struct，
  传 `'BIGINT'` 字符串直接报错，这条路不通。

### 实际做法：按需挂载 + 只预热确定会用且便宜的

1. 启动只登记 `name → DDL`（名字从 DDL 正则里取，不维护第二份清单）；
2. `execute_one/execute_all` 在跑 SQL 前扫一遍名字，命中未挂载的才 CREATE，
   并记 `view mounted on demand: <name> (<cost>)`；
   **挂载必须在拿 `_read_limiter` 之前**，否则一张 40s 的视图会把所有读堵住；
3. 建不出来（空目录）就**留在待办**，等数据落盘后自动补上；
4. 后台预热 5 张「首屏确定会用且便宜」的（`kline_index_daily` 3.48s + 四张字典表），
   贵的两张**故意不预热**。

### 结果

| | 改前 | 改后 |
| --- | --- | --- |
| 启动到 ready（干净重启） | **64s** | **6.6s** |
| 首屏 36 个端点 | 0 超预算 | 0 超预算，中位最差 184ms |

跑完全部 36 个端点后，日志里只有 5 张视图被挂载（就是预热那 5 张），
`kline_daily`(14.7s) 与 `kline_enriched`(40.0s) **全程未被创建** —— 它们服务的
路径本来就走收窄后的 `_scan_*_range`，视图是纯启动开销。

注意：如果将来有页面开始查这两张视图，第一次会付 14.7s / 40.0s。届时正确的
修法不是把它们加回预热，而是像其他热路径一样收窄文件范围（`_daily_scan_source`）。

## 仍然已知慢的地方（2026-11-24 深夜更新）

> 原列的三项已全部处理，见 `docs/caching-strategy.md` 的「执行记录」：
> `citizenship` 404.7ms→10.1ms（预序列化缓存）、`crowding-verdict` 35.4s→3.9ms
> （SWR + 结论物化，冷进程首次读 16ms）、`decision/overview` 287ms→130ms
> （账本签名缓存）。

- **`/api/market-recap/dragon-tiger` 246ms** → **18ms**（2026-11-24 深夜）。
  不是解析慢，是取数策略：当日榜单不落盘（`actual < today` 才写），所以收盘后到
  `pull_latest` 跑完这段窗口里每个请求都实时拉三榜 158ms/次；外加
  `_local_trading_days` 被调两遍。改法：`resolve_trade_date` 复用已取好的日历 +
  当日结果进 60s 进程内缓存（`source_unavailable` 也缓存）。
- **`/api/decision/slate` 100ms**：下一个值得拆的，与 `overview` 同源（也是多次
  读同一批账本）。
- **`gates.evaluate` 87ms**（`decision/overview` 的主体）：四条门禁判断「数据
  新不新鲜」，**缓存它们等于让门禁对陈旧数据放行**。这是设计约束，不是性能缺陷。
- **本地没有分钟数据的标的**：`/api/kline/minute` 会走 live 兜底，约 200ms/次，
  不可缓存。这是数据覆盖问题，不是性能问题。
- **写路径**继续付 19s 级开销（`indicators/pipeline`、`daily_pipeline`），那里要的是
  全表真值。注意读路径已经没有任何 `list_partition_dates(kline_daily)` 调用方，
  有 `tests/test_thin_api_scan_guard.py` 守着不许写回去。

## 环境坑：`localhost` 比 `127.0.0.1` 慢 200ms

容器只把端口发布在 IPv4（`0.0.0.0:3018`）。浏览器访问 `http://localhost:3018`
时会先试 `::1`，等它超时才回退：

```
curl http://localhost:3018/health      → conn=0.208s  total=0.215s
curl -4 http://127.0.0.1:3018/health   → conn=0.001s  total=0.009s
```

每个**新建连接**都要付这 200ms（页面首屏 6 条连接并行，重叠后约 200~300ms）。
这不属于后端代码，桌面版启动器本来就用的 `127.0.0.1`；Docker 部署时请把书签
写成 `http://127.0.0.1:3018`。

## 复现与回归

```bash
# 后端单测（按目录/文件跑，全量 collection 会触发 bulk-guard）
backend/.venv/Scripts/python.exe -m pytest \
  backend/tests/test_data_status_coverage_days.py \
  backend/tests/test_latest_daily_date.py \
  backend/tests/test_minute_latest_date_probe.py \
  backend/tests/test_index_quotes_tail_source.py \
  backend/tests/test_job_list_parse_cache.py \
  backend/tests/test_crowding_verdict.py -q

# 延迟门禁
python scripts/probe_api_latency.py --runs 5
```

每个修复都有「慢路径被禁时仍要出正确答案」的测试（把 `list_partition_dates`
或 `execute_one` 换成抛异常），这样性能回退会直接变红，而不是靠人记得数字。
