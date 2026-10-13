from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, cast

from oke_hpc_mgmt.benchmarks.models import (
    BenchmarkCase, BenchmarkError, HealthcheckPlan, HealthcheckRequest, NodeTarget, ServicePolicy,
)
from oke_hpc_mgmt.benchmarks.planning import node_target, pair_allowed, resolve_network, select_healthcheck_nodes
from oke_hpc_mgmt.benchmarks.preflight import validate_runtime_nodes
from oke_hpc_mgmt.benchmarks.profiles import execution_fingerprint
from oke_hpc_mgmt.benchmarks.records import RunRecord, TERMINAL_CASE_STATES, timestamp
from oke_hpc_mgmt.models import DiscoverySnapshot


@dataclass(frozen=True)
class PriorAssessment:
    run_id: str
    completed_at: datetime
    status: Literal["PASS", "FAIL", "INCOMPLETE"]
    failed_partners: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class ServiceDecision:
    node: str
    state: str
    reason: str
    prior_run: str | None = None
    partner: str | None = None


@dataclass(frozen=True)
class ServiceSelection:
    decisions: tuple[ServiceDecision, ...]
    plan: HealthcheckPlan | None


def latest_assessment(
    target: NodeTarget, category: str, fingerprint: str, runs: tuple[RunRecord, ...], now: datetime,
) -> PriorAssessment | None:
    """Only a matching node boot and recorded test contract can establish freshness."""
    if not target.boot_id:
        return None
    candidates: list[PriorAssessment] = []
    for run in runs:
        matched = [(case, saved) for case, saved in zip(run.plan.cases, run.cases)
                   if case.check_type == category and case.profile_fingerprint == fingerprint
                   and any(node.uid == target.uid and node.boot_id == target.boot_id and node.pool == target.pool
                           for node in case.nodes)]
        if not matched or any(saved.state not in TERMINAL_CASE_STATES for _, saved in matched):
            continue
        completed = max(timestamp(saved.completed_at or run.created_at) for _, saved in matched)
        if now.utcoffset() is None or completed > now:
            raise BenchmarkError("Healthcheck history is ahead of the observation clock; freshness is unverified.")
        passed = all(saved.state == "SUCCEEDED" and saved.completed_at and saved.evidence_sha256
                     and saved.result and all(saved.result.get(key) == "PASS" for key in ("status", "correctness", "coverage"))
                     and saved.result.get("transport") == ("LOCAL" if case.network == "local" else "RDMA")
                     for case, saved in matched)
        failed = any(saved.state == "FAILED" for _, saved in matched)
        partners = {(node.uid, node.boot_id) for case, saved in matched if saved.state == "FAILED"
                    for node in case.nodes if node.uid != target.uid}
        candidates.append(PriorAssessment(run.run_id, completed, "PASS" if passed else "FAIL" if failed else "INCOMPLETE",
                                          tuple(sorted(partners, key=lambda item: (item[0], item[1] or "")))))
    return max(candidates, key=lambda item: (item.completed_at, item.status != "PASS", item.run_id), default=None)


def validate_service_idle(
    case: BenchmarkCase, raw_nodes: list[dict[str, Any]], pods: list[dict[str, Any]],
    policy: ServicePolicy, now: datetime,
) -> None:
    policy.validate()
    if now.utcoffset() is None:
        raise BenchmarkError("Service observation time must include a timezone.")
    validate_runtime_nodes(case, raw_nodes, pods)
    selected = {target.name for target in case.nodes}
    for target in case.nodes:
        raw = next(node for node in raw_nodes if node["metadata"]["name"] == target.name)
        if not target.boot_id:
            raise BenchmarkError(f"{target.name} lacks a verified boot identity for service freshness.")
        ready = [condition for condition in (raw.get("status") or {}).get("conditions") or []
                 if condition.get("type") == "Ready"]
        if len(ready) != 1 or not ready[0].get("lastTransitionTime"):
            raise BenchmarkError(f"{target.name} Ready stability time is unavailable.")
        since = timestamp(ready[0]["lastTransitionTime"])
        if (now - since).total_seconds() < policy.min_ready_seconds:
            raise BenchmarkError(f"{target.name} has not been stably Ready for {policy.min_ready_seconds}s.")
    for pod in pods:
        if (pod.get("spec") or {}).get("nodeName") not in selected or (
            (pod.get("status") or {}).get("phase") in {"Succeeded", "Failed"}
        ):
            continue
        metadata = pod.get("metadata") or {}
        owners = [owner for owner in metadata.get("ownerReferences") or [] if owner.get("controller") is True]
        daemon = len(owners) == 1 and owners[0].get("kind") == "DaemonSet" and owners[0].get("apiVersion") == "apps/v1" and owners[0].get("uid")
        mirror = (metadata.get("annotations") or {}).get("kubernetes.io/config.mirror")
        if not daemon and not mirror:
            raise BenchmarkError(f"Application Pod {metadata.get('namespace')}/{metadata.get('name')} prevents an idle service test.")


