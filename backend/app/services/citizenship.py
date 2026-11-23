"""短线雷达（计算内核）—— 全市场个股的「博弈角色主场」分类。

模块名沿用 `citizenship`（页面曾叫「市场公民」）：6 个分析脚本与两篇冻结研究
直接按此路径引用，改名会改写研究 provenance。对外 API 与页面已统一为
`/api/shortline-radar` 与「短线雷达」。

为什么主轴不是「成交额排名」
----------------------------
成交额(≈规模×换手率)是一个标量，把「谁在这里博弈」压成一个数，会得到
「看起来权威、实则错误」的分类。`scripts/probe_liquidity_identity.py` 用
2024–25 数据证明：成交额与换手率选的不是同一批票，两者在最近两年分道扬镳。

因此这里用两根**结构轴**分主场（当日横截面分位，1=最高）：
    scale  = 20 日流通市值中位（存量：盘子多大）
    turn   = 20 日换手率中位（流量：筹码多久换一遍）
四格 = 两种「谁在定价」的行为痕迹：
    institution  大市值 × 低换手 → 长钱/被动按权重调仓的痕迹
    quant        大市值 × 高换手 → 高频交易盘接力的痕迹
    hotmoney     小市值 × 高换手 → 打板/题材资金的痕迹
    nowhere      小市值 × 低换手 → 没有定价者，卖出即砸价

为什么必须用 20 日滚动中位，而不是当日值
----------------------------------------
「主场」是结构概念。实测（analysis/_diag_citizenship_arena_stability2.py）：
按当日换手分类，同一只票日间切换主场 **9.35%**（平均 11 天换一次）；
按 20 日中位只有 **1.37%**。每 11 天换一次的不叫主场，叫噪声。
中位版的分层还更干净（analysis/_diag_citizenship_arena_med.py）：
fwd20 跌超 20% 的概率 2.72% / 2.75% / 6.89% / 9.89%，跨度 3.6 倍，34 期同向。

注意风险不是市值驱动的：**大市值 × 高换手（量化主场）是最危险的一格**。
「大市值=安全」是错的——拥挤的大盘成长股左尾最厚。这是 2026 年最贵的错觉。

位次标签（core/active/background/silent，原「公民等级」）降级为**标签**：它只回答「这只票在
全市场成交额/换手的分位上排第几」，不再作为页面主轴。实测它在主场内部仍有
增量（小市值高换手里 core 组 p_dd20 达 21.3% vs active 7.5%），所以保留为标签，
而不是删除。

只读 `kline_daily_enriched` + `instruments`，不写任何数据。
"""

from __future__ import annotations

import bisect
import os
import threading
from datetime import date as date_cls
from pathlib import Path

import polars as pl

# ===== 参数 =====

# 主场窗口：20 个交易日。把「当日截面」压成「结构性身份」。
#
# 不加 min_samples：中位是「窗口内组内中位」，有 1 天就取那一天。
# 次新（窗口内只有几天数据）必须能被分类 —— 被丢掉的新股恰好是最活跃的一批。
ARENA_WINDOW = 20
# 标签窗口：60 个交易日。蓝筹要「60 日低波动」、妖股要「60 日内涨停次数」，
# 都需要比主场更长的窗口，否则「稳」和「妖」都只是一周的情绪。
# 一次读 60 个分区（实测 ~0.1s），主场的中位仍只用最后 20 日，两个窗口不混。
STATS_WINDOW = 60
# 结果缓存上限：面板是百 MB 级中间态，不能随标签页数量线性吃内存。
_CACHE_MAX = 4

# ===== 主场切分 =====
#
# 两轴各切一刀，四格完备互斥。阈值用「分位」，因为这是**相对市场的位置**，
# 不是绝对规模——100 亿在 2015 年是巨无霸，在 2026 年是中位数。
#   scale 切 0.50：一半的票在「大市值」侧
#   turn  切 0.75：前 25% 的票在「高换手」侧
# 语义可解释，且两轴都是中位值 → 不会因为单日异动跳格。
ARENA_CUT = {"scale": 0.50, "turn": 0.75}

ARENA_ORDER = ["institution", "quant", "hotmoney", "nowhere"]

ARENA_LABELS = {
    "institution": "机构主场",
    "quant": "量化主场",
    "hotmoney": "游资主场",
    "nowhere": "无人区",
}

# 措辞纪律：本地数据只能观测「行为痕迹」，不能观测「谁在买」（无持仓、无席位）。
# 所以这里写「……的痕迹一致」，不写「……在这里」。
ARENA_HINTS = {
    "institution": "大市值 × 低换手：与长钱、被动资金按权重调仓的痕迹一致",
    "quant": "大市值 × 高换手：与高频交易盘接力推价的痕迹一致",
    "hotmoney": "小市值 × 高换手：与打板、题材资金的痕迹一致",
    "nowhere": "小市值 × 低换手：没有定价者，卖出即砸价",
}

# ===== 各主场的实测尾部风险（外部验证）=====
#
# 口径：2024-01..2026-10 每月首个交易日共 34 个基期，fwd20 = 基期收盘买入并
# 持有 20 个交易日的收益；dd20 = P(fwd20 < -20%)；med_ret20 = fwd20 中位。
# 样本 182,488 条。重算入口：`python analysis/_diag_citizenship_arena_med.py --check`
# （漂移 > 5% 退出 1）。这不是预测，是「这一格历史上有多危险」的读数。
ARENA_RISK = {
    "institution": {"dd20": 0.0272, "med_ret20": -0.0076},
    "nowhere": {"dd20": 0.0275, "med_ret20": -0.0030},
    "hotmoney": {"dd20": 0.0689, "med_ret20": -0.0201},
    "quant": {"dd20": 0.0989, "med_ret20": -0.0315},
}

# ===== 位次标签（原「公民等级」，降级为标签，不再作主轴）=====
#
# 保留原四层与迁移信号：它回答「这只票在成交额/换手分位上排第几」。
# 每日重算，所以仍是时变的；但页面按主场分组，等级只作行内 chip 与图例。
LEVEL_RULES = {
    "core": {"min_amount": 0.90},
    "active": {"min_amount": 0.50, "min_turnover": 0.90},
    "silent": {"max_amount": 0.10, "max_turnover": 0.10},
}

LEVEL_ORDER = ["core", "active", "background", "silent"]

LEVEL_LABELS = {
    "core": "核心",
    "active": "活跃",
    "background": "背景",
    "silent": "沉默",
}

LEVEL_HINTS = {
    "core": "成交额全市场前 10%，流动性的绝对中心",
    "active": "成交活跃，或小而活跃的异动票",
    "background": "长尾背景板，没有明显活跃信号",
    "silent": "成交与换手都在后 10%",
}

# ===== 风险标记（行内标签，不是独立模块）=====
RISK_RULES = {
    "high": "名称含 ST / *ST：已被实施风险警示，下一站是退市整理期",
    "watch": "流通市值 < 10 亿，或当日成交额 < 2000 万：接近退市红线 / 流动性不足",
}
RISK_ORDER = ["high", "watch", "none"]
RISK_LABELS = {"high": "红灯", "watch": "黄灯", "none": "正常"}

