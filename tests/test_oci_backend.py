import unittest
from types import SimpleNamespace

from oke_hpc_mgmt.backends.oci import (
    BootVolumeAttachmentPending,
    OciBackend,
    OciDiscoveryError,
    _clone_pool_freeform_tags,
    _retarget_oke_node_labels,
)
from oke_hpc_mgmt.models import (
    AddonInfo,
    PoolBootVolumeReplaceSpec,
    PoolCreateSpec,
    WorkerPoolInfo,
)


class _Model:
    swagger_types = {
        "compute_cluster_id": "str",
        "host_group_id": "str",
    }

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _LegacyModel:
    swagger_types: dict[str, str] = {}

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ComputeManagement:
    def __init__(
        self,
        cluster_network=None,
        instance_configuration=None,
        instance_pools=None,
        instances=None,
    ):
        self.cluster_network = cluster_network
        self.instance_configuration = instance_configuration
        self.instance_pools = instance_pools or []
        self.instances = instances or {}
        self.calls = []

    def get_cluster_network(self, cluster_network_id):
        self.calls.append(("get_cluster_network", cluster_network_id))
        return SimpleNamespace(
            data=self.cluster_network,
            headers={"etag": "cluster-network-etag"},
        )

    def list_cluster_networks(self, compartment_id):
        self.calls.append(("list_cluster_networks", compartment_id))
        cluster_networks = (
            [] if self.cluster_network is None else [self.cluster_network]
        )
        return SimpleNamespace(data=cluster_networks)

    def update_cluster_network(self, cluster_network_id, details, **kwargs):
        call = ("update_cluster_network", cluster_network_id, details)
        self.calls.append(call + (kwargs,) if kwargs else call)
        return SimpleNamespace(headers={"opc-work-request-id": "wr-cluster-network"})

    def create_cluster_network(self, details, **kwargs):
        self.calls.append(("create_cluster_network", details, kwargs))
        return SimpleNamespace(
            data=SimpleNamespace(
                id="cluster-network-new",
                instance_pools=[SimpleNamespace(id="instance-pool-new")],
            ),
            headers={"opc-work-request-id": "wr-cluster-network-create"},
        )

    def get_instance_configuration(self, instance_configuration_id):
        self.calls.append(("get_instance_configuration", instance_configuration_id))
        return SimpleNamespace(data=self.instance_configuration)

    def create_instance_configuration(self, details, **kwargs):
        self.calls.append(("create_instance_configuration", details, kwargs))
        return SimpleNamespace(
            data=SimpleNamespace(id="instance-configuration-new"),
            headers={},
        )

    def delete_instance_configuration(self, instance_configuration_id):
        self.calls.append(
            ("delete_instance_configuration", instance_configuration_id)
        )
        return SimpleNamespace(headers={})

    def update_instance_pool(self, instance_pool_id, details, **kwargs):
        call = ("update_instance_pool", instance_pool_id, details)
        self.calls.append(call + (kwargs,) if kwargs else call)
        return SimpleNamespace(headers={"opc-work-request-id": "wr-instance-pool"})

    def create_instance_pool(self, details, **kwargs):
        self.calls.append(("create_instance_pool", details, kwargs))
        return SimpleNamespace(
            data=SimpleNamespace(id="instance-pool-blue"),
            headers={"opc-work-request-id": "wr-instance-pool-blue"},
        )

    def get_instance_pool(self, instance_pool_id):
        self.calls.append(("get_instance_pool", instance_pool_id))
        instance_pool = next(
            pool
            for pool in self.instance_pools
            if pool.id == instance_pool_id
        )
        return SimpleNamespace(
            data=instance_pool,
            headers={"etag": "instance-pool-etag"},
        )

    def terminate_cluster_network(self, cluster_network_id):
        self.calls.append(("terminate_cluster_network", cluster_network_id))
        return SimpleNamespace(
            headers={"opc-work-request-id": "wr-cluster-network-delete"}
        )

    def terminate_instance_pool(self, instance_pool_id):
        self.calls.append(("terminate_instance_pool", instance_pool_id))
        return SimpleNamespace(
            headers={"opc-work-request-id": "wr-instance-pool-delete"}
        )

    def detach_instance_pool_instance(self, instance_pool_id, details):
        self.calls.append(("detach_instance_pool_instance", instance_pool_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-detach"})

    def list_instance_pools(self, compartment_id):
        self.calls.append(("list_instance_pools", compartment_id))
        return SimpleNamespace(data=self.instance_pools)

    def list_instance_pool_instances(self, compartment_id, instance_pool_id):
        self.calls.append(("list_instance_pool_instances", compartment_id, instance_pool_id))
        return SimpleNamespace(data=self.instances.get(instance_pool_id, []))


class _ContainerEngine:
    def __init__(self, node_pools=None, addons=None, cluster=None):
        self.node_pools = node_pools or []
        self.addons = addons or []
        self.cluster = cluster
        self.addon_options = []
        self.calls = []

    def get_cluster(self, cluster_id):
        self.calls.append(("get_cluster", cluster_id))
        return SimpleNamespace(
            data=self.cluster,
            headers={"etag": "cluster-etag"},
        )

    def get_cluster_options(self, cluster_id, **kwargs):
        self.calls.append(("get_cluster_options", cluster_id, kwargs))
        return SimpleNamespace(
            data=SimpleNamespace(
                kubernetes_versions=["v1.35.3", "v1.36.2"]
            )
        )

    def update_cluster(self, cluster_id, details, **kwargs):
        self.calls.append(("update_cluster", cluster_id, details, kwargs))
        return SimpleNamespace(
            headers={"opc-work-request-id": "wr-cluster-upgrade"}
        )

    def list_node_pools(self, **kwargs):
        self.calls.append(("list_node_pools", kwargs))
        return SimpleNamespace(data=[SimpleNamespace(id=pool.id) for pool in self.node_pools])

    def get_node_pool(self, node_pool_id):
        self.calls.append(("get_node_pool", node_pool_id))
        pool = next(pool for pool in self.node_pools if pool.id == node_pool_id)
        return SimpleNamespace(data=pool, headers={"etag": "node-pool-etag"})

    def update_node_pool(self, node_pool_id, details, **kwargs):
        self.calls.append(("update_node_pool", node_pool_id, details, kwargs))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-node-pool"})

    def replace_boot_volume_cluster_node(
        self,
        cluster_id,
        node_id,
        details,
        **kwargs,
    ):
        self.calls.append(
            (
                "replace_boot_volume_cluster_node",
                cluster_id,
                node_id,
                details,
                kwargs,
            )
        )
        return SimpleNamespace(headers={"opc-work-request-id": "wr-node-bvr"})

    def create_node_pool(self, details, **kwargs):
        self.calls.append(("create_node_pool", details, kwargs))
        return SimpleNamespace(
            data=None,
            headers={"opc-work-request-id": "wr-node-pool-create"},
        )

    def delete_node_pool(self, node_pool_id):
        self.calls.append(("delete_node_pool", node_pool_id))
        return SimpleNamespace(
            headers={"opc-work-request-id": "wr-node-pool-delete"}
        )

    def list_addons(self, cluster_id):
        self.calls.append(("list_addons", cluster_id))
        return SimpleNamespace(data=self.addons)

    def list_addon_options(self, kubernetes_version, **kwargs):
        self.calls.append(
            ("list_addon_options", kubernetes_version, kwargs)
        )
        return SimpleNamespace(data=self.addon_options)


class _WorkRequests:
    def __init__(self, status="SUCCEEDED", percent=100.0, errors=None, summaries=None):
        self.status = status
        self.percent = percent
        self.errors = errors or []
        self.summaries = summaries or []
        self.calls = []

    def get_work_request(self, work_request_id):
        self.calls.append(("get_work_request", work_request_id))
        return SimpleNamespace(
            data=SimpleNamespace(status=self.status, percent_complete=self.percent)
        )

    def list_work_request_errors(self, *args):
        self.calls.append(("list_work_request_errors", *args))
        return SimpleNamespace(data=self.errors)

    def list_work_requests(self, compartment_id, **kwargs):
        self.calls.append(("list_work_requests", compartment_id, kwargs))
        return SimpleNamespace(data=self.summaries)


class _Compute:
    def __init__(
        self,
        shapes=None,
        boot_volume_ids=None,
        image_operating_systems=None,
    ):
        self.shapes = shapes or {}
        self.boot_volume_ids = boot_volume_ids or {}
        self.image_operating_systems = image_operating_systems or {}
        self.calls = []
        self.gpu_memory_clusters = []
        self.gpu_memory_cluster = None
        self.compute_clusters = {}
        self.compute_host_groups = {}

    def get_instance(self, instance_id):
        self.calls.append(("get_instance", instance_id))
        return SimpleNamespace(
            data=SimpleNamespace(
                shape=self.shapes.get(instance_id),
                availability_domain="AD-1",
                compartment_id="compartment-1",
            ),
            headers={"etag": "instance-etag"},
        )

    def update_instance(self, instance_id, details, **kwargs):
        self.calls.append(("update_instance", instance_id, details, kwargs))
        return SimpleNamespace(data=SimpleNamespace(id=instance_id), headers={})

    def terminate_instance(self, instance_id, **kwargs):
        self.calls.append(("terminate_instance", instance_id, kwargs))
        return SimpleNamespace(headers={})

    def get_compute_cluster(self, compute_cluster_id):
        self.calls.append(("get_compute_cluster", compute_cluster_id))
        return SimpleNamespace(data=self.compute_clusters[compute_cluster_id])

    def create_compute_cluster(self, details, **kwargs):
        self.calls.append(("create_compute_cluster", details, kwargs))
        created = SimpleNamespace(
            id="compute-cluster-new",
            display_name=details.display_name,
            availability_domain=details.availability_domain,
            compartment_id=details.compartment_id,
            lifecycle_state="ACTIVE",
            freeform_tags=details.freeform_tags,
        )
        self.compute_clusters[created.id] = created
        return SimpleNamespace(data=created, headers={"etag": "cc-etag"})

    def get_compute_host_group(self, host_group_id):
        self.calls.append(("get_compute_host_group", host_group_id))
        return SimpleNamespace(data=self.compute_host_groups[host_group_id])

    def list_compute_gpu_memory_clusters(self, compartment_id):
        return SimpleNamespace(data=self.gpu_memory_clusters)

    def get_compute_gpu_memory_cluster(self, cluster_id):
        self.calls.append(("get_compute_gpu_memory_cluster", cluster_id))
        return SimpleNamespace(
            data=self.gpu_memory_cluster,
            headers={"etag": "gmc-etag"},
        )

    def list_compute_gpu_memory_cluster_instances(self, cluster_id):
        return SimpleNamespace(data=[])

    def update_compute_gpu_memory_cluster(self, cluster_id, details, **kwargs):
        self.calls.append(
            ("update_compute_gpu_memory_cluster", cluster_id, details, kwargs)
        )
        return SimpleNamespace(headers={"opc-work-request-id": "wr-gmc"})

    def create_compute_gpu_memory_cluster(self, details, **kwargs):
        self.calls.append(
            ("create_compute_gpu_memory_cluster", details, kwargs)
        )
        return SimpleNamespace(
            data=SimpleNamespace(id="gmc-blue"),
            headers={"opc-work-request-id": "wr-gmc-blue"},
        )

    def list_boot_volume_attachments(
        self,
        availability_domain,
        compartment_id,
        **kwargs,
    ):
        instance_id = kwargs["instance_id"]
        boot_volume_id = self.boot_volume_ids.get(instance_id)
        attachments = (
            []
            if boot_volume_id is None
            else [
                SimpleNamespace(
                    boot_volume_id=boot_volume_id,
                    lifecycle_state="ATTACHED",
                )
            ]
        )
        return SimpleNamespace(data=attachments)

    def list_shapes(self, compartment_id, **kwargs):
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    shape="BM.GPU4.8",
                    local_disks=8,
                    rdma_ports=8,
                    platform_names=["NVIDIA A100"],
                ),
                SimpleNamespace(
                    shape="BM.GPU.Test.8",
                    local_disks=0,
                    rdma_ports=0,
                    platform_names=["Test GPU"],
                ),
                SimpleNamespace(
                    shape="VM.Standard.E5.Flex",
                    local_disks=0,
                    rdma_ports=0,
                    platform_names=[],
                ),
                SimpleNamespace(
                    shape="VM.GPU.A10.1",
                    local_disks=0,
                    rdma_ports=0,
                    platform_names=["NVIDIA A10"],
                ),
                SimpleNamespace(
                    shape="VM.GPU.A10.2",
                    local_disks=0,
                    rdma_ports=0,
                    platform_names=["NVIDIA A10"],
                ),
            ]
        )

    def get_image(self, image_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                operating_system=self.image_operating_systems.get(
                    image_id,
                    "Oracle Linux",
                )
            )
        )


