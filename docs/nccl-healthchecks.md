# NCCL Healthchecks

NCCL healthchecks submit run-owned MPIJobs against explicitly selected NVIDIA
workers. The command terminology follows OCI HPC Slurm: `nodes healthchecks`
selects the diagnostic category, while `health` manages recorded runs.

This feature is a preview. The executable diagnostic component is NCCL. Full
host diagnostics, GPU stress/SDC, separate InfiniBand bandwidth/latency checks,
RCCL, Slurm reservations, specialized transports, and periodic services remain
outside the currently executable subset. A missing component is not a passing
check. Existing `health run` remains a read-only inventory evaluation.

## Prerequisites

- An installed, available MPI Operator exposing `kubeflow.org/v2beta1`.
- An explicitly selected, Ready pool or node set with idle, exclusive physical
  GPUs and a reviewed shape profile. MIG and shared GPU allocations are unsupported.
- Existing Kueue admission configuration when Kueue is installed. Supply its
  namespace-scoped LocalQueue with `--queue`; the tool does not create queues,
  change quotas, or bypass admission.
- A namespace admitting the rendered MPIJob and Pod security requirements.
  Host-network and VF RDMA worker profiles use privileged containers.
- For VF tests, an existing compatible SR-IOV network attachment and sufficient
  allocatable `nvidia.com/rdma-vf` resources on every participant.
- Space for the test image on workers and private evidence exports on the operator.

Review available profiles without contacting the cluster:

```bash
mgmt-oke health profiles
mgmt-oke nodes healthchecks --help
```

Profile availability does not establish live validation of every listed shape.
Image/driver labels are compatibility hints; the worker validates GPU UUIDs and
MIG state at runtime, and results require actual execution evidence.

### Kubernetes Permissions

Preflight verifies API access and performs server-side dry runs. The effective
identity needs node and Pod reads, logs, namespace/CRD/controller discovery,
Kueue topology/queue/flavor reads, namespaced Workload listing, event listing,
and network-attachment reads for VF profiles.
It also needs namespaced ConfigMap and MPIJob creation/updates, MPIJob deletion,
and access to the dedicated benchmark Lease in `kube-system`. All-namespace
ConfigMap listing is required for overlapping-run detection. Pod-create access
is checked through admission dry runs, not used to launch Pods directly.

Do not grant node mutation, eviction, OCI lifecycle, or operator installation
permissions solely for NCCL tests. Tests do not cordon, drain, evict, reboot,
tag, terminate, resize, or reconfigure workers.

## Select Participants

Specify exactly one of `--nodes`, `--fields`, or `--pool`.

```bash
# Non-stress API readiness checks; currently reports incomplete host coverage.
mgmt-oke nodes healthchecks --nodes 'gpu-[01-04]'

# One local NCCL test per selected pool member.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type active --checks nccl --queue <local-queue> --dry-run

# Slurm-compatible anchor/partner testing. Additional partners are displayed.
mgmt-oke nodes healthchecks --nodes gpu-01 \
  --type multi-node --checks nccl --queue <local-queue> --dry-run

# One collective across every selected pool member.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type multi-node --mode all-nodes --checks nccl \
  --queue <local-queue> --dry-run

# One separate collective per complete local-block domain.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type multi-node --mode per-local-block --checks nccl \
  --queue <local-queue> --dry-run

# An exact two-node collective rather than two independently selected anchors.
mgmt-oke nodes healthchecks --nodes gpu-01,gpu-02 \
  --type multi-node --mode all-nodes --checks nccl \
  --queue <local-queue> --dry-run
```

`--type all --checks nccl` includes API passive evidence, single-node NCCL, and
multi-node NCCL; it does not certify full Slurm diagnostic parity. `--type all`
without an explicit supported component fails instead of omitting diagnostics.

NodeSet expressions retain bracketed commas. Pool/field selections bound the
partner set. Explicit anchors may acquire compatible partners from the same
pool; every participant is displayed before confirmation. `--exclude-node`
excludes a partner, not an explicitly selected anchor. It is invalid for
`all-nodes` and `per-local-block` modes.

