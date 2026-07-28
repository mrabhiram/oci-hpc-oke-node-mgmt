import unittest
from types import SimpleNamespace

from oke_hpc_mgmt.backends.oci import (
    OciBackend,
    OciDiscoveryError,
    _clone_pool_freeform_tags,
    _retarget_oke_node_labels,
)
from oke_hpc_mgmt.models import PoolCreateSpec


class _Model:
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
        return SimpleNamespace(data=self.cluster_network)

    def list_cluster_networks(self, compartment_id):
        self.calls.append(("list_cluster_networks", compartment_id))
        cluster_networks = (
            [] if self.cluster_network is None else [self.cluster_network]
        )
        return SimpleNamespace(data=cluster_networks)

    def update_cluster_network(self, cluster_network_id, details):
        self.calls.append(("update_cluster_network", cluster_network_id, details))
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

    def update_instance_pool(self, instance_pool_id, details):
        self.calls.append(("update_instance_pool", instance_pool_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-instance-pool"})

    def get_instance_pool(self, instance_pool_id):
        self.calls.append(("get_instance_pool", instance_pool_id))
        instance_pool = next(
            pool
            for pool in self.instance_pools
            if pool.id == instance_pool_id
        )
        return SimpleNamespace(data=instance_pool)

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
        self.calls = []

    def get_cluster(self, cluster_id):
        self.calls.append(("get_cluster", cluster_id))
        return SimpleNamespace(data=self.cluster)

    def list_node_pools(self, **kwargs):
        self.calls.append(("list_node_pools", kwargs))
        return SimpleNamespace(data=[SimpleNamespace(id=pool.id) for pool in self.node_pools])

    def get_node_pool(self, node_pool_id):
        self.calls.append(("get_node_pool", node_pool_id))
        pool = next(pool for pool in self.node_pools if pool.id == node_pool_id)
        return SimpleNamespace(data=pool)

    def update_node_pool(self, node_pool_id, details):
        self.calls.append(("update_node_pool", node_pool_id, details))
        return SimpleNamespace(headers={"opc-work-request-id": "wr-node-pool"})

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
    def __init__(self, shapes=None):
        self.shapes = shapes or {}

    def get_instance(self, instance_id):
        return SimpleNamespace(data=SimpleNamespace(shape=self.shapes.get(instance_id)))

    def list_shapes(self, compartment_id, **kwargs):
        return SimpleNamespace(
            data=[
                SimpleNamespace(shape="BM.GPU4.8", local_disks=8),
                SimpleNamespace(shape="VM.Standard.E5.Flex", local_disks=0),
                SimpleNamespace(shape="VM.GPU.A10.1", local_disks=0),
                SimpleNamespace(shape="VM.GPU.A10.2", local_disks=0),
            ]
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
            )
        )
    )
    backend._compute_mgmt = _ComputeManagement(
        cluster_network,
        instance_configuration or _instance_configuration(),
    )
    backend._container_engine = _ContainerEngine(node_pools=node_pools)
    backend._compute = _Compute()
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
        backend = _backend(instance_configuration=owned)

        backend.delete_mgmt_created_instance_configuration(
            "instance-configuration-source"
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

    def test_resize_managed_node_pool_sends_only_size(self):
        backend = _backend()

        work_request = backend.resize_managed_node_pool("node-pool-1", 3)

        self.assertEqual("wr-node-pool", work_request)
        _, node_pool_id, details = backend._container_engine.calls[-1]
        self.assertEqual("node-pool-1", node_pool_id)
        self.assertEqual({"size": 3}, details.node_config_details.__dict__)

    def test_resize_managed_node_pool_rejects_negative_size(self):
        backend = _backend()

        with self.assertRaises(OciDiscoveryError):
            backend.resize_managed_node_pool("node-pool-1", -1)

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
        self.assertTrue(discovered[0].active)
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
