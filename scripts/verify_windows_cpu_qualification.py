#!/usr/bin/env python3
"""Verify retained Windows CI evidence offline; never dispatch or run inference."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "experiments" / "windows-cpu-v1"


def reconstruct_receipt(directory=EVIDENCE):
    directory = Path(directory)
    ledger = json.loads((directory / "receipt-export.json").read_text())
    encoded = (directory / "windows-cpu-model-smoke-public.json").read_bytes()
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


def original_analyzer(directory=EVIDENCE):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    name = "analyze_receipt_original.py"
    expected = next(row["sha256"] for row in manifest["files"] if row["file"] == name)
    if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
        raise ValueError("Original analyzer differs from recorded source")
    return runpy.run_path(str(directory / name))["analyze"]


def verify(directory=EVIDENCE):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    for row in manifest["files"]:
        if Path(row["file"]).name != row["file"]:
            raise ValueError("Evidence records must name direct child files")
        payload = (directory / row["file"]).read_bytes()
        if len(payload) != row["bytes"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError("Evidence payload differs from its recorded digest")
    receipt, raw = reconstruct_receipt(directory)
    retained = json.loads((directory / "analysis-original.json").read_text())
    result = original_analyzer(directory)(receipt, run_id=retained["run_id"],
        workflow_sha=retained["workflow_sha"],
        source_manifest=json.loads((directory / "product-source-manifest.json").read_text()),
        run_metadata=json.loads((directory / "api-metadata.json").read_text()))
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
