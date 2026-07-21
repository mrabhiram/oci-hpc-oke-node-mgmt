from __future__ import annotations

import unittest

from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo
from oke_hpc_mgmt.selection import (
    SelectionError,
    node_filter_values,
    parse_field_filters,
    select_nodes,
    split_identifiers,
)


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ready_gpu = NodeInfo(
            k8s_name="gpu-1",
            internal_ip="10.0.0.1",
            pool_name="oke-gpu",
            shape="VM.GPU.A10.1",
            ready=True,
            allocatable={"nvidia.com/gpu": "1"},
            running_workload_pods=2,
        )
        self.not_ready = NodeInfo(
            k8s_name="cpu-1",
            internal_ip="10.0.0.2",
            pool_name="oke-cpu",
            shape="VM.Standard.E5.Flex",
            ready=False,
        )
        self.snapshot = DiscoverySnapshot(nodes=[self.ready_gpu, self.not_ready])

    def test_parse_field_filters_types_booleans_and_integers(self):
        self.assertEqual(
            {"ready": True, "workload_pods": 2, "pool": "oke-gpu"},
            parse_field_filters("ready=yes,workload_pods=2,pool=oke-gpu"),
        )

    def test_parse_field_filters_rejects_unknown_or_malformed_fields(self):
        with self.assertRaises(SelectionError):
            parse_field_filters("unknown=value")
        with self.assertRaises(SelectionError):
            parse_field_filters("ready")

    def test_split_identifiers_splits_csv_and_preserves_order_without_duplicates(self):
        self.assertEqual(
            ["gpu-1", "10.0.0.1", "cpu-1"],
            split_identifiers(["gpu-1,10.0.0.1", "gpu-1", "cpu-1"]),
        )

    def test_select_nodes_reports_missing_identifiers_and_applies_filters(self):
        nodes, missing = select_nodes(
            self.snapshot,
            identifiers=("gpu-1", "missing"),
            fields="gpu=true,workload_pods=2",
        )

        self.assertEqual([self.ready_gpu], nodes)
        self.assertEqual(["missing"], missing)

    def test_select_nodes_supports_not_ready_workload_and_pool_shortcuts(self):
        not_ready, _ = select_nodes(self.snapshot, not_ready=True)
        workloads, _ = select_nodes(self.snapshot, workloads=True, pool="oke-gpu")

        self.assertEqual([self.not_ready], not_ready)
        self.assertEqual([self.ready_gpu], workloads)

    def test_node_filter_values_exposes_operational_fields(self):
        values = node_filter_values(self.ready_gpu)

        self.assertEqual("Ready", values["status"])
        self.assertTrue(values["gpu"])
        self.assertEqual(2, values["workload_pods"])


if __name__ == "__main__":
    unittest.main()
