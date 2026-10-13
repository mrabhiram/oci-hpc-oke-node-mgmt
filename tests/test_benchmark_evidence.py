from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock, patch

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.evidence import MAX_LOG_BYTES, CaseEvidence, EvidenceCollector, assess_case
from oke_hpc_mgmt.benchmarks.manifests import CASE_LABEL, ROLE_LABEL, case_name, owned_labels, render_mpijob
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from test_benchmark_records import record


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        kubernetes = Mock()
        kubernetes.client.ApiClient.return_value = ApiClient()
        self.backend = BenchmarkBackend(kubernetes)
        self.collector = EvidenceCollector(self.backend)
        self.core, self.batch = self.collector.core, self.collector.batch
        initial = replace(record(), uid="run-uid", resource_version="1")
        self.case = initial.plan.cases[0]
        self.checkpoint = initial.update_case(self.case.case_id, "SUBMITTING").update_case(
            self.case.case_id, "RUNNING", job_uid="mpi-uid",
        )
        self.job = render_mpijob(self.checkpoint.run_id, self.case, self.checkpoint.plan.request,
                                 self.checkpoint.options, owner_uid=self.checkpoint.uid)
        self.job["metadata"].update(uid="mpi-uid", resourceVersion="2")
        self.job["status"] = {"conditions": [{"type": "Succeeded", "status": "True"}]}
        self.backend.custom.get_namespaced_custom_object.return_value = self.job
        self.mpi_owner = {"apiVersion": "kubeflow.org/v2beta1", "kind": "MPIJob",
                          "name": case_name(self.checkpoint.run_id, self.case), "uid": "mpi-uid", "controller": True}
        self.parent: dict[str, Any] = {"metadata": {"name": "launcher-job", "namespace": "default", "uid": "launcher-job-uid",
                                    "ownerReferences": [self.mpi_owner]}}
        self.batch.read_namespaced_job.return_value = self.parent
        self.pods = [self.pod("worker"), self.pod("launcher")]
        self.core.list_namespaced_pod.return_value = {"items": self.pods}
        self.core.list_namespaced_event.return_value = {"items": []}
        self.core.read_namespaced_pod.side_effect = self.read_pod
        self.stream = self.core.read_namespaced_pod_log.return_value
        self.stream.read.return_value = b"synthetic test output\n"
        self.core.read_node.side_effect = lambda name, **_: {"metadata": {
            "name": name, "uid": self.case.nodes[0].uid if name == self.case.nodes[0].name else "cpu-uid",
        }}

    def pod(self, role: str) -> dict[str, Any]:
        owner = self.mpi_owner if role == "worker" else {
            "apiVersion": "batch/v1", "kind": "Job", "name": "launcher-job",
            "uid": "launcher-job-uid", "controller": True,
        }
        return {
            "metadata": {"name": f"{role}-pod", "namespace": "default", "uid": f"{role}-uid",
                         "labels": {**owned_labels(self.checkpoint.run_id), CASE_LABEL: self.case.case_id, ROLE_LABEL: role},
                         "ownerReferences": [copy.deepcopy(owner)]},
            "spec": {"nodeName": self.case.nodes[0].name if role == "worker" else "cpu-node",
                     "containers": [{"name": f"mpi-{role}", "image": "example.invalid/nccl:synthetic"}]},
            "status": {"phase": "Succeeded" if role == "launcher" else "Running", "containerStatuses": [{
                "name": f"mpi-{role}", "containerID": f"cri-o://{role}-id", "restartCount": 0,
                "imageID": "docker-pullable://example.invalid/nccl@sha256:" + "a" * 64,
                "state": {"terminated": {"exitCode": 0}} if role == "launcher" else {"running": {}},
            }]},
        }

    def read_pod(self, name: str, namespace: str, **_: Any) -> dict[str, Any]:
        return copy.deepcopy(next(pod for pod in self.pods if pod["metadata"]["name"] == name))

    def collect(self) -> CaseEvidence:
        return self.collector.collect(self.checkpoint, self.case)

    def test_complete_snapshot_has_exact_node_pod_image_and_exit_evidence(self) -> None:
        result = self.collect()
        self.assertTrue(result.coverage_complete)
        self.assertTrue(result.succeeded)
        self.assertFalse(result.failed)
        self.assertEqual("mpi-uid", result.job_uid)
        self.assertEqual("uid-1", result.pods[0].node_uid)
        self.assertEqual(0, result.pods[1].exit_code)
        self.assertEqual("synthetic test output\n", result.pods[1].log)
        self.assertTrue(result.pods[1].log_complete)
        self.assertEqual(MAX_LOG_BYTES + 1, self.core.read_namespaced_pod_log.call_args.kwargs["limit_bytes"])
        self.assertFalse(self.core.read_namespaced_pod_log.call_args.kwargs["_preload_content"])
        self.stream.read.assert_called_with(MAX_LOG_BYTES + 1)
        self.assertEqual(2, self.stream.close.call_count)
        self.assertEqual(2, self.stream.release_conn.call_count)
        self.assertNotIn("follow", self.core.read_namespaced_pod_log.call_args.kwargs)
        for method in self.core.mock_calls:
            self.assertTrue(str(method).startswith("call.read_") or str(method).startswith("call.list_"))
        self.backend.custom.create_namespaced_custom_object.assert_not_called()

    def test_labels_alone_do_not_authorize_log_access(self) -> None:
        self.pods[0]["metadata"]["ownerReferences"] = []
        with self.assertRaisesRegex(BenchmarkError, "controller owner"):
            self.collect()
        self.core.read_namespaced_pod_log.assert_not_called()

    def test_wrong_owner_uid_namespace_api_version_role_and_extra_container_fail(self) -> None:
        pristine = copy.deepcopy(self.pods[0])
        for mutation in ("uid", "owner-api", "namespace", "role", "case", "container", "controllers"):
            self.pods[0] = copy.deepcopy(pristine)
            metadata = self.pods[0]["metadata"]
            if mutation == "uid":
                metadata["ownerReferences"][0]["uid"] = "different"
            elif mutation == "owner-api":
                metadata["ownerReferences"][0]["apiVersion"] = "other/v1"
            elif mutation == "namespace":
                metadata["namespace"] = "other"
            elif mutation == "role":
                metadata["labels"][ROLE_LABEL] = "other"
            elif mutation == "case":
                metadata["labels"][CASE_LABEL] = "case-9999"
            elif mutation == "container":
                self.pods[0]["spec"]["containers"].append({"name": "injected"})
            else:
                metadata["ownerReferences"].append(copy.deepcopy(metadata["ownerReferences"][0]))
            with self.subTest(mutation=mutation), self.assertRaises(BenchmarkError):
                self.collect()

    def test_launcher_requires_verified_batch_job_chain(self) -> None:
        self.parent["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(BenchmarkError, "Launcher Job identity changed"):
            self.collect()
        self.parent["metadata"]["uid"] = "launcher-job-uid"
        self.parent["metadata"]["ownerReferences"][0]["uid"] = "foreign"
        with self.assertRaisesRegex(BenchmarkError, "controller chain"):
            self.collect()

    def test_replacement_after_listing_and_during_log_read_is_not_adopted(self) -> None:
        replacement = copy.deepcopy(self.pods[0])
        replacement["metadata"]["uid"] = "new-pod"
        self.core.read_namespaced_pod.side_effect = [self.pods[1], self.pods[1], replacement]
        with self.assertRaisesRegex(BenchmarkError, "after discovery"):
            self.collect()
        self.core.read_namespaced_pod.side_effect = [self.pods[1], self.pods[1], self.pods[0], replacement]
        with self.assertRaisesRegex(BenchmarkError, "do not combine attempts"):
            self.collect()

    def test_restart_or_container_id_change_during_read_invalidates_attempt(self) -> None:
        for field, value in (("restartCount", 1), ("containerID", "cri-o://replacement")):
            replacement = copy.deepcopy(self.pods[0])
            replacement["status"]["containerStatuses"][0][field] = value
            self.core.read_namespaced_pod.side_effect = [self.pods[1], self.pods[1], self.pods[0], replacement]
            with self.subTest(field=field), self.assertRaisesRegex(BenchmarkError, "do not combine attempts"):
                self.collect()

    def test_terminated_during_read_requires_final_log_refetch(self) -> None:
        launcher = self.pods[1]
        running = copy.deepcopy(launcher)
        running["status"]["containerStatuses"][0]["state"] = {"running": {}}
        self.core.read_namespaced_pod.side_effect = [running, launcher, self.pods[0], self.pods[0]]
        result = self.collect()
        self.assertFalse(result.coverage_complete)
        self.assertFalse(result.pods[1].log_complete)
        self.assertIn("completed while logs", " ".join(result.pods[1].warnings))

    def test_unavailable_or_truncated_log_never_counts_as_complete(self) -> None:
        self.core.read_namespaced_pod_log.side_effect = ApiException(status=403)
        result = self.collect()
        self.assertFalse(result.coverage_complete)
        self.assertIn("HTTP 403", " ".join(result.pods[0].warnings))
        self.core.read_namespaced_pod_log.side_effect = None
        self.stream.read.return_value = b"x" * (MAX_LOG_BYTES + 1)
        result = self.collect()
        self.assertFalse(result.coverage_complete)
        self.assertEqual(MAX_LOG_BYTES, len(result.pods[0].log))
        self.assertFalse(result.pods[0].log_complete)
        self.stream.read.return_value = {"unexpected": "json"}
        with self.assertRaisesRegex(BenchmarkError, "non-byte"):
            self.collect()

    def test_pending_and_restarted_containers_are_incomplete(self) -> None:
        status = self.pods[0]["status"]["containerStatuses"][0]
        status.pop("containerID")
        status["restartCount"] = 1
        status.pop("imageID")
        result = self.collect()
        self.assertFalse(result.coverage_complete)
        self.assertEqual(1, self.core.read_namespaced_pod_log.call_count)
        for fragment in ("not started", "restarted", "digest"):
            self.assertIn(fragment, " ".join(result.pods[0].warnings))

    def test_raw_logs_preserve_newlines_and_fail_closed_on_invalid_bytes_or_stream_error(self) -> None:
        self.stream.read.return_value = b"first\nsecond\tcolumn\n"
        self.assertEqual("first\nsecond\tcolumn\n", self.collect().pods[1].log)
        self.stream.read.return_value = b"invalid\xffbytes"
        result = self.collect()
        self.assertFalse(result.pods[1].log_complete)
        self.assertIn("UTF-8", " ".join(result.pods[1].warnings))
        self.stream.read.side_effect = OSError("stream disconnected")
        self.stream.close.reset_mock()
        self.stream.release_conn.reset_mock()
        result = self.collect()
        self.assertFalse(result.pods[1].log_complete)
        self.assertIn("stream failed", " ".join(result.pods[1].warnings))
        self.assertEqual(2, self.stream.close.call_count)
        self.assertEqual(2, self.stream.release_conn.call_count)

    def test_missing_duplicate_and_replaced_node_coverage_is_incomplete(self) -> None:
        self.core.read_node.side_effect = lambda name, **_: {"metadata": {"name": name, "uid": "replacement"}}
        result = self.collect()
        self.assertFalse(result.coverage_complete)
        self.assertIn("originally selected", " ".join(result.pods[0].warnings))
        self.core.list_namespaced_pod.return_value = {"items": [self.pods[0], self.pods[0]]}
        result = self.collect()
        self.assertIn("duplicated", " ".join(result.warnings))
        self.assertIn("one launcher", " ".join(result.warnings))
        self.core.list_namespaced_pod.return_value = {"items": []}
        self.assertFalse(self.collect().coverage_complete)

    def test_missing_uid_or_job_never_becomes_success(self) -> None:
        with self.assertRaisesRegex(BenchmarkError, "Persist the MPIJob UID"):
            self.collector.collect(record(), self.case)
        self.backend.custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        with self.assertRaisesRegex(BenchmarkError, "MPIJob is missing"):
            self.collect()
        self.backend.custom.get_namespaced_custom_object.side_effect = [self.job, ApiException(status=404)]
        with self.assertRaisesRegex(BenchmarkError, "disappeared"):
            self.collect()

    def test_pagination_is_never_silently_ignored(self) -> None:
        for response in ({"metadata": {"continue": "token"}, "items": self.pods},
                         {"items": [self.pods[0]] * 100}):
            self.core.list_namespaced_pod.return_value = response
            with self.assertRaisesRegex(BenchmarkError, "Too many Pods"):
                self.collect()
        self.core.read_namespaced_pod_log.assert_not_called()

    def test_status_change_suspend_and_conflicting_conditions_invalidate_snapshot(self) -> None:
        changed = copy.deepcopy(self.job)
        changed["spec"]["runPolicy"]["suspend"] = True
        changed["status"]["conditions"].append({"type": "Failed", "status": "True"})
        self.backend.custom.get_namespaced_custom_object.side_effect = [self.job, changed]
        result = self.collect()
        self.assertFalse(result.coverage_complete)
        self.assertTrue(result.suspended)
        self.assertTrue(result.failed)
        for fragment in ("changed", "conflicting", "suspended"):
            self.assertIn(fragment, " ".join(result.warnings))

    def test_api_failures_and_malformed_identity_are_not_empty_evidence(self) -> None:
        self.core.list_namespaced_pod.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "HTTP 403"):
            self.collect()
        self.core.list_namespaced_pod.side_effect = None
        status = self.pods[0]["status"]["containerStatuses"][0]
        status["restartCount"] = -1
        with self.assertRaisesRegex(BenchmarkError, "restart count"):
            self.collect()
        status["restartCount"] = 0
        status["state"] = {"terminated": {"exitCode": "0"}}
        with self.assertRaisesRegex(BenchmarkError, "exit code"):
            self.collect()
        status["state"] = {"running": {}}
        self.pods[0]["status"]["containerStatuses"].append(copy.deepcopy(status))
        with self.assertRaisesRegex(BenchmarkError, "Duplicate"):
            self.collect()

    def test_aggregate_log_budget_prioritizes_launcher_and_bounds_memory(self) -> None:
        self.stream.read.return_value = b"x" * 10
        with patch("oke_hpc_mgmt.benchmarks.evidence.MAX_CASE_LOG_BYTES", 5):
            result = self.collect()
        self.assertEqual(5, sum(len(pod.log.encode()) for pod in result.pods))
        self.assertEqual("x" * 5, result.pods[1].log)
        self.assertIn("budget exhausted", " ".join(result.pods[0].warnings))
        self.assertEqual(1, self.core.read_namespaced_pod_log.call_count)
        self.assertEqual("launcher-pod", self.core.read_namespaced_pod_log.call_args.args[0])
        self.assertFalse(result.coverage_complete)

    def test_late_pod_creation_or_restart_invalidates_snapshot(self) -> None:
        changed = copy.deepcopy(self.pods)
        changed[0]["status"]["containerStatuses"][0]["restartCount"] = 1
        for latest in ({"items": changed}, {"items": self.pods[:1]},
                       {"items": self.pods, "metadata": {"continue": "token"}}):
            self.core.list_namespaced_pod.side_effect = [{"items": self.pods}, latest]
            with self.assertRaisesRegex(BenchmarkError, "Pod set changed"):
                self.collect()

    def test_invalid_image_digest_and_missing_node_identity_do_not_pass(self) -> None:
        self.pods[0]["status"]["containerStatuses"][0]["imageID"] = "sha256:not-a-digest"
        self.assertIn("digest", " ".join(self.collect().pods[0].warnings))
        self.core.read_node.side_effect = None
        self.core.read_node.return_value = {"metadata": {"name": "cpu-node"}}
        with self.assertRaisesRegex(BenchmarkError, "Node identity is unavailable"):
            self.collect()

    def passing_evidence(self) -> CaseEvidence:
        result = self.collect()
        node = self.case.nodes[0].name
        lines = [f"MGMT_NCCL_RANK\t{rank}\t{rank}\t{node}\tworker-uid\n"
                 f"# Rank {rank} Group 0 Pid {100 + rank} on {node} device {rank}\n"
                 for rank in range(self.case.ranks)]
        lines.extend([
            "8589934592 2147483648 float sum -1 200 43.0 43.0 0 201 42.0 42.0 0\n",
            "# Avg bus bandwidth : 42.0\n",
        ])
        return replace(result, pods=(result.pods[0], replace(result.pods[1], log="".join(lines))))

    def test_assessment_uses_uid_bound_launcher_evidence(self) -> None:
        result = assess_case(self.case, self.passing_evidence())
        self.assertEqual("PASS", result.status)
        self.assertEqual("UNGRADED", result.performance)
        self.assertEqual("LOCAL", result.transport)
        with self.assertRaisesRegex(BenchmarkError, "different benchmark case"):
            assess_case(self.case, replace(self.passing_evidence(), case_id="case-9999"))

    def test_evidence_gaps_override_apparently_passing_nccl_metrics(self) -> None:
        evidence = self.passing_evidence()
        for changed in (
            replace(evidence, warnings=("Unverified snapshot",)),
            replace(evidence, pods=(replace(evidence.pods[0], warnings=("Unverified worker",)), evidence.pods[1])),
            replace(evidence, pods=(replace(evidence.pods[0], log_complete=False), evidence.pods[1])),
        ):
            result = assess_case(self.case, changed, min_bus_bandwidth=1)
            self.assertEqual("INCOMPLETE", result.status)
            self.assertEqual("UNVERIFIED", result.correctness)
            self.assertEqual("UNGRADED", result.performance)
        self.assertEqual("FAIL", assess_case(self.case, replace(evidence, failed=True)).status)
        self.assertEqual("FAIL", assess_case(self.case, replace(evidence, pods=(
            replace(evidence.pods[0], exit_code=1), evidence.pods[1],
        ))).status)
        self.assertEqual("INCOMPLETE", assess_case(self.case, replace(evidence, suspended=True)).status)
        self.assertEqual("INCOMPLETE", assess_case(self.case, replace(evidence, pods=())).status)
        self.assertEqual("INCOMPLETE", assess_case(self.case, replace(evidence, pods=(
            *evidence.pods, evidence.pods[1],
        ))).status)


if __name__ == "__main__":
    unittest.main()
