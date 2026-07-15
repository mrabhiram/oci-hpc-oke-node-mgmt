from types import SimpleNamespace
import unittest

from oke_hpc_mgmt.backends.oci import OciBackend, OciDiscoveryError


class _Model:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ComputeManagement:
    def __init__(self, cluster_network=None, instance_pools=None, instances=None):
        self.cluster_network = cluster_network
        self.instance_pools = instance_pools or []
        self.instances = instances or {}
        self.calls = []

    def get_cluster_network(self, cluster_network_id):
        self.calls.append(("get_cluster_network", cluster_network_id))
        return SimpleNamespace(data=self.cluster_network)

    def update_cluster_network(self, cluster_network_id, details):
        self.calls.append(("update_cluster_network", cluster_network_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-cluster-network"})

    def update_instance_pool(self, instance_pool_id, details):
        self.calls.append(("update_instance_pool", instance_pool_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-instance-pool"})

    def detach_instance_pool_instance(self, instance_pool_id, details):
        self.calls.append(("detach_instance_pool_instance", instance_pool_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-detach"})

    def list_instance_pools(self, compartment_id):
        self.calls.append(("list_instance_pools", compartment_id))
        return SimpleNamespace(data=self.instance_pools)

    def list_instance_pool_instances(self, compartment_id, instance_pool_id):
        self.calls.append(("list_instance_pool_instances", compartment_id, instance_pool_id))
        return SimpleNamespace(data=self.instances.get(instance_pool_id, []))


class _ContainerEngine:
    def __init__(self, node_pools=None, addons=None):
        self.node_pools = node_pools or []
        self.addons = addons or []
        self.calls = []

    def list_node_pools(self, **kwargs):
        self.calls.append(("list_node_pools", kwargs))
        return SimpleNamespace(data=[SimpleNamespace(id=pool.id) for pool in self.node_pools])

    def get_node_pool(self, node_pool_id):
        self.calls.append(("get_node_pool", node_pool_id))
        pool = next(pool for pool in self.node_pools if pool.id == node_pool_id)
        return SimpleNamespace(data=pool)

    def update_node_pool(self, node_pool_id, details):
        self.calls.append(("update_node_pool", node_pool_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-node-pool"})

    def list_addons(self, cluster_id):
        self.calls.append(("list_addons", cluster_id))
        return SimpleNamespace(data=self.addons)


class _Compute:
    def __init__(self, shapes=None):
        self.shapes = shapes or {}

    def get_instance(self, instance_id):
        return SimpleNamespace(data=SimpleNamespace(shape=self.shapes.get(instance_id)))


class _Pagination:
    @staticmethod
    def list_call_get_all_results(function, *args, **kwargs):
        return function(*args, **kwargs)


def _backend(cluster_network=None):
    backend = OciBackend(auth="none")
    backend._oci = SimpleNamespace(
        pagination=_Pagination(),
        container_engine=SimpleNamespace(
            models=SimpleNamespace(
                UpdateNodePoolNodeConfigDetails=_Model,
                UpdateNodePoolDetails=_Model,
            )
        ),
        core=SimpleNamespace(
            models=SimpleNamespace(
                UpdateClusterNetworkInstancePoolDetails=_Model,
                UpdateClusterNetworkDetails=_Model,
                UpdateInstancePoolDetails=_Model,
                DetachInstancePoolInstanceDetails=_Model,
            )
        )
    )
    backend._compute_mgmt = _ComputeManagement(cluster_network)
    backend._container_engine = _ContainerEngine()
    backend._compute = _Compute()
    return backend


class OciBackendMutationTests(unittest.TestCase):
    def test_resize_managed_node_pool_sends_only_size(self):
        backend = _backend()

        work_request = backend.resize_managed_node_pool("node-pool-1", 3)

        self.assertEqual("wr-node-pool", work_request)
        _, node_pool_id, details = backend._container_engine.calls[-1]
        self.assertEqual("node-pool-1", node_pool_id)
        self.assertEqual({"size": 3}, details.node_config_details.__dict__)

    def test_resize_managed_node_pool_rejects_negative_size(self):
        backend = _backend()

        with self.assertRaises(OciDiscoveryError):
            backend.resize_managed_node_pool("node-pool-1", -1)

    def test_resize_cluster_network_preserves_pool_fields(self):
        pool = SimpleNamespace(
            id="pool-1",
            instance_configuration_id="config-1",
            display_name="oke-rdma",
            size=2,
            defined_tags={"ns": {"key": "value"}},
            freeform_tags={"pool": "oke-rdma"},
        )
        cluster = SimpleNamespace(
            display_name="oke-rdma",
            defined_tags={"ns": {"key": "value"}},
            freeform_tags={"pool": "oke-rdma"},
            instance_pools=[pool],
        )
        backend = _backend(cluster)

        work_request = backend.resize_cluster_network("cluster-1", "pool-1", 3)

        self.assertEqual("wr-cluster-network", work_request)
        _, cluster_id, details = backend._compute_mgmt.calls[-1]
        self.assertEqual("cluster-1", cluster_id)
        self.assertEqual("oke-rdma", details.display_name)
        self.assertEqual(3, details.instance_pools[0].size)
        self.assertEqual("config-1", details.instance_pools[0].instance_configuration_id)

    def test_resize_cluster_network_rejects_unrelated_instance_pool(self):
        cluster = SimpleNamespace(instance_pools=[SimpleNamespace(id="pool-1", size=2)])
        backend = _backend(cluster)

        with self.assertRaises(OciDiscoveryError):
            backend.resize_cluster_network("cluster-1", "pool-2", 3)

    def test_resize_instance_pool(self):
        backend = _backend()

        work_request = backend.resize_instance_pool("pool-1", 4)

        self.assertEqual("wr-instance-pool", work_request)
        _, pool_id, details = backend._compute_mgmt.calls[-1]
        self.assertEqual("pool-1", pool_id)
        self.assertEqual(4, details.size)

    def test_detach_instance_pool_node_with_replacement(self):
        backend = _backend()

        work_request = backend.detach_instance_pool_node(
            "pool-1",
            "instance-1",
            decrement_size=False,
        )

        self.assertEqual("wr-detach", work_request)
        _, pool_id, details = backend._compute_mgmt.calls[-1]
        self.assertEqual("pool-1", pool_id)
        self.assertEqual("instance-1", details.instance_id)
        self.assertFalse(details.is_decrement_size)
        self.assertTrue(details.is_auto_terminate)

    def test_call_wraps_sdk_exception_with_operation(self):
        def fail():
            raise ValueError("bad request")

        with self.assertRaisesRegex(OciDiscoveryError, "Read failed: bad request"):
            OciBackend._call("Read", fail)


class OciBackendDiscoveryTests(unittest.TestCase):
    def test_managed_compute_cluster_pool_preserves_placement_metadata(self):
        node_config = SimpleNamespace(
            size=2,
            compute_cluster_id="compute-cluster-1",
            placement_configs=[
                SimpleNamespace(
                    availability_domain="AD-1",
                    host_group_id="host-group-1",
                )
            ],
        )
        pool = SimpleNamespace(
            id="node-pool-1",
            name="oke-rdma",
            node_shape="BM.GPU4.8",
            node_config_details=node_config,
            initial_node_labels=[
                SimpleNamespace(
                    key="oci.oraclecloud.com/slinky-hostname-prefix",
                    value="rdma",
                )
            ],
            nodes=[
                SimpleNamespace(id="instance-active", lifecycle_state="ACTIVE"),
                SimpleNamespace(id="instance-deleted", lifecycle_state="DELETED"),
            ],
        )
        backend = _backend()
        backend._container_engine = _ContainerEngine([pool])

        discovered = backend.list_managed_node_pools("compartment-1", "cluster-1")[0]

        self.assertEqual("compute-cluster", discovered.placement_type)
        self.assertEqual("compute-cluster-1", discovered.compute_cluster_id)
        self.assertEqual({"host-group-1"}, discovered.host_group_ids)
        self.assertEqual("AD-1", discovered.availability_domain)
        self.assertTrue(discovered.rdma_enabled)
        self.assertEqual({"instance-active"}, discovered.oci_instance_ids)
        self.assertEqual("rdma", discovered.labels["oci.oraclecloud.com/slinky-hostname-prefix"])

    def test_list_cluster_addons_maps_lifecycle_version_and_error(self):
        addons = [
            SimpleNamespace(
                name="NodeFeatureDiscovery",
                lifecycle_state="ACTIVE",
                current_installed_version="v0.17.3-1",
                version=None,
                addon_error=None,
            ),
            SimpleNamespace(
                name="NvidiaNetworkOperator",
                lifecycle_state="NEEDS_ATTENTION",
                current_installed_version=None,
                version="v25.10.0",
                addon_error="rollout failed",
            ),
        ]
        backend = _backend()
        backend._container_engine = _ContainerEngine(addons=addons)

        discovered = backend.list_cluster_addons("cluster-1")

        self.assertEqual("v0.17.3-1", discovered[0].version)
        self.assertTrue(discovered[0].active)
        self.assertEqual("rollout failed", discovered[1].error)
        self.assertFalse(discovered[1].active)

    def test_instance_pool_discovery_hides_managed_backing_pools(self):
        pools = [
            SimpleNamespace(
                id="pool-compute-cluster",
                display_name="oke-rdma-backing",
                lifecycle_state="RUNNING",
                size=2,
                placement_configurations=[
                    SimpleNamespace(
                        availability_domain="AD-1",
                        compute_cluster_id="compute-cluster-1",
                    )
                ],
            ),
            SimpleNamespace(
                id="pool-overlap",
                display_name="oke-rdma-overlap",
                lifecycle_state="RUNNING",
                size=1,
                placement_configurations=[],
            ),
            SimpleNamespace(
                id="pool-standalone",
                display_name="standalone",
                lifecycle_state="RUNNING",
                size=1,
                placement_configurations=[],
            ),
        ]
        instances = {
            "pool-compute-cluster": [
                SimpleNamespace(id="instance-cc", lifecycle_state="RUNNING")
            ],
            "pool-overlap": [
                SimpleNamespace(id="instance-managed", lifecycle_state="RUNNING")
            ],
            "pool-standalone": [
                SimpleNamespace(id="instance-standalone", lifecycle_state="RUNNING")
            ],
        }
        backend = _backend()
        backend._compute_mgmt = _ComputeManagement(instance_pools=pools, instances=instances)
        backend._compute = _Compute(
            {
                "instance-cc": "BM.GPU4.8",
                "instance-managed": "BM.GPU4.8",
                "instance-standalone": "VM.Standard.E5.Flex",
            }
        )

        discovered = backend.list_instance_pools(
            "compartment-1",
            skip_compute_cluster_ids={"compute-cluster-1"},
            skip_instance_ids={"instance-managed"},
        )

        self.assertEqual(["standalone"], [pool.name for pool in discovered])
        self.assertEqual("instance-pool", discovered[0].placement_type)

    def test_instance_pool_in_unmanaged_compute_cluster_remains_visible(self):
        pool = SimpleNamespace(
            id="pool-1",
            display_name="external-compute-cluster-pool",
            lifecycle_state="RUNNING",
            size=0,
            placement_configurations=[
                SimpleNamespace(
                    availability_domain="AD-1",
                    compute_cluster_id="compute-cluster-external",
                )
            ],
        )
        backend = _backend()
        backend._compute_mgmt = _ComputeManagement(instance_pools=[pool])

        discovered = backend.list_instance_pools("compartment-1")

        self.assertEqual(1, len(discovered))
        self.assertEqual("compute-cluster", discovered[0].placement_type)
        self.assertEqual("compute-cluster-external", discovered[0].compute_cluster_id)


if __name__ == "__main__":
    unittest.main()
