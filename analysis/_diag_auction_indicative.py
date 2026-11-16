"""集合竞价的「虚拟匹配量」在 09:24 就已经把最终成交量说出来了 —— 于是 κ 不必预测。

## 为什么要这个产物

`_diag_l1_weight_frontier` 把容量写成 `T_max(α) = Σ λ·κ_i·A_i`，其中
`κ_i = 竞价成交额 / 昨成交额`、`λ` 是单票参与率上限（`PARTICIPATION_CAP`）。整条链
的前提是 **κ_i 事前不可知**，因此：

1. 需要 κ 的历史面板（L2 采购 / 日更采样 / 30 天 ICC）；
2. 容量只能报成 κ 分布的一个分位（见该产物 `gate80` 的 `t80_wan`）；
3. `π_i ≤ PARTICIPATION_CAP` 只能事后核，不能事前保证。

这个前提在数据上是错的。开盘集合竞价的单价撮合阶段（`AUCTION_OPEN` 起至 `DEADLINE`），行情快照的
`b1p == a1p`（虚拟成交价）且 `b1v == a1v`（**虚拟匹配量**），且**单调不减**。
09:20 之后禁止撤单 ⇒ 匹配量非降是**交易所规则**，不是统计规律 ⇒
`V_final ≥ V_obs` 是一条硬下界，不是估计。

于是 `x_i ≤ V_obs_i / V_OBS_DIVISOR` 就已经保证参与率 ≤ `PARTICIPATION_CAP`（下单后
总量为 `V_others + x`），不需要 κ 预测、不需要 L2、不需要 ICC。

## 本脚本量化四件事

1. **可见份额**：各读点（`HH:MM:SS`，见下）的 `V_obs / V_final` 分布（跨票）。
2. **排序保真**：`log V_obs` 与 `log V_final` 的 Pearson / Spearman（含并列秩）。
3. **末端增量**：`(V_final − V_obs(t)) / V_final`，即「最后这段时间又涌进来多少」。
   它决定常量倍数能不能用：末端份额越大，早读点的倍数越不可移植。
4. **单位陷阱**：科创板 `[6]` 成交量字段是**股**，其余是**手**；而 `b1v` **两种板都是手**。
   用 `amount·1e4/(vol·price)` 反解每票单位并要求落在 {1, 100} 的 1% 内，
   否则直接报错 —— 这一条错了，688 的倍数会凭空大 `LOT` 倍。

## 输入

`--frames` 指向 `qt.gtimg.cn` 连续快照的 jsonl（`{"ts","snap":{sym:{...}}}`）。
原始帧一律 `--archive` 落成 parquet 存 `data/research/auction_indicative/frames/`：
**竞价数据不可回补**，帧是唯一证据，汇总表替不了它（`_diag_auction_indicative_result.json`
只留派生表 + `source.sha256`，原文丢了就再也算不出来）。归档列必须自足：
`open` 与二~五档都要在，否则「竞价价 vs 开盘价」这类实验无法从证据本体复现。

## 口径（写清是为了不出第二种读法）

- `date` 一律是**入场日**（与 `l1_cohort` 一致）；本产物只有一天。
- `V_final` = 该票 stamp 落在 `[09:25:00, 09:30:00)` 的**首帧** `amount_wan`（万元）。
  逐票取，不要求整帧所有票同时到位（交易所推送有秒级先后）。
- `V_obs(t)` = **ts ≤ t 的最后一帧** 的 `b1v × LOT × b1p / 1e4`（万元）；`t` 精确到秒。
- `A`（昨成交额）= 首帧的 `amount_wan`（该帧 stamp 是前一交易日，字段仍是全天成交额）。
- `κ = V_final / A`，用**同一列**（amount_wan）相除，不跨口径。

## 读点必须精确到秒（2026-11-12 修正）

首版 `frame_at()` 拿 `HH:MM` 字符串比大小，`<= "09:24"` 实际选中的是 09:24:59 那一帧：
**标签写 09:24、值却是 09:24:59 的**。这个 59 秒偏移同时污染三处——

1. 09:24 的倍数被系统性低估（最后一分钟恰恰是流入最猛的一分钟）；
2. 「退回 `V_obs` 原值只损失多少头寸」的分母跟着错（09:24:00 与 09:24:59 是两个数）；
3. 验收判据（倍数分位 ≤ 阈值）的结论会随读点翻转，于是判据本身不成立。

现在探针一律写全 `HH:MM:SS`，并在 `probes` 里记录**实际用到的帧时间** `used_ts`、
距 09:25:00 的申报余量 `buffer_s`、以及该票末端增量份额。`buffer_s <= 0` 的读点
直接标 `actionable=false`（能算不能下单），不再混进容量口径。

## 三条二值守卫（2026-11-12 补）

判据层的问题不是标签写错，是**有的判据永不红**：`b1p==a1p` 在撮合期恒成立，
因为射程本来就被定义成「只包含它成立的那个区间」；而竞价前那几分钟的帧
`b1v==a1v==0` 同样让等式成立、比值却是 0 —— 两个读数互相矛盾。
替代是三条能被打红的守卫，每条在 `backend/tests/test_diag_auction_read_points.py`
里都配了一个**故意错的输入**（全绿的守卫不是守卫，是装饰）：

1. `stamp[:8] == 交易日`：内容时间 ≠ 拉取时间。缺了它，「昨日收盘快照」与
   「今日撮合帧」在字段上长得一样；
2. 帧级撮合率 ≥ `FRAME_MATCH_MIN`，且要求 `b1v > 0`：把「上游在撮合模式」
   与「已有量」分开；
3. 09:20 之后 `V_obs > 0`：为 0 说明该票不在撮合里，静默丢票会让日聚合偏向活跃票。

判红不等于丢证据：产物照写，`guards.failures` 非空 + 退出码 2。

## 采集

N=20、一天只够证明机制，不够定倍数分布。`--collect` 用 `qt.gtimg.cn` 免费快照
全市场扫一遍，落在 `data/research/auction_indicative/<date>.jsonl`。
**点采样不够**：它要求机器恰好在那一秒醒着，而且只能给读点的点值、给不出路径。
基准做法是在竞价时段连续扫（`--burst --interval N`），任何时刻的倍数都能事后算，
掉几帧也不影响。**轮询间隔不许只写在文档里**：量化值逐次记进
`guards.frame_interval_s`（p50/p95/max/直方图），声明与实测不一致时以实测为准。
倍数分布要在**应用集**（`l1_cohort` 候选）上算，不是在全市场或 20 只流动性好的票上算；
判据取**下分位**（`V_final/V_obs` 小了才会破 `PARTICIPATION_CAP` 那条线），不是上分位。

**下分位在 N=20 上不可识别。** `p05` 的分布无关置信区间由次序统计量给
（见 `_order_stat_ci`）：下界秩由二项尾概率定，n=20 时满足所需覆盖概率的秩不存在，
只剩上界。所以「p05 ≥ 阈值」这个方向**无法用 20 只票证伪**，只能反过来说
「p05 的上界已经低到危险」，或者把样本扩到 `l1_cohort` 候选集/全市场。

## 用法

    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py --archive
    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py --frames <path> --archive
    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py --collect --label 092430
    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py --frames data/research/auction_indicative
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics as st
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAMES = ROOT / ".workbuddy" / "tmp" / "auction_burst.jsonl"
OUT = ROOT / "analysis" / "_diag_auction_indicative_result.json"
#: 原始快照落这里（运行时数据，默认不进版本控制）；**帧的 parquet 归档**是证据本体，
#: 按天逐文件放行到 `frames/`（见 .gitignore，每个新文件都要显式加一行）。
COLLECT_DIR = ROOT / "data" / "research" / "auction_indicative"
FRAMES_DIR = COLLECT_DIR / "frames"

#: 读点一律 `HH:MM:SS`：写 `HH:MM` 会静默取到该分钟的**最后一帧**（少 59 秒）。
PROBES = ("09:20:00", "09:22:00", "09:23:00", "09:24:00", "09:24:30", "09:24:59")
DEADLINE = "09:25:00"
#: 读点与帧的对齐容差（秒）。超过就是帧太稀或标签写错，判红。
PROBE_TOL_S = 5
#: 帧级撮合率下限：该帧里「`b1p==a1p` 且 `b1v==a1v` 且 `b1v>0`」的票占比。
#: 低于它就是竞价前的空帧或连续竞价帧，不能当撮合帧用。单一产地，docstring 引名字不引数。
FRAME_MATCH_MIN = 0.9
#: 距 09:25:00 少于这么多秒的读点只能算、不能下单（不足以下单+回报）。
ACTION_MIN_BUFFER_S = 30
#: 少于这么多帧的一天不归档：那是 `--collect` 的定点快照，没有路径可算。
MIN_ARCHIVE_FRAMES = 3
#: 单票参与率上限。单一产地：docstring 与 `/ V_OBS_DIVISOR` 都引这个名字，不引数。
PARTICIPATION_CAP = 0.1
#: 由上限反解出的分母：`x <= V_obs / V_OBS_DIVISOR` ⇔ `x / (V_others + x) <= 上限`。
#: 用 `V_obs` 而不是 `V_others` 是保守方向（`V_obs >= V_others`），不是等号。
V_OBS_DIVISOR = int(round(1 / PARTICIPATION_CAP)) - 1
#: 开盘集合竞价的起点；终点是申报截止 `DEADLINE`，不是窗口结束。
AUCTION_OPEN = "09:15:00"

#: 科创板 `[6]` 是股，其余是手；`b1v` 两种板都是手 ⇒ 虚拟匹配额统一 ×100。
LOT = 100


def _sec(t: str) -> int:
    """`HH:MM:SS` → 当日秒。越界直接抛，不给静默兜底。"""
    h, m, s = int(t[0:2]), int(t[3:5]), int(t[6:8])
    return h * 3600 + m * 60 + s


def _hhmmss(f: dict) -> str:
    """帧的**轮询时间**（精确到秒）。标签与它比，不比到分钟。"""
    return f["ts"][11:19]


def _stamp_day(v: dict) -> str:
    return str(v.get("stamp", ""))[:8]


def load(path: Path) -> list[dict]:
    """`path` 可以是单文件，也可以是目录（按文件名排序拼起来）。"""
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    out: list[dict] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    out.sort(key=lambda r: r["ts"])
    return out


# ---------------------------------------------------------------- 采集

def _qq(sym: str) -> str:
    num = sym.split(".")[0]
    return ("sh" if sym.endswith("SH") else "sz") + num


def fetch_batch(symbols: list[str], timeout: float = 8.0) -> dict[str, dict]:
    """一次最多 60 只。

    字段索引（2026-11-12 实测一遍，不再靠记忆）：`9..18` 是买一~买五的
    **价/量交替**（9,10 = 买一价,买一量；11,12 = 买二；…17,18 = 买五），
    `19..28` 是卖一~卖五，`30` 是内容时间，`37` 是成交额（万元）。

    二~五档此前被直接丢掉。补它不是「顺手多存几个字段」，而是为了**证伪一个假设**：
    竞价期的虚拟簿到底有没有二~五档。连续竞价期实测五档齐全（2026-11-12 11:54），
    但集合竞价期的上证 Level-1 快照通常只发虚拟匹配一档 —— 若明天 09:19 补采回来
    全是 0，那就是一次成功的证伪，省掉后面按「五档簿」设计冲击实验的时间。
    **在拿到那一跑之前，不许把补字段说成「能测冲击弹性」。**
    """
    codes = ",".join(_qq(s) for s in symbols)
    url = f"https://qt.gtimg.cn/q={codes}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "replace")
    out: dict[str, dict] = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        head, _, body = line.partition("=")
        code = head[2:][2:]
        f = body.strip('"').split("~")
        if len(f) < 40:
            continue
        sym = f"{code}.{'SH' if head[2:4] == 'sh' else 'SZ'}"

        def g(i: int, cast=float):
            try:
                return cast(f[i])
            except (IndexError, ValueError):
                return None

        out[sym] = {"name": f[1], "last": g(3), "prev": g(4), "open": g(5),
                    "vol_hand": g(6), "amount_wan": g(37),
                    "b1p": g(9), "b1v": g(10), "a1p": g(19), "a1v": g(20),
                    # 二~五档：价/量交替。竞价期是否有值待 2026-11-13 实测。
                    "b2p": g(11), "b2v": g(12), "b3p": g(13), "b3v": g(14),
                    "b4p": g(15), "b4v": g(16), "b5p": g(17), "b5v": g(18),
                    "a2p": g(21), "a2v": g(22), "a3p": g(23), "a3v": g(24),
                    "a4p": g(25), "a4v": g(26), "a5p": g(27), "a5v": g(28),
                    "stamp": f[30] if len(f) > 30 else ""}
    return out


def universe() -> list[str]:
    import polars as pl

    df = pl.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")
    return [s for s in df.filter(pl.col("type") == "stock")["symbol"].to_list()]


def _sweep(syms: list[str], workers: int, batch: int) -> dict[str, dict]:
    from concurrent.futures import ThreadPoolExecutor

    batches = [syms[i:i + batch] for i in range(0, len(syms), batch)]
    snap: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(fetch_batch, batches):
            snap.update(part)
    return snap


def _append(out_dir: Path, rec: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{rec['ts'][:10]}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def collect(label: str, out_dir: Path, workers: int = 16, batch: int = 80) -> int:
    """单次全市场快照。只用于「管线是否活着」，不能当倍数分布的样本（一个点算不出路径）。"""
    from datetime import datetime

    syms = universe()
    snap = _sweep(syms, workers, batch)
    rec = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "label": label, "snap": snap}
    _append(out_dir, rec)
    print(f"[{rec['ts']}] {label}: {len(snap)}/{len(syms)} 只有快照 → {out_dir}")
    return len(snap)


def burst(out_dir: Path, interval: float, until: str, workers: int = 16,
          batch: int = 80, max_sweeps: int = 0, since: str = "") -> int:
    """09:15–09:26 连续扫全市场，一次一轮，轮间隔 `interval` 秒。

    为什么要连续而不是三个定点：定点要求机器恰好在那一秒醒着（一天只有一次机会），
    而且只给点值、给不出路径 —— 「最后一分钟涌进来多少」这个决定常量倍数能不能用的
    量，必须靠路径才看得见。连续扫掉几帧无所谓，定点漏一次就少一天。
    """
    from datetime import datetime
    import time

    syms = universe()
    stop = _sec(until)
    n = 0
    if since:
        from datetime import datetime as _dt
        import time as _t
        while _sec(_dt.now().strftime("%H:%M:%S")) < _sec(since):
            _t.sleep(min(5.0, max(0.2, _sec(since) - _sec(_dt.now().strftime("%H:%M:%S")))))
    while True:
        t0 = time.time()
        now = datetime.now()
        if _sec(now.strftime("%H:%M:%S")) >= stop:
            break
        snap = _sweep(syms, workers, batch)
        rec = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "label": "burst",
               "snap": snap}
        _append(out_dir, rec)
        n += 1
        print(f"[{rec['ts']}] burst#{n}: {len(snap)}/{len(syms)} 只  ({time.time()-t0:.1f}s)", flush=True)
        if max_sweeps and n >= max_sweeps:
            break
        time.sleep(max(0.0, interval - (time.time() - t0)))
    return n


#: 归档要存的档位列。**一档不够自足**：`open` 不在 `snap[:, 9..28]` 里，而
#: 「竞价虚拟价 vs 开盘价」这个位移实验只在 `last` 上做得到 —— `last` 在 final 帧
#: 恰好等于开盘成交价，但那是巧合不是契约。归档缺 `open`，那份实验就无法从证据本体复现。
LEVEL_COLS = ("b1p", "b1v", "a1p", "a1v",
              "b2p", "b2v", "b3p", "b3v", "b4p", "b4v", "b5p", "b5v",
              "a2p", "a2v", "a3p", "a3v", "a4p", "a4v", "a5p", "a5v")


def archive(frames: list[dict], out_dir: Path, tag: str) -> Path:
    """把原始帧落成 parquet 长表。**竞价数据不可回补**，这份才是证据本体。"""
    import polars as pl

    recs = []
    for f in frames:
        for s, v in f["snap"].items():
            rec = {
                "ts": f["ts"], "symbol": s, "stamp": str(v.get("stamp", "")),
                "name": v.get("name"), "last": v.get("last"), "prev": v.get("prev"),
                "open": v.get("open"),
                "vol_hand": v.get("vol_hand"), "amount_wan": v.get("amount_wan"),
            }
            for c in LEVEL_COLS:
                rec[c] = v.get(c)
            recs.append(rec)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(recs)
    out = None
    skipped = []
    for day in sorted({r["ts"][:10] for r in recs}):
        sub = df.filter(pl.col("ts").str.slice(0, 10) == day)
        n_frame = sub["ts"].n_unique()
        out = out_dir / f"{day}_{tag}.parquet"
        # ① 单帧日不是 burst（是 `--collect` 的定点快照），归了也没路径可算；
        # ② 归档只许变大：一次「输入只有 1 帧」的分析曾把 569 帧的归档覆盖成 5,532 行 × 1 帧。
        # 帧不可回补，所以宁可留着旧的多，也不接受少（要换证据就换 tag，别覆盖）。
        # 两种情况都只跳过这一天，不中断整批 —— 否则一天的问题会让后面的日子也不落盘。
        if n_frame < MIN_ARCHIVE_FRAMES:
            skipped.append(f"{day}: 只有 {n_frame} 帧")
            continue
        if out.exists():
            old_rows = pl.read_parquet(out).height
            if old_rows > sub.height:
                skipped.append(f"{day}: 现有归档 {old_rows} 行 > 本次 {sub.height} 行")
                continue
        sub.write_parquet(out, compression="zstd")
        print(f"归档 {sub.height} 行 × {n_frame} 帧 → {out.name}（{out.stat().st_size/1024:.0f} KB）")
    for sk in skipped:
        print(f"· 跳过归档 {sk}")
    return out



def unit_factor(frame: dict, sym: str) -> float:
    """反解 `[6]` 的单位：amount(万元)·1e4 / (vol · price) ∈ {1, 100}。"""
    v = frame["snap"][sym]
    vol, amt, px = v.get("vol_hand") or 0.0, v.get("amount_wan") or 0.0, v.get("last") or 0.0
    if not vol or not px:
        raise ValueError(f"{sym}: 无成交/无价格，无法判定单位")
    u = amt * 1e4 / (vol * px)
    for cand in (1.0, 100.0):
        if abs(u - cand) / cand < 0.01:
            return cand
    raise ValueError(f"{sym}: 单位既不是 1 也不是 100（{u:.3f}）——口径变了，先查字段")


def indicative_amt(frame: dict, sym: str) -> float:
    """虚拟匹配额（万元）。`b1v` 是手 ⇒ ×100 股；无匹配时 b1v=0。"""
    v = frame["snap"][sym]
    q = v.get("b1v") or 0.0
    px = v.get("b1p") or v.get("last") or 0.0
    return q * LOT * px / 1e4


def _rank(xs: list[float]) -> list[float]:
    idx = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[idx[j + 1]] == xs[idx[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[idx[k]] = avg
        i = j + 1
    return out


def corr(a: list[float], b: list[float]) -> float:
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else float("nan")


def quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _frame_match_rate(f: dict) -> float:
    """帧级撮合率：`b1p==a1p` **且** `b1v==a1v` **且** `b1v>0` 的票占比。

    第三个条件是这条守卫的全部意义。去掉它，09:10–09:15 的帧会得到「撮合率 100%、
    虚拟匹配量 0」——两个读数互相矛盾，而首版正是把那个 100% 当成
    「该帧是有效竞价帧」的证据。`b1p==a1p` 只说明上游在撮合模式，不说明已有量。
    """
    n = ok = 0
    for v in f["snap"].values():
        if v.get("b1p") is None:
            continue
        n += 1
        if (v.get("b1p") == v.get("a1p") and v.get("b1v") == v.get("a1v")
                and (v.get("b1v") or 0.0) > 0.0):
            ok += 1
    return ok / n if n else 0.0


def analyze_day(frames: list[dict]) -> tuple[list[dict], dict]:
    """一天的配对：`V_obs(t)` vs 该票的最终竞价额，`t` 精确到秒。"""
    day = frames[-1]["ts"][:10].replace("-", "")
    syms = sorted({s for f in frames for s in f["snap"]})

    # 逐票取 V_final：stamp 落在 [09:25:00, 09:30:00) 的**首帧**的 amount_wan。
    # 逐票而不是逐帧：交易所推送有秒级先后（092500 / 092501 / 092502 都有），
    # 要求整帧所有票同时到位会把 8/20 只无声丢掉 —— 那是集合选择，不是缺失。
    final: dict[str, tuple[float, str, str]] = {}
    for f in frames:
        for s, v in f["snap"].items():
            if s in final:
                continue
            stp = str(v.get("stamp", ""))
            if stp[:8] == day and "092500" <= stp[8:14] < "093000":
                final[s] = (v.get("amount_wan") or 0.0, stp[8:14], f["ts"])
    if not final:
        return [], {"date": day, "skipped": "没有 stamp 落在 [09:25:00,09:30:00) 的快照"}

    # ---------------------------------------------------------------- 守卫
    # 三条二值守卫，每条都配一个「故意做错的输入」在
    # `backend/tests/test_diag_auction_read_points.py` 里必须把它打红。
    # 全绿的守卫如果永远不可能红，就不是守卫，是装饰。
    veto: dict = {"stale_obs": [], "zero_obs": [], "frame_match_rate": {},
                  "prev_amt_stale": []}

    # ① 内容时间 ≠ 拉取时间。09:06:43–09:10:04 的帧 stamp 是**前一交易日 16:14**，
    #    它们的 offset_s=0、buffer_s 很大 ⇒ 首版每一条守卫都判 actionable=True。
    #    `A`（昨成交额）恰好要靠这种帧，但那是**主动取**，不是**误取**。
    f0 = frames[0]
    for s, v in f0["snap"].items():
        if _stamp_day(v) == day:
            # 首帧已是当日 ⇒ A 从「昨全天成交额」变成「今日成交额」，κ 与 mult 全错。
            veto["prev_amt_stale"].append({"symbol": s, "stamp": str(v.get("stamp", ""))})

    # ② 帧级撮合率：读点所在的帧必须真的是撮合帧。
    def frame_at(t: str) -> dict | None:
        """ts ≤ t 的最后一帧。`t` 是 `HH:MM:SS`；传 `HH:MM` 会静默取到该分钟末帧。"""
        if len(t) != 8:
            raise ValueError(f"读点必须是 HH:MM:SS（收到 {t!r}）：HH:MM 会少 59 秒")
        lim = _sec(t)
        best = None
        for f in frames:
            if _sec(_hhmmss(f)) <= lim:
                best = f
        return best

    probes = {t: frame_at(t) for t in PROBES}
    for t, f in probes.items():
        veto["frame_match_rate"][t] = None if f is None else round(_frame_match_rate(f), 4)

    # 单调性：09:21 之后（禁撤单已成事实）虚拟匹配量必须非降。
    # 09:20:00 那一瞬仍可能落进 09:19:5x 的撤单，单独记为 boundary 而不是违规。
    violations, boundary = [], []
    for s in syms:
        seq = [(_hhmmss(f), (f["snap"][s].get("b1v") or 0.0))
               for f in frames if "09:20:00" <= _hhmmss(f) < "09:25:00" and s in f["snap"]]
        for (t1, a), (t2, b) in zip(seq, seq[1:]):
            if b < a - 0.5:
                item = {"symbol": s, "from": t1, "to": t2, "a": a, "b": b}
                (boundary if t1 < "09:21:00" else violations).append(item)
                break

    probe_meta = {}
    for t, f in probes.items():
        if f is None:
            probe_meta[t] = {"used_ts": None, "offset_s": None}
            continue
        off = _sec(t) - _sec(_hhmmss(f))
        buf = _sec(DEADLINE) - _sec(_hhmmss(f))
        # 「可行动」= 读点真的被满足（不是回落到的老帧）**且** 余量够下单。
        # 只看余量会骗人：帧停在 09:19:00 时余量 6 分钟，看着最宽裕，其实是 5 分半前的老帧。
        probe_meta[t] = {"used_ts": f["ts"], "offset_s": off, "buffer_s": buf,
                         "actionable": buf >= ACTION_MIN_BUFFER_S and abs(off) <= PROBE_TOL_S,
                         "frame_match_rate": veto["frame_match_rate"][t]}

    rows = []
    for s in syms:
        if s not in final:
            continue
        v0 = frames[0]["snap"].get(s)
        if not v0:
            continue
        auc_amt, final_stamp, final_ts = final[s]
        prev_amt = v0.get("amount_wan") or 0.0
        if auc_amt <= 0:
            continue  # 竞价无成交：倍数无定义，不补零（补零会把「没交易」算成「极度薄」）
        ffin = next(f for f in frames if f["ts"] == final_ts)
        try:
            unit = unit_factor(ffin, s)
        except ValueError:
            unit = None
        rec = {"date": frames[-1]["ts"][:10], "symbol": s, "name": v0.get("name"),
               "board": "STAR" if s.startswith("688") else "MAIN/GEM",
               "unit_field6": unit, "prev_amt_wan": prev_amt, "auc_amt_wan": auc_amt,
               "final_stamp": final_stamp,
               "kappa_pct": (auc_amt / prev_amt * 100) if prev_amt else None}
        for t, f in probes.items():
            if f is None or s not in f["snap"]:
                continue
            v = f["snap"][s]
            # ③ 逐票内容时间：该读点用的这一票的 stamp 必须是当日。
            #    陈旧帧不会让 `frame_at` 报错（它只看 ts），只会让读数变成昨收盘口。
            if _stamp_day(v) != day:
                veto["stale_obs"].append({"symbol": s, "probe": t,
                                          "stamp": str(v.get("stamp", ""))})
            amt = indicative_amt(f, s)
            if amt <= 0 and t >= "09:20:00":
                # 09:20 之后 b1v 必须 > 0；为 0 说明该票此刻不在撮合里（停牌/无申报），
                # 静默丢票会让日聚合偏向活跃票。
                veto["zero_obs"].append({"symbol": s, "probe": t})
            rec[f"ind_amt_wan_{t}"] = amt
            rec[f"mult_{t}"] = (auc_amt / amt) if amt > 0 else None
            rec[f"visible_{t}"] = (amt / auc_amt) if auc_amt else None
            # 末端增量：从 t 到开盘竞价结束，又涌进来多少（占最终额的比例）。
            # 这一列是「早读点的倍数能不能移植到晚读点」的唯一依据。
            rec[f"late_share_{t}"] = ((auc_amt - amt) / auc_amt) if auc_amt and amt > 0 else None
        rows.append(rec)

    meta = {"date": frames[-1]["ts"][:10], "n_symbols": len(syms), "n_pairs": len(rows),
            "n_final": len(final), "probes": probe_meta, "veto": veto,
            "monotonicity": {"violations_after_0921": violations,
                             "boundary_0920": boundary}}
    return rows, meta


def _order_stat_ci(xs: list[float], q: float, level: float = 0.95) -> dict:
    """q 分位的**分布无关**置信区间（次序统计量，二项分布定秩）。

    不用 bootstrap：极值分位上自助重抽样被最小值截断，给出的下界恒等于 `min`，
    看起来是区间、其实是边界。这里直接算覆盖概率：

        P(X_(r) ≤ x_q) = P(Bin(n,q) ≥ r)

    要一个 95% 双侧区间，下界秩 r 需满足 `P(Bin≥r) ≥ 0.975`。
    **n=20、q=0.05 时 `P(Bin≥1) = 0.64 < 0.975` ⇒ 下界秩不存在。**
    也就是说 N=20 只能给 p05 的**上界**，给不出下界 —— 于是「p05 ≥ 阈值」这个
    方向的判据在 N=20 上**不可证伪**，必须扩样本（要 p05 可识别，n 得显著更大：
    n=100 时 `P(Bin≥1)=0.994`，下界秩才出现）。
    """
    from math import comb

    n = len(xs)
    s = sorted(xs)
    pmf = [comb(n, k) * q**k * (1 - q) ** (n - k) for k in range(n + 1)]
    tail = (1 - level) / 2

    lo_rank = None
    for r in range(1, n + 1):
        if sum(pmf[r:]) >= 1 - tail:   # P(Bin >= r)
            lo_rank = r
    cdf, acc = [], 0.0
    for p in pmf:
        acc += p
        cdf.append(acc)
    hi_rank = None
    for t in range(1, n + 1):
        if cdf[t - 1] >= 1 - tail:     # P(Bin <= t-1)
            hi_rank = t
            break

    return {"level": level,
            "lower": round(s[lo_rank - 1], 3) if lo_rank else None,
            "upper": round(s[hi_rank - 1], 3) if hi_rank else None,
            "lower_rank": lo_rank, "upper_rank": hi_rank,
            #: 下界秩 = 1 时「下界」就是最小值本身 —— 那不是分位的下界，只是
            #: 「最小的那只票恰好是它」。所以只有秩 > 1 才算真的识别出来。
            #: 秩不存在（N=20）或秩=1（N≈100）都只能判「小到危险」，
            #: 不能判「大到安全」：**判据方向只有一侧可证伪**。
            "two_sided_identifiable": bool(lo_rank and lo_rank > 1)}


def _stat(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None and x > 0]
    if not xs:
        return {}
    return {"n": len(xs), "median": round(st.median(xs), 3),
            "p05": round(quantile(xs, 0.05), 3), "p10": round(quantile(xs, 0.10), 3),
            "p90": round(quantile(xs, 0.90), 3), "p95": round(quantile(xs, 0.95), 3),
            #: 判据门槛用的是 p05，所以只有它配区间；其余分位不报区间，
            #: 免得读者把「p95 也有区间」当成「两个分位可以并排比」。
            "p05_ci95": _order_stat_ci(xs, 0.05),
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


def frame_interval_stats(frames: list[dict]) -> dict:
    """实测轮询间隔。声明「N 秒一帧」而不量，就是又一处只靠自觉的口径。

    `--interval` 只是**下限**：一轮全市场扫描本身要时间，所以实际间隔 = 扫描耗时
    与设定值的较大者，会出现 2 秒与 3 秒混用。具体分布记在产物同名字段里，
    不写进这段散文（读数写进 docstring 就多一个产地）。
    """
    ts = sorted({f["ts"] for f in frames})
    gaps = [_sec(b[11:19]) - _sec(a[11:19]) for a, b in zip(ts, ts[1:])]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return {}
    return {"n_frames": len(ts), "n_gaps": len(gaps),
            "p50": st.median(gaps), "p95": quantile(gaps, 0.95), "max": max(gaps),
            "hist": {str(g): gaps.count(g) for g in sorted(set(gaps))}}


def summarize(rows: list[dict], probes: list[str]) -> dict:
    rank = {}
    for t in probes:
        xs = [r for r in rows if r.get(f"ind_amt_wan_{t}") and r.get("auc_amt_wan")]
        if len(xs) < 3:
            continue
        x = [math.log(max(r[f"ind_amt_wan_{t}"], 1e-3)) for r in xs]
        y = [math.log(max(r["auc_amt_wan"], 1e-3)) for r in xs]
        late = [r.get(f"late_share_{t}") for r in xs]
        rank[t] = {"n": len(xs), "pearson_log": round(corr(x, y), 4),
                   "spearman_log": round(corr(_rank(x), _rank(y)), 4),
                   "mult": _stat([r[f"mult_{t}"] for r in xs]),
                   "late_share": _stat([v for v in late if v is not None])}
    return rank


def summarize_guards(metas: list[dict], frames: list[dict]) -> dict:
    """把逐日的 `veto` 汇成一份可判红的结构。

    三条二值守卫，**每一条都见过它红的输入**：
      `prev_amt_stale` ← burst 从 09:10 之后起（首帧 stamp 已是当日 ⇒ A 不是昨额）
      `stale_obs`      ← 读点回落到前一交易日的帧
      `zero_obs`       ← 某票 09:20 后 V_obs=0（停牌 / 无申报）
      `frame_match_rate < 0.9` ← 读点落进竞价前的空帧（`b1v` 全 0）
    判红不等于丢证据：产物照写，`failures` 非空 + 退出码 2。
    """
    guards: dict = {"failures": [], "by_day": {}}
    for m in metas:
        v = m.get("veto") or {}
        day_fail = []
        if v.get("prev_amt_stale"):
            day_fail.append(f"prev_amt_stale={len(v['prev_amt_stale'])}"
                            f"（首帧 stamp 已是当日 ⇒ A 不是昨成交额）")
        if v.get("stale_obs"):
            day_fail.append(f"stale_obs={len(v['stale_obs'])}（读点用了非当日帧）")
        if v.get("zero_obs"):
            day_fail.append(f"zero_obs={len(v['zero_obs'])}（09:20 后 V_obs=0）")
        low = {t: r for t, r in (v.get("frame_match_rate") or {}).items()
               if r is not None and r < FRAME_MATCH_MIN}
        if low:
            day_fail.append(f"frame_match_rate<0.9: {low}")
        guards["by_day"][m.get("date")] = day_fail
        if day_fail:
            guards["failures"].extend(f"{m.get('date')}: {x}" for x in day_fail)
    guards["frame_interval_s"] = frame_interval_stats(frames)
    return guards


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=Path, default=DEFAULT_FRAMES)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--collect", action="store_true", help="抓一次全市场快照并落盘")
    ap.add_argument("--burst", action="store_true", help="连续扫全市场直到 --until")
    ap.add_argument("--interval", type=float, default=5.0, help="burst 的轮间隔（秒）")
    ap.add_argument("--threads", type=int, default=16, help="并发请求数")
    ap.add_argument("--batch", type=int, default=80, help="单请求股票数")
    ap.add_argument("--until", default="09:26:00", help="burst 停在第几秒（HH:MM:SS）")
    ap.add_argument("--since", default="", help="burst 从第几秒开始（空=立刻）")
    ap.add_argument("--archive", action="store_true", help="把帧落成 parquet（证据本体）")
    ap.add_argument("--label", default="", help="快照标签，如 092430")
    ap.add_argument("--expect-date", default="",
                    help="要求输入至少含这一交易日（YYYY-MM-DD 或 YYYYMMDD）；不含则退出码 3")
    args = ap.parse_args()

    # `--out` 是相对路径时，产物**已经写完**才会在 `relative_to(ROOT)` 上抛异常，
    # 于是表现成「盘上有新产物 + 退出码 1」——最坏的一种：失败与成功同时为真。
    args.out = args.out.resolve()

    if args.collect:
        collect(args.label or datetime.now().strftime("%H%M"), COLLECT_DIR, args.threads, args.batch)
        return 0
    if args.burst:
        burst(COLLECT_DIR, args.interval, args.until, args.threads, args.batch, since=args.since)
        return 0

    frames = load(args.frames)
    if not frames:
        raise SystemExit("无帧")

    # 输入必须是「那一天」的帧。`--frames` 的默认值指向 `.workbuddy/tmp` 里某个**具体
    # 交易日**的 jsonl：那个路径不在版本控制内，也不会自己变新。少了这道检查，明天跑
    # 同一条命令会把昨天的帧重算成一份「成功」的产物 —— 退出码 0、逐字段与昨天相同、
    # git 也没有 diff，唯一变化是 mtime。绿而无新证据比红更难发现，所以要显式点名。
    if args.expect_date:
        want = args.expect_date.replace("-", "")
        got = sorted({f["ts"][:10] for f in frames})
        if want not in {g.replace("-", "") for g in got}:
            print(f"✗ 输入不含 {args.expect_date}（实际 {got}）—— 拒绝把旧帧重算成新产物")
            return 3

    if args.archive:
        archive(frames, FRAMES_DIR, args.label or "burst")

    days: dict[str, list[dict]] = {}
    for f in frames:
        days.setdefault(f["ts"][:10], []).append(f)

    all_rows: list[dict] = []
    metas = []
    for _, fs in sorted(days.items()):
        rows, meta = analyze_day(fs)
        all_rows.extend(rows)
        metas.append(meta)

    # 对齐守卫：读点与所用帧的偏移必须 ≤ PROBE_TOL_S。首版正是这里无声偏了 59 秒，
    # 于是「09:24 的倍数」其实是 09:24:59 的 —— 判据跟着翻转。这一条要能红。
    guard = {"tol_s": PROBE_TOL_S, "by_probe": {}, "misaligned": []}
    for m in metas:
        for t, pm in (m.get("probes") or {}).items():
            if pm.get("offset_s") is None:
                guard["by_probe"][t] = None
                continue
            guard["by_probe"][t] = pm["offset_s"]
            if abs(pm["offset_s"]) > PROBE_TOL_S:
                guard["misaligned"].append({"date": m["date"], "probe": t,
                                            "used_ts": pm["used_ts"],
                                            "offset_s": pm["offset_s"]})

    if not all_rows:
        # 空射程不是通过：输入里没有一天能配上 V_final 时，写盘就是把上一版证据清空。
        raise SystemExit(f"0 对配对（帧 {len(frames)}、天 {len(days)}）—— 不写 {args.out.name}；"
                         "先查输入是不是只有采集时的连续竞价帧")

    # 三条二值守卫的汇总见 `summarize_guards`（那里逐条写明了它见过的红输入）。
    guards = summarize_guards(metas, frames)

    rank = summarize(all_rows, list(PROBES))
    sum_final = sum(r["auc_amt_wan"] for r in all_rows)
    src_files = sorted(args.frames.glob("*.jsonl")) if args.frames.is_dir() else [args.frames]

    # `buffer_s` / `actionable` 说的是「这个读点还能不能下单」，必须取**最新一天**的读点。
    # 取 `metas[0]` 在多日输入下会命中最早的、而且可能被 skipped 的那一天（probes 为空），
    # 于是 actionable 静默变 None、容量段整段消失，而退出码仍然是 0。
    cap_meta = next((m for m in reversed(metas) if m.get("probes")), metas[-1])

    result = {
        "source": {
            "frames_path": str(args.frames.relative_to(ROOT)) if args.frames.is_relative_to(ROOT) else str(args.frames),
            "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in src_files},
            "ts_first": frames[0]["ts"], "ts_last": frames[-1]["ts"],
            "frames": len(frames), "days": len(days),
        },
        "date_semantics": "date 是入场日口径（与 l1_cohort 一致）；本产物不与收益连接",
        "read_point_semantics": (
            "所有读点是 HH:MM:SS 的精确秒，不是分钟。probes[*].used_ts 是实际用到的帧，"
            "offset_s = 读点 − 帧时间；buffer_s = 09:25:00 − 帧时间，<=0 为不可行动"
        ),
        "field_semantics": {
            "b1v_eq_a1v": "竞价阶段 b1p==a1p 且 b1v==a1v ⇒ 该字段是虚拟成交价/虚拟匹配量",
            "b1v_unit": "手（两种板都是）",
            "field6_unit": "科创板=股，其余=手；本轮用 amount/vol/price 反解并断言落在 {1,100}",
            "monotone_rule": "09:20 后禁撤单 ⇒ 匹配量非降是交易所规则，不是统计假设",
            "late_share": "(V_final − V_obs(t)) / V_final，即 t 之后又涌进来的增量占比",
        },
        "probe_guard": guard,
        "guards": guards,
        "days": metas,
        "rank_preservation": rank,
        "multiplier_stats": {t: (rank.get(t) or {}).get("mult", {}) for t in PROBES},
        "late_share_stats": {t: (rank.get(t) or {}).get("late_share", {}) for t in PROBES},
        "capacity_observed": {
            "by_probe": {t: {"sum_ind_amt_wan": round(sum(r.get(f"ind_amt_wan_{t}") or 0.0 for r in all_rows), 1),
                             "cap_10pct_of_observed_wan": round(sum(r.get(f"ind_amt_wan_{t}") or 0.0 for r in all_rows) / V_OBS_DIVISOR, 1),
                             "buffer_s": (cap_meta.get("probes", {}).get(t) or {}).get("buffer_s"),
                             "actionable": (cap_meta.get("probes", {}).get(t) or {}).get("actionable")}
                         for t in PROBES},
            "sum_final_auc_wan": round(sum_final, 1),
            "cap_10pct_of_final_wan": round(sum_final / V_OBS_DIVISOR, 1),
            "n_pairs": len(all_rows),
            "note": ("x_i <= V_obs_i/9 保证 x/(V_others+x) <= 10%，用 V_obs 是保守方向；"
                     "但**只有 buffer_s > 0 的读点才是可下单的读点**，容量口径必须取这种点"),
        },
        "per_stock": all_rows,
        "caveats": [
            "倍数分布的可信度等于天数×票数；一天 20 只只够证明机制，不够定分位。",
            "在 20 只流动性好的票上定的分位不能直接用到 l1_cohort 的候选集（薄票末端更跳）。",
            "可见份额只对「同一只票同一天」成立；跨票排序是 Spearman，不是可替代关系。",
            "科创板 b1v 是手、[6] 是股；混用会让 688 的倍数大 100 倍。",
            "盘中调用 --collect 只能验证管线，盘的字段是连续竞价语义，不能进倍数分布。",
            "倍数要在**下分位**定（V_final/V_obs 小了才破 10% 线），拿上分位当验收会反向。",
        ],
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"帧 {len(frames)}  {frames[0]['ts']} -> {frames[-1]['ts']}  天 {len(days)}  配对 {len(all_rows)}")
    fi = guards["frame_interval_s"]
    if fi:
        print(f"实测间隔 p50 {fi['p50']}s  p95 {fi['p95']}s  max {fi['max']}s  "
              f"（直方图 {fi['hist']}）")
    bad = sum(len(m.get("monotonicity", {}).get("violations_after_0921", [])) for m in metas)
    print(f"单调性违规（09:21 之后）{bad} 处；09:20 边界 {sum(len(m.get('monotonicity', {}).get('boundary_0920', [])) for m in metas)} 处")
    print("\n倍数分布（最终竞价额 / ind）与末端增量份额")
    for t in PROBES:
        s = result["multiplier_stats"][t]
        if s:
            pm = (cap_meta.get("probes", {}).get(t) or {})
            ls = result["late_share_stats"][t]
            ci = s.get("p05_ci95") or {}
            lo = ci.get("lower")
            citxt = (f" p05 95%CI [{'不可识别' if lo is None else lo}, {ci.get('upper')}]"
                     if ci else "")
            print(f"  {t}: n={s['n']} 中位 {s['median']} p05 {s['p05']}{citxt}"
                  f" 区间 [{s['min']}, {s['max']}]"
                  f" | 之后又涌进 中位 {ls.get('median')} | 余量 {pm.get('buffer_s')}s"
                  f" 帧 {pm.get('used_ts')}")
    print("排序保真", {t: v["spearman_log"] for t, v in rank.items()})
    if guard["misaligned"]:
        print(f"✗ 读点对齐失败 {len(guard['misaligned'])} 处：" + str(guard["misaligned"][:3]))
    for fail in guards["failures"]:
        print(f"✗ 守卫红：{fail}")
    c = result["capacity_observed"]
    act = [t for t in PROBES if (c["by_probe"][t] or {}).get("actionable")]
    if act:
        t = act[-1]
        print(f"\n可行动读点 {t}：Σ ind = {c['by_probe'][t]['sum_ind_amt_wan'] / 1e4:.2f} 亿 "
              f"⇒ 10% 口径 {c['by_probe'][t]['cap_10pct_of_observed_wan'] / 1e4:.2f} 亿 / {c['n_pairs']} 对")
    print(f"→ {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    if guards["failures"]:
        return 2
    return 1 if guard["misaligned"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
