from __future__ import annotations

import base64
import os
import re
import socket
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from oke_hpc_mgmt.models import AutoscalerEntry, DrainPod, KueueSummary, NodeInfo
from oke_hpc_mgmt.upgrades import (
    CHECKPOINT_NAME,
    CHECKPOINT_NAMESPACE,
    UpgradeCheckpoint,
)


INSTANCE_OCID_RE = re.compile(r"(ocid1\.instance[.\w-]+)")
AUTOSCALER_NODES_RE = re.compile(r"^--nodes=(\d+):(\d+):(.+)$")

KNOWN_NODE_POOL_LABELS = (
    "oke.oraclecloud.com/pool.name",
    "oke.oraclecloud.com/pool.id",
    "oci.oraclecloud.com/oke-nodepool-id",
    "oci.oraclecloud.com/oke-nodepool",
    "oke.oraclecloud.com/nodepool-id",
    "oke.oraclecloud.com/nodepool",
    "oci.oraclecloud.com/nodepool-id",
    "oci.oraclecloud.com/node-pool-id",
    "oci.oraclecloud.com/nodepool",
)


class KubernetesDiscoveryError(RuntimeError):
    """Raised when Kubernetes discovery cannot run."""


def parse_instance_ocid(provider_id: str | None) -> str | None:
    if not provider_id:
        return None
    match = INSTANCE_OCID_RE.search(provider_id)
    if match:
        return match.group(1)
    if provider_id.startswith("ocid1.instance"):
        return provider_id
    return None


def _node_pool_value_from_labels(labels: dict[str, str]) -> str | None:
    for key in KNOWN_NODE_POOL_LABELS:
        value = labels.get(key)
        if value:
            return value
    for key, value in labels.items():
        normalized = key.lower().replace("_", "-")
        if "nodepool" in normalized or "node-pool" in normalized:
            return value
    return None


def _node_pool_id_from_labels(labels: dict[str, str]) -> str | None:
    value = _node_pool_value_from_labels(labels)
    if value and value.startswith("ocid1.nodepool"):
        return value
    return None


def _node_pool_name_from_labels(labels: dict[str, str]) -> str | None:
    value = _node_pool_value_from_labels(labels)
    if value and not value.startswith("ocid1."):
        return value
    return None


def _is_ready(node: Any) -> bool:
    for condition in node.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def _internal_ip(node: Any) -> str | None:
    for address in node.status.addresses or []:
        if address.type == "InternalIP":
            return address.address
    return None


def _taints(node: Any) -> list[str]:
    return [
        f"{taint.key}={taint.value}:{taint.effect}" if taint.value else f"{taint.key}:{taint.effect}"
        for taint in (node.spec.taints or [])
    ]


def _shape_from_labels(labels: dict[str, str]) -> str | None:
    return labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type")


def _is_daemonset_pod(pod: Any) -> bool:
    for owner in pod.metadata.owner_references or []:
        if owner.kind == "DaemonSet":
            return True
    return False


def _is_mirror_pod(pod: Any) -> bool:
    return bool((pod.metadata.annotations or {}).get("kubernetes.io/config.mirror"))


def _is_running_or_pending(pod: Any) -> bool:
    return pod.status.phase in {"Running", "Pending", "Unknown"}


def _is_slinky_worker_pod(pod: Any) -> bool:
    labels = pod.metadata.labels or {}
    return bool(
        labels.get("slinky.slurm.net/nodeset")
        or (
            labels.get("app.kubernetes.io/name") == "slurmd"
            and labels.get("app.kubernetes.io/component") == "worker"
        )
    )


