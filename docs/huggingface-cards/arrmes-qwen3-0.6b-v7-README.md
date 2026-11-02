---
license: apache-2.0
base_model: Qwen/Qwen3-0.6B
library_name: mlx
pipeline_tag: text-generation
language:
  - en
tags:
  - function-calling
  - tool-use
  - mlx
  - apple-silicon
  - self-hosted
  - agent-safety
  - arrmes
  - norvitech
---

# Arrmes-Qwen3-0.6B (Published MLX LoRA Baseline & v7 Adapter)

**Arrmes-Qwen3-0.6B** provides function-calling LoRA adapters for a separately downloaded `Qwen/Qwen3-0.6B` base and the 20-action `arrmes.tools.v2` self-hosted media-operations contract (`e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8`).

- **Preserved Root Baseline (`adapters.safetensors`):** The unchanged root adapter from initial publication (`8a9bcf224e6f9fd6bf238267093c70360ee837cb`) has SHA-256 `a9a7ab8705d68e16f0e9597d96e2ddf58b6f3dfffcecf683e2b36c8b11f23fab`. Its 196 LoRA tensors are stored in **float32** (`345,563,976` bytes / ~`345.6 MB` combined with the 4-bit base `mlx-community/Qwen3-0.6B-4bit@73e3e38d981303bc594367cd910ea6eb48349da8`; weight-file size on disk is not runtime RAM). It differs across all 196 tensors from an internal research checkpoint that shared the `v6` directory label.
- **Subdirectory `v7-lora/` (`v1.4.0`):** Contains the `v7` LoRA adapter trained on `TrainPool-v7` (`5,827` examples, `seed=20261102`, `200` iterations, `lr=5e-5`) and its provenance manifest (`v7-lora/provenance.json`).
- **Companion Neural Router:** For sub-millisecond CPU routing across both `arrmes.tools.v2` (20 tools) and `arrmes.tools.v3` (23 tools including 31 native `service_query` pairs and Bazarr subtitle workflows), see [`NorviTech/arrmes-neural-router-v3`](https://huggingface.co/NorviTech/arrmes-neural-router-v3) (`7.52 MiB`, `p50 = 0.674 ms`).

![Arrmes v1.4.0 Locked Acceptance-v5 Accuracy and Confidence Bounds](arrmes_v7_acceptance_v5_accuracy_bounds.png)

## Highlights & Architectural Scope

- **20-Name Neural Contract (`arrmes.tools.v2`):** Defines 18 service-operation names and two control names across ten named integration domains: Sonarr (`tv_status`, `tv_missing`, `tv_search_missing`), Radarr (`movie_status`, `movie_search`), Lidarr (`music_status`, `music_search`), Tautulli (`now_playing`, `watch_history`), qBittorrent (`torrent_list`, `torrent_pause`, `torrent_resume`), Seer (`seer_discover`), Request Portal (`request_status`, `request_media`), Armiger (`armiger_parse`), Bitmagnet (`bitmagnet_search`), DH-Waypoint (`waypoint_status`), and structured abstention (`clarify`, `unsupported`). Expanded `arrmes.tools.v3` native operations (`service_query`, `bazarr_subtitle_search`, `bazarr_subtitle_download`) are handled by [`NorviTech/arrmes-neural-router-v3`](https://huggingface.co/NorviTech/arrmes-neural-router-v3) or explicit grammar routing, not by this `v2` generative adapter.
- **Measured Apple Silicon MLX Latency & Memory (`v1.4.0`):** On an Apple M4 Pro (`macOS 26.3 ARM64`, 48 GiB unified memory) with static system-prompt KV caching (`make_prompt_cache`), the 4-bit `Qwen3-0.6B v7` checkpoint (`346.93 MB` / `330.86 MiB` on disk) runs warm generation at **`p50 = 114.23 ms / p95 = 121.62 ms`** (`256.7 tok/s`) and warm routing + `verify_proposal` at **`p50 = 114.40 ms / p95 = 121.86 ms`**, with MLX Metal unified peak memory **`1,593.34 MiB`** (active `343.62 MiB`) and process RSS **`805.23 MiB`** (unified memory; do not sum). The earlier ~230–245 ms figure timed the unbatched generation core alone.
- **Execution Safeguards:** Model outputs are untrusted proposals. Runtime schema validation (`verify_proposal`), caller scope checks, and single-use HMAC-SHA256 confirmation tokens (`WriteConfirmationStore`) are mandatory before any consequential write executes.

![Frozen Acceptance-v4 Error Taxonomy and Verifier Repair Progression](arrmes_v7_error_taxonomy_and_verifier_repair.png)

## Evaluation Results (Raw Track A vs Verified Track B)

Raw model output (`raw_unverified`, Track A), verifier-processed proposals (`raw` / `verify_proposal`, Track B), and controlled fixture execution (Track C) are measured and reported separately:

- **Frozen 1,000-Case Diagnostic (`acceptance-v4`, `v1.4.0` Verifier Replay):**
  - On the frozen 1,000-case corpus ([`docs/benchmark-acceptance-v4.md`](https://github.com/spencernorton/arrmes/blob/v1.4.0/docs/benchmark-acceptance-v4.md)), the unchanged public fine-tuned `Qwen3-0.6B` scores **`770 / 1,000` (`77.00%`) raw exact** versus `396 / 1,000` (`39.60%`) for the untuned `Qwen3-0.6B` base and `782 / 1,000` (`78.20%`) for the `Qwen3-4B` reference.
  - While an intermediate pre-final verifier snapshot (`e5e76b8`) regressed from `717/1,000` to `678/1,000` by damaging 135 correct predictions on contrastive subordinate clauses (`rather than`, `instead of`, `do not`) and multi-entity Lidarr `(artist, album)` cards, the general grammatical repairs in **Arrmes `v1.4.0`** restore and improve verified accuracy on those identical raw predictions to **`981 / 1,000` (`98.10%`)**, repairing **211** model errors with **`0` correct predictions damaged** and **`0` unexpected write proposals**.
- **Locked `acceptance-v5` Stratified 200-Case Generative Comparison (`seed=7007`, [`NorviTech/arrmes-bench-v5`](https://huggingface.co/datasets/NorviTech/arrmes-bench-v5)):**
  - Evaluated on a stratified 200-case slice (`100` positive + `100` hard-negative/safety) with zero query or entity overlap against `TrainPool-v7` (`5,827` rows) and `acceptance-v4` (`1,000` rows):
    - **Released `Qwen3-0.6B` (`fp16 + LoRA`)**: Positive Track A `88/100` (`88.0%`), Track B **`91/100` (`91.0%`)**; Hard-Negative Track A `43/100`, Track B **`96/100` (`96.0%`)**, **`0` unauthorized writes**.
    - **Trained `Qwen3-0.6B v7` (`fp16 + LoRA`, `v7-lora/`)**: Positive Track A `76/100` (`76.0%`), Track B `77/100` (`77.0%`); Hard-Negative Track A `40/100`, Track B **`97/100` (`97.0%`)**, **`0` unauthorized writes**. (Post-training 4-bit quantization of `Qwen3-0.6B v7` degraded unseen entity copying to `53/100` raw / `62/100` verified; keep `Qwen3-0.6B` in `fp16 + LoRA` or use `ArrmesNeuralRouterV3`.)
- **Historical `sealed-acceptance-v3` (`N = 314`) Audit Disclosure:**
  - Exact-public replay of the root adapter (`8a9bcf2...`) with original public source (`a536483...`) measured **296/314 raw exact (`94.27%`)** and **310/314 verifier-processed exact (`98.73%`)**, with 4 raw unexpected write proposals and 1 surviving the original verifier among **226** (not 229) non-write opportunities (`311/314` and `0` unexpected proposals after the later descriptor-grounding repair).
  - The 4,389-row historical training file shares 37 normalized templates with 261/314 `sealed-v3` rows (`0` identical full query+context records, `34` recurring query strings across different contexts). Use [`NorviTech/arrmes-bench-v5`](https://huggingface.co/datasets/NorviTech/arrmes-bench-v5) for zero-leakage evaluation.

![Cross-Platform Runtime Latency and Memory Footprint](arrmes_v7_runtime_latency_memory_footprint.png)

## Quickstart (MLX on Apple Silicon)

`raw` is verifier-processed output; inspect `raw_unverified` separately when measuring raw model generation.

```python
from arrmes.model import LocalModel

model = LocalModel(
    "models/qwen3-06b",                       # 4-bit or fp16 Qwen3-0.6B base
    family="hermes",
    adapter="adapters/arrmes-qwen3-0.6b",     # root v6 baseline or v7-lora/
    contract_version="v2",
)

# 1. Read-only status proposal
res = model.infer("Check Sonarr for any missing episodes of Sintel without starting a search.")
print(res["raw_unverified"])  # Track A direct model output
print(res["raw"])             # Track B verified proposal
# <tool_call>{"name": "tv_missing", "arguments": {"title": "Sintel"}}</tool_call>
```

## Licence

Released under the Apache-2.0 licence (inheriting from `Qwen/Qwen3-0.6B`). Part of the [NorviTech Suite](https://norvitech.com) and [Arrmes `v1.4.0`](https://github.com/spencernorton/arrmes/releases/tag/v1.4.0).
