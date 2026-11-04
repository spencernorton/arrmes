#!/usr/bin/env python3
"""Render the two front-page comparisons from recorded evaluation JSON.

Requires matplotlib. This script makes no model or service calls and does not
modify the recorded results. Run from any directory; output defaults to
docs/figures, or pass --output-dir for a separate rendering directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch


ROOT = Path(__file__).resolve().parents[1]
INK = "#162738"
BLUE = "#2563EB"
GREEN = "#087F6B"
GREY = "#A8B6C8"


def read_record(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def style_axis(ax: plt.Axes) -> None:
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#E2E8F0", linewidth=0.8)
    ax.tick_params(axis="both", length=0, labelcolor=INK, pad=9)
    for spine in ax.spines.values():
        spine.set_visible(False)


def save(fig: plt.Figure, output: Path, name: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    fig.savefig(output / name, dpi=180, facecolor="white",
                metadata={"Software": "Arrmes comparison charts"})
    plt.close(fig)


def accuracy_chart(output: Path) -> None:
    record = read_record("experiments/acceptance-v5/summary.json")
    models = record["mlx_generative_models_stratified_200"]
    rows = [
        ("released_qwen3_06b_v6", "Qwen3-0.6B v6\nFP16 + public LoRA"),
        ("trained_qwen3_06b_v7_fp16_lora", "Qwen3-0.6B v7\nFP16 + LoRA"),
        ("trained_qwen3_06b_v7_4bit", "Qwen3-0.6B v7\n4-bit MLX"),
        ("public_functiongemma_270m_v5", "FunctionGemma-270M v5\nBF16 + public LoRA"),
        ("trained_functiongemma_270m_v7_4bit", "FunctionGemma-270M v7\n4-bit MLX"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 7.4), sharey=True)
    fig.subplots_adjust(left=0.235, right=0.97, top=0.73, bottom=0.24, wspace=0.14)
    fig.text(0.035, 0.94, "Accuracy across Arrmes model checkpoints",
             fontsize=23, weight="bold", color=INK)
    fig.text(0.035, 0.887,
             "Locked acceptance-v5 · the same 100 positive + 100 negative cases for all five checkpoints",
             fontsize=12.5, color="#475569")
    fig.legend(handles=[Patch(color=GREY, label="Track A · raw model output"),
                        Patch(color=BLUE, label="Track B · after Arrmes verification")],
               loc="upper left", bbox_to_anchor=(0.029, 0.856),
               frameon=False, ncol=2, fontsize=12)

    panels = [("positive_v2_stratified_100", "Positive requests"),
              ("negative_stratified_100", "Negative & ambiguous requests")]
    for ax, (split, title) in zip(axes, panels):
        style_axis(ax)
        for index, (key, _) in enumerate(rows):
            result = models[key][split]
            assert result["total_cases"] == 100
            assert result["total_clusters"] == 20
            raw = result["track_a_direct_model"]["joint_exact"]
            verified = result["track_b_complete_system"]["verified_joint_exact"]
            for offset, count, color in [(-0.18, raw, GREY), (0.18, verified, BLUE)]:
                ax.barh(index + offset, count, height=0.30, color=color, zorder=3)
                ax.text(count + 1.5, index + offset, f"{count}%", va="center",
                        color=INK, fontsize=11.5, weight="bold")
        ax.set_xlim(0, 112)
        ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
        ax.set_title(f"{title}\n100 cases · 20 clusters", loc="left", pad=16,
                     fontsize=13, weight="bold", color=INK)
        ax.set_xlabel("Exact expected proposals", labelpad=12, color=INK, fontsize=11.5)
    axes[0].set_yticks(range(len(rows)), [label for _, label in rows], fontsize=12)
    axes[0].invert_yaxis()
    axes[1].tick_params(axis="y", left=False)
    fig.text(0.035, 0.035,
             "Synthetic clustered inputs; these scores measure proposals, not service execution.\n"
             "Recorded 2026-11-02 · Source: experiments/acceptance-v5/summary.json",
             fontsize=11, color="#475569", linespacing=1.65)
    save(fig, output, "arrmes-model-accuracy-comparison.png")


def runtime_chart(output: Path) -> None:
    profiles = read_record("experiments/runtime-profile-v7.json")["profiles"]
    rows = [
        ("arrmes_neural_router_v3_macos_arm64_cpu", "Neural router v3\nCPU · 200 warm samples"),
        ("functiongemma_270m_v7_4bit_macos_arm64_mlx", "FunctionGemma-270M v7\n4-bit MLX · 40 warm samples"),
        ("qwen3_06b_v7_4bit_macos_arm64_mlx", "Qwen3-0.6B v7\n4-bit MLX · 40 warm samples"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 6.6), sharey=True,
                             gridspec_kw={"width_ratios": [1.25, 1]})
    fig.subplots_adjust(left=0.25, right=0.95, top=0.70, bottom=0.26, wspace=0.24)
    fig.text(0.035, 0.935, "Local routing speed and model size",
             fontsize=23, weight="bold", color=INK)
    fig.text(0.035, 0.875,
             "Recorded Apple M4 Pro profiles · warm routing + verification · lower is better",
             fontsize=12.5, color="#475569")
    fig.legend(handles=[Patch(color=GREEN, label="Warm p50"),
                        Patch(color=GREY, label="Warm p95")],
               loc="upper left", bbox_to_anchor=(0.029, 0.84),
               frameon=False, ncol=2, fontsize=12)
    for ax in axes:
        style_axis(ax)
    for index, (key, _) in enumerate(rows):
        profile = profiles[key]
        timing = profile["routing_and_verifier_ms"]
        for offset, percentile, color in [(-0.18, "p50", GREEN), (0.18, "p95", GREY)]:
            value = timing[percentile]
            axes[0].barh(index + offset, value, height=0.30, color=color, zorder=3)
            axes[0].text(value * 1.15, index + offset, f"{value:g} ms",
                         va="center", fontsize=11.5, color=INK, weight="bold")
        size = profile["artifact_mib"]
        axes[1].barh(index, size, height=0.48, color=BLUE, zorder=3)
        axes[1].text(size + 8, index, f"{size:.2f} MiB", va="center",
                     fontsize=11.5, color=INK, weight="bold")
    axes[0].set_xscale("log")
    axes[0].set_xlim(0.1, 650)
    axes[0].set_xticks([0.1, 1, 10, 100], ["0.1", "1", "10", "100"])
    axes[0].minorticks_off()
    axes[0].set_title("Warm routing latency", loc="left", pad=14,
                      fontsize=13, weight="bold", color=INK)
    axes[0].set_xlabel("Milliseconds · logarithmic scale", labelpad=12,
                       color=INK, fontsize=11.5)
    axes[1].set_xlim(0, 465)
    axes[1].set_xticks([0, 100, 200, 300, 400])
    axes[1].set_title("Model files on disk", loc="left", pad=14,
                      fontsize=13, weight="bold", color=INK)
    axes[1].set_xlabel("MiB · linear scale", labelpad=12, color=INK, fontsize=11.5)
    axes[0].set_yticks(range(len(rows)), [label for _, label in rows], fontsize=12)
    axes[0].invert_yaxis()
    axes[1].tick_params(axis="y", left=False)
    fig.text(0.035, 0.035,
             "Routing microbenchmarks; excludes service/network time. Model size is not runtime RAM.\n"
             "Recorded 2026-11-02 · Source: experiments/runtime-profile-v7.json",
             fontsize=11, color="#475569", linespacing=1.65)
    save(fig, output, "arrmes-local-runtime-comparison.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/figures")
    args = parser.parse_args()
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    accuracy_chart(args.output_dir)
    runtime_chart(args.output_dir)


if __name__ == "__main__":
    main()
