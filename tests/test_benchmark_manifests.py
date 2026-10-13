from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Literal

from oke_hpc_mgmt.benchmarks.manifests import (
    CASE_LABEL, ExecutionOptions, RANK_SCRIPT, ROLE_LABEL, RUN_LABEL, case_name, launcher_script,
    gpu_preflight_script, nccl_argv, owned_labels, render_mpijob, validate_run_id, worker_script,
)
from oke_hpc_mgmt.benchmarks.models import BenchmarkError
from oke_hpc_mgmt.benchmarks.planning import plan_healthchecks
from oke_hpc_mgmt.benchmarks.profiles import PROFILES, execution_fingerprint
from test_benchmark_planning import inventory, request


RUN = "hc-0123456789abcdefabcd"


class BenchmarkManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = request(mode="all-nodes")
        self.case = plan_healthchecks(inventory(2), self.request).cases[0]
        self.options = ExecutionOptions()

    def render(self, **changes):
        case = replace(self.case, **changes)
        if case.nodes:
            case = replace(case, profile_fingerprint=execution_fingerprint(case.nodes[0].shape, case.network, self.request))
        return render_mpijob(RUN, case, self.request, self.options)

    def test_recorded_profile_cannot_be_replaced_by_new_renderer_defaults(self) -> None:
        with self.assertRaisesRegex(BenchmarkError, "Recorded execution profile"):
            render_mpijob(RUN, replace(self.case, profile_fingerprint="f" * 64), self.request, self.options)

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
        self.assertEqual([{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [name]}]}
                          for name in ("gpu-01", "gpu-02")], terms)
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
            if not profile.vf_count:
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

    def test_rtxpro_preserves_ucx_and_tree_without_advertising_vf(self) -> None:
        nodes = tuple(replace(node, shape="BM.GPU.RTXPRO.8") for node in self.case.nodes)
        schedulers: tuple[tuple[Literal["direct", "kueue"], str | None], ...] = (("direct", None), ("kueue", "gpu-validation"))
        for scheduler, queue in schedulers:
            self.request = replace(self.request, scheduler=scheduler, queue=queue)
            job = self.render(nodes=nodes)
            replicas = job["spec"]["mpiReplicaSpecs"]
            command = shlex.split(replicas["Launcher"]["template"]["spec"]["containers"][0]["command"][-1].splitlines()[-1])
            self.assertEqual("ucx", command[command.index("pml") + 1])
            self.assertIn("NCCL_ALGO=Tree", command)
            worker = replicas["Worker"]["template"]["spec"]
            self.assertTrue(worker["hostNetwork"])
            self.assertEqual(8, worker["containers"][0]["resources"]["limits"]["nvidia.com/gpu"])
            with self.assertRaisesRegex(BenchmarkError, "no reviewed vf"):
                self.render(nodes=nodes, network="vf")

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
        replicas = self.render(nodes=nodes)["spec"]["mpiReplicaSpecs"]
        for role in ("Worker", "Launcher"):
            pod = replicas[role]["template"]["spec"]
            self.assertEqual([{"key": "nvidia.com/gpu", "value": "present", "operator": "Equal", "effect": "NoSchedule"}],
                             pod["tolerations"])
        self.assertNotIn("nvidia.com/gpu", replicas["Launcher"]["template"]["spec"]["containers"][0]["resources"]["requests"])
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

    def test_launcher_retries_transient_readiness_with_pinned_key_and_bounded_deadline(self) -> None:
        self._exercise_launcher_keys(self.case.network)

    def test_host_network_launcher_matches_port_qualified_keyscan_entries(self) -> None:
        self._exercise_launcher_keys("host")

    def _exercise_launcher_keys(self, network: Literal["local", "host", "vf"]) -> None:
        self.case = replace(self.case, network=network)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hostfile = root / "hostfile"
            hostfile.write_text("worker-0 slots=8\nworker-1 slots=8\n")
            scripts = {
                "mpirun": "#!/bin/sh\necho 'Open MPI test fixture'\n",
                "getent": "#!/bin/sh\n[ \"$DNS_FAIL\" != 1 ] || exit 2\n"
                          "case \"$2\" in worker-0) echo \"${ADDRESS_0:-192.0.2.1} STREAM worker-0\";;\n"
                          "worker-1) echo '2001:db8::2 STREAM worker-1';; *) exit 2;; esac\n",
                "ssh-keyscan": "#!/bin/sh\nhost=$5\n[ \"$4\" = 22 ] || host=\"[$5]:$4\"\n"
                               "echo \"$host ssh-ed25519 synthetic-public-key\"\n",
                "ssh": "#!/bin/sh\n"
                       "known=${2%mgmt-ssh-config}mgmt-known-hosts\n"
                       "awk '$1 == \"HostKeyAlias\" {print $2}' \"$2\" | while read -r alias; do\n"
                       "awk -v alias=\"$alias\" '$1 == alias {found=1} END {exit !found}' \"$known\" || exit 1\n"
                       "done || exit 1\n"
                       "n=0\n[ ! -f \"$COUNT_FILE\" ] || n=$(cat \"$COUNT_FILE\")\n"
                       "n=$((n+1))\necho $n > \"$COUNT_FILE\"\n[ \"$FAIL_ALWAYS\" != 1 ] && [ $n -gt 2 ]\n",
                "timeout": "#!/bin/sh\n"
                           "if [ \"$1\" = 5s ]; then shift; exec \"$@\"; fi\n"
                           "test \"$OMPI_MCA_orte_keep_fqdn_hostnames\" = 1 || exit 11\n"
                           "test \"$PRTE_MCA_prte_keep_fqdn_hostnames\" = 1 || exit 12\n"
                           "test \"$OMPI_MCA_plm_rsh_no_tree_spawn\" = 1 || exit 13\n"
                           "test \"$PRTE_MCA_plm_ssh_no_tree_spawn\" = 1 || exit 14\n"
                           "case \"$PRTE_MCA_plm_ssh_args\" in *StrictHostKeyChecking=yes*) ;; *) exit 15;; esac\n"
                           "echo executed > \"$EXECUTED_FILE\"\n",
                "sleep": "#!/bin/sh\n/bin/sleep 0.2\n",
            }
            for name, body in scripts.items():
                tool = root / name
                tool.write_text(body)
                tool.chmod(0o700)
            script = launcher_script(self.case, self.request, replace(self.options, startup_timeout=3))
            script = script.replace("/etc/mpi/hostfile", shlex.quote(str(hostfile)))
            # Keep even the launcher's ephemeral files inside the fixture.
            script = script.replace("/tmp/mgmt-", str(root / "mgmt-"))
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                   "COUNT_FILE": str(root / "count"), "EXECUTED_FILE": str(root / "executed"), "FAIL_ALWAYS": "0"}
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("4", (root / "count").read_text().strip())
            self.assertEqual(["MGMT_MPI_SSH_READY\tworker-0", "MGMT_MPI_SSH_READY\tworker-1"],
                             [line for line in result.stdout.splitlines() if line.startswith("MGMT_MPI_SSH_READY")])
            self.assertTrue((root / "executed").exists())
            config = (root / "mgmt-ssh-config").read_text()
            aliases = ("[192.0.2.1]:2222", "[2001:db8::2]:2222") if network == "host" else ("192.0.2.1", "2001:db8::2")
            self.assertEqual(f"Host worker-0\n  HostName 192.0.2.1\n  HostKeyAlias {aliases[0]}\n"
                             f"Host worker-1\n  HostName 2001:db8::2\n  HostKeyAlias {aliases[1]}\n", config)
            self.assertEqual(1, script.count("ssh-keyscan"))
            self.assertIn("StrictHostKeyChecking=yes", script)
            (root / "executed").unlink()
            result = subprocess.run(["bash", "-c", script], env={**env, "FAIL_ALWAYS": "1"},
                                    text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("readiness timed out", result.stderr)
            self.assertFalse((root / "executed").exists())
            result = subprocess.run(["bash", "-c", script], env={**env, "DNS_FAIL": "1"},
                                    text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("DNS startup timed out", result.stderr)
            self.assertFalse((root / "executed").exists())
            for address in ("2001:db8::2", "bad;command"):
                result = subprocess.run(["bash", "-c", script], env={**env, "ADDRESS_0": address},
                                        text=True, capture_output=True, timeout=10, check=False)
                with self.subTest(address=address):
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse((root / "executed").exists())

    def test_runtime_gpu_validation_rejects_mig_duplicate_missing_or_unknown_devices(self) -> None:
        uuid = "GPU-00000000-0000-0000-0000-000000000001"
        second = "GPU-00000000-0000-0000-0000-000000000002"
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "nvidia-smi"
            tool.write_text("#!/bin/sh\ncase \"$1\" in\n--query-gpu=uuid) printf '%s\\n' \"$GPU_UUIDS\";;\n"
                            "--query-gpu=mig.mode.current) printf '%s\\n' \"$MIG_MODES\";;\n*) exit 1;;\nesac\n")
            tool.chmod(0o700)
            for uuids, modes, expected, success in (
                (uuid, "Disabled", 1, True), (uuid, "[N/A]", 1, True),
                (uuid + "\n" + second, "Disabled\nDisabled", 2, True),
                (uuid, "Enabled", 1, False), (uuid, "Unknown", 1, False),
                (uuid, "", 1, False), ("MIG-" + uuid, "Disabled", 1, False),
                (uuid + "\n" + uuid, "Disabled\nDisabled", 2, False),
                (uuid, "Disabled", 2, False), ("", "Disabled", 1, False),
            ):
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + gpu_preflight_script(expected)],
                                        env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                                             "GPU_UUIDS": uuids, "MIG_MODES": modes},
                                        text=True, capture_output=True, check=False)
                with self.subTest(uuids=uuids, modes=modes, expected=expected):
                    self.assertEqual(success, result.returncode == 0, result.stderr)
                    self.assertEqual(success, "MGMT_GPU_MODE_VERIFIED" in result.stdout)
        for count in (0, True, 7, 16):
            with self.assertRaises(BenchmarkError):
                gpu_preflight_script(count)

    def test_rank_keeps_diagnostics_out_of_partial_rows_and_preserves_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "identity"
            identity.write_text("export MGMT_NODE_NAME=gpu-01\nexport MGMT_POD_UID=pod-uid\n")
            script = RANK_SCRIPT.replace("/tmp/mgmt-worker-identity", shlex.quote(str(identity)))
            script = script.replace("/tmp/mgmt-nccl-debug.XXXXXXXX", shlex.quote(str(root / "debug.XXXXXXXX")))
            env = {**os.environ, "OMPI_COMM_WORLD_RANK": "0", "OMPI_COMM_WORLD_LOCAL_RANK": "0"}
            program = "printf '8589934592 2147483648 float sum -1 '; " \
                      "printf 'NCCL INFO diagnostic during measurement\\n' > \"$NCCL_DEBUG_FILE\"; " \
                      "printf '200 43 43 0 201 42 42 0\\n'; exit 0"
            result = subprocess.run(["bash", "-c", script, "rank", "bash", "-c", program],
                                    env=env, text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            row = "8589934592 2147483648 float sum -1 200 43 43 0 201 42 42 0\n"
            self.assertIn(row, result.stdout)
            self.assertLess(result.stdout.index(row), result.stdout.index("NCCL INFO diagnostic"))
            self.assertIn("MGMT_NCCL_DEBUG_END\t0", result.stdout)
            for program, expected in (("exit 17", 17), ("true", 1),
                                      ("printf debug > \"$NCCL_DEBUG_FILE\"; exit 19", 19),
                                      ("head -c 1048577 /dev/zero > \"$NCCL_DEBUG_FILE\"", 1)):
                result = subprocess.run(["bash", "-c", script, "rank", "bash", "-c", program],
                                        env=env, text=True, capture_output=True, check=False)
                with self.subTest(program=program):
                    self.assertEqual(expected, result.returncode)
                    self.assertLess(len(result.stdout), 1024)
