from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from oke_hpc_mgmt.models import KueueSummary, UPGRADE_STRATEGIES, WorkerPoolInfo


VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_NAME = "mgmt-oke-kubernetes-upgrade"
CHECKPOINT_NAMESPACE = "kube-system"


class UpgradeValidationError(ValueError):
    """Raised when an upgrade request violates OKE or Kubernetes policy."""


@dataclass(frozen=True)
class UpgradeGateEvidence:
    pool: str
    nodes: tuple[str, ...]
    ready: bool
    externally_cordoned: bool
    active_pods: tuple[str, ...] = ()
    verification_errors: tuple[str, ...] = ()
    kueue_blockers: tuple[str, ...] = ()
    slurm_blockers: tuple[str, ...] = ()

    @property
    def positively_blocked(self) -> bool:
        return bool(self.active_pods or self.kueue_blockers or self.slurm_blockers)

    @property
    def verification_available(self) -> bool:
        return not self.verification_errors

    @property
    def passed(self) -> bool:
        return bool(
            self.ready
            and self.externally_cordoned
            and not self.positively_blocked
            and self.verification_available
        )


def validate_workload_gate(
    evidence: UpgradeGateEvidence,
    *,
    acknowledged: bool,
    emergency_ack_unverified_drain: bool,
) -> None:
    if not evidence.ready:
        raise UpgradeValidationError(
            f"Pool {evidence.pool} contains nodes that are not Ready."
        )
    if not evidence.externally_cordoned:
        raise UpgradeValidationError(
            f"Pool {evidence.pool} is not externally cordoned."
        )
    if evidence.positively_blocked:
        blockers = (
            *evidence.active_pods,
            *evidence.kueue_blockers,
            *evidence.slurm_blockers,
        )
        raise UpgradeValidationError(
            f"Pool {evidence.pool} has active workloads: {', '.join(blockers)}."
        )
    if evidence.verification_errors and not emergency_ack_unverified_drain:
        raise UpgradeValidationError(
            f"Workload verification for pool {evidence.pool} is unavailable: "
            f"{'; '.join(evidence.verification_errors)}. "
            "Use --emergency-ack-unverified-drain only after independently "
            "verifying the pool."
        )
    if not acknowledged:
        raise UpgradeValidationError(
            f"Workload preparation for pool {evidence.pool} requires "
            "--ack-workloads-drained or the typed DRAINED confirmation."
        )


def kueue_upgrade_blockers(
    summary: KueueSummary,
    resource_flavor: str | None,
) -> tuple[str, ...]:
    if not resource_flavor:
        return ()
    relevant: dict[str, dict[str, Any]] = {}
    for queue in summary.cluster_queues:
        flavors = {
            str(flavor.get("name", ""))
            for group in queue.get("spec", {}).get("resourceGroups", [])
            for flavor in group.get("flavors", [])
            if isinstance(flavor, dict)
        }
        if resource_flavor in flavors:
            name = str(queue.get("metadata", {}).get("name", "unknown"))
            relevant[name] = queue
    blockers: list[str] = []
    for name, queue in sorted(relevant.items()):
        policy = str(queue.get("spec", {}).get("stopPolicy", "") or "")
        if policy not in {"Hold", "HoldAndDrain"}:
            blockers.append(
                f"ClusterQueue/{name} stopPolicy={policy or 'None'}"
            )
        status = queue.get("status", {})
        admitted = _integer(status.get("admittedWorkloads"))
        reserving = _integer(status.get("reservingWorkloads"))
        if admitted > 0 or reserving > 0:
            blockers.append(
                f"ClusterQueue/{name} admitted={admitted} reserving={reserving}"
            )
    for workload in summary.workloads:
        status = workload.get("status", {})
        admission = status.get("admission") or {}
        queue_name = str(admission.get("clusterQueue", "") or "")
        if queue_name not in relevant:
            continue
        conditions = status.get("conditions", [])
        admitted = any(
            condition.get("type") == "Admitted"
            and str(condition.get("status", "")).casefold() == "true"
            for condition in conditions
            if isinstance(condition, dict)
        )
        if admission and (admitted or not conditions):
            namespace = workload.get("metadata", {}).get("namespace", "default")
            name = workload.get("metadata", {}).get("name", "unknown")
            blockers.append(f"Workload/{namespace}/{name} admitted")
    return tuple(dict.fromkeys(blockers))


