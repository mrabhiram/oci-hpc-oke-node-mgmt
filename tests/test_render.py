import csv
import io
import unittest
from contextlib import redirect_stdout

from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo, WorkerPoolInfo
from oke_hpc_mgmt.render import print_snapshot, topology_rows


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


if __name__ == "__main__":
    unittest.main()
