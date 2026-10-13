from __future__ import annotations

import time
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import click

from oke_hpc_mgmt.benchmarks.artifacts import EvidenceStore
from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.evidence import EvidenceCollector
from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions, validate_run_id
from oke_hpc_mgmt.benchmarks.models import (
    BenchmarkError, BenchmarkParameters, HealthcheckRequest, HealthcheckType, Network, Pairing,
    PairScope, Scheduler, TestMode, parse_bytes,
)
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks, select_healthcheck_nodes
from oke_hpc_mgmt.benchmarks.profiles import PROFILES
from oke_hpc_mgmt.benchmarks.records import RunRecord, TERMINAL_CASE_STATES
from oke_hpc_mgmt.benchmarks.workflow import BenchmarkWorkflow
from oke_hpc_mgmt.commands.common import (
    CliState, CommandFunction, confirm_plans, health_exit_code, node_selector_options, output_option,
    pass_state, print_discovery_warnings, print_plan_warnings, resolve_output,
)
from oke_hpc_mgmt.exit_codes import ACTION_REQUIRED, CANCELLED, DEGRADED, OPERATION_ERROR, SUCCESS
from oke_hpc_mgmt.health import evaluate_health
from oke_hpc_mgmt.models import DiscoverySnapshot, HealthResult, OperationPlan
from oke_hpc_mgmt.render import HEALTH_COLUMNS, health_rows, print_records


CASE_COLUMNS = ["case", "type", "state", "nodes", "ranks", "network", "job", "correctness", "performance", "transport", "coverage"]
RUN_COLUMNS = ["run_id", "namespace", "state", "outcome", "cases", "submitted", "pending", "passed", "failed", "incomplete", "created_at"]


def evidence_directory() -> Path:
    return Path.home() / ".local" / "state" / "mgmt-oke" / "healthchecks"


def output_directory_option(function: CommandFunction) -> CommandFunction:
    return click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=evidence_directory,
                        show_default="~/.local/state/mgmt-oke/healthchecks", help="Directory for private benchmark evidence.")(function)


def _namespace_value(ctx: click.Context, parameter: click.Parameter, value: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value):
        raise click.BadParameter("Must be a Kubernetes namespace DNS label.", ctx, parameter)
    return value


def namespace_option(function: CommandFunction) -> CommandFunction:
    return click.option("--namespace", default="default", show_default=True, callback=_namespace_value,
                        help="Namespace containing the healthcheck run.")(function)


def _backend(state: CliState) -> BenchmarkBackend:
    return BenchmarkBackend(state.service().kubernetes_backend())


def _workflow(state: CliState, output_dir: Path) -> BenchmarkWorkflow:
    return BenchmarkWorkflow(_backend(state), EvidenceStore(output_dir))


def _outcome(run: RunRecord) -> str:
    states = {case.state for case in run.cases}
    if states - TERMINAL_CASE_STATES:
        return "IN_PROGRESS"
    for value in ("FAILED", "INCOMPLETE", "CANCELLED"):
        if value in states:
            return value
    if any(check.status == "FAIL" for check in run.plan.passive_checks):
        return "FAILED"
    if any(check.status == "WARN" for check in run.plan.passive_checks):
        return "INCOMPLETE"
    return "SUCCEEDED"


def benchmark_exit_code(run: RunRecord) -> int:
    return {"IN_PROGRESS": ACTION_REQUIRED, "FAILED": OPERATION_ERROR, "INCOMPLETE": DEGRADED,
            "CANCELLED": CANCELLED, "SUCCEEDED": SUCCESS}[_outcome(run)]


def run_summary(run: RunRecord) -> dict[str, Any]:
    return {"run_id": run.run_id, "namespace": run.plan.request.namespace, "state": run.state, "outcome": _outcome(run),
            "cases": len(run.cases), "submitted": sum(bool(case.job_uid) for case in run.cases),
            "pending": sum(case.state == "PLANNED" for case in run.cases),
            "passed": sum(case.state == "SUCCEEDED" for case in run.cases),
            "failed": sum(case.state == "FAILED" for case in run.cases),
            "incomplete": sum(case.state == "INCOMPLETE" for case in run.cases), "created_at": run.created_at}


