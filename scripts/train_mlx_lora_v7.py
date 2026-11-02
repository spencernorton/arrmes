#!/usr/bin/env python3
"""Train FunctionGemma-270M-Arrmes-v7 and Qwen3-0.6B-Arrmes-v7 LoRA checkpoints with MLX."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import time
from typing import Any

import mlx.core as mx
from arrmes.catalog import tools_v2
from arrmes.model import LocalModel, finalize_output, messages
from arrmes.protocol import Call, decode, encode, normalized

ROOT_DIR = Path(__file__).resolve().parents[1]
PREV_DIR = Path("../arrmes-baseline")
PYTHON_BIN = str(PREV_DIR / ".venv" / "bin" / "python")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_mlx_datasets() -> dict[str, Any]:
    rows = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "train-v7" / "train.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    v2_rows = [r for r in rows if r.get("contract_version", "v2") == "v2"]
    synth_rows = [r for r in v2_rows if str(r.get("id", "")).startswith("train-v7-synth-")]
    base_rows = [r for r in v2_rows if not str(r.get("id", "")).startswith("train-v7-synth-")]

    rng = random.Random(20261102)
    rng.shuffle(base_rows)
    curriculum = synth_rows + base_rows[:600]
    rng.shuffle(curriculum)

    split_idx = int(len(curriculum) * 0.92)
    train_split = curriculum[:split_idx]
    valid_split = curriculum[split_idx:]

    tool_defs = tools_v2()
    stats: dict[str, Any] = {}

    for family in ("functiongemma", "hermes"):
        out_dir = ROOT_DIR / "data" / "train-v7" / f"mlx_{family}"
        out_dir.mkdir(parents=True, exist_ok=True)
        for split_name, split_rows in (("train", train_split), ("valid", valid_split)):
            out_path = out_dir / f"{split_name}.jsonl"
            with out_path.open("w", encoding="utf-8") as f:
                for r in split_rows:
                    msgs = list(messages(r["query"], r.get("context"), family=family, contract_version="v2"))
                    target_str = encode(Call(r["expected"]["name"], r["expected"]["arguments"]), family=family)
                    msgs.append({"role": "assistant", "content": target_str})
                    f.write(json.dumps({"messages": msgs, "tools": tool_defs}, ensure_ascii=False) + "\n")
        stats[family] = {
            "train_count": len(train_split),
            "valid_count": len(valid_split),
            "train_sha256": sha256_file(out_dir / "train.jsonl"),
            "valid_sha256": sha256_file(out_dir / "valid.jsonl"),
        }
    return stats


def verify_adapter_delta(old_file: Path, new_file: Path) -> dict[str, Any]:
    old_w = mx.load(str(old_file))
    new_w = mx.load(str(new_file))
    max_delta = 0.0
    mean_deltas = []
    finite_all = True
    for k, v_new in new_w.items():
        if not bool(mx.all(mx.isfinite(v_new)).item()):
            finite_all = False
        if k in old_w and old_w[k].shape == v_new.shape:
            diff = mx.abs(v_new.astype(mx.float32) - old_w[k].astype(mx.float32))
            mx_d = float(mx.max(diff).item())
            mn_d = float(mx.mean(diff).item())
            max_delta = max(max_delta, mx_d)
            mean_deltas.append(mn_d)
    return {
        "num_tensors": len(new_w),
        "all_finite": finite_all,
        "max_abs_delta": round(max_delta, 8),
        "mean_abs_delta": round(sum(mean_deltas) / max(len(mean_deltas), 1), 8),
        "non_zero_update": max_delta > 1e-6,
    }


def train_and_fuse_one(
    name: str,
    family: str,
    base_model: Path,
    resume_adapter: Path,
    adapter_out_dir: Path,
    fused_4bit_dir: Path,
    iters: int,
    lr: float,
    batch_size: int,
    num_layers: int,
    scale: float,
) -> dict[str, Any]:
    adapter_out_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = ROOT_DIR / "configs" / f"{name}.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = ROOT_DIR / "data" / "train-v7" / f"mlx_{family}"

    yaml_content = f"""model: "{base_model}"
train: true
fine_tune_type: lora
data: "{data_dir}"
seed: 20261102
num_layers: {num_layers}
batch_size: {batch_size}
iters: {iters}
val_batches: 4
learning_rate: {lr}
steps_per_report: 30
steps_per_eval: {iters}
resume_adapter_file: "{resume_adapter}"
adapter_path: "{adapter_out_dir}"
save_every: {iters}
test: false
grad_checkpoint: true
mask_prompt: true
max_seq_length: 2560
lora_parameters:
  rank: 8
  dropout: 0.0
  scale: {scale}
