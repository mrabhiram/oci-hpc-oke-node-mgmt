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
from oke_hpc_mgmt.health import HEALTH_TYPES, evaluate_health
from oke_hpc_mgmt.render import HEALTH_COLUMNS, health_rows, print_records


@click.group(help="Run deterministic AI/HPC readiness checks.")
def health() -> None:
    pass


@health.command("run", help="Check pool, node, GPU, RDMA, add-on, and scheduler health.")
@click.option(
    "--type",
    "check_type",
    type=click.Choice(HEALTH_TYPES, case_sensitive=False),
    default="all",
    show_default=True,
    help="Health-check category.",
)
@click.option("--pool", help="Only evaluate this worker pool and its nodes.")
@output_option
@pass_state
def run_health(
    state: CliState,
    check_type: str,
    pool: str | None,
    output_override: str | None,
) -> int:
    snapshot = state.service().discover()
    results = evaluate_health(snapshot, check_type=check_type, pool_name=pool)
    print_records(
        health_rows(results),
        resolve_output(state, output_override),
        HEALTH_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return health_exit_code(results)
