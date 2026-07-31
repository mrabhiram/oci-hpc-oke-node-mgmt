# Controller Node Installation

This guide installs the OKE HPC node management tool on an OKE HPC controller,
operator, or bastion-style admin node where `kubectl` is already configured for
the target cluster.

For the complete guide index, see [`README.md`](README.md). For implementation
details, see [`architecture.md`](architecture.md).

The tool provides two entrypoints backed by the same code:

```bash
mgmt-oke ...
kubectl oke ...
```

Use `mgmt-oke` for direct CLI use. Use `kubectl oke` when the `kubectl-oke`
entrypoint is on `PATH`.

## Prerequisites

The controller/operator node must have:

- Python 3.9 or newer
- OCI Python SDK 2.181.1 or newer; package installation installs this dependency
- `kubectl` configured for the target OKE cluster
- OCI CLI on `PATH` and able to use instance principal auth
- IAM policy allowing the instance principal to read the target OKE cluster,
  inspect its node pools and add-ons, inspect backing compute resources, and
  inspect work requests used by `--wait`
- additional IAM permissions to create, update, or delete OKE node pools,
  Compute Clusters, Instance Configurations, Cluster Networks, and Instance
  Pools when lifecycle commands will be used
- OKE node-pool resource-principal permission
  `COMPUTE_CLUSTER_LAUNCH_INSTANCE` for managed Compute Cluster RDMA creation,
  plus `HOST_GROUP_LAUNCH_INSTANCE` when a Compute Host Group is selected
- permission to read OKE available Kubernetes versions, virtual node pools, and
  add-on options when upgrade status or planning will be used
- permission to update the OKE cluster and node pools; create and update
  Instance Configurations; update Cluster Networks, Instance Pools, instances,
  and GPU Memory Clusters; and inspect the resulting work requests when
  upgrade execution will be used
- permission to invoke OKE cluster-node boot volume replacement and read
  compute instance, image, boot volume, and work-request state when BVR commands
  will be used
- Kubernetes RBAC to read nodes, pods, Kueue resources, and Slinky controller
  pods, plus read-only pod exec for Slinky upgrade verification
- Kubernetes RBAC to create, get, update, and delete the
  `kube-system/mgmt-oke-kubernetes-upgrade` ConfigMap and operate the existing
  `kube-system/mgmt-oke-mutation` Lease when upgrades will be executed
- an enhanced OKE cluster for boot volume replacement
- an enhanced OKE cluster for managed Compute Cluster RDMA pools
- Network access to the Kubernetes API and OCI regional APIs

Validate the controller environment before installing:

```bash
python3 --version
kubectl get nodes -o wide
oci iam region list --auth instance_principal
```

## Get The Source

Clone the public repository on the controller/operator node:

```bash
cd /home/ubuntu
git clone https://github.com/mrabhiram/oci-hpc-oke-node-mgmt.git
```

## Install

On the controller/operator node:

```bash
cd /home/ubuntu/oci-hpc-oke-node-mgmt
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install .
```

Verify the package entrypoints:

```bash
.venv/bin/mgmt-oke --version
.venv/bin/mgmt-oke --help
.venv/bin/kubectl-oke --help
```

Example version output:

```text
mgmt-oke, version 0.10.0
```

## Upgrade An Existing Installation

Update the checkout and reinstall the package in its existing virtual
environment:

```bash
cd /home/ubuntu/oci-hpc-oke-node-mgmt
git pull --ff-only
.venv/bin/python -m pip install --upgrade --force-reinstall .
```

Reinstalling refreshes both `mgmt-oke` and `kubectl-oke` entrypoints.

## Put The Commands On PATH

Create stable command links in `~/bin`:

```bash
mkdir -p /home/ubuntu/bin
ln -sf /home/ubuntu/oci-hpc-oke-node-mgmt/.venv/bin/mgmt-oke /home/ubuntu/bin/mgmt-oke
ln -sf /home/ubuntu/oci-hpc-oke-node-mgmt/.venv/bin/kubectl-oke /home/ubuntu/bin/kubectl-oke
```

Make sure `~/bin` is on the shell path:

```bash
export PATH=/home/ubuntu/bin:$PATH
```

To persist it for future SSH sessions:

```bash
printf '\nexport PATH=/home/ubuntu/bin:$PATH\n' >> /home/ubuntu/.bashrc
```

