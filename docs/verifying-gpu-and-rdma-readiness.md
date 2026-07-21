# Verifying GPU and RDMA Readiness

This guide explains how `mgmt-oke` evaluates GPU and RDMA workers and how to
diagnose a pool that has reached its OCI size but is not ready for workloads.

## Overview

Pool size alone does not prove that an HPC worker is usable. A node can be
ACTIVE in OCI while Kubernetes, GPU device discovery, RDMA topology labeling,
or SR-IOV virtual functions are still initializing.

With `--wait`, the CLI checks the layers applicable to the selected pool:

1. desired OCI size
2. active OCI instance count, when available
3. Kubernetes Ready node count
4. allocatable GPU resource count for GPU pools
5. valid OCI RDMA topology for RDMA pools
6. allocatable `nvidia.com/rdma-vf` when the NVIDIA Network Operator is active

## Prerequisites

- working `mgmt-oke`, `kubectl`, and OCI authentication
- a GPU or RDMA worker pool
- OKE add-ons and device plug-ins deployed according to the stack configuration

## Inspect Pool Readiness

```bash
mgmt-oke --auth instance_principal pools get oke-rdma
mgmt-oke --auth instance_principal nodes list --pool oke-rdma
```

Example pool output:

```text
name      kind             placement        shape      desired  oci_active  k8s_ready  gpu             rdma  rdma_vf_required  slinky  autoscaler  kueue_flavor
--------  ---------------  ---------------  ---------  -------  ----------  ---------  --------------  ----  ----------------  ------  ----------  ------------
oke-rdma  cluster-network  cluster-network  BM.GPU4.8  2        2           2          nvidia.com/gpu  yes   no                no      -           -
```

For a stable pool, `desired`, `oci_active`, and `k8s_ready` should agree.

The node view reports:

- `status`
- allocatable `gpu`
- strict `rdma` topology readiness
- allocatable `rdma_vf`
- workload and system pod counts

## Verify GPU Allocation

List the target pool:

```bash
mgmt-oke --auth instance_principal nodes list --pool oke-gpu \
  --columns name,status,pool,shape,gpu,rdma,workload_pods
```

Example output:

```text
name        status  pool     shape         gpu               rdma  workload_pods
----------  ------  -------  ------------  ----------------  ----  -------------
gpu-node-1  Ready   oke-gpu  VM.GPU.A10.1  nvidia.com/gpu=1  no    0
```

This example shows selected columns from the live node inventory.

NVIDIA nodes should advertise `nvidia.com/gpu`. AMD nodes should advertise
`amd.com/gpu`. A node can be Kubernetes Ready before its GPU resource becomes
allocatable, so a resize wait does not complete until the GPU count is ready.

Cross-check one node with Kubernetes:

```bash
kubectl describe node <node-name>
```

Review the `Allocatable` section and the GPU device plug-in pods when the
resource is missing.

## Verify RDMA Topology

```bash
mgmt-oke --auth instance_principal nodes list --rdma-only \
  --columns name,status,pool,shape,gpu,rdma,workload_pods
mgmt-oke --auth instance_principal topology list --pool oke-rdma
```

Example node and topology output:

```text
name         status  pool      shape      gpu               rdma  workload_pods
-----------  ------  --------  ---------  ----------------  ----  -------------
rdma-node-1  Ready   oke-rdma  BM.GPU4.8  nvidia.com/gpu=8  yes   0
rdma-node-2  Ready   oke-rdma  BM.GPU4.8  nvidia.com/gpu=8  yes   0

hpc_island  network_block  local_block  nodes  ready  shapes
----------  -------------  -----------  -----  -----  ---------
island-a    block-a        local-a      1      1      BM.GPU4.8
island-a    block-a        local-b      1      1      BM.GPU4.8
```

Node and topology identifiers are representative replacements; the readiness
counts and shape are retained from the captured output.

The CLI requires valid values for HPC Island, Network Block, and Local Block.
It rejects missing values and sentinel values including:

- `no-imds-data`
- `not-available`
- `unknown`
- `none`
- `null`
- `-`

A bare-metal GPU shape alone is treated as RDMA-capable, not topology-ready.

Inspect the underlying labels when needed:

```bash
kubectl get node <node-name> --show-labels
```

## Verify OKE Add-ons

