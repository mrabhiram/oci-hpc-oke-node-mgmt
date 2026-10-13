from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from oke_hpc_mgmt.benchmarks.capabilities import KUEUE_GROUP, served_version
from oke_hpc_mgmt.benchmarks.manifests import MPI_GROUP, MPI_VERSION, case_name
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError
from oke_hpc_mgmt.benchmarks.records import RunRecord

if TYPE_CHECKING:
    from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend


REQUEST_TIMEOUT = (10, 30)
MAX_WORKLOADS = 16
MAX_EVENTS = 128
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
JOB_UID_LABEL = "kueue.x-k8s.io/job-uid"


@dataclass(frozen=True)
class SchedulingEvidence:
    workloads: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()

    def signature(self) -> str:
        # Repeated event counts and resource versions do not constitute another
        # scheduling decision. Preserve them in exports without snapshot churn.
        workloads = [{"uid": item["metadata"]["uid"], "active": item.get("active", True), "status": item["status"]}
                     for item in self.workloads]
        events = [{key: item.get(key) for key in ("involvedObject", "reason", "message", "type")}
                  for item in self.events]
        for event in events:
            event["involvedObject"] = {key: value for key, value in (event["involvedObject"] or {}).items()
                                       if key != "resourceVersion"}
        value = {"workloads": workloads, "events": events}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SchedulingCollector:
    """Bounded read-only diagnostics, tied to owned object UIDs rather than names."""

    def __init__(self, backend: BenchmarkBackend) -> None:
        self.backend = backend

    @staticmethod
    def _items(response: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items = response.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items) or (
            len(items) > limit or (response.get("metadata") or {}).get("continue")
        ):
            raise BenchmarkError("Scheduling diagnostics exceed collection limits; retain resources for inspection.")
        return items

    def owned_workloads(self, run: RunRecord, case: BenchmarkCase, job_uid: str) -> list[dict[str, Any]]:
        if not run.plan.request.queue:
            return []
        crd = self.backend._object(self.backend.client.ApiextensionsV1Api().read_custom_resource_definition(
            f"workloads.{KUEUE_GROUP}", _request_timeout=REQUEST_TIMEOUT,
        ))
        version = served_version(crd, ("v1beta2", "v1beta1"), namespaced=True)
        namespace = run.plan.request.namespace
        response = self.backend._object(self.backend.custom.list_namespaced_custom_object(
            KUEUE_GROUP, version, namespace, "workloads", label_selector=f"{JOB_UID_LABEL}={job_uid}",
            limit=MAX_WORKLOADS, _request_timeout=REQUEST_TIMEOUT,
        ))
        result: list[dict[str, Any]] = []
        identities: set[str] = set()
        for item in self._items(response, MAX_WORKLOADS):
            metadata, spec = item.get("metadata") or {}, item.get("spec") or {}
            owners = [owner for owner in metadata.get("ownerReferences") or [] if owner.get("controller") is True]
            if len(owners) != 1 or any(owners[0].get(key) != value for key, value in {
                "apiVersion": f"{MPI_GROUP}/{MPI_VERSION}", "kind": "MPIJob",
                "name": case_name(run.run_id, case), "uid": job_uid,
            }.items()) or metadata.get("namespace") != namespace or not all(
                isinstance(metadata.get(key), str) and metadata[key] for key in ("name", "uid", "resourceVersion")
            ) or spec.get("queueName") != run.plan.request.queue or metadata.get("uid") in identities:
                raise BenchmarkError("Cannot verify Kueue Workload ownership and queue; retain resources.")
            identities.add(metadata["uid"])
            status = item.get("status") or {}
            if not isinstance(status, dict) or type(spec.get("active", True)) is not bool:
                raise BenchmarkError("Invalid Kueue Workload status in scheduling diagnostics.")
            result.append({"apiVersion": f"{KUEUE_GROUP}/{version}", "kind": "Workload",
                           "metadata": {key: metadata[key] for key in ("name", "namespace", "uid", "resourceVersion")},
                           "queue_name": spec["queueName"], "active": spec.get("active", True), "status": copy.deepcopy(status)})
        return sorted(result, key=lambda item: item["metadata"]["uid"])

    def collect(
        self, run: RunRecord, case: BenchmarkCase, job_uid: str,
        pods: tuple[tuple[str, str], ...],
    ) -> SchedulingEvidence:
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not saved or not job_uid or saved.job_uid != job_uid:
            raise BenchmarkError("Scheduling diagnostics require the recorded MPIJob UID.")
        namespace = run.plan.request.namespace
        try:
            workloads = self.owned_workloads(run, case, job_uid)
            objects = [("MPIJob", case_name(run.run_id, case), job_uid),
                       *(("Pod", name, uid) for name, uid in pods),
                       *(("Workload", item["metadata"]["name"], item["metadata"]["uid"]) for item in workloads)]
            events: list[dict[str, Any]] = []
            for kind, name, uid in objects:
                response = self.backend._object(self.backend.core.list_namespaced_event(
                    namespace, field_selector=f"involvedObject.uid={uid}", limit=MAX_EVENTS,
                    _request_timeout=REQUEST_TIMEOUT,
                ))
                for item in self._items(response, MAX_EVENTS):
                    involved, metadata = item.get("involvedObject") or {}, item.get("metadata") or {}
                    if any(involved.get(key) != value for key, value in {
                        "kind": kind, "name": name, "uid": uid, "namespace": namespace,
                    }.items()) or metadata.get("namespace") != namespace or not metadata.get("uid"):
                        raise BenchmarkError("Event does not match the verified workload object UID.")
                    events.append({
                        "metadata": {key: metadata.get(key) for key in ("name", "namespace", "uid", "resourceVersion")},
                        "involvedObject": copy.deepcopy(involved),
                        **{key: copy.deepcopy(item.get(key)) for key in (
                            "reason", "message", "type", "count", "firstTimestamp", "lastTimestamp", "eventTime",
                            "series", "action", "reportingComponent", "reportingInstance",
                        )},
                    })
                    if len(events) > MAX_EVENTS:
                        raise BenchmarkError("Too many scheduling events; retain resources for inspection.")
            evidence = SchedulingEvidence(tuple(workloads), tuple(sorted(events, key=lambda item: item["metadata"]["uid"])))
            if len(json.dumps(asdict(evidence), allow_nan=False).encode()) > MAX_DIAGNOSTIC_BYTES:
                raise BenchmarkError("Scheduling diagnostics exceed the export budget; retain resources.")
            return evidence
        except (ApiException, HTTPError, OSError) as exc:
            raise BenchmarkError("Cannot read scheduling diagnostics; retain workloads and retry observation.") from exc
