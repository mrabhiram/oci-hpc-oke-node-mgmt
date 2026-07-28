from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oke_hpc_mgmt.models import (
    ClusterNetworkCreateResult,
    DiscoverySnapshot,
    DrainPod,
    NodeInfo,
    PoolResourceReadiness,
    WorkerPoolInfo,
    WorkRequestInfo,
)
from oke_hpc_mgmt.workflows.lifecycle import (
    IAC_CREATE_DRIFT_WARNING,
    IAC_DRIFT_WARNING,
    INSTANCE_CONFIGURATION_DERIVATION_NOTICE,
    WorkflowError,
    WorkflowNotFound,
    execute_node_removal,
    execute_pool_create,
    execute_pool_resize,
    prepare_node_removal,
    prepare_pool_create,
    prepare_pool_resize,
    readiness_status,
    resource_counts_match,
    wait_for_pool_creation,
    wait_for_pool_size,
)


def _service(snapshot: DiscoverySnapshot) -> Mock:
    service = Mock()
    service.options = SimpleNamespace(
        auth="instance_principal",
        skip_oci=False,
        skip_kubernetes=False,
        include_pod_counts=True,
        include_autoscaler=True,
        include_kueue=False,
        include_addons=True,
    )
    service.discover.return_value = snapshot
    service.resolve_oci_target.return_value = SimpleNamespace(
        compartment_id="compartment-1"
    )
    return service


