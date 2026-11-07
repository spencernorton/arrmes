# Changelog

## [1.5.0] - 2026-11-06

### Added
- Native `*Arr` and companion service expansion (`src/arrmes/services/contract.py`, `src/arrmes/services/native.py`, `src/arrmes/services/arr_writes.py`, `src/arrmes/catalog.py`) adding `prowlarr` (`api/v1`: `health`, `indexers`, `search`, `applications`, `history`), `readarr` / Chaptarr (`api/v1`), and `whisparr` (`api/v3`), plus richer read operations across `*Arr` services (`wanted` cutoff-unmet, `calendar` upcoming airings, `quality_profiles`, `indexers`, and boolean `monitored` filtering) for 68 validated `(service, operation)` pairs while preserving the immutable `arrmes.tools.v2` hash (`e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8`) and 23-tool `arrmes.tools.v3` surface.
- Caller-bound Seerr TV series/season request creation (`execute_tv_request`) and pending request cancellation (`execute_cancel_request`) in `src/arrmes/services/requests.py` with per-principal isolation and account-incarnation checks.
- Bounded two-step read-to-confirm workflows (`src/arrmes/workflows.py`: `build_read_to_confirm_steps`, `execute_read_to_confirm_workflow`), confirmed Readarr book/author indexer search (`execute_book_search`, `confirm_book_search`), and Bitmagnet `content_type` facet filtering (`src/arrmes/services/bitmagnet.py`) plus portable tool filtering (`portable_tools_v2`, `portable_tools_v3`).
- `CatalogInvertedIndex` (`src/arrmes/planner.py`, `src/arrmes/live_adapter.py`) providing O(1) normalized title, `originalTitle`, and `alternateTitles` lookup (`1.17 us` p50 across 5,000 Radarr movies; `22,495.7x` faster than linear scan).
- Unquoted natural-language span extraction with confidence and margin gating (`extract_unquoted_service_span` in `src/arrmes/services/routing.py`), updated `ArrmesNeuralRouterV3` (`models/arrmes-neural-router-v3`), and mixed-precision QLoRA + router-assisted constrained decoding for `Qwen3-0.6B v8` and `FunctionGemma-270M v8` (`src/arrmes/model.py`, `experiments/mlx-lora-v8-provenance.json`).
- Locked 2,580-case `acceptance-v6` benchmark suite (`data/acceptance-v6/`, `experiments/acceptance-v6/summary.json`, `seed=8008`, 516 clusters, 0 query and 0 entity overlap) and reproducible upstream Docker container binary qualification (`scripts/qualify_upstream_lab.py`, `experiments/upstream-lab-v1/summary.json`).

### Fixed
- Eliminated the 3 residual positive misses on `acceptance-v5` (now retired to `data/dev-v6/` at `1,260/1,260` = `100.00%`) and recovered 4-bit post-training quantization span-copying accuracy on `Qwen3-0.6B` from `53/100` (`v7` 4-bit) to `82/100` unassisted mixed-precision QLoRA and `100/100` router-assisted constrained decoding.
- Hardened upstream container compatibility (`src/arrmes/services/native.py`, `src/arrmes/services/transfers.py`) for qBittorrent v5.1.4 `QBT_SID_<port>` base64 session cookies and Tautulli v2.18.2 empty `get_activity` payloads.

## [1.4.4] - 2026-11-05

### Fixed
- Percent-encode native Seerr search queries (`src/arrmes/services/requests.py`, `src/arrmes/services/native.py`). `urlencode` turned spaces into `+`, which Seerr 3.5.1 rejects with HTTP 400 (`Parameter 'query' must be url encoded`), so movie requests, request status reads and `service_query` Seerr searches failed for any title containing a space.
- Accept Bazarr's numeric process start time (`src/arrmes/services/bazarr_subtitles.py`). Bazarr 1.6.2 reports `start_time` as `time.time()` epoch seconds, which the adapter rejected as `invalid_bazarr_process_identity`, so every subtitle search failed against a real Bazarr. Text values are still accepted; missing, boolean, non-positive and non-finite values are refused (an overflowing number such as `1e400` parses to `inf`).

