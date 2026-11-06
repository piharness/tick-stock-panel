"""确认段读取闸门：把「没读确认段」从一句声明变成可执行、可复核的检查。

预注册 §5 把 2026-10-09 起的 250 个决策日划为确认样本，§8 要求分析代码固定。
这两条原先只有文字，没有任何执行点，而漏洞是**按时钟打开的**：

  `probe_window.build()` 的决策日边界是 `len(cal)-HORIZON`，没有日期上限。
  2026-10-09 的 t+20 在 2026-11-06 收盘后齐备——从那天起，一次例行
  `--rebuild` 就会把这个确认日写进三个缓存，并且不打印任何提示。
  此后的 `--nmax=325` 只是决定**打印几行**，不决定算过什么、看过什么。

所以本模块做三件事，缺一件这条规则就不成立：

  * **截断** `cap_calendar()`：决策日截到 2026-10-08，但保留 t+20 所需的
    未来价——那是探索日决策的收益，不是确认段样本（§5 的采样单位是决策日，
    不是价格日历）。
  * **记账** `log_read()`：往 `docs/decision-read-log.jsonl` 追加一行，每行
    带上一行哈希；`verify_log()` 能查出改写与删除，git 历史再兜一层。
  * **封签** `seal()`：把冻结的分析代码（**对 `_load()` 闭合**，含被
    `probe_net_diff` 导入的 `probe_paired_diff`）与 5 个产物哈希写成
    `docs/decision-seal.json`，哈希记进预注册 §12。脚本改一个字节，封签
    就对不上——这是「分析代码固定」唯一能验证的形式。每次签字另记一行到
    `docs/decision-seal-history.jsonl`（哈希链），重签不会抹掉上一版。

边界（必须写明，否则这个闸门会被当成它做不到的东西）：它管得住**写盘的
产物**与**经过这两个入口的调用**，管不住有人直接 scan parquet 打印一个数。
后者只能按 §5.1 定义为「读取」，靠日志与封签的对照发现，不能靠代码阻止。
说清楚这一点，比假装闸门是完备的有用。

**两个量，两条日历，不能互相顶替。** `audit()` 同时报：

  * **分析窗**——用 `cap_calendar()` 截过的历，恒 326 个决策日、末日
    2026-10-08。它回答「`probe_window.build()` 默认会算哪些天」，产物里的
    决策日必须落在它里面。
  * **确认时钟**——用全历。它回答「250 天还差多少」，**必须**每个交易日
    增长；截断它，时钟会永远停在 0。

v6 之前这两个量共用一个字段（`decision_days_last`），在 366 个分区时恰好
都等于 2026-10-08，所以看不出来。第 367 个分区一落盘，时钟前进而分析窗
不动，同一个字段就同时成了「正确」与「越界」——CI 会在产物干净的情况下
报一个「确认段越界」的错。分开报、分开断言，是这两个量唯一的正确形态。

用法：
    python scripts/decision_gate.py audit        # 分析窗（截断）+ 确认时钟（全历）
    python scripts/decision_gate.py seal --reason="改了什么"   # 只在干净状态下
    python scripts/decision_gate.py verify-log   # 校验读取日志的哈希链
    python scripts/decision_gate.py verify-seal  # 校验冻结文件是否漂移
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
TABLE = "kline_daily_enriched"

PREREG = DOCS / "decision-target-prereg.md"
SEAL = DOCS / "decision-seal.json"
SEAL_HISTORY = DOCS / "decision-seal-history.jsonl"
READ_LOG = DOCS / "decision-read-log.jsonl"

#: 探索段最后一个**决策日**。第 326 天（2026-10-08）已进入统计，一并划为
#: 探索性；改这个日期等于改样本区间，必须同时改预注册 §5。
EXPLORATION_END = "2026-10-08"
CONFIRM_START = "2026-10-09"
CONFIRM_DAYS = 250
PREREG_VERSION = "v13c"

#: 与 `probe_window.py` / `probe_net_diff.py` 同一组常数。这里重复一份是必要的：
#: 闸门不能 import 被它检查的脚本，否则脚本一改，闸门跟着改，检查就失效了。
LOOKBACK, HORIZON, TOP_N = 20, 20, 20
TZ = timezone(timedelta(hours=8))
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXC_CACHE = DATA / ".probe_per_day_exc.json"
NEUTRAL_CACHE = DATA / ".probe_neutral_per_day.json"
ARTIFACTS = (
    EXC_CACHE,
    DATA / ".probe_per_day_exc_mean.json",
    DATA / ".probe_per_day_exc_mcap.json",
    DATA / ".probe_per_day.json",
    #: v12：`probe_neutral.py` 的逐日格内序列。加进来只有一个理由——它是
    #: 「写出确认段读数」的第五条通路。产物清单少一个，`audit` 就会在
    #: 越界发生时报「干净」，而封签也没盖住它。清单必须对**写盘**闭合。
    NEUTRAL_CACHE,
    #: v13a：`probe_cs_ic.py` 的全截面 IC 逐日缓存（627KB，{策略:{日期}}）。
    #: 产地脚本不 import 闸门、不落 §5.2 拦截表——v12 的「清单对写盘闭合」
    #: 对它不成立，`audit` 恒报「无此产物」。366 个分区时它恰好只算到
    #: 2026-10-08，所以看起来干净；第 367 个分区落盘后第一次重跑 cs_ic，
    #: 2026-10-09 就会写进一个 audit 看不见的文件。日期上限防「算」，
    #: 清单闭合防「算了没人知道」——两件事，缺一不可。
    DATA / ".probe_cs_ic.json",
)
#: 封签覆盖的文件：冻结的分析代码 + 产物。**不含文档**——封签哈希要写进
#: 预注册，把预注册本身哈希进去会形成循环，永远对不上。
#:
#: 覆盖集必须**对 `_load()` 闭合**：`probe_net_diff.py` 的配对 p 值与 se_HAC
#: 来自 `probe_paired_diff.py`，只哈希调用方等于把被调方留在门外。这条由
#: `test_seal_is_closed_over_loaded_scripts` 守着，不靠这份清单的自觉。
SEALED = (
    ROOT / "scripts" / "probe_window.py",
    ROOT / "scripts" / "probe_net_diff.py",
    ROOT / "scripts" / "probe_cohort_check.py",
    ROOT / "scripts" / "probe_basis_calibration.py",
    ROOT / "scripts" / "probe_paired_diff.py",
    #: v12：中性化是 §6.1 `146` 那一行的产地。不封它，等于把「low 是唯一
    #: 进得了门的策略」这个结论留在闸门外。
    ROOT / "scripts" / "probe_neutral.py",
    #: v13c（§12.12 预注册落地）：确认段判定的唯一合法生产入口。判定路径
    #: 的前置检查 e 靠封签自保——入口自己不进封签，「verify-seal ✓」就
    #: 钉不住任何东西。
    ROOT / "scripts" / "probe_confirm.py",
    Path(__file__).resolve(),
) + ARTIFACTS


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sessions(data_dir: Path | None = None) -> list[str]:
    base = (Path(data_dir) if data_dir else DATA) / TABLE
    if not base.is_dir():
        return []
    return sorted(c.name.split("=", 1)[1] for c in base.iterdir()
                  if c.is_dir() and c.name.startswith("date="))


def decision_days(cal: list[str]) -> list[str]:
    """`probe_window.build()` 的循环边界：t 需要 cal[i-20]、t、t+1、t+20。"""
    return cal[LOOKBACK: len(cal) - HORIZON]


def cap_calendar(cal: list[str], end: str = EXPLORATION_END) -> list[str]:
    """截到探索段最后一个决策日，再往后留 HORIZON 天供 t+20 取价。

    只截决策日、不截价格：2026-10-08 的 20 日收益要读到 2026-11-05 的收盘，
    那是这个决策的收益。把价格日历也截掉，会把第 326 天整个丢掉。
    """
    hi = bisect.bisect_right(cal, end) + HORIZON
    return cal[:min(len(cal), hi)]


def artifact_dates(path: Path) -> list[str]:
    """缓存是 {策略: {决策日: [...]}}；只认 YYYY-MM-DD 的键。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: set[str] = set()
    for per_day in raw.values():
        if isinstance(per_day, dict):
            out.update(k for k in per_day if DAY.match(k))
    return sorted(out)


