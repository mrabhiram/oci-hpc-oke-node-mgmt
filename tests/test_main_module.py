import os
import subprocess
import sys
import unittest
from pathlib import Path


class MainModuleTests(unittest.TestCase):
    def test_module_entrypoint_propagates_cli_error_status(self):
        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root / "src")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "oke_hpc_mgmt",
                "--auth",
                "none",
                "addons",
                "status",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("requires OCI auth", result.stderr)

    def test_module_entrypoint_help_succeeds(self):
        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root / "src")

        result = subprocess.run(
            [sys.executable, "-m", "oke_hpc_mgmt", "--help"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertIn("Management CLI for OCI HPC OKE", result.stdout)


if __name__ == "__main__":
    unittest.main()