`--local-block <id>` restricts an unambiguous parent island/network/local-block
domain. Singleton blocks cannot pass an inter-node test. Unavailable members of
an exact set are not silently dropped.

## Execute And Reconnect

Replace `--dry-run` with `--wait` to submit and advance the full run:

```bash
mgmt-oke nodes healthchecks --nodes gpu-01,gpu-02 \
  --type multi-node --mode all-nodes --checks nccl \
  --queue <local-queue> --wait
```

Submission requires typed confirmation of the displayed run ID, or `--yes` for
noninteractive execution. Tests allocate all physical GPUs on each participant.
Use a maintenance interval with sufficient queue, network, and storage capacity.

Without `--wait`, the command submits only the initial bounded batch and returns
the run ID. A successful submission is not a benchmark pass. Pending cases need
an active caller:

```bash
mgmt-oke health list --pool <pool-name>
mgmt-oke health get <run-id> --format json
mgmt-oke health wait <run-id> --namespace <namespace>
mgmt-oke health results <run-id> --format json
mgmt-oke health logs <run-id> --case case-0001 --follow
```

`health wait` can submit pending cases from the recorded plan and clean up
completed resources. `health list`, `get`, `results`, and `logs` never submit
workloads or refresh checkpoints. `logs --follow` observes existing jobs only;
it does not replace `health wait`.

Runs are persisted as namespaced ConfigMaps. Resuming uses the existing run ID,
job ownership, and object UIDs; it does not create a replacement run. A short
Lease serializes submission/checkpoint transactions, not the entire test.

### Example Submission Output

Fresh CLI submission of a single A100 worker with eight GPUs produced the
following output. The run ID is replaced with a stable example identifier.
This is submission evidence, not a completed benchmark or performance result.

```json
[
  {
    "cases": 1,
    "created_at": "2026-10-12T23:54:03.267723+00:00",
    "failed": 0,
    "incomplete": 0,
    "namespace": "default",
    "outcome": "IN_PROGRESS",
    "passed": 0,
    "pending": 0,
    "run_id": "hc-0123456789abcdefabcd",
    "state": "RUNNING",
    "submitted": 1
  }
]
```

Run-level `RUNNING` means orchestration is active. Its MPIJob may still be queued;
inspect case state and Kueue admission before interpreting it as GPU execution.

That run subsequently reached its 180-second queue deadline without creating
benchmark Pods. Its case was recorded as `INCOMPLETE`, and orchestration reached
`CLEANED` after removing the owned MPIJob. The retained admission event identified
an untolerated GPU-node taint for the launcher; no NCCL performance result was
produced. The renderer now gives the CPU-only launcher the workers' exact known
GPU-taint tolerations, supporting queues that offer only GPU-node flavors without
changing queue policy or requesting an additional GPU.

### Example Failed Result

A subsequent fresh execution reached worker startup and verified eight physical
GPUs, but failed during Open MPI daemon launch with a worker-name resolution
error. The following result is from that execution, with node/run identifiers
replaced. This is a negative-path example, not a successful performance result.

```bash
mgmt-oke health results hc-0123456789abcdefabcd --format table
```

```text
case       type    state   nodes   ranks  network  job                                correctness  performance  transport  coverage
---------  ------  ------  ------  -----  -------  ---------------------------------  -----------  -----------  ---------  --------
case-0001  active  FAILED  gpu-01  8      local    hc-0123456789abcdefabcd-case-0001  UNVERIFIED   UNGRADED     LOCAL      FAIL
```

The command returned `2`. The run subsequently reported `CLEANED`, with logs and
scheduling diagnostics retained. No NCCL measurement was produced. `LOCAL`
identifies the requested local-only profile; it does not override failure or
certify a successful transport test.

## Settings

