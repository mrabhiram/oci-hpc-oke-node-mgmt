from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import click

from oke_hpc_mgmt.commands.bvr_options import (
    boot_volume_replace_wait_options,
)
from oke_hpc_mgmt.commands.common import (
    CliState,
    confirm_plans,
    display_options,
    emit_plans,
    mutation_options,
    node_selector_options,
    output_option,
    pass_state,
    print_discovery_warnings,
    progress,
    resolve_output,
    selected_identifiers,
    wait_options,
)
from oke_hpc_mgmt.render import NODE_COLUMNS, node_rows, parse_columns, print_records, sort_records
from oke_hpc_mgmt.selection import select_nodes
from oke_hpc_mgmt.workflows.lifecycle import (
    execute_node_boot_volume_replace,
    execute_node_removal,
    prepare_node_boot_volume_replace,
    prepare_node_removal,
)
from oke_hpc_mgmt.workflows.node_maintenance import (
    execute_node_maintenance,
    prepare_node_maintenance,
)


NODE_AVAILABLE_COLUMNS = [
    *NODE_COLUMNS,
    "ready",
    "schedulable",
    "instance_ocid",
    "node_pool_id",
]


@click.group(help="Discover, maintain, replace, or terminate Kubernetes worker nodes.")
def nodes() -> None:
    pass


@nodes.command("list", help="List Kubernetes worker nodes with optional exact-match filters.")
@click.argument("identifiers", nargs=-1)
@node_selector_options
@click.option("--pool", help="Only show nodes in this worker pool.")
@click.option("--rdma-only", is_flag=True, help="Only show nodes with valid OCI RDMA topology.")
@click.option("--not-ready", is_flag=True, help="Only show nodes that are not Ready.")
@click.option("--workloads", is_flag=True, help="Only show nodes running non-system workload pods.")
@display_options
@pass_state
def list_nodes(
    state: CliState,
    identifiers: tuple[str, ...],
    node_values: tuple[str, ...],
    fields: str | None,
    pool: str | None,
    rdma_only: bool,
    not_ready: bool,
    workloads: bool,
    output_override: str | None,
    columns: str | None,
    sort_specification: str | None,
    no_header: bool,
    one_line: bool,
) -> int:
    snapshot = state.service(include_autoscaler=False, include_kueue=False).discover()
    selected, missing = select_nodes(
        snapshot,
        identifiers=selected_identifiers(identifiers, node_values),
        fields=fields,
        pool=pool,
        rdma_only=rdma_only,
        not_ready=not_ready,
        workloads=workloads,
    )
    rows = node_rows(selected)
    if sort_specification:
        parse_columns(sort_specification, NODE_AVAILABLE_COLUMNS)
        rows = sort_records(rows, sort_specification) if rows else rows
    selected_columns = parse_columns(columns, NODE_AVAILABLE_COLUMNS) if columns else NODE_COLUMNS
    if columns:
        rows = [{column: row.get(column) for column in selected_columns} for row in rows]
    output = resolve_output(state, output_override)
    print_records(
        rows,
        output,
        None if output == "json" and not columns else selected_columns,
        show_header=not no_header,
        one_line=one_line,
    )
    if missing:
        click.echo(f"Nodes not found: {', '.join(missing)}", err=True)
    print_discovery_warnings(snapshot.warnings)
    return 1 if missing else 0


@nodes.command("get", help="Get nodes by name, Slurm name, IP, provider ID, or instance OCID.")
@click.argument("identifiers", nargs=-1, required=True)
@output_option
@pass_state
def get_nodes(
    state: CliState,
    identifiers: tuple[str, ...],
    output_override: str | None,
) -> int:
    snapshot = state.service(include_autoscaler=False, include_kueue=False).discover()
    selected, missing = select_nodes(snapshot, identifiers=identifiers)
    output = resolve_output(state, output_override)
    print_records(node_rows(selected), output, None if output == "json" else NODE_COLUMNS)
    if missing:
        click.echo(f"Nodes not found: {', '.join(missing)}", err=True)
    print_discovery_warnings(snapshot.warnings)
    return 1 if missing else 0


