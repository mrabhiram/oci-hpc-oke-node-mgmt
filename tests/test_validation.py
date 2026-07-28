import unittest

from oke_hpc_mgmt.validation import normalize_pool_name


class ValidationTests(unittest.TestCase):
    def test_normalize_pool_name_accepts_kubernetes_label_values(self):
        self.assertEqual("oke-rdma-2", normalize_pool_name("  oke-rdma-2  "))
        self.assertEqual("RDMA_pool.2", normalize_pool_name("RDMA_pool.2"))

    def test_normalize_pool_name_rejects_invalid_values(self):
        for value in ("", "-rdma", "rdma-", "rdma/pool", "x" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_pool_name(value)
