from __future__ import annotations

import argparse
import os
import sys
import time
import warnings

_OCI_STRICT_WARNING_FILTERS = (
    "ignore::FutureWarning:urllib3.poolmanager",
    "ignore:The 'strict' parameter is no longer needed:FutureWarning",
)


def _suppress_oci_cli_strict_warning() -> None:
    existing = [item for item in os.environ.get("PYTHONWARNINGS", "").split(",") if item]
    for warning_filter in _OCI_STRICT_WARNING_FILTERS:
        if warning_filter not in existing:
            existing.append(warning_filter)
    os.environ["PYTHONWARNINGS"] = ",".join(existing)


_suppress_oci_cli_strict_warning()
warnings.filterwarnings(
    "ignore",
    message=".*'strict' parameter is no longer needed.*",
    category=FutureWarning,
)
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"urllib3\.poolmanager",
)

from oke_hpc_mgmt import __version__
from oke_hpc_mgmt.discovery import DiscoveryOptions, DiscoveryService
from oke_hpc_mgmt.render import (
    AUTOSCALER_COLUMNS,
    NODE_COLUMNS,
    POOL_COLUMNS,
    TOPOLOGY_COLUMNS,
    autoscaler_rows,
    node_rows,
    pool_rows,
    print_records,
    print_snapshot,
    print_warnings,
    topology_rows,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_program_name(),
        description="Management CLI for OCI HPC OKE worker pools and nodes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--compartment-id", default=os.getenv("OCI_COMPARTMENT_ID"))
    parser.add_argument("--cluster-id", default=os.getenv("OKE_CLUSTER_ID"))
    parser.add_argument("--region", default=os.getenv("OCI_REGION"))
    parser.add_argument(
        "--auth",
        choices=("config_file", "instance_principal", "resource_principal", "none"),
        default=os.getenv("OCI_AUTH", "config_file"),
        help="OCI authentication method. Use 'none' for Kubernetes-only discovery.",
    )
    parser.add_argument("--oci-config-file", default=os.getenv("OCI_CONFIG_FILE"))
    parser.add_argument("--oci-profile", default=os.getenv("OCI_PROFILE"))
    parser.add_argument("--kubeconfig", default=os.getenv("KUBECONFIG"))
    parser.add_argument("--context", default=os.getenv("KUBE_CONTEXT"))
    parser.add_argument("--in-cluster", action="store_true")
    parser.add_argument("--skip-oci", action="store_true")
    parser.add_argument("--skip-kubernetes", action="store_true")
    parser.add_argument(
        "--format",
        "--output",
        dest="output",
        choices=("table", "json", "csv"),
        default="table",
        help="Output format.",
    )

    subparsers = parser.add_subparsers(dest="command")
    add_pools(subparsers)
    add_nodes(subparsers)
    add_topology(subparsers)
    add_autoscaler(subparsers)
    reconcile = subparsers.add_parser("reconcile", help="Show a full discovery snapshot.")
    add_output_option(reconcile)
    reconcile.set_defaults(handler=cmd_reconcile)
    return parser


