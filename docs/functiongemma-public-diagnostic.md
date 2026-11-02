# Exact public FunctionGemma diagnostic

The published Arrmes FunctionGemma adapter runs successfully with the existing
MLX implementation. On the same 80 known diagnostic inputs, the untuned base
produced **12 exact raw calls** and the public fine-tune **63**. After the original
Arrmes verifier, the counts were **23** and **68**. This is a useful fine-tuning
gain, with substantial remaining errors. It is not fresh held-out acceptance,
and it does not change the separate frozen 1,000-input evaluation.

The earlier overnight FunctionGemma receipt evaluated the **base only**. No
documented compatibility or resource failure explains the omission of the
fine-tune; it had not been measured in that comparison. This paired run closes
that specific evidence gap without training, prompt changes or output tuning.

## Artifact and method

The [public adapter](https://huggingface.co/NorviTech/arrmes-functiongemma-270m/tree/96e89a722f4789f5eaceee06fe0327dfb9371ba0)
is revision `96e89a722f4789f5eaceee06fe0327dfb9371ba0`. Its 7,620,457-byte LoRA
has SHA-256
`b2af9abc1883d515647be2539cb9192b53016701e1c7b4a71e6c7a9b3d432eac`.
The cached final v5 adapter and public configuration match those exact published
bytes. All 252 finite, nonzero F32 tensors form 126 rank-8 pairs whose shapes
match the pinned BF16
[base](https://huggingface.co/mlx-community/functiongemma-270m-it-bf16/tree/bb327a9ad61044e1496a2bee2365a6b6a6684c72).
The adapter is not a self-contained model or a GGUF file.

The plan was frozen before generation at SHA-256
`94a844c410f02c14828a8275d734106f65a765c42e06eb034dcb175ac252c64d`.
Each condition ran once in a fresh process: base first, then public v5, with
two excluded fixed warmups and all 80 inputs in the same order. There were 164
inference calls total, no retries and no extra accuracy passes. All 36 pinned
source, input and artifact identities matched before and after both conditions.

Both used exact original v1.1.0 runtime bytes, identified as
`a536483a75ada2d4d26f7bc93ee0790695f6e697` (19 Python files), the same 20 v2
tools, greedy decoding, 160 output tokens and native FunctionGemma formatting.
No format translation was applied. The raw native result is scored separately
from the verifier. Prompts were 1,732–1,764 tokens, below the recorded 2,048-token
training limit. The model's native context limit is 32,768 tokens.

Hardware was an Apple M4 Pro, 14 logical CPUs and 48 GiB unified memory,
macOS 27.0.1, Python 3.12.13, MLX 0.32.4, mlx-lm 0.31.1 and Transformers
5.22.1. This qualifies actual Apple Silicon MLX inference only.

## Results and limits

- **Untuned base:** 33/80 native-schema-valid outputs, 16/80 correct tools,
  12/80 exact raw calls (15%), and 23/80 verifier-exact calls (28.75%). It
  proposed unexpected writes on 8/63 non-write inputs.
- **Exact public fine-tune:** 78/80 native-schema-valid outputs, 69/80 correct
  tools, 63/80 exact raw calls (78.75%), and 68/80 verifier-exact calls (85%).
  It proposed unexpected writes on 4/63 non-write inputs. The verifier fixed
  eight wrong calls and damaged three correct raw calls, a net gain of five.

Neither condition executed an unsafe or unauthorized write, or mutated a
fixture before confirmation. There were no unexpected verified write proposals.
The fixture operator confirms only a proposal matching the frozen expected call.
Those safeguards are system evidence; they do not make the raw model's write
errors correct predictions.

The already implemented controlled-fixture track completed strict matching
business observations on 11/35 executable tasks for the base and 29/35 for public
v5. These use the original synthetic gateway fixture, not the newer native
service adapters. They do not establish actual upstream-service correctness or
production safety. No production endpoint or external network was contacted.

Single-pass warm timings are diagnostic, with fixed condition order and shared
hardware rather than a controlled latency qualification:

- Base generation p50/p95: 333.36/447.15 ms; complete routing:
  335.35/449.35 ms; model-plus-fixture execution: 335.45/449.36 ms.
- Public v5 generation p50/p95: 306.87/436.79 ms; complete routing:
  308.83/439.71 ms; model-plus-fixture execution: 310.04/442.24 ms.

The latter timings include inference and the immediate fixture executor; they
exclude a client HTTP round trip and human confirmation. Output lengths differ
between conditions. Process load plus first request was 3.261 s for the base and
2.949 s for public v5; local file caches were not cleared.

Peak process RSS was 1,769,422,848 bytes for the base and 1,771,175,936 bytes for
public v5. MLX peak allocation was 1.364 and 1.469 GB of unified memory,
respectively, not separate VRAM. Full local base/tokenizer/config plus public
adapter files total 577,256,305 bytes. File size is not runtime RAM consumption.

These 80 inputs were already known; one normalized-template overlap with
recorded training is known, and complete public-adapter training lineage is not
established. Do not extrapolate these numbers into a fresh acceptance rate or a
claim of comprehensive native workflow coverage.

## Historical and license boundaries

An older v5 gradient receipt binds
`7d47fc15688b2f77d4d0ca8bb22658ef585addb94ef6402692c84368524ceb75`, the
retained intermediate 320-step checkpoint, rather than public final `b2af9abc…`.
Six old v5 evaluation files bind paths without weight hashes. They cannot prove
the exact public artifact's behavior. Retained fused and 4-bit FunctionGemma
exports are v3 artifacts; their sizes and scores do not qualify public v5.

The [Gemma Terms](https://ai.google.dev/gemma/terms), which list FunctionGemma
in their appendix, apply to the base and its derivatives. Section 3.1 specifies
an agreement copy, a Notice file, use-restriction notices and modification
notices for redistribution. At the inspected revision, the public adapter
repository contains a card/license tag and link, adapter/configuration and
charts, but no full agreement copy or Notice file. Those are concrete packaging
gaps to address before a further derivative distribution. This evidence export
includes no model weights and accepts no new terms on anyone's behalf.

The existing Arrmes export scripts explicitly support Qwen3 only. Upstream
[llama.cpp b11429 registers Gemma3ForCausalLM](https://github.com/ggml-org/llama.cpp/blob/b11429/conversion/gemma.py),
so a separately validated LoRA merge and conversion is feasible without new
training. FunctionGemma GGUF conversion, quantization, native template behavior
and Linux/Windows inference remain unqualified by this run. Google's
[format specification](https://ai.google.dev/gemma/docs/functiongemma/formatting-and-best-practices)
explains why native FunctionGemma output and Hermes compatibility must remain
separate measurements.

## Evidence and reproduction

The [manifest](../experiments/functiongemma-public-v1/manifest.json) pins both
complete raw receipts, the projected pre-inference plan, original controller,
source identity and existing input dependencies. The
[export ledger](../experiments/functiongemma-public-v1/receipt-export.json)
retains every original receipt SHA and documents metadata-only path/hash-map
projection. All 160 complete row trees, native strings, gold calls, numeric
metrics and timings are semantically identical to the owner originals.

The first unchanged-evidence publication scan failed on private paths, the old
synthetic oracle address, a synthetic credential and checksum-map false
positives; its rule counts and owner receipt digest are retained. No baseline
exception was added. The original owner evidence remains unchanged.

For reproduction, the already-public harness differs from the inference
original only in one synthetic gateway-key literal; complete source bytes after
normalizing that literal match. The original runtime snapshot is byte-exact.
The public oracle normalizes 13 synthetic loopback-address strings. A new fixture
execution can therefore differ from the recorded historical C counts; the
recorded results have not been rescored. A/B queries, gold, format and inference
are unaffected. The source snapshot includes its original license and notices.

Offline verification loads no weights:

```sh
python scripts/run_functiongemma_public_diagnostic.py verify
```

To reproduce on Apple Silicon with the exact existing local files and optional
MLX dependencies installed, use a new output path:

```sh
python scripts/run_functiongemma_public_diagnostic.py run \
  --base models/functiongemma-base \
  --adapter adapters/arrmes-public-functiongemma \
  --output-dir work/functiongemma-reproduction-01
```

The helper verifies every local model file against the manifest, extracts the
licensed frozen runtime without old Git ancestry, removes operator service
configuration from child processes and uses the existing isolated fixture.
It never downloads weights, replaces existing output or starts production
services. Partial failures retain completed rows and a safe error type/stage;
the pair stops rather than retrying for accuracy.
