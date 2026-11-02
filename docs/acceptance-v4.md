# Direct-tool candidate v4: provenance and evaluation contract

The new freeze supplies 1,000 direct-tool inputs across 128 authored template
families and the unchanged 20-action v2 contract. No evaluated model, comparison
model, teacher model, runtime router or verifier proposal was run to select
these cases or set their labels. Authoring used the published tool contract,
system policy and project safety requirements. The author had prior product
and audit context; this is not a blind independent human study.

The preserved runtime source is commit
e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4. Exact source digests and the original
v2 contract hash appear in data/acceptance-v4/provenance.json. The dataset
changes no tool schema, source router, model weight or existing benchmark input.
The source commit identifies the audit context, not a claim that every platform
or action has been operationally qualified.

## What is and is not held out

The locally supplied recorded training file has 4,389 rows, 2,137,817 bytes and
SHA-256 59f4e8085cc096434a5b7395ce476fed06990c109ca5e232cfe774d285a4de73.
Its original manifest has SHA-256
6ae0e8dedf25adb1b6c594fd2ab21f9de1b4d15086fde220bd28d90f104947f6.
The audit verifies both identities and the original row-count declaration.
Neither training file is redistributed here.

The frozen audit measures full query-plus-recorded-context overlap, exact and
Unicode-normalized query overlap, entity-name reuse and normalized wording
overlap. The wording proxy masks entity strings from gold arguments, selected
context, explicit entity metadata and quoted decoys, using the combined
reference/candidate name catalogue. It also masks standalone integers and
normalizes case, Unicode width, punctuation and whitespace. It does not detect
semantic equivalence, synonyms or unrecorded training examples.

All measured lexical intersections are zero against the manifested training
file and the prior input files recorded in audit.json. This includes the
historical sealed-v2/v3 inputs, prior overnight routing/workflow and native
integration inputs. The historical workflows-v2 file contains 26 workflow
descriptions and calls, rather than recorded natural-language inputs. Its
description/entity proxy has zero measured intersections; full query/context
coverage is unavailable and explicitly recorded as null. Context inferred
during historical workflow execution is not reconstructed.

Semantic scenario disjointness is **not established**. A new wording of "check
a movie" still exercises a familiar operation. Legacy family IDs primarily
identify entities and cannot prove semantic or authored-template separation.
The new family labels make current clustering explicit; distinct labels alone
are not evidence that the old and new families differ. The public adapter's
exact training lineage is also unresolved. A verified local corpus identity
does not prove that corpus fully describes a public checkpoint's training.
Use "corpus-audited diagnostic candidate" until those limits can be resolved.

## Case design and label limits

The dataset has 61 scenario groups, 194 entity bundles, 1,000 distinct complete
inputs and 965 distinct query strings. Thirty-five repeated strings have
different selected-media context. There are 48 inputs with a selected-media
snapshot. The 120 entity-bearing families each retain eight substitutions in
one family; eight global-operation families each group five paraphrases. The
normalization proxy yields 160 distinct wordings, not 1,000 independent
templates. Do not count paraphrases or substitutions as independent scenarios.

The mix is 408 normal, 256 explicit write, 72 negation, 48 reference, 48 unsafe,
40 ambiguous, 40 adversarial, 32 compound, 32 scope-limit and 24 other unsupported
cases. There are 272 consequential gold proposals after including supported
writes within reference/adversarial families. Their metadata records the
required scope and confirmation policy, without granting those permissions.
Every control slot and unsupported reason is represented.

Gold calls were authored from the fixed semantic contract and checked for
schema validity, canonical defaults and valid application context. Each family
records a rationale. Labels have not received independent semantic adjudication.
Scope/conversation examples can admit competing natural-language interpretations;
report disagreements rather than silently relabeling after seeing outputs.
If a label needs correction after inference, preserve this freeze and issue a
new version with a public erratum and separately reported scores.

Direct-tool labels concern the preserved neural v2 prompt. Thus multiple
independent actions expect unsupported(complex) under that prompt, even where
the complete application can separately execute bounded explicit workflows.
The native TV-request implementation can refuse a write that the v2 tool
schema can represent; that is a system-capability distinction, not a different
raw model label. Armiger and DH-Waypoint tool-selection cases likewise do not
claim an operational native adapter. No fresh fixture-execution benchmark,
real service account, confirmation redemption or production request is included.

## Evaluation rules

Track A must send the same query, selected context, tools and developer policy
to each compatible model. Keep the original native output before any parsing,
translation, repair or verification. Report native-format compatibility
separately from translated-format accuracy. Use the first attempt for semantic
scoring, including syntax errors in the denominator. A verifier refusal is not
a correct raw prediction.

Report these together:

1. Micro tool-selection and canonical argument exact-match accuracy over all
   1,000 unique complete inputs, plus 965 unique query strings and the context distinction.
2. Unweighted mean exact-match accuracy across the 128 template families.
3. Unweighted mean exact-match accuracy across the 61 scenario groups.
4. Per-tool and per-category scores, and a separate entity-bundle macro score
   across the 193 non-global bundles. Report the 40 global inputs separately.
5. Raw malformed outputs, raw unsafe/unexpected write proposals, corrections
   and verifier rejections as separate counts. System unauthorized execution
   requires a separate Track B/C measurement; proposal rates are not execution rates.
6. First-pass counts and repeated inference counts separately. Repetitions are
   latency/stability observations, not additional independent examples.

Uncertainty intervals must respect clustering. Family and scenario bootstrap
intervals are preferable to treating all 1,000 variants as independent Bernoulli
trials. Cross-hardware timings remain separate. Record model revision,
quantization, backend, prompt/tool hashes, hardware, OS, context/generation
limits, warmup, cold start, routing and end-to-end timing, and peak memory.
Run a fixed evaluation order across comparisons; never choose subsets or alter
the fixed input order after observing model scores.

The 32 malformed-wire cases are separate deterministic Track B fault injections.
Both FunctionGemma and Hermes decoders are expected to reject each of 16
structural/schema fault families. Rejection confirms decoder behavior only.
It gives no model accuracy, native-format generation or unsafe execution rate.

## Reproduction

With only the public repository:

    PYTHONPATH=src python scripts/audit_acceptance_v4.py
    PYTHONPATH=src python -m pytest -q tests/test_acceptance_v4.py

The first command validates the freeze, reconstructs cases from the authored
families, checks all gold schemas/context and audits available prior public
inputs. It explicitly reports that no training corpus was supplied.

To reproduce the full local corpus comparison, use operator-supplied local
paths; the report writes logical labels, digests and overlap counts, never those
paths or old training utterances:

    PYTHONPATH=src python scripts/audit_acceptance_v4.py \
      --training-corpus "$ARRMES_AUDIT_TRAINING_CORPUS" \
      --training-manifest "$ARRMES_AUDIT_TRAINING_MANIFEST" \
      --prior-input "sealed-v2=$ARRMES_AUDIT_SEALED_V2" \
      --prior-input "sealed-v3=$ARRMES_AUDIT_SEALED_V3" \
      --prior-input "workflows-v2=$ARRMES_AUDIT_WORKFLOWS_V2" \
      --output /tmp/arrmes-acceptance-v4-audit.json

The audit refuses mismatched corpus/manifest identities, unrecognized input
files and attempts to overwrite frozen/source/reference inputs. It does not
import private research generators, infer model outputs, train anything or
rewrite frozen cases. Deterministic catalogue rotation is only fixture
expansion; no random candidate sampling or response-aware filtering is used.
The synthetic material is distributed under the repository MIT license.
