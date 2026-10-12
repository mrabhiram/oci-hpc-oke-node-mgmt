from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from oke_hpc_mgmt.benchmarks.capabilities import LEASE_NAME, LEASE_NAMESPACE
from oke_hpc_mgmt.benchmarks.manifests import MANAGED_BY
from oke_hpc_mgmt.benchmarks.models import BenchmarkError

if TYPE_CHECKING:
    from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend


REQUEST_TIMEOUT = (5, 20)
SAFETY_MARGIN_SECONDS = 40


class SubmissionLease:
    """Short, renewable transaction lock; never held while waiting for a job."""

    def __init__(self, backend: BenchmarkBackend, *, duration_seconds: int = 300) -> None:
        if type(duration_seconds) is not int or not 120 <= duration_seconds <= 900:
            raise BenchmarkError("Submission lease duration must be an integer in 120..900 seconds.")
        self.backend = backend
        self.api = backend.client.CoordinationV1Api()
        self.duration = duration_seconds
        self.holder = "mgmt-oke-" + secrets.token_hex(16)
        self.uid: str | None = None
        self.resource_version: str | None = None
        self.deadline = 0.0

    @staticmethod
    def _validate(value: dict[str, Any]) -> dict[str, Any]:
        metadata, spec = value.get("metadata") or {}, value.get("spec") or {}
        if metadata.get("name") != LEASE_NAME or metadata.get("namespace") != LEASE_NAMESPACE or (
            (metadata.get("labels") or {}).get("app.kubernetes.io/managed-by") != MANAGED_BY
        ) or not metadata.get("uid") or not metadata.get("resourceVersion") or metadata.get("deletionTimestamp"):
            raise BenchmarkError("Submission lease identity or ownership is unverified; do not modify it.")
        duration = spec.get("leaseDurationSeconds")
        if type(duration) is not int or not 120 <= duration <= 900 or not spec.get("holderIdentity"):
            raise BenchmarkError("Submission lease has an invalid holder or duration.")
        return value

    @staticmethod
    def _expires(value: dict[str, Any]) -> datetime:
        spec = value["spec"]
        try:
            renewed = datetime.fromisoformat(spec["renewTime"].replace("Z", "+00:00"))
            if renewed.utcoffset() is None:
                raise ValueError("Missing timezone")
            return renewed + timedelta(seconds=spec["leaseDurationSeconds"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BenchmarkError("Submission lease renewal time is unverified; cannot reclaim it.") from exc

    def _read(self) -> dict[str, Any]:
        return self._validate(self.backend._object(self.api.read_namespaced_lease(
            LEASE_NAME, LEASE_NAMESPACE, _request_timeout=REQUEST_TIMEOUT,
        )))

    def _body(self, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        metadata = {"name": LEASE_NAME, "namespace": LEASE_NAMESPACE,
                    "labels": {"app.kubernetes.io/managed-by": MANAGED_BY}}
        if previous:
            metadata.update({key: previous["metadata"][key] for key in ("uid", "resourceVersion")})
        acquired = previous["spec"].get("acquireTime", now) if previous and previous["spec"]["holderIdentity"] == self.holder else now
        return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": metadata,
                "spec": {"holderIdentity": self.holder, "leaseDurationSeconds": self.duration,
                         "acquireTime": acquired, "renewTime": now}}

    def _record(self, value: Any, started: float) -> None:
        lease = self._validate(self.backend._object(value))
        if lease["spec"]["holderIdentity"] != self.holder or lease["spec"]["leaseDurationSeconds"] != self.duration:
            raise BenchmarkError("Submission lock acquisition was not confirmed.")
        if self.uid is not None and self.uid != lease["metadata"]["uid"]:
            raise BenchmarkError("Submission lease was replaced; stop mutations.")
        self.uid, self.resource_version = lease["metadata"]["uid"], lease["metadata"]["resourceVersion"]
        self.deadline = started + self.duration - SAFETY_MARGIN_SECONDS
        self.assert_held()

    def assert_held(self) -> None:
        if not self.uid or not self.resource_version or time.monotonic() >= self.deadline:
            raise BenchmarkError("Submission lease is absent or expired; reread current state before mutation.")

    def __enter__(self) -> SubmissionLease:
        if self.uid:
            raise BenchmarkError("Submission lease objects cannot be entered twice.")
        started = time.monotonic()
        try:
            try:
                previous = self._read()
            except ApiException as exc:
                if exc.status != 404:
                    raise
                previous = None
            if previous is None:
                value = self.api.create_namespaced_lease(LEASE_NAMESPACE, self._body(), _request_timeout=REQUEST_TIMEOUT)
            else:
                if self._expires(previous) + timedelta(seconds=SAFETY_MARGIN_SECONDS) > datetime.now(timezone.utc):
                    raise BenchmarkError("Another healthcheck submission transaction owns the lease; retry after it finishes.")
                value = self.api.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, self._body(previous),
                                                         _request_timeout=REQUEST_TIMEOUT)
            self._record(value, started)
            return self
        except (ApiException, HTTPError, OSError) as exc:
            raise BenchmarkError("Submission lease acquisition was not confirmed; no workload may be submitted.") from exc

    def renew(self) -> None:
        self.assert_held()
        started = time.monotonic()
        try:
            current = self._read()
            if current["metadata"]["uid"] != self.uid or current["metadata"]["resourceVersion"] != self.resource_version or (
                current["spec"]["holderIdentity"] != self.holder
            ):
                raise BenchmarkError("Submission lease ownership changed; stop mutations and reread the run.")
            self.assert_held()
            value = self.api.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, self._body(current),
                                                     _request_timeout=REQUEST_TIMEOUT)
            self._record(value, started)
        except BenchmarkError:
            self.deadline = 0.0
            raise
        except (ApiException, HTTPError, OSError) as exc:
            self.deadline = 0.0
            raise BenchmarkError("Submission lease renewal failed; stop mutations and reread current state.") from exc

    def release(self) -> None:
        if not self.uid:
            return
        try:
            current = self._read()
            if current["metadata"]["uid"] != self.uid or current["spec"]["holderIdentity"] != self.holder:
                return
            self.api.delete_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, body={
                "apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
                    "uid": current["metadata"]["uid"], "resourceVersion": current["metadata"]["resourceVersion"],
                },
            }, _request_timeout=REQUEST_TIMEOUT)
        except ApiException as exc:
            if exc.status != 404:
                raise BenchmarkError("Submission lease release was not confirmed; it will expire without renewal.") from exc
        except (HTTPError, OSError) as exc:
            raise BenchmarkError("Submission lease release could not reach the API; it will expire without renewal.") from exc
        finally:
            self.deadline = 0.0

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        try:
            self.release()
        except BenchmarkError:
            if exc is None:
                raise
