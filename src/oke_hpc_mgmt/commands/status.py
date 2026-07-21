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
from oke_hpc_mgmt.health import evaluate_health
from oke_hpc_mgmt.render import STATUS_COLUMNS, print_records, status_rows


@click.command("status", help="Show concise AI/HPC cluster health and capacity status.")
@output_option
@pass_state
def status(state: CliState, output_override: str | None) -> int:
    snapshot = state.service().discover()
    results = evaluate_health(snapshot)
    print_records(
        status_rows(snapshot, results),
        resolve_output(state, output_override),
        STATUS_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return health_exit_code(results)
