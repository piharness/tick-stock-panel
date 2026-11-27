"""国资入股 / 纾困：权益变动报告书字段抽取（200 条级验证版）。

输入：data/.scratch/state/state_injection_candidates.parquet（三条检索式：权益变动 国有/国资/纾困）
输出：data/.scratch/state/injection_events.parquet + _validate.csv

抽取字段：
  listed_name / listed_code / discloser / change_nature / method
  shares / pct / price / amount / controller_after / transferor
每个字段附 evidence（命中处前后 80 字），供人工核对准确率。

不追求全自动：先量准确率，再决定要不要扩到全量、要不要上 LLM 兜底。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"
PDF = OUT / "pdf"
QUERIES = ["权益变动 国有", "权益变动 国资", "权益变动 纾困"]


def norm(t: str) -> str:
    t = t.replace("\u3000", " ").replace("\xa0", " ").replace("\u2212", "-")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t


def grab(pat: str, t: str, flags: int = 0, group: int = 1):
    m = re.search(pat, t, flags)
    if not m:
        return None, None
    s = max(0, m.start() - 80)
    return m.group(group).strip(), t[s : m.end() + 80].replace("\n", " ")


def parse(t: str) -> dict:
    t = norm(t)
    head = t[:2500].replace("\n", " ")
    out: dict = {}

    out["listed_name"], _ = grab(r"上市公司名称[：:]\s*\n?\s*([^\n]{2,40})", t)
    out["listed_code"], _ = grab(r"股票代码[：:]\s*(\d{6})", t)
    out["discloser"], _ = grab(r"信息披露义务人(?:名称)?(?:[（(][一二三四1-9][）)])?[：:]\s*\n?\s*([^\n]{2,60})", t)
    out["change_nature"], _ = grab(r"股份变动性质[：:]\s*\n?\s*([^\n]{2,40})", t)
    cn = out.get("change_nature") or ""
    if "增加" in cn or "增持" in cn:
        out["direction"] = "increase"
    elif "减少" in cn or "减持" in cn:
        out["direction"] = "decrease"
    elif "不变" in cn:
        out["direction"] = "unchanged"
    else:
        out["direction"] = None

    # 方式：优先正文里的「本次权益变动方式为…」，否则按关键词
    raw, ev = grab(r"本次(?:权益变动|股份变动)方式为([^。；\n]{2,60})", t)
    method = None
    for kw in ["无偿划转", "表决权委托", "司法拍卖", "协议转让", "集中竞价", "大宗交易", "发行股份购买资产", "继承", "赠与"]:
        if raw and kw in raw:
            method = kw
            break
    if not method:
        for kw in ["无偿划转", "表决权委托", "司法拍卖", "协议转让", "集中竞价", "大宗交易", "发行股份购买资产", "继承", "赠与"]:
            if kw in t[:12000]:
                method = kw
                break
    out["method"] = method
    out["method_ev"] = ev

    # 股数 + 比例：取「本次」附近第一处
    # 股数 × 比例：先收集所有候选对，再用总股本选一致的那一对（见 resolve_size）
    cands = []
    for m in re.finditer(r"([\d,]{3,}(?:\.\d+)?)\s*(万)?股[^。；]{0,70}?([\d.]+)\s*%", t):
        sh = float(m.group(1).replace(",", "")) * (10000 if m.group(2) else 1)
        pc = float(m.group(3))
        cands.append({"shares": sh, "pct": pc, "ev": t[max(0, m.start() - 80) : m.end() + 60].replace("\n", " ")})
    out["size_cands"] = json.dumps(cands[:12], ensure_ascii=False)

    # 标准表格优先：简式（准则15号）直接给「变动数量/变动比例」；详式给「变动前/后」，相减
    out["size_pair"] = None
    t2 = re.sub(r"\s+", "", t)
    m = re.search(r"本次发生拥有权益的股份变动的数量及变动比例(.{0,500})", t2, re.S)
    if m:
        seg = m.group(1)
        a = re.search(r"变动数量[：:]?([\d,]+(?:\.\d+)?)(万)?股", seg)
        b = re.search(r"变动比例[：:]?([\d.]+)%", seg)
        if a and b:
            out["size_pair"] = [float(a.group(1).replace(",", "")) * (10000 if a.group(2) else 1), float(b.group(1)), "简式变动表"]
    if out["size_pair"] is None:
        pre = re.search(r"本次权益变动前(.{0,400}?)持股数量[：:]?([\d,]+)股(.{0,200}?)持股比例[：:]?([\d.]+)%", t2, re.S)
        post = re.search(r"本次权益变动后(.{0,400}?)持股数量[：:]?([\d,]+)股(.{0,200}?)持股比例[：:]?([\d.]+)%", t2, re.S)
        if pre and post:
            ds = float(post.group(2).replace(",", "")) - float(pre.group(2).replace(",", ""))
            dp = float(post.group(4)) - float(pre.group(4))
            if ds != 0:
                out["size_pair"] = [abs(ds), abs(dp), "详式前后相减"]

    out["price"], out["price_ev"] = grab(r"(?:转让价格|每股转让价格|转让单价|划转价格|转让价)[：:为是]?\s*(?:人民币)?\s*([\d.]+)\s*元\s*/?\s*股", t)
    out["amount"], out["amount_ev"] = grab(r"(?:作价|股份转让价款|转让价款|交易金额|转让总价)(?:合计)?(?:为)?(?:人民币)?\s*([\d,]+(?:\.\d+)?)\s*元", t)

    m = re.search(r"实际控制人(?:将)?(?:变更为|由[^。；]{2,30}?变更为)\s*([^\n。；]{2,30})", t)
    if m:
        out["controller_after"] = m.group(1).strip()
        out["controller_ev"] = t[max(0, m.start() - 90) : m.end() + 90].replace("\n", " ")
    elif re.search(r"实际控制人(?:未|没有)(?:发生)?(?:变更|变化)", t):
        out["controller_after"] = "未变更"
        out["controller_ev"] = "实际控制人未变更"
    else:
        out["controller_after"] = None
        out["controller_ev"] = None

    out["transferor"], out["transferor_ev"] = grab(r"转让方[：:]\s*([^\n]{2,30})", t)
    if not out["transferor"]:
        out["transferor"], out["transferor_ev"] = grab(r"([\u4e00-\u9fa5A-Za-z（）()]{4,30})(?:将|拟将)(?:其)?(?:所)?持有的", t)
    out["acquirer"], out["acquirer_ev"] = grab(r"受让方[：:]\s*([^\n]{2,30})", t)
    return out


def main() -> None:
    PDF.mkdir(parents=True, exist_ok=True)
    c = pd.read_parquet(OUT / "state_injection_candidates.parquet")
    c = c[c["query"].isin(QUERIES)].drop_duplicates(subset=["code", "id"]).copy()
    c = c[c["title"].str.contains("权益变动|收购|纾困|划转", regex=True)]
    rows = []
    for i, r in enumerate(c.itertuples(), 1):
        p = PDF / f"{r.code}_{r.id}.pdf"
        if not p.exists():
            try:
                urllib.request.urlretrieve(r.url, p)
                time.sleep(0.15)
            except Exception as e:  # noqa: BLE001
                print(f"  skip {r.code} {e}")
                continue
        try:
            doc = pymupdf.open(p)
            txt = "\n".join(page.get_text() for page in doc)
        except Exception as e:  # noqa: BLE001
            print(f"  parse fail {r.code} {e}")
            continue
        rec = {"code": r.code, "name": r.name, "date": r.date, "title": r.title, "url": r.url, "pages": len(doc)}
        rec.update(parse(txt))
        rows.append(rec)
        if i % 20 == 0:
            print(f"  {i}/{len(c)}", flush=True)
    df = pd.DataFrame(rows)
    inst = pd.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet", columns=["code", "total_shares"])
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df.merge(inst, on="code", how="left")
    picks = []
    for r in df.itertuples():
        best = None
        if getattr(r, "size_pair", None):
            sp = r.size_pair
            picks.append((sp[0], sp[1], sp[2], True, 1.0))
            continue
        for c in json.loads(r.size_cands or "[]"):
            if not r.total_shares or r.total_shares <= 0 or c["pct"] <= 0 or c["shares"] <= 0:
                continue
            implied = c["shares"] / (c["pct"] / 100.0)          # 报告时点的总股本
            ratio = implied / r.total_shares                     # 与当前总股本的比
            score = abs(np.log(ratio)) if ratio > 0 else 99
            ev = c.get("ev", "")
            if re.search(r"变动数量|受让|转让|划转|增持|减持|变动比例|本次权益变动", ev):
                score -= 1.2
            if re.search(r"变动后持有|披露前持有|持股数量|持股比例|合计持有", ev):
                score += 1.2
            if best is None or score < best[0]:
                best = (score, c, ratio)
        if best:
            ok = 0.3 <= best[2] <= 3.0
            picks.append((best[1]["shares"], best[1]["pct"], best[1]["ev"], ok, round(best[2], 3)))
        else:
            picks.append((None, None, None, False, None))
    df[["shares", "pct", "size_ev", "size_ok", "size_err"]] = pd.DataFrame(picks, index=df.index)
    df = df.drop(columns=["size_cands", "size_pair"], errors="ignore")
    df.to_parquet(OUT / "injection_events.parquet", index=False)
    df.to_csv(OUT / "injection_events.csv", index=False, encoding="utf-8-sig")
    print(f"\nn={len(df)}")
    for col in ["listed_name", "discloser", "change_nature", "method", "shares", "pct", "price", "amount", "controller_after", "transferor"]:
        print(f"  {col:>16} 覆盖 {df[col].notna().mean():.1%}")
    rep = df[df["title"].str.contains("权益变动报告书")]
    print(f"  报告书 n={len(rep)} · 股数/比例合理(0.3–3x) {rep['size_ok'].mean():.1%} · 覆盖 {rep['shares'].notna().mean():.1%}")
    print(f"  隐含总股本/当前总股本 中位 {rep['size_err'].median():.2f} · p10 {rep['size_err'].quantile(.1):.2f} · p90 {rep['size_err'].quantile(.9):.2f}")


if __name__ == "__main__":
    main()
