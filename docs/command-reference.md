# Command Reference

`mgmt-oke` and `kubectl oke` are equivalent entrypoints. Global options must
appear before the command group; command-specific options appear after it.

```bash
mgmt-oke [global-options] <command> [command-options]
kubectl oke [global-options] <command> [command-options]
```

Use `-h` or `--help` at any level for the installed version's authoritative
syntax:

```bash
mgmt-oke --help
mgmt-oke pools --help
mgmt-oke nodes terminate --help
mgmt-oke nodes boot-volume-replace --help
```

Example top-level help excerpt:

```text
Usage: mgmt-oke [OPTIONS] COMMAND [ARGS]...

Commands:
  addons           Inspect and validate OKE accelerator add-ons.
  autoscaler       Inspect Cluster Autoscaler ownership of worker pools.
  clusters         Slurm-style aliases for OKE worker-pool lifecycle commands.
  health           Run deterministic AI/HPC readiness checks.
  nodes            Discover, maintain, replace, or terminate workers.
  pools            Manage the lifecycle of OCI HPC OKE worker pools.
  recommendations  Show actionable findings derived from cluster health.
  reconcile        Run full OCI and Kubernetes discovery.
  status           Show concise AI/HPC cluster health and capacity status.
  topology         Inspect OCI RDMA placement topology.
  upgrades         Plan, execute, resume, and audit Kubernetes upgrades.
```

## Global Options

| Option | Purpose |
| --- | --- |
| `--compartment-id <ocid>` | Override automatic compartment discovery. |
| `--cluster-id <ocid>` | Override the cluster OCID read from kubeconfig. |
| `--region <region>` | Override the region read from kubeconfig. |
| `--auth <method>` | Select `config_file`, `instance_principal`, `resource_principal`, or `none`. |
| `--oci-config-file <path>` | Select an OCI SDK configuration file. |
| `--oci-profile <name>` | Select an OCI SDK configuration profile. |
| `--kubeconfig <path>` | Select the kubeconfig used for Kubernetes and OKE target discovery. |
| `--context <name>` | Explicit troubleshooting override for kubeconfig context selection. |
| `--in-cluster` | Use Kubernetes service-account configuration. |
| `--skip-oci` | Disable OCI inventory and OCI-backed mutations. |
| `--skip-kubernetes` | Disable Kubernetes inventory and Kubernetes-backed operations. |
| `--format <format>` | Select `table`, `json`, or `csv`. |
| `--debug` | Print an exception traceback when a command fails. |

## Cluster Views

Show a concise capacity and health summary:

```bash
mgmt-oke status
```

Run full discovery and correlation:

```bash
mgmt-oke reconcile
mgmt-oke --format json reconcile
```

Example `status` output:

```text
overall  pools  nodes  ready  not_ready  gpu_nodes  rdma_nodes  addons_active  addons_total  autoscaler_pools  slinky_nodes  kueue_flavors
-------  -----  -----  -----  ---------  ---------  ----------  -------------  ------------  ----------------  ------------  -------------
HEALTHY  4      6      6      0          3          2           7              7             0                 0             2
```

`pools list` is the faster inventory path. `reconcile`, `status`, health, and
recommendation commands include the additional pod, autoscaler, Kueue, and
add-on correlations required by their checks.

## Worker Pools

```bash
mgmt-oke pools list
mgmt-oke pools get <pool-name-or-ocid>
```

Example pool inventory output:

```text
name        kind             placement        shape                desired  oci_active  k8s_ready  gpu             rdma
----------  ---------------  ---------------  -------------------  -------  ----------  ---------  --------------  ----
oke-rdma    cluster-network  cluster-network  BM.GPU4.8            2        2           2          nvidia.com/gpu  yes
oke-cpu     node-pool        standard         VM.Standard.E5.Flex  1        1           1          -               no
oke-gpu     node-pool        standard         VM.GPU.A10.1         1        1           1          nvidia.com/gpu  no
oke-system  node-pool        standard         VM.Standard.E5.Flex  2        2           2          -               no
```

