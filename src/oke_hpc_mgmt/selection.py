from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo


class SelectionError(ValueError):
    """Raised when a node selector is invalid or ambiguous."""


NODE_FILTER_FIELDS = frozenset(
    {
        "name",
        "slurm_name",
        "ip",
        "status",
        "pool",
        "shape",
        "ready",
        "schedulable",
        "gpu",
        "rdma",
        "rdma_vf",
        "workload_pods",
        "slurm_pods",
        "system_pods",
        "daemonsets",
    }
)


def parse_field_filters(specification: str | None) -> dict[str, object]:
    if not specification:
        return {}

    filters: dict[str, object] = {}
    for item in specification.split(","):
        field, separator, raw_value = item.strip().partition("=")
        if not separator or not field or not raw_value:
            raise SelectionError(
                f"Invalid field filter '{item}'. Use comma-separated key=value filters."
            )
        if field not in NODE_FILTER_FIELDS:
            valid = ", ".join(sorted(NODE_FILTER_FIELDS))
            raise SelectionError(f"Unknown node field '{field}'. Valid fields: {valid}")
        filters[field] = _typed_filter_value(raw_value)
    return filters


def split_identifiers(values: Iterable[str]) -> list[str]:
    identifiers: list[str] = []
    for value in values:
        identifiers.extend(item.strip() for item in value.split(",") if item.strip())
    return list(dict.fromkeys(identifiers))


def select_nodes(
    snapshot: DiscoverySnapshot,
    identifiers: Iterable[str] = (),
    fields: str | None = None,
    pool: str | None = None,
    rdma_only: bool = False,
    not_ready: bool = False,
    workloads: bool = False,
) -> tuple[list[NodeInfo], list[str]]:
    requested = split_identifiers(identifiers)
    filters = parse_field_filters(fields)
    missing: list[str] = []

    if requested:
        nodes: list[NodeInfo] = []
        for identifier in requested:
            node = snapshot.node_by_identifier(identifier)
            if node is None:
                missing.append(identifier)
            elif node not in nodes:
                nodes.append(node)
    else:
        nodes = list(snapshot.nodes)

    if filters:
        nodes = [node for node in nodes if _matches_filters(node, filters)]
    if pool:
        nodes = [node for node in nodes if node.pool_name == pool]
    if rdma_only:
        nodes = [node for node in nodes if node.rdma_topology_ready]
    if not_ready:
        nodes = [node for node in nodes if not node.ready]
    if workloads:
        nodes = [node for node in nodes if node.running_workload_pods > 0]
    return nodes, missing


def node_filter_values(node: NodeInfo) -> dict[str, object]:
    return {
        "name": node.k8s_name,
        "slurm_name": node.slurm_name,
        "ip": node.internal_ip,
        "status": node.status,
        "pool": node.pool_name,
        "shape": node.shape,
        "ready": node.ready,
        "schedulable": node.schedulable,
        "gpu": bool(node.gpu_allocatable),
        "rdma": node.rdma_topology_ready,
        "rdma_vf": _positive_resource(node.rdma_vf_allocatable),
        "workload_pods": node.running_workload_pods,
        "slurm_pods": node.slinky_workload_pods,
        "system_pods": node.system_pods,
        "daemonsets": node.daemonset_pods,
    }


def _matches_filters(node: NodeInfo, filters: dict[str, object]) -> bool:
    values = node_filter_values(node)
    return all(_values_equal(values[field], expected) for field, expected in filters.items())


def _typed_filter_value(value: str) -> object:
    normalized = value.strip().lower()
    if normalized in {"true", "yes"}:
        return True
    if normalized in {"false", "no"}:
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _values_equal(actual: Any, expected: object) -> bool:
    if actual is None:
        return str(expected).lower() in {"none", "null", "-"}
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.lower() == expected.lower()
    return actual == expected


def _positive_resource(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False
