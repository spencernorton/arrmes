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


def test_zero_markdown_pipe_tables_across_repo_docs() -> None:
    """Enforce zero markdown pipe tables (`^|`) across README, CHANGELOG, and docs."""
    md_files = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").rglob("*.md")),
    ]
    for md_p in md_files:
        if not md_p.is_file():
            continue
        for lineno, line in enumerate(md_p.read_text(encoding="utf-8").splitlines(), 1):
            assert not line.startswith("|"), f"Forbidden markdown pipe table at {md_p.relative_to(ROOT)}:{lineno}"


def test_v10_receipts_and_charts_parity() -> None:
    """Verify v10 receipts, cluster bootstrap CIs, provenance, runtime profiles, and stripped v10 PNG figures."""
    b10 = json.loads((ROOT / "experiments" / "bench_receipt_v10.json").read_text(encoding="utf-8"))
    av8 = json.loads((ROOT / "experiments" / "acceptance-v8" / "summary.json").read_text(encoding="utf-8"))
    rt10 = json.loads((ROOT / "experiments" / "runtime-profile-v10.json").read_text(encoding="utf-8"))
    pv10 = json.loads((ROOT / "experiments" / "mlx-lora-v10-provenance.json").read_text(encoding="utf-8"))

    assert av8["total_cases"] == 3135
    assert av8["positive_cases"] == 1685
    assert av8["negative_cases"] == 1450

    # Verify cryptographic seal verification, separate POSIX telemetry, & epistemically qualified adjudication on acceptance-v8
    assert av8["cryptographic_seal_verification"]["cryptographic_seal_intact"] is True
    assert av8["cryptographic_seal_verification"]["bootstrap_seed10010_deterministic_match"] is True
    assert "query_and_label_files_unopened_before_phase4" not in av8["cryptographic_seal_verification"]
    assert av8["recorded_filesystem_seal_telemetry"]["all_four_files_mode_0444"] is True
    assert av8["seal_adjudication"]["status"] == "WAIVED_AND_CRYPTOGRAPHICALLY_VERIFIED"
    assert "recorded_filesystem_telemetry" in av8["seal_adjudication"]
    assert "cryptographic_content_verification" in av8["seal_adjudication"]

    # Verify bench_receipt_v10 matches acceptance-v8 summary across ALL 5 models x ALL 4 metrics
    models_by_name = {m["name"]: m["metrics"] for m in b10["models"]}
    expected_map = {
        "NeuralRouterV3 v10": av8["models"]["arrmes_neural_router_v3_v10"],
        "HybridRouterMLX v10": av8["models"]["hybrid_router_mlx_qwen3_06b_v10"],
        "Qwen3-0.6B v10": av8["models"]["qwen3_06b_v10"],
        "FunctionGemma-270M v10": av8["models"]["functiongemma_270m_v10"],
        "Regex Baseline": av8["models"]["deterministic_regex_baseline"],
    }
    for m_label, m_src in expected_map.items():
        assert models_by_name[m_label]["pos_track_a"] == round(m_src["acceptance_v8_positive_1685"]["raw_accuracy"], 4)
        assert models_by_name[m_label]["pos_track_b"] == round(m_src["acceptance_v8_positive_1685"]["verified_accuracy"], 4)
        assert models_by_name[m_label]["neg_track_a"] == round(m_src["acceptance_v8_negative_1450"]["raw_accuracy"], 4)
        assert models_by_name[m_label]["neg_track_b"] == round(m_src["acceptance_v8_negative_1450"]["verified_accuracy"], 4)

    rtr = av8["models"]["arrmes_neural_router_v3_v10"]
    qw = av8["models"]["qwen3_06b_v10"]

    # Verify bench_receipt_v10_convergence.json matches v9 and v10 provenance receipts with zero drift
    conv10 = json.loads((ROOT / "experiments" / "bench_receipt_v10_convergence.json").read_text(encoding="utf-8"))
    v9p = json.loads((ROOT / "experiments" / "mlx-lora-v9-provenance.json").read_text(encoding="utf-8"))
    conv_by_name = {m["name"]: m["metrics"] for m in conv10["models"]}
    assert "N=300 slice" in conv10["subtitle"] and "N=2,580" in conv10["subtitle"]
    assert conv_by_name["Qwen3-0.6B v9 (N=300 slice)"]["ov1_ver"] == round(
        v9p["qwen3_06b_v9_qlora"]["evaluation"]["overnight_v1_80"]["track_b_verified_accuracy"], 4
    ) == 0.9
    assert conv_by_name["FG-270M v9 (N=300 slice)"]["ov1_ver"] == round(
        v9p["functiongemma_270m_v9_qlora"]["evaluation"]["overnight_v1_80"]["track_b_verified_accuracy"], 4
    ) == 0.8625
    assert conv_by_name["Qwen3-0.6B v10 (N=2,580)"]["av7_neg_raw"] == round(
        pv10["qwen3_06b_v10"]["dev_benchmarks"]["acceptance_v7_negative_full_1200"]["raw_accuracy"], 4
    )
    assert conv_by_name["FG-270M v10 (N=2,580)"]["av7_neg_raw"] == round(
        pv10["functiongemma_270m_v10"]["dev_benchmarks"]["acceptance_v7_negative_full_1200"]["raw_accuracy"], 4
    )

    # Verify cluster bootstrap CIs resolved multi-cluster IDs (>100 clusters, never collapsed to 1)
    assert rtr["acceptance_v8_positive_1685"]["verified_cluster_bootstrap_95"]["clusters"] >= 300
    assert rtr["acceptance_v8_negative_1450"]["verified_cluster_bootstrap_95"]["clusters"] >= 250
    assert qw["acceptance_v8_positive_1685"]["verified_cluster_bootstrap_95"]["clusters"] >= 300
    assert qw["acceptance_v8_negative_1450"]["verified_cluster_bootstrap_95"]["clusters"] >= 250

    # Verify provenance & runtime-profile-v10 budgets
    assert pv10["train_pool_v10"]["sft_rows"] == 12620
    assert pv10["train_pool_v10"]["dpo_pairs"] == 3100
    profs10 = rt10["profiles"]
    assert profs10["arrmes_neural_router_v3_macos_arm64_cpu"]["routing_and_verifier_ms"]["p50"] <= 0.75
    assert profs10["arrmes_neural_router_v3_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p50"] <= 1.0
    assert profs10["functiongemma_270m_v10_gguf_q4_k_m_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p50"] <= 250.0
    assert profs10["functiongemma_270m_v10_gguf_q4_k_m_linux_cuda"]["routing_and_verifier_ms"]["p50"] <= 55.0
    assert profs10["qwen3_06b_v10_gguf_q4_k_m_linux_cuda"]["routing_and_verifier_ms"]["p50"] <= 55.0
    for pk, pv in profs10.items():
        if pk.endswith(("_linux_x86_64_cpu", "_linux_cuda")) and "gguf" in pk:
            mem = pv["memory_accounting_mib"]
            assert mem["combined_resident_mib"] == round(mem["llama_server_peak_rss_mib"] + mem["python_app_rss_mib"], 2)

    # Verify v10 PNG figures exist and have zero metadata chunks
    for stem in (
        "arrmes_v10_acceptance_v8_accuracy_bounds",
        "arrmes_v10_v9_vs_v10_convergence",
        "arrmes_v10_runtime_latency_memory_footprint",
    ):
        png = ROOT / "docs" / "figures" / f"{stem}.png"
        assert png.is_file() and png.stat().st_size > 1024
        raw = png.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        pos = 8
        while pos < len(raw):
            length = struct.unpack(">I", raw[pos : pos + 4])[0]
            ctype = raw[pos + 4 : pos + 8]
            assert ctype not in (b"tEXt", b"zTXt", b"iTXt", b"tIME"), f"Unstripped chunk {ctype} in {png}"
            pos += 12 + length


