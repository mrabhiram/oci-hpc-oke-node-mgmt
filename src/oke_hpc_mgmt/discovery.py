from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from oke_hpc_mgmt.backends.kubeconfig import (
    KubeconfigDiscoveryError,
    load_oke_kubeconfig_context,
)
from oke_hpc_mgmt.backends.kubernetes import KubernetesBackend
from oke_hpc_mgmt.backends.oci import OciBackend, OciDiscoveryError
from oke_hpc_mgmt.models import (
    SLINKY_HOSTNAME_PREFIX_LABEL,
    AddonInfo,
    DiscoverySnapshot,
    NodeInfo,
    WorkerPoolInfo,
)


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
    include_addons: bool = True
    include_pools: bool = True


@dataclass(frozen=True)
class ResolvedOciTarget:
    compartment_id: str | None
    cluster_id: str | None
    region: str | None


class DiscoveryService:
    def __init__(self, options: DiscoveryOptions) -> None:
        self.options = options
        self._k8s_backend: KubernetesBackend | None = None
        self._oci_backend: OciBackend | None = None
        self._oci_target_resolved = False
        self._oci_target_error: str | None = None

    def discover(self) -> DiscoverySnapshot:
        snapshot = DiscoverySnapshot(
            oci_discovery_enabled=(
                not self.options.skip_oci and self.options.auth != "none"
            ),
            kubernetes_discovery_enabled=not self.options.skip_kubernetes,
        )
        self.resolve_oci_target()
        nodes = self._discover_kubernetes(snapshot)
        pools = self._discover_oci(snapshot)
        addons = self._discover_addons(snapshot)

        if pools:
            self._assign_nodes_to_pools(nodes, pools)
        else:
            pools = self._infer_pools_from_nodes(nodes)

        autoscaler_entries = self._discover_autoscaler(snapshot)
        kueue = self._discover_kueue(snapshot)
        snapshot.nodes = nodes
        snapshot.pools = pools
        snapshot.addons = addons
        snapshot.autoscaler_entries = autoscaler_entries
        snapshot.kueue = kueue

        if self.options.include_autoscaler:
            for pool in snapshot.pools:
                pool.autoscaler_owned = False
        self._apply_autoscaler_ownership(snapshot)
        self._apply_kueue_flavors(snapshot)
        self._recalculate_pool_counts(snapshot)
        self._apply_addon_expectations(snapshot)
        return snapshot

    def resolve_oci_target(
        self,
        require_compartment: bool = False,
        require_cluster: bool = False,
    ) -> ResolvedOciTarget:
        if not self._oci_target_resolved:
            self._resolve_oci_target()

        target = ResolvedOciTarget(
            compartment_id=self.options.compartment_id,
            cluster_id=self.options.cluster_id,
            region=self.options.region,
        )
        if require_compartment and not target.compartment_id:
            raise OciDiscoveryError(
                self._oci_target_error
                or "Unable to determine the OCI compartment. Use --compartment-id as an override."
            )
        if require_cluster and not target.cluster_id:
            raise OciDiscoveryError(
                self._oci_target_error
                or "Unable to determine the OKE cluster OCID. Use --cluster-id as an override."
            )
        return target

    def _resolve_oci_target(self) -> None:
        self._oci_target_resolved = True
        if self.options.skip_oci:
            self._oci_target_error = "OCI discovery is disabled by --skip-oci."
            return
        if self.options.auth == "none":
            self._oci_target_error = "OCI authentication is disabled."
            return

        context_error: str | None = None
        if not self.options.in_cluster and (
            not self.options.cluster_id or not self.options.region
        ):
            try:
                kube_context = load_oke_kubeconfig_context(
                    kubeconfig=self.options.kubeconfig,
                    context=self.options.context,
                )
            except KubeconfigDiscoveryError as exc:
                context_error = f"Automatic OKE target discovery from kubeconfig failed: {exc}"
            else:
                if (
                    self.options.cluster_id
                    and self.options.cluster_id != kube_context.cluster_id
                ):
                    context_error = (
                        "The explicit OKE cluster OCID does not match the selected kubeconfig "
                        "context; provide --region for the explicit cluster."
                    )
                else:
                    self.options.cluster_id = self.options.cluster_id or kube_context.cluster_id
                    self.options.region = self.options.region or kube_context.region
        elif self.options.in_cluster and not self.options.cluster_id:
            context_error = (
                "Automatic OKE target discovery is unavailable with --in-cluster; "
                "provide --cluster-id and --region."
            )

        if self.options.compartment_id:
            self._oci_target_error = context_error
            return
        if not self.options.cluster_id:
            self._oci_target_error = context_error or (
                "Unable to determine the OKE cluster OCID from kubeconfig. "
                "Use --cluster-id or --compartment-id as an override."
            )
            return
        if (
            not self.options.region
            and self.options.auth in {"instance_principal", "resource_principal"}
        ):
            self._oci_target_error = context_error or (
                "Unable to determine the OCI region from kubeconfig. "
                "Use --region or --compartment-id as an override."
            )
            return

        try:
            self.options.compartment_id = self._oci().get_cluster_compartment_id(
                self.options.cluster_id
            )
        except Exception as exc:
            self._oci_target_error = (
                "Automatic compartment discovery from the OKE cluster failed: "
                f"{exc}. Use --compartment-id as an override."
            )
        else:
            self._oci_target_error = context_error

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

    def oci_backend(self) -> OciBackend:
        """Return the OCI backend configured with the resolved target region."""

        self.resolve_oci_target()
        return self._oci()

    def kubernetes_backend(self) -> KubernetesBackend:
        """Return the Kubernetes backend configured for the selected cluster."""

        if self.options.skip_kubernetes:
            raise OciDiscoveryError("Kubernetes access is disabled by --skip-kubernetes.")
        return self._k8s()

    def _discover_kubernetes(self, snapshot: DiscoverySnapshot) -> list[NodeInfo]:
        if self.options.skip_kubernetes:
            return []
        try:
            return self._k8s().list_nodes(include_pod_counts=self.options.include_pod_counts)
        except Exception as exc:
            snapshot.warnings.append(f"Kubernetes discovery skipped: {exc}")
            return []

    def _discover_oci(self, snapshot: DiscoverySnapshot) -> list[WorkerPoolInfo]:
        if (
            self.options.skip_oci
            or self.options.auth == "none"
            or not self.options.include_pools
        ):
            return []
        if not self.options.compartment_id:
            snapshot.warnings.append(
                "OCI worker-pool discovery skipped: "
                + (
                    self._oci_target_error
                    or "the compartment could not be determined; use --compartment-id as an override."
                )
            )
            return []

        backend = self._oci()
        pools: list[WorkerPoolInfo] = []
        try:
            managed_pools = backend.list_managed_node_pools(
                self.options.compartment_id,
                cluster_id=self.options.cluster_id,
            )
            pools.extend(managed_pools)
        except Exception as exc:
            snapshot.warnings.append(f"Managed node pool discovery skipped: {exc}")
            managed_pools = []

        managed_compute_cluster_ids = {
            pool.compute_cluster_id for pool in managed_pools if pool.compute_cluster_id
        }
        managed_instance_ids = {
            instance_id for pool in managed_pools for instance_id in pool.oci_instance_ids
        }

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
                    skip_compute_cluster_ids=managed_compute_cluster_ids,
                    skip_instance_ids=managed_instance_ids,
                )
            )
        except Exception as exc:
            snapshot.warnings.append(f"Standalone instance pool discovery skipped: {exc}")

        return pools

    def _discover_addons(self, snapshot: DiscoverySnapshot) -> list[AddonInfo]:
        if (
            self.options.skip_oci
            or self.options.auth == "none"
            or not self.options.include_addons
        ):
            return []
        if not self.options.cluster_id:
            if self._oci_target_error:
                snapshot.warnings.append(
                    f"OKE add-on discovery skipped: {self._oci_target_error}"
                )
            return []
        try:
            return self._oci().list_cluster_addons(self.options.cluster_id)
        except Exception as exc:
            snapshot.warnings.append(f"OKE add-on discovery skipped: {exc}")
            return []

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
            matched_pool: WorkerPoolInfo | None = None
            if node.node_pool_id:
                matched_pool = pools_by_node_pool_id.get(node.node_pool_id)
            if matched_pool is None and node.instance_ocid:
                matched_pool = pools_by_instance_id.get(node.instance_ocid)
            if matched_pool is None and node.pool_name:
                matched_pool = pools_by_name.get(node.pool_name)
            if matched_pool is None and node.shape and len(pools_by_shape[node.shape]) == 1:
                matched_pool = pools_by_shape[node.shape][0]
            if matched_pool is not None:
                node.pool_name = matched_pool.name

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
                    placement_type=(
                        first.labels.get("oke.oraclecloud.com/pool.mode") or "kubernetes-inferred"
                    ),
                    slinky_managed=any(node.slinky_managed for node in group_nodes),
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
            matched_pool = pools_by_target.get(entry.target_id)
            if not matched_pool:
                continue
            matched_pool.autoscaler_owned = True
            matched_pool.autoscaler_min = entry.min_size
            matched_pool.autoscaler_max = entry.max_size

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
                pool.slinky_managed = pool.slinky_managed or any(
                    node.slinky_managed for node in pool_nodes
                )
                pool.gpu_resource = pool.gpu_resource or next(
                    (node.gpu_resource for node in pool_nodes if node.gpu_resource),
                    None,
                )
                pool.shape = pool.shape or next((node.shape for node in pool_nodes if node.shape), None)
            pool.slinky_managed = pool.slinky_managed or bool(
                pool.labels.get(SLINKY_HOSTNAME_PREFIX_LABEL)
            )

    @staticmethod
    def _apply_addon_expectations(snapshot: DiscoverySnapshot) -> None:
        if not snapshot.network_operator_active:
            return
        for pool in snapshot.pools:
            if pool.rdma_enabled:
                pool.rdma_vf_required = True
