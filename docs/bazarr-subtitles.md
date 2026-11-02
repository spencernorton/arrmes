# Confirmed Bazarr subtitle workflow

The native controller supports one exact movie or episode at a time: confirmed
manual provider search, followed by a separately confirmed download of a selected
candidate. These operations use explicit deterministic routes. They are not new
capabilities learned by the released neural adapter, and the v2 neural contract
remains unchanged.

The initial compatibility target is **Bazarr 1.6.2**, whose HTTP API uses the
unversioned `/api` prefix. Other versions are refused until qualified. The Arrmes
v3 tool contract is separate from Bazarr's API naming.

## User workflow

For a movie:

```text
Find English non-forced non-HI subtitles for movie "Sintel" (2010) in Bazarr
```

For an episode:

```text
Find English non-forced non-HI subtitles for episode S01E02 of "Fixture series" (2020) in Bazarr
```

Both examples first prepare an exact target and ask for confirmation. Confirming
the search contacts Bazarr's configured providers and returns bounded candidate
references. Select one returned reference and request its download:

```text
Download English non-forced non-HI subtitle candidate <24-hex-reference> for movie "Sintel" (2010) in Bazarr
```

The placeholder must be replaced with a reference returned to that same caller.
The target, language and flags must match the search. The response also includes
the exact `download_arguments` for structured clients. The download requires a
new confirmation; confirming the search does not authorize a later download.

A strict alternative grammar accepts Bazarr's enabled two-letter language code:

```text
Search Bazarr subtitles for movie "Sintel" (2010) language en forced false HI false
Download Bazarr subtitle candidate <24-hex-reference> for movie "Sintel" (2010) language en forced false HI false
```

Language and both forced/hearing-impaired flags are required. Omitted or
unsupported variants clarify instead of choosing a default. The natural grammar
maps English, French, Spanish, German, Italian, Japanese, Korean, Portuguese,
Chinese, Arabic, Hindi, Russian, Dutch, Polish and Swedish to codes. Language
availability and compatibility with the exact media profile are checked against
Bazarr. Regional names are not silently broadened. Titles must be quoted, episode
coordinates explicit, and compound writes are refused. A duplicate title requires
disambiguation; a year can distinguish movie or series remakes.

## Confirmation and execution

Both new actions require `media:manage` and the write gate:

- `bazarr_subtitle_search` performs manual provider search. Although Bazarr exposes
  it as GET, its handler may refresh subtitle indexes and missing-language metadata
  and it contacts providers. It therefore has write effect and needs confirmation.
- `bazarr_subtitle_download` accepts only a server-held candidate reference. It
  never accepts arbitrary provider URLs or upstream subtitle cache UUIDs from a
  user. It performs one form POST and does not retry the mutation.

The existing `service_query` subtitle status operation remains read-only and does
not call provider-search endpoints. Catalog metadata lookup, provider subtitle
search and downloading a subtitle are distinct operations. Native-only read mode
keeps both new actions disabled.

Before any provider search or download, the controller checks current scopes,
configured service identity/version, enabled language, a complete bounded media
catalogue and an exact unique entity. Episode resolution uses series ID plus
season/episode and never selects an episode globally by title. HMAC confirmation
binds caller, canonical arguments, resolved IDs, version/process start identity,
media path digest, full language-profile digest and candidate fingerprint.
Redemption repeats those reads without cached catalogue results. Changed targets,
profiles, origins, restarted services and revoked scopes are refused.

Candidate references expire after at most five minutes and are bounded globally
and per caller. Evicted, expired, changed and other-caller references are refused.
The candidate is claimed atomically before dispatch, so multiple confirmations
cannot post the same candidate twice. Confirmation tokens are single-use; expiry,
replay and argument/resolution tampering are tested. A provider search is not
performed before its confirmation, and no POST occurs before download confirmation.

## Completion and tradeoffs

Bazarr's POST returns 204 after queueing asynchronous work. Accepted downloads
remain `dispatched` with `verified_complete=false`. A bounded fresh metadata check
can separately observe a new external subtitle with the requested language/
forced/HI identity and positive file size for the same media target. That sets
`metadata_postcondition_observed=true`; it does not complete the selected candidate
job. A timeout, redirect or upstream 5xx is an unknown outcome, and the POST is not
retried.

The pinned API exposes a random cache UUID for a search candidate, while history
records the provider-native subtitle ID. The candidate response omits that native
ID, and the download response supplies no correlated job ID. Provider/language/
timestamp matching cannot distinguish another candidate downloaded concurrently,
even by the same provider. No additional provider search or heuristic history
match is used to claim completion.

Metadata observation is not an independent filesystem check or correlation with
the queued provider job. `job_completion_correlated` and `postcondition_verified`
remain false, and the response records its observation basis. Concurrent upstream
administration cannot be
locked by this HTTP API; fresh signed-snapshot checks reduce the client-side
recheck window without providing a cross-service transaction.

An already-present matching external subtitle blocks this workflow rather than
silently overwriting it. Subtitle replacement/upgrades, deletion, upload, bulk
search, automatic choice of a provider candidate and arbitrary regional-language
translation are outside this increment. When dispatch has an unknown outcome,
check the actual subtitle status before starting a new search/download workflow.

The validation here uses protocol-faithful isolated HTTP fixtures, including
adversarial responses. Actual Bazarr binaries, live subtitle providers, provider
quotas and real subtitle files are separate qualification work; fixture passing
does not claim those were tested.

## Primary API evidence

The stable release was checked against
[Bazarr v1.6.2](https://github.com/morpheus65535/bazarr/releases/tag/v1.6.2), commit
`8c68f16a1e12f8e7985baeb73fb4199b5149108a`. This is Bazarr itself, rather than
another similarly named service or a fork. No Bazarr implementation code is
vendored; this controller implements its HTTP protocol.

- [Movie provider endpoints](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/providers/providers_movies.py): GET `radarrid`; POST `radarrid`, `hi`, `forced`, `original_format`, `provider`, `subtitle`.
- [Episode provider endpoints](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/providers/providers_episodes.py): GET `episodeid`; POST `seriesid`, `episodeid` and the same candidate/flag fields.
- [Manual search/download implementation](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/subtitles/manual.py): per-media profiles, candidate flags and asynchronous jobs.
- [Subtitle cache](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/subtitles/cache.py): opaque UUID references with upstream expiry; Arrmes uses a shorter caller-bound lifetime.
- [Download processing](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/subtitles/processing.py) and [history response](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/movies/history.py): provider-native subtitle IDs do not expose the candidate-cache UUID or a correlated download job ID.
- [Languages](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/system/languages.py), [profiles](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/system/languages_profiles.py), and [subtitle metadata fields](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/swaggerui.py).
- [Episode metadata](https://github.com/morpheus65535/bazarr/blob/8c68f16a1e12f8e7985baeb73fb4199b5149108a/bazarr/api/episodes/episodes.py): its marshalled response omits profile ID, so the resolved series profile is authoritative.
