import unittest
from unittest.mock import patch

from oke_hpc_mgmt.backends.kubeconfig import KubeconfigDiscoveryError, OkeKubeconfigContext
from oke_hpc_mgmt.backends.oci import OciDiscoveryError
from oke_hpc_mgmt.discovery import DiscoveryOptions, DiscoveryService
from oke_hpc_mgmt.models import AddonInfo, NodeInfo, WorkerPoolInfo


class _OciBackend:
    def __init__(
        self,
        managed=None,
        cluster_networks=None,
        standalone=None,
        addons=None,
        discovered_compartment="discovered-compartment",
    ):
        self.managed = managed or []
        self.cluster_networks = cluster_networks or []
        self.standalone = standalone or []
        self.addons = addons or []
        self.instance_pool_call = None
        self.managed_pool_call = None
        self.cluster_lookups = []
        self.discovered_compartment = discovered_compartment

    def list_managed_node_pools(self, compartment_id, cluster_id=None):
        self.managed_pool_call = (compartment_id, cluster_id)
        return self.managed

    def get_cluster_compartment_id(self, cluster_id):
        self.cluster_lookups.append(cluster_id)
        return self.discovered_compartment

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
    @patch("oke_hpc_mgmt.discovery.load_oke_kubeconfig_context")
    def test_cluster_region_and_compartment_are_discovered_automatically(self, load_context):
        load_context.return_value = OkeKubeconfigContext(
            context_name="london",
            cluster_name="london-cluster",
            cluster_id="cluster-from-kubeconfig",
            region="uk-london-1",
        )
        pool = WorkerPoolInfo(name="oke-cpu", kind="node-pool")
        backend = _OciBackend(managed=[pool])
        service = _service(
            backend,
            _KubernetesBackend(),
            compartment_id=None,
            cluster_id=None,
            region=None,
        )

        snapshot = service.discover()

        self.assertEqual([pool], snapshot.pools)
        self.assertEqual("cluster-from-kubeconfig", service.options.cluster_id)
        self.assertEqual("uk-london-1", service.options.region)
        self.assertEqual("discovered-compartment", service.options.compartment_id)
        self.assertEqual(["cluster-from-kubeconfig"], backend.cluster_lookups)
        self.assertEqual(
            ("discovered-compartment", "cluster-from-kubeconfig"),
            backend.managed_pool_call,
        )

    @patch("oke_hpc_mgmt.discovery.load_oke_kubeconfig_context")
    def test_explicit_target_values_take_precedence(self, load_context):
        backend = _OciBackend()
        service = _service(backend, _KubernetesBackend(), region="uk-london-1")

        target = service.resolve_oci_target(require_compartment=True, require_cluster=True)

        self.assertEqual("compartment-1", target.compartment_id)
        self.assertEqual("cluster-1", target.cluster_id)
        self.assertEqual("uk-london-1", target.region)
        self.assertIs(backend, service.oci_backend())
        load_context.assert_not_called()
        self.assertEqual([], backend.cluster_lookups)

    @patch("oke_hpc_mgmt.discovery.load_oke_kubeconfig_context")
    def test_explicit_compartment_uses_kubeconfig_cluster_without_oci_lookup(self, load_context):
        load_context.return_value = OkeKubeconfigContext(
            context_name="london",
            cluster_name="london-cluster",
            cluster_id="cluster-from-kubeconfig",
            region="uk-london-1",
        )
        backend = _OciBackend()
        service = _service(
            backend,
            _KubernetesBackend(),
            cluster_id=None,
            region=None,
        )

        service.discover()

        self.assertEqual([], backend.cluster_lookups)
        self.assertEqual(
            ("compartment-1", "cluster-from-kubeconfig"),
            backend.managed_pool_call,
        )

    @patch("oke_hpc_mgmt.discovery.load_oke_kubeconfig_context")
    def test_kubeconfig_failure_preserves_kubernetes_only_inventory(self, load_context):
        load_context.side_effect = KubeconfigDiscoveryError("config unavailable")
        node = NodeInfo(k8s_name="node-1", pool_name="oke-cpu", ready=True)
        service = _service(
            _OciBackend(),
            _KubernetesBackend([node]),
            compartment_id=None,
            cluster_id=None,
            region=None,
        )

        snapshot = service.discover()

        self.assertEqual([node], snapshot.nodes)
        self.assertEqual("oke-cpu", snapshot.pools[0].name)
        self.assertTrue(
            any("Automatic OKE target discovery" in warning for warning in snapshot.warnings)
        )

    def test_required_compartment_reports_in_cluster_discovery_limit(self):
        service = _service(
            _OciBackend(),
            _KubernetesBackend(),
            compartment_id=None,
            cluster_id=None,
            region=None,
            in_cluster=True,
        )

        with self.assertRaisesRegex(OciDiscoveryError, "--in-cluster"):
            service.resolve_oci_target(require_compartment=True)

    @patch("oke_hpc_mgmt.discovery.load_oke_kubeconfig_context")
    def test_skip_oci_does_not_read_kubeconfig(self, load_context):
        service = _service(
            None,
            _KubernetesBackend(),
            compartment_id=None,
            cluster_id=None,
            region=None,
            skip_oci=True,
        )

        snapshot = service.discover()

        load_context.assert_not_called()
        self.assertFalse(snapshot.oci_discovery_enabled)
        self.assertTrue(snapshot.kubernetes_discovery_enabled)

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
        self.assertTrue(snapshot.oci_discovery_enabled)
        self.assertFalse(snapshot.kubernetes_discovery_enabled)


if __name__ == "__main__":
    unittest.main()
