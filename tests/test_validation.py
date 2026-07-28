import unittest

from oke_hpc_mgmt.models import NvmeRaidSpec, PoolCreateSpec
from oke_hpc_mgmt.validation import (
    normalize_pool_name,
    parse_key_value_options,
    validate_pool_create_spec,
)


class ValidationTests(unittest.TestCase):
    def test_normalize_pool_name_accepts_kubernetes_label_values(self):
        self.assertEqual("oke-rdma-2", normalize_pool_name("  oke-rdma-2  "))
        self.assertEqual("RDMA_pool.2", normalize_pool_name("RDMA_pool.2"))

    def test_normalize_pool_name_rejects_invalid_values(self):
        for value in ("", "-rdma", "rdma-", "rdma/pool", "x" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_pool_name(value)

    def test_parse_key_value_options_rejects_missing_and_duplicate_keys(self):
        self.assertEqual(
            (("team", "ai"),),
            parse_key_value_options(
                ("team=ai",),
                option_name="--freeform-tag",
            ),
        )
        with self.assertRaisesRegex(ValueError, "KEY=VALUE"):
            parse_key_value_options(("invalid",), option_name="--node-label")
        with self.assertRaisesRegex(ValueError, "repeats key"):
            parse_key_value_options(
                ("team=ai", "team=hpc"),
                option_name="--freeform-tag",
            )

    def test_pool_create_validation_is_backend_specific(self):
        with self.assertRaisesRegex(ValueError, "Cluster Network settings"):
            validate_pool_create_spec(
                PoolCreateSpec(
                    pool_type="cpu",
                    boot_volume_vpus_per_gb=10,
                )
            )
        with self.assertRaisesRegex(ValueError, "managed OKE settings"):
            validate_pool_create_spec(
                PoolCreateSpec(
                    pool_type="rdma",
                    node_cycling_enabled=True,
                )
            )

    def test_pool_create_rejects_reserved_ownership_tags(self):
        for key in ("mgmt-oke-created", "pool", "role", "state_id"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "ownership tags"),
            ):
                validate_pool_create_spec(
                    PoolCreateSpec(
                        pool_type="rdma",
                        freeform_tags=((key, "override"),),
                    )
                )

    def test_storage_selection_requires_explicit_composition_mode(self):
        with self.assertRaisesRegex(ValueError, "storage-mode"):
            validate_pool_create_spec(
                PoolCreateSpec(
                    pool_type="rdma",
                    nvme_raid=NvmeRaidSpec(10),
                )
            )
        self.assertEqual(
            "replace",
            validate_pool_create_spec(
                PoolCreateSpec(
                    pool_type="rdma",
                    storage_mode="replace",
                    nvme_raid=NvmeRaidSpec(10),
                )
            ).storage_mode,
        )
