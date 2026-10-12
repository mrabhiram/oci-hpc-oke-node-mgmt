from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock, patch

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ProtocolError

from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend
from oke_hpc_mgmt.benchmarks.capabilities import LEASE_NAME, LEASE_NAMESPACE
from oke_hpc_mgmt.benchmarks.lease import SubmissionLease
from oke_hpc_mgmt.benchmarks.manifests import MANAGED_BY
from oke_hpc_mgmt.benchmarks.models import BenchmarkError


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        kubernetes = Mock()
        kubernetes.client.ApiClient.return_value = ApiClient()
        self.backend = BenchmarkBackend(kubernetes)
        self.lease = SubmissionLease(self.backend)
        self.api = self.lease.api
        self.current: dict[str, Any] | None = None
        self.api.read_namespaced_lease.side_effect = self.read
        self.api.create_namespaced_lease.side_effect = self.create
        self.api.replace_namespaced_lease.side_effect = self.replace

    def read(self, name: str, namespace: str, **_: Any) -> dict[str, Any]:
        self.assertEqual((LEASE_NAME, LEASE_NAMESPACE), (name, namespace))
        if self.current is None:
            raise ApiException(status=404)
        return copy.deepcopy(self.current)

    def create(self, namespace: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.assertEqual(LEASE_NAMESPACE, namespace)
        if self.current is not None:
            raise ApiException(status=409)
        self.current = copy.deepcopy(body)
        self.current["metadata"].update(uid="lease-uid", resourceVersion="1")
        return copy.deepcopy(self.current)

    def replace(self, name: str, namespace: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.assertEqual((LEASE_NAME, LEASE_NAMESPACE), (name, namespace))
        assert self.current is not None
        self.assertEqual(self.current["metadata"]["uid"], body["metadata"]["uid"])
        self.assertEqual(self.current["metadata"]["resourceVersion"], body["metadata"]["resourceVersion"])
        self.current = copy.deepcopy(body)
        self.current["metadata"]["resourceVersion"] = str(int(body["metadata"]["resourceVersion"]) + 1)
        return copy.deepcopy(self.current)

    def existing(self, *, expired: bool = False) -> dict[str, Any]:
        now = datetime.now(timezone.utc) - timedelta(hours=1 if expired else 0)
        return {"metadata": {"name": LEASE_NAME, "namespace": LEASE_NAMESPACE, "uid": "lease-uid", "resourceVersion": "1",
                             "labels": {"app.kubernetes.io/managed-by": MANAGED_BY}},
                "spec": {"holderIdentity": "another-client", "leaseDurationSeconds": 300,
                         "renewTime": now.isoformat(), "acquireTime": now.isoformat()}}

    def test_acquire_renew_release_use_only_benchmark_lease_and_compare_and_swap(self) -> None:
        with self.lease as acquired:
            self.assertIs(self.lease, acquired)
            self.lease.assert_held()
            assert self.current is not None
            acquire_time = self.current["spec"]["acquireTime"]
            self.lease.renew()
            self.assertEqual("2", self.lease.resource_version)
            self.assertEqual(acquire_time, self.current["spec"]["acquireTime"])
        body = self.api.delete_namespaced_lease.call_args.kwargs["body"]
        self.assertEqual({"uid": "lease-uid", "resourceVersion": "2"}, body["preconditions"])
        with self.assertRaisesRegex(BenchmarkError, "expired"):
            self.lease.assert_held()
        self.assertEqual([], self.backend.core.mock_calls)
        self.assertEqual([], self.backend.custom.mock_calls)

    def test_live_lease_blocks_instead_of_stealing(self) -> None:
        self.current = self.existing()
        with self.assertRaisesRegex(BenchmarkError, "Another healthcheck"):
            self.lease.__enter__()
        self.api.create_namespaced_lease.assert_not_called()
        self.api.replace_namespaced_lease.assert_not_called()
        self.api.delete_namespaced_lease.assert_not_called()

    def test_expired_lease_takeover_requires_unchanged_resource_version(self) -> None:
        self.current = self.existing(expired=True)
        self.lease.__enter__()
        self.assertEqual(self.lease.holder, self.current["spec"]["holderIdentity"])
        self.assertEqual("2", self.lease.resource_version)
        self.api.create_namespaced_lease.assert_not_called()

    def test_acquisition_conflict_read_denial_and_network_failure_never_grant_lock(self) -> None:
        for error in (ApiException(status=409), ApiException(status=403), ProtocolError("network failure")):
            lease = SubmissionLease(self.backend)
            self.api.create_namespaced_lease.side_effect = error
            with self.subTest(error=error), self.assertRaisesRegex(BenchmarkError, "not confirmed"):
                lease.__enter__()
            with self.assertRaises(BenchmarkError):
                lease.assert_held()
        self.api.read_namespaced_lease.side_effect = ApiException(status=403)
        with self.assertRaises(BenchmarkError):
            SubmissionLease(self.backend).__enter__()

    def test_reclaim_conflict_does_not_delete_or_overwrite_other_owner(self) -> None:
        self.current = self.existing(expired=True)
        self.api.replace_namespaced_lease.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "not confirmed"):
            self.lease.__enter__()
        self.api.delete_namespaced_lease.assert_not_called()

    def test_foreign_corrupt_and_terminating_leases_are_not_touched(self) -> None:
        for modification in ("name", "namespace", "label", "uid", "duration", "terminating", "renewTime"):
            self.current = self.existing(expired=True)
            if modification in {"name", "namespace", "uid"}:
                self.current["metadata"][modification] = ""
            elif modification == "label":
                self.current["metadata"]["labels"] = {}
            elif modification == "duration":
                self.current["spec"]["leaseDurationSeconds"] = True
            elif modification == "terminating":
                self.current["metadata"]["deletionTimestamp"] = "timestamp"
            else:
                self.current["spec"]["renewTime"] = "2026-01-01T00:00:00"
            with self.subTest(modification=modification), self.assertRaises(BenchmarkError):
                SubmissionLease(self.backend).__enter__()
        self.api.replace_namespaced_lease.assert_not_called()
        self.api.delete_namespaced_lease.assert_not_called()

    def test_changed_owner_uid_or_resource_version_invalidates_client(self) -> None:
        for modification in ("holder", "uid", "resourceVersion"):
            self.current = None
            lease = SubmissionLease(self.backend)
            lease.__enter__()
            assert self.current is not None
            if modification == "holder":
                self.current["spec"]["holderIdentity"] = "new-owner"
            else:
                self.current["metadata"][modification] = "new"
            with self.subTest(modification=modification), self.assertRaisesRegex(BenchmarkError, "ownership changed"):
                lease.renew()
            with self.assertRaises(BenchmarkError):
                lease.assert_held()

    def test_renew_failure_invalidates_lock_even_before_original_expiry(self) -> None:
        self.lease.__enter__()
        self.api.replace_namespaced_lease.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "renewal failed"):
            self.lease.renew()
        with self.assertRaises(BenchmarkError):
            self.lease.assert_held()

    def test_monotonic_expiry_accounts_for_slow_acquisition(self) -> None:
        with patch("oke_hpc_mgmt.benchmarks.lease.time.monotonic", side_effect=[0.0, 280.0]):
            with self.assertRaisesRegex(BenchmarkError, "expired"):
                self.lease.__enter__()
        self.current = None
        lease = SubmissionLease(self.backend)
        with patch("oke_hpc_mgmt.benchmarks.lease.time.monotonic", return_value=100.0):
            lease.__enter__()
        with patch("oke_hpc_mgmt.benchmarks.lease.time.monotonic", return_value=361.0):
            with self.assertRaises(BenchmarkError):
                lease.renew()
        self.api.replace_namespaced_lease.assert_not_called()

    def test_release_never_deletes_a_successor_and_missing_is_idempotent(self) -> None:
        self.lease.release()
        self.api.read_namespaced_lease.assert_not_called()
        self.lease.__enter__()
        assert self.current is not None
        self.current["spec"]["holderIdentity"] = "successor"
        self.lease.release()
        self.api.delete_namespaced_lease.assert_not_called()
        self.current = None
        self.lease.release()

    def test_release_race_is_guarded_by_delete_preconditions(self) -> None:
        self.lease.__enter__()
        self.api.delete_namespaced_lease.side_effect = ApiException(status=409)
        with self.assertRaisesRegex(BenchmarkError, "release was not confirmed"):
            self.lease.release()
        self.assertEqual({"uid": "lease-uid", "resourceVersion": "1"},
                         self.api.delete_namespaced_lease.call_args.kwargs["body"]["preconditions"])

    def test_context_exit_preserves_primary_failure_if_release_also_fails(self) -> None:
        self.api.delete_namespaced_lease.side_effect = ApiException(status=500)
        with self.assertRaisesRegex(RuntimeError, "original error"):
            with self.lease:
                raise RuntimeError("original error")
        self.current = None
        with self.assertRaisesRegex(BenchmarkError, "release was not confirmed"):
            with SubmissionLease(self.backend):
                pass

    def test_duration_and_double_entry_are_rejected(self) -> None:
        for duration in (True, 0, 119, 901):
            with self.subTest(duration=duration), self.assertRaises(BenchmarkError):
                SubmissionLease(self.backend, duration_seconds=duration)
        self.lease.__enter__()
        with self.assertRaisesRegex(BenchmarkError, "twice"):
            self.lease.__enter__()