def removal_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--force-after-grace",
        is_flag=True,
        help="Force managed OKE compute deletion after its eviction grace period.",
    )(function)
    function = click.option(
        "--eviction-grace",
        default="PT10M",
        show_default=True,
        help="Managed OKE node eviction grace duration.",
    )(function)
    function = click.option(
        "--drain-timeout",
        type=click.IntRange(min=1),
        default=600,
        show_default=True,
        help="Maximum seconds to wait for Kubernetes evictions.",
    )(function)
    function = click.option(
        "--grace-period",
        type=click.IntRange(min=0),
        default=30,
        show_default=True,
        help="Pod termination grace period in seconds.",
    )(function)
    function = click.option(
        "--force",
        "force_unmanaged",
        is_flag=True,
        help="Allow eviction of pods without a controller.",
    )(function)
    function = click.option(
        "--delete-emptydir-data",
        is_flag=True,
        help="Acknowledge deletion of pod-local emptyDir data.",
    )(function)
    function = click.option(
        "--allow-workloads",
        is_flag=True,
        help="Allow removal without drain when workload pods are present.",
    )(function)
    function = click.option(
        "--drain/--no-drain",
        default=True,
        show_default=True,
        help="Cordon and safely evict pods before terminating workers.",
    )(function)
    function = click.option(
        "--keep-size",
        is_flag=True,
        help="Keep desired capacity so the owning service replaces each terminated worker.",
    )(function)
    return function


