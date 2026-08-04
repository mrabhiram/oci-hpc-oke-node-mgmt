# Scope

This page describes the current implementation scope for the OKE HPC Node
Management Tool.

## Implemented

- read-only discovery
- OCI/Kubernetes node join
- inferred Kubernetes-only pools when OCI is disabled or unavailable
- cluster status plus pool, node, topology, add-on, autoscaler, and reconcile views
- JSON/CSV/table output
- stable `v1` row schemas, documented exit status, and debug tracebacks
- node field selectors, identifier lists, column projection, sorting, header suppression,
  and one-line output
- graceful warnings and degraded health when one discovery source is unavailable
- automatic OKE cluster OCID and region discovery from the selected OCI-generated
  kubeconfig context
- automatic compartment discovery from the resolved cluster through the OCI OKE
  `GetCluster` API
- explicit CLI and environment resource-target overrides with deterministic precedence
- multi-file kubeconfig loading and explicit, current, or unambiguous single-context
  selection
- instance and resource principal propagation to the kubeconfig OCI CLI exec plugin
- consistent process exit status from console-script and Python module entrypoints
- managed OKE Compute Cluster placement and host-group discovery
- suppression of OKE-internal Compute Cluster backing instance pools
- ownership-aware mutation routing between OKE node pools, legacy Cluster
  Networks, and standalone Instance Pools
- read-only OKE add-on lifecycle and installed-version discovery
- strict RDMA topology validation that rejects missing and sentinel IMDS values
- RDMA VF readiness when `NvidiaNetworkOperator` is active
- Slinky Slurm hostname aliases for node lookup and output
- fail-closed Slinky protection for node removal, replacement, and pool scale-down
- guarded managed OKE node pool resize through a size-only `node_config_details` update
- guarded self-managed cluster-network and instance-pool resize
- guarded creation of managed CPU/GPU node pools through OKE `CreateNodePool`
  from matching stack templates
- guarded creation of managed RDMA node pools through OKE `CreateNodePool`,
  using an existing Compute Cluster or an automatically created dedicated
  Compute Cluster
- existing Compute Host Group placement for managed pools, with lifecycle,
  availability-domain, and shape or platform validation
- canonical and display-form availability-domain resolution through OCI
- guarded creation of self-managed RDMA Cluster Network pools by deriving a new
  Instance Configuration from an existing RDMA pool
- custom image, shape, availability domain, subnet, NSG, boot, Flex, Kubernetes,
  metadata, tag, capacity, fault-domain, CNI, encryption, IMDS, and lifecycle
  overrides with effective-request dry-run output
- official OCI HPC OKE worker cloud-init composition for local NVMe RAID and
  existing FSS and Lustre mounts
- guarded whole-pool deletion for managed OKE node pools, self-managed Cluster
  Networks, and standalone Instance Pools, including ownership-checked cleanup
  of derived RDMA Instance Configurations on waited deletion
- Slurm-style `clusters list`, `clusters create`, `clusters delete`, and
  `clusters add node` worker-pool aliases
- explicit `pools add` and `pools remove` capacity commands
- validated dry-run plans for every mutation
- Kubernetes Lease serialization for concurrent mutations
- wait for OCI active count and Kubernetes Ready count after resize
- wait for allocatable GPU, RDMA topology, and applicable RDMA VF readiness
- guarded specific managed OKE node removal/termination through OKE `delete_node`
- guarded specific self-managed node removal/replacement through instance-pool detach and automatic termination
- guarded individual boot volume replacement for managed and self-managed
  workers through OKE `ReplaceBootVolumeClusterNode`, preserving the compute
  instance, network address, image, and existing node configuration
- guarded managed node-pool boot volume replacement through OKE
  `UpdateNodePool` with `BOOT_VOLUME_REPLACE` cycling and supported image, boot
  KMS, boot size, Kubernetes version, metadata, and SSH-key updates
- enhanced-cluster, Linux distribution, image/shape, pool-wide health,
  eviction, instance identity, boot volume identity, GPU, RDMA topology, and
  RDMA VF validation for BVR, while permitting individual repair of a NotReady
  worker