For cron, remote one-liners, and other non-interactive shells, set a complete
path explicitly. OCI-generated kubeconfig also needs the OCI CLI authentication
method when `kubectl` is invoked directly:

```bash
export PATH=/home/ubuntu/bin:/usr/local/bin:/usr/bin:/bin
export OCI_CLI_AUTH=instance_principal
```

Validate both command shapes:

```bash
mgmt-oke --help
kubectl oke --help
```

## Configure Defaults

For the current shell, select instance principal authentication:

```bash
export OCI_AUTH=instance_principal
```

With an OCI-generated kubeconfig selected, the tool automatically reads the OKE
cluster OCID and region from kubeconfig and obtains the compartment OCID from
the OCI OKE `GetCluster` API. No cluster-specific OCIDs need to be added to the
shell profile. The tool also maps `OCI_AUTH=instance_principal` to a process-local
`OCI_CLI_AUTH=instance_principal` for the kubeconfig OCI CLI exec plugin unless
`OCI_CLI_AUTH` is already set explicitly.

The automatic sequence is:

```text
kubeconfig context -> cluster OCID and region -> OKE GetCluster -> compartment OCID
```

Explicit command-line values take precedence over environment variables, which
take precedence over automatic discovery. The optional overrides below are
therefore intended for in-cluster execution or other nonstandard environments
rather than normal stack operator nodes.

The following optional overrides are available for nonstandard environments:

```bash
export OCI_REGION=<region>
export OCI_COMPARTMENT_ID=ocid1.compartment.oc1..example
export OKE_CLUSTER_ID=ocid1.cluster.oc1.iad.example
```

If the authentication default should be available for every login, add
`OCI_AUTH=instance_principal` to `/home/ubuntu/.bashrc` or the controller node's
standard profile file.

## Validate The Installation