# ===== 属性标签（白马 / 蓝筹 / 龙头 / 妖股）=====
#
# 为什么是「标签」不是「分类」：这四个词不在同一维度上。
#     蓝筹 = 规模 + 稳定        白马 = 盈利质量 + 成长
#     龙头 = 行业内相对地位      妖股 = 波动 + 投机
# 实测（analysis/_diag_citizen_tags.py）：18 只同时命中 3 个标签，
# 蓝筹×龙头 168 只，蓝筹×妖股 0 只。做成互斥分组必然给出错误断言。
#
# 更关键的实测：它们不是新主轴，而是「主场内部的二次筛选」——
#   蓝筹 100% 落在机构主场内，但把机构主场的 fwd20 中位从 −0.88% 提到 −0.34%，
#   dd20 从 2.76% 压到 0.98%（跨 34 期）。所以标签的价值是**收窄**，不是分类。
TAG_ORDER = ["bluechip", "whitehorse", "leader", "yaogu"]

TAG_LABELS = {
    "bluechip": "蓝筹",
    "whitehorse": "白马",
    "leader": "龙头",
    "yaogu": "妖股",
}

TAG_HINTS = {
    "bluechip": "20 日市值前 20% + 换手后 50% + 60 日波动后 50%：大而稳",
    "whitehorse": "ROE≥12% 且营收、净利同比均为正、毛利率不低于行业中位、资产负债率≤60%",
    "leader": "所属行业市值前 20% 且 行业 20 日涨幅前 20%：大且强",
    "yaogu": "60 日内 ≥3 个涨停，或 20 日涨幅≥50% 且换手前 10%",
}

# 标签切分阈值。分位口径与主场一致（当日横截面），但轴不同：
#   bluechip 用 vol60（60 日收益波动）—— 这是主场没有的第三根轴。
TAG_CUT = {
    "bluechip_mcap": 0.80,
    "bluechip_turn": 0.50,
    "bluechip_vol": 0.50,
    "leader_in_ind": 0.80,
    "yaogu_lu_days": 3,
    "yaogu_ret20": 0.50,
    "yaogu_turn": 0.90,
}

# 白马阈值。财务是百分数口径（roe=12 表示 12%）。
# 上界不是经济判断，是**剔数据异常**：本表 net_income_yoy 最大 98070%、
# debt_to_asset_ratio 最大 257.8%、roe 最大 347%。不加界等于没筛。
WHITEHORSE_RULES = {
    "min_roe": 12.0,
    "max_roe": 40.0,
    "min_revenue_yoy": 0.0,
    "max_revenue_yoy": 200.0,
    "min_net_income_yoy": 0.0,
    "max_net_income_yoy": 500.0,
    "max_debt_to_asset": 60.0,
    "min_mcap_pct": 0.50,
}

# ===== 各标签的实测尾部风险（外部验证）=====
#
# 口径：2024-01..2026-10 每月首个交易日共 34 个基期，fwd20 = 基期收盘持有 20 日；
# dd20 = P(fwd20 < -20%)；med_ret20 = fwd20 中位。
# 重算入口：`python analysis/_diag_citizen_tags_stability.py --check`（漂移 >5% 退出 1）。
#
# 只有三个纯价量标签参与验证。白马依赖财报，而 metrics 只有最新一期
# （2026-09-30，公告 2026-11-13），在历史基期上用它就是前视 ⇒ dd20 = None，
# 页面不显示它的历史读数。这是本模块唯一「有标签但无背书」的地方，必须显式标注。
TAG_RISK: dict[str, dict | None] = {
    "bluechip": {"dd20": 0.0098, "med_ret20": -0.0034},
    "whitehorse": None,
    "leader": {"dd20": 0.0623, "med_ret20": -0.0202},
    "yaogu": {"dd20": 0.0887, "med_ret20": -0.0321},
}
# 全市场基线（同一套 34 期），页面用来做对照：标签有没有真的比大盘好。
MARKET_RISK = {"dd20": 0.0404, "med_ret20": -0.0112}

# ===== 触点（focus）：把「分类」升级成「注意力预算」=====
#
# 为什么不给每只票加一个「游资评分」
# --------------------------------
# 加一列评分，页面仍是 5,400 行，用户注意力没有被分配，只是多了一个可排序字段。
# 这里改成**漏斗**：全市场 → 结构达标 → 冷启动 → 今日触点。每层都写清删掉了什么，
# 因为「被删掉的部分」才是「剩下的为什么值得看」。
#
# 三条实测依据（analysis/_diag_youzi_features4.py、_diag_youzi_validate.py、
# _diag_youzi_by_regime.py；产物 data/.scratch/youzi_*.json）：
#
#  1) 振幅是唯一主轴。冷票样本 917,136 股票日，严格滞后 T-1 的单变量 AUC：
#       20 日均振幅 0.734 > 换手 0.680 > 250 日涨幅 0.618 > 成交额 0.610
#       > 60 日涨停次数 0.594 > 自由流通市值 **0.504**。
#     市值单变量无信息，但控制振幅/成交额后系数 −0.436（抑制变量）⇒ 它是
#     「控制波动之后才生效」的第五位变量，不是第一约束。列里保留 heat 的
#     市值项（1 − p_scale）正是这个条件效应，不是又把市值请回主轴。
#
#  2) 冷启动才有效。全市场打分 top20 有 **95.9%** 已在过去 20 日涨停过 ——
#     那是追高榜。剔除后（无 20 日涨停、|ret20|<15%）top20 仍有 lift **6.98**：
#     基准 23.3bp → 162bp（2026-06..2026-11 样本外，117 个交易日）。
#     被剔的那一半不是「次优」，是「别人已经赚完、你在接货」。
#
#  3) 分制度拟合不值得。主板/创业板/科创板各拟合一套，样本外 AUC 只涨
#     +0.0016 / −0.0016 / +0.0087（< 0.01）⇒ 不是「四个模型的平均」。
#     但 ST 是异类：振幅系数 0.074（主板 0.373），涨停史系数 +0.456（主板 −0.022）
#     ⇒ ST 必须排除；北交所正样本仅 15 个 ⇒ 不给预测，只保留结构读数。
#
# 输出纪律：**不输出概率**。只有 AUC 没有校准，写「结构分」和「同类历史命中率」
# 更诚实；分数是排序用的，不是「上涨概率」。
FOCUS_TOP_N = 15
# 成交额下限 3000 万：低于此，2,500 万仓位要占次日成交 50%+（实测卖压 53%），
# 「进得去出不来」在这一档才真的成立。这是容量约束，不是审美。
FOCUS_MIN_AMOUNT = 3e7
# 结构分门槛：振幅/换手双高。低于 0.75 的格子实测命中率跌到基准以下。
HEAT_CUT = 0.75
# 次新门槛：上市不足 250 个交易日不进触点。未解禁前流通盘小、换手天然畸高，
# heat 是制度性虚高不是结构，龙虎榜也因换手达标而高频上榜（联亚药业上市 36 天
# 近 60 日上榜 10 次，洛轴股份 72 天上榜 15 次）。
# 回放（_diag_youzi_kill_backtest.py）：次新启动率 108.5bp vs 存活 153.7bp（z=1.30，
# 不显著，且分年不稳定——2025 年次新 153.8bp 并不低）。所以这条的正当性不是
# 「启动率更低」，而是「换手/龙虎榜不可比 + 解禁盘悬顶」，属于风险排除，不是收益筛选。
FOCUS_MIN_LISTING_DAYS = 250
# 下跌中继门槛：近 60 日跌幅超 10% 的不进触点。这是**唯一有回放背书**的杀手：
# 回放 46,402 个候选里，falling 组未来 5 日涨停率 110.3bp vs 存活 153.7bp，
# z=+3.02，且 2025/2026 两年一致（54.7/114.0 vs 167.4/150.2）。
# 高波动要区分「出货 vs 进攻」，方向就是答案：跟风盘不会在持续下跌中追高。
FOCUS_MIN_RET60 = -0.10