Create a pool from a matching stack template:

```bash
mgmt-oke pools create <new-pool-name> \
  --type <cpu|gpu|rdma> \
  --count 2 \
  --from-pool <source-pool> \
  --dry-run
mgmt-oke pools create <new-pool-name> \
  --type <cpu|gpu|rdma> \
  --count 2 \
  --from-pool <source-pool> \
  --wait
```

`cpu` and `gpu` create managed OKE node pools. `rdma` defaults to a legacy
self-managed Cluster Network and derived Instance Configuration. Add
`--rdma-mode compute-cluster` to create a managed OKE RDMA node pool in an
existing or automatically created Compute Cluster.

```bash
mgmt-oke pools create <managed-rdma-pool> \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 1 \
  --from-pool <managed-gpu-or-rdma-source> \
  --availability-domain <availability-domain> \
  --shape <rdma-capable-bare-metal-gpu-shape> \
  --compute-cluster-name <new-compute-cluster-name> \
  --dry-run
```

The example creates a dedicated Compute Cluster. To use existing placement,
replace `--compute-cluster-name` with
`--compute-cluster-id <compute-cluster-ocid>`. Add
`--host-group-id <compute-host-group-ocid>` only when the workers must also use
an existing Compute Host Group. Custom images and backend-appropriate
placement, shape, networking, boot, Kubernetes, lifecycle, metadata,
cloud-init, FSS, Lustre, and NVMe settings are available. Unspecified values
are inherited from the matching source.

During migration from a legacy Cluster Network pool, retain the managed source
for OKE-owned fields and add the legacy bootstrap source:

```bash
mgmt-oke pools create <managed-rdma-pool> \
  --type rdma \
  --rdma-mode compute-cluster \
  --count 2 \
  --from-pool <managed-gpu-or-rdma-source> \
  --bootstrap-from-pool <legacy-cluster-network-pool> \
  --availability-domain <availability-domain> \
  --shape <rdma-capable-bare-metal-gpu-shape> \
  --compute-cluster-name <new-compute-cluster-name> \
  --dry-run \
  --format json
```

| Placement option | Purpose |
| --- | --- |
| `--rdma-mode cluster-network\|compute-cluster` | Select legacy self-managed or managed OKE RDMA. Default: `cluster-network`. |
| `--compute-cluster-id <ocid>` | Use an existing `ACTIVE` Compute Cluster. |
| `--compute-cluster-name <name>` | Name an automatically created Compute Cluster. |
| `--compute-cluster-compartment-id <ocid>` | Select the compartment for automatic Compute Cluster creation. |
| `--host-group-id <ocid>` | Use an existing `ACTIVE` Compute Host Group for the single selected placement. |
| `--bootstrap-from-pool <pool>` | Import cloud-init, bootstrap hooks, and non-reserved custom metadata from a legacy Cluster Network RDMA pool into managed Compute Cluster creation. |
| `--availability-domain <name>` | Select placement using a canonical tenancy-prefixed or display-form AD name. |

See [Creating Worker Pools](./creating-worker-pools.md) and
[Worker Bootstrap and Storage](./worker-bootstrap-and-storage.md) for the full
option matrix and examples. [Live Worker Pool Creation
Validation](./live-pool-creation-validation.md) contains sanitized output from
live managed GPU, managed Compute Cluster RDMA, and legacy RDMA operations.

Replace every boot volume in a managed pool while applying at least one
supported property update:

```bash
mgmt-oke pools boot-volume-replace <managed-pool> \
  --image-id <replacement-image-ocid> \
  --maximum-unavailable 1 \
  --dry-run \
  --format json
mgmt-oke pools boot-volume-replace <managed-pool> \
  --image-id <replacement-image-ocid> \
  --maximum-unavailable 1 \
  --wait
```

Supported updates are image ID, boot volume size, boot volume KMS key,
Kubernetes version, non-reserved node metadata, and SSH public key. The command
is limited to managed OKE node pools and enhanced clusters. It refuses image
changes across Linux distributions and boot volume size reductions.