def case_rows(run: RunRecord) -> list[dict[str, Any]]:
    rows = []
    for case, saved in zip(run.plan.cases, run.cases):
        result = saved.result or {}
        rows.append({"run_id": run.run_id, "case": case.case_id, "type": case.check_type, "state": saved.state,
                     "nodes": [node.name for node in case.nodes], "node_uids": [node.uid for node in case.nodes],
                     "anchors": case.anchors, "ranks": case.ranks, "network": case.network, "job": saved.job_name,
                     "job_uid": saved.job_uid, "correctness": result.get("correctness", "UNVERIFIED"),
                     "performance": result.get("performance", "UNGRADED"), "transport": result.get("transport", "UNVERIFIED"),
                     "coverage": result.get("coverage", "UNVERIFIED"), "result": saved.result,
                     "error": saved.error, "evidence_sha256": saved.evidence_sha256, "snapshots": [asdict(item) for item in saved.snapshots]})
    return rows


def passive_results(snapshot: DiscoverySnapshot, request: HealthcheckRequest) -> list[HealthResult]:
    selected = select_healthcheck_nodes(snapshot, request)
    filtered = replace(snapshot, nodes=list(selected), pools=[pool for pool in snapshot.pools if any(
        node.pool_name == pool.name for node in selected
    )])
    results = [item for category in ("node", "gpu", "rdma") for item in evaluate_health(filtered, check_type=category)]
    results.append(HealthResult("passive-host-coverage", "selected-nodes", "WARN",
                                "API readiness only: host setup and GPU/RDMA diagnostics have not been executed."))
    return results


def _print_run(run: RunRecord, output: str, *, detailed: bool = False) -> None:
    if detailed and output == "json":
        print_records([{**asdict(run), "outcome": _outcome(run)}], output)
    elif detailed:
        _print_results(run, output)
    else:
        print_records([run_summary(run)], output, RUN_COLUMNS)


def _print_results(run: RunRecord, output: str) -> None:
    if output == "table":
        print_records(case_rows(run), output, CASE_COLUMNS)
        if run.plan.passive_checks:
            print_records(health_rows(list(run.plan.passive_checks)), output, HEALTH_COLUMNS)
        return
    rows = [{"record_type": "nccl", **row} for row in case_rows(run)]
    rows.extend({"record_type": "passive", "run_id": run.run_id, **asdict(check)} for check in run.plan.passive_checks)
    print_records(rows, output, None if output == "json" else ["record_type", *CASE_COLUMNS, *HEALTH_COLUMNS])


