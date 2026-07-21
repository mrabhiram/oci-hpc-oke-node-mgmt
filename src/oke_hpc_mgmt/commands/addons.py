from __future__ import annotations

import click

from oke_hpc_mgmt.commands.common import (
    CliState,
    health_exit_code,
    output_option,
    pass_state,
    print_discovery_warnings,
    resolve_output,
)
from oke_hpc_mgmt.health import addon_validation_results
from oke_hpc_mgmt.render import ADDON_COLUMNS, HEALTH_COLUMNS, addon_rows, health_rows, print_records
from oke_hpc_mgmt.workflows.lifecycle import WorkflowError


@click.group(help="Inspect and validate OKE accelerator add-ons.")
def addons() -> None:
    pass


@addons.command("status", help="Show OKE add-on lifecycle and installed versions.")
@output_option
@pass_state
def addon_status(state: CliState, output_override: str | None) -> int:
    if state.auth == "none" or state.skip_oci:
        raise WorkflowError("OKE add-on discovery requires OCI authentication.")
    service = state.service(
        include_pod_counts=False,
        include_autoscaler=False,
        include_kueue=False,
        include_pools=False,
        skip_kubernetes=True,
    )
    service.resolve_oci_target(require_cluster=True)
    snapshot = service.discover()
    print_records(
        addon_rows(snapshot.addons),
        resolve_output(state, output_override),
        ADDON_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return 0


@addons.command("validate", help="Validate GPU and RDMA add-ons against discovered worker capacity.")
@click.option(
    "--target",
    type=click.Choice(("all", "gpu", "rdma"), case_sensitive=False),
    default="all",
    show_default=True,
    help="Accelerator stack to validate.",
)
@click.option("--pool", help="Only validate this worker pool and its nodes.")
@output_option
@pass_state
def validate_addons(
    state: CliState,
    target: str,
    pool: str | None,
    output_override: str | None,
) -> int:
    snapshot = state.service().discover()
    results = addon_validation_results(snapshot, target=target, pool_name=pool)
    print_records(
        health_rows(results),
        resolve_output(state, output_override),
        HEALTH_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return health_exit_code(results)
