"""v12：格内序列落盘之后，§6.1 的 `146` 从「外推」变成「可重算」。

v6 起 §6.1 的第三列就挂着「开方外推，不是实测」的注。四档全样本在 v9 用 250
天滚窗量过（586.9 / 214.7 / 257.9 / 403.0），只有 `low×格内` 那一行没有序列，
于是 146 既不能确认也不能否证——它只能被引用。这一组测试钉四件事：

1. 落盘的那条序列**就是**封签判定用的那条：`|all` 逐日复现均值缓存（0.01bp），
   `@326` 复现 §6.2 的 B=40 列（509.2 / 210.7 / 273.5 / 326.4）。不先证明
   这一点，后面的重算只是另一套算法在自言自语。
2. `146` 是 √(325/250) 的外推，不是 250 天上的读数：窗中位 122.7。
3. 改正必须在**同一窗内**做（打印值 ÷ 该窗的 cohort 比），不能拿两个极值相乘：
   逐窗改正后 128.9–141.8，最大值仍在 200 以内。§4 的「low 是唯一进得了门的」
   因此不依赖 146 这个数。
4. 这条通路已经被闸门管住：产物在 `ARTIFACTS` 里、日期扫得出来、默认截断。

第 3 条是这一组唯一会**推翻结论**的测试：若某个窗改正后越过 200，§6.1 那句
「250 天只能给 low 一个可判定的结论」就要改写，而不是换个说法。
"""
from __future__ import annotations

import json
import importlib.util
import statistics
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERIES = REPO / "data" / ".probe_neutral_per_day.json"
MEAN_CACHE = REPO / "data" / ".probe_per_day_exc_mean.json"
PREREG = REPO / "docs" / "decision-target-prereg.md"
SIGNALS = ("momentum_20", "reversal_20", "low_turnover", "high_turnover")
BASES = ("all", "amt", "ind", "cell", "resid")
WINDOW, STEP, N = 250, 10, 326


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
pcc = _load("probe_cohort_check", "scripts/probe_cohort_check.py")
gate = _load("decision_gate", "scripts/decision_gate.py")


@pytest.fixture(scope="module")
def ser() -> dict[str, dict[str, float]]:
    if not SERIES.exists():
        pytest.skip("还没有落盘：先跑 `probe_neutral.py --basis=mean --nmax=326`")
    return json.loads(SERIES.read_text(encoding="utf-8"))


def _x(ser, key: str) -> list[float]:
    return [ser[key][d] for d in sorted(ser[key])]


def _mde(x: list[float], reps: int = 1000) -> float:
    """与 `evaluate_series` 同一个定义、同一个块长、同一个种子。"""
    return (2.486 * statistics.stdev(
        pnd.block_dist(x, block=pnd.BLOCK, reps=reps, seed=0)))


def _windows(ser, key: str) -> list[list[float]]:
    x = _x(ser, key)
    return [x[s:s + WINDOW] for s in range(0, len(x) - WINDOW + 1, STEP)]


def test_series_is_flat_so_the_gate_can_scan_it(ser) -> None:
    """两层结构不是风格问题：`artifact_dates()` 只向下看一层。

    换成 `{策略: {基准: {日: 值}}}` 这种更自然的三层，`audit` 会把这个产物
    读成 0 天——写得进确认段，却扫不出来。这条测试守的就是那个形状。
    """
    assert len(ser) == len(SIGNALS) * len(BASES) == 20
    assert all("|" in k and k.split("|")[1] in BASES for k in ser)
    days = gate.artifact_dates(SERIES)
    assert len(days) == N, len(days)
    assert days[-1] == gate.EXPLORATION_END
    assert max(days) < gate.CONFIRM_START


def test_neutral_cache_is_audited_and_sealed() -> None:
    """落盘但不在产物清单里，等于给闸门开一扇它不知道的门。

    v11 的「11 文件」是按 10 + `probe_neutral.py` 算的。序列一旦落盘，这个
    算法就少了一件：产物不进 `ARTIFACTS`，`audit` 在越界发生时仍报「干净」，
    而 `verify-seal` 也不覆盖它。清单要对**写盘**闭合，所以 v12 是 12、
    v13a 把 `probe_cs_ic.py` 的缓存补进来后是 13；v13c（§12.12）把确认段
    判定入口 `probe_confirm.py` 并进 SEALED，代码文件 7→8、合计 14。
    13s（§12.15）加生存偏差产物，8/15；13t（§12.16）再把它的生成器
    `probe_survivorship_bound.py` 与数据层 `probe_decile_net.py` 并进来，
    代码文件 8→**10**、合计 **17**。

    数字**故意**写死：清单每变一次都要显式改这里，才不会「顺手补一个」
    把封签射程悄悄扩大或缩小。
    """
    assert gate.NEUTRAL_CACHE in gate.ARTIFACTS
    assert gate.NEUTRAL_CACHE in gate.SEALED
    assert len(gate.ARTIFACTS) == 6
    assert len([p for p in gate.SEALED if str(p).endswith(".py")]) == 10


def test_series_reproduces_the_mean_cache(ser) -> None:
    """`|all` 必须逐日等于封签用的均值缓存——否则这是另一条测量。

    0.01bp 的容差是落盘的 `round(…, 4)` 造成的，不是方法差。
    """
    if not MEAN_CACHE.exists():
        pytest.skip("本机没有均值口径缓存")
    cache = json.loads(MEAN_CACHE.read_text(encoding="utf-8"))
    for name in SIGNALS:
        key = f"{name}|all"
        days = [d for d in sorted(ser[key]) if d in cache[name]]
        assert len(days) == N, (name, len(days))
        worst = max(abs(ser[key][d] - sum(cache[name][d]) / len(cache[name][d]) * 1e4)
                    for d in days)
        assert worst < 0.01, (name, worst)


