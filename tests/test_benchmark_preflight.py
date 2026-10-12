from __future__ import annotations

import copy
import unittest
from typing import Any

from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks
from oke_hpc_mgmt.benchmarks.preflight import (
    GPU_RESOURCE, VF_RESOURCE, device_count, pod_devices, resource_occupants, validate_runtime_nodes,
)
from test_benchmark_planning import inventory, request


def raw_inventory() -> list[dict]:
    return [{
        "metadata": {"name": node.k8s_name, "uid": node.kubernetes_uid, "resourceVersion": "1",
                     "labels": {**node.labels, "kubernetes.io/hostname": node.k8s_name,
                                "node.kubernetes.io/instance-type": node.shape}},
        "spec": {},
        "status": {"allocatable": node.allocatable, "conditions": [
            {"type": name, "status": "True" if name == "Ready" else "False"}
            for name in ("Ready", "DiskPressure", "MemoryPressure", "PIDPressure")
        ]},
    } for node in inventory(2).nodes]


def gpu_pod() -> dict:
    return {"metadata": {"name": "training", "namespace": "jobs", "uid": "pod-uid"},
            "spec": {"nodeName": "gpu-01", "containers": [{"resources": {"limits": {GPU_RESOURCE: 8}}}]},
            "status": {"phase": "Running"}}


class BenchmarkPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = plan_healthchecks(inventory(2), request()).cases[0]

    def test_quantities_use_kubernetes_parser_and_require_integral_devices(self) -> None:
        for value, expected in ((8, 8), ("8", 8), ("8e0", 8), ("8k", 8000), ("1000m", 1), (0, 0)):
            self.assertEqual(expected, device_count(value))
        invalid: Any
        for invalid in (True, None, [], "500m", -1, "-8", "NaN", "Inf", "eight", float("inf")):
            with self.subTest(value=invalid), self.assertRaises(BenchmarkError):
                device_count(invalid)

    def test_init_and_restartable_sidecar_resource_accounting(self) -> None:
        pod = gpu_pod()
        self.assertEqual(8, pod_devices(pod, GPU_RESOURCE))
        pod["spec"]["containers"] = [{"resources": {"requests": {GPU_RESOURCE: "2"}}}]
        pod["spec"]["initContainers"] = [
            {"restartPolicy": "Always", "resources": {"limits": {GPU_RESOURCE: "1"}}},
            {"resources": {"limits": {GPU_RESOURCE: "4"}}},
            {"restartPolicy": "Always", "resources": {"limits": {GPU_RESOURCE: "2"}}},
        ]
        self.assertEqual(5, pod_devices(pod, GPU_RESOURCE))
        pod["spec"]["overhead"] = {GPU_RESOURCE: "1"}
        self.assertEqual(6, pod_devices(pod, GPU_RESOURCE))
        pod["spec"]["initContainers"].append({"resources": {"limits": {GPU_RESOURCE: "8"}}})
        self.assertEqual(12, pod_devices(pod, GPU_RESOURCE))
        self.assertEqual(0, pod_devices(pod, VF_RESOURCE))

    def test_terminating_and_unknown_pods_still_hold_resources(self) -> None:
        for phase in ("Running", "Pending", "Unknown", None):
            pod = gpu_pod()
            pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
            pod["status"]["phase"] = phase
            occupant = resource_occupants([pod])[0]
            self.assertTrue(occupant.terminating)
            self.assertEqual(8, occupant.gpus)
            self.assertEqual("pod-uid", occupant.uid)
        for phase in ("Succeeded", "Failed"):
            pod = gpu_pod()
            pod["status"]["phase"] = phase
            self.assertFalse(resource_occupants([pod]))
        pod = gpu_pod()
        del pod["spec"]["nodeName"]
        self.assertFalse(resource_occupants([pod]))

    def test_readonly_preflight_records_current_resource_versions(self) -> None:
        nodes = raw_inventory()
        before = copy.deepcopy(nodes)
        evidence = validate_runtime_nodes(self.case, nodes, [])
        self.assertEqual((("gpu-01", "uid-1"), ("gpu-02", "uid-2")), evidence.nodes)
        self.assertEqual((("gpu-01", "1"), ("gpu-02", "1")), evidence.resource_versions)
        self.assertIn("cannot prove", evidence.warnings[0])
        self.assertEqual(before, nodes)

    def test_busy_gpus_and_vfs_block_without_excluding_daemonsets(self) -> None:
        for resource in (GPU_RESOURCE, VF_RESOURCE):
            pod = gpu_pod()
            pod["metadata"]["ownerReferences"] = [{"kind": "DaemonSet", "name": "owned-agent"}]
            pod["spec"]["containers"][0]["resources"]["limits"] = {resource: "1"}
            with self.subTest(resource=resource), self.assertRaisesRegex(BenchmarkError, "already allocated"):
                validate_runtime_nodes(self.case, raw_inventory(), [pod])

    def test_name_reuse_and_drift_block(self) -> None:
        for section, change in (
            ("metadata", {"uid": "replacement"}), ("metadata", {"resourceVersion": None}),
            ("metadata", {"deletionTimestamp": "2026-01-01T00:00:00Z"}),
            ("spec", {"unschedulable": True}),
            ("spec", {"taints": [{"key": "maintenance", "effect": "NoSchedule"}]}),
            ("spec", {"taints": [{"key": GPU_RESOURCE, "effect": "NoSchedule"}]}),
            ("status", {"allocatable": {GPU_RESOURCE: "7"}}),
        ):
            nodes = raw_inventory()
            nodes[0][section].update(change)
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                validate_runtime_nodes(self.case, nodes, [])
        with self.assertRaisesRegex(BenchmarkError, "no longer exists"):
            validate_runtime_nodes(self.case, raw_inventory()[1:], [])

    def test_missing_or_unknown_pressure_conditions_fail_closed(self) -> None:
        for pressure in ("Ready", "DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"):
            for value in ("Unknown", "True" if pressure != "Ready" else "False"):
                nodes = raw_inventory()
                nodes[0]["status"]["conditions"] = [condition for condition in nodes[0]["status"]["conditions"]
                                                      if condition["type"] != pressure]
                nodes[0]["status"]["conditions"].append({"type": pressure, "status": value})
                with self.subTest(pressure=pressure, value=value), self.assertRaises(BenchmarkError):
                    validate_runtime_nodes(self.case, nodes, [])
        nodes = raw_inventory()
        nodes[0]["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
        with self.assertRaisesRegex(BenchmarkError, "not verified"):
            validate_runtime_nodes(self.case, nodes, [])

    def test_hostname_shape_topology_and_sharing_checks(self) -> None:
        for change in ({"kubernetes.io/hostname": "gpu-02"}, {"kubernetes.io/hostname": ""},
                       {"node.kubernetes.io/instance-type": "other"}, {"nvidia.com/gpu.replicas": "2"},
                       {"nvidia.com/gpu.sharing-strategy": "mps"}, {"nvidia.com/mig.strategy": "mixed"}):
            nodes = raw_inventory()
            nodes[0]["metadata"]["labels"].update(change)
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                validate_runtime_nodes(self.case, nodes, [])
        nodes = raw_inventory()
        nodes[0]["metadata"]["labels"] = {key: value for key, value in nodes[0]["metadata"]["labels"].items()
                                            if key in {"kubernetes.io/hostname", "node.kubernetes.io/instance-type"}}
        with self.assertRaisesRegex(BenchmarkError, "topology changed"):
            validate_runtime_nodes(self.case, nodes, [])

    def test_host_port_conflict_blocks_even_non_gpu_pod(self) -> None:
        for host_network in (True, False):
            pod = gpu_pod()
            pod["spec"]["hostNetwork"] = host_network
            pod["spec"]["containers"] = [{"ports": [
                {"containerPort": 2222} if host_network else {"containerPort": 22, "hostPort": 2222},
            ]}]
            with self.assertRaisesRegex(BenchmarkError, "port 2222"):
                validate_runtime_nodes(self.case, raw_inventory(), [pod])
