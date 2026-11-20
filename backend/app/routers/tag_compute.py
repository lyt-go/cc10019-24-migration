"""标签计算路由：执行标签计算任务，读取第 2 版结果并管理兼容迁移。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.tag_compute import (
    compute_tag,
    get_compute_result,
    get_compute_result_v2,
    list_compute_results,
    list_v2_batches,
    migrate_legacy_results,
    migration_status,
    recompute_tag,
)
from app.store import store

router = APIRouter(prefix="/api/tag-compute", tags=["tag_compute"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("tag_compute")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("tag_compute")
    return {"items": rows, "total": len(rows)}


# ========== 第 2 版结果与兼容迁移 ==========

@router.get("/results/v2")
def v2_batches() -> dict[str, Any]:
    """列出所有第 2 版结果批次（含迁移来源与覆盖方式）。"""
    items = list_v2_batches()
    return {"items": items, "total": len(items)}


@router.get("/results/v2/{tag_id}")
def v2_result_by_tag(tag_id: str, result_version: str | None = None) -> dict[str, Any]:
    """按标签读取结果；``result_version=v1`` 时返回第 1 版回放数据。"""
    if result_version in ("v1", "1"):
        artifacts = [a for a in list_compute_results() if a.get("tag_id") == tag_id]
        if not artifacts:
            raise HTTPException(status_code=404, detail="Result not found")
        return max(artifacts, key=lambda a: int(a.get("sequence", 0)))
    batches = [b for b in list_v2_batches() if b.get("tag_id") == tag_id]
    if not batches:
        raise HTTPException(status_code=404, detail="Result not found")
    return max(batches, key=lambda b: int(b.get("sequence", 0)))


@router.get("/migration")
def migration() -> dict[str, Any]:
    return migration_status()


@router.post("/migration/run")
def migration_run(interrupt_after: int | None = None) -> dict[str, Any]:
    try:
        return migrate_legacy_results(interrupt_after=interrupt_after)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("tag_compute", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{task_id}/run")
def run(task_id: str) -> ActionResult:
    try:
        result = compute_tag(task_id)
        return ActionResult(ok=True, message="Computed", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{task_id}/result")
def result(task_id: str) -> dict[str, Any]:
    """默认返回第 2 版结果；旧读者可显式取 v1 兼容视图。"""
    res = get_compute_result_v2(task_id)
    if res is None:
        res = get_compute_result(task_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/{task_id}/result/v1")
def result_v1(task_id: str) -> dict[str, Any]:
    res = get_compute_result(task_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/artifacts/all")
def artifacts() -> list[dict[str, Any]]:
    return list_compute_results()


@router.post("/{task_id}/recompute")
def recompute(task_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    scope = payload.get("scope") if payload else None
    try:
        result = recompute_tag(task_id, scope)
        return ActionResult(ok=True, message="Recomputed", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("tag_compute", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
