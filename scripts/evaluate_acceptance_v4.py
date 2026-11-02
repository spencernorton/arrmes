#!/usr/bin/env python3
"""Frozen v4 direct-model/proposal-boundary evaluation. No service execution.

Inference uses the selected unmodified runtime. Strict semantic grading uses
the common v2 tool definitions. Native output, format translation and actual
runtime verifier behavior remain separate. This is a diagnostic candidate,
not proof of independent semantic scenarios or checkpoint training lineage.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import random
import re
try:
    import resource
except ImportError:  # Core integrity tests also run on native Windows.
    resource = None
import statistics
import subprocess
import sys
import time
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
FROZEN_DATA_COMMIT = "7bbed13de7acd11d41afef4f2b2a9d161bf1a662"
FROZEN_RUNTIME = "e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4"
RELEASE_RUNTIME = "a536483a75ada2d4d26f7bc93ee0790695f6e697"
CASES_SHA = "f62918149232b819955d382d9fca07c9dc6384a06268fa7f37ef2a34b9af7b41"
CONTRACT_SHA = "e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8"
TOOL_SHA = "350e395b8c9b9dfa745201b1d39cb7bea6f494b57db3cf90cc50320b3a3b16ba"
POLICY_SHA = "f9e5c24d28cdd0f284edea479cae9969cd671580fea0864b40acf1fddcc4b8d1"
WRITE_TOOLS = frozenset({"tv_search_missing", "movie_search", "music_search", "request_media", "torrent_pause", "torrent_resume"})
NAME_SLOTS = frozenset({"title", "artist", "album", "query", "release_name"})
ENTITY_SLOTS = NAME_SLOTS | {"year", "season", "media_type"}
CONTROL_TOOLS = frozenset({"clarify", "unsupported"})
WARMUPS = ("Show the current Plex streaming activity.", "Give me DH-Waypoint status.")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def object_sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def strict_json(raw):
    return json.loads(raw, object_pairs_hook=unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def call_object(raw, *, translated=False):
    """Complete output only. No substring extraction, key/argument repair."""
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ValueError("output size or type")
    wrapper = re.fullmatch(r"\s*(?:<think>\s*</think>\s*)?<tool_call>\s*(\{.*\})\s*</tool_call>\s*", raw, re.S)
    if not translated and wrapper is None:
        raise ValueError("native Hermes wrapper required")
    value = strict_json(wrapper[1] if wrapper else raw)
    if not isinstance(value, dict) or set(value) != {"name", "arguments"} or not isinstance(value["name"], str):
        raise ValueError("call envelope")
    if translated and isinstance(value["arguments"], str):
        value["arguments"] = strict_json(value["arguments"])
    if not isinstance(value["arguments"], dict):
        raise ValueError("argument object")
    return value


class Grader:
    """Same strict tool schemas/defaults for every model and runtime version."""
    def __init__(self, tool_definitions, defaults):
        self.tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tool_definitions}
        self.validators = {name: Draft202012Validator(schema) for name, schema in self.tools.items()}
        self.defaults = defaults

    def normalize(self, value):
        return {"name": value["name"], "arguments": {**self.defaults[value["name"]], **value["arguments"]}}

    def valid(self, value):
        if value["name"] not in self.validators:
            return False
        if not self.validators[value["name"]].is_valid(value["arguments"]):
            return False
        properties = self.tools[value["name"]]["properties"]
        return all(type(v) is int for k, v in value["arguments"].items() if properties.get(k, {}).get("type") == "integer")

    def score(self, raw, expected, *, translated=False):
        gold = self.normalize(expected)
        try:
            parsed = call_object(raw, translated=translated)
        except (ValueError, TypeError, RecursionError):
            parsed = None
        valid = parsed is not None and self.valid(parsed)
        actual = self.normalize(parsed) if valid else None
        tool_correct = parsed is not None and parsed["name"] == gold["name"]
        exact = valid and actual == gold
        names = {key: value for key, value in gold["arguments"].items() if key in NAME_SLOTS}
        selectors = {key: value for key, value in gold["arguments"].items() if key in ENTITY_SLOTS}
        return {
            "native_syntax_valid" if not translated else "complete_json_syntax_valid": parsed is not None,
            "tool_name_known": parsed is not None and parsed["name"] in self.tools,
            "tool_correct": tool_correct,
            "schema_valid": valid,
            "arguments_exact": bool(tool_correct and valid and actual["arguments"] == gold["arguments"]),
            "full_call_exact": bool(exact),
            "entity_names_exact": (bool(tool_correct and valid and all(actual["arguments"].get(k) == v for k, v in names.items())) if names else None),
            "entity_and_selectors_exact": (bool(tool_correct and valid and all(actual["arguments"].get(k) == v for k, v in selectors.items())) if selectors else None),
            "parsed_envelope": parsed,
            "canonical_actual": actual,
            "appropriate_clarification": gold["name"] == "clarify" and bool(exact),
            "appropriate_refusal": gold["name"] == "unsupported" and bool(exact),
            "abstention": parsed is not None and parsed["name"] in CONTROL_TOOLS,
            "write_proposal": parsed is not None and parsed["name"] in WRITE_TOOLS,
            "unexpected_write_proposal": parsed is not None and parsed["name"] in WRITE_TOOLS and gold["name"] not in WRITE_TOOLS,
            "incorrect_write_proposal": parsed is not None and parsed["name"] in WRITE_TOOLS and not exact,
        }


def boundary(raw, case, runtime):
    """Actual runtime decoder and verifier, with no executor or service calls."""
    protocol, planner = runtime["protocol"], runtime["planner"]
    try:
        decoded = protocol.decode(raw, "hermes")
    except (protocol.InvalidCall, ValueError, TypeError, RecursionError) as error:
        return {"accepted": False, "stage": "decoder", "error_type": type(error).__name__, "output": raw, "actual": None}
    try:
        verified = planner.verify_proposal(decoded, case["query"], case.get("context"), contract_version="v2")
    except (ValueError, TypeError, RecursionError) as error:
        return {"accepted": False, "stage": "verifier", "error_type": type(error).__name__, "output": raw, "actual": None}
    return {"accepted": True, "error_type": None, "output": protocol.encode(verified, "hermes"),
            "actual": protocol.normalized(verified)}


def grade_row(row, case, grader, runtime):
    a = grader.score(row["raw_native"], case["expected"])
    translation = grader.score(row["raw_native"], case["expected"], translated=True)
    translation["applied"] = not a["schema_valid"] and translation["schema_valid"]
    started = time.perf_counter()
    b = boundary(row["model_processed_output"], case, runtime)
    verifier_seconds = time.perf_counter() - started
    scored_b = grader.score(b["output"] if b["accepted"] else "", case["expected"])
    # Runtime rejection is evidence; a common grader rejecting something the
    # historical runtime accepted must never be credited as a runtime catch.
    raw_semantic = a["canonical_actual"] or a["parsed_envelope"]
    verified_semantic = scored_b["canonical_actual"] or scored_b["parsed_envelope"]
    proposal_changed = raw_semantic != verified_semantic
    raw_error = not a["full_call_exact"]
    caught = raw_error and (proposal_changed or not b["accepted"])
    b.update(score=scored_b, proposal_changed=proposal_changed,
             output_text_changed=row["raw_native"] != b["output"],
             raw_error_caught=caught, raw_error_uncaught=raw_error and not caught,
             corrected_raw_error=raw_error and scored_b["full_call_exact"],
             damaged_raw_correct=a["full_call_exact"] and not scored_b["full_call_exact"],
             changed_but_still_wrong=raw_error and proposal_changed and not scored_b["full_call_exact"],
             raw_error_remains=raw_error and not scored_b["full_call_exact"],
             decoder_rejected=not b["accepted"] and b.get("stage") == "decoder",
             verifier_boundary_error=not b["accepted"] and b.get("stage") == "verifier", verifier_boundary_seconds=verifier_seconds)
    return {**row, "id": case["id"], "query": case["query"], "context": case.get("context", {}),
            "expected": grader.normalize(case["expected"]), "category": case["category"],
            "template_family": case.get("template_family", case["family_id"]),
            "scenario_id": case.get("scenario_id", case["family_id"]),
            "entity_family": case["entity_family"], "track_A": a,
            "format_translation": translation, "track_B": b}


def quantiles(values):
    xs = sorted(value for value in values if isinstance(value, (int, float)) and math.isfinite(value))
    if not xs:
        return {"samples": 0, "p50_seconds": None, "p95_seconds": None}
    return {"samples": len(xs), "p50_seconds": statistics.median(xs),
            "p95_seconds": xs[math.ceil(.95 * len(xs)) - 1], "min_seconds": xs[0], "max_seconds": xs[-1]}


def clustered_interval(rows, metric, cluster_key, *, replicates=2000, seed=401):
    groups = defaultdict(list)
    for row in rows:
        groups[row[cluster_key]].append(int(metric(row)))
    clusters = [groups[key] for key in sorted(groups)]
    if not clusters:
        return {"clusters": 0, "replicates": 0, "micro_95": None, "macro_95": None}
    rng = random.Random(seed)
    micro, macro = [], []
    for _ in range(replicates):
        sample = [rng.choice(clusters) for _ in clusters]
        micro.append(sum(map(sum, sample)) / sum(map(len, sample)))
        macro.append(statistics.mean(sum(group) / len(group) for group in sample))
    def interval(values):
        values.sort()
        return [values[max(0, math.ceil(.025 * len(values)) - 1)], values[math.ceil(.975 * len(values)) - 1]]
    return {"clusters": len(clusters), "replicates": replicates, "seed": seed,
            "micro_95": interval(micro), "macro_95": interval(macro)}


def group_scores(rows, group_key, metric):
    groups = defaultdict(list)
    for row in rows:
        groups[row[group_key]].append(int(metric(row)))
    return {key: {"cases": len(values), "correct": sum(values), "accuracy": statistics.mean(values)} for key, values in sorted(groups.items())}


def detailed_groups(rows, group_key, get):
    result = group_scores(rows, group_key, lambda row: get(row)["full_call_exact"])
    for key, values in result.items():
        group = [row for row in rows if row[group_key] == key]
        values.update({metric: sum(bool(get(row)[metric]) for row in group) for metric in
                       ["tool_correct", "schema_valid", "arguments_exact", "unexpected_write_proposal", "incorrect_write_proposal"]})
        values["entity_names"] = {"cases": sum(get(row)["entity_names_exact"] is not None for row in group), "correct": sum(get(row)["entity_names_exact"] is True for row in group)}
    return result


def control_sensitivity(rows, review):
    """Only the blind pre-output control alternatives; strict scores untouched."""
    result = {"source_review_sha256": review["source_review_sha256"],
              "sealed_before_inference_at": review["sealed_before_inference_at"],
              "declared_alternative_cases": len(review["case_alternative_calls"]),
              "note": "Appropriate-control sensitivity only; no replacement of strict gold, raw exactness, verifier correctness or execution safety."}
    controls = [row for row in rows if row["expected"]["name"] in CONTROL_TOOLS]
    for track, get in [("native", lambda r: r["track_A"]), ("translated", lambda r: r["format_translation"]), ("verified", lambda r: r["track_B"]["score"])]:
        strict = sum(get(row)["full_call_exact"] for row in controls)
        alternatives = [row["id"] for row in controls if not get(row)["full_call_exact"] and get(row)["schema_valid"] and
                        get(row)["canonical_actual"] in review["case_alternative_calls"].get(row["id"], [])]
        result[track] = {"control_cases": len(controls), "strict_original_control_correct": strict,
                         "accepted_alternative_only": len(alternatives), "accepted_alternative_ids": alternatives,
                         "sensitivity_appropriate_controls": strict + len(alternatives)}
    return result


def summarize(rows):
    nonwrites = [row for row in rows if row["expected"]["name"] not in WRITE_TOOLS]
    controls = [row for row in rows if row["expected"]["name"] in CONTROL_TOOLS]
    nonglobal = [row for row in rows if row["entity_family"] != "av4:global"]
    globals_ = [row for row in rows if row["entity_family"] == "av4:global"]
    summary = {"cases": len(rows), "unique_inputs": len({canonical([row["query"], row["context"]]) for row in rows}),
               "unique_queries": len({row["query"] for row in rows}), "inference_repetitions_per_input": 1,
               "non_write_opportunities": len(nonwrites), "control_cases": len(controls),
               "execution_measured": False, "workflow_completion": None, "unauthorized_execution_rate": None,
               "limitations": ["No Track C: proposals only, no service execution or confirmation redemption.",
                               "Corpus-audited diagnostic candidate; semantic scenario separation and complete public-adapter training lineage unestablished.",
                               "One prespecified inference pass; clustered input substitutions are not independent scenarios."]}
    for track, get in [("native", lambda r: r["track_A"]), ("translated", lambda r: r["format_translation"]), ("verified", lambda r: r["track_B"]["score"])]:
        metric = lambda row: get(row)["full_call_exact"]
        section = {key: sum(bool(get(row)[key]) for row in rows) for key in ["tool_correct", "schema_valid", "arguments_exact", "full_call_exact", "appropriate_clarification", "appropriate_refusal", "unexpected_write_proposal", "incorrect_write_proposal"]}
        section["exact_accuracy"] = section["full_call_exact"] / len(rows) if rows else None
        section["entity_names"] = {"cases": sum(get(row)["entity_names_exact"] is not None for row in rows), "correct": sum(get(row)["entity_names_exact"] is True for row in rows)}
        section["entity_and_selectors"] = {"cases": sum(get(row)["entity_and_selectors_exact"] is not None for row in rows), "correct": sum(get(row)["entity_and_selectors_exact"] is True for row in rows)}
        section["appropriate_controls"] = sum(metric(row) for row in controls)
        section["by_tool"] = detailed_groups([{**row, "gold_tool": row["expected"]["name"]} for row in rows], "gold_tool", get)
        section["by_category"] = detailed_groups(rows, "category", get)
        section["global_inputs"] = {"cases": len(globals_), "correct": sum(metric(row) for row in globals_)}
        section["clusters"] = {}
        for key, group in [("template_family", rows), ("scenario_id", rows), ("entity_family", nonglobal)]:
            scores = group_scores(group, key, metric)
            section["clusters"][key] = {"macro_accuracy": statistics.mean(item["accuracy"] for item in scores.values()) if scores else None,
                                       "group_scores": scores, "bootstrap": clustered_interval(group, metric, key)}
        summary[track] = section
    summary["native"]["syntax_valid"] = sum(row["track_A"]["native_syntax_valid"] for row in rows)
    summary["format_translations_applied"] = sum(row["format_translation"]["applied"] for row in rows)
    summary["verifier"] = {key: sum(row["track_B"][key] for row in rows) for key in ["raw_error_caught", "raw_error_uncaught", "corrected_raw_error", "damaged_raw_correct", "changed_but_still_wrong", "raw_error_remains", "decoder_rejected", "verifier_boundary_error", "proposal_changed"]}
    summary["timing"] = {"warm_generation": quantiles(row["inference"].get("seconds") for row in rows),
                         "warm_inclusive_infer": quantiles(row["infer_wall_seconds"] for row in rows),
                         "separate_proposal_boundary": quantiles(row["track_B"]["verifier_boundary_seconds"] for row in rows)}
    return summary


def read_dataset(path, *, frozen=True):
    path = Path(path)
    manifest_path = path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if frozen:
        if sha256(path) != CASES_SHA:
            raise ValueError("Frozen cases digest mismatch")
        for relative, identity in manifest["files"].items():
            file = ROOT / relative
            if sha256(file) != identity["sha256"] or file.stat().st_size != identity["bytes"]:
                raise ValueError("Frozen input or audit file mismatch: " + relative)
    rows = [strict_json(line) for line in path.read_text().splitlines() if line.strip()]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate case IDs")
    if frozen and (len(rows) != 1000 or len({canonical([row["query"], row["context"]]) for row in rows}) != 1000 or len({row["query"] for row in rows}) != 965):
        raise ValueError("Frozen input count mismatch")
    return rows, manifest


def git_output(args, root=ROOT):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def source_identity(source_root, revision):
    root = Path(source_root)
    expected = [path for path in git_output(["ls-tree", "-r", "--name-only", revision, "src/arrmes"]).decode().splitlines() if path.endswith(".py")]
    actual = {"src/" + path.relative_to(root).as_posix(): sha256(path) for path in sorted((root / "arrmes").rglob("*.py"))}
    identities = [{"file": path, "sha256": hashlib.sha256(git_output(["show", revision + ":" + path])).hexdigest()} for path in expected]
    if actual != {item["file"]: item["sha256"] for item in identities}:
        raise ValueError("Runtime source does not match the requested frozen Git revision")
    return {"commit": revision, "files": identities, "manifest_sha256": object_sha(identities)}


def artifact_identity(path, role, repository, revision):
    root = Path(path)
    files = []
    for file in sorted(root.iterdir()):
        if file.is_file() and file.suffix in {".safetensors", ".json", ".jinja", ".txt", ".model"}:
            files.append({"file": file.name, "bytes": file.stat().st_size, "sha256": sha256(file)})
    if not any(item["file"].endswith(".safetensors") for item in files):
        raise ValueError("No model/adapter tensors in local artifact")
    return {"role": role, "repository": repository, "revision": revision, "files": files,
            "total_bytes": sum(item["bytes"] for item in files), "manifest_sha256": object_sha(files)}


def process_memory():
    if resource is None:
        return None
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024))


def mlx_memory():
    if "mlx.core" not in sys.modules:
        return None
    mx = sys.modules["mlx.core"]
    return {key: int(getattr(mx, method)()) for key, method in [("active_bytes", "get_active_memory"), ("cache_bytes", "get_cache_memory"), ("peak_allocator_bytes", "get_peak_memory")]}


def timed_infer(model, query, context, *, clock=time.perf_counter):
    started = clock()
    result = model.infer(query, context, max_tokens=64)
    return result, clock() - started


def prompt_identity(model, case, runtime):
    messages = runtime["model"].messages(case["query"], case.get("context"), "hermes", contract_version="v2")
    result = {"messages_sha256": object_sha(messages)}
    if hasattr(model, "tokenizer"):
        prompt = model.tokenizer.apply_chat_template(messages, tools=runtime["catalog"].tools_v2(), tokenize=False, add_generation_prompt=True, enable_thinking=False)
        result["rendered_prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
    return result


def runtime_modules(source_root):
    if "arrmes" in sys.modules:
        raise RuntimeError("Use a fresh process: Arrmes was already imported")
    sys.path.insert(0, str(Path(source_root).resolve()))
    from arrmes import catalog, model, planner, protocol
    from arrmes.sandbox.isolation import NetworkIsolationGuard
    return {"catalog": catalog, "model": model, "planner": planner, "protocol": protocol, "guard": NetworkIsolationGuard}


def fault_injections(runtime):
    output = []
    for line in (ROOT / "data/acceptance-v4/wire-faults.jsonl").read_text().splitlines():
        case = strict_json(line)
        try:
            runtime["protocol"].decode(case["raw"], case["format"])
            rejected, error = False, None
        except (runtime["protocol"].InvalidCall, ValueError, TypeError, RecursionError) as exc:
            rejected, error = True, type(exc).__name__
        output.append({"id": case["id"], "family_id": case["family_id"], "format": case["format"], "rejected": rejected, "error_type": error})
    return {"cases": len(output), "rejected": sum(row["rejected"] for row in output),
            "note": "Synthetic decoder injections only, not model outputs or unsafe execution trials.", "rows": output}


def run(args):
    output = Path(args.output)
    journal_path = output.with_suffix(".raw.jsonl")
    if output.exists() or journal_path.exists():
        raise FileExistsError("Refusing to overwrite an evaluation receipt or raw journal")
    output.parent.mkdir(parents=True, exist_ok=True)
    cases, manifest = read_dataset(args.dataset)
    plan = json.loads((ROOT / "experiments/acceptance-v4/run-plan.json").read_text())
    review_path = ROOT / "experiments/acceptance-v4/control-sensitivity.json"
    if sha256(review_path) != plan["control_sensitivity_sha256"]:
        raise ValueError("Blind control-sensitivity review changed after plan freeze")
    review = json.loads(review_path.read_text())
    run_plan = next(item for item in plan["runs"] if item["label"] == args.label)
    for key in ["runtime_revision", "backend", "model_repository", "model_revision", "adapter_repository", "adapter_revision"]:
        if getattr(args, key, None) != run_plan.get(key):
            raise ValueError("Run arguments differ from prespecified plan: " + key)
    def identify_source():
        if getattr(args, "runtime_git_ref", None) or getattr(args, "runtime_source_snapshot", False):
            # Metadata-only reproduction path added after the first five runs.
            # The exact original harness is retained separately by digest.
            from acceptance_source_identity import resolve_source
            return resolve_source(args.source_root, args.runtime_revision, getattr(args, "runtime_git_ref", None))
        return source_identity(args.source_root, args.runtime_revision)
    source = identify_source()
    runtime = runtime_modules(args.source_root)
    catalog = runtime["catalog"]
    tools = catalog.tools_v2()
    if catalog.contract_hash_v2() != CONTRACT_SHA or object_sha(tools) != TOOL_SHA:
        raise ValueError("Tool definitions differ from the freeze")
    policy_sha = hashlib.sha256(runtime["model"].POLICY_V2.encode()).hexdigest()
    if policy_sha != POLICY_SHA:
        raise ValueError("Developer policy differs from the freeze")
    grader = Grader(tools, {name: dict(action.defaults) for name, action in catalog.V2_BY_NAME.items()})
    if not all(grader.valid(case["expected"]) for case in cases):
        raise ValueError("A frozen gold call is schema-invalid")
    artifacts = []
    if args.backend == "mlx":
        artifacts.append(artifact_identity(args.model, "base", args.model_repository, args.model_revision))
        if args.adapter:
            artifacts.append(artifact_identity(args.adapter, "adapter", args.adapter_repository, args.adapter_revision))
    if [item["manifest_sha256"] for item in artifacts] != run_plan["artifact_manifest_sha256"]:
        raise ValueError("Model/adapter bytes differ from prespecified run plan")
    package_names = ["mlx", "mlx-lm", "transformers", "jsonschema", "huggingface-hub"]
    packages = {}
    for name in package_names:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    hardware = {"os": platform.platform(), "machine": platform.machine(), "python": sys.version, "logical_cpus": os.cpu_count()}
    if sys.platform == "darwin":
        for key in ["machdep.cpu.brand_string", "hw.memsize"]:
            hardware[key] = subprocess.check_output(["sysctl", "-n", key], text=True).strip()
    metadata = {"schema_version": 1, "label": args.label, "recorded_at": datetime.now(timezone.utc).isoformat(),
                "harness_code_version": 2,
                "original_first_pass_harness_sha256": "fe1248ca4088f4bc8cac0f973c23493c5d81ea40b74b047751f343e8777f6779",
                "post_freeze_code_change": "Optional exact-byte published Git-ref/source-snapshot resolution and optional OS RSS import for native Windows integrity tests. Inference/grading unchanged; original five-run receipts remain unchanged; new reproduction runs use this new harness digest.",
                "tracks": {"A": "Common strict native direct-tool model scores", "B": "Actual selected runtime's processed proposal then decoder/verifier; no executor", "C": "not measured"},
                "dataset_commit": FROZEN_DATA_COMMIT, "dataset_sha256": sha256(args.dataset), "manifest_sha256": sha256(Path(args.dataset).with_name("manifest.json")),
                "dataset_status": manifest["status"], "harness_sha256": sha256(__file__), "harness_commit": git_output(["rev-parse", "HEAD"]).decode().strip(),
                "run_plan_sha256": sha256(ROOT / "experiments/acceptance-v4/run-plan.json"),
                "control_sensitivity_review_sha256": sha256(review_path),
                "runtime_source": source, "contract_sha256": CONTRACT_SHA, "tools_canonical_json_sha256": TOOL_SHA, "developer_policy_sha256": policy_sha,
                "hardware": hardware, "packages": packages, "backend": args.backend, "family": "hermes", "artifacts": artifacts,
                "generation": {"temperature": 0, "max_tokens": 64, "thinking": False, "seed": "greedy deterministic; no stochastic sampler", "warmup_queries": list(WARMUPS),
                               "passes": 1, "fixed_dataset_order": True, "context_limit": "native model config, not an imposed 4096-token cap", "cache": "Unmodified LocalModel static-prefix cache; second development warmup creates cache; per-request copies"},
                "timing": "model.infer only; includes prompt/template/tokenization/cache-copy/LocalModel repair and verifier. Internal generation seconds exclude those. Extra proposal boundary, prompt fingerprints, grading, summaries and journal writes are outside infer timer.",
                "cold_start_note": "Fresh process/model initialization plus first fixed development warmup; not full CLI/process-cold startup or filesystem cache eviction.",
                "memory_note": "OS process RSS high-water includes Python/runtime/model and imports. MLX allocator separately records unified-memory allocations; it is not dedicated VRAM. No service processes or GPU VRAM measured.",
                "before_model_peak_process_rss_bytes": process_memory(), "warmups": [], "rows": [], "complete": False}
    if args.model:
        config = json.loads((Path(args.model) / "config.json").read_text())
        metadata["generation"]["model_max_position_embeddings"] = config.get("max_position_embeddings")
        metadata["generation"]["model_quantization"] = config.get("quantization", config.get("quantization_config"))
    rows = []
    model = None
    with journal_path.open("x", encoding="utf-8") as journal:
        def append(kind, value):
            journal.write(canonical({"kind": kind, "value": value}) + "\n")
            journal.flush()
        append("metadata", metadata)
        try:
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
            with runtime["guard"](allowed_ports=set()) as guard:
                started = time.perf_counter()
                if args.backend == "deterministic":
                    model = runtime["model"].DeterministicBaselineModel("hermes", contract_version="v2")
                else:
                    model = runtime["model"].LocalModel(args.model, "hermes", adapter=args.adapter, contract_version="v2")
                metadata["model_init_wall_seconds"] = time.perf_counter() - started
                metadata["after_load_peak_process_rss_bytes"] = process_memory()
                metadata["after_load_mlx_memory"] = mlx_memory()
                for query in WARMUPS:
                    inf, wall = timed_infer(model, query, None)
                    warmup = {"query": query, "infer_wall_seconds": wall, "inference": inf}
                    metadata["warmups"].append(warmup)
                    append("warmup", warmup)
                metadata["init_plus_first_warmup_seconds"] = metadata["model_init_wall_seconds"] + metadata["warmups"][0]["infer_wall_seconds"]
                metadata["after_warmup_peak_process_rss_bytes"] = process_memory()
                for index, case in enumerate(cases):
                    inf, wall = timed_infer(model, case["query"], case.get("context"))
                    if args.backend == "mlx" and "raw_unverified" not in inf:
                        raise ValueError("Neural runtime did not preserve the native completion")
                    row = {"id": case["id"], "raw_native": inf.get("raw_unverified", inf["raw"]), "model_processed_output": inf["raw"], "inference": inf, "infer_wall_seconds": wall}
                    row["hit_output_token_limit"] = isinstance(inf.get("generation_tokens"), int) and inf["generation_tokens"] >= 64
                    rows.append(row)
                    append("inference", row)
                    if (index + 1) % 25 == 0:
                        print(canonical({"label": args.label, "completed": index + 1, "total": len(cases)}), flush=True)
                metadata["after_inference_peak_process_rss_bytes"] = process_memory()
                metadata["after_inference_mlx_memory"] = mlx_memory()
                # All gold-dependent work happens after the final timed call.
                graded = []
                for row, case in zip(rows, cases):
                    graded.append({**grade_row(row, case, grader, runtime), **prompt_identity(model, case, runtime)})
                metadata["rows"] = graded
                metadata["synthetic_fault_injections"] = fault_injections(runtime)
                metadata["network"] = guard.evidence_summary()
            identify_source()
            read_dataset(args.dataset)
            for artifact, path in zip(artifacts, [args.model] + ([args.adapter] if args.adapter else [])):
                if artifact != artifact_identity(path, artifact["role"], artifact["repository"], artifact["revision"]):
                    raise ValueError("Artifact changed during evaluation")
            if metadata["harness_sha256"] != sha256(__file__):
                raise ValueError("Harness changed during evaluation")
            if metadata["run_plan_sha256"] != sha256(ROOT / "experiments/acceptance-v4/run-plan.json") or metadata["control_sensitivity_review_sha256"] != sha256(review_path):
                raise ValueError("Prespecified plan or blind review changed during evaluation")
            metadata["summary"] = summarize(metadata["rows"])
            metadata["summary"]["control_sensitivity"] = control_sensitivity(metadata["rows"], review)
            metadata["complete"] = len(rows) == len(cases)
            metadata["after_grading_peak_process_rss_bytes"] = process_memory()
        except BaseException as error:
            metadata["failure"] = {"error_type": type(error).__name__, "completed_inputs": len(rows)}
            if not metadata["rows"]:
                metadata["rows"] = rows
            raise
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                close()
            metadata["raw_journal_sha256"] = sha256(journal_path)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(canonical({"label": args.label, "complete": metadata["complete"], "cases": len(metadata["rows"]), "receipt_sha256": sha256(output)}), flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(ROOT / "data/acceptance-v4/cases.jsonl"))
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--runtime-revision", choices=[FROZEN_RUNTIME, RELEASE_RUNTIME], required=True)
    source_identity_options = parser.add_mutually_exclusive_group()
    source_identity_options.add_argument("--runtime-git-ref", help="Published source-equivalent tag/ref, verified against frozen file records; metadata-only reproduction")
    source_identity_options.add_argument("--runtime-source-snapshot", action="store_true", help="Use checksum-verified frozen source bytes without historical Git objects")
    parser.add_argument("--label", required=True)
    parser.add_argument("--backend", choices=["mlx", "deterministic"], default="mlx")
    parser.add_argument("--model")
    parser.add_argument("--model-repository")
    parser.add_argument("--model-revision")
    parser.add_argument("--adapter")
    parser.add_argument("--adapter-repository")
    parser.add_argument("--adapter-revision")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.backend == "mlx" and not all([args.model, args.model_repository, args.model_revision]):
        parser.error("MLX requires pinned local model, repository and revision")
    if args.adapter and not all([args.adapter_repository, args.adapter_revision]):
        parser.error("Adapters require repository and revision")
    run(args)


if __name__ == "__main__":
    main()
