#!/usr/bin/env python3
"""Generate publication-grade PNG visual charts for Arrmes v1.5 (v8 models & acceptance-v6).

Includes:
1. `arrmes-model-accuracy-comparison.png` and `arrmes_v8_acceptance_v6_accuracy_bounds.png`:
   Side-by-side accuracy comparison across `overnight-v1` (N=80), `acceptance-v4` (N=1,000),
   retired `dev-v6` (`acceptance-v5`, N=2,460), and locked `acceptance-v6` (N=2,580) plus
   `Qwen3-0.6B v8` and `FunctionGemma-270M v8` mixed-precision QLoRA & router-assisted decoding.
2. `arrmes-local-runtime-comparison.png` and `arrmes_v8_runtime_latency_memory_footprint.png`:
   Warm routing + verifier latency (`0.597 ms` p50 / `0.885 ms` p95), `CatalogInvertedIndex`
   5,000-movie lookup speedup (`1.17 us` vs `26.23 ms` linear scan = `22,495.7x` faster),
   and local RAM / GPU VRAM / disk footprint.
3. `arrmes_v8_track_c_workflow_scorecard.png`:
   Track C end-to-end execution scorecard (`93/93` HTTP fixture workflows + `45/45` live
   upstream Docker container binary verifications across Sonarr v4, Radarr v5/v6, Lidarr v2,
   Prowlarr v1, Bazarr 1.6+, Tautulli v2, and qBittorrent v5).
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT_DIR / "docs" / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

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


def _save_png(fig: plt.Figure, stems: list[str], extra_dir: Path | None = None) -> None:
    for s in stems:
        out_f = FIG_DIR / f"{s}.png"
        fig.savefig(out_f, dpi=240, bbox_inches="tight")
        _strip_png_metadata(out_f)
        if extra_dir is not None and extra_dir.exists():
            shutil.copy2(out_f, extra_dir / f"{s}.png")
    plt.close(fig)


def make_chart_1_accuracy(extra_dir: Path | None = None) -> None:
    fig, (ax1, ax2, ax3) = plt.subplots(
        1, 3, figsize=(22.5, 7.4), gridspec_kw={"width_ratios": [1.18, 1.08, 1.18]}
    )
    fig.patch.set_facecolor("#FFFFFF")

    # Panel 1: Multi-Suite NeuralRouterV3 Progression (v1.4 -> v1.5)
    p1_suites = [
        "Locked acceptance-v6 ★\nPositive v3 (N=1,380, 276 Clust.)",
        "Locked acceptance-v6 ★\nPositive v2 (N=1,060, 212 Clust.)",
        "Locked acceptance-v6 ★\nHard Negatives (N=1,200, 240 Clust.)",
        "Retired dev-v6 (acceptance-v5)\nPositive v3 (N=1,260, v1.4->v1.5)",
        "Retired dev-v6 (acceptance-v5)\nHard Negatives (N=1,200)",
        "Frozen acceptance-v4\nMixed Suite (N=1,000)",
        "Frozen overnight-v1\nOpen-Model Head-to-Head (N=80)",
    ]
    p1_v14 = [99.6, 99.7, 99.6, 99.76, 100.0, 100.0, 97.5]
    p1_v15 = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 97.5]
    p1_counts = [
        "1,380/1,380 (LB 99.72%)",
        "1,060/1,060 (LB 99.64%)",
        "1,200/1,200 (LB 99.68%)",
        "1,260/1,260 (up from 1,257)",
        "1,200/1,200 (0 unauth writes)",
        "1,000/1,000 (0 damaged)",
        "78/80 (beats Qwen3-4B 63/80)",
    ]

    y1 = np.arange(len(p1_suites))
    h1 = 0.36
    ax1.barh(y1 - h1 / 2, p1_v14, height=h1, label="Arrmes v1.4 Baseline (%)", color="#CBD5E1", edgecolor="#334155", linewidth=0.7, zorder=3)
    b1_v15 = ax1.barh(y1 + h1 / 2, p1_v15, height=h1, label="Arrmes v1.5 ★ (Track A Raw = Track B Verified)", color="#15803D", edgecolor="#0F172A", linewidth=0.8, zorder=3)

    for bar, val, cnt in zip(b1_v15, p1_v15, p1_counts):
        ax1.text(
            val + 0.8, bar.get_y() + bar.get_height() / 2,
            f"{val:.1f}% · {cnt}", va="center", ha="left",
            fontsize=7.6, color="#14532D", fontweight="bold"
        )

    ax1.axvline(99.0, color="#DC2626", linestyle="--", linewidth=1.3, label="99.0% Acceptance Gate")
    ax1.set_yticks(y1)
    ax1.set_yticklabels(p1_suites, fontsize=8.3, fontweight="bold", color="#0F172A")
    ax1.invert_yaxis()
    ax1.set_xlim(85, 118)
    ax1.set_xlabel("Exact Call / Safe Abstention Accuracy (%)", fontsize=9.2, fontweight="bold", color="#1E293B")
    ax1.set_title("1. ArrmesNeuralRouterV3 Across All Locked & Historical Suites\n100.0% on Locked acceptance-v6 (2,580/2,580) & dev-v6 (2,460/2,460)", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="x")
    ax1.legend(loc="lower right", fontsize=7.6, framealpha=0.95)

    # Panel 2: Generative Checkpoints v7 -> v8 (Fixing 4-Bit Quantization Span Drop)
    p2_models = [
        "Qwen3-0.6B v7\n4-Bit Quantized\n(Prior v1.4 Drop)",
        "Qwen3-0.6B v7\nFP16 + LoRA\n(Prior v1.4 Ref)",
        "Qwen3-0.6B v8 ★\nMixed-Prec QLoRA\n(Unassisted Raw)",
        "FG-270M v8 ★\nMixed-Prec QLoRA\n(Unassisted Raw)",
        "Qwen3 & FG v8 ★\nRouter-Assisted\nConstrained",
    ]
    p2_dev6 = [53.0, 76.0, 82.0, 82.0, 100.0]
    p2_av6 = [51.0, 74.0, 81.0, 86.0, 100.0]

    x2 = np.arange(len(p2_models))
    w2 = 0.36
    b2_d = ax2.bar(x2 - w2 / 2, p2_dev6, width=w2, label="dev-v6 Positive Slice (N=100)", color="#0284C7", edgecolor="#0F172A", linewidth=0.8, zorder=3)
    b2_a = ax2.bar(x2 + w2 / 2, p2_av6, width=w2, label="Locked acceptance-v6 Slice (N=100)", color="#15803D", edgecolor="#0F172A", linewidth=0.8, zorder=3)
    b2_d[0].set_facecolor("#F97316")
    b2_a[0].set_facecolor("#DC2626")

    for bars, col in ((b2_d, "#0369A1"), (b2_a, "#14532D")):
        for bar in bars:
            v = bar.get_height()
            ax2.text(
                bar.get_x() + bar.get_width() / 2, v + 1.4,
                f"{v:.0f}%", ha="center", va="bottom",
                fontsize=7.8, fontweight="bold", color=col
            )

    ax2.set_xticks(x2)
    ax2.set_xticklabels(p2_models, fontsize=8.0, fontweight="bold", color="#0F172A")
    ax2.set_ylim(0, 118)
    ax2.set_ylabel("Track A Direct Raw Model Exact Match (%)", fontsize=9.3, fontweight="bold", color="#1E293B")
    ax2.set_title("2. Generative 4-Bit Span Recovery (v7 vs. v8 Checkpoints)\nFP16 LoRA Overlay Lifts Unassisted 4-Bit Qwen3 from 53% to 82%", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="y")
    ax2.legend(loc="upper left", fontsize=7.8, framealpha=0.95)

    # Panel 3: Locked acceptance-v6 Zero-Leakage Suite (N=2,580)
    p3_models = [
        "Arrmes v1.5 ★\nNeuralRouterV3\n(N=2,580 Full)",
        "Arrmes v1.5 ★\nQwen3-0.6B v8\n(Hybrid, N=200)",
        "Arrmes v1.5 ★\nFG-270M v8\n(Hybrid, N=200)",
        "Qwen3-0.6B v8\nUnassisted Raw\n(N=100 Pos)",
        "FG-270M v8\nUnassisted Raw\n(N=100 Pos)",
    ]
    p3_pos_raw = [100.0, 100.0, 100.0, 81.0, 86.0]
    p3_pos_ver = [100.0, 100.0, 100.0, 96.0, 96.0]
    p3_neg_ver = [100.0, 100.0, 100.0, 98.0, 97.0]

    x3 = np.arange(len(p3_models))
    w3 = 0.26
    b3_r = ax3.bar(x3 - w3, p3_pos_raw, width=w3, label="Positive Track A (Raw Model)", color="#CBD5E1", edgecolor="#334155", linewidth=0.7, zorder=3)
    b3_p = ax3.bar(x3, p3_pos_ver, width=w3, label="Positive Track B (Verified System)", color="#0284C7", edgecolor="#0F172A", linewidth=0.7, zorder=3)
    b3_n = ax3.bar(x3 + w3, p3_neg_ver, width=w3, label="Hard-Negative Track B (Safe Abstention)", color="#15803D", edgecolor="#0F172A", linewidth=0.7, zorder=3)

    for b_idx, (bars, col) in enumerate(((b3_r, "#475569"), (b3_p, "#0369A1"), (b3_n, "#14532D"))):
        for m_idx, bar in enumerate(bars):
            v = bar.get_height()
            y_off = 3.6 if (b_idx == 1 and m_idx < 3) else 1.2
            ax3.text(
                bar.get_x() + bar.get_width() / 2, v + y_off,
                f"{v:.0f}%", ha="center", va="bottom",
                fontsize=7.2, fontweight="bold", color=col
            )

    ax3.axhline(99.0, color="#DC2626", linestyle="--", linewidth=1.4, label="99.0% Acceptance Gate")
    ax3.set_xticks(x3)
    ax3.set_xticklabels(p3_models, fontsize=8.0, fontweight="bold", color="#0F172A")
    ax3.set_ylim(0, 122)
    ax3.set_ylabel("Exact Match / Safe Abstention Accuracy (%)", fontsize=9.3, fontweight="bold", color="#1E293B")
    ax3.set_title("3. Locked Zero-Leakage acceptance-v6 Suite (N=2,580)\n0 Query Overlap, 0 Entity Overlap, 0 Damaged, 0 Unauth Writes", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax3.grid(axis="y")
    ax3.legend(loc="lower right", fontsize=7.5, framealpha=0.95)

    fig.suptitle(
        "Arrmes v1.5 Model Accuracy & Generative/Router Convergence Across Locked & Historical Benchmarks",
        fontsize=14.2, fontweight="bold", color="#0F172A", y=0.99
    )
    fig.text(
        0.01, 0.005,
        "Measured on Apple M4 Pro (2026-11-06). Track A reports direct model output before verification; Track B reports system output after verify_proposal (damaged == 0, unexpected_writes == 0). Explicit deterministic routes are tracked separately.",
        fontsize=8.6, color="#475569"
    )
    fig.tight_layout(rect=[0.0, 0.025, 1.0, 0.94])
    _save_png(
        fig,
        [
            "arrmes-model-accuracy-comparison",
            "arrmes_v7_acceptance_v5_accuracy_bounds",
            "arrmes_v7_open_model_accuracy_comparison",
            "arrmes_v8_acceptance_v6_accuracy_bounds",
        ],
        extra_dir=extra_dir,
    )


def make_chart_2_runtime_and_scaling(extra_dir: Path | None = None) -> None:
    fig, (ax1, ax2, ax3) = plt.subplots(
        1, 3, figsize=(22.5, 7.2), gridspec_kw={"width_ratios": [1.18, 1.12, 1.18]}
    )
    fig.patch.set_facecolor("#FFFFFF")

    # Panel 1: Warm Routing + Verifier Latency Across Backends & Hardware
    r_labels = [
        "ArrmesNeuralRouterV3 ★\nApple M4 Pro CPU (2026-11-06)",
        "ArrmesNeuralRouterV3 ★\nRyzen 9 7950X3D Linux (2026-11-02*)",
        "ArrmesNeuralRouterV3 ★\nHTTP Loopback /route (M4 Pro)",
        "FunctionGemma-270M v8\n4-Bit MLX + FP16 LoRA (M4 Pro)",
        "Qwen3-0.6B v5 GGUF Q4_K_M\nRTX 4070 Ti SUPER CUDA (2026-11-02*)",
        "Qwen3-0.6B v8\n4-Bit MLX + FP16 LoRA (M4 Pro)",
        "Qwen3-0.6B v5 GGUF Q4_K_M\nRyzen 9 7950X3D 4-Thread CPU*",
    ]
    p50_ms = [0.597, 0.728, 1.278, 43.5, 65.7, 114.4, 240.3]
    p95_ms = [0.885, 0.892, 1.650, 48.2, 68.0, 128.5, 250.3]

    y1 = np.arange(len(r_labels))
    h1 = 0.35
    ax1.barh(y1 - h1 / 2, p50_ms, height=h1, label="Warm p50 Latency (ms)", color="#15803D", edgecolor="#0F172A", linewidth=0.7, zorder=3)
    ax1.barh(y1 + h1 / 2, p95_ms, height=h1, label="Warm p95 Latency (ms)", color="#0284C7", edgecolor="#0F172A", linewidth=0.7, zorder=3)
    ax1.set_xscale("log")
    ax1.set_xlim(0.2, 1200.0)
    ax1.axvline(1.0, color="#DC2626", linestyle=":", linewidth=1.3, label="1.0 ms Neural Router Target")

    for i, (v50, v95) in enumerate(zip(p50_ms, p95_ms)):
        ax1.text(
            v95 * 1.15, y1[i],
            f"p50 {v50:g} ms | p95 {v95:g} ms",
            va="center", ha="left", fontsize=7.6, fontweight="bold",
            color="#14532D" if i < 3 else "#0F172A"
        )

    ax1.set_yticks(y1)
    ax1.set_yticklabels(r_labels, fontsize=8.2, fontweight="bold", color="#0F172A")
    ax1.invert_yaxis()
    ax1.set_xlabel("Warm Routing + Verifier Latency in ms — Log Scale", fontsize=9.2, fontweight="bold", color="#1E293B")
    ax1.set_title("1. Cross-Platform Routing + Verifier Latency\n0.597 ms p50 / 0.885 ms p95 on M4 Pro (< 0.75 ms / 1.0 ms Targets)", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="x", which="major")
    ax1.legend(loc="lower right", fontsize=7.8, framealpha=0.95)

    # Panel 2: Large-Library Catalog Resolution (5,000 Radarr Movies)
    c_labels = [
        "Legacy Linear Scan\n(5,000 Movies Unindexed\nExact Title Lookup)",
        "Legacy Linear Scan\n(5,000 Movies Unindexed\nDiacritic / AltTitle Lookup)",
        "CatalogInvertedIndex ★\n(5,000 Movies O(1) Hash\nExact Title Lookup)",
        "CatalogInvertedIndex ★\n(5,000 Movies O(1) Hash\nDiacritic / AltTitle Lookup)",
    ]
    c_us = [26230.0, 26410.0, 1.17, 0.96]
    c_cols = ["#DC2626", "#F97316", "#15803D", "#10B981"]

    y2 = np.arange(len(c_labels))
    bars2 = ax2.barh(y2, c_us, color=c_cols, edgecolor="#0F172A", linewidth=0.8, height=0.52, zorder=3)
    ax2.set_xscale("log")
    ax2.set_xlim(0.3, 250000.0)

    notes2 = [
        "26,230 us (26.23 ms per call)",
        "26,410 us (26.41 ms per call)",
        "1.17 us p50 ★ (22,495.7x faster)",
        "0.96 us p50 ★ (27,510.4x faster)",
    ]
    for i, (bar, val, note) in enumerate(zip(bars2, c_us, notes2)):
        ax2.text(
            val * 1.22, bar.get_y() + bar.get_height() / 2,
            note, va="center", ha="left", fontsize=8.0, fontweight="bold",
            color="#14532D" if i >= 2 else "#7F1D1D"
        )

    ax2.set_yticks(y2)
    ax2.set_yticklabels(c_labels, fontsize=8.4, fontweight="bold", color="#0F172A")
    ax2.invert_yaxis()
    ax2.set_xlabel("Lookup Latency in Microseconds (us) — Log Scale", fontsize=9.2, fontweight="bold", color="#1E293B")
    ax2.set_title("2. Large-Library Catalog Resolution (5,000 Radarr Movies)\nCatalogInvertedIndex Cuts Lookup from 26.23 ms to 1.17 us (22,495x)", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="x", which="major")

    # Panel 3: Disk Weights & Runtime RAM / VRAM Footprint
    m_labels = [
        "ArrmesNeuralRouterV3 ★\n(Pure NumPy / Stdlib CPU)",
        "FunctionGemma-270M v8\n(4-Bit MLX + FP16 LoRA)",
        "Qwen3-0.6B v8\n(4-Bit MLX + FP16 LoRA)",
        "Qwen3-0.6B v5 GGUF\n(RTX 4070 Ti SUPER CUDA*)",
        "Qwen3-4B Instruct 2507\n(Open 4B Reference Q4)",
    ]
    w_mib = [7.52, 182.9, 336.4, 378.3, 2158.2]
    rss_mib = [80.7, 798.0, 678.0, 736.1, 2510.5]

    y3 = np.arange(len(m_labels))
    h3 = 0.34
    ax3.barh(y3 - h3 / 2, w_mib, height=h3, label="Artifact Disk Size (MiB)", color="#CBD5E1", edgecolor="#334155", linewidth=0.7, zorder=3)
    ax3.barh(y3 + h3 / 2, rss_mib, height=h3, label="Peak Process RSS / RAM (MiB)", color="#0284C7", edgecolor="#0F172A", linewidth=0.7, zorder=3)

    for i, (w_v, r_v) in enumerate(zip(w_mib, rss_mib)):
        mx = max(w_v, r_v)
        ax3.text(
            mx + 45, y3[i],
            f"{w_v:.1f} MiB disk | {r_v:.1f} MiB RAM",
            va="center", ha="left", fontsize=7.8, fontweight="bold",
            color="#14532D" if i == 0 else "#0F172A"
        )

    ax3.axvline(15.0, color="#15803D", linestyle=":", linewidth=1.2, label="15 MiB Router Weight Cap")
    ax3.set_yticks(y3)
    ax3.set_yticklabels(m_labels, fontsize=8.2, fontweight="bold", color="#0F172A")
    ax3.invert_yaxis()
    ax3.set_xlim(0, 3400)
    ax3.set_xlabel("Size in MiB — Linear Scale (Lower is Better)", fontsize=9.2, fontweight="bold", color="#1E293B")
    ax3.set_title("3. Model Artifact Size & Runtime Memory Footprint\n7.52 MiB Router Weight (< 15 MiB Cap) with Stdlib .npz Fallback", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax3.grid(axis="x")
    ax3.legend(loc="lower right", fontsize=7.6, framealpha=0.95)

    fig.suptitle(
        "Arrmes v1.5 Speed, Large-Library Indexing Throughput, and Cross-Platform Memory Footprint",
        fontsize=14.2, fontweight="bold", color="#0F172A", y=0.99
    )
    fig.text(
        0.01, 0.005,
        "Apple M4 Pro profiles measured 2026-11-06 (`experiments/runtime-profile-v8.json`). *Linux x86_64 (`Ryzen 9 7950X3D`) & NVIDIA CUDA (`RTX 4070 Ti SUPER`) physical hardware receipts recorded 2026-11-02 (`experiments/runtime-profile-v7.json`).",
        fontsize=8.6, color="#475569"
    )
    fig.tight_layout(rect=[0.0, 0.025, 1.0, 0.94])
    _save_png(
        fig,
        [
            "arrmes-local-runtime-comparison",
            "arrmes_v7_runtime_latency_memory_footprint",
            "arrmes_v7_claude_sonnet_and_resource_comparison",
            "arrmes_v8_runtime_latency_memory_footprint",
        ],
        extra_dir=extra_dir,
    )


def make_chart_3_track_c_and_containers(extra_dir: Path | None = None) -> None:
    fig, ax = plt.subplots(figsize=(14.5, 5.8))
    fig.patch.set_facecolor("#FFFFFF")
    groups = [
        "Track C HTTP Fixture Suite: v2 Reads & Gated Writes\n(12 Sandbox Reads + 6 Confirmation-Gated Writes + Replay Block)",
        "Track C HTTP Fixture Suite: All 68 Native service_query Pairs\n(Sonarr, Radarr, Lidarr, Prowlarr, Readarr, Whisparr, Bazarr, Tautulli, Seerr, qBit, Bitmagnet)",
        "Track C HTTP Fixture Suite: Extended v1.5 Capabilities\n(Caller-Bound Seerr TV Season + Cancel, Readarr Book Search, Bounded Read-to-Confirm)",
        "Track C HTTP Fixture Scorecard Total\n(93/93 End-to-End Fixture Workflows Completed; 0 Unconfirmed Writes)",
        "Actual Upstream Docker Container Binary Lab ★\n(Live Sonarr v4, Radarr v5/v6 w/ 320 Seeded Movies, Lidarr v2, Prowlarr v1,\nBazarr 1.6+, Tautulli v2, qBittorrent v5 on 127.0.0.1 Bridge)",
    ]
    passed = [18, 68, 7, 93, 45]
    totals = [18, 68, 7, 93, 45]
    pcts = [100.0 * p / t for p, t in zip(passed, totals)]
    y = np.arange(len(groups))
    colors = ["#0284C7", "#0EA5E9", "#10B981", "#059669", "#15803D"]

    bars = ax.barh(y, pcts, color=colors, height=0.56, edgecolor="#0F172A")
    ax.axvline(99.0, color="#DC2626", linestyle="--", linewidth=1.5, label="99.0% Acceptance Threshold")
    ax.set_xlim(0, 118)
    ax.set_yticks(y)
    ax.set_yticklabels(groups, fontsize=8.8, fontweight="bold", color="#0F172A")
    ax.invert_yaxis()
    ax.set_xlabel("Verified Completion Rate (%) — HTTP Fixtures & Upstream Container Binaries Reported Separately", fontsize=9.8, fontweight="bold", color="#1E293B")
    ax.set_title(
        "Track C End-to-End Execution: 93/93 HTTP Fixture Workflows + 45/45 Live Upstream Container Binary Checks",
        fontsize=12.2, fontweight="bold", color="#0F172A", pad=12,
    )
    ax.grid(axis="x")
    ax.legend(loc="lower right", fontsize=9)

    for bar, p, t in zip(bars, passed, totals):
        w_val = bar.get_width()
        ax.annotate(
            f"{p}/{t} ({w_val:.1f}%)",
            xy=(w_val, bar.get_y() + bar.get_height() / 2),
            xytext=(6, 0), textcoords="offset points",
            ha="left", va="center", fontsize=9.2, fontweight="bold", color="#0F172A",
        )

    fig.tight_layout()
    _save_png(
        fig,
        [
            "arrmes_v7_track_c_workflow_scorecard",
            "arrmes_v8_track_c_workflow_scorecard",
        ],
        extra_dir=extra_dir,
    )


if __name__ == "__main__":
    make_chart_1_accuracy()
    make_chart_2_runtime_and_scaling()
    make_chart_3_track_c_and_containers()
    print("Generated all v8 PNG charts in docs/figures/ and artifact directory.")
