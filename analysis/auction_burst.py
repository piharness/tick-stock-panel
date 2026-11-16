"""集合竞价帧采集器 —— 唯一进版本控制的采集入口。

## 为什么这段必须从 `.workbuddy/tmp` 里搬出来

2026-11-12 的整条链（`auction_size.py burst` → `_diag_auction_indicative.py --frames`）
依赖的文件全部落在 `.workbuddy/tmp/`，而 `.gitignore:112` 是 `.workbuddy/`。
`git check-ignore -v` 对 `auction_size.py` 与 `auction_burst.jsonl` 都命中这一行。
后果不是「不方便」，是三种具体的失败：

1. **tmp 被清一次 → 采集脚本没了**，而错误形态是 `SystemExit("无帧")` → exit 1，
   看上去像代码坏掉，实际是证据链断在版本控制之外；
2. **帧按固定名 append**（旧脚本 `out = HERE/"auction_burst.jsonl"` + `open("a")`）：
   次日直接跑会把 569 帧和新帧写进同一个文件，第二天的 `days[0]` 与
   `frame_interval_s` 直方图同时被污染，且没有任何报错；
3. **没有起点概念**：旧脚本只有 `stop = now.replace(9,26,30)`，什么时候开始抓
   取决于人什么时候按回车。09:10 之后开始 ⇒ `A`（昨成交额）取到当日成交额 ⇒
   kappa 全错（虚高 1/0.705 ≈ 1.42×）；容量**只在 homogeneous 与 rule_ 口径错**
   —— 主口径 `cap = 0.1·Σ(A·κ) = 0.1·ΣV` 里 A 约掉，这是 `_diag_l1_weight_frontier.py`
   用 `k_f = V/A` 定义 κ 的代数结果，不是巧合。而 `mult_092430` 仍然是 2.09
   （A 只进 kappa）⇒ 数字分辨不出，唯一信号是分析侧的退出码。

所以这里把「采集」与「下单表」拆开：**本文件只负责把原始帧写下来，不做任何交易判断**。
`NAMES` 里那些 `prev_close` / 涨停价 / 昨日成交额是每日变量，写进代码就是过期常量
（2026-11-12 实测 20/20 只收盘价相对 11-11 都动了，002086 与 000678 各 +10%），
它们不进这里、也不进池子 JSON。

## 输出

- 帧：`data/research/auction_indicative/raw/<YYYYMMDD>_burst.jsonl`
  （按交易日定名 ⇒ 跨日不可能混；`data/**` 默认被忽略，这是对的 —— 证据本体是
  `--archive` 落下的 parquet，raw jsonl 只是它的上游）
- 元数据：同目录 `<YYYYMMDD>_burst.meta.json`，记 `interval_hist` / `gaps_s` /
  `a_frame` / `a_daily`。**声明与实测不一致时以实测为准**，所以间隔不写进文档只写进这里。

## 退出码（无人值守时退出码是唯一信号）

- `0` 帧已落盘，且首帧 stamp 不是当日 ⇒ `A` 可用
- `5` 帧已落盘，但首帧 stamp 已是当日 ⇒ **A 不可用**（起点晚于 09:10）。
      帧仍然有效（`V_obs` 不受影响，A 只进 kappa），所以不许删帧、不必重跑当天
- `2` 拒绝启动：周末 / 已过 09:25:00 / 池子空 / 当日文件已存在且没给 `--resume`
- `3` 一帧也没写下来（网络全失）

## 用法

    python analysis/auction_burst.py --dry-run          # 只打印计划，不联网
    python analysis/auction_burst.py --start 09:05:00   # 早点拉起，自己等到点
    python analysis/auction_burst.py --resume           # 同日重启，跳过重复 ts

`--start` 在未来时**等待**而不是立刻开抓：排程器可以 09:00 拉起它，
不需要有人在 09:05 按一下，也不需要 `mv` 旧文件给新文件让位。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POOL_DIR = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "research" / "auction_indicative" / "raw"
DAILY_DIR = ROOT / "data" / "kline_daily"

#: 申报截止。分析侧 `_diag_auction_indicative.DEADLINE` 是同一件事的另一个产地，
#: 两处必须相等（`test_auction_burst_collector.py` 断言）。
AUCTION_DEADLINE = dt_time(9, 25, 0)
#: 尾盘定版线。单一产地是 `backend/app/services/data_integrity.py::CLOSE_CUTOFF`
#: （该模块的判据文档：quote_ts 早于当日这条线 ⇒ 盘中快照，不是权威行）。
#: 这里不 import backend 是为了采集器能在不装后端依赖的机器上跑；一致性由测试绑定。
CLOSE_CUTOFF = dt_time(15, 30, 0)
#: 单次 HTTP 最多 60 只（上游限制），超了必须分批。
BATCH = 60
#: 09:10 之后上游 stamp 已是当日 ⇒ 首帧不再是「前一交易日 16:xx 收盘帧」。只用于报告。
A_FRAME_DEADLINE = dt_time(9, 10, 0)

_UA = "Mozilla/5.0"
#: 档位字段，**必须按上游的字段序号排列**：`9..18` 是买一~买五（价/量交替），
#: `19..28` 是卖一~卖五。写成 b1/…/b5 之后接 a1/…/a5 才与 `range(9, 29)` 对得上；
#: 若按「买一/卖一/买二/卖二」的直觉排，b2p 会静默读到 b3p 的位置 —— 档位整体错位，
#: 而价格量级相近，肉眼看不错。
_LEVELS = ("b1p", "b1v", "b2p", "b2v", "b3p", "b3v", "b4p", "b4v", "b5p", "b5v",
           "a1p", "a1v", "a2p", "a2v", "a3p", "a3v", "a4p", "a4v", "a5p", "a5v")


def _qq(sym: str) -> str:
    """`600000.SH` -> `sh600000`。北交所必须走 `bj`，不能落进 `sz`。"""
    code, _, ex = sym.partition(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(ex.upper(), "sz") + code


def _chunks(items: list[str], size: int = BATCH) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or []


def parse_snapshot(raw: str) -> dict[str, dict]:
    """把 `v_sh600000="..."` 的响应体解析成 `{symbol_code: fields}`。

    字段索引沿用项目内既有口径（2026-11-12 实测）：`1` 名称、`3` 现价、`4` 昨收、
    `5` 开盘、`6` 成交量、`9..18` 买一~买五的价/量交替、`19..28` 卖一~卖五、
    `30` 内容时间、`37` 成交额（万元）。**二~五档是否在竞价期有值，是待证伪的问题，
    不是假设** —— 补字段的目的就是让 2026-11-13 那一跑能回答它。
    """
    out: dict[str, dict] = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        head, _, body = line.partition("=")
        # `v_sz000530` -> 去掉 `v_` 与交易所前缀，只留 6 位代码：调用侧按 `_qq(s)[2:]` 配对。
        code = head[2:][2:]
        f = body.strip().strip('"').split("~")
        if len(f) < 40:
            continue

        def g(i: int):
            try:
                return float(f[i])
            except (IndexError, ValueError):
                return None

        rec = {"code": code, "name": f[1], "last": g(3), "prev": g(4), "open": g(5),
               "vol_hand": g(6), "amount_wan": g(37),
               "stamp": f[30] if len(f) > 30 else ""}
        for k, idx in zip(_LEVELS, range(9, 29)):
            rec[k] = g(idx)
        out[code] = rec
    return out


def fetch(symbols: list[str], timeout: float = 8.0) -> tuple[dict[str, dict], int]:
    """分批抓一轮。返回 `({symbol: fields}, 失败批数)`。单批失败不中断整轮。"""
    got: dict[str, dict] = {}
    failed = 0
    for part in _chunks(symbols):
        url = "https://qt.gtimg.cn/q=" + ",".join(_qq(s) for s in part)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "replace")
        except Exception:  # noqa: BLE001 —— 单批失败是常态(上游限流)，不吞掉计数
            failed += 1
            continue
        by_code = parse_snapshot(raw)
        if not by_code:
            # 解析出 0 只与「网络失败」是两种病：一种是上游回空/口径变了，一种是断网。
            # 两者都必须计进 failed，否则「取到 0 只」会静默通过，最后落一个空帧。
            failed += 1
            continue
        for s in part:
            rec = by_code.get(_qq(s)[2:])
            if rec:
                got[s] = rec
    return got, failed


# ----------------------------------------------------------------- 前一日 A（可选）
def _partition_dates() -> tuple[list[tuple[date, Path]], str]:
    """`kline_daily` 的分区日期。日分区 `date=YYYY-MM-DD` 与月分区 `date=YYYY-MM`
    两种形态都要认：`backend/app/decision/bars.py` 声明自 2026-11 起按月分区，
    而当前盘上是按日 —— 声明与盘面不一致时以盘面为准，且把形态记进 meta。"""
    daily: list[tuple[date, Path]] = []
    monthly: list[tuple[date, Path]] = []
    if not DAILY_DIR.is_dir():
        return [], "missing"
    for p in DAILY_DIR.iterdir():
        if not p.is_dir() or not p.name.startswith("date="):
            continue
        key = p.name[5:]
        try:
            if len(key) == 10:
                daily.append((date.fromisoformat(key), p))
            elif len(key) == 7:
                monthly.append((date.fromisoformat(key + "-01"), p))
        except ValueError:
            continue
    if daily:
        return sorted(daily), "daily"
    return sorted(monthly), "monthly"


def _as_date(value) -> date | None:
    """parquet 的 `date` 列可能是 `date`、`datetime` 或字符串，统一成 `date`。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _bar_is_final(quote_ts_ms: int | None) -> bool:
    """这一格日线是不是**定版行**。

    判据与 `backend/app/services/data_integrity.py::_is_snapshot` 严格互补：
    全空 `quote_ts` = batch/盘后写入的权威行 ⇒ 定版；否则看时刻是否已过
    `CLOSE_CUTOFF`。互补关系由测试逐点断言，不靠两处注释互相引用。
    """
    if quote_ts_ms is None:
        return True
    return datetime.fromtimestamp(int(quote_ts_ms) / 1000).time() >= CLOSE_CUTOFF


