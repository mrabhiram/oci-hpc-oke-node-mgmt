from __future__ import annotations

import json
import unittest
from dataclasses import replace

from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions
from oke_hpc_mgmt.benchmarks.models import BenchmarkError, HealthcheckRequest
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks
from oke_hpc_mgmt.benchmarks.records import (
    EvidenceSnapshot, RunRecord, assert_no_overlap, decode_record, mark_cancelling, next_cases, payload_digest, timestamp,
)
from test_benchmark_planning import inventory, request


RUN = "hc-0123456789abcdefabcd"


def record() -> RunRecord:
    return RunRecord.create(RUN, plan_healthchecks(inventory(4), request(check_type="all")),
                            ExecutionOptions(concurrency=2))


class BenchmarkRecordTests(unittest.TestCase):
    def test_overlaps_reserve_planned_and_active_nodes_across_namespaces_by_uid(self) -> None:
        current = record()
        other = replace(current, uid="other-uid", plan=replace(current.plan, request=replace(current.plan.request, namespace="other")))
        with self.assertRaisesRegex(BenchmarkError, "overlap unfinished healthcheck other/"):
            assert_no_overlap(current.plan, (other,))
        assert_no_overlap(current.plan, (other,), exclude_uid="other-uid")
        with self.assertRaises(BenchmarkError):
            assert_no_overlap(current.plan, (other,), exclude_uid="different-uid")
        for case in other.cases:
            other = other.update_case(case.case_id, "CANCELLED")
        assert_no_overlap(current.plan, (other,))
        replaced_nodes = replace(current.plan, cases=tuple(replace(case, nodes=tuple(
            replace(node, uid="replacement-" + node.uid) for node in case.nodes
        )) for case in current.plan.cases))
        assert_no_overlap(replaced_nodes, (current,))

    def test_roundtrip_preserves_typed_plan_without_embedding_object_identity(self) -> None:
        original = replace(record(), uid="object-uid", resource_version="1")
        decoded = decode_record(original.payload(), uid="object-uid", resource_version="2")
        self.assertEqual(original, decoded)
        self.assertEqual("2", decoded.resource_version)
        self.assertNotIn("resource_version", json.loads(original.payload()))
        self.assertNotIn("uid", json.loads(original.payload()))
        self.assertEqual(64, len(payload_digest(original.payload())))
        self.assertEqual(payload_digest(original.payload()), payload_digest(decoded.payload()))

    def test_passive_does_not_create_record(self) -> None:
        plan = plan_healthchecks(inventory(1), HealthcheckRequest(pool="gpu-pool"))
        with self.assertRaisesRegex(BenchmarkError, "Passive"):
            RunRecord.create(RUN, plan, ExecutionOptions())

    def test_transitions_require_intent_and_job_identity(self) -> None:
        initial = record()
        case_id = initial.cases[0].case_id
        with self.assertRaisesRegex(BenchmarkError, "Invalid case transition"):
            initial.update_case(case_id, "RUNNING", job_uid="job-uid")
        submitting = initial.update_case(case_id, "SUBMITTING")
        self.assertEqual(2, submitting.revision)
        with self.assertRaisesRegex(BenchmarkError, "requires its MPIJob UID"):
            submitting.update_case(case_id, "RUNNING")
        queued = submitting.update_case(case_id, "QUEUED", job_uid="job-uid")
        running = queued.update_case(case_id, "RUNNING")
        with self.assertRaisesRegex(BenchmarkError, "replacement MPIJob"):
            running.update_case(case_id, "RUNNING", job_uid="new-job-uid")
        with self.assertRaisesRegex(BenchmarkError, "new exported observation"):
            running.update_case(case_id, "QUEUED")
        with self.assertRaisesRegex(BenchmarkError, "passing assessment"):
            running.update_case(case_id, "SUCCEEDED")
        complete = running.update_case(case_id, "SUCCEEDED", result={"status": "PASS"}, evidence_sha256="a" * 64)
        self.assertEqual("SUCCEEDED", complete.cases[0].state)
        self.assertEqual("RUNNING", complete.state)
        with self.assertRaises(BenchmarkError):
            complete.update_case(case_id, "RUNNING")
        with self.assertRaises(BenchmarkError):
            initial.update_case("missing", "SUBMITTING")
        with self.assertRaisesRegex(BenchmarkError, "cannot change case identity"):
            initial.update_case(case_id, "SUBMITTING", job_name="foreign-job")

    def test_case_completion_aggregates_without_claiming_partial_success(self) -> None:
        for terminal, final in (("FAILED", "FAILED"), ("INCOMPLETE", "INCOMPLETE"), ("CANCELLED", "CANCELLED")):
            current = record()
            for item in current.cases:
                current = current.update_case(item.case_id, "SUBMITTING")
                current = current.update_case(item.case_id, terminal)  # type: ignore[arg-type]
            self.assertEqual(final, current.state)
            self.assertFalse(next_cases(current))
        current = record()
        for item in current.cases:
            current = current.update_case(item.case_id, "SUBMITTING")
            current = current.update_case(item.case_id, "SUCCEEDED", job_uid=f"uid-{item.case_id}",
                                           result={"status": "PASS"}, evidence_sha256="a" * 64)
        self.assertEqual("SUCCEEDED", current.state)
        self.assertEqual(current, mark_cancelling(current))

    def test_bounded_disjoint_concurrency(self) -> None:
        current = record()
        first = next_cases(current)
        self.assertEqual(["gpu-01", "gpu-02"], [case.nodes[0].name for case in first])
        for case in first:
            current = current.update_case(case.case_id, "SUBMITTING")
        self.assertFalse(next_cases(current))
        current = current.update_case(first[0].case_id, "FAILED")
        selected = next_cases(current)
        self.assertEqual(1, len(selected))
        self.assertEqual("gpu-03", selected[0].nodes[0].name)
        # With all active single-node cases complete, overlapping pair cases
        # still cannot be submitted concurrently merely because slots are free.
        for item in current.cases:
            if item.state in {"PLANNED", "SUBMITTING"} and item.case_id in {"case-0002", "case-0003", "case-0004"}:
                if item.state == "PLANNED":
                    current = current.update_case(item.case_id, "SUBMITTING")
                current = current.update_case(item.case_id, "FAILED")
        pair_cases = next_cases(current)
        self.assertEqual(1, len(pair_cases))
        self.assertEqual(2, len(pair_cases[0].nodes))

    def test_cancel_stops_new_submission_and_cleaned_is_immutable(self) -> None:
        current = record()
        cancelling = mark_cancelling(current)
        self.assertEqual("CANCELLING", cancelling.state)
        self.assertFalse(next_cases(cancelling))
        for item in cancelling.cases:
            cancelling = cancelling.update_case(item.case_id, "CANCELLED")
        self.assertEqual("CANCELLED", cancelling.state)
        cleaned = replace(cancelling, state="CLEANED")
        with self.assertRaises(BenchmarkError):
            cleaned.update_case(cleaned.cases[0].case_id, "CANCELLED")
        with self.assertRaises(BenchmarkError):
            mark_cancelling(cleaned)

    def test_corrupt_records_and_false_success_are_rejected(self) -> None:
        encoded = record().payload()
        for key, value in (("schema_version", 2), ("revision", True), ("state", "FAKE"),
                           ("state", "SUCCEEDED"), ("uid", "injected"), ("run_id", "bad"),
                           ("cases", []), ("plan", "not-a-plan"), ("options", {"timeout": 0})):
            payload = json.loads(encoded)
            payload[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(BenchmarkError):
                decode_record(json.dumps(payload), uid="uid", resource_version="1")
        for payload in ("{}", "[]", "null", "broken", "x" * 700001):
            with self.assertRaises(BenchmarkError):
                decode_record(payload, uid="uid", resource_version="1")
        altered = json.loads(encoded)
        altered["cases"][0]["job_name"] = "foreign"
        with self.assertRaisesRegex(BenchmarkError, "identities differ"):
            decode_record(json.dumps(altered), uid="uid", resource_version="1")
        altered = json.loads(encoded)
        altered["cases"][0]["state"] = "RUNNING"
        with self.assertRaisesRegex(BenchmarkError, "MPIJob UID"):
            decode_record(json.dumps(altered), uid="uid", resource_version="1")
        with self.assertRaises(BenchmarkError):
            decode_record(encoded, uid="", resource_version="1")
        oversized = replace(record(), cases=(replace(record().cases[0], error="x" * 700001),))
        with self.assertRaisesRegex(BenchmarkError, "limit"):
            oversized.payload()

    def test_requeues_require_append_only_exported_observations(self) -> None:
        initial = record()
        case = initial.cases[0]
        running = initial.update_case(case.case_id, "SUBMITTING").update_case(case.case_id, "RUNNING", job_uid="uid")
        snapshot = EvidenceSnapshot("2026-01-01T00:00:00Z", "QUEUED", (), "a" * 64)
        queued = running.update_case(case.case_id, "QUEUED", snapshots=(snapshot,))
        self.assertEqual((snapshot,), queued.cases[0].snapshots)
        self.assertEqual(queued, decode_record(queued.payload(), uid="uid", resource_version="1"))
        with self.assertRaisesRegex(BenchmarkError, "append-only"):
            queued.update_case(case.case_id, "QUEUED", snapshots=())
        with self.assertRaisesRegex(BenchmarkError, "limit"):
            queued.update_case(case.case_id, "QUEUED", snapshots=(snapshot,) * 129)

    def test_snapshot_identity_time_and_state_validation(self) -> None:
        good = EvidenceSnapshot("2026-01-01T00:00:00Z", "RUNNING", (("pod", "uid", "container", 0),), "a" * 64)
        good.validate()
        for invalid in (replace(good, phase="other"), replace(good, sha256="bad"),  # type: ignore[arg-type]
                        replace(good, observed_at="yesterday"), replace(good, pod_identities=good.pod_identities * 2),
                        replace(good, pod_identities=(("pod", "uid", None, -1),))):
            with self.subTest(invalid=invalid), self.assertRaises(BenchmarkError):
                invalid.validate()
        for value in ("2026-01-01T00:00:00", "invalid", None):
            with self.assertRaises(BenchmarkError):
                timestamp(value)  # type: ignore[arg-type]
