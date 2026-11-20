"""用户分群服务：基于标签计算结果生成用户分群。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.schemas import SegmentResult
from app.services.tag_compute import _latest_batch_for_tag, get_result_switch


def compute_segment(segment_id: str) -> SegmentResult:
    """执行分群计算，消费第 2 版标签结果。"""
    segment = store.find_by_field("user_segment", "segment_id", segment_id)
    if segment is None:
        raise ValueError(f"Segment not found: {segment_id}")

    tier_rules = segment.get("tier_rules", [])
    result_version = 2 if get_result_switch() == "v2" else 1

    # 第 2 版按批次里的实际值求分层人数；旧版本仍可用老口径回放。
    tier_distribution: dict[str, int] = {}
    user_count = segment.get("size_estimate", 1000)
    if result_version == 2:
        for rule in tier_rules:
            tag_id = rule.get("tag", "")
            batch = _latest_batch_for_tag(tag_id)
            if batch is None:
                continue
            expected = rule.get("value", True)
            if isinstance(expected, bool) and expected:
                tier_distribution[tag_id] = int(batch.get("hit_count", 0))
            else:
                # 需要未命中集合的规则，只有覆盖了未命中的批次才能给出人数。
                tier_distribution[tag_id] = int(batch.get("miss_count", 0)) if batch.get("miss_covered") else 0
        if tier_distribution:
            user_count = max(tier_distribution.values())
    else:
        tag_results = store.list_artifacts("tag_compute")
        for rule in tier_rules:
            tag_id = rule.get("tag", "")
            matched = sum(1 for r in tag_results if r.get("tag_id") == tag_id)
            tier_distribution[tag_id] = matched if matched > 0 else user_count // max(len(tier_rules), 1)

    if not tier_distribution:
        tier_distribution["default"] = user_count

    result = SegmentResult(
        segment_id=segment_id,
        segment_name=segment["segment_name"],
        user_count=user_count,
        tier_distribution=tier_distribution,
        output_set=f"segment_{segment_id}",
    )
    payload = result.model_dump()
    payload["result_version"] = result_version

    # 存储产物
    store.set_artifact("user_segment", segment_id, payload)

    # 更新状态
    store.update("user_segment", segment["id"], {
        "status": "已就绪",
        "pending": False,
    })

    return result


def get_segment_result(segment_id: str) -> dict[str, Any] | None:
    """获取分群结果。"""
    artifact = store.get_artifact("user_segment", segment_id)
    if artifact:
        return artifact
    try:
        result = compute_segment(segment_id)
        return result.model_dump()
    except ValueError:
        return None
