# Troubleshooting

This guide covers common installation, discovery, readiness, and mutation
problems when using `mgmt-oke` on an OCI HPC OKE operator node.

## Command Not Found

Check the installed entrypoint and shell path:

```bash
command -v mgmt-oke
command -v kubectl-oke
echo "$PATH"
ls -l /home/ubuntu/bin/mgmt-oke
```

Example output after installation:

```text
/home/ubuntu/bin/mgmt-oke
/home/ubuntu/bin/kubectl-oke
```

If the virtual environment exists but the stable links do not, repeat the
`PATH` steps in [Controller Node Installation](./controller-install.md).

## OCI CLI Not Found During Kubernetes Authentication

An OCI-generated kubeconfig invokes the OCI CLI to generate an OKE token.
Confirm that it is available to the process running `mgmt-oke`:

```bash
command -v oci
oci --version
kubectl get nodes
```

A minimal non-interactive shell might not include `/home/ubuntu/bin` even when
an interactive login does. It might also omit the auth selection used by the
kubeconfig exec plugin. Set both values explicitly for cron, SSH one-liners,
and other non-interactive execution:

```bash
export PATH=/home/ubuntu/bin:/usr/local/bin:/usr/bin:/bin
export OCI_CLI_AUTH=instance_principal
kubectl get nodes
```

When `mgmt-oke --auth instance_principal` starts the Kubernetes client, it
supplies `OCI_CLI_AUTH=instance_principal` to the exec plugin unless that
variable is already set. Direct `kubectl` does not receive that setting from
`mgmt-oke`.

## Kubernetes Unauthorized

Confirm the kubeconfig and OCI exec-plugin arguments:

```bash
kubectl config current-context
kubectl config view --minify \
  -o jsonpath='{.users[0].user.exec.command}{"\n"}{.users[0].user.exec.args}{"\n"}'
kubectl get nodes
```

On an operator node, use instance-principal auth so the CLI can supply the same
method to the kubeconfig OCI CLI subprocess:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list
```

## Cluster or Region Discovery Failed

The selected kubeconfig user must invoke:

```text
oci ce cluster generate-token --cluster-id <cluster-ocid> --region <region>
```

The normal stack operator kubeconfig contains one cluster. The tool uses
`current-context`, or the only unambiguous context when current context is
missing. There is no context-selection environment variable.

For a nonstandard kubeconfig, select a context explicitly for one command:

```bash
mgmt-oke --context <context-name> --auth instance_principal pools list
```

## Compartment Discovery Failed

The compartment is not parsed from kubeconfig. The tool calls OKE `GetCluster`
with the discovered cluster OCID and reads `cluster.compartment_id`.

Validate instance-principal access:

```bash
oci ce cluster get --cluster-id <cluster-ocid> --auth instance_principal
```

Use `--compartment-id` only as an explicit override while diagnosing a
nonstandard environment:

```bash
mgmt-oke --auth instance_principal \
  --compartment-id <compartment-ocid> pools list
```

## OCI Inventory Is Partial

Read-only commands preserve available results and print warnings when one
source fails. Use the partial modes to isolate the failing side:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list \
  --columns name,status,pool,shape --sort pool,name
mgmt-oke --auth instance_principal --skip-kubernetes pools list
```

Example Kubernetes-only output using selected columns:

```text
name           status  pool        shape
-------------  ------  ----------  -------------------
cpu-node-1     Ready   oke-cpu     VM.Standard.E5.Flex
gpu-node-1     Ready   oke-gpu     VM.GPU.A10.1
rdma-node-1    Ready   oke-rdma    BM.GPU4.8
rdma-node-2    Ready   oke-rdma    BM.GPU4.8
system-node-1  Ready   oke-system  VM.Standard.E5.Flex
system-node-2  Ready   oke-system  VM.Standard.E5.Flex
```

Do not treat partial discovery as sufficient mutation preflight.

## Pool Appears Twice

A managed Compute Cluster-backed OKE node pool can have an internal backing
Instance Pool. Current discovery filters that internal resource by Compute
Cluster and instance membership.

If both appear, collect JSON inventory and verify that the OKE identity can read
full node-pool and Instance Pool details:

```bash
mgmt-oke --auth instance_principal --format json pools list
```

Do not resize the apparent standalone Instance Pool until ownership is
resolved.

## Pool or Node Not Found

Refresh inventory and use an accepted identifier:

```bash
mgmt-oke --auth instance_principal pools list
mgmt-oke --auth instance_principal nodes list
```

Pool lookup accepts the displayed pool name or backing resource OCID. Node
lookup accepts Kubernetes name, Slinky name, internal IP, provider ID, or
instance OCID.

Example failed lookup:

