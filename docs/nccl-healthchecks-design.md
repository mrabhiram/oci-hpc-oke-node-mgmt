# Slurm-Compatible NCCL Healthchecks

Status: implementation in progress. The planning, manifest, preflight,
checkpoint, workload-evidence, result-validation, and execution-workflow components
have initial implementations and unit tests. The proposed public commands below
are not yet available; CLI integration, additional adapters, and live benchmark
validation remain incomplete. Existing `mgmt-oke health run`
behavior is unchanged.

## Objective

Add GPU collective testing to `mgmt-oke` using the healthcheck terminology of the
OCI HPC Slurm management tool. Preserve familiar commands and test granularity
while using OKE-native workload execution, GPU/RDMA configuration, and scheduler
integration.

Track command compatibility, diagnostic coverage, and live validation separately.
Matching command names does not establish complete diagnostic parity.

## Command Interface

The shared interface uses `nodes healthchecks` and `services`. There is no
separate top-level `tests nccl` command family.

| Proposed command | Behavior |
| --- | --- |
| `mgmt-oke nodes healthchecks --nodes <nodes> --type passive` | Non-stress setup/readiness checks; no NCCL workload. |
| `mgmt-oke nodes healthchecks --nodes <nodes> --type active` | One local active suite per selected node, including NCCL on supported NVIDIA workers. |
| `mgmt-oke nodes healthchecks --nodes <nodes> --type multi-node` | Two-node checks with recorded anchor and partner nodes. |
| `mgmt-oke nodes healthchecks --nodes <nodes> --type all` | Passive, active, and multi-node stages. |
| `mgmt-oke services active-hc` | One eligibility/frequency-based pass for single-node active checks. |
| `mgmt-oke services multi-node-hc` | One pass for eligible pairs, initial validation, and alternate-partner diagnosis. |
| `mgmt-oke services update-metadata` | Collect owned workload observations and refresh recorded health results. |
| `mgmt-oke services all` | Run the explicitly enabled OKE service handlers once. |

The default type remains `passive`. `--type all` selects all healthcheck
categories, not all cluster nodes. A service invocation does not install or
start a recurring schedule.

### Selection and Compatibility

- Preserve the option names `--nodes`, `--fields`, `--type`, `--exclude-node`,
  and `--reservation`.
- Require one primary selector: `--nodes`, `--fields`, or the OKE extension
  `--pool`. No selector must not trigger a cluster-wide load test.
- Support comma-separated node identifiers and validated Slurm-style NodeSet
  expressions, such as `gpu-node-[01-04]`, through the established ClusterShell
  parser. Validate its supported Python versions before adding the dependency.
- Reuse existing name, Slurm alias, IP, provider ID, and instance OCID resolution.
  Serial and OCI display-name selection require authoritative discovery data
  and ambiguity checks before being advertised as supported.
- Preserve `--fields key=value,key=value` syntax using documented OKE fields.
  Reject unsupported Slurm database fields rather than silently ignoring them.
- Preserve the default multi-node anchor workflow: a selected node is tested
  with an eligible partner. Do not reinterpret two supplied anchors as a single
  collective over exactly those two nodes.
- For explicit anchors, select compatible partners from the same pool and show
  every additional participant before submission. Pool/field selections bound
  the candidate set. Do not cross pools implicitly.
- `--exclude-node` excludes a partner in pairwise mode. Conflicting anchor and
  exclusion selections fail validation. This option is invalid for exact-set
  and per-local-block collectives.
- `--reservation` means a real Slurm reservation through a supported Slinky
  adapter. Native Kubernetes execution rejects it and directs users to
  `--queue`; a Kueue queue is not a Slurm reservation.

Without `--wait`, active commands return run and job identifiers after submission.
Submission success is not a passed benchmark. `--wait` reports terminal results.
`--dry-run` performs non-mutating planning and validation. Workload submissions
use the existing interactive confirmation or `--yes` convention.

## OKE Extensions

The following extensions remain on `nodes healthchecks` and are not presented as
options available in upstream Slurm mgmt.

```text
# Preview one NCCL collective over the complete selected pool.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type multi-node --mode all-nodes --checks nccl --dry-run

# Run one collective across every member of the selected pool.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type multi-node --mode all-nodes --checks nccl --wait

# Run a separate collective within each local block.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type multi-node --mode per-local-block --checks nccl --wait

# Run one collective within a particular local block.
mgmt-oke nodes healthchecks --pool <pool-name> --local-block <local-block-id> \
  --type multi-node --mode all-nodes --checks nccl --wait

# Run a collective on exactly these two nodes, without choosing other partners.
mgmt-oke nodes healthchecks --nodes <node-a>,<node-b> \
  --type multi-node --mode all-nodes --checks nccl --wait

# Run single-node NCCL on each selected pool member.
mgmt-oke nodes healthchecks --pool <pool-name> \
  --type active --checks nccl --wait
```

