# Replacing Worker Boot Volumes

`mgmt-oke` supports the two boot volume replacement workflows provided by
Oracle Kubernetes Engine:

- replace the boot volume of a specific managed or self-managed worker while
  preserving its current node configuration
- replace the boot volumes of every worker in a managed node pool while
  applying supported node-pool property updates, including a new image

Boot volume replacement requires an enhanced OKE cluster. It is supported for
virtual machine and bare metal workers.

## Choose The Operation

| Requirement | Command | Supported pool model |
| --- | --- | --- |
| Repair one worker without terminating its compute instance | `nodes boot-volume-replace` | Managed OKE or self-managed |
| Apply a new image to existing workers in place | `pools boot-volume-replace --image-id` | Managed OKE only |
| Change boot size, boot KMS key, Kubernetes version, node metadata, or SSH key during BVR | `pools boot-volume-replace` | Managed OKE only |
| Change the image of one individual worker | Not supported by the OKE individual-node BVR API | Use managed-pool BVR or create a replacement pool |

An individual-node BVR deliberately preserves the existing image and
configuration. It does not accept `--image-id`. OKE supports image changes
during a managed node-pool BVR because the updated property is applied
consistently to every worker in that pool.

## Prerequisites

- an enhanced OKE cluster
- a healthy worker pool for pool-wide rolling BVR; individual BVR can repair a
  selected NotReady worker
- OCI permission to inspect the cluster, node pools, compute instances, images,
  work requests, and boot volumes
- OCI permission to update managed node pools and invoke cluster-node BVR
- Kubernetes permission to list nodes and pods, dry-run Eviction requests, and
  manage the mutation Lease
- PodDisruptionBudgets and replica counts that permit the planned disruption

For BVR using a custom image, the OKE cluster principal must be able to read
that image. Oracle documents the following policy when access is not already
provided:

```text
ALLOW any-user to read instance-images in TENANCY where request.principal.type = 'cluster'
```

Scope the policy according to the tenancy's security design.

## Replace One Worker Boot Volume

Always review the plan first:

```bash
mgmt-oke nodes boot-volume-replace <node-name-or-ip> \
  --dry-run \
  --format json
```

Submit the operation and wait for complete recovery:

```bash
mgmt-oke nodes boot-volume-replace <node-name-or-ip> \
  --wait
```

`nodes bvr` and `nodes boot-volume-swap` are aliases for the same command.

The node can be selected by Kubernetes name, Slinky name, internal IP,
provider ID, or compute instance OCID. Exact field selection is also supported:

```bash
mgmt-oke nodes boot-volume-replace \
  --fields pool=oke-gpu,ready=true \
  --wait
```

Multiple individual-node operations require `--wait`. The CLI submits and
verifies them sequentially so it does not disrupt every selected worker at
once.

### Individual Options

| Option | Purpose |
| --- | --- |
| `--eviction-grace <duration>` | OKE cordon-and-drain grace duration from `PT0M` through `PT60M`. Default: `PT60M`. |
| `--force-after-grace` | Allow OKE to continue after the grace duration even when cordon or drain has not completed. |
| `--delete-emptydir-data` | Acknowledge loss of pod-local `emptyDir` data. |
| `--force` | Acknowledge eviction of pods without a controller. |
| `--allow-system-pool` | Permit BVR of an `oke-system` worker after explicit review. |
| `--wait` | Wait for OKE, boot volume, node, and accelerator convergence. |
| `--timeout <seconds>` | Set the total wait for each selected worker. Default: `7200`. |

## Replace A Managed Pool And Change Its Image

At least one supported worker property must change. To move every existing
worker to a new image:

```bash
mgmt-oke pools boot-volume-replace <managed-pool> \
  --image-id <replacement-image-ocid> \
  --maximum-unavailable 1 \
  --dry-run \
  --format json
```

After reviewing the effective image and operation plan:

```bash
mgmt-oke pools boot-volume-replace <managed-pool> \
  --image-id <replacement-image-ocid> \
  --maximum-unavailable 1 \
  --wait
```

`pools bvr` is an alias for the same command.

The replacement image must be Linux, advertise compatibility with the pool's
shape in every selected availability domain, and use the same Linux
distribution as the current image. BVR cannot change the worker shape,
placement, subnet, CNI, or other unsupported node-pool properties.

### Supported Pool Updates

| Option | Updated OKE property |
| --- | --- |
| `--image-id <ocid>` | `ImageId` |
| `--boot-volume-size <gib>` | `BootVolumeSizeInGBs`; reduction is refused |
| `--boot-volume-kms-key-id <ocid>` | `KmsKeyId` |
| `--kubernetes-version <version>` | `KubernetesVersion` |
| `--node-metadata KEY=VALUE` | Merge non-reserved `NodeMetadata`; repeatable |
| `--ssh-public-key-file <path>` | `SshPublicKey` |
| `--maximum-unavailable <count-or-percent>` | Rolling BVR parallelism; must be positive |

