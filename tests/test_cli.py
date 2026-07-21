from __future__ import annotations

import json
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from click.testing import CliRunner

from oke_hpc_mgmt.cli import _configure_oci_cli_auth, _program_name, main
from oke_hpc_mgmt.commands import cli
from oke_hpc_mgmt.models import (
    AddonInfo,
    DiscoverySnapshot,
    NodeInfo,
    OperationPlan,
    WorkerPoolInfo,
)
from oke_hpc_mgmt.workflows.lifecycle import PreparedNodeRemoval, PreparedPoolResize
from oke_hpc_mgmt.workflows.node_maintenance import PreparedNodeMaintenance


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_global_help_exposes_modular_command_families(self):
        result = self.runner.invoke(cli, ["--help"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("Management CLI for OCI HPC OKE", result.output)
        for command in (
            "status",
            "pools",
            "nodes",
            "topology",
            "autoscaler",
            "addons",
            "health",
            "recommendations",
            "reconcile",
        ):
            self.assertIn(command, result.output)

    def test_node_lifecycle_help_exposes_remove_alias_and_maintenance(self):
        result = self.runner.invoke(cli, ["nodes", "--help"])

        self.assertEqual(0, result.exit_code)
        for command in ("remove", "terminate", "cordon", "drain", "uncordon"):
            self.assertIn(command, result.output)

    def test_pool_resize_help_defines_delta_signs(self):
        result = self.runner.invoke(cli, ["pools", "resize", "--help"])

        self.assertEqual(0, result.exit_code)
        self.assertIn(
            "positive adds nodes; negative removes nodes",
            " ".join(result.output.split()),
        )

    def test_kube_context_environment_variable_is_not_used(self):
        service = Mock()
        service.discover.return_value = DiscoverySnapshot()
        with (
            patch.dict(os.environ, {"KUBE_CONTEXT": "unexpected-context"}),
            patch("oke_hpc_mgmt.commands.common.DiscoveryService", return_value=service) as factory,
        ):
            result = self.runner.invoke(cli, ["--auth", "none", "pools", "list"])

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIsNone(factory.call_args.args[0].context)

    def test_explicit_context_override_is_supported(self):
        service = Mock()
        service.discover.return_value = DiscoverySnapshot()
        with patch(
            "oke_hpc_mgmt.commands.common.DiscoveryService", return_value=service
        ) as factory:
            result = self.runner.invoke(
                cli,
                ["--auth", "none", "--context", "operator-context", "pools", "list"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual("operator-context", factory.call_args.args[0].context)

    def test_instance_principal_is_propagated_to_kubeconfig_oci_exec(self):
        with patch.dict(os.environ, {}, clear=True):
            _configure_oci_cli_auth("instance_principal")

            self.assertEqual("instance_principal", os.environ["OCI_CLI_AUTH"])

    def test_explicit_kubeconfig_oci_exec_auth_is_preserved(self):
        with patch.dict(os.environ, {"OCI_CLI_AUTH": "security_token"}, clear=True):
            _configure_oci_cli_auth("instance_principal")

            self.assertEqual("security_token", os.environ["OCI_CLI_AUTH"])

    def test_config_file_auth_does_not_set_kubeconfig_oci_exec_auth(self):
        with patch.dict(os.environ, {}, clear=True):
            _configure_oci_cli_auth("config_file")

            self.assertNotIn("OCI_CLI_AUTH", os.environ)

    def test_direct_entrypoint_name(self):
        with patch.object(sys, "argv", ["/usr/local/bin/mgmt-oke"]):
            self.assertEqual("mgmt-oke", _program_name())

    def test_kubectl_plugin_name(self):
        with patch.object(sys, "argv", ["/usr/local/bin/kubectl-oke"]):
            self.assertEqual("kubectl oke", _program_name())

    def test_addons_status_requires_oci_auth_with_stable_exit_code(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, main(["--auth", "none", "addons", "status"]))

    def test_unknown_command_has_usage_exit_code(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, main(["does-not-exist"]))

    def test_status_is_degraded_when_discovery_is_partial(self):
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(
            warnings=["OCI discovery skipped: access denied"]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("oke_hpc_mgmt.commands.common.DiscoveryService", return_value=service),
            redirect_stderr(stderr),
            patch("sys.stdout", new=stdout),
        ):
            exit_status = main(["--auth", "none", "status"])

        self.assertEqual(1, exit_status)
        self.assertIn("DEGRADED", stdout.getvalue())
        self.assertIn("OCI discovery skipped", stderr.getvalue())

    def test_pool_resize_dry_run_prints_plan_without_execution(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            desired_size=2,
            node_pool_id="node-pool-1",
        )
        prepared = PreparedPoolResize(
            snapshot=DiscoverySnapshot(pools=[pool]),
            pool=pool,
            plan=OperationPlan(
                operation="pool-resize",
                target="oke-rdma",
                pool="oke-rdma",
                current_size=2,
                target_size=3,
                steps=("resize",),
            ),
        )
        with (
            patch("oke_hpc_mgmt.commands.pools.prepare_pool_resize", return_value=prepared),
            patch("oke_hpc_mgmt.commands.pools.execute_pool_resize") as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "--auth",
                    "instance_principal",
                    "pools",
                    "resize",
                    "oke-rdma",
                    "--delta",
                    "1",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(3, json.loads(result.output)[0]["target_size"])
        execute.assert_not_called()

    def test_pool_add_translates_count_to_positive_delta(self):
        pool = WorkerPoolInfo(name="oke-cpu", kind="node-pool", desired_size=1)
        prepared = PreparedPoolResize(
            snapshot=DiscoverySnapshot(pools=[pool]),
            pool=pool,
            plan=OperationPlan(operation="pool-resize", target="oke-cpu"),
        )
        with patch(
            "oke_hpc_mgmt.commands.pools.prepare_pool_resize", return_value=prepared
        ) as prepare:
            result = self.runner.invoke(
                cli,
                ["pools", "add", "oke-cpu", "--count", "2", "--dry-run"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(2, prepare.call_args.kwargs["delta"])

    def test_pool_remove_translates_count_to_negative_delta(self):
        pool = WorkerPoolInfo(name="oke-cpu", kind="node-pool", desired_size=3)
        prepared = PreparedPoolResize(
            snapshot=DiscoverySnapshot(pools=[pool]),
            pool=pool,
            plan=OperationPlan(operation="pool-resize", target="oke-cpu"),
        )
        with patch(
            "oke_hpc_mgmt.commands.pools.prepare_pool_resize", return_value=prepared
        ) as prepare:
            result = self.runner.invoke(
                cli,
                ["pools", "remove", "oke-cpu", "--count", "2", "--dry-run"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(-2, prepare.call_args.kwargs["delta"])

    def test_nodes_list_filters_projects_sorts_and_prints_one_line(self):
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(
            nodes=[
                NodeInfo("gpu-b", pool_name="oke-gpu", ready=True),
                NodeInfo("gpu-a", pool_name="oke-gpu", ready=True),
                NodeInfo("cpu-a", pool_name="oke-cpu", ready=True),
            ]
        )
        with patch("oke_hpc_mgmt.commands.common.DiscoveryService", return_value=service):
            result = self.runner.invoke(
                cli,
                [
                    "--auth",
                    "none",
                    "nodes",
                    "list",
                    "--fields",
                    "pool=oke-gpu,ready=true",
                    "--columns",
                    "name,pool,ready,schedulable",
                    "--sort",
                    "name",
                    "--one-line",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual("gpu-a,gpu-b", result.output.strip())

    def test_nodes_list_projects_ready_and_schedulable_columns(self):
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(
            nodes=[NodeInfo("gpu-a", ready=True, schedulable=False)]
        )
        with patch("oke_hpc_mgmt.commands.common.DiscoveryService", return_value=service):
            result = self.runner.invoke(
                cli,
                [
                    "--auth",
                    "none",
                    "nodes",
                    "list",
                    "--columns",
                    "name,ready,schedulable",
                    "--sort",
                    "schedulable,name",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("gpu-a", result.output)
        self.assertIn("yes", result.output)
        self.assertIn("no", result.output)

    def test_nodes_remove_alias_uses_shared_termination_dry_run(self):
        node = NodeInfo("cpu-1", pool_name="oke-cpu", instance_ocid="instance-1")
        pool = WorkerPoolInfo("oke-cpu", "node-pool", desired_size=1)
        prepared = PreparedNodeRemoval(
            snapshot=DiscoverySnapshot(pools=[pool], nodes=[node]),
            nodes=(node,),
            pools={"oke-cpu": pool},
            plans=(OperationPlan("node-remove", "cpu-1", pool="oke-cpu"),),
            drain_pods={},
            target_sizes={"oke-cpu": 0},
            decrement_size=True,
        )
        with (
            patch("oke_hpc_mgmt.commands.nodes.prepare_node_removal", return_value=prepared),
            patch("oke_hpc_mgmt.commands.nodes.execute_node_removal") as execute,
        ):
            result = self.runner.invoke(
                cli,
                ["nodes", "remove", "cpu-1", "--dry-run", "--format", "json"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual("node-remove", json.loads(result.output)[0]["operation"])
        execute.assert_not_called()

    def test_node_maintenance_dry_run_never_executes(self):
        node = NodeInfo("cpu-1", pool_name="oke-cpu", ready=True)
        prepared = PreparedNodeMaintenance(
            action="cordon",
            snapshot=DiscoverySnapshot(nodes=[node]),
            nodes=(node,),
            plans=(OperationPlan("node-cordon", "cpu-1", owner="kubernetes"),),
            drain_pods={},
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.nodes.prepare_node_maintenance",
                return_value=prepared,
            ),
            patch("oke_hpc_mgmt.commands.nodes.execute_node_maintenance") as execute,
        ):
            result = self.runner.invoke(
                cli,
                ["nodes", "cordon", "cpu-1", "--dry-run"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        execute.assert_not_called()

    def test_status_process_exit_reflects_health_severity(self):
        healthy = DiscoverySnapshot(
            pools=[
                WorkerPoolInfo(
                    "oke-cpu",
                    "node-pool",
                    desired_size=1,
                    active_oci_instances=1,
                    ready_k8s_nodes=1,
                )
            ],
            nodes=[NodeInfo("cpu-1", pool_name="oke-cpu", ready=True)],
            addons=[AddonInfo("CoreDNS", "ACTIVE")],
        )
        service = Mock()
        service.discover.return_value = healthy
        with (
            patch("oke_hpc_mgmt.commands.common.DiscoveryService", return_value=service),
            redirect_stderr(io.StringIO()),
            patch("sys.stdout", new=io.StringIO()),
        ):
            healthy_status = main(["--auth", "none", "status"])
            healthy.nodes[0].schedulable = False
            warning_status = main(["--auth", "none", "status"])
            healthy.nodes[0].ready = False
            failed_status = main(["--auth", "none", "status"])

        self.assertEqual(0, healthy_status)
        self.assertEqual(1, warning_status)
        self.assertEqual(2, failed_status)


if __name__ == "__main__":
    unittest.main()
