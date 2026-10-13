from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from oke_hpc_mgmt.benchmarks.diagnostics import SchedulingCollector, SchedulingEvidence
from oke_hpc_mgmt.benchmarks.manifests import (
    CASE_LABEL, MPI_GROUP, MPI_VERSION, ROLE_LABEL, RUN_LABEL, case_name,
)
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError
from oke_hpc_mgmt.benchmarks.records import RunRecord
from oke_hpc_mgmt.benchmarks.results import NcclVerdict, assess_nccl, parse_nccl_output

if TYPE_CHECKING:
    from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend


MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_CASE_LOG_BYTES = 32 * 1024 * 1024
REQUEST_TIMEOUT = (10, 30)


@dataclass(frozen=True)
class PodEvidence:
    name: str
    uid: str
    role: Literal["worker", "launcher"]
    node_name: str | None
    node_uid: str | None
    phase: str
    image: str
    image_id: str | None
    container_id: str | None
    restart_count: int
    exit_code: int | None
    log: str
    log_complete: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CaseEvidence:
    case_id: str
    job_uid: str
    job_resource_version: str
    succeeded: bool
    failed: bool
    suspended: bool
    pods: tuple[PodEvidence, ...]
    warnings: tuple[str, ...]
    scheduling: SchedulingEvidence = SchedulingEvidence()
    observation_stable: bool = True

    @property
    def coverage_complete(self) -> bool:
        return not self.warnings and bool(self.pods) and all(not pod.warnings for pod in self.pods)

    @property
    def terminal_observation_ready(self) -> bool:
        if not self.observation_stable:
            return False
        if self.failed:
            return True
        launchers = [pod for pod in self.pods if pod.role == "launcher"]
        return self.succeeded and len(launchers) == 1 and launchers[0].exit_code is not None


def _controller(metadata: dict[str, Any], kind: str, api_version: str) -> dict[str, Any]:
    owners = [owner for owner in metadata.get("ownerReferences") or [] if owner.get("controller") is True]
    if len(owners) != 1 or owners[0].get("kind") != kind or owners[0].get("apiVersion") != api_version or (
        not owners[0].get("name") or not owners[0].get("uid")
    ):
        raise BenchmarkError(f"Expected one verified {kind} controller owner for benchmark evidence.")
    return dict(owners[0])


def _container_status(pod: dict[str, Any], container: str) -> dict[str, Any]:
    statuses = [item for item in (pod.get("status") or {}).get("containerStatuses") or []
                if item.get("name") == container]
    if len(statuses) > 1:
        raise BenchmarkError("Duplicate benchmark container status.")
    return dict(statuses[0]) if statuses else {}


def _attempt_identity(pod: dict[str, Any], container: str) -> tuple[str, str | None, int, str | None]:
    metadata = pod.get("metadata") or {}
    status = _container_status(pod, container)
    restarts = status.get("restartCount", 0)
    if type(restarts) is not int or restarts < 0 or not metadata.get("uid"):
        raise BenchmarkError("Invalid Pod UID or restart count in benchmark evidence.")
    return metadata["uid"], (pod.get("spec") or {}).get("nodeName"), restarts, status.get("containerID")


