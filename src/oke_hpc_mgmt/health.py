from __future__ import annotations

from oke_hpc_mgmt.models import DiscoverySnapshot, HealthResult, NodeInfo, WorkerPoolInfo


HEALTH_TYPES = (
    "all",
    "discovery",
    "node",
    "pool",
    "gpu",
    "rdma",
    "addons",
    "scheduler",
)


def evaluate_health(
    snapshot: DiscoverySnapshot,
    check_type: str = "all",
    pool_name: str | None = None,
) -> list[HealthResult]:
    if check_type not in HEALTH_TYPES:
        raise ValueError(
            f"Unknown health type '{check_type}'. Valid types: {', '.join(HEALTH_TYPES)}"
        )

    pools = [pool for pool in snapshot.pools if not pool_name or pool.name == pool_name]
    nodes = [node for node in snapshot.nodes if not pool_name or node.pool_name == pool_name]
    if pool_name and not pools:
        raise ValueError(f"Pool not found: {pool_name}")

    results: list[HealthResult] = []
    if check_type in {"all", "discovery"}:
        results.extend(_discovery_health(snapshot))
    if check_type in {"all", "pool"}:
        results.extend(_pool_health(pools))
    if check_type in {"all", "node"}:
        results.extend(_node_health(nodes))
    if check_type in {"all", "gpu"}:
        results.extend(_gpu_health(pools, nodes))
    if check_type in {"all", "rdma"}:
        results.extend(_rdma_health(pools, nodes, snapshot.network_operator_active))
    if check_type in {"all", "addons"}:
        results.extend(_addon_health(snapshot, pools))
    if check_type in {"all", "scheduler"}:
        results.extend(_scheduler_health(snapshot, pools, nodes))
    return sorted(results, key=lambda item: (_status_order(item.status), item.check, item.scope))


def _discovery_health(snapshot: DiscoverySnapshot) -> list[HealthResult]:
    messages = list(snapshot.warnings)
    if not snapshot.oci_discovery_enabled:
        messages.append(
            "OCI discovery is disabled; health and capacity results use partial inventory."
        )
    if not snapshot.kubernetes_discovery_enabled:
        messages.append(
            "Kubernetes discovery is disabled; health and capacity results use partial inventory."
        )
    if not messages:
        return [
            HealthResult(
                check="discovery-completeness",
                scope="cluster",
                status="PASS",
                message="Discovery completed without warnings.",
            )
        ]
    return [
        HealthResult(
            check="discovery-completeness",
            scope="cluster",
            status="WARN",
            message=warning,
            recommendation=(
                "Resolve the discovery warning before relying on health or capacity results."
            ),
        )
        for warning in messages
    ]


def actionable_recommendations(results: list[HealthResult]) -> list[HealthResult]:
    return [
        result
        for result in results
        if result.status in {"FAIL", "WARN"} and result.recommendation
    ]


def addon_validation_results(
    snapshot: DiscoverySnapshot,
    target: str,
    pool_name: str | None = None,
) -> list[HealthResult]:
    if target not in {"all", "gpu", "rdma"}:
        raise ValueError("Add-on validation target must be one of: all, gpu, rdma")
    all_results = evaluate_health(snapshot, check_type="all", pool_name=pool_name)
    prefixes = {
        "gpu": ("addon-node-feature-discovery", "addon-gpu-operator", "gpu-"),
        "rdma": ("addon-node-feature-discovery", "addon-network-operator", "rdma-"),
        "all": ("addon-", "gpu-", "rdma-"),
    }[target]
    return [result for result in all_results if result.check.startswith(prefixes)]


