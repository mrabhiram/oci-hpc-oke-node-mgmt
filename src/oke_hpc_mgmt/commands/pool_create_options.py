from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from oke_hpc_mgmt.models import (
    CLUSTER_NETWORK_PLACEMENT_CONSTRAINTS,
    NODE_CYCLING_MODES,
    NODE_POOL_CNI_TYPES,
    POOL_CREATE_TYPES,
    POOL_STORAGE_MODES,
    FssMountSpec,
    LustreMountSpec,
    NvmeRaidSpec,
    PoolCreateSpec,
)
from oke_hpc_mgmt.validation import (
    parse_key_value_options,
    validate_pool_create_spec,
)

CommandFunction = TypeVar("CommandFunction", bound=Callable[..., Any])


def pool_create_options(function: CommandFunction) -> CommandFunction:
    options: tuple[Callable[[CommandFunction], CommandFunction], ...] = (
        click.option(
            "--type",
            "pool_type",
            type=click.Choice(POOL_CREATE_TYPES, case_sensitive=False),
            required=True,
            help="Pool backend: managed CPU, managed GPU, or self-managed RDMA.",
        ),
        click.option(
            "--count",
            type=click.IntRange(min=1),
            required=True,
            help="Initial number of workers.",
        ),
        click.option(
            "--from-pool",
            "source_identifier",
            help="Source pool whose proven OKE bootstrap and defaults are inherited.",
        ),
        click.option("--availability-domain", help="Target availability domain name."),
        click.option("--shape", help="OCI Compute shape override."),
        click.option("--image-id", help="Custom or platform image OCID override."),
        click.option(
            "--subnet-id",
            "primary_subnet_id",
            help="Worker primary VNIC subnet OCID.",
        ),
        click.option(
            "--pod-subnet-id",
            "pod_subnet_ids",
            multiple=True,
            help="VCN-native pod subnet OCID. Repeat for managed pools.",
        ),
        click.option(
            "--node-nsg-id",
            "node_nsg_ids",
            multiple=True,
            help="Worker VNIC network security group OCID. Repeatable.",
        ),
        click.option(
            "--pod-nsg-id",
            "pod_nsg_ids",
            multiple=True,
            help="VCN-native pod network security group OCID. Repeatable.",
        ),
        click.option(
            "--boot-volume-size",
            "boot_volume_size_in_gbs",
            type=click.IntRange(min=1),
            help="Boot volume size in GB.",
        ),
        click.option(
            "--boot-volume-vpus-per-gb",
            type=click.IntRange(min=1),
            help="Boot volume performance for self-managed RDMA workers.",
        ),
        click.option(
            "--boot-volume-kms-key-id",
            help="Boot volume Vault key OCID.",
        ),
        click.option("--ocpus", type=click.FloatRange(min=0, min_open=True)),
        click.option(
            "--memory-in-gbs",
            type=click.FloatRange(min=0, min_open=True),
        ),
        click.option("--kubernetes-version", help="Worker Kubernetes version."),
        click.option(
            "--max-pods-per-node",
            type=click.IntRange(min=1, max=110),
        ),
        click.option(
            "--ssh-public-key-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="File containing SSH public key data for new workers.",
        ),
        click.option(
            "--cloud-init-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="Additional cloud-init MIME, cloud-config, or shell-script part.",
        ),
        click.option(
            "--node-label",
            "node_label_values",
            multiple=True,
            metavar="KEY=VALUE",
            help="Initial Kubernetes node label. Repeatable.",
        ),
        click.option(
            "--node-metadata",
            "node_metadata_values",
            multiple=True,
            metavar="KEY=VALUE",
            help="Additional non-reserved instance metadata. Repeatable.",
        ),
        click.option(
            "--freeform-tag",
            "freeform_tag_values",
            multiple=True,
            metavar="KEY=VALUE",
            help="OCI freeform tag. Repeatable.",
        ),
        click.option(
            "--capacity-reservation-id",
            help="Capacity reservation OCID for placement.",
        ),
        click.option(
            "--fault-domain",
            "fault_domains",
            multiple=True,
            help="Fault domain such as FD-1. Repeatable for managed pools.",
        ),
        click.option(
            "--cni-type",
            type=click.Choice(NODE_POOL_CNI_TYPES, case_sensitive=False),
            help="Expected cluster CNI type; it must match the source pool.",
        ),
        click.option(
            "--placement-constraint",
            type=click.Choice(
                CLUSTER_NETWORK_PLACEMENT_CONSTRAINTS,
                case_sensitive=False,
            ),
            help="Self-managed RDMA Cluster Network placement constraint.",
        ),
        click.option(
            "--assign-public-ip/--no-assign-public-ip",
            default=None,
            help="Assign public IPs to self-managed RDMA primary VNICs.",
        ),
        click.option(
            "--pv-encryption-in-transit/--no-pv-encryption-in-transit",
            default=None,
            help="Override paravirtualized volume in-transit encryption.",
        ),
        click.option(
            "--legacy-imds-endpoints-disabled/--legacy-imds-endpoints-enabled",
            default=None,
            help="Control legacy instance metadata service endpoints.",
        ),
        click.option(
            "--node-cycling-enabled/--node-cycling-disabled",
            default=None,
            help="Managed OKE node-pool cycling policy.",
        ),
        click.option(
            "--node-cycling-max-surge",
            help="Managed OKE maximum cycling surge, as a count or percentage.",
        ),
        click.option(
            "--node-cycling-max-unavailable",
            help="Managed OKE maximum unavailable, as a count or percentage.",
        ),
        click.option(
            "--node-cycling-mode",
            type=click.Choice(NODE_CYCLING_MODES, case_sensitive=False),
            help="Managed OKE node or boot-volume replacement mode.",
        ),
        click.option(
            "--eviction-grace-duration",
            help="Managed OKE ISO-8601 node eviction grace duration.",
        ),
        click.option(
            "--force-delete-after-eviction-grace/--no-force-delete-after-eviction-grace",
            default=None,
        ),
        click.option(
            "--force-action-after-eviction-grace/--no-force-action-after-eviction-grace",
            default=None,
        ),
        click.option(
            "--pre-bootstrap-script-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="Bash commands run through pre_oke before OKE bootstrap.",
        ),
        click.option(
            "--post-bootstrap-script-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="Bash commands run through post_oke after OKE bootstrap.",
        ),
        click.option(
            "--kubelet-extra-args",
            help="Additional kubelet arguments exposed through OKE metadata.",
        ),
        click.option(
            "--storage-mode",
            type=click.Choice(POOL_STORAGE_MODES, case_sensitive=False),
            default="inherit",
            show_default=True,
            help="Inherit, append to, or replace official storage bootstrap commands.",
        ),
        click.option(
            "--nvme-raid-level",
            type=click.Choice(("0", "1", "5", "6", "10")),
            help="Create local NVMe RAID at the selected level.",
        ),
        click.option(
            "--nvme-device-pattern",
            help="NVMe device glob. Defaults to /dev/nvme*n1 when RAID is enabled.",
        ),
        click.option(
            "--nvme-mount-path",
            help="NVMe RAID mount path. Defaults to /mnt/nvme.",
        ),
        click.option("--fss-mount-target-ip", help="Existing OCI FSS mount target IP."),
        click.option("--fss-export-path", help="Existing OCI FSS export path."),
        click.option(
            "--fss-mount-path",
            help="Worker FSS mount path. Defaults to /mnt/oci-fss.",
        ),
        click.option(
            "--lustre-management-address",
            help="Existing OCI Lustre management service address.",
        ),
        click.option(
            "--lustre-filesystem-name",
            help="Existing OCI Lustre filesystem name.",
        ),
        click.option(
            "--lustre-mount-path",
            help="Worker Lustre mount path. Defaults to /mnt/oci-lustre.",
        ),
    )
    for option in reversed(options):
        function = option(function)
    return function


