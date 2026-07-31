# Architecture

This document describes the core behaviors of the OKE HPC Node Management
Tool:

- automatic discovery of the OKE cluster, region, and compartment
- ownership-aware management of standard, Compute Cluster-backed, and legacy
  Cluster Network worker pools
- version-aware, checkpointed control-plane and worker upgrades across managed
  and self-managed backends

The CLI uses Kubernetes and OCI as complementary sources. Kubernetes provides
the live node and workload view. OCI provides the authoritative worker-pool
configuration, placement model, desired size, instance membership, and mutation
APIs.

## Implementation Map

| File | Responsibility |
| --- | --- |
| `src/oke_hpc_mgmt/backends/kubeconfig.py` | Loads and validates kubeconfig, selects a context, and extracts the OKE cluster OCID and region from OCI CLI exec arguments. |
| `src/oke_hpc_mgmt/discovery.py` | Resolves and caches the OCI target, joins Kubernetes and OCI inventory, classifies pool ownership, and applies add-on readiness expectations. |
| `src/oke_hpc_mgmt/backends/oci.py` | Calls OKE `GetCluster`, discovers managed and self-managed pools, filters internal backing pools, implements ownership-specific OCI and upgrade mutations, enforces ETags, and reads work requests. |
| `src/oke_hpc_mgmt/backends/kubernetes.py` | Discovers nodes, pods, Kueue, and Slinky state; manages ordinary lifecycle cordon/eviction; stores upgrade checkpoints; and serializes mutations with a Lease. |
| `src/oke_hpc_mgmt/models.py` | Defines node, pool, cluster, virtual-pool, add-on, placement, operation-plan, drain, health, and discovery models. |
| `src/oke_hpc_mgmt/upgrades.py` | Defines typed Kubernetes versions, target and skew validation, workload-gate evidence, strategy selection, checkpoint phases, and checkpoint serialization. |
| `src/oke_hpc_mgmt/commands/` | Defines the modular Click command tree, including upgrade status, planning, execution, recovery, output, and acknowledgements. |
| `src/oke_hpc_mgmt/workflows/upgrades.py` | Builds and executes control-plane and worker upgrade plans, verifies workload gates, writes checkpoints, resumes from observed state, and routes every backend strategy. |
| `src/oke_hpc_mgmt/workflows/` | Implements reusable ownership-aware pool, node termination, Kubernetes maintenance, boot-volume, and upgrade workflows. |
| `src/oke_hpc_mgmt/health.py` | Evaluates deterministic node, pool, GPU, RDMA, add-on, and scheduler health. |
| `src/oke_hpc_mgmt/selection.py` | Parses node identifiers and exact field selectors. |
| `src/oke_hpc_mgmt/cli.py` | Provides warning suppression, entrypoint naming, exception handling, and stable process exit status. |
| `src/oke_hpc_mgmt/render.py` | Exposes stable table, JSON, CSV, plan, health, and status row schemas. |
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

Example output after kubeconfig and compartment discovery:

```text
name        kind             placement        shape                desired  oci_active  k8s_ready  gpu             rdma  rdma_vf_required  slinky  autoscaler  kueue_flavor
----------  ---------------  ---------------  -------------------  -------  ----------  ---------  --------------  ----  ----------------  ------  ----------  ------------
oke-rdma    cluster-network  cluster-network  BM.GPU4.8            2        2           2          nvidia.com/gpu  yes   no                no      -           -
oke-cpu     node-pool        standard         VM.Standard.E5.Flex  1        1           1          -               no    no                no      -           -
oke-gpu     node-pool        standard         VM.GPU.A10.1         1        1           1          nvidia.com/gpu  no    no                no      -           -
oke-system  node-pool        standard         VM.Standard.E5.Flex  2        2           2          -               no    no                no      -           -
```

This output demonstrates the join between authoritative OCI ownership and
Kubernetes readiness. A managed v26.7 RDMA pool is reported as
`kind=node-pool, placement=compute-cluster`; the captured cluster shown above
uses the supported legacy Cluster Network model.

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
mgmt-oke --auth instance_principal --skip-oci nodes list \
  --columns name,status,pool,shape --sort pool,name
