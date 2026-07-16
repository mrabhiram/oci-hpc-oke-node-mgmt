# Architecture

This document describes two core behaviors of the OKE HPC Node Management
Tool:

- automatic discovery of the OKE cluster, region, and compartment
- ownership-aware management of standard, Compute Cluster-backed, and legacy
  Cluster Network worker pools

The CLI uses Kubernetes and OCI as complementary sources. Kubernetes provides
the live node and workload view. OCI provides the authoritative worker-pool
configuration, placement model, desired size, instance membership, and mutation
APIs.

## Implementation Map

| File | Responsibility |
| --- | --- |
| `src/oke_hpc_mgmt/backends/kubeconfig.py` | Loads and validates kubeconfig, selects a context, and extracts the OKE cluster OCID and region from OCI CLI exec arguments. |
| `src/oke_hpc_mgmt/discovery.py` | Resolves and caches the OCI target, joins Kubernetes and OCI inventory, classifies pool ownership, and applies add-on readiness expectations. |
| `src/oke_hpc_mgmt/backends/oci.py` | Calls OKE `GetCluster`, discovers managed and self-managed pools, filters internal backing pools, and implements ownership-specific OCI mutations. |
| `src/oke_hpc_mgmt/backends/kubernetes.py` | Discovers Kubernetes nodes, pod safety counts, allocatable resources, topology labels, and Slinky metadata. |
| `src/oke_hpc_mgmt/models.py` | Defines node, pool, add-on, placement, RDMA readiness, and discovery snapshot models. |
| `src/oke_hpc_mgmt/cli.py` | Defines commands and overrides, propagates kubeconfig exec authentication, enforces mutation safety, routes OCI APIs, and waits for convergence. |
| `src/oke_hpc_mgmt/render.py` | Exposes ownership, placement, Compute Cluster, add-on, Slinky, GPU, and RDMA fields in table, JSON, and CSV output. |
| `src/oke_hpc_mgmt/__main__.py` | Preserves CLI exit status when invoked with `python -m oke_hpc_mgmt`. |
| `pyproject.toml` | Declares OCI, Kubernetes, and safe YAML parsing dependencies plus development checks. |
| `tests/` | Covers target parsing and precedence, OCI API payloads, ownership classification, mutation routing, readiness, rendering, help text, and process exit behavior. |

## Automatic OCI Target Discovery

An OCI-generated OKE kubeconfig contains the cluster OCID and region in the
exec-plugin arguments used to obtain a Kubernetes authentication token. A
typical user entry invokes:

```text
oci ce cluster generate-token --cluster-id <cluster-ocid> --region <region>
```

The kubeconfig does not normally contain the cluster compartment OCID. The tool
therefore resolves the complete OCI target in two stages:

1. Read the cluster OCID and region from the selected kubeconfig context.
2. Call the OKE `GetCluster` API and read `cluster.compartment_id` from the
   response.

This allows an operator node with one configured OKE cluster to run:

```bash
mgmt-oke --auth instance_principal pools list
```

without setting `OKE_CLUSTER_ID`, `OCI_REGION`, or `OCI_COMPARTMENT_ID`.

### Kubeconfig Selection

The tool applies the following context selection order:

1. The context supplied explicitly with `--context`.
2. The kubeconfig `current-context`.
3. The only unambiguous context when the kubeconfig identifies one cluster and
   one user selection.

Stack operator nodes normally contain one cluster and do not need
`--context`. The option is retained only as a visible troubleshooting override;
there is no environment-variable context selector.

The kubeconfig path comes from `--kubeconfig`, then `KUBECONFIG`, then
`~/.kube/config`. Multiple paths in `KUBECONFIG` are merged using kubeconfig
path-separator semantics, with the first named cluster, context, or user entry
taking precedence.

The selected user must use the OCI CLI exec plugin and its arguments must
contain `ce cluster generate-token` and a valid `--cluster-id`. Both split and
equals-style arguments are supported:

