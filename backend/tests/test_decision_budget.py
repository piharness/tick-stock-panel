"""§6.1 / §6.3 的 250 天预算：第一次有测试重算它。

§12.3 的「v7 不建立什么」第 4 条是「§6 的数字都对」——封签只证明来源，
不证明算术。§6.2 之后还剩 588 / 249 / 306 / 146 没被任何测试碰过。这一组
补上 249 / 306，并把重算覆盖不到的那两件事单独钉住：

1. **§6.1 的算术**：`MDE@250 = MDE@325 × √(325/250)`，输入取当前缓存；
2. **§6.3 的三张脸**：现样本、开方外推、250 天滚窗实测，逐格重算；
3. **n=250 只有 7 个块**：低于 `MIN_BLOCKS=8`，这是装置的自相矛盾，不是笔误；
4. **长块的「低估」是离散度，不是均值**：§6.2 的 0.67 是单条合成序列的低尾，
   60 条平均后 B=80 与 B=40 只差 3%。

146 那一行仍不在这里：它要 `probe_neutral.py --basis=mean` 的格内序列，
而那个序列没有落盘。测试不假装覆盖它。
"""
from __future__ import annotations

import importlib.util
import json
import math
import random
import statistics
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
PREREG = REPO / "docs" / "decision-target-prereg.md"


def _series(n: int | None = None, basis: str = "mean") -> dict[str, list[float]]:
    """日序列；`n` 截到前 n 天（§6.1 用 325），`basis` 选中心。"""
    name = ".probe_per_day_exc_mean.json" if basis == "mean" \
        else ".probe_per_day_exc.json"
    cache = REPO / "data" / name
    if not cache.exists():
        pytest.skip(f"本机没有 {basis} 口径缓存")
    exc = json.loads(cache.read_text(encoding="utf-8"))
    days = sorted(set.intersection(*(set(v) for v in exc.values())))
    if n:
        days = days[:n]
    return {k: [sum(v[d]) / len(v[d]) * 1e4 for d in days] for k, v in exc.items()}


def _table(text: str, heading: str, ncol: int) -> dict[str, list[str]]:
    """取 heading 之后的第一张表。单元格去掉 `**`（强调不参与比较）。"""
    start = text.find(heading)
    assert start >= 0, f"预注册里没有 {heading}"
    rows: dict[str, list[str]] = {}
    for line in text[start:].splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip().strip("*") for c in line.strip("|").split("|")]
        if len(cells) != ncol or cells[0] in ("策略", "---"):
            continue
        if set(cells[0]) <= {"-", " "}:
            continue
        rows[cells[0]] = cells[1:]
    return rows


def test_section_6_1_arithmetic_recomputes() -> None:
    """249 / 306 / 374 必须等于 `round(MDE@325 × √(325/250))`。

    输入不是文档里的 515 / 218 / 268 / 328，而是**当前缓存**上
    `evaluate_series()` 的读数——否则这条只是在抄自己。取整用 `round`：
    文档是从未取整的 mde 算的（268.5×√(325/575)=201.9→202），从 268 起算
    会差 1。格内那一行没有序列，跳过并单独断言它是唯一没被重算的。
    """
    text = PREREG.read_text(encoding="utf-8")
    rows = _table(text, "### 6.1 ", 5)
    ser = _series(n=325)
    got = {k: pnd.evaluate_series(x)["mde"] for k, x in ser.items()}
    checked = 0
    for name, cells in rows.items():
        if "格内" in name:
            assert cells[2] == "146"
            continue
        key = next(k for k in ser if k.startswith(name.split("（")[0]))
        mde = got[key]
        # 显示列容 1bp：文档在 low 上把 268.52 截成了 268。派生列不容差。
        assert abs(mde - float(cells[0])) < 1.0, (name, mde, cells[0])
        assert int(round(mde * math.sqrt(325 / 250))) == int(cells[2]), \
            (name, cells[2])
        assert int(round(mde * math.sqrt(325 / 575))) == int(cells[1]), \
            (name, cells[1])
        checked += 1
    assert checked == 4, rows


