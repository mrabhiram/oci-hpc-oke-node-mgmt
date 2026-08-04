import unittest

from oke_hpc_mgmt.models import (
    NvmeRaidSpec,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
)
from oke_hpc_mgmt.validation import (
    normalize_pool_name,
    parse_key_value_options,
    validate_eviction_grace_duration,
    validate_maximum_unavailable,
    validate_pool_boot_volume_replace_spec,
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

    def test_managed_rdma_mode_is_explicit_and_defaults_remain_legacy(self):
        legacy = validate_pool_create_spec(PoolCreateSpec(pool_type="rdma"))
        managed = validate_pool_create_spec(
            PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
            )
        )

        self.assertEqual("cluster-network", legacy.effective_rdma_mode)
        self.assertFalse(legacy.managed)
        self.assertEqual("compute-cluster", managed.effective_rdma_mode)
        self.assertTrue(managed.managed)
        self.assertTrue(managed.creates_compute_cluster)

    def test_managed_placement_options_reject_conflicting_backends(self):
        invalid_specs = (
            (
                PoolCreateSpec(
                    pool_type="gpu",
                    rdma_mode="compute-cluster",
                ),
                "rdma-mode",
            ),
            (
                PoolCreateSpec(
                    pool_type="rdma",
                    compute_cluster_id="compute-cluster-1",
                ),
                "compute-cluster-id",
            ),
            (
                PoolCreateSpec(
                    pool_type="rdma",
                    host_group_id="host-group-1",
                ),
                "host-group-id",
            ),
            (
                PoolCreateSpec(
                    pool_type="rdma",
                    rdma_mode="compute-cluster",
                    fault_domains=("FD-1",),
                ),
                "fault domains",
            ),
            (
                PoolCreateSpec(
                    pool_type="gpu",
                    host_group_id="host-group-1",
                    capacity_reservation_id="capacity-1",
                ),
                "conflicting placement",
            ),
        )
        for spec, message in invalid_specs:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError,
                message,
            ):
                validate_pool_create_spec(spec)

    def test_existing_compute_cluster_disables_automatic_creation(self):
        spec = validate_pool_create_spec(
            PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                compute_cluster_id="compute-cluster-1",
                host_group_id="host-group-1",
            )
        )

        self.assertFalse(spec.creates_compute_cluster)
        self.assertEqual("host-group-1", spec.host_group_id)
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

    def test_pool_bvr_requires_a_supported_property_update(self):
        with self.assertRaisesRegex(ValueError, "at least one supported update"):
            validate_pool_boot_volume_replace_spec(
                PoolBootVolumeReplaceSpec()
            )

        spec = validate_pool_boot_volume_replace_spec(
            PoolBootVolumeReplaceSpec(
                image_id="ocid1.image.oc1..example",
                maximum_unavailable="25%",
            )
        )
        self.assertEqual("25%", spec.maximum_unavailable)

    def test_pool_bvr_rejects_reserved_metadata_and_invalid_parallelism(self):
        with self.assertRaisesRegex(ValueError, "reserved OKE node metadata"):
            validate_pool_boot_volume_replace_spec(
                PoolBootVolumeReplaceSpec(
                    node_metadata=(("user_data", "replacement"),),
                )
            )
        for value in ("0", "-1", "0%", "101%", "half"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_maximum_unavailable(value)

    def test_eviction_grace_duration_accepts_zero_through_sixty_minutes(self):
        for value, expected in (
            ("PT0M", "PT0M"),
            ("pt30m", "PT30M"),
            ("PT1H", "PT1H"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    expected,
                    validate_eviction_grace_duration(value),
                )
        for value in ("PT61M", "PT2H", "30m", "PT"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_eviction_grace_duration(value)
