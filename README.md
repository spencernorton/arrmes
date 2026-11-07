# Arrmes

Local control for your self-hosted media stack.

[![CI](https://github.com/spencernorton/arrmes/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/spencernorton/arrmes/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/spencernorton/arrmes?label=release)](https://github.com/spencernorton/arrmes/releases)
[![MIT licence](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Arrmes on NorviTech](https://img.shields.io/badge/NorviTech-Suite-FD8024.svg)](https://norvitech.com/arrmes/)
[![Hugging Face models](https://img.shields.io/badge/Hugging_Face-models-FFD21E.svg)](https://huggingface.co/NorviTech/arrmes-qwen3-0.6b)

Arrmes turns plain-language requests into validated API calls across Sonarr, Radarr,
Lidarr, Prowlarr, Readarr/Chaptarr, Whisparr, Bazarr, qBittorrent, Tautulli, Seerr,
and Bitmagnet. Routing runs locally; service credentials stay in the application.

## What it does

- Check libraries, missing or cutoff-unmet media, quality profiles, indexers, upcoming calendar airings, queues, watch history, and current activity.
- Search Prowlarr indexers and Bitmagnet's torrent index (with optional content-type filtering).
- Pause or resume a specific qBittorrent transfer.
- Search for missing episodes, movies, albums, books, or subtitles, including bounded read-to-confirm workflows.
- Submit movie or TV series/season requests through Seerr, check caller-isolated request status, and cancel pending requests.

For example:

```text
Show Sonarr queue
Show Radarr history
What episodes of Severance are missing in Sonarr?
Search Prowlarr for Ubuntu
Check Readarr missing books
```

Writes require explicit, single-use HMAC confirmation. Ambiguous targets prompt for
clarification. Supported operations vary by service; see the
[capability guide](docs/capabilities.md) for versions and limits.

## Install

Requires Python 3.12+. On macOS or Linux:

```sh
git clone https://github.com/spencernorton/arrmes.git
cd arrmes
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Follow the [HTTP setup](docs/portable-runtime.md#authenticated-http-service) to
create private client and service registries, start the server, and make a first
request. It starts in model-free mode with read-only permissions.

For learned routing, use the [7.52 MiB neural router](docs/portable-runtime.md#neural-router).
Optional [GGUF](docs/portable-runtime.md#cpu-installation) and
[MLX](docs/portable-runtime.md#routing-backends) backends are also available.
See the runtime guide for [Windows](docs/portable-runtime.md#native-windows)
and [Docker](docs/portable-runtime.md#linux-containers) setup.

## Benchmarks

Recorded neural-router, unassisted generative MLX, and system results on the template-skeleton-disjoint `acceptance-v7` (`N=2,580`) and historical suites:

- **Locked `acceptance-v7` positive calls (`v3` contract, 276 clusters, 0 template-skeleton overlap):** `1,380 / 1,380` (`100.00%` Track A raw and Track B verified; 95% Wilson LB `99.72%`, 95% cluster-bootstrap LB `100.00%`).
- **Locked `acceptance-v7` positive calls (`v2` contract, 212 clusters):** `1,060 / 1,060` (`100.00%` Track A raw and Track B verified; 95% Wilson LB `99.64%`, 95% cluster-bootstrap LB `100.00%`).
- **Locked `acceptance-v7` hard-negative & safety cases (240 clusters):** `1,200 / 1,200` (`100.00%` Track A raw and Track B verified; `0` damaged raw predictions, `0` unauthorized writes).
- **Unassisted `Qwen3-0.6B v9` mixed-precision QLoRA (`LocalModel`, `mlx-lm`):** `138 / 150` (`92.0%` Track A raw) and `147 / 150` (`98.0%` Track B verified) on `acceptance-v7` `v2` positives; `67 / 150` (`44.7%` Track A raw) and `141 / 150` (`94.0%` Track B verified) on hard negatives; `64 / 80` (`80.0%` raw) and `72 / 80` (`90.0%` verified) on `overnight-v1`.
- **Track C end-to-end execution workflows (HTTP fixtures):** `93 / 93` (`100.00%` across `v2` reads/writes, all `68` native `service_query` pairs, Readarr book search, Seerr TV season request/cancellation, and bounded read-to-confirm workflows).
- **11-service live upstream Docker container binary qualification (`experiments/upstream-lab-v1/summary.json`):** `73 / 73` (`100.00%` live checks across official Sonarr v4, Radarr v5/v6 with 320 seeded movies + confirmed `MoviesSearch`, Lidarr v2, Readarr 0.4.19 with seeded author/book + confirmed `BookSearch`, Whisparr 3.6.5 v3 `eros`, Prowlarr v1, Bazarr 1.6+, Tautulli v2, qBittorrent v5, Bitmagnet v0.10.1 + PostgreSQL 16 GraphQL search, and Seerr 3.5.1 with caller-owned status and confirmed request cancellation).

![Model accuracy across template-disjoint acceptance-v7 and unassisted MLX QLoRA v9](docs/figures/arrmes-model-accuracy-comparison.png)

[Recorded `acceptance-v7` scores](experiments/acceptance-v7/summary.json) and [MLX LoRA v9 provenance](experiments/mlx-lora-v9-provenance.json) report Track A direct unmodified model output separately from Track B verified system proposals and explicit deterministic routes.

![Warm routing latency, 5,000-movie CatalogInvertedIndex lookup throughput, and model footprint](docs/figures/arrmes-local-runtime-comparison.png)

[Recorded runtime profiles](experiments/runtime-profile-v9.json): `0.678 ms` warm p50 / `0.998 ms` warm p95 routing + verification on Apple M4 Pro (`7.50 MiB` weights), and `0.96 us` p50 title lookup across a 5,000-movie library via `CatalogInvertedIndex` (`28,200x` faster than linear scan).

## Documentation

- [Supported services and configuration](docs/capabilities.md)
- [Runtime setup and measured performance](docs/portable-runtime.md)
- [Subtitle operations](docs/bazarr-subtitles.md), [movie & TV requests](docs/native-requests.md), [media searches](docs/native-arr-writes.md), and [Bitmagnet](docs/native-bitmagnet.md)
- [Contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md), and [security](SECURITY.md)

Questions: [Discussions](https://github.com/spencernorton/arrmes/discussions).
Bugs: [Issues](https://github.com/spencernorton/arrmes/issues).
Security issues: [private reporting](https://github.com/spencernorton/arrmes/security/advisories/new).

## Licence

[MIT](LICENSE) © Spencer Norton. Model weights have separate licences and notices.

[Support development](https://buy.stripe.com/8x26oH2U44f65TRe574wM04)

---

<p align="center">
  <a href="https://norvitech.com"><img alt="Part of the NorviTech Suite" src="https://norvitech.com/assets/banner.svg" width="640"></a>
</p>

<p align="center">
  <a href="https://github.com/spencernorton/helios">Helios</a> ·
  <a href="https://github.com/spencernorton/DH-Waypoint">DH-Waypoint</a> ·
  <a href="https://github.com/spencernorton/armiger">Armiger</a> ·
  <a href="https://github.com/spencernorton/arrmes">Arrmes</a> ·
  <a href="https://github.com/spencernorton/endexis">Endexis</a> ·
  <a href="https://github.com/spencernorton/xnote">XNote</a> ·
  <a href="https://github.com/spencernorton/xnote-placement">XNote Placement</a> ·
  <a href="https://github.com/spencernorton/snipsnap">SnipSnap</a> ·
  <a href="https://github.com/spencernorton/conductor">Conductor</a> ·
  <a href="https://github.com/spencernorton/norvi-os">NorviOS</a> ·
  <a href="https://github.com/spencernorton/indigo">Indigo</a> ·
  <a href="https://github.com/spencernorton/roadtrack">Road Track</a> ·
  <a href="https://norvitech.com">norvitech.com</a>
</p>
