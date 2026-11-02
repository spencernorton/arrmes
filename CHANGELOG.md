# Changelog

## 1.1.0 — 2026-11-01

- Added `arrmes.tools.v2` 20-action contract across nine self-hosted service domains (`sonarr`, `radarr`, `lidarr`, `tautulli`, `qbittorrent`, `seer`, `requests`, `armiger`, `bitmagnet`, `dh-waypoint`) plus `clarify` and `unsupported`.
- Added entity-agnostic pre-dispatch verification (`verify_proposal`) and multi-turn `SessionEntityLedger` with 3-turn TTL and cross-principal isolation.
- Added `LiveGatewayAdapter`, `WriteConfirmationStore` (HMAC-SHA256 single-use write confirmation tokens), and an authenticated HTTP service (`GET /healthz`, `GET /v1/metrics`, `POST /v1/route`, `POST /v1/execute`, `POST /v1/confirm`). Callers authenticate with bearer tokens resolved from an operator-managed registry.
- Ships a 4-bit Qwen3 0.6B base with an fp16 rank-8 LoRA adapter (about 344 MB) and dual prefix KV caching; measured warm p50 routing latency is roughly 230–245 ms on an Apple M4 Pro.
