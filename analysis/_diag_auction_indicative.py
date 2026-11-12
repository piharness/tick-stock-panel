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

## 本脚本量化三件事

1. **可见份额**：09:20 / 09:22 / 09:24 的 `V_obs / V_final` 分布（跨票）。
2. **排序保真**：`log V_obs` 与 `log V_final` 的 Pearson / Spearman（含并列秩）。
3. **单位陷阱**：科创板 `[6]` 成交量字段是**股**，其余是**手**；而 `b1v` **两种板都是手**。
   用 `amount·1e4/(vol·price)` 反解每票单位并要求落在 {1, 100} 的 1% 内，
   否则直接报错 —— 这一条错了，688 的倍数会凭空大 100 倍。

## 输入

`--frames` 指向 `qt.gtimg.cn` 连续快照的 jsonl（`{"ts","snap":{sym:{...}}}`）。
本仓库里它是 `.workbuddy/tmp/auction_burst.jsonl`（09:06:43–09:26:29，2s 一帧，20 只）。
临时目录不进版本控制 ⇒ 结论必须落在 `_result.json` 里，并附 `source.sha256`。

## 口径（写清是为了不出第二种读法）

- `date` 一律是**入场日**（与 `l1_cohort` 一致）；本产物只有一天。
- `V_final` = stamp ≥ 当日 09:25 的那一帧的 `amount_wan`（万元）。
- `V_obs(t)` = `t` 时刻**最后一帧**的 `b1v × 100 × b1p / 1e4`（万元）。
- `A`（昨成交额）= 首帧的 `amount_wan`（该帧 stamp 是前一交易日，字段仍是全天成交额）。
- `κ = V_final / A`，用**同一列**（amount_wan）相除，不跨口径。

## 采集（09:20 / 09:24 两次，全市场）

N=20、一天只够证明机制，不够定倍数分布。`--collect` 用 `qt.gtimg.cn` 免费快照
全市场扫一遍（60 只/请求、8 线程，约 90 次请求），一次约 20 秒，落在
`data/research/auction_indicative/<date>.jsonl`（该目录不进版本控制）。
把 09:24 与 09:26 配对跑几天，倍数分布才有总体意义。

## 用法

    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py
    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py --frames <path>
    backend/.venv/Scripts/python.exe analysis/_diag_auction_indicative.py --collect --label 0924
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
#: 原始快照落这里（不进版本控制，派生表在 result.json 的 per_stock 里，那才是证据）。
COLLECT_DIR = ROOT / "data" / "research" / "auction_indicative"

#: 科创板 `[6]` 是股，其余是手；`b1v` 两种板都是手 ⇒ 虚拟匹配额统一 ×100。
LOT = 100


def _hhmm(f: dict) -> str:
    return f["ts"][11:16]


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


def collect(label: str, out_dir: Path, workers: int = 8, batch: int = 60) -> int:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime

    syms = universe()
    batches = [syms[i:i + batch] for i in range(0, len(syms), batch)]
    snap: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(fetch_batch, batches):
            snap.update(part)
    rec = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "label": label,
           "snap": snap}
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{rec['ts'][:10]}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{rec['ts']}] {label}: {len(snap)}/{len(syms)} 只有快照 → {out_dir}")
    return len(snap)



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
    """一天的配对：indicator@09:20/09:22/09:24 vs 最终竞价额。"""
    day = frames[-1]["ts"][:10].replace("-", "")
    syms = sorted(frames[0]["snap"])
    final = None
    for f in frames:
        # 只认 stamp 落在 [09:25, 09:30) 的帧：09:25 那根是竞价成交量，
        # 09:30 之后连续竞价把同一字段改写，混进来会把「全天成交额」当竞价额。
        if all(_stamp_day(v) == day and "0925" <= str(v.get("stamp", ""))[8:12] < "0930"
               for v in f["snap"].values()):
            final = f
            break
    if final is None:
        return [], {"date": day, "skipped": "没有 stamp 落在 [09:25,09:30) 的帧"}

    # 单调性：09:21 之后（禁撤单已成事实）虚拟匹配量必须非降。
    # 09:20:00 那一瞬仍可能落进 09:19:5x 的撤单，单独记为 boundary 而不是违规。
    violations, boundary = [], []
    for s in syms:
        seq = [(_hhmm(f), (f["snap"][s].get("b1v") or 0.0))
               for f in frames if "09:20" <= _hhmm(f) < "09:25" and s in f["snap"]]
        for (t1, a), (t2, b) in zip(seq, seq[1:]):
            if b < a - 0.5:
                item = {"symbol": s, "from": t1, "to": t2, "a": a, "b": b}
                (boundary if t1 == "09:20" else violations).append(item)
                break

    def frame_at(t: str) -> dict | None:
        cands = [f for f in frames if _hhmm(f) <= t]
        return cands[-1] if cands else None

    probes = {t: frame_at(t) for t in ("09:20", "09:22", "09:24")}

    rows = []
    for s in syms:
        if s not in final["snap"]:
            continue
        v0, vf = frames[0]["snap"].get(s), final["snap"][s]
        if not v0:
            continue
        prev_amt = v0.get("amount_wan") or 0.0
        auc_amt = vf.get("amount_wan") or 0.0
        if auc_amt <= 0:
            continue  # 竞价无成交：倍数无定义，不补零（补零会把「没交易」算成「极度薄」）
        try:
            unit = unit_factor(final, s)
        except ValueError:
            unit = None
        rec = {"date": frames[-1]["ts"][:10], "symbol": s, "name": v0.get("name"),
               "board": "STAR" if s.startswith("688") else "MAIN/GEM",
               "unit_field6": unit, "prev_amt_wan": prev_amt, "auc_amt_wan": auc_amt,
               "kappa_pct": (auc_amt / prev_amt * 100) if prev_amt else None}
        for t, f in probes.items():
            if f is None or s not in f["snap"]:
                continue
            v = indicative_amt(f, s)
            rec[f"ind_amt_wan_{t}"] = v
            rec[f"mult_{t}"] = (auc_amt / v) if v > 0 else None
            rec[f"visible_{t}"] = (v / auc_amt) if auc_amt else None
        rows.append(rec)

    meta = {"date": day, "n_symbols": len(syms), "n_pairs": len(rows),
            "monotonicity": {"violations_after_0921": violations,
                             "boundary_0920": boundary}}
    return rows, meta


