---
license: mit
task_categories:
  - text-generation
language:
  - en
tags:
  - function-calling
  - tool-use
  - agent-benchmark
  - sandbox-verified
  - norvitech
size_categories:
  - 1K<n<10K
---

# Arrmes-Bench-v2: Sandbox-Execution-Verified Tool-Calling Corpus

**Arrmes-Bench-v2** describes synthetic tool-calling and distillation records for the `arrmes.tools.v2` vocabulary: 18 service-operation names and two control names across ten named integration domains. Candidate labels were checked against a resettable SQLite HTTP sandbox. This verifies fixture semantics, not real upstream service binaries, comprehensive live integrations or model-selected workflow completion.

![Arrmes v1.4 vs. Similar Open Models Accuracy Comparison](arrmes_v7_acceptance_v5_accuracy_bounds.png)

The figure is historical internal-experiment evidence. The overlap audit below supersedes the original card's “leak-free” and wholly disjoint acceptance claims.

## Splits and provenance

The accompanying source defines the following corpus records. Training-source counts and evaluation definitions do not by themselves prove which files are redistributed at a particular Hugging Face revision; pin and inspect the downloaded artifact's file inventory.

- **`distilled-train-v2.jsonl` (`N = 4,389`, 100-series cohort):** the regenerated training file matches its original manifest SHA-256, `59f4e8085cc096434a5b7395ce476fed06990c109ca5e232cfe774d285a4de73`. It combines teacher candidates, seeds and programmatic templates. The original card's 2,175-row count does not describe this audited training file.
- **`distilled-dev-v2.jsonl` (`N = 147`, 200-series cohort):** development validation data.
- **`regression-v2.jsonl` (`N = 85`):** adversarial, compound, ambiguous and read/write contrast diagnostics.
- **`sealed-acceptance-v2.jsonl` (`N = 139`, 300-series cohort):** historical qualification inputs, with 99 non-write opportunities.
- **`sealed-acceptance-v3.jsonl` (`N = 314`, 400-series cohort):** historical inputs, with **226**, not 229, non-write opportunities. Prior inspection and the overlap below prevent presenting this reused suite as fresh template-disjoint acceptance.
- **`workflows-v2.jsonl` (`N = 26`):** historical scripted dependent-proposal executor fixtures. Their completion score does not establish a neural model selecting all steps; the original 30-workflow count and model-workflow interpretation are not supported by the retained file.

## Training and evaluation overlap

The manifest-verified 4,389 training rows share **37 normalized templates with 261/314 sealed-v3 rows**. The historical `family_id` is primarily an entity/cohort identifier, so zero family overlap does not establish template disjointness. There are **zero identical full query-plus-context records**, while **34 query strings recur with different contexts**. Do not replace that distinction with a claim of zero exact-query overlap.

Reconstructing the recorded Qwen v6 refinement selection gives **649 rows including repeats** (438 distinct query strings); **229 sealed-v3 rows share templates with that selection**. The reconstruction uses hash-pinned source data and recorded selection logic. It does not prove that the currently published public adapter is identical to the preserved internal v6 checkpoint or establish every row's adapter lineage.

The later **80-case diagnostic** was frozen before its inference runs, with fictional entities absent from the retained release data. It has no exact-query overlap with the audited training data and one known normalized-template overlap (`ON1-023`). It remains an authored diagnostic, not a wholly scenario/entity/template-disjoint acceptance test. No new training used those 80 inputs.

## Interpreting model and workflow results

Raw native tool/argument accuracy, explicit format translation, verifier-processed accuracy and actual fixture execution are separate tracks. A guardrail correction or refusal must not count as a correct raw model prediction. Historical scripted workflows and later model-selected diagnostics are also distinct.

For the exact published Qwen adapter, the historical 314-case replay measured 296/314 raw exact (94.27%) and 310/314 after the original verifier (98.73%), with one unexpected write proposal surviving verification among 226 non-write opportunities. No replay write was executed. A later cached replay with a descriptor-grounding repair reaches 311/314 verified and zero unexpected proposals; raw accuracy is unchanged. That is a post-result code diagnostic, not fresh acceptance, new neural accuracy or a production safety guarantee.

See the [v1.2 source change set, artifact hashes and comparative evidence](https://github.com/spencernorton/arrmes/pull/2). Its expanded deterministic service routes and portable runtime do not imply that the unchanged released model learned new tools or that every named integration is complete.

## Licence

[MIT](https://opensource.org/licenses/MIT) © Spencer Norton. Part of the [NorviTech Suite](https://norvitech.com).

## Superseded by `NorviTech/arrmes-bench-v5` (`v1.4.0`)

For zero-leakage evaluation with `0` exact query overlaps and `0` entity overlaps (`1,260` positive scenarios across `252` clusters + `1,200` hard-negative/ambiguity/safety scenarios across `240` clusters and `35` families), use [`NorviTech/arrmes-bench-v5`](https://huggingface.co/datasets/NorviTech/arrmes-bench-v5) and see [`docs/benchmark-acceptance-v5.md`](https://github.com/spencernorton/arrmes/blob/v1.4.0/docs/benchmark-acceptance-v5.md).

![Frozen Acceptance-v4 Error Taxonomy and Verifier Repair Progression](arrmes_v7_error_taxonomy_and_verifier_repair.png)


## Visual Benchmark & Efficiency Comparisons (`v1.4.2`)

![Arrmes v1.4 vs. Similar Open Models Accuracy Comparison](arrmes_v7_acceptance_v5_accuracy_bounds.png)

![With vs. Without Arrmes: Claude Sonnet Task Completion Time, Token Usage, & Local RAM / VRAM Footprint](arrmes_v7_runtime_latency_memory_footprint.png)