def test_section_6_median_table_recomputes() -> None:
    """§6 那张表（中位数口径，n=325）的 se、MDE、天数三列。

    588 是中位数口径的 momentum，不在 §6.1 的均值列里——v9 之前它一条测试
    都没有。行业×规模那列（403/182/107/299）依赖 `probe_neutral.py` 的格内
    序列，这里跳过；天数按 `325×(MDE/200)²` 反算，容 3%（文档按百/十取整）。
    """
    text = PREREG.read_text(encoding="utf-8")
    rows = _table(text, "## 6. 功效预算", 7)
    ser = _series(n=325, basis="median")
    assert len(rows) == 4, rows
    for name, cells in rows.items():
        key = next(k for k in ser if k.startswith(name))
        r = pnd.evaluate_series(ser[key])
        assert abs(r["sd_b"] - float(cells[1])) < 1.0, (name, r["sd_b"])
        assert abs(r["mde"] - float(cells[2])) < 1.0, (name, r["mde"])
        want = float(cells[5].lstrip("~").replace(",", ""))
        assert abs(325 * (r["mde"] / 200) ** 2 - want) / want < 0.03, name


def test_section_6_3_table_recomputes() -> None:
    """§6.3 的三列 MDE 与 nb 必须与 `budget_row()` 逐格相等。

    这张表是唯一同时报「外推」与「实测」的地方；若只留一列，读者会以为
    250 天的分辨率是一个确定的数。
    """
    rows = _table(PREREG.read_text(encoding="utf-8"), "### 6.3 ", 6)
    ser = _series()
    assert set(rows) == set(ser), (set(rows), set(ser))
    for name, cells in rows.items():
        r = pnd.budget_row(ser[name])
        assert r["mde"] == pytest.approx(float(cells[0]), abs=0.05), name
        assert r["mde_proj"] == pytest.approx(float(cells[1]), abs=0.05), name
        assert r["mde_meas"] == pytest.approx(float(cells[2]), abs=0.05), name
        assert r["nb"] == int(cells[4]), name


def test_confirmation_has_fewer_blocks_than_the_floor() -> None:
    """n=250、B=40 只有 7 个块，低于 §6.2 的 MIN_BLOCKS=8。

    这不是算术细节：§4 说 250 天后出判定，§6.2 说 nb<8 的列不能读。
    两者不能同时成立，而确认时钟现在是 0/250——还有时间决定怎么解。
    """
    nb = -(-pnd.CONFIRM_N // pnd.BLOCK)
    assert nb == 7
    assert nb < pnd.MIN_BLOCKS
    assert pnd.CONFIRM_N / pnd.BLOCK < pnd.MIN_BLOCKS


def _ma(n: int, m: int = 20, seed: int = 0) -> list[float]:
    """20 日重叠 MA 的白噪声。Var(均值) = 1/n 是解析值（f=m=20）。"""
    rng = random.Random(seed)
    e = [rng.gauss(0.0, 1.0) for _ in range(n + m - 1)]
    return [sum(e[i:i + m]) / m for i in range(n)]


def test_block_bias_is_dispersion_not_trend() -> None:
    """长块的问题是**不稳定**，不是系统性偏窄。

    §6.2 用 seed=7 的一条序列得出「B=80 只有真值的 0.67 倍」。把 24 条序列
    平均，B=80 与 B=40 只差几个百分点——那个 0.67 是低尾的一次抽样。
    变的量是跨样本 sd：块越长，se 越飘。所以 `!` 标记的理由要写成
    「不可读」，不是「必然低估」。
    """
    n, reps, draws = 326, 300, 24
    true = 1.0 / math.sqrt(n)
    ratios: dict[int, list[float]] = {40: [], 80: []}
    for seed in range(draws):
        x = _ma(n, seed=seed)
        for b in ratios:
            se = statistics.stdev(pnd.block_dist(x, block=b, reps=reps, seed=0))
            ratios[b].append(se / true)
    m40, m80 = statistics.mean(ratios[40]), statistics.mean(ratios[80])
    assert 0.65 < m40 < 0.85, m40
    assert 0.65 < m80 < 0.90, m80
    assert abs(m80 / m40 - 1) < 0.15, (m40, m80)
    assert statistics.stdev(ratios[80]) > statistics.stdev(ratios[40])


def test_section_6_no_longer_mixes_bases() -> None:
    """§6 的那句「全样本 588 / 218 / 268 / 328」把中位数与均值并排写。

    588 是中位数口径的 momentum；均值口径是 515。v9 之前这四个数看起来
    像同一个口径，读者无法分辨。这条只防这一种回退。
    """
    text = PREREG.read_text(encoding="utf-8")
    # 只看 §6 正文：§12 的变更记录必须能引用那个错串，否则历史不可读。
    sec6 = text[text.find("## 6. 功效预算"):text.find("### 6.1")]
    assert "588 / 218 / 268 / 328" not in sec6
    assert "515 / 218 / 268 / 328" in sec6
