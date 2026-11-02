# Arrmes: measured operating range and remaining gaps

The specialist improves substantially on its untuned base, but this work does not establish comprehensive or autonomously safe media-stack operation. In the new 1,000-input comparison, public Arrmes is **77.0% raw exact**, versus **39.6%** for its untuned base and **78.2%** for the stronger 4B reference. The frozen improved verifier is a regression: final exact calls fall from **717 to 678**, even as unexpected write proposals fall from **10 to 2**. All original outputs remain published, including losses.

This report is dated 2026-11-02. The five frozen comparisons evaluate the original release and pre-final source `e5e76b8`, not the later release candidate. The candidate repairs defects discovered during this investigation and has separate development tests. It has no new independent 1,000-case accuracy claim. No model was retrained, and no frozen gold label was changed.

## Comparison A: direct model selection and complete arguments

Every row uses the same 1,000 complete inputs, tool schemas, policy, contexts, first-pass order and full-call evaluation. The denominator includes malformed predictions. Tool-name correctness is separate from argument/schema correctness. Native syntax/schema counts use the Arrmes Hermes envelope; complete-JSON translation is scored separately and is never used to repair Track B.

- **Released Arrmes v1.1.0:** native tool name 835/1000; raw complete call 770/1000; separately translated 770/1000; proposal boundary 717/1000. Unexpected write proposals 51 → 10 of 728 non-write-gold cases. All incorrect write proposals, including wrong targets on write-gold tasks: 72 → 10.

- **Frozen improved source e5:** native tool name 835/1000; raw complete call 770/1000; separately translated 770/1000; proposal boundary 678/1000. Unexpected write proposals 51 → 2 of 728 non-write-gold cases. All incorrect write proposals, including wrong targets on write-gold tasks: 72 → 2.

- **Same Qwen3 0.6B, no adapter:** native tool name 479/1000; raw complete call 396/1000; separately translated 396/1000; proposal boundary 439/1000. Unexpected write proposals 46 → 2 of 728 non-write-gold cases. All incorrect write proposals, including wrong targets on write-gold tasks: 102 → 10.

- **Qwen3 4B Instruct 2507:** native tool name 833/1000; raw complete call 782/1000; separately translated 782/1000; proposal boundary 720/1000. Unexpected write proposals 8 → 0 of 728 non-write-gold cases. All incorrect write proposals, including wrong targets on write-gold tasks: 19 → 0.

- **Deterministic v2 baseline:** native tool name 281/1000; raw complete call 89/1000; separately translated 89/1000; proposal boundary 105/1000. Unexpected write proposals 0 → 0 of 728 non-write-gold cases. All incorrect write proposals, including wrong targets on write-gold tasks: 0 → 0.

Every score is a count out of 1,000 unless its denominator is explicitly 728. “Incorrect writes” includes both unexpected write tools and wrong arguments on tasks that did call for a write. These are proposals, not executed commands. None of these five runs contacted a media service, redeemed confirmation or measured unauthorized execution.

Raw strict appropriate clarification/refusal is 106/200 for Arrmes, 0/200 for the untuned base, 24/200 for 4B and 32/200 for deterministic rules. Released and frozen improved boundaries both score 68/200 strict controls. A blind review sealed before outputs identified 24 defensible alternative control labels; sensitivity reaches 76/200 for these two boundaries. Primary scores remain unchanged. A safe refusal with the wrong reason is still an exact-match error.

The specialist's tool-name score is 835/1000 versus 833/1000 for 4B; its full-call score is lower. Family-macro raw accuracy is 77.19% versus 78.65%. Scenario-macro raw accuracy reverses the comparison: 69.23% versus 62.89%. Thus no broad model-family or statistically established win is claimed. Fixed-seed 2,000-resample cluster intervals and per-family/entity metrics are in each raw receipt. They quantify this authored set, not an external workload population.

## Comparison B: what the verifier changed

Released verification corrects 42 raw errors but damages 95 originally correct predictions. Frozen improved verification corrects 43 but damages 135. It catches or changes 173 raw errors, leaving 57 unchanged; catching or changing an error is not the same as correcting it. The raw strings are byte-identical across both fine-tuned runs.

The 40 added damages are five correlated families: ordinary caller-self/request-class phrases were mistaken for identity overrides, and an explicit performer relationship was rejected. Other defects included rewriting validated control reasons/slots, treating positive “resuming” as a read, and reacting to a negated history term. The scope checker could erase a track selector when the model called that selector an album name. In a new isolated fixture, that could select an unrelated album after simulated confirmation. It never ran in production or in the frozen 1,000-case comparison.

