from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal


HealthcheckType = Literal["passive", "active", "multi-node", "all"]
TestMode = Literal["pairs", "all-nodes", "per-local-block"]
Network = Literal["auto", "local", "host", "vf"]
Pairing = Literal["anchor", "adjacent", "reference", "all"]
PairScope = Literal["any", "same-local-block", "cross-local-block"]
Scheduler = Literal["auto", "direct", "kueue", "slurm"]
REPORT_BYTES = 8 * 1024**3
MAX_SELECTED_NODES = 4096
MAX_CASES = 1024
SOURCE_REVISION = "6c94a4201d9ec6887f37eceeca325ec18b03b703"


class BenchmarkError(ValueError):
    """An invalid or unsafe benchmark request; no implicit fallback is allowed."""


def parse_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KMGTP]?)(?:i?B)?", value.strip(), re.I)
    if match is None:
        raise BenchmarkError("Sizes must be positive byte counts or binary sizes such as 4G.")
    number, suffix = match.groups()
    result = int(number) * 1024 ** ("KMGTP".index(suffix.upper()) + 1 if suffix else 0)
    if result > 1024**5:
        raise BenchmarkError("A benchmark message cannot exceed 1 PiB.")
    return result


@dataclass(frozen=True)
class BenchmarkParameters:
    min_bytes: int = 4 * 1024**3
    max_bytes: int = REPORT_BYTES
    factor: int = 2
    iterations: int = 20
    warmup_iterations: int = 5
    repetitions: int = 1
    min_bus_bandwidth: float | None = None

    def validate(self) -> None:
        for name, value, minimum, maximum in (
            ("min-bytes", self.min_bytes, 1, 1024**5),
            ("max-bytes", self.max_bytes, 1, 1024**5),
            ("factor", self.factor, 2, 1024),
            ("iterations", self.iterations, 1, 100000),
            ("warmup-iterations", self.warmup_iterations, 0, 100000),
            ("repetitions", self.repetitions, 1, 100),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise BenchmarkError(f"{name} must be an integer in {minimum}..{maximum}.")
        if self.min_bytes > self.max_bytes:
            raise BenchmarkError("min-bytes cannot exceed max-bytes.")
        size = self.min_bytes
        while size < REPORT_BYTES:
            size *= self.factor
        if size != REPORT_BYTES or self.max_bytes < REPORT_BYTES:
            raise BenchmarkError("The message-size sequence must include exactly 8 GiB.")
        if self.min_bus_bandwidth is not None and (
            not math.isfinite(self.min_bus_bandwidth) or self.min_bus_bandwidth <= 0
        ):
            raise BenchmarkError("min-bus-bandwidth must be a finite positive GB/s value.")


@dataclass(frozen=True)
class HealthcheckRequest:
    nodes: tuple[str, ...] = ()
    fields: str | None = None
    pool: str | None = None
    check_type: HealthcheckType = "passive"
    mode: TestMode | None = None
    checks: tuple[str, ...] = ()
    local_block: str | None = None
    exclude_node: str | None = None
    pairing: Pairing = "anchor"
    pair_scope: PairScope = "any"
    reference_node: str | None = None
    network: Network = "auto"
    scheduler: Scheduler = "auto"
    namespace: str = "default"
    queue: str | None = None
    reservation: str | None = None
    image: str | None = None
    environment: tuple[str, ...] = ()
    parameters: BenchmarkParameters = BenchmarkParameters()

    def validate(self) -> None:
        if sum(bool(value) for value in (self.nodes, self.fields, self.pool)) != 1:
            raise BenchmarkError("Specify exactly one of --nodes, --fields, or --pool.")
        choices = (
            ("type", self.check_type, ("passive", "active", "multi-node", "all")),
            ("mode", self.mode, (None, "pairs", "all-nodes", "per-local-block")),
            ("network", self.network, ("auto", "local", "host", "vf")),
            ("pairing", self.pairing, ("anchor", "adjacent", "reference", "all")),
            ("pair-scope", self.pair_scope, ("any", "same-local-block", "cross-local-block")),
            ("scheduler", self.scheduler, ("auto", "direct", "kueue", "slurm")),
        )
        for name, value, accepted in choices:
            if value not in accepted:
                raise BenchmarkError(f"Unsupported {name}: {value}.")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.namespace):
            raise BenchmarkError("namespace must be a Kubernetes DNS label.")
        if self.queue and (len(self.queue) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[-.a-z0-9]*[a-z0-9])?", self.queue
        )):
            raise BenchmarkError("queue must be a Kubernetes resource name.")
        if self.scheduler == "direct" and self.queue:
            raise BenchmarkError("--queue cannot be combined with --scheduler direct.")
        if self.reservation and self.scheduler != "slurm":
            raise BenchmarkError("--reservation requires the Slurm adapter; use --queue for Kueue.")
        if self.scheduler == "slurm":
            raise BenchmarkError("Slurm/Slinky benchmark execution is not implemented yet.")
        if self.mode is not None and self.check_type not in {"multi-node", "all"}:
            raise BenchmarkError("--mode applies only to the multi-node stage.")
        pair_options = self.exclude_node or self.reference_node or (
            self.pairing != "anchor" or self.pair_scope != "any"
        )
        if pair_options and (
            self.check_type not in {"multi-node", "all"} or self.mode not in {None, "pairs"}
        ):
            raise BenchmarkError("Pairing and exclusion options require multi-node pairs mode.")
        if bool(self.reference_node) != (self.pairing == "reference"):
            raise BenchmarkError("--pairing reference and --reference-node must be used together.")
        if self.check_type == "passive":
            if self.checks or self.image or self.environment or self.network != "auto":
                raise BenchmarkError("Passive checks do not accept benchmark runtime options.")
        else:
            if self.checks != ("nccl",):
                raise BenchmarkError(
                    "The full Slurm diagnostic suite is not implemented yet. "
                    "Use --checks nccl to explicitly request the supported NCCL component."
                )
            self.parameters.validate()
        if self.image and (any(char.isspace() for char in self.image) or "\x00" in self.image):
            raise BenchmarkError("image must be a container image reference without whitespace.")


@dataclass(frozen=True)
class NodeTarget:
    name: str
    uid: str
    pool: str
    shape: str
    gpu_count: int
    topology: tuple[str, str, str] | None
    instance_id: str | None
    tolerations: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    check_type: Literal["active", "multi-node"]
    nodes: tuple[NodeTarget, ...]
    anchors: tuple[str, ...]
    network: Literal["local", "host", "vf"]
    repetition: int

    @property
    def ranks(self) -> int:
        return sum(node.gpu_count for node in self.nodes)


@dataclass(frozen=True)
class HealthcheckPlan:
    request: HealthcheckRequest
    selected_nodes: tuple[str, ...]
    cases: tuple[BenchmarkCase, ...]
    passive_nodes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
