from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError, HealthcheckRequest
from oke_hpc_mgmt.benchmarks.profiles import execution_fingerprint, get_profile, profile_environment


MANAGED_BY = "mgmt-oke-healthchecks"
RUN_LABEL = "mgmt-oke.oraclecloud.com/healthcheck-run"
CASE_LABEL = "mgmt-oke.oraclecloud.com/healthcheck-case"
ROLE_LABEL = "mgmt-oke.oraclecloud.com/healthcheck-role"
MPI_GROUP = "kubeflow.org"
MPI_VERSION = "v2beta1"
MPI_PLURAL = "mpijobs"
MAX_RANK_DIAGNOSTIC_BYTES = 1024 * 1024
RANK_SCRIPT = "\n".join([
    "set -euo pipefail",
    "source /tmp/mgmt-worker-identity",
    "printf 'MGMT_NCCL_RANK\\t%s\\t%s\\t%s\\t%s\\n' "
    '"${OMPI_COMM_WORLD_RANK:?}" "${OMPI_COMM_WORLD_LOCAL_RANK:?}" '
    '"$MGMT_NODE_NAME" "$MGMT_POD_UID"',
    # NCCL can log during a partially printed nccl-tests table row. Retain
    # diagnostics separately, then emit them only after that rank has exited.
    'NCCL_DEBUG_FILE=$(mktemp /tmp/mgmt-nccl-debug.XXXXXXXX)',
    "export NCCL_DEBUG_FILE",
    "exit_code=0",
    '"$@" || exit_code=$?',
    "printf '\\nMGMT_NCCL_DEBUG_BEGIN\\t%s\\n' \"$OMPI_COMM_WORLD_RANK\"",
    f'if test -s "$NCCL_DEBUG_FILE" && test "$(wc -c < "$NCCL_DEBUG_FILE")" -le {MAX_RANK_DIAGNOSTIC_BYTES}; then',
    '  cat "$NCCL_DEBUG_FILE" || exit_code=1',
    "else",
    '  echo "NCCL diagnostic log is missing, empty, or exceeds the per-rank evidence limit" >&2',
    '  if test "$exit_code" -eq 0; then exit_code=1; fi',
    "fi",
    "printf '\\nMGMT_NCCL_DEBUG_END\\t%s\\n' \"$OMPI_COMM_WORLD_RANK\"",
    'exit "$exit_code"',
])


@dataclass(frozen=True)
class TopologyPlacement:
    case_id: str
    key: str
    required: bool
    hostnames: tuple[str, ...]

    def validate(self) -> None:
        if not re.fullmatch(r"case-[0-9]{4}", self.case_id) or self.key not in {
            "oci.oraclecloud.com/rdma.local_block_id", "kubernetes.io/hostname",
        } or type(self.required) is not bool:
            raise BenchmarkError("Invalid topology placement policy.")
        if not self.hostnames or len(set(self.hostnames)) != len(self.hostnames) or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9](?:[-a-zA-Z0-9_.]{0,61}[a-zA-Z0-9])?", value)
            for value in self.hostnames
        ):
            raise BenchmarkError("Topology placement requires unique verified hostname labels.")


@dataclass(frozen=True)
class ExecutionOptions:
    concurrency: int = 1
    queue_timeout: int = 900
    startup_timeout: int = 600
    timeout: int = 1800
    network_attachment: str = "rdma-vf"
    placements: tuple[TopologyPlacement, ...] = ()

    def validate(self) -> None:
        for name, value, maximum in (
            ("concurrency", self.concurrency, 32),
            ("queue-timeout", self.queue_timeout, 86400),
            ("startup-timeout", self.startup_timeout, 86400),
            ("timeout", self.timeout, 86400),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise BenchmarkError(f"{name} must be an integer in 1..{maximum}.")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.network_attachment):
            raise BenchmarkError("network-attachment must be a name in the benchmark namespace.")
        for placement in self.placements:
            placement.validate()
        if len({item.case_id for item in self.placements}) != len(self.placements):
            raise BenchmarkError("Topology placement has duplicate case identities.")


