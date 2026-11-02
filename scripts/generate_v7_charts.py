#!/usr/bin/env python3
"""Generate publication-grade PNG + PDF visual charts for Arrmes v7 evaluation and runtime profiling."""
from __future__ import annotations

from pathlib import Path
import shutil

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT_DIR / "docs" / "figures"
BRAIN_DIR = Path("work/figures")
FIG_DIR.mkdir(parents=True, exist_ok=True)
BRAIN_DIR.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
    "axes.edgecolor": "#CBD5E1",
    "axes.linewidth": 0.9,
    "grid.color": "#E2E8F0",
    "grid.linestyle": "--",
    "grid.alpha": 0.7,
    "figure.facecolor": "#FFFFFF",
    "axes.facecolor": "#F8FAFC",
})


def _save_both(fig: plt.Figure, stem: str) -> None:
    for ext in ("png", "pdf"):
        out_f = FIG_DIR / f"{stem}.{ext}"
        fig.savefig(out_f, dpi=300, bbox_inches="tight")
        shutil.copy2(out_f, BRAIN_DIR / f"{stem}.{ext}")
    plt.close(fig)


def make_chart_1_acceptance_v5() -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 6.2), gridspec_kw={"width_ratios": [1.15, 1.0]})
    fig.suptitle(
        "Arrmes v7 Locked Acceptance-v5 Evaluation (Seed 7007, Zero Entity/Utterance Leakage)",
        fontsize=14, fontweight="bold", color="#0F172A", y=1.02,
    )

    # Panel A: Full 2,460-case locked acceptance-v5 (ArrmesNeuralRouterV3 vs Deterministic Baseline)
    splits = [
        "Positive v2\n(1,060 cases, 212 clusters)",
        "Positive v3 All\n(1,260 cases, 252 clusters)",
        "Hard-Negative / Safety\n(1,200 cases, 240 clusters)",
    ]
    x = np.arange(len(splits))
    w = 0.24

    nr_point = [100.00, 99.76, 100.00]
    nr_wilson_lb = [99.64, 99.30, 99.68]
    nr_boot_lb = [100.00, 99.44, 100.00]
    det_point = [16.60, 13.97, 21.92]

    b1 = ax1.bar(x - w, nr_point, width=w, label="ArrmesNeuralRouterV3 Point Est. (Track A = Track B)", color="#0284C7", edgecolor="#0369A1")
    b2 = ax1.bar(x, nr_wilson_lb, width=w, label="ArrmesNeuralRouterV3 95% Wilson Lower Bound", color="#0EA5E9", edgecolor="#0284C7")
    b3 = ax1.bar(x + w, nr_boot_lb, width=w, label="ArrmesNeuralRouterV3 95% Cluster-Bootstrap LB", color="#10B981", edgecolor="#059669")

    ax1.axhline(99.0, color="#DC2626", linestyle="--", linewidth=1.6, label="99.0% Acceptance Gate Threshold")
    ax1.set_ylim(94.0, 101.4)
    ax1.set_xticks(x)
    ax1.set_xticklabels(splits, fontsize=9.5, fontweight="bold", color="#1E293B")
    ax1.set_ylabel("Accuracy / Confidence Bound (%)", fontsize=10.5, fontweight="bold", color="#1E293B")
    ax1.set_title("A. Full Locked Acceptance-v5 Gate (2,460 Cases, 492 Clusters)\n(Deterministic Baseline = 16.6% / 14.0% / 21.9%)", fontsize=11, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="y")
    ax1.legend(loc="lower right", fontsize=8.5, framealpha=0.95)

    for bars in (b1, b2, b3):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(
                f"{h:.2f}%",
                xy=(bar.get_x() + bar.get_width() / 2, h),
                xytext=(0, 4), textcoords="offset points",
                ha="center", va="bottom", fontsize=8.2, fontweight="bold", color="#0F172A",
            )

    # Panel B: Stratified 200-case locked acceptance-v5 comparison across neural router & 4 MLX generative checkpoints
    models = [
        "ArrmesNeuralRouterV3\n(7.5 MiB CPU)",
        "FunctionGemma-270M v7\n(175.8 MiB 4-bit MLX)",
        "FunctionGemma-270M v5\n(Public Adapter)",
        "Released Qwen3-0.6B v6\n(fp16 + LoRA)",
        "Qwen3-0.6B v7\n(fp16 + LoRA)",
    ]
    y = np.arange(len(models))
    h_bar = 0.34
    pos_track_a = [100.0, 70.0, 68.0, 88.0, 76.0]
    pos_track_b = [100.0, 91.0, 78.0, 91.0, 77.0]
    neg_track_b = [100.0, 88.0, 87.0, 96.0, 97.0]

    r1 = ax2.barh(y + h_bar, pos_track_a, height=h_bar * 0.88, label="Positive Track A (Raw Model Exact)", color="#6366F1")
    r2 = ax2.barh(y, pos_track_b, height=h_bar * 0.88, label="Positive Track B (Verified System Exact)", color="#0EA5E9")
    r3 = ax2.barh(y - h_bar, neg_track_b, height=h_bar * 0.88, label="Negative Track B (Verified Safe Abstention)", color="#10B981")

    ax2.axvline(99.0, color="#DC2626", linestyle="--", linewidth=1.4)
    ax2.set_xlim(0, 112)
    ax2.set_yticks(y)
    ax2.set_yticklabels(models, fontsize=9, fontweight="bold", color="#1E293B")
    ax2.invert_yaxis()
    ax2.set_xlabel("Exact Match Accuracy (%) — 0 Unauthorized Writes Across All Models", fontsize=10, fontweight="bold", color="#1E293B")
    ax2.set_title("B. Stratified 200-Case Locked Comparison Across Architectures", fontsize=11, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="x")
    ax2.legend(loc="lower right", fontsize=8.3, framealpha=0.95)

    for bars in (r1, r2, r3):
        for bar in bars:
            w_val = bar.get_width()
            ax2.annotate(
                f"{w_val:.0f}%",
                xy=(w_val, bar.get_y() + bar.get_height() / 2),
                xytext=(4, 0), textcoords="offset points",
                ha="left", va="center", fontsize=8, fontweight="bold", color="#0F172A",
            )

    fig.tight_layout()
    _save_both(fig, "arrmes_v7_acceptance_v5_accuracy_bounds")