The later candidate checks unquoted restrictions before masking model entities, preserves non-executing controls after hard unsafe/compound guards, binds known caller-self language, retains numeric episode restrictions and transfer prohibitions, and displays album titles with IDs during confirmation. Sixty-one fresh-entity regression tests cover positive and negative controls. Their observed failures and fixes are development evidence. See the [post-diagnostic investigation](postdiagnostic-verifier-analysis.md); these repairs do not replace the frozen scores.

## Provenance, independence and reproducibility

The corpus was frozen before inference with 1,000 unique query/context inputs, 965 distinct query strings, 128 template families, 61 scenario groups and 194 entity bundles. Forty inputs are global. The audited recorded training corpus contains 4,389 rows; measured full-input, exact/NFKC query, entity-name and normalized-template intersections are zero against it and ten prior input corpora. One historical workflow set lacks original queries, so only its retained proxy fields could be audited.

Semantic scenario separation and the public adapter's complete training lineage remain unproved. This is a **corpus-audited diagnostic**, not clean independent acceptance. It contains 728 non-write-gold cases, not 1,000 hard negatives. Substituted entities and paraphrases are correlated and do not become independent scenarios. The old historical sealed corpus shared wording templates with recorded training; its 310/314 verified reproduction is not a clean generalization result.

Four neural runs and one deterministic run were sequential fresh processes on one Apple M4 Pro, 48 GiB unified memory, macOS 27.0.1 ARM64, Python 3.12.13, MLX 0.32.4 and mlx-lm 0.31.1. Each neural run used greedy decoding, disabled thinking, 64 output tokens, two fixed development warmups and one prespecified pass. Context used each model's native configured limit rather than an imposed 4096-token cap. Revisions, quantization, prompt fingerprints, packages and artifact inventories are pinned in the [run plan](../experiments/acceptance-v4/run-plan.json) and receipts.

The 0.6B base is `mlx-community/Qwen3-0.6B-4bit` revision `73e3e38`; the actual public adapter is `NorviTech/arrmes-qwen3-0.6b` revision `8a9bcf2`. The reference is `mlx-community/Qwen3-4B-Instruct-2507-4bit` revision `50d4277`, selected for a practical stronger tool-capable comparison. Community conversion provenance and native template differences remain limitations. Full digests are in the plan. The raw journal flushes outputs before grading; 32 separate malformed-wire injections are synthetic decoder tests, not model predictions.

The [reproduction guide](evaluate-acceptance-v4.md) preserves the original harness and source-only, hash-validated runtime snapshots. Reproduction writes new result paths and refuses overwrite. No production endpoint is configured; all five recorded runs had zero outbound attempts. Do not train, tune or select reruns against these inputs.

## Routing latency, startup and memory

- **Released Arrmes v1.1.0:** inclusive routing p50/p95 318.987/406.006 ms; internal generation or rule work 315.858/402.934 ms; model initialization plus first development warmup 2.399 s; OS high-water RSS 724.6 MB; MLX peak allocator 2.220 GB.

- **Frozen improved source e5:** inclusive routing p50/p95 315.864/403.414 ms; internal generation or rule work 312.823/400.038 ms; model initialization plus first development warmup 1.908 s; OS high-water RSS 723.9 MB; MLX peak allocator 2.220 GB.

- **Same Qwen3 0.6B, no adapter:** inclusive routing p50/p95 261.860/335.674 ms; internal generation or rule work 258.694/332.667 ms; model initialization plus first development warmup 2.155 s; OS high-water RSS 719.0 MB; MLX peak allocator 1.669 GB.

- **Qwen3 4B Instruct 2507:** inclusive routing p50/p95 605.296/940.699 ms; internal generation or rule work 602.248/937.719 ms; model initialization plus first development warmup 5.004 s; OS high-water RSS 2651.5 MB; MLX peak allocator 3.752 GB.

- **Deterministic v2 baseline:** inclusive routing p50/p95 0.014/0.022 ms; internal generation or rule work 0.014/0.021 ms; model initialization plus first development warmup 0.000 s; OS high-water RSS 48.9 MB; MLX peak allocator unmeasured.

There are 1,000 warm timing samples per row, from one pass over 1,000 inputs. Repetitions are not hidden extra accuracy cases. Inclusive routing times include template rendering, tokenization, prefix-cache handling and runtime output processing; gold-dependent grading and the additional boundary are outside that timer. This is not complete HTTP request latency. The deterministic row measures rule work, not neural generation. Initialization plus one warmup excludes full process/CLI startup and does not evict filesystem caches.

