from __future__ import annotations

import re
from collections.abc import Iterable

from oke_hpc_mgmt.models import (
    CLUSTER_NETWORK_PLACEMENT_CONSTRAINTS,
    NODE_CYCLING_MODES,
    NODE_POOL_CNI_TYPES,
    POOL_CREATE_TYPES,
    POOL_STORAGE_MODES,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
)

_POOL_NAME_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?"
)
_LABEL_NAME_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?"
)
_DNS_PREFIX_PATTERN = re.compile(
    r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*"
)
_RESERVED_NODE_METADATA_KEYS = frozenset(
    {
        "apiserver_host",
        "cluster_ca_cert",
        "oke-initial-node-labels",
        "oke-k8version",
        "oke-max-pods",
        "oke-native-pod-networking",
        "pod-nsgids",
        "pod-subnets",
        "ssh_authorized_keys",
        "user_data",
    }
)
_RESERVED_FREEFORM_TAG_KEYS = frozenset(
    {
        "mgmt-oke-created",
        "pool",
        "role",
        "state_id",
    }
)
_ISO_HOUR_MINUTE_DURATION_PATTERN = re.compile(
    r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
)


def normalize_pool_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("Pool name cannot be empty.")
    if not _POOL_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Pool name must be 1-63 characters, contain only letters, numbers, "
            "'.', '_' or '-', and start and end with a letter or number."
        )
    return name


def parse_key_value_options(
    values: Iterable[str],
    *,
    option_name: str,
) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_value in values:
        key, separator, value = raw_value.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError(
                f"{option_name} values must use KEY=VALUE syntax: {raw_value!r}"
            )
        if key in seen:
            raise ValueError(f"{option_name} repeats key: {key}")
        seen.add(key)
        parsed.append((key, value))
    return tuple(parsed)


def validate_pool_create_spec(spec: PoolCreateSpec) -> PoolCreateSpec:
    if spec.pool_type not in POOL_CREATE_TYPES:
        raise ValueError(
            f"Pool type must be one of: {', '.join(POOL_CREATE_TYPES)}."
        )
    if spec.storage_mode not in POOL_STORAGE_MODES:
        raise ValueError(
            f"Storage mode must be one of: {', '.join(POOL_STORAGE_MODES)}."
        )
    if spec.cni_type and spec.cni_type not in NODE_POOL_CNI_TYPES:
        raise ValueError(
            f"CNI type must be one of: {', '.join(NODE_POOL_CNI_TYPES)}."
        )
    if (
        spec.placement_constraint
        and spec.placement_constraint not in CLUSTER_NETWORK_PLACEMENT_CONSTRAINTS
    ):
        raise ValueError(
            "Cluster Network placement constraint must be one of: "
            + ", ".join(CLUSTER_NETWORK_PLACEMENT_CONSTRAINTS)
            + "."
        )
    if spec.node_cycling_mode and spec.node_cycling_mode not in NODE_CYCLING_MODES:
        raise ValueError(
            f"Node cycling mode must be one of: {', '.join(NODE_CYCLING_MODES)}."
        )

    _require_positive(spec.boot_volume_size_in_gbs, "Boot volume size")
    _require_positive(spec.boot_volume_vpus_per_gb, "Boot volume VPUs per GB")
    _require_positive_float(spec.ocpus, "OCPUs")
    _require_positive_float(spec.memory_in_gbs, "Memory")
    if spec.max_pods_per_node is not None and not 1 <= spec.max_pods_per_node <= 110:
        raise ValueError("Maximum pods per node must be between 1 and 110.")

    for key, value in spec.node_labels:
        validate_kubernetes_label(key, value)
    metadata_keys = {key for key, _value in spec.node_metadata}
    reserved = sorted(metadata_keys.intersection(_RESERVED_NODE_METADATA_KEYS))
    if reserved:
        raise ValueError(
            "Use dedicated pool-create options instead of overriding reserved OKE "
            f"node metadata: {', '.join(reserved)}."
        )
    freeform_tag_keys = {key for key, _value in spec.freeform_tags}
    reserved_tags = sorted(
        freeform_tag_keys.intersection(_RESERVED_FREEFORM_TAG_KEYS)
    )
    if reserved_tags:
        raise ValueError(
            "Worker ownership tags are managed by mgmt-oke and cannot be "
            f"overridden: {', '.join(reserved_tags)}."
        )

    if spec.pool_type == "rdma":
        managed_only = {
            "--node-cycling-enabled": spec.node_cycling_enabled,
            "--node-cycling-max-surge": spec.node_cycling_max_surge,
            "--node-cycling-max-unavailable": spec.node_cycling_max_unavailable,
            "--node-cycling-mode": spec.node_cycling_mode,
            "--eviction-grace-duration": spec.eviction_grace_duration,
            "--force-delete-after-eviction-grace": (
                spec.force_delete_after_eviction_grace
            ),
            "--force-action-after-eviction-grace": (
                spec.force_action_after_eviction_grace
            ),
        }
        selected = [name for name, value in managed_only.items() if value is not None]
        if selected:
            raise ValueError(
                "Self-managed RDMA pools do not support managed OKE settings: "
                + ", ".join(selected)
                + "."
            )
        if len(spec.pod_subnet_ids) > 1:
            raise ValueError(
                "Self-managed RDMA bootstrap accepts one pod subnet. Specify "
                "--pod-subnet-id once."
            )
    else:
        rdma_only = {
            "--boot-volume-vpus-per-gb": spec.boot_volume_vpus_per_gb,
            "--placement-constraint": spec.placement_constraint,
            "--assign-public-ip": spec.assign_public_ip,
        }
        selected = [name for name, value in rdma_only.items() if value is not None]
        if selected:
            raise ValueError(
                "Managed CPU/GPU pools do not support Cluster Network settings: "
                + ", ".join(selected)
                + "."
            )

    has_storage = bool(spec.nvme_raid or spec.fss_mounts or spec.lustre_mounts)
    if spec.storage_mode == "inherit" and has_storage:
        raise ValueError(
            "Select --storage-mode append or --storage-mode replace when adding "
            "FSS, Lustre, or NVMe RAID bootstrap."
        )
    if spec.nvme_raid:
        if spec.nvme_raid.level not in {0, 1, 5, 6, 10}:
            raise ValueError("NVMe RAID level must be one of: 0, 1, 5, 6, 10.")
        _validate_absolute_path(spec.nvme_raid.mount_path, "NVMe mount path")
        if not spec.nvme_raid.device_pattern.startswith("/dev/"):
            raise ValueError("NVMe device pattern must start with /dev/.")
    for fss_mount in spec.fss_mounts:
        _validate_absolute_path(fss_mount.export_path, "FSS export path")
        _validate_absolute_path(fss_mount.mount_path, "FSS mount path")
        _validate_single_line(
            fss_mount.mount_target_ip,
            "FSS mount target",
        )
    for lustre_mount in spec.lustre_mounts:
        _validate_single_line(
            lustre_mount.management_address,
            "Lustre management address",
        )
        _validate_single_line(
            lustre_mount.filesystem_name,
            "Lustre filesystem name",
        )
        _validate_absolute_path(
            lustre_mount.mount_path,
            "Lustre mount path",
        )
    return spec


