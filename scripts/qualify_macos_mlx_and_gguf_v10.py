#!/usr/bin/env python3
"""Convert v10 merged BF16 checkpoints to MLX 4-bit/8-bit and qualify macOS MLX + GGUF runtimes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

from arrmes.auth import BearerTokenAuthenticator
from arrmes.backends import LlamaServerModel
from arrmes.evaluate import score
from arrmes.model import HybridRouterMLXModel, LocalModel
from arrmes.neural_router import ArrmesNeuralRouterV3
from arrmes.server import ArrmesRuntimeService, create_http_server


def rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024.0, 2)


def dir_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_prepare_dir(target: Path) -> None:
    """Ensure we never overwrite a symlink target in the parent repo."""
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def normalize_hf_config_for_mlx(merged_dir: Path) -> None:
    """Ensure transformers-5.x exported configs retain top-level rope_theta and dict extra_special_tokens for mlx-lm."""
    cfg_p = merged_dir / "config.json"
    if cfg_p.is_file():
        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        changed = False
        rp = cfg.get("rope_parameters") or {}
        if "rope_theta" not in cfg and isinstance(rp, dict):
            theta = rp.get("rope_theta") or (rp.get("full_attention") or {}).get("rope_theta")
            if theta is not None:
                cfg["rope_theta"] = theta
                changed = True
            l_theta = (rp.get("sliding_attention") or {}).get("rope_theta")
            if l_theta is not None and "rope_local_base_freq" not in cfg:
                cfg["rope_local_base_freq"] = l_theta
                changed = True
        if changed:
            cfg_p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tcfg_p = merged_dir / "tokenizer_config.json"
    if tcfg_p.is_file():
        tcfg = json.loads(tcfg_p.read_text(encoding="utf-8"))
        changed_t = False
        if isinstance(tcfg.get("extra_special_tokens"), list):
            tcfg.pop("extra_special_tokens", None)
            changed_t = True
        if tcfg.get("tokenizer_class") == "TokenizersBackend":
            tcfg["tokenizer_class"] = "PreTrainedTokenizerFast"
            changed_t = True
        if changed_t:
            tcfg_p.write_text(json.dumps(tcfg, indent=2) + "\n", encoding="utf-8")


def convert_mlx_quant(merged_dir: Path, out_dir: Path, q_bits: int) -> dict[str, Any]:
    normalize_hf_config_for_mlx(merged_dir)
    safe_prepare_dir(out_dir)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mlx_lm.convert",
            "--hf-path",
            str(merged_dir),
            "--mlx-path",
            str(out_dir),
            "-q",
            "--q-bits",
            str(q_bits),
        ],
        check=True,
    )
    sz = dir_bytes(out_dir)
    st_digests = {f.name: sha256_file(f) for f in sorted(out_dir.glob("*.safetensors"))}
    return {
        "mlx_dir": out_dir.name,
        "q_bits": q_bits,
        "artifact_bytes": sz,
        "artifact_mb_decimal": round(sz / 1_000_000.0, 2),
        "artifact_mib": round(sz / (1024 * 1024), 2),
        "meets_400mb_budget": sz <= 400_000_000,
        "meets_250mb_stretch": sz <= 250_000_000,
        "safetensors_sha256": st_digests,
    }


def profile_mlx_model(
    mlx_dir: Path,
    family: str,
    ov1_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    import mlx.core as mx
    mx.reset_peak_memory()
    t0 = time.perf_counter()
    model = LocalModel(str(mlx_dir), family=family, contract_version="v2")
    cold_load_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    t_first = time.perf_counter()
    _ = model.infer(ov1_rows[0]["query"], ov1_rows[0].get("context"))
    first_route_ms = round((time.perf_counter() - t_first) * 1000.0, 2)

    gen_ms: list[float] = []
    wall_ms: list[float] = []
    tps_list: list[float] = []
    raw_ok = 0
    ver_ok = 0
    ver_outputs: list[str] = []

    for idx, c in enumerate(ov1_rows):
        tw = time.perf_counter()
        inf = model.infer(c["query"], c.get("context"))
        w_ms = (time.perf_counter() - tw) * 1000.0
        if idx >= 3:
            gen_ms.append(inf["seconds"] * 1000.0)
            wall_ms.append(w_ms)
            if isinstance(inf.get("generation_tps"), (int, float)):
                tps_list.append(float(inf["generation_tps"]))
        raw_ok += int(score(inf["raw_unverified"], c["expected"], family, allow_v3=False)["exact"])
        ver_ok += int(score(inf["raw"], c["expected"], family, allow_v3=False)["exact"])
        ver_outputs.append(inf["raw"])

    sz = dir_bytes(mlx_dir)
    peak_metal_mb = round(mx.get_peak_memory() / (1024 * 1024), 2)
    return {
        "backend": f"mlx-lm fused ({family})",
        "hardware": "Apple M4 Pro (macOS ARM64 Metal)",
        "captured_at": "2026-11-11",
        "artifact_bytes": sz,
        "artifact_mb_decimal": round(sz / 1_000_000.0, 2),
        "artifact_mib": round(sz / (1024 * 1024), 2),
        "meets_400mb_budget": sz <= 400_000_000,
        "meets_250mb_stretch": sz <= 250_000_000,
        "cold_load_ms": cold_load_ms,
        "first_route_ms": first_route_ms,
        "cold_load_plus_first_route_ms": round(cold_load_ms + first_route_ms, 2),
        "warm_samples": len(wall_ms),
        "model_only_ms": {
            "p50": round(statistics.median(gen_ms), 3),
            "p95": round(sorted(gen_ms)[int(0.95 * len(gen_ms))], 3),
            "mean": round(statistics.mean(gen_ms), 3),
        },
        "routing_and_verifier_ms": {
            "p50": round(statistics.median(wall_ms), 3),
            "p95": round(sorted(wall_ms)[int(0.95 * len(wall_ms))], 3),
            "mean": round(statistics.mean(wall_ms), 3),
        },
        "mean_generation_tps": round(statistics.mean(tps_list), 2) if tps_list else None,
        "overnight_v1_80": {
            "raw_exact": raw_ok,
            "raw_accuracy": round(raw_ok / len(ov1_rows), 6),
            "verified_exact": ver_ok,
            "verified_accuracy": round(ver_ok / len(ov1_rows), 6),
            "verified_outputs_sha256": hashlib.sha256("\n".join(ver_outputs).encode()).hexdigest(),
        },
        "memory_accounting_mb": {
            "process_rss_mb": rss_mb(),
            "mlx_metal_unified_peak_mb": peak_metal_mb,
            "note": "On Apple Silicon unified memory, isolated_process_peak_rss_mib measures whole-process OS ru_maxrss (including CPU heap such as FunctionGemma's 262k-token BPE tokenizer) while mlx_metal_unified_peak_mib measures mx.get_peak_memory() inside the Metal GPU tensor allocator only; the two overlap in physical RAM and must not be summed.",
        },
    }


def profile_hybrid_mlx(mlx_dir: Path, router_dir: Path, ov1_rows: list[dict[str, Any]]) -> dict[str, Any]:
    import mlx.core as mx
    mx.reset_peak_memory()
    t0 = time.perf_counter()
    hyb = HybridRouterMLXModel(str(mlx_dir), family="hermes", contract_version="v2", router_dir=str(router_dir))
    cold_load_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    t_first = time.perf_counter()
    _ = hyb.infer(ov1_rows[0]["query"], ov1_rows[0].get("context"))
    first_route_ms = round((time.perf_counter() - t_first) * 1000.0, 2)

    wall_ms: list[float] = []
    ver_ok = 0
    mlx_fallbacks = 0
    for idx, c in enumerate(ov1_rows):
        inf = hyb.infer(c["query"], c.get("context"))
        if idx >= 3:
            wall_ms.append(inf["seconds"] * 1000.0)
        if inf.get("hybrid_route_source") == "mlx-lm":
            mlx_fallbacks += 1
        ver_ok += int(score(inf["raw"], c["expected"], "hermes", allow_v3=False)["exact"])

    sz = dir_bytes(mlx_dir) + (router_dir / "router_weights.npz").stat().st_size
    return {
        "backend": "hybrid-router-mlx (ArrmesNeuralRouterV3 v10 + Qwen3-0.6B v10 4-bit MLX fallback)",
        "hardware": "Apple M4 Pro (macOS ARM64 CPU + Metal)",
        "captured_at": "2026-11-11",
        "artifact_bytes": sz,
        "artifact_mb_decimal": round(sz / 1_000_000.0, 2),
        "artifact_mib": round(sz / (1024 * 1024), 2),
        "meets_400mb_budget": sz <= 400_000_000,
        "meets_250mb_stretch": sz <= 250_000_000,
        "cold_load_ms": cold_load_ms,
        "first_route_ms": first_route_ms,
        "warm_samples": len(wall_ms),
        "mlx_fallback_count_of_80": mlx_fallbacks,
        "overnight_v1_verified_accuracy": round(ver_ok / len(ov1_rows), 6),
        "routing_and_verifier_ms": {
            "p50": round(statistics.median(wall_ms), 3),
            "p95": round(sorted(wall_ms)[int(0.95 * len(wall_ms))], 3),
            "mean": round(statistics.mean(wall_ms), 3),
        },
        "memory_accounting_mb": {
            "process_rss_mb": rss_mb(),
            "mlx_metal_unified_peak_mb": round(mx.get_peak_memory() / (1024 * 1024), 2),
        },
    }


def profile_macos_gguf(gguf_path: Path, family: str, device: str, ov1_rows: list[dict[str, Any]]) -> dict[str, Any]:
    t0 = time.perf_counter()
    with LlamaServerModel(str(gguf_path), family=family, contract_version="v2", device=device, context_size=4096) as model:
        load_ms = round(model.load_seconds * 1000.0, 2)
        tf = time.perf_counter()
        _ = model.infer(ov1_rows[0]["query"], ov1_rows[0].get("context"))
        first_ms = round((time.perf_counter() - tf) * 1000.0, 2)
        cold_plus = round((time.perf_counter() - t0) * 1000.0, 2)
        wall_ms: list[float] = []
        raw_ok = ver_ok = 0
        ver_outputs: list[str] = []
        for idx, c in enumerate(ov1_rows):
            inf = model.infer(c["query"], c.get("context"))
            if idx >= 3:
                wall_ms.append(inf["wall_seconds"] * 1000.0)
            raw_ok += int(score(inf["raw_unverified"], c["expected"], family, allow_v3=False)["exact"])
            ver_ok += int(score(inf["raw"], c["expected"], family, allow_v3=False)["exact"])
            ver_outputs.append(inf["raw"])
    sz = gguf_path.stat().st_size
    return {
        "backend": f"llama-server GGUF ({device}, ctx 4096)",
        "hardware": f"Apple M4 Pro (macOS ARM64 {device})",
        "captured_at": "2026-11-11",
        "artifact_bytes": sz,
        "artifact_mb_decimal": round(sz / 1_000_000.0, 2),
        "artifact_mib": round(sz / (1024 * 1024), 2),
        "meets_400mb_budget": sz <= 400_000_000,
        "meets_250mb_stretch": sz <= 250_000_000,
        "cold_load_ms": load_ms,
        "first_route_ms": first_ms,
        "cold_load_plus_first_route_ms": cold_plus,
        "warm_samples": len(wall_ms),
        "routing_and_verifier_ms": {
            "p50": round(statistics.median(wall_ms), 2),
            "p95": round(sorted(wall_ms)[int(0.95 * len(wall_ms))], 2),
            "mean": round(statistics.mean(wall_ms), 2),
        },
        "overnight_v1_80": {
            "raw_exact": raw_ok,
            "raw_accuracy": round(raw_ok / len(ov1_rows), 6),
            "verified_exact": ver_ok,
            "verified_accuracy": round(ver_ok / len(ov1_rows), 6),
            "verified_outputs_sha256": hashlib.sha256("\n".join(ver_outputs).encode()).hexdigest(),
        },
    }


def profile_router_http(router_dir: Path, pos_cases: list[dict[str, Any]]) -> dict[str, float]:
    router = ArrmesNeuralRouterV3(str(router_dir), family="hermes", contract_version="v3")
    auth = BearerTokenAuthenticator.from_mapping({
        "clients": [{"token_sha256": hashlib.sha256(b"bench-token-v10").hexdigest(), "subject": "bench-user", "scopes": ["media:read"]}]
    })
    prev_gw = os.environ.get("ARRMES_GATEWAY_URL")
    os.environ["ARRMES_GATEWAY_URL"] = "http://127.0.0.1:1"
    rt_service = ArrmesRuntimeService(model=router, write_mode="confirm")
    srv = create_http_server(rt_service, authenticator=auth, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    http_ms: list[float] = []
    try:
        for c in pos_cases[:65]:
            body = json.dumps({"query": c["query"], "context": c.get("context") or {}}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/route",
                data=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer bench-token-v10"},
                method="POST",
            )
            ts = time.perf_counter()
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            http_ms.append((time.perf_counter() - ts) * 1000.0)
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)
        if prev_gw is None:
            os.environ.pop("ARRMES_GATEWAY_URL", None)
        else:
            os.environ["ARRMES_GATEWAY_URL"] = prev_gw
    warm = http_ms[5:]
    return {
        "p50": round(statistics.median(warm), 3),
        "p95": round(sorted(warm)[int(0.95 * len(warm))], 3),
        "mean": round(statistics.mean(warm), 3),
    }


def main() -> int:
    models_dir = ROOT_DIR / "models"
    ov1_rows = [json.loads(l) for l in (ROOT_DIR / "data/overnight-v1/routing-heldout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    pos_cases = [json.loads(l) for l in (ROOT_DIR / "data/acceptance-v7/cases-positive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()][:80]

    mlx_exports = {
        "qwen3_06b_v10_4bit": convert_mlx_quant(models_dir / "arrmes-qwen3-0.6b-v10-merged", models_dir / "qwen3-06b-arrmes-v10-4bit", 4),
        "qwen3_06b_v10_8bit": convert_mlx_quant(models_dir / "arrmes-qwen3-0.6b-v10-merged", models_dir / "qwen3-06b-arrmes-v10-8bit", 8),
        "functiongemma_270m_v10_4bit": convert_mlx_quant(models_dir / "arrmes-functiongemma-270m-v10-merged", models_dir / "functiongemma-270m-arrmes-v10-4bit", 4),
        "functiongemma_270m_v10_8bit": convert_mlx_quant(models_dir / "arrmes-functiongemma-270m-v10-merged", models_dir / "functiongemma-270m-arrmes-v10-8bit", 8),
    }

    router_http = profile_router_http(models_dir / "arrmes-neural-router-v3", pos_cases)

    profiles = {
        "qwen3_06b_v10_4bit_macos_arm64_mlx": profile_mlx_model(models_dir / "qwen3-06b-arrmes-v10-4bit", "hermes", ov1_rows),
        "qwen3_06b_v10_8bit_macos_arm64_mlx": profile_mlx_model(models_dir / "qwen3-06b-arrmes-v10-8bit", "hermes", ov1_rows),
        "functiongemma_270m_v10_4bit_macos_arm64_mlx": profile_mlx_model(models_dir / "functiongemma-270m-arrmes-v10-4bit", "functiongemma", ov1_rows),
        "functiongemma_270m_v10_8bit_macos_arm64_mlx": profile_mlx_model(models_dir / "functiongemma-270m-arrmes-v10-8bit", "functiongemma", ov1_rows),
        "hybrid_router_mlx_qwen3_06b_v10_macos_arm64": profile_hybrid_mlx(
            models_dir / "qwen3-06b-arrmes-v10-4bit",
            models_dir / "arrmes-neural-router-v3",
            ov1_rows,
        ),
    }

    gguf_dir = models_dir / "gguf-v10"
    if (gguf_dir / "arrmes-qwen3-0.6b-v10-q4_k_m.gguf").is_file():
        profiles["qwen3_06b_v10_gguf_q4_k_m_macos_arm64_cpu"] = profile_macos_gguf(
            gguf_dir / "arrmes-qwen3-0.6b-v10-q4_k_m.gguf", "hermes", "cpu", ov1_rows
        )
        profiles["qwen3_06b_v10_gguf_q4_k_m_macos_arm64_metal"] = profile_macos_gguf(
            gguf_dir / "arrmes-qwen3-0.6b-v10-q4_k_m.gguf", "hermes", "metal", ov1_rows
        )
    if (gguf_dir / "arrmes-functiongemma-270m-v10-q4_k_m.gguf").is_file():
        profiles["functiongemma_270m_v10_gguf_q4_k_m_macos_arm64_cpu"] = profile_macos_gguf(
            gguf_dir / "arrmes-functiongemma-270m-v10-q4_k_m.gguf", "functiongemma", "cpu", ov1_rows
        )
        profiles["functiongemma_270m_v10_gguf_q4_k_m_macos_arm64_metal"] = profile_macos_gguf(
            gguf_dir / "arrmes-functiongemma-270m-v10-q4_k_m.gguf", "functiongemma", "metal", ov1_rows
        )

    out = {"mlx_exports": mlx_exports, "router_http_end_to_end_ms": router_http, "profiles": profiles}
    out_p = ROOT_DIR / "work/macos_v10_qualification.json"
    out_p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print("[DONE] Wrote", out_p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
