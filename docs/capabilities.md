# Service capability audit and native read workflows

The released `arrmes.tools.v2` catalog has **20 names: 18 service operations and
two control operations** (`clarify`, `unsupported`). The code names ten service
categories, rather than the nine claimed in older prose. A catalog entry, a
training label, a sandbox handler and a working live integration are separate
evidence. Several release claims combined them.

[The current matrix](capabilities-after.json) records native operations, scopes, route layers and fixture coverage. The released v2 catalog and historical sandbox handlers remain separate from this native API qualification. A fixture test does not prove compatibility with every actual upstream deployment.

This increment adds **29 native read workflows over seven service APIs**. Each
advertised service/operation pair is tested from an explicit natural-language
request through `BoundedExecutor` to a loopback HTTP service fixture under
`NetworkIsolationGuard`. These are HTTP contract tests, not tests against real
Sonarr, Radarr, Lidarr, Bazarr, qBittorrent, Tautulli or Seerr binaries. No
production service was used for training, evaluation or mutation.

Six existing neural v2 reads also execute directly against configured native
services: `tv_status`, `tv_missing`, `movie_status`, `music_status`,
`now_playing` and `watch_history`. This bridge preserves the trained tool names
and separately validates the model-selected entity. A bounded library must be
complete before uniqueness is claimed; remakes and duplicate artists clarify.
Sonarr missing episodes include aired, monitored inventory and report unknown
air dates without calling them aired. A small actual-model HTTP fixture smoke
test is recorded separately from the 29 explicit grammar checks.

## What changed by service

- **Sonarr:** v2 had series status, title-specific missing episodes and an
  indexer-search write. Native reads now cover the global library, monitored
  missing inventory, metadata lookup, download queue, event history and health.
  Missing inventory never sends `MissingEpisodeSearch`. New reads preserve
  nested series/episode metadata so queue and missing rows retain their target.
- **Radarr:** v2 had movie status and an indexer-search write. Native reads add
  global library, monitored missing inventory, metadata lookup, queue, history
  and health. Metadata lookup calls `movie/lookup`; it does not add a movie or
  start a download.
- **Lidarr:** v2 had artist/album status and indexer-search writes. Native reads
  add artist library, monitored missing albums, artist metadata lookup, queue,
  history and health. Track-level management and album-specific discovery remain
  gaps.
- **Bazarr:** absent from v2 at every layer. Native reads now report missing
  movie or TV subtitles, subtitle status for an exact movie/series title, and
  health issues. Movie and TV scope is explicit. TV subtitle status first
  resolves the series through the bounded library, then reads its episodes;
  duplicate titles clarify. Subtitle download, upload, deletion, synchronization
  and language-profile changes remain unsupported.
- **qBittorrent:** v2's live listing uses a QUI facade, and its live pause/resume
  actions are explicitly unsupported. Native reads now list torrents with
  bounded offset pagination, report transfer speeds/counters and inspect
  application/WebAPI versions. Existing SID cookies work independently of the
  newer API-key authentication path. The existing trained `torrent_list` also
  supports native state/title filters. Existing trained `torrent_pause` and
  `torrent_resume` now have native exact-hash, confirmation-gated implementations;
  the legacy gateway still cannot execute these writes.
- **Tautulli:** v2 already had current streams and watch history. Native reads
  now connect directly, paginate history, inspect activity and report server
  version. Every response must contain the successful Tautulli application
  envelope; an HTTP 200 with an application error is rejected. `activity:read`
  remains required for operator-visible activity/history.