```text
--cluster-id <cluster-ocid>
--cluster-id=<cluster-ocid>
```

The same forms are supported for `--region`.

### Resolution Precedence

Explicit values always take precedence over automatic discovery:

| Value | Resolution order |
| --- | --- |
| Compartment OCID | `--compartment-id`, `OCI_COMPARTMENT_ID`, OKE `GetCluster` |
| Cluster OCID | `--cluster-id`, `OKE_CLUSTER_ID`, selected kubeconfig context |
| Region | `--region`, `OCI_REGION`, selected kubeconfig context, OCI config where applicable |

Automatic discovery fills only missing values. It does not replace an explicit
cluster, region, or compartment. If an explicit cluster does not match the
selected kubeconfig context, the tool does not borrow the kubeconfig region for
that cluster.

### Authentication

OCI inventory and mutations use the authentication method selected by
`--auth` or `OCI_AUTH`:

- `config_file`
- `instance_principal`
- `resource_principal`
- `none`

The Kubernetes client may also need to run the OCI CLI exec plugin from the
kubeconfig. When instance-principal or resource-principal authentication is
selected, the CLI supplies the corresponding process-local `OCI_CLI_AUTH`
value to that subprocess unless `OCI_CLI_AUTH` is already set explicitly.

The OCI CLI executable must be on `PATH` for an OCI-generated kubeconfig.

### Failure Behavior

Read-only and mutating commands handle target-resolution failures differently:

| Command type | Behavior when the OCI target cannot be resolved |
| --- | --- |
| Read-only discovery | Preserves available Kubernetes inventory and prints an actionable OCI warning. |
| Pool resize | Fails before confirmation or any OCI mutation if the compartment cannot be resolved. |
| Specific node removal | Fails before confirmation or any OCI mutation if the compartment cannot be resolved. |
| Add-on status | Fails if the cluster OCID cannot be resolved. |

