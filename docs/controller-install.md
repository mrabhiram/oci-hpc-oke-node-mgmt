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
- additional IAM permissions to manage node pools, Cluster Networks, or
  Instance Pools when resize or node-removal commands will be used
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
.venv/bin/mgmt-oke --help
.venv/bin/kubectl-oke --help
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
```

`pools list` should classify each OCI resource by both ownership and placement.
Common results are:

| Pool type | Expected fields |
| --- | --- |
| Standard managed OKE pool | `kind=node-pool`, `placement=standard` |
| OKE v26.7 managed RDMA pool | `kind=node-pool`, `placement=compute-cluster` |
| Legacy self-managed RDMA pool | `kind=cluster-network`, `placement=cluster-network` |

No `--cluster-id`, `--region`, or `--compartment-id` option is required for
these checks when the operator kubeconfig identifies one OKE cluster.

## Mutating Commands

The current mutating commands are:

```bash
mgmt-oke pools resize <pool> (--size <n> | --delta <n>) [--dry-run] [--wait]
mgmt-oke pools add <pool> --count <n> [--dry-run] [--wait]
mgmt-oke pools remove <pool> --count <n> [--dry-run] [--wait]
mgmt-oke nodes cordon <node...> [--dry-run]
mgmt-oke nodes drain <node...> [--dry-run]
mgmt-oke nodes uncordon <node...> [--dry-run]
mgmt-oke nodes terminate <node...> [--keep-size] [--dry-run] [--wait]
```

Safety behavior:

- Discovery commands are read-only.
- Pool mutations and node termination require OCI auth.
- Every mutation supports a validated `--dry-run` plan.
- Every mutation requires either `--yes` or an interactive typed confirmation.
- Mutations acquire a Kubernetes Lease by default to prevent concurrent tool operations.
- The tool refuses to resize or remove nodes from Cluster Autoscaler-owned pools
  by default.
- Node termination cordons and drains through the Kubernetes Eviction API by
  default. PodDisruptionBudgets remain authoritative.
- Drain requires explicit acknowledgement for `emptyDir` data or pods without
  a controller.
- Compute Cluster-backed OKE pools are resized and modified through OKE APIs;
  their internal backing instance pools are not mutation targets.
- Legacy Cluster Network pools are resized with `UpdateClusterNetwork`; their
  existing embedded Instance Configuration remains unchanged.
- Specific nodes in managed pools are removed with OKE `DeleteNode`. Specific
  nodes in legacy Cluster Network or standalone Instance Pools are detached
  with automatic termination.
- Slinky-managed pools refuse node removal, replacement, and pool scale-down
  until a Slurm-aware drain workflow is available. Scale-up remains supported.
- When the NVIDIA Network Operator add-on is active, RDMA convergence also
  requires allocatable `nvidia.com/rdma-vf` resources.

Example managed or self-managed pool resize:

```bash
mgmt-oke pools add oke-cpu --count 1 --dry-run
mgmt-oke pools add oke-cpu --count 1 --wait
```

A negative delta reduces capacity but does not select the departing worker. Use
`nodes terminate` when a particular worker must be removed.

Example specific node replacement while keeping pool size:

```bash
mgmt-oke nodes terminate <node-name-or-ip> --keep-size --dry-run
mgmt-oke nodes terminate <node-name-or-ip> --keep-size --wait
```

Use `--yes` only for non-interactive operations where the target pool or node
has already been selected intentionally.

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
