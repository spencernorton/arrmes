# Frozen direct-tool candidate v4

This directory contains 1,000 new synthetic direct-tool cases authored before
any evaluated-model inference. It is a corpus-audited diagnostic candidate,
not proof of semantic scenario separation or established public-adapter lineage.
The directory name continues the historical acceptance-set version sequence;
the name alone does not qualify its statistical or provenance claims.

There are 128 authored template families, 61 scenario groups and 194 entity
bundles. All 1,000 query-plus-context inputs are distinct; they contain 965
distinct query strings. Thirty-five additional inputs reuse a reference
utterance with different selected-media context. The 120 entity-bearing
families each have eight substitutions, grouped in the same family. Eight
global-operation families each group five authored paraphrases. These are
correlated variants, not 1,000 independently authored scenarios.

The 20 v2 actions and original tool hash are preserved. Cases cover reads,
explicit writes, special episodes, title/year/artist/album binding, missing
slots, explicitly ambiguous entities, references, negation, unsupported scope,
independent compound requests and instruction-like entity text. Context cases
contain the supported selected-media snapshot; they do not claim a full
multi-turn conversation benchmark. Fictional entities are lookup strings, not
assertions that any real service has those records.

- cases.jsonl is the fixed Track A input and gold call corpus.
- families.json contains the authored structures, grouped wordings and gold rationales.
- entities.json records the invented names and deterministic substitution catalogue.
- case.schema.json validates record structure; the unchanged v2 schemas validate gold calls.
- wire-faults.jsonl separately contains 32 synthetic decoder failures across 16 fault families and both native formats. They are Track B fault injections, never model predictions.
- manifest.json binds inputs, authoring/audit code and provenance by SHA-256.
- provenance.json records authorship, license, source and training-corpus identity.
- audit.json records the local corpus comparison without copying local training text or paths.

Read the [method and limitations](../../docs/acceptance-v4.md) before evaluation.
This synthetic dataset is released under the repository [MIT license](../../LICENSE).
Never train, fine-tune, distill, select adapters or alter routing rules against
these cases. Preserve any future results even when Arrmes loses.
