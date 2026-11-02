# Changelog

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
