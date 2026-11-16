"""日分区纯度诊断：去指数行 / 去重复键之后，票池名单是否逐字不变。

不修改 `data/`。做法是给 `l1_cohort` 的取数函数套一层内存过滤器，
再跑同一个 `candidates(as_of)`，与冻结 cohort 比对。

用法:
    backend/.venv/Scripts/python.exe analysis/_diag_panel_purity.py
    backend/.venv/Scripts/python.exe analysis/_diag_panel_purity.py --as-of 2026-11-11

退出码: 0 = 三种口径的结论符合预期；1 = 预期被推翻。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.decision import l1_cohort  # noqa: E402
from app.parquet import scan_enriched_parquet as _orig_scan  # noqa: E402

FROZEN = ROOT / "analysis" / "auction_pool_cohort_2026-11-12.json"


def _index_symbols(data_dir: Path) -> set[str]:
    """instruments 里 asset_type=index 的全部 symbol。"""
    p = data_dir / "instruments_index" / "instruments_index.parquet"
    if not p.exists():
        return set()
    df = pl.read_parquet(p)
    if "asset_type" not in df.columns:
        return set(df["symbol"].to_list())
    return set(df.filter(pl.col("asset_type") == "index")["symbol"].to_list())


def _run(data_dir: Path, as_of: str, *, drop_index: bool, dedupe: bool) -> dict:
    l1_cohort._CACHE.clear()
    idx = _index_symbols(data_dir) if drop_index else set()

    def patched(glob_or_path):
        lf = _orig_scan(glob_or_path)
        if idx:
            lf = lf.filter(~pl.col("symbol").is_in(sorted(idx)))
        return lf

    l1_cohort.scan_enriched_parquet = patched
    try:
        return l1_cohort.candidates(data_dir, as_of)
    finally:
        l1_cohort.scan_enriched_parquet = _orig_scan


def _sym_list(res: dict) -> list[str]:
    """`candidates()` 的键是 `candidates`（不是 plan/picks）。"""
    rows = res.get("candidates") or []
    out = []
    for r in rows:
        s = r.get("symbol") if isinstance(r, dict) else r
        if s:
            out.append(str(s))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="日分区纯度诊断")
    ap.add_argument("--as-of", default="2026-11-11")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    as_of = args.as_of

    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))["symbols"] if FROZEN.exists() else []

    # A: 原样 —— 应当 raise（重复键）
    try:
        _run(data_dir, as_of, drop_index=False, dedupe=False)
        a_state = "OK"
    except ValueError as e:
        a_state = f"RAISE: {e}"
    print(f"[A] 原样                : {a_state}")

    # B/C 需要在 _panel 里做去重；这里用等价手法：预先把重复行按 keep=last 收敛
    # 通过给 patched 追加 unique 已足够——但 _panel 现成判据会在 unique 后通过。
    def _dedupe_wrap(enabled: bool, drop_index: bool):
        l1_cohort._CACHE.clear()
        idx = _index_symbols(data_dir) if drop_index else set()

        def patched(glob_or_path):
            lf = _orig_scan(glob_or_path)
            if idx:
                lf = lf.filter(~pl.col("symbol").is_in(sorted(idx)))
            if enabled:
                lf = lf.sort(["date", "symbol"]).unique(
                    subset=["date", "symbol"], keep="last", maintain_order=True
                )
            return lf

        l1_cohort.scan_enriched_parquet = patched

    results = {}
    for tag, dedupe, drop_index in (("B", True, False), ("C", True, True)):
        _dedupe_wrap(dedupe, drop_index)
        try:
            res = l1_cohort.candidates(data_dir, as_of)
            syms = _sym_list(res)
            results[tag] = syms
            print(f"[{tag}] 去重={'Y' if dedupe else 'N'} 去指数={'Y' if drop_index else 'N'}: n={len(syms)}")
        except Exception as e:  # noqa: BLE001
            results[tag] = None
            print(f"[{tag}] 失败: {type(e).__name__}: {e}")
        finally:
            l1_cohort.scan_enriched_parquet = _orig_scan

    b, c = results.get("B"), results.get("C")
    ok = True
    if b is None or c is None:
        print("!! B/C 未能产出名单，判据无法评估")
        return 1
    same_bc = sorted(b) == sorted(c)
    same_frozen = sorted(c) == sorted(frozen) if frozen else None
    print(f"\nB == C          : {same_bc}")
    print(f"C == 冻结 cohort: {same_frozen}  (冻结 n={len(frozen)})")
    if not same_bc:
        print("  B-C 差集:", sorted(set(b) ^ set(c)))
        ok = False
    if same_frozen is False:
        print("  C-冻结 差集:", sorted(set(c) ^ set(frozen)))
        ok = False
    print("\n结论:", "去重与去指数都不改变名单" if ok else "存在实质差异，需人工判断")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
