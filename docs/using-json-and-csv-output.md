# Using JSON and CSV Output

This guide shows how to consume `mgmt-oke` inventory from shell scripts,
`jq`, spreadsheets, and monitoring workflows.

## Overview

Every command supports table, JSON, and CSV output:

```bash
--format table
--format json
--format csv
```

`--output` is an alias for `--format`. Table output is intended for interactive
use. JSON and CSV are intended for automation.

Command-specific warnings are written to standard error so structured standard
output remains parseable. Full `reconcile` JSON includes warnings in its
`warnings` field.

## Prerequisites

- `mgmt-oke` installed and configured
- `jq` for the JSON examples

## Pool Inventory as JSON

```bash
mgmt-oke --auth instance_principal --format json pools list
```

List pool names, ownership, placement, and counts:

```bash
mgmt-oke --auth instance_principal --format json pools list | \
  jq -r '.[] | [.name, .kind, .placement, .desired, .k8s_ready] | @tsv'
```

Select managed Compute Cluster pools:

```bash
mgmt-oke --auth instance_principal --format json pools list | \
  jq '.[] | select(.placement == "compute-cluster")'
```

Find pools that have not converged:

```bash
mgmt-oke --auth instance_principal --format json pools list | \
  jq '[.[] | select(.desired != .oci_active or .desired != .k8s_ready)]'
```

## Node Inventory as JSON

```bash
mgmt-oke --auth instance_principal --format json nodes list
```

List nodes with ordinary workload pods:

```bash
mgmt-oke --auth instance_principal --format json nodes list | \
  jq '.[] | select(.workload_pods > 0) | {name, pool, workload_pods}'
```

List RDMA nodes and virtual-function allocation:

```bash
mgmt-oke --auth instance_principal --format json nodes list --rdma-only | \
  jq '.[] | {name, pool, rdma, rdma_vf}'
```

## Full Snapshot as JSON

```bash
mgmt-oke --auth instance_principal --format json reconcile > snapshot.json
```

The full snapshot contains raw `pools`, `nodes`, `addons`,
`autoscaler_entries`, `kueue`, and `warnings` sections. Use the command-specific
JSON views when a flattened row format is preferable.

## CSV Export

Export pools or nodes:

```bash
mgmt-oke --auth instance_principal --format csv pools list > pools.csv
mgmt-oke --auth instance_principal --format csv nodes list > nodes.csv
```

Export the full snapshot:

```bash
mgmt-oke --auth instance_principal --format csv reconcile > cluster-snapshot.csv
```

Full-snapshot CSV includes a `record_type` column so pool, node, add-on,
autoscaler, and Kueue records can be separated after import.

## Exit Status

Use process status in automation rather than parsing human-readable messages:

| Status | Meaning |
| --- | --- |
| `0` | Command completed successfully. |
| `1` | A requested pool or node was not found. |
| `2` | Validation, target discovery, OCI operation, or timeout failure. |
| `130` | Interactive cancellation or keyboard interruption. |

Example:

```bash
if ! mgmt-oke --auth instance_principal --format json pools list > pools.json; then
  echo "Worker-pool inventory failed" >&2
  exit 1
fi
```

## Safe Mutation Automation

Inspect and validate a target before using `--yes`. For example:

```bash
POOL=oke-cpu

mgmt-oke --auth instance_principal --format json autoscaler status \
  > autoscaler.json

jq -e --arg pool "$POOL" 'any(.[]; .pool == $pool) | not' \
  autoscaler.json

mgmt-oke --auth instance_principal --format json pools get "$POOL" | \
  jq -e '.[0].slinky == false'

mgmt-oke --auth instance_principal pools resize "$POOL" \
  --delta 1 --wait --yes
```

The CLI repeats authoritative autoscaler and ownership checks during mutation
preflight. Script-side validation is an additional guard, not a replacement.

## Capturing Warnings

Keep structured output and warnings in separate files:

```bash
mgmt-oke --auth instance_principal --format json pools list \
  > pools.json 2> pool-warnings.log
```

Treat a successful exit with warnings as partial discovery. Review the warning
before using that output to make an operational decision. Full `reconcile` JSON
also preserves warnings in the document itself.
