"""标签计算引擎：执行标签规则的批量计算，并产出第 2 版标签结果数据。

结果版本说明
------------
* 第 1 版（``tag_compute`` 产物）：只有布尔标签明细，靠“行是否存在”表达命中，
  无法区分“未计算”和“未命中”。保留它只用于旧活动回放。
* 第 2 版（``tag_result_v2`` 产物）：每个会员一条显式记录，
  ``computed`` / ``hit`` / ``tag_value`` 三个字段把“未计算 / 未命中 / 已计算”
  拆开；批次上带 ``value_type``、``coverage``，说明值语义和会员覆盖方式。

兼容迁移
--------
旧结果里有两类批次：一类记录了全体会员（explicit），另一类只留下命中者
（hit_only）。迁移只转换旧结果里**确实记录过**的信息：

* explicit 批次的非命中行迁为 ``hit=False`` 的未命中记录；
* hit_only 批次只迁命中记录，未出现的会员是“未知覆盖”，绝不能补成未命中；
* 旧行里值缺失/``unknown`` 的记录记为迁移问题并跳过，同样不补未命中。
"""
from __future__ import annotations

import re
import time
from typing import Any

from app.store import store
from app.schemas import TagComputeResult, TagResultBatchV2, TagValueRecord

V2_ARTIFACT = "tag_result_v2"
LEGACY_ARTIFACT = "tag_compute"
RESULT_VERSION_V1 = 1
RESULT_VERSION_V2 = 2

_UNKNOWN = object()
_UNKNOWN_TEXT = {"", "unknown", "none", "null", "nan", "未计算", "未知"}


# ==================== 条件求值 ====================

def _eval_condition(user: dict[str, Any], logic: str, attr_defs: dict[str, dict[str, Any]]) -> bool:
    """简化条件求值：支持 AND/OR/NOT 和基本比较。"""
    logic = logic.strip()
    # 处理括号
    if logic.startswith("(") and logic.endswith(")"):
        return _eval_condition(user, logic[1:-1], attr_defs)

    # 处理 NOT
    if logic.upper().startswith("NOT "):
        return not _eval_condition(user, logic[4:], attr_defs)

    # 处理 AND
    if " AND " in logic.upper():
        parts = _split_by_op(logic, "AND")
        return all(_eval_condition(user, p, attr_defs) for p in parts)

    # 处理 OR
    if " OR " in logic.upper():
        parts = _split_by_op(logic, "OR")
        return any(_eval_condition(user, p, attr_defs) for p in parts)

    # 基本比较：field op value
    return _eval_atom(user, logic, attr_defs)


def _split_by_op(logic: str, op: str) -> list[str]:
    """按操作符分割，注意括号嵌套。"""
    parts: list[str] = []
    depth = 0
    current = ""
    tokens = logic.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if tok.upper() == op and depth == 0:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += tok + " "
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts if len(parts) > 1 else [logic]


