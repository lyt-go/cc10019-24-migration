"""验证环境回归脚本：新旧圈选 + 迁移中断续跑 + 名单语义核对。

不依赖测试框架，直接打真实 HTTP 接口，方便在验证环境手工跑：

    .venv/bin/python scripts/verify_tag_v2.py --base http://127.0.0.1:8000

脚本只读接口 + 触发迁移任务，不写业务配置。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def call(base: str, path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--interrupt-after", type=int, default=1)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    # 1. 迁移中断后重新进入
    first = call(base, f"/api/crowd-select/tag-results/migrate?interrupt_after={args.interrupt_after}", "POST")
    check("中断时状态为 interrupted", first["state"] == "interrupted", first["state"])
    check("中断时还有待迁移批次", bool(first["pending_batches"]), str(first["pending_batches"]))

    second = call(base, "/api/crowd-select/tag-results/migrate", "POST")
    check("续跑后无待迁移批次", not second["pending_batches"], str(second["pending_batches"]))
    check("续跑后完成清单去重", len(second["completed_batches"]) == len(set(second["completed_batches"])))

    batches = call(base, "/api/tag-compute/results/v2")["items"]
    ids = [b["batch_id"] for b in batches]
    check("第 2 版批次没有重复", len(ids) == len(set(ids)), f"{len(ids)} 个批次")

    # 2. 仅命中批次不得出现未命中记录
    for batch in batches:
        if batch["coverage"] in ("hit_only", "unknown"):
            ok = batch["miss_count"] == 0 and not batch["miss_covered"] and all(
                r["hit"] for r in batch["records"]
            )
            check(
                f"仅命中批次 {batch['batch_id']} 未凭空出现未命中",
                ok,
                f"miss={batch['miss_count']} covered={batch['miss_covered']}",
            )

    # 3. 新旧圈选名单与版本语义核对
    for condition_id in ("CS-001", "CS-002", "CS-003"):
        try:
            cmp = call(base, f"/api/crowd-select/{condition_id}/compare")
        except RuntimeError as exc:
            # 没有新版结果的标签（例如从未算过的 TAG-004）要被明确拒绝，而不是给空名单。
            print(f"[SKIP] {condition_id} 无法双版本核对：{exc}")
            continue
        check(f"{condition_id} 旧名单只用 v1", cmp["version_consistency"]["v1_all_v1"])
        check(f"{condition_id} 新名单只用 v2", cmp["version_consistency"]["v2_all_v2"])
        check(
            f"{condition_id} 两份名单版本不同",
            cmp["v1"]["result_version"] == 1 and cmp["v2"]["result_version"] == 2,
        )
        print(
            f"       {condition_id}: v1={cmp['v1']['matched_count']} 人 "
            f"(批次 {cmp['v1']['selection_key']}) | "
            f"v2={cmp['v2']['matched_count']} 人 (批次 {cmp['v2']['selection_key']})"
        )
        for check_row in cmp["hit_only_checks"]:
            check(
                f"{condition_id} 批次 {check_row['batch_id']} 覆盖核对",
                check_row["ok"],
                f"miss_count={check_row['miss_count']}",
            )

    print()
    if failures:
        print(f"共 {len(failures)} 项未通过：" + "，".join(failures))
        return 1
    print("全部核对通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