@click.command("terminate", help="Safely terminate selected workers through their owning OCI service.")
@click.argument("identifiers", nargs=-1)
@node_selector_options
@removal_options
@wait_options
@mutation_options
@output_option
@pass_state
def terminate_nodes(
    state: CliState,
    identifiers: tuple[str, ...],
    node_values: tuple[str, ...],
    fields: str | None,
    keep_size: bool,
    drain: bool,
    allow_workloads: bool,
    delete_emptydir_data: bool,
    force_unmanaged: bool,
    grace_period: int,
    drain_timeout: int,
    eviction_grace: str,
    force_after_grace: bool,
    wait: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    lock: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    chosen = selected_identifiers(identifiers, node_values)
    service = state.service(include_autoscaler=True, include_kueue=False)
    prepared = prepare_node_removal(
        service,
        identifiers=chosen,
        fields=fields,
        keep_size=keep_size,
        drain=drain,
        allow_workloads=allow_workloads,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        eviction_grace=eviction_grace,
        force_after_grace=force_after_grace,
        drain_grace_period_seconds=grace_period,
    )
    if dry_run:
        emit_plans(state, output_override, prepared.plans)
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    confirmation = (
        prepared.nodes[0].k8s_name
        if len(prepared.nodes) == 1
        else f"terminate {len(prepared.nodes)} nodes"
    )
    confirm_plans(prepared.plans, confirmation, approved=yes)
    results = execute_node_removal(
        service,
        prepared,
        drain=drain,
        drain_grace_period_seconds=grace_period,
        drain_timeout_seconds=drain_timeout,
        wait=wait,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        lock=lock,
        eviction_grace=eviction_grace,
        force_after_grace=force_after_grace,
        progress=progress,
    )
    print_records(results, resolve_output(state, output_override))
    print_discovery_warnings(prepared.snapshot.warnings)
    return 0


nodes.add_command(terminate_nodes)
nodes.add_command(terminate_nodes, "remove")


def boot_volume_replace_options(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    function = click.option(
        "--grace-period",
        type=click.IntRange(min=0),
        default=30,
        show_default=True,
        help="Pod termination grace period used for the eviction preflight.",
    )(function)
    function = click.option(
        "--allow-system-pool",
        is_flag=True,
        help="Permit BVR of an oke-system worker after explicit review.",
    )(function)
    function = click.option(
        "--force",
        "force_unmanaged",
        is_flag=True,
        help="Acknowledge eviction of pods without a controller.",
    )(function)
    function = click.option(
        "--delete-emptydir-data",
        is_flag=True,
        help="Acknowledge deletion of pod-local emptyDir data.",
    )(function)
    function = click.option(
        "--force-after-grace",
        is_flag=True,
        help="Force the OKE BVR action when the eviction grace period ends.",
    )(function)
    function = click.option(
        "--eviction-grace",
        default="PT60M",
        show_default=True,
        help="OKE cordon-and-drain grace duration from PT0M through PT60M.",
    )(function)
    return function


@click.command(
    "boot-volume-replace",
    help=(
        "Replace boot volumes of specific managed or self-managed workers. "
        "This preserves the current image and node configuration."
    ),
)
@click.argument("identifiers", nargs=-1)
@node_selector_options
@boot_volume_replace_options
@boot_volume_replace_wait_options
@mutation_options
@output_option
@pass_state
def replace_node_boot_volumes(
    state: CliState,
    identifiers: tuple[str, ...],
    node_values: tuple[str, ...],
    fields: str | None,
    eviction_grace: str,
    force_after_grace: bool,
    delete_emptydir_data: bool,
    force_unmanaged: bool,
    allow_system_pool: bool,
    grace_period: int,
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
        include_addons=True,
    )
    prepared = prepare_node_boot_volume_replace(
        service,
        identifiers=selected_identifiers(identifiers, node_values),
        fields=fields,
        delete_emptydir_data=delete_emptydir_data,
        force_unmanaged=force_unmanaged,
        allow_system_pool=allow_system_pool,
        eviction_grace_duration=eviction_grace,
        force_after_grace=force_after_grace,
        drain_grace_period_seconds=grace_period,
    )
    if dry_run:
        emit_plans(state, output_override, prepared.plans)
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    confirmation = (
        prepared.nodes[0].k8s_name
        if len(prepared.nodes) == 1
        else f"replace boot volumes for {len(prepared.nodes)} nodes"
    )
    confirm_plans(prepared.plans, confirmation, approved=yes)
    results = execute_node_boot_volume_replace(
        service,
        prepared,
        wait=wait,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        lock=lock,
        drain_grace_period_seconds=grace_period,
        progress=progress,
    )
    print_records(results, resolve_output(state, output_override))
    print_discovery_warnings(prepared.snapshot.warnings)
    return 0


nodes.add_command(replace_node_boot_volumes)
nodes.add_command(replace_node_boot_volumes, "bvr")
nodes.add_command(replace_node_boot_volumes, "boot-volume-swap")


def maintenance_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--timeout",
        type=click.IntRange(min=1),
        default=600,
        show_default=True,
        help="Maximum seconds to wait for pod eviction.",
    )(function)
    function = click.option(
        "--grace-period",
        type=click.IntRange(min=0),
        default=30,
        show_default=True,
        help="Pod termination grace period in seconds.",
    )(function)
    function = click.option(
        "--force",
        "force_unmanaged",
        is_flag=True,
        help="Allow eviction of pods without a controller.",
    )(function)
    function = click.option(
        "--delete-emptydir-data",
        is_flag=True,
        help="Acknowledge deletion of pod-local emptyDir data.",
    )(function)
    return function


def _maintenance_command(action: str) -> click.Command:
    @click.command(action, help=f"{action.capitalize()} selected Kubernetes worker nodes.")
    @click.argument("identifiers", nargs=-1)
    @node_selector_options
    @maintenance_options
    @mutation_options
    @output_option
    @pass_state
    def command(
        state: CliState,
        identifiers: tuple[str, ...],
        node_values: tuple[str, ...],
        fields: str | None,
        delete_emptydir_data: bool,
        force_unmanaged: bool,
        grace_period: int,
        timeout: int,
        dry_run: bool,
        lock: bool,
        yes: bool,
        output_override: str | None,
    ) -> int:
        service = state.service(
            skip_oci=True,
            include_autoscaler=False,
            include_kueue=False,
            include_addons=False,
        )
        prepared = prepare_node_maintenance(
            service,
            action,
            identifiers=selected_identifiers(identifiers, node_values),
            fields=fields,
            delete_emptydir_data=delete_emptydir_data,
            force_unmanaged=force_unmanaged,
            grace_period_seconds=grace_period,
        )
        if dry_run:
            emit_plans(state, output_override, prepared.plans)
            print_discovery_warnings(prepared.snapshot.warnings)
            return 0
        confirmation = (
            prepared.nodes[0].k8s_name
            if len(prepared.nodes) == 1
            else f"{action} {len(prepared.nodes)} nodes"
        )
        confirm_plans(prepared.plans, confirmation, approved=yes)
        results = execute_node_maintenance(
            service,
            prepared,
            grace_period_seconds=grace_period,
            timeout_seconds=timeout,
            lock=lock,
        )
        print_records(results, resolve_output(state, output_override))
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0

    return cast(click.Command, command)


for maintenance_action in ("cordon", "uncordon", "drain"):
    nodes.add_command(_maintenance_command(maintenance_action))
