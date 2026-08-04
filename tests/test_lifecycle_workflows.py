from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oke_hpc_mgmt.backends.oci import (
    BootVolumeAttachmentPending,
    OciDiscoveryError,
)
from oke_hpc_mgmt.models import (
    AddonInfo,
    ClusterNetworkCreateResult,
    ComputeClusterInfo,
    DiscoverySnapshot,
    DrainPod,
    ManagedNodePoolCreateResult,
    NodeInfo,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
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
    execute_node_boot_volume_replace,
    execute_node_removal,
    execute_pool_boot_volume_replace,
    execute_pool_create,
    execute_pool_delete,
    execute_pool_resize,
    prepare_node_boot_volume_replace,
    prepare_node_removal,
    prepare_pool_boot_volume_replace,
    prepare_pool_create,
    prepare_pool_delete,
    prepare_pool_resize,
    _try_get_instance_boot_volume_id,
    readiness_status,
    resource_counts_match,
    wait_for_pool_creation,
    wait_for_pool_deleted,
    wait_for_node_boot_volume_replace,
    wait_for_compute_cluster_active,
    wait_for_pool_boot_volume_replace,
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
        compartment_id="compartment-1",
        cluster_id="cluster-1",
    )
    service.oci_backend.return_value.preview_cluster_network_pool_create.return_value = {
        "backend": "cluster-network"
    }
    service.oci_backend.return_value.preview_managed_node_pool_create.return_value = {
        "backend": "oke-node-pool"
    }
    service.oci_backend.return_value.get_cluster_type.return_value = (
        "ENHANCED_CLUSTER"
    )
    service.oci_backend.return_value.get_instance_boot_volume_id.side_effect = (
        lambda instance_id: f"boot-{instance_id}"
    )
    service.oci_backend.return_value.preview_managed_pool_boot_volume_replace.return_value = {
        "current": {"image_id": "image-old"},
        "effective": {"image_id": "image-new"},
    }
    service.kubernetes_backend.return_value.list_drain_pods.return_value = []
    return service


def _bvr_snapshot(
    *,
    kind: str = "node-pool",
    pool_name: str = "oke-gpu",
    instance_id: str = "instance-1",
    boot_id: str = "boot-session-old",
) -> tuple[DiscoverySnapshot, WorkerPoolInfo, NodeInfo]:
    pool = WorkerPoolInfo(
        name=pool_name,
        kind=kind,
        shape="VM.GPU.A10.1",
        desired_size=1,
        active_oci_instances=1,
        ready_k8s_nodes=1,
        node_pool_id="node-pool-1" if kind == "node-pool" else None,
        cluster_network_id=(
            "cluster-network-1" if kind == "cluster-network" else None
        ),
        instance_pool_id=(
            "instance-pool-1" if kind != "node-pool" else None
        ),
        oci_instance_ids={instance_id},
        gpu_resource="nvidia.com/gpu",
        rdma_enabled=kind != "node-pool",
    )
    labels = {}
    shape = "VM.GPU.A10.1"
    allocatable = {"nvidia.com/gpu": "1"}
    if kind != "node-pool":
        shape = "BM.GPU4.8"
        pool.shape = shape
        labels = {
            "oci.oraclecloud.com/rdma.hpc_island_id": "island-1",
            "oci.oraclecloud.com/rdma.network_block_id": "block-1",
            "oci.oraclecloud.com/rdma.local_block_id": "local-1",
        }
        allocatable["nvidia.com/gpu"] = "8"
    node = NodeInfo(
        k8s_name=f"{pool_name}-node-1",
        internal_ip="10.0.0.10",
        instance_ocid=instance_id,
        pool_name=pool_name,
        node_pool_id=pool.node_pool_id,
        shape=shape,
        ready=True,
        schedulable=True,
        allocatable=allocatable,
        labels=labels,
        boot_id=boot_id,
    )
    return DiscoverySnapshot(pools=[pool], nodes=[node]), pool, node


