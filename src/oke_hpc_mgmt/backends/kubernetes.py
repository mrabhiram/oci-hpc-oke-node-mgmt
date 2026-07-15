from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from oke_hpc_mgmt.models import AutoscalerEntry, KueueSummary, NodeInfo


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
                )
            )
        return nodes

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
            for version in versions:
                try:
                    response = custom.list_cluster_custom_object("kueue.x-k8s.io", version, plural)
                    return response.get("items", [])
                except Exception:
                    continue
            return []

        summary.topologies = list_first_available("topologies")
        summary.resource_flavors = list_first_available("resourceflavors")
        summary.cluster_queues = list_first_available("clusterqueues")
        summary.local_queues = list_first_available("localqueues")
        return summary
