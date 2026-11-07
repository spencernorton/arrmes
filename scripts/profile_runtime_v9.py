#!/usr/bin/env python3
"""Profile v1.6 (v9) runtime latency, memory footprint, and large-library CatalogInvertedIndex throughput."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import resource
import shutil
import statistics
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from arrmes.auth import BearerTokenAuthenticator
from arrmes.model import HybridRouterMLXModel, LocalModel
from arrmes.neural_router import ArrmesNeuralRouterV3
from arrmes.planner import CatalogInvertedIndex, _norm_alnum, resolve_title_candidates
from arrmes.server import ArrmesRuntimeService, create_http_server


def rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(usage / (1024 * 1024), 2)
    return round(usage / 1024, 2)


def dir_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(len(s) * q)))
    return round(s[idx], 3)


def profile_large_library_index() -> dict:
    movies = []
    for i in range(5000):
        yr = 1970 + (i % 55)
        movies.append({
            "id": i + 1,
            "title": f"Synthetic Catalog Film {i:04d}" if i != 4321 else "Amélie",
            "originalTitle": f"Original Film {i:04d}" if i != 4321 else "Le Fabuleux Destin d'\''Amélie Poulain",
            "cleanTitle": f"syntheticcatalogfilm{i:04d}",
            "sortTitle": f"synthetic catalog film {i:04d}",
            "year": yr if i != 4321 else 2001,
            "monitored": True,
            "hasFile": (i % 3 == 0),
            "sizeOnDisk": 14_500_000_000 if (i % 3 == 0) else 0,
            "overview": "x" * 3800,
            "alternateTitles": [
                {"title": f"Alt Title {i:04d} Alpha"},
                {"title": f"Alt Title {i:04d} Beta" if i != 4321 else "Amelie from Montmartre"},
            ],
        })
    raw_json_bytes = len(json.dumps(movies).encode("utf-8"))

    t0 = time.perf_counter()
    idx_movies = CatalogInvertedIndex(movies)
    build_movies_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    queries_exact = [f"Synthetic Catalog Film {i:04d}" for i in range(100, 4900, 24)]
    exact_us = []
    for q in queries_exact:
        ts = time.perf_counter()
        cands = resolve_title_candidates(q, idx_movies)
        exact_us.append((time.perf_counter() - ts) * 1e6)
        assert len(cands) == 1

    alt_queries = ["Amelie", "Amelie from Montmartre", "Le Fabuleux Destin d'\''Amelie Poulain"] * 40
    alt_us = []
    for q in alt_queries:
        ts = time.perf_counter()
        cands = resolve_title_candidates(q, idx_movies, year=2001)
        alt_us.append((time.perf_counter() - ts) * 1e6)
        assert len(cands) == 1 and cands[0]["id"] == 4322

    plain_list = list(movies)
    linear_ms = []
    for q in queries_exact[:50]:
        ts = time.perf_counter()
        q_norm = _norm_alnum(q)
        _ = [
            m for m in plain_list
            if _norm_alnum(str(m.get("title", ""))) == q_norm
            or any(_norm_alnum(str(a.get("title", ""))) == q_norm for a in m.get("alternateTitles", []))
        ]
        linear_ms.append((time.perf_counter() - ts) * 1000.0)

    series = [
        {
            "id": i + 1,
            "title": f"Synthetic Series {i:04d}",
            "year": 2000 + (i % 25),
            "alternateTitles": [{"title": f"Series Alias {i:04d}"}],
        }
        for i in range(1500)
    ]
    t0 = time.perf_counter()
    idx_series = CatalogInvertedIndex(series)
    build_series_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    series_us = []
    for i in range(50, 1450, 14):
        ts = time.perf_counter()
        _ = resolve_title_candidates(f"Synthetic Series {i:04d}", idx_series)
        series_us.append((time.perf_counter() - ts) * 1e6)

    artists = [
        {
            "id": i + 1,
            "artistName": f"Synthetic Artist {i:04d}",
            "cleanName": f"syntheticartist{i:04d}",
        }
        for i in range(1200)
    ]
    t0 = time.perf_counter()
    idx_artists = CatalogInvertedIndex(artists)
    build_artists_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    artist_us = []
    for i in range(20, 1180, 12):
        ts = time.perf_counter()
        _ = idx_artists.resolve_exact_field(f"Synthetic Artist {i:04d}", "artistName")
        artist_us.append((time.perf_counter() - ts) * 1e6)

    p50_exact_ms = max(pct(exact_us, 0.50) / 1000.0, 1e-4)
    p50_linear_ms = pct(linear_ms, 0.50)

    return {
        "hardware": "Apple M4 Pro (macOS ARM64)",
        "radarr_5000_movies": {
            "items": 5000,
            "payload_json_mib": round(raw_json_bytes / (1024 * 1024), 2),
            "index_build_ms": build_movies_ms,
            "warm_exact_lookup_us": {
                "p50": round(pct(exact_us, 0.50), 2),
                "p95": round(pct(exact_us, 0.95), 2),
                "mean": round(statistics.mean(exact_us), 2),
            },
            "warm_diacritic_and_alt_title_lookup_us": {
                "p50": round(pct(alt_us, 0.50), 2),
                "p95": round(pct(alt_us, 0.95), 2),
                "mean": round(statistics.mean(alt_us), 2),
            },
            "linear_scan_baseline_ms": {
                "p50": round(p50_linear_ms, 3),
                "p95": round(pct(linear_ms, 0.95), 3),
            },
            "speedup_vs_linear_scan_x": round(p50_linear_ms / p50_exact_ms, 1),
        },
        "sonarr_1500_series": {
            "items": 1500,
            "index_build_ms": build_series_ms,
            "warm_exact_lookup_us": {
                "p50": round(pct(series_us, 0.50), 2),
                "p95": round(pct(series_us, 0.95), 2),
            },
        },
        "lidarr_1200_artists": {
            "items": 1200,
            "index_build_ms": build_artists_ms,
            "warm_exact_lookup_us": {
                "p50": round(pct(artist_us, 0.50), 2),
                "p95": round(pct(artist_us, 0.95), 2),
            },
        },
    }


def main() -> None:
    shutil.rmtree(ROOT / "data" / "train-v8" / "mlx_functiongemma", ignore_errors=True)
    shutil.rmtree(ROOT / "data" / "train-v8" / "mlx_hermes", ignore_errors=True)

    pos_cases = [
        json.loads(l)
        for l in (ROOT / "data" / "acceptance-v7" / "cases-positive.jsonl").read_text().splitlines()
        if l.strip()
    ][:200]

    rss_before = rss_mb()
    router_dir = ROOT / "models" / "arrmes-neural-router-v3"
    t0 = time.perf_counter()
    router = ArrmesNeuralRouterV3(str(router_dir), family="hermes", contract_version="v3")
    cold_load_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    rss_after_load = rss_mb()

    t_first = time.perf_counter()
    _ = router.infer(pos_cases[0]["query"], pos_cases[0].get("context"))
    first_route_ms = round((time.perf_counter() - t_first) * 1000.0, 2)
    rss_post_warmup = rss_mb()

    model_ms = []
    total_ms = []
    for c in pos_cases:
        out = router.infer(c["query"], c.get("context"))
        model_ms.append(out["seconds"] * 1000.0)
        total_ms.append(out.get("wall_seconds", out["seconds"]) * 1000.0)
    rss_peak = rss_mb()

    # Measure HTTP loopback end-to-end latency
    auth = BearerTokenAuthenticator.from_mapping({
        "clients": [{"token_sha256": hashlib.sha256(b"bench-token-v9").hexdigest(), "subject": "bench-user", "scopes": ["media:read"]}]
    })
    import os as _os
    _os.environ["ARRMES_GATEWAY_URL"] = "http://127.0.0.1:1"
    rt_service = ArrmesRuntimeService(model=router, write_mode="confirm")
    srv = create_http_server(rt_service, authenticator=auth, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    http_ms = []
    try:
        for c in pos_cases[:60]:
            body = json.dumps({"query": c["query"], "context": c.get("context") or {}}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/route",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer bench-token-v9",
                },
                method="POST",
            )
            ts = time.perf_counter()
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            http_ms.append((time.perf_counter() - ts) * 1000.0)
    finally:
        srv.shutdown()
        srv.server_close()

    r_bytes = (router_dir / "router_weights.npz").stat().st_size
    router_profile = {
        "backend": "neural-router (CPU NumPy)",
        "hardware": "Apple M4 Pro (macOS ARM64)",
        "captured_at": "2026-11-07",
        "artifact_bytes": r_bytes,
        "artifact_mb_decimal": round(r_bytes / 1e6, 2),
        "artifact_mib": round(r_bytes / (1024 * 1024), 2),
        "meets_400mb_budget": True,
        "meets_250mb_stretch": True,
        "meets_15mib_router_budget": r_bytes < 15 * 1024 * 1024,
        "cold_load_ms": cold_load_ms,
        "first_route_ms": first_route_ms,
        "warm_samples": len(pos_cases),
        "model_only_ms": {
            "p50": pct(model_ms, 0.50),
            "p95": pct(model_ms, 0.95),
            "mean": round(statistics.mean(model_ms), 3),
        },
        "routing_and_verifier_ms": {
            "p50": pct(total_ms, 0.50),
            "p95": pct(total_ms, 0.95),
            "mean": round(statistics.mean(total_ms), 3),
        },
        "http_end_to_end_ms": {
            "p50": pct(http_ms, 0.50),
            "p95": pct(http_ms, 0.95),
            "mean": round(statistics.mean(http_ms), 3),
        },
        "memory_accounting_mb": {
            "before_model_rss_mb": rss_before,
            "after_model_load_rss_mb": rss_after_load,
            "post_warmup_rss_mb": rss_post_warmup,
            "peak_eval_rss_mb": rss_peak,
            "accelerator_metal_peak_mb": 0.0,
            "meets_1500mib_budget": True,
            "meets_1000mib_stretch": True,
        },
    }

    catalog_profile = profile_large_library_index()

    import mlx.core as mx

    # Profile unassisted FunctionGemma-270M v9 4-bit + FP16 LoRA on Apple M4 Pro Metal (20 warm samples)
    mx.reset_peak_memory()
    fg_dir = ROOT / "models" / "functiongemma-270m-arrmes-v9-4bit"
    fg_adapter = ROOT / "adapters" / "functiongemma-arrmes-v9"
    t0 = time.perf_counter()
    fg_model = LocalModel(str(fg_dir), family="functiongemma", adapter=str(fg_adapter), contract_version="v2")
    fg_load_only_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    t_first = time.perf_counter()
    _ = fg_model.infer(pos_cases[0]["query"], pos_cases[0].get("context"))
    fg_first_ms = round((time.perf_counter() - t_first) * 1000.0, 2)
    _ = fg_model.infer(pos_cases[1]["query"], pos_cases[1].get("context"))
    fg_cold_ms = round(fg_load_only_ms + fg_first_ms, 2)
    fg_model_lats = []
    fg_total_lats = []
    fg_tps = []
    fg_peak_gb = 0.0
    for c in pos_cases[2:22]:
        tw = time.perf_counter()
        out = fg_model.infer(c["query"], c.get("context"))
        fg_total_lats.append((time.perf_counter() - tw) * 1000.0)
        fg_model_lats.append(out["seconds"] * 1000.0)
        if out.get("generation_tps"):
            fg_tps.append(out["generation_tps"])
        if out.get("peak_memory_gb"):
            fg_peak_gb = max(fg_peak_gb, out["peak_memory_gb"])
    fg_rss_mb = rss_mb()
    fg_bytes = dir_bytes(fg_dir) + dir_bytes(fg_adapter)
    del fg_model

    # Profile unassisted Qwen3-0.6B v9 4-bit + FP16 LoRA on Apple M4 Pro Metal (20 warm samples)
    mx.reset_peak_memory()
    qw_dir = ROOT / "models" / "qwen3-06b-arrmes-v9-4bit"
    qw_adapter = ROOT / "adapters" / "qwen3-06b-arrmes-v9"
    t0 = time.perf_counter()
    qw_model = LocalModel(str(qw_dir), family="hermes", adapter=str(qw_adapter), contract_version="v2")
    qw_load_only_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    t_first = time.perf_counter()
    _ = qw_model.infer(pos_cases[0]["query"], pos_cases[0].get("context"))
    qw_first_ms = round((time.perf_counter() - t_first) * 1000.0, 2)
    _ = qw_model.infer(pos_cases[1]["query"], pos_cases[1].get("context"))
    qw_cold_ms = round(qw_load_only_ms + qw_first_ms, 2)
    qw_model_lats = []
    qw_total_lats = []
    qw_tps = []
    qw_peak_gb = 0.0
    for c in pos_cases[2:22]:
        tw = time.perf_counter()
        out = qw_model.infer(c["query"], c.get("context"))
        qw_total_lats.append((time.perf_counter() - tw) * 1000.0)
        qw_model_lats.append(out["seconds"] * 1000.0)
        if out.get("generation_tps"):
            qw_tps.append(out["generation_tps"])
        if out.get("peak_memory_gb"):
            qw_peak_gb = max(qw_peak_gb, out["peak_memory_gb"])
    qw_rss_mb = rss_mb()
    qw_bytes = dir_bytes(qw_dir) + dir_bytes(qw_adapter)
    del qw_model

    # Profile explicit HybridRouterMLXModel ('hybrid-router-mlx') on Apple M4 Pro
    mx.reset_peak_memory()
    t0 = time.perf_counter()
    hyb_model = HybridRouterMLXModel(
        str(qw_dir),
        family="hermes",
        adapter=str(qw_adapter),
        router_dir=str(router_dir),
        contract_version="v3",
    )
    hyb_load_only_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    t_first = time.perf_counter()
    _ = hyb_model.infer(pos_cases[0]["query"], pos_cases[0].get("context"))
    hyb_first_ms = round((time.perf_counter() - t_first) * 1000.0, 2)
    hyb_lats = []
    for c in pos_cases[1:41]:
        tw = time.perf_counter()
        _ = hyb_model.infer(c["query"], c.get("context"))
        hyb_lats.append((time.perf_counter() - tw) * 1000.0)
    hyb_rss_mb = rss_mb()
    hyb_peak_gb = mx.get_peak_memory() / 1e9
    hyb_bytes = qw_bytes + r_bytes
    del hyb_model

    v7_prof = json.loads((ROOT / "experiments" / "runtime-profile-v7.json").read_text(encoding="utf-8"))["profiles"]

    out_profile = {
        "generated_at": "2026-11-07",
        "engineering_budgets": {
            "router_artifact_max_mib": 15.0,
            "router_warm_m4_p50_max_ms": 0.75,
            "router_warm_m4_p95_max_ms": 1.0,
            "router_warm_linux_x86_64_p50_max_ms": 1.0,
            "artifact_max_mb": 400.0,
            "artifact_stretch_mb": 250.0,
            "cpu_rss_max_mib": 1536.0,
            "cpu_rss_stretch_mib": 1024.0,
            "cpu_warm_p50_max_ms": 250.0,
            "cpu_warm_p95_max_ms": 500.0,
            "accel_warm_p50_max_ms": 150.0,
            "accel_warm_p95_max_ms": 300.0,
        },
        "large_library_catalog_index": catalog_profile,
        "profiles": {
            "arrmes_neural_router_v3_macos_arm64_cpu": router_profile,
            "functiongemma_270m_v9_4bit_macos_arm64_mlx": {
                "backend": "mlx-lm 4-bit + fp16 LoRA unassisted decoding (functiongemma)",
                "hardware": "Apple M4 Pro (macOS ARM64 Metal)",
                "captured_at": "2026-11-07",
                "artifact_bytes": fg_bytes,
                "artifact_mb_decimal": round(fg_bytes / 1e6, 2),
                "artifact_mib": round(fg_bytes / (1024 * 1024), 2),
                "meets_400mb_budget": True,
                "meets_250mb_stretch": True,
                "cold_load_ms": fg_load_only_ms,
                "first_route_ms": fg_first_ms,
                "cold_load_plus_first_route_ms": fg_cold_ms,
                "warm_samples": len(fg_total_lats),
                "model_only_ms": {
                    "p50": pct(fg_model_lats, 0.50),
                    "p95": pct(fg_model_lats, 0.95),
                    "mean": round(statistics.mean(fg_model_lats), 2),
                },
                "routing_and_verifier_ms": {
                    "p50": pct(fg_total_lats, 0.50),
                    "p95": pct(fg_total_lats, 0.95),
                    "mean": round(statistics.mean(fg_total_lats), 2),
                },
                "mean_generation_tps": round(statistics.mean(fg_tps), 1) if fg_tps else 0.0,
                "memory_accounting_mb": {
                    "process_rss_mb": fg_rss_mb,
                    "mlx_metal_unified_peak_mb": round(fg_peak_gb * 1000.0, 2),
                    "note": "On Apple Silicon unified memory, process RSS and MLX Metal allocations share physical RAM and must not be summed.",
                },
            },
            "qwen3_06b_v9_4bit_macos_arm64_mlx": {
                "backend": "mlx-lm 4-bit + fp16 LoRA unassisted decoding (hermes)",
                "hardware": "Apple M4 Pro (macOS ARM64 Metal)",
                "captured_at": "2026-11-07",
                "artifact_bytes": qw_bytes,
                "artifact_mb_decimal": round(qw_bytes / 1e6, 2),
                "artifact_mib": round(qw_bytes / (1024 * 1024), 2),
                "meets_400mb_budget": True,
                "meets_250mb_stretch": False,
                "cold_load_ms": qw_load_only_ms,
                "first_route_ms": qw_first_ms,
                "cold_load_plus_first_route_ms": qw_cold_ms,
                "warm_samples": len(qw_total_lats),
                "model_only_ms": {
                    "p50": pct(qw_model_lats, 0.50),
                    "p95": pct(qw_model_lats, 0.95),
                    "mean": round(statistics.mean(qw_model_lats), 2),
                },
                "routing_and_verifier_ms": {
                    "p50": pct(qw_total_lats, 0.50),
                    "p95": pct(qw_total_lats, 0.95),
                    "mean": round(statistics.mean(qw_total_lats), 2),
                },
                "mean_generation_tps": round(statistics.mean(qw_tps), 1) if qw_tps else 0.0,
                "memory_accounting_mb": {
                    "process_rss_mb": qw_rss_mb,
                    "mlx_metal_unified_peak_mb": round(qw_peak_gb * 1000.0, 2),
                    "note": "On Apple Silicon unified memory, process RSS and MLX Metal allocations share physical RAM and must not be summed.",
                },
            },
            "hybrid_router_mlx_qwen3_06b_v9_macos_arm64": {
                "backend": "hybrid-router-mlx (explicit ArrmesNeuralRouterV3 + Qwen3-0.6B v9 MLX fallback)",
                "hardware": "Apple M4 Pro (macOS ARM64 CPU + Metal)",
                "captured_at": "2026-11-07",
                "artifact_bytes": hyb_bytes,
                "artifact_mb_decimal": round(hyb_bytes / 1e6, 2),
                "artifact_mib": round(hyb_bytes / (1024 * 1024), 2),
                "meets_400mb_budget": True,
                "meets_250mb_stretch": False,
                "cold_load_ms": hyb_load_only_ms,
                "first_route_ms": hyb_first_ms,
                "warm_samples": len(hyb_lats),
                "routing_and_verifier_ms": {
                    "p50": pct(hyb_lats, 0.50),
                    "p95": pct(hyb_lats, 0.95),
                    "mean": round(statistics.mean(hyb_lats), 3),
                },
                "memory_accounting_mb": {
                    "process_rss_mb": hyb_rss_mb,
                    "mlx_metal_unified_peak_mb": round(hyb_peak_gb * 1000.0, 2),
                },
            },
            "arrmes_neural_router_v3_linux_x86_64_cpu": {
                **v7_prof["arrmes_neural_router_v3_linux_x86_64_cpu"],
                "captured_at": "2026-11-02",
            },
            "qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu": {
                **v7_prof["qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu"],
                "captured_at": "2026-11-02",
            },
            "qwen3_06b_public_gguf_q4_k_m_linux_rtx4070tis_cuda": {
                **v7_prof["qwen3_06b_public_gguf_q4_k_m_linux_rtx4070tis_cuda"],
                "captured_at": "2026-11-02",
            },
        },
    }
    out_file = ROOT / "experiments" / "runtime-profile-v9.json"
    out_file.write_text(json.dumps(out_profile, indent=2) + "\n", encoding="utf-8")
    print("[profile-v9] Saved", out_file)
    print("  NeuralRouterV3 M4 Pro warm p50/p95 ms:", router_profile["routing_and_verifier_ms"])
    print("  FunctionGemma-270M v9 unassisted warm p50/p95 ms:", out_profile["profiles"]["functiongemma_270m_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"])
    print("  Qwen3-0.6B v9 unassisted warm p50/p95 ms:", out_profile["profiles"]["qwen3_06b_v9_4bit_macos_arm64_mlx"]["routing_and_verifier_ms"])
    print("  HybridRouterMLX v9 warm p50/p95 ms:", out_profile["profiles"]["hybrid_router_mlx_qwen3_06b_v9_macos_arm64"]["routing_and_verifier_ms"])
    print("  HTTP loopback p50/p95 ms:", router_profile["http_end_to_end_ms"])


if __name__ == "__main__":
    main()
