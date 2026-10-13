from __future__ import annotations

import csv
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from unittest.mock import Mock, patch

from click.testing import CliRunner, Result

from oke_hpc_mgmt.benchmarks.capabilities import CapabilityEvidence
from oke_hpc_mgmt.benchmarks.evidence import CaseEvidence, PodEvidence
from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions
from oke_hpc_mgmt.benchmarks.models import BenchmarkError, HealthcheckPlan
from oke_hpc_mgmt.benchmarks.records import EvidenceSnapshot, RunRecord
from oke_hpc_mgmt.benchmarks.workflow import PreparedRun
from oke_hpc_mgmt.cli import main
from oke_hpc_mgmt.commands import cli
from oke_hpc_mgmt.commands.benchmarks import benchmark_exit_code, case_rows, evidence_directory, log_rows, run_summary
from oke_hpc_mgmt.models import HealthResult
from test_benchmark_planning import inventory
from test_benchmark_records import RUN, record


MODULE = "oke_hpc_mgmt.commands.benchmarks."


class BenchmarkCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner(mix_stderr=False)
        self.snapshot = inventory(4)
        self.service = Mock()
        self.service.discover.return_value = self.snapshot
        self.backend = Mock()
        self.workflow = Mock()
        self.workflow.backend = self.backend
        self.workflow.prepare.side_effect = self.prepare
        self.workflow.submit.side_effect = self.submit
        self.recorded = replace(record(), uid="run-uid", resource_version="1")
        self.backend.get_run.return_value = self.recorded
        self.backend.list_runs.return_value = (self.recorded,)
        self.workflow.wait.return_value = self.recorded
        for name, replacement in (("CliState.service", self.service), ("BenchmarkBackend", self.backend),
                                  ("BenchmarkWorkflow", self.workflow)):
            patcher = patch(MODULE + name)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            mock.return_value = replacement

    def prepare(self, plan: HealthcheckPlan, options: ExecutionOptions) -> PreparedRun:
        run = RunRecord.create(RUN, plan, options)
        capability = CapabilityEvidence("direct", "namespace-uid", "0.8.0", None, None, (), (), (), options)
        return PreparedRun(run, capability)

    def submit(self, prepared: PreparedRun) -> RunRecord:
        return replace(prepared.record, uid="run-uid", resource_version="1")

    def invoke(self, args: list[str], *, input: str | None = None) -> Result:
        return self.runner.invoke(cli, args, input=input, standalone_mode=False)

    def test_help_registers_the_shared_terminology_and_no_tests_nccl_group(self) -> None:
        for args, expected in ((["nodes", "healthchecks", "--help"], ("--nodes", "--fields", "--reservation", "--mode", "passive")),
                               (["health", "--help"], ("profiles", "list", "get", "results", "wait", "logs", "cancel", "cleanup", "run"))):
            result = self.invoke(args)
            self.assertEqual(0, result.exit_code, result.output)
            for term in expected:
                self.assertIn(term, result.stdout)
        self.assertNotIn("tests", cli.commands)
        self.service.discover.assert_not_called()

    def test_passive_default_selects_only_requested_nodes_and_never_constructs_workload(self) -> None:
        result = self.invoke(["nodes", "healthchecks", "--nodes", "gpu-[01-02]", "--format", "json"])
        self.assertEqual(1, result.return_value, result.exception)
        rows = json.loads(result.stdout)
        self.assertEqual({"gpu-01", "gpu-02"}, {row["scope"] for row in rows if row["check"] == "node-readiness"})
        self.assertTrue(any(row["check"] == "passive-host-coverage" and row["status"] == "WARN" for row in rows))
        self.workflow.prepare.assert_not_called()
        self.service.kubernetes_backend.assert_not_called()

    def test_missing_conflicting_and_unsupported_requests_fail_before_discovery(self) -> None:
        for options in ([], ["--nodes", "gpu-01", "--pool", "gpu-pool"],
                        ["--nodes", "gpu-01", "--type", "active"],
                        ["--pool", "gpu-pool", "--type", "multi-node", "--checks", "rccl"],
                        ["--pool", "gpu-pool", "--reservation", "reservation"],
                        ["--pool", "gpu-pool", "--scheduler", "slurm"],
                        ["--pool", "gpu-pool", "--mode", "all-nodes"],
                        ["--pool", "gpu-pool", "--type", "active", "--checks", "nccl", "--min-bus-bandwidth", "nan"]):
            with self.subTest(options=options):
                result = self.invoke(["nodes", "healthchecks", *options])
                self.assertIsInstance(result.exception, BenchmarkError)
        self.service.discover.assert_not_called()
        self.workflow.submit.assert_not_called()

    def test_nodeset_commas_are_not_naively_split_and_anchor_partners_are_shown(self) -> None:
        result = self.invoke(["nodes", "healthchecks", "--nodes", "gpu-[01-02,04]", "--type", "multi-node",
                              "--checks", "nccl", "--dry-run", "--format", "json"])
        self.assertEqual(0, result.return_value, result.exception)
        payload = json.loads(result.stdout)[0]
        self.assertEqual(["gpu-01", "gpu-02", "gpu-04"], payload["plan"]["selected_nodes"])
        self.assertEqual(["gpu-[01-02,04]"], payload["plan"]["request"]["nodes"])
        self.assertTrue(payload["dry_run"])
        self.workflow.submit.assert_not_called()

    def test_all_modes_parameter_overrides_and_output_are_wired(self) -> None:
        for node in self.snapshot.nodes:
            node.allocatable["nvidia.com/rdma-vf"] = "8"
        for mode in ("pairs", "all-nodes", "per-local-block"):
            result = self.invoke(["nodes", "healthchecks", "--pool", "gpu-pool", "--type", "multi-node", "--mode", mode,
                                  "--checks", "nccl", "--network", "vf", "--queue", "queue", "--scheduler", "kueue",
                                  "--namespace", "benchmarks", "--network-attachment", "existing-nad", "--env", "NCCL_IB_TIMEOUT=23",
                                  "--image", "example.invalid/nccl:test", "--min-bytes", "2G", "--max-bytes", "8G",
                                  "--iterations", "3", "--warmup-iterations", "1", "--repetitions", "2", "--concurrency", "2",
                                  "--queue-timeout", "10", "--startup-timeout", "20", "--timeout", "30", "--min-bus-bandwidth", "1.5",
                                  "--dry-run", "--format", "json"])
            self.assertEqual(0, result.return_value, result.exception)
            plan, options = self.workflow.prepare.call_args.args
            self.assertEqual(mode, plan.request.mode)
            self.assertEqual("benchmarks", plan.request.namespace)
            self.assertEqual(("NCCL_IB_TIMEOUT=23",), plan.request.environment)
            self.assertEqual(2 * 1024**3, plan.request.parameters.min_bytes)
            self.assertEqual(1.5, plan.request.parameters.min_bus_bandwidth)
            self.assertEqual(ExecutionOptions(2, 10, 20, 30, "existing-nad"), options)
            self.assertTrue(all(case.network == "vf" for case in plan.cases))
        self.workflow.submit.assert_not_called()

    def test_all_category_persists_passive_coverage_without_claiming_full_parity(self) -> None:
        result = self.invoke(["nodes", "healthchecks", "--pool", "gpu-pool", "--type", "all", "--checks", "nccl",
                              "--dry-run", "--format", "json"])
        self.assertEqual(0, result.return_value, result.exception)
        payload = json.loads(result.stdout)[0]
        self.assertTrue(payload["plan"]["passive_checks"])
        self.assertIn("not full Slurm", result.stderr)
        self.assertEqual({"active", "multi-node"}, {case["type"] for case in payload["cases"]})

    def test_declined_submission_never_mutates_and_confirmation_shows_participants(self) -> None:
        result = self.invoke(["nodes", "healthchecks", "--nodes", "gpu-01", "--type", "multi-node", "--checks", "nccl"], input="no\n")
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("gpu-01, gpu-02", result.stderr)
        self.assertIn("16 ranks", result.stderr)
        self.workflow.submit.assert_not_called()

    def test_yes_and_typed_confirmation_submit_without_polluting_json(self) -> None:
        for options, input in ((["--yes"], None), ([], RUN + "\n")):
            with patch("oke_hpc_mgmt.commands.common.click.prompt", return_value=RUN):
                result = self.invoke(["nodes", "healthchecks", "--nodes", "gpu-01", "--type", "active", "--checks", "nccl",
                                      "--format", "json", *options], input=input)
            self.assertEqual(0, result.return_value, result.exception)
            self.assertEqual("IN_PROGRESS", json.loads(result.stdout)[0]["outcome"])
            self.assertIn("health wait", result.stderr)
        self.assertEqual(2, self.workflow.submit.call_count)
        self.workflow.wait.assert_not_called()

    def test_wait_and_artifact_settings_reach_the_workflow(self) -> None:
        result = self.invoke(["nodes", "healthchecks", "--nodes", "gpu-01", "--type", "active", "--checks", "nccl",
                              "--wait", "--keep-resources", "--yes", "--output-dir", "/tmp/synthetic-evidence",
                              "--poll-interval", "2", "--observation-timeout", "40", "--format", "json"])
        self.assertEqual(3, result.return_value, result.exception)
        self.workflow.wait.assert_called_once_with("default", RUN, poll_interval=2, observation_timeout=40, keep_resources=True)
        self.assertEqual("IN_PROGRESS", json.loads(result.stdout)[0]["outcome"])

    def test_validation_dry_run_table_and_csv_and_global_output_work(self) -> None:
        for output in ("table", "csv", "json"):
            result = self.invoke(["--format", output, "nodes", "healthchecks", "--fields", "name=gpu-01", "--type", "active",
                                  "--checks", "nccl", "--dry-run"])
            self.assertEqual(0, result.return_value, result.exception)
            self.assertIn("gpu-01", result.stdout)
        self.workflow.submit.assert_not_called()

    def test_health_profiles_does_not_need_cluster_credentials(self) -> None:
        result = self.invoke(["health", "profiles", "--format", "json"])
        self.assertEqual(0, result.return_value, result.exception)
        rows = json.loads(result.stdout)
        self.assertIn("BM.GPU4.8", {row["shape"] for row in rows})
        self.assertEqual(["local"], next(row for row in rows if row["shape"] == "VM.GPU.A10.1")["networks"])
        self.service.kubernetes_backend.assert_not_called()
        self.assertTrue(str(evidence_directory()).endswith(".local/state/mgmt-oke/healthchecks"))

    def test_health_list_filters_pool_and_all_namespaces_without_advancing(self) -> None:
        result = self.invoke(["health", "list", "--all-namespaces", "--pool", "gpu-pool", "--format", "json"])
        self.assertEqual(0, result.return_value, result.exception)
        self.backend.list_runs.assert_called_once_with(None)
        self.assertEqual(RUN, json.loads(result.stdout)[0]["run_id"])
        empty = self.invoke(["health", "list", "--namespace", "benchmarks", "--pool", "absent", "--format", "json"])
        self.assertEqual([], json.loads(empty.stdout))
        self.backend.list_runs.assert_called_with("benchmarks")
        self.workflow.wait.assert_not_called()

    def test_get_and_results_are_read_only_and_preserve_case_evidence(self) -> None:
        for command in ("get", "results"):
            result = self.invoke(["health", command, RUN, "--format", "json"])
            self.assertIn(result.return_value, (0, 3), result.exception)
            self.assertIn("case-0001", result.stdout)
        self.workflow.wait.assert_not_called()
        self.workflow.submit.assert_not_called()

    def test_health_wait_uses_existing_identity_and_never_makes_a_new_run(self) -> None:
        result = self.invoke(["health", "wait", RUN, "--namespace", "benchmarks", "--poll-interval", "3",
                              "--observation-timeout", "60", "--keep-resources", "--format", "json"])
        self.assertEqual(3, result.return_value, result.exception)
        self.workflow.wait.assert_called_once_with("benchmarks", RUN, poll_interval=3, observation_timeout=60, keep_resources=True)
        self.workflow.prepare.assert_not_called()
        self.workflow.submit.assert_not_called()

    def terminal(self, state: str = "CANCELLED") -> RunRecord:
        current = self.recorded
        for case in current.cases:
            current = current.update_case(case.case_id, "SUBMITTING").update_case(
                case.case_id, state, job_uid="job-" + case.case_id,  # type: ignore[arg-type]
                result={"status": "PASS"} if state == "SUCCEEDED" else None, evidence_sha256="a" * 64,
            )
        return current

    def test_outcomes_keep_cancel_failure_and_incomplete_distinct_after_cleanup(self) -> None:
        for state, expected in (("SUCCEEDED", 0), ("FAILED", 2), ("INCOMPLETE", 1), ("CANCELLED", 130)):
            run = replace(self.terminal(state), state="CLEANED")
            self.assertEqual(expected, benchmark_exit_code(run))
            self.assertEqual(state, run_summary(run)["outcome"])
            self.assertEqual(state, case_rows(run)[0]["state"])
        for status, expected in (("WARN", 1), ("FAIL", 2)):
            run = self.terminal("SUCCEEDED")
            run = replace(run, plan=replace(run.plan, passive_checks=(HealthResult("passive", "node", status, "synthetic"),)))
            self.assertEqual(expected, benchmark_exit_code(run))

    def test_cancel_and_cleanup_dry_run_never_mutate(self) -> None:
        self.backend.get_run.return_value = self.terminal()
        for command in ("cancel", "cleanup"):
            result = self.invoke(["health", command, RUN, "--dry-run", "--format", "json"])
            self.assertEqual(0, result.return_value, result.exception)
            self.assertEqual(RUN, json.loads(result.stdout)[0]["run_id"])
        self.workflow.cancel.assert_not_called()
        self.workflow.cleanup.assert_not_called()

    def test_results_preserve_passive_rows_in_json_and_one_csv_schema(self) -> None:
        run = self.terminal("SUCCEEDED")
        self.backend.get_run.return_value = replace(run, plan=replace(run.plan, passive_checks=(
            HealthResult("passive-host-coverage", "selected-nodes", "WARN", "Host checks unavailable"),
        )))
        for output in ("json", "csv"):
            result = self.invoke(["health", "results", RUN, "--format", output])
            self.assertEqual(1, result.return_value, result.exception)
            rows = json.loads(result.stdout) if output == "json" else list(csv.DictReader(io.StringIO(result.stdout)))
            self.assertEqual({"nccl", "passive"}, {row["record_type"] for row in rows})
            self.assertEqual(1, len([row for row in rows if row["record_type"] == "passive"]))
            self.assertEqual("WARN", rows[-1]["status"])
            self.assertEqual("Host checks unavailable", rows[-1]["message"])

    def test_cancel_cleanup_confirmation_pending_and_completed_reporting(self) -> None:
        for command in ("cancel", "cleanup"):
            self.backend.get_run.return_value = self.terminal()
            rejected = self.invoke(["health", command, RUN], input="no\n")
            self.assertNotEqual(0, rejected.exit_code)
            target = self.workflow.cancel if command == "cancel" else self.workflow.cleanup
            target.assert_not_called()
            target.return_value = replace(self.recorded, state="CANCELLING") if command == "cancel" else self.terminal()
            pending = self.invoke(["health", command, RUN, "--yes", "--format", "json"])
            self.assertEqual(3, pending.return_value, pending.exception)
            self.assertIn("not yet converged", pending.stderr)
            target.return_value = self.terminal() if command == "cancel" else replace(self.terminal(), state="CLEANED")
            final = self.invoke(["health", command, RUN, "--yes", "--format", "json"])
            self.assertEqual(0, final.return_value, final.exception)
            target.assert_called_with("default", RUN)

    def test_cleanup_refuses_unfinished_cases_and_invalid_ids_namespaces_never_call_api(self) -> None:
        result = self.invoke(["health", "cleanup", RUN, "--yes"])
        self.assertIsInstance(result.exception, BenchmarkError)
        self.workflow.cleanup.assert_not_called()
        self.backend.get_run.reset_mock()
        for args in (["health", "get", "bad"], ["health", "wait", RUN, "--namespace", "bad/ns"],
                     ["health", "logs", RUN, "--namespace", "UPPER"]):
            self.assertNotEqual(0, self.invoke(list(args)).exit_code)
        self.backend.get_run.assert_not_called()

    def test_production_entrypoint_returns_failure_code_with_readable_error(self) -> None:
        output = io.StringIO()
        with redirect_stderr(output):
            code = main(["nodes", "healthchecks", "--type", "active", "--checks", "nccl"])
        self.assertEqual(2, code)
        self.assertIn("Specify exactly one", output.getvalue())

    def test_production_entrypoint_uses_recorded_results_exit_code(self) -> None:
        self.backend.get_run.return_value = self.terminal("FAILED")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["health", "results", RUN, "--format", "json"])
        self.assertEqual(2, code)
        self.assertEqual("FAILED", json.loads(output.getvalue())[0]["state"])


class BenchmarkLogCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner(mix_stderr=False)
        self.backend = Mock()
        self.store = Mock()
        self.run_record = replace(record(), uid="run-uid", resource_version="1")
        self.case = self.run_record.plan.cases[0]
        self.run_record = self.run_record.update_case(self.case.case_id, "SUBMITTING").update_case(
            self.case.case_id, "RUNNING", job_uid="job-uid")
        self.pod = PodEvidence("launcher", "pod-uid", "launcher", "cpu-node", "cpu-uid", "Running", "image", "digest",
                               "container", 0, None, "synthetic log\n", True, ())
        self.evidence = CaseEvidence(self.case.case_id, "job-uid", "1", False, False, False, (self.pod,), ())
        self.collector = Mock()
        self.collector.collect.return_value = self.evidence
        self.backend.get_job.return_value = {"metadata": {"uid": "job-uid"}}
        self.backend.get_run.return_value = self.run_record
        for name, replacement in (("_backend", self.backend), ("EvidenceStore", self.store), ("EvidenceCollector", self.collector)):
            patcher = patch(MODULE + name, return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(self, *args: str) -> Result:
        return self.runner.invoke(cli, ["health", "logs", RUN, "--case", self.case.case_id, *args], standalone_mode=False)

    def test_live_logs_retain_pod_identity_and_do_not_advance(self) -> None:
        result = self.invoke("--format", "json")
        self.assertEqual(0, result.return_value, result.exception)
        row = json.loads(result.stdout)[0]
        self.assertEqual("pod-uid", row["pod_uid"])
        self.assertEqual("kubernetes", row["source"])
        self.assertEqual("synthetic log\n", row["log"])
        self.backend.create_job.assert_not_called()
        self.backend.save_run.assert_not_called()

    def test_missing_live_job_reads_verified_exports_without_combining_snapshots(self) -> None:
        snapshot = EvidenceSnapshot("2026-01-01T00:00:00Z", "CANCELLING", (), "a" * 64)
        final = self.run_record.update_case(self.case.case_id, "CANCELLED", evidence_sha256="b" * 64, snapshots=(snapshot,))
        self.backend.get_run.return_value = final
        self.backend.get_job.return_value = None
        self.store.verify.side_effect = [{"evidence": {"pods": []}}, {"evidence": {"pods": [asdict(self.pod)]}}]
        result = self.invoke("--format", "json")
        self.assertEqual(0, result.return_value, result.exception)
        row = json.loads(result.stdout)[0]
        self.assertEqual("a" * 64, row["evidence_sha256"])
        self.assertEqual("export", row["source"])
        self.collector.collect.assert_not_called()

    def test_role_filter_unknown_case_incomplete_and_empty_logs(self) -> None:
        rows, _ = log_rows(self.backend, self.store, self.run_record, self.case.case_id, "worker")
        self.assertEqual([], rows)
        with self.assertRaisesRegex(BenchmarkError, "Unknown case"):
            log_rows(self.backend, self.store, self.run_record, "case-9999", "all")
        self.collector.collect.return_value = replace(self.evidence, pods=(replace(self.pod, log_complete=False),))
        self.assertEqual(1, self.invoke("--format", "json").return_value)
        self.collector.collect.return_value = replace(self.evidence, pods=())
        result = self.invoke("--format", "json")
        self.assertEqual(1, result.return_value)
        self.assertIn("No logs", result.stderr)

    def test_text_logs_escape_terminal_control_characters(self) -> None:
        self.collector.collect.return_value = replace(self.evidence, pods=(replace(self.pod, log="safe\n\x1b]0;injected\x07\n"),))
        result = self.invoke()
        self.assertEqual(0, result.return_value, result.exception)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\x1b", result.stdout)

    def test_follow_prints_new_content_and_stops_at_terminal_observation(self) -> None:
        first = replace(self.evidence, pods=(replace(self.pod, log="first\n"),))
        second = replace(self.evidence, succeeded=True, pods=(replace(self.pod, log="first\nsecond\n"),))
        self.collector.collect.side_effect = [first, second]
        with patch(MODULE + "time.sleep"):
            result = self.invoke("--follow")
        self.assertEqual(0, result.return_value, result.exception)
        self.assertEqual(1, result.stdout.count("first\n"))
        self.assertEqual(1, result.stdout.count("second\n"))
        self.backend.save_run.assert_not_called()

    def test_follow_timeout_and_rotation_are_honest_and_non_mutating(self) -> None:
        self.collector.collect.side_effect = [self.evidence, replace(self.evidence, pods=(replace(self.pod, log="rotated\n"),))]
        with patch(MODULE + "time.monotonic", side_effect=[0, 0, 0, 2]), patch(MODULE + "time.sleep"):
            result = self.invoke("--follow", "--observation-timeout", "1")
        self.assertEqual(3, result.return_value, result.exception)
        self.assertIn("changed or rotated", result.stdout)
        self.assertIn("no workloads were submitted", result.stderr)
        self.backend.create_job.assert_not_called()

    def test_follow_json_is_rejected_and_corrupt_export_does_not_fall_back(self) -> None:
        result = self.invoke("--follow", "--format", "json")
        self.assertIsInstance(result.exception, BenchmarkError)
        final = self.run_record.update_case(self.case.case_id, "CANCELLED", evidence_sha256="a" * 64)
        self.backend.get_run.return_value = final
        self.backend.get_job.return_value = None
        self.store.verify.side_effect = BenchmarkError("checksum differs")
        result = self.invoke("--format", "json")
        self.assertIsInstance(result.exception, BenchmarkError)
        self.assertEqual("", result.stdout)
