from __future__ import annotations

import unittest

from oke_hpc_mgmt.commands.common import health_exit_code
from oke_hpc_mgmt.health import (
    actionable_recommendations,
    addon_validation_results,
    evaluate_health,
)
from oke_hpc_mgmt.models import (
    AddonInfo,
    DiscoverySnapshot,
    HealthResult,
    NodeInfo,
    WorkerPoolInfo,
)


def _rdma_labels() -> dict[str, str]:
    return {
        "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
        "oci.oraclecloud.com/rdma.network_block_id": "network-1",
        "oci.oraclecloud.com/rdma.local_block_id": "local-1",
    }


class HealthTests(unittest.TestCase):
    def healthy_snapshot(self) -> DiscoverySnapshot:
        return DiscoverySnapshot(
            pools=[
                WorkerPoolInfo(
                    name="oke-rdma",
                    kind="node-pool",
                    desired_size=1,
                    active_oci_instances=1,
                    ready_k8s_nodes=1,
                    gpu_resource="nvidia.com/gpu",
                    rdma_enabled=True,
                )
            ],
            nodes=[
                NodeInfo(
                    k8s_name="rdma-1",
                    pool_name="oke-rdma",
                    shape="BM.GPU4.8",
                    ready=True,
                    allocatable={"nvidia.com/gpu": "8"},
                    labels=_rdma_labels(),
                )
            ],
            addons=[
                AddonInfo("NodeFeatureDiscovery", "ACTIVE", "v1"),
                AddonInfo("NvidiaGpuOperator", "ACTIVE", "v1"),
            ],
        )

    def test_healthy_gpu_rdma_snapshot_passes_with_optional_network_operator_info(self):
        results = evaluate_health(self.healthy_snapshot())

        self.assertFalse(any(result.status in {"FAIL", "WARN"} for result in results))
        discovery = next(
            result for result in results if result.check == "discovery-completeness"
        )
        self.assertEqual("PASS", discovery.status)
        network = next(result for result in results if result.check == "addon-network-operator")
        self.assertEqual("INFO", network.status)

    def test_discovery_warning_degrades_health_and_is_actionable(self):
        snapshot = self.healthy_snapshot()
        snapshot.warnings = ["Kubernetes discovery skipped: access denied"]

        results = evaluate_health(snapshot, check_type="discovery")

        self.assertEqual(1, len(results))
        self.assertEqual("WARN", results[0].status)
        self.assertIn("Kubernetes discovery skipped", results[0].message)
        self.assertIsNotNone(results[0].recommendation)
        self.assertEqual(1, health_exit_code(results))

    def test_disabled_discovery_sources_degrade_health_without_snapshot_warnings(self):
        snapshot = self.healthy_snapshot()
        snapshot.oci_discovery_enabled = False
        snapshot.kubernetes_discovery_enabled = False

        results = evaluate_health(snapshot, check_type="discovery")

        self.assertEqual(2, len(results))
        self.assertTrue(all(result.status == "WARN" for result in results))
        self.assertTrue(any("OCI discovery is disabled" in result.message for result in results))
        self.assertTrue(
            any("Kubernetes discovery is disabled" in result.message for result in results)
        )

    def test_failures_produce_actionable_recommendations(self):
        snapshot = self.healthy_snapshot()
        snapshot.pools[0].ready_k8s_nodes = 0
        snapshot.nodes[0].ready = False
        snapshot.nodes[0].allocatable = {}
        snapshot.nodes[0].labels = {}

        results = evaluate_health(snapshot)
        recommendations = actionable_recommendations(results)

        self.assertTrue(any(result.check == "pool-convergence" for result in recommendations))
        self.assertTrue(any(result.check == "gpu-allocatable" for result in recommendations))
        self.assertTrue(any(result.check == "rdma-topology" for result in recommendations))

    def test_active_network_operator_requires_rdma_vf_capacity(self):
        snapshot = self.healthy_snapshot()
        snapshot.addons.append(AddonInfo("NvidiaNetworkOperator", "ACTIVE", "v1"))

        results = evaluate_health(snapshot, check_type="rdma")

        rdma_vf = next(result for result in results if result.check == "rdma-vf")
        self.assertEqual("FAIL", rdma_vf.status)

    def test_addon_validation_can_select_gpu_or_rdma_results(self):
        snapshot = self.healthy_snapshot()

        gpu = addon_validation_results(snapshot, "gpu")
        rdma = addon_validation_results(snapshot, "rdma")

        self.assertTrue(all("gpu" in result.check or "node-feature" in result.check for result in gpu))
        self.assertTrue(all("rdma" in result.check or "node-feature" in result.check or "network" in result.check for result in rdma))

    def test_amd_gpu_requires_nfd_but_not_nvidia_gpu_operator(self):
        snapshot = DiscoverySnapshot(
            pools=[
                WorkerPoolInfo(
                    name="amd",
                    kind="node-pool",
                    desired_size=0,
                    active_oci_instances=0,
                    ready_k8s_nodes=0,
                    gpu_resource="amd.com/gpu",
                )
            ]
        )

        results = evaluate_health(snapshot, check_type="addons")

        checks = {result.check for result in results}
        self.assertIn("addon-node-feature-discovery", checks)
        self.assertNotIn("addon-gpu-operator", checks)

    def test_unknown_type_and_pool_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_health(self.healthy_snapshot(), check_type="unknown")
        with self.assertRaises(ValueError):
            evaluate_health(self.healthy_snapshot(), pool_name="missing")

    def test_health_exit_status_contract(self):
        self.assertEqual(0, health_exit_code([HealthResult("c", "s", "PASS", "ok")]))
        self.assertEqual(1, health_exit_code([HealthResult("c", "s", "WARN", "warn")]))
        self.assertEqual(2, health_exit_code([HealthResult("c", "s", "FAIL", "fail")]))


if __name__ == "__main__":
    unittest.main()