def make_chart_2_error_taxonomy_av4() -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 5.8), gridspec_kw={"width_ratios": [1.2, 1.0]})
    fig.suptitle(
        "Frozen Acceptance-v4 (1,000 Cases): Error Taxonomy Attribution & General Verifier Repair Progression",
        fontsize=13.5, fontweight="bold", color="#0F172A", y=1.02,
    )

    configs = [
        "ArrmesNeuralRouterV3\n(v7 Learned Router)",
        "Released Arrmes\n(Qwen3-0.6B v6)",
        "Qwen3-4B\nReference",
        "Untuned\nQwen3-0.6B",
        "Deterministic\nBaseline",
    ]
    x = np.arange(len(configs))
    w = 0.25

    raw_scores = [1000, 770, 782, 396, 89]
    pre_v131_ver = [1000, 678, 720, 439, 105]
    v7_ver = [1000, 981, 903, 628, 193]

    b_raw = ax1.bar(x - w, raw_scores, width=w, label="Track A: Raw Model Exact (/1000)", color="#64748B")
    b_old = ax1.bar(x, pre_v131_ver, width=w, label="Track B: Snapshot e5e76b8 Verifier (/1000)", color="#F59E0B")
    b_new = ax1.bar(x + w, v7_ver, width=w, label="Track B: General v7 Verifier (/1000, 0 Damaged)", color="#0284C7")

    ax1.axhline(990, color="#DC2626", linestyle="--", linewidth=1.4, label="990/1000 (99.0%) Target")
    ax1.set_ylim(0, 1110)
    ax1.set_xticks(x)
    ax1.set_xticklabels(configs, fontsize=9, fontweight="bold", color="#1E293B")
    ax1.set_ylabel("Exact Proposals (/1,000 Cases)", fontsize=10.5, fontweight="bold", color="#1E293B")
    ax1.set_title("A. Same-M4 Frozen 1,000-Case Diagnostic Progression", fontsize=11, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="y")
    ax1.legend(loc="upper right", fontsize=8.3, framealpha=0.95)

    for bars in (b_raw, b_old, b_new):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(
                f"{int(h)}",
                xy=(bar.get_x() + bar.get_width() / 2, h),
                xytext=(0, 3), textcoords="offset points",
                ha="center", va="bottom", fontsize=8.2, fontweight="bold", color="#0F172A",
            )

    # Panel B: Attribution of the 230 Raw Model Errors in Released Arrmes (770/1000 raw -> 981/1000 verified)
    categories = [
        "Wrong Clarify Slot or\nAbstention Reason (Repaired)",
        "Over-Predicting Concrete Tool\non Ambiguous/Unsafe (Repaired)",
        "Wrong Concrete Tool Selected\non Valid/Contrast Call (Repaired)",
        "Wrong or Missing Typed\nArguments/Entities (Repaired)",
        "Residual Unrepaired Model\nIntent Confusions (19/1000)",
        "Correct Raw Predictions\nDamaged by v7 Verifier (0/1000)",
    ]
    counts = [110, 56, 31, 14, 19, 0]
    colors = ["#0EA5E9", "#10B981", "#3B82F6", "#6366F1", "#F43F5E", "#94A3B8"]
    y_pos = np.arange(len(categories))

    bars_b = ax2.barh(y_pos, counts, color=colors, height=0.62, edgecolor="#334155")
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels(categories, fontsize=8.8, fontweight="bold", color="#1E293B")
    ax2.invert_yaxis()
    ax2.set_xlim(0, 130)
    ax2.set_xlabel("Number of Cases out of 230 Raw Model Errors", fontsize=10, fontweight="bold", color="#1E293B")
    ax2.set_title("B. Released Arrmes (770/1000 Raw) Error Attribution & Repair\n(211/230 Repaired by General Verifier; 0 Correct Calls Damaged)", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="x")

    for bar in bars_b:
        w_val = bar.get_width()
        ax2.annotate(
            f"{int(w_val)} cases",
            xy=(w_val, bar.get_y() + bar.get_height() / 2),
            xytext=(5, 0), textcoords="offset points",
            ha="left", va="center", fontsize=8.5, fontweight="bold", color="#0F172A",
        )

    fig.tight_layout()
    _save_both(fig, "arrmes_v7_error_taxonomy_and_verifier_repair")


