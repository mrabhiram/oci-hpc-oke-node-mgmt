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
- stdlib unit tests for model/provider-ID parsing
- guarded managed OKE node pool resize through `node_config_details.size`
- wait for OCI active count and Kubernetes Ready count after resize
- guarded specific managed OKE node removal through OKE `delete_node`

## Documentation

- [`controller-install.md`](controller-install.md)

## Not Implemented Yet

- RDMA cluster-network-backed instance pool resize
- explicit Kubernetes cordon/drain workflow outside OKE delete-node eviction
- node termination
- boot volume replacement wrapper
- Kueue quota sync
- Cluster Autoscaler bounds updates
- health check execution
