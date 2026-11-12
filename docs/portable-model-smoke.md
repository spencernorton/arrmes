# Portable model smoke

`scripts/smoke_portable_model.py` loads an exact approved `v10` (or retained `C7`)
GGUF artifact through a native CPU runtime. It needs only Arrmes' core
dependencies and the separately installed `llama-server` executable (`b11429` /
`d81235049`). MLX, CUDA, torch and Transformers are not required.

Approved `v10` release GGUF SHA-256 digests:

- `arrmes-qwen3-0.6b-v10-q4_k_m.gguf` (`396,704,640` bytes, default `--family hermes`):
  `0e5310b8d22a0c89826b8c9cd9e857656b301b0de4b63cdf27285338ef09dd84`
- `arrmes-functiongemma-270m-v10-q4_k_m.gguf` (`261,330,112` bytes, `--family functiongemma`):
  `1b72dcd37745d50ff6f63f71e13f4274079239d8989e098a728e1dc277885123`
- Retained historical `C7` artifact (`396,704,352` bytes, `--expected-sha256 c7d6f1ee...`):
  `c7d6f1eef1eb4cd0f93769dadca7e270f3c3d470cfb542240733859c7d02f255`

The helper rejects a different artifact before starting the runtime, checks the
runtime source revision, retains native and verified calls separately, samples
native OS child peak resident/commit RAM before shutdown (via Win32
`K32GetProcessMemoryInfo` on Windows and `/proc/<pid>/status` `VmHWM` on Linux),
and executes one activity read against an isolated fixture. Four documented
examples and eight operational checks are operational smoke evidence; full `v10`
Windows x86_64 latency, memory, `overnight-v1` (`N = 80`), `v3` `service_query`,
and `ArrmesNeuralRouterV3` `N = 2,580` bitwise parity are measured separately by
`scripts/qualify_windows_v10.py`.

From a core-only environment with a separately installed `b11429` runtime:

```sh
python scripts/smoke_portable_model.py \
  --model /absolute/path/arrmes-qwen3-0.6b-v10-q4_k_m.gguf \
  --llama-server /absolute/path/llama-server \
  --threads 4 --output work/cpu-model-smoke.json
```

Use the corresponding `.exe` paths in PowerShell. The helper records actual
imported package-file hashes, runtime executable hash/version, model hash,
generation settings, native peak RAM, raw calls, verifier calls, fixture result
and cleanup.

## Dispatched Windows x86_64 Qualification CI

The manually dispatched **Windows CPU model smoke** workflow
(`.github/workflows/windows-model-smoke.yml`) installs the current checkout into
a fresh Python 3.12 x64 environment (`pip install .`, zero MLX/CUDA/PyTorch
extras), downloads the official `llama-b11429-bin-win-cpu-x64.zip` (`19,398,918`
bytes, SHA-256 `1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe`),
and anonymously fetches and SHA-256-verifies the pinned `v10` Hugging Face
artifacts (`NorviTech/arrmes-neural-router-v3@f20f3a62222b2297eea86dae31f710052294bea2`,
`NorviTech/arrmes-functiongemma-270m@44ab0fecc06a899995402ec409f1ea6701449ab3`,
and `NorviTech/arrmes-qwen3-0.6b@30e35bc6fa950cd8cbc01dc3b3f41f3323e39e90`) plus the public benchmark split (`datasets/NorviTech/arrmes-bench-v7@fd8911f72cb23e3aae169994beb017eb07f79582`).
It then runs `scripts/qualify_windows_v10.py` across all three models plus both
8-check isolated smoke suites and uploads `work/windows-v10-qualification.json`.
See [Windows CPU qualification](windows-cpu-qualification.md) for the recorded
`v10` Windows Server 2025 AMD64 receipts and the explicit CI-versus-physical-hardware
boundary.

## Artifact inspection

`scripts/audit_gguf_release.py` reads every GGUF metadata value and tensor-index
entry, hashes the complete artifact and scans all serialized bytes for selected
plaintext secret/path signatures. Operator-specific exact deny values can be
provided in a local JSON file containing `label`/`value` pairs. Reports redact
matched values; an optional full metadata dump is for private owner inspection.

```sh
python scripts/audit_gguf_release.py /absolute/path/arrmes-qwen3-0.6b-v10-q4_k_m.gguf \
  --expected-sha256 0e5310b8d22a0c89826b8c9cd9e857656b301b0de4b63cdf27285338ef09dd84 \
  --output work/gguf-audit.json
```

This bounded inspection is separate from the estate publication gate. A clean
plaintext scan is not exhaustive secret clearance or proof against memorization.
