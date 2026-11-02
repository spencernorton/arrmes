#!/usr/bin/env python3
"""Generate publication-grade PNG + PDF visual charts for Arrmes v1.4 evaluation and runtime profiling.

Includes:
1. Clear side-by-side accuracy comparison of Arrmes v1.4 (NeuralRouterV3, Qwen3-0.6B, FunctionGemma-270M v7)
   against similar open models (Qwen3-4B Instruct 2507, Qwen3-1.7B, IBM Granite 4.0 350M BF16/Q4,
   untuned base models, and deterministic rules) across overnight-v1 (N=80), acceptance-v4 (N=1,000),
   and locked acceptance-v5 (N=2,460).
2. With vs. Without Arrmes comparison of Claude Sonnet task completion time, token usage & cost,
   and local RAM / VRAM / disk footprint across Arrmes backends and open models.
3. Error taxonomy attribution & general v1.4 verifier repair progression on acceptance-v4 (N=1,000).
4. Track C end-to-end sandbox and native API workflow execution scorecard (52/52 workflows).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import struct

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
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


def _save_both(fig: plt.Figure, stem: str, extra_stems: list[str] | None = None) -> None:
    all_stems = [stem] + (extra_stems or [])
    for s in all_stems:
        for ext in ("png", "pdf"):
            out_f = FIG_DIR / f"{s}.{ext}"
            fig.savefig(out_f, dpi=300, bbox_inches="tight")
            if ext == "png":
                _strip_png_metadata(out_f)
            shutil.copy2(out_f, BRAIN_DIR / f"{s}.{ext}")
    plt.close(fig)


def make_chart_1_open_model_accuracy() -> None:
    """3-panel accuracy comparison of Arrmes v1.4 vs. similar open models across all 3 benchmark suites."""
    fig, (ax1, ax2, ax3) = plt.subplots(
        1, 3, figsize=(22.5, 7.4), gridspec_kw={"width_ratios": [1.30, 1.0, 1.14]}
    )
    fig.patch.set_facecolor("#FFFFFF")

    # -------------------------------------------------------------------------
    # Panel 1: 80-Case Open-Model Head-to-Head (overnight-v1, Same Apple M4 Pro)
    # -------------------------------------------------------------------------
    p1_models = [
        "Arrmes v1.4 ★\n(NeuralRouterV3, 7.5 MiB)",
        "Arrmes v1.4\n(Qwen3-0.6B Fine-Tuned)",
        "Arrmes v1.4\n(FG-270M Fine-Tuned)",
        "Qwen3-4B Instruct 2507\n(Open 4B Reference Q4)",
        "Qwen3-0.6B\n(Untuned Base)",
        "FunctionGemma-270M\n(Untuned Base)",
        "Deterministic / Regex\n(Rule Baseline)",
        "Qwen3-1.7B Instruct\n(Open 1.7B Q4)",
        "IBM Granite 4.0 350M\n(Publisher BF16)",
        "IBM Granite 4.0 350M\n(Community Q4)",
    ]
    p1_raw = [97.5, 78.8, 78.8, 67.5, 26.3, 15.0, 25.0, 46.3, 33.8, 0.0]
    p1_ver = [97.5, 87.5, 85.0, 78.8, 51.2, 37.5, 28.8, 20.0, 12.5, 11.2]
    p1_raw_counts = ["78/80", "63/80", "63/80", "54/80", "21/80*", "12/80", "20/80", "37/80*", "27/80*", "0/80"]
    p1_ver_counts = ["78/80", "70/80", "68/80", "63/80", "41/80", "30/80", "23/80", "16/80", "10/80", "9/80"]

    y1 = np.arange(len(p1_models))
    h1 = 0.36
    b1_raw = ax1.barh(
        y1 - h1 / 2, p1_raw, height=h1,
        label="Track A: Raw / Translated Model Output (%)",
        color="#CBD5E1", edgecolor="#334155", linewidth=0.7, zorder=3
    )
    b1_ver = ax1.barh(
        y1 + h1 / 2, p1_ver, height=h1,
        label="Track B: Full System (+ Arrmes v1.4 Verifier) (%)",
        color="#0284C7", edgecolor="#0F172A", linewidth=0.8, zorder=3
    )
    b1_ver[0].set_facecolor("#15803D")

    for bar, val, cnt in zip(b1_raw, p1_raw, p1_raw_counts):
        ax1.text(
            val + 1.2, bar.get_y() + bar.get_height() / 2,
            f"{val:.1f}% ({cnt})", va="center", ha="left",
            fontsize=7.4, color="#475569", fontweight="bold"
        )
    for idx, (bar, val, cnt) in enumerate(zip(b1_ver, p1_ver, p1_ver_counts)):
        col = "#14532D" if idx == 0 else ("#0369A1" if idx in (1, 2) else "#0F172A")
        ax1.text(
            val + 1.2, bar.get_y() + bar.get_height() / 2,
            f"{val:.1f}% ({cnt})", va="center", ha="left",
            fontsize=7.7, color=col, fontweight="bold"
        )

    ax1.axvline(95.0, color="#DC2626", linestyle=":", linewidth=1.3, label="95% Reference Line")
    ax1.set_yticks(y1)
    ax1.set_yticklabels(p1_models, fontsize=8.4, fontweight="bold", color="#0F172A")
    ax1.invert_yaxis()
    ax1.set_xlim(0, 124)
    ax1.set_xlabel("Exact Call Accuracy (%) on Same Apple M4 Pro (*translated JSON)", fontsize=9.2, fontweight="bold", color="#1E293B")
    ax1.set_title("1. Open-Model Head-to-Head (overnight-v1, N=80)\n10 Models Compared on Identical Hardware & Prompts", fontsize=10.8, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="x")
    ax1.legend(
        handles=[
            mpatches.Patch(facecolor="#CBD5E1", edgecolor="#334155", label="Track A: Raw / Translated Model Output (%)"),
            mpatches.Patch(facecolor="#0284C7", edgecolor="#0F172A", label="Track B: Full System (+ Arrmes v1.4 Verifier) (%)"),
            mpatches.Patch(facecolor="#15803D", edgecolor="#0F172A", label="Arrmes v1.4 ★ NeuralRouterV3 (97.5% Raw & Verified)"),
            plt.Line2D([0], [0], color="#DC2626", linestyle=":", linewidth=1.3, label="95% Reference Line"),
        ],
        loc="lower right", fontsize=7.5, framealpha=0.95
    )

    # -------------------------------------------------------------------------
    # Panel 2: 1,000-Case Benchmark (acceptance-v4, Same Apple M4 Pro)
    # -------------------------------------------------------------------------
    p2_models = [
        "Arrmes v1.4 ★\n(NeuralRouterV3\n7.5 MiB)",
        "Arrmes v1.4\n(Qwen3-0.6B\n331 MiB)",
        "Qwen3-4B\nInstruct 2507\n(2,158 MiB)",
        "Qwen3-0.6B\nUntuned Base\n(331 MiB)",
        "Deterministic\nRule/Regex\nBaseline",
    ]
    p2_raw = [100.0, 77.0, 78.2, 39.6, 8.9]
    p2_ver = [100.0, 98.1, 90.3, 62.8, 19.3]

    x2 = np.arange(len(p2_models))
    w2 = 0.36
    b2_raw = ax2.bar(
        x2 - w2 / 2, p2_raw, width=w2,
        label="Track A: Raw Model Output",
        color="#CBD5E1", edgecolor="#334155", linewidth=0.8, zorder=3
    )
    b2_ver = ax2.bar(
        x2 + w2 / 2, p2_ver, width=w2,
        label="Track B: Full System (+ v1.4 Verifier)",
        color="#0284C7", edgecolor="#0F172A", linewidth=0.8, zorder=3
    )
    b2_ver[0].set_facecolor("#15803D")

    for bar, val in zip(b2_raw, p2_raw):
        lbl = "100%" if val == 100.0 else f"{val:.1f}%"
        ax2.text(
            bar.get_x() + bar.get_width() / 2, val + 1.4,
            lbl, ha="center", va="bottom",
            fontsize=7.6, fontweight="bold", color="#475569"
        )
    for idx, (bar, val) in enumerate(zip(b2_ver, p2_ver)):
        col = "#14532D" if idx == 0 else ("#0369A1" if idx == 1 else "#0F172A")
        lbl = "100%" if val == 100.0 else f"{val:.1f}%"
        ax2.text(
            bar.get_x() + bar.get_width() / 2, val + 1.4,
            lbl, ha="center", va="bottom",
            fontsize=8.0, fontweight="bold", color=col
        )

    ax2.axhline(99.0, color="#DC2626", linestyle="--", linewidth=1.4, label="99.0% Acceptance Gate")
    ax2.set_xticks(x2)
    ax2.set_xticklabels(p2_models, fontsize=8.2, fontweight="bold", color="#0F172A")
    ax2.set_ylim(0, 122)
    ax2.set_ylabel("Exact Match Accuracy (%) — N=1,000 Cases", fontsize=9.5, fontweight="bold", color="#1E293B")
    ax2.set_title("2. 1,000-Task Benchmark (acceptance-v4)\nArrmes v1.4 Beats 4B Reference at 1/280th Size", fontsize=10.8, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="y")
    ax2.legend(
        handles=[
            mpatches.Patch(facecolor="#CBD5E1", edgecolor="#334155", label="Track A: Raw Model Output"),
            mpatches.Patch(facecolor="#0284C7", edgecolor="#0F172A", label="Track B: Full System (+ v1.4 Verifier)"),
            mpatches.Patch(facecolor="#15803D", edgecolor="#0F172A", label="Arrmes v1.4 ★ NeuralRouterV3 (100%)"),
            plt.Line2D([0], [0], color="#DC2626", linestyle="--", linewidth=1.4, label="99.0% Acceptance Gate"),
        ],
        loc="upper right", fontsize=7.5, framealpha=0.95
    )

    # -------------------------------------------------------------------------
    # Panel 3: Locked acceptance-v5 Zero-Leakage Suite (N=2,460 + Stratified Slice)
    # -------------------------------------------------------------------------
    p3_models = [
        "Arrmes v1.4 ★\n(NeuralRouterV3\n7.5 MiB, N=2,460)",
        "Arrmes v1.4\n(Qwen3-0.6B +\nVerifier, N=200)",
        "Arrmes v1.4\n(FG-270M v7 +\nVerifier, N=200)",
        "Arrmes Public v5\n(FG-270M v5 +\nVerifier, N=200)",
        "Deterministic\nRule Baseline\n(N=2,460)",
    ]
    p3_pos_raw = [99.8, 88.0, 70.0, 68.0, 16.6]
    p3_pos_ver = [99.8, 91.0, 91.0, 78.0, 16.6]
    p3_neg_ver = [100.0, 97.0, 88.0, 87.0, 21.9]

    x3 = np.arange(len(p3_models))
    w3 = 0.27
    b3_r = ax3.bar(x3 - w3, p3_pos_raw, width=w3, label="Positive Track A (Raw Model)", color="#CBD5E1", edgecolor="#334155", linewidth=0.7, zorder=3)
    b3_p = ax3.bar(x3, p3_pos_ver, width=w3, label="Positive Track B (Verified System)", color="#0284C7", edgecolor="#0F172A", linewidth=0.7, zorder=3)
    b3_n = ax3.bar(x3 + w3, p3_neg_ver, width=w3, label="Hard-Negative Track B (Safe Abstention)", color="#15803D", edgecolor="#0F172A", linewidth=0.7, zorder=3)

    for b_idx, (bars, col) in enumerate(((b3_r, "#475569"), (b3_p, "#0369A1"), (b3_n, "#14532D"))):
        for m_idx, bar in enumerate(bars):
            v = bar.get_height()
            lbl = "100%" if v == 100.0 else f"{v:.1f}%"
            y_off = 3.8 if (b_idx == 1 and m_idx in (0, 4)) else 1.2
            ax3.text(
                bar.get_x() + bar.get_width() / 2, v + y_off,
                lbl, ha="center", va="bottom",
                fontsize=7.0, fontweight="bold", color=col
            )

    ax3.axhline(99.0, color="#DC2626", linestyle="--", linewidth=1.4, label="99.0% Acceptance Gate")
    ax3.set_xticks(x3)
    ax3.set_xticklabels(p3_models, fontsize=8.0, fontweight="bold", color="#0F172A")
    ax3.set_ylim(0, 124)
    ax3.set_ylabel("Exact Match / Safe Abstention Accuracy (%)", fontsize=9.5, fontweight="bold", color="#1E293B")
    ax3.set_title("3. Locked Zero-Leakage Suite (acceptance-v5)\n99.8% Positive (1,257/1,260) & 100% Hard-Neg (1,200/1,200)", fontsize=10.8, fontweight="bold", color="#0F172A", pad=10)
    ax3.grid(axis="y")
    ax3.legend(loc="upper right", fontsize=7.6, framealpha=0.95)

    fig.suptitle(
        "Arrmes v1.4 vs. Similar Open Models: Raw Model Output vs. Full Verified System Across 3 Benchmark Suites",
        fontsize=14.5, fontweight="bold", color="#0F172A", y=0.99
    )
    fig.text(
        0.01, 0.005,
        "Measured on Apple M4 Pro. Track A reports strict native/translated model accuracy before verification; Track B reports complete system accuracy with the v1.4 boundary verifier (0 correct predictions damaged; 0 unauthorized writes).",
        fontsize=8.8, color="#475569"
    )
    fig.tight_layout(rect=[0.0, 0.025, 1.0, 0.94])
    _save_both(fig, "arrmes_v7_acceptance_v5_accuracy_bounds", extra_stems=["arrmes_v7_open_model_accuracy_comparison"])


def make_chart_2_claude_sonnet_and_resources() -> None:
    """3-panel comparison: Task Completion Time, Token Usage (Claude Sonnet With vs Without Arrmes), and Local RAM/VRAM Footprint."""
    fig, (ax1, ax2, ax3) = plt.subplots(
        1, 3, figsize=(22.5, 7.4), gridspec_kw={"width_ratios": [1.16, 1.06, 1.26]}
    )
    fig.patch.set_facecolor("#FFFFFF")

    # -------------------------------------------------------------------------
    # Panel 1: Task Completion Time — Claude Sonnet Without vs. With Arrmes
    # -------------------------------------------------------------------------
    t_labels = [
        "Without Arrmes:\nClaude Sonnet 4.6\n(3-Turn Raw API / MCP\nDiscovery & JSON Parse)",
        "Without Arrmes:\nClaude Sonnet 4.6\n(1-Turn Direct Call w/\n23 Media Tool Schemas)",
        "With Arrmes Tool:\nClaude Sonnet 4.6 +\nArrmes Single Tool\n(1 Compact Delegate Call)",
        "With Arrmes Standalone:\nArrmes Qwen3-0.6B v7\n(Local 4-Bit MLX + Verifier)",
        "With Arrmes Standalone:\nArrmes FG-270M v7\n(Local 4-Bit MLX + Verifier)",
        "With Arrmes Standalone ★:\nArrmesNeuralRouterV3\n(Local CPU + HTTP Execute)",
    ]
    t_ms = [14200.0, 3450.0, 1120.0, 114.4, 43.5, 1.11]
    t_colors = ["#DC2626", "#F97316", "#0284C7", "#0EA5E9", "#10B981", "#15803D"]

    y1 = np.arange(len(t_labels))
    bars1 = ax1.barh(y1, t_ms, color=t_colors, edgecolor="#0F172A", linewidth=0.8, height=0.58, zorder=3)
    ax1.set_xscale("log")
    ax1.set_xlim(0.5, 110000.0)
    ax1.set_xticks([1, 10, 100, 1000, 10000])
    ax1.set_xticklabels(["1 ms ★", "10 ms", "100 ms", "1 s", "10 s"], fontsize=8.0, fontweight="bold")
    ax1.xaxis.set_minor_formatter(plt.NullFormatter())
    ax1.axvline(250.0, color="#DC2626", linestyle=":", linewidth=1.3, label="250 ms Local Instant-UX Budget")

    annotations_p1 = [
        "14.2 s (baseline multi-step API)",
        "3.45 s (single-turn 23 schemas)",
        "1.12 s (3.1x–12.7x faster w/ tool)",
        "114.4 ms (30x–124x faster local)",
        "43.5 ms (79x–326x faster local)",
        "1.11 ms HTTP / 0.67 ms route ★ (>3,100x faster)",
    ]
    for idx, (bar, ms, note) in enumerate(zip(bars1, t_ms, annotations_p1)):
        col = "#14532D" if idx >= 4 else ("#0369A1" if idx in (2, 3) else "#0F172A")
        ax1.text(
            ms * 1.18, bar.get_y() + bar.get_height() / 2,
            note, va="center", ha="left",
            fontsize=7.8, fontweight="bold", color=col
        )

    ax1.set_yticks(y1)
    ax1.set_yticklabels(t_labels, fontsize=8.3, fontweight="bold", color="#0F172A")
    ax1.invert_yaxis()
    ax1.set_xlabel("Task Completion Time — Log Scale (Lower is Better)", fontsize=9.3, fontweight="bold", color="#1E293B")
    ax1.set_title("1. Task Completion Time: Claude Sonnet Without vs. With Arrmes\nCuts Cloud Turn Time 3x–12x (or >3,100x in Standalone Local Mode)", fontsize=10.6, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="x", which="major")
    ax1.legend(loc="lower right", fontsize=8.0, framealpha=0.95)

    # -------------------------------------------------------------------------
    # Panel 2: Claude Sonnet Token Usage per Task & Cost per 1,000 Tasks
    # -------------------------------------------------------------------------
    tok_labels = [
        "Without Arrmes:\nClaude Sonnet\n3-Turn Raw API\n(Schemas+JSON)",
        "Without Arrmes:\nClaude Sonnet\n1-Turn Direct\n(23 Schemas)",
        "With Arrmes:\nClaude Sonnet\n+ 1 Tool Call\n(-91% to -96%)",
        "With Arrmes ★:\nStandalone\nLocal Router\n(Zero Cloud)",
    ]
    input_toks = [9400, 3680, 310, 0]
    output_toks = [450, 140, 50, 0]
    total_toks = [i + o for i, o in zip(input_toks, output_toks)]
    cost_per_1k = [(i * 3.0 + o * 15.0) / 1000.0 for i, o in zip(input_toks, output_toks)]

    x2 = np.arange(len(tok_labels))
    w2 = 0.48
    b_in = ax2.bar(x2, input_toks, width=w2, label="Claude Input Tokens (Schemas + API Payloads)", color="#F97316", edgecolor="#0F172A", linewidth=0.8, zorder=3)
    b_out = ax2.bar(x2, output_toks, bottom=input_toks, width=w2, label="Claude Output Tokens (Tool Calls + Summary)", color="#DC2626", edgecolor="#0F172A", linewidth=0.8, zorder=3)

    b_in[2].set_facecolor("#0EA5E9")
    b_out[2].set_facecolor("#0284C7")

    for i, (tot, cost) in enumerate(zip(total_toks, cost_per_1k)):
        if tot > 0:
            col = "#0369A1" if i == 2 else "#0F172A"
            extra = "\n(-91% to -96%)" if i == 2 else ""
            ax2.text(
                x2[i], tot + 200,
                f"{tot:,} tok/task\n(${cost:.2f} / 1k){extra}",
                ha="center", va="bottom", fontsize=7.8, fontweight="bold", color=col
            )
        else:
            ax2.text(
                x2[i], 2350,
                "0 Claude Tokens ★\n100% Local\n($0.00 API Cost)",
                ha="center", va="bottom", fontsize=7.8, fontweight="bold", color="#15803D",
                bbox={"boxstyle": "round,pad=0.28", "facecolor": "#F0FDF4", "edgecolor": "#16A34A", "linewidth": 1.1}
            )

    ax2.set_xticks(x2)
    ax2.set_xticklabels(tok_labels, fontsize=7.9, fontweight="bold", color="#0F172A")
    ax2.set_ylim(0, 11800)
    ax2.set_ylabel("Claude Sonnet Tokens per Task (Lower is Better)", fontsize=9.3, fontweight="bold", color="#1E293B")
    ax2.set_title("2. Claude Sonnet Token Use: Without vs. With Arrmes\nEliminates 2,532-Token Schema Prefill & Raw API JSON Dumps", fontsize=10.6, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="y")
    ax2.legend(loc="upper right", fontsize=7.8, framealpha=0.95)

    # -------------------------------------------------------------------------
    # Panel 3: Local RAM / VRAM & Disk Weights Across Arrmes & Open Models
    # -------------------------------------------------------------------------
    mem_models = [
        "Arrmes v1.4 ★\nNeuralRouterV3 (CPU)",
        "Arrmes v1.4\nFG-270M v7 (4-Bit MLX)",
        "IBM Granite 4.0 350M\n(Community Q4 MLX)",
        "Arrmes v1.4\nQwen3-0.6B v7 (4-Bit MLX)",
        "Arrmes Qwen3-0.6B\n(GGUF Q4 RTX 4070TiS)",
        "Arrmes Qwen3-0.6B\n(GGUF Q4 CPU 4-Thread)",
        "Qwen3-1.7B Instruct\n(Open 1.7B Q4 MLX)",
        "Qwen3-4B Instruct 2507\n(Open 4B Q4 MLX)",
    ]
    weights_mib = [7.5, 175.8, 189.2, 330.9, 378.3, 378.3, 923.2, 2158.2]
    ram_rss_mib = [80.7, 797.5, 431.7, 676.0, 736.1, 1417.4, 1271.6, 2510.5]
    vram_metal_mib = [0.0, 1061.0, 1327.5, 1593.3, 1082.0, 0.0, 2307.3, 3841.9]

    y3 = np.arange(len(mem_models))
    h3 = 0.25
    ax3.barh(y3 - h3, weights_mib, height=h3, label="Disk Weights (MiB)", color="#CBD5E1", edgecolor="#334155", linewidth=0.6, zorder=3)
    ax3.barh(y3, ram_rss_mib, height=h3, label="Process RAM / Host RSS (MiB)", color="#0284C7", edgecolor="#0F172A", linewidth=0.6, zorder=3)
    ax3.barh(y3 + h3, vram_metal_mib, height=h3, label="GPU VRAM / Apple Metal Peak* (MiB)", color="#6366F1", edgecolor="#0F172A", linewidth=0.6, zorder=3)

    for i in range(len(mem_models)):
        max_v = max(weights_mib[i], ram_rss_mib[i], vram_metal_mib[i])
        if i == 0:
            lbl = "7.5 MiB disk | 80.7 MiB RAM (63.5 MiB Linux) | 0 MiB VRAM ★"
            col = "#14532D"
        elif vram_metal_mib[i] == 0:
            lbl = f"{weights_mib[i]:.0f} MiB disk | {ram_rss_mib[i]:.0f} MiB RAM | 0 MiB VRAM"
            col = "#0F172A"
        else:
            lbl = f"{weights_mib[i]:.0f}M disk | {ram_rss_mib[i]:.0f}M RAM | {vram_metal_mib[i]:.0f}M VRAM/Metal"
            col = "#0F172A"
        ax3.text(max_v + 55, y3[i], lbl, va="center", ha="left", fontsize=7.4, fontweight="bold", color=col)

    ax3.axvline(1536.0, color="#DC2626", linestyle=":", linewidth=1.3, label="1.5 GiB (1,536 MiB) RAM Budget")
    ax3.set_yticks(y3)
    ax3.set_yticklabels(mem_models, fontsize=8.2, fontweight="bold", color="#0F172A")
    ax3.set_ylim(len(mem_models) - 0.35, -1.25)
    ax3.set_xlim(0, 6100)
    ax3.set_xlabel("Memory / Storage Footprint in MiB (Lower is Better)", fontsize=9.3, fontweight="bold", color="#1E293B")
    ax3.set_title("3. Local RAM, GPU VRAM & Weight Size vs. Open Models\nNeuralRouterV3 Uses 80.7 MiB RAM & 0 VRAM (31x Smaller RAM than 4B)", fontsize=10.6, fontweight="bold", color="#0F172A", pad=10)
    ax3.grid(axis="x")
    ax3.legend(loc="upper right", fontsize=7.5, framealpha=0.95, ncol=2)

    fig.suptitle(
        "With vs. Without Arrmes: Claude Sonnet Task Completion Time, Token Usage, & Local RAM / VRAM Footprint",
        fontsize=14.5, fontweight="bold", color="#0F172A", y=0.99
    )
    fig.text(
        0.01, 0.005,
        "*On Apple Silicon (MLX), process RSS and Metal unified allocations share physical RAM and must not be added. RTX 4070 Ti SUPER row measures dedicated NVIDIA GPU VRAM (1,082 MiB). Claude Sonnet token counts compare loading all 23 raw media tool schemas (2,532 prompt tokens) + raw API JSON vs. 1 compact Arrmes tool call (360 tokens) or standalone local execution (0 cloud tokens).",
        fontsize=8.5, color="#475569"
    )
    fig.tight_layout(rect=[0.0, 0.025, 1.0, 0.94])
    _save_both(fig, "arrmes_v7_runtime_latency_memory_footprint", extra_stems=["arrmes_v7_claude_sonnet_and_resource_comparison"])


def make_chart_3_error_taxonomy_av4() -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16.5, 5.8), gridspec_kw={"width_ratios": [1.18, 1.0]})
    fig.patch.set_facecolor("#FFFFFF")
    fig.suptitle(
        "Frozen Acceptance-v4 (1,000 Cases): Error Taxonomy Attribution & General Verifier Repair Progression",
        fontsize=13.8, fontweight="bold", color="#0F172A", y=0.99,
    )

    configs = [
        "Arrmes v1.4 ★\n(NeuralRouterV3)",
        "Arrmes v1.4\n(Qwen3-0.6B)",
        "Qwen3-4B\nReference",
        "Untuned\nQwen3-0.6B",
        "Deterministic\nBaseline",
    ]
    x = np.arange(len(configs))
    w = 0.25

    raw_scores = [1000, 770, 782, 396, 89]
    pre_v131_ver = [1000, 678, 720, 439, 105]
    v7_ver = [1000, 981, 903, 628, 193]

    b_raw = ax1.bar(x - w, raw_scores, width=w, label="Track A: Raw Model Exact (/1000)", color="#CBD5E1", edgecolor="#334155")
    b_old = ax1.bar(x, pre_v131_ver, width=w, label="Track B: Snapshot e5e76b8 Verifier (/1000)", color="#F59E0B", edgecolor="#1E293B")
    b_new = ax1.bar(x + w, v7_ver, width=w, label="Track B: Arrmes v1.4 Verifier (/1000, 0 Damaged)", color="#0284C7", edgecolor="#0F172A")
    b_new[0].set_facecolor("#15803D")

    ax1.axhline(990, color="#DC2626", linestyle="--", linewidth=1.4, label="990/1000 (99.0%) Target")
    ax1.set_ylim(0, 1240)
    ax1.set_xticks(x)
    ax1.set_xticklabels(configs, fontsize=9, fontweight="bold", color="#0F172A")
    ax1.set_ylabel("Exact Proposals (/1,000 Cases)", fontsize=10, fontweight="bold", color="#1E293B")
    ax1.set_title("A. Same-M4 Frozen 1,000-Case Progression Across Models", fontsize=11, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="y")
    ax1.legend(
        handles=[
            mpatches.Patch(facecolor="#CBD5E1", edgecolor="#334155", label="Track A: Raw Model Exact (/1000)"),
            mpatches.Patch(facecolor="#F59E0B", edgecolor="#1E293B", label="Track B: Snapshot e5e76b8 Verifier (/1000)"),
            mpatches.Patch(facecolor="#0284C7", edgecolor="#0F172A", label="Track B: Arrmes v1.4 Verifier (/1000, 0 Damaged)"),
            mpatches.Patch(facecolor="#15803D", edgecolor="#0F172A", label="Arrmes v1.4 ★ NeuralRouterV3 (1000/1000)"),
            plt.Line2D([0], [0], color="#DC2626", linestyle="--", linewidth=1.4, label="990/1000 (99.0%) Target"),
        ],
        loc="upper right", fontsize=7.5, framealpha=0.95
    )

    for bars in (b_raw, b_old, b_new):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(
                f"{int(h)}",
                xy=(bar.get_x() + bar.get_width() / 2, h),
                xytext=(0, 3), textcoords="offset points",
                ha="center", va="bottom", fontsize=7.8, fontweight="bold", color="#0F172A",
            )

    categories = [
        "Wrong Clarify Slot or\nAbstention Reason (Repaired)",
        "Over-Predicting Concrete Tool\non Ambiguous/Unsafe (Repaired)",
        "Wrong Concrete Tool Selected\non Valid/Contrast Call (Repaired)",
        "Wrong or Missing Typed\nArguments/Entities (Repaired)",
        "Residual Unrepaired Model\nIntent Confusions (19/1000)",
        "Correct Raw Predictions\nDamaged by v1.4 Verifier (0/1000)",
    ]
    counts = [110, 56, 31, 14, 19, 0]
    colors = ["#0EA5E9", "#10B981", "#3B82F6", "#6366F1", "#F43F5E", "#94A3B8"]
    y_pos = np.arange(len(categories))

    bars_b = ax2.barh(y_pos, counts, color=colors, height=0.60, edgecolor="#1E293B")
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels(categories, fontsize=8.8, fontweight="bold", color="#0F172A")
    ax2.invert_yaxis()
    ax2.set_xlim(0, 132)
    ax2.set_xlabel("Number of Cases out of 230 Raw Model Errors", fontsize=10, fontweight="bold", color="#1E293B")
    ax2.set_title("B. Released Arrmes (770/1000 Raw) Error Attribution & Repair\n(211/230 Repaired by v1.4 Verifier; 0 Correct Calls Damaged)", fontsize=10.5, fontweight="bold", color="#0F172A", pad=10)
    ax2.grid(axis="x")

    for bar in bars_b:
        w_val = bar.get_width()
        ax2.annotate(
            f"{int(w_val)} cases",
            xy=(w_val, bar.get_y() + bar.get_height() / 2),
            xytext=(5, 0), textcoords="offset points",
            ha="left", va="center", fontsize=8.5, fontweight="bold", color="#0F172A",
        )

    fig.tight_layout(rect=[0, 0, 1, 0.94])
    _save_both(fig, "arrmes_v7_error_taxonomy_and_verifier_repair")


def make_chart_4_track_c_workflows() -> None:
    fig, ax = plt.subplots(figsize=(13.0, 5.2))
    fig.patch.set_facecolor("#FFFFFF")
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
    colors = ["#0284C7", "#0EA5E9", "#10B981", "#059669", "#6366F1", "#15803D"]

    bars = ax.barh(y, pcts, color=colors, height=0.58, edgecolor="#0F172A")
    ax.axvline(99.0, color="#DC2626", linestyle="--", linewidth=1.5, label="99.0% Acceptance Threshold")
    ax.set_xlim(0, 116)
    ax.set_yticks(y)
    ax.set_yticklabels(groups, fontsize=9.0, fontweight="bold", color="#0F172A")
    ax.invert_yaxis()
    ax.set_xlabel("Verified End-to-End Workflow Completion Rate (%)", fontsize=10.2, fontweight="bold", color="#1E293B")
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
            ha="left", va="center", fontsize=9.2, fontweight="bold", color="#0F172A",
        )

    fig.tight_layout()
    _save_both(fig, "arrmes_v7_track_c_workflow_scorecard")


if __name__ == "__main__":
    make_chart_1_open_model_accuracy()
    make_chart_2_claude_sonnet_and_resources()
    make_chart_3_error_taxonomy_av4()
    make_chart_4_track_c_workflows()
    print("Generated all PNG + PDF chart pairs in docs/figures/ and work/figures/.")
