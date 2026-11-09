#!/usr/bin/env python3
"""Generate publication-grade PNG + vector PDF visual charts for Arrmes v1.6.0 (v9 models & acceptance-v7).

Every plotted number is loaded directly from committed JSON receipts via `load_chart_metrics()`
and verified by `tests/test_chart_receipt_parity.py`. Historical `docs/figures/arrmes_v7_*.png`
files are never modified by this script.
"""
from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT_DIR / "docs" / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
        "axes.edgecolor": "#CBD5E1",
        "axes.linewidth": 0.9,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": "#CBD5E1",
        "grid.linestyle": "--",
        "grid.alpha": 0.55,
        "figure.facecolor": "#FFFFFF",
        "axes.facecolor": "#FFFFFF",
        "savefig.facecolor": "#FFFFFF",
    })
except ImportError:  # pragma: no cover - minimal CI environments without matplotlib
    matplotlib = None  # type: ignore[assignment]
    plt = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


def _strip_png_metadata(png_path: Path) -> None:
    """Strip tEXt, zTXt, iTXt, and tIME chunks from PNG for deterministic privacy-clean output."""
    raw = png_path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return
    out = bytearray(raw[:8])
    pos = 8
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        chunk_data = raw[pos + 8 : pos + 8 + length]
        crc = raw[pos + 8 + length : pos + 12 + length]
        pos += 12 + length
        if ctype in (b"tEXt", b"zTXt", b"iTXt", b"tIME"):
            continue
        out.extend(struct.pack(">I", length))
        out.extend(ctype)
        out.extend(chunk_data)
        out.extend(crc)
    png_path.write_bytes(bytes(out))


def _save_png_and_pdf(fig: plt.Figure, stems: list[str], extra_dir: Path | None = None) -> None:
    for s in stems:
        assert not s.startswith("arrmes_v7_"), f"Refusing to overwrite historical v7 figure: {s}"
        out_png = FIG_DIR / f"{s}.png"
        fig.savefig(out_png, dpi=240, bbox_inches="tight")
        _strip_png_metadata(out_png)
        if extra_dir is not None and extra_dir.exists():
            shutil.copy2(out_png, extra_dir / f"{s}.png")
            fig.savefig(
                extra_dir / f"{s}.pdf",
                format="pdf",
                bbox_inches="tight",
                metadata={"Creator": "", "Producer": "", "Title": s, "CreationDate": None},
            )
    plt.close(fig)


def _parse_ratio(val: str) -> tuple[int, int]:
    num, den = val.split("/")
    return int(num), int(den)


