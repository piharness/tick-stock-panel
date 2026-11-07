"""证据产物的自洽性：口径必须活在产物里，不能只活在上一轮的散文里。

三条守的是同一类事故——**数字对、标签错**，于是下游照标签读，读出一个不存在的量：
  * 池「均值 − 中位」这条恒等式旁边写着「日度中位的中位」。两个时间聚合一混，
    差 57bp，而恒等式本身在产物里是闭合的，所以没人发现。
  * `layer_w99` 被印成「双侧缩尾」，实现只 `clip(upper_bound=...)`——单边上尾。
  * `since` 子样本只存起点，于是「2025-01 起」被读成近两年，实际 1.5 年。

这些都不是估计量的问题，是**字段没把口径说出来**。所以测试也只钉口径：
恒等式必须闭合、两个统计量必须是两个字段、每个子样本必须自带终点与天数。

产物不在时跳过——新克隆没有 `data/.scratch/*`，那不是缺陷。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / "data" / ".scratch"


def _gate():
    """闸门模块：只看 `SEALED` / `ARTIFACTS` 两个清单，不执行任何检查。"""
    spec = importlib.util.spec_from_file_location(
        "decision_gate", REPO / "scripts" / "decision_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _probe():
    spec = importlib.util.spec_from_file_location(
        "probe_survivorship_bound", REPO / "scripts" / "probe_survivorship_bound.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load(name: str) -> dict:
    p = SCRATCH / name
    if not p.exists():
        pytest.skip(f"{name} 不在（新克隆 / CI），本地重跑探针后才有")
    return json.loads(p.read_text(encoding="utf-8"))


def _blocks(d: dict):
    """取出所有带 `pool_mean_minus_median_bp` 的块（4 个口径 × 2 个 H）。"""
    for key, v in d.items():
        if isinstance(v, dict) and "pool_mean_minus_median_bp" in v:
            yield key, v


def test_pool_identity_closes_inside_the_artifact():
    """池均值 − 池中位 必须等于产物里那一个差，且「中位的中位」是另一个字段。

    这条是口径错的钉子：只要有人再把两个时间聚合写成一个数，恒等式就不闭合；
    而只要两个字段合一，第二条断言就红。
    """
    d = _load("width_frontier.json")
    n = 0
    for key, b in _blocks(d):
        lhs = b["pool_mu_bp"] - b["pool_med_bp"]
        assert abs(lhs - b["pool_mean_minus_median_bp"]) < 0.05, key
        assert abs(b["pool_med_of_daily_med_bp"] - b["pool_med_bp"]) > 1.0, key
        n += 1
    assert n == 4, f"应有 4 个口径块，实际 {n}"


def test_winsorize_labels_say_which_tail():
    """单边上尾与双侧缩尾必须是两个字段，且定义里写明是哪一个。

    它们数值接近（本例相差 0.3bp），正是这一点让标签错一直没被数值异常抓住——
    所以这里不比较大小，只比较**名字有没有把口径说出来**。
    """
    d = _load("width_frontier.json")
    defs = d["definitions"]
    assert "单边" in defs["layer_w99_bp"]
    assert "双侧" in defs["layer_w01w99_bp"]
    for key, b in _blocks(d):
        assert "layer_w99_bp" in b and "layer_w01w99_bp" in b, key


def test_both_win_rate_calibers_exist():
    """单次持有胜率只认 `stride_win`：每 h 日取一日，取到的 fwd_h 互不相交。

    逐日胜率（重叠）与分块均值胜率都会高估它——本例 72.9% / 79.5% / 67.5%，
    三个数都在产物里，谁也不能冒充谁。
    """
    d = _load("width_frontier.json")
    for key, b in _blocks(d):
        for f in ("win_daily", "block_win", "stride_win"):
            assert f in b, (key, f)
        assert 0.0 <= b["stride_win"]["win_rate"] <= 1.0, key
        # ceil(days/h) vs floor(days/h)：两个口径的样本数只差这一个。
        assert b["stride_win"]["n"] in (b["block_win"]["n_blocks"],
                                        b["block_win"]["n_blocks"] + 1), key


def test_since_blocks_carry_end_and_day_count():
    """子样本只存起点等于「不给分母」：383 天会被读成两年。"""
    d = _load("width_frontier.json")
    tags = [k for k in d if "_since_" in k]
    assert tags, "至少有一个子样本块"
    for t in tags:
        b = d[t]
        assert b["since"] and b["until"], t
        assert b["until"] > b["since"], t
        assert b["days"] > 0, t
    full = d["size_turnover_H60"]
    assert full["days"] > max(d[t]["days"] for t in tags), "全样本天数必须多于子样本"


def test_liquidity_identity_keeps_slopes_and_window():
    """每档斜率必须与网格同形落盘；没有斜率字段，「杀伤只在小盘端」就无法证伪。"""
    d = _load("liquidity_identity.json")
    for k in ("size_x_rate", "size_x_rate_since2024", "rate_x_amount", "price_x_amount"):
        b = d[k]
        assert len(b["slope_per_quintile_bp"]) == len(b["mat"]), k
        assert all(isinstance(v, float) for v in b["slope_per_quintile_bp"]), k
        assert b["until"] and b["days"] > 0, k


def test_survivorship_census_is_recorded():
    """生存偏差的**输入**必须写进产物：只报一个点估计、不记缺了多少只与占多大权重，
    那个数就既无法被复核，也无法被推翻。"""
    d = _load("survivorship_bound.json")
    c = d["census"]
    assert c["instruments"] > c["in_dump"] > 0
    assert c["missing"] == c["instruments"] - c["in_dump"]
    assert c["delisted"] > 0
    assert c["missing_weight"] == pytest.approx(c["missing"] / c["instruments"])
    assert d["baseline_bp"] > 0
    assert d["injection"], "注入结果不能是空的"
    assert d["mechanism_ladder"], "机制梯不能是空的"


def test_survivorship_artifact_is_sealed_but_not_date_scanned():
    """产物进封签、**不进**日期扫描表——两个清单服务两个问题，不许互相顶替。

    不给它封签的后果是实测过的：改产物 + 改注释，`audit` / `verify-seal` /
    溯源三道门可以一起绿，而证据已经换了人。反过来把它塞进 `ARTIFACTS` 也错：
    这份 JSON 没有逐日键，`audit` 只会多报一行「0 天」，把「清单已闭合」
    变成一句看着像检查、其实什么都没扫的话。
    """
    gate = _gate()
    art = REPO / "data" / ".scratch" / "survivorship_bound.json"
    assert art in gate.SEALED, "产物不在封签里，改它不会触发漂移"
    assert art not in gate.ARTIFACTS, "无逐日键的产物不该进日期扫描表"


def test_survivorship_draw_is_deterministic():
    """同一个种子必须抽到同一批标的，**且不依赖字典顺序**。

    旧版直接把 `df["thscode"].unique().to_numpy()` 喂给 `rng.choice`。polars 的
    `unique()` 每次调用顺序都不同（`maintain_order` 默认 False），于是 `SEED`
    只钉住了随机数序列、没钉住「从谁里面抽」：同一进程里两次同种子运行，同一格
    的重复均值可以差到与该格自身散布同量级。产物里那个 `seed` 字段因此是装饰品
    ——它声明的是确定性，而实际发生的是重新抽签。具体量级记在账本 §9.42 五，
    不进这里：散文里的数没有出处，只会变成下一份报告里的第二个 `−60bp`。
    """
    probe = _probe()
    import numpy as np
    import polars as pl

    names = ("c", "a", "b", "d", "e", "f")
    idx = {s: pl.DataFrame({"date": range(200)}) for s in names}
    first = probe.draw(idx, 3, np.random.default_rng(0))
    # 换一个插入顺序：排序之后，抽到的必须一模一样
    shuffled = {s: idx[s] for s in reversed(names)}
    second = probe.draw(shuffled, 3, np.random.default_rng(0))
    assert first == second, "抽签依赖字典顺序 ⇒ 种子钉不住标的"
    assert len(first) == 3


def test_truncate_only_arm_is_a_paired_control():
    """`crash=0` 必须能把「削平」单独拆出来，否则它不配当稀释读数。

    探针的 docstring 曾承诺一个 `truncate_only` 档，代码里却没有：`clip=False`
    分支零调用点，于是 `crash=0` 一直是「截断 + 削平」的混合物，而账本拿它当
    稀释代表。这条钉住三件事：这一档真的落在产物里、与 `crash=0` 档**配对**
    （同一批标的 ⇒ 在场率逐位相同）、只截断不削平者的损失更小。
    """
    d = _load("survivorship_bound.json")
    tr = d.get("truncate_only")
    assert tr, "缺 truncate_only 档 ⇒ crash=0 又变成「含削平」的代名词"
    ref = {(r["mode"], r["k"], r["crash"]): r for r in d["injection"]}
    for r in tr:
        k = r["k"]
        base = ref[("forced", k, 0.0)]
        assert r["clip"] is False
        assert r["reps"] == base["reps"], "两档必须同一批 rep，否则差里混着抽样噪声"
        assert r["presence_med"] == pytest.approx(
            d["presence_med"][str(k)], abs=1e-9), "在场率必须相同才叫配对"
        assert r["delta_bp_mean"] > base["delta_bp_mean"], "不削平应比削平损失更小"


def test_closed_form_keeps_three_distinct_anchors():
    """三个锚是三个对象，不许互相顶替。

    `identity_bp` 假设层线性长到 N+K 且原成员全留；实现是分位截断（`_layer_flag`），
    注入后层的增量只有 K 的两成，原层却要被挤出相当于 K 的八成，所以它系统性
    偏负，只能当一阶参照。
    真正可独立复算的是 `displacement_identity_bp`（解析式），它必须与「全在场重算」
    的模拟值贴得很近——否则说明解析式的层定义又和实现分家了。
    """
    d = _load("survivorship_bound.json")
    for c in d["closed_form"]:
        k, sim = c["k"], c["all_present_pool_mean_bp"]
        ana, one = c["displacement_identity_bp"], c["identity_bp"]
        assert abs(ana - sim) < 2.0, (
            f"K={k} 解析锚 {ana:.2f} 与全在场重算 {sim:.2f} 差得太多，层定义分家了")
        assert one < sim - 4.0, f"K={k} 一阶参照没有偏负，公式或数据被换过"


def test_forced_arm_records_its_actual_layer_rate():
    """`forced` 是「近似强制」，真实入层率必须进产物——否则「上界」无从复核。

    `protocol.forced` 曾指向 `injection[].synth_in_layer`，而那一列并不存在；
    一个自称上界的臂，读者连它有几成真的进了层都查不到。
    """
    d = _load("survivorship_bound.json")
    rows = [r for r in d["injection"] if r["mode"] == "forced"]
    assert rows, "forced 臂不能是空的"
    for r in rows:
        assert "synth_in_layer" in r, "protocol 引用了不存在的字段"
        assert 0.85 <= r["synth_in_layer"] <= 1.0, (
            f"K={r['k']} 入层率 {r['synth_in_layer']}：forced 已不近似强制")


def test_abs_width_rule_fails_the_cross_layer_and_pre2024_gate():
    """`abs` 不可采纳——由 CI 守，不由记忆守。

    判据（预注册 §12.17 第一条）：一个宽度规则算「可采纳」，当且仅当**存在同一个
    N**，使它在两个流动性层同时满足
      (a) 全样本 |t| ≥ 2；
      (b) 2017–2023 子样本 |t| ≥ 2。
    两个层、两个区间，缺一个都不行：

      * 只看成交额层 ⇒ `abs/500` 的全样本 |t| 过线，而同一规则在换手率层不过线；
      * 只看全样本 ⇒ 均值集中在 2024 起，2017–2023 是另一回事。

    这两条检验此前**一条都没做**，于是「abs 有机制」被写进了报告，而 `t=3.07`
    其实是命中一个「只在成交额层、且主要在 2024 之后」的格子。把判据写成断言
    而不是散文，是因为散文里的判据会在下一次争论里被重新解释一遍。

    断言的方向是「撤回成立」：哪天它红了，说明证据反过来了，账本必须重写。
    """
    d = _load("abs_replicate.json")
    n_layers = len(d["layers"])
    assert n_layers >= 2, "只有一个流动性层 ⇒ 跨层检验无从谈起"

    worst: dict[str, dict[str, float]] = {}
    for key, layer in d["layers"].items():
        assert layer["cells"], key
        for name, c in layer["cells"].items():
            if not name.startswith("abs/"):
                continue
            worst.setdefault(name, {})[key] = min(abs(c["t"]), abs(c["pre2024"]["t"]))
    assert worst, "产物里一格 abs 都没有 ⇒ 这条钉子没在看东西"

    for name, per_layer in worst.items():
        assert len(per_layer) == n_layers, (
            f"{name} 只出现在 {sorted(per_layer)} 层——同一个 N 必须在每一层都有读数")

    best_n, best = max(((n, min(v.values())) for n, v in worst.items()),
                       key=lambda kv: kv[1])
    assert best < 2.0, (
        f"{best_n} 在「两层 × 两段」上的较差 |t| 已到 {best:.2f} ≥ 2："
        "「abs 不构成机制」这条撤回失效，账本 §9.43 与预注册 §12.17 必须重写")