Run non-mutating checks:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list
mgmt-oke pools list
mgmt-oke nodes list
mgmt-oke topology list
mgmt-oke autoscaler status
mgmt-oke addons status
mgmt-oke status
mgmt-oke health run
mgmt-oke addons validate --target all
mgmt-oke reconcile
mgmt-oke upgrades status
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades plan --to v1.36
mgmt-oke upgrades apply --to v1.36 --dry-run
```

Example `status` result after the validation sequence:

```text
overall  pools  nodes  ready  not_ready  gpu_nodes  rdma_nodes  addons_active  addons_total  autoscaler_pools  slinky_nodes  kueue_flavors
-------  -----  -----  -----  ---------  ---------  ----------  -------------  ------------  ----------------  ------------  -------------
HEALTHY  4      6      6      0          3          2           7              7             0                 0             2
```

The values are deployment-specific. A successful installation should report
authoritative OCI pools, corresponding Kubernetes workers, and no discovery
warnings before mutation commands are used.

`pools list` should classify each OCI resource by both ownership and placement.
Common results are:

| Pool type | Expected fields |
| --- | --- |
| Standard managed OKE pool | `kind=node-pool`, `placement=standard` |
| OKE v26.7 managed RDMA pool | `kind=node-pool`, `placement=compute-cluster` |
| Managed Host Group pool | `kind=node-pool`, `placement=host-group` |
| Legacy self-managed RDMA pool | `kind=cluster-network`, `placement=cluster-network` |

No `--cluster-id`, `--region`, or `--compartment-id` option is required for
these checks when the operator kubeconfig identifies one OKE cluster.

## Mutating Commands

The current mutating commands are:

```bash
mgmt-oke pools create <name> --type <cpu|gpu|rdma> --count <n> [--rdma-mode <mode>] [--from-pool <pool>] [--bootstrap-from-pool <legacy-rdma-pool>] [--dry-run] [--wait]
mgmt-oke pools delete <pool> [--dry-run] [--wait]
mgmt-oke pools resize <pool> (--size <n> | --delta <n>) [--dry-run] [--wait]
mgmt-oke pools add <pool> --count <n> [--dry-run] [--wait]
mgmt-oke pools remove <pool> --count <n> [--dry-run] [--wait]
mgmt-oke pools boot-volume-replace <managed-pool> <property-update> [--dry-run] [--wait]
mgmt-oke nodes cordon <node...> [--dry-run]
mgmt-oke nodes drain <node...> [--dry-run]
mgmt-oke nodes uncordon <node...> [--dry-run]
mgmt-oke nodes terminate <node...> [--tag <unhealthy|none>] [--keep-size] [--dry-run] [--wait]
mgmt-oke nodes boot-volume-replace <node...> [--dry-run] [--wait]
mgmt-oke clusters upgrade --to <version> [--dry-run]
mgmt-oke pools upgrade <pool> --to <version> --strategy <strategy> [--dry-run]
mgmt-oke upgrades apply --to <version> [--dry-run]
mgmt-oke upgrades resume [--ack-workloads-drained]
mgmt-oke upgrades abandon [--yes]
mgmt-oke upgrades cleanup [--yes]
```

Safety behavior:

- Discovery commands are read-only.
- Pool mutations, node termination, and BVR require OCI auth.
- Every mutation supports a validated `--dry-run` plan.
- Every mutation requires either `--yes` or an interactive typed confirmation.
- Mutations acquire a Kubernetes Lease by default to prevent concurrent tool operations.
- Pool creation inherits cluster bootstrap from a matching source and validates
  custom image, shape, placement, and network compatibility before submission.
- Managed Compute Cluster creation can explicitly inherit legacy RDMA
  cloud-init while preserving current managed OKE identity and network fields.
- Whole-pool deletion drains by default and protects `oke-system`.
- Waited deletion removes only derived Instance Configurations carrying the
  tool's ownership tag; stack-owned configurations are preserved.
- The tool refuses to resize or remove nodes from Cluster Autoscaler-owned pools
  by default.
- Node termination cordons and drains through the Kubernetes Eviction API by
  default. PodDisruptionBudgets remain authoritative.
- Drain requires explicit acknowledgement for `emptyDir` data or pods without
  a controller.
- Boot volume replacement requires an enhanced cluster, eviction preflight,
  and explicit acknowledgement for `emptyDir` data or pods without a
  controller. Pool-wide BVR also requires a fully healthy pool; individual BVR
  can repair a NotReady worker.
- Individual BVR preserves the selected compute instance, network address,
  image, and current node configuration. Managed-pool BVR is the supported path
  for applying a new image to existing workers.
- Compute Cluster-backed OKE pools are resized and modified through OKE APIs;
  their internal backing instance pools are not mutation targets.
- Legacy Cluster Network pools are resized with `UpdateClusterNetwork`; their
  existing embedded Instance Configuration remains unchanged.
- Specific nodes in managed pools are removed with OKE `DeleteNode`. Specific
  nodes in legacy Cluster Network or standalone Instance Pools are detached
  with automatic termination.
- Node termination can merge and verify
  `ComputeInstanceHostActions.CustomerReportedHostStatus=unhealthy` before the
  destructive OCI call. If `--tag` is omitted, the CLI asks for each node;
  `--tag none` is the explicit noninteractive opt-out.
- Slinky-managed pools refuse node removal, replacement, and pool scale-down
  until a Slurm-aware drain workflow is available. Scale-up remains supported.
- When the NVIDIA Network Operator add-on is active, RDMA convergence also
  requires allocatable `nvidia.com/rdma-vf` resources.
- Upgrade status, planning, and `--dry-run` are read-only. Upgrade execution
  always waits for OCI work requests and observed Kubernetes convergence.
- The upgrade subsystem never cordons, drains, evicts, or uncordons a worker.
  Operators prepare workloads externally, then attest that preparation
  separately from `--yes`.
- `--emergency-ack-unverified-drain` is accepted only for API, RBAC, or exec
  verification failures. Positively detected pods, Kueue workloads, or Slurm
  jobs remain blocking.
- Full-cluster upgrades store one non-secret, resource-version-protected
  checkpoint ConfigMap and can resume from observed OCI and Kubernetes state.
- Upgrade commands query OKE add-on compatibility but never install, update, or
  remove add-ons.

See [`replacing-worker-boot-volumes.md`](replacing-worker-boot-volumes.md) for
BVR IAM, image, data-loss, and operational requirements.
See [`kubernetes-upgrades.md`](kubernetes-upgrades.md) for upgrade IAM, strategy,
workload-gate, execution, and recovery requirements.

Example managed or self-managed pool resize:

```bash
mgmt-oke pools add oke-cpu --count 1 --dry-run
mgmt-oke pools add oke-cpu --count 1 --wait
```

Example managed CPU, managed GPU, managed RDMA, and legacy RDMA creation previews:

```bash
mgmt-oke pools create cpu-batch \
  --type cpu \
  --count 2 \
  --from-pool oke-cpu \
  --dry-run
