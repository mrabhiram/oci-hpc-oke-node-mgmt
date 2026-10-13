from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Callable, cast

from oke_hpc_mgmt.benchmarks.artifacts import EvidenceStore
from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.capabilities import CapabilityEvidence, CapabilityPreflight
from oke_hpc_mgmt.benchmarks.evidence import CaseEvidence, EvidenceCollector, assess_case
from oke_hpc_mgmt.benchmarks.lease import SubmissionLease
from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError, HealthcheckPlan
from oke_hpc_mgmt.benchmarks.records import (
    MAX_SNAPSHOTS, TERMINAL_CASE_STATES, CaseRecord, CaseState, EvidenceSnapshot, RunRecord, SnapshotPhase,
    assert_no_overlap, mark_cancelling, next_cases, timestamp,
)
from oke_hpc_mgmt.benchmarks.service_policy import validate_service_case


@dataclass(frozen=True)
class PreparedRun:
    record: RunRecord
    capabilities: CapabilityEvidence


def _saved(run: RunRecord, case: BenchmarkCase) -> CaseRecord:
    return next(item for item in run.cases if item.case_id == case.case_id)


def _phase(evidence: CaseEvidence) -> SnapshotPhase:
    if evidence.suspended:
        return "QUEUED"
    if evidence.succeeded or evidence.failed:
        return "TERMINAL"
    if any(pod.role == "launcher" and pod.container_id for pod in evidence.pods):
        return "RUNNING"
    return "STARTING"


def _log_loss_risk(evidence: CaseEvidence) -> bool:
    return any(pod.container_id and not pod.log_complete for pod in evidence.pods)


def deadline_reason(run: RunRecord, case: BenchmarkCase, now: datetime) -> str | None:
    """Deadlines never reset on requeue, reconnect, or Pod replacement."""
    saved = _saved(run, case)
    created = timestamp(saved.submitted_at or saved.created_at or run.created_at)
    if saved.admitted_at is None:
        deadline, duration, kind = created, run.options.queue_timeout, "queue"
    elif saved.started_at is None:
        deadline, duration, kind = timestamp(saved.admitted_at), run.options.startup_timeout, "startup"
    else:
        deadline, duration, kind = timestamp(saved.started_at), run.options.timeout, "execution"
    if now.utcoffset() is None or now < created:
        raise BenchmarkError("Clock precedes the recorded submission or lacks timezone; deadline is unverified.")
    total = run.options.queue_timeout + run.options.startup_timeout + run.options.timeout
    if (now - created).total_seconds() >= total:
        return f"{case.case_id} exceeded its total queue/startup/execution deadline ({total}s)."
    if (now - deadline).total_seconds() >= duration:
        return f"{case.case_id} exceeded its {kind} deadline ({duration}s)."
    return None


