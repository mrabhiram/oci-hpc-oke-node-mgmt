# OKE HPC Node Management Tool

Management CLI for OCI HPC OKE clusters.

This tool provides inventory, safety visibility, and guarded node-pool
management for OCI HPC OKE clusters:

- discover managed OKE node pools `[Implemented]`
- discover RDMA cluster-network-backed instance pools `[Implemented]`
- discover Kubernetes nodes `[Implemented]`
- join Kubernetes nodes to OCI instances where possible `[Implemented]`
- show GPU allocatable resources `[Implemented]`
- show RDMA topology labels `[Implemented]`
- show Cluster Autoscaler pool ownership `[Implemented]`
- show Kueue resource counts and ResourceFlavor-to-pool matches `[Implemented]`
- resize managed OKE node pools with explicit confirmation `[Implemented]`
- remove a specific managed OKE node with explicit confirmation `[Implemented]`

Discovery commands are read-only. `pools resize` and `nodes remove` mutate OCI resources and require OCI auth plus either `--yes` or an interactive confirmation.

## Install

For controller/operator node installation, use
[`docs/controller-install.md`](docs/controller-install.md).

From the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
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
| `pools resize <pool> (--size <n> \| --delta <n>)` | Resize one managed OKE node pool. |
| `nodes list` | List Kubernetes nodes. |
| `nodes get <identifier...>` | Get nodes by name, internal IP, provider ID, or instance OCID. |
| `nodes remove <node>` | Remove one specific managed OKE node. |
| `topology list` | Group nodes by RDMA topology labels. |
| `autoscaler status` | Show Cluster Autoscaler pool ownership. |
| `reconcile` | Show a full discovery snapshot. |

Resize options:

| Option | Description |
| --- | --- |
| `--size <n>` | Set the node pool to an exact size. |
| `--delta <n>` | Change the current desired size by `n`. Positive values add nodes; negative values remove nodes. For example, `--delta 2` adds two nodes and `--delta -1` removes one node. |
| `--wait` | Wait until OCI and Kubernetes show the target size. |
| `--timeout <seconds>` | Maximum seconds to wait. Default: `1800`. |
| `--poll-interval <seconds>` | Wait polling interval. Default: `30`. |
| `--yes` | Do not prompt for confirmation. |

Node removal options:

| Option | Description |
| --- | --- |
| `--keep-size` | Delete the node but keep the pool size so OKE can replace it. |
| `--allow-workloads` | Allow removing a node that currently has non-system workload pods. |
| `--eviction-grace <duration>` | OKE eviction grace duration. Default: `PT10M`. |
| `--force-after-grace` | Force compute deletion if pods cannot be evicted before the grace duration expires. |
| `--wait` | Wait until the node is absent and counts settle. |
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

### Nodes

```bash
mgmt-oke nodes list
mgmt-oke nodes list --pool oke-rdma
mgmt-oke nodes list --rdma-only
mgmt-oke nodes get 10.0.127.32
mgmt-oke nodes get ocid1.instance.oc1.iad.example
mgmt-oke nodes remove 10.0.127.32 --wait --yes
mgmt-oke nodes remove 10.0.127.32 --keep-size --wait --yes
```

### RDMA topology

```bash
mgmt-oke topology list
mgmt-oke topology list --pool oke-rdma
```

### Cluster Autoscaler

```bash
mgmt-oke autoscaler status
```

## Output Formats

All commands support:

```bash
--format table
--format json
--format csv
```

`table` is the default.

## Current Scope

Implemented:

- read-only discovery
- OCI/Kubernetes node join
- inferred Kubernetes-only pools when OCI is disabled or unavailable
- pool, node, topology, autoscaler, and reconcile views
- JSON/CSV/table output
- graceful warnings when one discovery source is unavailable
- stdlib unit tests for model/provider-ID parsing
- guarded managed OKE node pool resize through `node_config_details.size`
- wait for OCI active count and Kubernetes Ready count after resize
- guarded specific managed OKE node removal through OKE `delete_node`

Documentation:

- [`docs/controller-install.md`](docs/controller-install.md)

Not implemented yet:

- RDMA cluster-network-backed instance pool resize
- explicit Kubernetes cordon/drain workflow outside OKE delete-node eviction
- node termination
- boot volume replacement wrapper
- Kueue quota sync
- Cluster Autoscaler bounds updates
- health check execution

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

4. Run full OCI + Kubernetes discovery:

   ```bash
   mgmt-oke --auth instance_principal \
     --region <region> \
     --compartment-id <compartment_ocid> \
     --cluster-id <oke_cluster_ocid> \
     reconcile
   ```
