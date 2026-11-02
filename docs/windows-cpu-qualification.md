# Windows CPU qualification

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
Windows CUDA, physical Windows hardware, concurrent load, populated services
and Windows memory profiling remain unqualified.
