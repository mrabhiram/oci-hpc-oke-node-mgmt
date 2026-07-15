# Controller Node Installation

This guide installs the OKE HPC node management tool on an OKE HPC controller,
operator, or bastion-style admin node where `kubectl` is already configured for
the target cluster.

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
- OCI CLI or OCI SDK dependencies able to use instance principal auth
- IAM policy allowing the instance principal to inspect and manage the target
  OKE node pools and backing compute resources
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

Validate both command shapes:

```bash
mgmt-oke --help
kubectl oke --help
```

## Configure Defaults

For the current shell, set the cluster defaults:

```bash
export OCI_AUTH=instance_principal
export OCI_REGION=<region>
export OCI_COMPARTMENT_ID=<compartment_ocid>
export OKE_CLUSTER_ID=<cluster_ocid>
```

Example:

```bash
export OCI_AUTH=instance_principal
export OCI_REGION=us-ashburn-1
export OCI_COMPARTMENT_ID=ocid1.compartment.oc1..example
export OKE_CLUSTER_ID=ocid1.cluster.oc1.iad.example
```

If these values should be available for every login, add them to
`/home/ubuntu/.bashrc` or the controller node's standard profile file.

## Validate The Installation

Run non-mutating checks:

```bash
mgmt-oke --auth none nodes list
mgmt-oke pools list
mgmt-oke nodes list
mgmt-oke topology list
mgmt-oke autoscaler status
mgmt-oke addons status
mgmt-oke reconcile
```

## Mutating Commands

The current mutating commands are:

```bash
mgmt-oke pools resize <pool> (--size <n> | --delta <n>) [--wait] [--yes]
mgmt-oke nodes remove <node> [--keep-size] [--wait] [--yes]
```

Safety behavior:

- Discovery commands are read-only.
- Resize and remove require OCI auth.
- Resize and remove require either `--yes` or an interactive confirmation.
- The tool refuses to resize or remove nodes from Cluster Autoscaler-owned pools
  by default.
- `nodes remove` refuses nodes with non-system workload pods unless
  `--allow-workloads` is provided.
- Compute Cluster-backed OKE pools are resized and modified through OKE APIs;
  their internal backing instance pools are not mutation targets.
- Slinky-managed pools refuse node removal, replacement, and pool scale-down
  until a Slurm-aware drain workflow is available. Scale-up remains supported.
- When the NVIDIA Network Operator add-on is active, RDMA convergence also
  requires allocatable `nvidia.com/rdma-vf` resources.

Example managed or self-managed pool resize:

```bash
mgmt-oke pools resize oke-cpu --delta 1 --wait
```

Example specific node replacement while keeping pool size:

```bash
mgmt-oke nodes remove <node-name-or-ip> --keep-size --wait
```

Use `--yes` only for non-interactive operations where the target pool or node
has already been selected intentionally.

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
echo "$OCI_REGION"
```

If Kubernetes discovery fails:

```bash
kubectl config current-context
kubectl get nodes
mgmt-oke --auth none nodes list
```

If table output is not convenient for automation, use JSON or CSV:

```bash
mgmt-oke reconcile --format json
mgmt-oke nodes list --format csv
```
