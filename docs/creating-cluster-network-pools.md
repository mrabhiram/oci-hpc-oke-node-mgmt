# Creating Cluster Network Worker Pools

`mgmt-oke pools create` creates a self-managed RDMA worker pool backed by an
OCI Cluster Network and an embedded Instance Pool. It derives a new Instance
Configuration from an existing Cluster Network pool, applies the new pool
identity, and reuses the source placement. The new workers inherit the proven
OKE image and bootstrap configuration without identifying as the source pool.

This command does not create a managed OKE node-pool object or a Compute
Cluster-backed pool.

## Prerequisites

- an existing, running Cluster Network-backed RDMA pool in the selected OKE
  cluster
- OCI permission to read and create Instance Configurations, Cluster Networks,
  and Instance Pools
- Kubernetes access for the mutation Lease and for `--wait` readiness checks
- sufficient service limits and physical capacity for the source shape and
  availability domain

On a stack operator node, cluster, region, and compartment discovery remain
automatic. Configure the authentication method once:

```bash
export OCI_AUTH=instance_principal
```

## Preview Creation

When `oke-rdma` is present, it is the default source:

```bash
mgmt-oke pools create oke-rdma-2 \
  --count 2 \
  --dry-run \
  --format json
```

Select the source explicitly for repeatable automation:

```bash
mgmt-oke pools create oke-rdma-2 \
  --count 2 \
  --from-pool oke-rdma \
  --dry-run \
  --format json
```

Example dry-run output:

```json
[
  {
    "current_size": 0,
    "decrement_size": null,
    "operation": "pool-create",
    "owner": "compute-management",
    "pool": "oke-rdma-2",
    "status": "planned",
    "steps": [
      "derive an Instance Configuration from oke-rdma",
      "retarget instance, VNIC, and Kubernetes node pool identity",
      "create a Cluster Network with one embedded Instance Pool",
      "allow the inherited cloud-init to bootstrap workers into OKE"
    ],
    "target": "oke-rdma-2",
    "target_size": 2,
    "warnings": [
      "A new Instance Configuration is derived from the source; image, cloud-init, and OKE bootstrap settings are preserved while pool identity is updated.",
      "This direct OCI mutation creates resources outside Terraform or OCI Resource Manager state; import or declare the new pool before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Dry-run rejects:

- a pool name that already exists, using case-insensitive comparison
- a missing or non-Cluster Network source
- incomplete OCI pool inventory
- an ambiguous source when neither `oke-rdma` nor a single eligible pool can
  be selected
- a worker count below one
- a name that cannot be used as a Kubernetes label value
- an Instance Configuration without the required OKE bootstrap metadata

## Create the Pool

After reviewing the plan:

```bash
mgmt-oke pools create oke-rdma-2 \
  --count 2 \
  --from-pool oke-rdma \
  --wait
```

The interactive confirmation requires the new pool name. Use `--yes` only for
an intentionally reviewed non-interactive operation:

```bash
mgmt-oke pools create oke-rdma-2 \
  --count 2 \
  --from-pool oke-rdma \
  --wait \
  --yes
```

The workflow:

1. discovers every OCI worker pool and rejects duplicate names
2. resolves the source Cluster Network and embedded Instance Pool
3. acquires the Kubernetes mutation Lease
4. reads the source Cluster Network through Compute Management
5. deep-copies its Instance Configuration launch details
6. preserves the image, cloud-init, OKE join metadata, shape, agent settings,
   boot-volume settings, and networking
7. applies the new pool name to instance tags, VNIC tags, and
   `oke.oraclecloud.com/pool.name`
8. removes source Terraform state and module ownership markers
9. creates the derived Instance Configuration
10. creates a new Cluster Network with one embedded Instance Pool
11. monitors the OCI work request when `--wait` is enabled
12. waits for desired OCI instances, Kubernetes Ready nodes, allocatable GPUs,
   valid RDMA topology, and applicable RDMA virtual functions

The source Instance Configuration is not modified. The source Cluster Network
and its workers are not resized or restarted. If Cluster Network creation fails
after the derived Instance Configuration is created, the command reports that
configuration's OCID so the operator can inspect the partial operation.

## Source Selection

Source selection follows this order:

1. the pool supplied through `--from-pool`
2. a Cluster Network pool named `oke-rdma`
3. the only eligible Cluster Network pool

When multiple eligible pools remain, the command stops and lists them instead
of guessing.

## Infrastructure-As-Code Ownership

Creation through `mgmt-oke` is a direct OCI mutation. It does not update the OCI
HPC OKE Terraform variables or an OCI Resource Manager stack. Import or declare
the derived Instance Configuration, Cluster Network, and Instance Pool before a
later stack apply if that stack must own the new resources.

`pools remove` reduces worker capacity; it does not delete the Cluster Network
resource.

## Verify

```bash
mgmt-oke pools get oke-rdma-2
mgmt-oke nodes list --pool oke-rdma-2
mgmt-oke topology list --pool oke-rdma-2
mgmt-oke health run --type rdma --pool oke-rdma-2
```