def add_pools(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("pools", help="Worker-pool discovery and guarded resize operations.")
    sub = parser.add_subparsers(dest="pools_command", required=True)

    list_cmd = sub.add_parser("list", help="List discovered worker pools.")
    add_output_option(list_cmd)
    list_cmd.set_defaults(handler=cmd_pools_list)

    get_cmd = sub.add_parser("get", help="Get one worker pool by name or OCID.")
    get_cmd.add_argument("pool")
    add_output_option(get_cmd)
    get_cmd.set_defaults(handler=cmd_pools_get)

    resize_cmd = sub.add_parser("resize", help="Resize one discovered worker pool.")
    resize_cmd.add_argument("pool", help="Pool name or backing OCI resource OCID.")
    size = resize_cmd.add_mutually_exclusive_group(required=True)
    size.add_argument("--size", type=int, help="Set the worker pool to this exact desired size.")
    size.add_argument(
        "--delta",
        type=int,
        help="Change the desired size by this amount. Positive values add nodes; negative values remove nodes.",
    )
    resize_cmd.add_argument(
        "--wait",
        action="store_true",
        help="Wait for pool counts and applicable GPU/RDMA resource readiness.",
    )
    resize_cmd.add_argument("--timeout", type=int, default=1800, help="Maximum seconds to wait. Default: 1800.")
    resize_cmd.add_argument("--poll-interval", type=int, default=30, help="Wait polling interval in seconds. Default: 30.")
    resize_cmd.add_argument("--yes", action="store_true", help="Do not prompt for confirmation.")
    add_output_option(resize_cmd)
    resize_cmd.set_defaults(handler=cmd_pools_resize)


def add_nodes(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("nodes", help="Kubernetes node discovery and guarded node removal.")
    sub = parser.add_subparsers(dest="nodes_command", required=True)

    list_cmd = sub.add_parser("list", help="List Kubernetes nodes.")
    list_cmd.add_argument("--pool", help="Filter by worker pool name.")
    list_cmd.add_argument(
        "--rdma-only",
        action="store_true",
        help="Only show nodes with OCI RDMA topology labels.",
    )
    add_output_option(list_cmd)
    list_cmd.set_defaults(handler=cmd_nodes_list)

    get_cmd = sub.add_parser("get", help="Get nodes by name, internal IP, provider ID, or instance OCID.")
    get_cmd.add_argument("identifiers", nargs="+")
    add_output_option(get_cmd)
    get_cmd.set_defaults(handler=cmd_nodes_get)

    remove_cmd = sub.add_parser("remove", help="Remove one specific worker node.")
    remove_cmd.add_argument("node", help="Node name, internal IP, provider ID, or instance OCID.")
    remove_cmd.add_argument(
        "--keep-size",
        action="store_true",
        help="Delete the node but keep the pool size, allowing the pool to replace it.",
    )
    remove_cmd.add_argument(
        "--allow-workloads",
        action="store_true",
        help="Allow removing a node that currently has non-system workload pods.",
    )
    remove_cmd.add_argument(
        "--eviction-grace",
        default="PT10M",
        help="Managed OKE node eviction grace duration. Default: PT10M.",
    )
    remove_cmd.add_argument(
        "--force-after-grace",
        action="store_true",
        help="For managed OKE pools, force compute deletion after the eviction grace duration.",
    )
    remove_cmd.add_argument(
        "--wait",
        action="store_true",
        help="Wait for node removal, pool convergence, and applicable GPU/RDMA resource readiness.",
    )
    remove_cmd.add_argument("--timeout", type=int, default=1800, help="Maximum seconds to wait. Default: 1800.")
    remove_cmd.add_argument("--poll-interval", type=int, default=30, help="Wait polling interval in seconds. Default: 30.")
    remove_cmd.add_argument("--yes", action="store_true", help="Do not prompt for confirmation.")
    add_output_option(remove_cmd)
    remove_cmd.set_defaults(handler=cmd_nodes_remove)


def add_topology(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("topology", help="Read-only RDMA topology view.")
    sub = parser.add_subparsers(dest="topology_command", required=True)

    list_cmd = sub.add_parser("list", help="Group nodes by HPC Island, Network Block, and Local Block.")
    list_cmd.add_argument("--pool", help="Filter by worker pool name.")
    add_output_option(list_cmd)
    list_cmd.set_defaults(handler=cmd_topology_list)


def add_autoscaler(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("autoscaler", help="Read-only Cluster Autoscaler discovery.")
    sub = parser.add_subparsers(dest="autoscaler_command", required=True)

    status_cmd = sub.add_parser("status", help="Show autoscaler --nodes bindings.")
    add_output_option(status_cmd)
    status_cmd.set_defaults(handler=cmd_autoscaler_status)


def options_from_args(args: argparse.Namespace, **overrides) -> DiscoveryOptions:
    values = dict(
        compartment_id=args.compartment_id,
        cluster_id=args.cluster_id,
        region=args.region,
        auth=args.auth,
        oci_config_file=args.oci_config_file,
        oci_profile=args.oci_profile,
        kubeconfig=args.kubeconfig,
        context=args.context,
        in_cluster=args.in_cluster,
        skip_oci=args.skip_oci,
        skip_kubernetes=args.skip_kubernetes,
    )
    values.update(overrides)
    return DiscoveryOptions(**values)


def add_output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        "--output",
        dest="output",
        choices=("table", "json", "csv"),
        default=argparse.SUPPRESS,
        help="Output format.",
    )


def discover(args: argparse.Namespace, **overrides):
    return DiscoveryService(options_from_args(args, **overrides)).discover()


def cmd_pools_list(args: argparse.Namespace) -> int:
    snapshot = discover(args, include_pod_counts=False, include_autoscaler=False, include_kueue=False)
    print_records(pool_rows(snapshot.pools), args.output, POOL_COLUMNS)
    print_warnings(snapshot.warnings)
    return 0


def cmd_pools_get(args: argparse.Namespace) -> int:
    snapshot = discover(args, include_pod_counts=False, include_autoscaler=False, include_kueue=False)
    pool = snapshot.pool_by_name(args.pool)
    if not pool:
        print(f"Pool not found: {args.pool}", file=sys.stderr)
        print_warnings(snapshot.warnings)
        return 1
    print_records(pool_rows([pool]), args.output, None if args.output == "json" else POOL_COLUMNS)
    print_warnings(snapshot.warnings)
    return 0


def cmd_pools_resize(args: argparse.Namespace) -> int:
    if args.auth == "none" or args.skip_oci:
        print("Resize requires OCI discovery. Use --auth instance_principal on the operator host.", file=sys.stderr)
        return 2
    if not args.compartment_id:
        print("Resize requires --compartment-id or OCI_COMPARTMENT_ID.", file=sys.stderr)
        return 2

    snapshot = discover(args, include_pod_counts=False, include_autoscaler=True, include_kueue=False)
    pool = snapshot.pool_by_name(args.pool)
    if not pool:
        print(f"Pool not found: {args.pool}", file=sys.stderr)
        print_warnings(snapshot.warnings)
        return 1
    supported_kinds = {"node-pool", "cluster-network", "instance-pool"}
    if pool.kind not in supported_kinds:
        print(f"Resize for pool kind '{pool.kind}' is not supported: {pool.name}", file=sys.stderr)
        return 2
    if pool.autoscaler_owned:
        print(f"Refusing to resize autoscaler-owned pool: {pool.name}", file=sys.stderr)
        return 2
    if pool.desired_size is None:
        print(f"Cannot determine current desired size for pool: {pool.name}", file=sys.stderr)
        return 2

    original_size = pool.desired_size
    target_size = args.size if args.size is not None else original_size + args.delta
    if target_size < 0:
        print(f"Target size cannot be negative: {target_size}", file=sys.stderr)
        return 2
    if target_size == pool.desired_size:
        status = "unchanged"
        if args.wait:
            pool = _wait_for_pool_size(args, pool.name, target_size)
            status = "ready"
        print_records([_resize_row(pool, pool.desired_size, target_size, None, status)], args.output)
        print_warnings(snapshot.warnings)
        return 0

    if not args.yes and not _confirm_resize(pool.name, original_size, target_size):
        print("Resize cancelled.", file=sys.stderr)
        return 130

    backend = DiscoveryService(options_from_args(args))._oci()
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
        print(f"Pool is missing the OCI backing resource required for resize: {pool.name}", file=sys.stderr)
        return 2
    status = "submitted"
    if args.wait:
        pool = _wait_for_pool_size(args, pool.name, target_size)
        status = "ready"
    print_records([_resize_row(pool, original_size, target_size, work_request_id, status)], args.output)
    print_warnings(snapshot.warnings)
    return 0


def cmd_nodes_list(args: argparse.Namespace) -> int:
    snapshot = discover(args, include_autoscaler=False, include_kueue=False)
    nodes = snapshot.nodes
    if args.pool:
        nodes = [node for node in nodes if node.pool_name == args.pool]
    if args.rdma_only:
        nodes = [node for node in nodes if node.has_rdma_labels]
    print_records(node_rows(nodes), args.output, NODE_COLUMNS)
    print_warnings(snapshot.warnings)
    return 0


def cmd_nodes_get(args: argparse.Namespace) -> int:
    snapshot = discover(args, include_autoscaler=False, include_kueue=False)
    found = []
    missing = []
    for identifier in args.identifiers:
        node = snapshot.node_by_identifier(identifier)
        if node:
            found.append(node)
        else:
            missing.append(identifier)
    print_records(node_rows(found), args.output, None if args.output == "json" else NODE_COLUMNS)
    if missing:
        print(f"Nodes not found: {', '.join(missing)}", file=sys.stderr)
    print_warnings(snapshot.warnings)
    return 1 if missing else 0


def cmd_nodes_remove(args: argparse.Namespace) -> int:
    if args.auth == "none" or args.skip_oci:
        print("Node removal requires OCI discovery. Use --auth instance_principal on the operator host.", file=sys.stderr)
        return 2
    if not args.compartment_id:
        print("Node removal requires --compartment-id or OCI_COMPARTMENT_ID.", file=sys.stderr)
        return 2

    snapshot = discover(args, include_autoscaler=True, include_kueue=False)
    node = snapshot.node_by_identifier(args.node)
    if not node:
        print(f"Node not found: {args.node}", file=sys.stderr)
        print_warnings(snapshot.warnings)
        return 1
    if not node.instance_ocid:
        print(f"Node has no OCI instance OCID: {node.k8s_name}", file=sys.stderr)
        return 2
    pool = snapshot.pool_by_name(node.pool_name or node.node_pool_id or "")
    if not pool and node.node_pool_id:
        pool = snapshot.pool_by_name(node.node_pool_id)
    if not pool:
        print(f"Cannot determine pool for node: {node.k8s_name}", file=sys.stderr)
        return 2
    supported_kinds = {"node-pool", "cluster-network", "instance-pool"}
    if pool.kind not in supported_kinds:
        print(f"Specific node removal for pool kind '{pool.kind}' is not supported: {pool.name}", file=sys.stderr)
        return 2
    if pool.autoscaler_owned:
        print(f"Refusing to remove node from autoscaler-owned pool: {pool.name}", file=sys.stderr)
        return 2
    if pool.kind in {"cluster-network", "instance-pool"}:
        if args.force_after_grace:
            print("--force-after-grace applies only to managed OKE node pools.", file=sys.stderr)
            return 2
        if args.eviction_grace != "PT10M":
            print("--eviction-grace applies only to managed OKE node pools.", file=sys.stderr)
            return 2
    if node.running_workload_pods and not args.allow_workloads:
        print(
            f"Refusing to remove {node.k8s_name}: {node.running_workload_pods} workload pod(s) are running. "
            "Use --allow-workloads to override.",
            file=sys.stderr,
        )
        return 2

    decrement_size = not args.keep_size
    if pool.desired_size is None:
        print(f"Cannot determine current desired size for pool: {pool.name}", file=sys.stderr)
        return 2
    target_size = pool.desired_size - 1 if decrement_size else pool.desired_size
    if target_size < 0:
        print(f"Target size cannot be negative: {target_size}", file=sys.stderr)
        return 2

    if not args.yes and not _confirm_node_remove(node.k8s_name, pool.name, decrement_size):
        print("Node removal cancelled.", file=sys.stderr)
        return 130

    backend = DiscoveryService(options_from_args(args))._oci()
    if pool.kind == "node-pool" and pool.node_pool_id:
        work_request_id = backend.delete_node(
            pool.node_pool_id,
            node.instance_ocid,
            decrement_size=decrement_size,
            override_eviction_grace_duration=args.eviction_grace,
            force_after_grace=args.force_after_grace,
        )
    elif pool.kind in {"cluster-network", "instance-pool"} and pool.instance_pool_id:
        work_request_id = backend.detach_instance_pool_node(
            pool.instance_pool_id,
            node.instance_ocid,
            decrement_size=decrement_size,
        )
    else:
        print(f"Pool is missing the OCI backing resource required for node removal: {pool.name}", file=sys.stderr)
        return 2
    status = "submitted"
    if args.wait:
        pool = _wait_for_node_removed(args, pool.name, node, target_size)
        status = "removed"
    print_records([_node_remove_row(pool, node, target_size, decrement_size, work_request_id, status)], args.output)
    print_warnings(snapshot.warnings)
    return 0


def cmd_topology_list(args: argparse.Namespace) -> int:
    snapshot = discover(args, include_pod_counts=False, include_autoscaler=False, include_kueue=False)
    nodes = snapshot.nodes
    if args.pool:
        nodes = [node for node in nodes if node.pool_name == args.pool]
    print_records(topology_rows(nodes), args.output, TOPOLOGY_COLUMNS)
    print_warnings(snapshot.warnings)
    return 0


def cmd_autoscaler_status(args: argparse.Namespace) -> int:
    snapshot = discover(args, include_pod_counts=False, include_kueue=False)
    print_records(autoscaler_rows(snapshot), args.output, AUTOSCALER_COLUMNS)
    print_warnings(snapshot.warnings)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    snapshot = discover(args)
    print_snapshot(snapshot, args.output)
    return 0


def _confirm_resize(pool_name: str, current_size: int, target_size: int) -> bool:
    print(f"About to resize {pool_name}: {current_size} -> {target_size}", file=sys.stderr)
    print(f"Type the pool name to continue: ", end="", file=sys.stderr, flush=True)
    try:
        return input().strip() == pool_name
    except EOFError:
        return False


def _confirm_node_remove(node_name: str, pool_name: str, decrement_size: bool) -> bool:
    size_text = "and decrement pool size" if decrement_size else "and allow replacement"
    print(f"About to remove node {node_name} from {pool_name} {size_text}.", file=sys.stderr)
    print(f"Type the node name to continue: ", end="", file=sys.stderr, flush=True)
    try:
        return input().strip() == node_name
    except EOFError:
        return False


def _wait_for_pool_size(args: argparse.Namespace, pool_name: str, target_size: int):
    deadline = time.monotonic() + args.timeout
    last_status = ""
    while True:
        snapshot = discover(args, include_pod_counts=False, include_autoscaler=False, include_kueue=False)
        pool = snapshot.pool_by_name(pool_name)
        if pool is None:
            raise RuntimeError(f"Pool disappeared while waiting: {pool_name}")
        status = (
            f"{pool.name}: desired={pool.desired_size} "
            f"oci_active={pool.active_oci_instances} k8s_ready={pool.ready_k8s_nodes}"
        )
        gpu_ready, rdma_ready = _pool_resource_readiness(snapshot, pool)
        if gpu_ready is not None:
            status += f" gpu_ready={gpu_ready}"
        if rdma_ready is not None:
            status += f" rdma_ready={rdma_ready}"
        if status != last_status:
            print(f"Waiting: {status}", file=sys.stderr)
            last_status = status

        desired_ok = pool.desired_size == target_size
        active_ok = pool.active_oci_instances is None or pool.active_oci_instances == target_size
        ready_ok = pool.ready_k8s_nodes == target_size
        gpu_ok = gpu_ready is None or gpu_ready == target_size
        rdma_ok = rdma_ready is None or rdma_ready == target_size
        if desired_ok and active_ok and ready_ok and gpu_ok and rdma_ok:
            return pool
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {pool_name} to reach size {target_size}. Last status: {status}")
        time.sleep(args.poll_interval)


def _wait_for_node_removed(args: argparse.Namespace, pool_name: str, original_node, target_size: int):
    deadline = time.monotonic() + args.timeout
    last_status = ""
    while True:
        snapshot = discover(args, include_pod_counts=False, include_autoscaler=False, include_kueue=False)
        pool = snapshot.pool_by_name(pool_name)
        if pool is None:
            raise RuntimeError(f"Pool disappeared while waiting: {pool_name}")
        node_present = snapshot.node_by_identifier(original_node.k8s_name) is not None
        node_present = node_present or snapshot.node_by_identifier(original_node.instance_ocid or "") is not None
        status = (
            f"{pool.name}: desired={pool.desired_size} "
            f"oci_active={pool.active_oci_instances} k8s_ready={pool.ready_k8s_nodes} "
            f"node_present={node_present}"
        )
        gpu_ready, rdma_ready = _pool_resource_readiness(snapshot, pool)
        if gpu_ready is not None:
            status += f" gpu_ready={gpu_ready}"
        if rdma_ready is not None:
            status += f" rdma_ready={rdma_ready}"
        if status != last_status:
            print(f"Waiting: {status}", file=sys.stderr)
            last_status = status

        desired_ok = pool.desired_size == target_size
        active_ok = pool.active_oci_instances is None or pool.active_oci_instances == target_size
        ready_ok = pool.ready_k8s_nodes == target_size
        gpu_ok = gpu_ready is None or gpu_ready == target_size
        rdma_ok = rdma_ready is None or rdma_ready == target_size
        if not node_present and desired_ok and active_ok and ready_ok and gpu_ok and rdma_ok:
            return pool
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for {original_node.k8s_name} to be removed. Last status: {status}"
            )
        time.sleep(args.poll_interval)


def _pool_resource_readiness(snapshot, pool) -> tuple[int | None, int | None]:
    pool_nodes = [node for node in snapshot.nodes if node.pool_name == pool.name and node.ready]
    gpu_ready = None
    if pool.gpu_resource:
        gpu_ready = sum(
            1
            for node in pool_nodes
            if _positive_resource(node.allocatable.get(pool.gpu_resource))
        )
    rdma_ready = None
    if pool.rdma_enabled:
        rdma_ready = sum(1 for node in pool_nodes if node.has_rdma_labels)
    return gpu_ready, rdma_ready


def _positive_resource(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False


def _resize_row(pool, old_size: int, target_size: int, work_request_id: str | None, status: str):
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


def _node_remove_row(pool, node, target_size: int, decrement_size: bool, work_request_id: str | None, status: str):
    return {
        "node": node.k8s_name,
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


def _program_name() -> str:
    executable = os.path.basename(sys.argv[0])
    if executable == "kubectl-oke":
        return "kubectl oke"
    return "mgmt-oke"
