from __future__ import annotations

import inspect
import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from oke_hpc_mgmt.models import (
    AddonCompatibility,
    AddonInfo,
    ClusterNetworkCreateResult,
    ClusterInfo,
    DiscoverySnapshot,
    NodeInfo,
    ManagedNodePoolCreateResult,
    WorkerPoolInfo,
    WorkRequestInfo,
    VirtualNodePoolInfo,
)
from oke_hpc_mgmt.upgrades import (
    UpgradeCheckpoint,
    UpgradeGateEvidence,
    UpgradePhase,
)
from oke_hpc_mgmt.workflows import upgrades as upgrade_workflows
from oke_hpc_mgmt.workflows.lifecycle import WorkflowError
from oke_hpc_mgmt.workflows.upgrades import (
    PoolUpgradeSpec,
    PreparedPoolUpgrade,
    collect_upgrade_gate_evidence,
    execute_control_plane_upgrade,
    execute_pool_upgrade,
    prepare_cluster_upgrade_plan,
    prepare_control_plane_upgrade,
    prepare_pool_upgrade,
)


def _snapshot(
    *,
    control_version: str = "v1.35.2",
    pool_version: str = "v1.35.2",
    kubelet_version: str = "v1.35.2",
    cordoned: bool = True,
) -> DiscoverySnapshot:
    pool = WorkerPoolInfo(
        name="oke-cpu",
        kind="node-pool",
        shape="VM.Standard.E5.Flex",
        compartment_id="compartment-1",
        desired_size=1,
        active_oci_instances=1,
        ready_k8s_nodes=1,
        node_pool_id="node-pool-1",
        kubernetes_version=pool_version,
    )
    node = NodeInfo(
        k8s_name="cpu-1",
        instance_ocid="instance-1",
        pool_name=pool.name,
        ready=True,
        schedulable=not cordoned,
        kubelet_version=kubelet_version,
    )
    return DiscoverySnapshot(
        cluster=ClusterInfo(
            cluster_id="cluster-1",
            compartment_id="compartment-1",
            kubernetes_version=control_version,
            lifecycle_state="ACTIVE",
            cluster_type="ENHANCED_CLUSTER",
            available_kubernetes_versions=(
                control_version,
                "v1.36.1",
            ),
            etag="cluster-etag",
        ),
        pools=[pool],
        nodes=[node],
        addons=[
            AddonInfo(
                name="KubeProxy",
                lifecycle_state="ACTIVE",
                version="v1.35.2",
                update_mode="AUTOMATIC",
            )
        ],
    )


class _Kubernetes:
    def __init__(self) -> None:
        self.cordon_node = Mock()
        self.uncordon_node = Mock()
        self.evict_drain_pods = Mock()
        self.set_node_schedulable = Mock()
        self.list_upgrade_blocking_pods = Mock(return_value=[])
        self.cluster_connection_data = Mock(
            return_value=("10.0.0.1:6443", "certificate")
        )
        self.read_upgrade_checkpoint = Mock(return_value=None)
        self.write_upgrade_checkpoint = Mock(
            return_value="resource-version-next"
        )
        self.delete_upgrade_checkpoint = Mock()

    @contextmanager
    def mutation_lease(self, **kwargs):
        yield "holder"


