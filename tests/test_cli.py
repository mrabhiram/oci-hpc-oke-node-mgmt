import sys
import unittest
from unittest.mock import patch

from oke_hpc_mgmt.cli import _program_name


class CliTests(unittest.TestCase):
    def test_direct_entrypoint_name(self):
        with patch.object(sys, "argv", ["/usr/local/bin/mgmt-oke"]):
            self.assertEqual("mgmt-oke", _program_name())

    def test_kubectl_plugin_name(self):
        with patch.object(sys, "argv", ["/usr/local/bin/kubectl-oke"]):
            self.assertEqual("kubectl oke", _program_name())


if __name__ == "__main__":
    unittest.main()
