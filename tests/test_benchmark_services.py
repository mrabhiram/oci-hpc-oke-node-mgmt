from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from click.testing import CliRunner

from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.workflow import BenchmarkWorkflow
from oke_hpc_mgmt.commands import cli
from oke_hpc_mgmt.commands.services import select_refresh_runs
from test_benchmark_records import RUN, record


MODULE = "oke_hpc_mgmt.commands.services."


class MetadataServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = Mock(spec=BenchmarkBackend)
        self.run = replace(record(), uid="run-uid", resource_version="1")
        self.backend.list_runs.return_value = (self.run,)
        self.backend.get_run.return_value = self.run

    def test_selection_is_namespace_and_pool_scoped_without_truncation(self) -> None:
        self.assertEqual((self.run,), select_refresh_runs(self.backend, "default", (), "gpu-pool", 1))
        self.backend.list_runs.assert_called_once_with("default")
        self.assertEqual((), select_refresh_runs(self.backend, "default", (), "other-pool", 1))
        self.backend.list_runs.return_value = (self.run, self.run)
        with self.assertRaisesRegex(BenchmarkError, "Too many"):
            select_refresh_runs(self.backend, "default", (), None, 1)

    def test_explicit_selection_validates_all_ids_before_reading_and_never_lists(self) -> None:
        self.assertEqual((self.run,), select_refresh_runs(self.backend, "default", (RUN,), None, 1))
        self.backend.get_run.assert_called_once_with("default", RUN)
        self.backend.list_runs.assert_not_called()
        self.backend.get_run.reset_mock()
        for ids, pool, maximum in (((RUN, RUN), None, 2), ((RUN,), "pool", 2),
                                   ((RUN, "invalid"), None, 2), ((RUN, "hc-" + "f" * 20), None, 1),
                                   ((), None, True), ((), None, 0), ((), None, 129)):
            with self.subTest(ids=ids, pool=pool, maximum=maximum), self.assertRaises(BenchmarkError):
                select_refresh_runs(self.backend, "default", ids, pool, maximum)
        self.backend.get_run.assert_not_called()

    def test_default_skips_terminal_and_cancelling_but_explicit_id_can_report_them(self) -> None:
        terminal = self.run
        for case in terminal.cases:
            terminal = terminal.update_case(case.case_id, "CANCELLED")
        cancelling = replace(self.run, state="CANCELLING")
        self.backend.list_runs.return_value = (terminal, cancelling)
        self.assertEqual((), select_refresh_runs(self.backend, "default", (), None, 32))
        self.backend.get_run.return_value = terminal
        self.assertEqual((terminal,), select_refresh_runs(self.backend, "default", (RUN,), None, 32))

    def test_missing_uid_and_wrong_namespace_are_rejected(self) -> None:
        for run in (replace(self.run, uid=None), replace(self.run, plan=replace(
            self.run.plan, request=replace(self.run.plan.request, namespace="other"),
        ))):
            self.backend.get_run.return_value = run
            with self.assertRaisesRegex(BenchmarkError, "identity or namespace"):
                select_refresh_runs(self.backend, "default", (RUN,), None, 32)

    def test_sorting_is_deterministic(self) -> None:
        older = replace(self.run, created_at="2020-01-01T00:00:00+00:00")
        newer = replace(self.run, created_at="2021-01-01T00:00:00+00:00")
        self.backend.list_runs.return_value = (newer, older)
        self.assertEqual((older, newer), select_refresh_runs(self.backend, "default", (), None, 2))

    def test_cli_dry_run_creates_no_files_or_workflow_and_keeps_json_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(MODULE + "CliState.service"), \
                patch(MODULE + "BenchmarkBackend", return_value=self.backend), \
                patch(MODULE + "BenchmarkWorkflow") as workflow:
            directory = Path(temporary) / "not-created"
            result = CliRunner(mix_stderr=False).invoke(cli, [
                "services", "update-metadata", "--run-id", RUN, "--dry-run",
                "--output-dir", str(directory), "--format", "json",
            ], standalone_mode=False)
            self.assertEqual(0, result.return_value, result.exception)
            self.assertEqual(RUN, json.loads(result.stdout)[0]["run_id"])
            self.assertFalse(directory.exists())
            workflow.assert_not_called()

    def test_cli_confirmation_and_yes_only_refresh_selected_checkpoint_uids(self) -> None:
        workflow = Mock(spec=BenchmarkWorkflow)
        workflow.refresh.return_value = self.run
        with patch(MODULE + "CliState.service"), patch(MODULE + "BenchmarkBackend", return_value=self.backend), \
                patch(MODULE + "BenchmarkWorkflow", return_value=workflow), \
                patch(MODULE + "confirm_plans") as confirm:
            result = CliRunner(mix_stderr=False).invoke(cli, [
                "services", "update-metadata", "--run-id", RUN, "--yes", "--format", "json",
            ], standalone_mode=False)
            self.assertEqual(0, result.return_value, result.exception)
            self.assertEqual(("default", True), confirm.call_args.args[1:])
            workflow.refresh.assert_called_once_with("default", RUN, expected_uid="run-uid")
            for name in ("submit", "advance", "wait", "cancel", "cleanup"):
                getattr(workflow, name).assert_not_called()
            self.assertEqual("IN_PROGRESS", json.loads(result.stdout)[0]["outcome"])

    def test_declined_confirmation_or_empty_selection_never_refreshes(self) -> None:
        with patch(MODULE + "CliState.service"), patch(MODULE + "BenchmarkBackend", return_value=self.backend), \
                patch(MODULE + "BenchmarkWorkflow") as workflow:
            result = CliRunner().invoke(cli, ["services", "update-metadata"], input="no\n", standalone_mode=False)
            self.assertNotEqual(0, result.exit_code)
            workflow.assert_not_called()
            self.backend.list_runs.return_value = ()
            result = CliRunner().invoke(cli, ["services", "update-metadata", "--format", "json"], standalone_mode=False)
            self.assertEqual(0, result.return_value, result.exception)
            self.assertEqual([], json.loads(result.stdout))
            workflow.assert_not_called()

    def test_help_never_connects_and_describes_observation_only_contract(self) -> None:
        with patch(MODULE + "CliState.service") as service:
            for arguments in (["services", "--help"], ["services", "update-metadata", "--help"]):
                result = CliRunner().invoke(cli, arguments)
                self.assertEqual(0, result.exit_code, result.output)
                self.assertIn("update-metadata", result.output)
            self.assertIn("Never submit, cancel", result.output)
            service.assert_not_called()
