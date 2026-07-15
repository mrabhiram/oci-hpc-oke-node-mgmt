# OKE HPC Node Management Tool

Management CLI for OCI HPC OKE clusters.

This tool provides inventory, safety visibility, and guarded node-pool
management for OCI HPC OKE clusters:

- discover managed OKE node pools, including Compute Cluster placement `[Implemented]`
- discover RDMA cluster-network-backed instance pools `[Implemented]`
- suppress OKE-internal backing instance pools from standalone pool inventory `[Implemented]`
- discover Kubernetes nodes `[Implemented]`
- join Kubernetes nodes to OCI instances where possible `[Implemented]`
- show GPU allocatable resources `[Implemented]`
- validate OCI RDMA topology labels and reject missing or sentinel values `[Implemented]`
- show OKE add-on lifecycle state and installed versions `[Implemented]`
- require `nvidia.com/rdma-vf` readiness when the NVIDIA Network Operator add-on is active `[Implemented]`
- show Cluster Autoscaler pool ownership `[Implemented]`
- show Kueue resource counts and ResourceFlavor-to-pool matches `[Implemented]`
- discover Slinky Slurm node aliases and protect Slinky workers from unsafe removal `[Implemented]`
- resize standard and Compute Cluster-backed OKE node pools and self-managed cluster-network pools `[Implemented]`
- remove or replace a specific managed or self-managed worker node `[Implemented]`
- wait for OCI, Kubernetes, GPU, RDMA topology, and applicable RDMA VF readiness `[Implemented]`

Discovery commands are read-only. `pools resize` and `nodes remove` mutate OCI resources and require OCI auth plus either `--yes` or an interactive confirmation.

## Install

For controller/operator node installation, use
[`docs/controller-install.md`](docs/controller-install.md).

From the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install .
```

Python 3.9 or newer is supported.

This installs two entrypoints backed by the same code:

```bash
mgmt-oke --help
kubectl-oke --help
```

For kubectl plugin usage, make sure `kubectl-oke` is on `PATH`, then run:

```bash
kubectl oke --help
```

## Usage

The tool can be invoked directly as `mgmt-oke` or as a kubectl plugin:

```bash
mgmt-oke [global options] <command> [command options]
kubectl oke [global options] <command> [command options]
```

Display help:

```bash
mgmt-oke -h
mgmt-oke pools -h
mgmt-oke nodes -h
kubectl oke -h
```

Global options:

| Option | Description |
| --- | --- |
| `--version` | Print the tool version. |
| `--compartment-id <ocid>` | OCI compartment OCID used for OCI discovery and mutations. |
| `--cluster-id <ocid>` | OKE cluster OCID used to filter managed node pools. |
| `--region <region>` | OCI region, for example `us-ashburn-1`. |
| `--auth config_file\|instance_principal\|resource_principal\|none` | OCI authentication method. Use `none` for Kubernetes-only discovery. |
| `--oci-config-file <path>` | OCI config file path when using config-file authentication. |
| `--oci-profile <profile>` | OCI config profile when using config-file authentication. |
| `--kubeconfig <path>` | kubeconfig path. |
| `--context <name>` | kubeconfig context. |
| `--in-cluster` | Use Kubernetes in-cluster configuration. |
| `--skip-oci` | Skip OCI discovery. |
| `--skip-kubernetes` | Skip Kubernetes discovery. |
| `--format table\|json\|csv` | Output format. `table` is the default. |

Command groups:

| Command | Description |
| --- | --- |
| `pools list` | List discovered worker pools. |
| `pools get <pool>` | Get one worker pool by name or OCID. |
| `pools resize <pool> (--size <n> \| --delta <n>)` | Resize a managed OKE node pool, cluster network, or instance pool. |
| `nodes list` | List Kubernetes nodes. |
| `nodes get <identifier...>` | Get nodes by Kubernetes name, Slurm name, internal IP, provider ID, or instance OCID. |
| `nodes remove <node>` | Remove or replace one specific managed or self-managed worker node. |
| `topology list` | Group nodes by RDMA topology labels. |
| `autoscaler status` | Show Cluster Autoscaler pool ownership. |
| `addons status` | Show OKE add-on lifecycle state and installed versions. |
| `reconcile` | Show a full discovery snapshot. |

Resize options:

| Option | Description |
| --- | --- |
| `--size <n>` | Set the worker pool to an exact desired size. |
| `--delta <n>` | Change the current desired size by `n`. Positive values add nodes; negative values remove nodes. For example, `--delta 2` adds two nodes and `--delta -1` removes one node. |
| `--wait` | Wait for target OCI and Kubernetes counts. GPU/RDMA pools also wait for allocatable GPUs and valid RDMA topology; when `NvidiaNetworkOperator` is active, RDMA pools also wait for `nvidia.com/rdma-vf`. |
| `--timeout <seconds>` | Maximum seconds to wait. Default: `1800`. |
| `--poll-interval <seconds>` | Wait polling interval. Default: `30`. |
| `--yes` | Do not prompt for confirmation. |

Node removal options:

| Option | Description |
| --- | --- |
| `--keep-size` | Delete the node but keep the pool size so the backing pool replaces it. |
| `--allow-workloads` | Allow removing a node that currently has non-system workload pods. |
| `--eviction-grace <duration>` | Managed OKE node eviction grace duration. Default: `PT10M`. |
| `--force-after-grace` | For managed OKE pools, force compute deletion if pods cannot be evicted before the grace duration expires. |
| `--wait` | Wait until the selected node is absent and the pool has converged, including applicable GPU, RDMA topology, and RDMA VF readiness. |
| `--timeout <seconds>` | Maximum seconds to wait. Default: `1800`. |
| `--poll-interval <seconds>` | Wait polling interval. Default: `30`. |
| `--yes` | Do not prompt for confirmation. |

## Authentication

The tool has two discovery sources:

- Kubernetes API, using kubeconfig or in-cluster config
- OCI API, using config-file auth, instance principals, or resource principals

On an OKE HPC operator host, the normal command will look like:

```bash
mgmt-oke \
  --auth instance_principal \
  --region us-ashburn-1 \
  --compartment-id ocid1.compartment.oc1..example \
  --cluster-id ocid1.cluster.oc1.iad.example \
  pools list
