## 2026-11-07 — v1.6.0: Unassisted MLX QLoRA v9, template-disjoint acceptance-v7, 11-service live container lab, and receipt-bound charts

- **Track 1 (Unassisted Generative MLX QLoRA v9 & Template-Disjoint `acceptance-v7`)**: Removed `router_assisted` from `LocalModel` (`src/arrmes/model.py`) so `LocalModel` (`mlx-lm`) strictly returns unmodified LLM decoding in `raw_unverified` (Track A) and `finalize_output` in `raw` (Track B); added explicit `HybridRouterMLXModel` (`hybrid-router-mlx`). Retired `acceptance-v6` to `TrainPool-v9` (`10,649` rows) and authored `acceptance-v7` (`2,580` cases across `516` clusters, `0` query, `0` entity, and `0` normalized template-skeleton overlap). `ArrmesNeuralRouterV3 v9` achieves `1,380/1,380` (`100.00%`, Wilson LB `99.72%`) on `v3` positives, `1,060/1,060` (`100.00%`, Wilson LB `99.64%`) on `v2` positives, and `1,200/1,200` (`100.00%`, `0` unauthorized writes) on hard negatives. Unassisted `Qwen3-0.6B v9` reaches `92.0%` Track A / `98.0%` Track B on `acceptance-v7` `v2` positives and `44.67%` Track A / `94.0%` Track B on hard negatives (`80.0%` / `90.0%` on `overnight-v1`); unassisted `FunctionGemma-270M v9` reaches `84.0%` Track A / `90.0%` Track B on `v2` positives and `32.67%` Track A / `88.67%` Track B on hard negatives.
- **Track 2 (11-Service Live Upstream Docker Container Lab)**: Extended `scripts/qualify_upstream_lab.py` (`experiments/upstream-lab-v1/summary.json`) to boot, seed, and verify all 11 upstream services (`73/73` checks passed across `Sonarr`, `Radarr`, `Lidarr`, `Readarr 0.4.19.2811` with confirmed `BookSearch`, `Whisparr 3.6.5.1886` `eros` v3, `Prowlarr`, `Bazarr`, `Tautulli`, `qBittorrent`, `Bitmagnet v0.10.1` + Postgres 16 GraphQL `search`, and `Seerr 3.5.1` with caller-owned `request_status` and confirmed `cancel_request`). Fixed Bitmagnet `v0.10.1` multi-column `orderBy` SQL builder error (`SQLSTATE 42703`) in `src/arrmes/services/bitmagnet.py` and Whisparr v3 `/api/v3/movie` fallback in `src/arrmes/services/native.py`.
- **Track 3 & Track 4 (Runtime Profile v9, Chart Integrity & Public Release)**: Profiled `v9` runtimes in `experiments/runtime-profile-v9.json` (`0.678 ms` warm p50 / `0.998 ms` warm p95 on M4 Pro for `ArrmesNeuralRouterV3 v9`), restored `docs/figures/arrmes_v7_*.png` from `c5844ca`, replaced hardcoded chart arrays with JSON-receipt-driven `scripts/generate_v9_charts.py` verified by `tests/test_chart_receipt_parity.py`, updated `docs/capabilities.md` and `docs/capabilities-after.json` for all 68 native pairs across 11 services, and published `v9` weights/adapters and `NorviTech/arrmes-bench-v7` to Hugging Face.

## 2026-11-06 — v1.5.0: Model convergence, *Arr ecosystem expansion, CatalogInvertedIndex, and upstream container qualification

