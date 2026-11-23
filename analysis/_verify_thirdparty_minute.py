"""第三方分钟数据的验收闸门：能不能用，用数字回答，不看口碑。

为什么必须先建这个
------------------
市面上能买到的沪深 5 分钟全历史（几十到几百元）几乎都是通达信/大智慧导出的转储。
它们共同的未知不是"准不准"，而是三件会**静默毁掉研究**的事：

  1. 价格口径：原始价 / 前复权 / 后复权，卖家通常不说；
  2. 成交量单位：股 / 手，差 100 倍且不会报错；
  3. **生存偏差**：转储的是"当前在册符号的历史"，已退市的代码可能整只不存在。
     前两项还能对账，第三项如果只看"数据很全"永远发现不了——而本任务的全部意义
     就是不含生存偏差。

四条判据（全部可复现）
--------------------
A. 重叠度：我们自己已下的标的里，第三方覆盖了多少；
B. 时间轴：同一 (symbol, datetime) 上 OHLC 的 max|Δ| 与 >0.001 的占比；
C. 单位：成交量比值中位数（≈1 = 手，≈100 = 股，≈0.01 = 反了）；
D. **退市股探针**：把 306 只缺口股代码丢进去，看它认几个。
   只认现存股的数据源，D 会是 0——不管 A/B/C 多漂亮。

用法
----
    python analysis/_verify_thirdparty_minute.py --src /data/wstock --ext .parquet
    python analysis/_verify_thirdparty_minute.py --src /data/x --ext .csv --csv-cols 日期,时间,开盘,最高,最低,收盘,成交量

第三方只需给出 symbol / datetime / OHLC / volume 五类字段；列名用 `--csv-cols` 指定顺序。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

ROOT = Path(r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
OURS = DATA / "backfill_bs_minute" / "f5_merged"

STD = ["symbol", "datetime", "open", "high", "low", "close", "volume"]


def gap_symbols() -> list[str]:
    p = DATA / "backfill_20261117/stock_gap_daily.parquet"
    if not p.exists():
        return []
    return sorted(pl.read_parquet(p, columns=["symbol"])["symbol"].unique().to_list())


def read_thirdparty(src: Path, ext: str, csv_cols: str | None, limit: int) -> pl.DataFrame:
    files = sorted(src.rglob(f"*{ext}"))[:limit]
    if not files:
        return pl.DataFrame()
    frames = []
    for f in files:
        try:
            if ext == ".csv":
                cols = [c.strip() for c in (csv_cols or "").split(",") if c.strip()]
                if len(cols) != 7:
                    raise SystemExit("--csv-cols 需要 7 列：symbol,datetime,open,high,low,close,volume")
                d = pl.read_csv(f, has_header=True, infer_schema_length=0)
                d = d.select([pl.col(c).alias(n) for c, n in zip(cols, STD)])
                d = d.with_columns(pl.col("symbol").cast(pl.String).str.replace_all(r"\.(XSH[EG]|SH|SZ)$", ""))
                # 代码补市场后缀：6/9 开头沪市，其余深市
                d = d.with_columns(
                    pl.when(pl.col("symbol").str.starts_with("6") | pl.col("symbol").str.starts_with("9"))
                      .then(pl.concat_str([pl.col("symbol"), pl.lit(".SH")]))
                      .otherwise(pl.concat_str([pl.col("symbol"), pl.lit(".SZ")])).alias("symbol"))
                d = d.with_columns(pl.col("datetime").str.replace(" ", "T").str.to_datetime(strict=False))
            else:
                d = pl.read_parquet(f)
                d = d.rename({c: c.lower() for c in d.columns})
                if "symbol" not in d.columns:
                    d = d.with_columns(pl.lit(f.stem, dtype=pl.String).alias("symbol"))
                d = d.select([c for c in STD if c in d.columns])
            for c in ("open", "high", "low", "close", "volume"):
                if c in d.columns:
                    d = d.with_columns(pl.col(c).cast(pl.Float64))
            frames.append(d.select([c for c in STD if c in d.columns]))
        except Exception as e:  # noqa: BLE001
            print(f"  跳过 {f.name}: {type(e).__name__} {str(e)[:60]}")
    return pl.concat(frames) if frames else pl.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--ext", default=".parquet", choices=[".parquet", ".csv"])
    ap.add_argument("--csv-cols", default=None)
    ap.add_argument("--limit", type=int, default=3000, help="最多读多少个文件（先小样本试）")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.exists():
        print(f"目录不存在 {src}")
        return 1
    third = read_thirdparty(src, a.ext, a.csv_cols, a.limit)
    if third.is_empty():
        print("没读到任何数据")
        return 1
    print(f"第三方：{third.height:,} 行 / {third['symbol'].n_unique()} 只 / "
          f"{third['datetime'].min()} ~ {third['datetime'].max()}")

    # D. 退市股探针（先做，因为它是否决项）
    gaps = set(gap_symbols())
    if gaps:
        hit = len(set(third["symbol"].unique().to_list()) & gaps)
        print(f"\n[D] 退市股探针：306 只缺口股里命中 {hit} 只 "
              f"({hit/len(gaps):.1%})")
        if hit == 0:
            print("  ⇒ **否决**：这个源没有退市股，买了也解决不了生存偏差")

    # A/B/C 对账
    ours_files = sorted(OURS.glob("*.parquet"))
    if not ours_files:
        print("本机 f5_merged 为空，跳过对账")
        return 0
    ours_syms = {p.stem for p in ours_files}
    third_syms = set(third["symbol"].unique().to_list())
    cover = ours_syms & third_syms
    print(f"[A] 重叠度：我们已下 {len(ours_syms)} 只，第三方覆盖 {len(cover)} 只 "
          f"({len(cover)/len(ours_syms):.1%})")
    if not cover:
        print("  无重叠，无法对账")
        return 0

    ours = pl.concat([pl.read_parquet(OURS / f"{s}.parquet") for s in sorted(cover)])
    j = ours.join(third, on=["symbol", "datetime"], suffix="_tp", how="inner")
    print(f"[B] 对齐 {j.height:,} 根（占我们这批的 {j.height/max(ours.height,1):.1%}）")
    for c in ("open", "high", "low", "close"):
        if f"{c}_tp" not in j.columns:
            continue
        d = (j[c] - j[f"{c}_tp"]).abs()
        print(f"    {c:<5} max|Δ|={d.max():.6f}  >0.001 占比={float((d > 0.001).mean()):.6f}")
    if "volume_tp" in j.columns:
        r = (j["volume"] / j["volume_tp"]).filter(j["volume_tp"] > 0)
        med = float(r.median())
        unit = "手(同口径)" if abs(med - 1) < 0.01 else ("股(差 100 倍)" if abs(med - 0.01) < 1e-4 else f"未知({med:.4f})")
        print(f"[C] 成交量比值 median={med:.4f} ⇒ {unit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
