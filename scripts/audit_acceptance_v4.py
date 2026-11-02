#!/usr/bin/env python3
"""Validate a pre-inference freeze and audit local input corpora without inference.

Private training files are read locally and never copied into this report.
Template overlap is a lexical proxy; it is not semantic-scenario separation or
proof of which examples trained a published adapter.
"""
from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jsonschema import Draft202012Validator
from arrmes.catalog import V2_BY_NAME, contract_hash_v2
from arrmes.model import messages
from arrmes.protocol import decode, InvalidCall, normalized, validate_call

DATA = ROOT / "data" / "acceptance-v4"
TRAIN_SHA256 = "59f4e8085cc096434a5b7395ce476fed06990c109ca5e232cfe774d285a4de73"
TRAIN_MANIFEST_SHA256 = "6ae0e8dedf25adb1b6c594fd2ab21f9de1b4d15086fde220bd28d90f104947f6"
SEMANTIC_SLOTS = frozenset({"title", "artist", "album", "query", "release_name"})
PRIOR_INPUTS = (
    "data/overnight-v1/routing-heldout.jsonl",
    "data/overnight-v1/prefreeze-draft.jsonl",
    "data/overnight-v1/workflows.jsonl",
    "data/overnight-v1/native-media-smoke-inputs.json",
    "data/overnight-v1/native-only-http-inputs.json",
    "data/native-workflows-v1/scenarios.json",
    "data/native-arr-workflows-v1/scenarios.json",
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _render(value: Any, bindings: dict) -> Any:
    if isinstance(value, str):
        if re.fullmatch(r"\{[a-z_]+\}", value):
            return bindings[value[1:-1]]
        return value.format_map(bindings)
    if isinstance(value, list):
        return [_render(v, bindings) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, bindings) for k, v in value.items()}
    return value


def expand_families(families: list[dict], pools: dict) -> list[dict]:
    """Deterministic transcription of authored templates; no RNG or selection."""
    rows = []
    entity_slots = {"title": "title", "other_title": "title", "artist": "artist",
                    "album": "album", "torrent_title": "torrent", "search_text": "search",
                    "release_name": "release", "slug": "search"}
    for family_index, family in enumerate(families):
        kind = family["entity_kind"]
        for variant in range(family["variants"]):
            # Rotation distributes the fixed catalogue without treating entity
            # substitutions as independent authored templates.
            offset = (family_index * 5 + variant) % 16
            title_pool = pools["tv"] if kind.endswith("tv") else pools["movie"]
            title = (pools["adversarial"][variant] if kind.startswith("adversarial_")
                     else title_pool[offset])
            artist = (pools["adversarial"][variant] if kind == "adversarial_music"
                      else pools["artist"][offset])
            year = pools["years"][offset]
            bindings = {
                "title": title, "other_title": title_pool[(offset + 7) % 16],
                "year": year, "other_year": pools["years"][(offset + 7) % 16],
                "artist": artist, "album": pools["album"][(offset + 3) % 16],
                "season": variant % 6 + 1, "other_season": variant % 6 + 2,
                "episode": variant + 2, "slug": title.casefold().replace(" ", "-"),
                "search_text": title + " 1080p",
                "release_name": title.replace(" ", ".") + f".{year}.1080p.WEB-DL.FableGroup.mkv",
                "torrent_title": title.replace(" ", ".") + f".{year}.1080p.FableGroup",
            }
            query_template = family["queries"][variant if kind == "none" else 0]
            expected_template = (family["expected"][variant] if kind == "none" else family["expected"])
            expected = _render(expected_template, bindings)
            context = _render(family["context"], bindings)
            query = _render(query_template, bindings)
            source = canonical([query_template, expected_template, family["context"]])
            used = set(re.findall(r"\{([a-z_]+)\}", source))
            entities = sorted(
                [{"kind": entity_slots[key], "name": bindings[key]} for key in used & entity_slots.keys()],
                key=lambda entity: (entity["kind"], entity["name"]),
            )
            entity_family = ("av4:global" if not entities else
                             "av4:entities:" + hashlib.sha256(canonical(entities).encode()).hexdigest()[:16])
            name = expected["name"]
            action = V2_BY_NAME[name]
            rows.append({
                "id": f"AV4-{len(rows) + 1:04d}",
                "query": query, "context": context, "expected": expected,
                "family_id": family["template_family"], "template_family": family["template_family"],
                "scenario_id": family["scenario_family"], "entity_family": entity_family,
                "variant_index": variant, "category": family["category"],
                "entities": entities,
                "track": "A_direct_tool", "contract": "arrmes.tools.v2",
                "execution_policy": {"effect": action.effect, "permission": action.permission,
                                     "confirmation_required": action.effect in {"write", "request", "download"}},
                "gold_rationale": family["rationale"],
                "provenance": "pre-inference authored family; deterministic entity expansion; never training data",
            })
    return rows