class _VirtualNetwork:
    def get_subnet(self, subnet_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                id=subnet_id,
                vcn_id="vcn-1",
                availability_domain=None,
            )
        )

    def get_network_security_group(self, nsg_id):
        return SimpleNamespace(
            data=SimpleNamespace(id=nsg_id, vcn_id="vcn-1")
        )


class _Identity:
    def __init__(self):
        self.availability_domains = [
            SimpleNamespace(name="AD-1"),
            SimpleNamespace(name="AD-2"),
            SimpleNamespace(name="AD-3"),
        ]

    def list_availability_domains(self, compartment_id):
        return SimpleNamespace(data=self.availability_domains)


class _Pagination:
    @staticmethod
    def list_call_get_all_results(function, *args, **kwargs):
        return function(*args, **kwargs)


def _instance_configuration():
    return SimpleNamespace(
        id="instance-configuration-source",
        compartment_id="compartment-1",
        display_name="oke-rdma",
        deferred_fields=[],
        freeform_tags={
            "pool": "oke-rdma",
            "role": "worker",
            "state_id": "terraform-state",
        },
        instance_details=SimpleNamespace(
            launch_details=SimpleNamespace(
                display_name=None,
                shape="BM.GPU4.8",
                shape_config=None,
                availability_domain="UK-LONDON-1-AD-3",
                freeform_tags={
                    "pool": "oke-rdma",
                    "role": "worker",
                    "state_id": "terraform-state",
                },
                metadata={
                    "apiserver_host": "10.0.0.1:6443",
                    "cluster_ca_cert": "certificate",
                    "oke-initial-node-labels": (
                        "custom.example/label=value,"
                        "oke.oraclecloud.com/pool.mode=cluster-network,"
                        "oke.oraclecloud.com/pool.name=oke-rdma,"
                        "oke.oraclecloud.com/tf.module=terraform-oci-oke,"
                        "oke.oraclecloud.com/tf.state_id=terraform-state"
                    ),
                    "user_data": "cloud-init",
                },
                create_vnic_details=SimpleNamespace(
                    subnet_id="subnet-1",
                    nsg_ids=["nsg-1"],
                    freeform_tags={
                        "pool": "oke-rdma",
                        "role": "worker",
                        "state_id": "terraform-state",
                    }
                ),
                source_details=SimpleNamespace(
                    source_type="image",
                    image_id="image-1",
                    boot_volume_size_in_gbs=512,
                    boot_volume_vpus_per_gb=10,
                    kms_key_id=None,
                ),
            ),
            secondary_vnics=[],
            block_volumes=[],
        ),
    )


def _backend(cluster_network=None, instance_configuration=None, node_pools=None):
    backend = OciBackend(auth="none")
    backend._oci = SimpleNamespace(
        pagination=_Pagination(),
        container_engine=SimpleNamespace(
            models=SimpleNamespace(
                UpdateNodePoolNodeConfigDetails=_Model,
                UpdateNodePoolDetails=_Model,
                UpdateClusterDetails=_Model,
                CreateNodePoolDetails=_Model,
                CreateNodePoolNodeConfigDetails=_Model,
                NodePoolPlacementConfigDetails=_Model,
                NodeSourceViaImageDetails=_Model,
                CreateNodeShapeConfigDetails=_Model,
                OciVcnIpNativeNodePoolPodNetworkOptionDetails=_Model,
                FlannelOverlayNodePoolPodNetworkOptionDetails=_Model,
                KeyValue=_Model,
                NodePoolCyclingDetails=_Model,
                NodeEvictionNodePoolSettings=_Model,
                NodeEvictionSettings=_Model,
                ReplaceBootVolumeClusterNodeDetails=_Model,
            )
        ),
        core=SimpleNamespace(
            models=SimpleNamespace(
                UpdateClusterNetworkInstancePoolDetails=_Model,
                UpdateClusterNetworkDetails=_Model,
                CreateClusterNetworkDetails=_Model,
                CreateClusterNetworkInstancePoolDetails=_Model,
                ClusterNetworkPlacementConfigurationDetails=_Model,
                CreateInstanceConfigurationDetails=_Model,
                UpdateInstancePoolDetails=_Model,
                DetachInstancePoolInstanceDetails=_Model,
                UpdateInstanceDetails=_Model,
                UpdateInstanceSourceViaImageDetails=_Model,
                UpdateComputeGpuMemoryClusterDetails=_Model,
                CreateInstancePoolDetails=_Model,
                CreateComputeGpuMemoryClusterDetails=_Model,
                CreateComputeClusterDetails=_Model,
            )
        )
    )
    backend._compute_mgmt = _ComputeManagement(
        cluster_network,
        instance_configuration or _instance_configuration(),
    )
    backend._container_engine = _ContainerEngine(node_pools=node_pools)
    backend._compute = _Compute()
    backend._identity = _Identity()
    backend._virtual_network = _VirtualNetwork()
    backend._work_requests = _WorkRequests()
    return backend


def _managed_node_pool(shape="VM.Standard.E5.Flex"):
    return SimpleNamespace(
        id="node-pool-source",
        cluster_id="cluster-1",
        compartment_id="compartment-1",
        name="oke-gpu" if "GPU" in shape else "oke-cpu",
        lifecycle_state="ACTIVE",
        kubernetes_version="v1.35.2",
        node_shape=shape,
        node_shape_config=(
            SimpleNamespace(ocpus=6.0, memory_in_gbs=32.0)
            if "Flex" in shape
            else None
        ),
        node_source_details=SimpleNamespace(
            image_id="image-source",
            boot_volume_size_in_gbs=256,
        ),
        node_config_details=SimpleNamespace(
            size=1,
            nsg_ids=["node-nsg-source"],
            kms_key_id=None,
            is_pv_encryption_in_transit_enabled=True,
            freeform_tags={"pool": "source", "state_id": "terraform-state"},
            defined_tags={},
            placement_configs=[
                SimpleNamespace(
                    availability_domain="AD-1",
                    subnet_id="worker-subnet-source",
                    capacity_reservation_id=None,
                    preemptible_node_config=None,
                    fault_domains=["FD-1"],
                )
            ],
            node_pool_pod_network_option_details=SimpleNamespace(
                cni_type="OCI_VCN_IP_NATIVE",
                max_pods_per_node=64,
                pod_nsg_ids=["pod-nsg-source"],
                pod_subnet_ids=["pod-subnet-source"],
            ),
        ),
        node_metadata={
            "apiserver_host": "10.0.0.1:6443",
            "user_data": "inherited-cloud-init",
        },
        initial_node_labels=[
            SimpleNamespace(
                key="oke.oraclecloud.com/pool.name",
                value="source",
            ),
            SimpleNamespace(
                key="oke.oraclecloud.com/tf.state_id",
                value="terraform-state",
            ),
        ],
        ssh_public_key="ssh-ed25519 source",
        freeform_tags={"pool": "source", "state_id": "terraform-state"},
        defined_tags={},
        node_pool_cycling_details=SimpleNamespace(
            is_node_cycling_enabled=False,
            maximum_surge="25%",
            maximum_unavailable="0",
            cycle_modes=["INSTANCE_REPLACE"],
        ),
        node_eviction_node_pool_settings=SimpleNamespace(
            eviction_grace_duration="PT5M",
            is_force_delete_after_grace_duration=True,
            is_force_action_after_grace_duration=True,
        ),
    )


