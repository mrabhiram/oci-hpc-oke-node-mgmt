from __future__ import annotations

import click

from oke_hpc_mgmt.commands.common import CliState, output_option, pass_state, resolve_output
from oke_hpc_mgmt.render import print_snapshot


@click.command("reconcile", help="Run full OCI and Kubernetes discovery and print one snapshot.")
@output_option
@pass_state
def reconcile(state: CliState, output_override: str | None) -> int:
    snapshot = state.service().discover()
    print_snapshot(snapshot, resolve_output(state, output_override))
    return 0
