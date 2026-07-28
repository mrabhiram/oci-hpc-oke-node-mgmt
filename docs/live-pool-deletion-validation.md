# Live Worker Pool Deletion Validation

This report shows representative `mgmt-oke 0.7.0` commands and output captured
from live worker-pool deletion validation against an OCI HPC OKE cluster in
`uk-london-1`.

Resource identifiers, temporary pool names, and Kubernetes node names are
replaced with descriptive placeholders. Pool ownership, counts, operations,
safety decisions, and convergence results are retained.

## Managed GPU Pool

The managed GPU pool created during the lifecycle validation contained one
Ready `VM.GPU.A10.1` worker.

### Preview

```bash
mgmt-oke pools delete gpu-validation \
  --dry-run \
  --format json
```

Example output:

```json
[
  {
    "current_size": 1,
    "details": {
      "kind": "node-pool",
      "nodes": [
        "<gpu-worker-node>"
      ],
      "placement": "standard"
    },
    "operation": "pool-delete",
    "owner": "oke",
    "pool": "gpu-validation",
    "status": "planned",
    "steps": [
      "cordon every Kubernetes node in the pool",
      "evict non-DaemonSet pods through the Eviction API",
      "delete the managed OKE node-pool resource"
    ],
    "target_size": 0,
    "warnings": [
      "Pool deletion permanently removes its workers and their boot volumes.",
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Dry-run completed the ownership, membership, workload, eviction-admission,
Cluster Autoscaler, Slinky, and system-pool checks without cordoning or deleting
anything.

### Delete And Wait

```bash
mgmt-oke pools delete gpu-validation \
  --wait \
  --timeout 1800 \
  --poll-interval 20 \
  --yes \
  --format json
```

Example output:

```json
[
  {
    "kind": "node-pool",
    "name": "gpu-validation",
    "old_size": 1,
    "placement": "standard",
    "status": "deleted",
    "target_size": 0,
    "work_request_id": "<oke-work-request-ocid>"
  }
]
```

The live command cordoned the selected pool member, revalidated the workload
view, submitted pod evictions when required, called OKE `DeleteNodePool`, and
waited until the node-pool resource and Kubernetes worker were absent.

## Self-Managed RDMA Cluster Network

The retained production RDMA pool was not deleted. The complete destructive
plan was validated live with `--dry-run`:

```bash
mgmt-oke pools delete oke-rdma \
  --dry-run \
  --format json
```

Example output:

```json
[
  {
    "current_size": 2,
    "details": {
      "instance_configuration_id": null,
      "kind": "cluster-network",
      "nodes": [
        "<rdma-worker-1>",
        "<rdma-worker-2>"
      ],
      "placement": "cluster-network"
    },
    "operation": "pool-delete",
    "owner": "compute-management",
    "pool": "oke-rdma",
    "status": "planned",
    "steps": [
      "cordon every Kubernetes node in the pool",
      "evict non-DaemonSet pods through the Eviction API",
      "terminate the Cluster Network and its embedded Instance Pool"
    ],
    "target_size": 0,
    "warnings": [
      "Pool deletion permanently removes its workers and their boot volumes.",
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

The plan routes deletion through Compute Management
`TerminateClusterNetwork`; it does not terminate the embedded Instance Pool
directly.

## Instance Configuration Ownership

`mgmt-oke` tags the Instance Configuration derived during RDMA pool creation
with `mgmt-oke-created=true`.

For a waited deletion:

- the Cluster Network is terminated first
- the ownership tag is read again after termination
- a tool-created Instance Configuration is deleted only when that ownership
  check still succeeds
- a stack-owned or otherwise unowned Instance Configuration is preserved

The live source `oke-rdma` pool was classified as stack-owned, so its Instance
Configuration was not scheduled for cleanup. `--no-wait` always retains the
derived configuration and reports its identifier because safe post-termination
ownership revalidation has not completed.

## System Pool Protection

Live validation confirmed that the default system-pool guard refuses deletion:

```bash
mgmt-oke pools delete oke-system --dry-run
```

Example output:

```text
Error: Refusing to delete the OKE system pool. Use --allow-system-pool only
after another system-capable pool is ready.
```

`--allow-system-pool` is an acknowledgement, not a readiness guarantee. Verify
that critical add-ons and system workloads have another schedulable destination
before using it.

## Additional Safety Boundaries

Whole-pool deletion is refused when:

- the Cluster Autoscaler owns the pool
- Slinky Slurm owns the pool or one of its workers
- Kubernetes membership or workload discovery is incomplete
- drain would remove unacknowledged `emptyDir` data
- drain would evict an unmanaged pod without `--force`
- workload membership changes after the nodes are cordoned and the new state
  violates the selected drain options
- the live OCI membership differs from the reviewed plan
- another mutation holds the Kubernetes Lease

## Verify Deletion

```bash
mgmt-oke pools list
mgmt-oke nodes list --pool gpu-validation
mgmt-oke status
```

After the live managed GPU deletion, the temporary pool was absent from both
OCI inventory and Kubernetes. The original managed CPU, managed GPU, system,
and self-managed RDMA source pools remained at their initial desired and Ready
counts.

See [Creating Worker Pools](./creating-worker-pools.md) for lifecycle creation,
[Command Reference](./command-reference.md) for all deletion options, and
[Architecture](./architecture.md) for backend routing and mutation safety.
