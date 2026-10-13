from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions
from oke_hpc_mgmt.benchmarks.models import BenchmarkError, ServicePolicy
from oke_hpc_mgmt.benchmarks.planning import node_target, plan_healthchecks
from oke_hpc_mgmt.benchmarks.profiles import execution_fingerprint
from oke_hpc_mgmt.benchmarks.records import RunRecord
from oke_hpc_mgmt.benchmarks.service_policy import (
    PriorAssessment, active_prerequisite, due_reason, latest_assessment, select_service_plan,
    validate_service_case, validate_service_idle,
)
from test_benchmark_planning import inventory, request
from test_benchmark_preflight import raw_inventory


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


class ServicePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = inventory(2)
        self.raw = raw_inventory()
        for node, raw in zip(self.snapshot.nodes, self.raw):
            node.boot_id = "boot-" + node.k8s_name
            raw["status"]["nodeInfo"] = {"bootID": node.boot_id}
            raw["status"]["conditions"][0]["lastTransitionTime"] = (NOW - timedelta(hours=1)).isoformat()
        self.request = request(check_type="active")
        self.policy = ServicePolicy(max_cases=2)

    def past(self, *, category: str = "active", status: str = "PASS", age: int = 600, number: int = 1) -> RunRecord:
        plan = plan_healthchecks(self.snapshot, replace(self.request, check_type=category))  # type: ignore[arg-type]
        run = RunRecord.create("hc-" + f"{number:020x}", plan, ExecutionOptions())
        run = replace(run, uid=f"run-{number}", created_at=(NOW - timedelta(seconds=age + 60)).isoformat())
        for case in plan.cases:
            run = run.update_case(case.case_id, "SUBMITTING")
            run = run.update_case(case.case_id, "SUCCEEDED" if status == "PASS" else "FAILED" if status == "FAIL" else "INCOMPLETE",
                                  job_uid="job-" + case.case_id, completed_at=(NOW - timedelta(seconds=age)).isoformat(),
                                  evidence_sha256="a" * 64, result={"status": status, "correctness": status, "coverage": "PASS",
                                                                   "transport": "LOCAL" if category == "active" else "RDMA"})
        return replace(run, state="CLEANED")

    def select(self, runs=(), pods=None, request_override=None, policy=None):
        return select_service_plan(self.snapshot, request_override or self.request, policy or self.policy,
                                   self.raw, pods or [], runs, NOW)

    def test_policy_bounds_and_due_retry_intervals(self) -> None:
        self.policy.validate()
        for key, value in (("interval_seconds", 0), ("retry_seconds", True), ("min_ready_seconds", 299),
                           ("max_cases", 33), ("active_cooldown_seconds", 0), ("active_validity_seconds", 31536001)):
            with self.subTest(key=key), self.assertRaises(BenchmarkError):
                replace(self.policy, **{key: value}).validate()
        self.assertTrue(due_reason(None, self.policy, NOW)[0])
        for status, age, due in (("PASS", 86399, False), ("PASS", 86400, True), ("FAIL", 599, False),
                                 ("FAIL", 600, True), ("INCOMPLETE", 600, True)):
            prior = PriorAssessment("run", NOW - timedelta(seconds=age), status)  # type: ignore[arg-type]
            self.assertEqual(due, due_reason(prior, self.policy, NOW)[0])
        with self.assertRaises(BenchmarkError):
            due_reason(PriorAssessment("run", NOW + timedelta(seconds=1), "PASS"), self.policy, NOW)

    def test_initial_active_pass_is_bounded_and_deterministic(self) -> None:
        result = self.select(policy=replace(self.policy, max_cases=1))
        self.assertEqual(["SELECTED", "DEFERRED"], [item.state for item in result.decisions])
        self.assertEqual(("gpu-01",), result.plan.selected_nodes)
        self.assertEqual(1, len(result.plan.cases))
        self.assertEqual(self.snapshot.nodes[0].boot_id, result.plan.cases[0].nodes[0].boot_id)
        self.assertEqual(1, result.plan.service_policy.max_cases)
        self.assertTrue(result.plan.cases[0].profile_fingerprint)

    def test_fresh_evidence_skips_and_expired_evidence_is_selected(self) -> None:
        result = self.select((self.past(),))
        self.assertIsNone(result.plan)
        self.assertEqual(["FRESH", "FRESH"], [item.state for item in result.decisions])
        expired = self.select((self.past(age=86400),))
        self.assertIsNotNone(expired.plan)

    def test_replacement_reboot_profile_and_threshold_changes_invalidate_freshness(self) -> None:
        previous = self.past()
        for change in ("uid", "boot", "profile", "threshold", "iterations", "image"):
            snapshot, raw = copy.deepcopy(self.snapshot), copy.deepcopy(self.raw)
            runs, requested = (previous,), self.request
            if change == "uid":
                snapshot.nodes[0].kubernetes_uid = raw[0]["metadata"]["uid"] = "new-uid"
            elif change == "boot":
                snapshot.nodes[0].boot_id = raw[0]["status"]["nodeInfo"]["bootID"] = "new-boot"
            elif change == "profile":
                runs = (replace(previous, plan=replace(previous.plan, cases=tuple(
                    replace(case, profile_fingerprint=None) for case in previous.plan.cases))),)
            elif change == "threshold":
                requested = replace(requested, parameters=replace(requested.parameters, min_bus_bandwidth=100))
            elif change == "iterations":
                requested = replace(requested, parameters=replace(requested.parameters, iterations=40))
            else:
                requested = replace(requested, image="example.invalid/new:tag")
            result = select_service_plan(snapshot, requested, self.policy, raw, [], runs, NOW)
            with self.subTest(change=change):
                self.assertEqual("SELECTED", result.decisions[0].state)

    def test_newer_failure_never_inherits_older_pass_and_retry_is_bounded(self) -> None:
        previous = self.past(age=3600)
        failed = self.past(status="FAIL", age=60, number=2)
        result = self.select((previous, failed))
        self.assertIsNone(result.plan)
        self.assertEqual(["COOLDOWN", "COOLDOWN"], [item.state for item in result.decisions])
        retry = self.select((previous, self.past(status="FAIL", age=600, number=2)))
        self.assertIsNotNone(retry.plan)

    def test_failure_in_one_repetition_cannot_be_hidden_by_a_later_pass(self) -> None:
        run = self.past()
        case = replace(run.plan.cases[0], case_id="case-0003", repetition=2)
        saved = replace(run.cases[0], case_id=case.case_id, state="FAILED", job_name="failure",
                        result={"status": "FAIL"}, completed_at=(NOW - timedelta(seconds=700)).isoformat())
        run = replace(run, plan=replace(run.plan, cases=(*run.plan.cases, case)), cases=(*run.cases, saved))
        target = node_target(self.snapshot.nodes[0])
        fingerprint = run.plan.cases[0].profile_fingerprint
        assert fingerprint is not None
        prior = latest_assessment(target, "active", fingerprint, (run,), NOW)
        assert prior is not None
        self.assertEqual("FAIL", prior.status)

    def test_missing_correctness_or_completion_never_establishes_pass(self) -> None:
        for changes in ({"completed_at": None}, {"evidence_sha256": None}, {"result": {"status": "PASS"}}):
            run = self.past()
            run = replace(run, cases=tuple(replace(case, **changes) for case in run.cases))
            result = self.select((run,))
            self.assertNotEqual("FRESH", result.decisions[0].state)

    def test_future_history_and_missing_boot_fail_closed(self) -> None:
        result = self.select((self.past(age=-1),))
        self.assertTrue(all(item.state == "INELIGIBLE" for item in result.decisions))
        self.snapshot.nodes[0].boot_id = None
        result = self.select()
        self.assertEqual("INELIGIBLE", result.decisions[0].state)
        self.assertIsNone(latest_assessment(node_target(self.snapshot.nodes[0]), "active", "fingerprint", (), NOW))

    def test_application_gpu_and_terminating_pods_make_nodes_ineligible(self) -> None:
        pod: dict[str, Any] = {"metadata": {"namespace": "jobs", "name": "cpu-work", "uid": "pod"},
                               "spec": {"nodeName": "gpu-01", "containers": [{}]}, "status": {"phase": "Running"}}
        for phase in ("Running", "Pending", "Unknown"):
            pod["status"]["phase"] = phase
            pod["metadata"]["deletionTimestamp"] = NOW.isoformat()
            self.assertEqual("INELIGIBLE", self.select(pods=[pod]).decisions[0].state)
        pod["status"]["phase"] = "Succeeded"
        self.assertEqual("SELECTED", self.select(pods=[pod]).decisions[0].state)
        pod["status"]["phase"] = "Running"
        pod["metadata"]["ownerReferences"] = [{"kind": "DaemonSet", "apiVersion": "apps/v1", "controller": True, "uid": "ds"}]
        self.assertEqual("SELECTED", self.select(pods=[pod]).decisions[0].state)
        pod["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": 1}}
        self.assertEqual("INELIGIBLE", self.select(pods=[pod]).decisions[0].state)

    def test_unfinished_checkpoint_blocks_even_without_pods(self) -> None:
        run = self.past()
        run = replace(run, cases=(replace(run.cases[0], state="PLANNED"), *run.cases[1:]))
        result = self.select((run,))
        self.assertEqual("INELIGIBLE", result.decisions[0].state)
        self.assertIn("unfinished", result.decisions[0].reason)

    def test_ready_stability_pressure_and_missing_condition(self) -> None:
        case = plan_healthchecks(self.snapshot, self.request).cases[0]
        for change in ("recent", "future", "missing", "duplicate", "pressure", "boot"):
            raw = copy.deepcopy(self.raw)
            if change in {"recent", "future"}:
                raw[0]["status"]["conditions"][0]["lastTransitionTime"] = (NOW + timedelta(seconds=1 if change == "future" else -299)).isoformat()
            elif change == "missing":
                raw[0]["status"]["conditions"][0].pop("lastTransitionTime")
            elif change == "duplicate":
                raw[0]["status"]["conditions"].append(raw[0]["status"]["conditions"][0])
            elif change == "boot":
                raw[0]["status"]["nodeInfo"]["bootID"] = "changed"
            else:
                raw[0]["status"]["conditions"][1]["status"] = "True"
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                validate_service_idle(case, raw, [], self.policy, NOW)
        with self.assertRaises(BenchmarkError):
            validate_service_idle(case, self.raw, [], self.policy, NOW.replace(tzinfo=None))

    def test_multi_node_requires_passed_active_check_with_cooldown(self) -> None:
        multi = replace(self.request, check_type="multi-node")
        for runs in ((), (self.past(status="FAIL"),), (self.past(age=60),), (self.past(age=86400),)):
            result = self.select(runs, request_override=multi)
            self.assertIsNone(result.plan)
            self.assertTrue(all(item.state == "NEEDS_ACTIVE" for item in result.decisions))
        result = self.select((self.past(age=120),), request_override=multi)
        self.assertEqual(1, len(result.plan.cases))
        self.assertEqual(2, len(result.plan.cases[0].nodes))
        self.assertFalse(active_prerequisite(None, self.policy, NOW))

    def test_failed_pair_is_not_repeated_without_alternate_partner(self) -> None:
        result = self.select((self.past(), self.past(category="multi-node", status="FAIL", age=700, number=2)),
                             request_override=replace(self.request, check_type="multi-node"))
        self.assertIsNone(result.plan)
        self.assertTrue(all(item.state == "NO_PARTNER" for item in result.decisions))

    def test_failed_anchor_uses_a_different_healthy_partner_from_same_pool(self) -> None:
        failed = self.past(category="multi-node", status="FAIL", age=700, number=2)
        third = copy.deepcopy(self.snapshot.nodes[1])
        third.k8s_name, third.kubernetes_uid, third.boot_id = "gpu-03", "uid-3", "boot-gpu-03"
        self.snapshot.nodes.append(third)
        raw = copy.deepcopy(self.raw[1])
        raw["metadata"].update(name="gpu-03", uid="uid-3")
        raw["metadata"]["labels"]["kubernetes.io/hostname"] = "gpu-03"
        raw["status"]["nodeInfo"]["bootID"] = third.boot_id
        self.raw.append(raw)
        multi = replace(self.request, pool=None, nodes=("gpu-01",), check_type="multi-node")
        result = self.select((self.past(), failed), request_override=multi)
        self.assertEqual(["gpu-01", "gpu-03"], [node.name for node in result.plan.cases[0].nodes])
        self.assertEqual(("gpu-01",), result.plan.cases[0].anchors)
        self.assertEqual("gpu-03", result.decisions[0].partner)

    def test_guard_rejects_freshened_selection_changed_boot_and_wrong_profile(self) -> None:
        case = self.select().plan.cases[0]
        for runs, changed in (((self.past(),), case), ((), replace(case, profile_fingerprint="f" * 64))):
            with self.assertRaises(BenchmarkError):
                validate_service_case(changed, self.request, self.policy, self.raw, [], runs, NOW)
        multi = replace(self.request, check_type="multi-node")
        pair = self.select((self.past(),), request_override=multi).plan.cases[0]
        with self.assertRaisesRegex(BenchmarkError, "recent passing active"):
            validate_service_case(pair, multi, self.policy, self.raw, [], (), NOW)
        with self.assertRaisesRegex(BenchmarkError, "different partner"):
            validate_service_case(pair, multi, self.policy, self.raw, [],
                                  (self.past(), self.past(category="multi-node", status="FAIL", age=700, number=2)), NOW)

    def test_unsupported_service_options_fail_before_selection(self) -> None:
        for requested in (replace(self.request, check_type="all"),
                          replace(self.request, network="host"),
                          replace(self.request, parameters=replace(self.request.parameters, repetitions=2)),
                          replace(self.request, check_type="multi-node", mode="all-nodes"),
                          replace(self.request, check_type="multi-node", pairing="all")):
            with self.assertRaises(BenchmarkError):
                self.select(request_override=requested)

    def test_profile_fingerprint_is_effective_and_deterministic(self) -> None:
        base = execution_fingerprint("BM.GPU.B200.8", "local", self.request)
        self.assertEqual(base, execution_fingerprint("BM.GPU.B200.8", "local", replace(self.request, nodes=("any",), pool=None)))
        self.assertNotEqual(base, execution_fingerprint("BM.GPU.B200.8", "host", self.request))
        self.assertNotEqual(base, execution_fingerprint("BM.GPU.B200.8", "local", replace(self.request, environment=("NCCL_CUMEM_ENABLE=0",))))
