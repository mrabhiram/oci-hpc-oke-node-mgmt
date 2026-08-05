# Live Unhealthy Host Termination Validation

This page records `mgmt-oke 0.10.0` node-termination dry runs executed against
a running OCI HPC OKE cluster in `uk-london-1` on July 31, 2026. Kubernetes
node identities are sanitized. Pool ownership, shapes, counts, operations,
steps, and workload counts are retained.

## Validation Boundary

No Compute instance was tagged or terminated during this validation.

The commands performed live Kubernetes and OCI discovery, selected the target
workers, resolved their owning services, inspected workloads, checked dry-run
eviction admission, calculated target capacity, and rendered operation plans.
Because `--dry-run` was used, they did not call `UpdateInstance`, OKE
`DeleteNode`, Compute Management `DetachInstancePoolInstance`, Kubernetes node
patch, or the Eviction API.

## Validation Environment

| Worker | Pool model | Shape | Observed state |
| --- | --- | --- | --- |
| Managed GPU | Standard OKE node pool | `VM.GPU.A10.1` | `1/1 Ready`, no workload pods |
| RDMA GPU | Self-managed Cluster Network and Instance Pool | `BM.GPU4.8` | `2/2 Ready`, valid RDMA topology, no workload pods on target |

The CLI used instance-principal authentication and discovered the OKE cluster,
region, and compartment from the operator kubeconfig and OKE `GetCluster`.

## Unhealthy Tag Plan

The following command selected one managed A10 worker and one A100 RDMA worker
in a single operation:

```bash
mgmt-oke --auth instance_principal nodes terminate \
  gpu-a10-node-1 rdma-a100-node-1 \
  --tag unhealthy \
  --keep-size \
  --dry-run \
  --format json
```

Example live output:

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
    "target": "gpu-a10-node-1",
    "target_size": 1,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  },
  {
    "current_size": 2,
    "decrement_size": false,
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
    "target": "rdma-a100-node-1",
    "target_size": 2,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

The managed worker was routed to OKE `DeleteNode`. The self-managed RDMA
worker was routed to Instance Pool detach with automatic termination. Both
plans placed tag application and verification after workload preparation and
before the ownership-specific termination step.

## Explicit No-Tag Plan

The explicit automation opt-out was validated against the managed A10 worker:

```bash
mgmt-oke --auth instance_principal nodes terminate gpu-a10-node-1 \
  --tag none \
  --keep-size \
  --dry-run \
  --format json
```

Example live output:

```json
[
  {
    "current_size": 1,
    "decrement_size": false,
    "details": {
      "customer_reported_host_status": "not-requested"
    },
    "operation": "node-remove",
    "owner": "oke",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": [
      "cordon Kubernetes node",
      "evict non-DaemonSet pods through the Eviction API",
      "delete the selected worker through OKE DeleteNode"
    ],
    "target": "gpu-a10-node-1",
    "target_size": 1,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

## Result

| Check | Result |
| --- | --- |
| Public CLI version | `0.10.0` |
| Managed GPU ownership and plan | Passed live |
| Self-managed RDMA ownership and plan | Passed live |
| Multi-pool unhealthy-tag plan | Passed live |
| Explicit `--tag none` plan | Passed live |
| Workload and eviction preflight | Passed live |
| Actual `UpdateInstance` tag mutation | Not performed; unit-tested |
| Actual node termination or replacement | Not performed |

This validation proves live discovery, selection, safety preflight, ownership
routing, and dry-run rendering. It is not evidence of a completed unhealthy
tag update or worker replacement.
