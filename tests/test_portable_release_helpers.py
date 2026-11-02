"""Release checks operate without model frameworks or an actual model."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

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
    report = helper.run_smoke(path, tmp_path / "not-installed")
    assert not report["all_checks_passed"]
    assert report["failure"] == {"stage": "model_identity", "error_type": "ValueError"}
    assert report["hardware"] and report["rows"] == []
    assert "source_file_sha256" not in report


PRIVATE_ERROR = "synthetic-private-marker /private/owner/file Authorization: Bearer private-value https://private.invalid"


def fake_cpu_run(tmp_path, monkeypatch, *, startup_failure=False, fail_generation=None, close_failure=False):
    """Controlled resources only; never starts a native process or inference."""
    import arrmes.backends
    import arrmes.executor
    import arrmes.sandbox
    from arrmes.protocol import Call, encode

    helper = load_helper("smoke_portable_model")
    model_path = tmp_path / "fixture.gguf"
    model_path.write_bytes(b"synthetic-model-for-failure-tests-only")
    executable = tmp_path / "fixture-native"
    executable.write_bytes(b"not-executable")
    expected = hashlib.sha256(model_path.read_bytes()).hexdigest()
    state = {"model_close_calls": 0, "cluster_stop_calls": 0}

    class FakeCluster:
        def __init__(self, path):
            state["temporary_path"] = path
            self._server = self._thread = None
            self.port = 43210
            self.base_url = "http://127.0.0.1:43210"

        def start(self):
            self._server = self._thread = object()
            return self

        def stop(self):
            state["cluster_stop_calls"] += 1
            self._server = self._thread = None

    class FakeModel:
        backend, device, endpoint = "llama-server", "cpu", "http://127.0.0.1:43211"

        def __init__(self):
            self.calls = 0
            self.closed = False
            self._process = SimpleNamespace(poll=lambda: 0 if self.closed else None)

        def infer(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == fail_generation:
                raise RuntimeError(PRIVATE_ERROR)
            call = [Call("now_playing", {}), Call("tv_missing", {"title": "Sintel"}),
                    Call("unsupported", {"reason": "unsafe"}), Call("unsupported", {"reason": "unsafe"})][self.calls - 1]
            raw = encode(call, "hermes")
            return {"raw": raw, "raw_unverified": raw, "generation_tokens": 4}

        def close(self):
            state["model_close_calls"] += 1
            self.closed = True
            if close_failure:
                raise RuntimeError(PRIVATE_ERROR)

    def create_model(*_args, **_kwargs):
        if startup_failure:
            raise RuntimeError(PRIVATE_ERROR)
        state["model"] = FakeModel()
        return state["model"]

    class FakeExecutor:
        def __init__(self, _adapter, *, allow_writes):
            assert allow_writes is False

        def execute_raw_proposal(self, *_args, **_kwargs):
            return SimpleNamespace(terminal_state="completed", steps_executed=1,
                                   observation=SimpleNamespace(data={"source": "controlled_fixture"}))

    monkeypatch.setattr(helper.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(stdout="llama-server b11429 d812350"))
    monkeypatch.setattr(arrmes.backends, "create_model", create_model)
    monkeypatch.setattr(arrmes.sandbox, "SandboxCluster", FakeCluster)
    monkeypatch.setattr(arrmes.executor, "BoundedExecutor", FakeExecutor)
    return helper, model_path, executable, expected, state


def test_cpu_smoke_runtime_identity_failure_retains_minimal_private_safe_receipt(tmp_path, monkeypatch):
    helper, model, executable, expected, state = fake_cpu_run(tmp_path, monkeypatch)
    def fail(*_args, **_kwargs):
        raise OSError(PRIVATE_ERROR)
    monkeypatch.setattr(helper.subprocess, "run", fail)
    report = helper.run_smoke(model, executable, expected_sha256=expected)
    assert report["failure"] == {"stage": "runtime_identity", "error_type": "OSError"}
    assert not report["all_checks_passed"] and report["hardware"] and report["model"]["sha256"] == expected
    assert "source_file_sha256" not in report and state["cluster_stop_calls"] == 0
    assert PRIVATE_ERROR not in json.dumps(report) and "/private/owner" not in json.dumps(report)


def test_cpu_smoke_cli_failed_startup_retains_source_hardware_and_nonzero_exit(tmp_path, monkeypatch, capsys):
    helper, model, executable, expected, state = fake_cpu_run(tmp_path, monkeypatch, startup_failure=True)
    original = helper.run_smoke
    monkeypatch.setattr(helper, "run_smoke", lambda *args, **kwargs:
                        original(*args, **kwargs, expected_sha256=expected))
    output = tmp_path / "startup-failure.json"
    monkeypatch.setattr(sys, "argv", ["smoke", "--model", str(model), "--llama-server", str(executable), "--output", str(output)])
    with pytest.raises(SystemExit) as result:
        helper.main()
    assert result.value.code == 1
    report = json.loads(output.read_text())
    assert report["failure"] == {"stage": "model_startup", "error_type": "RuntimeError"}
    assert not report["all_checks_passed"] and report["rows"] == []
    assert report["source_file_sha256"] and report["source_manifest_sha256"] and report["hardware"]
    assert report["checks"]["sandbox_stopped"] and report["checks"]["temporary_sandbox_removed"]
    assert state["cluster_stop_calls"] == 1 and not state["temporary_path"].exists()
    assert state["model_close_calls"] == 0
    assert PRIVATE_ERROR not in output.read_text() + capsys.readouterr().out


def test_cpu_smoke_mid_generation_failure_preserves_prior_rows_and_cleans_every_resource(tmp_path, monkeypatch):
    helper, model, executable, expected, state = fake_cpu_run(tmp_path, monkeypatch, fail_generation=2)
    report = helper.run_smoke(model, executable, expected_sha256=expected)
    assert report["failure"] == {"stage": "generation", "error_type": "RuntimeError"}
    assert report["active_case"] == "missing_route" and not report["all_checks_passed"]
    assert len(report["rows"]) == 2 and report["rows"][0]["inference"]["raw_unverified"]
    assert report["rows"][0]["fixture_execution"]["terminal_state"] == "completed"
    assert report["rows"][1]["id"] == "missing_route" and "inference" not in report["rows"][1]
    assert report["source_file_sha256"] and report["hardware"]
    assert all(report["checks"][key] for key in ("managed_native_process_stopped", "sandbox_stopped", "temporary_sandbox_removed"))
    assert state["model_close_calls"] == state["cluster_stop_calls"] == 1
    assert state["model"].closed and not state["temporary_path"].exists()
    assert report["network_isolation"]["blocked_target_details_omitted"]
    assert PRIVATE_ERROR not in json.dumps(report)


def test_cpu_smoke_cleanup_failure_is_not_a_pass_and_remaining_cleanup_still_runs(tmp_path, monkeypatch):
    helper, model, executable, expected, state = fake_cpu_run(tmp_path, monkeypatch, close_failure=True)
    report = helper.run_smoke(model, executable, expected_sha256=expected)
    assert report["failure"] == {"stage": "native_shutdown", "error_type": "RuntimeError"}
    assert not report["all_checks_passed"] and report["checks"]["managed_native_process_stopped"] is False
    assert report["checks"]["sandbox_stopped"] and report["checks"]["temporary_sandbox_removed"]
    assert len(report["rows"]) == 4 and state["cluster_stop_calls"] == 1 and not state["temporary_path"].exists()
    assert PRIVATE_ERROR not in json.dumps(report)


def test_cpu_smoke_success_keeps_all_eight_checks_and_cleanup(tmp_path, monkeypatch):
    helper, model, executable, expected, state = fake_cpu_run(tmp_path, monkeypatch)
    report = helper.run_smoke(model, executable, expected_sha256=expected)
    assert report["all_checks_passed"] and len(report["checks"]) == 8
    assert len(report["rows"]) == 4 and "failure" not in report
    assert state["model_close_calls"] == state["cluster_stop_calls"] == 1 and not state["temporary_path"].exists()