Pool BVR also supports `--delete-emptydir-data`, `--force`,
`--allow-system-pool`, `--wait`, `--timeout`, `--poll-interval`, `--lock`,
`--dry-run`, and `--yes`.

Self-managed Cluster Network and standalone Instance Pool workers are not
eligible for pool-wide property updates through OKE. Select one of their
workers with `nodes boot-volume-replace`; its existing image, cloud-init, OKE
bootstrap, networking, and instance configuration are preserved.

## Safety And Execution

Before either BVR operation, the CLI:

1. resolves the cluster and compartment from kubeconfig and OKE
2. confirms the cluster type is `ENHANCED_CLUSTER`
3. requires complete OCI pool inventory
4. requires a pool-wide BVR target to be fully healthy; an individual target
   can be NotReady when BVR is being used as a repair
5. refuses Cluster Autoscaler-owned and Slinky-managed workers
6. protects `oke-system` unless explicitly allowed
7. dry-runs Kubernetes Eviction admission
8. requires acknowledgement for `emptyDir` data and unmanaged pods
9. records the current instance and boot volume identities
10. acquires the Kubernetes mutation Lease
11. rediscovers ownership, membership, pod state, and boot volume identity
    immediately before submission

For a specific node, the CLI invokes OKE
`ReplaceBootVolumeClusterNode`. OKE cordons and drains the node, stops the
existing compute instance, replaces its boot volume, and restarts the same
instance.

For a managed pool, the CLI invokes OKE `UpdateNodePool` with
`cycleModes=["BOOT_VOLUME_REPLACE"]` and the reviewed property updates. OKE
cycles the workers according to `maximumUnavailable`.

## Wait Verification

With `--wait`, completion requires:

- the OCI work request has not failed or been canceled
- every original compute instance OCID is still present
- every original internal IP address is unchanged
- every boot volume OCID changed
- the Kubernetes boot ID changed when it was available before BVR
- every selected or pool-cycled worker is Ready and schedulable
- individual BVR preserves desired and active OCI capacity and restores the
  selected worker; pool-wide BVR requires desired, active OCI, and Kubernetes
  Ready counts to match
- allocatable GPU capacity is restored for GPU pools
- RDMA topology is restored for RDMA workers
- `nvidia.com/rdma-vf` is restored when the NVIDIA Network Operator is active
- requested managed node-pool properties are visible from OKE

The operation preserves the compute instance, but it is disruptive. Data stored
only on the old boot volume or in pod-local `emptyDir` storage is not preserved.
Persistent volumes and external file systems follow their own lifecycle.

## Example Dry-Run Output

The individual operation plan makes the preserved configuration explicit:

```json
[
  {
    "current_size": 1,
    "decrement_size": null,
    "details": {
      "eviction_grace_duration": "PT60M",
      "force_after_grace": false,
      "instance_ocid": "instance-example",
      "kind": "node-pool",
      "old_boot_volume_id": "boot-volume-example",
      "preserves_existing_configuration": true
    },
    "operation": "node-boot-volume-replace",
    "owner": "oke",
    "pool": "oke-gpu",
    "status": "planned",
    "steps": [
      "ask OKE to cordon and drain the selected worker",
      "stop the existing compute instance",
      "replace its boot volume while preserving node configuration",
      "restart the same instance",
      "verify node identity, Ready state, and GPU/RDMA resources"
    ],
    "target": "gpu-node-1",
    "target_size": 1,
    "warnings": [
      "The current boot volume is replaced; data stored only on that boot volume is not preserved.",
      "The instance OCID and network address are preserved, but workloads are disrupted while OKE cordons, drains, stops, and restarts it.",
      "Individual-node BVR preserves the node's existing image and configuration. Use pools boot-volume-replace to change a managed pool image."
    ],
    "workload_pods": 0
  }
]
```

For a managed-pool image update, the dry-run includes separate `current`,
`effective`, and `updates` fields. Metadata values and SSH key content are not
printed.

## Infrastructure As Code

An individual BVR preserves the existing node configuration and does not change
the declared pool properties.

A managed-pool BVR that changes the image, boot settings, Kubernetes version,
metadata, or SSH key mutates the OKE node pool directly. Update the
corresponding Terraform or OCI Resource Manager inputs before the next apply so
declared state does not reverse the operation.

## Self-Managed CA Rotation

Individual BVR preserves the original self-managed worker bootstrap. If the
OKE cluster CA credentials were rotated after that worker joined, update the
self-managed instance's OKE CA bootstrap metadata before BVR. Otherwise the
restarted worker can fail to rejoin.

See Oracle's
[Replacing Boot Volumes of Worker Nodes](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/replace-boot-volume-worker-node-top.htm)
and
[Updating Worker Nodes by Replacing Boot Volumes](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengupgradingimageworkernode_topic-InPlace_Worker_Node_Update_By_Cycling_and_Replacing_Boot_Volumes.htm)
for the underlying OKE behavior.
