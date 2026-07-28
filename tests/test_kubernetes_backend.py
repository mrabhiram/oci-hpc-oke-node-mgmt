from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from oke_hpc_mgmt.backends.kubernetes import (
    KubernetesBackend,
    KubernetesDiscoveryError,
    _is_slinky_worker_pod,
    _pod_counts_by_node,
)


def _pod(
    name: str,
    *,
    node: str = "node-1",
    namespace: str = "default",
    labels: dict[str, str] | None = None,
    owner_kind: str | None = None,
    phase: str = "Running",
    empty_dir: bool = False,
    mirror: bool = False,
):
    owners = (
        [SimpleNamespace(kind=owner_kind, name=f"{name}-owner", controller=True)]
        if owner_kind
        else []
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels=labels or {},
            annotations={"kubernetes.io/config.mirror": "hash"} if mirror else {},
            owner_references=owners,
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(
            node_name=node,
            volumes=[SimpleNamespace(empty_dir=SimpleNamespace())] if empty_dir else [],
        ),
        status=SimpleNamespace(phase=phase),
    )


def _node():
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="node-1",
            labels={
                "oke.oraclecloud.com/pool.name": "oke-rdma",
                "node.kubernetes.io/instance-type": "BM.GPU4.8",
            },
            annotations={"nodeset.slinky.slurm.net/hostname-override": "rdma-1"},
        ),
        spec=SimpleNamespace(
            provider_id="oci://ocid1.instance.oc1.lhr.node1",
            unschedulable=False,
            taints=[],
        ),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")],
            addresses=[SimpleNamespace(type="InternalIP", address="10.0.0.1")],
            allocatable={"nvidia.com/gpu": "8", "nvidia.com/rdma-vf": "8"},
            node_info=SimpleNamespace(boot_id="boot-session-1"),
        ),
    )


class _CoreApi:
    def __init__(self, nodes, pods):
        self.nodes = nodes
        self.pods = pods

    def list_node(self):
        return SimpleNamespace(items=self.nodes)

    def list_pod_for_all_namespaces(self, **kwargs):
        return SimpleNamespace(items=self.pods)

    def patch_node(self, name, body):
        self.last_patch = (name, body)

    def create_namespaced_pod_eviction(self, name, namespace, body, **kwargs):
        return None


class _Client:
    def __init__(self, core):
        self.core = core

    def CoreV1Api(self):
        return self.core


class _ApiError(Exception):
    def __init__(self, status: int, reason: str = "error") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _KubernetesModelsClient(_Client):
    def __init__(self, core, policy=None, coordination=None, custom=None):
        super().__init__(core)
        self.policy = policy or Mock()
        self.coordination = coordination or Mock()
        self.custom = custom or Mock()

    def PolicyV1Api(self):
        return self.policy

    def CoordinationV1Api(self):
        return self.coordination

    def CustomObjectsApi(self):
        return self.custom

    @staticmethod
    def V1ObjectMeta(**kwargs):
        kwargs.setdefault("resource_version", None)
        return SimpleNamespace(**kwargs)

    @staticmethod
    def V1DeleteOptions(**kwargs):
        return SimpleNamespace(**kwargs)

    @staticmethod
    def V1Eviction(**kwargs):
        return SimpleNamespace(**kwargs)

    @staticmethod
    def V1LeaseSpec(**kwargs):
        return SimpleNamespace(**kwargs)

    @staticmethod
    def V1Lease(**kwargs):
        return SimpleNamespace(**kwargs)

    @staticmethod
    def V1ConfigMap(**kwargs):
        return SimpleNamespace(**kwargs)

    @staticmethod
    def V1Preconditions(**kwargs):
        return SimpleNamespace(**kwargs)