def validate_run_id(run_id: str) -> None:
    if not re.fullmatch(r"hc-[a-f0-9]{20}", run_id):
        raise BenchmarkError("Invalid healthcheck run ID; expected hc- followed by 20 hex digits.")


def case_name(run_id: str, case: BenchmarkCase) -> str:
    validate_run_id(run_id)
    if not re.fullmatch(r"case-[0-9]{4}", case.case_id):
        raise BenchmarkError("Invalid benchmark case ID.")
    return f"{run_id}-{case.case_id}"


def owned_labels(run_id: str) -> dict[str, str]:
    validate_run_id(run_id)
    return {"app.kubernetes.io/managed-by": MANAGED_BY, RUN_LABEL: run_id}


def nccl_argv(request: HealthcheckRequest) -> list[str]:
    request.parameters.validate()
    parameters = request.parameters
    return [
        "/workspace/nccl-tests/build/all_reduce_perf",
        "-b", str(parameters.min_bytes), "-e", str(parameters.max_bytes),
        "-f", str(parameters.factor), "-g", "1", "-c", "1",
        "-n", str(parameters.iterations), "-w", str(parameters.warmup_iterations),
    ]


def gpu_preflight_script(gpu_count: int) -> str:
    if type(gpu_count) is not int or gpu_count not in {1, 2, 8}:
        raise BenchmarkError("GPU startup validation requires a reviewed physical GPU count.")
    return "\n".join([
        "gpu_uuids=$(nvidia-smi --query-gpu=uuid --format=csv,noheader,nounits)",
        f'test "$(printf \'%s\\n\' "$gpu_uuids" | wc -l)" -eq {gpu_count}',
        f'test "$(printf \'%s\\n\' "$gpu_uuids" | sort -u | wc -l)" -eq {gpu_count}',
        'while IFS= read -r uuid; do',
        '  [[ "$uuid" =~ ^GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || exit 1',
        'done <<< "$gpu_uuids"',
        "mig_modes=$(nvidia-smi --query-gpu=mig.mode.current --format=csv,noheader,nounits)",
        f'test "$(printf \'%s\\n\' "$mig_modes" | wc -l)" -eq {gpu_count}',
        'while IFS= read -r mode; do',
        '  case "$mode" in Disabled|"[N/A]") ;; *) echo "MIG is enabled or unverifiable" >&2; exit 1 ;; esac',
        'done <<< "$mig_modes"',
        'printf "MGMT_GPU_MODE_VERIFIED\\t%s\\n" "' + str(gpu_count) + '"',
    ])


def worker_script(port: int, rdma: bool, gpu_count: int = 8) -> str:
    if port not in {22, 2222}:
        raise BenchmarkError("Unsupported worker SSH port.")
    # Only run-owned container files are written. Node identity comes from the
    # downward API, not a hostname inferred from MPI or a user-controlled label.
    return "\n".join([
        "set -euo pipefail",
        "umask 077",
        "printf 'export MGMT_NODE_NAME=%q\\nexport MGMT_POD_UID=%q\\n' "
        '"$MGMT_NODE_NAME" "$MGMT_POD_UID" > /tmp/mgmt-worker-identity',
        "printf '%s\\n' " + shlex.quote(RANK_SCRIPT) + " > /tmp/mgmt-nccl-rank",
        "echo MGMT_GPU_INVENTORY_BEGIN",
        "nvidia-smi --query-gpu=index,uuid,pci.bus_id,name,driver_version --format=csv,noheader",
        "echo MGMT_GPU_INVENTORY_END",
        gpu_preflight_script(gpu_count),
        "test -x /workspace/nccl-tests/build/all_reduce_perf",
        "command -v mpirun >/dev/null",
        "ibv_devinfo" if rdma else ":",
        "mkdir -p /var/run/sshd",
        "ssh-keygen -A",
        f"exec /usr/sbin/sshd -D -e -p {port}",
    ])


