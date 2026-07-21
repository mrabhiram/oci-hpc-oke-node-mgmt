from __future__ import annotations

import click

from oke_hpc_mgmt.commands.common import (
    CliState,
    output_option,
    pass_state,
    print_discovery_warnings,
    resolve_output,
)
from oke_hpc_mgmt.render import AUTOSCALER_COLUMNS, autoscaler_rows, print_records


@click.group(help="Inspect Cluster Autoscaler ownership of worker pools.")
def autoscaler() -> None:
    pass


@autoscaler.command("status", help="Show Cluster Autoscaler --nodes bindings.")
@output_option
@pass_state
def autoscaler_status(state: CliState, output_override: str | None) -> int:
    snapshot = state.service(include_pod_counts=False, include_kueue=False).discover()
    print_records(
        autoscaler_rows(snapshot),
        resolve_output(state, output_override),
        AUTOSCALER_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return 0
