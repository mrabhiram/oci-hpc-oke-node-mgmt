from __future__ import annotations

import os
import sys
import traceback
import warnings


_OCI_STRICT_WARNING_FILTERS = (
    "ignore::FutureWarning:urllib3.poolmanager",
    "ignore:The 'strict' parameter is no longer needed:FutureWarning",
)


def _suppress_oci_cli_strict_warning() -> None:
    existing = [item for item in os.environ.get("PYTHONWARNINGS", "").split(",") if item]
    for warning_filter in _OCI_STRICT_WARNING_FILTERS:
        if warning_filter not in existing:
            existing.append(warning_filter)
    os.environ["PYTHONWARNINGS"] = ",".join(existing)


_suppress_oci_cli_strict_warning()
warnings.filterwarnings(
    "ignore",
    message=".*'strict' parameter is no longer needed.*",
    category=FutureWarning,
)
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"urllib3\.poolmanager",
)

import click  # noqa: E402

from oke_hpc_mgmt.backends.kubernetes import KubernetesDiscoveryError  # noqa: E402
from oke_hpc_mgmt.backends.oci import OciDiscoveryError  # noqa: E402
from oke_hpc_mgmt.commands import cli  # noqa: E402
from oke_hpc_mgmt.commands.common import configure_oci_cli_auth  # noqa: E402
from oke_hpc_mgmt.exit_codes import CANCELLED, NOT_FOUND, OPERATION_ERROR, SUCCESS  # noqa: E402
from oke_hpc_mgmt.selection import SelectionError  # noqa: E402
from oke_hpc_mgmt.workflows.lifecycle import (  # noqa: E402
    WorkflowError,
    WorkflowNotFound,
    readiness_status,
    resource_counts_match,
)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else None
    debug = "--debug" in (arguments if arguments is not None else sys.argv[1:])
    try:
        result = cli.main(
            args=arguments,
            prog_name=_program_name(),
            standalone_mode=False,
        )
        return int(result or SUCCESS)
    except WorkflowNotFound as exc:
        click.echo(f"Error: {exc}", err=True)
        return NOT_FOUND
    except click.Abort:
        click.echo("Operation cancelled.", err=True)
        return CANCELLED
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        return CANCELLED
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except (
        WorkflowError,
        SelectionError,
        OciDiscoveryError,
        KubernetesDiscoveryError,
        TimeoutError,
        ValueError,
    ) as exc:
        click.echo(f"Error: {exc}", err=True)
        return OPERATION_ERROR
    except Exception as exc:
        if debug:
            traceback.print_exc()
        else:
            click.echo(f"Error: {exc}", err=True)
        return OPERATION_ERROR


def _configure_oci_cli_auth(auth: str) -> None:
    """Compatibility wrapper for callers that imported the pre-0.4 helper."""

    configure_oci_cli_auth(auth)


def _program_name() -> str:
    executable = os.path.basename(sys.argv[0])
    if executable == "kubectl-oke":
        return "kubectl oke"
    return "mgmt-oke"


_readiness_status = readiness_status
_resource_counts_match = resource_counts_match
