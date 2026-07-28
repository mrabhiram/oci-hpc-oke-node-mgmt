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
- guarded creation of self-managed RDMA Cluster Network pools by deriving a new
  Instance Configuration from an existing RDMA pool
- custom image, shape, availability domain, subnet, NSG, boot, Flex, Kubernetes,
  metadata, tag, capacity, fault-domain, CNI, encryption, IMDS, and lifecycle
  overrides with effective-request dry-run output
- official OCI HPC OKE worker cloud-init composition for local NVMe RAID and
  existing FSS and Lustre mounts
- Slurm-style `clusters list`, `clusters create`, and
  `clusters add node` worker-pool aliases
- explicit `pools add` and `pools remove` capacity commands
- validated dry-run plans for every mutation
- Kubernetes Lease serialization for concurrent mutations
- wait for OCI active count and Kubernetes Ready count after resize
- wait for allocatable GPU, RDMA topology, and applicable RDMA VF readiness
- guarded specific managed OKE node removal/termination through OKE `delete_node`
- guarded specific self-managed node removal/replacement through instance-pool detach and automatic termination
- multi-node termination by identifier or exact field selector
- Kubernetes cordon, uncordon, and PDB-aware drain workflows
- default drain before node termination, with explicit `emptyDir` and unmanaged-pod acknowledgements
- deterministic pool, node, GPU, RDMA, add-on, and scheduler health checks
- actionable recommendations derived from health warnings and failures
- accelerator add-on validation against discovered GPU and RDMA capacity
- modular Click command architecture and shell-completion support

## Documentation

- [`README.md`](README.md)
- [`architecture.md`](architecture.md)
- [`controller-install.md`](controller-install.md)
- [`command-reference.md`](command-reference.md)

## Not Implemented Yet

- whole worker-pool and Cluster Network deletion
- boot volume replacement wrapper
- automatic Kueue `ClusterQueue` quota updates after worker-pool capacity changes
- Cluster Autoscaler bounds updates
- OKE add-on installation, update, or removal
- Slurm-aware node drain and resume; destructive operations on detected Slinky workers are refused
