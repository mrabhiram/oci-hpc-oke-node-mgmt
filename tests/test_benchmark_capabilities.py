from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ProtocolError

from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.capabilities import (
    CapabilityPreflight, KUEUE_GROUP, label_selector_matches, required_permissions,
    served_version, topology_placement, validate_driver_hint, validate_nad,
)
from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions, TopologyPlacement, render_mpijob
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks
from oke_hpc_mgmt.benchmarks.profiles import execution_fingerprint
from oke_hpc_mgmt.benchmarks.records import RunRecord, decode_record
from test_benchmark_planning import inventory, request


HOSTNAME = "kubernetes.io/hostname"
LOCAL_BLOCK = "oci.oraclecloud.com/rdma.local_block_id"
RUN = "hc-0123456789abcdefabcd"


def metadata(name: str, **changes: Any) -> dict[str, Any]:
    return {"name": name, "uid": f"uid-{name}", "resourceVersion": "1", "generation": 1, **changes}


def crd(name: str) -> dict[str, Any]:
    namespaced = name.split(".")[0] in {"mpijobs", "localqueues", "workloads", "network-attachment-definitions"}
    version = "v2beta1" if name.startswith("mpijobs.") else (
        "v1" if name.startswith("network-attachment-definitions.") else "v1beta2"
    )
    return {"metadata": metadata(name), "spec": {"scope": "Namespaced" if namespaced else "Cluster",
            "versions": [{"name": version, "served": True}]},
            "status": {"conditions": [{"type": "Established", "status": "True"}]}}


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        kubernetes = Mock()
        self.kubernetes = kubernetes
        kubernetes.client.ApiClient.return_value = ApiClient()
        self.backend = BenchmarkBackend(kubernetes)
        self.checker = CapabilityPreflight(self.backend)
        self.options = ExecutionOptions()
        self.plan = plan_healthchecks(inventory(2), request(mode="all-nodes"))
        self.nodes = {node.name: {"metadata": metadata(node.name, uid=node.uid, labels={
            HOSTNAME: node.name, "nvidia.com/cuda.driver.major": "590", "nvidia.com/gpu": "true",
            "oci.oraclecloud.com/rdma.hpc_island_id": "island-a",
            "oci.oraclecloud.com/rdma.network_block_id": "network-a", LOCAL_BLOCK: "block-a",
        })} for node in self.plan.cases[0].nodes}
        self.namespace = {"metadata": metadata("default", labels={}), "status": {"phase": "Active"}}
        self.backend.core.read_namespace.return_value = self.namespace
        self.backend.core.list_node.return_value = {"items": list(self.nodes.values())}
        self.checker.authorization.create_self_subject_access_review.return_value = {"status": {"allowed": True}}
        self.installed_kueue = False
        self.checker.extensions.read_custom_resource_definition.side_effect = self.read_crd
        self.controller = {
            "metadata": metadata("mpi-operator", namespace="mpi-system"),
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "mpi-operator", "image": "example.invalid/mpi-operator:v0.8.0"},
            ]}}}, "status": {"observedGeneration": 1, "availableReplicas": 1},
        }
        self.checker.apps.list_deployment_for_all_namespaces.return_value = {"items": [self.controller]}
        active = {"conditions": [{"type": "Active", "status": "True", "observedGeneration": 1}]}
        self.local = {"metadata": metadata("gpu", namespace="default"),
                      "spec": {"clusterQueue": "gpu-cq"}, "status": copy.deepcopy(active)}
        self.cluster = {"metadata": metadata("gpu-cq"), "spec": {"namespaceSelector": {}, "resourceGroups": [{
            "coveredResources": ["cpu", "memory", "nvidia.com/gpu"],
            "flavors": [{"name": "gpu-flavor", "resources": [{"name": "nvidia.com/gpu", "nominalQuota": 16}]}],
        }]}, "status": copy.deepcopy(active)}
        self.flavor = {"metadata": metadata("gpu-flavor"), "spec": {"nodeLabels": {"nvidia.com/gpu": "true"}}}
        self.topology = {"metadata": metadata("oci-rdma"), "spec": {"levels": [
            {"nodeLabel": key} for key in ("oci.oraclecloud.com/rdma.hpc_island_id",
                                           "oci.oraclecloud.com/rdma.network_block_id", LOCAL_BLOCK, HOSTNAME)
        ]}}
        self.nad = {"metadata": metadata("rdma-vf", namespace="default", annotations={
            "k8s.v1.cni.cncf.io/resourceName": "nvidia.com/rdma-vf",
        }), "spec": {"config": json.dumps({"type": "sriov"})}}
        self.backend.custom.get_namespaced_custom_object.side_effect = self.namespaced
        self.backend.custom.get_cluster_custom_object.side_effect = self.cluster_object
        self.backend.custom.create_namespaced_custom_object.side_effect = self.admit

    def read_crd(self, name: str, **_: Any) -> dict[str, Any]:
        if name == f"localqueues.{KUEUE_GROUP}" and not self.installed_kueue:
            raise ApiException(status=404)
        return crd(name)

    def namespaced(self, group: str, version: str, namespace: str, plural: str, name: str, **_: Any) -> dict[str, Any]:
        self.assertEqual("default", namespace)
        return self.local if plural == "localqueues" else self.nad

    def cluster_object(self, group: str, version: str, plural: str, name: str, **_: Any) -> dict[str, Any]:
        return {"clusterqueues": self.cluster, "resourceflavors": self.flavor, "topologies": self.topology}[plural]

    def admit(self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        admitted = copy.deepcopy(body)
        if self.installed_kueue:
            admitted["spec"]["runPolicy"]["suspend"] = True
        return admitted

    def queue(self, *, topology: bool = False) -> None:
        self.installed_kueue = True
        self.plan = replace(self.plan, request=replace(self.plan.request, queue="gpu"))
        if topology:
            self.flavor["spec"]["topologyName"] = "oci-rdma"

    def check(self, **kwargs: Any):
        return self.checker.check(self.plan, self.options, **kwargs)

    def test_direct_uses_only_reads_self_reviews_and_dry_run_creates(self) -> None:
        result = self.check()
        self.assertEqual("direct", result.scheduler)
        self.assertEqual("0.8.0", result.mpi_operator_version)
        self.assertIsNone(result.kueue_version)
        self.assertEqual("uid-default", result.namespace_uid)
        self.assertEqual(2, self.backend.core.create_namespaced_pod.call_count)
        self.assertTrue(result.resource_versions)
        for client in (self.backend.core, self.backend.custom):
            for call in client.mock_calls:
                if call[0].startswith("create_"):
                    self.assertEqual("All", call.kwargs["dry_run"])
                else:
                    self.assertTrue(call[0].startswith(("read_", "get_", "list_")), call)
        self.kubernetes.mutation_lease.assert_not_called()

    def test_kueue_requires_explicit_queue_and_never_falls_back_on_api_denial(self) -> None:
        self.installed_kueue = True
        with self.assertRaisesRegex(BenchmarkError, "select an existing --queue"):
            self.check()
        self.checker.extensions.read_custom_resource_definition.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "HTTP 403"):
            self.check()
        self.backend.custom.create_namespaced_custom_object.assert_not_called()

    def test_requested_missing_kueue_refuses(self) -> None:
        self.plan = replace(self.plan, request=replace(self.plan.request, queue="gpu"))
        with self.assertRaisesRegex(BenchmarkError, "not installed"):
            self.check()

    def test_normal_queue_has_no_topology_and_dry_run_proves_webhook_interception(self) -> None:
        self.queue()
        result = self.check()
        self.assertEqual("kueue", result.scheduler)
        self.assertEqual("gpu-cq", result.cluster_queue)
        self.assertEqual("v1beta2", result.kueue_version)
        self.assertEqual((("case-0001", ("gpu-flavor",)),), result.compatible_flavors)
        self.assertEqual((), result.execution_options.placements)
        body = self.backend.custom.create_namespaced_custom_object.call_args.args[4]
        self.assertFalse(body["spec"]["runPolicy"]["suspend"])
        actual = render_mpijob(RUN, self.plan.cases[0], self.plan.request, result.execution_options)
        self.assertTrue(actual["spec"]["runPolicy"]["suspend"])
        self.backend.custom.create_namespaced_custom_object.side_effect = lambda *args, **kwargs: args[4]
        with self.assertRaisesRegex(BenchmarkError, "did not suspend"):
            self.check()

    def test_kueue_patch_permission_is_required_for_reliable_cancellation(self) -> None:
        self.queue()
        def authorize(body, **kwargs):
            attributes = body["spec"]["resourceAttributes"]
            return {"status": {"allowed": not (attributes["resource"] == "workloads" and attributes["verb"] == "patch")}}
        self.checker.authorization.create_self_subject_access_review.side_effect = authorize
        with self.assertRaisesRegex(BenchmarkError, "workloads"):
            self.check()
        self.backend.custom.create_namespaced_custom_object.assert_not_called()

    def test_topology_queue_resolves_exact_affinity_and_persists_typed_options(self) -> None:
        self.queue(topology=True)
        result = self.check()
        placement = result.execution_options.placements[0]
        self.assertEqual(LOCAL_BLOCK, placement.key)
        self.assertFalse(placement.required)
        self.assertEqual(("gpu-01", "gpu-02"), placement.hostnames)
        record = RunRecord.create(RUN, self.plan, result.execution_options)
        self.assertEqual(record, decode_record(record.payload(), uid="uid", resource_version="1"))
        job = render_mpijob(RUN, self.plan.cases[0], self.plan.request, result.execution_options)
        worker = job["spec"]["mpiReplicaSpecs"]["Worker"]["template"]
        self.assertEqual(LOCAL_BLOCK, worker["metadata"]["annotations"]["kueue.x-k8s.io/podset-preferred-topology"])
        terms = worker["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual([["gpu-01"], ["gpu-02"]], [term["matchExpressions"][0]["values"] for term in terms])
        self.assertEqual([["gpu-01"], ["gpu-02"]], [term["matchFields"][0]["values"] for term in terms])
        self.assertIn("Topology/oci-rdma", [item[0] for item in result.resource_versions])

    def test_per_block_is_required_and_checks_parent_domains_not_just_local_id(self) -> None:
        self.queue(topology=True)
        self.plan = replace(self.plan, request=replace(self.plan.request, mode="per-local-block"))
        self.assertTrue(self.check().execution_options.placements[0].required)
        self.nodes["gpu-02"]["metadata"]["labels"]["oci.oraclecloud.com/rdma.hpc_island_id"] = "another-island"
        with self.assertRaisesRegex(BenchmarkError, "complete topology domain"):
            self.check()

    def test_topology_requires_complete_labels_and_unique_hostname_leaf(self) -> None:
        self.queue(topology=True)
        for modification in ("missing", "duplicate", "leaf", "levels"):
            nodes, topology = copy.deepcopy(self.nodes), copy.deepcopy(self.topology)
            if modification == "missing":
                del nodes["gpu-01"]["metadata"]["labels"][LOCAL_BLOCK]
            elif modification == "duplicate":
                nodes["gpu-02"]["metadata"]["labels"][HOSTNAME] = "gpu-01"
            elif modification == "leaf":
                topology["spec"]["levels"].pop()
            else:
                topology["spec"]["levels"].append({"nodeLabel": HOSTNAME})
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                topology_placement(self.plan.cases[0], topology, nodes, per_local_block=False)
        self.topology["spec"]["levels"] = [{"nodeLabel": HOSTNAME}]
        self.assertEqual(HOSTNAME, self.check().execution_options.placements[0].key)
        with self.assertRaisesRegex(BenchmarkError, "containing the local block"):
            topology_placement(self.plan.cases[0], self.topology, self.nodes, per_local_block=True)

    def test_queue_holds_stale_readiness_namespace_denial_and_missing_flavor_block(self) -> None:
        self.queue()
        for modification in ("local-hold", "cq-hold", "inactive", "stale", "namespace", "flavor", "quota-resource"):
            local, cluster, flavor = copy.deepcopy(self.local), copy.deepcopy(self.cluster), copy.deepcopy(self.flavor)
            if modification == "local-hold":
                self.local["spec"]["stopPolicy"] = "HoldAndDrain"
            elif modification == "cq-hold":
                self.cluster["spec"]["stopPolicy"] = "Hold"
            elif modification == "inactive":
                self.local["status"]["conditions"][0]["status"] = "False"
            elif modification == "stale":
                self.cluster["metadata"]["generation"] = 2
            elif modification == "namespace":
                self.cluster["spec"]["namespaceSelector"] = None
            elif modification == "flavor":
                self.flavor["spec"]["nodeLabels"]["shape"] = "not-selected"
            else:
                self.cluster["spec"]["resourceGroups"][0]["flavors"][0]["resources"] = []
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                self.check()
            self.local, self.cluster, self.flavor = local, cluster, flavor
        self.backend.custom.create_namespaced_custom_object.assert_not_called()

    def test_vf_can_use_separate_resource_groups_and_keeps_topology_annotation(self) -> None:
        self.queue(topology=True)
        self.plan = replace(self.plan, cases=(replace(self.plan.cases[0], network="vf", profile_fingerprint=
                            execution_fingerprint(self.plan.cases[0].nodes[0].shape, "vf", self.plan.request)),))
        self.cluster["spec"]["resourceGroups"].append({"coveredResources": ["nvidia.com/rdma-vf"], "flavors": [
            {"name": "gpu-flavor", "resources": [{"name": "nvidia.com/rdma-vf", "nominalQuota": 64}]},
        ]})
        result = self.check()
        body = render_mpijob(RUN, self.plan.cases[0], self.plan.request, result.execution_options)
        annotations = body["spec"]["mpiReplicaSpecs"]["Worker"]["template"]["metadata"]["annotations"]
        self.assertIn("kueue.x-k8s.io/podset-preferred-topology", annotations)
        self.assertIn("k8s.v1.cni.cncf.io/networks", annotations)
        self.assertEqual(1, len([item for item in result.resource_versions if item[0] == "ResourceFlavor/gpu-flavor"]))
        self.cluster["spec"]["resourceGroups"].pop()
        with self.assertRaisesRegex(BenchmarkError, "GPU/RDMA flavor"):
            self.check()

    def test_mixed_compatible_topologies_are_not_guessed(self) -> None:
        self.queue(topology=True)
        self.cluster["spec"]["resourceGroups"][0]["flavors"].append({
            "name": "other", "resources": [{"name": "nvidia.com/gpu"}],
        })
        other = {"metadata": metadata("other"), "spec": {"nodeLabels": {}, "topologyName": "another-topology"}}
        self.backend.custom.get_cluster_custom_object.side_effect = lambda *args, **kwargs: (
            other if args[3] == "other" else self.cluster_object(*args, **kwargs)
        )
        with self.assertRaisesRegex(BenchmarkError, "incompatible topologies"):
            self.check()

    def test_upstream_launcher_flavor_is_not_mistaken_for_worker_topology(self) -> None:
        self.queue(topology=True)
        self.cluster["spec"]["resourceGroups"][0]["flavors"].append({
            "name": "launcher", "resources": [{"name": "nvidia.com/gpu", "nominalQuota": 0}],
        })
        launcher = {"metadata": metadata("launcher"), "spec": {}}
        self.backend.custom.get_cluster_custom_object.side_effect = lambda *args, **kwargs: (
            launcher if args[3] == "launcher" else self.cluster_object(*args, **kwargs)
        )
        result = self.check()
        self.assertEqual((("case-0001", ("gpu-flavor",)),), result.compatible_flavors)
        self.assertEqual(1, len(result.execution_options.placements))

    def test_namespace_terminating_restricted_or_identity_missing_refuses(self) -> None:
        original = copy.deepcopy(self.namespace)
        for modification in ("uid", "terminating", "deleting", "baseline", "restricted"):
            self.namespace.clear()
            self.namespace.update(copy.deepcopy(original))
            if modification == "uid":
                self.namespace["metadata"].pop("uid")
            elif modification == "terminating":
                self.namespace["status"]["phase"] = "Terminating"
            elif modification == "deleting":
                self.namespace["metadata"]["deletionTimestamp"] = "timestamp"
            else:
                self.namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = modification
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                self.check()
        self.backend.custom.create_namespaced_custom_object.assert_not_called()

    def test_permissions_fail_closed_on_denied_missing_and_evaluation_errors(self) -> None:
        for status in ({}, {"allowed": False}, {"allowed": True, "denied": True},
                       {"allowed": True, "evaluationError": "unavailable"}):
            self.checker.authorization.create_self_subject_access_review.return_value = {"status": status}
            with self.subTest(status=status), self.assertRaisesRegex(BenchmarkError, "denied or unverified"):
                self.check()
        verbs = {(item.resource, item.subresource, item.verb) for item in required_permissions("default")}
        self.assertNotIn(("pods", "eviction", "create"), verbs)
        self.assertNotIn(("nodes", "", "patch"), verbs)
        self.assertIn(("pods", "log", "get"), verbs)
        self.assertIn(("events", "", "list"), verbs)

    def test_kueue_workload_diagnostics_permission_is_required_only_for_queued_runs(self) -> None:
        self.checker.authorization.create_self_subject_access_review.side_effect = lambda body, **_: {
            "status": {"allowed": body["spec"]["resourceAttributes"]["resource"] != "workloads"},
        }
        self.check()
        self.queue()
        with self.assertRaisesRegex(BenchmarkError, "list workloads"):
            self.check()

    def test_operator_version_availability_and_ambiguity_are_verified(self) -> None:
        pristine = copy.deepcopy(self.controller)
        for modification in ("old", "untagged", "unavailable", "stale", "missing", "multiple", "paginated"):
            self.controller = copy.deepcopy(pristine)
            response: dict[str, Any] = {"items": [self.controller]}
            if modification in {"old", "untagged"}:
                self.controller["spec"]["template"]["spec"]["containers"][0]["image"] = (
                    "example.invalid/mpi:0.7.0" if modification == "old" else "example.invalid/mpi:latest"
                )
            elif modification == "unavailable":
                self.controller["status"]["availableReplicas"] = 0
            elif modification == "stale":
                self.controller["status"]["observedGeneration"] = 0
            elif modification == "missing":
                response["items"] = []
            elif modification == "multiple":
                response["items"].append(copy.deepcopy(self.controller))
            else:
                response["metadata"] = {"continue": "token"}
            self.checker.apps.list_deployment_for_all_namespaces.return_value = response
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                self.check()

    def test_missing_replaced_or_paginated_nodes_block(self) -> None:
        original = copy.deepcopy(self.backend.core.list_node.return_value)
        response: dict[str, Any]
        for response in ({"items": []}, {**original, "metadata": {"continue": "token"}},
                         {"items": [{"metadata": metadata("gpu-01", uid="new")}, self.nodes["gpu-02"]]}):
            self.backend.core.list_node.return_value = response
            with self.subTest(response=response), self.assertRaises(BenchmarkError):
                self.check()

    def test_no_probe_is_explicit_and_transport_failures_are_unverified(self) -> None:
        result = self.check(probe_admission=False)
        self.assertIn("submission is not validated", " ".join(result.warnings))
        self.backend.custom.create_namespaced_custom_object.assert_not_called()
        for error in (ApiException(status=500), ProtocolError("network failure"), TimeoutError("timeout")):
            self.backend.core.read_namespace.side_effect = error
            with self.subTest(error=error), self.assertRaises(BenchmarkError):
                self.check()

    def test_dry_run_rejection_propagates_without_any_persisted_create(self) -> None:
        self.backend.core.create_namespaced_pod.side_effect = ApiException(status=403)
        with self.assertRaisesRegex(BenchmarkError, "HTTP 403"):
            self.check()
        self.assertEqual("All", self.backend.core.create_namespaced_pod.call_args.kwargs["dry_run"])

    def test_driver_labels_are_hints_and_old_driver_is_a_block(self) -> None:
        case = self.plan.cases[0]
        image = "example.invalid/nccl:cuda-13.4.1-ubuntu"
        self.assertIn("do not prove", " ".join(validate_driver_hint(case, self.nodes, image)))
        self.nodes["gpu-01"]["metadata"]["labels"]["nvidia.com/cuda.driver.major"] = "575"
        with self.assertRaisesRegex(BenchmarkError, "below CUDA 13"):
            validate_driver_hint(case, self.nodes, image)
        self.nodes["gpu-01"]["metadata"]["labels"].pop("nvidia.com/cuda.driver.major")
        self.assertIn("not reported", " ".join(validate_driver_hint(case, self.nodes, image)))
        self.assertIn("unverified", " ".join(validate_driver_hint(case, self.nodes, "custom:latest")))

    def test_nad_resource_namespace_cni_json_and_sriov_are_required(self) -> None:
        validate_nad(self.nad, "default", "rdma-vf")
        for modification in ("namespace", "resource", "json", "list", "plugin"):
            nad = copy.deepcopy(self.nad)
            if modification == "namespace":
                nad["metadata"]["namespace"] = "other"
            elif modification == "resource":
                nad["metadata"]["annotations"] = {}
            else:
                nad["spec"]["config"] = {"json": "invalid", "list": "[]", "plugin": '{"type":"bridge"}'}[modification]
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                validate_nad(nad, "default", "rdma-vf")


class CapabilityParserTests(unittest.TestCase):
    def test_selectors_match_kubernetes_semantics(self) -> None:
        self.assertFalse(label_selector_matches(None, {}))
        self.assertTrue(label_selector_matches({}, {}))
        self.assertFalse(label_selector_matches({"matchLabels": {"team": "gpu"}}, {"team": "cpu"}))
        for operator, values, present, absent in (("In", ["gpu"], True, False), ("NotIn", ["gpu"], False, True),
                                                  ("Exists", [], True, False), ("DoesNotExist", [], False, True)):
            selector = {"matchExpressions": [{"key": "team", "operator": operator, "values": values}]}
            self.assertEqual(present, label_selector_matches(selector, {"team": "gpu"}))
            self.assertEqual(absent, label_selector_matches(selector, {}))
        for expression in ({"key": "team", "operator": "Gt", "values": ["1"]},
                           {"key": "team", "operator": "In"}, {"key": "team", "operator": "Exists", "values": ["x"]},
                           {"operator": "Exists"}):
            with self.subTest(expression=expression), self.assertRaises(BenchmarkError):
                label_selector_matches({"matchExpressions": [expression]}, {})
        with self.assertRaises(BenchmarkError):
            label_selector_matches({"unsupported": True}, {})

    def test_crd_scope_version_and_establishment(self) -> None:
        value = crd("mpijobs.kubeflow.org")
        self.assertEqual("v2beta1", served_version(value, ("v2beta1",), namespaced=True))
        for modification in ("scope", "served", "established", "deleting"):
            changed = copy.deepcopy(value)
            if modification == "scope":
                changed["spec"]["scope"] = "Cluster"
            elif modification == "served":
                changed["spec"]["versions"][0]["served"] = False
            elif modification == "established":
                changed["status"]["conditions"] = []
            else:
                changed["metadata"]["deletionTimestamp"] = "timestamp"
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                served_version(changed, ("v2beta1",), namespaced=True)

    def test_placement_validation_and_old_checkpoint_compatibility(self) -> None:
        placement = TopologyPlacement("case-0001", LOCAL_BLOCK, False, ("gpu-01", "gpu-02"))
        placement.validate()
        for changes in ({"case_id": "other"}, {"key": "arbitrary"}, {"required": "true"},
                        {"hostnames": ()}, {"hostnames": ("gpu-01", "gpu-01")}, {"hostnames": ("bad/value",)}):
            with self.subTest(changes=changes), self.assertRaises(BenchmarkError):
                replace(placement, **changes).validate()  # type: ignore[arg-type]
        with self.assertRaisesRegex(BenchmarkError, "duplicate case"):
            ExecutionOptions(placements=(placement, placement)).validate()
        plan = plan_healthchecks(inventory(2), request(mode="all-nodes"))
        options = ExecutionOptions(placements=(placement,))
        with self.assertRaisesRegex(BenchmarkError, "requires a queue"):
            render_mpijob(RUN, plan.cases[0], plan.request, options)
        record = RunRecord.create(RUN, plan, ExecutionOptions())
        payload = json.loads(record.payload())
        payload["options"].pop("placements")
        self.assertEqual(record, decode_record(json.dumps(payload), uid="uid", resource_version="1"))