def _coerce_bool(value: Any) -> bool | None:
    """把 true/false 的常见写法归一成布尔；无法识别返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
    return None


def _eval_atom(user: dict[str, Any], logic: str, attr_defs: dict[str, Any]) -> bool:
    """求值原子条件。"""
    logic = logic.strip()
    # 支持 =, !=, <, >, <=, >=, IN, NOT IN
    for op in ["!=", "<=", ">=", "=", "<", ">"]:
        if op in logic:
            parts = logic.split(op, 1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()
                left_val = user.get(left)
                if _is_tag_ref(left) and left not in user:
                    # 依赖标签在该会员上没有值（未知覆盖），不能按 False 处理。
                    raise ValueError(f"依赖标签 {left} 在该会员上未知")
                # 处理字符串引号
                if right.startswith("'") and right.endswith("'"):
                    right = right[1:-1]
                elif right.startswith('"') and right.endswith('"'):
                    right = right[1:-1]
                # 处理布尔（大小写不敏感，兼容历史表达式里的 true/false）
                bool_value = _coerce_bool(right)
                if bool_value is not None:
                    right = bool_value
                else:
                    # 处理数值
                    try:
                        right = float(right)
                        if left_val is not None:
                            try:
                                left_val = float(left_val)
                            except (TypeError, ValueError):
                                pass
                    except ValueError:
                        pass

                if op == "=":
                    return left_val == right
                elif op == "!=":
                    return left_val != right
                elif op == "<":
                    if left_val is None:
                        return False
                    return left_val < right
                elif op == ">":
                    if left_val is None:
                        return False
                    return left_val > right
                elif op == "<=":
                    if left_val is None:
                        return False
                    return left_val <= right
                elif op == ">=":
                    if left_val is None:
                        return False
                    return left_val >= right
    # 处理 IN
    if " IN " in logic.upper():
        parts = logic.upper().split(" IN ", 1)
        field = logic[:len(parts[0])].strip()
        val_list_str = logic[len(parts[0])+4:].strip()
        if val_list_str.startswith("[") and val_list_str.endswith("]"):
            val_list_str = val_list_str[1:-1]
        vals = [v.strip().strip("'\"").strip() for v in val_list_str.split(",")]
        field_val = str(user.get(field, ""))
        return field_val in vals

    return bool(user.get(logic, False))


# ==================== 第 2 版结果 ====================

def _tag_ids_in_logic(logic: str) -> list[str]:
    return sorted(set(re.findall(r"TAG-\d+", logic or "")))


def _is_tag_ref(name: str) -> bool:
    return bool(re.fullmatch(r"TAG-\d+", (name or "").strip()))


def _value_type_of_tag(tag: dict[str, Any]) -> str:
    """标签值类型以标签定义为准，默认布尔。"""
    declared = str(tag.get("value_type", "") or "").lower()
    if declared in ("bool", "int", "float", "string"):
        return declared
    return "bool"


def _batch_sequence() -> int:
    return store.next_sequence()


def _make_record(
    user_id: str,
    tag_id: str,
    task_id: str,
    tag_value: Any,
    hit: bool,
    value_type: str,
    coverage: str,
) -> dict[str, Any]:
    return TagValueRecord(
        user_id=user_id,
        tag_id=tag_id,
        tag_value=tag_value,
        hit=hit,
        computed=True,
        value_type=value_type,
        task_id=task_id,
        coverage=coverage,
    ).model_dump()


def _build_v2_batch(
    task: dict[str, Any],
    tag: dict[str, Any],
    records: list[dict[str, Any]],
    coverage: str,
    value_type: str,
    source_version: int = RESULT_VERSION_V2,
    migrated_from: str | None = None,
    compute_time_ms: int = 0,
    batch_id: str | None = None,
    undetermined_count: int = 0,
) -> dict[str, Any]:
    hit_count = sum(1 for r in records if r["hit"])
    miss_count = sum(1 for r in records if not r["hit"]) if coverage == "explicit" else 0
    batch = TagResultBatchV2(
        batch_id=batch_id or f"TRB-{task['task_id']}-V2",
        result_version=RESULT_VERSION_V2,
        task_id=task["task_id"],
        tag_id=task["tag_id"],
        compute_scope=task.get("compute_scope", "all"),
        value_type=value_type,
        coverage=coverage,
        source_version=source_version,
        migrated_from=migrated_from,
        output_partition=task.get("output_partition", ""),
        member_count=len(records),
        hit_count=hit_count,
        miss_count=miss_count,
        miss_covered=(coverage == "explicit"),
        undetermined_count=undetermined_count,
        records=records,
        sequence=_batch_sequence(),
        compute_time_ms=compute_time_ms,
    )
    return batch.model_dump()


def _has_pending_legacy(tag_id: str) -> bool:
    """该标签是否还有未迁移的升级前旧结果。"""
    return any(
        task.get("tag_id") == tag_id
        for artifact, task in migration_status_pairs()
    )


def _completed_batch_ids(legacy: list[tuple[dict[str, Any], dict[str, Any]]]) -> set[str]:
    """完成清单以“批次确实落在结果仓库里”为准，日志只作为记录。"""
    logged = set(store.runtime()["tag_result_migration"]["completed"])
    return {
        f"TRB-{task['task_id']}-V2"
        for artifact, task in legacy
        if f"TRB-{task['task_id']}-V2" in logged
        and store.get_artifact(V2_ARTIFACT, f"TRB-{task['task_id']}-V2") is not None
    }


def migration_status_pairs() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """待迁移的（旧结果, 任务）对，供依赖解析判断是否需要先迁移。"""
    ensure_legacy_results()
    legacy = _migration_scope()
    completed = _completed_batch_ids(legacy)
    return [
        (artifact, task)
        for artifact, task in legacy
        if f"TRB-{task['task_id']}-V2" not in completed
    ]


def _computable_task_for_tag(tag_id: str) -> dict[str, Any] | None:
    """找可离线补算的任务；失败任务不参与，避免用坏结果兜底。

    已经迁移出第 2 版结果的旧任务不参与补算：重算会覆盖它锁定的历史批次。
    升级前有输出分区的任务也必须走兼容迁移，不能被静默重算顶替。
    """
    tasks = [
        t for t in store.find_all_by_field("tag_compute", "tag_id", tag_id)
        if t.get("exec_state") != "failed"
        and not t.get("legacy_output_coverage")
        and not store.get_artifact(V2_ARTIFACT, f"TRB-{t['task_id']}-V2")
    ]
    if not tasks:
        return None
    order = {"success": 0, "running": 1}
    return sorted(tasks, key=lambda t: order.get(t.get("exec_state"), 9))[0]


def _latest_batch_for_tag(tag_id: str) -> dict[str, Any] | None:
    batches = [b for b in store.list_artifacts(V2_ARTIFACT) if b.get("tag_id") == tag_id]
    if not batches:
        return None
    return max(batches, key=lambda b: int(b.get("sequence", 0)))


def _resolve_source_tag_values(tag: dict[str, Any], user_ids: list[str], depth: int) -> dict[str, dict[str, bool]]:
    """把标签逻辑里引用的其他标签值解析出来，供复合标签计算使用。

    返回 ``{tag_id: {user_id: hit}}``。没有被批次覆盖的会员不会出现在内层字典里，
    这样求值时不会把“未知覆盖”当作未命中。
    """
    sources: list[str] = []
    for source in tag.get("source_attrs", []):
        if isinstance(source, str) and source.startswith("TAG-"):
            sources.append(source)
    for tag_id in _tag_ids_in_logic(tag.get("compute_logic", "")):
        if tag_id not in sources:
            sources.append(tag_id)

    resolved: dict[str, dict[str, bool]] = {}
    for source_tag_id in sources:
        if source_tag_id == tag.get("tag_id"):
            continue
        batch = _latest_batch_for_tag(source_tag_id)
        if batch is None and depth < 4:
            # 依赖标签只有升级前的旧结果时，先做兼容迁移，再按新版批次取值。
            if _has_pending_legacy(source_tag_id):
                migrate_legacy_results()
                batch = _latest_batch_for_tag(source_tag_id)
            if batch is None:
                task = _computable_task_for_tag(source_tag_id)
                if task is not None:
                    compute_tag(task["task_id"], _depth=depth + 1)
                    batch = _latest_batch_for_tag(source_tag_id)
        if batch is None:
            continue
        # 逻辑里出现 NOT/否定比较却只有仅命中批次时，无法得到未命中集合。
        logic = (tag.get("compute_logic") or "").upper()
        needs_miss = "NOT " in logic or "= FALSE" in logic or "!= TRUE" in logic
        if needs_miss and not batch.get("miss_covered", False):
            raise ValueError(
                f"标签 {source_tag_id} 的结果批次只覆盖命中会员，无法计算依赖未命中的逻辑"
            )
        resolved[source_tag_id] = {
            r["user_id"]: bool(r["hit"]) for r in batch.get("records", [])
        }
    return resolved


# ==================== 计算 ====================

def compute_tag(task_id: str, _depth: int = 0) -> TagComputeResult:
    """执行标签计算任务，写出第 2 版结果并保留第 1 版兼容视图。"""
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")
    if _depth > 4:
        raise ValueError(f"标签依赖层级过深: {task_id}")

    tag_id = task_row["tag_id"]
    tag = store.find_by_field("tag_define", "tag_id", tag_id)
    if tag is None:
        raise ValueError(f"Tag not found: {tag_id}")

    start_time = time.time()
    scope = task_row["compute_scope"]

    # 获取属性定义用于类型转换
    attr_defs = {}
    for attr in store.rows("attr_dict"):
        attr_defs[attr["attr_id"]] = attr

    users = _load_users_for_tag(tag, attr_defs, scope)
    user_ids = [u.get("user_id") for u in users]

    # 复合标签依赖其他标签的批次值；未知覆盖保持缺失，不当作 False。
    source_values = _resolve_source_tag_values(tag, user_ids, _depth)
    for source_tag_id, values in source_values.items():
        for user in users:
            if user["user_id"] in values:
                user[source_tag_id] = values[user["user_id"]]

    value_type = _value_type_of_tag(tag)
    records: list[dict[str, Any]] = []
    undetermined = 0
    for user in users:
        try:
            matched = bool(_eval_condition(user, tag["compute_logic"], attr_defs))
        except (TypeError, ValueError):
            # 依赖标签在该会员上未知：本轮不落记录，保持“未计算”而不是伪造未命中。
            undetermined += 1
            continue
        records.append(_make_record(
            user_id=str(user.get("user_id")),
            tag_id=tag_id,
            task_id=task_id,
            tag_value=matched,
            hit=bool(matched),
            value_type=value_type,
            coverage="explicit",
        ))

    elapsed_ms = int((time.time() - start_time) * 1000)
    batch = _build_v2_batch(
        task=task_row,
        tag=tag,
        records=records,
        coverage="explicit",
        value_type=value_type,
        source_version=RESULT_VERSION_V2,
        compute_time_ms=elapsed_ms,
        undetermined_count=undetermined,
    )
    store.set_artifact(V2_ARTIFACT, batch["batch_id"], batch)

    # 第 1 版兼容视图：旧读者仍可读出布尔明细，但它只是第 2 版结果的投影。
    legacy_view = {
        "task_id": task_id,
        "tag_id": tag_id,
        "compute_scope": scope,
        "result_version": RESULT_VERSION_V1,
        "source_result_version": RESULT_VERSION_V2,
        "batch_id": batch["batch_id"],
        "coverage": "explicit",
        "value_type": value_type,
        "sequence": _batch_sequence(),
        "result_count": len(records),
        "user_tag_values": [
            {
                "user_id": r["user_id"],
                "tag_id": tag_id,
                "tag_value": r["hit"],
                "value_type": value_type,
                "computed": True,
                "compute_time": task_row.get("output_partition", ""),
            }
            for r in records[:100]
        ],
        "output_partition": task_row.get("output_partition", ""),
        "compute_time_ms": elapsed_ms,
    }
    store.set_artifact(LEGACY_ARTIFACT, task_id, legacy_view)

    # 更新任务状态
    store.update("tag_compute", task_row["id"], {
        "exec_state": "success",
        "status": "已完成",
        "pending": False,
        "result_count": len(records),
        "result_version": RESULT_VERSION_V2,
        "result_batch_id": batch["batch_id"],
        "error_log": "",
    })

    return TagComputeResult(**{
        "task_id": task_id,
        "tag_id": tag_id,
        "compute_scope": scope,
        "result_count": len(records),
        "user_tag_values": legacy_view["user_tag_values"],
        "output_partition": task_row.get("output_partition", ""),
        "compute_time_ms": elapsed_ms,
    })


def _load_users_for_tag(tag: dict[str, Any], attr_defs: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    """加载需要计算标签的用户数据。"""
    users = []
    for i in range(1, 126):
        user = {
            "user_id": f"U{i:05d}",
            "ATTR-001": ["app", "web", "miniapp", "h5"][i % 4],
            "ATTR-002": i % 60,
            "ATTR-003": float(i * 15.5),
            "ATTR-004": ["ios", "android", "pc", "other"][i % 4],
            "ATTR-005": ["普通", "银卡", "金卡", "钻石"][i % 4],
        }
        if scope == "incremental" and i > 32:
            continue
        if scope == "delta" and i % 3 != 0:
            continue
        users.append(user)
    return users


def get_compute_result(task_id: str) -> dict[str, Any] | None:
    """获取第 1 版兼容视图（旧读者使用）。"""
    artifact = store.get_artifact(LEGACY_ARTIFACT, task_id)
    if artifact:
        return artifact
    try:
        result = compute_tag(task_id)
        return store.get_artifact(LEGACY_ARTIFACT, task_id) or result.model_dump()
    except ValueError:
        return None


def get_compute_result_v2(task_id: str) -> dict[str, Any] | None:
    """获取任务最新产出的第 2 版结果批次。"""
    row = store.find_by_field("tag_compute", "task_id", task_id)
    if row is None:
        return None
    batch_id = row.get("result_batch_id")
    if batch_id and store.get_artifact(V2_ARTIFACT, batch_id):
        return store.get_artifact(V2_ARTIFACT, batch_id)
    batches = [b for b in store.list_artifacts(V2_ARTIFACT) if b.get("task_id") == task_id]
    return max(batches, key=lambda b: int(b.get("sequence", 0))) if batches else None


def list_compute_results() -> list[dict[str, Any]]:
    """列出所有标签计算结果（第 1 版兼容视图）。"""
    return store.list_artifacts(LEGACY_ARTIFACT)


def list_v2_batches() -> list[dict[str, Any]]:
    """列出所有第 2 版结果批次。"""
    return sorted(store.list_artifacts(V2_ARTIFACT), key=lambda b: int(b.get("sequence", 0)))


def recompute_tag(task_id: str, new_scope: str | None = None) -> TagComputeResult:
    """重新计算标签，支持切换计算范围。"""
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")

    if new_scope:
        store.update("tag_compute", task_row["id"], {"compute_scope": new_scope})
        store.invalidate_compute_cache(f"tag_compute:{task_id}")

    return compute_tag(task_id)


# ==================== 旧结果与兼容迁移 ====================

def _is_unknown_value(raw: Any) -> bool:
    if raw is _UNKNOWN or raw is None:
        return True
    if isinstance(raw, str) and raw.strip().lower() in _UNKNOWN_TEXT:
        return True
    return False


def _legacy_coverage(task: dict[str, Any]) -> str:
    """旧任务当初的输出覆盖方式；未标注时按全体会员处理。"""
    coverage = task.get("legacy_output_coverage") or "explicit"
    return coverage if coverage in ("explicit", "hit_only", "unknown") else "unknown"


def _write_legacy_artifact(task: dict[str, Any], coverage: str) -> dict[str, Any]:
    """按旧版本格式落一份历史结果，模拟升级前遗留的输出分区。"""
    tag = store.find_by_field("tag_define", "tag_id", task["tag_id"])
    users = _load_users_for_tag(tag, {}, task["compute_scope"]) if tag else []
    values: list[dict[str, Any]] = []
    for user in users:
        try:
            matched = _eval_condition(user, tag["compute_logic"], {}) if tag else False
        except (TypeError, ValueError):
            # 复合标签依赖的另一标签在旧结果里没有值，这条旧输出也就无从判断。
            continue
        if coverage == "hit_only" and not matched:
            continue
        values.append({
            "user_id": user["user_id"],
            "tag_id": task["tag_id"],
            "tag_value": bool(matched),
            "value_type": "bool",
            "computed": True,
            "compute_time": task.get("output_partition", ""),
        })
    artifact = {
        "task_id": task["task_id"],
        "tag_id": task["tag_id"],
        "compute_scope": task["compute_scope"],
        "result_version": RESULT_VERSION_V1,
        "coverage": coverage,
        "value_type": "bool",
        "sequence": _batch_sequence(),
        "result_count": len(values),
        "user_tag_values": values,
        "output_partition": task.get("output_partition", ""),
        "compute_time_ms": 0,
    }
    store.set_artifact(LEGACY_ARTIFACT, task["task_id"], artifact)
    return artifact


def ensure_legacy_results() -> list[dict[str, Any]]:
    """确保旧任务的历史结果可读（升级前的输出分区）。

    只为显式标注了 ``legacy_output_coverage`` 的任务生成历史结果：
    没有升级前输出记录的任务本来就没有旧名单，不能凭空造一份。
    新版本写出的兼容视图带有 ``source_result_version=2``，不会被当成旧结果。
    """
    for task in store.rows("tag_compute"):
        if task.get("exec_state") != "success":
            continue
        if not task.get("legacy_output_coverage"):
            continue
        existing = store.get_artifact(LEGACY_ARTIFACT, task["task_id"])
        if existing is not None:
            continue
        if _latest_batch_for_tag(task["tag_id"]) is not None:
            continue
        _write_legacy_artifact(task, _legacy_coverage(task))
    return list_compute_results()


def _infer_value_type(values: list[dict[str, Any]]) -> str:
    kinds = set()
    for item in values:
        value = item.get("tag_value", _UNKNOWN)
        if _is_unknown_value(value):
            continue
        kinds.add(type(value).__name__)
    if kinds <= {"bool"}:
        return "bool"
    if kinds <= {"int", "bool"}:
        return "int" if "int" in kinds else "bool"
    if kinds <= {"int", "float", "bool"}:
        return "float" if "float" in kinds else "int"
    return "string"


def _migrate_legacy_artifact(artifact: dict[str, Any], task: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把一条旧结果转成第 2 版批次；只转换旧结果真正记录过的信息。"""
    # 没有声明覆盖方式一律按“未知”处理，绝不用默认值假装覆盖了全体会员。
    coverage = artifact.get("coverage") or "unknown"
    raw_values = artifact.get("user_tag_values", [])
    value_type = artifact.get("value_type") or _infer_value_type(raw_values)
    issues: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    declared = artifact.get("result_count")
    if coverage == "explicit" and isinstance(declared, int) and declared > len(raw_values):
        # 旧结果只列出了部分会员，剩余会员无法枚举：覆盖方式降级为未知。
        coverage = "unknown"
        issues.append({
            "task_id": task["task_id"],
            "tag_id": task["tag_id"],
            "reason": "truncated_members",
            "detail": f"旧结果声明 {declared} 位会员但只明细了 {len(raw_values)} 位，其余会员保持未知，不迁移为未命中",
        })

    for item in raw_values:
        user_id = str(item.get("user_id", ""))
        raw = item.get("tag_value", _UNKNOWN)
        if _is_unknown_value(raw):
            # 旧行值未知：既不是命中也不是未命中，只登记问题。
            issues.append({
                "task_id": task["task_id"],
                "tag_id": task["tag_id"],
                "user_id": user_id,
                "reason": "unknown_value",
                "detail": "旧结果中的标签值缺失或为 unknown，未迁移为未命中",
            })
            continue
        if value_type == "bool":
            hit = _coerce_bool(raw)
            if hit is None:
                issues.append({
                    "task_id": task["task_id"],
                    "tag_id": task["tag_id"],
                    "user_id": user_id,
                    "reason": "unparsable_value",
                    "detail": f"无法按布尔解析旧值 {raw!r}，未迁移为未命中",
                })
                continue
        else:
            # 非布尔值没有统一的“命中”定义，这里按真值判断并保留原值。
            hit = bool(raw)

        if coverage != "explicit" and not hit:
            # 仅命中批次（或覆盖方式未知）不应出现未命中行；即便出现也不迁。
            issues.append({
                "task_id": task["task_id"],
                "tag_id": task["tag_id"],
                "user_id": user_id,
                "reason": "unexpected_miss_row",
                "detail": f"覆盖方式为 {coverage} 的旧批次出现未命中行，已忽略",
            })
            continue
        records.append(_make_record(
            user_id=user_id,
            tag_id=task["tag_id"],
            task_id=task["task_id"],
            tag_value=raw,
            hit=bool(hit),
            value_type=value_type,
            coverage=coverage,
        ))

    if coverage not in ("explicit", "hit_only"):
        # 覆盖方式未知：保留已知命中，未出现的会员记为未知覆盖，不补未命中。
        coverage = "unknown"
        issues.append({
            "task_id": task["task_id"],
            "tag_id": task["tag_id"],
            "reason": "unknown_coverage",
            "detail": "旧批次未声明会员覆盖方式，未出现的会员保持未知，不迁移为未命中",
        })

    batch = _build_v2_batch(
        task=task,
        tag=store.find_by_field("tag_define", "tag_id", task["tag_id"]) or {"tag_id": task["tag_id"]},
        records=records,
        coverage=coverage,
        value_type=value_type,
        source_version=RESULT_VERSION_V1,
        migrated_from="v1",
        batch_id=f"TRB-{task['task_id']}-V2",
    )
    batch["migration_issues"] = issues
    batch["unresolved_count"] = len(issues)
    batch["source_result_count"] = artifact.get("result_count", len(raw_values))
    store.set_artifact(V2_ARTIFACT, batch["batch_id"], batch)
    return batch, issues