def make_chart_3_runtime_profile() -> None:
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(16.5, 5.4))
    fig.suptitle(
        "Arrmes v7 Cross-Platform Hardware Profile: Artifact Size, Warm Routing Latency, and Memory Accounting",
        fontsize=13.5, fontweight="bold", color="#0F172A", y=1.03,
    )

    # Panel A: Artifact Footprint (MB decimal)
    art_labels = [
        "ArrmesNeuralRouterV3\n(CPU NumPy .npz)",
        "FunctionGemma-270M v7\n(4-bit MLX)",
        "Qwen3-0.6B v7\n(4-bit MLX)",
        "Qwen3-0.6B Public\n(GGUF Q4_K_M)",
    ]
    art_mb = [7.88, 184.37, 346.93, 396.70]
    art_colors = ["#10B981", "#0EA5E9", "#3B82F6", "#6366F1"]
    x1 = np.arange(len(art_labels))
    b_art = ax1.bar(x1, art_mb, color=art_colors, width=0.55, edgecolor="#1E293B")
    ax1.axhline(400.0, color="#DC2626", linestyle="--", linewidth=1.5, label="400 MB Max Budget")
    ax1.axhline(250.0, color="#F59E0B", linestyle=":", linewidth=1.5, label="250 MB Stretch Goal")
    ax1.set_ylim(0, 460)
    ax1.set_xticks(x1)
    ax1.set_xticklabels(art_labels, fontsize=8.3, fontweight="bold", rotation=12)
    ax1.set_ylabel("Model + Tokenizer Size (MB)", fontsize=10, fontweight="bold")
    ax1.set_title("A. Disk Artifact Footprint", fontsize=11, fontweight="bold")
    ax1.grid(axis="y")
    ax1.legend(loc="upper left", fontsize=8)
    for bar in b_art:
        h = bar.get_height()
        ax1.annotate(f"{h:.1f} MB", xy=(bar.get_x() + bar.get_width() / 2, h), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8.3, fontweight="bold")

    # Panel B: Warm Routing + Verifier Latency (ms, log scale for clarity across 0.67 ms to 114 ms)
    lat_labels = [
        "NeuralRouterV3\nM4 Pro CPU",
        "NeuralRouterV3\nM4 Pro HTTP",
        "NeuralRouterV3\nRyzen 7950X3D CPU",
        "FG-270M v7\nM4 Pro Metal",
        "Qwen3-0.6B GGUF\nRTX 4070TiS CUDA",
        "Qwen3-0.6B v7\nM4 Pro Metal",
    ]
    p50_ms = [0.674, 1.113, 0.944, 43.54, 61.90, 114.40]
    p95_ms = [0.882, 1.264, 1.219, 47.07, 87.70, 121.86]
    x2 = np.arange(len(lat_labels))
    w2 = 0.35
    b_p50 = ax2.bar(x2 - w2 / 2, p50_ms, width=w2, label="Warm p50 (ms)", color="#0284C7")
    b_p95 = ax2.bar(x2 + w2 / 2, p95_ms, width=w2, label="Warm p95 (ms)", color="#38BDF8")
    ax2.axhline(100.0, color="#F59E0B", linestyle=":", linewidth=1.4, label="100 ms Accel p50 Budget")
    ax2.axhline(200.0, color="#DC2626", linestyle="--", linewidth=1.4, label="200 ms Accel p95 / 250 ms CPU p50")
    ax2.set_yscale("log")
    ax2.set_ylim(0.3, 400.0)
    ax2.set_xticks(x2)
    ax2.set_xticklabels(lat_labels, fontsize=7.8, fontweight="bold", rotation=18)
    ax2.set_ylabel("Latency in ms (Log Scale)", fontsize=10, fontweight="bold")
    ax2.set_title("B. Warm Routing + Verifier Latency", fontsize=11, fontweight="bold")
    ax2.grid(axis="y", which="both")
    ax2.legend(loc="upper left", fontsize=7.8)
    for bar in b_p50:
        h = bar.get_height()
        lbl = f"{h:.2f}" if h < 5 else f"{h:.1f}"
        ax2.annotate(lbl, xy=(bar.get_x() + bar.get_width() / 2, h), xytext=(-2, 3), textcoords="offset points", ha="center", fontsize=7.3, fontweight="bold")
    for bar in b_p95:
        h = bar.get_height()
        lbl = f"{h:.2f}" if h < 5 else f"{h:.1f}"
        ax2.annotate(lbl, xy=(bar.get_x() + bar.get_width() / 2, h), xytext=(2, 3), textcoords="offset points", ha="center", fontsize=7.3, fontweight="bold")

    # Panel C: Resident Memory Footprint (MiB)
    mem_labels = [
        "NeuralRouterV3\nLinux x86_64 CPU",
        "NeuralRouterV3\nmacOS M4 Pro CPU",
        "FG-270M v7\nMLX Metal Peak",
        "Qwen3 GGUF\nLinux CPU Total",
    ]
    mem_mib = [63.50, 80.70, 1060.96, 1200.20]
    mem_colors = ["#10B981", "#059669", "#0EA5E9", "#6366F1"]
    x3 = np.arange(len(mem_labels))
    b_mem = ax3.bar(x3, mem_mib, color=mem_colors, width=0.52, edgecolor="#1E293B")
    ax3.axhline(1536.0, color="#DC2626", linestyle="--", linewidth=1.5, label="1.5 GiB (1,536 MiB) Max Budget")
    ax3.axhline(1024.0, color="#F59E0B", linestyle=":", linewidth=1.5, label="1.0 GiB (1,024 MiB) Stretch Goal")
    ax3.set_ylim(0, 1750)
    ax3.set_xticks(x3)
    ax3.set_xticklabels(mem_labels, fontsize=8.3, fontweight="bold", rotation=12)
    ax3.set_ylabel("Peak Resident / Unified Memory (MiB)", fontsize=10, fontweight="bold")
    ax3.set_title("C. Peak Resident Memory (No Double-Count)", fontsize=11, fontweight="bold")
    ax3.grid(axis="y")
    ax3.legend(loc="upper left", fontsize=8)
    for bar in b_mem:
        h = bar.get_height()
        ax3.annotate(f"{h:.1f} MiB", xy=(bar.get_x() + bar.get_width() / 2, h), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8.3, fontweight="bold")

    fig.tight_layout()
    _save_both(fig, "arrmes_v7_runtime_latency_memory_footprint")


