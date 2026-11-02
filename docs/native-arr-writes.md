# Confirmed native media searches

The existing trained v2 actions `tv_search_missing`, `movie_search` and
`music_search` can dispatch directly to configured native Sonarr, Radarr and
Lidarr APIs. They do not require the custom Arrmes gateway. Tool names, arguments,
model weights and the released v2 schema/hash are unchanged. The application
controller implements these actions; this is not new model training.

## Scope

- **Sonarr:** `tv_search_missing` resolves one exact unique series and reads its
  complete episode response. Eligible episodes are monitored, missing a file and
  known to have aired by the current aware UTC clock. A requested season limits
  the target set. Specials are excluded unless `season=0` was explicitly given.
  Future, undated, existing and unmonitored episodes are not searched.
- **Radarr:** `movie_search` resolves one exact movie title and optional year. It
  searches that one existing movie; it does not add it or change monitoring.
  Existing files and unmonitored movies can still be explicitly searched under
  Radarr's configured release/quality policy, including possible upgrades. Their
  current states are displayed and bound into confirmation.
- **Lidarr:** an explicit album resolves uniquely under one exact artist. The
  artist and album must be monitored and the album known to be released. An
  artist-only request is limited to that artist's monitored released albums with
  known missing tracks (`trackCount > trackFileCount`). Complete albums are
  excluded from artist-wide scope. Unknown/zero track metadata clarifies instead
  of guessing. A fully known empty missing set is a no-op with zero POSTs;
  an empty selection containing excluded undated monitored albums clarifies.

The existing `music_search` representation has no field distinguishing an explicit
artist-wide request from a model accidentally omitting an intended album. An
album-absent typed call is therefore meaningful only as artist-wide scope; absence
alone does not establish natural-language intent. The NL verifier must require
clear artist-wide/all/missing-albums wording and clarify an unspecified album.
The controller freezes and displays the exact resulting album IDs. It never
substitutes another artist or turns an album-specific request into artist-wide
scope. Broader artist-wide upgrades of complete albums remain unsupported.

Empty exact scopes and ambiguous entities clarify before confirmation. A target
set exceeding the finite dispatch budget also clarifies; it is never truncated
and partially sent. The default maximum is 250 targets, reduced by a smaller
native record budget. Complete unpaginated library arrays are scanned inside
the byte/time budget before asserting uniqueness, including entries beyond an
output-record cap. Unexpected paginated wrappers, duplicate IDs, mixed-parent
responses and malformed data fail closed.

## Commands and compatibility

Only these fixed command shapes are posted:

```json
{"name":"EpisodeSearch","episodeIds":[1,7]}
{"name":"MoviesSearch","movieIds":[2]}
{"name":"AlbumSearch","albumIds":[8,13]}
```

They use `POST /api/v3/command` for Sonarr/Radarr and
`POST /api/v1/command` for Lidarr. IDs come only from the freshly resolved signed
target set. No command with an empty ID list, generic command name, global
missing-search operation, `seriesId` filter or user-supplied raw ID is accepted.
Lidarr's `ArtistSearch` is deliberately not used: its singular `artistId` command
does not freeze a selected album set.

Initial supported application majors are **Sonarr 4, Radarr 6 and Lidarr 3**,
with API v3/v3/v1 respectively. Numeric stable versions are checked against the
configured service's `appName`; version and process start identity are signed and
rechecked. Earlier majors and prerelease version strings are refused until their
commands are qualified. The audited current stable source versions are Sonarr
4.0.20.3014, Radarr 6.4.4.10685 and Lidarr 3.1.6.5079. Source/protocol validation
does not claim every version in those major series or real binaries were tested.

## Confirmation and results

All three operations require current `media:manage` permission and the write
gate. Preparation performs only fixed GETs, then returns a single-use HMAC
confirmation in the `native-arr:` namespace. It binds caller, canonical tool
arguments, service/API version and process start, configured origin digest,
resolved title/year or artist/album, path identity, explicit target IDs and the
eligibility snapshot. Raw private paths and service credentials are not returned.

Redemption rechecks current scopes and the write gate and repeats uncached
resolution and eligibility reads. A changed entity, monitoring/file state,
release date, missing-track count, eligible episode/album set, origin, path,
version or process invalidates the signed snapshot. Newly aired media is not
silently added after confirmation. Expired, replayed, caller-mismatched and
tampered tokens are refused. Disabled mode performs no upstream requests; shadow
mode may prepare the target but never posts a command.

After valid confirmation, exactly one POST is attempted. Redirects, transport
failures and uncertain server errors are not retried. Known rejections and
unknown outcomes are reported separately. Responses are bounded, duplicate JSON
keys and nonfinite values are rejected, and only positive C# Int32 IDs are used.

