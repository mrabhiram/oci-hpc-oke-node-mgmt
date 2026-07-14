import unittest

from oke_hpc_mgmt.backends.kubernetes import parse_instance_ocid
from oke_hpc_mgmt.cli import _pool_resource_readiness
from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo, WorkerPoolInfo


class ModelTests(unittest.TestCase):
    def test_parse_instance_ocid_from_oci_provider_id(self):
        provider_id = "oci://ocid1.instance.oc1.iad.exampleuniqueid"

        self.assertEqual(parse_instance_ocid(provider_id), "ocid1.instance.oc1.iad.exampleuniqueid")

    def test_parse_instance_ocid_from_raw_ocid(self):
        provider_id = "ocid1.instance.oc1.iad.exampleuniqueid"

        self.assertEqual(parse_instance_ocid(provider_id), "ocid1.instance.oc1.iad.exampleuniqueid")

    def test_node_status_ready_and_unschedulable(self):
        node = NodeInfo(k8s_name="10.0.0.1", ready=True, schedulable=False)

        self.assertEqual(node.status, "SchedulingDisabled")

    def test_a10_topology_labels_do_not_imply_rdma_capability(self):
        node = NodeInfo(
            k8s_name="a10",
            shape="VM.GPU.A10.1",
            labels={
                "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
                "oci.oraclecloud.com/rdma.network_block_id": "block-1",
                "feature.node.kubernetes.io/rdma.available": "true",
            },
        )

        self.assertFalse(node.has_rdma_labels)

    def test_bare_metal_gpu_with_topology_labels_is_rdma_capable(self):
        node = NodeInfo(
            k8s_name="a100",
            shape="BM.GPU4.8",
            labels={
                "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
                "oci.oraclecloud.com/rdma.network_block_id": "block-1",
                "oci.oraclecloud.com/rdma.local_block_id": "local-1",
            },
        )

        self.assertTrue(node.has_rdma_labels)

    def test_pool_resource_readiness_requires_allocatable_gpu_and_rdma_labels(self):
        labels = {
            "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
            "feature.node.kubernetes.io/rdma.available": "true",
        }
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
        )
        snapshot = DiscoverySnapshot(
            pools=[pool],
            nodes=[
                NodeInfo(
                    k8s_name="ready",
                    pool_name="oke-rdma",
                    shape="BM.GPU4.8",
                    ready=True,
                    allocatable={"nvidia.com/gpu": "8"},
                    labels=labels,
                ),
                NodeInfo(
                    k8s_name="gpu-pending",
                    pool_name="oke-rdma",
                    shape="BM.GPU4.8",
                    ready=True,
                    labels=labels,
                ),
            ],
        )

        self.assertEqual((1, 2), _pool_resource_readiness(snapshot, pool))


if __name__ == "__main__":
    unittest.main()
