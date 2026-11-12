# Windows CPU qualification

## Arrmes v1.7.1 / v10 Native Windows x86_64 Qualification (2026-11-12)

The dispatched [Windows v10 qualification run](https://github.com/spencernorton/arrmes/actions/runs/43238234622)
completed on `2026-11-12` on `Windows Server 2025 AMD64` (`10.0.26100`, 4-vCPU Intel Xeon runner,
64-bit Python `3.12.10` MSC v.1943, core `pip install .` with zero MLX/CUDA/PyTorch/NumPy wheels)
against the official `llama-b11429-bin-win-cpu-x64.zip` runtime (`d81235049`, Clang `20.1.8`,
`llama-server.exe` SHA-256 `7e236243c9b670c6a49410c03186fd575c57a0efe3fd8a21d8727503d571d5b6`).
Every model weight and benchmark JSONL input was fetched anonymously from Hugging Face and SHA-256-verified
before execution (`NorviTech/arrmes-neural-router-v3@f20f3a62222b2297eea86dae31f710052294bea2`,
`NorviTech/arrmes-functiongemma-270m@44ab0fecc06a899995402ec409f1ea6701449ab3`,
`NorviTech/arrmes-qwen3-0.6b@30e35bc6fa950cd8cbc01dc3b3f41f3323e39e90`, and
`datasets/NorviTech/arrmes-bench-v7@fd8911f72cb23e3aae169994beb017eb07f79582`).

Scope boundary: this is **hosted CI qualification on a 4-vCPU Windows Server 2025 AMD64 virtual machine**,
kept strictly distinct from owned physical Windows hardware and Windows NVIDIA CUDA driver execution.
The complete receipt is committed at `experiments/windows-v10-qualification/summary.json`:

- **`ArrmesNeuralRouterV3 v10` (`stdlib_array` fallback, zero C-extension wheels)**:
  - **Three-Platform Bitwise Parity (`acceptance-v7`, `N = 2,580`)**: `1,380 / 1,380` (`100.00%`) positive exact, `1,200 / 1,200` (`100.00%`) `v3` negative exact (`1,195 / 1,200` strict `v2`), and `80 / 80` (`100.00%`) on `overnight-v1`. Output digest `f23e6de6933347e47c2765727c7401cff02b22b5096a822c60a63335f19a9198` matches `macOS ARM64` and `Linux x86_64` byte-for-byte.
  - **Latency & Win32 Memory**: Cold load `65.06 ms`, first route `25.50 ms`, warm model-only `p50 = 3.97 ms` / `p95 = 4.64 ms`, warm routing + verifier `p50 = 4.48 ms` / `p95 = 5.42 ms`, authenticated HTTP loopback (`POST /v1/route`) `p50 = 6.30 ms`, Python `PeakWorkingSetSize = 79.27 MiB`.
- **`arrmes-functiongemma-270m-v10-q4_k_m.gguf` (`261.33 MB`, 4 CPU threads, `ctx=4096`)**:
  - **Latency & Throughput**: Cold load `1,652.59 ms`, first route `3,425.78 ms`, warm routing + verifier `p50 = 372.40 ms` / `p95 = 706.47 ms` (`56.45 tok/s` across `77` warm samples on shared 4-vCPU CI), authenticated HTTP loopback `p50 = 419.30 ms`.
  - **Accuracy & Parity**: `overnight-v1` (`N = 80`) `65 / 80` (`81.25%`) raw and `71 / 80` (`88.75%`) verified; verified output digest `6f7b238409bd5cb289d0b700aa7dd80fd9b9cdf133d8b57589bb1e95df0bb197` matches `Linux x86_64` CPU and `macOS ARM64` Metal GGUF byte-for-byte. Passes `4 / 5` `arrmes.tools.v3` `service_query` probes (`negated_write_trap` abstains `unsupported`) and `8 / 8` isolated operational smoke checks (`0` blocked outbound network attempts).
  - **Win32 Process Memory (`K32GetProcessMemoryInfo`)**: `llama-server.exe` `PeakWorkingSetSize = 460.02 MiB` (`PeakPagefileUsage` commit `257.40 MiB`), Python `79.27 MiB`, combined peak working set `539.29 MiB` (within the `<= 1,536 MiB` budget).
- **`arrmes-qwen3-0.6b-v10-q4_k_m.gguf` (`396.70 MB`, 4 CPU threads, `ctx=4096`)**:
  - **Latency & Throughput**: Cold load `1,388.55 ms`, first route `14,971.57 ms`, warm routing + verifier `p50 = 2,457.73 ms` / `p95 = 4,146.74 ms` (`11.42 tok/s` across `77` warm samples on shared 4-vCPU CI), authenticated HTTP loopback `p50 = 2,897.56 ms`.
  - **Accuracy & Smoke**: `overnight-v1` (`N = 80`) `71 / 80` (`88.75%`) raw and `71 / 80` (`88.75%`) verified (`SHA-256 8794ffc34bb5...`). Passes `5 / 5` (`100%`) `arrmes.tools.v3` `service_query` probes and `8 / 8` isolated operational smoke checks (`4 / 4` raw schema-valid).
  - **Win32 Process Memory (`K32GetProcessMemoryInfo`)**: `llama-server.exe` `PeakWorkingSetSize = 1,131.34 MiB` (`PeakPagefileUsage` commit `803.28 MiB`), Python `79.27 MiB`, combined peak working set `1,210.61 MiB` (within the `<= 1,536 MiB` budget).

## Historical v1.2.0 / C7 Baseline (2026-11-02)

The [single hosted Windows run](https://github.com/spencernorton/arrmes/actions/runs/41436216278)
completed successfully on 2026-11-02. It downloaded the exact public experimental
C7 GGUF, verified its full SHA-256, loaded the official b11429 CPU executable,
made four actual model calls and passed all eight operational checks.

This is **hosted CI qualification on Windows Server 2025 AMD64**, not owned
Windows hardware, NVIDIA testing or broad accuracy acceptance. The installed
product was v1.2.0 at `e76bfcfa`; the workflow/helper came from the protected
v1.2.1 tag at `b937d5c`. Newer native workflows have separate Python fixture/CI
evidence. Do not transfer the four-call result to every service or model.

The calls cover a current-activity read with actual controlled fixture
execution, missing-episode routing, an unsafe-action refusal and a bulk/wildcard
transfer refusal. All four raw and verified tool names match.
The fixture read completed, the two controls abstained, no outbound attempts
occurred and the managed model/sandbox processes were stopped. Neither RAM nor
VRAM was measured. Four calls cannot establish latency percentiles or a model
accuracy rate. The first route took about 18 seconds including its uncached
prompt; it is not a complete process-cold startup distribution.

The inference environment installed the core application and its dependencies;
MLX, CUDA, PyTorch and llama-cpp-python distributions were absent. Generation
used two CPU threads, one slot, context 4096, maximum 64 output tokens and seed 0.
No host drivers or production services were changed.

## Retained evidence and offline verification

Versioned inputs and results live under `experiments/windows-cpu-v1/`:

- The original analyzer, product source manifest and independently retrieved
  GitHub run metadata bind the run, source, model and native executable.
  The public API projection retains every analyzer field and omits unrelated
  repository/commit metadata; the owner archive retains the full response.
- The public receipt changes only the source-path/hash map into ordered digest
  records and uses LF line endings. Raw model output, fixture observations,
  timestamps, numeric values and every source hash remain unchanged.
- `receipt-export.json` identifies the original CRLF receipt and public export.
  The verifier reconstructs the exact original bytes and reproduces the
  original 13 identity/operational checks. An existing output is never replaced.
  The verifier pins the experiment manifest and original analyzer in its own
  code, and executes only the already verified source bytes. A relocated
  evidence directory cannot nominate a replacement analyzer or manifest.
- `model-publication.json` records the five licensed release assets and GitHub
  digests; this CI run independently checked the downloaded model digest.

From the checkout, with ordinary Python and no inference libraries:

```sh
python scripts/verify_windows_cpu_qualification.py \
  --output work/windows-evidence-recheck.json
```

This verifies saved evidence offline; it does not run another Windows benchmark.
For a new operational run, use `scripts/smoke_portable_model.py` with a local
hash-checked model and official CPU executable as described in the
[portable guide](portable-runtime.md). Retain every failed/partial receipt.
Owned physical Windows hardware, Windows NVIDIA CUDA execution, concurrent load, and populated Windows upstream services remain unqualified.
