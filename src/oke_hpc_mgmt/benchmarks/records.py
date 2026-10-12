from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import Any, Literal, cast

from oke_hpc_mgmt.benchmarks.manifests import ExecutionOptions, TopologyPlacement, case_name, validate_run_id
from oke_hpc_mgmt.benchmarks.models import (
    BenchmarkCase, BenchmarkError, BenchmarkParameters, HealthcheckPlan, HealthcheckRequest, NodeTarget,
)


CaseState = Literal["PLANNED", "SUBMITTING", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"]
RunState = Literal["PLANNED", "RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLING", "CANCELLED", "CLEANED"]
TERMINAL_CASE_STATES = frozenset({"SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"})
MAX_RECORD_BYTES = 700_000


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

    @classmethod
    def create(cls, run_id: str, plan: HealthcheckPlan, options: ExecutionOptions) -> RunRecord:
        validate_run_id(run_id)
        plan.request.validate()
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
            "RUNNING": {"RUNNING", "SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"},
        }
        if self.state == "CLEANED" or state not in transitions.get(current.state, {current.state}):
            raise BenchmarkError(f"Invalid case transition {current.state} -> {state}.")
        if any(name not in {"job_uid", "created_at", "started_at", "completed_at", "result", "error", "evidence_sha256"}
               for name in changes):
            raise BenchmarkError("A checkpoint update cannot change case identity or its plan.")
        if current.job_uid and changes.get("job_uid", current.job_uid) != current.job_uid:
            raise BenchmarkError("A replacement MPIJob must be recorded as a new attempt, not adopted.")
        updated = replace(current, state=state, **changes)
        if state in {"QUEUED", "RUNNING", "SUCCEEDED"} and not updated.job_uid:
            raise BenchmarkError("An observed workload state requires its MPIJob UID.")
        _validate_success(updated)
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
                nodes.append(NodeTarget(**node))
            case["nodes"], case["anchors"] = tuple(nodes), tuple(case["anchors"])
            cases.append(BenchmarkCase(**case))
        for key in ("selected_nodes", "passive_nodes", "warnings"):
            plan[key] = tuple(plan.get(key, []))
        typed_plan = HealthcheckPlan(request=typed_request, cases=tuple(cases), **plan)
        option_values = _keys(value.pop("options"), ExecutionOptions)
        placements: list[TopologyPlacement] = []
        for placement_value in option_values.pop("placements", []):
            placement = _keys(placement_value, TopologyPlacement)
            placement["hostnames"] = tuple(placement["hostnames"])
            placements.append(TopologyPlacement(**placement))
        options = ExecutionOptions(**option_values, placements=tuple(placements))
        options.validate()
        records = tuple(CaseRecord(**_keys(item, CaseRecord)) for item in value.pop("cases"))
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
            _validate_success(case_record)
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


def mark_cancelling(record: RunRecord) -> RunRecord:
    if record.state == "CLEANED":
        raise BenchmarkError("A cleaned run cannot be cancelled.")
    if all(case.state in TERMINAL_CASE_STATES for case in record.cases):
        return record
    return replace(record, state=cast(RunState, "CANCELLING"), revision=record.revision + 1)


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
