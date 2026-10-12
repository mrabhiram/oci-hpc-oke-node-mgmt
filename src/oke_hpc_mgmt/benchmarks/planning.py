from __future__ import annotations

from collections import defaultdict
from itertools import combinations
import re
from typing import Literal

from ClusterShell.NodeSet import NodeSet, NodeSetParseError, RESOLVER_NOGROUP

from oke_hpc_mgmt.benchmarks.models import (
    MAX_CASES, MAX_SELECTED_NODES, BenchmarkCase, BenchmarkError,
    HealthcheckPlan, HealthcheckRequest, NodeTarget,
)
from oke_hpc_mgmt.benchmarks.profiles import get_profile, profile_environment, validate_exclusive_gpu_labels
from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo, RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS
from oke_hpc_mgmt.selection import select_nodes


def resolve_identifier(snapshot: DiscoverySnapshot, identifier: str) -> NodeInfo:
    matches = [node for node in snapshot.nodes if identifier in {
        node.k8s_name, node.internal_ip, node.instance_ocid, node.provider_id, node.slurm_name,
    }]
    if len(matches) != 1:
        raise BenchmarkError(f"Node identifier {identifier!r} matched {len(matches)} nodes; expected one.")
    return matches[0]


def expand_nodes(snapshot: DiscoverySnapshot, expressions: tuple[str, ...]) -> tuple[NodeInfo, ...]:
    selected: dict[str, NodeInfo] = {}
    for expression in expressions:
        if not expression.strip() or len(expression) > 8192:
            raise BenchmarkError("Node expressions must be nonempty and at most 8192 characters.")
        exact = [node for node in snapshot.nodes if expression in {
            node.k8s_name, node.internal_ip, node.instance_ocid, node.provider_id, node.slurm_name,
        }]
        if exact:
            node = resolve_identifier(snapshot, expression)
            selected[node.k8s_name] = node
            continue
        if "@" in expression:
            raise BenchmarkError("External NodeSet groups are not supported; select explicit nodes.")
        _bound_range_expansion(expression)
        try:
            expanded = NodeSet(expression, resolver=RESOLVER_NOGROUP)
            size = len(expanded)
        except (NodeSetParseError, ValueError, TypeError, OverflowError) as exc:
            raise BenchmarkError(f"Invalid node expression {expression!r}: {exc}") from exc
        if not size or size > MAX_SELECTED_NODES:
            raise BenchmarkError(f"A node expression must select 1..{MAX_SELECTED_NODES} identifiers.")
        for identifier in expanded:
            node = resolve_identifier(snapshot, identifier)
            selected[node.k8s_name] = node
        if len(selected) > MAX_SELECTED_NODES:
            raise BenchmarkError(f"A request cannot select more than {MAX_SELECTED_NODES} nodes.")
    return tuple(selected[name] for name in sorted(selected))


def _bound_range_expansion(expression: str) -> None:
    # ClusterShell stores each range member in a set during parsing. Bound
    # numeric ranges before invoking it; the library still performs expansion.
    cardinality = 1
    for bracket in re.findall(r"\[([^\[\]]*)\]", expression):
        members = 0
        for component in bracket.split(","):
            match = re.fullmatch(r"(-?\d+)(?:\s*-\s*(-?\d+))?(?:/(\d+))?", component.strip())
            if match is None or any(len(value) > 32 for value in match.groups() if value):
                raise BenchmarkError("Invalid or oversized numeric NodeSet range.")
            start, end, stride = match.groups()
            first, last, step = int(start), int(end or start), int(stride or "1")
            if step < 1 or last < first:
                raise BenchmarkError("NodeSet ranges require ascending bounds and a positive step.")
            members += (last - first) // step + 1
        cardinality *= members
        if cardinality > MAX_SELECTED_NODES:
            raise BenchmarkError(f"NodeSet expansion exceeds the {MAX_SELECTED_NODES}-node safety limit.")


