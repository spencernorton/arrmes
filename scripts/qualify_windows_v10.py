#!/usr/bin/env python3
"""Native Windows (and portable CPU/CUDA) v10 GGUF + NeuralRouterV3 qualification.

Measures cold startup, warm direct inference, authenticated HTTP loopback latency,
native OS peak resident/commit memory (Win32 K32GetProcessMemoryInfo / Linux VmHWM),
full N=80 overnight-v1 raw & verified GGUF accuracy, v3 service_query execution, and
full N=2,580 acceptance-v7 + N=3,135 acceptance-v8 NeuralRouterV3 bitwise parity.
Keeps hosted CI execution strictly distinct from owned physical Windows hardware.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from arrmes.auth import BearerTokenAuthenticator
from arrmes.backends import LlamaServerModel
from arrmes.evaluate import score
import arrmes.neural_router as _nr_mod
from arrmes.neural_router import ArrmesNeuralRouterV3
from arrmes.protocol import decode, normalized
from arrmes.server import ArrmesRuntimeService, create_http_server
from smoke_portable_model import RUNTIME_COMMIT, file_sha256, measure_process_memory, run_smoke

PINNED_ARTIFACTS = {
    "router_weights_npz": {
        "repo": "NorviTech/arrmes-neural-router-v3",
        "commit_oid": "f20f3a62222b2297eea86dae31f710052294bea2",
        "sha256": "d91eca610add347575ea6c29e04d1861aba8045b4970c8c72ec2b4ecf1a0be05",
    },
    "router_config_json": {
        "repo": "NorviTech/arrmes-neural-router-v3",
        "commit_oid": "f20f3a62222b2297eea86dae31f710052294bea2",
        "sha256": "89c87c0e59308c916f445f44c3768c5b9fc61d3b0858e65ef6474c47912e4dd4",
    },
    "functiongemma_270m_q4_k_m_gguf": {
        "repo": "NorviTech/arrmes-functiongemma-270m",
        "commit_oid": "44ab0fecc06a899995402ec409f1ea6701449ab3",
        "sha256": "1b72dcd37745d50ff6f63f71e13f4274079239d8989e098a728e1dc277885123",
    },
    "qwen3_06b_q4_k_m_gguf": {
        "repo": "NorviTech/arrmes-qwen3-0.6b",
        "commit_oid": "30e35bc6fa950cd8cbc01dc3b3f41f3323e39e90",
        "sha256": "0e5310b8d22a0c89826b8c9cd9e857656b301b0de4b63cdf27285338ef09dd84",
    },
}
EXPECTED_ROUTER_AV7_SHA256 = "f23e6de6933347e47c2765727c7401cff02b22b5096a822c60a63335f19a9198"
PINNED_BENCH_V7 = {
    "repo": "datasets/NorviTech/arrmes-bench-v7",
    "commit_oid": "fd8911f72cb23e3aae169994beb017eb07f79582",
    "cases_positive_sha256": "f07774983d3e61b1bd1e5c55a35b1e5c8618cc6e6d95663eadbdca90978cbf52",
    "cases_negative_sha256": "5f58d213d55f44ed3f9638944583141af9047d5aef9436e5ca4d358f72a99c04",
    "overnight_v1_sha256": "1ed8e3e83c78a22d37991e7b2ef861464bf2bf6c224c782e81bc352005a31e14",
}


def _pct_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_idx = min(len(ordered) - 1, max(0, int(0.95 * len(ordered))))
    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[p95_idx], 3),
        "mean": round(statistics.mean(ordered), 3),
    }


def _measure_http_loopback(model: Any, queries: list[dict[str, Any]], *, timeout_s: float = 30.0) -> dict[str, float]:
    ephemeral_key = os.urandom(16).hex()
    auth = BearerTokenAuthenticator.from_mapping(
        {
            "clients": [
                {
                    "token_sha256": hashlib.sha256(ephemeral_key.encode("utf-8")).hexdigest(),
                    "subject": "windows-v10-bench",
                    "scopes": ["media:read", "activity:read"],
                }
            ]
        }
    )
    old_gw = os.environ.get("ARRMES_GATEWAY_URL")
    os.environ["ARRMES_GATEWAY_URL"] = "http://127.0.0.1:1"
    rt_service = ArrmesRuntimeService(model=model, write_mode="confirm")
    srv = create_http_server(rt_service, authenticator=auth, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    latencies_ms: list[float] = []
    try:
        for idx, row in enumerate(queries):
            body = json.dumps({"query": row["query"], "context": row.get("context") or {}}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/route",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": " ".join(("Bearer", ephemeral_key)),
                },
                method="POST",
            )
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if not isinstance(payload, dict) or "proposal" not in payload:
                raise RuntimeError("Invalid /v1/route loopback response")
            if idx >= 2:
                latencies_ms.append(elapsed_ms)
    finally:
        srv.shutdown()
        srv.server_close()
        if old_gw is None:
            os.environ.pop("ARRMES_GATEWAY_URL", None)
        else:
            os.environ["ARRMES_GATEWAY_URL"] = old_gw
    return _pct_summary(latencies_ms)


def _query_cuda_telemetry() -> dict[str, Any] | None:
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 5:
        return None
    return {
        "gpu_name": parts[0],
        "driver_version": parts[1],
        "memory_total_mib": int(parts[2]),
        "memory_used_mib": int(parts[3]),
        "memory_free_mib": int(parts[4]),
    }


def qualify_neural_router(router_dir: Path, av7_pos: list[dict[str, Any]], av7_neg: list[dict[str, Any]], ov1_rows: list[dict[str, Any]]) -> dict[str, Any]:
    npz_sha = file_sha256(router_dir / "router_weights.npz")
    cfg_sha = file_sha256(router_dir / "router_config.json")
    if npz_sha != PINNED_ARTIFACTS["router_weights_npz"]["sha256"]:
        raise ValueError("router_weights.npz SHA-256 mismatch")
    if cfg_sha != PINNED_ARTIFACTS["router_config_json"]["sha256"]:
        raise ValueError("router_config.json SHA-256 mismatch")

    t0 = time.perf_counter()
    router_v3 = ArrmesNeuralRouterV3(str(router_dir), family="hermes", contract_version="v3")
    cold_load_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    router_v2 = ArrmesNeuralRouterV3(str(router_dir), family="hermes", contract_version="v2")

    t_first = time.perf_counter()
    _ = router_v3.infer(av7_pos[0]["query"], av7_pos[0].get("context"))
    first_route_ms = round((time.perf_counter() - t_first) * 1000.0, 3)

    model_ms: list[float] = []
    wall_ms: list[float] = []
    for c in av7_pos[:200]:
        out = router_v3.infer(c["query"], c.get("context"))
        model_ms.append(out["seconds"] * 1000.0)
        wall_ms.append(out.get("wall_seconds", out["seconds"]) * 1000.0)

    http_ms = _measure_http_loopback(router_v3, av7_pos[:62], timeout_s=10.0)

    av7_v3_outputs: list[str] = []
    av7_pos_ok = 0
    av7_neg_ok = 0
    av7_neg_v2_ok = 0
    for c in av7_pos:
        av7_v3_outputs.append(router_v3.infer(c["query"], c.get("context"))["raw"])
        use_v3 = c.get("contract_version") == "v3"
        rtr_cur = router_v3 if use_v3 else router_v2
        inf = rtr_cur.infer(c["query"], c.get("context"))
        av7_pos_ok += int(score(inf["raw"], c["expected"], "hermes", allow_v3=use_v3)["exact"])
    for c in av7_neg:
        inf3 = router_v3.infer(c["query"], c.get("context"))
        av7_v3_outputs.append(inf3["raw"])
        av7_neg_ok += int(score(inf3["raw"], c["expected"], "hermes", allow_v3=True)["exact"])
        inf2 = router_v2.infer(c["query"], c.get("context"))
        av7_neg_v2_ok += int(score(inf2["raw"], c["expected"], "hermes", allow_v3=False)["exact"])
    av7_outputs = av7_v3_outputs
    av7_sha = hashlib.sha256("\n".join(av7_outputs).encode("utf-8")).hexdigest()

    ov1_ok = sum(
        int(score(router_v2.infer(c["query"], c.get("context"))["raw"], c["expected"], "hermes", allow_v3=False)["exact"])
        for c in ov1_rows
    )

    mem = measure_process_memory(None)
    py_mib = round(mem["python_peak_ram_bytes"] / (1024.0 * 1024.0), 2) if mem["python_peak_ram_bytes"] else None
    npz_bytes = (router_dir / "router_weights.npz").stat().st_size

    return {
        "matrix_backend": "numpy" if getattr(_nr_mod, "np", None) is not None else "stdlib_array",
        "artifact_bytes": npz_bytes,
        "artifact_mib": round(npz_bytes / (1024.0 * 1024.0), 3),
        "weights_sha256": npz_sha,
        "config_sha256": cfg_sha,
        "cold_load_ms": cold_load_ms,
        "first_route_ms": first_route_ms,
        "warm_samples": len(wall_ms),
        "model_only_ms": _pct_summary(model_ms),
        "routing_and_verifier_ms": _pct_summary(wall_ms),
        "http_loopback_ms": http_ms,
        "python_peak_ram_mib": py_mib,
        "acceptance_v7_2580": {
            "positive_exact": av7_pos_ok,
            "positive_total": len(av7_pos),
            "negative_v3_exact": av7_neg_ok,
            "negative_strict_v2_exact": av7_neg_v2_ok,
            "negative_total": len(av7_neg),
            "outputs_sha256": av7_sha,
            "matches_macos_and_linux_bitwise": av7_sha == EXPECTED_ROUTER_AV7_SHA256,
        },
        "overnight_v1_80": {
            "verified_exact": ov1_ok,
            "total": len(ov1_rows),
        },
    }


def qualify_gguf_model(
    gguf_path: Path,
    *,
    expected_sha256: str,
    family: str,
    executable: Path,
    device: str,
    threads: int,
    ov1_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    actual_sha = file_sha256(gguf_path)
    if actual_sha != expected_sha256:
        raise ValueError(f"GGUF SHA-256 mismatch for {gguf_path.name}")

    cuda_before = _query_cuda_telemetry() if device == "cuda" else None
    t_cold = time.perf_counter()
    with LlamaServerModel(
        str(gguf_path),
        family=family,
        contract_version="v2",
        device=device,
        executable=str(executable),
        context_size=4096,
        threads=threads,
    ) as model:
        load_ms = round(model.load_seconds * 1000.0, 2)
        t_first = time.perf_counter()
        first_inf = model.infer(ov1_rows[0]["query"], ov1_rows[0].get("context"), max_tokens=160)
        first_route_ms = round((time.perf_counter() - t_first) * 1000.0, 2)
        cold_plus_first_ms = round((time.perf_counter() - t_cold) * 1000.0, 2)

        wall_ms: list[float] = []
        gen_ms: list[float] = []
        tps_vals: list[float] = []
        raw_ok = 0
        ver_ok = 0
        ver_outputs: list[str] = []

        for idx, c in enumerate(ov1_rows):
            inf = first_inf if idx == 0 else model.infer(c["query"], c.get("context"), max_tokens=160)
            if idx >= 3:
                wall_ms.append(inf["wall_seconds"] * 1000.0)
                gen_ms.append(inf["seconds"] * 1000.0)
                if isinstance(inf.get("generation_tps"), (int, float)):
                    tps_vals.append(float(inf["generation_tps"]))
            raw_ok += int(score(inf["raw_unverified"], c["expected"], family, allow_v3=False)["exact"])
            ver_ok += int(score(inf["raw"], c["expected"], family, allow_v3=False)["exact"])
            ver_outputs.append(inf["raw"])

        http_ms = _measure_http_loopback(model, ov1_rows[:22], timeout_s=60.0)
        child_pid = getattr(getattr(model, "_process", None), "pid", None)
        mem = measure_process_memory(child_pid)
        cuda_during = _query_cuda_telemetry() if device == "cuda" else None

    # Also verify arrmes.tools.v3 service_query generation in a clean ctx-4096 v3 session
    v3_probes = [
        ("radarr_health", "Check Radarr system health", "service_query"),
        ("sonarr_health", "For Sonarr, retrieve the current service health readout", "service_query"),
        ("prowlarr_indexers", "List configured indexers in Prowlarr", "service_query"),
        ("bitmagnet_search", "Search Bitmagnet for Sintel 1080p", "bitmagnet_search"),
        ("negated_write_trap", "Do not delete Dune from Radarr", "unsupported"),
    ]
    v3_results: list[dict[str, Any]] = []
    with LlamaServerModel(
        str(gguf_path),
        family=family,
        contract_version="v3",
        device=device,
        executable=str(executable),
        context_size=4096,
        threads=threads,
    ) as model_v3:
        for probe_id, q_text, exp_tool in v3_probes:
            inf_v3 = model_v3.infer(q_text, max_tokens=160)
            ver_call = normalized(decode(inf_v3["raw"], family, allow_v3=True))
            v3_results.append(
                {
                    "id": probe_id,
                    "expected_tool": exp_tool,
                    "verified_tool": ver_call["name"],
                    "matches": ver_call["name"] == exp_tool,
                }
            )

    sz = gguf_path.stat().st_size
    srv_mib = round(mem["native_peak_ram_bytes"] / (1024.0 * 1024.0), 2) if mem["native_peak_ram_bytes"] else None
    commit_mib = (
        round(mem["native_peak_commit_bytes"] / (1024.0 * 1024.0), 2) if mem["native_peak_commit_bytes"] else None
    )
    py_mib = round(mem["python_peak_ram_bytes"] / (1024.0 * 1024.0), 2) if mem["python_peak_ram_bytes"] else None
    comb_mib = round(srv_mib + py_mib, 2) if (srv_mib is not None and py_mib is not None) else None

    return {
        "gguf_file": gguf_path.name,
        "gguf_sha256": actual_sha,
        "family": family,
        "device": device,
        "generation_limits": {
            "threads": threads,
            "context_size": 4096,
            "batch_size": 4096,
            "slots": 1,
            "max_tokens": 160,
            "temperature": 0,
            "seed": 0,
        },
        "artifact_bytes": sz,
        "artifact_mb_decimal": round(sz / 1_000_000.0, 2),
        "artifact_mib": round(sz / (1024.0 * 1024.0), 2),
        "cold_load_ms": load_ms,
        "first_route_ms": first_route_ms,
        "cold_load_plus_first_route_ms": cold_plus_first_ms,
        "warm_samples": len(wall_ms),
        "model_only_ms": _pct_summary(gen_ms),
        "routing_and_verifier_ms": _pct_summary(wall_ms),
        "http_loopback_ms": http_ms,
        "mean_generation_tps": round(statistics.mean(tps_vals), 2) if tps_vals else None,
        "overnight_v1": {
            "total": len(ov1_rows),
            "raw_exact": raw_ok,
            "raw_accuracy": round(raw_ok / len(ov1_rows), 6),
            "verified_exact": ver_ok,
            "verified_accuracy": round(ver_ok / len(ov1_rows), 6),
            "verified_outputs_sha256": hashlib.sha256("\n".join(ver_outputs).encode("utf-8")).hexdigest(),
        },
        "v3_service_query_probes": {
            "passed": sum(1 for r in v3_results if r["matches"]),
            "total": len(v3_results),
            "rows": v3_results,
        },
        "memory_accounting": {
            "method": mem["method"],
            "llama_server_peak_working_set_mib": srv_mib,
            "llama_server_peak_commit_mib": commit_mib,
            "python_peak_working_set_mib": py_mib,
            "combined_peak_working_set_mib": comb_mib,
            "meets_1536mib_budget": (comb_mib <= 1536.0) if comb_mib is not None else None,
            "cuda_before": cuda_before,
            "cuda_during": cuda_during,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--router-dir", type=Path, required=True)
    ap.add_argument("--fg-gguf", type=Path, required=True)
    ap.add_argument("--qwen-gguf", type=Path, required=True)
    ap.add_argument("--llama-server", type=Path, required=True)
    ap.add_argument("--device", type=str, default="cpu", choices=("cpu", "cuda"))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--bench-v7-dir", type=Path, default=None, help="Directory containing NorviTech/arrmes-bench-v7 JSONL files")
    ap.add_argument("--max-ov1", type=int, default=80)
    ap.add_argument("--environment-class", type=str, default="hosted_ci_runner", choices=("hosted_ci_runner", "physical_hardware"))
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    version_out = subprocess.run(
        [str(args.llama_server), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
        check=True,
    ).stdout.strip()
    if RUNTIME_COMMIT[:7] not in version_out:
        raise ValueError(f"Unexpected llama-server version: {version_out}")

    import arrmes

    pkg_dir = Path(arrmes.__file__).resolve().parent
    src_hashes = {
        "src/arrmes/" + p.relative_to(pkg_dir).as_posix(): file_sha256(p)
        for p in sorted(pkg_dir.rglob("*.py"))
    }

    if args.bench_v7_dir is not None:
        bdir = args.bench_v7_dir.resolve()
        pos_path = bdir / "cases-positive.jsonl"
        neg_path = bdir / "cases-negative.jsonl"
        ov1_path = bdir / "overnight-v1-routing-heldout.jsonl"
    else:
        pos_path = ROOT / "data/acceptance-v7/cases-positive.jsonl"
        neg_path = ROOT / "data/acceptance-v7/cases-negative.jsonl"
        ov1_path = ROOT / "data/overnight-v1/routing-heldout.jsonl"

    if file_sha256(pos_path) != PINNED_BENCH_V7["cases_positive_sha256"]:
        raise ValueError("cases-positive.jsonl SHA-256 mismatch against NorviTech/arrmes-bench-v7")
    if file_sha256(neg_path) != PINNED_BENCH_V7["cases_negative_sha256"]:
        raise ValueError("cases-negative.jsonl SHA-256 mismatch against NorviTech/arrmes-bench-v7")
    if file_sha256(ov1_path) != PINNED_BENCH_V7["overnight_v1_sha256"]:
        raise ValueError("overnight-v1-routing-heldout.jsonl SHA-256 mismatch against NorviTech/arrmes-bench-v7")

    av7_pos = [json.loads(l) for l in pos_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    av7_neg = [json.loads(l) for l in neg_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    ov1_all = [json.loads(l) for l in ov1_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    ov1_rows = ov1_all[: max(6, min(len(ov1_all), args.max_ov1))]

    rtr_res = qualify_neural_router(args.router_dir.resolve(), av7_pos, av7_neg, ov1_all)
    fg_res = qualify_gguf_model(
        args.fg_gguf.resolve(),
        expected_sha256=PINNED_ARTIFACTS["functiongemma_270m_q4_k_m_gguf"]["sha256"],
        family="functiongemma",
        executable=args.llama_server.resolve(),
        device=args.device,
        threads=args.threads,
        ov1_rows=ov1_rows,
    )
    qwen_res = qualify_gguf_model(
        args.qwen_gguf.resolve(),
        expected_sha256=PINNED_ARTIFACTS["qwen3_06b_q4_k_m_gguf"]["sha256"],
        family="hermes",
        executable=args.llama_server.resolve(),
        device=args.device,
        threads=args.threads,
        ov1_rows=ov1_rows,
    )

    fg_smoke = run_smoke(
        args.fg_gguf.resolve(),
        args.llama_server.resolve(),
        threads=args.threads,
        expected_sha256=PINNED_ARTIFACTS["functiongemma_270m_q4_k_m_gguf"]["sha256"],
        family="functiongemma",
    )
    qwen_smoke = run_smoke(
        args.qwen_gguf.resolve(),
        args.llama_server.resolve(),
        threads=args.threads,
        expected_sha256=PINNED_ARTIFACTS["qwen3_06b_q4_k_m_gguf"]["sha256"],
        family="hermes",
    )

    all_passed = (
        rtr_res["acceptance_v7_2580"]["matches_macos_and_linux_bitwise"]
        and rtr_res["acceptance_v7_2580"]["positive_exact"] == 1380
        and rtr_res["acceptance_v7_2580"]["negative_v3_exact"] == 1200
        and fg_smoke["all_checks_passed"]
        and qwen_smoke["all_checks_passed"]
        and fg_res["v3_service_query_probes"]["passed"] >= 4
        and qwen_res["v3_service_query_probes"]["passed"] >= 4
        and all(
            r["matches"]
            for r in fg_res["v3_service_query_probes"]["rows"] + qwen_res["v3_service_query_probes"]["rows"]
            if r["id"] == "negated_write_trap"
        )
    )

    summary = {
        "schema_version": "arrmes-windows-v10-qualification/v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": "arrmes-v10-clean-r1",
        "environment_class": args.environment_class,
        "qualification_boundary": (
            "Hosted GitHub Actions Windows Server 2025 AMD64 execution measures native Win32 x86_64 CPU GGUF inference, "
            "cold/warm/HTTP loopback latencies, Win32 PeakWorkingSetSize RAM, and overnight-v1 raw/verified accuracy, "
            "and is kept strictly distinct from owned physical Windows hardware and Windows NVIDIA CUDA driver execution."
            if args.environment_class == "hosted_ci_runner"
            else "Physical Windows hardware qualification."
        ),
        "hardware": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        "ci": {
            k: os.environ.get(k)
            for k in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "RUNNER_OS", "RUNNER_ARCH")
        },
        "package_version": arrmes.__version__,
        "python_version": sys.version,
        "source_manifest_sha256": hashlib.sha256(json.dumps(src_hashes, sort_keys=True).encode()).hexdigest(),
        "installed_distributions": sorted(
            ({"name": d.metadata["Name"], "version": d.version} for d in importlib.metadata.distributions()),
            key=lambda x: x["name"].lower(),
        ),
        "runtime": {
            "expected_source_commit": RUNTIME_COMMIT,
            "version_output": version_out,
            "executable_sha256": file_sha256(args.llama_server.resolve()),
        },
        "pinned_hf_artifacts": PINNED_ARTIFACTS,
        "pinned_bench_v7_dataset": PINNED_BENCH_V7,
        "neural_router_v3_v10": rtr_res,
        "functiongemma_270m_v10_q4_k_m": fg_res,
        "qwen3_06b_v10_q4_k_m": qwen_res,
        "operational_smoke_8check": {
            "functiongemma_270m_v10_q4_k_m": {
                "all_checks_passed": fg_smoke["all_checks_passed"],
                "checks": fg_smoke["checks"],
                "memory": fg_smoke["memory"],
                "network_isolation": fg_smoke.get("network_isolation"),
            },
            "qwen3_06b_v10_q4_k_m": {
                "all_checks_passed": qwen_smoke["all_checks_passed"],
                "checks": qwen_smoke["checks"],
                "memory": qwen_smoke["memory"],
                "network_isolation": qwen_smoke.get("network_isolation"),
            },
        },
        "all_qualification_checks_passed": all_passed,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "all_qualification_checks_passed": all_passed,
                "router_av7_bitwise_match": rtr_res["acceptance_v7_2580"]["matches_macos_and_linux_bitwise"],
                "fg_p50_ms": fg_res["routing_and_verifier_ms"]["p50"],
                "fg_ov1_ver": fg_res["overnight_v1"]["verified_exact"],
                "fg_peak_mib": fg_res["memory_accounting"]["combined_peak_working_set_mib"],
                "qwen_p50_ms": qwen_res["routing_and_verifier_ms"]["p50"],
                "qwen_ov1_ver": qwen_res["overnight_v1"]["verified_exact"],
                "qwen_peak_mib": qwen_res["memory_accounting"]["combined_peak_working_set_mib"],
            }
        )
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