```

Example Kubernetes-only output:

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
| Managed OKE Host Group pool | `kind=node-pool` | `host-group` | OKE `UpdateNodePool` | OKE `DeleteNode` |
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

When placement rows contain `host_group_id` without a Compute Cluster, the pool
is reported with `placement=host-group`. A pool using both remains
`placement=compute-cluster` and exposes its Host Group OCIDs separately.

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

Before either managed or self-managed termination, the default workflow:

1. discovers and validates every selected node and its owning pool
2. performs dry-run Kubernetes eviction admission for non-DaemonSet pods
3. rejects unacknowledged `emptyDir` data and unmanaged pods
4. acquires the cluster mutation Lease
5. cordons all selected nodes
6. evicts eligible pods through the `policy/v1` Eviction API
7. applies and verifies every requested customer-reported unhealthy-host tag
8. invokes the ownership-specific OCI termination operation

`--no-drain` bypasses steps 2, 5, and 6 and requires `--allow-workloads` when
ordinary workload pods remain.

### Customer-Reported Unhealthy Host Tagging

Node termination accepts `--tag unhealthy` or `--tag none`. When the option is
omitted, the CLI records an independent operator decision for every selected
node through an interactive prompt. `--yes` is intentionally separate and
only confirms the destructive OCI operation.

For each requested unhealthy tag, the OCI backend:

1. gets the Compute instance and its ETag
2. deep-copies the existing `defined_tags` map
3. merges
   `ComputeInstanceHostActions.CustomerReportedHostStatus=unhealthy`
4. calls `UpdateInstance` with `if_match=<etag>`
5. gets the instance again and verifies the exact namespace, key, and value

Execution tags and verifies all requested instances before submitting any OKE
`DeleteNode` or Compute Management `DetachInstancePoolInstance` operation. A
missing ETag, authorization error, tag-namespace error, concurrent update, or
failed read-back stops termination. An instance that already has the exact tag
is accepted without another update.

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

### Worker Pool Creation

`pools create` requires an explicit `--type` and derives a new pool from a
matching, working stack pool:

- `cpu`: managed non-GPU, non-RDMA OKE source
- `gpu`: managed GPU, non-RDMA OKE source
- `rdma --rdma-mode compute-cluster`: existing managed Compute Cluster RDMA
  source, then a regular managed GPU source for the first pool
- `rdma` or `rdma --rdma-mode cluster-network`: legacy Cluster Network source

Preparation requires complete OCI pool discovery, rejects duplicate names, and
selects the source through `--from-pool`, conventional stack naming, or an
unambiguous single candidate. A managed Compute Cluster RDMA pool is not
eligible as a regular GPU source, but is preferred for another managed RDMA
pool.

For CPU, GPU, and managed Compute Cluster RDMA, the backend reads the source OKE
node pool and builds `CreateNodePoolDetails`. It preserves cluster-specific
bootstrap, CNI, pod networking, labels, tags, cycling, and eviction settings
unless a dedicated option overrides them. The new pool name and identity labels
are retargeted.

Managed Compute Cluster migration can use a second, explicit legacy bootstrap
source. `--bootstrap-from-pool` resolves a Cluster Network and embedded Instance
Pool, reads its Instance Configuration, and validates the required OKE metadata.
The workflow performs that lookup during planning and again under the mutation
Lease. Any metadata change after planning aborts the operation and requires a
new reviewed plan. It overlays legacy `user_data`, bootstrap hooks, and
non-reserved custom metadata onto the managed request. Managed endpoint, CA, version, labels, CNI,
pod networking, placement, and lifecycle fields remain authoritative. Mismatched
endpoint or CA values fail before mutation, preventing a legacy template from a
different OKE cluster from being executed.

Managed RDMA validates that the OKE cluster is enhanced, resolves display-form
AD names to canonical tenancy-prefixed values, requires one placement with no
fault domains, and verifies that the shape/image advertises RDMA ports. An
existing Compute Cluster must be `ACTIVE` in that AD. If no OCID is supplied,
the workflow creates and waits for a dedicated Compute Cluster before OKE
`CreateNodePool`. A supplied Host Group must be `ACTIVE` in the same AD and
have a `VALID` target matching the selected shape or an OCI platform name.

Compute Cluster and Host Group OCIDs are placed only in OKE's supported
`compute_cluster_id` and placement `host_group_id` fields. The tool verifies the
installed OCI SDK exposes both fields. It creates Compute Clusters but does not
create Host Groups or attach hosts.

For legacy RDMA, the backend reads the source Cluster Network, embedded Instance
Pool, and Instance Configuration. It deep-copies launch details, including the
image, shape, cloud-init, OKE join metadata, agent settings, primary and
secondary VNICs, block volumes, and networking. It applies selected overrides,
retargets instance and VNIC tags, updates the Kubernetes pool identity, and
removes source Terraform module and state labels.

The legacy RDMA backend calls `CreateInstanceConfiguration`, then
`CreateClusterNetwork` with one embedded Instance Pool. Already allocated
network-block configuration is not copied; Compute Management allocates
placement for the new Cluster Network. The source resources remain unchanged.

Every backend validates image and shape compatibility in the selected
availability domains, VCN consistency for subnets and NSGs, CNI compatibility,
and local-disk availability for NVMe RAID. Dry-run reports both requested and
effective configuration.

Optional cloud-init composition uses pinned copies of the official
`oci-hpc-oke` worker scripts. NVMe RAID is inserted before OKE bootstrap; FSS
and Lustre mounts are inserted after it. FSS and Lustre settings mount existing
endpoints and do not provision storage, network, IAM, or Kubernetes PV
resources.

If RDMA Instance Configuration creation succeeds but Cluster Network creation
fails, the error reports the derived Instance Configuration OCID. With
`--wait`, creation monitors the work request and uses the same OCI, Kubernetes,
GPU, RDMA topology, and applicable RDMA VF convergence checks as resize.

If dedicated Compute Cluster creation succeeds but OKE node-pool submission or
convergence fails, both OCIDs are reported and retained. This avoids deleting a
placement resource while an accepted but not yet observable OKE request could
still reference it.

### Legacy Node Removal Or Replacement

For a node in a self-managed Cluster Network or standalone Instance Pool, the
CLI calls `DetachInstancePoolInstance` with automatic termination enabled:

- default removal uses `is_decrement_size=true`
- `--keep-size` uses `is_decrement_size=false`, allowing the Instance Pool to
  launch a replacement from its existing Instance Configuration

Managed OKE eviction options are rejected for this path because Kubernetes
eviction is not controlled by the OKE `DeleteNode` API.

### Whole-Pool Deletion

`pools delete` snapshots the pool and complete Kubernetes membership, validates
drain admission, and acquires the mutation Lease. Immediately before mutation,
it rediscovers the pool and refuses execution if ownership or membership
changed after planning.

The default workflow cordons every member, evicts eligible pods, and routes
deletion to the owner:

- managed pool: OKE `DeleteNodePool`
- self-managed RDMA pool: Compute Management `TerminateClusterNetwork`
- standalone Instance Pool: Compute Management `TerminateInstancePool`

Deletion refuses autoscaler-owned and Slinky-managed pools. `oke-system` also
requires a dedicated override. If execution fails before the OCI request is
submitted, nodes cordoned by the operation are uncordoned.

Cluster Network discovery also records the embedded Instance Configuration.
After a waited deletion, the tool removes that configuration only when both the
pool and configuration carry the `mgmt-oke-created=true` ownership tag.
Configurations owned by the OCI HPC OKE stack are not deleted. A no-wait
deletion reports the retained configuration for manual cleanup.

### Boot Volume Replacement

OKE exposes two different BVR contracts, and the CLI keeps them separate.

`nodes boot-volume-replace` accepts specific managed or self-managed workers.
Preparation confirms the cluster is enhanced, ownership is stable, and
eviction admission succeeds. A selected worker can be NotReady when BVR is
being used as a repair. The plan records the compute instance OCID, internal
IP, Kubernetes boot ID, and current boot volume OCID. Execution invokes OKE
`ReplaceBootVolumeClusterNode`; it does not call Compute boot volume
replacement directly.

The individual OKE API accepts node eviction settings but no image or other
node-property update. OKE preserves the existing image, cloud-init, bootstrap,
networking, and node configuration.

`pools boot-volume-replace` is limited to fully healthy managed OKE node pools.
It builds
`UpdateNodePoolDetails` with
`NodePoolCyclingDetails(cycle_modes=["BOOT_VOLUME_REPLACE"])` and at least one
supported update:

- image ID
- boot volume size
- boot volume KMS key
- Kubernetes version
- node metadata
- SSH public key

Image preparation verifies shape availability, VCN consistency, Linux-only
operation, and the same Linux distribution as the current image. Boot volume
size reduction and reserved OKE metadata changes are refused.

Both workflows perform eviction dry-runs, require explicit acknowledgement for
`emptyDir` data or unmanaged pods, protect Cluster Autoscaler and Slinky
ownership, and rediscover state under the mutation Lease before submission.

The BVR waiter verifies the OKE work request, unchanged instance OCID and
internal IP, changed boot volume OCID, changed Kubernetes boot ID when
available, Ready and schedulable state, complete pool counts, GPU allocation,
RDMA topology, and applicable Network Operator VFs. The managed-pool waiter
also verifies the requested node-pool properties from OKE.

## Kubernetes Upgrade Architecture

Upgrade discovery extends the regular snapshot with:

- OKE control-plane version, lifecycle state, cluster type, ETag, and
  advertised Kubernetes targets
- virtual node-pool versions and lifecycle state
- declared node-pool Kubernetes versions and actual kubelet versions
- installed and selected OKE add-on versions plus automatic or pinned update
  mode
- Kueue ClusterQueue stop policy and admitted workload count
- Slinky ownership and read-only `slurmctld` verification targets
- Cluster Network, Instance Pool, Compute Cluster, and GPU Memory Cluster
  ownership and Instance Configuration references

A major/minor target is resolved against the OKE-advertised list to the latest
production patch. Typed version validation rejects unsupported exact patches,
preview `.0` targets without acknowledgement, downgrades, control-plane minor
jumps, and invalid worker skew.

### Planning And Ordering

`upgrades status`, `upgrades plan`, and `--dry-run` do not acquire a persistent
checkpoint or mutate OCI or Kubernetes. Planning still performs all available
discovery, add-on option queries, image validation, workload evidence
collection, and self-managed bootstrap transformation.

The full plan places all control-plane steps first. Worker order is stable:
CPU canary, system, regular GPU, managed Compute Cluster RDMA, legacy Cluster
Network RDMA, GPU Memory Cluster, then custom pools. An explicit order must name
every pool exactly once.

### Workload Verification Boundary

The upgrade workflow deliberately has no dependency on the lifecycle
workflow's cordon, uncordon, or eviction methods. Its Kubernetes backend calls
are limited to discovery, checkpoint, Lease, and read-only Slinky exec.

Pool evidence requires:

- Ready Kubernetes nodes
- `spec.unschedulable=true` from an external cordon
- no ordinary workload pod after excluding DaemonSets, static mirror pods, and
  recognized scheduler infrastructure
- matching Kueue ClusterQueues in `Hold` or `HoldAndDrain` with zero admitted
  workloads
- Slinky partitions and nodes in safe states, with no active job referencing
  the target nodes

Positive evidence remains blocking. The emergency acknowledgement is evaluated
only when API, RBAC, or exec failure makes verification unavailable.

### Control-Plane Path

Immediately before `UpdateCluster`, the workflow rediscovers the cluster under
the mutation Lease, confirms every worker is Ready, revalidates version policy
and add-on compatibility, and sends the current cluster ETag. It records the
work request before waiting for target-version `ACTIVE` state. OKE-managed
virtual pools and automatic add-ons must converge after the control plane.

`clusters upgrade` executes exactly one patch or minor step.
`upgrades apply` can retain an ordered list of valid one-minor steps and advance
only after each observed control-plane state converges.

### Managed Worker Path

Standard CPU/GPU pools and Compute Cluster RDMA pools remain OKE-owned:

- `boot-volume-replace` submits `UpdateNodePool` with
  `cycleModes=["BOOT_VOLUME_REPLACE"]`
- `instance-replace` submits `UpdateNodePool` with
  `cycleModes=["INSTANCE_REPLACE"]`
- `blue-green` clones the full source node pool and uses a deterministic retry
  token for idempotent creation

The source node-pool ETag is required for cycling updates. Image overrides are
validated against shape, availability domains, and Linux distribution.
Compute Cluster placement and host-group details are preserved, while the
OKE-internal backing Instance Pool remains hidden and untouched.

### Self-Managed Worker Path

Legacy Cluster Networks, standalone Instance Pools, and GMCs first clone their
current Instance Configuration. The transformation uses structured SDK models
and multipart MIME parsing rather than text substitution. It preserves launch,
network, storage, agent, tag, SSH, FSS, Lustre, NVMe RAID, kubelet, and custom
metadata while updating the target image when requested, the live API endpoint,
the current cluster CA, and the Kubernetes bootstrap version.

The resulting configuration must prove both refreshed connection data and the
target version before it can be created. It is attached to the backend with the
current Cluster Network, Instance Pool, or GMC ETag before any worker cycles.

- self-managed BVR updates one instance at a time with `ALLOW_DOWNTIME`,
  preserves the previous boot volume and instance identity, and waits for the
  same instance to return at the target version
- self-managed instance replacement grows by one, verifies a new target worker,
  then terminates one externally drained old worker and restores desired size
- self-managed blue-green clones the complete parallel backend; GMC placement
  must be explicitly usable when the source fabric or Compute Cluster cannot be
  shared

Create operations use an operation-derived retry token, allowing resume to
reissue an ambiguous create safely.

### Checkpoint State Machine

Full orchestration writes one non-secret ConfigMap:

```text
kube-system/mgmt-oke-kubernetes-upgrade
```

It records operation ID, source and target versions, control-plane steps, pool
order, strategies, image and cycling overrides, phase, active work request,
created and retained resources, target and superseded Instance
Configurations, acknowledgements, and the last error.

Transitions are:

```text
planned -> control-plane -> worker-configs -> pool-gate
        -> pool-upgrade -> verify -> completed