def topology(node: NodeInfo) -> tuple[str, str, str] | None:
    if not node.rdma_topology_ready:
        return None
    island, network, local = RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS
    return node.labels[island], node.labels[network], node.labels[local]


def select_healthcheck_nodes(
    snapshot: DiscoverySnapshot, request: HealthcheckRequest,
) -> tuple[NodeInfo, ...]:
    request.validate()
    if not snapshot.kubernetes_discovery_enabled:
        raise BenchmarkError("Kubernetes discovery is required for node healthchecks.")
    if request.nodes:
        nodes = expand_nodes(snapshot, request.nodes)
    else:
        pool_name = request.pool
        if pool_name:
            matches = [pool for pool in snapshot.pools if pool_name in {pool.name, pool.backing_id}]
            if len(matches) != 1:
                raise BenchmarkError(f"Pool {pool_name!r} matched {len(matches)} pools; expected one.")
            pool_name = matches[0].name
        filtered, _ = select_nodes(snapshot, fields=request.fields, pool=pool_name)
        nodes = tuple(sorted(filtered, key=lambda item: item.k8s_name))
    if request.local_block:
        matching = []
        for node in nodes:
            block = topology(node)
            if block and block[2] == request.local_block:
                matching.append(node)
        if request.nodes and len(matching) != len(nodes):
            raise BenchmarkError("Explicit nodes do not all belong to the requested local block.")
        if len({topology(node) for node in matching}) > 1:
            raise BenchmarkError("Local block ID is ambiguous across parent network/island domains.")
        nodes = tuple(matching)
    if not nodes:
        raise BenchmarkError("No nodes matched the healthcheck selection.")
    if len(nodes) > MAX_SELECTED_NODES or len({node.k8s_name for node in nodes}) != len(nodes):
        raise BenchmarkError("Node inventory is duplicated or exceeds the selection limit.")
    return nodes


def node_target(node: NodeInfo) -> NodeTarget:
    if not node.ready or not node.schedulable:
        raise BenchmarkError(f"{node.k8s_name} is not Ready and schedulable; it cannot be omitted.")
    if not node.kubernetes_uid or not node.pool_name:
        raise BenchmarkError(f"{node.k8s_name} lacks authoritative node UID or pool identity.")
    if node.slinky_managed:
        raise BenchmarkError(f"{node.k8s_name} is Slinky-owned; native MPIJobs cannot take its GPUs.")
    if node.gpu_resource != "nvidia.com/gpu":
        raise BenchmarkError(f"{node.k8s_name} requires a supported NVIDIA NCCL profile.")
    profile = get_profile(node.shape or "unknown")
    try:
        count = int(node.allocatable.get("nvidia.com/gpu", "0"))
    except ValueError as exc:
        raise BenchmarkError(f"Invalid GPU count on {node.k8s_name}.") from exc
    if count != profile.gpu_count:
        raise BenchmarkError(f"{node.k8s_name} requires {profile.gpu_count} exclusive GPUs; found {count}.")
    validate_exclusive_gpu_labels(node.labels, profile.gpu_count)
    for taint in node.taints:
        key = taint.split("=", 1)[0].split(":", 1)[0]
        if taint.endswith((":NoSchedule", ":NoExecute")) and key != "nvidia.com/gpu":
            raise BenchmarkError(f"{node.k8s_name} has a protected or unsupported taint: {taint}.")
    return NodeTarget(
        name=node.k8s_name, uid=node.kubernetes_uid, pool=node.pool_name,
        shape=profile.shape, gpu_count=count, topology=topology(node),
        instance_id=node.instance_ocid, tolerations=tuple(node.taints),
    )


def pair_allowed(first: NodeInfo, second: NodeInfo, request: HealthcheckRequest) -> bool:
    if first.k8s_name == second.k8s_name or not first.pool_name or first.pool_name != second.pool_name:
        return False
    if first.shape != second.shape:
        return False
    if request.pair_scope == "any":
        return True
    left, right = topology(first), topology(second)
    if left is None or right is None:
        return False
    if request.pair_scope == "same-local-block":
        return left == right
    return left[:2] == right[:2] and left[2] != right[2]


