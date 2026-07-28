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

`cpu` and `gpu` create managed OKE node pools. `rdma` creates a self-managed
Cluster Network and derived Instance Configuration. Custom images and
backend-appropriate placement, shape, networking, boot, Kubernetes, lifecycle,
metadata, cloud-init, FSS, Lustre, and NVMe settings are available. Unspecified
values are inherited from the matching source.

See [Creating Worker Pools](./creating-worker-pools.md) and
[Worker Bootstrap and Storage](./worker-bootstrap-and-storage.md) for the full
option matrix and examples. [Live Worker Pool Creation
Validation](./live-pool-creation-validation.md) contains sanitized output from
live managed GPU and self-managed RDMA creation operations.

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

## Node Termination And Replacement

`nodes remove` is an alias for `nodes terminate`.

Terminate selected workers and decrement desired pool capacity:

```bash
mgmt-oke nodes terminate <node-name-or-ip> --dry-run
mgmt-oke nodes terminate <node-name-or-ip> --wait
```

Terminate and replace workers while preserving desired capacity:

```bash
mgmt-oke nodes terminate <node-name-or-ip> --keep-size --wait
```

Terminate several selected workers:

```bash
mgmt-oke nodes terminate --nodes <node-a>,<node-b> --dry-run
```

Example replacement dry-run output:

```bash
mgmt-oke nodes terminate gpu-node-1 --keep-size --dry-run --format json
```

```json
[
  {
    "current_size": 1,
    "decrement_size": false,
    "operation": "node-remove",
    "owner": "oke",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": [
      "cordon Kubernetes node",
      "evict non-DaemonSet pods through the Eviction API",
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
| `--yes` | Skip interactive typed confirmation. |

Managed OKE pools use OKE `DeleteNode`. Legacy Cluster Network and standalone
Instance Pool workers use instance-pool detach with automatic termination.

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
| `130` | Interactive cancellation or keyboard interruption. |

Warnings and progress are written to standard error. Table, JSON, and CSV data
are written to standard output.
