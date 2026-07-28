# Creating Worker Pools

`mgmt-oke pools create` creates one of three worker-pool types from a proven
pool already registered with the selected OKE cluster:

| `--type` | New resource | Required source |
| --- | --- | --- |
| `cpu` | Managed OKE node pool | Managed non-GPU, non-RDMA node pool |
| `gpu` | Managed OKE node pool | Managed GPU node pool without RDMA placement |
| `rdma` | Self-managed OCI Cluster Network with one embedded Instance Pool | Existing Cluster Network-backed RDMA pool |

The command does not create or delete the OKE control plane. It also does not
create a managed Compute Cluster-backed RDMA pool; `--type rdma` deliberately
uses the self-managed Cluster Network model.

## Source Templates

A source pool is required because OCI HPC OKE workers carry cluster-specific
certificate, API endpoint, CNI, OKE bootstrap, agent, and network settings.
`mgmt-oke` inherits those working settings and applies only the requested
overrides.

Select the source explicitly with `--from-pool`. When omitted, the command uses
the matching conventional pool name (`oke-cpu`, `oke-gpu`, or `oke-rdma`) when
present. If there is no conventional pool, exactly one eligible source must
exist. Ambiguous or incompatible sources are rejected.

## Managed CPU Pool

Preview a Flex CPU pool:

```bash
mgmt-oke pools create cpu-batch \
  --type cpu \
  --count 2 \
  --from-pool oke-cpu \
  --availability-domain <availability-domain> \
  --shape VM.Standard.E5.Flex \
  --ocpus 16 \
  --memory-in-gbs 128 \
  --subnet-id <worker-subnet-ocid> \
  --dry-run \
  --format json
```

After reviewing the effective request:

```bash
mgmt-oke pools create cpu-batch \
  --type cpu \
  --count 2 \
  --from-pool oke-cpu \
  --availability-domain <availability-domain> \
  --shape VM.Standard.E5.Flex \
  --ocpus 16 \
  --memory-in-gbs 128 \
  --subnet-id <worker-subnet-ocid> \
  --wait
```

The command submits OKE `CreateNodePool`. OKE remains the owner of provisioning,
registration, resize, and specific-node deletion.

## Managed GPU Pool

Every pool type accepts an image override. The image must advertise
compatibility with the selected shape in each selected availability domain.

```bash
mgmt-oke pools create gpu-training \
  --type gpu \
  --count 2 \
  --from-pool oke-gpu \
  --availability-domain <availability-domain> \
  --shape VM.GPU.A10.1 \
  --image-id <custom-image-ocid> \
  --subnet-id <worker-subnet-ocid> \
  --node-nsg-id <worker-nsg-ocid> \
  --boot-volume-size 500 \
  --dry-run \
  --format json
```

The source pool's OKE bootstrap, CNI, pod networking, GPU labels, cycling
settings, and eviction settings are retained unless their dedicated options
override them.

## Self-Managed RDMA Pool

Preview a Cluster Network pool in a selected availability domain:

```bash
mgmt-oke pools create rdma-training \
  --type rdma \
  --count 2 \
  --from-pool oke-rdma \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --image-id <custom-image-ocid> \
  --subnet-id <worker-subnet-ocid> \
  --placement-constraint SINGLE_TIER \
  --boot-volume-size 500 \
  --boot-volume-vpus-per-gb 20 \
  --dry-run \
  --format json
```

The RDMA workflow:

1. reads the source Cluster Network and embedded Instance Pool
2. reads and validates the source Instance Configuration
3. deep-copies all launch, VNIC, secondary VNIC, agent, block-volume, and OKE
   bootstrap settings
4. applies requested image, shape, placement, network, boot, metadata, and
   cloud-init overrides
5. retargets instance, VNIC, and Kubernetes pool identity
6. creates a new Instance Configuration
7. creates a Cluster Network with one embedded Instance Pool
8. waits for OCI, Kubernetes, GPU, RDMA topology, and applicable Network
   Operator virtual-function readiness when `--wait` is selected