```text
Error: Pool not found: pool-does-not-exist
```

The command exits with status `1`.

## Manual Mutation Refused for Autoscaler Ownership

```bash
mgmt-oke --auth instance_principal autoscaler status
```

Example output when no Cluster Autoscaler owns a discovered pool:

```text
(none)
```

Manual resize and node termination are intentionally refused for a matched
autoscaler target. Coordinate the change through Cluster Autoscaler rather than
using `--yes`.

## Node Drain Preflight Is Refused

Inspect the node and its pods:

```bash
mgmt-oke --auth instance_principal nodes get <node-name-or-ip>
kubectl get pods -A --field-selector spec.nodeName=<node-name>
```

Run a dry-run plan to identify the exact refusal:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> --dry-run
```

Example safety refusal:

```text
Error: Refusing to drain cpu-node-1: pods use emptyDir data:
kueue-system/kueue-controller-manager-example,
monitoring/kube-prometheus-stack-grafana-0. Use --delete-emptydir-data to
acknowledge data loss.
```

The command exits with status `2` without changing node or OCI state.

Use `--delete-emptydir-data` only after accepting loss of pod-local data. Use
`--force` only when a pod without a controller can be discarded. A
PodDisruptionBudget warning must be resolved or allowed to clear; the actual
drain keeps retrying eviction until its timeout.

`--no-drain` is intended for nodes drained by an external workflow. It requires
`--allow-workloads` when ordinary workload pods remain.

## Mutation Lease Is Held

Every mutation uses `kube-system/mgmt-oke-mutation` by default. An error naming
another holder means another tool process is changing cluster state or its
Lease has not yet expired.

Inspect the Lease and the other process before retrying:

```bash
kubectl -n kube-system get lease mgmt-oke-mutation -o yaml
```

Do not delete an active Lease. `--no-lock` is available only for a reviewed
recovery when no other mutation is running.

## Slinky Operation Refused

The tool refuses Slinky scale-down, node removal, and replacement because those
operations require Slurm-aware drain. `--allow-workloads` and `--yes` do not
bypass the check. Scale-up remains supported.

## OCI Work Request Failed

With `--wait`, the CLI monitors OKE, Compute, and Compute Management work
requests. For self-managed resources, it snapshots existing resource requests
before mutation so a newly created request is monitored even when the mutation
response omits its identifier. A failed or canceled request stops the wait and
prints the service error details. Resolve that OCI error before submitting
another mutation.

## Resize or Removal Timed Out

A timeout means convergence was not observed before the deadline. It does not
prove that OCI rolled back the request.

Re-run read-only inventory:

```bash
mgmt-oke --auth instance_principal pools get <pool-name>
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
```

Inspect the OCI work request and avoid submitting a duplicate mutation until
the original state is understood. A timeout without an OCI failure generally
means that the request is still running or post-provisioning
Kubernetes/resource readiness did not converge. Use the Cluster Network or
Instance Pool lifecycle state together with `pools get` and `nodes list` as the
authoritative progress view. Repeating the current exact target with `--wait`
is non-mutating and can be used as a convergence barrier.

## Boot Volume Replacement Refused

BVR requires an enhanced OKE cluster. Pool-wide BVR requires a fully healthy
pool, while individual BVR can repair a NotReady worker. The CLI also refuses
autoscaler-owned, Slinky-managed, or unacknowledged system-pool operations.

For an individual worker, do not pass an image update: the OKE API preserves
the current image and node configuration. Apply a compatible image to a
managed pool instead:

```bash
mgmt-oke pools boot-volume-replace <managed-pool> \
  --image-id <replacement-image-ocid> \
  --dry-run
