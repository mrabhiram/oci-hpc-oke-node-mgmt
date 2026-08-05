from __future__ import annotations

import base64
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import yaml

from oke_hpc_mgmt.bootstrap import (
    BootstrapCompositionError,
    compose_worker_user_data,
    decode_user_data,
    encode_user_data,
    load_upstream_asset,
    summarize_worker_bootstrap,
)
from oke_hpc_mgmt.models import (
    FssMountSpec,
    LustreMountSpec,
    NvmeRaidSpec,
    PoolCreateSpec,
)


def _source_user_data() -> str:
    root = EmailMessage(policy=policy.default)
    root.make_mixed()
    part = EmailMessage(policy=policy.default)
    part.set_content(
        "#cloud-config\n"
        + yaml.safe_dump(
            {
                "ssh_authorized_keys": ["old-key"],
                "runcmd": [
                    (
                        "curl -sL -o /var/run/oke-nvme-raid.sh "
                        "https://raw.githubusercontent.com/oracle-quickstart/"
                        "oci-hpc-oke/refs/heads/main/files/oke-nvme-raid.sh && "
                        "bash /var/run/oke-nvme-raid.sh '10'"
                    ),
                    (
                        "curl -sL -o /var/run/oke-ubuntu-cloud-init.sh "
                        "https://raw.githubusercontent.com/oracle-quickstart/"
                        "oci-hpc-oke/refs/heads/main/files/oke-ubuntu-cloud-init.sh "
                        "&& bash /var/run/oke-ubuntu-cloud-init.sh 'v1.35.2' "
                        "'true' 'false'"
                    ),
                    (
                        "curl -sL -o /var/run/oke-fss-mount.sh "
                        "https://raw.githubusercontent.com/oracle-quickstart/"
                        "oci-hpc-oke/refs/heads/main/files/oke-fss-mount.sh && "
                        "bash /var/run/oke-fss-mount.sh '/old' '/mnt/old' '10.0.0.1'"
                    ),
                ],
            },
            sort_keys=False,
        ),
        subtype="cloud-config",
    )
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename="50-worker.yml",
    )
    root.attach(part)
    return encode_user_data(root.as_bytes(policy=policy.default))


def _cloud_configs(encoded: str) -> list[dict[str, object]]:
    message = BytesParser(policy=policy.default).parsebytes(
        decode_user_data(encoded)
    )
    configs = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_type() != "text/cloud-config":
            continue
        text = str(part.get_content())
        if text.lstrip().startswith("#cloud-config"):
            text = text[text.index("#cloud-config") + len("#cloud-config") :]
        configs.append(yaml.safe_load(text) or {})
    return configs


class BootstrapTests(unittest.TestCase):
    def test_encode_decode_round_trip_is_deterministic(self):
        payload = b"#cloud-config\nruncmd: []\n"

        encoded = encode_user_data(payload)

        self.assertEqual(payload, decode_user_data(encoded))
        self.assertEqual(encoded, encode_user_data(payload))

    def test_bootstrap_summary_reports_hash_size_hooks_and_storage_scripts(self):
        source = _source_user_data()

        summary = summarize_worker_bootstrap(
            {
                "user_data": source,
                "pre_oke": "encoded-hook",
            }
        )

        self.assertEqual(["pre_oke"], summary["bootstrap_hook_keys"])
        self.assertGreater(summary["decoded_bytes"], 0)
        self.assertTrue(summary["oke_bootstrap_detected"])
        self.assertEqual(
            ["oke-nvme-raid.sh", "oke-fss-mount.sh"],
            summary["storage_scripts_detected"],
        )
        self.assertRegex(summary["user_data_sha256"], r"^[0-9a-f]{64}$")

    def test_bootstrap_summary_requires_user_data(self):
        with self.assertRaisesRegex(BootstrapCompositionError, "cloud-init"):
            summarize_worker_bootstrap({})

    def test_replace_storage_preserves_bootstrap_order_and_embeds_assets(self):
        spec = PoolCreateSpec(
            pool_type="rdma",
            storage_mode="replace",
            nvme_raid=NvmeRaidSpec(10, "/dev/nvme*n1", "/mnt/nvme"),
            fss_mounts=(
                FssMountSpec("/export", "/mnt/fss", "10.0.0.2"),
            ),
            lustre_mounts=(
                LustreMountSpec("10.0.0.3", "lustrefs", "/mnt/lustre"),
            ),
        )

        composed = compose_worker_user_data(_source_user_data(), spec)
        config = _cloud_configs(composed)[0]
        commands = config["runcmd"]
        command_text = [" ".join(command) if isinstance(command, list) else command for command in commands]

        self.assertLess(
            command_text.index(
                "bash /var/lib/mgmt-oke/bootstrap/oke-nvme-raid.sh "
                "10 /dev/nvme*n1 /mnt/nvme"
            ),
            next(
                index
                for index, command in enumerate(command_text)
                if "oke-ubuntu-cloud-init.sh" in command
            ),
        )
        self.assertGreater(
            command_text.index(
                "bash /var/lib/mgmt-oke/bootstrap/oke-fss-mount.sh "
                "/export /mnt/fss 10.0.0.2"
            ),
            next(
                index
                for index, command in enumerate(command_text)
                if "oke-ubuntu-cloud-init.sh" in command
            ),
        )
        self.assertFalse(any("/old" in command for command in command_text))
        paths = {entry["path"] for entry in config["write_files"]}
        self.assertEqual(
            {
                "/var/lib/mgmt-oke/bootstrap/oke-nvme-raid.sh",
                "/var/lib/mgmt-oke/bootstrap/oke-fss-mount.sh",
                "/var/lib/mgmt-oke/bootstrap/oke-lustre-mount.sh",
            },
            paths,
        )
        for entry in config["write_files"]:
            self.assertTrue(base64.b64decode(entry["content"]).startswith(b"#!"))

    def test_version_ssh_and_custom_cloud_init_are_composed(self):
        spec = PoolCreateSpec(
            pool_type="gpu",
            kubernetes_version="v1.36.1",
            ssh_public_key="ssh-ed25519 test",
            cloud_init=b"#cloud-config\npackages:\n- jq\n",
        )

        configs = _cloud_configs(
            compose_worker_user_data(_source_user_data(), spec)
        )

        self.assertEqual(["ssh-ed25519 test"], configs[0]["ssh_authorized_keys"])
        self.assertIn("v1.36.1", " ".join(configs[0]["runcmd"]))
        self.assertEqual(["jq"], configs[1]["packages"])

    def test_nvme_requires_identifiable_oke_bootstrap(self):
        source = encode_user_data(b"#cloud-config\nruncmd:\n- echo ready\n")
        spec = PoolCreateSpec(
            pool_type="cpu",
            storage_mode="append",
            nvme_raid=NvmeRaidSpec(0),
        )

        with self.assertRaisesRegex(BootstrapCompositionError, "before OKE bootstrap"):
            compose_worker_user_data(source, spec)

    def test_only_known_upstream_assets_can_be_loaded(self):
        self.assertIn(b"OKE setup completed successfully", load_upstream_asset("oke-ubuntu-cloud-init.sh"))
        with self.assertRaises(BootstrapCompositionError):
            load_upstream_asset("../../unexpected")


if __name__ == "__main__":
    unittest.main()
