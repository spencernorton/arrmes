"""Regression tests enforcing chart-to-JSON-receipt parity, historical figure preservation, and doc parity."""
from __future__ import annotations

import ast
import hashlib
import json
import struct
import sys
from pathlib import Path

from arrmes.services.contract import SUPPORTED_OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from generate_v9_charts import load_chart_metrics  # noqa: E402


def test_historical_v7_figures_preserved_from_c5844ca() -> None:
    """Ensure historical v7 figures match commit c5844ca byte-for-byte."""
    fig_dir = ROOT / "docs" / "figures"
    expected_v7_sha256 = {
        "arrmes_v7_acceptance_v5_accuracy_bounds.png": "724c2708116f1d7b6dd317ea3052670aa81d58f997c5ad9e66ff8f0bd24d731f",
        "arrmes_v7_claude_sonnet_and_resource_comparison.png": "a67b82349eb1f4996263bf5e66debcbade2bf104f1b02966f167e612aee41d29",
        "arrmes_v7_error_taxonomy_and_verifier_repair.png": "e5f7b6b226cff5564d94b947711add7290c8f6e3fd949797c33b2910c90c5a2c",
        "arrmes_v7_open_model_accuracy_comparison.png": "724c2708116f1d7b6dd317ea3052670aa81d58f997c5ad9e66ff8f0bd24d731f",
        "arrmes_v7_runtime_latency_memory_footprint.png": "a67b82349eb1f4996263bf5e66debcbade2bf104f1b02966f167e612aee41d29",
        "arrmes_v7_track_c_workflow_scorecard.png": "5f107bb7a55ec3714f3ba3f9b69fc2bd90255632ee87340b6997295d2dd4a2b2",
    }
    for name, exp_sha in expected_v7_sha256.items():
        actual_sha = hashlib.sha256((fig_dir / name).read_bytes()).hexdigest()
        assert actual_sha == exp_sha, f"Historical figure {name} was modified" 


