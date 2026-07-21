import unittest

from oke_hpc_mgmt.backends.kubernetes import parse_instance_ocid
from oke_hpc_mgmt.models import (
    AddonInfo,
    DiscoverySnapshot,
    NodeInfo,
    PoolResourceReadiness,
    WorkerPoolInfo,
)
from oke_hpc_mgmt.workflows.lifecycle import pool_resource_readiness


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

    def test_sentinel_topology_value_is_not_ready(self):
        node = NodeInfo(
            k8s_name="a100",
            shape="BM.GPU4.8",
            labels={
                "oci.oraclecloud.com/rdma.hpc_island_id": "no-imds-data",
                "oci.oraclecloud.com/rdma.network_block_id": "block-1",
                "oci.oraclecloud.com/rdma.local_block_id": "local-1",
            },
        )

        self.assertTrue(node.rdma_capable)
        self.assertFalse(node.rdma_topology_ready)

    def test_slurm_name_is_a_node_identifier(self):
        node = NodeInfo(
            k8s_name="10.0.0.9",
            annotations={"nodeset.slinky.slurm.net/hostname-override": "rdma-9"},
        )
        snapshot = DiscoverySnapshot(nodes=[node])

        self.assertEqual("rdma-9", node.slurm_name)
        self.assertTrue(node.slinky_managed)
        self.assertIs(node, snapshot.node_by_identifier("rdma-9"))

    def test_slinky_prefix_and_worker_pods_mark_node_as_managed(self):
        prefixed = NodeInfo(
            k8s_name="prefixed",
            labels={"oci.oraclecloud.com/slinky-hostname-prefix": "gpu"},
        )
        worker = NodeInfo(k8s_name="worker", slinky_workload_pods=1)

        self.assertTrue(prefixed.slinky_managed)
        self.assertTrue(worker.slinky_managed)

    def test_addon_active_and_normalized_lookup(self):
        active = AddonInfo(
            name="NvidiaNetworkOperator",
            lifecycle_state="ACTIVE",
            version="v25.10.0",
        )
        failed = AddonInfo(name="NvidiaGpuOperator", lifecycle_state="ACTIVE", error="failed")
        snapshot = DiscoverySnapshot(addons=[active, failed])

        self.assertTrue(active.active)
        self.assertFalse(failed.active)
        self.assertIs(active, snapshot.addon_by_name("nvidia-network-operator"))
        self.assertTrue(snapshot.network_operator_active)

    def test_pool_resource_readiness_requires_allocatable_gpu_and_rdma_labels(self):
        labels = {
            "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
            "oci.oraclecloud.com/rdma.network_block_id": "block-1",
            "oci.oraclecloud.com/rdma.local_block_id": "local-1",
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

        self.assertEqual(
            PoolResourceReadiness(gpu_ready=1, rdma_topology_ready=2),
            pool_resource_readiness(snapshot, pool),
        )

    def test_pool_resource_readiness_requires_rdma_vf_when_enabled(self):
        labels = {
            "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
            "oci.oraclecloud.com/rdma.network_block_id": "block-1",
            "oci.oraclecloud.com/rdma.local_block_id": "local-1",
        }
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            rdma_enabled=True,
            rdma_vf_required=True,
        )
        snapshot = DiscoverySnapshot(
            pools=[pool],
            nodes=[
                NodeInfo(
                    k8s_name="ready",
                    pool_name="oke-rdma",
                    shape="BM.GPU4.8",
                    ready=True,
                    labels=labels,
                    allocatable={"nvidia.com/rdma-vf": "8"},
                ),
                NodeInfo(
                    k8s_name="vf-pending",
                    pool_name="oke-rdma",
                    shape="BM.GPU4.8",
                    ready=True,
                    labels=labels,
                ),
            ],
        )

        self.assertEqual(
            PoolResourceReadiness(rdma_topology_ready=2, rdma_vf_ready=1),
            pool_resource_readiness(snapshot, pool),
        )


if __name__ == "__main__":
    unittest.main()
