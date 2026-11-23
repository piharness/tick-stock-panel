"""行业 ETF 盘后扫描读数 API —— 只读产出物，不重算。

产出者是 `analysis/etf_watch.py`（盘后 CLI），落盘在 `DATA_DIR/etf_watch/latest.json`。
本路由不在这里复算 dd/波动分位：判定口径的唯一产地在 `analysis/_diag_industry_etf_asym.py`，
后端若再算一遍，同一个「跌破 35%」就有了两个答案。

GET /api/etf-watch → 最新一份扫描快照 + 事件账本 + V1 预注册判据读数
GET /api/etf-watch/daily/{date} → 某一交易日的冻结快照（不可改写）
GET /api/etf-watch/replay → 历史回放与信号追踪（analysis/etf_watch_replay.py 产出）
GET /api/etf-watch/account → 运营账户状态与次日指令（analysis/etf_watch_account.py 产出）
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.config import settings

router = APIRouter(prefix="/api/etf-watch", tags=["etf-watch"])


def _root(request: Request) -> Path:
    override = getattr(request.app.state, "data_dir", None)
    return Path(override) if override else Path(settings.data_dir)


def _load(path: Path) -> dict:
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"还没有扫描结果（{path.name} 不存在）：先跑 `python analysis/etf_watch.py`",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"扫描结果不是合法 JSON：{exc}") from exc


def _store_last_session(request: Request) -> str | None:
    """本地 ETF 日线覆盖到的最后一个交易日。

    只读分区目录名（日分区从名字直读，不扫 parquet），失败一律 None：
    这一行是给页面判「落后」用的元数据，宁可没有，也不能报一个假日期。
    """
    try:
        from app.tickflow.partitions import list_partition_dates

        days = list_partition_dates(_root(request) / "kline_etf_daily")
        return max(days).isoformat() if days else None
    except Exception:  # noqa: BLE001 —— 元数据失败不能影响扫描结果本身
        return None


@router.get("")
def get_etf_watch(request: Request) -> dict:
    payload = _load(_root(request) / "etf_watch" / "latest.json")
    # 附带「本地 ETF 日线覆盖到哪一天」。这不是判定字段（不参与 F1/F2/F3/F6），
    # 只回答页面必须回答的一个问题：扫描落后了，还是本地行情本来就没到？
    # 2026-11-23 的事故：latest.json 停在 11-20、本地分区已有 11-23，
    # 页面却没有任何地方说得出「今天没跑」—— 缺的不是计算，是这条元数据。
    payload.setdefault("meta", {})["store_last_session"] = _store_last_session(request)
    return payload


@router.get("/daily/{day}")
def get_etf_watch_day(request: Request, day: str) -> dict:
    # 只接受 YYYY-MM-DD，避免路径穿越（day 直接进文件名）
    if len(day) != 10 or day[4] != "-" or day[7] != "-" or not (day[:4] + day[5:7] + day[8:]).isdigit():
        raise HTTPException(status_code=400, detail="day 必须是 YYYY-MM-DD")
    return _load(_root(request) / "etf_watch" / "daily" / f"{day}.json")


@router.get("/replay")
def get_etf_watch_replay(request: Request) -> dict:
    """历史回放是独立产出物：逐日 scan 由 analysis 脚本完成，API 只读，不在这里重算。"""
    path = _root(request) / "etf_watch" / "replay.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="还没有历史回放（replay.json 不存在）：先跑 `python analysis/etf_watch_replay.py`",
        )
    return _load(path)


@router.get("/account")
def get_etf_watch_account(request: Request) -> dict:
    """运营账户（腿 1 + 债券叠加）状态与次日指令。只读产出物，引擎在 analysis 侧。"""
    path = _root(request) / "etf_watch" / "account.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="还没有运营账户快照（account.json 不存在）：先跑 `python analysis/etf_watch_account.py`",
        )
    return _load(path)


@router.get("/cash-leg")
def get_etf_watch_cash_leg(request: Request) -> dict:
    """现金腿下单计划（短融打底 + 国债锚）。

    取整只发生在 `analysis/etf_watch_cash_leg.py::plan`；这里读产出物，不按权益重算一份，
    否则页面上的份数与账户脚本会各自取整一次 —— 100 份整手和佣金下限任何一处不同就是两个答案。
    """
    path = _root(request) / "etf_watch" / "cash_leg.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="还没有现金腿计划（cash_leg.json 不存在）：先跑 `python analysis/etf_watch_account.py`"
                   "（它会在写完账户快照后顺带生成）",
        )
    return _load(path)
