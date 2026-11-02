<h1 align="center">Arrmes</h1>

<p align="center">
  <strong>A bounded local tool-calling router and execution verifier for self-hosted media and download stacks.</strong><br>
  Native media-service workflows, optional MLX or portable GGUF inference, and single-use confirmation for consequential writes.
</p>

<p align="center">
  <a href="https://norvitech.com"><img alt="NorviTech Suite" src="https://img.shields.io/badge/NorviTech-Suite-FD8024.svg"></a>
  <a href="https://github.com/spencernorton/arrmes/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/spencernorton/arrmes/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/spencernorton/arrmes/tags"><img alt="Latest release" src="https://img.shields.io/github/v/tag/spencernorton/arrmes?label=release&sort=semver"></a>
  <a href="#install"><img alt="Build from source" src="https://img.shields.io/badge/install-from%20source-2D2D2D.svg"></a>
  <a href="LICENSE"><img alt="Licence MIT" src="https://img.shields.io/badge/licence-MIT-blue.svg"></a>
  <a href="https://buy.stripe.com/8x26oH2U44f65TRe574wM04"><img alt="Donate" src="https://img.shields.io/badge/donate-Stripe-635bff.svg?logo=stripe&logoColor=white"></a>
</p>

Arrmes turns media-stack requests into validated tool calls. A small local model handles the released media intents; a separate deterministic route adds native service reads and explicit confirmed workflows. Trusted code owns service credentials, entity resolution, permissions, confirmation and execution.

## What it does

- **Native read workflows:** 31 service/operation pairs across Sonarr, Radarr, Lidarr, Bazarr, qBittorrent, Tautulli, Seerr and Bitmagnet. Inspect libraries, missing media or subtitles, metadata search, queues, history, activity and health where each service supports them. [Capabilities and exact limits](docs/capabilities.md).
- **Direct execution of existing media intents:** the trained `tv_status`, `tv_missing`, `movie_status`, `music_status`, `now_playing` and `watch_history` actions now use native service adapters when configured. They do not require the legacy gateway.
- **Native torrent discovery:** search Bitmagnet's indexed metadata through its native GraphQL API, inspect health and use the existing `bitmagnet_search` action without the BitAgent facade. [Setup and read-only limits](docs/native-bitmagnet.md).
- **Confirmed transfer control:** list torrents and pause/resume one exact, uniquely resolved transfer through native qBittorrent 4.1+ or 5.x APIs. Confirmation binds the caller, arguments, torrent hash, service version and expiry. Mutations are never automatically retried.
- **Confirmed native media searches:** search missing Sonarr episodes, one Radarr movie or a Lidarr album/bounded missing-album set using fixed IDs. Accepted indexer jobs remain dispatched; they do not prove downloaded files. [Exact scope and service versions](docs/native-arr-writes.md).
- **Subtitle workflow:** separately confirm a Bazarr provider search, inspect server-issued candidates, then confirm one exact movie/episode subtitle download. Language and subtitle flags are explicit. [Setup and completion limits](docs/bazarr-subtitles.md).
- **Caller-owned movie requests:** map an authenticated Arrmes subject to its Seerr account, inspect that caller's request status and confirm one movie submission. TV/4K creation and approval/cancellation remain gaps. [Identity and request policy](docs/native-requests.md).
- **Bounded independent reads:** “Show Sonarr queue and Radarr history” validates every clause and scope before execution. An unavailable service produces a partial result while other independent reads continue. Compound writes remain unsupported.
- **A preserved neural contract:** `arrmes.tools.v2` has 18 service actions plus `clarify` and `unsupported`. New native reads use `arrmes.tools.v3` and report `routing_source=explicit_service_deterministic`; they are not newly trained v6 model capabilities.
- **Portable inference:** optional native llama.cpp/GGUF on CPU, with explicit accelerator selection; optional MLX on Apple Silicon. CPU inference installs neither CUDA nor MLX. [Runtime setup, conversion and tested platforms](docs/portable-runtime.md).

