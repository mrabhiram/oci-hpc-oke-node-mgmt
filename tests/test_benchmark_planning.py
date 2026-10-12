from __future__ import annotations

import json
import unittest
from dataclasses import replace

from oke_hpc_mgmt.benchmarks.models import (
    BenchmarkError, BenchmarkParameters, HealthcheckRequest, REPORT_BYTES, parse_bytes,
)
from oke_hpc_mgmt.benchmarks.planning import (
    expand_nodes, node_target, pair_allowed, plan_healthchecks, resolve_identifier,
    select_healthcheck_nodes, topology,
)
from oke_hpc_mgmt.benchmarks.profiles import PROFILES, get_profile, profile_environment, validate_exclusive_gpu_labels
from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo, RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS, WorkerPoolInfo


def gpu_node(number: int, *, block: str = "block-a", pool: str = "gpu-pool") -> NodeInfo:
    return NodeInfo(
        k8s_name=f"gpu-{number:02d}", kubernetes_uid=f"uid-{number}",
        internal_ip=f"192.0.2.{number}", pool_name=pool, instance_ocid=f"instance-{number}",
        shape="BM.GPU.B200.8", ready=True, allocatable={"nvidia.com/gpu": "8"},
        labels=dict(zip(RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS, ("island-a", "network-a", block))),
    )


def inventory(count: int = 4) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        nodes=[gpu_node(number, block="block-a" if number <= 2 else "block-b")
               for number in range(1, count + 1)],
        pools=[WorkerPoolInfo(name="gpu-pool", kind="node-pool", node_pool_id="pool-id")],
    )


def request(**changes) -> HealthcheckRequest:
    return replace(HealthcheckRequest(pool="gpu-pool", check_type="multi-node", checks=("nccl",)), **changes)


class BenchmarkModelTests(unittest.TestCase):
    def test_binary_sizes(self) -> None:
        for value, expected in (("8G", REPORT_BYTES), ("8GiB", REPORT_BYTES),
                                ("8192M", REPORT_BYTES), ("16", 16), (" 2kB ", 2048)):
            with self.subTest(value=value):
                self.assertEqual(expected, parse_bytes(value))
        for value in ("0", "-1", "1.5G", "abc", "2PiB", "8G;exit", "8 G"):
            with self.subTest(value=value), self.assertRaises(BenchmarkError):
                parse_bytes(value)

    def test_parameter_bounds_and_exact_reporting_size(self) -> None:
        BenchmarkParameters().validate()
        for changes in (
            {"min_bytes": 1024**3, "factor": 4, "max_bytes": 16 * 1024**3},
            {"min_bytes": 16 * 1024**3}, {"max_bytes": 1024**3}, {"factor": 1},
            {"iterations": 0}, {"warmup_iterations": -1}, {"repetitions": 101},
            {"min_bytes": True}, {"min_bus_bandwidth": float("nan")},
            {"min_bus_bandwidth": float("inf")}, {"min_bus_bandwidth": 0},
        ):
            with self.subTest(changes=changes), self.assertRaises(BenchmarkError):
                replace(BenchmarkParameters(), **changes).validate()

    def test_request_validation_rejects_incompatible_options(self) -> None:
        HealthcheckRequest(nodes=("gpu-01",)).validate()
        for value in (
            HealthcheckRequest(), request(nodes=("gpu-01",)), request(fields="ready=true"),
            request(checks=()), request(checks=("rccl",)), request(namespace="UPPER"),
            request(queue="bad/queue"), request(queue="queue", scheduler="direct"),
            request(reservation="validation"), request(scheduler="slurm"),
            request(reference_node="gpu-02"), request(pairing="reference"),
            request(mode="all-nodes", exclude_node="gpu-01"),
            request(mode="per-local-block", pair_scope="cross-local-block"),
            request(check_type="active", mode="pairs"), request(image="bad image"),
            request(check_type="passive"),
        ):
            with self.subTest(value=value), self.assertRaises(BenchmarkError):
                value.validate()


