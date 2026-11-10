"""GP 字段探针（第 1 步）：一次调用回答三件事——有没有权限、返回什么形状、能不能落成本地列。

**尚未执行。** 本脚本会消耗账户积分，必须拿到明确授权后手动跑：

    backend/.venv/Scripts/python.exe analysis/_diag_probe_gp.py --yes

不带 `--yes` 时只打印将要发出的请求，不连服务器（连 DLL 都不加载）。

三件为什么这么写：

1. **不能照厂商文档写参数。** 文档写的是 `get_gpjy_value(stock_code, field_list, start_date, end_date)`，
   装机的 1.2.2 实际是 `(stock_list, field_list, start_time, end_time)`，另一个 `by_date`
   版本是 `(stock_list, field_list, year, mmdd)`。按文档写会得到一整轮空返回，还容易被读成"无权限"。
2. **不能直接静调 `tqs.<method>`。** 项目既有路径是先 `materialize_lib` 把 token 写进
   `TdxAiData.ini`/`user.ini`、设 `TDX_AI_DATA_LIB`，再用 `_NativeTdxAiData.get_instance()`
   调原生方法，最后由 `tqs._clean` 解包（见 `backend/app/plugins/tdxquant/client.py`）。
   绕开这套拿到的空返回，无法区分"没权限"和"没初始化"。
3. **必须把错误原文打出来。** 探针的作用是区分四种情况：无权限 / 无覆盖期 / 参数错 / 有数据。
   只报 `None` 或 `空`，下次还得再花一次积分重探。

产出：`analysis/_diag_probe_gp_result.json`（含每个接口的 keys、形状、前几行、错误原文）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_REPO_ROOT = HERE.parent
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

RESULT_PATH = HERE / "_diag_probe_gp_result.json"

#: 与打板本体直接相关的四个专业数据字段，覆盖期见厂商文档 tdxaidata使用文档.md:2022-2061
DEFAULT_FIELDS = ["GP14", "GP15", "GP22", "GP24"]
#: 主板/创业板/科创板各取一只，用来区分"某只没数据"与"整批没权限"
DEFAULT_SYMBOLS = ["600000.SH", "000001.SZ", "300750.SZ", "688981.SH"]


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
                        out["date_range"] = [
                            str(value[col].min()),
                            str(value[col].max()),
                        ]
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
            first = next(iter(value.values()), None) if value else None
            out["first_value"] = _shape(first, head)
        elif isinstance(value, (list, tuple)):
            out["n"] = len(value)
            out["head"] = [str(item)[:200] for item in list(value)[:head]]
        else:
            out["repr"] = str(value)[:300]
    except Exception as exc:  # noqa: BLE001 — 形状摘要是诊断用，不能自己炸
        out["shape_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _call(client: Any, method: str, *args: Any) -> dict[str, Any]:
    """调一个原生方法，把结果或错误原文都记下来。"""
    record: dict[str, Any] = {"method": method, "args_preview": [str(a)[:160] for a in args]}
    try:
        value = client._raw(method, *args)
        record["ok"] = True
        record["shape"] = _shape(value)
    except Exception as exc:  # noqa: BLE001 — 探针就是要抓异常原文
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description="GP 字段权限/形状探针（消耗积分）")
    ap.add_argument("--yes", action="store_true", help="确认消耗账户积分并真正发起调用")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--fields", nargs="+", default=DEFAULT_FIELDS)
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2024-01-31")
    args = ap.parse_args()

    plan = {
        "get_gpjy_value": [args.symbols, args.fields, args.start, args.end],
        "get_zdt_data": [args.symbols],
        "get_more_info_batch": [args.symbols, []],
    }
    if not args.yes:
        print("将要发出的调用（未执行，不带 --yes 不连服务器）：")
        for method, a in plan.items():
            print(f"  {method}{tuple(str(x)[:60] for x in a)}")
        print("\n确认消耗积分后加 --yes 重跑。")
        return 0

    from app.plugins.tdxquant.client import TdxQuantClient
    from app.plugins.tdxquant.provider import get_api_key

    key = get_api_key()
    if not key:
        print("未配置 tdxquant Key，无法探测。", file=sys.stderr)
        return 1

    # DLL 冷启动握手约 30s，客户端的 _raw 自带重试；这里只记录单次探测结果。
    client = TdxQuantClient(api_key=key)
    records = [
        _call(client, "get_gpjy_value", args.symbols, args.fields, args.start, args.end),
        _call(client, "get_zdt_data", args.symbols),
        _call(client, "get_more_info_batch", args.symbols, []),
    ]

    result = {
        "generated_by": "analysis/_diag_probe_gp.py",
        "request": {
            "symbols": args.symbols,
            "fields": args.fields,
            "start": args.start,
            "end": args.end,
        },
        "records": records,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for rec in records:
        status = "OK  " if rec["ok"] else "FAIL"
        print(f"[{status}] {rec['method']}")
        if rec["ok"]:
            print(f"        {json.dumps(rec['shape'], ensure_ascii=False)[:600]}")
        else:
            print(f"        {rec['error']}")
    print(f"\n→ {RESULT_PATH.name}")
    return 0 if all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