def launcher_script(
    case: BenchmarkCase, request: HealthcheckRequest, options: ExecutionOptions,
) -> str:
    options.validate()
    profile = get_profile(case.nodes[0].shape)
    environment = profile_environment(profile, case.network, request.environment)
    port = 2222 if case.network == "host" else 22
    # SSH is confined to this ephemeral MPIJob. Keep an explicit per-run host-key
    # file so an upstream image's global StrictHostKeyChecking=no is not inherited.
    ssh_options = f"-F /tmp/mgmt-ssh-config -p {port} -o BatchMode=yes -o ConnectTimeout=5 " + (
        "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/mgmt-known-hosts"
    )
    command = [
        "mpirun", "--allow-run-as-root", "--hostfile", "/etc/mpi/hostfile",
        "-np", str(case.ranks), "-npernode", str(profile.gpu_count),
        "--bind-to", "numa", "-mca", "coll", "^hcoll",
        "-mca", "coll_hcoll_enable", "0", "-mca", "plm_rsh_args", ssh_options,
    ]
    if profile.family == "rtxpro" and case.network == "host":
        command.extend(["-mca", "pml", "ucx"])
    for key, value in sorted(environment.items()):
        command.extend(["-x", f"{key}={value}"])
    command.extend(["/bin/bash", "/tmp/mgmt-nccl-rank", *nccl_argv(request)])
    return "\n".join([
        "set -euo pipefail",
        "umask 077",
        "mpirun --version",
        f"expected_hosts={len(case.nodes)}",
        "hosts=()",
        "while IFS= read -r host; do hosts+=(\"$host\"); done < <(awk 'NF && $1 !~ /^#/ {print $1}' /etc/mpi/hostfile)",
        'test "${#hosts[@]}" -eq "$expected_hosts"',
        'test "$(printf \'%s\\n\' "${hosts[@]}" | sort -u | wc -l)" -eq "$expected_hosts"',
        ": > /tmp/mgmt-known-hosts",
        ": > /tmp/mgmt-ssh-config",
        "addresses=''",
        f"deadline=$((SECONDS + {options.startup_timeout}))",
        'for host in "${hosts[@]}"; do',
        '  [[ "$host" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || exit 1',
        # Independent CoreDNS negative caches can outlive the first successful
        # lookup. Freeze each run-owned worker address before SSH/MPI starts.
        "  until address=$(timeout 5s getent ahosts \"$host\" | awk '$2 == \"STREAM\" && !address {address=$1} END {print address}') "
        '&& [[ "$address" =~ ^[0-9a-fA-F:.]+$ ]]; do',
        '    if (( SECONDS >= deadline )); then echo "Worker DNS startup timed out" >&2; exit 1; fi',
        "    sleep 2",
        "  done",
        '  if [[ " $addresses " == *" $address "* ]]; then echo "Workers resolved to a duplicate address" >&2; exit 1; fi',
        '  addresses+=" $address"',
        '  key_alias="$address"' if port == 22 else f'  key_alias="[$address]:{port}"',
        "  printf 'Host %s\\n  HostName %s\\n  HostKeyAlias %s\\n' \"$host\" \"$address\" \"$key_alias\" >> /tmp/mgmt-ssh-config",
        "  printf 'MGMT_MPI_WORKER_ADDRESS\\t%s\\t%s\\n' \"$host\" \"$address\"",
        f'  until ssh-keyscan -T 5 -p {port} "$address" > /tmp/mgmt-host-key 2>/dev/null '
        "&& test -s /tmp/mgmt-host-key; do",
        '    if (( SECONDS >= deadline )); then echo "Worker SSH startup timed out" >&2; exit 1; fi',
        "    sleep 2",
        "  done",
        "  cat /tmp/mgmt-host-key >> /tmp/mgmt-known-hosts",
        f'  until ssh {ssh_options} "$host" test -s /tmp/mgmt-worker-identity; do',
        '    if (( SECONDS >= deadline )); then echo "Worker SSH readiness timed out" >&2; exit 1; fi',
        "    sleep 2",
        "  done",
        "  printf 'MGMT_MPI_SSH_READY\\t%s\\n' \"$host\"",
        "done",
        # MPI Operator injects ORTE settings, which PRRTE does not inherit.
        # Keep the hostfile names pinned by SSH and launch from this container:
        # worker-to-worker tree spawning would not have its known-hosts file.
        "export OMPI_MCA_orte_keep_fqdn_hostnames=1",
        "export PRTE_MCA_prte_keep_fqdn_hostnames=1",
        "export OMPI_MCA_plm_rsh_no_tree_spawn=1",
        "export PRTE_MCA_plm_ssh_no_tree_spawn=1",
        "export PRTE_MCA_plm_ssh_args=" + shlex.quote(ssh_options),
        "exec timeout --signal=TERM --kill-after=30s " + str(options.timeout) + "s " + shlex.join(command),
    ])