def prev_amount_from_daily(today: date) -> dict:
    """前一交易日的 `amount`（万元），**只在那一格是定版行时才返回数值**。

    为什么需要它：`A` 现在只能取自「09:10 之前的首帧」，一旦起点晚了就彻底没有，
    于是整条链被绑死在 09:05 有人值守。`A` 本身在权威日线里是现成的 ——
    2026-11-12 实测：首帧 `amount_wan×1e4`（stamp=20261111161415，起点 09:06:43）
    与 `kline_daily/date=2026-11-11.amount` **20/20 同源，max_rel_diff=5.1e-6**
    （rel==0 是 0/20：帧记整万元、日线记元，差在取整）⇒ 同一个数，不是等价近似。

    但「现成」有陷阱：2026-11-12 盘上那一格 `quote_ts` max=11:30:05，是**盘中快照**，
    用它会得到 `A` 偏小 → kappa 偏大（该池实测 11:30 累计额 / 11-11 全天额
    中位 0.705、最小 0.494、最大 2.570，n=20）⇒ 错在危险的那一侧。注意别把
    「kappa 偏大」顺口说成「容量被高估」：主口径 `0.1·Σ(A·κ)` 里 A 约掉，
    真正随 A 走的是 `homogeneous_kappa_wan` 与两个 `rule_*_kappa.total_wan`。
    所以这里不发明新阈值，套用项目自己的判据 `CLOSE_CUTOFF`：`quote_ts` 早于当日
    15:30 就是快照，拒绝出数。**宁可交出「不可用」，不交一个偏小的 A。**
    """
    info: dict = {"prev_trade_day": None, "partition_form": None,
                  "quote_ts_ms": None, "final": False, "reason": "", "wan": {}}
    parts, form = _partition_dates()
    info["partition_form"] = form
    if not parts:
        info["reason"] = f"{DAILY_DIR} 无可识别分区"
        return info
    candidates = [(d, p) for d, p in parts if d < today]
    if not candidates:
        info["reason"] = "没有早于今天的日线分区"
        return info
    day, path = candidates[-1]

    try:
        import pyarrow.parquet as pq
    except Exception:  # noqa: BLE001
        info["reason"] = "无 pyarrow，跳过日线 A 快照"
        return info

    files = sorted(path.glob("*.parquet"))
    if not files:
        info["reason"] = f"{path} 内无 parquet"
        return info
    raw: list[dict] = []
    for f in files:
        try:
            have = set(pq.ParquetFile(f).schema_arrow.names)
            # `quote_ts` 列不存在 = 权威 batch 行（老分区没有这一列），不是读失败。
            cols = [c for c in ("symbol", "date", "amount", "quote_ts") if c in have]
            table = pq.ParquetFile(f).read(columns=cols)
        except Exception as exc:  # noqa: BLE001
            info["reason"] = f"读 {f.name} 失败: {type(exc).__name__}"
            return info
        raw.extend(table.to_pylist())

    if form == "monthly":
        # 月分区的目录名只有月份，真实交易日只能从 `date` 列取。
        # 不取的话 `prev_trade_day` 会变成月初，A 取到的是整个月的合计。
        avail = sorted({_as_date(r.get("date")) for r in raw} - {None})
        avail = [d for d in avail if d < today]
        if not avail:
            info["reason"] = f"{path.name} 内没有早于 {today} 的行"
            return info
        day = avail[-1]
        raw = [r for r in raw if _as_date(r.get("date")) == day]
    info["prev_trade_day"] = day.isoformat()
    rows = raw
    if not rows:
        info["reason"] = f"{path.name} 内没有 {day} 的行"
        return info

    qts = [r.get("quote_ts") for r in rows if r.get("quote_ts")]
    qmax = max(qts) if qts else None
    info["quote_ts_ms"] = qmax
    info["final"] = _bar_is_final(qmax)
    if qmax is None:
        info["reason"] = "quote_ts 全空 ⇒ 权威 batch 行"
    elif info["final"]:
        info["reason"] = (f"quote_ts={datetime.fromtimestamp(qmax / 1000):%H:%M:%S}"
                          f" ≥ 定版线 {CLOSE_CUTOFF:%H:%M}")
    else:
        info["reason"] = (f"quote_ts={datetime.fromtimestamp(qmax / 1000):%H:%M:%S}"
                          f" < 定版线 {CLOSE_CUTOFF:%H:%M} ⇒ 盘中快照，"
                          f"A 会被低估，拒绝出数")
    if info["final"]:
        wan = {}
        for r in rows:
            a = r.get("amount")
            if a is None:
                continue
            wan[r["symbol"]] = round(float(a) / 1e4, 4)  # 元 -> 万元，与分析侧同列同单位
        info["wan"] = wan
    return info