| Option | Contract |
| --- | --- |
| `--pool` | Select the named pool's current membership explicitly. |
| `--mode pairs` | Default two-node testing with recorded anchor/partner coverage. |
| `--mode all-nodes` | One collective across the entire selected set, with at least two nodes. |
| `--mode per-local-block` | One collective per unambiguous local block, with no scheduling spillover. |
| `--checks nccl` | Request only the NVIDIA NCCL component, not the entire active diagnostic suite. |
| `--local-block` | Restrict selection to a local block; reject ambiguous IDs and conflicting explicit nodes. |
| `--pairing`, `--pair-scope` | Explicit pairing policy and same-block/cross-block constraints. Exhaustive pairing requires an explicit request and displayed job count. |
| `--network` | Select a supported host-network, VF, or explicitly enabled preview transport profile. |
| `--scheduler`, `--namespace`, `--queue` | Use supported execution/admission integrations and existing customer resources. |
| `--image`, `--env` | Validated image and environment overrides with recorded provenance. |
| `--min-bytes`, `--max-bytes`, `--iterations`, `--warmup-iterations`, `--repetitions` | Typed benchmark settings with correctness checks and deliberate coverage of the reporting message size. |
| `--min-bus-bandwidth` | Optional, explicitly defined performance expectation for the selected profile and metric. |
| `--concurrency`, `--queue-timeout`, `--startup-timeout`, `--timeout` | Bounded concurrency and separate admission, startup, and execution deadlines. |
| `--output-dir`, `--keep-resources` | Export evidence and optionally retain owned resources for investigation. |

`--mode` affects only the multi-node stage. Invalid category, component, and
mode combinations fail before submission. Required diagnostic components must
not disappear from a suite because a binary or capability is unavailable.
Unsupported full-suite requests fail preflight; explicitly requested supported
subsets can run without claiming full-suite success.

Local-block grouping uses the complete parent island/network/local-block
identity internally. Output emphasizes local block ID, node count, and node
names. A singleton group cannot pass an inter-node RDMA test.

## Run Management and Results

Add these helpers under the existing `health` group:

```text
mgmt-oke health profiles
mgmt-oke health list --pool <pool-name>
mgmt-oke health get <run-id>
mgmt-oke health logs <run-id> --follow
mgmt-oke health results <run-id> --format json
mgmt-oke health wait <run-id>
mgmt-oke health cancel <run-id>
mgmt-oke health cleanup <run-id>
```

The existing `health run` remains read-only and does not submit benchmark
workloads. Cancellation and cleanup require confirmation and affect only
resources owned by the selected run.

Each run records:

- Requested and observed node names/UIDs, pool membership, and topology.
- Actual worker Pod UIDs, GPU/rank counts, and MPI rank placement.
- Image digests, runtime/library versions, effective settings, and profile source.
- Completion conditions, process exit status, correctness errors, and transport
  evidence.
- In-place and out-of-place bandwidth/latency, including the exact 8 GiB row
  where required by the reporting profile.
- Attempts, retries, events, logs, timestamps, and coverage of every requested
  participant.

Report execution, correctness, performance, transport, and coverage separately.
Without a configured performance baseline, bandwidth is measured but performance
is ungraded. Do not report socket fallback as verified RDMA. A failed collective
does not establish that every participating node has defective hardware.

Persist bounded, non-secret run metadata and summaries. Export raw logs before
deleting their only copy. Reconnect using recorded object identities, preserve
attempt boundaries after requeue/retry, and protect shared resources during
idempotent cleanup.

Workload evidence collection verifies the MPIJob UID and the controller ownership
chain for each worker and launcher Pod. It checks Pod/container identity before
and after bounded log reads and records resolved image digests, node UIDs, and
exit codes. Missing logs, changed identities, incomplete coverage, and conflicting
completion conditions cannot establish a successful benchmark. Private evidence
snapshots now include the immutable request, execution options, selected case,
Pod identities, and raw logs. Export uses content-addressed files, atomic
publication, and checksum read-back; verification rejects missing, modified,
publicly readable, or identity-mismatched files. The execution workflow exports
these snapshots before cancellation and cleanup, and verifies every recorded
snapshot before deleting the workload.

