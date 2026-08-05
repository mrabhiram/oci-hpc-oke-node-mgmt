from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Optional

from oke_hpc_mgmt.discovery import DiscoveryService
from oke_hpc_mgmt.models import (
    AddonCompatibility,
    DiscoverySnapshot,
    OperationPlan,
    PoolCreateSpec,
    WorkerPoolInfo,
)
from oke_hpc_mgmt.upgrades import (
    KubernetesVersion,
    UpgradeCheckpoint,
    UpgradeGateEvidence,
    UpgradePhase,
    UpgradePoolState,
    UpgradeValidationError,
    control_plane_steps,
    default_pool_order,
    kueue_upgrade_blockers,
    parse_slurm_record,
    resolve_upgrade_target,
    select_upgrade_strategy,
    slurm_upgrade_blockers,
    validate_control_plane_step,
    validate_cycling_value,
    validate_worker_skew,
    validate_workload_gate,
)
from oke_hpc_mgmt.workflows.lifecycle import (
    WorkflowError,
    WorkflowNotFound,
    mutation_lock,
    pool_resource_readiness,
    resource_counts_match,
)


Progress = Callable[[str], None]
WorkRequestObserver = Callable[[Optional[str]], None]
ResourceObserver = Callable[[tuple[str, ...], Optional[str]], None]


@dataclass(frozen=True)
class PoolUpgradeSpec:
    strategy: str = "auto"
    image_id: str | None = None
    maximum_unavailable: str | None = None
    maximum_surge: str | None = None
    blue_green_name: str | None = None
    blue_green_compute_cluster_id: str | None = None
    blue_green_gpu_memory_fabric_id: str | None = None


@dataclass(frozen=True)
class ClusterUpgradePlan:
    snapshot: DiscoverySnapshot
    source_version: str
    target_version: str
    control_plane_steps: tuple[str, ...]
    pool_order: tuple[str, ...]
    pool_specs: dict[str, PoolUpgradeSpec]
    addon_compatibility: tuple[AddonCompatibility, ...]
    plans: tuple[OperationPlan, ...]


@dataclass(frozen=True)
class PreparedPoolUpgrade:
    snapshot: DiscoverySnapshot
    pool: WorkerPoolInfo
    target_version: str
    spec: PoolUpgradeSpec
    strategy: str
    evidence: UpgradeGateEvidence
    plan: OperationPlan
    managed_details: Any | None = None
    managed_etag: str | None = None
    instance_configuration_preview: Any | None = None
    connection_data: tuple[str, str] | None = None
    target_instance_configuration_id: str | None = None


@dataclass(frozen=True)
class UpgradeExecutionResult:
    operation: str
    target: str
    status: str
    work_request_ids: tuple[str, ...] = ()
    created_resource_ids: tuple[str, ...] = ()
    retained_resource_ids: tuple[str, ...] = ()
    action: str | None = None
    target_instance_configuration_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "target": self.target,
            "status": self.status,
            "work_request_ids": list(self.work_request_ids),
            "created_resource_ids": list(self.created_resource_ids),
            "retained_resource_ids": list(self.retained_resource_ids),
            "action": self.action,
            "target_instance_configuration_id": (
                self.target_instance_configuration_id
            ),
        }


def prepare_cluster_upgrade_plan(
    service: DiscoveryService,
    target: str,
    *,
    allow_preview: bool = False,
    order: Iterable[str] = (),
    default_strategy: str = "auto",
    strategy_overrides: dict[str, str] | None = None,
    image_overrides: dict[str, str] | None = None,
    pool_spec_overrides: dict[str, PoolUpgradeSpec] | None = None,
) -> ClusterUpgradePlan:
    _require_upgrade_access(service)
    snapshot = service.discover()
    cluster = _require_cluster(snapshot)
    resolved = resolve_upgrade_target(
        target,
        cluster.available_kubernetes_versions,
        allow_preview=allow_preview,
    )
    if KubernetesVersion.parse(str(resolved)) <= KubernetesVersion.parse(
        cluster.kubernetes_version
    ):
        raise WorkflowError(
            f"Upgrade target {resolved} must be newer than control plane "
            f"{cluster.kubernetes_version}."
        )
    steps = tuple(
        str(item)
        for item in control_plane_steps(
            cluster.kubernetes_version,
            str(resolved),
            cluster.available_kubernetes_versions,
        )
    )
    _validate_current_skew(snapshot)
    compatibility = tuple(
        service.oci_backend().get_addon_compatibility(
            str(resolved),
            snapshot.addons,
        )
    )
    incompatible = [item for item in compatibility if not item.compatible]
    if incompatible:
        raise WorkflowError(
            "Pinned or unavailable OKE add-ons block the target version: "
            + "; ".join(
                f"{item.name}: {item.reason or 'incompatible'}"
                for item in incompatible
            )
        )
    pool_order = _resolve_pool_order(snapshot.pools, order)
    overrides = strategy_overrides or {}
    images = image_overrides or {}
    unknown_overrides = (
        set(overrides)
        | set(images)
        | set(pool_spec_overrides or {})
    ) - set(pool_order)
    if unknown_overrides:
        raise WorkflowNotFound(
            "Upgrade override pools not found: "
            + ", ".join(sorted(unknown_overrides))
        )
    explicit_specs = pool_spec_overrides or {}
    specs = {}
    for pool in snapshot.pools:
        explicit = explicit_specs.get(pool.name)
        specs[pool.name] = (
            replace(
                explicit,
                strategy=overrides.get(pool.name, explicit.strategy),
                image_id=images.get(pool.name, explicit.image_id),
            )
            if explicit
            else PoolUpgradeSpec(
                strategy=overrides.get(pool.name, default_strategy),
                image_id=images.get(pool.name),
            )
        )
    plans: list[OperationPlan] = [
        OperationPlan(
            operation="control-plane-upgrade",
            target=step,
            owner="oke",
            steps=(
                "revalidate OKE target, version policy, ETag, and add-ons",
                "call OKE UpdateCluster",
                "wait for work request and control-plane convergence",
            ),
            details={
                "source_version": (
                    cluster.kubernetes_version
                    if index == 0
                    else steps[index - 1]
                ),
                "target_version": step,
            },
        )
        for index, step in enumerate(steps)
    ]
    for name in pool_order:
        prepared_pool = _prepare_pool_upgrade_from_snapshot(
            service,
            snapshot,
            name,
            resolved,
            specs[name],
            allow_future_control_plane=True,
            allow_current_target=False,
        )
        specs[name] = prepared_pool.spec
        plans.append(prepared_pool.plan)
    return ClusterUpgradePlan(
        snapshot=snapshot,
        source_version=cluster.kubernetes_version,
        target_version=str(resolved),
        control_plane_steps=steps,
        pool_order=pool_order,
        pool_specs=specs,
        addon_compatibility=compatibility,
        plans=tuple(plans),
    )


def prepare_control_plane_upgrade(
    service: DiscoveryService,
    target: str,
    *,
    allow_preview: bool = False,
) -> ClusterUpgradePlan:
    _require_upgrade_access(service)
    snapshot = service.discover()
    cluster = _require_cluster(snapshot)
    resolved = resolve_upgrade_target(
        target,
        cluster.available_kubernetes_versions,
        allow_preview=allow_preview,
    )
    steps = tuple(
        str(item)
        for item in control_plane_steps(
            cluster.kubernetes_version,
            str(resolved),
            cluster.available_kubernetes_versions,
        )
    )
    if len(steps) != 1:
        raise WorkflowError(
            "clusters upgrade executes one control-plane patch or minor step. "
            f"The requested target needs {len(steps)} steps; "
            "use upgrades apply for ordered orchestration."
        )
    _validate_current_skew(snapshot)
    not_ready = sorted(
        node.k8s_name for node in snapshot.nodes if not node.ready
    )
    if not_ready:
        raise WorkflowError(
            "All worker nodes must be Ready before a control-plane upgrade; "
            "not Ready: " + ", ".join(not_ready)
        )
    compatibility = tuple(
        service.oci_backend().get_addon_compatibility(
            str(resolved),
            snapshot.addons,
        )
    )
    incompatible = [item for item in compatibility if not item.compatible]
    if incompatible:
        raise WorkflowError(
            "Pinned or unavailable OKE add-ons block the target version: "
            + "; ".join(
                f"{item.name}: {item.reason or 'incompatible'}"
                for item in incompatible
            )
        )
    operation = OperationPlan(
        operation="control-plane-upgrade",
        target=steps[0],
        owner="oke",
        steps=(
            "revalidate OKE target, version policy, ETag, and add-ons",
            "call OKE UpdateCluster",
            "wait for work request and control-plane convergence",
        ),
        details={
            "source_version": cluster.kubernetes_version,
            "target_version": steps[0],
        },
    )
    return ClusterUpgradePlan(
        snapshot=snapshot,
        source_version=cluster.kubernetes_version,
        target_version=str(resolved),
        control_plane_steps=steps,
        pool_order=(),
        pool_specs={},
        addon_compatibility=compatibility,
        plans=(operation,),
    )


