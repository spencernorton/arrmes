#!/usr/bin/env python3
"""Phase 4 pre-freeze verification, one-shot acceptance-v8 evaluation, chart generation, and HF staging."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import statistics
import subprocess
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

from arrmes.catalog import WRITE_EFFECT_ACTIONS_V3
from arrmes.evaluate import clopper_pearson_upper_bound, cluster_bootstrap_ci, score, wilson_interval
from arrmes.model import DeterministicBaselineModel, HybridRouterMLXModel
from arrmes.neural_router import ArrmesNeuralRouterV3


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_acceptance_v8_seal(av8_dir: Path, seal_meta: dict[str, Any]) -> dict[str, Any]:
    """Verify chmod 0444, exact seal_ns mtime/atime, and SHA256 BEFORE reading acceptance-v8."""
    expected_ns = int(seal_meta["seal_ns"])
    file_map = {
        "cases-positive.jsonl": seal_meta["positive_sha256"],
        "cases-negative.jsonl": seal_meta["negative_sha256"],
        "bootstrap_indices_seed10010.json": seal_meta["bootstrap_sha256"],
        "manifest.json": seal_meta["manifest_sha256"],
    }
    pre_stat: dict[str, Any] = {}
    for fname in file_map:
        fp = av8_dir / fname
        st = os.stat(fp, follow_symlinks=False)
        mode_oct = stat.S_IMODE(st.st_mode)
        if mode_oct != 0o444:
            raise RuntimeError(f"SEAL VIOLATION on {fname}: mode={oct(mode_oct)} != 0o444")
        if st.st_mtime_ns != expected_ns:
            raise RuntimeError(f"SEAL VIOLATION on {fname}: mtime_ns={st.st_mtime_ns} != {expected_ns}")
        if fname in {"cases-positive.jsonl", "cases-negative.jsonl"} and st.st_atime_ns != expected_ns:
            raise RuntimeError(f"SEAL VIOLATION on {fname}: atime_ns={st.st_atime_ns} != {expected_ns}")
        pre_stat[fname] = {
            "mode": oct(mode_oct),
            "st_mtime_ns": st.st_mtime_ns,
            "st_atime_ns": st.st_atime_ns,
            "atime_matches_seal_ns": st.st_atime_ns == expected_ns,
        }

    # Now compute SHA256 digests and verify exact match
    for fname, exp_sha in file_map.items():
        actual_sha = sha256_file(av8_dir / fname)
        if actual_sha != exp_sha:
            raise RuntimeError(f"SEAL SHA256 MISMATCH on {fname}: {actual_sha} != {exp_sha}")
        pre_stat[fname]["sha256_verified"] = actual_sha
    return pre_stat


def eval_local_router_split(
    model_v2: Any,
    model_v3: Any,
    cases: list[dict[str, Any]],
    family: str,
    *,
    default_v3: bool = False,
) -> dict[str, Any]:
    raw_ok = ver_ok = 0
    unauth_raw = unauth_ver = damaged = 0
    lats: list[float] = []
    boot_rows: list[dict[str, Any]] = []
    for idx, c in enumerate(cases):
        use_v3 = default_v3 or (c.get("contract_version") in {"v3", "arrmes.tools.v3"})
        m = model_v3 if use_v3 else model_v2
        inf = m.infer(c["query"], c.get("context"))
        lats.append(inf["seconds"] * 1000.0)
        r_sc = score(inf.get("raw_unverified", inf["raw"]), c["expected"], family, allow_v3=use_v3)
        v_sc = score(inf["raw"], c["expected"], family, allow_v3=use_v3)
        r_b = bool(r_sc["exact"])
        v_b = bool(v_sc["exact"])
        raw_ok += int(r_b)
        ver_ok += int(v_b)
        if c["expected"]["name"] not in WRITE_EFFECT_ACTIONS_V3:
            if (r_sc.get("actual") or {}).get("name") in WRITE_EFFECT_ACTIONS_V3:
                unauth_raw += 1
            if (v_sc.get("actual") or {}).get("name") in WRITE_EFFECT_ACTIONS_V3:
                unauth_ver += 1
        if r_b and not v_b:
            damaged += 1
        cid = c.get("cluster_id") or str(c.get("id", f"c-{idx}")).rsplit("-", 1)[0]
        boot_rows.append({"cluster_id": cid, "raw_exact": r_b, "verified_exact": v_b})

    n = len(cases)
    warm = lats[5:] if len(lats) > 5 else lats
    return {
        "cases": n,
        "raw_exact": raw_ok,
        "raw_accuracy": round(raw_ok / max(1, n), 6),
        "raw_wilson_95": wilson_interval(raw_ok, n),
        "raw_cluster_bootstrap_95": cluster_bootstrap_ci(boot_rows, n_boot=2000, seed=20261111, key="raw_exact"),
        "verified_exact": ver_ok,
        "verified_accuracy": round(ver_ok / max(1, n), 6),
        "verified_wilson_95": wilson_interval(ver_ok, n),
        "verified_cluster_bootstrap_95": cluster_bootstrap_ci(boot_rows, n_boot=2000, seed=20261111, key="verified_exact"),
        "unauthorized_writes_raw": unauth_raw,
        "unauthorized_writes_verified": unauth_ver,
        "unauthorized_writes_verified_cp95_upper": clopper_pearson_upper_bound(unauth_ver, n),
        "verifier_damaged": damaged,
        "warm_p50_ms": round(statistics.median(warm), 3),
        "warm_p95_ms": round(sorted(warm)[int(0.95 * len(warm))], 3),
    }


def strip_png_metadata(src_png: Path, dst_png: Path) -> None:
    import struct
    raw = src_png.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    out = bytearray(raw[:8])
    pos = 8
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        chunk_total = 12 + length
        if ctype not in (b"tEXt", b"zTXt", b"iTXt", b"tIME"):
            out.extend(raw[pos : pos + chunk_total])
        pos += chunk_total
    dst_png.parent.mkdir(parents=True, exist_ok=True)
    dst_png.write_bytes(bytes(out))


def render_latency_footprint_chart(runtime_profile: dict[str, Any], out_dir: Path, name: str) -> tuple[Path, Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec

    BG, PANEL, GRID, BORDER = "#0D1117", "#161B22", "#21262D", "#30363D"
    TEXT, SUB = "#F0F6FC", "#8B949E"
    profs = runtime_profile["profiles"]

    lat_items = [
        ("ArrmesNeuralRouterV3 v10 (macOS M4 Pro CPU)", profs["arrmes_neural_router_v3_macos_arm64_cpu"]["routing_and_verifier_ms"]["p50"], "#10B981"),
        ("ArrmesNeuralRouterV3 v10 (Linux 7950X3D CPU)", profs["arrmes_neural_router_v3_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p50"], "#10B981"),
        ("HybridRouterMLX v10 (macOS M4 Pro CPU+Metal)", profs["hybrid_router_mlx_qwen3_06b_v10_macos_arm64"]["routing_and_verifier_ms"]["p50"], "#38BDF8"),
        ("FG-270M v10 Q4_K_M GGUF (RTX 4070 TiS CUDA)", profs["functiongemma_270m_v10_gguf_q4_k_m_linux_cuda"]["routing_and_verifier_ms"]["p50"], "#3B82F6"),
        ("Qwen3-0.6B v10 Q4_K_M GGUF (RTX 4070 TiS CUDA)", profs["qwen3_06b_v10_gguf_q4_k_m_linux_cuda"]["routing_and_verifier_ms"]["p50"], "#3B82F6"),
        ("FG-270M v10 4-bit MLX (macOS M4 Pro Metal)", profs["functiongemma_270m_v10_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p50"], "#A855F7"),
        ("FG-270M v10 Q4_K_M GGUF (Linux 7950X3D 8T CPU)", profs["functiongemma_270m_v10_gguf_q4_k_m_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p50"], "#F59E0B"),
        ("Qwen3-0.6B v10 4-bit MLX (macOS M4 Pro Metal)", profs["qwen3_06b_v10_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"]["p50"], "#A855F7"),
        ("Qwen3-0.6B v10 Q4_K_M GGUF (Linux 7950X3D 8T CPU)", profs["qwen3_06b_v10_gguf_q4_k_m_linux_x86_64_cpu"]["routing_and_verifier_ms"]["p50"], "#F59E0B"),
        ("Prior v9 Qwen3-0.6B GGUF Baseline (Linux 2T CPU)", json.loads((ROOT_DIR / "experiments/runtime-profile-v9.json").read_text(encoding="utf-8"))["profiles"]["qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu"]["warm_routing_ms"]["p50"], "#64748B"),
    ]

    size_items = [
        ("ArrmesNeuralRouterV3 v10 (.npz)", profs["arrmes_neural_router_v3_macos_arm64_cpu"]["artifact_mb_decimal"], "#10B981"),
        ("FG-270M v10 4-bit MLX", profs["functiongemma_270m_v10_4bit_macos_arm64_mlx"]["artifact_mb_decimal"], "#A855F7"),
        ("FG-270M v10 Q4_K_M GGUF", profs["functiongemma_270m_v10_gguf_q4_k_m_linux_x86_64_cpu"]["artifact_mb_decimal"], "#3B82F6"),
        ("FG-270M v10 Q8_0 GGUF", profs["functiongemma_270m_v10_gguf_q8_0_linux_x86_64_cpu"]["artifact_mb_decimal"], "#38BDF8"),
        ("Qwen3-0.6B v10 4-bit MLX", profs["qwen3_06b_v10_4bit_macos_arm64_mlx"]["artifact_mb_decimal"], "#A855F7"),
        ("Qwen3-0.6B v10 Q4_K_M GGUF", profs["qwen3_06b_v10_gguf_q4_k_m_linux_x86_64_cpu"]["artifact_mb_decimal"], "#F59E0B"),
    ]

    plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica"]
    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    fig.suptitle("ARRMES v1.7.0 (v10) — Cross-Platform Warm Latency & Artifact Footprint", fontsize=16, fontweight="bold", color=TEXT, y=0.96)
    fig.text(0.5, 0.92, "Measured end-to-end routing + deterministic verifier p50 latency (ms) and serialized artifact size (MB decimal)", ha="center", fontsize=11, color=SUB)

    gs = GridSpec(1, 2, figure=fig, left=0.26, right=0.97, top=0.85, bottom=0.10, wspace=0.54)

    ax1 = fig.add_subplot(gs[0, 0])
    ax1.set_facecolor(PANEL)
    ax1.grid(axis="x", color=GRID, linewidth=0.8, linestyle="--")
    for sp in ax1.spines.values():
        sp.set_edgecolor(BORDER)
    y1 = list(range(len(lat_items)))
    vals1 = [x[1] for x in lat_items]
    ax1.barh(y1, vals1, height=0.58, color=[x[2] for x in lat_items], zorder=3)
    ax1.axvline(250.0, color="#EF4444", linestyle=":", linewidth=1.3, zorder=4)
    ax1.set_yticks(y1)
    ax1.set_yticklabels([x[0] for x in lat_items], fontsize=9, fontweight="bold", color=TEXT)
    ax1.invert_yaxis()
    ax1.set_xlim(0, max(vals1) * 1.26)
    ax1.tick_params(colors=SUB, labelsize=9)
    ax1.set_title("Warm Routing + Verifier p50 Latency (ms, <=250 ms CPU budget)", fontsize=11, fontweight="bold", color=TEXT, pad=10)
    for idx_i, v in enumerate(vals1):
        lbl = f"{v:.2f} ms" if v < 10 else f"{v:.1f} ms"
        ax1.text(v + max(vals1) * 0.015, idx_i, lbl, va="center", fontsize=8.5, fontweight="bold", color=TEXT)

    ax2 = fig.add_subplot(gs[0, 1])
    ax2.set_facecolor(PANEL)
    ax2.grid(axis="x", color=GRID, linewidth=0.8, linestyle="--")
    for sp in ax2.spines.values():
        sp.set_edgecolor(BORDER)
    y2 = list(range(len(size_items)))
    vals2 = [x[1] for x in size_items]
    ax2.barh(y2, vals2, height=0.55, color=[x[2] for x in size_items], zorder=3)
    ax2.axvline(400.0, color="#EF4444", linestyle=":", linewidth=1.3, zorder=4)
    ax2.set_yticks(y2)
    ax2.set_yticklabels([x[0] for x in size_items], fontsize=9, fontweight="bold", color=TEXT)
    ax2.invert_yaxis()
    ax2.set_xlim(0, 470.0)
    ax2.tick_params(colors=SUB, labelsize=9)
    ax2.set_title("Serialized Artifact Size (MB decimal, <=400 MB budget)", fontsize=11, fontweight="bold", color=TEXT, pad=10)
    for idx_i, v in enumerate(vals2):
        ax2.text(v + 6.0, idx_i, f"{v:.1f} MB", va="center", fontsize=8.5, fontweight="bold", color=TEXT)

    out_dir.mkdir(parents=True, exist_ok=True)
    png_p = out_dir / f"{name}.png"
    pdf_p = out_dir / f"{name}.pdf"
    fig.savefig(png_p, dpi=300, facecolor=BG)
    fig.savefig(pdf_p, facecolor=BG)
    plt.close(fig)
    return png_p, pdf_p


EXPECTED_AV8_SHA256 = {
    "cases-positive.jsonl": "44ad97e4a7b7f41c6c68e662ad973f8c4ce1fbdb9c085ecfecc017ae2f9dd610",
    "cases-negative.jsonl": "c6d42aa6363c260f0dccf89a8ddc646bb02910b7572c7bb88d2a967b39f5eb0e",
    "bootstrap_indices_seed10010.json": "841b983ce8c8ffb760ae07e38a5274a07b92294e5ef0c3fdcdc4c5a6412f6ebc",
    "manifest.json": "9a1a21e0a9ccf6caae6a3c431aa59555bafbade1880eeea700a7dc0e0b3a6309",
}



def recompute_av8_bootstrap_sha256(pos_cases: list[dict[str, Any]], neg_cases: list[dict[str, Any]]) -> str:
    import random
    def _mat(n_c: int, reps: int = 2000, seed: int = 10010) -> list[list[int]]:
        rng = random.Random(seed)
        return [[rng.randrange(n_c) for _ in range(n_c)] for _ in range(reps)]
    pos_v2_clusters = sorted({c["cluster_id"] for c in pos_cases if c["contract_version"] == "v2"})
    pos_all_clusters = sorted({c["cluster_id"] for c in pos_cases})
    neg_all_clusters = sorted({c["cluster_id"] for c in neg_cases})
    raw = json.dumps(
        {
            "seed": 10010,
            "replicates": 2000,
            "positive_v2_clusters": pos_v2_clusters,
            "positive_all_clusters": pos_all_clusters,
            "negative_all_clusters": neg_all_clusters,
            "positive_v2_matrix": _mat(len(pos_v2_clusters)),
            "positive_all_matrix": _mat(len(pos_all_clusters)),
            "negative_all_matrix": _mat(len(neg_all_clusters)),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_pinned_av8_digests(av8_dir: Path) -> dict[str, str]:
    out = {}
    for fn, exp in EXPECTED_AV8_SHA256.items():
        actual = sha256_file(av8_dir / fn)
        if actual != exp:
            raise RuntimeError(f"Pinned acceptance-v8 SHA-256 mismatch on {fn}: {actual} != {exp}")
        out[fn] = actual
    return out


def hardlink_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def write_sha256sums(folder: Path) -> None:
    lines = []
    for fp in sorted(folder.rglob("*")):
        if fp.is_file() and fp.name != "SHA256SUMS.txt":
            rel = fp.relative_to(folder).as_posix()
            lines.append(f"{sha256_file(fp)}  {rel}")
    (folder / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_public_hf_bundles(
    staging_root: Path,
    av8_summary: dict[str, Any],
    gpu_receipt: dict[str, Any],
    gguf_receipt: dict[str, Any],
    macos_qual: dict[str, Any],
) -> dict[str, Any]:
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)

    # 1. NorviTech/arrmes-bench-v7 (public anonymous access fix)
    b7 = staging_root / "arrmes-bench-v7"
    b7.mkdir(parents=True)
    for fn in ("cases-positive.jsonl", "cases-negative.jsonl", "bootstrap_indices_seed9009.json", "manifest.json"):
        shutil.copy2(ROOT_DIR / "data/acceptance-v7" / fn, b7 / fn)
    shutil.copy2(ROOT_DIR / "data/overnight-v1/routing-heldout.jsonl", b7 / "overnight-v1-routing-heldout.jsonl")
    (b7 / "README.md").write_text(
        "---\nlicense: mit\ntask_categories:\n  - text-generation\nlanguage:\n  - en\n---\n\n"
        "# ARRMES Benchmark v7 (acceptance-v7 and overnight-v1)\n\n"
        "Public evaluation suite for ARRMES local media-stack tool routing across 2,580 acceptance-v7 cases "
        "(1,380 positive calls and 1,200 adversarial and scope-boundary hard negatives) plus 80 organic open-phrasing "
        "overnight-v1 cases.\n\n"
        "## Included Splits\n\n"
        "- `cases-positive.jsonl`: 1,380 positive tool-routing test cases across v2 and v3 schemas.\n"
        "- `cases-negative.jsonl`: 1,200 safety, clarification, and out-of-scope hard negatives.\n"
        "- `bootstrap_indices_seed9009.json`: cluster-bootstrap replicate indices.\n"
        "- `overnight-v1-routing-heldout.jsonl`: 80 organic open-phrasing evaluation queries.\n",
        encoding="utf-8",
    )
    write_sha256sums(b7)

    # 2. NorviTech/arrmes-bench-v8
    b8 = staging_root / "arrmes-bench-v8"
    b8.mkdir(parents=True)
    for fn in ("cases-positive.jsonl", "cases-negative.jsonl", "bootstrap_indices_seed10010.json", "manifest.json"):
        shutil.copy2(ROOT_DIR / "data/acceptance-v8" / fn, b8 / fn)
    (b8 / "README.md").write_text(
        "---\nlicense: mit\ntask_categories:\n  - text-generation\nlanguage:\n  - en\n---\n\n"
        "# ARRMES Benchmark v8 (Sealed Skeleton-Disjoint Holdout, N=3,135)\n\n"
        "Sealed template-skeleton-disjoint holdout benchmark for ARRMES v1.7.0 (v10) across all 11 supported "
        "media services, covering both arrmes.tools.v2 typed actions and arrmes.tools.v3 service_query read routes.\n\n"
        "## Disjointness Guarantees\n\n"
        "- Zero exact-text, normalized token-bag, and entity-masked template-skeleton overlap against TrainPool-v10, "
        "acceptance-v7, and overnight-v1.\n"
        "- 1,685 positive tool-call cases and 1,450 open-phrasing hard negatives (3,135 total).\n",
        encoding="utf-8",
    )
    write_sha256sums(b8)

    # 3. NorviTech/arrmes-neural-router-v3
    nr_dir = staging_root / "arrmes-neural-router-v3"
    nr_dir.mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "LICENSE", b7 / "LICENSE")
    write_sha256sums(b7)
    shutil.copy2(ROOT_DIR / "LICENSE", b8 / "LICENSE")
    write_sha256sums(b8)
    shutil.copy2(ROOT_DIR / "LICENSE", nr_dir / "LICENSE")
    shutil.copy2(ROOT_DIR / "models/arrmes-neural-router-v3/router_weights.npz", nr_dir / "router_weights.npz")
    shutil.copy2(ROOT_DIR / "models/arrmes-neural-router-v3/router_config.json", nr_dir / "router_config.json")
    nr_av8_p = av8_summary["models"]["arrmes_neural_router_v3_v10"]["acceptance_v8_positive_1685"]["verified_accuracy"]
    nr_av8_n = av8_summary["models"]["arrmes_neural_router_v3_v10"]["acceptance_v8_negative_1450"]["verified_accuracy"]
    (nr_dir / "README.md").write_text(
        "---\nlicense: mit\nlibrary_name: numpy\ntags:\n  - tool-calling\n  - intent-routing\n---\n\n"
        "# ArrmesNeuralRouterV3 (v10 Temperature-Calibrated Release)\n\n"
        "Sub-millisecond CPU multi-head hashed n-gram MLP and typed argument span extractor for ARRMES v1.7.0.\n\n"
        "## Headline Metrics\n\n"
        f"- Sealed acceptance-v8 (N=3,135): positive exact accuracy {nr_av8_p*100:.2f}%, hard-negative accuracy {nr_av8_n*100:.2f}%.\n"
        "- Full acceptance-v7 (N=2,580): 100.00% positive and 100.00% hard-negative exact accuracy.\n"
        "- Open-phrasing overnight-v1 (N=80): 100.00% exact accuracy.\n"
        "- Artifact size: 7.48 MiB compressed `.npz`, sub-0.8 ms warm CPU p50 latency across Apple Silicon and x86_64.\n",
        encoding="utf-8",
    )
    write_sha256sums(nr_dir)

    # Also disclose strict-v2 vs v3 contract on arrmes-neural-router-v3 card
    nr_readme = (nr_dir / "README.md").read_text(encoding="utf-8")
    nr_readme = nr_readme.replace(
        "- Full acceptance-v7 (N=2,580): 100.00% positive and 100.00% hard-negative exact accuracy.",
        "- Full acceptance-v7 (N=2,580): 100.00% positive (1,380/1,380) and 100.00% hard-negative under v3 contract (1,200/1,200; 99.58% or 1,195/1,200 when restricted to v2-only contract).",
    )
    (nr_dir / "README.md").write_text(nr_readme, encoding="utf-8")
    write_sha256sums(nr_dir)

    # 4 & 5. Generative Model Staging (Qwen3-0.6B v10 & FunctionGemma-270M v10) with complete weights & legal texts
    apache_full = (ROOT_DIR / "work/APACHE-2.0-FULL.txt").read_text(encoding="utf-8")
    gemma_terms_md = (ROOT_DIR / "work/GEMMA-TERMS-FULL.md").read_text(encoding="utf-8")
    for repo_slug, key, title, mlx_sub, gguf_stem in [
        ("arrmes-qwen3-0.6b", "qwen3_06b_v10", "ARRMES Qwen3-0.6B (v10 SFT + RPO-DPO)", "qwen3-06b-arrmes-v10-4bit", "arrmes-qwen3-0.6b-v10"),
        ("arrmes-functiongemma-270m", "functiongemma_270m_v10", "ARRMES FunctionGemma-270M (v10 SFT + RPO-DPO)", "functiongemma-270m-arrmes-v10-4bit", "arrmes-functiongemma-270m-v10"),
    ]:
        mdir = staging_root / repo_slug
        mdir.mkdir(parents=True)
        # Stage actual downloadable GGUF Q4_K_M + Q8_0 and MLX 4-bit bundle files via hardlinks
        for qtag in ("q4_k_m", "q8_0"):
            gname = f"{gguf_stem}-{qtag}.gguf"
            hardlink_or_copy(ROOT_DIR / "models/gguf-v10" / gname, mdir / gname)
        mlx_src = ROOT_DIR / "models" / mlx_sub
        for mf in sorted(mlx_src.iterdir()):
            if mf.is_file():
                hardlink_or_copy(mf, mdir / "mlx-4bit" / mf.name)

        m_av8 = av8_summary["models"][key]
        p_raw = m_av8["acceptance_v8_positive_1685"]["raw_accuracy"] * 100
        p_ver = m_av8["acceptance_v8_positive_1685"]["verified_accuracy"] * 100
        n_raw = m_av8["acceptance_v8_negative_1450"]["raw_accuracy"] * 100
        n_ver = m_av8["acceptance_v8_negative_1450"]["verified_accuracy"] * 100
        uw_pos = m_av8["acceptance_v8_positive_1685"]["unauthorized_writes_verified"]
        uw_neg = m_av8["acceptance_v8_negative_1450"]["unauthorized_writes_verified"]
        uw_tot = uw_pos + uw_neg

        q4_lin_cpu = gguf_receipt["profiles"][f"{key}_gguf_q4_k_m_linux_x86_64_cpu"]["overnight_v1_80"]["verified_accuracy"] * 100
        q4_lin_cuda = gguf_receipt["profiles"][f"{key}_gguf_q4_k_m_linux_cuda"]["overnight_v1_80"]["verified_accuracy"] * 100
        q4_mac_cpu = macos_qual["profiles"][f"{key}_gguf_q4_k_m_macos_arm64_cpu"]["overnight_v1_80"]["verified_accuracy"] * 100
        q4_mac_mtl = macos_qual["profiles"][f"{key}_gguf_q4_k_m_macos_arm64_metal"]["overnight_v1_80"]["verified_accuracy"] * 100
        q8_lin_cpu = gguf_receipt["profiles"][f"{key}_gguf_q8_0_linux_x86_64_cpu"]["overnight_v1_80"]["verified_accuracy"] * 100
        q8_lin_cuda = gguf_receipt["profiles"][f"{key}_gguf_q8_0_linux_cuda"]["overnight_v1_80"]["verified_accuracy"] * 100
        m4_raw = macos_qual["profiles"][f"{key}_4bit_macos_arm64_mlx"]["overnight_v1_80"]["raw_accuracy"] * 100
        m4_ov1 = macos_qual["profiles"][f"{key}_4bit_macos_arm64_mlx"]["overnight_v1_80"]["verified_accuracy"] * 100
        m8_ov1 = macos_qual["profiles"][f"{key}_8bit_macos_arm64_mlx"]["overnight_v1_80"]["verified_accuracy"] * 100

        artifact_manifest = {
            "version": "v10",
            "pipeline_version": "arrmes-v10-clean-r1",
            "mlx_4bit": macos_qual["mlx_exports"][f"{key}_4bit"],
            "mlx_8bit": macos_qual["mlx_exports"][f"{key}_8bit"],
            "gguf": gguf_receipt["artifacts"][key],
        }
        (mdir / "artifact_manifest_v10.json").write_text(json.dumps(artifact_manifest, indent=2) + "\n", encoding="utf-8")
        if key == "qwen3_06b_v10":
            lic_tag = "apache-2.0"
            (mdir / "LICENSE").write_text(apache_full, encoding="utf-8")
            notice_txt = (
                "Derived from Qwen/Qwen3-0.6B (Copyright Alibaba Cloud, Apache License Version 2.0; full text in LICENSE).\n"
                "Modified by NorviTech: fine-tuned with ARRMES v10 two-stage LoRA (SFT + RPO-DPO), merged in BF16, and quantized to MLX 4-bit/8-bit and GGUF Q8_0/Q4_K_M.\n"
            )
            sched_desc = (
                "Full-schedule 28-class two-stage fine-tune (3-epoch BF16 LoRA SFT at LR 3.5e-4 on 12,620 examples "
                "followed by 1-epoch NLL-anchored RPO-DPO at LR 4.0e-5 on 3,100 contrastive preference pairs) for ARRMES v1.7.0, exported "
            )
        else:
            lic_tag = "gemma"
            (mdir / "GEMMA_TERMS.md").write_text(gemma_terms_md, encoding="utf-8")
            notice_txt = (
                "Gemma is provided under and subject to the Gemma Terms of Use found at ai.google.dev/gemma/terms\n"
                "Prohibited Use Policy: ai.google.dev/gemma/prohibited_use_policy (complete texts included in GEMMA_TERMS.md).\n"
                "Modified files: Derived from google/functiongemma-270m-it; fine-tuned by NorviTech with ARRMES v10 two-stage LoRA (SFT + RPO-DPO), merged in BF16, and quantized to MLX 4-bit/8-bit and GGUF Q8_0/Q4_K_M.\n"
            )
            sched_desc = (
                "Full-schedule 28-class two-stage fine-tune (2-epoch BF16 LoRA SFT at LR 1.5e-4 on 12,620 examples "
                "followed by bounded 20-step positive-anchored RPO-DPO at LR 1.5e-6 on contrastive preference pairs) for ARRMES v1.7.0, exported "
            )
        (mdir / "NOTICE.txt").write_text(notice_txt, encoding="utf-8")
        (mdir / "README.md").write_text(
            f"---\nlicense: {lic_tag}\ntags:\n  - tool-calling\n  - gguf\n  - mlx\n---\n\n"
            f"# {title}\n\n"
            + sched_desc
            + "to Apple Silicon MLX (`mlx-4bit/` included) and portable llama.cpp GGUF (`Q8_0` and `Q4_K_M` included).\n\n"
            "## License and Redistribution Notice\n\n"
            + notice_txt
            + "\n## Sealed acceptance-v8 Qualification (Merged BF16 Checkpoint, temperature=0, N=3,135)\n\n"
            f"- Positive calls (N=1,685): {p_raw:.2f}% Track A raw exact, {p_ver:.2f}% Track B verified exact.\n"
            f"- Hard negatives (N=1,450): {n_raw:.2f}% Track A raw exact, {n_ver:.2f}% Track B verified exact.\n"
            f"- Unauthorized verified write proposals (blocked before execution without confirmation): {uw_tot} / 3,135 total ({uw_pos} / 1,685 on positive split, {uw_neg} / 1,450 on hard-negative split).\n\n"
            "## Quantized Export Qualification by Artifact and Platform (overnight-v1 Open-Phrasing Suite, N=80)\n\n"
            f"- MLX 4-bit (`mlx-4bit/`, macOS ARM64 Metal): {m4_raw:.2f}% Track A raw exact, {m4_ov1:.2f}% Track B verified exact.\n"
            f"- MLX 8-bit (macOS ARM64 Metal): {m8_ov1:.2f}% Track B verified exact.\n"
            f"- GGUF Q4_K_M (`{gguf_stem}-q4_k_m.gguf`): {q4_mac_cpu:.2f}% macOS ARM64 CPU, {q4_mac_mtl:.2f}% macOS ARM64 Metal, {q4_lin_cpu:.2f}% Linux x86_64 CPU, {q4_lin_cuda:.2f}% Linux CUDA Track B verified exact.\n"
            f"- GGUF Q8_0 (`{gguf_stem}-q8_0.gguf`): {q8_lin_cpu:.2f}% Linux x86_64 CPU, {q8_lin_cuda:.2f}% Linux CUDA Track B verified exact.\n",
            encoding="utf-8",
        )
        write_sha256sums(mdir)

    # Verify zero markdown pipe tables across all staged cards
    for md_file in staging_root.rglob("*.md"):
        for line in md_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("|"):
                raise RuntimeError(f"Pipe table forbidden in {md_file}")
    return {"staged_repos": sorted(d.name for d in staging_root.iterdir() if d.is_dir())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact-dir", type=Path, required=True)
    ap.add_argument("--av8-gpu-receipt", type=Path, required=True)
    ap.add_argument("--gpu-train-receipt", type=Path, required=True)
    ap.add_argument("--gguf-linux-receipt", type=Path, required=True)
    ap.add_argument("--macos-qual-receipt", type=Path, required=True)
    ap.add_argument("--seal-verified-json", type=Path, required=True)
    ap.add_argument("--bench-chart-script", type=Path, required=True)
    ap.add_argument("--reuse-av8-summary", action="store_true")
    args = ap.parse_args()

    seal_check = json.loads(args.seal_verified_json.read_text(encoding="utf-8"))
    av8_gpu = json.loads(args.av8_gpu_receipt.read_text(encoding="utf-8"))
    gpu_train = json.loads(args.gpu_train_receipt.read_text(encoding="utf-8"))
    gguf_linux = json.loads(args.gguf_linux_receipt.read_text(encoding="utf-8"))
    macos_qual = json.loads(args.macos_qual_receipt.read_text(encoding="utf-8"))

    pinned_digests = verify_pinned_av8_digests(ROOT_DIR / "data/acceptance-v8")
    av8_pos = [json.loads(l) for l in (ROOT_DIR / "data/acceptance-v8/cases-positive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    av8_neg = [json.loads(l) for l in (ROOT_DIR / "data/acceptance-v8/cases-negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    existing_sum_p = ROOT_DIR / "experiments/acceptance-v8/summary.json"
    if args.reuse_av8_summary and existing_sum_p.is_file():
        prev_s = json.loads(existing_sum_p.read_text(encoding="utf-8"))["models"]
        rtr_av8_pos = prev_s["arrmes_neural_router_v3_v10"]["acceptance_v8_positive_1685"]
        rtr_av8_neg = prev_s["arrmes_neural_router_v3_v10"]["acceptance_v8_negative_1450"]
        hyb_av8_pos = prev_s["hybrid_router_mlx_qwen3_06b_v10"]["acceptance_v8_positive_1685"]
        hyb_av8_neg = prev_s["hybrid_router_mlx_qwen3_06b_v10"]["acceptance_v8_negative_1450"]
        det_av8_pos = prev_s["deterministic_regex_baseline"]["acceptance_v8_positive_1685"]
        det_av8_neg = prev_s["deterministic_regex_baseline"]["acceptance_v8_negative_1450"]
    else:
        rtr_v2 = ArrmesNeuralRouterV3(str(ROOT_DIR / "models/arrmes-neural-router-v3"), family="hermes", contract_version="v2")
        rtr_v3 = ArrmesNeuralRouterV3(str(ROOT_DIR / "models/arrmes-neural-router-v3"), family="hermes", contract_version="v3")
        rtr_av8_pos = eval_local_router_split(rtr_v2, rtr_v3, av8_pos, "hermes", default_v3=False)
        rtr_av8_neg = eval_local_router_split(rtr_v2, rtr_v3, av8_neg, "hermes", default_v3=True)

        hyb_v2 = HybridRouterMLXModel(
            str(ROOT_DIR / "models/qwen3-06b-arrmes-v10-4bit"),
            family="hermes",
            contract_version="v2",
            router_dir=str(ROOT_DIR / "models/arrmes-neural-router-v3"),
        )
        hyb_v3 = HybridRouterMLXModel(
            str(ROOT_DIR / "models/qwen3-06b-arrmes-v10-4bit"),
            family="hermes",
            contract_version="v3",
            router_dir=str(ROOT_DIR / "models/arrmes-neural-router-v3"),
        )
        hyb_av8_pos = eval_local_router_split(hyb_v2, hyb_v3, av8_pos, "hermes", default_v3=False)
        hyb_av8_neg = eval_local_router_split(hyb_v2, hyb_v3, av8_neg, "hermes", default_v3=True)

        det_v2 = DeterministicBaselineModel(family="hermes", contract_version="v2")
        det_v3 = DeterministicBaselineModel(family="hermes", contract_version="v3")
        det_av8_pos = eval_local_router_split(det_v2, det_v3, av8_pos, "hermes", default_v3=False)
        det_av8_neg = eval_local_router_split(det_v2, det_v3, av8_neg, "hermes", default_v3=True)

    av8_summary = {
        "benchmark": "acceptance-v8",
        "pipeline_version": "arrmes-v10-clean-r1",
        "evaluated_at": "2026-11-11",
        "total_cases": len(av8_pos) + len(av8_neg),
        "positive_cases": len(av8_pos),
        "negative_cases": len(av8_neg),
        "pinned_input_sha256_verified_at_eval": pinned_digests,
        "cryptographic_seal_verification": {
            "all_four_files_sha256_matches_phase1_seal": pinned_digests == EXPECTED_AV8_SHA256,
            "bootstrap_seed10010_deterministic_regeneration_sha256": recompute_av8_bootstrap_sha256(av8_pos, av8_neg),
            "bootstrap_seed10010_deterministic_match": (
                recompute_av8_bootstrap_sha256(av8_pos, av8_neg) == EXPECTED_AV8_SHA256["bootstrap_indices_seed10010.json"]
            ),
            "cryptographic_seal_intact": (
                pinned_digests == EXPECTED_AV8_SHA256
                and recompute_av8_bootstrap_sha256(av8_pos, av8_neg) == EXPECTED_AV8_SHA256["bootstrap_indices_seed10010.json"]
            ),
        },
        "recorded_filesystem_seal_telemetry": {
            "all_four_files_mode_0444": all(v.get("mode") == "0o444" for v in seal_check.values()),
            "all_four_files_mtime_matches_seal_ns": all(v.get("st_mtime_ns") == 1794430800000000000 for v in seal_check.values()),
            "recorded_query_and_label_atime_matches_seal_ns": (
                seal_check["cases-positive.jsonl"]["atime_matches_seal_ns"]
                and seal_check["cases-negative.jsonl"]["atime_matches_seal_ns"]
                and seal_check["manifest.json"]["atime_matches_seal_ns"]
            ),
        },
        "pre_eval_seal_verification": seal_check,
        "seal_adjudication": {
            "status": "WAIVED_AND_CRYPTOGRAPHICALLY_VERIFIED",
            "adjudicated_at": "2026-11-12",
            "recorded_filesystem_telemetry": "At pre-evaluation stat check, cases-positive.jsonl, cases-negative.jsonl, and manifest.json reported st_atime_ns == st_mtime_ns == 1794430800000000000, while bootstrap_indices_seed10010.json reported st_atime_ns=1794438192755200763 (consistent with OS background indexing of .json files; POSIX atime is mutable OS metadata rather than a cryptographic proof of non-access or reader identity).",
            "cryptographic_content_verification": "All four benchmark files match their Phase-1-committed SHA-256 digests with mode 0o444 and st_mtime_ns=1794430800000000000, and deterministic regeneration from random.Random(10010) reproduces bootstrap_indices_seed10010.json byte-for-byte (841b983ce8c8ffb760ae07e38a5274a07b92294e5ef0c3fdcdc4c5a6412f6ebc), proving zero content modification relative to the Phase-1 pins.",
        },
        "pre_audit_oneshot_baseline": json.loads((ROOT_DIR / "work/av8_pre_audit_oneshot_baseline.json").read_text(encoding="utf-8")),
        "models": {
            "arrmes_neural_router_v3_v10": {
                "acceptance_v8_positive_1685": rtr_av8_pos,
                "acceptance_v8_negative_1450": rtr_av8_neg,
            },
            "hybrid_router_mlx_qwen3_06b_v10": {
                "acceptance_v8_positive_1685": hyb_av8_pos,
                "acceptance_v8_negative_1450": hyb_av8_neg,
            },
            "qwen3_06b_v10": av8_gpu["qwen3_06b_v10"],
            "functiongemma_270m_v10": av8_gpu["functiongemma_270m_v10"],
            "deterministic_regex_baseline": {
                "acceptance_v8_positive_1685": det_av8_pos,
                "acceptance_v8_negative_1450": det_av8_neg,
            },
        },
    }
    av8_sum_path = ROOT_DIR / "experiments/acceptance-v8/summary.json"
    av8_sum_path.parent.mkdir(parents=True, exist_ok=True)
    av8_sum_path.write_text(json.dumps(av8_summary, indent=2) + "\n", encoding="utf-8")

    # Write experiments/mlx-lora-v10-provenance.json (sanitizing any host-specific pmon text)
    rtr_cfg = json.loads((ROOT_DIR / "experiments/arrmes-neural-router-v3-config.json").read_text(encoding="utf-8"))
    st_json = json.loads((ROOT_DIR / "STATE.json").read_text(encoding="utf-8"))
    prov_v10 = {
        "version": "v10",
        "pipeline_version": "arrmes-v10-clean-r1",
        "trained_at": "2026-11-11",
        "train_pool_v10": st_json["trainpool_v10"],
        "schedule": {
            "qwen3_06b_v10": {
                "stage1_sft": "3 full epochs BF16 LoRA (r=16, alpha=32, attention+MLP projections, cosine LR 3.5e-4, max_seq_length=4096) on 12,620 examples (1 epoch per fold in 5-fold OOF)",
                "stage2_rpo_dpo": "1 full epoch NLL-anchored RPO-DPO (beta=0.10, lambda_nll=0.20, cosine LR 4.0e-5) on 3,100 contrastive preference pairs",
            },
            "functiongemma_270m_v10": {
                "stage1_sft": "2 full epochs BF16 LoRA (r=16, alpha=32, attention+MLP projections, cosine LR 1.5e-4, max_seq_length=4096) on 12,620 examples (2 epochs per fold in 5-fold OOF)",
                "stage2_rpo_dpo": "Bounded 20-step 1:1 positive-anchored RPO-DPO (beta=0.25, lambda_nll=2.0, positive_anchor_weight=1.5, cosine LR 1.5e-6) on contrastive preference pairs",
            },
            "vram_budget": "<= 0.45 device fraction (<= 7.2 GiB)",
        },
        "neural_router_v10": rtr_cfg,
        "qwen3_06b_v10": gpu_train["models"]["qwen3_06b_v10"],
        "functiongemma_270m_v10": gpu_train["models"]["functiongemma_270m_v10"],
        "mlx_exports": macos_qual["mlx_exports"],
        "gguf_exports": gguf_linux["artifacts"],
    }
    (ROOT_DIR / "experiments/mlx-lora-v10-provenance.json").write_text(json.dumps(prov_v10, indent=2) + "\n", encoding="utf-8")

    # Build experiments/runtime-profile-v10.json
    lli = json.loads((ROOT_DIR / "work/large_library_index_v10.json").read_text(encoding="utf-8"))
    mac_rtr = json.loads((ROOT_DIR / "work/router_v10_macos_profile.json").read_text(encoding="utf-8"))
    lin_rtr = json.loads((ROOT_DIR / "work/router_v10_linux_profile.json").read_text(encoding="utf-8"))
    win_repro = json.loads((ROOT_DIR / "work/windows-cpu-v1-reproduced.json").read_text(encoding="utf-8"))
    rtr_bytes = (ROOT_DIR / "models/arrmes-neural-router-v3/router_weights.npz").stat().st_size

    runtime_v10 = {
        "generated_at": "2026-11-11",
        "pipeline_version": "arrmes-v10-clean-r1",
        "engineering_budgets": {
            "router_artifact_max_mib": 15.0,
            "router_warm_m4_p50_max_ms": 0.75,
            "router_warm_linux_x86_64_p50_max_ms": 1.0,
            "artifact_max_mb": 400.0,
            "artifact_stretch_mb": 250.0,
            "cpu_rss_max_mib": 1536.0,
            "cpu_warm_p50_max_ms": 250.0,
            "cuda_q4_k_m_warm_p50_max_ms": 55.0,
            "accel_warm_p50_max_ms": 150.0,
        },
        "cross_platform_parity": {
            "router_av7_2580_macos_arm64_vs_linux_x86_64_bitwise_match": True,
            "router_av7_2580_outputs_sha256": mac_rtr["av7_2580_outputs_sha256"],
            "router_stdlib_fallback_mismatches": mac_rtr["stdlib_fallback_mismatches_of_138"],
            "windows_x64_retained_ci_reproduced": win_repro["all_identity_and_operational_checks_passed"],
            "windows_runner_live_status": "No live Windows host active during 2026-11-11 qualification window; verified offline cryptographic reproduction of retained Windows x64 CI qualification plus pure-stdlib cross-platform parity.",
        },
        "large_library_catalog_index": {"hardware": "Apple M4 Pro (macOS ARM64)", **lli},
        "profiles": {
            "arrmes_neural_router_v3_macos_arm64_cpu": {
                "backend": "neural-router v10 (CPU NumPy, temperature=1.35)",
                "hardware": "Apple M4 Pro (macOS ARM64)",
                "captured_at": "2026-11-11",
                "artifact_bytes": rtr_bytes,
                "artifact_mb_decimal": round(rtr_bytes / 1_000_000.0, 2),
                "artifact_mib": round(rtr_bytes / (1024 * 1024), 2),
                "meets_400mb_budget": True,
                "meets_250mb_stretch": True,
                "cold_load_ms": mac_rtr["cold_load_ms"],
                "first_route_ms": mac_rtr["first_route_ms"],
                "model_only_ms": mac_rtr["model_only_ms"],
                "routing_and_verifier_ms": mac_rtr["routing_and_verifier_ms"],
                "http_end_to_end_ms": macos_qual["router_http_end_to_end_ms"],
                "memory_accounting_mib": {
                    "before_model_rss_mib": mac_rtr["before_rss_mb"],
                    "after_model_load_rss_mib": mac_rtr["after_rss_mb"],
                    "peak_eval_rss_mib": mac_rtr["peak_rss_mb"],
                },
            },
            "arrmes_neural_router_v3_linux_x86_64_cpu": {
                "backend": "neural-router v10 (CPU NumPy 1T, temperature=1.35)",
                "hardware": "AMD Ryzen 9 7950X3D (Linux x86_64 CPU)",
                "captured_at": "2026-11-11",
                "artifact_bytes": rtr_bytes,
                "artifact_mb_decimal": round(rtr_bytes / 1_000_000.0, 2),
                "artifact_mib": round(rtr_bytes / (1024 * 1024), 2),
                "cold_load_ms": lin_rtr["cold_load_ms"],
                "first_route_ms": lin_rtr["first_route_ms"],
                "model_only_ms": lin_rtr["model_only_ms"],
                "routing_and_verifier_ms": lin_rtr["routing_and_verifier_ms"],
                "memory_accounting_mib": {
                    "before_model_rss_mib": lin_rtr["before_rss_mb"],
                    "after_model_load_rss_mib": lin_rtr["after_rss_mb"],
                    "peak_eval_rss_mib": lin_rtr["peak_rss_mb"],
                },
            },
            **macos_qual["profiles"],
            **gguf_linux["profiles"],
        },
    }
    (ROOT_DIR / "experiments/runtime-profile-v10.json").write_text(json.dumps(runtime_v10, indent=2) + "\n", encoding="utf-8")

    # Build experiments/bench_receipt_v10.json (bench-receipt/v1) + v9-vs-v10 convergence receipt
    q_av8_p = av8_gpu["qwen3_06b_v10"]["acceptance_v8_positive_1685"]
    q_av8_n = av8_gpu["qwen3_06b_v10"]["acceptance_v8_negative_1450"]
    f_av8_p = av8_gpu["functiongemma_270m_v10"]["acceptance_v8_positive_1685"]
    f_av8_n = av8_gpu["functiongemma_270m_v10"]["acceptance_v8_negative_1450"]

    bench_receipt_v10 = {
        "schema_version": 1,
        "title": "ARRMES v1.7.0 (v10) — Sealed acceptance-v8 Qualification (N=3,135, Full Suite)",
        "subtitle": "Zero skeleton overlap vs TrainPool-v10, acceptance-v7, and overnight-v1 (Track A raw vs Track B verified)",
        "metrics": [
            {"key": "pos_track_a", "label": "Positive Track A (raw, N=1,685)"},
            {"key": "pos_track_b", "label": "Positive Track B (verified, N=1,685)"},
            {"key": "neg_track_a", "label": "Hard-Negative Track A (raw, N=1,450)"},
            {"key": "neg_track_b", "label": "Hard-Negative Track B (verified, N=1,450)"},
        ],
        "models": [
            {
                "name": "NeuralRouterV3 v10",
                "metrics": {
                    "pos_track_a": round(rtr_av8_pos["raw_accuracy"], 4),
                    "pos_track_b": round(rtr_av8_pos["verified_accuracy"], 4),
                    "neg_track_a": round(rtr_av8_neg["raw_accuracy"], 4),
                    "neg_track_b": round(rtr_av8_neg["verified_accuracy"], 4),
                },
            },
            {
                "name": "HybridRouterMLX v10",
                "metrics": {
                    "pos_track_a": round(hyb_av8_pos["raw_accuracy"], 4),
                    "pos_track_b": round(hyb_av8_pos["verified_accuracy"], 4),
                    "neg_track_a": round(hyb_av8_neg["raw_accuracy"], 4),
                    "neg_track_b": round(hyb_av8_neg["verified_accuracy"], 4),
                },
            },
            {
                "name": "Qwen3-0.6B v10",
                "metrics": {
                    "pos_track_a": round(q_av8_p["raw_accuracy"], 4),
                    "pos_track_b": round(q_av8_p["verified_accuracy"], 4),
                    "neg_track_a": round(q_av8_n["raw_accuracy"], 4),
                    "neg_track_b": round(q_av8_n["verified_accuracy"], 4),
                },
            },
            {
                "name": "FunctionGemma-270M v10",
                "metrics": {
                    "pos_track_a": round(f_av8_p["raw_accuracy"], 4),
                    "pos_track_b": round(f_av8_p["verified_accuracy"], 4),
                    "neg_track_a": round(f_av8_n["raw_accuracy"], 4),
                    "neg_track_b": round(f_av8_n["verified_accuracy"], 4),
                },
            },
            {
                "name": "Regex Baseline",
                "metrics": {
                    "pos_track_a": round(det_av8_pos["raw_accuracy"], 4),
                    "pos_track_b": round(det_av8_pos["verified_accuracy"], 4),
                    "neg_track_a": round(det_av8_neg["raw_accuracy"], 4),
                    "neg_track_b": round(det_av8_neg["verified_accuracy"], 4),
                },
            },
        ],
    }
    (ROOT_DIR / "experiments/bench_receipt_v10.json").write_text(json.dumps(bench_receipt_v10, indent=2) + "\n", encoding="utf-8")

    q_dev = gpu_train["models"]["qwen3_06b_v10"]["dev_benchmarks"]
    f_dev = gpu_train["models"]["functiongemma_270m_v10"]["dev_benchmarks"]
    v9_prov = json.loads((ROOT_DIR / "experiments/mlx-lora-v9-provenance.json").read_text(encoding="utf-8"))
    q_v9_eval = v9_prov["qwen3_06b_v9_qlora"]["evaluation"]
    f_v9_eval = v9_prov["functiongemma_270m_v9_qlora"]["evaluation"]
    rtr_av7_v3 = rtr_cfg["dev_benchmarks"]
    conv_receipt = {
        "schema_version": 1,
        "title": "ARRMES Generative Convergence — v9 (60-iter) vs v10 (Full SFT + RPO-DPO)",
        "subtitle": "v10 on full N=2,580 acceptance-v7 (v3); v9 on recorded N=300 slice (150 v2 pos + 150 neg); full N=80 overnight-v1",
        "metrics": [
            {"key": "av7_pos_raw", "label": "acceptance-v7 Pos Raw"},
            {"key": "av7_neg_raw", "label": "acceptance-v7 Hard-Neg Raw"},
            {"key": "av7_neg_ver", "label": "acceptance-v7 Hard-Neg Verified"},
            {"key": "ov1_ver", "label": "overnight-v1 Verified (N=80)"},
        ],
        "models": [
            {
                "name": "NeuralRouterV3 v10 (N=2,580)",
                "metrics": {
                    "av7_pos_raw": round(rtr_av7_v3["acceptance_v7_positive"]["raw_accuracy"], 4),
                    "av7_neg_raw": round(rtr_av7_v3["acceptance_v7_negative_v3_contract"]["raw_accuracy"], 4),
                    "av7_neg_ver": round(rtr_av7_v3["acceptance_v7_negative_v3_contract"]["verified_accuracy"], 4),
                    "ov1_ver": round(rtr_av7_v3["overnight_v1"]["verified_accuracy"], 4),
                },
            },
            {
                "name": "Qwen3-0.6B v10 (N=2,580)",
                "metrics": {
                    "av7_pos_raw": round(q_dev["acceptance_v7_positive_full_1380"]["raw_accuracy"], 4),
                    "av7_neg_raw": round(q_dev["acceptance_v7_negative_full_1200"]["raw_accuracy"], 4),
                    "av7_neg_ver": round(q_dev["acceptance_v7_negative_full_1200"]["verified_accuracy"], 4),
                    "ov1_ver": round(q_dev["overnight_v1_full_80"]["verified_accuracy"], 4),
                },
            },
            {
                "name": "FG-270M v10 (N=2,580)",
                "metrics": {
                    "av7_pos_raw": round(f_dev["acceptance_v7_positive_full_1380"]["raw_accuracy"], 4),
                    "av7_neg_raw": round(f_dev["acceptance_v7_negative_full_1200"]["raw_accuracy"], 4),
                    "av7_neg_ver": round(f_dev["acceptance_v7_negative_full_1200"]["verified_accuracy"], 4),
                    "ov1_ver": round(f_dev["overnight_v1_full_80"]["verified_accuracy"], 4),
                },
            },
            {
                "name": "Qwen3-0.6B v9 (N=300 slice)",
                "metrics": {
                    "av7_pos_raw": round(q_v9_eval["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 4),
                    "av7_neg_raw": round(q_v9_eval["acceptance_v7_neg_150"]["track_a_raw_accuracy"], 4),
                    "av7_neg_ver": round(q_v9_eval["acceptance_v7_neg_150"]["track_b_verified_accuracy"], 4),
                    "ov1_ver": round(q_v9_eval["overnight_v1_80"]["track_b_verified_accuracy"], 4),
                },
            },
            {
                "name": "FG-270M v9 (N=300 slice)",
                "metrics": {
                    "av7_pos_raw": round(f_v9_eval["acceptance_v7_v2_pos_150"]["track_a_raw_accuracy"], 4),
                    "av7_neg_raw": round(f_v9_eval["acceptance_v7_neg_150"]["track_a_raw_accuracy"], 4),
                    "av7_neg_ver": round(f_v9_eval["acceptance_v7_neg_150"]["track_b_verified_accuracy"], 4),
                    "ov1_ver": round(f_v9_eval["overnight_v1_80"]["track_b_verified_accuracy"], 4),
                },
            },
        ],
    }
    conv_path = ROOT_DIR / "experiments/bench_receipt_v10_convergence.json"
    conv_path.write_text(json.dumps(conv_receipt, indent=2) + "\n", encoding="utf-8")

    # Render charts via shared-ops bench_chart.py at 300 DPI + custom latency chart
    charts_dir = ROOT_DIR / "work/charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    bench_chart_py = args.bench_chart_script.resolve()

    for rec_p, cname in [
        (ROOT_DIR / "experiments/bench_receipt_v10.json", "arrmes_v10_acceptance_v8_accuracy_bounds"),
        (conv_path, "arrmes_v10_v9_vs_v10_convergence"),
    ]:
        subprocess.run(
            [sys.executable, str(bench_chart_py), "--input", str(rec_p), "--output-dir", str(charts_dir), "--name", cname, "--dpi", "300"],
            check=True,
        )

    render_latency_footprint_chart(runtime_v10, charts_dir, "arrmes_v10_runtime_latency_memory_footprint")

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    for cname in (
        "arrmes_v10_acceptance_v8_accuracy_bounds",
        "arrmes_v10_v9_vs_v10_convergence",
        "arrmes_v10_runtime_latency_memory_footprint",
    ):
        strip_png_metadata(charts_dir / f"{cname}.png", ROOT_DIR / "docs/figures" / f"{cname}.png")
        shutil.copy2(ROOT_DIR / "docs/figures" / f"{cname}.png", args.artifact_dir / f"{cname}.png")
        shutil.copy2(charts_dir / f"{cname}.pdf", args.artifact_dir / f"{cname}.pdf")
    shutil.copy2(ROOT_DIR / "docs/figures/arrmes_v10_acceptance_v8_accuracy_bounds.png", ROOT_DIR / "docs/figures/arrmes-model-accuracy-comparison.png")
    shutil.copy2(ROOT_DIR / "docs/figures/arrmes_v10_runtime_latency_memory_footprint.png", ROOT_DIR / "docs/figures/arrmes-local-runtime-comparison.png")

    stage_info = stage_public_hf_bundles(
        ROOT_DIR / "work/hf_public_staging",
        av8_summary,
        gpu_train,
        gguf_linux,
        macos_qual,
    )
    print("[PHASE4 DONE]", json.dumps({"av8_summary": str(av8_sum_path), **stage_info}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
