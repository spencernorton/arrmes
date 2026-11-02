---
license: apache-2.0
task_categories:
  - text-generation
language:
  - en
tags:
  - benchmark
  - tool-calling
  - function-calling
  - media-server
  - arrmes
---

# Arrmes Benchmark Suite v5 (`arrmes-bench-v5`)

Locked, zero-leakage evaluation benchmark for the Arrmes local media assistant.

## Suite Structure

- **Positive Suite (`cases-positive.jsonl`)**: `1,260` supported scenarios across `252` independent clusters (`5` cases/cluster), comprising `1,060` `arrmes.tools.v2` scenarios across all 18 concrete actions and `200` `arrmes.tools.v3` scenarios covering all 31 native `service_query` read pairs plus `bazarr_subtitle_search` and `bazarr_subtitle_download`.
- **Hard-Negative, Ambiguity, & Safety Suite (`cases-negative.jsonl`)**: `1,200` scenarios across `240` independent clusters (`5` cases/cluster) spanning all 35 negative taxonomy families (`AV5N-001` through `AV5N-035`), including missing slots, unbound references, unsupported write scopes, destructive commands, credential extraction, shell/SSH requests, prompt injections, compound multi-action requests, and confusable service names (`Sightseer`, `Seeress`, `BitAgent`, `Bitwarden`, `BitTorrent`).
- **Pre-Evaluation Leakage Audit**: Verified `0` exact query overlaps and `0` entity overlaps (`130` unique `acceptance-v5` entities) against `TrainPool-v7` (`5,333` rows), `acceptance-v4` (`1,000` rows), and `overnight-v1` (`80` rows).
