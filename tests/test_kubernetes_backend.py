from __future__ import annotations

from types import SimpleNamespace
import unittest

from oke_hpc_mgmt.backends.kubernetes import (
    KubernetesBackend,
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
):
    owners = [SimpleNamespace(kind=owner_kind)] if owner_kind else []
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels=labels or {},
            annotations={},
            owner_references=owners,
        ),
        spec=SimpleNamespace(node_name=node),
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
        ),
    )


class _CoreApi:
    def __init__(self, nodes, pods):
        self.nodes = nodes
        self.pods = pods

    def list_node(self):
        return SimpleNamespace(items=self.nodes)

    def list_pod_for_all_namespaces(self):
        return SimpleNamespace(items=self.pods)


class _Client:
    def __init__(self, core):
        self.core = core

    def CoreV1Api(self):
        return self.core


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


if __name__ == "__main__":
    unittest.main()
