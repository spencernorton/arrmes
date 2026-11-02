# Caller-owned native Seerr requests

The native adapter supports the existing `request_status` and `request_media`
tool names without the legacy request portal. Status reads cover an exact,
uniquely resolved movie or TV title. Creation covers **one standard-quality
movie request**, with explicit confirmation. TV creation, partial seasons, 4K,
approval, cancellation, deletion and retries remain unsupported.

This integration targets **seerr-team/seerr**, audited at stable
[v3.5.1](https://github.com/seerr-team/seerr/releases/tag/v3.5.1), commit
`564bfb08b54caa374bddaf820abfd226e5e69151`. Its API prefix is `/api/v1`.
It is separate from the older custom `Seer` request-portal compatibility route.
No claim is made that arbitrary similarly named services have this contract.
The adapter implements Seerr 3.x API-v1 version syntax, with exact application
version binding during confirmation. Older Overseerr 1.x, Jellyseerr 2.x and
unknown major versions return an unsupported-version error. Only the pinned
3.5.1 contract was audited; accepting another 3.x version string does not qualify
that patch or deployment.

## Configuration and caller identity

An operator must map every permitted Arrmes subject to its actual Seerr user ID:

```json
{
  "services": {
    "seerr": {
      "base_url": "http://localhost:5055",
      "api_key": "replace-locally",
      "user_ids": {"local:viewer": 7}
    }
  }
}
```

Obtain the ID through trusted Seerr user administration. The authenticated Arrmes
client registry owns `local:viewer`; request bodies and model arguments cannot
provide a subject, user ID, API root or credential. Keep both registries private
and restart after changes. `requests:read` permits status reads;
`requests:create` permits a movie submission and its necessary read preflight.
Seerr's own permissions and quotas still apply.

The stable [authentication middleware](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/server/middleware/auth.ts)
defaults API-key calls to administrator user 1 if `X-API-User` is absent. Arrmes
therefore sends the mapped `X-API-User` and `X-Api-Key` on **every** request,
checks `/auth/me.id` before metadata or request lookups, and checks it again
immediately before mutation. A missing mapping, wrong effective ID or a dropped
identity header fails closed. A configured Seerr connection never falls back to
the legacy gateway because its caller mapping is missing.

The adapter also requires a timezone-aware `createdAt` in `/auth/me` and binds an
account fingerprint built from ID, normalized creation instant and email when
supplied. This detects a deleted/recreated account reusing the same numeric ID.
The fingerprint is checked before reading confirmation-time account data and
again immediately before POST. Creation timestamps, email and the raw user body
are not returned. A legitimate email change after preview also requires a fresh
confirmation; this conservative tradeoff prevents an old preview from authorizing
a different account identity.

A reverse proxy must consistently preserve these headers on every endpoint.
Arrmes checks identity before submission and ownership after it; it cannot prove
how a proxy or server handled a header after transmission. Session-cookie login
and client-supplied acting-user overrides are not supported.

## Selection and privacy

Search scans the complete bounded `/search` result before declaring an exact
title/type/year match unique. Remakes and movie/TV name collisions clarify.
Valid person and collection results are recognized but cannot become movie or TV
targets. The selected `/movie/{id}` or `/tv/{id}` detail must agree with the
search result's provider ID, title and release year. There is no fuzzy match,
first-hit fallback or model-supplied provider ID.

Status has only a title argument in the preserved v2 schema. It resolves that
unqualified title conservatively across movie and TV results. A request for a
particular remake, season or quality requires a representation that preserves
that qualifier; it must not be silently interpreted as a different title/version.
Movie creation has representable `media_type` and optional `year` arguments.

Caller-owned requests are read with `requestedBy=<mapped ID>` and bounded
`take`/`skip` pagination. Every returned record must name that same owner, even
if the upstream user has administrator privileges. An ignored filter or foreign
record returns an error without records or counts. Incomplete pagination cannot
establish that no request exists or authorize a duplicate write.

Returned data includes only matching owned request IDs, canonical media identity,
request state, quality and library availability. It omits other users, email,
tokens, profile/server settings, filesystem paths, global request counts and raw
upstream errors. Availability is a media-library observation; it does not prove
that a transfer completed. A request can be pending approval while the submission
operation itself has completed successfully.

## Confirmation and execution

A typical existing-tool workflow is “Request the movie \"Fixture movie\" (2020)”,
followed by confirmation of the returned token. Status can be read with “What is
my request status for \"Fixture movie\"?”. These examples exercise the existing
tool representation; adding a native adapter does not establish new model
selection accuracy or change the frozen neural prompt.

The preview identifies one movie, canonical year, mapped Seerr account and
standard quality. It explains that Seerr approval/download policy applies. A
short-lived, single-use HMAC token binds the principal, original arguments,
canonical TMDB ID/title/type/year, Seerr version, mapped account fingerprint and configured API
root digest. Confirmation checks current scopes/mapping and repeats the complete
fresh lookup. An upgrade, changed identity or changed target requires a new
confirmation. Expiry and replay fail before mutation.

The explicit phrase `Request movie "Fixture movie" (2020) in Seerr` reaches the
same controller without neural inference. `Show my request status for "Fixture
movie" in Seerr` reads the uniquely resolved, caller-owned request. An explicit
Seerr request with no native Seerr configuration clarifies rather than falling
back to a different portal. The model-free server defaults to read-only; its
deliberate confirmed-write opt-in is documented in the [runtime guide](portable-runtime.md).
These routes are deterministic application behavior, not newly trained tools.

An existing own pending/approved request is a verified no-op. An own completed
request is a no-op only when the media is still available. Failed, declined or
multiple conflicting own requests require review; this adapter does not infer a
retry. Another user's request never becomes a successful caller-owned no-op.
Concurrent confirmations in one adapter serialize their write preflights, so the
second can observe the first submission instead of sending another POST.

The only allowed mutation is one `POST /request` containing exactly:

```json
{"mediaType": "movie", "mediaId": 101}
```

The numeric ID comes from validated metadata and the signed confirmation. There
are no `userId`, `ignoreQuota`, `serverId`, `profileId`, `rootFolder`, season or
quality overrides. Seerr's configured defaults and caller policy remain in
control. Shadow and disabled modes do not send this POST; direct mode is not
available.

A completed submission requires both a valid HTTP 201 body and a fresh
`GET /request/{id}` agreeing on caller, request ID, TMDB ID, movie type, standard
quality and a recognized request/availability state. A lost postcondition after
a valid creation is `dispatched` with unknown outcome. Timeouts, 5xx and malformed
creation responses remain unknown; known permission/quota/duplicate rejections
remain rejected. No mutation is automatically retried, and the confirmation token
is already consumed.

## Version evidence and validation limits

The pinned [request routes](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/server/routes/request.ts)
define `requestedBy`, caller ownership, HTTP 201 creation and request lookup.
The [stable implementation enums](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/server/constants/media.ts)
define request states 1–5 and availability 1–7, including blocklisted 6 and
deleted 7. Some examples/descriptions in the same release's
[API schema](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/seerr-api.yml)
are stale; the adapter follows the stable implementation rather than accepting
example status 0 or confusing blocklisted with deleted. The
[search result mapper](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/server/models/Search.ts)
also confirms movie, TV, person and collection shapes and camelCase date fields.

Run the controlled HTTP tests with no model or accelerator:

```sh
python -m pip install -e '.[test]'
python -m pytest -q tests/test_native_requests.py
```

Fixtures cover identity-header loss/admin fallback, ownership isolation, scopes,
exact metadata, ambiguity, bounded pagination, signed bindings, expiry/replay,
concurrent confirmations, numeric user-ID reuse, last-moment account replacement,
unsupported version families, rejected/uncertain mutations and postconditions.
Response and page limits inherit the native adapter's finite budgets. Existing
v2 schemas and model artifacts are unchanged.

The stable [User entity](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/server/entity/User.ts)
persists `createdAt`, and the
[authenticated-user route](https://github.com/seerr-team/seerr/blob/564bfb08b54caa374bddaf820abfd226e5e69151/server/routes/auth.ts)
returns it through `toJSON()`. Additional synthetic version-string tests exercise
the 3.x parser and binding, not real compatibility with untested patch releases.

**No actual Seerr binary, real account, production endpoint, production write or
new model training was used for this increment.** Scripted model-output fixtures
test the executor/adapter boundary; they do not qualify the published model's
natural-language accuracy. TV creation remains a durable gap: all-season scope,
existing partial requests, already available seasons and upstream normalization
must be qualified before it is enabled.