- **Track 1 (Model Accuracy & Generative/Router Convergence)**: Retired `acceptance-v5` (`2,460` cases) to `data/dev-v6/`, built leakage-audited `TrainPool-v8` (`7,415` examples), and locked disjoint `acceptance-v6` (`2,580` cases across `516` clusters, `seed=8008`, `0` query and `0` entity overlap). Replaced quote-only `service_query` entity extraction with confidence- and margin-gated span extraction (`extract_unquoted_service_span`). `ArrmesNeuralRouterV3` reaches `1,260/1,260` (`100.00%`) on `dev-v6` Positive v3 and `1,380/1,380` (`100.00%`, Wilson LB `99.72%`, Bootstrap LB `100.00%`) on locked `acceptance-v6` Positive v3 plus `1,200/1,200` (`100.00%`, `0` damaged, `0` unauthorized writes) on Hard Negatives. Fixed the `Qwen3-0.6B` 4-bit span-copying drop (`53/100` in `v7`) via FP16 LoRA overlay on the 4-bit base (`82/100` unassisted raw on `dev-v6`, `81/100` on `acceptance-v6`, and `100/100` with router-assisted constrained decoding).
- **Track 2 (Speed & Large-Library Throughput)**: Added `CatalogInvertedIndex` (`src/arrmes/planner.py`, `src/arrmes/live_adapter.py`) indexing normalized `title`, `originalTitle`, and `alternateTitles`, cutting 5,000-movie Radarr title resolution from `26.23 ms` linear scan to `1.17 us` p50 (`22,495.7x` speedup). Warm `ArrmesNeuralRouterV3` + `verify_proposal` latency on Apple M4 Pro measures `0.597 ms` p50 / `0.885 ms` p95 (`7.52 MiB` weights).
- **Track 3 & Track 4 (Expanded Capabilities & Upstream Container Lab)**: Added native `prowlarr` (`api/v1`), `readarr` / Chaptarr (`api/v1`), and `whisparr` (`api/v3`) adapters, richer `*Arr` operations (`wanted`, `calendar`, `quality_profiles`, `indexers`, `monitored`), caller-bound Seerr TV series/season request creation and cancellation, bounded read-to-confirm workflows (`src/arrmes/workflows.py`), and Bitmagnet `content_type` filtering while keeping `arrmes.tools.v2` hash `e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8` invariant. Qualified against live official upstream Docker containers (`Sonarr v4`, `Radarr v5/v6` with 320 seeded multi-page movies, `Lidarr v2`, `Prowlarr v1`, `Bazarr 1.6+`, `Tautulli v2.18.2`, `qBittorrent v5.1.4`) on an isolated loopback bridge (`45/45` live checks passed; `93/93` HTTP fixture workflows passed).

## 2026-11-06 — Public repository navigation

Aligned the public README with the suite navigation and added direct links to the model page and GitHub releases.

## 2026-11-05 — v1.4.4: Seerr query encoding and Bazarr start time

- Released the first outside contribution (PR16, squash 969e670): native Seerr searches percent-encode the query, because Seerr's API validator rejects the `+` that `urlencode` uses for spaces, and the Bazarr subtitle adapter accepts the numeric `start_time` that Bazarr 1.6.2 reports. Multi-word Seerr searches and every Bazarr subtitle search failed before.
- Validation: 932 passed, 66 skipped locally on the PR head; reverting the three source files fails 112 tests in the touched suites. All four required checks passed on exact head 87cac4a. The macOS job was re-run once after it was cancelled without ever getting a runner. The contributor reports real Seerr 3.5.1 and Bazarr 1.6.2 runs; that is their evidence, not repeated here.
- Review follow-up: `_start_time` also requires a finite number. The JSON parser rejects literal `NaN`/`Infinity`, but `1e400` parses to `inf`; a new fixture test sends it raw and fails without the guard.
- Version 1.4.3 → 1.4.4. Backfilled the missing 1.4.3 changelog entry from PR11.

## 2026-11-03 — Bound CI status checks and superseded PR work

Cancel superseded pull-request runs by PR number; give non-PR and reusable workflow runs separate groups so they can finish independently. Validation: actionlint, workflow structure checks and the required native pull-request checks.

## 2026-11-03 — Front-page model and runtime comparisons

- Added two readable README charts generated directly from the recorded acceptance-v5 and runtime-profile JSON: raw versus verified proposals for five checkpoints on the common 200-case slice, and M4 Pro warm routing latency plus model files on disk for three local backends.
- Kept the generative slice separate from full-suite neural-router results. Chart labels retain quantization, sample counts, p50/p95, logarithmic latency scale and the proposal-versus-execution boundary. No new inference, training or benchmark rescoring.
- Prepared matching figures for the three Hugging Face model cards and the Arrmes organization-profile section; corrected existing model-card image paths to their actual figures directory. Publication and exact-head CI are verified separately.

## 2026-11-03 — Shorter README and usable setup instructions

