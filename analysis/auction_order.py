"""集合竞价下单规模表 —— 唯一进版本控制的定价入口。

## 为什么这段必须从 `.workbuddy/tmp` 里搬出来

2026-11-12 之前，`auction_size.py` 与它引用的 `auction_samples.jsonl` 都在
`.workbuddy/tmp/`，而 `.gitignore` 第一行就是 `.workbuddy/`。后果不是「不方便」：

1. **tmp 清一次 → 定价脚本没了**，而错误形态是 `SystemExit("无法载入挂单价定义")`
   → 看上去像代码坏掉，实际是证据断在版本控制之外；
2. **`NAMES` 四列里有三列是每日变量**（前收 / 挂单价 / 昨日成交额），值是 2026-11-11
   口径。2026-11-12 实测 20/20 只收盘价相对 11-11 都动了 ⇒ 拿常量算下单股数是
   **安静地错**：exit 0、表是绿的、`opens_below_limit` 从 13/20 变 19/20。
   这不是「数错了」，是**换了策略**；
3. **固定名 + append** 的采样日志，次日直接跑会把两天的快照接进同一个文件。

## 三条口径，逐条钉死

**挂单价**：`app.decision.l1_cohort._limit_price(帧 f[4] × 0.99, tick floor)`。
本文件**不重写**这个式子。理由不是洁癖：`_limit_price` 的 docstring 记着
「限价定义曾在生产路径与回测路径各写一遍，只有一条被测试覆盖，一分钱的方向就是从
两条实现的缝里漏的」。再内联一行 `floor(prev*0.99, 2)` 就是**第三份拷贝**，而且
这一份的输入换成行情帧、更难对拍。载入失败直接 `SystemExit`，**不回落本地实现** ——
回落会让「用了帧里的前收」和「还在用常量」在输出上无法区分。

**昨日成交额（guard2 的分母）**：不写常量，走
`auction_burst.prev_amount_from_daily()`，即「前一交易日、且那一格是定版行」。
2026-11-12 盘上 `quote_ts=11:30:03` 是**午盘快照**（`kline_daily` 与
`kline_daily_enriched` 两个目录都是），拿它当分母会把「拿昨全天当竞价量」这件事
判反。判据不在这里发明：`data_integrity.CLOSE_CUTOFF` 已有唯一定义。

**漂移见证**：`auction_order_witness_2026-11-11.json` 是**负控**，冻结 11-11 收盘
与据此算出的挂单价。它**只**用来数「帧相对它漂了多少」，绝不进挂单价/股数。
它是本文件唯一能区分「改了」和「被改回去」的观测量 —— 而负控一旦被续命就失效，
所以它必须永远停在 11-11；分叉恒 >0 是它的正常形态，不是缺陷。**一道恒红的判据
等于没有判据**，所以 `check` 刻意不做红灯，只把两列并排放出来。

## 第四条口径：票池的**名单**也是每日变量

`forbidden_keys` 挡的是「池子里存了过期的**值**」，挡不住「池子存了过期的**名单**」。
`auction_pool_cohort_2026-11-12.json` 里那 20 只是**11-12 入场**的 cohort：
`candidates(as_of="2026-11-11")` 逐字吻合 20/20（2026-11-12 实测）。
11-13 入场的是另一组 —— 与它只重合 16/20，4 只出、4 只进。

拿 11-12 的名单跑 11-13 的下单表，与「用 11-11 收盘价算限价」**完全同构**：
绿表、exit 0、`opens_below_limit` 好看，只是买的不是今天该买的那 20 只。
所以 `run`/`check` 默认由 `candidates(前一交易日)` 现算名单，并且要求**前一交易日的
enriched 分区已定版**（判据 `auction_burst._bar_is_final` —— 与 guard2 分母同一把尺）。
未定版就 exit 4，**不退回冻结名单**：拿盘中快照的 close 排序，连 cohort 的身份都是错的。

`--pool frozen` 保留那份 20 只，仅供复盘与机制对照；非 `--rehearsal` 时 exit 4。

## 第四条口径的三个缺口（2026-11-12 补，同日二次）

**「定版」不等于「是前一格」。** 定版只回答「这一格是不是盘后权威行」。
`_prev_trade_day` 取「最后一格 < today」，所以前一交易日**整格缺失**时它会安静地退回
再前一天，`_bar_is_final` 照样为真 —— 名单又变成上一天的，而表是绿的、exit 0。

补丁 1 曾写成 `candidates().entry_date` 必须是今天。**它挡不住这个缺口**：`entry_date`
是 `dates[i0+1] if i0+1 < len(dates) else None`，而「整格缺失」的定义就是「下一格不在
面板里」⇒ `entry_date` 恒为 `None` ⇒ 闸门恒放行。2026-11-12 实测（喂 `entry_date=None`
的入参）：20 只、`mode=today`、exit 0、零告警，与修补前逐字相同。
`None` 同时兼容「今天分区还没落」（正常，实测 11-12 的分区 13:34 才建）与「前一交易日
丢了」（P0）—— 在这一个入参下**不可判别**，所以它不是一条轴，只是半个。

补丁 2 是真正能说话的那条：`run` 里「帧前收 vs 名单日线收盘」逐票全不等 ⇒ exit 4。
但它当时**只在 `run` 里**，而明早第一步跑的是 `check` —— 实测 20/20 全不等时 `check`
返回 0。现在抽成 `pool_frame_mismatch()`，两个入口共用，`check` 的 exit 4 也由此而来。

补丁 3：`_bar_is_final(None) is True`（「quote_ts 全空的 batch 行算定版」，与
`data_integrity` 互补，本身没错），但 `_enriched_quote_ts()` 在**分区不存在**时也返回
`None` ⇒ 缺失被读成定版。这是 P0 之前的一层，连「kline_daily 有没有这一格」都不需要。
修法不动判据，只在调用处先问一次分区是否存在。

**剩下的那个真正缺口不是代码，是数据：`kline_daily` 与 `kline_daily_enriched` 的日期
集合实测 371=371、双向零差 —— 它们不是两条线，是同一批 dump 的两个目录。**
`kline_index_daily` 与它 249 天逐日相同、mtime 只差 4 秒（同一次 pipeline 的相邻步骤），
能分「个股单侧失败」，分不了「整天没跑」。所以「相邻」在缺失当天没有独立观测量：
要么内置交易日历常量（并用历史逐日自证），要么在这一天拒出表。

补丁 4（2026-11-13）：选了前者 —— `analysis/trading_calendar.py` 的常量日历。
它不依赖任何一次运行，所以能反过来审面板：面板区间内每个工作日「有没有分区」与
「常量说是不是交易日」**逐日相等、无一例外**（数字由
`test_calendar_constant_explains_every_panel_working_day` 现算并断言，不在这里复述 ——
复述一遍就是又一处没有产地的读数）。同时这也给出这一格的实证 base rate：这份 dump 里
「整格缺失」**从未发生过** ⇒ 那条基准收益上「错一天」的混合效应没有历史实例可估，
它是构造出来的，不是观测到的。

这条轴必须与补丁 2 并存而不是替代它：补丁 2 是**相对**判据（两张表是不是同一天），
两条线同源落后一天时它只会说「同一天」；补丁 4 是**绝对**锚（今天的前一格到底是
哪一天），代价是一份每年要续的常量，收益是那一格能失败。超覆盖区间返回 `None`
⇒ **拒出表**、不放行：留一条「未知就放行」的分支，等于把这一格重新变成静默。

## 补丁 5：常量到期之后，这条轴靠实探接班（仍不放手）

补丁 4 的悬崖是日程表上的：`COVERED` 止于 2026-12-31，此后每天 `gap_days` 返回
`None` ⇒ 09:20 的下单闸门每天 `exit 4`。那是**下单路径**，不是某张诊断表。

2026-11-12 实测 fuyao `/calendar/trading-days` 无入参、恒返回 `[今天-365天, 今天]`
共 243 个交易日、八种参数写法全被忽略，**没有未来表**。它不需要未来表：本文件问的
`(prev, today)` 全在过去，滚动一年窗口每天都含着整个过去一年。

于是接法与闹钟同源：`trading_calendar.usable()`（体检 + 重叠区逐日对账）判「能不能
接班」，取数走 `analysis/calendar_probe.py`（全仓唯一一处向磁盘外问日期的地方）。

**只在常量覆盖之外才取**（`today > COVERED[1]`）。这不是性能优化，是可用性要求：
09:20 那个窗口不该为了一个 2027 年才用得到的轴，在每个早晨多绑一次外部服务的
可用性。代价是这条分支在 2026 年不会被自动跑到 —— 所以它有两条独立演练：注入
集合的单测，和 `_diag_calendar_live_rehearsal.py`（把常量右端提前到昨天，用**已知
答案的那一天**跑 2027 的分支）。

实探接不上时仍是 `exit 4`，但病因必须分开写：**没凭证 / 上游停更 / 与常量分歧 /
today 在实探表外** 四种，处置完全不同。把它们缩成同一句「补日历」，就是让操作的人
在 09:15 去改一份没有证据的常量表 —— 那比停一天更贵。所以下面 `_refusal()` 按
`live_info` 分四支，每支都点名「这一步该修什么、哪一步是错的修法」。

**名单会变，票池里的制度参数也得逐票变。** 北交所在面板里出现过（近 60 个 as_of
有 **31 天**含 920 系，共 **48 只-日**，见 `_diag_pool_freshness_result.json`）：
`qq_symbol` 曾把 `BJ` 映射成 `sz`，上游返回 `v_pv_none_match`
⇒ `no_snapshot`(SKIP)、表绿、exit 0、少买 100 万。交易所前缀映射已有第一份实现
（`auction_burst._qq`，三地都对），此处曾是第二份且少一个分支。定价、前缀、批次规则
写两遍，只有一份被测试覆盖 —— 这是本文件开头讲的那个病的同构复发。
（科创板 200 股起 / 10 万股单笔上限：当前 5 只实测 1300–15500 股均不触线，
触发条件是 10% 帽子把股数压到 <200，等价 `amt < 2000×limit`。）

## 退出码（无人值守时退出码是唯一信号）

- `0` 正常（`check`：快照可用；`run`：表已生成）
- `2` 用法错误
- `3` 抓不到快照
- `4` 票池不可用（前一交易日未定版 / enriched 分区不存在 / **日历显示 `prev` 不是今天的
  前一格（缺哪几个交易日会逐个列出）/ 今天超出常量覆盖区间且实探接不上（四种病因分开写）**
  / 候选为空 / 冻结名单在非演练模式被使用）；或 `check`/`run` 拒出表（帧前收与名单逐票全不等）
- `5` 帧里 `prev` 缺失 —— `run` 会把该票判 `prev_missing`(SKIP)，不回落常量
- `6` 池子里有票没有对照收盘 —— `check` **判不了**，这不是「通过」

## 用法

    python analysis/auction_order.py plan          # 离线：负控表 + 分叉说明
    python analysis/auction_order.py check         # 联网：今日票池 × 帧前收 vs 日线收盘
    python analysis/auction_order.py run --save    # 联网：09:20 下单表（--save 落下当日快照）

    加 `--pool frozen --rehearsal`：用 11-12 那份冻结名单演练（不可下单）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # analysis/ 的上一层就是仓库根
HERE = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
DATA_DIR = ROOT / "data"
POOL_JSON = HERE / "auction_pool_cohort_2026-11-12.json"
WITNESS_JSON = HERE / "auction_order_witness_2026-11-11.json"
RAW_DIR = ROOT / "data" / "research" / "auction_indicative" / "raw"
FROZEN_ENTRY = "2026-11-12"     # 冻结名单的入场日；非今日即为过期

# --- 挂单价的唯一定义点，不在这里重写 ---
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
try:
    from app.decision.l1_cohort import _limit_price
except Exception as exc:  # noqa: BLE001
    raise SystemExit(
        f"无法载入挂单价定义 app.decision.l1_cohort._limit_price: {exc!r}\n"
        f"  用 backend/.venv 的解释器跑: backend/.venv/Scripts/python.exe "
        f"{Path(sys.argv[0]).name}\n"
        f"  **不回落到本地 floor(prev*0.99)**：回落会让「用了帧」和「用了常量」"
        f"在输出上无法区分。"
    )

BUDGET = 20_000_000.0     # 备资
PER_NAME = 1_000_000.0    # 每票上限
PARTICIPATION = 0.10      # 竞价成交量参与率上限
LOT = 100

# 阶段判据用「时间」，不用 open 字段 —— 盘前 open 返回的是昨日开盘价（实测非零），
# 拿它判「已开盘」会与「拿到今日竞价量」互斥，把 09:20 全表拒成 DEGRADED。
AUCTION_START = (9, 20)   # 竞价尾段，虚拟匹配量开始稳定
AUCTION_MATCH = (9, 25)   # 撮合瞬间：vol 即最终竞价量
AFTER_MATCH = (9, 26)     # 之后 vol 混入连续竞价，10% 会高估

#: guard2 阈值：竞价额 / 昨全天额 超过它 ⇒ 这一格是昨日残留，不是竞价量。
#: 0.80 不是拍的：本池实测竞价额占昨全天额 5% 量级，残留是 1.0 量级，中间地带很宽。
GUARD2_RATIO = 0.80


def _sibling(name: str):
    """按路径载入同目录的分析脚本（`analysis/` 不是包，没有 `__init__.py`）。"""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 内置交易日历。**唯一能守住「名单与帧同源落后一天」的轴** ——
#: `pool_frame_mismatch()` 是相对判据，两边一起退回前一天时它只会说「同一天」。
#: 模块级加载（它只有常量与纯函数，没有依赖），测试替换 `ao.CAL` 即可做变异对照。
CAL = _sibling("trading_calendar")

#: 实探日历的取数模块。**按需加载**：常量覆盖内的每个早晨都不该为一个 14 个月后
#: 才用得到的轴付出一次 import（`calendar_probe` 会往 `sys.path` 里塞 backend）。
_PROBE_CACHE = None


def _probe_module():
    """按路径载入 `analysis/calendar_probe.py`（`analysis/` 不是包）。"""
    global _PROBE_CACHE
    if _PROBE_CACHE is None:
        _PROBE_CACHE = _sibling("calendar_probe")
    return _PROBE_CACHE


def _live_calendar(today: date, cal=None) -> tuple[set | None, dict]:
    """常量没覆盖 `today` 时的实探日历。**覆盖内不发请求**，返回 `(None, 读数)`。

    读数里的 `reason` 是给 `_refusal()` 用的病因，原样往下带：「没凭证」「上游
    停更」「与常量分歧」在这一层缩成同一个 `None`，在 09:20 就再也分不出来了。

    「能不能用」不在这里判 —— `cal.usable()` 是全仓唯一一处把「体检通过」与
    「重叠区零分歧」合起来的定义点，闹钟的 `_carries()` 调的是同一个函数。
    本函数只负责**取**与**记账**（`used/n/right/right_gap`），让分支消息能说清
    是哪一种接不上。
    """
    cal = cal or CAL
    lo, hi = cal.COVERED
    if today <= hi:
        return None, {"used": False,
                      "reason": f"常量覆盖内（{lo}~{hi}）：不取实探，零网络"}
    raw, note = _probe_module().probe()
    if raw is None:
        return None, {"used": True, "n": 0, "note": note,
                      "health": None, "reconcile": None,
                      "right": None, "right_gap": None}
    ok, judged = cal.usable(raw, today)
    h = judged["health"]
    span = cal.live_span(ok)
    return ok, {"used": True, "n": len(raw), "note": note,
                "health": h, "reconcile": judged["reconcile"],
                # 可用时取「可接班的集合」的右端，不可用时取体检读到的右端 ——
                # 两者在分歧那种情形下不同，而消息里印的必须是**实际被拒的那份**。
                "right": span[1].isoformat() if span else h.get("right"),
                "right_gap": (today - span[1]).days if span else h.get("right_gap")}


def _refusal(prev: str, today: date, live_info: dict, cal=None) -> str:
    """「判不了」那句话，按病因分四支 —— 每一支的处置都不同，所以不能合成一句。

    四种病因：没凭证 / 与常量分歧 / 上游停更 / today 在实探表外。前三种都**不该**
    靠改常量解决，第四种更不该：常量到 2027 的休市日还没有任何证据，写进去就是把
    猜测当地址，而错误形态是「一年后的某个本该休市的日子被判成缺一格」。
    """
    cal = cal or CAL
    lo, hi = cal.COVERED
    head = f"票池不可用：判不了 {prev} 是不是 {today} 的前一格 —— "
    if not live_info.get("used"):
        return (head + f"内置交易日历只覆盖 {lo}~{hi}，{today} 超出该区间。"
                f"**不在未知上放行**：补 analysis/trading_calendar.py 的休市日再跑。")
    if not live_info.get("n"):
        return (head + f"实探日历不可用（{live_info.get('note')}），而常量止于 {hi}。"
                f"**补常量在这里没用**：{today} 的休市日没有任何证据，写进去就是把猜测"
                f"当地址。先让实探可用（fuyao key / 网络 / 上游），再出表。")
    dis = (live_info.get("reconcile") or {}).get("disagree") or []
    if dis:
        a, b = live_info["reconcile"]["overlap"]
        return (head + f"实探日历与常量在 {a}…{b} 分歧 {len(dis)} 天"
                f"（{', '.join(dis[:5])}{' …' if len(dis) > 5 else ''}）。"
                f"两份独立权威打架 ⇒ 至少一边错 ⇒ 拒出表。先对账"
                f"（scripts/panel_stall_alarm.py 输出的「实探日历」两行），"
                f"**不要挑一个顺着自己结论的**。")
    h = live_info.get("health") or {}
    if not h.get("ok"):
        return (head + f"实探体检不合格：{h.get('reason')}"
                f"（{h.get('left')}…{h.get('right')}，窗口 {h.get('span_days')} 天）。"
                f"一份停更或只剩半年的表比没有表更危险：`prev_market_day` 会安静返回"
                f"更早的一天，于是面板停摆几天也判绿。**不要改常量表**，先查上游。")
    return (head + f"实探右端 {live_info.get('right')} 落后今天 {live_info.get('right_gap')} 天"
            f"（体检容忍 {cal.LIVE_MAX_RIGHT_GAP_DAYS} 天，所以体检是通过的），"
            f"{today} 仍在表外 ⇒ 它之后那一段无法作答。先把实探右端追平，"
            f"**不要改常量表**。")


def calendar_line(pinfo: dict) -> str:
    """「名单是哪一天的」这一行 —— **单一定义点**，`_header` 与 `check` 都取它。

    两个入口各写一遍格式串，加一个字段就要改两处，而漏掉的那一处不会红：
    它会继续印一句看上去对的话。少了「（实探）」这三个字，读者就分不出这个前格
    是常量算的还是实探算的 —— 而两者的失败模式不同（常量会到期，实探会停更）。
    """
    cal = pinfo.get("calendar") or {}
    axis = "实探" if cal.get("axis") == "live" else "常量"
    gap = cal.get("gap") or []
    return (f"日历={cal.get('covered')}（{axis}）前格={cal.get('expected_prev')} "
            f"{'✓相邻' if not gap else '×缺 ' + ','.join(gap)}")


def load_pool(path: Path | None = None) -> list[str]:
    """池子只有「身份」，没有「每日变量」。

    `forbidden_keys` 就是 2026-11-12 那三列的教训：前收 / 挂单价 / 昨日成交额
    一旦落到池子里就会过期，而过期是安静的。前两个现在由帧现算，第三个由日线现取。
    """
    doc = json.loads((path or POOL_JSON).read_text(encoding="utf-8"))
    syms = list(doc["symbols"])
    leaked = set(doc) & set(doc.get("forbidden_keys") or [])
    if leaked:
        raise SystemExit(f"池子里出现了每日变量键 {leaked}：它们会过期，且过期是安静的")
    if not syms:
        raise SystemExit("池子是空的")
    return syms


def load_witness(path: Path | None = None) -> dict[str, dict]:
    doc = json.loads((path or WITNESS_JSON).read_text(encoding="utf-8"))
    return {r["symbol"]: r for r in doc["rows"]}


def _enriched_quote_ts(day: str, data_dir: Path | None = None) -> int | None:
    """`kline_daily_enriched/date=<day>` 的 `quote_ts` 最大值（ms）。

    这是 `candidates()` 真正读进去的那一格的行情时刻。取不到列或取不到值 ⇒ None，
    交给 `_bar_is_final(None)` 判成「权威 batch 行」—— 与它自身的口径一致，
    不在这里另发明一条。
    """
    base = Path(data_dir or DATA_DIR) / "kline_daily_enriched" / f"date={day}"
    files = sorted(base.glob("*.parquet"))
    if not files:
        return None
    try:
        import pyarrow.parquet as pq
    except Exception:  # noqa: BLE001
        return None
    try:
        t = pq.ParquetFile(files[0]).read(columns=["quote_ts"])
    except Exception:  # noqa: BLE001
        return None
    vals = [v for v in t.column("quote_ts").to_pylist() if v is not None]
    return max(vals) if vals else None


def _prev_trade_day(today: date) -> str | None:
    """早于 `today` 的最后一个 `kline_daily` 分区。分区形态（日/月）交给 burst 认。"""
    burst = _sibling("auction_burst")
    parts, _form = burst._partition_dates()
    older = [d for d, _p in parts if d < today]
    return older[-1].isoformat() if older else None


def cohort_doc(as_of: str, data_dir: Path | None = None) -> dict:
    """`candidates()` 的薄包装：唯一产地仍是 `app.decision.l1_cohort`。"""
    from app.decision.l1_cohort import candidates

    return candidates(Path(data_dir or DATA_DIR), as_of)


def resolve_pool(today: date, mode: str = "today",
                 data_dir: Path | None = None) -> tuple[list[str], dict, dict[str, dict]]:
    """票池 + 对照收盘。返回 `(symbols, meta, refs)`，`refs` 与 `load_witness()` 同形。

    `refs` 是**帧前收的独立对照**：today 模式取日线收盘（`prev_close_raw`），
    frozen 模式取 11-11 负控。两者都不是现价，都只用来量漂移。

    为什么 today 模式也要有对照：拿 11-11 那份负控去对 11-13 的帧，分叉恒 20/20 ——
    **一道恒红的判据等于没有判据**。而 `candidates` 给出的 `prev_close_raw` 是
    同一格收盘价的另一条来路（日线），它应当与 09:20 帧里的 `f[4]` 逐票相等；
    不相等只有两种病因，都是要人看的那种（帧滞后 / 日线是快照）。
    """
    if mode == "frozen":
        syms = load_pool()
        wit = load_witness()
        refs = {s: {"prev_close": wit[s]["prev_close"], "limit": wit[s]["limit"]}
                for s in syms if s in wit}
        return syms, {"mode": "frozen", "entry": FROZEN_ENTRY, "as_of": "2026-11-11",
                      "n": len(syms), "covered": len(refs), "warnings": []}, refs

    prev = _prev_trade_day(today)
    if prev is None:
        print(f"票池不可用：没有早于 {today} 的日线分区", file=sys.stderr)
        raise SystemExit(4)
    # 第三轴（**绝对锚**）：`prev` 是不是 `today` 的前一格。
    # 上面两条相对判据都没有观测量能分开「两条线同时缺一天」与「今天本来就该用这一格」：
    # 同源落后一天时帧前收与名单日线收盘逐票相等 ⇒ `pool_frame_mismatch` 静默；
    # 若帧也整体滞后（名单同样滞后），则逐票 `stale_snapshot`(SKIP) ⇒ `degraded()`
    # 打一张每票 100 万的降级表 ⇒ **exit 0**。全 SKIP 不是拒绝出表，是照常下单。
    # 这条轴只引入一份常量（`analysis/trading_calendar.py`），不引入外部服务，
    # 且被面板 371 个分区逐日自证（见 test_calendar_constant_explains_every_panel_working_day）。
    # 常量到期之后右端改由实探承担（补丁 5）：**只在覆盖之外才取**，2026 年内
    # 行为逐字节不变、零网络。`live` 为 None 时两个函数的行为与接轴前完全相同。
    live, live_info = _live_calendar(today)
    cal_gap = CAL.gap_days(prev, today, live)
    if cal_gap is None:
        print(_refusal(prev, today, live_info), file=sys.stderr)
        raise SystemExit(4)
    if cal_gap:
        expect = CAL.prev_market_day(today, live).isoformat()
        miss = ", ".join(d.isoformat() for d in cal_gap)
        print(f"票池不可用：{prev} 之后还有交易日 {miss} 没有分区 ⇒ 名单的排序日不是 "
              f"{today} 的前一格（应为 {expect}）。这一格相对判据结构上守不住：帧与名单"
              f"同源落后一天时两列逐票相等、分叉为 0（名单也滞后时，逐票 stale_snapshot "
              f"会走 degraded() 并 exit 0）。", file=sys.stderr)
        raise SystemExit(4)
    burst = _sibling("auction_burst")
    # `_bar_is_final(None)` 是 **True** —— 它回答的是「quote_ts 全空的 batch 行算不算
    # 定版」，不回答「这个分区存不存在」。而在本函数里两者是同一个值：enriched 分区
    # 整格缺失、parquet 没有 `quote_ts` 列、pyarrow 不可用，`_enriched_quote_ts` 都返回
    # None，于是「分区不存在」被读成「定版」。2026-11-12 实测：这是 P0 之前的一层，
    # 它连「kline_daily 有没有这一格」都不需要。
    # 判据本身不动（`_bar_is_final(None)=True` 与 data_integrity 的互补性由测试绑着），
    # 只在调用处把「缺失」与「已知为空」分开问一次。
    if not (Path(data_dir or DATA_DIR) / "kline_daily_enriched" / f"date={prev}").is_dir():
        print(f"票池不可用：前一交易日 {prev} 的 enriched 分区不存在 ⇒ 无法判定是否定版。"
              f"`_bar_is_final(None)` 为 True 只表示「quote_ts 全空的 batch 行」，"
              f"不表示这个分区存在。", file=sys.stderr)
        raise SystemExit(4)
    qts = _enriched_quote_ts(prev, data_dir)
    if not burst._bar_is_final(qts):
        stamp = datetime.fromtimestamp(qts / 1000).strftime("%H:%M:%S")
        print(f"票池不可用：前一交易日 {prev} 的 enriched 分区 quote_ts={stamp} "
              f"< 定版线 {burst.CLOSE_CUTOFF:%H:%M} ⇒ 盘中快照。"
              f"拿它排序选出的是错的 cohort（身份错，不只是价错）。", file=sys.stderr)
        raise SystemExit(4)
    try:
        doc = cohort_doc(prev, data_dir)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        # enriched 分区缺失或 (date,symbol) 重复 —— 都是「算不出名单」，不是「算出空名单」。
        # 重复键这一支要指路：`_panel()` 拒绝的是**全panel**判据，任意一天的重复会让
        # **所有** as_of 拒绝（2026-11-12 实测：11-05 的 3 个键让 10-30/11-11/11-12 一起拒）。
        # 只说「ValueError 重复行」，人在 09:15 无从知道该看哪一天。
        hint = ("\n  → 重复键是全panel判据（任意一天都会让所有 as_of 拒绝）。定位方法："
                "\n    backend/.venv/Scripts/python.exe analysis/_diag_panel_dup_key.py"
                if "重复行" in str(exc) else "")
        print(f"票池不可用：candidates(as_of={prev}) 失败 {exc!r}{hint}", file=sys.stderr)
        raise SystemExit(4) from None
    rows = doc.get("candidates") or []
    # 闸门的第二条轴：**相邻**。定版只回答「这一格是不是盘后权威行」，
    # 不回答「这一格是不是今天的前一格」。`_prev_trade_day` 取的是「最后一格 < today」，
    # 所以前一交易日整格缺失时它会安静地退回再前一天 —— 2026-11-12 实测复现：
    # 只有 11-11 分区时 `resolve_pool(2026-11-13)` 原样返回 11-12 那 20 只，
    # mode=today、entry=2026-11-13、exit 0、零告警。那正是本文件声称堵死的洞。
    #
    # 判据不新增数据源：`candidates()` 自己声明了 `entry_date`（as_of 之后的下一格，
    # 没有就是 None）。名单若来自过期的那一天，它声明的入场日必然不是今天。
    declared = doc.get("entry_date")
    today_iso = today.isoformat()
    if declared is not None and declared != today_iso:
        print(f"票池不可用：candidates(as_of={prev}) 自己声明的入场日是 {declared}，"
              f"不是今天 {today_iso} ⇒ {prev} 之后还落过盘、前一交易日不是 {prev}。"
              f"退回上一天的名单与冻结名单同错（换手实测中位 5/20），故 exit 4，"
              f"不按 {prev} 的名单出表。", file=sys.stderr)
        raise SystemExit(4)
    # `declared is None` = `as_of` 就是面板的最后一格（`candidates` 取 `dates[i0+1]`，
    # 越界即 None）。它**同时**兼容两种相反的情形：
    #   (a) 今天的分区还没落（正常早晨 09:20 —— 实测 11-12 的分区是 13:34 才建的）
    #   (b) 前一交易日整格缺失（P0：11-12 丢了，面板最后一格是 11-11）
    # 旧写法 `if declared and ...` 把 (b) 一起放行了。2026-11-12 实测：喂
    # `entry_date=None` 时本函数原样返回 20 只、mode=today、exit 0、零告警。
    # 这个入参本身**不是一条轴**（`None` 对 (a)(b) 同形），所以它只做记账，
    # 判定交给上面那条绝对锚 + 下面的 `pool_frame_mismatch` 相对判据。
    today_partition_absent = declared is None
    syms = [r["symbol"] for r in rows]
    if not syms:
        print(f"票池不可用：candidates(as_of={prev}) 为空（{doc.get('reason')}）",
              file=sys.stderr)
        raise SystemExit(4)
    refs = {r["symbol"]: {"prev_close": float(r["prev_close_raw"]),
                          "limit": float(r["limit_price"])} for r in rows}
    return syms, {"mode": "today", "entry": str(today), "as_of": prev, "n": len(syms),
                  "covered": len(refs), "today_partition_absent": today_partition_absent,
                  # 产物自己声明它服务的是哪一天，以及这个声明凭什么成立：
                  # `expected_prev` 由绝对锚算、`gap` 为空就是「相邻已被绝对锚证明」。
                  # 只写 `as_of` 等于让读者自己相信 `as_of` 是前一格 —— 那正是 P0。
                  # `axis` 再往下写一层：证明它的**是谁**。常量与实探的失败模式不同
                  # （前者会到期、后者会停更），所以这个前格是谁算的必须落盘，
                  # 不能靠读的人回忆今天常量还在不在覆盖区间内。
                  "calendar": {"expected_prev": CAL.prev_market_day(today, live).isoformat(),
                               "gap": [],
                               "covered": "~".join(d.isoformat() for d in CAL.COVERED),
                               "axis": "live" if live else "constant"},
                  "warnings": list(doc.get("warnings") or [])}, refs


#: 交易所前缀。**这不是第三份实现**：`auction_burst._qq` 是同一条映射的第一份，
#: 它写了三个分支，这里只写了两个 —— 2026-11-12 复现过后果：北交所票进池
#: （近 60 个 as_of 有 31 天含 920 系，共 48 只-日，见 `_diag_pool_freshness_result.json`），
#: `sz920895` 上游返回 `v_pv_none_match`，`fetch` 静默少一只 ⇒ `no_snapshot`(SKIP)、表绿、exit 0、
#: 少买 100 万。**票池冻结时这条永远不发作，名单动态化后第一个北交所就发作。**
#: 两份实现的一致性由 test_auction_order_plan 绑定（逐个交易所对拍），不靠注释。
_EX_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def qq_symbol(sym: str) -> str:
    code, _, ex = sym.partition(".")
    return _EX_PREFIX.get(ex.upper(), "sz") + code


def parse_stamp(s: str) -> str:
    if len(s) >= 14:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    return s


def fetch(symbols: list[str], timeout: float = 8.0) -> dict[str, dict]:
    """腾讯 L1 快照。字段口径已用 11-11 收盘校准：`[6]` 量、`[37]` 额(万)、`[30]` 内容时间。"""
    codes = ",".join(qq_symbol(s) for s in symbols)
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={codes}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "replace")
    out: dict[str, dict] = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        head, _, body = line.partition("=")
        code = head[2:]
        f = body.strip('"').split("~")
        if len(f) < 40:
            continue
        sym = next((s for s in symbols if qq_symbol(s) == code), code)

        def g(i, cast=float):
            try:
                return cast(f[i])
            except (IndexError, ValueError):
                return None

        out[sym] = {
            "name": f[1],
            "last": g(3),
            "prev": g(4),
            "open": g(5),
            "vol_hand": g(6),
            "amount_wan": g(37),
            "b1p": g(9), "b1v": g(10),
            "a1p": g(19), "a1v": g(20),
            "stamp": f[30] if len(f) > 30 else "",
        }
    return out


def yday_amount_yuan(today: date, daily_dir: Path | None = None) -> tuple[dict[str, float], dict]:
    """前一交易日全天成交额（元）。**只在那一格是定版行时出数。**

    「现成」两个字有陷阱：2026-11-12 盘上 `quote_ts max = 11:30:05`，
    `kline_daily` 与 `kline_daily_enriched` 都是午盘快照。拿它当分母会把
    guard2 的比值放大 1/0.712 ≈ 1.40 倍 —— 方向是「更容易误判成残留」，
    保守但会解释错原因。判据不在这里发明：套 `auction_burst.prev_amount_from_daily`，
    它内部用的是 `data_integrity.CLOSE_CUTOFF`。
    """
    burst = _sibling("auction_burst")
    if daily_dir is not None:
        burst.DAILY_DIR = Path(daily_dir)
    info = burst.prev_amount_from_daily(today)
    if not info.get("final"):
        return {}, info
    return {s: w * 1e4 for s, w in info["wan"].items()}, info


def fallback_rows(rows: list[dict]) -> list[str]:
    """定价用的前收 ≠ 帧里给出的前收 —— 这就是「回落到常量」的形状。

    只看这一条不等式，不依赖任何两个计数器之间的一致性：
      - `prev = s.get("prev") or 常量`：`prev_field` 仍是 None，`limit` 却有了值；
      - `prev = 常量`：`prev_field` 是 None 或别的数，`prev_frame` 却是常量。
    两种都让「定价前收 == 帧前收」不成立。

    为什么不能只靠 `n_prev_moved`：那个计数器读的是行里的 `prev`。而最常见的
    回退写法是 `prev = s.get("prev") or 常量`，一旦真回退，`prev` 就**等于**常量，
    `n_prev_moved` 与 `n_limit_shifted` **同时**变 0，两个观测量一起瞎掉。
    2026-11-12 复现过：帧 `prev=None` 时状态仍是 OK、limit=11.16(11-11 常量)、
    lots=896，四个计数器全 0，表头干净。
    """
    out: list[str] = []
    for r in rows:
        if r.get("limit") is None:
            continue
        used, raw = r.get("prev_frame"), r.get("prev_field")
        # `prev_in_frame` 是**另一条独立表达式**算出来的：只要帧里没有前收，它就是 False。
        # 它不看 `prev_field`，所以 `prev_field = s.get("prev") or 常量` 这种
        # 「把常量写进同一个变量」的回退也会被它撞见 —— 2026-11-12 变异复现过：
        # 那一格 status=OK、limit=11.16、lots=896，四个计数器全 0，表头干净。
        if not r.get("prev_in_frame") or raw is None or used is None:
            out.append(r["symbol"])
        elif abs(used - raw) > 1e-9:
            out.append(r["symbol"])
    return out


def decide(snap: dict, *, witness: dict[str, dict] | None = None,
           yday: dict[str, float] | None = None,
           now: datetime | None = None) -> tuple[list[dict], dict]:
    """守卫链：只有「今天 + 竞价窗口 + 量级合理」的快照才允许进 10% 分母。

    盘前实测（09:02）`vol`/`amount` 返回的是**昨日全天**残留：
    300949 = 100,953 手 / 51,572 万 = 11-11 全天。若不加守卫，这张表会算出
    「10% 上限 5,157 万」并全票给 100 万 —— 绿、好看、分母是假的。
    """
    witness = witness or {}
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    hm = (now.hour, now.minute)
    in_window = AUCTION_START <= hm < AFTER_MATCH
    # 阶段只做标注，不做拒绝：「拒」会把表推给 degraded（固定 100 万），
    # 而连续竞价阶段 min(100万, 10%×vol) 的结果通常也正好是 100 万，拒了没有收益。
    if hm < AUCTION_START:
        phase = "pre"
    elif hm < AUCTION_MATCH:
        phase = "auction"
    elif hm < AFTER_MATCH:
        phase = "match"
    else:
        phase = "continuous"

    rows: list[dict] = []
    for sym in snap.get("_pool") or []:
        s = snap.get(sym) or {}
        # prev 只从帧 f[4] 取（当日昨收；除权日源给的已是除权参考价）。
        # 负控列只用来量漂移，绝不进 lots / amt_used —— 这条由 test 钉住。
        # prev 只从帧 f[4] 取（当日昨收；除权日源给的已是除权参考价）。
        # `prev_field` 保留「帧里到底是什么」的原样（含 None），`prev_frame` 才进定价。
        # 两个变量之间**只允许赋值**：任何 `or 常量` 都会让 fallback_rows 立刻看见。
        prev_field = s.get("prev")
        prev_frame = prev_field
        lim = float(_limit_price(prev_frame)) if prev_frame and prev_frame > 0 else None
        w = witness.get(sym) or {}
        yd = (yday or {}).get(sym)
        r = {"symbol": sym, "limit": lim, "prev_frame": prev_frame,
             "prev_field": prev_field,
             # 独立于上面两行再问一次「帧里到底有没有这一格」：不读 prev_field，
             # 所以把常量写进 prev_field 的那种回退也绕不过它。
             "prev_in_frame": "prev" in s and s.get("prev") is not None,
             "prev_witness": w.get("prev_close"), "limit_witness": w.get("limit"),
             "yday_yuan": yd}
        if not s:
            r.update(status="SKIP", why="no_snapshot", lots=0, amount=0.0)
            rows.append(r)
            continue
        if lim is None:
            # 帧里前收缺失或为 0 ⇒ 无法定价。**不回落常量**：回落就是拿过期价下单，
            # 而且整张表看起来是绿的。这一格必须 SKIP 且可被 render 一眼看见。
            r.update(status="SKIP", why="prev_missing", lots=0, amount=0.0)
            rows.append(r)
            continue
        vol = s["vol_hand"] or 0.0
        amt_wan = s["amount_wan"] or 0.0
        amt = amt_wan * 1e4
        px = s["last"] or 0.0
        stamp = parse_stamp(s["stamp"])
        # `[6]` 的单位按板块分叉: 688 系是「股」，其余是「手」。实测校准：
        #   000530 2594062×100×11.28 = 29.3 亿 ≈ amt 28.6 亿 -> 手
        #   688836 6061495×765       = 46.4 亿 ≈ amt 45.9 亿 -> 股
        lot = 1 if sym.startswith("688") else LOT
        if amt <= 0 and vol > 0 and px > 0:
            amt = vol * lot * px
        r.update(vol_hand=vol, amount=amt, last=px, open=s["open"], stamp=stamp)

        # --- 守卫 1: 时间戳必须是今天 ---
        if not stamp.startswith(today):
            r.update(status="SKIP", why=f"stale_snapshot({stamp[:10]})", lots=0)
            rows.append(r)
            continue
        # --- 守卫 2: 成交额与昨日全天额同量级 => 昨日残留 ---
        # **必须走金额口径**。用 vol 比昨日股数会撞上单位分叉（`[6]` 在 688 系是股），
        # 竞含量 5.5% 在股口径下算成 5.5，恒 >0.8 => 5 只科创板被静默跳过，
        # 且 why 写成 looks_like_yday_total，把「守卫单位错」伪装成「源没数据」。
        if yd is None:
            r["guard2"] = "no_denominator"
        elif amt / yd > GUARD2_RATIO:
            r.update(status="SKIP", why="looks_like_yday_total", lots=0, guard2="red")
            rows.append(r)
            continue
        else:
            r["guard2"] = "ok"
        # --- 守卫 3: 空量单独成因（必须在阶段判定之前，否则被掩盖）---
        if vol <= 0 or amt <= 0 or px <= 0:
            r.update(status="SKIP", why="auction_vol_empty", lots=0, alloc=0.0)
            rows.append(r)
            continue
        cap = PARTICIPATION * amt
        alloc = min(PER_NAME, cap)
        lots = int(math.floor(alloc / (lim * LOT)))
        r.update(status="OK", alloc=alloc, lots=lots, amount_used=lots * LOT * lim,
                 binding="cap_10pct" if cap < PER_NAME else "per_name_1m",
                 opens_below_limit=(px <= lim + 1e-9), phase=phase)
        rows.append(r)

    n_shift = sum(1 for r in rows
                  if r["limit"] is not None and r["limit_witness"] is not None
                  and abs(r["limit"] - r["limit_witness"]) > 1e-9)
    n_moved = sum(1 for r in rows
                  if r["prev_frame"] and r["prev_witness"]
                  and abs(r["prev_frame"] - r["prev_witness"]) > 1e-9)
    #: 「可比」= 两列都有值。`n_prev_moved` 的分母必须是它，不能是 `n_rows`：
    #: 少一只快照（北交所前缀错、停牌、上游漏票）就会让「逐票全不等」永远差一只，
    #: 判据在最需要它的那个早晨恰好不响。
    n_cmp = sum(1 for r in rows
                if r["prev_frame"] is not None and r["prev_witness"] is not None)
    return rows, {
        "in_window": in_window, "today": today, "phase": phase, "n_rows": len(rows),
        "n_limit_shifted": n_shift, "n_prev_moved": n_moved,
        "n_prev_comparable": n_cmp,
        "n_prev_missing": sum(1 for r in rows if r.get("why") == "prev_missing"),
        "n_prev_filled": len(fallback_rows(rows)),
        "n_guard2_no_denominator": sum(1 for r in rows
                                       if r.get("guard2") == "no_denominator"),
    }


def pool_frame_mismatch(meta: dict, pinfo: dict) -> str | None:
    """第二轴：**帧里的昨收** 与 **名单里的日线收盘** 逐票全不等 ⇒ 拒出表。

    这条轴不引入外部日历：`n_prev_moved` 已经逐票比过两列。逐票全不等只有两种病因
    （名单过期 / 帧整体滞后），都不能下单；少数几只不等才是除权或单票滞后，那种
    逐票报、不拦表 —— 所以判据是「全不等」，不是「有不等」。

    为什么必须和 `resolve_pool` 的 `entry_date` 闸门并存：`entry_date is None` 时
    （`as_of` 是面板最后一格）那道闸门结构性放行，而「整格缺失」恰好就是
    `entry_date is None` 的形态之一。只有帧能在那一天说话。

    分母必须是 `n_prev_comparable`（两列都有值），**不是** `n_rows`：少一只快照
    （北交所前缀错、停牌、上游漏票）就会让「全不等」永远差一只，判据在最需要它的
    那个早晨恰好不响。

    2026-11-12 实测：本函数只在 `cmd_run` 里被调用，`check` 没有它 ⇒ 20/20 全不等
    时 `check` 仍返回 0，而明早第一步跑的就是 `check`。
    """
    if pinfo.get("mode") != "today":
        return None
    n_cmp, n_moved = meta.get("n_prev_comparable") or 0, meta.get("n_prev_moved") or 0
    if n_cmp > 1 and n_moved == n_cmp:
        return (f"{n_moved}/{n_cmp} 只可比票帧前收 ≠ 名单里的日线收盘 ⇒ "
                f"名单的排序日与帧的昨收日不是同一天（名单过期或帧整体滞后）。"
                f"除权只动那几只，不会全动。")
    return None


def _header(meta: dict, yday_info: dict, pool: dict | None = None) -> list[str]:
    """表头就是本轮的验收口径 —— 它不依赖任何外部收盘价。

    `pool` 单独占一行：**名单和价格一样会过期**，而名单过期时表更绿
    （冻结的 20 只里有 4 只不是今天该买的）。这一行不写出来，就没有第二个地方会写。
    """
    final = yday_info.get("final")
    denom = (f"日线({yday_info.get('prev_trade_day')} 定版)" if final
             else f"不可用（{yday_info.get('reason') or '无日线分区'}）")
    out: list[str] = []
    if pool:
        tag = ("今日入场" if pool.get("mode") == "today"
               else f"冻结名单({pool.get('entry')} 入场)")
        out.append(f"票池={tag} as_of={pool.get('as_of')} {pool.get('n')} 只 | "
                   f"对照收盘 {pool.get('covered')} 只")
        cal = pool.get("calendar")
        if cal:
            # 「名单是哪一天的」不能只由 `as_of` 自证：`_prev_trade_day` 取「最后一格
            # < today」，前一交易日整格缺失时它会安静退回再前一天，而表是绿的。
            # 这一行是绝对锚的读数：前格由绝对锚算、缺口为空才算相邻。
            out.append(calendar_line(pool))
    out.append(f"限价来源=帧 f[4] 现算({meta['n_rows']} 只) | 分叉 "
               f"{meta['n_limit_shifted']} 只 | prev 缺失 {meta['n_prev_missing']} 只")
    out.append(f"guard2 分母={denom}")
    if pool and pool.get("n") and pool.get("covered", pool["n"]) < pool["n"]:
        miss = pool["n"] - pool["covered"]
        out.append(f"!! {miss} 只没有对照收盘 => 分叉/回退探测对它们不生效，"
                   f"表头这两列是在更小的分母上算的")
    if meta["n_prev_moved"] and not meta["n_limit_shifted"]:
        out.append(f"!! prev 动了 {meta['n_prev_moved']} 只但限价一只没动 "
                   f"=> 限价可能又回到常量了，这张表不能下单")
    if meta["n_prev_moved"] and meta["n_limit_shifted"]:
        # 正常早晨这一列是 0：帧 f[4] 就是前一交易日收盘，日线也是同一个数。
        # 非零只有三种来路，而它们在这一行上分不开 —— 所以不猜，报出来逐个核。
        out.append(f"!! {meta['n_prev_moved']} 只帧前收 ≠ 日线收盘"
                   f"（{meta['n_limit_shifted']} 只限价随之变）"
                   f"=> 帧滞后 / 除权 / 常量回退，三者在这里分不开，逐个核")
    if meta["n_prev_filled"]:
        out.append(f"!! {meta['n_prev_filled']} 只前收缺失却给出了挂单价 "
                   f"=> 回落到常量了（prev 缺失必须是 SKIP）")
    if meta["n_guard2_no_denominator"]:
        out.append(f"!! {meta['n_guard2_no_denominator']} 只没有昨日成交额分母 "
                   f"=> guard2 对它们未生效，昨天的残留量识别不出来")
    return out


def render(rows: list[dict]) -> None:
    ok = [r for r in rows if r["status"] == "OK"]
    skip = [r for r in rows if r["status"] != "OK"]
    print(f"{'symbol':<11}{'limit':>8}{'last':>8}{'vol[6]':>11}{'竞价额(万)':>11}"
          f"{'10%上限(万)':>12}{'下单股':>8}{'金额(万)':>9}  绑定")
    tot_amt = 0.0
    for r in rows:
        if r["status"] != "OK":
            lim = r.get("limit")
            lim_s = f"{lim:.2f}" if lim else "?"
            print(f"{r['symbol']:<11}{lim_s:>8}"
                  f"{'-':>8}{'-':>11}{'-':>11}{'-':>12}{'SKIP':>8}{'-':>9}  {r.get('why','')}")
            continue
        tot_amt += r["amount_used"]
        flag = "" if r["opens_below_limit"] else "  <限价,不成交"
        print(f"{r['symbol']:<11}{r['limit']:>8.2f}{r['last']:>8.2f}"
              f"{r['vol_hand']:>11.0f}{r['amount']/1e4:>11.0f}"
              f"{PARTICIPATION*r['amount']/1e4:>12.1f}{r['lots']*LOT:>8d}"
              f"{r['amount_used']/1e4:>9.1f}  {r['binding']}{flag}")
    n_bind = sum(1 for r in ok if r["binding"] == "cap_10pct")
    n_go = sum(1 for r in ok if r["opens_below_limit"])
    exp_amt = sum(r["amount_used"] for r in ok if r["opens_below_limit"])
    ph = ok[0].get("phase") if ok else "-"
    print(f"\n阶段={ph}  可下单 {len(ok)} / 跳过 {len(skip)} | 10%上限绑定 {n_bind} / "
          f"每票100万绑定 {len(ok)-n_bind}")
    if ph == "continuous":
        print("  !! 已过 09:25, vol 混入连续竞价 => 10% 会被高估, 此时下单已晚")
    print(f"挂单总额 {tot_amt/1e4:.1f} 万 (备资 {BUDGET/1e4:.0f} 万, 余 "
          f"{(BUDGET-tot_amt)/1e4:.1f} 万) | 虚拟价已低于限价 {n_go}/{len(ok)}")
    print(f"预计成交(按 09:20 虚拟价) {n_go} 只 / {exp_amt/1e4:.1f} 万 "
          f"| 未成交 {len(ok)-n_go} 只挂单作废")
    if skip:
        print("跳过: " + ", ".join(f"{r['symbol']}({r.get('why')})" for r in skip))


def degraded(rows: list[dict], yday_info: dict) -> None:
    """全表读不到 => 系统性退化: 按用户授权走固定 20×100 万（备资仍 2000 万）。

    这与「个别票读不到就跳过」是两条不同的规则，不能混成一张全 SKIP 表。
    但「读不到」有两种病因，必须分开报：stale_snapshot 是源真的没有今日数据，
    其它原因（如 no_snapshot）是本地解析或字段缺失 —— 处理动作不同。
    """
    whys = sorted({r.get("why", "?") for r in rows})
    stale = all(str(w).startswith("stale_snapshot") for w in whys)
    print(f"\n*** DEGRADED: 无可用竞价量 ({', '.join(whys)}) ***")
    if stale:
        print("    病因=源返回的全部是昨日收盘快照 (今日数据尚未生成), 不是字段缺失。")
    else:
        print("    病因=非时间戳原因, 先查解析/字段口径, 不要直接当「源不可用」。")
    print("    10% 规则无法实时计算, 改走固定每票 100 万（用户已授权的降级路径）。")
    print(f"    guard2 分母={yday_info.get('reason') or '不可用'}")
    print(f"{'symbol':<11}{'limit':>9}{'虚拟价':>9}{'距限价':>9}{'成交?':>8}"
          f"{'下单股':>9}{'金额(万)':>10}  风险")
    tot = 0.0
    n_risk = 0
    n_go = 0
    for r in rows:
        lim = r.get("limit")
        if not lim or lim <= 0:
            print(f"{r['symbol']:<11}{'?':>9}{'-':>9}{'-':>9}{'-':>8}{'-':>9}{'-':>10}"
                  f"  {r.get('why', '')}")
            continue
        lots = int(math.floor(PER_NAME / (lim * LOT)))
        amt = lots * LOT * lim
        tot += amt
        px = r.get("last") or 0.0
        go = px > 0 and px <= lim + 1e-9
        n_go += go
        gap_bp = (px / lim - 1) * 1e4 if px > 0 else float("nan")
        # 降级路径下不能算 10%：分母缺失时不编一个 p25 出来，只报「不可判」。
        cap10 = PARTICIPATION * r["yday_yuan"] if r.get("yday_yuan") else None
        risk = "不可判(无分母)" if cap10 is None else ("可能破10%" if cap10 < PER_NAME else "安全")
        n_risk += cap10 is not None and cap10 < PER_NAME
        print(f"{r['symbol']:<11}{lim:>9.2f}{px:>9.2f}{gap_bp:>+9.0f}"
              f"{('是' if go else '否'):>8}{lots*LOT:>9d}{amt/1e4:>10.1f}  {risk}")
    print(f"合计挂单 {tot/1e4:.1f} 万 / 预计成交 {n_go}/{len(rows)} 只 "
          f"≈ {n_go*PER_NAME/1e4:.0f} 万（成交价=开盘价, 不高于限价）")


def cmd_plan() -> int:
    """离线：只打印负控表。**这不是明天的挂单价**，它是 11-11 的冻结对照。"""
    syms = load_pool()
    wit = load_witness()
    print(f"{'symbol':<11}{'prev(11-11)':>13}{'limit':>9}{'现算':>9}{'Δ分':>6}")
    n_shift = 0
    for sym in syms:
        w = wit.get(sym)
        if not w:
            print(f"{sym:<11}{'?':>13}")
            continue
        now_lim = float(_limit_price(w["prev_close"]))
        d = int(round((now_lim - w["limit"]) / 0.01))
        n_shift += d != 0
        print(f"{sym:<11}{w['prev_close']:>13.2f}{w['limit']:>9.2f}{now_lim:>9.2f}{d:>+6d}")
    print(f"\n{len(syms)} 只 / 负控 as_of=2026-11-11（永不更新，更新即关掉 revert 探测）")
    print(f"负控第三列与 `_limit_price(负控前收)` 分叉 {n_shift}/{len(syms)} 只 —— "
          f"说明那一列本来就是同一个函数的结果，**不是独立校验**。")
    print("`run` 不读本表：挂单价在运行时由帧 f[4] 现算。")


def cmd_check(pool_mode: str = "today", data_dir: Path | None = None) -> int:
    """预检：帧 f[4]（当日昨收）vs **同一格的日线收盘**。一把尺，不是闸。

    为什么不再拿 11-11 负控当对照：从 11-13 起那份负控记的是 11-11 收盘，分叉恒
    20/20 —— **恒红的判据等于没有判据**。today 模式的对照是 `candidates()` 带的
    `prev_close_raw`，即同一格收盘价的另一条来路，正常应当逐票相等；不相等只有两种
    病因（帧滞后 / 日线是盘中被截断的那一版），都要人看。
    """
    syms, pinfo, refs = resolve_pool(datetime.now().date(), pool_mode, data_dir)
    snap = fetch(syms)
    if not snap:
        print("抓不到快照", file=sys.stderr)
        return 3
    tag = "今日入场" if pinfo["mode"] == "today" else f"冻结({pinfo['entry']})"
    print(f"票池={tag} as_of={pinfo['as_of']} {len(syms)} 只 | "
          f"对照=日线收盘 {pinfo['covered']} 只")
    cal = pinfo.get("calendar")
    if cal:
        print(calendar_line(pinfo)
              + f" | 今日分区{'未落（正常早晨）' if pinfo.get('today_partition_absent') else '已落'}")
    print(f"{'symbol':<11}{'prev帧':>9}{'对照':>9}{'Δbp':>8}{'limit帧':>9}{'限价Δ股':>10}")
    n_shift = n_moved = n_missing = n_noref = 0
    for sym in syms:
        s = snap.get(sym) or {}
        ref = refs.get(sym) or {}
        prev = s.get("prev")
        prev_c = ref.get("prev_close")
        lim_c = ref.get("limit")
        held = f"{lim_c:.2f}" if lim_c else "?"
        ref_s = f"{prev_c:.2f}" if prev_c else "?"
        if prev is None or prev <= 0:
            n_missing += 1
            print(f"{sym:<11}{'?':>9}{ref_s:>9}{'-':>8}{'-':>9}{held:>10}{'-':>10}"
                  f"  prev_missing")
            continue
        lim = float(_limit_price(prev))
        if prev_c is None or lim_c is None:
            # 池子里有新票、对照表里没有它 —— 这不是「通过」，是判不了。
            n_noref += 1
            print(f"{sym:<11}{prev:>9.2f}{'-':>9}{'-':>8}{lim:>9.2f}{'-':>10}  无对照收盘")
            continue
        dbp = (prev / prev_c - 1) * 1e4
        lots_new = math.floor(PER_NAME / (lim * LOT)) * LOT
        lots_old = math.floor(PER_NAME / (lim_c * LOT)) * LOT
        n_shift += abs(lim - lim_c) > 1e-9
        n_moved += abs(prev - prev_c) > 1e-9
        print(f"{sym:<11}{prev:>9.2f}{prev_c:>9.2f}{dbp:>+8.0f}"
              f"{lim:>9.2f}{lots_new - lots_old:>+10d}")
    n_cmp = len(syms) - n_missing - n_noref
    print(f"\n分叉 {n_shift}/{n_cmp} 只可比（prev 缺失 {n_missing} 只，"
          f"无对照 {n_noref} 只，名单 {len(syms)} 只）。0 => 帧给出的昨收与日线收盘逐票相等。")
    if n_missing:
        print("!! 有票帧里没有前收 => `run` 会把该票判 prev_missing(SKIP)，不回落常量。")
    if n_noref:
        print(f"!! {n_noref} 只没有对照收盘 => 分叉这一列在更小的分母上算的，"
              f"别把它读成「全部一致」。")
    if n_moved and not n_shift:
        print(f"!! prev 动了 {n_moved} 只但限价一只没动 => 限价可能又回到常量了。")
    # 第二轴：`check` 是明早的第一道命令，所以它必须和 `run` 用同一条判据。
    # 2026-11-12 实测：只有 `run` 有这条轴时，20/20 全不等 ⇒ `check` 返回 0。
    bad = pool_frame_mismatch(
        {"n_prev_comparable": n_cmp, "n_prev_moved": n_moved}, pinfo)
    if bad:
        print(f"!! 不可下单：{bad}", file=sys.stderr)
        return 4
    if n_missing:
        return 5
    if n_noref:
        return 6
    return 0


def cmd_run(save: bool, pool_mode: str = "today",
            data_dir: Path | None = None) -> int:
    now = datetime.now()
    syms, pinfo, refs = resolve_pool(now.date(), pool_mode, data_dir)
    snap = fetch(syms)
    if not snap:
        print("抓不到快照", file=sys.stderr)
        return 3
    snap["_pool"] = syms
    yday, yday_info = yday_amount_yuan(now.date())
    rows, meta = decide(snap, witness=refs, yday=yday, now=now)
    print(f"快照 {now:%Y-%m-%d %H:%M:%S}  源=qt.gtimg.cn  样本 {len(snap)-1}/{len(syms)}")
    for line in _header(meta, yday_info, pinfo):
        print(line)
    for warn in pinfo.get("warnings") or []:
        print(f"   候选清单告警: {warn}")
    # 第二轴：**名单的排序日**必须与**帧的昨收日**是同一天（判据见 pool_frame_mismatch）。
    bad = pool_frame_mismatch(meta, pinfo)
    if bad:
        print(f"!! 拒出表：{bad}", file=sys.stderr)
        return 4
    if not meta["in_window"]:
        print(f"!! 阶段={meta['phase']} (非 09:20-09:26): "
              "结果只用于演练或复盘, 不可作为下单依据")
    print()
    if all(r["status"] != "OK" for r in rows):
        degraded(rows, yday_info)
    else:
        render(rows)
    if save:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        out = RAW_DIR / f"{now:%Y%m%d}_order.jsonl"
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                                 "pool": pinfo,
                                 "snap": {k: v for k, v in snap.items() if k != "_pool"},
                                 "meta": meta}, ensure_ascii=False) + "\n")
        print(f"\n快照已存 -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="集合竞价下单规模表")
    ap.add_argument("mode", nargs="?", default="plan", choices=("plan", "check", "run"))
    ap.add_argument("--save", action="store_true", help="run 时落下当日快照（按日定名）")
    ap.add_argument("--pool", choices=("today", "frozen"), default="today",
                    help="today=按前一交易日 cohort 现算名单；frozen=11-12 冻结名单（演练用）")
    ap.add_argument("--rehearsal", action="store_true",
                    help="允许在非入场日使用冻结名单（结果不可下单）")
    args = ap.parse_args(argv)
    if args.mode in ("check", "run") and args.pool == "frozen" and not args.rehearsal:
        print(f"冻结名单是 {FROZEN_ENTRY} 入场的，今天用它买错的是**名单**不是价格；"
              f"确认只是演练请加 --rehearsal。", file=sys.stderr)
        return 4
    if args.mode == "check":
        return cmd_check(args.pool)
    if args.mode == "run":
        return cmd_run(args.save, args.pool)
    cmd_plan()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
