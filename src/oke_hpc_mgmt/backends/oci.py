from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from oke_hpc_mgmt.models import AddonInfo, WorkerPoolInfo


class OciDiscoveryError(RuntimeError):
    """Raised when OCI discovery cannot run."""


T = TypeVar("T")


class OciBackend:
    def __init__(
        self,
        auth: str = "config_file",
        region: str | None = None,
        config_file: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.auth = auth
        self.region = region
        self.config_file = config_file
        self.profile = profile or "DEFAULT"
        self._oci = None
        self._config: dict[str, Any] | None = None
        self._signer = None
        self._container_engine = None
        self._compute_mgmt = None
        self._compute = None

    def _ensure_loaded(self) -> None:
        if self._oci is not None:
            return
        try:
            import oci
            import oci.auth.signers
            import oci.config
            import oci.container_engine
            import oci.core
            import oci.pagination
        except ImportError as exc:
            raise OciDiscoveryError(
                "The oci Python package is not installed. Install the project dependencies first."
            ) from exc

        self._oci = oci
        if self.auth == "none":
            raise OciDiscoveryError("OCI auth is disabled.")
        if self.auth == "instance_principal":
            self._config = {"region": self.region} if self.region else {}
            self._signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        elif self.auth == "resource_principal":
            self._config = {"region": self.region} if self.region else {}
            self._signer = oci.auth.signers.get_resource_principals_signer()
        else:
            self._config = oci.config.from_file(file_location=self.config_file, profile_name=self.profile)
            if self.region:
                self._config["region"] = self.region

    @property
    def oci(self):
        self._ensure_loaded()
        return self._oci

    @property
    def container_engine(self):
        self._ensure_loaded()
        if self._container_engine is None:
            self._container_engine = self.oci.container_engine.ContainerEngineClient(
                config=self._config or {}, signer=self._signer
            )
        return self._container_engine

    @property
    def compute_mgmt(self):
        self._ensure_loaded()
        if self._compute_mgmt is None:
            self._compute_mgmt = self.oci.core.ComputeManagementClient(
                config=self._config or {}, signer=self._signer
            )
        return self._compute_mgmt

    @property
    def compute(self):
        self._ensure_loaded()
        if self._compute is None:
            self._compute = self.oci.core.ComputeClient(config=self._config or {}, signer=self._signer)
        return self._compute

    @staticmethod
    def _call(operation: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return function(*args, **kwargs)
        except OciDiscoveryError:
            raise
        except Exception as exc:
            raise OciDiscoveryError(f"{operation} failed: {exc}") from exc

    def list_managed_node_pools(self, compartment_id: str, cluster_id: str | None = None) -> list[WorkerPoolInfo]:
        kwargs: dict[str, Any] = {"compartment_id": compartment_id}
        if cluster_id:
            kwargs["cluster_id"] = cluster_id
        response = self.oci.pagination.list_call_get_all_results(self.container_engine.list_node_pools, **kwargs)

        pools: list[WorkerPoolInfo] = []
        for summary in response.data:
            try:
                node_pool = self.container_engine.get_node_pool(summary.id).data
            except Exception:
                node_pool = summary

            nodes = list(getattr(node_pool, "nodes", None) or [])
            active_nodes = [
                node
                for node in nodes
                if getattr(node, "lifecycle_state", "").upper() in {"ACTIVE", "RUNNING"}
            ]
            shape = getattr(node_pool, "node_shape", None) or getattr(summary, "node_shape", None)
            desired_size = _node_pool_size(node_pool)
            node_config = getattr(node_pool, "node_config_details", None)
            placement_configs = list(getattr(node_config, "placement_configs", None) or [])
            compute_cluster_id = getattr(node_config, "compute_cluster_id", None)
            pools.append(
                WorkerPoolInfo(
                    name=getattr(node_pool, "name", None) or getattr(summary, "name", None) or summary.id,
                    kind="node-pool",
                    shape=shape,
                    compartment_id=compartment_id,
                    desired_size=desired_size,
                    active_oci_instances=len(active_nodes) if nodes else desired_size,
                    node_pool_id=summary.id,
                    placement_type="compute-cluster" if compute_cluster_id else "standard",
                    compute_cluster_id=compute_cluster_id,
                    host_group_ids={
                        host_group_id
                        for placement in placement_configs
                        if (host_group_id := getattr(placement, "host_group_id", None))
                    },
                    availability_domain=_first_node_pool_placement_ad(node_config),
                    oci_instance_ids={
                        node_id for node in active_nodes if (node_id := _object_id(node))
                    },
                    gpu_resource=_gpu_resource_for_shape(shape),
                    rdma_enabled=bool(compute_cluster_id),
                    labels=_initial_node_labels(node_pool),
                )
            )
        return pools

    def list_cluster_addons(self, cluster_id: str) -> list[AddonInfo]:
        response = self.oci.pagination.list_call_get_all_results(
            self.container_engine.list_addons,
            cluster_id,
        )
        return [
            AddonInfo(
                name=getattr(addon, "name", "unknown"),
                lifecycle_state=getattr(addon, "lifecycle_state", None),
                version=(
                    getattr(addon, "current_installed_version", None)
                    or getattr(addon, "version", None)
                ),
                error=_addon_error(addon),
            )
            for addon in response.data
        ]

    def resize_managed_node_pool(self, node_pool_id: str, size: int) -> str | None:
        if size < 0:
            raise OciDiscoveryError("Node pool size cannot be negative.")

        update_node_config = self.oci.container_engine.models.UpdateNodePoolNodeConfigDetails(
            size=size,
        )
        update_details = self.oci.container_engine.models.UpdateNodePoolDetails(
            node_config_details=update_node_config,
        )
        response = self._call(
            "Managed OKE node pool resize",
            self.container_engine.update_node_pool,
            node_pool_id,
            update_details,
        )
        return response.headers.get("opc-work-request-id")

    def resize_cluster_network(
        self,
        cluster_network_id: str,
        instance_pool_id: str,
        size: int,
    ) -> str | None:
        if size < 0:
            raise OciDiscoveryError("Cluster network pool size cannot be negative.")

        cluster_network = self._call(
            "Cluster Network lookup",
            self.compute_mgmt.get_cluster_network,
            cluster_network_id,
        ).data
        instance_pools = list(getattr(cluster_network, "instance_pools", None) or [])
        if not instance_pools:
            raise OciDiscoveryError("The cluster network does not contain an instance pool.")

        update_pools = []
        matched = False
        model = self.oci.core.models.UpdateClusterNetworkInstancePoolDetails
        for instance_pool in instance_pools:
            pool_id = getattr(instance_pool, "id", None)
            is_target = pool_id == instance_pool_id
            matched = matched or is_target
            update_pools.append(
                model(
                    id=pool_id,
                    instance_configuration_id=getattr(
                        instance_pool,
                        "instance_configuration_id",
                        None,
                    ),
                    display_name=getattr(instance_pool, "display_name", None),
                    size=size if is_target else getattr(instance_pool, "size", None),
                    defined_tags=getattr(instance_pool, "defined_tags", None),
                    freeform_tags=getattr(instance_pool, "freeform_tags", None),
                )
            )
        if not matched:
            raise OciDiscoveryError(
                f"Instance pool {instance_pool_id} is not part of cluster network {cluster_network_id}."
            )

        update_details = self.oci.core.models.UpdateClusterNetworkDetails(
            display_name=getattr(cluster_network, "display_name", None),
            defined_tags=getattr(cluster_network, "defined_tags", None),
            freeform_tags=getattr(cluster_network, "freeform_tags", None),
            instance_pools=update_pools,
        )
        response = self._call(
            "Cluster Network resize",
            self.compute_mgmt.update_cluster_network,
            cluster_network_id,
            update_details,
        )
        return response.headers.get("opc-work-request-id")

    def resize_instance_pool(self, instance_pool_id: str, size: int) -> str | None:
        if size < 0:
            raise OciDiscoveryError("Instance pool size cannot be negative.")
        update_details = self.oci.core.models.UpdateInstancePoolDetails(size=size)
        response = self._call(
            "Instance pool resize",
            self.compute_mgmt.update_instance_pool,
            instance_pool_id,
            update_details,
        )
        return response.headers.get("opc-work-request-id")

    def delete_node(
        self,
        node_pool_id: str,
        node_id: str,
        decrement_size: bool = True,
        override_eviction_grace_duration: str | None = None,
        force_after_grace: bool = False,
    ) -> str | None:
        kwargs: dict[str, Any] = {
            "is_decrement_size": decrement_size,
            "is_force_deletion_after_override_grace_duration": force_after_grace,
        }
        if override_eviction_grace_duration:
            kwargs["override_eviction_grace_duration"] = override_eviction_grace_duration
        response = self._call(
            "Managed OKE node deletion",
            self.container_engine.delete_node,
            node_pool_id,
            node_id,
            **kwargs,
        )
        return response.headers.get("opc-work-request-id")

    def detach_instance_pool_node(
        self,
        instance_pool_id: str,
        instance_id: str,
        decrement_size: bool = True,
    ) -> str | None:
        details = self.oci.core.models.DetachInstancePoolInstanceDetails(
            instance_id=instance_id,
            is_decrement_size=decrement_size,
            is_auto_terminate=True,
        )
        response = self._call(
            "Instance pool node detach",
            self.compute_mgmt.detach_instance_pool_instance,
            instance_pool_id,
            details,
        )
        return response.headers.get("opc-work-request-id")

    def list_cluster_network_pools(self, compartment_id: str) -> list[WorkerPoolInfo]:
        response = self.oci.pagination.list_call_get_all_results(
            self.compute_mgmt.list_cluster_networks,
            compartment_id=compartment_id,
        )
        pools: list[WorkerPoolInfo] = []
        for cluster_network in response.data:
            state = getattr(cluster_network, "lifecycle_state", "")
            if state and state.upper() in {"TERMINATED", "TERMINATING", "DELETING", "DELETED"}:
                continue
            instance_pool_refs = list(getattr(cluster_network, "instance_pools", None) or [])
            instance_pool_id = getattr(instance_pool_refs[0], "id", None) if instance_pool_refs else None
            pool = WorkerPoolInfo(
                name=(
                    getattr(cluster_network, "display_name", None)
                    or getattr(cluster_network, "id", None)
                    or "unknown-cluster-network"
                ),
                kind="cluster-network",
                compartment_id=compartment_id,
                cluster_network_id=getattr(cluster_network, "id", None),
                instance_pool_id=instance_pool_id,
                placement_type="cluster-network",
                rdma_enabled=True,
            )
            if instance_pool_id:
                self._enrich_from_instance_pool(pool, compartment_id, instance_pool_id)
            pools.append(pool)
        return pools

    def list_instance_pools(
        self,
        compartment_id: str,
        skip_ids: set[str] | None = None,
        skip_compute_cluster_ids: set[str] | None = None,
        skip_instance_ids: set[str] | None = None,
    ) -> list[WorkerPoolInfo]:
        skip_ids = skip_ids or set()
        skip_compute_cluster_ids = skip_compute_cluster_ids or set()
        skip_instance_ids = skip_instance_ids or set()
        response = self.oci.pagination.list_call_get_all_results(
            self.compute_mgmt.list_instance_pools,
            compartment_id=compartment_id,
        )
        pools: list[WorkerPoolInfo] = []
        for instance_pool in response.data:
            if instance_pool.id in skip_ids:
                continue
            state = getattr(instance_pool, "lifecycle_state", "")
            if state and state.upper() in {"TERMINATED", "TERMINATING", "DELETING", "DELETED"}:
                continue
            pool = WorkerPoolInfo(
                name=getattr(instance_pool, "display_name", None) or instance_pool.id,
                kind="instance-pool",
                compartment_id=compartment_id,
                instance_pool_id=instance_pool.id,
                placement_type="instance-pool",
            )
            self._enrich_from_instance_pool(pool, compartment_id, instance_pool.id, instance_pool=instance_pool)
            if _is_internal_managed_backing_pool(
                pool,
                skip_compute_cluster_ids,
                skip_instance_ids,
            ):
                continue
            pools.append(pool)
        return pools

    def _enrich_from_instance_pool(
        self,
        pool: WorkerPoolInfo,
        compartment_id: str,
        instance_pool_id: str,
        instance_pool: Any | None = None,
    ) -> None:
        if instance_pool is None:
            instance_pool = self.compute_mgmt.get_instance_pool(instance_pool_id).data
        pool.desired_size = getattr(instance_pool, "size", None)
        pool.availability_domain = _first_placement_ad(instance_pool)
        compute_cluster_ids = _placement_compute_cluster_ids(instance_pool)
        if compute_cluster_ids:
            pool.compute_cluster_id = sorted(compute_cluster_ids)[0]
            if pool.placement_type != "cluster-network":
                pool.placement_type = "compute-cluster"

        instances = self.oci.pagination.list_call_get_all_results(
            self.compute_mgmt.list_instance_pool_instances,
            compartment_id,
            instance_pool_id,
        ).data
        active = [
            item
            for item in instances
            if _lifecycle_state(item).upper() in {"RUNNING", "ACTIVE", "STARTING", "PROVISIONING"}
        ]
        pool.active_oci_instances = len(active)
        pool.oci_instance_ids = {item_id for item in instances if (item_id := _object_id(item))}

        if active:
            try:
                instance = self.compute.get_instance(_object_id(active[0])).data
                pool.shape = getattr(instance, "shape", None)
            except Exception:
                pass
        pool.gpu_resource = _gpu_resource_for_shape(pool.shape)
        pool.rdma_enabled = pool.rdma_enabled or bool(pool.shape and pool.shape.startswith("BM.GPU"))


def _node_pool_size(node_pool: Any) -> int | None:
    node_config = getattr(node_pool, "node_config_details", None)
    if node_config and getattr(node_config, "size", None) is not None:
        return node_config.size
    if getattr(node_pool, "quantity_per_subnet", None) is not None:
        subnets = getattr(node_pool, "subnet_ids", None) or []
        return node_pool.quantity_per_subnet * max(1, len(subnets))
    nodes = getattr(node_pool, "nodes", None)
    if nodes is not None:
        return len(nodes)
    return None


def _first_placement_ad(instance_pool: Any) -> str | None:
    placement_configs = getattr(instance_pool, "placement_configurations", None) or []
    if not placement_configs:
        return None
    return getattr(placement_configs[0], "availability_domain", None)


def _first_node_pool_placement_ad(node_config: Any) -> str | None:
    placement_configs = getattr(node_config, "placement_configs", None) or []
    if not placement_configs:
        return None
    return getattr(placement_configs[0], "availability_domain", None)


def _placement_compute_cluster_ids(instance_pool: Any) -> set[str]:
    return {
        compute_cluster_id
        for placement in (getattr(instance_pool, "placement_configurations", None) or [])
        if (compute_cluster_id := getattr(placement, "compute_cluster_id", None))
    }


def _is_internal_managed_backing_pool(
    pool: WorkerPoolInfo,
    managed_compute_cluster_ids: set[str],
    managed_instance_ids: set[str],
) -> bool:
    return bool(
        (pool.compute_cluster_id and pool.compute_cluster_id in managed_compute_cluster_ids)
        or pool.oci_instance_ids.intersection(managed_instance_ids)
    )


def _initial_node_labels(node_pool: Any) -> dict[str, str]:
    return {
        key: value
        for label in (getattr(node_pool, "initial_node_labels", None) or [])
        if (key := getattr(label, "key", None))
        if (value := getattr(label, "value", None)) is not None
    }


def _addon_error(addon: Any) -> str | None:
    error = getattr(addon, "addon_error", None)
    if error is None:
        return None
    return str(error)


def _object_id(item: Any) -> str | None:
    for attr in ("id", "instance_id", "node_id"):
        value = getattr(item, attr, None)
        if value:
            return value
    return None


def _lifecycle_state(item: Any) -> str:
    return getattr(item, "lifecycle_state", None) or getattr(item, "state", None) or ""


def _gpu_resource_for_shape(shape: str | None) -> str | None:
    if not shape or "GPU" not in shape:
        return None
    if ".MI" in shape:
        return "amd.com/gpu"
    return "nvidia.com/gpu"
