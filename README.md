# OKE HPC Node Management Tool

Management CLI for OCI HPC OKE clusters.

This tool provides inventory, safety visibility, and guarded node-pool
management for OCI HPC OKE clusters:

- discover managed OKE node pools, including Compute Cluster placement `[Implemented]`
- discover the OKE cluster OCID and region from kubeconfig, then resolve the
  compartment through OKE `GetCluster` `[Implemented]`
- discover RDMA cluster-network-backed instance pools `[Implemented]`
- suppress OKE-internal backing instance pools from standalone pool inventory `[Implemented]`
- discover Kubernetes nodes `[Implemented]`
- join Kubernetes nodes to OCI instances where possible `[Implemented]`
- show GPU allocatable resources `[Implemented]`
- validate OCI RDMA topology labels and reject missing or sentinel values `[Implemented]`
- show OKE add-on lifecycle state and installed versions `[Implemented]`
- require `nvidia.com/rdma-vf` readiness when the NVIDIA Network Operator add-on is active `[Implemented]`
- show Cluster Autoscaler pool ownership `[Implemented]`
- show Kueue resource counts and ResourceFlavor-to-pool matches `[Implemented]`
- discover Slinky Slurm node aliases and protect Slinky workers from unsafe removal `[Implemented]`
- create managed CPU and GPU node pools or self-managed RDMA Cluster Network
  pools from proven stack pool templates, with custom image, shape, placement,
  network, boot, Kubernetes, label, tag, and lifecycle settings `[Implemented]`
- compose the official OCI HPC OKE worker bootstrap for existing FSS and Lustre
  mounts and local NVMe RAID `[Implemented]`
- discover control-plane, virtual-pool, declared worker, and actual kubelet
  versions together with OKE-advertised upgrade targets `[Implemented]`
- resolve a minor target to the latest supported production patch and validate
  one-minor-at-a-time sequencing, skew, preview, downgrade, add-on, Kueue, and
  Slinky constraints `[Implemented]`
- plan and execute checkpointed Kubernetes upgrades across managed CPU/GPU,
  Compute Cluster RDMA, legacy Cluster Network, standalone Instance Pool, and
  GPU Memory Cluster worker backends `[Implemented]`
- support boot-volume-replace, instance-replace, and blue-green worker upgrade
  strategies without cordoning, draining, evicting, or uncordoning workloads
  from the upgrade subsystem `[Implemented]`
- resize standard and Compute Cluster-backed OKE node pools and self-managed cluster-network pools `[Implemented]`
- drain and delete complete managed OKE, Cluster Network, or standalone Instance
  Pool worker pools `[Implemented]`
- add or remove pool capacity with explicit `pools add` and `pools remove` commands `[Implemented]`
- preview every mutation as a validated operation plan with `--dry-run` `[Implemented]`
- serialize concurrent mutations with a Kubernetes Lease `[Implemented]`
- cordon, drain, and uncordon selected Kubernetes workers `[Implemented]`
- remove, replace, or terminate one or more managed or self-managed workers `[Implemented]`
- replace the boot volume of a specific managed or self-managed worker while
  preserving its instance identity and existing node configuration `[Implemented]`
- roll every worker in a managed OKE pool through boot volume replacement while
  updating supported properties such as its image `[Implemented]`
- select nodes by identifiers or exact operational fields `[Implemented]`
- project and sort node output for shell and automation workflows `[Implemented]`
- wait for OCI, Kubernetes, GPU, RDMA topology, and applicable RDMA VF readiness `[Implemented]`
- report concise cluster status and deterministic node, pool, GPU, RDMA, add-on, and scheduler health `[Implemented]`
- derive actionable recommendations from failed or degraded health checks `[Implemented]`
- validate OKE accelerator add-ons against discovered GPU and RDMA capacity `[Implemented]`

