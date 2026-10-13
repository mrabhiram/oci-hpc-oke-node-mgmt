from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock, patch

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.manifests import owned_labels, render_mpijob
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.records import RunRecord, mark_cancelling, payload_digest
from test_benchmark_preflight import gpu_pod, raw_inventory
from test_benchmark_records import record


def configmap(run: RunRecord) -> dict:
    return {
        "metadata": {"name": run.run_id, "namespace": run.plan.request.namespace,
                     "uid": run.uid or "run-uid", "resourceVersion": run.resource_version or "1",
                     "labels": owned_labels(run.run_id)},
        "data": {"record.json": run.payload(), "sha256": payload_digest(run.payload())},
    }


class BenchmarkBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kubernetes = Mock()
        self.kubernetes.client.ApiClient.return_value = ApiClient()
        self.backend = BenchmarkBackend(self.kubernetes)
        self.core, self.custom = self.backend.core, self.backend.custom
        self.checkpoint = replace(record(), uid="run-uid", resource_version="1")
        self.case = self.checkpoint.plan.cases[0]
        self.core.read_namespaced_config_map.return_value = configmap(self.checkpoint)

    def job(self) -> dict:
        body = render_mpijob(self.checkpoint.run_id, self.case, self.checkpoint.plan.request, self.checkpoint.options, owner_uid=self.checkpoint.uid)
        body["metadata"].update({"uid": "job-uid", "resourceVersion": "2"})
        return body

    def test_api_read_failures_do_not_turn_into_empty_gpu_inventory(self) -> None:
        self.core.read_node.side_effect = raw_inventory()
        self.core.list_pod_for_all_namespaces.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "Cannot verify"):
            self.backend.runtime_preflight(self.case)
        self.core.list_pod_for_all_namespaces.side_effect = None
        self.core.list_pod_for_all_namespaces.return_value = {"items": [gpu_pod()]}
        self.core.read_node.side_effect = raw_inventory()
        with self.assertRaisesRegex(BenchmarkError, "already allocated"):
            self.backend.runtime_preflight(self.case)
        self.core.list_pod_for_all_namespaces.return_value = {"items": []}
        self.core.read_node.side_effect = raw_inventory()
        self.assertEqual((("gpu-01", "uid-1"),), self.backend.runtime_preflight(self.case).nodes)
        self.custom.create_namespaced_custom_object.assert_not_called()

    def test_allocation_inventory_is_bounded_and_requires_a_pod_list(self) -> None:
        for names in ((), ("gpu-01", "gpu-01")):
            with self.assertRaisesRegex(BenchmarkError, "bounded unique"):
                self.backend.read_allocation_inventory(names)
        self.core.read_node.assert_not_called()
        self.core.read_node.return_value = raw_inventory()[0]
        self.core.list_pod_for_all_namespaces.return_value = {}
        with self.assertRaisesRegex(BenchmarkError, "verified Pod list"):
            self.backend.read_allocation_inventory(("gpu-01",))

    def test_read_and_list_validate_ownership_hash_and_namespaced_identity(self) -> None:
        self.assertEqual(self.checkpoint, self.backend.get_run("default", self.checkpoint.run_id))
        self.core.list_namespaced_config_map.return_value = {"items": [configmap(self.checkpoint)]}
        self.assertEqual((self.checkpoint,), self.backend.list_runs("default"))
        for key, value in (("labels", {}), ("uid", ""), ("namespace", "different"),
                           ("deletionTimestamp", "2026-01-01T00:00:00Z")):
            body = configmap(self.checkpoint)
            body["metadata"][key] = value
            self.core.read_namespaced_config_map.return_value = body
            with self.subTest(key=key), self.assertRaises(BenchmarkError):
                self.backend.get_run("default", self.checkpoint.run_id)
        body = configmap(self.checkpoint)
        body["data"]["record.json"] += " "
        self.core.read_namespaced_config_map.return_value = body
        with self.assertRaisesRegex(BenchmarkError, "integrity"):
            self.backend.get_run("default", self.checkpoint.run_id)
        self.core.read_namespaced_config_map.side_effect = ApiException(status=404)
        with self.assertRaisesRegex(BenchmarkError, "HTTP 404"):
            self.backend.get_run("default", self.checkpoint.run_id)
        self.core.list_namespaced_config_map.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "HTTP 403"):
            self.backend.list_runs("default")
        with self.assertRaisesRegex(BenchmarkError, "unexpected"):
            self.backend._object([])

    def test_history_reads_all_namespaces_and_all_pages_for_overlap_detection(self) -> None:
        other = replace(self.checkpoint, run_id="hc-" + "f" * 20, uid="other-uid")
        other = replace(other, cases=tuple(replace(case, job_name=f"{other.run_id}-{case.case_id}") for case in other.cases),
                        plan=replace(other.plan, request=replace(other.plan.request, namespace="other")))
        self.core.list_config_map_for_all_namespaces.side_effect = [
            {"metadata": {"continue": "next-page"}, "items": [configmap(self.checkpoint)]},
            {"items": [configmap(other)]},
        ]
        result = self.backend.list_runs(None)
        self.assertEqual({"default", "other"}, {run.plan.request.namespace for run in result})
        self.assertEqual("next-page", self.core.list_config_map_for_all_namespaces.call_args.kwargs["_continue"])
        self.core.list_namespaced_config_map.assert_not_called()

    def test_pagination_limits_and_duplicates_fail_instead_of_partial_overlap_inventory(self) -> None:
        body = configmap(self.checkpoint)
        self.core.list_config_map_for_all_namespaces.return_value = {"items": [body, body]}
        with self.assertRaisesRegex(BenchmarkError, "duplicate run UIDs"):
            self.backend.list_runs(None)
        self.core.list_config_map_for_all_namespaces.return_value = {"metadata": {"continue": "same"}, "items": []}
        with self.assertRaisesRegex(BenchmarkError, "pagination repeated"):
            self.backend.list_runs(None)
        self.core.list_config_map_for_all_namespaces.return_value = {"items": [body]}
        for setting in ("MAX_RUNS", "MAX_HISTORY_BYTES"):
            with patch("oke_hpc_mgmt.benchmarks.backend." + setting, 0):
                with self.assertRaisesRegex(BenchmarkError, "inventory limits"):
                    self.backend.list_runs(None)
        self.core.read_node.side_effect = raw_inventory()
        self.core.list_pod_for_all_namespaces.return_value = {"metadata": {"continue": "next"}, "items": []}
        with self.assertRaisesRegex(BenchmarkError, "incomplete"):
            self.backend.runtime_preflight(self.case)

    def test_creation_does_not_overwrite_colliding_checkpoint(self) -> None:
        initial = record()
        self.core.create_namespaced_config_map.return_value = configmap(initial)
        saved = self.backend.create_run(initial)
        self.assertEqual("run-uid", saved.uid)
        body = self.core.create_namespaced_config_map.call_args.args[1]
        self.assertEqual("ConfigMap", body["kind"])
        self.assertEqual(initial.plan.to_dict()["selected_nodes"], saved.plan.selected_nodes)
        self.core.create_namespaced_config_map.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "inspect run"):
            self.backend.create_run(initial)
        with self.assertRaisesRegex(BenchmarkError, "Only a new"):
            self.backend.create_run(saved)
        self.core.patch_namespaced_config_map.assert_not_called()

    def test_save_uses_compare_and_swap_and_does_not_retry_conflict(self) -> None:
        updated = self.checkpoint.update_case(self.case.case_id, "SUBMITTING")
        response = configmap(replace(updated, resource_version="2"))
        self.core.patch_namespaced_config_map.return_value = response
        saved = self.backend.save_run(updated)
        self.assertEqual("2", saved.resource_version)
        patch = self.core.patch_namespaced_config_map.call_args.args[2]
        self.assertEqual({"uid": "run-uid", "resourceVersion": "1"}, patch["metadata"])
        for bad in (replace(updated, uid="other"), replace(updated, resource_version=""),
                    replace(updated, revision=3), replace(updated, created_at="different")):
            with self.assertRaises(BenchmarkError):
                self.backend.save_run(bad)
        self.core.patch_namespaced_config_map.reset_mock()
        self.core.patch_namespaced_config_map.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "reread"):
            self.backend.save_run(updated)
        self.assertEqual(1, self.core.patch_namespaced_config_map.call_count)

    def test_job_submission_requires_recorded_intent_and_captures_uid(self) -> None:
        with self.assertRaisesRegex(BenchmarkError, "SUBMITTING"):
            self.backend.create_job(self.checkpoint, self.case)
        submitting = self.checkpoint.update_case(self.case.case_id, "SUBMITTING")
        with self.assertRaisesRegex(BenchmarkError, "Persist and reread"):
            self.backend.create_job(submitting, self.case)
        self.core.read_namespaced_config_map.return_value = configmap(submitting)
        self.custom.create_namespaced_custom_object.return_value = self.job()
        observed = self.backend.create_job(submitting, self.case)
        self.assertEqual("job-uid", observed["metadata"]["uid"])
        self.core.read_namespaced_config_map.return_value = configmap(submitting)
        queued = submitting.update_case(self.case.case_id, "QUEUED", job_uid="job-uid")
        self.core.patch_namespaced_config_map.return_value = configmap(replace(queued, resource_version="2"))
        saved = self.backend.record_job_uid(submitting, self.case, observed)
        self.assertEqual("job-uid", saved.cases[0].job_uid)
        self.custom.create_namespaced_custom_object.side_effect = ApiException(status=504)
        with self.assertRaisesRegex(BenchmarkError, "reread the existing case"):
            self.backend.create_job(submitting, self.case)

    def test_cancellation_and_cleanup_refuse_label_only_or_replaced_job(self) -> None:
        submitting = self.checkpoint.update_case(self.case.case_id, "SUBMITTING")
        running = submitting.update_case(self.case.case_id, "RUNNING", job_uid="job-uid")
        cancelling = mark_cancelling(running)
        self.core.read_namespaced_config_map.return_value = configmap(cancelling)
        change: dict[str, Any]
        for change in ({"labels": {}}, {"ownerReferences": []}, {"uid": "different"},
                       {"resourceVersion": ""}, {"namespace": "other"}, {"name": "foreign"}):
            body = self.job()
            body["metadata"].update(change)
            self.custom.get_namespaced_custom_object.return_value = body
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                self.backend.cancel_job(cancelling, self.case)
        self.custom.patch_namespaced_custom_object.assert_not_called()
        with self.assertRaisesRegex(BenchmarkError, "cancellation intent"):
            self.backend.cancel_job(running, self.case)
        with self.assertRaisesRegex(BenchmarkError, "Export terminal-case evidence"):
            self.backend.delete_job(running, self.case)

    def test_cancel_and_delete_use_uid_and_resource_version_preconditions(self) -> None:
        running = self.checkpoint.update_case(self.case.case_id, "SUBMITTING").update_case(
            self.case.case_id, "RUNNING", job_uid="job-uid",
        )
        self.custom.get_namespaced_custom_object.return_value = self.job()
        cancelling = mark_cancelling(running)
        self.core.read_namespaced_config_map.return_value = configmap(cancelling)
        self.backend.cancel_job(cancelling, self.case)
        patch = self.custom.patch_namespaced_custom_object.call_args.args[5]
        self.assertEqual({"uid": "job-uid", "resourceVersion": "2"}, patch["metadata"])
        self.assertEqual({"runPolicy": {"suspend": True}}, patch["spec"])
        terminal = running.update_case(self.case.case_id, "CANCELLED", evidence_sha256="a" * 64)
        self.core.read_namespaced_config_map.return_value = configmap(terminal)
        self.backend.delete_job(terminal, self.case)
        body = self.custom.delete_namespaced_custom_object.call_args.kwargs["body"]
        self.assertEqual({"uid": "job-uid", "resourceVersion": "2"}, body["preconditions"])
        self.assertEqual("Foreground", body["propagationPolicy"])
        self.custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        self.backend.delete_job(terminal, self.case)
        self.core.read_namespaced_config_map.return_value = configmap(cancelling)
        self.backend.cancel_job(cancelling, self.case)
        self.assertEqual(1, self.custom.delete_namespaced_custom_object.call_count)

    def test_api_errors_are_not_treated_as_confirmed_cancel_or_cleanup(self) -> None:
        cancelling = mark_cancelling(self.checkpoint)
        self.core.read_namespaced_config_map.return_value = configmap(cancelling)
        self.custom.get_namespaced_custom_object.return_value = self.job()
        self.custom.patch_namespaced_custom_object.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "Cancellation was not confirmed"):
            self.backend.cancel_job(cancelling, self.case)
        terminal = self.checkpoint.update_case(self.case.case_id, "CANCELLED", evidence_sha256="a" * 64)
        self.core.read_namespaced_config_map.return_value = configmap(terminal)
        self.custom.delete_namespaced_custom_object.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "Cleanup was not confirmed"):
            self.backend.delete_job(terminal, self.case)
        self.custom.delete_namespaced_custom_object.side_effect = ApiException(status=404)
        self.backend.delete_job(terminal, self.case)
        self.custom.get_namespaced_custom_object.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "Cannot inspect"):
            self.backend.get_job(self.checkpoint, self.case)

    def test_backend_never_calls_node_or_other_workload_mutation(self) -> None:
        self.core.read_node.side_effect = raw_inventory()
        self.core.list_pod_for_all_namespaces.return_value = {"items": []}
        before = copy.deepcopy(self.checkpoint.plan.to_dict())
        self.backend.runtime_preflight(self.case)
        self.assertEqual(before, self.checkpoint.plan.to_dict())
        serialized_calls = json.dumps([str(call) for call in self.core.mock_calls])
        for forbidden in ("patch_node", "delete_node", "eviction", "create_namespaced_pod", "delete_namespaced_pod"):
            self.assertNotIn(forbidden, serialized_calls)

    def test_wrong_case_and_stale_checkpoint_never_mutate(self) -> None:
        submitting = self.checkpoint.update_case(self.case.case_id, "SUBMITTING")
        self.core.read_namespaced_config_map.return_value = configmap(submitting)
        wrong_case = replace(self.case, nodes=self.case.nodes + self.checkpoint.plan.cases[1].nodes)
        with self.assertRaisesRegex(BenchmarkError, "immutable plan"):
            self.backend.create_job(submitting, wrong_case)
        self.custom.get_namespaced_custom_object.return_value = self.job()
        with self.assertRaisesRegex(BenchmarkError, "immutable plan"):
            self.backend.get_job(submitting, wrong_case)
        cancelling = mark_cancelling(submitting)
        with self.assertRaisesRegex(BenchmarkError, "Persist and reread"):
            self.backend.cancel_job(cancelling, self.case)
        terminal = submitting.update_case(self.case.case_id, "FAILED", evidence_sha256="a" * 64)
        with self.assertRaisesRegex(BenchmarkError, "Persist and reread"):
            self.backend.delete_job(terminal, self.case)
        self.custom.create_namespaced_custom_object.assert_not_called()
        self.custom.patch_namespaced_custom_object.assert_not_called()
        self.custom.delete_namespaced_custom_object.assert_not_called()

    def test_resource_version_changes_after_export_block_cancel_and_delete(self) -> None:
        submitting = self.checkpoint.update_case(self.case.case_id, "SUBMITTING")
        running = submitting.update_case(self.case.case_id, "RUNNING", job_uid="job-uid")
        cancelling = mark_cancelling(running)
        self.core.read_namespaced_config_map.return_value = configmap(cancelling)
        self.custom.get_namespaced_custom_object.return_value = self.job()
        with self.assertRaisesRegex(BenchmarkError, "changed after evidence"):
            self.backend.cancel_job(cancelling, self.case, evidence_resource_version="stale")
        terminal = running.update_case(self.case.case_id, "CANCELLED", evidence_sha256="a" * 64)
        self.core.read_namespaced_config_map.return_value = configmap(terminal)
        with self.assertRaisesRegex(BenchmarkError, "changed after evidence"):
            self.backend.delete_job(terminal, self.case, evidence_resource_version="stale")
        self.custom.patch_namespaced_custom_object.assert_not_called()
        self.custom.delete_namespaced_custom_object.assert_not_called()

    def test_pod_release_requires_a_complete_empty_inventory(self) -> None:
        for response in ({"items": [{"metadata": {"uid": "still-running"}}]},
                         {"items": [], "metadata": {"continue": "next"}}):
            self.core.list_namespaced_pod.return_value = response
            self.assertFalse(self.backend.case_pods_absent(self.checkpoint, self.case))
        self.core.list_namespaced_pod.return_value = {"items": []}
        self.assertTrue(self.backend.case_pods_absent(self.checkpoint, self.case))
        self.assertEqual(1, self.core.list_namespaced_pod.call_args.kwargs["limit"])
        self.core.list_namespaced_pod.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "not confirmed released"):
            self.backend.case_pods_absent(self.checkpoint, self.case)
        with self.assertRaisesRegex(BenchmarkError, "immutable plan"):
            self.backend.case_pods_absent(self.checkpoint, replace(self.case, case_id="case-9999"))

    def test_transport_failures_remain_unconfirmed_without_automatic_retries(self) -> None:
        submitting = self.checkpoint.update_case(self.case.case_id, "SUBMITTING")
        self.core.read_namespaced_config_map.return_value = configmap(submitting)
        self.custom.create_namespaced_custom_object.side_effect = HTTPError("response lost")
        with self.assertRaisesRegex(BenchmarkError, "submission was not confirmed"):
            self.backend.create_job(submitting, self.case)
        self.assertEqual(1, self.custom.create_namespaced_custom_object.call_count)
        self.custom.get_namespaced_custom_object.side_effect = OSError("connection reset")
        with self.assertRaisesRegex(BenchmarkError, "do not create a replacement"):
            self.backend.get_job(submitting, self.case)
        self.core.create_namespaced_config_map.side_effect = OSError("connection reset")
        with self.assertRaisesRegex(BenchmarkError, "inspect run"):
            self.backend.create_run(record())
        self.core.patch_namespaced_config_map.side_effect = HTTPError("response lost")
        with self.assertRaisesRegex(BenchmarkError, "same run"):
            self.backend.save_run(submitting.update_case(self.case.case_id, "SUBMITTING"))
        self.core.read_namespaced_config_map.side_effect = OSError("connection reset")
        with self.assertRaisesRegex(BenchmarkError, "state is unverified"):
            self.backend.get_run("default", self.checkpoint.run_id)
        self.core.list_config_map_for_all_namespaces.side_effect = HTTPError("response lost")
        with self.assertRaisesRegex(BenchmarkError, "overlap inventory is unverified"):
            self.backend.list_runs(None)

    def test_cancel_cleanup_and_pod_inventory_transport_errors_are_not_success(self) -> None:
        cancelling = mark_cancelling(self.checkpoint)
        self.core.read_namespaced_config_map.return_value = configmap(cancelling)
        self.custom.get_namespaced_custom_object.return_value = self.job()
        self.custom.patch_namespaced_custom_object.side_effect = OSError("connection reset")
        with self.assertRaisesRegex(BenchmarkError, "Cancellation could not reach"):
            self.backend.cancel_job(cancelling, self.case)
        terminal = self.checkpoint.update_case(self.case.case_id, "CANCELLED", evidence_sha256="a" * 64)
        self.core.read_namespaced_config_map.return_value = configmap(terminal)
        self.custom.delete_namespaced_custom_object.side_effect = HTTPError("response lost")
        with self.assertRaisesRegex(BenchmarkError, "resource removal is unverified"):
            self.backend.delete_job(terminal, self.case)
        self.core.list_namespaced_pod.side_effect = HTTPError("response lost")
        with self.assertRaisesRegex(BenchmarkError, "not confirmed released"):
            self.backend.case_pods_absent(terminal, self.case)
