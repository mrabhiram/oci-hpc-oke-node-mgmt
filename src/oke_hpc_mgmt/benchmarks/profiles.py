from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

from oke_hpc_mgmt.benchmarks.models import BenchmarkError, HealthcheckRequest, REPORT_BYTES, SOURCE_REVISION


DEFAULT_IMAGE = (
    "iad.ocir.io/idxzjcdglx2s/nccl-tests:"
    "cuda-13.4.1-ubuntu-24.04-nccl-2.32.3-100526.0"
)


@dataclass(frozen=True)
class NcclProfile:
    shape: str
    gpu_count: int
    host_hcas: str | None
    max_bytes: int
    family: Literal["ampere", "hopper-blackwell", "rtxpro", "local"]
    source_revision: str = SOURCE_REVISION
    image: str = DEFAULT_IMAGE

    @property
    def vf_count(self) -> int:
        if self.host_hcas is None or self.family == "rtxpro":
            return 0
        return len(self.host_hcas.removeprefix("=").split(","))

    @property
    def networks(self) -> tuple[str, ...]:
        if self.host_hcas is None:
            return ("local",)
        return ("local", "host", "vf") if self.vf_count else ("local", "host")


# Device names are shape facts from the pinned upstream MPIJob profiles, not
# interchangeable GPU-generation defaults. GB-family DRA profiles are separate.
PROFILES = (
    NcclProfile("BM.GPU4.8", 8,
                "=mlx5_0,mlx5_2,mlx5_6,mlx5_8,mlx5_10,mlx5_12,mlx5_14,mlx5_16,"
                "mlx5_1,mlx5_3,mlx5_7,mlx5_9,mlx5_11,mlx5_13,mlx5_15,mlx5_17",
                REPORT_BYTES, "ampere"),
    NcclProfile("BM.GPU.A100-v2.8", 8,
                "=mlx5_1,mlx5_2,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_7,mlx5_8,"
                "mlx5_14,mlx5_15,mlx5_16,mlx5_17,mlx5_9,mlx5_10,mlx5_11,mlx5_12",
                2 * REPORT_BYTES, "ampere"),
    NcclProfile("BM.GPU.B4.8", 8,
                "=mlx5_1,mlx5_2,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_7,mlx5_8,"
                "mlx5_14,mlx5_15,mlx5_16,mlx5_17,mlx5_9,mlx5_10,mlx5_11,mlx5_12",
                2 * REPORT_BYTES, "ampere"),
    NcclProfile("BM.GPU.H100.8", 8,
                "=mlx5_0,mlx5_1,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_7,mlx5_8,"
                "mlx5_9,mlx5_10,mlx5_12,mlx5_13,mlx5_14,mlx5_15,mlx5_16,mlx5_17",
                2 * REPORT_BYTES, "hopper-blackwell"),
    NcclProfile("BM.GPU.H200.8", 8,
                "=mlx5_0,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_9,mlx5_10,mlx5_11",
                2 * REPORT_BYTES, "hopper-blackwell"),
    NcclProfile("BM.GPU.B200.8", 8,
                "=mlx5_0,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_9,mlx5_10,mlx5_11",
                2 * REPORT_BYTES, "hopper-blackwell"),
    NcclProfile("BM.GPU.B300.8", 8,
                "=mlx5_0,mlx5_1,mlx5_7,mlx5_8,mlx5_9,mlx5_10,mlx5_11,mlx5_12,"
                "mlx5_13,mlx5_14,mlx5_16,mlx5_17,mlx5_18,mlx5_19,mlx5_20,mlx5_21",
                2 * REPORT_BYTES, "hopper-blackwell"),
    # The pinned RTXPRO source has host-network examples, not a VF profile.
    NcclProfile("BM.GPU.RTXPRO.8", 8,
                "=mlx5_0,mlx5_1,mlx5_2,mlx5_3,mlx5_6,mlx5_7,mlx5_8,mlx5_9",
                2 * REPORT_BYTES, "rtxpro"),
    # A10 profiles are local-only additions; no upstream RDMA profile is implied.
    NcclProfile("VM.GPU.A10.1", 1, None, REPORT_BYTES, "local"),
    NcclProfile("VM.GPU.A10.2", 2, None, REPORT_BYTES, "local"),
)


def get_profile(shape: str) -> NcclProfile:
    for profile in PROFILES:
        if profile.shape == shape:
            return profile
    raise BenchmarkError(f"No reviewed execution profile for shape {shape}; refusing substitution.")


