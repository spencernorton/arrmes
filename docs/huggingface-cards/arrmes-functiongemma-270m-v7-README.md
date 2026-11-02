---
license: gemma
base_model: google/functiongemma-270m-it
library_name: mlx
pipeline_tag: text-generation
language:
  - en
tags:
  - function-calling
  - tool-use
  - mlx
  - functiongemma
  - self-hosted
  - agent-safety
  - arrmes
  - norvitech
---

# Arrmes-FunctionGemma-270M (Published MLX LoRA Baseline & v7 Adapter)

**Arrmes-FunctionGemma-270M** is a 270M-parameter function-calling LoRA adapter fine-tuned from `google/functiongemma-270m-it` for the 20-action `arrmes.tools.v2` self-hosted media-operations contract (`<start_function_call>call:name{...}<end_function_call>`).

- **Preserved Root Baseline (`adapters.safetensors`):** The unchanged root adapter from initial publication (`96e89a722f4789f5eaceee06fe0327dfb9371ba0`) contains **252 float32 LoRA tensors** (`7,620,457` bytes / ~`7.3 MiB`, SHA-256 `b2af9abc9027ffbc7ff5bc228de788bf37583804e10796d34fb259bb2f4c7036`) for the separately downloaded `google/functiongemma-270m-it` bf16 base (`536,222,768` bytes / ~`511.4 MiB`).
- **Subdirectory `v7-lora/` (`v1.4.0`):** Contains the `v7` LoRA adapter trained on `TrainPool-v7` (`5,827` examples, `seed=20261102`, `200` iterations, `lr=1e-4`) and its provenance manifest (`v7-lora/provenance.json`).
- **Gemma Terms of Use Notice:** Distribution and use of these weights are subject to the [Gemma Terms of Use](https://ai.google.dev/gemma/terms) and include [`GEMMA_TERMS_NOTICE.txt`](GEMMA_TERMS_NOTICE.txt) per Section 3.1.

![Arrmes v1.4.0 Locked Acceptance-v5 Accuracy and Confidence Bounds](arrmes_v7_acceptance_v5_accuracy_bounds.png)

## Evaluation Results (Paired Base vs Fine-Tuned & Locked `acceptance-v5`)

Raw model output (`raw_unverified`, Track A), verifier-processed proposals (`raw` / `verify_proposal`, Track B), and controlled fixture execution (Track C) are measured and reported separately:

- **Predeclared 80-Case Paired Diagnostic (Identical BF16 Base Without vs With Root `v5` Adapter, [`docs/functiongemma-public-diagnostic.md`](https://github.com/spencernorton/arrmes/blob/v1.4.0/docs/functiongemma-public-diagnostic.md)):**
  - **Untuned `google/functiongemma-270m-it` BF16 Base:** **`12 / 80` (`15.0%`) raw exact**, **`23 / 80` (`28.8%`) after verification**, and **`11 / 35` strict fixture workflows**.
  - **Root Public `v5` Adapter (`96e89a7...`):** **`63 / 80` (`78.8%`) raw exact** (`+51` calls), **`68 / 80` (`85.0%`) after verification** (`+45` calls), and **`29 / 35` strict fixture workflows** (`0` unexpected write proposals). One normalized template in the 80-case diagnostic overlaps training (`ON1-023`), so it is recorded as a known-input diagnostic rather than a template-disjoint acceptance suite.
- **Locked `acceptance-v5` Stratified 200-Case Generative Comparison (`seed=7007`, [`NorviTech/arrmes-bench-v5`](https://huggingface.co/datasets/NorviTech/arrmes-bench-v5)):**
  - Evaluated on a stratified 200-case slice (`100` positive + `100` hard-negative/safety) with **0 exact query overlaps** and **0 entity overlaps** against `TrainPool-v7` (`5,827` rows) and `acceptance-v4` (`1,000` rows):
    - **Public `FunctionGemma-270M v5` (`bf16 + root LoRA`)**: Positive Track A **`68 / 100` (`68.0%`)**, Track B **`78 / 100` (`78.0%`)**; Hard-Negative Track A `33 / 100`, Track B **`79 / 100` (`79.0%`)**, **`0` unauthorized writes**.
    - **Trained `FunctionGemma-270M v7` (`4-bit MLX` fused from `v7-lora/`, `175.83 MiB`)**: Positive Track A **`70 / 100` (`70.0%`)**, Track B **`91 / 100` (`91.0%`, `+13.0 pp` over `v5`)**; Hard-Negative Track A `35 / 100`, Track B **`88 / 100` (`88.0%`, `+9.0 pp` over `v5`)**, **`0` unauthorized writes**.
- **Historical `sealed-acceptance-v3` (`N = 314`) Audit Note:**
  - The 4,389-row historical training file shares 37 normalized templates with 261/314 `sealed-v3` rows (`226` non-write opportunities). Use [`NorviTech/arrmes-bench-v5`](https://huggingface.co/datasets/NorviTech/arrmes-bench-v5) for zero-leakage evaluation, and [`NorviTech/arrmes-neural-router-v3`](https://huggingface.co/NorviTech/arrmes-neural-router-v3) (`7.52 MiB`) when `>=99%` raw Track A accuracy and `arrmes.tools.v3` coverage are required.

![Cross-Platform Runtime Latency and Memory Footprint](arrmes_v7_runtime_latency_memory_footprint.png)

## Measured Runtime Profile (Apple M4 Pro, `v1.4.0`)

- **`FunctionGemma-270M v7` (`4-bit MLX`, `184,369,899` bytes / `175.83 MiB` weight files on disk):**
  - Cold load + static prompt KV cache warmup: **`869.67 ms`**
  - Warm model generation (`548.3 tok/s`): **`p50 = 43.30 ms / p95 = 46.84 ms`**
  - Warm routing + `verify_proposal`: **`p50 = 43.54 ms / p95 = 47.07 ms`**
  - Unified memory on Apple M4 Pro: MLX Metal peak **`1,060.96 MiB`** (active **`180.02 MiB`**), process RSS **`797.53 MiB`** (shared unified memory; do not sum).

## Quickstart (MLX)

```python
from arrmes.model import LocalModel

model = LocalModel(
    "models/functiongemma",
    family="functiongemma",
    adapter="adapters/arrmes-functiongemma-270m",  # root v5 baseline or v7-lora/
    contract_version="v2",
)

res = model.infer("Is anyone streaming from Plex at this moment?")
print(res["raw_unverified"])  # Track A direct model output
print(res["raw"])             # Track B verified proposal
# <start_function_call>call:now_playing{}<end_function_call>
```

## Licence & Notice

Gemma is provided under and subject to the [Gemma Terms of Use](https://ai.google.dev/gemma/terms). See [`GEMMA_TERMS_NOTICE.txt`](GEMMA_TERMS_NOTICE.txt). Part of the [NorviTech Suite](https://norvitech.com) and [Arrmes `v1.4.0`](https://github.com/spencernorton/arrmes/releases/tag/v1.4.0).
