# Native workflow integration evidence

The 1.3 workflows use the unchanged public Qwen adapter and v2 neural prompt.
New Bazarr tools and service-explicit request phrases use deterministic routing.
Neither native adapter additions nor verifier corrections are model training.
The [original comparisons](benchmark-comparison.md) remain the baseline.

## Actual model runs

The first eight integration scenarios passed **5/8 system outcomes**. Five neural
calls over **three unique prompts** produced **2/5 exact raw calls and 2/5 exact
verifier calls**. The model shortened the quoted title `Fixture movie` to
`Fixture` in two request-status scenarios and shortened the unquoted Bitmagnet
query `Fixture title` to `Fixture`. These are failures, including when an
unavailable-service error happened to match the expected terminal state. The
business oracle also requires the intended arguments, routing layer and mutation
behavior. A terminal state alone cannot pass a scenario.

The movie submission, ambiguity handling and three deterministic Seerr/Bazarr
workflows passed. The Bazarr workflows each confirm provider search, inspect a
server-issued candidate, then separately confirm its download. They remain
`dispatched` with `verified_complete=false`: matching subtitle metadata is not a
correlated provider job or proof of completed file download.

A separate four-prompt actual-model Arr run passed **4/4 system workflows**:
**3/4 raw exact and 4/4 verifier exact**. All four raw tool names were correct;
the verifier recovered the complete artist from a shortened quoted span. The
runtime executed the preserved model-selected tools through native controllers:

- Sonarr `EpisodeSearch` with eligible IDs `[1,7]`.
- Radarr `MoviesSearch` with one exact movie ID `[2]`.
- Lidarr `AlbumSearch` with exact album ID `[8]`.
- Lidarr artist-wide missing-album search with fixed IDs `[8,13]`.

Every mutation required an explicit single-use confirmation. There were zero
POSTs before confirmation, exactly one command/submission POST afterward, and
confirmation replay was denied. Bitmagnet's immutable GraphQL POST query is a
read, reported separately from write mutations. Arr command acceptance remains
`dispatched`; even a correlated completed search job does not prove media files.

The runs used an Apple M4 Pro, Darwin ARM64, MLX, temperature zero, a 64-token
generation bound, no warmup and an empty server-owned entity ledger per scenario.
Inputs and application/fixture/harness bytes were frozen **before model loading**.
The exact base is `mlx-community/Qwen3-0.6B-4bit` revision
`73e3e38d981303bc594367cd910ea6eb48349da8`; adapter is
`NorviTech/arrmes-qwen3-0.6b` revision
`8a9bcf224e6f9fd6bf238267093c70360ee837cb`. File hashes, package versions,
raw outputs, transitions, native HTTP calls and source snapshots are in
[the versioned receipts](../experiments/native-workflows-v1/).
Source JSONL stores exact UTF-8 line chunks in `source_lines`; concatenating them
reconstructs each original file and its recorded SHA-256. This avoids serialization
joining a newline escape to a Python decorator and misreading it as an address.
The source bytes reconstruct exactly. Public receipt metadata encodes checksum
maps as path/digest records and origin fingerprints with an explicit algorithm.
The [export ledger](receipt-export.json) links public container-byte hashes to
preserved owner-original receipts. Raw outputs, golds, decisions, timings and
numeric measurements are unchanged.

## Post-diagnostic development and replay

The title truncation exposed a general boundary defect. Development added
complete quoted-span conservation, distinct entity roles, compatible typed
context, typographic-negation handling and clarification for episode/track/disc/
quality restrictions absent from the tool schema. Independent adversarial
review also found duplicate Bazarr episode IDs and contradictory Arr command
names; targeted fixtures now reject them before consequential effects or before
claiming correlation, respectively.

The preserved original outputs replayed through the final verifier/runtime pass
**7/8 initial scenarios**, with **2/5 raw and 4/5 verifier exact calls** unchanged
in their separate roles. The unquoted Bitmagnet truncation still fails. The Arr
outputs replay **4/4 system outcomes**, with **3/4 raw and 4/4 verifier exact**.
No new generation occurred in either replay. These are development regressions
on inspected scenarios, **not fresh acceptance or improved raw model accuracy**.
The original failing receipts are never overwritten.

Independent review of the source candidate passed its 27 existing probes plus
three exact follow-up cases. Some probes repeat a failure class; they are not
30 independent language scenarios. This review and fixture suites do not
establish exhaustive adversarial safety.

## Reproduction

Use the test extra for loopback fixtures, and MLX only for actual generation:

```sh
python -m pip install -e '.[test,mlx]'
python scripts/benchmark_native_write_smoke.py \
  --model /absolute/path/pinned-base --adapter /absolute/path/pinned-adapter \
  --inputs data/native-workflows-v1/scenarios.json --output-dir work/new-eight-run
python scripts/benchmark_native_write_smoke.py \
  --model /absolute/path/pinned-base --adapter /absolute/path/pinned-adapter \
  --inputs data/native-arr-workflows-v1/scenarios.json --output-dir work/new-arr-run
```

For cached-output regression without a model or accelerator:

```sh
python -m pip install -e '.[test]'
python scripts/replay_native_write_smoke.py \
  --receipt experiments/native-workflows-v1/initial-eight.json \
  --output work/new-cached-eight.json
```

Output destinations must be new. Existing original receipts and input freezes
are refused rather than replaced. A newly run model may produce different raw
calls; retain every result rather than selecting the best repetition.

## Limits

These small authored integration sets derive from known development fixtures.
They are not scenario/entity/template-disjoint language acceptance. Their five
and four neural calls are too few for broad quality or safety-rate claims. Raw,
verifier and system denominators differ and must not be combined. Recorded wall
times are immediate-confirmation in-process fixture timings; they exclude real
services, TLS, concurrency and human delay and are not a new latency distribution.
Peak memory was not newly measured here. Real Bazarr/Seerr binaries, providers,
indexers, seeded upstream libraries and resulting files remain unqualified.
