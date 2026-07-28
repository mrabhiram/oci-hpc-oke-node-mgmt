# Worker Bootstrap and Storage

Pool creation inherits the source worker's complete OKE bootstrap. Optional
storage settings compose the same worker scripts used by the OCI HPC OKE stack
instead of replacing the cluster-specific bootstrap.

## Bundled Worker Assets

The package includes version-pinned, unmodified copies of the four worker
scripts used by `oci-hpc-oke`:

- `oke-ubuntu-cloud-init.sh`
- `oke-nvme-raid.sh`
- `oke-fss-mount.sh`
- `oke-lustre-mount.sh`

The upstream commit is recorded in
`src/oke_hpc_mgmt/assets/oci_hpc_oke/README.md`. Scripts are embedded in
cloud-init with `write_files`; new workers do not download mutable scripts from
the default branch during first boot.

The upstream worker order is preserved:

1. initialize local NVMe RAID
2. run OKE package installation and node bootstrap
3. mount existing FSS exports
4. mount existing Lustre file systems

NVMe runs first because the official script bind-mounts container, kubelet, and
pod paths onto the array before OKE starts those services.

## Storage Modes

`--storage-mode` controls inherited official storage commands:

| Mode | Behavior |
| --- | --- |
| `inherit` | Preserve source cloud-init exactly. New storage options are rejected. |
| `append` | Preserve inherited commands and append the selected storage bootstrap. |
| `replace` | Remove inherited official NVMe, FSS, and Lustre commands/files, then add only the selected configuration. |

Use `replace` when changing an inherited mount or RAID configuration. Selecting
`replace` without a storage option removes official storage bootstrap from the
new pool while preserving OKE bootstrap.

## Local NVMe RAID

```bash
mgmt-oke pools create cpu-dense \
  --type cpu \
  --count 2 \
  --from-pool oke-cpu \
  --shape VM.DenseIO.E5.Flex \
  --ocpus 16 \
  --memory-in-gbs 192 \
  --storage-mode replace \
  --nvme-raid-level 0 \
  --nvme-device-pattern '/dev/nvme*n1' \
  --nvme-mount-path /mnt/nvme \
  --dry-run
```

The selected shape must advertise local disks. The official script creates the
array and filesystem, mounts it, and bind-mounts:

- `/var/lib/containers`
- `/var/lib/kubelet`
- `/var/log/pods`

The operation is destructive to matching uninitialized local NVMe devices on
new workers. Review the device pattern and shape before applying.

## Existing FSS Export

```bash
mgmt-oke pools create gpu-fss \
  --type gpu \
  --count 2 \
  --from-pool oke-gpu \
  --storage-mode replace \
  --fss-mount-target-ip <mount-target-ip> \
  --fss-export-path <export-path> \
  --fss-mount-path /mnt/oci-fss \
  --dry-run
```

The command mounts an existing export on each new worker. It does not create
the FSS file system, mount target, export, subnet, NSG rules, IAM policy,
Kubernetes PersistentVolume, or PersistentVolumeClaim. Those resources must
already exist and be reachable from the worker subnet.

## Existing OCI Lustre File System

```bash
mgmt-oke pools create rdma-lustre \
  --type rdma \
  --count 2 \
  --from-pool oke-rdma \
  --storage-mode replace \
  --lustre-management-address <management-service-address> \
  --lustre-filesystem-name <filesystem-name> \
  --lustre-mount-path /mnt/oci-lustre \
  --dry-run
```

The command mounts an existing OCI Lustre file system. It does not create the
file system, cluster placement group, subnet, NSG rules, service policy,
PersistentVolume, or PersistentVolumeClaim. The custom image must contain a
compatible Lustre client and `lnet` kernel module.

## Combined Bootstrap

Selections can be combined:

```bash
mgmt-oke pools create rdma-data \
  --type rdma \
  --count 2 \
  --from-pool oke-rdma \
  --storage-mode replace \
  --nvme-raid-level 0 \
  --fss-mount-target-ip <mount-target-ip> \
  --fss-export-path <export-path> \
  --lustre-management-address <management-service-address> \
  --lustre-filesystem-name <filesystem-name> \
  --dry-run \
  --format json
```

### Example Storage Dry-Run Output

This excerpt is derived from the command above against a live RDMA source
template; endpoint values are examples:

```json
{
  "effective": {
    "storage": {
      "fss_mounts": [
        {
          "export_path": "/training",
          "mount_path": "/mnt/oci-fss",
          "mount_target_ip": "10.0.0.5"
        }
      ],
      "lustre_mounts": [
        {
          "filesystem_name": "training",
          "management_address": "10.0.0.6",
          "mount_path": "/mnt/oci-lustre"
        }
      ],
      "mode": "replace",
      "nvme_raid": {
        "device_pattern": "/dev/nvme*n1",
        "level": 0,
        "mount_path": "/mnt/nvme"
      }
    }
  },
  "operation": "pool-create",
  "owner": "compute-management",
  "status": "planned"
}
```

## Additional Bootstrap

Add a cloud-init part:

```bash
mgmt-oke pools create cpu-custom \
  --type cpu \
  --count 1 \
  --from-pool oke-cpu \
  --cloud-init-file ./worker-extra.yaml \
  --dry-run
```

Run scripts through the OKE bootstrap hooks:

```bash
mgmt-oke pools create gpu-custom \
  --type gpu \
  --count 1 \
  --from-pool oke-gpu \
  --pre-bootstrap-script-file ./pre-oke.sh \
  --post-bootstrap-script-file ./post-oke.sh \
  --kubelet-extra-args '<kubelet-arguments>' \
  --dry-run
```

`pre_oke` and `post_oke` are base64-encoded into instance metadata, matching the
official stack bootstrap contract. Generic metadata overrides cannot replace
reserved OKE keys.
