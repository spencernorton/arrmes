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

Arrmes turns media-stack requests into validated tool calls. A small local model handles the released media intents; a separate deterministic route adds native service reads. Trusted code owns service credentials, entity resolution, permissions, confirmation and execution.

## What it does

- **Native read workflows:** 29 service/operation pairs across Sonarr, Radarr, Lidarr, Bazarr, qBittorrent, Tautulli and Seerr. Inspect libraries, missing media or subtitles, metadata search, queues, history, activity and health where each service supports them. [Capabilities and exact limits](docs/capabilities.md).
- **Direct execution of existing media intents:** the trained `tv_status`, `tv_missing`, `movie_status`, `music_status`, `now_playing` and `watch_history` actions now use native service adapters when configured. They do not require the legacy gateway.
- **Confirmed transfer control:** list torrents and pause/resume one exact, uniquely resolved transfer through native qBittorrent 4.1+ or 5.x APIs. Confirmation binds the caller, arguments, torrent hash, service version and expiry. Mutations are never automatically retried.
- **Bounded independent reads:** “Show Sonarr queue and Radarr history” validates every clause and scope before execution. An unavailable service produces a partial result while other independent reads continue. Compound writes remain unsupported.
- **A preserved neural contract:** `arrmes.tools.v2` has 18 service actions plus `clarify` and `unsupported`. New native reads use `arrmes.tools.v3` and report `routing_source=explicit_service_deterministic`; they are not newly trained v6 model capabilities.
- **Portable inference:** optional native llama.cpp/GGUF on CPU, with explicit accelerator selection; optional MLX on Apple Silicon. CPU inference installs neither CUDA nor MLX. [Runtime setup, conversion and tested platforms](docs/portable-runtime.md).

Every dispatch validates the tool, arguments, entity and caller scopes. The HTTP service requires bearer authentication and explicit confirmation for consequential writes. Execution references come from a verified per-caller ledger. Credentials are kept out of model prompts, image layers and audit responses.

## Measurements and scope

The original 230–245 ms warm p50 claim timed generation only. Prompt preparation, verification, HTTP handling and service execution must be measured separately. The original roughly 344 MB of model/adapter files is not a runtime RAM measurement; the published adapter tensors are float32. The current public adapter also differs from a retained research checkpoint with the same v6 label, so their results are recorded separately.

New comparisons retain raw model predictions, translated-format scores, verifier decisions and controlled fixture execution as separate tracks. See the [comparison report](docs/benchmark-comparison.md) and versioned inputs under `data/overnight-v1/`. Historical sealed-suite results are not a substitute for performance on new requests.

The native adapters are validated against loopback HTTP fixtures. Actual upstream application versions, large live libraries and production writes still need qualification. Native Bitmagnet integration, subtitle mutations, request lifecycle management and genuine Armiger/DH-Waypoint adapters remain gaps. The old gateway Armiger and Waypoint substitutions now return unsupported.

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
- `ARRMES_BACKEND=native-only` — an optional model-free, read-only mode for the explicit service grammar. It loads no weights or inference runtime and clarifies requests outside that grammar.
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
