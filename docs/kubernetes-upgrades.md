# Kubernetes Upgrades

This guide covers Kubernetes control-plane and worker upgrades for OCI HPC OKE
clusters. The workflow supports managed CPU and GPU node pools, managed RDMA
node pools placed in Compute Clusters, legacy RDMA Cluster Networks, standalone
Instance Pools, virtual node pools, and GPU Memory Clusters (GMCs).

## Safety Model

The upgrade subsystem never calls Kubernetes cordon, drain, eviction, or
uncordon APIs. Operators prepare workloads with their existing Kubernetes,
Kueue, and Slurm procedures. `mgmt-oke` verifies the observed state, requires a
separate workload attestation, mutates the owning OCI service, and waits for
convergence.

`--yes` confirms an OCI mutation only. It never replaces:

- `--ack-application-compatibility`
- `--ack-iac-drift`
- `--ack-workloads-drained`

Without `--ack-workloads-drained`, a single-pool command prompts the operator
to type `DRAINED <pool>`. Full-cluster orchestration is non-interactive at each
pool boundary and therefore requires the flag when execution reaches a worker
pool.

`--emergency-ack-unverified-drain` is accepted only when workload verification
is unavailable because of an API, RBAC, or Slinky exec failure. It cannot
bypass a Ready-state failure, a schedulable node, an active pod, an admitted
Kueue workload, or an active Slurm job.

OKE can still perform its mandatory final cordon and drain while cycling a
managed node pool. That OKE-owned behavior is separate from Kubernetes
scheduling mutations by `mgmt-oke`.

## Version Selection

Use an exact patch or a major/minor target:

```bash
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades status --to v1.36.1
```

A target such as `v1.36` resolves to the latest production patch advertised by
OKE. An exact patch must also be advertised by OKE. Preview `.0` releases are
rejected unless the target is explicitly available and `--allow-preview` is
present.

The tool refuses:

- a downgrade
- a control-plane jump of more than one minor version
- a worker target newer than the current control plane
- an unsupported kubelet/control-plane skew
- an exact target not advertised by OKE

`upgrades apply` can build an ordered sequence of one-minor control-plane steps.
`clusters upgrade` intentionally executes exactly one valid step.

## Inspect Readiness

Run read-only status before selecting a maintenance window:

```bash
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades status --to v1.36 --format json
```

Status includes the control-plane version and available targets, virtual-pool
versions, each pool's declared Kubernetes version, actual node kubelet
versions, scheduler state, available strategies, and target-version add-on
compatibility.

Example live-derived output:

```text
kind             name            declared_version  actual_versions  target_version  state       strategy                 scheduler_state
---------------  --------------  ----------------  ---------------  --------------  ----------  -----------------------  ---------------
control-plane    <cluster-ocid>  v1.35.2           v1.35.2          v1.36.1         ACTIVE      OKE UpdateCluster        n/a
node-pool        oke-cpu         v1.35.2           [v1.35.2]        v1.36.1         1/1 Ready   auto,boot-volume-...    blocked
node-pool        oke-gpu         v1.35.2           [v1.35.2]        v1.36.1         1/1 Ready   auto,boot-volume-...    not-drained
node-pool        oke-system      v1.35.2           [v1.35.2]        v1.36.1         2/2 Ready   auto,boot-volume-...    blocked
cluster-network  oke-rdma        v1.35.2           [v1.35.2]        v1.36.1         2/2 Ready   auto,boot-volume-...    blocked
addon            CoreDNS         v1.12.2-fips-4    v1.12.2-fips-4  v1.36.1         compatible  AUTOMATIC                n/a
addon            KubeProxy       v1.35.2           v1.35.2          v1.36.1         compatible  AUTOMATIC                n/a
addon            NvidiaGpuOperator v25.10.1        v25.10.1         v1.36.1         compatible  PINNED                   n/a
```

The values above came from a fresh London validation cluster and were
sanitized. The minor target resolved from `v1.36` to production `v1.36.1`.

## Generate A Plan

Generate the complete plan without mutation:

```bash
mgmt-oke upgrades plan --to v1.36
mgmt-oke upgrades plan --to v1.36 --format json
```

The default order is:

1. CPU canary pools
2. system pools
3. regular GPU pools
4. managed Compute Cluster RDMA pools
5. legacy Cluster Network RDMA pools
6. GPU Memory Clusters
7. custom pools

Override the complete worker order by repeating `--pool-order` once for every
pool:

```bash
mgmt-oke upgrades plan --to v1.36 \
  --pool-order oke-cpu \
  --pool-order oke-system \
  --pool-order oke-gpu \
  --pool-order oke-rdma
```

Example plan summary from the same live validation:

```text
operation              target      owner            strategy             workloads  scheduler evidence
---------------------  ----------  ---------------  -------------------  ---------  ------------------------------------------
control-plane-upgrade  v1.36.1     oke              OKE UpdateCluster    0          add-ons compatible
worker-pool-upgrade    oke-cpu     node-pool        boot-volume-replace  9          active pods; node not cordoned
worker-pool-upgrade    oke-system  node-pool        boot-volume-replace  12         active pods; node not cordoned
worker-pool-upgrade    oke-gpu     node-pool        boot-volume-replace  0          node not cordoned
worker-pool-upgrade    oke-rdma    cluster-network  instance-replace     1          active pod; Kueue stopPolicy=None
```

Planning structurally cloned the legacy RDMA Instance Configuration, proved
that the current API endpoint and cluster CA were refreshed, and retained all
existing metadata keys. No OCI or Kubernetes resource was changed.

## Worker Strategies

Select one strategy globally or override individual pools:

```bash
mgmt-oke upgrades plan --to v1.36 \
  --strategy auto \
  --pool-strategy oke-gpu=instance-replace \
  --pool-strategy oke-rdma=blue-green
```

| Strategy | Managed OKE pool | Cluster Network, Instance Pool, or GMC |
| --- | --- | --- |
| `auto` | `boot-volume-replace` | `instance-replace` |
| `boot-volume-replace` | OKE `UpdateNodePool` with `BOOT_VOLUME_REPLACE`; default `maximumUnavailable=1` | Sequential instance source and metadata update; preserves instance OCID and networking and retains the previous boot volume |
| `instance-replace` | OKE `UpdateNodePool` with `INSTANCE_REPLACE`; default `maximumUnavailable=0`, `maximumSurge=1` | Attaches the target Instance Configuration, adds and verifies target capacity, then removes one externally drained old instance |
| `blue-green` | Clones the complete OKE node pool | Clones the complete backend and its target Instance Configuration |

Managed Compute Cluster RDMA pools remain OKE-owned and use the managed
strategies. Their internal backing Instance Pools are never direct mutation
targets.

For self-managed backends, the target Instance Configuration preserves FSS,
Lustre, local NVMe RAID, pre/post-bootstrap scripts, kubelet arguments, SSH
configuration, VNICs, RDMA agents, tags, and custom metadata. The current API
endpoint and cluster CA are refreshed, and preparation fails unless the target
Kubernetes version is proven in the resulting bootstrap.

GMC blue-green requires an explicitly usable Compute Cluster or GPU Memory
Fabric when the source placement cannot be shared:

```bash
mgmt-oke pools upgrade <gmc-pool> \
  --to v1.36.1 \
  --strategy blue-green \
  --blue-green-compute-cluster-id <compute-cluster-ocid> \
  --blue-green-gpu-memory-fabric-id <gpu-memory-fabric-ocid> \
  --dry-run
```

## Workload Gate

Before a pool mutation, every target node must be:

- Kubernetes Ready
- externally cordoned
- free of ordinary workload pods

DaemonSet pods, static mirror pods, and recognized scheduler infrastructure are
allowed. Kueue ClusterQueues associated with the pool must use `Hold` or
`HoldAndDrain`, and no admitted workload can remain.

When Slinky ownership is detected, the tool uses read-only exec against the
unique `slurmctld` container. Relevant partitions must be `DOWN` or `INACTIVE`,
target Slurm nodes must be drained/down, and no running, configuring,
completing, suspended, or resizing job can reference those nodes.

After preparing the pool externally, dry-run it again:

```bash
mgmt-oke pools upgrade oke-gpu \
  --to v1.36.1 \
  --strategy boot-volume-replace \
  --dry-run
```

A worker-only dry-run is expected to fail when its target is newer than the
current control plane:

```text
Error: Worker target v1.36.1 cannot be newer than control plane v1.35.2.
```

Use `upgrades apply` to upgrade the control plane first, or execute
`clusters upgrade` and wait for it to complete before running the pool command.

## Upgrade The Control Plane

Preview one valid OKE control-plane step:

```bash
mgmt-oke clusters upgrade --to v1.36 --dry-run
```

Execute it only after the target and maintenance window are approved:

```bash
mgmt-oke clusters upgrade \
  --to v1.36.1 \
  --ack-application-compatibility \
  --ack-iac-drift \
  --yes
```

The command revalidates all worker Ready state, version policy, add-on
compatibility, and the cluster ETag under the mutation Lease. It submits
`UpdateCluster` and always waits for its work request and ACTIVE target-version
convergence. It does not cycle worker pools.

## Upgrade One Pool

After the control plane is at the target and the pool is externally prepared:

```bash
mgmt-oke pools upgrade oke-gpu \
  --to v1.36.1 \
  --strategy boot-volume-replace \
  --ack-application-compatibility \
  --ack-iac-drift \
  --ack-workloads-drained \
  --yes
```

Use `--image-id <image-ocid>` to apply a compatible custom image. Managed image
validation covers shape, availability domain, and Linux distribution. A
self-managed image override is written into the cloned target Instance
Configuration and validated against its launch placement.

Cycling controls accept a count or percentage:

```bash
mgmt-oke pools upgrade oke-gpu \
  --to v1.36.1 \
  --strategy instance-replace \
  --maximum-unavailable 0 \
  --maximum-surge 1 \
  --dry-run
```

## Apply The Complete Upgrade

Run all available validation without changing resources:

```bash
mgmt-oke upgrades apply --to v1.36 --dry-run --format json
```

The live validation produced one control-plane plan and four worker-pool plans.
It did not create the upgrade ConfigMap, acquire a persistent checkpoint, or
submit an OCI request.

Execute only after reviewing the plan and externally preparing every pool:

```bash
mgmt-oke upgrades apply \
  --to v1.36 \
  --ack-application-compatibility \
  --ack-iac-drift \
  --ack-workloads-drained \
  --yes
```

The orchestrator:

1. revalidates the plan under `kube-system/mgmt-oke-mutation`
2. creates a checkpoint
3. upgrades each control-plane minor step and verifies OKE-dependent add-ons
   and virtual pools
4. prepares every worker launch configuration for the target
5. gates and cycles one worker pool at a time
6. verifies kubelet versions, GPU resources, RDMA topology and VFs, add-ons,
   virtual pools, and Slinky registration
7. marks the checkpoint completed

## Checkpoint And Recovery

One non-secret checkpoint is stored in
`kube-system/mgmt-oke-kubernetes-upgrade`. Writes and deletion use the
ConfigMap `resourceVersion`; concurrent changes fail closed. OCI updates use
resource ETags, and deterministic retry tokens protect create operations.

Resume from observed OCI and Kubernetes state:

```bash
mgmt-oke upgrades resume --ack-workloads-drained
```

Work-request IDs, created resource IDs, target Instance Configurations, current
phase, and pool index are recorded before and after transitions. Resume checks
whether each operation already converged before submitting another request.

Blue-green returns exit status `3` and `action-required` after the parallel
backend reaches the target. Migrate workloads, externally drain the retained
source pool, explicitly remove or finalize that source through its reviewed
lifecycle workflow, then resume. The source is not silently deleted.

Abandon the checkpoint without rollback:

```bash
mgmt-oke upgrades abandon --yes
```

Abandoning does not claim to reverse control-plane, pool, boot-volume, or
Instance Configuration changes.

After successful completion, delete only superseded Instance Configurations
created and tagged by this operation, then remove the checkpoint:

```bash
mgmt-oke upgrades cleanup --yes
```

Stack-owned Instance Configurations, retained source pools, and previous boot
volumes are outside cleanup.

## Add-Ons And IaC

The tool queries OKE add-on options for the selected Kubernetes target. A
pinned add-on with no compatible target build blocks the upgrade. Automatic
add-ons are left to OKE and verified after the control plane converges.
`mgmt-oke` never installs, changes, or removes an add-on.

Every direct OCI change can diverge from Terraform or OCI Resource Manager
state. `--ack-iac-drift` confirms that the operator will reconcile the approved
version, image, cycling, and replacement settings into the stack source of
truth.

## Permissions

Upgrade status and planning require read access to the OKE cluster, node pools,
virtual pools, add-ons and add-on options, Compute resources, Compute
Management resources, images, work requests, Kubernetes nodes and pods, Kueue
resources, and Slinky controller pods.

Execution additionally requires the corresponding update, create, delete, and
instance lifecycle permissions. Kubernetes RBAC must permit the mutation Lease
and the checkpoint ConfigMap in `kube-system`. Slinky verification requires
read-only pod exec. No upgrade permission requires pod eviction.

## References

- [Upgrading an OKE control plane](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengupgradingk8smasternode.htm)
- [OKE Kubernetes version policy](https://docs.oracle.com/en-us/iaas/Content/ContEng/Concepts/contengupgradeoverview.htm)
- [OKE add-on options](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/list-add-ons.htm)
- [Updating managed worker nodes](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengupgradingk8sworkernode.htm)
- [Replacing a Cluster Network Instance Configuration](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/update-cluster-network-instance-configuration.htm)
- [Kueue ClusterQueue stop policy](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/)
- [Slurm node and partition controls](https://slurm.schedmd.com/scontrol.html)
