"""把前端 `boardLimitRate` 绑到后端 `board_limit_pct` 上 —— 同一映射只允许一个产地。

背景：A 股涨跌停幅度在两处各写了一遍。后端 `app/price_limits.py::board_limit_pct`
是产地（回测/指标/接口都在用），前端 `lib/price-limit.ts::boardLimitRate` 是给
分时图窄栏画涨跌停带用的第二份，注释里写着「与后端 price_limits.py 当前口径一致」——
但那只是一句承诺，没有任何东西在它不成立时变红。

调一次板块规则（例如北交所 30% → 20%），页面画的带就与实际规则不符：价格明明已经
贴着涨停，图上看着还差得远。这种偏差不会抛异常、不会被测试抓到、页面也不报错。

本脚本把「当前后端口径」固化成前端可 import 的 JSON，两层把它绑住：
  - `backend/tests/test_price_limit_frontend_parity.py`：后端重算 == JSON（CI 里跑）
  - `frontend/src/lib/price-limit.test.ts`：前端函数 == JSON（本地 vitest 跑）
链路后端 → JSON → 前端，任何一环改了而没同步，都会有一层变红。

口径边界：`boardLimitRate` 只承诺「按 symbol 的板块映射」这一层。带日期与 ST 标记
的那部分（创业板 2020-08-24 前 10%、主板 ST 2026-07-06 前 5%）仍只由后端承担 ——
前端函数没有日期轴，别在这个 JSON 里假装它有。

用法：
  python analysis/_verify_price_limit_fixtures.py            # 只打印
  python analysis/_verify_price_limit_fixtures.py --write     # 写 frontend/src/lib/priceLimitFixtures.json
  python analysis/_verify_price_limit_fixtures.py --check     # 与已有 JSON 逐项比, 不同即退出 1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.price_limits import board_limit_pct  # noqa: E402

DATA = ROOT / "data"
INSTRUMENTS = DATA / "instruments" / "instruments.parquet"
OUT = ROOT / "frontend" / "src" / "lib" / "priceLimitFixtures.json"


def load_symbols() -> list[str]:
    """标的宇宙里的全部代码 (排序去重)。维表缺失就直接说清楚, 不要产出空 JSON。"""
    if not INSTRUMENTS.exists():
        raise SystemExit(f"缺少标的维表 {INSTRUMENTS}；先在数据页同步 instruments")
    df = pl.read_parquet(INSTRUMENTS, columns=["symbol"])
    return sorted({str(s) for s in df["symbol"].to_list() if s})


def prefix_key(symbol: str) -> str:
    """板块分组键: 北交所用 BJ 标记, 其余用前三位数字。"""
    return "BJ" if symbol.endswith(".BJ") else symbol[:3]


def build_cases(symbols: list[str]) -> tuple[dict[str, float], dict[str, dict]]:
    """每个板块取字典序首尾两只 —— 覆盖前缀边界与代码形状差异。

    不逐只落盘 (5892 条 ≈ 180KB): `board_limit_pct` 是纯前缀函数, 每个前缀的
    代表值就能完全决定结果。首尾兼取是为了让 `302`/`689` 这类只有一两只的
    边角板块也进得来。
    """
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(prefix_key(symbol), []).append(symbol)

    cases: dict[str, float] = {}
    summary: dict[str, dict] = {}
    for key in sorted(groups):
        members = sorted(groups[key])
        picks = {members[0], members[-1]}
        for symbol in sorted(picks):
            cases[symbol] = board_limit_pct(symbol)
        summary[key] = {"count": len(members), "rate": board_limit_pct(members[0])}
    return dict(sorted(cases.items())), summary


def build_payload() -> dict:
    symbols = load_symbols()
    cases, groups = build_cases(symbols)
    return {
        "generated_at": date.today().isoformat(),
        "source": "backend/app/price_limits.py::board_limit_pct",
        "universe": len(symbols),
        "groups": groups,
        "note": (
            "boardLimitRate 只承诺按 symbol 的板块映射; 带日期/ST 的口径仍只在后端。"
            "重算: python analysis/_verify_price_limit_fixtures.py --write"
        ),
        "cases": cases,
    }


def compare(payload: dict, old: dict) -> list[str]:
    """逐项比, 不比百分比 —— 这些是离散常数 (0.1/0.2/0.3), 任何差异都是口径变了。"""
    problems: list[str] = []
    new_cases = payload["cases"]
    old_cases = old.get("cases") or {}
    for symbol in sorted(set(new_cases) | set(old_cases)):
        new_v = new_cases.get(symbol)
        old_v = old_cases.get(symbol)
        if new_v is None:
            problems.append(f"消失: {symbol} (原 {old_v})")
        elif old_v is None:
            problems.append(f"新增: {symbol} → {new_v}")
        elif abs(float(new_v) - float(old_v)) > 1e-12:
            problems.append(f"变更: {symbol} {old_v} → {new_v}")
    new_groups = set(payload["groups"])
    old_groups = set(old.get("groups") or {})
    for key in sorted(new_groups - old_groups):
        problems.append(f"新板块前缀: {key} (rate={payload['groups'][key]['rate']}) —— 前端正则可能不认识")
    for key in sorted(old_groups - new_groups):
        problems.append(f"板块前缀消失: {key} (原 rate={(old.get('groups') or {})[key]['rate']})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    payload = build_payload()
    print(f"标的 {payload['universe']} 只 / 板块前缀 {len(payload['groups'])} 组", flush=True)
    for key, info in payload["groups"].items():
        print(f"  {key:>4s}  n={info['count']:<5d} rate={info['rate']}", flush=True)
    print(f"落盘用例 {len(payload['cases'])} 条", flush=True)

    if args.write:
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"写入 {OUT}", flush=True)

    if args.check:
        if not OUT.exists():
            print("没有可比的基准文件；先跑 --write", flush=True)
            return 1
        old = json.loads(OUT.read_text(encoding="utf-8"))
        problems = compare(payload, old)
        if problems:
            print("与基准不一致:\n  " + "\n  ".join(problems), flush=True)
            return 1
        print("与基准逐项一致", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
