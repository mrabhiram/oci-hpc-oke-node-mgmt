from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner

from oke_hpc_mgmt.cli import _configure_oci_cli_auth, _program_name, main
from oke_hpc_mgmt.commands import cli
from oke_hpc_mgmt.models import (
    AddonInfo,
    ClusterInfo,
    DiscoverySnapshot,
    NodeInfo,
    OperationPlan,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
    WorkerPoolInfo,
)
from oke_hpc_mgmt.workflows.lifecycle import (
    PreparedNodeBootVolumeReplace,
    PreparedNodeRemoval,
    PreparedPoolBootVolumeReplace,
    PreparedPoolCreate,
    PreparedPoolDelete,
    PreparedPoolResize,
)
from oke_hpc_mgmt.workflows.node_maintenance import PreparedNodeMaintenance
from oke_hpc_mgmt.workflows.upgrades import (
    PoolUpgradeSpec,
    PreparedPoolUpgrade,
    UpgradeExecutionResult,
)
from oke_hpc_mgmt.upgrades import UpgradeGateEvidence


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
            "upgrades",
        ):
            self.assertIn(command, result.output)

    def test_upgrade_help_is_wired_to_cluster_pool_and_orchestration_groups(self):
        for arguments, commands in (
            (["clusters", "--help"], ("upgrade",)),
            (["pools", "--help"], ("upgrade",)),
            (
                ["upgrades", "--help"],
                ("status", "plan", "apply", "resume", "abandon", "cleanup"),
            ),
        ):
            result = self.runner.invoke(cli, arguments)
            self.assertEqual(0, result.exit_code, result.output)
            for command in commands:
                self.assertIn(command, result.output)

    def test_pool_upgrade_dry_run_never_executes(self):
        pool = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            kubernetes_version="v1.35.2",
        )
        prepared = PreparedPoolUpgrade(
            snapshot=DiscoverySnapshot(pools=[pool]),
            pool=pool,
            target_version="v1.36.1",
            spec=PoolUpgradeSpec(),
            strategy="boot-volume-replace",
            evidence=UpgradeGateEvidence(
                pool=pool.name,
                nodes=("gpu-1",),
                ready=True,
                externally_cordoned=False,
            ),
            plan=OperationPlan(
                operation="worker-pool-upgrade",
                target=pool.name,
            ),
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.upgrades.prepare_pool_upgrade",
                return_value=prepared,
            ),
            patch(
                "oke_hpc_mgmt.commands.upgrades.execute_pool_upgrade"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "pools",
                    "upgrade",
                    "oke-gpu",
                    "--to",
                    "v1.36.1",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(
            "worker-pool-upgrade",
            json.loads(result.output)[0]["operation"],
        )
        execute.assert_not_called()

    def test_pool_upgrade_action_required_has_stable_exit_code(self):
        pool = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
        )
        prepared = PreparedPoolUpgrade(
            snapshot=DiscoverySnapshot(pools=[pool]),
            pool=pool,
            target_version="v1.36.1",
            spec=PoolUpgradeSpec(),
            strategy="blue-green",
            evidence=UpgradeGateEvidence(
                pool=pool.name,
                nodes=("gpu-1",),
                ready=True,
                externally_cordoned=True,
            ),
            plan=OperationPlan(
                operation="worker-pool-upgrade",
                target=pool.name,
            ),
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.upgrades.prepare_pool_upgrade",
                return_value=prepared,
            ),
            patch(
                "oke_hpc_mgmt.commands.upgrades.execute_pool_upgrade",
                return_value=UpgradeExecutionResult(
                    operation="worker-pool-upgrade",
                    target=pool.name,
                    status="action-required",
                ),
            ),
            patch("sys.stdout", new=io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_status = main(
                [
                    "pools",
                    "upgrade",
                    "oke-gpu",
                    "--to",
                    "v1.36.1",
                    "--ack-application-compatibility",
                    "--ack-iac-drift",
                    "--ack-workloads-drained",
                    "--yes",
                ],
            )

        self.assertEqual(3, exit_status)

    def test_upgrade_status_reports_control_plane_and_addons_without_target(self):
        snapshot = DiscoverySnapshot(
            cluster=ClusterInfo(
                cluster_id="cluster-1",
                compartment_id="compartment-1",
                kubernetes_version="v1.35.2",
                lifecycle_state="ACTIVE",
                available_kubernetes_versions=(
                    "v1.35.2",
                    "v1.36.1",
                ),
            ),
            addons=[
                AddonInfo(
                    name="NvidiaGpuOperator",
                    lifecycle_state="ACTIVE",
                    version="v25.3",
                    update_mode="AUTOMATIC",
                )
            ],
        )
        service = Mock()
        service.discover.return_value = snapshot

        with patch(
            "oke_hpc_mgmt.commands.upgrades.CliState.service",
            return_value=service,
        ):
            result = self.runner.invoke(
                cli,
                ["upgrades", "status", "--format", "json"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        rows = json.loads(result.output)
        self.assertEqual("control-plane", rows[0]["kind"])
        self.assertEqual("addon", rows[1]["kind"])
        self.assertEqual("AUTOMATIC", rows[1]["strategy"])

    def test_upgrade_plan_dry_run_executes_every_preflight_but_no_mutation(self):
        plan = SimpleNamespace(
            plans=(
                OperationPlan(
                    operation="control-plane-upgrade",
                    target="v1.36.1",
                ),
                OperationPlan(
                    operation="worker-pool-upgrade",
                    target="oke-gpu",
                ),
            ),
            snapshot=DiscoverySnapshot(),
            target_version="v1.36.1",
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.upgrades.prepare_cluster_upgrade_plan",
                return_value=plan,
            ) as prepare,
            patch(
                "oke_hpc_mgmt.commands.upgrades.execute_upgrade_apply"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "upgrades",
                    "plan",
                    "--to",
                    "v1.36",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(2, len(json.loads(result.output)))
        prepare.assert_called_once()
        execute.assert_not_called()

    def test_control_plane_upgrade_dry_run_never_executes(self):
        prepared = SimpleNamespace(
            plans=(
                OperationPlan(
                    operation="control-plane-upgrade",
                    target="v1.36.1",
                ),
            ),
            target_version="v1.36.1",
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.upgrades.prepare_control_plane_upgrade",
                return_value=prepared,
            ),
            patch(
                "oke_hpc_mgmt.commands.upgrades.execute_control_plane_upgrade"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "clusters",
                    "upgrade",
                    "--to",
                    "v1.36",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        execute.assert_not_called()

    def test_upgrade_apply_dry_run_never_creates_checkpoint_or_mutates(self):
        plan = SimpleNamespace(
            plans=(
                OperationPlan(
                    operation="control-plane-upgrade",
                    target="v1.36.1",
                ),
            ),
            snapshot=DiscoverySnapshot(),
            target_version="v1.36.1",
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.upgrades.prepare_cluster_upgrade_plan",
                return_value=plan,
            ),
            patch(
                "oke_hpc_mgmt.commands.upgrades.execute_upgrade_apply"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "upgrades",
                    "apply",
                    "--to",
                    "v1.36",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        execute.assert_not_called()

    def test_upgrade_resume_abandon_and_cleanup_commands_are_wired(self):
        result_row = UpgradeExecutionResult(
            operation="cluster-upgrade",
            target="v1.36.1",
            status="completed",
        )
        cases = (
            (
                ["upgrades", "resume", "--format", "json"],
                "resume_upgrade",
            ),
            (
                [
                    "upgrades",
                    "abandon",
                    "--yes",
                    "--format",
                    "json",
                ],
                "abandon_upgrade",
            ),
            (
                [
                    "upgrades",
                    "cleanup",
                    "--yes",
                    "--format",
                    "json",
                ],
                "cleanup_upgrade",
            ),
        )
        for arguments, function_name in cases:
            with self.subTest(command=arguments[1]):
                return_value = (
                    [result_row]
                    if function_name == "resume_upgrade"
                    else result_row
                )
                with patch(
                    f"oke_hpc_mgmt.commands.upgrades.{function_name}",
                    return_value=return_value,
                ) as command:
                    result = self.runner.invoke(cli, arguments)

                self.assertEqual(0, result.exit_code, result.output)
                self.assertEqual(
                    "cluster-upgrade",
                    json.loads(result.output)[0]["operation"],
                )
                command.assert_called_once()

    def test_node_lifecycle_help_exposes_remove_alias_and_maintenance(self):
        result = self.runner.invoke(cli, ["nodes", "--help"])

        self.assertEqual(0, result.exit_code)
        for command in (
            "remove",
            "terminate",
            "boot-volume-replace",
            "boot-volume-swap",
            "bvr",
            "cordon",
            "drain",
            "uncordon",
        ):
            self.assertIn(command, result.output)

    def test_bvr_help_distinguishes_individual_and_pool_image_behavior(self):
        node_result = self.runner.invoke(
            cli,
            ["nodes", "boot-volume-replace", "--help"],
        )
        pool_result = self.runner.invoke(
            cli,
            ["pools", "boot-volume-replace", "--help"],
        )

        self.assertEqual(0, node_result.exit_code, node_result.output)
        self.assertIn(
            "preserves the current image",
            " ".join(node_result.output.split()),
        )
        self.assertNotIn("--image-id", node_result.output)
        self.assertEqual(0, pool_result.exit_code, pool_result.output)
        self.assertIn("--image-id", pool_result.output)
        self.assertIn("--maximum-unavailable", pool_result.output)

    def test_pool_resize_help_defines_delta_signs(self):
        result = self.runner.invoke(cli, ["pools", "resize", "--help"])

        self.assertEqual(0, result.exit_code)
        self.assertIn(
            "positive adds nodes; negative removes nodes",
            " ".join(result.output.split()),
        )

    def test_pool_help_exposes_create_command(self):
        result = self.runner.invoke(cli, ["pools", "--help"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("create", result.output)
        self.assertIn("Manage the lifecycle", result.output)

        create_result = self.runner.invoke(cli, ["pools", "create", "--help"])
        self.assertEqual(0, create_result.exit_code, create_result.output)
        for option in (
            "--rdma-mode",
            "--compute-cluster-id",
            "--compute-cluster-name",
            "--host-group-id",
        ):
            self.assertIn(option, create_result.output)

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

    def test_pool_create_dry_run_prints_plan_without_execution(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            cluster_network_id="cluster-network-1",
            instance_pool_id="instance-pool-1",
        )
        prepared = PreparedPoolCreate(
            snapshot=DiscoverySnapshot(pools=[source]),
            source_pool=source,
            name="oke-rdma-2",
            count=2,
            spec=PoolCreateSpec(pool_type="rdma"),
            plan=OperationPlan(
                operation="pool-create",
                target="oke-rdma-2",
                pool="oke-rdma-2",
                current_size=0,
                target_size=2,
            ),
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.pools.prepare_pool_create",
                return_value=prepared,
            ) as prepare,
            patch("oke_hpc_mgmt.commands.pools.execute_pool_create") as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "pools",
                    "create",
                    "oke-rdma-2",
                    "--type",
                    "rdma",
                    "--count",
                    "2",
                    "--from-pool",
                    "oke-rdma",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual("pool-create", json.loads(result.output)[0]["operation"])
        prepare.assert_called_once()
        self.assertEqual("oke-rdma", prepare.call_args.kwargs["source_identifier"])
        execute.assert_not_called()

    def test_pool_create_requires_explicit_type(self):
        result = self.runner.invoke(
            cli,
            [
                "pools",
                "create",
                "new-pool",
                "--count",
                "1",
                "--dry-run",
            ],
        )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("--type", result.output)

    def test_pool_create_builds_managed_custom_image_and_storage_spec(self):
        source = WorkerPoolInfo(
            name="oke-gpu",
            kind="node-pool",
            node_pool_id="node-pool-1",
            gpu_resource="nvidia.com/gpu",
        )
        prepared = PreparedPoolCreate(
            snapshot=DiscoverySnapshot(pools=[source]),
            source_pool=source,
            name="gpu-batch",
            count=2,
            spec=PoolCreateSpec(pool_type="gpu"),
            plan=OperationPlan(
                operation="pool-create",
                target="gpu-batch",
            ),
        )
        with patch(
            "oke_hpc_mgmt.commands.pools.prepare_pool_create",
            return_value=prepared,
        ) as prepare:
            result = self.runner.invoke(
                cli,
                [
                    "pools",
                    "create",
                    "gpu-batch",
                    "--type",
                    "gpu",
                    "--count",
                    "2",
                    "--from-pool",
                    "oke-gpu",
                    "--availability-domain",
                    "AD-2",
                    "--shape",
                    "VM.GPU.A10.2",
                    "--image-id",
                    "image-custom",
                    "--storage-mode",
                    "replace",
                    "--fss-mount-target-ip",
                    "10.0.0.5",
                    "--fss-export-path",
                    "/training",
                    "--dry-run",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        spec = prepare.call_args.kwargs["spec"]
        self.assertEqual("gpu", spec.pool_type)
        self.assertEqual("AD-2", spec.availability_domain)
        self.assertEqual("VM.GPU.A10.2", spec.shape)
        self.assertEqual("image-custom", spec.image_id)
        self.assertEqual("/mnt/oci-fss", spec.fss_mounts[0].mount_path)

    def test_pool_create_builds_compute_cluster_and_host_group_spec(self):
        source = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="node-pool-rdma",
            compute_cluster_id="compute-cluster-source",
            rdma_enabled=True,
        )
        prepared = PreparedPoolCreate(
            snapshot=DiscoverySnapshot(pools=[source]),
            source_pool=source,
            name="rdma-batch",
            count=2,
            spec=PoolCreateSpec(pool_type="rdma"),
            plan=OperationPlan(operation="pool-create", target="rdma-batch"),
        )
        with patch(
            "oke_hpc_mgmt.commands.pools.prepare_pool_create",
            return_value=prepared,
        ) as prepare:
            result = self.runner.invoke(
                cli,
                [
                    "pools",
                    "create",
                    "rdma-batch",
                    "--type",
                    "rdma",
                    "--rdma-mode",
                    "compute-cluster",
                    "--count",
                    "2",
                    "--from-pool",
                    "oke-rdma",
                    "--bootstrap-from-pool",
                    "legacy-rdma",
                    "--compute-cluster-id",
                    "compute-cluster-target",
                    "--host-group-id",
                    "host-group-1",
                    "--availability-domain",
                    "AD-1",
                    "--dry-run",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        spec = prepare.call_args.kwargs["spec"]
        self.assertEqual("compute-cluster", spec.rdma_mode)
        self.assertEqual("compute-cluster-target", spec.compute_cluster_id)
        self.assertEqual("host-group-1", spec.host_group_id)
        self.assertFalse(spec.creates_compute_cluster)
        self.assertEqual(
            "legacy-rdma",
            prepare.call_args.kwargs["bootstrap_source_identifier"],
        )

    def test_pool_create_rejects_storage_without_composition_mode(self):
        result = self.runner.invoke(
            cli,
            [
                "pools",
                "create",
                "cpu-new",
                "--type",
                "cpu",
                "--count",
                "1",
                "--fss-mount-target-ip",
                "10.0.0.5",
                "--fss-export-path",
                "/training",
                "--dry-run",
            ],
        )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("storage-mode", str(result.exception))

    def test_clusters_group_exposes_slurm_style_pool_lifecycle_aliases(self):
        result = self.runner.invoke(cli, ["clusters", "--help"])
        add_result = self.runner.invoke(cli, ["clusters", "add", "--help"])

        self.assertEqual(0, result.exit_code, result.output)
        for command in ("list", "create", "delete", "add"):
            self.assertIn(command, result.output)
        self.assertEqual(0, add_result.exit_code, add_result.output)
        self.assertIn("node", add_result.output)
        self.assertIn("OKE control plane", result.output)

    def test_pool_delete_dry_run_does_not_execute(self):
        pool = WorkerPoolInfo(
            name="cpu-batch",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=1,
        )
        prepared = PreparedPoolDelete(
            snapshot=DiscoverySnapshot(pools=[pool]),
            pool=pool,
            nodes=(),
            drain_pods={},
            allow_workloads=False,
            delete_emptydir_data=False,
            force_unmanaged=False,
            plan=OperationPlan(
                operation="pool-delete",
                target="cpu-batch",
                pool="cpu-batch",
            ),
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.pools.prepare_pool_delete",
                return_value=prepared,
            ),
            patch(
                "oke_hpc_mgmt.commands.pools.execute_pool_delete"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "pools",
                    "delete",
                    "cpu-batch",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(
            "pool-delete",
            json.loads(result.output)[0]["operation"],
        )
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
                [
                    "nodes",
                    "remove",
                    "cpu-1",
                    "--tag",
                    "none",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        plan = json.loads(result.output)[0]
        self.assertEqual("node-remove", plan["operation"])
        self.assertEqual(
            "not-requested",
            plan["details"]["customer_reported_host_status"],
        )
        execute.assert_not_called()

    def test_node_termination_tag_option_is_visible_and_validated(self):
        help_result = self.runner.invoke(cli, ["nodes", "terminate", "--help"])
        invalid_result = self.runner.invoke(
            cli,
            ["nodes", "terminate", "cpu-1", "--tag", "broken"],
        )

        self.assertEqual(0, help_result.exit_code, help_result.output)
        normalized_help = " ".join(help_result.output.split())
        self.assertIn("--tag [unhealthy|none]", normalized_help)
        self.assertIn(
            "Omission prompts for each node, including with --yes",
            normalized_help,
        )
        self.assertNotEqual(0, invalid_result.exit_code)
        self.assertIn("Invalid value for '--tag'", invalid_result.output)

    def test_explicit_unhealthy_tag_is_in_node_removal_dry_run(self):
        node = NodeInfo("gpu-1", pool_name="oke-gpu", instance_ocid="instance-1")
        pool = WorkerPoolInfo("oke-gpu", "node-pool", desired_size=1)
        prepared = PreparedNodeRemoval(
            snapshot=DiscoverySnapshot(pools=[pool], nodes=[node]),
            nodes=(node,),
            pools={pool.name: pool},
            plans=(
                OperationPlan(
                    "node-remove",
                    node.k8s_name,
                    pool=pool.name,
                    steps=("delete the selected worker through OKE DeleteNode",),
                ),
            ),
            drain_pods={},
            target_sizes={pool.name: 0},
            decrement_size=True,
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.nodes.prepare_node_removal",
                return_value=prepared,
            ),
            patch("oke_hpc_mgmt.commands.nodes.execute_node_removal") as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "nodes",
                    "terminate",
                    node.k8s_name,
                    "--tag",
                    "unhealthy",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        plan = json.loads(result.output)[0]
        self.assertEqual(
            "unhealthy",
            plan["details"]["customer_reported_host_status"],
        )
        self.assertIn("tag OCI instance", " ".join(plan["steps"]))
        execute.assert_not_called()

    def test_omitted_tag_prompts_even_with_yes(self):
        node = NodeInfo("gpu-1", pool_name="oke-gpu", instance_ocid="instance-1")
        pool = WorkerPoolInfo("oke-gpu", "node-pool", desired_size=1)
        prepared = PreparedNodeRemoval(
            snapshot=DiscoverySnapshot(pools=[pool], nodes=[node]),
            nodes=(node,),
            pools={pool.name: pool},
            plans=(OperationPlan("node-remove", node.k8s_name, pool=pool.name),),
            drain_pods={},
            target_sizes={pool.name: 0},
            decrement_size=True,
        )
        with patch(
            "oke_hpc_mgmt.commands.nodes.prepare_node_removal",
            return_value=prepared,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "nodes",
                    "terminate",
                    node.k8s_name,
                    "--yes",
                    "--dry-run",
                    "--format",
                    "json",
                ],
                input="y\n",
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn(
            "Is gpu-1 unhealthy and should it be tagged before termination?",
            result.output,
        )
        self.assertIn('"customer_reported_host_status": "unhealthy"', result.output)

    def test_omitted_tag_can_be_declined(self):
        node = NodeInfo("cpu-1", pool_name="oke-cpu", instance_ocid="instance-1")
        pool = WorkerPoolInfo("oke-cpu", "node-pool", desired_size=1)
        prepared = PreparedNodeRemoval(
            snapshot=DiscoverySnapshot(pools=[pool], nodes=[node]),
            nodes=(node,),
            pools={pool.name: pool},
            plans=(OperationPlan("node-remove", node.k8s_name, pool=pool.name),),
            drain_pods={},
            target_sizes={pool.name: 0},
            decrement_size=True,
        )
        with patch(
            "oke_hpc_mgmt.commands.nodes.prepare_node_removal",
            return_value=prepared,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "nodes",
                    "remove",
                    node.k8s_name,
                    "--dry-run",
                    "--format",
                    "json",
                ],
                input="n\n",
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn('"customer_reported_host_status": "not-requested"', result.output)

    def test_individual_node_bvr_dry_run_never_executes(self):
        node = NodeInfo(
            "gpu-1",
            pool_name="oke-gpu",
            instance_ocid="instance-1",
        )
        pool = WorkerPoolInfo(
            "oke-gpu",
            "node-pool",
            desired_size=1,
            node_pool_id="node-pool-1",
        )
        prepared = PreparedNodeBootVolumeReplace(
            snapshot=DiscoverySnapshot(pools=[pool], nodes=[node]),
            nodes=(node,),
            pools={"oke-gpu": pool},
            plans=(
                OperationPlan(
                    "node-boot-volume-replace",
                    "gpu-1",
                    pool="oke-gpu",
                ),
            ),
            old_boot_volume_ids={"instance-1": "boot-volume-old"},
            drain_pods={},
            delete_emptydir_data=False,
            force_unmanaged=False,
            allow_system_pool=False,
            eviction_grace_duration="PT60M",
            force_after_grace=False,
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.nodes.prepare_node_boot_volume_replace",
                return_value=prepared,
            ),
            patch(
                "oke_hpc_mgmt.commands.nodes.execute_node_boot_volume_replace"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "nodes",
                    "bvr",
                    "gpu-1",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(
            "node-boot-volume-replace",
            json.loads(result.output)[0]["operation"],
        )
        execute.assert_not_called()

    def test_managed_pool_bvr_dry_run_builds_image_update(self):
        pool = WorkerPoolInfo(
            "oke-gpu",
            "node-pool",
            desired_size=1,
            node_pool_id="node-pool-1",
        )
        node = NodeInfo(
            "gpu-1",
            pool_name=pool.name,
            instance_ocid="instance-1",
        )
        prepared = PreparedPoolBootVolumeReplace(
            snapshot=DiscoverySnapshot(pools=[pool], nodes=[node]),
            pool=pool,
            nodes=(node,),
            old_boot_volume_ids={"instance-1": "boot-volume-old"},
            drain_pods={},
            spec=PoolBootVolumeReplaceSpec(image_id="image-new"),
            delete_emptydir_data=False,
            force_unmanaged=False,
            allow_system_pool=False,
            plan=OperationPlan(
                "pool-boot-volume-replace",
                pool.name,
                pool=pool.name,
            ),
        )
        with (
            patch(
                "oke_hpc_mgmt.commands.pools.prepare_pool_boot_volume_replace",
                return_value=prepared,
            ) as prepare,
            patch(
                "oke_hpc_mgmt.commands.pools.execute_pool_boot_volume_replace"
            ) as execute,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "pools",
                    "bvr",
                    "oke-gpu",
                    "--image-id",
                    "image-new",
                    "--maximum-unavailable",
                    "1",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(
            "pool-boot-volume-replace",
            json.loads(result.output)[0]["operation"],
        )
        self.assertEqual(
            "image-new",
            prepare.call_args.args[2].image_id,
        )
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