Inventory, status, health, recommendation, topology, autoscaler, add-on status,
upgrade status and planning, and reconciliation commands are read-only. Pool
creation, deletion, capacity, boot volume replacement, node termination, and
upgrade execution commands mutate OCI resources. Node maintenance commands
mutate Kubernetes. Upgrade commands never cordon, drain, evict, or uncordon
workers; operators prepare workloads externally and provide a separate safety
attestation. Every mutation supports `--dry-run`, uses a Kubernetes Lease by
default, and requires either `--yes` or an interactive confirmation.

## Install

For controller/operator node installation, use
[`docs/controller-install.md`](docs/controller-install.md).

From the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install .
```

Python 3.9 or newer is supported.

See [`docs/architecture.md`](docs/architecture.md) for the target-discovery
algorithm and the API routing used for managed Compute Cluster and legacy
Cluster Network worker pools.

This installs two entrypoints backed by the same code:

```bash
mgmt-oke --help
kubectl-oke --help
```

For kubectl plugin usage, make sure `kubectl-oke` is on `PATH`, then run:

```bash
kubectl oke --help
```

## Usage

The tool can be invoked directly as `mgmt-oke` or as a kubectl plugin:

```bash
mgmt-oke [global options] <command> [command options]
kubectl oke [global options] <command> [command options]
```

Display help:

```bash
mgmt-oke -h
mgmt-oke pools -h
mgmt-oke nodes -h
kubectl oke -h
```

Global options:

| Option | Description |
| --- | --- |
| `--version` | Print the tool version. |
| `--compartment-id <ocid>` | Optional compartment override. By default, the compartment is read from the OKE cluster. |
| `--cluster-id <ocid>` | Optional OKE cluster override. By default, the cluster OCID is read from kubeconfig. |
| `--region <region>` | Optional region override. By default, the region is read from kubeconfig. |
| `--auth config_file\|instance_principal\|resource_principal\|none` | OCI API authentication method. Use `none` to disable OCI API calls; kubeconfig authentication remains independent. |
| `--oci-config-file <path>` | OCI config file path when using config-file authentication. |
| `--oci-profile <profile>` | OCI config profile when using config-file authentication. |
| `--kubeconfig <path>` | kubeconfig path used for Kubernetes access and OKE target discovery. |
| `--context <name>` | Explicit kubeconfig context override for troubleshooting. The current or only unambiguous context is used by default. |
| `--in-cluster` | Use Kubernetes in-cluster configuration. |
| `--skip-oci` | Skip OCI discovery. |
| `--skip-kubernetes` | Skip Kubernetes discovery. |
| `--format table\|json\|csv` | Output format. `table` is the default. |
| `--debug` | Print an exception traceback when troubleshooting a failed command. |

Command groups:

| Command | Description |
| --- | --- |
| `pools list` | List discovered worker pools. |
| `pools get <pool>` | Get one worker pool by name or OCID. |
| `pools create <name> --type <cpu\|gpu\|rdma> --count <n>` | Create a managed CPU/GPU pool or self-managed RDMA Cluster Network pool from an existing stack pool template. |
| `pools delete <pool>` | Drain workers and delete the complete owning pool resource. |
| `pools resize <pool> (--size <n> \| --delta <n>)` | Resize a managed OKE node pool, cluster network, or instance pool. |
| `pools add <pool> --count <n>` | Add `n` workers to desired pool capacity. |
| `pools remove <pool> --count <n>` | Remove `n` workers from desired pool capacity; OCI selects the workers. |
| `pools boot-volume-replace <pool> <property-update>` | Replace every managed worker boot volume while applying a supported image, boot, Kubernetes, metadata, or SSH-key update. |
| `pools upgrade <pool> --to <version> --strategy <strategy>` | Upgrade one externally prepared worker pool and wait for complete convergence. |
| `nodes list` | List and filter Kubernetes nodes. |
| `nodes get <identifier...>` | Get nodes by Kubernetes name, Slurm name, internal IP, provider ID, or instance OCID. |
| `nodes terminate <identifier...>` | Drain and terminate selected managed or self-managed workers. |
| `nodes remove <identifier...>` | Compatibility alias for `nodes terminate`. |
| `nodes boot-volume-replace <identifier...>` | Replace selected managed or self-managed worker boot volumes while preserving the current image and configuration. |
| `nodes cordon <identifier...>` | Mark selected workers unschedulable. |
| `nodes drain <identifier...>` | Cordon workers and evict non-DaemonSet pods through the Eviction API. |
| `nodes uncordon <identifier...>` | Mark selected workers schedulable. |
| `topology list` | Group nodes by RDMA topology labels. |
| `autoscaler status` | Show Cluster Autoscaler pool ownership. |
| `addons status` | Show OKE add-on lifecycle state and installed versions. |
| `addons validate` | Validate NFD, GPU Operator, Network Operator, GPU, and RDMA readiness. |
| `status` | Show concise cluster capacity and health. |
| `health run` | Run deterministic health checks by category or pool. |
| `recommendations list` | Show actionable warnings and failures. |
| `reconcile` | Show a full discovery snapshot. |
| `upgrades status [--to <version>]` | Show current and target versions, add-on compatibility, scheduler state, and supported strategies. |
| `upgrades plan --to <version>` | Build the complete ordered control-plane and worker plan without mutation. |
| `upgrades apply --to <version>` | Run checkpointed full-cluster orchestration after all safety acknowledgements. |
| `upgrades resume\|abandon\|cleanup` | Resume observed state, abandon checkpoint state without rollback, or delete operation-owned superseded configurations after success. |
| `clusters upgrade --to <version>` | Execute one valid OKE control-plane patch or minor upgrade step. |
| `clusters list\|create\|delete\|add node` | Slurm-style compatibility aliases for worker-pool lifecycle commands. These aliases do not create or delete the OKE control plane. |

Pool creation options:

| Option | Description |
| --- | --- |
| `--type cpu\|gpu\|rdma` | Required pool backend. CPU and GPU create managed OKE node pools; RDMA creates a self-managed Cluster Network. |
| `--count <n>` | Initial worker count. The value must be at least one. |
| `--from-pool <pool>` | Select the source template. If omitted, the matching conventional pool (`oke-cpu`, `oke-gpu`, or `oke-rdma`) is used when present; otherwise the only eligible pool is used. |
| `--image-id`, `--shape`, `--availability-domain`, `--subnet-id` | Override the inherited worker image, shape, placement, or primary subnet. |
| `--pod-subnet-id`, `--node-nsg-id`, `--pod-nsg-id` | Override worker and VCN-native pod networking. Repeat where supported. |
| `--boot-volume-*`, `--ocpus`, `--memory-in-gbs` | Override boot-volume and Flex-shape settings. |
| `--kubernetes-version`, `--max-pods-per-node`, `--node-label`, `--node-metadata` | Override worker bootstrap and Kubernetes registration settings. |
| `--storage-mode inherit\|append\|replace` with `--nvme-*`, `--fss-*`, or `--lustre-*` | Preserve inherited storage bootstrap by default, or compose selected official OCI HPC OKE storage scripts. |

Run `mgmt-oke pools create --help` for the complete authoritative option list.
See [Creating Worker Pools](docs/creating-worker-pools.md) and
[Worker Bootstrap and Storage](docs/worker-bootstrap-and-storage.md) for
backend-specific examples and prerequisites.

Pool resize options:

| Option | Description |
| --- | --- |
| `--size <n>` | Set the worker pool to an exact desired size. |
| `--delta <n>` | Change the current desired size by `n`. Positive values add nodes; negative values remove nodes. For example, `--delta 2` adds two nodes and `--delta -1` removes one node. |

Shared pool mutation options:

| Option | Description |
| --- | --- |
| `--wait` | Monitor the submitted OCI work request and wait for target OCI and Kubernetes counts. OCI failures are reported immediately. GPU/RDMA pools also wait for allocatable GPUs and valid RDMA topology; when `NvidiaNetworkOperator` is active, RDMA pools also wait for `nvidia.com/rdma-vf`. |
| `--timeout <seconds>` | Maximum seconds to wait. Default: `1800`. |
| `--poll-interval <seconds>` | Wait polling interval. Default: `30`. |
| `--dry-run` | Validate ownership and safety, then print the operation plan without mutation. |
| `--lock` / `--no-lock` | Use or bypass the Kubernetes mutation Lease. Locking is enabled by default. |
| `--yes` | Do not prompt for confirmation. |

Node removal options:

| Option | Description |
| --- | --- |
| `--keep-size` | Delete the node but keep the pool size so the backing pool replaces it. |
| `--drain` / `--no-drain` | Cordon and evict pods before termination. Drain is enabled by default. |
| `--allow-workloads` | Allow `--no-drain` termination when workload pods are present. |
| `--delete-emptydir-data` | Acknowledge deletion of pod-local `emptyDir` data during drain. |
| `--force` | Allow drain of pods without a controller. |
| `--grace-period <seconds>` | Pod termination grace period. Default: `30`. |
| `--drain-timeout <seconds>` | Maximum time for Kubernetes eviction. Default: `600`. |
| `--eviction-grace <duration>` | Managed OKE node eviction grace duration. Default: `PT10M`. |
| `--force-after-grace` | For managed OKE pools, force compute deletion if pods cannot be evicted before the grace duration expires. |
| `--wait` | Monitor submitted OCI work requests, then wait until the selected node is absent and the pool has converged, including applicable GPU, RDMA topology, and RDMA VF readiness. |
| `--timeout <seconds>` | Maximum seconds to wait. Default: `1800`. |
| `--poll-interval <seconds>` | Wait polling interval. Default: `30`. |
| `--dry-run` | Validate selection, ownership, drain constraints, and eviction admission without mutation. |
| `--lock` / `--no-lock` | Use or bypass the Kubernetes mutation Lease. Locking is enabled by default. |
| `--yes` | Do not prompt for confirmation. |

Boot volume replacement commands require an enhanced OKE cluster. Individual
node BVR preserves the current image and node configuration. Managed-pool BVR
requires at least one supported update and accepts `--image-id`,
`--boot-volume-size`, `--boot-volume-kms-key-id`, `--kubernetes-version`,
`--node-metadata`, or `--ssh-public-key-file`. Both paths preflight eviction,
preserve compute instance identity, and verify replacement boot volume, node,
GPU, and RDMA readiness with `--wait`.

See [Replacing Worker Boot Volumes](docs/replacing-worker-boot-volumes.md) for
the complete behavior, safety options, and image constraints.

## Authentication

The tool has two discovery sources:

- Kubernetes API, using kubeconfig or in-cluster config
- OCI API, using config-file auth, instance principals, or resource principals

On an OKE HPC operator host with `kubectl` configured for the cluster, no OCI
resource OCIDs are required on the command line:

```bash
mgmt-oke --auth instance_principal pools list
```

The tool reads the OKE cluster OCID and region from the OCI CLI exec arguments
in the selected kubeconfig context. It then calls the OCI OKE `GetCluster` API
and reads the cluster's compartment OCID. Selection uses `--context` when
provided, then `current-context`, then the only unambiguous cluster in
kubeconfig.

The resulting flow is:

```text
selected kubeconfig context
  -> cluster OCID and region
  -> OKE GetCluster(cluster OCID)
  -> compartment OCID
  -> worker-pool and add-on discovery
