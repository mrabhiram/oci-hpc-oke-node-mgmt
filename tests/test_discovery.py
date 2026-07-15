import unittest

from oke_hpc_mgmt.discovery import DiscoveryOptions, DiscoveryService
from oke_hpc_mgmt.models import AddonInfo, NodeInfo, WorkerPoolInfo


class _OciBackend:
    def __init__(self, managed=None, cluster_networks=None, standalone=None, addons=None):
        self.managed = managed or []
        self.cluster_networks = cluster_networks or []
        self.standalone = standalone or []
        self.addons = addons or []
        self.instance_pool_call = None

    def list_managed_node_pools(self, compartment_id, cluster_id=None):
        return self.managed

    def list_cluster_network_pools(self, compartment_id):
        return self.cluster_networks

    def list_instance_pools(
        self,
        compartment_id,
        skip_ids=None,
        skip_compute_cluster_ids=None,
        skip_instance_ids=None,
    ):
        self.instance_pool_call = {
            "skip_ids": skip_ids,
            "skip_compute_cluster_ids": skip_compute_cluster_ids,
            "skip_instance_ids": skip_instance_ids,
        }
        return self.standalone

    def list_cluster_addons(self, cluster_id):
        return self.addons


class _KubernetesBackend:
    def __init__(self, nodes=None):
        self.nodes = nodes or []

    def list_nodes(self, include_pod_counts=True):
        return self.nodes

    def list_autoscaler_entries(self):
        return []

    def get_kueue_summary(self):
        raise AssertionError("Kueue should be disabled in these tests")


def _service(oci_backend=None, kubernetes_backend=None, **overrides):
    values = {
        "compartment_id": "compartment-1",
        "cluster_id": "cluster-1",
        "auth": "instance_principal",
        "include_autoscaler": False,
        "include_kueue": False,
    }
    values.update(overrides)
    service = DiscoveryService(DiscoveryOptions(**values))
    service._oci_backend = oci_backend
    service._k8s_backend = kubernetes_backend
    return service


class DiscoveryTests(unittest.TestCase):
    def test_managed_compute_cluster_metadata_is_used_to_filter_instance_pools(self):
        managed = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="node-pool-1",
            compute_cluster_id="compute-cluster-1",
            placement_type="compute-cluster",
            oci_instance_ids={"instance-1"},
        )
        legacy = WorkerPoolInfo(
            name="legacy-rdma",
            kind="cluster-network",
            instance_pool_id="legacy-pool-1",
        )
        backend = _OciBackend(managed=[managed], cluster_networks=[legacy])
        service = _service(backend, _KubernetesBackend())

        snapshot = service.discover()

        self.assertEqual([managed, legacy], snapshot.pools)
        self.assertEqual({"legacy-pool-1"}, backend.instance_pool_call["skip_ids"])
        self.assertEqual(
            {"compute-cluster-1"},
            backend.instance_pool_call["skip_compute_cluster_ids"],
        )
        self.assertEqual({"instance-1"}, backend.instance_pool_call["skip_instance_ids"])

    def test_network_operator_requires_vf_readiness_only_for_rdma_pools(self):
        rdma = WorkerPoolInfo(name="oke-rdma", kind="node-pool", rdma_enabled=True)
        cpu = WorkerPoolInfo(name="oke-cpu", kind="node-pool")
        backend = _OciBackend(
            managed=[rdma, cpu],
            addons=[
                AddonInfo(name="NvidiaNetworkOperator", lifecycle_state="ACTIVE")
            ],
        )
        service = _service(backend, _KubernetesBackend())

        snapshot = service.discover()

        self.assertTrue(snapshot.network_operator_active)
        self.assertTrue(rdma.rdma_vf_required)
        self.assertFalse(cpu.rdma_vf_required)

    def test_inactive_network_operator_does_not_require_vfs(self):
        rdma = WorkerPoolInfo(name="oke-rdma", kind="node-pool", rdma_enabled=True)
        backend = _OciBackend(
            managed=[rdma],
            addons=[
                AddonInfo(
                    name="NvidiaNetworkOperator",
                    lifecycle_state="NEEDS_ATTENTION",
                )
            ],
        )

        _service(backend, _KubernetesBackend()).discover()

        self.assertFalse(rdma.rdma_vf_required)

    def test_node_annotation_marks_discovered_pool_as_slinky_managed(self):
        pool = WorkerPoolInfo(name="oke-rdma", kind="cluster-network")
        node = NodeInfo(
            k8s_name="10.0.0.1",
            pool_name="oke-rdma",
            ready=True,
            annotations={"nodeset.slinky.slurm.net/hostname-override": "rdma-1"},
        )
        service = _service(_OciBackend(cluster_networks=[pool]), _KubernetesBackend([node]))

        service.discover()

        self.assertTrue(pool.slinky_managed)
        self.assertEqual(1, pool.ready_k8s_nodes)

    def test_kubernetes_only_discovery_infers_placement_and_slinky_state(self):
        node = NodeInfo(
            k8s_name="10.0.0.1",
            pool_name="oke-rdma",
            ready=True,
            labels={
                "oke.oraclecloud.com/pool.mode": "cluster-network",
                "oci.oraclecloud.com/slinky-hostname-prefix": "rdma",
            },
        )
        service = _service(
            None,
            _KubernetesBackend([node]),
            auth="none",
            skip_oci=True,
        )

        snapshot = service.discover()

        self.assertEqual(1, len(snapshot.pools))
        self.assertEqual("cluster-network", snapshot.pools[0].placement_type)
        self.assertTrue(snapshot.pools[0].slinky_managed)

    def test_addon_only_discovery_does_not_require_compartment(self):
        backend = _OciBackend(
            addons=[AddonInfo(name="NodeFeatureDiscovery", lifecycle_state="ACTIVE")]
        )
        service = _service(
            backend,
            _KubernetesBackend(),
            compartment_id=None,
            include_pools=False,
            skip_kubernetes=True,
        )

        snapshot = service.discover()

        self.assertEqual(1, len(snapshot.addons))
        self.assertEqual([], snapshot.warnings)


if __name__ == "__main__":
    unittest.main()
