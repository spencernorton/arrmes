---
license: apache-2.0
language:
  - en
library_name: numpy
tags:
  - tool-calling
  - function-calling
  - media-server
  - arrmes
---

# ArrmesNeuralRouterV3 (`arrmes-neural-router-v3`)

Compact multi-head neural intent router (`router_weights.npz`, 7.89 MB compressed) paired with a deterministic typed span and argument resolver for the Arrmes local media assistant.

## Supported Contracts

- **`arrmes.tools.v2` (Frozen 20-Tool Contract)**:
  - Schema SHA-256: `e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8`
  - Covers `tv_status`, `tv_missing`, `tv_search_missing`, `movie_status`, `movie_search`, `music_status`, `music_search`, `now_playing`, `watch_history`, `request_status`, `request_media`, `seer_discover`, `armiger_parse`, `bitmagnet_search`, `waypoint_status`, `torrent_list`, `torrent_pause`, `torrent_resume`, `clarify`, and `unsupported`.
- **`arrmes.tools.v3` (Versioned 23-Tool Contract)**:
  - Preserves all 20 `v2` tools unchanged and adds `service_query` (31 native read service/operation pairs across Sonarr, Radarr, Lidarr, Bazarr, qBittorrent, Tautulli, Seerr, and Bitmagnet), `bazarr_subtitle_search`, and `bazarr_subtitle_download`.

## Architecture & Runtime Profile

- **Input Representation**: 16,384-dimensional signed Blake2b hashed word/character n-gram projection (with negation-scope tagging on contrast clauses and entity-stripped surface normalization) concatenated with 32 structural indicators (`input_dim = 16,416`).
- **Classifier Head**: 2-layer ReLU multilayer perceptron (`16,416 -> 128 -> 28` classes) trained with AdamW and label smoothing (`seed = 20261102`) on `TrainPool-v7` (`5,333` examples with disjoint training-only entities).
- **Footprint & Latency**:
  - Artifact size on disk: `7.52 MiB` (`7,888,929` bytes)
  - Warm CPU routing latency (Apple M4 Pro): `p50 = 0.66 ms`, `p95 = 1.06 ms`
  - Resident process memory: `~85 MiB` RSS
