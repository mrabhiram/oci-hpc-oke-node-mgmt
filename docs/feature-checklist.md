# Feature Checklist

Baseline release: `mgmt-oke 0.10.0`

This page is the review checklist for implemented capabilities, validation
coverage, and proposed work. Feature status and validation evidence are kept
separate:

- `[x]` means the production capability is implemented.
- `[ ]` means the capability is not implemented.
- An evidence label describes the strongest completed validation; it does not
  change implementation status.

An implemented feature remains checked when its execution path is automated or
live-plan validated but does not yet have a publishable live mutation record.

## Validation Levels

- `LIVE-MUTATION`: completed against a live OKE cluster and verified after the
  mutation converged
- `LIVE-READ`: exercised against a live OKE cluster without changing resources
- `LIVE-PLAN`: exercised against live OCI and Kubernetes APIs with `--dry-run`
- `AUTOMATED`: covered by automated tests using controlled API responses
- `NOT-IMPLEMENTED`: intentionally absent or proposed for future work

## Installation And Target Discovery

- [x] Installable Python package with `mgmt-oke`, `kubectl-oke`, and
  `kubectl oke` entry points. `AUTOMATED`
- [x] Click-based command tree, command help, version output, and shell-friendly
  exit codes. `AUTOMATED`
- [x] Automatic OKE cluster OCID and region discovery from the current or only
  unambiguous kubeconfig context. `LIVE-READ`
- [x] Automatic compartment discovery through OKE `GetCluster`. `LIVE-READ`
- [x] Explicit cluster, compartment, region, kubeconfig, and context overrides
  for nonstandard environments. `AUTOMATED`
- [x] Instance-principal authentication, including propagation to the OCI CLI
  kubeconfig exec plugin. `LIVE-READ`
- [x] Config-file, resource-principal, OCI-disabled, in-cluster, and multi-file
  kubeconfig modes. `AUTOMATED`
- [x] Kubernetes-only discovery and degraded output when an optional discovery
  source is unavailable. `AUTOMATED`

## Cluster, Pool, And Node Inventory

- [x] Compact cluster status and full OCI/Kubernetes reconciliation.
  `LIVE-READ`
- [x] Discovery of managed OKE node pools, including standard and Compute
  Cluster placement. `LIVE-READ`
- [x] Discovery of legacy Cluster Network worker backends. `LIVE-READ`
- [x] Discovery of standalone Instance Pool worker backends. `AUTOMATED`
- [x] Discovery models for Compute Host Group and GPU Memory Cluster ownership.
  `AUTOMATED`
- [x] Suppression of OKE-internal backing Instance Pools from user-facing pool
  inventory. `LIVE-READ`
- [x] Kubernetes node correlation to OKE nodes and OCI instances where the
  ownership data is available. `LIVE-READ`
- [x] Node lookup by Kubernetes name, Slinky name, internal IP, provider ID, or
  instance OCID. `LIVE-READ`
- [x] Pool, Ready state, workload, RDMA, and exact-field node filters.
  `LIVE-READ`
- [x] Selectable columns, sorting, header suppression, and one-line output.
  `LIVE-READ`
- [x] Human-readable table output and machine-readable JSON output.
  `LIVE-READ`
- [x] CSV output and stable `v1` row schemas. `AUTOMATED`
- [x] RDMA topology inventory and placement-aware pool summaries. `LIVE-READ`

## Worker Pool Creation

- [x] Managed CPU pool creation derived from a compatible stack-managed source
  pool. `LIVE-PLAN`
- [x] Managed GPU pool creation and post-create convergence checks.
  `LIVE-MUTATION`
- [x] Managed RDMA pool creation in an automatically created Compute Cluster.
  `LIVE-MUTATION`
- [x] Managed RDMA pool creation in an existing Compute Cluster. `AUTOMATED`
- [x] Managed placement in an existing Compute Host Group, with AD, lifecycle,
  shape, and platform validation. `AUTOMATED`
