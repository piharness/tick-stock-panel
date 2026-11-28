"""冷却额度诊断：60 日冷却吃掉多少候选段首，以及账户启用切口暴露多大。

背景（2026-11-27 真事）：电子在 11-27 重新成为 CANDIDATE，账户不买 —— 因为
11-04 的段首还在 60 日冷却里。但 11-04 早于账户启用日 11-20，账户从未持有过
那笔，冷却额度却照常被消耗。本脚本只做静态量化，不跑策略变体
（预注册 `prereg-cooldown-credit.md` 的纪律：定规则之前不偷看收益结果）。

输出三段：
1. raw 候选段首 / 冷却后事件 / 被吃掉的数量
2. 被吃掉的段首距上一段首的交易日数：<20（与持仓重叠）vs ≥20（上一笔已平仓）
3. 账户启用日前 60 个交易日内的段首（= 启用时已被占用、但从未进场的额度）与到期日

用法：python analysis/_diag_cooldown_credit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch as W  # noqa: E402
import etf_watch_experiments as X  # noqa: E402

ADOPTION = pd.Timestamp("2026-11-20")


def main() -> None:
    m = pd.read_parquet(ROOT / "data/etf_watch/_scan_matrix.parquet")
    m["date"] = pd.to_datetime(m["date"])
    mats = X.daily_matrices(m)
    st, dd = mats["status"], mats["dd"]
    sessions = list(st.index)
    pos = {t: i for i, t in enumerate(sessions)}

    cand = st.eq("CANDIDATE")
    raw = W.run_starts(cand).fillna(False)
    kept = X.build_events(st, dd)

    # ---- 1. 总量
    raw_n = int(raw.sum().sum())
    print(f"raw 候选段首 {raw_n} 个 / 冷却后保留 {len(kept)} 个 / 被吃掉 {raw_n - len(kept)} 个"
          f"（{(raw_n - len(kept)) / raw_n:.0%}）")

    # ---- 2. 被吃掉的段首距同行业上一段首多少交易日
    rows = []
    for ind in raw.columns:
        last = None
        for t in raw.index[raw[ind]]:
            i = pos[t]
            if last is not None and i - last < W.COOLDOWN:
                rows.append({"industry": ind, "date": t, "gap": i - last, "prev": sessions[last]})
            else:
                last = i
    eaten = pd.DataFrame(rows)
    if eaten.empty:
        print("没有被吃掉的段首")
    else:
        overlap = eaten[eaten.gap < W.HOLD]
        after = eaten[eaten.gap >= W.HOLD]
        print(f"其中距上一段首 <{W.HOLD} 日（上一笔还没到期平仓）：{len(overlap)} 个")
        print(f"距上一段首 ≥{W.HOLD} 日（上一笔已平仓，额度仍未解锁）：{len(after)} 个")
        if len(after):
            print(after.assign(date=lambda d: d.date.dt.date,
                               prev=lambda d: d.prev.map(lambda x: x.date()))
                  .to_string(index=False))

    # ---- 3. 启用切口：启用日前 60 交易日内已有段首的行业
    if ADOPTION in pos:
        idx = pos[ADOPTION]
        win = {t for t in sessions if idx - W.COOLDOWN <= pos[t] < idx}
        ghost = kept[kept["date"].isin(win)]
        print(f"\n账户启用 {ADOPTION.date()} 前 {W.COOLDOWN} 个交易日内已有段首（额度被占、但账户从未进场）：")
        for ind, g in ghost.groupby("industry"):
            for d in g["date"]:
                expire_i = pos[d] + W.COOLDOWN
                if expire_i < len(sessions):
                    expire = str(sessions[expire_i].date())
                else:
                    # 矩阵末端之外：用 60 个工作日近似（忽略节假日，标 ≈）
                    expire = f"≈{(d + pd.offsets.BDay(W.COOLDOWN)).date()}"
                print(f"  {ind:6s} 段首 {d.date()} → 解锁 {expire}"
                      f"（距启用 {pos[d] - idx:+d} 个交易日）")
        print(f"共 {ghost['industry'].nunique()} 个行业 / {len(ghost)} 个段首")
    else:
        print(f"\n[skip] 启用日 {ADOPTION.date()} 不在扫描矩阵里")


if __name__ == "__main__":
    main()
