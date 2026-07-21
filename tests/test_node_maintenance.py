from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from oke_hpc_mgmt.models import DiscoverySnapshot, DrainPod, NodeInfo
from oke_hpc_mgmt.workflows.lifecycle import WorkflowError, WorkflowNotFound
from oke_hpc_mgmt.workflows.node_maintenance import (
    execute_node_maintenance,
    prepare_node_maintenance,
)


def _service(node: NodeInfo) -> Mock:
    service = Mock()
    service.options = SimpleNamespace(skip_kubernetes=False)
    service.discover.return_value = DiscoverySnapshot(nodes=[node])
    return service


class NodeMaintenanceTests(unittest.TestCase):
    def test_prepare_cordon_and_uncordon_create_kubernetes_plans(self):
        node = NodeInfo(k8s_name="cpu-1", pool_name="oke-cpu", ready=True)

        cordon = prepare_node_maintenance(
            _service(node), "cordon", identifiers=("cpu-1",)
        )
        uncordon = prepare_node_maintenance(
            _service(node), "uncordon", identifiers=("cpu-1",)
        )

        self.assertEqual("node-cordon", cordon.plans[0].operation)
        self.assertEqual("node-uncordon", uncordon.plans[0].operation)
        self.assertEqual("kubernetes", cordon.plans[0].owner)

    def test_prepare_drain_uses_eviction_inventory(self):
        node = NodeInfo(k8s_name="cpu-1", pool_name="oke-cpu", ready=True)
        service = _service(node)
        service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod("default", "job", controller="Job/job"),
            DrainPod("kube-system", "daemon", daemonset=True),
        ]

        prepared = prepare_node_maintenance(
            service,
            "drain",
            identifiers=("cpu-1",),
        )

        self.assertEqual(1, prepared.plans[0].workload_pods)
        self.assertEqual(2, len(prepared.drain_pods["cpu-1"]))

    def test_prepare_rejects_unknown_empty_and_slinky_drain(self):
        node = NodeInfo(k8s_name="cpu-1", ready=True)
        with self.assertRaises(WorkflowError):
            prepare_node_maintenance(_service(node), "reboot", identifiers=("cpu-1",))
        with self.assertRaises(WorkflowNotFound):
            prepare_node_maintenance(_service(node), "cordon", identifiers=("missing",))

        slinky = NodeInfo(k8s_name="slurm-1", slinky_workload_pods=1)
        with self.assertRaisesRegex(WorkflowError, "Slurm-aware drain"):
            prepare_node_maintenance(
                _service(slinky), "drain", identifiers=("slurm-1",)
            )

    def test_execute_cordon_uncordon_and_drain(self):
        node = NodeInfo(k8s_name="cpu-1", pool_name="oke-cpu", ready=True)

        cordon_service = _service(node)
        cordon = prepare_node_maintenance(
            cordon_service, "cordon", identifiers=("cpu-1",)
        )
        execute_node_maintenance(cordon_service, cordon, lock=False)
        cordon_service.kubernetes_backend.return_value.cordon_node.assert_called_once_with(
            "cpu-1"
        )

        uncordon_service = _service(node)
        uncordon = prepare_node_maintenance(
            uncordon_service, "uncordon", identifiers=("cpu-1",)
        )
        execute_node_maintenance(uncordon_service, uncordon, lock=False)
        uncordon_service.kubernetes_backend.return_value.uncordon_node.assert_called_once_with(
            "cpu-1"
        )

        drain_service = _service(node)
        drain_service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod("default", "job", controller="Job/job")
        ]
        drain = prepare_node_maintenance(
            drain_service, "drain", identifiers=("cpu-1",)
        )
        results = execute_node_maintenance(drain_service, drain, lock=False)
        backend = drain_service.kubernetes_backend.return_value
        backend.cordon_node.assert_called_once_with("cpu-1")
        backend.evict_drain_pods.assert_called_once()
        self.assertEqual(1, results[0]["pods"])


if __name__ == "__main__":
    unittest.main()