- Reduced the README from 2,315 to 338 words: plain-language overview, three example requests, installation links and a compact benchmark summary. Removed repeated internals, charts and promotional claims from the front page; reports and evidence remain available in the guides.
- Put installation before performance evidence in the runtime guide. Replaced the README's training shortcut with a pinned router download and checksum; training inputs are no longer in Git.
- Core suite: 923 passed, 66 skipped without optional model/data inputs. Five neural-router checks and all three README examples pass with the documented download. Checked 33 local documentation links/anchors and Python compilation. No runtime behavior changed.

## 2026-11-02 — Arrmes v7 (`ArrmesNeuralRouterV3`), Protocol v3, General Verifier Repairs, and Locked `acceptance-v5`

- **Protocol v3 & Verifier Repairs**: Added `arrmes.tools.v3` (`V3_ACTIONS`, `contract_hash_v3`) while preserving `arrmes.tools.v2` hash `e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8`. Repaired general entity-agnostic grounding in `src/arrmes/planner.py`; on frozen `acceptance-v4` (`1,000` cases), Released Arrmes verified accuracy rose from `678/1000` (`e5e76b8`) and `795/1000` (`v1.3.1`) to `981/1000` (`0` damaged, `0` unauthorized writes).
- **Model Development & Training**: Built leakage-audited `TrainPool-v7` (`5,827` examples), trained `ArrmesNeuralRouterV3` (`models/arrmes-neural-router-v3/router_weights.npz`, `7.52 MiB`), and trained MLX LoRA v7 checkpoints for `FunctionGemma-270M` (`175.83 MiB` 4-bit) and `Qwen3-0.6B` (`330.86 MiB` 4-bit) (`experiments/mlx-lora-v7-provenance.json`).
- **Locked `acceptance-v5` (`seed=7007`, `2,460` cases across `492` clusters)**: Retired initial development splits into `data/dev-v5` and evaluated once on freshly locked `data/acceptance-v5` (`0` query and `0` entity overlap). `ArrmesNeuralRouterV3` achieved `1,060/1,060` (`100.00%`, Wilson LB `99.64%`, Bootstrap LB `100.00%`) on Positive v2, `1,257/1,260` (`99.76%`, Wilson LB `99.30%`, Bootstrap LB `99.44%`) on Positive v3, `1,200/1,200` (`100.00%`, Wilson LB `99.68%`, Bootstrap LB `100.00%`, `0` unauthorized writes) on Negative/Safety, and `52/52` (`100.00%`) on Track C end-to-end execution workflows.

## 2026-11-02 — pin evidence before executing its retained analyzer

- Independent review of the unpublished offline verifier caught a self-signed manifest/analyzer trust flaw. Pin both the known experiment manifest and original analyzer, authenticate bounded payload buffers once, then compile only those verified bytes. A relocated evidence directory cannot supply new code or win a read/execute race.
- Six independent regressions reject coordinated receipt/manifest/analyzer forgery, permit relocation of authentic evidence and prove a single authenticated snapshot. All13 Windows evidence checks pass after the fix. The actual original receipt/API projection/analysis remain exact; no new generation.
- Correct the Windows description to a bulk/wildcard refusal, rather than a clarification. Prior full984/1skip and publication313-file/52,828,382-byte zero-finding checks are retained; the final hardened candidate is validated separately before release.

## 2026-11-02 — qualify the previously omitted public FunctionGemma adapter

- A separately predeclared known80-input pair on original v1.1.0 source gives12→63 raw and23→68 verified calls for the same BF16 base without/with the exact public finalv5 adapter. All160 complete row trees remain unchanged. This closes an omitted measurement, not fresh acceptance or an added1,000-case run.
- Version the public-safe metadata projection, original digests, source/harness distinction and guarded offline verifier/reproducer. Seventeen meaningful checks cover identity/tamper/failure retention; no new training or weight distribution. Old gradient evidence binds an intermediate checkpoint, and old quantized exports arev3; neither qualifies publicv5.
- Current Gemma terms and missing distribution agreement/Notice files are explicit follow-up work. FunctionGemma GGUF conversion remains unqualified. The comparison now links the actual hosted Windows CPU proof and this separate diagnostic while retaining all frozen1,000-case results.

