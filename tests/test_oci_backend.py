from types import SimpleNamespace
import unittest

from oke_hpc_mgmt.backends.oci import OciBackend, OciDiscoveryError


class _Model:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ComputeManagement:
    def __init__(self, cluster_network=None):
        self.cluster_network = cluster_network
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


def _backend(cluster_network=None):
    backend = OciBackend(auth="none")
    backend._oci = SimpleNamespace(
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
    return backend


class OciBackendMutationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