def validate_cases(rows: list[dict], schema: dict) -> dict:
    validator = Draft202012Validator(schema)
    seen_ids, seen_inputs = set(), set()
    for row in rows:
        if list(validator.iter_errors(row)):
            raise ValueError("case schema mismatch")
        if row["id"] in seen_ids:
            raise ValueError("duplicate case ID")
        seen_ids.add(row["id"])
        signature = full_input(row)
        if signature in seen_inputs:
            raise ValueError("duplicate query-plus-context input")
        seen_inputs.add(signature)
        call = validate_call(row["expected"]["name"], row["expected"]["arguments"], allow_v3=False)
        if normalized(call) != row["expected"]:
            raise ValueError("gold must include canonical default arguments")
        # Construct and validate messages only. No backend is constructed or run.
        messages(row["query"], row["context"], "hermes", contract_version="v2")
        if row["family_id"] != row["template_family"]:
            raise ValueError("scoring family must be the authored template family")
        action = V2_BY_NAME[call.name]
        expected_policy = {"effect": action.effect, "permission": action.permission,
                           "confirmation_required": action.effect in {"write", "request", "download"}}
        if row["execution_policy"] != expected_policy:
            raise ValueError("execution policy differs from fixed tool contract")
    return {
        "cases": len(rows), "unique_inputs": len(seen_inputs),
        "unique_queries": len({row["query"] for row in rows}),
        "template_families": len({row["template_family"] for row in rows}),
        "scenario_families": len({row["scenario_id"] for row in rows}),
        "entity_families": len({row["entity_family"] for row in rows}),
        "tool_counts": dict(sorted(Counter(row["expected"]["name"] for row in rows).items())),
        "category_counts": dict(sorted(Counter(row["category"] for row in rows).items())),
        "family_sizes": dict(sorted(Counter(row["template_family"] for row in rows).items())),
        "context_rows": sum(bool(row["context"]) for row in rows),
        "confirmation_required_proposals": sum(row["execution_policy"]["confirmation_required"] for row in rows),
        "repeat_inference_samples": 0,
    }


def full_input(row: dict) -> str:
    # Missing historical context and an explicit empty object mean the same input.
    return canonical({"query": row["query"], "context": row.get("context") or {}})


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def entity_names(row: dict) -> set[str]:
    names = {entity["name"] for entity in row.get("entities", [])}
    expected = row.get("expected", {})
    if isinstance(expected, dict):
        names.update(v for k, v in expected.get("arguments", {}).items()
                     if k in SEMANTIC_SLOTS and isinstance(v, str) and v.strip())
    selected = (row.get("context") or {}).get("selected_media")
    if isinstance(selected, dict) and isinstance(selected.get("title"), str):
        names.add(selected["title"])
    # Quoted decoys and ambiguous alternatives are not always in gold arguments.
    names.update(re.findall(r'"([^"\n]{1,512})"', row["query"]))
    return names


@lru_cache(maxsize=8)
def _entity_pattern(literals: frozenset[str]):
    choices = [re.escape(item) for item in sorted(literals, key=lambda item: (-len(item), item)) if item]
    return re.compile(r"(?<!\w)(?:" + "|".join(choices) + r")(?!\w)") if choices else None


def normalized_template(row: dict, lexicon: set[str] | None = None) -> str:
    query = _fold(row["query"])
    literals = frozenset({_fold(name) for name in entity_names(row)} | (lexicon or set()))
    pattern = _entity_pattern(literals)
    if pattern is not None:
        query = pattern.sub(" <entity> ", query)
    query = re.sub(r"(?<!\w)\d{1,4}(?!\w)", " <number> ", query)
    query = re.sub(r"[^\w<>]+", " ", query)
    return re.sub(r"\s+", " ", query).strip()


