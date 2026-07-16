# Removing and Replacing Worker Nodes

This guide explains how to select one worker node, remove it from its pool, or
replace it while preserving the pool's desired size.

## Overview

`nodes remove` always targets a specific Kubernetes worker. The node can be
identified by Kubernetes name, Slinky name, internal IP, provider ID, or OCI
instance OCID.

Use `nodes remove` instead of pool-level scale-down when the identity of the
departing worker matters. `pools resize --delta -1` reduces capacity but leaves
worker selection to the owning OCI service.

Default behavior removes the selected node and decrements desired pool size.
`--keep-size` removes the selected node without decrementing desired size, so
the owning service launches a replacement.

| Pool ownership | Removal API |
| --- | --- |
| Managed OKE node pool | OKE `DeleteNode` |
| Managed OKE Compute Cluster pool | OKE `DeleteNode` |
| Legacy Cluster Network | `DetachInstancePoolInstance` with automatic termination |
| Standalone Instance Pool | `DetachInstancePoolInstance` with automatic termination |

## Prerequisites

- complete OCI and Kubernetes inventory
- IAM permission to delete an OKE node or detach an Instance Pool instance
- application workloads safely evacuated or explicitly accepted
- no Cluster Autoscaler ownership of the target pool
- no Slinky ownership of the target node or pool

## Procedure

### Step 1: Inspect the Node

```bash
mgmt-oke --auth instance_principal nodes get <node-name-or-ip>
```

Review:

- `pool`
- `status`
- `shape`
- `workload_pods`
- `slurm_pods`
- GPU and RDMA resources

List the rest of the pool before changing it:

```bash
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
mgmt-oke --auth instance_principal pools get <pool-name>
```

### Step 2: Remove and Decrement Pool Size

Use the default operation when the selected node and one unit of desired
capacity should both be removed:

```bash
mgmt-oke --auth instance_principal nodes remove <node-name-or-ip> --wait
```

The CLI asks for the exact node name before submitting the mutation. The final
target size is the current desired size minus one.

### Step 3: Replace and Keep Pool Size

Use `--keep-size` when the selected worker should be replaced:

```bash
mgmt-oke --auth instance_principal nodes remove <node-name-or-ip> \
  --keep-size --wait
```

For a managed pool, OKE deletes the node with `is_decrement_size=false`. For a
self-managed pool, Compute Management detaches and automatically terminates the
instance with `is_decrement_size=false`. The pool then creates a replacement.

### Step 4: Verify the Result

```bash
mgmt-oke --auth instance_principal pools get <pool-name>
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
```

For replacement, verify that the original node is absent and a new Ready node
has joined. For GPU or RDMA pools, `--wait` also checks applicable resource
readiness.

The replacement wait does not finish merely because the selected node has
disappeared. It also requires desired size, active OCI membership, Kubernetes
Ready count, and applicable GPU, RDMA topology, and RDMA VF counts to converge.

## Managed OKE Eviction Options

Managed OKE deletion uses a default eviction grace duration of `PT10M`.
Override it when workloads need more time:

```bash
mgmt-oke --auth instance_principal nodes remove <node-name> \
  --keep-size --eviction-grace PT20M --wait
```

`--force-after-grace` allows OKE to force compute deletion after the override
grace period:

```bash
mgmt-oke --auth instance_principal nodes remove <node-name> \
  --keep-size --eviction-grace PT20M --force-after-grace --wait
```

These options apply only to managed OKE node pools. They are rejected for
Cluster Network and standalone Instance Pool nodes.

## Workload Safety

The CLI refuses removal when ordinary workload pods are running on the node.
DaemonSets, mirror pods, and known system-namespace pods are counted separately.

`--allow-workloads` bypasses the workload-count refusal:

```bash
mgmt-oke --auth instance_principal nodes remove <node-name-or-ip> \
  --keep-size --allow-workloads --wait
```

> [!WARNING]
> The self-managed Instance Pool path does not implement a Kubernetes cordon or
> drain. Drain and validate a self-managed node before using
> `--allow-workloads`. Managed OKE deletion delegates eviction to OKE.

## Non-Interactive Execution

Use `--yes` only after automation has selected the node and checked workload,
pool, autoscaler, and Slinky state:

```bash
mgmt-oke --auth instance_principal nodes remove <node-name-or-ip> \
  --keep-size --wait --yes
```

## Refused Operations

The CLI refuses node removal when:

- the node cannot be joined to an OCI instance
- the owning pool cannot be determined
- Cluster Autoscaler owns the pool
- the pool or node is Slinky-managed
- workload pods are present without `--allow-workloads`
- managed-only eviction options are supplied for a self-managed pool
- OCI target discovery fails

There is no force option that bypasses Cluster Autoscaler or Slinky ownership
protection.

## Infrastructure-As-Code Ownership

Default removal decrements live desired size. Update the corresponding stack
input before the next Terraform or Resource Manager apply. Replacement with
`--keep-size` does not change desired size but does replace the selected live
instance.