def due_reason(prior: PriorAssessment | None, policy: ServicePolicy, now: datetime) -> tuple[bool, str]:
    if prior is None:
        return True, "Initial validation: no matching completed evidence."
    age = (now - prior.completed_at).total_seconds()
    if age < 0:
        raise BenchmarkError("Healthcheck completion is in the future; do not schedule a retry.")
    limit = policy.interval_seconds if prior.status == "PASS" else policy.retry_seconds
    if age < limit:
        return False, "Matching evidence is fresh." if prior.status == "PASS" else "Previous non-passing attempt is in retry cooldown."
    return True, "Periodic validation is overdue." if prior.status == "PASS" else "Retry non-passing evidence without inferring hardware fault."


def active_prerequisite(prior: PriorAssessment | None, policy: ServicePolicy, now: datetime) -> bool:
    return bool(prior and prior.status == "PASS" and policy.active_cooldown_seconds <= (
        now - prior.completed_at
    ).total_seconds() < policy.active_validity_seconds)


def validate_service_case(
    case: BenchmarkCase, request: HealthcheckRequest, policy: ServicePolicy,
    raw_nodes: list[dict[str, Any]], pods: list[dict[str, Any]], runs: tuple[RunRecord, ...], now: datetime,
) -> None:
    """Recheck a confirmed selection under the submission Lease, never substitute nodes."""
    validate_service_idle(case, raw_nodes, pods, policy, now)
    if case.profile_fingerprint != execution_fingerprint(case.nodes[0].shape, case.network, request):
        raise BenchmarkError("Service execution profile changed; prepare and confirm a new pass.")
    for target in case.nodes:
        prior = latest_assessment(target, case.check_type, case.profile_fingerprint, runs, now)
        if target.name in case.anchors and not due_reason(prior, policy, now)[0]:
            raise BenchmarkError(f"{target.name} is no longer due; prepare a new service pass.")
        if case.check_type == "multi-node":
            active = latest_assessment(target, "active", execution_fingerprint(target.shape, "local", request), runs, now)
            if not active_prerequisite(active, policy, now):
                raise BenchmarkError(f"{target.name} needs a recent passing active check and cooldown before pairing.")
            if prior and prior.status == "FAIL" and any(
                (partner.uid, partner.boot_id) in prior.failed_partners for partner in case.nodes if partner.uid != target.uid
            ):
                raise BenchmarkError("Suspect pair requires a different partner; do not repeat the same failure automatically.")


