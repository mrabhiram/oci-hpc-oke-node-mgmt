import csv
import io
import unittest
from contextlib import redirect_stdout

from oke_hpc_mgmt.models import (
    AddonInfo,
    DiscoverySnapshot,
    HealthResult,
    NodeInfo,
    OperationPlan,
    WorkerPoolInfo,
)
from oke_hpc_mgmt.render import (
    addon_rows,
    health_rows,
    node_rows,
    operation_plan_rows,
    parse_columns,
    pool_rows,
    print_records,
    print_snapshot,
    sort_records,
    status_rows,
    topology_rows,
)


class RenderTests(unittest.TestCase):
    def test_reconcile_csv_has_typed_pool_node_and_kueue_rows(self):
        snapshot = DiscoverySnapshot(
            pools=[WorkerPoolInfo(name="oke-rdma", kind="cluster-network")],
            nodes=[NodeInfo(k8s_name="node-1", pool_name="oke-rdma", ready=True)],
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_snapshot(snapshot, "csv")

        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(["pool", "node", "kueue"], [row["record_type"] for row in rows])

    def test_topology_omits_nodes_without_rdma_capability(self):
        topology_labels = {
            "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
            "oci.oraclecloud.com/rdma.network_block_id": "block-1",
            "oci.oraclecloud.com/rdma.local_block_id": "local-1",
        }
        nodes = [
            NodeInfo(
                k8s_name="a100",
                shape="BM.GPU4.8",
                ready=True,
                labels=topology_labels,
            ),
            NodeInfo(
                k8s_name="a10",
                shape="VM.GPU.A10.1",
                ready=True,
                labels=topology_labels,
            ),
            NodeInfo(k8s_name="cpu", shape="VM.Standard.E5.Flex", ready=True),
        ]

        rows = topology_rows(nodes)

        self.assertEqual(1, len(rows))
        self.assertEqual(["a100"], rows[0]["node_names"])

    def test_topology_omits_sentinel_imds_values(self):
        node = NodeInfo(
            k8s_name="a100",
            shape="BM.GPU4.8",
            ready=True,
            labels={
                "oci.oraclecloud.com/rdma.hpc_island_id": "no-imds-data",
                "oci.oraclecloud.com/rdma.network_block_id": "block-1",
                "oci.oraclecloud.com/rdma.local_block_id": "local-1",
            },
        )

        self.assertEqual([], topology_rows([node]))

    def test_pool_rows_expose_compute_cluster_and_safety_metadata(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            placement_type="compute-cluster",
            compute_cluster_id="compute-cluster-1",
            host_group_ids={"host-group-1"},
            rdma_enabled=True,
            rdma_vf_required=True,
            slinky_managed=True,
        )

        row = pool_rows([pool])[0]

        self.assertEqual("compute-cluster", row["placement"])
        self.assertEqual("compute-cluster-1", row["compute_cluster_id"])
        self.assertEqual({"host-group-1"}, row["host_group_ids"])
        self.assertTrue(row["rdma_vf_required"])
        self.assertTrue(row["slinky"])

    def test_node_rows_expose_slurm_alias_and_rdma_vfs(self):
        node = NodeInfo(
            k8s_name="10.0.0.1",
            annotations={"nodeset.slinky.slurm.net/hostname-override": "rdma-1"},
            allocatable={"nvidia.com/rdma-vf": "8"},
            ready=True,
            schedulable=False,
            slinky_workload_pods=1,
        )

        row = node_rows([node])[0]

        self.assertEqual("rdma-1", row["slurm_name"])
        self.assertEqual("8", row["rdma_vf"])
        self.assertTrue(row["ready"])
        self.assertFalse(row["schedulable"])
        self.assertEqual(1, row["slurm_pods"])

    def test_addon_rows_report_active_state(self):
        rows = addon_rows(
            [
                AddonInfo(
                    name="NvidiaGpuOperator",
                    lifecycle_state="ACTIVE",
                    version="v25.10.1",
                )
            ]
        )

        self.assertEqual("NvidiaGpuOperator", rows[0]["name"])
        self.assertEqual("v25.10.1", rows[0]["version"])
        self.assertTrue(rows[0]["active"])

    def test_operation_health_and_status_rows_have_stable_fields(self):
        plan = OperationPlan(
            operation="pool-resize",
            target="oke-gpu",
            current_size=1,
            target_size=2,
            steps=("resize",),
        )
        health = HealthResult("pool-convergence", "oke-gpu", "PASS", "ready")
        snapshot = DiscoverySnapshot(
            pools=[WorkerPoolInfo("oke-gpu", "node-pool")],
            nodes=[NodeInfo("gpu-1", ready=True, allocatable={"nvidia.com/gpu": "1"})],
            addons=[AddonInfo("NvidiaGpuOperator", "ACTIVE")],
        )

        self.assertEqual("planned", operation_plan_rows([plan])[0]["status"])
        self.assertEqual("PASS", health_rows([health])[0]["status"])
        self.assertEqual("HEALTHY", status_rows(snapshot, [health])[0]["overall"])

    def test_parse_columns_validates_requested_schema(self):
        self.assertEqual(["name", "status"], parse_columns("name,status", ["name", "status"]))
        with self.assertRaises(ValueError):
            parse_columns("unknown", ["name", "status"])

    def test_sort_records_sorts_numeric_values_numerically(self):
        rows = [{"name": "ten", "pods": 10}, {"name": "two", "pods": 2}]

        self.assertEqual("two", sort_records(rows, "pods")[0]["name"])

    def test_print_records_supports_headerless_and_one_line_output(self):
        output = io.StringIO()
        with redirect_stdout(output):
            print_records(
                [{"name": "node-1"}, {"name": "node-2"}],
                "table",
                ["name"],
                show_header=False,
                one_line=True,
            )

        self.assertEqual("node-1,node-2\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