The source Instance Configuration and source Cluster Network are not changed.
If Cluster Network creation fails after Instance Configuration creation, the
error identifies the derived configuration for inspection and cleanup.

## Customization

Use `mgmt-oke pools create --help` for the installed version's authoritative
option list. Supported overrides include:

- availability domain, shape, image, worker subnet, pod subnet, and NSGs
- boot-volume size, performance, and Vault key
- Flex OCPUs and memory
- Kubernetes version, maximum pods, node labels, metadata, and freeform tags
- capacity reservation, fault domains, CNI validation, and RDMA placement
- public IP, in-transit encryption, and legacy IMDS behavior
- managed OKE node cycling and eviction settings
- SSH public key, extra cloud-init, pre/post OKE scripts, and kubelet arguments
- official OCI HPC OKE NVMe RAID, FSS mount, and Lustre mount bootstrap

Unspecified values are inherited from the source template. Dedicated options
take precedence over inherited values. Reserved OKE bootstrap metadata cannot
be replaced through generic `--node-metadata`.

## Validation

Dry-run performs live, read-only validation before producing a plan:

- complete OCI pool discovery and unique pool name
- source type and lifecycle
- image and shape availability in the selected availability domain
- CPU versus GPU versus RDMA shape compatibility
- local-disk availability when NVMe RAID is requested
- worker, pod, and NSG resources in the source VCN
- availability-domain-scoped subnet compatibility
- CNI compatibility with the source OKE bootstrap
- required OKE cloud-init and registration metadata
- backend-specific option compatibility

`--dry-run` makes no OCI or Kubernetes mutation. The plan contains separate
`requested` and `effective` sections so inherited values are visible before
approval.

### Example Dry-Run Output

The following JSON is derived from a live managed CPU creation dry-run;
resource identifiers are replaced with stable examples:

```json
[
  {
    "current_size": 0,
    "decrement_size": null,
    "details": {
      "effective": {
        "availability_domains": ["example-ad-3"],
        "backend": "oke-node-pool",
        "boot_volume_size_in_gbs": 256,
        "cni_type": "OCI_VCN_IP_NATIVE",
        "count": 1,
        "image_id": "ocid1.image.example",
        "kubernetes_version": "v1.35.2",
        "memory_in_gbs": 32.0,
        "name": "cpu-batch",
        "ocpus": 6.0,
        "primary_subnet_ids": ["ocid1.subnet.example"],
        "shape": "VM.Standard.E5.Flex",
        "storage": {
          "fss_mounts": [],
          "lustre_mounts": [],
          "mode": "inherit",
          "nvme_raid": null
        }
      },
      "requested": {
        "storage_mode": "inherit",
        "type": "cpu"
      },
      "source_pool": "oke-cpu"
    },
    "operation": "pool-create",
    "owner": "oke",
    "pool": "cpu-batch",
    "status": "planned",
    "target": "cpu-batch",
    "target_size": 1,
    "workload_pods": 0
  }
]
```

## Infrastructure Ownership

Pool creation is a direct OCI mutation. It does not update Terraform or OCI
Resource Manager state.

For managed CPU and GPU pools, declare or import the new OKE node pool before a
later stack apply. For RDMA pools, declare or import the derived Instance
Configuration, Cluster Network, and embedded Instance Pool. Keep the live and
declared configurations aligned before the next apply.

## Verify

```bash
mgmt-oke pools get <new-pool>
mgmt-oke nodes list --pool <new-pool>
mgmt-oke health run --type pool --pool <new-pool>
```

For RDMA:

```bash
mgmt-oke topology list --pool <new-pool>
mgmt-oke health run --type rdma --pool <new-pool>
mgmt-oke addons validate --target rdma --pool <new-pool>
```