```

For Kubernetes-only discovery:

```bash
mgmt-oke --auth none nodes list
```

Useful environment variables:

```bash
export OCI_COMPARTMENT_ID=ocid1.compartment.oc1..example
export OKE_CLUSTER_ID=ocid1.cluster.oc1.iad.example
export OCI_REGION=us-ashburn-1
export OCI_AUTH=instance_principal
export KUBECONFIG=$HOME/.kube/config
```

## Commands

### Full snapshot

`reconcile` is read-only. It combines worker-pool and Kubernetes node inventory,
OKE add-on status, Cluster Autoscaler ownership, GPU/RDMA capability status,
Slinky aliases, and Kueue resource counts in one snapshot.

```bash
mgmt-oke reconcile
mgmt-oke --format json reconcile
```

### Worker pools

```bash
mgmt-oke pools list
mgmt-oke pools get oke-rdma
mgmt-oke --format json pools list
```

OCI HPC OKE v26.7 supports two RDMA worker ownership models:

| Pool model | Inventory | Resize operation | Specific node removal |
| --- | --- | --- | --- |
| Managed OKE node pool placed in a Compute Cluster | `kind=node-pool`, `placement=compute-cluster` | OKE `UpdateNodePool` with only the desired size | OKE `DeleteNode` |
| Self-managed Cluster Network instance pool | `kind=cluster-network`, `placement=cluster-network` | Compute Management `UpdateClusterNetwork` | Instance-pool detach with automatic termination |

The Compute Cluster is placement metadata for a managed OKE pool. The tool does
not resize or detach its OKE-internal backing instance pool directly.

Add one node to a pool:

```bash
mgmt-oke pools resize oke-cpu --delta 1 --wait --yes
```

Remove one node from a pool:

```bash
mgmt-oke pools resize oke-cpu --delta -1 --wait --yes
```

Set a pool to an exact desired size:

```bash
mgmt-oke pools resize oke-cpu --size 3 --wait --yes
```

The same resize command applies to managed Compute Cluster-backed RDMA pools and
legacy self-managed RDMA Cluster Network pools. Discovery selects the correct
OCI API from the pool ownership metadata:

```bash
mgmt-oke pools resize oke-rdma --delta 1 --wait --yes
```

### Nodes

```bash
mgmt-oke nodes list
mgmt-oke nodes list --pool oke-rdma
mgmt-oke nodes list --rdma-only
mgmt-oke nodes get 10.0.127.32
mgmt-oke nodes get ocid1.instance.oc1.iad.example
mgmt-oke nodes get <slurm-node-name>
mgmt-oke nodes remove 10.0.127.32 --wait --yes
mgmt-oke nodes remove 10.0.127.32 --keep-size --wait --yes
```

Replace a specific RDMA worker while keeping the pool at its current size:

```bash
mgmt-oke nodes remove <rdma-node-name> --keep-size --wait --yes
```

For Slinky-managed pools, node removal, replacement, and pool scale-down are
refused because they require a Slurm-aware drain. Pool scale-up remains
available. `--allow-workloads` does not bypass this protection.

### RDMA topology

```bash
mgmt-oke topology list
mgmt-oke topology list --pool oke-rdma
```

### Cluster Autoscaler

```bash
mgmt-oke autoscaler status
```

### OKE add-ons

```bash
mgmt-oke addons status
mgmt-oke --format json addons status
```

The add-on view is read-only. When `NvidiaNetworkOperator` is active, readiness
checks for RDMA pools include the `nvidia.com/rdma-vf` allocatable resource in
addition to GPU and OCI RDMA topology readiness.

## Resource Ownership

Pool resize and node removal update live OCI resources. If those resources are
also managed by Terraform or OCI Resource Manager, update the corresponding
stack variables before a later apply so the declared size does not replace the
live value.

## Output Formats

All commands support:

```bash
--format table
--format json
--format csv
```

`table` is the default.

## Tests

After installing the package, run the unit-test suite from the project root:

```bash
python -m unittest discover -s tests -v
```

## Current Scope

See [`docs/scope.md`](docs/scope.md) for implemented features and planned
items.

## Cluster Validation

After installing the tool on a controller/operator node:

1. Confirm kubeconfig:

   ```bash
   kubectl get nodes -o wide
   ```

2. Confirm OCI auth:

   ```bash
   oci iam region list --auth instance_principal
   ```

3. Run Kubernetes-only discovery:

   ```bash
   mgmt-oke --auth none reconcile
   ```

4. Confirm OKE add-on status:

   ```bash
   mgmt-oke addons status
   ```

5. Run full OCI + Kubernetes discovery:

   ```bash
   mgmt-oke --auth instance_principal \
     --region <region> \
     --compartment-id <compartment_ocid> \
     --cluster-id <oke_cluster_ocid> \
     reconcile
   ```
