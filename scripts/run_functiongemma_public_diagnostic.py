#!/usr/bin/env python3
"""Verify published evidence, or reproduce the known-input MLX diagnostic once.

Verification is offline and loads no model. Reproduction uses the exact released
runtime snapshot and existing local model files; it never downloads weights.
"""
from __future__ import annotations

import argparse
import re
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "experiments/functiongemma-public-v1"
MANIFEST_SHA256 = "f0979577c2649846c259ac8fb1b0b0d922f18b773f4ed510a96083f89571c21e"
RUNTIME_COMMIT = "a536483a75ada2d4d26f7bc93ee0790695f6e697"
ORIGINAL_PLAN_SHA256 = "94a844c410f02c14828a8275d734106f65a765c42e06eb034dcb175ac252c64d"


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def checked_file(path, record):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size != record["bytes"] or file_sha(path) != record["sha256"]:
        raise ValueError("Frozen evidence file identity mismatch")


def project_receipt_metadata(original):
    """The complete row trees stay intact; only three metadata shapes change."""
    public = copy.deepcopy(original)
    public["source_root"] = "source-snapshot/src"
    public["source_file_sha256"] = [{"path": name, "digest": value}
                                    for name, value in sorted(original["source_file_sha256"].items())]
    for item in public["artifacts"]:
        name = Path(item["file"]).name
        group = "public-adapter" if name in {"adapters.safetensors", "adapter_config.json"} else "base"
        item["file"] = "local-model/" + group + "/" + name
    return public


def normalized_harness_source(text):
    """Compare all UTF-8 source except one synthetic fixture credential."""
    pattern = re.compile(r"gateway_api_key\s*=\s*(['\"])[^'\"\n]*\1")
    normalized, changed = pattern.subn("gateway_api_key='fixture'", text)
    if changed != 1:
        raise ValueError("Unexpected synthetic fixture keyword count")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def verify_originals(originals, ledger):
    """Optional owner-side check: all raw rows, gold, numbers and timing match."""
    originals = Path(originals)
    for record in ledger["receipt_records"]:
        source = originals / record["file"]
        if file_sha(source) != record["original_sha256"]:
            raise ValueError("Original receipt identity mismatch")
        original, exported = read_json(source), read_json(EVIDENCE / record["file"])
        if project_receipt_metadata(original) != exported:
            raise ValueError("Receipt changed outside the declared metadata projection")
        if original["rows"] != exported["rows"] or object_sha(original["rows"]) != record["native_output_gold_and_timing_rows_sha256"]:
            raise ValueError("Original model/gold/timing rows changed")
    if file_sha(originals / "plan.json") != ORIGINAL_PLAN_SHA256:
        raise ValueError("Original pre-inference plan identity mismatch")
    original_harness = originals / "benchmark_comparison.py"
    proof = ledger["source_and_harness"]
    if file_sha(original_harness) != proof["inference_harness_original_sha256"] or normalized_harness_source(original_harness.read_text()) != proof["normalized_source_sha256"]:
        raise ValueError("Original and reproduction harness logic differs")


def verify_bundle(originals=None):
    if file_sha(EVIDENCE / "manifest.json") != MANIFEST_SHA256:
        raise ValueError("Evidence manifest changed")
    manifest = read_json(EVIDENCE / "manifest.json")
    expected_names = {record["file"] for record in manifest["files"]} | {"manifest.json"}
    if {path.name for path in EVIDENCE.iterdir() if path.is_file()} != expected_names:
        raise ValueError("Unexpected or missing evidence payload")
    for record in manifest["files"]:
        checked_file(EVIDENCE / record["file"], record)
    for record in manifest["dependencies"]:
        checked_file(ROOT / record["file"], record)
    ledger = read_json(EVIDENCE / "receipt-export.json")
    receipts = [read_json(EVIDENCE / (name + ".json")) for name in ("base", "public_v5")]
    for receipt, record in zip(receipts, ledger["receipt_records"]):
        rows = receipt["rows"]
        if len(rows) != 80 or len({row["id"] for row in rows}) != 80 or len(receipt["warmups"]) != 2:
            raise ValueError("Diagnostic case or warmup count changed")
        if object_sha(rows) != record["native_output_gold_and_timing_rows_sha256"]:
            raise ValueError("Recorded output/gold/timing row identity changed")
        summary = receipt["summary"]
        metrics = [("raw_native_schema_valid", "track_A", "native_schema_valid"),
                   ("raw_tool_correct", "track_A", "tool_correct"),
                   ("raw_exact", "track_A", "arguments_and_entities_exact"),
                   ("verified_exact", "track_B", "verified_exact")]
        if any(summary[name] != sum(row[track][key] for row in rows) for name, track, key in metrics):
            raise ValueError("Recorded count does not match its frozen row flags")
    if [(row["id"], row["query"], row["context"], row["expected"]) for row in receipts[0]["rows"]] != [(row["id"], row["query"], row["context"], row["expected"]) for row in receipts[1]["rows"]]:
        raise ValueError("Paired diagnostic inputs differ")
    archive = ROOT / "experiments/acceptance-v4/sources/released.jsonl"
    source = [json.loads(line) for line in archive.read_text("utf-8").splitlines()]
    actual = [{"path": item["file"][4:], "digest": hashlib.sha256(item["text"].encode()).hexdigest()}
              for item in source if item["file"].endswith(".py")]
    if sorted(actual, key=lambda item: item["path"]) != manifest["runtime_files"]:
        raise ValueError("Licensed source snapshot differs from inference runtime")
    proof = ledger["source_and_harness"]
    if normalized_harness_source((EVIDENCE / "reproduction-harness.py").read_text()) != proof["normalized_source_sha256"]:
        raise ValueError("Reproduction harness logic changed")
    if originals is not None:
        verify_originals(originals, ledger)
    return {"status": "verified", "models_loaded": 0, "generation_calls": 0,
            "receipts": 2, "rows_per_receipt": 80, "runtime_python_files": 19,
            "original_plan_sha256": ORIGINAL_PLAN_SHA256,
            "original_receipt_hashes": manifest["original_receipt_hashes"]}