@click.command("healthchecks", help="Run selected node healthchecks; passive API checks are the default. Active suites currently require --checks nccl.")
@node_selector_options
@click.option("--pool", help="Select every current node in this worker pool.")
@click.option("--type", "check_type", type=click.Choice(["passive", "active", "multi-node", "all"]), default="passive", show_default=True)
@click.option("--checks", help="Comma-separated diagnostic components. Currently only an explicit nccl subset is executable.")
@click.option("--mode", type=click.Choice(["pairs", "all-nodes", "per-local-block"]), help="Multi-node grouping; pairs is the default.")
@click.option("--exclude-node", help="Exclude this partner, not a selected anchor, from pairwise testing.")
@click.option("--local-block", help="Select one unambiguous local block within its parent topology.")
@click.option("--pairing", type=click.Choice(["anchor", "adjacent", "reference", "all"]), default="anchor", show_default=True)
@click.option("--pair-scope", type=click.Choice(["any", "same-local-block", "cross-local-block"]), default="any", show_default=True)
@click.option("--reference-node", help="Required partner with --pairing reference.")
@click.option("--reservation", help="Real Slurm reservation; requires the Slurm adapter (not yet available).")
@click.option("--network", type=click.Choice(["auto", "local", "host", "vf"]), default="auto", show_default=True)
@click.option("--scheduler", type=click.Choice(["auto", "direct", "kueue", "slurm"]), default="auto", show_default=True)
@namespace_option
@click.option("--queue", help="Existing Kueue LocalQueue in the selected namespace.")
@click.option("--network-attachment", default="rdma-vf", show_default=True, help="Existing SR-IOV network attachment for VF tests.")
@click.option("--image", help="Override the reviewed container image; record the actual resolved digest.")
@click.option("--env", "environment", multiple=True, metavar="KEY=VALUE", help="Validated NCCL_/UCX_/HCOLL_ environment override.")
@click.option("--min-bytes", default="4G", show_default=True)
@click.option("--max-bytes", default="8G", show_default=True)
@click.option("--factor", type=click.IntRange(2, 1024), default=2, show_default=True)
@click.option("--iterations", type=click.IntRange(1, 100000), default=20, show_default=True)
@click.option("--warmup-iterations", type=click.IntRange(0, 100000), default=5, show_default=True)
@click.option("--repetitions", type=click.IntRange(1, 100), default=1, show_default=True)
@click.option("--min-bus-bandwidth", type=float, help="Explicit minimum GB/s for both 8 GiB bus-bandwidth measurements.")
@click.option("--concurrency", type=click.IntRange(1, 32), default=1, show_default=True)
@click.option("--queue-timeout", type=click.IntRange(1, 86400), default=900, show_default=True, help="Queue deadline in seconds while a caller advances the run.")
@click.option("--startup-timeout", type=click.IntRange(1, 86400), default=600, show_default=True)
@click.option("--timeout", type=click.IntRange(1, 86400), default=1800, show_default=True, help="Per-case execution deadline, in seconds.")
@click.option("--wait/--no-wait", default=False, help="Advance pending cases and wait for results; otherwise submit the initial batch only.")
@click.option("--observation-timeout", type=click.IntRange(1, 604800), default=7200, show_default=True, help="Maximum seconds this invocation waits; expiration leaves the run resumable.")
@click.option("--poll-interval", type=click.IntRange(1, 300), default=10, show_default=True)
@click.option("--keep-resources", is_flag=True, help="Retain owned MPIJobs/Pods; overlapping later cases wait until these are released.")
@output_directory_option
@click.option("--dry-run", is_flag=True, help="Validate API resources and server admission without persisting workloads.")
@click.option("--yes", is_flag=True, help="Approve this selected workload submission without typed confirmation.")
@output_option
@pass_state
def node_healthchecks(
    state: CliState, node_values: tuple[str, ...], fields: str | None, pool: str | None, check_type: str,
    checks: str | None, mode: str | None, exclude_node: str | None, local_block: str | None,
    pairing: str, pair_scope: str, reference_node: str | None, reservation: str | None, network: str,
    scheduler: str, namespace: str, queue: str | None, network_attachment: str, image: str | None,
    environment: tuple[str, ...], min_bytes: str, max_bytes: str, factor: int, iterations: int,
    warmup_iterations: int, repetitions: int, min_bus_bandwidth: float | None, concurrency: int,
    queue_timeout: int, startup_timeout: int, timeout: int, wait: bool, observation_timeout: int,
    poll_interval: int, keep_resources: bool, output_dir: Path, dry_run: bool, yes: bool,
    output_override: str | None,
) -> int:
    request = HealthcheckRequest(
        nodes=node_values, fields=fields, pool=pool, check_type=cast(HealthcheckType, check_type),
        checks=tuple(part.strip() for part in checks.split(",")) if checks else (), mode=cast("TestMode | None", mode),
        exclude_node=exclude_node, local_block=local_block, pairing=cast(Pairing, pairing),
        pair_scope=cast(PairScope, pair_scope), reference_node=reference_node, reservation=reservation,
        network=cast(Network, network), scheduler=cast(Scheduler, scheduler), namespace=namespace,
        queue=queue, image=image, environment=environment,
        parameters=BenchmarkParameters(parse_bytes(min_bytes), parse_bytes(max_bytes), factor, iterations,
                                       warmup_iterations, repetitions, min_bus_bandwidth),
    )
    request.validate()
    service = state.service()
    snapshot = service.discover()
    print_discovery_warnings(snapshot.warnings)
    output = resolve_output(state, output_override)
    if check_type == "passive":
        if wait or keep_resources:
            raise BenchmarkError("Passive checks do not submit workloads; --wait and --keep-resources do not apply.")
        results = passive_results(snapshot, request)
        print_records(health_rows(results), output, HEALTH_COLUMNS)
        return health_exit_code(results)
    plan = plan_healthchecks(snapshot, request)
    if check_type == "all":
        plan = replace(plan, passive_checks=tuple(passive_results(snapshot, replace(request, check_type="passive", checks=(),
                                                                                  mode=None, exclude_node=None, pairing="anchor",
                                                                                  pair_scope="any", reference_node=None,
                                                                                  network="auto", image=None, environment=()))))
    workflow = BenchmarkWorkflow(BenchmarkBackend(service.kubernetes_backend()), EvidenceStore(output_dir))
    options = ExecutionOptions(concurrency, queue_timeout, startup_timeout, timeout, network_attachment)
    prepared = workflow.prepare(plan, options)
    warnings = [*plan.warnings, *prepared.capabilities.warnings,
                "Active NCCL tests allocate every GPU on each displayed participant; host/VF RDMA workers are privileged.",
                "Submission is not a passed benchmark. Pending cases and queue deadlines need an active health wait caller."]
    if check_type == "all":
        warnings.append("The passive stage currently covers API readiness only; this is an NCCL subset, not full Slurm diagnostic parity.")
    operation = OperationPlan("healthcheck-submit", prepared.record.run_id, warnings=tuple(warnings))
    if dry_run:
        print_plan_warnings([operation])
        if output == "json":
            print_records([{"dry_run": True, "plan": asdict(plan), "options": asdict(prepared.record.options),
                            "capabilities": asdict(prepared.capabilities), "cases": case_rows(prepared.record)}], output)
        else:
            print_records(case_rows(prepared.record), output, CASE_COLUMNS)
        return SUCCESS
    for case in plan.cases:
        click.echo(f"{case.case_id} ({case.check_type}, {case.ranks} ranks): {', '.join(node.name for node in case.nodes)}", err=True)
    confirm_plans([operation], prepared.record.run_id, yes)
    run = workflow.submit(prepared)
    click.echo(f"Recorded run: {run.run_id}; evidence directory: {output_dir.expanduser().absolute()}", err=True)
    if wait:
        run = workflow.wait(namespace, run.run_id, poll_interval=poll_interval,
                            observation_timeout=observation_timeout, keep_resources=keep_resources)
    else:
        click.echo(f"Use mgmt-oke health wait {run.run_id} --namespace {namespace} with the same --output-dir to advance remaining cases.", err=True)
    _print_run(run, output, detailed=wait)
    return benchmark_exit_code(run) if wait else SUCCESS


