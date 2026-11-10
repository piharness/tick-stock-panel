"""GP 字段探针（P-GP-SEAL-1）：一次授权回答三件事——有没有权限、返回什么形状、能不能落成本地列。

**尚未执行。** 本脚本会消耗账户积分，必须拿到明确授权后手动跑：

    backend/.venv/Scripts/python.exe analysis/_diag_probe_gp.py --yes
    backend/.venv/Scripts/python.exe analysis/_diag_probe_gp.py --evaluate   # 跑完再判 P1/P2/P3

不带 `--yes` 时只打印将要发出的请求，不连服务器（连 DLL 都不加载）。

四件为什么这么写：

1. **不能照厂商文档写参数。** 文档写的是 `get_gpjy_value(stock_code, field_list, start_date, end_date)`，
   装机的 1.2.2 实际是 `(stock_list, field_list, start_time, end_time)`，另一个 `by_date`
   版本是 `(stock_list, field_list, year, mmdd)`。按文档写会得到一整轮空返回，还容易被读成"无权限"。
2. **不能直接静调 `tqs.<method>`。** 项目既有路径是先 `materialize_lib` 把 token 写进
   `TdxAiData.ini`/`user.ini`、设 `TDX_AI_DATA_LIB`，再用 `_NativeTdxAiData.get_instance()`
   调原生方法，最后由 `tqs._clean` 解包（见 `backend/app/plugins/tdxquant/client.py`）。
   绕开这套拿到的空返回，无法区分"没权限"和"没初始化"。
3. **不能走 `client._raw("get_gpjy_value", ...)`。** 实测 `hasattr(_NativeTdxAiData, 'get_gpjy_value')
   == False`：GP 系列是 `tqs` 的 classmethod（内部转 `get_pro_data` 的 `type=3` 协议），
   原生层只暴露 `get_pro_data`。走 `_raw` 会 `AttributeError`，被 `_raw` 包成一句不含
   "权限/积分/额度"的普通异常 —— 按预注册 §4 会被判成 F3→重试→"按 F1 处理"→**错误关线**。
   所以这里直接构造 `_pro` 同款 payload 调 `get_pro_data`，顺带拿到未清洗的 `ErrorId/Error` 原文
   （`_clean` 只 print 不抛，走 `tqs.get_gpjy_value` 会把错误文本吃掉）。
4. **必须把错误原文打出来。** 探针的作用是区分五种情况：无权限 / 无覆盖期 / 参数错 / 调用层不可用 / 有数据。
   只报 `None` 或 `空`，下次还得再花一次积分重探。

产出：`analysis/_diag_probe_gp_result.json`（每票一个请求，含 ErrorId/Error、形状、值原文）

**判据不在本文件里。** 判据（PASS 三条 / FAIL 五类 / 选样口径）在
`analysis/_prereg_gp_seal_probe.md`，必须先提交；本脚本在 `--yes` 之前只做两件强制检查：
文件在场且已被 git 跟踪、且该文件工作区无未提交改动。否则拒跑 —— 否则拿到 JSON 再改判据，
等于把 `.scratch` 换个后缀。理由见预注册 §0：这次调用只能否决研究线，不能授权。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_REPO_ROOT = HERE.parent
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

RESULT_PATH = HERE / "_diag_probe_gp_result.json"
VERDICT_PATH = HERE / "_diag_probe_gp_verdict.json"
PREREG_PATH = HERE / "_prereg_gp_seal_probe.md"

#: 与打板本体直接相关的四个专业数据字段，覆盖期见厂商文档 tdxaidata使用文档.md:2022-2030
#: 语义：GP14=涨停金额+开板次数；GP15=涨跌停状态+封单金额(万)；GP22=封成比+封流比；
#: GP24=首次涨停时间+涨停最大封单额(万)。**GP14 不是封单量**，别拿它当封板强度。
DEFAULT_FIELDS = ["GP14", "GP15", "GP22", "GP24"]
#: 只有显式 --symbols 时生效；缺省走龙虎榜当日正控/负控（见 _pick_controls）
DEFAULT_SYMBOLS: list[str] | None = None

#: 探针基准日 —— 与本地 data/dragon_tiger/date=<该日>.json 同日，供交叉核对
DEFAULT_DAY = "2025-11-13"

#: 板块自适应达板阈值（单位：change 小数）。主板 10%、创业/科创 20%、北交 30%
_BOARD_LIMIT = {"main": 0.099, "growth": 0.199, "bj": 0.299}

#: 北交前缀。**43 也要算**：430xxx 是老新三板转板代码，漏了会按主板 9.9% 阈值被当成"达板"
_BJ_PREFIX = ("43", "83", "87", "88", "92")


def _board(code: str) -> str:
    if code.startswith(_BJ_PREFIX):
        return "bj"
    if code.startswith(("30", "68")):
        return "growth"
    return "main"


def _suffix(code: str) -> str:
    board = _board(code)
    if board == "bj":
        return ".BJ"
    if board == "growth":
        return ".SZ" if code.startswith("3") else ".SH"
    return ".SH" if code.startswith("6") else ".SZ"


def _pick_controls(data_dir: Path, day: str, n_pos: int = 2, n_neg: int = 2) -> dict[str, Any]:
    """按预注册 §2 的口径挑正控/负控。规则写死在文档里，这里只做实现，不改口径。

    三个坑写在这里防复发：
    - `change` 是小数不是百分数（当日 max=0.299661），阈值必须用 0.099/0.199/0.299。
    - 不能用 `|change| < 5%` 选负控：ST 票 5% 即涨停，会被选成"非涨停"。
    - 榜单按上榜原因可能同一 ticker 多行（当日 70 行 / 唯一 64），必须先按 ticker 去重。
    """
    path = data_dir / "dragon_tiger" / f"date={day}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = ((raw.get("all") or {}).get("stock_items")) or []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items:
        code = str(it.get("ticker") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            chg = float(it.get("change"))
        except (TypeError, ValueError):
            continue
        name = str(it.get("name") or "")
        rows.append({"code": code, "name": name, "change": chg, "st": "ST" in name.upper()})

    pos = [
        r for r in rows
        if not r["st"] and _board(r["code"]) != "bj" and r["change"] >= _BOARD_LIMIT[_board(r["code"])]
    ]
    neg = [r for r in rows if not r["st"] and _board(r["code"]) != "bj" and abs(r["change"]) < 0.02]
    pos.sort(key=lambda r: -r["change"])
    neg.sort(key=lambda r: abs(r["change"]))

    pos_pick, neg_pick = pos[:n_pos], neg[:n_neg]
    if len(pos_pick) < n_pos or len(neg_pick) < n_neg:
        raise SystemExit(
            f"{day} 凑不齐对照：正控 {len(pos_pick)}/{n_pos}（候选 {len(pos)}），"
            f"负控 {len(neg_pick)}/{n_neg}（候选 {len(neg)}）"
        )
    return {
        "day": day,
        "universe": {"items": len(items), "unique": len(seen), "hit_limit": len(pos), "below_2pct": len(neg)},
        "positive": pos_pick,
        "negative": neg_pick,
        "symbols": [r["code"] + _suffix(r["code"]) for r in pos_pick + neg_pick],
    }


def _prereg_guard() -> dict[str, Any]:
    """判据护栏：文件在场 + 已被 git 跟踪 + 工作区干净。返回可写进结果 JSON 的凭证（或拒跑）。"""
    if not PREREG_PATH.exists():
        raise SystemExit(f"缺少预注册 {PREREG_PATH.name}，拒绝发起真实调用（判据必须先于数据）")
    sha = hashlib.sha256(PREREG_PATH.read_bytes()).hexdigest()
    repo = HERE.parent
    guard: dict[str, Any] = {"path": PREREG_PATH.name, "sha256": sha, "dirty_check": "skipped_no_git"}
    if not (repo / ".git").exists():
        return guard

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SystemExit(f"git 不可查（{exc}），无法证明判据未改，拒绝发起调用") from exc

    # ① 必须已被跟踪。只查 status 不够：文件若落进 .gitignore，status 永远是空 ⇒ 假绿。
    tracked = _git("ls-files", "--error-unmatch", str(PREREG_PATH))
    if tracked.returncode != 0:
        raise SystemExit(
            f"预注册未被 git 跟踪（可能被 .gitignore 遮挡）：{PREREG_PATH.name} —— "
            "护栏无法区分「已提交」与「被忽略」，拒绝发起调用"
        )
    # ② 工作区不能有未提交改动（含 untracked / staged 未提交 / 已删）
    dirty = _git("status", "--porcelain", "--", str(PREREG_PATH))
    if dirty.stdout.strip():
        raise SystemExit(
            f"预注册有未提交改动：{dirty.stdout.strip()[:100]} —— 先提交判据再跑，否则等于事后改标准"
        )
    guard["dirty_check"] = "clean"
    guard["git_head"] = _git("rev-parse", "HEAD").stdout.strip()
    # 回执的可复核标识必须是 blob 号：sha256 取的是工作区字节，core.autocrlf 下
    # 干净 clone 出来是 LF、本机是 CRLF，同一份判据会算出两个哈希（实测 c5c8eac3 vs 1aba83f7）。
    rel = PREREG_PATH.relative_to(repo).as_posix()
    guard["git_blob"] = _git("rev-parse", f"HEAD:{rel}").stdout.strip()
    guard["sha256_note"] = "sha256 为工作区字节（受行尾影响）；对账请用 git_blob"
    return guard


def _shape(value: Any, head: int = 3) -> dict[str, Any]:
    """把一个返回值压成可读的形状摘要，尽量保留日期范围。"""
    out: dict[str, Any] = {"type": type(value).__name__}
    try:
        if hasattr(value, "to_dict") and hasattr(value, "columns"):  # polars / pandas
            columns = list(value.columns)
            out["columns"] = columns
            out["n_rows"] = int(value.height if hasattr(value, "height") else len(value))
            for col in columns:
                if any(k in col.lower() for k in ("date", "time", "day")):
                    try:
                        out["date_range"] = [str(value[col].min()), str(value[col].max())]
                    except Exception:  # noqa: BLE001 — 探针，取不到就算了
                        pass
                    break
            try:
                rows = value.head(head).to_dicts() if hasattr(value, "to_dicts") else value.head(head).to_dict("records")
                out["head"] = json.loads(json.dumps(rows, default=str))
            except Exception:  # noqa: BLE001
                out["head"] = str(value.head(head))
        elif isinstance(value, dict):
            out["keys"] = list(value)[:20]
            out["n_keys"] = len(value)
        elif isinstance(value, (list, tuple)):
            out["n"] = len(value)
            out["head"] = [str(item)[:200] for item in list(value)[:head]]
        else:
            out["repr"] = str(value)[:300]
    except Exception as exc:  # noqa: BLE001 — 形状摘要是诊断用，不能自己炸
        out["shape_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _pro_request(code: str, fields: list[str], start: str, end: str, tqs_mod: Any) -> dict[str, Any]:
    """复刻 `tqs._pro` 的 type=3 payload。时间格式必须走 `_time`，否则 "2025-11-13" 会被当非法。"""
    return {
        "type": 3,
        "code": code,
        "table_list": list(fields),
        "start_time": tqs_mod._time(start),
        "end_time": tqs_mod._time(end),
    }


def _gp_probe(native: Any, tqs_mod: Any, symbols: list[str], fields: list[str],
              start: str, end: str) -> list[dict[str, Any]]:
    """每票一个 `get_pro_data` 请求，保留未清洗的 ErrorId / Error 原文。

    为什么不用 `tqs.get_gpjy_value`：它内部 `_clean` 遇 ErrorId!=0 只 print 并返回 None，
    文本被吃掉，F1 的归因就没了。这里拿 raw 自己解析。
    """
    records: list[dict[str, Any]] = []
    for sym in symbols:
        req = _pro_request(sym, fields, start, end, tqs_mod)
        rec: dict[str, Any] = {"symbol": sym, "request": req}
        try:
            raw = native.get_pro_data(json.dumps(req, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 — 探针就是要抓异常原文
            rec.update(ok=False, empty=False, error=f"{type(exc).__name__}: {exc}")
            records.append(rec)
            continue

        rec["raw_head"] = str(raw)[:600]
        obj: Any = None
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            pass
        if isinstance(obj, dict):
            rec["error_id"] = obj.get("ErrorId")
            rec["error_text"] = obj.get("Error") or obj.get("error")
            rec["result_set_num"] = obj.get("ResultSetNum")

        value = tqs_mod._clean(raw)
        if isinstance(value, dict) and "Value" in value:
            value = value["Value"]
        rec["shape"] = _shape(value)
        rec["empty"] = value in (None, {}, [])
        rec["ok"] = not rec["empty"]
        rec["value_json"] = json.dumps(value, ensure_ascii=False, default=str)[:40000] if rec["ok"] else None
        records.append(rec)
    return records


# ---------------------------------------------------------------- evaluate

#: 子列顺序由厂商文档 tdxaidata使用文档.md:2022-2030 定义（不是从数据里猜列名再拟合）。
#: 实测回执形状：{"GP15": [{"Date": "20251113", "Value": ["2.00", "30270.45"]}]}
_FIELD_COLS = {
    "GP14": ("涨停金额万", "开板次数"),
    "GP15": ("涨跌停状态", "封单金额万"),
    "GP22": ("封成比", "封流比"),
    "GP24": ("首次涨停时间HHMMSS", "最大封单额万"),
}


def _series(value: Any) -> dict[str, dict[str, list[str]]]:
    """{FIELD: [{Date, Value:[...]}]} -> {FIELD: {Date: [值...]}}。"""
    out: dict[str, dict[str, list[str]]] = {}
    for field, rows in (value or {}).items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.setdefault(str(field), {})[str(row.get("Date") or "")] = [str(v) for v in (row.get("Value") or [])]
    return out


def _num(series: dict[str, dict[str, list[str]]], field: str, day: str, idx: int) -> float | None:
    vals = series.get(field, {}).get(day.replace("-", ""))
    if not vals or idx >= len(vals):
        return None
    try:
        return float(vals[idx])
    except (TypeError, ValueError):
        return None


def _amounts(day: str, symbols: list[str]) -> dict[str, float]:
    """当日成交额（元）。缺失即 P2 该只不计入（预注册 §3）。"""
    path = _REPO_ROOT / "data" / "kline_daily" / f"date={day}" / "part.parquet"
    if not path.exists():
        return {}
    try:
        import duckdb
    except ImportError:
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute(
            "select symbol, amount from read_parquet(?) where symbol in (" +
            ",".join("?" * len(symbols)) + ")", [str(path), *symbols]
        ).fetchall()
    finally:
        con.close()
    return {str(s): float(a) for s, a in rows if a is not None}


def evaluate() -> int:
    """按预注册 §3 判 P1/P2/P3，按 §4 归类 FAIL。只做机械映射，不改判据。"""
    if not RESULT_PATH.exists():
        print(f"缺少 {RESULT_PATH.name}，先跑 --yes", file=sys.stderr)
        return 2
    res = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    pos = {r["code"] + _suffix(r["code"]) for r in res["controls"]["positive"]}
    neg = {r["code"] + _suffix(r["code"]) for r in res["controls"]["negative"]}
    day = res["controls"]["day"]
    amounts = _amounts(day, list(pos | neg))

    per: dict[str, Any] = {}
    p1_pos: list[bool] = []
    p1_neg: list[bool] = []
    p2_hits: list[bool] = []
    p2_detail: list[dict[str, Any]] = []
    p3_flags: list[bool] = []
    p1_literal = True

    for rec in res["records"]:
        sym = rec["symbol"]
        role = "pos" if sym in pos else "neg"
        item: dict[str, Any] = {"role": role, "ok": rec["ok"]}
        if not rec["ok"]:
            item["why"] = rec.get("error_text") or rec.get("error") or "空返回且无错误原文"
            p1_literal = False
            per[sym] = item
            continue
        series = _series(json.loads(rec["value_json"] or "{}"))
        item["fields"] = sorted(series)
        if not series:
            p1_literal = False          # 例如 {"Data": {}}：结构非空、数据为空

        status = _num(series, "GP15", day, 0)
        seal = _num(series, "GP15", day, 1)
        item["GP15_status"] = status
        item["GP15_seal_wan"] = seal
        if role == "pos":
            p1_pos.append(status in (2, 1))
        else:
            # 负控"没有 GP15 行"本身就是合格结果：涨跌停字段家族是事件条件表
            p1_neg.append(status is None or status not in (2, 1) or not seal)

        if role == "pos" and seal is not None and sym in amounts and amounts[sym] > 0:
            fcb = _num(series, "GP22", day, 0)
            flr = _num(series, "GP22", day, 1)
            if fcb:
                # 量纲（§3）：落在 [1,100] 视作百分数，否则视作小数；择一，不得回头再换。
                scale = "percent" if 1.0 <= abs(fcb) <= 100.0 else "decimal"
                fcb_v = fcb / 100.0 if scale == "percent" else fcb
                implied = seal * 1e4 / amounts[sym]
                rel = abs(implied - fcb_v) / abs(fcb_v)
                ok = rel < 0.20
                item["P2"] = {"封单万": seal, "封成比原文": fcb, "封流比": flr, "量纲": scale,
                              "反算": implied, "相对误差": rel, "过": ok}
                p2_hits.append(ok)
                p2_detail.append({"symbol": sym, **item["P2"]})

        # P3 只对"有行"的标的判覆盖：负控无行是合格结果，谈不上覆盖/不覆盖
        if series:
            p3_flags.append(bool(series.get("GP15", {}).get(day.replace("-", ""))))
        item["P3_date_hit"] = bool(series.get("GP15", {}).get(day.replace("-", ""))) if series else None
        per[sym] = item

    # ---- 归类（§4）：F 类才关线；C1 是判据缺陷，不产生研究结论
    errs = [r for r in res["records"] if not r["ok"]]
    texts = " ".join(str(r.get("error_text") or r.get("error") or "") for r in errs)
    p1_sub = bool(p1_pos) and all(p1_pos) and bool(p1_neg) and all(p1_neg)
    p2 = sum(1 for h in p2_hits if h) >= 2
    p3 = bool(p3_flags) and all(p3_flags)

    if errs and len(errs) == len(res["records"]):
        if any(k in texts for k in ("权限", "积分", "额度", "认证", "套餐", "未开通", "token", "unauthori")):
            fail = "F1"
        elif not texts.strip():
            fail = "F4"
        else:
            fail = "F0"
    elif any(k in texts for k in ("has no attribute", "ImportError", "OSError")):
        fail = "F0"
    elif not p2 or not p3:
        fail = "F3"
    elif not p1_sub:
        fail = "F2"
    elif not p1_literal:
        fail = "C1"
    else:
        fail = "PASS"

    out = {
        "prereg_sha256": res["prereg"]["sha256"],
        "prereg_git_blob": res["prereg"].get("git_blob"),
        "git_head": res["prereg"].get("git_head"),
        "run_at": res["run_at"],
        "attempt": res["attempt"],
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "P1_literal": {"pass": p1_literal, "criterion": "§3 原文：4 只票都返回非空"},
        "P1_substantive": {"pass": p1_sub, "pos": p1_pos, "neg": p1_neg,
                           "criterion": "正控 GP15 状态∈{2,1}；负控无行或状态∉{2,1} 或封单为 0"},
        "P2": {"pass": p2, "hits": sum(1 for h in p2_hits if h), "n": len(p2_hits), "detail": p2_detail},
        "P3": {"pass": p3, "n": len(p3_flags)},
        "fail_class": fail,
        "per_symbol": per,
    }
    VERDICT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("P1_literal", "P1_substantive", "P2", "P3", "fail_class")},
                     ensure_ascii=False, indent=2))
    print(f"→ {VERDICT_PATH.name}")
    return 0 if fail == "PASS" else 1


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="GP 字段权限/语义探针（消耗积分）")
    ap.add_argument("--yes", action="store_true", help="确认消耗账户积分并真正发起调用")
    ap.add_argument("--evaluate", action="store_true", help="只判 P1/P2/P3（读已有 result JSON，不联网）")
    ap.add_argument("--day", default=DEFAULT_DAY, help="基准交易日；窗口默认收敛到这一天")
    ap.add_argument("--data-dir", default=str(_REPO_ROOT / "data"))
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="覆盖对照选样（不建议）")
    ap.add_argument("--fields", nargs="+", default=DEFAULT_FIELDS)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--attempt", type=int, default=1, help="预注册 §4 F3 允许原地重试一次，重试记 2")
    args = ap.parse_args()

    if args.evaluate:
        return evaluate()

    start = args.start or args.day
    end = args.end or args.day
    controls = _pick_controls(Path(args.data_dir), args.day)
    symbols = args.symbols or controls["symbols"]

    if not args.yes:
        print(f"对照（{args.day}，预注册 §2）：")
        for tag in ("positive", "negative"):
            for r in controls[tag]:
                print(f"  [{tag[:3]}] {r['code']} {r['name']} change={r['change']*100:.2f}%")
        print(f"  宇宙：{controls['universe']}")
        print(f"\n将要发出的 {len(symbols)} 个 get_pro_data 请求（type=3，每票一个，未执行）：")
        for sym in symbols:
            print(f"  {sym}  fields={args.fields}  {start}~{end}")
        print(f"\n判据：{PREREG_PATH.name}（提交后加 --yes 才允许真实调用）")
        return 0

    guard = _prereg_guard()

    from app.plugins.tdxquant.client import TdxQuantClient
    from app.plugins.tdxquant.provider import get_api_key

    key = get_api_key()
    if not key:
        print("未配置 tdxquant Key，无法探测。", file=sys.stderr)
        return 1

    from tdxaidata import tqs

    # DLL 冷启动握手约 30s。这里直接把 native 单例交给探针，不复用 _raw（它会把错误类型抹平）。
    client = TdxQuantClient(api_key=key)
    native = client._native_cls.get_instance()
    records = _gp_probe(native, tqs, symbols, args.fields, start, end)

    result = {
        "generated_by": "analysis/_diag_probe_gp.py",
        "prereg": guard,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "attempt": args.attempt,
        "controls": controls,
        "request": {"symbols": symbols, "fields": args.fields, "start": start, "end": end,
                    "call_layer": "native.get_pro_data(type=3)"},
        "records": records,
        # P1/P2/P3 由 --evaluate 按预注册 §3 机械判定；本文件只存原始回执
        "verdict": None,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for rec in records:
        status = "OK  " if rec["ok"] else "FAIL"
        print(f"[{status}] {rec['symbol']}")
        if rec["ok"]:
            print(f"        {json.dumps(rec['shape'], ensure_ascii=False)[:600]}")
        else:
            print(f"        {rec.get('error_text') or rec.get('error')}")
    print(f"\n→ {RESULT_PATH.name}")
    return 0 if all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