The old 230–245 ms claim measured warm generation-core work. The new harder task mix has approximately 313 ms generation p50 and 316 ms inclusive routing p50 for the fine-tuned model. A separate earlier authenticated HTTP experiment measured 250.0/342.7 ms p50/p95 over 40 requests and eight read tasks; it has a different task mix and cannot be pooled with this run. TLS, remote network delay, load and human confirmation time remain unmeasured.

OS RSS and the MLX allocator are overlapping unified-memory counters and must not be added or labelled dedicated VRAM. The fine-tuned weight files total 345,563,976 bytes; complete base/adapter inventories including tokenizer files total 359,826,159 bytes. The portable Q4 GGUF is 396,704,352 bytes. Those sizes are not runtime RAM. The measured CPU/GPU backend, context and container overhead are documented separately.

## Comparison C: execution and actual service compatibility

The earlier identical 80-task controlled benchmark retains strict business observations: released source 28/35 versus v1.2.0 30/35 executable operations. The difference includes a fixture-IP/public-placeholder discrepancy, not a neural improvement. CPU/CUDA GGUF completes 32/35 in its own recorded run. None of these 35-case figures is the execution score for the fresh 1,000 cases.

Separate native writes have eight Seerr/Bazarr/Bitmagnet scenarios (initial system 5/8, final frozen cached replay 7/8) and four Arr indexer-search scenarios (actual-model 4/4 system, 3/4 raw exact). The first eight use five neural calls across three unique prompts; raw exact is 2/5. Explicit grammar routes are recorded separately. Commands are scoped, confirmed and replay-protected; “dispatched” never means files downloaded. These tiny mixes establish working increments, not comprehensive model accuracy.

Actual Sonarr 4.0.20.3014 ARM64 passes eight selected empty-library checks through a GET-only Docker bridge. Actual official Seerr 3.5.1 Linux AMD64 passes 25 identity/empty-read checks plus 22 harness tests. Initial failures and cleanup receipts remain retained. These probes do not qualify populated searches, provider work, request submission, direct TCP/TLS or every API version. Other service adapters remain fixture/source-qualified. The [capability matrix](capabilities.md) lists layer-specific operations, permissions and gaps.

## Other models, tools and platforms

The separate earlier 80-case study includes currently available Qwen3 1.7B, FunctionGemma 270M base, Granite 350M community Q4 and publisher BF16, internal Arrmes v6 and deterministic rules. Native/translated scores and failed publisher-format smokes remain separate. Its 160-token limit differs from this 64-token freeze. It cannot be pooled with or used to rank these 1,000-case results. The fine-tuned FunctionGemma artifact was not qualified. See the [earlier comparison](benchmark-comparison.md) for pinned primary sources and raw data.

Pyarr 6.9 is a relevant structured service-client baseline: 30 read probes returned 20 exact fixture results and ten per-series missing-filter mismatches; its measured client wall time excludes language routing. MediaStackMCP is a relevant qualitative tool-server comparison, not an executed benchmark or standalone local router. No unrelated product is assigned a fabricated accuracy score.

Actual inference ran on Apple Silicon MLX and macOS ARM64 CPU, Linux x86_64 CPU, Linux RTX 4070 Ti SUPER CUDA, and a Linux ARM64 Docker VM. Linux CPU Q4 routing p50/p95 was 1.133/1.705 s; CUDA 61.88/87.68 ms from 240 samples, with 1082 MiB sampled VRAM peak (a lower bound). Those are cross-hardware 80-case measurements, not fresh 1,000-case timings. Native Windows CPU compilation is CI-validated; Windows model generation is pending in this dated report. Windows CUDA, other ARM64 hosts, Metal GGUF, Vulkan and SYCL remain untested. Consult the [portable guide](portable-runtime.md) for installation and platform classifications.

## What remains before a comprehensive product claim

The highest-impact gaps are reliable clarification/entity/selector handling on genuinely independent tasks; populated/versioned upstream qualification; broader request lifecycle, subtitle-provider and service workflows; Windows model/accelerator qualification; and complete public training/artifact provenance. Confirmation and scope enforcement reduce damage but do not turn a wrong prediction into a correct one. Keep writes opt-in and show the exact target at confirmation. Current implemented native coverage is useful and testable; comprehensive portable operation is an ongoing qualification goal.