## 2026-11-02 — retain actual hosted Windows CPU qualification

- The exact published experimental C7 model passed the single hosted Windows Server2025 AMD64 workflow41436216278: four actual model calls, eight operational checks and13 independent identity/environment/source checks. Product source is pinned v1.2.0/e76, helper/workflow v1.2.1/b937. No broad model accuracy, newer-workflow inference, owned Windows hardware, NVIDIA or memory result is claimed.
- Version the original analysis/source manifest/API metadata and a lossless source-hash-record export. The offline verifier reconstructs the original CRLF receipt SHA and reproduces all13 checks; six artifact/run/source/device/dependency/cleanup mutants fail qualification. Seven new evidence tests pass.
- Add exact model download/checksum/license instructions and keep Windows CPU CI versus other hardware/accelerator classes explicit. Core runtime behavior and the neural contract are unchanged; this is a1.3.1 qualification/documentation increment.

## 2026-11-02 — isolate body deadlines from CI scheduling

- Public macOS CI at65b8e80 passed958 tests but one80ms slow-body assertion hit the separate finite socket timeout before its expected body-deadline branch. The failure is retained; no CI rerun or accepted two-error assertion hides it.
- Replace sleep-dependent timing with an actual loopback HTTP response and module-local elapsed-body clock. Require the precise504/deadline error after two real bytes, no third read/retry and a closed response; retain the independent real HTTP byte limit test.
- All58 focused Bitmagnet checks pass, and the new deadline case passes25 real-HTTP repetitions. Only tests change; runtime source, model predictions and all benchmark inputs/results remain unchanged. The new public head requires its own four gates.

## 2026-11-02 — complete integrated source and reproduction checks

- Combined source cb4f064 passes 959 tests with one optional export skip. Independent final review passes all61 new guard regressions; recorded-only native replays remain7/8 and4/4, retaining raw5/9 and verifier8/9 over nine neural rows. No fresh model inference or acceptance score.
- Complete tracked candidate publication scan covers289files/52,042,940bytes: zero unaccepted findings with the canonical merged baseline and real gitleaks8.24.2. The first synthetic-home-path failure remains in the owner audit.
- The original frozen harness and19/35-file licensed source snapshots reconstruct byte-exact runtime trees without old Git objects. Reproduction outputs now use fresh work paths rather than existing immutable results. Public PR3 remains subject to its own final exact-head checks.

## 2026-11-02 Published-ref reproduction metadata

Added exact source-byte resolution against published squash tags or licensed
source-only snapshots, preserving
the original inference harness verbatim and all five receipt/journal digests.
The optional resolver checks complete Git and local source inventories against
the original runtime vectors. It records the actual published commit and its
frozen byte-equivalent source identity separately. No ancestry is invented.

The post-freeze entry point also makes OS RSS imports optional for native
Windows integrity tests. Original inference, timed-call, grading and run-loop
logic is unchanged; explicit AST comparisons verify the limited metadata diff.
All 49 focused checks pass, including source tampering, wrong refs, extra
modules, manifest drift and byte-for-byte preservation of every original result.
No model inference or numeric rescoring accompanied this change. Documentation
uses exact licensed source snapshots and records both harness versions explicitly.
The 19 released and 35 improved Python files reconstruct byte for byte from
bounded text records, avoiding unavailable branch ancestry and compressed-byte
false positives. Snapshot extraction preserves existing paths and unrelated data.

## 2026-11-02 — retain relative scope and publish the losing comparison

- Independent review adds before/after episode subsets to the unrepresentable write scopes. Sixty-one new regression checks pass; the first 59-check result remains historical rather than being overwritten.
- The readable 1,000-case report leads with raw public770/base396/4B782 and the frozen verifier regression717→678. It separates proposal write errors, complete execution evidence, native format, partial entity diagnostics, clustered independence, startup/RAM and later development fixes.
- A fabricated private-home-path test value is now assembled from fragments, preserving its GGUF privacy-detector assertion without shipping a home-path literal. The first complete candidate publication scan caught this synthetic test and remains recorded. No real path, model, frozen input or result is scrubbed.

## 2026-11-02 — extend independent scope regressions before release