class LifecycleWorkflowTests(unittest.TestCase):
    def test_bvr_wait_retries_only_transient_attachment_gap(self):
        backend = Mock()
        backend.get_instance_boot_volume_id.side_effect = (
            BootVolumeAttachmentPending("attachment is changing")
        )
        self.assertIsNone(
            _try_get_instance_boot_volume_id(backend, "instance-1")
        )

        backend.get_instance_boot_volume_id.side_effect = OciDiscoveryError(
            "not authorized"
        )
        with self.assertRaisesRegex(OciDiscoveryError, "not authorized"):
            _try_get_instance_boot_volume_id(backend, "instance-1")

    def test_prepare_individual_bvr_supports_managed_and_self_managed_nodes(self):
        for kind in ("node-pool", "cluster-network"):
            with self.subTest(kind=kind):
                snapshot, pool, node = _bvr_snapshot(kind=kind)
                service = _service(snapshot)
                service.kubernetes_backend.return_value.list_drain_pods.return_value = []

                prepared = prepare_node_boot_volume_replace(
                    service,
                    identifiers=(node.k8s_name,),
                    eviction_grace_duration="pt30m",
                )

                self.assertEqual(
                    "node-boot-volume-replace",
                    prepared.plans[0].operation,
                )
                self.assertEqual("PT30M", prepared.eviction_grace_duration)
                self.assertTrue(
                    prepared.plans[0].details[
                        "preserves_existing_configuration"
                    ]
                )
                self.assertEqual(
                    f"boot-{node.instance_ocid}",
                    prepared.old_boot_volume_ids[node.instance_ocid],
                )
                self.assertEqual(pool.name, prepared.pools[pool.name].name)

    def test_prepare_bvr_requires_enhanced_healthy_unowned_pool(self):
        snapshot, pool, node = _bvr_snapshot()
        service = _service(snapshot)
        service.oci_backend.return_value.get_cluster_type.return_value = (
            "BASIC_CLUSTER"
        )
        with self.assertRaisesRegex(WorkflowError, "enhanced cluster"):
            prepare_node_boot_volume_replace(
                service,
                identifiers=(node.k8s_name,),
            )

        service = _service(snapshot)
        pool.autoscaler_owned = True
        with self.assertRaisesRegex(WorkflowError, "autoscaler-owned"):
            prepare_node_boot_volume_replace(
                service,
                identifiers=(node.k8s_name,),
            )

        snapshot, pool, node = _bvr_snapshot(pool_name="oke-system")
        service = _service(snapshot)
        with self.assertRaisesRegex(WorkflowError, "system pool"):
            prepare_node_boot_volume_replace(
                service,
                identifiers=(node.k8s_name,),
            )

    def test_individual_bvr_allows_not_ready_node_as_repair(self):
        snapshot, pool, node = _bvr_snapshot()
        node.ready = False
        node.schedulable = False
        pool.ready_k8s_nodes = 0
        service = _service(snapshot)

        prepared = prepare_node_boot_volume_replace(
            service,
            identifiers=(node.k8s_name,),
        )

        self.assertTrue(
            any(
                "treated as a repair" in warning
                for warning in prepared.plans[0].warnings
            )
        )

    def test_execute_individual_bvr_submits_and_waits_sequentially(self):
        snapshot, pool, node = _bvr_snapshot()
        service = _service(snapshot)
        service.kubernetes_backend.return_value.list_drain_pods.return_value = []
        prepared = prepare_node_boot_volume_replace(
            service,
            identifiers=(node.k8s_name,),
        )
        service.oci_backend.return_value.replace_cluster_node_boot_volume.return_value = (
            "work-request-1"
        )

        with patch(
            "oke_hpc_mgmt.workflows.lifecycle.wait_for_node_boot_volume_replace",
            return_value=(node, pool, "boot-volume-new"),
        ) as waiter:
            results = execute_node_boot_volume_replace(
                service,
                prepared,
                wait=True,
                lock=False,
            )

        service.oci_backend.return_value.replace_cluster_node_boot_volume.assert_called_once_with(
            "cluster-1",
            "instance-1",
            eviction_grace_duration="PT60M",
            force_after_grace=False,
        )
        waiter.assert_called_once()
        self.assertEqual("ready", results[0]["status"])
        self.assertEqual("boot-volume-new", results[0]["new_boot_volume_id"])
        self.assertTrue(results[0]["same_instance"])

    def test_multiple_individual_bvr_requires_wait(self):
        snapshot, pool, first = _bvr_snapshot()
        second = NodeInfo(
            k8s_name="oke-gpu-node-2",
            internal_ip="10.0.0.11",
            instance_ocid="instance-2",
            pool_name=pool.name,
            node_pool_id=pool.node_pool_id,
            shape=pool.shape,
            ready=True,
            schedulable=True,
            allocatable={"nvidia.com/gpu": "1"},
            boot_id="boot-session-2",
        )
        snapshot.nodes.append(second)
        pool.desired_size = 2
        pool.active_oci_instances = 2
        pool.ready_k8s_nodes = 2
        pool.oci_instance_ids.add("instance-2")
        service = _service(snapshot)
        service.kubernetes_backend.return_value.list_drain_pods.return_value = []
        prepared = prepare_node_boot_volume_replace(
            service,
            identifiers=(first.k8s_name, second.k8s_name),
        )

        with self.assertRaisesRegex(WorkflowError, "require --wait"):
            execute_node_boot_volume_replace(
                service,
                prepared,
                wait=False,
                lock=False,
            )

    def test_managed_pool_bvr_supports_image_change_and_refuses_self_managed(self):
        snapshot, pool, _node = _bvr_snapshot()
        service = _service(snapshot)
        spec = PoolBootVolumeReplaceSpec(image_id="image-new")

        prepared = prepare_pool_boot_volume_replace(
            service,
            pool.name,
            spec,
        )

        self.assertEqual("pool-boot-volume-replace", prepared.plan.operation)
        self.assertEqual("image-new", prepared.plan.details["updates"]["image_id"])

        rdma_snapshot, rdma_pool, _rdma_node = _bvr_snapshot(
            kind="cluster-network"
        )
        with self.assertRaisesRegex(WorkflowError, "managed OKE node pools"):
            prepare_pool_boot_volume_replace(
                _service(rdma_snapshot),
                rdma_pool.name,
                spec,
            )

    def test_managed_pool_bvr_requires_pod_data_acknowledgements(self):
        snapshot, pool, _node = _bvr_snapshot()
        service = _service(snapshot)
        service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod(
                "training",
                "checkpoint-writer",
                controller="Job/job-1",
                has_empty_dir=True,
            ),
            DrainPod("training", "manual-debugger"),
        ]
        spec = PoolBootVolumeReplaceSpec(image_id="image-new")

        with self.assertRaisesRegex(WorkflowError, "emptyDir"):
            prepare_pool_boot_volume_replace(
                service,
                pool.name,
                spec,
            )
        with self.assertRaisesRegex(WorkflowError, "unmanaged pods"):
            prepare_pool_boot_volume_replace(
                service,
                pool.name,
                spec,
                delete_emptydir_data=True,
            )

        prepared = prepare_pool_boot_volume_replace(
            service,
            pool.name,
            spec,
            delete_emptydir_data=True,
            force_unmanaged=True,
        )
        self.assertEqual(2, len(prepared.drain_pods["oke-gpu-node-1"]))

        pool.ready_k8s_nodes = 0
        with self.assertRaisesRegex(WorkflowError, "fully Ready"):
            prepare_pool_boot_volume_replace(
                service,
                pool.name,
                spec,
                delete_emptydir_data=True,
                force_unmanaged=True,
            )

    def test_execute_managed_pool_bvr_submits_and_waits(self):
        snapshot, pool, _node = _bvr_snapshot()
        service = _service(snapshot)
        prepared = prepare_pool_boot_volume_replace(
            service,
            pool.name,
            PoolBootVolumeReplaceSpec(image_id="image-new"),
        )
        service.oci_backend.return_value.replace_managed_pool_boot_volumes.return_value = (
            "work-request-pool"
        )

        with patch(
            "oke_hpc_mgmt.workflows.lifecycle.wait_for_pool_boot_volume_replace",
            return_value=(pool, {"instance-1": "boot-volume-new"}),
        ) as waiter:
            result = execute_pool_boot_volume_replace(
                service,
                prepared,
                wait=True,
                lock=False,
            )

        service.oci_backend.return_value.replace_managed_pool_boot_volumes.assert_called_once_with(
            "node-pool-1",
            prepared.spec,
        )
        waiter.assert_called_once()
        self.assertEqual("ready", result["status"])
        self.assertEqual(1, result["replaced_nodes"])

    def test_bvr_waiters_verify_boot_and_node_identity(self):
        old_snapshot, pool, old_node = _bvr_snapshot()
        new_snapshot, new_pool, new_node = _bvr_snapshot(
            boot_id="boot-session-new"
        )
        service = _service(new_snapshot)
        service.oci_backend.return_value.get_instance_boot_volume_id.side_effect = (
            None
        )
        service.oci_backend.return_value.get_instance_boot_volume_id.return_value = (
            "boot-volume-new"
        )
        service.oci_backend.return_value.managed_pool_boot_volume_replace_applied.return_value = (
            True
        )

        observed_node, observed_pool, boot_volume_id = (
            wait_for_node_boot_volume_replace(
                service,
                old_node,
                pool,
                "boot-volume-old",
                None,
                timeout_seconds=1,
                poll_interval_seconds=1,
            )
        )
        self.assertEqual(new_node.boot_id, observed_node.boot_id)
        self.assertEqual(new_pool.name, observed_pool.name)
        self.assertEqual("boot-volume-new", boot_volume_id)

        prepared = prepare_pool_boot_volume_replace(
            _service(old_snapshot),
            pool.name,
            PoolBootVolumeReplaceSpec(image_id="image-new"),
        )
        observed_pool, boot_volume_ids = wait_for_pool_boot_volume_replace(
            service,
            prepared,
            None,
            timeout_seconds=1,
            poll_interval_seconds=1,
        )
        self.assertEqual(new_pool.name, observed_pool.name)
        self.assertEqual(
            {"instance-1": "boot-volume-new"},
            boot_volume_ids,
        )

    def test_prepare_pool_delete_protects_system_autoscaler_and_slinky_pools(self):
        system = WorkerPoolInfo(
            name="oke-system",
            kind="node-pool",
            node_pool_id="node-pool-system",
            desired_size=2,
        )
        with self.assertRaisesRegex(WorkflowError, "system pool"):
            prepare_pool_delete(
                _service(DiscoverySnapshot(pools=[system])),
                "oke-system",
                drain=False,
            )

        autoscaled = WorkerPoolInfo(
            name="autoscaled",
            kind="node-pool",
            node_pool_id="node-pool-autoscaled",
            autoscaler_owned=True,
        )
        with self.assertRaisesRegex(WorkflowError, "autoscaler-owned"):
            prepare_pool_delete(
                _service(DiscoverySnapshot(pools=[autoscaled])),
                "autoscaled",
                drain=False,
            )

        slinky = WorkerPoolInfo(
            name="slurm-workers",
            kind="node-pool",
            node_pool_id="node-pool-slinky",
            slinky_managed=True,
        )
        with self.assertRaisesRegex(WorkflowError, "Slurm-aware drain"):
            prepare_pool_delete(
                _service(DiscoverySnapshot(pools=[slinky])),
                "slurm-workers",
                drain=False,
            )

    def test_execute_pool_delete_routes_managed_and_cluster_network_pools(self):
        managed = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=0,
        )
        managed_service = _service(DiscoverySnapshot(pools=[managed]))
        managed_prepared = prepare_pool_delete(
            managed_service,
            "cpu-batch",
            drain=False,
        )

        managed_result = execute_pool_delete(
            managed_service,
            managed_prepared,
            drain=False,
            lock=False,
        )

        managed_service.oci_backend.return_value.delete_managed_node_pool.assert_called_once_with(
            "node-pool-1"
        )
        self.assertEqual("submitted", managed_result["status"])

        rdma = WorkerPoolInfo(
            name="rdma-batch",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            instance_configuration_id="instance-configuration-1",
            created_by_mgmt_oke=True,
            desired_size=0,
        )
        rdma_service = _service(DiscoverySnapshot(pools=[rdma]))
        rdma_prepared = prepare_pool_delete(
            rdma_service,
            "rdma-batch",
            drain=False,
        )

        rdma_result = execute_pool_delete(
            rdma_service,
            rdma_prepared,
            drain=False,
            lock=False,
        )

        rdma_service.oci_backend.return_value.terminate_cluster_network.assert_called_once_with(
            "cluster-network-1"
        )
        rdma_service.oci_backend.return_value.delete_mgmt_created_instance_configuration.assert_not_called()
        self.assertEqual(
            "retained",
            rdma_result["instance_configuration_status"],
        )

        rdma_wait_service = _service(DiscoverySnapshot(pools=[rdma]))
        rdma_wait_prepared = prepare_pool_delete(
            rdma_wait_service,
            "rdma-batch",
            drain=False,
        )
        with patch(
            "oke_hpc_mgmt.workflows.lifecycle.wait_for_pool_deleted"
        ) as wait_for_delete:
            rdma_wait_result = execute_pool_delete(
                rdma_wait_service,
                rdma_wait_prepared,
                drain=False,
                wait=True,
                lock=False,
            )

        wait_for_delete.assert_called_once()
        rdma_wait_service.oci_backend.return_value.delete_mgmt_created_instance_configuration.assert_called_once_with(
            "instance-configuration-1"
        )
        self.assertEqual(
            "deleted",
            rdma_wait_result["instance_configuration_status"],
        )

        standalone = WorkerPoolInfo(
            name="standalone-batch",
            kind="instance-pool",
            instance_pool_id="instance-pool-2",
            desired_size=0,
        )
        standalone_service = _service(DiscoverySnapshot(pools=[standalone]))
        standalone_prepared = prepare_pool_delete(
            standalone_service,
            "standalone-batch",
            drain=False,
        )

        execute_pool_delete(
            standalone_service,
            standalone_prepared,
            drain=False,
            lock=False,
        )

        standalone_service.oci_backend.return_value.terminate_instance_pool.assert_called_once_with(
            "instance-pool-2"
        )

    def test_prepare_pool_delete_validates_workloads_and_drain_data(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        node = NodeInfo(
            "cpu-1",
            pool_name="cpu-batch",
            node_pool_id="node-pool-1",
            running_workload_pods=1,
        )
        snapshot = DiscoverySnapshot(pools=[pool], nodes=[node])

        with self.assertRaisesRegex(WorkflowError, "without drain"):
            prepare_pool_delete(
                _service(snapshot),
                "cpu-batch",
                drain=False,
            )

        no_drain = prepare_pool_delete(
            _service(snapshot),
            "cpu-batch",
            drain=False,
            allow_workloads=True,
        )
        self.assertEqual(1, no_drain.plan.workload_pods)

        service = _service(snapshot)
        service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod(
                "default",
                "scratch",
                controller="Job/scratch",
                has_empty_dir=True,
            )
        ]
        with self.assertRaisesRegex(WorkflowError, "emptyDir"):
            prepare_pool_delete(service, "cpu-batch")

        prepared = prepare_pool_delete(
            service,
            "cpu-batch",
            delete_emptydir_data=True,
        )
        self.assertTrue(prepared.delete_emptydir_data)
        self.assertFalse(prepared.force_unmanaged)

        service.kubernetes_backend.return_value.list_drain_pods.return_value = [
            DrainPod(
                "default",
                "pdb-blocked",
                controller="Deployment/pdb-blocked",
                eviction_blocker="PodDisruptionBudget denied eviction",
            )
        ]
        blocked = prepare_pool_delete(service, "cpu-batch")
        self.assertIn("pdb-blocked", " ".join(blocked.plan.warnings))

    def test_prepare_pool_delete_requires_explicit_kubernetes_bypass(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        snapshot = DiscoverySnapshot(
            pools=[pool],
            warnings=["Kubernetes discovery skipped: unavailable"],
        )

        with self.assertRaisesRegex(WorkflowError, "successful Kubernetes discovery"):
            prepare_pool_delete(_service(snapshot), "cpu-batch")
        with self.assertRaisesRegex(WorkflowError, "workload presence cannot be verified"):
            prepare_pool_delete(
                _service(snapshot),
                "cpu-batch",
                drain=False,
            )

        prepared = prepare_pool_delete(
            _service(snapshot),
            "cpu-batch",
            drain=False,
            allow_workloads=True,
        )
        self.assertTrue(prepared.allow_workloads)

    def test_execute_pool_delete_revalidates_drain_after_cordon(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        node = NodeInfo(
            "cpu-1",
            pool_name="cpu-batch",
            node_pool_id="node-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[pool], nodes=[node]))
        kubernetes = service.kubernetes_backend.return_value
        kubernetes.list_drain_pods.side_effect = [
            [],
            [
                DrainPod(
                    "default",
                    "late-scratch",
                    controller="Job/late-scratch",
                    has_empty_dir=True,
                )
            ],
        ]
        prepared = prepare_pool_delete(service, "cpu-batch")

        with self.assertRaisesRegex(WorkflowError, "late-scratch"):
            execute_pool_delete(service, prepared, lock=False)

        kubernetes.cordon_node.assert_called_once_with("cpu-1")
        kubernetes.uncordon_node.assert_called_once_with("cpu-1")
        kubernetes.evict_drain_pods.assert_not_called()
        service.oci_backend.return_value.delete_managed_node_pool.assert_not_called()

    def test_execute_pool_delete_refuses_membership_change(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        node = NodeInfo(
            "cpu-1",
            pool_name="cpu-batch",
            node_pool_id="node-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[pool], nodes=[node]))
        prepared = prepare_pool_delete(service, "cpu-batch", drain=False)
        replacement = NodeInfo(
            "cpu-2",
            pool_name="cpu-batch",
            node_pool_id="node-pool-1",
        )
        service.discover.return_value = DiscoverySnapshot(
            pools=[pool],
            nodes=[replacement],
        )

        with self.assertRaisesRegex(WorkflowError, "membership changed"):
            execute_pool_delete(
                service,
                prepared,
                drain=False,
                lock=False,
            )

        service.oci_backend.return_value.delete_managed_node_pool.assert_not_called()

    def test_execute_pool_delete_rechecks_no_drain_workloads(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        node = NodeInfo(
            "cpu-1",
            pool_name="cpu-batch",
            node_pool_id="node-pool-1",
        )
        service = _service(DiscoverySnapshot(pools=[pool], nodes=[node]))
        prepared = prepare_pool_delete(service, "cpu-batch", drain=False)
        busy_node = NodeInfo(
            "cpu-1",
            pool_name="cpu-batch",
            node_pool_id="node-pool-1",
            running_workload_pods=1,
        )
        service.discover.return_value = DiscoverySnapshot(
            pools=[pool],
            nodes=[busy_node],
        )

        with self.assertRaisesRegex(WorkflowError, "now running"):
            execute_pool_delete(
                service,
                prepared,
                drain=False,
                lock=False,
            )

        service.oci_backend.return_value.delete_managed_node_pool.assert_not_called()

    def test_wait_for_pool_deleted_handles_discovery_convergence(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        service = _service(DiscoverySnapshot(pools=[pool]))
        service.discover.side_effect = [
            DiscoverySnapshot(pools=[pool]),
            DiscoverySnapshot(),
        ]
        progress = Mock()

        with patch("oke_hpc_mgmt.workflows.lifecycle.time.sleep") as sleep:
            wait_for_pool_deleted(
                service,
                pool,
                None,
                timeout_seconds=10,
                poll_interval_seconds=1,
                progress=progress,
            )

        sleep.assert_called_once_with(1)
        self.assertEqual(2, progress.call_count)

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
        service.resolve_oci_target.assert_called_once_with(
            require_compartment=True,
            require_cluster=True,
        )
        service.oci_backend.return_value.preview_cluster_network_pool_create.assert_called_once_with(
            "cluster-network-1",
            "instance-pool-1",
            "oke-rdma-2",
            2,
            PoolCreateSpec(pool_type="rdma"),
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

    def test_prepare_pool_create_routes_managed_cpu_and_gpu_templates(self):
        cpu = WorkerPoolInfo(
            name="oke-cpu",
            kind="node-pool",
            node_pool_id="node-pool-cpu",
            shape="VM.Standard.E5.Flex",
        )
        gpu = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-gpu",
            shape="VM.GPU.A10.1",
            gpu_resource="nvidia.com/gpu",
        )
        service = _service(DiscoverySnapshot(pools=[gpu, cpu]))

        prepared_cpu = prepare_pool_create(
            service,
            "cpu-batch",
            2,
            spec=PoolCreateSpec(pool_type="cpu", image_id="image-cpu"),
        )
        prepared_gpu = prepare_pool_create(
            service,
            "gpu-batch",
            1,
            spec=PoolCreateSpec(pool_type="gpu", image_id="image-gpu"),
        )

        self.assertEqual(cpu, prepared_cpu.source_pool)
        self.assertEqual("oke", prepared_cpu.plan.owner)
        self.assertEqual(gpu, prepared_gpu.source_pool)
        self.assertEqual("oke", prepared_gpu.plan.owner)
        self.assertEqual(
            2,
            service.oci_backend.return_value
            .preview_managed_node_pool_create.call_count,
        )

    def test_prepare_pool_create_routes_managed_compute_cluster_rdma(self):
        rdma = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="node-pool-rdma",
            compute_cluster_id="compute-cluster-source",
            placement_type="compute-cluster",
            shape="BM.GPU4.8",
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
        )
        service = _service(DiscoverySnapshot(pools=[rdma]))
        service.oci_backend.return_value.preview_managed_node_pool_create.return_value = {
            "backend": "oke-node-pool",
            "placement": "compute-cluster",
            "availability_domains": ["AD-1"],
        }
        spec = PoolCreateSpec(
            pool_type="rdma",
            rdma_mode="compute-cluster",
            compute_cluster_id="compute-cluster-target",
            host_group_id="host-group-1",
        )

        prepared = prepare_pool_create(
            service,
            "rdma-batch",
            2,
            spec=spec,
        )

        self.assertEqual(rdma, prepared.source_pool)
        self.assertEqual("oke", prepared.plan.owner)
        warnings = " ".join(prepared.plan.warnings)
        self.assertIn("COMPUTE_CLUSTER_LAUNCH_INSTANCE", warnings)
        self.assertIn("HOST_GROUP_LAUNCH_INSTANCE", warnings)
        service.oci_backend.return_value.preview_managed_node_pool_create.assert_called_once_with(
            "node-pool-rdma",
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            2,
            spec,
        )

    def test_first_managed_rdma_pool_uses_regular_gpu_template(self):
        legacy_rdma = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            shape="BM.GPU4.8",
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
        )
        gpu = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-gpu",
            shape="VM.GPU.A10.1",
            gpu_resource="nvidia.com/gpu",
        )
        service = _service(
            DiscoverySnapshot(pools=[legacy_rdma, gpu])
        )
        service.oci_backend.return_value.preview_managed_node_pool_create.return_value = {
            "backend": "oke-node-pool",
            "placement": "compute-cluster",
            "availability_domains": ["AD-1"],
        }
        spec = PoolCreateSpec(
            pool_type="rdma",
            rdma_mode="compute-cluster",
            shape="BM.GPU4.8",
        )

        prepared = prepare_pool_create(
            service,
            "rdma-managed",
            1,
            spec=spec,
        )

        self.assertEqual(gpu, prepared.source_pool)
        service.oci_backend.return_value.preview_managed_node_pool_create.assert_called_once_with(
            "node-pool-gpu",
            "cluster-1",
            "compartment-1",
            "rdma-managed",
            1,
            spec,
        )

    def test_managed_rdma_prefers_existing_compute_cluster_template(self):
        gpu = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-gpu",
            gpu_resource="nvidia.com/gpu",
        )
        managed_rdma = WorkerPoolInfo(
            name="rdma-existing",
            kind="node-pool",
            node_pool_id="node-pool-rdma",
            compute_cluster_id="compute-cluster-existing",
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
        )
        service = _service(
            DiscoverySnapshot(pools=[gpu, managed_rdma])
        )

        prepared = prepare_pool_create(
            service,
            "rdma-managed-2",
            1,
            spec=PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                compute_cluster_id="compute-cluster-target",
            ),
        )

        self.assertEqual(managed_rdma, prepared.source_pool)

    def test_execute_managed_rdma_create_builds_compute_cluster_first(self):
        rdma = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="node-pool-rdma",
            compute_cluster_id="compute-cluster-source",
            placement_type="compute-cluster",
            shape="BM.GPU4.8",
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
        )
        service = _service(DiscoverySnapshot(pools=[rdma]))
        backend = service.oci_backend.return_value
        backend.preview_managed_node_pool_create.return_value = {
            "backend": "oke-node-pool",
            "placement": "compute-cluster",
            "availability_domains": ["AD-1"],
        }
        compute_cluster = ComputeClusterInfo(
            compute_cluster_id="compute-cluster-new",
            display_name="rdma-batch-cc",
            availability_domain="AD-1",
            compartment_id="compartment-1",
            lifecycle_state="ACTIVE",
        )
        backend.create_compute_cluster.return_value = compute_cluster
        backend.get_compute_cluster_info.return_value = compute_cluster
        backend.create_managed_node_pool.return_value = (
            ManagedNodePoolCreateResult(
                node_pool_id="node-pool-new",
                work_request_id="work-request-new",
                compute_cluster_id="compute-cluster-new",
            )
        )
        prepared = prepare_pool_create(
            service,
            "rdma-batch",
            2,
            spec=PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
            ),
        )

        result = execute_pool_create(service, prepared, lock=False)

        backend.create_compute_cluster.assert_called_once_with(
            compartment_id="compartment-1",
            availability_domain="AD-1",
            display_name="rdma-batch-cc",
            pool_name="rdma-batch",
            freeform_tags={},
        )
        runtime_spec = backend.create_managed_node_pool.call_args.args[-1]
        self.assertEqual("compute-cluster-new", runtime_spec.compute_cluster_id)
        self.assertFalse(runtime_spec.creates_compute_cluster)
        self.assertEqual("compute-cluster", result["placement"])
        self.assertTrue(result["compute_cluster_created"])

    def test_managed_rdma_submission_failure_retains_created_compute_cluster(self):
        gpu = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-gpu",
            gpu_resource="nvidia.com/gpu",
        )
        service = _service(DiscoverySnapshot(pools=[gpu]))
        backend = service.oci_backend.return_value
        backend.preview_managed_node_pool_create.return_value = {
            "backend": "oke-node-pool",
            "placement": "compute-cluster",
            "availability_domains": ["AD-1"],
        }
        compute_cluster = ComputeClusterInfo(
            compute_cluster_id="compute-cluster-new",
            display_name="rdma-batch-cc",
            availability_domain="AD-1",
            compartment_id="compartment-1",
            lifecycle_state="ACTIVE",
        )
        backend.create_compute_cluster.return_value = compute_cluster
        backend.get_compute_cluster_info.return_value = compute_cluster
        backend.create_managed_node_pool.side_effect = OciDiscoveryError(
            "request timed out"
        )
        prepared = prepare_pool_create(
            service,
            "rdma-batch",
            1,
            spec=PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                shape="BM.GPU4.8",
            ),
        )

        with self.assertRaisesRegex(
            WorkflowError,
            "compute-cluster-new is retained",
        ):
            execute_pool_create(service, prepared, lock=False)

    def test_managed_rdma_wait_requires_vfs_when_network_operator_is_active(self):
        gpu = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-gpu",
            gpu_resource="nvidia.com/gpu",
        )
        snapshot = DiscoverySnapshot(
            pools=[gpu],
            addons=[
                AddonInfo(
                    name="NvidiaNetworkOperator",
                    lifecycle_state="ACTIVE",
                )
            ],
        )
        service = _service(snapshot)
        backend = service.oci_backend.return_value
        backend.preview_managed_node_pool_create.return_value = {
            "backend": "oke-node-pool",
            "placement": "compute-cluster",
            "availability_domains": ["AD-1"],
        }
        compute_cluster = ComputeClusterInfo(
            compute_cluster_id="compute-cluster-new",
            display_name="rdma-batch-cc",
            availability_domain="AD-1",
            compartment_id="compartment-1",
            lifecycle_state="ACTIVE",
        )
        backend.create_compute_cluster.return_value = compute_cluster
        backend.get_compute_cluster_info.return_value = compute_cluster
        created = ManagedNodePoolCreateResult(
            node_pool_id="node-pool-new",
            work_request_id="work-request-new",
            compute_cluster_id="compute-cluster-new",
        )
        backend.create_managed_node_pool.return_value = created
        prepared = prepare_pool_create(
            service,
            "rdma-batch",
            1,
            spec=PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                shape="BM.GPU4.8",
            ),
        )

        with patch(
            "oke_hpc_mgmt.workflows.lifecycle.wait_for_pool_creation",
            return_value=WorkerPoolInfo(
                name="rdma-batch",
                kind="node-pool",
                desired_size=1,
                ready_k8s_nodes=1,
            ),
        ) as waiter:
            execute_pool_create(
                service,
                prepared,
                wait=True,
                lock=False,
            )

        self.assertTrue(waiter.call_args.kwargs["require_rdma_vf"])

    def test_wait_for_compute_cluster_requires_active_state(self):
        backend = Mock()
        backend.get_compute_cluster_info.side_effect = [
            ComputeClusterInfo(
                compute_cluster_id="compute-cluster-1",
                display_name="rdma-cc",
                availability_domain="AD-1",
                compartment_id="compartment-1",
                lifecycle_state="PROVISIONING",
            ),
            ComputeClusterInfo(
                compute_cluster_id="compute-cluster-1",
                display_name="rdma-cc",
                availability_domain="AD-1",
                compartment_id="compartment-1",
                lifecycle_state="ACTIVE",
            ),
        ]
        progress = Mock()

        with patch("oke_hpc_mgmt.workflows.lifecycle.time.sleep") as sleep:
            wait_for_compute_cluster_active(
                backend,
                "compute-cluster-1",
                timeout_seconds=10,
                poll_interval_seconds=1,
                progress=progress,
            )

        sleep.assert_called_once_with(1)
        self.assertEqual(2, progress.call_count)

    def test_managed_compute_cluster_rdma_pool_is_not_a_gpu_template(self):
        rdma = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="managed-rdma-1",
            gpu_resource="nvidia.com/gpu",
            rdma_enabled=True,
            placement_type="compute-cluster",
        )
        service = _service(DiscoverySnapshot(pools=[rdma]))

        with self.assertRaisesRegex(WorkflowError, "No eligible gpu pool"):
            prepare_pool_create(
                service,
                "gpu-new",
                1,
                spec=PoolCreateSpec(pool_type="gpu"),
            )

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

        with self.assertRaisesRegex(WorkflowError, "eligible rdma template"):
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
            PoolCreateSpec(pool_type="rdma"),
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

    def test_rdma_create_wait_failure_reports_created_resource_ids(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
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
        service.oci_backend.return_value.create_cluster_network_pool.return_value = (
            created
        )
        prepared = prepare_pool_create(service, "oke-rdma-2", 1)

        with (
            patch(
                "oke_hpc_mgmt.workflows.lifecycle.wait_for_pool_creation",
                side_effect=WorkflowError("Insufficient capacity"),
            ),
            self.assertRaisesRegex(
                WorkflowError,
                "cluster-network-new.*instance-configuration-new",
            ),
        ):
            execute_pool_create(
                service,
                prepared,
                wait=True,
                lock=False,
            )

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