def scan_artifacts(paths=ARTIFACTS) -> dict:
    rep = {}
    for p in map(Path, paths):
        if not p.exists():
            continue
        ds = artifact_dates(p)
        rep[p.name] = {
            "n_days": len(ds),
            "first": ds[0] if ds else None,
            "last": ds[-1] if ds else None,
            "confirmation_days": [d for d in ds if d >= CONFIRM_START],
        }
    return rep


def audit(data_dir: Path | None = None, paths=ARTIFACTS) -> dict:
    """报告分析窗与确认时钟**两个**量。合成一个数就会在分区增长时自相矛盾。"""
    cal = sessions(data_dir)
    #: 分析窗：与 `probe_window.build()` 同一截断，不随分区增长。
    dd_analysis = decision_days(cap_calendar(cal))
    #: 确认时钟：全历。它必须每天增长，否则 250 天永远到不了。
    dd_clock = decision_days(cal)
    art = scan_artifacts(paths)
    violations = [(name, d) for name, r in art.items()
                  for d in r["confirmation_days"]]
    conf = [d for d in dd_clock if d >= CONFIRM_START]
    return {
        "prereg_version": PREREG_VERSION,
        "exploration_end": EXPLORATION_END,
        "confirm_start": CONFIRM_START,
        "confirm_days_required": CONFIRM_DAYS,
        "price_sessions": {"n": len(cal), "first": cal[0] if cal else None,
                           "last": cal[-1] if cal else None},
        "analysis_window": {
            "decision_days": len(dd_analysis),
            "last_decision_day": dd_analysis[-1] if dd_analysis else None,
        },
        "confirmation_clock": {
            "decision_days_computable": len(conf),
            "first": conf[0] if conf else None,
            "last": conf[-1] if conf else None,
            "required": CONFIRM_DAYS,
            "remaining": max(0, CONFIRM_DAYS - len(conf)),
        },
        "artifacts": art,
        "violations": violations,
        "clean": not violations,
    }


