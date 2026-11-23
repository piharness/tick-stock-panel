"""按「广度缺口」补全 5 分钟历史：baostock 2020-01-02 起，含退市股。

为什么需要这个文件
------------------
本地 `data/kline_5min` 只有 532 天（2024-09-02 ~ 2026-11-16），且前 42 天是采集爬坡
（2024-09-02 仅 9 只，11-07 才 4,753 只），并且**只覆盖今天在册的 5,572 只**。
两处硬缺口：
  1. 2020-01-02 ~ 2024-11-06 共 1,134 个交易日，全市场无分钟数据；
  2. 306 只缺口标的（含 154 只退市股）完全无分钟数据。

东财（push2his）分钟只保留最近 1,536 根（≈32 天），拿不到历史 —— 实测不是猜测。
Tushare 历史分钟是独立权限 2,000 元/年。baostock 是唯一免费路径，实测 2020-01-02 起。

落盘位置为什么不放进 data/kline_5min
-----------------------------------
分区里多一个文件会让同一天被读两遍，且读取器不报错（本仓已知事故，见
`data/backfill_20261117/MANIFEST.md`）。所以单独 landing，下游显式 union。

口径
----
- `adjustflag="3"` 不复权原始价 = 本地日线 `raw_close`（实测 000001 2020-05-27 两边
  都是 13.000，完全同源）。本地 `kline_5min` 存的是**前复权价**（= 日线 `close`），
  与 baostock 原始价是两套口径，不能直接拼接。
  ⇒ 落盘统一存**原始价**，与项目「事实层存原始价」架构一致（见
  `backend/app/services/minute_adjust.py`），下游按需用 `adj_factor` 复权投影。
- baostock `volume` 单位是**股**，本地 `volume` 是**手**（重叠段实测比值 median=0.0100
  = 1/100）。落盘保留 baostock 原值，字段名 `volume_raw`，合并阶段再除 100。
- baostock `af=1`（前复权）价格被放大到千元级（000001 2020-05 显示 1533），
  `af=2`（后复权）与本地前复权算法不同源，两者都不可用于直接合并。
- **不请求 `amount` 字段**：实测 baostock 服务端在「amount + 大数据量（约 8 万根）」
  时会永久挂起（无响应），小数据量正常 —— 服务端慢查询问题。分钟级回测不依赖
  amount（volume 股 + close 可近似，本地日线也有 amount）。

三分类
------
缺一根分钟线有三种原因，混为一谈会让"服务端没返回"变成"当天没交易"：
  ok             拿到数据
  not_listed     该区间日线也无成交 ⇒ 未上市/已退市/长期停牌，正常
  unresolved     日线有成交但分钟为空 ⇒ **真缺口**，需要重试
  error          网络/解析异常，需重试

baostock 0.9.4 客户端 bug（已修复，见 data/_probe_t0/_fixed_bs.py）
-------------------------------------------------------------------
官方 baostock 的 `send_msg` 用 `receive[-13:] == b"<![CDATA[]]>\\n"` 判断读完，
但压缩响应（K线查询）的结尾是「\\1<crc>\\n<![CDATA[]]>\\n」且要按消息头里的
body 长度字段读。原版死等一个永不出现的标记 ⇒ K线查询永久挂起（login 非压缩能通）。
修正版 `FixedBaostock` 用「读 21 字节头 → 解析 body 长度 → 精确读 body → 读尾部
标记」的正确协议。**不要改回原生 baostock 的 query_history_k_data_plus。**

并发上限（实测，2026-11-19）
------------------------------
baostock 对**单 IP 并发会话数硬限 6**：开 12 个 client → 6 成功，开 16 个 → 同样 6 成功。
超过 6 的 client `login()` 返回 False，且失败者若继续当 worker 用（sock=None），
每个任务都会立刻抛 AttributeError 并被计成 `error` ⇒ 必须把失败 client 从池里剔除。
单连接吞吐实测 ~800 行/s（与行数线性：1 年 848 行/s、5 年 797 行/s），
6 连接 ⇒ ~4,800 行/s，现存股 5,572 只 × 56,304 根 ≈ 3.1 亿行 ⇒ **约 18 小时**。
不是「2 天」：2 天是 3 并发的外推，而 3 并发浪费了一半可用带宽。

排序为什么按事件密度（--order inplay）
------------------------------------
同样 18 小时，但先下「涨停日最多」的票，中断在任何时刻都能得到可用的研究样本。
实测覆盖曲线（2020-01-02~2024-11-06，92,800 个涨停只-日）：
  前 500 只 22.6% / 前 1,000 只 38.7% / 前 1,500 只 51.9% / 前 3,000 只 80.5%。
没有 80/20，但前 1,500 只 = 5 小时 ⇒ 半数事件已可下结论。

用法
----
    python analysis/backfill_bs_minute.py --list                 # 任务清单统计
    python analysis/backfill_bs_minute.py --status                # 进度/ETA/状态分布
    python analysis/backfill_bs_minute.py --verify               # 重叠段一致性验证
    python analysis/backfill_bs_minute.py --limit 5 --verbose     # 小批试跑
    python analysis/backfill_bs_minute.py --only-delisted         # 只补退市/缺口股
    python analysis/backfill_bs_minute.py --workers 6             # 现存股全量（~18h）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data/_probe_t0/_extlib"))

import polars as pl  # noqa: E402

ROOT = Path(os.environ.get("TSP_ROOT") or r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
OUT = DATA / "backfill_bs_minute"
LANDING = OUT / "f5"
STATE = OUT / "_state.jsonl"
MISSING_LOG = OUT / "_missing.parquet"

BS_START = "2020-01-02"
LOCAL_MIN_DATE = "2024-09-02"      # data/kline_5min 起点
LOCAL_FULL_DATE = "2024-11-07"     # 全市场覆盖起点（爬坡结束）
LAST_LOCAL = "2026-11-16"

RETRIES = 4
RECV_TIMEOUT = 60


# ---------------------------------------------------------------- 标的池

def local_symbols() -> list[str]:
    """本地 5min 里出现过、且今天是最后一个交易日仍在交易的标的。"""
    import os
    parts = sorted(os.listdir(DATA / "kline_5min"))
    last = [p for p in parts if p.startswith("date=")][-1]
    d = pl.read_parquet(DATA / "kline_5min" / last / "part.parquet", columns=["symbol"])
    return sorted(d["symbol"].unique().to_list())


def gap_symbols() -> list[str]:
    """306 只缺口标的（154 退市 + 100 ST + 52 其他），日线来自 backfill_20261117。"""
    g = pl.read_parquet(DATA / "backfill_20261117/stock_gap_daily.parquet", columns=["symbol"])
    return sorted(g["symbol"].unique().to_list())


def instrument_symbols() -> list[str]:
    """本地标的维表全量（5,893 只，含 155 只「退市××」）。"""
    p = DATA / "instruments" / "instruments.parquet"
    if not p.exists():
        return []
    return sorted(pl.read_parquet(p, columns=["symbol"])["symbol"].unique().to_list())


def universe_symbols() -> list[str]:
    """全窗口标的池 = 本地 5min 名录 ∪ 306 缺口股 ∪ 标的维表。

    为什么不能只用「最后一个 5min 分区的 symbol」：那是对**一天**的名录采样，
    不是对窗口的采样。当日停牌的存续股不在这一天的分区里，于是整只从任务表
    消失——它们既不在 `kline_daily_enriched`（按在册名单回溯填充），也不在
    306 缺口股里，没有任何一处会报错。实测漏 15 只，其中 6 只在 2020 后仍有
    交易（688798 艾为电子、600185 珠免集团、688121 *ST卓然、688496 *ST清越、
    002717 *ST岭南、300114 中航电测），另有 920309（2026-11-17 上市）。
    多带 15 只的代价只是 15 次空查询。
    """
    return sorted(set(local_symbols()) | set(gap_symbols()) | set(instrument_symbols()))


def tasks(only_delisted: bool = False, order: str = "inplay",
          seed: int = 0, include_bj: bool = False) -> list[dict]:
    """生成 (symbol, start, end) 任务。现存股只补缺段，缺口股补到其真实末根日。

    缺口股的 end 用 gap 日线的末根日（而非 2026-11-18），避免对已退市标的
    查询到未来的无效窗口 —— 这既省时，也减少服务端对无数据扫描的限流触发。
    """
    out = []
    gaps = set(gap_symbols())
    # 缺口股末根日（本地日线已知）
    gap_last = {}
    if gaps:
        g = pl.read_parquet(DATA / "backfill_20261117/stock_gap_daily.parquet",
                            columns=["symbol", "date"])
        gap_last = {r["symbol"]: str(r["date"]) for r in
                    g.group_by("symbol").agg(pl.col("date").max()).iter_rows(named=True)}
    if only_delisted:
        syms = sorted(gaps)
    else:
        syms = universe_symbols()
    skipped = 0
    bj = 0
    late = 0
    # 2024-11-06 之后才上市的票，在这个窗口内必然无数据：每只白跑一次请求。
    # 实测 170 只 ⇒ 170 次请求 + 数分钟。在「请求数可能计入配额」的前提下不该浪费。
    late_set: set[str] = set()
    inst_p = DATA / "instruments" / "instruments.parquet"
    if inst_p.exists():
        t = pl.read_parquet(inst_p, columns=["symbol", "listing_date"])
        late_set = set(t.filter(pl.col("listing_date").cast(pl.Utf8) > "2024-11-06")["symbol"].to_list())
    for s in syms:
        if s.endswith(".BJ") and not include_bj:
            # baostock 完全不认北交所：实测 bj/920211、bj/430047、bj/830799 三个段位
            # 全部返回 `10004011 股票代码`，而 sh/920211 能通（返回 0 行）。
            # 留着它们只会每只白等 20 秒重试并写 356 条 error。
            bj += 1
            continue
        if s in late_set and s not in gaps:
            late += 1
            continue
        if s in gaps:
            end = gap_last.get(s, "2026-11-18")
            # 末根早于 baostock 分钟起点（2020-01-02）→ 拿不到，直接跳过
            if end < BS_START:
                skipped += 1
                continue
        else:
            end = "2024-11-06"
        out.append({"symbol": s, "start": BS_START, "end": end})
    if skipped:
        print(f"  跳过 {skipped} 只（末根早于 {BS_START}，baostock 无分钟）")
    if bj:
        print(f"  跳过 {bj} 只北交所（baostock 不认 bj.* 代码，10004011）；"
              f"北交所 2020-2024 分钟线在本数据源缺失，属**已知口径边界**，不是遗漏")
    if late:
        print(f"  跳过 {late} 只（{BS_START} 窗口结束后才上市，窗口内必无数据）")
    return _order(out, order, seed)


def inplay_rank() -> dict[str, int]:
    """事件密度排名：2020-01-02~2024-11-06 涨停只-日数降序，同分按中位成交额。

    排序只影响「先下谁」，不影响最终集合；成本为零（5.3M 行扫描 0.1s）。
    """
    p = OUT / "_inplay.parquet"
    if not p.exists():
        q = (pl.scan_parquet(DATA / "archive/kline_daily_qfq/**/*.parquet",
                             extra_columns="ignore")
             .filter(pl.col("date").is_between(pl.lit("2020-01-02").str.to_date(),
                                               pl.lit("2024-11-06").str.to_date()))
             .select(["symbol", "date", "close", "amount"]).collect()
             .sort(["symbol", "date"])
             .with_columns(((pl.col("close") / pl.col("close").shift(1) - 1) * 100)
                           .over("symbol").alias("pct")))
        q.group_by("symbol").agg(
            n_lu=(pl.col("pct") >= 9.5).sum(),
            amt_med=pl.col("amount").median(),
        ).write_parquet(p)
    t = pl.read_parquet(p).sort(["n_lu", "amt_med"], descending=[True, True])
    return {s: i for i, s in enumerate(t["symbol"].to_list())}


def _order(tl: list[dict], order: str, seed: int = 0) -> list[dict]:
    """缺口股先（样本已齐，先保失败重跑），其余按 `order` 排。

    `random` 是**为了在只下了一部分时还能做总体推断**：按事件密度排序（inplay）
    会让"先下的那批"系统性地更活跃，用它估计全体封板率会偏高，而退市股那侧是
    全量、无选择的 ⇒ 两侧不可比。随机抽样则保持无偏，代价是要多下几只才有足够
    事件量。**抽样必须可复现**，所以 seed 进命令行并写进日志。
    """
    if order == "symbol":
        return sorted(tl, key=lambda t: t["symbol"])
    gaps = set(gap_symbols())
    if order == "random":
        import random
        rest = sorted([t for t in tl if t["symbol"] not in gaps],
                      key=lambda t: t["symbol"])
        random.Random(seed).shuffle(rest)
        head = [t for t in tl]
        return ([t for t in head if t["symbol"] in gaps] + rest)
    rank = inplay_rank()
    big = len(rank)

    def key(t):
        s = t["symbol"]
        return (-1, 0) if s in gaps else (0, rank.get(s, big))

    return sorted(tl, key=key)


def to_bs(symbol: str) -> str:
    code, mkt = symbol.split(".")
    return f"{mkt.lower()}.{code}"


# ---------------------------------------------------------------- 落盘

def atomic_write(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def load_state() -> dict[str, dict]:
    st = {}
    if STATE.exists():
        for line in STATE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            st[r["symbol"]] = r
    return st


def append_state(rec: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with STATE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def daily_calendar() -> list[str]:
    import os
    p = DATA / "kline_daily_enriched"
    if not p.exists():
        return []          # 分片机上没有本地日线：只影响空结果的分类，不影响下载
    parts = sorted(os.listdir(p))
    return [x[5:] for x in parts if x.startswith("date=")]


def symbol_daily_dates(symbol: str, cal: list[str], start: str, end: str) -> set[str]:
    """该票在 [start,end] 内**有成交**的交易日。用日线判定，避免把退市当缺口。"""
    ok = set()
    for d in cal:
        if d < start or d > end:
            continue
        p = DATA / "kline_daily_enriched" / f"date={d}" / "part.parquet"
        if not p.exists():
            continue
        t = pl.read_parquet(p, columns=["symbol", "volume"])
        if t.filter((pl.col("symbol") == symbol) & (pl.col("volume") > 0)).height:
            ok.add(d)
    return ok


# ---------------------------------------------------------------- 下载

# 修正版客户端：绕开 baostock 0.9.4 的 send_msg 死循环 bug。
# **延迟导入**：`--status / --collect / --audit-universe / --export-plan` 是纯本地
# 操作，分片机往往只拷了落地目录、没带客户端；模块级 import 会让这些命令直接崩
# （实测：TSP_ROOT 指向临时目录时 `--collect` 报 ModuleNotFoundError）。
def _bs_client():
    """按「TSP_ROOT/data/_probe_t0」找客户端，找不到再退回脚本所在仓库。

    为什么要有退回：分片机上 TSP_ROOT 通常指向落地目录，而客户端可能放在仓库树里。
    只认 DATA 一条路会让分片机报 ModuleNotFoundError，错误信息完全不指向真实原因。
    """
    for base in (DATA, Path(__file__).resolve().parent.parent / "data"):
        p = base / "_probe_t0"
        if (p / "_fixed_bs.py").exists():
            sys.path.insert(0, str(p))
            break
    from _fixed_bs import FixedBaostock
    return FixedBaostock()

#: 服务端封禁码。**这是整轮失效，不是单只失败**：2026-11-19 09:00 触发后，
#: 连 `login` 都返回它。此时继续跑只会把剩下几千只全部写成 error，且每秒都在加深封禁。
BLACKLIST_CODE = "10001011"

#: 一旦置位，所有 worker 立即退出（把任务放回队列，不写终态）。
ABORT = threading.Event()

#: 主动收工（今日行数达到预算）。与 ABORT 的区别：这是**计划内**停止，
#: 任务已成功、不该重跑，也不该被记成封禁。
QUOTA_STOP = threading.Event()


def _is_blacklist(text: str) -> bool:
    return BLACKLIST_CODE in (text or "") or "黑名单" in (text or "")



def fetch_5min(client: FixedBaostock, symbol: str, start: str, end: str,
               verbose: bool = False, per_page: int = 2000):
    """返回 (DataFrame, status, note)。DataFrame 未做单位换算，保留 baostock 原值。"""
    code = to_bs(symbol)
    last_exc = None
    for attempt in range(RETRIES):
        try:
            # 注意：不请求 amount 字段。实测 baostock 服务端在「amount + 大数据量
            # （约 8 万根）」时会永久挂起（无响应），但小数据量正常 —— 是服务端
            # 的慢查询/超时问题，不是客户端。amount 可用 volume×均价近似，或从
            # 本地日线补齐，分钟级回测不依赖它。
            r = client.query_k(code, "date,time,open,high,low,close,volume",
                               start, end, frequency="5", adjustflag="3",
                               per_page=per_page)
            if r["error_code"] != "0":
                last_exc = f"code={r['error_code']} msg={r['error_msg']}"
                if _is_blacklist(last_exc) or _is_blacklist(str(r.get("fields"))):
                    ABORT.set()
                    return None, "blacklist", last_exc
                if verbose:
                    print(f"    attempt{attempt} {symbol} {last_exc}", flush=True)
                time.sleep(2 * (attempt + 1))
                continue
            rows = r["rows"]
            if not rows:
                return None, "empty", "no rows"
            df = pl.DataFrame(
                rows,
                schema=["date", "time", "open", "high", "low", "close",
                        "volume"], orient="row")
            df = df.with_columns([
                pl.col("time").str.slice(0, 14).str.to_datetime("%Y%m%d%H%M%S").alias("datetime"),
            ]).with_columns([
                pl.col("open").cast(pl.Float64), pl.col("high").cast(pl.Float64),
                pl.col("low").cast(pl.Float64), pl.col("close").cast(pl.Float64),
                pl.col("volume").cast(pl.Float64).alias("volume_raw"),
            ]).select(["datetime", "open", "high", "low", "close", "volume_raw"]) \
              .sort("datetime")
            # 丢掉全空的占位行（停牌日 baostock 会返回空字符串价格）
            df = df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
            if df.height == 0:
                return None, "empty", "all placeholder rows"
            df = df.with_columns(pl.lit(symbol, dtype=pl.String).alias("symbol")) \
                   .select(["symbol", "datetime", "open", "high", "low", "close",
                            "volume_raw"])
            return df, "ok", f"{df.height} rows"
        except Exception as e:  # noqa: BLE001
            last_exc = f"{type(e).__name__}: {e}"
            if verbose:
                print(f"    attempt{attempt} {symbol} EXC {last_exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    return None, "error", str(last_exc)


# ---------------------------------------------------------------- 验证

def cmd_verify() -> None:
    """重叠段一致性：baostock 2024-11-07~2026-11-16 vs 本地 kline_5min。

    这是合并前的唯一硬关卡。若 OHLC 不能对齐到 0.001，说明两边不是同一口径，
    合并会污染历史。检验两只票：一只从未除权（000001 有分红但前复权后可比 raw），
    一只高价股。
    """
    import baostock as bs
    socket.setdefaulttimeout(RECV_TIMEOUT)
    client = _bs_client()
    assert client.login(), "baostock 登录失败"
    print("login ok", flush=True)

    for symbol in ["000001.SZ", "600519.SH"]:
        df, status, note = fetch_5min(client, symbol, "2024-11-07", "2024-11-15", verbose=True)
        print(f"\n[{symbol}] bs status={status} {note}", flush=True)
        if df is None:
            continue
        loc = pl.concat([
            pl.read_parquet(DATA / "kline_5min" / f"date={d}" / "part.parquet")
              .filter(pl.col("symbol") == symbol)
            for d in ["2024-11-07", "2024-11-08", "2024-11-11", "2024-11-12",
                      "2024-11-13", "2024-11-14", "2024-11-15"]
        ]).sort("datetime")
        j = loc.join(df, on="datetime", suffix="_bs")
        print(f"  本地 {loc.height} 根 / bs {df.height} 根 / 对齐 {j.height} 根", flush=True)
        if j.height == 0:
            print("  !! 时间轴对不上，检查时区/bar 标签口径", flush=True)
            continue
        for c in ["open", "high", "low", "close"]:
            d = (j[c] - j[c + "_bs"]).abs()
            print(f"  {c:<6} max|Δ|={d.max():.6f}  >0.001 占比={float((d > 0.001).mean()):.4f}",
                  flush=True)
        rv = (j["volume"] / j["volume_raw"])
        print(f"  volume 本地/bs 比值 median={rv.median():.4f} p1={rv.quantile(0.01):.4f} "
              f"p99={rv.quantile(0.99):.4f}  (0.01 = 本地手 / bs 股)", flush=True)
        # amount 不做对账: fetch_5min 不取该字段 (服务端在 amount + 大数据量时会
        # 永久挂起, 见该函数注释)。此前这里读 `amount_bs` 会 KeyError, 而它是唯一
        # 的口径复核入口 —— 坏了就没人能复核「两段是不是同一口径」。
    client.logout()


def cmd_probe() -> None:
    """单次探活：login + 一次 3 日小组查询。用来判断封禁是否解除。

    封禁期间两者都会返回 `10001011 黑名单用户`（login 也失败），所以一次 login
    就能定性；查询用来确认解封后取数正常。
    """
    socket.setdefaulttimeout(30)
    c = _bs_client()
    ok = c.login()
    print(f"login: {'ok' if ok else 'FAIL'}  {getattr(c, 'last_error', '')[:120]}")
    if not ok:
        if _is_blacklist(getattr(c, "last_error", "")):
            print("  ⇒ 仍在封禁（登录即被拒），等下一轮再试")
        else:
            print("  ⇒ 不是封禁，是连接/网络问题，先查网络再重试")
        return
    t0 = time.time()
    r = c.query_k("sh.600000", "date,time,close", "2024-11-04", "2024-11-06",
                  frequency="5", adjustflag="3")
    bad = _is_blacklist(str(r.get("error_msg"))) or r["error_code"] != "0"
    print(f"query: code={r['error_code']} rows={len(r['rows'])} "
          f"{time.time()-t0:.1f}s")
    if bad:
        print(f"  ⇒ login 通了但查询被拒：{str(r.get('error_msg'))[:80]}")
    else:
        print("  ⇒ 已解封，可以续跑")
    c.logout()


def _probe_once() -> bool:
    """返回是否可用（login + 一次小组查询都通过）。"""
    socket.setdefaulttimeout(30)
    c = _bs_client()
    if not c.login():
        return False
    try:
        r = c.query_k("sh.600000", "date,time,close", "2024-11-04", "2024-11-06",
                      frequency="5", adjustflag="3")
        return r["error_code"] == "0" and not _is_blacklist(str(r.get("error_msg")))
    finally:
        try:
            c.logout()
        except Exception:  # noqa: BLE001
            pass


def wait_unban(interval_min: int) -> None:
    """封禁期挂起：每 interval_min 分钟探活一次，解封即返回。

    为什么要这个：2026-11-19 那次封禁持续 ≥5 小时，而脚本当时每 20 秒
    试一次并写一条 error —— 4.5 小时里既没网也没进度。挂起等待是唯一不浪费的做法。
    """
    n = 0
    while not _probe_once():
        n += 1
        print(f"  [{time.strftime('%H:%M:%S')}] 仍封禁（第 {n} 次），"
              f"{interval_min} 分钟后重试", flush=True)
        time.sleep(interval_min * 60)
    print(f"  [{time.strftime('%H:%M:%S')}] 已解封，开始下载", flush=True)


# ---------------------------------------------------------------- 进度

def done_symbol(sym: str, st: dict[str, dict]) -> bool:
    """该 symbol 在窗口内是否已无待办（`--status` 与 main 共用同一判据）。

    「ok」不能只看 state：state 是 append-only 的记账，文件可能被删/写坏/迁移丢失。
    只看 state 会让这类票**永久跳过**，而 `--status` 只是打印一行提示、不修——
    于是覆盖率永远差这几只，且没人会重跑（`下满了但没用` 的一种）。

    换码股（300114 中航电测 → 302132 中航成飞）多一条：当前码在 baostock 侧
    2025 年才启用，2020-2024 段按当前码查恒空（unresolved），数据只能挂在历史码下。
    若不在这里认历史码，该票既永远 `unresolved`、又让「待跑」永远 >0 ⇒
    无人值守循环的终止条件 `待跑 0` 永不成立。minute_union 读取时会把历史码
    canonicalize 到当前码，所以这里认旧码不算漏。
    """
    r = st.get(sym, {})
    s_ = r.get("status")
    if s_ == "not_listed":
        return True
    if s_ == "ok":
        return (LANDING / f"{sym}.parquet").exists()
    from code_alias import historical_codes
    for h in historical_codes(sym):
        rh = st.get(h, {})
        if rh.get("status") == "ok" and (LANDING / f"{h}.parquet").exists():
            return True
    return False


def cmd_status() -> None:
    """进度自证：不靠 _state 的 ok，直接数 f5 文件 + 对账总任务数。

    「已完成」的定义是**文件在**且**状态为 ok/not_listed**；两者不一致的单独列出，
    因为历史上有过「state 写 ok 但文件没落盘」的窗口（进程被杀在两者之间）。
    """
    tl = tasks()
    st = load_state()
    files = {p.stem for p in LANDING.glob("*.parquet")}
    ok_st = {s for s, r in st.items() if r.get("status") in ("ok", "not_listed")}
    done = ok_st & files if files else set()
    pend = [t["symbol"] for t in tl if not done_symbol(t["symbol"], st)]
    # not_listed = 窗口内日线也无成交（未上市/撤回发行/换码旧码），本来就不该有文件。
    # 把它算进 ghost，「待跑 0」旁边就会永远挂一条「需重跑」——已被误读两次。
    no_data = {s for s, r in st.items() if r.get("status") == "not_listed"}
    ghost = sorted(s for s in ok_st - files if s not in no_data)
    orphan = sorted(files - ok_st)
    secs = [r.get("secs", 0) for r in st.values() if r.get("status") == "ok" and r.get("secs")]
    rows = sum(int(r.get("rows", 0) or 0) for r in st.values() if r.get("status") == "ok")

    print(f"任务总数      {len(tl)}")
    print(f"已完成(有文件) {len(done)}  ({len(done)/len(tl):.1%})")
    print(f"待跑          {len(pend)}")
    if ghost:
        print(f"!! state=ok 但文件缺失 {len(ghost)} 只（需重跑）: {ghost[:5]}")
    if no_data:
        print(f"   无文件属预期 {len(no_data)} 只（窗口内无成交/未上市/换码旧码）: {sorted(no_data)[:5]}")
    if orphan:
        print(f"!! 有文件但 state 未记 {len(orphan)} 只: {orphan[:5]}")
    if secs:
        med = sorted(secs)[len(secs) // 2]
        print(f"单只耗时 median={med:.1f}s  n={len(secs)}（缺口股口径，现存股更长）")
    # 行数口径 ETA：现存股每只 ~n_days×48 根，实测 ~800 行/s/连接 × 6 连接。
    # 按只数外推会低估，因为缺口股平均只有 820 天、现存股有 1,134 天。
    exp_rows = 0
    p = OUT / "_inplay.parquet"
    if p.exists() and pend:
        ip = {r["symbol"]: r["n_days"] for r in pl.read_parquet(p).iter_rows(named=True)}
        exp_rows = sum(ip.get(s, 1134) * 48 for s in pend)
        print(f"待跑预计行数  {exp_rows:,}（按日线交易日×48）")
        # 800 行/s/连接 是实测单连接吞吐。6 连接虽可登录，但实测 35 分钟即被封，
        # 所以「安全 ETA」只能按 3 连接给；6 连接那一行仅供对比，不要当作计划。
        print(f"ETA(3 连接, 实测安全) ≈ {exp_rows/(800*3)/3600:.1f} 小时")
        print(f"ETA(6 连接, 35min 后封禁) ≈ {exp_rows/(800*6)/3600:.1f} 小时（不可用）")
    print(f"累计行数      {rows:,}")
    # 今日行数是判断 H1（并发触发）还是 H2（日配额）的唯一读数：
    # 09:00 那次封禁发生在当日累计 20.9M 行（3 并发 9.1M + 6 并发 11.8M）。
    # 若 3 并发放到 20M 以上仍未被封 ⇒ H1，全量 ≈30 小时；在 20M 附近再封 ⇒ H2。
    from datetime import date, datetime
    t0 = datetime.combine(date.today(), datetime.min.time()).timestamp()
    today = [r for r in st.values()
             if r.get("ts", 0) >= t0 and r.get("status") == "ok"]
    if today:
        tr = sum(int(r.get("rows", 0) or 0) for r in today)
        print(f"今日已下      {tr:,} 行 / {len(today)} 只  "
              f"（参照：9.1M 无事 / 20.9M 已被封）")
    print(f"落地体积      {sum(p.stat().st_size for p in LANDING.glob('*.parquet'))/2**30:.2f} GiB")
    from collections import Counter
    from datetime import datetime
    print("状态分布      ", dict(Counter(r.get("status") for r in st.values())))
    bl = [r for r in st.values() if r.get("status") == "error" and _is_blacklist(str(r.get("note")))]
    if bl:
        ts = sorted(r["ts"] for r in bl)
        print(f"封禁期失败    {len(bl)} 条（首个 "
              f"{datetime.fromtimestamp(ts[0]):%H:%M:%S}，末个 "
              f"{datetime.fromtimestamp(ts[-1]):%H:%M:%S}）；这些会被重跑，不算终态")


# ---------------------------------------------------------------- 标的池审计

def cmd_audit_universe() -> None:
    """对账标的池：任务集合是否覆盖了「2020 年后交易过的全部股票」。

    这是本任务唯一的有效性判据。下满 5,800 只但池子是按今天在册名单建的，
    等于只研究了幸存者——而三份被证伪的分钟线研究正是要拿退市股去检验。
    """
    loc, gaps, ins = set(local_symbols()), set(gap_symbols()), set(instrument_symbols())
    uni = set(universe_symbols())
    tl = {t["symbol"] for t in tasks(order="symbol")}

    print(f"本地 5min 名录（{len(loc)}）  末个分区单日采样")
    print(f"缺口股        （{len(gaps)}）  154 退市 + 100 ST + 52 其他")
    print(f"标的维表      （{len(ins)}）  含 155 只「退市××」")
    print(f"并集          （{len(uni)}） / 任务 {len(tl)}（剔除末根早于 {BS_START}）")
    print(f"  ∪ - 本地名录 多出 {len(uni - loc)} 只（其中维表独有 {len(ins - loc - gaps)}）")

    inst = pl.read_parquet(DATA / "instruments/instruments.parquet",
                           columns=["symbol", "name", "listing_date"])
    only_inst = inst.filter(pl.col("symbol").is_in(sorted(ins - loc - gaps)))
    if only_inst.height:
        print("\n只被维表覆盖的标的（旧两路并集会整只漏掉）：")
        for r in only_inst.sort("listing_date").iter_rows(named=True):
            print(f"  {r['symbol']}  {r['name']:<8} 上市 {r['listing_date']}")

    # 缺口股的「末个交易日」分布 = 退市年份分布，用来证明池子不是幸存者名单
    g = pl.read_parquet(DATA / "backfill_20261117/stock_gap_daily.parquet",
                        columns=["symbol", "date"])
    last = g.group_by("symbol").agg(pl.col("date").max().alias("last"))
    dist = (last.with_columns(pl.col("last").dt.year().alias("y"))
            .group_by("y").len().sort("y"))
    tail = {r["y"]: r["len"] for r in dist.iter_rows(named=True) if r["y"] >= 2019}
    print(f"\n缺口股末个交易日 ≥2019 的只数（= 覆盖到的退市批次）：{tail}")

    # 独立佐证：财务/股本流水里的标的是否都在池内（它们含已退市公司）
    fs = DATA / "backfill_20261117/fin_shares.parquet"
    if fs.exists():
        fsyms = set(pl.read_parquet(fs, columns=["symbol"])["symbol"].unique().to_list())
        print(f"fin_shares 标的 {len(fsyms)}，不在任务池内 {len(fsyms - uni)}："
              f"{sorted(fsyms - uni)[:10]}")


# ---------------------------------------------------------------- PIT 池审计

_A_SHARE = None


def _is_a_share(bs_code: str) -> bool:
    """baostock 代码 → 是否 A 股。用它而不是「在不在本地维表里」做过滤：
    拿维表过滤等于先假设维表完整，而完整性正是要检验的对象。"""
    global _A_SHARE
    if _A_SHARE is None:
        import re
        _A_SHARE = re.compile(r"^(sh\.(60[0135]|68[89])\d{3}|sz\.(00[0123]|30[01])\d{3}"
                              r"|bj\.(4\d{2}|8\d{2}|92\d)\d{3})$")
    return bool(_A_SHARE.match(bs_code))


def cmd_audit_pit(n_days: int = 16) -> None:
    """用 baostock 的 PIT 名录取样，算出标的池**漏了谁**。

    为什么必须有这一步：本地三份清单（5min 名录、日K归档、标的维表）都可能
    被「今天在册名单」污染过，用它们互相审计是自证。`query_all_stock(day)`
    返回那一天真实有行情的全部代码，是唯一的外部基准。
    抽 16 个交易日取并集，任何在窗口内交易过、但不在池内的股票都会暴露。
    """
    cal = daily_calendar()
    cal = [d for d in cal if d >= "2020-01-02"]
    if not cal:
        print("交易日历为空")
        return
    step = max(1, len(cal) // n_days)
    picks = cal[::step][:n_days]

    socket.setdefaulttimeout(RECV_TIMEOUT)
    c = _bs_client()
    if not c.login():
        print(f"login 失败：{c.last_error[:120]}")
        return
    uni = set(universe_symbols())
    seen: dict[str, tuple[str, str]] = {}
    try:
        for d in picks:
            r = c.query_all(d)
            if r["error_code"] != "0":
                print(f"  {d} 查询失败 code={r['error_code']} {r['error_msg'][:60]}")
                if _is_blacklist(str(r["error_msg"])):
                    return
                continue
            n = 0
            for row in r["rows"]:
                code = row[0]
                if not _is_a_share(code):
                    continue
                mk, cd = code.split(".")
                sym = f"{cd}.{mk.upper()}"
                seen.setdefault(sym, (d, row[1] if len(row) > 1 else ""))
                n += 1
            print(f"  {d}  当日 A 股 {n} 只", flush=True)
    finally:
        c.logout()

    missing = sorted(set(seen) - uni)
    print(f"\n取样 {len(picks)} 个交易日，并集 {len(seen)} 只；"
          f"不在任务池内 {len(missing)} 只")
    for s in missing[:40]:
        d, name = seen[s]
        print(f"  {s}  {name:<10} 首次出现在 {d}")


def cmd_bench_page() -> None:
    """实测「每页行数」对吞吐的影响（不需要下载全量）。

    动机：单只 5 年 = 54,432 根，按 2,000/页需要 27 次往返。若服务端接受更大页，
    往返次数下降会直接抬高 rows/s。这是**唯一能在不改并发的前提下提速的旋钮**
    （加并发会触发封禁，见 MANIFEST）。
    判据：rows/s 与「返回行数是否仍为 2000 的整数倍」——若扩大页后 rows 恒等于 2000，
    说明服务端静默截断，此路不通。
    """
    socket.setdefaulttimeout(RECV_TIMEOUT)
    c = _bs_client()
    if not c.login():
        print(f"login 失败：{c.last_error[:120]}")
        return
    print(f"login ok；基准标的 sh.600000  2020-01-02 ~ 2020-12-31（约 11,664 根）")
    try:
        for pp in (2000, 5000, 10000):
            t0 = time.time()
            r = c.query_k("sh.600000", "date,time,close", "2020-01-02", "2020-12-31",
                          frequency="5", adjustflag="3", per_page=pp)
            el = time.time() - t0
            n = len(r["rows"])
            if r["error_code"] != "0":
                print(f"  per_page={pp:>5}  失败 {r['error_code']} {r['error_msg'][:50]}")
                if _is_blacklist(str(r["error_msg"])):
                    return
                continue
            print(f"  per_page={pp:>5}  {n:>6} 行 / {el:5.1f}s = {n/el:>6.0f} 行/s"
                  f"{'   ← 疑似被截断到 2000' if pp > 2000 and n % 2000 == 0 and n < 11000 else ''}")
    finally:
        c.logout()


# ---------------------------------------------------------------- 分片与汇总

def cmd_export_plan(dest: str, shards: int, order: str, seed: int,
                    include_bj: bool = False) -> None:
    """把任务切成 N 片，每片一个 JSON，供不同机器/不同出口 IP 并行跑。

    为什么这样切
    ------------
    服务端限制是**单 IP 6 个并发会话**（实测），所以吞吐只能靠"多条 IP"线性叠加。
    切法 `tl[i::N]` 确定性、均衡、不需要哈希：同一份 plan 重放结果完全一致，
    事后能证明"这片确实该是这些票"。

    分片机需要什么
    --------------
    只要三样：本脚本、`data/_probe_t0/`（修正版客户端 + baostock 源码）、polars。
    **不需要**本地日线/维表 —— 它们只用于把"空结果"分成 not_listed / unresolved，
    这个分类在汇总端由覆盖率审计重做（那边有 5313 个日线分区）。

    汇总方式
    --------
    每台机的落地目录各自独立（`f5/` 里一票一文件、文件名即代码），
    scp/rsync 回主机的不同目录，再 `--collect` 到统一 `f5/`。
    同 symbol 出现两次说明切分有重叠，`--collect` 会报冲突而不是覆盖。
    """
    tl = tasks(False, order, seed, include_bj)
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(shards):
        part = tl[i::shards]
        p = d / f"shard_{i:02d}.json"
        p.write_text(json.dumps(
            {"meta": {"order": order, "seed": seed, "shards": shards,
                      "shard": i, "n": len(part), "total": len(tl),
                      "bs_start": BS_START},
             "tasks": part}, ensure_ascii=False), encoding="utf-8")
        print(f"  {p.name}  {len(part):>5} 只")
    print(f"合计 {len(tl)} 只 → {shards} 片，目录 {d}")


def cmd_collect(srcs: list[str]) -> None:
    """把各分片机的 f5 落地汇总进主机的 `f5/`。

    冲突（同 symbol、不同行数）= 切分重叠或重复下载，必须暴露出来：
    静默覆盖会让"这片下过没有"永远查不清，而这正是本任务最容易出错的地方。
    """
    import shutil
    LANDING.mkdir(parents=True, exist_ok=True)
    added = dup = 0
    conflicts = []
    for s in srcs:
        p = Path(s)
        if not p.is_dir():
            print(f"  跳过（不是目录）{s}")
            continue
        for f in sorted(p.glob("*.parquet")):
            dst = LANDING / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
                added += 1
                d = pl.read_parquet(dst, columns=["datetime"])
                # 必须补一条 state：续跑只认 `_state.jsonl`，只把文件拷进来而不记账，
                # 主机会把刚汇总的票当成"没下过"重下一遍（且 `--status` 会报孤儿文件）。
                append_state({"symbol": f.stem, "start": BS_START, "end": "shard",
                              "ts": int(time.time()), "status": "ok",
                              "rows": d.height, "note": "collected from shard",
                              "first": str(d["datetime"][0]) if d.height else "",
                              "last": str(d["datetime"][-1]) if d.height else ""})
                continue
            a = pl.read_parquet(dst, columns=["datetime"]).height
            b = pl.read_parquet(f, columns=["datetime"]).height
            dup += 1
            if a != b:
                conflicts.append((f.name, a, b))
    print(f"新增 {added} 只，重复 {dup} 只，冲突 {len(conflicts)} 只")
    for c in conflicts[:10]:
        print(f"  冲突 {c[0]}: 主机 {c[1]} 根 vs 分片 {c[2]} 根")
    if conflicts:
        print("  ⇒ 切分重叠。不要直接删，先按 --export-plan 的 meta 核对分片规则")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--audit-universe", action="store_true")
    ap.add_argument("--audit-pit", nargs="?", type=int, const=16, default=None,
                    metavar="N", help="抽 N 个交易日的 baostock PIT 名录，算池子漏了谁")
    ap.add_argument("--bench-page", action="store_true",
                    help="实测每页行数对吞吐的影响（不解封就测不了）")
    ap.add_argument("--export-plan", default=None, metavar="DIR",
                    help="把任务切成 N 片写到 DIR（配合 --shards），供多机/多出口 IP 并行")
    ap.add_argument("--shards", type=int, default=0, help="分片数（配 --export-plan）")
    ap.add_argument("--plan", default=None, metavar="FILE",
                    help="按导出的分片 JSON 跑（分片机上不需要本地日线/维表）")
    ap.add_argument("--collect", nargs="+", default=None, metavar="DIR",
                    help="把各分片机的落地目录汇总进本机 f5/（冲突会报，不覆盖）")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-delisted", action="store_true")
    ap.add_argument("--order", default="inplay", choices=["inplay", "symbol", "random"],
                    help="下载顺序：inplay=按事件密度（先出可研究样本），random=无偏抽样")
    ap.add_argument("--seed", type=int, default=0, help="--order random 的随机种子")
    ap.add_argument("--include-bj", action="store_true",
                    help="保留北交所（默认排除：baostock 对 bj.* 一律返回 10004011）")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--force", action="store_true", help="已完成的也重下")
    ap.add_argument("--workers", type=int, default=6,
                    help="并发 worker 数（默认 6；服务端单 IP 硬限 6，且 6 并发约 35 分钟就会封禁，"
                         "稳定跑请用 3）")
    ap.add_argument("--wait-unban", type=int, default=0, metavar="MIN",
                    help="开跑前先探活，封禁则每 MIN 分钟重试直到解封（0=不等）")
    ap.add_argument("--per-page", type=int, default=2000,
                    help="每次往返请求的行数。默认 2000（客户端默认）。先跑 --bench-page "
                         "看服务端是否接受更大页；被静默截断时调大不会变慢也不会变快")
    ap.add_argument("--max-rows", type=int, default=0, metavar="N",
                    help="今日行数达到 N 就主动收工（0=不限）。用来避免撞上日配额被封："
                         "被封会赔掉当天剩下的全部时间，主动停只损失几分钟")
    args = ap.parse_args()

    if args.status:
        cmd_status()
        return
    if args.audit_universe:
        cmd_audit_universe()
        return
    if args.audit_pit:
        cmd_audit_pit(args.audit_pit)
        return
    if args.bench_page:
        cmd_bench_page()
        return
    if args.export_plan:
        cmd_export_plan(args.export_plan, args.shards or 1, args.order, args.seed,
                        args.include_bj)
        return
    if args.collect:
        cmd_collect(args.collect)
        return
    if args.probe:
        cmd_probe()
        return
    if args.verify:
        cmd_verify()
        return

    if args.plan:
        tj = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        tl = tj["tasks"]
        print(f"plan {Path(args.plan).name}: {tj.get('meta')}", flush=True)
    else:
        tl = tasks(args.only_delisted, args.order, args.seed, args.include_bj)
    if args.start:
        for t in tl:
            t["start"] = args.start
    if args.end:
        for t in tl:
            t["end"] = args.end

    if args.list:
        gaps = set(gap_symbols())
        print(f"任务 {len(tl)} 只（{len([t for t in tl if t['symbol'] in gaps])} 缺口股 / "
              f"{len([t for t in tl if t['symbol'] not in gaps])} 现存股）")
        print(f"现存股区间 {BS_START} ~ 2024-11-06（本地已有 2024-09-02 起，将去重）")
        print(f"缺口股区间 {BS_START} ~ 2026-11-18")
        print(f"落地 {LANDING}")
        return

    st = load_state()
    cal = daily_calendar()

    # 今日预算的起点：**必须把本进程启动前已经下过的算进去**。
    # 否则封禁解除当天再起一个进程，计数器从 0 开始，会叠加到已用额度上直接撞线
    # ——那正是 --max-rows 想避免的事。
    budget0 = 0
    if args.max_rows:
        from datetime import date, datetime
        t0 = datetime.combine(date.today(), datetime.min.time()).timestamp()
        budget0 = sum(int(r.get("rows", 0) or 0) for r in st.values()
                      if r.get("ts", 0) >= t0 and r.get("status") == "ok")
        left = max(args.max_rows - budget0, 0)
        print(f"今日已下 {budget0:,} 行；预算 {args.max_rows:,}，剩余 {left:,} 行", flush=True)
        if left <= 0:
            print("今日预算已用完，明天再跑（同一条命令）", flush=True)
            return

    if not args.force:
        # 判据与 cmd_status 共用 done_symbol()：state=ok 但文件缺失要重跑；
        # 换码股认历史码（否则 302132 永远 unresolved，「待跑 0」永不成立）。
        tl = [t for t in tl if not done_symbol(t["symbol"], st)]
    if args.limit:
        tl = tl[:args.limit]
    print(f"待跑 {len(tl)} 只", flush=True)
    if not tl:
        return
    if args.wait_unban:
        wait_unban(args.wait_unban)

    socket.setdefaulttimeout(RECV_TIMEOUT)
    workers = args.workers

    # 每个 worker 独立 login（baostock socket 不跨线程共享）。
    # 实测单 IP 硬限 6 会话：超过 6 的 client login 必失败，必须从池里剔除，
    # 否则它会对每个任务抛 AttributeError(sock=None) 并把任务误标成 error。
    import threading
    clients = [_bs_client() for _ in range(workers)]
    lock = threading.Lock()
    login_ok = []

    def init_client(c):
        if c.login():
            with lock:
                login_ok.append(c)

    threads = [threading.Thread(target=init_client, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pool = login_ok
    if not pool:
        err = getattr(clients[0], "last_error", "") if clients else ""
        print(f"login 全部失败：{err[:140]}", flush=True)
        return
    if len(pool) < workers:
        print(f"login ok x{len(pool)} / 请求 {workers}（服务端单 IP 上限 6，多余的丢弃）",
              flush=True)
    else:
        print(f"login ok x{len(pool)}", flush=True)

    def ensure(c) -> bool:
        """任务前确认连接可用；断了就重连（服务端限流会静默关连接）。"""
        if c.sock is not None:
            return True
        for _ in range(3):
            if c.login():
                return True
            time.sleep(5)
        return False

    # 并发 worker：从任务队列取任务，每个 worker 持自己的 client
    t_all = time.time()
    done = [0]
    rows_done = [0]
    lock = threading.Lock()
    from queue import Empty, Queue
    q = Queue()
    for t in tl:
        q.put({**t, "tries": 0})

    def worker(c, wid):
        while not ABORT.is_set() and not QUOTA_STOP.is_set():
            try:
                t = q.get_nowait()
            except Empty:
                return
            if not ensure(c):
                q.put(t)          # 连接彻底不可用：放回任务，本 worker 退出
                ABORT.set()       # 连不上多半也是封禁，整体停比逐个重试更省时间
                print(f"  (w{wid}) 连接不可用，退出", flush=True)
                return
            sym, start, end = t["symbol"], t["start"], t["end"]
            dest = LANDING / f"{sym}.parquet"
            rec = {"symbol": sym, "start": start, "end": end, "ts": int(time.time())}
            if dest.exists() and not args.force:
                have = pl.read_parquet(dest, columns=["datetime"])
                rec.update(status="ok", rows=have.height, note="cached")
            else:
                t0 = time.time()
                try:
                    df, status, note = fetch_5min(c, sym, start, end, args.verbose,
                                                  per_page=args.per_page)
                except Exception as e:  # noqa: BLE001
                    # 传输层崩了：重连后重排队，最多 3 次，不把瞬时故障记成终态
                    if t["tries"] < 3:
                        c.logout()
                        q.put({**t, "tries": t["tries"] + 1})
                        print(f"  {sym} 重排队({t['tries']+1}) {type(e).__name__}", flush=True)
                        continue
                    df, status, note = None, "error", f"{type(e).__name__}: {e}"
                if status == "ok":
                    atomic_write(df, dest)
                    rec.update(status="ok", rows=df.height, note=note,
                               first=str(df["datetime"][0]), last=str(df["datetime"][-1]),
                               secs=round(time.time() - t0, 1))
                elif status == "blacklist":
                    # 不写终态、不计数：封禁解除后这只票还要重跑
                    q.put(t)
                    ABORT.set()
                    print(f"  !! 服务端封禁（{note}），停止所有 worker", flush=True)
                    return
                elif status == "empty":
                    if not cal:
                        # 分片机没有本地日线 ⇒ 无法区分"未上市/已退市"与"真缺口"。
                        # 记成 empty 而不是 not_listed：not_listed 会被当成终态跳过，
                        # 而这里只是"还不知道"。汇总端由覆盖率审计按只-日重判。
                        rec.update(status="empty", rows=0, note="分片机无本地日线，未分类",
                                   secs=round(time.time() - t0, 1))
                    else:
                        dts = symbol_daily_dates(sym, cal, start, end)
                        if dts:
                            rec.update(status="unresolved", rows=0,
                                       note=f"日线有 {len(dts)} 天却无分钟",
                                       secs=round(time.time() - t0, 1))
                        else:
                            rec.update(status="not_listed", rows=0, note="日线同期也无成交",
                                       secs=round(time.time() - t0, 1))
                else:
                    rec.update(status="error", rows=0, note=note,
                               secs=round(time.time() - t0, 1))
                time.sleep(1.0)  # 限流保护
            with lock:
                append_state(rec)
                done[0] += 1
                rows_done[0] += int(rec.get("rows", 0) or 0)
                n = done[0]
                rd = rows_done[0]
                if args.max_rows and budget0 + rd >= args.max_rows:
                    QUOTA_STOP.set()
            if n % 20 == 0 or rec.get("status") != "ok":
                el = time.time() - t_all
                rate = rd / max(el, 1e-9)
                eta_min = (len(tl) - n) * (el / max(n, 1)) / 60
                print(f"  [{n}/{len(tl)}] {sym} {rec.get('status')} rows={rec.get('rows', 0)} "
                      f"{rec.get('secs', 0)}s  {rate:.0f}行/s  累计 {el/60:.1f}min  "
                      f"ETA {eta_min:.0f}min  (w{wid})", flush=True)

    wthreads = [threading.Thread(target=worker, args=(c, i)) for i, c in enumerate(pool)]
    for t in wthreads:
        t.start()
    for t in wthreads:
        t.join()

    for c in pool:
        c.logout()
    print(f"已处理 {done[0]} / {len(tl)} 只，耗时 {(time.time()-t_all)/60:.1f}min", flush=True)
    if ABORT.is_set():
        print("!! 本轮提前中止（服务端封禁或连接不可用）。已完成的不受影响，"
              "重跑同一条命令即从断点继续。先用 --probe 确认解封。", flush=True)
    if QUOTA_STOP.is_set():
        print(f"== 今日行数达到预算 {args.max_rows:,}（实下 {rows_done[0]:,} 行），主动收工。"
              f"任务已记账，同一条命令可直接续跑。", flush=True)

    # 缺口汇总
    final = load_state()
    rows = []
    for sym, r in final.items():
        if r.get("status") in ("unresolved", "error"):
            rows.append({"symbol": sym, "status": r["status"], "note": r.get("note", "")})
    if rows:
        pl.DataFrame(rows).write_parquet(MISSING_LOG)
        print(f"未解决 {len(rows)} 只 → {MISSING_LOG}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