A 201/202 response is **dispatched**, not downloaded media. When its response
contains a correlatable command ID/name/body, one bounded GET reads that command
ID. Both the resource name and its body name must match the expected command;
the observed command must match the exact ID set without a
broader scope. A successful completed command can set
`search_job_completion_verified=true`; that describes the indexer-search job
only. The operation still remains dispatched with `verified_complete=false` and
`media_download_completion_verified=false`. No file appearance, unrelated queue
entry or other command is credited as completion. There is no background polling
or automatic second dispatch. Existing native queue/history reads can show later
service activity.

The source APIs provide no cross-service transaction locking catalogue changes.
Fresh signed-snapshot checks narrow the race window but cannot freeze upstream
administration between the last read and command execution. Keeping explicit IDs
prevents the command from expanding to an unapproved library scope.

## Verification boundary

Tests use isolated, protocol-faithful HTTP fixtures, including changed targets,
wrong parents, empty/oversized sets, invalid types/JSON, authentication failures,
expiry, replay, signature tampering, redirects and uncertain mutation responses.
Scripted `RecordingModel` cases exercise NL through the actual runtime, verifier,
executor, controller, confirmation and native HTTP. They demonstrate system wiring
with a scripted proposal, not real model selection quality.

Actual Sonarr/Radarr/Lidarr binaries, live indexers, download clients, real media
files and real-model selection for these new native implementations are separate
qualification work. No production endpoint, training artifact or frozen benchmark
input is changed by this increment.

## Primary command evidence

The controller implements the HTTP protocol; no upstream GPL implementation code
is vendored. The following stable source revisions were inspected:

- [Sonarr v4.0.20.3014](https://github.com/Sonarr/Sonarr/releases/tag/v4.0.20.3014), commit `cab419ade8ac7fcab5bf80394ee492abd35d5f5a`: [EpisodeSearchCommand](https://github.com/Sonarr/Sonarr/blob/cab419ade8ac7fcab5bf80394ee492abd35d5f5a/src/NzbDrone.Core/IndexerSearch/EpisodeSearchCommand.cs) declares `List<int> EpisodeIds`; [execution](https://github.com/Sonarr/Sonarr/blob/cab419ade8ac7fcab5bf80394ee492abd35d5f5a/src/NzbDrone.Core/IndexerSearch/EpisodeSearchService.cs) iterates those IDs.
- [Radarr v6.4.4.10685](https://github.com/Radarr/Radarr/releases/tag/v6.4.4.10685), commit `a96bf7c3ecb23cb76993770727285dc17a613fbc`: [MoviesSearchCommand](https://github.com/Radarr/Radarr/blob/a96bf7c3ecb23cb76993770727285dc17a613fbc/src/NzbDrone.Core/IndexerSearch/MoviesSearchCommand.cs) declares `List<int> MovieIds`.
- [Lidarr v3.1.6.5079](https://github.com/Lidarr/Lidarr/releases/tag/v3.1.6.5079), commit `da7b4dfb1a9e7e1d6625c2dbc3fff96971ab26bd`: [AlbumSearchCommand](https://github.com/Lidarr/Lidarr/blob/da7b4dfb1a9e7e1d6625c2dbc3fff96971ab26bd/src/NzbDrone.Core/IndexerSearch/AlbumSearchCommand.cs) declares `List<int> AlbumIds`; [execution](https://github.com/Lidarr/Lidarr/blob/da7b4dfb1a9e7e1d6625c2dbc3fff96971ab26bd/src/NzbDrone.Core/IndexerSearch/AlbumSearchService.cs) iterates those IDs; [album statistics](https://github.com/Lidarr/Lidarr/blob/da7b4dfb1a9e7e1d6625c2dbc3fff96971ab26bd/src/Lidarr.Api.V1/Albums/AlbumStatisticsResource.cs) exposes `trackCount` and `trackFileCount`.
- [Sonarr command controller](https://github.com/Sonarr/Sonarr/blob/cab419ade8ac7fcab5bf80394ee492abd35d5f5a/src/Sonarr.Api.V3/Commands/CommandController.cs), [Radarr controller](https://github.com/Radarr/Radarr/blob/a96bf7c3ecb23cb76993770727285dc17a613fbc/src/Radarr.Api.V3/Commands/CommandController.cs) and [Lidarr controller](https://github.com/Lidarr/Lidarr/blob/da7b4dfb1a9e7e1d6625c2dbc3fff96971ab26bd/src/Lidarr.Api.V1/Commands/CommandController.cs) deserialize the named C# command and queue it. The [command resource](https://github.com/Sonarr/Sonarr/blob/cab419ade8ac7fcab5bf80394ee492abd35d5f5a/src/Sonarr.Api.V3/Commands/CommandResource.cs) exposes command ID, name, body, status and result for bounded correlation.
