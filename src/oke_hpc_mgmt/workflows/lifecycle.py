from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace

from oke_hpc_mgmt.backends.oci import (
    BootVolumeAttachmentPending,
    OciBackend,
)
from oke_hpc_mgmt.bootstrap import (
    BootstrapCompositionError,
    summarize_worker_bootstrap,
)
from oke_hpc_mgmt.discovery import DiscoveryService
from oke_hpc_mgmt.models import (
    ClusterNetworkCreateResult,
    CustomerReportedHostStatus,
    DiscoverySnapshot,
    DrainPod,
    ManagedNodePoolCreateResult,
    NodeInfo,
    OperationPlan,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
    PoolResourceReadiness,
    WorkerPoolInfo,
    WorkRequestInfo,
)
from oke_hpc_mgmt.selection import select_nodes
from oke_hpc_mgmt.validation import (
    normalize_pool_name,
    validate_eviction_grace_duration,
    validate_pool_boot_volume_replace_spec,
)

IAC_DRIFT_WARNING = (
    "This direct OCI mutation does not update Terraform or OCI Resource Manager "
    "input values; reconcile the declared pool size before the next apply."
)
IAC_CREATE_DRIFT_WARNING = (
    "This direct OCI mutation creates resources outside Terraform or OCI Resource "
    "Manager state; import or declare the new pool before the next apply."
)
INSTANCE_CONFIGURATION_DERIVATION_NOTICE = (
    "A new Instance Configuration is derived from the source; image, cloud-init, "
    "and OKE bootstrap settings are preserved while pool identity is updated."
)
MANAGED_POOL_DERIVATION_NOTICE = (
    "A new managed OKE node pool is derived from the source; OKE bootstrap, "
    "networking, labels, and lifecycle defaults are preserved unless overridden."
)
COMPUTE_CLUSTER_IAM_NOTICE = (
    "OKE requires a node-pool resource-principal policy granting "
    "COMPUTE_CLUSTER_LAUNCH_INSTANCE on the selected Compute Cluster."
)
HOST_GROUP_IAM_NOTICE = (
    "OKE requires a node-pool resource-principal policy granting "
    "HOST_GROUP_LAUNCH_INSTANCE on the selected Compute Host Group."
)
LEGACY_BOOTSTRAP_INHERITANCE_NOTICE = (
    "The managed pool will execute cloud-init inherited from a legacy RDMA "
    "Instance Configuration. Current managed OKE cluster identity, CNI, version, "
    "networking, and lifecycle settings remain authoritative."
)


class WorkflowError(RuntimeError):
    """Raised when a lifecycle operation cannot be performed safely."""


class WorkflowNotFound(WorkflowError):
    """Raised when an explicitly requested resource is not present."""


@dataclass(frozen=True)
class PreparedPoolResize:
    snapshot: DiscoverySnapshot
    pool: WorkerPoolInfo
    plan: OperationPlan


@dataclass(frozen=True)
class PreparedPoolDelete:
    snapshot: DiscoverySnapshot
    pool: WorkerPoolInfo
    nodes: tuple[NodeInfo, ...]
    drain_pods: dict[str, tuple[DrainPod, ...]]
    allow_workloads: bool
    delete_emptydir_data: bool
    force_unmanaged: bool
    plan: OperationPlan


@dataclass(frozen=True)
class PreparedPoolCreate:
    snapshot: DiscoverySnapshot
    source_pool: WorkerPoolInfo
    name: str
    count: int
    spec: PoolCreateSpec
    plan: OperationPlan
    bootstrap_source_pool: WorkerPoolInfo | None = None
    bootstrap_source_metadata: tuple[tuple[str, str], ...] | None = None


@dataclass(frozen=True)
class PreparedNodeRemoval:
    snapshot: DiscoverySnapshot
    nodes: tuple[NodeInfo, ...]
    pools: dict[str, WorkerPoolInfo]
    plans: tuple[OperationPlan, ...]
    drain_pods: dict[str, tuple[DrainPod, ...]]
    target_sizes: dict[str, int]
    decrement_size: bool
    host_tags: dict[str, CustomerReportedHostStatus] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedNodeBootVolumeReplace:
    snapshot: DiscoverySnapshot
    nodes: tuple[NodeInfo, ...]
    pools: dict[str, WorkerPoolInfo]
    plans: tuple[OperationPlan, ...]
    old_boot_volume_ids: dict[str, str]
    drain_pods: dict[str, tuple[DrainPod, ...]]
    delete_emptydir_data: bool
    force_unmanaged: bool
    allow_system_pool: bool
    eviction_grace_duration: str
    force_after_grace: bool


@dataclass(frozen=True)
class PreparedPoolBootVolumeReplace:
    snapshot: DiscoverySnapshot
    pool: WorkerPoolInfo
    nodes: tuple[NodeInfo, ...]
    old_boot_volume_ids: dict[str, str]
    drain_pods: dict[str, tuple[DrainPod, ...]]
    spec: PoolBootVolumeReplaceSpec
    delete_emptydir_data: bool
    force_unmanaged: bool
    allow_system_pool: bool
    plan: OperationPlan


@dataclass(frozen=True)
class ResourceWorkRequestWatch:
    compartment_id: str
    resource_id: str
    ignored_ids: frozenset[str]


def prepare_pool_create(
    service: DiscoveryService,
    name: str,
    count: int,
    spec: PoolCreateSpec | None = None,
    source_identifier: str | None = None,
    bootstrap_source_identifier: str | None = None,
) -> PreparedPoolCreate:
    spec = spec or PoolCreateSpec(pool_type="rdma")
    try:
        normalized_name = normalize_pool_name(name)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    if count < 1:
        raise WorkflowError("Pool count must be at least one.")
    if service.options.auth == "none" or service.options.skip_oci:
        raise WorkflowError(
            "Pool creation requires OCI discovery. Use instance-principal "
            "authentication on the operator host."
        )

    target = service.resolve_oci_target(
        require_compartment=True,
        require_cluster=True,
    )
    if not target.compartment_id or not target.cluster_id:
        raise WorkflowError("Pool creation requires an OKE cluster and compartment.")
    snapshot = service.discover()
    _require_complete_pool_inventory(snapshot)
    _ensure_pool_name_available(snapshot, normalized_name)

    source_pool = _select_pool_template(snapshot, spec, source_identifier)
    bootstrap_source_pool = _select_legacy_bootstrap_template(
        snapshot,
        spec,
        bootstrap_source_identifier,
    )
    bootstrap_source_metadata: tuple[tuple[str, str], ...] | None = None
    backend = service.oci_backend()
    steps: tuple[str, ...]
    if not spec.managed:
        if not source_pool.cluster_network_id or not source_pool.instance_pool_id:
            raise WorkflowError(
                f"Source pool is missing its Cluster Network backing identifiers: "
                f"{source_pool.name}"
            )
        effective = backend.preview_cluster_network_pool_create(
            source_pool.cluster_network_id,
            source_pool.instance_pool_id,
            normalized_name,
            count,
            spec,
        )
        owner = "compute-management"
        steps = (
            f"derive an Instance Configuration from {source_pool.name}",
            "apply requested shape, image, placement, networking, and bootstrap overrides",
            "retarget instance, VNIC, and Kubernetes node pool identity",
            "create a Cluster Network with one embedded Instance Pool",
            "allow the inherited OKE bootstrap to register self-managed RDMA workers",
        )
    else:
        if spec.uses_compute_cluster:
            _require_enhanced_cluster(
                backend.get_cluster_type(target.cluster_id)
            )
        if not source_pool.node_pool_id:
            raise WorkflowError(
                f"Source pool is missing its managed OKE node-pool OCID: "
                f"{source_pool.name}"
            )
        bootstrap_metadata: dict[str, str] | None = None
        if bootstrap_source_pool is not None:
            bootstrap_metadata = _load_legacy_bootstrap_metadata(
                backend,
                bootstrap_source_pool,
            )
            bootstrap_source_metadata = tuple(sorted(bootstrap_metadata.items()))
        preview_kwargs = (
            {"bootstrap_metadata": bootstrap_metadata}
            if bootstrap_metadata is not None
            else {}
        )
        effective = backend.preview_managed_node_pool_create(
            source_pool.node_pool_id,
            target.cluster_id,
            target.compartment_id,
            normalized_name,
            count,
            spec,
            **preview_kwargs,
        )
        if bootstrap_source_pool is not None and bootstrap_metadata is not None:
            effective = dict(effective)
            effective["bootstrap_source"] = {
                "pool": bootstrap_source_pool.name,
                **_legacy_bootstrap_summary(
                    bootstrap_source_pool,
                    bootstrap_metadata,
                ),
            }
        owner = "oke+compute" if spec.creates_compute_cluster else "oke"
        managed_steps = [
            f"derive a managed OKE node-pool request from {source_pool.name}",
            "apply requested shape, image, placement, networking, and bootstrap overrides",
            "retarget Kubernetes labels, metadata, tags, and node lifecycle settings",
        ]
        if bootstrap_source_pool is not None:
            managed_steps.insert(
                1,
                "inherit legacy RDMA cloud-init and supported bootstrap metadata "
                f"from {bootstrap_source_pool.name}",
            )
        if spec.creates_compute_cluster:
            managed_steps.append(
                "create and wait for a dedicated Compute Cluster"
            )
        elif spec.compute_cluster_id:
            managed_steps.append(
                "use the validated existing Compute Cluster"
            )
        if spec.host_group_id:
            managed_steps.append(
                "place workers through the validated Compute Host Group"
            )
        managed_steps.extend(
            [
                "create the managed OKE node pool",
                "allow OKE to provision and register workers",
            ]
        )
        steps = tuple(managed_steps)
    warnings = [
        (
            INSTANCE_CONFIGURATION_DERIVATION_NOTICE
            if not spec.managed
            else MANAGED_POOL_DERIVATION_NOTICE
        ),
        IAC_CREATE_DRIFT_WARNING,
    ]
    if spec.uses_compute_cluster:
        warnings.append(COMPUTE_CLUSTER_IAM_NOTICE)
    if spec.host_group_id:
        warnings.append(HOST_GROUP_IAM_NOTICE)
    if bootstrap_source_pool is not None:
        warnings.append(LEGACY_BOOTSTRAP_INHERITANCE_NOTICE)
    details: dict[str, object] = {
        "source_pool": source_pool.name,
        "requested": spec.as_dict(),
        "effective": effective,
    }
    if bootstrap_source_pool is not None:
        details["bootstrap_source_pool"] = bootstrap_source_pool.name
    plan = OperationPlan(
        operation="pool-create",
        target=normalized_name,
        pool=normalized_name,
        owner=owner,
        current_size=0,
        target_size=count,
        steps=steps,
        warnings=tuple(warnings),
        details=details,
    )
    return PreparedPoolCreate(
        snapshot=snapshot,
        source_pool=source_pool,
        name=normalized_name,
        count=count,
        spec=spec,
        plan=plan,
        bootstrap_source_pool=bootstrap_source_pool,
        bootstrap_source_metadata=bootstrap_source_metadata,
    )


