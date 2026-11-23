"""投资手册的活数据体检 —— 页面每天读的那几个产物，是不是**最近一个交易日**的。

为什么需要它：
  手册的「执行」章与「工具 · 单日回放」不再手抄数字，全部来自四个产物
  （扫描 / 账户 / 现金腿 / 回放）。产物由不同脚本在不同时刻写出，于是就出现了
  「扫描停在 11-20、今天已是 11-23」这种页面看不出来的落后 —— 日期控件里
  根本没有今天，读者只会以为控件坏了。

  判定「该到哪一天」只能靠交易日历，不能靠产物之间互相比：
  扫描与指数帧一起停在 11-20 时，`lag_sessions` 仍然是 0（它只回答 ETF 帧 vs 指数帧）。
  2026-11-23 19:19 就发生过这件事：脚本打印「扫描日 = 最近交易日」，而当天是交易日、
  已收盘、数据分区也已存在 —— 假绿比没有检查更糟。

它不做什么：
  - 不重算任何指标（判定口径在 analysis/etf_watch.py）；
  - 不格式化页面文案（那是 frontend/src/pages/structural-edge/live.ts 的唯一职责）。

退出码：
  0  全部覆盖到最近交易日
  1  有产物落后 / 不同日（需要补跑，命令见输出末尾）
  2  关键产物缺失（页面会显示「未就绪」）

用法：
  python analysis/handbook_audit.py
  python analysis/handbook_audit.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import trading_calendar as CAL  # noqa: E402

DATA = ROOT / "data/etf_watch"

#: 收盘后多久算「今天的产物应该已经在了」。15:00 收盘，留 10 分钟给数据落盘。
CLOSE_READY = dtime(15, 10)

OK, WARN, FAIL = "OK", "WARN", "FAIL"
CHAIN = ("python analysis/etf_watch.py && python analysis/etf_watch_account.py "
         "&& python analysis/etf_watch_replay.py --lookback 120")


@dataclass
class Row:
    name: str
    status: str
    detail: str


def _load(name: str) -> dict | None:
    path = DATA / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def expected_session(now: datetime) -> tuple[date | None, str]:
    """按交易日历回答「现在页面上应该能看到哪一天」。

    交易日 15:10 之后 = 今天；之前（或休市日）= 上一交易日。
    超出常量覆盖区间返回 None —— 判不了就说判不了，不用「周末以外都是交易日」兜底。
    """
    today = now.date()
    _lo, hi = CAL.COVERED
    if today > hi:
        return None, f"超出交易日历覆盖区间（止于 {hi}），判不了；先补交易日历"
    if CAL.is_market_day(today):
        if now.time() >= CLOSE_READY:
            return today, f"今天（{today}）已收盘"
        return CAL.prev_market_day(today), f"今天（{today}）未到收盘，应覆盖上一交易日"
    return CAL.prev_market_day(today), f"今天（{today}）休市"


def missing_sessions(asof: date, expected: date) -> list[date]:
    out: list[date] = []
    d = asof + timedelta(days=1)
    while d <= expected:
        if CAL.is_market_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _behind(name: str, asof: str | None, expected: date, generated: str | None) -> Row:
    if not asof:
        return Row(name, FAIL, "产物缺失或没有数据日：页面会显示「未就绪」")
    try:
        got = date.fromisoformat(asof[:10])
    except ValueError:
        return Row(name, FAIL, f"数据日无法解析：{asof!r}")
    when = f"，生成 {generated}" if generated else ""
    # 「应覆盖」是下界：只要不早于它就算过。早于它才是落后。
    # （盘中重跑过一次、产物日期比「上一交易日」还新，不是错误，不该每天上午刷一条 WARN。）
    if got >= expected:
        extra = f"（应覆盖 ≥ {expected}）" if got > expected else ""
        return Row(name, OK, f"数据日 {got}{when}{extra}")
    gap = missing_sessions(got, expected)
    head = "、".join(d.isoformat() for d in gap[:5]) + ("…" if len(gap) > 5 else "")
    return Row(name, FAIL, f"数据日 {got}{when}，应到 {expected}：缺 {len(gap)} 个交易日（{head}）")


def audit(now: datetime | None = None) -> tuple[list[Row], dict]:
    now = now or datetime.now()
    exp, why = expected_session(now)

    latest = _load("latest.json")
    if latest is None:
        return [Row("扫描快照", FAIL, "latest.json 缺失：先跑 analysis/etf_watch.py")], {}

    account = _load("account.json")
    cash_leg = _load("cash_leg.json")
    replay = _load("replay.json")

    rows: list[Row] = [Row("交易日历", OK if exp else FAIL, why)]
    if exp is None:
        return rows, {}

    scan_asof = (latest.get("meta") or {}).get("asof")
    rows.append(_behind("扫描快照", scan_asof, exp, (latest.get("meta") or {}).get("generated_at")))

    # 回放是「工具 · 交易日期」控件的唯一数据源：它落后一天，控件里就没有今天。
    rows.append(_behind("单日回放", (replay or {}).get("meta", {}).get("end"), exp,
                        (replay or {}).get("meta", {}).get("generated_at")))
    rows.append(_behind("账户快照", (account or {}).get("asof"), exp,
                        (account or {}).get("meta", {}).get("generated_at")))
    rows.append(_behind("现金腿", (cash_leg or {}).get("meta", {}).get("asof"), exp,
                        (cash_leg or {}).get("meta", {}).get("generated_at")))

    # 帧间缺口是另一件事：它回答「ETF 帧 vs 指数帧差几天」，与「今天跑没跑」正交。
    lag = (latest.get("data") or {}).get("lag_sessions")
    if isinstance(lag, int):
        rows.append(Row("ETF ↔ 指数", OK if lag == 0 else FAIL,
                        "两帧同日" if lag == 0 else f"ETF 帧落后指数 {lag} 个交易日：先补数据"))

    # 账户与现金腿由同一次运行产出，必须同日。
    a = (account or {}).get("asof")
    c = (cash_leg or {}).get("meta", {}).get("asof")
    if a and c and a != c:
        rows.append(Row("账户 ↔ 现金腿", FAIL, f"{a} ≠ {c}：同一次运行的产物不该不同日，重跑 etf_watch_account.py"))

    v1 = (latest.get("ledger") or {}).get("v1", {})
    required, settled = v1.get("required"), v1.get("settled_clusters")
    if required is not None and settled is not None:
        expect = f"{settled}/{required}"
        progress = str(v1.get("progress", "")).replace(" ", "")
        if progress and progress != expect:
            rows.append(Row("V1 账本", WARN, f"progress {v1.get('progress')} 与 settled/required {expect} 不一致"))
        else:
            rows.append(Row("V1 账本", OK, f"{expect} 簇（已结算）"))

    facts = {
        "now": now.isoformat(timespec="minutes"),
        "expected_session": exp.isoformat(),
        # 页面据此决定要不要「无感更新」：收盘后/休市可自动，盘中只提示不自动跑。
        "phase": ("closed" if now.time() >= CLOSE_READY else "pre_close")
        if CAL.is_market_day(now.date()) else "holiday",
        "missing": ([d.isoformat() for d in missing_sessions(date.fromisoformat(scan_asof), exp)]
                    if scan_asof else []),
        "asof": scan_asof,
        "replay_end": (replay or {}).get("meta", {}).get("end"),
        "account_asof": a,
        "cash_leg_asof": c,
        "counts": latest.get("counts", {}),
        "v1": {"progress": v1.get("progress"), "settled": settled, "required": required, "state": v1.get("state")},
    }
    return rows, facts


def report(rows: list[Row], facts: dict) -> int:
    """打印体检结论并返回退出码。refresh 直接复用这里，不重复一套判定。"""
    failed = any(r.status == FAIL for r in rows)
    warned = any(r.status == WARN for r in rows)
    print(f"=== 投资手册 · 活数据体检 {datetime.now():%Y-%m-%d %H:%M} ===")
    for row in rows:
        print(f"[{row.status:<4}] {row.name:<10} {row.detail}")
    print("---")
    if failed:
        print("结论：今天的盘后链路没跑完或被跳过。页面上会缺今天，日期控件也不会出现今天。")
        print(f"补跑：{CHAIN}")
    elif warned:
        print("结论：可以读，但标黄的那几行不是最近交易日的数据。")
    else:
        print("结论：扫描 / 回放 / 账户 / 现金腿都覆盖到最近交易日。")
    return 2 if not facts else 1 if (failed or warned) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="投资手册活数据体检（只读）")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = ap.parse_args()

    rows, facts = audit()
    if args.json:
        failed = any(r.status == FAIL for r in rows)
        warned = any(r.status == WARN for r in rows)
        print(json.dumps({"rows": [asdict(r) for r in rows], "facts": facts,
                          "status": "FAIL" if failed else "WARN" if warned else "OK"},
                         ensure_ascii=False, indent=2))
        return 2 if not facts else 1 if (failed or warned) else 0
    return report(rows, facts)


if __name__ == "__main__":
    sys.exit(main())
