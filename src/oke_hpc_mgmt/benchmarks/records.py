from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import Any, Literal, cast

from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions, TopologyPlacement, case_name, validate_run_id
from oke_hpc_mgmt.benchmarks.models import (
    BenchmarkCase, BenchmarkError, BenchmarkParameters, HealthcheckPlan, HealthcheckRequest, NodeTarget, ServicePolicy,
)
from oke_hpc_mgmt.models import HealthResult


CaseState = Literal["PLANNED", "SUBMITTING", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"]
RunState = Literal["PLANNED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLING", "CANCELLED", "CLEANED"]
TERMINAL_CASE_STATES = frozenset({"SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"})
MAX_RECORD_BYTES = 700_000
MAX_SNAPSHOTS = 128
SnapshotPhase = Literal["QUEUED", "STARTING", "RUNNING", "TERMINAL", "CANCELLING"]


@dataclass(frozen=True)
class EvidenceSnapshot:
    observed_at: str
    phase: SnapshotPhase
    pod_identities: tuple[tuple[str, str, str | None, int], ...]
    sha256: str
    scheduling_sha256: str | None = None

    def validate(self) -> None:
        if self.phase not in {"QUEUED", "STARTING", "RUNNING", "TERMINAL", "CANCELLING"} or not (
            isinstance(self.sha256, str) and re.fullmatch(r"[a-f0-9]{64}", self.sha256)
        ):
            raise BenchmarkError("Invalid evidence snapshot phase or digest.")
        timestamp(self.observed_at)
        if self.scheduling_sha256 is not None and (
            not isinstance(self.scheduling_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.scheduling_sha256)
        ):
            raise BenchmarkError("Invalid scheduling diagnostics digest.")
        names: set[str] = set()
        for name, uid, container_id, restarts in self.pod_identities:
            if not isinstance(name, str) or not name or name in names or not isinstance(uid, str) or not uid or (
                container_id is not None and not isinstance(container_id, str)
            ) or type(restarts) is not int or restarts < 0:
                raise BenchmarkError("Invalid or duplicate Pod identity in evidence snapshot.")
            names.add(name)


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("Missing timezone")
        return parsed
    except (ValueError, AttributeError, TypeError) as exc:
        raise BenchmarkError("Checkpoint timestamps must be timezone-aware ISO timestamps.") from exc


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    job_name: str
    state: CaseState = "PLANNED"
    job_uid: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    evidence_sha256: str | None = None
    submitted_at: str | None = None
    admitted_at: str | None = None
    snapshots: tuple[EvidenceSnapshot, ...] = ()
    cancellation_delete_requested: bool = False


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    created_at: str
    plan: HealthcheckPlan
    options: ExecutionOptions
    cases: tuple[CaseRecord, ...]
    state: RunState = "PLANNED"
    revision: int = 1
    schema_version: int = 1
    uid: str | None = field(default=None, compare=False)
    resource_version: str | None = field(default=None, compare=False)
    stop_reason: str | None = None

    @classmethod
    def create(cls, run_id: str, plan: HealthcheckPlan, options: ExecutionOptions) -> RunRecord:
        validate_run_id(run_id)
        plan.request.validate()
        if plan.service_policy:
            plan.service_policy.validate()
        options.validate()
        if not plan.cases:
            raise BenchmarkError("Passive checks do not create persistent workload runs.")
        return cls(run_id, datetime.now(timezone.utc).isoformat(), plan, options, tuple(
            CaseRecord(case.case_id, case_name(run_id, case)) for case in plan.cases
        ))

    def update_case(self, case_id: str, state: CaseState, **changes: Any) -> RunRecord:
        current = next((item for item in self.cases if item.case_id == case_id), None)
        if current is None:
            raise BenchmarkError(f"Unknown case {case_id}.")
        transitions = {
            "PLANNED": {"PLANNED", "SUBMITTING", "CANCELLED"},
            "SUBMITTING": {"SUBMITTING", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"},
            "QUEUED": {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"},
            "RUNNING": {"RUNNING", "QUEUED", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"},
        }
        if self.state == "CLEANED" or state not in transitions.get(current.state, {current.state}):
            raise BenchmarkError(f"Invalid case transition {current.state} -> {state}.")
        if any(name not in {"job_uid", "created_at", "started_at", "completed_at", "result", "error", "evidence_sha256",
                            "submitted_at", "admitted_at", "snapshots", "cancellation_delete_requested"}
               for name in changes):
            raise BenchmarkError("A checkpoint update cannot change case identity or its plan.")
        if current.job_uid and changes.get("job_uid", current.job_uid) != current.job_uid:
            raise BenchmarkError("A replacement MPIJob must be recorded as a new attempt, not adopted.")
        if current.cancellation_delete_requested and changes.get("cancellation_delete_requested", True) is not True:
            raise BenchmarkError("Cancellation deletion intent cannot be cleared.")
        if changes.get("cancellation_delete_requested") is True and self.state != "CANCELLING":
            raise BenchmarkError("Persist cancellation before recording job deletion intent.")
        updated = replace(current, state=state, **changes)
        if updated.snapshots[:len(current.snapshots)] != current.snapshots:
            raise BenchmarkError("Previously exported evidence snapshots are append-only.")
        if current.state == "RUNNING" and state == "QUEUED" and (
            len(updated.snapshots) <= len(current.snapshots) or updated.snapshots[-1].phase != "QUEUED"
        ):
            raise BenchmarkError("A requeue requires a new exported observation; do not combine attempts.")
        if state in {"QUEUED", "RUNNING", "SUCCEEDED"} and not updated.job_uid:
            raise BenchmarkError("An observed workload state requires its MPIJob UID.")
        _validate_case(updated)
        cases = tuple(updated if item.case_id == case_id else item for item in self.cases)
        overall: RunState = "RUNNING"
        if all(item.state in TERMINAL_CASE_STATES for item in cases):
            if any(item.state == "FAILED" for item in cases):
                overall = "FAILED"
            elif any(item.state == "INCOMPLETE" for item in cases):
                overall = "INCOMPLETE"
            elif any(item.state == "CANCELLED" for item in cases):
                overall = "CANCELLED"
            else:
                overall = "SUCCEEDED"
        elif self.state == "CANCELLING":
            overall = "CANCELLING"
        return replace(self, cases=cases, state=overall, revision=self.revision + 1)

    def payload(self) -> str:
        value = asdict(self)
        value.pop("uid")
        value.pop("resource_version")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
            raise BenchmarkError("Run metadata exceeds the ConfigMap record limit; narrow the run.")
        return encoded


def _keys(value: Any, model: type) -> dict[str, Any]:
    if not isinstance(value, dict) or any(key not in {item.name for item in fields(model)} for key in value):
        raise BenchmarkError(f"Invalid {model.__name__} checkpoint structure.")
    return dict(value)


def decode_record(payload: str, *, uid: str, resource_version: str) -> RunRecord:
    if len(payload.encode("utf-8")) > MAX_RECORD_BYTES or not uid or not resource_version:
        raise BenchmarkError("Invalid run metadata size or Kubernetes identity.")
    try:
        value = _keys(json.loads(payload), RunRecord)
        if "uid" in value or "resource_version" in value or value.get("schema_version") != 1:
            raise BenchmarkError("Unsupported run schema or embedded Kubernetes object identity.")
        plan = _keys(value.pop("plan"), HealthcheckPlan)
        request = _keys(plan.pop("request"), HealthcheckRequest)
        request["parameters"] = BenchmarkParameters(**_keys(request.get("parameters", {}), BenchmarkParameters))
        for key in ("nodes", "checks", "environment"):
            request[key] = tuple(request.get(key, []))
        typed_request = HealthcheckRequest(**request)
        typed_request.validate()
        cases: list[BenchmarkCase] = []
        for case_value in plan.pop("cases"):
            case = _keys(case_value, BenchmarkCase)
            nodes: list[NodeTarget] = []
            for node_value in case.pop("nodes"):
                node = _keys(node_value, NodeTarget)
                node["topology"] = tuple(node["topology"]) if node.get("topology") else None
                node["tolerations"] = tuple(node.get("tolerations", []))
                if node.get("boot_id") is not None and (not isinstance(node["boot_id"], str) or not node["boot_id"]):
                    raise BenchmarkError("Invalid node boot identity in checkpoint.")
                nodes.append(NodeTarget(**node))
            case["nodes"], case["anchors"] = tuple(nodes), tuple(case["anchors"])
            if case.get("profile_fingerprint") is not None and (
                not isinstance(case["profile_fingerprint"], str) or not re.fullmatch(r"[a-f0-9]{64}", case["profile_fingerprint"])
            ):
                raise BenchmarkError("Invalid profile fingerprint in checkpoint.")
            cases.append(BenchmarkCase(**case))
        for key in ("selected_nodes", "passive_nodes", "warnings"):
            plan[key] = tuple(plan.get(key, []))
        plan["passive_checks"] = tuple(HealthResult(**_keys(item, HealthResult)) for item in plan.get("passive_checks", []))
        if plan.get("service_policy") is not None:
            plan["service_policy"] = ServicePolicy(**_keys(plan["service_policy"], ServicePolicy))
            plan["service_policy"].validate()
        typed_plan = HealthcheckPlan(request=typed_request, cases=tuple(cases), **plan)
        option_values = _keys(value.pop("options"), ExecutionOptions)
        placements: list[TopologyPlacement] = []
        for placement_value in option_values.pop("placements", []):
            placement = _keys(placement_value, TopologyPlacement)
            placement["hostnames"] = tuple(placement["hostnames"])
            placements.append(TopologyPlacement(**placement))
        options = ExecutionOptions(**option_values, placements=tuple(placements))
        options.validate()
        records_list: list[CaseRecord] = []
        for item in value.pop("cases"):
            case_record_values = _keys(item, CaseRecord)
            snapshots: list[EvidenceSnapshot] = []
            for snapshot_value in case_record_values.pop("snapshots", []):
                snapshot = _keys(snapshot_value, EvidenceSnapshot)
                snapshot["pod_identities"] = tuple(tuple(identity) for identity in snapshot["pod_identities"])
                snapshots.append(EvidenceSnapshot(**snapshot))
            records_list.append(CaseRecord(**case_record_values, snapshots=tuple(snapshots)))
        records = tuple(records_list)
        record = RunRecord(plan=typed_plan, options=options, cases=records, uid=uid,
                           resource_version=resource_version, **value)
        validate_run_id(record.run_id)
        if not records or len({case.case_id for case in records}) != len(records) or (
            tuple((case.case_id, case_name(record.run_id, case)) for case in cases)
            != tuple((case.case_id, case.job_name) for case in records)
        ):
            raise BenchmarkError("Checkpoint case identities differ from the immutable plan.")
        if type(record.revision) is not int or record.revision < 1 or record.state not in {
            "PLANNED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLING", "CANCELLED", "CLEANED",
        } or any(case.state not in TERMINAL_CASE_STATES | {"PLANNED", "SUBMITTING", "QUEUED", "RUNNING"}
                 for case in records):
            raise BenchmarkError("Invalid checkpoint state or revision.")
        for case_record in records:
            if case_record.state in {"QUEUED", "RUNNING", "SUCCEEDED"} and not case_record.job_uid:
                raise BenchmarkError("An observed workload state requires its MPIJob UID.")
            _validate_case(case_record)
        timestamp(record.created_at)
        if record.stop_reason is not None and (not isinstance(record.stop_reason, str) or not record.stop_reason):
            raise BenchmarkError("Invalid recorded cancellation reason.")
        if record.state == "SUCCEEDED" and any(item.state != "SUCCEEDED" for item in records):
            raise BenchmarkError("Run success requires success evidence for every case.")
        return record
    except (TypeError, KeyError, AttributeError, ValueError, OverflowError) as exc:
        if isinstance(exc, BenchmarkError):
            raise
        raise BenchmarkError("Malformed healthcheck checkpoint; no resources were modified.") from exc


def payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_success(case: CaseRecord) -> None:
    if case.state == "SUCCEEDED" and (
        not isinstance(case.result, dict) or case.result.get("status") != "PASS"
        or not re.fullmatch(r"[a-f0-9]{64}", case.evidence_sha256 or "")
    ):
        raise BenchmarkError("Successful cases require a passing assessment and exported evidence digest.")


def _validate_case(case: CaseRecord) -> None:
    _validate_success(case)
    if type(case.cancellation_delete_requested) is not bool or case.cancellation_delete_requested and (
        not case.job_uid or not re.fullmatch(r"[a-f0-9]{64}", case.evidence_sha256 or "")
    ):
        raise BenchmarkError("Cancellation deletion intent requires a boolean, recorded job UID, and exported evidence.")
    for value in (case.submitted_at, case.created_at, case.admitted_at, case.started_at, case.completed_at):
        if value is not None:
            timestamp(value)
    if len(case.snapshots) > MAX_SNAPSHOTS:
        raise BenchmarkError("Evidence snapshot limit reached; retain workloads and inspect run history.")
    for snapshot in case.snapshots:
        snapshot.validate()


def next_cases(record: RunRecord) -> tuple[BenchmarkCase, ...]:
    if record.state in {"CANCELLING", "CANCELLED", "FAILED", "INCOMPLETE", "SUCCEEDED", "CLEANED"}:
        return ()
    states = {case.case_id: case.state for case in record.cases}
    active = [case for case in record.plan.cases if states[case.case_id] in {"SUBMITTING", "QUEUED", "RUNNING"}]
    occupied = {node.uid for case in active for node in case.nodes}
    available = record.options.concurrency - len(active)
    selected: list[BenchmarkCase] = []
    for case in record.plan.cases:
        if available <= 0:
            break
        if states[case.case_id] != "PLANNED" or occupied.intersection(node.uid for node in case.nodes):
            continue
        selected.append(case)
        occupied.update(node.uid for node in case.nodes)
        available -= 1
    return tuple(selected)


def mark_cancelling(record: RunRecord, *, reason: str | None = None) -> RunRecord:
    if record.state == "CLEANED":
        raise BenchmarkError("A cleaned run cannot be cancelled.")
    if all(case.state in TERMINAL_CASE_STATES for case in record.cases):
        return record
    return replace(record, state=cast(RunState, "CANCELLING"), stop_reason=record.stop_reason or reason,
                   revision=record.revision + 1)


def assert_no_overlap(plan: HealthcheckPlan, existing: tuple[RunRecord, ...], *, exclude_uid: str | None = None) -> None:
    selected = {node.uid for case in plan.cases for node in case.nodes}
    for run in existing:
        if run.state == "CLEANED" or (exclude_uid is not None and run.uid == exclude_uid):
            continue
        states = {item.case_id: item.state for item in run.cases}
        occupied = {node.uid for case in run.plan.cases if states[case.case_id] not in TERMINAL_CASE_STATES
                    for node in case.nodes}
        if selected & occupied:
            raise BenchmarkError(f"Selected nodes overlap unfinished healthcheck {run.plan.request.namespace}/{run.run_id}.")
