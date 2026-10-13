from __future__ import annotations

from pathlib import Path

import click

from oke_hpc_mgmt.benchmarks.artifacts import EvidenceStore
from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.manifests import validate_run_id
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.records import RunRecord, TERMINAL_CASE_STATES
from oke_hpc_mgmt.benchmarks.workflow import BenchmarkWorkflow
from oke_hpc_mgmt.commands.benchmarks import (
    RUN_COLUMNS, namespace_option, output_directory_option, run_summary,
)
from oke_hpc_mgmt.commands.common import (
    CliState, confirm_plans, output_option, pass_state, resolve_output,
)
from oke_hpc_mgmt.exit_codes import SUCCESS
from oke_hpc_mgmt.models import OperationPlan
from oke_hpc_mgmt.render import print_records


@click.group(help="Run explicitly requested healthcheck service passes; no recurring schedule is installed.")
def services() -> None:
    pass


def select_refresh_runs(
    backend: BenchmarkBackend, namespace: str, run_ids: tuple[str, ...], pool: str | None, max_runs: int,
) -> tuple[RunRecord, ...]:
    if type(max_runs) is not int or not 1 <= max_runs <= 128:
        raise BenchmarkError("Metadata refresh max-runs must be an integer in 1..128.")
    if run_ids and pool is not None:
        raise BenchmarkError("Choose --run-id or --pool, not both.")
    if len(run_ids) != len(set(run_ids)) or len(run_ids) > max_runs:
        raise BenchmarkError("Run IDs must be unique and within --max-runs.")
    for run_id in run_ids:
        validate_run_id(run_id)
    if run_ids:
        selected = tuple(backend.get_run(namespace, run_id) for run_id in run_ids)
    else:
        selected = tuple(run for run in backend.list_runs(namespace) if run.state != "CANCELLING" and any(
            case.state not in TERMINAL_CASE_STATES for case in run.cases
        ) and (pool is None or any(node.pool == pool for case in run.plan.cases for node in case.nodes)))
    if len(selected) > max_runs:
        raise BenchmarkError("Too many matching runs; narrow --pool/--run-id or explicitly increase --max-runs.")
    if any(not run.uid or run.plan.request.namespace != namespace for run in selected):
        raise BenchmarkError("Selected runs lack verified checkpoint identity or namespace.")
    return tuple(sorted(selected, key=lambda run: (run.created_at, run.run_id)))


@services.command("update-metadata", help="Observe recorded healthchecks once. Never submit, cancel, or delete workloads; exit 0 means refresh completed, not benchmark success.")
@namespace_option
@click.option("--run-id", "run_ids", multiple=True, help="Refresh these recorded runs only; repeat for multiple IDs.")
@click.option("--pool", help="Restrict nonterminal recorded runs to this pool.")
@click.option("--max-runs", type=click.IntRange(1, 128), default=32, show_default=True, help="Refuse a broader pass instead of truncating it.")
@output_directory_option
@click.option("--dry-run", is_flag=True, help="List selected checkpoints without exporting evidence or writing metadata.")
@click.option("--yes", is_flag=True, help="Confirm checkpoint/evidence updates without a prompt.")
@output_option
@pass_state
def update_metadata(
    state: CliState, namespace: str, run_ids: tuple[str, ...], pool: str | None, max_runs: int,
    output_dir: Path, dry_run: bool, yes: bool, output_override: str | None,
) -> int:
    backend = BenchmarkBackend(state.service().kubernetes_backend())
    runs = select_refresh_runs(backend, namespace, run_ids, pool, max_runs)
    if runs and not dry_run:
        confirm_plans([OperationPlan("healthcheck-metadata", run.run_id) for run in runs], namespace, yes)
        workflow = BenchmarkWorkflow(backend, EvidenceStore(output_dir))
        runs = tuple(workflow.refresh(namespace, run.run_id, expected_uid=run.uid or "") for run in runs)
    print_records([run_summary(run) for run in runs], resolve_output(state, output_override), RUN_COLUMNS)
    return SUCCESS
