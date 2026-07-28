from __future__ import annotations

import unittest

from oke_hpc_mgmt.models import KueueSummary, WorkerPoolInfo
from oke_hpc_mgmt.upgrades import (
    KubernetesVersion,
    UpgradeCheckpoint,
    UpgradeGateEvidence,
    UpgradePhase,
    UpgradeValidationError,
    control_plane_steps,
    default_pool_order,
    kueue_upgrade_blockers,
    parse_slurm_record,
    resolve_upgrade_target,
    select_upgrade_strategy,
    slurm_upgrade_blockers,
    validate_cycling_value,
    validate_control_plane_step,
    validate_worker_skew,
    validate_workload_gate,
)


class KubernetesVersionTests(unittest.TestCase):
    def test_parse_normalizes_optional_v_and_minor_target(self):
        self.assertEqual("v1.36", str(KubernetesVersion.parse("1.36")))
        self.assertEqual("v1.36.2", str(KubernetesVersion.parse("v1.36.2")))

    def test_parse_rejects_unstructured_values(self):
        with self.assertRaisesRegex(UpgradeValidationError, "Invalid"):
            KubernetesVersion.parse("latest")

    def test_minor_target_resolves_latest_non_preview_patch(self):
        target = resolve_upgrade_target(
            "v1.36",
            (
                "v1.36",
                "v1.36.0",
                "v1.36.2",
                "v1.36.1",
            ),
        )
        self.assertEqual(KubernetesVersion(1, 36, 2), target)

    def test_exact_target_must_be_advertised(self):
        with self.assertRaisesRegex(UpgradeValidationError, "does not advertise"):
            resolve_upgrade_target("v1.36.2", ("v1.36.1",))

    def test_preview_target_requires_acknowledgement(self):
        with self.assertRaisesRegex(UpgradeValidationError, "allow-preview"):
            resolve_upgrade_target("v1.36", ("v1.36.0",))
        self.assertEqual(
            KubernetesVersion(1, 36, 0),
            resolve_upgrade_target(
                "v1.36",
                ("v1.36.0",),
                allow_preview=True,
            ),
        )

    def test_control_plane_refuses_downgrade_and_minor_jump(self):
        with self.assertRaisesRegex(UpgradeValidationError, "downgrade"):
            validate_control_plane_step("v1.35.2", "v1.34.5")
        with self.assertRaisesRegex(UpgradeValidationError, "one minor"):
            validate_control_plane_step("v1.34.5", "v1.36.2")

    def test_control_plane_steps_select_latest_intermediate_patch(self):
        steps = control_plane_steps(
            "v1.34.4",
            "v1.36.2",
            ("v1.35.1", "v1.35.4", "v1.36.2"),
        )
        self.assertEqual(
            (KubernetesVersion(1, 35, 4), KubernetesVersion(1, 36, 2)),
            steps,
        )

    def test_worker_skew_rejects_newer_and_too_old_kubelet(self):
        with self.assertRaisesRegex(UpgradeValidationError, "cannot be newer"):
            validate_worker_skew("v1.35.1", "v1.36.0")
        with self.assertRaisesRegex(UpgradeValidationError, "more than three"):
            validate_worker_skew("v1.35.1", "v1.31.9")


