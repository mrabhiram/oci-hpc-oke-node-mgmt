from __future__ import annotations

import unittest
from dataclasses import replace

from oke_hpc_mgmt.benchmarks.models import BenchmarkError, REPORT_BYTES
from oke_hpc_mgmt.benchmarks.results import NcclVerdict, assess_nccl, parse_nccl_output


# Synthetic parser fixture, not benchmark evidence or a performance baseline.
SYNTHETIC_OUTPUT = """MGMT_NCCL_RANK\t0\t0\tgpu-a\tpod-a
MGMT_NCCL_RANK\t1\t0\tgpu-b\tpod-b
# Rank 0 Group 0 Pid 100 on gpu-a device 0
# Rank 1 Group 0 Pid 101 on gpu-b device 0
gpu-a:100:110 [0] NCCL INFO Channel 00/0 : 0[0] -> 1[0] [send] via NET/IB/0/GDRDMA
# size count type redop root time algbw busbw #wrong time algbw busbw #wrong
4294967296 1073741824 float sum -1 100 42.0 42.0 0 101 41.0 41.0 0
8589934592 2147483648 float sum -1 200 43.0 43.0 0 201 42.0 42.0 0
# Avg bus bandwidth : 42.0
"""
NODES = (("gpu-a", "uid-a"), ("gpu-b", "uid-b"))
WORKERS = (("gpu-a", "pod-a"), ("gpu-b", "pod-b"))


def verdict(
    output: str = SYNTHETIC_OUTPUT, *, expected_ranks: int = 2,
    expected_nodes: tuple[tuple[str, str], ...] = NODES,
    observed_nodes: tuple[tuple[str, str], ...] = NODES,
    observed_workers: tuple[tuple[str, str], ...] = WORKERS,
    job_succeeded: bool = True, exit_code: int | None = 0,
    rdma_required: bool = True, min_bus_bandwidth: float | None = None,
) -> NcclVerdict:
    return assess_nccl(
        parse_nccl_output(output), expected_ranks=expected_ranks,
        expected_nodes=expected_nodes, observed_nodes=observed_nodes,
        observed_workers=observed_workers,
        job_succeeded=job_succeeded, exit_code=exit_code,
        rdma_required=rdma_required, min_bus_bandwidth=min_bus_bandwidth,
    )