@click.command("profiles", help="List reviewed NCCL execution profiles; listed hardware is not a live validation claim.")
@output_option
@pass_state
def health_profiles(state: CliState, output_override: str | None) -> int:
    rows = [{**asdict(profile), "networks": list(profile.networks),
             "vf_count": profile.vf_count} for profile in PROFILES]
    print_records(rows, resolve_output(state, output_override), ["shape", "gpu_count", "networks", "max_bytes", "source_revision"])
    return SUCCESS


@click.command("list", help="List recorded healthcheck runs without advancing or submitting workloads.")
@namespace_option
@click.option("--all-namespaces", is_flag=True)
@click.option("--pool", help="Filter by recorded participant pool name.")
@output_option
@pass_state
def health_list(state: CliState, namespace: str, all_namespaces: bool, pool: str | None, output_override: str | None) -> int:
    runs = _backend(state).list_runs(None if all_namespaces else namespace)
    rows = [run_summary(run) for run in runs if not pool or any(node.pool == pool for case in run.plan.cases for node in case.nodes)]
    print_records(rows, resolve_output(state, output_override), RUN_COLUMNS)
    return SUCCESS


@click.command("get", help="Show the recorded immutable request, cases, and observation history.")
@click.argument("run_id")
@namespace_option
@output_option
@pass_state
def health_get(state: CliState, run_id: str, namespace: str, output_override: str | None) -> int:
    validate_run_id(run_id)
    _print_run(_backend(state).get_run(namespace, run_id), resolve_output(state, output_override), detailed=True)
    return SUCCESS