def execute_control_plane_upgrade(
    service: DiscoveryService,
    prepared: ClusterUpgradePlan,
    *,
    acknowledge_application_compatibility: bool,
    acknowledge_iac_drift: bool,
    timeout_seconds: int = 3600,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    progress: Progress | None = None,
    work_request_observer: WorkRequestObserver | None = None,
) -> UpgradeExecutionResult:
    _require_acknowledgements(
        acknowledge_application_compatibility,
        acknowledge_iac_drift,
    )
    target = prepared.control_plane_steps[0]
    with mutation_lock(service, lock, timeout_seconds):
        backend = service.oci_backend()
        fresh_snapshot = service.discover()
        if any(not node.ready for node in fresh_snapshot.nodes):
            raise WorkflowError(
                "All worker nodes must be Ready before a control-plane upgrade."
            )
        current = backend.get_cluster_info(
            _require_cluster(fresh_snapshot).cluster_id,
            _require_cluster(fresh_snapshot).compartment_id,
        )
        validate_control_plane_step(current.kubernetes_version, target)
        resolve_upgrade_target(
            target,
            current.available_kubernetes_versions,
            allow_preview=KubernetesVersion.parse(target).preview,
        )
        compatibility = backend.get_addon_compatibility(
            target,
            fresh_snapshot.addons,
        )
        incompatible = [item for item in compatibility if not item.compatible]
        if incompatible:
            raise WorkflowError(
                "Add-on compatibility changed after planning: "
                + "; ".join(
                    f"{item.name}: {item.reason or 'incompatible'}"
                    for item in incompatible
                )
            )
        work_request_id = backend.upgrade_control_plane(
            current.cluster_id,
            target,
            current.etag or "",
        )
        if work_request_observer:
            work_request_observer(work_request_id)
        _wait_for_work_request(
            service,
            work_request_id,
            current.compartment_id,
            timeout_seconds,
            poll_interval_seconds,
            progress,
        )
        if work_request_observer:
            work_request_observer(None)
        _wait_for_control_plane(
            service,
            target,
            timeout_seconds,
            poll_interval_seconds,
            progress,
        )
    return UpgradeExecutionResult(
        operation="control-plane-upgrade",
        target=target,
        status="completed",
        work_request_ids=(work_request_id,) if work_request_id else (),
    )


def prepare_pool_upgrade(
    service: DiscoveryService,
    pool_name: str,
    target: str,
    spec: PoolUpgradeSpec,
    *,
    allow_preview: bool = False,
) -> PreparedPoolUpgrade:
    _require_upgrade_access(service)
    snapshot = service.discover()
    cluster = _require_cluster(snapshot)
    target_version = resolve_upgrade_target(
        target,
        cluster.available_kubernetes_versions,
        allow_preview=allow_preview,
    )
    return _prepare_pool_upgrade_from_snapshot(
        service,
        snapshot,
        pool_name,
        target_version,
        spec,
        allow_future_control_plane=False,
        allow_current_target=False,
    )


def _prepare_pool_upgrade_from_snapshot(
    service: DiscoveryService,
    snapshot: DiscoverySnapshot,
    pool_name: str,
    target_version: KubernetesVersion,
    spec: PoolUpgradeSpec,
    *,
    allow_future_control_plane: bool,
    allow_current_target: bool,
) -> PreparedPoolUpgrade:
    cluster = _require_cluster(snapshot)
    pool = snapshot.pool_by_name(pool_name)
    if pool is None:
        raise WorkflowNotFound(f"Pool not found: {pool_name}")
    control = KubernetesVersion.parse(cluster.kubernetes_version).require_exact()
    if target_version > control and not allow_future_control_plane:
        raise WorkflowError(
            f"Worker target {target_version} cannot be newer than control plane {control}."
        )
    if pool.kubernetes_version:
        current = KubernetesVersion.parse(pool.kubernetes_version).require_exact()
        if target_version < current or (
            target_version == current and not allow_current_target
        ):
            raise WorkflowError(
                f"Pool {pool.name} target {target_version} must be newer than "
                f"its declared version {current}."
            )
    strategy = select_upgrade_strategy(pool, spec.strategy)
    maximum_unavailable = validate_cycling_value(
        spec.maximum_unavailable,
        "maximumUnavailable",
    )
    maximum_surge = validate_cycling_value(
        spec.maximum_surge,
        "maximumSurge",
    )
    spec = replace(
        spec,
        maximum_unavailable=maximum_unavailable,
        maximum_surge=maximum_surge,
    )
    evidence = collect_upgrade_gate_evidence(service, snapshot, pool)
    backend = service.oci_backend()
    managed_details = None
    managed_etag = None
    instance_preview = None
    connection_data = None
    details: dict[str, Any] = {
        "current_version": pool.kubernetes_version,
        "target_version": str(target_version),
        "strategy": strategy,
        "image_id": spec.image_id,
        "requires_external_drain": True,
        "gate_ready": evidence.ready,
        "gate_cordoned": evidence.externally_cordoned,
        "gate_active_pods": list(evidence.active_pods),
        "gate_kueue_blockers": list(evidence.kueue_blockers),
        "gate_slurm_blockers": list(evidence.slurm_blockers),
        "gate_verification_errors": list(evidence.verification_errors),
    }
    if pool.kind == "node-pool":
        if not pool.node_pool_id:
            raise WorkflowError(
                f"Managed pool {pool.name} has no node pool OCID."
            )
        if strategy in {"boot-volume-replace", "instance-replace"}:
            managed_details, managed_etag, preview = (
                backend.preview_managed_pool_upgrade(
                    pool.node_pool_id,
                    str(target_version),
                    strategy=strategy,
                    image_id=spec.image_id,
                    maximum_unavailable=spec.maximum_unavailable,
                    maximum_surge=spec.maximum_surge,
                )
            )
            details.update(preview)
        else:
            preview_spec = PoolCreateSpec(
                pool_type=(
                    "rdma"
                    if pool.rdma_enabled
                    else "gpu" if pool.gpu_resource else "cpu"
                ),
                rdma_mode=(
                    "compute-cluster" if pool.rdma_enabled else None
                ),
                compute_cluster_id=pool.compute_cluster_id,
                host_group_id=(
                    next(iter(sorted(pool.host_group_ids)), None)
                ),
                kubernetes_version=str(target_version),
                image_id=spec.image_id,
            )
            details.update(
                backend.preview_managed_node_pool_create(
                    pool.node_pool_id,
                    cluster.cluster_id,
                    cluster.compartment_id,
                    _blue_green_name(pool, target_version, spec),
                    pool.desired_size or 0,
                    preview_spec,
                )
            )
    else:
        if not pool.instance_configuration_id:
            raise WorkflowError(
                f"Self-managed pool {pool.name} has no Instance Configuration."
            )
        connection_data = service.kubernetes_backend().cluster_connection_data()
        instance_preview, preview = (
            backend.preview_instance_configuration_upgrade(
                pool.instance_configuration_id,
                str(target_version),
                operation_id="dry-run",
                api_server=connection_data[0],
                cluster_ca=connection_data[1],
                image_id=spec.image_id,
                availability_domain=pool.availability_domain,
            )
        )
        details.update(preview)
        if (
            strategy == "blue-green"
            and pool.kind == "gpu-memory-cluster"
            and (
                not spec.blue_green_compute_cluster_id
                or not spec.blue_green_gpu_memory_fabric_id
            )
        ):
            raise WorkflowError(
                "GPU Memory Cluster blue-green requires explicit target "
                "Compute Cluster and GPU Memory Fabric OCIDs."
            )
    return PreparedPoolUpgrade(
        snapshot=snapshot,
        pool=pool,
        target_version=str(target_version),
        spec=spec,
        strategy=strategy,
        evidence=evidence,
        plan=OperationPlan(
            operation="worker-pool-upgrade",
            target=pool.name,
            pool=pool.name,
            owner=pool.kind,
            current_size=pool.desired_size,
            target_size=pool.desired_size,
            workload_pods=len(evidence.active_pods),
            steps=_pool_strategy_steps(pool, strategy),
            warnings=tuple(evidence.verification_errors),
            details=details,
        ),
        managed_details=managed_details,
        managed_etag=managed_etag,
        instance_configuration_preview=instance_preview,
        connection_data=connection_data,
    )


def execute_pool_upgrade(
    service: DiscoveryService,
    prepared: PreparedPoolUpgrade,
    *,
    acknowledge_application_compatibility: bool,
    acknowledge_iac_drift: bool,
    acknowledge_workloads_drained: bool,
    emergency_ack_unverified_drain: bool = False,
    timeout_seconds: int = 7200,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    progress: Progress | None = None,
    operation_id: str | None = None,
    work_request_observer: WorkRequestObserver | None = None,
    resource_observer: ResourceObserver | None = None,
) -> UpgradeExecutionResult:
    _require_acknowledgements(
        acknowledge_application_compatibility,
        acknowledge_iac_drift,
    )
    validate_workload_gate(
        prepared.evidence,
        acknowledged=acknowledge_workloads_drained,
        emergency_ack_unverified_drain=emergency_ack_unverified_drain,
    )
    operation_id = operation_id or UpgradeCheckpoint.create(
        cluster_id=_require_cluster(prepared.snapshot).cluster_id,
        source_version=_require_cluster(prepared.snapshot).kubernetes_version,
        target_version=prepared.target_version,
        control_plane_steps=(),
        pool_order=(prepared.pool.name,),
        strategies={prepared.pool.name: prepared.strategy},
    ).operation_id
    with mutation_lock(service, lock, timeout_seconds):
        current = prepare_pool_upgrade(
            service,
            prepared.pool.name,
            prepared.target_version,
            prepared.spec,
            allow_preview=KubernetesVersion.parse(
                prepared.target_version
            ).preview,
        )
        validate_workload_gate(
            current.evidence,
            acknowledged=acknowledge_workloads_drained,
            emergency_ack_unverified_drain=emergency_ack_unverified_drain,
        )
        if current.pool.kind == "node-pool":
            return _execute_managed_pool_upgrade(
                service,
                current,
                operation_id,
                timeout_seconds,
                poll_interval_seconds,
                progress,
                work_request_observer,
                resource_observer,
            )
        return _execute_self_managed_pool_upgrade(
            service,
            current,
            operation_id,
            timeout_seconds,
            poll_interval_seconds,
            progress,
            work_request_observer,
            resource_observer,
        )