def test_series_reproduces_the_sixth_section(ser) -> None:
    """`@326` 的四个 MDE 复现 §6.2 的 B=40 列，容 0.3bp。

    588 那组数是外推列，不能拿来比；这里比的是 509.2 / 210.7 / 273.5 / 326.4
    这一列——它是打印值，且已经进了封签文档。
    """
    want = {"momentum_20": 509.2, "reversal_20": 210.7,
            "low_turnover": 273.5, "high_turnover": 326.4}
    for name, target in want.items():
        assert _mde(_x(ser, f"{name}|all")) == pytest.approx(target, abs=0.3), name


def test_146_is_an_extrapolation_not_a_measurement(ser) -> None:
    """250 天上的实测打印值是 122.7，不是 146。

    146 = 128（n=325）× √(325/250)。开方外推假设 `se ∝ n^{-1/2}`，但这台设备
    的长度是从**块数**来的：n=326 有 9 个块，n=250 只有 7 个，se 并不按天数
    缩。所以第三列只能当量级，不能当读数。
    """
    x = _x(ser, "low_turnover|cell")
    full = _mde(x)
    proj = full * (N / WINDOW) ** 0.5
    wins = [_mde(w) for w in _windows(ser, "low_turnover|cell")]
    med = statistics.median(wins)

    assert len(wins) == 8, wins
    assert med == pytest.approx(122.7, abs=2.0), med
    #: 外推比实测中位高 17%，而 146 又比外推的 148.8 低——三者不是同一个数。
    assert proj / med > 1.10, (proj, med)
    assert 146 / med > 1.15, med
    assert max(wins) < 200, wins          # 没有任何一窗自己就越过门
    #: 8 个窗共享 240 天，跨度是敏感性带不是抽样分布——这条只是防止有人
    #: 把 max−min 读成置信区间（它们高度相关，sd 会被低估）。
    assert max(wins) - min(wins) > 30, wins


def test_low_cell_survives_the_cohort_correction(ser) -> None:
    """逐窗改正后仍是 200 以内——这是 §6.1 那句「只有 low 可判定」的验收项。

    改正必须**逐窗**做：`mde_s / ratio_s`，因为打印值与 cohort 比在同一个窗上
    是相关的（第 70 号窗两者同时最高，相乘会把 160.6×1.13 误读成一个界）。
    拿两个极值相乘是这一节最容易犯的错，也正是它会被误判成「刀锋」的原因。
    """
    corrected = []
    for w in _windows(ser, "low_turnover|cell"):
        se, _ = pcc.cohort_se(w)
        #: `mde / (sd_b / se)` 与 `2.486 × se_cohort` 是同一个数：打印值先被
        #: 块重抽的窄 se 压低，再被 cohort 比除回去。写成两步是为了让「改正
        #: 用的是同一窗的两个量」这件事在代码里看得见。
        corrected.append(_mde(w) / (pcc.pnd.evaluate_series(w)["sd_b"] / se))
    assert min(corrected) == pytest.approx(128.9, abs=3), corrected
    assert max(corrected) == pytest.approx(141.8, abs=3), corrected
    assert max(corrected) == pytest.approx(2.486 * max(
        pcc.cohort_se(w)[0] for w in _windows(ser, "low_turnover|cell")), abs=0.1)
    assert max(corrected) < pnd.MDE_GATE, corrected
    assert pnd.MDE_GATE - max(corrected) > 50, corrected


def test_cap_is_a_date_not_a_count() -> None:
    """第 367 个分区落盘后，`cap_calendar` 仍把决策日截在 2026-10-08。

    `--nmax=326` 数的是「算成了的天」：任何一天因 `base.height < 200` 被
    `continue` 跳过，第 327 个算成的天就可能是确认段首日而计数仍等于 326。
    所以日期上界只能由截断给，这条测试把两件事分开断言。
    """
    cal = gate.sessions() + [f"2099-01-{i:02d}" for i in range(1, 31)]
    dd = gate.decision_days(gate.cap_calendar(cal))
    assert len(dd) == N, len(dd)
    assert dd[-1] == gate.EXPLORATION_END
    #: 保留 t+20 所需的价格：截断的是决策日，不是价格日历。
    assert len(gate.cap_calendar(cal)) > len(dd)
    #: 反向的边界也钉一下：未来分区不足 20 天时，截断只会**更早**停，不会更晚。
    #: 这是 `min(len(cal), hi)` 的方向性，不是巧合——它保证这个函数在任何
    #: 数据完整度下都不会把确认段放进来。
    short = [d for d in gate.sessions() if d <= gate.EXPLORATION_END] + ["2099-01-01"]
    assert gate.decision_days(gate.cap_calendar(short))[-1] < gate.EXPLORATION_END


def test_neutral_default_path_is_gated() -> None:
    """截断与拦截必须写在 `main()` 的默认路径上，不能只在文档里。"""
    text = (REPO / "scripts" / "probe_neutral.py").read_text(encoding="utf-8")
    assert "GATE.cap_calendar(cal)" in text
    assert "GATE.require_exploration(GATE.decision_days(cal)" in text
    assert "--include-confirmation" in text
    assert "log_read(" in text


def test_section_6_6_records_the_measured_number() -> None:
    """§6.6 必须写下三个数（外推/实测/改正），只写一个会退回单点引用。"""
    text = PREREG.read_text(encoding="utf-8")
    start = text.find("### 6.6 ")
    assert start >= 0, "预注册里没有 §6.6"
    body = text[start:text.find("\n## ", start)]
    for token in ("148.8", "122.7", "141.8", "12"):
        assert token in body, token
