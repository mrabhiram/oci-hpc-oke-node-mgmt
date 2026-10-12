from __future__ import annotations

import copy
import json
import re
import secrets
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from oke_hpc_mgmt.benchmarks.manifests import (
    MPI_GROUP, MPI_PLURAL, MPI_VERSION, ExecutionOptions, TopologyPlacement, render_mpijob,
)
from oke_hpc_mgmt.benchmarks.models import BenchmarkCase, BenchmarkError, HealthcheckPlan
from oke_hpc_mgmt.benchmarks.preflight import GPU_RESOURCE, VF_RESOURCE
from oke_hpc_mgmt.benchmarks.profiles import get_profile

if TYPE_CHECKING:
    from oke_hpc_mgmt.benchmarks.backend import BenchmarkBackend


REQUEST_TIMEOUT = (10, 30)
KUEUE_GROUP = "kueue.x-k8s.io"
LEASE_NAMESPACE = "kube-system"
LEASE_NAME = "mgmt-oke-healthcheck-submission"


@dataclass(frozen=True)
class Permission:
    group: str
    resource: str
    verb: str
    namespace: str = ""
    subresource: str = ""


@dataclass(frozen=True)
class CapabilityEvidence:
    scheduler: Literal["direct", "kueue"]
    namespace_uid: str
    mpi_operator_version: str
    kueue_version: str | None
    cluster_queue: str | None
    compatible_flavors: tuple[tuple[str, tuple[str, ...]], ...]
    resource_versions: tuple[tuple[str, str, str], ...]
    warnings: tuple[str, ...]
    execution_options: ExecutionOptions


@dataclass(frozen=True)
class QueueEvidence:
    cluster_queue: str
    flavors: tuple[tuple[str, tuple[str, ...]], ...]
    placements: tuple[TopologyPlacement, ...]
    resource_versions: tuple[tuple[str, str, str], ...]


def topology_placement(
    case: BenchmarkCase, topology: dict[str, Any], nodes: dict[str, dict[str, Any]], *, per_local_block: bool,
) -> TopologyPlacement:
    levels = (topology.get("spec") or {}).get("levels") or []
    keys = [level.get("nodeLabel") for level in levels if isinstance(level, dict)]
    if len(keys) != len(levels) or not keys or any(not isinstance(key, str) or not key for key in keys) or (
        len(set(keys)) != len(keys) or keys[-1] != "kubernetes.io/hostname"
    ):
        raise BenchmarkError("Exact-node benchmarks require a valid Topology with kubernetes.io/hostname as its leaf.")
    labels = [(nodes[target.name].get("metadata") or {}).get("labels") or {} for target in case.nodes]
    if any(not label.get(key) for label in labels for key in keys):
        raise BenchmarkError("Every selected node must have all labels of the queue's Topology.")
    local_key = "oci.oraclecloud.com/rdma.local_block_id"
    required = per_local_block and case.check_type == "multi-node"
    if required:
        if local_key not in keys:
            raise BenchmarkError("Per-local-block cases require a queue Topology containing the local block level.")
        parents = keys[:keys.index(local_key) + 1]
        if len({tuple(label[key] for key in parents) for label in labels}) != 1:
            raise BenchmarkError("Per-local-block participants do not share the same complete topology domain.")
    placement = TopologyPlacement(case.case_id, local_key if local_key in keys else keys[-1], required,
                                  tuple(label["kubernetes.io/hostname"] for label in labels))
    placement.validate()
    return placement