```bash
mgmt-oke --auth instance_principal addons status
mgmt-oke --auth instance_principal addons validate --target gpu
mgmt-oke --auth instance_principal addons validate --target rdma --pool oke-rdma
```

Example add-on status output:

```text
name                  state   version         active  error
--------------------  ------  --------------  ------  -----
NodeFeatureDiscovery  ACTIVE  v0.17.3-1       yes     -
NvidiaGpuOperator     ACTIVE  v25.10.1        yes     -
NodeProblemDetector   ACTIVE  v0.8.20         yes     -
```

The full command also reports the core networking, DNS, proxy, and
observability add-ons discovered on the cluster.

Review `state`, `version`, `active`, and `error`. The add-on view is read-only;
the CLI does not install, update, or remove OKE add-ons.

Validation correlates add-on lifecycle with discovered capacity. It checks Node
Feature Discovery for accelerator pools, GPU Operator for NVIDIA pools, Network
Operator state for RDMA, GPU allocation, RDMA topology, and required RDMA VFs.

Run the underlying health categories directly when diagnosing a failure:

```bash
mgmt-oke --auth instance_principal health run --type gpu --pool oke-gpu
mgmt-oke --auth instance_principal health run --type rdma --pool oke-rdma
mgmt-oke --auth instance_principal recommendations list --type rdma --pool oke-rdma
```

Example health output:

```text
check            scope        status  message                                       recommendation
---------------  -----------  ------  --------------------------------------------  --------------
gpu-allocatable  gpu-node-1   PASS    nvidia.com/gpu=1                              -
rdma-topology    rdma-node-1  PASS    Required OCI RDMA topology labels are valid.  -
rdma-topology    rdma-node-2  PASS    Required OCI RDMA topology labels are valid.  -

Recommendations
(none)
```

## Verify Network Operator Virtual Functions

When `NvidiaNetworkOperator` is ACTIVE, RDMA pools are marked
`rdma_vf_required=yes`. Each Ready worker must advertise a positive
`nvidia.com/rdma-vf` value.

```bash
mgmt-oke --auth instance_principal nodes list --pool oke-rdma
kubectl describe node <rdma-node-name>
```

If `rdma_vf` is empty or zero, inspect the Network Operator pods and the
`rdma-vf` device plug-in resources before retrying a workload.

## Verify a Resize or Replacement

Use `--wait` for lifecycle operations:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --delta 1 --wait
```

or:

```bash
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip> \
  --keep-size --wait
```

Wait status is printed only when a value changes. For a GPU with RDMA pool it
can progress through:

```text
desired=3 oci_active=2 k8s_ready=2 gpu_ready=2 rdma_ready=2
desired=3 oci_active=3 k8s_ready=2 gpu_ready=2 rdma_ready=2
desired=3 oci_active=3 k8s_ready=3 gpu_ready=2 rdma_ready=3
desired=3 oci_active=3 k8s_ready=3 gpu_ready=3 rdma_ready=3
```

This ordering is not fixed. The important condition is that all applicable
counts equal the target before the operation reports `ready` or `removed`.
When RDMA VF readiness is required, the status also includes
`rdma_vf_ready=<count>`.

For `nodes terminate --keep-size`, the waiter additionally requires
`node_present=False` for the selected worker before accepting the replacement
as complete.

## Troubleshooting

### OCI Active Count Is Below Desired

Inspect OCI capacity, instance lifecycle state, and the work request. Do not
submit another resize until the first request is understood.

### Kubernetes Ready Count Is Below OCI Active

Inspect cloud-init, kubelet, network reachability, and node registration. For a
legacy Cluster Network, verify that the existing Instance Configuration still
contains the expected OKE bootstrap configuration.

### GPU Ready Count Is Below Kubernetes Ready

Inspect Node Feature Discovery, GPU Operator or device plug-in pods, driver
state, and node taints.

### RDMA Ready Count Is Below Kubernetes Ready

Inspect OCI topology labels and the topology labeler. Sentinel IMDS values are
intentionally treated as not ready.

### RDMA VF Ready Count Is Below Kubernetes Ready

Inspect the NVIDIA Network Operator, SR-IOV configuration, and device plug-in
resources. Add-on ACTIVE state alone does not prove that every node advertises
virtual functions.