STAGE_ORDER = ["cold", "warming", "hot", "fading"]

STAGE_LABELS = {
    "cold": "待启动",
    "warming": "已启动",
    "hot": "正在炒",
    "fading": "退潮",
}

STAGE_HINTS = {
    "cold": "20 日内无涨停、|20 日涨幅|<15%：结构够格但还没动，唯一有预测价值的一段",
    "warming": "20 日内无涨停但已涨超 15%：资金在推，尚未封板，追进去要赌当天封板",
    "hot": "20 日内已有涨停：正在炒。实测此段占全市场高分票 95.9%，是追高榜的主体",
    "fading": "20 日跌超 20% 且无涨停：动量已破，高波动来自出货不是来自进攻",
}

_CACHE: dict[tuple, dict] = {}
_CACHE_LOCK = threading.Lock()


def _quantile_rank(col: str) -> pl.Expr:
    """当日全市场的分位（0~1，1=最高）。用 ordinal rank 归一，稳定且无 NaN。"""
    return (
        (pl.col(col).rank("ordinal").over("date") - 1)
        / (pl.col(col).count().over("date") - 1).clip(lower_bound=1)
    )


def _assign_arena() -> pl.Expr:
    """把「中位市值分位 × 中位换手分位」映射到四格。

    顺序无关（四格互斥完备），但写成 when 链便于读：
      大 + 高换手 = 量化；大 + 低换手 = 机构；
      小 + 高换手 = 游资；其余 = 无人区。
    """
    big = pl.col("p_scale") >= ARENA_CUT["scale"]
    hot = pl.col("p_turn_med") >= ARENA_CUT["turn"]
    return (
        pl.when(big & hot).then(pl.lit("quant"))
        .when(big).then(pl.lit("institution"))
        .when(hot).then(pl.lit("hotmoney"))
        .otherwise(pl.lit("nowhere"))
        .alias("arena")
    )


def _assign_stage() -> pl.Expr:
    """把「近 20 日是否已涨停 / 20 日涨幅」映射到四段。

    这是**唯一**区分「有预测价值」和「已被兑现」的地方：
      已涨停  → hot     预测价值已被吃掉（实测占高分票 95.9%）
      涨超15% → warming 在推但没封板，赌当天
      跌超20% → fading  动量已破
      其余    → cold    结构够格但还没动，唯一有 lift 的一段

    fade 阈值 −20%（不是 −15%）是回放校准的结果：analysis/_diag_youzi_kill_backtest.py
    证明 −15%~−20% 的「先涨后急跌」票未来启动率 159.8bp，反而高于存活组的 153.7bp
    —— 那是洗盘/超跌反抽，不是退潮。曾改成 −15% 对称是错的，已回退。
    """
    hot = pl.col("lu20").fill_null(0) >= 1
    warm = pl.col("ret20").is_not_null() & (pl.col("ret20") >= 0.15)
    fade = pl.col("ret20").is_not_null() & (pl.col("ret20") <= -0.20)
    return (
        pl.when(hot).then(pl.lit("hot"))
        .when(warm).then(pl.lit("warming"))
        .when(fade).then(pl.lit("fading"))
        .otherwise(pl.lit("cold"))
        .alias("stage")
    )


def _is_bse() -> pl.Expr:
    """北交所：.BJ 后缀或 920 段。北交所正样本仅 15 个 ⇒ 只给结构读数，不进触点。"""
    s = pl.col("symbol")
    return (s.str.ends_with(".BJ") | s.str.starts_with("920")).fill_null(False)


def _reason_array() -> pl.Expr:
    """触点入选理由：把分位翻译成人话，且只写**可复核**的那几条。

    不写「资金流入」「主力吸筹」这类无法从日线观测的断言 —— 本地没有席位、没有持仓，
    写出来就是把推测讲成事实。
    """
    def top(label: str, col: str) -> pl.Expr:
        """「前 X%」：分位 1 = 最高，故 X = 100×(1−p)。"""
        x = (100 - (pl.col(col) * 100).round().cast(pl.Int32)).cast(pl.Utf8)
        return pl.when(pl.col(col) >= 0.75).then(pl.lit(label) + x + pl.lit("%")) \
                 .otherwise(pl.lit(None).cast(pl.Utf8))

    def bottom(label: str, col: str) -> pl.Expr:
        x = (pl.col(col) * 100).round().cast(pl.Int32).cast(pl.Utf8)
        return pl.when(pl.col(col) <= 0.25).then(pl.lit(label) + x + pl.lit("%")) \
                 .otherwise(pl.lit(None).cast(pl.Utf8))

    return pl.concat_list([
        top("振幅前", "p_amp"),
        top("换手前", "p_turn_med"),
        top("成交额前", "p_amount"),
        bottom("市值后", "p_scale"),
    ]).list.drop_nulls()


def _assign_level() -> pl.Expr:
    """把当日成交额/换手分位映射到位次标签。规则见 LEVEL_RULES，顺序即优先级。"""
    t, a = pl.col("p_turnover"), pl.col("p_amount")
    core = a >= LEVEL_RULES["core"]["min_amount"]
    active = (a >= LEVEL_RULES["active"]["min_amount"]) | \
             (t >= LEVEL_RULES["active"]["min_turnover"])
    silent = (a < LEVEL_RULES["silent"]["max_amount"]) & \
             (t < LEVEL_RULES["silent"]["max_turnover"])
    return (
        pl.when(core).then(pl.lit("core"))
        .when(active).then(pl.lit("active"))
        .when(silent).then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
    )


def _is_st_name() -> pl.Expr:
    """名称前缀识别 ST / *ST / S*ST / SST。返回布尔（null 按 False）。"""
    nm = pl.col("name").fill_null("")
    return (
        nm.str.starts_with("ST")
        | nm.str.starts_with("*ST")
        | nm.str.starts_with("S*ST")
        | nm.str.starts_with("SST")
    ).fill_null(False)


def _risk_level() -> pl.Expr:
    """风险标记：high（ST）优先于 watch（市值/流动性），其余 none。"""
    tiny = pl.col("mcap") < 10e8
    thin = pl.col("amount") < 2e7
    return (
        pl.when(_is_st_name()).then(pl.lit("high"))
        .when(tiny | thin).then(pl.lit("watch"))
        .otherwise(pl.lit("none"))
        .alias("risk")
    )


def _risk_reason() -> pl.Expr:
    """风险原因（人类可读），无风险时为 null。"""
    tiny = pl.col("mcap") < 10e8
    thin = pl.col("amount") < 2e7
    return (
        pl.when(_is_st_name()).then(pl.lit("ST"))
        .when(tiny & thin).then(pl.lit("市值<10亿+成交额<2000万"))
        .when(tiny).then(pl.lit("市值<10亿"))
        .when(thin).then(pl.lit("成交额<2000万"))
        .otherwise(pl.lit(None).cast(pl.Utf8))
        .alias("risk_reason")
    )


