"""控制权变更公告：解析「变更前 / 变更后」控制人，筛 A 级（民营 → 国资）。

为什么换这个公告类型：
  《权益变动报告书》的前后持股表只写披露人自己，96% 不写原控制人（见 §10）。
  「控制权变更提示性公告」正好相反——第一段必写「实际控制人将由 X 变更为 Y」。

流程：
  1. 按 code 聚类（间隔 ≤180 天算同一事件），每簇取「首次披露」那一条
     （提示性公告 > 停牌/筹划 > 其他；排除进展/回复/法律意见/更正）
  2. 解析：变更前控制人、变更后控制人、方式、受让方、变动后比例
  3. 分类：state / platform / finance / person / corp
  4. A 级 = 变更前为民营或自然人，变更后为国资

输出：
  data/.scratch/state/control_change_events.parquet
  data/.scratch/state/control_change_A.csv
  docs/structural-edge/control-change-A.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
from _parse_equity_change_full import classify, fetch_text  # noqa: E402

OUT = ROOT / "data" / ".scratch" / "state"
TARGET = OUT / "control_change_events.parquet"
PROGRESS = OUT / "cc_progress.json"

NOISE = re.compile(r"进展|回复|问询|关注函|法律意见|核查意见|更正|补充|说明会|风险提示的更正")
FIRST = re.compile(r"提示性公告|停牌|筹划|暨.*变更|控制权.*变更")


def cluster(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["rank"] = d["title"].map(lambda t: 0 if ("提示性公告" in t and not NOISE.search(t)) else
                               1 if re.search(r"停牌|筹划", t) else
                               2 if not NOISE.search(t) else 9)
    d = d[d["rank"] < 9]
    d = d.sort_values(["code", "date", "rank"])
    d["gap"] = d.groupby("code")["date"].diff().dt.days
    d["cluster"] = ((d["gap"].isna()) | (d["gap"] > 180)).groupby(d["code"]).cumsum()
    # 每簇取 rank 最小、日期最早的一条
    ev = d.sort_values(["code", "cluster", "rank", "date"]).drop_duplicates(subset=["code", "cluster"], keep="first")
    return ev.reset_index(drop=True)


def clean(s: str | None, n: int = 40) -> str | None:
    if not isinstance(s, str):
        return None
    s = re.sub(r"[（(]以下简称[^）)]*[）)]", "", s)
    s = s.replace("先生", "").replace("女士", "")
    s = re.split(r"[，。；、]|住所|通讯地址", s)[0].strip()
    return s[:n] if 2 <= len(s) <= n else None


def parse(t: str) -> dict:
    t2 = re.sub(r"\s+", "", t)
    out: dict = {}

    # 变更后
    m = re.search(r"实际控制人(?:将)?由([^，。；]{2,60}?)变更为([^，。；]{2,30})", t2)
    if not m:
        m = re.search(r"实际控制人(?:将)?由([^，。；]{2,60}?)(?:变更|变更)为([^，。；]{2,30})", t2)
    if m:
        out["prior_ctrl"] = clean(m.group(1), 60)
        out["new_ctrl"] = clean(m.group(2))
        out["pair_ev"] = m.group(0)[:120]
    else:
        out["pair_ev"] = None
        out["prior_ctrl"] = None
        out["new_ctrl"] = None

    if out["new_ctrl"] is None:
        for pat in [
            r"实际控制人(?:将)?变更为([^，。；]{2,30})",
            r"([^，。；]{2,20})将成为(?:公司|上市公司)?(?:的)?实际控制人",
            r"([^，。；]{2,20})成为(?:公司|上市公司)?(?:的)?实际控制人",
        ]:
            m = re.search(pat, t2)
            if m:
                out["new_ctrl"] = clean(m.group(1))
                break

    out["new_holder"] = None
    for pat in [
        r"控股股东(?:将)?变更为([^，。；]{2,30})",
        r"控股股东(?:将)?由[^，。；]{2,40}?变更为([^，。；]{2,30})",
        r"([^，。；]{2,20})将成为(?:公司|上市公司)?(?:的)?控股股东",
    ]:
        m = re.search(pat, t2)
        if m:
            out["new_holder"] = clean(m.group(1))
            break

    # 变更前（无「由…变更为」时，找「控股股东/实际控制人为X」的第一处）
    if out["prior_ctrl"] is None:
        for pat in [
            r"(?:公司|上市公司)?(?:的)?(?:原)?实际控制人(?:为|系)([^，。；]{2,30})",
            r"(?:公司|上市公司)(?:的)?(?:原)?控股股东(?:为|系)([^，。；]{2,30})",
        ]:
            m = re.search(pat, t2[:6000])
            if m:
                out["prior_ctrl"] = clean(m.group(1))
                break

    # 方式
    method = None
    for kw in ["司法拍卖", "司法变卖", "表决权委托", "表决权放弃", "无偿划转", "间接收购", "协议转让", "定向增发", "非公开发行", "继承"]:
        if kw in t2[:6000]:
            method = kw
            break
    out["method"] = method

    # 受让方
    m = re.search(r"受让方(?:为|系)?[：:]?([^，。；]{2,30})", t2[:6000])
    out["buyer"] = clean(m.group(1)) if m else None

    # 变动后比例
    m = re.search(r"占(?:公司|上市公司)?总股本(?:的)?([\d.]+)%", t2[:6000])
    out["pct"] = float(m.group(1)) if m else None

    out["no_change"] = bool(re.search(r"(?:未|不会)(?:发生)?(?:导致)?[^。]{0,20}(?:实际控制人|控制权)[^。]{0,10}(?:变更|变化)", t2[:3000]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    idx = pd.read_parquet(OUT / "control_change_index.parquet")
    ev = cluster(idx)
    print(f"公告 {len(idx):,} → 事件簇 {len(ev):,} / {ev['code'].nunique():,} 只", flush=True)
    if args.limit:
        ev = ev.head(args.limit)

    rows = pd.read_parquet(TARGET).to_dict("records") if TARGET.exists() else []
    done = set(json.loads(PROGRESS.read_text(encoding="utf-8"))["ids"]) if PROGRESS.exists() and rows else set()
    queue = [r for r in ev.itertuples() if str(r.id) not in done]
    print(f"待解析 {len(queue):,}", flush=True)

    def work(r):
        txt, pages = fetch_text(r.url, pages=6)
        rec = {"id": str(r.id), "code": str(r.code).zfill(6), "name": r.name, "date": r.date,
               "title": r.title, "url": r.url, "pages": pages, "ok": bool(txt)}
        if txt:
            rec.update(parse(txt))
        return rec

    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(work, r) for r in queue]
        for i, f in enumerate(as_completed(futs), 1):
            rows.append(f.result())
            if i % 100 == 0:
                rate = i / (time.time() - t0)
                print(f"  {i:,}/{len(queue):,} · {rate:.1f}/s · 剩 {(len(queue)-i)/rate/60:.1f} 分钟", flush=True)
            if i % 500 == 0:
                pd.DataFrame(rows).to_parquet(TARGET, index=False)
                PROGRESS.write_text(json.dumps({"ids": [x["id"] for x in rows]}), encoding="utf-8")

    df = pd.DataFrame(rows)
    df = df.loc[:, ~df.columns.duplicated()].drop_duplicates(subset=["id"])
    for c in ["prior_ctrl", "new_ctrl", "buyer"]:
        df[c] = df[c].astype("string")
    df["prior_type"] = df["prior_ctrl"].map(lambda x: classify(str(x)) if isinstance(x, str) else "unknown")
    df["new_type"] = df["new_ctrl"].map(lambda x: classify(str(x)) if isinstance(x, str) else "unknown")
    df["buyer_type"] = df["buyer"].map(lambda x: classify(str(x)) if isinstance(x, str) else "unknown")
    df.to_parquet(TARGET, index=False)
    PROGRESS.write_text(json.dumps({"ids": df["id"].tolist()}), encoding="utf-8")

    print(f"\n解析 {len(df):,} · 成功 {df['ok'].mean():.1%}")
    for c in ["prior_ctrl", "new_ctrl", "buyer", "method", "pct"]:
        print(f"  {c:>11} 覆盖 {df[c].notna().mean():.1%}")
    print("\n变更前类型："); print(df["prior_type"].value_counts().to_string())
    print("\n变更后类型："); print(df["new_type"].value_counts().to_string())

    A = df[(df["prior_type"].isin(["person", "corp"])) & (df["new_type"] == "state")].copy()
    A.to_csv(OUT / "control_change_A.csv", index=False, encoding="utf-8-sig")
    print(f"\nA 级（民营/自然人 → 国资）：{len(A):,} 个 / {A['code'].nunique()} 只")
    if len(A):
        print(A.sort_values("date", ascending=False).head(15)[["date", "code", "name", "prior_ctrl", "new_ctrl", "method", "pct"]].to_string(index=False))


if __name__ == "__main__":
    main()
