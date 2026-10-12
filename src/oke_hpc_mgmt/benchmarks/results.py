from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from oke_hpc_mgmt.benchmarks.models import BenchmarkError, REPORT_BYTES


@dataclass(frozen=True)
class Measurement:
    time_us: float
    algorithm_gbs: float
    bus_gbs: float
    wrong: int


@dataclass(frozen=True)
class NcclRow:
    size_bytes: int
    count: int
    datatype: str
    operation: str
    out_of_place: Measurement
    in_place: Measurement


@dataclass(frozen=True)
class RankIdentity:
    rank: int
    local_rank: int
    node: str
    pod_uid: str


@dataclass(frozen=True)
class DataChannel:
    source_rank: int
    destination_rank: int
    transport: str


@dataclass(frozen=True)
class NcclOutput:
    rows: tuple[NcclRow, ...]
    ranks: tuple[int, ...]
    average_bus_gbs: float | None
    errors: tuple[str, ...]
    ib_channels_observed: bool
    socket_channels_observed: bool
    rank_identities: tuple[RankIdentity, ...]
    data_channels: tuple[DataChannel, ...]


@dataclass(frozen=True)
class NcclVerdict:
    status: Literal["PASS", "FAIL", "INCOMPLETE"]
    correctness: Literal["PASS", "FAIL", "UNVERIFIED"]
    performance: Literal["PASS", "FAIL", "UNGRADED"]
    transport: Literal["RDMA_OBSERVED", "LOCAL", "SOCKET_FALLBACK", "UNVERIFIED"]
    coverage: Literal["PASS", "FAIL"]
    reasons: tuple[str, ...]
    report_rows: tuple[NcclRow, ...]


def _measurement(values: list[str]) -> Measurement:
    time_us, alg, bus = (float(value) for value in values[:3])
    wrong = int(values[3])
    if any(not math.isfinite(value) or value < 0 for value in (time_us, alg, bus)) or wrong < 0:
        raise ValueError("Non-finite or negative metric.")
    return Measurement(time_us, alg, bus, wrong)


def parse_nccl_output(output: str) -> NcclOutput:
    rows: list[NcclRow] = []
    ranks: set[int] = set()
    errors: list[str] = []
    average: float | None = None
    identities: list[RankIdentity] = []
    channels: set[DataChannel] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("MGMT_NCCL_RANK"):
            parts = stripped.split("\t")
            try:
                if len(parts) != 5 or int(parts[1]) < 0 or int(parts[2]) < 0 or not parts[3] or not parts[4]:
                    raise ValueError("Invalid rank identity.")
                identities.append(RankIdentity(int(parts[1]), int(parts[2]), parts[3], parts[4]))
            except ValueError:
                errors.append("Invalid MPI rank identity evidence.")
        channel = re.search(
            r"\bChannel\s+\d+/\d+\s*:\s*(\d+)\[\d+\]\s*->\s*(\d+)\[\d+\]"
            r"\s+\[(?:send|receive)\]\s+via NET/(IB|Socket)(?:/|\b)", line,
        )
        if channel:
            channels.add(DataChannel(int(channel[1]), int(channel[2]), channel[3]))
        rank = re.match(r"#\s*Rank\s+(\d+)\b", stripped)
        if rank:
            number = int(rank.group(1))
            if number in ranks:
                errors.append(f"Repeated rank header {number}; combined attempts are not one result.")
            ranks.add(number)
        if re.search(r"\b(?:CUDA error|NCCL error|Segmentation fault|MPI_ABORT)\b", line, re.I):
            errors.append("Benchmark runtime reported a fatal error.")
        if stripped.startswith("# Avg bus bandwidth"):
            try:
                measured = float(stripped.rsplit(None, 1)[-1])
                if not math.isfinite(measured) or measured < 0 or average is not None:
                    raise ValueError("Invalid or repeated summary.")
                average = measured
            except ValueError:
                errors.append("Invalid or repeated average bus-bandwidth summary.")
        if not re.match(r"\d+\s", stripped):
            continue
        fields = stripped.split()
        if len(fields) != 13:
            errors.append("Truncated or unsupported NCCL measurement row.")
            continue
        try:
            size, count = int(fields[0]), int(fields[1])
            int(fields[4])
            if size <= 0 or count <= 0:
                raise ValueError("Invalid message size/count.")
            if fields[2:5] != ["float", "sum", "-1"] or size != count * 4:
                raise ValueError("Unexpected collective datatype, reduction, root, or count.")
            rows.append(NcclRow(
                size, count, fields[2], fields[3],
                _measurement(fields[5:9]), _measurement(fields[9:13]),
            ))
        except (ValueError, OverflowError):
            errors.append("Invalid metrics or disabled correctness in NCCL measurement row.")
    return NcclOutput(
        tuple(rows), tuple(sorted(ranks)), average, tuple(dict.fromkeys(errors)),
        bool(re.search(r"\bvia NET/IB(?:/|\b)", output)),
        bool(re.search(r"\bvia NET/Socket(?:/|\b)", output)),
        tuple(identities), tuple(sorted(channels, key=lambda item: (item.source_rank, item.destination_rank, item.transport))),
    )


