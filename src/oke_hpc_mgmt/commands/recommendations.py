from __future__ import annotations

import click

from oke_hpc_mgmt.commands.common import (
    CliState,
    output_option,
    pass_state,
    print_discovery_warnings,
    resolve_output,
)
from oke_hpc_mgmt.health import HEALTH_TYPES, actionable_recommendations, evaluate_health
from oke_hpc_mgmt.render import HEALTH_COLUMNS, health_rows, print_records


@click.group(help="Show actionable findings derived from cluster health checks.")
def recommendations() -> None:
    pass


@recommendations.command("list", help="List warnings and failures with recommended actions.")
@click.option(
    "--type",
    "check_type",
    type=click.Choice(HEALTH_TYPES, case_sensitive=False),
    default="all",
    show_default=True,
    help="Health-check category used to derive recommendations.",
)
@click.option("--pool", help="Only evaluate this worker pool and its nodes.")
@output_option
@pass_state
def list_recommendations(
    state: CliState,
    check_type: str,
    pool: str | None,
    output_override: str | None,
) -> int:
    snapshot = state.service().discover()
    results = actionable_recommendations(
        evaluate_health(snapshot, check_type=check_type, pool_name=pool)
    )
    print_records(
        health_rows(results),
        resolve_output(state, output_override),
        HEALTH_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return 0
