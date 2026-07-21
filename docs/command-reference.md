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

`pools list` is the faster inventory path. `reconcile`, `status`, health, and
recommendation commands include the additional pod, autoscaler, Kueue, and
add-on correlations required by their checks.

## Worker Pools

```bash
mgmt-oke pools list
mgmt-oke pools get <pool-name-or-ocid>
```

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
