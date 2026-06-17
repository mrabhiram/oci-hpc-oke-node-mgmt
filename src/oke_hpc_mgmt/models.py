from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RDMA_LABEL_KEYS = (
    "oci.oraclecloud.com/rdma.hpc_island_id",
    "oci.oraclecloud.com/rdma.network_block_id",
    "oci.oraclecloud.com/rdma.local_block_id",
    "oci.oraclecloud.com/rdma.host_id",
)

GPU_RESOURCES = ("nvidia.com/gpu", "amd.com/gpu")


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
    taints: list[str] = field(default_factory=list)
    running_workload_pods: int = 0
    daemonset_pods: int = 0
    system_pods: int = 0

    @property
    def rdma_labels(self) -> dict[str, str]:
        return {key: self.labels[key] for key in RDMA_LABEL_KEYS if key in self.labels}

    @property
    def has_rdma_labels(self) -> bool:
        return any(key in self.labels for key in RDMA_LABEL_KEYS)

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
    oci_instance_ids: set[str] = field(default_factory=set)
    gpu_resource: str | None = None
    rdma_enabled: bool = False
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
            }:
                return node
        return None
