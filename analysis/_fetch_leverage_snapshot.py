"""接外部杠杆数据：融资余额 + 股权质押 —— 先做代理性检验，再决定是否全量回填。

这一步不是测「雪崩假说」，而是回答一个更前置的问题：
  融资余额占比 / 质押比例，是不是又是「换手/规模/波动/久期」的代理？
  全卷 H1–H8 已经 8 次发现「标签拆开是换手/规模/波动/涨跌幅」。杠杆数据是最后的外部数据源，
  若它也是代理，就当场证伪；若正交，才值得花时间回填历史做事件研究。

数据源（东方财富 datacenter，免鉴权，urllib 直连，注意限流）：
  融资：RPTA_WEB_RZRQ_GGMX（个股两融明细，RZYEZB = 融资余额/流通市值 %）
  质押：RPTA_APP_ACCUMDETAILS（重要股东质押明细，ACCUM_PLEDGE_TSR = 累计质押占总股本 %，
        WARNING_LINE=预警线 / OPENLINE=平仓线）

产物：
  data/.scratch/leverage/margin_snapshot.parquet   （全市场两融标的某日快照）
  data/.scratch/leverage/pledge_snapshot.parquet   （股票级累计质押比例快照）
  data/.scratch/leverage/corr.json                 （与换手/波动/规模/久期/脆弱性的相关）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
OUT.mkdir(parents=True, exist_ok=True)
INST = ROOT / "data" / "instruments" / "instruments.parquet"
H1 = ROOT / "data" / ".scratch" / "chip_h1" / "panel.parquet"

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://data.eastmoney.com/rzrq/"}


def get_json(params: dict) -> dict:
    url = BASE + "?" + urlencode(params)
    req = Request(url, headers=HEADERS)
    for _ in range(3):
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"      retry ({e})", flush=True)
            time.sleep(2)
    raise RuntimeError("fetch failed")


def fetch_margin_snapshot(date: str = "2026-11-25") -> pd.DataFrame:
    rows = []
    page = 1
    while True:
        d = get_json({"reportName": "RPTA_WEB_RZRQ_GGMX", "columns": "ALL", "source": "WEB",
                      "sortColumns": "RZYE", "sortTypes": "-1", "pageNumber": str(page),
                      "pageSize": "500", "filter": f"(DATE='{date}')"})
        data = d.get("result", {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        total = d.get("result", {}).get("count", 0)
        if page * 500 >= total:
            break
        page += 1
        time.sleep(0.3)
    df = pd.DataFrame(rows)
    print(f"      融资快照 {len(df)} 只（{date}）", flush=True)
    return df


def fetch_pledge_snapshot(since: str = "2024-11-01") -> pd.DataFrame:
    rows = []
    page = 1
    while True:
        d = get_json({"reportName": "RPTA_APP_ACCUMDETAILS", "columns": "ALL", "source": "WEB",
                      "sortColumns": "NOTICE_DATE", "sortTypes": "-1", "pageNumber": str(page),
                      "pageSize": "500", "filter": f"(NOTICE_DATE>='{since}')"})
        data = d.get("result", {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        total = d.get("result", {}).get("count", 0)
        if page * 500 >= total:
            break
        page += 1
        time.sleep(0.3)
    df = pd.DataFrame(rows)
    print(f"      质押明细 {len(df)} 条（{since} 起）", flush=True)
    return df


def main():
    # 1) 拉融资快照
    print("[1/3] 融资快照 ...", flush=True)
    m = fetch_margin_snapshot()
    m = m[["SCODE", "SECNAME", "RZYE", "RZRQYE", "RZYEZB", "SPJ"]].copy()
    m["code"] = m["SCODE"].astype(str).str.zfill(6)
    m.to_parquet(OUT / "margin_snapshot.parquet", index=False)

    # 2) 拉质押明细并聚合
    print("[2/3] 质押快照 ...", flush=True)
    p = fetch_pledge_snapshot()
    p["code"] = p["SECURITY_CODE"].astype(str).str.zfill(6)
    p["notice"] = pd.to_datetime(p["NOTICE_DATE"])
    p["accum"] = pd.to_numeric(p["ACCUM_PLEDGE_TSR"], errors="coerce")
    p["control"] = p["IS_CONTROL_SHAREHOLDER"].astype(str)
    # 股票级：每个股东取最新一条的 ACCUM_PLEDGE_TSR，再求和
    p = p.dropna(subset=["accum", "notice"])
    p = p.sort_values("notice")
    latest_per_holder = p.groupby(["code", "HOLDER_NAME"])["accum"].last().reset_index()
    pledge = latest_per_holder.groupby("code")["accum"].sum().rename("pledge_tsr").reset_index()
    # 控股股东质押占比（单独）
    ctrl = p[p["control"] == "1"].sort_values("notice").groupby(["code", "HOLDER_NAME"])["accum"].last().reset_index()
    ctrl_sum = ctrl.groupby("code")["accum"].sum().rename("pledge_ctrl_tsr").reset_index()
    pledge = pledge.merge(ctrl_sum, on="code", how="left")
    pledge.to_parquet(OUT / "pledge_snapshot.parquet", index=False)
    print(f"      股票级质押比例 {len(pledge)} 只", flush=True)

    # 3) 代理性检验
    print("[3/3] 与换手/波动/规模/久期/脆弱性的相关 ...", flush=True)
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    h1 = pd.read_parquet(H1)
    h1["date"] = pd.to_datetime(h1["date"])
    last = h1[h1["date"] == h1["date"].max()][["symbol", "D", "turn20", "vol20", "logamt20", "fragility", "fwd20"]].copy()
    last["symbol"] = last["symbol"].astype(str)

    def merge_and_corr(df, key):
        df = df.merge(inst, on="code", how="left")
        df["symbol"] = df["symbol"].astype(str)
        df = df.merge(last, on="symbol", how="inner")
        out = {}
        for col in ["D", "turn20", "vol20", "logamt20", "fragility"]:
            x = df[[key, col]].dropna()
            if len(x) > 50:
                out[col] = round(float(x.corr(method="spearman").iloc[0, 1]), 3)
        out["n"] = int(len(df))
        return out

    m["symbol"] = m["code"]
    corr_margin = merge_and_corr(m[["code", "RZYEZB"]], "RZYEZB")
    corr_pledge = merge_and_corr(pledge[["code", "pledge_tsr"]], "pledge_tsr")
    corr_ctrl = merge_and_corr(pledge.dropna(subset=["pledge_ctrl_tsr"])[["code", "pledge_ctrl_tsr"]], "pledge_ctrl_tsr")

    # 描述
    summary = {
        "margin": {"n": int(len(m)), "RZYEZB_median": round(float(m["RZYEZB"].median()), 2),
                   "RZYEZB_p90": round(float(m["RZYEZB"].quantile(0.9)), 2)},
        "pledge": {"n": int(len(pledge)), "pledge_tsr_median": round(float(pledge["pledge_tsr"].median()), 2),
                   "pledge_tsr_p90": round(float(pledge["pledge_tsr"].quantile(0.9)), 2),
                   "nonzero_share": round(float((pledge["pledge_tsr"] > 0).mean()), 3)},
        "corr_margin_RZYEZB": corr_margin,
        "corr_pledge_tsr": corr_pledge,
        "corr_pledge_ctrl_tsr": corr_ctrl,
    }
    (OUT / "corr.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