- Independent review caught residual universal-episode subranges, negated resume inflections and quoted identity clauses. Check numeric/ordinal scope before removing universal wording, retain all resume prohibitions, and reject any remaining AS/UNDER identity introducer after recognized caller-self phrases.
- Fifty-nine new regression checks cover positive commands and these fail-closed boundaries; the initial 46-check pass and subsequent eight/four failures are preserved as development history. All final targeted checks pass. No benchmark input, original raw prediction or frozen score changes.
- Frozen sources and raw receipts are integrated separately; a source-only reproduction snapshot will preserve e5 after the new candidate differs. A final full suite and exact-head public CI are required on the complete release candidate.

## 2026-11-02 Frozen five-comparison diagnostic results

Completed one prespecified pass over all 1,000 inputs for each comparison,
with fresh processes, two development warmups, unchanged 64-token greedy
settings and no network attempts. All native outputs and append-only journals
are retained. Original and improved runtimes produce identical raw public-model
outputs: 770 exact calls. The untuned base scores 396, the larger 4B reference
782, and the deterministic router 89. Full-JSON format translation adds no
exact calls in these runs.

Proposal-boundary exact calls are 717 released, 678 improved, 439 base, 720
reference and 105 deterministic. The stricter verifier reduces unexpected
write proposals from ten to two on 728 non-write inputs, while damaging more
correct raw predictions. This is a real accuracy regression, not evidence of
improved routing quality. No executor, service write, confirmation redemption
or unauthorized execution rate was measured. The blind control sensitivity
remains separate from strict original scores.

The recorded M4 Pro inclusive-infer p50/p95 is 316/403 ms for the improved
public model and 605/941 ms for the larger reference. These are warm inference
measurements, not HTTP latency or full CLI cold startup. RSS and MLX unified
allocator peaks are separate. Clustered macro scores and intervals remain in
the receipts. Corpus lineage, semantic holdout, malformed generation and
guardrail limitations remain explicit; no runtime or weight was tuned.

The 39 harness/frozen-input checks pass after making the source-identity unit
fixture independent of future product changes and shallow Git clones. The
publication scan reports no gitleaks findings, but a frozen synthetic title
collides with an estate-name rule; exact context evidence remains private
pending formal publication review. Frozen inputs and raw results are unchanged.

## 2026-11-02 Pre-inference direct-tool evaluation harness

Added a reproducible Track A direct-model and Track B proposal-boundary harness
for the frozen 1,000-input corpus-audited diagnostic candidate. Native syntax,
tool/argument/entity scores, full-JSON format translation, verifier correction
and decoder rejection are separate; no service execution or confirmation
qualification is claimed. A pre-output blind control review is preserved as
separate sensitivity metadata while all original gold labels remain unchanged.

The prespecified five-model/runtime comparisons share fixed order, tools,
policy, two development warmups, greedy 64-token generation and one pass.
Timing excludes gold-dependent grading; RSS and MLX allocator accounting are
separate. Source, model, input and harness checksums bind receipts. The harness
and frozen-input checks pass 38 tests, including the previously inspected
80-case native-score replay. No fresh acceptance output existed at this freeze;
model runs and results are subsequent evidence, without runtime tuning.

## 2026-11-02 — preserve frozen diagnostics and repair proposal scope checks

- The frozen 1,000-case comparison at runtime e5e76b8 reports 770 raw exact calls, 678 after verification versus released verification 717, and two remaining unexpected write proposals. Preserve all original inputs, raw outputs, scores and sources; post-output repairs and cached replays are development evidence, not fresh acceptance.
- Inspect unquoted scope before masking proposed entity text, so a track/quality/episode selector cannot become an unrelated album/title. Literal names containing selector words require explicit quoting. Album confirmations now include each selected title and ID. A new isolated wrong-album regression is blocked before preflight; no production calls.
- Preserve validated control calls after trusted unsafe/compound guards; recognize bounded caller-self/request-class phrases and explicit performer labels; keep negated request reads in the library and recognize positive resuming without converting a negated history reference into history. Narrow/conditional/foreign-identity writes still abstain.
- Forty-six independent post-diagnostic regressions pass. Integrated core validation: 910 passed, one optional export skip. No weight, training, benchmark-gold or recorded model-output changes. Independent review and final publication gates remain pending.

