# Scope

This page describes the current implementation scope for the OKE HPC Node
Management Tool.

## Implemented

- read-only discovery
- OCI/Kubernetes node join
- inferred Kubernetes-only pools when OCI is disabled or unavailable
- pool, node, topology, add-on, autoscaler, and reconcile views
- JSON/CSV/table output
- graceful warnings when one discovery source is unavailable
- managed OKE Compute Cluster placement and host-group discovery
- suppression of OKE-internal Compute Cluster backing instance pools
- read-only OKE add-on lifecycle and installed-version discovery
- strict RDMA topology validation that rejects missing and sentinel IMDS values
- RDMA VF readiness when `NvidiaNetworkOperator` is active
- Slinky Slurm hostname aliases for node lookup and output
- fail-closed Slinky protection for node removal, replacement, and pool scale-down
- guarded managed OKE node pool resize through a size-only `node_config_details` update
- guarded self-managed cluster-network and instance-pool resize
- wait for OCI active count and Kubernetes Ready count after resize
- wait for allocatable GPU, RDMA topology, and applicable RDMA VF readiness
- guarded specific managed OKE node removal/termination through OKE `delete_node`
- guarded specific self-managed node removal/replacement through instance-pool detach and automatic termination

## Documentation

- [`controller-install.md`](controller-install.md)

## Not Implemented Yet

- explicit Kubernetes cordon/drain workflow outside OKE delete-node eviction
- boot volume replacement wrapper
- automatic Kueue `ClusterQueue` quota updates after worker-pool capacity changes
- Cluster Autoscaler bounds updates
- health check execution
- OKE add-on installation, update, or removal
- Slurm-aware node drain and resume; destructive operations on detected Slinky workers are refused