```

When `--auth instance_principal` or `--auth resource_principal` is selected, the
tool also supplies that authentication method to the kubeconfig OCI CLI exec
plugin unless `OCI_CLI_AUTH` is already set explicitly.

Explicit resource-target options and their environment-variable equivalents
take precedence over automatic discovery. They remain available for in-cluster
execution and other nonstandard environments. Kubeconfig context selection has
no environment-variable override.

For Kubernetes-only discovery on an operator host, retain authentication for
the kubeconfig exec plugin and skip OCI inventory calls:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list
```

Use `--auth none` when OCI API discovery is disabled and the selected
kubeconfig does not need the tool to supply an OCI CLI authentication method.

Optional environment defaults and overrides:

```bash
export OCI_AUTH=instance_principal
export OCI_CLI_AUTH=instance_principal
export KUBECONFIG=$HOME/.kube/config
export OCI_COMPARTMENT_ID=ocid1.compartment.oc1..example
export OKE_CLUSTER_ID=ocid1.cluster.oc1.iad.example
export OCI_REGION=us-ashburn-1
```

## Commands

### Full snapshot

`reconcile` is read-only. It combines worker-pool and Kubernetes node inventory,
OKE add-on status, Cluster Autoscaler ownership, GPU/RDMA capability status,
Slinky aliases, and Kueue resource counts in one snapshot.