| Option | Purpose |
| --- | --- |
| `--image-id <ocid>` | Apply a compatible image to every worker. |
| `--boot-volume-size <gib>` | Increase the worker boot volume size. |
| `--boot-volume-kms-key-id <ocid>` | Change boot volume encryption key. |
| `--kubernetes-version <version>` | Change the worker Kubernetes version. |
| `--node-metadata KEY=VALUE` | Merge a non-reserved metadata key; repeatable. |
| `--ssh-public-key-file <path>` | Apply the UTF-8 public key file. |
| `--maximum-unavailable <value>` | Set positive rolling parallelism as a count or percentage. Default: `1`. |
| `--delete-emptydir-data` | Acknowledge loss of pod-local data. |
| `--force` | Acknowledge eviction of unmanaged pods. |
| `--allow-system-pool` | Permit BVR of `oke-system` after explicit review. |

`pools bvr` is an alias. See
[Replacing Worker Boot Volumes](./replacing-worker-boot-volumes.md).

Delete an entire pool:

```bash
mgmt-oke pools delete <pool-name> --dry-run --format json
mgmt-oke pools delete <pool-name> --wait
```

Deletion drains by default. It refuses Cluster Autoscaler-owned and
Slinky-managed pools and protects `oke-system` unless
`--allow-system-pool` is selected explicitly.

For a Cluster Network created by this tool, `--wait` also removes its derived
Instance Configuration after termination. `--no-wait` retains and reports that
configuration. Stack-owned configurations are never removed by this cleanup.
See [Live Worker Pool Deletion
Validation](./live-pool-deletion-validation.md) for live managed deletion
output and the reviewed RDMA deletion plan.

Set an exact desired size:

```bash
mgmt-oke pools resize <pool-name> --size 3 --dry-run
mgmt-oke pools resize <pool-name> --size 3 --wait
```

Apply a signed change. Positive values add workers; negative values remove
capacity:

```bash
mgmt-oke pools resize <pool-name> --delta 2 --dry-run
mgmt-oke pools resize <pool-name> --delta -1 --dry-run
```

Use explicit add and remove aliases when signed arithmetic is unnecessary:

```bash
mgmt-oke pools add <pool-name> --count 2 --wait
mgmt-oke pools remove <pool-name> --count 1 --wait
```

Example add-capacity dry-run:

```bash
mgmt-oke pools add oke-cpu --count 1 --dry-run --format json
```

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

Pool-level scale-down delegates worker selection to OKE or Compute Management.
Use `nodes terminate` when the departing worker must be selected explicitly.

All pool mutations support:

| Option | Purpose |
| --- | --- |
| `--dry-run` | Validate and print a plan without changing OCI. |
| `--wait` | Monitor the OCI work request, including resource-scoped fallback when Compute omits its identifier, and wait for OCI, Kubernetes, GPU, and applicable RDMA convergence. |
| `--timeout <seconds>` | Set the convergence timeout. Default: `1800`. |
| `--poll-interval <seconds>` | Set the readiness polling interval. Default: `30`. |
| `--lock` / `--no-lock` | Enable or bypass the Kubernetes mutation Lease. |
| `--yes` | Skip interactive typed confirmation. |

Boot volume replacement commands use a `7200` second default timeout because
OKE must drain, stop, update, restart, and revalidate workers. Other pool
mutations retain the `1800` second default.

Whole-pool deletion additionally supports:

| Option | Purpose |
| --- | --- |
| `--drain` / `--no-drain` | Enable or bypass Kubernetes drain. Drain is the default. |
| `--allow-workloads` | Permit `--no-drain` while workload pods are present. |
| `--delete-emptydir-data` | Acknowledge deletion of pod-local data. |
| `--force` | Acknowledge eviction of pods without a controller. |
| `--allow-system-pool` | Permit deletion of `oke-system` after explicit review. |
| `--grace-period <seconds>` | Set pod termination grace. Default: `30`. |
| `--drain-timeout <seconds>` | Set the Kubernetes drain timeout. Default: `600`. |

## Slurm-Style Pool Aliases

