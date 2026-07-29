# Live Kubernetes Upgrade Validation

This page records upgrade commands executed against a running OCI HPC OKE
cluster in `uk-london-1` on July 29, 2026. Resource identifiers and individual
pod names are sanitized. Versions, pool types, pool sizes, workload counts,
strategies, validation results, and errors are retained.

## Validation Boundary

No Kubernetes version was changed during this validation.

The following operations were executed:

- upgrade status and target resolution
- full-cluster upgrade planning
- control-plane upgrade dry run
- full-cluster orchestration dry run
- managed GPU and legacy RDMA worker ordering checks
- post-run checkpoint verification

The following operations were not executed:

- OKE `UpdateCluster`
- OKE `UpdateNodePool`
- Cluster Network Instance Configuration replacement
- worker boot-volume or instance replacement
- any OCI work request that changes the cluster

The live cluster remained at Kubernetes `v1.35.2`. The requested minor target
`v1.36` resolved to production patch `v1.36.1`.

## Validation Environment

| Resource | Observed state |
| --- | --- |
| Control plane | `v1.35.2`, `ACTIVE` |
| Managed CPU pool | `oke-cpu`, `1/1 Ready`, kubelet `v1.35.2` |
| Managed GPU pool | `oke-gpu`, `1/1 Ready`, kubelet `v1.35.2` |
| Managed system pool | `oke-system`, `2/2 Ready`, kubelet `v1.35.2` |
| Legacy RDMA backend | `oke-rdma`, Cluster Network, `2/2 Ready`, kubelet `v1.35.2` |
| Requested target | `v1.36`, resolved to `v1.36.1` |

The commands below were run through the public `mgmt-oke` 0.9.0 console
entrypoint with instance-principal authentication and automatic target
discovery.

## Upgrade Status

Command:

```bash
mgmt-oke --format json upgrades status --to v1.36
```

Example live output:

```json
[
  {
    "actual_versions": "v1.35.2",
    "available_versions": [
      "v1.35.2",
      "v1.36.0",
      "v1.36.1"
    ],
    "declared_version": "v1.35.2",
    "kind": "control-plane",
    "name": "<cluster-ocid>",
    "scheduler_state": "n/a",
    "state": "ACTIVE",
    "strategy": "OKE UpdateCluster",
    "target_version": "v1.36.1"
  },
  {
    "actual_versions": [
      "v1.35.2"
    ],
    "declared_version": "v1.35.2",
    "kind": "node-pool",
    "name": "oke-cpu",
    "scheduler_state": "blocked",
    "state": "1/1 Ready",
    "strategy": "auto,boot-volume-replace,instance-replace,blue-green",
    "target_version": "v1.36.1"
  },
  {
    "actual_versions": [
      "v1.35.2"
    ],
    "declared_version": "v1.35.2",
    "kind": "node-pool",
    "name": "oke-gpu",
    "scheduler_state": "not-drained",
    "state": "1/1 Ready",
    "strategy": "auto,boot-volume-replace,instance-replace,blue-green",
    "target_version": "v1.36.1"
  },
  {
    "actual_versions": [
      "v1.35.2"
    ],
    "declared_version": "v1.35.2",
    "kind": "node-pool",
    "name": "oke-system",
    "scheduler_state": "blocked",
    "state": "2/2 Ready",
    "strategy": "auto,boot-volume-replace,instance-replace,blue-green",
    "target_version": "v1.36.1"
  },
  {
    "actual_versions": [
      "v1.35.2"
    ],
    "declared_version": "v1.35.2",
    "kind": "cluster-network",
    "name": "oke-rdma",
    "scheduler_state": "blocked",
    "state": "2/2 Ready",
    "strategy": "auto,boot-volume-replace,instance-replace,blue-green",
    "target_version": "v1.36.1"
  },
  {
    "actual_versions": "v25.10.1",
    "declared_version": "v25.10.1",
    "kind": "addon",
    "name": "NvidiaGpuOperator",
    "scheduler_state": "n/a",
    "state": "compatible",
    "strategy": "PINNED",
    "target_version": "v1.36.1"
  }
]
```

