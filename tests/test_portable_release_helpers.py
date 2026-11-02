"""Release checks operate without model frameworks or an actual model."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

import pytest


def load_helper(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def string(value):
    encoded = value.encode()
    return struct.pack("<Q", len(encoded)) + encoded


def fixture_gguf(tmp_path, entries):
    data = struct.pack("<4sIQQ", b"GGUF", 3, 0, len(entries))
    for key, kind, encoded in entries:
        data += string(key) + struct.pack("<I", kind) + encoded
    data += b"\0" * (-len(data) % 32)
    path = tmp_path / "synthetic.gguf"
    path.write_bytes(data)
    return path


def test_gguf_audit_reads_all_arrays_without_flagging_security_vocabulary(tmp_path):
    helper = load_helper("audit_gguf_release")
    tokens = ["hello", "API_KEY", "password"]
    array = struct.pack("<IQ", 8, len(tokens)) + b"".join(string(token) for token in tokens)
    path = fixture_gguf(tmp_path, [("general.name", 8, string("Synthetic")), ("tokenizer.ggml.tokens", 9, array)])
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    report, index = helper.audit(path, expected_sha256=expected)
    assert report["release_candidate_checks_passed"]
    assert index["metadata"]["tokenizer.ggml.tokens"] == tokens
    assert report["metadata_summary"]["tokenizer.ggml.tokens"]["array_count"] == 3


def test_gguf_audit_detects_private_values_without_copying_them_into_report(tmp_path):
    helper = load_helper("audit_gguf_release")
    private = "/Users/synthetic-owner/private-work"
    path = fixture_gguf(tmp_path, [("general.description", 8, string(private))])
    report, _ = helper.audit(path)
    assert not report["release_candidate_checks_passed"]
    assert report["plaintext_scan"]["findings"][0]["signature"] == "private_home_path"
    assert private not in json.dumps(report)
    assert report["metadata_summary"]["general.description"]["redacted_due_to_plaintext_finding"]


def test_gguf_audit_detects_literal_in_tensor_bytes_and_hash_mismatch(tmp_path):
    helper = load_helper("audit_gguf_release")
    path = fixture_gguf(tmp_path, [])
    with path.open("ab") as output:
        output.write(b"synthetic-confidential-reference")
    report, _ = helper.audit(path, expected_sha256="0" * 64,
        literal_denials=[{"label": "fixture-only", "value": "synthetic-confidential-reference"}])
    assert not report["expected_sha256_matches"] and not report["release_candidate_checks_passed"]
    assert report["plaintext_scan"]["findings"][0]["region"] == "tensor_data"


def test_gguf_audit_rejects_truncated_and_duplicate_metadata(tmp_path):
    helper = load_helper("audit_gguf_release")
    path = fixture_gguf(tmp_path, [("name", 8, string("one")), ("name", 8, string("two"))])
    with pytest.raises(ValueError, match="Duplicate"):
        helper.read_index(path)
    path.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 0, 1) + string("name") + struct.pack("<IQ", 8, 100))
    with pytest.raises(ValueError, match="Truncated"):
        helper.read_index(path)


def test_cpu_smoke_rejects_wrong_artifact_before_executing_native_binary(tmp_path, monkeypatch):
    helper = load_helper("smoke_portable_model")
    path = tmp_path / "wrong.gguf"
    path.write_bytes(b"not the approved artifact")
    def unexpected(*args, **kwargs):
        raise AssertionError("A rejected artifact must not start a runtime")
    monkeypatch.setattr(helper.subprocess, "run", unexpected)
    with pytest.raises(ValueError, match="SHA-256"):
        helper.run_smoke(path, tmp_path / "not-installed")