- [x] Dual-source managed RDMA creation: OKE-owned configuration from a managed
  source plus worker bootstrap from a legacy Cluster Network source.
  `LIVE-MUTATION`
- [x] Legacy self-managed RDMA Cluster Network creation from an existing stack
  pool. `LIVE-MUTATION`
- [x] Custom pool name, size, AD, shape, image, subnet, NSG, boot volume, Flex
  shape, CNI, tag, metadata, and Kubernetes settings. `LIVE-PLAN`
- [x] Managed pool creation with OCI node-cycling and eviction-setting
  overrides. `AUTOMATED`
- [x] Inherited cloud-init and NVMe RAID bootstrap on managed Compute Cluster
  workers. `LIVE-MUTATION`
- [x] Composed pre/post-bootstrap scripts, kubelet arguments, NVMe RAID,
  existing FSS mounts, and existing Lustre mounts. `LIVE-PLAN`
- [x] Infrastructure-as-code drift warnings for resources created directly by
  the CLI. `LIVE-PLAN`

## Worker Pool Capacity And Deletion

- [x] Exact-size and signed-delta resize for managed OKE node pools.
  `LIVE-MUTATION`
- [x] Resize routing for Compute Cluster-backed managed pools. `AUTOMATED`
- [x] Resize routing for legacy Cluster Network backends. `LIVE-MUTATION`
- [x] Resize routing for standalone Instance Pool backends. `AUTOMATED`
- [x] Explicit `pools add --count` and `pools remove --count` interfaces.
  `AUTOMATED`
- [x] Cluster Autoscaler ownership refusal before direct resize. `AUTOMATED`
- [x] Managed standard and Compute Cluster node-pool deletion.
  `LIVE-MUTATION`
- [x] Legacy Cluster Network deletion planning and stack-owned configuration
  protection. `LIVE-PLAN`
- [x] Standalone Instance Pool deletion and ownership-checked cleanup of a
  derived Instance Configuration. `AUTOMATED`
- [x] System-pool protection and explicit override. `LIVE-PLAN`
- [x] Slurm-style `clusters list`, `clusters create`, `clusters add node`, and
  `clusters delete` aliases for worker-pool operations. `AUTOMATED`

## Node Lifecycle

- [x] Node inventory with GPU resources, RDMA state, pod counts, and Slinky
  aliases. `LIVE-READ`
- [x] Explicit node cordon, PDB-aware drain, and uncordon commands.
  `AUTOMATED`
- [x] Workload and dry-run eviction preflight before destructive operations.
  `LIVE-PLAN`
- [x] Specific managed-node termination with default pool decrement or
  `--keep-size` replacement. `LIVE-MUTATION`
- [x] Managed Compute Cluster and self-managed Instance Pool termination
  routing. `AUTOMATED`
- [x] Unhealthy-host tagging plan using
  `ComputeInstanceHostActions.CustomerReportedHostStatus=unhealthy`.
  `LIVE-PLAN`
- [x] ETag-protected tag update, tag read-back, and fail-closed ordering before
  termination. `AUTOMATED`
- [x] Individual managed or self-managed worker boot volume replacement while
  preserving instance identity and image. `AUTOMATED`
- [x] Managed pool-wide boot volume replacement with image, boot volume,
  Kubernetes version, metadata, and SSH-key updates. `AUTOMATED`

## GPU, RDMA, Add-Ons, And Schedulers

- [x] GPU allocatable-resource validation. `LIVE-READ`
- [x] Strict RDMA topology validation. `LIVE-READ`
- [x] OKE add-on status and target validation for Node Feature Discovery, GPU
  Operator, and Network Operator. `LIVE-READ`
- [x] Network Operator `nvidia.com/rdma-vf` enforcement when the add-on is
  active. `AUTOMATED`
- [x] Cluster Autoscaler ownership discovery. `LIVE-READ`
- [x] Kueue ResourceFlavor and ClusterQueue discovery and pool matching.
  `LIVE-READ`