def _tag_flags() -> list[pl.Expr]:
    """四个属性标签的可计算列（互不排斥，可同时命中）。

    设计纪律：这里算的是**事实条件**，不是评分。每条都能单句复述、能用数据证伪。
    如果把它们加权成「妖股指数」之类，就变成黑盒了。
    """
    wr = WHITEHORSE_RULES
    bluechip = (
        (pl.col("p_scale") >= TAG_CUT["bluechip_mcap"])
        & (pl.col("p_turn_med") <= TAG_CUT["bluechip_turn"])
        & (pl.col("p_vol60") <= TAG_CUT["bluechip_vol"])
    ).fill_null(False)

    # 白马必须四条同时成立。财务缺失（回看早于公告日）时整条为 False，
    # 不能因为「查不到」就默认通过。
    whitehorse = (
        pl.col("roe").is_not_null()
        & (pl.col("roe") >= wr["min_roe"]) & (pl.col("roe") <= wr["max_roe"])
        & (pl.col("revenue_yoy") >= wr["min_revenue_yoy"])
        & (pl.col("revenue_yoy") <= wr["max_revenue_yoy"])
        & (pl.col("net_income_yoy") >= wr["min_net_income_yoy"])
        & (pl.col("net_income_yoy") <= wr["max_net_income_yoy"])
        & (pl.col("gross_margin") >= pl.col("_gm_ind"))
        & (pl.col("debt_to_asset_ratio") <= wr["max_debt_to_asset"])
        & (pl.col("p_scale") >= wr["min_mcap_pct"])
    ).fill_null(False)

    leader = (
        (pl.col("_pi_mcap") >= TAG_CUT["leader_in_ind"])
        & (pl.col("_pi_ret20") >= TAG_CUT["leader_in_ind"])
    ).fill_null(False)

    yaogu = (
        (pl.col("lu_days") >= TAG_CUT["yaogu_lu_days"])
        | ((pl.col("ret20") >= TAG_CUT["yaogu_ret20"])
           & (pl.col("p_turn_med") >= TAG_CUT["yaogu_turn"]))
    ).fill_null(False)

    return [
        bluechip.alias("t_bluechip"),
        whitehorse.alias("t_whitehorse"),
        leader.alias("t_leader"),
        yaogu.alias("t_yaogu"),
    ]


def _tags_array() -> pl.Expr:
    """把四个 bool 列压成字符串数组（前端筛选直接 includes）。"""
    return pl.concat_list([
        pl.when(pl.col(f"t_{k}")).then(pl.lit(k)).otherwise(pl.lit(None).cast(pl.Utf8))
        for k in TAG_ORDER
    ]).list.drop_nulls()


def _enriched_days(data_dir: Path) -> list[date_cls]:
    """一次 scandir 返回所有 enriched 日分区日期（升序）。

    enriched 是**日分区**，目录名即日期，只读目录名即可（~ms 级）。
    这里做成一次扫描 + 复用的列表：`_latest`/`_prev`/`_window` 都从它切片，
    避免每次调用都重扫 5000+ 个目录（旧实现 `_prev_trading_day` 会）。
    """
    base = Path(data_dir) / "kline_daily_enriched"
    if not base.is_dir():
        return []
    out: list[date_cls] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith("date=") or not entry.is_dir():
                    continue
                value = name[5:]
                if len(value) != 10:
                    continue
                try:
                    out.append(date_cls.fromisoformat(value))
                except ValueError:
                    continue
    except OSError:
        return []
    out.sort()
    return out


def _resolve(days: list[date_cls], as_of: date_cls | None) -> date_cls | None:
    """把用户日期解析为「有 enriched 数据的最近交易日」。

    用户可能输入非交易日（周末/节假日）：该日没有分区，直接返回空会让页面
    显示「暂无数据」，令人困惑。这里向前回溯（最多 10 个自然日）到最近一个
    有数据的交易日。`as_of=None` 时用最新交易日。
    """
    if not days:
        return None
    if as_of is None:
        return days[-1]
    i = bisect.bisect_right(days, as_of)
    return days[i - 1] if i > 0 else None


def _window(days: list[date_cls], target: date_cls, n: int) -> list[date_cls]:
    """target 及其之前（含）的 n 个交易日。"""
    i = bisect.bisect_right(days, target)
    return days[max(0, i - n):i]


def _instruments(data_dir: Path) -> pl.DataFrame:
    """读标的维表，取 symbol/name/float_shares/listing_date。缺列时降级为空，不阻断。"""
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return pl.DataFrame(schema={"symbol": pl.Utf8, "name": pl.Utf8, "float_shares": pl.Float64,
                                   "listing_date": pl.Utf8})
    cols = pl.read_parquet_schema(path)
    keep = [c for c in ("symbol", "name", "float_shares", "listing_date") if c in cols]
    if "symbol" not in keep:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "name": pl.Utf8, "float_shares": pl.Float64,
                                   "listing_date": pl.Utf8})
    df = pl.read_parquet(path, columns=keep)
    if "name" not in keep:
        df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("name"))
    if "float_shares" not in keep:
        df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("float_shares"))
    if "listing_date" not in keep:
        df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("listing_date"))
    return df


def _read_day(data_dir: Path, day: date_cls) -> pl.DataFrame:
    """读单个交易日的 enriched 分区（只取需要的列）。缺文件返回空。"""
    f = Path(data_dir) / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    if not f.exists():
        return pl.DataFrame()
    return pl.read_parquet(f, columns=["symbol", "date", "close", "amount", "turnover_rate"])


def _struct_stats(
    data_dir: Path, win: list[date_cls], inst: pl.DataFrame
) -> pl.DataFrame:
    """窗口内每只票的结构统计，返回每票一行。

    一个 60 日面板同时供两个用途，避免重复读盘（60 分区实测 ~0.1s）：
      主场轴     turn_med / mcap_med     ← 20 日窗口的中位（结构：平时是谁的地盘）
      标签轴     vol60 / lu_days / ret20 ← 60 日窗口的波动、涨停、涨幅（二次筛选）

    口径纪律：
      - turn_med/mcap_med 只用**最近 20 日**（与 ARENA_WINDOW 一致），
        否则主场会随窗口长度漂移，和历史分级不可比。
      - vol60 要求 ≥20 个样本，否则置 null：新股 5 天的低波动不是「稳」，是还没波动。
      - lu_days 用窗口内「涨停天数之和」，不用 rolling_sum(60)：后者数行不数天，
        停牌票会把 45 个交易日的涨停算成 60 行的窗口。
    """
    frames: list[pl.DataFrame] = []
    for d in win:
        f = Path(data_dir) / "kline_daily_enriched" / f"date={d.isoformat()}" / "part.parquet"
        if not f.exists():
            continue
        frames.append(
            pl.read_parquet(f, columns=["symbol", "date", "close", "high", "low",
                                        "turnover_rate", "consecutive_limit_ups"])
        )
    if not frames:
        return pl.DataFrame(schema={
            "symbol": pl.Utf8, "turn_med": pl.Float64, "mcap_med": pl.Float64,
            "n_days": pl.UInt32, "vol60": pl.Float64, "lu_days": pl.UInt32,
            "ret20": pl.Float64, "amp20": pl.Float64, "lu20": pl.UInt32,
        })

    panel = (
        pl.concat(frames, how="vertical_relaxed")
        .join(inst.select(["symbol", "float_shares"]), on="symbol", how="left")
        .filter(
            (pl.col("close") > 0)
            & (pl.col("turnover_rate") > 0)
            & (pl.col("float_shares") > 0)
        )
        .with_columns((pl.col("close") * pl.col("float_shares")).alias("_mcap"))
        .sort(["symbol", "date"])
    )

    # ---- 60 日统计（全窗口）----
    panel = panel.with_columns([
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_r1"),
        pl.col("close").shift(ARENA_WINDOW).over("symbol").alias("_close_20"),
        pl.col("consecutive_limit_ups").gt(0).cast(pl.Int8).alias("_lu"),
    ])
    agg = panel.group_by("symbol").agg([
        pl.col("_r1").std().alias("_vol"),
        pl.col("_r1").count().alias("_n_ret"),
        pl.col("_lu").sum().alias("lu_days"),
        pl.col("close").last().alias("_close_last"),
        pl.col("_close_20").last().alias("_c20"),
        pl.len().alias("n_days"),
    ]).with_columns([
        # 样本不足 20 天不给波动率：否则新股会被判成「低波动蓝筹」
        pl.when(pl.col("_n_ret") >= 20)
        .then(pl.col("_vol")).otherwise(None).alias("vol60"),
        pl.when(pl.col("_c20").is_not_null() & (pl.col("_c20") > 0))
        .then(pl.col("_close_last") / pl.col("_c20") - 1)
        .otherwise(None).alias("ret20"),
    ]).drop(["_vol", "_n_ret", "_close_last", "_c20"])

    # ---- 20 日中位（只用最近 ARENA_WINDOW 日）----
    recent_set = set(win[-ARENA_WINDOW:])
    sub = panel.filter(pl.col("date").is_in(list(recent_set)))
    med = sub.group_by("symbol").agg([
        pl.col("turnover_rate").median().alias("turn_med"),
        pl.col("_mcap").median().alias("mcap_med"),
        # 振幅只用近 20 日：它是「波动性供给」的当期读数，60 日会把上一轮炒作
        # 的余温算进来，正是 _diag_youzi_features4 里被 shift(1) 修掉的那个内生性。
        pl.col("high").sub(pl.col("low")).truediv(pl.col("close")).mean().alias("amp20"),
        # 近 20 日涨停天数：阶段判定用（hot 段 = 已经在炒，预测价值已被吃掉）
        pl.col("_lu").sum().alias("lu20"),
    ])
    return agg.join(med, on="symbol", how="left")