def _legacy_candidates() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """待迁移的旧结果列表（兼容视图和已有原生第 2 版结果除外）。"""
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for artifact in store.list_artifacts(LEGACY_ARTIFACT):
        if artifact.get("result_version", RESULT_VERSION_V1) != RESULT_VERSION_V1:
            continue
        if artifact.get("source_result_version") == RESULT_VERSION_V2:
            # 新版本写出的兼容视图，它的本体已经是第 2 版结果。
            continue
        task = store.find_by_field("tag_compute", "task_id", artifact.get("task_id"))
        if task is None:
            continue
        candidates.append((artifact, task))
    candidates.sort(key=lambda pair: (pair[1].get("task_id", ""), int(pair[0].get("sequence", 0))))
    return candidates


def migration_status() -> dict[str, Any]:
    mig = store.runtime()["tag_result_migration"]
    # 先把升级前的输出分区读成旧结果视图，进度才反映真实待迁移量。
    ensure_legacy_results()
    legacy = _migration_scope()
    completed = _completed_batch_ids(legacy)
    # 完成清单以实际落盘的批次为准，同时回写日志，避免数据仓库重置后进度虚高。
    mig["completed"] = sorted(completed)
    pending = [
        f"TRB-{task['task_id']}-V2"
        for artifact, task in legacy
        if f"TRB-{task['task_id']}-V2" not in completed
    ]
    return {
        "state": mig["state"],
        "attempts": mig["attempts"],
        "total_legacy": len(legacy),
        "completed_batches": list(mig["completed"]),
        "pending_batches": pending,
        "issues": list(mig["issues"]),
        "result_switch": store.runtime()["tag_result_switch"],
    }