```bash
mgmt-oke reconcile
mgmt-oke --format json reconcile
```

### Worker pools

```bash
mgmt-oke pools list
mgmt-oke pools get oke-rdma
mgmt-oke --format json pools list
```

`pools list` and `pools get` use a fast inventory path. They do not scan
workload pod counts, Cluster Autoscaler deployments, or Kueue resources. Use
`reconcile` when those cross-system correlations are required.

OCI HPC OKE v26.7 deploys GPU with RDMA workers as managed OKE node pools
placed in Compute Clusters by default. Legacy deployments can expose a
self-managed Cluster Network with an embedded Instance Pool. The tool supports
both ownership models:

| Pool model | Inventory | Create operation | Resize operation | Specific node removal |
| --- | --- | --- | --- | --- |
| Standard managed OKE node pool | `kind=node-pool`, `placement=standard` | OKE `CreateNodePool` from a matching CPU/GPU source | OKE `UpdateNodePool` with only the desired size | OKE `DeleteNode` |
| Managed OKE node pool placed in a Compute Cluster | `kind=node-pool`, `placement=compute-cluster` | Not created by this command; `--type rdma` intentionally creates the self-managed Cluster Network model | OKE `UpdateNodePool` with only the desired size | OKE `DeleteNode` |
| Self-managed Cluster Network instance pool | `kind=cluster-network`, `placement=cluster-network` | Compute Management `CreateInstanceConfiguration` and `CreateClusterNetwork` from an existing pool template | Compute Management `UpdateClusterNetwork` | Instance-pool detach with automatic termination |
| Standalone Instance Pool | `kind=instance-pool` | Not supported | Compute Management `UpdateInstancePool` | Instance-pool detach with automatic termination |