def execute_pool_create(
    service: DiscoveryService,
    prepared: PreparedPoolCreate,
    wait: bool = False,
    timeout_seconds: int = 1800,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    source_pool = prepared.source_pool

    with mutation_lock(service, lock, timeout_seconds):
        target = service.resolve_oci_target(
            require_compartment=True,
            require_cluster=True,
        )
        if not target.compartment_id or not target.cluster_id:
            raise WorkflowError("Pool creation requires an OKE cluster and compartment.")
        current_snapshot = service.discover()
        _require_complete_pool_inventory(current_snapshot)
        _ensure_pool_name_available(current_snapshot, prepared.name)
        current_source = current_snapshot.pool_by_name(source_pool.backing_id or "")
        if current_source is None or not _pool_matches_create_type(
            current_source,
            prepared.spec,
        ):
            raise WorkflowError(
                f"Source pool changed after planning: {source_pool.name}"
            )

        backend = service.oci_backend()
        if not prepared.spec.managed:
            if (
                not current_source.cluster_network_id
                or not current_source.instance_pool_id
            ):
                raise WorkflowError(
                    f"Source pool is missing Cluster Network identifiers: "
                    f"{current_source.name}"
                )
            created: ClusterNetworkCreateResult | ManagedNodePoolCreateResult = (
                backend.create_cluster_network_pool(
                    current_source.cluster_network_id,
                    current_source.instance_pool_id,
                    prepared.name,
                    prepared.count,
                    prepared.spec,
                )
            )
        else:
            if not current_source.node_pool_id:
                raise WorkflowError(
                    f"Source pool is missing its managed OKE node-pool OCID: "
                    f"{current_source.name}"
                )
            if prepared.spec.uses_compute_cluster:
                _require_enhanced_cluster(
                    backend.get_cluster_type(target.cluster_id)
                )
            runtime_bootstrap_metadata: dict[str, str] | None = None
            if prepared.bootstrap_source_pool is not None:
                current_bootstrap_source = current_snapshot.pool_by_name(
                    prepared.bootstrap_source_pool.backing_id or ""
                )
                if current_bootstrap_source is None:
                    raise WorkflowError(
                        "Legacy bootstrap source changed after planning: "
                        f"{prepared.bootstrap_source_pool.name}"
                    )
                runtime_bootstrap_metadata = _load_legacy_bootstrap_metadata(
                    backend,
                    current_bootstrap_source,
                )
                if tuple(sorted(runtime_bootstrap_metadata.items())) != (
                    prepared.bootstrap_source_metadata
                ):
                    raise WorkflowError(
                        "Legacy bootstrap source changed after planning: "
                        f"{prepared.bootstrap_source_pool.name}. Rerun the command "
                        "to review and confirm the current bootstrap."
                    )
            preview_kwargs = (
                {"bootstrap_metadata": runtime_bootstrap_metadata}
                if runtime_bootstrap_metadata is not None
                else {}
            )
            effective = backend.preview_managed_node_pool_create(
                current_source.node_pool_id,
                target.cluster_id,
                target.compartment_id,
                prepared.name,
                prepared.count,
                prepared.spec,
                **preview_kwargs,
            )
            runtime_spec = prepared.spec
            created_compute_cluster_id: str | None = None
            if prepared.spec.creates_compute_cluster:
                availability_domains = tuple(
                    str(value)
                    for value in effective.get("availability_domains", [])
                    if value
                )
                if len(availability_domains) != 1:
                    raise WorkflowError(
                        "Compute Cluster creation requires exactly one effective "
                        "availability domain."
                    )
                compute_cluster = backend.create_compute_cluster(
                    compartment_id=(
                        prepared.spec.compute_cluster_compartment_id
                        or target.compartment_id
                    ),
                    availability_domain=availability_domains[0],
                    display_name=(
                        prepared.spec.compute_cluster_name
                        or f"{prepared.name}-cc"
                    ),
                    pool_name=prepared.name,
                    freeform_tags=dict(prepared.spec.freeform_tags),
                )
                created_compute_cluster_id = (
                    compute_cluster.compute_cluster_id
                )
                wait_for_compute_cluster_active(
                    backend,
                    created_compute_cluster_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress=progress,
                )
                runtime_spec = replace(
                    prepared.spec,
                    compute_cluster_id=created_compute_cluster_id,
                    compute_cluster_name=None,
                    compute_cluster_compartment_id=None,
                )
            try:
                if runtime_bootstrap_metadata is None:
                    created = backend.create_managed_node_pool(
                        current_source.node_pool_id,
                        target.cluster_id,
                        target.compartment_id,
                        prepared.name,
                        prepared.count,
                        runtime_spec,
                    )
                else:
                    created = backend.create_managed_node_pool(
                        current_source.node_pool_id,
                        target.cluster_id,
                        target.compartment_id,
                        prepared.name,
                        prepared.count,
                        runtime_spec,
                        bootstrap_metadata=runtime_bootstrap_metadata,
                    )
            except Exception as exc:
                if created_compute_cluster_id:
                    raise WorkflowError(
                        f"{exc} Created Compute Cluster "
                        f"{created_compute_cluster_id} is retained because the "
                        "node-pool request outcome could not be proven."
                    ) from exc
                raise
            if created_compute_cluster_id:
                created = replace(
                    created,
                    compute_cluster_id=created_compute_cluster_id,
                    compute_cluster_created=True,
                )
        observed_pool: WorkerPoolInfo | None = None
        status = "submitted"
        if wait:
            require_rdma_vf = bool(
                source_pool.rdma_vf_required
                or (
                    prepared.spec.pool_type == "rdma"
                    and current_snapshot.network_operator_active
                )
            )
            try:
                observed_pool = wait_for_pool_creation(
                    service,
                    prepared.name,
                    prepared.count,
                    created,
                    timeout_seconds,
                    poll_interval_seconds,
                    require_rdma_vf=require_rdma_vf,
                    progress=progress,
                )
            except Exception as exc:
                if isinstance(created, ClusterNetworkCreateResult):
                    raise WorkflowError(
                        f"{exc} Created Cluster Network "
                        f"{created.cluster_network_id} and derived Instance "
                        f"Configuration {created.instance_configuration_id} "
                        "may require cleanup."
                    ) from exc
                if created.compute_cluster_created:
                    raise WorkflowError(
                        f"{exc} Managed node pool {prepared.name} and Compute "
                        f"Cluster {created.compute_cluster_id} are retained for "
                        "inspection."
                    ) from exc
                raise
            status = "ready"
    return pool_create_result_row(
        prepared,
        created,
        observed_pool=observed_pool,
        status=status,
    )


def prepare_pool_delete(
    service: DiscoveryService,
    pool_identifier: str,
    *,
    drain: bool = True,
    allow_workloads: bool = False,
    delete_emptydir_data: bool = False,
    force_unmanaged: bool = False,
    allow_system_pool: bool = False,
    drain_grace_period_seconds: int = 30,
) -> PreparedPoolDelete:
    if service.options.auth == "none" or service.options.skip_oci:
        raise WorkflowError(
            "Pool deletion requires OCI discovery. Use instance-principal "
            "authentication on the operator host."
        )
    service.resolve_oci_target(require_compartment=True)
    snapshot = service.discover()
    _require_complete_pool_inventory(snapshot)
    kubernetes_unavailable = (
        service.options.skip_kubernetes
        or any(
            warning.startswith("Kubernetes discovery skipped:")
            for warning in snapshot.warnings
        )
    )
    if kubernetes_unavailable:
        if drain:
            raise WorkflowError(
                "Pool deletion with drain requires successful Kubernetes discovery."
            )
        if not allow_workloads:
            raise WorkflowError(
                "Kubernetes discovery is unavailable, so workload presence cannot "
                "be verified. Use --no-drain with --allow-workloads only after "
                "explicit review."
            )
    pool = snapshot.pool_by_name(pool_identifier)
    if pool is None:
        raise WorkflowNotFound(f"Pool not found: {pool_identifier}")
    _validate_pool_mutation(pool)
    if pool.name == "oke-system" and not allow_system_pool:
        raise WorkflowError(
            "Refusing to delete the OKE system pool. Use --allow-system-pool "
            "only after another system-capable pool is ready."
        )
    if pool.slinky_managed:
        raise WorkflowError(_slinky_pool_mutation_error(pool.name))

    nodes = tuple(_nodes_for_pool(snapshot, pool))
    if not drain and not allow_workloads:
        busy = [node for node in nodes if node.running_workload_pods]
        if busy:
            raise WorkflowError(
                f"Refusing to delete {pool.name} without drain: "
                f"{sum(node.running_workload_pods for node in busy)} workload "
                "pod(s) are running. Use --drain or --allow-workloads."
            )

    drain_pods: dict[str, tuple[DrainPod, ...]] = {}
    if drain:
        kubernetes = service.kubernetes_backend()
        for node in nodes:
            pods = kubernetes.list_drain_pods(
                node.k8s_name,
                grace_period_seconds=drain_grace_period_seconds,
                check_evictions=True,
            )
            _validate_drain_pods(
                node,
                pods,
                delete_emptydir_data,
                force_unmanaged,
            )
            drain_pods[node.k8s_name] = tuple(pods)

    owner, delete_step = _pool_delete_owner(pool)
    owned_instance_configuration_id = (
        pool.instance_configuration_id
        if (
            pool.kind == "cluster-network"
            and pool.created_by_mgmt_oke
            and pool.instance_configuration_id
        )
        else None
    )
    steps: list[str] = []
    warnings = [
        "Pool deletion permanently removes its workers and their boot volumes.",
        IAC_DRIFT_WARNING,
    ]
    if drain and nodes:
        blockers = [
            f"{pod.namespace}/{pod.name} ({pod.eviction_blocker})"
            for pods in drain_pods.values()
            for pod in pods
            if pod.eviction_blocker
        ]
        if blockers:
            warnings.append(
                "Eviction dry-run reported blockers: " + ", ".join(blockers)
            )
        steps.extend(
            (
                "cordon every Kubernetes node in the pool",
                "evict non-DaemonSet pods through the Eviction API",
            )
        )
    elif nodes:
        warnings.insert(0, "Pool deletion will proceed without Kubernetes drain.")
    steps.append(delete_step)
    if owned_instance_configuration_id:
        steps.append(
            "with --wait, delete the mgmt-oke-owned Instance Configuration "
            "after Cluster Network termination"
        )
        warnings.append(
            "Automatic cleanup of the derived Instance Configuration requires "
            "--wait; --no-wait retains it for manual cleanup."
        )
    return PreparedPoolDelete(
        snapshot=snapshot,
        pool=pool,
        nodes=nodes,
        drain_pods=drain_pods,
        allow_workloads=allow_workloads,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        plan=OperationPlan(
            operation="pool-delete",
            target=pool.name,
            pool=pool.name,
            owner=owner,
            current_size=pool.desired_size,
            target_size=0,
            workload_pods=sum(node.running_workload_pods for node in nodes),
            steps=tuple(steps),
            warnings=tuple(warnings),
            details={
                "kind": pool.kind,
                "placement": pool.placement_type,
                "nodes": [node.k8s_name for node in nodes],
                "instance_configuration_id": owned_instance_configuration_id,
            },
        ),
    )


def execute_pool_delete(
    service: DiscoveryService,
    prepared: PreparedPoolDelete,
    *,
    drain: bool = True,
    drain_grace_period_seconds: int = 30,
    drain_timeout_seconds: int = 600,
    wait: bool = False,
    timeout_seconds: int = 1800,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    pool = prepared.pool
    kubernetes = service.kubernetes_backend() if drain or lock else None
    cordoned: list[str] = []
    submitted = False
    owned_instance_configuration_id = (
        pool.instance_configuration_id
        if (
            pool.kind == "cluster-network"
            and pool.created_by_mgmt_oke
            and pool.instance_configuration_id
        )
        else None
    )
    instance_configuration_status: str | None = (
        "retained" if owned_instance_configuration_id else None
    )
    with mutation_lock(
        service,
        lock,
        max(timeout_seconds, drain_timeout_seconds),
    ):
        try:
            current = service.discover()
            current_pool = current.pool_by_name(pool.backing_id or pool.name)
            if (
                current_pool is None
                or current_pool.kind != pool.kind
                or current_pool.backing_id != pool.backing_id
            ):
                raise WorkflowError(
                    f"Pool changed after deletion planning: {pool.name}"
                )
            _validate_pool_mutation(current_pool)
            if current_pool.slinky_managed:
                raise WorkflowError(
                    _slinky_pool_mutation_error(current_pool.name)
                )
            current_nodes = tuple(_nodes_for_pool(current, current_pool))
            if {node.k8s_name for node in current_nodes} != {
                node.k8s_name for node in prepared.nodes
            }:
                raise WorkflowError(
                    f"Pool membership changed after deletion planning: {pool.name}"
                )
            if not drain and not prepared.allow_workloads:
                busy = [node for node in current_nodes if node.running_workload_pods]
                if busy:
                    raise WorkflowError(
                        f"Refusing to delete {pool.name} without drain: "
                        f"{sum(node.running_workload_pods for node in busy)} workload "
                        "pod(s) are now running. Use --allow-workloads."
                    )

            if drain and kubernetes:
                for node in prepared.nodes:
                    kubernetes.cordon_node(node.k8s_name)
                    cordoned.append(node.k8s_name)
                for node in prepared.nodes:
                    pods = kubernetes.list_drain_pods(
                        node.k8s_name,
                        grace_period_seconds=drain_grace_period_seconds,
                        check_evictions=True,
                    )
                    _validate_drain_pods(
                        node,
                        pods,
                        prepared.delete_emptydir_data,
                        prepared.force_unmanaged,
                    )
                    kubernetes.evict_drain_pods(
                        pods,
                        grace_period_seconds=drain_grace_period_seconds,
                        timeout_seconds=drain_timeout_seconds,
                    )

            backend = service.oci_backend()
            if current_pool.kind == "node-pool" and current_pool.node_pool_id:
                work_request_id = backend.delete_managed_node_pool(
                    current_pool.node_pool_id
                )
            elif (
                current_pool.kind == "cluster-network"
                and current_pool.cluster_network_id
            ):
                work_request_id = backend.terminate_cluster_network(
                    current_pool.cluster_network_id
                )
            elif (
                current_pool.kind == "instance-pool"
                and current_pool.instance_pool_id
            ):
                work_request_id = backend.terminate_instance_pool(
                    current_pool.instance_pool_id
                )
            else:
                raise WorkflowError(
                    f"Pool is missing the OCI resource required for deletion: "
                    f"{pool.name}"
                )
            submitted = True
            status = "submitted"
            if wait:
                wait_for_pool_deleted(
                    service,
                    pool,
                    work_request_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress=progress,
                )
                if owned_instance_configuration_id:
                    backend.delete_mgmt_created_instance_configuration(
                        owned_instance_configuration_id
                    )
                    instance_configuration_status = "deleted"
                status = "deleted"
        except Exception:
            if kubernetes and not submitted:
                for node_name in cordoned:
                    try:
                        kubernetes.uncordon_node(node_name)
                    except Exception:
                        pass
            raise
    return pool_delete_result_row(
        pool,
        work_request_id,
        status,
        instance_configuration_id=owned_instance_configuration_id,
        instance_configuration_status=instance_configuration_status,
    )


def prepare_pool_resize(
    service: DiscoveryService,
    pool_identifier: str,
    size: int | None = None,
    delta: int | None = None,
) -> PreparedPoolResize:
    if (size is None) == (delta is None):
        raise WorkflowError("Specify exactly one of size or delta.")
    if service.options.auth == "none" or service.options.skip_oci:
        raise WorkflowError(
            "Pool resize requires OCI discovery. Use instance-principal authentication on the operator host."
        )

    service.resolve_oci_target(require_compartment=True)
    snapshot = service.discover()
    pool = snapshot.pool_by_name(pool_identifier)
    if pool is None:
        raise WorkflowNotFound(f"Pool not found: {pool_identifier}")
    _validate_pool_mutation(pool)
    if pool.desired_size is None:
        raise WorkflowError(f"Cannot determine current desired size for pool: {pool.name}")

    target_size = size if size is not None else pool.desired_size + int(delta or 0)
    if target_size < 0:
        raise WorkflowError(f"Target size cannot be negative: {target_size}")
    if target_size < pool.desired_size and pool.slinky_managed:
        raise WorkflowError(_slinky_pool_mutation_error(pool.name))

    owner, mutation_step = _pool_owner(pool)
    steps = [mutation_step]
    warnings = [IAC_DRIFT_WARNING]
    if target_size < pool.desired_size:
        warnings.insert(
            0,
            "Pool-level scale-down delegates worker selection to the owning service; "
            "use nodes terminate when worker identity matters.",
        )
    plan = OperationPlan(
        operation="pool-resize",
        target=pool.name,
        pool=pool.name,
        owner=owner,
        current_size=pool.desired_size,
        target_size=target_size,
        steps=tuple(steps),
        warnings=tuple(warnings),
    )
    return PreparedPoolResize(snapshot=snapshot, pool=pool, plan=plan)


def execute_pool_resize(
    service: DiscoveryService,
    prepared: PreparedPoolResize,
    wait: bool = False,
    timeout_seconds: int = 1800,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    pool = prepared.pool
    target_size = prepared.plan.target_size
    current_size = prepared.plan.current_size
    if target_size is None or current_size is None:
        raise WorkflowError("Pool resize plan is missing size information.")

    if target_size == current_size:
        status = "unchanged"
        if wait:
            pool = wait_for_pool_size(
                service,
                pool.name,
                target_size,
                timeout_seconds,
                poll_interval_seconds,
                require_rdma_vf=pool.rdma_vf_required,
                progress=progress,
            )
            status = "ready"
        return resize_result_row(pool, current_size, target_size, None, status)

    with mutation_lock(service, lock, timeout_seconds):
        backend = service.oci_backend()
        work_request_watches = (
            _prepare_resource_work_request_watches(
                service,
                _pool_resize_work_request_resources(pool),
            )
            if wait
            else ()
        )
        if pool.kind == "node-pool" and pool.node_pool_id:
            work_request_id = backend.resize_managed_node_pool(pool.node_pool_id, target_size)
        elif pool.kind == "cluster-network" and pool.cluster_network_id and pool.instance_pool_id:
            work_request_id = backend.resize_cluster_network(
                pool.cluster_network_id,
                pool.instance_pool_id,
                target_size,
            )
        elif pool.kind == "instance-pool" and pool.instance_pool_id:
            work_request_id = backend.resize_instance_pool(pool.instance_pool_id, target_size)
        else:
            raise WorkflowError(
                f"Pool is missing the OCI backing resource required for resize: {pool.name}"
            )

        status = "submitted"
        if wait:
            pool = wait_for_pool_size(
                service,
                pool.name,
                target_size,
                timeout_seconds,
                poll_interval_seconds,
                require_rdma_vf=pool.rdma_vf_required,
                work_request_id=work_request_id,
                work_request_watches=work_request_watches,
                progress=progress,
            )
            status = "ready"
    return resize_result_row(pool, current_size, target_size, work_request_id, status)


def prepare_node_removal(
    service: DiscoveryService,
    identifiers: Iterable[str] = (),
    fields: str | None = None,
    keep_size: bool = False,
    drain: bool = True,
    allow_workloads: bool = False,
    delete_emptydir_data: bool = False,
    force_unmanaged: bool = False,
    eviction_grace: str = "PT10M",
    force_after_grace: bool = False,
    drain_grace_period_seconds: int = 30,
) -> PreparedNodeRemoval:
    if service.options.auth == "none" or service.options.skip_oci:
        raise WorkflowError(
            "Node removal requires OCI discovery. Use instance-principal authentication on the operator host."
        )
    identifier_tuple = tuple(identifiers)
    if not identifier_tuple and not fields:
        raise WorkflowError("Specify nodes by identifier or with --fields.")

    service.resolve_oci_target(require_compartment=True)
    snapshot = service.discover()
    nodes, missing = select_nodes(snapshot, identifiers=identifier_tuple, fields=fields)
    if missing:
        raise WorkflowNotFound(f"Nodes not found: {', '.join(missing)}")
    if not nodes:
        raise WorkflowNotFound("No nodes matched the requested selection.")

    pools: dict[str, WorkerPoolInfo] = {}
    node_pools: dict[str, WorkerPoolInfo] = {}
    for node in nodes:
        if not node.instance_ocid:
            raise WorkflowError(f"Node has no OCI instance OCID: {node.k8s_name}")
        pool = _pool_for_node(snapshot, node)
        _validate_pool_mutation(pool)
        if pool.slinky_managed or node.slinky_managed:
            raise WorkflowError(_slinky_node_mutation_error(node.k8s_name, pool.name))
        if pool.kind in {"cluster-network", "instance-pool"}:
            if force_after_grace:
                raise WorkflowError("--force-after-grace applies only to managed OKE node pools.")
            if eviction_grace != "PT10M":
                raise WorkflowError("--eviction-grace applies only to managed OKE node pools.")
        if node.running_workload_pods and not drain and not allow_workloads:
            raise WorkflowError(
                f"Refusing to remove {node.k8s_name}: {node.running_workload_pods} workload pod(s) "
                "are running. Use --drain or --allow-workloads."
            )
        pools[pool.name] = pool
        node_pools[node.k8s_name] = pool

    decrement_size = not keep_size
    removals = Counter(node_pools[node.k8s_name].name for node in nodes)
    target_sizes: dict[str, int] = {}
    for pool_name, count in removals.items():
        pool = pools[pool_name]
        if pool.desired_size is None:
            raise WorkflowError(f"Cannot determine current desired size for pool: {pool.name}")
        target = pool.desired_size - count if decrement_size else pool.desired_size
        if target < 0:
            raise WorkflowError(
                f"Removing {count} node(s) would make {pool.name} size negative."
            )
        target_sizes[pool_name] = target

    drain_pods: dict[str, tuple[DrainPod, ...]] = {}
    if drain:
        backend = service.kubernetes_backend()
        for node in nodes:
            pods = backend.list_drain_pods(
                node.k8s_name,
                grace_period_seconds=drain_grace_period_seconds,
                check_evictions=True,
            )
            _validate_drain_pods(node, pods, delete_emptydir_data, force_unmanaged)
            drain_pods[node.k8s_name] = tuple(pods)

    plans: list[OperationPlan] = []
    for node in nodes:
        pool = node_pools[node.k8s_name]
        owner, mutation_step = _node_owner(pool)
        steps: list[str] = []
        warnings: list[str] = [IAC_DRIFT_WARNING]
        if drain:
            steps.extend(("cordon Kubernetes node", "evict non-DaemonSet pods through the Eviction API"))
            blockers = [pod for pod in drain_pods[node.k8s_name] if pod.eviction_blocker]
            if blockers:
                warnings.append(
                    "Eviction dry-run reported blockers: "
                    + ", ".join(
                        f"{pod.namespace}/{pod.name} ({pod.eviction_blocker})" for pod in blockers
                    )
                )
        else:
            warnings.insert(0, "Node removal will proceed without a Kubernetes drain.")
        steps.append(mutation_step)
        plans.append(
            OperationPlan(
                operation="node-remove",
                target=node.k8s_name,
                pool=pool.name,
                owner=owner,
                current_size=pool.desired_size,
                target_size=target_sizes[pool.name],
                decrement_size=decrement_size,
                workload_pods=node.running_workload_pods,
                steps=tuple(steps),
                warnings=tuple(warnings),
            )
        )

    return PreparedNodeRemoval(
        snapshot=snapshot,
        nodes=tuple(nodes),
        pools=pools,
        plans=tuple(plans),
        drain_pods=drain_pods,
        target_sizes=target_sizes,
        decrement_size=decrement_size,
    )


def apply_node_removal_host_tags(
    prepared: PreparedNodeRemoval,
    host_tags: Mapping[str, CustomerReportedHostStatus],
) -> PreparedNodeRemoval:
    selected_names = {node.k8s_name for node in prepared.nodes}
    unknown_names = sorted(set(host_tags) - selected_names)
    if unknown_names:
        raise WorkflowError(
            "Host tag decisions reference unselected nodes: "
            + ", ".join(unknown_names)
        )

    decisions = dict(host_tags)
    plans: list[OperationPlan] = []
    for plan in prepared.plans:
        status = decisions.get(plan.target)
        details = dict(plan.details)
        details["customer_reported_host_status"] = (
            status.value if status is not None else "not-requested"
        )
        steps = list(plan.steps)
        if status is CustomerReportedHostStatus.UNHEALTHY:
            tag_steps = (
                "tag OCI instance as customer-reported unhealthy",
                "verify OCI instance unhealthy tag",
            )
            insertion_index = max(len(steps) - 1, 0)
            steps[insertion_index:insertion_index] = tag_steps
        plans.append(
            replace(
                plan,
                details=details,
                steps=tuple(steps),
            )
        )
    return replace(prepared, plans=tuple(plans), host_tags=decisions)


def execute_node_removal(
    service: DiscoveryService,
    prepared: PreparedNodeRemoval,
    drain: bool = True,
    drain_grace_period_seconds: int = 30,
    drain_timeout_seconds: int = 600,
    wait: bool = False,
    timeout_seconds: int = 1800,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    eviction_grace: str = "PT10M",
    force_after_grace: bool = False,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    work_requests: dict[str, str | None] = {}
    host_tag_statuses: dict[str, str] = {
        node.k8s_name: "not-requested" for node in prepared.nodes
    }
    cordoned: list[str] = []
    submitted: set[str] = set()
    kubernetes = service.kubernetes_backend() if drain or lock else None

    with mutation_lock(service, lock, max(timeout_seconds, drain_timeout_seconds)):
        try:
            work_request_watches = (
                _prepare_resource_work_request_watches(
                    service,
                    _node_removal_work_request_resources(prepared.pools.values()),
                )
                if wait
                else ()
            )
            if drain and kubernetes:
                for node in prepared.nodes:
                    kubernetes.cordon_node(node.k8s_name)
                    cordoned.append(node.k8s_name)
                for node in prepared.nodes:
                    kubernetes.evict_drain_pods(
                        list(prepared.drain_pods.get(node.k8s_name, ())),
                        grace_period_seconds=drain_grace_period_seconds,
                        timeout_seconds=drain_timeout_seconds,
                    )

            backend = service.oci_backend()
            for node in prepared.nodes:
                host_status = prepared.host_tags.get(node.k8s_name)
                if host_status is None:
                    continue
                instance_ocid = node.instance_ocid
                if instance_ocid is None:
                    raise WorkflowError(
                        f"Node has no OCI instance OCID: {node.k8s_name}"
                    )
                if host_status is CustomerReportedHostStatus.UNHEALTHY:
                    host_tag_statuses[node.k8s_name] = (
                        backend.tag_instance_customer_reported_unhealthy(
                            instance_ocid
                        )
                    )

            for node in prepared.nodes:
                pool = _pool_for_prepared_node(prepared, node)
                instance_ocid = node.instance_ocid
                if instance_ocid is None:
                    raise WorkflowError(f"Node has no OCI instance OCID: {node.k8s_name}")
                if pool.kind == "node-pool" and pool.node_pool_id:
                    work_request = backend.delete_node(
                        pool.node_pool_id,
                        instance_ocid,
                        decrement_size=prepared.decrement_size,
                        override_eviction_grace_duration=eviction_grace,
                        force_after_grace=force_after_grace,
                    )
                elif pool.kind in {"cluster-network", "instance-pool"} and pool.instance_pool_id:
                    work_request = backend.detach_instance_pool_node(
                        pool.instance_pool_id,
                        instance_ocid,
                        decrement_size=prepared.decrement_size,
                    )
                else:
                    raise WorkflowError(
                        f"Pool is missing the OCI backing resource required for node removal: {pool.name}"
                    )
                submitted.add(node.k8s_name)
                work_requests[node.k8s_name] = work_request
        except Exception:
            if kubernetes:
                for node_name in cordoned:
                    if node_name not in submitted:
                        try:
                            kubernetes.uncordon_node(node_name)
                        except Exception:
                            pass
            raise

        observed_pools = prepared.pools
        status = "submitted"
        if wait:
            observed_pools = wait_for_nodes_removed(
                service,
                prepared.nodes,
                prepared.target_sizes,
                timeout_seconds,
                poll_interval_seconds,
                prepared.pools,
                work_request_ids=tuple(
                    work_request
                    for work_request in work_requests.values()
                    if work_request
                ),
                work_request_watches=work_request_watches,
                progress=progress,
            )
            status = "removed"

    return [
        node_remove_result_row(
            observed_pools[pool.name],
            node,
            prepared.target_sizes[pool.name],
            prepared.decrement_size,
            work_requests.get(node.k8s_name),
            status,
            prepared.host_tags.get(node.k8s_name),
            host_tag_statuses[node.k8s_name],
        )
        for node in prepared.nodes
        for pool in [_pool_for_prepared_node(prepared, node)]
    ]


def prepare_node_boot_volume_replace(
    service: DiscoveryService,
    identifiers: Iterable[str] = (),
    fields: str | None = None,
    *,
    delete_emptydir_data: bool = False,
    force_unmanaged: bool = False,
    allow_system_pool: bool = False,
    eviction_grace_duration: str = "PT60M",
    force_after_grace: bool = False,
    drain_grace_period_seconds: int = 30,
) -> PreparedNodeBootVolumeReplace:
    if service.options.auth == "none" or service.options.skip_oci:
        raise WorkflowError(
            "Node boot volume replacement requires OCI discovery. Use "
            "instance-principal authentication on the operator host."
        )
    identifier_tuple = tuple(identifiers)
    if not identifier_tuple and not fields:
        raise WorkflowError("Specify nodes by identifier or with --fields.")
    try:
        eviction_grace_duration = validate_eviction_grace_duration(
            eviction_grace_duration
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc

    target = service.resolve_oci_target(
        require_compartment=True,
        require_cluster=True,
    )
    if not target.cluster_id:
        raise WorkflowError("Node boot volume replacement requires an OKE cluster.")
    backend = service.oci_backend()
    _require_enhanced_cluster(backend.get_cluster_type(target.cluster_id))

    snapshot = service.discover()
    _require_complete_pool_inventory(snapshot)
    nodes, missing = select_nodes(
        snapshot,
        identifiers=identifier_tuple,
        fields=fields,
    )
    if missing:
        raise WorkflowNotFound(f"Nodes not found: {', '.join(missing)}")
    if not nodes:
        raise WorkflowNotFound("No nodes matched the requested selection.")

    pools: dict[str, WorkerPoolInfo] = {}
    node_pools: dict[str, WorkerPoolInfo] = {}
    old_boot_volume_ids: dict[str, str] = {}
    drain_pods: dict[str, tuple[DrainPod, ...]] = {}
    kubernetes = service.kubernetes_backend()
    for node in nodes:
        if not node.instance_ocid:
            raise WorkflowError(f"Node has no OCI instance OCID: {node.k8s_name}")
        pool = _pool_for_node(snapshot, node)
        _validate_boot_volume_pool(
            snapshot,
            pool,
            allow_system_pool=allow_system_pool,
            require_fully_ready=False,
        )
        if pool.slinky_managed or node.slinky_managed:
            raise WorkflowError(
                _slinky_node_bvr_error(node.k8s_name, pool.name)
            )
        pods = kubernetes.list_drain_pods(
            node.k8s_name,
            grace_period_seconds=drain_grace_period_seconds,
            check_evictions=True,
        )
        _validate_drain_pods(
            node,
            pods,
            delete_emptydir_data,
            force_unmanaged,
        )
        pools[pool.name] = pool
        node_pools[node.k8s_name] = pool
        old_boot_volume_ids[node.instance_ocid] = (
            backend.get_instance_boot_volume_id(node.instance_ocid)
        )
        drain_pods[node.k8s_name] = tuple(pods)

    plans: list[OperationPlan] = []
    for node in nodes:
        pool = node_pools[node.k8s_name]
        instance_ocid = node.instance_ocid
        if not instance_ocid:
            raise WorkflowError(
                f"Node has no OCI instance OCID: {node.k8s_name}"
            )
        warnings = [
            "The current boot volume is replaced; data stored only on that "
            "boot volume is not preserved.",
            "The instance OCID and network address are preserved, but workloads "
            "are disrupted while OKE cordons, drains, stops, and restarts it.",
            "Individual-node BVR preserves the node's existing image and "
            "configuration. Use pools boot-volume-replace to change a managed "
            "pool image.",
        ]
        if pool.kind != "node-pool":
            warnings.append(
                "For a self-managed node whose cluster CA was rotated after it "
                "joined, refresh the OKE CA bootstrap metadata before BVR."
            )
        if force_after_grace:
            warnings.append(
                "OKE will force the BVR action after the eviction grace period "
                "even if cordon or drain has not completed."
            )
        if not node.ready or not node.schedulable:
            warnings.append(
                "The selected node is not currently Ready and schedulable; "
                "BVR will be treated as a repair and --wait must observe full "
                "recovery."
            )
        blockers = [
            pod for pod in drain_pods[node.k8s_name] if pod.eviction_blocker
        ]
        if blockers:
            warnings.append(
                "Eviction dry-run reported blockers: "
                + ", ".join(
                    f"{pod.namespace}/{pod.name} ({pod.eviction_blocker})"
                    for pod in blockers
                )
            )
        plans.append(
            OperationPlan(
                operation="node-boot-volume-replace",
                target=node.k8s_name,
                pool=pool.name,
                owner="oke",
                current_size=pool.desired_size,
                target_size=pool.desired_size,
                workload_pods=node.running_workload_pods,
                steps=(
                    "ask OKE to cordon and drain the selected worker",
                    "stop the existing compute instance",
                    "replace its boot volume while preserving node configuration",
                    "restart the same instance",
                    "verify node identity, Ready state, and GPU/RDMA resources",
                ),
                warnings=tuple(warnings),
                details={
                    "kind": pool.kind,
                    "instance_ocid": instance_ocid,
                    "old_boot_volume_id": old_boot_volume_ids[instance_ocid],
                    "eviction_grace_duration": eviction_grace_duration,
                    "force_after_grace": force_after_grace,
                    "preserves_existing_configuration": True,
                },
            )
        )

    return PreparedNodeBootVolumeReplace(
        snapshot=snapshot,
        nodes=tuple(nodes),
        pools=pools,
        plans=tuple(plans),
        old_boot_volume_ids=old_boot_volume_ids,
        drain_pods=drain_pods,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        allow_system_pool=allow_system_pool,
        eviction_grace_duration=eviction_grace_duration,
        force_after_grace=force_after_grace,
    )


def execute_node_boot_volume_replace(
    service: DiscoveryService,
    prepared: PreparedNodeBootVolumeReplace,
    *,
    wait: bool = False,
    timeout_seconds: int = 3600,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    drain_grace_period_seconds: int = 30,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    if len(prepared.nodes) > 1 and not wait:
        raise WorkflowError(
            "Multiple individual-node BVR operations require --wait so they run "
            "sequentially."
        )
    target = service.resolve_oci_target(
        require_compartment=True,
        require_cluster=True,
    )
    if not target.cluster_id:
        raise WorkflowError("Node boot volume replacement requires an OKE cluster.")
    backend = service.oci_backend()
    results: list[dict[str, object]] = []

    with mutation_lock(service, lock, timeout_seconds):
        _require_enhanced_cluster(backend.get_cluster_type(target.cluster_id))
        current = service.discover()
        _require_complete_pool_inventory(current)
        current_nodes: dict[str, NodeInfo] = {}
        current_pools: dict[str, WorkerPoolInfo] = {}
        for original in prepared.nodes:
            if not original.instance_ocid:
                raise WorkflowError(
                    f"Node has no OCI instance OCID: {original.k8s_name}"
                )
            node = current.node_by_identifier(original.instance_ocid)
            if node is None or node.k8s_name != original.k8s_name:
                raise WorkflowError(
                    f"Node identity changed after BVR planning: "
                    f"{original.k8s_name}"
                )
            pool = _pool_for_node(current, node)
            original_pool = _pool_for_prepared_node(prepared, original)
            if (
                pool.kind != original_pool.kind
                or pool.backing_id != original_pool.backing_id
            ):
                raise WorkflowError(
                    f"Pool ownership changed after BVR planning: "
                    f"{original.k8s_name}"
                )
            _validate_boot_volume_pool(
                current,
                pool,
                allow_system_pool=prepared.allow_system_pool,
                require_fully_ready=False,
            )
            if pool.slinky_managed or node.slinky_managed:
                raise WorkflowError(
                    _slinky_node_bvr_error(node.k8s_name, pool.name)
                )
            pods = service.kubernetes_backend().list_drain_pods(
                node.k8s_name,
                grace_period_seconds=drain_grace_period_seconds,
                check_evictions=True,
            )
            _validate_drain_pods(
                node,
                pods,
                prepared.delete_emptydir_data,
                prepared.force_unmanaged,
            )
            old_boot_volume_id = prepared.old_boot_volume_ids[
                original.instance_ocid
            ]
            if (
                backend.get_instance_boot_volume_id(original.instance_ocid)
                != old_boot_volume_id
            ):
                raise WorkflowError(
                    f"Boot volume changed after BVR planning: "
                    f"{original.k8s_name}"
                )
            current_nodes[original.instance_ocid] = node
            current_pools[original.instance_ocid] = pool

        for original in prepared.nodes:
            instance_ocid = original.instance_ocid
            if not instance_ocid:
                raise WorkflowError(
                    f"Node has no OCI instance OCID: {original.k8s_name}"
                )
            work_request_id = backend.replace_cluster_node_boot_volume(
                target.cluster_id,
                instance_ocid,
                eviction_grace_duration=prepared.eviction_grace_duration,
                force_after_grace=prepared.force_after_grace,
            )
            observed_node = current_nodes[instance_ocid]
            observed_pool = current_pools[instance_ocid]
            new_boot_volume_id: str | None = None
            status = "submitted"
            if wait:
                (
                    observed_node,
                    observed_pool,
                    new_boot_volume_id,
                ) = wait_for_node_boot_volume_replace(
                    service,
                    original,
                    observed_pool,
                    prepared.old_boot_volume_ids[instance_ocid],
                    work_request_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress=progress,
                )
                status = "ready"
            results.append(
                node_boot_volume_replace_result_row(
                    observed_node,
                    observed_pool,
                    prepared.old_boot_volume_ids[instance_ocid],
                    new_boot_volume_id,
                    work_request_id,
                    status,
                )
            )
    return results


def prepare_pool_boot_volume_replace(
    service: DiscoveryService,
    pool_identifier: str,
    spec: PoolBootVolumeReplaceSpec,
    *,
    delete_emptydir_data: bool = False,
    force_unmanaged: bool = False,
    allow_system_pool: bool = False,
    drain_grace_period_seconds: int = 30,
) -> PreparedPoolBootVolumeReplace:
    if service.options.auth == "none" or service.options.skip_oci:
        raise WorkflowError(
            "Pool boot volume replacement requires OCI discovery. Use "
            "instance-principal authentication on the operator host."
        )
    try:
        spec = validate_pool_boot_volume_replace_spec(spec)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    target = service.resolve_oci_target(
        require_compartment=True,
        require_cluster=True,
    )
    if not target.cluster_id:
        raise WorkflowError("Pool boot volume replacement requires an OKE cluster.")
    backend = service.oci_backend()
    _require_enhanced_cluster(backend.get_cluster_type(target.cluster_id))

    snapshot = service.discover()
    _require_complete_pool_inventory(snapshot)
    pool = snapshot.pool_by_name(pool_identifier)
    if pool is None:
        raise WorkflowNotFound(f"Pool not found: {pool_identifier}")
    _validate_boot_volume_pool(
        snapshot,
        pool,
        allow_system_pool=allow_system_pool,
        require_fully_ready=True,
    )
    if pool.kind != "node-pool" or not pool.node_pool_id:
        raise WorkflowError(
            "Pool-wide BVR with property updates is supported only for managed "
            f"OKE node pools: {pool.name}. Use nodes boot-volume-replace for an "
            "individual self-managed worker."
        )
    if pool.slinky_managed:
        raise WorkflowError(_slinky_pool_bvr_error(pool.name))

    nodes = tuple(_nodes_for_pool(snapshot, pool))
    old_boot_volume_ids: dict[str, str] = {}
    drain_pods: dict[str, tuple[DrainPod, ...]] = {}
    kubernetes = service.kubernetes_backend()
    for node in nodes:
        if not node.instance_ocid:
            raise WorkflowError(f"Node has no OCI instance OCID: {node.k8s_name}")
        old_boot_volume_ids[node.instance_ocid] = (
            backend.get_instance_boot_volume_id(node.instance_ocid)
        )
        pods = kubernetes.list_drain_pods(
            node.k8s_name,
            grace_period_seconds=drain_grace_period_seconds,
            check_evictions=True,
        )
        _validate_drain_pods(
            node,
            pods,
            delete_emptydir_data,
            force_unmanaged,
        )
        drain_pods[node.k8s_name] = tuple(pods)
    preview = backend.preview_managed_pool_boot_volume_replace(
        pool.node_pool_id,
        spec,
    )
    warnings = [
        "Every existing worker boot volume in the managed pool is replaced; "
        "data stored only on those boot volumes is not preserved.",
        "OKE preserves each compute instance OCID and network address while "
        "cycling workers up to maximum-unavailable at a time.",
        IAC_DRIFT_WARNING,
    ]
    blockers = [
        f"{pod.namespace}/{pod.name} ({pod.eviction_blocker})"
        for pods in drain_pods.values()
        for pod in pods
        if pod.eviction_blocker
    ]
    if blockers:
        warnings.append(
            "Eviction dry-run reported blockers: " + ", ".join(blockers)
        )
    return PreparedPoolBootVolumeReplace(
        snapshot=snapshot,
        pool=pool,
        nodes=nodes,
        old_boot_volume_ids=old_boot_volume_ids,
        drain_pods=drain_pods,
        spec=spec,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        allow_system_pool=allow_system_pool,
        plan=OperationPlan(
            operation="pool-boot-volume-replace",
            target=pool.name,
            pool=pool.name,
            owner="oke",
            current_size=pool.desired_size,
            target_size=pool.desired_size,
            workload_pods=sum(node.running_workload_pods for node in nodes),
            steps=(
                "update the supported managed node-pool properties",
                "enable BOOT_VOLUME_REPLACE node cycling",
                "let OKE cordon, drain, stop, update, and restart each worker",
                "verify instance identity, replacement boot volumes, Ready state, "
                "and GPU/RDMA resources",
            ),
            warnings=tuple(warnings),
            details={
                "kind": pool.kind,
                "nodes": [node.k8s_name for node in nodes],
                "updates": spec.as_dict(),
                **preview,
            },
        ),
    )


def execute_pool_boot_volume_replace(
    service: DiscoveryService,
    prepared: PreparedPoolBootVolumeReplace,
    *,
    wait: bool = False,
    timeout_seconds: int = 7200,
    poll_interval_seconds: int = 30,
    lock: bool = True,
    drain_grace_period_seconds: int = 30,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    target = service.resolve_oci_target(
        require_compartment=True,
        require_cluster=True,
    )
    if not target.cluster_id:
        raise WorkflowError("Pool boot volume replacement requires an OKE cluster.")
    backend = service.oci_backend()
    pool = prepared.pool
    if not pool.node_pool_id:
        raise WorkflowError(
            f"Managed OKE pool is missing its node-pool OCID: {pool.name}"
        )

    with mutation_lock(service, lock, timeout_seconds):
        _require_enhanced_cluster(backend.get_cluster_type(target.cluster_id))
        current = service.discover()
        _require_complete_pool_inventory(current)
        current_pool = current.pool_by_name(pool.node_pool_id)
        if (
            current_pool is None
            or current_pool.kind != "node-pool"
            or current_pool.node_pool_id != pool.node_pool_id
        ):
            raise WorkflowError(
                f"Pool ownership changed after BVR planning: {pool.name}"
            )
        _validate_boot_volume_pool(
            current,
            current_pool,
            allow_system_pool=prepared.allow_system_pool,
            require_fully_ready=True,
        )
        if current_pool.slinky_managed:
            raise WorkflowError(_slinky_pool_bvr_error(current_pool.name))
        current_nodes = tuple(_nodes_for_pool(current, current_pool))
        if {
            node.instance_ocid for node in current_nodes
        } != {
            node.instance_ocid for node in prepared.nodes
        }:
            raise WorkflowError(
                f"Pool membership changed after BVR planning: {pool.name}"
            )
        for node in current_nodes:
            if not node.instance_ocid:
                raise WorkflowError(
                    f"Node has no OCI instance OCID: {node.k8s_name}"
                )
            if (
                backend.get_instance_boot_volume_id(node.instance_ocid)
                != prepared.old_boot_volume_ids[node.instance_ocid]
            ):
                raise WorkflowError(
                    f"Boot volume changed after BVR planning: {node.k8s_name}"
                )
            pods = service.kubernetes_backend().list_drain_pods(
                node.k8s_name,
                grace_period_seconds=drain_grace_period_seconds,
                check_evictions=True,
            )
            _validate_drain_pods(
                node,
                pods,
                prepared.delete_emptydir_data,
                prepared.force_unmanaged,
            )

        work_request_id = backend.replace_managed_pool_boot_volumes(
            pool.node_pool_id,
            prepared.spec,
        )
        observed_pool = current_pool
        new_boot_volume_ids: dict[str, str] = {}
        status = "submitted"
        if wait:
            observed_pool, new_boot_volume_ids = (
                wait_for_pool_boot_volume_replace(
                    service,
                    prepared,
                    work_request_id,
                    timeout_seconds,
                    poll_interval_seconds,
                    progress=progress,
                )
            )
            status = "ready"

    return pool_boot_volume_replace_result_row(
        observed_pool,
        prepared,
        new_boot_volume_ids,
        work_request_id,
        status,
    )


def mutation_lock(
    service: DiscoveryService,
    enabled: bool,
    timeout_seconds: int,
) -> AbstractContextManager[str | None]:
    if not enabled:
        return nullcontext(None)
    if service.options.skip_kubernetes:
        raise WorkflowError(
            "Mutation locking requires Kubernetes access. Remove --skip-kubernetes or use --no-lock."
        )
    return service.kubernetes_backend().mutation_lease(
        duration_seconds=max(300, timeout_seconds + 120)
    )


def wait_for_pool_size(
    service: DiscoveryService,
    pool_name: str,
    target_size: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
    require_rdma_vf: bool = False,
    work_request_id: str | None = None,
    work_request_watches: tuple[ResourceWorkRequestWatch, ...] = (),
    progress: Callable[[str], None] | None = None,
) -> WorkerPoolInfo:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    _configure_wait_discovery(service)
    while True:
        _raise_for_failed_work_requests(
            service,
            (work_request_id,) if work_request_id else (),
            work_request_watches,
        )
        snapshot = service.discover()
        pool = snapshot.pool_by_name(pool_name)
        if pool is None:
            raise WorkflowError(f"Pool disappeared while waiting: {pool_name}")
        pool.rdma_vf_required = pool.rdma_vf_required or require_rdma_vf
        readiness = pool_resource_readiness(snapshot, pool)
        status = (
            f"{pool.name}: desired={pool.desired_size} oci_active={pool.active_oci_instances} "
            f"k8s_ready={pool.ready_k8s_nodes}{readiness_status(readiness)}"
        )
        if progress and status != last_status:
            progress(status)
            last_status = status
        if _pool_matches_target(pool, readiness, target_size):
            return pool
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for {pool_name} to reach size {target_size}. Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def wait_for_pool_creation(
    service: DiscoveryService,
    pool_name: str,
    target_size: int,
    created: ClusterNetworkCreateResult | ManagedNodePoolCreateResult,
    timeout_seconds: int,
    poll_interval_seconds: int,
    require_rdma_vf: bool = False,
    progress: Callable[[str], None] | None = None,
) -> WorkerPoolInfo:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    _configure_wait_discovery(service)
    while True:
        _raise_for_failed_work_requests(
            service,
            (created.work_request_id,) if created.work_request_id else (),
        )
        snapshot = service.discover()
        created_id = (
            created.cluster_network_id
            if isinstance(created, ClusterNetworkCreateResult)
            else created.node_pool_id
        )
        pool = snapshot.pool_by_name(created_id or "")
        if pool is None:
            pool = snapshot.pool_by_name(pool_name)

        if pool is None:
            status = f"{pool_name}: awaiting OCI discovery"
        else:
            pool.rdma_vf_required = pool.rdma_vf_required or require_rdma_vf
            readiness = pool_resource_readiness(snapshot, pool)
            status = (
                f"{pool.name}: desired={pool.desired_size} "
                f"oci_active={pool.active_oci_instances} "
                f"k8s_ready={pool.ready_k8s_nodes}{readiness_status(readiness)}"
            )
            if _pool_matches_target(pool, readiness, target_size):
                return pool

        if progress and status != last_status:
            progress(status)
            last_status = status
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for {pool_name} to be created at size "
                f"{target_size}. Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def wait_for_compute_cluster_active(
    backend: OciBackend,
    compute_cluster_id: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Callable[[str], None] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while True:
        cluster = backend.get_compute_cluster_info(compute_cluster_id)
        lifecycle_state = cluster.lifecycle_state.upper()
        status = (
            f"{cluster.display_name}: compute_cluster={lifecycle_state}"
        )
        if progress and status != last_status:
            progress(status)
            last_status = status
        if lifecycle_state == "ACTIVE":
            return
        if lifecycle_state == "DELETED":
            raise WorkflowError(
                f"Compute Cluster was deleted while waiting: "
                f"{compute_cluster_id}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for Compute Cluster to become ACTIVE. "
                f"Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def wait_for_pool_deleted(
    service: DiscoveryService,
    original_pool: WorkerPoolInfo,
    work_request_id: str | None,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Callable[[str], None] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    _configure_wait_discovery(service)
    while True:
        _raise_for_failed_work_requests(
            service,
            (work_request_id,) if work_request_id else (),
        )
        snapshot = service.discover()
        pool = snapshot.pool_by_name(
            original_pool.backing_id or original_pool.name
        )
        status = (
            f"{original_pool.name}: deleted"
            if pool is None
            else (
                f"{pool.name}: desired={pool.desired_size} "
                f"oci_active={pool.active_oci_instances} "
                f"k8s_ready={pool.ready_k8s_nodes}"
            )
        )
        if progress and status != last_status:
            progress(status)
            last_status = status
        if pool is None:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for pool deletion. Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def wait_for_nodes_removed(
    service: DiscoveryService,
    nodes: tuple[NodeInfo, ...],
    target_sizes: dict[str, int],
    timeout_seconds: int,
    poll_interval_seconds: int,
    original_pools: dict[str, WorkerPoolInfo],
    work_request_ids: tuple[str, ...] = (),
    work_request_watches: tuple[ResourceWorkRequestWatch, ...] = (),
    progress: Callable[[str], None] | None = None,
) -> dict[str, WorkerPoolInfo]:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    _configure_wait_discovery(service)
    while True:
        _raise_for_failed_work_requests(
            service,
            work_request_ids,
            work_request_watches,
        )
        snapshot = service.discover()
        present = [
            node.k8s_name
            for node in nodes
            if snapshot.node_by_identifier(node.k8s_name)
            or snapshot.node_by_identifier(node.instance_ocid or "")
        ]
        observed: dict[str, WorkerPoolInfo] = {}
        pool_states: list[str] = []
        ready = True
        for pool_name, target_size in target_sizes.items():
            pool = snapshot.pool_by_name(pool_name)
            if pool is None:
                raise WorkflowError(f"Pool disappeared while waiting: {pool_name}")
            pool.rdma_vf_required = (
                pool.rdma_vf_required or original_pools[pool_name].rdma_vf_required
            )
            readiness = pool_resource_readiness(snapshot, pool)
            observed[pool_name] = pool
            pool_states.append(
                f"{pool_name}[desired={pool.desired_size},oci_active={pool.active_oci_instances},"
                f"k8s_ready={pool.ready_k8s_nodes}{readiness_status(readiness)}]"
            )
            ready = ready and _pool_matches_target(pool, readiness, target_size)
        status = f"nodes_present={','.join(present) or '-'} " + " ".join(pool_states)
        if progress and status != last_status:
            progress(status)
            last_status = status
        if not present and ready:
            return observed
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for selected nodes to be removed. Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def wait_for_node_boot_volume_replace(
    service: DiscoveryService,
    original_node: NodeInfo,
    original_pool: WorkerPoolInfo,
    old_boot_volume_id: str,
    work_request_id: str | None,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Callable[[str], None] | None = None,
) -> tuple[NodeInfo, WorkerPoolInfo, str]:
    if not original_node.instance_ocid:
        raise WorkflowError(
            f"Node has no OCI instance OCID: {original_node.k8s_name}"
        )
    if original_pool.desired_size is None:
        raise WorkflowError(
            f"Cannot determine desired size for pool: {original_pool.name}"
        )
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    backend = service.oci_backend()
    _configure_wait_discovery(service)
    while True:
        _raise_for_failed_work_requests(
            service,
            (work_request_id,) if work_request_id else (),
        )
        snapshot = service.discover()
        node = snapshot.node_by_identifier(original_node.instance_ocid)
        pool = snapshot.pool_by_name(
            original_pool.backing_id or original_pool.name
        )
        new_boot_volume_id = _try_get_instance_boot_volume_id(
            backend,
            original_node.instance_ocid
        )
        boot_replaced = new_boot_volume_id != old_boot_volume_id
        node_ready = bool(
            node
            and node.k8s_name == original_node.k8s_name
            and (
                not original_node.internal_ip
                or node.internal_ip == original_node.internal_ip
            )
            and node.ready
            and node.schedulable
            and (
                not original_node.boot_id
                or (
                    node.boot_id is not None
                    and node.boot_id != original_node.boot_id
                )
            )
        )
        pool_capacity_stable = False
        gpu_ready = False
        rdma_ready = False
        rdma_vf_ready = False
        if pool:
            pool.rdma_vf_required = (
                pool.rdma_vf_required or original_pool.rdma_vf_required
            )
            pool_capacity_stable = bool(
                pool.desired_size == original_pool.desired_size
                and (
                    pool.active_oci_instances is None
                    or pool.active_oci_instances == original_pool.desired_size
                )
            )
        if node:
            gpu_ready = bool(
                not original_pool.gpu_resource
                or positive_resource(
                    node.allocatable.get(original_pool.gpu_resource)
                )
            )
            rdma_ready = bool(
                not original_pool.rdma_enabled or node.rdma_topology_ready
            )
            rdma_vf_ready = bool(
                not original_pool.rdma_vf_required
                or positive_resource(node.rdma_vf_allocatable)
            )
        status = (
            f"{original_node.k8s_name}: boot_volume_replaced={boot_replaced} "
            f"node_ready={node_ready} "
            f"pool_capacity_stable={pool_capacity_stable} "
            f"gpu_ready={gpu_ready} rdma_ready={rdma_ready} "
            f"rdma_vf_ready={rdma_vf_ready}"
        )
        if progress and status != last_status:
            progress(status)
            last_status = status
        if (
            node
            and pool
            and boot_replaced
            and node_ready
            and pool_capacity_stable
            and gpu_ready
            and rdma_ready
            and rdma_vf_ready
            and new_boot_volume_id
        ):
            return node, pool, new_boot_volume_id
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for node boot volume replacement. "
                f"Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def wait_for_pool_boot_volume_replace(
    service: DiscoveryService,
    prepared: PreparedPoolBootVolumeReplace,
    work_request_id: str | None,
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: Callable[[str], None] | None = None,
) -> tuple[WorkerPoolInfo, dict[str, str]]:
    pool = prepared.pool
    if not pool.node_pool_id:
        raise WorkflowError(
            f"Managed OKE pool is missing its node-pool OCID: {pool.name}"
        )
    if pool.desired_size is None:
        raise WorkflowError(
            f"Cannot determine desired size for pool: {pool.name}"
        )
    original_nodes = {
        node.instance_ocid: node
        for node in prepared.nodes
        if node.instance_ocid
    }
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    backend = service.oci_backend()
    _configure_wait_discovery(service)
    while True:
        _raise_for_failed_work_requests(
            service,
            (work_request_id,) if work_request_id else (),
        )
        snapshot = service.discover()
        observed_pool = snapshot.pool_by_name(pool.node_pool_id)
        new_boot_volume_ids = {}
        for instance_id in original_nodes:
            boot_volume_id = _try_get_instance_boot_volume_id(
                backend,
                instance_id,
            )
            if boot_volume_id:
                new_boot_volume_ids[instance_id] = boot_volume_id
        replaced = {
            instance_id
            for instance_id, boot_volume_id in new_boot_volume_ids.items()
            if boot_volume_id
            != prepared.old_boot_volume_ids[instance_id]
        }
        identity_ready = True
        for instance_id, original_node in original_nodes.items():
            node = snapshot.node_by_identifier(instance_id)
            identity_ready = identity_ready and bool(
                node
                and node.k8s_name == original_node.k8s_name
                and (
                    not original_node.internal_ip
                    or node.internal_ip == original_node.internal_ip
                )
                and node.ready
                and node.schedulable
                and (
                    not original_node.boot_id
                    or (
                        node.boot_id is not None
                        and node.boot_id != original_node.boot_id
                    )
                )
            )
        pool_ready = False
        readiness = PoolResourceReadiness()
        if observed_pool:
            observed_pool.rdma_vf_required = (
                observed_pool.rdma_vf_required or pool.rdma_vf_required
            )
            readiness = pool_resource_readiness(snapshot, observed_pool)
            pool_ready = _pool_matches_target(
                observed_pool,
                readiness,
                pool.desired_size,
            )
        properties_applied = (
            backend.managed_pool_boot_volume_replace_applied(
                pool.node_pool_id,
                prepared.spec,
            )
        )
        status = (
            f"{pool.name}: replaced={len(replaced)}/{len(original_nodes)} "
            f"identity_ready={identity_ready} pool_ready={pool_ready} "
            f"properties_applied={properties_applied}"
            f"{readiness_status(readiness)}"
        )
        if progress and status != last_status:
            progress(status)
            last_status = status
        if (
            observed_pool
            and len(replaced) == len(original_nodes)
            and identity_ready
            and pool_ready
            and properties_applied
        ):
            return observed_pool, new_boot_volume_ids
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for managed-pool boot volume replacement. "
                f"Last status: {status}"
            )
        time.sleep(poll_interval_seconds)


def pool_resource_readiness(
    snapshot: DiscoverySnapshot,
    pool: WorkerPoolInfo,
) -> PoolResourceReadiness:
    pool_nodes = [node for node in snapshot.nodes if node.pool_name == pool.name and node.ready]
    gpu_ready = None
    if pool.gpu_resource:
        gpu_ready = sum(
            1 for node in pool_nodes if positive_resource(node.allocatable.get(pool.gpu_resource))
        )
    rdma_ready = None
    if pool.rdma_enabled:
        rdma_ready = sum(1 for node in pool_nodes if node.rdma_topology_ready)
    rdma_vf_ready = None
    if pool.rdma_vf_required:
        rdma_vf_ready = sum(
            1 for node in pool_nodes if positive_resource(node.rdma_vf_allocatable)
        )
    return PoolResourceReadiness(
        gpu_ready=gpu_ready,
        rdma_topology_ready=rdma_ready,
        rdma_vf_ready=rdma_vf_ready,
    )


def readiness_status(readiness: PoolResourceReadiness) -> str:
    fields = (
        ("gpu_ready", readiness.gpu_ready),
        ("rdma_ready", readiness.rdma_topology_ready),
        ("rdma_vf_ready", readiness.rdma_vf_ready),
    )
    return "".join(f" {name}={value}" for name, value in fields if value is not None)


def resource_counts_match(readiness: PoolResourceReadiness, target_size: int) -> bool:
    counts = (
        readiness.gpu_ready,
        readiness.rdma_topology_ready,
        readiness.rdma_vf_ready,
    )
    return all(count is None or count == target_size for count in counts)


def positive_resource(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False


def resize_result_row(
    pool: WorkerPoolInfo,
    old_size: int,
    target_size: int,
    work_request_id: str | None,
    status: str,
) -> dict[str, object]:
    return {
        "name": pool.name,
        "kind": pool.kind,
        "shape": pool.shape,
        "old_size": old_size,
        "target_size": target_size,
        "oci_active": pool.active_oci_instances,
        "k8s_ready": pool.ready_k8s_nodes,
        "status": status,
        "work_request_id": work_request_id,
    }


def pool_create_result_row(
    prepared: PreparedPoolCreate,
    created: ClusterNetworkCreateResult | ManagedNodePoolCreateResult,
    observed_pool: WorkerPoolInfo | None,
    status: str,
) -> dict[str, object]:
    source_pool = prepared.source_pool
    result: dict[str, object] = {
        "name": prepared.name,
        "kind": (
            "cluster-network"
            if isinstance(created, ClusterNetworkCreateResult)
            else "node-pool"
        ),
        "placement": (
            "cluster-network"
            if isinstance(created, ClusterNetworkCreateResult)
            else "compute-cluster"
            if created.compute_cluster_id
            else "host-group" if created.host_group_id else "standard"
        ),
        "type": prepared.spec.pool_type,
        "source_pool": source_pool.name,
        "shape": observed_pool.shape if observed_pool else source_pool.shape,
        "target_size": prepared.count,
        "oci_active": (
            observed_pool.active_oci_instances if observed_pool else None
        ),
        "k8s_ready": observed_pool.ready_k8s_nodes if observed_pool else 0,
        "status": status,
        "work_request_id": created.work_request_id,
    }
    if isinstance(created, ClusterNetworkCreateResult):
        result.update(
            {
                "cluster_network_id": created.cluster_network_id,
                "instance_pool_id": (
                    observed_pool.instance_pool_id
                    if observed_pool and observed_pool.instance_pool_id
                    else created.instance_pool_id
                ),
                "instance_configuration_id": (
                    created.instance_configuration_id
                ),
            }
        )
    else:
        result["node_pool_id"] = (
            observed_pool.node_pool_id
            if observed_pool and observed_pool.node_pool_id
            else created.node_pool_id
        )
        if created.compute_cluster_id:
            result["compute_cluster_id"] = created.compute_cluster_id
            result["compute_cluster_created"] = (
                created.compute_cluster_created
            )
        if created.host_group_id:
            result["host_group_id"] = created.host_group_id
    return result


def pool_delete_result_row(
    pool: WorkerPoolInfo,
    work_request_id: str | None,
    status: str,
    *,
    instance_configuration_id: str | None = None,
    instance_configuration_status: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": pool.name,
        "kind": pool.kind,
        "placement": pool.placement_type,
        "old_size": pool.desired_size,
        "target_size": 0,
        "status": status,
        "work_request_id": work_request_id,
    }
    if instance_configuration_id:
        result["instance_configuration_id"] = instance_configuration_id
        result["instance_configuration_status"] = instance_configuration_status
    return result


def node_remove_result_row(
    pool: WorkerPoolInfo,
    node: NodeInfo,
    target_size: int,
    decrement_size: bool,
    work_request_id: str | None,
    status: str,
    host_tag: CustomerReportedHostStatus | None = None,
    host_tag_status: str = "not-requested",
) -> dict[str, object]:
    return {
        "node": node.k8s_name,
        "slurm_name": node.slurm_name,
        "ip": node.internal_ip,
        "pool": pool.name,
        "shape": node.shape,
        "target_size": target_size,
        "decrement_size": decrement_size,
        "oci_active": pool.active_oci_instances,
        "k8s_ready": pool.ready_k8s_nodes,
        "status": status,
        "host_tag": host_tag.value if host_tag is not None else None,
        "host_tag_status": host_tag_status,
        "work_request_id": work_request_id,
    }


def node_boot_volume_replace_result_row(
    node: NodeInfo,
    pool: WorkerPoolInfo,
    old_boot_volume_id: str,
    new_boot_volume_id: str | None,
    work_request_id: str | None,
    status: str,
) -> dict[str, object]:
    return {
        "node": node.k8s_name,
        "ip": node.internal_ip,
        "pool": pool.name,
        "kind": pool.kind,
        "shape": node.shape,
        "instance_ocid": node.instance_ocid,
        "same_instance": True,
        "old_boot_volume_id": old_boot_volume_id,
        "new_boot_volume_id": new_boot_volume_id,
        "preserves_existing_configuration": True,
        "status": status,
        "work_request_id": work_request_id,
    }


def pool_boot_volume_replace_result_row(
    pool: WorkerPoolInfo,
    prepared: PreparedPoolBootVolumeReplace,
    new_boot_volume_ids: dict[str, str],
    work_request_id: str | None,
    status: str,
) -> dict[str, object]:
    return {
        "name": pool.name,
        "kind": pool.kind,
        "shape": pool.shape,
        "size": pool.desired_size,
        "replaced_nodes": len(new_boot_volume_ids),
        "updates": prepared.spec.as_dict(),
        "new_boot_volume_ids": new_boot_volume_ids,
        "status": status,
        "work_request_id": work_request_id,
    }


def _validate_pool_mutation(pool: WorkerPoolInfo) -> None:
    if pool.kind not in {"node-pool", "cluster-network", "instance-pool"}:
        raise WorkflowError(
            f"Mutation for pool kind '{pool.kind}' is not supported: {pool.name}"
        )
    if pool.autoscaler_owned:
        raise WorkflowError(f"Refusing to mutate autoscaler-owned pool: {pool.name}")


def _validate_boot_volume_pool(
    snapshot: DiscoverySnapshot,
    pool: WorkerPoolInfo,
    *,
    allow_system_pool: bool,
    require_fully_ready: bool,
) -> None:
    _validate_pool_mutation(pool)
    if pool.name.casefold() == "oke-system" and not allow_system_pool:
        raise WorkflowError(
            "Refusing to replace boot volumes in the OKE system pool. Use "
            "--allow-system-pool only after another system-capable pool is ready."
        )
    if pool.desired_size is None:
        raise WorkflowError(
            f"Cannot determine desired size for pool: {pool.name}"
        )
    if not require_fully_ready:
        return
    readiness = pool_resource_readiness(snapshot, pool)
    if not _pool_matches_target(pool, readiness, pool.desired_size):
        raise WorkflowError(
            f"Pool must be fully Ready before boot volume replacement: "
            f"{pool.name} (desired={pool.desired_size}, "
            f"oci_active={pool.active_oci_instances}, "
            f"k8s_ready={pool.ready_k8s_nodes}"
            f"{readiness_status(readiness)})"
        )


def _try_get_instance_boot_volume_id(
    backend: OciBackend,
    instance_id: str,
) -> str | None:
    try:
        return backend.get_instance_boot_volume_id(instance_id)
    except BootVolumeAttachmentPending:
        return None


def _require_enhanced_cluster(cluster_type: str) -> None:
    if cluster_type.upper() != "ENHANCED_CLUSTER":
        raise WorkflowError(
            "OKE boot volume replacement requires an enhanced cluster; "
            f"discovered cluster type: {cluster_type or 'unknown'}."
        )


def _select_pool_template(
    snapshot: DiscoverySnapshot,
    spec: PoolCreateSpec,
    source_identifier: str | None,
) -> WorkerPoolInfo:
    pool_type = spec.pool_type
    candidates = [
        pool
        for pool in snapshot.pools
        if _pool_matches_create_type(pool, spec)
    ]
    if source_identifier:
        selected = snapshot.pool_by_name(source_identifier)
        if selected is None:
            raise WorkflowNotFound(f"Source pool not found: {source_identifier}")
        if selected not in candidates:
            raise WorkflowError(
                f"Source pool is not an eligible {pool_type} template: "
                f"{selected.name}"
            )
        return selected

    if spec.uses_compute_cluster:
        compute_cluster_sources = [
            pool for pool in candidates if pool.compute_cluster_id
        ]
        conventional_rdma = [
            pool
            for pool in compute_cluster_sources
            if pool.name == "oke-rdma"
        ]
        if len(conventional_rdma) == 1:
            return conventional_rdma[0]
        if len(compute_cluster_sources) == 1:
            return compute_cluster_sources[0]
        if len(compute_cluster_sources) > 1:
            names = ", ".join(
                sorted(pool.name for pool in compute_cluster_sources)
            )
            raise WorkflowError(
                "Multiple managed Compute Cluster RDMA templates are "
                f"available. Select one with --from-pool: {names}"
            )

        conventional_gpu = [
            pool for pool in candidates if pool.name == "oke-gpu"
        ]
        if len(conventional_gpu) == 1:
            return conventional_gpu[0]

    conventional_name = {
        "cpu": "oke-cpu",
        "gpu": "oke-gpu",
        "rdma": "oke-rdma",
    }[spec.pool_type]
    conventional = [pool for pool in candidates if pool.name == conventional_name]
    if len(conventional) == 1:
        return conventional[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise WorkflowError(
            f"No eligible {pool_type} pool is available as a creation template."
        )
    names = ", ".join(sorted(pool.name for pool in candidates))
    raise WorkflowError(
        f"Multiple {pool_type} pool templates are available. Select one with "
        f"--from-pool: {names}"
    )


def _select_legacy_bootstrap_template(
    snapshot: DiscoverySnapshot,
    spec: PoolCreateSpec,
    identifier: str | None,
) -> WorkerPoolInfo | None:
    if not identifier:
        return None
    if not spec.uses_compute_cluster:
        raise WorkflowError(
            "--bootstrap-from-pool is valid only with --type rdma "
            "--rdma-mode compute-cluster."
        )
    pool = snapshot.pool_by_name(identifier)
    if pool is None:
        raise WorkflowNotFound(f"Legacy bootstrap source pool not found: {identifier}")
    if (
        pool.kind != "cluster-network"
        or not pool.cluster_network_id
        or not pool.instance_pool_id
    ):
        raise WorkflowError(
            "Legacy bootstrap source must be a Cluster Network-backed RDMA pool: "
            f"{pool.name}"
        )
    return pool


def _load_legacy_bootstrap_metadata(
    backend: OciBackend,
    pool: WorkerPoolInfo,
) -> dict[str, str]:
    if not pool.cluster_network_id or not pool.instance_pool_id:
        raise WorkflowError(
            "Legacy bootstrap source is missing Cluster Network identifiers: "
            f"{pool.name}"
        )
    return backend.get_cluster_network_pool_bootstrap_metadata(
        pool.cluster_network_id,
        pool.instance_pool_id,
    )


def _legacy_bootstrap_summary(
    pool: WorkerPoolInfo,
    metadata: dict[str, str],
) -> dict[str, object]:
    try:
        return summarize_worker_bootstrap(metadata)
    except BootstrapCompositionError as exc:
        raise WorkflowError(
            f"Legacy bootstrap source {pool.name} cannot be inspected safely: {exc}"
        ) from exc


def _pool_matches_create_type(
    pool: WorkerPoolInfo,
    spec: PoolCreateSpec,
) -> bool:
    pool_type = spec.pool_type
    if pool_type == "rdma" and not spec.managed:
        return bool(
            pool.kind == "cluster-network"
            and pool.cluster_network_id
            and pool.instance_pool_id
        )
    if pool_type == "rdma":
        return bool(
            pool.kind == "node-pool"
            and pool.node_pool_id
            and pool.gpu_resource
            and (pool.compute_cluster_id or not pool.rdma_enabled)
        )
    if pool.kind != "node-pool" or not pool.node_pool_id:
        return False
    if pool_type == "gpu":
        return bool(pool.gpu_resource and not pool.rdma_enabled)
    return not pool.gpu_resource and not pool.rdma_enabled


def _ensure_pool_name_available(
    snapshot: DiscoverySnapshot,
    name: str,
) -> None:
    if any(pool.name.casefold() == name.casefold() for pool in snapshot.pools):
        raise WorkflowError(f"A worker pool named '{name}' already exists.")


def _require_complete_pool_inventory(snapshot: DiscoverySnapshot) -> None:
    if not snapshot.oci_discovery_enabled:
        raise WorkflowError("Pool mutation requires complete OCI pool discovery.")
    failure_prefixes = (
        "Managed node pool discovery skipped:",
        "Cluster network discovery skipped:",
        "Standalone instance pool discovery skipped:",
    )
    failures = [
        warning
        for warning in snapshot.warnings
        if warning.startswith(failure_prefixes)
    ]
    if failures:
        raise WorkflowError(
            "Pool mutation requires complete OCI pool discovery: "
            + " ".join(failures)
        )


def _pool_owner(pool: WorkerPoolInfo) -> tuple[str, str]:
    if pool.kind == "node-pool" and pool.node_pool_id:
        return "oke", "update the managed OKE node-pool desired size"
    if pool.kind == "cluster-network" and pool.cluster_network_id and pool.instance_pool_id:
        return "compute-management", "update the Cluster Network's embedded Instance Pool size"
    if pool.kind == "instance-pool" and pool.instance_pool_id:
        return "compute-management", "update the standalone Instance Pool size"
    raise WorkflowError(f"Pool is missing its required OCI backing identifier: {pool.name}")


def _pool_delete_owner(pool: WorkerPoolInfo) -> tuple[str, str]:
    if pool.kind == "node-pool" and pool.node_pool_id:
        return "oke", "delete the managed OKE node-pool resource"
    if pool.kind == "cluster-network" and pool.cluster_network_id:
        return (
            "compute-management",
            "terminate the Cluster Network and its embedded Instance Pool",
        )
    if pool.kind == "instance-pool" and pool.instance_pool_id:
        return "compute-management", "terminate the standalone Instance Pool"
    raise WorkflowError(
        f"Pool is missing its required OCI backing identifier: {pool.name}"
    )


def _node_owner(pool: WorkerPoolInfo) -> tuple[str, str]:
    if pool.kind == "node-pool" and pool.node_pool_id:
        return "oke", "delete the selected worker through OKE DeleteNode"
    if pool.kind in {"cluster-network", "instance-pool"} and pool.instance_pool_id:
        return "compute-management", "detach and automatically terminate the selected Instance Pool member"
    raise WorkflowError(f"Pool is missing its required OCI backing identifier: {pool.name}")


def _pool_for_node(snapshot: DiscoverySnapshot, node: NodeInfo) -> WorkerPoolInfo:
    pool = snapshot.pool_by_name(node.pool_name or node.node_pool_id or "")
    if pool is None and node.node_pool_id:
        pool = snapshot.pool_by_name(node.node_pool_id)
    if pool is None:
        raise WorkflowError(f"Cannot determine pool for node: {node.k8s_name}")
    return pool


def _nodes_for_pool(
    snapshot: DiscoverySnapshot,
    pool: WorkerPoolInfo,
) -> list[NodeInfo]:
    return [
        node
        for node in snapshot.nodes
        if node.pool_name == pool.name
        or (
            pool.node_pool_id is not None
            and node.node_pool_id == pool.node_pool_id
        )
    ]


def _pool_for_prepared_node(
    prepared: PreparedNodeRemoval | PreparedNodeBootVolumeReplace,
    node: NodeInfo,
) -> WorkerPoolInfo:
    if node.pool_name and node.pool_name in prepared.pools:
        return prepared.pools[node.pool_name]
    matches = [pool for pool in prepared.pools.values() if node.instance_ocid in pool.oci_instance_ids]
    if len(matches) == 1:
        return matches[0]
    raise WorkflowError(f"Cannot determine prepared pool for node: {node.k8s_name}")


def _validate_drain_pods(
    node: NodeInfo,
    pods: list[DrainPod],
    delete_emptydir_data: bool,
    force_unmanaged: bool,
) -> None:
    empty_dir = [pod for pod in pods if pod.evictable and pod.has_empty_dir]
    if empty_dir and not delete_emptydir_data:
        names = ", ".join(f"{pod.namespace}/{pod.name}" for pod in empty_dir)
        raise WorkflowError(
            f"Refusing to drain {node.k8s_name}: pods use emptyDir data: {names}. "
            "Use --delete-emptydir-data to acknowledge data loss."
        )
    unmanaged = [pod for pod in pods if pod.evictable and not pod.controller]
    if unmanaged and not force_unmanaged:
        names = ", ".join(f"{pod.namespace}/{pod.name}" for pod in unmanaged)
        raise WorkflowError(
            f"Refusing to drain {node.k8s_name}: unmanaged pods were found: {names}. "
            "Use --force to acknowledge that they will not be recreated."
        )


def _pool_matches_target(
    pool: WorkerPoolInfo,
    readiness: PoolResourceReadiness,
    target_size: int,
) -> bool:
    return bool(
        pool.desired_size == target_size
        and (pool.active_oci_instances is None or pool.active_oci_instances == target_size)
        and pool.ready_k8s_nodes == target_size
        and resource_counts_match(readiness, target_size)
    )


def _configure_wait_discovery(service: DiscoveryService) -> None:
    service.options.include_pod_counts = False
    service.options.include_autoscaler = False
    service.options.include_kueue = False
    service.options.include_addons = False


def _raise_for_failed_work_requests(
    service: DiscoveryService,
    work_request_ids: tuple[str, ...],
    resource_watches: tuple[ResourceWorkRequestWatch, ...] = (),
) -> None:
    if not work_request_ids and not resource_watches:
        return
    backend = service.oci_backend()
    target = service.resolve_oci_target(require_compartment=True)
    if not target.compartment_id:
        raise WorkflowError("OCI compartment is required to monitor work requests.")
    checked_ids: set[str] = set()
    for work_request_id in work_request_ids:
        checked_ids.add(work_request_id)
        work_request = backend.get_work_request_status(
            work_request_id,
            compartment_id=target.compartment_id,
        )
        _raise_for_failed_work_request(work_request)

    for watch in resource_watches:
        summaries = backend.list_resource_work_requests(
            watch.compartment_id,
            watch.resource_id,
        )
        for summary in summaries:
            work_request_id = summary.work_request_id
            if work_request_id in watch.ignored_ids or work_request_id in checked_ids:
                continue
            checked_ids.add(work_request_id)
            if not summary.failed:
                continue
            work_request = backend.get_work_request_status(
                work_request_id,
                compartment_id=target.compartment_id,
            )
            _raise_for_failed_work_request(work_request)


def _raise_for_failed_work_request(work_request: WorkRequestInfo) -> None:
    if not work_request.failed:
        return
    details = "; ".join(work_request.errors) or "no error details were returned"
    raise WorkflowError(
        f"OCI work request {work_request.work_request_id} ended in "
        f"{work_request.status}: {details}"
    )


def _prepare_resource_work_request_watches(
    service: DiscoveryService,
    resource_ids: tuple[str, ...],
) -> tuple[ResourceWorkRequestWatch, ...]:
    if not resource_ids:
        return ()
    target = service.resolve_oci_target(require_compartment=True)
    if not target.compartment_id:
        raise WorkflowError("OCI compartment is required to monitor resource work requests.")
    backend = service.oci_backend()
    watches: list[ResourceWorkRequestWatch] = []
    for resource_id in sorted(set(resource_ids)):
        existing = backend.list_resource_work_requests(
            target.compartment_id,
            resource_id,
        )
        watches.append(
            ResourceWorkRequestWatch(
                compartment_id=target.compartment_id,
                resource_id=resource_id,
                ignored_ids=frozenset(item.work_request_id for item in existing),
            )
        )
    return tuple(watches)


def _pool_resize_work_request_resources(pool: WorkerPoolInfo) -> tuple[str, ...]:
    if pool.kind == "cluster-network" and pool.cluster_network_id:
        return (pool.cluster_network_id,)
    if pool.kind == "instance-pool" and pool.instance_pool_id:
        return (pool.instance_pool_id,)
    return ()


def _node_removal_work_request_resources(
    pools: Iterable[WorkerPoolInfo],
) -> tuple[str, ...]:
    resources: set[str] = set()
    for pool in pools:
        if pool.kind not in {"cluster-network", "instance-pool"}:
            continue
        if pool.instance_pool_id:
            resources.add(pool.instance_pool_id)
        if pool.cluster_network_id:
            resources.add(pool.cluster_network_id)
    return tuple(sorted(resources))


def _slinky_pool_mutation_error(pool_name: str) -> str:
    return (
        f"Refusing to scale down Slinky-managed pool {pool_name}: Slurm-aware drain is required "
        "before OKE capacity is removed. Scale-up remains supported."
    )


def _slinky_node_mutation_error(node_name: str, pool_name: str) -> str:
    return (
        f"Refusing to remove Slinky-managed node {node_name} from {pool_name}: "
        "Slurm-aware drain is required before node deletion or replacement."
    )


def _slinky_pool_bvr_error(pool_name: str) -> str:
    return (
        f"Refusing to replace boot volumes in Slinky-managed pool {pool_name}: "
        "Slurm-aware drain and resume coordination is required."
    )


def _slinky_node_bvr_error(node_name: str, pool_name: str) -> str:
    return (
        f"Refusing to replace the boot volume of Slinky-managed node "
        f"{node_name} in {pool_name}: Slurm-aware drain and resume coordination "
        "is required."
    )