The complete command also returned compatible target options for CoreDNS,
KubeProxy, Node Feature Discovery, Node Problem Detector, Observability Agent,
and OCI VCN-Native Pod Networking.

## Full Upgrade Plan

Command:

```bash
mgmt-oke --format json upgrades plan --to v1.36
```

Example live output:

```json
[
  {
    "operation": "control-plane-upgrade",
    "owner": "oke",
    "status": "planned",
    "target": "v1.36.1",
    "workload_pods": 0
  },
  {
    "details": {
      "current_version": "v1.35.2",
      "gate_active_pod_count": 9,
      "gate_cordoned": false,
      "gate_ready": true,
      "strategy": "boot-volume-replace",
      "target_version": "v1.36.1"
    },
    "operation": "worker-pool-upgrade",
    "owner": "node-pool",
    "status": "planned",
    "target": "oke-cpu"
  },
  {
    "details": {
      "current_version": "v1.35.2",
      "gate_active_pod_count": 12,
      "gate_cordoned": false,
      "gate_ready": true,
      "strategy": "boot-volume-replace",
      "target_version": "v1.36.1"
    },
    "operation": "worker-pool-upgrade",
    "owner": "node-pool",
    "status": "planned",
    "target": "oke-system"
  },
  {
    "details": {
      "current_version": "v1.35.2",
      "gate_active_pod_count": 0,
      "gate_cordoned": false,
      "gate_ready": true,
      "strategy": "boot-volume-replace",
      "target_version": "v1.36.1"
    },
    "operation": "worker-pool-upgrade",
    "owner": "node-pool",
    "status": "planned",
    "target": "oke-gpu"
  },
  {
    "details": {
      "api_server_refreshed": true,
      "cluster_ca_refreshed": true,
      "current_version": "v1.35.2",
      "gate_active_pod_count": 1,
      "gate_cordoned": false,
      "gate_kueue_blockers": [
        "ClusterQueue/<rdma-cluster-queue> stopPolicy=None"
      ],
      "gate_ready": true,
      "metadata_keys_preserved": [
        "apiserver_host",
        "cluster_ca_cert",
        "oke-initial-node-labels",
        "oke-k8version",
        "oke-kubeproxy-proxy-mode",
        "oke-max-pods",
        "oke-native-pod-networking",
        "oke-tenancy-id",
        "pod-nsgids",
        "pod-subnets",
        "ssh_authorized_keys",
        "user_data"
      ],
      "strategy": "instance-replace",
      "target_version": "v1.36.1"
    },
    "operation": "worker-pool-upgrade",
    "owner": "cluster-network",
    "status": "planned",
    "target": "oke-rdma"
  }
]
```

The output above is a sanitized projection of the returned JSON. Image and
Instance Configuration OCIDs were removed, and each `gate_active_pods` array
was replaced with its observed count.

The RDMA plan proved that target bootstrap generation refreshed the API server
and cluster CA while preserving the existing Kubernetes, networking, SSH, and
cloud-init metadata keys.

## Control-Plane Dry Run

Command:

```bash
mgmt-oke --format json clusters upgrade --to v1.36 --dry-run
```

Example live output:

```json
[
  {
    "current_size": null,
    "decrement_size": null,
    "details": {
      "source_version": "v1.35.2",
      "target_version": "v1.36.1"
    },
    "operation": "control-plane-upgrade",
    "owner": "oke",
    "pool": null,
    "status": "planned",
    "steps": [
      "revalidate OKE target, version policy, ETag, and add-ons",
      "call OKE UpdateCluster",
      "wait for work request and control-plane convergence"
    ],
    "target": "v1.36.1",
    "target_size": null,
    "warnings": [],
    "workload_pods": 0
  }
]
```

This validated target resolution, one-minor sequencing, add-on compatibility,
worker readiness, and the planned OKE API path. It did not call
`UpdateCluster`.

## Full-Orchestration Dry Run

Command:

```bash
mgmt-oke upgrades apply --to v1.36 --dry-run
```

