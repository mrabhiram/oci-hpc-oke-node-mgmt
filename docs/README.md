# Documentation

Guides for installing and operating the OKE HPC Node Management Tool on an OCI
HPC OKE operator node. For the project overview and command summary, see the
[main README](../README.md).

## Getting started

- [Controller Node Installation](./controller-install.md): Install or upgrade
  `mgmt-oke` and `kubectl-oke` on a stack operator node.
- [Operator Quick Start](./operator-quickstart.md): Validate access and inspect a
  cluster without changing any resources.
- [Command Reference](./command-reference.md): Review every command, selector,
  output control, mutation option, and exit status.
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
- [Removing and Replacing Worker Nodes](./removing-and-replacing-worker-nodes.md):
  Drain and terminate selected workers or replace them while preserving pool size.
- [Managing RDMA Worker Pools](./managing-rdma-worker-pools.md): Operate both the
  OKE v26.7 Compute Cluster model and legacy self-managed Cluster Network model.

## Safety and troubleshooting

- [Cluster Autoscaler and Slinky Safety](./cluster-autoscaler-and-slinky-safety.md):
  Understand mutation ownership checks and protected Slurm workers.
- [Troubleshooting](./troubleshooting.md): Diagnose authentication, kubeconfig,
  inventory, readiness, timeout, and mutation refusal errors.
- [Current Scope](./scope.md): Review implemented features and planned work.
