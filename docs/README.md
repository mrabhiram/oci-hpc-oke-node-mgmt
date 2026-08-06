# Documentation

Guides for installing and operating the OKE HPC Node Management Tool on an OCI
HPC OKE operator node. For the project overview and command summary, see the
[main README](../README.md).

## Output examples

Operational guides include representative `mgmt-oke` output captured from live
commands against a running OCI HPC OKE cluster. Node names, IP addresses,
topology identifiers, and OCI resource identifiers are replaced with stable
example values. Pool counts, shapes, ownership classifications, resource
counts, status values, and command behavior are retained. Add-on versions and
capacity naturally vary by deployment.

## Getting started

- [Controller Node Installation](./controller-install.md): Install or upgrade
  `mgmt-oke` and `kubectl-oke` on a stack operator node.
- [Operator Quick Start](./operator-quickstart.md): Validate access and inspect a
  cluster without changing any resources.
- [Command Reference](./command-reference.md): Review every command, selector,
  output control, mutation option, and exit status.
- [Feature Checklist](./feature-checklist.md): Review implemented capabilities,
  validation depth, outstanding live acceptance, and proposed work.
- [Kubernetes Upgrades](./kubernetes-upgrades.md): Inspect version readiness,
  plan and execute control-plane and worker upgrades, and recover checkpointed
  operations without mutating Kubernetes scheduling state.
- [Architecture](./architecture.md): Understand automatic OCI target discovery,
  worker-pool ownership, API routing, readiness, and safety boundaries.

## Inventory and readiness

- [Discovering Worker Pools and Nodes](./discovering-worker-pools-and-nodes.md):
  Inspect OKE node pools, legacy Cluster Networks, Instance Pools, Kubernetes
  nodes, add-ons, topology, Cluster Autoscaler, and Kueue.
- [Verifying GPU and RDMA Readiness](./verifying-gpu-and-rdma-readiness.md):
  Check GPU allocation, RDMA topology, Network Operator virtual functions, and
  deterministic health and add-on validation.
- [Using JSON and CSV Output](./using-json-and-csv-output.md): Consume inventory
  safely from scripts, `jq`, spreadsheets, and monitoring workflows.

## Capacity and node lifecycle

- [Resizing Worker Pools](./resizing-worker-pools.md): Add or remove capacity
  from managed OKE, Compute Cluster-backed, Cluster Network, and standalone
  Instance Pools.
- [Creating Worker Pools](./creating-worker-pools.md): Create managed CPU/GPU
  pools, managed Compute Cluster RDMA pools, or legacy self-managed RDMA
  Cluster Network pools with inherited defaults and explicit overrides.
- [Worker Bootstrap and Storage](./worker-bootstrap-and-storage.md): Compose the
  official OCI HPC OKE cloud-init, NVMe RAID, FSS mount, and Lustre mount
  workflows.
- [Removing and Replacing Worker Nodes](./removing-and-replacing-worker-nodes.md):
  Drain and terminate selected workers or replace them while preserving pool size.
- [Replacing Worker Boot Volumes](./replacing-worker-boot-volumes.md): Replace
  one managed or self-managed worker boot volume, or roll a managed pool to a
  supported new image or node property.
- [Managing RDMA Worker Pools](./managing-rdma-worker-pools.md): Operate both the
  OKE v26.7 Compute Cluster model and legacy self-managed Cluster Network model.

## Live lifecycle validation

- [Live Kubernetes Upgrade Validation](./live-kubernetes-upgrade-validation.md):
  Review fresh status, plan, dry-run, ordering-guard, and checkpoint output,
  together with the explicit boundary that no live version change was made.
- [Live Worker Pool Creation Validation](./live-pool-creation-validation.md):
  Review sanitized output from managed GPU creation, managed Compute Cluster
  RDMA validation, self-managed RDMA planning, and capacity-limited submissions.
- [Live Worker Pool Deletion Validation](./live-pool-deletion-validation.md):
  Review sanitized output from managed GPU deletion, RDMA deletion planning,
  ownership-aware cleanup, and system-pool protection.
- [Live Unhealthy Host Termination Validation](./live-unhealthy-host-termination-validation.md):
  Review sanitized managed A10 and A100 RDMA tag-and-terminate dry-run output,
  including the explicit boundary that no host tag or termination was applied.

## Safety and troubleshooting

- [Cluster Autoscaler and Slinky Safety](./cluster-autoscaler-and-slinky-safety.md):
  Understand mutation ownership checks and protected Slurm workers.
- [Troubleshooting](./troubleshooting.md): Diagnose authentication, kubeconfig,
  inventory, readiness, timeout, and mutation refusal errors.
- [Current Scope](./scope.md): Review implemented features and planned work.
