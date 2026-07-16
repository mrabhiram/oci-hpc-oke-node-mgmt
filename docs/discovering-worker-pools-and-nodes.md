# Discovering Worker Pools and Nodes

This guide explains how to inspect OCI worker resources and their corresponding
Kubernetes nodes before performing a capacity or node-lifecycle operation.

## Overview

The tool joins two inventory sources:

- OCI APIs provide node-pool ownership, placement, desired size, active
  instances, Compute Cluster metadata, and Cluster Network metadata.
- Kubernetes provides node readiness, allocatable GPU and RDMA resources,
  workload counts, topology labels, add-on-related readiness, and Slinky
  metadata.

The cluster OCID and region are discovered from kubeconfig. The compartment is
then resolved through OKE `GetCluster`.

## Prerequisites

- `mgmt-oke` installed on the operator node
- working `kubectl` access to the cluster
- OCI authentication with permission to read the cluster, node pools, Cluster
  Networks, Instance Pools, and instances

## Worker-Pool Inventory

List all discovered pools:

```bash
mgmt-oke --auth instance_principal pools list
```

Get one pool by name or backing OCI resource OCID:

```bash
mgmt-oke --auth instance_principal pools get oke-rdma
```

The ownership fields determine how later mutations are routed:

| `kind` | `placement` | Interpretation |
| --- | --- | --- |
| `node-pool` | `standard` | Standard managed OKE node pool. |
| `node-pool` | `compute-cluster` | Managed OKE node pool placed in a Compute Cluster. |
| `cluster-network` | `cluster-network` | Legacy self-managed RDMA Cluster Network and embedded Instance Pool. |
| `instance-pool` | `instance-pool` | Standalone Instance Pool. |
| `kubernetes-inferred` | `kubernetes-inferred` | Kubernetes inventory is available but authoritative OCI pool discovery is not. |

For JSON output, additional backing identifiers are included:

```bash
mgmt-oke --auth instance_principal --format json pools get oke-rdma
```

The JSON row includes applicable `node_pool_id`, `cluster_network_id`,
`instance_pool_id`, `compute_cluster_id`, and `host_group_ids` fields.

## Node Inventory

List all Kubernetes nodes:

```bash
mgmt-oke --auth instance_principal nodes list
```

Filter by pool:

```bash
mgmt-oke --auth instance_principal nodes list --pool oke-gpu
```

Show only nodes with complete OCI RDMA topology labels:

```bash
mgmt-oke --auth instance_principal nodes list --rdma-only
```

Inspect one or several nodes:

```bash
mgmt-oke --auth instance_principal nodes get <node-name>
mgmt-oke --auth instance_principal nodes get <node-name> <node-ip>
```

Review `workload_pods` before removing a node. DaemonSet, mirror, and known
system-namespace pods are reported separately so they do not look like ordinary
application workloads.

## Add-on Inventory

List the add-ons reported by OKE:

```bash
mgmt-oke --auth instance_principal addons status
```

The result includes lifecycle state, installed version, active state, and any
reported add-on error. When the NVIDIA Network Operator is active, RDMA pool
wait operations also require `nvidia.com/rdma-vf` readiness.

## RDMA Topology

Group Ready and non-Ready RDMA nodes by OCI topology:

```bash
mgmt-oke --auth instance_principal topology list
mgmt-oke --auth instance_principal topology list --pool oke-rdma
```

The topology view includes HPC Island, Network Block, Local Block, node count,
Ready count, and shapes. Nodes with missing or sentinel topology values are not
reported as topology-ready.

## Cluster Autoscaler Inventory

Inspect Cluster Autoscaler `--nodes` bindings:

```bash
mgmt-oke --auth instance_principal autoscaler status
```

Use this command or the full `reconcile` view before manual mutation. The fast
`pools list` and `pools get` views do not scan workload pod counts, Cluster
Autoscaler deployments, or Kueue resources. Their autoscaler and Kueue fields
are therefore not authoritative. Resize and node-removal preflight always
performs the required ownership and workload checks.

## Full Reconciliation

```bash
mgmt-oke --auth instance_principal reconcile
```

Use full reconciliation when comparing pool counts, workload state, autoscaler
ownership, add-ons, Kueue resources, and ResourceFlavor-to-pool matches in one
operation.

## Partial Discovery Modes

Kubernetes-only discovery is useful when OCI APIs are temporarily unavailable:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list
```

OCI-only pool discovery is useful when the Kubernetes API is unavailable:

```bash
mgmt-oke --auth instance_principal --skip-kubernetes pools list
```

Partial discovery is intended for diagnosis. OCI-backed mutations still require
complete OCI inventory, and safe node removal requires Kubernetes workload
visibility.

## Verification

For a stable pool, compare:

```text
desired == oci_active == k8s_ready
```

Temporary differences are normal while OCI is provisioning or terminating
instances, while a node is joining Kubernetes, or while GPU and network device
plugins are initializing.