@click.command("results", help="Read recorded benchmark verdicts; does not execute tests or refresh checkpoints.")
@click.argument("run_id")
@namespace_option
@output_option
@pass_state
def health_results(state: CliState, run_id: str, namespace: str, output_override: str | None) -> int:
    validate_run_id(run_id)
    run = _backend(state).get_run(namespace, run_id)
    _print_results(run, resolve_output(state, output_override))
    return benchmark_exit_code(run)


@click.command("wait", help="Advance an existing run, collect evidence, submit its pending cases, and wait for results.")
@click.argument("run_id")
@namespace_option
@output_directory_option
@click.option("--poll-interval", type=click.IntRange(1, 300), default=10, show_default=True)
@click.option("--observation-timeout", type=click.IntRange(1, 604800), default=7200, show_default=True)
@click.option("--keep-resources", is_flag=True)
@output_option
@pass_state
def health_wait(state: CliState, run_id: str, namespace: str, output_dir: Path, poll_interval: int,
                observation_timeout: int, keep_resources: bool, output_override: str | None) -> int:
    validate_run_id(run_id)
    run = _workflow(state, output_dir).wait(namespace, run_id, poll_interval=poll_interval,
                                           observation_timeout=observation_timeout, keep_resources=keep_resources)
    _print_run(run, resolve_output(state, output_override), detailed=True)
    return benchmark_exit_code(run)


def _lifecycle(
    state: CliState, run_id: str, namespace: str, output_dir: Path, yes: bool, dry_run: bool,
    output_override: str | None, *, cleanup: bool,
) -> int:
    validate_run_id(run_id)
    workflow = _workflow(state, output_dir)
    run = workflow.backend.get_run(namespace, run_id)
    operation = "healthcheck-cleanup" if cleanup else "healthcheck-cancel"
    if cleanup and any(case.state not in TERMINAL_CASE_STATES for case in run.cases):
        raise BenchmarkError("Cancel or complete every case before cleanup.")
    if dry_run:
        _print_run(run, resolve_output(state, output_override), detailed=True)
        return SUCCESS
    confirm_plans([OperationPlan(operation, run_id, warnings=(
        "Only this run's owned resources are affected; readable exported evidence is required before destructive actions.",
    ))], run_id, yes)
    updated = workflow.cleanup(namespace, run_id) if cleanup else workflow.cancel(namespace, run_id)
    _print_run(updated, resolve_output(state, output_override))
    pending = updated.state != "CLEANED" if cleanup else updated.state == "CANCELLING"
    if pending:
        click.echo(f"{operation} is not yet converged; repeat this command to verify removal.", err=True)
        return ACTION_REQUIRED
    return SUCCESS


@click.command("cancel", help="Export current evidence and cancel only this run's owned workloads.")
@click.argument("run_id")
@namespace_option
@output_directory_option
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@output_option
@pass_state
def health_cancel(state: CliState, run_id: str, namespace: str, output_dir: Path, yes: bool,
                  dry_run: bool, output_override: str | None) -> int:
    return _lifecycle(state, run_id, namespace, output_dir, yes, dry_run, output_override, cleanup=False)


@click.command("cleanup", help="Verify evidence and remove only terminal runs' owned workloads; retain their checkpoints.")
@click.argument("run_id")
@namespace_option
@output_directory_option
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@output_option
@pass_state
def health_cleanup(state: CliState, run_id: str, namespace: str, output_dir: Path, yes: bool,
                   dry_run: bool, output_override: str | None) -> int:
    return _lifecycle(state, run_id, namespace, output_dir, yes, dry_run, output_override, cleanup=True)