def test_upstream_lab_v1_has_all_12_container_digests() -> None:
    """Verify every qualified container image in experiments/upstream-lab-v1/summary.json records a non-empty sha256 digest."""
    lab = json.loads((ROOT / "experiments" / "upstream-lab-v1" / "summary.json").read_text(encoding="utf-8"))
    images = lab["images"]
    assert len(images) == 12
    for img, digest in images.items():
        assert isinstance(digest, str) and "sha256:" in digest and len(digest) > 20, f"Missing image digest for {img}: {digest!r}"


def test_corpus_v3_capabilities_match_contract_and_disk() -> None:
    """Ensure V3 ApiCapabilityEntry definitions in corpus.py match contract.py and point to real symbols and test files."""
    import importlib
    from arrmes.corpus import API_CAPABILITIES
    from arrmes.services.contract import SERVICE_PERMISSIONS, SUPPORTED_OPERATIONS

    v3_entries = [
        e for e in API_CAPABILITIES
        if e.arrmes_action in {"service_query", "bazarr_subtitle_search", "bazarr_subtitle_download"}
    ]
    assert len(v3_entries) == 7
    covered_services: set[str] = set()
    for entry in v3_entries:
        if entry.arrmes_action == "service_query":
            if entry.service in SUPPORTED_OPERATIONS:
                covered_services.add(entry.service)
                ops = entry.parameters_schema["properties"]["operation"]["enum"]
                assert set(ops) == set(SUPPORTED_OPERATIONS[entry.service]), f"Operations mismatch for {entry.service}"
                assert entry.required_permission == SERVICE_PERMISSIONS[entry.service], f"Permission mismatch for {entry.service}"
            else:
                covered_services.update(entry.parameters_schema["properties"]["service"]["enum"])
        else:
            from arrmes.catalog import V3_BY_NAME
            assert entry.parameters_schema == V3_BY_NAME[entry.arrmes_action].tool()["function"]["parameters"], (
                f"Schema drift on {entry.capability_id} vs V3_BY_NAME[{entry.arrmes_action!r}]"
            )
        assert entry.adapter_entrypoint is not None
        mod_name, sym_name = entry.adapter_entrypoint.split(":", 1)
        obj = importlib.import_module(mod_name)
        for part in sym_name.split("."):
            assert hasattr(obj, part), f"Missing symbol {entry.adapter_entrypoint}"
            obj = getattr(obj, part)
        assert entry.sandbox_verification_test is not None
        test_rel = entry.sandbox_verification_test.split("::", 1)[0]
        assert (ROOT / test_rel).is_file(), f"Missing test file {test_rel} for {entry.capability_id}"
    assert covered_services == set(SUPPORTED_OPERATIONS.keys())