def _financials(data_dir: Path, as_of: date_cls) -> pl.DataFrame:
    """读财务指标，并按公告日做 PIT 过滤。

    本表只有最新一期（实测 2026-09-30，公告最晚 2026-11-13）。若 as_of 早于
    公告日，该期数据在当时还不存在 —— 必须置空，否则「白马」标签会前视。
    这也是页面回看旧日期时白马标签整体消失的原因（正确行为，不是 bug）。
    """
    path = Path(data_dir) / "financials" / "metrics" / "part.parquet"
    empty = pl.DataFrame(schema={
        "symbol": pl.Utf8, "roe": pl.Float64, "revenue_yoy": pl.Float64,
        "net_income_yoy": pl.Float64, "gross_margin": pl.Float64,
        "debt_to_asset_ratio": pl.Float64, "report_period": pl.Utf8,
    })
    if not path.exists():
        return empty
    cols = pl.read_parquet_schema(path)
    keep = [c for c in ("symbol", "period_end", "announce_date", "roe", "revenue_yoy",
                        "net_income_yoy", "gross_margin", "debt_to_asset_ratio")
            if c in cols]
    if "symbol" not in keep or "announce_date" not in keep:
        return empty
    df = pl.read_parquet(path, columns=keep)
    df = df.filter(
        (pl.col("announce_date").cast(pl.Utf8) <= as_of.isoformat())
        | pl.col("announce_date").is_null()
    )
    if df.is_empty():
        return empty
    # 同一票多期时取最新一期
    df = df.sort("period_end").group_by("symbol").last()
    if "period_end" in df.columns:
        df = df.with_columns(pl.col("period_end").cast(pl.Utf8).alias("report_period"))
    else:
        df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("report_period"))
    return df


def _industries(data_dir: Path) -> pl.DataFrame:
    """读同花顺行业（一级）。缺文件时返回空表，龙头标签整体失效但不阻断页面。"""
    path = Path(data_dir) / "ext_data" / "ext_hy_ths" / "part.parquet"
    empty = pl.DataFrame(schema={"symbol": pl.Utf8, "industry": pl.Utf8})
    if not path.exists():
        return empty
    df = pl.read_parquet(path)
    if "symbol" not in df.columns or "所属同花顺行业" not in df.columns:
        return empty
    return (
        df.select([
            "symbol",
            pl.col("所属同花顺行业").str.split("-").list.first().alias("industry"),
        ])
        .filter(pl.col("industry").is_not_null())
        .unique(subset=["symbol"])
    )