The workflow now connects preparation, checkpoint creation, bounded submission,
observation, waiting, cancellation, and cleanup. It records intent before a job
POST and recovers an accepted job by its recorded name and verified ownership
when the response was lost. Submission is revalidated under the dedicated Lease;
waiting releases the Lease between bounded passes. A checkpoint conflict or
unverified API response stops that pass without claiming success.

Observed requeues and changes to Pod/container identities receive separate,
append-only evidence snapshots. Assessments use one current snapshot, never
concatenated logs from different observations. This is observed history, not a
claim to capture transitions that occurred entirely between polls. Requeue does
not reset an already recorded startup or execution deadline. A total deadline
also bounds the combined queue/startup/execution interval.

Cancellation records intent before suspension and requires readable logs for
started containers. It waits for the case's Pods to disappear before recording
cancellation. Cleanup uses UID/resource-version preconditions, refuses changed
execution attempts, and waits for observed MPIJob and Pod removal. A completed
case's assessment remains tied to its original evidence digest even when later
cleanup snapshots are exported.

These workflow methods do not install a background controller. Advancement and
client-enforced queue deadlines currently require an active caller; stopping a
wait does not restart or automatically cancel the run. Retaining resources can
prevent later overlapping cases from starting until those resources are released.
Public command integration and service-driven advancement remain delivery items.

## Execution Architecture

- Reuse the existing discovery, node selection, Kubernetes client, output
  formats, and CLI conventions.
- Use versioned, reviewed shape-specific profiles derived from the OCI HPC OKE
  manifests. Record source revision and licensing information. Do not fetch and
  apply moving upstream manifests at runtime.
- Render structured MPIJobs with exact selected-node affinity and one worker
  per selected node. Request GPU devices through the scheduler; do not bypass
  placement by assigning `nodeName`.
- Validate readiness, GPU availability, supported device-sharing mode, image
  compatibility, taints, resources, RBAC, CRDs, and network prerequisites before
  submission. Read-only validation must disclose evidence it cannot obtain.
- Use existing MPI Operator and Kueue configuration. Direct MPIJob execution
  does not imply gang scheduling. Do not bypass a cluster's admission policy.
- Keep host-network and SR-IOV VF profiles distinct. Specialized DRA/MNNVL,
  Dranet, RCCL, and Slinky execution require their own tested profiles/adapters.
- Use unique run-owned names, bounded disjoint-node concurrency, overlap checks,
  and timeouts. Do not hold the cluster lifecycle mutation lease for the
  duration of a benchmark.
- Never silently substitute a smaller ready subset for a requested full-pool
  test. Node replacement or incomplete participation invalidates coverage.

Capability preflight checks the selected namespace, required permissions, served
MPI/Kueue APIs, MPI controller readiness, queue holds and namespace selection,
GPU/RDMA ResourceFlavors, and the existing SR-IOV network attachment when used.
Server-side dry runs validate the MPIJob and both Pod templates without creating
workloads. Kueue admission must suspend the queued MPIJob probe; the tool does
not fall back to direct execution when queue verification fails.

For topology-aware queues, preflight requires a hostname leaf and all topology
labels on every selected worker. Generated affinity uses one OR term per node
name, with a matching hostname constraint. Local-block cases require their
complete parent domain; other collective cases prefer local-block placement
without shrinking the requested node set. Queue compatibility does not reserve
capacity or prove that a workload has been admitted.

GPU Operator MIG advertisement policy is checked separately from active GPU
partitioning. Full-GPU discovery can coexist with `mig.strategy=single` or
`mixed`; shared/MIG products, incomplete discovery, and transitional MIG
configuration are rejected. Before starting worker SSH, runtime validation
requires the expected unique physical GPU UUIDs and disabled or unsupported MIG
mode. Image tags and driver labels remain compatibility hints, not runtime proof.

## Healthcheck Parity and Safety

Slurm's active healthchecks include more than NCCL: GPU stress and SDC checks,
vendor-specific diagnostics, and RDMA bandwidth/latency in multi-node suites.
These remain explicit delivery items. API-only readiness evidence is not a
substitute for complete host setup validation.

Service handlers select overdue idle nodes, eligible new nodes, and suspect
pairs requiring alternate partners. Eligibility uses scheduler/resource evidence,
not GPU utilization alone. Freshness is tied to node identity and profile, so
reused node names cannot inherit an earlier machine's results. Periodic
execution requires explicit configuration and installation.