KEY_VALUE_RE = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=(.*?)(?=\s[A-Za-z][A-Za-z0-9_]*=|$)")


def parse_slurm_record(value: str) -> dict[str, str]:
    return {
        key: item.strip()
        for key, item in KEY_VALUE_RE.findall(value.strip())
    }


def slurm_upgrade_blockers(
    node_records: Iterable[str],
    partition_records: Iterable[str],
    jobs_output: str,
) -> tuple[str, ...]:
    blockers: list[str] = []
    for raw in node_records:
        record = parse_slurm_record(raw)
        name = record.get("NodeName", "unknown")
        states = {
            state
            for state in re.split(r"[+,*~#$@!%^&]+", record.get("State", "").upper())
            if state
        }
        if not states.intersection({"DRAIN", "DRAINED", "DOWN"}):
            blockers.append(
                f"SlurmNode/{name} state={record.get('State', 'UNKNOWN')}"
            )
    for raw in partition_records:
        record = parse_slurm_record(raw)
        name = record.get("PartitionName", "unknown")
        state = record.get("State", "").upper()
        if state not in {"DOWN", "INACTIVE"}:
            blockers.append(f"SlurmPartition/{name} state={state or 'UNKNOWN'}")
    for line in jobs_output.splitlines():
        value = line.strip()
        if value:
            blockers.append(f"SlurmJob/{value}")
    return tuple(dict.fromkeys(blockers))


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, order=True)
class KubernetesVersion:
    major: int
    minor: int
    patch: int | None = None

    @classmethod
    def parse(cls, value: str) -> "KubernetesVersion":
        match = VERSION_RE.fullmatch(value.strip())
        if not match:
            raise UpgradeValidationError(
                f"Invalid Kubernetes version {value!r}; use v<major>.<minor> "
                "or v<major>.<minor>.<patch>."
            )
        major, minor, patch = match.groups()
        return cls(int(major), int(minor), int(patch) if patch is not None else None)

    @property
    def exact(self) -> bool:
        return self.patch is not None

    @property
    def preview(self) -> bool:
        return self.patch == 0

    @property
    def minor_key(self) -> tuple[int, int]:
        return self.major, self.minor

    def require_exact(self) -> "KubernetesVersion":
        if self.patch is None:
            raise UpgradeValidationError(f"Kubernetes version {self} is not an exact patch.")
        return self

    def __str__(self) -> str:
        suffix = "" if self.patch is None else f".{self.patch}"
        return f"v{self.major}.{self.minor}{suffix}"


def resolve_upgrade_target(
    requested: str,
    available: Iterable[str],
    *,
    allow_preview: bool = False,
) -> KubernetesVersion:
    target = KubernetesVersion.parse(requested)
    supported = sorted(
        {
            parsed
            for value in available
            if (parsed := KubernetesVersion.parse(value)).exact
        }
    )
    if target.exact:
        if target not in supported:
            raise UpgradeValidationError(
                f"OKE does not advertise {target} as an available cluster version."
            )
        resolved = target
    else:
        candidates = [item for item in supported if item.minor_key == target.minor_key]
        production = [item for item in candidates if not item.preview]
        if production:
            resolved = production[-1]
        elif candidates:
            resolved = candidates[-1]
        else:
            raise UpgradeValidationError(
                f"OKE does not advertise a supported patch for {target}."
            )
    if resolved.preview and not allow_preview:
        raise UpgradeValidationError(
            f"{resolved} is a preview .0 target; use --allow-preview to acknowledge it."
        )
    return resolved


def validate_control_plane_step(
    current: str,
    target: str,
) -> tuple[KubernetesVersion, KubernetesVersion]:
    source = KubernetesVersion.parse(current).require_exact()
    destination = KubernetesVersion.parse(target).require_exact()
    if destination <= source:
        direction = "downgrade" if destination < source else "no-op"
        raise UpgradeValidationError(
            f"Control-plane {direction} is not supported: {source} -> {destination}."
        )
    if source.major != destination.major:
        raise UpgradeValidationError(
            f"Control-plane major-version jumps are not supported: {source} -> {destination}."
        )
    if destination.minor > source.minor + 1:
        raise UpgradeValidationError(
            "OKE control-plane minor upgrades must be performed one minor version "
            f"at a time: {source} -> {destination}."
        )
    return source, destination