def collect_upgrade_gate_evidence(
    service: DiscoveryService,
    snapshot: DiscoverySnapshot,
    pool: WorkerPoolInfo,
) -> UpgradeGateEvidence:
    nodes = [node for node in snapshot.nodes if node.pool_name == pool.name]
    errors: list[str] = []
    active_pods: tuple[str, ...] = ()
    if not nodes:
        errors.append("No Kubernetes nodes were matched to the pool.")
    kubernetes = service.kubernetes_backend()
    try:
        pods = kubernetes.list_upgrade_blocking_pods(
            {node.k8s_name for node in nodes}
        )
        active_pods = tuple(
            f"{pod.namespace}/{pod.name}" for pod in pods
        )
    except Exception as exc:
        errors.append(f"Kubernetes pod verification failed: {exc}")
    kueue_errors = [
        warning
        for warning in snapshot.warnings
        if warning.startswith("Kueue discovery skipped:")
    ]
    errors.extend(kueue_errors)
    kueue_blockers = kueue_upgrade_blockers(
        snapshot.kueue,
        pool.kueue_flavor,
    )
    slurm_blocker_values: tuple[str, ...] = ()
    if pool.slinky_managed or any(node.slinky_managed for node in nodes):
        try:
            node_records: list[str] = []
            partitions: set[str] = set()
            slurm_names = [
                node.slurm_name
                for node in nodes
                if node.slurm_name
            ]
            if len(slurm_names) != len(nodes):
                raise WorkflowError(
                    "One or more Slinky workers have no Slurm hostname annotation."
                )
            for name in slurm_names:
                record = kubernetes.exec_slurmctld(
                    ("scontrol", "show", "node", name, "--oneliner")
                )
                node_records.append(record)
                parsed = parse_slurm_record(record)
                partitions.update(
                    item
                    for item in parsed.get("Partitions", "").split(",")
                    if item and item != "(null)"
                )
            partition_records = [
                kubernetes.exec_slurmctld(
                    ("scontrol", "show", "partition", name, "--oneliner")
                )
                for name in sorted(partitions)
            ]
            jobs = kubernetes.exec_slurmctld(
                (
                    "squeue",
                    "--noheader",
                    "--states=RUNNING,CONFIGURING,COMPLETING,SUSPENDED,RESIZING",
                    f"--nodes={','.join(slurm_names)}",
                    "--format=%i:%T:%N",
                )
            )
            slurm_blocker_values = slurm_upgrade_blockers(
                node_records,
                partition_records,
                jobs,
            )
        except Exception as exc:
            errors.append(f"Slinky verification failed: {exc}")
    return UpgradeGateEvidence(
        pool=pool.name,
        nodes=tuple(node.k8s_name for node in nodes),
        ready=bool(nodes) and all(node.ready for node in nodes),
        externally_cordoned=bool(nodes)
        and all(not node.schedulable for node in nodes),
        active_pods=active_pods,
        verification_errors=tuple(errors),
        kueue_blockers=kueue_blockers,
        slurm_blockers=slurm_blocker_values,
    )


def checkpoint_from_plan(plan: ClusterUpgradePlan) -> UpgradeCheckpoint:
    return UpgradeCheckpoint.create(
        cluster_id=_require_cluster(plan.snapshot).cluster_id,
        source_version=plan.source_version,
        target_version=plan.target_version,
        control_plane_steps=plan.control_plane_steps,
        pool_order=plan.pool_order,
        strategies={
            name: select_upgrade_strategy(
                plan.snapshot.pool_by_name(name),  # type: ignore[arg-type]
                plan.pool_specs[name].strategy,
            )
            for name in plan.pool_order
        },
        images={
            name: spec.image_id
            for name, spec in plan.pool_specs.items()
            if spec.image_id
        },
        pool_options={
            name: {
                "maximum_unavailable": spec.maximum_unavailable,
                "maximum_surge": spec.maximum_surge,
                "blue_green_name": spec.blue_green_name,
                "blue_green_compute_cluster_id": (
                    spec.blue_green_compute_cluster_id
                ),
                "blue_green_gpu_memory_fabric_id": (
                    spec.blue_green_gpu_memory_fabric_id
                ),
            }
            for name, spec in plan.pool_specs.items()
        },
    )


def execute_upgrade_apply(
    service: DiscoveryService,
    plan: ClusterUpgradePlan,
    *,
    acknowledge_application_compatibility: bool,
    acknowledge_iac_drift: bool,
    acknowledge_workloads_drained: bool,
    emergency_ack_unverified_drain: bool = False,
    timeout_seconds: int = 7200,
    poll_interval_seconds: int = 30,
    progress: Progress | None = None,
) -> list[UpgradeExecutionResult]:
    _require_acknowledgements(
        acknowledge_application_compatibility,
        acknowledge_iac_drift,
    )
    kubernetes = service.kubernetes_backend()
    checkpoint = checkpoint_from_plan(plan).replace(
        acknowledged_application_compatibility=True,
        acknowledged_iac_drift=True,
    )
    results: list[UpgradeExecutionResult] = []
    with mutation_lock(service, True, timeout_seconds):
        existing = kubernetes.read_upgrade_checkpoint()
        resource_version = None
        if existing:
            previous, resource_version = existing
            if previous.phase not in {
                UpgradePhase.ABANDONED,
                UpgradePhase.COMPLETED,
            }:
                raise WorkflowError(
                    f"Upgrade operation {previous.operation_id} is already active "
                    f"in phase {previous.phase.value}; use upgrades resume or abandon."
                )
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )
        try:
            checkpoint, resource_version, continued = (
                _continue_checkpoint(
                    service,
                    checkpoint,
                    resource_version,
                    acknowledge_workloads_drained=(
                        acknowledge_workloads_drained
                    ),
                    emergency_ack_unverified_drain=(
                        emergency_ack_unverified_drain
                    ),
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    progress=progress,
                )
            )
        except Exception as exc:
            _record_checkpoint_failure(
                kubernetes,
                checkpoint.operation_id,
                exc,
            )
            raise
        results.extend(continued)
    return results


def resume_upgrade(
    service: DiscoveryService,
    *,
    acknowledge_workloads_drained: bool,
    emergency_ack_unverified_drain: bool = False,
    timeout_seconds: int = 7200,
    poll_interval_seconds: int = 30,
    progress: Progress | None = None,
) -> list[UpgradeExecutionResult]:
    _require_upgrade_access(service)
    kubernetes = service.kubernetes_backend()
    results: list[UpgradeExecutionResult] = []
    with mutation_lock(service, True, timeout_seconds):
        record = kubernetes.read_upgrade_checkpoint()
        if record is None:
            raise WorkflowNotFound("No upgrade checkpoint exists.")
        checkpoint, resource_version = record
        if checkpoint.phase == UpgradePhase.ABANDONED:
            raise WorkflowError(
                f"Upgrade operation {checkpoint.operation_id} was abandoned."
            )
        if checkpoint.phase == UpgradePhase.COMPLETED:
            return [
                UpgradeExecutionResult(
                    operation="cluster-upgrade",
                    target=checkpoint.target_version,
                    status="completed",
                )
            ]
        try:
            checkpoint, resource_version, continued = (
                _continue_checkpoint(
                    service,
                    checkpoint,
                    resource_version,
                    acknowledge_workloads_drained=(
                        acknowledge_workloads_drained
                    ),
                    emergency_ack_unverified_drain=(
                        emergency_ack_unverified_drain
                    ),
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    progress=progress,
                )
            )
        except Exception as exc:
            _record_checkpoint_failure(
                kubernetes,
                checkpoint.operation_id,
                exc,
            )
            raise
        results.extend(continued)
    return results


def abandon_upgrade(service: DiscoveryService) -> UpgradeExecutionResult:
    _require_upgrade_access(service)
    kubernetes = service.kubernetes_backend()
    with mutation_lock(service, True, 300):
        record = kubernetes.read_upgrade_checkpoint()
        if record is None:
            raise WorkflowNotFound("No upgrade checkpoint exists.")
        checkpoint, resource_version = record
        if checkpoint.phase == UpgradePhase.COMPLETED:
            raise WorkflowError(
                "A completed upgrade cannot be abandoned; use upgrades cleanup."
            )
        checkpoint = checkpoint.replace(phase=UpgradePhase.ABANDONED)
        kubernetes.write_upgrade_checkpoint(checkpoint, resource_version)
    return UpgradeExecutionResult(
        operation="cluster-upgrade-abandon",
        target=checkpoint.target_version,
        status="abandoned",
        action=(
            "No OCI or Kubernetes resource was rolled back. Inspect observed "
            "state before starting another operation."
        ),
    )


def _record_checkpoint_failure(
    kubernetes: Any,
    operation_id: str,
    error: Exception,
) -> None:
    try:
        record = kubernetes.read_upgrade_checkpoint()
    except Exception:
        return
    if record is None:
        return
    checkpoint, resource_version = record
    if checkpoint.operation_id != operation_id:
        return
    failed = checkpoint.replace(
        phase=UpgradePhase.FAILED,
        error=str(error),
    )
    try:
        kubernetes.write_upgrade_checkpoint(failed, resource_version)
    except Exception:
        return