def migrate_legacy_results(interrupt_after: int | None = None) -> dict[str, Any]:
    """兼容迁移旧结果到第 2 版。

    ``interrupt_after`` 用于验证“切换中断后重新进入”：写入 N 个批次后模拟中断，
    中断点之后的批次不在完成清单里，重新调用会按确定性批次编号重做，不会产生副本。
    """
    ensure_legacy_results()
    mig = store.runtime()["tag_result_migration"]
    mig["attempts"] = int(mig.get("attempts", 0)) + 1
    mig["state"] = "running"
    legacy = _migration_scope()
    mig["total"] = len(legacy)
    completed = _completed_batch_ids(legacy)
    processed = 0

    for artifact, task in legacy:
        batch_id = f"TRB-{task['task_id']}-V2"
        if batch_id in completed:
            continue
        batch, issues = _migrate_legacy_artifact(artifact, task)
        mig["issues"].extend(issues)
        processed += 1
        if interrupt_after is not None and processed >= interrupt_after:
            # 模拟在写完成标记前中断：批次已落盘，但完成清单还没更新。
            mig["state"] = "interrupted"
            return migration_status()
        completed.add(batch_id)
        mig["completed"] = sorted(completed)

    mig["state"] = "completed_with_issues" if mig["issues"] else "completed"
    return migration_status()


