"""详式报告：读「前后持股」与「实际控制人」，筛出变动前为民营的事件。

为什么只做详式：简式报告（<20%）不要求披露实际控制人变化，也不要求写前后持股表；
详式（≥20% 或取得控制权）必须写「本次权益变动前，上市公司控股股东为 X，实际控制人为 Y」。

抽取四组：
  1. 封面性质：`权益变动性质：变更实际控制人` / `不变`
  2. 变动前控制人：`本次权益变动前…控股股东为 X，实际控制人为 Y`
  3. 变动后控制人：`…控股股东将变更为 X，实际控制人将变更为 Y`
  4. 前后持股：`本次权益变动前，信息披露义务人持有…A 股，占…B%` / `完成后…C 股，占…D%`

输出：
  data/.scratch/state/prior_controller.parquet
  data/.scratch/state/prior_private.csv     变动前民营 + 买方国资
  data/.scratch/state/prior_report.json     覆盖率与分层

用法：
  backend/.venv/Scripts/python.exe analysis/_parse_prior_controller.py --n 40     # 试跑
  backend/.venv/Scripts/python.exe analysis/_parse_prior_controller.py --all
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
TARGET = OUT / "prior_controller.parquet"
PROGRESS = OUT / "prior_progress.json"

STATE_WORDS = re.compile(
    r"国有资产|国资委|国资运营|国有资本|财政局|财政厅|财政部|国务院|人民政府|管委会|中央汇金|"
    r"中国证券金融|中国国新|诚通|华润|招商局|中粮|保利|中船|兵器|航天|航空工业|中电科|中核|"
    r"中广核|大唐|华能|华电|国家电投|中国三峡|三峡集团|中车|中国建筑|中国铁建|中国交建|"
    r"中国电建|中国能建|中国化学|中国黄金|中国铝业|五矿|中钢|中煤|神华|国家能源|中远海运|"
    r"中国国航|东方航空|南方航空|中国商飞|中国船舶|中国重工|中国中冶|华侨城|中国旅游|"
    r"城投|城建|交投|水务|燃气|地铁|机场|港口|高速公路|国有资产经营|供销社|供销合作"
)
NONE_WORDS = re.compile(r"无实际控制人|不存在实际控制人|股权分散|无控股股东")


def clean(s: str | None, n: int = 40) -> str | None:
    if not s:
        return None
    s = re.sub(r"[（(]以下简称[^）)]*[）)]", "", s)
    s = re.split(r"[，。；、]|住所|注册地址|通讯地址", s)[0].strip()
    return s[:n] if 2 <= len(s) <= n else None


def ctrl_type(name: str | None) -> str:
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    if NONE_WORDS.search(name):
        return "none"
    if STATE_WORDS.search(name):
        return "state"
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}(先生|女士)?", name):
        return "person"
    return "private"


def parse(t: str, listed: str | None = None) -> dict:
    """只抽「上市公司」层面的控制人，不抽信息披露义务人自己的母公司。"""
    t2 = re.sub(r"\s+", "", t)
    out: dict = {}

    m = re.search(r"(?:权益变动性质|股份变动性质|变动性质)[：:]([^。；]{2,40})", t2)
    out["cover_nature"] = m.group(1) if m else None
    out["cover_control_change"] = bool(m and re.search(r"变更实际控制人|实际控制人变更|控制权变更|变更控制权", m.group(1)))

    # 去掉目录：正文从「第一节」第二次出现处开始
    body_start = 0
    first = t2.find("第一节")
    if first >= 0:
        second = t2.find("第一节", first + 3)
        body_start = second if second > 0 else first
    body = t2[body_start:][:16000]

    # 1) 控制权是否变化
    out["no_change"] = bool(
        re.search(r"(?:未|不会|不)(?:导致|引起|致使).{0,30}(?:控股股东|实际控制人).{0,12}(?:变更|变化)", body)
        or re.search(r"(?:控股股东|实际控制人).{0,12}(?:均)?(?:未|没有)(?:发生)?(?:变更|变化)", body)
        or re.search(r"(?:控股股东|实际控制人)(?:仍|依然)(?:为|是)", body)
    )
    out["acquires_control"] = bool(
        re.search(r"成为(?:上市公司)?(?:的)?(?:控股股东|第一大股东)", body)
        or re.search(r"取得(?:上市公司)?(?:的)?控制权", body)
        or re.search(r"(?:上市公司)?(?:的)?(?:控股股东|实际控制人)(?:将)?(?:变更为|由.{2,30}?变更为)", body)
        or out["cover_control_change"]
    )

    # 2) 变动前控制人：多种句式，按可靠度排序；不再只限「上市公司」主语
    def first(patterns, text):
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                v = clean(m.group(1))
                if v:
                    return v
        return None

    zone_end = body.find("本次权益变动后")
    if zone_end < 0:
        zone_end = body.find("本次权益变动完成后")
    prior_zone = body[: zone_end if zone_end > 0 else 6000]
    after_zone = body[zone_end:] if zone_end > 0 else body

    out["prior_ctrl"] = first([
        r"本次权益变动前[^。]{0,40}?(?:上市公司|公司)?(?:的)?实际控制人(?:为|系)([^，。；、]{2,30})",
        r"(?:上市公司|公司)(?:的)?实际控制人(?:为|系)([^，。；、]{2,30})",
    ], prior_zone) or first([
        r"本次权益变动前[^。]{0,40}?实际控制人(?:为|系)([^，。；、]{2,30})",
    ], body)

    out["prior_holder"] = first([
        r"本次权益变动前[^。]{0,40}?(?:上市公司|公司)?(?:的)?控股股东(?:为|系)([^，。；、]{2,30})",
        r"(?:上市公司|公司)(?:的)?控股股东(?:为|系)([^，。；、]{2,30})",
        r"控股股东(?:名称)?[：:]([^，。；、\s]{2,30})",
    ], prior_zone) or first([
        r"本次权益变动前[^。]{0,40}?控股股东(?:为|系)([^，。；、]{2,30})",
    ], body)

    out["new_ctrl"] = first([
        r"实际控制人(?:将)?(?:变更为|由.{2,40}?变更为)([^，。；、]{2,30})",
    ], after_zone)
    out["new_holder"] = first([
        r"控股股东(?:将)?(?:变更为|由.{2,40}?变更为)([^，。；、]{2,30})",
    ], after_zone)

    # 3) 前后持股比例（信息披露义务人自己的）
    def pct(pat):
        m = re.search(pat, body)
        if not m:
            return None
        v = float(m.group(1))
        return v if 0 <= v <= 100 else None

    out["pre_pct"] = pct(r"本次权益变动前[^。]{0,320}?占(?:上市公司|公司)?(?:总股本|股份总数|股本总额)(?:的)?([\d.]+)%")
    out["post_pct"] = pct(r"本次权益变动(?:完成后|后)[^。]{0,320}?占(?:上市公司|公司)?(?:总股本|股份总数|股本总额)(?:的)?([\d.]+)%")

    m = re.search(r"(?:转让方|出让方)(?:为|系)?[：:]?([^，。；、]{2,30})", body)
    out["seller"] = clean(m.group(1)) if m else None
    return out


def load_progress() -> set[str]:
    if PROGRESS.exists():
        d = json.loads(PROGRESS.read_text(encoding="utf-8"))
        return set(d.get("ids") or d.get("id") or [])
    return set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    d = pd.read_parquet(OUT / "equity_change_parsed.parquet")
    d = d[d["ok"] & d["doc_type"].isin(["详式报告书", "收购报告书"])].copy()
    d = d.sort_values(["code", "date"]).drop_duplicates(subset=["code", "date"], keep="first")
    if not args.all and args.n:
        d = d.sample(min(args.n, len(d)), random_state=42)
    print(f"详式事件 {len(d):,}", flush=True)

    rows = pd.read_parquet(TARGET).to_dict("records") if TARGET.exists() else []
    done = load_progress() if rows else set()
    queue = [r for r in d.itertuples() if str(r.id) not in done]
    print(f"待解析 {len(queue):,}", flush=True)

    def work(r):
        txt, _ = fetch_text(r.url, pages=18)
        rec = {"id": str(r.id), "code": r.code, "name": r.name, "date": r.date, "title": r.title,
               "url": r.url, "discloser": r.discloser, "disclosers": r.disclosers,
               "method": getattr(r, "method", None), "doc_type": r.doc_type, "run_ok": bool(txt)}
        if txt:
            rec.update(parse(txt, r.name))
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
    df = df.loc[:, ~df.columns.duplicated()]
    df = df.drop_duplicates(subset=["id"])
    df["buyer_type"] = df["discloser"].fillna("").astype(str).map(classify)
    df["prior_type_explicit"] = df["prior_ctrl"].map(ctrl_type)
    df["holder_type_explicit"] = df["prior_holder"].map(ctrl_type)

    # 推断：若报告称「控制人未变更」，则变动前 = 变动后 = 买方一侧
    def infer(row) -> str:
        if row["prior_type_explicit"] != "unknown":
            return row["prior_type_explicit"]
        if row["no_change"]:
            return row["buyer_type"]
        return "unknown"

    df["prior_type"] = df.apply(infer, axis=1)
    df["prior_basis"] = df.apply(
        lambda r: "报告明写" if r["prior_type_explicit"] != "unknown" else ("未变更推断" if r["no_change"] else "无"), axis=1)
    df.to_parquet(TARGET, index=False)
    PROGRESS.write_text(json.dumps({"id": df["id"].tolist()}), encoding="utf-8")

    print(f"\n解析 {len(df):,} · 成功 {df['run_ok'].mean():.1%}")
    for c in ["prior_ctrl", "prior_holder", "new_ctrl", "pre_pct", "post_pct", "cover_nature"]:
        print(f"  {c:>16} 覆盖 {df[c].notna().mean():.1%}")
    print("\n变动前控制人类型：")
    print(df["prior_type"].value_counts().to_string())
    print("\n控制状态：")
    print(f"  取得控制权（成为控股股东/变更为/封面） {df['acquires_control'].mean():.1%}")
    print(f"  正文写「未发生变更」                  {df['no_change'].mean():.1%}")
    print("\n前控制人判定依据：")
    print(df["prior_basis"].value_counts().to_string())

    TRANSFER = ("无偿划转", "国有股行政划转")
    mcol = df["method"].astype("string").fillna("") if "method" in df.columns else pd.Series("", index=df.index)
    base = df[(df["buyer_type"] == "state") & (~mcol.isin(TRANSFER))].copy()

    # A 级：报告明写变动前为民营/自然人，买方国资
    a = base[(base["prior_type_explicit"].isin(["private", "person"])) & (base["acquires_control"].fillna(False))].copy()
    # B 级：取得控制权、非划转、前控制人无法读出（多为民营，但未证实）
    b = base[(base["acquires_control"].fillna(False)) & (base["prior_type_explicit"] == "unknown") & (~base["no_change"].fillna(False))].copy()
    cand = pd.concat([a.assign(evidence="A 明写民营"), b.assign(evidence="B 取得控制权·未明写")])
    cand = cand.sort_values("date", ascending=False)
    cand.to_csv(OUT / "prior_private.csv", index=False, encoding="utf-8-sig")

    report = {
        "events": int(len(df)),
        "coverage": {c: round(float(df[c].notna().mean()), 3) for c in ["prior_ctrl", "prior_holder", "new_ctrl", "pre_pct", "post_pct"]},
        "prior_type": {k: int(v) for k, v in df["prior_type"].value_counts().items()},
        "buyer_type": {k: int(v) for k, v in df["buyer_type"].value_counts().items()},
        "control_change": round(float(df["acquires_control"].mean()), 3),
        "no_change": round(float(df["no_change"].mean()), 3),
        "candidates_A": int(len(a)),
        "candidates_B": int(len(b)),
        "candidates": int(len(cand)),
        "candidates_codes": int(cand["code"].nunique()) if len(cand) else 0,
    }
    (OUT / "prior_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n买方国资 + 取得控制权：A 级（明写民营）{len(a):,} · B 级（未明写）{len(b):,} · 合计 {len(cand):,} 个 / {report['candidates_codes']} 只")
    if len(cand):
        print(cand.head(12)[["date", "code", "name", "discloser", "prior_ctrl", "new_ctrl", "pre_pct", "post_pct", "evidence"]].to_string(index=False))


if __name__ == "__main__":
    main()
