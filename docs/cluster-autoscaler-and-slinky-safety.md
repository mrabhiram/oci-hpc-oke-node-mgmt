# Cluster Autoscaler and Slinky Safety

This guide explains the ownership checks that prevent manual capacity changes
from conflicting with Cluster Autoscaler or Slinky Slurm.

## Overview

`mgmt-oke` can mutate OCI worker capacity, but it does not replace a workload
scheduler or autoscaler. Before resize, node removal, or boot volume
replacement, the CLI discovers external ownership and refuses operations that
require coordination it cannot perform safely.

Two protections are enforced:

- manual resize, node removal, and BVR are refused for a pool targeted by
  Cluster Autoscaler
- Slinky-managed pools allow scale-up but refuse scale-down, specific node
  removal, replacement, and maintenance BVR until a Slurm-aware drain workflow
  exists

Kubernetes upgrade commands use a separate read-only workload gate. They can
proceed only after the operator changes Slurm state externally and the CLI
proves the partition, node, and job conditions described below.

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

Example output when no Cluster Autoscaler owns a pool:

```text
(none)
```

An empty result means manual ownership protection is not active; it does not
disable the other drain, Slinky, confirmation, or mutation-lock safeguards.

Use the full snapshot when reviewing multiple ownership signals:

```bash
mgmt-oke --auth instance_principal reconcile
```

Example scheduler sections from the full output:

```text
Cluster Autoscaler
(none)

Kueue
topologies  resource_flavors  cluster_queues  local_queues
----------  ----------------  --------------  ------------
1           2                 1               1
```

> [!NOTE]
> `pools list` is optimized for inventory and does not scan Cluster Autoscaler
> deployments. Resize, node-removal, and BVR preflight always perform the scan.

## Autoscaler-Owned Pool Behavior

The following commands are refused when the selected pool matches an autoscaler
target:

```bash
mgmt-oke --auth instance_principal pools resize <pool-name> --delta 1
mgmt-oke --auth instance_principal nodes terminate <node-name-or-ip>
mgmt-oke --auth instance_principal nodes boot-volume-replace <node-name-or-ip>
mgmt-oke --auth instance_principal pools boot-volume-replace <pool-name> --image-id <image-ocid>
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
mgmt-oke --auth instance_principal nodes terminate <slinky-node>
mgmt-oke --auth instance_principal nodes terminate <slinky-node> --keep-size
mgmt-oke --auth instance_principal nodes boot-volume-replace <slinky-node>
mgmt-oke --auth instance_principal pools boot-volume-replace <slinky-pool> --image-id <image-ocid>
```

`--allow-workloads` and `--yes` do not bypass Slinky ownership protection.

## Upgrade Workload Gate

Upgrade planning reads Kueue and Slinky state without changing either
scheduler:

```bash
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades plan --to v1.36 --format json
```

For every ResourceFlavor associated with the target pool, its ClusterQueue must
use `Hold` or `HoldAndDrain`, and the queue must have no admitted workload.

For a Slinky pool, the Kubernetes backend:

1. requires one discoverable `slurmctld` container
2. uses read-only pod exec to run `scontrol show partition`, `scontrol show
   node`, and `squeue`
3. requires affected partitions to be `DOWN` or `INACTIVE`
4. requires every target Slurm node to be drained/down
5. blocks running, configuring, completing, suspended, or resizing jobs that
   reference the target nodes

The operator performs all Kueue holds, workload moves, Slurm node drains, and
partition changes outside the tool. After the Kubernetes nodes are also Ready,
externally cordoned, and free of ordinary pods, execute with an independent
attestation:

```bash
mgmt-oke pools upgrade <slinky-pool> \
  --to v1.36.1 \
  --strategy instance-replace \
  --ack-application-compatibility \
  --ack-iac-drift \
  --ack-workloads-drained \
  --yes
```

`--yes` does not replace the three acknowledgements. The emergency
acknowledgement is limited to an API, RBAC, or exec observation failure and
cannot bypass detected pods, admitted Kueue work, or active Slurm jobs.

After worker convergence, the tool verifies each Slinky node is registered
again with the expected name. See
[Kubernetes Upgrades](./kubernetes-upgrades.md).

## Operational Guidance

Do not remove Slinky labels, annotations, or pods to make a destructive command
pass. Those signals represent control-plane ownership. Drain and transition the
worker through Slurm-aware tooling when that workflow becomes available.

## Kueue Scope

The full reconciliation view reports Kueue Topology, ResourceFlavor,
ClusterQueue, and LocalQueue counts and can associate a ResourceFlavor with a
pool. The CLI does not change Kueue quota after a resize and does not change
ClusterQueue stop policy during an upgrade. Update queue quota and stop policy
through the Kueue configuration workflow when capacity or maintenance state
requires it.

## Verification

Before any manual mutation, run:

```bash
mgmt-oke --auth instance_principal autoscaler status
mgmt-oke --auth instance_principal pools get <pool-name>
mgmt-oke --auth instance_principal nodes list --pool <pool-name>
```

The mutation command repeats ownership discovery immediately before submitting
an OCI request.