`services all` runs only the enabled OKE handlers documented by the tool. It does
not imply implementation of Slurm's Ansible, database, autoscaling, or reservation
release workflows. Results may inform node summaries and recommendations, but
reading those views must not trigger tests or remediation.

The benchmark subsystem will not:

- Cordon, drain, evict, reboot, tag, or terminate nodes automatically.
- Modify OCI pool size or other cloud lifecycle settings.
- Install operators or change customer queues, quotas, or host networking modes.
- Evict Slinky workers to obtain their GPUs.
- Treat missing, unsupported, untested, or stale evidence as a successful test.

## Delivery and Acceptance

Items remain unchecked until their complete acceptance criterion is implemented
and verified. Passing component tests alone do not establish end-to-end support.

### Command Compatibility and NCCL Execution

- [ ] Implement the shared command names, type defaults, identifier selection,
      field validation, exclusion semantics, and explicit adapter restrictions.
- [ ] Implement NVIDIA single-node and default two-node NCCL testing.
- [ ] Implement whole-pool, exact-subset, and per-local-block execution.
- [ ] Implement supported host-network/VF profiles and MPI Operator/Kueue paths.
- [ ] Implement dry-run, submission, wait, results, history, cancellation, and
      ownership-protected cleanup.

### Service and Diagnostic Coverage

- [ ] Implement bounded service passes, freshness, initial validation,
      alternate-partner retries, and metadata/recommendation integration.
- [ ] Add supported passive host evidence, GPU stress/SDC, and RDMA
      bandwidth/latency components with individual coverage reporting.
- [ ] Add supported RCCL, Slinky/reservation, DRA/MNNVL, and preview transport
      adapters, each with explicit compatibility requirements.
- [ ] Add opt-in periodic execution using the same service handlers.

### Validation and Documentation

- [ ] Add unit and CLI compatibility tests for parsers, selection, profiles,
      manifests, result interpretation, and service eligibility.
- [ ] Add workflow tests for admission/requeue, RBAC failures, unavailable
      resources, partial creation, retries, reconnect, cancellation, and cleanup.
- [ ] Assert no benchmark execution path invokes node scheduling or OCI
      lifecycle mutation methods; retain existing `health run` regression tests.
- [ ] Run the repository's full test, lint, type-check, and packaging checks.
- [ ] Run approved live single-node, two-node, whole-pool, local-block, and
      cross-block tests. Verify actual ranks, correctness, transport, and logs.
- [ ] Validate busy-node handling, cancellation, and cleanup on live resources.
- [ ] Update README, installation/RBAC, command reference, architecture, scope,
      troubleshooting, feature checklist, and sanitized examples from fresh runs.

Unsupported hardware or unavailable environments remain explicitly unvalidated.
Successful submission, synthetic fixtures, and historical results do not satisfy
the live acceptance criteria.

## References

The design was reviewed against these source revisions:

- OCI HPC Slurm: `419ef59599f44862302cfa2de5752a57e68dbfdb`.
- OCI HPC OKE: `6c94a4201d9ec6887f37eceeca325ec18b03b703`.

- [Slurm healthcheck CLI](https://github.com/oracle-quickstart/oci-hpc/blob/419ef59599f44862302cfa2de5752a57e68dbfdb/mgmt/lib/cli/nodes/commands/healthchecks.py)
- [Slurm healthcheck services](https://github.com/oracle-quickstart/oci-hpc/blob/419ef59599f44862302cfa2de5752a57e68dbfdb/mgmt/lib/cli/services/commands/run.py)
- [Slurm NCCL healthcheck runbook](https://github.com/oracle-quickstart/oci-hpc/blob/419ef59599f44862302cfa2de5752a57e68dbfdb/documentation/Healthcheck-NCCL-Runbook.md)
- [OKE NCCL/RCCL testing](https://github.com/oracle-quickstart/oci-hpc-oke/blob/6c94a4201d9ec6887f37eceeca325ec18b03b703/docs/running-nccl-rccl-tests.md)
- [OKE NCCL manifests](https://github.com/oracle-quickstart/oci-hpc-oke/tree/6c94a4201d9ec6887f37eceeca325ec18b03b703/manifests/nccl-tests)
- [OKE RCCL manifests](https://github.com/oracle-quickstart/oci-hpc-oke/tree/6c94a4201d9ec6887f37eceeca325ec18b03b703/manifests/rccl-tests)
- [MPI Operator v0.8.2 resource ownership](https://github.com/kubeflow/mpi-operator/blob/v0.8.2/pkg/controller/mpi_job_controller.go)
