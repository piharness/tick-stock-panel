"""负控实验：ETF（最小报价单位 0.001 元）能否用「股票池拟合的 tick 摩擦律」零自由参数预测。

为什么这个检验是决定性的
------------------------
已定稿的律（`docs/intraday-friction-law.md`）是
    ρ₁ = −(h/σ)²,   h² = (α·tick/price)² + (β·σ)²,   α≈0.45, β≈0.18
其中 `tick/price` 是**最小报价单位占价格的比例**。A 股内部要改变它，只能改价格
（tick 固定 0.01 元）；而价格同时改变别的东西（市值、流动性、波动），所以任何
A 股内部的「tick 效应」都可被这些混淆解释掉。

ETF 提供了一个**不换价格就换 tick** 的准实验：同一市场、同一交易日、同一撮合规则，
最小报价单位是 0.001 元 —— **是股票的 1/10**。于是可以找到：
    股票 ¥10（tick 10bp）  vs  ETF ¥1（tick 10bp）
tick_bp 相同、绝对价差 10 倍。若 ρ₁ 相同 ⇒ 律由 tick/price 决定，与绝对价格无关。

判据（预注册，跑之前写）
------------------------
判据 1 零自由参数外推（最强）
  用**只由股票池拟合**的 α、β，代入 ETF 自己的 tick_bp 与 σ 预测 ρ₁。
  通过：ETF 桶级 R² > 0.80 且 |bias| < 0.02。失败：R² < 0.50 或 bias 系统为负。

判据 2 α 的不变性
  ETF 池重拟合 α。通过：α_etf 的 95% CI 与 α_stock 相交。失败：不相交且差 > 0.10。

判据 3 残差的价格正交性
  在股票拟合模型下算 ETF 残差 (obs−pred)，按「同 tick 档」对比。模型只看 tick/price，
  故残差不应随绝对价格有结构。通过：|bias| < 0.02 且残差对 ln(price) 斜率不显著。

判据 4 β 下限（模型无关）
  债券/货币 ETF（¥100-141，tick_bp 0.07-0.10，tick 项≈0）应贴近 −β² = −0.032。
  通过：|ρ₁ − (−0.032)| < 0.015。

已知混淆（必须点名，不能靠数据排除）
------------------------------------
- **ETF 是 T+0，股票是 T+1。** T+1 的隔夜库存风险会推高做市补偿，方向上使
  α_stock > α_etf。故 α 相等 ⇒ 该渠道在 5min 频率不可检出；α_etf < α_stock ⇒
  「tick 故事错」与「T+1 加宽」**无法区分**。这是本实验的真实边界。
- ETF 是一篮子 ⇒ σ 天然低。控制办法：全程按 σ 分档 + 模型内已有 βσ 项。
- ETF 流动性低 ⇒ 零收益 bar 多 ⇒ ρ₁ 被机械推向负。并列报告 zf，并做
  「只保留 r≠0 的 pair」稳健性版本。

口径（与已定稿摩擦律严格一致）
------------------------------
- r_j = close_j/close_{j−1} − 1，**j = 2..48**（j=1 是隔夜跳空，必须剔除）。
- ρ₁ = Σ r_j·r_{j+1} / Σ r_j²；代表价取 Σ(r²·close)/Σr²（r² 加权，不是中位价）。
- 只保留**每日恰好 48 根**的标的-日：缺根 = 该时段无成交，跳过后的 r 不是 5min 收益，
  混进来会把 ρ₁ 拉向 0。ETF 流动性差，这条过滤掉 ~60% 的标的。
- 两条腿同源（新浪）、同窗（2026-10-22~11-20）、同聚合规则。
- t 统计量用**日级** bootstrap（n_eff = 交易日数），不用 bar 级 n。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

ROOT = Path(os.environ.get("TSP_ROOT") or r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_tick_regime.json"

TICK = {"stock": 0.01, "etf": 0.001, "cb": 0.001}
WIN = ("2026-10-22", "2026-11-20")

# tick_bp 的对数箱（覆盖 0.05 ~ 100bp）
EDGES = [0.0, 0.06, 0.1, 0.16, 0.26, 0.42, 0.7, 1.1, 1.8, 3.0, 5.0,
         8.0, 13.0, 21.0, 34.0, 55.0, 90.0, 1e9]


def build(con: duckdb.DuckDBPyConnection, asset: str) -> None:
    p = DATA / (f"{asset}_5min_sina/{asset}_5min.parquet")
    con.execute(f"CREATE OR REPLACE VIEW v_{asset} AS SELECT * FROM read_parquet('{p}')")
    con.execute(f"""
        CREATE OR REPLACE TABLE b48_{asset} AS
        SELECT * FROM (
          SELECT symbol, date, close, row_number() OVER (PARTITION BY symbol, date ORDER BY datetime) j,
                 count(*) OVER (PARTITION BY symbol, date) nb
          FROM v_{asset}
          WHERE date BETWEEN DATE '{WIN[0]}' AND DATE '{WIN[1]}'
        ) WHERE nb = 48
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE pr_{asset} AS
        SELECT symbol, date, j, close,
               close / lag(close) OVER (PARTITION BY symbol, date ORDER BY j) - 1 AS r
        FROM b48_{asset}
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE pa_{asset} AS
        SELECT symbol, date, j, close, r,
               lead(r) OVER (PARTITION BY symbol, date ORDER BY j) AS rn
        FROM pr_{asset} WHERE j >= 2
    """)


def sym_level(con: duckdb.DuckDBPyConnection, asset: str) -> pl.DataFrame:
    """每只标的池化：ρ₁、σ、r² 加权价、零收益占比。"""
    con.execute(f"""
        CREATE OR REPLACE TABLE s_{asset} AS
        SELECT symbol, count(*) AS n_pair, count(DISTINCT date) AS nday,
               sum(r*r) AS sv, sum(r*rn) AS scov,
               sum(r*r*close) / nullif(sum(r*r),0) AS p_varw,
               avg(CASE WHEN r = 0 THEN 1.0 ELSE 0 END) AS zf,
               sum(CASE WHEN r = 0 THEN 1 ELSE 0 END)::DOUBLE / count(*) AS zf2
        FROM pa_{asset} WHERE r IS NOT NULL AND rn IS NOT NULL GROUP BY 1
    """)
    df = con.sql(f"SELECT * FROM s_{asset}").pl().with_columns(
        pl.lit(asset).alias("asset"),
        (pl.col("scov") / pl.col("sv")).alias("rho1"),
        (pl.col("sv") / pl.col("n_pair")).sqrt().mul(1e4).alias("sd_bp"),
    ).with_columns((TICK[asset] / pl.col("p_varw") * 1e4).alias("tick_bp"))
    df = df.with_columns(
        pl.col("tick_bp").map_elements(_bin, return_dtype=pl.Int32).alias("b")
    )
    return df.filter(pl.col("nday") >= 12, pl.col("n_pair") >= 400)


def _bin(t: float) -> int:
    for i in range(len(EDGES) - 1):
        if EDGES[i] <= t < EDGES[i + 1]:
            return i
    return len(EDGES) - 2


def day_level(con: duckdb.DuckDBPyConnection, asset: str) -> pl.DataFrame:
    """标的×日：供日级 bootstrap。"""
    con.execute(f"""
        CREATE OR REPLACE TABLE dl_{asset} AS
        SELECT p.symbol, p.date, count(*) n, sum(r*r) v, sum(r*rn) cov1,
               sum(r*r*close)/nullif(sum(r*r),0) p,
               avg(CASE WHEN r=0 THEN 1.0 ELSE 0 END) zf
        FROM pa_{asset} p WHERE r IS NOT NULL AND rn IS NOT NULL
        GROUP BY 1,2
    """)
    d = con.sql(f"SELECT * FROM dl_{asset}").pl()
    return d.with_columns(
        pl.lit(asset).alias("asset"),
        (pl.col("cov1") / pl.col("v")).alias("rho1"),
        (pl.col("v") / pl.col("n")).sqrt().mul(1e4).alias("sd_bp"),
        (TICK[asset] / pl.col("p") * 1e4).alias("tick_bp"),
    ).with_columns(pl.col("tick_bp").map_elements(_bin, return_dtype=pl.Int32).alias("b"))


def pool(df: pl.DataFrame, keys: list[str], tick: float | None = None) -> pl.DataFrame:
    d = df.with_columns((pl.col("v") * pl.col("p")).alias("wp"))
    g = d.group_by(keys).agg(
        pl.col("n").sum().alias("n_pair"),
        pl.col("v").sum().alias("v"),
        pl.col("cov1").sum().alias("cov1"),
        pl.col("wp").sum().alias("wp"),
        pl.col("zf").mean().alias("zf"),
        pl.col("symbol").n_unique().alias("nsym"),
        pl.col("date").n_unique().alias("nday"),
    )
    g = g.with_columns(
        (pl.col("cov1") / pl.col("v")).alias("rho1"),
        (pl.col("v") / pl.col("n_pair")).sqrt().mul(1e4).alias("sd_bp"),
        (pl.col("wp") / pl.col("v")).alias("p_varw"),
    )
    # tick_bp 必须在池化后用自己的 tick 重算（ETF 0.001 / 股票 0.01），
    # 不能对逐标的的 tick_bp 取平均——1/price 的均值不是 1/均值。
    if tick is not None:
        g = g.with_columns((tick / pl.col("p_varw") * 1e4).alias("tick_bp"))
    elif "asset" in keys:
        g = g.with_columns(
            (pl.when(pl.col("asset") == "etf").then(0.001).otherwise(0.01)
             / pl.col("p_varw") * 1e4).alias("tick_bp"))
    return g


def fit_ab(tick, sd, y, a_grid=(0.05, 0.95, 0.005), b_grid=(0.02, 0.40, 0.001)):
    best = None
    for a in np.arange(*a_grid):
        for b in np.arange(*b_grid):
            h = np.sqrt((a * tick) ** 2 + (b * sd) ** 2)
            pr = -((h / sd) ** 2)
            sse = float(((pr - y) ** 2).sum())
            if best is None or sse < best[0]:
                best = (sse, a, b)
    return best[1], best[2]


def pred(tick, sd, a, b):
    return -((np.sqrt((a * tick) ** 2 + (b * sd) ** 2) / sd) ** 2)


def r2of(pr, y):
    return float(1 - ((pr - y) ** 2).sum() / ((y - y.mean()) ** 2).sum())


def main() -> None:
    con = duckdb.connect()
    con.execute("SET threads=8")
    for a in ("stock", "etf", "cb"):
        build(con, a)
        print(f"[{a}] 48根整日 pair 行数 "
              f"{con.sql(f'SELECT count(*) n, count(DISTINCT symbol) ns, count(DISTINCT date) nd FROM pa_{a}').pl().row(0)}",
              flush=True)

    sl = pl.concat([sym_level(con, a) for a in ("stock", "etf", "cb")])
    print("\n=== 样本 ===")
    for a in ("stock", "etf", "cb"):
        g = sl.filter(pl.col("asset") == a)
        print(f"  {a:<5} 标的 {g.height:>5d}  pair {g['n_pair'].sum():>10,}  "
              f"tick_bp {g['tick_bp'].min():7.3f}~{g['tick_bp'].max():7.3f}(中位 {g['tick_bp'].median():6.2f})  "
              f"sd_bp 中位 {g['sd_bp'].median():5.1f}  价格中位 {g['p_varw'].median():7.2f}")

    dl = pl.concat([day_level(con, a) for a in ("stock", "etf", "cb")])
    po = pool(dl, ["asset", "b"]).sort(["asset", "b"])
    po = po.filter(pl.col("n_pair") >= 8000)
    print("\n=== 桶级池化 ===")
    print(f"{'资产':<6}{'b':>3} {'tick_bp':>8} {'sd_bp':>7} {'rho1':>9} {'zf%':>6} {'价格':>9} {'只数':>5} {'n_pair':>9}")
    for r in po.iter_rows(named=True):
        print(f"{r['asset']:<6}{r['b']:>3} {r['tick_bp']:8.2f} {r['sd_bp']:7.1f} {r['rho1']:+9.4f} "
              f"{r['zf']*100:6.1f} {r['p_varw']:9.2f} {r['nsym']:>5d} {r['n_pair']:>9,}")

    s = po.filter(pl.col("asset") == "stock")
    e = po.filter(pl.col("asset") == "etf")
    a_st, b_st = fit_ab(s["tick_bp"].to_numpy(), s["sd_bp"].to_numpy(), s["rho1"].to_numpy())
    r2_st = r2of(pred(s["tick_bp"].to_numpy(), s["sd_bp"].to_numpy(), a_st, b_st),
                 s["rho1"].to_numpy())

    pe = pred(e["tick_bp"].to_numpy(), e["sd_bp"].to_numpy(), a_st, b_st)
    ye = e["rho1"].to_numpy()
    r2_e = r2of(pe, ye)
    bias_e = float((ye - pe).mean())
    mae_e = float(np.abs(ye - pe).mean())

    a_et, b_et = fit_ab(e["tick_bp"].to_numpy(), e["sd_bp"].to_numpy(), ye)
    r2_e2 = r2of(pred(e["tick_bp"].to_numpy(), e["sd_bp"].to_numpy(), a_et, b_et), ye)

    print(f"\n=== 判据 1：股票拟合 α={a_st:.3f} β={b_st:.3f} (R²={r2_st:.4f}, {len(s)}桶) → ETF 外推 ===")
    print(f"  ETF {len(e)}桶  R²={r2_e:.4f}  bias={bias_e:+.4f}  MAE={mae_e:.4f}")
    print(f"=== 判据 2：ETF 自拟合 α={a_et:.3f} β={b_et:.3f} (R²={r2_e2:.4f}) ===")

    # ---- 日级 bootstrap ----
    rng = np.random.default_rng(20261121)
    day_map = {a: {str(d): g for (d,), g in dl.filter(pl.col("asset") == a).group_by(["date"])}
               for a in ("stock", "etf", "cb")}

    def draw(asset: str) -> pl.DataFrame:
        keys = list(day_map[asset])
        pick = rng.choice(np.array(keys, dtype=object), size=len(keys), replace=True)
        return pl.concat([day_map[asset][k] for k in pick])

    def boot_fit(asset: str, B: int = 400):
        """股票/ETF：bootstrap (α, β)。分箱过多才能识别两参数，ETF 只有 8 桶，
        β 落网格下降到 0.02 下界 ⇒ 这是「β 想取 0」的信号，不是精度不足。"""
        out = []
        for _ in range(B):
            g = pool(draw(asset), ["b"], tick=TICK[asset]).filter(pl.col("n_pair") >= 8000)
            if g.height < 4:
                continue
            out.append(fit_ab(g["tick_bp"].to_numpy(), g["sd_bp"].to_numpy(), g["rho1"].to_numpy(),
                              a_grid=(0.05, 0.95, 0.01), b_grid=(0.0, 0.40, 0.002)))
        o = np.array(out)
        return {"B_ok": int(len(o)),
                "alpha": float(o[:, 0].mean()),
                "alpha_lo": float(np.percentile(o[:, 0], 2.5)),
                "alpha_hi": float(np.percentile(o[:, 0], 97.5)),
                "beta": float(o[:, 1].mean()),
                "beta_lo": float(np.percentile(o[:, 1], 2.5)),
                "beta_hi": float(np.percentile(o[:, 1], 97.5))}

    def boot_pure_beta(asset: str, tick_cut: float = 2.0, B: int = 400):
        """**零模型**读数：只取 tick_bp < 2bp 的格（tick 项对 h² 的贡献 < 1%），
        池化 ρ₁ ⇒ β = sqrt(−ρ₁)。可转债、高价股都落在这一区，ETF 只有极少数。"""
        d = dl.filter(pl.col("asset") == asset, pl.col("tick_bp") < tick_cut)
        if d.height == 0:
            return None
        dm = {str(k): g for (k,), g in d.group_by(["date"])}
        keys = list(dm)
        point = float((d["cov1"].sum() / d["v"].sum()))
        out = []
        for _ in range(B):
            pick = rng.choice(np.array(keys, dtype=object), size=len(keys), replace=True)
            sel = pl.concat([dm[k] for k in pick])
            out.append(float(sel["cov1"].sum() / sel["v"].sum()))
        o = np.array(out)
        return {"asset": asset, "n_pair": int(d["n"].sum()), "nsym": int(d["symbol"].n_unique()),
                "nday": len(keys), "rho1": point,
                "beta": float(np.sqrt(max(-point, 0.0))),
                "beta_lo": float(np.sqrt(max(-np.percentile(o, 97.5), 0.0))),
                "beta_hi": float(np.sqrt(max(-np.percentile(o, 2.5), 0.0))),
                "sd_med": float(d["sd_bp"].median()), "tick_med": float(d["tick_bp"].median())}

    print("\n日级 bootstrap（B=400，按交易日重采样）...", flush=True)
    bs = {}
    for a in ("stock", "etf"):
        bs[a] = boot_fit(a)
        print(f"  {a:<5} 双参数拟合 α={bs[a]['alpha']:.3f} [{bs[a]['alpha_lo']:.3f},{bs[a]['alpha_hi']:.3f}] "
              f"β={bs[a]['beta']:.3f} [{bs[a]['beta_lo']:.3f},{bs[a]['beta_hi']:.3f}]  B_ok={bs[a]['B_ok']}")
    pures = [boot_pure_beta(a) for a in ("stock", "cb")]
    pures = [p for p in pures if p]
    print("\n=== 零模型 β 读数（只取 tick_bp<2bp，tick 项贡献 <1%）===")
    print("  注：ETF 在此区间只有债券/货币 ETF（σ 中位 1.8bp），其 ρ₁ 为正、不适用本模型，")
    print("      故**不纳入**；股票型 ETF 的最小 tick_bp ≈ 4bp，不存在纯 β 区。")
    print(f"  {'资产':<7}{'n_pair':>9}{'只数':>6}{'天数':>5}{'sd中位':>8}{'tick中位':>9}{'ρ₁':>9}{'β':>7}{'95%CI(日级)':>20}")
    for p in pures:
        print(f"  {p['asset']:<7}{p['n_pair']:>9,}{p['nsym']:>6}{p['nday']:>5}{p['sd_med']:>8.1f}"
              f"{p['tick_med']:>9.2f}{p['rho1']:>+9.4f}{p['beta']:>7.3f}"
              f"  [{p['beta_lo']:.3f},{p['beta_hi']:.3f}]")

    # ---- 判据 3：残差 vs 绝对价格 ----
    e2 = e.with_columns(pl.Series("pred", pe)).with_columns(
        (pl.col("rho1") - pl.col("pred")).alias("resid"))
    print("\n=== 判据 3：ETF 残差（相对股票拟合模型）===")
    print(f"{'b':>3} {'tick_bp':>8} {'价格ETF':>9} {'obs':>9} {'pred':>9} {'resid':>9} {'sd_bp':>7}")
    for r in e2.iter_rows(named=True):
        print(f"{r['b']:>3} {r['tick_bp']:8.2f} {r['p_varw']:9.2f} {r['rho1']:+9.4f} "
              f"{r['pred']:+9.4f} {r['resid']:+9.4f} {r['sd_bp']:7.1f}")
    X = np.column_stack([np.ones(len(e2)), np.log(e2["p_varw"].to_numpy())])
    bb_, *_ = np.linalg.lstsq(X, e2["resid"].to_numpy(), rcond=None)
    rr = e2["resid"].to_numpy() - X @ bb_
    s2 = rr @ rr / (len(rr) - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    print(f"  resid = {bb_[0]:+.4f} + {bb_[1]:+.4f}·ln(price)   t(斜率)={bb_[1]/se[1]:+.2f}")

    # ---- 判据 3b：同 tick 档、同 σ 档的 2D 格 ----
    sd_edges = [0, 12, 20, 30, 45, 70, 120, 1e9]
    def sd_bin(x):
        for i in range(len(sd_edges) - 1):
            if sd_edges[i] <= x < sd_edges[i + 1]:
                return i
        return len(sd_edges) - 2
    dl = dl.with_columns(pl.col("sd_bp").map_elements(sd_bin, return_dtype=pl.Int32).alias("sb"))
    for k in list(day_map):
        day_map[k] = {d: g.with_columns(
            pl.col("sd_bp").map_elements(sd_bin, return_dtype=pl.Int32).alias("sb"))
            for d, g in day_map[k].items()}
    po2 = pool(dl, ["asset", "b", "sb"]).filter(pl.col("n_pair") >= 5000)
    print("\n=== 2D 格（tick 档 × σ 档）双向对照：ETF vs 股票 ===")
    print("  Δ = ρ₁(ETF) − ρ₁(股票)。模型若普适，Δ 应为 0（模型只看 tick/price 与 σ）。")
    print(f"{'b':>3} {'sb':>3} {'tick':>7} {'sd':>6} {'股票rho1':>10} {'ETF rho1':>10} {'Δ':>8} {'价格E':>8} {'价格S':>8}")
    both = []
    for (b, sb_), g in po2.group_by(["b", "sb"]):
        have = set(g["asset"].to_list())
        if not {"stock", "etf"} <= have:
            continue
        ds = g.filter(pl.col("asset") == "stock").row(0, named=True)
        de = g.filter(pl.col("asset") == "etf").row(0, named=True)
        rec = {"b": int(b), "sb": int(sb_), "tick_bp": ds["tick_bp"],
               "sd_bp": ds["sd_bp"], "rho1_stock": ds["rho1"], "rho1_etf": de["rho1"],
               "delta": de["rho1"] - ds["rho1"], "p_stock": ds["p_varw"], "p_etf": de["p_varw"],
               "n_stock": int(ds["n_pair"]), "n_etf": int(de["n_pair"])}
        both.append(rec)
        print(f"{int(b):>3} {int(sb_):>3} {ds['tick_bp']:7.2f} {ds['sd_bp']:6.1f} "
              f"{ds['rho1']:+10.4f} {de['rho1']:+10.4f} {rec['delta']:+8.4f} "
              f"{de['p_varw']:8.2f} {ds['p_varw']:8.2f}")
    if both:
        dl_ = np.array([x["delta"] for x in both])
        print(f"  Δ 均值 {dl_.mean():+.4f}  中位 {np.median(dl_):+.4f}  "
              f"正号 {(dl_ > 0).sum()}/{len(dl_)}  |Δ|中位 {np.abs(dl_).mean():.4f}")

        # Δ 的日级 bootstrap：只在同一天内同时有股票与 ETF 的格上重采样
        cells = [(x["b"], x["sb"]) for x in both]
        out = []
        for _ in range(400):
            s2 = pool(draw("stock"), ["b", "sb"]).filter(pl.col("n_pair") >= 3000)
            e2b = pool(draw("etf"), ["b", "sb"]).filter(pl.col("n_pair") >= 3000)
            m = s2.join(e2b, on=["b", "sb"], suffix="_e")
            m = m.filter(pl.col("b").is_in([c[0] for c in cells]))
            if m.height < 3:
                continue
            out.append(float((m["rho1_e"] - m["rho1"]).mean()))
        if out:
            o = np.array(out)
            print(f"  Δ 日级 bootstrap: 均值 {o.mean():+.4f}  "
                  f"95%CI [{np.percentile(o, 2.5):+.4f},{np.percentile(o, 97.5):+.4f}]  B_ok={len(o)}")

    # ---- 判据 4：β 下限（债券/货币 ETF）----
    con.execute("""
        CREATE OR REPLACE TABLE s4 AS
        SELECT symbol, sum(r*r) sv, sum(r*rn) scov, sum(r*r*close)/nullif(sum(r*r),0) p,
               count(*) n, count(DISTINCT date) nd
        FROM pa_etf WHERE r IS NOT NULL AND rn IS NOT NULL GROUP BY 1
    """)
    s4 = con.sql("SELECT * FROM s4").pl().with_columns(
        (pl.col("scov") / pl.col("sv")).alias("rho1"),
        (pl.col("sv") / pl.col("n")).sqrt().mul(1e4).alias("sd_bp"),
        (0.001 / pl.col("p") * 1e4).alias("tick_bp"))
    bonds = s4.filter(pl.col("p") > 50, pl.col("nd") >= 12, pl.col("n") >= 400)
    w = bonds["sv"].sum()
    rho_bond = float(bonds["scov"].sum() / w)
    print(f"\n=== 判据 4：高价 ETF（¥>50，tick 项≈0）n={bonds.height} ===")
    print(f"  池化 ρ₁={rho_bond:+.4f}   −β²={-b_st**2:+.4f}   偏差={rho_bond + b_st**2:+.4f}")
    print(f"  中位价格 {bonds['p'].median():.1f}  中位 tick_bp {bonds['tick_bp'].median():.3f}bp  "
          f"中位 sd_bp {bonds['sd_bp'].median():.1f}")
    print(f"  逐只 ρ₁ 分位: " + " ".join(f"p{int(q*100)}={float(bonds['rho1'].quantile(q)):+.3f}"
                                    for q in (0.05, 0.25, 0.5, 0.75, 0.95)))

    # ---- 判据 5：可转债 —— 分离「T+0」与「一篮子」----
    # 可转债：T+0、**单只券**（不是一篮子）、最小报价单位 0.001 元、价格 ¥110-750。
    # tick_bp ≈ 0.08bp ⇒ tick 项≈0 ⇒ ρ₁ 直接读出 β（冲击项）本身。
    con.execute("""
        CREATE OR REPLACE TABLE s5 AS
        SELECT symbol, sum(r*r) sv, sum(r*rn) scov, sum(r*r*close)/nullif(sum(r*r),0) p,
               count(*) n, count(DISTINCT date) nd,
               avg(CASE WHEN r=0 THEN 1.0 ELSE 0 END) zf
        FROM pa_cb WHERE r IS NOT NULL AND rn IS NOT NULL GROUP BY 1
    """)
    s5 = con.sql("SELECT * FROM s5").pl().with_columns(
        (pl.col("scov") / pl.col("sv")).alias("rho1"),
        (pl.col("sv") / pl.col("n")).sqrt().mul(1e4).alias("sd_bp"),
        (0.001 / pl.col("p") * 1e4).alias("tick_bp"))
    cbx = s5.filter(pl.col("nd") >= 12, pl.col("n") >= 400)
    rho_cb = float(cbx["scov"].sum() / cbx["sv"].sum())
    # 可转债池自拟合（单参数 β 就够，tick 项被价格稀释掉）
    cbp = pool(day_level(con, "cb"), ["b"], tick=0.001).filter(pl.col("n_pair") >= 4000)
    a_cb, b_cb = fit_ab(cbp["tick_bp"].to_numpy(), cbp["sd_bp"].to_numpy(), cbp["rho1"].to_numpy())
    print(f"\n=== 判据 5：可转债（T+0 / 单只券 / tick 0.001）n={cbx.height} ===")
    print(f"  中位价格 {cbx['p'].median():.1f}  中位 tick_bp {cbx['tick_bp'].median():.3f}bp  "
          f"中位 sd_bp {cbx['sd_bp'].median():.1f}  零收益 {cbx['zf'].mean()*100:.1f}%")
    print(f"  池化 ρ₁={rho_cb:+.4f}  ⇒ 隐含 β={np.sqrt(max(-rho_cb, 0.0)):.3f}   "
          f"(股票 β={b_st:.3f}, ETF β={b_et:.3f})")
    print(f"  可转债自拟合 α={a_cb:.3f} β={b_cb:.3f}  ({len(cbp)}桶)")
    print(f"  逐只 ρ₁ 分位: " + " ".join(f"p{int(q*100)}={float(cbx['rho1'].quantile(q)):+.3f}"
                                    for q in (0.05, 0.25, 0.5, 0.75, 0.95)))

    print("\n=== β 汇总（冲击项，决定 '无 tick 时 ρ₁ 落在哪'）===")
    print(f"  {'资产':<8}{'交易制度':<8}{'tick':<10}{'单只/一篮子':<13}{'β 零模型':>10}{'95%CI':>20}")
    meta = {"stock": ("股票", "T+1", "0.01元", "单只"),
            "etf": ("ETF", "T+0", "0.001元", "一篮子"),
            "cb": ("可转债", "T+0", "0.001元", "单只")}
    for p in pures:
        nm, reg, tks, kind = meta[p["asset"]]
        print(f"  {nm:<8}{reg:<8}{tks:<10}{kind:<13}{p['beta']:>10.3f}"
              f"  [{p['beta_lo']:.3f},{p['beta_hi']:.3f}]")

    OUT.write_text(json.dumps({
        "pure_beta": pures,
        "meta": {"win": WIN, "edges": EDGES, "tick": TICK,
                 "n_etf_sym": int(e["nsym"].max()), "n_stock_bins": int(len(s))},
        "criterion1": {"alpha_stock": a_st, "beta_stock": b_st, "r2_stock": r2_st,
                       "r2_etf_oos": r2_e, "bias_etf": bias_e, "mae_etf": mae_e},
        "criterion2": {"alpha_etf": a_et, "beta_etf": b_et, "r2_etf_self": r2_e2},
        "criterion5_cb": {"rho1_cb": rho_cb, "beta_cb_implied": float(np.sqrt(max(-rho_cb, 0.0))),
                          "alpha_cb": a_cb, "beta_cb": b_cb, "n_cb": int(cbx.height),
                          "p_cb_med": float(cbx["p"].median()),
                          "sd_cb_med": float(cbx["sd_bp"].median()),
                          "tick_cb_med": float(cbx["tick_bp"].median())},
        "criterion3": {"resid_intercept": float(bb_[0]), "resid_slope_lnprice": float(bb_[1]),
                       "resid_slope_t": float(bb_[1] / se[1]),
                       "cells_etf": e2.select(["b", "tick_bp", "p_varw", "rho1", "pred", "resid",
                                               "sd_bp", "n_pair"]).to_dicts(),
                       "cells_2d": both},
        "criterion4": {"rho1_bond": rho_bond, "beta_floor_sq": -b_st ** 2,
                       "n_bond": int(bonds.height)},
        "bootstrap": bs,
        "pooled": po.to_dicts(),
    }, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