The Compute Cluster is placement metadata for a managed OKE pool. The tool does
not resize or detach its OKE-internal backing Instance Pool directly. Discovery
suppresses that internal resource from standalone pool output, preventing one
managed RDMA pool from appearing twice.

Specific-node BVR is routed through OKE for every managed and self-managed pool
model. Pool-wide BVR with image or property updates is available only for
managed OKE node pools, including Compute Cluster-backed RDMA pools.

Create a managed CPU pool:

```bash
mgmt-oke pools create cpu-batch \
  --type cpu \
  --count 2 \
  --from-pool oke-cpu \
  --shape VM.Standard.E5.Flex \
  --ocpus 16 \
  --memory-in-gbs 128 \
  --dry-run
```

Create a managed GPU pool with a custom image in a selected availability domain:

```bash
mgmt-oke pools create gpu-training \
  --type gpu \
  --count 2 \
  --from-pool oke-gpu \
  --availability-domain <availability-domain> \
  --shape VM.GPU.A10.1 \
  --image-id <custom-image-ocid> \
  --subnet-id <worker-subnet-ocid> \
  --dry-run
```

Create a self-managed RDMA Cluster Network pool:

```bash
mgmt-oke pools create rdma-training \
  --type rdma \
  --count 2 \
  --from-pool oke-rdma \
  --availability-domain <availability-domain> \
  --shape BM.GPU4.8 \
  --image-id <custom-image-ocid> \
  --subnet-id <worker-subnet-ocid> \
  --dry-run
```

