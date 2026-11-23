"""ROE 行业拐点 → 行业 ETF 轮动：行业聚合能不能救活公司层面的弱信号？

上游结论（`_diag_roe_inflection.py` / `docs/roe-inflection.md`）：
  公司层面 拐点可观测（毛利率 t−4 分位 42.8→57.9），但不可交易（多空 +2.59%/250日 t=1.16）；
  行业层面 7 个特征对次年行业收益 IC 全部 |t|<2。

本轮只回答一件事：**把 90 个行业当截面、买「拐点向上」的行业，能不能把 t=1.16 推到 2？**
判据（预登记，不得事后改）：主口径 t>=2 且 两个安慰剂残差同时为正。

四道口径门（不过就是假数据）：
1. 入场必须 PIT：年报法定截止 4/30 ⇒ 5 月建仓，持有 12 个月（非重叠，N=年数）。
   季度重叠样本会把 t 放大 sqrt(4) 倍（本仓母题）。
2. 行业 ROE 必须**加总后再相除**（Σ归母净利TTM / Σ平均净资产）；`d_roe` 用**年差=1**重算，
   不用 `shift(4)` 数行——行业-季有缺口时 shift 会串期。
3. 收益必须「日历相邻 + 复权价」：`kline_daily_enriched.close` 已复权（000001 除权日 0.9654
   vs 原始 0.740）；缺口股只有原始价 ⇒ |日收益|>25% 按拆分裂口置 0。ETF 同理（1:2 拆分
   = 当日 −0.50，共 100+ 例），但 ETF 分红**未还原**（见 caveat）。
4. 行业标签是 2026-11 快照、非时点；ETF 池 1,720 只**全部活到 2026-11**（零已清算）⇒
   车辆层面 100% 生存偏差。本轮任何正结果只能当**上界**。

三个信号（预登记 S2 为主口径）：
  S1 d_roe : 行业 ROE 同比变化（G 段直出，水平）
  S2 accel : d_roe − d_roe(去年)   ← 「拐点强度」，二阶信号，本轮主口径
  S3 turn  : d_roe>0 且 去年 d_roe<=0（离散拐点），组合等权

五个检验层（只换资产的颗粒度, 其余机器完全相同）：
  F   5,842 只公司 × EW           2005-2025  N=21  ← 基准: 信号在公司层面值多少
  A   90 个原子行业 × EW           2005-2025  N=21  ← 聚合第一步
  Ap  29 个 ETF 篮子 × EW          2005-2025  N=21  ← 可交易子集
  App 同上 × 市值加权                            ← 与 ETF 权重口径对齐
  B   29 个篮子 × 真实 ETF        2020-2025  N=6   ← 可实施性（成本/流动性/存续）

结论（本轮实测）: t 单调下降 1.95 → 0.24 → −0.38 → 0.55 → −1.69。
  accel 只有 8% 的方差在行业间 ⇒ 聚合把信噪比打到 8 折以下, 不是「降噪」而是「抹掉」。

运行:
    python analysis/_diag_roe_industry_rotation.py            # 只读
    python analysis/_diag_roe_industry_rotation.py --write    # 落 json
    python analysis/_diag_roe_industry_rotation.py --check    # 与 json 比对, 漂移 >5% 退出码 1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
import _diag_roe_inflection as RI  # noqa: E402  信号定义只允许有一个来源

BF = ROOT / "data" / "backfill_20261117"
STOCK_K = ROOT / "data" / "kline_daily_enriched"
GAP = BF / "stock_gap_daily.parquet"
ETF_PX = BF / "etf_daily_full.parquet"
IND = ROOT / "data" / "ext_data" / "ext_hy_ths" / "part.parquet"
ETF_INST = ROOT / "data" / "instruments_etf" / "instruments_etf.parquet"
SHARES = BF / "fin_shares.parquet"
OUT = ROOT / "analysis" / "_diag_roe_industry_rotation_result.json"

ENTRY_MONTH = 5          # 4/30 年报截止 ⇒ 5 月建仓
HOLD_MONTHS = 12         # 非重叠, 不放大 t
TOPK_Q = 0.2             # 多头 = 截面最高 20%
MIN_AMT20 = 2e7          # ETF 建仓前 20 日均成交额 ≥ 2000 万元
MIN_LISTED = 250         # ETF 建仓前至少 250 个交易日（≈1 年）
MIN_STOCK_LISTED = 60    # 个股上市满 60 个交易日后才计入行业组合
MIN_FIDELITY = 0.70      # ETF 与所属篮子的月收益相关性下限（低于此值说明名字对了、敞口不对）
COST_BPS_SIDE = {"etf": 7.5, "stock": 25.0}
SEED = 11
N_DRAW = 1000

# ETF→行业：显式篮子，不做模糊匹配。members = 该 ETF 跟踪指数覆盖的原子行业（THS 标签）。
# pat/neg 只用于在 1,720 只名字里挑候选；命中后还要过「收益相关性验证」（audit.etf_map）。
BASKETS: list[dict] = [
    {"name": "银行", "members": ["银行"], "pat": r"银行", "neg": r"金融地产"},
    {"name": "证券", "members": ["证券"], "pat": r"证券ETF|券商ETF|上证券商",
     "neg": r"保险|香港|金融地产|中银证券"},
    {"name": "白酒", "members": ["白酒"], "pat": r"酒ETF", "neg": None},
    {"name": "家电", "members": ["白色家电", "厨卫电器", "小家电", "黑色家电"],
     "pat": r"家电", "neg": r"港股"},
    {"name": "医药", "members": ["化学制药", "中药", "生物制品", "医药商业"],
     "pat": r"医药ETF|生物医药ETF", "neg": r"港股|创业板|医疗|创新药"},
    {"name": "医疗", "members": ["医疗器械", "医疗服务"], "pat": r"医疗ETF|医疗器械ETF",
     "neg": r"港股|创新药|医药"},
    {"name": "半导体", "members": ["半导体"], "pat": r"半导体ETF|芯片ETF",
     "neg": r"设备|港股|中韩|龙头|设计"},
    {"name": "消费电子", "members": ["消费电子"], "pat": r"消费电子ETF", "neg": r"港股"},
    {"name": "计算机", "members": ["软件开发", "IT服务", "计算机设备"], "pat": r"计算机ETF",
     "neg": r"港股"},
    {"name": "通信", "members": ["通信设备", "通信服务"], "pat": r"通信ETF|5GETF",
     "neg": r"港股|信息技术"},
    {"name": "传媒", "members": ["文化传媒", "游戏", "影视院线"], "pat": r"传媒ETF",
     "neg": r"游戏传媒|港股"},
    {"name": "电力", "members": ["电力"], "pat": r"电力ETF", "neg": r"绿色|绿电|港股"},
    {"name": "煤炭", "members": ["煤炭开采加工"], "pat": r"煤炭ETF", "neg": None},
    # 石油ETF 与 油气ETF 的收益相关性互相 0.8+, 且 561360 与「油气开采」比与「石油加工贸易」更近
    # ⇒ 合并成一个篮子, 不给同一敞口两个投票权
    {"name": "石油石化", "members": ["石油加工贸易", "油气开采及服务"],
     "pat": r"石油ETF|石化ETF|油气ETF", "neg": r"标普|港股"},
    {"name": "钢铁", "members": ["钢铁"], "pat": r"钢铁ETF", "neg": None},
    {"name": "有色", "members": ["工业金属", "小金属", "贵金属", "金属新材料", "能源金属"],
     "pat": r"有色金属ETF|有色ETF|有色嘉实", "neg": r"工业有色|稀土|港股|矿业"},
    {"name": "化工", "members": ["化学制品", "化学原料", "化学纤维", "农化制品",
                                "塑料制品", "橡胶制品"],
     "pat": r"化工ETF|化工南方", "neg": r"能源化工|港股"},
    {"name": "建材", "members": ["建筑材料"], "pat": r"建材ETF", "neg": None},
    {"name": "建筑", "members": ["建筑装饰"], "pat": r"基建ETF", "neg": None},
    {"name": "房地产", "members": ["房地产"], "pat": r"房地产ETF|地产ETF",
     "neg": r"金融地产|港股"},
    {"name": "工程机械", "members": ["工程机械"], "pat": r"工程机械ETF", "neg": None},
    {"name": "汽车", "members": ["汽车整车", "汽车零部件", "汽车服务及其他"],
     "pat": r"汽车ETF", "neg": r"港股|新能源|智能|零部件"},
    {"name": "电池", "members": ["电池"], "pat": r"电池ETF|锂电池ETF",
     "neg": r"储能|港股|创业板"},
    {"name": "光伏", "members": ["光伏设备"], "pat": r"光伏ETF", "neg": r"港股|龙头"},
    {"name": "电网设备", "members": ["电网设备"], "pat": r"电网设备ETF", "neg": None},
    {"name": "军工", "members": ["军工装备", "军工电子"], "pat": r"军工ETF|国防ETF",
     "neg": r"龙头|航天|卫星|航空"},
    {"name": "交运", "members": ["公路铁路运输", "港口航运", "机场航运", "物流"],
     "pat": r"交运ETF|交通运输ETF", "neg": r"港股"},
    {"name": "农业", "members": ["种植业与林业", "养殖业", "农产品加工"],
     "pat": r"农业ETF", "neg": r"港股|养殖"},
    {"name": "旅游", "members": ["旅游及酒店"], "pat": r"旅游ETF", "neg": r"港股"},
]
# 剔除（相关性验证抓到的错配, 见 audit.etf_validation）:
#   环保ETF 512580 —— 跟踪中证环保产业, 成分是宁德/阳光等新能源, 与环境治理相关性 0.58
#   教育ETF 513360 —— 港股/美股中概教育 QDII, 与 A 股教育行业不是同一敞口
# 保留条件: 车辆与自己篮子的月收益相关性 ≥ MIN_FIDELITY（结构属性, 不按收益选）


# ---------------------------------------------------------------- 统计工具
def tstat(a) -> float:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if a.size < 3:
        return float("nan")
    return float(a.mean() / (a.std(ddof=1) / math.sqrt(a.size)))


def nw_t(x, lag: int = 12) -> float:
    """Newey-West t: 12 个月持有期的月频序列必然自相关, 裸 t 会虚高。"""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 24:
        return float("nan")
    e = x - x.mean()
    s = float(e @ e) / n
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1)
        s += 2.0 * w * float(e[k:] @ e[:-k]) / n
    return float(x.mean() / math.sqrt(s / n)) if s > 0 else float("nan")


def boot_ci(a, n_boot: int = 2000, seed: int = SEED) -> list[float]:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if a.size < 4:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, a.size, size=(n_boot, a.size))
    m = a[idx].mean(axis=1)
    return [round(float(np.percentile(m, 2.5)), 2), round(float(np.percentile(m, 97.5)), 2)]


def _rank(a: np.ndarray) -> np.ndarray:
    o = np.argsort(np.asarray(a, dtype=float), kind="mergesort")
    r = np.empty(len(a), dtype=float)
    r[o] = np.arange(len(a))
    return r


def _z(a) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    sd = a.std()
    return (a - a.mean()) / sd if sd > 0 else np.zeros_like(a)


def mde(sd: float, n: int, t: float = 2.0) -> float:
    """最小可检测效应: |t|=2 对应的均值。用来说明「不显著」是没效应还是没功效。"""
    return float(t * sd / math.sqrt(max(n, 1)))


# ---------------------------------------------------------------- 行情 → 月频
def stock_monthly() -> tuple[pl.DataFrame, pl.DataFrame]:
    """个股月收益 + 月末市值。返回 (m, symbol, ret, rn_end), (m, symbol, w[上月末])。"""
    rich = (
        pl.scan_parquet(STOCK_K / "**" / "*.parquet")
        .select(["symbol", "date", pl.col("close").alias("px")])
        .filter(pl.col("date") >= pl.lit("2004-06-01").str.to_date())
        .collect()
        .with_columns(pl.lit(0, dtype=pl.Int8).alias("src"))      # 0 = 复权价
    )
    gap = (
        pl.read_parquet(GAP, columns=["symbol", "date", "close"])
        .rename({"close": "px"})
        .filter(pl.col("date") >= pl.lit("2004-06-01").str.to_date())
        .with_columns(pl.lit(1, dtype=pl.Int8).alias("src"))      # 1 = 原始价
    )
    px = (
        pl.concat([rich, gap], how="vertical_relaxed")
        .unique(subset=["symbol", "date"], keep="first")           # 分区优先, 缺口股只补缺口
        .sort(["symbol", "date"])
        .with_columns(pl.int_range(0, pl.len()).over("symbol").alias("rn"))
        .with_columns([
            pl.col("px").shift(1).over("symbol").alias("px_prev"),
            pl.col("src").shift(1).over("symbol").alias("src_prev"),
        ])
        .with_columns(
            pl.when(pl.col("px_prev").is_null() | (pl.col("px_prev") <= 0) | (pl.col("px") <= 0))
            .then(None).otherwise(pl.col("px") / pl.col("px_prev") - 1.0).alias("r")
        )
    )
    # 原始价（缺口股）|r|>25% ⇒ 拆分裂口, 置 0; 复权价保留真实涨跌（含退市整理期 −90%）
    px = px.with_columns(
        pl.when(pl.col("src_prev") == 1)
        .then(pl.when(pl.col("r").abs() > 0.25).then(0.0).otherwise(pl.col("r")))
        .otherwise(pl.col("r")).alias("r")
    )
    px = px.with_columns(pl.col("date").dt.truncate("1mo").alias("m"))
    ret = (
        px.filter(pl.col("r").is_not_null())
        .group_by(["m", "symbol"])
        .agg([(pl.col("r") + 1.0).product().alias("r1"), pl.col("rn").max().alias("rn_end")])
        .with_columns((pl.col("r1") - 1.0).alias("ret"))
        .filter(pl.col("rn_end") >= MIN_STOCK_LISTED)             # 上市未满 60 交易日剔除
        .select(["m", "symbol", "ret", "rn_end"])
    )
    me = px.group_by(["m", "symbol"]).agg(pl.col("px").last().alias("px_end"))
    sh = (
        pl.read_parquet(SHARES, columns=["symbol", "end_date", "total_shares"])
        .filter((pl.col("total_shares") > 0) & pl.col("end_date").is_not_null())
        .sort(["symbol", "end_date"])
    )
    mc = (
        me.sort(["symbol", "m"])
        .join_asof(sh, left_on="m", right_on="end_date", by="symbol", strategy="backward")
        .with_columns((pl.col("px_end") * pl.col("total_shares")).alias("mcap"))
        .sort(["symbol", "m"])
        .with_columns(pl.col("mcap").shift(1).over("symbol").alias("w"))
        .select(["m", "symbol", "w"])
        .filter(pl.col("w").is_not_null() & (pl.col("w") > 0))
    )
    return ret, mc


def panel_returns(ret: pl.DataFrame, w: pl.DataFrame, ind: pl.DataFrame,
                  key: str = "asset") -> pl.DataFrame:
    """(m, asset) 的行业组合月收益: 等权 + 市值加权。"""
    r = ret.join(ind.select(["symbol", key]), on="symbol", how="inner")
    ew = r.group_by(["m", key]).agg([pl.col("ret").mean().alias("ew"),
                                     pl.len().alias("n_member")])
    rw = r.join(w, on=["m", "symbol"], how="inner").with_columns(
        (pl.col("ret") * pl.col("w")).alias("rw"))
    cw = (
        rw.group_by(["m", key])
        .agg([pl.col("rw").sum().alias("num"), pl.col("w").sum().alias("den")])
        .with_columns((pl.col("num") / pl.col("den")).alias("cw"))
        .select(["m", key, "cw"])
    )
    return ew.join(cw, on=["m", key], how="left")


def etf_monthly() -> tuple[pl.DataFrame, pl.DataFrame]:
    """ETF 月收益（拆分裂口置 0）+ 月末 20 日均成交额。"""
    e = (
        pl.read_parquet(ETF_PX)
        .sort(["symbol", "date"])
        .with_columns(pl.int_range(0, pl.len()).over("symbol").alias("rn"))
        .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("r"))
    )
    # ETF 日涨跌幅上限 10%/20%（双创 20%），|r|>25% 只可能是份额拆分或数据错误
    e = e.with_columns(
        pl.when(pl.col("r").abs() > 0.25).then(0.0).otherwise(pl.col("r")).alias("r"))
    e = e.with_columns(pl.col("amount").rolling_mean(20).shift(1).over("symbol").alias("amt20"))
    e = e.with_columns(pl.col("date").dt.truncate("1mo").alias("m"))
    ret = (
        e.group_by(["m", "symbol"])
        .agg([(pl.col("r") + 1.0).product().alias("r1"), pl.col("rn").max().alias("rn_end")])
        .with_columns((pl.col("r1") - 1.0).alias("ret"))
        .select(["m", "symbol", "ret", "rn_end"])
    )
    liq = e.group_by(["m", "symbol"]).agg(pl.col("amt20").last().alias("amt20"))
    return ret, liq


# ---------------------------------------------------------------- 信号
def _annualize(agg: pl.DataFrame, key: str) -> pl.DataFrame:
    """行业-季聚合面板 → 年频; d_roe 用年差=1 重算, accel = d_roe 的变化。"""
    a = (
        agg.filter(pl.col("period_end").dt.month() == 12)
        .with_columns(pl.col("period_end").dt.year().alias("y"))
        .sort([key, "y"])
        .with_columns([pl.col("roe").shift(1).over(key).alias("_roe_p"),
                       pl.col("y").shift(1).over(key).alias("_y_p"),
                       (pl.col("d_roe") if "d_roe" in agg.columns
                        else pl.lit(None, dtype=pl.Float64)).alias("d_roe_g")])
        .with_columns(
            pl.when((pl.col("y") - pl.col("_y_p")) == 1)
            .then(pl.col("roe") - pl.col("_roe_p")).otherwise(None).alias("d_roe"))
        .sort([key, "y"])
        .with_columns([pl.col("d_roe").shift(1).over(key).alias("_d_p"),
                       pl.col("y").shift(1).over(key).alias("_y_p2")])
        .with_columns(
            pl.when((pl.col("y") - pl.col("_y_p2")) == 1)
            .then(pl.col("d_roe") - pl.col("_d_p")).otherwise(None).alias("accel"))
        .with_columns(
            pl.when(pl.col("d_roe").is_not_null() & pl.col("_d_p").is_not_null())
            .then((pl.col("d_roe") > 0) & (pl.col("_d_p") <= 0)).otherwise(None).alias("turn"))
        # 已实现的下一期 ROE 变化（要到 R+2 年 4 月才可知）——用于拆链条第二段
        .with_columns([pl.col("roe").shift(-1).over(key).alias("_roe_n"),
                       pl.col("y").shift(-1).over(key).alias("_y_n")])
        .with_columns(
            pl.when((pl.col("_y_n") - pl.col("y")) == 1)
            .then(pl.col("_roe_n") - pl.col("roe")).otherwise(None).alias("d_roe_fwd"))
    )
    return a.select([key, "y", "roe", "d_roe", "d_roe_g", "accel", "turn",
                     "d_roe_fwd", "_n"])


def industry_signal() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, dict]:
    """G 段（part_h_industry）的行业聚合面板 → 年频信号 + 公司层面面板（方差分解用）。"""
    raw = RI.load_fin()
    df = RI.prep(raw, require_fwd=False)
    agg = RI.part_h_industry(df)["_agg"]

    ind = pl.read_parquet(IND).select(["symbol", "所属同花顺行业"]).rename(
        {"所属同花顺行业": "ind_l1"}).with_columns(
        pl.col("ind_l1").str.split("-").list.first())
    tagged = set(ind["symbol"].to_list())
    drop = 1.0 - df.filter(pl.col("symbol").is_in(list(tagged))).height / df.height
    audit = {"firm_quarter_dropped_by_industry_tag": round(drop, 4),
             "n_firm_quarter": df.height, "n_industry": agg["ind_l1"].n_unique()}

    atomic = _annualize(agg, "ind_l1")
    m = atomic.drop_nulls(["d_roe", "d_roe_g"]).select(
        (pl.col("d_roe") - pl.col("d_roe_g")).abs().alias("d"))
    audit["annual_vs_G_d_roe_max_absdiff"] = round(float(m["d"].max() if m.height else 0), 6)

    # 公司层面年频（Q4）：用于 between/within 方差分解
    firm = (
        df.filter(pl.col("period_end").dt.month() == 12)
        .with_columns(pl.col("period_end").dt.year().alias("y"))
        .join(ind, on="symbol", how="inner")
        .sort(["symbol", "y"])
        .with_columns([pl.col("roe").shift(1).over("symbol").alias("_rp"),
                      pl.col("y").shift(1).over("symbol").alias("_yp")])
        .with_columns(
            pl.when((pl.col("y") - pl.col("_yp")) == 1)
            .then(pl.col("roe") - pl.col("_rp")).otherwise(None).alias("d_roe"))
        .sort(["symbol", "y"])
        .with_columns([pl.col("d_roe").shift(1).over("symbol").alias("_dp"),
                      pl.col("y").shift(1).over("symbol").alias("_yp2")])
        .with_columns(
            pl.when((pl.col("y") - pl.col("_yp2")) == 1)
            .then(pl.col("d_roe") - pl.col("_dp")).otherwise(None).alias("accel"))
        .with_columns(
            pl.when(pl.col("d_roe").is_not_null() & pl.col("_dp").is_not_null())
            .then((pl.col("d_roe") > 0) & (pl.col("_dp") <= 0)).otherwise(None).alias("turn"))
        .select(["symbol", "y", "ind_l1", "roe", "d_roe", "accel", "turn", "d_roe_fwd"])
    )
    return atomic, agg, firm, audit


def basket_signal(agg: pl.DataFrame, baskets: list[dict]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """篮子 = 成员原子行业**加总后再相除**（不是行业 d_roe 的均值）。"""
    pairs = [(b["name"], i) for b in baskets for i in b["members"]]
    m = pl.DataFrame({"basket": [p[0] for p in pairs], "ind_l1": [p[1] for p in pairs]})
    b = (
        agg.join(m, on="ind_l1", how="inner")
        .group_by(["period_end", "qidx", "basket"])
        .agg([pl.col("_ni").sum().alias("_ni"), pl.col("_eq").sum().alias("_eq"),
              pl.col("_n").sum().alias("_n")])
        .with_columns((pl.col("_ni") / pl.col("_eq") * 100).alias("roe"))
        .with_columns(pl.col("basket").alias("ind_l1"))
    )
    return _annualize(b, "ind_l1"), m


def shift_signal(sig: pl.DataFrame, key: str = "ind_l1") -> pl.DataFrame:
    """安慰剂 2: 用 y−1 年的信号值给 y 年排序（同样的建仓日）。"""
    prev = sig.select([key, (pl.col("y") + 1).alias("y"), pl.col("d_roe").alias("d_roe_s"),
                       pl.col("accel").alias("accel_s"), pl.col("turn").alias("turn_s")])
    j = sig.join(prev, on=[key, "y"], how="inner")
    return j.select([key, "y", pl.col("d_roe_s").alias("d_roe"),
                     pl.col("accel_s").alias("accel"), pl.col("turn_s").alias("turn")])


# ---------------------------------------------------------------- 组合构造
def cohort_cache(monthly: pl.DataFrame, key: str, val_col: str = "ew") -> list[dict]:
    """把每个年度的 12 个月收益矩阵缓存下来（回测与安慰剂共用, 避免重复 pivot）。"""
    months = sorted(monthly["m"].unique().to_list())
    mpos = {m: i for i, m in enumerate(months)}
    out = []
    for y in sorted({m.year for m in months}):
        m0 = [m for m in months if m.year == y and m.month == ENTRY_MONTH]
        if not m0:
            continue
        i0 = mpos[m0[0]]
        if i0 + HOLD_MONTHS > len(months):
            continue
        win = months[i0:i0 + HOLD_MONTHS]
        piv = (
            monthly.filter(pl.col("m").is_in(win))
            .pivot(index="m", on=key, values=val_col)
            .sort("m")
        )
        cols = [c for c in piv.columns if c != "m"]
        if not cols or piv.height != HOLD_MONTHS:
            continue
        mat = piv.select(cols).to_numpy().astype(float)
        mat = np.where(np.isfinite(mat), mat, np.nan)
        comp = np.prod(np.where(np.isfinite(mat), 1.0 + mat, 1.0), axis=0) - 1.0
        ok = [i for i in range(len(cols)) if np.isfinite(mat[:, i]).all()]
        if len(ok) < 6:
            continue
        out.append({"y": y, "names": cols, "mat": mat, "comp": comp, "ok": np.array(ok)})
    return out


def backtest(cache: list[dict], sig: pl.DataFrame, val: str, key: str = "ind_l1",
             lag: int = 0) -> dict:
    """lag=0 为可交易口径（年报 R 配 R+1 年 5 月起的收益）；
    lag<0 把窗口前移（lag=-1 即「报告当年」, 大部分时间点在披露之前）⇒ 泄漏剖面。"""
    per = {k: [] for k in ("top", "bot", "univ", "spread", "ex_top", "ic", "slope")}
    per["monthly_top"], per["monthly_ex"], per["years"], per["cohorts"] = [], [], [], []
    for c in cache:
        # 口径门 #1: 报告期 R 的年报在 R+1 年 4/30 才可知 ⇒ 只能配 R+1 年 5 月起的 12 个月。
        # 窗口 c["y"] 的起点是 5 月, 所以可用信号年 = c["y"] - 1（不是 c["y"]）。
        s = sig.filter(pl.col("y") == c["y"] - 1 - lag).filter(pl.col(key).is_in(c["names"]))
        s = s.drop_nulls([val])
        if s.height < 6:
            continue
        pos = {n: i for i, n in enumerate(c["names"])}
        names = [n for n in s[key].to_list() if pos[n] in set(c["ok"].tolist())]
        if len(names) < 6:
            continue
        vmap = dict(zip(s[key].to_list(), s[val].to_list()))
        vals = np.array([float(vmap[n]) for n in names])
        if val == "turn":
            # 布尔信号有大量并列 ⇒ 用拐点强度做次级排序（否则 top 名单取决于字母序）
            amap = dict(zip(s[key].to_list(), s["accel"].to_list()))
            acc = np.array([float(amap.get(n) or 0.0) for n in names])
            acc = np.where(np.isfinite(acc), acc, 0.0)
            vals = vals * 1e6 + acc
        comp = np.array([c["comp"][pos[n]] for n in names])
        okall = np.array([c["comp"][pos[n]] for n in c["names"] if pos[n] in set(c["ok"].tolist())])
        order = np.argsort(-vals)
        k = max(1, int(round(len(names) * TOPK_Q)))
        top, bot = order[:k], order[-k:]
        r_top, r_bot, r_un = comp[top].mean(), comp[bot].mean(), okall.mean()
        per["top"].append(r_top); per["bot"].append(r_bot)
        per["univ"].append(r_un); per["spread"].append(r_top - r_bot)
        per["ex_top"].append(r_top - r_un); per["years"].append(c["y"])
        mtop = np.nanmean(c["mat"][:, [pos[n] for n in names]][:, top], axis=1)
        mun = np.nanmean(c["mat"][:, [pos[n] for n in names]], axis=1)
        per["monthly_top"].extend(mtop.tolist()); per["monthly_ex"].extend((mtop - mun).tolist())
        rx, ry = _rank(vals), _rank(comp)
        if rx.std() > 0 and ry.std() > 0:
            per["ic"].append(float(np.corrcoef(rx, ry)[0, 1]))
            per["slope"].append(float(np.polyfit(_z(vals), _z(comp), 1)[0]))
        per["cohorts"].append({
            "y": c["y"], "sig_year": c["y"] - 1 - lag,
            "window": f"{c['y']}-05~{c['y'] + 1}-04",
            "n_assets": len(names), "n_top": int(k),
            "top": round(float(r_top) * 100, 2), "univ": round(float(r_un) * 100, 2),
            "bot": round(float(r_bot) * 100, 2),
            "ex_top": round(float(r_top - r_un) * 100, 2),
            "spread": round(float(r_top - r_bot) * 100, 2),
            "top_names": [names[i] for i in top],
        })
    out: dict = {"n": len(per["years"]), "val": val}
    if out["n"] < 5:
        return out
    for k in ("top", "bot", "univ", "spread", "ex_top"):
        a = np.array(per[k])
        out[k + "_mean"] = round(float(a.mean()) * 100, 2)
        out[k + "_t"] = round(tstat(a), 2)
        out[k + "_pos"] = round(float((a > 0).mean()), 3)
        out[k + "_ci"] = boot_ci(a)
    out["sd_ex_top"] = round(float(np.std(per["ex_top"], ddof=1)) * 100, 2)
    out["sd_spread"] = round(float(np.std(per["spread"], ddof=1)) * 100, 2)
    out["mde_ex_top"] = round(mde(float(np.std(per["ex_top"], ddof=1)), out["n"]) * 100, 2)
    out["mde_spread"] = round(mde(float(np.std(per["spread"], ddof=1)), out["n"]) * 100, 2)
    out["monthly_ex_nw_t"] = round(nw_t(np.array(per["monthly_ex"])), 2)
    out["ic_mean"] = round(float(np.nanmean(per["ic"])), 4) if per["ic"] else None
    out["ic_t"] = round(tstat(np.array(per["ic"])), 2) if per["ic"] else None
    out["ic_sd"] = round(float(np.std(per["ic"], ddof=1)), 4) if len(per["ic"]) > 2 else None
    out["mde_ic"] = round(mde(float(np.std(per["ic"], ddof=1)), len(per["ic"])), 4) \
        if len(per["ic"]) > 2 else None
    out["slope_mean"] = round(float(np.nanmean(per["slope"])), 4) if per["slope"] else None
    out["slope_t"] = round(tstat(np.array(per["slope"])), 2) if per["slope"] else None
    out["years"] = per["years"]
    out["cohorts"] = per["cohorts"]
    return out


def partial_slope(cache: list[dict], sig: pl.DataFrame, val: str,
                  key: str = "ind_l1") -> dict:
    """控行业动量（过去 12 月）与 ROE 水平后, 信号对同期行业收益的偏斜率。

    动量的观测窗口 = 上一个 cohort 的那 12 个月（入场前已实现, 无前视）。
    """
    prev: dict[str, float] = {}
    coefs, mom_coefs, roe_coefs = [], [], []
    for c in cache:
        pos = {n: i for i, n in enumerate(c["names"])}
        okidx = set(c["ok"].tolist())
        s = (sig.filter(pl.col("y") == c["y"] - 1)
             .filter(pl.col(key).is_in(c["names"])).drop_nulls([val, "roe"]))
        vmap = dict(zip(s[key].to_list(), s[val].to_list()))
        rmap = dict(zip(s[key].to_list(), s["roe"].to_list()))
        rows = [(n, float(vmap[n]), float(rmap[n])) for n in s[key].to_list()]
        mom = np.array([prev.get(n, np.nan) for n, _, _ in rows])
        keep = np.array([(n in prev and pos[n] in okidx) for n, _, _ in rows]) & np.isfinite(mom)
        prev = {n: c["comp"][pos[n]] for n in c["names"] if pos[n] in okidx}
        if keep.sum() < 10:
            continue
        y = np.array([c["comp"][pos[n]] for n, _, _ in rows])[keep]
        x_sig = _z([v for _, v, _ in rows])[keep]
        x_roe = _z([r for _, _, r in rows])[keep]
        A = np.column_stack([np.ones(keep.sum()), x_sig, _z(mom[keep]), x_roe])
        b, *_ = np.linalg.lstsq(A, _z(y), rcond=None)
        coefs.append(float(b[1]))
        mom_coefs.append(float(b[2]))
        roe_coefs.append(float(b[3]))
    if not coefs:
        return {"n": 0}
    return {"n": len(coefs),
            "coef": round(float(np.mean(coefs)), 4), "t": round(tstat(np.array(coefs)), 2),
            "mom_coef": round(float(np.mean(mom_coefs)), 4),
            "mom_t": round(tstat(np.array(mom_coefs)), 2),
            "roe_coef": round(float(np.mean(roe_coefs)), 4),
            "roe_t": round(tstat(np.array(roe_coefs)), 2)}


def placebo_random(cache: list[dict], years: list[int], n_draw: int = N_DRAW) -> dict:
    """安慰剂 1: 同一批日期、同样数量的**随机资产**。残差 = 观测 − 安慰剂均值。"""
    use = [c for c in cache if c["y"] in years]
    rng = np.random.default_rng(SEED)
    draws = np.empty(n_draw)
    for d in range(n_draw):
        acc = []
        for c in use:
            ok = c["ok"]
            k = max(1, int(round(len(ok) * TOPK_Q)))
            pick = rng.choice(ok, size=k, replace=False)
            acc.append(c["comp"][pick].mean() - c["comp"][ok].mean())
        draws[d] = float(np.mean(acc)) * 100 if acc else np.nan
    draws = draws[np.isfinite(draws)]
    return {"mean": round(float(draws.mean()), 2),
            "p05": round(float(np.percentile(draws, 5)), 2),
            "p95": round(float(np.percentile(draws, 95)), 2),
            "n_draw": int(draws.size), "_draws": draws}


def costs(per: dict, side_bps: float) -> dict:
    """真实成本: 按持有名单实际重合度算年换手, 双边计费。"""
    turn, prev = [], set()
    for c in per["cohorts"]:
        cur = set(c["top_names"])
        turn.append(1.0 if not prev else len(cur - prev) / max(len(cur), 1))
        prev = cur
    t = float(np.mean(turn))
    drag = t * side_bps * 2 / 100.0
    return {"turnover_per_year": round(t, 3), "side_bps": side_bps,
            "drag_pct_per_year": round(drag, 2),
            "ex_top_net": round(per["ex_top_mean"] - drag, 2),
            "spread_net": round(per["spread_mean"] - drag, 2)}


# ---------------------------------------------------------------- 审计
def etf_map_table() -> tuple[list[dict], pl.DataFrame]:
    """名字候选 → 流动性最好的那只 → 与篮子股票组合的收益相关性验证。"""
    inst = pl.read_parquet(ETF_INST).select(["symbol", "name"])
    rows = []
    for b in BASKETS:
        cand = inst.filter(pl.col("name").str.contains(b["pat"]))
        if b["neg"]:
            cand = cand.filter(~pl.col("name").str.contains(b["neg"]))
        rows.append({"basket": b["name"], "members": len(b["members"]),
                     "n_cand": cand.height,
                     "cands": cand["name"].sort().to_list()[:5]})
    return rows, inst


def etf_picks(etf_ret: pl.DataFrame, liq: pl.DataFrame, inst: pl.DataFrame,
              min_amt: float = MIN_AMT20) -> pl.DataFrame:
    """每个 (建仓年, 篮子) 选一只 ETF: 决策日 = 4 月末（年报截止当日）,
    要求已上市满 250 个交易日、4 月末 20 日均额达标, 取最活跃的那只。"""
    e = (
        etf_ret.filter(pl.col("m").dt.month() == 4)
        .with_columns(pl.col("m").dt.year().alias("y"))
        .join(liq, on=["m", "symbol"], how="left")
        .filter((pl.col("rn_end") >= MIN_LISTED) & (pl.col("amt20") >= min_amt))
    )
    rows = []
    for b in BASKETS:
        cand = inst.filter(pl.col("name").str.contains(b["pat"]))
        if b["neg"]:
            cand = cand.filter(~pl.col("name").str.contains(b["neg"]))
        syms = set(cand["symbol"].to_list())
        if not syms:
            continue
        for y in sorted(e["y"].unique().to_list()):
            sub = e.filter((pl.col("y") == y) & pl.col("symbol").is_in(list(syms)))
            if sub.height == 0:
                continue
            best = sub.sort("amt20", descending=True).row(0, named=True)
            rows.append({"basket": b["name"], "y": int(y), "symbol": best["symbol"],
                         "amt20": float(best["amt20"])})
    if not rows:
        return pl.DataFrame(schema={"basket": pl.Utf8, "y": pl.Int32,
                                    "symbol": pl.Utf8, "amt20": pl.Float64})
    return pl.DataFrame(rows).with_columns(pl.col("y").cast(pl.Int32))


def etf_validation(picks: pl.DataFrame, etf_ret: pl.DataFrame,
                   monthly_basket: pl.DataFrame) -> list[dict]:
    """映射验证: ETF 月收益 vs 篮子股票等权; 以及全市场相关性最高的原子行业（防错配）。"""
    out = []
    top = picks.group_by("basket").agg(pl.col("amt20").mean().alias("a")).sort("a", descending=True)
    for r in top.head(40).to_dicts():
        b = r["basket"]
        pk = picks.filter(pl.col("basket") == b).sort("y")
        # 取该篮子成交额最大的那只 ETF
        s = (pk.group_by("symbol").agg(pl.col("amt20").sum().alias("a"))
             .sort("a", descending=True).row(0, named=True)["symbol"])
        e = etf_ret.filter(pl.col("symbol") == s).select(["m", "ret"])
        bb = monthly_basket.filter(pl.col("asset") == b).select(["m", "ew"])
        j = e.join(bb, on="m", how="inner").drop_nulls()
        corr = float(np.corrcoef(j["ret"].to_numpy(), j["ew"].to_numpy())[0, 1]) if j.height > 24 else None
        best, bc = None, -9.0
        for ind in monthly_basket["asset"].unique().to_list():
            a = monthly_basket.filter(pl.col("asset") == ind).select(["m", "ew"])
            j2 = e.join(a, on="m", how="inner").drop_nulls()
            if j2.height < 24:
                continue
            c = float(np.corrcoef(j2["ret"].to_numpy(), j2["ew"].to_numpy())[0, 1])
            if c > bc:
                best, bc = ind, c
        out.append({"basket": b, "symbol": s, "corr_basket": round(corr, 3) if corr else None,
                    "best_atomic": best, "corr_best": round(bc, 3)})
    return out


def apply_fidelity(picks: pl.DataFrame, val: list[dict],
                   thr: float = MIN_FIDELITY) -> tuple[pl.DataFrame, list[str]]:
    """按「ETF 与自己篮子的月收益相关性」剔除错配车辆（结构属性, 不按策略收益选）。"""
    good = {r["basket"] for r in val if (r["corr_basket"] or 0) >= thr}
    dropped = sorted({r["basket"] for r in val if (r["corr_basket"] or 0) < thr})
    return picks.filter(pl.col("basket").is_in(list(good))), dropped


def between_within(firm: pl.DataFrame) -> dict:
    """信号在哪一层: 行业内偏离 vs 行业间。行业聚合只能救「行业间」那部分。"""
    out = {}
    for s in ("d_roe", "accel"):
        rs, var_b, var_t, shares = [], [], [], []
        for _, g in firm.filter(pl.col(s).is_not_null() & pl.col("d_roe_fwd").is_not_null()
                                ).group_by("y"):
            g = g.select([s, "ind_l1", "d_roe_fwd"]).drop_nulls()
            if g.height < 200 or g["ind_l1"].n_unique() < 10:
                continue
            x = g[s].to_numpy().astype(float)
            y = g["d_roe_fwd"].to_numpy().astype(float)
            gi = g["ind_l1"].to_numpy()
            # 行业均值 / 行业内偏离（各自去全市场均值）
            mx, my = x.mean(), y.mean()
            dfm = pl.DataFrame({"g": gi, "x": x, "y": y})
            agg = dfm.group_by("g").agg([pl.col("x").mean().alias("xb"),
                                         pl.col("y").mean().alias("yb")])
            dfm = dfm.join(agg, on="g", how="left")
            xb = dfm["xb"].to_numpy().astype(float)
            yb = dfm["yb"].to_numpy().astype(float)
            if xb.std() == 0 or yb.std() == 0:
                continue
            rs.append(float(np.corrcoef(_rank(xb), _rank(yb))[0, 1]))
            var_b.append(float(np.var(xb) / (np.var(x) + 1e-12)))
            var_t.append(float(np.var(yb) / (np.var(y) + 1e-12)))
            # 行业内秩相关
            ics = []
            for key, sub in dfm.group_by("g"):
                if sub.height >= 10:
                    rx, ry = _rank(sub["x"].to_numpy()), _rank(sub["y"].to_numpy())
                    if rx.std() > 0 and ry.std() > 0:
                        ics.append(float(np.corrcoef(rx, ry)[0, 1]))
            shares.append(float(np.mean(ics)) if ics else np.nan)
        out[s] = {
            "ic_between": round(float(np.nanmean(rs)), 4) if rs else None,
            "t_between": round(tstat(np.array(rs)), 2) if rs else None,
            "ic_within": round(float(np.nanmean(shares)), 4) if shares else None,
            "var_share_between_signal": round(float(np.nanmean(var_b)), 3) if var_b else None,
            "var_share_between_target": round(float(np.nanmean(var_t)), 3) if var_t else None,
            "n_years": len(rs),
        }
    return out


def judge(tests: dict, plac: dict) -> dict:
    """预登记判据: 主口径 S2 accel 的 t>=2 且两个安慰剂残差同为正。"""
    out = {}
    for tag, blk in tests.items():
        r = blk.get("accel") or {}
        if not r.get("cohorts"):
            out[tag] = "样本不足"
            continue
        ok_t = (r["ex_top_t"] or 0) >= 2 or (r["spread_t"] or 0) >= 2
        p = plac.get(tag)
        resid_ok = bool(p and p["resid_random"] > 0 and (p["resid_shift"] or -1) > 0)
        out[tag] = (f"t(超额)={r['ex_top_t']} t(多空)={r['spread_t']} "
                    f"⇒ {'通过' if ok_t else '未过'}；安慰剂残差 "
                    f"{'同为正' if resid_ok else '未同为正'} ⇒ "
                    f"{'判据成立' if (ok_t and resid_ok) else '判据不成立'}"
                    f"（N={r['n']}, MDE={r['mde_ex_top']}pct）")
    return out


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    res: dict = {}

    print("== 0 信号面板（复用 G 段 part_h_industry） ==", flush=True)
    atomic, agg, firm, audit = industry_signal()
    bsig, bmap = basket_signal(agg, BASKETS)
    print(f"  原子行业 {audit['n_industry']} 个 | 年频 {atomic.height} 行 "
          f"{atomic['y'].min()}~{atomic['y'].max()}")
    print(f"  d_roe 与 G 段季度版最大差 {audit['annual_vs_G_d_roe_max_absdiff']}")
    print(f"  无行业标签而丢弃的公司-季 {audit['firm_quarter_dropped_by_industry_tag']:.2%}")
    print(f"  篮子 {bsig['ind_l1'].n_unique()} 个 / 覆盖原子行业 {bmap['ind_l1'].n_unique()}/90")
    res["meta"] = {"entry_month": ENTRY_MONTH, "hold_months": HOLD_MONTHS, "top_q": TOPK_Q,
                   "min_amt20": MIN_AMT20, "min_listed": MIN_LISTED,
                   "cost_bps_side": COST_BPS_SIDE, "seed": SEED, "n_draw": N_DRAW}
    res["audit"] = audit

    print("\n== 1 行情 → 月频 ==", flush=True)
    sret, sw = stock_monthly()
    print(f"  个股月收益 {sret.height:,} 行 / {sret['symbol'].n_unique():,} 只 "
          f"({sret['m'].min()} ~ {sret['m'].max()})")
    ind = pl.read_parquet(IND).select(["symbol", "所属同花顺行业"]).rename(
        {"所属同花顺行业": "asset"}).with_columns(
        pl.col("asset").str.split("-").list.first())
    A = panel_returns(sret, sw, ind, "asset")                       # 原子行业
    bmap2 = (bmap.rename({"ind_l1": "asset"}).join(ind, on="asset", how="inner")
             .select(["symbol", "basket"]).unique())
    AP = panel_returns(sret, sw, bmap2.rename({"basket": "asset"}), "asset")  # 篮子

    print("\n== 2 ETF 映射 ==", flush=True)
    eret, eliq = etf_monthly()
    inst = pl.read_parquet(ETF_INST).select(["symbol", "name"])
    rows, _ = etf_map_table()
    picks = etf_picks(eret, eliq, inst)
    n_b = picks["basket"].n_unique() if picks.height else 0
    print(f"  有可投 ETF 的篮子 {n_b}/{len(BASKETS)} 个 | 配对 {picks.height if picks.height else 0} 个")
    print(f"  ETF 池 {inst.height:,} 只, 全部活到 2026-11 ⇒ 车辆层面零清算（生存偏差上界）")
    if picks.height:
        cov = (picks.group_by("y").agg(pl.col("basket").n_unique().alias("n")).sort("y"))
        print("  可投篮子数: " + "  ".join(f"{r['y']}:{r['n']}" for r in cov.to_dicts()))
        res["audit"]["etf_coverage"] = cov.to_dicts()
    res["audit"]["etf_map"] = rows
    val = etf_validation(picks, eret, AP) if picks.height else []
    res["audit"]["etf_validation"] = val
    if val:
        print(f"  映射保真度（ETF vs 篮子月收益相关性, ≥{MIN_FIDELITY:.2f} 才进 ETF 层）:")
        for r in sorted(val, key=lambda x: -(x["corr_basket"] or 0)):
            mark = "" if (r["corr_basket"] or 0) >= MIN_FIDELITY else "  ✗"
            print(f"   {r['basket']:<8}{r['symbol']} corr={r['corr_basket']} "
                  f"最强原子行业={r['best_atomic']}({r['corr_best']}){mark}")
        picks, dropped = apply_fidelity(picks, val)
        res["audit"]["dropped_by_fidelity"] = dropped
        print(f"  保真度不足而剔除: {dropped if dropped else '无'}")

    print("\n== 3 回测（年度非重叠, 5 月建仓持 12 月） ==", flush=True)
    tests: dict = {}
    cacheA = cohort_cache(A, "asset")
    cacheP = cohort_cache(AP, "asset")
    cachePcw = cohort_cache(AP.drop("ew").rename({"cw": "ew"}), "asset")
    nb = bsig["ind_l1"].n_unique()
    cacheF = cohort_cache(sret.rename({"symbol": "ind_l1"}), "ind_l1", val_col="ret")
    sigF = firm.drop("ind_l1").rename({"symbol": "ind_l1"})
    for tag, cache, sig, key in (
            ("A_原子90_股票EW", cacheA, atomic, "ind_l1"),
            (f"Ap_篮子{nb}_股票EW", cacheP, bsig, "ind_l1"),
            (f"App_篮子{nb}_股票CW", cachePcw, bsig, "ind_l1"),
            ("F_公司层面_股票EW", cacheF, sigF, "ind_l1")):
        tests[tag] = {s: backtest(cache, sig, s, key) for s in ("d_roe", "accel", "turn")}
        r = tests[tag]["accel"]
        print(f"  {tag}: N={r['n']} 超额 {r.get('ex_top_mean')}% t={r.get('ex_top_t')}")

    # B 层: 真实 ETF。每个 (篮子, 建仓年) 用当年选出的那只 ETF 的月收益拼成月度长表
    cacheB = None
    if picks.height:
        ym = (
            eret.join(picks.select(["basket", "y", "symbol"]), on="symbol", how="inner")
            .with_columns(pl.when(pl.col("m").dt.month() >= ENTRY_MONTH)
                          .then(pl.col("m").dt.year()).otherwise(pl.col("m").dt.year() - 1)
                          .alias("cy"))
            .filter(pl.col("cy") == pl.col("y"))
            .select(["m", "basket", pl.col("ret").alias("ew")])
        )
        cacheB = cohort_cache(ym, "basket")
        tests[f"B_篮子{nb}_ETF"] = {s: backtest(cacheB, bsig, s, "ind_l1")
                                    for s in ("d_roe", "accel", "turn")}
        r = tests[f"B_篮子{nb}_ETF"]["accel"]
        print(f"  B_篮子{nb}_ETF: N={r['n']} 建仓 {cacheB[0]['y'] if cacheB else '-'}~"
              f"{cacheB[-1]['y'] if cacheB else '-'} 超额 {r.get('ex_top_mean')}% "
              f"t={r.get('ex_top_t')}")
    res["tests"] = tests

    print("\n== 4 安慰剂（主口径 accel） ==", flush=True)
    plac: dict = {}
    placebo_specs = [("F_公司层面_股票EW", cacheF, sigF),
                     ("A_原子90_股票EW", cacheA, atomic),
                     (f"Ap_篮子{nb}_股票EW", cacheP, bsig),
                     (f"App_篮子{nb}_股票CW", cachePcw, bsig)]
    if cacheB:
        placebo_specs.append((f"B_篮子{nb}_ETF", cacheB, bsig))
    for tag, cache, sig in placebo_specs:
        r = tests[tag]["accel"]
        if not r.get("cohorts"):
            continue
        p1 = placebo_random(cache, r["years"])
        sh = backtest(cache, shift_signal(sig, key), "accel", key)
        draws = p1.pop("_draws")
        # 置换 p 值: 随机安慰剂分布与信号无关, 对所有信号复用同一批抽样
        pv, side = {}, {}
        for sv, blk in tests[tag].items():
            o = blk.get("ex_top_mean")
            if o is None:
                continue
            hi = float((draws >= o).mean())
            lo = float((draws <= o).mean())
            pv[sv] = round(min(1.0, 2 * min(hi, lo)), 3)      # 双侧置换 p
            side[sv] = "正" if hi < lo else "负"
        plac[tag] = {"random": p1, "obs_ex_top": r["ex_top_mean"],
                     "obs_spread": r["spread_mean"],
                     "resid_random": round(r["ex_top_mean"] - p1["mean"], 2),
                     "p_perm": pv, "sign": side,
                     "shift_ex_top": sh.get("ex_top_mean"),
                     "resid_shift": round(r["ex_top_mean"] - (sh.get("ex_top_mean") or 0), 2)}
        print(f"  {tag}: 观测 {r['ex_top_mean']:+.2f}% | 随机 {p1['mean']:+.2f}% "
              f"[{p1['p05']:+.2f},{p1['p95']:+.2f}] 残差 {plac[tag]['resid_random']:+.2f} "
              f"p(双侧)={plac[tag]['p_perm']} | 错位4季 {sh.get('ex_top_mean')}% "
              f"残差 {plac[tag]['resid_shift']:+.2f}")
    res["placebo"] = plac

    print("\n== 5 增量检验: 控行业动量+ROE水平后的偏斜率 ==", flush=True)
    inc: dict = {}
    for tag, cache, sig in (("A_原子90_股票EW", cacheA, atomic),
                            (f"Ap_篮子{nb}_股票EW", cacheP, bsig)):
        inc[tag] = {}
        for sv in ("d_roe", "accel", "turn"):
            r = partial_slope(cache, sig, sv)
            inc[tag][sv] = r
            print(f"  {tag:<16}{sv:<7} N={r.get('n')} 偏斜率 {r.get('coef')} "
                  f"(t={r.get('t')}) | 动量 {r.get('mom_coef')} (t={r.get('mom_t')}) "
                  f"| ROE {r.get('roe_coef')} (t={r.get('roe_t')})")
    res["incremental"] = inc

    print("\n== 6 泄漏剖面: 信号年与收益窗口错开 k 年 ==", flush=True)
    print("   lag=0 可交易（年报 R → R+1 年 5 月起）; lag=-1 大部分时间点在披露前")
    leak: dict = {}
    for tag, cache, sig in (("A_原子90", cacheA, atomic), (f"Ap_篮子{nb}", cacheP, bsig),
                            ("F_公司层面", cacheF, sigF)):
        leak[tag] = {}
        for sv in ("d_roe", "accel"):
            row = {}
            for lg in (-2, -1, 0, 1):
                r = backtest(cache, sig, sv, lag=lg)
                row[lg] = {"ic": r.get("ic_mean"), "ic_t": r.get("ic_t"),
                           "spread": r.get("spread_mean"), "spread_t": r.get("spread_t")}
            leak[tag][sv] = row
            print(f"  {tag:<10}{sv:<7}" + "".join(
                f" lag{lg:+d}: IC {row[lg]['ic']:+.3f}(t{row[lg]['ic_t']:+.1f}) "
                f"多空 {row[lg]['spread']:+.1f}%(t{row[lg]['spread_t']:+.1f})" for lg in (-2,-1,0,1)))
    res["leakage"] = {k: {sv: {str(lg): v for lg, v in row.items()} for sv, row in blk.items()}
                      for k, blk in leak.items()}

    print("\n== 6b 恒等式校验: d_roe_fwd(R) ≡ d_roe(R+1) ==")
    print("   同一变量两条代码路径（val=d_roe_fwd & lag=1 vs val=d_roe & lag=0）必须给同一组数")
    ident: dict = {}
    for tag, cache, sig in (("A_原子90", cacheA, atomic), (f"Ap_篮子{nb}", cacheP, bsig)):
        r1 = backtest(cache, sig, "d_roe", lag=0)
        r2 = backtest(cache, sig, "d_roe_fwd", lag=1)
        d_ic = abs((r1.get("ic_mean") or 0) - (r2.get("ic_mean") or 0))
        d_sp = abs((r1.get("spread_mean") or 0) - (r2.get("spread_mean") or 0))
        ident[tag] = {"d_ic": round(d_ic, 10), "d_spread": round(d_sp, 10)}
        print(f"  {tag:<10} ΔIC={d_ic:.2e} Δ多空={d_sp:.2e} "
              f"⇒ {'一致' if (d_ic < 1e-6 and d_sp < 1e-6) else '不一致（年对齐有误）'}")
        if d_ic > 1e-6 or d_sp > 1e-6:
            raise SystemExit(f"对齐校验失败: {tag}")
    res["identity_check"] = ident

    print("\n== 7 信号在哪一层（between/within） ==", flush=True)
    dec = between_within(firm)
    res["decomp"] = dec
    for k, v in dec.items():
        print(f"  {k}: 行业间 IC={v['ic_between']} (t={v['t_between']}) 行业内 IC={v['ic_within']} "
              f"| 信号行业间方差占比 {v['var_share_between_signal']:.0%} "
              f"目标 {v['var_share_between_target']:.0%}")

    for tag, blk in tests.items():
        print(f"\n== {tag} ==")
        print(f"  {'信号':<8}{'N':>3}{'超额%':>8}{'t':>6}{'多空%':>8}{'多空t':>6}"
              f"{'IC':>7}{'ICt':>6}{'MDE':>6}{'NWt':>6}")
        for s, r in blk.items():
            if not r.get("cohorts"):
                print(f"  {s:<8} 样本不足")
                continue
            print(f"  {s:<8}{r['n']:>3}{r['ex_top_mean']:>8.2f}{r['ex_top_t']:>6.2f}"
                  f"{r['spread_mean']:>8.2f}{r['spread_t']:>6.2f}"
                  f"{r['ic_mean'] or 0:>7.3f}{r['ic_t'] or 0:>6.2f}"
                  f"{r['mde_ex_top']:>6.1f}{r['monthly_ex_nw_t'] or 0:>6.2f}")
        if blk.get("accel", {}).get("cohorts"):
            c = costs(blk["accel"], COST_BPS_SIDE["etf" if tag.startswith("B") else "stock"])
            print(f"  成本: 年换手 {c['turnover_per_year']:.0%} × 双边 {c['side_bps']}bp "
                  f"⇒ {c['drag_pct_per_year']:.2f}%/年 | 净超额 {c['ex_top_net']:+.2f}pct "
                  f"净多空 {c['spread_net']:+.2f}pct")
            res.setdefault("costs", {})[tag] = c

    verdict = judge(tests, plac)
    res["verdict"] = verdict
    print("\n== 判据 ==")
    for k, v in verdict.items():
        print(f"  {k}: {v}")

    if args.write:
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        print(f"\nwrote {OUT.relative_to(ROOT)}")
    if args.check:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        drift = 0.0
        for tag, blk in res["tests"].items():
            for s, r in blk.items():
                p = prev.get("tests", {}).get(tag, {}).get(s, {})
                for k in ("ex_top_mean", "spread_mean"):
                    if p.get(k) and r.get(k):
                        drift = max(drift, abs(p[k] - r[k]) / max(abs(p[k]), 1e-9))
        print(f"\ncheck drift={drift:.4%}")
        raise SystemExit(0 if drift < 0.05 else 1)


if __name__ == "__main__":
    main()
