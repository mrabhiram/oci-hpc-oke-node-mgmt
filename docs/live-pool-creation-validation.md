# Live Worker Pool Creation Validation

This report shows representative `mgmt-oke` commands and output captured from
live worker-pool creation validation against an OCI HPC OKE cluster in
`uk-london-1`.

The validation cluster contained:

- a managed OKE GPU pool using `VM.GPU.A10.1`
- a self-managed RDMA Cluster Network pool using `BM.GPU4.8`
- working OKE bootstrap metadata on both source pools
- an enhanced OKE control plane with the managed Compute Cluster API available

OCI resource identifiers, availability-domain names, subnet identifiers,
temporary pool names, and Kubernetes node names are replaced with descriptive
placeholders. Pool types, shapes, counts, operations, status values, and failure
reasons are retained.

## Managed GPU Pool

### Preview

The source pool supplies its image, OKE bootstrap, VCN-native pod networking,
labels, tags, cycling settings, and other compatible defaults:

```bash
mgmt-oke pools create gpu-validation \
  --type gpu \
  --count 1 \
  --from-pool oke-gpu \
  --dry-run \
  --format json
```

Example output excerpt:

```json
[
  {
    "current_size": 0,
    "details": {
      "effective": {
        "backend": "oke-node-pool",
        "count": 1,
        "name": "gpu-validation",
        "shape": "VM.GPU.A10.1",
        "storage": {
          "fss_mounts": [],
          "lustre_mounts": [],
          "mode": "inherit",
          "nvme_raid": null
        }
      },
      "requested": {
        "storage_mode": "inherit",
        "type": "gpu"
      },
      "source_pool": "oke-gpu"
    },
    "operation": "pool-create",
    "owner": "oke",
    "pool": "gpu-validation",
    "status": "planned",
    "target_size": 1
  }
]
```

### Create And Wait

After reviewing the plan, the live validation used non-interactive confirmation
and waited for OCI and Kubernetes convergence:

```bash
mgmt-oke pools create gpu-validation \
  --type gpu \
  --count 1 \
  --from-pool oke-gpu \
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
    "k8s_ready": 1,
    "kind": "node-pool",
    "name": "gpu-validation",
    "node_pool_id": "<managed-node-pool-ocid>",
    "oci_active": 1,
    "placement": "standard",
    "shape": "VM.GPU.A10.1",
    "source_pool": "oke-gpu",
    "status": "ready",
    "target_size": 1,
    "type": "gpu",
    "work_request_id": "<oke-work-request-ocid>"
  }
]
```

The command returned only after the new OKE node pool had one active OCI worker,
one Ready Kubernetes node, and the expected allocatable GPU resource.

## Managed Compute Cluster RDMA Pool

This validation was executed on 2026-08-04. It used a regular managed GPU pool
as the source for the first managed RDMA pool.

### Fresh Placement Preview

The command intentionally supplied the display-form AD name. The tool resolved
it to the tenancy-prefixed canonical name before validating shape, image,
network, and placement compatibility:

```bash
mgmt-oke pools create rdma-managed-validation \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool oke-gpu \
  --availability-domain UK-LONDON-1-AD-3 \
  --shape BM.GPU4.8 \
  --compute-cluster-name rdma-managed-validation-cc \
  --dry-run \
  --format json
```

Sanitized output excerpt from that command:

```json
[
  {
    "current_size": 0,
    "details": {
      "effective": {
        "availability_domains": ["<canonical-ad-3>"],
        "backend": "oke-node-pool",
        "cni_type": "OCI_VCN_IP_NATIVE",
        "compute_cluster_action": "create",
        "compute_cluster_id": null,
        "compute_cluster_name": "rdma-managed-validation-cc",
        "count": 1,
        "fault_domains": [],
        "host_group_ids": [],
        "kubernetes_version": "v1.35.2",
        "name": "rdma-managed-validation",
        "placement": "compute-cluster",
        "shape": "BM.GPU4.8",
        "storage": {
          "fss_mounts": [],
          "lustre_mounts": [],
          "mode": "inherit",
          "nvme_raid": null
        }
      },
      "requested": {
        "availability_domain": "UK-LONDON-1-AD-3",
        "compute_cluster_name": "rdma-managed-validation-cc",
        "create_compute_cluster": true,
        "rdma_mode": "compute-cluster",
        "shape": "BM.GPU4.8",
        "storage_mode": "inherit",
        "type": "rdma"
      },
      "source_pool": "oke-gpu"
    },
    "operation": "pool-create",
    "owner": "oke+compute",
    "pool": "rdma-managed-validation",
    "status": "planned",
    "target_size": 1
  }
]
```

The full captured output also retained the inherited image, worker subnet, pod
subnet, NSGs, boot-volume size, maximum pods, and OKE bootstrap settings. Those
resource identifiers are omitted here rather than replaced with realistic
OCIDs.

### Initial Capacity Failure

The first mutation used the reviewed request with `--wait --yes`. The dedicated
Compute Cluster reached `ACTIVE`, and OKE accepted creation of the managed node
pool. The OKE worker-provisioning work request then failed:

```text
Error: OCI work request <oke-work-request-ocid> ended in FAILED: Out of host
capacity. Managed node pool rdma-managed-validation and Compute Cluster
<compute-cluster-ocid> are retained for inspection.
```

Post-failure inventory showed the node pool resource but no registered
Kubernetes worker. The failed node pool was deleted through `mgmt-oke`, and the
empty retained Compute Cluster was then deleted explicitly. A fresh OCI Compute
Capacity Report still returned:

```json
[
  {
    "shape": "BM.GPU4.8",
    "status": "OUT_OF_HOST_CAPACITY",
    "available_count": 0,
    "fault_domain": null
  }
]
```

This exercised Compute Cluster creation, OKE request submission, failed
work-request monitoring, retained-resource reporting, and cleanup.

### Successful Live Retry

After OCI reported one available host, the same reviewed request was submitted
again:

```bash
mgmt-oke pools create rdma-managed-validation \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool oke-gpu \
  --availability-domain UK-LONDON-1-AD-3 \
  --shape BM.GPU4.8 \
  --compute-cluster-name rdma-managed-validation-cc \
  --wait \
  --timeout 3600 \
  --poll-interval 15 \
  --yes \
  --format json
```

Captured progress and sanitized result:

```text
Waiting: rdma-managed-validation-cc: compute_cluster=ACTIVE
Waiting: rdma-managed-validation: desired=1 oci_active=1 k8s_ready=0 gpu_ready=0 rdma_ready=0
Waiting: rdma-managed-validation: desired=1 oci_active=1 k8s_ready=1 gpu_ready=0 rdma_ready=1
```

```json
[
  {
    "compute_cluster_created": true,
    "compute_cluster_id": "<compute-cluster-ocid>",
    "k8s_ready": 1,
    "kind": "node-pool",
    "name": "rdma-managed-validation",
    "node_pool_id": "<managed-node-pool-ocid>",
    "oci_active": 1,
    "placement": "compute-cluster",
    "shape": "BM.GPU4.8",
    "source_pool": "oke-gpu",
    "status": "ready",
    "target_size": 1,
    "type": "rdma",
    "work_request_id": "<oke-work-request-ocid>"
  }
]
```

### Inventory And Health Output

```bash
mgmt-oke pools get rdma-managed-validation --format json
```

```json
[
  {
    "compute_cluster_id": "<compute-cluster-ocid>",
    "desired": 1,
    "gpu": "nvidia.com/gpu",
    "host_group_ids": [],
    "k8s_ready": 1,
    "kind": "node-pool",
    "name": "rdma-managed-validation",
    "oci_active": 1,
    "placement": "compute-cluster",
    "rdma": true,
    "shape": "BM.GPU4.8"
  }
]
```