# ==================== 结果版本切换 ====================

def get_result_switch() -> str:
    return store.runtime()["tag_result_switch"]


def _migration_scope() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """迁移范围：只迁移升级前就存在的旧任务。

    旧结果本身就是一批结果；升级前留下的输出分区（带 ``legacy_output_coverage``）
    或没有结果版本标记的历史任务都算旧结果。新流程写出的第 1 版兼容视图
    （``source_result_version=2``）和本次升级后新建的任务不算。
    """
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for artifact, task in _legacy_candidates():
        if task.get("legacy_output_coverage") or not task.get("result_version"):
            candidates.append((artifact, task))
    return candidates


def set_result_switch(version: str) -> dict[str, Any]:
    """切换人群圈选读取的结果版本。

    只有迁移完成后才允许切到第 2 版，避免新旧结果在同一条件里互相解释。
    """
    if version not in ("v1", "v2"):
        raise ValueError("结果版本只支持 v1 或 v2")
    if version == "v2":
        status = migration_status()
        if status["pending_batches"]:
            raise ValueError(
                "旧结果尚未完成兼容迁移，不能切换到第 2 版；待迁移批次："
                + ", ".join(status["pending_batches"])
            )
    store.runtime()["tag_result_switch"] = version
    return {"switch": version, "migration": migration_status()}
