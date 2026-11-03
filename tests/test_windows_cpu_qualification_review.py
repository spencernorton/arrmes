"""Independent regressions for fixed-experiment trust and authenticated code bytes."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / "scripts/verify_windows_cpu_qualification.py"))
EVIDENCE = ROOT / "experiments/windows-cpu-v1"
pytestmark = pytest.mark.skipif(not EVIDENCE.exists(), reason="Local benchmark evidence not checked into git")
CANARY = b"\nraise AssertionError('forged-analyzer-code-ran')\n"


def copied_evidence(tmp_path):
    target = tmp_path / "evidence"
    shutil.copytree(EVIDENCE, target)
    return target


def update_manifest_row(directory, filename):
    manifest = directory / "manifest.json"
    document = json.loads(manifest.read_text())
    data = (directory / filename).read_bytes()
    record = next(row for row in document["files"] if row["file"] == filename)
    record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest.write_bytes((json.dumps(document, indent=2) + "\n").encode())


def test_known_evidence_may_be_relocated_without_reclassifying_hardware(tmp_path):
    result = helper["verify"](copied_evidence(tmp_path))
    assert result["all_identity_and_operational_checks_passed"]
    assert result["native_model_calls"] == result["raw_expected_tool_matches"] == 4
    assert result["verified_expected_tool_matches"] == 4
    assert result["reconstructed_receipt_sha256"] == "383cd92d2964f803dbaecb94e06ed0b89b93bc12bfc7e991749eee953d2ba550"
    assert result["memory_measured_by_smoke"] is False
    assert result["scope"] == "Offline reproduction of retained CI evidence, not a new Windows run"


@pytest.mark.parametrize("entrypoint", ["verify", "original_analyzer"])
def test_self_signed_manifest_cannot_nominate_executable_analyzer(tmp_path, entrypoint):
    directory = copied_evidence(tmp_path)
    analyzer = directory / "analyze_receipt_original.py"
    analyzer.write_bytes(analyzer.read_bytes() + CANARY)
    # The old verifier trusted precisely this self-consistent new checksum.
    update_manifest_row(directory, analyzer.name)
    with pytest.raises(ValueError, match="pinned|manifest|recorded"):
        helper[entrypoint](directory)


def test_consistent_source_receipt_ledger_and_manifest_forgery_cannot_qualify(tmp_path):
    directory = copied_evidence(tmp_path)
    public_path = directory / "windows-cpu-model-smoke-public.json"
    public = json.loads(public_path.read_text())
    public["source_file_sha256_records"][0]["sha256"] = "0" * 64
    mapping = {row["file"]: row["sha256"] for row in public["source_file_sha256_records"]}
    public["source_manifest_sha256"] = hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()
    public_path.write_bytes((json.dumps(public, indent=2) + "\n").encode())
    source_path = directory / "product-source-manifest.json"
    source = json.loads(source_path.read_text())
    source["files"][0]["sha256"] = "0" * 64
    source_path.write_bytes((json.dumps(source, indent=2) + "\n").encode())
    original = {("source_file_sha256" if key == "source_file_sha256_records" else key):
                (mapping if key == "source_file_sha256_records" else value) for key, value in public.items()}
    raw = (json.dumps(original, indent=2) + "\n").replace("\n", "\r\n").encode()
    ledger_path = directory / "receipt-export.json"
    ledger = json.loads(ledger_path.read_text())
    ledger.update(original_sha256=hashlib.sha256(raw).hexdigest(), original_bytes=len(raw),
                  public_sha256=hashlib.sha256(public_path.read_bytes()).hexdigest(), public_bytes=public_path.stat().st_size)
    ledger_path.write_bytes((json.dumps(ledger, indent=2) + "\n").encode())
    for filename in [public_path.name, source_path.name, ledger_path.name]:
        update_manifest_row(directory, filename)
    # An original field-consistency analysis alone still accepts this fake
    # source map. The outer fixed-manifest identity must reject it.
    retained = json.loads((EVIDENCE / "analysis-original.json").read_text())
    control = helper["original_analyzer"]()(original, run_id=retained["run_id"],
        workflow_sha=retained["workflow_sha"], source_manifest=source,
        run_metadata=json.loads((EVIDENCE / "api-metadata.json").read_text()))
    assert control["all_identity_and_operational_checks_passed"]
    with pytest.raises(ValueError, match="pinned|manifest"):
        helper["verify"](directory)


def test_verified_analyzer_buffer_is_executed_without_path_reread(tmp_path, monkeypatch):
    directory = copied_evidence(tmp_path)
    namespace = helper["verify"].__globals__
    original_loader = namespace["load_payloads"]
    calls = []
    def load_then_replace_path(selected=EVIDENCE):
        payloads = original_loader(selected)
        calls.append(selected)
        analyzer = Path(selected) / "analyze_receipt_original.py"
        analyzer.write_bytes(payloads[analyzer.name] + CANARY)
        return payloads
    monkeypatch.setitem(namespace, "load_payloads", load_then_replace_path)
    result = helper["verify"](directory)
    assert result["all_identity_and_operational_checks_passed"]
    assert len(calls) == 1
    assert (directory / "analyze_receipt_original.py").read_bytes().endswith(CANARY)


def test_analyzer_pin_applies_even_to_an_explicit_buffer():
    payloads = copy.deepcopy(helper["load_payloads"]())
    payloads["analyze_receipt_original.py"] += CANARY
    with pytest.raises(ValueError, match="analyzer|recorded"):
        helper["original_analyzer"](payloads=payloads)