The following aliases mirror the command style of the OCI HPC Slurm management
tool while retaining OKE worker-pool semantics:

```bash
mgmt-oke clusters list
mgmt-oke clusters create <name> --type <cpu|gpu|rdma> --count <n>
mgmt-oke clusters add node <pool> --count <n>
mgmt-oke clusters delete <pool>
```

These aliases create, add capacity to, and delete worker pools. They do not
create or delete the OKE control plane.

## Node Inventory

```bash
mgmt-oke nodes list
mgmt-oke nodes get <identifier> [<identifier> ...]
```

An identifier can be a Kubernetes node name, Slinky hostname, internal IP,
provider ID, or instance OCID.

Inventory filters can be combined:

```bash
mgmt-oke nodes list --pool <pool-name>
mgmt-oke nodes list --rdma-only
mgmt-oke nodes list --not-ready
mgmt-oke nodes list --workloads
mgmt-oke nodes list --fields pool=oke-rdma,ready=true,rdma=true
```

Valid exact-match `--fields` keys are:

```text
name, slurm_name, ip, status, pool, shape, ready, schedulable, gpu, rdma,
rdma_vf, workload_pods, slurm_pods, system_pods, daemonsets
```

Select and sort output fields:

```bash
mgmt-oke nodes list --columns name,status,pool,shape --sort pool,name
mgmt-oke nodes list --columns name,workload_pods --sort workload_pods,name --format csv
mgmt-oke nodes list --pool <pool-name> --one-line
mgmt-oke nodes list --no-header --columns name,ip
```

Example selected-column output:

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

Projected node output can include `ready` and `schedulable` independently of
the combined Kubernetes `status` field.

## Node Maintenance

Select nodes positionally, through repeatable or comma-separated `--nodes`, or
with exact `--fields`:

```bash
mgmt-oke nodes cordon <node-a> <node-b> --dry-run
mgmt-oke nodes cordon --nodes <node-a>,<node-b>
mgmt-oke nodes drain --fields pool=oke-cpu,ready=true --dry-run
mgmt-oke nodes uncordon <node-a> <node-b>
```

Example cordon dry-run output:

```bash
mgmt-oke nodes cordon gpu-node-1 --dry-run --format json
```

```json
[
  {
    "current_size": null,
    "decrement_size": null,
    "operation": "node-cordon",
    "owner": "kubernetes",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": ["set spec.unschedulable=true"],
    "target": "gpu-node-1",
    "target_size": null,
    "warnings": [],
    "workload_pods": 0
  }
]
```

`nodes drain` cordons the selected workers, ignores DaemonSet and mirror pods,
and evicts remaining pods through the Kubernetes `policy/v1` Eviction API.
PodDisruptionBudgets remain authoritative.

Drain refuses pod-local `emptyDir` data and pods without a controller unless
the operator acknowledges those conditions:

```bash
mgmt-oke nodes drain <node-name> --delete-emptydir-data --force
```

Node maintenance supports `--dry-run`, `--lock` / `--no-lock`, and `--yes`.
Drain also supports `--grace-period` and `--timeout`.

## Node Boot Volume Replacement

Replace the boot volume of a selected managed or self-managed worker:

```bash
mgmt-oke nodes boot-volume-replace <node-name-or-ip> --dry-run
mgmt-oke nodes boot-volume-replace <node-name-or-ip> --wait
```

The command invokes OKE `ReplaceBootVolumeClusterNode`. OKE cordons and drains
the worker, stops the existing compute instance, replaces its boot volume, and
restarts the same instance. The instance OCID, network address, image, and
existing node configuration are preserved.

An individual-node BVR does not accept a new image. Use
`pools boot-volume-replace --image-id` for a managed-pool image update.

Several nodes can be selected by identifiers or `--fields`. Multiple BVR
requests require `--wait` and execute sequentially:

```bash
mgmt-oke nodes boot-volume-replace \
  --nodes <node-a>,<node-b> \
  --wait
```