def _pool_health(pools: list[WorkerPoolInfo]) -> list[HealthResult]:
    results: list[HealthResult] = []
    for pool in pools:
        counts = [pool.desired_size, pool.active_oci_instances, pool.ready_k8s_nodes]
        comparable = [count for count in counts if count is not None]
        converged = bool(comparable) and len(set(comparable)) == 1
        if converged:
            results.append(
                HealthResult(
                    check="pool-convergence",
                    scope=pool.name,
                    status="PASS",
                    message=(
                        f"desired={pool.desired_size}, oci_active={pool.active_oci_instances}, "
                        f"k8s_ready={pool.ready_k8s_nodes}"
                    ),
                )
            )
        else:
            results.append(
                HealthResult(
                    check="pool-convergence",
                    scope=pool.name,
                    status="FAIL",
                    message=(
                        f"desired={pool.desired_size}, oci_active={pool.active_oci_instances}, "
                        f"k8s_ready={pool.ready_k8s_nodes}"
                    ),
                    recommendation=(
                        f"Inspect OCI work requests and Kubernetes node readiness for pool {pool.name}."
                    ),
                )
            )
    return results


def _node_health(nodes: list[NodeInfo]) -> list[HealthResult]:
    results: list[HealthResult] = []
    for node in nodes:
        if not node.ready:
            results.append(
                HealthResult(
                    check="node-readiness",
                    scope=node.k8s_name,
                    status="FAIL",
                    message="Kubernetes Ready condition is false.",
                    recommendation=(
                        f"Inspect conditions, kubelet, and console history for {node.k8s_name}; "
                        "replace the node if it cannot recover."
                    ),
                )
            )
        else:
            results.append(
                HealthResult(
                    check="node-readiness",
                    scope=node.k8s_name,
                    status="PASS",
                    message="Kubernetes Ready condition is true.",
                )
            )
        if node.ready and not node.schedulable:
            results.append(
                HealthResult(
                    check="node-schedulability",
                    scope=node.k8s_name,
                    status="WARN",
                    message="Node is cordoned.",
                    recommendation=(
                        f"Confirm maintenance is complete, then run nodes uncordon {node.k8s_name}."
                    ),
                )
            )
    return results


def _gpu_health(
    pools: list[WorkerPoolInfo],
    nodes: list[NodeInfo],
) -> list[HealthResult]:
    results: list[HealthResult] = []
    gpu_pools = {pool.name: pool for pool in pools if pool.gpu_resource}
    for node in nodes:
        pool = gpu_pools.get(node.pool_name or "")
        if pool is None:
            continue
        value = node.allocatable.get(pool.gpu_resource or "")
        if _positive_resource(value):
            results.append(
                HealthResult(
                    check="gpu-allocatable",
                    scope=node.k8s_name,
                    status="PASS",
                    message=f"{pool.gpu_resource}={value}",
                )
            )
        else:
            results.append(
                HealthResult(
                    check="gpu-allocatable",
                    scope=node.k8s_name,
                    status="FAIL",
                    message=f"{pool.gpu_resource} is not allocatable.",
                    recommendation=(
                        "Validate Node Feature Discovery and the GPU Operator, then inspect "
                        f"device-plugin pods on {node.k8s_name}."
                    ),
                )
            )
    return results


def _rdma_health(
    pools: list[WorkerPoolInfo],
    nodes: list[NodeInfo],
    network_operator_active: bool,
) -> list[HealthResult]:
    results: list[HealthResult] = []
    rdma_pools = {pool.name: pool for pool in pools if pool.rdma_enabled}
    for node in nodes:
        pool = rdma_pools.get(node.pool_name or "")
        if pool is None:
            continue
        if node.rdma_topology_ready:
            results.append(
                HealthResult(
                    check="rdma-topology",
                    scope=node.k8s_name,
                    status="PASS",
                    message="Required OCI RDMA topology labels are valid.",
                )
            )
        else:
            results.append(
                HealthResult(
                    check="rdma-topology",
                    scope=node.k8s_name,
                    status="FAIL",
                    message="Required OCI RDMA topology labels are missing or invalid.",
                    recommendation=(
                        "Validate the RDMA worker image, Node Feature Discovery, instance metadata, "
                        f"and pool placement for {node.k8s_name}."
                    ),
                )
            )
        if pool.rdma_vf_required or network_operator_active:
            value = node.rdma_vf_allocatable
            if _positive_resource(value):
                results.append(
                    HealthResult(
                        check="rdma-vf",
                        scope=node.k8s_name,
                        status="PASS",
                        message=f"nvidia.com/rdma-vf={value}",
                    )
                )
            else:
                results.append(
                    HealthResult(
                        check="rdma-vf",
                        scope=node.k8s_name,
                        status="FAIL",
                        message="NVIDIA Network Operator is active but RDMA VFs are not allocatable.",
                        recommendation=(
                            "Inspect Network Operator, NicClusterPolicy, SR-IOV, and device-plugin "
                            f"pods on {node.k8s_name}."
                        ),
                    )
                )
    return results


