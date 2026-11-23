"""退市红线 —— 三条硬约束的距离必须按原始价、年报口径计算。

实测动机（600340.SH / *ST华幸，2026-11-17）：
  收盘 0.92 元，前一日 1.02 元，跌破面值第 1 天；
  2025 年报净资产 -48.31 亿元，2026Q3 已到 -127.80 亿元。
页面上「关键价位」和「AI 四维分析」都在讲支撑位与形态，没有一个数字说明
这只票还有多少天会被面值退市摘牌 —— 本文件守的正是这几个数字。

四个必须成立的断言：
  1. 面值线用 raw_close 判，不是 close：前复权因子会把 0.92 显示成 1.84，
     用 close 判会得出「距面值还有 84% 余量」这种致命的安全假象；
  2. 连续天数从最新一根往前数，中间断一天就归零；
  3. 财务类只认年报（period_end 落在 12-31），三季报净资产不参与判定；
  4. 算不出来时是 unknown，不是 safe —— 缺数据不能长得像安全。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import delisting_guard as dg


class FakeRepo:
    """只实现面板用到的那四个入口，避免为一次纯逻辑测试铺整套 parquet。"""

    def __init__(self, daily: pl.DataFrame, inst: pl.DataFrame, data_dir: Path):
        self._daily = daily
        self._inst = inst
        self.store = SimpleNamespace(data_dir=data_dir)

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return self._daily

    def get_instruments_asset(self, asset_type):
        return self._inst


def _daily(closes: list[float], raws: list[float] | None = None) -> pl.DataFrame:
    start = date(2026, 10, 1)
    n = len(closes)
    return pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "close": closes,
        "raw_close": raws if raws is not None else closes,
        "volume": [1.0] * n,
    })


def _inst(name: str, shares: float = 3_893_355_062.0, symbol: str = "600340.SH") -> pl.DataFrame:
    return pl.DataFrame({"symbol": [symbol], "name": [name], "total_shares": [shares]})


def _annual(period_end: str, equity: float | None = None, revenue: float | None = None,
            net_income: float | None = None) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600340.SH"] * 1,
        "period_end": [period_end],
        "total_equity": [equity],
        "revenue": [revenue],
        "net_income_attributable": [net_income],
    })


@pytest.fixture()
def patch_fin(monkeypatch):
    def _apply(tables: dict[str, pl.DataFrame]):
        monkeypatch.setattr(dg, "get_financial_df", lambda _dir, table: tables.get(table, pl.DataFrame()))
    return _apply


def test_par_gate_uses_raw_close_not_adjusted_close(tmp_path: Path, patch_fin):
    """前复权陷阱：close=1.84（因子 0.5）而 raw_close=0.92，必须判「已跌破」。"""
    prices = _daily([1.84], raws=[0.92])
    repo = FakeRepo(prices, _inst("*ST华幸"), tmp_path)
    patch_fin({"balance_sheet": pl.DataFrame(), "income": pl.DataFrame()})

    guard = dg.build_risk_guard(repo, "600340.SH")
    par = next(g for g in guard.gates if g.id == "par")

    assert par.value == pytest.approx(0.92)
    assert par.status == "counting", "用 close 判会得出 safe，这是必须被抓住的错误"
    assert par.distance_pct == pytest.approx(-8.0)


def test_streak_counts_back_from_latest_and_resets(tmp_path: Path, patch_fin):
    """连续 3 天低于 1 元 → streak=3；第 4 天回到 1.02 → streak=0。"""
    patch_fin({"balance_sheet": pl.DataFrame(), "income": pl.DataFrame()})

    below = _daily([1.05, 0.98, 0.96, 0.92])
    g1 = dg.build_risk_guard(FakeRepo(below, _inst("*ST华幸"), tmp_path), "600340.SH")
    par1 = next(g for g in g1.gates if g.id == "par")
    assert par1.streak == 3 and par1.status == "counting"

    recovered = _daily([0.98, 0.96, 0.92, 1.02])
    g2 = dg.build_risk_guard(FakeRepo(recovered, _inst("*ST华幸"), tmp_path), "600340.SH")
    par2 = next(g for g in g2.gates if g.id == "par")
    assert par2.streak == 0 and par2.status == "near"


def test_twenty_days_below_par_is_triggered(tmp_path: Path, patch_fin):
    patch_fin({"balance_sheet": pl.DataFrame(), "income": pl.DataFrame()})
    prices = _daily([0.99] * 20)
    guard = dg.build_risk_guard(FakeRepo(prices, _inst("*ST华幸"), tmp_path), "600340.SH")
    par = next(g for g in guard.gates if g.id == "par")
    assert par.streak == 20
    assert par.status == "triggered"
    assert "终止上市" in par.detail


def test_financial_gate_reads_annual_not_quarter(tmp_path: Path, patch_fin):
    """2026Q3 净资产为负但还没出年报 → unknown，不能提前判退市。"""
    quarter = pl.DataFrame({
        "symbol": ["600340.SH"],
        "period_end": ["2026-09-30"],
        "total_equity": [-12_779_531_680.77],
    })
    patch_fin({"balance_sheet": quarter, "income": pl.DataFrame()})

    guard = dg.build_risk_guard(FakeRepo(_daily([0.92]), _inst("*ST华幸"), tmp_path), "600340.SH")
    eq = next(g for g in guard.gates if g.id == "equity")
    assert eq.status == "unknown"
    assert "年报" in eq.detail


def test_negative_equity_is_triggered_with_consecutive_years(tmp_path: Path, patch_fin):
    bs = pl.concat([
        _annual("2024-12-31", equity=-1.2e9),
        _annual("2025-12-31", equity=-4.831e9),
    ])
    patch_fin({"balance_sheet": bs, "income": pl.DataFrame()})

    guard = dg.build_risk_guard(FakeRepo(_daily([0.92]), _inst("*ST华幸"), tmp_path), "600340.SH")
    eq = next(g for g in guard.gates if g.id == "equity")
    assert eq.status == "triggered"
    assert eq.streak == 2
    assert "再负一年即终止上市" in eq.detail


def test_revenue_gate_needs_both_conditions(tmp_path: Path, patch_fin):
    """净利为负但营收 31 亿 > 3 亿门槛 → near，不是 triggered。"""
    inc = _annual("2025-12-31", revenue=8.603e9, net_income=-2.2859e10)
    patch_fin({"balance_sheet": pl.DataFrame(), "income": inc})

    guard = dg.build_risk_guard(FakeRepo(_daily([0.92]), _inst("*ST华幸"), tmp_path), "600340.SH")
    rev = next(g for g in guard.gates if g.id == "revenue")
    assert rev.status == "near"
    assert "高于 3 亿门槛" in rev.detail


def test_chinext_uses_three_yi_mcap_line(tmp_path: Path, patch_fin):
    patch_fin({"balance_sheet": pl.DataFrame(), "income": pl.DataFrame()})
    # 300 开头：市值红线 3 亿，不是主板 5 亿
    repo = FakeRepo(_daily([10.0]), _inst("*ST测试", shares=1e6, symbol="300001.SZ"), tmp_path)
    guard = dg.build_risk_guard(repo, "300001.SZ")
    mcap = next(g for g in guard.gates if g.id == "mcap")
    assert guard.board == "创业板"
    assert mcap.threshold == 3e8


def test_missing_daily_is_unknown_not_safe(tmp_path: Path, patch_fin):
    """什么都不算出来时，结论不能说「未触及」—— unknown 不能长得像 safe。

    实测反例（000587.SZ *ST金洲，本地无日线、无财务）：旧逻辑下四条红线全是
    unknown，结论却输出「当前未触及交易类/财务类硬红线」，正好把「这只票
    已经退市、本地查不到」说成了「它是安全的」。
    """
    patch_fin({"balance_sheet": pl.DataFrame(), "income": pl.DataFrame()})
    guard = dg.build_risk_guard(FakeRepo(pl.DataFrame(), _inst("*ST华幸"), tmp_path), "600340.SH")
    assert all(g.status == "unknown" for g in guard.gates if g.id in ("par", "mcap"))
    assert any("已退市" in c for c in guard.caveats)
    assert guard.counts.get("verdict") == "本地数据不足，无法判定"


def test_caveat_always_states_no_probability(tmp_path: Path, patch_fin):
    patch_fin({"balance_sheet": pl.DataFrame(), "income": pl.DataFrame()})
    guard = dg.build_risk_guard(FakeRepo(_daily([0.92]), _inst("*ST华幸"), tmp_path), "600340.SH")
    assert any("不给摘帽/退市概率" in c for c in guard.caveats)


def test_risk_guard_endpoint_is_a_passthrough(monkeypatch, tmp_path: Path):
    """端点只做转发：空 symbol 400，正常 symbol 返回可 JSON 序列化的 dict。

    直接调函数而不是走 TestClient —— 这个端点的全部逻辑在 service 里，
    这里只要证明「参数校验没漏、返回值能序列化」。
    """
    from fastapi import HTTPException
    from types import SimpleNamespace

    from app.api import stock_analysis as api

    repo = FakeRepo(_daily([0.92]), _inst("*ST华幸"), tmp_path)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    monkeypatch.setattr(dg, "get_financial_df", lambda _d, _t: pl.DataFrame())

    out = api.get_risk_guard(req, symbol="600340.SH")
    assert out["symbol"] == "600340.SH"
    assert out["gates"][0]["status"] == "counting"
    assert isinstance(out["caveats"], list)

    with pytest.raises(HTTPException) as exc:
        api.get_risk_guard(req, symbol="")
    assert exc.value.status_code == 400


# ── 全市场倒计时 build_market_countdown ──────────────────────────

def _countdown_repo(tmp_path: Path, daily: pl.DataFrame, inst: pl.DataFrame) -> "FakeRepo":
    """构造能喂给 build_market_countdown 的 repo + 最小 enriched parquet 目录。"""
    enriched = tmp_path / "kline_daily_enriched"
    for d in daily["date"].unique().sort():
        part = enriched / f"date={d}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        sub = daily.filter(pl.col("date") == d)
        sub.write_parquet(part)
    return FakeRepo(daily, inst, tmp_path)


def test_market_countdown_par_streak(tmp_path: Path):
    """一只票连续 3 日破面值 → par_streak=3，未破线的票不进结果。"""
    from datetime import timedelta

    base = date(2026, 11, 1)
    daily = pl.DataFrame({
        "symbol": ["600340.SH"] * 5 + ["000001.SZ"] * 5,
        "date": [base + timedelta(days=i) for i in range(5)] * 2,
        "raw_close": [1.05, 0.98, 0.96, 0.92, 0.90] + [10.0, 10.1, 10.2, 10.3, 10.4],
        "close": [1.05, 0.98, 0.96, 0.92, 0.90] + [10.0, 10.1, 10.2, 10.3, 10.4],
    })
    inst = pl.DataFrame({
        "symbol": ["600340.SH", "000001.SZ"],
        "name": ["*ST华幸", "平安银行"],
        # 单位是**股**，与生产维表 `instruments.parquet` 一致（平安银行 1.94e10）。
        # 这里曾写 `... / 1e4`（万股），恰好配套了 `build_market_countdown`
        # 里那个多乘的 1e4 ⇒ 测试绿、生产错 1 万倍。
        "total_shares": [3_893_355_062.0, 19_405_918_198.0],
    })
    repo = _countdown_repo(tmp_path, daily, inst)

    out = dg.build_market_countdown(repo, date(2026, 11, 17))
    # 只有 600340 破面值（连续 4 日 <1 元），000001 不进结果
    assert len(out["countdown"]) == 1
    row = out["countdown"][0]
    assert row["symbol"] == "600340.SH"
    assert row["par_streak"] == 4
    assert row["streak_limit"] == 20


def test_market_countdown_chinext_mcap_threshold(tmp_path: Path):
    """科创板（688）市值红线是 3 亿，不是主板 5 亿。"""
    from datetime import timedelta

    base = date(2026, 11, 1)
    # 688 股：价格 < 20 元，股本 1500 万股 → 市值 < 3 亿，在科创板 3 亿线以下
    daily = pl.DataFrame({
        "symbol": ["688121.SH"] * 3,
        "date": [base + timedelta(days=i) for i in range(3)],
        "raw_close": [19.0, 18.5, 18.0],
        "close": [19.0, 18.5, 18.0],
    })
    inst = pl.DataFrame({
        "symbol": ["688121.SH"], "name": ["测试"],
        "total_shares": [15_000_000.0],  # 1500 万股，单位=股
    })
    repo = _countdown_repo(tmp_path, daily, inst)

    out = dg.build_market_countdown(repo, date(2026, 11, 17))
    row = out["countdown"][0]
    assert row["board"] == "科创板"
    # 市值 = 18 元 × 1500 万股 = 2.7 亿 < 3 亿 → 连续 3 日破市值线
    assert row["mcap_streak"] == 3
    # 直接锁量级：多乘 1e4 会得到 2.7e12（2.7 万亿），mcap_streak 归零、
    # 市值退市整条腿静默失效。只断言 streak 的话，阈值刚好卡在边界时仍可能
    # 「碰巧通过」；量级断言没有这种运气。
    assert row["mcap"] == pytest.approx(18.0 * 15_000_000, rel=1e-6)


def _naive_countdown(daily: pl.DataFrame, inst: pl.DataFrame) -> dict:
    """朴素逐股参考实现 —— 这段规则的**规范**，不参与生产。

    与 `build_market_countdown` 现在的表达式实现并存，用来对拍：
    从末尾往前数，遇到「未破线」或「空值」就停，停止位置距末尾的天数即 streak；
    整段都破线时 streak = 组内行数；股本缺失或 ≤0 时市值无法判定（streak=0）。
    """
    shares_map = {
        r["symbol"]: float(r["total_shares"])
        for r in inst.to_dicts()
        if r.get("total_shares") is not None
    }
    out: dict = {}
    for key, grp in daily.sort(["symbol", "date"]).group_by("symbol", maintain_order=True):
        sym = str(key[0]) if isinstance(key, (tuple, list)) else str(key)
        board = dg.classify_board(sym.split(".")[0])
        limit = dg._MCAP_MAIN if board == "主板" else dg._MCAP_OTHER
        shares = shares_map.get(sym)
        prices = grp.sort("date")["raw_close"].to_list()

        par_streak = 0
        for v in reversed(prices):
            if v is None:
                break
            if float(v) < dg._PAR_VALUE:
                par_streak += 1
            else:
                break

        mcap_streak = 0
        last_mcap = None
        if shares and shares > 0:
            if prices and prices[-1] is not None:
                last_mcap = float(prices[-1]) * shares
            for v in reversed(prices):
                if v is None:
                    break
                if float(v) * shares < limit:
                    mcap_streak += 1
                else:
                    break

        if par_streak > 0 or mcap_streak > 0:
            out[sym] = (
                round(float(prices[-1]), 4) if prices and prices[-1] is not None else None,
                round(last_mcap, 2) if last_mcap is not None else None,
                par_streak,
                mcap_streak,
                board,
            )
    return out


def test_market_countdown_vectorized_matches_naive_reference(tmp_path: Path):
    """逐票等价：表达式实现必须与朴素逐股实现给出**同一份**名单。

    这是把 4.45s 的 Python 循环换成表达式（0.03s）的正确性判据。参考实现
    留在测试里，因为它是这段规则的规范 —— 生产只能有一个实现，但必须有人
    记得它原本是什么意思。

    覆盖四个最容易写错的边界：
      · 尾部空值（停牌）：停而**不计入** streak，不能把停牌前的破线也数进来；
      · 股本为 0（退市股维表常见）：市值「无法判定」，不能算成「市值 0 元破线」；
      · 整段破线：streak 等于窗口行数，不能被截成 0；
      · 深度 0（从未破线）：不得进名单。
    """
    import random

    random.seed(20261120)
    base = date(2026, 11, 1)
    n_days = 25
    syms = [f"6000{i:02d}.SH" for i in range(40)] + [f"3000{i:02d}.SZ" for i in range(20)]

    rows = []
    for s in syms:
        depth = random.choice([0, 0, 1, 3, 8, 19, 25])
        tail_null = random.random() < 0.15
        base_px = random.choice([0.6, 0.95, 1.05, 3.0, 12.0])
        for ti in range(n_days):
            from_end = n_days - 1 - ti
            px: float | None = base_px if from_end >= depth else 0.85
            if tail_null and from_end < 2:
                px = None
            rows.append({
                "symbol": s, "date": base + timedelta(days=ti),
                "raw_close": px, "close": px,
            })
    daily = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    inst = pl.DataFrame({
        "symbol": syms,
        "name": [f"票{i}" for i in range(len(syms))],
        "total_shares": [random.choice([0.0, 8e7, 3.5e9, 1.2e10]) for _ in syms],
    })
    repo = _countdown_repo(tmp_path, daily, inst)

    out = dg.build_market_countdown(repo, date(2026, 11, 25))
    got = {
        r["symbol"]: (r["close"], r["mcap"], r["par_streak"], r["mcap_streak"], r["board"])
        for r in out["countdown"]
    }
    assert got == _naive_countdown(daily, inst)
