from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

import click

from oke_hpc_mgmt.discovery import DiscoveryOptions, DiscoveryService
from oke_hpc_mgmt.exit_codes import DEGRADED, OPERATION_ERROR, SUCCESS
from oke_hpc_mgmt.models import HealthResult, OperationPlan
from oke_hpc_mgmt.render import operation_plan_rows, print_records, print_warnings
from oke_hpc_mgmt.selection import split_identifiers


OUTPUT_FORMATS = ("table", "json", "csv")
AUTH_METHODS = ("config_file", "instance_principal", "resource_principal", "none")

CommandFunction = TypeVar("CommandFunction", bound=Callable[..., Any])


@dataclass(frozen=True)
class CliState:
    compartment_id: str | None
    cluster_id: str | None
    region: str | None
    auth: str
    oci_config_file: str | None
    oci_profile: str | None
    kubeconfig: str | None
    context: str | None
    in_cluster: bool
    skip_oci: bool
    skip_kubernetes: bool
    output: str
    debug: bool

    def discovery_options(self, **overrides: object) -> DiscoveryOptions:
        options = DiscoveryOptions(
            compartment_id=self.compartment_id,
            cluster_id=self.cluster_id,
            region=self.region,
            auth=self.auth,
            oci_config_file=self.oci_config_file,
            oci_profile=self.oci_profile,
            kubeconfig=self.kubeconfig,
            context=self.context,
            in_cluster=self.in_cluster,
            skip_oci=self.skip_oci,
            skip_kubernetes=self.skip_kubernetes,
        )
        unknown = [name for name in overrides if not hasattr(options, name)]
        if unknown:
            raise TypeError(f"Unknown discovery option(s): {', '.join(sorted(unknown))}")
        for name, value in overrides.items():
            setattr(options, name, value)
        return options

    def service(self, **overrides: object) -> DiscoveryService:
        return DiscoveryService(self.discovery_options(**overrides))


pass_state = click.make_pass_decorator(CliState)


def output_option(function: CommandFunction) -> CommandFunction:
    return click.option(
        "--format",
        "--output",
        "output_override",
        type=click.Choice(OUTPUT_FORMATS, case_sensitive=False),
        default=None,
        help="Output format. Overrides the global setting.",
    )(function)


def display_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--one-line",
        is_flag=True,
        help="Print selected record names on one comma-separated line.",
    )(function)
    function = click.option(
        "--no-header",
        is_flag=True,
        help="Suppress table or CSV column headings.",
    )(function)
    function = click.option(
        "--sort",
        "sort_specification",
        metavar="FIELDS",
        help="Sort by comma-separated output fields.",
    )(function)
    function = click.option(
        "--columns",
        metavar="FIELDS",
        help="Show only the specified comma-separated output fields.",
    )(function)
    return output_option(function)


def wait_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--poll-interval",
        type=click.IntRange(min=1),
        default=30,
        show_default=True,
        help="Seconds between readiness checks.",
    )(function)
    function = click.option(
        "--timeout",
        type=click.IntRange(min=1),
        default=1800,
        show_default=True,
        help="Maximum seconds to wait for convergence.",
    )(function)
    function = click.option(
        "--wait/--no-wait",
        default=False,
        help="Wait for OCI, Kubernetes, GPU, and applicable RDMA readiness.",
    )(function)
    return function


def mutation_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--yes",
        is_flag=True,
        help="Approve the operation without an interactive typed confirmation.",
    )(function)
    function = click.option(
        "--lock/--no-lock",
        default=True,
        show_default=True,
        help="Serialize mutations with a Kubernetes Lease.",
    )(function)
    function = click.option(
        "--dry-run",
        is_flag=True,
        help="Validate the request and print the operation plan without changing resources.",
    )(function)
    return function


def node_selector_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--fields",
        metavar="KEY=VALUE,...",
        help="Select nodes by exact field values; multiple filters are ANDed.",
    )(function)
    function = click.option(
        "--nodes",
        "node_values",
        multiple=True,
        metavar="IDENTIFIERS",
        help="Select comma-separated node names, IPs, Slurm names, provider IDs, or OCIDs.",
    )(function)
    return function


def resolve_output(state: CliState, output_override: str | None) -> str:
    return output_override or state.output


def selected_identifiers(
    positional: Iterable[str],
    node_values: Iterable[str],
) -> tuple[str, ...]:
    return tuple(split_identifiers((*positional, *node_values)))


def emit_plans(
    state: CliState,
    output_override: str | None,
    plans: Iterable[OperationPlan],
) -> None:
    print_records(
        operation_plan_rows(list(plans)),
        resolve_output(state, output_override),
    )


def confirm_plans(
    plans: Iterable[OperationPlan],
    confirmation: str,
    approved: bool,
) -> None:
    plan_list = list(plans)
    _print_plan_warnings(plan_list)
    if approved:
        return
    for plan in plan_list:
        click.echo(
            f"Plan: {plan.operation} target={plan.target} pool={plan.pool or '-'} "
            f"size={plan.current_size if plan.current_size is not None else '-'}"
            f"->{plan.target_size if plan.target_size is not None else '-'}",
            err=True,
        )
    response = click.prompt(
        f"Type '{confirmation}' to continue",
        type=str,
        err=True,
        show_default=False,
    )
    if response.strip() != confirmation:
        raise click.Abort()


def print_plan_warnings(plans: Iterable[OperationPlan]) -> None:
    _print_plan_warnings(list(plans))


def print_discovery_warnings(warnings: Iterable[str]) -> None:
    print_warnings(warnings)


def progress(message: str) -> None:
    click.echo(f"Waiting: {message}", err=True)


def health_exit_code(results: Iterable[HealthResult]) -> int:
    statuses = {result.status for result in results}
    if "FAIL" in statuses:
        return OPERATION_ERROR
    if "WARN" in statuses:
        return DEGRADED
    return SUCCESS


def configure_oci_cli_auth(auth: str) -> None:
    if auth in {"instance_principal", "resource_principal"} and not os.environ.get(
        "OCI_CLI_AUTH"
    ):
        os.environ["OCI_CLI_AUTH"] = auth


def _print_plan_warnings(plans: list[OperationPlan]) -> None:
    warnings = list(
        dict.fromkeys(warning for plan in plans for warning in plan.warnings)
    )
    if not warnings:
        return
    click.echo("Warnings", err=True)
    for warning in warnings:
        click.echo(f"- {warning}", err=True)