"""
    cfg_path.write_text(yaml_content, encoding="utf-8")
    t0 = time.perf_counter()
    subprocess.run(
        [PYTHON_BIN, "-m", "mlx_lm", "lora", "--config", str(cfg_path)],
        check=True,
    )
    train_sec = time.perf_counter() - t0
    new_adapter_file = adapter_out_dir / "adapters.safetensors"
    delta_info = verify_adapter_delta(resume_adapter, new_adapter_file)

    import shutil
    fused_fp16_dir = ROOT_DIR / "models" / f"_tmp_fp16_{name}"
    for d in (fused_fp16_dir, fused_4bit_dir):
        if d.exists():
            shutil.rmtree(d)
    t_fuse = time.perf_counter()
    subprocess.run(
        [
            PYTHON_BIN,
            "-m",
            "mlx_lm",
            "fuse",
            "--model",
            str(base_model),
            "--adapter-path",
            str(adapter_out_dir),
            "--save-path",
            str(fused_fp16_dir),
        ],
        check=True,
    )
    subprocess.run(
        [
            PYTHON_BIN,
            "-m",
            "mlx_lm",
            "convert",
            "--hf-path",
            str(fused_fp16_dir),
            "--mlx-path",
            str(fused_4bit_dir),
            "-q",
            "--q-bits",
            "4",
        ],
        check=True,
    )
    shutil.rmtree(fused_fp16_dir, ignore_errors=True)
    fuse_sec = time.perf_counter() - t_fuse
    total_bytes = sum(p.stat().st_size for p in fused_4bit_dir.rglob("*") if p.is_file())
    return {
        "name": name,
        "family": family,
        "base_model": str(base_model),
        "resume_adapter": str(resume_adapter),
        "resume_adapter_sha256": sha256_file(resume_adapter),
        "adapter_out_dir": str(adapter_out_dir),
        "new_adapter_sha256": sha256_file(new_adapter_file),
        "fused_4bit_dir": str(fused_4bit_dir),
        "fused_4bit_bytes": total_bytes,
        "fused_4bit_mb": round(total_bytes / (1024 * 1024), 2),
        "iters": iters,
        "learning_rate": lr,
        "batch_size": batch_size,
        "num_layers": num_layers,
        "seed": 20261102,
        "train_seconds": round(train_sec, 2),
        "fuse_seconds": round(fuse_sec, 2),
        "delta_verification": delta_info,
    }


def eval_on_overnight_v1(model_path: str, family: str, adapter: str | None = None) -> dict[str, Any]:
    cases = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "overnight-v1" / "routing-heldout.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    m = LocalModel(model_path, family=family, adapter=adapter, contract_version="v2")
    # Warmup
    m.infer(cases[0]["query"], cases[0].get("context"))
    raw_ok = ver_ok = 0
    lats = []
    for c in cases:
        out = m.infer(c["query"], c.get("context"))
        lats.append(out.get("wall_seconds", out["seconds"]) * 1000.0)
        exp = normalized(Call(c["expected"]["name"], c["expected"]["arguments"]))
        try:
            rc = normalized(decode(out["raw_unverified"], family=family))
        except Exception:
            rc = None
        try:
            vc = normalized(decode(out["raw"], family=family))
        except Exception:
            vc = None
        if rc == exp:
            raw_ok += 1
        if vc == exp:
            ver_ok += 1
    lats_s = sorted(lats)
    n = len(cases)
    return {
        "total": n,
        "raw_exact": raw_ok,
        "raw_accuracy": round(raw_ok / n, 4),
        "verified_exact": ver_ok,
        "verified_accuracy": round(ver_ok / n, 4),
        "warm_p50_ms": round(lats_s[n // 2], 2),
        "warm_p95_ms": round(lats_s[int(n * 0.95)], 2),
    }


if __name__ == "__main__":
    print("[mlx-v7] Building MLX JSONL datasets...")
    ds_stats = build_mlx_datasets()
    print("[mlx-v7] Dataset stats:", json.dumps(ds_stats, indent=2))

    fg_meta = train_and_fuse_one(
        name="functiongemma-270m-arrmes-v7",
        family="functiongemma",
        base_model=PREV_DIR / "models" / "functiongemma",
        resume_adapter=PREV_DIR / "adapters" / "functiongemma-distilled-v5" / "adapters.safetensors",
        adapter_out_dir=ROOT_DIR / "adapters" / "functiongemma-arrmes-v7",
        fused_4bit_dir=ROOT_DIR / "models" / "functiongemma-270m-arrmes-v7-4bit",
        iters=80,
        lr=2e-5,
        batch_size=2,
        num_layers=-1,
        scale=16.0,
    )
    print("[mlx-v7] FunctionGemma v7 trained:", json.dumps(fg_meta, indent=2))

    qw_meta = train_and_fuse_one(
        name="qwen3-06b-arrmes-v7",
        family="hermes",
        base_model=PREV_DIR / "models" / "qwen3-06b",
        resume_adapter=PREV_DIR / "adapters" / "qwen3-06b-distilled-v6" / "adapters.safetensors",
        adapter_out_dir=ROOT_DIR / "adapters" / "qwen3-06b-arrmes-v7",
        fused_4bit_dir=ROOT_DIR / "models" / "qwen3-06b-arrmes-v7-4bit",
        iters=80,
        lr=1.5e-5,
        batch_size=1,
        num_layers=14,
        scale=8.0,
    )
    print("[mlx-v7] Qwen3-0.6B v7 trained:", json.dumps(qw_meta, indent=2))

    eval_results = {
        "functiongemma_v5_public_4bit": eval_on_overnight_v1(
            str(PREV_DIR / "models" / "functiongemma-v5-4bit"), "functiongemma"
        ),
        "functiongemma_v7_trained_4bit": eval_on_overnight_v1(
            str(ROOT_DIR / "models" / "functiongemma-270m-arrmes-v7-4bit"), "functiongemma"
        ),
        "qwen3_06b_v7_trained_4bit": eval_on_overnight_v1(
            str(ROOT_DIR / "models" / "qwen3-06b-arrmes-v7-4bit"), "hermes"
        ),
    }
    provenance = {
        "generated_at": "2026-11-02",
        "datasets": ds_stats,
        "checkpoints": {
            "functiongemma_270m_arrmes_v7": fg_meta,
            "qwen3_06b_arrmes_v7": qw_meta,
        },
        "overnight_v1_diagnostic": eval_results,
    }
    out_p = ROOT_DIR / "experiments" / "mlx-lora-v7-provenance.json"
    out_p.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print("[mlx-v7] Saved provenance to", out_p)
    print(json.dumps(eval_results, indent=2))