```bash
mgmt-oke nodes list --pool rdma-managed-validation --format json
```

```json
[
  {
    "gpu": {"nvidia.com/gpu": "8"},
    "name": "rdma-managed-node-1",
    "pool": "rdma-managed-validation",
    "rdma": true,
    "ready": true,
    "schedulable": true,
    "shape": "BM.GPU4.8",
    "status": "Ready",
    "workload_pods": 0
  }
]
```

```bash
mgmt-oke topology list --pool rdma-managed-validation --format json
```

```json
[
  {
    "hpc_island": "<hpc-island>",
    "local_block": "<local-block>",
    "network_block": "<network-block>",
    "node_names": ["rdma-managed-node-1"],
    "nodes": 1,
    "ready": 1,
    "shapes": ["BM.GPU4.8"]
  }
]
```

```bash
mgmt-oke health run --type pool --pool rdma-managed-validation --format json
mgmt-oke health run --type gpu --pool rdma-managed-validation --format json
mgmt-oke health run --type rdma --pool rdma-managed-validation --format json
```

```json
[
  {
    "check": "pool-convergence",
    "message": "desired=1, oci_active=1, k8s_ready=1",
    "scope": "rdma-managed-validation",
    "status": "PASS"
  }
]
```

```json
[
  {
    "check": "gpu-allocatable",
    "message": "nvidia.com/gpu=8",
    "scope": "rdma-managed-node-1",
    "status": "PASS"
  }
]
```

```json
[
  {
    "check": "rdma-topology",
    "message": "Required OCI RDMA topology labels are valid.",
    "scope": "rdma-managed-node-1",
    "status": "PASS"
  }
]
```

```bash
mgmt-oke addons validate \
  --target gpu \
  --pool rdma-managed-validation \
  --format json
mgmt-oke addons validate \
  --target rdma \
  --pool rdma-managed-validation \
  --format json
```

```json
[
  {
    "check": "addon-gpu-operator",
    "message": "OKE add-on is active at version v25.10.1.",
    "scope": "NvidiaGpuOperator",
    "status": "PASS"
  },
  {
    "check": "addon-node-feature-discovery",
    "message": "OKE add-on is active at version v0.17.3-1.",
    "scope": "NodeFeatureDiscovery",
    "status": "PASS"
  },
  {
    "check": "gpu-allocatable",
    "message": "nvidia.com/gpu=8",
    "scope": "rdma-managed-node-1",
    "status": "PASS"
  }
]
```

```json
[
  {
    "check": "addon-network-operator",
    "message": "Optional OKE add-on is not enabled; host-network RDMA remains supported.",
    "scope": "NvidiaNetworkOperator",
    "status": "INFO"
  },
  {
    "check": "addon-node-feature-discovery",
    "message": "OKE add-on is active at version v0.17.3-1.",
    "scope": "NodeFeatureDiscovery",
    "status": "PASS"
  },
  {
    "check": "rdma-topology",
    "message": "Required OCI RDMA topology labels are valid.",
    "scope": "rdma-managed-node-1",
    "status": "PASS"
  }
]
```

A Compute instance list filtered by the dedicated Compute Cluster OCID returned
the same running `BM.GPU4.8` worker, providing direct placement membership
evidence in addition to the OKE node-pool configuration.

### Cleanup Output

The pool was workload-free. The deletion preview identified one managed worker
and `placement=compute-cluster`. The reviewed deletion then returned:

```bash
mgmt-oke pools delete rdma-managed-validation \
  --wait \
  --timeout 1800 \
  --poll-interval 15 \
  --yes \
  --format json
```

```json
[
  {
    "kind": "node-pool",
    "name": "rdma-managed-validation",
    "old_size": 1,
    "placement": "compute-cluster",
    "status": "deleted",
    "target_size": 0,
    "work_request_id": "<oke-work-request-ocid>"
  }
]
```

