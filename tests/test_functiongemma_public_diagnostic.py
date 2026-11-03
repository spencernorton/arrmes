"""Integrity and failure controls; no model, network or accelerator required."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (ROOT / "data/overnight-v1/routing-heldout.jsonl").exists() or not (ROOT / "experiments/functiongemma-public-v1/manifest.json").exists(), reason="Local benchmark dataset not checked into git")


@pytest.fixture
def diagnostic():
    spec = importlib.util.spec_from_file_location("functiongemma_diagnostic", ROOT / "scripts/run_functiongemma_public_diagnostic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_evidence_verifies_without_loading_a_model(diagnostic, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Verification must not start a process or model")
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = diagnostic.verify_bundle()
    assert result["models_loaded"] == result["generation_calls"] == 0
    assert result["rows_per_receipt"] == 80
    assert result["runtime_python_files"] == 19


def test_changed_raw_evidence_is_rejected(diagnostic, tmp_path, monkeypatch):
    import shutil
    target = tmp_path / "evidence"
    shutil.copytree(diagnostic.EVIDENCE, target)
    data = diagnostic.read_json(target / "public_v5.json")
    data["rows"][0]["raw_unverified"] += "changed"
    (target / "public_v5.json").write_text(json.dumps(data))
    monkeypatch.setattr(diagnostic, "EVIDENCE", target)
    with pytest.raises(ValueError, match="identity mismatch"):
        diagnostic.verify_bundle()


@pytest.mark.parametrize("field", ["raw", "gold", "timing", "context"])
def test_owner_check_detects_protected_row_changes(diagnostic, tmp_path, monkeypatch, field):
    evidence, originals = tmp_path / "public", tmp_path / "originals"
    evidence.mkdir(); originals.mkdir()
    original = diagnostic.read_json(diagnostic.EVIDENCE / "base.json")
    original["source_root"] = "/operator-runtime/src"
    original["source_file_sha256"] = {item["path"]: item["digest"] for item in original["source_file_sha256"]}
    for artifact in original["artifacts"]:
        artifact["file"] = "/operator-cache/" + Path(artifact["file"]).name
    source = originals / "base.json"
    source.write_text(json.dumps(original))
    public = diagnostic.project_receipt_metadata(original)
    if field == "raw":
        public["rows"][0]["raw_unverified"] += "changed"
    elif field == "gold":
        public["rows"][0]["expected"]["name"] = "unsupported"
    elif field == "timing":
        public["rows"][0]["latency"]["generation_seconds"] += 0.01
    else:
        public["rows"][0]["context"] = {"changed": True}
    (evidence / "base.json").write_text(json.dumps(public))
    ledger = {"receipt_records": [{"file": "base.json", "original_sha256": diagnostic.file_sha(source),
                                   "native_output_gold_and_timing_rows_sha256": diagnostic.object_sha(original["rows"])}]}
    monkeypatch.setattr(diagnostic, "EVIDENCE", evidence)
    with pytest.raises(ValueError, match="outside the declared metadata"):
        diagnostic.verify_originals(originals, ledger)


def test_source_proof_permits_only_one_fixture_literal(diagnostic):
    source = (diagnostic.EVIDENCE / "reproduction-harness.py").read_text()
    expected = diagnostic.normalized_harness_source(source)
    assert diagnostic.normalized_harness_source(source.replace("gateway_api_key='fixture'", "gateway_api_key='synthetic-test'")) == expected
    assert diagnostic.normalized_harness_source(source.replace("max_tokens=args.max_tokens", "max_tokens=17")) != expected
    with pytest.raises(ValueError):
        diagnostic.normalized_harness_source(source.replace("gateway_api_key=", "other_key="))


@pytest.mark.parametrize("kind", ["existing", "model-parent", "model-child", "evidence"])
def test_output_protection_preserves_existing_data(diagnostic, tmp_path, kind):
    model = tmp_path / "models"
    model.mkdir()
    marker = model / "keep.txt"
    marker.write_text("unchanged")
    if kind == "existing":
        target = model
    elif kind == "model-parent":
        target = tmp_path / "new-parent"
        protected = target / "models"
        with pytest.raises(ValueError):
            diagnostic.fresh_output(target, [protected])
        assert marker.read_text() == "unchanged"
        return
    elif kind == "model-child":
        target = model / "output"
    else:
        target = diagnostic.EVIDENCE / "new-output"
    with pytest.raises((ValueError, FileExistsError)):
        diagnostic.fresh_output(target, [model, diagnostic.EVIDENCE])
    assert marker.read_text() == "unchanged"


def test_output_rejects_symlink_parent(diagnostic, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit unprivileged test symlinks")
    with pytest.raises(ValueError, match="canonical"):
        diagnostic.fresh_output(link / "new", [])
    assert list(real.iterdir()) == []


def test_operator_services_and_proxies_are_not_inherited(diagnostic, monkeypatch, tmp_path):
    monkeypatch.setenv("ARRMES_GATEWAY_URL", "https://service.invalid")
    monkeypatch.setenv("ARRMES_SERVICES_FILE", "operator-config")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    environment = diagnostic.child_environment(tmp_path)
    assert not any(key.startswith("ARRMES_") for key in environment)
    assert "HTTPS_PROXY" not in environment
    assert environment["HF_HUB_OFFLINE"] == environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment["GIT_CEILING_DIRECTORIES"] == str(tmp_path.resolve())


def test_wrong_model_fails_before_output_or_generation(diagnostic, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(diagnostic.platform, "machine", lambda: "arm64")
    output = tmp_path / "never-created"
    with pytest.raises(ValueError, match="local base or public adapter"):
        diagnostic.reproduce(SimpleNamespace(base=tmp_path / "missing-base", adapter=tmp_path / "missing-adapter", output_dir=output))
    assert not output.exists()


def test_extra_intermediate_checkpoint_is_rejected(diagnostic, tmp_path):
    base, adapter = tmp_path / "base", tmp_path / "adapter"
    base.mkdir(); adapter.mkdir()
    (base / "model.safetensors").write_bytes(b"base")
    (adapter / "adapters.safetensors").write_bytes(b"adapter")
    records = [{"file": "local-model/" + group + "/" + name, "bytes": path.stat().st_size, "sha256": diagnostic.file_sha(path)}
               for group, name, path in [("base", "model.safetensors", base / "model.safetensors"),
                                          ("public-adapter", "adapters.safetensors", adapter / "adapters.safetensors")]]
    diagnostic.verify_model_files(base, adapter, records)
    (adapter / "intermediate.safetensors").write_bytes(b"unused")
    with pytest.raises(ValueError, match="Unexpected model"):
        diagnostic.verify_model_files(base, adapter, records)


@pytest.mark.parametrize("mid_generation", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="The original MLX controller uses POSIX alarm deadlines")
def test_controller_preserves_failures_and_cleanup(diagnostic, tmp_path, mid_generation):
    marker = tmp_path / "active.txt"
    fake = tmp_path / "fake_harness.py"
    body = "from pathlib import Path\nclass ControlledFailure(Exception): pass\ndef run(args):\n"
    if mid_generation:
        body += "    rows=[{'id':'completed-synthetic-row','raw_unverified':'synthetic-call'}]\n"
        body += "    warmups=[{'query':'synthetic-warmup'}]\n"
        body += "    marker=Path(" + repr(str(marker)) + ")\n    marker.write_text('active')\n"
        body += "    try:\n        raise ControlledFailure('private-detail')\n    finally:\n        marker.unlink()\n"
    else:
        body += "    raise ControlledFailure('private-detail')\n"
    fake.write_text(body)
    plan = {"source_root": str(tmp_path), "harness_path": str(fake), "base_path": str(tmp_path),
            "conditions": {"base": {"adapter_path": None, "label": "synthetic", "model_revision": "synthetic"}},
            "dataset_path": str(tmp_path / "unused"), "oracle_path": str(tmp_path / "unused"),
            "maximum_seconds_per_condition": 10,
            "frozen_files": [{"path": str(fake), "bytes": fake.stat().st_size, "sha256": diagnostic.file_sha(fake)}]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    result = subprocess.run([sys.executable, str(diagnostic.EVIDENCE / "condition-controller.py"),
                             "--plan", str(path), "--plan-sha256", diagnostic.file_sha(path), "--condition", "base"],
                            capture_output=True, text=True, timeout=20)
    failure = diagnostic.read_json(tmp_path / "base-failure.json")
    assert result.returncode == 1
    assert failure["all_checks_passed"] is False
    assert failure["error_type"] == "ControlledFailure"
    assert failure["stage"] == "harness_run"
    assert len(failure["partial"].get("rows", [])) == int(mid_generation)
    assert "private-detail" not in json.dumps(failure)
    assert not marker.exists()