| Option | Purpose |
| --- | --- |
| `--eviction-grace <duration>` | Set OKE drain grace from `PT0M` through `PT60M`. Default: `PT60M`. |
| `--force-after-grace` | Continue after the grace period even if drain has not completed. |
| `--delete-emptydir-data` | Acknowledge loss of pod-local data. |
| `--force` | Acknowledge eviction of unmanaged pods. |
| `--allow-system-pool` | Permit BVR of an `oke-system` worker after explicit review. |
| `--wait` | Verify work request, instance identity, new boot volume, Ready state, GPU, and RDMA recovery. |
| `--timeout <seconds>` | Set the per-node convergence timeout. Default: `7200`. |

`nodes bvr` and `nodes boot-volume-swap` are aliases.

Example dry-run output:

```text
operation                         target      pool     owner  current_size  target_size  status
--------------------------------  ----------  -------  -----  ------------  -----------  -------
node-boot-volume-replace          gpu-node-1  oke-gpu  oke    1             1            planned
```

## Node Termination And Replacement

`nodes remove` is an alias for `nodes terminate`.

Terminate selected workers and decrement desired pool capacity:

```bash
mgmt-oke nodes terminate <node-name-or-ip> --tag none --dry-run
mgmt-oke nodes terminate <node-name-or-ip> --tag none --wait
```

Report an unhealthy host, then terminate and replace it while preserving
desired capacity:

```bash
mgmt-oke nodes terminate <node-name-or-ip> \
  --tag unhealthy --keep-size --wait
```

Terminate several selected workers:

```bash
mgmt-oke nodes terminate --nodes <node-a>,<node-b> --tag none --dry-run
```

Example replacement dry-run output:

```bash
mgmt-oke nodes terminate gpu-node-1 \
  --tag unhealthy --keep-size --dry-run --format json
```

```json
[
  {
    "current_size": 1,
    "decrement_size": false,
    "details": {
      "customer_reported_host_status": "unhealthy"
    },
    "operation": "node-remove",
    "owner": "oke",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": [
      "cordon Kubernetes node",
      "evict non-DaemonSet pods through the Eviction API",
      "tag OCI instance as customer-reported unhealthy",
      "verify OCI instance unhealthy tag",
      "delete the selected worker through OKE DeleteNode"
    ],
    "target": "gpu-node-1",
    "target_size": 1,
    "warnings": [
      "This direct OCI mutation does not update Terraform or OCI Resource Manager input values; reconcile the declared pool size before the next apply."
    ],
    "workload_pods": 0
  }
]
```

Termination drains by default. The safety options are:

| Option | Purpose |
| --- | --- |
| `--tag unhealthy\|none` | Apply and verify `ComputeInstanceHostActions.CustomerReportedHostStatus=unhealthy`, or explicitly skip host tagging. If omitted, prompt for each selected node. |
| `--keep-size` | Preserve desired capacity and allow replacement. |
| `--drain` / `--no-drain` | Enable or bypass Kubernetes drain. Drain is the default. |
| `--allow-workloads` | Permit `--no-drain` when workload pods are present. |
| `--delete-emptydir-data` | Acknowledge deletion of pod-local data. |
| `--force` | Acknowledge eviction of pods without a controller. |
| `--grace-period <seconds>` | Set pod termination grace. Default: `30`. |
| `--drain-timeout <seconds>` | Set the Kubernetes drain timeout. Default: `600`. |
| `--eviction-grace <duration>` | Set managed OKE deletion grace. Default: `PT10M`. |
| `--force-after-grace` | Allow managed OKE compute deletion after its grace period. |
| `--wait` | Monitor OCI work requests and wait for node absence and complete pool/resource convergence. |
| `--dry-run` | Validate selection, drain admission, ownership, and target capacity. |
| `--lock` / `--no-lock` | Enable or bypass the Kubernetes mutation Lease. |
| `--yes` | Skip interactive typed mutation confirmation; it does not answer an omitted `--tag` question. |