def test_acceptance_v7_and_v8_sha256_digests_immutable() -> None:
    """Verify pinned acceptance-v8 digests in summary.json and verify local dataset files when present on disk."""
    expected_av8 = {
        "cases-positive.jsonl": "44ad97e4a7b7f41c6c68e662ad973f8c4ce1fbdb9c085ecfecc017ae2f9dd610",
        "cases-negative.jsonl": "c6d42aa6363c260f0dccf89a8ddc646bb02910b7572c7bb88d2a967b39f5eb0e",
        "bootstrap_indices_seed10010.json": "841b983ce8c8ffb760ae07e38a5274a07b92294e5ef0c3fdcdc4c5a6412f6ebc",
        "manifest.json": "9a1a21e0a9ccf6caae6a3c431aa59555bafbade1880eeea700a7dc0e0b3a6309",
    }
    av8_sum = json.loads((ROOT / "experiments" / "acceptance-v8" / "summary.json").read_text(encoding="utf-8"))
    assert av8_sum["pinned_input_sha256_verified_at_eval"] == expected_av8
    assert "arrmes_neural_router_v3_v10" in av8_sum["pre_audit_oneshot_baseline"]

    optional_disk_files = {
        "data/acceptance-v7/cases-positive.jsonl": "f07774983d3e61b1bd1e5c55a35b1e5c8618cc6e6d95663eadbdca90978cbf52",
        "data/acceptance-v7/cases-negative.jsonl": "5f58d213d55f44ed3f9638944583141af9047d5aef9436e5ca4d358f72a99c04",
        **{f"data/acceptance-v8/{k}": v for k, v in expected_av8.items()},
    }
    for rel, exp in optional_disk_files.items():
        fp = ROOT / rel
        if fp.is_file():
            actual = hashlib.sha256(fp.read_bytes()).hexdigest()
            assert actual == exp, f"Immutable benchmark file {rel} changed: {actual} != {exp}"