class EvidenceCollector:
    """Read owned workload evidence without accepting labels as ownership proof."""

    def __init__(self, backend: BenchmarkBackend) -> None:
        self.backend = backend
        self.core = backend.core
        self.batch = backend.client.BatchV1Api()

    def _verify_pod(
        self, pod: dict[str, Any], run: RunRecord, case: BenchmarkCase, job_uid: str,
    ) -> tuple[Literal["worker", "launcher"], str]:
        metadata = pod.get("metadata") or {}
        self.backend._verify_labels(metadata, run.run_id)
        labels = metadata.get("labels") or {}
        role = labels.get(ROLE_LABEL)
        if labels.get(CASE_LABEL) != case.case_id or role not in {"worker", "launcher"} or (
            metadata.get("namespace") != run.plan.request.namespace
        ) or not metadata.get("name") or not metadata.get("uid"):
            raise BenchmarkError("Pod does not match the requested healthcheck case and namespace.")
        if role == "launcher":
            owner = _controller(metadata, "Job", "batch/v1")
            parent = self.backend._object(self.batch.read_namespaced_job(
                owner["name"], run.plan.request.namespace, _request_timeout=REQUEST_TIMEOUT,
            ))
            parent_metadata = parent.get("metadata") or {}
            if parent_metadata.get("uid") != owner["uid"] or (
                parent_metadata.get("namespace") != run.plan.request.namespace
            ) or parent_metadata.get("name") != owner["name"]:
                raise BenchmarkError("Launcher Job identity changed; refusing replacement evidence.")
            owner = _controller(parent_metadata, "MPIJob", f"{MPI_GROUP}/{MPI_VERSION}")
        else:
            owner = _controller(metadata, "MPIJob", f"{MPI_GROUP}/{MPI_VERSION}")
        if owner["uid"] != job_uid or owner["name"] != case_name(run.run_id, case):
            raise BenchmarkError("Pod controller chain does not belong to the recorded MPIJob UID.")
        container = "mpi-worker" if role == "worker" else "mpi-launcher"
        containers = (pod.get("spec") or {}).get("containers") or []
        if len(containers) != 1 or containers[0].get("name") != container:
            raise BenchmarkError("Unexpected benchmark container layout; cannot prove one attempt.")
        return role, container

    def _pod(
        self, listed: dict[str, Any], run: RunRecord, case: BenchmarkCase, job_uid: str, log_budget: int,
    ) -> PodEvidence:
        namespace = run.plan.request.namespace
        self._verify_pod(listed, run, case, job_uid)
        name = listed["metadata"]["name"]
        before = self.backend._object(self.core.read_namespaced_pod(name, namespace, _request_timeout=REQUEST_TIMEOUT))
        role, container = self._verify_pod(before, run, case, job_uid)
        if before["metadata"]["uid"] != listed["metadata"]["uid"]:
            raise BenchmarkError("Pod was replaced after discovery; start a separate evidence attempt.")
        identity = _attempt_identity(before, container)
        status = _container_status(before, container)
        warnings: list[str] = []
        log = ""
        complete = False
        limit = min(MAX_LOG_BYTES, log_budget)
        if status.get("containerID") and limit > 0:
            try:
                response = self.core.read_namespaced_pod_log(
                    name, namespace, container=container, timestamps=False,
                    limit_bytes=limit + 1, _preload_content=False, _request_timeout=REQUEST_TIMEOUT,
                )
                # Bypass SDK str(bytes) conversion, which can escape all log
                # newlines. Bound the raw response even if the server ignores
                # limitBytes, and release the connection on every path.
                try:
                    encoded = response.read(limit + 1)
                finally:
                    response.close()
                    response.release_conn()
                if not isinstance(encoded, bytes):
                    raise BenchmarkError("Kubernetes returned a non-byte benchmark log stream.")
                complete = len(encoded) <= limit
                try:
                    log = encoded[:limit].decode("utf-8")
                except UnicodeDecodeError:
                    log = encoded[:limit].decode("utf-8", errors="replace")
                    complete = False
                    warnings.append("Log is not complete UTF-8 text; invalid bytes cannot establish success.")
                if len(encoded) > limit:
                    warnings.append("Log reached the evidence size limit; truncated output cannot establish success.")
            except ApiException as exc:
                warnings.append(f"Container log unavailable (Kubernetes HTTP {exc.status}).")
            except (HTTPError, OSError):
                warnings.append("Container log stream failed; complete runtime evidence is unavailable.")
        elif not status.get("containerID"):
            warnings.append("Container has not started; runtime evidence is unavailable.")
        else:
            warnings.append("Case log budget exhausted; additional logs were not collected.")
        after = self.backend._object(self.core.read_namespaced_pod(name, namespace, _request_timeout=REQUEST_TIMEOUT))
        self._verify_pod(after, run, case, job_uid)
        if _attempt_identity(after, container) != identity:
            raise BenchmarkError("Pod or container changed while collecting logs; do not combine attempts.")
        after_status = _container_status(after, container)
        terminated = (after_status.get("state") or {}).get("terminated") or {}
        exit_code = terminated.get("exitCode")
        if exit_code is not None and type(exit_code) is not int:
            raise BenchmarkError("Invalid benchmark container exit code.")
        # A running-to-terminated transition can leave the fetched log short of
        # its final rows. Collect again after termination before assessing it.
        if (status.get("state") or {}).get("terminated") != (after_status.get("state") or {}).get("terminated"):
            complete = False
            warnings.append("Container completed while logs were read; collect its final log again.")
        if identity[2]:
            warnings.append("Container restarted; this snapshot is not a single complete attempt.")
        node_uid: str | None = None
        if identity[1]:
            node = self.backend._object(self.core.read_node(identity[1], _request_timeout=REQUEST_TIMEOUT))
            node_metadata = node.get("metadata") or {}
            node_uid = node_metadata.get("uid")
            if node_metadata.get("name") != identity[1] or not node_uid:
                raise BenchmarkError("Node identity is unavailable for the observed Pod.")
            target = next((item for item in case.nodes if item.name == identity[1]), None)
            if target and target.boot_id and (node.get("status") or {}).get("nodeInfo", {}).get("bootID") != target.boot_id:
                warnings.append("Selected node boot identity changed; this observation cannot establish success.")
        if role == "worker" and (identity[1], node_uid) not in {(node.name, node.uid) for node in case.nodes}:
            warnings.append("Worker is not on an originally selected node UID.")
        image_id = after_status.get("imageID")
        if not image_id or re.search(r"(?:@|://|^)sha256:[a-f0-9]{64}$", image_id) is None:
            warnings.append("Resolved container image digest is unavailable.")
        if not complete:
            warnings.append("Complete container logs were not obtained.")
        return PodEvidence(
            name, identity[0], role, identity[1], node_uid, (after.get("status") or {}).get("phase", "Unknown"),
            before["spec"]["containers"][0].get("image", ""), image_id, identity[3], identity[2],
            exit_code, log, complete, tuple(warnings),
        )

    def collect(self, run: RunRecord, case: BenchmarkCase) -> CaseEvidence:
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not saved or not saved.job_uid:
            raise BenchmarkError("Persist the MPIJob UID before collecting execution evidence.")
        job = self.backend.get_job(run, case)
        if job is None:
            raise BenchmarkError("MPIJob is missing; its absence does not establish a completed test.")
        namespace = run.plan.request.namespace
        selector = f"{RUN_LABEL}={run.run_id},{CASE_LABEL}={case.case_id}"
        try:
            listed = self.backend._object(self.core.list_namespaced_pod(
                namespace, label_selector=selector,
                limit=2 * len(case.nodes) + 16, _request_timeout=REQUEST_TIMEOUT,
            ))
            pods = listed.get("items") or []
            if (listed.get("metadata") or {}).get("continue") or len(pods) > 2 * len(case.nodes) + 16:
                raise BenchmarkError("Too many Pods for one benchmark attempt; inspect requeues before collecting evidence.")
            for pod in pods:
                self._verify_pod(pod, run, case, saved.job_uid)
            collected: list[PodEvidence] = []
            budget = MAX_CASE_LOG_BYTES
            # Read launcher output first; large worker inventories must not
            # consume the entire bounded evidence budget before NCCL results.
            for pod in sorted(pods, key=lambda item: (item.get("metadata") or {}).get("labels", {}).get(ROLE_LABEL) != "launcher"):
                observed = self._pod(pod, run, case, saved.job_uid, budget)
                collected.append(observed)
                budget -= len(observed.log.encode("utf-8"))
            evidence = tuple(sorted(collected, key=lambda item: (item.role == "launcher", item.name)))
            latest = self.backend._object(self.core.list_namespaced_pod(
                namespace, label_selector=selector, limit=2 * len(case.nodes) + 16,
                _request_timeout=REQUEST_TIMEOUT,
            ))
            latest_identities: set[tuple[str, str, str | None, int, str | None]] = set()
            for pod in latest.get("items") or []:
                _, container = self._verify_pod(pod, run, case, saved.job_uid)
                latest_identities.add((pod["metadata"]["name"], *_attempt_identity(pod, container)))
            recorded_identities = {(pod.name, pod.uid, pod.node_name, pod.restart_count, pod.container_id)
                                   for pod in evidence}
            if (latest.get("metadata") or {}).get("continue") or latest_identities != recorded_identities:
                raise BenchmarkError("Pod set changed during evidence collection; do not combine attempts.")
            scheduling = SchedulingCollector(self.backend).collect(
                run, case, saved.job_uid, tuple((pod.name, pod.uid) for pod in evidence),
            )
            final = self.backend.get_job(run, case)
        except ApiException as exc:
            raise BenchmarkError(f"Cannot verify owned benchmark evidence (Kubernetes HTTP {exc.status}).") from exc
        if final is None or final["metadata"]["uid"] != saved.job_uid:
            raise BenchmarkError("MPIJob disappeared or was replaced during evidence collection.")
        warnings: list[str] = []
        stable = final.get("status") == job.get("status") and final.get("spec") == job.get("spec")
        if not stable:
            warnings.append("MPIJob changed during collection; refresh before assessing success.")
        workers = [pod for pod in evidence if pod.role == "worker"]
        launchers = [pod for pod in evidence if pod.role == "launcher"]
        expected = sorted((node.name, node.uid) for node in case.nodes)
        observed_nodes = sorted((pod.node_name or "", pod.node_uid or "") for pod in workers)
        if expected != observed_nodes:
            warnings.append("Worker coverage is missing, duplicated, or replaced; do not combine attempts.")
        if len(launchers) != 1:
            warnings.append("Exactly one launcher Pod is required for an unambiguous attempt.")
        conditions = (final.get("status") or {}).get("conditions") or []
        succeeded = any(item.get("type") == "Succeeded" and item.get("status") == "True" for item in conditions)
        failed = any(item.get("type") == "Failed" and item.get("status") == "True" for item in conditions)
        suspended = (final.get("spec") or {}).get("runPolicy", {}).get("suspend") is True
        if succeeded and failed:
            warnings.append("MPIJob has conflicting terminal conditions.")
        if suspended:
            warnings.append("MPIJob is suspended; historical conditions do not prove this attempt completed.")
        return CaseEvidence(case.case_id, saved.job_uid, final["metadata"]["resourceVersion"], succeeded, failed, suspended,
                            evidence, tuple(warnings), scheduling, stable)


