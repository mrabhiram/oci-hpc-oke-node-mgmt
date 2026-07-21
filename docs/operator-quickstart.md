# Operator Quick Start

This guide validates a new `mgmt-oke` installation and builds a read-only view
of an OCI HPC OKE cluster. Every command in this guide is read-only.

## Prerequisites

- `mgmt-oke` installed by following the
  [Controller Node Installation](./controller-install.md) guide
- `kubectl` configured for the stack's OKE cluster
- OCI CLI available on `PATH`
- instance-principal access to read the OKE cluster and related compute
  resources

## Procedure

### Step 1: Validate Kubernetes Access

Confirm that kubeconfig points to the operator node's cluster and that the
Kubernetes API is reachable:

```bash
kubectl config current-context
kubectl get nodes -o wide
```

Stack operator nodes normally have one cluster in kubeconfig. No context
environment variable is required.

For a non-interactive shell, make sure the OCI CLI is on the effective path and
select the authentication method used by direct `kubectl`:

```bash
export PATH=/home/ubuntu/bin:/usr/local/bin:/usr/bin:/bin
export OCI_CLI_AUTH=instance_principal
kubectl get nodes -o wide
```

### Step 2: Validate OCI Authentication

Confirm that the operator instance can call OCI APIs with its instance
principal:

```bash
command -v oci
oci iam region list --auth instance_principal
```

### Step 3: Validate the CLI

```bash
mgmt-oke --version
mgmt-oke --help
```

The examples use the direct entrypoint. `kubectl oke` exposes the same commands
when `kubectl-oke` is installed on `PATH`.

The operator command needs only the authentication method. The cluster OCID and
region are read from kubeconfig, and the compartment OCID is read from OKE
`GetCluster`:

```bash
mgmt-oke --auth instance_principal pools list
```

### Step 4: Inspect Worker Pools

```bash
mgmt-oke --auth instance_principal pools list
```

Review these fields:

| Field | Meaning |
| --- | --- |
| `kind` | Owning resource, such as `node-pool` or `cluster-network`. |
| `placement` | Placement model, such as `standard`, `compute-cluster`, or `cluster-network`. |
| `desired` | Configured OCI pool size. |
| `oci_active` | Active OCI instances discovered for the pool. |
| `k8s_ready` | Kubernetes nodes assigned to the pool and reporting Ready. |
| `gpu` | GPU extended resource expected from the pool. |
| `rdma` | Whether the pool is RDMA-capable. |

### Step 5: Inspect Kubernetes Nodes

```bash
mgmt-oke --auth instance_principal nodes list
```

To inspect one pool or one node:

```bash
mgmt-oke --auth instance_principal nodes list --pool oke-rdma
mgmt-oke --auth instance_principal nodes get <node-name-or-ip>
```

`nodes get` accepts a Kubernetes node name, Slinky node name, internal IP,
provider ID, or OCI instance OCID.

### Step 6: Inspect Add-ons and RDMA Topology

```bash
mgmt-oke --auth instance_principal addons status
mgmt-oke --auth instance_principal topology list
mgmt-oke --auth instance_principal nodes list --rdma-only
```

An empty topology view is expected when the cluster has no RDMA workers. For an
RDMA pool, topology output should contain valid HPC Island, Network Block, and
Local Block values.

### Step 7: Run Health And Add-on Validation

```bash
mgmt-oke --auth instance_principal status
mgmt-oke --auth instance_principal health run
mgmt-oke --auth instance_principal addons validate --target all
mgmt-oke --auth instance_principal recommendations list
```

`status` and `health run` return `1` for warnings and `2` for failures, making
them suitable for monitoring and installation gates. Optional components, such
as Network Operator on a host-network RDMA deployment, are informational.

### Step 8: Build a Full Snapshot

```bash
mgmt-oke --auth instance_principal reconcile
```

`reconcile` combines worker pools, nodes, OKE add-ons, Cluster Autoscaler
bindings, and Kueue counts. Use JSON when the result will be consumed by a
script:

```bash
mgmt-oke --auth instance_principal --format json reconcile
```

## Verification

The installation is ready for operator use when:

- `pools list` returns each expected worker pool once
- `desired`, `oci_active`, and `k8s_ready` agree for stable pools
- GPU nodes report an allocatable GPU resource
- RDMA nodes appear in `topology list`
- `health run` contains no unexplained warnings or failures
- the command completes without target-discovery or authentication warnings

Review the [Verifying GPU and RDMA Readiness](./verifying-gpu-and-rdma-readiness.md)
guide before changing RDMA capacity.

## Troubleshooting

If Kubernetes works but OCI discovery does not, verify instance-principal IAM
permissions and run:

```bash
mgmt-oke --auth instance_principal --skip-oci nodes list
```

If OCI inventory works but Kubernetes discovery reports an authentication
error, verify that `oci` is on `PATH` because the kubeconfig exec plugin invokes
the OCI CLI. See [Troubleshooting](./troubleshooting.md) for additional checks.