def execution_fingerprint(shape: str, network: str, request: HealthcheckRequest) -> str:
    """Version the effective test contract, independently of participant names."""
    profile = get_profile(shape)
    value = {"version": 1, "profile": asdict(profile), "network": network,
             "image": request.image or profile.image, "parameters": asdict(request.parameters),
             "environment": profile_environment(profile, network, request.environment), "checks": request.checks}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_exclusive_gpu_labels(labels: dict[str, str], gpu_count: int) -> None:
    if labels.get("nvidia.com/gpu.replicas", "1") != "1" or labels.get("nvidia.com/gpu.sharing-strategy", "none") != "none":
        raise BenchmarkError("Exclusive physical GPUs are required; GPU sharing is configured.")
    strategy = labels.get("nvidia.com/mig.strategy", "none")
    product = labels.get("nvidia.com/gpu.product", "")
    if strategy not in {"none", "single", "mixed"} or "MIG" in product.upper() or "SHARED" in product.upper():
        raise BenchmarkError("GPU sharing or active MIG devices require a dedicated execution profile.")
    if labels.get("nvidia.com/mig.config.state", "success") != "success":
        raise BenchmarkError("MIG configuration is not stable; runtime availability is unverified.")
    # mig.strategy describes how the device plugin advertises devices, not
    # whether MIG is enabled. GFD can report 'single' for full physical GPUs.
    # A configured strategy needs positive full-GPU labels; runtime still checks
    # current MIG mode and physical UUID coverage before opening worker SSH.
    if strategy != "none" and (
        not product or labels.get("nvidia.com/gpu.count") != str(gpu_count)
        or labels.get("nvidia.com/gpu.mode") != "compute"
    ):
        raise BenchmarkError("MIG strategy is configured but full physical GPU discovery is unverified.")


def profile_environment(
    profile: NcclProfile,
    network: str,
    overrides: tuple[str, ...] = (),
) -> dict[str, str]:
    if network not in {"local", "host", "vf"}:
        raise BenchmarkError(f"Unsupported NCCL network profile: {network}.")
    if network not in profile.networks:
        raise BenchmarkError(f"{profile.shape} has no reviewed {network} execution profile.")
    environment = {"NCCL_DEBUG": "INFO", "NCCL_DEBUG_SUBSYS": "INIT,NET,GRAPH"}
    if network == "local":
        environment["NCCL_IB_DISABLE"] = "1"
    else:
        if profile.host_hcas is None:
            raise BenchmarkError(f"{profile.shape} has no RDMA execution profile.")
        environment.update({
            "NCCL_IB_SPLIT_DATA_ON_QPS": "0",
            "NCCL_IB_GID_INDEX": "3",
            "NCCL_IB_HCA": profile.host_hcas if network == "host" else "mlx5",
            "NCCL_IB_TC": "41", "NCCL_IB_SL": "0", "NCCL_IB_TIMEOUT": "22",
            "HCOLL_ENABLE_MCAST_ALL": "0", "UCX_TLS": "tcp", "UCX_NET_DEVICES": "eth0",
        })
        if profile.family == "ampere":
            environment["NCCL_IB_QPS_PER_CONNECTION"] = "4"
        else:
            environment.update({
                "NCCL_CUMEM_ENABLE": "0", "RX_QUEUE_LEN": "8192", "IB_RX_QUEUE_LEN": "8192",
                "NCCL_SOCKET_IFNAME": "eth0", "NCCL_IGNORE_CPU_AFFINITY": "1",
            })
        if profile.family == "rtxpro":
            environment.update({
                "NCCL_ALGO": "Tree", "NCCL_MIN_NCHANNELS": "8",
                "NCCL_IB_QPS_PER_CONNECTION": "1", "NCCL_NET_PLUGIN": "none",
            })
    seen: set[str] = set()
    for item in overrides:
        key, separator, value = item.partition("=")
        if not separator or not value or not re.fullmatch(r"(?:NCCL|UCX|HCOLL)_[A-Z0-9_]+", key):
            raise BenchmarkError("Environment overrides must be nonempty NCCL_/UCX_/HCOLL_ KEY=VALUE.")
        if key in seen or any(character in value for character in "\x00\n\r"):
            raise BenchmarkError(f"Duplicate or invalid environment override: {key}.")
        if key in {"NCCL_DEBUG", "NCCL_DEBUG_SUBSYS", "NCCL_DEBUG_FILE"}:
            raise BenchmarkError(f"{key} is reserved for evidence collection.")
        if key.startswith("NCCL_IB_") and network == "local":
            raise BenchmarkError("RDMA overrides cannot be used with the local profile.")
        if key == "NCCL_IB_DISABLE" and value != "0" and network != "local":
            raise BenchmarkError("RDMA tests cannot disable InfiniBand.")
        if key == "NCCL_NET" and value.lower() != "ib" and network != "local":
            raise BenchmarkError("RDMA tests cannot force a non-IB transport.")
        if key == "NCCL_IB_HCA" and network == "vf" and value != "mlx5":
            raise BenchmarkError("VF device names are dynamic; NCCL_IB_HCA must remain mlx5.")
        seen.add(key)
        environment[key] = value
    return environment
