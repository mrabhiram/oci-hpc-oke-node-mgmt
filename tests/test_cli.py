import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from oke_hpc_mgmt.cli import (
    _configure_oci_cli_auth,
    _program_name,
    _readiness_status,
    _resource_counts_match,
    build_parser,
    cmd_addons_status,
    cmd_nodes_remove,
    cmd_pools_resize,
)
from oke_hpc_mgmt.models import (
    AddonInfo,
    DiscoverySnapshot,
    NodeInfo,
    PoolResourceReadiness,
    WorkerPoolInfo,
)


class CliTests(unittest.TestCase):
    def test_kube_context_environment_variable_is_not_used(self):
        with patch.dict(os.environ, {"KUBE_CONTEXT": "unexpected-context"}):
            args = build_parser().parse_args(["pools", "list"])

        self.assertIsNone(args.context)

    def test_explicit_context_override_is_supported(self):
        args = build_parser().parse_args(
            ["--context", "operator-context", "pools", "list"]
        )

        self.assertEqual("operator-context", args.context)

    def test_global_help_describes_target_discovery_modes(self):
        help_text = " ".join(build_parser().format_help().split())

        self.assertIn("resolved from the OKE cluster", help_text)
        self.assertIn("read from the selected kubeconfig context", help_text)
        self.assertIn("Automatic kubeconfig target discovery is unavailable", help_text)
        self.assertIn("Skip OCI inventory and disable OCI-backed mutations", help_text)

    def test_instance_principal_is_propagated_to_kubeconfig_oci_exec(self):
        with patch.dict(os.environ, {}, clear=True):
            _configure_oci_cli_auth("instance_principal")

            self.assertEqual("instance_principal", os.environ["OCI_CLI_AUTH"])

    def test_explicit_kubeconfig_oci_exec_auth_is_preserved(self):
        with patch.dict(os.environ, {"OCI_CLI_AUTH": "security_token"}, clear=True):
            _configure_oci_cli_auth("instance_principal")

            self.assertEqual("security_token", os.environ["OCI_CLI_AUTH"])

    def test_empty_kubeconfig_oci_exec_auth_uses_selected_principal(self):
        with patch.dict(os.environ, {"OCI_CLI_AUTH": ""}, clear=True):
            _configure_oci_cli_auth("resource_principal")

            self.assertEqual("resource_principal", os.environ["OCI_CLI_AUTH"])

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

    def test_addons_status_is_registered(self):
        args = build_parser().parse_args(
            [
                "--auth",
                "instance_principal",
                "--cluster-id",
                "cluster-1",
                "addons",
                "status",
            ]
        )

        self.assertIs(cmd_addons_status, args.handler)

    def test_addons_status_requires_oci_auth(self):
        args = build_parser().parse_args(["--auth", "none", "addons", "status"])
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = cmd_addons_status(args)

        self.assertEqual(2, result)
        self.assertIn("requires OCI auth", stderr.getvalue())

    def test_addons_status_resolves_cluster_from_kubeconfig(self):
        args = build_parser().parse_args(
            ["--auth", "instance_principal", "addons", "status"]
        )
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(
            addons=[AddonInfo(name="NodeFeatureDiscovery", lifecycle_state="ACTIVE")]
        )

        with (
            patch("oke_hpc_mgmt.cli.DiscoveryService", return_value=service),
            redirect_stdout(io.StringIO()),
        ):
            result = cmd_addons_status(args)

        self.assertEqual(0, result)
        service.resolve_oci_target.assert_called_once_with(require_cluster=True)

    def test_compute_cluster_backed_pool_resize_uses_oke_api(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            placement_type="compute-cluster",
            compute_cluster_id="compute-cluster-1",
            node_pool_id="node-pool-1",
            desired_size=2,
        )
        args = build_parser().parse_args(
            [
                "--auth",
                "instance_principal",
                "--cluster-id",
                "cluster-1",
                "pools",
                "resize",
                "oke-rdma",
                "--delta",
                "1",
                "--yes",
            ]
        )
        backend = Mock()
        backend.resize_managed_node_pool.return_value = "work-request-1"
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(pools=[pool])
        service.oci_backend.return_value = backend

        with (
            patch("oke_hpc_mgmt.cli.DiscoveryService", return_value=service),
            redirect_stdout(io.StringIO()),
        ):
            result = cmd_pools_resize(args)

        self.assertEqual(0, result)
        service.resolve_oci_target.assert_called_once_with(require_compartment=True)
        backend.resize_managed_node_pool.assert_called_once_with("node-pool-1", 3)
        backend.resize_instance_pool.assert_not_called()

    def test_slinky_pool_scale_down_is_refused_before_oci_call(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            node_pool_id="node-pool-1",
            desired_size=2,
            slinky_managed=True,
        )
        args = build_parser().parse_args(
            [
                "--auth",
                "instance_principal",
                "--compartment-id",
                "compartment-1",
                "pools",
                "resize",
                "oke-rdma",
                "--delta",
                "-1",
                "--yes",
            ]
        )
        stderr = io.StringIO()
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(pools=[pool])

        with (
            patch("oke_hpc_mgmt.cli.DiscoveryService", return_value=service),
            redirect_stderr(stderr),
        ):
            result = cmd_pools_resize(args)

        self.assertEqual(2, result)
        self.assertIn("Slurm-aware drain", stderr.getvalue())
        service.oci_backend.assert_not_called()

    def test_compute_cluster_backed_node_removal_uses_oke_delete(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="node-pool",
            placement_type="compute-cluster",
            node_pool_id="node-pool-1",
            desired_size=2,
        )
        node = NodeInfo(
            k8s_name="10.0.0.1",
            instance_ocid="instance-1",
            pool_name="oke-rdma",
        )
        args = build_parser().parse_args(
            [
                "--auth",
                "instance_principal",
                "nodes",
                "remove",
                "10.0.0.1",
                "--keep-size",
                "--yes",
            ]
        )
        backend = Mock()
        backend.delete_node.return_value = "work-request-1"
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(pools=[pool], nodes=[node])
        service.oci_backend.return_value = backend

        with (
            patch("oke_hpc_mgmt.cli.DiscoveryService", return_value=service),
            redirect_stdout(io.StringIO()),
        ):
            result = cmd_nodes_remove(args)

        self.assertEqual(0, result)
        service.resolve_oci_target.assert_called_once_with(require_compartment=True)
        backend.delete_node.assert_called_once_with(
            "node-pool-1",
            "instance-1",
            decrement_size=False,
            override_eviction_grace_duration="PT10M",
            force_after_grace=False,
        )
        backend.detach_instance_pool_node.assert_not_called()

    def test_slinky_node_removal_is_refused_even_with_allow_workloads(self):
        pool = WorkerPoolInfo(
            name="oke-rdma",
            kind="cluster-network",
            instance_pool_id="instance-pool-1",
            desired_size=2,
            slinky_managed=True,
        )
        node = NodeInfo(
            k8s_name="10.0.0.1",
            instance_ocid="instance-1",
            pool_name="oke-rdma",
            slinky_workload_pods=1,
        )
        args = build_parser().parse_args(
            [
                "--auth",
                "instance_principal",
                "--compartment-id",
                "compartment-1",
                "nodes",
                "remove",
                "10.0.0.1",
                "--allow-workloads",
                "--yes",
            ]
        )
        stderr = io.StringIO()
        service = Mock()
        service.discover.return_value = DiscoverySnapshot(pools=[pool], nodes=[node])

        with (
            patch("oke_hpc_mgmt.cli.DiscoveryService", return_value=service),
            redirect_stderr(stderr),
        ):
            result = cmd_nodes_remove(args)

        self.assertEqual(2, result)
        self.assertIn("Slurm-aware drain", stderr.getvalue())
        service.oci_backend.assert_not_called()

    def test_readiness_status_and_match_include_rdma_vfs(self):
        ready = PoolResourceReadiness(
            gpu_ready=2,
            rdma_topology_ready=2,
            rdma_vf_ready=2,
        )
        pending = PoolResourceReadiness(
            gpu_ready=2,
            rdma_topology_ready=2,
            rdma_vf_ready=1,
        )

        self.assertIn("rdma_vf_ready=2", _readiness_status(ready))
        self.assertTrue(_resource_counts_match(ready, 2))
        self.assertFalse(_resource_counts_match(pending, 2))


if __name__ == "__main__":
    unittest.main()
