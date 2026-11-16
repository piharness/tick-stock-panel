"""分钟线补齐的验收判据 —— CLI 薄壳；判据本体在 `app.services.minute_audit`。

## 为什么只剩薄壳（2026-11-13）

判据原先住在这里，而 `analysis/` **不在镜像里**（`Dockerfile` 只 COPY
`backend/app`）—— 于是「明天自动生效」在容器里等于「没有这个判据」，出问题时
也没有修法。现在四条轴（L/M/B/S）、阈值、修法与退出码都在
`backend/app/services/minute_audit.py`，`daily_pipeline` 与本文具调用同一份。

本文件只保留两件 `analysis/` 独有的东西：
1. **长文档**（下面这段实证，是判据的来历，不该随代码搬进服务层）；
2. **产物写法** —— `--json` 要过 `_artifact_guard`（散文由字段生成）。

## 为什么换判据（2026-11-13）

「n == 240」对三种真实故障全部失明：
  * 停牌被补成 240 根 0 量  → 记成「满」；
  * 多一根 + 少一根，净 240  → 记成「满」（一增一减天然抵消）；
  * 全天只有 73 根但每根都在  → 「根数」不比集合，看不出缺时段。

## 两个判据，不许混

**L 标注（时刻集合）** 该 symbol 当日的时刻集合 == 规范网格
  规范网格 = 09:31..11:30 ∪ 13:01..15:00（**按区间末日标注**，240 个）。
  实测 5,570 只/日里 5,530+ 只都落在这个网格上，所以它是数据的既有约定，
  不是本脚本的假设。

**M 金额（跨表对账）** Σ(该 symbol 当日**全部** volume) == 日线 volume
  这是「表里存的东西加起来等不等于交易所的数」。**全量求和，不做网格过滤** ——
  过滤等于把幽灵行从分母里放走，判据就永远绿了，而表里那几行还在。
  附加 amount 同口径对账（volume 相等而 amount 差，说明均价口径错，另一类故障）。

**D 归因（不是判据）** Σ(仅规范网格内的 volume) 与日线的差
  用来回答「M 的差额是不是全部来自网格外那几行」。11-05/11-12 的幽灵行
  就靠这一列定性：D 归零而 M 不归零 ⇒ 差额 = 网格外那几行，且它们的量
  确实被重复计入。

## 外部轴（--external）

本地日线与被检的分钟表可能同源（同一次同步的两个产物），拿它当「真值」是自证。
`--external` 另取 fuyao 日线（`plugins/fuyao`，与本表数据链路独立）。
2026-11-13 实测：55 个可疑 (symbol, day) 组合上本地日线与 fuyao **0 处偏差**，
Σmin 全量偏差 **55/55**，Σmin 规范网格偏差 **1/55** —— 这条轴才是否掉
「是不是本表自己错了」的那一票。默认不联网，只在显式传参时取。

## 分类（决定修法，不是决定红绿）

  phantom_1300  13:00 行且 (open,high,low,close,volume) 与同 symbol 当日 11:30
                行**逐字段相等** ⇒ 11:30 那根被贴了第二个标签。
                amount 与 11:30 差在末两位（round 到 100 元）也一并记录，
                它是「同一根 bar 被两条路径各写一次」的指纹。
  filler_grid   0 量且无日线行（停牌）⇒ 合成网格用了 09:31..11:29 ∪ 13:00..15:00
                （缺 11:30、多 13:00），数量仍是 240 ⇒ 只有 L 看得见。
  volume_gap    网格内 Σ 与日线差 > max(5bp, 100 手) ⇒ 真差异，需人工定性。
                （5bp + 100 手是两条阈值：北交所 920xxx 普遍 Σmin−daily = +1 手，
                 只按比例卡会产出 37 条 ±1 手的假阳性。）

## 另外两条均价轴（2026-11-13 补，都只看**均价与自身价格的相容性**）

**B 收盘 bar** `amount/(volume·100)` vs 该 bar 的 `close`
  14:57–15:00 只有一个价 ⇒ 相等。偏离只能来自不进 OHLC 却进量额的成交。
  判据写**整手残差** `|amount − volume·100·close|/(100·close) > 1 手`，不写百分比：
  volume 整手取整，纯 close 成交的 bar 残差恒 ≤ 1 手（实测 V<1000 的 17,540 根
  max = 0.99），越线的只有 9 根、下一根在 40 手，阈值不敏感。
  写成「dev > 1%」会得到 286 根，其中 277 根手数 < 100（中位 30）——
  那些 1%~29% 全是取整噪声。

**S 日线自身** `amount/(volume·100)` 必须落在**该行** `[low, high]` 内（±2bp）
  6 天 33,430 行里只有 6 行越界、全 BJ；SH/SZ 31,301 行 0 命中。
  它不看外部源就能否掉一行：`920717.BJ 11-06` 均价 6.1666 < low 6.18，
  而 20,580 − 14,152 = 6,428 正好是 fuyao 的值。
  与 fuyao 轴互补：`920640 11-10` / `920964 11-11` / `920375 11-11` 三行
  fuyao 也含那笔量（外部轴恒绿），只有 S 看得见；`920112 11-05` 的成交价
  落在区间内，只有 fuyao 看得见。**单靠任何一轴都不够。**

## 用法

  python analysis/_diag_minute_audit_1113.py                 # 盘上最新 6 个分区
  python analysis/_diag_minute_audit_1113.py --all           # 全部分区
  python analysis/_diag_minute_audit_1113.py --external      # 追加 fuyao 对账
  python analysis/_diag_minute_audit_1113.py --json          # 落产物（过 assert_clean）
  python analysis/_diag_minute_audit_1113.py --repair-phantom            # 只看修法
  python analysis/_diag_minute_audit_1113.py --repair-phantom --apply    # 真改（先备份）

默认窗口取**盘上最新 6 个已完成的分钟分区**（`latest_days()`），不是硬编码日期 ——
否则「明天自动生效」实际是「明天再报一次这一周」。未收盘的今天不算一天：09:30 到
15:05 之间今天的帧天然只有几根，算进去就是 1400+ 条假「标注异常」（窗口规则只看
钟点，15:05 之后今天照样进窗口、照样可以被判红）。

容器里没有 `analysis/`，用同一份判据的模块入口：

  docker exec <c> python -m app.services.minute_audit --repair-phantom --apply

退出码：0 全绿 / 2 仅标注异常 / 3 有金额·量差异 / 4 有非竞价成交（B 或 S 命中）。
3 优先于 4：金额差异更接近「表坏了」，4 更接近「口径未定」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent

_backend = str(ROOT / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from app.services import minute_audit  # noqa: E402

#: 规范网格、阈值、退出码都只有一份，在这里只做转发（不复制值）。
GRID = minute_audit.GRID
GRID_SET = minute_audit.GRID_SET
REL_TOL = minute_audit.REL_TOL
ABS_TOL = minute_audit.ABS_TOL
MIN_VOL = minute_audit.MIN_VOL
PRICE_EPS = minute_audit.PRICE_EPS
WINDOW = minute_audit.WINDOW
latest_days = minute_audit.latest_days
classify = minute_audit.classify
audit_day = minute_audit.audit_day
repair_plan = minute_audit.repair_plan


def _write_artifact(nv: dict) -> None:
    """产物写法留在 analysis/：`reading` 必须由字段生成（`_artifact_guard`）。"""
    from _artifact_guard import assert_clean, render_reading  # noqa: PLC0415

    nv["reading_template"] = (
        "{days} 个交易日：标注异常 {label_bad} 处、金额异常 {money_bad} 处，"
        "其中 {phantom} 行是 13:00 幽灵（11:30 那根的副本）、"
        "{filler} 只是停牌合成网格；扣掉网格外行后仍对不上账的 {gap_bad} 处。"
        "另有 {block_1500} 根收盘 bar 的均价与收盘价不符（不进 OHLC 却进量额的成交），"
        "{daily_bad} 行日线自身的 amount/(volume·100) 已越出该行 low/high。"
        "落盘路径另隔离了 {quarantined} 行越界副本（不进量额，证据留在 data/_quarantine/）。"
    )
    nv["reading_fields"] = {
        "days": "$totals.days", "label_bad": "$totals.label_bad",
        "money_bad": "$totals.money_bad", "phantom": "$totals.phantom",
        "filler": "$totals.filler", "gap_bad": "$totals.gap_bad",
        "block_1500": "$totals.block_1500", "daily_bad": "$totals.daily_bad",
        "quarantined": "$totals.quarantined",
    }
    nv["reading"] = render_reading(nv)
    assert_clean(nv, "minute_audit")
    out = HERE / "_diag_minute_audit_1113_result.json"
    out.write_text(json.dumps(nv, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"=> 产物 {out.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    return minute_audit.main(argv, data_dir=DATA, write_artifact=_write_artifact)


if __name__ == "__main__":
    sys.exit(main())