def plan_pairs(
    snapshot: DiscoverySnapshot, selected: tuple[NodeInfo, ...], request: HealthcheckRequest,
) -> list[tuple[tuple[NodeInfo, ...], tuple[str, ...]]]:
    excluded = resolve_identifier(snapshot, request.exclude_node) if request.exclude_node else None
    if excluded and any(node.k8s_name == excluded.k8s_name for node in selected):
        raise BenchmarkError("An explicitly selected anchor cannot also be --exclude-node.")
    candidates = list(snapshot.nodes if request.nodes else selected)
    reference = resolve_identifier(snapshot, request.reference_node) if request.reference_node else None
    if reference and reference not in candidates:
        raise BenchmarkError("Reference node is outside the selected pool/field candidate set.")
    viable: list[NodeInfo] = []
    for node in sorted(candidates, key=lambda item: item.k8s_name):
        if excluded and node.k8s_name == excluded.k8s_name:
            continue
        try:
            node_target(node)
        except BenchmarkError:
            continue
        viable.append(node)
    result: dict[tuple[str, ...], tuple[tuple[NodeInfo, ...], set[str]]] = {}

    def record(first: NodeInfo, second: NodeInfo, anchors: tuple[str, ...]) -> None:
        if not pair_allowed(first, second, request):
            raise BenchmarkError(f"Pair {first.k8s_name}/{second.k8s_name} violates placement policy.")
        pair = tuple(sorted((first, second), key=lambda item: item.k8s_name))
        key = tuple(node.k8s_name for node in pair)
        if key not in result:
            result[key] = pair, set()
        result[key][1].update(anchors)
        if len(result) > MAX_CASES:
            raise BenchmarkError(f"Pair selection exceeds the {MAX_CASES}-case limit.")

    if request.pairing == "all":
        count = len(selected)
        if count * (count - 1) // 2 > MAX_CASES:
            raise BenchmarkError(f"All-pairs would exceed {MAX_CASES} cases; narrow the selection.")
        for first, second in combinations(selected, 2):
            if pair_allowed(first, second, request):
                record(first, second, (first.k8s_name, second.k8s_name))
    elif request.pairing == "adjacent":
        for index in range(0, len(selected) - 1, 2):
            first, second = selected[index:index + 2]
            record(first, second, (first.k8s_name, second.k8s_name))
        if len(selected) > 1 and len(selected) % 2:
            record(selected[-1], selected[0], (selected[-1].k8s_name,))
    else:
        selected_names = {node.k8s_name for node in selected}
        for anchor in selected:
            partners = [node for node in viable if pair_allowed(anchor, node, request)]
            if reference:
                if anchor.k8s_name == reference.k8s_name:
                    continue
                partners = [node for node in partners if node.k8s_name == reference.k8s_name]
            partners.sort(key=lambda node: (node.k8s_name not in selected_names, node.k8s_name))
            if not partners:
                raise BenchmarkError(f"No eligible partner for anchor {anchor.k8s_name}.")
            names = (anchor.k8s_name, reference.k8s_name) if reference else (anchor.k8s_name,)
            record(anchor, partners[0], names)
    covered = {node.k8s_name for pair, _ in result.values() for node in pair}
    missing = sorted({node.k8s_name for node in selected} - covered)
    if missing:
        raise BenchmarkError(f"Pair plan leaves nodes untested: {', '.join(missing)}.")
    return [(pair, tuple(sorted(anchors))) for pair, anchors in result.values()]


