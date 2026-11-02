# Actual Seerr 3.5.1: identity and empty reads

An isolated Linux x86_64 run of the official Seerr 3.5.1 image passed 25 checks
on 2 November 2026. The harness's 22 boundary tests also passed on Linux.
This qualifies the actual status/identity/empty-request response shapes used by
Arrmes. It does not qualify populated request workflows, movie submission,
external-account setup or a direct TCP client.

The machine was an AMD Ryzen 9 7950X3D with 32 logical CPUs, running Ubuntu
26.04.1 LTS and Docker 29.9.2. Before testing, live CPU pressure was low and
available RAM exceeded 76 GiB. One test container was limited to one CPU,
1,610,612,736 bytes of RAM and the same total memory-plus-swap allowance.
It used the runc runtime, no GPU devices, no network, no published ports,
no bind mounts, a read-only root and disposable tmpfs directories. Production
containers, stack configuration and drivers were not targeted.

## Primary artifact identity

The [official container instructions](https://docs.seerr.dev/getting-started/docker/)
identify the Seerr-team GHCR image and version tags. The
[immutable v3.5.1 release](https://github.com/seerr-team/seerr/releases/tag/v3.5.1)
points to source revision 564bfb08b54caa374bddaf820abfd226e5e69151.
The observed OCI source, version and revision labels match that release.

The registry metadata was fetched over authenticated anonymous-pull HTTPS;
the token was neither stored nor published. Declared and computed registry
digests agreed:

- Tag: ghcr.io/seerr-team/seerr:v3.5.1
- Index: sha256:d8fab675b55ad293ded98fd71efcfd46054cbad9746534ae1cc4794a88c0b3be
- Linux AMD64 manifest: sha256:2636191726a315dbd75228537f7be74b2fa7498998bb22e37b5b5c8756018fba
- Config: sha256:61f6fa0cf472caa95736d00fa624aea848f687b2ac899c2370a509de93e8abba

The test pulled and ran the AMD64 manifest digest, not a mutable tag.
Cosign was unavailable on the test host, so no cryptographic signature or SBOM
verification is claimed. Digest/label checks do not replace signature
verification. The upstream
[signed-artifact guide](https://docs.seerr.dev/using-seerr/advanced/verifying-signed-artifacts/)
documents the exact release-workflow identity and OIDC issuer for that additional
check.

## Actual behavior observed

Two local synthetic users were inserted into the fresh database with the
application's actual stable User ORM and permission enums: administrator 1 and
non-admin requester 2. The upstream server performed migrations and generated
their creation timestamps. This deliberately bypasses normal external-media
account setup; no Plex, Jellyfin, Emby, TMDB or production account was used.
No media requests were seeded or submitted.

The actual application returned:

- GET /api/v1/status: HTTP 200 and version 3.5.1.
- GET /api/v1/auth/me with a valid key and X-API-User 2: HTTP 200, user 2 and a
  timezone-aware createdAt string accepted by Arrmes's identity fingerprint.
- A valid API key with no user header: HTTP 200 as administrator 1.
- Unknown mapped user or invalid key: HTTP 403; missing key: HTTP 401.
- GET /api/v1/request with requestedBy=2: HTTP 200, an empty result list and
  pageInfo containing pages=0, pageSize=2, results=0 and page=1.
- A non-admin user requesting requestedBy=1: HTTP 403 even in the empty database.
  Invalid/absent authentication on the request list was also rejected.

These results corroborate the stable
[authentication middleware](https://github.com/seerr-team/seerr/blob/v3.5.1/server/middleware/auth.ts),
[identity route](https://github.com/seerr-team/seerr/blob/v3.5.1/server/routes/auth.ts),
[User entity](https://github.com/seerr-team/seerr/blob/v3.5.1/server/entity/User.ts)
and [request ownership filter](https://github.com/seerr-team/seerr/blob/v3.5.1/server/routes/request.ts).
The administrator fallback makes the trusted user mapping and fresh identity
check necessary; a valid key alone does not establish caller identity.

Actual Arrmes source from e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4 accepted the
real identity fingerprint, version and bounded empty-owned-request page.
Its normal request-read helpers always sent the mapped user header. Missing
current scopes or a trusted user mapping caused zero upstream calls. An unknown
mapped identity's real HTTP 403 propagated before any metadata lookup.
The complete explicit phrase "Show Seerr health" passed through runtime routing
and the executor to the actual upstream status response without inference.

The transport was Arrmes SDK -> a loopback GET-only audit proxy -> Docker exec
Node HTTP GET -> the actual Seerr server on its own loopback. Successful
responses were forwarded unchanged. The whitelist excludes metadata search,
media details and every write route. It cannot issue a mutation or arbitrary
HTTP request. Network-none prevents the Seerr instance from reaching external
services. This is not direct network/TLS/proxy qualification, and the Arrmes
HTTP entrypoint was not exercised.

## Memory, failures and cleanup

Seerr's cgroup memory peak, including disposable Docker-exec probes, was
306,696,192 bytes in run 02 and 302,829,568 bytes in run 03. These are measured
peaks, not image-size estimates or measurements of Arrmes/model RAM.
Docker reports an image Size of 1,342,799,055 bytes; its compressed layers
total 254,180,426 bytes. No inference runtime or model was loaded.

All three original receipts are retained:

- Run 01 stopped before creating a container. Its harness assumed legacy Docker
  image IDs were config digests; Docker 29's containerd store reports the manifest.
  The corrected guard accepts either known ID while still requiring the exact
  repository digest, architecture, source revision and version labels.
- Run 02 passed 20/21 checks. Missing API keys correctly received 401, whereas
  the initial harness expected 403. That failed receipt remains unchanged.
- Run 03 passed 25/25 checks after correcting the exact authentication
  expectation and adding request-list authentication/identity-propagation probes.

Every run's full harness snapshot and digest is retained, and all 35 application
source-file hashes were checked against the public source commit.
Published receipts encode that checksum map as path/digest records, because a
security scanner otherwise misidentifies the auth.py file checksum as an API
key. The export ledger records original and published hashes; restoring the map
and original JSON formatting exactly reconstructs the original receipt bytes.
No check, status, timing or memory value changed. The owner evidence archive
also retains all original receipts and complete source snapshots.
The final Linux boundary suite passed 22 tests. It covers the GET whitelist,
authentication-status/body preservation, credentials outside process arguments,
write rejection, both Docker image-ID forms, failure receipt preservation,
proxy cleanup and refusal to delete a container with another ownership label.

The final host check found zero test containers and zero test processes.
The temporary source, venv, logs and test directories were removed; no persistent
network or volume was created. The single pinned image remains cached for
reproduction, as recorded in the cleanup receipt. Images were not pruned.

## Reproduction and remaining gaps

Use an operator-approved disposable Linux workspace after checking current host
resource availability and applicable lease rules. The helper never pulls an
image or alters host configuration:

    docker pull --platform linux/amd64 ghcr.io/seerr-team/seerr@sha256:2636191726a315dbd75228537f7be74b2fa7498998bb22e37b5b5c8756018fba
    python3 -m venv "$ARRMES_TEST_WORKSPACE/venv"
    "$ARRMES_TEST_WORKSPACE/venv/bin/python" -m pip install jsonschema==4.26.0
    "$ARRMES_TEST_WORKSPACE/venv/bin/python" scripts/smoke_seerr_upstream.py \
      --work-root "$ARRMES_TEST_WORKSPACE" \
      --output "$ARRMES_TEST_WORKSPACE/seerr-read-receipt.json"

The output must be a new path so earlier evidence cannot be overwritten.
The helper records exact source hashes; its source-basis field describes the
original authoring base. A run against later application files needs its own
source verification and qualification statement.

The raw evidence is under experiments/upstream-seerr-v1. It establishes neither
nonempty ownership filtering nor complete title-based request_status execution,
which would require metadata resolution excluded by this test. Movie submission,
confirmation/postcondition behavior, request lifecycle transitions, external
login, real provider access, direct TCP/TLS and other Seerr versions remain
unqualified against the actual binary here. Controlled HTTP fixtures cover some
of these behaviors separately and must not be described as this binary run.
