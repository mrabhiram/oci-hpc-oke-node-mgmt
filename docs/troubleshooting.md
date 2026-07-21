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
mgmt-oke --auth instance_principal --skip-oci nodes list
mgmt-oke --auth instance_principal --skip-kubernetes pools list
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

## Manual Mutation Refused for Autoscaler Ownership

```bash
mgmt-oke --auth instance_principal autoscaler status
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

Check Node Feature Discovery, GPU Operator or device plug-in state, NVIDIA
Network Operator state, topology labels, and allocatable resources. See
[Verifying GPU and RDMA Readiness](./verifying-gpu-and-rdma-readiness.md).

## Live Size Reverts After a Stack Apply

The CLI changes live OCI capacity but does not edit Terraform or Resource
Manager inputs. Update the declared pool size before the next apply. Review the
plan carefully if RDMA ownership settings also changed, because switching
between Cluster Network and Compute Cluster models can replace resources.
