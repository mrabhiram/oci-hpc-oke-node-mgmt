# Creating Worker Pools

`mgmt-oke pools create` creates workers from a proven pool already registered
with the selected OKE cluster:

| Type and mode | New resource | Preferred source |
| --- | --- | --- |
| `--type cpu` | Managed OKE node pool | Managed non-GPU pool |
| `--type gpu` | Managed OKE node pool | Managed non-RDMA GPU pool |
| `--type rdma --rdma-mode compute-cluster` | Managed OKE node pool in a Compute Cluster | Existing managed Compute Cluster RDMA pool, then managed GPU pool |
| `--type rdma` | Legacy self-managed Cluster Network with one embedded Instance Pool | Existing Cluster Network RDMA pool |

The command does not create or delete the OKE control plane. Legacy Cluster
Network mode remains the default for `--type rdma` so existing scripts do not
change behavior.

## Source Templates

A source pool is required because OCI HPC OKE workers carry cluster-specific
certificate, API endpoint, CNI, OKE bootstrap, agent, and network settings.
`mgmt-oke` inherits those working settings and applies only the requested
overrides. This includes the complete source cloud-init and any existing NVMe,
FSS, or Lustre bootstrap.

Select the source explicitly with `--from-pool`. When omitted, the command uses
the matching conventional pool name (`oke-cpu`, `oke-gpu`, or `oke-rdma`) when
present. If there is no conventional pool, exactly one eligible source must
exist. Ambiguous or incompatible sources are rejected.

For managed Compute Cluster RDMA, an existing managed RDMA source is preferred.
When creating the first such pool, a regular managed GPU source such as
`oke-gpu` is eligible; specify an RDMA-capable shape and, when needed, a
compatible image override.

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
registration, resize, specific-node deletion, and whole-pool deletion.

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

## Managed Compute Cluster RDMA Pool

Preview a managed RDMA pool with a dedicated Compute Cluster:

```bash
mgmt-oke pools create rdma-managed \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool oke-gpu \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --compute-cluster-name rdma-managed-cc \
  --dry-run \
  --format json
```

Apply the reviewed request by replacing `--dry-run --format json` with
`--wait`. When `--compute-cluster-id` is omitted, `mgmt-oke` creates an empty
dedicated Compute Cluster, waits for `ACTIVE`, and then submits OKE
`CreateNodePool` with its OCID. Use
`--compute-cluster-compartment-id <ocid>` to create that placement resource in
another accessible compartment.

Use an existing Compute Cluster instead:

```bash
mgmt-oke pools create rdma-managed \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool <managed-gpu-or-rdma-source> \
  --availability-domain <availability-domain> \
  --shape <rdma-capable-bare-metal-gpu-shape> \
  --compute-cluster-id <compute-cluster-ocid> \
  --wait
```

An existing Compute Host Group can constrain the managed placement:

```bash
mgmt-oke pools create rdma-managed \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool <managed-gpu-or-rdma-source> \
  --availability-domain <availability-domain> \
  --shape <rdma-capable-bare-metal-gpu-shape> \
  --compute-cluster-id <compute-cluster-ocid> \
  --host-group-id <compute-host-group-ocid> \
  --wait
```

The placement preflight requires:

- an enhanced OKE cluster
- exactly one placement row and availability domain
- no fault domains or capacity reservation for Compute Cluster placement
- an `ACTIVE` Compute Cluster in the selected availability domain
- an `ACTIVE` Host Group, when supplied, in the same availability domain
- a Host Group `VALID` target matching the worker shape or an OCI platform name
- a shape/image combination that advertises RDMA ports in the selected AD
- OKE node-pool resource-principal permissions
  `COMPUTE_CLUSTER_LAUNCH_INSTANCE` and, when applicable,
  `HOST_GROUP_LAUNCH_INSTANCE`

The tool creates Compute Clusters but not Compute Host Groups or hosts. If the
node-pool request fails or has an uncertain outcome after dedicated Compute
Cluster creation, the error reports and retains both resources for inspection.
Deleting the managed node pool does not delete its Compute Cluster or Host
Group; remove a tool-created dedicated Compute Cluster separately after all
instances and node pools that use it are gone.

Oracle documents the OKE prerequisites and placement contract in
[Using Compute Clusters with Managed Nodes](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengusingcomputeclusters.htm)
and [Using Compute Host Groups with Managed Nodes](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengusinghostgroups.htm).

## Legacy Self-Managed RDMA Pool

To change only the name, AD, shape, and size while inheriting every other
setting from the existing RDMA pool:

```bash
mgmt-oke pools create <new-rdma-pool> \
  --type rdma \
  --count 1 \
  --from-pool oke-rdma \
  --availability-domain <different-availability-domain> \
  --shape <rdma-capable-shape> \
  --wait
```

`--storage-mode inherit` is the default and does not need to be specified.

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
error identifies both created resources and the derived configuration for
inspection and cleanup.

## Customization

Use `mgmt-oke pools create --help` for the installed version's authoritative
option list. Supported overrides include:

- availability domain, shape, image, worker subnet, pod subnet, and NSGs
- RDMA backend, existing or dedicated Compute Cluster, and existing Host Group
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
- canonical or display-form availability-domain resolution
- CPU versus GPU versus RDMA shape compatibility
- Compute Cluster state, single-AD placement, no fault domains, and RDMA ports
- Compute Host Group state, AD, and shape or platform compatibility
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

For managed CPU, GPU, and Compute Cluster RDMA pools, declare or import the new
OKE node pool before a later stack apply. Also declare or import a dedicated
Compute Cluster created by the tool. For legacy RDMA pools, declare or import
the derived Instance Configuration, Cluster Network, and embedded Instance
Pool. Keep the live and declared configurations aligned before the next apply.

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

See [Live Worker Pool Creation Validation](./live-pool-creation-validation.md)
for sanitized output from managed GPU, managed Compute Cluster RDMA, and legacy
Cluster Network validation.

## Delete a Complete Pool

Preview deletion before draining or terminating anything:

```bash
mgmt-oke pools delete <pool> --dry-run --format json
```

Apply after reviewing the node membership and workload count:

```bash
mgmt-oke pools delete <pool> --wait
```

Managed pools are deleted through OKE. Cluster Network pools are terminated
through Compute Management. Standalone Instance Pools use their owning Compute
Management API. The command drains by default, refuses autoscaler-owned and
Slinky-managed pools, and protects `oke-system` unless the dedicated override
is supplied. With `--wait`, a Cluster Network created by `mgmt-oke` also has
its derived Instance Configuration removed after termination. The ownership
tag is revalidated immediately before deletion; stack-owned configurations are
preserved. `--no-wait` retains and reports the derived configuration.

See [Live Worker Pool Deletion Validation](./live-pool-deletion-validation.md)
for managed deletion output, the self-managed RDMA termination plan, and the
system-pool protection result captured from live commands.
