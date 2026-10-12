from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

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