- multi-node termination by identifier or exact field selector
- Kubernetes cordon, uncordon, and PDB-aware drain workflows
- default drain before node termination, with explicit `emptyDir` and unmanaged-pod acknowledgements
- deterministic pool, node, GPU, RDMA, add-on, and scheduler health checks
- actionable recommendations derived from health warnings and failures
- accelerator add-on validation against discovered GPU and RDMA capacity
- OKE control-plane version, advertised target, virtual-pool version, declared
  worker version, actual kubelet version, add-on update mode, Kueue stop policy,
  and GMC ownership discovery
- typed Kubernetes target resolution, including production minor aliases,
  preview acknowledgement, downgrade refusal, one-minor control-plane
  sequencing, and kubelet skew validation
- read-only `upgrades status`, complete ordered `upgrades plan`, and validated
  upgrade `--dry-run` commands
- one-step OKE control-plane upgrades with ETag validation, mandatory
  convergence waiting, add-on compatibility checks, and automatic add-on and
  virtual-pool post-verification
- managed CPU, GPU, and Compute Cluster RDMA worker upgrades through OKE
  `BOOT_VOLUME_REPLACE`, `INSTANCE_REPLACE`, or complete blue-green node-pool
  cloning
- legacy Cluster Network, standalone Instance Pool, and GPU Memory Cluster
  worker upgrades through structurally cloned Instance Configurations,
  sequential identity-preserving BVR, surge-first instance replacement, or
  complete blue-green backend creation
- self-managed bootstrap preservation for FSS, Lustre, NVMe RAID, pre/post
  scripts, kubelet arguments, SSH configuration, networking, RDMA agents, and
  custom metadata, with live API endpoint and cluster CA refresh
- compatible custom worker image overrides across managed and self-managed
  upgrade paths
- read-only workload gates for Kubernetes pods, Kueue ClusterQueue stop policy
  and admitted workloads, and Slinky partition, node, and job state
- strict separation between upgrade execution and Kubernetes cordon, drain,
  eviction, or uncordon APIs
- separate application, IaC-drift, workload-drain, and verification-unavailable
  acknowledgements that are not replaced by `--yes`
- checkpointed `upgrades apply`, observed-state `upgrades resume`, explicit
  no-rollback `upgrades abandon`, and ownership-checked `upgrades cleanup`
- optimistic ConfigMap `resourceVersion`, OCI ETag, deterministic create retry
  token, work-request, and mutation-Lease concurrency controls
- target-version GPU, RDMA topology, Network Operator VF, add-on, virtual-pool,
  and Slinky registration verification
- modular Click command architecture and shell-completion support

## Documentation

- [`README.md`](README.md)
- [`architecture.md`](architecture.md)
- [`controller-install.md`](controller-install.md)
- [`command-reference.md`](command-reference.md)
- [`creating-worker-pools.md`](creating-worker-pools.md)
- [`live-pool-creation-validation.md`](live-pool-creation-validation.md)
- [`live-pool-deletion-validation.md`](live-pool-deletion-validation.md)
- [`replacing-worker-boot-volumes.md`](replacing-worker-boot-volumes.md)
- [`kubernetes-upgrades.md`](kubernetes-upgrades.md)

## Not Implemented Yet

- automatic Kueue `ClusterQueue` quota updates after worker-pool capacity changes
- Cluster Autoscaler bounds updates
- OKE add-on installation, update, or removal
- Slurm-aware node drain and resume; destructive operations on detected Slinky workers are refused
- automatic workload cordon, drain, eviction, uncordon, Kueue policy changes,
  or Slurm state changes as part of an upgrade
- automatic Terraform or OCI Resource Manager source updates after a live
  upgrade
- automatic deletion of source pools after a blue-green migration

GPU Memory Cluster mutation paths are implemented and unit-tested with OCI SDK
models. Live GMC mutation validation remains pending an available GMC test
environment.
