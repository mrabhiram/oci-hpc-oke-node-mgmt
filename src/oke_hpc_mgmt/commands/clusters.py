from __future__ import annotations

import click

from oke_hpc_mgmt.commands.pools import (
    add_pool_capacity,
    create_pool,
    delete_pool,
    list_pools,
)


@click.group(
    help=(
        "Slurm-style aliases for OKE worker-pool lifecycle commands. These "
        "commands do not create or delete the OKE control plane."
    )
)
def clusters() -> None:
    pass


@click.group("add", help="Add capacity to an existing OKE worker pool.")
def add_cluster_resource() -> None:
    pass


clusters.add_command(list_pools, "list")
clusters.add_command(create_pool, "create")
clusters.add_command(delete_pool, "delete")
add_cluster_resource.add_command(add_pool_capacity, "node")
clusters.add_command(add_cluster_resource)
