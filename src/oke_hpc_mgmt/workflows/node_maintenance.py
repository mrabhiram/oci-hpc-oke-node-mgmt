from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from oke_hpc_mgmt.discovery import DiscoveryService
from oke_hpc_mgmt.models import DiscoverySnapshot, DrainPod, NodeInfo, OperationPlan
from oke_hpc_mgmt.selection import select_nodes
from oke_hpc_mgmt.workflows.lifecycle import (
    WorkflowError,
    WorkflowNotFound,
    mutation_lock,
)


NODE_MAINTENANCE_ACTIONS = ("cordon", "uncordon", "drain")


@dataclass(frozen=True)
class PreparedNodeMaintenance:
    action: str
    snapshot: DiscoverySnapshot
    nodes: tuple[NodeInfo, ...]
    plans: tuple[OperationPlan, ...]
    drain_pods: dict[str, tuple[DrainPod, ...]]


def prepare_node_maintenance(
    service: DiscoveryService,
    action: str,
    identifiers: Iterable[str] = (),
    fields: str | None = None,
    delete_emptydir_data: bool = False,
    force_unmanaged: bool = False,
    grace_period_seconds: int = 30,
) -> PreparedNodeMaintenance:
    if action not in NODE_MAINTENANCE_ACTIONS:
        raise WorkflowError(
            f"Unknown node maintenance action '{action}'. Valid actions: "
            f"{', '.join(NODE_MAINTENANCE_ACTIONS)}"
        )
    identifier_tuple = tuple(identifiers)
    if not identifier_tuple and not fields:
        raise WorkflowError("Specify nodes by identifier or with --fields.")
    if service.options.skip_kubernetes:
        raise WorkflowError("Node maintenance requires Kubernetes access.")

    snapshot = service.discover()
    nodes, missing = select_nodes(snapshot, identifiers=identifier_tuple, fields=fields)
    if missing:
        raise WorkflowNotFound(f"Nodes not found: {', '.join(missing)}")
    if not nodes:
        raise WorkflowNotFound("No nodes matched the requested selection.")

    kubernetes = service.kubernetes_backend()
    drain_pods: dict[str, tuple[DrainPod, ...]] = {}
    plans: list[OperationPlan] = []
    for node in nodes:
        if action == "drain" and node.slinky_managed:
            raise WorkflowError(
                f"Refusing to drain Slinky-managed node {node.k8s_name}: use a Slurm-aware drain workflow."
            )
        steps: tuple[str, ...]
        warnings: list[str] = []
        workload_pods = node.running_workload_pods
        if action == "cordon":
            steps = ("set spec.unschedulable=true",)
        elif action == "uncordon":
            steps = ("set spec.unschedulable=false",)
        else:
            pods = kubernetes.list_drain_pods(
                node.k8s_name,
                grace_period_seconds=grace_period_seconds,
                check_evictions=True,
            )
            _validate_drain_pods(node, pods, delete_emptydir_data, force_unmanaged)
            drain_pods[node.k8s_name] = tuple(pods)
            blockers = [pod for pod in pods if pod.evictable and pod.eviction_blocker]
            if blockers:
                warnings.append(
                    "Eviction dry-run reported blockers: "
                    + ", ".join(
                        f"{pod.namespace}/{pod.name} ({pod.eviction_blocker})" for pod in blockers
                    )
                )
            workload_pods = sum(1 for pod in pods if pod.evictable)
            steps = (
                "set spec.unschedulable=true",
                "evict non-DaemonSet pods through the policy/v1 Eviction API",
                "wait for evicted pods to leave the node",
            )
        plans.append(
            OperationPlan(
                operation=f"node-{action}",
                target=node.k8s_name,
                pool=node.pool_name,
                owner="kubernetes",
                workload_pods=workload_pods,
                steps=steps,
                warnings=tuple(warnings),
            )
        )

    return PreparedNodeMaintenance(
        action=action,
        snapshot=snapshot,
        nodes=tuple(nodes),
        plans=tuple(plans),
        drain_pods=drain_pods,
    )


def execute_node_maintenance(
    service: DiscoveryService,
    prepared: PreparedNodeMaintenance,
    grace_period_seconds: int = 30,
    timeout_seconds: int = 600,
    lock: bool = True,
) -> list[dict[str, object]]:
    kubernetes = service.kubernetes_backend()
    with mutation_lock(service, lock, timeout_seconds):
        for node in prepared.nodes:
            if prepared.action == "cordon":
                kubernetes.cordon_node(node.k8s_name)
            elif prepared.action == "uncordon":
                kubernetes.uncordon_node(node.k8s_name)
            else:
                kubernetes.cordon_node(node.k8s_name)
                kubernetes.evict_drain_pods(
                    list(prepared.drain_pods[node.k8s_name]),
                    grace_period_seconds=grace_period_seconds,
                    timeout_seconds=timeout_seconds,
                )
    return [
        {
            "operation": f"node-{prepared.action}",
            "node": node.k8s_name,
            "pool": node.pool_name,
            "status": "completed",
            "pods": sum(
                1 for pod in prepared.drain_pods.get(node.k8s_name, ()) if pod.evictable
            ),
        }
        for node in prepared.nodes
    ]


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