def _tolerations(case: BenchmarkCase) -> list[dict[str, str]]:
    tolerations: list[dict[str, str]] = []
    for taint in sorted({taint for node in case.nodes for taint in node.tolerations}):
        key_value, separator, effect = taint.rpartition(":")
        key, equal, value = key_value.partition("=")
        if not separator or key != "nvidia.com/gpu" or effect not in {"NoSchedule", "PreferNoSchedule"}:
            raise BenchmarkError("Only known GPU scheduling taints may be tolerated by benchmarks.")
        tolerations.append({"key": key, "operator": "Equal", "value": value if equal else "", "effect": effect})
    return tolerations


def render_mpijob(
    run_id: str,
    case: BenchmarkCase,
    request: HealthcheckRequest,
    options: ExecutionOptions,
    *,
    owner_uid: str | None = None,
) -> dict[str, Any]:
    request.validate()
    options.validate()
    name = case_name(run_id, case)
    if not case.nodes or len({node.name for node in case.nodes}) != len(case.nodes):
        raise BenchmarkError("A benchmark case requires unique participants.")
    if len({(node.pool, node.shape, node.gpu_count) for node in case.nodes}) != 1:
        raise BenchmarkError("MPIJob participants must share a pool and reviewed hardware profile.")
    profile = get_profile(case.nodes[0].shape)
    if case.profile_fingerprint and case.profile_fingerprint != execution_fingerprint(profile.shape, case.network, request):
        raise BenchmarkError("Recorded execution profile differs from this build; do not change an existing test contract.")
    if any(not node.uid or node.gpu_count != profile.gpu_count for node in case.nodes):
        raise BenchmarkError("Every participant needs a node UID and the profile's exact GPU count.")
    if request.parameters.max_bytes > profile.max_bytes:
        raise BenchmarkError("Requested message size exceeds the shape profile.")
    if (case.network == "local") != (case.check_type == "active") or (
        len(case.nodes) != 1 if case.check_type == "active" else len(case.nodes) < 2
    ):
        raise BenchmarkError("Case category, participant count, and network profile disagree.")
    if request.scheduler == "kueue" and not request.queue:
        raise BenchmarkError("Kueue execution requires an existing --queue.")
    labels = {**owned_labels(run_id), CASE_LABEL: case.case_id}
    image = request.image or profile.image
    port = 2222 if case.network == "host" else 22
    environment = profile_environment(profile, case.network, request.environment)
    # A required field affinity selects node object names without changing node
    # labels or bypassing the scheduler. Anti-affinity prevents duplicate workers.
    affinity: dict[str, Any] = {
        "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchFields": [{"key": "metadata.name", "operator": "In",
                               "values": [node.name]}]} for node in case.nodes
        ]}},
        "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
            "labelSelector": {"matchLabels": {**labels, ROLE_LABEL: "worker"}},
            "topologyKey": "kubernetes.io/hostname",
        }]},
    }
    placement = next((item for item in options.placements if item.case_id == case.case_id), None)
    if placement:
        if not request.queue or len(placement.hostnames) != len(case.nodes):
            raise BenchmarkError("Topology placement requires a queue and every selected node's hostname label.")
        terms = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        for term, hostname in zip(terms, placement.hostnames):
            term["matchExpressions"] = [{"key": "kubernetes.io/hostname", "operator": "In", "values": [hostname]}]
    worker_container: dict[str, Any] = {
        "name": "mpi-worker", "image": image, "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/bash", "-c", worker_script(port, case.network != "local", profile.gpu_count)],
        "env": [{"name": key, "value": value} for key, value in sorted(environment.items())] + [
            {"name": "MGMT_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
            {"name": "MGMT_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        ],
        "ports": [{"name": "mpijob-port", "containerPort": port, "protocol": "TCP"}],
        "resources": {
            "requests": {"cpu": "1", "memory": "8Gi", "nvidia.com/gpu": profile.gpu_count},
            "limits": {"memory": "64Gi", "nvidia.com/gpu": profile.gpu_count},
        },
        "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}],
        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
    }
    volumes: list[dict[str, Any]] = [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"}}]
    if case.network != "local":
        worker_container["securityContext"]["privileged"] = True
        worker_container["volumeMounts"].append({"name": "infiniband", "mountPath": "/dev/infiniband"})
        volumes.append({"name": "infiniband", "hostPath": {"path": "/dev/infiniband", "type": "Directory"}})
    worker: dict[str, Any] = {
        "metadata": {"labels": {**labels, ROLE_LABEL: "worker"}},
        "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "activeDeadlineSeconds": options.startup_timeout + options.timeout + 30,
            "affinity": affinity, "tolerations": _tolerations(case),
            "terminationGracePeriodSeconds": 30,
            "containers": [worker_container], "volumes": volumes,
        },
    }
    if placement:
        policy = "required" if placement.required else "preferred"
        worker["metadata"]["annotations"] = {f"kueue.x-k8s.io/podset-{policy}-topology": placement.key}
    if case.network == "vf":
        worker["metadata"].setdefault("annotations", {})["k8s.v1.cni.cncf.io/networks"] = json.dumps([
            {"name": options.network_attachment} for _ in range(profile.vf_count)
        ])
        for section in ("requests", "limits"):
            worker_container["resources"][section]["nvidia.com/rdma-vf"] = profile.vf_count
    launcher: dict[str, Any] = {
        "metadata": {"labels": {**labels, ROLE_LABEL: "launcher"}},
        "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            # Some existing queues offer only a GPU-node flavor for both PodSets.
            # Permit its known GPU taint without requesting a GPU for the launcher.
            "tolerations": _tolerations(case),
            "activeDeadlineSeconds": options.startup_timeout + options.timeout + 30,
            "terminationGracePeriodSeconds": 30,
            "containers": [{
                "name": "mpi-launcher", "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/bash", "-c", launcher_script(case, request, options)],
                "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                "resources": {"requests": {"cpu": "250m", "memory": "512Mi"},
                              "limits": {"memory": "4Gi"}},
            }],
        },
    }
    if case.network == "host":
        for template in (worker, launcher):
            template["spec"].update({"hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet"})
        worker_container["ports"][0]["hostPort"] = port
    metadata: dict[str, Any] = {
        "name": name, "namespace": request.namespace, "labels": dict(labels),
        "annotations": {"mgmt-oke.oraclecloud.com/profile-revision": profile.source_revision},
    }
    if owner_uid is not None:
        if not owner_uid or any(char.isspace() for char in owner_uid):
            raise BenchmarkError("Invalid run ConfigMap UID.")
        metadata["ownerReferences"] = [{
            "apiVersion": "v1", "kind": "ConfigMap", "name": run_id, "uid": owner_uid,
            "controller": False, "blockOwnerDeletion": False,
        }]
    if request.queue:
        metadata["labels"]["kueue.x-k8s.io/queue-name"] = request.queue
    return {
        "apiVersion": f"{MPI_GROUP}/{MPI_VERSION}", "kind": "MPIJob", "metadata": metadata,
        "spec": {
            "slotsPerWorker": profile.gpu_count, "runLauncherAsWorker": False,
            "launcherCreationPolicy": "WaitForWorkersReady", "sshAuthMountPath": "/root/.ssh",
            "runPolicy": {
                "cleanPodPolicy": "None", "backoffLimit": 0,
                "activeDeadlineSeconds": options.startup_timeout + options.timeout,
                "suspend": bool(request.queue),
            },
            "mpiReplicaSpecs": {
                "Launcher": {"replicas": 1, "restartPolicy": "Never", "template": launcher},
                "Worker": {"replicas": len(case.nodes), "restartPolicy": "Never", "template": worker},
            },
        },
    }