def compute(
    data_dir: Path,
    as_of: date_cls | None = None,
) -> dict:
    """计算全市场博弈角色主场 + 位次标签。

    两层口径必须分清：
      主场  用窗口**中位**（结构）：这只票平时是谁的地盘，日间切换率 1.37%；
      等级  用**当日**分位（当下）：今天它在成交额/换手排第几。
    混用会让「主场」变成噪声，或让「等级」变成静态标签。

    返回结构：
      as_of       实际使用的交易日
      prev_as_of  前一交易日（迁移信号的比较基准）
      window_days 主场窗口（恒为 ARENA_WINDOW）
      summary     {total, arenas[], levels[], risk, transitions, risk_absent}
      rows        每只票：arena、level、两轴分位、当日读数、迁移、风险
      disclaimer  口径说明（不是收益承诺）
    """
    days = _enriched_days(data_dir)
    latest = _resolve(days, as_of)
    if latest is None:
        return _empty()

    inst = _instruments(data_dir)
    day = _read_day(data_dir, latest)
    if day.is_empty():
        return _empty(latest)

    df = (
        day.join(inst, on="symbol", how="left")
        .filter(
            (pl.col("close") > 0) & (pl.col("amount") > 0)
            & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
        )
        .with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
    )

    # 上市天数（次新杀手用：未解禁前换手天然虚高，heat 是制度性虚高不是结构）。
    df = df.with_columns(
        pl.when(pl.col("listing_date").is_not_null())
        .then((latest - pl.col("listing_date").str.to_date()).dt.total_days())
        .otherwise(None).cast(pl.Int64).alias("listing_days")
    )

    # 60 日涨幅（下跌中继杀手用：高波动来自出货 vs 进攻，靠方向区分）。
    # 用「60 个交易日前的收盘」算，窗口读一次即可，不进 60 日统计面板。
    i60 = bisect.bisect_right(days, latest) - 1 - STATS_WINDOW
    if i60 >= 0:
        day60 = _read_day(data_dir, days[i60])
        if not day60.is_empty():
            df = df.join(day60.select(["symbol", pl.col("close").alias("close60")]),
                         on="symbol", how="left") \
                   .with_columns((pl.col("close") / pl.col("close60") - 1.0).alias("ret60"))
        else:
            df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("ret60"))
    else:
        df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("ret60"))

    # 结构层：60 日窗口的统计（含 20 日中位）。窗口内无有效数据时用当日值兜底，不漏票。
    win = _window(days, latest, STATS_WINDOW)
    stats = _struct_stats(data_dir, win, inst)
    df = df.join(stats, on="symbol", how="left").with_columns([
        pl.col("turn_med").fill_null(pl.col("turnover_rate")),
        pl.col("mcap_med").fill_null(pl.col("mcap")),
    ])

    # 两轴分位（分类轴，用中位）+ 当日成交额分位（等级用）。
    # 注意 p_turnover 是**当日**换手分位（等级口径），p_turn_med 是**中位**（主场口径）。
    df = df.with_columns([
        _quantile_rank("mcap_med").alias("p_scale"),
        _quantile_rank("turn_med").alias("p_turn_med"),
        _quantile_rank("turnover_rate").alias("p_turnover"),
        _quantile_rank("amount").alias("p_amount"),
        # 标签轴：波动率分位（主场没有的第三根轴）
        _quantile_rank("vol60").alias("p_vol60"),
    ]).with_columns([
        _assign_arena(),
        _assign_level().alias("level"),
        _risk_level(),
        _risk_reason(),
        (pl.col("turnover_rate") / pl.col("turn_med").clip(lower_bound=1e-6))
        .alias("turn_ratio"),
    ])

    # ---- 结构分（heat）与阶段（stage）：触点漏斗用 ----
    #
    # heat 的前三项是结构（20 日中位/60 日窗口），第四项 p_amount 是**当日**成交额分位，
    # 所以 heat 会随放量跳动。这是有意的：页面的问题是「今天看哪只」，不是「长期质地」。
    # 结构分不是概率 —— 没有校准就不写概率，只用于排序。
    df = df.with_columns([
        _quantile_rank("amp20").alias("p_amp"),
    ]).with_columns([
        ((pl.col("p_amp").fill_null(0)
          + pl.col("p_turn_med")
          + (1.0 - pl.col("p_scale"))
          + pl.col("p_amount")) / 4.0).alias("heat"),
        _assign_stage(),
    ])

    # ---- 属性标签：财务（PIT）+ 行业 + 行业内分位 ----
    fin = _financials(data_dir, latest)
    ind = _industries(data_dir)
    df = df.join(fin, on="symbol", how="left").join(ind, on="symbol", how="left")
    if "gross_margin" in df.columns and "industry" in df.columns:
        df = df.with_columns(
            pl.col("gross_margin").median().over("industry").alias("_gm_ind")
        )
    else:
        df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("_gm_ind"))

    # 行业内分位（龙头用）。行业为空时 over(null) 会退化，故先判空再算。
    if "industry" in df.columns:
        df = df.with_columns([
            (pl.col("mcap_med").rank("ordinal").over("industry")
             / pl.col("mcap_med").count().over("industry")).alias("_pi_mcap"),
            (pl.col("ret20").rank("ordinal").over("industry")
             / pl.col("ret20").count().over("industry")).alias("_pi_ret20"),
        ])
    else:
        df = df.with_columns([
            pl.lit(None).cast(pl.Float64).alias("_pi_mcap"),
            pl.lit(None).cast(pl.Float64).alias("_pi_ret20"),
        ])
    df = df.with_columns(_tag_flags())

    # 等级迁移：前一交易日用自己的横截面算 prev_level（与当日口径一致）。
    prev = None
    i = bisect.bisect_left(days, latest)
    if i > 0:
        prev = days[i - 1]
    transition_expr = pl.lit("new").cast(pl.Utf8).alias("transition")
    prev_level_expr = pl.lit(None, dtype=pl.Utf8).alias("prev_level")
    if prev is not None:
        prev_day = _read_day(data_dir, prev)
        if not prev_day.is_empty():
            prev_df = (
                prev_day.join(inst, on="symbol", how="left")
                .filter(
                    (pl.col("close") > 0) & (pl.col("amount") > 0)
                    & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
                )
                .with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
                .with_columns([
                    _quantile_rank("turnover_rate").alias("p_turnover"),
                    _quantile_rank("amount").alias("p_amount"),
                ])
                .with_columns(_assign_level().alias("prev_level"))
                .select(["symbol", "prev_level"])
            )
            df = df.join(prev_df, on="symbol", how="left")

            order = {lv: i for i, lv in enumerate(LEVEL_ORDER)}
            df = df.with_columns([
                pl.col("level").replace_strict(order, default=99).alias("_lvl_rank"),
                pl.col("prev_level").replace_strict(order, default=99).alias("_prev_rank"),
            ])
            transition_expr = (
                pl.when(pl.col("prev_level").is_null())
                .then(pl.lit("new"))
                .when(pl.col("_prev_rank") > pl.col("_lvl_rank"))
                .then(pl.lit("up"))
                .when(pl.col("_prev_rank") < pl.col("_lvl_rank"))
                .then(pl.lit("down"))
                .otherwise(pl.lit("stay"))
                .alias("transition")
            )
        else:
            prev = None

    df = df.with_columns(prev_level_expr, transition_expr)

    arena_order = {a: i for i, a in enumerate(ARENA_ORDER)}
    rows = (
        df.with_columns(_tags_array().alias("tags"))
        .select([
            "symbol", "name", "arena", "level", "tags", "stage",
            "industry", "mcap", "turnover_rate", "amount",
            "turn_med", "turn_ratio", "n_days", "amp20", "lu20", "heat",
            "ret60", "listing_days",
            "p_scale", "p_turn_med", "p_turnover", "p_amount", "p_amp",
            "vol60", "ret20", "lu_days",
            "roe", "revenue_yoy", "net_income_yoy", "gross_margin",
            "debt_to_asset_ratio", "report_period",
            "prev_level", "transition",
            "risk", "risk_reason",
            "t_bluechip", "t_whitehorse", "t_leader", "t_yaogu",
        ])
        .with_columns(pl.col("arena").replace_strict(arena_order, default=99).alias("_ar"))
        .sort(["_ar", "amount"], descending=[False, True])
        .drop("_ar")
    )

    summary = _summary(rows)
    # 全量 ST 名单里，当日无成交（停牌/退市整理）的票不会进 rows —— 它们
    # 在页面上完全不可见，但持仓者最需要知道「我手里这只是 ST 且已经卖不掉」。
    st_all = inst.filter(_is_st_name()).height if "name" in inst.columns else 0
    risk_high_n = int((df["risk"] == "high").sum())
    summary["risk_absent"] = max(0, int(st_all) - risk_high_n)
    summary["financial_period"] = (
        str(df["report_period"].drop_nulls().max())
        if "report_period" in df.columns and df["report_period"].drop_nulls().len() else None
    )

    backing = _seat_backing(data_dir, latest)
    focus = _focus(rows, backing)
    focus["emotion"] = _market_emotion(data_dir, days, latest)
    # 席位接力榜：今日被高声誉席位净买入的票。与冷启动触点正交——
    # 触点是「还没动」的预测，席位接力是「已经动、谁在买」的背书。
    if backing:
        nm = dict(inst.select(["symbol", "name"]).iter_rows()) if "name" in inst.columns else {}
        focus["seat_backing"] = sorted(
            ({"symbol": s, "name": nm.get(s), **v} for s, v in backing.items()),
            key=lambda x: -x["rep20"],
        )
    else:
        focus["seat_backing"] = []

    from app.services import shortline_audit  # 局部导入：本模块被研究脚本引用，避免模块级环
    focus["sort_audit"] = shortline_audit.load_artifact(data_dir)

    return {
        "as_of": latest.isoformat(),
        "prev_as_of": prev.isoformat() if prev else None,
        "window_days": ARENA_WINDOW,
        "stats_window": STATS_WINDOW,
        "summary": summary,
        "focus": focus,
        "rows": rows.to_dicts(),
        "disclaimer": (
            "主场 = 20 日换手率中位 × 20 日流通市值中位 的当日横截面分位，"
            "不是当日快照：当日读数只说明「今天」，中位才说明「平时」。"
            "属性标签（蓝筹/白马/龙头/妖股）不是分类而是**过滤条件**：它们可以同时命中"
            "（实测 18 只同时命中 3 个），且大部分落在主场内部 —— 标签的价值是收窄，不是分组。"
            "标签的左尾与中位取自 2024-01..2026-10 共 34 个基期的 fwd20；"
            "白马依赖财报，只有单期数据、无法做历史验证，故不给历史读数。"
            "触点是**排序**不是概率：结构分 heat 由振幅/换手/市值/当日成交额四项分位等权，"
            "无校准，不能读成「上涨概率」。唯一的历史读数是：冷启动段 top20 在 2026-06..11 "
            "样本外的未来 5 日打板率为基准的 6.98 倍（23.3bp → 162bp），"
            "而全市场 top20（不剔已涨停）里有 95.9% 已在 20 日内涨停过 —— 那是追高榜。"
            "ST 与北交所不进触点：ST 的振幅系数仅为主板的 1/5（0.074 vs 0.373），"
            "北交所训练期正样本仅 15 个，均不足以支撑预测。"
            "次新（上市 <250 日）与下跌中继（60 日跌超 10%）也不进：前者未解禁换手虚高，"
            "后者高波动来自出货不是进攻 —— 这两条是逻辑否定，不是模型打分。"
        ),
    }