def cleanup_upgrade(service: DiscoveryService) -> UpgradeExecutionResult:
    _require_upgrade_access(service)
    kubernetes = service.kubernetes_backend()
    deleted: list[str] = []
    with mutation_lock(service, True, 1800):
        record = kubernetes.read_upgrade_checkpoint()
        if record is None:
            raise WorkflowNotFound("No upgrade checkpoint exists.")
        checkpoint, resource_version = record
        if checkpoint.phase != UpgradePhase.COMPLETED:
            raise WorkflowError(
                "Upgrade cleanup is permitted only after successful completion."
            )
        backend = service.oci_backend()
        for pool in checkpoint.pools:
            for configuration_id in (
                pool.superseded_instance_configuration_ids
            ):
                backend.delete_mgmt_created_instance_configuration(
                    configuration_id,
                    operation_id=checkpoint.operation_id,
                )
                deleted.append(configuration_id)
        kubernetes.delete_upgrade_checkpoint(resource_version)
    return UpgradeExecutionResult(
        operation="cluster-upgrade-cleanup",
        target=checkpoint.target_version,
        status="completed",
        retained_resource_ids=(),
        action=(
            f"Deleted {len(deleted)} superseded mgmt-oke Instance "
            "Configurations and removed the checkpoint."
        ),
    )


def _prepare_worker_configurations(
    service: DiscoveryService,
    checkpoint: UpgradeCheckpoint,
    resource_version: str,
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> tuple[UpgradeCheckpoint, str, list[UpgradeExecutionResult]]:
    kubernetes = service.kubernetes_backend()
    backend = service.oci_backend()
    results: list[UpgradeExecutionResult] = []
    target = KubernetesVersion.parse(
        checkpoint.target_version
    ).require_exact()
    for index, pool_state in enumerate(checkpoint.pools):
        if pool_state.phase in {
            "configured",
            "upgrading",
            "completed",
            "action-required",
        }:
            continue
        snapshot = service.discover()
        pool = snapshot.pool_by_name(pool_state.name)
        if pool is None:
            raise WorkflowNotFound(
                f"Checkpoint pool no longer exists: {pool_state.name}"
            )
        spec = _checkpoint_pool_spec(pool_state)
        work_requests = list(pool_state.work_request_ids)
        created_ids = list(pool_state.created_resource_ids)
        if pool.kind == "node-pool":
            if pool_state.strategy != "blue-green":
                if not pool.node_pool_id:
                    raise WorkflowError(
                        f"Managed pool {pool.name} has no node pool OCID."
                    )
                details, etag, preview = (
                    backend.preview_managed_pool_upgrade(
                        pool.node_pool_id,
                        str(target),
                        strategy=pool_state.strategy,
                        image_id=spec.image_id,
                        maximum_unavailable=spec.maximum_unavailable,
                        maximum_surge=spec.maximum_surge,
                        enable_cycling=False,
                    )
                )
                already_configured = (
                    pool.kubernetes_version == str(target)
                    and (
                        not spec.image_id
                        or preview.get("current_image_id") == spec.image_id
                    )
                )
                if not already_configured:
                    work_request_id = backend.upgrade_managed_pool(
                        pool.node_pool_id,
                        details,
                        etag,
                    )
                    if work_request_id:
                        work_requests.append(work_request_id)
                    updated_state = replace(
                        pool_state,
                        phase="configuring",
                        work_request_ids=tuple(
                            dict.fromkeys(work_requests)
                        ),
                    )
                    checkpoint = _replace_checkpoint_pool(
                        checkpoint,
                        index,
                        updated_state,
                    ).replace(
                        phase=UpgradePhase.WORKER_CONFIGS,
                        active_work_request_id=work_request_id,
                    )
                    resource_version = (
                        kubernetes.write_upgrade_checkpoint(
                            checkpoint,
                            resource_version,
                        )
                    )
                    _wait_for_work_request(
                        service,
                        work_request_id,
                        _require_cluster(snapshot).compartment_id,
                        timeout_seconds,
                        poll_interval_seconds,
                        progress,
                    )
                    _wait_for_worker_configuration(
                        service,
                        pool.name,
                        str(target),
                        None,
                        timeout_seconds,
                        poll_interval_seconds,
                        progress,
                    )
            configured_state = replace(
                pool_state,
                phase="configured",
                work_request_ids=tuple(dict.fromkeys(work_requests)),
            )
        else:
            if not pool.instance_configuration_id:
                raise WorkflowError(
                    f"Self-managed pool {pool.name} has no Instance Configuration."
                )
            connection_data = (
                service.kubernetes_backend().cluster_connection_data()
            )
            target_configuration_id = (
                pool_state.target_instance_configuration_id
            )
            if not target_configuration_id:
                target_configuration_id = (
                    backend.create_upgrade_instance_configuration(
                        pool.instance_configuration_id,
                        str(target),
                        operation_id=checkpoint.operation_id,
                        api_server=connection_data[0],
                        cluster_ca=connection_data[1],
                        image_id=spec.image_id,
                        availability_domain=pool.availability_domain,
                    )
                )
                created_ids.append(target_configuration_id)
                pool_state = replace(
                    pool_state,
                    phase="configuring",
                    previous_instance_configuration_id=(
                        pool.instance_configuration_id
                    ),
                    target_instance_configuration_id=(
                        target_configuration_id
                    ),
                    created_resource_ids=tuple(
                        dict.fromkeys(created_ids)
                    ),
                )
                checkpoint = _replace_checkpoint_pool(
                    checkpoint,
                    index,
                    pool_state,
                ).replace(phase=UpgradePhase.WORKER_CONFIGS)
                resource_version = kubernetes.write_upgrade_checkpoint(
                    checkpoint,
                    resource_version,
                )
            if (
                pool_state.strategy != "blue-green"
                and pool.instance_configuration_id
                != target_configuration_id
            ):
                work_request_id = (
                    backend.attach_upgrade_instance_configuration(
                        pool,
                        target_configuration_id,
                    )
                )
                if work_request_id:
                    work_requests.append(work_request_id)
                pool_state = replace(
                    pool_state,
                    phase="configuring",
                    work_request_ids=tuple(
                        dict.fromkeys(work_requests)
                    ),
                )
                checkpoint = _replace_checkpoint_pool(
                    checkpoint,
                    index,
                    pool_state,
                ).replace(
                    phase=UpgradePhase.WORKER_CONFIGS,
                    active_work_request_id=work_request_id,
                )
                resource_version = kubernetes.write_upgrade_checkpoint(
                    checkpoint,
                    resource_version,
                )
                _wait_for_work_request(
                    service,
                    work_request_id,
                    _require_cluster(snapshot).compartment_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress,
                )
                _wait_for_worker_configuration(
                    service,
                    pool.name,
                    str(target),
                    target_configuration_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress,
                )
            configured_state = replace(
                pool_state,
                phase="configured",
                previous_instance_configuration_id=(
                    pool_state.previous_instance_configuration_id
                    or pool.instance_configuration_id
                ),
                target_instance_configuration_id=(
                    target_configuration_id
                ),
                created_resource_ids=tuple(
                    dict.fromkeys(created_ids)
                ),
                work_request_ids=tuple(dict.fromkeys(work_requests)),
            )
        checkpoint = _replace_checkpoint_pool(
            checkpoint,
            index,
            configured_state,
        ).replace(
            phase=UpgradePhase.WORKER_CONFIGS,
            active_work_request_id=None,
        )
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )
        results.append(
            UpgradeExecutionResult(
                operation="worker-launch-configuration",
                target=pool.name,
                status="completed",
                work_request_ids=tuple(dict.fromkeys(work_requests)),
                created_resource_ids=tuple(dict.fromkeys(created_ids)),
                target_instance_configuration_id=(
                    configured_state.target_instance_configuration_id
                ),
            )
        )
    return checkpoint, resource_version, results


