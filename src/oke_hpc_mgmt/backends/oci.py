from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any, TypeVar

from oke_hpc_mgmt.bootstrap import (
    BootstrapCompositionError,
    compose_worker_user_data,
    summarize_worker_bootstrap,
)
from oke_hpc_mgmt.models import (
    AddonCompatibility,
    AddonInfo,
    ClusterInfo,
    ClusterNetworkCreateResult,
    ComputeClusterInfo,
    ManagedNodePoolCreateResult,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
    VirtualNodePoolInfo,
    WorkerPoolInfo,
    WorkRequestInfo,
)
from oke_hpc_mgmt.validation import normalize_pool_name


class OciDiscoveryError(RuntimeError):
    """Raised when OCI discovery cannot run."""


class BootVolumeAttachmentPending(OciDiscoveryError):
    """Raised while a BVR instance has no active boot volume attachment."""


T = TypeVar("T")

_MANAGED_BOOTSTRAP_AUTHORITY_KEYS = frozenset(
    {
        "apiserver_host",
        "cluster_ca_cert",
        "oke-initial-node-labels",
        "oke-k8version",
        "oke-max-pods",
        "oke-native-pod-networking",
        "pod-nsgids",
        "pod-subnets",
        "ssh_authorized_keys",
    }
)
_CLUSTER_IDENTITY_METADATA_KEYS = (
    "apiserver_host",
    "cluster_ca_cert",
)


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
        self._identity = None
        self._virtual_network = None
        self._work_requests = None

    def _ensure_loaded(self) -> None:
        if self._oci is not None:
            return
        try:
            import oci
            import oci.auth.signers
            import oci.config
            import oci.container_engine
            import oci.core
            import oci.identity
            import oci.pagination
            import oci.work_requests
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

    @property
    def identity(self):
        self._ensure_loaded()
        if self._identity is None:
            self._identity = self.oci.identity.IdentityClient(
                config=self._config or {},
                signer=self._signer,
            )
        return self._identity

    @property
    def virtual_network(self):
        self._ensure_loaded()
        if self._virtual_network is None:
            self._virtual_network = self.oci.core.VirtualNetworkClient(
                config=self._config or {},
                signer=self._signer,
            )
        return self._virtual_network

    @property
    def work_requests(self):
        self._ensure_loaded()
        if self._work_requests is None:
            self._work_requests = self.oci.work_requests.WorkRequestClient(
                config=self._config or {}, signer=self._signer
            )
        return self._work_requests

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
            host_group_ids = {
                host_group_id
                for placement in placement_configs
                if (host_group_id := getattr(placement, "host_group_id", None))
            }
            pools.append(
                WorkerPoolInfo(
                    name=getattr(node_pool, "name", None) or getattr(summary, "name", None) or summary.id,
                    kind="node-pool",
                    shape=shape,
                    compartment_id=compartment_id,
                    desired_size=desired_size,
                    active_oci_instances=len(active_nodes) if nodes else desired_size,
                    node_pool_id=summary.id,
                    created_by_mgmt_oke=_is_mgmt_oke_created(
                        getattr(node_pool, "freeform_tags", None)
                    ),
                    placement_type=(
                        "compute-cluster"
                        if compute_cluster_id
                        else "host-group" if host_group_ids else "standard"
                    ),
                    compute_cluster_id=compute_cluster_id,
                    host_group_ids=host_group_ids,
                    availability_domain=_first_node_pool_placement_ad(node_config),
                    oci_instance_ids={
                        node_id for node in active_nodes if (node_id := _object_id(node))
                    },
                    gpu_resource=_gpu_resource_for_shape(shape),
                    rdma_enabled=bool(compute_cluster_id),
                    labels=_initial_node_labels(node_pool),
                    kubernetes_version=getattr(
                        node_pool,
                        "kubernetes_version",
                        None,
                    ),
                )
            )
        return pools

    def list_virtual_node_pools(
        self,
        compartment_id: str,
        cluster_id: str,
    ) -> list[VirtualNodePoolInfo]:
        response = self.oci.pagination.list_call_get_all_results(
            self.container_engine.list_virtual_node_pools,
            compartment_id,
            cluster_id=cluster_id,
        )
        return [
            VirtualNodePoolInfo(
                name=getattr(pool, "display_name", None) or pool.id,
                virtual_node_pool_id=pool.id,
                kubernetes_version=getattr(
                    pool,
                    "kubernetes_version",
                    None,
                ),
                size=getattr(pool, "size", None),
                lifecycle_state=getattr(pool, "lifecycle_state", None),
            )
            for pool in response.data
        ]

    def get_cluster_info(
        self,
        cluster_id: str,
        compartment_id: str | None = None,
    ) -> ClusterInfo:
        response = self._call(
            "OKE cluster lookup",
            self.container_engine.get_cluster,
            cluster_id,
        )
        cluster = response.data
        resolved_compartment = getattr(cluster, "compartment_id", None)
        version = getattr(cluster, "kubernetes_version", None)
        if not isinstance(resolved_compartment, str) or not resolved_compartment:
            raise OciDiscoveryError(
                f"OKE cluster {cluster_id} did not return a compartment OCID."
            )
        if not isinstance(version, str) or not version:
            raise OciDiscoveryError(
                f"OKE cluster {cluster_id} did not return a Kubernetes version."
            )
        versions = set(
            getattr(cluster, "available_kubernetes_upgrades", None) or ()
        )
        versions.add(version)
        try:
            options = self._call(
                "OKE cluster version options lookup",
                self.container_engine.get_cluster_options,
                cluster_id,
                compartment_id=compartment_id or resolved_compartment,
                should_list_all_patch_versions=True,
            ).data
            versions.update(getattr(options, "kubernetes_versions", None) or ())
        except OciDiscoveryError:
            if not versions:
                raise
        endpoints = getattr(cluster, "endpoints", None)
        endpoint = (
            getattr(endpoints, "private_endpoint", None)
            or getattr(endpoints, "public_endpoint", None)
            or getattr(endpoints, "kubernetes", None)
        )
        return ClusterInfo(
            cluster_id=cluster_id,
            compartment_id=resolved_compartment,
            kubernetes_version=version,
            lifecycle_state=getattr(cluster, "lifecycle_state", None),
            cluster_type=getattr(cluster, "type", None),
            endpoint=endpoint,
            available_kubernetes_versions=tuple(sorted(versions)),
            etag=(getattr(response, "headers", None) or {}).get("etag"),
        )

    def get_cluster_compartment_id(self, cluster_id: str) -> str:
        response = self._call(
            "OKE cluster lookup",
            self.container_engine.get_cluster,
            cluster_id,
        )
        compartment_id = getattr(response.data, "compartment_id", None)
        if not isinstance(compartment_id, str) or not compartment_id:
            raise OciDiscoveryError(
                f"OKE cluster {cluster_id} did not return a compartment OCID."
            )
        return compartment_id

    def get_cluster_type(self, cluster_id: str) -> str:
        response = self._call(
            "OKE cluster type lookup",
            self.container_engine.get_cluster,
            cluster_id,
        )
        cluster_type = str(getattr(response.data, "type", "") or "").upper()
        if not cluster_type:
            raise OciDiscoveryError(
                f"OKE cluster {cluster_id} did not return its cluster type."
            )
        return cluster_type

    def list_cluster_addons(self, cluster_id: str) -> list[AddonInfo]:
        response = self.oci.pagination.list_call_get_all_results(
            self.container_engine.list_addons,
            cluster_id,
        )
        addons: list[AddonInfo] = []
        for addon in response.data:
            selected_version = getattr(addon, "version", None)
            addons.append(
                AddonInfo(
                    name=getattr(addon, "name", "unknown"),
                    lifecycle_state=getattr(addon, "lifecycle_state", None),
                    version=(
                        getattr(addon, "current_installed_version", None)
                        or selected_version
                    ),
                    selected_version=selected_version,
                    update_mode=(
                        "AUTOMATIC"
                        if selected_version is None
                        else "PINNED"
                    ),
                    configurations=tuple(
                        sorted(
                            (
                                str(getattr(item, "key", "")),
                                str(getattr(item, "value", "")),
                            )
                            for item in (
                                getattr(addon, "configurations", None) or ()
                            )
                            if getattr(item, "key", None)
                        )
                    ),
                    error=_addon_error(addon),
                )
            )
        return addons

    def get_addon_compatibility(
        self,
        target_version: str,
        installed: list[AddonInfo],
    ) -> list[AddonCompatibility]:
        response = self._call(
            "OKE add-on options lookup",
            self.oci.pagination.list_call_get_all_results,
            self.container_engine.list_addon_options,
            target_version,
            should_show_all_versions=True,
        )
        options_by_name = {
            str(getattr(option, "name", "")): option
            for option in response.data
            if getattr(option, "name", None)
        }
        results: list[AddonCompatibility] = []
        for addon in installed:
            option = options_by_name.get(addon.name)
            supported = tuple(
                sorted(
                    {
                        str(getattr(version, "version_number", ""))
                        for version in (
                            getattr(option, "versions", None) or ()
                        )
                        if (
                            getattr(version, "version_number", None)
                            and (
                                not getattr(version, "status", None)
                                or str(
                                    getattr(version, "status", "")
                                ).upper()
                                == "ACTIVE"
                            )
                            and _addon_version_supports_kubernetes(
                                version,
                                target_version,
                            )
                        )
                    }
                )
            )
            automatic = (addon.update_mode or "").upper() in {
                "AUTOMATIC",
                "AUTO",
            }
            selected_version = addon.selected_version or (
                addon.version if not automatic else None
            )
            lifecycle_ready = (
                not addon.lifecycle_state
                or addon.lifecycle_state.upper() == "ACTIVE"
            )
            compatible = (
                bool(option)
                and bool(supported)
                and lifecycle_ready
                and (
                    automatic
                    or selected_version is None
                    or selected_version in supported
                )
            )
            reason = None
            if option is None:
                reason = f"Add-on is not offered for Kubernetes {target_version}."
            elif not supported:
                reason = (
                    f"Add-on has no supported version for Kubernetes "
                    f"{target_version}."
                )
            elif not lifecycle_ready:
                reason = (
                    f"Add-on lifecycle state is "
                    f"{addon.lifecycle_state or 'UNKNOWN'}."
                )
            elif (
                not automatic
                and selected_version
                and selected_version not in supported
            ):
                reason = (
                    f"Pinned version {selected_version} is not supported for "
                    f"Kubernetes {target_version}."
                )
            results.append(
                AddonCompatibility(
                    name=addon.name,
                    installed_version=addon.version,
                    update_mode=addon.update_mode,
                    supported_versions=supported,
                    compatible=compatible,
                    reason=reason,
                )
            )
        return results

    def upgrade_control_plane(
        self,
        cluster_id: str,
        target_version: str,
        etag: str,
    ) -> str | None:
        if not etag:
            raise OciDiscoveryError(
                "Control-plane upgrade requires the current OKE cluster ETag."
            )
        details = self.oci.container_engine.models.UpdateClusterDetails(
            kubernetes_version=target_version,
        )
        response = self._call(
            "OKE control-plane upgrade",
            self.container_engine.update_cluster,
            cluster_id,
            details,
            if_match=etag,
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def get_work_request_status(
        self,
        work_request_id: str,
        compartment_id: str | None = None,
    ) -> WorkRequestInfo:
        is_oke_work_request = work_request_id.startswith("ocid1.clustersworkrequest")
        client = self.container_engine if is_oke_work_request else self.work_requests
        response = self._call(
            "OCI work request lookup",
            client.get_work_request,
            work_request_id,
        )
        status = str(getattr(response.data, "status", "UNKNOWN"))
        percent = getattr(response.data, "percent_complete", None)
        errors: tuple[str, ...] = ()
        if status.upper() in {"FAILED", "CANCELED", "CANCELLED"}:
            error_args: tuple[str, ...]
            if is_oke_work_request:
                if not compartment_id:
                    raise OciDiscoveryError(
                        "OKE work request error lookup requires the compartment OCID."
                    )
                error_args = (compartment_id, work_request_id)
            else:
                error_args = (work_request_id,)
            error_response = self._call(
                "OCI work request error lookup",
                self.oci.pagination.list_call_get_all_results,
                client.list_work_request_errors,
                *error_args,
            )
            errors = tuple(
                _work_request_error(error) for error in error_response.data
            )
        return WorkRequestInfo(
            work_request_id=work_request_id,
            status=status,
            percent_complete=float(percent) if percent is not None else None,
            errors=errors,
        )

    def list_resource_work_requests(
        self,
        compartment_id: str,
        resource_id: str,
    ) -> list[WorkRequestInfo]:
        response = self._call(
            "OCI resource work request listing",
            self.oci.pagination.list_call_get_all_results,
            self.work_requests.list_work_requests,
            compartment_id,
            resource_id=resource_id,
        )
        work_requests: list[WorkRequestInfo] = []
        for summary in response.data:
            work_request_id = getattr(summary, "id", None)
            if not isinstance(work_request_id, str) or not work_request_id:
                continue
            percent = getattr(summary, "percent_complete", None)
            work_requests.append(
                WorkRequestInfo(
                    work_request_id=work_request_id,
                    status=str(getattr(summary, "status", "UNKNOWN")),
                    percent_complete=float(percent) if percent is not None else None,
                )
            )
        return work_requests

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

    def get_compute_cluster_info(
        self,
        compute_cluster_id: str,
    ) -> ComputeClusterInfo:
        response = self._call(
            "Compute Cluster lookup",
            self.compute.get_compute_cluster,
            compute_cluster_id,
        )
        return _compute_cluster_info(response.data)

    def resolve_availability_domain(
        self,
        compartment_id: str,
        requested_name: str,
    ) -> str:
        requested = requested_name.strip()
        if not requested:
            raise OciDiscoveryError("Availability domain cannot be empty.")
        if ":" in requested:
            return requested

        response = self._call(
            "Availability domain lookup",
            self.identity.list_availability_domains,
            compartment_id,
        )
        names = tuple(
            str(name)
            for item in response.data
            if (name := getattr(item, "name", None))
        )
        requested_folded = requested.casefold()
        matches = tuple(
            name
            for name in names
            if name.casefold() == requested_folded
            or name.rsplit(":", 1)[-1].casefold() == requested_folded
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise OciDiscoveryError(
                f"Availability domain name is ambiguous: {requested}. "
                "Use its canonical tenancy-prefixed name."
            )
        available = ", ".join(sorted(names)) or "none visible"
        raise OciDiscoveryError(
            f"Availability domain was not found: {requested}. Available: "
            f"{available}."
        )

    def create_compute_cluster(
        self,
        *,
        compartment_id: str,
        availability_domain: str,
        display_name: str,
        pool_name: str,
        freeform_tags: dict[str, str] | None = None,
        opc_retry_token: str | None = None,
    ) -> ComputeClusterInfo:
        tags = dict(freeform_tags or {})
        tags.update(
            {
                "mgmt-oke-created": "true",
                "mgmt-oke-pool": pool_name,
            }
        )
        details = self.oci.core.models.CreateComputeClusterDetails(
            availability_domain=availability_domain,
            compartment_id=compartment_id,
            display_name=display_name,
            freeform_tags=tags,
        )
        response = self._call(
            "Compute Cluster creation",
            self.compute.create_compute_cluster,
            details,
            opc_retry_token=opc_retry_token or str(uuid.uuid4()),
        )
        return _compute_cluster_info(response.data)

    def preview_managed_node_pool_create(
        self,
        source_node_pool_id: str,
        cluster_id: str,
        compartment_id: str,
        name: str,
        size: int,
        spec: PoolCreateSpec,
        *,
        bootstrap_metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        details = self._build_managed_node_pool_create_details(
            source_node_pool_id,
            cluster_id,
            compartment_id,
            name,
            size,
            spec,
            bootstrap_metadata=bootstrap_metadata,
        )
        return _managed_node_pool_create_preview(details, spec)

    def create_managed_node_pool(
        self,
        source_node_pool_id: str,
        cluster_id: str,
        compartment_id: str,
        name: str,
        size: int,
        spec: PoolCreateSpec,
        *,
        bootstrap_metadata: Mapping[str, str] | None = None,
        opc_retry_token: str | None = None,
    ) -> ManagedNodePoolCreateResult:
        if spec.creates_compute_cluster:
            raise OciDiscoveryError(
                "Create the dedicated Compute Cluster before submitting the "
                "managed node-pool request."
            )
        details = self._build_managed_node_pool_create_details(
            source_node_pool_id,
            cluster_id,
            compartment_id,
            name,
            size,
            spec,
            bootstrap_metadata=bootstrap_metadata,
        )
        response = self._call(
            "Managed OKE node pool creation",
            self.container_engine.create_node_pool,
            details,
            opc_retry_token=opc_retry_token or str(uuid.uuid4()),
        )
        headers = getattr(response, "headers", None) or {}
        node_config = getattr(details, "node_config_details", None)
        return ManagedNodePoolCreateResult(
            node_pool_id=getattr(
                getattr(response, "data", None),
                "id",
                None,
            ),
            work_request_id=headers.get("opc-work-request-id"),
            compute_cluster_id=getattr(
                node_config,
                "compute_cluster_id",
                None,
            ),
            host_group_id=spec.host_group_id,
        )

    def _build_managed_node_pool_create_details(
        self,
        source_node_pool_id: str,
        cluster_id: str,
        compartment_id: str,
        name: str,
        size: int,
        spec: PoolCreateSpec,
        *,
        bootstrap_metadata: Mapping[str, str] | None = None,
    ) -> Any:
        spec = self._with_resolved_availability_domain(
            compartment_id,
            spec,
        )
        try:
            normalized_name = normalize_pool_name(name)
        except ValueError as exc:
            raise OciDiscoveryError(str(exc)) from exc
        if size < 1:
            raise OciDiscoveryError("Managed node pool size must be at least one.")
        if spec.pool_type not in {"cpu", "gpu"} and not spec.uses_compute_cluster:
            raise OciDiscoveryError(
                "Managed node-pool creation requires pool type cpu or gpu, or "
                "RDMA mode compute-cluster."
            )

        source = self._call(
            "Source managed OKE node pool lookup",
            self.container_engine.get_node_pool,
            source_node_pool_id,
        ).data
        source_cluster_id = getattr(source, "cluster_id", None)
        if source_cluster_id and source_cluster_id != cluster_id:
            raise OciDiscoveryError(
                "The source managed node pool belongs to a different OKE cluster."
            )
        lifecycle_state = str(
            getattr(source, "lifecycle_state", "") or ""
        ).upper()
        if lifecycle_state and lifecycle_state not in {"ACTIVE", "RUNNING"}:
            raise OciDiscoveryError(
                f"Source managed node pool is not active: {lifecycle_state}"
            )

        source_config = getattr(source, "node_config_details", None)
        if source_config is None:
            raise OciDiscoveryError(
                "Source managed node pool does not expose node_config_details."
            )
        placement_configs = _build_managed_placement_configs(
            self.oci,
            source_config,
            spec,
        )
        pod_network = _build_managed_pod_network(
            self.oci,
            source_config,
            spec,
        )

        shape = spec.shape or getattr(source, "node_shape", None)
        if not shape:
            raise OciDiscoveryError(
                "Source managed node pool does not expose its shape."
            )
        _validate_shape_for_pool_type(shape, spec.pool_type)
        shape_config = _build_managed_shape_config(self.oci, source, shape, spec)

        source_details = getattr(source, "node_source_details", None)
        image_id = spec.image_id or getattr(source_details, "image_id", None)
        boot_size = spec.boot_volume_size_in_gbs or getattr(
            source_details,
            "boot_volume_size_in_gbs",
            None,
        )
        if not image_id:
            raise OciDiscoveryError(
                "Source managed node pool does not expose an image OCID."
            )
        node_source = self.oci.container_engine.models.NodeSourceViaImageDetails(
            source_type="IMAGE",
            image_id=image_id,
            boot_volume_size_in_gbs=boot_size,
        )

        freeform_tags = _clone_pool_freeform_tags(
            getattr(source, "freeform_tags", None),
            normalized_name,
        )
        freeform_tags.update(dict(spec.freeform_tags))
        node_config_freeform_tags = _clone_pool_freeform_tags(
            getattr(source_config, "freeform_tags", None),
            normalized_name,
        )
        node_config_freeform_tags.update(dict(spec.freeform_tags))
        source_compute_cluster_id = getattr(
            source_config,
            "compute_cluster_id",
            None,
        )
        effective_compute_cluster_id = spec.compute_cluster_id
        if (
            not effective_compute_cluster_id
            and not spec.creates_compute_cluster
            and source_compute_cluster_id
        ):
            effective_compute_cluster_id = source_compute_cluster_id

        node_config_values: dict[str, Any] = {
            "size": size,
            "nsg_ids": (
                list(spec.node_nsg_ids)
                if spec.node_nsg_ids
                else list(getattr(source_config, "nsg_ids", None) or [])
            ),
            "kms_key_id": (
                spec.boot_volume_kms_key_id
                or getattr(source_config, "kms_key_id", None)
            ),
            "is_pv_encryption_in_transit_enabled": (
                spec.pv_encryption_in_transit
                if spec.pv_encryption_in_transit is not None
                else getattr(
                    source_config,
                    "is_pv_encryption_in_transit_enabled",
                    None,
                )
            ),
            "freeform_tags": node_config_freeform_tags,
            "defined_tags": deepcopy(
                getattr(source_config, "defined_tags", None)
            ),
            "placement_configs": placement_configs,
            "node_pool_pod_network_option_details": pod_network,
        }
        node_config_model = (
            self.oci.container_engine.models.CreateNodePoolNodeConfigDetails
        )
        if effective_compute_cluster_id:
            if not _sdk_model_supports_field(
                node_config_model,
                "compute_cluster_id",
            ):
                raise OciDiscoveryError(
                    "The installed OCI Python SDK cannot create managed Compute "
                    "Cluster node pools. Upgrade the OCI SDK to the version "
                    "declared by this project."
                )
            node_config_values["compute_cluster_id"] = (
                effective_compute_cluster_id
            )
        node_config = node_config_model(**node_config_values)

        metadata = _string_metadata(
            getattr(source, "node_metadata", None),
            source="Managed OKE source pool",
        )
        if bootstrap_metadata is not None:
            metadata = _merge_legacy_bootstrap_metadata(
                metadata,
                bootstrap_metadata,
            )
        source_user_data = metadata.get("user_data")
        if not source_user_data:
            raise OciDiscoveryError(
                "Source managed node pool is missing OKE worker cloud-init."
            )
        if _requires_user_data_composition(spec):
            metadata["user_data"] = compose_worker_user_data(
                source_user_data,
                spec,
            )
        if spec.pre_bootstrap_script is not None:
            metadata["pre_oke"] = base64.b64encode(
                spec.pre_bootstrap_script
            ).decode("ascii")
        if spec.post_bootstrap_script is not None:
            metadata["post_oke"] = base64.b64encode(
                spec.post_bootstrap_script
            ).decode("ascii")
        if spec.kubelet_extra_args is not None:
            metadata["kubelet-extra-args"] = spec.kubelet_extra_args
        if spec.legacy_imds_endpoints_disabled is not None:
            metadata["areLegacyImdsEndpointsDisabled"] = (
                "true" if spec.legacy_imds_endpoints_disabled else "false"
            )
        metadata.update(dict(spec.node_metadata))

        labels = _retarget_managed_node_labels(
            self.oci,
            getattr(source, "initial_node_labels", None),
            normalized_name,
            spec.node_labels,
        )
        cycling = _build_node_cycling_details(self.oci, source, spec)
        eviction = _build_node_eviction_settings(self.oci, source, spec)
        ssh_public_key = (
            spec.ssh_public_key
            or getattr(source, "ssh_public_key", None)
        )
        kubernetes_version = (
            spec.kubernetes_version
            or getattr(source, "kubernetes_version", None)
        )
        if not kubernetes_version:
            raise OciDiscoveryError(
                "Source managed node pool does not expose a Kubernetes version."
            )

        shape_targets_by_ad = self._validate_create_compatibility(
            compartment_id=compartment_id,
            shape=shape,
            image_id=image_id,
            availability_domains=tuple(
                getattr(placement, "availability_domain", "")
                for placement in placement_configs
            ),
            source_subnet_id=getattr(
                next(
                    iter(
                        getattr(source_config, "placement_configs", None)
                        or ()
                    ),
                    None,
                ),
                "subnet_id",
                None,
            ),
            primary_subnet_ids=tuple(
                getattr(placement, "subnet_id", "")
                for placement in placement_configs
            ),
            pod_subnet_ids=tuple(
                getattr(pod_network, "pod_subnet_ids", None) or ()
            ),
            nsg_ids=tuple(
                dict.fromkeys(
                    [
                        *(getattr(node_config, "nsg_ids", None) or []),
                        *(getattr(pod_network, "pod_nsg_ids", None) or []),
                    ]
                )
            ),
            require_local_nvme=spec.nvme_raid is not None,
            require_rdma=spec.uses_compute_cluster,
        )
        self._validate_managed_placement(
            shape=shape,
            placements=tuple(placement_configs),
            compute_cluster_id=effective_compute_cluster_id,
            create_compute_cluster=spec.creates_compute_cluster,
            host_group_id=spec.host_group_id,
            shape_targets_by_ad=shape_targets_by_ad,
        )

        return self.oci.container_engine.models.CreateNodePoolDetails(
            compartment_id=compartment_id,
            cluster_id=cluster_id,
            name=normalized_name,
            kubernetes_version=kubernetes_version,
            node_metadata=metadata,
            node_source_details=node_source,
            node_shape=shape,
            node_shape_config=shape_config,
            initial_node_labels=labels,
            ssh_public_key=ssh_public_key,
            node_config_details=node_config,
            freeform_tags=freeform_tags,
            defined_tags=deepcopy(getattr(source, "defined_tags", None)),
            node_eviction_node_pool_settings=eviction,
            node_pool_cycling_details=cycling,
        )

    def _validate_create_compatibility(
        self,
        *,
        compartment_id: str,
        shape: str,
        image_id: str,
        availability_domains: tuple[str, ...],
        source_subnet_id: str | None,
        primary_subnet_ids: tuple[str, ...],
        pod_subnet_ids: tuple[str, ...],
        nsg_ids: tuple[str, ...],
        require_local_nvme: bool,
        require_rdma: bool = False,
    ) -> dict[str, frozenset[str]]:
        shape_targets_by_ad: dict[str, frozenset[str]] = {}
        for availability_domain in dict.fromkeys(availability_domains):
            if not availability_domain:
                raise OciDiscoveryError(
                    "Pool creation requires an availability domain."
                )
            response = self._call(
                "OCI shape and image compatibility lookup",
                self.oci.pagination.list_call_get_all_results,
                self.compute.list_shapes,
                compartment_id,
                availability_domain=availability_domain,
                image_id=image_id,
            )
            matches = [
                candidate
                for candidate in response.data
                if getattr(candidate, "shape", None) == shape
            ]
            if not matches:
                raise OciDiscoveryError(
                    f"Image {image_id} does not advertise shape {shape} in "
                    f"{availability_domain}."
                )
            if require_local_nvme and not any(
                int(getattr(candidate, "local_disks", 0) or 0) > 0
                for candidate in matches
            ):
                raise OciDiscoveryError(
                    f"NVMe RAID was requested, but shape {shape} does not "
                    "advertise local disks."
                )
            if require_rdma and not any(
                int(getattr(candidate, "rdma_ports", 0) or 0) > 0
                for candidate in matches
            ):
                raise OciDiscoveryError(
                    "Managed Compute Cluster RDMA was requested, but shape "
                    f"{shape} does not advertise RDMA ports in "
                    f"{availability_domain}."
                )
            shape_targets_by_ad[availability_domain] = frozenset(
                {
                    shape,
                    *(
                        str(platform_name)
                        for candidate in matches
                        for platform_name in (
                            getattr(candidate, "platform_names", None) or ()
                        )
                        if platform_name
                    ),
                }
            )

        source_vcn_id: str | None = None
        if source_subnet_id:
            source_subnet = self._call(
                "Source worker subnet lookup",
                self.virtual_network.get_subnet,
                source_subnet_id,
            ).data
            source_vcn_id = getattr(source_subnet, "vcn_id", None)
        target_subnet_ids = tuple(
            dict.fromkeys(
                [
                    *primary_subnet_ids,
                    *pod_subnet_ids,
                ]
            )
        )
        for subnet_id in target_subnet_ids:
            if not subnet_id:
                continue
            subnet = self._call(
                "Worker subnet lookup",
                self.virtual_network.get_subnet,
                subnet_id,
            ).data
            if source_vcn_id and getattr(subnet, "vcn_id", None) != source_vcn_id:
                raise OciDiscoveryError(
                    f"Subnet {subnet_id} is not in the source worker pool VCN."
                )
            subnet_ad = getattr(subnet, "availability_domain", None)
            if subnet_ad and subnet_ad not in availability_domains:
                raise OciDiscoveryError(
                    f"Subnet {subnet_id} is scoped to {subnet_ad}, which is not "
                    "one of the selected availability domains."
                )
        for nsg_id in nsg_ids:
            nsg = self._call(
                "Worker network security group lookup",
                self.virtual_network.get_network_security_group,
                nsg_id,
            ).data
            if source_vcn_id and getattr(nsg, "vcn_id", None) != source_vcn_id:
                raise OciDiscoveryError(
                    f"Network security group {nsg_id} is not in the source "
                    "worker pool VCN."
                )
        return shape_targets_by_ad

    def _validate_managed_placement(
        self,
        *,
        shape: str,
        placements: tuple[Any, ...],
        compute_cluster_id: str | None,
        create_compute_cluster: bool,
        host_group_id: str | None,
        shape_targets_by_ad: dict[str, frozenset[str]],
    ) -> None:
        if not placements:
            raise OciDiscoveryError(
                "Managed node-pool creation requires a placement configuration."
            )
        availability_domains = {
            str(getattr(placement, "availability_domain", "") or "")
            for placement in placements
        }
        availability_domains.discard("")

        if compute_cluster_id or create_compute_cluster:
            if len(placements) != 1 or len(availability_domains) != 1:
                raise OciDiscoveryError(
                    "Compute Cluster-backed node pools require exactly one "
                    "placement configuration in one availability domain."
                )
            if any(
                getattr(placement, "fault_domains", None)
                for placement in placements
            ):
                raise OciDiscoveryError(
                    "Compute Cluster-backed node pools cannot specify fault domains."
                )
            if compute_cluster_id:
                cluster = self.get_compute_cluster_info(compute_cluster_id)
                if cluster.lifecycle_state.upper() != "ACTIVE":
                    raise OciDiscoveryError(
                        f"Compute Cluster is not ACTIVE: "
                        f"{cluster.lifecycle_state or 'UNKNOWN'}"
                    )
                selected_ad = next(iter(availability_domains))
                if cluster.availability_domain != selected_ad:
                    raise OciDiscoveryError(
                        f"Compute Cluster is in {cluster.availability_domain}, "
                        f"but the node-pool placement uses {selected_ad}."
                    )

        if not host_group_id:
            return
        if len(placements) != 1 or len(availability_domains) != 1:
            raise OciDiscoveryError(
                "One --host-group-id requires exactly one managed node-pool "
                "placement configuration. Select its availability domain with "
                "--availability-domain."
            )
        get_host_group = getattr(self.compute, "get_compute_host_group", None)
        if get_host_group is None:
            raise OciDiscoveryError(
                "The installed OCI Python SDK cannot inspect Compute Host "
                "Groups. Upgrade the OCI SDK to the version declared by this "
                "project."
            )
        response = self._call(
            "Compute Host Group lookup",
            get_host_group,
            host_group_id,
        )
        host_group = response.data
        lifecycle_state = str(
            getattr(host_group, "lifecycle_state", "") or ""
        ).upper()
        if lifecycle_state != "ACTIVE":
            raise OciDiscoveryError(
                f"Compute Host Group is not ACTIVE: "
                f"{lifecycle_state or 'UNKNOWN'}"
            )
        selected_ad = next(iter(availability_domains))
        host_group_ad = str(
            getattr(host_group, "availability_domain", "") or ""
        )
        if host_group_ad != selected_ad:
            raise OciDiscoveryError(
                f"Compute Host Group is in {host_group_ad or 'an unknown AD'}, "
                f"but the node-pool placement uses {selected_ad}."
            )
        configurations = list(
            getattr(host_group, "configurations", None) or []
        )
        valid_targets = {
            str(getattr(configuration, "target", "") or "")
            for configuration in configurations
            if str(
                getattr(configuration, "state", "VALID") or "VALID"
            ).upper()
            == "VALID"
        }
        valid_targets.discard("")
        compatible_targets = shape_targets_by_ad.get(
            selected_ad,
            frozenset({shape}),
        )
        if not {
            target.casefold() for target in valid_targets
        }.intersection(
            target.casefold() for target in compatible_targets
        ):
            targets = ", ".join(sorted(valid_targets)) or "none"
            raise OciDiscoveryError(
                f"Compute Host Group does not expose a VALID configuration "
                f"for shape {shape} or one of its OCI platforms. Reported "
                f"targets: {targets}."
            )

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

    def create_cluster_network_pool(
        self,
        source_cluster_network_id: str,
        source_instance_pool_id: str,
        name: str,
        size: int,
        spec: PoolCreateSpec | None = None,
        *,
        instance_configuration_retry_token: str | None = None,
        cluster_network_retry_token: str | None = None,
    ) -> ClusterNetworkCreateResult:
        spec = spec or PoolCreateSpec(pool_type="rdma")
        try:
            normalized_name = normalize_pool_name(name)
        except ValueError as exc:
            raise OciDiscoveryError(str(exc)) from exc
        if size < 1:
            raise OciDiscoveryError("Cluster Network pool size must be at least one.")

        source, source_pool, placement = self._get_cluster_network_pool_source(
            source_cluster_network_id,
            source_instance_pool_id,
        )
        compartment_id = source.compartment_id
        source_instance_configuration = self._get_instance_configuration_template(
            source_pool.instance_configuration_id
        )
        instance_configuration_details = self._build_instance_configuration_details(
            source_instance_configuration,
            compartment_id,
            normalized_name,
            spec,
        )
        instance_configuration_response = self._call(
            "Instance Configuration creation",
            self.compute_mgmt.create_instance_configuration,
            instance_configuration_details,
            opc_retry_token=(
                instance_configuration_retry_token
                or str(uuid.uuid4())
            ),
        )
        instance_configuration_id = getattr(
            instance_configuration_response.data,
            "id",
            None,
        )
        if not instance_configuration_id:
            raise OciDiscoveryError(
                "Instance Configuration creation did not return the new resource OCID."
            )
        return self._create_cluster_network_with_configuration(
            source,
            source_pool,
            placement,
            normalized_name,
            size,
            str(instance_configuration_id),
            spec,
            opc_retry_token=cluster_network_retry_token,
        )

    def get_cluster_network_pool_bootstrap_metadata(
        self,
        source_cluster_network_id: str,
        source_instance_pool_id: str,
    ) -> dict[str, str]:
        _source, source_pool, _placement = self._get_cluster_network_pool_source(
            source_cluster_network_id,
            source_instance_pool_id,
        )
        source_configuration = self._get_instance_configuration_template(
            source_pool.instance_configuration_id
        )
        instance_details = getattr(source_configuration, "instance_details", None)
        launch_details = getattr(instance_details, "launch_details", None)
        if launch_details is None:
            raise OciDiscoveryError(
                "Legacy bootstrap source Instance Configuration does not expose "
                "compute launch details."
            )
        return _required_oke_bootstrap_metadata(
            getattr(launch_details, "metadata", None),
            source="Legacy bootstrap source Instance Configuration",
        )

    def _create_cluster_network_with_configuration(
        self,
        source: Any,
        source_pool: Any,
        placement: Any,
        name: str,
        size: int,
        instance_configuration_id: str,
        spec: PoolCreateSpec,
        *,
        opc_retry_token: str | None = None,
    ) -> ClusterNetworkCreateResult:
        primary_vnic_subnets = (
            None
            if spec.primary_subnet_id
            else getattr(placement, "primary_vnic_subnets", None)
        )
        placement_details = self.oci.core.models.ClusterNetworkPlacementConfigurationDetails(
            availability_domain=(
                spec.availability_domain or placement.availability_domain
            ),
            placement_constraint=(
                spec.placement_constraint or placement.placement_constraint
            ),
            primary_subnet_id=(
                spec.primary_subnet_id
                or (
                    None
                    if primary_vnic_subnets
                    else getattr(placement, "primary_subnet_id", None)
                )
            ),
            primary_vnic_subnets=primary_vnic_subnets,
            secondary_vnic_subnets=getattr(placement, "secondary_vnic_subnets", None),
        )
        instance_pool_details = (
            self.oci.core.models.CreateClusterNetworkInstancePoolDetails(
                display_name=name,
                freeform_tags=_clone_pool_freeform_tags(
                    getattr(source_pool, "freeform_tags", None),
                    name,
                ),
                instance_configuration_id=instance_configuration_id,
                size=size,
            )
        )
        create_details = self.oci.core.models.CreateClusterNetworkDetails(
            compartment_id=getattr(source, "compartment_id", None),
            display_name=name,
            freeform_tags=_clone_pool_freeform_tags(
                getattr(source, "freeform_tags", None),
                name,
            ),
            instance_pools=[instance_pool_details],
            placement_configuration=placement_details,
        )
        create_details.freeform_tags.update(dict(spec.freeform_tags))
        instance_pool_details.freeform_tags.update(dict(spec.freeform_tags))
        try:
            response = self._call(
                "Cluster Network pool creation",
                self.compute_mgmt.create_cluster_network,
                create_details,
                opc_retry_token=opc_retry_token or str(uuid.uuid4()),
            )
        except OciDiscoveryError as exc:
            raise OciDiscoveryError(
                f"{exc} The derived Instance Configuration was created as "
                f"{instance_configuration_id}; verify whether a Cluster Network "
                "was created before removing it."
            ) from exc
        created = response.data
        cluster_network_id = getattr(created, "id", None)
        if not cluster_network_id:
            raise OciDiscoveryError(
                "Cluster Network creation did not return the new resource OCID. "
                f"The derived Instance Configuration is {instance_configuration_id}."
            )
        created_pools = list(getattr(created, "instance_pools", None) or [])
        headers = getattr(response, "headers", None) or {}
        return ClusterNetworkCreateResult(
            cluster_network_id=str(cluster_network_id),
            instance_configuration_id=instance_configuration_id,
            instance_pool_id=(
                getattr(created_pools[0], "id", None) if created_pools else None
            ),
            work_request_id=headers.get("opc-work-request-id"),
        )

    def preview_cluster_network_pool_create(
        self,
        source_cluster_network_id: str,
        source_instance_pool_id: str,
        name: str,
        size: int,
        spec: PoolCreateSpec,
    ) -> dict[str, Any]:
        try:
            normalized_name = normalize_pool_name(name)
        except ValueError as exc:
            raise OciDiscoveryError(str(exc)) from exc
        if size < 1:
            raise OciDiscoveryError("Cluster Network pool size must be at least one.")
        source, source_pool, placement = self._get_cluster_network_pool_source(
            source_cluster_network_id,
            source_instance_pool_id,
        )
        source_instance_configuration = self._get_instance_configuration_template(
            source_pool.instance_configuration_id
        )
        details = self._build_instance_configuration_details(
            source_instance_configuration,
            source.compartment_id,
            normalized_name,
            spec,
        )
        launch = details.instance_details.launch_details
        source_details = getattr(launch, "source_details", None)
        vnic = getattr(launch, "create_vnic_details", None)
        metadata = dict(getattr(launch, "metadata", None) or {})
        return {
            "backend": "cluster-network",
            "name": normalized_name,
            "count": size,
            "shape": getattr(launch, "shape", None),
            "image_id": getattr(source_details, "image_id", None),
            "boot_volume_size_in_gbs": getattr(
                source_details,
                "boot_volume_size_in_gbs",
                None,
            ),
            "boot_volume_vpus_per_gb": getattr(
                source_details,
                "boot_volume_vpus_per_gb",
                None,
            ),
            "availability_domain": (
                spec.availability_domain
                or getattr(placement, "availability_domain", None)
            ),
            "placement_constraint": (
                spec.placement_constraint
                or getattr(placement, "placement_constraint", None)
            ),
            "primary_subnet_id": (
                spec.primary_subnet_id
                or getattr(placement, "primary_subnet_id", None)
                or getattr(vnic, "subnet_id", None)
            ),
            "node_nsg_ids": list(getattr(vnic, "nsg_ids", None) or []),
            "pod_subnet_ids": _split_metadata_values(
                metadata.get("pod-subnets")
            ),
            "pod_nsg_ids": _split_metadata_values(metadata.get("pod-nsgids")),
            "kubernetes_version": metadata.get("oke-k8version"),
            "max_pods_per_node": _optional_int(
                metadata.get("oke-max-pods")
            ),
            "worker_bootstrap": _worker_bootstrap_preview(metadata),
            "storage": _storage_preview(spec),
        }

    def validate_cluster_network_pool_template(
        self,
        source_cluster_network_id: str,
        source_instance_pool_id: str,
        name: str,
    ) -> None:
        try:
            normalized_name = normalize_pool_name(name)
        except ValueError as exc:
            raise OciDiscoveryError(str(exc)) from exc
        source, source_pool, _placement = self._get_cluster_network_pool_source(
            source_cluster_network_id,
            source_instance_pool_id,
        )
        source_instance_configuration = self._get_instance_configuration_template(
            source_pool.instance_configuration_id
        )
        self._build_instance_configuration_details(
            source_instance_configuration,
            source.compartment_id,
            normalized_name,
            PoolCreateSpec(pool_type="rdma"),
        )

    def _get_instance_configuration_template(
        self,
        instance_configuration_id: str,
    ) -> Any:
        source = self._call(
            "Source Instance Configuration lookup",
            self.compute_mgmt.get_instance_configuration,
            instance_configuration_id,
        ).data
        if getattr(source, "deferred_fields", None):
            raise OciDiscoveryError(
                "Source Instance Configuration contains deferred fields and cannot "
                "be cloned safely."
            )
        return source

    def _build_instance_configuration_details(
        self,
        source: Any,
        compartment_id: str,
        name: str,
        spec: PoolCreateSpec | None = None,
    ) -> Any:
        spec = spec or PoolCreateSpec(pool_type="rdma")
        spec = self._with_resolved_availability_domain(
            compartment_id,
            spec,
        )
        if spec.pool_type != "rdma":
            raise OciDiscoveryError(
                "Cluster Network creation requires pool type rdma."
            )
        instance_details = deepcopy(getattr(source, "instance_details", None))
        launch_details = getattr(instance_details, "launch_details", None)
        if launch_details is None:
            raise OciDiscoveryError(
                "Source Instance Configuration does not expose compute launch details."
            )

        metadata = _required_oke_bootstrap_metadata(
            getattr(launch_details, "metadata", None),
            source="Source Instance Configuration",
        )
        source_cni = (
            "OCI_VCN_IP_NATIVE"
            if _metadata_truthy(metadata.get("oke-native-pod-networking"))
            else "FLANNEL_OVERLAY"
        )
        if spec.cni_type and spec.cni_type != source_cni:
            raise OciDiscoveryError(
                f"Requested CNI {spec.cni_type} does not match the inherited "
                f"OKE bootstrap CNI {source_cni}."
            )

        metadata["oke-initial-node-labels"] = _retarget_oke_node_labels(
            metadata["oke-initial-node-labels"],
            name,
            spec.node_labels,
        )
        if spec.kubernetes_version:
            metadata["oke-k8version"] = spec.kubernetes_version
        if spec.max_pods_per_node is not None:
            metadata["oke-max-pods"] = str(spec.max_pods_per_node)
        if spec.pod_subnet_ids:
            metadata["pod-subnets"] = ",".join(spec.pod_subnet_ids)
            metadata["oke-native-pod-networking"] = "true"
        if spec.pod_nsg_ids:
            metadata["pod-nsgids"] = ",".join(spec.pod_nsg_ids)
        if spec.ssh_public_key:
            metadata["ssh_authorized_keys"] = spec.ssh_public_key
        if spec.pre_bootstrap_script is not None:
            metadata["pre_oke"] = base64.b64encode(
                spec.pre_bootstrap_script
            ).decode("ascii")
        if spec.post_bootstrap_script is not None:
            metadata["post_oke"] = base64.b64encode(
                spec.post_bootstrap_script
            ).decode("ascii")
        if spec.kubelet_extra_args is not None:
            metadata["kubelet-extra-args"] = spec.kubelet_extra_args
        if _requires_user_data_composition(spec):
            metadata["user_data"] = compose_worker_user_data(
                metadata["user_data"],
                spec,
            )
        metadata.update(dict(spec.node_metadata))
        launch_details.metadata = metadata
        launch_details.display_name = name
        launch_details.freeform_tags = _clone_pool_freeform_tags(
            getattr(launch_details, "freeform_tags", None),
            name,
        )
        launch_details.freeform_tags.update(dict(spec.freeform_tags))
        shape = spec.shape or getattr(launch_details, "shape", None)
        if not shape:
            raise OciDiscoveryError(
                "Source Instance Configuration does not expose its shape."
            )
        _validate_shape_for_pool_type(shape, "rdma")
        launch_details.shape = shape
        if spec.availability_domain:
            launch_details.availability_domain = spec.availability_domain
        if spec.capacity_reservation_id:
            launch_details.capacity_reservation_id = (
                spec.capacity_reservation_id
            )
        if spec.fault_domains:
            if len(spec.fault_domains) > 1:
                raise OciDiscoveryError(
                    "A self-managed RDMA launch configuration accepts one fault domain."
                )
            launch_details.fault_domain = spec.fault_domains[0]
        if spec.pv_encryption_in_transit is not None:
            launch_details.is_pv_encryption_in_transit_enabled = (
                spec.pv_encryption_in_transit
            )
        if spec.legacy_imds_endpoints_disabled is not None:
            options = getattr(launch_details, "instance_options", None)
            if options is None:
                options = self.oci.core.models.InstanceConfigurationInstanceOptions()
                launch_details.instance_options = options
            options.are_legacy_imds_endpoints_disabled = (
                spec.legacy_imds_endpoints_disabled
            )
        _apply_rdma_shape_config(self.oci, launch_details, shape, spec)
        _apply_rdma_source_details(launch_details, spec)

        primary_vnic = getattr(launch_details, "create_vnic_details", None)
        if primary_vnic is None:
            raise OciDiscoveryError(
                "Source Instance Configuration does not expose primary VNIC details."
            )
        source_subnet_id = getattr(primary_vnic, "subnet_id", None)
        if spec.primary_subnet_id:
            primary_vnic.subnet_id = spec.primary_subnet_id
        if spec.node_nsg_ids:
            primary_vnic.nsg_ids = list(spec.node_nsg_ids)
        if spec.assign_public_ip is not None:
            primary_vnic.assign_public_ip = spec.assign_public_ip
        _retarget_vnic_tags(
            primary_vnic,
            name,
        )
        primary_vnic.freeform_tags.update(dict(spec.freeform_tags))
        for secondary_vnic in list(
            getattr(instance_details, "secondary_vnics", None) or []
        ):
            secondary_details = getattr(
                secondary_vnic,
                "create_vnic_details",
                None,
            )
            _retarget_vnic_tags(
                secondary_details,
                name,
            )
            if secondary_details is not None:
                secondary_details.freeform_tags.update(
                    dict(spec.freeform_tags)
                )
        for block_volume in list(
            getattr(instance_details, "block_volumes", None) or []
        ):
            create_details = getattr(block_volume, "create_details", None)
            if create_details is not None and getattr(
                create_details,
                "freeform_tags",
                None,
            ):
                create_details.freeform_tags = _clone_pool_freeform_tags(
                    create_details.freeform_tags,
                    name,
                )

        source_details = getattr(launch_details, "source_details", None)
        image_id = getattr(source_details, "image_id", None)
        if not image_id:
            raise OciDiscoveryError(
                "Source Instance Configuration does not expose an image OCID."
            )
        selected_ad = (
            spec.availability_domain
            or getattr(launch_details, "availability_domain", None)
        )
        self._validate_create_compatibility(
            compartment_id=compartment_id,
            shape=shape,
            image_id=image_id,
            availability_domains=(selected_ad or "",),
            source_subnet_id=source_subnet_id,
            primary_subnet_ids=(getattr(primary_vnic, "subnet_id", "") or "",),
            pod_subnet_ids=tuple(
                _split_metadata_values(metadata.get("pod-subnets"))
            ),
            nsg_ids=tuple(
                dict.fromkeys(
                    [
                        *(getattr(primary_vnic, "nsg_ids", None) or []),
                        *_split_metadata_values(metadata.get("pod-nsgids")),
                    ]
                )
            ),
            require_local_nvme=spec.nvme_raid is not None,
        )

        instance_configuration_tags = _clone_pool_freeform_tags(
            getattr(source, "freeform_tags", None),
            name,
        )
        instance_configuration_tags.update(dict(spec.freeform_tags))
        return self.oci.core.models.CreateInstanceConfigurationDetails(
            compartment_id=compartment_id,
            display_name=name,
            freeform_tags=instance_configuration_tags,
            source="NONE",
            instance_details=instance_details,
        )

    def _with_resolved_availability_domain(
        self,
        compartment_id: str,
        spec: PoolCreateSpec,
    ) -> PoolCreateSpec:
        if not spec.availability_domain:
            return spec
        availability_domain = self.resolve_availability_domain(
            compartment_id,
            spec.availability_domain,
        )
        if availability_domain == spec.availability_domain:
            return spec
        return replace(
            spec,
            availability_domain=availability_domain,
        )

    def _get_cluster_network_pool_source(
        self,
        source_cluster_network_id: str,
        source_instance_pool_id: str,
    ) -> tuple[Any, Any, Any]:
        source = self._call(
            "Source Cluster Network lookup",
            self.compute_mgmt.get_cluster_network,
            source_cluster_network_id,
        ).data
        lifecycle_state = str(getattr(source, "lifecycle_state", "") or "").upper()
        if lifecycle_state and lifecycle_state not in {"ACTIVE", "RUNNING"}:
            raise OciDiscoveryError(
                f"Source Cluster Network is not running: {lifecycle_state}"
            )

        source_pools = list(getattr(source, "instance_pools", None) or [])
        source_pool = next(
            (
                pool
                for pool in source_pools
                if getattr(pool, "id", None) == source_instance_pool_id
            ),
            None,
        )
        if source_pool is None:
            raise OciDiscoveryError(
                f"Instance pool {source_instance_pool_id} is not part of "
                f"cluster network {source_cluster_network_id}."
            )

        compartment_id = getattr(source, "compartment_id", None)
        instance_configuration_id = getattr(
            source_pool,
            "instance_configuration_id",
            None,
        )
        placement = getattr(source, "placement_configuration", None)
        if not compartment_id:
            raise OciDiscoveryError(
                "Source Cluster Network does not expose its compartment OCID."
            )
        if not instance_configuration_id:
            raise OciDiscoveryError(
                "Source Cluster Network instance pool does not expose an "
                "Instance Configuration OCID."
            )
        if placement is None:
            raise OciDiscoveryError(
                "Source Cluster Network does not expose its placement configuration."
            )
        if not getattr(placement, "availability_domain", None):
            raise OciDiscoveryError(
                "Source Cluster Network placement does not expose an availability domain."
            )
        if not (
            getattr(placement, "primary_subnet_id", None)
            or getattr(placement, "primary_vnic_subnets", None)
        ):
            raise OciDiscoveryError(
                "Source Cluster Network placement does not expose primary VNIC "
                "subnet configuration."
            )
        if not getattr(placement, "placement_constraint", None):
            raise OciDiscoveryError(
                "Source Cluster Network placement does not expose its placement constraint."
            )
        return source, source_pool, placement

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

    def delete_managed_node_pool(self, node_pool_id: str) -> str | None:
        response = self._call(
            "Managed OKE node pool deletion",
            self.container_engine.delete_node_pool,
            node_pool_id,
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def terminate_cluster_network(self, cluster_network_id: str) -> str | None:
        response = self._call(
            "Cluster Network termination",
            self.compute_mgmt.terminate_cluster_network,
            cluster_network_id,
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def terminate_instance_pool(self, instance_pool_id: str) -> str | None:
        response = self._call(
            "Instance Pool termination",
            self.compute_mgmt.terminate_instance_pool,
            instance_pool_id,
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def delete_mgmt_created_instance_configuration(
        self,
        instance_configuration_id: str,
        operation_id: str | None = None,
    ) -> None:
        instance_configuration = self._call(
            "Instance Configuration ownership lookup",
            self.compute_mgmt.get_instance_configuration,
            instance_configuration_id,
        ).data
        if not _is_mgmt_oke_created(
            getattr(instance_configuration, "freeform_tags", None)
        ):
            raise OciDiscoveryError(
                "Refusing to delete an Instance Configuration that is not "
                f"tagged mgmt-oke-created=true: {instance_configuration_id}"
            )
        tags = dict(
            getattr(instance_configuration, "freeform_tags", None) or {}
        )
        if (
            operation_id is not None
            and tags.get("mgmt-oke-upgrade-operation") != operation_id
        ):
            raise OciDiscoveryError(
                "Refusing to delete an Instance Configuration that is not "
                f"owned by upgrade operation {operation_id}: "
                f"{instance_configuration_id}"
            )
        self._call(
            "Instance Configuration deletion",
            self.compute_mgmt.delete_instance_configuration,
            instance_configuration_id,
        )

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

    def get_instance_boot_volume_id(self, instance_id: str) -> str:
        instance = self._call(
            "Compute instance boot volume lookup",
            self.compute.get_instance,
            instance_id,
        ).data
        availability_domain = getattr(instance, "availability_domain", None)
        compartment_id = getattr(instance, "compartment_id", None)
        if not isinstance(availability_domain, str) or not availability_domain:
            raise OciDiscoveryError(
                f"Compute instance {instance_id} did not return its "
                "availability domain."
            )
        if not isinstance(compartment_id, str) or not compartment_id:
            raise OciDiscoveryError(
                f"Compute instance {instance_id} did not return its compartment "
                "OCID."
            )
        response = self._call(
            "Compute boot volume attachment lookup",
            self.oci.pagination.list_call_get_all_results,
            self.compute.list_boot_volume_attachments,
            availability_domain,
            compartment_id,
            instance_id=instance_id,
        )
        attached_ids: set[str] = set()
        attaching_ids: set[str] = set()
        for attachment in response.data:
            lifecycle_state = str(
                getattr(attachment, "lifecycle_state", "") or ""
            ).upper()
            boot_volume_id = getattr(attachment, "boot_volume_id", None)
            if not isinstance(boot_volume_id, str) or not boot_volume_id:
                continue
            if lifecycle_state == "ATTACHED":
                attached_ids.add(boot_volume_id)
            elif lifecycle_state == "ATTACHING":
                attaching_ids.add(boot_volume_id)
        if len(attached_ids) == 1:
            return next(iter(attached_ids))
        if not attached_ids and len(attaching_ids) == 1:
            return next(iter(attaching_ids))
        active_count = len(attached_ids | attaching_ids)
        if active_count == 0:
            raise BootVolumeAttachmentPending(
                f"Compute instance {instance_id} has no active boot volume "
                "attachment."
            )
        raise OciDiscoveryError(
            f"Compute instance {instance_id} has {active_count} "
            "active boot volume attachments; exactly one is required."
        )

    def replace_cluster_node_boot_volume(
        self,
        cluster_id: str,
        node_id: str,
        *,
        eviction_grace_duration: str,
        force_after_grace: bool,
    ) -> str | None:
        eviction = self.oci.container_engine.models.NodeEvictionSettings(
            eviction_grace_duration=eviction_grace_duration,
            is_force_action_after_grace_duration=force_after_grace,
        )
        details = (
            self.oci.container_engine.models.ReplaceBootVolumeClusterNodeDetails(
                node_eviction_settings=eviction,
            )
        )
        response = self._call(
            "OKE node boot volume replacement",
            self.container_engine.replace_boot_volume_cluster_node,
            cluster_id,
            node_id,
            details,
            opc_retry_token=str(uuid.uuid4()),
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def preview_managed_pool_boot_volume_replace(
        self,
        node_pool_id: str,
        spec: PoolBootVolumeReplaceSpec,
    ) -> dict[str, Any]:
        _details, _etag, preview = self._managed_pool_bvr_update(
            node_pool_id,
            spec,
        )
        return preview

    def replace_managed_pool_boot_volumes(
        self,
        node_pool_id: str,
        spec: PoolBootVolumeReplaceSpec,
    ) -> str | None:
        details, etag, _preview = self._managed_pool_bvr_update(
            node_pool_id,
            spec,
        )
        kwargs: dict[str, Any] = {}
        if etag:
            kwargs["if_match"] = etag
        response = self._call(
            "Managed OKE node-pool boot volume replacement",
            self.container_engine.update_node_pool,
            node_pool_id,
            details,
            **kwargs,
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def managed_pool_boot_volume_replace_applied(
        self,
        node_pool_id: str,
        spec: PoolBootVolumeReplaceSpec,
    ) -> bool:
        source = self._call(
            "Managed OKE node-pool BVR verification lookup",
            self.container_engine.get_node_pool,
            node_pool_id,
        ).data
        source_details = getattr(source, "node_source_details", None)
        node_config = getattr(source, "node_config_details", None)
        metadata = dict(getattr(source, "node_metadata", None) or {})
        if (
            spec.image_id
            and getattr(source_details, "image_id", None) != spec.image_id
        ):
            return False
        if (
            spec.boot_volume_size_in_gbs is not None
            and getattr(source_details, "boot_volume_size_in_gbs", None)
            != spec.boot_volume_size_in_gbs
        ):
            return False
        if (
            spec.boot_volume_kms_key_id
            and getattr(node_config, "kms_key_id", None)
            != spec.boot_volume_kms_key_id
        ):
            return False
        if (
            spec.kubernetes_version
            and getattr(source, "kubernetes_version", None)
            != spec.kubernetes_version
        ):
            return False
        if any(metadata.get(key) != value for key, value in spec.node_metadata):
            return False
        if (
            spec.ssh_public_key
            and getattr(source, "ssh_public_key", None) != spec.ssh_public_key
        ):
            return False
        return True

    def _managed_pool_bvr_update(
        self,
        node_pool_id: str,
        spec: PoolBootVolumeReplaceSpec,
    ) -> tuple[Any, str | None, dict[str, Any]]:
        response = self._call(
            "Managed OKE node-pool BVR source lookup",
            self.container_engine.get_node_pool,
            node_pool_id,
        )
        source = response.data
        lifecycle_state = str(
            getattr(source, "lifecycle_state", "") or ""
        ).upper()
        if lifecycle_state and lifecycle_state not in {"ACTIVE", "RUNNING"}:
            raise OciDiscoveryError(
                f"Managed OKE node pool is not active: {lifecycle_state}"
            )

        source_details = getattr(source, "node_source_details", None)
        current_image_id = getattr(source_details, "image_id", None)
        current_boot_size = getattr(
            source_details,
            "boot_volume_size_in_gbs",
            None,
        )
        if not current_image_id:
            raise OciDiscoveryError(
                "Managed OKE node pool does not expose its current image OCID."
            )
        if (
            spec.boot_volume_size_in_gbs is not None
            and current_boot_size is not None
            and spec.boot_volume_size_in_gbs < current_boot_size
        ):
            raise OciDiscoveryError(
                "Boot volume replacement cannot reduce boot volume size "
                f"from {current_boot_size} GB to {spec.boot_volume_size_in_gbs} GB."
            )

        effective_image_id = spec.image_id or current_image_id
        effective_boot_size = (
            spec.boot_volume_size_in_gbs
            if spec.boot_volume_size_in_gbs is not None
            else current_boot_size
        )
        if spec.image_id:
            compartment_id = getattr(source, "compartment_id", None)
            shape = getattr(source, "node_shape", None)
            if not isinstance(compartment_id, str) or not compartment_id:
                raise OciDiscoveryError(
                    "Managed OKE node pool does not expose its compartment OCID."
                )
            if not isinstance(shape, str) or not shape:
                raise OciDiscoveryError(
                    "Managed OKE node pool does not expose its worker shape."
                )
            self._validate_bvr_image_distribution(
                current_image_id,
                spec.image_id,
            )
            node_config = getattr(source, "node_config_details", None)
            placement_configs = tuple(
                getattr(node_config, "placement_configs", None) or ()
            )
            pod_network = getattr(
                node_config,
                "node_pool_pod_network_option_details",
                None,
            )
            self._validate_create_compatibility(
                compartment_id=compartment_id,
                shape=shape,
                image_id=spec.image_id,
                availability_domains=tuple(
                    getattr(placement, "availability_domain", "")
                    for placement in placement_configs
                ),
                source_subnet_id=getattr(
                    next(iter(placement_configs), None),
                    "subnet_id",
                    None,
                ),
                primary_subnet_ids=tuple(
                    getattr(placement, "subnet_id", "")
                    for placement in placement_configs
                ),
                pod_subnet_ids=tuple(
                    getattr(pod_network, "pod_subnet_ids", None) or ()
                ),
                nsg_ids=tuple(
                    dict.fromkeys(
                        [
                            *(getattr(node_config, "nsg_ids", None) or []),
                            *(getattr(pod_network, "pod_nsg_ids", None) or []),
                        ]
                    )
                ),
                require_local_nvme=False,
            )

        update_values: dict[str, Any] = {
            "node_pool_cycling_details": (
                self.oci.container_engine.models.NodePoolCyclingDetails(
                    maximum_unavailable=spec.maximum_unavailable,
                    is_node_cycling_enabled=True,
                    cycle_modes=["BOOT_VOLUME_REPLACE"],
                )
            ),
        }
        if spec.image_id or spec.boot_volume_size_in_gbs is not None:
            update_values["node_source_details"] = (
                self.oci.container_engine.models.NodeSourceViaImageDetails(
                    source_type="IMAGE",
                    image_id=effective_image_id,
                    boot_volume_size_in_gbs=effective_boot_size,
                )
            )
        if spec.kubernetes_version:
            update_values["kubernetes_version"] = spec.kubernetes_version
        if spec.node_metadata:
            metadata = dict(getattr(source, "node_metadata", None) or {})
            metadata.update(dict(spec.node_metadata))
            update_values["node_metadata"] = metadata
        if spec.ssh_public_key:
            update_values["ssh_public_key"] = spec.ssh_public_key
        if spec.boot_volume_kms_key_id:
            update_values["node_config_details"] = (
                self.oci.container_engine.models.UpdateNodePoolNodeConfigDetails(
                    kms_key_id=spec.boot_volume_kms_key_id,
                )
            )

        current_config = getattr(source, "node_config_details", None)
        current = {
            "image_id": current_image_id,
            "boot_volume_size_in_gbs": current_boot_size,
            "boot_volume_kms_key_id": getattr(
                current_config,
                "kms_key_id",
                None,
            ),
            "kubernetes_version": getattr(
                source,
                "kubernetes_version",
                None,
            ),
            "node_metadata_keys": sorted(
                (getattr(source, "node_metadata", None) or {}).keys()
            ),
            "ssh_public_key_configured": bool(
                getattr(source, "ssh_public_key", None)
            ),
        }
        effective = {
            "image_id": effective_image_id,
            "boot_volume_size_in_gbs": effective_boot_size,
            "boot_volume_kms_key_id": (
                spec.boot_volume_kms_key_id
                or current["boot_volume_kms_key_id"]
            ),
            "kubernetes_version": (
                spec.kubernetes_version or current["kubernetes_version"]
            ),
            "node_metadata_keys": sorted(
                set(current["node_metadata_keys"])
                | {key for key, _value in spec.node_metadata}
            ),
            "ssh_public_key_configured": bool(
                spec.ssh_public_key or current["ssh_public_key_configured"]
            ),
            "maximum_unavailable": spec.maximum_unavailable,
            "cycle_modes": ["BOOT_VOLUME_REPLACE"],
        }
        details = self.oci.container_engine.models.UpdateNodePoolDetails(
            **update_values
        )
        headers = getattr(response, "headers", None) or {}
        return details, headers.get("etag"), {
            "current": current,
            "effective": effective,
        }

    def _validate_bvr_image_distribution(
        self,
        current_image_id: str,
        replacement_image_id: str,
    ) -> None:
        operating_systems: list[str] = []
        for label, image_id in (
            ("current", current_image_id),
            ("replacement", replacement_image_id),
        ):
            image = self._call(
                f"Managed OKE {label} BVR image lookup",
                self.compute.get_image,
                image_id,
            ).data
            operating_system = str(
                getattr(image, "operating_system", "") or ""
            ).strip()
            if not operating_system:
                raise OciDiscoveryError(
                    f"The {label} image {image_id} does not expose its operating "
                    "system."
                )
            if operating_system.casefold() == "windows":
                raise OciDiscoveryError(
                    "OKE boot volume replacement supports Linux images only."
                )
            operating_systems.append(operating_system)
        if operating_systems[0].casefold() != operating_systems[1].casefold():
            raise OciDiscoveryError(
                "The replacement image must use the same Linux distribution as "
                f"the current image ({operating_systems[0]}); discovered "
                f"{operating_systems[1]}."
            )

    def preview_managed_pool_upgrade(
        self,
        node_pool_id: str,
        target_version: str,
        *,
        strategy: str,
        image_id: str | None = None,
        maximum_unavailable: str | None = None,
        maximum_surge: str | None = None,
        enable_cycling: bool = True,
    ) -> tuple[Any, str, dict[str, Any]]:
        response = self._call(
            "Managed OKE node pool lookup",
            self.container_engine.get_node_pool,
            node_pool_id,
        )
        pool = response.data
        etag = (getattr(response, "headers", None) or {}).get("etag")
        if not etag:
            raise OciDiscoveryError(
                f"Managed node pool {node_pool_id} did not return an ETag."
            )
        current_version = getattr(pool, "kubernetes_version", None)
        if not current_version:
            raise OciDiscoveryError(
                f"Managed node pool {node_pool_id} did not return a Kubernetes version."
            )
        if strategy not in {"boot-volume-replace", "instance-replace"}:
            raise OciDiscoveryError(
                f"Managed in-place upgrade does not support strategy {strategy!r}."
            )
        source = getattr(pool, "node_source_details", None)
        current_image_id = getattr(source, "image_id", None)
        effective_image_id = image_id or current_image_id
        if not effective_image_id:
            raise OciDiscoveryError(
                f"Managed node pool {node_pool_id} did not return an image OCID."
            )
        if image_id and current_image_id:
            self._validate_bvr_image_distribution(current_image_id, image_id)
            node_config = getattr(pool, "node_config_details", None)
            placements = tuple(
                getattr(node_config, "placement_configs", None) or ()
            )
            shape = getattr(pool, "node_shape", None)
            compartment_id = getattr(pool, "compartment_id", None)
            if not shape or not compartment_id:
                raise OciDiscoveryError(
                    "Managed node pool does not expose shape and compartment "
                    "details required for custom-image validation."
                )
            self._validate_create_compatibility(
                compartment_id=compartment_id,
                shape=shape,
                image_id=image_id,
                availability_domains=tuple(
                    getattr(placement, "availability_domain", "")
                    for placement in placements
                ),
                source_subnet_id=getattr(
                    next(iter(placements), None),
                    "subnet_id",
                    None,
                ),
                primary_subnet_ids=tuple(
                    getattr(placement, "subnet_id", "")
                    for placement in placements
                ),
                pod_subnet_ids=(),
                nsg_ids=(),
                require_local_nvme=False,
            )

        mode = (
            "BOOT_VOLUME_REPLACE"
            if strategy == "boot-volume-replace"
            else "INSTANCE_REPLACE"
        )
        values: dict[str, Any] = {
            "kubernetes_version": target_version,
        }
        cycling = None
        if enable_cycling:
            cycling = self.oci.container_engine.models.NodePoolCyclingDetails(
                maximum_unavailable=(
                    maximum_unavailable
                    if maximum_unavailable is not None
                    else ("1" if strategy == "boot-volume-replace" else "0")
                ),
                maximum_surge=(
                    maximum_surge
                    if maximum_surge is not None
                    else (None if strategy == "boot-volume-replace" else "1")
                ),
                is_node_cycling_enabled=True,
                cycle_modes=[mode],
            )
            values["node_pool_cycling_details"] = cycling
        if image_id:
            values["node_source_details"] = (
                self.oci.container_engine.models.NodeSourceViaImageDetails(
                    source_type="IMAGE",
                    image_id=effective_image_id,
                    boot_volume_size_in_gbs=getattr(
                        source,
                        "boot_volume_size_in_gbs",
                        None,
                    ),
                )
            )
        details = self.oci.container_engine.models.UpdateNodePoolDetails(**values)
        return details, etag, {
            "current_version": current_version,
            "target_version": target_version,
            "current_image_id": current_image_id,
            "target_image_id": effective_image_id,
            "strategy": strategy,
            "phase": "cycle" if enable_cycling else "launch-configuration",
            "cycle_mode": mode if enable_cycling else None,
            "maximum_unavailable": (
                cycling.maximum_unavailable if cycling else None
            ),
            "maximum_surge": cycling.maximum_surge if cycling else None,
        }

    def upgrade_managed_pool(
        self,
        node_pool_id: str,
        details: Any,
        etag: str,
    ) -> str | None:
        response = self._call(
            "Managed OKE worker upgrade",
            self.container_engine.update_node_pool,
            node_pool_id,
            details,
            if_match=etag,
        )
        return (getattr(response, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def preview_instance_configuration_upgrade(
        self,
        instance_configuration_id: str,
        target_version: str,
        *,
        operation_id: str,
        api_server: str,
        cluster_ca: str,
        image_id: str | None = None,
        availability_domain: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        source = self._get_instance_configuration_template(
            instance_configuration_id
        )
        details, summary = self._build_upgrade_instance_configuration_details(
            source,
            target_version,
            operation_id=operation_id,
            api_server=api_server,
            cluster_ca=cluster_ca,
            image_id=image_id,
            availability_domain=availability_domain,
        )
        return details, summary

    def create_upgrade_instance_configuration(
        self,
        instance_configuration_id: str,
        target_version: str,
        *,
        operation_id: str,
        api_server: str,
        cluster_ca: str,
        image_id: str | None = None,
        availability_domain: str | None = None,
    ) -> str:
        details, _summary = self.preview_instance_configuration_upgrade(
            instance_configuration_id,
            target_version,
            operation_id=operation_id,
            api_server=api_server,
            cluster_ca=cluster_ca,
            image_id=image_id,
            availability_domain=availability_domain,
        )
        response = self._call(
            "Upgrade Instance Configuration creation",
            self.compute_mgmt.create_instance_configuration,
            details,
            opc_retry_token=_operation_retry_token(
                operation_id,
                "instance-configuration",
                instance_configuration_id,
                target_version,
            ),
        )
        configuration_id = getattr(response.data, "id", None)
        if not configuration_id:
            raise OciDiscoveryError(
                "Upgrade Instance Configuration creation returned no OCID."
            )
        return str(configuration_id)

    def _build_upgrade_instance_configuration_details(
        self,
        source: Any,
        target_version: str,
        *,
        operation_id: str,
        api_server: str,
        cluster_ca: str,
        image_id: str | None,
        availability_domain: str | None,
    ) -> tuple[Any, dict[str, Any]]:
        instance_details = deepcopy(getattr(source, "instance_details", None))
        launch = getattr(instance_details, "launch_details", None)
        if launch is None:
            raise OciDiscoveryError(
                "Source Instance Configuration does not expose launch details."
            )
        metadata = dict(getattr(launch, "metadata", None) or {})
        required = ("user_data", "oke-initial-node-labels")
        missing = [name for name in required if not metadata.get(name)]
        if missing:
            raise OciDiscoveryError(
                "Source Instance Configuration is missing OKE bootstrap "
                f"metadata: {', '.join(missing)}."
            )
        if not api_server or not cluster_ca:
            raise OciDiscoveryError(
                "Current Kubernetes API endpoint and cluster CA are required "
                "to refresh self-managed worker bootstrap."
            )
        metadata["apiserver_host"] = api_server
        metadata["cluster_ca_cert"] = cluster_ca
        metadata["oke-k8version"] = target_version
        metadata["user_data"] = compose_worker_user_data(
            metadata["user_data"],
            PoolCreateSpec(
                pool_type="rdma",
                kubernetes_version=target_version,
            ),
        )
        launch.metadata = metadata
        source_details = getattr(launch, "source_details", None)
        if source_details is None:
            raise OciDiscoveryError(
                "Source Instance Configuration has no image source details."
            )
        current_image_id = getattr(source_details, "image_id", None)
        if not current_image_id:
            raise OciDiscoveryError(
                "Source Instance Configuration does not expose an image OCID."
            )
        if image_id:
            self._validate_bvr_image_distribution(current_image_id, image_id)
            source_details.image_id = image_id
            shape = getattr(launch, "shape", None)
            compartment_id = getattr(source, "compartment_id", None)
            selected_ad = (
                availability_domain
                or getattr(launch, "availability_domain", None)
            )
            primary_vnic = getattr(launch, "create_vnic_details", None)
            if not shape or not compartment_id or not selected_ad:
                raise OciDiscoveryError(
                    "Custom-image upgrade validation requires the inherited "
                    "shape, compartment, and availability domain."
                )
            self._validate_create_compatibility(
                compartment_id=compartment_id,
                shape=shape,
                image_id=image_id,
                availability_domains=(selected_ad,),
                source_subnet_id=getattr(
                    primary_vnic,
                    "subnet_id",
                    None,
                ),
                primary_subnet_ids=(
                    getattr(primary_vnic, "subnet_id", "") or "",
                ),
                pod_subnet_ids=tuple(
                    _split_metadata_values(metadata.get("pod-subnets"))
                ),
                nsg_ids=tuple(
                    dict.fromkeys(
                        [
                            *(
                                getattr(primary_vnic, "nsg_ids", None)
                                or []
                            ),
                            *_split_metadata_values(
                                metadata.get("pod-nsgids")
                            ),
                        ]
                    )
                ),
                require_local_nvme=False,
            )
        effective_image = image_id or current_image_id
        tags = dict(getattr(source, "freeform_tags", None) or {})
        tags.update(
            {
                "mgmt-oke-created": "true",
                "mgmt-oke-upgrade-operation": operation_id,
                "mgmt-oke-upgrade-source-config": str(
                    getattr(source, "id", "")
                ),
            }
        )
        launch_tags = dict(getattr(launch, "freeform_tags", None) or {})
        launch_tags.update(tags)
        launch.freeform_tags = launch_tags
        display_name = (
            f"{getattr(source, 'display_name', 'oke-worker')}-"
            f"{target_version.lstrip('v').replace('.', '-')}"
        )
        details = self.oci.core.models.CreateInstanceConfigurationDetails(
            compartment_id=getattr(source, "compartment_id", None),
            display_name=display_name,
            freeform_tags=tags,
            defined_tags=deepcopy(getattr(source, "defined_tags", None)),
            source="NONE",
            instance_details=instance_details,
        )
        if metadata.get("oke-k8version") != target_version:
            raise OciDiscoveryError(
                "Target Kubernetes version could not be proven in cloned bootstrap."
            )
        return details, {
            "source_instance_configuration_id": getattr(source, "id", None),
            "target_version": target_version,
            "image_id": effective_image,
            "api_server_refreshed": True,
            "cluster_ca_refreshed": True,
            "metadata_keys_preserved": sorted(metadata),
        }

    def attach_upgrade_instance_configuration(
        self,
        pool: WorkerPoolInfo,
        instance_configuration_id: str,
    ) -> str | None:
        if pool.kind == "cluster-network" and pool.cluster_network_id:
            response = self._call(
                "Cluster Network lookup",
                self.compute_mgmt.get_cluster_network,
                pool.cluster_network_id,
            )
            cluster_network = response.data
            etag = (getattr(response, "headers", None) or {}).get("etag")
            if not etag:
                raise OciDiscoveryError("Cluster Network lookup returned no ETag.")
            updates = []
            matched = False
            for item in list(
                getattr(cluster_network, "instance_pools", None) or ()
            ):
                item_id = getattr(item, "id", None)
                is_target = item_id == pool.instance_pool_id
                matched = matched or is_target
                updates.append(
                    self.oci.core.models.UpdateClusterNetworkInstancePoolDetails(
                        id=item_id,
                        display_name=getattr(item, "display_name", None),
                        size=getattr(item, "size", None),
                        instance_configuration_id=(
                            instance_configuration_id
                            if is_target
                            else getattr(item, "instance_configuration_id", None)
                        ),
                        defined_tags=getattr(item, "defined_tags", None),
                        freeform_tags=getattr(item, "freeform_tags", None),
                    )
                )
            if not matched:
                raise OciDiscoveryError(
                    f"Instance pool {pool.instance_pool_id} is not part of "
                    f"Cluster Network {pool.cluster_network_id}."
                )
            details = self.oci.core.models.UpdateClusterNetworkDetails(
                display_name=getattr(cluster_network, "display_name", None),
                defined_tags=getattr(cluster_network, "defined_tags", None),
                freeform_tags=getattr(cluster_network, "freeform_tags", None),
                instance_pools=updates,
            )
            updated = self._call(
                "Cluster Network upgrade configuration attachment",
                self.compute_mgmt.update_cluster_network,
                pool.cluster_network_id,
                details,
                if_match=etag,
            )
        elif pool.kind == "instance-pool" and pool.instance_pool_id:
            response = self._call(
                "Instance Pool lookup",
                self.compute_mgmt.get_instance_pool,
                pool.instance_pool_id,
            )
            etag = (getattr(response, "headers", None) or {}).get("etag")
            if not etag:
                raise OciDiscoveryError("Instance Pool lookup returned no ETag.")
            details = self.oci.core.models.UpdateInstancePoolDetails(
                instance_configuration_id=instance_configuration_id,
            )
            updated = self._call(
                "Instance Pool upgrade configuration attachment",
                self.compute_mgmt.update_instance_pool,
                pool.instance_pool_id,
                details,
                if_match=etag,
            )
        elif (
            pool.kind == "gpu-memory-cluster"
            and pool.gpu_memory_cluster_id
        ):
            response = self._call(
                "GPU Memory Cluster lookup",
                self.compute.get_compute_gpu_memory_cluster,
                pool.gpu_memory_cluster_id,
            )
            etag = (getattr(response, "headers", None) or {}).get("etag")
            if not etag:
                raise OciDiscoveryError(
                    "GPU Memory Cluster lookup returned no ETag."
                )
            details = self.oci.core.models.UpdateComputeGpuMemoryClusterDetails(
                instance_configuration_id=instance_configuration_id,
            )
            updated = self._call(
                "GPU Memory Cluster upgrade configuration attachment",
                self.compute.update_compute_gpu_memory_cluster,
                pool.gpu_memory_cluster_id,
                details,
                if_match=etag,
            )
        else:
            raise OciDiscoveryError(
                f"Pool {pool.name} does not expose a supported self-managed backend."
            )
        return (getattr(updated, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def replace_self_managed_instance_boot_volume(
        self,
        instance_id: str,
        target_instance_configuration_id: str,
    ) -> None:
        configuration = self._get_instance_configuration_template(
            target_instance_configuration_id
        )
        launch = getattr(
            getattr(configuration, "instance_details", None),
            "launch_details",
            None,
        )
        source = getattr(launch, "source_details", None)
        image_id = getattr(source, "image_id", None)
        metadata = dict(getattr(launch, "metadata", None) or {})
        if not image_id or not metadata.get("oke-k8version"):
            raise OciDiscoveryError(
                "Target Instance Configuration does not prove its image and "
                "Kubernetes bootstrap version."
            )
        instance_response = self._call(
            "Compute instance lookup",
            self.compute.get_instance,
            instance_id,
        )
        etag = (getattr(instance_response, "headers", None) or {}).get("etag")
        if not etag:
            raise OciDiscoveryError(
                f"Compute instance {instance_id} lookup returned no ETag."
            )
        update_source = self.oci.core.models.UpdateInstanceSourceViaImageDetails(
            source_type="IMAGE",
            image_id=image_id,
            boot_volume_size_in_gbs=getattr(
                source,
                "boot_volume_size_in_gbs",
                None,
            ),
            kms_key_id=getattr(source, "kms_key_id", None),
            is_preserve_boot_volume_enabled=True,
        )
        details = self.oci.core.models.UpdateInstanceDetails(
            metadata=metadata,
            source_details=update_source,
            update_operation_constraint="ALLOW_DOWNTIME",
        )
        self._call(
            "Self-managed worker boot volume replacement",
            self.compute.update_instance,
            instance_id,
            details,
            if_match=etag,
        )

    def create_managed_blue_green_pool(
        self,
        pool: WorkerPoolInfo,
        *,
        cluster_id: str,
        compartment_id: str,
        target_version: str,
        name: str,
        image_id: str | None,
        operation_id: str,
    ) -> ManagedNodePoolCreateResult:
        if not pool.node_pool_id or not pool.desired_size:
            raise OciDiscoveryError(
                f"Managed pool {pool.name} has no source OCID or positive size."
            )
        pool_type = (
            "rdma"
            if pool.rdma_enabled
            else "gpu" if pool.gpu_resource else "cpu"
        )
        return self.create_managed_node_pool(
            pool.node_pool_id,
            cluster_id,
            compartment_id,
            name,
            pool.desired_size,
            PoolCreateSpec(
                pool_type=pool_type,
                rdma_mode=(
                    "compute-cluster" if pool.rdma_enabled else None
                ),
                compute_cluster_id=pool.compute_cluster_id,
                host_group_id=(
                    next(iter(sorted(pool.host_group_ids)), None)
                ),
                kubernetes_version=target_version,
                image_id=image_id,
                freeform_tags=(
                    ("mgmt-oke-upgrade-operation", operation_id),
                    ("mgmt-oke-blue-green-source", pool.name),
                ),
            ),
            opc_retry_token=_operation_retry_token(
                operation_id,
                "managed-blue-green",
                pool.node_pool_id,
                name,
            ),
        )

    def create_cluster_network_blue_green_pool(
        self,
        pool: WorkerPoolInfo,
        *,
        target_version: str,
        name: str,
        image_id: str | None,
        operation_id: str,
        target_instance_configuration_id: str | None = None,
    ) -> ClusterNetworkCreateResult:
        if (
            not pool.cluster_network_id
            or not pool.instance_pool_id
            or not pool.desired_size
        ):
            raise OciDiscoveryError(
                f"Cluster Network pool {pool.name} is missing its source configuration."
            )
        spec = PoolCreateSpec(
            pool_type="rdma",
            kubernetes_version=target_version,
            image_id=image_id,
            freeform_tags=(
                ("mgmt-oke-upgrade-operation", operation_id),
                ("mgmt-oke-blue-green-source", pool.name),
            ),
        )
        if not target_instance_configuration_id:
            return self.create_cluster_network_pool(
                pool.cluster_network_id,
                pool.instance_pool_id,
                name,
                pool.desired_size,
                spec,
                instance_configuration_retry_token=(
                    _operation_retry_token(
                        operation_id,
                        "instance-configuration",
                        pool.instance_configuration_id or pool.instance_pool_id,
                        target_version,
                    )
                ),
                cluster_network_retry_token=_operation_retry_token(
                    operation_id,
                    "cluster-network-blue-green",
                    pool.cluster_network_id,
                    name,
                ),
            )
        source, source_pool, placement = self._get_cluster_network_pool_source(
            pool.cluster_network_id,
            pool.instance_pool_id,
        )
        return self._create_cluster_network_with_configuration(
            source,
            source_pool,
            placement,
            normalize_pool_name(name),
            pool.desired_size,
            target_instance_configuration_id,
            spec,
            opc_retry_token=_operation_retry_token(
                operation_id,
                "cluster-network-blue-green",
                pool.cluster_network_id,
                name,
            ),
        )

    def create_instance_pool_blue_green(
        self,
        pool: WorkerPoolInfo,
        *,
        target_instance_configuration_id: str,
        name: str,
        operation_id: str,
    ) -> tuple[str, str | None]:
        if not pool.instance_pool_id or not pool.desired_size:
            raise OciDiscoveryError(
                f"Instance Pool {pool.name} is missing its source configuration."
            )
        response = self._call(
            "Source Instance Pool lookup",
            self.compute_mgmt.get_instance_pool,
            pool.instance_pool_id,
        )
        source = response.data
        tags = dict(getattr(source, "freeform_tags", None) or {})
        tags.update(
            {
                "mgmt-oke-created": "true",
                "mgmt-oke-upgrade-operation": operation_id,
                "mgmt-oke-blue-green-source": pool.name,
            }
        )
        details = self.oci.core.models.CreateInstancePoolDetails(
            compartment_id=getattr(source, "compartment_id", None),
            display_name=name,
            instance_configuration_id=target_instance_configuration_id,
            placement_configurations=deepcopy(
                getattr(source, "placement_configurations", None)
            ),
            size=pool.desired_size,
            load_balancers=deepcopy(getattr(source, "load_balancers", None)),
            instance_display_name_formatter=getattr(
                source,
                "instance_display_name_formatter",
                None,
            ),
            instance_hostname_formatter=getattr(
                source,
                "instance_hostname_formatter",
                None,
            ),
            lifecycle_management=deepcopy(
                getattr(source, "lifecycle_management", None)
            ),
            freeform_tags=tags,
            defined_tags=deepcopy(getattr(source, "defined_tags", None)),
        )
        created = self._call(
            "Blue-green Instance Pool creation",
            self.compute_mgmt.create_instance_pool,
            details,
            opc_retry_token=_operation_retry_token(
                operation_id,
                "instance-pool-blue-green",
                pool.instance_pool_id,
                name,
            ),
        )
        pool_id = getattr(created.data, "id", None)
        if not pool_id:
            raise OciDiscoveryError(
                "Blue-green Instance Pool creation returned no OCID."
            )
        return str(pool_id), (
            getattr(created, "headers", None) or {}
        ).get("opc-work-request-id")

    def create_gpu_memory_cluster_blue_green(
        self,
        pool: WorkerPoolInfo,
        *,
        target_instance_configuration_id: str,
        name: str,
        operation_id: str,
        compute_cluster_id: str | None,
        gpu_memory_fabric_id: str | None,
    ) -> tuple[str, str | None]:
        if not pool.gpu_memory_cluster_id or not pool.desired_size:
            raise OciDiscoveryError(
                f"GPU Memory Cluster {pool.name} is missing its source configuration."
            )
        if not compute_cluster_id or not gpu_memory_fabric_id:
            raise OciDiscoveryError(
                "GPU Memory Cluster blue-green requires explicit "
                "--blue-green-compute-cluster-id and "
                "--blue-green-gpu-memory-fabric-id targets."
            )
        source = self._call(
            "Source GPU Memory Cluster lookup",
            self.compute.get_compute_gpu_memory_cluster,
            pool.gpu_memory_cluster_id,
        ).data
        tags = dict(getattr(source, "freeform_tags", None) or {})
        tags.update(
            {
                "mgmt-oke-created": "true",
                "mgmt-oke-upgrade-operation": operation_id,
                "mgmt-oke-blue-green-source": pool.name,
            }
        )
        details = self.oci.core.models.CreateComputeGpuMemoryClusterDetails(
            availability_domain=getattr(source, "availability_domain", None),
            compartment_id=getattr(source, "compartment_id", None),
            gpu_memory_fabric_id=gpu_memory_fabric_id,
            compute_cluster_id=compute_cluster_id,
            instance_configuration_id=target_instance_configuration_id,
            size=pool.desired_size,
            display_name=name,
            gpu_memory_cluster_scale_config=deepcopy(
                getattr(source, "gpu_memory_cluster_scale_config", None)
            ),
            freeform_tags=tags,
            defined_tags=deepcopy(getattr(source, "defined_tags", None)),
        )
        created = self._call(
            "Blue-green GPU Memory Cluster creation",
            self.compute.create_compute_gpu_memory_cluster,
            details,
            opc_retry_token=_operation_retry_token(
                operation_id,
                "gpu-memory-cluster-blue-green",
                pool.gpu_memory_cluster_id,
                name,
            ),
        )
        cluster_id = getattr(created.data, "id", None)
        if not cluster_id:
            raise OciDiscoveryError(
                "Blue-green GPU Memory Cluster creation returned no OCID."
            )
        return str(cluster_id), (
            getattr(created, "headers", None) or {}
        ).get("opc-work-request-id")

    def resize_upgrade_backend(
        self,
        pool: WorkerPoolInfo,
        size: int,
    ) -> str | None:
        if size < 0:
            raise OciDiscoveryError("Upgrade backend size cannot be negative.")
        if pool.kind == "cluster-network" and pool.cluster_network_id:
            response = self._call(
                "Cluster Network lookup",
                self.compute_mgmt.get_cluster_network,
                pool.cluster_network_id,
            )
            cluster_network = response.data
            etag = (getattr(response, "headers", None) or {}).get("etag")
            if not etag:
                raise OciDiscoveryError("Cluster Network lookup returned no ETag.")
            updates = []
            matched = False
            for item in list(
                getattr(cluster_network, "instance_pools", None) or ()
            ):
                item_id = getattr(item, "id", None)
                is_target = item_id == pool.instance_pool_id
                matched = matched or is_target
                updates.append(
                    self.oci.core.models.UpdateClusterNetworkInstancePoolDetails(
                        id=item_id,
                        display_name=getattr(item, "display_name", None),
                        size=size if is_target else getattr(item, "size", None),
                        instance_configuration_id=getattr(
                            item,
                            "instance_configuration_id",
                            None,
                        ),
                        defined_tags=getattr(item, "defined_tags", None),
                        freeform_tags=getattr(item, "freeform_tags", None),
                    )
                )
            if not matched:
                raise OciDiscoveryError(
                    f"Cluster Network {pool.name} does not contain its expected "
                    f"Instance Pool {pool.instance_pool_id}."
                )
            details = self.oci.core.models.UpdateClusterNetworkDetails(
                display_name=getattr(cluster_network, "display_name", None),
                defined_tags=getattr(cluster_network, "defined_tags", None),
                freeform_tags=getattr(cluster_network, "freeform_tags", None),
                instance_pools=updates,
            )
            updated = self._call(
                "Cluster Network upgrade resize",
                self.compute_mgmt.update_cluster_network,
                pool.cluster_network_id,
                details,
                if_match=etag,
            )
        elif pool.kind == "instance-pool" and pool.instance_pool_id:
            response = self._call(
                "Instance Pool lookup",
                self.compute_mgmt.get_instance_pool,
                pool.instance_pool_id,
            )
            etag = (getattr(response, "headers", None) or {}).get("etag")
            if not etag:
                raise OciDiscoveryError("Instance Pool lookup returned no ETag.")
            details = self.oci.core.models.UpdateInstancePoolDetails(size=size)
            updated = self._call(
                "Instance Pool upgrade resize",
                self.compute_mgmt.update_instance_pool,
                pool.instance_pool_id,
                details,
                if_match=etag,
            )
        elif (
            pool.kind == "gpu-memory-cluster"
            and pool.gpu_memory_cluster_id
        ):
            response = self._call(
                "GPU Memory Cluster lookup",
                self.compute.get_compute_gpu_memory_cluster,
                pool.gpu_memory_cluster_id,
            )
            etag = (getattr(response, "headers", None) or {}).get("etag")
            if not etag:
                raise OciDiscoveryError(
                    "GPU Memory Cluster lookup returned no ETag."
                )
            details = self.oci.core.models.UpdateComputeGpuMemoryClusterDetails(
                size=size
            )
            updated = self._call(
                "GPU Memory Cluster upgrade resize",
                self.compute.update_compute_gpu_memory_cluster,
                pool.gpu_memory_cluster_id,
                details,
                if_match=etag,
            )
        else:
            raise OciDiscoveryError(
                f"Pool {pool.name} does not expose a resizable upgrade backend."
            )
        return (getattr(updated, "headers", None) or {}).get(
            "opc-work-request-id"
        )

    def terminate_upgrade_instance(self, instance_id: str) -> None:
        response = self._call(
            "Compute instance lookup",
            self.compute.get_instance,
            instance_id,
        )
        etag = (getattr(response, "headers", None) or {}).get("etag")
        if not etag:
            raise OciDiscoveryError(
                f"Compute instance {instance_id} lookup returned no ETag."
            )
        self._call(
            "Upgrade instance termination",
            self.compute.terminate_instance,
            instance_id,
            preserve_boot_volume=False,
            if_match=etag,
        )

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
                created_by_mgmt_oke=_is_mgmt_oke_created(
                    getattr(cluster_network, "freeform_tags", None)
                ),
                placement_type="cluster-network",
                rdma_enabled=True,
            )
            if instance_pool_id:
                self._enrich_from_instance_pool(pool, compartment_id, instance_pool_id)
            pools.append(pool)
        return pools

    def list_gpu_memory_cluster_pools(
        self,
        compartment_id: str,
    ) -> list[WorkerPoolInfo]:
        response = self.oci.pagination.list_call_get_all_results(
            self.compute.list_compute_gpu_memory_clusters,
            compartment_id,
        )
        pools: list[WorkerPoolInfo] = []
        for summary in response.data:
            state = str(getattr(summary, "lifecycle_state", "") or "").upper()
            if state in {"DELETED", "DELETING", "TERMINATED", "TERMINATING"}:
                continue
            cluster_id = getattr(summary, "id", None)
            if not cluster_id:
                continue
            cluster_response = self._call(
                "GPU Memory Cluster lookup",
                self.compute.get_compute_gpu_memory_cluster,
                cluster_id,
            )
            cluster = cluster_response.data
            instances_response = self._call(
                "GPU Memory Cluster instance listing",
                self.oci.pagination.list_call_get_all_results,
                self.compute.list_compute_gpu_memory_cluster_instances,
                cluster_id,
            )
            instances = list(instances_response.data)
            active = [
                instance
                for instance in instances
                if _lifecycle_state(instance).upper()
                in {"ACTIVE", "RUNNING", "STARTING", "PROVISIONING"}
            ]
            shape = None
            if active and (instance_id := _object_id(active[0])):
                try:
                    shape = getattr(
                        self.compute.get_instance(instance_id).data,
                        "shape",
                        None,
                    )
                except Exception:
                    pass
            instance_configuration_id = getattr(
                cluster,
                "instance_configuration_id",
                None,
            )
            pools.append(
                WorkerPoolInfo(
                    name=getattr(cluster, "display_name", None) or cluster_id,
                    kind="gpu-memory-cluster",
                    shape=shape,
                    compartment_id=compartment_id,
                    availability_domain=getattr(
                        cluster,
                        "availability_domain",
                        None,
                    ),
                    desired_size=getattr(cluster, "size", None),
                    active_oci_instances=len(active),
                    placement_type="gpu-memory-cluster",
                    compute_cluster_id=getattr(
                        cluster,
                        "compute_cluster_id",
                        None,
                    ),
                    oci_instance_ids={
                        item_id
                        for item in instances
                        if (item_id := _object_id(item))
                    },
                    gpu_resource=_gpu_resource_for_shape(shape),
                    rdma_enabled=True,
                    instance_configuration_id=instance_configuration_id,
                    kubernetes_version=(
                        self._instance_configuration_kubernetes_version(
                            instance_configuration_id
                        )
                    ),
                    gpu_memory_cluster_id=cluster_id,
                    gpu_memory_fabric_id=getattr(
                        cluster,
                        "gpu_memory_fabric_id",
                        None,
                    ),
                    created_by_mgmt_oke=_is_mgmt_oke_created(
                        getattr(cluster, "freeform_tags", None)
                    ),
                )
            )
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
        pool.instance_configuration_id = getattr(
            instance_pool,
            "instance_configuration_id",
            None,
        )
        pool.created_by_mgmt_oke = (
            pool.created_by_mgmt_oke
            or _is_mgmt_oke_created(
                getattr(instance_pool, "freeform_tags", None)
            )
        )
        pool.availability_domain = _first_placement_ad(instance_pool)
        compute_cluster_ids = _placement_compute_cluster_ids(instance_pool)
        if compute_cluster_ids:
            pool.compute_cluster_id = min(compute_cluster_ids)
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
        pool.kubernetes_version = (
            self._instance_configuration_kubernetes_version(
                pool.instance_configuration_id
            )
        )

    def _instance_configuration_kubernetes_version(
        self,
        instance_configuration_id: str | None,
    ) -> str | None:
        if not instance_configuration_id:
            return None
        try:
            configuration = self.compute_mgmt.get_instance_configuration(
                instance_configuration_id
            ).data
        except Exception:
            return None
        launch = getattr(
            getattr(configuration, "instance_details", None),
            "launch_details",
            None,
        )
        metadata = dict(getattr(launch, "metadata", None) or {})
        value = metadata.get("oke-k8version")
        return str(value) if value else None


def _build_managed_placement_configs(
    oci_module: Any,
    source_config: Any,
    spec: PoolCreateSpec,
) -> list[Any]:
    source_placements = list(
        getattr(source_config, "placement_configs", None) or []
    )
    if not source_placements:
        raise OciDiscoveryError(
            "Source managed node pool does not expose placement configurations."
        )
    if spec.availability_domain:
        matching = [
            placement
            for placement in source_placements
            if getattr(placement, "availability_domain", None)
            == spec.availability_domain
        ]
        source_placements = matching or [source_placements[0]]

    placement_model = (
        oci_module.container_engine.models.NodePoolPlacementConfigDetails
    )
    if spec.host_group_id and not _sdk_model_supports_field(
        placement_model,
        "host_group_id",
    ):
        raise OciDiscoveryError(
            "The installed OCI Python SDK cannot create managed node pools "
            "with Compute Host Groups. Upgrade the OCI SDK to the version "
            "declared by this project."
        )

    placements = []
    for source in source_placements:
        values: dict[str, Any] = {
            "availability_domain": (
                spec.availability_domain
                or getattr(source, "availability_domain", None)
            ),
            "subnet_id": (
                spec.primary_subnet_id
                or getattr(source, "subnet_id", None)
            ),
            "capacity_reservation_id": (
                spec.capacity_reservation_id
                or getattr(source, "capacity_reservation_id", None)
            ),
            "preemptible_node_config": deepcopy(
                getattr(source, "preemptible_node_config", None)
            ),
            "fault_domains": (
                []
                if spec.uses_compute_cluster
                else list(spec.fault_domains)
                if spec.fault_domains
                else list(getattr(source, "fault_domains", None) or [])
            ),
        }
        if spec.host_group_id:
            values["host_group_id"] = spec.host_group_id
        placements.append(placement_model(**values))
    if any(
        not getattr(placement, "availability_domain", None)
        or not getattr(placement, "subnet_id", None)
        for placement in placements
    ):
        raise OciDiscoveryError(
            "Managed node-pool placement requires an availability domain and subnet."
        )
    return placements


def _build_managed_pod_network(
    oci_module: Any,
    source_config: Any,
    spec: PoolCreateSpec,
) -> Any:
    source = getattr(
        source_config,
        "node_pool_pod_network_option_details",
        None,
    )
    if source is None:
        raise OciDiscoveryError(
            "Source managed node pool does not expose its pod network configuration."
        )
    source_cni = str(getattr(source, "cni_type", "") or "").upper()
    if not source_cni:
        raise OciDiscoveryError(
            "Source managed node pool pod network does not expose its CNI type."
        )
    if spec.cni_type and spec.cni_type != source_cni:
        raise OciDiscoveryError(
            f"Requested CNI {spec.cni_type} does not match source pool CNI "
            f"{source_cni}. OKE cluster CNI cannot be changed per pool."
        )
    if source_cni == "OCI_VCN_IP_NATIVE":
        pod_subnet_ids = (
            list(spec.pod_subnet_ids)
            if spec.pod_subnet_ids
            else list(getattr(source, "pod_subnet_ids", None) or [])
        )
        if not pod_subnet_ids:
            raise OciDiscoveryError(
                "VCN-native managed node pools require at least one pod subnet."
            )
        return (
            oci_module.container_engine.models
            .OciVcnIpNativeNodePoolPodNetworkOptionDetails(
                cni_type="OCI_VCN_IP_NATIVE",
                max_pods_per_node=(
                    spec.max_pods_per_node
                    if spec.max_pods_per_node is not None
                    else getattr(source, "max_pods_per_node", None)
                ),
                pod_nsg_ids=(
                    list(spec.pod_nsg_ids)
                    if spec.pod_nsg_ids
                    else list(getattr(source, "pod_nsg_ids", None) or [])
                ),
                pod_subnet_ids=pod_subnet_ids,
            )
        )
    if source_cni == "FLANNEL_OVERLAY":
        if spec.pod_subnet_ids or spec.pod_nsg_ids or spec.max_pods_per_node:
            raise OciDiscoveryError(
                "Pod subnets, pod NSGs, and maximum pods are VCN-native CNI settings."
            )
        return (
            oci_module.container_engine.models
            .FlannelOverlayNodePoolPodNetworkOptionDetails(
                cni_type="FLANNEL_OVERLAY"
            )
        )
    raise OciDiscoveryError(f"Unsupported managed node-pool CNI: {source_cni}")


def _build_managed_shape_config(
    oci_module: Any,
    source: Any,
    shape: str,
    spec: PoolCreateSpec,
) -> Any | None:
    source_config = getattr(source, "node_shape_config", None)
    if "Flex" not in shape:
        if spec.ocpus is not None or spec.memory_in_gbs is not None:
            raise OciDiscoveryError(
                "OCPU and memory overrides require an OCI Flex shape."
            )
        return None
    ocpus = (
        spec.ocpus
        if spec.ocpus is not None
        else getattr(source_config, "ocpus", None)
    )
    memory = (
        spec.memory_in_gbs
        if spec.memory_in_gbs is not None
        else getattr(source_config, "memory_in_gbs", None)
    )
    if ocpus is None or memory is None:
        raise OciDiscoveryError(
            "Flex shape creation requires both OCPU and memory values."
        )
    return oci_module.container_engine.models.CreateNodeShapeConfigDetails(
        ocpus=ocpus,
        memory_in_gbs=memory,
    )


def _retarget_managed_node_labels(
    oci_module: Any,
    source_labels: Any,
    name: str,
    extra_labels: tuple[tuple[str, str], ...],
) -> list[Any]:
    labels: dict[str, str] = {}
    for label in list(source_labels or []):
        key = (
            label.get("key")
            if isinstance(label, dict)
            else getattr(label, "key", None)
        )
        value = (
            label.get("value")
            if isinstance(label, dict)
            else getattr(label, "value", None)
        )
        if key:
            labels[str(key)] = "" if value is None else str(value)
    labels.pop("oke.oraclecloud.com/tf.module", None)
    labels.pop("oke.oraclecloud.com/tf.state_id", None)
    labels["oke.oraclecloud.com/pool.mode"] = "node-pool"
    labels["oke.oraclecloud.com/pool.name"] = name
    labels.update(dict(extra_labels))
    return [
        oci_module.container_engine.models.KeyValue(key=key, value=value)
        for key, value in labels.items()
    ]


def _build_node_cycling_details(
    oci_module: Any,
    source: Any,
    spec: PoolCreateSpec,
) -> Any | None:
    inherited = getattr(source, "node_pool_cycling_details", None)
    requested = any(
        value is not None
        for value in (
            spec.node_cycling_enabled,
            spec.node_cycling_max_surge,
            spec.node_cycling_max_unavailable,
            spec.node_cycling_mode,
        )
    )
    if inherited is None and not requested:
        return None
    return oci_module.container_engine.models.NodePoolCyclingDetails(
        is_node_cycling_enabled=(
            spec.node_cycling_enabled
            if spec.node_cycling_enabled is not None
            else getattr(inherited, "is_node_cycling_enabled", False)
        ),
        maximum_surge=(
            spec.node_cycling_max_surge
            or getattr(inherited, "maximum_surge", "1")
        ),
        maximum_unavailable=(
            spec.node_cycling_max_unavailable
            or getattr(inherited, "maximum_unavailable", "0")
        ),
        cycle_modes=(
            [spec.node_cycling_mode]
            if spec.node_cycling_mode
            else deepcopy(
                getattr(inherited, "cycle_modes", ["INSTANCE_REPLACE"])
            )
        ),
    )


def _build_node_eviction_settings(
    oci_module: Any,
    source: Any,
    spec: PoolCreateSpec,
) -> Any | None:
    inherited = getattr(source, "node_eviction_node_pool_settings", None)
    requested = any(
        value is not None
        for value in (
            spec.eviction_grace_duration,
            spec.force_delete_after_eviction_grace,
            spec.force_action_after_eviction_grace,
        )
    )
    if inherited is None and not requested:
        return None
    return oci_module.container_engine.models.NodeEvictionNodePoolSettings(
        eviction_grace_duration=(
            spec.eviction_grace_duration
            or getattr(inherited, "eviction_grace_duration", "PT5M")
        ),
        is_force_delete_after_grace_duration=(
            spec.force_delete_after_eviction_grace
            if spec.force_delete_after_eviction_grace is not None
            else getattr(
                inherited,
                "is_force_delete_after_grace_duration",
                True,
            )
        ),
        is_force_action_after_grace_duration=(
            spec.force_action_after_eviction_grace
            if spec.force_action_after_eviction_grace is not None
            else getattr(
                inherited,
                "is_force_action_after_grace_duration",
                True,
            )
        ),
    )


def _apply_rdma_shape_config(
    oci_module: Any,
    launch_details: Any,
    shape: str,
    spec: PoolCreateSpec,
) -> None:
    if "Flex" not in shape:
        if spec.ocpus is not None or spec.memory_in_gbs is not None:
            raise OciDiscoveryError(
                "OCPU and memory overrides require an OCI Flex shape."
            )
        return
    inherited = getattr(launch_details, "shape_config", None)
    ocpus = spec.ocpus if spec.ocpus is not None else getattr(inherited, "ocpus", None)
    memory = (
        spec.memory_in_gbs
        if spec.memory_in_gbs is not None
        else getattr(inherited, "memory_in_gbs", None)
    )
    if ocpus is None or memory is None:
        raise OciDiscoveryError(
            "Flex shape creation requires both OCPU and memory values."
        )
    if inherited is None:
        inherited = (
            oci_module.core.models
            .InstanceConfigurationLaunchInstanceShapeConfigDetails()
        )
        launch_details.shape_config = inherited
    inherited.ocpus = ocpus
    inherited.memory_in_gbs = memory


def _apply_rdma_source_details(
    launch_details: Any,
    spec: PoolCreateSpec,
) -> None:
    source = getattr(launch_details, "source_details", None)
    if source is None:
        raise OciDiscoveryError(
            "Source Instance Configuration does not expose image source details."
        )
    if spec.image_id:
        source.image_id = spec.image_id
    if spec.boot_volume_size_in_gbs is not None:
        source.boot_volume_size_in_gbs = spec.boot_volume_size_in_gbs
    if spec.boot_volume_vpus_per_gb is not None:
        source.boot_volume_vpus_per_gb = spec.boot_volume_vpus_per_gb
    if spec.boot_volume_kms_key_id:
        source.kms_key_id = spec.boot_volume_kms_key_id
    if not getattr(source, "image_id", None):
        raise OciDiscoveryError(
            "Source Instance Configuration does not expose an image OCID."
        )


def _managed_node_pool_create_preview(
    details: Any,
    spec: PoolCreateSpec,
) -> dict[str, Any]:
    config = details.node_config_details
    source = details.node_source_details
    pod_network = config.node_pool_pod_network_option_details
    placements = list(config.placement_configs or [])
    compute_cluster_id = getattr(config, "compute_cluster_id", None)
    host_group_ids = [
        host_group_id
        for placement in placements
        if (host_group_id := getattr(placement, "host_group_id", None))
    ]
    fault_domains = [
        list(getattr(placement, "fault_domains", None) or [])
        for placement in placements
    ]
    return {
        "backend": "oke-node-pool",
        "placement": (
            "compute-cluster"
            if compute_cluster_id or spec.creates_compute_cluster
            else "host-group" if host_group_ids else "standard"
        ),
        "name": details.name,
        "count": config.size,
        "shape": details.node_shape,
        "ocpus": getattr(details.node_shape_config, "ocpus", None),
        "memory_in_gbs": getattr(
            details.node_shape_config,
            "memory_in_gbs",
            None,
        ),
        "image_id": getattr(source, "image_id", None),
        "boot_volume_size_in_gbs": getattr(
            source,
            "boot_volume_size_in_gbs",
            None,
        ),
        "kubernetes_version": details.kubernetes_version,
        "availability_domains": [
            getattr(placement, "availability_domain", None)
            for placement in placements
        ],
        "primary_subnet_ids": [
            getattr(placement, "subnet_id", None)
            for placement in placements
        ],
        "fault_domains": fault_domains if any(fault_domains) else [],
        "compute_cluster_action": (
            "create"
            if spec.creates_compute_cluster
            else "use-existing" if compute_cluster_id else None
        ),
        "compute_cluster_id": compute_cluster_id,
        "compute_cluster_name": (
            spec.compute_cluster_name or f"{details.name}-cc"
            if spec.creates_compute_cluster
            else None
        ),
        "compute_cluster_compartment_id": (
            spec.compute_cluster_compartment_id or details.compartment_id
            if spec.creates_compute_cluster
            else None
        ),
        "host_group_ids": host_group_ids,
        "node_nsg_ids": list(config.nsg_ids or []),
        "cni_type": getattr(pod_network, "cni_type", None),
        "pod_subnet_ids": list(
            getattr(pod_network, "pod_subnet_ids", None) or []
        ),
        "pod_nsg_ids": list(
            getattr(pod_network, "pod_nsg_ids", None) or []
        ),
        "max_pods_per_node": getattr(
            pod_network,
            "max_pods_per_node",
            None,
        ),
        "worker_bootstrap": _worker_bootstrap_preview(details.node_metadata),
        "storage": _storage_preview(spec),
    }


def _string_metadata(value: Any, *, source: str) -> dict[str, str]:
    metadata = dict(value or {})
    invalid = sorted(
        str(key)
        for key, item in metadata.items()
        if not isinstance(key, str) or not isinstance(item, str)
    )
    if invalid:
        raise OciDiscoveryError(
            f"{source} contains non-string metadata values: {', '.join(invalid)}."
        )
    return metadata


def _worker_bootstrap_preview(metadata: Mapping[str, str]) -> dict[str, Any]:
    try:
        return summarize_worker_bootstrap(metadata)
    except BootstrapCompositionError as exc:
        raise OciDiscoveryError(
            f"Worker cloud-init cannot be inspected safely: {exc}"
        ) from exc


def _required_oke_bootstrap_metadata(
    value: Any,
    *,
    source: str,
) -> dict[str, str]:
    metadata = _string_metadata(value, source=source)
    required = (
        "apiserver_host",
        "cluster_ca_cert",
        "oke-initial-node-labels",
        "user_data",
    )
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise OciDiscoveryError(
            f"{source} is missing required OKE bootstrap metadata: "
            f"{', '.join(missing)}"
        )
    return metadata


def _merge_legacy_bootstrap_metadata(
    managed_metadata: Mapping[str, str],
    legacy_metadata: Mapping[str, str],
) -> dict[str, str]:
    current = _string_metadata(
        managed_metadata,
        source="Managed OKE source pool",
    )
    legacy = _required_oke_bootstrap_metadata(
        legacy_metadata,
        source="Legacy bootstrap source Instance Configuration",
    )
    for key in _CLUSTER_IDENTITY_METADATA_KEYS:
        current_value = current.get(key)
        legacy_value = legacy.get(key)
        if current_value and legacy_value and current_value != legacy_value:
            raise OciDiscoveryError(
                "Legacy bootstrap source does not belong to the same OKE cluster: "
                f"{key} differs from the managed source pool."
            )

    merged = dict(current)
    for key in _CLUSTER_IDENTITY_METADATA_KEYS:
        if not merged.get(key) and legacy.get(key):
            merged[key] = legacy[key]
    merged.update(
        {
            key: value
            for key, value in legacy.items()
            if key not in _MANAGED_BOOTSTRAP_AUTHORITY_KEYS
        }
    )
    merged["user_data"] = legacy["user_data"]
    return merged


def _requires_user_data_composition(spec: PoolCreateSpec) -> bool:
    return bool(
        spec.cloud_init is not None
        or spec.kubernetes_version
        or spec.ssh_public_key
        or spec.storage_mode != "inherit"
        or spec.nvme_raid
        or spec.fss_mounts
        or spec.lustre_mounts
    )


def _storage_preview(spec: PoolCreateSpec) -> dict[str, Any]:
    return {
        "mode": spec.storage_mode,
        "nvme_raid": spec.nvme_raid.as_dict() if spec.nvme_raid else None,
        "fss_mounts": [mount.as_dict() for mount in spec.fss_mounts],
        "lustre_mounts": [mount.as_dict() for mount in spec.lustre_mounts],
    }


def _validate_shape_for_pool_type(shape: str, pool_type: str) -> None:
    is_gpu = "GPU" in shape.upper()
    if pool_type == "cpu" and is_gpu:
        raise OciDiscoveryError(
            f"CPU pool type cannot use GPU shape: {shape}"
        )
    if pool_type == "gpu" and not is_gpu:
        raise OciDiscoveryError(
            f"GPU pool type requires a GPU shape: {shape}"
        )
    if pool_type == "rdma" and not shape.upper().startswith("BM.GPU"):
        raise OciDiscoveryError(
            f"RDMA pool type requires a BM.GPU shape: {shape}"
        )


def _split_metadata_values(value: Any) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


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


def _addon_version_supports_kubernetes(
    addon_version: Any,
    target_version: str,
) -> bool:
    filters = getattr(addon_version, "kubernetes_version_filters", None)
    if filters is None:
        return True
    exact = tuple(
        str(value)
        for value in (
            getattr(filters, "exact_kubernetes_versions", None) or ()
        )
    )
    if exact:
        return target_version in exact
    try:
        from oke_hpc_mgmt.upgrades import KubernetesVersion

        target = KubernetesVersion.parse(target_version)
        minimum_value = getattr(filters, "minimal_version", None)
        maximum_value = getattr(filters, "maximum_version", None)
        minimum = (
            KubernetesVersion.parse(str(minimum_value))
            if minimum_value
            else None
        )
        maximum = (
            KubernetesVersion.parse(str(maximum_value))
            if maximum_value
            else None
        )
    except ValueError:
        return False
    if minimum and target < minimum:
        return False
    if maximum and target > maximum:
        return False
    return True


def _work_request_error(error: Any) -> str:
    code = getattr(error, "code", None)
    message = getattr(error, "message", None) or str(error)
    return f"{code}: {message}" if code else str(message)


def _operation_retry_token(operation_id: str, *parts: str) -> str:
    value = ":".join(("mgmt-oke", operation_id, *parts))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


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


def _compute_cluster_info(resource: Any) -> ComputeClusterInfo:
    compute_cluster_id = str(getattr(resource, "id", "") or "")
    availability_domain = str(
        getattr(resource, "availability_domain", "") or ""
    )
    compartment_id = str(getattr(resource, "compartment_id", "") or "")
    if not compute_cluster_id or not availability_domain or not compartment_id:
        raise OciDiscoveryError(
            "Compute Cluster response is missing its OCID, compartment, or "
            "availability domain."
        )
    return ComputeClusterInfo(
        compute_cluster_id=compute_cluster_id,
        display_name=str(
            getattr(resource, "display_name", "") or compute_cluster_id
        ),
        availability_domain=availability_domain,
        compartment_id=compartment_id,
        lifecycle_state=str(
            getattr(resource, "lifecycle_state", "") or "UNKNOWN"
        ),
    )


def _sdk_model_supports_field(model_type: Any, field_name: str) -> bool:
    try:
        model = model_type()
    except Exception:
        return False
    return field_name in dict(getattr(model, "swagger_types", {}) or {})


def _clone_pool_freeform_tags(
    source_tags: dict[str, str] | None,
    name: str,
) -> dict[str, str]:
    tags = dict(source_tags or {})
    tags.pop("state_id", None)
    tags["pool"] = name
    tags.setdefault("role", "worker")
    tags["mgmt-oke-created"] = "true"
    return tags


def _is_mgmt_oke_created(tags: dict[str, str] | None) -> bool:
    return str((tags or {}).get("mgmt-oke-created", "")).lower() == "true"


def _retarget_vnic_tags(vnic_details: Any, name: str) -> None:
    if vnic_details is None:
        return
    vnic_details.freeform_tags = _clone_pool_freeform_tags(
        getattr(vnic_details, "freeform_tags", None),
        name,
    )


def _retarget_oke_node_labels(
    labels: str,
    name: str,
    extra_labels: tuple[tuple[str, str], ...] = (),
) -> str:
    replacements = {
        "oke.oraclecloud.com/pool.mode": "cluster-network",
        "oke.oraclecloud.com/pool.name": name,
    }
    removed = {
        "oke.oraclecloud.com/tf.module",
        "oke.oraclecloud.com/tf.state_id",
    }
    parsed: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    for raw_entry in labels.split(","):
        entry = raw_entry.strip()
        if not entry:
            raise OciDiscoveryError(
                "Source Instance Configuration contains an empty initial node label."
            )
        key, separator, value = entry.partition("=")
        if not key or key != key.strip():
            raise OciDiscoveryError(
                "Source Instance Configuration contains an invalid initial node label."
            )
        if key in seen:
            raise OciDiscoveryError(
                f"Source Instance Configuration repeats initial node label: {key}"
            )
        seen.add(key)
        if key in removed:
            continue
        if key in replacements:
            parsed.append((key, replacements[key], True))
        else:
            parsed.append((key, value, bool(separator)))

    for key, value in replacements.items():
        if key not in seen:
            parsed.append((key, value, True))
    extras = dict(extra_labels)
    updated: list[tuple[str, str, bool]] = []
    for key, value, has_separator in parsed:
        if key in extras:
            updated.append((key, extras.pop(key), True))
        else:
            updated.append((key, value, has_separator))
    updated.extend((key, value, True) for key, value in extras.items())

    return ",".join(
        f"{key}={value}" if has_separator else key
        for key, value, has_separator in updated
    )