def control_plane_steps(
    current: str,
    target: str,
    available: Iterable[str],
) -> tuple[KubernetesVersion, ...]:
    source = KubernetesVersion.parse(current).require_exact()
    destination = KubernetesVersion.parse(target).require_exact()
    if destination <= source:
        validate_control_plane_step(str(source), str(destination))
    supported = sorted(
        parsed
        for value in available
        if (parsed := KubernetesVersion.parse(value)).exact
    )
    steps: list[KubernetesVersion] = []
    cursor = source
    while cursor.minor < destination.minor:
        next_minor = cursor.minor + 1
        candidates = [
            version
            for version in supported
            if version.major == cursor.major and version.minor == next_minor
        ]
        if not candidates:
            raise UpgradeValidationError(
                f"No supported OKE patch was found for v{cursor.major}.{next_minor}."
            )
        step = destination if destination.minor == next_minor else candidates[-1]
        validate_control_plane_step(str(cursor), str(step))
        steps.append(step)
        cursor = step
    if cursor < destination:
        validate_control_plane_step(str(cursor), str(destination))
        steps.append(destination)
    return tuple(steps)


def validate_worker_skew(control_plane: str, kubelet: str) -> None:
    control = KubernetesVersion.parse(control_plane).require_exact()
    worker = KubernetesVersion.parse(kubelet).require_exact()
    if worker.major != control.major:
        raise UpgradeValidationError(
            f"Kubelet {worker} and control plane {control} have different major versions."
        )
    if worker.minor > control.minor:
        raise UpgradeValidationError(
            f"Kubelet {worker} cannot be newer than control plane {control}."
        )
    if control.minor - worker.minor > 3:
        raise UpgradeValidationError(
            f"Kubelet {worker} is more than three minor versions behind {control}."
        )


def select_upgrade_strategy(pool: WorkerPoolInfo, requested: str) -> str:
    normalized = requested.lower()
    if normalized not in UPGRADE_STRATEGIES:
        raise UpgradeValidationError(
            f"Unknown upgrade strategy {requested!r}; choose "
            f"{', '.join(UPGRADE_STRATEGIES)}."
        )
    if normalized != "auto":
        return normalized
    if pool.kind == "node-pool":
        return "boot-volume-replace"
    return "instance-replace"


