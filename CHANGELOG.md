# Changelog

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
