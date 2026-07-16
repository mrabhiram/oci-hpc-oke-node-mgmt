import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oke_hpc_mgmt.backends.kubeconfig import (
    KubeconfigDiscoveryError,
    load_oke_kubeconfig_context,
    parse_oke_kubeconfig_context,
)


CLUSTER_ID = "ocid1.cluster.oc1.lhr.example"
OTHER_CLUSTER_ID = "ocid1.cluster.oc1.iad.example"


def _user(name, cluster_id=CLUSTER_ID, region="uk-london-1", equals=False):
    cluster_option = f"--cluster-id={cluster_id}" if equals else "--cluster-id"
    region_option = f"--region={region}" if equals else "--region"
    args = ["ce", "cluster", "generate-token", cluster_option]
    if not equals:
        args.append(cluster_id)
    args.append(region_option)
    if not equals:
        args.append(region)
    return {
        "name": name,
        "user": {
            "exec": {
                "command": "/usr/local/bin/oci",
                "args": args,
            }
        },
    }


def _config():
    return {
        "current-context": "london",
        "clusters": [{"name": "london-cluster", "cluster": {"server": "https://example"}}],
        "contexts": [
            {
                "name": "london",
                "context": {"cluster": "london-cluster", "user": "london-user"},
            }
        ],
        "users": [_user("london-user")],
    }


class KubeconfigParsingTests(unittest.TestCase):
    def test_current_context_returns_cluster_id_and_region(self):
        result = parse_oke_kubeconfig_context(_config())

        self.assertEqual("london", result.context_name)
        self.assertEqual("london-cluster", result.cluster_name)
        self.assertEqual(CLUSTER_ID, result.cluster_id)
        self.assertEqual("uk-london-1", result.region)

    def test_explicit_context_overrides_current_context_and_accepts_equals_options(self):
        config = _config()
        config["clusters"].append(
            {"name": "ashburn-cluster", "cluster": {"server": "https://example-2"}}
        )
        config["contexts"].append(
            {
                "name": "ashburn",
                "context": {"cluster": "ashburn-cluster", "user": "ashburn-user"},
            }
        )
        config["users"].append(
            _user("ashburn-user", OTHER_CLUSTER_ID, "us-ashburn-1", equals=True)
        )

        result = parse_oke_kubeconfig_context(config, context="ashburn")

        self.assertEqual("ashburn", result.context_name)
        self.assertEqual(OTHER_CLUSTER_ID, result.cluster_id)
        self.assertEqual("us-ashburn-1", result.region)

    def test_single_cluster_is_selected_without_current_context(self):
        config = _config()
        config.pop("current-context")
        config["contexts"].append(
            {
                "name": "london-secondary",
                "context": {"cluster": "london-cluster", "user": "london-user"},
            }
        )

        result = parse_oke_kubeconfig_context(config)

        self.assertEqual("london", result.context_name)
        self.assertEqual(CLUSTER_ID, result.cluster_id)

    def test_multiple_clusters_without_current_context_require_context_override(self):
        config = _config()
        config.pop("current-context")
        config["clusters"].append(
            {"name": "ashburn-cluster", "cluster": {"server": "https://example-2"}}
        )
        config["contexts"].append(
            {
                "name": "ashburn",
                "context": {"cluster": "ashburn-cluster", "user": "ashburn-user"},
            }
        )
        config["users"].append(_user("ashburn-user", OTHER_CLUSTER_ID, "us-ashburn-1"))

        with self.assertRaisesRegex(KubeconfigDiscoveryError, "Select one with --context"):
            parse_oke_kubeconfig_context(config)

    def test_single_cluster_with_multiple_users_requires_context_override(self):
        config = _config()
        config.pop("current-context")
        config["contexts"].append(
            {
                "name": "london-secondary",
                "context": {"cluster": "london-cluster", "user": "secondary-user"},
            }
        )
        config["users"].append(_user("secondary-user"))

        with self.assertRaisesRegex(KubeconfigDiscoveryError, "Select one with --context"):
            parse_oke_kubeconfig_context(config)

    def test_non_oci_exec_plugin_is_rejected(self):
        config = _config()
        config["users"][0]["user"]["exec"]["command"] = "other-authenticator"

        with self.assertRaisesRegex(KubeconfigDiscoveryError, "OCI CLI exec plugin"):
            parse_oke_kubeconfig_context(config)

    def test_missing_cluster_id_is_rejected(self):
        config = _config()
        config["users"][0]["user"]["exec"]["args"] = [
            "ce",
            "cluster",
            "generate-token",
            "--region",
            "uk-london-1",
        ]

        with self.assertRaisesRegex(KubeconfigDiscoveryError, "valid OKE cluster OCID"):
            parse_oke_kubeconfig_context(config)

    def test_invalid_named_entry_is_rejected(self):
        config = _config()
        config["contexts"] = "invalid"

        with self.assertRaisesRegex(KubeconfigDiscoveryError, "must be a list"):
            parse_oke_kubeconfig_context(config)


class KubeconfigLoadingTests(unittest.TestCase):
    def test_multiple_kubeconfig_files_are_merged(self):
        config = _config()
        first = {
            "current-context": config["current-context"],
            "clusters": config["clusters"],
            "contexts": config["contexts"],
        }
        second = {"users": config["users"]}
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "config-one"
            second_path = Path(directory) / "config-two"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")
            missing_path = Path(directory) / "missing"

            result = load_oke_kubeconfig_context(
                os.pathsep.join((str(first_path), str(missing_path), str(second_path)))
            )

        self.assertEqual(CLUSTER_ID, result.cluster_id)
        self.assertEqual("uk-london-1", result.region)

    def test_kubeconfig_environment_variable_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(json.dumps(_config()), encoding="utf-8")
            with patch.dict(os.environ, {"KUBECONFIG": str(path)}):
                result = load_oke_kubeconfig_context()

        self.assertEqual(CLUSTER_ID, result.cluster_id)

    def test_missing_kubeconfig_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing"

            with self.assertRaisesRegex(KubeconfigDiscoveryError, "file not found"):
                load_oke_kubeconfig_context(str(path))

    def test_malformed_yaml_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text("contexts: [\n", encoding="utf-8")

            with self.assertRaisesRegex(KubeconfigDiscoveryError, "Cannot parse kubeconfig"):
                load_oke_kubeconfig_context(str(path))

    def test_non_utf8_kubeconfig_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(KubeconfigDiscoveryError, "Cannot read kubeconfig"):
                load_oke_kubeconfig_context(str(path))


if __name__ == "__main__":
    unittest.main()
