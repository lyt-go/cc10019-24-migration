"""人群圈选路由：执行圈选、按版本回放名单，并核对结果版本语义。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.crowd_select import get_crowd_result, replay_crowd, select_crowd
from app.services.tag_compute import (
    get_result_switch,
    migrate_legacy_results,
    migration_status,
    set_result_switch,
)
from app.store import store, query_filters

router = APIRouter(prefix="/api/crowd-select", tags=["crowd_select"])


def _parse_version(raw: str | int | None) -> int | None:
    if raw in (None, "", "current"):
        return None
    text = str(raw).strip().lower()
    if text in ("v1", "1"):
        return 1
    if text in ("v2", "2"):
        return 2
    raise HTTPException(status_code=400, detail="result_version 只支持 v1 或 v2")


@router.get("")
def index(request: Request, page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("crowd_select")
    filters=query_filters(request)
    if filters:
        rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in filters.items())]
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


# ========== 标签结果版本与兼容迁移 ==========

@router.get("/tag-results/migration")
def tag_result_migration() -> dict[str, Any]:
    """兼容迁移进度：完成清单、待迁移批次与迁移问题。"""
    return migration_status()


@router.post("/tag-results/migrate")
def tag_result_migrate(interrupt_after: int | None = None) -> dict[str, Any]:
    """执行兼容迁移；``interrupt_after`` 用于验证中断后重新进入。"""
    try:
        return migrate_legacy_results(interrupt_after=interrupt_after)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tag-results/switch")
def tag_result_switch() -> dict[str, Any]:
    return {"switch": get_result_switch(), "migration": migration_status()}


@router.post("/tag-results/switch")
def tag_result_switch_set(version: str) -> dict[str, Any]:
    """切换人群圈选读取的结果版本；未完成迁移时禁止切到 v2。"""
    try:
        return set_result_switch(version)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("crowd_select", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{condition_id}/run")
def run(condition_id: str, result_version: str | None = None) -> ActionResult:
    try:
        result = select_crowd(condition_id, result_version=_parse_version(result_version))
        return ActionResult(ok=True, message="Selected", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{condition_id}/replay")
def replay(condition_id: str) -> ActionResult:
    """旧名单回放：固定按第 1 版结果重算。"""
    try:
        result = replay_crowd(condition_id)
        return ActionResult(ok=True, message="Replayed as v1", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{condition_id}/compare")
def compare(condition_id: str) -> dict[str, Any]:
    """在验证环境同时跑新旧两版圈选，核对两份名单的版本与字段语义。

    只做读取与核对，不改变运行期开关，也不把任一版本的结果写回另一版本的产物键。
    """
    try:
        v1 = replay_crowd(condition_id)
        v2 = select_crowd(condition_id, result_version=2)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    def semantics(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "result_version": result["result_version"],
            "value_type": result["value_type"],
            "matched_count": result["matched_count"],
            "member_count": result["member_count"],
            "interpretable": result["interpretable"],
            "blocked_reason": result["blocked_reason"],
            "batches": result["batches"],
            "selection_key": result["selection_key"],
        }

    v1_users = set(v1["matched_users"])
    v2_users = set(v2["matched_users"])

    # 只留命中者的批次不得凭空出现未命中记录：逐个批次复核。
    hit_only_checks = []
    for tag_id, batch in v2["batches"].items():
        if batch["coverage"] == "hit_only" or batch["coverage"] == "unknown":
            stored = store.get_artifact("tag_result_v2", batch["batch_id"]) or {}
            hit_only_checks.append({
                "tag_id": tag_id,
                "batch_id": batch["batch_id"],
                "coverage": batch["coverage"],
                "miss_count": stored.get("miss_count", 0),
                "miss_covered": stored.get("miss_covered", False),
                "records_all_hits": all(r["hit"] for r in stored.get("records", [])),
                "ok": stored.get("miss_count", 0) == 0 and not stored.get("miss_covered", False),
            })

    mismatched_versions = [
        item for item in v2["conditions"] if item.get("result_version") != 2
    ]
    return {
        "condition_id": condition_id,
        "v1": semantics(v1),
        "v2": semantics(v2),
        "only_in_v1": sorted(v1_users - v2_users),
        "only_in_v2": sorted(v2_users - v1_users),
        "same_users": v1_users == v2_users,
        "hit_only_checks": hit_only_checks,
        "version_consistency": {
            "v1_all_v1": all(c.get("result_version") == 1 for c in v1["conditions"]),
            "v2_all_v2": not mismatched_versions,
            "mismatched": mismatched_versions,
        },
    }


@router.get("/{condition_id}/result")
def result(condition_id: str, result_version: str | None = None) -> dict[str, Any]:
    res = get_crowd_result(condition_id, result_version=_parse_version(result_version))
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("crowd_select", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
