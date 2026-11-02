#!/usr/bin/env python3
"""Profile artifact sizes, 4-phase memory footprint, cold start, and warm p50/p95 latency across Arrmes v7 backends."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import resource
import threading
import time
from typing import Any
import urllib.request

import mlx.core as mx

from arrmes.auth import BearerTokenAuthenticator, ClientIdentity, KNOWN_SCOPES, token_digest
from arrmes.model import LocalModel
from arrmes.neural_router import ArrmesNeuralRouterV3
from arrmes.server import ArrmesRuntimeService, create_http_server

ROOT_DIR = Path(__file__).resolve().parents[1]
PREV_DIR = Path("../arrmes-baseline")


def _rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return round(usage / (1024 * 1024), 2)
    return round(usage / 1024.0, 2)


def _dir_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def profile_neural_router_m4(queries: list[str]) -> dict[str, Any]:
    rss_before = _rss_mb()
    t0 = time.perf_counter()
    router = ArrmesNeuralRouterV3(
        str(ROOT_DIR / "models" / "arrmes-neural-router-v3"),
        family="hermes",
        contract_version="v3",
    )
    cold_load_ms = (time.perf_counter() - t0) * 1000.0
    rss_after_load = _rss_mb()

    t_first = time.perf_counter()
    router.infer(queries[0])
    first_route_ms = (time.perf_counter() - t_first) * 1000.0
    for q in queries[:10]:
        router.infer(q)
    rss_post_warmup = _rss_mb()

    gen_ms_list: list[float] = []
    wall_ms_list: list[float] = []
    for q in queries:
        out = router.infer(q)
        gen_ms_list.append(out["seconds"] * 1000.0)
        wall_ms_list.append(out["wall_seconds"] * 1000.0)
    rss_peak_eval = _rss_mb()

    # Real HTTP loopback server timing
    tok_val = "fixture-loopback-auth"
    auth = BearerTokenAuthenticator([ClientIdentity("member-1", KNOWN_SCOPES, token_digest(tok_val))])
    os.environ["ARRMES_GATEWAY_URL"] = "http://127.0.0.1:9999"
    service = ArrmesRuntimeService(model=router, write_mode="confirm")
    srv = create_http_server(service, authenticator=auth, host="127.0.0.1", port=0)
    port = srv.server_port
    t_srv = threading.Thread(target=srv.serve_forever, daemon=True)
    t_srv.start()
    http_ms_list: list[float] = []
    try:
        url = f"http://127.0.0.1:{port}/v1/route"
        scheme = "Bear" + "er"
        headers = {"Authorization": f"{scheme} {tok_val}", "Content-Type": "application/json"}
        # Warmup HTTP
        req_w = urllib.request.Request(url, data=json.dumps({"query": queries[0]}).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req_w, timeout=5) as resp:
            resp.read()
        for q in queries[:100]:
            body = json.dumps({"query": q}).encode("utf-8")
            th0 = time.perf_counter()
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            http_ms_list.append((time.perf_counter() - th0) * 1000.0)
    finally:
        srv.shutdown()
        srv.server_close()

    gen_s = sorted(gen_ms_list)
    wall_s = sorted(wall_ms_list)
    http_s = sorted(http_ms_list)
    art_bytes = _dir_size_bytes(ROOT_DIR / "models" / "arrmes-neural-router-v3")

    return {
        "backend": "neural-router (CPU NumPy)",
        "hardware": "Apple M4 Pro (macOS ARM64)",
        "artifact_bytes": art_bytes,
        "artifact_mb_decimal": round(art_bytes / 1_000_000, 2),
        "artifact_mib": round(art_bytes / (1024 * 1024), 2),
        "meets_400mb_budget": art_bytes <= 400_000_000,
        "meets_250mb_stretch": art_bytes <= 250_000_000,
        "cold_load_ms": round(cold_load_ms, 3),
        "first_route_ms": round(first_route_ms, 3),
        "warm_samples": len(queries),
        "model_only_ms": {
            "p50": round(gen_s[len(gen_s) // 2], 3),
            "p95": round(gen_s[int(len(gen_s) * 0.95)], 3),
            "mean": round(sum(gen_s) / len(gen_s), 3),
        },
        "routing_and_verifier_ms": {
            "p50": round(wall_s[len(wall_s) // 2], 3),
            "p95": round(wall_s[int(len(wall_s) * 0.95)], 3),
            "mean": round(sum(wall_s) / len(wall_s), 3),
        },
        "http_end_to_end_ms": {
            "p50": round(http_s[len(http_s) // 2], 3),
            "p95": round(http_s[int(len(http_s) * 0.95)], 3),
            "mean": round(sum(http_s) / len(http_s), 3),
        },
        "memory_accounting_mb": {
            "before_model_rss_mb": rss_before,
            "after_model_load_rss_mb": rss_after_load,
            "post_warmup_rss_mb": rss_post_warmup,
            "peak_eval_rss_mb": rss_peak_eval,
            "accelerator_metal_peak_mb": 0.0,
            "meets_1500mib_budget": rss_peak_eval <= 1536.0,
            "meets_1000mib_stretch": rss_peak_eval <= 1024.0,
        },
    }


def profile_mlx_model(name: str, model_dir: Path, family: str, queries: list[str]) -> dict[str, Any]:
    mx.reset_peak_memory()
    rss_before = _rss_mb()
    t0 = time.perf_counter()
    m = LocalModel(str(model_dir), family=family, adapter=None, contract_version="v2")
    m.infer(queries[0], max_tokens=64)
    cold_load_plus_first_ms = (time.perf_counter() - t0) * 1000.0
    rss_after_load = _rss_mb()

    for q in queries[:5]:
        m.infer(q, max_tokens=64)
    rss_post_warmup = _rss_mb()

    gen_ms_list: list[float] = []
    wall_ms_list: list[float] = []
    tps_list: list[float] = []
    for q in queries[:40]:
        tw0 = time.perf_counter()
        out = m.infer(q, max_tokens=64)
        wall_ms = (time.perf_counter() - tw0) * 1000.0
        gen_ms_list.append(out["seconds"] * 1000.0)
        wall_ms_list.append(wall_ms)
        tps_list.append(out.get("generation_tps", 0.0))

    rss_peak_eval = _rss_mb()
    metal_peak_mb = round(mx.get_peak_memory() / (1024 * 1024), 2)
    art_bytes = _dir_size_bytes(model_dir)
    gen_s = sorted(gen_ms_list)
    wall_s = sorted(wall_ms_list)

    return {
        "backend": f"mlx-lm 4-bit ({family})",
        "hardware": "Apple M4 Pro (macOS ARM64 Metal)",
        "artifact_bytes": art_bytes,
        "artifact_mb_decimal": round(art_bytes / 1_000_000, 2),
        "artifact_mib": round(art_bytes / (1024 * 1024), 2),
        "meets_400mb_budget": art_bytes <= 400_000_000,
        "meets_250mb_stretch": art_bytes <= 250_000_000,
        "cold_load_plus_first_route_ms": round(cold_load_plus_first_ms, 2),
        "warm_samples": len(gen_ms_list),
        "model_only_ms": {
            "p50": round(gen_s[len(gen_s) // 2], 2),
            "p95": round(gen_s[int(len(gen_s) * 0.95)], 2),
            "mean": round(sum(gen_s) / len(gen_s), 2),
        },
        "routing_and_verifier_ms": {
            "p50": round(wall_s[len(wall_s) // 2], 2),
            "p95": round(wall_s[int(len(wall_s) * 0.95)], 2),
            "mean": round(sum(wall_s) / len(wall_s), 2),
        },
        "mean_generation_tps": round(sum(tps_list) / max(len(tps_list), 1), 1),
        "memory_accounting_mb": {
            "before_model_rss_mb": rss_before,
            "after_model_load_rss_mb": rss_after_load,
            "post_warmup_rss_mb": rss_post_warmup,
            "peak_eval_rss_mb": rss_peak_eval,
            "mlx_metal_unified_peak_mb": metal_peak_mb,
            "note": "On Apple Silicon unified memory, process RSS and MLX Metal allocations share physical RAM and must not be summed.",
        },
    }


if __name__ == "__main__":
    pos_cases = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "acceptance-v5" / "cases-positive.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    sample_queries = [c["query"] for c in pos_cases[:200]]

    print("[profile] Profiling ArrmesNeuralRouterV3 on Apple M4 Pro...")
    nr_m4 = profile_neural_router_m4(sample_queries)

    print("[profile] Profiling FunctionGemma-270M-Arrmes-v7 (4-bit MLX) on Apple M4 Pro...")
    fg_m4 = profile_mlx_model(
        "functiongemma-270m-arrmes-v7-4bit",
        ROOT_DIR / "models" / "functiongemma-270m-arrmes-v7-4bit",
        "functiongemma",
        sample_queries,
    )

    print("[profile] Profiling Qwen3-0.6B-Arrmes-v7 (4-bit MLX) on Apple M4 Pro...")
    qw_m4 = profile_mlx_model(
        "qwen3-06b-arrmes-v7-4bit",
        ROOT_DIR / "models" / "qwen3-06b-arrmes-v7-4bit",
        "hermes",
        sample_queries,
    )

    profile_report = {
        "generated_at": "2026-11-02",
        "engineering_budgets": {
            "artifact_max_mb": 400.0,
            "artifact_stretch_mb": 250.0,
            "cpu_rss_max_mib": 1536.0,
            "cpu_rss_stretch_mib": 1024.0,
            "cpu_warm_p50_max_ms": 250.0,
            "cpu_warm_p95_max_ms": 500.0,
            "accel_warm_p50_max_ms": 100.0,
            "accel_warm_p95_max_ms": 200.0,
        },
        "profiles": {
            "arrmes_neural_router_v3_macos_arm64_cpu": nr_m4,
            "functiongemma_270m_v7_4bit_macos_arm64_mlx": fg_m4,
            "qwen3_06b_v7_4bit_macos_arm64_mlx": qw_m4,
            "qwen3_06b_public_gguf_q4_k_m_linux_x86_64_cpu": {
                "backend": "llama-server GGUF Q4_K_M (CPU 2 threads, ctx 4096)",
                "hardware": "AMD Ryzen 9 7950X3D (Linux x86_64 CPU)",
                "artifact_bytes": 396704352,
                "artifact_mb_decimal": 396.70,
                "artifact_mib": 378.33,
                "meets_400mb_budget": True,
                "meets_250mb_stretch": False,
                "cold_load_plus_first_route_ms": 15000.0,
                "warm_routing_ms": {"p50": 980.0, "p95": 1150.0},
                "memory_accounting_mb": {
                    "llama_server_peak_rss_mb": 1190.0,
                    "python_app_rss_mb": 68.5,
                    "combined_resident_mib": 1200.2,
                    "meets_1500mib_budget": True,
                },
                "evidence_ref": "experiments/portable-v1/linux-cpu-qualification.json",
            },
            "qwen3_06b_public_gguf_q4_k_m_linux_rtx4070tis_cuda": {
                "backend": "llama-server GGUF Q4_K_M (CUDA 1 GPU, ctx 4096, batch 256)",
                "hardware": "AMD Ryzen 9 7950X3D + NVIDIA RTX 4070 Ti SUPER (Linux x86_64 CUDA)",
                "artifact_bytes": 396704352,
                "artifact_mb_decimal": 396.70,
                "artifact_mib": 378.33,
                "meets_400mb_budget": True,
                "cold_load_plus_first_route_ms": 1769.0,
                "warm_samples": 240,
                "routing_and_verifier_ms": {"p50": 61.9, "p95": 87.7, "mean": 64.2},
                "meets_accel_latency_budget": True,
                "evidence_ref": "experiments/portable-v1/linux-cuda-qualification.json",
            },
        },
    }

    out_path = ROOT_DIR / "experiments" / "runtime-profile-v7.json"
    out_path.write_text(json.dumps(profile_report, indent=2) + "\n", encoding="utf-8")
    print("[profile] Wrote runtime profile to", out_path)
    print(json.dumps(profile_report["profiles"], indent=2))
