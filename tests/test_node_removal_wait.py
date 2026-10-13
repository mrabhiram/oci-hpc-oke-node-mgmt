from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock, patch

from oke_hpc_mgmt.discovery import DiscoveryOptions
from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo, WorkerPoolInfo, WorkRequestInfo
from oke_hpc_mgmt.workflows.lifecycle import (
    WorkflowError, execute_node_removal, prepare_node_removal, wait_for_nodes_removed,
)


class NodeRemovalWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = WorkerPoolInfo(
            name="gpu-rdma", kind="node-pool", node_pool_id="pool-id",
            compute_cluster_id="compute-cluster-id", placement_type="compute-cluster",
            shape="BM.GPU.B200.8", desired_size=2, active_oci_instances=2,
            ready_k8s_nodes=2, gpu_resource="nvidia.com/gpu", rdma_enabled=True,
            rdma_vf_required=True, oci_instance_ids={"instance-old", "instance-survivor"},
        )
        labels = {
            "oci.oraclecloud.com/rdma.hpc_island_id": "island",
            "oci.oraclecloud.com/rdma.network_block_id": "block",
            "oci.oraclecloud.com/rdma.local_block_id": "local",
        }
        self.removed = NodeInfo(
            k8s_name="gpu-old", instance_ocid="instance-old", pool_name=self.pool.name,
            node_pool_id=self.pool.node_pool_id, ready=True, shape=self.pool.shape,
            allocatable={"nvidia.com/gpu": "8"}, labels=labels,
        )
        self.survivor = NodeInfo(
            k8s_name="gpu-survivor", instance_ocid="instance-survivor", pool_name=self.pool.name,
            node_pool_id=self.pool.node_pool_id, ready=True, shape=self.pool.shape,
            allocatable={"nvidia.com/gpu": "0"},
        )
        self.observed = replace(
            self.pool, desired_size=1, active_oci_instances=1, ready_k8s_nodes=1,
            oci_instance_ids={"instance-survivor"},
        )
        self.snapshot = DiscoverySnapshot(pools=[self.observed], nodes=[self.survivor])
        self.service = Mock()
        self.service.options = DiscoveryOptions(auth="instance_principal")
        self.service.discover.return_value = self.snapshot

    def wait(self, **options: Any) -> dict[str, WorkerPoolInfo]:
        return wait_for_nodes_removed(
            self.service, (self.removed,), {self.pool.name: 1}, 30, 1,
            {self.pool.name: self.pool}, **options,
        )

    def backend_pool(self, kind: str) -> WorkerPoolInfo:
        return replace(
            self.pool, kind=kind,
            node_pool_id="pool-id" if kind == "node-pool" else None,
            cluster_network_id="cluster-network-id" if kind == "cluster-network" else None,
            instance_pool_id="instance-pool-id" if kind != "node-pool" else None,
            compute_cluster_id="compute-cluster-id" if kind == "node-pool" else None,
        )

    def test_removal_finishes_with_degraded_survivor_gpu_rdma_and_vf_readiness(self) -> None:
        for kind in ("node-pool", "cluster-network", "instance-pool"):
            with self.subTest(kind=kind):
                self.pool = self.backend_pool(kind)
                self.observed = replace(self.observed, kind=kind, node_pool_id=self.pool.node_pool_id,
                                        cluster_network_id=self.pool.cluster_network_id,
                                        instance_pool_id=self.pool.instance_pool_id,
                                        compute_cluster_id=self.pool.compute_cluster_id)
                self.snapshot.pools = [self.observed]
                self.service.discover.reset_mock()
                progress = Mock()
                with patch("oke_hpc_mgmt.workflows.lifecycle.time.sleep") as sleep:
                    result = self.wait(require_replacement_ready=False, progress=progress)
                self.assertIs(self.observed, result[self.pool.name])
                self.service.discover.assert_called_once_with()
                sleep.assert_not_called()
                self.assertIn("gpu_ready=0", progress.call_args_list[0].args[0])
                self.assertIn("Check health separately", progress.call_args.args[0])

    def test_removal_does_not_require_survivors_to_be_ready(self) -> None:
        self.observed.ready_k8s_nodes = 0
        self.survivor.ready = False
        self.assertIs(self.observed, self.wait(require_replacement_ready=False)[self.pool.name])

    def test_removal_does_not_wait_for_surviving_instances_to_register_with_kubernetes(self) -> None:
        self.observed.ready_k8s_nodes = 0
        self.snapshot.nodes = []
        progress = Mock()
        self.assertIs(self.observed, self.wait(require_replacement_ready=False, progress=progress)[self.pool.name])
        self.assertIn("k8s_registered=0", progress.call_args_list[0].args[0])

    def test_wait_observes_delayed_oci_and_kubernetes_removal_without_an_extra_poll(self) -> None:
        initial = replace(self.observed, active_oci_instances=2, ready_k8s_nodes=2,
                          oci_instance_ids={"instance-old", "instance-survivor"})
        intermediate = replace(self.observed, ready_k8s_nodes=2)
        self.service.discover.side_effect = [
            DiscoverySnapshot(pools=[initial], nodes=[self.removed, self.survivor]),
            DiscoverySnapshot(pools=[intermediate], nodes=[self.removed, self.survivor]),
            self.snapshot,
        ]
        with patch("oke_hpc_mgmt.workflows.lifecycle.time.sleep") as sleep:
            self.assertIs(self.observed, self.wait(require_replacement_ready=False)[self.pool.name])
        self.assertEqual(3, self.service.discover.call_count)
        self.assertEqual(2, sleep.call_count)

    def test_incomplete_capacity_or_selected_membership_does_not_finish(self) -> None:
        for problem in ("desired", "active", "unknown-active", "kubernetes", "oci-membership"):
            observed = replace(self.observed, oci_instance_ids=set(self.observed.oci_instance_ids))
            snapshot = DiscoverySnapshot(pools=[observed], nodes=[self.survivor])
            if problem == "desired":
                observed.desired_size = 2
            elif problem == "active":
                observed.active_oci_instances = 2
            elif problem == "unknown-active":
                observed.active_oci_instances = None
            elif problem == "kubernetes":
                snapshot.nodes.append(self.removed)
            elif problem == "oci-membership":
                observed.oci_instance_ids.add(self.removed.instance_ocid or "")
            self.service.discover.return_value = snapshot
            with self.subTest(problem=problem), patch("oke_hpc_mgmt.workflows.lifecycle.time.monotonic", side_effect=[0, 31]):
                with self.assertRaisesRegex(TimeoutError, "selected nodes"):
                    self.wait(require_replacement_ready=False)

    def test_zero_size_completion_requires_complete_discovery(self) -> None:
        for problem in ("oci", "kubernetes", "warning"):
            pool = replace(self.observed, desired_size=0, active_oci_instances=0, ready_k8s_nodes=0, oci_instance_ids=set())
            snapshot = DiscoverySnapshot(pools=[pool])
            if problem == "oci":
                snapshot.oci_discovery_enabled = False
            elif problem == "kubernetes":
                snapshot.kubernetes_discovery_enabled = False
            else:
                snapshot.warnings = ["Kubernetes discovery skipped: synthetic RBAC failure"]
            self.service.discover.return_value = snapshot
            with self.subTest(problem=problem), self.assertRaisesRegex(WorkflowError, "not rolled back"):
                wait_for_nodes_removed(
                    self.service, (self.removed,), {self.pool.name: 0}, 30, 1,
                    {self.pool.name: self.pool}, require_replacement_ready=False,
                )

    def test_zero_size_removal_completes_with_empty_authoritative_inventories(self) -> None:
        self.observed.desired_size = self.observed.active_oci_instances = self.observed.ready_k8s_nodes = 0
        self.observed.oci_instance_ids = set()
        self.snapshot.nodes = []
        result = wait_for_nodes_removed(
            self.service, (self.removed,), {self.pool.name: 0}, 30, 1,
            {self.pool.name: self.pool}, require_replacement_ready=False,
        )
        self.assertIs(self.observed, result[self.pool.name])

    def test_disappeared_or_replaced_owning_pool_is_refused(self) -> None:
        for problem in ("missing", "id", "kind"):
            observed = replace(self.observed)
            snapshot = DiscoverySnapshot(pools=[observed], nodes=[self.survivor])
            if problem == "missing":
                snapshot.pools = []
            elif problem == "id":
                observed.node_pool_id = "replacement-pool-id"
            else:
                observed.kind = "instance-pool"
            self.service.discover.return_value = snapshot
            with self.subTest(problem=problem), self.assertRaises(WorkflowError):
                self.wait(require_replacement_ready=False)

    def test_replacement_still_requires_full_gpu_rdma_and_vf_readiness(self) -> None:
        for problem in ("ready", "gpu", "rdma", "vf"):
            survivor = replace(self.survivor, labels=dict(self.removed.labels),
                               allocatable={"nvidia.com/gpu": "8", "nvidia.com/rdma-vf": "8"})
            observed = replace(self.observed)
            if problem == "ready":
                survivor.ready = False
                observed.ready_k8s_nodes = 0
            elif problem == "gpu":
                survivor.allocatable["nvidia.com/gpu"] = "0"
            elif problem == "rdma":
                survivor.labels = {}
            else:
                survivor.allocatable["nvidia.com/rdma-vf"] = "0"
            self.service.discover.return_value = DiscoverySnapshot(pools=[observed], nodes=[survivor])
            with self.subTest(problem=problem), patch("oke_hpc_mgmt.workflows.lifecycle.time.monotonic", side_effect=[0, 31]):
                with self.assertRaises(TimeoutError):
                    self.wait(require_replacement_ready=True)

    def test_replacement_finishes_only_after_resource_readiness_converges(self) -> None:
        healthy = replace(self.survivor, labels=dict(self.removed.labels),
                          allocatable={"nvidia.com/gpu": "8", "nvidia.com/rdma-vf": "8"})
        self.service.discover.side_effect = [self.snapshot, DiscoverySnapshot(pools=[self.observed], nodes=[healthy])]
        with patch("oke_hpc_mgmt.workflows.lifecycle.time.sleep") as sleep:
            self.assertIs(self.observed, self.wait(require_replacement_ready=True)[self.pool.name])
        sleep.assert_called_once_with(1)

    def test_known_failed_work_request_prevents_success_even_with_converged_capacity(self) -> None:
        backend = self.service.oci_backend.return_value
        backend.get_work_request_status.return_value = WorkRequestInfo("request-id", "FAILED", errors=("synthetic failure",))
        self.service.resolve_oci_target.return_value = Mock(compartment_id="synthetic-compartment")
        with self.assertRaisesRegex(WorkflowError, "synthetic failure"):
            self.wait(require_replacement_ready=False, work_request_ids=("request-id",))
        self.service.discover.assert_not_called()

    def test_execute_selects_removal_or_replacement_wait_from_keep_size(self) -> None:
        for kind in ("node-pool", "cluster-network", "instance-pool"):
            pool = self.backend_pool(kind)
            removed = replace(self.removed, node_pool_id=pool.node_pool_id)
            for keep_size in (False, True):
                original = DiscoverySnapshot(pools=[pool], nodes=[removed, self.survivor])
                self.service.discover.return_value = original
                self.service.resolve_oci_target.return_value = Mock(compartment_id="synthetic-compartment")
                backend = self.service.oci_backend.return_value
                backend.list_resource_work_requests.return_value = []
                backend.delete_node.return_value = backend.detach_instance_pool_node.return_value = "request-id"
                prepared = prepare_node_removal(self.service, identifiers=(removed.k8s_name,), keep_size=keep_size, drain=False)
                with self.subTest(kind=kind, keep_size=keep_size), patch("oke_hpc_mgmt.workflows.lifecycle.wait_for_nodes_removed", return_value={pool.name: self.observed}) as wait:
                    rows = execute_node_removal(self.service, prepared, drain=False, wait=True, lock=False)
                self.assertEqual(keep_size, wait.call_args.kwargs["require_replacement_ready"])
                self.assertEqual("removed", rows[0]["status"])
                self.assertEqual(not keep_size, rows[0]["decrement_size"])