| Option | Meaning |
| --- | --- |
| `--network auto\|local\|host\|vf` | Local tests or supported RDMA transport profile. |
| `--scheduler auto\|direct\|kueue\|slurm` | Admission adapter; Slurm is reserved and currently rejected. |
| `--reservation` | Real Slurm reservation, not a Kueue queue; unavailable until the Slurm adapter exists. |
| `--image` | Container image override, subject to runtime validation. |
| `--env KEY=VALUE` | Repeatable validated NCCL/UCX/HCOLL overrides; evidence settings cannot be disabled. |
| `--min-bytes`, `--max-bytes`, `--factor` | Binary message sizes and multiplier. The sequence must include exactly 8 GiB. |
| `--iterations`, `--warmup-iterations` | Per-message measurement and warmup counts. |
| `--repetitions` | Separate recorded cases, not concatenated attempts. |
| `--min-bus-bandwidth` | Explicit minimum GB/s for both 8 GiB in-place and out-of-place bus bandwidth. |
| `--concurrency` | Maximum concurrently active cases on disjoint nodes. |
| `--queue-timeout`, `--startup-timeout`, `--timeout` | Separate per-case phase deadlines in seconds. |
| `--observation-timeout` | Maximum duration of this wait/log-follow invocation; expiration does not restart the run. |
| `--output-dir` | Private evidence directory; use the same directory when reconnecting. |
| `--keep-resources` | Retain MPIJobs/Pods after collection. Overlapping later cases remain blocked; resume `health wait` without this flag to release completed resources and continue. |

Queue deadlines and case advancement currently require an active caller. No
background service is installed. Kubernetes workload deadlines bound already
started Pods, but do not replace client-side queue supervision.

## Results And Evidence

Results separate execution state, correctness, performance, transport, and
coverage. Success requires exact selected node UIDs, worker Pod UIDs, all MPI
ranks, successful completion, correctness checks, complete logs, and the exact
8 GiB reporting row. RDMA tests additionally require inter-node data-channel
evidence covering every participant. Socket fallback is not verified RDMA.

Without `--min-bus-bandwidth`, measured performance is `UNGRADED`. A failed
collective does not identify every participant as faulty hardware.

The default evidence directory is `~/.local/state/mgmt-oke/healthchecks` on the
operator. Immutable checksum-addressed exports contain the selected case,
request/options, runtime identities, logs, Kueue Workload status, and events tied
to verified MPIJob, Pod, and Workload UIDs. Each run directory is private.
Observed requeues and Pod/container changes have distinct snapshots; logs from
different attempts are never combined into one passing assessment.
Changed scheduling decisions also produce snapshots, including when no Pod has
started. Repeated event counts alone do not create another snapshot. Events may
expire before observation; an empty event list is not proof that no event occurred.
Log reads use bounded raw responses and explicit UTF-8 decoding. Invalid text or
an interrupted stream cannot establish success. Worker SSH readiness retries
transient failures within the startup deadline, retaining the initially collected
per-run host keys and strict host-key checking throughout.

```bash
mgmt-oke health logs <run-id> --role all --format json
mgmt-oke health cancel <run-id>
mgmt-oke health cleanup <run-id>
```

Cancellation exports readable logs before suspension and waits for Pod removal.
Cleanup requires all cases to be terminal, verifies recorded exports, and removes
only owned workloads with UID/resource-version preconditions. It retains the run
checkpoint. Repeating a pending cancel/cleanup command verifies convergence;
`CLEANED` means resource cleanup, not that the benchmark passed.

Do not remove or relocate evidence files before cleanup. If an export cannot be
verified, logs cannot be read, or the workload attempt changes, the tool retains
resources and reports the verification failure.

## Exit Status

Submission without `--wait` returns `0` for an accepted initial batch. For
`health results` and waits: `0` means all requested checks passed, `1` incomplete
or degraded evidence, `2` failure, `3` still in progress/action required, and
`130` cancellation. `health get` and `list` report retrieval success separately
from recorded outcome. Pending cancellation or cleanup returns `3`; successful
cleanup itself does not turn a failed test into a passed test.