def _continue_checkpoint(
    service: DiscoveryService,
    checkpoint: UpgradeCheckpoint,
    resource_version: str,
    *,
    acknowledge_workloads_drained: bool,
    emergency_ack_unverified_drain: bool,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> tuple[UpgradeCheckpoint, str, list[UpgradeExecutionResult]]:
    kubernetes = service.kubernetes_backend()
    results: list[UpgradeExecutionResult] = []
    if checkpoint.active_work_request_id:
        compartment_id = service.resolve_oci_target(
            require_compartment=True
        ).compartment_id
        if not compartment_id:
            raise WorkflowError(
                "Resolved OCI target has no compartment OCID."
            )
        _wait_for_work_request(
            service,
            checkpoint.active_work_request_id,
            compartment_id,
            timeout_seconds,
            poll_interval_seconds,
            progress,
        )
        checkpoint = checkpoint.replace(active_work_request_id=None)
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )

    def observe_control_plane_work_request(
        work_request_id: str | None,
    ) -> None:
        nonlocal checkpoint, resource_version
        checkpoint = checkpoint.replace(
            phase=UpgradePhase.CONTROL_PLANE,
            active_work_request_id=work_request_id,
        )
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )

    for index in range(
        checkpoint.control_plane_index,
        len(checkpoint.control_plane_steps),
    ):
        step = checkpoint.control_plane_steps[index]
        snapshot = service.discover()
        cluster = _require_cluster(snapshot)
        observed_version = KubernetesVersion.parse(
            cluster.kubernetes_version
        ).require_exact()
        step_version = KubernetesVersion.parse(step).require_exact()
        if observed_version < step_version:
            prepared = prepare_control_plane_upgrade(
                service,
                step,
                allow_preview=KubernetesVersion.parse(step).preview,
            )
            result = execute_control_plane_upgrade(
                service,
                prepared,
                acknowledge_application_compatibility=(
                    checkpoint.acknowledged_application_compatibility
                ),
                acknowledge_iac_drift=checkpoint.acknowledged_iac_drift,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                lock=False,
                progress=progress,
                work_request_observer=(
                    observe_control_plane_work_request
                ),
            )
            results.append(result)
        elif observed_version == step_version:
            _wait_for_control_plane(
                service,
                step,
                timeout_seconds,
                poll_interval_seconds,
                progress,
            )
        elif observed_version > step_version:
            # The OCI mutation converged before its checkpoint write.
            pass
        checkpoint = checkpoint.replace(
            phase=UpgradePhase.CONTROL_PLANE,
            control_plane_index=index + 1,
            active_work_request_id=None,
        )
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )

    checkpoint = checkpoint.replace(phase=UpgradePhase.WORKER_CONFIGS)
    resource_version = kubernetes.write_upgrade_checkpoint(
        checkpoint,
        resource_version,
    )
    checkpoint, resource_version, configuration_results = (
        _prepare_worker_configurations(
            service,
            checkpoint,
            resource_version,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            progress=progress,
        )
    )
    results.extend(configuration_results)
    for index in range(checkpoint.pool_index, len(checkpoint.pool_order)):
        pool_state = checkpoint.pools[index]
        snapshot = service.discover()
        current_pool = snapshot.pool_by_name(pool_state.name)
        if pool_state.phase == "upgrading":
            target = KubernetesVersion.parse(
                checkpoint.target_version
            ).require_exact()
            if pool_state.strategy == "blue-green":
                if current_pool is not None:
                    target_name = _blue_green_name(
                        current_pool,
                        target,
                        _checkpoint_pool_spec(pool_state),
                    )
                    observed_target = _wait_for_pool_version(
                        service,
                        target_name,
                        checkpoint.target_version,
                        timeout_seconds,
                        poll_interval_seconds,
                        progress,
                    )
                    pool_state = replace(
                        pool_state,
                        phase="action-required",
                        created_resource_ids=tuple(
                            dict.fromkeys(
                                (
                                    *pool_state.created_resource_ids,
                                    *(
                                        (observed_target.backing_id,)
                                        if observed_target.backing_id
                                        else ()
                                    ),
                                )
                            )
                        ),
                    )
                    checkpoint = _replace_checkpoint_pool(
                        checkpoint,
                        index,
                        pool_state,
                    )
                    resource_version = (
                        kubernetes.write_upgrade_checkpoint(
                            checkpoint,
                            resource_version,
                        )
                    )
            elif (
                current_pool is not None
                and current_pool.kind == "node-pool"
            ):
                _wait_for_pool_version(
                    service,
                    current_pool.name,
                    checkpoint.target_version,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress,
                )
                pool_state = replace(pool_state, phase="completed")
                checkpoint = _replace_checkpoint_pool(
                    checkpoint,
                    index,
                    pool_state,
                ).replace(pool_index=index + 1)
                resource_version = (
                    kubernetes.write_upgrade_checkpoint(
                        checkpoint,
                        resource_version,
                    )
                )
                results.append(
                    UpgradeExecutionResult(
                        operation="worker-pool-upgrade",
                        target=pool_state.name,
                        status="completed",
                        work_request_ids=pool_state.work_request_ids,
                        action="Recovered from observed managed-pool state.",
                    )
                )
                continue
            elif (
                current_pool is not None
                and _pool_observed_at_target(
                    snapshot,
                    current_pool,
                    checkpoint.target_version,
                )
            ):
                pool_state = replace(pool_state, phase="completed")
                checkpoint = _replace_checkpoint_pool(
                    checkpoint,
                    index,
                    pool_state,
                ).replace(pool_index=index + 1)
                resource_version = (
                    kubernetes.write_upgrade_checkpoint(
                        checkpoint,
                        resource_version,
                    )
                )
                results.append(
                    UpgradeExecutionResult(
                        operation="worker-pool-upgrade",
                        target=pool_state.name,
                        status="completed",
                        work_request_ids=pool_state.work_request_ids,
                        action="Recovered from observed self-managed state.",
                    )
                )
                continue
        if pool_state.phase == "action-required":
            if current_pool is not None:
                checkpoint = checkpoint.replace(
                    phase=UpgradePhase.POOL_GATE,
                    pool_index=index,
                )
                resource_version = kubernetes.write_upgrade_checkpoint(
                    checkpoint,
                    resource_version,
                )
                results.append(
                    UpgradeExecutionResult(
                        operation="worker-pool-upgrade",
                        target=pool_state.name,
                        status="action-required",
                        created_resource_ids=pool_state.created_resource_ids,
                        action=(
                            "Blue-green target is ready. Complete external "
                            "migration and remove/finalize the retained old "
                            f"backend {pool_state.name}, then run upgrades resume."
                        ),
                    )
                )
                return checkpoint, resource_version, results
            checkpoint = _replace_checkpoint_pool(
                checkpoint,
                index,
                replace(pool_state, phase="completed"),
            )
            checkpoint = checkpoint.replace(pool_index=index + 1)
            resource_version = kubernetes.write_upgrade_checkpoint(
                checkpoint,
                resource_version,
            )
            continue
        if current_pool is None:
            raise WorkflowNotFound(
                f"Checkpoint pool no longer exists: {pool_state.name}"
            )
        spec = _checkpoint_pool_spec(pool_state)
        target_version = KubernetesVersion.parse(
            checkpoint.target_version
        ).require_exact()
        pool_prepared = _prepare_pool_upgrade_from_snapshot(
            service,
            snapshot,
            pool_state.name,
            target_version,
            spec,
            allow_future_control_plane=False,
            allow_current_target=True,
        )
        pool_prepared = replace(
            pool_prepared,
            target_instance_configuration_id=(
                pool_state.target_instance_configuration_id
            ),
        )
        try:
            validate_workload_gate(
                pool_prepared.evidence,
                acknowledged=acknowledge_workloads_drained,
                emergency_ack_unverified_drain=(
                    emergency_ack_unverified_drain
                ),
            )
        except UpgradeValidationError as exc:
            checkpoint = checkpoint.replace(
                phase=UpgradePhase.POOL_GATE,
                pool_index=index,
            )
            resource_version = kubernetes.write_upgrade_checkpoint(
                checkpoint,
                resource_version,
            )
            results.append(
                UpgradeExecutionResult(
                    operation="worker-pool-upgrade",
                    target=pool_state.name,
                    status="action-required",
                    action=str(exc),
                )
            )
            return checkpoint, resource_version, results
        checkpoint = checkpoint.replace(
            phase=UpgradePhase.POOL_UPGRADE,
            pool_index=index,
        )
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )

        def observe_pool_work_request(
            work_request_id: str | None,
        ) -> None:
            nonlocal checkpoint, resource_version
            current_state = checkpoint.pools[index]
            work_request_ids = list(current_state.work_request_ids)
            if work_request_id:
                work_request_ids.append(work_request_id)
            current_state = replace(
                current_state,
                phase="upgrading",
                work_request_ids=tuple(
                    dict.fromkeys(work_request_ids)
                ),
            )
            checkpoint = _replace_checkpoint_pool(
                checkpoint,
                index,
                current_state,
            ).replace(
                phase=UpgradePhase.POOL_UPGRADE,
                active_work_request_id=work_request_id,
            )
            resource_version = kubernetes.write_upgrade_checkpoint(
                checkpoint,
                resource_version,
            )

        def observe_pool_resources(
            resource_ids: tuple[str, ...],
            target_instance_configuration_id: str | None,
        ) -> None:
            nonlocal checkpoint, resource_version
            current_state = checkpoint.pools[index]
            current_state = replace(
                current_state,
                phase="upgrading",
                target_instance_configuration_id=(
                    target_instance_configuration_id
                    or current_state.target_instance_configuration_id
                ),
                created_resource_ids=tuple(
                    dict.fromkeys(
                        (
                            *current_state.created_resource_ids,
                            *resource_ids,
                        )
                    )
                ),
            )
            checkpoint = _replace_checkpoint_pool(
                checkpoint,
                index,
                current_state,
            ).replace(phase=UpgradePhase.POOL_UPGRADE)
            resource_version = kubernetes.write_upgrade_checkpoint(
                checkpoint,
                resource_version,
            )

        result = execute_pool_upgrade(
            service,
            pool_prepared,
            acknowledge_application_compatibility=(
                checkpoint.acknowledged_application_compatibility
            ),
            acknowledge_iac_drift=checkpoint.acknowledged_iac_drift,
            acknowledge_workloads_drained=True,
            emergency_ack_unverified_drain=(
                emergency_ack_unverified_drain
            ),
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            lock=False,
            progress=progress,
            operation_id=checkpoint.operation_id,
            work_request_observer=observe_pool_work_request,
            resource_observer=observe_pool_resources,
        )
        results.append(result)
        current_checkpoint_state = checkpoint.pools[index]
        new_pool_state = replace(
            current_checkpoint_state,
            phase=(
                "action-required"
                if result.status == "action-required"
                else "completed"
            ),
            target_instance_configuration_id=(
                result.target_instance_configuration_id
                or current_checkpoint_state.target_instance_configuration_id
            ),
            created_resource_ids=tuple(
                dict.fromkeys(
                    (
                        *current_checkpoint_state.created_resource_ids,
                        *result.created_resource_ids,
                    )
                )
            ),
            superseded_instance_configuration_ids=(
                current_checkpoint_state
                .superseded_instance_configuration_ids
            ),
            work_request_ids=tuple(
                dict.fromkeys(
                    (
                        *current_checkpoint_state.work_request_ids,
                        *result.work_request_ids,
                    )
                )
            ),
        )
        checkpoint = _replace_checkpoint_pool(
            checkpoint,
            index,
            new_pool_state,
        )
        if result.status == "action-required":
            checkpoint = checkpoint.replace(
                phase=UpgradePhase.POOL_GATE,
                pool_index=index,
            )
            resource_version = kubernetes.write_upgrade_checkpoint(
                checkpoint,
                resource_version,
            )
            return checkpoint, resource_version, results
        checkpoint = checkpoint.replace(pool_index=index + 1)
        resource_version = kubernetes.write_upgrade_checkpoint(
            checkpoint,
            resource_version,
        )

    checkpoint = checkpoint.replace(phase=UpgradePhase.VERIFY)
    resource_version = kubernetes.write_upgrade_checkpoint(
        checkpoint,
        resource_version,
    )
    _verify_cluster_target(service, checkpoint.target_version)
    checkpoint = checkpoint.replace(phase=UpgradePhase.COMPLETED)
    resource_version = kubernetes.write_upgrade_checkpoint(
        checkpoint,
        resource_version,
    )
    results.append(
        UpgradeExecutionResult(
            operation="cluster-upgrade",
            target=checkpoint.target_version,
            status="completed",
            action=(
                "OKE and worker convergence verified. Kueue and Slurm remain "
                "paused; resume them through their native operational workflow."
            ),
        )
    )
    return checkpoint, resource_version, results