class OciBackendMutationTests(unittest.TestCase):
    def test_control_plane_upgrade_uses_cluster_etag(self):
        cluster = SimpleNamespace(
            id="cluster-1",
            compartment_id="compartment-1",
            kubernetes_version="v1.35.2",
            available_kubernetes_upgrades=["v1.35.3"],
            lifecycle_state="ACTIVE",
            type="ENHANCED_CLUSTER",
            endpoints=SimpleNamespace(
                private_endpoint="10.0.0.1:6443",
                public_endpoint=None,
                kubernetes=None,
            ),
        )
        backend = _backend()
        backend._container_engine.cluster = cluster

        info = backend.get_cluster_info("cluster-1")
        work_request = backend.upgrade_control_plane(
            "cluster-1",
            "v1.35.3",
            info.etag,
        )

        self.assertEqual("cluster-etag", info.etag)
        self.assertIn("v1.36.2", info.available_kubernetes_versions)
        self.assertEqual("wr-cluster-upgrade", work_request)
        call = backend._container_engine.calls[-1]
        self.assertEqual("update_cluster", call[0])
        self.assertEqual("cluster-etag", call[3]["if_match"])
        self.assertEqual("v1.35.3", call[2].kubernetes_version)

    def test_managed_upgrade_builds_expected_cycling_modes_and_etag(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        backend = _backend(node_pools=[source])

        bvr, etag, bvr_preview = backend.preview_managed_pool_upgrade(
            source.id,
            "v1.35.3",
            strategy="boot-volume-replace",
        )
        replacement, _, replacement_preview = (
            backend.preview_managed_pool_upgrade(
                source.id,
                "v1.35.3",
                strategy="instance-replace",
            )
        )
        launch_configuration, _, launch_preview = (
            backend.preview_managed_pool_upgrade(
                source.id,
                "v1.35.3",
                strategy="boot-volume-replace",
                enable_cycling=False,
            )
        )
        work_request = backend.upgrade_managed_pool(
            source.id,
            bvr,
            etag,
        )

        self.assertEqual("node-pool-etag", etag)
        self.assertEqual(["BOOT_VOLUME_REPLACE"], bvr.node_pool_cycling_details.cycle_modes)
        self.assertEqual("1", bvr_preview["maximum_unavailable"])
        self.assertEqual(["INSTANCE_REPLACE"], replacement.node_pool_cycling_details.cycle_modes)
        self.assertEqual("0", replacement_preview["maximum_unavailable"])
        self.assertEqual("1", replacement_preview["maximum_surge"])
        self.assertIsNone(
            getattr(
                launch_configuration,
                "node_pool_cycling_details",
                None,
            )
        )
        self.assertEqual(
            "launch-configuration",
            launch_preview["phase"],
        )
        self.assertEqual("wr-node-pool", work_request)
        self.assertEqual(
            "node-pool-etag",
            backend._container_engine.calls[-1][3]["if_match"],
        )

    def test_self_managed_upgrade_clone_preserves_bootstrap_and_refreshes_target(self):
        backend = _backend()
        source = backend._compute_mgmt.instance_configuration
        original_metadata = dict(
            source.instance_details.launch_details.metadata
        )

        details, preview = backend.preview_instance_configuration_upgrade(
            source.id,
            "v1.35.3",
            operation_id="operation-1",
            api_server="10.0.0.2:6443",
            cluster_ca="new-certificate",
        )

        metadata = details.instance_details.launch_details.metadata
        self.assertEqual("v1.35.3", metadata["oke-k8version"])
        self.assertEqual("10.0.0.2:6443", metadata["apiserver_host"])
        self.assertEqual("new-certificate", metadata["cluster_ca_cert"])
        self.assertIn("oke-initial-node-labels", metadata)
        self.assertEqual(
            original_metadata["oke-initial-node-labels"],
            metadata["oke-initial-node-labels"],
        )
        self.assertTrue(preview["api_server_refreshed"])
        self.assertEqual(
            "operation-1",
            details.freeform_tags["mgmt-oke-upgrade-operation"],
        )

    def test_upgrade_instance_configuration_retry_token_is_operation_stable(self):
        backend = _backend()
        source_id = backend._compute_mgmt.instance_configuration.id

        for _attempt in range(2):
            backend.create_upgrade_instance_configuration(
                source_id,
                "v1.35.3",
                operation_id="operation-1",
                api_server="10.0.0.2:6443",
                cluster_ca="new-certificate",
            )

        calls = [
            call
            for call in backend._compute_mgmt.calls
            if call[0] == "create_instance_configuration"
        ]
        self.assertEqual(
            calls[-2][2]["opc_retry_token"],
            calls[-1][2]["opc_retry_token"],
        )

    def test_self_managed_bvr_preserves_instance_and_old_boot_volume(self):
        backend = _backend()
        backend._compute.shapes["instance-1"] = "BM.GPU4.8"
        (
            backend._compute_mgmt.instance_configuration
            .instance_details.launch_details.metadata["oke-k8version"]
        ) = "v1.35.3"

        backend.replace_self_managed_instance_boot_volume(
            "instance-1",
            "instance-configuration-source",
        )

        call = backend._compute.calls[-1]
        self.assertEqual("update_instance", call[0])
        self.assertEqual("instance-1", call[1])
        self.assertTrue(
            call[2].source_details.is_preserve_boot_volume_enabled
        )
        self.assertEqual("instance-etag", call[3]["if_match"])

    def test_upgrade_configuration_attachment_uses_etag_for_all_self_managed_backends(self):
        cluster_network = SimpleNamespace(
            id="cluster-network-1",
            display_name="oke-rdma",
            defined_tags={},
            freeform_tags={},
            instance_pools=[
                SimpleNamespace(
                    id="instance-pool-1",
                    display_name="oke-rdma",
                    size=2,
                    instance_configuration_id="config-old",
                    defined_tags={},
                    freeform_tags={},
                )
            ],
        )
        instance_pool = SimpleNamespace(
            id="instance-pool-2",
            display_name="standalone",
            compartment_id="compartment-1",
            size=1,
            instance_configuration_id="config-old",
            placement_configurations=[],
            freeform_tags={},
            defined_tags={},
        )
        backend = _backend(cluster_network=cluster_network)
        backend._compute_mgmt.instance_pools = [instance_pool]
        backend._compute.gpu_memory_cluster = SimpleNamespace(
            id="gmc-1",
            display_name="gmc",
            compartment_id="compartment-1",
            availability_domain="AD-1",
            size=1,
            instance_configuration_id="config-old",
            gpu_memory_fabric_id="fabric-1",
            compute_cluster_id="compute-cluster-1",
            freeform_tags={},
            defined_tags={},
        )

        requests = [
            backend.attach_upgrade_instance_configuration(
                WorkerPoolInfo(
                    name="oke-rdma",
                    kind="cluster-network",
                    cluster_network_id="cluster-network-1",
                    instance_pool_id="instance-pool-1",
                ),
                "config-new",
            ),
            backend.attach_upgrade_instance_configuration(
                WorkerPoolInfo(
                    name="standalone",
                    kind="instance-pool",
                    instance_pool_id="instance-pool-2",
                ),
                "config-new",
            ),
            backend.attach_upgrade_instance_configuration(
                WorkerPoolInfo(
                    name="gmc",
                    kind="gpu-memory-cluster",
                    gpu_memory_cluster_id="gmc-1",
                ),
                "config-new",
            ),
        ]

        self.assertEqual(
            ["wr-cluster-network", "wr-instance-pool", "wr-gmc"],
            requests,
        )
        cluster_call = next(
            call
            for call in backend._compute_mgmt.calls
            if call[0] == "update_cluster_network"
        )
        self.assertEqual("cluster-network-etag", cluster_call[3]["if_match"])
        pool_call = next(
            call
            for call in backend._compute_mgmt.calls
            if call[0] == "update_instance_pool"
        )
        self.assertEqual("instance-pool-etag", pool_call[3]["if_match"])
        gmc_call = next(
            call
            for call in backend._compute.calls
            if call[0] == "update_compute_gpu_memory_cluster"
        )
        self.assertEqual("gmc-etag", gmc_call[3]["if_match"])

    def test_gmc_blue_green_requires_explicit_placement_and_clones_source(self):
        backend = _backend()
        backend._compute.gpu_memory_cluster = SimpleNamespace(
            id="gmc-1",
            display_name="gmc",
            compartment_id="compartment-1",
            availability_domain="AD-1",
            size=2,
            gpu_memory_cluster_scale_config=SimpleNamespace(),
            freeform_tags={"team": "ai"},
            defined_tags={},
        )
        pool = WorkerPoolInfo(
            name="gmc",
            kind="gpu-memory-cluster",
            desired_size=2,
            gpu_memory_cluster_id="gmc-1",
        )

        with self.assertRaisesRegex(OciDiscoveryError, "requires explicit"):
            backend.create_gpu_memory_cluster_blue_green(
                pool,
                target_instance_configuration_id="config-new",
                name="gmc-green",
                operation_id="operation-1",
                compute_cluster_id=None,
                gpu_memory_fabric_id=None,
            )
        cluster_id, request_id = (
            backend.create_gpu_memory_cluster_blue_green(
                pool,
                target_instance_configuration_id="config-new",
                name="gmc-green",
                operation_id="operation-1",
                compute_cluster_id="compute-green",
                gpu_memory_fabric_id="fabric-green",
            )
        )

        self.assertEqual("gmc-blue", cluster_id)
        self.assertEqual("wr-gmc-blue", request_id)
        details = backend._compute.calls[-1][1]
        self.assertEqual("compute-green", details.compute_cluster_id)
        self.assertEqual("fabric-green", details.gpu_memory_fabric_id)
        self.assertEqual("config-new", details.instance_configuration_id)

    def test_create_managed_gpu_pool_applies_custom_image_and_network_overrides(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        backend = _backend(node_pools=[source])
        spec = PoolCreateSpec(
            pool_type="gpu",
            availability_domain="AD-2",
            shape="VM.GPU.A10.2",
            image_id="image-custom",
            primary_subnet_id="worker-subnet-new",
            pod_subnet_ids=("pod-subnet-new",),
            node_nsg_ids=("node-nsg-new",),
            pod_nsg_ids=("pod-nsg-new",),
            boot_volume_size_in_gbs=512,
            max_pods_per_node=80,
            node_labels=(("workload.example/type", "training"),),
            freeform_tags=(("team", "ai"),),
        )

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "gpu-batch",
            2,
            spec,
        )
        created = backend.create_managed_node_pool(
            source.id,
            "cluster-1",
            "compartment-1",
            "gpu-batch",
            2,
            spec,
        )

        self.assertEqual("image-custom", preview["image_id"])
        self.assertEqual(["AD-2"], preview["availability_domains"])
        self.assertEqual(["pod-subnet-new"], preview["pod_subnet_ids"])
        self.assertEqual("wr-node-pool-create", created.work_request_id)
        call = next(
            item
            for item in backend._container_engine.calls
            if item[0] == "create_node_pool"
        )
        details = call[1]
        self.assertEqual("VM.GPU.A10.2", details.node_shape)
        self.assertEqual("image-custom", details.node_source_details.image_id)
        self.assertEqual(512, details.node_source_details.boot_volume_size_in_gbs)
        self.assertEqual(
            "worker-subnet-new",
            details.node_config_details.placement_configs[0].subnet_id,
        )
        labels = {label.key: label.value for label in details.initial_node_labels}
        self.assertEqual("gpu-batch", labels["oke.oraclecloud.com/pool.name"])
        self.assertEqual("training", labels["workload.example/type"])
        self.assertNotIn("oke.oraclecloud.com/tf.state_id", labels)
        self.assertEqual("ai", details.freeform_tags["team"])

    def test_create_managed_rdma_pool_uses_compute_cluster_and_host_group(self):
        source = _managed_node_pool("BM.GPU4.8")
        source.name = "oke-rdma"
        source.node_config_details.compute_cluster_id = "compute-cluster-source"
        source.node_config_details.placement_configs[0].fault_domains = []
        backend = _backend(node_pools=[source])
        backend._compute.compute_clusters["compute-cluster-target"] = (
            SimpleNamespace(
                id="compute-cluster-target",
                display_name="target-cc",
                availability_domain="AD-1",
                compartment_id="compartment-1",
                lifecycle_state="ACTIVE",
            )
        )
        backend._compute.compute_host_groups["host-group-1"] = (
            SimpleNamespace(
                id="host-group-1",
                availability_domain="AD-1",
                compartment_id="compartment-1",
                lifecycle_state="ACTIVE",
                configurations=[
                    SimpleNamespace(
                        target="BM.GPU4.8",
                        state="VALID",
                    )
                ],
            )
        )
        spec = PoolCreateSpec(
            pool_type="rdma",
            rdma_mode="compute-cluster",
            compute_cluster_id="compute-cluster-target",
            host_group_id="host-group-1",
        )

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            2,
            spec,
        )
        created = backend.create_managed_node_pool(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            2,
            spec,
        )

        call = next(
            item
            for item in backend._container_engine.calls
            if item[0] == "create_node_pool"
        )
        node_config = call[1].node_config_details
        placement = node_config.placement_configs[0]
        self.assertEqual("compute-cluster", preview["placement"])
        self.assertEqual(
            "compute-cluster-target",
            node_config.compute_cluster_id,
        )
        self.assertEqual("host-group-1", placement.host_group_id)
        self.assertEqual([], placement.fault_domains)
        self.assertEqual([], preview["fault_domains"])
        self.assertEqual("compute-cluster-target", created.compute_cluster_id)
        self.assertEqual("host-group-1", created.host_group_id)

    def test_managed_rdma_pool_inherits_legacy_bootstrap_without_stale_identity(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        source.node_metadata.update(
            {
                "cluster_ca_cert": "current-certificate",
                "oke-k8version": "v1.35.2",
                "pod-subnets": "pod-subnet-source",
                "managed-custom": "current",
            }
        )
        backend = _backend(node_pools=[source])
        backend._compute.compute_clusters["compute-cluster-target"] = (
            SimpleNamespace(
                id="compute-cluster-target",
                display_name="target-cc",
                availability_domain="AD-1",
                compartment_id="compartment-1",
                lifecycle_state="ACTIVE",
            )
        )
        legacy_metadata = {
            "apiserver_host": "10.0.0.1:6443",
            "cluster_ca_cert": "current-certificate",
            "oke-initial-node-labels": "stale=true",
            "oke-k8version": "v1.34.1",
            "pod-subnets": "stale-pod-subnet",
            "user_data": "legacy-rdma-cloud-init",
            "pre_oke": "legacy-pre-hook",
            "legacy-custom": "preserved",
        }
        spec = PoolCreateSpec(
            pool_type="rdma",
            rdma_mode="compute-cluster",
            compute_cluster_id="compute-cluster-target",
            shape="BM.GPU4.8",
        )

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            2,
            spec,
            bootstrap_metadata=legacy_metadata,
        )
        backend.create_managed_node_pool(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            2,
            spec,
            bootstrap_metadata=legacy_metadata,
        )

        call = next(
            item
            for item in backend._container_engine.calls
            if item[0] == "create_node_pool"
        )
        metadata = call[1].node_metadata
        self.assertEqual("legacy-rdma-cloud-init", metadata["user_data"])
        self.assertEqual("legacy-pre-hook", metadata["pre_oke"])
        self.assertEqual("preserved", metadata["legacy-custom"])
        self.assertEqual("current", metadata["managed-custom"])
        self.assertEqual("v1.35.2", metadata["oke-k8version"])
        self.assertEqual("pod-subnet-source", metadata["pod-subnets"])
        self.assertNotEqual("stale=true", metadata.get("oke-initial-node-labels"))
        self.assertEqual(2, call[1].node_config_details.size)
        self.assertEqual(
            len("legacy-rdma-cloud-init"),
            preview["worker_bootstrap"]["decoded_bytes"],
        )

    def test_managed_rdma_pool_rejects_legacy_bootstrap_from_another_cluster(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        source.node_metadata["cluster_ca_cert"] = "current-certificate"
        backend = _backend(node_pools=[source])
        spec = PoolCreateSpec(
            pool_type="rdma",
            rdma_mode="compute-cluster",
            shape="BM.GPU4.8",
        )
        legacy_metadata = {
            "apiserver_host": "another-cluster:6443",
            "cluster_ca_cert": "another-certificate",
            "oke-initial-node-labels": "pool=legacy",
            "user_data": "legacy-rdma-cloud-init",
        }

        with self.assertRaisesRegex(OciDiscoveryError, "same OKE cluster"):
            backend.preview_managed_node_pool_create(
                source.id,
                "cluster-1",
                "compartment-1",
                "rdma-batch",
                2,
                spec,
                bootstrap_metadata=legacy_metadata,
            )

    def test_managed_rdma_preview_plans_dedicated_compute_cluster(self):
        source = _managed_node_pool("BM.GPU4.8")
        source.name = "oke-rdma"
        source.node_config_details.compute_cluster_id = "compute-cluster-source"
        source.node_config_details.placement_configs[0].fault_domains = []
        backend = _backend(node_pools=[source])

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            2,
            PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
            ),
        )

        self.assertEqual("create", preview["compute_cluster_action"])
        self.assertEqual("rdma-batch-cc", preview["compute_cluster_name"])
        self.assertIsNone(preview["compute_cluster_id"])

    def test_managed_rdma_can_derive_from_regular_managed_gpu_pool(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        backend = _backend(node_pools=[source])

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-batch",
            1,
            PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                shape="BM.GPU4.8",
                availability_domain="AD-1",
            ),
        )

        self.assertEqual("compute-cluster", preview["placement"])
        self.assertEqual("BM.GPU4.8", preview["shape"])
        self.assertEqual("create", preview["compute_cluster_action"])

    def test_managed_compute_cluster_may_use_another_compartment(self):
        source = _managed_node_pool("BM.GPU4.8")
        source.node_config_details.placement_configs[0].fault_domains = []
        backend = _backend(node_pools=[source])
        backend._compute.compute_clusters["compute-cluster-target"] = (
            SimpleNamespace(
                id="compute-cluster-target",
                display_name="target-cc",
                availability_domain="AD-1",
                compartment_id="compute-compartment-2",
                lifecycle_state="ACTIVE",
            )
        )

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "node-pool-compartment-1",
            "rdma-batch",
            1,
            PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                compute_cluster_id="compute-cluster-target",
            ),
        )

        self.assertEqual("compute-cluster-target", preview["compute_cluster_id"])

    def test_managed_placement_rejects_invalid_compute_resources(self):
        source = _managed_node_pool("BM.GPU4.8")
        source.node_config_details.placement_configs[0].fault_domains = []
        spec = PoolCreateSpec(
            pool_type="rdma",
            rdma_mode="compute-cluster",
            compute_cluster_id="compute-cluster-target",
        )
        invalid_clusters = (
            ("DELETED", "AD-1", "not ACTIVE"),
            ("ACTIVE", "AD-2", "but the node-pool placement uses"),
        )
        for lifecycle_state, availability_domain, message in invalid_clusters:
            with self.subTest(message=message):
                backend = _backend(node_pools=[source])
                backend._compute.compute_clusters[
                    "compute-cluster-target"
                ] = SimpleNamespace(
                    id="compute-cluster-target",
                    display_name="target-cc",
                    availability_domain=availability_domain,
                    compartment_id="compartment-1",
                    lifecycle_state=lifecycle_state,
                )
                with self.assertRaisesRegex(OciDiscoveryError, message):
                    backend.preview_managed_node_pool_create(
                        source.id,
                        "cluster-1",
                        "compartment-1",
                        "rdma-batch",
                        2,
                        spec,
                    )

    def test_host_group_requires_matching_active_shape_and_ad(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        spec = PoolCreateSpec(
            pool_type="gpu",
            host_group_id="host-group-1",
        )
        backend = _backend(node_pools=[source])
        backend._compute.compute_host_groups["host-group-1"] = (
            SimpleNamespace(
                id="host-group-1",
                availability_domain="AD-1",
                lifecycle_state="ACTIVE",
                configurations=[
                    SimpleNamespace(
                        target="BM.GPU4.8",
                        state="VALID",
                    )
                ],
            )
        )

        with self.assertRaisesRegex(OciDiscoveryError, "for shape"):
            backend.preview_managed_node_pool_create(
                source.id,
                "cluster-1",
                "compartment-1",
                "gpu-host-group",
                1,
                spec,
            )

    def test_host_group_accepts_valid_shape_platform_target(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        spec = PoolCreateSpec(
            pool_type="gpu",
            host_group_id="host-group-1",
        )
        backend = _backend(node_pools=[source])
        backend._compute.compute_host_groups["host-group-1"] = (
            SimpleNamespace(
                id="host-group-1",
                availability_domain="AD-1",
                lifecycle_state="ACTIVE",
                configurations=[
                    SimpleNamespace(
                        target="NVIDIA A10",
                        state="VALID",
                    )
                ],
            )
        )

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "gpu-host-group",
            1,
            spec,
        )

        self.assertEqual("host-group", preview["placement"])
        self.assertEqual(["host-group-1"], preview["host_group_ids"])

    def test_host_group_requires_active_state_and_matching_ad(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        spec = PoolCreateSpec(
            pool_type="gpu",
            host_group_id="host-group-1",
        )
        invalid_groups = (
            ("DELETED", "AD-1", "not ACTIVE"),
            ("ACTIVE", "AD-2", "but the node-pool placement uses"),
        )
        for lifecycle_state, availability_domain, message in invalid_groups:
            with self.subTest(message=message):
                backend = _backend(node_pools=[source])
                backend._compute.compute_host_groups["host-group-1"] = (
                    SimpleNamespace(
                        id="host-group-1",
                        availability_domain=availability_domain,
                        lifecycle_state=lifecycle_state,
                        configurations=[
                            SimpleNamespace(
                                target="NVIDIA A10",
                                state="VALID",
                            )
                        ],
                    )
                )
                with self.assertRaisesRegex(OciDiscoveryError, message):
                    backend.preview_managed_node_pool_create(
                        source.id,
                        "cluster-1",
                        "compartment-1",
                        "gpu-host-group",
                        1,
                        spec,
                    )

    def test_managed_rdma_requires_shape_with_rdma_ports(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        backend = _backend(node_pools=[source])

        with self.assertRaisesRegex(OciDiscoveryError, "does not advertise RDMA"):
            backend.preview_managed_node_pool_create(
                source.id,
                "cluster-1",
                "compartment-1",
                "rdma-bad-shape",
                1,
                PoolCreateSpec(
                    pool_type="rdma",
                    rdma_mode="compute-cluster",
                    shape="BM.GPU.Test.8",
                    availability_domain="AD-1",
                ),
            )

    def test_managed_placement_requires_current_oci_sdk_fields(self):
        source = _managed_node_pool("BM.GPU4.8")
        source.node_config_details.placement_configs[0].fault_domains = []
        backend = _backend(node_pools=[source])
        backend._oci.container_engine.models.CreateNodePoolNodeConfigDetails = (
            _LegacyModel
        )

        with self.assertRaisesRegex(OciDiscoveryError, "installed OCI Python SDK"):
            backend.preview_managed_node_pool_create(
                source.id,
                "cluster-1",
                "compartment-1",
                "rdma-old-sdk",
                1,
                PoolCreateSpec(
                    pool_type="rdma",
                    rdma_mode="compute-cluster",
                    compute_cluster_id="compute-cluster-1",
                ),
            )

        backend = _backend(node_pools=[source])
        backend._oci.container_engine.models.NodePoolPlacementConfigDetails = (
            _LegacyModel
        )
        with self.assertRaisesRegex(OciDiscoveryError, "Compute Host Groups"):
            backend.preview_managed_node_pool_create(
                source.id,
                "cluster-1",
                "compartment-1",
                "gpu-old-sdk",
                1,
                PoolCreateSpec(
                    pool_type="gpu",
                    host_group_id="host-group-1",
                ),
            )

    def test_create_compute_cluster_tags_ownership(self):
        backend = _backend()

        created = backend.create_compute_cluster(
            compartment_id="compartment-1",
            availability_domain="AD-1",
            display_name="rdma-batch-cc",
            pool_name="rdma-batch",
            freeform_tags={"team": "ai"},
            opc_retry_token="retry-token",
        )

        call = backend._compute.calls[-1]
        self.assertEqual("compute-cluster-new", created.compute_cluster_id)
        self.assertEqual("create_compute_cluster", call[0])
        self.assertEqual("true", call[1].freeform_tags["mgmt-oke-created"])
        self.assertEqual("rdma-batch", call[1].freeform_tags["mgmt-oke-pool"])
        self.assertEqual("ai", call[1].freeform_tags["team"])
        self.assertEqual("retry-token", call[2]["opc_retry_token"])

    def test_availability_domain_display_name_resolves_to_canonical_name(self):
        source = _managed_node_pool("BM.GPU4.8")
        source.node_config_details.placement_configs[0].fault_domains = []
        backend = _backend(node_pools=[source])
        backend._identity.availability_domains = [
            SimpleNamespace(name="example:UK-LONDON-1-AD-3")
        ]

        preview = backend.preview_managed_node_pool_create(
            source.id,
            "cluster-1",
            "compartment-1",
            "rdma-canonical-ad",
            1,
            PoolCreateSpec(
                pool_type="rdma",
                rdma_mode="compute-cluster",
                availability_domain="UK-LONDON-1-AD-3",
            ),
        )

        self.assertEqual(
            ["example:UK-LONDON-1-AD-3"],
            preview["availability_domains"],
        )

    def test_availability_domain_resolution_rejects_unknown_alias(self):
        backend = _backend()
        backend._identity.availability_domains = [
            SimpleNamespace(name="example:UK-LONDON-1-AD-3")
        ]

        with self.assertRaisesRegex(OciDiscoveryError, "was not found"):
            backend.resolve_availability_domain(
                "compartment-1",
                "UK-LONDON-1-AD-2",
            )

    def test_managed_pool_type_rejects_incompatible_shape(self):
        source = _managed_node_pool()
        backend = _backend(node_pools=[source])

        with self.assertRaisesRegex(OciDiscoveryError, "CPU pool type"):
            backend.preview_managed_node_pool_create(
                source.id,
                "cluster-1",
                "compartment-1",
                "cpu-bad",
                1,
                PoolCreateSpec(pool_type="cpu", shape="VM.GPU.A10.1"),
            )

    def test_whole_pool_delete_routes_to_owning_oci_api(self):
        backend = _backend()

        managed = backend.delete_managed_node_pool("node-pool-1")
        cluster_network = backend.terminate_cluster_network("cluster-network-1")
        instance_pool = backend.terminate_instance_pool("instance-pool-1")

        self.assertEqual("wr-node-pool-delete", managed)
        self.assertEqual("wr-cluster-network-delete", cluster_network)
        self.assertEqual("wr-instance-pool-delete", instance_pool)

    def test_instance_configuration_cleanup_requires_mgmt_ownership_tag(self):
        owned = _instance_configuration()
        owned.freeform_tags["mgmt-oke-created"] = "true"
        owned.freeform_tags[
            "mgmt-oke-upgrade-operation"
        ] = "operation-1"
        backend = _backend(instance_configuration=owned)

        backend.delete_mgmt_created_instance_configuration(
            "instance-configuration-source",
            operation_id="operation-1",
        )

        self.assertEqual(
            (
                "delete_instance_configuration",
                "instance-configuration-source",
            ),
            backend._compute_mgmt.calls[-1],
        )

        unowned = _instance_configuration()
        backend = _backend(instance_configuration=unowned)
        with self.assertRaisesRegex(OciDiscoveryError, "not tagged"):
            backend.delete_mgmt_created_instance_configuration(
                "instance-configuration-source"
            )
        self.assertNotIn(
            (
                "delete_instance_configuration",
                "instance-configuration-source",
            ),
            backend._compute_mgmt.calls,
        )

        backend = _backend(instance_configuration=owned)
        with self.assertRaisesRegex(OciDiscoveryError, "not owned"):
            backend.delete_mgmt_created_instance_configuration(
                "instance-configuration-source",
                operation_id="operation-2",
            )

    def test_resize_managed_node_pool_sends_only_size(self):
        backend = _backend()

        work_request = backend.resize_managed_node_pool("node-pool-1", 3)

        self.assertEqual("wr-node-pool", work_request)
        _, node_pool_id, details, kwargs = backend._container_engine.calls[-1]
        self.assertEqual("node-pool-1", node_pool_id)
        self.assertEqual({"size": 3}, details.node_config_details.__dict__)
        self.assertEqual({}, kwargs)

    def test_resize_managed_node_pool_rejects_negative_size(self):
        backend = _backend()

        with self.assertRaises(OciDiscoveryError):
            backend.resize_managed_node_pool("node-pool-1", -1)

    def test_individual_node_boot_volume_replace_uses_oke_api(self):
        backend = _backend()
        backend._compute = _Compute(
            boot_volume_ids={"instance-1": "boot-volume-old"}
        )

        boot_volume_id = backend.get_instance_boot_volume_id("instance-1")
        work_request_id = backend.replace_cluster_node_boot_volume(
            "cluster-1",
            "instance-1",
            eviction_grace_duration="PT30M",
            force_after_grace=True,
        )

        self.assertEqual("boot-volume-old", boot_volume_id)
        self.assertEqual("wr-node-bvr", work_request_id)
        call = backend._container_engine.calls[-1]
        self.assertEqual("replace_boot_volume_cluster_node", call[0])
        self.assertEqual("cluster-1", call[1])
        self.assertEqual("instance-1", call[2])
        self.assertEqual(
            "PT30M",
            call[3].node_eviction_settings.eviction_grace_duration,
        )
        self.assertTrue(
            call[3].node_eviction_settings.is_force_action_after_grace_duration
        )
        self.assertIn("opc_retry_token", call[4])

    def test_boot_volume_lookup_prefers_attached_during_transition(self):
        backend = _backend()
        backend._compute = _Compute()
        backend._compute.list_boot_volume_attachments = (
            lambda *_args, **_kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        boot_volume_id="boot-volume-old",
                        lifecycle_state="ATTACHED",
                    ),
                    SimpleNamespace(
                        boot_volume_id="boot-volume-new",
                        lifecycle_state="ATTACHING",
                    ),
                ]
            )
        )

        self.assertEqual(
            "boot-volume-old",
            backend.get_instance_boot_volume_id("instance-1"),
        )

    def test_boot_volume_lookup_distinguishes_pending_from_ambiguous(self):
        backend = _backend()
        backend._compute = _Compute()

        with self.assertRaises(BootVolumeAttachmentPending):
            backend.get_instance_boot_volume_id("instance-1")

        backend._compute.list_boot_volume_attachments = (
            lambda *_args, **_kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        boot_volume_id="boot-volume-a",
                        lifecycle_state="ATTACHED",
                    ),
                    SimpleNamespace(
                        boot_volume_id="boot-volume-b",
                        lifecycle_state="ATTACHED",
                    ),
                ]
            )
        )
        with self.assertRaises(OciDiscoveryError) as context:
            backend.get_instance_boot_volume_id("instance-1")
        self.assertNotIsInstance(
            context.exception,
            BootVolumeAttachmentPending,
        )

    def test_managed_pool_bvr_updates_image_and_cycles_boot_volumes(self):
        source = _managed_node_pool("VM.GPU.A10.1")
        backend = _backend(node_pools=[source])
        spec = PoolBootVolumeReplaceSpec(
            image_id="image-custom",
            boot_volume_size_in_gbs=512,
            boot_volume_kms_key_id="kms-key-1",
            kubernetes_version="v1.36.1",
            node_metadata=(("custom.example/mode", "training"),),
            ssh_public_key="ssh-ed25519 replacement",
            maximum_unavailable="50%",
        )

        preview = backend.preview_managed_pool_boot_volume_replace(
            source.id,
            spec,
        )
        work_request_id = backend.replace_managed_pool_boot_volumes(
            source.id,
            spec,
        )

        self.assertEqual("image-source", preview["current"]["image_id"])
        self.assertEqual("image-custom", preview["effective"]["image_id"])
        self.assertEqual("wr-node-pool", work_request_id)
        call = backend._container_engine.calls[-1]
        self.assertEqual("update_node_pool", call[0])
        details = call[2]
        self.assertEqual("image-custom", details.node_source_details.image_id)
        self.assertEqual(
            512,
            details.node_source_details.boot_volume_size_in_gbs,
        )
        self.assertEqual("v1.36.1", details.kubernetes_version)
        self.assertEqual(
            "training",
            details.node_metadata["custom.example/mode"],
        )
        self.assertEqual(
            "inherited-cloud-init",
            details.node_metadata["user_data"],
        )
        self.assertEqual("kms-key-1", details.node_config_details.kms_key_id)
        self.assertEqual(
            ["BOOT_VOLUME_REPLACE"],
            details.node_pool_cycling_details.cycle_modes,
        )
        self.assertEqual(
            "50%",
            details.node_pool_cycling_details.maximum_unavailable,
        )
        self.assertEqual({"if_match": "node-pool-etag"}, call[3])

    def test_managed_pool_bvr_verifies_applied_properties(self):
        source = _managed_node_pool()
        backend = _backend(node_pools=[source])
        spec = PoolBootVolumeReplaceSpec(
            image_id="image-new",
            node_metadata=(("custom", "value"),),
        )

        self.assertFalse(
            backend.managed_pool_boot_volume_replace_applied(source.id, spec)
        )

        source.node_source_details.image_id = "image-new"
        source.node_metadata["custom"] = "value"
        self.assertTrue(
            backend.managed_pool_boot_volume_replace_applied(source.id, spec)
        )

    def test_managed_pool_bvr_refuses_boot_volume_reduction(self):
        source = _managed_node_pool()
        backend = _backend(node_pools=[source])

        with self.assertRaisesRegex(OciDiscoveryError, "cannot reduce"):
            backend.preview_managed_pool_boot_volume_replace(
                source.id,
                PoolBootVolumeReplaceSpec(boot_volume_size_in_gbs=128),
            )

    def test_managed_pool_bvr_requires_same_linux_distribution(self):
        source = _managed_node_pool()
        backend = _backend(node_pools=[source])
        backend._compute = _Compute(
            image_operating_systems={
                "image-source": "Oracle Linux",
                "image-ubuntu": "Canonical Ubuntu",
            }
        )

        with self.assertRaisesRegex(
            OciDiscoveryError,
            "same Linux distribution",
        ):
            backend.preview_managed_pool_boot_volume_replace(
                source.id,
                PoolBootVolumeReplaceSpec(image_id="image-ubuntu"),
            )

    def test_resize_cluster_network_preserves_pool_fields(self):
        pool = SimpleNamespace(
            id="pool-1",
            instance_configuration_id="config-1",
            display_name="oke-rdma",
            size=2,
            defined_tags={"ns": {"key": "value"}},
            freeform_tags={"pool": "oke-rdma"},
        )
        cluster = SimpleNamespace(
            display_name="oke-rdma",
            defined_tags={"ns": {"key": "value"}},
            freeform_tags={"pool": "oke-rdma"},
            instance_pools=[pool],
        )
        backend = _backend(cluster)

        work_request = backend.resize_cluster_network("cluster-1", "pool-1", 3)

        self.assertEqual("wr-cluster-network", work_request)
        _, cluster_id, details = backend._compute_mgmt.calls[-1]
        self.assertEqual("cluster-1", cluster_id)
        self.assertEqual("oke-rdma", details.display_name)
        self.assertEqual(3, details.instance_pools[0].size)
        self.assertEqual("config-1", details.instance_pools[0].instance_configuration_id)

    def test_resize_cluster_network_rejects_unrelated_instance_pool(self):
        cluster = SimpleNamespace(instance_pools=[SimpleNamespace(id="pool-1", size=2)])
        backend = _backend(cluster)

        with self.assertRaises(OciDiscoveryError):
            backend.resize_cluster_network("cluster-1", "pool-2", 3)

    def test_resize_cluster_network_accepts_missing_work_request_header(self):
        pool = SimpleNamespace(
            id="pool-1",
            instance_configuration_id="config-1",
            display_name="oke-rdma",
            size=2,
        )
        backend = _backend(SimpleNamespace(instance_pools=[pool]))
        backend._compute_mgmt.update_cluster_network = lambda *_args: SimpleNamespace(
            headers={}
        )

        work_request = backend.resize_cluster_network("cluster-1", "pool-1", 3)

        self.assertIsNone(work_request)

    def test_create_cluster_network_pool_derives_configuration_and_reuses_placement(self):
        source_pool = SimpleNamespace(
            id="instance-pool-source",
            instance_configuration_id="instance-configuration-source",
            freeform_tags={
                "pool": "oke-rdma",
                "role": "worker",
                "state_id": "terraform-state",
                "custom": "preserved",
            },
        )
        placement = SimpleNamespace(
            availability_domain="UK-LONDON-1-AD-3",
            placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
            primary_subnet_id="subnet-1",
            primary_vnic_subnets=None,
            secondary_vnic_subnets=None,
        )
        source = SimpleNamespace(
            id="cluster-network-source",
            compartment_id="compartment-1",
            display_name="oke-rdma",
            lifecycle_state="RUNNING",
            freeform_tags={"pool": "oke-rdma", "state_id": "terraform-state"},
            instance_pools=[source_pool],
            placement_configuration=placement,
        )
        backend = _backend(source)

        created = backend.create_cluster_network_pool(
            "cluster-network-source",
            "instance-pool-source",
            "oke-rdma-2",
            2,
        )

        self.assertEqual("cluster-network-new", created.cluster_network_id)
        self.assertEqual(
            "instance-configuration-new",
            created.instance_configuration_id,
        )
        self.assertEqual("instance-pool-new", created.instance_pool_id)
        self.assertEqual("wr-cluster-network-create", created.work_request_id)
        _, details, kwargs = backend._compute_mgmt.calls[-1]
        self.assertEqual("compartment-1", details.compartment_id)
        self.assertEqual("oke-rdma-2", details.display_name)
        self.assertEqual("oke-rdma-2", details.freeform_tags["pool"])
        self.assertNotIn("state_id", details.freeform_tags)
        self.assertEqual(
            "instance-configuration-new",
            details.instance_pools[0].instance_configuration_id,
        )
        self.assertEqual(2, details.instance_pools[0].size)
        self.assertEqual("preserved", details.instance_pools[0].freeform_tags["custom"])
        self.assertNotIn("state_id", details.instance_pools[0].freeform_tags)
        self.assertEqual("UK-LONDON-1-AD-3", details.placement_configuration.availability_domain)
        self.assertEqual("subnet-1", details.placement_configuration.primary_subnet_id)
        self.assertTrue(kwargs["opc_retry_token"])
        _, instance_config_details, instance_config_kwargs = next(
            call
            for call in backend._compute_mgmt.calls
            if call[0] == "create_instance_configuration"
        )
        self.assertEqual("NONE", instance_config_details.source)
        self.assertEqual("oke-rdma-2", instance_config_details.display_name)
        self.assertNotIn("state_id", instance_config_details.freeform_tags)
        launch_details = instance_config_details.instance_details.launch_details
        self.assertEqual("oke-rdma-2", launch_details.display_name)
        self.assertEqual("cloud-init", launch_details.metadata["user_data"])
        self.assertIn(
            "oke.oraclecloud.com/pool.name=oke-rdma-2",
            launch_details.metadata["oke-initial-node-labels"],
        )
        self.assertNotIn(
            "oke.oraclecloud.com/tf.",
            launch_details.metadata["oke-initial-node-labels"],
        )
        self.assertEqual(
            "oke-rdma-2",
            launch_details.create_vnic_details.freeform_tags["pool"],
        )
        self.assertNotIn(
            "state_id",
            launch_details.create_vnic_details.freeform_tags,
        )
        self.assertTrue(instance_config_kwargs["opc_retry_token"])
        self.assertNotEqual(
            instance_config_kwargs["opc_retry_token"],
            kwargs["opc_retry_token"],
        )

    def test_reads_validated_bootstrap_metadata_from_cluster_network_pool(self):
        source_pool = SimpleNamespace(
            id="instance-pool-source",
            instance_configuration_id="instance-configuration-source",
        )
        cluster_network = SimpleNamespace(
            id="cluster-network-source",
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[source_pool],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
                primary_subnet_id="subnet-1",
                primary_vnic_subnets=None,
            ),
        )
        backend = _backend(cluster_network=cluster_network)

        metadata = backend.get_cluster_network_pool_bootstrap_metadata(
            "cluster-network-source",
            "instance-pool-source",
        )

        self.assertEqual("cloud-init", metadata["user_data"])
        self.assertEqual("certificate", metadata["cluster_ca_cert"])
        metadata["user_data"] = "modified"
        self.assertEqual(
            "cloud-init",
            backend._compute_mgmt.instance_configuration.instance_details
            .launch_details.metadata["user_data"],
        )
        self.assertEqual(
            "oke-rdma",
            backend._compute_mgmt.instance_configuration.instance_details
            .launch_details.freeform_tags["pool"],
        )

    def test_create_cluster_network_pool_rejects_invalid_source_and_size(self):
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[SimpleNamespace(id="instance-pool-source")],
            placement_configuration=SimpleNamespace(),
        )
        backend = _backend(source)

        with self.assertRaisesRegex(OciDiscoveryError, "not part of cluster network"):
            backend.create_cluster_network_pool(
                "cluster-network-source",
                "instance-pool-missing",
                "oke-rdma-2",
                2,
            )
        with self.assertRaisesRegex(OciDiscoveryError, "at least one"):
            backend.create_cluster_network_pool(
                "cluster-network-source",
                "instance-pool-source",
                "oke-rdma-2",
                0,
            )

    def test_create_cluster_network_pool_reports_partial_instance_configuration(self):
        source_pool = SimpleNamespace(
            id="instance-pool-source",
            instance_configuration_id="instance-configuration-source",
        )
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            freeform_tags={},
            instance_pools=[source_pool],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
                primary_subnet_id="subnet-1",
                primary_vnic_subnets=None,
                secondary_vnic_subnets=None,
            ),
        )
        backend = _backend(source)

        def fail_cluster_network(*_args, **_kwargs):
            raise ValueError("capacity unavailable")

        backend._compute_mgmt.create_cluster_network = fail_cluster_network

        with self.assertRaisesRegex(
            OciDiscoveryError,
            "derived Instance Configuration was created as "
            "instance-configuration-new",
        ):
            backend.create_cluster_network_pool(
                "cluster-network-source",
                "instance-pool-source",
                "oke-rdma-2",
                2,
            )

    def test_create_cluster_network_pool_requires_created_configuration_id(self):
        source_pool = SimpleNamespace(
            id="instance-pool-source",
            instance_configuration_id="instance-configuration-source",
        )
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[source_pool],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
                primary_subnet_id="subnet-1",
            ),
        )
        backend = _backend(source)
        backend._compute_mgmt.create_instance_configuration = (
            lambda *_args, **_kwargs: SimpleNamespace(
                data=SimpleNamespace(id=None),
                headers={},
            )
        )

        with self.assertRaisesRegex(
            OciDiscoveryError,
            "did not return the new resource OCID",
        ):
            backend.create_cluster_network_pool(
                "cluster-network-source",
                "instance-pool-source",
                "oke-rdma-2",
                2,
            )

        self.assertFalse(
            any(
                call[0] == "create_cluster_network"
                for call in backend._compute_mgmt.calls
            )
        )

    def test_validate_cluster_network_pool_template_requires_complete_placement(self):
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[
                SimpleNamespace(
                    id="instance-pool-source",
                    instance_configuration_id="instance-configuration-source",
                )
            ],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint=None,
                primary_subnet_id="subnet-1",
            ),
        )
        backend = _backend(source)

        with self.assertRaisesRegex(OciDiscoveryError, "placement constraint"):
            backend.validate_cluster_network_pool_template(
                "cluster-network-source",
                "instance-pool-source",
                "oke-rdma-2",
            )

    def test_validate_cluster_network_pool_template_accepts_primary_vnic_subnets(self):
        primary_vnic_subnets = SimpleNamespace(subnet_id="subnet-1")
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[
                SimpleNamespace(
                    id="instance-pool-source",
                    instance_configuration_id="instance-configuration-source",
                )
            ],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
                primary_subnet_id=None,
                primary_vnic_subnets=primary_vnic_subnets,
                secondary_vnic_subnets=None,
            ),
        )
        backend = _backend(source)

        backend.validate_cluster_network_pool_template(
            "cluster-network-source",
            "instance-pool-source",
            "oke-rdma-2",
        )
        backend.create_cluster_network_pool(
            "cluster-network-source",
            "instance-pool-source",
            "oke-rdma-2",
            1,
        )

        _, details, _kwargs = backend._compute_mgmt.calls[-1]
        self.assertIsNone(details.placement_configuration.primary_subnet_id)
        self.assertIs(
            primary_vnic_subnets,
            details.placement_configuration.primary_vnic_subnets,
        )

    def test_clone_pool_freeform_tags_renames_pool_and_removes_stack_ownership(self):
        tags = _clone_pool_freeform_tags(
            {"pool": "oke-rdma", "state_id": "abc", "custom": "value"},
            "oke-rdma-2",
        )

        self.assertEqual(
            {
                "pool": "oke-rdma-2",
                "role": "worker",
                "custom": "value",
                "mgmt-oke-created": "true",
            },
            tags,
        )

    def test_retarget_oke_node_labels_updates_identity_and_removes_iac_labels(self):
        labels = _retarget_oke_node_labels(
            "custom.example/key=value,"
            "oke.oraclecloud.com/pool.mode=cluster-network,"
            "oke.oraclecloud.com/pool.name=oke-rdma,"
            "oke.oraclecloud.com/tf.module=terraform-oci-oke,"
            "oke.oraclecloud.com/tf.state_id=state",
            "oke-rdma-2",
        )

        self.assertEqual(
            "custom.example/key=value,"
            "oke.oraclecloud.com/pool.mode=cluster-network,"
            "oke.oraclecloud.com/pool.name=oke-rdma-2",
            labels,
        )

    def test_retarget_oke_node_labels_rejects_duplicates(self):
        with self.assertRaisesRegex(OciDiscoveryError, "repeats initial node label"):
            _retarget_oke_node_labels(
                "oke.oraclecloud.com/pool.name=one,"
                "oke.oraclecloud.com/pool.name=two",
                "oke-rdma-2",
            )

    def test_validate_cluster_network_pool_template_requires_bootstrap_metadata(self):
        source_pool = SimpleNamespace(
            id="instance-pool-source",
            instance_configuration_id="instance-configuration-source",
        )
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[source_pool],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
                primary_subnet_id="subnet-1",
            ),
        )
        instance_configuration = _instance_configuration()
        del instance_configuration.instance_details.launch_details.metadata["user_data"]
        backend = _backend(source, instance_configuration)

        with self.assertRaisesRegex(OciDiscoveryError, "user_data"):
            backend.validate_cluster_network_pool_template(
                "cluster-network-source",
                "instance-pool-source",
                "oke-rdma-2",
            )

    def test_validate_cluster_network_pool_template_rejects_deferred_fields(self):
        source_pool = SimpleNamespace(
            id="instance-pool-source",
            instance_configuration_id="instance-configuration-source",
        )
        source = SimpleNamespace(
            compartment_id="compartment-1",
            lifecycle_state="RUNNING",
            instance_pools=[source_pool],
            placement_configuration=SimpleNamespace(
                availability_domain="UK-LONDON-1-AD-3",
                placement_constraint="PACKED_DISTRIBUTION_MULTI_BLOCK",
                primary_subnet_id="subnet-1",
            ),
        )
        instance_configuration = _instance_configuration()
        instance_configuration.deferred_fields = ["shape"]
        backend = _backend(source, instance_configuration)

        with self.assertRaisesRegex(OciDiscoveryError, "deferred fields"):
            backend.validate_cluster_network_pool_template(
                "cluster-network-source",
                "instance-pool-source",
                "oke-rdma-2",
            )

    def test_resize_instance_pool(self):
        backend = _backend()

        work_request = backend.resize_instance_pool("pool-1", 4)

        self.assertEqual("wr-instance-pool", work_request)
        _, pool_id, details = backend._compute_mgmt.calls[-1]
        self.assertEqual("pool-1", pool_id)
        self.assertEqual(4, details.size)

    def test_detach_instance_pool_node_with_replacement(self):
        backend = _backend()

        work_request = backend.detach_instance_pool_node(
            "pool-1",
            "instance-1",
            decrement_size=False,
        )

        self.assertEqual("wr-detach", work_request)
        _, pool_id, details = backend._compute_mgmt.calls[-1]
        self.assertEqual("pool-1", pool_id)
        self.assertEqual("instance-1", details.instance_id)
        self.assertFalse(details.is_decrement_size)
        self.assertTrue(details.is_auto_terminate)

    def test_call_wraps_sdk_exception_with_operation(self):
        def fail():
            raise ValueError("bad request")

        with self.assertRaisesRegex(OciDiscoveryError, "Read failed: bad request"):
            OciBackend._call("Read", fail)

    def test_generic_work_request_failure_includes_service_errors(self):
        backend = _backend()
        backend._work_requests = _WorkRequests(
            status="FAILED",
            percent=60,
            errors=[SimpleNamespace(code="1611", message="Insufficient capacity")],
        )

        result = backend.get_work_request_status(
            "ocid1.coreservicesworkrequest.oc1.region.example"
        )

        self.assertTrue(result.failed)
        self.assertEqual(60.0, result.percent_complete)
        self.assertEqual(("1611: Insufficient capacity",), result.errors)

    def test_oke_work_request_uses_container_engine_client(self):
        backend = _backend()
        work_requests = _WorkRequests(status="IN_PROGRESS", percent=25)
        backend._container_engine.get_work_request = work_requests.get_work_request
        backend._container_engine.list_work_request_errors = (
            work_requests.list_work_request_errors
        )

        result = backend.get_work_request_status(
            "ocid1.clustersworkrequest.oc1.region.example"
        )

        self.assertEqual("IN_PROGRESS", result.status)
        self.assertEqual(25.0, result.percent_complete)
        self.assertEqual(
            "get_work_request",
            work_requests.calls[0][0],
        )

    def test_oke_work_request_failure_uses_compartment_for_errors(self):
        backend = _backend()
        work_requests = _WorkRequests(
            status="FAILED",
            percent=50,
            errors=[SimpleNamespace(code="LimitExceeded", message="GPU limit reached")],
        )
        backend._container_engine.get_work_request = work_requests.get_work_request
        backend._container_engine.list_work_request_errors = (
            work_requests.list_work_request_errors
        )

        result = backend.get_work_request_status(
            "ocid1.clustersworkrequest.oc1.region.example",
            compartment_id="compartment-1",
        )

        self.assertTrue(result.failed)
        self.assertEqual(("LimitExceeded: GPU limit reached",), result.errors)
        self.assertEqual(
            (
                "list_work_request_errors",
                "compartment-1",
                "ocid1.clustersworkrequest.oc1.region.example",
            ),
            work_requests.calls[-1],
        )

    def test_oke_work_request_failure_requires_compartment(self):
        backend = _backend()
        work_requests = _WorkRequests(status="FAILED")
        backend._container_engine.get_work_request = work_requests.get_work_request

        with self.assertRaisesRegex(OciDiscoveryError, "requires the compartment"):
            backend.get_work_request_status(
                "ocid1.clustersworkrequest.oc1.region.example"
            )

    def test_list_resource_work_requests_returns_typed_summaries(self):
        backend = _backend()
        backend._work_requests = _WorkRequests(
            summaries=[
                SimpleNamespace(
                    id="work-request-1",
                    status="FAILED",
                    percent_complete=60,
                ),
                SimpleNamespace(status="ACCEPTED", percent_complete=0),
            ]
        )

        results = backend.list_resource_work_requests("compartment-1", "resource-1")

        self.assertEqual(1, len(results))
        self.assertEqual("work-request-1", results[0].work_request_id)
        self.assertTrue(results[0].failed)
        self.assertEqual(
            (
                "list_work_requests",
                "compartment-1",
                {"resource_id": "resource-1"},
            ),
            backend._work_requests.calls[0],
        )


