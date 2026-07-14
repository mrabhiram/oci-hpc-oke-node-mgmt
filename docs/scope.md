# Scope

This page describes the current implementation scope for the OKE HPC Node
Management Tool.

## Implemented

- read-only discovery
- OCI/Kubernetes node join
- inferred Kubernetes-only pools when OCI is disabled or unavailable
- pool, node, topology, autoscaler, and reconcile views
- JSON/CSV/table output
- graceful warnings when one discovery source is unavailable
- unit tests for model, provider-ID, OCI mutation, readiness, topology, and output behavior
- guarded managed OKE node pool resize through `node_config_details.size`
- guarded self-managed cluster-network and instance-pool resize
- wait for OCI active count and Kubernetes Ready count after resize
- wait for allocatable GPU and RDMA topology readiness on applicable pools
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