- [x] Slinky worker aliases and fail-closed destructive-operation protection.
  `AUTOMATED`
- [x] Deterministic pool, GPU, RDMA, add-on, and scheduler health checks.
  `LIVE-READ`
- [x] Actionable recommendations derived from discovery and health results.
  `LIVE-READ`

## Kubernetes Upgrades

- [x] Upgrade status, advertised target discovery, exact target resolution, and
  ordered planning. `LIVE-READ`
- [x] Preview gating, downgrade refusal, one-minor sequencing, version skew,
  add-on compatibility, and workload validation. `LIVE-PLAN`
- [x] One-step OKE control-plane upgrade execution and convergence workflow.
  `AUTOMATED`
- [x] Managed standard and Compute Cluster pool upgrades using boot volume
  replacement, instance replacement, or blue-green strategies. `AUTOMATED`
- [x] Legacy Cluster Network bootstrap transformation for a target Kubernetes
  version. `LIVE-PLAN`
- [x] Standalone Instance Pool and GPU Memory Cluster upgrade backends.
  `AUTOMATED`
- [x] Kueue and Slinky read-only workload gates. `LIVE-PLAN`
- [x] Checkpointed full-cluster apply, resume, abandon, cleanup, ETag checks,
  resource-version checks, and work-request recovery. `AUTOMATED`
- [x] Upgrade subsystem prohibition on cordon, drain, eviction, and uncordon
  mutations. `AUTOMATED`

## Mutation Safety

- [x] Validated dry-run plans before lifecycle and initial upgrade mutations.
  `LIVE-PLAN`
- [x] Explicit typed or flag-based confirmation for new mutation plans,
  independent of upgrade safety acknowledgements. `LIVE-MUTATION`
- [x] Per-cluster Kubernetes Lease for mutation serialization. `AUTOMATED`
- [x] OCI ETag and Kubernetes resource-version conflict detection where the
  backing API exposes them. `AUTOMATED`
- [x] OCI work-request monitoring and layered OCI, Kubernetes, GPU, and RDMA
  convergence checks. `LIVE-MUTATION`
- [x] Failure reporting that retains uncertain resources for inspection rather
  than claiming rollback. `LIVE-MUTATION`
- [x] Terraform and OCI Resource Manager drift warnings without editing source
  configuration. `LIVE-PLAN`

## Proposed

- [ ] Synchronize Kueue ClusterQueue quotas after pool capacity changes.
  `NOT-IMPLEMENTED`
- [ ] Update Cluster Autoscaler minimum and maximum bounds. `NOT-IMPLEMENTED`
- [ ] Install, update, or remove OKE add-ons. `NOT-IMPLEMENTED`
- [ ] Mutate Slurm node or partition state. `NOT-IMPLEMENTED`
- [ ] Automatically cordon, drain, evict, or change Kueue or Slurm policy during
  Kubernetes upgrades. `NOT-IMPLEMENTED`
- [ ] Update Terraform or OCI Resource Manager source after direct mutations.
  `NOT-IMPLEMENTED`
- [ ] Automatically remove source pools after blue-green migration.
  `NOT-IMPLEMENTED`
- [ ] Create standalone Instance Pools that are not part of a Cluster Network.
  `NOT-IMPLEMENTED`
- [ ] Create Compute Host Groups or attach hosts to them. Existing Host Group
  placement for managed OKE pools is implemented. `NOT-IMPLEMENTED`
- [ ] Create or delete initial GPU Memory Clusters through general pool
  lifecycle commands. GMC discovery and Kubernetes upgrade backends are
  implemented. `NOT-IMPLEMENTED`
- [ ] Provide a generic node or pool cycle command independent of BVR,
  termination with replacement, and Kubernetes upgrade strategies.
  `NOT-IMPLEMENTED`
- [ ] Automatically delete a dedicated Compute Cluster when its managed OKE
  node pool is deleted. `NOT-IMPLEMENTED`
