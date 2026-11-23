"""腿 1 + 债券 B&H 叠加的运营账户（2026-11-22 启用，裁决见 leg2-bond.md）。

设计原则（防状态漂移）：
- **无增量状态机**：每次运行从启用日全量重放。引擎与回测完全同一个
  （`etf_watch_leg2_rule.replay_joint`，含「腿 1 优先整格补足」修正），
  `account.json` 只是输出快照——不存在「状态文件与引擎不一致」的可能。
- 账户自启用日前最后一个收盘以**全现金**存在（不可交易过去）；
  首个债券动作在下一交易日收盘（tgt 序列首日=0，其余恒 1）。
- 事件源：扫描矩阵（历史，含 60 日冷却）+ 账本 `read_ledger()`（矩阵末日之后的活事件，
  void 语义已内建；只取 `qualified=True`）。
- **执行层与 V1 判定层相互独立**：账户按事件进场（与 JB0 回测口径一致），V1 不挡进场。

每日流程（收盘后）：
    python analysis/etf_watch.py            # 扫描 + 记账本（已有）
    python analysis/etf_watch_account.py    # 本脚本：重放 + 出次日指令
三条指令的语义见 docs/structural-edge/account-runbook.md。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402
import etf_watch_experiments as X  # noqa: E402
import etf_watch_portfolio as pf  # noqa: E402
import etf_watch_leg2_rule as LR  # noqa: E402
import etf_watch_leg2_bond as LB  # noqa: E402
import etf_watch_cash_leg as CL  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/account.json"
DEF_NAME = "511010 国债ETF国泰"
BOND_ALIGN_MIN = 3_000.0   # 债券对齐阈值：差额低于此不动（最低佣金 0.3 元在碎单上放大费率；纸面/实盘由此最多差 0.5% 权益）
ENTRY_CAP = 1.03           # 次日开盘限价 = 信号收盘 ×1.03；改它 = 改执行规则，必须重开预注册
ADOPTION = "2026-11-20"      # 启用日前最后一个收盘（账户以全现金存在于该日）


def load_world():
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)                      # 缓存命中约 1 分钟
    mats = X.daily_matrices(m)
    matrix_end = pd.Timestamp(m["date"].max())
    sessions_all = sorted(set(pd.DatetimeIndex(mats["status"].index)) |
                          set(pd.DatetimeIndex(d["date"].unique())))
    ev_hist = X.build_events(mats["status"], mats["dd"])

    # 矩阵末日之后的活事件（qualified=True），去重后并入
    live, seen = [], {(str(t.date()), ind) for t, ind in zip(ev_hist["date"], ev_hist["industry"])}
    led_events, _ = etf_watch.read_ledger()
    for e in led_events:
        key = (str(pd.Timestamp(e["date"]).date()), e["industry"])
        if pd.Timestamp(e["date"]) > matrix_end and e.get("qualified") and key not in seen:
            seen.add(key)
            live.append({"date": pd.Timestamp(e["date"]), "industry": e["industry"],
                         "dd": e.get("dd"), "ym": str(e["date"])[:7], "source": "ledger"})
    ev = pd.concat([ev_hist, pd.DataFrame(live)], ignore_index=True) if live else ev_hist
    return d, mats, sessions_all, ev, matrix_end


def derive_instructions(r: dict, ev_w: pd.DataFrame, sessions, sym_mat,
                        prices: dict | None = None) -> list[dict]:
    """次日三条指令：①到期卖出 ②新信号按权益/6 进场（钱不够先卖债券） ③剩余对齐债券。

    进场执行（2026-11-23 收敛）：不是收盘市价，而是「次日开盘限价 = 信号收盘 ×1.03」。
    一个限价单同时做两件事：开盘低于限价按开盘成交（不付尾盘溢价），
    开盘高于限价则等回落、不成交就放弃（不追已修复出区间的事件）。
    回测仍按 t+1 收盘（保守口径），执行叠加的读数在
    data/etf_watch/_probe_entry_timing.json。
    """
    asof = sessions[-1]
    eq = float(r["equity"].iloc[-1])
    out: list[dict] = []

    # ① 到期：开放仓位的 exit_i == len(sessions) ⇒ 在「下一交易日」到期
    nxt_i = len(sessions)
    exits = [p for p in r["open_positions"] if p.get("exit_i") == nxt_i]
    for p in exits:
        out.append({"no": len(out) + 1, "action": "SELL",
                    "what": f"{p['symbol']}（{p['industry']}）",
                    "est_amount": round(p["value"] * (1 - pf.COST), 0),
                    "why": f"持有满 20 个交易日（{p['entry']} 进场）",
                    "when": "下一交易日收盘"})

    # ② 新信号：最后一个收盘产生、尚未进场的信号
    pend = ev_w[ev_w["date"] == asof]
    slot = eq / pf.SLOTS
    for _, e in pend.iterrows():
        sym = LR._sym_for(e, sym_mat)
        name = _carrier_name(sym)
        limit = None
        if prices and sym in prices:
            px = prices[sym]
            if asof in px.index and np.isfinite(px.at[asof, "close"]):
                limit = round(float(px.at[asof, "close"]) * ENTRY_CAP, 3)
        rec = {"no": len(out) + 1, "action": "BUY",
               "what": f"{sym} {name}（{e['industry']}）",
               "est_amount": round(slot, 0),
               "why": f"腿 1 信号 {pd.Timestamp(e['date']).date()} 段首 · slot=权益/6 · 资金不足先卖债券",
               "when": "下一交易日 09:30–14:50 限价" if limit else "下一交易日收盘"}
        if limit:
            rec["limit_price"] = limit
        out.append(rec)

    # ③ 债券对齐：债券市值 = 权益 − 腿 1 持仓（含本指令成交后）；差额低于 BOND_ALIGN_MIN 不动
    leg1_after = sum(p["value"] for p in r["open_positions"]) + slot * len(pend) \
        - sum(p["value"] for p in exits)
    leg1_after = max(leg1_after, 0.0)
    bond_target = max(eq - leg1_after, 0.0)
    delta = bond_target - float(r["defensive_value"])
    if abs(delta) >= BOND_ALIGN_MIN:
        out.append({"no": len(out) + 1, "action": "BUY" if delta > 0 else "SELL",
                    "what": DEF_NAME,
                    "est_amount": round(abs(delta), 0),
                    "why": "债券 B&H：收盘后保持 债券市值 = 权益 − 腿 1 持仓",
                    "when": "腿 1 成交之后"})
    elif abs(delta) >= 1.0:
        out.append({"no": len(out) + 1, "action": "HOLD",
                    "what": DEF_NAME,
                    "est_amount": round(abs(delta), 0),
                    "why": f"差额低于 {BOND_ALIGN_MIN:,.0f} 元对齐阈值，暂不动（纸面与实盘由此最多差 0.5% 权益）",
                    "when": "累计到阈值再对齐"})
    return out


def _carrier_name(sym: str | None) -> str:
    if not sym:
        return "（载体缺失）"
    try:
        inst = pd.read_parquet(ROOT / "data/instruments_etf/instruments_etf.parquet")
        row = inst[inst.symbol == sym]
        return str(row["name"].iloc[0]) if len(row) else ""
    except Exception:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", type=float, default=600_000.0, help="初始资金（纸面账户，默认 60 万；改它等于改账户规模）")
    ap.add_argument("--adoption", type=str, default=ADOPTION)
    args = ap.parse_args()

    d, mats, sessions_all, ev, matrix_end = load_world()
    adoption = pd.Timestamp(args.adoption)
    start = max(adoption, sessions_all[0])
    start = min([t for t in sessions_all if t >= start])
    sessions = [t for t in sessions_all if t >= start]
    sym_mat = mats["symbol"]
    ev_w = ev[ev["date"] >= start].copy()

    # 债券资产与信号：tgt 首日=0（启用日收盘不起债券仓——不可交易过去），其余恒 1
    tr_level, bond_meta = LB.bond_total_return()
    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    sess_idx = pd.DatetimeIndex(sessions)
    bond_r = tr_level.reindex(cal).ffill().reindex(sess_idx).ffill().pct_change()
    tgt = pd.Series(1.0, index=sess_idx)
    if len(tgt):
        tgt.iloc[0] = 0.0
    cash_r = LR.cash_series(sess_idx)

    prices = X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()} | {"511010.SH"})
    r = LR.replay_joint(ev_w, sessions, prices, sym_mat,
                        div_r=bond_r, div_tgt=tgt, cash_r=cash_r,
                        use_div=True, start=start, allow_open=True, init=args.init,
                        entry_mode="open_limit", entry_cap=ENTRY_CAP)

    instructions = derive_instructions(r, ev_w, sessions, sym_mat, prices)
    eq = float(r["equity"].iloc[-1])
    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "policy": "JB0：腿 1 六格（固定 20 日、6bp/边、t+1 开盘限价 +3% 封顶；回测按收盘保守口径）+ 债券 B&H 吃剩余现金",
            "engine": "etf_watch_leg2_rule.replay_joint（与回测同一引擎，allow_open=True，entry_mode=open_limit）",
            "adoption": str(adoption.date()), "init": args.init,
            "defensive": {"symbol": "511010.SH", "name": DEF_NAME, **bond_meta},
            "rules": {
                "entry": "腿 1 信号段首日收盘已知 → 下一交易日开盘挂限价 = 信号收盘×1.03，按当时权益/6 进场；未成交则放弃",
                "exit": "进场后第 20 个交易日收盘卖出",
                "bond": "每日收盘后：债券市值 = 权益 − 腿 1 持仓（差额为正买、为负卖）",
                "crowding": "腿 1 进场缺钱先卖债券（整格补足），不挤掉任何腿 1 信号",
                "independence": "执行层与 V1 判定层独立；账户按事件进场，V1 只记录判据读数",
            },
            "entry": {
                "mode": "next_open_limit",
                "cap_pct": 3.0,
                "valid_until": "14:50",
                "note": f"限价 = 信号收盘 ×{ENTRY_CAP}；开盘低于限价按开盘成交，未成交则放弃（不追已修复出区间的事件）。回测仍按 t+1 收盘（保守口径），执行叠加读数见 _probe_entry_timing.json。",
            },
        },
        "asof": str(sessions[-1].date()),
        "state": {
            "equity": round(eq, 0),
            "cash": round(float(r["cash"]), 0),
            "defensive": {"value": round(float(r["defensive_value"]), 0),
                          "weight": round(float(r["defensive_value"]) / eq, 3) if eq else 0},
            "leg1": {"value": round(float(r["leg1_value"]), 0),
                     "positions": r["open_positions"]},
            "trades_closed": len(r["trades"]),
        },
        "instructions": instructions,
        "equity_tail": [[str(t.date()), round(float(v), 0)] for t, v in r["equity"].tail(5).items()],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}  asof={out['asof']}")
    print(f"权益 {eq:,.0f} = 现金 {out['state']['cash']:,.0f} + 债券 {out['state']['defensive']['value']:,.0f}"
          f" + 腿1 {out['state']['leg1']['value']:,.0f}")
    if instructions:
        print("下一交易日指令：")
        for i in instructions:
            lim = f"  限价 {i['limit_price']}" if i.get("limit_price") else ""
            print(f"  {i['no']}. {i['action']:4s} {i['what']}  ≈{i['est_amount']:,.0f}{lim}  {i['why']}  [{i['when']}]")
    else:
        print("下一交易日无指令。")

    # 现金腿（短融打底 + 国债锚）与账户快照同一次运行产出 —— 分两条命令必然有一条被忘掉。
    # 计算只在 etf_watch_cash_leg.plan 里发生一次；这里只传权益与腿1，不重复取整。
    try:
        cl = CL.build(equity=eq, leg1=float(r["leg1_value"]), write=True)
        print(f"现金腿：国债 {cl['bond_amount']:,.0f} / 短融 {cl['short_amount']:,.0f}"
              f" / 留现金 {cl['residual']:,.0f}（目标 {cl['bond_w']:.0%}，久期 {cl['duration_years']} 年）")
    except Exception as exc:  # noqa: BLE001 —— 现金腿失败不能吞掉账户快照，但必须喊出来
        print(f"[warn] 现金腿计划未生成：{exc}")


if __name__ == "__main__":
    main()
