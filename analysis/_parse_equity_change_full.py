"""《权益变动报告书》全量字段解析（流式，不落盘 PDF）。

为什么不用标题预筛：实测通用标题的简式报告书里 8.5% 的披露人是国资，
而标题筛只捞出 407/33,588（1.2%），召回约 14%。所以只能按文档解析。

流程：
  事件去重（code+date，一份报告书 + 核查意见 + 法律意见算一个事件，只解析报告书）
  → 流式下载（内存解析，写盘只写结果）→ 抽取 → 分类（国资/金融/自然人/一般法人）

关键字段：
  discloser      信息披露义务人（封面），本轮的核心——买方是谁
  acquirer_type  state / finance / person / corp
  method / direction / shares / pct / price / controller_after

用法：
  抽样跑通（默认 3,000 条，按年分层）：
    backend/.venv/Scripts/python.exe analysis/_parse_equity_change_full.py --n 3000
  全量：
    backend/.venv/Scripts/python.exe analysis/_parse_equity_change_full.py --all --workers 16

输出：
  data/.scratch/state/equity_change_parsed.parquet
  data/.scratch/state/parse_progress.json（断点续跑，按 doc id）
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TARGET = OUT / "equity_change_parsed.parquet"
PROGRESS = OUT / "parse_progress.json"

# 国资主体：部委 / 省级国资 / 央企集团 / 地方平台
STATE = re.compile(
    r"国有资产|国资委|国资运营|国有资本|国有股权|财政局|财政厅|财政部|国务院|人民政府|管委会|管理委员会|"
    r"中央汇金|中国证券金融|中国国新|诚通|国家电网|南方电网|中国石油|中国石化|中海油|中石油|中石化|"
    r"中国移动|中国电信|中国联通|中国烟草|中国铁路|国铁|华润|招商局|中粮|保利|中船|兵器|航天|"
    r"航空工业|中国电子|中电科|中核|中广核|大唐|华能|华电|国家电投|三峡|中车|中国建筑|中国铁建|"
    r"中国交建|中国电建|中国能建|中国化学|中国黄金|中国铝业|五矿|中钢|中煤|神华|国家能源|"
    r"中远海运|中国国航|东方航空|南方航空|中国商飞|中国船舶|中国重工|中国中冶|华侨城|中国旅游|"
    r"城投|城建投资|交通投资|水务集团|燃气集团|公共交通|地铁集团|机场集团|港口集团|高速公路|"
    r"国有资产经营|国有投资|国有控股"
)
# 模糊平台：只有「地名 + 投资/资本/控股」这类泛化名，需单列一档，不能直接算国资
PLATFORM = re.compile(r"投资控股|投资集团|产业投资|发展投资|建设投资|资本运营|投资发展|控股集团")
FIN = re.compile(r"基金|保险|信托|证券|资产管理|社保|QFII|银行|理财|私募")
PERSON = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")

METHODS = [
    "无偿划转",
    "国有股行政划转",
    "表决权委托",
    "表决权放弃",
    "司法拍卖",
    "协议转让",
    "集中竞价",
    "大宗交易",
    "发行股份购买资产",
    "继承",
    "赠与",
]


def norm(t: str) -> str:
    t = t.replace("\u3000", " ").replace("\xa0", " ").replace("\u2212", "-")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{2,}", "\n", t)


def clean_name(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^[0-9一二三四五六七八九十]+[、.．)）]\s*", "", s)
    s = re.sub(r"^[（(][0-9一二三四五六七八九十]+[）)]", "", s)
    s = re.sub(r"[（(](?:一致行动人|信息披露义务人)[）)]", "", s)
    s = re.sub(r"(先生|女士|同志)$", "", s)
    s = re.split(r"住所|注册地址|通讯地址|股票代码|股票简称|联系电话|邮政编码|签署日期|权益变动方式", s)[0]
    s = s.strip("：:，,。;；")
    return s if 2 <= len(s) <= 40 else None


def grab(pat: str, t: str, flags: int = 0):
    m = re.search(pat, t, flags)
    if not m:
        return None, None
    return m.group(1).strip(), t[max(0, m.start() - 80) : m.end() + 80].replace("\n", " ")


def parse(t: str, listed: str | None, title: str | None = None) -> dict:
    t = norm(t)
    t2 = re.sub(r"\s+", "", t)
    out: dict = {}

    # 披露人：封面格式很多——「信息披露义务人：」「名称：」「义务人一：」「义务人1：」
    # 「信息披露义务人及其一致行动人：A、B」。先抓全部，再按一致行动人拆开。
    PAT_OBLIGOR = re.compile(r"信息披露义务人[一二三四五六七八九十0-9]{0,2}(?:[（(][一二三四1-9][）)])?(?:及其一致行动人)?[：:]\s*([^\n]{2,90})")
    raw_names: list[str] = []
    for scope in (t[:2000], t[:9000]):
        for m in PAT_OBLIGOR.finditer(scope):
            for part in re.split(r"[、,，;；]", m.group(1)):
                v = clean_name(part)
                if v:
                    raw_names.append(v)
        if raw_names:
            break
    out["listed_name"], _ = grab(r"上市公司名称[：:]\s*\n?\s*([^\n]{2,40})", t)
    out["listed_code"], _ = grab(r"股票代码[：:]\s*(\d{6})", t)

    if title:
        m = re.search(r"[（(]([^（）()]{2,24})[）)]\s*$", title)
        if m and not re.search(r"修订|更新|更正|取消|补充|摘要|已取消|二次|三次", m.group(1)):
            v = clean_name(m.group(1))
            if v:
                raw_names.append(v)

    def core(x: str) -> str:
        return re.sub(r"(股份有限公司|有限责任公司|有限公司|集团|控股|中国)$", "", x.replace(" ", ""))

    names = list(dict.fromkeys(raw_names))
    listed_core = core(out["listed_name"] or "") or re.sub(r"^\*?S?T?|退$", "", (listed or "").replace(" ", ""))
    keep = []
    for n in names:
        if listed_core and (
            core(n) == listed_core
            or (len(listed_core) >= 2 and core(n).startswith(listed_core))
            or (len(listed_core) >= 3 and listed_core in core(n))
        ):
            continue
        keep.append(n)
    out["discloser"] = keep[0] if keep else None
    out["disclosers"] = " | ".join(keep[:5]) if keep else None
    out["discloser_fix"] = bool(keep) and keep[0] is not (names[0] if names else None)

    cn, _ = grab(r"股份变动性质[：:]\s*\n?\s*([^\n]{2,40})", t)
    out["change_nature"] = cn
    if cn:
        out["direction"] = "increase" if re.search(r"增加|增持", cn) else "decrease" if re.search(r"减少|减持", cn) else "unchanged" if "不变" in cn else None
    else:
        out["direction"] = None

    method, ev = None, None
    m = re.search(r"本次(?:权益变动|股份变动)的?(?:方式|形式)(?:为|系|是)([^。；\n]{2,80})", t)
    if m:
        seg = m.group(1)
        hits = [k for k in METHODS if k in seg]
        if len(hits) == 1:
            method, ev = hits[0], seg
        else:
            marked = [k for k in METHODS if re.search(r"[☑√■●▉◆]\\s*" + k, seg)]
            if len(marked) == 1:
                method, ev = marked[0], seg
    if method is None:
        marked = [k for k in METHODS if re.search(r"[☑√■●▉◆]\\s*" + k, t[:12000])]
        if len(marked) == 1:
            method, ev = marked[0], "勾选框"
    if method is None:
        m2 = re.search(r"通过(无偿划转|协议转让|集中竞价|大宗交易|司法拍卖|表决权委托|继承|赠与|发行股份购买资产)", t)
        if m2:
            method, ev = m2.group(1), m2.group(0)
    out["method"], out["method_ev"] = method, ev

    # 股数/比例：标准表优先
    size_pair = None
    m = re.search(r"本次发生拥有权益的股份变动的数量及变动比例(.{0,500})", t2, re.S)
    if m:
        seg = m.group(1)
        a = re.search(r"变动数量[：:]?([\d,]+(?:\.\d+)?)(万)?股", seg)
        b = re.search(r"变动比例[：:]?([\d.]+)%", seg)
        if a and b:
            size_pair = [float(a.group(1).replace(",", "")) * (10000 if a.group(2) else 1), float(b.group(1)), "简式变动表"]
    if size_pair is None:
        pre = re.search(r"本次权益变动前(.{0,400}?)持股数量[：:]?([\d,]+)股(.{0,200}?)持股比例[：:]?([\d.]+)%", t2, re.S)
        post = re.search(r"本次权益变动后(.{0,400}?)持股数量[：:]?([\d,]+)股(.{0,200}?)持股比例[：:]?([\d.]+)%", t2, re.S)
        if pre and post:
            ds = float(post.group(2).replace(",", "")) - float(pre.group(2).replace(",", ""))
            dp = float(post.group(4)) - float(pre.group(4))
            if ds != 0:
                size_pair = [abs(ds), abs(dp), "详式前后相减"]
    out["size_pair"] = json.dumps(size_pair, ensure_ascii=False) if size_pair else None

    out["price"], out["price_ev"] = grab(r"(?:转让价格|每股转让价格|转让单价|划转价格|转让价)[：:为是]?\s*(?:人民币)?\s*([\d.]+)\s*元\s*/?\s*股", t)
    out["amount"], _ = grab(r"(?:作价|股份转让价款|转让价款|交易金额|转让总价)(?:合计)?(?:为)?(?:人民币)?\s*([\d,]+(?:\.\d+)?)\s*元", t)

    m = re.search(r"实际控制人(?:将)?(?:变更为|由[^。；]{2,30}?变更为)\s*([^\n。；]{2,30})", t)
    if m:
        out["controller_after"] = m.group(1).strip()
    elif re.search(r"实际控制人(?:未|没有)(?:发生)?(?:变更|变化)", t):
        out["controller_after"] = "未变更"
    else:
        out["controller_after"] = None

    st, _ = grab(r"(?:转让方|出让方)[：:]\s*([^\n]{2,30})", t)
    out["transferor"] = clean_name(st)
    ac, _ = grab(r"(?:受让方|收购人)[：:]\s*([^\n]{2,30})", t)
    out["acquirer"] = clean_name(ac)
    return out


def classify(name: str | None) -> str:
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    if STATE.search(name):
        return "state"
    if PLATFORM.search(name):
        return "platform"
    if FIN.search(name):
        return "finance"
    if PERSON.match(name):
        return "person"
    return "corp"


def fetch_text(url: str, pages: int = 12) -> tuple[str, int]:
    for _ in range(3):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30).read()
            doc = pymupdf.open(stream=raw, filetype="pdf")
            n = len(doc)
            txt = "\n".join(doc[i].get_text() for i in range(min(pages, n)))
            return txt, n
        except Exception:  # noqa: BLE001
            time.sleep(0.8)
    return "", 0


def build_events(df: pd.DataFrame) -> pd.DataFrame:
    """一份事件只解析一份报告书：详式 > 收购 > 简式 > 未标；同类取最早。"""
    order = {"详式报告书": 0, "收购报告书": 1, "简式报告书": 2, "报告书(未标)": 3}
    d = df[df["is_report"]].copy()
    d["rank"] = d["doc_type"].map(order).fillna(9)
    d = d.sort_values(["code", "date", "rank", "id"]).drop_duplicates(subset=["code", "date"], keep="first")
    return d.reset_index(drop=True)


def sample_years(d: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    d = d.copy()
    d["year"] = pd.to_datetime(d["date"]).dt.year
    frac = n / len(d)
    parts = []
    for y, g in d.groupby("year"):
        k = max(30, int(round(len(g) * frac)))
        parts.append(g.sample(min(k, len(g)), random_state=seed))
    return pd.concat(parts).drop_duplicates(subset=["id"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    docs = pd.read_parquet(OUT / "equity_change_docs.parquet")
    ev = build_events(docs)
    todo = ev if args.all else sample_years(ev, args.n, args.seed)
    print(f"事件 {len(ev):,}；本次解析 {len(todo):,}（{'全量' if args.all else f'抽样 {args.n}'}）", flush=True)

    done_rows: list[dict] = []
    done_ids: set[str] = set()
    if TARGET.exists() and PROGRESS.exists():
        done_rows = pd.read_parquet(TARGET).to_dict("records")
        done_ids = set(json.loads(PROGRESS.read_text(encoding="utf-8"))["ids"])
        print(f"续跑：已有 {len(done_rows):,}", flush=True)

    queue = [r for r in todo.itertuples() if str(r.id) not in done_ids]
    print(f"待解析 {len(queue):,}", flush=True)

    def work(r):
        txt, pages = fetch_text(r.url)
        if not txt:
            return {"id": str(r.id), "code": r.code, "name": r.name, "date": r.date, "title": r.title,
                    "doc_type": r.doc_type, "url": r.url, "pages": pages, "ok": False}
        rec = {"id": str(r.id), "code": r.code, "name": r.name, "date": r.date, "title": r.title,
               "doc_type": r.doc_type, "url": r.url, "pages": pages, "ok": True,
               "head_text": re.sub(r"\s+", " ", txt)[:2000]}
        rec.update(parse(txt, r.name, r.title))
        return rec

    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(work, r): r for r in queue}
        for i, f in enumerate(as_completed(futs), 1):
            done_rows.append(f.result())
            if i % 100 == 0:
                rate = i / (time.time() - t0)
                print(f"  {i:,}/{len(queue):,} · {rate:.1f}/s · 剩 {(len(queue)-i)/rate/60:.1f} 分钟", flush=True)
            if i % 1000 == 0:
                pd.DataFrame(done_rows).to_parquet(TARGET, index=False)
                PROGRESS.write_text(json.dumps({"ids": [x["id"] for x in done_rows]}), encoding="utf-8")

    df = pd.DataFrame(done_rows).drop_duplicates(subset=["id"])
    PRI = {"state": 5, "platform": 4, "finance": 3, "person": 2, "corp": 1, "unknown": 0}
    def best_type(row) -> str:
        raw = row.get("disclosers")
        if not isinstance(raw, str) or not raw.strip():
            raw = row.get("discloser")
        if not isinstance(raw, str) or not raw.strip():
            return "unknown"
        parts = [p for p in raw.split("|") if p.strip()]
        return max((classify(p.strip()) for p in parts), key=lambda t: PRI.get(t, 0))
    df["acquirer_type"] = df.apply(best_type, axis=1)
    df.to_parquet(TARGET, index=False)
    df.to_csv(OUT / "equity_change_parsed.csv", index=False, encoding="utf-8-sig")
    PROGRESS.write_text(json.dumps({"ids": df["id"].tolist()}), encoding="utf-8")

    ok = df[df["ok"]]
    print(f"\n解析成功 {len(ok):,}/{len(df):,}（{len(ok)/max(len(df),1):.0%}）")
    for c in ["discloser", "method", "direction", "controller_after", "price"]:
        print(f"  {c:>16} 覆盖 {ok[c].notna().mean():.1%}")
    print("\n披露人类型：")
    print(ok["acquirer_type"].value_counts().to_string())
    st = ok[ok["acquirer_type"] == "state"]
    p = st.shape[0] / max(len(ok), 1)
    print(f"\n国资披露人 {len(st):,}（{p:.1%}，95%CI ±{1.96*np.sqrt(p*(1-p)/max(len(ok),1)):.1%}）")
    print("\n按年国资占比：")
    ok = ok.assign(year=pd.to_datetime(ok["date"]).dt.year)
    print(ok.groupby("year").apply(lambda g: pd.Series({"n": len(g), "state": (g.acquirer_type == "state").mean()})).round(3).to_string())
    print("\n国资样本：")
    print(st[["date", "code", "name", "discloser", "method"]].head(12).to_string(index=False))


if __name__ == "__main__":
    main()