def _replace_checkpoint_pool(
    checkpoint: UpgradeCheckpoint,
    index: int,
    state: UpgradePoolState,
) -> UpgradeCheckpoint:
    pools = list(checkpoint.pools)
    pools[index] = state
    return checkpoint.replace(pools=tuple(pools))


def _checkpoint_pool_spec(pool: UpgradePoolState) -> PoolUpgradeSpec:
    return PoolUpgradeSpec(
        strategy=pool.strategy,
        image_id=pool.image_id,
        maximum_unavailable=pool.maximum_unavailable,
        maximum_surge=pool.maximum_surge,
        blue_green_name=pool.blue_green_name,
        blue_green_compute_cluster_id=(
            pool.blue_green_compute_cluster_id
        ),
        blue_green_gpu_memory_fabric_id=(
            pool.blue_green_gpu_memory_fabric_id
        ),
    )


def _verify_cluster_target(
    service: DiscoveryService,
    target_version: str,
) -> None:
    snapshot = service.discover()
    cluster = _require_cluster(snapshot)
    if (
        cluster.kubernetes_version != target_version
        or (cluster.lifecycle_state or "").upper() != "ACTIVE"
    ):
        raise WorkflowError(
            "Final control-plane state is "
            f"{cluster.kubernetes_version}/{cluster.lifecycle_state or 'UNKNOWN'}, "
            f"expected {target_version}/ACTIVE."
        )
    mismatched = [
        node.k8s_name
        for node in snapshot.nodes
        if not node.ready or node.kubelet_version != target_version
    ]
    if mismatched:
        raise WorkflowError(
            "Final worker verification failed for: "
            + ", ".join(sorted(mismatched))
        )
    dependent_issues = _control_plane_dependent_issues(
        service,
        snapshot,
        target_version,
    )
    if dependent_issues:
        raise WorkflowError(
            "Final virtual-pool or add-on verification failed: "
            + "; ".join(dependent_issues)
        )
    slinky_issues = [
        issue
        for pool in snapshot.pools
        for issue in _slinky_registration_issues(service, snapshot, pool)
    ]
    if slinky_issues:
        raise WorkflowError(
            "Final Slinky registration verification failed: "
            + "; ".join(slinky_issues)
        )


def _execute_managed_pool_upgrade(
    service: DiscoveryService,
    prepared: PreparedPoolUpgrade,
    operation_id: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
    work_request_observer: WorkRequestObserver | None = None,
    resource_observer: ResourceObserver | None = None,
) -> UpgradeExecutionResult:
    backend = service.oci_backend()
    pool = prepared.pool
    if prepared.strategy == "blue-green":
        cluster = _require_cluster(prepared.snapshot)
        name = _blue_green_name(
            pool,
            KubernetesVersion.parse(prepared.target_version),
            prepared.spec,
        )
        created = backend.create_managed_blue_green_pool(
            pool,
            cluster_id=cluster.cluster_id,
            compartment_id=cluster.compartment_id,
            target_version=prepared.target_version,
            name=name,
            image_id=prepared.spec.image_id,
            operation_id=operation_id,
        )
        if resource_observer:
            resource_observer(
                (created.node_pool_id,) if created.node_pool_id else (),
                None,
            )
        if work_request_observer:
            work_request_observer(created.work_request_id)
        _wait_for_work_request(
            service,
            created.work_request_id,
            cluster.compartment_id,
            timeout_seconds,
            poll_interval_seconds,
            progress,
        )
        if work_request_observer:
            work_request_observer(None)
        observed_target = _wait_for_pool_version(
            service,
            name,
            prepared.target_version,
            timeout_seconds,
            poll_interval_seconds,
            progress,
        )
        created_node_pool_id = (
            observed_target.node_pool_id or created.node_pool_id
        )
        if resource_observer:
            resource_observer(
                (created_node_pool_id,) if created_node_pool_id else (),
                None,
            )
        return UpgradeExecutionResult(
            operation="worker-pool-upgrade",
            target=pool.name,
            status="action-required",
            work_request_ids=(
                (created.work_request_id,)
                if created.work_request_id
                else ()
            ),
            created_resource_ids=(
                (created_node_pool_id,) if created_node_pool_id else ()
            ),
            retained_resource_ids=(pool.node_pool_id,)
            if pool.node_pool_id
            else (),
            action=(
                f"Migrate workloads to {name}, externally drain and explicitly "
                f"remove or finalize {pool.name}, then resume the checkpoint. "
                "The old pool is retained until that action is complete."
            ),
        )
    if not prepared.managed_details or not prepared.managed_etag:
        raise WorkflowError("Managed pool upgrade preflight did not return an ETag.")
    if not pool.node_pool_id:
        raise WorkflowError(f"Managed pool {pool.name} has no node pool OCID.")
    work_request_id = backend.upgrade_managed_pool(
        pool.node_pool_id,
        prepared.managed_details,
        prepared.managed_etag,
    )
    if work_request_observer:
        work_request_observer(work_request_id)
    cluster = _require_cluster(prepared.snapshot)
    _wait_for_work_request(
        service,
        work_request_id,
        cluster.compartment_id,
        timeout_seconds,
        poll_interval_seconds,
        progress,
    )
    if work_request_observer:
        work_request_observer(None)
    _wait_for_pool_version(
        service,
        pool.name,
        prepared.target_version,
        timeout_seconds,
        poll_interval_seconds,
        progress,
    )
    return UpgradeExecutionResult(
        operation="worker-pool-upgrade",
        target=pool.name,
        status="completed",
        work_request_ids=(work_request_id,) if work_request_id else (),
    )