```

The replacement must be a Linux image from the same distribution as the
current image and must support the pool shape in every selected availability
domain. A BVR timeout does not prove rollback. Inspect the OKE work request,
current compute instance boot volume, node Ready state, and pool readiness
before retrying.

## Upgrade Target Is Refused

Refresh the authoritative version view:

```bash
mgmt-oke upgrades status
mgmt-oke upgrades status --to v1.36
```

An exact target must be advertised by OKE. A major/minor target resolves to the
latest advertised production patch. Preview `.0` targets require
`--allow-preview`; downgrades and control-plane jumps of more than one minor are
always refused.

A pool cannot be upgraded ahead of the control plane:

```text
Error: Worker target v1.36.1 cannot be newer than control plane v1.35.2.
```

Use `upgrades apply` to preserve control-plane-first ordering, or complete
`clusters upgrade` before retrying `pools upgrade`.

## Upgrade Workload Gate Is Refused

Inspect the plan evidence:

```bash
mgmt-oke upgrades plan --to v1.36 --format json
mgmt-oke nodes list --pool <pool-name> --workloads
```

Every target node must be Ready, externally cordoned, and free of ordinary
workload pods. Matching Kueue ClusterQueues must be `Hold` or `HoldAndDrain`
with no admitted workloads. Slinky partitions and nodes must be safe and no
active job can reference the target.

Prepare workloads outside `mgmt-oke`, then rerun the plan. The upgrade
subsystem does not cordon, drain, evict, uncordon, change Kueue policy, or
change Slurm state.

`--emergency-ack-unverified-drain` applies only when API, RBAC, or Slinky exec
verification is unavailable. It cannot bypass any positively detected pod,
Kueue workload, Slurm job, schedulable node, or Ready-state failure.

## Upgrade Is Blocked By An Add-On

Run:

```bash
mgmt-oke addons status
mgmt-oke upgrades status --to v1.36 --format json
```

A pinned add-on blocks when its selected version has no build supported for the
target Kubernetes version. Select and apply a compatible add-on version through
OKE before retrying. Automatic add-ons are managed by OKE and verified after
control-plane convergence. `mgmt-oke` does not modify add-ons.

## Slinky Upgrade Verification Is Unavailable

Slinky verification requires one discoverable `slurmctld` container and
read-only pod exec RBAC. The tool reads partitions, nodes, and jobs with
`scontrol`; it never changes Slurm state.

Resolve multiple-controller ambiguity, pod readiness, or RBAC before retrying.
Use the emergency acknowledgement only when the operator independently
verified drain and the failure is strictly an inability to observe it. Active
Slurm state remains non-bypassable.

## Upgrade Checkpoint Needs Recovery

Inspect checkpoint and Lease state:

```bash
kubectl -n kube-system get configmap mgmt-oke-kubernetes-upgrade -o yaml
kubectl -n kube-system get lease mgmt-oke-mutation -o yaml
mgmt-oke upgrades resume --ack-workloads-drained
```

Resume re-observes control-plane version, worker versions, work requests, and
created resources before continuing. Do not manually edit the checkpoint.
A `409` checkpoint conflict means another writer changed its
`resourceVersion`; stop concurrent operations and retry from observed state.

Use `upgrades abandon` only to stop orchestration without rollback:

```bash
mgmt-oke upgrades abandon --yes
```

Abandoning does not reverse any completed OCI change. `upgrades cleanup` is
accepted only after a completed operation and deletes only superseded Instance
Configurations tagged as owned by that operation:

```bash
mgmt-oke upgrades cleanup --yes
```

## Upgrade ETag Or Work Request Failed

An ETag failure means the OCI resource changed after planning. Generate a new
plan and review the difference; do not bypass optimistic concurrency.

A failed or canceled work request is recorded in the checkpoint and stops
execution. Correct the OCI service error, confirm the observed resource state,
and run `upgrades resume`. Deterministic retry tokens prevent an ambiguous
create from producing a second parallel resource.

## Blue-Green Requires External Action

Exit status `3` means the target backend reached the requested version and the
source was intentionally retained. Migrate workloads to the reported target,
externally drain the source, explicitly remove or finalize it through its
reviewed lifecycle workflow, and run:

```bash
mgmt-oke upgrades resume --ack-workloads-drained
```

GMC blue-green may also require an explicit usable Compute Cluster or GPU
Memory Fabric. The tool does not infer that an occupied source placement can be
shared.

## GPU or RDMA Readiness Is Incomplete

Run:

```bash
mgmt-oke --auth instance_principal addons status
mgmt-oke --auth instance_principal addons validate --target all
mgmt-oke --auth instance_principal health run
mgmt-oke --auth instance_principal recommendations list
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
mgmt-oke --auth instance_principal topology list --pool <pool-name>
```

Example healthy check output:

```text
check            scope        status  message                                       recommendation
---------------  -----------  ------  --------------------------------------------  --------------
gpu-allocatable  gpu-node-1   PASS    nvidia.com/gpu=1                              -
rdma-topology    rdma-node-1  PASS    Required OCI RDMA topology labels are valid.  -
rdma-topology    rdma-node-2  PASS    Required OCI RDMA topology labels are valid.  -
```

Check Node Feature Discovery, GPU Operator or device plug-in state, NVIDIA
Network Operator state, topology labels, and allocatable resources. See
[Verifying GPU and RDMA Readiness](./verifying-gpu-and-rdma-readiness.md).

## Live Size Reverts After a Stack Apply

The CLI changes live OCI capacity but does not edit Terraform or Resource
Manager inputs. Update the declared pool size before the next apply. Review the
plan carefully if RDMA ownership settings also changed, because switching
between Cluster Network and Compute Cluster models can replace resources.
