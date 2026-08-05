# Removing and Replacing Worker Nodes

This guide explains how to select one worker node, remove it from its pool, or
replace it while preserving the pool's desired size.

## Overview

`nodes terminate` always targets selected Kubernetes workers. `nodes remove`
is an equivalent compatibility alias. A node can be
identified by Kubernetes name, Slinky name, internal IP, provider ID, or OCI
instance OCID.

Use `nodes terminate` instead of pool-level scale-down when the identity of the
departing worker matters. `pools resize --delta -1` reduces capacity but leaves
worker selection to the owning OCI service.

To preserve the compute instance and replace only its boot volume, use
`nodes boot-volume-replace`. Individual BVR preserves the current image and
node configuration. To apply a new image to every worker in a managed pool, use
`pools boot-volume-replace --image-id`. See
[Replacing Worker Boot Volumes](./replacing-worker-boot-volumes.md).

To drain and remove an entire owning pool rather than selected workers, use
`pools delete`. See [Live Worker Pool Deletion
Validation](./live-pool-deletion-validation.md) for a managed deletion and a
self-managed RDMA deletion plan captured from live commands.

Default behavior removes the selected node and decrements desired pool size.
`--keep-size` removes the selected node without decrementing desired size, so
the owning service launches a replacement.

| Pool ownership | Removal API |
| --- | --- |
| Managed OKE node pool | OKE `DeleteNode` |
| Managed OKE Compute Cluster pool | OKE `DeleteNode` |
| Legacy Cluster Network | `DetachInstancePoolInstance` with automatic termination |
| Standalone Instance Pool | `DetachInstancePoolInstance` with automatic termination |

Before termination, the operator can report a bad host through the defined
tag `ComputeInstanceHostActions.CustomerReportedHostStatus=unhealthy`. The CLI
preserves all other defined tags, updates the instance with its current ETag,
and verifies the value through a second OCI read. If any requested tag cannot
be applied or verified, no selected worker is submitted for termination.

## Prerequisites

- complete OCI and Kubernetes inventory
- IAM permission to delete an OKE node or detach an Instance Pool instance and
  inspect its work requests when using `--wait`
- permission to inspect and update Compute instances when using
  `--tag unhealthy`, plus access to the `ComputeInstanceHostActions` tag
  namespace and `CustomerReportedHostStatus` tag definition
- Kubernetes permission to read nodes and pods, patch nodes, create pod
  evictions, and manage the `kube-system/mgmt-oke-mutation` Lease
- no Cluster Autoscaler ownership of the target pool
- no Slinky ownership of the target node or pool

## Procedure

### Step 1: Inspect the Node

```bash
mgmt-oke --auth instance_principal nodes get gpu-node-1
```

Example output:

```text
name        slurm_name  ip          status  pool     shape         gpu               rdma  rdma_vf  workload_pods  slurm_pods  system_pods  daemonsets
----------  ----------  ----------  ------  -------  ------------  ----------------  ----  -------  -------------  ----------  -----------  ----------
gpu-node-1  -           10.0.1.101  Ready   oke-gpu  VM.GPU.A10.1  nvidia.com/gpu=1  no    -        0              0           0            16
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

### Step 2: Preview Drain And Termination

```bash
mgmt-oke --auth instance_principal nodes terminate gpu-node-1 \
  --tag unhealthy --keep-size --dry-run --format json
