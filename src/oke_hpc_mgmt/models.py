from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RDMA_LABEL_KEYS = (
    "oci.oraclecloud.com/rdma.hpc_island_id",
    "oci.oraclecloud.com/rdma.network_block_id",
    "oci.oraclecloud.com/rdma.local_block_id",
    "oci.oraclecloud.com/rdma.host_id",
)

RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS = RDMA_LABEL_KEYS[:3]

INVALID_TOPOLOGY_VALUES = frozenset(
    {
        "",
        "-",
        "none",
        "no-imds-data",
        "not-available",
        "null",
        "unknown",
    }
)

RDMA_CAPABILITY_LABEL_KEYS = (
    "feature.node.kubernetes.io/rdma.available",
    "feature.node.kubernetes.io/rdma.capable",
)

GPU_RESOURCES = ("nvidia.com/gpu", "amd.com/gpu")
RDMA_VF_RESOURCE = "nvidia.com/rdma-vf"
SLINKY_HOSTNAME_ANNOTATION = "nodeset.slinky.slurm.net/hostname-override"
SLINKY_HOSTNAME_PREFIX_LABEL = "oci.oraclecloud.com/slinky-hostname-prefix"


@dataclass(frozen=True)
class AddonInfo:
    name: str
    lifecycle_state: str | None = None
    version: str | None = None
    error: str | None = None

    @property
    def active(self) -> bool:
        return (self.lifecycle_state or "").upper() == "ACTIVE" and not self.error


@dataclass
class NodeInfo:
    k8s_name: str
    internal_ip: str | None = None
    provider_id: str | None = None
    instance_ocid: str | None = None
    pool_name: str | None = None
    node_pool_id: str | None = None
    shape: str | None = None
    lifecycle_state: str | None = None
    ready: bool = False
    schedulable: bool = True
    allocatable: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    taints: list[str] = field(default_factory=list)
    running_workload_pods: int = 0
    daemonset_pods: int = 0
    system_pods: int = 0
    slinky_workload_pods: int = 0

    @property
    def rdma_labels(self) -> dict[str, str]:
        return {key: self.labels[key] for key in RDMA_LABEL_KEYS if key in self.labels}

    @property
    def rdma_capable(self) -> bool:
        if self.labels.get("oci.oraclecloud.com/rdma.cluster_type") == "baremetalcluster":
            return True
        if self.shape and self.shape.startswith("BM") and any(
            self.labels.get(key, "").lower() == "true" for key in RDMA_CAPABILITY_LABEL_KEYS
        ):
            return True
        return bool(self.shape and self.shape.startswith("BM.GPU"))

    @property
    def rdma_topology_ready(self) -> bool:
        if not self.rdma_capable:
            return False
        return all(
            _valid_topology_value(self.labels.get(key))
            for key in RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS
        )

    @property
    def has_rdma_labels(self) -> bool:
        """Backward-compatible alias for strict RDMA topology readiness."""
        return self.rdma_topology_ready

    @property
    def gpu_allocatable(self) -> dict[str, str]:
        return {key: self.allocatable[key] for key in GPU_RESOURCES if key in self.allocatable}

    @property
    def gpu_resource(self) -> str | None:
        for key in GPU_RESOURCES:
            if key in self.allocatable or key in self.labels:
                return key
        return None

    @property
    def rdma_vf_allocatable(self) -> str | None:
        return self.allocatable.get(RDMA_VF_RESOURCE)

    @property
    def slurm_name(self) -> str | None:
        return self.annotations.get(SLINKY_HOSTNAME_ANNOTATION)

    @property
    def slinky_managed(self) -> bool:
        return bool(
            self.slurm_name
            or self.labels.get(SLINKY_HOSTNAME_PREFIX_LABEL)
            or self.slinky_workload_pods
        )

    @property
    def status(self) -> str:
        if not self.ready:
            return "NotReady"
        if not self.schedulable:
            return "SchedulingDisabled"
        return "Ready"


@dataclass
class WorkerPoolInfo:
    name: str
    kind: str
    shape: str | None = None
    compartment_id: str | None = None
    availability_domain: str | None = None
    desired_size: int | None = None
    active_oci_instances: int | None = None
    ready_k8s_nodes: int = 0
    node_pool_id: str | None = None
    cluster_network_id: str | None = None
    instance_pool_id: str | None = None
    placement_type: str = "standard"
    compute_cluster_id: str | None = None
    host_group_ids: set[str] = field(default_factory=set)
    oci_instance_ids: set[str] = field(default_factory=set)
    gpu_resource: str | None = None
    rdma_enabled: bool = False
    rdma_vf_required: bool = False
    slinky_managed: bool = False
    autoscaler_owned: bool | None = None
    autoscaler_min: int | None = None
    autoscaler_max: int | None = None
    kueue_flavor: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def backing_id(self) -> str | None:
        return self.cluster_network_id or self.node_pool_id or self.instance_pool_id


@dataclass
class AutoscalerEntry:
    min_size: int
    max_size: int
    target_id: str
    deployment: str
    namespace: str


@dataclass
class KueueSummary:
    topologies: list[dict[str, Any]] = field(default_factory=list)
    resource_flavors: list[dict[str, Any]] = field(default_factory=list)
    cluster_queues: list[dict[str, Any]] = field(default_factory=list)
    local_queues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DiscoverySnapshot:
    pools: list[WorkerPoolInfo] = field(default_factory=list)
    nodes: list[NodeInfo] = field(default_factory=list)
    addons: list[AddonInfo] = field(default_factory=list)
    autoscaler_entries: list[AutoscalerEntry] = field(default_factory=list)
    kueue: KueueSummary = field(default_factory=KueueSummary)
    warnings: list[str] = field(default_factory=list)

    def pool_by_name(self, name: str) -> WorkerPoolInfo | None:
        for pool in self.pools:
            if pool.name == name or pool.backing_id == name:
                return pool
        return None

    def node_by_identifier(self, identifier: str) -> NodeInfo | None:
        for node in self.nodes:
            if identifier in {
                node.k8s_name,
                node.internal_ip,
                node.instance_ocid,
                node.provider_id,
                node.slurm_name,
            }:
                return node
        return None

    def addon_by_name(self, name: str) -> AddonInfo | None:
        normalized = _normalized_name(name)
        for addon in self.addons:
            if _normalized_name(addon.name) == normalized:
                return addon
        return None

    @property
    def network_operator_active(self) -> bool:
        addon = self.addon_by_name("NvidiaNetworkOperator")
        return bool(addon and addon.active)


@dataclass(frozen=True)
class PoolResourceReadiness:
    gpu_ready: int | None = None
    rdma_topology_ready: int | None = None
    rdma_vf_ready: int | None = None


def _valid_topology_value(value: str | None) -> bool:
    return bool(value and value.strip().lower() not in INVALID_TOPOLOGY_VALUES)


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())
