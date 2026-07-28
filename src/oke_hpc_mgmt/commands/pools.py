from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from oke_hpc_mgmt.commands.bvr_options import (
    boot_volume_replace_wait_options,
    build_pool_boot_volume_replace_spec,
    pool_boot_volume_replace_options,
)
from oke_hpc_mgmt.commands.common import (
    CliState,
    confirm_plans,
    emit_plans,
    mutation_options,
    output_option,
    pass_state,
    print_discovery_warnings,
    progress,
    resolve_output,
    wait_options,
)
from oke_hpc_mgmt.commands.pool_create_options import (
    build_pool_create_spec,
    pool_create_options,
)
from oke_hpc_mgmt.render import POOL_COLUMNS, pool_rows, print_records
from oke_hpc_mgmt.workflows.lifecycle import (
    WorkflowNotFound,
    execute_pool_boot_volume_replace,
    execute_pool_create,
    execute_pool_delete,
    execute_pool_resize,
    prepare_pool_boot_volume_replace,
    prepare_pool_create,
    prepare_pool_delete,
    prepare_pool_resize,
)


@click.group(help="Manage the lifecycle of OCI HPC OKE worker pools.")
def pools() -> None:
    pass


@pools.command("list", help="List discovered managed and self-managed worker pools.")
@output_option
@pass_state
def list_pools(state: CliState, output_override: str | None) -> int:
    snapshot = state.service(
        include_pod_counts=False,
        include_autoscaler=False,
        include_kueue=False,
    ).discover()
    print_records(pool_rows(snapshot.pools), resolve_output(state, output_override), POOL_COLUMNS)
    print_discovery_warnings(snapshot.warnings)
    return 0


@pools.command("get", help="Show one worker pool by name or OCI backing OCID.")
@click.argument("pool")
@output_option
@pass_state
def get_pool(state: CliState, pool: str, output_override: str | None) -> int:
    snapshot = state.service(
        include_pod_counts=False,
        include_autoscaler=False,
        include_kueue=False,
    ).discover()
    selected = snapshot.pool_by_name(pool)
    if selected is None:
        raise WorkflowNotFound(f"Pool not found: {pool}")
    output = resolve_output(state, output_override)
    print_records(pool_rows([selected]), output, None if output == "json" else POOL_COLUMNS)
    print_discovery_warnings(snapshot.warnings)
    return 0


