#!/usr/bin/env python3
"""Verify retained Windows CI evidence offline; never dispatch or run inference."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "experiments" / "windows-cpu-v1"
MANIFEST_SHA256 = "fd0156a4ec3a710e90f31d4d95cc25232b67fe985a218e73a77595d1dd5ba440"
ANALYZER_SHA256 = "6b5ed341cfeb848a50accd0f7bc132b1b2c9a9af1c9c922b578f0f6588fda0b8"


def load_payloads(directory=EVIDENCE):
    """Relocate this known experiment; never trust new code or a new manifest."""
    directory = Path(directory)
    with (directory / "manifest.json").open("rb") as source:
        encoded = source.read(65537)
    if len(encoded) > 65536 or hashlib.sha256(encoded).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Evidence manifest is not the pinned Windows experiment")
    manifest = json.loads(encoded)
    expected = {row["file"] for row in manifest["files"]} | {"manifest.json"}
    if {path.name for path in directory.iterdir() if path.is_file()} != expected:
        raise ValueError("Unexpected or missing evidence payload")
    payloads = {}
    for row in manifest["files"]:
        if Path(row["file"]).name != row["file"] or not 0 < row["bytes"] <= 2 << 20:
            raise ValueError("Invalid evidence payload record")
        with (directory / row["file"]).open("rb") as source:
            payload = source.read(row["bytes"] + 1)
        if len(payload) != row["bytes"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError("Evidence payload differs from its recorded digest")
        payloads[row["file"]] = payload
    return payloads


def reconstruct_receipt(directory=EVIDENCE, *, payloads=None):
    payloads = load_payloads(directory) if payloads is None else payloads
    ledger = json.loads(payloads["receipt-export.json"])
    encoded = payloads["windows-cpu-model-smoke-public.json"]
    if hashlib.sha256(encoded).hexdigest() != ledger["public_sha256"]:
        raise ValueError("Public receipt differs from the retained export")
    public = json.loads(encoded)
    records = public["source_file_sha256_records"]
    mapping = {item["file"]: item["sha256"] for item in records}
    if len(mapping) != len(records):
        raise ValueError("Duplicate source record")
    original = {("source_file_sha256" if key == "source_file_sha256_records" else key):
                (mapping if key == "source_file_sha256_records" else value)
                for key, value in public.items()}
    raw = (json.dumps(original, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != ledger["original_sha256"]:
        raise ValueError("Original CRLF receipt cannot be reconstructed exactly")
    return original, raw


def original_analyzer(directory=EVIDENCE, *, payloads=None):
    payloads = load_payloads(directory) if payloads is None else payloads
    name = "analyze_receipt_original.py"
    encoded = payloads[name]
    if hashlib.sha256(encoded).hexdigest() != ANALYZER_SHA256:
        raise ValueError("Original analyzer differs from recorded source")
    namespace = {"__name__": "recorded_windows_analyzer", "__file__": str(Path(directory) / name)}
    # Execute only the bytes just authenticated, without a second path read.
    exec(compile(encoded, namespace["__file__"], "exec"), namespace)
    return namespace["analyze"]


def verify(directory=EVIDENCE):
    payloads = load_payloads(directory)
    receipt, raw = reconstruct_receipt(payloads=payloads)
    retained = json.loads(payloads["analysis-original.json"])
    result = original_analyzer(directory, payloads=payloads)(receipt, run_id=retained["run_id"],
        workflow_sha=retained["workflow_sha"],
        source_manifest=json.loads(payloads["product-source-manifest.json"]),
        run_metadata=json.loads(payloads["api-metadata.json"]))
    if result != {key: value for key, value in retained.items() if key != "inputs"}:
        raise ValueError("Reconstructed evidence does not reproduce the original analysis")
    return {**result, "reconstructed_receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "Offline reproduction of retained CI evidence, not a new Windows run"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["all_identity_and_operational_checks_passed"],
                      "checks": result["checks"]}))
    return 0 if result["all_identity_and_operational_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