def validate_cycling_value(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not re.fullmatch(r"\d+%?", normalized):
        raise UpgradeValidationError(
            f"{name} must be a non-negative integer or percentage."
        )
    if normalized.endswith("%") and int(normalized[:-1]) > 100:
        raise UpgradeValidationError(f"{name} percentage cannot exceed 100%.")
    return normalized


def default_pool_order(pools: Iterable[WorkerPoolInfo]) -> tuple[str, ...]:
    def rank(pool: WorkerPoolInfo) -> tuple[int, str]:
        name = pool.name.casefold()
        is_gpu = bool(pool.gpu_resource)
        if not is_gpu and ("cpu" in name and "system" not in name):
            category = 0
        elif "system" in name:
            category = 1
        elif is_gpu and not pool.rdma_enabled:
            category = 2
        elif pool.kind == "node-pool" and pool.rdma_enabled:
            category = 3
        elif pool.kind == "cluster-network":
            category = 4
        elif pool.kind == "gpu-memory-cluster":
            category = 5
        else:
            category = 6
        return category, name

    return tuple(pool.name for pool in sorted(pools, key=rank))


class UpgradePhase(str, Enum):
    PLANNED = "planned"
    CONTROL_PLANE = "control-plane"
    WORKER_CONFIGS = "worker-configs"
    POOL_GATE = "pool-gate"
    POOL_UPGRADE = "pool-upgrade"
    VERIFY = "verify"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"


@dataclass(frozen=True)
class UpgradePoolState:
    name: str
    strategy: str
    image_id: str | None = None
    maximum_unavailable: str | None = None
    maximum_surge: str | None = None
    blue_green_name: str | None = None
    blue_green_compute_cluster_id: str | None = None
    blue_green_gpu_memory_fabric_id: str | None = None
    phase: str = "pending"
    previous_instance_configuration_id: str | None = None
    target_instance_configuration_id: str | None = None
    created_resource_ids: tuple[str, ...] = ()
    superseded_instance_configuration_ids: tuple[str, ...] = ()
    work_request_ids: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class UpgradeCheckpoint:
    operation_id: str
    cluster_id: str
    source_version: str
    target_version: str
    control_plane_steps: tuple[str, ...]
    pool_order: tuple[str, ...]
    pools: tuple[UpgradePoolState, ...]
    phase: UpgradePhase = UpgradePhase.PLANNED
    control_plane_index: int = 0
    pool_index: int = 0
    active_work_request_id: str | None = None
    acknowledged_application_compatibility: bool = False
    acknowledged_iac_drift: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: str | None = None

    @classmethod
    def create(
        cls,
        *,
        cluster_id: str,
        source_version: str,
        target_version: str,
        control_plane_steps: Iterable[str],
        pool_order: Iterable[str],
        strategies: dict[str, str],
        images: dict[str, str] | None = None,
        pool_options: dict[str, dict[str, str | None]] | None = None,
    ) -> "UpgradeCheckpoint":
        images = images or {}
        pool_options = pool_options or {}
        ordered = tuple(pool_order)

        def pool_state(name: str) -> UpgradePoolState:
            options = pool_options.get(name, {})
            return UpgradePoolState(
                name=name,
                strategy=strategies[name],
                image_id=images.get(name),
                maximum_unavailable=options.get("maximum_unavailable"),
                maximum_surge=options.get("maximum_surge"),
                blue_green_name=options.get("blue_green_name"),
                blue_green_compute_cluster_id=options.get(
                    "blue_green_compute_cluster_id"
                ),
                blue_green_gpu_memory_fabric_id=options.get(
                    "blue_green_gpu_memory_fabric_id"
                ),
            )

        return cls(
            operation_id=str(uuid.uuid4()),
            cluster_id=cluster_id,
            source_version=source_version,
            target_version=target_version,
            control_plane_steps=tuple(control_plane_steps),
            pool_order=ordered,
            pools=tuple(pool_state(name) for name in ordered),
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["phase"] = self.phase.value
        return json.dumps(
            {"schema_version": CHECKPOINT_SCHEMA_VERSION, "checkpoint": data},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "UpgradeCheckpoint":
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise UpgradeValidationError(f"Invalid upgrade checkpoint JSON: {exc}") from exc
        if document.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise UpgradeValidationError(
                "Unsupported upgrade checkpoint schema version: "
                f"{document.get('schema_version')!r}."
            )
        data = document.get("checkpoint")
        if not isinstance(data, dict):
            raise UpgradeValidationError("Upgrade checkpoint is missing its payload.")
        pools = data.get("pools", ())
        if not isinstance(pools, list):
            raise UpgradeValidationError("Upgrade checkpoint pools must be a list.")
        try:
            return cls(
                **{
                    **data,
                    "phase": UpgradePhase(data["phase"]),
                    "control_plane_steps": tuple(data.get("control_plane_steps", ())),
                    "pool_order": tuple(data.get("pool_order", ())),
                    "pools": tuple(
                        UpgradePoolState(
                            **{
                                **item,
                                "created_resource_ids": tuple(
                                    item.get("created_resource_ids", ())
                                ),
                                "superseded_instance_configuration_ids": tuple(
                                    item.get(
                                        "superseded_instance_configuration_ids",
                                        (),
                                    )
                                ),
                                "work_request_ids": tuple(
                                    item.get("work_request_ids", ())
                                ),
                            }
                        )
                        for item in pools
                    ),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpgradeValidationError(
                f"Invalid upgrade checkpoint payload: {exc}"
            ) from exc

    def replace(self, **changes: Any) -> "UpgradeCheckpoint":
        data = {
            **self.__dict__,
            **changes,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return UpgradeCheckpoint(**data)