def _market_emotion(data_dir: Path, days: list[date_cls], latest: date_cls) -> dict:
    """市场情绪闸门 —— 连板晋级率在近期窗口的分位。

    连板晋级率 = 今日连板数(consecutive_limit_ups>=2) / 昨日涨停数(>=1)。
    这是「状态依赖」的核心维度（analysis/_diag_emotion_state.py 验证：高晋级日
    打板次日高开溢价 +278bp vs 低晋级日 +184bp，差 95bp，单调）。

    状态：今日晋级率在近 120 个交易日的分位。
      >= 0.67 主升期(绿灯，触点照常) / 0.33~0.67 中性(黄灯) / < 0.33 退潮期(红灯，空仓)。
    只读 consecutive_limit_ups 一列，120 分区 ~0.2s。
    """
    win = _window(days, latest, 120)
    frames: list[pl.DataFrame] = []
    for d in win:
        f = Path(data_dir) / "kline_daily_enriched" / f"date={d.isoformat()}" / "part.parquet"
        if not f.exists():
            continue
        frames.append(pl.read_parquet(f, columns=["date", "consecutive_limit_ups"]))
    if len(frames) < 30:
        return {"available": False, "reason": "交易日不足 30 个"}
    panel = pl.concat(frames, how="vertical_relaxed")
    daily = (
        panel.group_by("date").agg([
            (pl.col("consecutive_limit_ups") >= 1).sum().alias("n_lu"),
            (pl.col("consecutive_limit_ups") >= 2).sum().alias("n_lianban"),
        ])
        .sort("date")
        .with_columns((pl.col("n_lianban") / pl.col("n_lu").shift(1)).alias("upgrade_rate"))
        .drop_nulls(["upgrade_rate"])
    )
    if daily.is_empty():
        return {"available": False, "reason": "无有效晋级率"}
    today_rate = float(daily["upgrade_rate"].tail(1)[0])
    today_n_lu = int(daily["n_lu"].tail(1)[0])
    today_n_lb = int(daily["n_lianban"].tail(1)[0])
    rates = daily["upgrade_rate"].to_list()
    pct = (sum(1 for x in rates if x <= today_rate) - 1) / max(len(rates) - 1, 1)
    pct = max(0.0, min(1.0, pct))
    state = "green" if pct >= 0.67 else ("amber" if pct >= 0.33 else "red")
    label = {"green": "主升期", "amber": "中性", "red": "退潮期"}[state]
    return {
        "available": True,
        "upgrade_rate": round(today_rate, 4),
        "n_lu_today": today_n_lu,
        "n_lianban_today": today_n_lb,
        "pct": round(pct, 3),
        "state": state,
        "label": label,
        "window_days": len(daily),
        "hint": {
            "green": "连板晋级率高，打板次日溢价历史 +278bp，触点照常",
            "amber": "连板晋级率中性，谨慎参与",
            "red": "连板晋级率退潮，打板次日溢价历史 +184bp 且退潮期常负，建议空仓",
        }[state],
    }


def _seat_backing(data_dir: Path, as_of: date_cls) -> dict[str, dict]:
    """触点候选的「席位背书」：当日是否有高声誉游资席位净买入。

    席位声誉 = 该营业部过去 20 次上榜买入的平均未来 3 日收益（离线预计算，
    analysis/_build_seat_rep.py，严格无前视）。全市场验证 4 档单调 +818bp，
    候选池内 +154bp，是「谁在买」里唯一被回放背书的正向信号。

    数据源：data/dragon_tiger_seats/date=*.parquet（东财营业部明细，免费接口）。
    龙虎榜 T 日盘后发布 → 用「≤ as_of 的最近席位交易日」的买入榜，无前视。
    缺数据（无席位目录/无当日明细）时返回空 dict，触点照常，只是无背书。
    """
    seat_dir = Path(data_dir) / "dragon_tiger_seats"
    if not seat_dir.is_dir():
        return {}
    try:
        seat_days = sorted(p.stem[5:] for p in seat_dir.glob("date=*.parquet"))
    except OSError:
        return {}
    if not seat_days:
        return {}
    latest_seat = max((d for d in seat_days if d <= as_of.isoformat()), default=None)
    if latest_seat is None:
        return {}
    rep_path = seat_dir / "seat_rep.parquet"
    if not rep_path.exists():
        return {}
    try:
        seats = pl.read_parquet(seat_dir / f"date={latest_seat}.parquet")
        rep = pl.read_parquet(rep_path)
    except Exception:
        return {}
    if seats.is_empty() or rep.is_empty():
        return {}

    # 买入席位 join 声誉，只认样本充足的席位（n>=20，否则 rep20 是「不足窗口」的噪声）
    if "n" not in rep.columns:
        return {}
    rep_ok = rep.filter((pl.col("n") >= 20) & pl.col("rep20").is_not_null())
    if rep_ok.is_empty():
        return {}
    buy = seats.filter((pl.col("side") == "buy") & (pl.col("buy") > 0))
    m = buy.join(rep_ok.select(["dept_code", "dept_name", "rep20"]), on="dept_code", how="inner")
    if m.is_empty():
        return {}
    # 每票取最高声誉席位（净买入为正）
    top = (
        m.filter(pl.col("net") > 0)
        .sort("rep20", descending=True)
        .group_by("symbol", maintain_order=True).first()
        .filter(pl.col("rep20") >= 0.03)   # 只背书「声誉明显为正」的席位
        .select(["symbol", "dept_name", "rep20", "net"])
    )
    out: dict[str, dict] = {}
    for r in top.to_dicts():
        out[r["symbol"]] = {
            "seat_name": r["dept_name"],
            "rep20": round(float(r["rep20"]), 4),
            "net": float(r["net"]) if r["net"] is not None else None,
            "date": latest_seat,
        }
    return out