## 2026-11-02 Actual Seerr identity and empty-read qualification

Recorded actual official Seerr 3.5.1 Linux AMD64 evidence in the capability matrix
and workflow limitations: 25/25 bounded binary/read-policy checks and 22 Linux
harness tests. Mapped identity with database-created createdAt, administrator
fallback without the user header, authentication/owner-filter status codes and
the empty owned-request parser match the native adapter assumptions. The
explicit health phrase reaches the actual binary through runtime and executor.
Both initial failed receipts and exact source snapshots remain retained.

These checks use a GET-only Docker-exec bridge with synthetic accounts. They do
not qualify populated requests, metadata resolution, HTTP request POST,
submission/confirmation/lifecycle, external login or direct TCP/TLS. Added stable
primary source digests without replacing the historical develop audit. The
31 read pairs over eight APIs are unchanged; no model, benchmark input, score or
runtime source changed. Temporary test containers/processes/source/venv were
removed; only the pinned image cache remains. JSON/schema, registry/receipt and
local-link consistency checks cover this documentation follow-up.

## 2026-11-02 Final scope-conservation regressions

Adjacent independent probes also rejected unquoted artist/album role splitting,
word-number episode ranges and song selectors. Album searches now require an
explicit performer relationship; text inside the album cannot invent an artist.
172 focused adapter/runtime/grounding/instrument checks pass. The complete source
suite is running for the integrated 1.3.0 candidate; the version bump represents
new confirmed native workflows, not changed model weights or accuracy acceptance.

## 2026-11-02 Independent native-write safety review

Independent adversarial review found scope loss despite green fixture tests:
single episodes could widen to seasons, omitted albums to artist sets, and
movie/TV context could ground another entity role. The verifier now clarifies
selectors absent from the typed schema, checks compatible context roles before
and after tool corrections, normalizes typographic negation and conserves one
quoted entity per slot. Quoted literal It stays a literal instead of becoming
a different selected movie. 139 focused boundary/runtime/instrument tests pass.

Bazarr now rejects duplicate episode IDs and mixed-series pages before provider
effects, including fresh confirmation. Native Arr job evidence requires matching
resource and body names; unknown-release empty artist scopes clarify. Owned
adapter suites pass 99 Bazarr and 84 Arr checks. These fixes were reviewed and
tested with isolated fixtures; no production request was sent.

The actual-model native Arr integration smoke passed 4/4 system workflows:
3/4 raw exact and 4/4 verifier exact, across four unique prompts. All commands
were exact-ID, confirmed once, dispatched rather than credited as file completion,
and replay was denied. The earlier 5/8 smoke and separate 7/8 cached regression
remain preserved; no model-result replacement or fresh-acceptance claim.

## 2026-11-02 Exact native indexer searches and quoted entity conservation

Integrated the three trained Arr search actions through native explicit-ID
commands and namespaced confirmations. Search-job correlation stays separate
from media-download completion. Artist-only scope is bounded to known released,
monitored missing albums; partial album intent cannot widen into artist scope.
127 focused controller/runtime/instrument/grounding tests pass.

The first frozen actual-public-model integration smoke passed 5/8 system
scenarios. Five neural calls across three unique prompts produced 2/5 exact raw
calls; quoted title truncation and an unquoted search-query truncation remain
in the preserved receipt. Three explicit workflows passed without inference.
A general quoted-span conservation safeguard and conditional-write refusal
were added after this diagnostic; any replay is separately labelled and cannot
replace raw model results or become held-out acceptance. No weights were trained.

## 2026-11-02 Confirmed subtitle and caller-owned request integration

Integrated exact-target Bazarr provider search/candidate download and Seerr
caller-owned movie submission/status through runtime routing, scoped execution
and namespaced single-use confirmations. Native-only mode remains read-only by
default; a deliberate confirmed-write opt-in enables documented explicit phrases.
New neural vocabulary or model training is not claimed. Accepted subtitle jobs
stay dispatched because metadata alone cannot correlate a particular download.