def _execute_self_managed_pool_upgrade(
    service: DiscoveryService,
    prepared: PreparedPoolUpgrade,
    operation_id: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
    work_request_observer: WorkRequestObserver | None = None,
    resource_observer: ResourceObserver | None = None,
) -> UpgradeExecutionResult:
    backend = service.oci_backend()
    pool = prepared.pool
    if not pool.instance_configuration_id or not prepared.connection_data:
        raise WorkflowError(
            "Self-managed upgrade preflight did not return bootstrap inputs."
        )
    target_configuration_id = prepared.target_instance_configuration_id
    if not target_configuration_id:
        target_configuration_id = (
            backend.create_upgrade_instance_configuration(
                pool.instance_configuration_id,
                prepared.target_version,
                operation_id=operation_id,
                api_server=prepared.connection_data[0],
                cluster_ca=prepared.connection_data[1],
                image_id=prepared.spec.image_id,
                availability_domain=pool.availability_domain,
            )
        )
        if resource_observer:
            resource_observer(
                (target_configuration_id,),
                target_configuration_id,
            )
    created_ids = [target_configuration_id]
    work_requests: list[str] = []
    if prepared.strategy == "blue-green":
        name = _blue_green_name(
            pool,
            KubernetesVersion.parse(prepared.target_version),
            prepared.spec,
        )
        if pool.kind == "cluster-network":
            created = backend.create_cluster_network_blue_green_pool(
                pool,
                target_version=prepared.target_version,
                name=name,
                image_id=prepared.spec.image_id,
                operation_id=operation_id,
                target_instance_configuration_id=(
                    target_configuration_id
                ),
            )
            created_ids.extend(
                [
                    created.cluster_network_id,
                    created.instance_configuration_id,
                ]
            )
            if created.work_request_id:
                work_requests.append(created.work_request_id)
        elif pool.kind == "instance-pool":
            resource_id, request_id = backend.create_instance_pool_blue_green(
                pool,
                target_instance_configuration_id=target_configuration_id,
                name=name,
                operation_id=operation_id,
            )
            created_ids.append(resource_id)
            if request_id:
                work_requests.append(request_id)
        elif pool.kind == "gpu-memory-cluster":
            resource_id, request_id = (
                backend.create_gpu_memory_cluster_blue_green(
                    pool,
                    target_instance_configuration_id=target_configuration_id,
                    name=name,
                    operation_id=operation_id,
                    compute_cluster_id=(
                        prepared.spec.blue_green_compute_cluster_id
                    ),
                    gpu_memory_fabric_id=(
                        prepared.spec.blue_green_gpu_memory_fabric_id
                    ),
                )
            )
            created_ids.append(resource_id)
            if request_id:
                work_requests.append(request_id)
        else:
            raise WorkflowError(
                f"Blue-green is not supported for backend {pool.kind}."
            )
        if resource_observer:
            resource_observer(
                tuple(dict.fromkeys(created_ids)),
                target_configuration_id,
            )
        for request_id in work_requests:
            if work_request_observer:
                work_request_observer(request_id)
            _wait_for_work_request(
                service,
                request_id,
                _require_cluster(prepared.snapshot).compartment_id,
                timeout_seconds,
                poll_interval_seconds,
                progress,
            )
            if work_request_observer:
                work_request_observer(None)
        observed_target = _wait_for_pool_version(
            service,
            name,
            prepared.target_version,
            timeout_seconds,
            poll_interval_seconds,
            progress,
        )
        if observed_target.backing_id:
            created_ids.append(observed_target.backing_id)
        if resource_observer:
            resource_observer(
                tuple(dict.fromkeys(created_ids)),
                target_configuration_id,
            )
        return UpgradeExecutionResult(
            operation="worker-pool-upgrade",
            target=pool.name,
            status="action-required",
            work_request_ids=tuple(work_requests),
            created_resource_ids=tuple(dict.fromkeys(created_ids)),
            retained_resource_ids=tuple(
                item
                for item in (pool.backing_id, pool.instance_configuration_id)
                if item
            ),
            action=(
                f"Migrate workloads to {name}, externally drain and explicitly "
                f"remove or finalize {pool.name}, then resume the checkpoint. "
                "The old backend is retained until that action is complete."
            ),
            target_instance_configuration_id=target_configuration_id,
        )

    request_id = None
    if pool.instance_configuration_id != target_configuration_id:
        request_id = backend.attach_upgrade_instance_configuration(
            pool,
            target_configuration_id,
        )
    if request_id:
        work_requests.append(request_id)
    if work_request_observer:
        work_request_observer(request_id)
    _wait_for_work_request(
        service,
        request_id,
        _require_cluster(prepared.snapshot).compartment_id,
        timeout_seconds,
        poll_interval_seconds,
        progress,
    )
    if work_request_observer:
        work_request_observer(None)
    pool_nodes = [
        node
        for node in prepared.snapshot.nodes
        if node.pool_name == pool.name and node.instance_ocid
    ]
    original_nodes = {
        instance_id: node
        for node in pool_nodes
        if node.kubelet_version != prepared.target_version
        if (instance_id := node.instance_ocid)
    }
    if prepared.strategy == "boot-volume-replace":
        for instance_id, node in original_nodes.items():
            backend.replace_self_managed_instance_boot_volume(
                instance_id,
                target_configuration_id,
            )
            _wait_for_preserved_node(
                service,
                pool.name,
                instance_id,
                node.boot_id,
                prepared.target_version,
                timeout_seconds,
                poll_interval_seconds,
                progress,
            )
    elif prepared.strategy == "instance-replace":
        desired = pool.desired_size or len(original_nodes)
        known_ids = {
            node.instance_ocid
            for node in pool_nodes
            if node.instance_ocid
        }
        for instance_id in tuple(original_nodes):
            resize_request = backend.resize_upgrade_backend(pool, desired + 1)
            if resize_request:
                work_requests.append(resize_request)
            if work_request_observer:
                work_request_observer(resize_request)
            _wait_for_work_request(
                service,
                resize_request,
                _require_cluster(prepared.snapshot).compartment_id,
                timeout_seconds,
                poll_interval_seconds,
                progress,
            )
            if work_request_observer:
                work_request_observer(None)
            replacement_id = _wait_for_new_pool_node(
                service,
                pool.name,
                known_ids,
                prepared.target_version,
                timeout_seconds,
                poll_interval_seconds,
                progress,
            )
            known_ids.add(replacement_id)
            if pool.kind in {"cluster-network", "instance-pool"}:
                if not pool.instance_pool_id:
                    raise WorkflowError(
                        f"Pool {pool.name} has no Instance Pool OCID."
                    )
                remove_request = backend.detach_instance_pool_node(
                    pool.instance_pool_id,
                    instance_id,
                    decrement_size=True,
                )
                if remove_request:
                    work_requests.append(remove_request)
                if work_request_observer:
                    work_request_observer(remove_request)
                _wait_for_work_request(
                    service,
                    remove_request,
                    _require_cluster(prepared.snapshot).compartment_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress,
                )
                if work_request_observer:
                    work_request_observer(None)
            else:
                backend.terminate_upgrade_instance(instance_id)
                resize_request = backend.resize_upgrade_backend(pool, desired)
                if resize_request:
                    work_requests.append(resize_request)
                if work_request_observer:
                    work_request_observer(resize_request)
                _wait_for_work_request(
                    service,
                    resize_request,
                    _require_cluster(prepared.snapshot).compartment_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress,
                )
                if work_request_observer:
                    work_request_observer(None)
            known_ids.discard(instance_id)
    else:
        raise WorkflowError(f"Unsupported self-managed strategy: {prepared.strategy}")
    _wait_for_pool_version(
        service,
        pool.name,
        prepared.target_version,
        timeout_seconds,
        poll_interval_seconds,
        progress,
    )
    return UpgradeExecutionResult(
        operation="worker-pool-upgrade",
        target=pool.name,
        status="completed",
        work_request_ids=tuple(dict.fromkeys(work_requests)),
        created_resource_ids=tuple(created_ids),
        retained_resource_ids=(pool.instance_configuration_id,),
        target_instance_configuration_id=target_configuration_id,
    )