def _chain(row: dict) -> str:
    body = {k: v for k, v in row.items() if k != "chain"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_log(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{p.name} 第 {i} 行不是 JSON：{e}") from e
    return rows


def log_read(script: str, argv=(), first: str | None = None,
             last: str | None = None, artifacts=(), path: Path | None = None) -> dict:
    """追加一行读取记录。**只追加**：没有任何改写或删除的入口。"""
    p = Path(path) if path else READ_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = _read_log(p)
    row = {
        "n": len(rows) + 1,
        "at": datetime.now(TZ).isoformat(timespec="seconds"),
        "script": script,
        "argv": " ".join(argv),
        "first_decision_day": first,
        "last_decision_day": last,
        "artifacts": {Path(a).name: sha256_file(Path(a))
                      for a in artifacts if Path(a).exists()},
        "prev": rows[-1]["chain"] if rows else "0" * 64,
    }
    row["chain"] = _chain(row)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def verify_log(path: Path | None = None) -> tuple[bool, str]:
    p = Path(path) if path else READ_LOG
    try:
        rows = _read_log(p)
    except ValueError as e:
        return False, str(e)
    prev = "0" * 64
    for i, row in enumerate(rows, 1):
        if row.get("n") != i:
            return False, f"第 {i} 行的序号是 {row.get('n')}——中间被删过"
        if row.get("prev") != prev:
            return False, f"第 {i} 行的 prev 对不上——日志被改写或插行"
        if _chain(row) != row.get("chain"):
            return False, f"第 {i} 行的哈希对不上——行内容被改过"
        prev = row["chain"]
    return True, f"{len(rows)} 行，链完整" if rows else "0 行（尚未读取确认段）"


def require_exploration(days, script: str, argv=(), allow: bool = False,
                        artifacts=(), path: Path | None = None) -> None:
    """含确认段决策日就拦。`allow=True` 是那一次正式读取，记账后放行。"""
    bad = [d for d in days if d >= CONFIRM_START]
    if not bad:
        return
    if not allow:
        raise SystemExit(
            f"{script}: 决策日 {bad[0]} … {bad[-1]}（{len(bad)} 天）属于确认段"
            f"（{CONFIRM_START} 起）。\n"
            "  确认段只读一次，且必须在 250 个决策日齐备后整体读（§5）。\n"
            "  若这确实是那一次：加 --include-confirmation 重跑。它会在\n"
            "  docs/decision-read-log.jsonl 追加一行不可撤销的记录。")
    log_read(script, argv, bad[0], bad[-1], artifacts, path=path)


def build_seal() -> dict:
    a = audit()
    files = {str(p.relative_to(ROOT)).replace("\\", "/"): sha256_file(p)
             for p in SEALED if p.exists()}
    payload = {
        "seal_version": 1,
        "prereg_version": PREREG_VERSION,
        "exploration_end": EXPLORATION_END,
        "confirm_start": CONFIRM_START,
        "confirm_days": CONFIRM_DAYS,
        "audit": a,
        "files": files,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["seal_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return payload


def seal_history_path(path: Path) -> Path:
    """历史文件跟着封签走：`decision-seal.json` → `decision-seal-history.jsonl`。"""
    p = Path(path)
    return p.with_name(p.stem + "-history.jsonl")


def _append_history(payload: dict, out: Path, old: dict | None,
                    reason: str = "") -> dict:
    """把这次签字接在链尾。**重签不再抹掉上一次的哈希。**

    只写一份 `decision-seal.json` 时，重签等于销毁证据：改到对得上为止这条
    路是敞开的。这里用与读取日志相同的哈希链，把每次签字钉在前一次后面，
    于是「签了几次、每次覆盖了什么」本身可复核。

    `reason` 只进历史行、不进封签哈希：两次内容相同的签字应当得到同一个
    `seal_sha256`，而「为什么又签了一次」是链上要回答的问题。v5→v6 只隔
    14 分钟、链上只剩两个哈希时，这个问题没有答案——链会退化成噪声，
    而噪声里藏改动正是这里要堵的路。
    """
    p = seal_history_path(out)
    rows = _read_log(p)
    if not rows and old:
        #: 迁移：历史文件还没有，但已有上一版封签——补成第 1 行，不丢。
        first = {"n": 1, "at": old.get("signed_at"),
                 "prereg_version": old.get("prereg_version"),
                 "seal_sha256": old.get("seal_sha256"),
                 "n_files": len(old.get("files", {})),
                 "prev": "0" * 64}
        first["chain"] = _chain(first)
        rows = [first]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n",
                     encoding="utf-8")
    row = {
        "n": len(rows) + 1,
        "at": payload["signed_at"],
        "prereg_version": payload["prereg_version"],
        "seal_sha256": payload["seal_sha256"],
        "n_files": len(payload["files"]),
        "reason": reason,
        "prev": rows[-1]["seal_sha256"] if rows else "0" * 64,
    }
    row["chain"] = _chain(row)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def verify_history(path: Path) -> tuple[list[str], dict | None]:
    """校验封签历史：序号连续、prev 相接、链哈希自洽。返回 (问题, 末行)。"""
    p = seal_history_path(path)
    if not p.exists():
        return [], None
    problems: list[str] = []
    try:
        rows = _read_log(p)
    except ValueError as e:
        return [str(e)], None
    prev = "0" * 64
    for i, row in enumerate(rows, 1):
        if row.get("n") != i:
            problems.append(f"{p.name} 第 {i} 行序号是 {row.get('n')}——被删过")
        if row.get("prev") != prev:
            problems.append(f"{p.name} 第 {i} 行 prev 对不上——链断")
        if _chain(row) != row.get("chain"):
            problems.append(f"{p.name} 第 {i} 行哈希对不上——被改过")
        #: 与读取日志不同，这里的 prev 存的是**上一版的封签哈希**，不是链哈希：
        #: 它要回答的是「我是接在哪一次签字后面的」，而不只是「我接在一行后面」。
        prev = row.get("seal_sha256")
    return problems, (rows[-1] if rows else None)


def seal(path: Path | None = None, reason: str = "") -> dict:
    payload = build_seal()
    if not payload["audit"]["clean"]:
        raise SystemExit(
            "缓存里已经出现确认段决策日，封签没有意义了：\n"
            f"  {payload['audit']['violations'][:3]}\n"
            "  先按 §5.3 记账（读了几次、读了哪一段），再谈封签。")
    payload["signed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    out = Path(path) if path else SEAL
    out.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    _append_history(payload, out, old, reason)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    return payload


def verify_seal(path: Path | None = None) -> tuple[bool, list[str]]:
    p = Path(path) if path else SEAL
    if not p.exists():
        return False, [f"缺少 {p.name}"]
    saved = json.loads(p.read_text(encoding="utf-8"))
    #: `signed_at` 与 `seal_sha256` 都不进哈希：前者是签的时刻，后者是哈希本身。
    body = {k: v for k, v in saved.items()
            if k not in ("seal_sha256", "signed_at")}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True)
    drift: list[str] = []
    if hashlib.sha256(blob.encode("utf-8")).hexdigest() != saved.get("seal_sha256"):
        drift.append("封签自身的哈希对不上——文件被改过")
    for rel, want in saved.get("files", {}).items():
        f = ROOT / rel
        if not f.exists():
            drift.append(f"{rel} 不存在")
        elif sha256_file(f) != want:
            drift.append(f"{rel} 已改动（封签后）")
    #: 历史链是「封签被换回去」的唯一探针：换回旧封签，历史末行就对不上。
    problems, last = verify_history(p)
    drift.extend(problems)
    if last is not None and last.get("seal_sha256") != saved.get("seal_sha256"):
        drift.append(f"封签历史末行 {str(last.get('seal_sha256'))[:12]}… "
                     "与当前封签不符——漏记账，或旧封签被换了回来")
    return (not drift), drift


def _report(a: dict) -> str:
    art = a["artifacts"]
    aw, clk = a["analysis_window"], a["confirmation_clock"]
    clock_line = (
        f"确认时钟   {clk['decision_days_computable']} / {clk['required']} "
        f"个确认段决策日已可算（首个 {clk['first']}，还差 {clk['remaining']} 个）"
        if clk["decision_days_computable"] else
        f"确认时钟   0 / {clk['required']} 个确认段决策日已可算"
        "（t+20 还没走完，签字时不存在可读的确认段结果）")
    lines = [
        f"探索段截止 {a['exploration_end']}   确认段起点 {a['confirm_start']}"
        f"   要求 {a['confirm_days_required']} 个决策日",
        f"价格日历   {a['price_sessions']['n']} 天"
        f"（{a['price_sessions']['first']} … {a['price_sessions']['last']}）",
        (f"分析窗     {aw['decision_days']} 个决策日，末日 {aw['last_decision_day']}"
         f"（截断到 {a['exploration_end']}，与 probe_window.build() 同源）"),
        clock_line,
        "",
        f"{'产物':<34}{'决策日':>7}{'末日':>13}{'确认段':>7}",
    ]
    for name, r in art.items():
        lines.append(f"{name:<34}{r['n_days']:>7}{str(r['last']):>13}"
                     f"{len(r['confirmation_days']):>7}")
    lines.append("")
    lines.append("判定：" + ("✓ 干净，产物里没有确认段决策日"
                             if a["clean"] else
                             f"✗ 越界 {len(a['violations'])} 处：{a['violations'][:5]}"))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="确认段读取闸门")
    ap.add_argument("cmd", choices=("audit", "seal", "verify-log", "verify-seal"))
    ap.add_argument("--reason", default="",
                    help="这次重签改了什么；随本次签字写进封签历史，链上才读得出原因")
    args = ap.parse_args()

    if args.cmd == "audit":
        a = audit()
        print(_report(a))
        sys.exit(0 if a["clean"] else 1)

    if args.cmd == "seal":
        a = audit()
        print(_report(a))
        if not a["clean"]:
            sys.exit(1)
        payload = seal(reason=args.reason)
        print(f"\n封签 {payload['seal_sha256']}")
        print(f"写入 {SEAL.relative_to(ROOT)}（{len(payload['files'])} 个文件）")
        print(f"原因 {args.reason or '（未填——下一次没人知道这次为什么签）'}")
        _, last = verify_history(SEAL)
        if last:
            print(f"历史第 {last['n']} 次签字，prev {str(last.get('prev'))[:12]}…"
                  f"（{seal_history_path(SEAL).name}）")
        print("把这个哈希抄进 docs/decision-target-prereg.md §12，否则封签无人认领。")
        return

    if args.cmd == "verify-log":
        ok, msg = verify_log()
        print(("✓ " if ok else "✗ ") + msg)
        sys.exit(0 if ok else 1)

    ok, drift = verify_seal()
    print(("✓ 封签未漂移" if ok else "✗ 封签已失效：\n  " + "\n  ".join(drift)))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
