from __future__ import annotations

import copy
import unittest
from dataclasses import asdict, replace
from unittest.mock import Mock, patch

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.diagnostics import JOB_UID_LABEL, SchedulingCollector, SchedulingEvidence
from oke_hpc_mgmt.benchmarks.manifests import case_name
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from test_benchmark_capabilities import crd
from test_benchmark_records import record


class SchedulingEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        client = Mock()
        client.client.ApiClient.return_value = ApiClient()
        self.backend = BenchmarkBackend(client)
        self.collector = SchedulingCollector(self.backend)
        initial = record()
        self.case = initial.plan.cases[0]
        self.run = replace(initial, plan=replace(initial.plan, request=replace(initial.plan.request, queue="test-queue")))
        self.run = self.run.update_case(self.case.case_id, "SUBMITTING").update_case(self.case.case_id, "QUEUED", job_uid="job-uid")
        self.workload = {
            "metadata": {"name": "test-workload", "namespace": "default", "uid": "workload-uid", "resourceVersion": "42",
                         "annotations": {"unrelated": "not exported"}, "ownerReferences": [{
                             "apiVersion": "kubeflow.org/v2beta1", "kind": "MPIJob", "controller": True,
                             "name": case_name(self.run.run_id, self.case), "uid": "job-uid",
                         }]},
            "spec": {"queueName": "test-queue", "podSets": ["not exported"]},
            "status": {"conditions": [{"type": "QuotaReserved", "status": "False", "reason": "Pending", "message": "untolerated GPU taint"}]},
        }
        self.event = {
            "metadata": {"name": "test-event", "namespace": "default", "uid": "event-uid", "resourceVersion": "43"},
            "involvedObject": {"kind": "Workload", "name": "test-workload", "namespace": "default", "uid": "workload-uid"},
            "reason": "Pending", "message": "could not assign launcher flavor", "type": "Warning", "count": 1,
        }
        self.backend.client.ApiextensionsV1Api().read_custom_resource_definition.return_value = crd("workloads.kueue.x-k8s.io")
        self.backend.custom.list_namespaced_custom_object.return_value = {"items": [self.workload]}
        self.backend.core.list_namespaced_event.side_effect = lambda namespace, **kwargs: {
            "items": [self.event] if kwargs["field_selector"] == "involvedObject.uid=workload-uid" else [],
        }

    def collect(self) -> SchedulingEvidence:
        return self.collector.collect(self.run, self.case, "job-uid", (("worker", "pod-uid"),))

    def test_uid_owned_workload_conditions_and_events_are_exportable(self) -> None:
        value = self.collect()
        self.assertEqual(self.workload["status"], value.workloads[0]["status"])
        self.assertEqual("could not assign launcher flavor", value.events[0]["message"])
        self.assertNotIn("spec", value.workloads[0])
        self.assertNotIn("annotations", value.workloads[0]["metadata"])
        self.assertEqual(64, len(value.signature()))
        self.assertEqual(f"{JOB_UID_LABEL}=job-uid", self.backend.custom.list_namespaced_custom_object.call_args.kwargs["label_selector"])
        self.assertEqual("v1beta2", self.backend.custom.list_namespaced_custom_object.call_args.args[1])
        self.assertEqual(3, self.backend.core.list_namespaced_event.call_count)
        self.assertIn("scheduling", {"scheduling": asdict(value)})
        for call in self.backend.custom.mock_calls + self.backend.core.mock_calls:
            self.assertTrue(str(call).startswith("call.list_"))

    def test_direct_execution_collects_events_without_querying_kueue(self) -> None:
        self.run = replace(self.run, plan=replace(self.run.plan, request=replace(self.run.plan.request, queue=None)))
        self.assertEqual((), self.collect().workloads)
        self.backend.custom.list_namespaced_custom_object.assert_not_called()
        self.backend.client.ApiextensionsV1Api().read_custom_resource_definition.assert_not_called()

    def test_older_served_api_is_supported_and_absence_is_not_an_api_failure(self) -> None:
        definition = crd("workloads.kueue.x-k8s.io")
        definition["spec"]["versions"] = [{"name": "v1beta1", "served": True}]
        self.backend.client.ApiextensionsV1Api().read_custom_resource_definition.return_value = definition
        self.backend.custom.list_namespaced_custom_object.return_value = {"items": []}
        self.assertEqual((), self.collect().workloads)
        self.assertEqual("v1beta1", self.backend.custom.list_namespaced_custom_object.call_args.args[1])

    def test_wrong_job_uid_queue_controller_namespace_or_duplicate_is_refused(self) -> None:
        original = copy.deepcopy(self.workload)
        for change in ("job_uid", "queue", "controller", "namespace", "duplicate", "resource_version", "owner_name"):
            item = copy.deepcopy(original)
            if change == "job_uid":
                item["metadata"]["ownerReferences"][0]["uid"] = "other"
            elif change == "queue":
                item["spec"]["queueName"] = "other"
            elif change == "controller":
                item["metadata"]["ownerReferences"][0]["controller"] = False
            elif change == "namespace":
                item["metadata"]["namespace"] = "other"
            elif change == "resource_version":
                item["metadata"].pop("resourceVersion")
            elif change == "owner_name":
                item["metadata"]["ownerReferences"][0]["name"] = "other"
            self.backend.custom.list_namespaced_custom_object.return_value = {"items": [item] * (2 if change == "duplicate" else 1)}
            with self.subTest(change=change), self.assertRaisesRegex(BenchmarkError, "ownership"):
                self.collect()

    def test_events_for_replaced_or_foreign_objects_are_refused(self) -> None:
        for key in ("uid", "name", "namespace", "kind"):
            event = copy.deepcopy(self.event)
            event["involvedObject"][key] = "other"
            self.backend.core.list_namespaced_event.side_effect = None
            self.backend.core.list_namespaced_event.return_value = {"items": [event]}
            with self.subTest(key=key), self.assertRaisesRegex(BenchmarkError, "verified workload object UID"):
                self.collect()

    def test_rbac_and_transport_errors_keep_workloads(self) -> None:
        for error in (ApiException(status=403), OSError("connection failed")):
            self.backend.custom.list_namespaced_custom_object.side_effect = error
            with self.assertRaisesRegex(BenchmarkError, "retain workloads"):
                self.collect()
        self.backend.custom.list_namespaced_custom_object.side_effect = None
        self.backend.core.list_namespaced_event.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "retain workloads"):
            self.collect()
        self.backend.custom.delete_namespaced_custom_object.assert_not_called()

    def test_pagination_item_and_byte_limits_fail_closed(self) -> None:
        for response in ({"items": [], "metadata": {"continue": "next"}}, {"items": [self.workload] * 17},
                         {"items": "not-a-list"}, {"items": [None]}):
            self.backend.custom.list_namespaced_custom_object.return_value = response
            with self.subTest(response=response), self.assertRaisesRegex(BenchmarkError, "collection limits"):
                self.collect()
        self.backend.custom.list_namespaced_custom_object.return_value = {"items": [self.workload]}
        with patch("oke_hpc_mgmt.benchmarks.diagnostics.MAX_DIAGNOSTIC_BYTES", 1):
            with self.assertRaisesRegex(BenchmarkError, "export budget"):
                self.collect()
        self.backend.core.list_namespaced_event.side_effect = None
        self.backend.core.list_namespaced_event.return_value = {"items": [], "metadata": {"continue": "next"}}
        with self.assertRaisesRegex(BenchmarkError, "collection limits"):
            self.collect()

    def test_signature_ignores_repeat_counts_but_not_reasons_or_workload_identity(self) -> None:
        first = self.collect()
        self.event["count"] = 10
        self.event["lastTimestamp"] = "2026-01-01T00:00:00Z"
        self.workload["metadata"]["resourceVersion"] = "99"
        self.assertEqual(first.signature(), self.collect().signature())
        self.event["message"] = "different blocker"
        self.assertNotEqual(first.signature(), self.collect().signature())
        self.event["message"] = "could not assign launcher flavor"
        self.workload["status"]["conditions"][0]["reason"] = "Inadmissible"
        self.assertNotEqual(first.signature(), self.collect().signature())
        self.workload["metadata"]["uid"] = "new-workload-uid"
        self.event["involvedObject"]["uid"] = "new-workload-uid"
        self.assertNotEqual(first.signature(), self.collect().signature())

    def test_unrecorded_job_is_not_accepted(self) -> None:
        with self.assertRaisesRegex(BenchmarkError, "recorded MPIJob UID"):
            self.collector.collect(self.run, self.case, "foreign-job", ())