def _focus(rows: pl.DataFrame, backing: dict[str, dict] | None = None) -> dict:
    """注意力预算：全市场 → 结构达标 → 冷启动 → 今日触点。

    每一层都显式返回「删掉了什么」，因为漏斗的价值不在剩下的 15 只，
    而在「为什么其余 5,400 只今天不该看」。没有这一层，页面只是又一个全市场列表。
    """
    stages_count = {k: 0 for k in STAGE_ORDER}
    if rows.is_empty():
        return {
            "budget": {"market": 0, "struct": 0, "cold": 0, "picks": 0},
            "stages": stages_count,
            "picks": [],
            "pool": [],
            "excluded": {"st": 0, "bse": 0, "thin": 0, "young": 0, "falling": 0,
                         "hot": 0, "warming": 0, "fading": 0},
        }
    for key, n in rows.group_by("stage").len().iter_rows():
        if key in stages_count:
            stages_count[str(key)] = int(n)

    bse = _is_bse()
    is_st = pl.col("risk") == "high"
    struct = rows.filter(pl.col("heat") >= HEAT_CUT)
    cold = struct.filter(pl.col("stage") == "cold")
    # 逻辑否定三道（都只靠方向，不靠模型）：
    #   次新（上市 < 250 日）—— 未解禁的高换手是制度虚高，且解禁盘是悬顶；
    #   下跌中继（60 日跌超 10%）—— 高波动来自出货，不是进攻；
    #   无 20 日收益（数据缺失，无法判阶段）。
    mature = pl.col("listing_days").is_null() | (pl.col("listing_days") >= FOCUS_MIN_LISTING_DAYS)
    not_falling = pl.col("ret60").is_null() | (pl.col("ret60") >= FOCUS_MIN_RET60)
    cold_ok = cold.filter(
        ~is_st & ~bse
        & (pl.col("amount") >= FOCUS_MIN_AMOUNT)
        & mature
        & not_falling
        & pl.col("ret20").is_not_null()
    )

    picks = (
        cold_ok.sort("heat", descending=True)
        .head(FOCUS_TOP_N)
        .with_columns(_reason_array().alias("reasons"))
        .select([
            "symbol", "name", "arena", "stage", "industry", "risk",
            "heat", "amp20", "turn_med", "amount", "mcap", "ret20", "ret60",
            "listing_days", "lu_days",
            "p_amp", "p_turn_med", "p_amount", "reasons",
        ])
    )

    # 排序体检需要池子本身的成员与 heat 四项分位（零分布 = 同日池内随机 15 只）。
    # 只暴露已有列，不新增计算；页面不展示，登记脚本用。
    pool = cold_ok.select([
        "symbol", "heat", "p_amp", "p_turn_med", "p_scale", "p_amount", "amount",
    ]).sort("heat", descending=True)

    base_ok = ~is_st & ~bse & (pl.col("amount") >= FOCUS_MIN_AMOUNT)
    excluded = {
        "st": int(struct.filter(is_st).height),
        "bse": int(struct.filter(~is_st & bse).height),
        "thin": int(cold.filter(~is_st & ~bse & (pl.col("amount") < FOCUS_MIN_AMOUNT)).height),
        "young": int(cold.filter(base_ok & (~mature | pl.col("ret20").is_null())).height),
        "falling": int(cold.filter(base_ok & mature & ~not_falling & pl.col("ret20").is_not_null()).height),
        "hot": stages_count.get("hot", 0),
        "warming": stages_count.get("warming", 0),
        "fading": stages_count.get("fading", 0),
    }
    picks_data = picks.to_dicts()
    if backing:
        for r in picks_data:
            r["seat_backing"] = backing.get(r["symbol"])
    return {
        "budget": {
            "market": int(rows.height),
            "struct": int(struct.height),
            "cold": int(cold_ok.height),
            "picks": int(picks.height),
        },
        "stages": stages_count,
        "picks": picks_data,
        "pool": pool.to_dicts(),
        "excluded": excluded,
    }


def _summary(rows: pl.DataFrame) -> dict:
    """主场分布（含各格等级构成）+ 属性标签计数 + 等级/风险/迁移计数。"""
    empty_risk = {k: 0 for k in RISK_ORDER}
    if rows.is_empty():
        return {
            "total": 0, "arenas": [], "levels": [], "tags": [],
            "stages": {k: 0 for k in STAGE_ORDER},
            "market": MARKET_RISK,
            "transitions": {"up": 0, "down": 0, "stay": 0, "new": 0},
            "risk": empty_risk, "risk_absent": 0, "financial_period": None,
        }
    total = rows.height

    # 属性标签：计数 + 已验证的历史风险读数（白马无背书，dd20 为 None）。
    tags = []
    for key in TAG_ORDER:
        col = f"t_{key}"
        n = int(rows[col].sum()) if col in rows.columns else 0
        risk = TAG_RISK[key]
        tags.append({
            "tag": key,
            "label": TAG_LABELS[key],
            "hint": TAG_HINTS[key],
            "count": n,
            "share": round(n / total, 4) if total else 0.0,
            "dd20": risk["dd20"] if risk else None,
            "med_ret20": risk["med_ret20"] if risk else None,
            "validated": risk is not None,
        })

    # 主场：一次 group_by 拿 count，再一次拿 (arena, level) 交叉计数。
    count_by_arena = dict(rows.group_by("arena").len().iter_rows())
    by_pair: dict[tuple[str, str], int] = {}
    for a, lv, n in rows.group_by(["arena", "level"]).len().iter_rows():
        by_pair[(a, lv)] = int(n)
    arenas = []
    for key in ARENA_ORDER:
        n = int(count_by_arena.get(key, 0))
        levels = {lv: by_pair.get((key, lv), 0) for lv in LEVEL_ORDER}
        arenas.append({
            "arena": key,
            "label": ARENA_LABELS[key],
            "hint": ARENA_HINTS[key],
            "count": n,
            "share": round(n / total, 4) if total else 0.0,
            "dd20": ARENA_RISK[key]["dd20"],
            "med_ret20": ARENA_RISK[key]["med_ret20"],
            "levels": levels,
        })

    # 等级计数（全市场，作 chip 图例用）。
    count_by_level = dict(rows.group_by("level").len().iter_rows())
    levels = [
        {
            "level": lv,
            "label": LEVEL_LABELS[lv],
            "hint": LEVEL_HINTS[lv],
            "count": int(count_by_level.get(lv, 0)),
            "share": round(int(count_by_level.get(lv, 0)) / total, 4),
        }
        for lv in LEVEL_ORDER
    ]

    risk_map: dict[str, int] = {k: 0 for k in RISK_ORDER}
    for key, n in rows.group_by("risk").len().iter_rows():
        if key in risk_map:
            risk_map[str(key)] = int(n)

    transitions = {"up": 0, "down": 0, "stay": 0, "new": 0}
    for key, n in rows.group_by("transition").len().iter_rows():
        if key in transitions:
            transitions[str(key)] = int(n)

    stages: dict[str, int] = {k: 0 for k in STAGE_ORDER}
    for key, n in rows.group_by("stage").len().iter_rows():
        if key in stages:
            stages[str(key)] = int(n)

    return {
        "total": total, "arenas": arenas, "levels": levels, "tags": tags,
        "stages": stages,
        "market": MARKET_RISK,
        "transitions": transitions, "risk": risk_map,
    }


def _empty(as_of: date_cls | None = None) -> dict:
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "prev_as_of": None,
        "window_days": ARENA_WINDOW,
        "stats_window": STATS_WINDOW,
        "summary": {
            "total": 0, "arenas": [], "levels": [], "tags": [],
            "stages": {k: 0 for k in STAGE_ORDER},
            "market": MARKET_RISK,
            "transitions": {"up": 0, "down": 0, "stay": 0, "new": 0},
            "risk": {k: 0 for k in RISK_ORDER}, "risk_absent": 0,
            "financial_period": None,
        },
        "focus": _focus(pl.DataFrame()),
        "rows": [],
        "disclaimer": "",
    }


def get_citizenship(data_dir: Path, as_of: date_cls | None = None) -> dict:
    """带缓存的入口。缓存键 = (data_dir, as_of)。"""
    key = (str(data_dir), as_of.isoformat() if as_of else None)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    result = compute(data_dir, as_of)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = result
    return result
