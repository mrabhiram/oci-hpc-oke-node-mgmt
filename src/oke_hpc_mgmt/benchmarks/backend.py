from __future__ import annotations

from dataclasses import replace
from typing import Any

from kubernetes.client.exceptions import ApiException

from oke_hpc_mgmt.backends.kubernetes import KubernetesBackend
from oke_hpc_mgmt.benchmarks.manifests import (
    MANAGED_BY, MPI_GROUP, MPI_PLURAL, MPI_VERSION, case_name, owned_labels,
    render_mpijob, validate_run_id,
)
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError
from oke_hpc_mgmt.benchmarks.preflight import PreflightEvidence, validate_runtime_nodes
from oke_hpc_mgmt.benchmarks.records import RunRecord, decode_record, payload_digest


REQUEST_TIMEOUT = (10, 30)
MAX_RUNS = 1000
MAX_HISTORY_BYTES = 32 * 1024 * 1024


class BenchmarkBackend:
    """Namespaced benchmark resources only; no node or OCI mutation methods."""

    def __init__(self, kubernetes: KubernetesBackend) -> None:
        self.kubernetes = kubernetes
        self.client = kubernetes.client
        self.core = self.client.CoreV1Api()
        self.custom = self.client.CustomObjectsApi()

    def _object(self, value: Any) -> dict[str, Any]:
        result = self.client.ApiClient().sanitize_for_serialization(value)
        if not isinstance(result, dict):
            raise BenchmarkError("Kubernetes returned an unexpected benchmark resource response.")
        return result

    def runtime_preflight(self, case: BenchmarkCase) -> PreflightEvidence:
        try:
            nodes = [self._object(self.core.read_node(node.name, _request_timeout=REQUEST_TIMEOUT))
                     for node in case.nodes]
            pods = self._object(self.core.list_pod_for_all_namespaces(_request_timeout=REQUEST_TIMEOUT))
        except ApiException as exc:
            raise BenchmarkError(f"Cannot verify current node/GPU allocations (Kubernetes HTTP {exc.status}).") from exc
        if (pods.get("metadata") or {}).get("continue"):
            raise BenchmarkError("GPU allocation inventory is incomplete; refusing submission.")
        return validate_runtime_nodes(case, nodes, pods.get("items", []))

    def _record(self, value: Any) -> RunRecord:
        obj = self._object(value)
        metadata, data = obj.get("metadata") or {}, obj.get("data") or {}
        run_id = metadata.get("name", "")
        validate_run_id(run_id)
        self._verify_labels(metadata, run_id)
        payload = data.get("record.json")
        if not isinstance(payload, str) or data.get("sha256") != payload_digest(payload):
            raise BenchmarkError("Checkpoint integrity validation failed.")
        record = decode_record(payload, uid=metadata.get("uid", ""),
                               resource_version=metadata.get("resourceVersion", ""))
        if record.run_id != run_id or record.plan.request.namespace != metadata.get("namespace"):
            raise BenchmarkError("Checkpoint namespace or run identity does not match its Kubernetes object.")
        if metadata.get("deletionTimestamp"):
            raise BenchmarkError("The run checkpoint is being deleted; refusing further mutations.")
        return record

    @staticmethod
    def _verify_labels(metadata: dict[str, Any], run_id: str) -> None:
        labels = metadata.get("labels") or {}
        if any(labels.get(key) != value for key, value in owned_labels(run_id).items()):
            raise BenchmarkError("Resource is not owned by this mgmt-oke healthcheck run.")

    def get_run(self, namespace: str, run_id: str) -> RunRecord:
        validate_run_id(run_id)
        try:
            value = self.core.read_namespaced_config_map(run_id, namespace, _request_timeout=REQUEST_TIMEOUT)
        except ApiException as exc:
            raise BenchmarkError(f"Cannot read run {run_id} (Kubernetes HTTP {exc.status}).") from exc
        return self._record(value)

    def list_runs(self, namespace: str | None) -> tuple[RunRecord, ...]:
        records: list[RunRecord] = []
        continuation: str | None = None
        seen: set[str] = set()
        size = 0
        try:
            while True:
                options: dict[str, Any] = {"label_selector": f"app.kubernetes.io/managed-by={MANAGED_BY}",
                                           "limit": 100, "_request_timeout": REQUEST_TIMEOUT}
                if continuation:
                    options["_continue"] = continuation
                values = self._object(self.core.list_config_map_for_all_namespaces(**options) if namespace is None
                                      else self.core.list_namespaced_config_map(namespace, **options))
                for item in values.get("items") or []:
                    record = self._record(item)
                    size += len(record.payload().encode("utf-8"))
                    if len(records) >= MAX_RUNS or size > MAX_HISTORY_BYTES:
                        raise BenchmarkError("Benchmark history exceeds safe inventory limits; archive completed runs before submission.")
                    if any(previous.uid == record.uid for previous in records):
                        raise BenchmarkError("Benchmark history contains duplicate run UIDs; inventory is unverified.")
                    records.append(record)
                continuation = (values.get("metadata") or {}).get("continue")
                if not continuation:
                    break
                if continuation in seen:
                    raise BenchmarkError("Benchmark history pagination repeated; inventory is incomplete.")
                seen.add(continuation)
        except ApiException as exc:
            raise BenchmarkError(f"Cannot list benchmark runs (Kubernetes HTTP {exc.status}).") from exc
        return tuple(sorted(records, key=lambda item: item.created_at))

    def create_run(self, record: RunRecord) -> RunRecord:
        if record.uid or record.resource_version or record.revision != 1 or record.state != "PLANNED":
            raise BenchmarkError("Only a new planned run may create a checkpoint.")
        payload = record.payload()
        decode_record(payload, uid="validation-only", resource_version="validation-only")
        for case in record.plan.cases:
            render_mpijob(record.run_id, case, record.plan.request, record.options)
        body = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": record.run_id, "namespace": record.plan.request.namespace,
                         "labels": owned_labels(record.run_id)},
            "data": {"record.json": payload, "sha256": payload_digest(payload)},
        }
        try:
            value = self.core.create_namespaced_config_map(
                record.plan.request.namespace, body, _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            raise BenchmarkError(
                f"Checkpoint creation was not confirmed (Kubernetes HTTP {exc.status}); "
                f"inspect run {record.run_id} before retrying."
            ) from exc
        return self._record(value)

    def save_run(self, record: RunRecord) -> RunRecord:
        if not record.uid or not record.resource_version:
            raise BenchmarkError("Checkpoint updates require Kubernetes UID and resource version.")
        current = self.get_run(record.plan.request.namespace, record.run_id)
        if current.uid != record.uid or current.resource_version != record.resource_version:
            raise BenchmarkError("Checkpoint was replaced or concurrently modified; reread before continuing.")
        if current.plan != record.plan or current.options != record.options or current.created_at != record.created_at:
            raise BenchmarkError("A run's plan and execution options are immutable.")
        if record.revision != current.revision + 1:
            raise BenchmarkError("Checkpoint updates must advance exactly one revision.")
        payload = record.payload()
        body = {"metadata": {"uid": record.uid, "resourceVersion": record.resource_version},
                "data": {"record.json": payload, "sha256": payload_digest(payload)}}
        try:
            value = self.core.patch_namespaced_config_map(
                record.run_id, record.plan.request.namespace, body, _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            raise BenchmarkError(f"Checkpoint update was not confirmed (Kubernetes HTTP {exc.status}); reread the run.") from exc
        return self._record(value)

    def _verify_job(self, obj: dict[str, Any], run: RunRecord, case: BenchmarkCase) -> dict[str, Any]:
        if case not in run.plan.cases:
            raise BenchmarkError("Case does not match the checkpoint's immutable plan.")
        metadata = obj.get("metadata") or {}
        self._verify_labels(metadata, run.run_id)
        if metadata.get("name") != case_name(run.run_id, case) or metadata.get("namespace") != run.plan.request.namespace:
            raise BenchmarkError("MPIJob identity does not match the planned case.")
        if not run.uid or not any(
            owner.get("apiVersion") == "v1" and owner.get("kind") == "ConfigMap"
            and owner.get("uid") == run.uid and owner.get("name") == run.run_id
            for owner in metadata.get("ownerReferences") or []
        ):
            raise BenchmarkError("MPIJob does not belong to the recorded run ConfigMap UID.")
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not saved or not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise BenchmarkError("MPIJob or checkpoint lacks a recorded Kubernetes identity.")
        if saved.job_uid and saved.job_uid != metadata["uid"]:
            raise BenchmarkError("MPIJob was replaced; refusing to adopt, cancel, or delete the replacement.")
        return obj

    def _require_current(self, run: RunRecord, case: BenchmarkCase) -> None:
        if case not in run.plan.cases:
            raise BenchmarkError("Case does not match the checkpoint's immutable plan.")
        current = self.get_run(run.plan.request.namespace, run.run_id)
        if current.uid != run.uid or current.resource_version != run.resource_version or (
            current.payload() != run.payload()
        ):
            raise BenchmarkError("Persist and reread the current checkpoint before a workload mutation.")

    def get_job(self, run: RunRecord, case: BenchmarkCase) -> dict[str, Any] | None:
        try:
            value = self.custom.get_namespaced_custom_object(
                MPI_GROUP, MPI_VERSION, run.plan.request.namespace, MPI_PLURAL,
                case_name(run.run_id, case), _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise BenchmarkError(f"Cannot inspect MPIJob (Kubernetes HTTP {exc.status}).") from exc
        return self._verify_job(self._object(value), run, case)

    def create_job(self, run: RunRecord, case: BenchmarkCase) -> dict[str, Any]:
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not run.uid or not saved or saved.state != "SUBMITTING" or saved.job_uid:
            raise BenchmarkError("Persist a SUBMITTING checkpoint before creating its MPIJob.")
        self._require_current(run, case)
        body = render_mpijob(run.run_id, case, run.plan.request, run.options, owner_uid=run.uid)
        try:
            value = self.custom.create_namespaced_custom_object(
                MPI_GROUP, MPI_VERSION, run.plan.request.namespace, MPI_PLURAL, body,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            raise BenchmarkError(
                f"MPIJob submission was not confirmed (Kubernetes HTTP {exc.status}); "
                "reread the existing case before retrying."
            ) from exc
        return self._verify_job(self._object(value), run, case)

    def cancel_job(self, run: RunRecord, case: BenchmarkCase) -> None:
        if run.state != "CANCELLING":
            raise BenchmarkError("Persist cancellation intent before suspending a benchmark.")
        self._require_current(run, case)
        job = self.get_job(run, case)
        if job is None:
            return
        metadata = job["metadata"]
        body = {"metadata": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]},
                "spec": {"runPolicy": {"suspend": True}}}
        try:
            self.custom.patch_namespaced_custom_object(
                MPI_GROUP, MPI_VERSION, run.plan.request.namespace, MPI_PLURAL,
                metadata["name"], body, _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            raise BenchmarkError(f"Cancellation was not confirmed (Kubernetes HTTP {exc.status}); reread before retrying.") from exc

    def delete_job(self, run: RunRecord, case: BenchmarkCase) -> None:
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not saved or not saved.evidence_sha256 or saved.state not in {"SUCCEEDED", "FAILED", "INCOMPLETE", "CANCELLED"}:
            raise BenchmarkError("Export terminal-case evidence before deleting its only in-cluster copy.")
        self._require_current(run, case)
        job = self.get_job(run, case)
        if job is None:
            return
        metadata = job["metadata"]
        body = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
                "preconditions": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}}
        try:
            self.custom.delete_namespaced_custom_object(
                MPI_GROUP, MPI_VERSION, run.plan.request.namespace, MPI_PLURAL,
                metadata["name"], body=body, _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise BenchmarkError(f"Cleanup was not confirmed (Kubernetes HTTP {exc.status}); reread the case.") from exc

    def record_job_uid(self, run: RunRecord, case: BenchmarkCase, job: dict[str, Any]) -> RunRecord:
        verified = self._verify_job(job, run, case)
        updated = run.update_case(case.case_id, "QUEUED", job_uid=verified["metadata"]["uid"],
                                  created_at=verified["metadata"].get("creationTimestamp"))
        return self.save_run(replace(updated, uid=run.uid, resource_version=run.resource_version))
