---
license: mit
language:
  - en
library_name: numpy
pipeline_tag: text-classification
tags:
  - function-calling
  - tool-use
  - router
  - self-hosted
  - media-server
  - agent-safety
  - arrmes
  - norvitech
---

# ArrmesNeuralRouterV3 (`arrmes-neural-router-v3`, `v1.4.0`)

**ArrmesNeuralRouterV3** is the default **7.52 MiB** (`7,883,511`-byte) learned neural intent router and typed entity/argument resolver for [Arrmes `v1.4.0`](https://github.com/spencernorton/arrmes/releases/tag/v1.4.0), a local assistant for operating a self-hosted media stack (Sonarr, Radarr, Lidarr, Bazarr, qBittorrent, Tautulli, Seerr, and Bitmagnet).

On the locked 2,460-case [`NorviTech/arrmes-bench-v5`](https://huggingface.co/datasets/NorviTech/arrmes-bench-v5) suite (`seed=7007`, zero query or entity overlap with training), **ArrmesNeuralRouterV3** achieves **`1,060 / 1,060` (`100.00%`)** raw Track A exact accuracy on `arrmes.tools.v2`, **`1,257 / 1,260` (`99.76%`)** on `arrmes.tools.v3` (both passing the two-sided 95% Wilson LCB `>= 99.0%` and 95% cluster-bootstrap LCB `>= 99.0%` gates before any downstream verifier repair), and **`1,200 / 1,200` (`100.00%`)** on the hard-negative/ambiguity/safety suite with **`0` unauthorized write proposals or executions** — while running in **`0.674 ms` warm p50** on Apple M4 Pro and **`0.944 ms` warm p50** on AMD Ryzen 9 7950X3D CPU.

![Arrmes v1.4.0 Locked Acceptance-v5 Accuracy and Confidence Bounds](arrmes_v7_acceptance_v5_accuracy_bounds.png)

## Supported Contracts (`v2` & `v3`)

- **`arrmes.tools.v2` (Frozen 20-Tool Contract):**
  - Schema SHA-256: `e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8`
  - Actions: `tv_status`, `tv_missing`, `tv_search_missing`, `movie_status`, `movie_search`, `music_status`, `music_search`, `now_playing`, `watch_history`, `request_status`, `request_media`, `seer_discover`, `armiger_parse`, `bitmagnet_search`, `waypoint_status`, `torrent_list`, `torrent_pause`, `torrent_resume`, `clarify` (`title`, `media_type`, `reference`, `scope`), and `unsupported` (`unsafe`, `complex`, `out_of_scope`).
- **`arrmes.tools.v3` (Versioned 23-Tool Contract):**
  - Schema SHA-256: `908eb9c52b5486e87df585978d4b7563961a5b942b87a83c39980cc1333eb204`
  - Preserves all 20 `v2` tools and adds:
    1. `service_query(service, operation, query?)`: routes all **31 native read service/operation pairs across 8 APIs** (`sonarr`, `radarr`, `lidarr`, `bazarr`, `qbittorrent`, `tautulli`, `seerr`, `bitmagnet`).
    2. `bazarr_subtitle_search(media_type, title, language, season?, episode?, year?, forced, hearing_impaired)`
    3. `bazarr_subtitle_download(media_type, title, language, candidate_id, season?, episode?, year?, forced, hearing_impaired)`

## Architecture & Artifact Lineage

- **Feature Representation (`input_dim = 16,416`):**
  - `16,384`-dimensional signed Blake2b hashed word, character, and clause n-gram projection with explicit **negation-scope tagging** on contrastive subordinate clauses (`rather than ...`, `instead of ...`, `without ...`, `do not ...`) and entity-stripped surface normalization (`<QUOTED_0>`, `<QUOTED_1>`).
  - Concatenated with `32` structural indicators (quoted span count, artist/album role markers, 24-hex candidate IDs, season/episode markers, lookalike-service traps, injection/shell/SQLi markers, and multi-action conjunctions).
- **Classifier Head & Typed Resolver:**
  - 2-layer ReLU MLP (`16,416 -> 128 -> 28` action/slot/reason classes) trained with AdamW and label smoothing (`seed = 20261102`, `28` epochs, `9.364 s` on Apple M4 Pro; cross-entropy loss `0.699036 -> 0.000058`) on `TrainPool-v7` (`5,827` examples with disjoint training-only entities).
  - Paired with `resolve_arguments_for_class` in [`src/arrmes/neural_router.py`](https://github.com/spencernorton/arrmes/blob/v1.4.0/src/arrmes/neural_router.py) for exact typed argument and entity extraction.
  - Includes a pure-stdlib `.npz` reader and sparse forward-pass fallback when `numpy` is not installed.
- **Weight Artifact (`router_weights.npz`):**
  - Size: **`7,883,511` bytes (`7.52 MiB`)**
  - SHA-256: `e640ead79ceee2929ccf63ec6f9d953d9cd8d85e76e011483bf336abe6235113`

![Track C Controlled End-to-End Workflow Execution Scorecard](arrmes_v7_track_c_workflow_scorecard.png)

## Locked `acceptance-v5` & Cross-Platform Runtime Results

- **Positive `v2` (`1,060` cases, `212` clusters):** Track A **`1,060 / 1,060` (`100.00%`)** (Wilson 95% LCB **`99.64%`**, cluster-bootstrap 95% LCB **`100.00%`**); Track B **`1,060 / 1,060` (`100.00%`)**, `0` damaged, `0` unauthorized writes.
- **Positive `v3` All (`1,260` cases, `252` clusters):** Track A **`1,257 / 1,260` (`99.76%`)** (Wilson 95% LCB **`99.30%`**, cluster-bootstrap 95% LCB **`99.44%`**); Track B **`1,257 / 1,260` (`99.76%`)**.
- **Hard-Negative / Ambiguity / Safety (`1,200` cases, `240` clusters across 35 families):** Track A **`1,200 / 1,200` (`100.00%`)** (Wilson 95% LCB **`99.68%`**, cluster-bootstrap 95% LCB **`100.00%`**); Track B **`1,200 / 1,200` (`100.00%`)**, **`0` unauthorized writes** (Clopper-Pearson 95% one-sided UB **`0.25%`** on negative suite, **`0.14%`** across all `2,170` non-write opportunities).
- **Frozen `acceptance-v4` (`1,000` cases):** Track A **`1,000 / 1,000` (`100.00%`)**, Track B **`1,000 / 1,000` (`100.00%`)**.
- **Track C Controlled Workflows:** **`52 / 52` (`100.00%`)** across `12/12` `SandboxCluster` reads, `6/6` confirmation-gated writes (replay blocked), `31/31` native `service_query` pairs, `2/2` Bazarr subtitle workflows, and `1/1` degraded-service `503` drill.

![Cross-Platform Runtime Latency and Memory Footprint](arrmes_v7_runtime_latency_memory_footprint.png)

- **Apple M4 Pro (`macOS 26.3 ARM64`):** Cold load **`16.71 ms`**, warm model-only **`p50 = 0.484 ms / p95 = 0.684 ms`**, warm routing + `verify_proposal` **`p50 = 0.674 ms / p95 = 0.882 ms`**, full authenticated HTTP `/v1/query` loopback **`p50 = 1.113 ms / p95 = 1.264 ms`**, peak process RSS **`80.70 MiB`**.
- **AMD Ryzen 9 7950X3D (`Linux 6.8 x86_64` CPU):** Cold load **`26.18 ms`**, warm model-only **`p50 = 0.726 ms / p95 = 0.979 ms`**, warm routing + `verify_proposal` **`p50 = 0.944 ms / p95 = 1.219 ms`**, peak process RSS **`63.50 MiB`**.

## Quickstart

```python
from arrmes.backends import create_model
from arrmes.protocol import decode, normalized

model = create_model(
    "models/arrmes-neural-router-v3",
    family="hermes",
    backend="neural-router",
    contract_version="v3",
)

out = model.infer("Start the search for all missing "Signal in the Reedbed" episodes rather than reporting status.")
print(normalized(decode(out["raw_unverified"], family="hermes", allow_v3=True)))
# {"name": "tv_search_missing", "arguments": {"title": "Signal in the Reedbed"}}

out_sq = model.infer("Check Sonarr health")
print(normalized(decode(out_sq["raw_unverified"], family="hermes", allow_v3=True)))
# {"name": "service_query", "arguments": {"service": "sonarr", "operation": "health"}}
```

## Licence

Released under the [MIT Licence](LICENSE) © Spencer Norton. Part of the [NorviTech Suite](https://norvitech.com) and [Arrmes `v1.4.0`](https://github.com/spencernorton/arrmes/releases/tag/v1.4.0).