def verify_model_files(base, adapter, records):
    for record in records:
        pieces = record["file"].split("/")
        path = Path(adapter if pieces[1] == "public-adapter" else base) / pieces[-1]
        # Read-only model-cache symlinks are allowed; expected content is pinned.
        if not path.is_file() or path.stat().st_size != record["bytes"] or file_sha(path) != record["sha256"]:
            raise ValueError("Pinned local base or public adapter identity mismatch")
    for directory, group in [(Path(base), "base"), (Path(adapter), "public-adapter")]:
        expected = {record["file"].split("/")[-1] for record in records if record["file"].split("/")[1] == group}
        actual = {path.name for suffix in ("*.safetensors", "*.json", "*.jinja") for path in directory.glob(suffix)}
        if actual != expected:
            raise ValueError("Unexpected model/config/intermediate-checkpoint files")


def fresh_output(path, protected):
    requested = Path(path).absolute()
    if any(item.is_symlink() or getattr(item, "is_junction", lambda: False)() for item in (requested, *requested.parents)):
        raise ValueError("Use a canonical output path without symlinks")
    result = requested.resolve()
    if result.exists():
        raise FileExistsError("Refusing to replace existing diagnostic output")
    if any(result == target or result in target.parents or target in result.parents for target in map(lambda value: Path(value).resolve(), protected)):
        raise ValueError("Output overlaps protected model or evidence paths")
    if result.is_relative_to(ROOT) and result.relative_to(ROOT).parts[0] != "work":
        raise ValueError("Repository outputs must be under work")
    return result


def child_environment(output):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("ARRMES_") and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                       PYTHONDONTWRITEBYTECODE="1", NO_PROXY="localhost,127.0.0.1",
                       GIT_CEILING_DIRECTORIES=str(Path(output).resolve()))
    return environment


def reproduce(args):
    verify_bundle()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("This diagnostic reproducer requires Apple Silicon MLX")
    manifest = read_json(EVIDENCE / "manifest.json")
    base, adapter = Path(args.base).resolve(), Path(args.adapter).resolve()
    verify_model_files(base, adapter, manifest["model_files"])
    output = fresh_output(args.output_dir, [base, adapter, EVIDENCE])
    output.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("diagnostic_source_identity", ROOT / "scripts/acceptance_source_identity.py")
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    source = identity.extract_source_snapshot(RUNTIME_COMMIT, output / "source")
    original = read_json(EVIDENCE / "plan-projection.json")
    conditions = copy.deepcopy(original["conditions"])
    conditions["public_v5"]["adapter_path"] = str(adapter)
    harness, controller = EVIDENCE / "reproduction-harness.py", EVIDENCE / "condition-controller.py"
    dataset, oracle = ROOT / "data/overnight-v1/routing-heldout.jsonl", ROOT / "data/overnight-v1/fixture-oracle.json"
    files = list(sorted(source.rglob("*.py"))) + [harness, controller, dataset, oracle, dataset.with_name("manifest.json")]
    for record in manifest["model_files"]:
        pieces = record["file"].split("/")
        files.append((adapter if pieces[1] == "public-adapter" else base) / pieces[-1])
    plan = {"scope": "New reproduction of known diagnostic inputs; never overwrite reference results",
            "source_root": str(source), "harness_path": str(harness), "base_path": str(base),
            "dataset_path": str(dataset), "oracle_path": str(oracle), "conditions": conditions,
            "maximum_seconds_per_condition": 1200,
            "frozen_files": [{"path": str(path), "bytes": path.stat().st_size, "sha256": file_sha(path)} for path in files]}
    plan_path = output / "reproduction-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    metadata = {"reference_plan_sha256": ORIGINAL_PLAN_SHA256, "reproduction_plan_sha256": file_sha(plan_path),
                "source_identity": identity.resolve_source(source, RUNTIME_COMMIT),
                "inference_harness_original_sha256": original["harness_sha256"],
                "reproduction_harness_sha256": file_sha(harness),
                "oracle_projection": "13 synthetic address values differ from original; recorded historical fixture counts remain unchanged"}
    (output / "reproduction-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    for condition in ("base", "public_v5"):
        result = subprocess.run([sys.executable, str(controller), "--plan", str(plan_path),
                                 "--plan-sha256", file_sha(plan_path), "--condition", condition],
                                env=child_environment(output), check=False)
        if result.returncode:
            return result.returncode
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    verify = command.add_parser("verify", help="Offline evidence check; no model load")
    verify.add_argument("--originals", help="Optional local owner-evidence directory; never uploaded")
    run = command.add_parser("run", help="One new paired MLX run with existing exact local weights")
    run.add_argument("--base", required=True)
    run.add_argument("--adapter", required=True)
    run.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            print(json.dumps(verify_bundle(args.originals), indent=2))
            return 0
        return reproduce(args)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "detail": "Validation or reproduction failed; raw exception text omitted"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