def label_selector_matches(selector: dict[str, Any] | None, labels: dict[str, str]) -> bool:
    # A null ClusterQueue namespace selector admits no namespaces; {} admits all.
    if selector is None:
        return False
    if set(selector) - {"matchLabels", "matchExpressions"}:
        raise BenchmarkError("Unsupported namespace selector fields.")
    if any(labels.get(key) != value for key, value in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if not key or not isinstance(values, list):
            raise BenchmarkError("Invalid namespace selector requirement.")
        if operator in {"In", "NotIn"}:
            if not values:
                raise BenchmarkError("Set-based namespace selector needs values.")
            match = labels.get(key) in values if operator == "In" else labels.get(key) not in values
        elif operator in {"Exists", "DoesNotExist"}:
            if values:
                raise BenchmarkError("Existence namespace selectors do not accept values.")
            match = (key in labels) if operator == "Exists" else (key not in labels)
        else:
            raise BenchmarkError(f"Unsupported namespace selector operator: {operator}.")
        if not match:
            return False
    return True


def served_version(crd: dict[str, Any], versions: tuple[str, ...], *, namespaced: bool) -> str:
    spec, metadata = crd.get("spec") or {}, crd.get("metadata") or {}
    conditions = {item.get("type"): item.get("status") for item in (crd.get("status") or {}).get("conditions") or []}
    if metadata.get("deletionTimestamp") or conditions.get("Established") != "True":
        raise BenchmarkError(f"CRD {metadata.get('name')} is not established.")
    if spec.get("scope") != ("Namespaced" if namespaced else "Cluster"):
        raise BenchmarkError(f"Unexpected scope for CRD {metadata.get('name')}.")
    available = {item.get("name") for item in spec.get("versions") or [] if item.get("served") is True}
    for version in versions:
        if version in available:
            return version
    raise BenchmarkError(f"CRD {metadata.get('name')} does not serve a supported API version.")


def required_permissions(namespace: str) -> tuple[Permission, ...]:
    permissions = [
        Permission("", "nodes", "list"), Permission("", "nodes", "get"),
        Permission("", "pods", "list"), Permission("", "pods", "get", namespace),
        Permission("", "pods", "get", namespace, "log"),
        Permission("", "pods", "create", namespace),  # Server-side dry-run admission probes only.
        Permission("", "configmaps", "list"), Permission("batch", "jobs", "get", namespace),
        Permission("apps", "deployments", "list"),
    ]
    for resource, group, verbs in (
        ("configmaps", "", ("get", "create", "patch")),
        (MPI_PLURAL, MPI_GROUP, ("get", "create", "patch", "delete")),
    ):
        permissions.extend(Permission(group, resource, verb, namespace) for verb in verbs)
    permissions.extend(Permission("coordination.k8s.io", "leases", verb, LEASE_NAMESPACE)
                       for verb in ("get", "create", "update", "delete"))
    return tuple(permissions)


def validate_nad(nad: dict[str, Any], namespace: str, name: str) -> None:
    metadata = nad.get("metadata") or {}
    if metadata.get("name") != name or metadata.get("namespace") != namespace or metadata.get("deletionTimestamp"):
        raise BenchmarkError("The selected RDMA network attachment is missing, replaced, or terminating.")
    if (metadata.get("annotations") or {}).get("k8s.v1.cni.cncf.io/resourceName") != VF_RESOURCE:
        raise BenchmarkError(f"RDMA network attachment must use device resource {VF_RESOURCE}.")
    try:
        config = json.loads((nad.get("spec") or {}).get("config", ""))
    except (TypeError, ValueError) as exc:
        raise BenchmarkError("RDMA network attachment has invalid CNI JSON.") from exc
    if not isinstance(config, dict):
        raise BenchmarkError("RDMA network attachment requires a CNI object.")
    plugins = config.get("plugins", [config])
    if not isinstance(plugins, list) or not all(isinstance(plugin, dict) for plugin in plugins) or not any(
        plugin.get("type") in {"sriov", "ib-sriov"} for plugin in plugins
    ):
        raise BenchmarkError("RDMA VF profile requires an existing SR-IOV network attachment.")


def validate_driver_hint(case: BenchmarkCase, nodes: dict[str, dict[str, Any]], image: str) -> tuple[str, ...]:
    # Image tags and GFD labels are preflight hints, not executable/runtime proof.
    # CUDA minor compatibility is only a lower bound; the worker must still start
    # and supply actual runtime and NCCL completion evidence.
    version = re.search(r":cuda-(12|13)\.[0-9]+\.[0-9]+-", image)
    if version is None:
        return ("Custom image CUDA/driver compatibility is unverified until worker runtime validation.",)
    minimum = 525 if version[1] == "12" else 580
    warnings: list[str] = []
    for target in case.nodes:
        labels = (nodes[target.name].get("metadata") or {}).get("labels") or {}
        major = labels.get("nvidia.com/cuda.driver.major", "")
        if not re.fullmatch(r"[0-9]+", str(major)):
            warnings.append(f"GPU driver version is not reported for {target.name}; runtime validation is required.")
        elif int(major) < minimum:
            raise BenchmarkError(f"{target.name} driver {major} is below CUDA {version[1]}.x minimum {minimum}.")
    warnings.append("Image tag and node driver labels do not prove runtime compatibility; worker validation is required.")
    return tuple(warnings)


class CapabilityPreflight:
    """API reads and server dry-runs; never installs or modifies cluster policy."""

    def __init__(self, backend: BenchmarkBackend) -> None:
        self.backend = backend
        self.core, self.custom = backend.core, backend.custom
        self.extensions = backend.client.ApiextensionsV1Api()
        self.authorization = backend.client.AuthorizationV1Api()
        self.apps = backend.client.AppsV1Api()

    def _crd(self, name: str, *, optional: bool = False) -> dict[str, Any] | None:
        try:
            return self.backend._object(self.extensions.read_custom_resource_definition(
                name, _request_timeout=REQUEST_TIMEOUT,
            ))
        except ApiException as exc:
            if optional and exc.status == 404:
                return None
            raise BenchmarkError(f"Cannot verify CRD {name} (Kubernetes HTTP {exc.status}).") from exc

    def _permissions(self, namespace: str) -> None:
        denied: list[str] = []
        for permission in required_permissions(namespace):
            attributes = {"group": permission.group, "resource": permission.resource, "verb": permission.verb}
            if permission.namespace:
                attributes["namespace"] = permission.namespace
            if permission.subresource:
                attributes["subresource"] = permission.subresource
            review = self.backend._object(self.authorization.create_self_subject_access_review(
                {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                 "spec": {"resourceAttributes": attributes}}, _request_timeout=REQUEST_TIMEOUT,
            ))
            status = review.get("status") or {}
            if status.get("allowed") is not True or status.get("denied") is True or status.get("evaluationError"):
                denied.append(f"{permission.verb} {permission.resource}"
                              f"{('/' + permission.subresource) if permission.subresource else ''} "
                              f"in {permission.namespace or 'cluster scope'}")
        if denied:
            raise BenchmarkError("Required benchmark permissions are denied or unverified: " + "; ".join(denied))

    @staticmethod
    def _identity(obj: dict[str, Any], kind: str) -> tuple[str, str, str]:
        metadata = obj.get("metadata") or {}
        if not all(metadata.get(key) for key in ("name", "uid", "resourceVersion")) or metadata.get("deletionTimestamp"):
            raise BenchmarkError(f"Cannot establish current {kind} identity.")
        return f"{kind}/{metadata['name']}", metadata["uid"], metadata["resourceVersion"]

    @staticmethod
    def _active_queue(queue: dict[str, Any], kind: str) -> None:
        if (queue.get("spec") or {}).get("stopPolicy", "None") != "None":
            raise BenchmarkError(f"{kind} is held; benchmarks cannot bypass scheduler holds.")
        active = [item for item in (queue.get("status") or {}).get("conditions") or []
                  if item.get("type") == "Active" and item.get("status") == "True"]
        generation = (queue.get("metadata") or {}).get("generation")
        if len(active) != 1 or (generation is not None and active[0].get("observedGeneration") != generation):
            raise BenchmarkError(f"{kind} readiness is not verified for its current generation.")

    def _queue(
        self, plan: HealthcheckPlan, version: str, namespace: dict[str, Any], nodes: dict[str, dict[str, Any]],
    ) -> QueueEvidence:
        local = self.backend._object(self.custom.get_namespaced_custom_object(
            KUEUE_GROUP, version, plan.request.namespace, "localqueues", plan.request.queue,
            _request_timeout=REQUEST_TIMEOUT,
        ))
        identities = [self._identity(local, "LocalQueue")]
        self._active_queue(local, "LocalQueue")
        name = (local.get("spec") or {}).get("clusterQueue")
        if not isinstance(name, str) or not name:
            raise BenchmarkError("LocalQueue has no ClusterQueue reference.")
        cluster = self.backend._object(self.custom.get_cluster_custom_object(
            KUEUE_GROUP, version, "clusterqueues", name, _request_timeout=REQUEST_TIMEOUT,
        ))
        identities.append(self._identity(cluster, "ClusterQueue"))
        self._active_queue(cluster, "ClusterQueue")
        spec = cluster.get("spec") or {}
        if not label_selector_matches(spec.get("namespaceSelector"), (namespace.get("metadata") or {}).get("labels") or {}):
            raise BenchmarkError("ClusterQueue does not admit the selected namespace.")
        flavor_cache: dict[str, dict[str, Any]] = {}
        topology_cache: dict[str, dict[str, Any]] = {}
        placements: list[TopologyPlacement] = []
        matched: list[tuple[str, tuple[str, ...]]] = []
        for case in plan.cases:
            needed = {GPU_RESOURCE} | ({VF_RESOURCE} if case.network == "vf" else set())
            eligible: set[str] = set()
            flavor_resources: dict[str, set[str]] = {}
            matched_resources: set[str] = set()
            for group in spec.get("resourceGroups") or []:
                covered = set(group.get("coveredResources") or [])
                required = needed & covered
                if not required:
                    continue
                for entry in group.get("flavors") or []:
                    if not required <= {resource.get("name") for resource in entry.get("resources") or []}:
                        continue
                    flavor_name = entry.get("name")
                    if not isinstance(flavor_name, str) or not flavor_name:
                        raise BenchmarkError("ClusterQueue contains an invalid ResourceFlavor reference.")
                    if flavor_name not in flavor_cache:
                        flavor_cache[flavor_name] = self.backend._object(self.custom.get_cluster_custom_object(
                            KUEUE_GROUP, version, "resourceflavors", flavor_name, _request_timeout=REQUEST_TIMEOUT,
                        ))
                        identities.append(self._identity(flavor_cache[flavor_name], "ResourceFlavor"))
                    labels = (flavor_cache[flavor_name].get("spec") or {}).get("nodeLabels") or {}
                    if all(all((nodes[target.name]["metadata"].get("labels") or {}).get(key) == value
                               for key, value in labels.items()) for target in case.nodes):
                        eligible.add(flavor_name)
                        flavor_resources.setdefault(flavor_name, set()).update(required)
                        matched_resources.update(required)
            if matched_resources != needed:
                raise BenchmarkError(f"No ClusterQueue GPU/RDMA flavor matches every selected node in {case.case_id}.")
            topology_names = {(flavor_cache[flavor].get("spec") or {}).get("topologyName") for flavor in eligible}
            topology_names.discard(None)
            topology_names.discard("")
            if len(topology_names) > 1:
                raise BenchmarkError("Matching queue flavors have incompatible topologies; select an unambiguous queue.")
            topology_name = next(iter(topology_names), None)
            if topology_name:
                # Upstream queues also contain a non-TAS, zero-GPU flavor for
                # the launcher. A TAS worker request must not treat it as an
                # incompatible worker topology or remove it from the queue.
                eligible = {flavor for flavor in eligible
                            if (flavor_cache[flavor].get("spec") or {}).get("topologyName") == topology_name}
                if set().union(*(flavor_resources[flavor] for flavor in eligible)) != needed:
                    raise BenchmarkError("Topology-aware flavors do not cover all required GPU/RDMA resources.")
                if not isinstance(topology_name, str):
                    raise BenchmarkError("ResourceFlavor has an invalid Topology reference.")
                if topology_name not in topology_cache:
                    if not topology_cache:
                        crd = self._crd(f"topologies.{KUEUE_GROUP}")
                        assert crd is not None
                        served_version(crd, (version,), namespaced=False)
                        identities.append(self._identity(crd, "CRD"))
                    topology_cache[topology_name] = self.backend._object(self.custom.get_cluster_custom_object(
                        KUEUE_GROUP, version, "topologies", topology_name, _request_timeout=REQUEST_TIMEOUT,
                    ))
                    identities.append(self._identity(topology_cache[topology_name], "Topology"))
                placements.append(topology_placement(case, topology_cache[topology_name], nodes,
                                                    per_local_block=plan.request.mode == "per-local-block"))
            matched.append((case.case_id, tuple(sorted(eligible))))
        return QueueEvidence(name, tuple(matched), tuple(placements), tuple(identities))

    def _mpi_controller(self) -> tuple[str, tuple[str, str, str]]:
        response = self.backend._object(self.apps.list_deployment_for_all_namespaces(
            label_selector="app.kubernetes.io/name=mpi-operator", _request_timeout=REQUEST_TIMEOUT,
        ))
        deployments = response.get("items") or []
        if (response.get("metadata") or {}).get("continue") or len(deployments) != 1:
            raise BenchmarkError("Cannot verify one MPI Operator deployment; resolve missing or ambiguous controllers.")
        deployment = deployments[0]
        identity = self._identity(deployment, "Deployment")
        metadata, status, spec = deployment["metadata"], deployment.get("status") or {}, deployment.get("spec") or {}
        if status.get("observedGeneration") != metadata.get("generation") or not metadata.get("generation") or (
            status.get("availableReplicas", 0) < 1 or spec.get("replicas", 1) < 1
        ):
            raise BenchmarkError("MPI Operator deployment is not available at its current generation.")
        containers = (spec.get("template") or {}).get("spec", {}).get("containers") or []
        images = [container.get("image", "") for container in containers if container.get("name") == "mpi-operator"]
        version = re.search(r":v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:@sha256:[a-f0-9]{64})?$", images[0]) if len(images) == 1 else None
        if version is None or tuple(int(part) for part in version.groups()) < (0, 8, 0):
            raise BenchmarkError("MPI Operator v0.8.0 or newer must be verifiable from its deployed image tag.")
        return ".".join(version.groups()), identity

    def check(self, plan: HealthcheckPlan, options: ExecutionOptions, *, probe_admission: bool = True) -> CapabilityEvidence:
        plan.request.validate()
        options.validate()
        if not plan.cases:
            raise BenchmarkError("Workload capability preflight requires an active benchmark plan.")
        namespace_name = plan.request.namespace
        versions: list[tuple[str, str, str]] = []
        warnings: list[str] = []
        try:
            namespace = self.backend._object(self.core.read_namespace(namespace_name, _request_timeout=REQUEST_TIMEOUT))
            namespace_identity = self._identity(namespace, "Namespace")
            versions.append(namespace_identity)
            if (namespace.get("status") or {}).get("phase") != "Active":
                raise BenchmarkError("Benchmark namespace is not Active.")
            policy = (namespace["metadata"].get("labels") or {}).get("pod-security.kubernetes.io/enforce")
            if policy in {"baseline", "restricted"}:
                raise BenchmarkError("NCCL SSH/IPC_LOCK and RDMA profiles require a namespace permitting these privileges.")
            self._permissions(namespace_name)
            mpi = self._crd(f"{MPI_PLURAL}.{MPI_GROUP}")
            assert mpi is not None
            served_version(mpi, (MPI_VERSION,), namespaced=True)
            versions.append(self._identity(mpi, "CRD"))
            mpi_version, mpi_identity = self._mpi_controller()
            versions.append(mpi_identity)
            kueue = self._crd(f"localqueues.{KUEUE_GROUP}", optional=True)
            kueue_version: str | None = None
            cluster_queue: str | None = None
            flavors: tuple[tuple[str, tuple[str, ...]], ...] = ()
            resolved_options = replace(options, placements=())
            scheduler: Literal["direct", "kueue"] = "direct"
            if kueue is not None:
                if plan.request.scheduler == "direct" or not plan.request.queue:
                    raise BenchmarkError("Kueue is installed; select an existing --queue. Direct execution cannot bypass it.")
                kueue_version = served_version(kueue, ("v1beta2", "v1beta1"), namespaced=True)
                versions.append(self._identity(kueue, "CRD"))
                for plural, namespaced in (("clusterqueues", False), ("resourceflavors", False), ("workloads", True)):
                    crd = self._crd(f"{plural}.{KUEUE_GROUP}")
                    assert crd is not None
                    served_version(crd, (kueue_version,), namespaced=namespaced)
                    versions.append(self._identity(crd, "CRD"))
                scheduler = "kueue"
            elif plan.request.scheduler == "kueue" or plan.request.queue:
                raise BenchmarkError("Kueue was requested but its LocalQueue API is not installed.")
            node_list = self.backend._object(self.core.list_node(_request_timeout=REQUEST_TIMEOUT))
            if (node_list.get("metadata") or {}).get("continue"):
                raise BenchmarkError("Node capability inventory is incomplete.")
            nodes = {node["metadata"]["name"]: node for node in node_list.get("items") or []}
            for case in plan.cases:
                if any(target.name not in nodes or nodes[target.name]["metadata"].get("uid") != target.uid for target in case.nodes):
                    raise BenchmarkError("Selected node identity changed before capability preflight.")
                warnings.extend(validate_driver_hint(case, nodes, plan.request.image or get_profile(case.nodes[0].shape).image))
            if kueue_version:
                queue = self._queue(plan, kueue_version, namespace, nodes)
                cluster_queue, flavors = queue.cluster_queue, queue.flavors
                versions.extend(queue.resource_versions)
                resolved_options = replace(options, placements=queue.placements)
                warnings.append("Queue/flavor compatibility does not reserve capacity or guarantee admission time.")
            if any(case.network == "vf" for case in plan.cases):
                nad_crd = self._crd("network-attachment-definitions.k8s.cni.cncf.io")
                assert nad_crd is not None
                served_version(nad_crd, ("v1",), namespaced=True)
                nad = self.backend._object(self.custom.get_namespaced_custom_object(
                    "k8s.cni.cncf.io", "v1", namespace_name, "network-attachment-definitions",
                    options.network_attachment, _request_timeout=REQUEST_TIMEOUT,
                ))
                validate_nad(nad, namespace_name, options.network_attachment)
                versions.extend((self._identity(nad_crd, "CRD"), self._identity(nad, "NetworkAttachmentDefinition")))
            if probe_admission:
                self._admission(plan, resolved_options, scheduler)
            else:
                warnings.append("Server admission probes were not performed; submission is not validated.")
            warnings.append("Runtime startup still requires observed workload convergence.")
            return CapabilityEvidence(scheduler, namespace_identity[1], mpi_version, kueue_version, cluster_queue,
                                      flavors, tuple(versions), tuple(dict.fromkeys(warnings)), resolved_options)
        except ApiException as exc:
            raise BenchmarkError(f"Benchmark capability validation failed (Kubernetes HTTP {exc.status}).") from exc
        except (HTTPError, OSError) as exc:
            raise BenchmarkError("Benchmark capability validation could not reach the API; capability is unverified.") from exc

    def _admission(self, plan: HealthcheckPlan, options: ExecutionOptions, scheduler: str) -> None:
        run_id = "hc-" + secrets.token_hex(10)
        for case in plan.cases:
            body = render_mpijob(run_id, case, plan.request, options)
            # With a queue, submit an unsuspended *dry-run* copy to prove the
            # installed Kueue admission webhook intercepts this namespace/job.
            # The real renderer still always creates queued jobs suspended.
            if scheduler == "kueue":
                body["spec"]["runPolicy"]["suspend"] = False
            admitted = self.backend._object(self.custom.create_namespaced_custom_object(
                MPI_GROUP, MPI_VERSION, plan.request.namespace, MPI_PLURAL, body,
                dry_run="All", _request_timeout=REQUEST_TIMEOUT,
            ))
            if scheduler == "kueue" and (admitted.get("spec") or {}).get("runPolicy", {}).get("suspend") is not True:
                raise BenchmarkError("Kueue did not suspend the MPIJob admission probe; verify MPI integration and namespace policy.")
            for role, replica in body["spec"]["mpiReplicaSpecs"].items():
                template = copy.deepcopy(replica["template"])
                template["metadata"].update(name=f"{body['metadata']['name']}-{role.lower()}-probe",
                                            namespace=plan.request.namespace)
                self.core.create_namespaced_pod(
                    plan.request.namespace, {"apiVersion": "v1", "kind": "Pod", **template},
                    dry_run="All", _request_timeout=REQUEST_TIMEOUT,
                )