def assess_case(
    case: BenchmarkCase, evidence: CaseEvidence, *, min_bus_bandwidth: float | None = None,
) -> NcclVerdict:
    if evidence.case_id != case.case_id:
        raise BenchmarkError("Evidence belongs to a different benchmark case.")
    launchers = [pod for pod in evidence.pods if pod.role == "launcher"]
    workers = [pod for pod in evidence.pods if pod.role == "worker"]
    launcher = launchers[0] if len(launchers) == 1 else None
    result = assess_nccl(
        parse_nccl_output(launcher.log if launcher else ""), expected_ranks=case.ranks,
        expected_nodes=tuple((node.name, node.uid) for node in case.nodes),
        observed_nodes=tuple((pod.node_name or "", pod.node_uid or "") for pod in workers),
        observed_workers=tuple((pod.node_name or "", pod.uid) for pod in workers),
        job_succeeded=evidence.succeeded and not evidence.failed and not evidence.suspended,
        exit_code=launcher.exit_code if launcher else None, rdma_required=case.network != "local",
        min_bus_bandwidth=min_bus_bandwidth,
    )
    reasons = [*evidence.warnings, *(warning for pod in evidence.pods for warning in pod.warnings)]
    if any(not pod.log_complete for pod in evidence.pods):
        reasons.append("One or more container logs are incomplete.")
    worker_failed = any(pod.exit_code is not None and pod.exit_code != 0 for pod in workers)
    if worker_failed:
        reasons.append("A benchmark worker exited unsuccessfully.")
    if evidence.failed:
        reasons.append("MPIJob reports a failed execution.")
    if not reasons:
        return result
    return replace(
        result, status="FAIL" if result.status == "FAIL" or worker_failed or evidence.failed else "INCOMPLETE",
        correctness="UNVERIFIED" if result.correctness == "PASS" else result.correctness,
        performance="UNGRADED" if result.performance == "PASS" else result.performance,
        reasons=tuple(dict.fromkeys((*result.reasons, *reasons))),
    )