- [ ] Change the image during individual-node BVR. OKE individual-node BVR
  preserves the existing image; pool-wide BVR supports image changes.
  `NOT-IMPLEMENTED`
- [ ] Provision FSS or Lustre services. Pool creation can mount existing
  endpoints but does not create the storage services. `NOT-IMPLEMENTED`
- [ ] Create or delete the OKE control plane. The `clusters` aliases operate on
  worker pools. `NOT-IMPLEMENTED`

## Validation Coverage

This table records evidence strength for implemented mutation paths. Evidence
below `LIVE-MUTATION` identifies the validation layer; it does not make the
feature incomplete.

| Capability | Implementation | Strongest evidence | Coverage note |
| --- | --- | --- | --- |
| Managed GPU pool creation | Complete | `LIVE-MUTATION` | Recorded in the live pool-creation guide |
| Managed two-worker Compute Cluster RDMA creation, dual-source bootstrap, and NVMe RAID | Complete | `LIVE-MUTATION` | Recorded in the live pool-creation guide |
| Legacy self-managed Cluster Network creation and Kubernetes convergence | Complete | `LIVE-MUTATION` | Confirmed on a live OKE deployment |
| Managed placement in an existing Compute Host Group | Complete | `AUTOMATED` | Request construction, placement validation, discovery, and execution routing covered |
| Existing FSS and Lustre mount composition during pool creation | Complete | `LIVE-PLAN` | Composition with existing endpoints validated; storage service provisioning is out of scope |
| GMC discovery and Kubernetes upgrade mutations | Complete | `AUTOMATED` | Discovery, ETag update, BVR, replacement, and blue-green routing covered |
| Control-plane and worker Kubernetes upgrades | Complete | `LIVE-PLAN` plus `AUTOMATED` | Live target resolution and planning plus mutation, convergence, checkpoint, and recovery coverage |
| Unhealthy-host tag update, read-back, termination, and replacement | Complete | `LIVE-PLAN` plus `AUTOMATED` | Live ownership and safety plan plus tag/read-back/fail-closed execution coverage |
| Individual managed/self-managed BVR and managed pool-wide BVR | Complete | `AUTOMATED` | Submission, wait, identity, boot-volume, GPU, and RDMA verification covered |
| Network Operator RDMA VF readiness validation | Complete | `LIVE-READ` plus `AUTOMATED` | Live add-on discovery plus conditional VF enforcement coverage |

## Command Validation Summary

| Commands | Strongest current evidence |
| --- | --- |
| `status`, `reconcile`, `pools list/get`, `nodes list/get` | `LIVE-READ` |
| `topology list`, `addons status/validate`, `health run`, `recommendations list` | `LIVE-READ` |
| `autoscaler status` | `LIVE-READ` |
| `pools create` | `LIVE-MUTATION` for managed GPU, Compute Cluster RDMA, and legacy Cluster Network RDMA; `LIVE-PLAN` for CPU; `AUTOMATED` for Host Group placement |
| `pools resize/add/remove` | `LIVE-MUTATION`, with alias and backend-specific automated coverage |
| `pools delete` | `LIVE-MUTATION` for managed pools; `LIVE-PLAN` for legacy Cluster Network; `AUTOMATED` for standalone Instance Pool |
| `nodes cordon/drain/uncordon` | `AUTOMATED` |
| `nodes terminate/remove` | `LIVE-MUTATION` for node removal; unhealthy-host tagging is `LIVE-PLAN` plus `AUTOMATED` execution coverage |
| `nodes boot-volume-replace`, `pools boot-volume-replace` | `AUTOMATED` |
| `upgrades status/plan` | `LIVE-READ` and `LIVE-PLAN` |
| `clusters upgrade`, `pools upgrade`, `upgrades apply` | `LIVE-PLAN` with `AUTOMATED` mutation and recovery coverage |
| `upgrades resume/abandon/cleanup` | `AUTOMATED` |
| `clusters list/create/add node/delete` | Same evidence as the underlying pool command |
