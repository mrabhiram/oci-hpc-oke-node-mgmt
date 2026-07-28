# Live Worker Pool Creation Validation

This report shows representative `mgmt-oke 0.7.0` commands and output captured
from live worker-pool creation validation against an OCI HPC OKE cluster in
`uk-london-1`.

The validation cluster contained:

- a managed OKE GPU pool using `VM.GPU.A10.1`
- a self-managed RDMA Cluster Network pool using `BM.GPU4.8`
- working OKE bootstrap metadata on both source pools

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
