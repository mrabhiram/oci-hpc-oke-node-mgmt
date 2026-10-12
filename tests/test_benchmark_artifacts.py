from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from oke_hpc_mgmt.benchmarks.artifacts import EvidenceStore
from oke_hpc_mgmt.benchmarks.evidence import CaseEvidence, PodEvidence
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from test_benchmark_records import record


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "results"
        self.store = EvidenceStore(self.root)
        initial = replace(record(), uid="run-uid", resource_version="1")
        self.case = initial.plan.cases[0]
        self.checkpoint = initial.update_case(self.case.case_id, "SUBMITTING").update_case(
            self.case.case_id, "RUNNING", job_uid="job-uid",
        )
        self.evidence = CaseEvidence(self.case.case_id, "job-uid", "2", False, False, False, (
            PodEvidence("worker-0", "pod-uid", "worker", "gpu-01", "uid-1", "Running",
                        "example.invalid/nccl:synthetic", "sha256:" + "a" * 64, "container-id", 0, None,
                        "synthetic raw log\n", True, ()),
        ), ("incomplete synthetic snapshot",))

    def test_export_is_private_immutable_and_verified_with_raw_logs(self) -> None:
        receipt = self.store.export(self.checkpoint, self.case, self.evidence)
        path = self.root / receipt.relative_path
        self.assertTrue(path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
        self.assertEqual(receipt.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
        payload = self.store.verify(self.checkpoint, self.case, receipt.sha256)
        self.assertEqual("synthetic raw log\n", payload["evidence"]["pods"][0]["log"])
        self.assertFalse(payload["evidence"]["succeeded"])
        self.assertEqual(self.checkpoint.uid, payload["run_uid"])
        self.assertEqual([path.name], [item.name for item in path.parent.iterdir()])

    def test_duplicate_snapshot_can_reuse_identical_content_without_overwrite(self) -> None:
        with patch("oke_hpc_mgmt.benchmarks.artifacts.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-01-01T00:00:00+00:00"
            first = self.store.export(self.checkpoint, self.case, self.evidence)
            path = self.root / first.relative_path
            inode = path.stat().st_ino
            second = self.store.export(self.checkpoint, self.case, self.evidence)
        self.assertEqual(first, second)
        self.assertEqual(inode, path.stat().st_ino)

    def test_snapshots_from_different_attempts_do_not_replace_logs(self) -> None:
        first = self.store.export(self.checkpoint, self.case, self.evidence)
        second = self.store.export(self.checkpoint, self.case, replace(self.evidence, pods=(
            replace(self.evidence.pods[0], uid="different-pod-uid", log="different attempt"),
        )))
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertTrue((self.root / first.relative_path).is_file())
        self.assertTrue((self.root / second.relative_path).is_file())

    def test_missing_case_identity_and_replacement_jobs_are_not_exported(self) -> None:
        for run, case, evidence in (
            (replace(self.checkpoint, uid=None), self.case, self.evidence),
            (self.checkpoint, replace(self.case, case_id="case-9999"), self.evidence),
            (record(), self.case, self.evidence),
            (self.checkpoint, self.case, replace(self.evidence, job_uid="replacement")),
            (self.checkpoint, self.case, replace(self.evidence, case_id="case-9999")),
        ):
            with self.subTest(run_uid=run.uid, case=case.case_id), self.assertRaises(BenchmarkError):
                self.store.export(run, case, evidence)
        self.assertFalse(self.root.exists())

    def test_modified_missing_or_public_files_never_authorize_cleanup(self) -> None:
        receipt = self.store.export(self.checkpoint, self.case, self.evidence)
        path = self.root / receipt.relative_path
        original = path.read_bytes()
        path.write_bytes(b"modified")
        with self.assertRaisesRegex(BenchmarkError, "checksum"):
            self.store.verify(self.checkpoint, self.case, receipt.sha256)
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(BenchmarkError, "private"):
            self.store.verify(self.checkpoint, self.case, receipt.sha256)
        path.unlink()
        with self.assertRaisesRegex(BenchmarkError, "unavailable"):
            self.store.verify(self.checkpoint, self.case, receipt.sha256)

    def test_existing_public_or_symlinked_run_directory_is_not_changed(self) -> None:
        self.root.mkdir()
        run_dir = self.root / self.checkpoint.run_id
        run_dir.mkdir(mode=0o755)
        run_dir.chmod(0o755)
        with self.assertRaisesRegex(BenchmarkError, "private"):
            self.store.export(self.checkpoint, self.case, self.evidence)
        self.assertEqual(0o755, stat.S_IMODE(run_dir.stat().st_mode))
        run_dir.rmdir()
        target = self.root / "unrelated"
        target.mkdir(mode=0o700)
        run_dir.symlink_to(target, target_is_directory=True)
        with self.assertRaises(BenchmarkError):
            self.store.export(self.checkpoint, self.case, self.evidence)
        self.assertEqual([], list(target.iterdir()))

    def test_symlink_file_and_fifo_are_never_followed_or_blocked_on(self) -> None:
        receipt = self.store.export(self.checkpoint, self.case, self.evidence)
        path = self.root / receipt.relative_path
        destination = path.parent / "unrelated"
        path.rename(destination)
        path.symlink_to(destination)
        with self.assertRaises(BenchmarkError):
            self.store.verify(self.checkpoint, self.case, receipt.sha256)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(BenchmarkError, "regular file"):
            self.store.verify(self.checkpoint, self.case, receipt.sha256)

    def test_full_disk_fsync_failure_and_limit_retain_failure_not_receipt(self) -> None:
        with patch("oke_hpc_mgmt.benchmarks.artifacts.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(BenchmarkError, "export failed"):
                self.store.export(self.checkpoint, self.case, self.evidence)
        self.assertEqual([], list((self.root / self.checkpoint.run_id).iterdir()))
        with patch("oke_hpc_mgmt.benchmarks.artifacts.MAX_ARTIFACT_BYTES", 10):
            with self.assertRaisesRegex(BenchmarkError, "size limit"):
                self.store.export(self.checkpoint, self.case, self.evidence)

    def test_temp_name_collision_does_not_delete_an_existing_file(self) -> None:
        self.store.export(self.checkpoint, self.case, self.evidence)
        existing = self.root / self.checkpoint.run_id / ".export-fixed"
        existing.write_text("do not remove")
        with patch("oke_hpc_mgmt.benchmarks.artifacts.secrets.token_hex", return_value="fixed"):
            with self.assertRaisesRegex(BenchmarkError, "export failed"):
                self.store.export(self.checkpoint, self.case, self.evidence)
        self.assertEqual("do not remove", existing.read_text())

    def test_same_hash_path_with_different_content_is_not_overwritten(self) -> None:
        with patch("oke_hpc_mgmt.benchmarks.artifacts.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-01-01T00:00:00+00:00"
            receipt = self.store.export(self.checkpoint, self.case, self.evidence)
            path = self.root / receipt.relative_path
            path.write_text("damaged evidence")
            with self.assertRaisesRegex(BenchmarkError, "does not match"):
                self.store.export(self.checkpoint, self.case, self.evidence)
        self.assertEqual("damaged evidence", path.read_text())

    def test_receipt_does_not_verify_against_another_run_or_case(self) -> None:
        receipt = self.store.export(self.checkpoint, self.case, self.evidence)
        with self.assertRaisesRegex(BenchmarkError, "identity"):
            self.store.verify(replace(self.checkpoint, uid="other"), self.case, receipt.sha256)
        with self.assertRaisesRegex(BenchmarkError, "identities"):
            self.store.verify(self.checkpoint, replace(self.case, case_id="case-9999"), receipt.sha256)
        for digest in ("../secrets", "", "z" * 64):
            with self.subTest(digest=digest), self.assertRaises(BenchmarkError):
                self.store.verify(self.checkpoint, self.case, digest)

    def test_invalid_json_even_with_recomputed_checksum_is_rejected(self) -> None:
        receipt = self.store.export(self.checkpoint, self.case, self.evidence)
        original = self.root / receipt.relative_path
        for encoded in (b"malformed", json.dumps({"schema_version": 1}).encode()):
            digest = hashlib.sha256(encoded).hexdigest()
            path = original.parent / f"{self.case.case_id}-{digest}.json"
            path.write_bytes(encoded)
            path.chmod(0o600)
            with self.subTest(encoded=encoded), self.assertRaises(BenchmarkError):
                self.store.verify(self.checkpoint, self.case, digest)

    def test_oversized_file_is_not_loaded(self) -> None:
        receipt = self.store.export(self.checkpoint, self.case, self.evidence)
        with patch("oke_hpc_mgmt.benchmarks.artifacts.MAX_ARTIFACT_BYTES", 10):
            with self.assertRaisesRegex(BenchmarkError, "bounded"):
                self.store.verify(self.checkpoint, self.case, receipt.sha256)