def resolve_network(
    nodes: tuple[NodeInfo, ...], request: HealthcheckRequest, local: bool,
) -> Literal["local", "host", "vf"]:
    if local:
        if request.check_type == "active" and request.network not in {"auto", "local"}:
            raise BenchmarkError("Single-node active checks use the local network profile.")
        return "local"
    if request.network == "local":
        raise BenchmarkError("A multi-node test cannot use the local network profile.")
    if any(topology(node) is None for node in nodes):
        raise BenchmarkError("Every multi-node RDMA participant requires complete topology labels.")
    if request.network == "auto":
        # Mixed VF availability is ambiguous; never silently fall back to host RDMA.
        has_vfs = [node.rdma_vf_allocatable not in {None, "0"} for node in nodes]
        if any(has_vfs) and not all(has_vfs):
            raise BenchmarkError("Mixed VF availability; select a verified uniform network profile.")
        network: Literal["host", "vf"] = "vf" if all(has_vfs) else "host"
    else:
        network = request.network
    if network == "vf":
        for node in nodes:
            try:
                count = int(node.rdma_vf_allocatable or "0")
            except ValueError as exc:
                raise BenchmarkError(f"Invalid VF allocation on {node.k8s_name}.") from exc
            if count < get_profile(node.shape or "unknown").vf_count:
                raise BenchmarkError(f"Insufficient RDMA VFs on {node.k8s_name}.")
    return network


def plan_healthchecks(snapshot: DiscoverySnapshot, request: HealthcheckRequest) -> HealthcheckPlan:
    selected = select_healthcheck_nodes(snapshot, request)
    names = tuple(node.k8s_name for node in selected)
    passive = names if request.check_type in {"passive", "all"} else ()
    if request.check_type == "passive":
        return HealthcheckPlan(request, names, (), passive, tuple(snapshot.warnings))
    for node in selected:
        node_target(node)
    groups: list[tuple[Literal["active", "multi-node"], tuple[NodeInfo, ...], tuple[str, ...]]] = []
    if request.check_type in {"active", "all"}:
        groups.extend(("active", (node,), (node.k8s_name,)) for node in selected)
    if request.check_type in {"multi-node", "all"}:
        if request.mode in {None, "pairs"}:
            groups.extend(("multi-node", pair, anchors) for pair, anchors in plan_pairs(
                snapshot, selected, request
            ))
        elif request.mode == "all-nodes":
            groups.append(("multi-node", selected, names))
        else:
            blocks: dict[tuple[str, str, str], list[NodeInfo]] = defaultdict(list)
            for node in selected:
                block = topology(node)
                if block is None:
                    raise BenchmarkError(f"Missing local-block topology on {node.k8s_name}.")
                blocks[block].append(node)
            groups.extend(("multi-node", tuple(blocks[key]), tuple(
                node.k8s_name for node in blocks[key]
            )) for key in sorted(blocks))
    if len(groups) * request.parameters.repetitions > MAX_CASES:
        raise BenchmarkError(f"Requested suite exceeds the {MAX_CASES}-case limit.")
    cases: list[BenchmarkCase] = []
    for category, participants, anchors in groups:
        if category == "multi-node" and len(participants) < 2:
            raise BenchmarkError("Inter-node tests require at least two nodes in every selected group.")
        if len({(node.pool_name, node.shape) for node in participants}) != 1:
            raise BenchmarkError("A collective requires one pool and one homogeneous shape.")
        profile = get_profile(participants[0].shape or "unknown")
        if request.parameters.max_bytes > profile.max_bytes:
            raise BenchmarkError(f"{profile.shape} profile supports at most {profile.max_bytes} bytes.")
        network = resolve_network(participants, request, category == "active")
        profile_environment(profile, network, request.environment)
        for repetition in range(1, request.parameters.repetitions + 1):
            cases.append(BenchmarkCase(
                case_id=f"case-{len(cases) + 1:04d}", check_type=category,
                nodes=tuple(node_target(node) for node in participants), anchors=anchors,
                network=network, repetition=repetition,
            ))
    warnings = list(snapshot.warnings)
    extras = sorted({node.name for case in cases for node in case.nodes} - set(names))
    if extras:
        warnings.append(f"Additional same-pool partner nodes: {', '.join(extras)}.")
    warnings.append("NCCL component only; this does not certify the complete Slurm diagnostic suite.")
    return HealthcheckPlan(request, names, tuple(cases), passive, tuple(warnings))
