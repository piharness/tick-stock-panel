"""可行性实测: 用东财免费接口拉全历史日K, 计时 + 口径校验。

验证三件事:
1. 抓取速率 (决定 6000 只全市场的工作量)
2. 退市股能否落盘
3. 与本地 enriched 重叠段是否逐字段一致
"""
import time, json, urllib.request, urllib.error
import pandas as pd
from pathlib import Path

OUT = Path(r"D:/DSH/tick-stock-panel/data/_probe_em")
OUT.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
FIELDS = "f51,f52,f53,f54,f55,f56,f57"   # date,open,close,high,low,vol,amount
COLS = ["date", "open", "close", "high", "low", "volume", "amount"]

def fetch(secid, klt=101, fqt=0, beg="19901219"):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1&fields2={FIELDS}&klt={klt}&fqt={fqt}"
           f"&beg={beg}&end=20500101&lmt=100000")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())["data"]
    if not d or not d.get("klines"):
        return pd.DataFrame(columns=COLS)
    rows = [k.split(",") for k in d["klines"]]
    df = pd.DataFrame(rows, columns=COLS)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

# 样本: 5 只存续 + 4 只退市 + 4 只指数
CASES = [
    ("1.600000", "600000.SH", "浦发(存续)"),
    ("0.000001", "000001.SZ", "平安(存续)"),
    ("1.601318", "601318.SH", "平安保险"),
    ("0.300750", "300750.SZ", "宁德"),
    ("1.688981", "688981.SH", "中芯"),
    ("1.600001", "600001.SH", "邯郸钢铁(退)"),
    ("0.000003", "000003.SZ", "PT金田A(退)"),
    ("1.600087", "600087.SH", "退市长油"),
    ("1.600005", "600005.SH", "武钢(吸并)"),
    ("1.000001", "000001.SH", "上证指数"),
    ("0.399001", "399001.SZ", "深证成指"),
    ("1.000300", "000300.SH", "沪深300"),
    ("1.510050", "510050.SH", "50ETF"),
]

t0 = time.time()
frames = []
print(f"{'标的':14s} {'行数':>6s} {'起':12s} {'止':12s} {'耗时':>6s}")
for secid, sym, name in CASES:
    t = time.time()
    try:
        df = fetch(secid)
        df.insert(0, "symbol", sym)
        frames.append(df)
        print(f"{name:14s} {len(df):6d} {str(df.date.min()):12s} {str(df.date.max()):12s} {time.time()-t:5.2f}s")
    except Exception as e:
        print(f"{name:14s} ERR {type(e).__name__} {str(e)[:40]}")
    time.sleep(0.08)

el = time.time() - t0
all_df = pd.concat(frames, ignore_index=True)
all_df.to_parquet(OUT / "probe.parquet", index=False)
print(f"\n合计 {len(all_df):,} 行, 耗时 {el:.1f}s, 单只均值 {el/len(CASES):.2f}s")
print(f"外推 6000 只: {el/len(CASES)*6000/60:.1f} 分钟 (单线程)")
