"""人群圈选服务：基于标签组合条件圈选用户。

上线兼容约定
------------
* 默认读取第 2 版标签结果（``tag_result_v2``），按批次上声明的
  ``value_type`` 解释标签值，并按会员覆盖记录判断每个条件是否可判定。
* 旧名单回放（``version="v1"``）只读第 1 版结果，不触碰第 2 版批次。
* 同一次圈选只会锁定一个结果版本：批次编号、值类型、覆盖方式在判定前解析一次，
  中途不会被另一版本的结果替换，也不会混用两个版本解释同一条件。
* 仅命中批次（``coverage=hit_only``/``unknown``）没有未命中记录：需要否定语义
  的条件直接判为“不可判定”，绝不把未出现的会员当成未命中。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.tag_compute import (
    RESULT_VERSION_V1,
    RESULT_VERSION_V2,
    _coerce_bool,
    _latest_batch_for_tag,
    ensure_legacy_results,
    get_result_switch,
)

_OP_ALIASES = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _normalize_op(op: str | None) -> str | None:
    if not op:
        return None
    return _OP_ALIASES.get(str(op).strip().lower(), str(op).strip())


def _compare(actual: Any, op: str | None, expected: Any, value_type: str) -> bool:
    """按批次声明的值类型比较；类型不一致不猜。"""
    op = _normalize_op(op) or "="
    if value_type == "bool":
        left = _coerce_bool(actual)
        right = _coerce_bool(expected)
        if left is None or right is None:
            raise ValueError(f"布尔语义无法比较：actual={actual!r} expected={expected!r}")
        actual, expected = left, right
    elif value_type in ("int", "float"):
        try:
            actual = float(actual)
            expected = float(expected)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"数值语义无法比较：actual={actual!r} expected={expected!r}") from exc
    else:
        actual, expected = str(actual), str(expected)

    if op == "=":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == ">":
        return actual > expected
    if op == ">=":
        return actual >= expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    raise ValueError(f"不支持的条件操作符：{op}")


def _condition_needs_miss(combo: dict[str, Any]) -> bool:
    """该条件是否要求“未命中会员集合”才能求值。"""
    if _normalize_op(combo.get("op")) == "!=":
        return True
    expected = combo.get("value")
    if isinstance(expected, bool):
        return expected is False
    coerced = _coerce_bool(expected)
    if coerced is not None:
        return coerced is False
    logic = str(combo.get("logic", "") or "").upper()
    return "NOT " in logic


def _logic_negated_tags(logic_expr: str) -> set[str]:
    """表达式里被 NOT 修饰的标签，需要未命中集合才能求值。"""
    import re

    return set(re.findall(r"NOT\s+(TAG-\d+)", (logic_expr or "").upper()))


# ==================== 版本锁定 ====================

def _lock_v2_batches(tag_ids: list[str]) -> dict[str, dict[str, Any]]:
    """在执行前一次性锁定每个标签的最新第 2 版批次。

    标签从未算过、但存在可执行任务时，先补算一次再锁定；补算失败不会伪造未命中。
    """
    from app.services.tag_compute import _computable_task_for_tag, compute_tag

    locked: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    held: list[str] = []
    for tag_id in dict.fromkeys(tag_ids):
        batch = _latest_batch_for_tag(tag_id)
        if batch is None:
            task = _computable_task_for_tag(tag_id)
            if task is not None:
                try:
                    compute_tag(task["task_id"])
                except ValueError:
                    task = None
            if task is not None:
                batch = _latest_batch_for_tag(tag_id)
            else:
                legacy_tasks = [
                    t for t in store.find_all_by_field("tag_compute", "tag_id", tag_id)
                    if t.get("legacy_output_coverage")
                ]
                if legacy_tasks:
                    held.append(tag_id)
        if batch is None:
            missing.append(tag_id)
        else:
            locked[tag_id] = batch
    if held:
        # 有旧输出分区但没有第 2 版批次：先做兼容迁移（只转换旧结果，不重算）。
        from app.services.tag_compute import migrate_legacy_results

        migrate_legacy_results()
        for tag_id in list(held):
            batch = _latest_batch_for_tag(tag_id)
            if batch is not None:
                locked[tag_id] = batch
                held.remove(tag_id)
                if tag_id in missing:
                    missing.remove(tag_id)
    if held:
        raise ValueError(
            "以下标签只有升级前的旧结果，必须先完成兼容迁移才能按新版语义圈选："
            + ", ".join(sorted(held))
        )
    if missing:
        raise ValueError(
            "以下标签尚无第 2 版结果，无法按新版语义圈选：" + ", ".join(sorted(missing))
        )
    return locked


def _lock_v1_artifacts(tag_ids: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """执行前锁定第 1 版结果，用于旧活动名单回放。

    返回 ``(可用结果, 缺失结果的标签)``。缺失的标签不补算、不读新版，
    由调用方把这次回放标成“无法回放”，而不是静默给出空名单。
    """
    ensure_legacy_results()
    artifacts: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for tag_id in dict.fromkeys(tag_ids):
        rows = [
            a for a in store.list_artifacts("tag_compute")
            if a.get("tag_id") == tag_id
            and a.get("result_version", RESULT_VERSION_V1) == RESULT_VERSION_V1
            and a.get("source_result_version") != RESULT_VERSION_V2
        ]
        if not rows:
            missing.append(tag_id)
        else:
            artifacts[tag_id] = max(rows, key=lambda a: int(a.get("sequence", 0)))
    return artifacts, missing


def _collected_members(artifacts: list[dict[str, Any]]) -> set[str]:
    members: set[str] = set()
    for artifact in artifacts:
        members.update(_v1_members(artifact))
    return members


# ==================== 第 2 版求值 ====================

def _v2_members(batches: list[dict[str, Any]]) -> set[str]:
    """所有参数批次记录过的会员并集。"""
    members: set[str] = set()
    for batch in batches:
        members.update(r["user_id"] for r in batch.get("records", []))
    return members


def _batch_detail(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": batch.get("batch_id"),
        "result_version": batch.get("result_version", RESULT_VERSION_V2),
        "source_version": batch.get("source_version"),
        "value_type": batch.get("value_type", "bool"),
        "coverage": batch.get("coverage", "unknown"),
        "sequence": batch.get("sequence"),
    }


def _v2_truth_set(batch: dict[str, Any], members: set[str]) -> tuple[set[str], set[str], list[str]]:
    """布尔标签的真值集合，以及“未覆盖/未计算”的会员。

    返回 ``(hits, undetermined, reasons)``。未出现在批次里的会员既不算真也不算假。
    """
    records = {r["user_id"]: r for r in batch.get("records", [])}
    hits: set[str] = set()
    undetermined: set[str] = set()
    reasons: list[str] = []
    for user_id in members:
        record = records.get(user_id)
        if record is None:
            # 未出现在批次里 = 未计算（或该批次没有覆盖到这位会员）。
            undetermined.add(user_id)
            continue
        if not record.get("computed", True):
            # 显式标了 computed=False：未计算，既不是命中也不是未命中。
            undetermined.add(user_id)
            continue
        if _coerce_bool(record.get("tag_value")) is True:
            hits.add(user_id)
    if undetermined and not batch.get("miss_covered", False):
        reasons.append(
            f"标签 {batch.get('tag_id')} 的批次只覆盖命中会员，未出现的会员保持未知"
        )
    return hits, undetermined, reasons


def _v2_combination_set(
    combo: dict[str, Any], batch: dict[str, Any], members: set[str]
) -> tuple[set[str], set[str], dict[str, Any]]:
    """按组合条件（op/value）求值，用于非布尔值类型或无条件表达式的情况。"""
    value_type = batch.get("value_type", "bool")
    records = {r["user_id"]: r for r in batch.get("records", [])}
    hits: set[str] = set()
    undetermined: set[str] = set()
    detail = {
        "tag_id": combo.get("tag_id", ""),
        **_batch_detail(batch),
        "op": combo.get("op"),
        "value": combo.get("value"),
        "interpretable": True,
        "reason": "",
    }
    if _condition_needs_miss(combo) and not batch.get("miss_covered", False):
        detail["interpretable"] = False
        detail["reason"] = "批次只覆盖命中会员，缺少未命中记录，否定条件无法判定"
        return set(), set(members), detail

    for user_id in members:
        record = records.get(user_id)
        if record is None or not record.get("computed", True):
            undetermined.add(user_id)
            continue
        if _compare(record.get("tag_value"), combo.get("op"), combo.get("value"), value_type):
            hits.add(user_id)

    detail["undetermined_count"] = len(undetermined)
    if undetermined and _condition_needs_miss(combo):
        detail["interpretable"] = False
        detail["reason"] = "存在未覆盖会员，否定条件无法给出确定名单"
    return hits, undetermined, detail


def _apply_logic(logic_expr: str, per_tag: dict[str, set[str]], universe: set[str]) -> set[str]:
    """按逻辑表达式组合各标签真值集合，支持 AND/OR/NOT 与括号。"""
    expr = (logic_expr or "").strip()
    if not expr:
        return set(universe)

    def evaluate(text: str) -> set[str]:
        text = text.strip()
        if text.startswith("(") and text.endswith(")"):
            return evaluate(text[1:-1])
        if text.upper().startswith("NOT "):
            return universe - evaluate(text[4:])
        or_parts = _split_top(text, "OR")
        if len(or_parts) > 1:
            result: set[str] = set()
            for part in or_parts:
                result |= evaluate(part)
            return result
        and_parts = _split_top(text, "AND")
        if len(and_parts) > 1:
            result = set(universe)
            for part in and_parts:
                result &= evaluate(part)
            return result
        return set(per_tag.get(text.strip(), set()))

    return evaluate(expr)


def _split_top(text: str, op: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for token in text.split():
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if token.upper() == op and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += token + " "
    if current.strip():
        parts.append(current.strip())
    return parts if len(parts) > 1 else [text]


def _eval_v2(
    tag_combination: list[dict[str, Any]], logic_expr: str
) -> tuple[set[str], set[str], list[dict[str, Any]], dict[str, Any]]:
    tag_ids = [c.get("tag_id", "") for c in tag_combination if c.get("tag_id")]
    locked = _lock_v2_batches(tag_ids)
    members = _v2_members(list(locked.values()))
    value_types = {b.get("value_type") for b in locked.values()}
    if len(value_types) > 1:
        raise ValueError(
            "同一次圈选涉及的标签值类型不一致，无法用同一套语义解释："
            + ", ".join(sorted(str(v) for v in value_types))
        )
    value_type = next(iter(value_types), "bool")

    details: list[dict[str, Any]] = []
    combined: set[str] = set()

    if value_type == "bool" and (logic_expr or "").strip():
        # 表达式是权威语义：每个标签 token 解释为“该标签为真”的会员集合。
        per_tag: dict[str, set[str]] = {}
        negated = _logic_negated_tags(logic_expr)
        for tag_id, batch in locked.items():
            hits, undetermined, reasons = _v2_truth_set(batch, members)
            per_tag[tag_id] = hits
            # 被否定的标签若缺少未命中记录，本次名单不可判定。
            blocked = tag_id in negated and not batch.get("miss_covered", False)
            detail = {
                "tag_id": tag_id,
                **_batch_detail(batch),
                "op": "=",
                "value": True,
                "interpretable": not blocked,
                "reason": "批次只覆盖命中会员，NOT 条件无法判定" if blocked else "",
                "undetermined_count": len(undetermined),
            }
            if not blocked and undetermined and tag_id in negated:
                detail["interpretable"] = False
                detail["reason"] = "；".join(reasons)
            details.append(detail)
        combined = _apply_logic(logic_expr, per_tag, members)
    else:
        # 没有表达式：按组合条件逐个求值后取交集（旧行为）。
        combined = set(members)
        for combo in tag_combination:
            tag_id = combo.get("tag_id", "")
            hits, undetermined, detail = _v2_combination_set(combo, locked[tag_id], members)
            combined &= hits
            details.append(detail)

    meta = {
        "result_version": RESULT_VERSION_V2,
        "value_type": value_type,
        "batches": {tag_id: _batch_detail(batch) for tag_id, batch in locked.items()},
    }
    return members, combined, details, meta


# ==================== 第 1 版回放 ====================

def _v1_members(artifact: dict[str, Any]) -> set[str]:
    return {u["user_id"] for u in artifact.get("user_tag_values", []) if u.get("user_id")}


def _v1_truth_set(artifact: dict[str, Any], members: set[str]) -> set[str]:
    """旧版本语义：缺行即未命中，这是旧名单当时的口径。"""
    values = {u["user_id"]: u for u in artifact.get("user_tag_values", [])}
    hits: set[str] = set()
    for user_id in members:
        record = values.get(user_id)
        if record is not None and _coerce_bool(record.get("tag_value")) is True:
            hits.add(user_id)
    return hits


def _eval_v1(
    tag_combination: list[dict[str, Any]], logic_expr: str
) -> tuple[set[str], set[str], list[dict[str, Any]], dict[str, Any]]:
    tag_ids = [c.get("tag_id", "") for c in tag_combination if c.get("tag_id")]
    locked, missing = _lock_v1_artifacts(tag_ids)
    members = _collected_members(list(locked.values()))

    if (logic_expr or "").strip():
        per_tag = {tag_id: _v1_truth_set(artifact, members) for tag_id, artifact in locked.items()}
        combined = _apply_logic(logic_expr, per_tag, members)
    else:
        combined = set(members)
        for combo in tag_combination:
            artifact = locked.get(combo["tag_id"])
            if artifact is None:
                continue
            values = {u["user_id"]: u for u in artifact.get("user_tag_values", [])}
            for user_id in members:
                record = values.get(user_id)
                actual = record.get("tag_value") if record else False
                if not _compare(actual, combo.get("op"), combo.get("value"), "bool"):
                    combined.discard(user_id)

    details = [{
        "tag_id": tag_id,
        **_batch_detail(artifact),
        "op": "=",
        "value": True,
        "interpretable": True,
        "reason": "旧版本回放：缺行按旧口径视为未命中",
    } for tag_id, artifact in locked.items()]
    if missing:
        # 旧活动里根本没算过的标签，当年名单无法还原；不补算、不读新版，也不给空名单充数。
        details.append({
            "tag_id": ",".join(sorted(missing)),
            "batch_id": None,
            "result_version": RESULT_VERSION_V1,
            "source_version": None,
            "value_type": "bool",
            "coverage": "missing",
            "op": None,
            "value": None,
            "interpretable": False,
            "reason": "旧结果中没有这些标签的输出，无法按第 1 版回放",
        })
    meta = {
        "result_version": RESULT_VERSION_V1,
        "value_type": "bool",
        "batches": {tag_id: _batch_detail(artifact) for tag_id, artifact in locked.items()},
        "missing_tags": sorted(missing),
    }
    return members, combined, details, meta


# ==================== 对外接口 ====================

def select_crowd(condition_id: str, result_version: int | None = None) -> dict[str, Any]:
    """执行人群圈选。

    ``result_version`` 不传时按运行期开关（默认第 2 版）。一次调用只使用一个版本。
    """
    condition = store.find_by_field("crowd_select", "condition_id", condition_id)
    if condition is None:
        raise ValueError(f"Crowd condition not found: {condition_id}")

    tag_combination = condition.get("tag_combination", [])
    logic_expr = condition["logic_expr"]
    if result_version is None:
        result_version = RESULT_VERSION_V2 if get_result_switch() == "v2" else RESULT_VERSION_V1
    if result_version not in (RESULT_VERSION_V1, RESULT_VERSION_V2):
        raise ValueError(f"不支持的结果版本：{result_version}")

    tag_ids = [c.get("tag_id", "") for c in tag_combination if c.get("tag_id")]
    if not tag_ids:
        raise ValueError("圈选条件未配置标签组合")

    if result_version == RESULT_VERSION_V2:
        members, matched, details, meta = _eval_v2(tag_combination, logic_expr)
    else:
        members, matched, details, meta = _eval_v1(tag_combination, logic_expr)

    interpretable = all(item["interpretable"] for item in details)
    if not interpretable:
        # 不可判定时不给名单，避免下游把残缺结果当成完整名单。
        matched = set()
    uninterpretable = [e for e in details if not e["interpretable"]]

    batch_ids = sorted(str(b.get("batch_id") or b.get("sequence")) for b in meta["batches"].values())
    result = {
        "condition_id": condition_id,
        "condition_name": condition["condition_name"],
        "logic_expr": logic_expr,
        "matched_users": sorted(matched)[:500],
        "matched_count": len(matched),
        "member_count": len(members),
        "output_set": condition["output_set"],
        "tag_combination": tag_combination,
        # 本次圈选实际使用的版本与字段语义，供新旧名单核对。
        **meta,
        "conditions": details,
        "interpretable": interpretable,
        "blocked_reason": "" if interpretable else "；".join(sorted({e["reason"] for e in uninterpretable if e["reason"]})),
        "selection_key": f"{condition_id}:v{result_version}:{','.join(batch_ids)}",
        "missing_tags": meta.get("missing_tags", []),
    }

    store.set_artifact("crowd_select", f"{condition_id}:v{result_version}", result)
    store.set_artifact("crowd_select", condition_id, result)

    if interpretable:
        store.update("crowd_select", condition["id"], {
            "status": "已生效",
            "pending": False,
        })

    return result


def get_crowd_result(condition_id: str, result_version: int | None = None) -> dict[str, Any] | None:
    """获取圈选结果；未指定版本时按当前开关返回对应版本的结果。"""
    if result_version is None:
        result_version = RESULT_VERSION_V2 if get_result_switch() == "v2" else RESULT_VERSION_V1
    artifact = store.get_artifact("crowd_select", f"{condition_id}:v{result_version}")
    if artifact:
        return artifact
    try:
        return select_crowd(condition_id, result_version=result_version)
    except ValueError:
        return None


def replay_crowd(condition_id: str) -> dict[str, Any]:
    """旧活动名单回放：固定使用第 1 版结果，与当前开关无关。

    回放结果单独存放，不覆盖新版结果产物，避免两次读取互相污染。
    """
    result = dict(select_crowd(condition_id, result_version=RESULT_VERSION_V1))
    result["replay"] = True
    store.set_artifact("crowd_select", f"{condition_id}:v1:replay", result)
    return result
