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
POOL_CREATE_TYPES = ("cpu", "gpu", "rdma")
POOL_STORAGE_MODES = ("inherit", "append", "replace")
CLUSTER_NETWORK_PLACEMENT_CONSTRAINTS = (
    "SINGLE_TIER",
    "SINGLE_BLOCK",
    "PACKED_DISTRIBUTION_MULTI_BLOCK",
)
NODE_POOL_CNI_TYPES = ("OCI_VCN_IP_NATIVE", "FLANNEL_OVERLAY")
NODE_CYCLING_MODES = ("INSTANCE_REPLACE", "BOOT_VOLUME_REPLACE")


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
    instance_configuration_id: str | None = None
    created_by_mgmt_oke: bool = False

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
    oci_discovery_enabled: bool = True
    kubernetes_discovery_enabled: bool = True

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


@dataclass(frozen=True)
class OperationPlan:
    operation: str
    target: str
    pool: str | None = None
    owner: str | None = None
    current_size: int | None = None
    target_size: int | None = None
    decrement_size: bool | None = None
    workload_pods: int = 0
    steps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DrainPod:
    namespace: str
    name: str
    phase: str | None = None
    controller: str | None = None
    daemonset: bool = False
    mirror: bool = False
    has_empty_dir: bool = False
    eviction_blocker: str | None = None

    @property
    def evictable(self) -> bool:
        return not self.daemonset and not self.mirror


@dataclass(frozen=True)
class HealthResult:
    check: str
    scope: str
    status: str
    message: str
    recommendation: str | None = None


@dataclass(frozen=True)
class WorkRequestInfo:
    work_request_id: str
    status: str
    percent_complete: float | None = None
    errors: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.status.upper() in {"FAILED", "CANCELED", "CANCELLED"}


@dataclass(frozen=True)
class ClusterNetworkCreateResult:
    cluster_network_id: str
    instance_configuration_id: str
    instance_pool_id: str | None = None
    work_request_id: str | None = None


@dataclass(frozen=True)
class ManagedNodePoolCreateResult:
    node_pool_id: str | None = None
    work_request_id: str | None = None


@dataclass(frozen=True)
class FssMountSpec:
    export_path: str
    mount_path: str
    mount_target_ip: str

    def as_dict(self) -> dict[str, str]:
        return {
            "export_path": self.export_path,
            "mount_path": self.mount_path,
            "mount_target_ip": self.mount_target_ip,
        }


@dataclass(frozen=True)
class LustreMountSpec:
    management_address: str
    filesystem_name: str
    mount_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "management_address": self.management_address,
            "filesystem_name": self.filesystem_name,
            "mount_path": self.mount_path,
        }


@dataclass(frozen=True)
class NvmeRaidSpec:
    level: int
    device_pattern: str = "/dev/nvme*n1"
    mount_path: str = "/mnt/nvme"

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "device_pattern": self.device_pattern,
            "mount_path": self.mount_path,
        }