def _contains_entity(text: str, literal: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", text) is not None


def audit_overlap(cases: list[dict], reference: list[dict]) -> dict:
    candidate_names = {_fold(name) for row in cases for name in entity_names(row)}
    reference_names = {_fold(name) for row in reference for name in entity_names(row)}
    lexicon = candidate_names | reference_names
    recorded_queries = [row for row in reference if row.get("audit_input_kind") != "workflow_description_proxy"]
    ref_full = {full_input(row) for row in recorded_queries}
    ref_queries = {row["query"] for row in recorded_queries}
    ref_folded = {_fold(row["query"]) for row in recorded_queries}
    ref_templates = {normalized_template(row, lexicon) for row in reference}
    templates = {row["id"]: normalized_template(row, lexicon) for row in cases}
    exact_ids = [row["id"] for row in cases if full_input(row) in ref_full]
    query_ids = [row["id"] for row in cases if row["query"] in ref_queries]
    folded_ids = [row["id"] for row in cases if _fold(row["query"]) in ref_folded]
    overlap_ids = [row["id"] for row in cases if templates[row["id"]] in ref_templates]
    # Also find names present in historical utterances but absent from gold.
    ref_text = "\n".join(_fold(row["query"]) + "\n" + _fold(canonical(row.get("context", {}))) for row in reference)
    occurrence_names = sorted(name for name in candidate_names if _contains_entity(ref_text, name))
    return {
        "reference_rows": len(reference), "reference_unique_recorded_inputs": len(ref_full),
        "reference_recorded_query_rows": len(recorded_queries),
        "reference_workflow_description_proxy_rows": len(reference) - len(recorded_queries),
        "reference_context_rows": sum(bool(row.get("context")) for row in reference),
        "full_query_context_audit_applicable": bool(recorded_queries),
        "full_query_context_overlap_count": len(exact_ids) if recorded_queries else None,
        "full_query_context_overlap_case_ids": exact_ids,
        "exact_query_overlap_count": len(query_ids) if recorded_queries else None, "exact_query_overlap_case_ids": query_ids,
        "nfkc_casefold_query_overlap_count": len(folded_ids) if recorded_queries else None,
        "nfkc_casefold_query_overlap_case_ids": folded_ids,
        "entity_name_exact_overlap_count": len(candidate_names & reference_names),
        "entity_name_exact_overlaps": sorted(candidate_names & reference_names),
        "candidate_entities_occurring_in_reference_input_count": len(occurrence_names),
        "candidate_entities_occurring_in_reference_inputs": occurrence_names,
        "normalized_template_overlap_case_count": len(overlap_ids),
        "normalized_template_overlap_case_ids": overlap_ids,
        "normalized_template_overlap_family_count": len({row["template_family"] for row in cases if row["id"] in overlap_ids}),
        "normalized_template_intersection_count": len(set(templates.values()) & ref_templates),
        "candidate_distinct_normalized_templates": len(set(templates.values())),
        "semantic_scenario_disjointness": "not_established",
        "template_family_identity_disjointness": "not_established_from_legacy_entity_family_ids",
    }


def _walk_inputs(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("query"), str) and (
            "expected" in value or "context" in value or "id" in value
        ):
            yield value
            # The parent carries expected arguments; do not mistake an argument
            # named query for another evaluation case.
            children = [value[k] for k in ("steps", "turns", "cases") if k in value]
        elif isinstance(value.get("description"), str) and isinstance(value.get("calls"), list):
            # Historical workflow fixtures have calls/descriptions, not recorded
            # user utterances. Audit their entities and description templates
            # without pretending to have their missing query/context inputs.
            entities = [{"kind": "title", "name": argument}
                        for call in value["calls"] if isinstance(call, dict)
                        for slot, argument in call.get("arguments", {}).items()
                        if slot in SEMANTIC_SLOTS and isinstance(argument, str)]
            yield {"query": value["description"], "context": {}, "entities": entities,
                   "audit_input_kind": "workflow_description_proxy"}
            children = []
        else:
            children = value.values()
        for child in children:
            yield from _walk_inputs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_inputs(child)


def read_input_rows(path: Path) -> list[dict]:
    values = read_jsonl(path) if path.suffix == ".jsonl" else [read_json(path)]
    return list(_walk_inputs(values))


def verify_wire_cases(rows: list[dict]) -> dict:
    for row in rows:
        if row["expected"] != "reject" or row["track"] != "B_decoder_fault_injection":
            raise ValueError("invalid wire-fault expectation")
        try:
            decode(row["raw"], row["format"])
        except InvalidCall:
            continue
        raise ValueError("fault-injected wire case unexpectedly schema-valid")
    return {"cases": len(rows), "fault_families": len({r["family_id"] for r in rows}),
            "all_expected_rejected": True, "model_generated_outputs": 0}


def verify_freeze(data_dir: Path = DATA) -> tuple[list[dict], dict]:
    manifest = read_json(data_dir / "manifest.json")
    for logical_path, expected in manifest["files"].items():
        path = ROOT / logical_path
        if not path.is_file() or sha256(path) != expected["sha256"]:
            raise ValueError("freeze artifact hash mismatch")
    if manifest["contract_sha256"] != contract_hash_v2():
        raise ValueError("released neural contract changed")
    families = read_json(data_dir / "families.json")["families"]
    cases = read_jsonl(data_dir / "cases.jsonl")
    if cases != expand_families(families, read_json(data_dir / "entities.json")):
        raise ValueError("frozen cases differ from authored families")
    summary = validate_cases(cases, read_json(data_dir / "case.schema.json"))
    if summary != manifest["case_summary"]:
        raise ValueError("freeze summary mismatch")
    summary = {**summary, "wire_faults": verify_wire_cases(read_jsonl(data_dir / "wire-faults.jsonl"))}
    return cases, summary


def audit_corpus(path: Path, cases: list[dict], *, manifest_path: Path | None = None,
                 require_original: bool = False) -> dict:
    digest = sha256(path)
    rows = read_input_rows(path)
    if not rows:
        raise ValueError("reference file has no recognized inputs")
    if require_original:
        if digest != TRAIN_SHA256 or len(rows) != 4389:
            raise ValueError("recorded training corpus identity mismatch")
        if manifest_path is None or sha256(manifest_path) != TRAIN_MANIFEST_SHA256:
            raise ValueError("recorded training manifest identity mismatch")
        declaration = read_json(manifest_path)["files"]["distilled-train-v2.jsonl"]
        if declaration["sha256"] != digest or declaration["count"] != len(rows):
            raise ValueError("training manifest declaration mismatch")
    return {"sha256": digest, "bytes": path.stat().st_size,
            "manifest_sha256": sha256(manifest_path) if manifest_path else None,
            "overlap": audit_overlap(cases, rows)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-corpus", type=Path)
    parser.add_argument("--training-manifest", type=Path)
    parser.add_argument("--prior-input", action="append", default=[], metavar="LABEL=FILE",
                        help="Additional local prior inputs; labels, not local paths, are written to the report")
    parser.add_argument("--output", type=Path, help="Optional new audit JSON; frozen inputs are never rewritten")
    args = parser.parse_args(argv)
    try:
        cases, summary = verify_freeze()
        report = {"schema_version": 1, "scope": "pre-inference input audit; no evaluated model or router called",
                  "freeze_manifest_sha256": sha256(DATA / "manifest.json"), "case_summary": summary,
                  "training_corpus": None, "prior_inputs": {}, "missing_default_prior_inputs": [],
                  "qualification": "corpus-audited diagnostic candidate; semantic scenario disjointness and public adapter lineage unverified",
                  "adapter_lineage": "The recorded corpus is manifested; its use by the public adapter is not proven.",
                  "normalization": "NFKC/casefold; mask declared/gold/context/quoted entity strings from both sets; mask standalone integers; collapse punctuation/spacing. No synonym or semantic matching.",
                  "workflow_context_limit": "Full-input audit uses recorded context only; unrecorded state derived during workflows is not reconstructed."}
        if args.training_corpus:
            report["training_corpus"] = audit_corpus(args.training_corpus, cases,
                manifest_path=args.training_manifest, require_original=True)
        for logical_path in PRIOR_INPUTS:
            path = ROOT / logical_path
            if path.is_file():
                report["prior_inputs"][logical_path] = audit_corpus(path, cases)
            else:
                report["missing_default_prior_inputs"].append(logical_path)
        for item in args.prior_input:
            if "=" not in item:
                raise ValueError("prior input must use LABEL=FILE")
            label, path_text = item.split("=", 1)
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", label) or label in report["prior_inputs"]:
                raise ValueError("invalid or duplicate prior-input label")
            report["prior_inputs"][label] = audit_corpus(Path(path_text), cases)
        if args.output:
            protected = {DATA / "manifest.json", *(ROOT / name for name in read_json(DATA / "manifest.json")["files"])}
            protected.update(ROOT / name for name in PRIOR_INPUTS)
            protected.update(Path(item.split("=", 1)[1]) for item in args.prior_input)
            protected.update(path for path in (args.training_corpus, args.training_manifest) if path is not None)
            if args.output.resolve() in {path.resolve() for path in protected}:
                raise ValueError("audit output would overwrite a protected input")
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"validated": True, "cases": summary["cases"],
                          "template_families": summary["template_families"],
                          "training_audited": report["training_corpus"] is not None,
                          "prior_input_files": len(report["prior_inputs"])}))
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        # Local corpus paths/content must not leak into a publishable receipt.
        print(json.dumps({"validated": False, "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
