from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oke_hpc_mgmt.benchmarks.evidence import CaseEvidence
from oke_hpc_mgmt.benchmarks.manifests import case_name, validate_run_id
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError
from oke_hpc_mgmt.benchmarks.records import RunRecord


MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExportReceipt:
    run_id: str
    case_id: str
    job_uid: str
    sha256: str
    relative_path: str


class EvidenceStore:
    """Immutable private snapshots; never overwrites an existing artifact."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.expanduser().absolute()

    def _directory(self, run_id: str, *, create: bool) -> int:
        validate_run_id(run_id)
        if create:
            self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = os.open(self.output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if create:
                try:
                    os.mkdir(run_id, mode=0o700, dir_fd=root)
                except FileExistsError:
                    pass
            descriptor = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            details = os.fstat(descriptor)
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
                os.close(descriptor)
                raise BenchmarkError("Evidence run directory must be owned by this user and private (mode 0700).")
            return descriptor
        finally:
            os.close(root)

    @staticmethod
    def _read(directory: int, name: str) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or (
                stat.S_IMODE(details.st_mode) & 0o077
            ) or details.st_size > MAX_ARTIFACT_BYTES:
                raise BenchmarkError("Evidence file is not a bounded, private, user-owned regular file.")
            value = stream.read(MAX_ARTIFACT_BYTES + 1)
            if len(value) > MAX_ARTIFACT_BYTES:
                raise BenchmarkError("Evidence file grew beyond the artifact size limit.")
            return value

    def export(self, run: RunRecord, case: BenchmarkCase, evidence: CaseEvidence) -> ExportReceipt:
        case_name(run.run_id, case)
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not run.uid or case not in run.plan.cases or not saved or not saved.job_uid or (
            saved.job_uid != evidence.job_uid or case.case_id != evidence.case_id
        ):
            raise BenchmarkError("Export requires evidence for the recorded case and MPIJob UID.")
        payload = {
            "schema_version": SCHEMA_VERSION, "run_id": run.run_id, "run_uid": run.uid,
            "namespace": run.plan.request.namespace, "case_id": case.case_id, "job_uid": evidence.job_uid,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "request": asdict(run.plan.request), "options": asdict(run.options), "case": asdict(case),
            "evidence": asdict(evidence),
        }
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BenchmarkError("Evidence could not be serialized; no workload resources were removed.") from exc
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise BenchmarkError("Evidence exceeds the artifact size limit; workload resources must be retained.")
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{case.case_id}-{digest}.json"
        receipt = ExportReceipt(run.run_id, case.case_id, evidence.job_uid, digest, f"{run.run_id}/{name}")
        directory: int | None = None
        temporary: str | None = None
        try:
            directory = self._directory(run.run_id, create=True)
            candidate = f".export-{secrets.token_hex(12)}"
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            temporary = candidate
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # Linking publishes a complete file atomically without replacing an
            # existing path. Relative dir_fd operations keep symlinks out of it.
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if self._read(directory, name) != encoded:
                    raise BenchmarkError("Existing evidence path does not match its content digest.") from None
            os.unlink(temporary, dir_fd=directory)
            temporary = None
            os.fsync(directory)
            if self._read(directory, name) != encoded:
                raise BenchmarkError("Evidence read-back differs from exported data; retain workload resources.")
            return receipt
        except OSError as exc:
            raise BenchmarkError("Evidence export failed; retain workload resources and verify output-directory storage.") from exc
        finally:
            if directory is not None:
                try:
                    if temporary is not None:
                        os.unlink(temporary, dir_fd=directory)
                finally:
                    os.close(directory)

    def verify(self, run: RunRecord, case: BenchmarkCase, digest: str) -> dict[str, Any]:
        case_name(run.run_id, case)
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BenchmarkError("Invalid exported evidence digest.")
        saved = next((item for item in run.cases if item.case_id == case.case_id), None)
        if not run.uid or case not in run.plan.cases or not saved or not saved.job_uid:
            raise BenchmarkError("Evidence verification requires recorded run, case, and MPIJob identities.")
        try:
            directory = self._directory(run.run_id, create=False)
            try:
                encoded = self._read(directory, f"{case.case_id}-{digest}.json")
            finally:
                os.close(directory)
            if hashlib.sha256(encoded).hexdigest() != digest:
                raise BenchmarkError("Exported evidence checksum differs; do not delete in-cluster evidence.")
            payload = json.loads(encoded)
        except (OSError, ValueError) as exc:
            if isinstance(exc, BenchmarkError):
                raise
            raise BenchmarkError("Exported evidence is unavailable or unreadable; retain workload resources.") from exc
        expected: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "run_id": run.run_id, "run_uid": run.uid,
                    "namespace": run.plan.request.namespace, "case_id": case.case_id, "job_uid": saved.job_uid}
        expected.update({"case": json.loads(json.dumps(asdict(case))),
                         "request": json.loads(json.dumps(asdict(run.plan.request))),
                         "options": json.loads(json.dumps(asdict(run.options)))})
        # Older immutable exports predate optional freshness fields. Only absent
        # null defaults may be omitted; a recorded identity must still match.
        if isinstance(payload, dict) and isinstance(payload.get("case"), dict):
            old_case = payload["case"]
            if expected["case"]["profile_fingerprint"] is None and "profile_fingerprint" not in old_case:
                expected["case"].pop("profile_fingerprint")
            old_nodes = old_case.get("nodes")
            if isinstance(old_nodes, list) and len(old_nodes) == len(expected["case"]["nodes"]):
                for old_node, node in zip(old_nodes, expected["case"]["nodes"]):
                    if isinstance(old_node, dict) and node["boot_id"] is None and "boot_id" not in old_node:
                        node.pop("boot_id")
        if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
            raise BenchmarkError("Exported evidence identity does not match this run and case.")
        return payload