@dataclass(frozen=True)
class PoolCreateSpec:
    pool_type: str
    availability_domain: str | None = None
    shape: str | None = None
    image_id: str | None = None
    primary_subnet_id: str | None = None
    pod_subnet_ids: tuple[str, ...] = ()
    node_nsg_ids: tuple[str, ...] = ()
    pod_nsg_ids: tuple[str, ...] = ()
    boot_volume_size_in_gbs: int | None = None
    boot_volume_vpus_per_gb: int | None = None
    boot_volume_kms_key_id: str | None = None
    ocpus: float | None = None
    memory_in_gbs: float | None = None
    kubernetes_version: str | None = None
    max_pods_per_node: int | None = None
    ssh_public_key: str | None = None
    cloud_init: bytes | None = None
    node_labels: tuple[tuple[str, str], ...] = ()
    node_metadata: tuple[tuple[str, str], ...] = ()
    freeform_tags: tuple[tuple[str, str], ...] = ()
    capacity_reservation_id: str | None = None
    fault_domains: tuple[str, ...] = ()
    cni_type: str | None = None
    placement_constraint: str | None = None
    assign_public_ip: bool | None = None
    pv_encryption_in_transit: bool | None = None
    legacy_imds_endpoints_disabled: bool | None = None
    node_cycling_enabled: bool | None = None
    node_cycling_max_surge: str | None = None
    node_cycling_max_unavailable: str | None = None
    node_cycling_mode: str | None = None
    eviction_grace_duration: str | None = None
    force_delete_after_eviction_grace: bool | None = None
    force_action_after_eviction_grace: bool | None = None
    pre_bootstrap_script: bytes | None = None
    post_bootstrap_script: bytes | None = None
    kubelet_extra_args: str | None = None
    storage_mode: str = "inherit"
    nvme_raid: NvmeRaidSpec | None = None
    fss_mounts: tuple[FssMountSpec, ...] = ()
    lustre_mounts: tuple[LustreMountSpec, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "type": self.pool_type,
            "availability_domain": self.availability_domain,
            "shape": self.shape,
            "image_id": self.image_id,
            "primary_subnet_id": self.primary_subnet_id,
            "pod_subnet_ids": list(self.pod_subnet_ids),
            "node_nsg_ids": list(self.node_nsg_ids),
            "pod_nsg_ids": list(self.pod_nsg_ids),
            "boot_volume_size_in_gbs": self.boot_volume_size_in_gbs,
            "boot_volume_vpus_per_gb": self.boot_volume_vpus_per_gb,
            "boot_volume_kms_key_id": self.boot_volume_kms_key_id,
            "ocpus": self.ocpus,
            "memory_in_gbs": self.memory_in_gbs,
            "kubernetes_version": self.kubernetes_version,
            "max_pods_per_node": self.max_pods_per_node,
            "ssh_public_key_configured": bool(self.ssh_public_key),
            "cloud_init_overridden": self.cloud_init is not None,
            "node_labels": dict(self.node_labels),
            "node_metadata_keys": [key for key, _value in self.node_metadata],
            "freeform_tags": dict(self.freeform_tags),
            "capacity_reservation_id": self.capacity_reservation_id,
            "fault_domains": list(self.fault_domains),
            "cni_type": self.cni_type,
            "placement_constraint": self.placement_constraint,
            "assign_public_ip": self.assign_public_ip,
            "pv_encryption_in_transit": self.pv_encryption_in_transit,
            "legacy_imds_endpoints_disabled": self.legacy_imds_endpoints_disabled,
            "node_cycling_enabled": self.node_cycling_enabled,
            "node_cycling_max_surge": self.node_cycling_max_surge,
            "node_cycling_max_unavailable": self.node_cycling_max_unavailable,
            "node_cycling_mode": self.node_cycling_mode,
            "eviction_grace_duration": self.eviction_grace_duration,
            "force_delete_after_eviction_grace": (
                self.force_delete_after_eviction_grace
            ),
            "force_action_after_eviction_grace": (
                self.force_action_after_eviction_grace
            ),
            "pre_bootstrap_script_configured": self.pre_bootstrap_script is not None,
            "post_bootstrap_script_configured": self.post_bootstrap_script is not None,
            "kubelet_extra_args_configured": self.kubelet_extra_args is not None,
            "storage_mode": self.storage_mode,
            "nvme_raid": self.nvme_raid.as_dict() if self.nvme_raid else None,
            "fss_mounts": [mount.as_dict() for mount in self.fss_mounts],
            "lustre_mounts": [mount.as_dict() for mount in self.lustre_mounts],
        }
        return {
            key: value
            for key, value in values.items()
            if value is not None and value not in ([], {})
        }


def _valid_topology_value(value: str | None) -> bool:
    return bool(value and value.strip().lower() not in INVALID_TOPOLOGY_VALUES)


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())
