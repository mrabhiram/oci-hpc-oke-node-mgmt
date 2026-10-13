from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from oke_hpc_mgmt.benchmarks.artifacts import EvidenceStore
from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.capabilities import CapabilityEvidence, CapabilityPreflight
from oke_hpc_mgmt.benchmarks.diagnostics import SchedulingEvidence
from oke_hpc_mgmt.benchmarks.evidence import CaseEvidence, EvidenceCollector, PodEvidence
from oke_hpc_mgmt.benchmarks.lease import SubmissionLease
from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions, render_mpijob
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError, HealthcheckPlan
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks
from oke_hpc_mgmt.benchmarks.records import RunRecord, decode_record, mark_cancelling
from oke_hpc_mgmt.benchmarks.workflow import BenchmarkWorkflow, PreparedRun, deadline_reason
from test_benchmark_planning import inventory, request
from test_benchmark_records import RUN


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.plan = plan_healthchecks(inventory(2), request(check_type="active"))
        self.initial = replace(RunRecord.create(RUN, self.plan, ExecutionOptions()), created_at=self.clock.isoformat())
        self.current: RunRecord | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, CaseEvidence] = {}
        self.events: list[str] = []
        self.held = False
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = EvidenceStore(Path(self.temporary.name))
        self.backend = Mock(spec=BenchmarkBackend)
        self.backend.list_runs.side_effect = lambda _: (self.current,) if self.current else ()
        self.backend.get_run.side_effect = self.get_run
        self.backend.create_run.side_effect = self.create_run
        self.backend.save_run.side_effect = self.save_run
        self.backend.create_job.side_effect = self.create_job
        self.backend.get_job.side_effect = lambda run, case: copy.deepcopy(self.jobs.get(case.case_id))
        self.backend.record_job_uid.side_effect = self.record_job_uid
        self.backend.cancel_job.side_effect = self.cancel_job
        self.backend.delete_job.side_effect = self.delete_job
        self.backend.case_pods_absent.return_value = True
        self.capabilities = Mock(spec=CapabilityPreflight)
        self.capabilities.check.side_effect = self.capability
        self.collector = Mock(spec=EvidenceCollector)
        self.collector.collect.side_effect = self.collect
        self.lease = MagicMock(spec=SubmissionLease)
        self.lease.__enter__.side_effect = self.enter
        self.lease.__exit__.side_effect = self.leave
        self.lease.renew.side_effect = self.assert_held
        self.lease.assert_held = Mock(side_effect=self.assert_held)
        self.workflow = BenchmarkWorkflow(self.backend, self.store, capabilities=self.capabilities,
                                         collector=self.collector, lease_factory=lambda: self.lease, now=lambda: self.clock)

    def enter(self) -> SubmissionLease:
        self.assertFalse(self.held)
        self.held = True
        return self.lease

    def leave(self, *args: Any) -> None:
        self.held = False

    def assert_held(self) -> None:
        self.assertTrue(self.held)

    def get_run(self, namespace: str, run_id: str) -> RunRecord:
        assert self.current is not None
        self.assertEqual((namespace, run_id), (self.current.plan.request.namespace, self.current.run_id))
        return self.current

    def capability(self, plan: HealthcheckPlan, options: ExecutionOptions) -> CapabilityEvidence:
        return CapabilityEvidence("direct", "namespace-uid", "0.8.0", None, None, (), (), (), options)

    def create_run(self, run: RunRecord) -> RunRecord:
        self.assert_held()
        self.assertIsNone(self.current)
        self.events.append("checkpoint-create")
        self.current = replace(run, uid="run-uid", resource_version="1")
        return self.current

    def save_run(self, run: RunRecord) -> RunRecord:
        self.assert_held()
        assert self.current is not None
        self.assertEqual(self.current.revision + 1, run.revision)
        self.assertEqual(self.current.resource_version, run.resource_version)
        self.events.append("checkpoint-save")
        self.current = decode_record(run.payload(), uid="run-uid", resource_version=str(run.revision))
        return self.current

    def create_job(self, run: RunRecord, case: BenchmarkCase) -> dict[str, Any]:
        self.assert_held()
        assert self.current is not None
        saved = next(item for item in self.current.cases if item.case_id == case.case_id)
        self.assertEqual("SUBMITTING", saved.state)
        self.assertIsNotNone(saved.submitted_at)
        self.assertNotIn(case.case_id, self.jobs)
        body = render_mpijob(run.run_id, case, run.plan.request, run.options, owner_uid=run.uid)
        body["metadata"].update(uid="job-" + case.case_id, resourceVersion="1", creationTimestamp=self.clock.isoformat())
        self.jobs[case.case_id] = body
        self.events.append("job-create:" + case.case_id)
        return copy.deepcopy(body)

    def record_job_uid(self, run: RunRecord, case: BenchmarkCase, job: dict[str, Any]) -> RunRecord:
        return self.save_run(run.update_case(case.case_id, "QUEUED", job_uid=job["metadata"]["uid"],
                                             created_at=job["metadata"]["creationTimestamp"]))

    def collect(self, run: RunRecord, case: BenchmarkCase) -> CaseEvidence:
        self.events.append("collect:" + case.case_id)
        if case.case_id in self.observations:
            return self.observations[case.case_id]
        job = self.jobs[case.case_id]
        return CaseEvidence(case.case_id, job["metadata"]["uid"], job["metadata"]["resourceVersion"], False, False,
                            job["spec"]["runPolicy"]["suspend"], (), ("synthetic pending case",))

    def cancel_job(self, run: RunRecord, case: BenchmarkCase, *, evidence_resource_version: str) -> None:
        self.assert_held()
        self.assertEqual("CANCELLING", run.state)
        saved = next(item for item in run.cases if item.case_id == case.case_id)
        self.assertIsNotNone(saved.evidence_sha256)
        self.store.verify(run, case, saved.evidence_sha256 or "")
        self.assertEqual(self.jobs[case.case_id]["metadata"]["resourceVersion"], evidence_resource_version)
        self.events.append("cancel:" + case.case_id)
        self.jobs[case.case_id]["spec"]["runPolicy"]["suspend"] = True

    def delete_job(self, run: RunRecord, case: BenchmarkCase, *, evidence_resource_version: str) -> None:
        self.assert_held()
        saved = next(item for item in run.cases if item.case_id == case.case_id)
        self.store.verify(run, case, saved.evidence_sha256 or "")
        self.assertEqual(self.jobs[case.case_id]["metadata"]["resourceVersion"], evidence_resource_version)
        self.events.append("delete:" + case.case_id)
        self.jobs.pop(case.case_id)

    def submit(self) -> RunRecord:
        return self.workflow.submit(PreparedRun(self.initial, self.capability(self.plan, self.initial.options)))

    def complete(self, case: BenchmarkCase) -> None:
        node = case.nodes[0]
        log = "\n".join(f"MGMT_NCCL_RANK\t{i}\t{i}\t{node.name}\tworker-uid\n# Rank {i} Group 0" for i in range(8))
        log += "\n8589934592 2147483648 float sum -1 200 43 43 0 201 42 42 0\n# Avg bus bandwidth : 42\n"
        worker = PodEvidence("worker", "worker-uid", "worker", node.name, node.uid, "Running", "image",
                             "sha256:" + "a" * 64, "worker-container", 0, None, "synthetic worker log", True, ())
        launcher = PodEvidence("launcher", "launcher-uid", "launcher", "cpu-node", "cpu-uid", "Succeeded", "image",
                               "sha256:" + "b" * 64, "launcher-container", 0, 0, log, True, ())
        job = self.jobs[case.case_id]
        self.observations[case.case_id] = CaseEvidence(case.case_id, job["metadata"]["uid"], "1", True, False, False,
                                                      (worker, launcher), ())

    def test_prepare_is_read_only_and_returns_resolved_plan(self) -> None:
        prepared = self.workflow.prepare(self.plan, ExecutionOptions())
        self.assertEqual(self.plan, prepared.record.plan)
        self.assertEqual(2, self.backend.runtime_preflight.call_count)
        self.backend.list_runs.assert_called_once_with(None)
        self.assertEqual([], self.events)
        self.lease.__enter__.assert_not_called()

    def test_metadata_refresh_assesses_evidence_without_submitting_pending_cases_or_cleanup(self) -> None:
        self.submit()
        self.complete(self.plan.cases[0])
        self.backend.create_job.reset_mock()
        run = self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.assertEqual(["SUCCEEDED", "PLANNED"], [case.state for case in run.cases])
        self.assertIsNotNone(run.cases[0].evidence_sha256)
        self.backend.create_job.assert_not_called()
        self.backend.delete_job.assert_not_called()
        self.backend.cancel_job.assert_not_called()
        self.assertFalse(self.held)

    def test_metadata_refresh_never_enforces_deadlines_or_changes_cancellation_intent(self) -> None:
        self.submit()
        self.clock += timedelta(hours=1)
        run = self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.assertEqual("QUEUED", run.cases[0].state)
        self.backend.cancel_job.assert_not_called()
        self.current = mark_cancelling(run)
        self.collector.collect.reset_mock()
        self.assertEqual(self.current, self.workflow.refresh("default", RUN, expected_uid="run-uid"))
        self.collector.collect.assert_not_called()

    def test_metadata_refresh_recovers_an_accepted_post_but_never_retries_a_missing_one(self) -> None:
        def lost(run: RunRecord, case: BenchmarkCase) -> dict[str, Any]:
            self.create_job(run, case)
            raise BenchmarkError("response lost")

        self.backend.create_job.side_effect = lost
        with self.assertRaises(BenchmarkError):
            self.submit()
        job = self.jobs.pop("case-0001")
        self.backend.create_job.reset_mock()
        unchanged = self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.assertEqual("SUBMITTING", unchanged.cases[0].state)
        self.collector.collect.assert_not_called()
        self.jobs["case-0001"] = job
        recovered = self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.assertEqual("QUEUED", recovered.cases[0].state)
        self.backend.create_job.assert_not_called()
        self.backend.delete_job.assert_not_called()
        self.backend.cancel_job.assert_not_called()

    def test_metadata_refresh_rejects_missing_or_replaced_checkpoint_identity(self) -> None:
        self.submit()
        self.collector.collect.reset_mock()
        for uid in ("", "replacement-uid"):
            with self.subTest(uid=uid), self.assertRaises(BenchmarkError):
                self.workflow.refresh("default", RUN, expected_uid=uid)
        self.collector.collect.assert_not_called()
        self.assertFalse(self.held)

    def test_metadata_refresh_keeps_resources_on_read_failure_and_skips_cleaned_runs(self) -> None:
        self.submit()
        self.collector.collect.side_effect = BenchmarkError("API unavailable")
        with self.assertRaisesRegex(BenchmarkError, "API unavailable"):
            self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.backend.delete_job.assert_not_called()
        self.backend.cancel_job.assert_not_called()
        self.assertFalse(self.held)
        assert self.current is not None
        self.current = replace(self.current, state="CLEANED")
        self.collector.collect.reset_mock()
        self.assertEqual(self.current, self.workflow.refresh("default", RUN, expected_uid="run-uid"))
        self.collector.collect.assert_not_called()

    def test_completion_during_collection_waits_for_a_stable_final_observation(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        final = self.observations[case.case_id]
        self.observations[case.case_id] = replace(final, observation_stable=False,
                                                 warnings=("MPIJob changed during collection; refresh before assessing success.",))
        waiting = self.workflow.advance("default", RUN, keep_resources=True)
        self.assertEqual("QUEUED", waiting.cases[0].state)
        self.assertIsNone(waiting.cases[0].result)
        self.backend.delete_job.assert_not_called()
        self.observations[case.case_id] = final
        completed = self.workflow.advance("default", RUN, keep_resources=True)
        self.assertEqual("SUCCEEDED", completed.cases[0].state)
        self.assertEqual(2, len(completed.cases[0].snapshots))

    def test_success_condition_before_launcher_exit_requires_refetch_not_final_incomplete(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        final = self.observations[case.case_id]
        self.observations[case.case_id] = replace(final, pods=tuple(
            replace(pod, exit_code=None, phase="Running") if pod.role == "launcher" else pod for pod in final.pods
        ))
        waiting = self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.assertIsNone(waiting.cases[0].result)
        self.observations[case.case_id] = final
        completed = self.workflow.refresh("default", RUN, expected_uid="run-uid")
        self.assertEqual("SUCCEEDED", completed.cases[0].state)

    def test_cancel_observes_completion_race_without_finalizing_or_destroying_it(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        final = self.observations[case.case_id]
        self.observations[case.case_id] = replace(final, observation_stable=False)
        waiting = self.workflow.cancel("default", RUN)
        self.assertEqual("CANCELLING", waiting.state)
        self.assertIsNone(waiting.cases[0].result)
        self.backend.cancel_job.assert_not_called()
        self.observations[case.case_id] = final
        completed = self.workflow.cancel("default", RUN)
        self.assertEqual("SUCCEEDED", completed.cases[0].state)
        self.assertEqual("CANCELLED", completed.cases[1].state)

    def test_cleanup_rejects_a_job_changed_during_collection(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        self.workflow.advance("default", RUN, keep_resources=True)
        self.observations[case.case_id] = replace(self.observations[case.case_id], observation_stable=False)
        with self.assertRaisesRegex(BenchmarkError, "changed during cleanup"):
            self.workflow.advance("default", RUN)
        self.backend.delete_job.assert_not_called()

    def test_terminal_readiness_covers_missing_launcher_failure_and_running(self) -> None:
        evidence = CaseEvidence("case-0001", "job", "1", False, False, False, (), ())
        self.assertFalse(evidence.terminal_observation_ready)
        self.assertFalse(replace(evidence, succeeded=True).terminal_observation_ready)
        self.assertTrue(replace(evidence, failed=True).terminal_observation_ready)
        self.assertFalse(replace(evidence, failed=True, observation_stable=False).terminal_observation_ready)
        with self.assertRaisesRegex(BenchmarkError, "Terminal observation"):
            self.workflow._complete_case(self.initial, self.plan.cases[0], evidence, self.lease)

    def test_submission_persists_intent_before_one_disjoint_batch(self) -> None:
        submitted = self.submit()
        self.assertEqual(["QUEUED", "PLANNED"], [case.state for case in submitted.cases])
        self.assertLess(self.events.index("checkpoint-save"), self.events.index("job-create:case-0001"))
        self.assertEqual(2, self.lease.__enter__.call_count)
        self.assertFalse(self.held)
        self.assertIsNotNone(submitted.cases[0].submitted_at)

    def test_prepare_overlap_and_unverified_capabilities_never_create(self) -> None:
        self.current = replace(self.initial, uid="foreign", resource_version="1")
        with self.assertRaisesRegex(BenchmarkError, "overlap"):
            self.workflow.prepare(self.plan, ExecutionOptions())
        self.current = None
        self.capabilities.check.side_effect = BenchmarkError("RBAC unavailable")
        with self.assertRaisesRegex(BenchmarkError, "RBAC"):
            self.submit()
        self.backend.create_run.assert_not_called()
        self.backend.create_job.assert_not_called()

    def test_namespace_or_placement_changed_after_confirmation_refuses_submission(self) -> None:
        for capability in (replace(self.capability(self.plan, self.initial.options), namespace_uid="replacement"),
                           replace(self.capability(self.plan, self.initial.options), execution_options=ExecutionOptions(timeout=99))):
            self.capabilities.check.side_effect = None
            self.capabilities.check.return_value = capability
            with self.assertRaisesRegex(BenchmarkError, "changed after planning"):
                self.submit()
        self.backend.create_run.assert_not_called()

    def test_post_accepted_then_connection_lost_is_recovered_without_duplicate(self) -> None:
        def lost(run: RunRecord, case: BenchmarkCase) -> dict[str, Any]:
            self.create_job(run, case)
            raise BenchmarkError("connection lost after POST")
        self.backend.create_job.side_effect = lost
        with self.assertRaisesRegex(BenchmarkError, "is recorded; resume"):
            self.submit()
        assert self.current is not None
        self.assertEqual("SUBMITTING", self.current.cases[0].state)
        self.backend.runtime_preflight.reset_mock()
        recovered = self.workflow.advance("default", RUN)
        self.assertEqual("QUEUED", recovered.cases[0].state)
        self.assertEqual(1, self.backend.create_job.call_count)
        self.backend.runtime_preflight.assert_not_called()

    def test_unaccepted_post_retries_only_same_name_after_preflight(self) -> None:
        self.backend.create_job.side_effect = BenchmarkError("POST not confirmed")
        with self.assertRaises(BenchmarkError):
            self.submit()
        self.backend.create_job.side_effect = self.create_job
        recovered = self.workflow.advance("default", RUN)
        self.assertEqual("QUEUED", recovered.cases[0].state)
        self.assertEqual(2, self.backend.create_job.call_count)
        self.assertEqual(1, len(self.jobs))

    def test_expired_unknown_submission_does_not_silently_restart(self) -> None:
        self.backend.create_job.side_effect = BenchmarkError("POST not confirmed")
        with self.assertRaises(BenchmarkError):
            self.submit()
        self.clock += timedelta(hours=1)
        with self.assertRaisesRegex(BenchmarkError, "cancel this run"):
            self.workflow.advance("default", RUN)
        self.assertEqual(1, self.backend.create_job.call_count)

    def test_snapshot_export_and_reassessment_produce_real_structured_verdict(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        observed = self.workflow.advance("default", RUN, keep_resources=True)
        saved = observed.cases[0]
        self.assertEqual("SUCCEEDED", saved.state)
        self.assertEqual("UNGRADED", (saved.result or {})["performance"])
        self.assertEqual("PASS", (saved.result or {})["coverage"])
        payload = self.store.verify(observed, case, saved.evidence_sha256 or "")
        self.assertIn("8589934592", payload["evidence"]["pods"][1]["log"])
        self.backend.delete_job.assert_not_called()

    def test_unreadable_final_logs_do_not_delete_or_claim_success(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        evidence = self.observations[case.case_id]
        self.observations[case.case_id] = replace(evidence, pods=(evidence.pods[0], replace(evidence.pods[1], log_complete=False)))
        with self.assertRaisesRegex(BenchmarkError, "Final logs are incomplete"):
            self.workflow.advance("default", RUN)
        assert self.current is not None
        self.assertEqual("QUEUED", self.current.cases[0].state)
        self.backend.delete_job.assert_not_called()

    def test_export_storage_failure_retains_job_and_checkpoint_state(self) -> None:
        self.submit()
        self.complete(self.plan.cases[0])
        with patch.object(self.store, "export", side_effect=BenchmarkError("disk full")):
            with self.assertRaisesRegex(BenchmarkError, "disk full"):
                self.workflow.advance("default", RUN)
        self.backend.delete_job.assert_not_called()
        self.backend.cancel_job.assert_not_called()

    def test_cancel_exports_before_suspend_and_waits_for_pod_absence(self) -> None:
        self.submit()
        first = self.workflow.cancel("default", RUN)
        self.assertEqual("CANCELLING", first.state)
        self.assertEqual("CANCELLED", first.cases[1].state)
        self.assertLess(self.events.index("collect:case-0001"), self.events.index("cancel:case-0001"))
        self.backend.case_pods_absent.return_value = False
        second = self.workflow.advance("default", RUN)
        self.assertEqual("CANCELLING", second.state)
        self.backend.case_pods_absent.return_value = True
        final = self.workflow.advance("default", RUN)
        self.assertEqual("CANCELLED", final.state)
        self.assertEqual(1, self.backend.cancel_job.call_count)
        self.assertEqual(1, self.backend.create_job.call_count)

    def test_cancel_reads_all_started_logs_or_refuses_suspension(self) -> None:
        self.submit()
        self.complete(self.plan.cases[0])
        evidence = self.observations["case-0001"]
        self.observations["case-0001"] = replace(evidence, succeeded=False, pods=(replace(evidence.pods[0], log_complete=False),))
        with self.assertRaisesRegex(BenchmarkError, "discard unread logs"):
            self.workflow.cancel("default", RUN)
        self.backend.cancel_job.assert_not_called()
        self.backend.delete_job.assert_not_called()

    def test_cancel_adopts_confirmed_partial_submission_before_suspending(self) -> None:
        def lost(run: RunRecord, case: BenchmarkCase) -> dict[str, Any]:
            self.create_job(run, case)
            raise BenchmarkError("lost")
        self.backend.create_job.side_effect = lost
        with self.assertRaises(BenchmarkError):
            self.submit()
        result = self.workflow.cancel("default", RUN)
        self.assertEqual("job-case-0001", result.cases[0].job_uid)
        self.backend.cancel_job.assert_called_once()

    def test_cancel_never_adopts_unsubmitted_job_or_missing_job_with_orphans(self) -> None:
        self.submit()
        case = self.plan.cases[1]
        self.jobs[case.case_id] = copy.deepcopy(self.jobs["case-0001"])
        with self.assertRaisesRegex(BenchmarkError, "unsubmitted"):
            self.workflow.cancel("default", RUN)
        self.jobs.clear()
        self.backend.case_pods_absent.return_value = False
        with self.assertRaisesRegex(BenchmarkError, "Pods remain"):
            self.workflow.cancel("default", RUN)

    def test_cancel_missing_job_without_export_does_not_claim_evidence_preserved(self) -> None:
        self.submit()
        self.jobs.clear()
        with self.assertRaisesRegex(BenchmarkError, "before evidence export"):
            self.workflow.cancel("default", RUN)

    def test_cleanup_rejects_active_run_and_waits_for_observed_removal(self) -> None:
        self.submit()
        with self.assertRaisesRegex(BenchmarkError, "complete every case"):
            self.workflow.cleanup("default", RUN)
        self.workflow.cancel("default", RUN)
        self.workflow.cancel("default", RUN)
        deleting = self.workflow.cleanup("default", RUN)
        self.assertEqual("CANCELLED", deleting.state)
        self.backend.case_pods_absent.return_value = False
        self.assertEqual("CANCELLED", self.workflow.cleanup("default", RUN).state)
        self.backend.case_pods_absent.return_value = True
        cleaned = self.workflow.cleanup("default", RUN)
        self.assertEqual("CLEANED", cleaned.state)
        self.assertEqual(cleaned, self.workflow.cleanup("default", RUN))
        self.assertEqual(cleaned, self.workflow.advance("default", RUN))

    def test_cleanup_verifies_older_exports_too_not_only_last_empty_snapshot(self) -> None:
        self.submit()
        first = self.workflow.cancel("default", RUN)
        digest = first.cases[0].evidence_sha256
        self.workflow.cancel("default", RUN)
        path = Path(self.temporary.name) / RUN / f"case-0001-{digest}.json"
        path.unlink()
        with self.assertRaisesRegex(BenchmarkError, "unavailable"):
            self.workflow.cleanup("default", RUN)
        self.backend.delete_job.assert_not_called()

    def test_concurrent_checkpoint_change_stops_before_workload_mutation(self) -> None:
        self.backend.save_run.side_effect = BenchmarkError("checkpoint conflict")
        with self.assertRaisesRegex(BenchmarkError, "checkpoint conflict"):
            self.submit()
        self.backend.create_job.assert_not_called()

    def test_lease_loss_stops_before_creation(self) -> None:
        self.lease.renew.side_effect = BenchmarkError("lease lost")
        with self.assertRaisesRegex(BenchmarkError, "lease lost"):
            self.submit()
        self.backend.create_run.assert_not_called()
        self.backend.create_job.assert_not_called()

    def test_queued_timeout_records_incomplete_not_hardware_failure(self) -> None:
        self.initial = replace(self.initial, plan=replace(self.plan, request=replace(self.plan.request, queue="queue")))
        self.submit()
        self.clock += timedelta(seconds=901)
        pending = self.workflow.advance("default", RUN)
        self.assertEqual("INCOMPLETE", pending.state)
        result = self.workflow.advance("default", RUN)
        self.assertEqual("INCOMPLETE", result.state)
        self.assertIn("queue deadline", result.cases[0].error or "")
        self.assertEqual("CANCELLED", result.cases[1].state)

    def test_wait_sleeps_outside_lease_and_observation_timeout_does_not_restart(self) -> None:
        self.submit()
        def sleep(_: float) -> None:
            self.assertFalse(self.held)
        with patch("oke_hpc_mgmt.benchmarks.workflow.time.monotonic", side_effect=[0, 0, 0, 2]), \
                patch("oke_hpc_mgmt.benchmarks.workflow.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(BenchmarkError, "remains resumable"):
                self.workflow.wait("default", RUN, observation_timeout=1, poll_interval=1)
        self.assertEqual(1, self.backend.create_job.call_count)
        self.backend.cancel_job.assert_not_called()
        self.backend.delete_job.assert_not_called()

    def test_wait_validates_bounds_and_retains_completed_resources_on_request(self) -> None:
        for options in ({"poll_interval": 0}, {"poll_interval": True}, {"observation_timeout": 0}):
            with self.assertRaises(BenchmarkError):
                self.workflow.wait("default", RUN, **options)
        self.initial = RunRecord.create(RUN, replace(self.plan, cases=self.plan.cases[:1]), ExecutionOptions())
        self.submit()
        self.complete(self.plan.cases[0])
        self.assertEqual("SUCCEEDED", self.workflow.wait("default", RUN, keep_resources=True).state)
        self.backend.delete_job.assert_not_called()

    def test_deadlines_are_timezone_aware_and_do_not_reset_on_requeue(self) -> None:
        self.submit()
        assert self.current is not None
        case = self.plan.cases[0]
        queued = replace(self.current, cases=(replace(self.current.cases[0], admitted_at=self.clock.isoformat()), *self.current.cases[1:]))
        self.assertIn("startup", deadline_reason(queued, case, self.clock + timedelta(seconds=601)) or "")
        running = replace(queued, cases=(replace(queued.cases[0], started_at=self.clock.isoformat()), *queued.cases[1:]))
        self.assertIn("execution", deadline_reason(running, case, self.clock + timedelta(seconds=1801)) or "")
        for invalid in (self.clock.replace(tzinfo=None), self.clock - timedelta(seconds=1)):
            with self.assertRaises(BenchmarkError):
                deadline_reason(running, case, invalid)

    def test_requeue_keeps_distinct_history_and_original_start_deadline(self) -> None:
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        running = replace(self.observations[case.case_id], succeeded=False,
                          pods=tuple(replace(pod, phase="Running", exit_code=None) for pod in self.observations[case.case_id].pods))
        self.observations[case.case_id] = running
        first = self.workflow.advance("default", RUN)
        self.clock += timedelta(seconds=10)
        self.observations[case.case_id] = replace(running, suspended=True, pods=())
        queued = self.workflow.advance("default", RUN)
        self.assertEqual("QUEUED", queued.cases[0].state)
        self.assertEqual(first.cases[0].started_at, queued.cases[0].started_at)
        self.assertEqual(["RUNNING", "QUEUED"], [snapshot.phase for snapshot in queued.cases[0].snapshots])
        for snapshot in queued.cases[0].snapshots:
            self.store.verify(queued, case, snapshot.sha256)

    def test_same_observation_is_not_appended_on_every_poll(self) -> None:
        self.submit()
        first = self.workflow.advance("default", RUN)
        second = self.workflow.advance("default", RUN)
        self.assertEqual(first, second)
        self.assertEqual(1, len(second.cases[0].snapshots))

    def test_cancellation_racing_with_completion_preserves_observed_result(self) -> None:
        self.submit()
        self.complete(self.plan.cases[0])
        result = self.workflow.cancel("default", RUN)
        self.assertEqual("SUCCEEDED", result.cases[0].state)
        self.assertEqual("CANCELLED", result.cases[1].state)
        self.backend.cancel_job.assert_not_called()

    def test_cleanup_rechecks_attempt_identity_and_does_not_replace_result_digest(self) -> None:
        self.initial = RunRecord.create(RUN, replace(self.plan, cases=self.plan.cases[:1]), ExecutionOptions())
        self.submit()
        case = self.plan.cases[0]
        self.complete(case)
        result = self.workflow.advance("default", RUN, keep_resources=True)
        digest = result.cases[0].evidence_sha256
        original = self.observations[case.case_id]
        self.observations[case.case_id] = replace(original, pods=(replace(original.pods[0], uid="replacement"), original.pods[1]))
        with self.assertRaisesRegex(BenchmarkError, "attempt changed"):
            self.workflow.cleanup("default", RUN)
        self.backend.delete_job.assert_not_called()
        self.observations[case.case_id] = original
        self.clock += timedelta(seconds=1)
        deleting = self.workflow.cleanup("default", RUN)
        self.assertEqual(digest, deleting.cases[0].evidence_sha256)
        self.assertNotEqual(digest, deleting.cases[0].snapshots[-1].sha256)
        self.assertEqual("CLEANED", self.workflow.cleanup("default", RUN).state)

    def test_changed_success_or_resumed_execution_never_authorizes_cleanup(self) -> None:
        self.initial = RunRecord.create(RUN, replace(self.plan, cases=self.plan.cases[:1]), ExecutionOptions())
        self.submit()
        self.complete(self.plan.cases[0])
        self.workflow.advance("default", RUN, keep_resources=True)
        original = self.observations["case-0001"]
        for evidence, message in ((replace(original, succeeded=False), "active workload"),
                                  (replace(original, failed=True), "Success evidence changed")):
            self.observations["case-0001"] = evidence
            with self.assertRaisesRegex(BenchmarkError, message):
                self.workflow.cleanup("default", RUN)
        self.backend.delete_job.assert_not_called()

    def test_automatic_progress_releases_overlapping_workers_before_next_repetition(self) -> None:
        case = self.plan.cases[0]
        repeat = replace(case, case_id="case-0002", repetition=2)
        self.initial = RunRecord.create(RUN, replace(self.plan, cases=(case, repeat)), ExecutionOptions())
        self.submit()
        self.complete(case)
        deleting = self.workflow.advance("default", RUN)
        self.assertEqual("PLANNED", deleting.cases[1].state)
        self.assertEqual(1, self.backend.create_job.call_count)
        self.backend.case_pods_absent.return_value = False
        self.workflow.advance("default", RUN)
        self.assertEqual(1, self.backend.create_job.call_count)
        self.backend.case_pods_absent.return_value = True
        submitted = self.workflow.advance("default", RUN)
        self.assertEqual("QUEUED", submitted.cases[1].state)
        self.assertEqual(2, self.backend.create_job.call_count)

    def test_pending_deletion_and_retained_resources_never_resubmit_overlapping_case(self) -> None:
        case = self.plan.cases[0]
        self.initial = RunRecord.create(RUN, replace(self.plan, cases=(case, replace(case, case_id="case-0002"))), ExecutionOptions())
        self.submit()
        self.complete(case)
        self.workflow.advance("default", RUN, keep_resources=True)
        self.assertEqual(1, self.backend.create_job.call_count)
        self.jobs[case.case_id]["metadata"]["deletionTimestamp"] = self.clock.isoformat()
        self.workflow.advance("default", RUN)
        self.backend.delete_job.assert_not_called()
        self.assertEqual(1, self.backend.create_job.call_count)

    def test_wait_completes_resource_release_before_returning(self) -> None:
        self.initial = RunRecord.create(RUN, replace(self.plan, cases=self.plan.cases[:1]), ExecutionOptions())
        self.submit()
        self.complete(self.plan.cases[0])
        result = self.workflow.wait("default", RUN)
        self.assertEqual("CLEANED", result.state)
        self.assertEqual("SUCCEEDED", result.cases[0].state)
        self.assertEqual({}, self.jobs)

    def test_multinode_modes_preserve_exact_planned_worker_count(self) -> None:
        for mode in ("pairs", "all-nodes", "per-local-block"):
            with self.subTest(mode=mode):
                self.current = None
                self.jobs.clear()
                plan = plan_healthchecks(inventory(4), request(mode=mode))
                self.initial = RunRecord.create(RUN, plan, ExecutionOptions(concurrency=2))
                result = self.submit()
                for case in plan.cases:
                    if case.case_id in self.jobs:
                        spec = self.jobs[case.case_id]["spec"]
                        self.assertEqual(len(case.nodes), spec["mpiReplicaSpecs"]["Worker"]["replicas"])
                        self.assertEqual("QUEUED", next(item for item in result.cases if item.case_id == case.case_id).state)

    def test_busy_node_or_changed_capability_on_resume_prevents_new_post(self) -> None:
        self.backend.create_job.side_effect = BenchmarkError("POST not confirmed")
        with self.assertRaises(BenchmarkError):
            self.submit()
        self.backend.runtime_preflight.side_effect = BenchmarkError("GPU already allocated")
        with self.assertRaisesRegex(BenchmarkError, "already allocated"):
            self.workflow.advance("default", RUN)
        self.backend.runtime_preflight.side_effect = None
        self.capabilities.check.side_effect = None
        self.capabilities.check.return_value = self.capability(self.plan, ExecutionOptions(timeout=99))
        with self.assertRaisesRegex(BenchmarkError, "placement is no longer valid"):
            self.workflow.advance("default", RUN)
        self.assertEqual(1, self.backend.create_job.call_count)

    def test_queue_diagnostic_changes_export_even_without_pod_or_phase_change(self) -> None:
        run = self.submit()
        case = run.plan.cases[0]
        evidence = self.collect(run, case)
        self.collector.collect.side_effect = None
        self.collector.collect.return_value = evidence
        first = self.workflow.advance("default", run.run_id, keep_resources=True)
        events = ({"metadata": {"uid": "event-uid"}, "involvedObject": {"uid": "job-uid"},
                   "reason": "Pending", "message": "launcher taint", "type": "Warning"},)
        self.collector.collect.return_value = replace(evidence, scheduling=SchedulingEvidence(events=events))
        changed = self.workflow.advance("default", run.run_id, keep_resources=True)
        self.assertGreater(len(changed.cases[0].snapshots), len(first.cases[0].snapshots))
        latest = changed.cases[0].snapshots[-1]
        payload = self.store.verify(changed, case, latest.sha256)
        self.assertEqual("launcher taint", payload["evidence"]["scheduling"]["events"][0]["message"])
        repeated = self.workflow.advance("default", run.run_id, keep_resources=True)
        self.assertEqual(changed.cases[0].snapshots, repeated.cases[0].snapshots)

    def test_snapshot_limit_and_unrecorded_terminal_job_block_mutations(self) -> None:
        self.submit()
        with patch("oke_hpc_mgmt.benchmarks.workflow.MAX_SNAPSHOTS", 0):
            with self.assertRaisesRegex(BenchmarkError, "history limit"):
                self.workflow.advance("default", RUN)
        assert self.current is not None
        self.current = mark_cancelling(self.current)
        self.jobs.clear()
        self.current = self.current.update_case("case-0001", "CANCELLED", job_uid="job-case-0001")
        with self.assertRaisesRegex(BenchmarkError, "digest"):
            with self.lease:
                self.workflow._release_case(self.current, self.plan.cases[0], self.lease)

    def test_no_node_or_cloud_mutation_method_is_in_the_workflow_contract(self) -> None:
        self.submit()
        self.workflow.advance("default", RUN)
        self.workflow.cancel("default", RUN)
        self.workflow.cancel("default", RUN)
        self.workflow.cleanup("default", RUN)
        allowed = {"list_runs", "get_run", "create_run", "save_run", "runtime_preflight", "get_job",
                   "create_job", "record_job_uid", "cancel_job", "delete_job", "case_pods_absent"}
        self.assertTrue(all(call[0] in allowed for call in self.backend.mock_calls))
