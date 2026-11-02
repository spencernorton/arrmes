---
license: mit
task_categories:
  - text-generation
  - text-classification
language:
  - en
tags:
  - benchmark
  - tool-calling
  - function-calling
  - media-server
  - agent-safety
  - arrmes
  - norvitech
size_categories:
  - 1K<n<10K
---

# Arrmes-Bench-v5 (`acceptance-v5`, `seed=7007`, `v1.4.0`)

**Arrmes-Bench-v5** is the locked, zero-leakage acceptance benchmark suite (`2,460` scenarios across `492` independent scenario clusters) for [Arrmes `v1.4.0`](https://github.com/spencernorton/arrmes/releases/tag/v1.4.0), evaluating local tool-calling models and routers across both `arrmes.tools.v2` (`20` tools) and `arrmes.tools.v3` (`23` tools).

![Arrmes v1.4.0 Locked Acceptance-v5 Accuracy and Confidence Bounds](arrmes_v7_acceptance_v5_accuracy_bounds.png)

## Suite Structure & Files

- **`cases-positive.jsonl` (`1,260` scenarios across `252` clusters, `5` cases/cluster, SHA-256 `67725d27b7fcff2113a3d525da79c9d347c9947b698bc2d45a730804f1cf2f06`):**
  - `1,060` `arrmes.tools.v2` scenarios (`212` clusters) covering all 18 concrete actions (`tv_status`, `tv_missing`, `tv_search_missing`, `movie_status`, `movie_search`, `music_status`, `music_search`, `now_playing`, `watch_history`, `torrent_list`, `torrent_pause`, `torrent_resume`, `request_status`, `request_media`, `seer_discover`, `armiger_parse`, `bitmagnet_search`, `waypoint_status`), including contrastive subordinate clauses (`rather than`, `instead of`, `without`, `do not`) and multi-entity Lidarr `(artist, album)` cards.
  - `200` `arrmes.tools.v3` scenarios (`40` clusters) covering all **31 native `service_query` read pairs across 8 APIs** (`sonarr`, `radarr`, `lidarr`, `bazarr`, `qbittorrent`, `tautulli`, `seerr`, `bitmagnet`) plus `bazarr_subtitle_search` and `bazarr_subtitle_download`.
- **`cases-negative.jsonl` (`1,200` scenarios across `240` clusters, `5` cases/cluster, SHA-256 `0d335c334a0819044ff4d3c53543a18d6574c0472e1102a525deaa222f46c4cf`):**
  - Spans 35 correlated negative/ambiguity/safety families (`AV5N-001` through `AV5N-035`) across `clarify:title`, `clarify:media_type`, `clarify:reference`, `clarify:scope`, `unsupported:unsafe`, `unsupported:complex`, and `unsupported:out_of_scope` (missing slots, unbound pronouns, broad artist-only writes, destructive commands, SSRF/SQLi/path traversal, prompt injections, compound write requests, and confusable lookalike service names such as `BitAgent` vs `Bitmagnet` and `Seerosen` vs `Seerr`).
- **`manifest.json` & `bootstrap_indices_seed7007.json`:**
  - Pre-evaluation leakage audit proving **`0` exact query overlaps** and **`0` entity overlaps** (`131` held-out entities) against `TrainPool-v7` (`5,827` examples) and `acceptance-v4` (`1,000` examples), plus deterministic `10,000`-resample cluster-bootstrap index pins (`seed=7007`).
- **`summary.json`:**
  - Complete Track A (raw model output), Track B (`verify_proposal` + confirmation boundary), and Track C (`52/52` controlled workflow execution) evaluation results.

![Track C Controlled End-to-End Workflow Execution Scorecard](arrmes_v7_track_c_workflow_scorecard.png)

## Headline Results (`summary.json`)

- **[`NorviTech/arrmes-neural-router-v3`](https://huggingface.co/NorviTech/arrmes-neural-router-v3) (`7.52 MiB`):**
  - Positive `v2` (`1,060` cases): Track A **`1,060 / 1,060` (`100.00%`)**, Wilson 95% LCB **`99.64%`**, cluster-bootstrap 95% LCB **`100.00%`**; Track B **`1,060 / 1,060` (`100.00%`)**.
  - Positive `v3` All (`1,260` cases): Track A **`1,257 / 1,260` (`99.76%`)**, Wilson 95% LCB **`99.30%`**, cluster-bootstrap 95% LCB **`99.44%`**; Track B **`1,257 / 1,260` (`99.76%`)**.
  - Hard-Negative / Safety (`1,200` cases): Track A **`1,200 / 1,200` (`100.00%`)**, Wilson 95% LCB **`99.68%`**, cluster-bootstrap 95% LCB **`100.00%`**; Track B **`1,200 / 1,200` (`100.00%`)**, **`0` unauthorized writes** (Clopper-Pearson 95% UB **`0.25%`**).
  - Track C Controlled Workflows: **`52 / 52` (`100.00%`)**.

## Licence

Released under the [MIT Licence](https://opensource.org/licenses/MIT) © Spencer Norton. Part of the [NorviTech Suite](https://norvitech.com) and [Arrmes `v1.4.0`](https://github.com/spencernorton/arrmes/releases/tag/v1.4.0).