Creation always inherits a working OKE bootstrap from a matching source pool.
The selected overrides are applied, the new Kubernetes pool identity is
retargeted, and the source remains unchanged. CPU and GPU use OKE
`CreateNodePool`; RDMA derives a new Instance Configuration and uses Compute
Management `CreateClusterNetwork`. Apply a reviewed plan by removing
`--dry-run`, adding `--wait`, and completing the typed confirmation.

Delete a complete pool after reviewing its drain and ownership plan:

```bash
mgmt-oke pools delete gpu-training --dry-run --format json
mgmt-oke pools delete gpu-training --wait
```

For an RDMA pool created by `mgmt-oke`, `--wait` also removes the derived
Instance Configuration after the Cluster Network terminates. Stack-owned
Instance Configurations are preserved.

See [Creating Worker Pools](docs/creating-worker-pools.md) for complete examples,
validation behavior, and infrastructure-as-code ownership guidance.

Replace all boot volumes in a managed GPU pool while applying a compatible new
image:

```bash
mgmt-oke pools boot-volume-replace oke-gpu \
  --image-id <replacement-image-ocid> \
  --maximum-unavailable 1 \
  --dry-run \
  --format json
mgmt-oke pools boot-volume-replace oke-gpu \
  --image-id <replacement-image-ocid> \
  --maximum-unavailable 1 \
  --wait
```

The image must use the same Linux distribution as the current image and remain
compatible with the pool shape and availability domains. Direct node-pool
property changes must also be reconciled with Terraform or OCI Resource
Manager inputs.

Add one node to a pool:

```bash
mgmt-oke pools add oke-cpu --count 1 --wait --yes
```

Remove one node from a pool:

```bash
mgmt-oke pools remove oke-cpu --count 1 --wait --yes
```

The signed `--delta` form remains available: positive values add capacity and
negative values remove capacity.

Preview the same change without mutating OCI:

```bash
mgmt-oke pools resize oke-cpu --delta 1 --dry-run --format json
```

Example output:

```json
[
  {
    "current_size": 1,
    "decrement_size": null,
    "operation": "pool-resize",
    "owner": "oke",
    "pool": "oke-cpu",
    "status": "planned",
    "steps": ["update the managed OKE node-pool desired size"],
    "target": "oke-cpu",
    "target_size": 2,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Pool-level scale-down changes desired capacity but does not select which worker
OCI removes. Use `nodes terminate` when a particular worker must be removed.

Set a pool to an exact desired size:

```bash
mgmt-oke pools resize oke-cpu --size 3 --wait --yes
```

The same resize command applies to managed Compute Cluster-backed RDMA pools and
legacy self-managed RDMA Cluster Network pools. Discovery selects the correct
OCI API from the pool ownership metadata:

```bash
mgmt-oke pools resize oke-rdma --delta 1 --wait --yes
```

For a managed Compute Cluster-backed pool, OKE creates the new node and places
it in the existing Compute Cluster. For a legacy Cluster Network pool, Compute
Management increases the size of the existing embedded Instance Pool; its
existing Instance Configuration supplies cloud-init and bootstrap data.

### Nodes

```bash
mgmt-oke nodes list
mgmt-oke nodes list --pool oke-rdma
mgmt-oke nodes list --rdma-only
mgmt-oke nodes get 10.0.127.32
mgmt-oke nodes get ocid1.instance.oc1.iad.example
mgmt-oke nodes get <slurm-node-name>
mgmt-oke nodes terminate 10.0.127.32 --wait --yes
mgmt-oke nodes terminate 10.0.127.32 --keep-size --wait --yes
mgmt-oke nodes boot-volume-replace 10.0.127.32 --dry-run
mgmt-oke nodes boot-volume-replace 10.0.127.32 --wait
```

`nodes remove` is an alias for `nodes terminate`.
`nodes bvr` and `nodes boot-volume-swap` are aliases for
`nodes boot-volume-replace`.

Node termination drains by default. Preflight lists pods, checks eviction
admission, rejects unacknowledged `emptyDir` data and unmanaged pods, then the
execution path cordons the node and uses the Kubernetes Eviction API before
calling the owning OCI service. Use `--no-drain` only for an intentionally
pre-drained node.

Preview a replacement plan:

```bash
mgmt-oke nodes terminate <node-name> --keep-size --dry-run
```

Operate on several explicitly selected nodes or an exact field selection:

```bash
mgmt-oke nodes cordon node-a node-b
mgmt-oke nodes drain --fields pool=oke-cpu,ready=true
mgmt-oke nodes uncordon --nodes node-a,node-b
```

Filter and shape inventory output:

```bash
mgmt-oke nodes list --not-ready
mgmt-oke nodes list --workloads --sort workload_pods,name
mgmt-oke nodes list --fields pool=oke-rdma,rdma=true \
  --columns name,status,shape,gpu,rdma_vf
