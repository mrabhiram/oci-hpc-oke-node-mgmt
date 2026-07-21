from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import ContextManager

from oke_hpc_mgmt.discovery import DiscoveryService
from oke_hpc_mgmt.models import (
    DiscoverySnapshot,
    DrainPod,
    NodeInfo,
    OperationPlan,
    PoolResourceReadiness,
    WorkerPoolInfo,
    WorkRequestInfo,
)
from oke_hpc_mgmt.selection import select_nodes


IAC_DRIFT_WARNING = (
    "This direct OCI mutation does not update Terraform or OCI Resource Manager "
    "input values; reconcile the declared pool size before the next apply."
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
class PreparedNodeRemoval:
    snapshot: DiscoverySnapshot
    nodes: tuple[NodeInfo, ...]
    pools: dict[str, WorkerPoolInfo]
    plans: tuple[OperationPlan, ...]
    drain_pods: dict[str, tuple[DrainPod, ...]]
    target_sizes: dict[str, int]
    decrement_size: bool


@dataclass(frozen=True)
class ResourceWorkRequestWatch:
    compartment_id: str
    resource_id: str
    ignored_ids: frozenset[str]


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
        )
        for node in prepared.nodes
        for pool in [_pool_for_prepared_node(prepared, node)]
    ]


def mutation_lock(
    service: DiscoveryService,
    enabled: bool,
    timeout_seconds: int,
) -> ContextManager[str | None]:
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


def node_remove_result_row(
    pool: WorkerPoolInfo,
    node: NodeInfo,
    target_size: int,
    decrement_size: bool,
    work_request_id: str | None,
    status: str,
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
        "work_request_id": work_request_id,
    }


def _validate_pool_mutation(pool: WorkerPoolInfo) -> None:
    if pool.kind not in {"node-pool", "cluster-network", "instance-pool"}:
        raise WorkflowError(
            f"Mutation for pool kind '{pool.kind}' is not supported: {pool.name}"
        )
    if pool.autoscaler_owned:
        raise WorkflowError(f"Refusing to mutate autoscaler-owned pool: {pool.name}")


def _pool_owner(pool: WorkerPoolInfo) -> tuple[str, str]:
    if pool.kind == "node-pool" and pool.node_pool_id:
        return "oke", "update the managed OKE node-pool desired size"
    if pool.kind == "cluster-network" and pool.cluster_network_id and pool.instance_pool_id:
        return "compute-management", "update the Cluster Network's embedded Instance Pool size"
    if pool.kind == "instance-pool" and pool.instance_pool_id:
        return "compute-management", "update the standalone Instance Pool size"
    raise WorkflowError(f"Pool is missing its required OCI backing identifier: {pool.name}")


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


def _pool_for_prepared_node(
    prepared: PreparedNodeRemoval,
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