The separately licensed [experimental Q4 GGUF](https://github.com/spencernorton/arrmes/releases/tag/v1.2.0) is available with its exact checksums and notices. Windows CPU generation now passes a [hosted CI operational test](docs/windows-cpu-qualification.md); Windows NVIDIA remains untested. The Windows test uses pinned v1.2.0 product source and does not qualify every newer workflow or model accuracy.

Every dispatch validates the tool, arguments, entity and caller scopes. The HTTP service requires bearer authentication and explicit confirmation for consequential writes. Execution references come from a verified per-caller ledger. Credentials are kept out of model prompts, image layers and audit responses.

## Measurements and scope

The original 230–245 ms warm p50 claim timed generation only. Prompt preparation, verification, HTTP handling and service execution must be measured separately. The original roughly 344 MB of model/adapter files is not a runtime RAM measurement; the published adapter tensors are float32. The current public adapter also differs from a retained research checkpoint with the same v6 label, so their results are recorded separately.

Comparisons retain raw predictions, translated-format scores, verifier decisions and fixture execution separately. The [new 1,000-input diagnostic](docs/benchmark-acceptance-v4.md) gets 770 raw exact calls versus 396 for the untuned base and 782 for a stronger 4B reference. Frozen improved verification regresses from 717 to 678 exact calls while reducing unexpected write proposals from ten to two. Later boundary repairs are development regressions, with no new independent acceptance claim. The [earlier 80-case comparison](docs/benchmark-comparison.md) remains a separate experiment. Historical sealed-suite results do not establish performance on new requests.

A separate [public FunctionGemma adapter diagnostic](docs/functiongemma-public-diagnostic.md) now measures the fine-tune previously omitted from the base-only comparison: 63/80 raw exact versus 12/80 for its identical BF16 base. This is a known-input Apple Silicon MLX result; FunctionGemma GGUF and distribution-file corrections remain open.

The native adapters are validated against loopback HTTP fixtures. Separate actual Sonarr 4.0.20.3014 and Seerr 3.5.1 instances pass selected empty-library reads and identity checks. Broader upstream versions, seeded entities, large libraries and actual service writes still need qualification. Advanced Bitmagnet/transfer management, subtitle upload/sync, broader request lifecycle and genuine Armiger/DH-Waypoint adapters remain gaps. The old gateway Armiger and Waypoint substitutions now return unsupported. New [workflow diagnostics](docs/native-workflow-evaluation.md) retain raw model failures separately from verifier recovery and explicit routing.

## Install

Use Python 3.12 or newer and an isolated environment:

```sh
git clone https://github.com/spencernorton/arrmes.git
cd arrmes
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
```

Install a pinned native llama.cpp runtime separately and provide a fused GGUF for CPU inference. The [runtime guide](docs/portable-runtime.md) includes Linux and native Windows instructions, optional NVIDIA configuration, reproducible CPU-only export and Linux container packaging. Docker images are built locally from this source; this repository does not claim a pre-published Arrmes container image.

For Apple Silicon MLX, install `.[mlx]` and provide the pinned base and adapter directories. Keep the original release artifacts intact when exporting GGUF variants.

## Configuration

- `ARRMES_CLIENTS_FILE` — required bearer-digest registry. The registry alone assigns subjects and scopes; request bodies cannot override identity.
- `ARRMES_SERVICES_FILE` — operator file containing native API roots and credentials. Native-only installations do not require a gateway.
- `ARRMES_GATEWAY_URL` — optional legacy gateway. Existing gateway operations remain a separate integration with their documented restrictions.
- `ARRMES_BACKEND` — `auto`, `mlx` or `llama-server`.
- `ARRMES_BACKEND=native-only` — an optional model-free mode, read-only by default. It loads no weights or inference runtime and clarifies requests outside the explicit grammar.
- `ARRMES_NATIVE_CONFIRMED_WRITES=1` — deliberate native-only opt-in for documented Bazarr/Seerr phrases, with current scopes and single-use confirmation still required.
- `ARRMES_MODEL_PATH` — MLX directory or fused GGUF file; `ARRMES_ADAPTER_PATH` applies to MLX. An unfused adapter is rejected on the GGUF path.
- `ARRMES_INFERENCE_URL` — optional explicitly configured native llama-server origin; otherwise the selected GGUF starts a managed local process.
- `ARRMES_DEVICE` — portable backend device selection; `cpu` is the default. See the runtime guide for supported accelerator names and platform qualification.
- `ARRMES_WRITE_MODE` — `confirm` (default), `shadow` or `disabled`. The HTTP entry point refuses direct mutations.
- `ARRMES_BIND_HOST` / `ARRMES_PORT` — default `127.0.0.1:8095`.
- `ARRMES_REQUEST_OWNER_SUBJECT` — the legacy gateway credential's request-owner subject. Other callers cannot submit gateway media requests.

After configuring a model, client registry and service registry:

```sh
.venv/bin/python -m arrmes.server
```

For explicit native reads alone, set `ARRMES_BACKEND=native-only` with the two registries and run the same command. No model path is needed. This mode supports requests such as “Show Sonarr queue”; it does not offer neural entity inference or transfer writes. Restart the service after changing either registry.

## Where data lives

Models and adapters stay in operator-selected local directories. Entity ledgers and pending confirmations are ephemeral and clear on process restart. Inference receives the query, selected media context and tool descriptions. Service credentials remain in trusted adapters; service calls go to configured API roots.

## Documentation and support

- [Capabilities](docs/capabilities.md), [runtime guide](docs/portable-runtime.md), [comparison report](docs/benchmark-comparison.md) and [implementation decisions](docs/implementation-decisions-2026-11-01.md).
- [Changelog](CHANGELOG.md), [security policy](SECURITY.md), [support](SUPPORT.md) and [contributing](CONTRIBUTING.md).
- Questions belong in [Discussions](https://github.com/spencernorton/arrmes/discussions); reproducible defects belong in [Issues](https://github.com/spencernorton/arrmes/issues).
- Report security issues through [private vulnerability reporting](https://github.com/spencernorton/arrmes/security/advisories/new).
- Support maintenance through [Stripe](https://buy.stripe.com/8x26oH2U44f65TRe574wM04).

## Development

```sh
.venv/bin/python -m pytest -q
```

Core tests use no weights, CUDA or MLX. Export-specific tensor tests run when the optional conversion environment is installed. Benchmark commands and provenance are documented in the comparison report.

## Licence

[MIT](LICENSE) © Spencer Norton. Model licenses and redistribution notices are separate; preserve the pinned model's license and attribution during conversion.

---

<p align="center">
  <a href="https://norvitech.com"><img alt="Part of the NorviTech Suite — open-source apps for the Linux desktop and the self-hosted stack" src="https://norvitech.com/assets/banner.svg" width="640"></a>
</p>

<p align="center">
  <a href="https://github.com/spencernorton/helios">Helios</a> ·
  <a href="https://github.com/spencernorton/DH-Waypoint">DH-Waypoint</a> ·
  <a href="https://github.com/spencernorton/armiger">Armiger</a> ·
  <a href="https://github.com/spencernorton/xnote">XNote</a> ·
  <a href="https://github.com/spencernorton/xnote-placement">XNote Placement</a> ·
  <a href="https://github.com/spencernorton/snipsnap">SnipSnap</a> ·
  <a href="https://github.com/spencernorton/conductor">Conductor</a> ·
  <a href="https://github.com/spencernorton/norvi-os">NorviOS</a> ·
  <a href="https://github.com/spencernorton/indigo">Indigo</a> ·
  <a href="https://github.com/spencernorton/roadtrack">Road Track</a> ·
  <a href="https://norvitech.com">norvitech.com</a>
</p>