mgmt-oke nodes list --pool oke-gpu --one-line
```

Replace a specific RDMA worker while keeping the pool at its current size:

```bash
mgmt-oke nodes terminate <rdma-node-name> --keep-size --wait --yes
```

Replace the boot volume of a specific managed or self-managed worker without
terminating its compute instance:

```bash
mgmt-oke nodes boot-volume-replace <node-name-or-ip> \
  --eviction-grace PT60M \
  --wait
```

OKE preserves the instance OCID and network address. Individual BVR also
preserves the current image and node configuration; use managed-pool BVR when a
new image must be applied.

Without `--keep-size`, the selected worker is removed and desired pool size is
decremented. With `--keep-size`, the selected worker is removed and the owning
service launches a replacement at the existing desired size.

For Slinky-managed pools, node removal, replacement, and pool scale-down are
refused because they require a Slurm-aware drain. Pool scale-up remains
available. `--allow-workloads` does not bypass this protection.

### Kubernetes upgrades

Inspect the complete version and compatibility state:

```bash
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades plan --to v1.36
mgmt-oke upgrades apply --to v1.36 --dry-run
```

A major/minor target resolves to the latest production patch advertised by
OKE. Preview `.0` targets require `--allow-preview`. Downgrades, unsupported
minor jumps, worker versions newer than the control plane, incompatible pinned
add-ons, and invalid kubelet skew are refused.

The default full-cluster order is control plane, CPU canary, system, regular
GPU, managed Compute Cluster RDMA, legacy Cluster Network RDMA, GPU Memory
Cluster, then custom pools. `auto` selects managed boot-volume replacement and
self-managed instance replacement. Explicit
`boot-volume-replace`, `instance-replace`, and `blue-green` strategies are also
available.

Upgrade commands never cordon, drain, evict, or uncordon workers. The operator
must prepare Kubernetes, Kueue, and Slinky workloads externally. Execution
requires application-compatibility and IaC-drift acknowledgements plus a
separate workload-drained attestation for worker pools; `--yes` does not replace
them. The emergency attestation applies only when verification is unavailable
and never bypasses positively detected workloads.

Preview one control-plane step or one pool:

```bash
mgmt-oke clusters upgrade --to v1.36 --dry-run
mgmt-oke pools upgrade oke-gpu \
  --to v1.36.1 \
  --strategy boot-volume-replace \
  --dry-run