The empty dedicated Compute Cluster was deleted separately and reached
`DELETED`. Compute Host Group placement is covered by request-model,
validation, discovery, CLI, and workflow tests; the live compartment contained
no Compute Host Group for a placement mutation.

## Self-Managed RDMA Pool

### Preview Inherited Configuration

For a legacy RDMA pool, `--from-pool` reads the source Cluster Network's
Instance Configuration. The new pool inherits the source image, subnet, network
settings, cloud-init, and storage bootstrap unless a dedicated option overrides
them:

```bash
mgmt-oke pools create rdma-validation \
  --type rdma \
  --count 1 \
  --from-pool oke-rdma \
  --dry-run \
  --format json
```

Example output excerpt:

```json
[
  {
    "current_size": 0,
    "details": {
      "effective": {
        "backend": "cluster-network",
        "count": 1,
        "name": "rdma-validation",
        "placement": "PACKED_DISTRIBUTION_MULTI_BLOCK",
        "shape": "BM.GPU4.8",
        "storage": {
          "mode": "inherit"
        }
      },
      "requested": {
        "storage_mode": "inherit",
        "type": "rdma"
      },
      "source_pool": "oke-rdma"
    },
    "operation": "pool-create",
    "owner": "compute-management",
    "pool": "rdma-validation",
    "status": "planned",
    "steps": [
      "derive a new Instance Configuration from oke-rdma",
      "apply requested launch, network, boot, Kubernetes, and storage overrides",
      "retarget worker identity to rdma-validation",
      "create a Cluster Network with an embedded Instance Pool",
      "allow inherited OKE bootstrap to register the workers"
    ],
    "target_size": 1
  }
]
```

### Override Placement, Shape, And Name

The same source can be reused while explicitly selecting the new pool name,
availability domain, and shape:

```bash
mgmt-oke pools create rdma-ad-validation \
  --type rdma \
  --count 1 \
  --from-pool oke-rdma \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --dry-run \
  --format json
```

`--storage-mode inherit` is the default. It preserves source cloud-init,
including source FSS, Lustre, or NVMe RAID bootstrap. Use `append` to retain the
inherited script and add requested storage actions, or `replace` to rebuild the
storage portion from explicitly supplied options.

### Live Submission And Capacity Result

The live validation also submitted an RDMA creation request:

```bash
mgmt-oke pools create rdma-validation \
  --type rdma \
  --count 1 \
  --from-pool oke-rdma \
  --wait \
  --timeout 1800 \
  --poll-interval 20 \
  --yes \
  --format json
```

Example failure output:

```text
Error: OCI work request CreateClusterNetworkReservation failed:
Insufficient capacity for cluster network. Created Cluster Network
<cluster-network-ocid> and derived Instance Configuration
<instance-configuration-ocid> may require cleanup.
```

This was an OCI capacity outcome, not a client-side validation or request-shape
failure. The Cluster Network reached `TERMINATED`. The command reported both
created resource identifiers so the derived Instance Configuration could be
reviewed and removed. The existing `oke-rdma` source pool was not modified.

## Negative Validation

Live dry-run validation also confirmed that the CLI rejected:

- a CPU pool using a GPU shape
- an RDMA pool using a VM GPU shape
- NVMe RAID on a shape without compatible local NVMe devices
- a requested CNI configuration that did not match the inherited OKE bootstrap

These failures occurred before OCI mutation.

## Verify A Successful Pool

```bash
mgmt-oke pools get gpu-validation
mgmt-oke nodes list --pool gpu-validation
mgmt-oke health run --type pool --pool gpu-validation
```

For a successfully provisioned RDMA pool, also run:

```bash
mgmt-oke topology list --pool <rdma-pool>
mgmt-oke health run --type rdma --pool <rdma-pool>
mgmt-oke addons validate --target rdma --pool <rdma-pool>
```

See [Creating Worker Pools](./creating-worker-pools.md) for the complete option
set and [Worker Bootstrap and Storage](./worker-bootstrap-and-storage.md) for
storage inheritance and composition behavior.