## [1.4.3] - 2026-11-03

### Changed
- Title resolution folds diacritics and uses Sonarr/Radarr `alternateTitles` to disambiguate, returning a candidate list when a title matches more than one library entry.
- Unquoted entity extraction and span snapping keep Unicode letters and parenthesized region qualifiers such as `(US)` and `(UK)`.
- Entity-scoped missing-episode queries and indexer search triggers route through the v2 media and write actions instead of the global v3 `service_query` route.

## [1.4.2] - 2026-11-02

### Added
- Multi-suite open-model accuracy comparison charts (`docs/figures/arrmes_v7_open_model_accuracy_comparison.{png,pdf}` and updated `arrmes_v7_acceptance_v5_accuracy_bounds.{png,pdf}`) comparing Arrmes v1.4 (`ArrmesNeuralRouterV3`, `Arrmes Qwen3-0.6B`, `Arrmes FunctionGemma-270M`) against `Qwen3-4B Instruct 2507`, `Qwen3-1.7B`, `IBM Granite 4.0 350M BF16/Q4`, untuned base models, and deterministic rules across `overnight-v1` (`N=80`), `acceptance-v4` (`N=1,000`), and locked `acceptance-v5` (`N=2,460`).
- With vs. Without Arrmes efficiency and footprint comparison charts (`docs/figures/arrmes_v7_claude_sonnet_and_resource_comparison.{png,pdf}` and updated `arrmes_v7_runtime_latency_memory_footprint.{png,pdf}`) comparing Claude Sonnet 4.6 task completion time, token usage, and local RAM / GPU VRAM / weight footprint across Arrmes backends and open models.

## [1.4.0] - 2026-11-02

### Added
- Versioned `arrmes.tools.v3` contract (`src/arrmes/catalog.py`, `src/arrmes/protocol.py`) unifying the 20 `v2` tools with `service_query` (31 native service/operation pairs across 8 APIs), `bazarr_subtitle_search`, and `bazarr_subtitle_download` while preserving exact `arrmes.tools.v2` hash compatibility.
- Learned compact neural router `ArrmesNeuralRouterV3` (`src/arrmes/neural_router.py`, `models/arrmes-neural-router-v3`, `7.52 MiB` CPU NumPy artifact, `<1 ms` warm CPU routing latency on macOS ARM64 and Linux x86_64) selectable via `ARRMES_BACKEND=neural-router` or `ARRMES_BACKEND=auto`.
- Locked 2,460-case `acceptance-v5` benchmark suite (`data/acceptance-v5/`, `seed=7007`, 492 independent clusters, automated zero-leakage gate) and `docs/benchmark-acceptance-v5.md` with Track A, Track B, and Track C execution scorecards.
- Cross-platform runtime and 4-phase memory profiler (`scripts/profile_runtime_v7.py`, `experiments/runtime-profile-v7.json`) and Hugging Face model/dataset cards (`docs/huggingface-cards/`).

### Fixed
- Repaired general entity-agnostic verifier grounding in `src/arrmes/planner.py` (parenthesized year extraction, causative imperative write preservation, `<QUOTED>` non-word regex boundary handling, contrast-clause negation scoping, and compound vs unsafe ordering), improving frozen `acceptance-v4` verified accuracy on Released Arrmes from `678/1000` (`e5e76b8`) / `795/1000` (`v1.3.1`) to `981/1000` with `0` damaged raw predictions and `0` unauthorized writes.
- Accelerated `LocalModel` MLX inference (`src/arrmes/model.py`) with in-process `mlx_lm.generate`, reducing per-query MLX latency from ~`3.2 s` subprocess spawn to `43.5 ms` (`FunctionGemma-270M`) and `114.4 ms` (`Qwen3-0.6B`).

## 1.3.1 — 2026-11-02

- Publish practical download/checksum/license instructions for the separately distributed experimental GGUF.
- Retain actual hosted Windows CPU generation and fixture evidence, with exact run/source/artifact binding, original receipt reconstruction and offline verification.
- Keep Windows CUDA, physical Windows hardware, RAM profiling and broad model accuracy explicitly unqualified.
- Evaluate the exact public FunctionGemma v5 adapter against its identical BF16 base on the existing 80 inputs; preserve raw predictions, historical provenance gaps, license-file gaps and unsupported portable-export status separately.