```

Example replacement dry-run output:

```json
[
  {
    "current_size": 1,
    "decrement_size": false,
    "details": {
      "customer_reported_host_status": "unhealthy"
    },
    "operation": "node-remove",
    "owner": "oke",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": [
      "cordon Kubernetes node",
      "evict non-DaemonSet pods through the Eviction API",
      "tag OCI instance as customer-reported unhealthy",
      "verify OCI instance unhealthy tag",
      "delete the selected worker through OKE DeleteNode"
    ],
    "target": "gpu-node-1",
    "target_size": 1,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Preflight resolves the owning OCI service, calculates target capacity, lists
pods, checks dry-run eviction admission, and reports every planned step. It
does not cordon, evict, tag, or terminate the node.

## Host Health Decision

Use `--tag unhealthy` when the selected worker is being removed because its
GPU, RDMA fabric, network links, or other host hardware is unhealthy:

```bash
mgmt-oke nodes terminate <node-name-or-ip> \
  --tag unhealthy --keep-size --wait
```

Use `--tag none` when the removal is not a customer-reported host failure:

```bash
mgmt-oke nodes terminate <node-name-or-ip> --tag none --wait
```

If `--tag` is omitted, the CLI asks the following question for each selected
node before showing or executing the plan:

```text
Is gpu-node-1 unhealthy and should it be tagged before termination? [y/N]:
```

For multi-node selection, `--tag unhealthy` applies to every selected node and
`--tag none` skips every node. Omit the option to answer independently per
node. `--yes` controls the later OCI mutation confirmation and never answers
the host health question.

### Step 3: Remove and Decrement Pool Size

Use the default operation when the selected node and one unit of desired
capacity should both be removed:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --tag none --wait
```

The CLI asks for the exact node name before submitting the mutation. The final
target size is the current desired size minus one.

By default, the CLI acquires the mutation Lease, cordons the node, evicts
eligible pods, and then invokes the owning OCI termination API.

### Step 4: Replace and Keep Pool Size

Use `--keep-size` when the selected worker should be replaced:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --tag unhealthy --keep-size --wait
```

For a managed pool, OKE deletes the node with `is_decrement_size=false`. For a
self-managed pool, Compute Management detaches and automatically terminates the
instance with `is_decrement_size=false`. The pool then creates a replacement.

### Step 5: Verify the Result

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
When OCI returns work-request identifiers, the waiter monitors each request and
reports a failed or canceled operation immediately.

## Managed OKE Eviction Options

Managed OKE deletion uses a default eviction grace duration of `PT10M`.
Override it when workloads need more time:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name> \
  --tag none --keep-size --eviction-grace PT20M --wait
```

`--force-after-grace` allows OKE to force compute deletion after the override
grace period:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name> \
  --tag none --keep-size --eviction-grace PT20M --force-after-grace --wait
```

These options apply only to managed OKE node pools. They are rejected for
Cluster Network and standalone Instance Pool nodes.

## Kubernetes Drain Safety

Drain is enabled by default. The CLI cordons every selected worker, ignores
DaemonSet and mirror pods, and submits the remaining pods through the
`policy/v1` Eviction API. PodDisruptionBudgets can delay or block the operation;
the command retries until `--drain-timeout` expires.

Pod-local `emptyDir` data requires explicit acknowledgement:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --tag none --delete-emptydir-data --wait
```

Example refusal without that acknowledgement:

```text
Error: Refusing to drain cpu-node-1: pods use emptyDir data:
kueue-system/kueue-controller-manager-example,
monitoring/kube-prometheus-stack-grafana-0. Use --delete-emptydir-data to
acknowledge data loss.
```

The command exits with status `2` and does not cordon or terminate the node.

Pods without a controller require `--force` because Kubernetes will not
recreate them:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --tag none --force --wait
```

Use `--no-drain` only when the worker has already been drained by an external
workflow. If ordinary workload pods remain, `--allow-workloads` is also
required:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --tag none --no-drain --allow-workloads --wait
```

Standalone node maintenance uses the same selection and eviction engine:

```bash
mgmt-oke nodes cordon gpu-node-1 --dry-run --format json
mgmt-oke nodes drain <node-name>
mgmt-oke nodes uncordon <node-name>
```

Example cordon dry-run output:

```json
[
  {
    "current_size": null,
    "decrement_size": null,
    "operation": "node-cordon",
    "owner": "kubernetes",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": ["set spec.unschedulable=true"],
    "target": "gpu-node-1",
    "target_size": null,
    "warnings": [],
    "workload_pods": 0
  }
]
```

## Non-Interactive Execution

Use `--yes` only after automation has selected the node and checked workload,
pool, autoscaler, and Slinky state:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --tag none --keep-size --wait --yes
```

Noninteractive execution must provide either `--tag unhealthy` or
`--tag none`; `--yes` does not infer host health.

## Refused Operations

The CLI refuses node removal when:

- the node cannot be joined to an OCI instance
- the owning pool cannot be determined
- Cluster Autoscaler owns the pool
- the pool or node is Slinky-managed
- `--no-drain` is selected with workload pods and without `--allow-workloads`
- drain would delete `emptyDir` data without acknowledgement
- drain would evict a pod without a controller and `--force` is absent
- managed-only eviction options are supplied for a self-managed pool
- the requested unhealthy tag cannot be authorized, applied, or verified
- OCI target discovery fails
- another mutation holds the Kubernetes Lease

There is no force option that bypasses Cluster Autoscaler or Slinky ownership
protection.

## Infrastructure-As-Code Ownership

Default removal decrements live desired size. Update the corresponding stack
input before the next Terraform or Resource Manager apply. Replacement with
`--keep-size` does not change desired size but does replace the selected live
instance.