class NcclResultTests(unittest.TestCase):
    def test_parse_in_place_out_of_place_and_average_separately(self) -> None:
        parsed = parse_nccl_output(SYNTHETIC_OUTPUT)
        self.assertEqual((), parsed.errors)
        self.assertEqual((0, 1), parsed.ranks)
        self.assertEqual(REPORT_BYTES, parsed.rows[-1].size_bytes)
        self.assertEqual(43, parsed.rows[-1].out_of_place.bus_gbs)
        self.assertEqual(42, parsed.rows[-1].in_place.bus_gbs)
        self.assertEqual(42, parsed.average_bus_gbs)
        result = verdict()
        self.assertEqual("PASS", result.status)
        self.assertEqual("UNGRADED", result.performance)
        self.assertEqual("RDMA_OBSERVED", result.transport)

    def test_performance_uses_weaker_8g_measurement_not_summary(self) -> None:
        self.assertEqual("PASS", verdict(min_bus_bandwidth=42).performance)
        result = verdict(min_bus_bandwidth=42.5)
        self.assertEqual("FAIL", result.performance)
        self.assertEqual("FAIL", result.status)

    def test_summary_alone_or_missing_size_does_not_pass(self) -> None:
        self.assertEqual("INCOMPLETE", verdict("# Avg bus bandwidth : 900\n").status)
        self.assertEqual("INCOMPLETE", verdict(SYNTHETIC_OUTPUT.replace(
            "8589934592", "8589934591"
        )).status)

    def test_nonzero_wrong_count_in_any_message_size_fails(self) -> None:
        output = SYNTHETIC_OUTPUT.replace("42.0 42.0 0", "42.0 42.0 1", 1)
        result = verdict(output)
        self.assertEqual("FAIL", result.correctness)
        self.assertEqual("FAIL", result.status)

    def test_nonfinite_negative_disabled_and_truncated_metrics_never_pass(self) -> None:
        for replacement in ("NaN", "Inf", "-4", "--", "unexpected"):
            with self.subTest(replacement=replacement):
                result = verdict(SYNTHETIC_OUTPUT.replace("200 43.0", f"{replacement} 43.0"))
                self.assertEqual("INCOMPLETE", result.status)
                self.assertEqual("UNVERIFIED", result.correctness)
        disabled = SYNTHETIC_OUTPUT.replace("43.0 43.0 0", "43.0 43.0 N/A")
        self.assertEqual("INCOMPLETE", verdict(disabled).status)
        self.assertEqual("INCOMPLETE", verdict(SYNTHETIC_OUTPUT.replace("201 42.0 42.0 0", "201")).status)

    def test_debug_interleaved_into_a_measurement_is_not_silently_reconstructed(self) -> None:
        output = SYNTHETIC_OUTPUT.replace(
            "1073741824 float sum -1 100",
            "1073741824 float sum -1gpu-a:100:100 [0] NCCL INFO Symmetric VA size=40GB\n100",
        )
        result = verdict(output)
        self.assertEqual("INCOMPLETE", result.status)
        self.assertEqual("UNVERIFIED", result.correctness)
        self.assertIn("Truncated or unsupported NCCL measurement row.", result.reasons)

    def test_node_replacement_missing_extra_or_duplicate_worker_invalidates_coverage(self) -> None:
        for observed in (NODES[:1], (*NODES, NODES[0]), (("gpu-a", "new-uid"), NODES[1])):
            with self.subTest(observed=observed):
                result = verdict(observed_nodes=observed)
                self.assertEqual("FAIL", result.coverage)
                self.assertEqual("INCOMPLETE", result.status)
        self.assertEqual("FAIL", verdict(expected_ranks=3).coverage)

    def test_job_completion_and_zero_exit_are_both_required(self) -> None:
        self.assertEqual("INCOMPLETE", verdict(job_succeeded=False).status)
        self.assertEqual("INCOMPLETE", verdict(exit_code=None).status)
        self.assertEqual("FAIL", verdict(exit_code=1).status)

    def test_transport_requires_data_channel_evidence_not_plugin_availability(self) -> None:
        output = SYNTHETIC_OUTPUT.replace("via NET/IB/0/GDRDMA", "NCCL INFO NET/IB : Using mlx5_0")
        self.assertEqual("UNVERIFIED", verdict(output).transport)
        self.assertEqual("INCOMPLETE", verdict(output).status)
        fallback = SYNTHETIC_OUTPUT.replace("NET/IB/0", "NET/Socket/0")
        self.assertEqual("SOCKET_FALLBACK", verdict(fallback).transport)
        self.assertEqual("FAIL", verdict(fallback).status)
        self.assertEqual("LOCAL", verdict(output, rdma_required=False).transport)

    def test_combined_attempts_and_duplicate_report_rows_cannot_pass(self) -> None:
        self.assertEqual("INCOMPLETE", verdict(SYNTHETIC_OUTPUT * 2).status)
        parsed = parse_nccl_output(SYNTHETIC_OUTPUT)
        result = assess_nccl(replace(parsed, rows=(*parsed.rows, parsed.rows[-1])),
                             expected_ranks=2, expected_nodes=NODES, observed_nodes=NODES,
                             observed_workers=WORKERS,
                             job_succeeded=True, exit_code=0, rdma_required=True)
        self.assertEqual("INCOMPLETE", result.status)

    def test_invalid_assessment_inputs_are_errors(self) -> None:
        for action in (
            lambda: verdict(expected_ranks=0), lambda: verdict(expected_nodes=()),
            lambda: verdict(expected_nodes=(NODES[0], NODES[0])),
            lambda: verdict(min_bus_bandwidth=float("nan")),
            lambda: verdict(min_bus_bandwidth=-1),
        ):
            with self.assertRaises(BenchmarkError):
                action()

    def test_fatal_lines_invalid_and_absent_summary_are_not_success(self) -> None:
        for output in (
            SYNTHETIC_OUTPUT + "CUDA error: illegal memory access\n",
            SYNTHETIC_OUTPUT.replace("# Avg bus bandwidth : 42.0", "# Avg bus bandwidth : NaN"),
            SYNTHETIC_OUTPUT.replace("# Avg bus bandwidth : 42.0", ""),
        ):
            with self.subTest(output=output):
                self.assertNotEqual("PASS", verdict(output).status)

    def test_pod_replacement_missing_rank_and_repeated_placement_fail_coverage(self) -> None:
        for output in (
            SYNTHETIC_OUTPUT.replace("\tpod-a", "\treplacement"),
            SYNTHETIC_OUTPUT.replace("MGMT_NCCL_RANK\t0\t0\tgpu-a\tpod-a\n", ""),
            SYNTHETIC_OUTPUT.replace("\t1\t0\tgpu-b\tpod-b", "\t0\t0\tgpu-a\tpod-a"),
            SYNTHETIC_OUTPUT.replace("\t1\t0\tgpu-b\tpod-b", "\t1\t1\tgpu-b\tpod-b"),
            SYNTHETIC_OUTPUT.replace("MGMT_NCCL_RANK\t0", "MGMT_NCCL_RANK\tbad"),
        ):
            with self.subTest(output=output):
                self.assertEqual("FAIL", verdict(output).coverage)
                self.assertEqual("INCOMPLETE", verdict(output).status)
        for workers in (WORKERS[:1], (*WORKERS, WORKERS[0]), (("gpu-a", "pod-a"), ("gpu-b", "pod-a"))):
            self.assertEqual("FAIL", verdict(observed_workers=workers).coverage)

    def test_one_verified_rdma_edge_does_not_certify_uncovered_nodes(self) -> None:
        four_nodes = (*NODES, ("gpu-c", "uid-c"), ("gpu-d", "uid-d"))
        four_workers = (*WORKERS, ("gpu-c", "pod-c"), ("gpu-d", "pod-d"))
        output = SYNTHETIC_OUTPUT + (
            "MGMT_NCCL_RANK\t2\t0\tgpu-c\tpod-c\nMGMT_NCCL_RANK\t3\t0\tgpu-d\tpod-d\n"
            "# Rank 2 Group 0 Pid 102 on gpu-c device 0\n# Rank 3 Group 0 Pid 103 on gpu-d device 0\n"
        )
        result = verdict(output, expected_ranks=4, expected_nodes=four_nodes,
                         observed_nodes=four_nodes, observed_workers=four_workers)
        self.assertEqual("PASS", result.coverage)
        self.assertEqual("UNVERIFIED", result.transport)
        output += "NCCL INFO Channel 00/0 : 2[0] -> 3[0] [send] via NET/IB/0/GDRDMA\n"
        self.assertEqual("RDMA_OBSERVED", verdict(
            output, expected_ranks=4, expected_nodes=four_nodes, observed_nodes=four_nodes,
            observed_workers=four_workers,
        ).transport)

    def test_unexpected_datatype_operation_root_and_count_are_rejected(self) -> None:
        for old, new in (("float", "half"), ("sum", "max"), ("-1", "0"), ("2147483648", "2147483647")):
            self.assertEqual("INCOMPLETE", verdict(SYNTHETIC_OUTPUT.replace(old, new)).status)