# ----------------------------------------------------------------- 主循环
def out_paths(day: date) -> tuple[Path, Path]:
    key = day.strftime("%Y%m%d")
    return RAW_DIR / f"{key}_burst.jsonl", RAW_DIR / f"{key}_burst.meta.json"


def _interval_hist(stamps: list[float]) -> dict[str, int]:
    """相邻帧间隔的整数秒直方图。采样抖动不四舍五入到分钟，宁可看着难看。"""
    hist: dict[str, int] = {}
    for a, b in zip(stamps, stamps[1:]):
        k = int(round(b - a))
        hist[str(k)] = hist.get(str(k), 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: int(kv[0])))


def _gaps_s(stamps: list[float], interval: float) -> list[int]:
    """超过 3 倍声明间隔的断点，单位秒。`--resume` 续跑留下的洞必须被列出来，
    而不是被一条平滑的「平均间隔」糊过去。"""
    return [int(round(b - a)) for a, b in zip(stamps, stamps[1:]) if b - a > 3 * interval]


def _hhmmss(value: str) -> dt_time:
    parts = [int(x) for x in value.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dt_time(*parts)


def collect(day: date, symbols: list[str], interval: float, start: dt_time,
            stop: dt_time, resume: bool, now_fn=datetime.now) -> int:
    frames_path, meta_path = out_paths(day)
    now = now_fn()
    if now.time() >= AUCTION_DEADLINE:
        print(f"拒绝启动：当前 {now:%H:%M:%S} 已过申报截止 {AUCTION_DEADLINE:%H:%M:%S}，"
              f"竞价窗口已关闭。帧不可回补，不要写半截文件。", file=sys.stderr)
        return 2
    if frames_path.exists() and not resume:
        print(f"拒绝启动：{frames_path} 已存在。同日重跑请加 --resume"
              f"（重复 ts 跳过，间隔直方图会把断点暴露出来），或用不同 --start 另存。",
              file=sys.stderr)
        return 2

    seen: set[str] = set()
    if resume and frames_path.exists():
        for line in frames_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line)["ts"])
            except Exception:  # noqa: BLE001
                continue

    frames_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime.combine(day, start)
    if now < t0:
        print(f"等待 {t0:%H:%M:%S} 开始（当前 {now:%H:%M:%S}）…", flush=True)

    written: list[str] = []
    failed_batches = 0
    n = 0
    with frames_path.open("a", encoding="utf-8") as fh:
        while True:
            target = t0 + timedelta(seconds=interval * n)
            wait = (target - now_fn()).total_seconds()
            if wait > 0:
                time.sleep(min(wait, 5.0))
                continue
            tick = now_fn()
            if tick.time() >= stop:
                break
            snap, bad = fetch(symbols)
            failed_batches += bad
            if snap:
                ts = tick.isoformat(timespec="seconds")
                if ts in seen:
                    pass
                else:
                    fh.write(json.dumps({"ts": ts, "snap": snap}, ensure_ascii=False) + "\n")
                    fh.flush()
                    seen.add(ts)
                    written.append(ts)
            n += 1
            if n % 30 == 0:
                print(f"  {tick:%H:%M:%S} 已写 {len(written)} 帧 / 样本 {len(snap)}", flush=True)

    if not written and not seen:
        print("一帧也没写下来：检查网络 / 上游。", file=sys.stderr)
        return 3

    stamps = [datetime.fromisoformat(t).timestamp() for t in sorted(seen)]
    hist = _interval_hist(stamps)
    gaps = _gaps_s(stamps, interval)
    first_ts, last_ts = sorted(seen)[0], sorted(seen)[-1]

    first_rec: dict | None = None
    try:
        with frames_path.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("snap"):
                    first_rec = rec
                    break
    except Exception:  # noqa: BLE001
        pass
    first_stamp = str(next(iter(first_rec["snap"].values())).get("stamp", "")) if first_rec else ""

    a_frame = {
        "first_stamp": first_stamp,
        "first_stamp_day": first_stamp[:8],
        "usable": bool(first_stamp) and first_stamp[:8] != day.strftime("%Y%m%d"),
        "note": ("首帧 stamp 是前一交易日收盘帧 ⇒ A 可用" if first_stamp
                 and first_stamp[:8] != day.strftime("%Y%m%d")
                 else "首帧 stamp 已是当日 ⇒ A 不可用（起点晚于 09:10）；"
                      "V_obs 不受影响，A 只进 kappa"),
    }
    a_daily = prev_amount_from_daily(day)
    compare = None
    if a_frame["usable"] and a_daily["final"] and a_daily["wan"] and first_rec:
        diffs = []
        for sym, rec in first_rec["snap"].items():
            a_fr = rec.get("amount_wan")
            a_da = a_daily["wan"].get(sym)
            if a_fr and a_da:
                diffs.append(abs(a_fr / a_da - 1))
        if diffs:
            compare = {"n": len(diffs), "max_rel_diff": round(max(diffs), 8),
                       "verdict": "同一产地" if max(diffs) < 1e-4 else "两产地不一致，先查复权/单位"}

    meta = {
        "date": day.isoformat(),
        "declared_interval_s": interval,
        "start": start.strftime("%H:%M:%S"),
        "stop": stop.strftime("%H:%M:%S"),
        "symbols": len(symbols),
        "batches_per_tick": len(_chunks(symbols)),
        "resumed": resume,
        "frames_new": len(written),
        "frames_total": len(seen),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "interval_hist_s": hist,
        "gaps_s": gaps,
        "failed_batches": failed_batches,
        "a_frame": a_frame,
        "a_daily": a_daily,
        "a_compare": compare,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n帧 {meta['frames_total']} 条（本次 {len(written)}）→ {frames_path.name}")
    print(f"  区间 {first_ts[11:]}–{last_ts[11:]}  间隔直方图 {hist}"
          + (f"  断点 {gaps} s" if gaps else ""))
    if a_frame["usable"]:
        print(f"  A(frame) 可用：首帧 stamp={first_stamp}")
    else:
        print(f"  !! A(frame) 不可用：首帧 stamp={first_stamp or '空'} "
              f"—— 起点晚于 {A_FRAME_DEADLINE:%H:%M}。帧本身有效，kappa 不可用。")
    print(f"  A(daily) prev_trade_day={a_daily['prev_trade_day']} "
          f"final={a_daily['final']} ({a_daily['reason']})")
    if compare:
        print(f"  两产地 A 比对：n={compare['n']} max_rel_diff={compare['max_rel_diff']} "
              f"⇒ {compare['verdict']}")
    return 0 if a_frame["usable"] else 5


def main(argv: list[str] | None = None, now_fn=datetime.now) -> int:
    ap = argparse.ArgumentParser(description="集合竞价帧采集器")
    ap.add_argument("--pool", default=str(POOL_DIR / "auction_pool_cohort_2026-11-12.json"),
                    help="票池 JSON（只认 symbols 键；每日变量不许进池子）")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--start", default="09:05:00", help="计划起点（未来则等待）")
    ap.add_argument("--stop", default="09:26:30", help="计划终点")
    ap.add_argument("--resume", action="store_true", help="同日重启：跳过重复 ts")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不联网")
    args = ap.parse_args(argv)

    now = now_fn()
    day = now.date()
    if day.weekday() >= 5:
        print(f"拒绝启动：{day} 是周末。", file=sys.stderr)
        return 2
    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    symbols = [str(s) for s in pool.get("symbols", [])]
    if not symbols:
        print(f"拒绝启动：池子 {args.pool} 没有 symbols。", file=sys.stderr)
        return 2
    if set(pool) & set(pool.get("forbidden_keys", [])):
        print("拒绝启动：池子里出现了每日变量键，先读 why_forbidden。", file=sys.stderr)
        return 2

    start, stop = _hhmmss(args.start), _hhmmss(args.stop)
    frames_path, meta_path = out_paths(day)
    if args.dry_run:
        print(f"交易日 {day}  池 {pool.get('pool_id')}  {len(symbols)} 只  "
              f"每轮 {len(_chunks(symbols))} 批")
        print(f"计划 {start:%H:%M:%S}–{stop:%H:%M:%S}  间隔 {args.interval}s  "
              f"预计 {int((datetime.combine(day, stop) - datetime.combine(day, start)).total_seconds() / args.interval)} 帧")
        print(f"帧 → {frames_path}")
        print(f"元数据 → {meta_path}")
        print(f"窗口状态：{now:%H:%M:%S}  vs 截止 {AUCTION_DEADLINE:%H:%M:%S} ⇒ "
              f"{'已关闭（会拒绝启动）' if now.time() >= AUCTION_DEADLINE else '可执行'}")
        return 0
    return collect(day, symbols, args.interval, start, stop, args.resume, now_fn)


if __name__ == "__main__":
    raise SystemExit(main())