class LifecycleWorkflowTests(unittest.TestCase):
    def test_prepare_pool_create_selects_conventional_template_and_builds_plan(self):
        conventional = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            desired_size=2,
        )
        other = WorkerPoolInfo(
            name="other-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-2",
            instance_pool_id="instance-pool-2",
            desired_size=2,
        )
        service = _service(DiscoverySnapshot(pools=[other, conventional]))

        prepared = prepare_pool_create(service, "oke-rdma-2", 2)

        self.assertEqual(conventional, prepared.source_pool)
        self.assertEqual("pool-create", prepared.plan.operation)
        self.assertEqual("compute-management", prepared.plan.owner)
        self.assertEqual(0, prepared.plan.current_size)
        self.assertEqual(2, prepared.plan.target_size)
        self.assertIn(IAC_CREATE_DRIFT_WARNING, prepared.plan.warnings)
        self.assertIn(
            INSTANCE_CONFIGURATION_DERIVATION_NOTICE,
            prepared.plan.warnings,
        )
        service.resolve_oci_target.assert_called_once_with(require_compartment=True)
        service.oci_backend.return_value.validate_cluster_network_pool_template.assert_called_once_with(
            "cluster-network-1",
            "instance-pool-1",
            "oke-rdma-2",
        )

    def test_prepare_pool_create_supports_explicit_template(self):
        source = WorkerPoolInfo(
            name="source-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[source]))

        prepared = prepare_pool_create(
            service,
            "new-rdma",
            1,
            source_identifier="cluster-network-1",
        )

        self.assertEqual(source, prepared.source_pool)

    def test_prepare_pool_create_rejects_duplicates_and_ambiguous_templates(self):
        first = WorkerPoolInfo(
            name="rdma-a",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
        )
        second = WorkerPoolInfo(
            name="rdma-b",
            kind="cluster-network",
            cluster_network_id="cluster-network-2",
            instance_pool_id="instance-pool-2",
        )
        service = _service(DiscoverySnapshot(pools=[first, second]))

        with self.assertRaisesRegex(WorkflowError, "already exists"):
            prepare_pool_create(service, "RDMA-A", 1)
        with self.assertRaisesRegex(WorkflowError, "--from-pool"):
            prepare_pool_create(service, "rdma-c", 1)
        with self.assertRaisesRegex(WorkflowError, "1-63 characters"):
            prepare_pool_create(service, "invalid/pool", 1)

    def test_prepare_pool_create_requires_complete_oci_inventory(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
        )
        snapshot = DiscoverySnapshot(
            pools=[source],
            warnings=["Cluster network discovery skipped: access denied"],
        )

        with self.assertRaisesRegex(WorkflowError, "complete OCI pool discovery"):
            prepare_pool_create(_service(snapshot), "new-rdma", 1)

    def test_prepare_pool_create_rejects_non_cluster_network_source(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="node-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[source]))

        with self.assertRaisesRegex(WorkflowError, "Cluster Network-backed"):
            prepare_pool_create(
                service,
                "new-rdma",
                1,
                source_identifier="oke-rdma",
            )

    def test_execute_pool_create_submits_clone_operation(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            shape="BM.GPU4.8",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[source]))
        created = ClusterNetworkCreateResult(
            cluster_network_id="cluster-network-new",
            instance_configuration_id="instance-configuration-new",
            instance_pool_id="instance-pool-new",
            work_request_id="work-request-new",
        )
        backend = service.oci_backend.return_value
        backend.create_cluster_network_pool.return_value = created
        prepared = prepare_pool_create(service, "oke-rdma-2", 2)

        result = execute_pool_create(service, prepared, lock=False)

        backend.create_cluster_network_pool.assert_called_once_with(
            "cluster-network-1",
            "instance-pool-1",
            "oke-rdma-2",
            2,
        )
        self.assertEqual("submitted", result["status"])
        self.assertEqual("cluster-network-new", result["cluster_network_id"])
        self.assertEqual(
            "instance-configuration-new",
            result["instance_configuration_id"],
        )
        self.assertEqual("instance-pool-new", result["instance_pool_id"])

    def test_execute_pool_create_rechecks_name_under_mutation_lock(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[source]))
        prepared = prepare_pool_create(service, "oke-rdma-2", 1)
        duplicate = WorkerPoolInfo(
            name="oke-rdma-2",
            kind="cluster-network",
            cluster_network_id="cluster-network-2",
            instance_pool_id="instance-pool-2",
        )
        service.discover.return_value = DiscoverySnapshot(pools=[source, duplicate])

        with self.assertRaisesRegex(WorkflowError, "already exists"):
            execute_pool_create(service, prepared, lock=False)

        service.oci_backend.return_value.create_cluster_network_pool.assert_not_called()

    def test_wait_for_pool_creation_tolerates_discovery_delay_and_checks_readiness(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            shape="BM.GPU4.8",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            rdma_enabled=True,
        )
        created = ClusterNetworkCreateResult(
            cluster_network_id="cluster-network-new",
            instance_configuration_id="instance-configuration-new",
            work_request_id="work-request-new",
        )
        observed = WorkerPoolInfo(
            name="oke-rdma-2",
            kind="cluster-network",
            shape="BM.GPU4.8",
            cluster_network_id="cluster-network-new",
            instance_pool_id="instance-pool-new",
            desired_size=1,
            active_oci_instances=1,
            ready_k8s_nodes=1,
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
        )
        ready_node = NodeInfo(
            "rdma-new",
            pool_name="oke-rdma-2",
            shape="BM.GPU4.8",
            ready=True,
            allocatable={"nvidia.com/gpu": "8"},
            labels={
                "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
                "oci.oraclecloud.com/rdma.network_block_id": "block-1",
                "oci.oraclecloud.com/rdma.local_block_id": "local-1",
            },
        )
        service = _service(DiscoverySnapshot(pools=[source]))
        service.discover.side_effect = [
            DiscoverySnapshot(),
            DiscoverySnapshot(pools=[observed], nodes=[ready_node]),
        ]
        service.oci_backend.return_value.get_work_request_status.return_value = (
            WorkRequestInfo("work-request-new", "SUCCEEDED")
        )

        with patch("oke_hpc_mgmt.workflows.lifecycle.time.sleep") as sleep:
            result = wait_for_pool_creation(
                service,
                "oke-rdma-2",
                1,
                created,
                timeout_seconds=10,
                poll_interval_seconds=1,
            )

        self.assertEqual(observed, result)
        sleep.assert_called_once_with(1)

    def test_prepare_pool_resize_builds_auditable_plan(self):
        pool = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=2,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))

        prepared = prepare_pool_resize(service, "oke-cpu", delta=1)

        self.assertEqual(3, prepared.plan.target_size)
        self.assertEqual("oke", prepared.plan.owner)
        self.assertIn(IAC_DRIFT_WARNING, prepared.plan.warnings)
        service.resolve_oci_target.assert_called_once_with(require_compartment=True)

    def test_prepare_pool_resize_rejects_invalid_ownership_and_selection(self):
        autoscaled = WorkerPoolInfo(
            name="autoscaled",
            kind="node-pool",
            desired_size=2,
            autoscaler_owned=True,
        )
        service = _service(DiscoverySnapshot(pools=[autoscaled]))

        with self.assertRaises(WorkflowError):
            prepare_pool_resize(service, "autoscaled", delta=-1)
        with self.assertRaises(WorkflowError):
            prepare_pool_resize(service, "autoscaled", size=1, delta=-1)
        with self.assertRaises(WorkflowNotFound):
            prepare_pool_resize(service, "missing", delta=1)

    def test_prepare_pool_resize_protects_slinky_scale_down(self):
        pool = WorkerPoolInfo(
            name="slurm",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=2,
            slinky_managed=True,
        )

        with self.assertRaisesRegex(WorkflowError, "Slurm-aware drain"):
            prepare_pool_resize(_service(DiscoverySnapshot(pools=[pool])), "slurm", delta=-1)

    def test_execute_compute_cluster_managed_pool_uses_oke_api(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            placement_type="compute-cluster",
            compute_cluster_id="compute-cluster-1",
            node_pool_id="node-pool-1",
            desired_size=2,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        backend = service.oci_backend.return_value
        backend.resize_managed_node_pool.return_value = "work-request-1"
        prepared = prepare_pool_resize(service, "oke-rdma", delta=1)

        result = execute_pool_resize(service, prepared, lock=False)

        backend.resize_managed_node_pool.assert_called_once_with("node-pool-1", 3)
        backend.resize_cluster_network.assert_not_called()
        self.assertEqual("submitted", result["status"])

    def test_execute_legacy_cluster_network_pool_uses_cluster_network_api(self):
        pool = WorkerPoolInfo(
            name="legacy-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            desired_size=2,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        prepared = prepare_pool_resize(service, "legacy-rdma", delta=1)

        execute_pool_resize(service, prepared, lock=False)

        service.oci_backend.return_value.resize_cluster_network.assert_called_once_with(
            "cluster-network-1", "instance-pool-1", 3
        )

    def test_unchanged_resize_does_not_call_oci(self):
        pool = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=2,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        prepared = prepare_pool_resize(service, "oke-cpu", size=2)

        result = execute_pool_resize(service, prepared, lock=False)

        self.assertEqual("unchanged", result["status"])
        service.oci_backend.assert_not_called()

    def test_unchanged_resize_can_wait_without_a_work_request(self):
        pool = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=2,
            active_oci_instances=2,
            ready_k8s_nodes=2,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        prepared = prepare_pool_resize(service, "oke-cpu", size=2)

        result = execute_pool_resize(service, prepared, wait=True, lock=False)

        self.assertEqual("ready", result["status"])
        service.oci_backend.assert_not_called()

    def test_resize_wait_monitors_submitted_work_request(self):
        pool = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=2,
            active_oci_instances=2,
            ready_k8s_nodes=2,
        )
        observed = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=3,
            active_oci_instances=3,
            ready_k8s_nodes=3,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        backend = service.oci_backend.return_value
        backend.resize_managed_node_pool.return_value = "work-request-1"
        backend.get_work_request_status.return_value = WorkRequestInfo(
            "work-request-1", "SUCCEEDED", percent_complete=100
        )
        prepared = prepare_pool_resize(service, "oke-cpu", delta=1)
        service.discover.return_value = DiscoverySnapshot(pools=[observed])

        result = execute_pool_resize(service, prepared, wait=True, lock=False)

        self.assertEqual("ready", result["status"])
        backend.get_work_request_status.assert_called_once_with(
            "work-request-1",
            compartment_id="compartment-1",
        )

    def test_legacy_resize_wait_ignores_old_resource_work_request_failures(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            desired_size=2,
            active_oci_instances=2,
            ready_k8s_nodes=2,
        )
        observed = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            desired_size=3,
            active_oci_instances=3,
            ready_k8s_nodes=3,
        )
        old_failure = WorkRequestInfo("old-request", "FAILED")
        service = _service(DiscoverySnapshot(pools=[pool]))
        backend = service.oci_backend.return_value
        backend.resize_cluster_network.return_value = None
        backend.list_resource_work_requests.side_effect = [
            [old_failure],
            [old_failure],
        ]
        prepared = prepare_pool_resize(service, "oke-rdma", delta=1)
        service.discover.return_value = DiscoverySnapshot(pools=[observed])

        result = execute_pool_resize(service, prepared, wait=True, lock=False)

        self.assertEqual("ready", result["status"])
        backend.get_work_request_status.assert_not_called()

    def test_legacy_resize_wait_detects_new_resource_work_request_failure(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            desired_size=2,
            active_oci_instances=2,
            ready_k8s_nodes=2,
        )
        old_failure = WorkRequestInfo("old-request", "FAILED")
        new_failure = WorkRequestInfo("new-request", "FAILED", percent_complete=60)
        service = _service(DiscoverySnapshot(pools=[pool]))
        backend = service.oci_backend.return_value
        backend.resize_cluster_network.return_value = None
        backend.list_resource_work_requests.side_effect = [
            [old_failure],
            [new_failure, old_failure],
        ]
        backend.get_work_request_status.return_value = WorkRequestInfo(
            "new-request",
            "FAILED",
            percent_complete=60,
            errors=("1611: Insufficient capacity",),
        )
        prepared = prepare_pool_resize(service, "oke-rdma", delta=1)

        with self.assertRaisesRegex(WorkflowError, "Insufficient capacity"):
            execute_pool_resize(service, prepared, wait=True, lock=False)

        backend.get_work_request_status.assert_called_once_with(
            "new-request",
            compartment_id="compartment-1",
        )
        self.assertEqual(1, service.discover.call_count)

    def test_prepare_multiple_node_removal_calculates_pool_target_once(self):
        pool = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=3,
        )
        nodes = [
            NodeInfo(k8s_name=f"cpu-{index}", instance_ocid=f"instance-{index}", pool_name="oke-cpu")
            for index in (1, 2)
        ]
        service = _service(DiscoverySnapshot(pools=[pool], nodes=nodes))

        prepared = prepare_node_removal(
            service,
            identifiers=("cpu-1", "cpu-2"),
            drain=False,
        )

        self.assertEqual({"oke-cpu": 1}, prepared.target_sizes)
        self.assertEqual(2, len(prepared.plans))
        self.assertTrue(all(plan.decrement_size for plan in prepared.plans))

    def test_prepare_node_removal_validates_workloads_emptydir_and_unmanaged_pods(self):
        pool = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        node = NodeInfo(
            k8s_name="cpu-1",
            instance_ocid="instance-1",
            pool_name="oke-cpu",
            running_workload_pods=1,
        )
        snapshot = DiscoverySnapshot(pools=[pool], nodes=[node])
        with self.assertRaisesRegex(WorkflowError, "--drain or --allow-workloads"):
            prepare_node_removal(
                _service(snapshot),
                identifiers=("cpu-1",),
                drain=False,
            )

        service = _service(snapshot)
        service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod("default", "scratch", controller="Job/job", has_empty_dir=True),
        ]
        with self.assertRaisesRegex(WorkflowError, "--delete-emptydir-data"):
            prepare_node_removal(service, identifiers=("cpu-1",), drain=True)

        service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod("default", "standalone"),
        ]
        with self.assertRaisesRegex(WorkflowError, "--force"):
            prepare_node_removal(service, identifiers=("cpu-1",), drain=True)

    def test_execute_node_removal_routes_managed_and_legacy_workers(self):
        managed = WorkerPoolInfo(
            name="managed",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        legacy = WorkerPoolInfo(
            name="legacy",
            kind="instance-pool",
            instance_pool_id="instance-pool-1",
            desired_size=1,
        )
        managed_node = NodeInfo(
            k8s_name="managed-1", instance_ocid="instance-1", pool_name="managed"
        )
        legacy_node = NodeInfo(
            k8s_name="legacy-1", instance_ocid="instance-2", pool_name="legacy"
        )
        service = _service(
            DiscoverySnapshot(pools=[managed, legacy], nodes=[managed_node, legacy_node])
        )
        prepared = prepare_node_removal(
            service,
            identifiers=("managed-1", "legacy-1"),
            drain=False,
        )

        results = execute_node_removal(service, prepared, drain=False, lock=False)

        backend = service.oci_backend.return_value
        backend.delete_node.assert_called_once_with(
            "node-pool-1",
            "instance-1",
            decrement_size=True,
            override_eviction_grace_duration="PT10M",
            force_after_grace=False,
        )
        backend.detach_instance_pool_node.assert_called_once_with(
            "instance-pool-1", "instance-2", decrement_size=True
        )
        self.assertEqual(2, len(results))

    def test_legacy_node_removal_wait_detects_resource_work_request_failure(self):
        pool = WorkerPoolInfo(
            name="legacy",
            kind="instance-pool",
            instance_pool_id="instance-pool-1",
            desired_size=1,
        )
        node = NodeInfo(
            k8s_name="legacy-1",
            instance_ocid="instance-1",
            pool_name="legacy",
        )
        service = _service(DiscoverySnapshot(pools=[pool], nodes=[node]))
        backend = service.oci_backend.return_value
        backend.detach_instance_pool_node.return_value = None
        backend.list_resource_work_requests.side_effect = [
            [],
            [WorkRequestInfo("new-request", "FAILED")],
        ]
        backend.get_work_request_status.return_value = WorkRequestInfo(
            "new-request",
            "FAILED",
            errors=("ServiceError: detach failed",),
        )
        prepared = prepare_node_removal(
            service,
            identifiers=("legacy-1",),
            drain=False,
        )

        with self.assertRaisesRegex(WorkflowError, "detach failed"):
            execute_node_removal(
                service,
                prepared,
                drain=False,
                wait=True,
                lock=False,
            )

        backend.get_work_request_status.assert_called_once_with(
            "new-request",
            compartment_id="compartment-1",
        )
        self.assertEqual(1, service.discover.call_count)

    def test_slinky_node_removal_is_refused(self):
        pool = WorkerPoolInfo(
            name="slurm",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
            slinky_managed=True,
        )
        node = NodeInfo(
            k8s_name="slurm-1",
            instance_ocid="instance-1",
            pool_name="slurm",
            slinky_workload_pods=1,
        )

        with self.assertRaisesRegex(WorkflowError, "Slurm-aware drain"):
            prepare_node_removal(
                _service(DiscoverySnapshot(pools=[pool], nodes=[node])),
                identifiers=("slurm-1",),
                drain=False,
                allow_workloads=True,
            )

    def test_readiness_helpers_include_rdma_vfs(self):
        ready = PoolResourceReadiness(2, 2, 2)
        pending = PoolResourceReadiness(2, 2, 1)

        self.assertIn("rdma_vf_ready=2", readiness_status(ready))
        self.assertTrue(resource_counts_match(ready, 2))
        self.assertFalse(resource_counts_match(pending, 2))

    def test_wait_fails_immediately_when_oci_work_request_fails(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            desired_size=2,
            active_oci_instances=2,
            ready_k8s_nodes=2,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        service.oci_backend.return_value.get_work_request_status.return_value = (
            WorkRequestInfo(
                "work-request-1",
                "FAILED",
                percent_complete=60,
                errors=("1611: Insufficient capacity",),
            )
        )

        with self.assertRaisesRegex(WorkflowError, "Insufficient capacity"):
            wait_for_pool_size(
                service,
                "oke-rdma",
                3,
                timeout_seconds=60,
                poll_interval_seconds=1,
                work_request_id="work-request-1",
            )

        service.discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
