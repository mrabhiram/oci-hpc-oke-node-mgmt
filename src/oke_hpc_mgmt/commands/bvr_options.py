from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from oke_hpc_mgmt.models import PoolBootVolumeReplaceSpec
from oke_hpc_mgmt.validation import (
    parse_key_value_options,
    validate_pool_boot_volume_replace_spec,
)

CommandFunction = TypeVar("CommandFunction", bound=Callable[..., Any])


def boot_volume_replace_wait_options(
    function: CommandFunction,
) -> CommandFunction:
    function = click.option(
        "--poll-interval",
        type=click.IntRange(min=1),
        default=30,
        show_default=True,
        help="Seconds between BVR state checks.",
    )(function)
    function = click.option(
        "--timeout",
        type=click.IntRange(min=1),
        default=7200,
        show_default=True,
        help="Maximum seconds to wait for BVR convergence.",
    )(function)
    function = click.option(
        "--wait/--no-wait",
        default=False,
        help="Wait for OKE, boot volume, node, and resource convergence.",
    )(function)
    return function


def pool_boot_volume_replace_options(
    function: CommandFunction,
) -> CommandFunction:
    options: tuple[Callable[[CommandFunction], CommandFunction], ...] = (
        click.option(
            "--image-id",
            help="New image OCID applied to every managed worker during BVR.",
        ),
        click.option(
            "--boot-volume-size",
            "boot_volume_size_in_gbs",
            type=click.IntRange(min=1),
            help="New boot volume size in GB; boot volumes cannot be reduced.",
        ),
        click.option(
            "--boot-volume-kms-key-id",
            help="New Vault key OCID for managed worker boot volumes.",
        ),
        click.option(
            "--kubernetes-version",
            help="New Kubernetes version applied during managed-pool BVR.",
        ),
        click.option(
            "--node-metadata",
            "node_metadata_values",
            multiple=True,
            metavar="KEY=VALUE",
            help="Non-reserved node metadata to merge before BVR. Repeatable.",
        ),
        click.option(
            "--ssh-public-key-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="UTF-8 SSH public key file applied during managed-pool BVR.",
        ),
        click.option(
            "--maximum-unavailable",
            default="1",
            show_default=True,
            help="Maximum unavailable workers as a positive count or percentage.",
        ),
    )
    for option in reversed(options):
        function = option(function)
    return function


def build_pool_boot_volume_replace_spec(
    *,
    image_id: str | None,
    boot_volume_size_in_gbs: int | None,
    boot_volume_kms_key_id: str | None,
    kubernetes_version: str | None,
    node_metadata_values: tuple[str, ...],
    ssh_public_key_file: Path | None,
    maximum_unavailable: str,
) -> PoolBootVolumeReplaceSpec:
    spec = PoolBootVolumeReplaceSpec(
        image_id=_stripped(image_id),
        boot_volume_size_in_gbs=boot_volume_size_in_gbs,
        boot_volume_kms_key_id=_stripped(boot_volume_kms_key_id),
        kubernetes_version=_stripped(kubernetes_version),
        node_metadata=parse_key_value_options(
            node_metadata_values,
            option_name="--node-metadata",
        ),
        ssh_public_key=_read_text(ssh_public_key_file),
        maximum_unavailable=maximum_unavailable.strip(),
    )
    return validate_pool_boot_volume_replace_spec(spec)


def _read_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"Input file is not UTF-8 text: {path}") from exc
    if not content:
        raise ValueError(f"Input file is empty: {path}")
    return content


def _stripped(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
