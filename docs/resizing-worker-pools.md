# Resizing Worker Pools

This guide explains how to add or remove worker capacity with `mgmt-oke` while
preserving the ownership model of each pool.

## Overview

The same command supports standard managed OKE pools, managed Compute
Cluster-backed RDMA pools, legacy Cluster Network pools, and standalone Instance
Pools. Discovery chooses the correct OCI API from the pool's `kind` and backing
identifiers.

| Pool ownership | Resize API |
| --- | --- |
| Managed OKE node pool | OKE `UpdateNodePool` |
| Managed OKE node pool in a Compute Cluster | OKE `UpdateNodePool` |
| Legacy Cluster Network | Compute Management `UpdateClusterNetwork` |
| Standalone Instance Pool | Compute Management `UpdateInstancePool` |

## Prerequisites

- working OCI and Kubernetes discovery
- IAM permission to update the target pool's owning OCI resource and inspect
  its work requests when using `--wait`
- no Cluster Autoscaler ownership of the target pool
- updated Terraform or Resource Manager inputs when the pool is also managed as
  infrastructure as code

## Procedure

### Step 1: Inspect the Pool

```bash
mgmt-oke --auth instance_principal pools get <pool-name>
mgmt-oke --auth instance_principal autoscaler status
```

Confirm the current `desired`, `oci_active`, and `k8s_ready` values. Check
`kind`, `placement`, `autoscaler`, and `slinky` before continuing.

Example inspection result for `oke-cpu`:

```text
name     kind       placement  shape                desired  oci_active  k8s_ready  gpu  rdma  rdma_vf_required  slinky  autoscaler  kueue_flavor
-------  ---------  ---------  -------------------  -------  ----------  ---------  ---  ----  ----------------  ------  ----------  ------------
oke-cpu  node-pool  standard   VM.Standard.E5.Flex  1        1           1          -    no    no                no      -           -

Cluster Autoscaler
(none)
```

### Step 2: Choose Exact Size or Delta

Set an exact desired size:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --size 3 --wait
```

Add one worker relative to the current desired size:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --delta 1 --wait
```

Remove one worker relative to the current desired size:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --delta -1 --wait
```

Positive deltas add capacity. Negative deltas remove capacity. A target below
zero is rejected before the OCI API is called.

The explicit aliases avoid signed arithmetic:

```bash
mgmt-oke --auth instance_principal pools add <pool-name> --count 1 --wait
mgmt-oke --auth instance_principal pools remove <pool-name> --count 1 --wait
```

Pool-level scale-down does not select a worker. The owning OCI service chooses
which instance leaves while converging to the lower desired size. When a
specific worker must be removed, use `nodes terminate` instead.

### Step 3: Preview the Operation

Run the same request with `--dry-run` before changing capacity:

```bash
mgmt-oke --auth instance_principal pools add <pool-name> --count 1 --dry-run
```

Concrete example with machine-readable output:

```bash
mgmt-oke --auth instance_principal pools add oke-cpu \
  --count 1 --dry-run --format json