- **Seerr:** the correctly identified upstream product is
  [seerr-team/seerr](https://github.com/seerr-team/seerr), with native `/api/v1`.
  New reads cover server status and paginated metadata search. `Seer` is accepted
  as an explicit NL spelling alias for this configured native service.
  Identity-sensitive request listing, approval and creation are not implemented
  in this adapter.
- **Legacy `seer_discover` and Request Service:** the old tool calls a custom
  request-portal discovery route, rather than native Seerr. The local service
  migration record says this portal moved discovery to TMDB and retired Seerr
  reads before this release. Existing request status/submission behavior and
  gateway-owner identity restrictions are distinct from native Seerr support.
- **Bitmagnet:** v2's fixed read-only GraphQL search uses the BitAgent facade.
  It is not a separately configured or version-qualified native Bitmagnet
  integration. Native endpoint authentication, schema handshake and search
  pagination remain gaps.
- **Armiger and DH-Waypoint:** their v2 sandbox fixtures are implemented, but
  the initial live methods used a local regex approximation and an unrelated
  batch service respectively. Those results cannot substantiate live Armiger
  inference or DH-Waypoint health. The integration change rejects these live
  operations until their actual service contracts are connected.

## Routing and policy tradeoff

The v6 model and frozen v2 tool prompt remain intact. The new `service_query`
contract is a separate v3 extension. Its route is a conservative deterministic
parser requiring an explicit service mention and a recognized read intent.
Responses identify `routing_source=explicit_service_deterministic`; these
successes must not be counted as raw neural accuracy or additional fine-tuned
model knowledge. This delivers useful native reads while model/data expansion
is still unqualified.

Examples supported by the extension:

```text
Show Sonarr queue
What is missing in Radarr?
Show Lidarr history
Look up "Sintel" in Radarr
Show Sonarr library
Show missing TV subtitles in Bazarr
Show movie subtitles for "Sintel" in Bazarr
Show qBittorrent transfer speeds
Show Tautulli activity
Look up "Sintel" in Seerr
Show Sonarr queue and Radarr health
```

New entity-bearing metadata and subtitle routes require a single quoted title.
They do not guess an entity from arbitrary prose. Service names in the quoted
title do not select an adapter. Unsupported pairs, multiple candidate targets
and conflicting movie/TV scope abstain. Existing v2 entity-specific missing,
filtered-torrent and confirmation-gated write intents remain on the legacy
neural v2 route, with native execution where implemented. Unqualified
paraphrases and context references are explicit gaps for
these new reads.

The runtime supports at most four independent native read clauses (or the
configured executor step bound). It validates all clauses and scopes before
dispatch, keeps titles containing conjunctions intact, and can continue another
independent read when one service fails. Conditional, entity-dependent and write
composites still abstain. This is bounded read orchestration, not a free-form
agent execution language.

`service_query` validates the service/operation pair as well as its arguments.
Permissions depend on the selected service: `media:read` for the three *arr
services, Bazarr and Seerr; `torrents:read` for qBittorrent; `activity:read` for
Tautulli. The adapter checks the current authenticated principal again before
network access. It never accepts caller/model URLs, raw IDs, credentials, HTTP
methods or arbitrary endpoints. All `service_query` upstream calls are GET
requests. Native transfer mutations have a separate confirmed boundary below.

## Confirmed native torrent controls

`Pause "Fixture torrent" in qBittorrent` and the corresponding resume request
retain their existing v2 model action names. A complete bounded native inventory
must resolve one exact normalized title; prefixes, fuzzy matches, duplicate names
and incomplete inventories cannot select a target. Only one 40- or 64-character
hexadecimal hash is permitted. There is no `all` target or bulk mutation.

`torrents:manage` authorizes the write and its internal exact-target preflight.
The principal's current scope is checked again on confirmation. A short-lived,
single-use HMAC token binds the caller, original arguments, exact hash, name,
application API major and observed state. Confirmation rechecks that hash/name
and version rather than resolving the title again. A renamed/replaced target or
changed major requires a fresh confirmation. Benign transfer progress changes
do not redirect the bound content hash. Expiry at the exact deadline, replay,
principal substitution, argument changes and resolved-target changes are tested.

The adapter chooses `/torrents/pause` and `/torrents/resume` for application
4.1–4.6, or `/torrents/stop` and `/torrents/start` for application 5.x, after
checking WebAPI major 2. Its single form POST contains the bound `hashes` value.
It refuses redirects and never retries a mutation after a timeout, lost response
or server error; those outcomes remain unknown. A follow-up GET must observe the
requested stopped/runnable state for completion. An accepted but unsettled
transition reports `dispatched`, with no completion claim. An already-satisfied
state is a verified no-op. Shadow/disabled modes do not mutate; there is no native
direct-write mode.

The published Qwen adapter selected all four recorded synthetic pause/resume
prompts correctly in [the actual model smoke receipt](../experiments/overnight-v1/torrent-transfer-smoke.json).
Those real outputs also pass the runtime, confirmation and native HTTP fixtures
for both 4.x and 5.x (eight model-output replays). The
[execution receipt](../experiments/overnight-v1/native-transfer-execution-smoke.json)
records zero writes before confirmation, one exact-hash write after confirmation,
verified state and rejected replay. These small integration checks do not qualify
general neural accuracy, real qBittorrent binaries or production use. Model
inference timings and output-replay/confirmation timings remain separate.

Reproduce the captured-output fixture evaluation from a checkout with the test
extra installed:

```sh
python scripts/verify_native_transfer_replay.py
python -m pytest -q tests/test_native_transfers.py
```

## Native API configuration

Set `ARRMES_SERVICES_FILE` to an operator-owned JSON registry. An API root may
include its reverse-proxy prefix; do not append `/api` to the configured root.
Example file (replace placeholders locally and keep the file out of Git):

```json
{
  "services": {
    "sonarr": {"base_url": "http://localhost:8989", "api_key": "replace-locally"},
    "radarr": {"base_url": "http://localhost:7878", "api_key": "replace-locally"},
    "lidarr": {"base_url": "http://localhost:8686", "api_key": "replace-locally"},
    "bazarr": {"base_url": "http://localhost:6767", "api_key": "replace-locally"},
    "qbittorrent": {"base_url": "http://localhost:8080", "session_id": "replace-locally"},
    "tautulli": {"base_url": "http://localhost:8181", "api_key": "replace-locally"},
    "seerr": {"base_url": "http://localhost:5055", "api_key": "replace-locally"}
  }
}
```

Protect this file with local filesystem permissions, for example
`chmod 600 services.json`. The client bearer-token registry is still required;
these upstream credentials do not define Arrmes caller identity. Native-only
installations do not require a legacy gateway. `ARRMES_BACKEND=native-only` also
removes the model/runtime requirement for explicit reads and forces writes off;
it clarifies outside the explicit grammar. Restart after changing registries.
Runtime and model setup are documented
in [portable-runtime.md](portable-runtime.md).

Native reads default to five pages, 50 records per page, 250 returned records
and 8 MiB per response. A listing that hits a budget, repeats a page, changes its
reported count, or cannot finish entity resolution reports `partial`. qBittorrent
does not return a total count, so an exhausted page budget reports an unknown
total rather than inventing one. Library title filters distinguish scanned
upstream rows from matched rows. Upstream unpaginated library endpoints can
still require substantial response parsing before projection; large libraries
can hit the response-byte bound.

Upstream 401/403, missing API routes, malformed JSON, non-finite JSON numbers,
application-envelope failures, response limits and transport errors produce
structured errors. No unconfigured service falls through to another facade.
Redirects are refused so keys/cookies cannot follow upstream URLs. Audit entries
contain the method, fixed API path and status; returned row projections omit
local file paths, IP addresses and credential fields, including nested fields.

## API versions and primary evidence

Audited source revisions, exact content digests and retrieval times are in
[capabilities-api-sources.json](capabilities-api-sources.json). Upstream
application releases and API path versions are separate facts. No installed
upstream application version was hardware-tested in this increment.

- **Sonarr:** the pinned upstream
  [v3 OpenAPI contract](https://github.com/Sonarr/Sonarr/blob/bc54afbdd0e38bdb0fca4785fcf7d7a6af77bf9a/src/Sonarr.Api.V3/openapi.json)
  covers Sonarr application v3/v4. The newest website also exposes newer API
  paths; this adapter explicitly uses `/api/v3` and does not claim v5 support.
- **Radarr:** the pinned
  [v3 OpenAPI contract](https://github.com/Radarr/Radarr/blob/a96bf7c3ecb23cb76993770727285dc17a613fbc/src/Radarr.Api.V3/openapi.json)
  defines library/lookup, missing, queue, history, health and system status.
- **Lidarr:** its pinned
  [v1 OpenAPI contract](https://github.com/Lidarr/Lidarr/blob/da7b4dfb1a9e7e1d6625c2dbc3fff96971ab26bd/src/Lidarr.Api.V1/openapi.json)
  defines the analogous artist/album operations under `/api/v1`.
- **Bazarr:** the official
  [API source](https://github.com/morpheus65535/bazarr/tree/bfe4593b04caa16eb4a09c2a233777b0bd7723b6/bazarr/api)
  defines an unversioned `/api` namespace. Wanted records use `start`/`length`;
  TV episode status needs a resolved series ID. Only the documented GET handlers
  and header authentication are implemented here.
- **qBittorrent:** the official
  [WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29)
  uses `/api/v2` and documents offset/limit listing and transfer statistics. An
  existing SID cookie is supported. Native stateless keys require application
  v5.2.0 / WebAPI v2.14.1 or newer and use
  [Bearer authentication](https://github.com/qbittorrent/qBittorrent/wiki/API-Key-Authentication-%28%E2%89%A5v5.2.0%29),
  supplied via the registry's `api_key` field. SID expiry is reported; Arrmes does
  not automatically log in. The native transfer adapter also follows the documented
  [4.1–4.6 mutation names](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-4.1%29)
  and 5.x stop/start names. These mappings are exercised by controlled fixtures.
- **Tautulli:** its official
  [API reference](https://github.com/Tautulli/Tautulli/wiki/Tautulli-API-Reference)
  defines `/api/v2?cmd=...`, offset/length history and application envelopes.
  This adapter uses the API-key header, requiring Tautulli v2.18.0 or newer;
  older query-string-key installations are not qualified.
- **Seerr:** the pinned
  [API schema](https://github.com/seerr-team/seerr/blob/b185621e8e48743bb38adaa0d15b334626b7b100/seerr-api.yml)
  defines status and paginated search under `/api/v1` and API-key header
  authentication. This does not establish compatibility with an arbitrary
  differently named Seer service or an older Overseerr/Jellyseerr deployment.
- **Bitmagnet:** the
  [upstream project](https://github.com/bitmagnet-io/bitmagnet) documents a
  GraphQL-based indexer/search engine. The existing BitAgent facade is a fork
  integration; its specific search schema does not qualify every upstream
  Bitmagnet version.

Run the native workflow regression suite with
`python -m pytest -q tests/test_native_services.py`. The tests include all
advertised pairs, real loopback requests, root runtime NL integration,
permission denial before network, ambiguity, pagination, application errors,
response bounds, nested redaction and redirect credential containment.