class UpgradePolicyTests(unittest.TestCase):
    def test_auto_strategy_uses_managed_bvr_and_self_managed_replace(self):
        managed = WorkerPoolInfo(name="cpu", kind="node-pool")
        self_managed = WorkerPoolInfo(name="rdma", kind="cluster-network")
        self.assertEqual(
            "boot-volume-replace",
            select_upgrade_strategy(managed, "auto"),
        )
        self.assertEqual(
            "instance-replace",
            select_upgrade_strategy(self_managed, "auto"),
        )

    def test_default_pool_order_matches_hpc_upgrade_policy(self):
        pools = [
            WorkerPoolInfo(
                name="custom",
                kind="instance-pool",
            ),
            WorkerPoolInfo(
                name="oke-rdma",
                kind="cluster-network",
                gpu_resource="nvidia.com/gpu",
                rdma_enabled=True,
            ),
            WorkerPoolInfo(
                name="oke-gpu",
                kind="node-pool",
                gpu_resource="nvidia.com/gpu",
            ),
            WorkerPoolInfo(name="oke-system", kind="node-pool"),
            WorkerPoolInfo(name="oke-cpu", kind="node-pool"),
            WorkerPoolInfo(
                name="managed-rdma",
                kind="node-pool",
                gpu_resource="nvidia.com/gpu",
                rdma_enabled=True,
            ),
            WorkerPoolInfo(
                name="gmc",
                kind="gpu-memory-cluster",
                gpu_resource="nvidia.com/gpu",
                rdma_enabled=True,
            ),
        ]
        self.assertEqual(
            (
                "oke-cpu",
                "oke-system",
                "oke-gpu",
                "managed-rdma",
                "oke-rdma",
                "gmc",
                "custom",
            ),
            default_pool_order(pools),
        )

    def test_positive_workload_blocker_cannot_be_emergency_bypassed(self):
        evidence = UpgradeGateEvidence(
            pool="gpu",
            nodes=("node-1",),
            ready=True,
            externally_cordoned=True,
            active_pods=("default/training",),
            verification_errors=("slurm exec forbidden",),
        )
        with self.assertRaisesRegex(UpgradeValidationError, "active workloads"):
            validate_workload_gate(
                evidence,
                acknowledged=True,
                emergency_ack_unverified_drain=True,
            )

    def test_unavailable_verification_can_be_emergency_acknowledged(self):
        evidence = UpgradeGateEvidence(
            pool="gpu",
            nodes=("node-1",),
            ready=True,
            externally_cordoned=True,
            verification_errors=("slurm exec forbidden",),
        )
        validate_workload_gate(
            evidence,
            acknowledged=True,
            emergency_ack_unverified_drain=True,
        )

    def test_cycling_values_accept_counts_and_percentages(self):
        self.assertEqual("1", validate_cycling_value("1", "value"))
        self.assertEqual("25%", validate_cycling_value("25%", "value"))
        with self.assertRaisesRegex(UpgradeValidationError, "cannot exceed"):
            validate_cycling_value("101%", "value")
        with self.assertRaisesRegex(UpgradeValidationError, "integer"):
            validate_cycling_value("-1", "value")

    def test_kueue_requires_hold_and_zero_admitted_workloads(self):
        summary = KueueSummary(
            cluster_queues=[
                {
                    "metadata": {"name": "gpu"},
                    "spec": {
                        "stopPolicy": "None",
                        "resourceGroups": [
                            {"flavors": [{"name": "a100"}]}
                        ],
                    },
                    "status": {"admittedWorkloads": 1},
                }
            ],
            workloads=[
                {
                    "metadata": {
                        "namespace": "training",
                        "name": "llm",
                    },
                    "status": {
                        "admission": {"clusterQueue": "gpu"},
                        "conditions": [
                            {"type": "Admitted", "status": "True"}
                        ],
                    },
                }
            ],
        )

        blockers = kueue_upgrade_blockers(summary, "a100")

        self.assertIn("ClusterQueue/gpu stopPolicy=None", blockers)
        self.assertIn("ClusterQueue/gpu admitted=1 reserving=0", blockers)
        self.assertIn("Workload/training/llm admitted", blockers)
        self.assertEqual((), kueue_upgrade_blockers(summary, "other"))

    def test_slurm_parser_requires_drained_nodes_down_partitions_and_no_jobs(self):
        record = parse_slurm_record(
            "NodeName=gpu-1 State=IDLE+DRAIN Partitions=gpu CfgTRES=cpu=8"
        )
        self.assertEqual("IDLE+DRAIN", record["State"])
        self.assertEqual(
            (),
            slurm_upgrade_blockers(
                ("NodeName=gpu-1 State=IDLE+DRAIN Partitions=gpu",),
                ("PartitionName=gpu State=DOWN",),
                "",
            ),
        )
        blockers = slurm_upgrade_blockers(
            ("NodeName=gpu-1 State=ALLOCATED Partitions=gpu",),
            ("PartitionName=gpu State=UP",),
            "123:RUNNING:gpu-1\n",
        )
        self.assertEqual(3, len(blockers))


class UpgradeCheckpointTests(unittest.TestCase):
    def test_checkpoint_round_trip_preserves_typed_state(self):
        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster-1",
            source_version="v1.34.4",
            target_version="v1.35.3",
            control_plane_steps=("v1.35.3",),
            pool_order=("cpu", "gpu"),
            strategies={
                "cpu": "boot-volume-replace",
                "gpu": "instance-replace",
            },
            images={"gpu": "image-1"},
        ).replace(phase=UpgradePhase.CONTROL_PLANE)

        restored = UpgradeCheckpoint.from_json(checkpoint.to_json())

        self.assertEqual(checkpoint, restored)
        self.assertEqual("image-1", restored.pools[1].image_id)

    def test_checkpoint_rejects_unknown_schema(self):
        with self.assertRaisesRegex(UpgradeValidationError, "schema"):
            UpgradeCheckpoint.from_json(
                '{"schema_version":99,"checkpoint":{}}'
            )


if __name__ == "__main__":
    unittest.main()