def assess_nccl(
    output: NcclOutput,
    *,
    expected_ranks: int,
    expected_nodes: tuple[tuple[str, str], ...],
    observed_nodes: tuple[tuple[str, str], ...],
    observed_workers: tuple[tuple[str, str], ...],
    job_succeeded: bool,
    exit_code: int | None,
    rdma_required: bool,
    min_bus_bandwidth: float | None = None,
) -> NcclVerdict:
    if expected_ranks <= 0 or not expected_nodes or len(set(expected_nodes)) != len(expected_nodes):
        raise BenchmarkError("Result assessment requires unique expected nodes and positive ranks.")
    if min_bus_bandwidth is not None and (
        not math.isfinite(min_bus_bandwidth) or min_bus_bandwidth <= 0
    ):
        raise BenchmarkError("Performance baseline must be finite and positive.")
    reasons = list(output.errors)
    complete = job_succeeded and exit_code == 0
    if not complete:
        reasons.append("A successful job condition and launcher exit code 0 are both required.")
    coverage: Literal["PASS", "FAIL"] = "PASS"
    if sorted(expected_nodes) != sorted(observed_nodes) or output.ranks != tuple(range(expected_ranks)):
        coverage = "FAIL"
        reasons.append("Observed node identities or MPI ranks do not match the requested participants.")
    workers = dict(observed_workers)
    nodes = {node for node, _ in expected_nodes}
    ranks_per_node, extra = divmod(expected_ranks, len(expected_nodes))
    expected_placement = {(name, local_rank) for name in nodes for local_rank in range(ranks_per_node)}
    actual_placement = {(identity.node, identity.local_rank) for identity in output.rank_identities}
    if extra or len(workers) != len(observed_workers) or set(workers) != nodes or (
        len(set(workers.values())) != len(workers) or not all(workers.values())
    ) or sorted(identity.rank for identity in output.rank_identities) != list(range(expected_ranks)) or (
        actual_placement != expected_placement
    ) or any(workers.get(identity.node) != identity.pod_uid for identity in output.rank_identities):
        coverage = "FAIL"
        reasons.append("MPI rank placement does not match the observed worker Pod UIDs and per-node GPU counts.")
    report_rows = tuple(row for row in output.rows if row.size_bytes == REPORT_BYTES)
    correctness: Literal["PASS", "FAIL", "UNVERIFIED"] = "UNVERIFIED"
    if any(row.out_of_place.wrong or row.in_place.wrong for row in output.rows):
        correctness = "FAIL"
        reasons.append("NCCL reported correctness errors.")
    elif report_rows and output.rows and not output.errors and complete and coverage == "PASS":
        correctness = "PASS"
    if len(report_rows) != 1:
        reasons.append("Exactly one 8 GiB measurement is required per attempt.")
        if correctness == "PASS":
            correctness = "UNVERIFIED"
    if output.average_bus_gbs is None:
        reasons.append("NCCL completion bandwidth summary is missing.")
    transport: Literal["RDMA_OBSERVED", "LOCAL", "SOCKET_FALLBACK", "UNVERIFIED"] = "LOCAL"
    if rdma_required:
        if output.socket_channels_observed:
            transport = "SOCKET_FALLBACK"
            reasons.append("NCCL used socket data channels during an RDMA test.")
        else:
            rank_nodes = {identity.rank: identity.node for identity in output.rank_identities}
            rdma_nodes: set[str] = set()
            for channel in output.data_channels:
                source, destination = rank_nodes.get(channel.source_rank), rank_nodes.get(channel.destination_rank)
                if channel.transport == "IB" and source and destination and source != destination:
                    rdma_nodes.update((source, destination))
            if coverage == "PASS" and rdma_nodes == nodes:
                transport = "RDMA_OBSERVED"
            else:
                transport = "UNVERIFIED"
                reasons.append("Inter-node NCCL RDMA data-channel evidence is required for every participant.")
    performance: Literal["PASS", "FAIL", "UNGRADED"] = "UNGRADED"
    if min_bus_bandwidth is not None and correctness == "PASS" and len(report_rows) == 1:
        row = report_rows[0]
        performance = "PASS" if min(row.out_of_place.bus_gbs, row.in_place.bus_gbs) >= min_bus_bandwidth else "FAIL"
        if performance == "FAIL":
            reasons.append("8 GiB bus bandwidth is below the explicitly configured baseline.")
    status: Literal["PASS", "FAIL", "INCOMPLETE"] = "PASS"
    if correctness == "FAIL" or performance == "FAIL" or transport == "SOCKET_FALLBACK" or (
        exit_code is not None and exit_code != 0
    ):
        status = "FAIL"
    elif reasons:
        status = "INCOMPLETE"
    return NcclVerdict(status, correctness, performance, transport, coverage, tuple(reasons), report_rows)