def test_cluster_bootstrap_ci_legacy_precedence_and_v7_stability() -> None:
    """Ensure family_id/entity_family retain precedence over cluster_id and acceptance-v7 CIs remain deterministic."""
    from arrmes.evaluate import cluster_bootstrap_ci

    legacy_rows = [
        {"id": f"r-{i}", "family_id": f"fam-{i // 2}", "cluster_id": "collapsed-single-cluster", "exact": (i % 3 != 0)}
        for i in range(20)
    ]
    ci_leg = cluster_bootstrap_ci(legacy_rows, n_boot=500, seed=1379)
    assert ci_leg["clusters"] == 10

    v8_style_rows = [
        {"id": f"c-{i}", "cluster_id": f"av8-pos-{i // 5:04d}", "category": "radarr", "exact": (i % 5 != 0)}
        for i in range(50)
    ]
    ci_v8 = cluster_bootstrap_ci(v8_style_rows, n_boot=500, seed=1379)
    assert ci_v8["clusters"] == 10

    av7_pos_file = ROOT / "data/acceptance-v7/cases-positive.jsonl"
    av7_neg_file = ROOT / "data/acceptance-v7/cases-negative.jsonl"
    if av7_pos_file.is_file() and av7_neg_file.is_file():
        pos_rows = [{**json.loads(l), "exact": True} for l in av7_pos_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        neg_rows = [{**json.loads(l), "exact": True} for l in av7_neg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert cluster_bootstrap_ci(pos_rows, n_boot=200, seed=9009)["clusters"] == 276
        assert cluster_bootstrap_ci(neg_rows, n_boot=200, seed=9009)["clusters"] == 240


def test_oof_checkpoint_fingerprint_and_hf_sha_pinner(tmp_path: Path) -> None:
    """Verify OOF checkpoint invalidation on experiment drift and immutable 40-char HF revision pinning."""
    import os
    import re
    import pytest
    from pin_hf_revision_in_docs import pin_router_revision_in_markdown
    from train_v10_gpu import compute_oof_experiment_fingerprint, load_valid_oof_fold_checkpoints, sha256_file

    base_d = tmp_path / "base"
    base_d.mkdir()
    (base_d / "config.json").write_text('{"architectures":["Qwen3ForCausalLM"]}', encoding="utf-8")
    (base_d / "tokenizer.json").write_text('{"version":"1.0","vocab":{"a":0}}', encoding="utf-8")
    (base_d / "model.safetensors").write_bytes(b"SAFEMODEL_V1_WEIGHTS")
    sft_a = [{"id": "1", "query": "show sonarr queue", "fold": 0}]
    dpo_a = [{"id": "d1", "query": "delete movie", "fold": 0}]

    fp1 = compute_oof_experiment_fingerprint(
        backbone_key="qwen3_06b_v10",
        family="hermes",
        base_dir=base_d,
        sft_rows=sft_a,
        dpo_rows=dpo_a,
        oof_sft_epochs=1,
        dpo_epochs=1,
    )
    fp2 = compute_oof_experiment_fingerprint(
        backbone_key="qwen3_06b_v10",
        family="hermes",
        base_dir=base_d,
        sft_rows=sft_a + [{"id": "2", "query": "changed dataset", "fold": 1}],
        dpo_rows=dpo_a,
        oof_sft_epochs=1,
        dpo_epochs=1,
    )
    assert fp1 != fp2

    # Mutating weights or tokenizer while keeping config.json identical MUST change OOF fingerprint
    (base_d / "model.safetensors").write_bytes(b"SAFEMODEL_V2_MUTATED_WEIGHTS")
    fp_mut_weights = compute_oof_experiment_fingerprint(
        backbone_key="qwen3_06b_v10",
        family="hermes",
        base_dir=base_d,
        sft_rows=sft_a,
        dpo_rows=dpo_a,
        oof_sft_epochs=1,
        dpo_epochs=1,
    )
    assert fp_mut_weights != fp1
    (base_d / "model.safetensors").write_bytes(b"SAFEMODEL_V1_WEIGHTS")

    (base_d / "tokenizer.json").write_text('{"version":"1.0","vocab":{"a":0,"b":1}}', encoding="utf-8")
    fp_mut_tok = compute_oof_experiment_fingerprint(
        backbone_key="qwen3_06b_v10",
        family="hermes",
        base_dir=base_d,
        sft_rows=sft_a,
        dpo_rows=dpo_a,
        oof_sft_epochs=1,
        dpo_epochs=1,
    )
    assert fp_mut_tok != fp1
    (base_d / "tokenizer.json").write_text('{"version":"1.0","vocab":{"a":0}}', encoding="utf-8")

    rows_p = tmp_path / "qwen3_06b_v10_oof_rows_fold0.jsonl"
    rows_p.write_text('{"id":"1","verified_exact":true}\n', encoding="utf-8")
    ckpt_p = tmp_path / "qwen3_06b_v10_oof_ckpt.json"
    ckpt_p.write_text(
        json.dumps([{"fold": 0, "experiment_fingerprint": fp1, "rows_sha256": sha256_file(rows_p), "eval": {"ok": 1}}]),
        encoding="utf-8",
    )
    assert 0 in load_valid_oof_fold_checkpoints(tmp_path, "qwen3_06b_v10", fp1)
    # Different experiment fingerprint must reject stale checkpoint
    assert load_valid_oof_fold_checkpoints(tmp_path, "qwen3_06b_v10", fp2) == {}
    # Tampered or missing fold rows file must reject checkpoint
    rows_p.write_text('{"id":"1","verified_exact":false}\n', encoding="utf-8")
    assert load_valid_oof_fold_checkpoints(tmp_path, "qwen3_06b_v10", fp1) == {}

    doc_txt = (ROOT / "docs/portable-runtime.md").read_text(encoding="utf-8")
    dummy_sha = "a" * 40
    pinned_txt = pin_router_revision_in_markdown(doc_txt, dummy_sha)
    assert f"/resolve/{dummy_sha}/router_weights.npz" in pinned_txt
    assert f"/resolve/{dummy_sha}/router_config.json" in pinned_txt
    assert "/resolve/main/" not in pinned_txt
    with pytest.raises(ValueError):
        pin_router_revision_in_markdown(doc_txt, "main")

    # Verify verify_live_router_urls enforces BOTH router_weights.npz and router_config.json SHA-256 digests
    import io
    import urllib.request
    from unittest.mock import patch
    from pin_hf_revision_in_docs import verify_live_router_urls

    w_bytes = b"weights-payload"
    c_bytes = b"config-payload"
    w_sha = hashlib.sha256(w_bytes).hexdigest()
    c_sha = hashlib.sha256(c_bytes).hexdigest()

    def _fake_urlopen(req, timeout=60):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        data = w_bytes if url.endswith("router_weights.npz") else c_bytes
        cm = io.BytesIO(data)
        cm.status = 200
        return cm

    with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
        res = verify_live_router_urls(dummy_sha, expected_weights_sha256=w_sha, expected_config_sha256=c_sha)
        assert res == {"router_weights.npz": w_sha, "router_config.json": c_sha}
        with pytest.raises(RuntimeError, match="router_config.json digest mismatch"):
            verify_live_router_urls(dummy_sha, expected_weights_sha256=w_sha, expected_config_sha256="0" * 64)
        with pytest.raises(RuntimeError, match="router_weights.npz digest mismatch"):
            verify_live_router_urls(dummy_sha, expected_weights_sha256="0" * 64, expected_config_sha256=c_sha)

    if os.environ.get("ARRMES_REQUIRE_PINNED_HF_SHA") == "1":
        assert "/resolve/main/" not in doc_txt
        assert re.search(r"/resolve/[0-9a-f]{40}/router_weights\.npz", doc_txt)
        assert re.search(r"/resolve/[0-9a-f]{40}/router_config\.json", doc_txt)


def test_windows_v10_qualification_receipt_and_runtime_profile_parity() -> None:
    """Verify committed Windows Server 2025 AMD64 v10 qualification receipt matches runtime-profile-v10.json."""
    win = json.loads((ROOT / "experiments" / "windows-v10-qualification" / "summary.json").read_text(encoding="utf-8"))
    rt10 = json.loads((ROOT / "experiments" / "runtime-profile-v10.json").read_text(encoding="utf-8"))
    assert win["all_qualification_checks_passed"] is True
    assert win["environment_class"] == "hosted_ci_runner"
    assert win["hardware"]["system"] == "Windows"
    assert win["neural_router_v3_v10"]["acceptance_v7_2580"]["matches_macos_and_linux_bitwise"] is True
    assert (
        win["neural_router_v3_v10"]["acceptance_v7_2580"]["outputs_sha256"]
        == rt10["cross_platform_parity"]["router_av7_2580_outputs_sha256"]
        == "f23e6de6933347e47c2765727c7401cff02b22b5096a822c60a63335f19a9198"
    )
    profs = rt10["profiles"]
    assert (
        profs["functiongemma_270m_v10_gguf_q4_k_m_windows_x86_64_cpu"]["memory_accounting_mib"]["combined_peak_working_set_mib"]
        == win["functiongemma_270m_v10_q4_k_m"]["memory_accounting"]["combined_peak_working_set_mib"]
        <= 1536.0
    )
    assert (
        profs["qwen3_06b_v10_gguf_q4_k_m_windows_x86_64_cpu"]["memory_accounting_mib"]["combined_peak_working_set_mib"]
        == win["qwen3_06b_v10_q4_k_m"]["memory_accounting"]["combined_peak_working_set_mib"]
        <= 1536.0
    )