class KubernetesBackendTests(unittest.TestCase):
    def test_slinky_worker_identification_uses_upstream_labels(self):
        pod = _pod(
            "slurmd",
            labels={
                "app.kubernetes.io/name": "slurmd",
                "app.kubernetes.io/component": "worker",
            },
        )
        nodeset_pod = _pod(
            "nodeset",
            labels={"slinky.slurm.net/nodeset": "rdma"},
        )

        self.assertTrue(_is_slinky_worker_pod(pod))
        self.assertTrue(_is_slinky_worker_pod(nodeset_pod))
        self.assertFalse(_is_slinky_worker_pod(_pod("ordinary")))

    def test_pod_counts_protect_slinky_daemonset_workers(self):
        pods = [
            _pod(
                "slurmd",
                labels={
                    "app.kubernetes.io/name": "slurmd",
                    "app.kubernetes.io/component": "worker",
                },
                owner_kind="DaemonSet",
            ),
            _pod("system", namespace="kube-system"),
            _pod("workload"),
            _pod("completed", phase="Succeeded"),
        ]

        counts = _pod_counts_by_node(pods)["node-1"]

        self.assertEqual(1, counts["slinky"])
        self.assertEqual(1, counts["daemonset"])
        self.assertEqual(1, counts["system"])
        self.assertEqual(1, counts["workload"])

    def test_list_nodes_copies_annotations_resources_and_slinky_counts(self):
        pods = [
            _pod(
                "slurmd",
                labels={"slinky.slurm.net/nodeset": "rdma"},
                owner_kind="DaemonSet",
            )
        ]
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _Client(_CoreApi([_node()], pods))

        node = backend.list_nodes()[0]

        self.assertEqual("rdma-1", node.slurm_name)
        self.assertEqual("8", node.rdma_vf_allocatable)
        self.assertEqual(1, node.slinky_workload_pods)
        self.assertEqual("oke-rdma", node.pool_name)
        self.assertEqual("ocid1.instance.oc1.lhr.node1", node.instance_ocid)
        self.assertEqual("boot-session-1", node.boot_id)

    def test_list_nodes_reports_kubelet_version(self):
        node_object = _node()
        node_object.status.node_info.kubelet_version = "v1.35.3"
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _Client(_CoreApi([node_object], []))

        self.assertEqual("v1.35.3", backend.list_nodes()[0].kubelet_version)

    def test_upgrade_pod_check_is_read_only_and_ignores_scheduler_infrastructure(self):
        pods = [
            _pod("training", owner_kind="Job"),
            _pod("daemon", owner_kind="DaemonSet"),
            _pod(
                "controller",
                labels={
                    "app.kubernetes.io/name": "kueue",
                    "app.kubernetes.io/component": "controller",
                },
            ),
        ]
        core = _CoreApi([], pods)
        core.create_namespaced_pod_eviction = Mock()
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(core)

        blockers = backend.list_upgrade_blocking_pods({"node-1"})

        self.assertEqual(["training"], [pod.name for pod in blockers])
        core.create_namespaced_pod_eviction.assert_not_called()

    def test_slurmctld_exec_requires_one_running_named_container(self):
        slurmctld = SimpleNamespace(
            metadata=SimpleNamespace(
                name="slurm-controller-0",
                namespace="slurm",
            ),
            spec=SimpleNamespace(
                containers=[SimpleNamespace(name="slurmctld")]
            ),
            status=SimpleNamespace(phase="Running"),
        )
        core = _CoreApi([], [slurmctld])
        core.connect_get_namespaced_pod_exec = Mock()
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _Client(core)

        with patch(
            "kubernetes.stream.stream",
            return_value="NodeName=worker-1 State=DRAIN",
        ) as stream:
            result = backend.exec_slurmctld(
                ("scontrol", "show", "node", "worker-1", "--oneliner")
            )

        self.assertEqual("NodeName=worker-1 State=DRAIN", result)
        self.assertEqual(
            ["scontrol", "show", "node", "worker-1", "--oneliner"],
            stream.call_args.kwargs["command"],
        )
        self.assertEqual("slurmctld", stream.call_args.kwargs["container"])
        core.pods.append(
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="slurm-controller-1",
                    namespace="slurm",
                ),
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name="slurmctld")]
                ),
                status=SimpleNamespace(phase="Running"),
            )
        )

        with self.assertRaisesRegex(
            KubernetesDiscoveryError,
            "exactly one running slurmctld container, found 2",
        ):
            backend.exec_slurmctld(("squeue",))

    def test_kueue_discovery_distinguishes_absent_crds_from_rbac_failure(self):
        missing = Mock()
        missing.list_cluster_custom_object.side_effect = _ApiError(
            404,
            "missing",
        )
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(
            _CoreApi([], []),
            custom=missing,
        )
        self.assertEqual([], backend.get_kueue_summary().cluster_queues)

        forbidden = Mock()
        forbidden.list_cluster_custom_object.side_effect = _ApiError(
            403,
            "forbidden",
        )
        backend._client = _KubernetesModelsClient(
            _CoreApi([], []),
            custom=forbidden,
        )
        with self.assertRaisesRegex(
            KubernetesDiscoveryError,
            "Unable to verify Kueue",
        ):
            backend.get_kueue_summary()

    def test_upgrade_checkpoint_uses_resource_version_for_replace_and_delete(self):
        from oke_hpc_mgmt.upgrades import UpgradeCheckpoint

        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster",
            source_version="v1.34.1",
            target_version="v1.35.2",
            control_plane_steps=("v1.35.2",),
            pool_order=("cpu",),
            strategies={"cpu": "boot-volume-replace"},
        )
        core = _CoreApi([], [])
        stored = SimpleNamespace(
            metadata=SimpleNamespace(resource_version="7"),
            data={"checkpoint.json": checkpoint.to_json()},
        )
        core.read_namespaced_config_map = Mock(return_value=stored)
        core.replace_namespaced_config_map = Mock(
            return_value=SimpleNamespace(
                metadata=SimpleNamespace(resource_version="8")
            )
        )
        core.delete_namespaced_config_map = Mock()
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(core)

        restored, resource_version = backend.read_upgrade_checkpoint()
        written = backend.write_upgrade_checkpoint(restored, resource_version)
        backend.delete_upgrade_checkpoint(written)

        self.assertEqual(checkpoint, restored)
        self.assertEqual("8", written)
        body = core.replace_namespaced_config_map.call_args.args[2]
        self.assertEqual("7", body.metadata.resource_version)
        delete_body = core.delete_namespaced_config_map.call_args.kwargs["body"]
        self.assertEqual("8", delete_body.preconditions.resource_version)

    def test_upgrade_checkpoint_reports_optimistic_concurrency_conflict(self):
        from oke_hpc_mgmt.upgrades import UpgradeCheckpoint

        checkpoint = UpgradeCheckpoint.create(
            cluster_id="cluster",
            source_version="v1.34.1",
            target_version="v1.35.2",
            control_plane_steps=("v1.35.2",),
            pool_order=(),
            strategies={},
        )
        core = _CoreApi([], [])
        core.replace_namespaced_config_map = Mock(
            side_effect=_ApiError(409, "conflict")
        )
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(core)

        with self.assertRaisesRegex(
            KubernetesDiscoveryError,
            "changed concurrently",
        ):
            backend.write_upgrade_checkpoint(checkpoint, "7")

    def test_cluster_connection_data_returns_active_host_and_base64_ca(self):
        class Configuration:
            @staticmethod
            def get_default_copy():
                return SimpleNamespace(
                    host="https://10.0.0.10:6443/",
                    ssl_ca_cert=str(ca_path),
                )

        with TemporaryDirectory() as directory:
            ca_path = Path(directory) / "ca.crt"
            ca_path.write_bytes(b"certificate")
            client = _Client(_CoreApi([], []))
            client.Configuration = Configuration
            backend = KubernetesBackend()
            backend._loaded = True
            backend._client = client

            endpoint, certificate = backend.cluster_connection_data()

        self.assertEqual("10.0.0.10:6443", endpoint)
        self.assertEqual("Y2VydGlmaWNhdGU=", certificate)

    def test_list_nodes_tolerates_pod_list_failure(self):
        core = _CoreApi([_node()], [])

        def fail():
            raise RuntimeError("forbidden")

        core.list_pod_for_all_namespaces = fail
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _Client(core)

        node = backend.list_nodes()[0]

        self.assertEqual(0, node.running_workload_pods)
        self.assertEqual(0, node.slinky_workload_pods)

    def test_cordon_and_uncordon_patch_only_unschedulable_state(self):
        core = _CoreApi([], [])
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _Client(core)

        backend.cordon_node("node-1")
        self.assertEqual(("node-1", {"spec": {"unschedulable": True}}), core.last_patch)
        backend.uncordon_node("node-1")
        self.assertEqual(("node-1", {"spec": {"unschedulable": False}}), core.last_patch)

    def test_list_drain_pods_classifies_daemonsets_mirrors_emptydir_and_blockers(self):
        pods = [
            _pod("job", owner_kind="Job", empty_dir=True),
            _pod("daemon", owner_kind="DaemonSet"),
            _pod("mirror", mirror=True),
            _pod("done", phase="Succeeded"),
        ]
        core = _CoreApi([], pods)
        core.create_namespaced_pod_eviction = Mock(side_effect=_ApiError(429, "PDB blocked"))
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(core)

        results = backend.list_drain_pods("node-1")

        self.assertEqual(["daemon", "job", "mirror"], [pod.name for pod in results])
        job = next(pod for pod in results if pod.name == "job")
        self.assertTrue(job.has_empty_dir)
        self.assertEqual("Job/job-owner", job.controller)
        self.assertIn("PDB blocked", job.eviction_blocker or "")
        self.assertFalse(next(pod for pod in results if pod.name == "daemon").evictable)
        self.assertFalse(next(pod for pod in results if pod.name == "mirror").evictable)
        core.create_namespaced_pod_eviction.assert_called_once()

    def test_evict_drain_pods_submits_once_then_waits_for_deletion(self):
        pod = _pod("job", owner_kind="Job")
        core = _CoreApi([], [])
        core.read_namespaced_pod = Mock(side_effect=[pod, _ApiError(404, "gone")])
        core.create_namespaced_pod_eviction = Mock()
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(core)

        with patch("oke_hpc_mgmt.backends.kubernetes.time.sleep"):
            backend.evict_drain_pods(
                [
                    SimpleNamespace(
                        namespace="default",
                        name="job",
                        evictable=True,
                    )
                ],
                timeout_seconds=5,
                poll_interval_seconds=1,
            )

        core.create_namespaced_pod_eviction.assert_called_once()

    def test_mutation_lease_is_created_and_released(self):
        coordination = Mock()
        created: list[object] = []

        def read(name, namespace):
            if not created:
                raise _ApiError(404, "missing")
            return created[0]

        coordination.read_namespaced_lease.side_effect = read
        coordination.create_namespaced_lease.side_effect = lambda namespace, body: created.append(body)
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(
            _CoreApi([], []), coordination=coordination
        )

        with backend.mutation_lease(duration_seconds=60) as holder:
            self.assertTrue(holder)
            self.assertEqual(holder, created[0].spec.holder_identity)

        coordination.delete_namespaced_lease.assert_called_once_with(
            "mgmt-oke-mutation", "kube-system"
        )

    def test_mutation_lease_rejects_active_holder_with_naive_datetime(self):
        coordination = Mock()
        coordination.read_namespaced_lease.return_value = SimpleNamespace(
            metadata=SimpleNamespace(resource_version="1"),
            spec=SimpleNamespace(
                holder_identity="other",
                renew_time=datetime.now(timezone.utc).replace(tzinfo=None),
                acquire_time=None,
                lease_duration_seconds=600,
            ),
        )
        backend = KubernetesBackend()
        backend._loaded = True
        backend._client = _KubernetesModelsClient(
            _CoreApi([], []), coordination=coordination
        )

        with self.assertRaisesRegex(KubernetesDiscoveryError, "Another mutation is active"):
            with backend.mutation_lease():
                pass


if __name__ == "__main__":
    unittest.main()
