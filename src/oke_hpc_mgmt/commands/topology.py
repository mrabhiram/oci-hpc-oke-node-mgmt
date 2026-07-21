from __future__ import annotations

import click

from oke_hpc_mgmt.commands.common import (
    CliState,
    output_option,
    pass_state,
    print_discovery_warnings,
    resolve_output,
)
from oke_hpc_mgmt.render import TOPOLOGY_COLUMNS, print_records, topology_rows


@click.group(help="Inspect OCI RDMA placement topology.")
def topology() -> None:
    pass


@topology.command("list", help="Group workers by HPC Island, Network Block, and Local Block.")
@click.option("--pool", help="Only show topology for this worker pool.")
@output_option
@pass_state
def list_topology(
    state: CliState,
    pool: str | None,
    output_override: str | None,
) -> int:
    snapshot = state.service(
        include_pod_counts=False,
        include_autoscaler=False,
        include_kueue=False,
    ).discover()
    selected = [node for node in snapshot.nodes if not pool or node.pool_name == pool]
    print_records(
        topology_rows(selected),
        resolve_output(state, output_override),
        TOPOLOGY_COLUMNS,
    )
    print_discovery_warnings(snapshot.warnings)
    return 0
