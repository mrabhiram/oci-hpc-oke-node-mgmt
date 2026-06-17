from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from oke_hpc_mgmt.backends.kubernetes import KubernetesBackend
from oke_hpc_mgmt.backends.oci import OciBackend
from oke_hpc_mgmt.models import DiscoverySnapshot, NodeInfo, WorkerPoolInfo


@dataclass
class DiscoveryOptions:
    compartment_id: str | None = None
    cluster_id: str | None = None
    region: str | None = None
    auth: str = "config_file"
    oci_config_file: str | None = None
    oci_profile: str | None = None
    kubeconfig: str | None = None
    context: str | None = None
    in_cluster: bool = False
    skip_oci: bool = False
    skip_kubernetes: bool = False
    include_pod_counts: bool = True
    include_autoscaler: bool = True
    include_kueue: bool = True


class DiscoveryService:
    def __init__(self, options: DiscoveryOptions) -> None:
        self.options = options
        self._k8s_backend: KubernetesBackend | None = None
        self._oci_backend: OciBackend | None = None

    def discover(self) -> DiscoverySnapshot:
        snapshot = DiscoverySnapshot()
        nodes = self._discover_kubernetes(snapshot)
        pools = self._discover_oci(snapshot)

        if pools:
            self._assign_nodes_to_pools(nodes, pools)
        else:
            pools = self._infer_pools_from_nodes(nodes)

        autoscaler_entries = self._discover_autoscaler(snapshot)
        kueue = self._discover_kueue(snapshot)
        snapshot.nodes = nodes
        snapshot.pools = pools
        snapshot.autoscaler_entries = autoscaler_entries
        snapshot.kueue = kueue

        if self.options.include_autoscaler:
            for pool in snapshot.pools:
                pool.autoscaler_owned = False
        self._apply_autoscaler_ownership(snapshot)
        self._apply_kueue_flavors(snapshot)
        self._recalculate_pool_counts(snapshot)
        return snapshot

    def _k8s(self) -> KubernetesBackend:
        if self._k8s_backend is None:
            self._k8s_backend = KubernetesBackend(
                kubeconfig=self.options.kubeconfig,
                context=self.options.context,
                in_cluster=self.options.in_cluster,
            )
        return self._k8s_backend

    def _oci(self) -> OciBackend:
        if self._oci_backend is None:
            self._oci_backend = OciBackend(
                auth=self.options.auth,
                region=self.options.region,
                config_file=self.options.oci_config_file,
                profile=self.options.oci_profile,
            )
        return self._oci_backend

    def _discover_kubernetes(self, snapshot: DiscoverySnapshot) -> list[NodeInfo]:
        if self.options.skip_kubernetes:
            return []
        try:
            return self._k8s().list_nodes(include_pod_counts=self.options.include_pod_counts)
        except Exception as exc:
            snapshot.warnings.append(f"Kubernetes discovery skipped: {exc}")
            return []

    def _discover_oci(self, snapshot: DiscoverySnapshot) -> list[WorkerPoolInfo]:
        if self.options.skip_oci or self.options.auth == "none":
            return []
        if not self.options.compartment_id:
            snapshot.warnings.append(
                "OCI discovery skipped: --compartment-id or OCI_COMPARTMENT_ID is required."
            )
            return []

        backend = self._oci()
        pools: list[WorkerPoolInfo] = []
        try:
            pools.extend(
                backend.list_managed_node_pools(
                    self.options.compartment_id,
                    cluster_id=self.options.cluster_id,
                )
            )
        except Exception as exc:
            snapshot.warnings.append(f"Managed node pool discovery skipped: {exc}")

        try:
            cluster_network_pools = backend.list_cluster_network_pools(self.options.compartment_id)
            pools.extend(cluster_network_pools)
            cluster_network_instance_pool_ids = {
                pool.instance_pool_id for pool in cluster_network_pools if pool.instance_pool_id
            }
        except Exception as exc:
            snapshot.warnings.append(f"Cluster network discovery skipped: {exc}")
            cluster_network_instance_pool_ids = set()

        try:
            pools.extend(
                backend.list_instance_pools(
                    self.options.compartment_id,
                    skip_ids=cluster_network_instance_pool_ids,
                )
            )
        except Exception as exc:
            snapshot.warnings.append(f"Standalone instance pool discovery skipped: {exc}")

        return pools

    def _discover_autoscaler(self, snapshot: DiscoverySnapshot):
        if self.options.skip_kubernetes or not self.options.include_autoscaler:
            return []
        try:
            return self._k8s().list_autoscaler_entries()
        except Exception as exc:
            snapshot.warnings.append(f"Cluster Autoscaler discovery skipped: {exc}")
            return []

    def _discover_kueue(self, snapshot: DiscoverySnapshot):
        if self.options.skip_kubernetes or not self.options.include_kueue:
            from oke_hpc_mgmt.models import KueueSummary

            return KueueSummary()
        try:
            return self._k8s().get_kueue_summary()
        except Exception as exc:
            snapshot.warnings.append(f"Kueue discovery skipped: {exc}")
            from oke_hpc_mgmt.models import KueueSummary

            return KueueSummary()

    @staticmethod
    def _assign_nodes_to_pools(nodes: list[NodeInfo], pools: list[WorkerPoolInfo]) -> None:
        pools_by_node_pool_id = {pool.node_pool_id: pool for pool in pools if pool.node_pool_id}
        pools_by_name = {pool.name: pool for pool in pools}
        pools_by_instance_id: dict[str, WorkerPoolInfo] = {}
        pools_by_shape: dict[str, list[WorkerPoolInfo]] = defaultdict(list)
        for pool in pools:
            for instance_id in pool.oci_instance_ids:
                pools_by_instance_id[instance_id] = pool
            if pool.shape:
                pools_by_shape[pool.shape].append(pool)

        for node in nodes:
            pool = None
            if node.node_pool_id:
                pool = pools_by_node_pool_id.get(node.node_pool_id)
            if pool is None and node.instance_ocid:
                pool = pools_by_instance_id.get(node.instance_ocid)
            if pool is None and node.pool_name:
                pool = pools_by_name.get(node.pool_name)
            if pool is None and node.shape and len(pools_by_shape[node.shape]) == 1:
                pool = pools_by_shape[node.shape][0]
            if pool is not None:
                node.pool_name = pool.name

    @staticmethod
    def _infer_pools_from_nodes(nodes: list[NodeInfo]) -> list[WorkerPoolInfo]:
        grouped: dict[str, list[NodeInfo]] = defaultdict(list)
        for node in nodes:
            group = node.pool_name or node.node_pool_id or node.shape or "kubernetes-nodes"
            grouped[group].append(node)

        pools: list[WorkerPoolInfo] = []
        for name, group_nodes in sorted(grouped.items()):
            first = group_nodes[0]
            for node in group_nodes:
                node.pool_name = name
            pools.append(
                WorkerPoolInfo(
                    name=name,
                    kind="kubernetes-inferred",
                    shape=first.shape,
                    desired_size=len(group_nodes),
                    active_oci_instances=None,
                    ready_k8s_nodes=sum(1 for node in group_nodes if node.ready),
                    node_pool_id=first.node_pool_id,
                    gpu_resource=first.gpu_resource,
                    rdma_enabled=any(node.has_rdma_labels for node in group_nodes),
                )
            )
        return pools

    @staticmethod
    def _apply_autoscaler_ownership(snapshot: DiscoverySnapshot) -> None:
        if not snapshot.autoscaler_entries:
            return

        pools_by_target: dict[str, WorkerPoolInfo] = {}
        for pool in snapshot.pools:
            for target in (pool.instance_pool_id, pool.node_pool_id, pool.cluster_network_id):
                if target:
                    pools_by_target[target] = pool

        for entry in snapshot.autoscaler_entries:
            pool = pools_by_target.get(entry.target_id)
            if not pool:
                continue
            pool.autoscaler_owned = True
            pool.autoscaler_min = entry.min_size
            pool.autoscaler_max = entry.max_size

    @staticmethod
    def _apply_kueue_flavors(snapshot: DiscoverySnapshot) -> None:
        for flavor in snapshot.kueue.resource_flavors:
            metadata = flavor.get("metadata", {})
            spec = flavor.get("spec", {})
            node_labels = spec.get("nodeLabels", {})
            flavor_name = metadata.get("name")
            shape = node_labels.get("node.kubernetes.io/instance-type")
            for pool in snapshot.pools:
                if shape and pool.shape == shape:
                    pool.kueue_flavor = flavor_name

    @staticmethod
    def _recalculate_pool_counts(snapshot: DiscoverySnapshot) -> None:
        nodes_by_pool: dict[str, list[NodeInfo]] = defaultdict(list)
        for node in snapshot.nodes:
            if node.pool_name:
                nodes_by_pool[node.pool_name].append(node)

        for pool in snapshot.pools:
            pool_nodes = nodes_by_pool.get(pool.name, [])
            pool.ready_k8s_nodes = sum(1 for node in pool_nodes if node.ready)
            if pool_nodes:
                pool.rdma_enabled = pool.rdma_enabled or any(node.has_rdma_labels for node in pool_nodes)
                pool.gpu_resource = pool.gpu_resource or next(
                    (node.gpu_resource for node in pool_nodes if node.gpu_resource),
                    None,
                )
                pool.shape = pool.shape or next((node.shape for node in pool_nodes if node.shape), None)