def build_pool_create_spec(
    *,
    pool_type: str,
    availability_domain: str | None,
    shape: str | None,
    image_id: str | None,
    primary_subnet_id: str | None,
    pod_subnet_ids: tuple[str, ...],
    node_nsg_ids: tuple[str, ...],
    pod_nsg_ids: tuple[str, ...],
    boot_volume_size_in_gbs: int | None,
    boot_volume_vpus_per_gb: int | None,
    boot_volume_kms_key_id: str | None,
    ocpus: float | None,
    memory_in_gbs: float | None,
    kubernetes_version: str | None,
    max_pods_per_node: int | None,
    ssh_public_key_file: Path | None,
    cloud_init_file: Path | None,
    node_label_values: tuple[str, ...],
    node_metadata_values: tuple[str, ...],
    freeform_tag_values: tuple[str, ...],
    capacity_reservation_id: str | None,
    fault_domains: tuple[str, ...],
    cni_type: str | None,
    placement_constraint: str | None,
    assign_public_ip: bool | None,
    pv_encryption_in_transit: bool | None,
    legacy_imds_endpoints_disabled: bool | None,
    node_cycling_enabled: bool | None,
    node_cycling_max_surge: str | None,
    node_cycling_max_unavailable: str | None,
    node_cycling_mode: str | None,
    eviction_grace_duration: str | None,
    force_delete_after_eviction_grace: bool | None,
    force_action_after_eviction_grace: bool | None,
    pre_bootstrap_script_file: Path | None,
    post_bootstrap_script_file: Path | None,
    kubelet_extra_args: str | None,
    storage_mode: str,
    nvme_raid_level: str | None,
    nvme_device_pattern: str | None,
    nvme_mount_path: str | None,
    fss_mount_target_ip: str | None,
    fss_export_path: str | None,
    fss_mount_path: str | None,
    lustre_management_address: str | None,
    lustre_filesystem_name: str | None,
    lustre_mount_path: str | None,
) -> PoolCreateSpec:
    nvme = _build_nvme_spec(
        nvme_raid_level,
        nvme_device_pattern,
        nvme_mount_path,
    )
    fss = _build_fss_mount(
        fss_mount_target_ip,
        fss_export_path,
        fss_mount_path,
    )
    lustre = _build_lustre_mount(
        lustre_management_address,
        lustre_filesystem_name,
        lustre_mount_path,
    )
    spec = PoolCreateSpec(
        pool_type=pool_type.lower(),
        availability_domain=_stripped(availability_domain),
        shape=_stripped(shape),
        image_id=_stripped(image_id),
        primary_subnet_id=_stripped(primary_subnet_id),
        pod_subnet_ids=_nonempty(pod_subnet_ids),
        node_nsg_ids=_nonempty(node_nsg_ids),
        pod_nsg_ids=_nonempty(pod_nsg_ids),
        boot_volume_size_in_gbs=boot_volume_size_in_gbs,
        boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
        boot_volume_kms_key_id=_stripped(boot_volume_kms_key_id),
        ocpus=ocpus,
        memory_in_gbs=memory_in_gbs,
        kubernetes_version=_stripped(kubernetes_version),
        max_pods_per_node=max_pods_per_node,
        ssh_public_key=_read_text(ssh_public_key_file, strip=True),
        cloud_init=_read_bytes(cloud_init_file),
        node_labels=parse_key_value_options(
            node_label_values,
            option_name="--node-label",
        ),
        node_metadata=parse_key_value_options(
            node_metadata_values,
            option_name="--node-metadata",
        ),
        freeform_tags=parse_key_value_options(
            freeform_tag_values,
            option_name="--freeform-tag",
        ),
        capacity_reservation_id=_stripped(capacity_reservation_id),
        fault_domains=_nonempty(fault_domains),
        cni_type=cni_type.upper() if cni_type else None,
        placement_constraint=(
            placement_constraint.upper() if placement_constraint else None
        ),
        assign_public_ip=assign_public_ip,
        pv_encryption_in_transit=pv_encryption_in_transit,
        legacy_imds_endpoints_disabled=legacy_imds_endpoints_disabled,
        node_cycling_enabled=node_cycling_enabled,
        node_cycling_max_surge=_stripped(node_cycling_max_surge),
        node_cycling_max_unavailable=_stripped(
            node_cycling_max_unavailable
        ),
        node_cycling_mode=(
            node_cycling_mode.upper() if node_cycling_mode else None
        ),
        eviction_grace_duration=_stripped(eviction_grace_duration),
        force_delete_after_eviction_grace=force_delete_after_eviction_grace,
        force_action_after_eviction_grace=force_action_after_eviction_grace,
        pre_bootstrap_script=_read_bytes(pre_bootstrap_script_file),
        post_bootstrap_script=_read_bytes(post_bootstrap_script_file),
        kubelet_extra_args=_stripped(kubelet_extra_args),
        storage_mode=storage_mode.lower(),
        nvme_raid=nvme,
        fss_mounts=(fss,) if fss else (),
        lustre_mounts=(lustre,) if lustre else (),
    )
    return validate_pool_create_spec(spec)