def _pod_counts_by_node(pods: list[Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    system_namespaces = {"kube-system", "kueue-system", "cert-manager", "monitoring"}
    for pod in pods:
        node_name = pod.spec.node_name
        if not node_name or not _is_running_or_pending(pod):
            continue
        if _is_slinky_worker_pod(pod):
            counts[node_name]["slinky"] += 1
        if _is_daemonset_pod(pod) or _is_mirror_pod(pod):
            counts[node_name]["daemonset"] += 1
        elif pod.metadata.namespace in system_namespaces:
            counts[node_name]["system"] += 1
        else:
            counts[node_name]["workload"] += 1
    return counts


def _empty_pod_count() -> dict[str, int]:
    return defaultdict(int)


def _pod_controller(pod: Any) -> str | None:
    owners = list(pod.metadata.owner_references or [])
    owner = next((item for item in owners if getattr(item, "controller", False)), None)
    if owner is None and owners:
        owner = owners[0]
    if owner is None:
        return None
    return f"{getattr(owner, 'kind', 'Unknown')}/{getattr(owner, 'name', 'unknown')}"


def _has_empty_dir(pod: Any) -> bool:
    return any(getattr(volume, "empty_dir", None) is not None for volume in (pod.spec.volumes or []))


def _api_error_text(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None) or str(exc)
    return f"HTTP {status}: {reason}" if status else str(reason)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class KubernetesBackend:
    def __init__(
        self,
        kubeconfig: str | None = None,
        context: str | None = None,
        in_cluster: bool = False,
    ) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        self.in_cluster = in_cluster
        self._loaded = False
        self._client = None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise KubernetesDiscoveryError(
                "The kubernetes Python package is not installed. Install the project dependencies first."
            ) from exc

        if self.in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config(config_file=self.kubeconfig, context=self.context)
        self._client = client
        self._loaded = True

    @property
    def client(self):
        self._ensure_loaded()
        return self._client

    def list_nodes(self, include_pod_counts: bool = True) -> list[NodeInfo]:
        client = self.client
        core = client.CoreV1Api()
        k8s_nodes = core.list_node().items
        pod_counts: dict[str, dict[str, int]] = defaultdict(_empty_pod_count)
        if include_pod_counts:
            try:
                pods = core.list_pod_for_all_namespaces().items
            except Exception:
                pods = []
            pod_counts = _pod_counts_by_node(pods)

        nodes: list[NodeInfo] = []
        for node in k8s_nodes:
            labels = dict(node.metadata.labels or {})
            annotations = dict(node.metadata.annotations or {})
            allocatable = dict(node.status.allocatable or {})
            counts = pod_counts[node.metadata.name]
            provider_id = node.spec.provider_id
            nodes.append(
                NodeInfo(
                    k8s_name=node.metadata.name,
                    internal_ip=_internal_ip(node),
                    provider_id=provider_id,
                    instance_ocid=parse_instance_ocid(provider_id),
                    pool_name=_node_pool_name_from_labels(labels),
                    node_pool_id=_node_pool_id_from_labels(labels),
                    shape=_shape_from_labels(labels),
                    ready=_is_ready(node),
                    schedulable=not bool(node.spec.unschedulable),
                    allocatable=allocatable,
                    labels=labels,
                    annotations=annotations,
                    taints=_taints(node),
                    running_workload_pods=counts["workload"],
                    daemonset_pods=counts["daemonset"],
                    system_pods=counts["system"],
                    slinky_workload_pods=counts["slinky"],
                    boot_id=getattr(
                        getattr(node.status, "node_info", None),
                        "boot_id",
                        None,
                    ),
                    kubelet_version=getattr(
                        getattr(node.status, "node_info", None),
                        "kubelet_version",
                        None,
                    ),
                )
            )
        return nodes

    def cluster_connection_data(self) -> tuple[str, str]:
        """Return the active API endpoint and base64-encoded cluster CA."""

        client = self.client
        configuration = client.Configuration.get_default_copy()
        host = str(getattr(configuration, "host", "") or "").strip()
        ca_path = str(
            getattr(configuration, "ssl_ca_cert", "") or ""
        ).strip()
        if not host:
            raise KubernetesDiscoveryError(
                "The active Kubernetes client configuration has no API endpoint."
            )
        if not ca_path:
            raise KubernetesDiscoveryError(
                "The active Kubernetes client configuration has no cluster CA file."
            )
        try:
            certificate = Path(ca_path).read_bytes()
        except OSError as exc:
            raise KubernetesDiscoveryError(
                f"Unable to read the active Kubernetes cluster CA: {exc}"
            ) from exc
        if not certificate:
            raise KubernetesDiscoveryError(
                "The active Kubernetes cluster CA file is empty."
            )
        endpoint = re.sub(r"^https?://", "", host).rstrip("/")
        return endpoint, base64.b64encode(certificate).decode("ascii")

    def list_upgrade_blocking_pods(self, node_names: set[str]) -> list[DrainPod]:
        """List active non-infrastructure pods without using eviction APIs."""

        client = self.client
        core = client.CoreV1Api()
        try:
            pods = core.list_pod_for_all_namespaces().items
        except Exception as exc:
            raise KubernetesDiscoveryError(
                f"Unable to verify worker pods: {_api_error_text(exc)}"
            ) from exc
        blockers: list[DrainPod] = []
        for pod in pods:
            if getattr(pod.spec, "node_name", None) not in node_names:
                continue
            if getattr(pod.status, "phase", None) in {"Succeeded", "Failed"}:
                continue
            if _is_daemonset_pod(pod) or _is_mirror_pod(pod):
                continue
            labels = dict(getattr(pod.metadata, "labels", None) or {})
            if _is_recognized_scheduler_infrastructure(labels):
                continue
            blockers.append(
                DrainPod(
                    namespace=pod.metadata.namespace,
                    name=pod.metadata.name,
                    phase=getattr(pod.status, "phase", None),
                    controller=_pod_controller(pod),
                    has_empty_dir=_has_empty_dir(pod),
                )
            )
        return sorted(blockers, key=lambda item: (item.namespace, item.name))

    def read_upgrade_checkpoint(
        self,
        name: str = CHECKPOINT_NAME,
        namespace: str = CHECKPOINT_NAMESPACE,
    ) -> tuple[UpgradeCheckpoint, str] | None:
        core = self.client.CoreV1Api()
        try:
            config_map = core.read_namespaced_config_map(name, namespace)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise KubernetesDiscoveryError(
                f"Unable to read upgrade checkpoint {namespace}/{name}: "
                f"{_api_error_text(exc)}"
            ) from exc
        payload = (getattr(config_map, "data", None) or {}).get("checkpoint.json")
        if not payload:
            raise KubernetesDiscoveryError(
                f"Upgrade checkpoint {namespace}/{name} has no checkpoint.json data."
            )
        resource_version = getattr(config_map.metadata, "resource_version", None)
        if not resource_version:
            raise KubernetesDiscoveryError(
                f"Upgrade checkpoint {namespace}/{name} has no resourceVersion."
            )
        return UpgradeCheckpoint.from_json(payload), str(resource_version)

    def write_upgrade_checkpoint(
        self,
        checkpoint: UpgradeCheckpoint,
        resource_version: str | None = None,
        name: str = CHECKPOINT_NAME,
        namespace: str = CHECKPOINT_NAMESPACE,
    ) -> str:
        client = self.client
        core = client.CoreV1Api()
        metadata = client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "mgmt-oke",
                "mgmt-oke.oracle.com/purpose": "kubernetes-upgrade",
            },
            resource_version=resource_version,
        )
        body = client.V1ConfigMap(
            metadata=metadata,
            immutable=False,
            data={"checkpoint.json": checkpoint.to_json()},
        )
        try:
            if resource_version:
                response = core.replace_namespaced_config_map(name, namespace, body)
            else:
                response = core.create_namespaced_config_map(namespace, body)
        except Exception as exc:
            conflict = " checkpoint changed concurrently" if getattr(exc, "status", None) == 409 else ""
            raise KubernetesDiscoveryError(
                f"Unable to write upgrade checkpoint {namespace}/{name}:{conflict} "
                f"{_api_error_text(exc)}"
            ) from exc
        result_version = getattr(response.metadata, "resource_version", None)
        if not result_version:
            raise KubernetesDiscoveryError(
                f"Upgrade checkpoint {namespace}/{name} write returned no resourceVersion."
            )
        return str(result_version)

    def delete_upgrade_checkpoint(
        self,
        resource_version: str,
        name: str = CHECKPOINT_NAME,
        namespace: str = CHECKPOINT_NAMESPACE,
    ) -> None:
        client = self.client
        core = client.CoreV1Api()
        body = client.V1DeleteOptions(
            preconditions=client.V1Preconditions(resource_version=resource_version)
        )
        try:
            core.delete_namespaced_config_map(name, namespace, body=body)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return
            raise KubernetesDiscoveryError(
                f"Unable to delete upgrade checkpoint {namespace}/{name}: "
                f"{_api_error_text(exc)}"
            ) from exc

    def exec_slurmctld(self, command: tuple[str, ...]) -> str:
        """Execute a read-only Slurm command in the unique slurmctld container."""

        client = self.client
        core = client.CoreV1Api()
        try:
            pods = core.list_pod_for_all_namespaces().items
        except Exception as exc:
            raise KubernetesDiscoveryError(
                f"Unable to discover slurmctld: {_api_error_text(exc)}"
            ) from exc
        running = [
            pod
            for pod in pods
            if getattr(pod.status, "phase", None) == "Running"
        ]
        matches: list[tuple[Any, str]] = []
        for pod in running:
            names = [
                item.name
                for item in (getattr(pod.spec, "containers", None) or [])
                if item.name == "slurmctld"
            ]
            if len(names) == 1:
                matches.append((pod, names[0]))
        if len(matches) != 1:
            raise KubernetesDiscoveryError(
                "Expected exactly one running slurmctld container, found "
                f"{len(matches)}."
            )
        pod, container = matches[0]
        try:
            from kubernetes.stream import stream

            return str(
                stream(
                    core.connect_get_namespaced_pod_exec,
                    pod.metadata.name,
                    pod.metadata.namespace,
                    command=list(command),
                    container=container,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )
            )
        except Exception as exc:
            raise KubernetesDiscoveryError(
                f"Unable to execute read-only slurmctld check: {_api_error_text(exc)}"
            ) from exc

    def set_node_schedulable(self, node_name: str, schedulable: bool) -> None:
        core = self.client.CoreV1Api()
        core.patch_node(
            node_name,
            {"spec": {"unschedulable": not schedulable}},
        )

    def cordon_node(self, node_name: str) -> None:
        self.set_node_schedulable(node_name, schedulable=False)

    def uncordon_node(self, node_name: str) -> None:
        self.set_node_schedulable(node_name, schedulable=True)

    def list_drain_pods(
        self,
        node_name: str,
        grace_period_seconds: int = 30,
        check_evictions: bool = True,
    ) -> list[DrainPod]:
        client = self.client
        core = client.CoreV1Api()
        pods = core.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        ).items
        results: list[DrainPod] = []
        for pod in pods:
            if getattr(pod.status, "phase", None) in {"Succeeded", "Failed"}:
                continue
            daemonset = _is_daemonset_pod(pod)
            mirror = _is_mirror_pod(pod)
            blocker = None
            if check_evictions and not daemonset and not mirror:
                blocker = self._dry_run_eviction(pod, grace_period_seconds)
            results.append(
                DrainPod(
                    namespace=pod.metadata.namespace,
                    name=pod.metadata.name,
                    phase=getattr(pod.status, "phase", None),
                    controller=_pod_controller(pod),
                    daemonset=daemonset,
                    mirror=mirror,
                    has_empty_dir=_has_empty_dir(pod),
                    eviction_blocker=blocker,
                )
            )
        return sorted(results, key=lambda item: (item.namespace, item.name))

    def _dry_run_eviction(self, pod: Any, grace_period_seconds: int) -> str | None:
        client = self.client
        core = client.CoreV1Api()
        body = client.V1Eviction(
            metadata=client.V1ObjectMeta(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
            ),
            delete_options=client.V1DeleteOptions(
                grace_period_seconds=grace_period_seconds,
            ),
        )
        try:
            core.create_namespaced_pod_eviction(
                pod.metadata.name,
                pod.metadata.namespace,
                body,
                dry_run="All",
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            return _api_error_text(exc)
        return None

    def evict_drain_pods(
        self,
        pods: list[DrainPod],
        grace_period_seconds: int = 30,
        timeout_seconds: int = 600,
        poll_interval_seconds: int = 2,
    ) -> None:
        client = self.client
        core = client.CoreV1Api()
        deadline = time.monotonic() + timeout_seconds
        pending = {(pod.namespace, pod.name): pod for pod in pods if pod.evictable}
        blockers: dict[tuple[str, str], str] = {}
        requested: set[tuple[str, str]] = set()

        while pending:
            for key, pod in list(pending.items()):
                namespace, name = key
                try:
                    current = core.read_namespaced_pod(name, namespace)
                except Exception as exc:
                    if getattr(exc, "status", None) == 404:
                        pending.pop(key, None)
                        blockers.pop(key, None)
                        requested.discard(key)
                        continue
                    raise KubernetesDiscoveryError(
                        f"Unable to inspect pod {namespace}/{name}: {_api_error_text(exc)}"
                    ) from exc

                if getattr(current.metadata, "deletion_timestamp", None) or key in requested:
                    continue
                body = client.V1Eviction(
                    metadata=client.V1ObjectMeta(name=name, namespace=namespace),
                    delete_options=client.V1DeleteOptions(
                        grace_period_seconds=grace_period_seconds,
                    ),
                )
                try:
                    core.create_namespaced_pod_eviction(name, namespace, body)
                    blockers.pop(key, None)
                    requested.add(key)
                except Exception as exc:
                    status = getattr(exc, "status", None)
                    if status == 404:
                        pending.pop(key, None)
                        blockers.pop(key, None)
                        requested.discard(key)
                    elif status == 429:
                        blockers[key] = _api_error_text(exc)
                    else:
                        raise KubernetesDiscoveryError(
                            f"Unable to evict pod {namespace}/{name}: {_api_error_text(exc)}"
                        ) from exc

            if not pending:
                return
            if time.monotonic() >= deadline:
                details = ", ".join(
                    f"{namespace}/{name} ({blockers.get((namespace, name), 'still terminating')})"
                    for namespace, name in sorted(pending)
                )
                raise KubernetesDiscoveryError(
                    f"Timed out draining pods after {timeout_seconds}s: {details}"
                )
            time.sleep(poll_interval_seconds)

    @contextmanager
    def mutation_lease(
        self,
        name: str = "mgmt-oke-mutation",
        namespace: str = "kube-system",
        duration_seconds: int = 1800,
    ) -> Iterator[str]:
        client = self.client
        coordination = client.CoordinationV1Api()
        holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        duration_seconds = max(60, duration_seconds)

        try:
            existing = coordination.read_namespaced_lease(name, namespace)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise KubernetesDiscoveryError(
                    f"Unable to inspect mutation Lease {namespace}/{name}: {_api_error_text(exc)}"
                ) from exc
            body = self._lease_body(name, namespace, holder, duration_seconds, now)
            try:
                coordination.create_namespaced_lease(namespace, body)
            except Exception as create_exc:
                raise KubernetesDiscoveryError(
                    f"Unable to acquire mutation Lease {namespace}/{name}: "
                    f"{_api_error_text(create_exc)}"
                ) from create_exc
        else:
            current_holder = getattr(existing.spec, "holder_identity", None)
            renew_time = (
                getattr(existing.spec, "renew_time", None)
                or getattr(existing.spec, "acquire_time", None)
            )
            lease_duration = getattr(existing.spec, "lease_duration_seconds", None) or duration_seconds
            active = bool(
                current_holder
                and renew_time
                and _utc_datetime(renew_time) + timedelta(seconds=lease_duration) > now
            )
            if active:
                raise KubernetesDiscoveryError(
                    f"Another mutation is active under Lease {namespace}/{name}: {current_holder}"
                )
            body = self._lease_body(name, namespace, holder, duration_seconds, now)
            body.metadata.resource_version = existing.metadata.resource_version
            try:
                coordination.replace_namespaced_lease(name, namespace, body)
            except Exception as replace_exc:
                raise KubernetesDiscoveryError(
                    f"Unable to acquire mutation Lease {namespace}/{name}: "
                    f"{_api_error_text(replace_exc)}"
                ) from replace_exc

        try:
            yield holder
        finally:
            try:
                current = coordination.read_namespaced_lease(name, namespace)
                if getattr(current.spec, "holder_identity", None) == holder:
                    coordination.delete_namespaced_lease(name, namespace)
            except Exception:
                pass

    def _lease_body(
        self,
        name: str,
        namespace: str,
        holder: str,
        duration_seconds: int,
        now: datetime,
    ) -> Any:
        client = self.client
        return client.V1Lease(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            spec=client.V1LeaseSpec(
                holder_identity=holder,
                lease_duration_seconds=duration_seconds,
                acquire_time=now,
                renew_time=now,
            ),
        )

    def list_autoscaler_entries(self) -> list[AutoscalerEntry]:
        client = self.client
        apps = client.AppsV1Api()
        entries: list[AutoscalerEntry] = []
        deployments = apps.list_deployment_for_all_namespaces().items
        for deployment in deployments:
            name = deployment.metadata.name
            labels = deployment.metadata.labels or {}
            if "cluster-autoscaler" not in name and labels.get("app") != "cluster-autoscaler":
                continue
            for container in deployment.spec.template.spec.containers:
                argv = list(container.command or []) + list(container.args or [])
                for arg in argv:
                    match = AUTOSCALER_NODES_RE.match(arg)
                    if not match:
                        continue
                    entries.append(
                        AutoscalerEntry(
                            min_size=int(match.group(1)),
                            max_size=int(match.group(2)),
                            target_id=match.group(3),
                            deployment=name,
                            namespace=deployment.metadata.namespace,
                        )
                    )
        return entries

    def get_kueue_summary(self) -> KueueSummary:
        client = self.client
        custom = client.CustomObjectsApi()
        summary = KueueSummary()

        def list_first_available(
            plural: str,
            versions: tuple[str, ...] = ("v1beta1", "v1beta2"),
        ) -> list[dict[str, Any]]:
            failures: list[Exception] = []
            for version in versions:
                try:
                    response = custom.list_cluster_custom_object("kueue.x-k8s.io", version, plural)
                    return response.get("items", [])
                except Exception as exc:
                    if getattr(exc, "status", None) == 404:
                        continue
                    failures.append(exc)
                    continue
            if failures:
                raise KubernetesDiscoveryError(
                    f"Unable to verify Kueue {plural}: "
                    f"{_api_error_text(failures[-1])}"
                )
            return []

        summary.topologies = list_first_available("topologies")
        summary.resource_flavors = list_first_available("resourceflavors")
        summary.cluster_queues = list_first_available("clusterqueues")
        summary.local_queues = list_first_available("localqueues")
        summary.workloads = list_first_available("workloads")
        return summary


def _is_recognized_scheduler_infrastructure(labels: dict[str, str]) -> bool:
    name = labels.get("app.kubernetes.io/name", "").casefold()
    component = labels.get("app.kubernetes.io/component", "").casefold()
    return bool(
        name in {"cluster-autoscaler", "kueue", "slurmctld"}
        and component in {"", "controller", "manager", "scheduler"}
    )