@click.command(
    "boot-volume-replace",
    help=(
        "Roll a managed OKE pool through BVR while changing supported worker "
        "properties such as its image."
    ),
)
@click.argument("pool")
@pool_boot_volume_replace_options
@click.option(
    "--grace-period",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
    help="Pod termination grace period used for eviction preflight.",
)
@click.option(
    "--force",
    "force_unmanaged",
    is_flag=True,
    help="Acknowledge eviction of pods without a controller.",
)
@click.option(
    "--delete-emptydir-data",
    is_flag=True,
    help="Acknowledge deletion of pod-local emptyDir data.",
)
@click.option(
    "--allow-system-pool",
    is_flag=True,
    help="Permit BVR of oke-system after explicit review.",
)
@boot_volume_replace_wait_options
@mutation_options
@output_option
@pass_state
def replace_pool_boot_volumes(
    state: CliState,
    pool: str,
    image_id: str | None,
    boot_volume_size_in_gbs: int | None,
    boot_volume_kms_key_id: str | None,
    kubernetes_version: str | None,
    node_metadata_values: tuple[str, ...],
    ssh_public_key_file: Path | None,
    maximum_unavailable: str,
    grace_period: int,
    force_unmanaged: bool,
    delete_emptydir_data: bool,
    allow_system_pool: bool,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    spec = build_pool_boot_volume_replace_spec(
        image_id=image_id,
        boot_volume_size_in_gbs=boot_volume_size_in_gbs,
        boot_volume_kms_key_id=boot_volume_kms_key_id,
        kubernetes_version=kubernetes_version,
        node_metadata_values=node_metadata_values,
        ssh_public_key_file=ssh_public_key_file,
        maximum_unavailable=maximum_unavailable,
    )
    service = state.service(
        include_pod_counts=True,
        include_autoscaler=True,
        include_kueue=False,
        include_addons=True,
    )
    prepared = prepare_pool_boot_volume_replace(
        service,
        pool,
        spec,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        allow_system_pool=allow_system_pool,
        drain_grace_period_seconds=grace_period,
    )
    if dry_run:
        emit_plans(state, output_override, [prepared.plan])
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    confirm_plans(
        [prepared.plan],
        prepared.pool.name,
        approved=yes,
    )
    result = execute_pool_boot_volume_replace(
        service,
        prepared,
        wait=wait,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        lock=lock,
        drain_grace_period_seconds=grace_period,
        progress=progress,
    )
    print_records([result], resolve_output(state, output_override))
    print_discovery_warnings(prepared.snapshot.warnings)
    return 0


pools.add_command(replace_pool_boot_volumes)
pools.add_command(replace_pool_boot_volumes, "bvr")


@pools.command(
    "create",
    help=(
        "Create a managed CPU/GPU pool or self-managed RDMA Cluster Network "
        "pool from a proven OKE source."
    ),
)
@click.argument("name")
@pool_create_options
@wait_options
@mutation_options
@output_option
@pass_state
def create_pool(
    state: CliState,
    name: str,
    count: int,
    source_identifier: str | None,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
    **create_options: Any,
) -> int:
    spec = build_pool_create_spec(
        pool_type=str(create_options.pop("pool_type")),
        **create_options,
    )
    service = state.service(
        include_pod_counts=False,
        include_autoscaler=False,
        include_kueue=False,
    )
    prepared = prepare_pool_create(
        service,
        name,
        count,
        spec=spec,
        source_identifier=source_identifier,
    )
    if dry_run:
        emit_plans(state, output_override, [prepared.plan])
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    confirm_plans([prepared.plan], prepared.name, approved=yes)
    result = execute_pool_create(
        service,
        prepared,
        wait=wait,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        lock=lock,
        progress=progress,
    )
    print_records([result], resolve_output(state, output_override))
    print_discovery_warnings(prepared.snapshot.warnings)
    return 0


@pools.command(
    "delete",
    help="Drain workers and delete an entire managed or self-managed pool.",
)
@click.argument("pool")
@click.option(
    "--drain/--no-drain",
    default=True,
    show_default=True,
    help="Cordon and evict non-DaemonSet pods before deleting the pool.",
)
@click.option(
    "--allow-workloads",
    is_flag=True,
    help="Allow --no-drain deletion while workload pods are present.",
)
@click.option(
    "--delete-emptydir-data",
    is_flag=True,
    help="Acknowledge deletion of pod-local emptyDir data.",
)
@click.option(
    "--force",
    "force_unmanaged",
    is_flag=True,
    help="Allow eviction of pods without a controller.",
)
@click.option(
    "--allow-system-pool",
    is_flag=True,
    help="Permit deletion of oke-system after explicit review.",
)
@click.option(
    "--grace-period",
    "drain_grace_period_seconds",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
)
@click.option(
    "--drain-timeout",
    "drain_timeout_seconds",
    type=click.IntRange(min=1),
    default=600,
    show_default=True,
)
@wait_options
@mutation_options
@output_option
@pass_state
def delete_pool(
    state: CliState,
    pool: str,
    drain: bool,
    allow_workloads: bool,
    delete_emptydir_data: bool,
    force_unmanaged: bool,
    allow_system_pool: bool,
    drain_grace_period_seconds: int,
    drain_timeout_seconds: int,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    service = state.service(
        include_pod_counts=True,
        include_autoscaler=True,
        include_kueue=False,
    )
    prepared = prepare_pool_delete(
        service,
        pool,
        drain=drain,
        allow_workloads=allow_workloads,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        allow_system_pool=allow_system_pool,
        drain_grace_period_seconds=drain_grace_period_seconds,
    )
    if dry_run:
        emit_plans(state, output_override, [prepared.plan])
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    confirm_plans([prepared.plan], prepared.pool.name, approved=yes)
    result = execute_pool_delete(
        service,
        prepared,
        drain=drain,
        drain_grace_period_seconds=drain_grace_period_seconds,
        drain_timeout_seconds=drain_timeout_seconds,
        wait=wait,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        lock=lock,
        progress=progress,
    )
    print_records([result], resolve_output(state, output_override))
    print_discovery_warnings(prepared.snapshot.warnings)
    return 0


@pools.command(
    "resize",
    help="Set an exact pool size or apply a signed size change.",
)
@click.argument("pool")
@click.option("--size", type=click.IntRange(min=0), help="Set the exact desired node count.")
@click.option(
    "--delta",
    type=int,
    help="Change capacity by this signed count: positive adds nodes; negative removes nodes.",
)
@wait_options
@mutation_options
@output_option
@pass_state
def resize_pool(
    state: CliState,
    pool: str,
    size: int | None,
    delta: int | None,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    return _resize(
        state,
        pool,
        size=size,
        delta=delta,
        wait=wait,
        timeout=timeout,
        poll_interval=poll_interval,
        dry_run=dry_run,
        lock=lock,
        yes=yes,
        output_override=output_override,
    )


@pools.command("add", help="Add a positive number of workers to a pool.")
@click.argument("pool")
@click.option(
    "--count",
    type=click.IntRange(min=1),
    required=True,
    help="Number of workers to add.",
)
@wait_options
@mutation_options
@output_option
@pass_state
def add_pool_capacity(
    state: CliState,
    pool: str,
    count: int,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    return _resize(
        state,
        pool,
        size=None,
        delta=count,
        wait=wait,
        timeout=timeout,
        poll_interval=poll_interval,
        dry_run=dry_run,
        lock=lock,
        yes=yes,
        output_override=output_override,
    )


@pools.command(
    "remove",
    help="Remove capacity from a pool; the owning service selects workers.",
)
@click.argument("pool")
@click.option(
    "--count",
    type=click.IntRange(min=1),
    required=True,
    help="Number of workers to remove from desired capacity.",
)
@wait_options
@mutation_options
@output_option
@pass_state
def remove_pool_capacity(
    state: CliState,
    pool: str,
    count: int,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    return _resize(
        state,
        pool,
        size=None,
        delta=-count,
        wait=wait,
        timeout=timeout,
        poll_interval=poll_interval,
        dry_run=dry_run,
        lock=lock,
        yes=yes,
        output_override=output_override,
    )


def _resize(
    state: CliState,
    pool: str,
    *,
    size: int | None,
    delta: int | None,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    service = state.service(
        include_pod_counts=True,
        include_autoscaler=True,
        include_kueue=False,
    )
    prepared = prepare_pool_resize(service, pool, size=size, delta=delta)
    if dry_run:
        emit_plans(state, output_override, [prepared.plan])
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    confirm_plans([prepared.plan], prepared.pool.name, approved=yes)
    result = execute_pool_resize(
        service,
        prepared,
        wait=wait,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        lock=lock,
        progress=progress,
    )
    print_records([result], resolve_output(state, output_override))
    print_discovery_warnings(prepared.snapshot.warnings)
    return 0
