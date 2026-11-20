"""标签结果第 2 版与兼容迁移的专项测试。

覆盖的问题：
1. 未计算 / 未命中 / 已计算在数据上可区分；
2. 全体会员批次与仅命中批次迁移后语义不变，仅命中批次不会凭空出现未命中；
3. 同一次圈选只用一个版本解释条件，旧名单可原样回放；
4. 迁移中断后重新进入可续跑，不产生重复批次；
5. 未知覆盖不被迁移为未命中。
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.seed import SEED_ROWS
from app.store import store
from app.services import crowd_select as cs
from app.services import tag_compute as tc

_SEED_TAG_COMPUTE = copy.deepcopy(SEED_ROWS["tag_compute"])


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        store.runtime()["tag_result_migration"] = {
            "state": "pending", "total": 0, "completed": [], "issues": [], "attempts": 0,
        }
        store.runtime()["tag_result_switch"] = "v2"

    def _legacy_only(self) -> None:
        """迁移/圈选用例只保留本用例登记的旧任务，排除种子数据干扰。"""
        store._tables["tag_compute"] = []
        store._next_ids["tag_compute"] = 1

    def tearDown(self) -> None:
        # 恢复种子任务表，避免影响同进程运行的其他链路测试。
        store._tables["tag_compute"] = copy.deepcopy(_SEED_TAG_COMPUTE)
        store._next_ids["tag_compute"] = max(int(r.get("id", 0)) for r in store._tables["tag_compute"]) + 1

    def _add_legacy(self, task_id: str, tag_id: str, coverage: str, values: list[dict], declared: int | None = None) -> None:
        store.insert("tag_compute", {
            "task_id": task_id, "tag_id": tag_id, "compute_scope": "all",
            "exec_state": "success", "legacy_output_coverage": coverage,
            "output_partition": "legacy_partition", "parallelism": 1, "engine": "python",
            "input_partitions": [], "status": "已完成", "pending": False, "abnormal": False,
        })
        store.set_artifact("tag_compute", task_id, {
            "task_id": task_id, "tag_id": tag_id, "result_version": 1,
            "coverage": coverage, "value_type": "bool",
            "sequence": store.next_sequence(),
            "result_count": declared if declared is not None else len(values),
            "user_tag_values": values, "output_partition": "legacy_partition",
        })


class TestMigration(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._legacy_only()

    def test_explicit_and_hit_only_have_distinct_semantics(self) -> None:
        self._add_legacy("TC-101", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
            {"user_id": "U2", "tag_value": False},
        ])
        self._add_legacy("TC-102", "TAG-002", "hit_only", [
            {"user_id": "U1", "tag_value": True},
        ])
        tc.migrate_legacy_results()

        explicit = store.get_artifact(tc.V2_ARTIFACT, "TRB-TC-101-V2")
        hit_only = store.get_artifact(tc.V2_ARTIFACT, "TRB-TC-102-V2")

        self.assertEqual((explicit["member_count"], explicit["hit_count"], explicit["miss_count"]), (2, 1, 1))
        self.assertTrue(explicit["miss_covered"])
        self.assertEqual([r["user_id"] for r in explicit["records"] if not r["hit"]], ["U2"])

        # 仅命中批次：只剩命中者，没有未命中，也不声明覆盖未命中。
        self.assertEqual((hit_only["member_count"], hit_only["hit_count"], hit_only["miss_count"]), (1, 1, 0))
        self.assertFalse(hit_only["miss_covered"])
        self.assertEqual(hit_only["coverage"], "hit_only")
        self.assertTrue(all(r["hit"] for r in hit_only["records"]))

    def test_hit_only_never_invents_misses(self) -> None:
        self._add_legacy("TC-103", "TAG-001", "hit_only", [
            {"user_id": "U1", "tag_value": True},
        ])
        tc.migrate_legacy_results()
        batch = store.get_artifact(tc.V2_ARTIFACT, "TRB-TC-103-V2")
        # 会员空间是 U1..U125，未出现的会员绝不能被写成未命中。
        self.assertEqual(len(batch["records"]), 1)
        self.assertEqual(batch["miss_count"], 0)
        self.assertEqual(batch["member_count"], 1)

    def test_unknown_value_is_not_migrated_as_miss(self) -> None:
        self._add_legacy("TC-104", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
            {"user_id": "U2", "tag_value": "unknown"},
            {"user_id": "U3", "tag_value": None},
        ])
        status = tc.migrate_legacy_results()
        batch = store.get_artifact(tc.V2_ARTIFACT, "TRB-TC-104-V2")
        self.assertEqual([r["user_id"] for r in batch["records"]], ["U1"])
        self.assertEqual(status["state"], "completed_with_issues")
        reasons = {i["reason"] for i in status["issues"] if i["task_id"] == "TC-104"}
        self.assertEqual(reasons, {"unknown_value"})

    def test_undeclared_coverage_downgrades_to_unknown(self) -> None:
        # 未声明覆盖方式 + 旧结果只列出命中者：未出现的会员保持未知。
        store.insert("tag_compute", {
            "task_id": "TC-105", "tag_id": "TAG-002", "compute_scope": "all",
            "exec_state": "success", "output_partition": "legacy", "parallelism": 1,
            "engine": "python", "input_partitions": [], "status": "已完成",
            "pending": False, "abnormal": False,
        })
        store.set_artifact("tag_compute", "TC-105", {
            "task_id": "TC-105", "tag_id": "TAG-002", "result_version": 1,
            "value_type": "bool", "sequence": store.next_sequence(),
            "result_count": 1, "user_tag_values": [{"user_id": "U1", "tag_value": True}],
        })
        tc.migrate_legacy_results()
        batch = store.get_artifact(tc.V2_ARTIFACT, "TRB-TC-105-V2")
        self.assertEqual(batch["coverage"], "unknown")
        self.assertEqual(batch["miss_count"], 0)
        self.assertFalse(batch["miss_covered"])

    def test_truncated_explicit_downgrades(self) -> None:
        self._add_legacy("TC-106", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
        ], declared=125)
        tc.migrate_legacy_results()
        batch = store.get_artifact(tc.V2_ARTIFACT, "TRB-TC-106-V2")
        self.assertEqual(batch["coverage"], "unknown")
        self.assertEqual(batch["miss_count"], 0)
        self.assertIn("truncated_members", {i["reason"] for i in batch["migration_issues"]})

    def test_interrupted_migration_resumes_without_duplicates(self) -> None:
        self._add_legacy("TC-107", "TAG-001", "explicit", [{"user_id": "U1", "tag_value": True}])
        self._add_legacy("TC-108", "TAG-002", "hit_only", [{"user_id": "U1", "tag_value": True}])

        first = tc.migrate_legacy_results(interrupt_after=1)
        self.assertEqual(first["state"], "interrupted")
        self.assertEqual(first["completed_batches"], [])
        self.assertEqual(len(first["pending_batches"]), 2)

        second = tc.migrate_legacy_results()
        self.assertIn(second["state"], ("completed", "completed_with_issues"))
        self.assertEqual(sorted(second["completed_batches"]), ["TRB-TC-107-V2", "TRB-TC-108-V2"])

        batch_ids = [b["batch_id"] for b in tc.list_v2_batches()]
        self.assertEqual(sorted(batch_ids), ["TRB-TC-107-V2", "TRB-TC-108-V2"])

        third = tc.migrate_legacy_results()
        self.assertEqual(third["attempts"], 3)
        self.assertEqual(len(tc.list_v2_batches()), 2)

    def test_switch_blocked_until_migration_completes(self) -> None:
        self._add_legacy("TC-109", "TAG-001", "explicit", [{"user_id": "U1", "tag_value": True}])
        with self.assertRaises(ValueError):
            tc.set_result_switch("v2")
        tc.migrate_legacy_results()
        self.assertEqual(tc.set_result_switch("v2")["switch"], "v2")


class TestCrowdSelectVersions(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._legacy_only()
        store.insert("crowd_select", {
            "condition_id": "CS-T1", "condition_name": "标签真值为真",
            "tag_combination": [{"tag_id": "TAG-001", "op": "eq", "value": True}],
            "logic_expr": "TAG-001", "size_estimate": 5, "refresh_freq": "daily",
            "output_set": "set_t1", "status": "草稿", "pending": True, "abnormal": False,
            "version": "v1.0",
        })
        store.insert("crowd_select", {
            "condition_id": "CS-T2", "condition_name": "标签为假（否定）",
            "tag_combination": [{"tag_id": "TAG-001", "op": "eq", "value": False}],
            "logic_expr": "NOT TAG-001", "size_estimate": 5, "refresh_freq": "daily",
            "output_set": "set_t2", "status": "草稿", "pending": True, "abnormal": False,
            "version": "v1.0",
        })

    def test_hit_only_tag_cannot_answer_negation_in_v2(self) -> None:
        self._add_legacy("TC-201", "TAG-001", "hit_only", [
            {"user_id": "U1", "tag_value": True},
        ])
        tc.migrate_legacy_results()
        result = cs.select_crowd("CS-T2", result_version=2)
        self.assertFalse(result["interpretable"])
        self.assertEqual(result["matched_count"], 0)
        self.assertIn("只覆盖命中会员", result["blocked_reason"])

    def test_explicit_tag_answers_negation_in_v2(self) -> None:
        self._add_legacy("TC-202", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
            {"user_id": "U2", "tag_value": False},
            {"user_id": "U3", "tag_value": False},
        ])
        tc.migrate_legacy_results()
        result = cs.select_crowd("CS-T2", result_version=2)
        self.assertTrue(result["interpretable"])
        self.assertEqual(result["matched_users"], ["U2", "U3"])

    def test_no_version_mixing_in_one_selection(self) -> None:
        self._add_legacy("TC-203", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
        ])
        tc.migrate_legacy_results()
        v2 = cs.select_crowd("CS-T1", result_version=2)
        # 第 2 版结果已经存在，旧活动回放仍然只能读到第 1 版。
        v1 = cs.replay_crowd("CS-T1")
        self.assertEqual(v2["result_version"], 2)
        self.assertEqual(v1["result_version"], 1)
        self.assertTrue(v1["replay"])
        # 两个版本各自锁定自己的批次来源，不允许共用。
        self.assertNotEqual(v2["selection_key"], v1["selection_key"])

    def test_midway_switch_fixed_by_lock(self) -> None:
        self._add_legacy("TC-204", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
            {"user_id": "U2", "tag_value": False},
        ])
        tc.migrate_legacy_results()
        # 新版计算产出新的批次（模拟升级后新链路），同时把运行期开关切回 v1。
        store.insert("tag_compute", {
            "task_id": "TC-900", "tag_id": "TAG-002", "compute_scope": "all",
            "exec_state": "draft", "output_partition": "p", "parallelism": 1,
            "engine": "python", "input_partitions": [], "status": "草稿",
            "pending": True, "abnormal": False,
        })
        tc.compute_tag("TC-900")
        store.runtime()["tag_result_switch"] = "v1"
        result = cs.select_crowd("CS-T2", result_version=2)
        # 显式指定版本时仍只按 v2 批次解释，不受开关影响。
        self.assertEqual(result["result_version"], 2)
        self.assertIn("TRB-TC-204-V2", result["selection_key"])
        self.assertTrue(all(c["result_version"] == 2 for c in result["conditions"]))

    def test_v1_replay_uses_legacy_rows(self) -> None:
        # 旧回放按旧口径枚举当年计算过的全体会员（缺行即未命中）；
        # 这里额外补一条命中，确保否定结果里能明确看到“命中者不在名单里”。
        self._add_legacy("TC-205", "TAG-001", "explicit", [
            {"user_id": "U1", "tag_value": True},
            {"user_id": "U2", "tag_value": False},
            {"user_id": "U3", "tag_value": False},
        ])
        result = cs.replay_crowd("CS-T2")
        self.assertEqual(result["result_version"], 1)
        self.assertEqual(result["member_count"], 3)
        self.assertEqual(result["matched_users"], ["U2", "U3"])
        # 回放只读第 1 版明细，条件解释不带第 2 版的批次编号。
        self.assertTrue(all(c["batch_id"] is None and c["result_version"] == 1 for c in result["conditions"]))

    def test_replay_without_legacy_output_is_blocked_not_empty_list(self) -> None:
        # 该标签在升级前从未算过：回放无法还原，不能悄悄给出空名单当成旧活动结果。
        result = cs.replay_crowd("CS-T1")
        self.assertEqual(result["result_version"], 1)
        self.assertFalse(result["interpretable"])
        self.assertEqual(result["missing_tags"], ["TAG-001"])
        self.assertEqual(result["matched_count"], 0)


class TestComputeV2(_Base):
    def test_compute_records_cover_all_members(self) -> None:
        result = tc.compute_tag("TC-001")
        batch = tc.get_compute_result_v2("TC-001")
        self.assertEqual(result.result_count, 125)
        self.assertEqual(batch["result_version"], 2)
        self.assertEqual(batch["member_count"], 125)
        self.assertEqual(batch["hit_count"] + batch["miss_count"], 125)
        self.assertTrue(all(r["computed"] and r["tag_id"] == "TAG-001" for r in batch["records"]))

    def test_legacy_view_is_projection_of_v2(self) -> None:
        tc.compute_tag("TC-001")
        batch = tc.get_compute_result_v2("TC-001")
        view = tc.get_compute_result("TC-001")
        self.assertEqual(view["source_result_version"], 2)
        self.assertEqual(view["batch_id"], batch["batch_id"])
        self.assertEqual(view["result_count"], batch["member_count"])

    def test_composite_tag_uses_dependency_batch(self) -> None:
        result = tc.compute_tag("TC-003")
        batch = tc.get_compute_result_v2("TC-003")
        self.assertEqual(result.result_count, 125)
        # TAG-003 依赖 TAG-001；TAG-001 的批次是先算出来的，按真值参与判定。
        self.assertGreater(batch["hit_count"], 0)
        self.assertEqual(batch["undetermined_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