def log_rows(backend: BenchmarkBackend, store: EvidenceStore, run: RunRecord, case_id: str | None, role: str) -> tuple[list[dict[str, Any]], bool]:
    cases = [case for case in run.plan.cases if not case_id or case.case_id == case_id]
    if not cases:
        raise BenchmarkError(f"Unknown case: {case_id}.")
    rows: list[dict[str, Any]] = []
    finished = True
    collector = EvidenceCollector(backend)
    for case in cases:
        saved = next(item for item in run.cases if item.case_id == case.case_id)
        digest: str | None = None
        pods: list[dict[str, Any]] = []
        if saved.job_uid and backend.get_job(run, case) is not None:
            evidence = collector.collect(run, case)
            finished = finished and (saved.state in TERMINAL_CASE_STATES or (
                (evidence.succeeded or evidence.failed) and not evidence.suspended
            ))
            pods = [asdict(pod) for pod in evidence.pods]
        else:
            finished = finished and saved.state in TERMINAL_CASE_STATES
        if not pods:
            digests = list(dict.fromkeys([snapshot.sha256 for snapshot in saved.snapshots] + (
                [saved.evidence_sha256] if saved.evidence_sha256 else []
            )))
            for digest in reversed(digests):
                payload = store.verify(run, case, digest)
                pods = payload["evidence"]["pods"]
                if any(pod["role"] == role or role == "all" for pod in pods):
                    break
        for pod in pods:
            if role == "all" or pod["role"] == role:
                rows.append({"run_id": run.run_id, "case": case.case_id, "pod": pod["name"], "pod_uid": pod["uid"],
                             "container_id": pod["container_id"], "restart_count": pod["restart_count"],
                             "role": pod["role"], "log": pod["log"], "complete": pod["log_complete"], "warnings": pod["warnings"],
                             "evidence_sha256": digest, "source": "export" if digest else "kubernetes"})
    return rows, finished


def _terminal_text(value: str) -> str:
    return "".join(character if character in "\n\t" or character.isprintable() else repr(character)[1:-1] for character in value)


@click.command("logs", help="Read owned workload logs or verified exports; following does not advance the run.")
@click.argument("run_id")
@namespace_option
@output_directory_option
@click.option("--case", "case_id", help="One recorded case ID.")
@click.option("--role", type=click.Choice(["launcher", "worker", "all"]), default="launcher", show_default=True)
@click.option("--follow", is_flag=True, help="Poll submitted workload logs; use health wait to submit pending cases.")
@click.option("--poll-interval", type=click.IntRange(1, 300), default=10, show_default=True)
@click.option("--observation-timeout", type=click.IntRange(1, 604800), default=600, show_default=True)
@output_option
@pass_state
def health_logs(state: CliState, run_id: str, namespace: str, output_dir: Path, case_id: str | None,
                role: str, follow: bool, poll_interval: int, observation_timeout: int, output_override: str | None) -> int:
    validate_run_id(run_id)
    output = resolve_output(state, output_override)
    if follow and output != "table":
        raise BenchmarkError("--follow emits text; use --format table, or omit --follow for JSON/CSV snapshots.")
    backend, store = _backend(state), EvidenceStore(output_dir)
    previous: dict[tuple[str, str, str | None, int], str] = {}
    deadline = time.monotonic() + observation_timeout
    while True:
        run = backend.get_run(namespace, run_id)
        rows, finished = log_rows(backend, store, run, case_id, role)
        if not rows:
            click.echo("No logs are available for the selected cases yet.", err=True)
        if output != "table":
            print_records(rows, output)
        else:
            for row in rows:
                identity = (row["case"], row["pod_uid"], row["container_id"], row["restart_count"])
                old, text = previous.get(identity, ""), row["log"]
                if text != old or identity not in previous:
                    click.echo(f"=== {row['case']} {row['role']} {row['pod']} uid={row['pod_uid']} ===")
                    if old and not text.startswith(old):
                        click.echo("[log changed or rotated; fresh snapshot follows]")
                    click.echo(_terminal_text(text[len(old):] if text.startswith(old) else text), nl=False)
                    if text and not text.endswith("\n"):
                        click.echo()
                previous[identity] = text
                print_discovery_warnings(row["warnings"])
        if not follow or finished:
            return SUCCESS if rows and all(row["complete"] for row in rows) else DEGRADED
        if time.monotonic() >= deadline:
            click.echo("Log observation timed out; no workloads were submitted, restarted, or cancelled.", err=True)
            return ACTION_REQUIRED
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


HEALTH_COMMANDS = (health_profiles, health_list, health_get, health_results, health_wait, health_logs, health_cancel, health_cleanup)