def load_chart_metrics(root: Path = ROOT_DIR) -> dict[str, Any]:
    """Load all chart series directly from committed JSON receipts (zero hardcoded numbers)."""
    av7 = json.loads((root / "experiments" / "acceptance-v7" / "summary.json").read_text(encoding="utf-8"))
    av6 = json.loads((root / "experiments" / "acceptance-v6" / "summary.json").read_text(encoding="utf-8"))
    av5 = json.loads((root / "experiments" / "acceptance-v5" / "summary.json").read_text(encoding="utf-8"))
    v9p = json.loads((root / "experiments" / "mlx-lora-v9-provenance.json").read_text(encoding="utf-8"))
    v8p = json.loads((root / "experiments" / "mlx-lora-v8-provenance.json").read_text(encoding="utf-8"))
    rt9 = json.loads((root / "experiments" / "runtime-profile-v9.json").read_text(encoding="utf-8"))
    lab = json.loads((root / "experiments" / "upstream-lab-v1" / "summary.json").read_text(encoding="utf-8"))

    r_av7 = av7["models"]["arrmes_neural_router_v3_contract_v3"]
    d_av7 = av7["models"]["deterministic_baseline_v2"]
    r_av6 = av6["models"]["arrmes_neural_router_v3_contract_v3"]
    d_av6 = av6["models"].get("deterministic_baseline_v2") or av6["models"].get("deterministic_rule_router_v2")
    r_av5 = av5["models"]["arrmes_neural_router_v3_contract_v3"]
    d_av5 = av5["models"].get("deterministic_rule_router_v2") or av5["models"].get("deterministic_baseline_v2")

    qw_v9 = v9p["qwen3_06b_v9_qlora"]["evaluation"]
    fg_v9 = v9p["functiongemma_270m_v9_qlora"]["evaluation"]
    qw_v8 = v8p["qwen3_06b_v8"]["evaluation"]
    fg_v8 = v8p["functiongemma_270m_v8"]["evaluation"]

    profs = rt9["profiles"]
    cat = rt9["large_library_catalog_index"]

    tc = av7["track_c_end_to_end_workflows"]
    r_p, r_t = tc["v2_sandbox_reads"]["completed"], tc["v2_sandbox_reads"]["total"]
    w_p, w_t = tc["v2_sandbox_writes"]["confirmed_and_verified"], tc["v2_sandbox_writes"]["total"]
    sq_p, sq_t = tc["v3_native_service_query_pairs"]["completed"], tc["v3_native_service_query_pairs"]["total"]
    ext_bools = [
        tc["v3_bazarr_subtitle_workflows"]["search_completed"],
        tc["v3_bazarr_subtitle_workflows"]["confirmed_download_completed"],
        tc["readarr_book_search_confirmed"],
        tc["seerr_tv_season_request_confirmed"],
        tc["seerr_request_cancellation_confirmed"],
        tc["bounded_read_to_confirm_workflow_completed"],
        tc["degraded_service_fault_drill_passed"],
    ]
    ext_passed = sum(1 for b in ext_bools if b)
    ext_total = len(ext_bools)

    return {
        "chart1": {
            "panel1_suites": [
                {
                    "label": f"Locked acceptance-v7 ★\nPositive v3 (N={r_av7['positive_v3_all_1320']['total_cases']:,})",
                    "router_pct": round(100.0 * r_av7["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "router_exact": r_av7["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_exact"],
                    "total": r_av7["positive_v3_all_1320"]["total_cases"],
                    "wilson_lb_pct": round(100.0 * r_av7["positive_v3_all_1320"]["track_b_complete_system"]["wilson_95_lower"], 2),
                    "det_pct": round(100.0 * d_av7["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
                {
                    "label": f"Locked acceptance-v7 ★\nPositive v2 (N={r_av7['positive_v2_1000']['total_cases']:,})",
                    "router_pct": round(100.0 * r_av7["positive_v2_1000"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "router_exact": r_av7["positive_v2_1000"]["track_b_complete_system"]["verified_joint_exact"],
                    "total": r_av7["positive_v2_1000"]["total_cases"],
                    "wilson_lb_pct": round(100.0 * r_av7["positive_v2_1000"]["track_b_complete_system"]["wilson_95_lower"], 2),
                    "det_pct": round(100.0 * d_av7["positive_v2_1000"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
                {
                    "label": f"Locked acceptance-v7 ★\nHard Negatives (N={r_av7['negative_all_1200']['total_cases']:,})",
                    "router_pct": round(100.0 * r_av7["negative_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "router_exact": r_av7["negative_all_1200"]["track_b_complete_system"]["verified_joint_exact"],
                    "total": r_av7["negative_all_1200"]["total_cases"],
                    "wilson_lb_pct": round(100.0 * r_av7["negative_all_1200"]["track_b_complete_system"]["wilson_95_lower"], 2),
                    "det_pct": round(100.0 * d_av7["negative_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
                {
                    "label": f"Retired acceptance-v6\nPositive v3 (N={r_av6['positive_v3_all_1320']['total_cases']:,})",
                    "router_pct": round(100.0 * r_av6["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "router_exact": r_av6["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_exact"],
                    "total": r_av6["positive_v3_all_1320"]["total_cases"],
                    "wilson_lb_pct": round(100.0 * r_av6["positive_v3_all_1320"]["track_b_complete_system"]["wilson_95_lower"], 2),
                    "det_pct": round(100.0 * d_av6["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
                {
                    "label": f"Retired acceptance-v6\nHard Negatives (N={r_av6['negative_all_1200']['total_cases']:,})",
                    "router_pct": round(100.0 * r_av6["negative_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "router_exact": r_av6["negative_all_1200"]["track_b_complete_system"]["verified_joint_exact"],
                    "total": r_av6["negative_all_1200"]["total_cases"],
                    "wilson_lb_pct": round(100.0 * r_av6["negative_all_1200"]["track_b_complete_system"]["wilson_95_lower"], 2),
                    "det_pct": round(100.0 * d_av6["negative_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
                {
                    "label": f"Retired dev-v6 (acceptance-v5)\nPositive v3 (N={r_av5['positive_v3_all_1200']['total_cases']:,})",
                    "router_pct": round(100.0 * r_av5["positive_v3_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "router_exact": r_av5["positive_v3_all_1200"]["track_b_complete_system"]["verified_joint_exact"],
                    "total": r_av5["positive_v3_all_1200"]["total_cases"],
                    "wilson_lb_pct": round(100.0 * r_av5["positive_v3_all_1200"]["track_b_complete_system"]["wilson_95_lower"], 2),
                    "det_pct": round(100.0 * d_av5["positive_v3_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
            ],
            "panel2_generative_progression": [
                {
                    "label": "Qwen3-0.6B v8\n(20-Iter Unbalanced\nUnassisted Raw)",
                    "pos_raw_pct": round(100.0 * qw_v8["acceptance_v6_pos_100"]["raw_mlx_unassisted_exact"] / 100.0, 2),
                    "neg_raw_pct": round(100.0 * qw_v8["acceptance_v6_neg_100"]["raw_mlx_unassisted_exact"] / 100.0, 2),
                },
                {
                    "label": "FG-270M v8\n(20-Iter Unbalanced\nUnassisted Raw)",
                    "pos_raw_pct": round(100.0 * fg_v8["acceptance_v6_pos_100"]["raw_mlx_unassisted_exact"] / 100.0, 2),
                    "neg_raw_pct": round(100.0 * fg_v8["acceptance_v6_neg_100"]["raw_mlx_unassisted_exact"] / 100.0, 2),
                },
                {
                    "label": "FG-270M v9 ★\n(60-Iter Balanced\nUnassisted Raw)",
                    "pos_raw_pct": round(100.0 * fg_v9["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 2),
                    "neg_raw_pct": round(100.0 * fg_v9["acceptance_v7_neg_150"]["track_a_raw_accuracy"], 2),
                },
                {
                    "label": "Qwen3-0.6B v9 ★\n(60-Iter Balanced\nUnassisted Raw)",
                    "pos_raw_pct": round(100.0 * qw_v9["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 2),
                    "neg_raw_pct": round(100.0 * qw_v9["acceptance_v7_neg_150"]["track_a_raw_accuracy"], 2),
                },
            ],
            "panel3_acceptance_v7_models": [
                {
                    "label": "ArrmesNeuralRouterV3 ★\n(Full Suite N=2,580)",
                    "pos_track_a_pct": round(100.0 * r_av7["positive_v3_all_1320"]["track_a_direct_model"]["joint_accuracy"], 2),
                    "pos_track_b_pct": round(100.0 * r_av7["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "neg_track_b_pct": round(100.0 * r_av7["negative_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
                {
                    "label": "Qwen3-0.6B v9 ★\nUnassisted MLX\n(N=300 Stratified)",
                    "pos_track_a_pct": round(100.0 * qw_v9["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 2),
                    "pos_track_b_pct": round(100.0 * qw_v9["acceptance_v7_v2_pos_150"]["track_b_verified_accuracy"], 2),
                    "neg_track_b_pct": round(100.0 * qw_v9["acceptance_v7_neg_150"]["track_b_verified_accuracy"], 2),
                },
                {
                    "label": "FG-270M v9 ★\nUnassisted MLX\n(N=300 Stratified)",
                    "pos_track_a_pct": round(100.0 * fg_v9["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 2),
                    "pos_track_b_pct": round(100.0 * fg_v9["acceptance_v7_v2_pos_150"]["track_b_verified_accuracy"], 2),
                    "neg_track_b_pct": round(100.0 * fg_v9["acceptance_v7_neg_150"]["track_b_verified_accuracy"], 2),
                },
                {
                    "label": "Qwen3-0.6B v9 ★\novernight-v1\n(N=80 Open Eval)",
                    "pos_track_a_pct": round(100.0 * qw_v9["overnight_v1_80"]["track_a_raw_accuracy"], 2),
                    "pos_track_b_pct": round(100.0 * qw_v9["overnight_v1_80"]["track_b_verified_accuracy"], 2),
                    "neg_track_b_pct": round(100.0 * fg_v9["overnight_v1_80"]["track_b_verified_accuracy"], 2),
                },
                {
                    "label": "Deterministic\nRegex Baseline\n(Full Suite N=2,580)",
                    "pos_track_a_pct": round(100.0 * d_av7["positive_v3_all_1320"]["track_a_direct_model"]["joint_accuracy"], 2),
                    "pos_track_b_pct": round(100.0 * d_av7["positive_v3_all_1320"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                    "neg_track_b_pct": round(100.0 * d_av7["negative_all_1200"]["track_b_complete_system"]["verified_joint_accuracy"], 2),
                },
            ],
        },
        "chart2": {
            "latency_rows": [
                {
                    "label": "ArrmesNeuralRouterV3 v9 ★\nApple M4 Pro CPU (2026-11-07)",
                    "p50_ms": profs["arrmes_neural_router_v3_macos_arm64_cpu"]["routing_and_verifier_ms"]["p50"],
                    "p95_ms": profs["arrmes_neural_router_v3_macos_arm64_cpu"]["routing_and_verifier_ms"]["p95"],
                },
                {
                    "label": "ArrmesNeuralRouterV3 ★\nRyzen 9 7950X3D Linux (2026-11-02*)",
                    "p50_ms": profs["arrmes_neural_router_v3_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p50"],
                    "p95_ms": profs["arrmes_neural_router_v3_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p95"],
                },
                {
                    "label": "ArrmesNeuralRouterV3 v9 ★\nHTTP Loopback /v1/route (M4 Pro)",
                    "p50_ms": profs["arrmes_neural_router_v3_macos_arm64_cpu"]["http_end_to_end_ms"]["p50"],
                    "p95_ms": profs["arrmes_neural_router_v3_macos_arm64_cpu"]["http_end_to_end_ms"]["p95"],
                },
                {
                    "label": "Qwen3-0.6B v5 GGUF Q4_K_M\nRTX 4070 Ti SUPER CUDA (2026-11-02*)",
                    "p50_ms": profs["qwen3_06b_public_gguf_q4_k_m_linux_rtx4070tis_cuda"]["routing_and_verifier_ms"]["p50"],
                    "p95_ms": profs["qwen3_06b_public_gguf_q4_k_m_linux_rtx4070tis_cuda"]["routing_and_verifier_ms"]["p95"],
                },
                {
                    "label": "FunctionGemma-270M v9 ★\nUnassisted 4-Bit MLX + LoRA (M4 Pro)",
                    "p50_ms": profs["functiongemma_270m_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p50"],
                    "p95_ms": profs["functiongemma_270m_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p95"],
                },
                {
                    "label": "Qwen3-0.6B v9 ★\nUnassisted 4-Bit MLX + LoRA (M4 Pro)",
                    "p50_ms": profs["qwen3_06b_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p50"],
                    "p95_ms": profs["qwen3_06b_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p95"],
                },
                {
                    "label": "Qwen3-0.6B v5 GGUF Q4_K_M\nRyzen 9 7950X3D 4-Thread CPU*",
                    "p50_ms": profs["qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu"]["warm_routing_ms"]["p50"],
                    "p95_ms": profs["qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu"]["warm_routing_ms"]["p95"],
                },
            ],
            "catalog_rows": [
                {
                    "label": "Linear Scan Baseline\n(5,000 Radarr Movies p50)",
                    "us": round(cat["radarr_5000_movies"]["linear_scan_baseline_ms"]["p50"] * 1000.0, 1),
                    "speedup_x": cat["radarr_5000_movies"]["speedup_vs_linear_scan_x"],
                },
                {
                    "label": "Linear Scan Baseline\n(5,000 Radarr Movies p95)",
                    "us": round(cat["radarr_5000_movies"]["linear_scan_baseline_ms"]["p95"] * 1000.0, 1),
                    "speedup_x": cat["radarr_5000_movies"]["speedup_vs_linear_scan_x"],
                },
                {
                    "label": "CatalogInvertedIndex ★\n(5,000 Movies Exact p50)",
                    "us": cat["radarr_5000_movies"]["warm_exact_lookup_us"]["p50"],
                    "speedup_x": cat["radarr_5000_movies"]["speedup_vs_linear_scan_x"],
                },
                {
                    "label": "CatalogInvertedIndex ★\n(5,000 Movies AltTitle/NFKD p50)",
                    "us": cat["radarr_5000_movies"]["warm_diacritic_and_alt_title_lookup_us"]["p50"],
                    "speedup_x": cat["radarr_5000_movies"]["speedup_vs_linear_scan_x"],
                },
            ],
            "memory_rows": [
                {
                    "label": "ArrmesNeuralRouterV3 v9 ★\n(Pure NumPy / Stdlib CPU)",
                    "disk_mib": profs["arrmes_neural_router_v3_macos_arm64_cpu"]["artifact_mib"],
                    "ram_mib": profs["arrmes_neural_router_v3_macos_arm64_cpu"]["memory_accounting_mb"]["peak_eval_rss_mb"],
                },
                {
                    "label": "FunctionGemma-270M v9 ★\n(4-Bit MLX + FP16 LoRA)",
                    "disk_mib": profs["functiongemma_270m_v9_4bit_macos_arm64_mlx"]["artifact_mib"],
                    "ram_mib": profs["functiongemma_270m_v9_4bit_macos_arm64_mlx"]["memory_accounting_mb"]["mlx_metal_unified_peak_mb"],
                },
                {
                    "label": "Qwen3-0.6B v9 ★\n(4-Bit MLX + FP16 LoRA)",
                    "disk_mib": profs["qwen3_06b_v9_4bit_macos_arm64_mlx"]["artifact_mib"],
                    "ram_mib": profs["qwen3_06b_v9_4bit_macos_arm64_mlx"]["memory_accounting_mb"]["mlx_metal_unified_peak_mb"],
                },
                {
                    "label": "HybridRouterMLX v9 ★\n(Router + Qwen3-0.6B v9)",
                    "disk_mib": profs["hybrid_router_mlx_qwen3_06b_v9_macos_arm64"]["artifact_mib"],
                    "ram_mib": profs["hybrid_router_mlx_qwen3_06b_v9_macos_arm64"]["memory_accounting_mb"]["mlx_metal_unified_peak_mb"],
                },
                {
                    "label": "Qwen3-0.6B v5 GGUF\n(Ryzen 9 7950X3D CPU*)",
                    "disk_mib": profs["qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu"]["artifact_mib"],
                    "ram_mib": profs["qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu"]["memory_accounting_mb"]["combined_resident_mib"],
                },
            ],
        },
        "chart3": {
            "groups": [
                {
                    "label": "Track C HTTP Fixture Suite: v2 Reads & Gated Writes\n(12 Sandbox Reads + 6 Confirmation-Gated Writes + Replay Block)",
                    "passed": r_p + w_p,
                    "total": r_t + w_t,
                },
                {
                    "label": "Track C HTTP Fixture Suite: All 68 Native service_query Pairs\n(Sonarr, Radarr, Lidarr, Prowlarr, Readarr, Whisparr, Bazarr, Tautulli, Seerr, qBit, Bitmagnet)",
                    "passed": sq_p,
                    "total": sq_t,
                },
                {
                    "label": "Track C HTTP Fixture Suite: Extended v1.6 Capabilities\n(Caller-Bound Seerr TV Season + Cancel, Readarr Book Search, Bounded Read-to-Confirm)",
                    "passed": ext_passed,
                    "total": ext_total,
                },
                {
                    "label": "Track C HTTP Fixture Scorecard Total\n(End-to-End Fixture Workflows Completed; 0 Unconfirmed Writes)",
                    "passed": tc["passed_workflows"],
                    "total": tc["total_workflows"],
                },
                {
                    "label": "11-Service Live Upstream Docker Container Binary Lab ★\n(Live Sonarr, Radarr w/ 320 Movies, Lidarr, Readarr w/ BookSearch, Whisparr v3,\nProwlarr, Bazarr, Tautulli, qBittorrent, Bitmagnet+Postgres, Seerr w/ Cancel)",
                    "passed": lab["summary"]["passed_checks"],
                    "total": lab["summary"]["total_checks"],
                },
            ],
            "live_services_qualified": lab["summary"]["live_services_qualified"],
        },
    }




def generate_all(extra_dir=None) -> dict:
    """Retired drawing: emit bench-receipt/v1 and render via shared bench_chart.py.

    load_chart_metrics() above is unchanged and remains the parity-checked
    data loader (tests/test_chart_receipt_parity.py). Historical
    docs/figures/arrmes_v7_*.png files are never touched.
    """
    import subprocess, sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from export_bench_receipt import build
    receipt_path = ROOT_DIR / "experiments" / "bench_receipt_v9.json"
    receipt_path.write_text(__import__("json").dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {receipt_path}")
    tool = ROOT_DIR.parent / "shared-ops" / "scripts" / "bench_chart.py"
    if tool.is_file():
        subprocess.call([sys.executable, str(tool), "--input", str(receipt_path),
                         "--output-dir", str(FIG_DIR), "--name", "arrmes_v9_comparison"])
    else:
        print("Render with: python3 ../shared-ops/scripts/bench_chart.py "
              f"--input {receipt_path} --output-dir {FIG_DIR} --name arrmes_v9_comparison")
    return load_chart_metrics()


if __name__ == "__main__":
    generate_all()
