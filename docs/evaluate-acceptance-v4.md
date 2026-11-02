# Frozen direct-model and proposal evaluation

This harness evaluates the corpus-audited diagnostic candidate in
`data/acceptance-v4`. Read its provenance and limitations before interpreting
scores. It is not a service-execution benchmark. The 1,000 distinct complete
inputs contain 965 query strings and correlated substitutions across 128
template families, 61 scenario groups and 193 non-global entity bundles. The
40 global inputs are reported separately. Semantic scenario separation and
the public adapter's complete training lineage remain unestablished.

`experiments/acceptance-v4/run-plan.json` fixes five sequential comparisons:
the released runtime with the actual public adapter, the frozen improved
runtime with identical weights, the same quantized base without an adapter,
the larger Qwen3-4B-Instruct-2507 reference, and the existing deterministic
router. Model revisions and complete artifact checksum inventories are pinned.
All receive the same tools, policy, cases and selected-media context. Native
chat templates can differ; prompt fingerprints record that difference.

Track A retains the native output and separately grades envelope syntax,
tool selection, schema validity, canonical argument/full-call exactness,
entity names and entity selectors. Tool selection can be correct even when
arguments are invalid. Malformed predictions remain in every full-call
denominator. A separate complete-JSON translation permits a single serialized
argument object; it does not search substrings, remove keys, repair arguments,
or feed translated calls to Track B.

The entity diagnostics check the expected name/selector slots, conditional on
the correct tool and a valid schema. Extra optional arguments are penalized by
canonical argument/full-call exactness; the partial expected-slot diagnostics
do not independently penalize extra selectors. Use full-call exactness for a
complete target-and-scope assessment.

Track B retains the unmodified runtime's processed output, then applies that
runtime's decoder and proposal verifier. A changed or rejected error is
separate from a corrected error. A preexisting wrong abstention is not a
verifier catch. Changes that damage correct predictions remain visible.
Neither an abstention nor a verifier correction improves a raw model score.
No executor, service adapter, confirmation redemption, workflow completion,
or unauthorized execution rate is measured. The 32 malformed-wire injections
are separate synthetic decoder checks, never model predictions.

A blind semantic review sealed before inference identified 24 defensible
alternative control labels. `control-sensitivity.json` records the alternatives
and original review digest. Primary scores always use the original frozen
gold. Only a separate appropriate-control sensitivity count uses these
alternatives; it is not a replacement exact-match or safety score.

Each comparison runs in a fresh Python process on the same hardware, one
model at a time, with greedy sampling and 64 output tokens. Two fixed
development warmups precede one pass over all 1,000 inputs in frozen order.
The initialization-plus-first-warmup measurement is not full process/CLI cold
startup, and filesystem caches are not evicted. Warm internal generation and
inclusive `infer` wall timings are separate. The latter includes prompt
formatting, tokenization, cache copying and the runtime's output processing.
Gold-dependent grading, the additional proposal boundary, prompt fingerprints,
summaries and journal writes are outside that timer. This is not HTTP latency.

OS process high-water RSS and MLX allocator statistics are separate. The latter
describe allocations in unified memory, not dedicated VRAM. Family, scenario
and entity macro accuracies and clustered bootstrap intervals use a fixed
2,000 resamples/seed 401; micro and cluster-macro intervals are labeled. These
intervals quantify this authored set's clustered variability, not an external
population or unmeasured hardware.

To prepare exact unmodified runtime trees from the repository:

```sh
mkdir -p work/runtime-released work/runtime-improved
git archive a536483a75ada2d4d26f7bc93ee0790695f6e697 src | tar -x -C work/runtime-released
git archive e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4 src | tar -x -C work/runtime-improved
PYTHONPATH=src python -m pytest -q tests/test_evaluate_acceptance_v4.py
```

Install the optional MLX dependencies on Apple Silicon and obtain the pinned
local models listed in the run plan. Set local `MODEL` and `ADAPTER` paths.
For the first comparison:

```sh
python scripts/evaluate_acceptance_v4.py \
  --source-root work/runtime-released/src \
  --runtime-revision a536483a75ada2d4d26f7bc93ee0790695f6e697 \
  --label released-public --backend mlx \
  --model "$MODEL" \
  --model-repository mlx-community/Qwen3-0.6B-4bit \
  --model-revision 73e3e38d981303bc594367cd910ea6eb48349da8 \
  --adapter "$ADAPTER" \
  --adapter-repository NorviTech/arrmes-qwen3-0.6b \
  --adapter-revision 8a9bcf224e6f9fd6bf238267093c70360ee837cb \
  --output experiments/acceptance-v4/released-public.json
```

Use the remaining run-plan labels and identities with the improved tree.
Every output and sibling `.raw.jsonl` is created exclusively; existing results
are never overwritten. The raw journal flushes every native output before any
gold-dependent work. Source, artifact, input, plan and harness digests are
checked again after inference. Local Hugging Face offline mode and a Python
socket guard block network access; no production endpoint is configured.
Preserve failed/partial results and document any run interruption. Do not
rerun or select outputs to improve scores, or tune against this freeze.