```

Example output:

```json
[
  {
    "current_size": 1,
    "decrement_size": null,
    "operation": "pool-resize",
    "owner": "oke",
    "pool": "oke-cpu",
    "status": "planned",
    "steps": ["update the managed OKE node-pool desired size"],
    "target": "oke-cpu",
    "target_size": 2,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

The plan reports the operation, owning service, current and target sizes, OCI
action, and infrastructure-as-code drift warning. Dry-run performs discovery
and safety validation but does not acquire the mutation Lease or call a
mutation API.

### Step 4: Confirm the Operation

Without `--yes`, the CLI prints the current and target sizes and requires the
pool name to be typed exactly. This is the recommended interactive workflow.

For reviewed non-interactive automation, add `--yes`:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --delta 1 --wait --yes
```

Do not use `--yes` in an automation that has not independently selected and
validated the target pool.

### Step 5: Wait for Convergence

`--wait` polls until desired OCI size, active OCI instances, and Kubernetes
Ready nodes reach the target. GPU and RDMA pools include additional resource
checks. The command also monitors the owning OCI work request and exits
immediately with OCI's error details if it fails or is canceled. For
self-managed resources, it snapshots existing resource work requests before
mutation so monitoring remains precise when the update response omits a work
request identifier.

These layers can converge at different times. For example, an RDMA pool can
temporarily report:

```text
desired=3 oci_active=3 k8s_ready=2 gpu_ready=2 rdma_ready=2
desired=3 oci_active=3 k8s_ready=3 gpu_ready=2 rdma_ready=3
desired=3 oci_active=3 k8s_ready=3 gpu_ready=3 rdma_ready=3
```

The command succeeds only after every applicable count reaches the target.

The default timeout is 1800 seconds with a 30-second polling interval. Override
them when provisioning is expected to take longer:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> \
  --size 4 --wait --timeout 3600 --poll-interval 60
```

### Submit and Wait Separately

Omit `--wait` to submit a resize and return after OCI accepts the request:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --size 3 --yes
```

The result is reported as `submitted`. To wait later, repeat the exact target
with `--wait`:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --size 3 --wait
```

When desired size is already 3, the second command does not submit another
resize. It acts as a convergence barrier and returns `ready` only after OCI,
Kubernetes, and applicable GPU and RDMA checks pass.

## Managed Compute Cluster Pools

For a managed OKE RDMA pool, the CLI sends only the desired size to OKE. OKE
creates or removes workers and maintains their Compute Cluster placement. The
tool does not update the Compute Cluster or its internal backing Instance Pool.

```bash
mgmt-oke --auth instance_principal pools resize oke-rdma --delta 1 --wait
```

## Legacy Cluster Network Pools

For a legacy self-managed RDMA pool, the CLI updates the selected embedded
Instance Pool size through `UpdateClusterNetwork`. The existing Instance
Configuration, cloud-init, tags, and other embedded pool fields are preserved.

```bash
mgmt-oke --auth instance_principal pools resize oke-rdma --size 3 --wait
```

No separate worker creation command is required. New instances use the
existing Instance Configuration and its OKE bootstrap cloud-init.

## Safety Checks

The CLI refuses a manual resize when:

- Cluster Autoscaler owns the target pool
- the target size is negative
- required OCI ownership metadata is missing
- a scale-down targets a Slinky-managed pool
- OCI target discovery cannot resolve the compartment
- another `mgmt-oke` mutation holds the Kubernetes Lease

Scale-up remains available for a Slinky-managed pool. Scale-down requires a
future Slurm-aware drain workflow and is currently refused.

Avoid scaling an OKE system pool to zero. The CLI validates numeric size but
does not replace the operational minimums required by the cluster.

The Lease is enabled by default. Use `--no-lock` only for an intentional
recovery operation when Kubernetes Lease access is unavailable.

## Verification

After the command completes, inspect the pool and nodes:

```bash
mgmt-oke --auth instance_principal pools get <pool-name>
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
```

For RDMA pools, also run:

```bash
mgmt-oke --auth instance_principal topology list --pool <pool-name>
mgmt-oke --auth instance_principal addons status
```

Example converged RDMA pool result:

```text
name      kind             placement        shape      desired  oci_active  k8s_ready  gpu             rdma
--------  ---------------  ---------------  ---------  -------  ----------  ---------  --------------  ----
oke-rdma  cluster-network  cluster-network  BM.GPU4.8  2        2           2          nvidia.com/gpu  yes
```

## Infrastructure-As-Code Ownership

A successful CLI resize changes live OCI state. It does not edit Terraform
variables or an OCI Resource Manager stack. Update the declared pool size before
the next apply, or the infrastructure-as-code workflow can restore the previous
value.

## Troubleshooting

If OCI reports a failed work request, resolve the reported service error before
retrying. If `--wait` times out without an OCI failure, the resize request might
still be active. Re-run the read-only inventory commands and inspect the OCI
work request before submitting another resize. A timeout does not imply
rollback.
