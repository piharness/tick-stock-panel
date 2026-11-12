"""集合竞价的「虚拟匹配量」在 09:24 就已经把最终成交量说出来了 —— 于是 κ 不必预测。

## 为什么要这个产物

`_diag_l1_weight_frontier` 把容量写成 `T_max(α) = Σ 0.1·κ_i·A_i`，其中
`κ_i = 竞价成交额 / 昨成交额`。整条链的前提是 **κ_i 事前不可知**，因此：

1. 需要 κ 的历史面板（L2 采购 / 日更采样 / 30 天 ICC）；
2. 容量只能报成 κ 分布的一个分位（80% 门 582.9 万）；
3. `π_i ≤ 10%` 只能事后核，不能事前保证。

这个前提在数据上是错的。09:15–09:25 的单价撮合阶段，行情快照的
`b1p == a1p`（虚拟成交价）且 `b1v == a1v`（**虚拟匹配量**），且**单调不减**。
09:20 之后禁止撤单 ⇒ 匹配量非降是**交易所规则**，不是统计规律 ⇒
`V_final ≥ V_obs` 是一条硬下界，不是估计。

于是 `x_i ≤ V_obs_i / 9` 就已经保证参与率 ≤ 10%（下单后总量为 `V_others + x`），
不需要 κ 预测、不需要 L2、不需要 ICC。

## 本脚本量化四件事

1. **可见份额**：各读点（`HH:MM:SS`，见下）的 `V_obs / V_final` 分布（跨票）。
2. **排序保真**：`log V_obs` 与 `log V_final` 的 Pearson / Spearman（含并列秩）。
3. **末端增量**：`(V_final − V_obs(t)) / V_final`，即「最后这段时间又涌进来多少」。
   它决定常量倍数能不能用：末端份额越大，早读点的倍数越不可移植。
4. **单位陷阱**：科创板 `[6]` 成交量字段是**股**，其余是**手**；而 `b1v` **两种板都是手**。
   用 `amount·1e4/(vol·price)` 反解每票单位并要求落在 {1, 100} 的 1% 内，
   否则直接报错 —— 这一条错了，688 的倍数会凭空大 100 倍。

## 输入

`--frames` 指向 `qt.gtimg.cn` 连续快照的 jsonl（`{"ts","snap":{sym:{...}}}`）。
原始帧一律 `--archive` 落成 parquet 存 `data/research/auction_indicative/frames/`：
**竞价数据不可回补**，帧是唯一证据，汇总表替不了它（`_result.json` 只留派生表 +
`source.sha256`，原文丢了就再也算不出来）。

## 口径（写清是为了不出第二种读法）

- `date` 一律是**入场日**（与 `l1_cohort` 一致）；本产物只有一天。
- `V_final` = 该票 stamp 落在 `[09:25:00, 09:30:00)` 的**首帧** `amount_wan`（万元）。
  逐票取，不要求整帧所有票同时到位（交易所推送有秒级先后）。
- `V_obs(t)` = **ts ≤ t 的最后一帧** 的 `b1v × 100 × b1p / 1e4`（万元）；`t` 精确到秒。
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

## 采集

N=20、一天只够证明机制，不够定倍数分布。`--collect` 用 `qt.gtimg.cn` 免费快照
全市场扫一遍，落在 `data/research/auction_indicative/<date>.jsonl`。
**点采样不够**：它要求机器恰好在那一秒醒着，而且只能给读点的点值、给不出路径。
基准做法是 09:15–09:26 连续 2–5 秒一帧（`--burst`），任何时刻的倍数都能事后算，
掉几帧也不影响。倍数分布要在**应用集**（`l1_cohort` 候选）上算，不是在全市场或
20 只流动性好的票上算；判据取**下分位**（`V_final/V_obs` 小了才会破 10% 线），
不是上分位。

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
#: 距 09:25:00 少于这么多秒的读点只能算、不能下单（不足以下单+回报）。
ACTION_MIN_BUFFER_S = 30

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
    """一次最多 60 只；字段口径沿用项目内既有解析（[6] 量 / [37] 额 / [9][10] 买一）。"""
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
          batch: int = 80, max_sweeps: int = 0) -> int:
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


def archive(frames: list[dict], out_dir: Path, tag: str) -> Path:
    """把原始帧落成 parquet 长表。**竞价数据不可回补**，这份才是证据本体。"""
    import polars as pl

    recs = []
    for f in frames:
        for s, v in f["snap"].items():
            recs.append({
                "ts": f["ts"], "symbol": s, "stamp": str(v.get("stamp", "")),
                "name": v.get("name"), "last": v.get("last"), "prev": v.get("prev"),
                "vol_hand": v.get("vol_hand"), "amount_wan": v.get("amount_wan"),
                "b1p": v.get("b1p"), "b1v": v.get("b1v"),
                "a1p": v.get("a1p"), "a1v": v.get("a1v"),
            })
    day = frames[-1]["ts"][:10]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{day}_{tag}.parquet"
    pl.DataFrame(recs).write_parquet(out, compression="zstd")
    print(f"归档 {len(recs)} 行 × {len(frames)} 帧 → {out}（{out.stat().st_size/1024:.0f} KB）")
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
                         "actionable": buf >= ACTION_MIN_BUFFER_S and abs(off) <= PROBE_TOL_S}

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
            v = indicative_amt(f, s)
            rec[f"ind_amt_wan_{t}"] = v
            rec[f"mult_{t}"] = (auc_amt / v) if v > 0 else None
            rec[f"visible_{t}"] = (v / auc_amt) if auc_amt else None
            # 末端增量：从 t 到开盘竞价结束，又涌进来多少（占最终额的比例）。
            # 这一列是「早读点的倍数能不能移植到晚读点」的唯一依据。
            rec[f"late_share_{t}"] = ((auc_amt - v) / auc_amt) if auc_amt and v > 0 else None
        rows.append(rec)

    meta = {"date": frames[-1]["ts"][:10], "n_symbols": len(syms), "n_pairs": len(rows),
            "n_final": len(final), "probes": probe_meta,
            "monotonicity": {"violations_after_0921": violations,
                             "boundary_0920": boundary}}
    return rows, meta


def _stat(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None and x > 0]
    if not xs:
        return {}
    return {"n": len(xs), "median": round(st.median(xs), 3),
            "p05": round(quantile(xs, 0.05), 3), "p10": round(quantile(xs, 0.10), 3),
            "p90": round(quantile(xs, 0.90), 3), "p95": round(quantile(xs, 0.95), 3),
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


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
    ap.add_argument("--archive", action="store_true", help="把帧落成 parquet（证据本体）")
    ap.add_argument("--label", default="", help="快照标签，如 092430")
    args = ap.parse_args()

    if args.collect:
        collect(args.label or datetime.now().strftime("%H%M"), COLLECT_DIR, args.threads, args.batch)
        return 0
    if args.burst:
        burst(COLLECT_DIR, args.interval, args.until, args.threads, args.batch)
        return 0

    frames = load(args.frames)
    if not frames:
        raise SystemExit("无帧")

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

    rank = summarize(all_rows, list(PROBES))
    sum_final = sum(r["auc_amt_wan"] for r in all_rows)
    src_files = sorted(args.frames.glob("*.jsonl")) if args.frames.is_dir() else [args.frames]

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
        "days": metas,
        "rank_preservation": rank,
        "multiplier_stats": {t: (rank.get(t) or {}).get("mult", {}) for t in PROBES},
        "late_share_stats": {t: (rank.get(t) or {}).get("late_share", {}) for t in PROBES},
        "capacity_observed": {
            "by_probe": {t: {"sum_ind_amt_wan": round(sum(r.get(f"ind_amt_wan_{t}") or 0.0 for r in all_rows), 1),
                             "cap_10pct_of_observed_wan": round(sum(r.get(f"ind_amt_wan_{t}") or 0.0 for r in all_rows) / 9, 1),
                             "buffer_s": (metas[0].get("probes", {}).get(t) or {}).get("buffer_s"),
                             "actionable": (metas[0].get("probes", {}).get(t) or {}).get("actionable")}
                         for t in PROBES},
            "sum_final_auc_wan": round(sum_final, 1),
            "cap_10pct_of_final_wan": round(sum_final / 9, 1),
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
    bad = sum(len(m.get("monotonicity", {}).get("violations_after_0921", [])) for m in metas)
    print(f"单调性违规（09:21 之后）{bad} 处；09:20 边界 {sum(len(m.get('monotonicity', {}).get('boundary_0920', [])) for m in metas)} 处")
    print("\n倍数分布（最终竞价额 / ind）与末端增量份额")
    for t in PROBES:
        s = result["multiplier_stats"][t]
        if s:
            pm = (metas[0].get("probes", {}).get(t) or {})
            ls = result["late_share_stats"][t]
            print(f"  {t}: n={s['n']} 中位 {s['median']} p05 {s['p05']} 区间 [{s['min']}, {s['max']}]"
                  f" | 之后又涌进 中位 {ls.get('median')} | 余量 {pm.get('buffer_s')}s"
                  f" 帧 {pm.get('used_ts')}")
    print("排序保真", {t: v["spearman_log"] for t, v in rank.items()})
    if guard["misaligned"]:
        print(f"✗ 读点对齐失败 {len(guard['misaligned'])} 处：" + str(guard["misaligned"][:3]))
    c = result["capacity_observed"]
    act = [t for t in PROBES if (c["by_probe"][t] or {}).get("actionable")]
    if act:
        t = act[-1]
        print(f"\n可行动读点 {t}：Σ ind = {c['by_probe'][t]['sum_ind_amt_wan'] / 1e4:.2f} 亿 "
              f"⇒ 10% 口径 {c['by_probe'][t]['cap_10pct_of_observed_wan'] / 1e4:.2f} 亿 / {c['n_pairs']} 对")
    print(f"→ {args.out.relative_to(ROOT)}")
    return 1 if guard["misaligned"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