Example live output:

```text
operation              target      owner            strategy             workloads  gate evidence                         status
---------------------  ----------  ---------------  -------------------  ---------  ------------------------------------  -------
control-plane-upgrade  v1.36.1     oke              OKE UpdateCluster    0          add-ons compatible                    planned
worker-pool-upgrade    oke-cpu     node-pool        boot-volume-replace  9          active pods; not cordoned             planned
worker-pool-upgrade    oke-system  node-pool        boot-volume-replace  12         active pods; not cordoned             planned
worker-pool-upgrade    oke-gpu     node-pool        boot-volume-replace  0          not cordoned                          planned
worker-pool-upgrade    oke-rdma    cluster-network  instance-replace     1          active pod; Kueue stopPolicy=None     planned
```

The table is normalized from the wider live CLI output. No OCI mutation
request, checkpoint ConfigMap, cordon, drain, eviction, or worker replacement
was performed.

## Worker Ordering Guards

A worker cannot be upgraded ahead of its control plane.

Managed GPU command:

```bash
mgmt-oke pools upgrade oke-gpu \
  --to v1.36.1 \
  --strategy boot-volume-replace \
  --dry-run
```

Example live output and exit status `2`:

```text
Error: Worker target v1.36.1 cannot be newer than control plane v1.35.2.
```

Legacy RDMA command:

```bash
mgmt-oke pools upgrade oke-rdma \
  --to v1.36.1 \
  --strategy instance-replace \
  --dry-run
```

Example live output and exit status `2`:

```text
Error: Worker target v1.36.1 cannot be newer than control plane v1.35.2.
```

These are expected refusals. The control plane must converge at `v1.36.1`
before either worker command becomes valid.

## Checkpoint Verification

Command run after all dry runs:

```bash
kubectl -n kube-system get configmap mgmt-oke-kubernetes-upgrade \
  --ignore-not-found \
  -o name
```

Example live output:

```text
<no output>
```

The empty result confirms that dry-run orchestration did not create persistent
upgrade state.

## Result

| Validation | Result |
| --- | --- |
| Target discovery and production patch resolution | Passed |
| Control-plane sequencing and downgrade/skew validation | Passed |
| OKE add-on target compatibility | Passed |
| Managed CPU, system, and GPU strategy planning | Passed |
| Legacy Cluster Network RDMA bootstrap transformation | Passed |
| Worker-before-control-plane refusal | Passed |
| Workload, cordon, and Kueue blocker reporting | Passed |
| Dry run left no checkpoint ConfigMap | Passed |
| Live control-plane version change | Not performed |
| Live managed-worker version change | Not performed |
| Live legacy RDMA worker replacement | Not performed |

This evidence validates discovery, planning, safety checks, strategy selection,
and dry-run command routing. It is not evidence of a completed Kubernetes
upgrade.

## Commands Reserved For Live Validation

The following commands were not executed. They require an approved target and
maintenance window, externally prepared workloads, and reconciliation of the
intended version change with Terraform or OCI Resource Manager.

Control-plane execution:

```bash
mgmt-oke clusters upgrade \
  --to v1.36.1 \
  --ack-application-compatibility \
  --ack-iac-drift \
  --yes
```

Full-cluster execution after every worker pool satisfies the workload gate:

```bash
mgmt-oke upgrades apply \
  --to v1.36.1 \
  --ack-application-compatibility \
  --ack-iac-drift \
  --ack-workloads-drained \
  --yes
```

The observed cluster was not ready for worker mutation at validation time:

- `oke-cpu` had nine ordinary pods and was not cordoned
- `oke-system` had twelve ordinary pods and was not cordoned
- `oke-gpu` had no ordinary pods but was not cordoned
- `oke-rdma` had one ordinary pod, was not cordoned, and its associated Kueue
  ClusterQueue used `stopPolicy=None`

After an approved live upgrade, capture the completed work-request,
control-plane, kubelet, add-on, GPU, RDMA, Kueue, Slinky, checkpoint, and
cleanup results on this page.
