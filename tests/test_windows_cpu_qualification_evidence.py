"""Retained evidence must bind the actual artifact, environment and cleanup."""
import copy
import hashlib
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / "scripts/verify_windows_cpu_qualification.py"))
EVIDENCE = ROOT / "experiments/windows-cpu-v1"
pytestmark = pytest.mark.skipif(not EVIDENCE.exists(), reason="Local benchmark evidence not checked into git")


def test_public_metadata_export_reconstructs_actual_crlf_receipt_and_analysis():
    receipt, raw = helper["reconstruct_receipt"]()
    assert hashlib.sha256(raw).hexdigest() == "383cd92d2964f803dbaecb94e06ed0b89b93bc12bfc7e991749eee953d2ba550"
    assert len(receipt["source_file_sha256"]) == 29
    result = helper["verify"]()
    assert result["all_identity_and_operational_checks_passed"]
    assert result["native_model_calls"] == result["raw_expected_tool_matches"] == 4
    assert not result["memory_measured_by_smoke"]


@pytest.mark.parametrize("fault", ["artifact", "run", "source", "cuda", "heavy_dependency", "cleanup"])
def test_actual_receipt_mutants_cannot_qualify_another_run_or_runtime(fault):
    receipt, _ = helper["reconstruct_receipt"]()
    receipt = copy.deepcopy(receipt)
    if fault == "artifact":
        receipt["model"]["sha256"] = "0" * 64
    elif fault == "run":
        receipt["ci"]["GITHUB_RUN_ID"] = "1"
    elif fault == "source":
        receipt["source_file_sha256"].pop(next(iter(receipt["source_file_sha256"])))
    elif fault == "cuda":
        receipt["generation"]["device"] = "cuda"
    elif fault == "heavy_dependency":
        receipt["installed_distributions"].append({"name": "torch", "version": "synthetic"})
    else:
        receipt["checks"]["managed_native_process_stopped"] = False
    analyze = helper["original_analyzer"]()
    result = analyze(receipt, run_id="41436216278", workflow_sha="b937d5c855a584fb5fb5bef642dfd5282e59f443",
        source_manifest=json.loads((EVIDENCE / "product-source-manifest.json").read_text()),
        run_metadata=json.loads((EVIDENCE / "api-metadata.json").read_text()))
    assert not result["all_identity_and_operational_checks_passed"]
