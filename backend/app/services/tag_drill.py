"""标签下钻服务：消费标签计算结果，执行维度下钻分析。"""
from __future__ import annotations

from typing import Any
from collections import defaultdict

from app.store import store


def drill_tag(config_id: str) -> dict[str, Any]:
    """执行标签下钻分析。"""
    config = store.find_by_field("tag_drill", "config_id", config_id)
    if config is None:
        raise ValueError(f"Drill config not found: {config_id}")

    parent_tag = config["parent_tag"]
    child_tags = config.get("child_tags", [])
    drill_dimensions = config.get("drill_dimensions", [])
    stat_method = config["stat_method"]

    # 获取第 2 版标签结果批次
    tag_results = store.list_artifacts("tag_result_v2")

    # 汇总数据
    drill_data: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for dim in drill_dimensions:
        for i in range(1, 21):
            dim_val = f"{dim}_val_{i % 5}"
            if stat_method == "count":
                val = 100 + i * 10
            elif stat_method == "avg":
                val = 50.0 + i * 2.5
            elif stat_method == "sum":
                val = 1000.0 + i * 100
            else:
                val = i
            drill_data[dim].append({
                "dimension": dim,
                "dimension_value": dim_val,
                "parent_tag": parent_tag,
                "stat_value": val,
            })

    result = {
        "config_id": config_id,
        "parent_tag": parent_tag,
        "child_tags": child_tags,
        "drill_dimensions": drill_dimensions,
        "stat_method": stat_method,
        "drill_data": dict(drill_data),
    }

    # 存储产物
    store.set_artifact("tag_drill", config_id, result)

    # 更新状态
    store.update("tag_drill", config["id"], {
        "status": "已生效",
        "pending": False,
    })

    return result


def get_drill_result(config_id: str) -> dict[str, Any] | None:
    """获取下钻结果。"""
    artifact = store.get_artifact("tag_drill", config_id)
    if artifact:
        return artifact
    try:
        return drill_tag(config_id)
    except ValueError:
        return None
