#!/usr/bin/env python3
"""Export v10 merged BF16 checkpoints to Q8_0 and Q4_K_M GGUF and profile CPU/CUDA runtimes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
for _sp in (ROOT_DIR / "src", ROOT_DIR / "scripts"):
    if str(_sp) not in sys.path:
        sys.path.insert(0, str(_sp))

from arrmes.backends import LlamaServerModel
from arrmes.evaluate import score


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def proc_vmhwm_mib(pid: int) -> float | None:
    status_p = Path(f"/proc/{pid}/status")
    if not status_p.exists():
        return None
    for line in status_p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("VmHWM:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return round(int(parts[1]) / 1024.0, 2)
    return None


def wait_for_gpu_idle(poll_s: int = 10) -> str:
    import os
    my_pid = os.getpid()
    while True:
        free_res = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True, text=True, check=False)
        free_mib = int(free_res.stdout.strip().splitlines()[0].strip()) if free_res.stdout.strip() else 0
        pmon = subprocess.run(["nvidia-smi", "pmon", "-c", "3", "-s", "u"], capture_output=True, text=True, check=False)
        busy = []
        for line in pmon.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4 and parts[1].isdigit() and int(parts[1]) != my_pid and parts[3].isdigit() and int(parts[3]) > 5:
                busy.append(int(parts[1]))
        if not busy and free_mib >= 4096:
            print(pmon.stdout, flush=True)
            return pmon.stdout
        print(f"[gpu-gate] Waiting {poll_s}s (busy={busy}, free_mib={free_mib})...", flush=True)
        time.sleep(poll_s)


def export_gguf_pair(
    merged_dir: Path,
    out_dir: Path,
    stem: str,
    convert_script: Path,
    quantize_bin: Path,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    f16_path = out_dir / f"{stem}-f16.gguf"
    q8_path = out_dir / f"{stem}-q8_0.gguf"
    q4_path = out_dir / f"{stem}-q4_k_m.gguf"

    merged_st = merged_dir / "model.safetensors"
    need_build = not (
        q8_path.is_file()
        and q4_path.is_file()
        and merged_st.is_file()
        and q8_path.stat().st_mtime >= merged_st.stat().st_mtime
        and q4_path.stat().st_mtime >= merged_st.stat().st_mtime
    )
    if need_build:
        import shutil
        stage_dir = merged_dir.parent / f"_gguf_stage_{stem}"
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True)
        try:
            for item in merged_dir.iterdir():
                if item.name.endswith(".safetensors"):
                    (stage_dir / item.name).symlink_to(item.resolve())
                elif item.is_file():
                    shutil.copy2(item, stage_dir / item.name)
            cfg_data = json.loads((stage_dir / "config.json").read_text(encoding="utf-8"))
            vsize = cfg_data.get("vocab_size")
            if isinstance(vsize, int) and cfg_data.get("model_type") == "gemma3_text":
                tj_p = stage_dir / "tokenizer.json"
                if tj_p.is_file():
                    tj = json.loads(tj_p.read_text(encoding="utf-8"))
                    tj["added_tokens"] = [t for t in tj.get("added_tokens", []) if t.get("id", 0) < vsize]
                    tj_p.write_text(json.dumps(tj), encoding="utf-8")
                tc_p = stage_dir / "tokenizer_config.json"
                if tc_p.is_file():
                    tc = json.loads(tc_p.read_text(encoding="utf-8"))
                    for k_rm in ("boi_token", "eoi_token", "image_token"):
                        tc.pop(k_rm, None)
                    if isinstance(tc.get("model_specific_special_tokens"), dict):
                        for k_rm in ("boi_token", "eoi_token", "image_token"):
                            tc["model_specific_special_tokens"].pop(k_rm, None)
                    if isinstance(tc.get("added_tokens_decoder"), dict):
                        tc["added_tokens_decoder"] = {k: v for k, v in tc["added_tokens_decoder"].items() if int(k) < vsize}
                    tc["tokenizer_class"] = "PreTrainedTokenizerFast"
                    tc_p.write_text(json.dumps(tc, indent=2), encoding="utf-8")

            print(f"[gguf] Converting {stage_dir.name} -> {f16_path}...", flush=True)
            subprocess.run(
                [sys.executable, str(convert_script), stage_dir.name, "--model-name", stem, "--outfile", str(f16_path), "--outtype", "f16"],
                cwd=str(stage_dir.parent),
                check=True,
            )
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

        for qtype, qpath in [("Q8_0", q8_path), ("Q4_K_M", q4_path)]:
            print(f"[gguf] Quantizing {qpath.name} ({qtype})...", flush=True)
            subprocess.run([str(quantize_bin), str(f16_path), str(qpath), qtype], check=True)
            qpath.chmod(0o644)
        f16_path.unlink(missing_ok=True)
    else:
        print(f"[gguf] Reusing up-to-date {q8_path.name} & {q4_path.name}", flush=True)

    res: dict[str, Any] = {}
    from audit_gguf_release import audit as audit_gguf
    for qkey, qpath in [("q8_0", q8_path), ("q4_k_m", q4_path)]:
        aud_report, _ = audit_gguf(qpath)
        f_list = aud_report["plaintext_scan"]["findings"]
        assert len(f_list) == 0 and aud_report["release_candidate_checks_passed"], (
            f"GGUF privacy audit failed on {qpath.name}: {f_list}"
        )
        sz = qpath.stat().st_size
        res[qkey] = {
            "gguf_audit_finding_count": len(f_list),
            "filename": qpath.name,
            "bytes": sz,
            "mb_decimal": round(sz / 1_000_000.0, 2),
            "mib": round(sz / (1024 * 1024), 2),
            "meets_400mb_budget": sz <= 400_000_000,
            "meets_250mb_stretch": sz <= 250_000_000,
            "sha256": sha256_file(qpath),
        }
    return res


def profile_gguf_runtime(
    gguf_path: Path,
    family: str,
    device: str,
    executable: str,
    ov1_rows: list[dict[str, Any]],
    *,
    threads: int | None = None,
) -> dict[str, Any]:
    rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    py_rss_mib = round(rss_raw / (1024.0 * 1024.0) if platform.system() == 'Darwin' else rss_raw / 1024.0, 2)
    t_cold = time.perf_counter()
    with LlamaServerModel(
        str(gguf_path),
        family=family,
        contract_version="v2",
        device=device,
        executable=executable,
        context_size=4096,
        threads=threads,
    ) as model:
        load_ms = round(model.load_seconds * 1000.0, 2)
        t_first = time.perf_counter()
        _ = model.infer(ov1_rows[0]["query"], ov1_rows[0].get("context"))
        first_route_ms = round((time.perf_counter() - t_first) * 1000.0, 2)
        cold_plus_first_ms = round((time.perf_counter() - t_cold) * 1000.0, 2)

        # Evaluate full overnight-v1 (N=80) and collect warm routing + verifier wall latencies
        wall_ms_list: list[float] = []
        gen_ms_list: list[float] = []
        tps_list: list[float] = []
        raw_ok = 0
        ver_ok = 0
        raw_outputs: list[str] = []
        for idx, c in enumerate(ov1_rows):
            inf = model.infer(c["query"], c.get("context"))
            if idx >= 3:
                wall_ms_list.append(inf["wall_seconds"] * 1000.0)
                gen_ms_list.append(inf["seconds"] * 1000.0)
                if isinstance(inf.get("generation_tps"), (int, float)):
                    tps_list.append(float(inf["generation_tps"]))
            raw_ok += int(score(inf["raw_unverified"], c["expected"], family, allow_v3=False)["exact"])
            ver_ok += int(score(inf["raw"], c["expected"], family, allow_v3=False)["exact"])
            raw_outputs.append(inf["raw"])

        srv_rss = proc_vmhwm_mib(model._process.pid) if model._process is not None else None

    sz = gguf_path.stat().st_size
    comb_mib = round((srv_rss or 0.0) + py_rss_mib, 2) if srv_rss is not None else None
    return {
        "gguf_file": gguf_path.name,
        "family": family,
        "device": device,
        "threads": threads,
        "uname": platform.platform(),
        "artifact_bytes": sz,
        "artifact_mb_decimal": round(sz / 1_000_000.0, 2),
        "artifact_mib": round(sz / (1024 * 1024), 2),
        "meets_400mb_budget": sz <= 400_000_000,
        "meets_250mb_stretch": sz <= 250_000_000,
        "cold_load_ms": load_ms,
        "first_route_ms": first_route_ms,
        "cold_load_plus_first_route_ms": cold_plus_first_ms,
        "warm_samples": len(wall_ms_list),
        "model_only_ms": {
            "p50": round(statistics.median(gen_ms_list), 2),
            "p95": round(sorted(gen_ms_list)[int(0.95 * len(gen_ms_list))], 2),
            "mean": round(statistics.mean(gen_ms_list), 2),
        },
        "routing_and_verifier_ms": {
            "p50": round(statistics.median(wall_ms_list), 2),
            "p95": round(sorted(wall_ms_list)[int(0.95 * len(wall_ms_list))], 2),
            "mean": round(statistics.mean(wall_ms_list), 2),
        },
        "mean_generation_tps": round(statistics.mean(tps_list), 2) if tps_list else None,
        "overnight_v1_80": {
            "raw_exact": raw_ok,
            "raw_accuracy": round(raw_ok / len(ov1_rows), 6),
            "verified_exact": ver_ok,
            "verified_accuracy": round(ver_ok / len(ov1_rows), 6),
            "verified_outputs_sha256": hashlib.sha256("\n".join(raw_outputs).encode()).hexdigest(),
        },
        "memory_accounting_mib": {
            "llama_server_peak_rss_mib": srv_rss,
            "python_app_rss_mib": py_rss_mib,
            "combined_resident_mib": comb_mib,
            "meets_1536mib_budget": (comb_mib <= 1536.0) if comb_mib is not None else True,
            "meets_1500mib_budget": (comb_mib <= 1500.0) if comb_mib is not None else True,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", type=Path, default=ROOT_DIR)
    ap.add_argument("--convert-script", type=Path, required=True)
    ap.add_argument("--quantize-bin", type=Path, required=True)
    ap.add_argument("--cpu-server-bin", type=str, required=True)
    ap.add_argument("--cuda-server-bin", type=str, required=True)
    ap.add_argument("--cpu-threads", type=int, default=8)
    args = ap.parse_args()

    wdir = args.work_dir.resolve()
    gguf_dir = wdir / "models" / "gguf-v10"
    ov1_rows = [json.loads(l) for l in (wdir / "data/overnight-v1/routing-heldout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    artifacts = {
        "qwen3_06b_v10": export_gguf_pair(
            wdir / "models/arrmes-qwen3-0.6b-v10-merged",
            gguf_dir,
            "arrmes-qwen3-0.6b-v10",
            args.convert_script,
            args.quantize_bin,
        ),
        "functiongemma_270m_v10": export_gguf_pair(
            wdir / "models/arrmes-functiongemma-270m-v10-merged",
            gguf_dir,
            "arrmes-functiongemma-270m-v10",
            args.convert_script,
            args.quantize_bin,
        ),
    }

    profiles: dict[str, Any] = {}
    # 1. CPU profiles (8 physical threads on single CCD)
    for key, stem, fam in [
        ("qwen3_06b_v10_gguf_q4_k_m_linux_x86_64_cpu", "arrmes-qwen3-0.6b-v10-q4_k_m.gguf", "hermes"),
        ("qwen3_06b_v10_gguf_q8_0_linux_x86_64_cpu", "arrmes-qwen3-0.6b-v10-q8_0.gguf", "hermes"),
        ("functiongemma_270m_v10_gguf_q4_k_m_linux_x86_64_cpu", "arrmes-functiongemma-270m-v10-q4_k_m.gguf", "functiongemma"),
        ("functiongemma_270m_v10_gguf_q8_0_linux_x86_64_cpu", "arrmes-functiongemma-270m-v10-q8_0.gguf", "functiongemma"),
    ]:
        print(f"[profile-cpu] {key}...", flush=True)
        profiles[key] = profile_gguf_runtime(
            gguf_dir / stem,
            fam,
            "cpu",
            args.cpu_server_bin,
            ov1_rows,
            threads=args.cpu_threads,
        )
        print(f"  -> p50={profiles[key]['routing_and_verifier_ms']['p50']}ms ov1_ver={profiles[key]['overnight_v1_80']['verified_accuracy']}", flush=True)

    # 2. CUDA profiles (polite GPU check first)
    wait_for_gpu_idle()
    for key, stem, fam in [
        ("qwen3_06b_v10_gguf_q4_k_m_linux_cuda", "arrmes-qwen3-0.6b-v10-q4_k_m.gguf", "hermes"),
        ("qwen3_06b_v10_gguf_q8_0_linux_cuda", "arrmes-qwen3-0.6b-v10-q8_0.gguf", "hermes"),
        ("functiongemma_270m_v10_gguf_q4_k_m_linux_cuda", "arrmes-functiongemma-270m-v10-q4_k_m.gguf", "functiongemma"),
        ("functiongemma_270m_v10_gguf_q8_0_linux_cuda", "arrmes-functiongemma-270m-v10-q8_0.gguf", "functiongemma"),
    ]:
        print(f"[profile-cuda] {key}...", flush=True)
        profiles[key] = profile_gguf_runtime(
            gguf_dir / stem,
            fam,
            "cuda",
            args.cuda_server_bin,
            ov1_rows,
        )
        print(f"  -> p50={profiles[key]['routing_and_verifier_ms']['p50']}ms ov1_ver={profiles[key]['overnight_v1_80']['verified_accuracy']}", flush=True)

    out_receipt = {"artifacts": artifacts, "profiles": profiles}
    out_p = wdir / "v10_gguf_linux_receipt.json"
    out_p.write_text(json.dumps(out_receipt, indent=2) + "\n", encoding="utf-8")
    print("[DONE] Wrote", out_p, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
