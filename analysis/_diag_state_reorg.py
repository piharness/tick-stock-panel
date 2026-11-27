"""国资入主后 12 个月内是否注入资产：与事件收益对照。

要检验的那句话：
  「如果国资入主后 12 个月内发生资产重组的子样本显著为正，右尾消失就不是制度缺陷，而是未到兑现时点。」

数据源：东财并购重组库 RPTA_WEB_BGCZMX（按 SCODE 逐只查）
  严格口径（注入）：ZRFS 含「发行股份购买」——上市公司发行股份购买资产，唯一无歧义的注入路径
  宽松口径（扩张）：OBJTYPE=股权 且 ZRFS 不是拍卖/司法裁定/行政划拨——含协议收购、增资
  剔除：JD=停止实施/未通过

窗口：控制权变更公告日 D 起 365 天（另算 730 天）
收益：复用 control_change_event.csv 的 car20/40/60/120

输出：data/.scratch/state/reorg_deals.parquet · state_reorg.json
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/bgcz/"}
DEAD = ("停止实施", "未通过")


def fetch(code: str) -> list[dict]:
    p = {
        "reportName": "RPTA_WEB_BGCZMX", "columns": "ALL", "source": "WEB", "client": "WEB",
        "pageNumber": "1", "pageSize": "100", "sortColumns": "SCGGRQ", "sortTypes": "1",
        "filter": f'(SCODE="{code}")',
    }
    for _ in range(3):
        try:
            u = BASE + "?" + urllib.parse.urlencode(p)
            with urllib.request.urlopen(urllib.request.Request(u, headers=HEADERS), timeout=25) as r:
                d = json.loads(r.read().decode())
            return (d.get("result") or {}).get("data") or []
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return []


def nw_t(x: np.ndarray, lag: int = 6) -> float:
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    e = x - x.mean(); s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    return float(x.mean() / np.sqrt(max(s, 1e-18) / n))


def main() -> None:
    A = pd.read_parquet(OUT / "control_change_A.parquet")
    A["code"] = A["code"].astype(str).str.zfill(6)
    codes = sorted(A["code"].unique())
    cache = OUT / "reorg_deals.parquet"
    if cache.exists():
        deals = pd.read_parquet(cache)
    else:
        rows = []
        for i, c in enumerate(codes, 1):
            for x in fetch(c):
                rows.append({
                    "code": c, "date": str(x.get("SCGGRQ"))[:10], "zrfs": x.get("ZRFS"),
                    "jd": x.get("JD"), "obj": x.get("OBJTYPE"),
                    "target": str(x.get("H_COMNAME"))[:40], "amount": x.get("JYJE"),
                })
            if i % 30 == 0:
                print(f"  {i}/{len(codes)}", flush=True)
            time.sleep(0.18)
        deals = pd.DataFrame(rows)
        deals.to_parquet(cache, index=False)
    deals["date"] = pd.to_datetime(deals["date"], errors="coerce")
    deals["code"] = deals["code"].astype(str).str.zfill(6)
    deals = deals[~deals["jd"].isin(DEAD)].copy()
    deals["is_inject"] = deals["zrfs"].astype(str).str.contains("发行股份购买")
    deals["is_equity"] = (deals["obj"] == "股权") & ~deals["zrfs"].astype(str).str.contains("拍卖|司法|行政划拨")
    print(f"并购记录 {len(deals):,} / 注入 {int(deals['is_inject'].sum()):,} / 股权交易 {int(deals['is_equity'].sum()):,}")

    ev = pd.read_csv(OUT / "control_change_event.csv", dtype={"code": str})
    ev["date"] = pd.to_datetime(ev["date"])
    ev["code"] = ev["code"].str.zfill(6)
    E = ev[ev["cls"] != "private"].copy()

    def flag(row, days: int, col: str):
        hi = row["date"] + pd.Timedelta(days=days)
        m = deals[(deals["code"] == row["code"]) & (deals[col]) & (deals["date"] >= row["date"]) & (deals["date"] <= hi)]
        return m["date"].min() if len(m) else pd.NaT

    for days in (365, 730):
        E[f"inj{days}"] = E.apply(lambda r: flag(r, days, "is_inject"), axis=1)
        E[f"eq{days}"] = E.apply(lambda r: flag(r, days, "is_equity"), axis=1)

    def agg(g: pd.DataFrame, label: str) -> dict:
        o = {"group": label, "n": int(len(g))}
        for k in (20, 40, 60, 120):
            x = g[f"car{k}"].dropna()
            if len(x) < 5:
                o[f"f{k}"] = None; o[f"t{k}"] = None; o[f"win{k}"] = None
                continue
            mm = g.dropna(subset=[f"car{k}"]).assign(ym=g["date"].dt.to_period("M"))
            bym = mm.groupby("ym")[f"car{k}"].mean()
            o[f"f{k}"] = round(float(x.mean() * 100), 1)
            o[f"t{k}"] = round(nw_t(bym.to_numpy()), 2)
            o[f"win{k}"] = round(float((x > 0).mean() * 100), 1)
        return o

    groups = [
        agg(E[E["inj365"].notna()], "12m 内注入（发行股份购买）"),
        agg(E[E["inj730"].notna()], "24m 内注入"),
        agg(E[E["eq365"].notna()], "12m 内股权收购（宽）"),
        agg(E[E["inj730"].isna()], "24m 内无注入"),
    ]

    diff = {}
    for col, name in [("inj365", "注入 − 无注入"), ("eq365", "股权收购 − 无")]:
        w = E.dropna(subset=["car60"]).assign(ym=E["date"].dt.to_period("M"))
        piv = w.pivot_table(index="ym", columns=w[col].notna(), values="car60", aggfunc="mean")
        if True in piv.columns and False in piv.columns:
            d = (piv[True] - piv[False]).dropna()
            diff[name] = {"v": round(float(d.mean() * 100), 1), "t": round(nw_t(d.to_numpy()), 2), "months": int(len(d))}

    res = {"events": int(len(E)), "inject12": int(E["inj365"].notna().sum()), "inject24": int(E["inj730"].notna().sum()),
           "equity12": int(E["eq365"].notna().sum()), "groups": groups, "diff": diff}
    (OUT / "state_reorg.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"A 级 {len(E)} · 12m 注入 {res['inject12']} · 24m 注入 {res['inject24']} · 12m 股权收购 {res['equity12']}")
    print(f"{'组':<28}{'n':>4}{'f20':>8}{'f40':>8}{'f60':>8}{'t':>7}{'胜率':>7}{'f120':>8}")
    for g in groups:
        def f(v, d=1):
            return f"{v:>{d}.1f}" if isinstance(v, (int, float)) else f"{'—':>{d}}"
        print(f"{g['group']:<28}{g['n']:>4}{f(g['f20']):>8}{f(g['f40']):>8}{f(g['f60']):>8}"
              f"{f(g['t60'],2):>7}{f(g['win60'],1):>7}{f(g['f120']):>8}")
    for k, v in diff.items():
        print(f"{k} · f60：{v['v']:+.1f}pp · t {v['t']} · {v['months']} 个月")


if __name__ == "__main__":
    main()