def make_chart_4_track_c_workflows() -> None:
    fig, ax = plt.subplots(figsize=(12.5, 5.2))
    groups = [
        "v2 SandboxCluster Reads\n(Sonarr, Radarr, Lidarr, Tautulli,\nSeerr, Armiger, Bitmagnet, Waypoint, qBit)",
        "v2 Confirmation-Gated Writes\n(Sonarr, Radarr, Lidarr, Seerr,\nqBit v5.1.2 Pause/Resume + Replay Block)",
        "v3 Native service_query Pairs\n(All 31 Operations Across 8 APIs:\nHealth, Queue, History, Library, Missing, Search)",
        "v3 Bazarr Subtitle Workflows\n(Provider Search + Two-Phase Confirmed\nDownload with Post-Condition Check)",
        "Degraded-Service Fault Drill\n(HTTP 503 Injection Fails Closed\nto Error State Without False Completion)",
        "Track C Overall End-to-End\nWorkflow Completion Scorecard\n(0 Unconfirmed Writes Executed)",
    ]
    passed = [12, 6, 31, 2, 1, 52]
    totals = [12, 6, 31, 2, 1, 52]
    pcts = [100.0 * p / t for p, t in zip(passed, totals)]
    y = np.arange(len(groups))
    colors = ["#0284C7", "#0EA5E9", "#10B981", "#059669", "#6366F1", "#0F172A"]

    bars = ax.barh(y, pcts, color=colors, height=0.58, edgecolor="#1E293B")
    ax.axvline(99.0, color="#DC2626", linestyle="--", linewidth=1.5, label="99.0% Acceptance Threshold")
    ax.set_xlim(0, 115)
    ax.set_yticks(y)
    ax.set_yticklabels(groups, fontsize=9.2, fontweight="bold", color="#1E293B")
    ax.invert_yaxis()
    ax.set_xlabel("Verified End-to-End Workflow Completion Rate (%)", fontsize=10.5, fontweight="bold", color="#1E293B")
    ax.set_title(
        "Track C End-to-End Execution Scorecard: 52/52 Workflows Completed (100.0%), 0 Unauthorized Writes",
        fontsize=12.5, fontweight="bold", color="#0F172A", pad=12,
    )
    ax.grid(axis="x")
    ax.legend(loc="lower right", fontsize=9)

    for bar, p, t in zip(bars, passed, totals):
        w_val = bar.get_width()
        ax.annotate(
            f"{p}/{t} ({w_val:.1f}%)",
            xy=(w_val, bar.get_y() + bar.get_height() / 2),
            xytext=(6, 0), textcoords="offset points",
            ha="left", va="center", fontsize=9.5, fontweight="bold", color="#0F172A",
        )

    fig.tight_layout()
    _save_both(fig, "arrmes_v7_track_c_workflow_scorecard")


if __name__ == "__main__":
    make_chart_1_acceptance_v5()
    make_chart_2_error_taxonomy_av4()
    make_chart_3_runtime_profile()
    make_chart_4_track_c_workflows()
    print("Generated all 4 PNG + PDF chart pairs in docs/figures/ and brain directory.")
