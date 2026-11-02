# Native Bitmagnet search and health

Arrmes now connects directly to **bitmagnet-io/bitmagnet** for indexed torrent
metadata search and health. This is separate from the legacy BitAgent gateway
facade. It does not import, classify, delete, download or add torrents to a client.

Configure an operator-owned `ARRMES_SERVICES_FILE` entry:

```json
{"services": {"bitmagnet": {"base_url": "http://localhost:3333"}}}
```

The root may include a reverse-proxy prefix; Arrmes appends `/graphql`. Grant the
authenticated Arrmes caller `releases:read`. An upstream credential never grants
Arrmes permissions. Keep configuration files private and restart after changes.

With `ARRMES_BACKEND=native-only`, no model or inference runtime is needed for:

```text
Search Bitmagnet for "Fixture title"
Find "Fixture title" in Bitmagnet
Look up "Fixture title" in Bitmagnet
Show Bitmagnet health
Show Sonarr queue and Bitmagnet health
```

These routes report `explicit_service_deterministic`. Search requires exactly one
quoted query and an explicit Bitmagnet mention. Unsupported filters, multiple
queries and mutation requests abstain. The quoted text becomes the upstream text
search expression; Arrmes does not reinterpret it as a library entity or invent
a target. Bitmagnet's [text-search syntax](https://bitmagnet.io/guides/text-search.html)
includes phrase, exclusion and wildcard operators. Such text is always a JSON
variable, never interpolated into a GraphQL document.

The already-trained `bitmagnet_search` action also executes directly against
this native connection when a neural model selects it. Its v2 schema and hash
remain unchanged. Plain unquoted searches retain that neural v2 route; model-free
mode clarifies them. Quoted requests with unsupported extra filters still clarify
instead of broadening. Scripted-output integration tests verify that executor bridge;
they do not establish new neural prediction accuracy or train the model on new
tools. Natural-language qualification beyond the explicit grammar remains part
of the separate model benchmark.

## API and authentication

The [official endpoint guide](https://bitmagnet.io/guides/endpoints.html) identifies
`/graphql`. The audited latest stable release is
[v0.10.0](https://github.com/bitmagnet-io/bitmagnet/releases/tag/v0.10.0), commit
`4d4042acdddfa1af05bfaf092a68c97b1e3698e2`. Current upstream source
`e31b30d7351c269dc801876f92ebc59d31fee545` is tagged `v0.10.1-beta.1`.
The queried root, search and model schemas match across those revisions:

- [Root query and health schema](https://github.com/bitmagnet-io/bitmagnet/blob/4d4042acdddfa1af05bfaf092a68c97b1e3698e2/graphql/schema/query.graphqls).
- [Search inputs, pagination and count fields](https://github.com/bitmagnet-io/bitmagnet/blob/4d4042acdddfa1af05bfaf092a68c97b1e3698e2/graphql/schema/torrent_content.graphqls).
- [Torrent and content result fields](https://github.com/bitmagnet-io/bitmagnet/blob/4d4042acdddfa1af05bfaf092a68c97b1e3698e2/graphql/schema/models.graphqls).
- [HTTP registration](https://github.com/bitmagnet-io/bitmagnet/blob/e31b30d7351c269dc801876f92ebc59d31fee545/internal/gql/httpserver/httpserver.go).

GraphQL has no versioned URL here. The response's `version` is recorded as an
application version, not confused with an API path version. Different/future
schemas produce structured errors rather than a fallback to the gateway.

Upstream lists authentication/API keys/access levels as
[unimplemented work](https://github.com/bitmagnet-io/bitmagnet/blob/e31b30d7351c269dc801876f92ebc59d31fee545/bitmagnet.io/index.md).
For an operator-configured proxy that accepts `X-Api-Key`, the existing registry
`api_key` field sends that header. This is proxy authentication, not a claim that
Bitmagnet issues native keys. Basic authentication, SSO login and session-cookie
authentication are not implemented by this adapter. A configured `session_id`
returns an explicit configuration error. HTTP 401/403 is reported; credentials
never follow a redirect or appear in audit output.

## Bounds and execution policy

Search sends a fixed named `query` to `torrentContent.search(input: ...)`.
Health sends another fixed query for `version`, health/check statuses and worker
started states. The application accepts neither a GraphQL document nor a URL
from the tool/model. The transport can select only those two immutable documents.
An injected GraphQL-looking search string remains a variable. No mutation
document, generic POST forwarding or automatic retry exists.

Default bounds are five pages, 50 records per page, 250 returned records and
8 MiB per response, with finite connection/body deadlines. Search has deterministic
relevance/hash ordering and finite offset pagination. The upstream provides no
snapshot token: concurrent index changes can still affect a scan. Repeated or
overlapping IDs, changing exact counts, inconsistent pagination and exhausted
budgets return `partial`, never a completed inventory.

Bitmagnet uses a [budgeted count that can be an estimate](https://bitmagnet.io/faq.html).
Arrmes preserves `reported_total_count` and `total_count_is_estimate` separately.
It follows `hasNextPage`, not that estimate, to find exhaustion. A partial scan
reports `total_records: null`; a complete scan reports the number actually read.
An estimated count can legitimately differ from the observed result count.

Results include validated content identity, torrent hash/name/size, optional
file/peer counts, classification and a minimal magnet URI constructed from the
validated hash. Paths, tracker URLs, raw sources and credential fields are omitted.
This URI does not cause a client transfer. GraphQL errors, including partial data
with HTTP 200, return an error without raw upstream messages. A reported health
state of `down` is a verified unhealthy observation; `unknown`/`inactive` does not
become a healthy claim.

## Verification and limits

Run the controlled native HTTP tests without weights or an accelerator:

```sh
python -m pip install -e '.[test]'
python -m pytest -q tests/test_native_bitmagnet.py tests/test_native_services.py
```

The fixtures exercise the real HTTP client, explicit NL runtime, executor,
permission boundary, existing v2 bridge, fixed POST queries, pagination,
estimated counts, errors, redaction, redirects and finite response budgets.
The v2 model-facing hash is checked against the immutable v1.2.0 baseline.
Source hashes and [schema-check evidence](../experiments/bitmagnet-v1/schema-validation.json)
are recorded separately. Both fixed documents validate against the full primary
SDL of stable v0.10.0 and the audited beta revision, with zero validation errors.
This is schema validation, not application execution. To reproduce in a separate
development environment:

```sh
python -m pip install graphql-core==3.2.6
git clone https://github.com/bitmagnet-io/bitmagnet.git ./work/bitmagnet-source
python scripts/validate_bitmagnet_schema.py --upstream ./work/bitmagnet-source \
  --output ./work/bitmagnet-schema-check.json
```

**No actual Bitmagnet binary, seeded database, production endpoint or production
write was tested in this increment.** HTTP fixtures and query-schema validation
do not qualify all upstream deployments. Facets, torrent files, index imports,
classification and lifecycle management remain outside this read adapter.
