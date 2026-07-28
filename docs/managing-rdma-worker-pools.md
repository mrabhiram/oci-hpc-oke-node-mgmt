# Managing RDMA Worker Pools

This guide explains how to identify and operate the two RDMA worker-pool models
used by OCI HPC OKE deployments.

## Overview

OCI HPC OKE v26.7 deploys GPU with RDMA workers as managed OKE node pools placed
in OCI Compute Clusters by default. Earlier deployments and deployments using
the legacy option can expose a self-managed Cluster Network with an embedded
Instance Pool.

The same `mgmt-oke` commands work with both models, but the owning OCI API is
different.

## Prerequisites

- an OCI HPC OKE cluster with an RDMA worker pool
- working OCI and Kubernetes discovery
- IAM permission to read and, for mutations, manage the owning pool resource;
  `--wait` also requires permission to inspect its work requests
- GPU and network device plug-ins initialized on the workers

## Identify the RDMA Ownership Model

```bash
mgmt-oke --auth instance_principal pools get oke-rdma
```

Example output for a legacy Cluster Network-backed pool:

```text
name      kind             placement        shape      desired  oci_active  k8s_ready  gpu             rdma  rdma_vf_required  slinky  autoscaler  kueue_flavor
--------  ---------------  ---------------  ---------  -------  ----------  ---------  --------------  ----  ----------------  ------  ----------  ------------
oke-rdma  cluster-network  cluster-network  BM.GPU4.8  2        2           2          nvidia.com/gpu  yes   no                no      -           -
```

Interpret `kind` and `placement`:

| Output | Model |
| --- | --- |
| `kind=node-pool`, `placement=compute-cluster` | Managed OKE RDMA node pool placed in a Compute Cluster. |
| `kind=cluster-network`, `placement=cluster-network` | Legacy self-managed Cluster Network. |

Use JSON to inspect backing identifiers:

```bash
mgmt-oke --auth instance_principal --format json pools get oke-rdma
```

A managed pool exposes `node_pool_id` and `compute_cluster_id`. A legacy pool
exposes `cluster_network_id` and `instance_pool_id`.

## Create A Cluster Network RDMA Pool

Create a second self-managed Cluster Network pool from the existing `oke-rdma`
configuration and placement:

```bash
mgmt-oke --auth instance_principal pools create oke-rdma-2 \
  --count 2 \
  --from-pool oke-rdma \
  --dry-run \
  --format json
```

Apply after reviewing the plan:

```bash
mgmt-oke --auth instance_principal pools create oke-rdma-2 \
  --count 2 \
  --from-pool oke-rdma \
  --wait
```

This derives a new Instance Configuration from the source, preserving its
image, cloud-init, OKE bootstrap metadata, and networking while retargeting
instance tags, VNIC tags, and the initial Kubernetes pool label. It then creates
a new Cluster Network and embedded Instance Pool. It does not create a managed
OKE node pool or alter the source pool.

See
[Creating Cluster Network Worker Pools](./creating-cluster-network-pools.md)
for source selection, safety checks, dry-run output, and infrastructure-as-code
ownership.

## Add an RDMA Worker

```bash
mgmt-oke --auth instance_principal pools add oke-rdma \
  --count 1 --dry-run --format json
```

Example dry-run output:

```json
[
  {
    "current_size": 2,
    "decrement_size": null,
    "operation": "pool-resize",
    "owner": "compute-management",
    "pool": "oke-rdma",
    "status": "planned",
    "steps": ["update the Cluster Network's embedded Instance Pool size"],
    "target": "oke-rdma",
    "target_size": 3,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Apply after reviewing the plan:

```bash
mgmt-oke --auth instance_principal pools add oke-rdma --count 1 --wait
```

For the managed model, OKE increases `node_config_details.size` and places the
new node in the existing Compute Cluster. The tool does not update the Compute
Cluster or its internal backing Instance Pool.

For the legacy model, Compute Management increases the embedded Instance Pool
size through `UpdateClusterNetwork`. Its existing Instance Configuration
already contains cloud-init and OKE bootstrap configuration.

`pools add` changes the size of an existing pool. It does not invoke the
separate `pools create` workflow.

## Remove RDMA Capacity

Reduce desired pool size by one without choosing a specific worker:

```bash
mgmt-oke --auth instance_principal pools remove oke-rdma \
  --count 1 --dry-run --format json
