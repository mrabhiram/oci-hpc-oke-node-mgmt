from __future__ import annotations

import os
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import oci

from oke_hpc_mgmt.backends.oci import OciAuthenticationError, OciBackend
from oke_hpc_mgmt.backends.kubeconfig import OkeKubeconfigContext
from oke_hpc_mgmt.discovery import DiscoveryOptions, DiscoveryService
from oke_hpc_mgmt.cli import main


class OciAuthenticationTests(unittest.TestCase):
    def test_omitted_path_uses_sdk_defaults_and_loads_once(self) -> None:
        backend = OciBackend()
        with patch("oci.config.from_file", return_value={"region": "test-region"}) as load:
            self.assertIs(oci, backend.oci)
            self.assertIs(oci, backend.oci)
        load.assert_called_once_with(profile_name="DEFAULT")
        self.assertEqual({"region": "test-region"}, backend._config)
        self.assertIsNone(backend._signer)

    def test_explicit_path_profile_and_region_are_preserved(self) -> None:
        backend = OciBackend(config_file="/example/oci-config", profile="CUSTOM", region="override-region")
        with patch("oci.config.from_file", return_value={"region": "configured-region"}) as load:
            self.assertIs(oci, backend.oci)
        load.assert_called_once_with(file_location="/example/oci-config", profile_name="CUSTOM")
        self.assertEqual("override-region", (backend._config or {})["region"])

    def test_sdk_default_home_and_environment_config_fallbacks_work(self) -> None:
        for use_environment in (False, True):
            with self.subTest(use_environment=use_environment), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "fallback-config" if use_environment else root / ".oci" / "config"
                path.parent.mkdir(parents=True, exist_ok=True)
                key_path = root / "dummy-key"
                key_path.write_text("synthetic fixture, not a private key\n", encoding="utf-8")
                path.write_text(
                    "[DEFAULT]\nuser=synthetic-user\ntenancy=synthetic-tenancy\n"
                    f"fingerprint=synthetic-fingerprint\nkey_file={key_path}\nregion=test-region\n",
                    encoding="utf-8",
                )
                environment = {"HOME": directory, "OCI_CONFIG_FILE": str(path) if use_environment else ""}
                with patch.dict(os.environ, environment):
                    backend = OciBackend()
                    self.assertIs(oci, backend.oci)
                self.assertEqual("test-region", (backend._config or {})["region"])

    def test_missing_invalid_profile_and_unreadable_configs_have_auth_guidance(self) -> None:
        errors = (
            oci.exceptions.ConfigFileNotFound("synthetic missing config"),
            oci.exceptions.ProfileNotFound("synthetic missing profile"),
            oci.exceptions.InvalidConfig({"region": "synthetic invalid region"}),
            OSError("synthetic unreadable config"),
            ValueError("synthetic invalid config"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__), patch("oci.config.from_file", side_effect=error):
                backend = OciBackend()
                with self.assertRaisesRegex(OciAuthenticationError, "OCI_AUTH=instance_principal") as caught:
                    _ = backend.oci
                self.assertIs(error, caught.exception.__cause__)
                self.assertIsNone(backend._oci)
                self.assertIsNone(backend._config)
                self.assertIsNone(backend._signer)

    def test_failed_config_initialization_can_retry_without_partial_client_state(self) -> None:
        backend = OciBackend()
        with patch("oci.config.from_file", side_effect=[
            oci.exceptions.ConfigFileNotFound("synthetic missing config"), {"region": "test-region"},
        ]) as load, patch("oci.container_engine.ContainerEngineClient", return_value=Mock()) as client:
            with self.assertRaises(OciAuthenticationError):
                _ = backend.container_engine
            client.assert_not_called()
            self.assertIs(client.return_value, backend.container_engine)
        self.assertEqual(2, load.call_count)
        client.assert_called_once_with(config={"region": "test-region"}, signer=None)

    def test_principal_auth_never_reads_a_config_file(self) -> None:
        for auth, signer_path in (
            ("instance_principal", "oci.auth.signers.InstancePrincipalsSecurityTokenSigner"),
            ("resource_principal", "oci.auth.signers.get_resource_principals_signer"),
        ):
            with self.subTest(auth=auth), patch(signer_path, return_value=Mock()) as signer, \
                    patch("oci.config.from_file") as load:
                backend = OciBackend(auth=auth, region="test-region", config_file="unused-config")
                self.assertIs(oci, backend.oci)
                self.assertIs(signer.return_value, backend._signer)
                self.assertEqual({"region": "test-region"}, backend._config)
                signer.assert_called_once_with()
                load.assert_not_called()

    def test_failed_principal_initialization_can_retry_without_partial_state(self) -> None:
        backend = OciBackend(auth="instance_principal", region="test-region")
        signer = Mock()
        with patch("oci.auth.signers.InstancePrincipalsSecurityTokenSigner", side_effect=[
            RuntimeError("synthetic metadata failure"), signer,
        ]) as load:
            with self.assertRaisesRegex(RuntimeError, "metadata failure"):
                _ = backend.oci
            self.assertIsNone(backend._oci)
            self.assertIsNone(backend._config)
            self.assertIsNone(backend._signer)
            self.assertIs(oci, backend.oci)
        self.assertEqual(2, load.call_count)
        self.assertIs(signer, backend._signer)

    def test_automatic_compartment_lookup_initializes_sdk_without_an_explicit_config_path(self) -> None:
        service = DiscoveryService(DiscoveryOptions())
        context = OkeKubeconfigContext("synthetic-context", "synthetic-cluster", "synthetic-cluster-id", "test-region")
        client = Mock()
        client.get_cluster.return_value = SimpleNamespace(data=SimpleNamespace(compartment_id="synthetic-compartment"))
        with patch("oke_hpc_mgmt.discovery.load_oke_kubeconfig_context", return_value=context), \
                patch("oci.config.from_file", return_value={"region": "test-region"}) as load, \
                patch("oci.container_engine.ContainerEngineClient", return_value=client):
            target = service.resolve_oci_target(require_compartment=True, require_cluster=True)
        self.assertEqual("synthetic-cluster-id", target.cluster_id)
        self.assertEqual("synthetic-compartment", target.compartment_id)
        self.assertEqual("test-region", target.region)
        load.assert_called_once_with(profile_name="DEFAULT")
        client.get_cluster.assert_called_once_with("synthetic-cluster-id")

    def test_termination_auth_failure_never_tags_drains_or_deletes_even_with_yes(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {"OCI_COMPARTMENT_ID": ""}), \
                patch("oci.config.from_file", side_effect=oci.exceptions.ConfigFileNotFound("synthetic missing config")), \
                patch("oke_hpc_mgmt.commands.nodes.execute_node_removal") as execute, \
                patch.object(OciBackend, "tag_instance_customer_reported_unhealthy") as tag, \
                patch.object(OciBackend, "delete_node") as delete, redirect_stderr(output):
            status = main([
                "--auth", "config_file", "--cluster-id", "synthetic-cluster-id", "--region", "test-region",
                "nodes", "terminate", "gpu-01", "--tag", "unhealthy", "--yes",
            ])
        self.assertEqual(2, status)
        self.assertIn("--auth instance_principal", output.getvalue())
        self.assertNotIn("--compartment-id", output.getvalue())
        execute.assert_not_called()
        tag.assert_not_called()
        delete.assert_not_called()

    def test_disabled_auth_never_reads_credentials_or_caches_loaded_state(self) -> None:
        backend = OciBackend(auth="none")
        with patch("oci.config.from_file") as load:
            for _ in range(2):
                with self.assertRaisesRegex(OciAuthenticationError, "disabled"):
                    _ = backend.oci
        self.assertIsNone(backend._oci)
        load.assert_not_called()
