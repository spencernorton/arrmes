# Arrmes native-only efficiency addendum

The new native-only mode completes explicit service reads without loading a model
or inference runtime. On controlled local HTTP fixtures, **140/140 measured
requests** match their expected proposals and business fields. Full authenticated
request latency is **1.35 ms p50 / 1.70 ms p95**, and the isolated Arrmes process
peaks at **48.0 MB RSS**. This is a separate task mix and does not establish a
speedup or quality improvement over the neural benchmark.

Recorded on 1 November 2026, America/Denver; receipt timestamp
`2026-11-02T05:29:23.762490+00:00`. The original comparison report and frozen model
outputs remain unchanged.

## Measurement and scope

Fourteen explicit-service read tasks were frozen before server launch and run ten
times each. The task set covers health, queues, missing media, library, subtitles,
activity, history and catalog search across the seven configured native adapter
identities: Sonarr, Radarr, Lidarr, Bazarr, qBittorrent, Tautulli and Seerr. They use
synthetic fixture responses, not seven running upstream service binaries. Twenty-
eight warmup requests, one route preview and one execution per task, are excluded.

All requests run serially on an Apple M4 Pro with 48 GiB unified memory,
macOS 27.0.1 ARM64 and Python 3.12.13. The Arrmes server runs in its own process;
the client and upstream fixture run separately. Both processes restrict network
access to the allowed loopback ports. Each request opens a fresh HTTP connection.

- Full authenticated `/v1/execute` client wall: **1.3545 / 1.6965 ms p50/p95**.
- Separate `/v1/route` HTTP preview wall: **0.5796 / 0.7609 ms**.
- Product routing-only duration: **0.1135 / 0.1561 ms**, from the route endpoint's
  internal timer rounded to 0.001 ms.
- Product execution-internal duration: **0.8795 / 1.1256 ms**.
- Process RSS before warmup: **47,644,672 bytes**; OS process high-water RSS:
  **47,955,968 bytes**. These exclude the separate client/fixture process.
- Fresh process launch to the flushed listening message: **123.9 ms**, one sample.
  Installation, downloads and a first application request are excluded; filesystem
  caches are not flushed.

Client wall includes JSON encoding, bearer-authenticated HTTP, response reading
and JSON decoding; execution also includes the native adapter and fixture HTTP
round trip. Expected-answer grading happens after the timer stops. Internal
product timers exclude HTTP ingress, authentication and response serialization.
The routing-only measurement is a separate non-executing request, not a timing
component subtracted from the execution requests.

The 140 execution requests pass exact proposal and independent expected business-
field checks, including IDs, titles, state and transfer statistics. Fixture
traffic totals 165 GETs including warmups and zero writes. Six separately frozen
safety checks all pass with zero upstream calls: missing and invalid credentials
return 401, insufficient read scope returns 403, a transfer-write request
clarifies, an unissued confirmation is denied with 403, and an outside-grammar
request clarifies. Native-only mode reports writes disabled and no neural model
loaded. Write abstention and confirmation denial do not count as successful write
execution.

## Dependencies and provenance

The isolated environment contains Arrmes 1.2.0, jsonschema 4.26.0, attrs 26.1.0,
jsonschema-specifications 2025.9.1, referencing 0.37.0, rpds-py 2026.9.1,
typing_extensions 4.16.0 and pip 26.0. No MLX, PyTorch, Transformers, llama-cpp,
NumPy or pytest package is installed or loaded. These are actual process and
package observations; no token-rate or allocator placeholder is used.

The measured source is the immutable public commit
`44c16f4c94bdf6d11cd38c3dfd151ec7df86459f`. All 29 Python source hashes and the
harness fingerprint match before and after execution. The fixture is the existing
`tests/test_native_services.py` prefix before its first test function, with only
the optional pytest import removed. The original file and transformed prefix
hashes are recorded. The actual fixture handlers are unchanged.

Versioned evidence consists of:

- `scripts/benchmark_native_only_http.py`
- `data/overnight-v1/native-only-http-inputs.json`
- `experiments/overnight-v1/native-only-http.json`

The canonical raw receipt SHA-256 is
`3fe850838f7f19a1d164a8b45dd0eca8d5bfdc3872b5f336abea8ec704b90a0d`.
Any public privacy export records its transformed hash separately.

To reproduce, install that immutable source in a fresh core-only environment, pin
the dependency versions above when exact environment matching is required, and
run the harness from the repository containing the benchmark scripts:

```sh
python -m pip install -e CHECKOUT
python scripts/benchmark_native_only_http.py \
  --source-root CHECKOUT \
  --source-commit 44c16f4c94bdf6d11cd38c3dfd151ec7df86459f \
  --work-dir ./work/native-only-reproduction \
  --inputs ./work/native-only-reproduction-inputs.json \
  --output ./work/native-only-reproduction-results.json \
  --repetitions 10
```

The current efficiency harness uses macOS process measurement APIs. These results
do not establish Windows or Linux performance. No TLS, external network,
concurrent load, large-library workload or actual upstream service-version
compatibility is measured here.

## Separate actual Sonarr read check

A separate isolated run uses the actual **Sonarr 4.0.20.3014 Linux ARM64 binary**
in the local Docker Desktop VM and the Mac native-only Arrmes HTTP application.
It passes **8/8 checks**, including four explicit natural-language reads: health,
queue, library and history. Five audited API v3 GETs return 200. The library is
empty. This checks real application response handling; it does not qualify seeded
entities, missing-episode semantics, metadata search, writes or other services.

Transport is Arrmes to a loopback GET-only audit proxy, then `docker exec curl`
to Sonarr's container-local HTTP endpoint, forwarding unchanged JSON. It does not
qualify direct TCP connectivity from Arrmes to an upstream service. This small
check is not part of the 140-request efficiency measurement, and its timings are
not combined with that distribution. The initial host-port setup failure is
retained separately. Temporary container, internal network and environments were
removed; the pinned image remains cached for reproduction.

Evidence is `experiments/portable-v1/sonarr-upstream-readonly-smoke.json` and
`experiments/portable-v1/sonarr-upstream-bootstrap-attempt1.json`. The successful
receipt records its source identity, full source/harness hashes, actual version,
wire audit and cleanup. Reproduction requires local ARM64 Docker Desktop,
available resource headroom and Python 3.12+; the harness enforces a one-CPU,
768-MiB disposable Sonarr budget:

```sh
docker pull --platform linux/arm64 \
  lscr.io/linuxserver/sonarr@sha256:d3bf2056aa6439d54e1e673ae850b7fc70425b5b2b441414c4f480532f8fe8e0
python scripts/smoke_sonarr_upstream.py \
  --python python3.12 --source-root CURRENT_PUBLIC_CHECKOUT \
  --output ./work/sonarr-upstream-reproduction.json
```

## Preserving the evidence bundle

The owner-private ZIP contains original measurement metadata and historical source
cuts. Install the current reviewed public source or release separately for normal
use; the historical cuts can retain bugs that were fixed later. Raw receipts and
frozen inputs carry read-only ZIP permission bits where extractors support them.
These bits aid preservation and are not an integrity boundary; the manifest hashes
are the content check.

The archived private `rescore_benchmark_observations.py` is intentionally preserved
as historical code and **mutates its input receipt**. Use disposable copies for
diagnostics. Prefer the current public-source helper, which writes a separate
diagnostic copy and refuses overwrites. With that current helper, select an unused
output directory explicitly:

```sh
python scripts/rescore_benchmark_observations.py \
  DISPOSABLE_RECEIPT_COPY.json \
  --oracle DISPOSABLE_ORACLE_COPY.json \
  --output-dir ./work/new-observation-diagnostic
```

## What improved, and what did not

On the same frozen 80-case neural diagnostic, the published adapter with the
improved runtime remains **63/80 raw exact and 68/80 verified**, the same as the
released-source run. No new model training or neural accuracy gain is claimed.
The new native-only mode and native adapters add product behavior through a
separate explicit grammar and execution path.

The released-source fixture run has **28/35** strict business-observation matches;
its older exact-call/completed-terminal metric is **30/35**. The improved-source
run has **30/35** strict matches. The two released-source strict mismatches retain
a Tautulli fixture address that differed between original source and frozen
oracle (public export placeholders coincide). Those stable fields were not silently discarded. This fixture metadata
difference does not demonstrate improved neural selection or a two-workflow
product gain. Raw predictions, frozen labels and original timing values remain
preserved; the strict cached rescore and its omissions are recorded separately.

The [main report](benchmark-overnight-v1.html) contains the wider model, portable
backend, safety and workflow diagnostics. The native-only measurement supports a
lightweight explicit-read operating mode; comprehensive natural-language media
workflows still require the remaining qualification work described there.