```

Example dry-run output:

```json
[
  {
    "current_size": 2,
    "decrement_size": null,
    "operation": "pool-resize",
    "owner": "compute-management",
    "pool": "oke-rdma",
    "status": "planned",
    "steps": ["update the Cluster Network's embedded Instance Pool size"],
    "target": "oke-rdma",
    "target_size": 1,
    "warnings": [
      "Pool-level scale-down delegates worker selection to the owning service; use nodes terminate when worker identity matters.",
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Apply after reviewing which ownership model will select the departing worker:

```bash
mgmt-oke --auth instance_principal pools remove oke-rdma --count 1 --wait
```

This operation delegates worker selection to OKE or Compute Management. It is
appropriate when any healthy capacity unit can leave; it is not a way to pick a
particular A100 or other RDMA worker.

Choose a specific worker and decrement desired size:

```bash
mgmt-oke --auth instance_principal nodes terminate rdma-node-1 \
  --dry-run --format json
```

Example dry-run output:

```json
[
  {
    "current_size": 2,
    "decrement_size": true,
    "operation": "node-remove",
    "owner": "compute-management",
    "pool": "oke-rdma",
    "status": "planned",
    "steps": [
      "cordon Kubernetes node",
      "evict non-DaemonSet pods through the Eviction API",
      "detach and automatically terminate the selected Instance Pool member"
    ],
    "target": "rdma-node-1",
    "target_size": 1,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Apply after reviewing the selected worker:

```bash
mgmt-oke --auth instance_principal nodes terminate <rdma-node-name-or-ip> --wait
```

For predictable maintenance, selecting a node is preferable when one worker is
known to be unhealthy. Review workload and topology data before removal.

## Replace a Specific RDMA Worker

```bash
mgmt-oke --auth instance_principal nodes terminate <rdma-node-name-or-ip> \
  --keep-size --wait
```

Managed pools use OKE `DeleteNode`. Legacy pools detach and automatically
terminate the selected Instance Pool instance. `--keep-size` preserves desired
capacity in both cases.

For the legacy model, default removal sends `is_decrement_size=true` to the
embedded Instance Pool. Replacement sends `is_decrement_size=false` together
with automatic termination, so the selected instance is terminated and the
pool launches another instance from its existing Instance Configuration.

## Understand RDMA Convergence

OCI instance state, Kubernetes registration, topology labels, and allocatable
GPU resources can become ready in separate polling intervals. A scale-up can
therefore progress through states such as:

```text
desired=3 oci_active=2 k8s_ready=2 gpu_ready=2 rdma_ready=2
desired=3 oci_active=3 k8s_ready=2 gpu_ready=2 rdma_ready=2
desired=3 oci_active=3 k8s_ready=3 gpu_ready=2 rdma_ready=3
desired=3 oci_active=3 k8s_ready=3 gpu_ready=3 rdma_ready=3
```

`--wait` continues until every applicable value reaches the target. When the
NVIDIA Network Operator is active, `rdma_vf_ready` must also reach the target.
The OCI work request is checked during the same loop. For a self-managed
Cluster Network, the tool snapshots existing resource requests before the
mutation, allowing it to identify the new request even when
`UpdateClusterNetwork` omits a work-request header. Capacity, placement, or
other OCI failures are returned immediately instead of waiting for the
convergence timeout.

## Verify Topology and Resources

```bash
mgmt-oke --auth instance_principal nodes list --pool oke-rdma
mgmt-oke --auth instance_principal nodes list --rdma-only
mgmt-oke --auth instance_principal topology list --pool oke-rdma
mgmt-oke --auth instance_principal addons status
mgmt-oke --auth instance_principal addons validate --target rdma --pool oke-rdma
mgmt-oke --auth instance_principal health run --type rdma --pool oke-rdma
```

Example RDMA health output:

```text
check          scope        status  message                                       recommendation
-------------  -----------  ------  --------------------------------------------  --------------
rdma-topology  rdma-node-1  PASS    Required OCI RDMA topology labels are valid.  -
rdma-topology  rdma-node-2  PASS    Required OCI RDMA topology labels are valid.  -
```

Every Ready RDMA worker should have valid values for:

- `oci.oraclecloud.com/rdma.hpc_island_id`
- `oci.oraclecloud.com/rdma.network_block_id`
- `oci.oraclecloud.com/rdma.local_block_id`

When the NVIDIA Network Operator add-on is active, every Ready RDMA worker must
also advertise a positive `nvidia.com/rdma-vf` allocatable resource before
`--wait` succeeds.

## Compute Cluster Inventory Protection

OKE can expose an internal Instance Pool backing a managed Compute Cluster node
pool. The CLI correlates Compute Cluster and instance membership and suppresses
that internal pool from standalone inventory.

The expected output contains one `oke-rdma` worker pool, not both an OKE node
pool and its internal Instance Pool. Mutations always target the OKE node pool.

## Slinky Slurm Pools

The tool detects Slinky ownership from upstream labels, annotations, and slurmd
worker pods. Scale-up remains available. Scale-down, specific removal, and
replacement are refused until a Slurm-aware drain workflow is implemented.

```bash
mgmt-oke --auth instance_principal pools get oke-rdma
```

If `slinky=yes`, do not attempt to bypass the refusal by changing Kubernetes
labels. Use the Slurm control plane to coordinate node state.

## Infrastructure-As-Code Ownership

Record live size changes in the corresponding Terraform or Resource Manager
input. A later apply can otherwise replace the live size and, when ownership
mode variables change, can replace the RDMA worker architecture itself.

## Troubleshooting

If a new worker reaches OCI ACTIVE but not Kubernetes Ready, inspect node join,
GPU device plug-in, and network operator state. If topology remains absent,
verify the OCI topology labeler and IMDS data before submitting another resize.
