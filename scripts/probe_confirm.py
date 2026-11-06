"""§4 确认段判定入口（v13c 预注册，docs/decision-target-prereg.md §12.12）。

§5 的两条规则——「确认段只读一次」与「必须在 250 个决策日齐备后整体读」
——在本文件存在之前都只有文字：`require_exploration()` 拦得住「越界」，
拦不住「提前」；250 天齐备那天，判定代码要在压力下临时写出来。本入口把
这两条变成启动前置检查，逐项报错原因，任一不过即退出（fail-closed）：

  a. `audit()` 干净——产物里没有确认段决策日越界；
  b. 确认时钟 ≥ 250/250——「齐备」从文字变成执行点；
  c. `verify_log()` 通过，且日志中没有 `probe_confirm` 行——判定输出
     只发生一次，这是「只读一次」在入口上的机械形式；
  d. SERIES 含全部四条格内均值键（策略名带 `_20` 后缀、格内基准叫
     `cell`；初稿误写 `|mean`，盘上不存在该命名，2026-11-06 复核修订改正）；
  e. `verify-seal` ✓——SERIES 不记 `--basis`，格内键的中心（mean/median）
     唯一机械溯源是封签钉住的字节；重物化（哪怕只换 `--basis`）必然破坏
     封签，本检查使判定路径免于静默读到 median 口径的序列。

边界（§12.12「不建立」清单原样有效）：不堵物化脚本的「提前放行」
（`--include-confirmation` 在时钟未满时仍会放行并记账——本入口只保证
**自己**不是那条通路的判定端）；不产出新口径、不改 §4/§5 任何常数；
判定门直接引用 `NET.CONFIRM_GATE` 单点定义，不在入口内重新推导——
`gate=CONFIRM_GATE` 的生产调用点全仓唯一（下方 `run()` 内一处）。

只读两样东西：SERIES（`data/.probe_neutral_per_day.json`，键 `策略|基准`，
值 `{决策日: bp}`）与日历/audit；**不写任何缓存**，唯一写盘动作是判定
打印完成后 `log_read("probe_confirm", ...)` 追加一行记账（附 SERIES 哈希）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: §12.12 预注册钉死的四条格内均值序列（§5 主基准：行业×规模格内均值）。
CONFIRM_KEYS = (
    "momentum_20|cell",
    "reversal_20|cell",
    "low_turnover|cell",
    "high_turnover|cell",
)
#: §12.12：决策行 = low×格内。
DECISION_ROW = "low_turnover|cell"
#: §5：确认窗 250 个决策日中缺失 ≤12 天（≤5%）直接判定并打印缺失清单；
#: >12 天拒绝，操作含义是顺延——等 SERIES 覆盖到凑足 250 个实际日再跑。
MAX_MISSING = 12


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 确认段闸门（audit / verify_log / verify_seal / log_read）。
GATE = _load("decision_gate", "scripts/decision_gate.py")
#: §4 判定核心（evaluate_series / holm / decide）与确认门常数。
NET = _load("probe_net_diff", "scripts/probe_net_diff.py")

SERIES_PATH = GATE.NEUTRAL_CACHE


def confirm_window(data_dir: Path | None = None) -> list[str]:
    """日历可算的前 250 个确认决策日（§5 原文：首个 2026-10-09）。

    决策日按全历算（不截断——截断历是分析窗的口径，确认时钟必须走）。
    首个可算确认日不是 2026-10-09，说明日历与 §5 的约定脱节，拒绝。
    """
    cal = GATE.sessions(data_dir)
    conf = [d for d in GATE.decision_days(cal) if d >= GATE.CONFIRM_START]
    win = conf[:GATE.CONFIRM_DAYS]
    if len(win) < GATE.CONFIRM_DAYS or win[0] != GATE.CONFIRM_START:
        raise SystemExit(
            "probe_confirm: 日历读不出 250 个确认决策日"
            f"（可算 {len(win)} 个，首个 {win[0] if win else '无'}）。"
            "分区没落齐就跑确认入口，说明日历与 §5 脱节——先查行情同步。")
    return win


def run(argv=(), *, data_dir: Path | None = None, series_path: Path | None = None,
        read_log_path: Path | None = None, seal_path: Path | None = None,
        echo=None) -> int:
    """前置检查 → 样本 → 判定 → 记账。返回 0；任何拒绝走 SystemExit（非零）。

    关键字参数仅供测试注入（tmp 日志 / tmp SERIES / 合成日历）；生产路径
    一律走默认值——SERIES 在封签钉住的路径上，日志在 read-log 上。
    """
    echo = echo if echo is not None else print
    argv = list(argv)
    log_path = Path(read_log_path) if read_log_path else GATE.READ_LOG
    series_path = Path(series_path) if series_path else SERIES_PATH

    # ---- 前置检查（§12.12 a–e，全部 fail-closed）----
    a = GATE.audit(data_dir=data_dir)
    if not a["clean"]:
        raise SystemExit(
            "probe_confirm: 前置检查 a 失败——audit 报产物越界：\n"
            f"  {a['violations'][:3]}\n"
            "  先按 §5.3 处理越界产物，再谈确认判定。")
    clk = a["confirmation_clock"]
    if clk["decision_days_computable"] < GATE.CONFIRM_DAYS:
        raise SystemExit(
            "probe_confirm: 前置检查 b 失败——确认时钟 "
            f"{clk['decision_days_computable']} / {GATE.CONFIRM_DAYS}，"
            f"还差 {clk['remaining']} 个确认决策日。\n"
            "  §5：250 个决策日齐备后才允许整体读；提前读是不完整的确认"
            "读取，样本作废。到齐之前本入口在真实数据上唯一能做的事就是拒绝。")

    ok, msg = GATE.verify_log(log_path)
    if not ok:
        raise SystemExit(
            f"probe_confirm: 前置检查 c 失败——verify_log 未通过：{msg}")
    #: 与闸门同包、同封签，读它的私有读取器等价于 `verify_log` 的内部视图。
    prior = [r for r in GATE._read_log(log_path)
             if r.get("script") == "probe_confirm"]
    if prior:
        raise SystemExit(
            "probe_confirm: 前置检查 c 失败——确认判定只读一次，read-log "
            f"已有 {len(prior)} 行 probe_confirm（首次 {prior[0].get('at')}）。"
            "§5 的判定输出已经发生过，不存在第二次。")

    series = json.loads(series_path.read_text(encoding="utf-8"))
    missing_keys = [k for k in CONFIRM_KEYS if k not in series]
    if missing_keys:
        raise SystemExit(
            "probe_confirm: 前置检查 d 失败——SERIES 缺少格内均值键：\n"
            f"  {missing_keys}\n"
            f"  盘上实际键形如 {sorted(series)[:5]} …\n"
            "  四条格内均值序列是 §5 主基准下的确认样本，缺一键即无法"
            "按预注册判定。")

    ok, drift = GATE.verify_seal(seal_path)
    if not ok:
        raise SystemExit(
            "probe_confirm: 前置检查 e 失败——verify-seal 未通过：\n  "
            + "\n  ".join(drift)
            + "\n  SERIES 不记 --basis，格内键的中心唯一机械溯源是封签字节；"
              "封签失配意味着产物可能已被重物化（含换口径），判定禁止进行。")

    # ---- 样本（§5 原文：日历前 250 个确认决策日中 SERIES 实际存在的天）----
    win = confirm_window(data_dir)
    plan: dict[str, tuple[list[str], list[str]]] = {}
    for key in CONFIRM_KEYS:
        days = set(series[key])
        used = [d for d in win if d in days]
        missing = [d for d in win if d not in days]
        if len(missing) > MAX_MISSING:
            raise SystemExit(
                f"probe_confirm: {key} 在确认窗内缺失 {len(missing)} 天"
                f"（> {MAX_MISSING}），按 §5 顺延——等 SERIES 覆盖到凑足 "
                "250 个实际日再跑。缺失清单：\n  " + "、".join(missing))
        plan[key] = (used, missing)
    #: 样本不得出现 2026-10-09 之前的决策日——win 由 CONFIRM_START 过滤，
    #: 这里是防线而不是装饰：SERIES 键再乱也进不了样本。
    assert all(d >= GATE.CONFIRM_START for u, _ in plan.values() for d in u)

    # ---- 判定（§4 原文：B=40、REPS=1000、SEED=0、成本 19bp、四格不变；
    #      gate=CONFIRM_GATE 生产调用点全仓唯一——本函数内这一处）----
    results, ps = {}, []
    for key in CONFIRM_KEYS:
        used, _ = plan[key]
        x = [float(series[key][d]) for d in used]
        r = NET.evaluate_series(x, gate=NET.CONFIRM_GATE)
        results[key] = r
        ps.append(r["p"])
    adj = NET.holm(ps)

    echo(f"确认窗 {win[0]} … {win[-1]}（{len(win)} 个决策日）"
         f"  判定门 {NET.CONFIRM_GATE:.1f}bp（CONFIRM_GATE，非裸门 200）")
    for i, key in enumerate(CONFIRM_KEYS):
        used, missing = plan[key]
        r = results[key]
        label = NET.decide(r["verdict"], adj[i])
        mark = "  ← 决策行（low×格内）" if key == DECISION_ROW else ""
        miss_txt = ("、".join(missing) if missing else "无")
        echo(f"{key:<22} n_used={len(used)}  缺失 {len(missing)} 天（{miss_txt}）"
             f"  mu={r['mu']:.1f}bp  净区间[{r['net_lo']:.1f}, {r['net_hi']:.1f}]"
             f"  MDE={r['mde']:.1f}bp  p={r['p']:.4f}  Holm={adj[i]:.4f}"
             f"  → {label}{mark}")
    echo(f"决策行 = {DECISION_ROW}（low×格内，Holm 族 = 4，§12.12）")

    # ---- 记账（唯一写盘动作，附 SERIES 哈希）----
    GATE.log_read("probe_confirm", argv, win[0], win[-1],
                  artifacts=(series_path,), path=log_path)
    echo("已记账 read-log ← probe_confirm")
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