```

Full execution stores a resumable, resource-version-protected ConfigMap in
`kube-system`, revalidates OCI ETags under the mutation Lease, waits after
every mutation, and supports `upgrades resume`, `upgrades abandon`, and
ownership-checked `upgrades cleanup`.

See [Kubernetes Upgrades](docs/kubernetes-upgrades.md) for the complete strategy
matrix, workload gate, custom-image behavior, recovery model, and sanitized
live validation output.

### Status, health, and recommendations

```bash
mgmt-oke status
mgmt-oke health run
mgmt-oke health run --type discovery
mgmt-oke health run --type rdma --pool oke-rdma
mgmt-oke recommendations list
```

Example `mgmt-oke status` output:

```text
overall  pools  nodes  ready  not_ready  gpu_nodes  rdma_nodes  addons_active  addons_total  autoscaler_pools  slinky_nodes  kueue_flavors
-------  -----  -----  -----  ---------  ---------  ----------  -------------  ------------  ----------------  ------------  -------------
HEALTHY  4      6      6      0          3          2           7              7             0                 0             2
```

Health checks are deterministic evaluations of the discovered control-plane
and node state. They do not run arbitrary commands on workers. `status`,
`health run`, and `addons validate` return `0` when healthy, `1` when degraded,
and `2` when a check fails or the command cannot complete. A partial OCI or
Kubernetes discovery is degraded rather than being presented as healthy.

### RDMA topology

```bash
mgmt-oke topology list
mgmt-oke topology list --pool oke-rdma
```

### Cluster Autoscaler

```bash
mgmt-oke autoscaler status
```

### OKE add-ons

```bash
mgmt-oke addons status
mgmt-oke --format json addons status
mgmt-oke addons validate --target gpu
mgmt-oke addons validate --target rdma --pool oke-rdma
```

The add-on view is read-only. When `NvidiaNetworkOperator` is active, readiness
checks for RDMA pools include the `nvidia.com/rdma-vf` allocatable resource in
addition to GPU and OCI RDMA topology readiness.

## Resource Ownership

Pool creation, deletion, capacity changes, and node termination update live OCI
resources. Before confirmation, the CLI reports that these direct mutations do
not update Terraform or OCI Resource Manager state or input values. Import,
declare, or remove the corresponding resources and variables before a later
stack apply.

All mutations acquire the `kube-system/mgmt-oke-mutation` Lease by default.
This prevents two tool processes from changing cluster capacity or node state
concurrently. `--no-lock` is available for recovery when Kubernetes Lease
access is intentionally unavailable.

## Output Formats

All commands support:

```bash
--format table
--format json
--format csv
```

`table` is the default.

Node inventory also supports `--fields`, `--columns`, `--sort`, `--no-header`,
and `--one-line`. Machine-readable row schemas are versioned as `v1`; field
changes that break automation require a new schema version.

## Tests

After installing the package, run the unit-test suite from the project root:

```bash
python -m pytest
```

For lint and type checks, install the development tools and run:

```bash
python -m pip install ".[dev]"
ruff check src tests
mypy src tests
```

CI runs pytest on Python 3.9, 3.10, 3.11, and 3.12, followed by Ruff and mypy.

## Documentation

- [`docs/README.md`](docs/README.md): task-oriented documentation index
- [`docs/architecture.md`](docs/architecture.md): target discovery, ownership
  classification, mutation API routing, readiness, and safety boundaries
- [`docs/controller-install.md`](docs/controller-install.md): operator node
  prerequisites, installation, authentication, validation, and troubleshooting
- [`docs/command-reference.md`](docs/command-reference.md): complete command,
  selector, mutation-option, output, and exit-status reference
- [`docs/kubernetes-upgrades.md`](docs/kubernetes-upgrades.md): Kubernetes
  target selection, planning, workload gates, strategies, orchestration, and
  recovery
- [`docs/live-pool-creation-validation.md`](docs/live-pool-creation-validation.md):
  sanitized output from live managed GPU and self-managed RDMA creation validation
- [`docs/live-pool-deletion-validation.md`](docs/live-pool-deletion-validation.md):
  sanitized output from live managed GPU deletion and RDMA deletion planning
- [`docs/scope.md`](docs/scope.md): implemented features and planned items

## Cluster Validation

After installing the tool on a controller/operator node:

1. Confirm kubeconfig:

   ```bash
   kubectl get nodes -o wide
   ```

2. Confirm OCI auth:

   ```bash
   oci iam region list --auth instance_principal
   ```

3. Run Kubernetes-only discovery:

   ```bash
   mgmt-oke --auth instance_principal --skip-oci reconcile
   ```

4. Confirm OKE add-on status:

   ```bash
   mgmt-oke --auth instance_principal addons status
   ```

5. Run full OCI + Kubernetes discovery. The cluster, region, and compartment
   are resolved automatically:

   ```bash
   mgmt-oke --auth instance_principal reconcile
   ```
