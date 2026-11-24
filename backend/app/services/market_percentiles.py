"""全市场横截面分位 —— 单票落在「今天的市场」什么位置。

三个指标都从**同一批分区**算出，一次读盘。分开读三次会让页面请求多付两倍的
9p 元数据开销（data/ 是网络挂载，按文件数计费）。

    turnover_pct  近 N 日平均换手率的分位
    vol_pct       近 N 日年化波动率的分位
    rev_pct       近 N 日区间涨幅的分位

为什么用**分位**而不是绝对值：换手率、波动率的中枢跨年份差一倍以上
（2015 与 2024 不可比）。分位回答的是"相对同期其他票有多极端"，这才是页面
要回答的问题；绝对值只能回答"高不高"，而"高"的标准每年都在变。

缓存：键含当日日期，同一天内复用。跨日自动失效 —— 不需要手工清理。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 20

_cache: dict[str, tuple[float, dict]] = {}


def _round(x: float, n: int = 4) -> float:
    return round(float(x), n)


def market_percentiles(repo, window: int = DEFAULT_WINDOW) -> dict[str, dict]:
    """返回 {symbol: {turnover_pct, vol_pct, rev_pct, to_avg, ann_vol, ret}}。

    任何一项算不出来就给 None，不给 0 —— 0 分位是"最温和的票"，
    与"没数据"是相反的含义。
    """
    data_dir = Path(repo.store.data_dir)
    base = data_dir / "kline_daily_enriched"
    if not base.exists():
        return {}
    key = f"mkt_pct:{base}:{window}:{cn_today().isoformat()}"
    hit = _cache.get(key)
    if hit:
        return hit[1]

    # **要 window+1 个分区**：N 个收盘价只给出 N-1 个交易日的涨跌幅。
    # 早先取 window 个，算出来的是 19 日收益；恰好被切掉的那一天若是大跌
    # （实测宁德时代 2026-10-20 单日 -12%），分位会从低端直接翻到高端。
    dirs = sorted([d for d in base.iterdir() if d.is_dir() and d.name.startswith("date=")])[
        -(window + 1):
    ]
    if len(dirs) < 6:
        return {}
    frames = []
    for d in dirs:
        f = d / "part.parquet"
        if not f.exists():
            continue
        try:
            frames.append(pl.read_parquet(f, columns=["symbol", "date", "close", "turnover_rate"]))
        except Exception as e:  # noqa: BLE001 - 单个坏分区不该让整块不可用
            logger.debug("market_percentiles 跳过 %s: %s", f, e)
    if not frames:
        return {}

    df = pl.concat([f for f in frames if not f.is_empty()], how="diagonal_relaxed")
    df = df.filter(pl.col("close").is_finite() & (pl.col("close") > 0))

    # **必须显式 sort_by("date")**：`group_by` 不保证组内行序，`first()/last()`
    # 会取到随机两端。实测由此得出的 20 日涨幅中位数是 -8.6%、只有 19% 的票为正
    # （同期市场其实是普涨），并且 `diff().std()` 也被放大成两倍方差。这类错误
    # 不会抛异常，只会让所有分位悄悄错位。
    cs = pl.col("close").sort_by("date")
    agg = df.group_by("symbol").agg(
        pl.col("turnover_rate").mean().alias("to_avg"),
        (cs.last() / cs.first() - 1).alias("ret"),
        # 日收益标准差 → 年化（这些票在这一窗口内的交易日数相同，不用按票归一）
        cs.pct_change().std().alias("sd"),
    )
    agg = agg.with_columns((pl.col("sd") * (252 ** 0.5)).alias("ann_vol"))

    def pct(col: str) -> pl.Expr:
        return (pl.col(col).rank("average") / pl.col(col).is_not_null().sum()).alias(f"{col}_pct")

    agg = agg.with_columns([pct("to_avg"), pct("ann_vol"), pct("ret")])

    out: dict[str, dict] = {}
    for r in agg.to_dicts():
        out[r["symbol"]] = {
            "turnover_pct": _round(r["to_avg_pct"]) if r.get("to_avg_pct") is not None else None,
            "vol_pct": _round(r["ann_vol_pct"]) if r.get("ann_vol_pct") is not None else None,
            "rev_pct": _round(r["ret_pct"]) if r.get("ret_pct") is not None else None,
            "to_avg": _round(r["to_avg"], 2) if r.get("to_avg") is not None else None,
            "ann_vol": _round(r["ann_vol"], 4) if r.get("ann_vol") is not None else None,
            "ret": _round(r["ret"], 4) if r.get("ret") is not None else None,
        }
    _cache[key] = (0.0, out)
    logger.debug("market_percentiles 计算完成 window=%d symbols=%d", window, len(out))
    return out


def turnover_pct(repo, window: int = DEFAULT_WINDOW) -> dict[str, float]:
    """只要换手率分位时的薄包装（股东象限用）。"""
    return {
        s: v["turnover_pct"]
        for s, v in market_percentiles(repo, window).items()
        if v.get("turnover_pct") is not None
    }


#: 拥挤度判决的进程内缓存。键 = data_dir。
#:
#: 为什么可以按目录缓存: 这个判决是对**全部本地日线**算的一个静态统计 (换手率
#: 分组 → 后续 20 日超额中位), 只在下一批 enriched 分区落盘时才变。而它每次
#: 要对 5316 个分区、1530 万行做窗口 + 分组, 容器内实测 **30.8s**
#: (`/api/stock-analysis/crowding-verdict` 账本均值 37s, 用户是「点开常识页
#: 然后等半分钟」)。
#:
#: ## 为什么是 stale-while-revalidate 而不是 TTL 或任务化
#:
#: 早期版本用 30 分钟 TTL: **每半小时就有一个倒霉的请求同步等 35s** —— 这等于
#: 把一次等待变成每天四十八次。任务化(202 + 轮询)能解决阻塞, 但它要求前端多一套
#: 状态机, 而这里不需要: 判决是静态结论, 晚一天看到没有代价。
#:
#: 现在: 命中就返回(哪怕"过期"); 数据写入时只**打个脏标记**; 下一次读发现是脏的,
#: 返回旧值并顺手起一个后台重算。于是
#:   - 首次(进程内还没有值)才同步算一次, 由启动预热兜住;
#:   - 之后任何时刻读都不阻塞, 最坏是看到昨天的结论。
#: 这与仓库对"新鲜度"判据的分工一致: 会驱动「要不要再更新」的判定不许陈旧,
#: 而展示型的静态统计允许落后一个批次。
_CROWDING_CACHE: dict[str, dict] = {}
_CROWDING_STALE: dict[str, bool] = {}
_CROWDING_LOCK = threading.Lock()
_CROWDING_BUILDING: set[str] = set()

#: 兜底 TTL: 脏标记靠写入方通知, 若有外部进程绕过仓库写 enriched (宿主脚本),
#: 标记不会被打上。超过这个时长即便没脏也重算一次。
_CROWDING_MAX_AGE_S = 24 * 3600.0
_CROWDING_BUILT_AT: dict[str, float] = {}

#: 结论物化的磁盘快照。**这是"冷启动也不再等 30.8s"的关键**：
#: 进程内缓存只在本进程活着时有意义，重启后第一次读仍要同步算一遍；启动预热
#: 只是抢在用户之前，进程刚起的那几秒里打开页面仍会撞上。把结论落盘后，
#: 冷进程的一次读就是一次 1KB 的 JSON 读取。
#:
#: 放 `_cache/` 而不是业务目录：不进任何 `date=*` / `**/*.parquet` 扫描。
_CROWDING_SNAPSHOT_VERSION = 1
_CROWDING_SNAPSHOT_NAME = "crowding_verdict.json"


def _crowding_snapshot_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "_cache" / _CROWDING_SNAPSHOT_NAME


def _read_crowding_snapshot(data_dir: Path | str) -> tuple[dict, float] | None:
    """返回 (结论, 距今秒数)；读不出/版本不符返回 None（调用方回退到同步算）。"""
    path = _crowding_snapshot_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("version") != _CROWDING_SNAPSHOT_VERSION:
        return None
    verdict = payload.get("verdict")
    if not isinstance(verdict, dict):
        return None
    try:
        built_at = float(payload.get("built_at_epoch") or 0.0)
    except (TypeError, ValueError):
        return None
    return verdict, max(0.0, time.time() - built_at)


def _write_crowding_snapshot(data_dir: Path | str, verdict: dict) -> None:
    """原子写。失败只记日志：快照是加速手段，不是正确性前提。"""
    path = _crowding_snapshot_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": _CROWDING_SNAPSHOT_VERSION,
                    "built_at_epoch": time.time(),
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "n": verdict.get("n"),
                    "verdict": verdict,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("crowding verdict 快照写入失败(不影响结果): %s", exc)


#: 被显式丢弃过快照的目录：下次读**不要**再从磁盘装回旧结论。
#:
#: 为什么用内存标记而不是删文件：删除走 `unlink`，在本仓的沙箱/回收站策略下
#: 会被拦（`SAFE_DELETE_BULK_GUARD`），而"清缓存"不该依赖删除权限。标记的
#: 代价只是重启后仍会装回那份快照 —— 而重启后重新算一遍本来就是可接受的。
_CROWDING_SNAPSHOT_IGNORED: set[str] = set()


def clear_crowding_cache() -> None:
    """清空并让下一次读同步重算（测试与"我要立刻看到新结论"的显式入口）。

    同时让已物化的快照失效（内存标记），否则下一次读又会把旧结论装回来。
    """
    with _CROWDING_LOCK:
        for key in _CROWDING_CACHE:
            _CROWDING_SNAPSHOT_IGNORED.add(key)
        _CROWDING_CACHE.clear()
        _CROWDING_STALE.clear()
        _CROWDING_BUILT_AT.clear()


def mark_crowding_stale(data_dir: Path | str) -> None:
    """写入方通知：enriched 有新分区落盘，判决需要重算（下一读触发后台重算）。"""
    key = str(Path(data_dir))
    with _CROWDING_LOCK:
        if key in _CROWDING_CACHE:
            _CROWDING_STALE[key] = True


def _recompute_in_background(repo, key: str) -> None:
    """后台重算。只为展示型结论服务，失败只记日志、保留旧值。"""
    with _CROWDING_LOCK:
        if key in _CROWDING_BUILDING:
            return
        _CROWDING_BUILDING.add(key)

    def _run() -> None:
        try:
            result = _compute_crowding_verdict(repo)
        except Exception as e:  # noqa: BLE001 —— 后台失败不许影响正在服务的旧值
            logger.warning("crowding verdict background refresh failed: %s", e)
            return
        finally:
            with _CROWDING_LOCK:
                _CROWDING_BUILDING.discard(key)
        with _CROWDING_LOCK:
            _CROWDING_CACHE[key] = result
            _CROWDING_STALE[key] = False
            _CROWDING_BUILT_AT[key] = time.monotonic()
            _CROWDING_SNAPSHOT_IGNORED.discard(key)
        # 重算完立刻物化：否则下一次重启又要从头算 30.8s
        _write_crowding_snapshot(key, result)

    threading.Thread(
        target=_run, name="crowding-verdict-refresh", daemon=True,
    ).start()


def build_crowding_verdict(repo) -> dict:
    """带缓存的入口。计算与缓存分离见 `_compute_crowding_verdict`。

    取值顺序：进程内 → 磁盘快照 → 同步计算。前两级都是 O(1)，所以
    "重启后第一次打开常识页"不再是冷路径。
    """
    data_dir = Path(repo.store.data_dir)
    key = str(data_dir)
    with _CROWDING_LOCK:
        hit = _CROWDING_CACHE.get(key)
        stale = _CROWDING_STALE.get(key, False)
        built_at = _CROWDING_BUILT_AT.get(key, 0.0)

    if hit is None:
        with _CROWDING_LOCK:
            ignored = key in _CROWDING_SNAPSHOT_IGNORED
        snap = None if ignored else _read_crowding_snapshot(data_dir)
        if snap is not None:
            verdict, age_s = snap
            with _CROWDING_LOCK:
                _CROWDING_CACHE[key] = verdict
                _CROWDING_STALE[key] = False
                # 换算到 monotonic 域：快照可能来自上一个进程，用「距今多久」
                # 反推一个等效的 monotonic 起点，后续的年龄判定不必分两套时钟。
                _CROWDING_BUILT_AT[key] = time.monotonic() - age_s
            hit = verdict
            built_at = time.monotonic() - age_s

    if hit is not None:
        too_old = (time.monotonic() - built_at) > _CROWDING_MAX_AGE_S
        if stale or too_old:
            _recompute_in_background(repo, key)  # 返回旧值，不阻塞
        return hit

    result = _compute_crowding_verdict(repo)
    with _CROWDING_LOCK:
        _CROWDING_CACHE[key] = result
        _CROWDING_STALE[key] = False
        _CROWDING_BUILT_AT[key] = time.monotonic()
        _CROWDING_SNAPSHOT_IGNORED.discard(key)
    _write_crowding_snapshot(data_dir, result)
    return result


def _compute_crowding_verdict(repo) -> dict:
    """换手率（拥挤度代理）→ 后续 20 日超额收益的判决 —— 证伪「拥挤=踩踏」。

    市场常识页「踩踏链」那条常识，原本用硬编码的 danger 值画「趋同→拥挤→
    踩踏」因果图。本函数用全市场日线算「高换手 vs 低换手」的后续 20 日超额
    收益（扣等权市场基准），判决「拥挤到底危不危险」。

    实证结论（本地 2005–2026 全市场，1530 万行）：
      - 换手率与后续 20 日超额收益**单调负相关**：高换手组中位超额约 −3.2%，
        低换手组约 −0.7%，高-低差约 −2.5%。
      - 控制价格动量后，换手率仍有**独立**负向预测力（每个动量组内高换手
        都跑输 1.2%~3.0%）—— 不是「已经涨多了」的投影。
      - 但控制市值（成交额）后，信号**只在大盘股成立**：大市值组内高换手跑输
        −3.2%，小市值组内高换手反而略好（+0.8%）。

    这个结论的诚实价值：它**同时**证伪了「拥挤必然踩踏」的粗糙叙事（小盘股
    高换手并不危险），又保留了精华（大盘股放量滞涨是真危险）。「拥挤危险」是
    有条件成立的命题，不是普适铁律。

    诚实边界：
      - 换手率只是拥挤度的「成交活跃」代理，不等于「同向持仓集中度」（真正的
        拥挤需要持仓/资金流数据，本地没有）；
      - 等权市场基准含生存偏差；
      - 20 日窗口只测「短期均值回归」，测不到「尾部踩踏」——踩踏是低频极端
        事件，用日频横截面回归测不到它的肥尾。

    返回:
      {
        "as_of": "...", "n": N,
        "quintiles": [  # 换手率分 5 组 → 20 日超额中位（bp）
          {"q": 1, "label": "低换手", "ex20_median_bp": ...}, ...
        ],
        "by_size": [  # 控制市值后，每个市值组内 高换手-低换手 超额差（bp）
          {"q": 1, "label": "小市值", "hi_minus_lo_bp": ...}, ...
        ],
        "caveats": [...],
      }
    本地无日线时返回 n=0。
    """
    from app.parquet import scan_enriched_parquet
    from app.tickflow.repository import enriched_dirname

    data_dir = Path(repo.store.data_dir)
    kroot = data_dir / enriched_dirname("stock")
    if not kroot.exists():
        return {"n": 0, "caveats": ["本地无日线分区"]}

    lf = scan_enriched_parquet(str(kroot / "**" / "*.parquet"))
    schema_cols = lf.collect_schema().names()
    if "raw_close" not in schema_cols or "turnover_rate" not in schema_cols:
        return {"n": 0, "caveats": ["日线缺 raw_close/turnover_rate 列"]}

    daily = lf.select(["symbol", "date", "raw_close", "turnover_rate", "amount"]).collect()
    daily = daily.with_columns(pl.col("date").cast(pl.Utf8))
    daily = daily.sort(["symbol", "date"])
    daily = daily.with_columns(pl.col("date").rank("ordinal").over("symbol").alias("rn"))

    # 过去 20 日价格动量 + 未来 20 日收益 + 等权市场基准
    daily = daily.with_columns(
        (pl.col("raw_close") / pl.col("raw_close").shift(20).over("symbol") - 1).alias("mom20"),
    )
    fut = daily.select(["symbol", "rn", pl.col("raw_close").alias("fut_close")]).rename({"rn": "fut_rn"})
    daily = daily.with_columns((pl.col("rn") + 20).alias("fut_rn_20"))
    daily = daily.join(fut, left_on=["symbol", "fut_rn_20"], right_on=["symbol", "fut_rn"], how="left")
    daily = daily.with_columns((pl.col("fut_close") / pl.col("raw_close") - 1).alias("fwd20"))
    mkt = daily.group_by("date").agg(pl.col("fwd20").mean().alias("mkt_fwd20"))
    daily = daily.join(mkt, on="date", how="left")
    daily = daily.with_columns((pl.col("fwd20") - pl.col("mkt_fwd20")).alias("ex20"))

    d = daily.filter(
        pl.col("ex20").is_not_null()
        & pl.col("turnover_rate").is_not_null()
        & pl.col("mom20").is_not_null()
        & pl.col("amount").is_not_null()
    )
    if d.height < 1:
        return {"n": int(d.height), "caveats": ["可算前向收益的样本不足"]}

    # 每日横截面分位
    d = d.with_columns(
        pl.col("turnover_rate").rank("ordinal").over("date").alias("turn_rank"),
        pl.col("mom20").rank("ordinal").over("date").alias("mom_rank"),
        pl.col("amount").rank("ordinal").over("date").alias("amt_rank"),
    )
    d = d.with_columns(
        (pl.col("turn_rank") / pl.col("turn_rank").max().over("date")).alias("turn_pct"),
        (pl.col("mom_rank") / pl.col("mom_rank").max().over("date")).alias("mom_pct"),
        (pl.col("amt_rank") / pl.col("amt_rank").max().over("date")).alias("amt_pct"),
    )
    d = d.with_columns(
        ((pl.col("turn_pct") * 5).ceil().clip(1, 5)).alias("turn_grp"),
        ((pl.col("mom_pct") * 5).ceil().clip(1, 5)).alias("mom_grp"),
        ((pl.col("amt_pct") * 5).ceil().clip(1, 5)).alias("amt_grp"),
    )

    labels = {1: "低换手", 2: "中低", 3: "中", 4: "中高", 5: "高换手"}
    size_labels = {1: "小市值", 2: "中小", 3: "中", 4: "中大", 5: "大市值"}

    quintiles = []
    for g in range(1, 6):
        sub = d.filter(pl.col("turn_grp") == g)["ex20"]
        med = sub.median()
        mean = sub.mean()
        quintiles.append({
            "q": g,
            "label": labels[g],
            "ex20_median_bp": round(float(med) * 10000, 1) if med is not None else None,
            "ex20_mean_bp": round(float(mean) * 10000, 1) if mean is not None else None,
        })

    by_size = []
    for ag in range(1, 6):
        sub = d.filter(pl.col("amt_grp") == ag)
        hi = sub.filter(pl.col("turn_grp") == 5)["ex20"].median()
        lo = sub.filter(pl.col("turn_grp") == 1)["ex20"].median()
        diff = None if hi is None or lo is None else hi - lo
        by_size.append({
            "q": ag,
            "label": size_labels[ag],
            "hi_minus_lo_bp": round(float(diff) * 10000, 1) if diff is not None else None,
        })

    return {
        "as_of": daily["date"].max(),
        "n": int(d.height),
        "quintiles": quintiles,
        "by_size": by_size,
        "caveats": [
            "换手率是「成交活跃」的拥挤代理，不等于「同向持仓集中度」——真正的拥挤需持仓/资金流数据，本地没有。",
            "20 日窗口只测「短期均值回归」，测不到低频极端的「尾部踩踏」；等权基准含生存偏差。",
        ],
    }