Request status refuses qualifiers the preserved title-only schema cannot carry.
Artist-only music writes require explicit broad/missing scope rather than losing
an album selector. Duplicate upstream JSON keys now fail before target selection.
Focused native runtime/service checks pass 211 tests; 29 confirmation-cache and
write-grounding checks pass after retaining the existing explicit missing-music
phrase. Full-suite integration will be rerun with the remaining native Arr writes.
No production service or model artifact was modified.

## 2026-11-01 Native Bitmagnet reads

Added direct native Bitmagnet indexed metadata search and health, with two
immutable GraphQL read documents, scoped explicit quoted-query routing and a
bridge for the existing trained bitmagnet_search action. Plain unquoted searches
retain the neural v2 route; model-free mode clarifies outside its explicit grammar.
Pagination follows hasNextPage rather than an estimated count. Response limits,
nonadvancing/changing pages, malformed data, GraphQL partial errors, proxy auth and
redirects fail closed or remain partial. Returned magnets use validated hashes
without upstream tracker URLs. No mutation document or generic POST is accepted.

The full local suite passed 465 tests with one skip; 258 focused native/routing
checks also passed. Both documents validate against full primary SDL at stable
v0.10.0 and the audited v0.10.1-beta.1 revision (four schema checks, zero errors).
Those checks are separate from actual upstream application execution, which was
not tested here. The v2 tool hash, weights, frozen inputs and old receipts remain
unchanged. Configuration, capability matrix, primary digests and reproduction
commands are documented. Basic/SSO authentication and advanced Bitmagnet
management remain gaps; the optional API key is only for an operator's proxy.

## 2026-11-02 Portable model inspection and CPU generation smoke

Added a standard-library GGUF metadata/plaintext inspection helper and an
operational CPU model smoke with raw/verified calls, exact artifact/runtime
identity and fixture execution. The exact CPU-exported public-adapter Q4 file
passed eight checks across four documented examples on an Apple Silicon CPU;
one activity read executed against an isolated fixture. This is not new model
accuracy acceptance. The full core suite passed 412 tests with one optional
export test skipped.

This patch versions the application as 1.2.1 for qualification tooling only and
retains the original Mac operational receipt after checking it contains no
private paths or estate references. The manual workflow obtains its helper from
the workflow ref while separately pinning the tested product source to v1.2.0.

A manually dispatched Windows CPU workflow pins the official runtime ZIP,
model SHA and v1.2.0 product source without model-framework/GPU dependencies.
It has not executed; Windows compilation remains separate from generation.
The model's Apache license, source notices and artifact-specific privacy review
materials were prepared privately. The strict raw binary gate retains its
findings; no global exception was applied and no weights were uploaded.

## 2026-11-01 Final validation of the portable/native increment

Fresh confirmation catalogs fix a defect found in independent review; 16 new
regressions exercise real cache behavior, changed IDs/titles/years and replay.
The full local suite passed 406 tests with one optional export skip. A later
receipt-preservation regression and eight benchmark integrity checks also pass;
cached rescoring now writes a separate diagnostic copy and refuses overwrites.
All four required GitHub jobs passed on 56c1714, with final evidence additions
checked on their own head before merge.

A core-only process completed 140/140 explicit reads with six access/abstention
controls, 1.35/1.70 ms HTTP p50/p95 and 47,955,968-byte peak RSS. This restricted
loopback workload is separate from neural accuracy. Fresh package-main bootstrap
passes with optional inference imports blocked. An actual Sonarr 4.0.20.3014
ARM64 empty app also passes eight selected read/bootstrap checks through a
GET-only Docker bridge; direct TCP, seeded entities and writes remain unqualified.
All temporary test resources were removed. Public weights remain unchanged;
new operations use explicit grammar, and improved v2 raw accuracy remains 63/80.

## 2026-11-01 Native workflows and portable inference

Added 29 explicit native read pairs, six trained native media read bridges,
confirmed native transfer control, optional model-free reads and GGUF inference.
The original v1.1.0 release, weights and v2 neural contract remain preserved.
Reproducible comparisons separate model output, format translation, verifier,
business outcomes and timing boundaries. Frozen inputs are excluded from training.
The local suite passed 387 tests with one optional export skip; Windows/macOS/
Linux Python CI and pinned Windows CPU compilation passed on 44c16f4. Actual
Windows generation and live upstream service binaries remain unqualified.
Final evidence integration is validated on its own subsequent head.
