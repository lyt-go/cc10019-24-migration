"""画像卡片服务：消费标签计算和分群结果，生成画像卡片数据。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.tag_compute import get_compute_result_v2
from app.services.user_segment import get_segment_result


def build_profile_card(card_id: str) -> dict[str, Any]:
    """构建画像卡片，消费标签计算和分群结果。"""
    card = store.find_by_field("profile_card", "card_id", card_id)
    if card is None:
        raise ValueError(f"Profile card not found: {card_id}")

    display_tags = card.get("display_tags", [])
    segment_id = card.get("segment_id", "")

    # 获取第 2 版标签结果
    tag_values: list[dict[str, Any]] = []
    for tag_id in display_tags:
        # 查找对应计算任务
        tasks = store.find_all_by_field("tag_compute", "tag_id", tag_id)
        for task in tasks:
            result = get_compute_result_v2(task["task_id"])
            if result:
                tag_values.append({
                    "tag_id": tag_id,
                    "tag_name": _get_tag_name(tag_id),
                    "batch_id": result.get("batch_id"),
                    "result_version": result.get("result_version"),
                    "value_type": result.get("value_type"),
                    "coverage": result.get("coverage"),
                    "member_count": result.get("member_count", 0),
                    "hit_count": result.get("hit_count", 0),
                })

    # 获取分群结果
    segment_info = None
    if segment_id:
        seg_result = get_segment_result(segment_id)
        if seg_result:
            segment_info = {
                "segment_id": segment_id,
                "segment_name": _get_segment_name(segment_id),
                "user_count": seg_result.get("user_count", 0),
                "tier_distribution": seg_result.get("tier_distribution", {}),
            }

    result = {
        "card_id": card_id,
        "card_name": card["card_name"],
        "display_tags": tag_values,
        "segment_info": segment_info,
        "data_source": card["data_source"],
        "display_style": card["display_style"],
    }

    # 存储产物
    store.set_artifact("profile_card", card_id, result)

    # 更新状态
    store.update("profile_card", card["id"], {
        "status": "已发布",
        "pending": False,
    })

    return result


def _get_tag_name(tag_id: str) -> str:
    """获取标签名称。"""
    tag = store.find_by_field("tag_define", "tag_id", tag_id)
    return tag["tag_name"] if tag else tag_id


def _get_segment_name(segment_id: str) -> str:
    """获取分群名称。"""
    seg = store.find_by_field("user_segment", "segment_id", segment_id)
    return seg["segment_name"] if seg else segment_id


def get_profile_result(card_id: str) -> dict[str, Any] | None:
    """获取画像卡片结果。"""
    artifact = store.get_artifact("profile_card", card_id)
    if artifact:
        return artifact
    try:
        return build_profile_card(card_id)
    except ValueError:
        return None