def _wait_for_work_request(
    service: DiscoveryService,
    work_request_id: str | None,
    compartment_id: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> None:
    if not work_request_id:
        return
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = service.oci_backend().get_work_request_status(
            work_request_id,
            compartment_id,
        )
        normalized = status.status.upper()
        if normalized in {"SUCCEEDED", "SUCCESS"}:
            return
        if status.failed:
            details = "; ".join(status.errors) or "no service error details"
            raise WorkflowError(
                f"OCI work request {work_request_id} failed: {details}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for OCI work request {work_request_id}."
            )
        if progress:
            progress(
                f"work request {work_request_id} is {status.status} "
                f"({status.percent_complete or 0:g}%)"
            )
        time.sleep(poll_interval_seconds)


def _wait_for_control_plane(
    service: DiscoveryService,
    target: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> None:
    cluster_id = service.resolve_oci_target(require_cluster=True).cluster_id
    compartment_id = service.resolve_oci_target(
        require_compartment=True
    ).compartment_id
    if not cluster_id or not compartment_id:
        raise WorkflowError(
            "Resolved OCI target is missing its cluster or compartment OCID."
        )
    deadline = time.monotonic() + timeout_seconds
    while True:
        cluster = service.oci_backend().get_cluster_info(
            cluster_id,
            compartment_id,
        )
        dependent_issues: tuple[str, ...] = ()
        if (
            cluster.kubernetes_version == target
            and (cluster.lifecycle_state or "").upper() == "ACTIVE"
        ):
            snapshot = service.discover()
            dependent_issues = _control_plane_dependent_issues(
                service,
                snapshot,
                target,
            )
            if not dependent_issues:
                return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for control plane {target}; observed "
                f"{cluster.kubernetes_version}/{cluster.lifecycle_state}; "
                f"dependent state: "
                f"{'; '.join(dependent_issues) or 'not ready'}."
            )
        if progress:
            status = (
                f"control plane is {cluster.kubernetes_version}/"
                f"{cluster.lifecycle_state or 'UNKNOWN'}"
            )
            if dependent_issues:
                status += "; " + "; ".join(dependent_issues)
            progress(status)
        time.sleep(poll_interval_seconds)


def _wait_for_worker_configuration(
    service: DiscoveryService,
    pool_name: str,
    target_version: str,
    target_instance_configuration_id: str | None,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = service.discover()
        pool = snapshot.pool_by_name(pool_name)
        version_ready = bool(
            pool and pool.kubernetes_version == target_version
        )
        configuration_ready = bool(
            pool
            and (
                target_instance_configuration_id is None
                or pool.instance_configuration_id
                == target_instance_configuration_id
            )
        )
        if version_ready and configuration_ready:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for pool {pool_name} launch "
                f"configuration {target_version}; observed version="
                f"{getattr(pool, 'kubernetes_version', None)} "
                f"instance_configuration_id="
                f"{getattr(pool, 'instance_configuration_id', None)}."
            )
        if progress:
            progress(
                f"pool {pool_name} launch configuration: version="
                f"{getattr(pool, 'kubernetes_version', None)} "
                f"instance-configuration="
                f"{getattr(pool, 'instance_configuration_id', None)}"
            )
        time.sleep(poll_interval_seconds)


def _wait_for_pool_version(
    service: DiscoveryService,
    pool_name: str,
    target: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> WorkerPoolInfo:
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = service.discover()
        pool = snapshot.pool_by_name(pool_name)
        nodes = [
            node for node in snapshot.nodes if node.pool_name == pool_name
        ]
        version_ready = bool(nodes) and all(
            node.kubelet_version == target for node in nodes
        )
        counts_ready = bool(
            pool
            and pool.desired_size is not None
            and pool.active_oci_instances == pool.desired_size
            and pool.ready_k8s_nodes == pool.desired_size
        )
        resources_ready = bool(
            pool
            and pool.desired_size is not None
            and resource_counts_match(
                pool_resource_readiness(snapshot, pool),
                pool.desired_size,
            )
        )
        declared_ready = bool(
            pool
            and (
                pool.kubernetes_version is None
                or pool.kubernetes_version == target
            )
        )
        slinky_issues: tuple[str, ...] = ()
        if (
            pool
            and version_ready
            and counts_ready
            and resources_ready
            and declared_ready
        ):
            slinky_issues = _slinky_registration_issues(
                service,
                snapshot,
                pool,
            )
            if not slinky_issues:
                return pool
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for pool {pool_name} to converge at "
                f"{target}; Slinky state: "
                f"{'; '.join(slinky_issues) or 'not ready'}."
            )
        if progress:
            status = (
                f"pool {pool_name}: declared={getattr(pool, 'kubernetes_version', None)} "
                f"nodes={len(nodes)} target-kubelets="
                f"{sum(node.kubelet_version == target for node in nodes)}"
            )
            if slinky_issues:
                status += "; " + "; ".join(slinky_issues)
            progress(status)
        time.sleep(poll_interval_seconds)


def _pool_observed_at_target(
    snapshot: DiscoverySnapshot,
    pool: WorkerPoolInfo,
    target_version: str,
) -> bool:
    nodes = [
        node for node in snapshot.nodes if node.pool_name == pool.name
    ]
    desired = pool.desired_size
    return bool(
        desired is not None
        and len(nodes) == desired
        and pool.active_oci_instances == desired
        and pool.ready_k8s_nodes == desired
        and all(
            node.ready and node.kubelet_version == target_version
            for node in nodes
        )
        and (
            pool.kubernetes_version is None
            or pool.kubernetes_version == target_version
        )
        and resource_counts_match(
            pool_resource_readiness(snapshot, pool),
            desired,
        )
    )


def _control_plane_dependent_issues(
    service: DiscoveryService,
    snapshot: DiscoverySnapshot,
    target_version: str,
) -> tuple[str, ...]:
    issues: list[str] = []
    for virtual in snapshot.virtual_pools:
        state = (virtual.lifecycle_state or "").upper()
        if virtual.kubernetes_version != target_version or state != "ACTIVE":
            issues.append(
                f"virtual pool {virtual.name} is "
                f"{virtual.kubernetes_version or 'unknown'}/"
                f"{virtual.lifecycle_state or 'UNKNOWN'}"
            )
    try:
        compatibility = service.oci_backend().get_addon_compatibility(
            target_version,
            snapshot.addons,
        )
    except Exception as exc:
        issues.append(f"add-on verification unavailable: {exc}")
        return tuple(issues)
    for addon in compatibility:
        if not addon.compatible:
            issues.append(
                f"add-on {addon.name}: "
                f"{addon.reason or 'not compatible'}"
            )
            continue
        if (
            (addon.update_mode or "").upper() in {"AUTOMATIC", "AUTO"}
            and addon.installed_version not in addon.supported_versions
        ):
            issues.append(
                f"automatic add-on {addon.name} is still "
                f"{addon.installed_version or 'uninstalled'}"
            )
    return tuple(issues)


def _slinky_registration_issues(
    service: DiscoveryService,
    snapshot: DiscoverySnapshot,
    pool: WorkerPoolInfo,
) -> tuple[str, ...]:
    nodes = [node for node in snapshot.nodes if node.pool_name == pool.name]
    if not pool.slinky_managed and not any(
        node.slinky_managed for node in nodes
    ):
        return ()
    issues: list[str] = []
    for node in nodes:
        if not node.slurm_name:
            issues.append(f"{node.k8s_name} has no Slurm node name")
            continue
        try:
            record = service.kubernetes_backend().exec_slurmctld(
                ("scontrol", "show", "node", node.slurm_name, "--oneliner")
            )
        except Exception as exc:
            issues.append(f"{node.slurm_name} lookup failed: {exc}")
            continue
        discovered_name = parse_slurm_record(record).get("NodeName")
        if discovered_name != node.slurm_name:
            issues.append(
                f"{node.slurm_name} is not registered with slurmctld"
            )
    return tuple(issues)


def _wait_for_preserved_node(
    service: DiscoveryService,
    pool_name: str,
    instance_id: str,
    previous_boot_id: str | None,
    target: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = service.discover()
        node = next(
            (
                item
                for item in snapshot.nodes
                if item.pool_name == pool_name
                and item.instance_ocid == instance_id
            ),
            None,
        )
        if (
            node
            and node.ready
            and node.kubelet_version == target
            and (
                previous_boot_id is None
                or (
                    node.boot_id is not None
                    and node.boot_id != previous_boot_id
                )
            )
        ):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for preserved instance {instance_id} "
                f"to return at {target}."
            )
        if progress:
            progress(f"preserved worker {instance_id} is restarting")
        time.sleep(poll_interval_seconds)


def _wait_for_new_pool_node(
    service: DiscoveryService,
    pool_name: str,
    known_ids: set[str],
    target: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Progress | None,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = service.discover()
        candidates = [
            node
            for node in snapshot.nodes
            if (
                node.pool_name == pool_name
                and node.instance_ocid
                and node.instance_ocid not in known_ids
                and node.ready
                and node.kubelet_version == target
            )
        ]
        if len(candidates) == 1:
            return str(candidates[0].instance_ocid)
        if len(candidates) > 1:
            raise WorkflowError(
                f"More than one replacement worker appeared in {pool_name}; "
                "refusing ambiguous old/new pairing."
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for a target-version replacement in {pool_name}."
            )
        if progress:
            progress(f"waiting for one new {target} worker in {pool_name}")
        time.sleep(poll_interval_seconds)


def _require_upgrade_access(service: DiscoveryService) -> None:
    if service.options.skip_oci or service.options.auth == "none":
        raise WorkflowError("Kubernetes upgrades require OCI API access.")
    if service.options.skip_kubernetes:
        raise WorkflowError(
            "Kubernetes upgrades require Kubernetes API access for safety checks."
        )
    service.resolve_oci_target(
        require_compartment=True,
        require_cluster=True,
    )


def _require_cluster(snapshot: DiscoverySnapshot):
    if snapshot.cluster is None:
        raise WorkflowError(
            "OKE control-plane discovery is required for upgrade management."
        )
    return snapshot.cluster


def _validate_current_skew(snapshot: DiscoverySnapshot) -> None:
    cluster = _require_cluster(snapshot)
    for node in snapshot.nodes:
        if not node.kubelet_version:
            raise WorkflowError(
                f"Node {node.k8s_name} did not report a kubelet version."
            )
        validate_worker_skew(
            cluster.kubernetes_version,
            node.kubelet_version,
        )


def _require_acknowledgements(
    application_compatibility: bool,
    iac_drift: bool,
) -> None:
    missing = []
    if not application_compatibility:
        missing.append("--ack-application-compatibility")
    if not iac_drift:
        missing.append("--ack-iac-drift")
    if missing:
        raise WorkflowError(
            "Upgrade execution requires separate safety acknowledgements: "
            + ", ".join(missing)
            + ". --yes does not replace them."
        )


def _resolve_pool_order(
    pools: list[WorkerPoolInfo],
    requested: Iterable[str],
) -> tuple[str, ...]:
    requested_tuple = tuple(requested)
    if not requested_tuple:
        return default_pool_order(pools)
    if len(set(requested_tuple)) != len(requested_tuple):
        raise WorkflowError("Pool upgrade order contains duplicate names.")
    available = {pool.name for pool in pools}
    unknown = set(requested_tuple) - available
    if unknown:
        raise WorkflowNotFound(
            "Pool upgrade order contains unknown pools: "
            + ", ".join(sorted(unknown))
        )
    omitted = available - set(requested_tuple)
    if omitted:
        raise WorkflowError(
            "Explicit pool order must include every discovered worker pool; "
            f"missing: {', '.join(sorted(omitted))}."
        )
    return requested_tuple


def _pool_strategy_steps(
    pool: WorkerPoolInfo,
    strategy: str,
) -> tuple[str, ...]:
    prefix = (
        "verify Ready, externally cordoned, workload-free worker state",
        "verify Kueue and Slinky state without scheduling mutations",
        "revalidate authoritative OCI state and ETag under the mutation Lease",
    )
    if pool.kind == "node-pool":
        if strategy == "blue-green":
            return prefix + (
                "clone the complete managed OKE pool at the target version",
                "wait for kubelet, GPU, and RDMA readiness",
                "pause for external workload migration; retain the old pool",
            )
        mode = (
            "BOOT_VOLUME_REPLACE"
            if strategy == "boot-volume-replace"
            else "INSTANCE_REPLACE"
        )
        return prefix + (
            f"call OKE UpdateNodePool with {mode}",
            "wait for OKE work request and target-version convergence",
        )
    common = prefix + (
        "clone and structurally update the complete Instance Configuration",
        "refresh API endpoint, cluster CA, and target bootstrap version",
        "attach the target Instance Configuration before cycling workers",
    )
    if strategy == "boot-volume-replace":
        return common + (
            "replace each boot volume sequentially while preserving instance identity",
            "retain previous boot volumes and Instance Configuration",
        )
    if strategy == "instance-replace":
        return common + (
            "add and verify one target-version worker before each termination",
            "terminate only the externally drained old worker",
        )
    return common + (
        "create a complete parallel backend",
        "wait for kubelet, GPU, and RDMA readiness",
        "pause for external workload migration; retain the old backend",
    )


def _blue_green_name(
    pool: WorkerPoolInfo,
    target: KubernetesVersion,
    spec: PoolUpgradeSpec,
) -> str:
    return spec.blue_green_name or (
        f"{pool.name}-v{target.major}-{target.minor}-{target.patch}"
    )