## 1.3.0 — 2026-11-02

- Dispatch existing Sonarr/Radarr/Lidarr search actions through confirmed exact-ID native commands, with search-job evidence separate from downloaded files.
- Add separately confirmed Bazarr provider search and exact candidate download for movies/episodes, including strict inventory identity checks and honest asynchronous dispatch.
- Add caller-owned native Seerr movie submission/status using trusted user mappings and account-incarnation binding; TV creation and lifecycle changes remain unsupported.
- Add a deliberate model-free confirmed-write opt-in for explicit Bazarr/Seerr phrases; preserve read-only defaults and unchanged neural v2 tools.
- Reject unsupported narrow/quality/conditional write scopes, incompatible context roles, typographic negation and ambiguous quote-slot reuse.
- Preserve failed actual-model integration outcomes and separately labelled cached verifier regressions; no new model training or acceptance claim.
- Preserve five predeclared 1,000-input direct-model comparisons, including the verifier regression; add reproducible source snapshots and independent post-diagnostic scope regressions without rewriting raw results.
- Add direct native Bitmagnet metadata search and health with bounded GraphQL read queries, estimated-count handling and scoped explicit natural-language routing.
- Execute the existing trained `bitmagnet_search` action through the native adapter when configured; preserve released v2 schemas, weights and benchmark inputs.

## 1.2.1 — 2026-11-02

- Add standard-library GGUF inspection and an artifact-bound CPU generation/fixture smoke helper.
- Add a manually dispatched Windows CPU qualification workflow with pinned runtime, model hash and v1.2.0 product source; actual Windows generation remains pending execution.
- Retain exact-artifact Mac CPU operational evidence separately from model accuracy benchmarks.

## 1.2.0 — 2026-11-01

- Add 29 native read workflows, including Bazarr subtitle status and explicit Sonarr/Radarr/Lidarr queue, library, missing, metadata and health queries.
- Execute six existing trained media read actions through native APIs and offer an optional model-free explicit read mode.
- Add native qBittorrent listing and confirmation-gated single-transfer pause/resume with API-version handling and verified postconditions.
- Add optional llama.cpp/GGUF inference, CPU-only adapter export and Linux container packaging; retain optional MLX.
- Bind confirmations to resolved service identities, enforce exact expiry and replay protection, and keep execution context server-owned.
- Remove false live Armiger/Waypoint success substitutions and distinguish Seerr from request-portal discovery.
- Add reproducible raw-model, system, HTTP and controlled-workflow comparisons. Correct generation-only latency and model provenance claims.

## 1.1.0 — 2026-11-01

The historical release prose below overstated some qualifications. The current
audit identifies ten named domains, float32 adapter tensors, generation-only
timing and a different exact-public evaluation result; see the 1.2.0 report.

- Added `arrmes.tools.v2` 20-action contract across nine self-hosted service domains (`sonarr`, `radarr`, `lidarr`, `tautulli`, `qbittorrent`, `seer`, `requests`, `armiger`, `bitmagnet`, `dh-waypoint`) plus `clarify` and `unsupported`.
- Added entity-agnostic pre-dispatch verification (`verify_proposal`) and multi-turn `SessionEntityLedger` with 3-turn TTL and cross-principal isolation.
- Added `LiveGatewayAdapter`, `WriteConfirmationStore` (HMAC-SHA256 single-use write confirmation tokens), and an authenticated HTTP service (`GET /healthz`, `GET /v1/metrics`, `POST /v1/route`, `POST /v1/execute`, `POST /v1/confirm`). Callers authenticate with bearer tokens resolved from an operator-managed registry.
- Ships a 4-bit Qwen3 0.6B base with an fp16 rank-8 LoRA adapter (about 344 MB) and dual prefix KV caching; measured warm p50 routing latency is roughly 230–245 ms on an Apple M4 Pro.
