# Portable model smoke

`scripts/smoke_portable_model.py` loads the exact approved experimental
CPU-exported public-adapter GGUF through a native CPU runtime. It needs only
Arrmes' core dependencies and the separately installed `llama-server` executable.
MLX, CUDA, torch and Transformers are not required.

The approved artifact SHA-256 is
`c7d6f1eef1eb4cd0f93769dadca7e270f3c3d470cfb542240733859c7d02f255`.
It contains 396,704,352 bytes. The helper rejects a different artifact before
starting the runtime, checks the runtime source revision, retains native and
verified calls separately, and executes one activity read against an isolated
fixture. Four documented examples and eight operational checks are not an
accuracy benchmark or fresh acceptance set. Memory peaks are not measured.

From a core-only environment with a separately installed b11429 runtime:

```sh
python scripts/smoke_portable_model.py \
  --model /absolute/path/approved-model.gguf \
  --llama-server /absolute/path/llama-server \
  --threads 2 --output work/cpu-model-smoke.json
```

Use the corresponding `.exe` paths in PowerShell. The helper records actual
imported package-file hashes, runtime executable hash/version, model hash,
generation settings, raw calls, verifier calls, fixture result and cleanup.
It has been executed on an Apple Silicon Mac CPU with this exact artifact;
native Windows generation is not established by that run.

## Optional Windows CI

The manually dispatched **Windows CPU model smoke** workflow uses the immutable
v1.2.0 product source, a fresh Python 3.12 x64 environment and the official
`llama-b11429-bin-win-cpu-x64.zip`. The 19,398,918-byte ZIP is fixed to SHA-256
`1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe`.
Its DLLs remain beside the executable. The model is independently fixed to the
approved hash above; a URL cannot select different weights.

The workflow accepts a public HTTPS model URL under the project's GitHub release
assets or NorviTech's Hugging Face namespace. It sends no authentication token
to the download origin. Core installation uses no model or GPU extra. The test
runs with two CPU threads, one slot and a 4,096-token context, then uploads only
its JSON evidence for 30 days. Model and runtime downloads are temporary runner
files, not source-controlled assets.

Run it only after artifact-specific release review accepts and publishes the exact model artifact
with its Apache-2.0 license, modification notice and manifest. At preparation,
this workflow has not run and there is no new Windows generation result.
A future green execution qualifies its recorded hosted runner environment;
it does not qualify owned Windows hardware or NVIDIA acceleration. The existing
Windows compile/`--version` job remains a separate check.

## Artifact inspection

`scripts/audit_gguf_release.py` reads every GGUF metadata value and tensor-index
entry, hashes the complete artifact and scans all serialized bytes for selected
plaintext secret/path signatures. Operator-specific exact deny values can be
provided in a local JSON file containing `label`/`value` pairs. Reports redact
matched values; an optional full metadata dump is for private owner inspection.

```sh
python scripts/audit_gguf_release.py /absolute/path/approved-model.gguf \
  --expected-sha256 c7d6f1eef1eb4cd0f93769dadca7e270f3c3d470cfb542240733859c7d02f255 \
  --output work/gguf-audit.json
```

This bounded inspection is separate from the estate publication gate. A clean
plaintext scan is not exhaustive secret clearance or proof against memorization.
The current full binary gate reports inherited-vocabulary and packed-tensor-byte
matches for this artifact; no gate exception is applied by either helper. Do not
publish weights merely because the lightweight scanner returns success.