def select_service_plan(
    snapshot: DiscoverySnapshot, request: HealthcheckRequest, policy: ServicePolicy,
    raw_nodes: list[dict[str, Any]], pods: list[dict[str, Any]], runs: tuple[RunRecord, ...], now: datetime,
) -> ServiceSelection:
    request.validate()
    policy.validate()
    if request.check_type not in {"active", "multi-node"} or request.mode not in {None, "pairs"} or request.parameters.repetitions != 1:
        raise BenchmarkError("Service passes require active or pairwise multi-node checks and one repetition per case.")
    if request.exclude_node or request.reference_node or request.pairing != "anchor":
        raise BenchmarkError("Service partner choice follows recorded history; manual pairing overrides are unsupported.")
    if request.check_type == "active" and request.network not in {"auto", "local"}:
        raise BenchmarkError("Single-node active service checks require the local network profile.")
    selected = select_healthcheck_nodes(snapshot, request)
    selected_names = {node.k8s_name for node in selected}
    pools = {node.pool_name for node in selected}
    candidates = tuple(node for node in snapshot.nodes if node.pool_name in pools) if request.nodes and request.check_type == "multi-node" else selected
    decisions: dict[str, ServiceDecision] = {}
    targets: dict[str, NodeTarget] = {}
    history: dict[str, PriorAssessment | None] = {}
    nodes = {node.k8s_name: node for node in candidates}
    occupied = {target.uid for run in runs for case, saved in zip(run.plan.cases, run.cases)
                if saved.state not in TERMINAL_CASE_STATES for target in case.nodes}
    for node in sorted(candidates, key=lambda item: item.k8s_name):
        name = node.k8s_name
        try:
            target = node_target(node)
            probe = BenchmarkCase("case-0001", "active", (target,), (name,), "local", 1)
            validate_service_idle(probe, raw_nodes, pods, policy, now)
            if target.uid in occupied:
                raise BenchmarkError("An unfinished benchmark already reserves this node identity.")
            network = "local" if request.check_type == "active" else resolve_network((node,), request, False)
            fingerprint = execution_fingerprint(target.shape, network, request)
            prior = latest_assessment(target, request.check_type, fingerprint, runs, now)
            if request.check_type == "multi-node":
                active = latest_assessment(target, "active", execution_fingerprint(target.shape, "local", request), runs, now)
                if not active_prerequisite(active, policy, now):
                    decisions[name] = ServiceDecision(name, "NEEDS_ACTIVE", "Requires recent passing active evidence and cooldown.")
                    continue
            targets[name], history[name] = target, prior
            due, reason = due_reason(prior, policy, now)
            decisions[name] = ServiceDecision(name, "DUE" if due else "FRESH" if prior and prior.status == "PASS" else "COOLDOWN",
                                               reason, prior.run_id if prior else None)
        except BenchmarkError as exc:
            decisions[name] = ServiceDecision(name, "INELIGIBLE", str(exc))
    cases: list[BenchmarkCase] = []
    used: set[str] = set()
    anchors = sorted((prior.completed_at if prior else datetime.min.replace(tzinfo=timezone.utc), name)
                     for name, prior in history.items() if name in selected_names and decisions[name].state == "DUE")
    for _, name in anchors:
        if name in used:
            continue
        if len(cases) >= policy.max_cases:
            decisions[name] = replace(decisions[name], state="DEFERRED", reason="Bounded service case limit reached.")
            continue
        participants: tuple[NodeTarget, ...] = (targets[name],)
        case_anchors: tuple[str, ...] = (name,)
        network = "local"
        if request.check_type == "multi-node":
            partners = []
            for other in sorted(targets):
                if other in used or not pair_allowed(nodes[name], nodes[other], request):
                    continue
                prior, counterpart = history[name], history[other]
                if prior and (targets[other].uid, targets[other].boot_id) in prior.failed_partners:
                    continue
                if counterpart and counterpart.status != "PASS":
                    continue
                partners.append(other)
            partners.sort(key=lambda other: (history[other] is None, other))
            if not partners:
                decisions[name] = replace(decisions[name], state="NO_PARTNER", reason="No idle compatible alternate partner with passing active evidence.")
                continue
            other = partners[0]
            participants = tuple(sorted((targets[name], targets[other]), key=lambda item: item.name))
            if other in selected_names and decisions[other].state == "DUE":
                case_anchors = tuple(sorted((name, other)))
            network = resolve_network((nodes[name], nodes[other]), request, False)
            decisions[other] = replace(decisions[other], state="SELECTED", reason="Selected as compatible pair participant.", partner=name)
        case = BenchmarkCase(f"case-{len(cases) + 1:04d}", cast(Literal["active", "multi-node"], request.check_type), participants, case_anchors, network, 1,
                             execution_fingerprint(participants[0].shape, network, request))
        validate_service_case(case, request, policy, raw_nodes, pods, runs, now)
        cases.append(case)
        used.update(target.name for target in participants)
        decisions[name] = replace(decisions[name], state="SELECTED", partner=next((target.name for target in participants if target.name != name), None))
    plan = HealthcheckPlan(request, tuple(sorted({anchor for case in cases for anchor in case.anchors})), tuple(cases),
                          warnings=("NCCL component service pass only; not complete Slurm host diagnostic parity.",),
                          service_policy=policy) if cases else None
    return ServiceSelection(tuple(decisions[name] for name in sorted(decisions)), plan)
