from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

from oke_hpc_mgmt.models import AddonInfo, DiscoverySnapshot, NodeInfo, WorkerPoolInfo


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


def print_records(records: list[dict[str, Any]], output: str, columns: list[str] | None = None) -> None:
    if output == "json":
        print(json.dumps(serializable(records), indent=2, sort_keys=True))
        return
    if output == "csv":
        print_csv(records, columns)
        return
    print_table(records, columns)


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


def print_csv(records: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    if not records:
        return
    columns = columns or list(records[0].keys())
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({key: _cell(record.get(key)) for key in columns})


def print_table(records: list[dict[str, Any]], columns: list[str] | None = None) -> None:
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


def _autoscaler_label(pool: WorkerPoolInfo) -> str:
    if pool.autoscaler_owned is None:
        return "-"
    if not pool.autoscaler_owned:
        return "no"
    if pool.autoscaler_min is None or pool.autoscaler_max is None:
        return "yes"
    return f"{pool.autoscaler_min}:{pool.autoscaler_max}"