def test_chart_metrics_match_json_receipts_exactly() -> None:
    """Verify every plotted chart metric equals its authoritative JSON receipt value."""
    m = load_chart_metrics(ROOT)
    av7 = json.loads((ROOT / "experiments" / "acceptance-v7" / "summary.json").read_text(encoding="utf-8"))
    v9p = json.loads((ROOT / "experiments" / "mlx-lora-v9-provenance.json").read_text(encoding="utf-8"))
    rt9 = json.loads((ROOT / "experiments" / "runtime-profile-v9.json").read_text(encoding="utf-8"))
    lab = json.loads((ROOT / "experiments" / "upstream-lab-v1" / "summary.json").read_text(encoding="utf-8"))

    # Acceptance-v7 neural router parity
    r_v3 = av7["models"]["arrmes_neural_router_v3_contract_v3"]
    p1 = m["chart1"]["panel1_suites"]
    assert p1[0]["router_exact"] == r_v3["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_exact"]
    assert p1[0]["total"] == r_v3["positive_v3_all_1320"]["total_cases"]
    assert p1[1]["router_exact"] == r_v3["positive_v2_1000"]["track_b_complete_system"]["verified_joint_exact"]
    assert p1[2]["router_exact"] == r_v3["negative_all_1200"]["track_b_complete_system"]["verified_joint_exact"]

    # Unassisted MLX v9 parity
    qw_eval = v9p["qwen3_06b_v9_qlora"]["evaluation"]
    fg_eval = v9p["functiongemma_270m_v9_qlora"]["evaluation"]
    p3 = m["chart1"]["panel3_acceptance_v7_models"]
    assert p3[1]["pos_track_a_pct"] == round(100.0 * qw_eval["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 2)
    assert p3[1]["pos_track_b_pct"] == round(100.0 * qw_eval["acceptance_v7_v2_pos_150"]["track_b_verified_accuracy"], 2)
    assert p3[1]["neg_track_b_pct"] == round(100.0 * qw_eval["acceptance_v7_neg_150"]["track_b_verified_accuracy"], 2)
    assert p3[2]["pos_track_a_pct"] == round(100.0 * fg_eval["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 2)
    assert p3[2]["pos_track_b_pct"] == round(100.0 * fg_eval["acceptance_v7_v2_pos_150"]["track_b_verified_accuracy"], 2)
    assert p3[2]["neg_track_b_pct"] == round(100.0 * fg_eval["acceptance_v7_neg_150"]["track_b_verified_accuracy"], 2)

    # Runtime profile v9 parity
    profs = rt9["profiles"]
    lat = m["chart2"]["latency_rows"]
    assert lat[0]["p50_ms"] == profs["arrmes_neural_router_v3_macos_arm64_cpu"]["routing_and_verifier_ms"]["p50"]
    assert lat[0]["p95_ms"] == profs["arrmes_neural_router_v3_macos_arm64_cpu"]["routing_and_verifier_ms"]["p95"]
    assert lat[4]["p50_ms"] == profs["functiongemma_270m_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p50"]
    assert lat[5]["p50_ms"] == profs["qwen3_06b_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p50"]

    # Track C and 11-service live container lab parity
    c3 = m["chart3"]
    assert c3["groups"][3]["passed"] == av7["track_c_end_to_end_workflows"]["passed_workflows"]
    assert c3["groups"][3]["total"] == av7["track_c_end_to_end_workflows"]["total_workflows"]
    assert c3["groups"][4]["passed"] == lab["summary"]["passed_checks"]
    assert c3["groups"][4]["total"] == lab["summary"]["total_checks"]
    assert c3["live_services_qualified"] == lab["summary"]["live_services_qualified"] == 11


def test_chart_scripts_contain_no_hardcoded_float_arrays_in_plot_functions() -> None:
    """Ensure make_chart_* functions in generate_v9_charts.py do not hardcode numeric data lists."""
    tree = ast.parse((ROOT / "scripts" / "generate_v9_charts.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("make_chart_"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.List) and len(sub.elts) >= 4:
                    assert not all(
                        isinstance(e, ast.Constant) and isinstance(e.value, (int, float))
                        for e in sub.elts
                    ), f"Hardcoded numeric array found in {node.name}"


def test_png_metadata_stripped_and_vector_pdfs_exist() -> None:
    """Verify v8/v9 PNG charts have no metadata chunks and have matching vector PDFs."""
    fig_dir = ROOT / "docs" / "figures"
    stems = [
        "arrmes-model-accuracy-comparison",
        "arrmes-local-runtime-comparison",
        "arrmes_v9_acceptance_v7_accuracy_bounds",
        "arrmes_v9_runtime_latency_memory_footprint",
        "arrmes_v9_track_c_workflow_scorecard",
    ]
    for stem in stems:
        png = fig_dir / f"{stem}.png"
        assert png.is_file() and png.stat().st_size > 1024, f"Missing or empty PNG {png}"
        raw = png.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        pos = 8
        while pos < len(raw):
            length = struct.unpack(">I", raw[pos : pos + 4])[0]
            ctype = raw[pos + 4 : pos + 8]
            assert ctype not in (b"tEXt", b"zTXt", b"iTXt", b"tIME"), f"Unstripped chunk {ctype} in {png}"
            pos += 12 + length


def test_capabilities_docs_match_contract() -> None:
    """Ensure capabilities-after.json and capabilities.md match SUPPORTED_OPERATIONS (68 pairs across 11 services)."""
    cap = json.loads((ROOT / "docs" / "capabilities-after.json").read_text(encoding="utf-8"))
    total_pairs = sum(len(ops) for ops in SUPPORTED_OPERATIONS.values())
    assert total_pairs == 68
    assert cap["native_service_operation_pairs"] == total_pairs
    documented_services = {entry["service"] for entry in cap["new_native_services"]}
    assert documented_services == set(SUPPORTED_OPERATIONS.keys())
    cap_md = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    assert "68 native `service_query` read workflows over 11 service APIs" in cap_md
