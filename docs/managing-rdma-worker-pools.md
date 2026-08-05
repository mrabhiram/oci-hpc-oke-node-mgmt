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
- for managed creation, OKE node-pool resource-principal permission
  `COMPUTE_CLUSTER_LAUNCH_INSTANCE` and, when used,
  `HOST_GROUP_LAUNCH_INSTANCE`
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

## Create A Managed Compute Cluster RDMA Pool

Create the first managed RDMA pool from a regular managed GPU source and let
the tool create a dedicated Compute Cluster:

```bash
mgmt-oke --auth instance_principal pools create oke-rdma-managed \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool oke-gpu \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --compute-cluster-name oke-rdma-managed-cc \
  --dry-run \
  --format json
```

Use `--compute-cluster-id <ocid>` for an existing Compute Cluster. Add
`--host-group-id <ocid>` to use an existing Compute Host Group in the selected
AD. The dry run validates enhanced-cluster support, image/shape RDMA ports,
single-AD placement, Compute Cluster state, and Host Group state and target.

Apply by replacing `--dry-run --format json` with `--wait`. OKE owns the new
node pool after creation, so resize and specific-node operations continue
through OKE rather than through an internal Instance Pool.

When the existing FSS, Lustre, NVMe RAID, or custom bootstrap is stored in a
legacy `oke-rdma` Instance Configuration, import it explicitly while retaining
`oke-gpu` as the managed OKE template:

```bash
mgmt-oke --auth instance_principal pools create oke-rdma-managed \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 2 \
  --from-pool oke-gpu \
  --bootstrap-from-pool oke-rdma \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --compute-cluster-name oke-rdma-managed-cc \
  --dry-run \
  --format json
```

The managed source controls current OKE identity, CNI, networking, version, and
lifecycle fields. The legacy source contributes its complete `user_data`,
supported bootstrap hooks, and non-reserved custom metadata. The dry run fails
if the sources expose different OKE endpoints or cluster CA values.

## Create A Legacy Cluster Network RDMA Pool

Create a second self-managed Cluster Network pool from the existing `oke-rdma`
configuration and placement:

```bash
mgmt-oke --auth instance_principal pools create oke-rdma-2 \
  --type rdma \
  --count 2 \
  --from-pool oke-rdma \
  --dry-run \
  --format json
```

Apply after reviewing the plan:

```bash
mgmt-oke --auth instance_principal pools create oke-rdma-2 \
  --type rdma \
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
[Creating Worker Pools](./creating-worker-pools.md)
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
  --tag unhealthy --dry-run --format json
```

Example dry-run output:

```json
[
  {
    "current_size": 2,
    "decrement_size": true,
    "details": {
      "customer_reported_host_status": "unhealthy"
    },
    "operation": "node-remove",
    "owner": "compute-management",
    "pool": "oke-rdma",
    "status": "planned",
    "steps": [
      "cordon Kubernetes node",
      "evict non-DaemonSet pods through the Eviction API",
      "tag OCI instance as customer-reported unhealthy",
      "verify OCI instance unhealthy tag",
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
mgmt-oke --auth instance_principal nodes terminate <rdma-node-name-or-ip> \
  --tag unhealthy --wait
```

For predictable maintenance, selecting a node is preferable when one worker is
known to be unhealthy. Review workload and topology data before removal.

## Replace a Specific RDMA Worker

```bash
mgmt-oke --auth instance_principal nodes terminate <rdma-node-name-or-ip> \
  --tag unhealthy --keep-size --wait
```

Managed pools use OKE `DeleteNode`. Legacy pools detach and automatically
terminate the selected Instance Pool instance. `--keep-size` preserves desired
capacity in both cases.

For the legacy model, default removal sends `is_decrement_size=true` to the
embedded Instance Pool. Replacement sends `is_decrement_size=false` together
with automatic termination, so the selected instance is terminated and the
pool launches another instance from its existing Instance Configuration.
The unhealthy-host tag is applied to the selected Compute instance before
either removal path. Existing defined tags are preserved and OCI read-back is
required before detach is submitted.

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

## RDMA Boot Volume Replacement

Specific-node BVR is supported for both RDMA ownership models:

```bash
mgmt-oke --auth instance_principal nodes boot-volume-replace \
  <rdma-node-name-or-ip> --wait
```

OKE preserves the compute instance, IP address, image, and current node
configuration. The waiter requires RDMA topology, GPUs, and applicable Network
Operator VFs to recover.

Only a managed Compute Cluster-backed OKE pool supports pool-wide image or
property updates:

```bash
mgmt-oke --auth instance_principal pools boot-volume-replace \
  <managed-rdma-pool> --image-id <image-ocid> --wait
```

For a legacy self-managed Cluster Network, use specific-node BVR without an
image update. See
[Replacing Worker Boot Volumes](./replacing-worker-boot-volumes.md).

## RDMA Kubernetes Upgrades

Inspect both RDMA ownership and upgrade readiness:

```bash
mgmt-oke pools get oke-rdma
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades plan --to v1.36 --format json
```

A managed Compute Cluster RDMA pool is upgraded through OKE exactly like other
managed node pools. `boot-volume-replace` and `instance-replace` use
`UpdateNodePool`; blue-green clones the complete node-pool configuration while
preserving Compute Cluster and host-group placement. The OKE-internal backing
Instance Pool remains hidden and is never mutated directly.

A legacy Cluster Network upgrade clones the existing Instance Configuration
before cycling capacity. The clone preserves RDMA VNICs, agents, cloud-init,
custom metadata, SSH configuration, FSS, Lustre, local NVMe RAID, and
pre/post-bootstrap scripts. It refreshes the current OKE API endpoint, cluster
CA, and Kubernetes bootstrap version, then attaches the new configuration with
the Cluster Network ETag.

The default legacy strategy is `instance-replace`: increase the embedded
Instance Pool by one, wait for a target-version RDMA worker with valid GPU,
RDMA topology, and applicable `nvidia.com/rdma-vf`, then detach and terminate
one externally drained old instance while decrementing back to desired size.

Preview an explicit legacy strategy:

```bash
mgmt-oke pools upgrade oke-rdma \
  --to v1.36.1 \
  --strategy instance-replace \
  --dry-run
```

Example live-derived output:

```text
operation            owner            pool      strategy          target_version  target_size  workload_pods
-------------------  ---------------  --------  ----------------  --------------  -----------  -------------
worker-pool-upgrade  cluster-network  oke-rdma  instance-replace  v1.36.1         2            1

api_server_refreshed=true
cluster_ca_refreshed=true
kueue_blocker=ClusterQueue/bm-gpu4-8-rdma-topology-aware stopPolicy=None
```

The live plan proved the transformed bootstrap without creating an Instance
Configuration or changing capacity. Execution remains blocked until the
ordinary pod is moved, Kueue is held and empty, both workers are externally
cordoned, and the operator provides the workload attestation.

No RDMA upgrade path calls Kubernetes cordon, drain, eviction, or uncordon.
See [Kubernetes Upgrades](./kubernetes-upgrades.md) for strategy details and
checkpoint recovery.

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
