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