```

`failed` retains observed state for `upgrades resume`. `abandoned` records that
orchestration stopped but does not imply rollback. Blue-green pauses within the
pool sequence as `action-required`, returns process status `3`, and retains the
source backend until external migration and drain are complete.

Every ConfigMap replacement supplies its latest `resourceVersion`. OCI updates
supply current ETags. Both concurrency controls are revalidated while holding
`kube-system/mgmt-oke-mutation`.

Cleanup is allowed only from `completed`. It deletes only superseded Instance
Configurations carrying this operation's ownership tags and then removes the
checkpoint. Stack-owned configurations, old blue-green backends, and retained
boot volumes are not generalized cleanup targets.

## Node And Resource Readiness

After a mutation with `--wait`, the tool reconciles the selected pool until:

- desired OCI size equals the target
- active OCI instance count equals the target when available
- Kubernetes Ready node count equals the target
- allocatable GPU count equals the target for GPU pools
- valid OCI RDMA topology labels exist on every Ready node for RDMA pools
- `nvidia.com/rdma-vf` is allocatable on every Ready RDMA node when the NVIDIA
  Network Operator add-on is active

Each wait poll also checks the mutation's work request through its owning
service. OKE work requests use the Container Engine API. Before a self-managed
Cluster Network or Instance Pool mutation, the tool snapshots existing
resource-scoped work requests through the generic Work Request API. If the
mutation response omits `opc-work-request-id`, any new request for that resource
is still monitored without confusing it with an older failure. A failed or
canceled request stops the wait immediately and reports the OCI error details;
state and resource convergence remain authoritative after the request succeeds.

Topology validation rejects missing values and sentinel values such as
`no-imds-data`, `unknown`, and `not-available`.

## Safety Boundaries

Before mutation, the CLI verifies pool ownership and backing identifiers. It
also refuses unsafe operations when:

- the pool is owned by Cluster Autoscaler
- a `--no-drain` target has workload pods and `--allow-workloads` is absent
- a drain would delete `emptyDir` data without `--delete-emptydir-data`
- a drain would evict an unmanaged pod without `--force`
- a requested target size is negative
- a scale-down or node removal targets a detected Slinky-managed worker
- required OCI target information cannot be resolved

Every mutation is represented by an `OperationPlan`. `--dry-run` prints that
validated plan and stops before acquiring the Lease or changing Kubernetes or
OCI. Actual mutations require `--yes` or an interactive typed confirmation.

Actual mutations acquire the `kube-system/mgmt-oke-mutation` Lease by default.
An active holder causes the second operation to fail before mutation. The Lease
duration covers the requested drain or convergence timeout and is released when
the operation exits. `--no-lock` is an explicit recovery override.

Direct OCI mutations warn that Terraform and OCI Resource Manager inputs are
not updated. This is a drift warning, not an automated stack-state change.

Discovery, topology, add-on status, autoscaler, status, health,
recommendations, and reconciliation commands are read-only.

## Health Model

`status`, `health run`, `recommendations list`, and `addons validate` share one
pure evaluation engine. It does not execute commands on worker nodes. Checks
are derived from the discovery snapshot and include:

- desired, active OCI, and Ready Kubernetes pool convergence
- Kubernetes node Ready and schedulable state
- allocatable NVIDIA or AMD GPU resources
- required OCI RDMA topology labels
- `nvidia.com/rdma-vf` when Network Operator is active or the pool requires VFs
- OKE add-on lifecycle and expected accelerator add-ons
- Cluster Autoscaler, Kueue, and Slinky metadata

Health commands return `0` for pass or informational results, `1` when warnings
exist, and `2` when a check fails.