class BenchmarkWorkflow:
    """One bounded pass per call; wait drives passes without holding a Lease."""

    def __init__(
        self, backend: BenchmarkBackend, store: EvidenceStore, *,
        capabilities: CapabilityPreflight | None = None, collector: EvidenceCollector | None = None,
        lease_factory: Callable[[], SubmissionLease] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.backend, self.store = backend, store
        self.capabilities = capabilities or CapabilityPreflight(backend)
        self.collector = collector or EvidenceCollector(backend)
        self.lease_factory = lease_factory or (lambda: SubmissionLease(backend))
        self.now = now or (lambda: datetime.now(timezone.utc))

    def prepare(self, plan: HealthcheckPlan, options: ExecutionOptions) -> PreparedRun:
        plan.request.validate()
        options.validate()
        assert_no_overlap(plan, self.backend.list_runs(None))
        for case in plan.cases:
            self.backend.runtime_preflight(case)
        self._service_preflight(plan)
        capability = self.capabilities.check(plan, options)
        run = RunRecord.create("hc-" + secrets.token_hex(10), plan, capability.execution_options)
        return PreparedRun(run, capability)

    def submit(self, prepared: PreparedRun) -> RunRecord:
        run = prepared.record
        with self.lease_factory() as lease:
            assert_no_overlap(run.plan, self.backend.list_runs(None))
            current = self.capabilities.check(run.plan, run.options)
            if current.namespace_uid != prepared.capabilities.namespace_uid or current.execution_options != run.options:
                raise BenchmarkError("Namespace or placement changed after planning; prepare and confirm a new run.")
            for case in run.plan.cases:
                lease.renew()
                self.backend.runtime_preflight(case)
            self._service_preflight(run.plan)
            lease.renew()
            run = self.backend.create_run(run)
        # Checkpoints reserve every planned participant. A failure after this
        # point is recovered by advance(run_id), never by another submit call.
        try:
            return self.advance(run.plan.request.namespace, run.run_id)
        except BenchmarkError as exc:
            raise BenchmarkError(f"Run {run.plan.request.namespace}/{run.run_id} is recorded; resume this run. {exc}") from exc

    def _revalidate(self, run: RunRecord, case: BenchmarkCase, lease: SubmissionLease) -> None:
        lease.renew()
        options = replace(run.options, placements=tuple(item for item in run.options.placements if item.case_id == case.case_id))
        plan = replace(run.plan, cases=(case,))
        current = self.capabilities.check(plan, options)
        if current.execution_options != options:
            raise BenchmarkError("Recorded placement is no longer valid; do not submit an altered benchmark.")
        self.backend.runtime_preflight(case)
        self._service_preflight(plan, exclude_uid=run.uid)
        lease.renew()
        assert_no_overlap(plan, self.backend.list_runs(None), exclude_uid=run.uid)
        lease.assert_held()

    def _service_preflight(self, plan: HealthcheckPlan, *, exclude_uid: str | None = None) -> None:
        if plan.service_policy is None:
            return
        runs = tuple(run for run in self.backend.list_runs(None) if run.uid != exclude_uid)
        for case in plan.cases:
            nodes, pods = self.backend.read_allocation_inventory(tuple(target.name for target in case.nodes))
            validate_service_case(case, plan.request, plan.service_policy, nodes, pods, runs, self.now())

    def _submit_case(self, run: RunRecord, case: BenchmarkCase, lease: SubmissionLease) -> RunRecord:
        saved = _saved(run, case)
        if saved.state == "SUBMITTING":
            job = self.backend.get_job(run, case)
            if job is not None:
                lease.renew()
                return self.backend.record_job_uid(run, case, job)
            if deadline_reason(run, case, self.now()):
                raise BenchmarkError("Unconfirmed submission exceeded its deadline; cancel this run before retrying.")
        self._revalidate(run, case, lease)
        if saved.state == "PLANNED":
            run = self.backend.save_run(run.update_case(case.case_id, "SUBMITTING", submitted_at=self.now().isoformat()))
        lease.assert_held()
        job = self.backend.create_job(run, case)
        lease.renew()
        return self.backend.record_job_uid(run, case, job)

    def _snapshot(
        self, run: RunRecord, case: BenchmarkCase, evidence: CaseEvidence, lease: SubmissionLease,
        *, cancelling: bool = False,
    ) -> RunRecord:
        saved = _saved(run, case)
        phase: SnapshotPhase = "CANCELLING" if cancelling else _phase(evidence)
        identities = tuple(sorted((pod.name, pod.uid, pod.container_id, pod.restart_count) for pod in evidence.pods))
        scheduling_digest = evidence.scheduling.signature()
        if saved.snapshots and saved.snapshots[-1].phase == phase and saved.snapshots[-1].pod_identities == identities and (
            phase not in {"TERMINAL", "CANCELLING"}
        ) and saved.snapshots[-1].scheduling_sha256 == scheduling_digest:
            return run
        if len(saved.snapshots) >= MAX_SNAPSHOTS:
            raise BenchmarkError("Snapshot history limit reached; retain workloads for manual inspection.")
        receipt = self.store.export(run, case, evidence)
        self.store.verify(run, case, receipt.sha256)
        snapshot = EvidenceSnapshot(self.now().isoformat(), phase, identities, receipt.sha256, scheduling_digest)
        lease.renew()
        state: CaseState = "QUEUED" if saved.state == "RUNNING" and phase == "QUEUED" else saved.state
        return self.backend.save_run(run.update_case(case.case_id, state,
                                    snapshots=(*saved.snapshots, snapshot), evidence_sha256=(
                                        saved.evidence_sha256 if saved.state in TERMINAL_CASE_STATES else receipt.sha256
                                    )))

    def _complete_case(self, run: RunRecord, case: BenchmarkCase, evidence: CaseEvidence, lease: SubmissionLease) -> RunRecord:
        # A transient log read or a just-completed container must not cause
        # automatic deletion of the only complete evidence copy.
        if not evidence.terminal_observation_ready:
            raise BenchmarkError("Terminal observation changed during collection; refresh before assigning a result.")
        if _log_loss_risk(evidence):
            raise BenchmarkError("Final logs are incomplete; retain resources and refresh this run.")
        result = assess_case(case, evidence, min_bus_bandwidth=run.plan.request.parameters.min_bus_bandwidth)
        state = cast(CaseState, {"PASS": "SUCCEEDED", "FAIL": "FAILED", "INCOMPLETE": "INCOMPLETE"}[result.status])
        lease.renew()
        return self.backend.save_run(run.update_case(case.case_id, state, result=asdict(result), completed_at=self.now().isoformat()))

    def _observe(self, run: RunRecord, case: BenchmarkCase, lease: SubmissionLease) -> RunRecord:
        evidence = self.collector.collect(run, case)
        run = self._snapshot(run, case, evidence, lease)
        saved = _saved(run, case)
        phase = _phase(evidence)
        if phase == "TERMINAL":
            if not evidence.terminal_observation_ready:
                return run
            return self._complete_case(run, case, evidence, lease)
        admitted = saved.admitted_at
        started = saved.started_at
        if not evidence.suspended:
            admitted = admitted or self.now().isoformat()
        if phase == "RUNNING":
            started = started or self.now().isoformat()
        state: CaseState = "RUNNING" if phase == "RUNNING" else "QUEUED"
        # STARTING after a previously running launcher disappeared is not a
        # successful continuation. Preserve RUNNING until an exported requeue.
        if saved.state == "RUNNING" and phase == "STARTING":
            state = "RUNNING"
        if (saved.state, saved.admitted_at, saved.started_at) != (state, admitted, started):
            lease.renew()
            run = self.backend.save_run(run.update_case(case.case_id, state, admitted_at=admitted, started_at=started))
        return run

    def _release_case(self, run: RunRecord, case: BenchmarkCase, lease: SubmissionLease) -> tuple[RunRecord, bool]:
        saved = _saved(run, case)
        if saved.state not in TERMINAL_CASE_STATES:
            return run, False
        previous_pods: set[tuple[str, str | None, int]] = set()
        if saved.job_uid:
            if not saved.evidence_sha256:
                raise BenchmarkError("Workload cleanup requires its exported evidence digest.")
            for digest in {saved.evidence_sha256, *(snapshot.sha256 for snapshot in saved.snapshots)}:
                payload = self.store.verify(run, case, digest)
                previous_pods.update((pod["uid"], pod["container_id"], pod["restart_count"])
                                     for pod in payload["evidence"]["pods"])
        job = self.backend.get_job(run, case)
        if job is not None:
            if not saved.job_uid:
                raise BenchmarkError("An unrecorded job exists for a terminal case; do not delete it.")
            if not job["metadata"].get("deletionTimestamp"):
                evidence = self.collector.collect(run, case)
                if not evidence.observation_stable:
                    raise BenchmarkError("Workload changed during cleanup observation; retain resources and refresh.")
                if _log_loss_risk(evidence):
                    raise BenchmarkError("Cleanup would discard unread logs; collect complete evidence first.")
                if _phase(evidence) != "TERMINAL" and not (evidence.suspended and not evidence.pods):
                    raise BenchmarkError("A terminal case has active workload resources; refresh before cleanup.")
                if any((pod.uid, pod.container_id, pod.restart_count) not in previous_pods for pod in evidence.pods):
                    raise BenchmarkError("Workload attempt changed after completion; retain replacement evidence and resources.")
                if saved.state == "SUCCEEDED" and not evidence.suspended and assess_case(
                    case, evidence, min_bus_bandwidth=run.plan.request.parameters.min_bus_bandwidth,
                ).status != "PASS":
                    raise BenchmarkError("Success evidence changed after assessment; inspect resources before cleanup.")
                run = self._snapshot(run, case, evidence, lease, cancelling=evidence.suspended)
                lease.renew()
                self.backend.delete_job(run, case, evidence_resource_version=evidence.job_resource_version)
            return run, False
        return run, self.backend.case_pods_absent(run, case)

    def advance(self, namespace: str, run_id: str, *, keep_resources: bool = False) -> RunRecord:
        with self.lease_factory() as lease:
            run = self.backend.get_run(namespace, run_id)
            if run.state == "CLEANED":
                return run
            if run.state == "CANCELLING":
                return self._cancel(run, lease)
            # Recover an accepted POST before preflight: its own Pods may now
            # occupy the GPUs, so treating it as a fresh request would deadlock.
            for case in run.plan.cases:
                if _saved(run, case).state == "SUBMITTING":
                    run = self._submit_case(run, case, lease)
                if _saved(run, case).state in {"QUEUED", "RUNNING"}:
                    lease.renew()
                    run = self._observe(run, case, lease)
                    if _saved(run, case).state not in TERMINAL_CASE_STATES:
                        reason = deadline_reason(run, case, self.now())
                        if reason:
                            run = self.backend.save_run(mark_cancelling(run, reason=reason))
                            return self._cancel(run, lease)
            unreleased: set[str] = set()
            for case in run.plan.cases:
                saved = _saved(run, case)
                if saved.state in TERMINAL_CASE_STATES:
                    if keep_resources:
                        released = self.backend.get_job(run, case) is None and self.backend.case_pods_absent(run, case)
                    else:
                        run, released = self._release_case(run, case, lease)
                    if not released:
                        unreleased.update(node.uid for node in case.nodes)
            for case in next_cases(run):
                if not unreleased.intersection(node.uid for node in case.nodes):
                    run = self._submit_case(run, case, lease)
            return run

    def cancel(self, namespace: str, run_id: str) -> RunRecord:
        with self.lease_factory() as lease:
            run = self.backend.get_run(namespace, run_id)
            cancelling = mark_cancelling(run)
            if cancelling != run:
                lease.renew()
                run = self.backend.save_run(cancelling)
            return self._cancel(run, lease) if run.state == "CANCELLING" else run

    def refresh(self, namespace: str, run_id: str, *, expected_uid: str) -> RunRecord:
        """Observe recorded attempts without submitting or changing workloads."""
        if not expected_uid:
            raise BenchmarkError("Metadata refresh requires the selected checkpoint UID.")
        with self.lease_factory() as lease:
            run = self.backend.get_run(namespace, run_id)
            if run.uid != expected_uid:
                raise BenchmarkError("Checkpoint identity changed after selection; refuse replacement metadata.")
            if run.state in {"CLEANED", "CANCELLING"}:
                return run
            for case in run.plan.cases:
                saved = _saved(run, case)
                if saved.state == "SUBMITTING":
                    lease.renew()
                    job = self.backend.get_job(run, case)
                    if job is None:
                        continue
                    # Recover only a verified accepted POST, never retry it.
                    run = self.backend.record_job_uid(run, case, job)
                if _saved(run, case).state in {"QUEUED", "RUNNING"}:
                    lease.renew()
                    run = self._observe(run, case, lease)
            return run

    def _cancel(self, run: RunRecord, lease: SubmissionLease) -> RunRecord:
        for case in run.plan.cases:
            saved = _saved(run, case)
            if saved.state in TERMINAL_CASE_STATES:
                continue
            lease.renew()
            job = self.backend.get_job(run, case)
            if job is not None:
                if not saved.job_uid:
                    if saved.state != "SUBMITTING":
                        raise BenchmarkError("Unrecorded MPIJob for an unsubmitted case; cancellation is unsafe.")
                    run = self.backend.record_job_uid(run, case, job)
                evidence = self.collector.collect(run, case)
                if _log_loss_risk(evidence):
                    raise BenchmarkError("Cancellation would discard unread logs; retry evidence collection first.")
                if _phase(evidence) == "TERMINAL":
                    run = self._snapshot(run, case, evidence, lease)
                    if evidence.terminal_observation_ready:
                        run = self._complete_case(run, case, evidence, lease)
                    continue
                run = self._snapshot(run, case, evidence, lease, cancelling=True)
                lease.renew()
                if not evidence.suspended or run.plan.request.queue:
                    changed = self.backend.cancel_job(run, case, evidence_resource_version=evidence.job_resource_version)
                    if changed or not evidence.suspended:
                        continue
                if not self.backend.case_pods_absent(run, case):
                    saved = _saved(run, case)
                    for digest in {saved.evidence_sha256, *(snapshot.sha256 for snapshot in saved.snapshots)}:
                        if not digest:
                            raise BenchmarkError("Cancellation deletion requires verified exported evidence.")
                        self.store.verify(run, case, digest)
                    if not saved.cancellation_delete_requested:
                        lease.renew()
                        run = self.backend.save_run(run.update_case(case.case_id, saved.state,
                                                                   cancellation_delete_requested=True))
                    lease.renew()
                    self.backend.delete_job(run, case, evidence_resource_version=evidence.job_resource_version, cancelling=True)
                    continue
            elif not self.backend.case_pods_absent(run, case):
                if saved.cancellation_delete_requested:
                    continue
                raise BenchmarkError("MPIJob disappeared but Pods remain; cannot confirm cancellation.")
            saved = _saved(run, case)
            if saved.job_uid and not saved.evidence_sha256:
                raise BenchmarkError("Job disappeared before evidence export; cancellation evidence is incomplete.")
            lease.renew()
            run = self.backend.save_run(run.update_case(case.case_id, "INCOMPLETE" if run.stop_reason and saved.state != "PLANNED" else "CANCELLED",
                                                       completed_at=self.now().isoformat(), error=run.stop_reason))
        return run

    def cleanup(self, namespace: str, run_id: str) -> RunRecord:
        with self.lease_factory() as lease:
            run = self.backend.get_run(namespace, run_id)
            if run.state == "CLEANED":
                return run
            if any(saved.state not in TERMINAL_CASE_STATES for saved in run.cases):
                raise BenchmarkError("Cancel or complete every case before cleanup.")
            released = []
            for case in run.plan.cases:
                run, absent = self._release_case(run, case, lease)
                released.append(absent)
            if all(released):
                lease.renew()
                return self.backend.save_run(replace(run, state="CLEANED", revision=run.revision + 1))
            return run

    def wait(
        self, namespace: str, run_id: str, *, poll_interval: int = 10, observation_timeout: int = 3600,
        keep_resources: bool = False,
    ) -> RunRecord:
        if type(poll_interval) is not int or not 1 <= poll_interval <= 300 or (
            type(observation_timeout) is not int or not 1 <= observation_timeout <= 604800
        ):
            raise BenchmarkError("Invalid bounded wait interval or observation timeout.")
        deadline = time.monotonic() + observation_timeout
        while True:
            run = self.advance(namespace, run_id, keep_resources=keep_resources)
            if all(saved.state in TERMINAL_CASE_STATES for saved in run.cases):
                if keep_resources or run.state == "CLEANED":
                    return run
                run = self.cleanup(namespace, run_id)
                if run.state == "CLEANED":
                    return run
            if time.monotonic() >= deadline:
                raise BenchmarkError(f"Observation timed out; {run_id} remains resumable. Reread its recorded case states before continuing.")
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
