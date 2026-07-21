from __future__ import annotations

import click

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
from oke_hpc_mgmt.render import POOL_COLUMNS, pool_rows, print_records
from oke_hpc_mgmt.workflows.lifecycle import (
    WorkflowNotFound,
    execute_pool_resize,
    prepare_pool_resize,
)


@click.group(help="Discover and resize OCI HPC OKE worker pools.")
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
