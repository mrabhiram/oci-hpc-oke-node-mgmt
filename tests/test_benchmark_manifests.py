from __future__ import annotations

import json
import shlex
import subprocess
import unittest
from dataclasses import replace

from oke_hpc_mgmt.benchmarks.manifests import (
    CASE_LABEL, ExecutionOptions, ROLE_LABEL, RUN_LABEL, case_name, launcher_script,
    nccl_argv, owned_labels, render_mpijob, validate_run_id, worker_script,
)
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks
from oke_hpc_mgmt.benchmarks.profiles import PROFILES
from test_benchmark_planning import inventory, request


RUN = "hc-0123456789abcdefabcd"


class BenchmarkManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = request(mode="all-nodes")
        self.case = plan_healthchecks(inventory(2), self.request).cases[0]
        self.options = ExecutionOptions()

    def render(self, **changes):
        return render_mpijob(RUN, replace(self.case, **changes), self.request, self.options)

    def test_resource_names_and_options_are_bounded(self) -> None:
        validate_run_id(RUN)
        self.assertEqual(f"{RUN}-case-0001", case_name(RUN, self.case))
        self.assertEqual(RUN, owned_labels(RUN)[RUN_LABEL])
        for run in ("test", "../pods", "hc-" + "f" * 21, RUN.upper()):
            with self.subTest(run=run), self.assertRaises(BenchmarkError):
                validate_run_id(run)
        with self.assertRaises(BenchmarkError):
            case_name(RUN, replace(self.case, case_id="../case"))
        for change in ({"concurrency": 0}, {"concurrency": 33}, {"timeout": True},
                       {"timeout": 86401}, {"queue_timeout": 0}, {"startup_timeout": 0},
                       {"network_attachment": "other/rdma"}):
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                replace(self.options, **change).validate()
        with self.assertRaises(BenchmarkError):
            worker_script(8080, True)

    def test_scheduler_exact_nodes_one_worker_per_node_no_node_name_bypass(self) -> None:
        job = self.render()
        spec = job["spec"]
        worker = spec["mpiReplicaSpecs"]["Worker"]
        pod = worker["template"]["spec"]
        self.assertEqual(2, worker["replicas"])
        self.assertEqual(8, spec["slotsPerWorker"])
        self.assertFalse(spec["runLauncherAsWorker"])
        self.assertNotIn("nodeName", pod)
        terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual([{"key": "metadata.name", "operator": "In", "values": ["gpu-01", "gpu-02"]}],
                         terms[0]["matchFields"])
        anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
        self.assertEqual("kubernetes.io/hostname", anti["topologyKey"])
        self.assertEqual(RUN, anti["labelSelector"]["matchLabels"][RUN_LABEL])
        self.assertEqual("worker", anti["labelSelector"]["matchLabels"][ROLE_LABEL])
        self.assertEqual("case-0001", anti["labelSelector"]["matchLabels"][CASE_LABEL])
        self.assertEqual(8, pod["containers"][0]["resources"]["requests"]["nvidia.com/gpu"])
        self.assertFalse(pod["automountServiceAccountToken"])
        launcher = spec["mpiReplicaSpecs"]["Launcher"]["template"]["spec"]
        self.assertNotIn("nvidia.com/gpu", launcher["containers"][0]["resources"]["requests"])
        self.assertFalse(launcher["automountServiceAccountToken"])

    def test_host_network_and_vf_are_distinct_and_shape_specific(self) -> None:
        host = self.render()["spec"]["mpiReplicaSpecs"]
        for role in ("Launcher", "Worker"):
            self.assertTrue(host[role]["template"]["spec"]["hostNetwork"])
            self.assertEqual("ClusterFirstWithHostNet", host[role]["template"]["spec"]["dnsPolicy"])
        container = host["Worker"]["template"]["spec"]["containers"][0]
        self.assertEqual(2222, container["ports"][0]["hostPort"])
        for profile in PROFILES:
            if profile.host_hcas is None:
                continue
            with self.subTest(shape=profile.shape):
                nodes = tuple(replace(node, shape=profile.shape) for node in self.case.nodes)
                job = self.render(network="vf", nodes=nodes)
                worker = job["spec"]["mpiReplicaSpecs"]["Worker"]["template"]
                self.assertNotIn("hostNetwork", worker["spec"])
                annotations = json.loads(worker["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"])
                expected = 8 if profile.shape in {"BM.GPU.H200.8", "BM.GPU.B200.8"} else 16
                self.assertEqual(expected, len(annotations))
                resources = worker["spec"]["containers"][0]["resources"]
                self.assertEqual(expected, resources["limits"]["nvidia.com/rdma-vf"])
                self.assertEqual(expected, resources["requests"]["nvidia.com/rdma-vf"])

    def test_local_has_no_rdma_mount_privilege_or_host_network(self) -> None:
        self.request = request(check_type="active")
        self.case = plan_healthchecks(inventory(1), self.request).cases[0]
        job = self.render()
        worker = job["spec"]["mpiReplicaSpecs"]["Worker"]["template"]["spec"]
        self.assertNotIn("hostNetwork", worker)
        self.assertNotIn("privileged", worker["containers"][0]["securityContext"])
        self.assertFalse(any("hostPath" in volume for volume in worker["volumes"]))
        self.assertEqual(1, job["spec"]["mpiReplicaSpecs"]["Worker"]["replicas"])
        environment = {entry["name"]: entry.get("value") for entry in worker["containers"][0]["env"]}
        self.assertEqual("1", environment["NCCL_IB_DISABLE"])

    def test_owner_reference_and_kueue_suspend_do_not_install_anything(self) -> None:
        self.request = replace(self.request, queue="gpu-validation", scheduler="kueue")
        job = render_mpijob(RUN, self.case, self.request, self.options, owner_uid="run-uid")
        self.assertTrue(job["spec"]["runPolicy"]["suspend"])
        self.assertEqual("gpu-validation", job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"])
        self.assertEqual("run-uid", job["metadata"]["ownerReferences"][0]["uid"])
        self.assertEqual("ConfigMap", job["metadata"]["ownerReferences"][0]["kind"])
        self.assertFalse(job["metadata"]["ownerReferences"][0]["blockOwnerDeletion"])
        with self.assertRaises(BenchmarkError):
            render_mpijob(RUN, self.case, self.request, self.options, owner_uid="")
        self.request = replace(self.request, queue=None)
        with self.assertRaisesRegex(BenchmarkError, "existing --queue"):
            self.render()

    def test_retry_deadline_and_evidence_retention(self) -> None:
        job = self.render()
        policy = job["spec"]["runPolicy"]
        self.assertEqual(0, policy["backoffLimit"])
        self.assertEqual("None", policy["cleanPodPolicy"])
        self.assertNotIn("ttlSecondsAfterFinished", policy)
        self.assertEqual(2400, policy["activeDeadlineSeconds"])
        self.assertEqual("WaitForWorkersReady", job["spec"]["launcherCreationPolicy"])
        for replica in job["spec"]["mpiReplicaSpecs"].values():
            self.assertEqual("Never", replica["restartPolicy"])
            self.assertEqual("Never", replica["template"]["spec"]["restartPolicy"])
            self.assertEqual(2430, replica["template"]["spec"]["activeDeadlineSeconds"])

    def test_taint_tolerations_are_exact_not_wildcards(self) -> None:
        nodes = tuple(replace(node, tolerations=("nvidia.com/gpu=present:NoSchedule",))
                      for node in self.case.nodes)
        pod = self.render(nodes=nodes)["spec"]["mpiReplicaSpecs"]["Worker"]["template"]["spec"]
        self.assertEqual([{"key": "nvidia.com/gpu", "value": "present", "operator": "Equal", "effect": "NoSchedule"}],
                         pod["tolerations"])
        for taint in ("maintenance:NoSchedule", "nvidia.com/gpu:NoExecute", "anything"):
            with self.subTest(taint=taint), self.assertRaises(BenchmarkError):
                self.render(nodes=tuple(replace(node, tolerations=(taint,)) for node in self.case.nodes))

    def test_invalid_cases_fail_before_rendering(self) -> None:
        for changes in (
            {"nodes": ()}, {"nodes": (self.case.nodes[0],) * 2},
            {"nodes": (replace(self.case.nodes[0], pool="other"), self.case.nodes[1])},
            {"nodes": tuple(replace(node, uid="") for node in self.case.nodes)},
            {"nodes": tuple(replace(node, gpu_count=4) for node in self.case.nodes)},
            {"network": "local"}, {"check_type": "active"}, {"nodes": self.case.nodes[:1]},
        ):
            with self.subTest(changes=changes), self.assertRaises(BenchmarkError):
                self.render(**changes)

    def test_shell_scripts_parse_and_overrides_are_literal_arguments(self) -> None:
        value = "literal;$(touch /tmp/not-executed)'quoted"
        self.request = replace(self.request, environment=("NCCL_SOCKET_IFNAME=" + value,))
        script = launcher_script(self.case, self.request, self.options)
        for candidate in (script, worker_script(2222, True), worker_script(22, False)):
            result = subprocess.run(["bash", "-n"], input=candidate, capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
        command = shlex.split(script.splitlines()[-1])
        self.assertIn("NCCL_SOCKET_IFNAME=" + value, command)
        self.assertIn("16", command)
        self.assertNotIn("-c", command[:command.index("/workspace/nccl-tests/build/all_reduce_perf")])
        self.assertIn("StrictHostKeyChecking=yes", script)
        self.assertNotIn("StrictHostKeyChecking=no", script)
        self.assertNotIn("UserKnownHostsFile=/dev/null", script)
        self.assertIn("timeout --signal=TERM --kill-after=30s 1800s", script)
        self.assertIn("MGMT_POD_UID", worker_script(22, False))
        arguments = nccl_argv(self.request)
        self.assertEqual("1", arguments[arguments.index("-c") + 1])
        self.assertEqual("8589934592", arguments[arguments.index("-e") + 1])

    def test_manifest_calls_do_not_share_mutable_labels(self) -> None:
        first = self.render()
        first["metadata"]["labels"][RUN_LABEL] = "changed"
        second = self.render()
        self.assertEqual(RUN, second["metadata"]["labels"][RUN_LABEL])
        worker_labels = first["spec"]["mpiReplicaSpecs"]["Worker"]["template"]["metadata"]["labels"]
        self.assertEqual(RUN, worker_labels[RUN_LABEL])