Managed OKE pools use OKE `DeleteNode`. Legacy Cluster Network and standalone
Instance Pool workers use instance-pool detach with automatic termination.
Requested unhealthy tags are merged with existing defined tags under the
instance ETag and read back before any selected worker is submitted for
termination. A tag update or verification failure stops the entire termination
submission. `--tag unhealthy` applies to every selected worker; omit `--tag`
for a per-node question or use `--tag none` in noninteractive workflows.

## Kubernetes Upgrades

Inspect current versions and optionally resolve a target:

```bash
mgmt-oke upgrades status
mgmt-oke upgrades status --to v1.36
mgmt-oke upgrades status --to v1.36 --format json
```

Generate the full ordered plan:

```bash
mgmt-oke upgrades plan --to v1.36
mgmt-oke upgrades plan --to v1.36 \
  --strategy auto \
  --pool-strategy oke-gpu=instance-replace \
  --pool-image oke-gpu=<image-ocid>
```

Execute one control-plane step, one externally prepared pool, or checkpointed
full-cluster orchestration:

```bash
mgmt-oke clusters upgrade --to v1.36.1 --dry-run
mgmt-oke pools upgrade oke-gpu \
  --to v1.36.1 \
  --strategy boot-volume-replace \
  --dry-run
mgmt-oke upgrades apply --to v1.36 --dry-run
```

Resume or resolve checkpoint state:

```bash
mgmt-oke upgrades resume --ack-workloads-drained
mgmt-oke upgrades abandon --yes
mgmt-oke upgrades cleanup --yes
```

Example live-derived plan output:

```text
operation              target      owner            strategy             target_size  workload_pods
---------------------  ----------  ---------------  -------------------  -----------  -------------
control-plane-upgrade  v1.36.1     oke              OKE UpdateCluster    -            0
worker-pool-upgrade    oke-cpu     node-pool        boot-volume-replace  1            9
worker-pool-upgrade    oke-system  node-pool        boot-volume-replace  2            12
worker-pool-upgrade    oke-gpu     node-pool        boot-volume-replace  1            0
worker-pool-upgrade    oke-rdma    cluster-network  instance-replace     2            1
```

This sanitized example was generated against a running CPU, A10, and A100 RDMA
cluster. `v1.36` resolved to the OKE-advertised production target `v1.36.1`.
The plan reported active pods, schedulability, and Kueue blockers but did not
mutate OCI or Kubernetes.

Target options:

| Option | Purpose |
| --- | --- |
| `--to <version>` | Required exact patch or major/minor target. A major/minor value resolves to the latest advertised production patch. |
| `--allow-preview` | Permit an explicitly advertised preview `.0` target. |

Strategies:

| Value | Behavior |
| --- | --- |
| `auto` | Managed OKE uses `boot-volume-replace`; self-managed backends use `instance-replace`. |
| `boot-volume-replace` | Preserve worker instance identity. Managed pools use OKE cycling; self-managed pools sequentially update source, metadata, and boot volume. |
| `instance-replace` | Managed pools use OKE surge cycling; self-managed pools add verified target capacity before removing old workers. |
| `blue-green` | Create and verify a parallel backend, retain the source, then return `action-required` for external migration. |

Pool strategy options:

| Option | Purpose |
| --- | --- |
| `--strategy <value>` | Select `auto`, `boot-volume-replace`, `instance-replace`, or `blue-green`. |
| `--image-id <ocid>` | Override the current compatible worker image. |
| `--maximum-unavailable <value>` | Managed cycling count or percentage. |
| `--maximum-surge <value>` | Managed cycling count or percentage. |
| `--blue-green-name <name>` | Name the parallel worker backend. |
| `--blue-green-compute-cluster-id <ocid>` | Select GMC target Compute Cluster placement. |
| `--blue-green-gpu-memory-fabric-id <ocid>` | Select GMC target fabric placement. |

Orchestration overrides repeat once per pool and use `POOL=VALUE`:

| Option | Purpose |
| --- | --- |
| `--pool-order <pool>` | Supply the complete custom order by repeating this option. |
| `--pool-strategy <pool=strategy>` | Override one pool strategy. |
| `--pool-image <pool=image-ocid>` | Override one pool image. |
| `--pool-maximum-unavailable <pool=value>` | Override managed cycling disruption. |
| `--pool-maximum-surge <pool=value>` | Override managed surge. |
| `--pool-blue-green-name <pool=name>` | Override the parallel backend name. |
| `--pool-blue-green-compute-cluster <pool=ocid>` | Select GMC blue-green Compute Cluster placement. |
| `--pool-blue-green-gpu-memory-fabric <pool=ocid>` | Select GMC blue-green fabric placement. |

Execution and acknowledgement options:

| Option | Purpose |
| --- | --- |
| `--ack-application-compatibility` | Attest that applications support the target version. |
| `--ack-iac-drift` | Acknowledge direct OCI changes that must be reconciled into IaC. |
| `--ack-workloads-drained` | Attest that targeted workers were externally prepared. |
| `--emergency-ack-unverified-drain` | Accept only unavailable API, RBAC, or exec verification; detected work remains blocking. |
| `--dry-run` | Perform available discovery and validation, print the plan, and stop before checkpoint or mutation. |
| `--yes` | Confirm OCI mutation only; never replaces safety acknowledgements. |
| `--timeout <seconds>` | Per-operation convergence timeout. Default: `7200`. |
| `--poll-interval <seconds>` | Convergence polling interval. Default: `30`. |

All upgrade execution paths wait. They do not expose `--no-wait`, scheduling,
eviction, or drain options. See
[Kubernetes Upgrades](./kubernetes-upgrades.md) for backend behavior,
checkpoint recovery, add-on validation, and workload gates.

## Accelerator And Scheduler Views

```bash
mgmt-oke topology list
mgmt-oke topology list --pool <pool-name>
mgmt-oke autoscaler status
mgmt-oke addons status
mgmt-oke addons validate --target all
mgmt-oke addons validate --target gpu
mgmt-oke addons validate --target rdma --pool <pool-name>
```

Example topology and autoscaler output:

```text
hpc_island  network_block  local_block  nodes  ready  shapes
----------  -------------  -----------  -----  -----  ---------
island-a    block-a        local-a      1      1      BM.GPU4.8
island-a    block-a        local-b      1      1      BM.GPU4.8

Cluster Autoscaler
(none)
```

## Health And Recommendations

```bash
mgmt-oke health run
mgmt-oke health run --type discovery
mgmt-oke health run --type node
mgmt-oke health run --type pool
mgmt-oke health run --type gpu --pool <pool-name>
mgmt-oke health run --type rdma --pool <pool-name>
mgmt-oke health run --type addons
mgmt-oke health run --type scheduler
mgmt-oke recommendations list
mgmt-oke recommendations list --type rdma --pool <pool-name>
```

Example health and recommendation output:

```text
check            scope        status  message                                       recommendation
---------------  -----------  ------  --------------------------------------------  --------------
gpu-allocatable  gpu-node-1   PASS    nvidia.com/gpu=1                              -
rdma-topology    rdma-node-1  PASS    Required OCI RDMA topology labels are valid.  -
rdma-topology    rdma-node-2  PASS    Required OCI RDMA topology labels are valid.  -

Recommendations
(none)
```

Health evaluation is read-only and deterministic. It compares desired, active,
and Ready capacity; discovery completeness; node state; allocatable
accelerators; RDMA topology and virtual functions; OKE add-on lifecycle;
autoscaler ownership; Kueue inventory; and Slinky protection metadata. A
partial discovery result is reported as `WARN` rather than as a healthy cluster.

## Exit Status

| Status | Meaning |
| --- | --- |
| `0` | Command succeeded; health checks found no failure or warning. |
| `1` | Requested resource was not found, or a health command found a warning. |
| `2` | Usage, validation, discovery, operation, timeout, or health failure. |
| `3` | Upgrade reached a safe external-action checkpoint, such as blue-green workload migration. |
| `130` | Interactive cancellation or keyboard interruption. |

Warnings and progress are written to standard error. Table, JSON, and CSV data
are written to standard output.
