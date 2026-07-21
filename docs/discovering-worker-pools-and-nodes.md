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

Example output:

```text
name      kind             placement        shape      desired  oci_active  k8s_ready  gpu             rdma  rdma_vf_required  slinky  autoscaler  kueue_flavor
--------  ---------------  ---------------  ---------  -------  ----------  ---------  --------------  ----  ----------------  ------  ----------  ------------
oke-rdma  cluster-network  cluster-network  BM.GPU4.8  2        2           2          nvidia.com/gpu  yes   no                no      -           -
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

For a concise cross-pool view, select the operational columns explicitly:

```bash
mgmt-oke --auth instance_principal nodes list \
  --columns name,status,pool,shape,gpu,rdma,ready,schedulable \
  --sort pool,name
```

Example output:

```text
name           status  pool        shape                gpu               rdma  ready  schedulable
-------------  ------  ----------  -------------------  ----------------  ----  -----  -----------
cpu-node-1     Ready   oke-cpu     VM.Standard.E5.Flex  -                 no    yes    yes
gpu-node-1     Ready   oke-gpu     VM.GPU.A10.1         nvidia.com/gpu=1  no    yes    yes
rdma-node-1    Ready   oke-rdma    BM.GPU4.8            nvidia.com/gpu=8  yes   yes    yes
rdma-node-2    Ready   oke-rdma    BM.GPU4.8            nvidia.com/gpu=8  yes   yes    yes
system-node-1  Ready   oke-system  VM.Standard.E5.Flex  -                 no    yes    yes
system-node-2  Ready   oke-system  VM.Standard.E5.Flex  -                 no    yes    yes
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

Example output:

```text
name                  state   version         active  error
--------------------  ------  --------------  ------  -----
CoreDNS               ACTIVE  v1.12.2-fips-4  yes     -
KubeProxy             ACTIVE  v1.35.2         yes     -
NodeFeatureDiscovery  ACTIVE  v0.17.3-1       yes     -
NodeProblemDetector   ACTIVE  v0.8.20         yes     -
NvidiaGpuOperator     ACTIVE  v25.10.1        yes     -
ObservabilityAgent    ACTIVE  v1.0.0          yes     -
OciVcnIpNative        ACTIVE  v3.3.0          yes     -
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

Example output for the pool-specific command:

```text
hpc_island  network_block  local_block  nodes  ready  shapes
----------  -------------  -----------  -----  -----  ---------
island-a    block-a        local-a      1      1      BM.GPU4.8
island-a    block-a        local-b      1      1      BM.GPU4.8
```

The topology view includes HPC Island, Network Block, Local Block, node count,
Ready count, and shapes. Nodes with missing or sentinel topology values are not
reported as topology-ready.

## Cluster Autoscaler Inventory

Inspect Cluster Autoscaler `--nodes` bindings:

```bash
mgmt-oke --auth instance_principal autoscaler status
```

Example output when no pool is autoscaler-owned:

```text
(none)
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

Example summary sections:

```text
Cluster Autoscaler
(none)

Kueue
topologies  resource_flavors  cluster_queues  local_queues
----------  ----------------  --------------  ------------
1           2                 1               1
```

Use full reconciliation when comparing pool counts, workload state, autoscaler
ownership, add-ons, Kueue resources, and ResourceFlavor-to-pool matches in one
operation.

## Partial Discovery Modes

Kubernetes-only discovery is useful when OCI APIs are temporarily unavailable:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list \
  --columns name,status,pool,shape --sort pool,name
```

Example output:

```text
name           status  pool        shape
-------------  ------  ----------  -------------------
cpu-node-1     Ready   oke-cpu     VM.Standard.E5.Flex
gpu-node-1     Ready   oke-gpu     VM.GPU.A10.1
rdma-node-1    Ready   oke-rdma    BM.GPU4.8
rdma-node-2    Ready   oke-rdma    BM.GPU4.8
system-node-1  Ready   oke-system  VM.Standard.E5.Flex
system-node-2  Ready   oke-system  VM.Standard.E5.Flex
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
