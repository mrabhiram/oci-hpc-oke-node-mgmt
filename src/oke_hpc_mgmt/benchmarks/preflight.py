from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from kubernetes.utils.quantity import parse_quantity

from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError
from oke_hpc_mgmt.benchmarks.profiles import get_profile, validate_exclusive_gpu_labels
from oke_hpc_mgmt.models import RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS


GPU_RESOURCE = "nvidia.com/gpu"
VF_RESOURCE = "nvidia.com/rdma-vf"


@dataclass(frozen=True)
class ResourceOccupant:
    namespace: str
    name: str
    uid: str
    node: str
    gpus: int
    vfs: int
    terminating: bool


@dataclass(frozen=True)
class PreflightEvidence:
    nodes: tuple[tuple[str, str], ...]
    resource_versions: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]


def device_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BenchmarkError("Device resource quantities must be nonnegative integers.")
    try:
        count = parse_quantity(value)
        if not count.is_finite() or count < 0 or count != count.to_integral_value():
            raise ValueError("Not a finite device count.")
        return int(count)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise BenchmarkError("Device resource quantities must be nonnegative integers.") from exc


def _container_devices(container: dict[str, Any], resource: str) -> int:
    resources = container.get("resources") or {}
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    return max(device_count(requests.get(resource, 0)), device_count(limits.get(resource, 0)))


def pod_devices(pod: dict[str, Any], resource: str) -> int:
    spec = pod.get("spec") or {}
    app = sum(_container_devices(container, resource) for container in spec.get("containers") or [])
    sidecars = 0
    init_peak = 0
    # Restartable init containers overlap with later init containers and the
    # application; the scheduler accounts for both peaks, not a simple max(init).
    for container in spec.get("initContainers") or []:
        amount = _container_devices(container, resource)
        if container.get("restartPolicy") == "Always":
            sidecars += amount
            init_peak = max(init_peak, sidecars)
        else:
            init_peak = max(init_peak, sidecars + amount)
    return max(app + sidecars, init_peak) + device_count((spec.get("overhead") or {}).get(resource, 0))


def resource_occupants(pods: list[dict[str, Any]]) -> tuple[ResourceOccupant, ...]:
    occupants: list[ResourceOccupant] = []
    for pod in pods:
        metadata = pod.get("metadata") or {}
        node = (pod.get("spec") or {}).get("nodeName")
        if not node or (pod.get("status") or {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        gpus, vfs = pod_devices(pod, GPU_RESOURCE), pod_devices(pod, VF_RESOURCE)
        if gpus or vfs:
            occupants.append(ResourceOccupant(
                str(metadata.get("namespace", "")), str(metadata.get("name", "")),
                str(metadata.get("uid", "")), str(node), gpus, vfs,
                bool(metadata.get("deletionTimestamp")),
            ))
    return tuple(occupants)


def validate_runtime_nodes(
    case: BenchmarkCase,
    raw_nodes: list[dict[str, Any]],
    pods: list[dict[str, Any]],
) -> PreflightEvidence:
    actual = {node["metadata"]["name"]: node for node in raw_nodes}
    versions: list[tuple[str, str]] = []
    hostnames: set[str] = set()
    for target in case.nodes:
        node = actual.get(target.name)
        if node is None:
            raise BenchmarkError(f"Selected node {target.name} no longer exists.")
        metadata, spec, status = node["metadata"], node.get("spec") or {}, node.get("status") or {}
        if metadata.get("uid") != target.uid:
            raise BenchmarkError(f"Node {target.name} was replaced after planning; replan using its new UID.")
        if target.boot_id and (status.get("nodeInfo") or {}).get("bootID") != target.boot_id:
            raise BenchmarkError(f"Node {target.name} boot identity changed or is unavailable; replan the test.")
        if not metadata.get("resourceVersion"):
            raise BenchmarkError(f"Missing resource version for {target.name}.")
        if metadata.get("deletionTimestamp") or spec.get("unschedulable"):
            raise BenchmarkError(f"Node {target.name} is terminating or unschedulable.")
        conditions = {item.get("type"): item.get("status") for item in status.get("conditions") or []}
        if conditions.get("Ready") != "True":
            raise BenchmarkError(f"Node {target.name} is not Ready.")
        for pressure in ("DiskPressure", "MemoryPressure", "PIDPressure"):
            if conditions.get(pressure) != "False":
                raise BenchmarkError(f"Node {target.name} {pressure} is not verified False.")
        if conditions.get("NetworkUnavailable", "False") != "False":
            raise BenchmarkError(f"Node {target.name} network readiness is not verified.")
        labels = metadata.get("labels") or {}
        hostname = labels.get("kubernetes.io/hostname")
        if not hostname or hostname in hostnames:
            raise BenchmarkError("Selected nodes need unique hostname labels for required anti-affinity.")
        hostnames.add(hostname)
        if labels.get("node.kubernetes.io/instance-type") != target.shape:
            raise BenchmarkError(f"Shape changed or is unverified on {target.name}.")
        validate_exclusive_gpu_labels(labels, target.gpu_count)
        if case.network != "local" and tuple(labels.get(key) for key in RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS) != target.topology:
            raise BenchmarkError(f"RDMA topology changed on {target.name}; replan the test.")
        observed_taints = set()
        for taint in spec.get("taints") or []:
            key, value, effect = taint.get("key"), taint.get("value"), taint.get("effect")
            if key != GPU_RESOURCE or effect not in {"NoSchedule", "PreferNoSchedule"}:
                raise BenchmarkError(f"Unexpected scheduling taint on {target.name}: {key}.")
            observed_taints.add(f"{key}={value}:{effect}" if value else f"{key}:{effect}")
        if observed_taints != set(target.tolerations):
            raise BenchmarkError(f"Node {target.name} taints changed after planning.")
        allocatable = status.get("allocatable") or {}
        if device_count(allocatable.get(GPU_RESOURCE, 0)) != target.gpu_count:
            raise BenchmarkError(f"Physical GPU allocatable changed on {target.name}.")
        if case.network == "vf" and device_count(allocatable.get(VF_RESOURCE, 0)) < get_profile(target.shape).vf_count:
            raise BenchmarkError(f"Insufficient RDMA VFs on {target.name}.")
        versions.append((target.name, str(metadata["resourceVersion"])))
    selected = {node.name for node in case.nodes}
    occupants = [item for item in resource_occupants(pods) if item.node in selected]
    if occupants:
        names = ", ".join(f"{item.namespace}/{item.name} on {item.node}" for item in occupants)
        raise BenchmarkError(f"Selected GPU/RDMA resources are already allocated: {names}.")
    if case.network == "host":
        for pod in pods:
            spec, status = pod.get("spec") or {}, pod.get("status") or {}
            if spec.get("nodeName") not in selected or status.get("phase") in {"Succeeded", "Failed"}:
                continue
            containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
            if any(port.get("hostPort") == 2222 or (spec.get("hostNetwork") and port.get("containerPort") == 2222)
                   for container in containers for port in container.get("ports") or []):
                raise BenchmarkError("A selected worker already has a pod using host SSH port 2222.")
    return PreflightEvidence(
        tuple((node.name, node.uid) for node in case.nodes), tuple(versions),
        ("API checks cannot prove GPU/driver/runtime health; worker startup and benchmark evidence are required.",),
    )
