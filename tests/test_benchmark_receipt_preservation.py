"""Diagnostic rescoring must preserve the versioned original evidence."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_cached_business_rescore_writes_new_copy_and_refuses_overwrite(tmp_path):
    source = tmp_path / "original" / "released-arrmes.json"
    source.parent.mkdir()
    blob = (ROOT / "experiments/overnight-v1/released-arrmes.json").read_bytes()
    source.write_bytes(blob)
    output = tmp_path / "diagnostic"
    command = [sys.executable, str(ROOT / "scripts/rescore_benchmark_observations.py"),
               str(source), "--output-dir", str(output)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert source.read_bytes() == blob
    result = json.loads((output / source.name).read_bytes())
    assert result["diagnostic_source_receipt_sha256"] == hashlib.sha256(blob).hexdigest()
    original = json.loads(blob)
    for before, after in zip(original["rows"], result["rows"]):
        for field in ("expected", "query", "context", "raw_unverified", "system_output",
                      "track_A", "track_B", "runtime"):
            assert before[field] == after[field]
        assert {k: v for k, v in before["latency"].items() if k != "measurement_note"} == {
            k: v for k, v in after["latency"].items() if k != "measurement_note"}
    saved = (output / source.name).read_bytes()
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode != 0
    assert (output / source.name).read_bytes() == saved
    assert source.read_bytes() == blob
