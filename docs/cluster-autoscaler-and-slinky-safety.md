# Cluster Autoscaler and Slinky Safety

This guide explains the ownership checks that prevent manual capacity changes
from conflicting with Cluster Autoscaler or Slinky Slurm.

## Overview

`mgmt-oke` can mutate OCI worker capacity, but it does not replace a workload
scheduler or autoscaler. Before resize or node removal, the CLI discovers
external ownership and refuses operations that require coordination it cannot
perform safely.

Two protections are enforced:

- manual resize and node removal are refused for a pool targeted by Cluster
  Autoscaler
- Slinky-managed pools allow scale-up but refuse scale-down, specific node
  removal, and replacement until a Slurm-aware drain workflow exists

## Cluster Autoscaler Detection

The CLI scans Kubernetes deployments identified as Cluster Autoscaler and
parses arguments in this form:

```text
--nodes=<minimum>:<maximum>:<target-ocid>
```

It then matches the target OCID to discovered node pools, Cluster Networks, or
Instance Pools.

## Inspect Autoscaler Ownership

```bash
mgmt-oke --auth instance_principal autoscaler status
```

The result includes deployment, namespace, minimum, maximum, target OCID, and
matched pool name.

Use the full snapshot when reviewing multiple ownership signals:

```bash
mgmt-oke --auth instance_principal reconcile
```

> [!NOTE]
> `pools list` is optimized for inventory and does not scan Cluster Autoscaler
> deployments. Resize and node-removal preflight always performs the scan.

## Autoscaler-Owned Pool Behavior

The following commands are refused when the selected pool matches an autoscaler
target:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --delta 1
mgmt-oke --auth instance_principal nodes remove <node-name-or-ip>
```

There is no force flag for bypassing this protection. Change capacity through
the autoscaler's configured bounds or transfer ownership through a separately
reviewed autoscaler change before using manual lifecycle commands.

## Slinky Detection

A worker or pool is treated as Slinky-managed when discovery finds one or more
of these signals:

- `nodeset.slinky.slurm.net/hostname-override` node annotation
- `oci.oraclecloud.com/slinky-hostname-prefix` pool or node label
- a running pod labeled as a Slinky nodeset
- a `slurmd` worker pod using the upstream application labels

The Slinky hostname is also accepted by `nodes get` for read-only lookup.

```bash
mgmt-oke --auth instance_principal nodes get <slurm-node-name>
```

## Supported Slinky Operation

Scale-up remains supported because it adds capacity without removing an active
Slurm worker:

```bash
mgmt-oke --auth instance_principal pools resize <slinky-pool> --delta 1 --wait
```

The new worker must still join the Slinky control plane before Slurm schedules
jobs on it. `mgmt-oke` verifies OCI, Kubernetes, GPU, and RDMA readiness but does
not verify Slurm state.

## Refused Slinky Operations

The CLI refuses:

```bash
mgmt-oke --auth instance_principal pools resize <slinky-pool> --delta -1
mgmt-oke --auth instance_principal nodes remove <slinky-node>
mgmt-oke --auth instance_principal nodes remove <slinky-node> --keep-size
```

`--allow-workloads` and `--yes` do not bypass Slinky ownership protection.

## Operational Guidance

Do not remove Slinky labels, annotations, or pods to make a destructive command
pass. Those signals represent control-plane ownership. Drain and transition the
worker through Slurm-aware tooling when that workflow becomes available.

## Kueue Scope

The full reconciliation view reports Kueue Topology, ResourceFlavor,
ClusterQueue, and LocalQueue counts and can associate a ResourceFlavor with a
pool. The CLI does not change Kueue quota after a resize. Update queue quota
through the Kueue configuration workflow when capacity changes require it.

## Verification

Before any manual mutation, run:

```bash
mgmt-oke --auth instance_principal autoscaler status
mgmt-oke --auth instance_principal pools get <pool-name>
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
```

The mutation command repeats ownership discovery immediately before submitting
an OCI request.