def _build_nvme_spec(
    level: str | None,
    device_pattern: str | None,
    mount_path: str | None,
) -> NvmeRaidSpec | None:
    if level is None:
        if device_pattern or mount_path:
            raise ValueError(
                "--nvme-device-pattern and --nvme-mount-path require "
                "--nvme-raid-level."
            )
        return None
    return NvmeRaidSpec(
        level=int(level),
        device_pattern=_stripped(device_pattern) or "/dev/nvme*n1",
        mount_path=_stripped(mount_path) or "/mnt/nvme",
    )


def _build_fss_mount(
    mount_target_ip: str | None,
    export_path: str | None,
    mount_path: str | None,
) -> FssMountSpec | None:
    values = (mount_target_ip, export_path, mount_path)
    if not any(values):
        return None
    if not mount_target_ip or not export_path:
        raise ValueError(
            "FSS bootstrap requires --fss-mount-target-ip and --fss-export-path."
        )
    return FssMountSpec(
        export_path=export_path.strip(),
        mount_path=_stripped(mount_path) or "/mnt/oci-fss",
        mount_target_ip=mount_target_ip.strip(),
    )


def _build_lustre_mount(
    management_address: str | None,
    filesystem_name: str | None,
    mount_path: str | None,
) -> LustreMountSpec | None:
    values = (management_address, filesystem_name, mount_path)
    if not any(values):
        return None
    if not management_address or not filesystem_name:
        raise ValueError(
            "Lustre bootstrap requires --lustre-management-address and "
            "--lustre-filesystem-name."
        )
    return LustreMountSpec(
        management_address=management_address.strip(),
        filesystem_name=filesystem_name.strip(),
        mount_path=_stripped(mount_path) or "/mnt/oci-lustre",
    )


def _read_bytes(path: Path | None) -> bytes | None:
    if path is None:
        return None
    content = path.read_bytes()
    if not content.strip():
        raise ValueError(f"Input file is empty: {path}")
    return content


def _read_text(path: Path | None, *, strip: bool) -> str | None:
    content = _read_bytes(path)
    if content is None:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Input file is not UTF-8 text: {path}") from exc
    return text.strip() if strip else text


def _nonempty(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def _stripped(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
