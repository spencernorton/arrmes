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