def validate_pool_boot_volume_replace_spec(
    spec: PoolBootVolumeReplaceSpec,
) -> PoolBootVolumeReplaceSpec:
    updates = (
        spec.image_id,
        spec.boot_volume_size_in_gbs,
        spec.boot_volume_kms_key_id,
        spec.kubernetes_version,
        spec.node_metadata,
        spec.ssh_public_key,
    )
    if not any(value not in (None, "", ()) for value in updates):
        raise ValueError(
            "Managed-pool boot volume replacement requires at least one "
            "supported update: image, boot volume size or KMS key, Kubernetes "
            "version, node metadata, or SSH public key."
        )
    _require_positive(spec.boot_volume_size_in_gbs, "Boot volume size")
    validate_maximum_unavailable(spec.maximum_unavailable)

    metadata_keys = {key for key, _value in spec.node_metadata}
    reserved = sorted(metadata_keys.intersection(_RESERVED_NODE_METADATA_KEYS))
    if reserved:
        raise ValueError(
            "Use dedicated boot-volume-replacement options instead of overriding "
            f"reserved OKE node metadata: {', '.join(reserved)}."
        )
    for key, value in spec.node_metadata:
        _validate_single_line(key, "Node metadata key")
        _validate_single_line(value, f"Node metadata value for {key}")
    return spec


def validate_maximum_unavailable(value: str) -> None:
    normalized = value.strip()
    if normalized.endswith("%"):
        percentage = normalized[:-1]
        if not percentage.isdigit() or not 1 <= int(percentage) <= 100:
            raise ValueError(
                "Maximum unavailable percentage must be between 1% and 100%."
            )
        return
    if not normalized.isdigit() or int(normalized) < 1:
        raise ValueError("Maximum unavailable must be a positive count or percentage.")


def validate_eviction_grace_duration(value: str) -> str:
    normalized = value.strip().upper()
    match = _ISO_HOUR_MINUTE_DURATION_PATTERN.fullmatch(normalized)
    if not match or not any(match.groupdict().values()):
        raise ValueError(
            "Eviction grace duration must be an ISO-8601 hour/minute duration "
            "such as PT30M."
        )
    minutes = int(match.group("hours") or 0) * 60 + int(
        match.group("minutes") or 0
    )
    if minutes > 60:
        raise ValueError("Eviction grace duration cannot exceed PT60M.")
    return normalized


def validate_kubernetes_label(key: str, value: str) -> None:
    prefix, separator, name = key.rpartition("/")
    if not separator:
        prefix = ""
    if not name or len(name) > 63 or not _LABEL_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid Kubernetes label key: {key}")
    if prefix and (len(prefix) > 253 or not _DNS_PREFIX_PATTERN.fullmatch(prefix)):
        raise ValueError(f"Invalid Kubernetes label prefix: {prefix}")
    if value and (
        len(value) > 63 or not _LABEL_NAME_PATTERN.fullmatch(value)
    ):
        raise ValueError(f"Invalid Kubernetes label value for {key}: {value}")


def _require_positive(value: int | None, label: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{label} must be greater than zero.")


def _require_positive_float(value: float | None, label: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{label} must be greater than zero.")


def _validate_absolute_path(value: str, label: str) -> None:
    _validate_single_line(value, label)
    if not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute path.")


def _validate_single_line(value: str, label: str) -> None:
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty single-line value.")
