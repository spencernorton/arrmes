"""Retained failures, decoded source bytes and replay labels must stay auditable."""
import hashlib
import json
from pathlib import Path

import pytest
ROOT = Path(__file__).resolve().parents[1] / "experiments/native-workflows-v1"
pytestmark = pytest.mark.skipif(not (ROOT / "SHA256SUMS.json").exists(), reason="Local benchmark dataset not checked into git")


def test_native_evidence_manifest_and_frozen_source_bytes_are_exact():
    manifest = json.loads((ROOT / "SHA256SUMS.json").read_text(encoding="utf-8"))
    for name, record in manifest.items():
        encoded = (ROOT / name).read_bytes()
        assert len(encoded) == record["bytes"]
        assert hashlib.sha256(encoded).hexdigest() == record["sha256"]
    for name in ("initial-eight-source.jsonl", "arr-four-source.jsonl"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            source = json.loads(line)
            decoded = "".join(source["source_lines"]).encode("utf-8")
            assert hashlib.sha256(decoded).hexdigest() == source["sha256"]


def test_initial_failures_remain_separate_from_cached_recovery():
    first = json.loads((ROOT / "initial-eight.json").read_text(encoding="utf-8"))
    replay = json.loads((ROOT / "final-eight-cached-replay.json").read_text(encoding="utf-8"))
    arr = json.loads((ROOT / "arr-four.json").read_text(encoding="utf-8"))
    assert first["summary"]["system_expected"] == 5 and first["summary"]["neural_raw_exact"] == 2
    assert len({row["query"] for row in first["rows"] if row["route"] == "neural_v2"}) == 3
    assert replay["summary"]["system_expected"] == 7 and replay["summary"]["raw_exact"] == 2
    assert "no new inference" in replay["kind"]
    ledger = json.loads((ROOT.parents[1] / "docs/receipt-export.json").read_text(encoding="utf-8"))
    exported = next(row for row in ledger["files"] if row["path"] == "experiments/native-workflows-v1/initial-eight.json")
    assert replay["original_receipt_sha256"] == exported["original_sha256"]
    assert exported["public_sha256"] == hashlib.sha256((ROOT / "initial-eight.json").read_bytes()).hexdigest()
    assert arr["summary"]["system_expected"] == 4 and arr["summary"]["neural_raw_exact"] == 3
