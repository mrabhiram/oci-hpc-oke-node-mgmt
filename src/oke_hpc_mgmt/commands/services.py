from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

import click

from oke_hpc_mgmt.benchmarks.artifacts import EvidenceStore
from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions, validate_run_id
from oke_hpc_mgmt.benchmarks.models import BenchmarkError, BenchmarkParameters, HealthcheckRequest, Network, Scheduler, ServicePolicy
from oke_hpc_mgmt.benchmarks.planning import select_healthcheck_nodes
from oke_hpc_mgmt.benchmarks.records import RunRecord, TERMINAL_CASE_STATES
from oke_hpc_mgmt.benchmarks.service_policy import select_service_plan
from oke_hpc_mgmt.benchmarks.workflow import BenchmarkWorkflow
from oke_hpc_mgmt.commands.benchmarks import (
    RUN_COLUMNS, benchmark_exit_code, case_rows, namespace_option, output_directory_option, run_summary,
)
from oke_hpc_mgmt.commands.common import (
    CliState, confirm_plans, node_selector_options, output_option, pass_state, print_discovery_warnings,
    print_plan_warnings, resolve_output,
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


def healthcheck_service(name: str, category: Literal["active", "multi-node"]) -> click.Command:
    @click.command(name, help="Run one bounded eligibility-based NCCL service pass. No recurring schedule is installed; ineligible nodes are reported, never forced.")
    @node_selector_options
    @click.option("--pool", help="Bound candidates to this worker pool.")
    @click.option("--checks", required=True, type=click.Choice(["nccl"]), help="Explicit diagnostic subset; full host suites are not yet available.")
    @namespace_option
    @click.option("--queue", help="Existing Kueue LocalQueue.")
    @click.option("--scheduler", type=click.Choice(["auto", "direct", "kueue"]), default="auto", show_default=True)
    @click.option("--network", type=click.Choice(["auto", "local", "host", "vf"]), default="auto", show_default=True)
    @click.option("--network-attachment", default="rdma-vf", show_default=True)
    @click.option("--image", help="Reviewed image override; changes invalidate previous freshness.")
    @click.option("--env", "environment", multiple=True, metavar="KEY=VALUE")
    @click.option("--iterations", type=click.IntRange(1, 100000), default=20, show_default=True)
    @click.option("--warmup-iterations", type=click.IntRange(0, 100000), default=5, show_default=True)
    @click.option("--min-bus-bandwidth", type=float)
    @click.option("--interval-hours", type=click.IntRange(1, 8760), default=24, show_default=True)
    @click.option("--retry-seconds", type=click.IntRange(60, 86400), default=600, show_default=True)
    @click.option("--max-cases", type=click.IntRange(1, 32), default=1, show_default=True)
    @click.option("--queue-timeout", type=click.IntRange(1, 86400), default=900, show_default=True)
    @click.option("--startup-timeout", type=click.IntRange(1, 86400), default=600, show_default=True)
    @click.option("--timeout", type=click.IntRange(1, 86400), default=1800, show_default=True)
    @click.option("--wait/--no-wait", default=False)
    @click.option("--observation-timeout", type=click.IntRange(1, 604800), default=7200, show_default=True)
    @output_directory_option
    @click.option("--dry-run", is_flag=True, help="Report every eligibility decision and validate selected manifests without creating a run.")
    @click.option("--yes", is_flag=True)
    @output_option
    @pass_state
    def command(
        state: CliState, node_values: tuple[str, ...], fields: str | None, pool: str | None, checks: str,
        namespace: str, queue: str | None, scheduler: str, network: str, network_attachment: str,
        image: str | None, environment: tuple[str, ...], iterations: int, warmup_iterations: int,
        min_bus_bandwidth: float | None, interval_hours: int, retry_seconds: int, max_cases: int,
        queue_timeout: int, startup_timeout: int, timeout: int, wait: bool, observation_timeout: int,
        output_dir: Path, dry_run: bool, yes: bool, output_override: str | None,
    ) -> int:
        requested = HealthcheckRequest(nodes=node_values, fields=fields, pool=pool, check_type=category, checks=(checks,),
                                       namespace=namespace, queue=queue, scheduler=cast(Scheduler, scheduler), network=cast(Network, network),
                                       image=image, environment=environment, parameters=BenchmarkParameters(iterations=iterations,
                                       warmup_iterations=warmup_iterations, min_bus_bandwidth=min_bus_bandwidth))
        requested.validate()
        policy = ServicePolicy(interval_seconds=interval_hours * 3600, retry_seconds=retry_seconds, max_cases=max_cases)
        policy.validate()
        service = state.service()
        snapshot = service.discover()
        print_discovery_warnings(snapshot.warnings)
        selected = select_healthcheck_nodes(snapshot, requested)
        pools = {node.pool_name for node in selected}
        candidates = tuple(node for node in snapshot.nodes if node.pool_name in pools) if node_values and category == "multi-node" else selected
        backend = BenchmarkBackend(service.kubernetes_backend())
        nodes, pods = backend.read_allocation_inventory(tuple(node.k8s_name for node in candidates))
        selection = select_service_plan(snapshot, requested, policy, nodes, pods, backend.list_runs(None), datetime.now(timezone.utc))
        rows = [{"record_type": "eligibility", **asdict(decision)} for decision in selection.decisions]
        output = resolve_output(state, output_override)
        if selection.plan is None:
            print_records(rows, output, ["node", "state", "reason", "prior_run", "partner"])
            return SUCCESS
        workflow = BenchmarkWorkflow(backend, EvidenceStore(output_dir))
        prepared = workflow.prepare(selection.plan, ExecutionOptions(1, queue_timeout, startup_timeout, timeout, network_attachment))
        operation = OperationPlan("healthcheck-service", prepared.record.run_id, warnings=tuple([
            *selection.plan.warnings, *prepared.capabilities.warnings,
            "Each selected test allocates all GPUs; host/VF RDMA profiles are privileged.",
            "Submission is not a pass. Use health wait to advance pending cases and enforce deadlines.",
        ]))
        if dry_run:
            print_plan_warnings([operation])
            rows.extend({"record_type": "planned-case", **row} for row in case_rows(prepared.record))
            print_records(rows, output, ["record_type", "node", "case", "state", "nodes", "reason"])
            return SUCCESS
        for decision in selection.decisions:
            click.echo(f"{decision.node}: {decision.state}: {decision.reason}", err=True)
        for case in selection.plan.cases:
            click.echo(f"{case.case_id}: {', '.join(node.name for node in case.nodes)}", err=True)
        confirm_plans([operation], prepared.record.run_id, yes)
        run = workflow.submit(prepared)
        click.echo(f"Recorded service run: {run.run_id}; reconnect with health wait {run.run_id}.", err=True)
        if wait:
            run = workflow.wait(namespace, run.run_id, observation_timeout=observation_timeout)
        rows.append({"record_type": "run", **run_summary(run)})
        print_records(rows, output, ["record_type", "node", "run_id", "state", "outcome", "reason"])
        return benchmark_exit_code(run) if wait else SUCCESS

    return command


services.add_command(healthcheck_service("active-hc", "active"))
services.add_command(healthcheck_service("multi-node-hc", "multi-node"))