class BenchmarkSelectionTests(unittest.TestCase):
    def test_nodeset_ranges_commas_and_identity_deduplication(self) -> None:
        snapshot = inventory()
        nodes = expand_nodes(snapshot, ("gpu-[01-02,04]", "192.0.2.1", "instance-3"))
        self.assertEqual([f"gpu-{i:02d}" for i in range(1, 5)], [node.k8s_name for node in nodes])
        self.assertEqual("gpu-01", resolve_identifier(snapshot, "instance-1").k8s_name)

    def test_bad_or_unbounded_ranges_and_missing_nodes_fail_closed(self) -> None:
        snapshot = inventory()
        for expression in ("", "@all", "gpu-[1-99999999]", "gpu-[01-99999999]", "gpu-[",
                           "gpu-[1-3/0]", "gpu-[4-1]", "gpu-01,unknown", "gpu-[1-100][1-100]"):
            with self.subTest(expression=expression), self.assertRaises(BenchmarkError):
                expand_nodes(snapshot, (expression,))

    def test_ambiguous_alias_fails_instead_of_picking_first(self) -> None:
        snapshot = inventory()
        snapshot.nodes[1].internal_ip = snapshot.nodes[0].internal_ip
        with self.assertRaisesRegex(BenchmarkError, "matched 2"):
            expand_nodes(snapshot, ("192.0.2.1",))

    def test_pool_id_and_fields_use_existing_inventory(self) -> None:
        snapshot = inventory()
        self.assertEqual(4, len(select_healthcheck_nodes(snapshot, request(pool="pool-id"))))
        selected = select_healthcheck_nodes(snapshot, request(pool=None, fields="name=gpu-02,ready=true"))
        self.assertEqual(["gpu-02"], [node.k8s_name for node in selected])
        for changes in ({"pool": "absent"}, {"pool": None, "fields": "role=compute"},
                        {"pool": None, "fields": "name=missing"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                select_healthcheck_nodes(snapshot, request(**changes))

    def test_topology_filter_respects_parent_domains_and_explicit_nodes(self) -> None:
        snapshot = inventory()
        selected = select_healthcheck_nodes(snapshot, request(local_block="block-a"))
        self.assertEqual(2, len(selected))
        with self.assertRaisesRegex(BenchmarkError, "Explicit nodes"):
            select_healthcheck_nodes(snapshot, request(pool=None, nodes=("gpu-01,gpu-03",), local_block="block-a"))
        snapshot.nodes[1].labels[RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS[0]] = "other-island"
        with self.assertRaisesRegex(BenchmarkError, "ambiguous"):
            select_healthcheck_nodes(snapshot, request(local_block="block-a"))
        snapshot.nodes[0].labels[RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS[2]] = "unknown"
        self.assertIsNone(topology(snapshot.nodes[0]))

    def test_uid_and_exact_physical_gpu_requirements(self) -> None:
        valid = gpu_node(1)
        self.assertEqual("uid-1", node_target(valid).uid)
        for changes in (
            {"ready": False}, {"schedulable": False}, {"kubernetes_uid": None},
            {"pool_name": None}, {"slinky_workload_pods": 1},
            {"allocatable": {"nvidia.com/gpu": "unknown"}},
            {"allocatable": {"nvidia.com/gpu": "7"}},
            {"allocatable": {"amd.com/gpu": "8"}},
            {"labels": {"nvidia.com/gpu.replicas": "2"}},
            {"labels": {"nvidia.com/mig.strategy": "mixed"}},
            {"taints": ["maintenance=true:NoSchedule"]},
            {"taints": ["nvidia.com/gpu-maintenance=true:NoSchedule"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(BenchmarkError):
                node_target(replace(valid, **changes))

    def test_mig_advertisement_policy_does_not_mean_partitioning_is_active(self) -> None:
        for strategy in ("single", "mixed"):
            labels = {"nvidia.com/mig.strategy": strategy, "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB",
                      "nvidia.com/gpu.count": "8", "nvidia.com/gpu.mode": "compute"}
            validate_exclusive_gpu_labels(labels, 8)
            self.assertEqual(8, node_target(replace(gpu_node(1), labels=labels)).gpu_count)
            for change in ({"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB-MIG-1g.5gb"},
                           {"nvidia.com/gpu.product": "NVIDIA-A100-SHARED"}, {"nvidia.com/gpu.count": "56"},
                           {"nvidia.com/gpu.mode": "mig"}, {"nvidia.com/gpu.sharing-strategy": "mps"},
                           {"nvidia.com/mig.config.state": "pending"}, {"nvidia.com/mig.strategy": "unknown"}):
                with self.subTest(strategy=strategy, change=change), self.assertRaises(BenchmarkError):
                    validate_exclusive_gpu_labels({**labels, **change}, 8)


class BenchmarkPlanningTests(unittest.TestCase):
    def test_vf_preflight_uses_hca_count_not_gpu_count(self) -> None:
        snapshot = inventory(2)
        for node in snapshot.nodes:
            node.shape = "BM.GPU4.8"
            node.allocatable["nvidia.com/rdma-vf"] = "8"
        with self.assertRaisesRegex(BenchmarkError, "Insufficient RDMA VFs"):
            plan_healthchecks(snapshot, request(network="vf"))
        for node in snapshot.nodes:
            node.allocatable["nvidia.com/rdma-vf"] = "16"
        self.assertEqual("vf", plan_healthchecks(snapshot, request(network="vf")).cases[0].network)

    def test_passive_is_read_only_and_does_not_require_a_gpu(self) -> None:
        snapshot = inventory(1)
        snapshot.nodes[0].allocatable = {}
        snapshot.nodes[0].kubernetes_uid = None
        plan = plan_healthchecks(snapshot, HealthcheckRequest(pool="gpu-pool"))
        self.assertEqual((), plan.cases)
        self.assertEqual(("gpu-01",), plan.passive_nodes)
        self.assertEqual("passive", json.loads(json.dumps(plan.to_dict()))["request"]["check_type"])

    def test_active_and_all_dispatch_do_not_share_upstream_elif_bug(self) -> None:
        snapshot = inventory(2)
        active = plan_healthchecks(snapshot, request(check_type="active"))
        self.assertEqual(["active", "active"], [case.check_type for case in active.cases])
        self.assertTrue(all(case.network == "local" and case.ranks == 8 for case in active.cases))
        combined = plan_healthchecks(snapshot, request(check_type="all"))
        self.assertEqual(["active", "active", "multi-node"], [case.check_type for case in combined.cases])
        self.assertEqual(("gpu-01", "gpu-02"), combined.passive_nodes)

    def test_exact_collective_covers_entire_selection(self) -> None:
        plan = plan_healthchecks(inventory(), request(mode="all-nodes"))
        self.assertEqual(1, len(plan.cases))
        self.assertEqual(32, plan.cases[0].ranks)
        self.assertEqual(plan.selected_nodes, tuple(node.name for node in plan.cases[0].nodes))

    def test_local_blocks_and_singleton_refusal(self) -> None:
        plan = plan_healthchecks(inventory(), request(mode="per-local-block"))
        self.assertEqual(2, len(plan.cases))
        self.assertEqual([("gpu-01", "gpu-02"), ("gpu-03", "gpu-04")],
                         [tuple(node.name for node in case.nodes) for case in plan.cases])
        with self.assertRaisesRegex(BenchmarkError, "at least two"):
            plan_healthchecks(inventory(3), request(mode="per-local-block"))

    def test_explicit_anchor_gets_disclosed_same_pool_partner(self) -> None:
        snapshot = inventory(3)
        plan = plan_healthchecks(snapshot, request(pool=None, nodes=("gpu-01",), exclude_node="gpu-02"))
        self.assertEqual(("gpu-01",), plan.selected_nodes)
        self.assertEqual(("gpu-01", "gpu-03"), tuple(node.name for node in plan.cases[0].nodes))
        self.assertIn("gpu-03", plan.warnings[0])
        snapshot.nodes[2].pool_name = "other-pool"
        with self.assertRaisesRegex(BenchmarkError, "No eligible partner"):
            plan_healthchecks(snapshot, request(pool=None, nodes=("gpu-01",), exclude_node="gpu-02"))

    def test_two_explicit_anchors_keep_pairwise_semantics(self) -> None:
        plan = plan_healthchecks(inventory(), request(pool=None, nodes=("gpu-03,gpu-04",)))
        self.assertEqual(1, len(plan.cases))
        self.assertEqual(("gpu-03", "gpu-04"), plan.cases[0].anchors)
        self.assertEqual(16, plan.cases[0].ranks)

    def test_odd_adjacent_and_exhaustive_pair_coverage(self) -> None:
        adjacent = plan_healthchecks(inventory(5), request(pairing="adjacent"))
        self.assertEqual(3, len(adjacent.cases))
        self.assertEqual(5, len({node.name for case in adjacent.cases for node in case.nodes}))
        exhaustive = plan_healthchecks(inventory(5), request(pairing="all"))
        self.assertEqual(10, len(exhaustive.cases))
        with self.assertRaisesRegex(BenchmarkError, "exceed"):
            plan_healthchecks(inventory(46), request(pairing="all"))

    def test_reference_pairing_and_scope(self) -> None:
        snapshot = inventory()
        plan = plan_healthchecks(snapshot, request(pairing="reference", reference_node="gpu-01"))
        self.assertEqual(3, len(plan.cases))
        self.assertTrue(all("gpu-01" in case.anchors for case in plan.cases))
        plan = plan_healthchecks(snapshot, request(pair_scope="cross-local-block"))
        self.assertTrue(all(case.nodes[0].topology != case.nodes[1].topology for case in plan.cases))
        snapshot.nodes[2].labels[RDMA_REQUIRED_TOPOLOGY_LABEL_KEYS[0]] = "other-island"
        self.assertFalse(pair_allowed(snapshot.nodes[0], snapshot.nodes[2], request(pair_scope="cross-local-block")))

    def test_pool_selection_never_silently_omits_bad_nodes(self) -> None:
        snapshot = inventory()
        snapshot.nodes[3].ready = False
        for mode in ("pairs", "all-nodes", "per-local-block"):
            with self.subTest(mode=mode), self.assertRaisesRegex(BenchmarkError, "cannot be omitted"):
                plan_healthchecks(snapshot, request(mode=mode))

    def test_exclusion_and_incomplete_topology_fail_before_submission(self) -> None:
        snapshot = inventory()
        with self.assertRaisesRegex(BenchmarkError, "anchor cannot"):
            plan_healthchecks(snapshot, request(exclude_node="gpu-01"))
        snapshot.nodes[0].labels = {}
        with self.assertRaisesRegex(BenchmarkError, "topology"):
            plan_healthchecks(snapshot, request(mode="all-nodes"))

    def test_mixed_shape_and_gpu_profile_refusal(self) -> None:
        snapshot = inventory(2)
        snapshot.nodes[1].shape = "BM.GPU4.8"
        with self.assertRaisesRegex(BenchmarkError, "homogeneous"):
            plan_healthchecks(snapshot, request(mode="all-nodes"))
        snapshot.nodes[0].shape = "BM.GPU4.8"
        with self.assertRaisesRegex(BenchmarkError, "at most"):
            plan_healthchecks(snapshot, request(mode="all-nodes", parameters=BenchmarkParameters(max_bytes=2 * REPORT_BYTES)))

    def test_vf_selection_requires_every_participant(self) -> None:
        snapshot = inventory(2)
        snapshot.nodes[0].allocatable["nvidia.com/rdma-vf"] = "8"
        with self.assertRaisesRegex(BenchmarkError, "Mixed VF"):
            plan_healthchecks(snapshot, request())
        snapshot.nodes[1].allocatable["nvidia.com/rdma-vf"] = "8"
        self.assertEqual("vf", plan_healthchecks(snapshot, request()).cases[0].network)
        snapshot.nodes[1].allocatable["nvidia.com/rdma-vf"] = "7"
        with self.assertRaisesRegex(BenchmarkError, "Insufficient"):
            plan_healthchecks(snapshot, request(network="vf"))

    def test_repetitions_have_distinct_cases(self) -> None:
        plan = plan_healthchecks(inventory(2), request(parameters=BenchmarkParameters(repetitions=3)))
        self.assertEqual([1, 2, 3], [case.repetition for case in plan.cases])
        self.assertEqual(3, len({case.case_id for case in plan.cases}))


class BenchmarkProfileTests(unittest.TestCase):
    def test_profiles_are_unique_and_gb_is_not_b200(self) -> None:
        self.assertEqual(len(PROFILES), len({profile.shape for profile in PROFILES}))
        self.assertEqual(8, get_profile("BM.GPU.B200.8").gpu_count)
        with self.assertRaises(BenchmarkError):
            get_profile("BM.GPU.GB200.4")
        for profile in PROFILES:
            self.assertEqual("1", profile_environment(profile, "local")["NCCL_IB_DISABLE"])
            if profile.host_hcas:
                self.assertTrue(profile_environment(profile, "host")["NCCL_IB_HCA"].startswith("="))
                self.assertEqual("mlx5", profile_environment(profile, "vf")["NCCL_IB_HCA"])

    def test_environment_validation_preserves_values_without_shell_interpretation(self) -> None:
        profile = get_profile("BM.GPU.B200.8")
        self.assertEqual("4", profile_environment(profile, "host", ("NCCL_IB_QPS_PER_CONNECTION=4",))["NCCL_IB_QPS_PER_CONNECTION"])
        for override in (
            "PATH=/tmp", "NCCL_DEBUG=OFF", "NCCL_DEBUG_FILE=/tmp/log", "NCCL_NET=Socket",
            "NCCL_IB_DISABLE=1", "NCCL_ALGO=", "NCCL_ALGO=Tree\nexit", "NCCL_ALGO",
        ):
            with self.subTest(override=override), self.assertRaises(BenchmarkError):
                profile_environment(profile, "host", (override,))
        with self.assertRaises(BenchmarkError):
            profile_environment(profile, "host", ("NCCL_ALGO=Tree", "NCCL_ALGO=Ring"))
        with self.assertRaises(BenchmarkError):
            profile_environment(profile, "vf", ("NCCL_IB_HCA==mlx5_0",))
        with self.assertRaises(BenchmarkError):
            profile_environment(get_profile("VM.GPU.A10.1"), "host")
