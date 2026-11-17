"""操盘档案 API — 判据库 / 统计 / 异常结构扫描 / 个股解剖 / 案例库。

只读本地 `kline_daily_enriched`、`dragon_tiger`、`instruments`，并把用户手工录入的
案例写进 `data/case_lab/cases/`。不采集任何新数据源。

重算入口：`python -m app.services.case_lab --write`（判据统计落盘后才秒开）。

接口分工：
  GET  /fingerprints        判据清单（定义、阈值、反例）
  GET  /stats               判据的全历史统计（n / 超额 / 中位数 / 胜率 / t）
  POST /stats/rebuild       重算并落盘
  GET  /scan                最近 N 日的结构异常候选（不是推荐，不是指控）
  GET  /dissect             单只票区间的逐日痕迹
  GET  /cases               案例列表
  POST /cases               新建 / 更新案例
  GET  /cases/{id}          案例详情
  GET  /cases/{id}/reconcile 叙事与痕迹对账
"""
from __future__ import annotations

from datetime import date as date_cls
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.services import case_lab

router = APIRouter(prefix="/api/case-lab", tags=["case-lab"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


def _parse_date(value: str | None, field: str) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"{field} 格式应为 YYYY-MM-DD, 收到: {value}") from exc


@router.get("/fingerprints")
def get_fingerprints():
    """判据库。阈值与展示文本来自同一份 params，不会出现「文档 2%、代码 3%」。"""
    return {
        "items": case_lab.fingerprint_catalog(),
        "limit_gap": case_lab.LIMIT_GAP,
        "min_hits": case_lab.MIN_HITS,
        "note": (
            "判据是「值得看一眼」的结构描述，不是操纵的证据。"
            "每条都写明它在什么情况下会给出错误印象（caveat）。"
        ),
    }


@router.get("/stats")
def get_stats(request: Request, compute: bool = Query(default=True)):
    """判据统计。首次调用会同步算一次（约 5–10 秒），之后读盘上 JSON。"""
    return case_lab.get_stats(_data_dir(request), allow_compute=compute)


@router.post("/stats/rebuild")
def rebuild_stats(request: Request, years: int = Query(default=case_lab.DEFAULT_STATS_YEARS, ge=1, le=15)):
    payload = case_lab.compute_stats(_data_dir(request), years)
    case_lab.write_stats(_data_dir(request), payload)
    return payload


@router.get("/scan")
def get_scan(
    request: Request,
    as_of: str | None = Query(default=None),
    lookback: int = Query(default=case_lab.DEFAULT_SCAN_LOOKBACK, ge=10, le=120),
    limit: int = Query(default=30, ge=5, le=100),
):
    target = _parse_date(as_of, "as_of")
    return case_lab.scan(_data_dir(request), target, lookback, limit)


@router.get("/dissect")
def get_dissect(
    request: Request,
    symbol: str = Query(..., description="标的代码，如 002086.SZ"),
    start: str = Query(..., description="区间起（YYYY-MM-DD）"),
    end: str = Query(..., description="区间止（YYYY-MM-DD）"),
):
    d0 = _parse_date(start, "start")
    d1 = _parse_date(end, "end")
    if d0 is None or d1 is None:
        raise HTTPException(400, "start 与 end 均必填")
    if not symbol.strip():
        raise HTTPException(400, "symbol 不能为空")
    return case_lab.dissect(_data_dir(request), symbol, d0, d1)


# ===== 案例库 =====

ActorType = Literal["hot_money", "cap_mgmt", "dealer", "institution", "unknown"]
CaseStatus = Literal["penalized", "investigated", "alleged", "trace_only"]
EvidenceKind = Literal["penalty", "inquiry", "filing", "leak", "media", "research", "other"]
Reliability = Literal["official", "media", "hearsay", "inferred"]


class EvidenceItem(BaseModel):
    id: str | None = None
    date: str | None = None
    kind: EvidenceKind = "other"
    source: str = ""
    url: str | None = None
    text: str = ""
    reliability: Reliability = "media"

    @field_validator("date")
    @classmethod
    def _date_ok(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        try:
            date_cls.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("date 格式应为 YYYY-MM-DD") from exc
        return v


class PhaseItem(BaseModel):
    label: str = Field(default="", description="用户自己写的阶段名，如 吸筹 / 拉升 / 派发")
    start: str | None = None
    end: str | None = None
    note: str = ""


class CasePayload(BaseModel):
    id: str | None = None
    symbol: str
    name: str | None = None
    title: str = ""
    actor_type: ActorType = "unknown"
    status: CaseStatus = "trace_only"
    start: str | None = None
    end: str | None = None
    thesis: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    phases: list[PhaseItem] = Field(default_factory=list)
    playbook: list[str] = Field(default_factory=list)
    lessons: str = ""

    @field_validator("symbol")
    @classmethod
    def _symbol_ok(cls, v: str) -> str:
        s = (v or "").strip().upper()
        if not s:
            raise ValueError("symbol 不能为空")
        if len(s) > 16 or not all(c.isalnum() or c == "." for c in s):
            raise ValueError("symbol 只允许字母数字与点，如 002086.SZ")
        return s

    @field_validator("start", "end")
    @classmethod
    def _range_ok(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        try:
            date_cls.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("日期格式应为 YYYY-MM-DD") from exc
        return v


@router.get("/cases")
def list_cases(request: Request):
    return {"items": case_lab.list_cases(_data_dir(request))}


@router.get("/cases/{case_id}")
def get_case(request: Request, case_id: str):
    case = case_lab.get_case(_data_dir(request), case_id)
    if case is None:
        raise HTTPException(404, "案例不存在")
    return case


@router.post("/cases")
def save_case(request: Request, payload: CasePayload):
    try:
        doc = case_lab.save_case(_data_dir(request), payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return doc


@router.delete("/cases/{case_id}")
def delete_case(request: Request, case_id: str):
    try:
        ok = case_lab.delete_case(_data_dir(request), case_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "案例不存在")
    return {"deleted": case_id}


@router.get("/cases/{case_id}/reconcile")
def reconcile(request: Request, case_id: str, tol_days: int = Query(default=3, ge=0, le=10)):
    out = case_lab.reconcile(_data_dir(request), case_id, tol_days)
    if out.get("state") == "not_found":
        raise HTTPException(404, "案例不存在")
    return out
