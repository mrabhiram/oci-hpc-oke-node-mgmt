from __future__ import annotations

import os

import click

from oke_hpc_mgmt import __version__
from oke_hpc_mgmt.commands.common import (
    AUTH_METHODS,
    OUTPUT_FORMATS,
    CliState,
    configure_oci_cli_auth,
)


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


@click.group(
    context_settings=CONTEXT_SETTINGS,
    invoke_without_command=True,
    help="Management CLI for OCI HPC OKE worker pools, nodes, accelerators, and topology.",
)
@click.version_option(version=__version__, prog_name="mgmt-oke")
@click.option(
    "--compartment-id",
    default=lambda: os.getenv("OCI_COMPARTMENT_ID"),
    help="OCI compartment OCID override. By default it is resolved from the OKE cluster.",
)
@click.option(
    "--cluster-id",
    default=lambda: os.getenv("OKE_CLUSTER_ID"),
    help="OKE cluster OCID override. By default it is read from kubeconfig.",
)
@click.option(
    "--region",
    default=lambda: os.getenv("OCI_REGION"),
    help="OCI region override. By default it is read from kubeconfig.",
)
@click.option(
    "--auth",
    type=click.Choice(AUTH_METHODS, case_sensitive=False),
    default=lambda: os.getenv("OCI_AUTH", "config_file"),
    show_default="config_file",
    help="OCI API authentication method. 'none' disables OCI API calls.",
)
@click.option("--oci-config-file", default=lambda: os.getenv("OCI_CONFIG_FILE"))
@click.option("--oci-profile", default=lambda: os.getenv("OCI_PROFILE"))
@click.option(
    "--kubeconfig",
    default=lambda: os.getenv("KUBECONFIG"),
    help="Kubeconfig used for Kubernetes access and automatic OKE target discovery.",
)
@click.option(
    "--context",
    help="Explicit kubeconfig context override. The current or only context is used by default.",
)
@click.option(
    "--in-cluster",
    is_flag=True,
    help="Use in-cluster Kubernetes configuration; cluster and region overrides are then required.",
)
@click.option(
    "--skip-oci",
    is_flag=True,
    help="Skip OCI inventory and disable OCI-backed mutations.",
)
@click.option(
    "--skip-kubernetes",
    is_flag=True,
    help="Skip Kubernetes node, workload, topology, autoscaler, and Kueue discovery.",
)
@click.option(
    "--format",
    "--output",
    "output",
    type=click.Choice(OUTPUT_FORMATS, case_sensitive=False),
    default="table",
    show_default=True,
    help="Default output format.",
)
@click.option("--debug", is_flag=True, help="Show exception tracebacks for troubleshooting.")
@click.pass_context
def cli(
    ctx: click.Context,
    compartment_id: str | None,
    cluster_id: str | None,
    region: str | None,
    auth: str,
    oci_config_file: str | None,
    oci_profile: str | None,
    kubeconfig: str | None,
    context: str | None,
    in_cluster: bool,
    skip_oci: bool,
    skip_kubernetes: bool,
    output: str,
    debug: bool,
) -> None:
    """Manage OCI HPC OKE infrastructure from the stack operator node."""

    configure_oci_cli_auth(auth)
    ctx.obj = CliState(
        compartment_id=compartment_id,
        cluster_id=cluster_id,
        region=region,
        auth=auth,
        oci_config_file=oci_config_file,
        oci_profile=oci_profile,
        kubeconfig=kubeconfig,
        context=context,
        in_cluster=in_cluster,
        skip_oci=skip_oci,
        skip_kubernetes=skip_kubernetes,
        output=output,
        debug=debug,
    )
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


from oke_hpc_mgmt.commands.addons import addons  # noqa: E402
from oke_hpc_mgmt.commands.autoscaler import autoscaler  # noqa: E402
from oke_hpc_mgmt.commands.health import health  # noqa: E402
from oke_hpc_mgmt.commands.nodes import nodes  # noqa: E402
from oke_hpc_mgmt.commands.pools import pools  # noqa: E402
from oke_hpc_mgmt.commands.recommendations import recommendations  # noqa: E402
from oke_hpc_mgmt.commands.reconcile import reconcile  # noqa: E402
from oke_hpc_mgmt.commands.status import status  # noqa: E402
from oke_hpc_mgmt.commands.topology import topology  # noqa: E402


cli.add_command(status)
cli.add_command(pools)
cli.add_command(nodes)
cli.add_command(topology)
cli.add_command(autoscaler)
cli.add_command(addons)
cli.add_command(health)
cli.add_command(recommendations)
cli.add_command(reconcile)
