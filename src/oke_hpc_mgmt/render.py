from __future__ import annotations

import csv
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from typing import Any

from oke_hpc_mgmt.models import (
    AddonInfo,
    DiscoverySnapshot,
    HealthResult,
    NodeInfo,
    OperationPlan,
    WorkerPoolInfo,
)

OUTPUT_SCHEMA_VERSION = "v1"


def serializable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: serializable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if isinstance(value, set):
        return sorted(value)
    return value


def print_records(
    records: list[dict[str, Any]],
    output: str,
    columns: list[str] | None = None,
    show_header: bool = True,
    one_line: bool = False,
) -> None:
    if output == "json":
        indent = None if one_line else 2
        separators = (",", ":") if one_line else None
        print(json.dumps(serializable(records), indent=indent, sort_keys=True, separators=separators))
        return
    if output == "csv":
        print_csv(records, columns, show_header=show_header)
        return
    if one_line:
        print(",".join(_cell(record.get("name")) for record in records))
        return
    print_table(records, columns, show_header=show_header)


def print_snapshot(snapshot: DiscoverySnapshot, output: str) -> None:
    if output == "json":
        print(json.dumps(serializable(snapshot), indent=2, sort_keys=True))
        return
    if output == "csv":
        records = _snapshot_csv_records(snapshot)
        columns = ["record_type"]
        for record in records:
            for key in record:
                if key not in columns:
                    columns.append(key)
        print_csv(records, columns)
        return

    print("Worker pools")
    print_table(pool_rows(snapshot.pools), POOL_COLUMNS)
    print()
    print("Nodes")
    print_table(node_rows(snapshot.nodes), NODE_COLUMNS)
    print()
    print("OKE add-ons")
    print_table(addon_rows(snapshot.addons), ADDON_COLUMNS)
    print()
    print("Cluster Autoscaler")
    print_table(autoscaler_rows(snapshot), AUTOSCALER_COLUMNS)
    print()
    print("Kueue")
    print_table([kueue_counts(snapshot)], ["topologies", "resource_flavors", "cluster_queues", "local_queues"])
    print_warnings(snapshot.warnings)


def print_warnings(warnings: Iterable[str]) -> None:
    warnings = list(warnings)
    if not warnings:
        return
    print("\nWarnings", file=sys.stderr)
    for warning in warnings:
        print(f"- {warning}", file=sys.stderr)


def print_csv(
    records: list[dict[str, Any]],
    columns: list[str] | None = None,
    show_header: bool = True,
) -> None:
    if not records:
        return
    columns = columns or list(records[0].keys())
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    if show_header:
        writer.writeheader()
    for record in records:
        writer.writerow({key: _cell(record.get(key)) for key in columns})