class OciBackendDiscoveryTests(unittest.TestCase):
    def test_addon_compatibility_blocks_only_unsupported_pinned_version(self):
        backend = _backend()
        backend._container_engine.addon_options = [
            SimpleNamespace(
                name="NvidiaGpuOperator",
                versions=[
                    SimpleNamespace(
                        version_number="v25.3",
                        kubernetes_version_filters=SimpleNamespace(
                            exact_kubernetes_versions=["v1.36.1"],
                            minimal_version=None,
                            maximum_version=None,
                        ),
                    )
                ],
            )
        ]

        compatibility = backend.get_addon_compatibility(
            "v1.36.1",
            [
                AddonInfo(
                    name="NvidiaGpuOperator",
                    version="v24.9",
                    update_mode="MANUAL",
                ),
                AddonInfo(
                    name="NvidiaGpuOperator",
                    version="v24.9",
                    update_mode="AUTOMATIC",
                ),
            ],
        )

        self.assertFalse(compatibility[0].compatible)
        self.assertIn("Pinned version", compatibility[0].reason or "")
        self.assertTrue(compatibility[1].compatible)

    def test_addon_compatibility_uses_selected_pinned_version_not_old_installed_build(self):
        backend = _backend()
        backend._container_engine.addon_options = [
            SimpleNamespace(
                name="NvidiaGpuOperator",
                versions=[
                    SimpleNamespace(
                        version_number="v25.3",
                        kubernetes_version_filters=SimpleNamespace(
                            exact_kubernetes_versions=["v1.36.1"],
                            minimal_version=None,
                            maximum_version=None,
                        ),
                    )
                ],
            )
        ]

        compatibility = backend.get_addon_compatibility(
            "v1.36.1",
            [
                AddonInfo(
                    name="NvidiaGpuOperator",
                    version="v24.9",
                    selected_version="v25.3",
                    update_mode="PINNED",
                )
            ],
        )

        self.assertTrue(compatibility[0].compatible)
        self.assertEqual("v24.9", compatibility[0].installed_version)

    def test_addon_compatibility_requires_a_supported_target_build(self):
        backend = _backend()
        backend._container_engine.addon_options = [
            SimpleNamespace(
                name="NvidiaGpuOperator",
                versions=[
                    SimpleNamespace(
                        version_number="v25.3",
                        kubernetes_version_filters=SimpleNamespace(
                            exact_kubernetes_versions=["v1.35.2"],
                            minimal_version=None,
                            maximum_version=None,
                        ),
                    )
                ],
            )
        ]

        compatibility = backend.get_addon_compatibility(
            "v1.36.1",
            [
                AddonInfo(
                    name="NvidiaGpuOperator",
                    version="v25.3",
                    lifecycle_state="ACTIVE",
                    update_mode="AUTOMATIC",
                )
            ],
        )

        self.assertFalse(compatibility[0].compatible)
        self.assertIn(
            "no supported version",
            compatibility[0].reason or "",
        )

    def test_gpu_memory_cluster_discovery_records_ownership_and_placement(self):
        backend = _backend()
        summary = SimpleNamespace(
            id="gmc-1",
            display_name="oke-gmc",
            lifecycle_state="ACTIVE",
        )
        backend._compute.gpu_memory_clusters = [summary]
        backend._compute.gpu_memory_cluster = SimpleNamespace(
            id="gmc-1",
            display_name="oke-gmc",
            lifecycle_state="ACTIVE",
            compartment_id="compartment-1",
            availability_domain="AD-1",
            size=4,
            instance_configuration_id="instance-configuration-source",
            gpu_memory_fabric_id="fabric-1",
            compute_cluster_id="compute-cluster-1",
            freeform_tags={"mgmt-oke-created": "true"},
        )

        discovered = backend.list_gpu_memory_cluster_pools(
            "compartment-1"
        )[0]

        self.assertEqual("gpu-memory-cluster", discovered.kind)
        self.assertEqual("gmc-1", discovered.gpu_memory_cluster_id)
        self.assertEqual("fabric-1", discovered.gpu_memory_fabric_id)
        self.assertEqual("compute-cluster-1", discovered.compute_cluster_id)
        self.assertTrue(discovered.rdma_enabled)
        self.assertTrue(discovered.created_by_mgmt_oke)

    def test_get_cluster_type(self):
        backend = _backend()
        backend._container_engine.cluster = SimpleNamespace(
            type="ENHANCED_CLUSTER"
        )

        self.assertEqual(
            "ENHANCED_CLUSTER",
            backend.get_cluster_type("cluster-1"),
        )

    def test_get_cluster_compartment_id(self):
        backend = _backend()
        backend._container_engine = _ContainerEngine(
            cluster=SimpleNamespace(compartment_id="compartment-1")
        )

        compartment_id = backend.get_cluster_compartment_id("cluster-1")

        self.assertEqual("compartment-1", compartment_id)
        self.assertEqual(("get_cluster", "cluster-1"), backend._container_engine.calls[-1])

    def test_get_cluster_compartment_id_requires_response_field(self):
        backend = _backend()
        backend._container_engine = _ContainerEngine(cluster=SimpleNamespace())

        with self.assertRaisesRegex(OciDiscoveryError, "did not return a compartment"):
            backend.get_cluster_compartment_id("cluster-1")

    def test_managed_compute_cluster_pool_preserves_placement_metadata(self):
        node_config = SimpleNamespace(
            size=2,
            compute_cluster_id="compute-cluster-1",
            placement_configs=[
                SimpleNamespace(
                    availability_domain="AD-1",
                    host_group_id="host-group-1",
                )
            ],
        )
        pool = SimpleNamespace(
            id="node-pool-1",
            name="oke-rdma",
            node_shape="BM.GPU4.8",
            node_config_details=node_config,
            initial_node_labels=[
                SimpleNamespace(
                    key="oci.oraclecloud.com/slinky-hostname-prefix",
                    value="rdma",
                )
            ],
            nodes=[
                SimpleNamespace(id="instance-active", lifecycle_state="ACTIVE"),
                SimpleNamespace(id="instance-deleted", lifecycle_state="DELETED"),
            ],
        )
        backend = _backend()
        backend._container_engine = _ContainerEngine([pool])

        discovered = backend.list_managed_node_pools("compartment-1", "cluster-1")[0]

        self.assertEqual("compute-cluster", discovered.placement_type)
        self.assertEqual("compute-cluster-1", discovered.compute_cluster_id)
        self.assertEqual({"host-group-1"}, discovered.host_group_ids)
        self.assertEqual("AD-1", discovered.availability_domain)
        self.assertTrue(discovered.rdma_enabled)
        self.assertEqual({"instance-active"}, discovered.oci_instance_ids)
        self.assertEqual("rdma", discovered.labels["oci.oraclecloud.com/slinky-hostname-prefix"])

    def test_managed_host_group_pool_preserves_placement_metadata(self):
        node_config = SimpleNamespace(
            size=1,
            compute_cluster_id=None,
            placement_configs=[
                SimpleNamespace(
                    availability_domain="AD-1",
                    host_group_id="host-group-1",
                )
            ],
        )
        pool = SimpleNamespace(
            id="node-pool-1",
            name="oke-gpu-host-group",
            node_shape="VM.GPU.A10.1",
            node_config_details=node_config,
            initial_node_labels=[],
            nodes=[],
        )
        backend = _backend()
        backend._container_engine = _ContainerEngine([pool])

        discovered = backend.list_managed_node_pools(
            "compartment-1",
            "cluster-1",
        )[0]

        self.assertEqual("host-group", discovered.placement_type)
        self.assertIsNone(discovered.compute_cluster_id)
        self.assertEqual({"host-group-1"}, discovered.host_group_ids)
        self.assertFalse(discovered.rdma_enabled)

    def test_list_cluster_addons_maps_lifecycle_version_and_error(self):
        addons = [
            SimpleNamespace(
                name="NodeFeatureDiscovery",
                lifecycle_state="ACTIVE",
                current_installed_version="v0.17.3-1",
                version=None,
                addon_error=None,
            ),
            SimpleNamespace(
                name="NvidiaNetworkOperator",
                lifecycle_state="NEEDS_ATTENTION",
                current_installed_version=None,
                version="v25.10.0",
                addon_error="rollout failed",
            ),
        ]
        backend = _backend()
        backend._container_engine = _ContainerEngine(addons=addons)

        discovered = backend.list_cluster_addons("cluster-1")

        self.assertEqual("v0.17.3-1", discovered[0].version)
        self.assertIsNone(discovered[0].selected_version)
        self.assertEqual("AUTOMATIC", discovered[0].update_mode)
        self.assertTrue(discovered[0].active)
        self.assertEqual("v25.10.0", discovered[1].selected_version)
        self.assertEqual("PINNED", discovered[1].update_mode)
        self.assertEqual("rollout failed", discovered[1].error)
        self.assertFalse(discovered[1].active)

    def test_cluster_network_discovery_records_owned_instance_configuration(self):
        cluster_network = SimpleNamespace(
            id="cluster-network-1",
            display_name="rdma-batch",
            lifecycle_state="RUNNING",
            freeform_tags={"mgmt-oke-created": "true"},
            instance_pools=[SimpleNamespace(id="instance-pool-1")],
        )
        instance_pool = SimpleNamespace(
            id="instance-pool-1",
            display_name="rdma-batch",
            lifecycle_state="RUNNING",
            size=1,
            instance_configuration_id="instance-configuration-1",
            freeform_tags={"mgmt-oke-created": "true"},
            placement_configurations=[],
        )
        backend = _backend()
        backend._compute_mgmt = _ComputeManagement(
            cluster_network=cluster_network,
            instance_pools=[instance_pool],
            instances={"instance-pool-1": []},
        )

        discovered = backend.list_cluster_network_pools("compartment-1")[0]

        self.assertEqual(
            "instance-configuration-1",
            discovered.instance_configuration_id,
        )
        self.assertTrue(discovered.created_by_mgmt_oke)

    def test_instance_pool_discovery_hides_managed_backing_pools(self):
        pools = [
            SimpleNamespace(
                id="pool-compute-cluster",
                display_name="oke-rdma-backing",
                lifecycle_state="RUNNING",
                size=2,
                placement_configurations=[
                    SimpleNamespace(
                        availability_domain="AD-1",
                        compute_cluster_id="compute-cluster-1",
                    )
                ],
            ),
            SimpleNamespace(
                id="pool-overlap",
                display_name="oke-rdma-overlap",
                lifecycle_state="RUNNING",
                size=1,
                placement_configurations=[],
            ),
            SimpleNamespace(
                id="pool-standalone",
                display_name="standalone",
                lifecycle_state="RUNNING",
                size=1,
                instance_configuration_id="instance-configuration-standalone",
                freeform_tags={"mgmt-oke-created": "true"},
                placement_configurations=[],
            ),
        ]
        instances = {
            "pool-compute-cluster": [
                SimpleNamespace(id="instance-cc", lifecycle_state="RUNNING")
            ],
            "pool-overlap": [
                SimpleNamespace(id="instance-managed", lifecycle_state="RUNNING")
            ],
            "pool-standalone": [
                SimpleNamespace(id="instance-standalone", lifecycle_state="RUNNING")
            ],
        }
        backend = _backend()
        backend._compute_mgmt = _ComputeManagement(instance_pools=pools, instances=instances)
        backend._compute = _Compute(
            {
                "instance-cc": "BM.GPU4.8",
                "instance-managed": "BM.GPU4.8",
                "instance-standalone": "VM.Standard.E5.Flex",
            }
        )

        discovered = backend.list_instance_pools(
            "compartment-1",
            skip_compute_cluster_ids={"compute-cluster-1"},
            skip_instance_ids={"instance-managed"},
        )

        self.assertEqual(["standalone"], [pool.name for pool in discovered])
        self.assertEqual("instance-pool", discovered[0].placement_type)
        self.assertEqual(
            "instance-configuration-standalone",
            discovered[0].instance_configuration_id,
        )
        self.assertTrue(discovered[0].created_by_mgmt_oke)

    def test_instance_pool_in_unmanaged_compute_cluster_remains_visible(self):
        pool = SimpleNamespace(
            id="pool-1",
            display_name="external-compute-cluster-pool",
            lifecycle_state="RUNNING",
            size=0,
            placement_configurations=[
                SimpleNamespace(
                    availability_domain="AD-1",
                    compute_cluster_id="compute-cluster-external",
                )
            ],
        )
        backend = _backend()
        backend._compute_mgmt = _ComputeManagement(instance_pools=[pool])

        discovered = backend.list_instance_pools("compartment-1")

        self.assertEqual(1, len(discovered))
        self.assertEqual("compute-cluster", discovered[0].placement_type)
        self.assertEqual("compute-cluster-external", discovered[0].compute_cluster_id)


if __name__ == "__main__":
    unittest.main()