mgmt-oke pools create gpu-training \
  --type gpu \
  --count 2 \
  --from-pool oke-gpu \
  --image-id <custom-image-ocid> \
  --dry-run
mgmt-oke pools create rdma-managed \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool oke-gpu \
  --bootstrap-from-pool oke-rdma \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --dry-run
mgmt-oke pools create rdma-training \
  --type rdma \
  --count 2 \
  --from-pool oke-rdma \
  --dry-run
```

CPU, GPU, and compute-cluster RDMA submit OKE `CreateNodePool`; managed RDMA
first validates or creates its Compute Cluster. Legacy RDMA derives a new
Instance Configuration and creates a Cluster Network with an embedded Instance
Pool. In the managed RDMA example, `oke-gpu` supplies OKE-owned pool settings
and `oke-rdma` supplies legacy cloud-init and storage bootstrap.
See [`creating-worker-pools.md`](creating-worker-pools.md) and
[`worker-bootstrap-and-storage.md`](worker-bootstrap-and-storage.md).
Sanitized live lifecycle output is available in
[`live-pool-creation-validation.md`](live-pool-creation-validation.md) and
[`live-pool-deletion-validation.md`](live-pool-deletion-validation.md).

A negative delta reduces capacity but does not select the departing worker. Use
`nodes terminate` when a particular worker must be removed.

Example specific node replacement while keeping pool size:

```bash
mgmt-oke nodes terminate <node-name-or-ip> \
  --tag unhealthy --keep-size --dry-run
mgmt-oke nodes terminate <node-name-or-ip> \
  --tag unhealthy --keep-size --wait
```

Use `--yes` only for non-interactive operations where the target pool or node
has already been selected intentionally. Node termination automation must also
specify `--tag unhealthy` or `--tag none`; `--yes` does not answer that
separate health question.

## Shell Completion

Click can generate completion scripts from the installed command. For Bash:

```bash
_MGMT_OKE_COMPLETE=bash_source mgmt-oke > /home/ubuntu/.mgmt-oke-complete.bash
printf '\n. /home/ubuntu/.mgmt-oke-complete.bash\n' >> /home/ubuntu/.bashrc
. /home/ubuntu/.mgmt-oke-complete.bash
```

For a one-shell activation without writing a completion file:

```bash
eval "$(_MGMT_OKE_COMPLETE=bash_source mgmt-oke)"
```

## Troubleshooting

If `mgmt-oke` is not found:

```bash
echo "$PATH"
ls -l /home/ubuntu/bin/mgmt-oke
/home/ubuntu/oci-hpc-oke-node-mgmt/.venv/bin/mgmt-oke --help
```

If `kubectl oke` is not found, confirm `kubectl-oke` is on `PATH`:

```bash
which kubectl-oke
kubectl oke --help
```

If OCI auth fails:

```bash
oci iam region list --auth instance_principal
echo "$OCI_AUTH"
echo "$OCI_CLI_AUTH"
echo "$OCI_REGION"
```

If automatic OKE target discovery fails, inspect the selected context and its
OCI CLI exec arguments:

```bash
command -v oci
kubectl config current-context
kubectl config view --minify -o jsonpath='{.users[0].user.exec.command}{"\n"}{.users[0].user.exec.args}{"\n"}'
```

An OCI-generated OKE kubeconfig should invoke `oci ce cluster generate-token`
with `--cluster-id` and `--region`, and that `oci` executable must be on `PATH`.
For a nonstandard kubeconfig, select its context explicitly with `--context`, or
provide `--cluster-id`, `--region`, and `--compartment-id` as explicit
overrides.

If cluster discovery succeeds but compartment discovery fails, verify that the
selected OCI identity can read the OKE cluster. Compartment discovery uses OKE
`GetCluster` and does not read a compartment value from kubeconfig.

If Kubernetes discovery fails:

```bash
kubectl config current-context
kubectl get nodes
mgmt-oke --auth instance_principal --skip-oci nodes list
```

If table output is not convenient for automation, use JSON or CSV:

```bash
mgmt-oke reconcile --format json
mgmt-oke nodes list --format csv
```