def print_table(
    records: list[dict[str, Any]],
    columns: list[str] | None = None,
    show_header: bool = True,
) -> None:
    if not records:
        print("(none)")
        return
    columns = columns or list(records[0].keys())
    widths = {
        column: max(len(column), *(len(_cell(record.get(column))) for record in records))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    rule = "  ".join("-" * widths[column] for column in columns)
    if show_header:
        print(header)
        print(rule)
    for record in records:
        print("  ".join(_cell(record.get(column)).ljust(widths[column]) for column in columns))


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    if isinstance(value, dict):
        if not value:
            return "-"
        return ",".join(f"{key}={item}" for key, item in value.items())
    return str(value)


POOL_COLUMNS = [
    "name",
    "kind",
    "placement",
    "shape",
    "desired",
    "oci_active",
    "k8s_ready",
    "gpu",
    "rdma",
    "rdma_vf_required",
    "slinky",
    "autoscaler",
    "kueue_flavor",
]

NODE_COLUMNS = [
    "name",
    "slurm_name",
    "ip",
    "status",
    "pool",
    "shape",
    "gpu",
    "rdma",
    "rdma_vf",
    "workload_pods",
    "slurm_pods",
    "system_pods",
    "daemonsets",
]

AUTOSCALER_COLUMNS = ["deployment", "namespace", "min", "max", "target", "pool"]

ADDON_COLUMNS = ["name", "state", "version", "active", "error"]

TOPOLOGY_COLUMNS = [
    "hpc_island",
    "network_block",
    "local_block",
    "nodes",
    "ready",
    "shapes",
]

PLAN_COLUMNS = [
    "operation",
    "target",
    "pool",
    "owner",
    "current_size",
    "target_size",
    "decrement_size",
    "workload_pods",
    "details",
    "steps",
    "warnings",
    "status",
]

HEALTH_COLUMNS = ["check", "scope", "status", "message", "recommendation"]

STATUS_COLUMNS = [
    "overall",
    "pools",
    "nodes",
    "ready",
    "not_ready",
    "gpu_nodes",
    "rdma_nodes",
    "addons_active",
    "addons_total",
    "autoscaler_pools",
    "slinky_nodes",
    "kueue_flavors",
]


def pool_rows(pools: list[WorkerPoolInfo]) -> list[dict[str, Any]]:
    return [
        {
            "name": pool.name,
            "kind": pool.kind,
            "placement": pool.placement_type,
            "shape": pool.shape,
            "desired": pool.desired_size,
            "oci_active": pool.active_oci_instances,
            "k8s_ready": pool.ready_k8s_nodes,
            "gpu": pool.gpu_resource,
            "rdma": pool.rdma_enabled,
            "rdma_vf_required": pool.rdma_vf_required,
            "slinky": pool.slinky_managed,
            "autoscaler": _autoscaler_label(pool),
            "kueue_flavor": pool.kueue_flavor,
            "node_pool_id": pool.node_pool_id,
            "cluster_network_id": pool.cluster_network_id,
            "instance_pool_id": pool.instance_pool_id,
            "instance_configuration_id": pool.instance_configuration_id,
            "created_by_mgmt_oke": pool.created_by_mgmt_oke,
            "compute_cluster_id": pool.compute_cluster_id,
            "host_group_ids": pool.host_group_ids,
        }
        for pool in sorted(pools, key=lambda item: (item.kind, item.name))
    ]


def node_rows(nodes: list[NodeInfo]) -> list[dict[str, Any]]:
    return [
        {
            "name": node.k8s_name,
            "slurm_name": node.slurm_name,
            "ip": node.internal_ip,
            "status": node.status,
            "ready": node.ready,
            "schedulable": node.schedulable,
            "pool": node.pool_name,
            "shape": node.shape,
            "gpu": node.gpu_allocatable or node.gpu_resource,
            "rdma": node.has_rdma_labels,
            "rdma_vf": node.rdma_vf_allocatable,
            "workload_pods": node.running_workload_pods,
            "slurm_pods": node.slinky_workload_pods,
            "system_pods": node.system_pods,
            "daemonsets": node.daemonset_pods,
            "instance_ocid": node.instance_ocid,
            "node_pool_id": node.node_pool_id,
        }
        for node in sorted(nodes, key=lambda item: item.k8s_name)
    ]


def addon_rows(addons: list[AddonInfo]) -> list[dict[str, Any]]:
    return [
        {
            "name": addon.name,
            "state": addon.lifecycle_state,
            "version": addon.version,
            "active": addon.active,
            "error": addon.error,
        }
        for addon in sorted(addons, key=lambda item: item.name.lower())
    ]


def autoscaler_rows(snapshot: DiscoverySnapshot) -> list[dict[str, Any]]:
    pools_by_target: dict[str, str] = {}
    for pool in snapshot.pools:
        for target in (pool.instance_pool_id, pool.node_pool_id, pool.cluster_network_id):
            if target:
                pools_by_target[target] = pool.name
    return [
        {
            "deployment": entry.deployment,
            "namespace": entry.namespace,
            "min": entry.min_size,
            "max": entry.max_size,
            "target": entry.target_id,
            "pool": pools_by_target.get(entry.target_id),
        }
        for entry in snapshot.autoscaler_entries
    ]


def topology_rows(nodes: list[NodeInfo]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[NodeInfo]] = {}
    for node in nodes:
        if not node.has_rdma_labels:
            continue
        labels = node.rdma_labels
        key = (
            labels.get("oci.oraclecloud.com/rdma.hpc_island_id", "-"),
            labels.get("oci.oraclecloud.com/rdma.network_block_id", "-"),
            labels.get("oci.oraclecloud.com/rdma.local_block_id", "-"),
        )
        groups.setdefault(key, []).append(node)

    rows = []
    for (hpc, network, local), group_nodes in sorted(groups.items()):
        rows.append(
            {
                "hpc_island": hpc,
                "network_block": network,
                "local_block": local,
                "nodes": len(group_nodes),
                "ready": sum(1 for node in group_nodes if node.ready),
                "shapes": sorted({node.shape for node in group_nodes if node.shape}),
                "node_names": sorted(node.k8s_name for node in group_nodes),
            }
        )
    return rows


def _snapshot_csv_records(snapshot: DiscoverySnapshot) -> list[dict[str, Any]]:
    records = [
        {"record_type": "pool", **row}
        for row in pool_rows(snapshot.pools)
    ]
    records.extend(
        {"record_type": "node", **row}
        for row in node_rows(snapshot.nodes)
    )
    records.extend(
        {"record_type": "addon", **row}
        for row in addon_rows(snapshot.addons)
    )
    records.extend(
        {"record_type": "autoscaler", **row}
        for row in autoscaler_rows(snapshot)
    )
    records.append({"record_type": "kueue", **kueue_counts(snapshot)})
    return records


def kueue_counts(snapshot: DiscoverySnapshot) -> dict[str, int]:
    return {
        "topologies": len(snapshot.kueue.topologies),
        "resource_flavors": len(snapshot.kueue.resource_flavors),
        "cluster_queues": len(snapshot.kueue.cluster_queues),
        "local_queues": len(snapshot.kueue.local_queues),
    }


def operation_plan_rows(plans: list[OperationPlan]) -> list[dict[str, Any]]:
    return [
        {
            "operation": plan.operation,
            "target": plan.target,
            "pool": plan.pool,
            "owner": plan.owner,
            "current_size": plan.current_size,
            "target_size": plan.target_size,
            "decrement_size": plan.decrement_size,
            "workload_pods": plan.workload_pods,
            "details": plan.details,
            "steps": plan.steps,
            "warnings": plan.warnings,
            "status": "planned",
        }
        for plan in plans
    ]


def health_rows(results: list[HealthResult]) -> list[dict[str, Any]]:
    return [
        {
            "check": result.check,
            "scope": result.scope,
            "status": result.status,
            "message": result.message,
            "recommendation": result.recommendation,
        }
        for result in results
    ]


def status_rows(snapshot: DiscoverySnapshot, health: list[HealthResult]) -> list[dict[str, Any]]:
    failed = any(result.status == "FAIL" for result in health)
    warning = any(result.status == "WARN" for result in health)
    return [
        {
            "overall": "FAILED" if failed else "DEGRADED" if warning else "HEALTHY",
            "pools": len(snapshot.pools),
            "nodes": len(snapshot.nodes),
            "ready": sum(1 for node in snapshot.nodes if node.ready),
            "not_ready": sum(1 for node in snapshot.nodes if not node.ready),
            "gpu_nodes": sum(1 for node in snapshot.nodes if node.gpu_resource),
            "rdma_nodes": sum(1 for node in snapshot.nodes if node.rdma_topology_ready),
            "addons_active": sum(1 for addon in snapshot.addons if addon.active),
            "addons_total": len(snapshot.addons),
            "autoscaler_pools": sum(1 for pool in snapshot.pools if pool.autoscaler_owned),
            "slinky_nodes": sum(1 for node in snapshot.nodes if node.slinky_managed),
            "kueue_flavors": len(snapshot.kueue.resource_flavors),
        }
    ]


def parse_columns(specification: str | None, available: list[str]) -> list[str]:
    if not specification:
        return list(available)
    requested = [field.strip() for field in specification.split(",") if field.strip()]
    invalid = [field for field in requested if field not in available]
    if invalid:
        raise ValueError(
            f"Unknown column(s): {', '.join(invalid)}. Valid columns: {', '.join(available)}"
        )
    if not requested:
        raise ValueError("At least one output column is required.")
    return requested


def sort_records(records: list[dict[str, Any]], specification: str | None) -> list[dict[str, Any]]:
    if not specification:
        return records
    fields = [field.strip() for field in specification.split(",") if field.strip()]
    available = set().union(*(record.keys() for record in records)) if records else set()
    invalid = [field for field in fields if field not in available]
    if invalid:
        raise ValueError(
            f"Unknown sort field(s): {', '.join(invalid)}. Valid fields: {', '.join(sorted(available))}"
        )

    def field_key(value: Any) -> tuple[int, int, float, str]:
        if value is None:
            return (1, 2, 0.0, "")
        if isinstance(value, bool):
            return (0, 0, float(value), "")
        if isinstance(value, (int, float)):
            return (0, 0, float(value), "")
        return (0, 1, 0.0, _cell(value).lower())

    def key(record: dict[str, Any]) -> tuple[tuple[int, int, float, str], ...]:
        return tuple(field_key(record.get(field)) for field in fields)

    return sorted(records, key=key)


def _autoscaler_label(pool: WorkerPoolInfo) -> str:
    if pool.autoscaler_owned is None:
        return "-"
    if not pool.autoscaler_owned:
        return "no"
    if pool.autoscaler_min is None or pool.autoscaler_max is None:
        return "yes"
    return f"{pool.autoscaler_min}:{pool.autoscaler_max}"