class _Oci:
    def __init__(self, snapshot: DiscoverySnapshot) -> None:
        self.snapshot = snapshot
        self.preview_managed_pool_upgrade = Mock(
            return_value=(
                SimpleNamespace(kubernetes_version="v1.36.1"),
                "node-pool-etag",
                {"strategy": "boot-volume-replace"},
            )
        )
        self.upgrade_managed_pool = Mock(return_value="work-request-1")
        self.upgrade_control_plane = Mock(
            return_value="work-request-control"
        )
        self.create_upgrade_instance_configuration = Mock(
            return_value="config-new"
        )
        self.attach_upgrade_instance_configuration = Mock(
            return_value="work-request-attach"
        )
        self.replace_self_managed_instance_boot_volume = Mock()
        self.resize_upgrade_backend = Mock(
            return_value="work-request-resize"
        )
        self.detach_instance_pool_node = Mock(
            return_value="work-request-detach"
        )
        self.terminate_upgrade_instance = Mock()
        self.create_cluster_network_blue_green_pool = Mock(
            return_value=ClusterNetworkCreateResult(
                cluster_network_id="cluster-network-green",
                instance_configuration_id="config-green",
                instance_pool_id="instance-pool-green",
                work_request_id="work-request-green",
            )
        )
        self.create_instance_pool_blue_green = Mock(
            return_value=("instance-pool-green", "work-request-green")
        )
        self.create_gpu_memory_cluster_blue_green = Mock(
            return_value=("gmc-green", "work-request-green")
        )
        self.create_managed_blue_green_pool = Mock(
            return_value=ManagedNodePoolCreateResult(
                node_pool_id="node-pool-green",
                work_request_id="work-request-green",
            )
        )

    def get_addon_compatibility(self, target, installed):
        return [
            AddonCompatibility(
                name=addon.name,
                installed_version=addon.version,
                update_mode=addon.update_mode,
                supported_versions=(addon.version or "",),
                compatible=True,
            )
            for addon in installed
        ]

    def get_cluster_info(self, cluster_id, compartment_id):
        return self.snapshot.cluster

    def get_work_request_status(self, work_request_id, compartment_id):
        return WorkRequestInfo(work_request_id, "SUCCEEDED", 100.0)


class _Service:
    def __init__(self, snapshot: DiscoverySnapshot) -> None:
        self.snapshot = snapshot
        self.options = SimpleNamespace(
            skip_oci=False,
            skip_kubernetes=False,
            auth="instance_principal",
        )
        self.kubernetes = _Kubernetes()
        self.oci = _Oci(snapshot)

    def discover(self):
        return self.snapshot

    def kubernetes_backend(self):
        return self.kubernetes

    def oci_backend(self):
        return self.oci

    def resolve_oci_target(self, **kwargs):
        return SimpleNamespace(
            cluster_id="cluster-1",
            compartment_id="compartment-1",
        )