`--skip-oci` disables OCI discovery and mutation. `--auth none` disables OCI API
authentication but does not independently configure the kubeconfig exec plugin.
For Kubernetes-only discovery on an operator node, use:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list
```

Kubeconfig target discovery is unavailable with `--in-cluster`. In-cluster
deployments must provide explicit target values required by the requested OCI
operation.

### Reuse During Operations

The resolved values and configured OCI backend are retained by one
`DiscoveryService` instance. Mutation preflight, the mutation call, and any
`--wait` polls therefore use the same cluster, region, compartment, and
authentication configuration. The tool does not repeatedly parse kubeconfig or
call `GetCluster` during one command.

## Worker-Pool Ownership

OCI HPC OKE v26.7 deploys GPU with RDMA workers as managed OKE node pools placed
in OCI Compute Clusters by default. Earlier deployments, and deployments using
the legacy Cluster Network option, expose a self-managed Cluster Network with
an embedded Instance Pool.

These models use different control planes. The CLI identifies the owner before
selecting a resize or node-removal API.

| Worker model | CLI classification | Placement | Resize owner | Specific node owner |
| --- | --- | --- | --- | --- |
| Standard managed OKE pool | `kind=node-pool` | `standard` | OKE `UpdateNodePool` | OKE `DeleteNode` |
| Managed OKE RDMA pool | `kind=node-pool` | `compute-cluster` | OKE `UpdateNodePool` | OKE `DeleteNode` |
| Legacy self-managed RDMA pool | `kind=cluster-network` | `cluster-network` | Compute Management `UpdateClusterNetwork` | Compute Management instance-pool detach |
| Standalone instance pool | `kind=instance-pool` | `instance-pool` or `compute-cluster` | Compute Management `UpdateInstancePool` | Compute Management instance-pool detach |

### Managed Compute Cluster Discovery

For every managed OKE node pool, the OCI backend reads the full node-pool
details and records:

- `node_config_details.size`
- `node_config_details.compute_cluster_id`
- placement availability domain
- placement host-group OCIDs, when present
- active OKE node instance OCIDs
- shape and GPU resource type
- initial node labels

When `compute_cluster_id` is present, the pool remains an OKE `node-pool` but is
reported with `placement=compute-cluster` and RDMA enabled. The Compute Cluster
is placement metadata; it is not treated as the worker-pool owner.

OKE may expose an internal Instance Pool that backs the managed node pool. The
tool suppresses that resource from standalone Instance Pool inventory when it
matches either:

- the managed node pool's Compute Cluster OCID, or
- an instance OCID already owned by the managed node pool

This prevents one managed RDMA pool from appearing twice and prevents mutation
from being routed to OKE-internal infrastructure.

### Managed Pool Resize

For both standard and Compute Cluster-backed managed pools, the CLI sends an OKE
`UpdateNodePool` request containing only:

```text
node_config_details.size = <target-size>
```

It does not copy placement, networking, encryption, tag, or pod-network fields
back into the request. OKE retains ownership of the node-pool configuration and
places new RDMA workers in the associated Compute Cluster.

The tool does not resize the Compute Cluster and does not directly resize its
OKE-internal backing Instance Pool.

Pool-level size reduction delegates instance selection to OKE. The specific
node API described below is used when the operator must select the worker.

### Managed Node Removal Or Replacement

For a node owned by a managed OKE pool, including a Compute Cluster-backed RDMA
pool, the CLI calls OKE `DeleteNode`:

- default removal sets `is_decrement_size=true`, so the desired pool size drops
  by one
- `--keep-size` sets `is_decrement_size=false`, so OKE replaces the deleted
  worker and preserves desired size
- `--eviction-grace` and `--force-after-grace` are passed only to the managed
  OKE deletion path

The selected node is never detached directly from an OKE-internal Instance
Pool.

### Legacy Cluster Network Resize

A legacy RDMA pool is represented by a Cluster Network containing an embedded
Instance Pool. The existing Instance Configuration already contains cloud-init
and node bootstrap configuration.

To add or remove capacity, the CLI reads the current Cluster Network, preserves
its other embedded pool fields, changes only the selected pool size, and calls
`UpdateClusterNetwork`. It does not recreate the Instance Configuration or
rerun a separate worker-pool creation workflow.

Pool-level size reduction delegates instance selection to Compute Management.
Use specific node removal when worker identity matters.

### Legacy Node Removal Or Replacement

For a node in a self-managed Cluster Network or standalone Instance Pool, the
CLI calls `DetachInstancePoolInstance` with automatic termination enabled:

- default removal uses `is_decrement_size=true`
- `--keep-size` uses `is_decrement_size=false`, allowing the Instance Pool to
  launch a replacement from its existing Instance Configuration

Managed OKE eviction options are rejected for this path because Kubernetes
eviction is not controlled by the OKE `DeleteNode` API.

## Node And Resource Readiness

After a mutation with `--wait`, the tool reconciles the selected pool until:

- desired OCI size equals the target
- active OCI instance count equals the target when available
- Kubernetes Ready node count equals the target
- allocatable GPU count equals the target for GPU pools
- valid OCI RDMA topology labels exist on every Ready node for RDMA pools
- `nvidia.com/rdma-vf` is allocatable on every Ready RDMA node when the NVIDIA
  Network Operator add-on is active

Topology validation rejects missing values and sentinel values such as
`no-imds-data`, `unknown`, and `not-available`.

## Safety Boundaries

Before mutation, the CLI verifies pool ownership and backing identifiers. It
also refuses unsafe operations when:

- the pool is owned by Cluster Autoscaler
- a target node has workload pods and `--allow-workloads` is absent
- a requested target size is negative
- a scale-down or node removal targets a detected Slinky-managed worker
- required OCI target information cannot be resolved

All mutations require `--yes` or an interactive confirmation. Discovery,
topology, add-on, autoscaler, and reconciliation commands are read-only.