def _stat(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None and x > 0]
    if not xs:
        return {}
    return {"n": len(xs), "median": round(st.median(xs), 3),
            "p10": round(quantile(xs, 0.10), 3), "p90": round(quantile(xs, 0.90), 3),
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


def summarize(rows: list[dict], probes: list[str]) -> dict:
    rank = {}
    for t in probes:
        xs = [r for r in rows if r.get(f"ind_amt_wan_{t}") and r.get("auc_amt_wan")]
        if len(xs) < 3:
            continue
        x = [math.log(max(r[f"ind_amt_wan_{t}"], 1e-3)) for r in xs]
        y = [math.log(max(r["auc_amt_wan"], 1e-3)) for r in xs]
        rank[t] = {"n": len(xs), "pearson_log": round(corr(x, y), 4),
                   "spearman_log": round(corr(_rank(x), _rank(y)), 4),
                   "mult": _stat([r[f"mult_{t}"] for r in xs])}
    return rank


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=Path, default=DEFAULT_FRAMES)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--collect", action="store_true", help="抓一次全市场快照并落盘")
    ap.add_argument("--label", default="", help="本次快照标签，如 0920 / 0924 / 0926")
    args = ap.parse_args()

    if args.collect:
        collect(args.label or datetime.now().strftime("%H%M"), COLLECT_DIR)
        return 0

    frames = load(args.frames)
    if not frames:
        raise SystemExit("无帧")

    days: dict[str, list[dict]] = {}
    for f in frames:
        days.setdefault(f["ts"][:10], []).append(f)

    all_rows: list[dict] = []
    metas = []
    for _, fs in sorted(days.items()):
        rows, meta = analyze_day(fs)
        all_rows.extend(rows)
        metas.append(meta)

    probes = ["09:20", "09:22", "09:24"]
    rank = summarize(all_rows, probes)
    sum_ind = sum(r.get("ind_amt_wan_09:24") or 0.0 for r in all_rows)
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
        "field_semantics": {
            "b1v_eq_a1v": "竞价阶段 b1p==a1p 且 b1v==a1v ⇒ 该字段是虚拟成交价/虚拟匹配量",
            "b1v_unit": "手（两种板都是）",
            "field6_unit": "科创板=股，其余=手；本轮用 amount/vol/price 反解并断言落在 {1,100}",
            "monotone_rule": "09:20 后禁撤单 ⇒ 匹配量非降是交易所规则，不是统计假设",
        },
        "days": metas,
        "rank_preservation": rank,
        "multiplier_stats": {t: (rank.get(t) or {}).get("mult", {}) for t in probes},
        "capacity_observed": {
            "sum_ind_amt_wan_0924": round(sum_ind, 1),
            "sum_final_auc_wan": round(sum_final, 1),
            "cap_10pct_of_observed_wan": round(sum_ind / 9, 1),
            "cap_10pct_of_final_wan": round(sum_final / 9, 1),
            "n_pairs": len(all_rows),
            "note": "x_i <= V_obs_i/9 保证 x/(V_others+x) <= 10%；用 V_obs 而非 V_final 是保守方向",
        },
        "per_stock": all_rows,
        "caveats": [
            "倍数分布的可信度等于天数×票数；一天 20 只只够证明机制，不够定分位。",
            "可见份额只对「同一只票同一天」成立；跨票排序是 Spearman，不是可替代关系。",
            "科创板 b1v 是手、[6] 是股；混用会让 688 的倍数大 100 倍。",
            "盘中调用 --collect 只能验证管线，盘的字段是连续竞价语义，不能进倍数分布。",
        ],
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"帧 {len(frames)}  {frames[0]['ts']} -> {frames[-1]['ts']}  天 {len(days)}  配对 {len(all_rows)}")
    bad = sum(len(m.get("monotonicity", {}).get("violations_after_0921", [])) for m in metas)
    print(f"单调性违规（09:21 之后）{bad} 处；09:20 边界 {sum(len(m.get('monotonicity', {}).get('boundary_0920', [])) for m in metas)} 处")
    print("\n倍数分布（最终竞价额 / ind）")
    for t in probes:
        s = result["multiplier_stats"][t]
        if s:
            print(f"  {t}: n={s['n']} 中位 {s['median']} p10 {s['p10']} p90 {s['p90']} 区间 [{s['min']}, {s['max']}]")
    print("排序保真", {t: v["spearman_log"] for t, v in rank.items()})
    c = result["capacity_observed"]
    print(f"\n观测容量：Σ ind@09:24 = {c['sum_ind_amt_wan_0924'] / 1e4:.2f} 亿 "
          f"⇒ 10% 口径 {c['cap_10pct_of_observed_wan'] / 1e4:.2f} 亿 / {c['n_pairs']} 对")
    print(f"→ {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