def _addon_health(
    snapshot: DiscoverySnapshot,
    pools: list[WorkerPoolInfo],
) -> list[HealthResult]:
    results: list[HealthResult] = []
    for addon in snapshot.addons:
        results.append(
            HealthResult(
                check="addon-lifecycle",
                scope=addon.name,
                status="PASS" if addon.active else "FAIL",
                message=(
                    f"state={addon.lifecycle_state or '-'}, version={addon.version or '-'}"
                    + (f", error={addon.error}" if addon.error else "")
                ),
                recommendation=(
                    None
                    if addon.active
                    else f"Inspect the OKE add-on work request and configuration for {addon.name}."
                ),
            )
        )

    has_gpu = any(pool.gpu_resource for pool in pools)
    has_nvidia_gpu = any(pool.gpu_resource == "nvidia.com/gpu" for pool in pools)
    has_rdma = any(pool.rdma_enabled for pool in pools)
    if has_gpu:
        results.append(
            _expected_addon(
                snapshot,
                "NodeFeatureDiscovery",
                "addon-node-feature-discovery",
                required=True,
            )
        )
    if has_nvidia_gpu:
        results.append(
            _expected_addon(
                snapshot,
                "NvidiaGpuOperator",
                "addon-gpu-operator",
                required=True,
            )
        )
    if has_rdma:
        results.append(
            _expected_addon(
                snapshot,
                "NvidiaNetworkOperator",
                "addon-network-operator",
                required=False,
            )
        )
    return results


def _expected_addon(
    snapshot: DiscoverySnapshot,
    name: str,
    check: str,
    required: bool,
) -> HealthResult:
    addon = snapshot.addon_by_name(name)
    if addon and addon.active:
        return HealthResult(
            check=check,
            scope=name,
            status="PASS",
            message=f"OKE add-on is active at version {addon.version or '-'}.",
        )
    if addon:
        return HealthResult(
            check=check,
            scope=name,
            status="FAIL",
            message=f"OKE add-on state is {addon.lifecycle_state or 'unknown'}.",
            recommendation=f"Repair or update the {name} OKE add-on.",
        )
    if required:
        return HealthResult(
            check=check,
            scope=name,
            status="WARN",
            message="Expected OKE add-on was not discovered.",
            recommendation=f"Confirm that the {name} OKE add-on is enabled for this cluster.",
        )
    return HealthResult(
        check=check,
        scope=name,
        status="INFO",
        message="Optional OKE add-on is not enabled; host-network RDMA remains supported.",
    )


def _scheduler_health(
    snapshot: DiscoverySnapshot,
    pools: list[WorkerPoolInfo],
    nodes: list[NodeInfo],
) -> list[HealthResult]:
    results: list[HealthResult] = []
    for pool in pools:
        if pool.autoscaler_owned:
            results.append(
                HealthResult(
                    check="autoscaler-ownership",
                    scope=pool.name,
                    status="INFO",
                    message=f"Cluster Autoscaler owns bounds {pool.autoscaler_min}:{pool.autoscaler_max}.",
                )
            )
    slinky_nodes = [node for node in nodes if node.slinky_managed]
    if slinky_nodes:
        results.append(
            HealthResult(
                check="slinky-protection",
                scope="cluster",
                status="INFO",
                message=f"{len(slinky_nodes)} Slinky worker node(s) are mutation-protected.",
            )
        )
    if snapshot.kueue.resource_flavors:
        results.append(
            HealthResult(
                check="kueue-inventory",
                scope="cluster",
                status="PASS",
                message=f"{len(snapshot.kueue.resource_flavors)} ResourceFlavor object(s) discovered.",
            )
        )
    return results


def _positive_resource(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False


def _status_order(status: str) -> int:
    return {"FAIL": 0, "WARN": 1, "INFO": 2, "PASS": 3}.get(status, 4)
