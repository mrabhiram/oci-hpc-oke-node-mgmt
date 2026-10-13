# NCCL Profile Coverage

This matrix distinguishes source coverage, implemented rendering, and actual
live validation. A manifest or profile being present does not establish that
the shape has passed a benchmark.

## Profile Storage

The installed Python package contains reviewed settings in
`src/oke_hpc_mgmt/benchmarks/profiles.py`. The structured renderer in
`src/oke_hpc_mgmt/benchmarks/manifests.py` creates MPIJobs using those settings
and the confirmed participant, namespace, admission, and network selections.

The tool does **not** fetch GitHub YAML at execution time. It also does not
bundle every upstream YAML file unchanged. Each run records the test options
and profile fingerprint; evidence includes resolved container image digests.
Workers obtain the configured test image from its container registry, not GitHub.

Profiles were reviewed against OCI HPC OKE revision
`6c94a4201d9ec6887f37eceeca325ec18b03b703`. That revision contains 40 NCCL YAML
files across host-network, virtual-function, and Dranet directories, with
separate MPI Operator and Kueue variants. This tool combines common rendering
logic rather than maintaining one copied YAML per scheduler and shape.

## NVIDIA Coverage

| Shape | Implemented profiles | Live validation in this project |
| --- | --- | --- |
| `BM.GPU4.8` | Local, host-network, VF | Local eight-rank and two-worker whole-pool sixteen-rank host-network correctness/coverage passes; RDMA observed in the latter; VF not yet validated |
| `BM.GPU.A100-v2.8` | Local, host-network, VF | Not yet validated |
| `BM.GPU.B4.8` | Local, host-network, VF | Not yet validated |
| `BM.GPU.H100.8` | Local, host-network, VF | Not yet validated |
| `BM.GPU.H200.8` | Local, host-network, VF | Not yet validated |
| `BM.GPU.B200.8` | Local, host-network, VF | Not yet validated |
| `BM.GPU.B300.8` | Local, host-network, VF | Not yet validated |
| `VM.GPU.A10.1`, `VM.GPU.A10.2` | Local-only extensions | Not yet validated |
| `BM.GPU.RTXPRO.8` | Local, host-network; VF rejected | Not yet validated |
| `BM.GPU.GB200.4`, `BM.GPU.GB200-v2.4`, `BM.GPU.GB200-v3.4` | Not yet implemented; specialized ComputeDomain/resource-claim and MNNVL handling required | Not yet validated |
| `BM.GPU.GB300.4` | Not yet implemented; specialized transport handling required | Not yet validated |

Dranet execution is not yet implemented, including its upstream A100 example.
The existence of a standard A100 host/VF profile does not imply Dranet support.
Unsupported requests fail instead of substituting another shape or transport.

The RTXPRO host profile preserves the upstream Tree algorithm, minimum eight
channels, one QP per connection, exact eight-HCA list, and UCX MPI selection.
The pinned source provides no RTXPRO VF manifest, so the tool does not infer VF
support from the number of HCAs. Automatic VF selection is refused for this
shape; explicitly selecting `--network host` uses the reviewed host profile.

## AMD Coverage

Upstream provides RCCL examples for `BM.GPU.MI300X.8`, `BM.GPU.MI355X.8`, and
`BM.GPU.MI355X-v1.8`, with transport coverage varying by shape. RCCL rendering,
result interpretation, and live validation are not yet implemented in this tool.
NCCL tests cannot be used as proof of AMD support.

## Validation Boundary

- Automated planning/rendering tests exercise profiles and rejection paths.
- API/admission dry runs prove that a selected manifest was accepted for validation,
  not that GPU communication or RDMA worked.
- The recorded local A100 pass proves eight-rank participation and correctness
  for that execution only. Its performance remains ungraded without a threshold.
- A fresh two-worker whole-pool A100 execution covered two different local
  blocks, all sixteen ranks, and observed RDMA channels. This does not establish
  default-pair or per-local-block execution, VF support, larger pool scaling,
  specialized adapters, or other shapes. Those remain separate live requirements.

See [NCCL Healthchecks](nccl-healthchecks.md) for current commands and fresh
sanitized examples, and [the upstream source tree](https://github.com/oracle-quickstart/oci-hpc-oke/tree/6c94a4201d9ec6887f37eceeca325ec18b03b703/manifests/nccl-tests)
for the reviewed manifest inventory.