class UpgradeWorkflowTests(unittest.TestCase):
    def test_cluster_plan_orders_control_plane_before_cpu_pool(self):
        service = _Service(_snapshot())

        plan = prepare_cluster_upgrade_plan(service, "v1.36")

        self.assertEqual(("v1.36.1",), plan.control_plane_steps)
        self.assertEqual(("oke-cpu",), plan.pool_order)
        self.assertEqual(
            ["control-plane-upgrade", "worker-pool-upgrade"],
            [item.operation for item in plan.plans],
        )
        service.oci.preview_managed_pool_upgrade.assert_called_once()
        self.assertTrue(plan.plans[1].details["gate_cordoned"])

    def test_cluster_plan_blocks_incompatible_pinned_addon(self):
        service = _Service(_snapshot())
        service.oci.get_addon_compatibility = Mock(
            return_value=[
                AddonCompatibility(
                    name="NvidiaGpuOperator",
                    installed_version="old",
                    update_mode="MANUAL",
                    supported_versions=("new",),
                    compatible=False,
                    reason="pinned",
                )
            ]
        )

        with self.assertRaisesRegex(WorkflowError, "add-ons block"):
            prepare_cluster_upgrade_plan(service, "v1.36")

    def test_control_plane_plan_does_not_preflight_worker_mutation_backends(self):
        service = _Service(_snapshot())

        plan = prepare_control_plane_upgrade(service, "v1.36")

        self.assertEqual(("v1.36.1",), plan.control_plane_steps)
        self.assertEqual((), plan.pool_order)
        self.assertEqual(["control-plane-upgrade"], [
            item.operation for item in plan.plans
        ])
        service.oci.preview_managed_pool_upgrade.assert_not_called()

    def test_control_plane_plan_requires_all_workers_ready(self):
        snapshot = _snapshot()
        snapshot.nodes[0].ready = False
        service = _Service(snapshot)

        with self.assertRaisesRegex(WorkflowError, "not Ready: cpu-1"):
            prepare_control_plane_upgrade(service, "v1.36")

    def test_gate_collects_pods_without_any_scheduling_mutation(self):
        snapshot = _snapshot()
        service = _Service(snapshot)
        service.kubernetes.list_upgrade_blocking_pods.return_value = [
            SimpleNamespace(namespace="training", name="job")
        ]

        evidence = collect_upgrade_gate_evidence(
            service,
            snapshot,
            snapshot.pools[0],
        )

        self.assertEqual(("training/job",), evidence.active_pods)
        service.kubernetes.cordon_node.assert_not_called()
        service.kubernetes.evict_drain_pods.assert_not_called()
        service.kubernetes.uncordon_node.assert_not_called()

    def test_prepare_managed_pool_validates_etag_payload_and_gate(self):
        snapshot = _snapshot(
            control_version="v1.36.1",
            pool_version="v1.35.2",
        )
        service = _Service(snapshot)

        prepared = prepare_pool_upgrade(
            service,
            "oke-cpu",
            "v1.36.1",
            PoolUpgradeSpec(strategy="boot-volume-replace"),
        )

        self.assertEqual("node-pool-etag", prepared.managed_etag)
        self.assertTrue(prepared.evidence.passed)
        self.assertEqual("boot-volume-replace", prepared.strategy)

    def test_managed_execution_never_calls_kubernetes_mutation_methods(self):
        snapshot = _snapshot(
            control_version="v1.36.1",
            pool_version="v1.35.2",
        )
        service = _Service(snapshot)
        prepared = PreparedPoolUpgrade(
            snapshot=snapshot,
            pool=snapshot.pools[0],
            target_version="v1.36.1",
            spec=PoolUpgradeSpec(strategy="boot-volume-replace"),
            strategy="boot-volume-replace",
            evidence=UpgradeGateEvidence(
                pool="oke-cpu",
                nodes=("cpu-1",),
                ready=True,
                externally_cordoned=True,
            ),
            plan=SimpleNamespace(),
            managed_details=SimpleNamespace(),
            managed_etag="node-pool-etag",
        )

        with (
            patch.object(
                upgrade_workflows,
                "prepare_pool_upgrade",
                return_value=prepared,
            ),
            patch.object(upgrade_workflows, "_wait_for_pool_version"),
        ):
            result = execute_pool_upgrade(
                service,
                prepared,
                acknowledge_application_compatibility=True,
                acknowledge_iac_drift=True,
                acknowledge_workloads_drained=True,
                lock=False,
            )

        self.assertEqual("completed", result.status)
        service.kubernetes.cordon_node.assert_not_called()
        service.kubernetes.evict_drain_pods.assert_not_called()
        service.kubernetes.uncordon_node.assert_not_called()
        service.oci.upgrade_managed_pool.assert_called_once()

    def test_control_plane_requires_separate_acknowledgements(self):
        service = _Service(_snapshot())
        prepared = prepare_cluster_upgrade_plan(service, "v1.36")

        with self.assertRaisesRegex(WorkflowError, "ack-iac-drift"):
            execute_control_plane_upgrade(
                service,
                prepared,
                acknowledge_application_compatibility=True,
                acknowledge_iac_drift=False,
                lock=False,
            )

    def test_upgrade_module_contains_no_scheduler_or_eviction_mutations(self):
        source = inspect.getsource(upgrade_workflows)
        for forbidden in (
            ".cordon_node(",
            ".uncordon_node(",
            ".evict_drain_pods(",
            ".set_node_schedulable(",
        ):
            self.assertNotIn(forbidden, source)

    def test_self_managed_strategy_matrix_routes_every_backend(self):
        backend_cases = (
            (
                "cluster-network",
                {
                    "cluster_network_id": "cluster-network-1",
                    "instance_pool_id": "instance-pool-1",
                },
            ),
            (
                "instance-pool",
                {"instance_pool_id": "instance-pool-1"},
            ),
            (
                "gpu-memory-cluster",
                {"gpu_memory_cluster_id": "gmc-1"},
            ),
        )
        for kind, identifiers in backend_cases:
            for strategy in (
                "boot-volume-replace",
                "instance-replace",
                "blue-green",
            ):
                with self.subTest(kind=kind, strategy=strategy):
                    snapshot = _snapshot(
                        control_version="v1.36.1",
                        pool_version="v1.35.2",
                    )
                    pool = WorkerPoolInfo(
                        name=f"{kind}-pool",
                        kind=kind,
                        desired_size=1,
                        active_oci_instances=1,
                        ready_k8s_nodes=1,
                        instance_configuration_id="config-old",
                        kubernetes_version="v1.35.2",
                        **identifiers,
                    )
                    snapshot.pools = [pool]
                    snapshot.nodes = [
                        NodeInfo(
                            k8s_name="worker-1",
                            instance_ocid="instance-old",
                            pool_name=pool.name,
                            ready=True,
                            schedulable=False,
                            kubelet_version="v1.35.2",
                            boot_id="boot-old",
                        )
                    ]
                    service = _Service(snapshot)
                    spec = PoolUpgradeSpec(
                        strategy=strategy,
                        blue_green_compute_cluster_id=(
                            "compute-green"
                            if kind == "gpu-memory-cluster"
                            else None
                        ),
                        blue_green_gpu_memory_fabric_id=(
                            "fabric-green"
                            if kind == "gpu-memory-cluster"
                            else None
                        ),
                    )
                    prepared = PreparedPoolUpgrade(
                        snapshot=snapshot,
                        pool=pool,
                        target_version="v1.36.1",
                        spec=spec,
                        strategy=strategy,
                        evidence=UpgradeGateEvidence(
                            pool=pool.name,
                            nodes=("worker-1",),
                            ready=True,
                            externally_cordoned=True,
                        ),
                        plan=SimpleNamespace(),
                        connection_data=(
                            "10.0.0.1:6443",
                            "certificate",
                        ),
                    )
                    with (
                        patch.object(
                            upgrade_workflows,
                            "_wait_for_work_request",
                        ),
                        patch.object(
                            upgrade_workflows,
                            "_wait_for_pool_version",
                        ),
                        patch.object(
                            upgrade_workflows,
                            "_wait_for_preserved_node",
                        ),
                        patch.object(
                            upgrade_workflows,
                            "_wait_for_new_pool_node",
                            return_value="instance-new",
                        ),
                    ):
                        result = (
                            upgrade_workflows
                            ._execute_self_managed_pool_upgrade(
                                service,
                                prepared,
                                "operation-1",
                                10,
                                1,
                                None,
                            )
                        )

                    if strategy == "boot-volume-replace":
                        self.assertEqual("completed", result.status)
                        (
                            service.oci
                            .replace_self_managed_instance_boot_volume
                            .assert_called_once_with(
                                "instance-old",
                                "config-new",
                            )
                        )
                    elif strategy == "instance-replace":
                        self.assertEqual("completed", result.status)
                        service.oci.resize_upgrade_backend.assert_called()
                        if kind == "gpu-memory-cluster":
                            (
                                service.oci.terminate_upgrade_instance
                                .assert_called_once_with("instance-old")
                            )
                        else:
                            service.oci.detach_instance_pool_node.assert_called_once()
                    else:
                        self.assertEqual("action-required", result.status)
                        self.assertIn("explicitly remove or finalize", result.action)
                        if kind == "cluster-network":
                            service.oci.create_cluster_network_blue_green_pool.assert_called_once()
                        elif kind == "instance-pool":
                            service.oci.create_instance_pool_blue_green.assert_called_once()
                        else:
                            service.oci.create_gpu_memory_cluster_blue_green.assert_called_once()

    def test_managed_strategy_matrix_routes_both_cycle_modes_and_blue_green(self):
        for strategy in (
            "boot-volume-replace",
            "instance-replace",
            "blue-green",
        ):
            with self.subTest(strategy=strategy):
                snapshot = _snapshot(
                    control_version="v1.36.1",
                    pool_version="v1.35.2",
                )
                service = _Service(snapshot)
                prepared = PreparedPoolUpgrade(
                    snapshot=snapshot,
                    pool=snapshot.pools[0],
                    target_version="v1.36.1",
                    spec=PoolUpgradeSpec(strategy=strategy),
                    strategy=strategy,
                    evidence=UpgradeGateEvidence(
                        pool="oke-cpu",
                        nodes=("cpu-1",),
                        ready=True,
                        externally_cordoned=True,
                    ),
                    plan=SimpleNamespace(),
                    managed_details=SimpleNamespace(),
                    managed_etag="node-pool-etag",
                )
                with (
                    patch.object(
                        upgrade_workflows,
                        "_wait_for_work_request",
                    ),
                    patch.object(
                        upgrade_workflows,
                        "_wait_for_pool_version",
                        return_value=WorkerPoolInfo(
                            name="oke-cpu-v1-36-1",
                            kind="node-pool",
                            node_pool_id="node-pool-green",
                        ),
                    ),
                ):
                    result = (
                        upgrade_workflows._execute_managed_pool_upgrade(
                            service,
                            prepared,
                            "operation-1",
                            10,
                            1,
                            None,
                        )
                    )

                if strategy == "blue-green":
                    self.assertEqual("action-required", result.status)
                    self.assertIn("explicitly remove or finalize", result.action)
                    self.assertEqual(
                        ("node-pool-green",),
                        result.created_resource_ids,
                    )
                    service.oci.create_managed_blue_green_pool.assert_called_once()
                else:
                    self.assertEqual("completed", result.status)
                    service.oci.upgrade_managed_pool.assert_called_once()

    def test_work_request_failure_is_terminal_and_includes_service_error(self):
        service = _Service(_snapshot())
        service.oci.get_work_request_status = Mock(
            return_value=WorkRequestInfo(
                "work-request-1",
                "FAILED",
                40.0,
                ("service rejected update",),
            )
        )

        with self.assertRaisesRegex(
            WorkflowError,
            "service rejected update",
        ):
            upgrade_workflows._wait_for_work_request(
                service,
                "work-request-1",
                "compartment-1",
                10,
                1,
                None,
            )

    def test_restart_recovery_uses_observed_control_plane_state(self):
        snapshot = _snapshot(
            control_version="v1.36.1",
            pool_version="v1.36.1",
            kubelet_version="v1.36.1",
        )
        snapshot.pools = []
        snapshot.nodes = []
        service = _Service(snapshot)
        service.kubernetes.write_upgrade_checkpoint = Mock(
            return_value="resource-version-next"
        )
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.34.5",
            target_version="v1.36.1",
            control_plane_steps=("v1.35.4", "v1.36.1"),
            pool_order=(),
            strategies={},
        ).replace(
            phase=UpgradePhase.CONTROL_PLANE,
            control_plane_index=0,
            acknowledged_application_compatibility=True,
            acknowledged_iac_drift=True,
        )

        restored, _, results = upgrade_workflows._continue_checkpoint(
            service,
            checkpoint,
            "resource-version-old",
            acknowledge_workloads_drained=False,
            emergency_ack_unverified_drain=False,
            timeout_seconds=10,
            poll_interval_seconds=1,
            progress=None,
        )

        self.assertEqual(UpgradePhase.COMPLETED, restored.phase)
        self.assertEqual(2, restored.control_plane_index)
        self.assertEqual("completed", results[-1].status)
        service.oci.upgrade_control_plane.assert_not_called()

    def test_restart_recovery_waits_for_target_control_plane_to_be_active(self):
        snapshot = _snapshot(
            control_version="v1.36.1",
            pool_version="v1.36.1",
            kubelet_version="v1.36.1",
        )
        snapshot.cluster = replace(
            snapshot.cluster,
            lifecycle_state="UPDATING",
        )
        snapshot.pools = []
        snapshot.nodes = []
        service = _Service(snapshot)
        service.kubernetes.write_upgrade_checkpoint = Mock(
            return_value="resource-version-next"
        )
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=(),
            strategies={},
        ).replace(
            phase=UpgradePhase.CONTROL_PLANE,
            acknowledged_application_compatibility=True,
            acknowledged_iac_drift=True,
        )

        def mark_active(*args, **kwargs):
            service.snapshot.cluster = replace(
                service.snapshot.cluster,
                lifecycle_state="ACTIVE",
            )

        with patch.object(
            upgrade_workflows,
            "_wait_for_control_plane",
            side_effect=mark_active,
        ) as wait_for_control_plane:
            restored, _, _ = upgrade_workflows._continue_checkpoint(
                service,
                checkpoint,
                "resource-version-old",
                acknowledge_workloads_drained=False,
                emergency_ack_unverified_drain=False,
                timeout_seconds=10,
                poll_interval_seconds=1,
                progress=None,
            )

        self.assertEqual(UpgradePhase.COMPLETED, restored.phase)
        wait_for_control_plane.assert_called_once_with(
            service,
            "v1.36.1",
            10,
            1,
            None,
        )

    def test_final_verification_requires_active_control_plane(self):
        snapshot = _snapshot(
            control_version="v1.36.1",
            pool_version="v1.36.1",
            kubelet_version="v1.36.1",
        )
        snapshot.cluster = replace(
            snapshot.cluster,
            lifecycle_state="UPDATING",
        )
        service = _Service(snapshot)

        with self.assertRaisesRegex(
            WorkflowError,
            "expected v1.36.1/ACTIVE",
        ):
            upgrade_workflows._verify_cluster_target(
                service,
                "v1.36.1",
            )

    def test_worker_configuration_phase_prepares_managed_and_self_managed_pools(self):
        snapshot = _snapshot(
            control_version="v1.36.1",
            pool_version="v1.35.2",
        )
        self_managed = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            desired_size=1,
            active_oci_instances=1,
            ready_k8s_nodes=1,
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
            instance_configuration_id="config-old",
            kubernetes_version="v1.35.2",
            rdma_enabled=True,
        )
        snapshot.pools.append(self_managed)
        snapshot.nodes.append(
            NodeInfo(
                k8s_name="rdma-1",
                instance_ocid="instance-rdma-1",
                pool_name=self_managed.name,
                ready=True,
                schedulable=False,
                kubelet_version="v1.35.2",
            )
        )
        service = _Service(snapshot)
        service.kubernetes.write_upgrade_checkpoint = Mock(
            return_value="resource-version-next"
        )
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=("oke-cpu", "oke-rdma"),
            strategies={
                "oke-cpu": "boot-volume-replace",
                "oke-rdma": "instance-replace",
            },
        ).replace(
            phase=UpgradePhase.WORKER_CONFIGS,
            control_plane_index=1,
        )

        with (
            patch.object(
                upgrade_workflows,
                "_wait_for_work_request",
            ),
            patch.object(
                upgrade_workflows,
                "_wait_for_worker_configuration",
            ),
        ):
            configured, _, results = (
                upgrade_workflows._prepare_worker_configurations(
                    service,
                    checkpoint,
                    "resource-version-old",
                    timeout_seconds=10,
                    poll_interval_seconds=1,
                    progress=None,
                )
            )

        self.assertEqual(
            ["configured", "configured"],
            [pool.phase for pool in configured.pools],
        )
        self.assertEqual(
            ["worker-launch-configuration"] * 2,
            [result.operation for result in results],
        )
        managed_call = (
            service.oci.preview_managed_pool_upgrade.call_args.kwargs
        )
        self.assertFalse(managed_call["enable_cycling"])
        service.oci.create_upgrade_instance_configuration.assert_called_once()
        service.oci.attach_upgrade_instance_configuration.assert_called_once()
        self.assertEqual(
            "config-new",
            configured.pools[1].target_instance_configuration_id,
        )

    def test_apply_creates_checkpoint_before_continuing(self):
        service = _Service(_snapshot())
        plan = prepare_cluster_upgrade_plan(service, "v1.36")
        completed = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=("oke-cpu",),
            strategies={"oke-cpu": "boot-volume-replace"},
        ).replace(phase=UpgradePhase.COMPLETED)
        continuation = [
            upgrade_workflows.UpgradeExecutionResult(
                operation="cluster-upgrade",
                target="v1.36.1",
                status="completed",
            )
        ]

        with patch.object(
            upgrade_workflows,
            "_continue_checkpoint",
            return_value=(
                completed,
                "resource-version-next",
                continuation,
            ),
        ) as continue_checkpoint:
            results = upgrade_workflows.execute_upgrade_apply(
                service,
                plan,
                acknowledge_application_compatibility=True,
                acknowledge_iac_drift=True,
                acknowledge_workloads_drained=True,
            )

        self.assertEqual(continuation, results)
        first_write = (
            service.kubernetes.write_upgrade_checkpoint.call_args_list[0]
        )
        persisted = first_write.args[0]
        self.assertEqual(UpgradePhase.PLANNED, persisted.phase)
        self.assertTrue(persisted.acknowledged_application_compatibility)
        self.assertTrue(persisted.acknowledged_iac_drift)
        continue_checkpoint.assert_called_once()

    def test_apply_refuses_to_replace_an_active_checkpoint(self):
        service = _Service(_snapshot())
        plan = prepare_cluster_upgrade_plan(service, "v1.36")
        active = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=("oke-cpu",),
            strategies={"oke-cpu": "boot-volume-replace"},
        )
        service.kubernetes.read_upgrade_checkpoint.return_value = (
            active,
            "resource-version-old",
        )

        with self.assertRaisesRegex(
            WorkflowError,
            "already active",
        ):
            upgrade_workflows.execute_upgrade_apply(
                service,
                plan,
                acknowledge_application_compatibility=True,
                acknowledge_iac_drift=True,
                acknowledge_workloads_drained=True,
            )

        service.kubernetes.write_upgrade_checkpoint.assert_not_called()

    def test_resume_continues_the_stored_checkpoint(self):
        service = _Service(_snapshot())
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=("oke-cpu",),
            strategies={"oke-cpu": "boot-volume-replace"},
        ).replace(phase=UpgradePhase.POOL_GATE)
        service.kubernetes.read_upgrade_checkpoint.return_value = (
            checkpoint,
            "resource-version-old",
        )
        continuation = [
            upgrade_workflows.UpgradeExecutionResult(
                operation="worker-pool-upgrade",
                target="oke-cpu",
                status="completed",
            )
        ]

        with patch.object(
            upgrade_workflows,
            "_continue_checkpoint",
            return_value=(
                checkpoint.replace(phase=UpgradePhase.COMPLETED),
                "resource-version-next",
                continuation,
            ),
        ) as continue_checkpoint:
            results = upgrade_workflows.resume_upgrade(
                service,
                acknowledge_workloads_drained=True,
            )

        self.assertEqual(continuation, results)
        self.assertTrue(
            continue_checkpoint.call_args.kwargs[
                "acknowledge_workloads_drained"
            ]
        )

    def test_abandon_records_state_without_oci_rollback(self):
        service = _Service(_snapshot())
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=("oke-cpu",),
            strategies={"oke-cpu": "boot-volume-replace"},
        ).replace(phase=UpgradePhase.POOL_GATE)
        service.kubernetes.read_upgrade_checkpoint.return_value = (
            checkpoint,
            "resource-version-old",
        )

        result = upgrade_workflows.abandon_upgrade(service)

        written = service.kubernetes.write_upgrade_checkpoint.call_args.args[0]
        self.assertEqual(UpgradePhase.ABANDONED, written.phase)
        self.assertEqual("abandoned", result.status)
        service.oci.upgrade_control_plane.assert_not_called()
        service.oci.upgrade_managed_pool.assert_not_called()

    def test_cleanup_deletes_only_recorded_operation_configurations(self):
        service = _Service(_snapshot())
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=("oke-cpu",),
            strategies={"oke-cpu": "boot-volume-replace"},
        )
        checkpoint = checkpoint.replace(
            phase=UpgradePhase.COMPLETED,
            pools=(
                replace(
                    checkpoint.pools[0],
                    superseded_instance_configuration_ids=(
                        "config-superseded",
                    ),
                ),
            ),
        )
        service.kubernetes.read_upgrade_checkpoint.return_value = (
            checkpoint,
            "resource-version-old",
        )
        service.oci.delete_mgmt_created_instance_configuration = Mock()

        result = upgrade_workflows.cleanup_upgrade(service)

        (
            service.oci.delete_mgmt_created_instance_configuration
            .assert_called_once_with(
                "config-superseded",
                operation_id=checkpoint.operation_id,
            )
        )
        (
            service.kubernetes.delete_upgrade_checkpoint
            .assert_called_once_with("resource-version-old")
        )
        self.assertEqual("completed", result.status)

    def test_cleanup_refuses_an_incomplete_checkpoint(self):
        service = _Service(_snapshot())
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.35.2",
            target_version="v1.36.1",
            control_plane_steps=("v1.36.1",),
            pool_order=(),
            strategies={},
        )
        service.kubernetes.read_upgrade_checkpoint.return_value = (
            checkpoint,
            "resource-version-old",
        )

        with self.assertRaisesRegex(
            WorkflowError,
            "only after successful completion",
        ):
            upgrade_workflows.cleanup_upgrade(service)

        service.kubernetes.delete_upgrade_checkpoint.assert_not_called()

    def test_control_plane_observer_records_submission_and_completion(self):
        service = _Service(_snapshot())
        prepared = prepare_cluster_upgrade_plan(service, "v1.36")
        observer = Mock()
        with (
            patch.object(
                upgrade_workflows,
                "_wait_for_work_request",
            ),
            patch.object(
                upgrade_workflows,
                "_wait_for_control_plane",
            ),
        ):
            execute_control_plane_upgrade(
                service,
                prepared,
                acknowledge_application_compatibility=True,
                acknowledge_iac_drift=True,
                lock=False,
                work_request_observer=observer,
            )

        self.assertEqual(
            [
                call("work-request-control"),
                call(None),
            ],
            observer.call_args_list,
        )

    def test_control_plane_dependents_require_virtual_and_addon_convergence(self):
        snapshot = _snapshot(control_version="v1.36.1")
        snapshot.virtual_pools = [
            VirtualNodePoolInfo(
                name="virtual-workers",
                virtual_node_pool_id="virtual-1",
                kubernetes_version="v1.35.2",
                size=1,
                lifecycle_state="UPDATING",
            )
        ]
        service = _Service(snapshot)
        service.oci.get_addon_compatibility = Mock(
            return_value=[
                AddonCompatibility(
                    name="KubeProxy",
                    installed_version="old-build",
                    update_mode="AUTOMATIC",
                    supported_versions=("new-build",),
                    compatible=True,
                )
            ]
        )

        issues = upgrade_workflows._control_plane_dependent_issues(
            service,
            snapshot,
            "v1.36.1",
        )

        self.assertTrue(
            any("virtual pool virtual-workers" in issue for issue in issues)
        )
        self.assertTrue(
            any("automatic add-on KubeProxy" in issue for issue in issues)
        )

    def test_slinky_registration_is_read_only_and_requires_matching_node(self):
        snapshot = _snapshot(control_version="v1.36.1")
        pool = snapshot.pools[0]
        pool.slinky_managed = True
        snapshot.nodes[0].annotations[
            "nodeset.slinky.slurm.net/hostname-override"
        ] = "slurm-cpu-1"
        service = _Service(snapshot)
        service.kubernetes.exec_slurmctld = Mock(
            return_value="NodeName=slurm-cpu-1 State=IDLE"
        )

        issues = upgrade_workflows._slinky_registration_issues(
            service,
            snapshot,
            pool,
        )

        self.assertEqual((), issues)
        service.kubernetes.exec_slurmctld.assert_called_once_with(
            (
                "scontrol",
                "show",
                "node",
                "slurm-cpu-1",
                "--oneliner",
            )
        )


if __name__ == "__main__":
    unittest.main()
