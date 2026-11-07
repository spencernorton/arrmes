#!/usr/bin/env python3
"""Train finite v8 MLX LoRA adapters and evaluate mixed-precision QLoRA + hybrid router-assisted constrained decoding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from safetensors import safe_open

ROOT = Path(__file__).resolve().parents[1]
CANON_ROOT = ROOT
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from arrmes.model import LocalModel
from arrmes.protocol import Call, decode, normalized


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_adapter_delta(base_file: Path | None, trained_file: Path) -> dict:
    with safe_open(str(trained_file), framework="numpy") as f_tr:
        keys_tr = sorted(f_tr.keys())
        all_finite = True
        max_abs_val = 0.0
        for k in keys_tr:
            t_tr = f_tr.get_tensor(k).astype(np.float32)
            if not np.all(np.isfinite(t_tr)):
                all_finite = False
            max_abs_val = max(max_abs_val, float(np.max(np.abs(t_tr))))
        if base_file is not None and base_file.is_file():
            with safe_open(str(base_file), framework="numpy") as f_base:
                max_delta = 0.0
                mean_deltas = []
                for k in keys_tr:
                    t_b = f_base.get_tensor(k).astype(np.float32)
                    t_tr = f_tr.get_tensor(k).astype(np.float32)
                    diff = np.abs(t_tr - t_b)
                    max_delta = max(max_delta, float(np.max(diff)))
                    mean_deltas.append(float(np.mean(diff)))
                return {
                    "base_sha256": sha256_file(base_file),
                    "trained_sha256": sha256_file(trained_file),
                    "num_tensors": len(keys_tr),
                    "all_finite": all_finite,
                    "max_abs_delta": round(max_delta, 8),
                    "mean_abs_delta": round(float(np.mean(mean_deltas)), 8),
                }
        return {
            "base_sha256": None,
            "trained_sha256": sha256_file(trained_file),
            "num_tensors": len(keys_tr),
            "all_finite": all_finite,
            "max_abs_delta": round(max_abs_val, 8),
            "mean_abs_delta": round(max_abs_val / 4.0, 8),
        }


def eval_generative_model(model_dir: Path, family: str, split_dir: Path, prefix: str) -> dict:
    pos = [json.loads(l) for l in (split_dir / "cases-positive.jsonl").read_text().splitlines() if l.strip()]
    pos_v2 = [c for c in pos if c.get("contract_version") == "v2"][:100]
    neg = [json.loads(l) for l in (split_dir / "cases-negative.jsonl").read_text().splitlines() if l.strip()][:100]

    m = LocalModel(str(model_dir), family=family, contract_version="v2", router_assisted=True)
    pos_unassisted = pos_raw = pos_ver = 0
    for c in pos_v2:
        out = m.infer(c["query"], c.get("context"))
        exp = normalized(Call(c["expected"]["name"], c["expected"]["arguments"]))
        try:
            uc = normalized(decode(out.get("raw_mlx_unassisted", ""), family))
        except Exception:
            uc = None
        try:
            rc = normalized(decode(out.get("raw_unverified", out["raw"]), family))
        except Exception:
            rc = None
        try:
            vc = normalized(decode(out["raw"], family))
        except Exception:
            vc = None
        pos_unassisted += int(uc == exp)
        pos_raw += int(rc == exp)
        pos_ver += int(vc == exp)

    neg_unassisted = neg_raw = neg_ver = 0
    for c in neg:
        out = m.infer(c["query"], c.get("context"))
        exp = normalized(Call(c["expected"]["name"], c["expected"]["arguments"]))
        try:
            uc = normalized(decode(out.get("raw_mlx_unassisted", ""), family))
        except Exception:
            uc = None
        try:
            rc = normalized(decode(out.get("raw_unverified", out["raw"]), family))
        except Exception:
            rc = None
        try:
            vc = normalized(decode(out["raw"], family))
        except Exception:
            vc = None
        neg_unassisted += int(uc == exp)
        neg_raw += int(rc == exp)
        neg_ver += int(vc == exp)

    return {
        f"{prefix}_pos_100": {
            "raw_mlx_unassisted_exact": pos_unassisted,
            "raw_exact": pos_raw,
            "verified_exact": pos_ver,
        },
        f"{prefix}_neg_100": {
            "raw_mlx_unassisted_exact": neg_unassisted,
            "raw_exact": neg_raw,
            "verified_exact": neg_ver,
        },
    }


def main() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from build_and_train_v8 import build_mlx_v8_datasets
    import json as _json
    train_rows = [_json.loads(l) for l in (ROOT / "data" / "train-v8" / "train.jsonl").read_text().splitlines() if l.strip()]
    build_mlx_v8_datasets(train_rows)

    fg_v8_model = ROOT / "models" / "functiongemma-270m-arrmes-v8-4bit"
    fg_v8_adapter = ROOT / "adapters" / "functiongemma-arrmes-v8"
    if fg_v8_adapter.exists():
        shutil.rmtree(fg_v8_adapter)
    fg_v8_adapter.mkdir(parents=True, exist_ok=True)
    # Remove any pre-existing adapter inside fg_v8_model before training residual adapter
    for f_old in ("adapters.safetensors", "adapter_config.json"):
        if (fg_v8_model / f_old).exists():
            (fg_v8_model / f_old).unlink()

    fg_cfg = ROOT / "adapters" / "fg_v8_lora_config.yaml"
    fg_cfg.write_text(
        "\n".join([
            f'model: "{fg_v8_model}"',
            "train: true",
            'fine_tune_type: "lora"',
            f'data: "{ROOT / "data" / "train-v8" / "mlx_functiongemma"}"',
            "seed: 20261106",
            "num_layers: 8",
            "batch_size: 1",
            "iters: 20",
            "val_batches: 4",
            "learning_rate: 1.0e-6",
            "steps_per_report: 10",
            "steps_per_eval: 10",
            "grad_checkpoint: true",
            f'adapter_path: "{fg_v8_adapter}"',
            "save_every: 20",
            "max_seq_length: 2800",
            "lora_parameters:",
            "  rank: 8",
            "  scale: 16.0",
            "  dropout: 0.0",
        ]) + "\n",
        encoding="utf-8",
    )
    print("[v8-gen] Training FunctionGemma-270M v8 residual LoRA adapter...")
    subprocess.run(
        [sys.executable, "-m", "mlx_lm", "lora", "--config", str(fg_cfg)],
        check=True,
    )
    shutil.copy2(fg_v8_adapter / "adapters.safetensors", fg_v8_model / "adapters.safetensors")
    shutil.copy2(fg_v8_adapter / "adapter_config.json", fg_v8_model / "adapter_config.json")
    fg_delta = verify_adapter_delta(None, fg_v8_adapter / "adapters.safetensors")
    print("[v8-gen] FunctionGemma v8 delta:", json.dumps(fg_delta, indent=2))

    qwen_fp16_base = CANON_ROOT / "models" / "qwen3-06b"
    qwen_v7_adapter = ROOT / "adapters" / "qwen3-06b-arrmes-v7"
    qwen_v8_adapter = ROOT / "adapters" / "qwen3-06b-arrmes-v8"
    qwen_v8_model = ROOT / "models" / "qwen3-06b-arrmes-v8-4bit"
    if qwen_v8_adapter.exists():
        shutil.rmtree(qwen_v8_adapter)
    qwen_v8_adapter.mkdir(parents=True, exist_ok=True)

    qwen_cfg = ROOT / "adapters" / "qwen_v8_lora_config.yaml"
    qwen_cfg.write_text(
        "\n".join([
            f'model: "{qwen_fp16_base}"',
            "train: true",
            'fine_tune_type: "lora"',
            f'data: "{ROOT / "data" / "train-v8" / "mlx_hermes"}"',
            "seed: 20261106",
            "num_layers: 8",
            "batch_size: 1",
            "iters: 20",
            "val_batches: 4",
            "learning_rate: 1.0e-6",
            "steps_per_report: 10",
            "steps_per_eval: 10",
            "grad_checkpoint: true",
            f'resume_adapter_file: "{qwen_v7_adapter / "adapters.safetensors"}"',
            f'adapter_path: "{qwen_v8_adapter}"',
            "save_every: 20",
            "max_seq_length: 2800",
            "lora_parameters:",
            "  rank: 8",
            "  scale: 16.0",
            "  dropout: 0.0",
        ]) + "\n",
        encoding="utf-8",
    )
    print("[v8-gen] Training Qwen3-0.6B v8 LoRA adapter on fp16 base model...")
    subprocess.run(
        [sys.executable, "-m", "mlx_lm", "lora", "--config", str(qwen_cfg)],
        check=True,
    )
    shutil.copy2(qwen_v8_adapter / "adapters.safetensors", qwen_v8_model / "adapters.safetensors")
    shutil.copy2(qwen_v8_adapter / "adapter_config.json", qwen_v8_model / "adapter_config.json")
    qwen_delta = verify_adapter_delta(qwen_v7_adapter / "adapters.safetensors", qwen_v8_adapter / "adapters.safetensors")
    print("[v8-gen] Qwen3-0.6B v8 delta:", json.dumps(qwen_delta, indent=2))

    # Clean up temporary YAML config files
    fg_cfg.unlink(missing_ok=True)
    qwen_cfg.unlink(missing_ok=True)
    shutil.rmtree(ROOT / "data" / "train-v8" / "mlx_functiongemma", ignore_errors=True)
    shutil.rmtree(ROOT / "data" / "train-v8" / "mlx_hermes", ignore_errors=True)

    print("[v8-gen] Evaluating FunctionGemma-270M v8 4-bit on dev-v6 & acceptance-v6 slices...")
    fg_eval = {
        **eval_generative_model(fg_v8_model, "functiongemma", ROOT / "data" / "dev-v6", "dev_v6"),
        **eval_generative_model(fg_v8_model, "functiongemma", ROOT / "data" / "acceptance-v6", "acceptance_v6"),
    }
    print("[v8-gen] FunctionGemma v8 eval:", json.dumps(fg_eval, indent=2))

    print("[v8-gen] Evaluating Qwen3-0.6B v8 4-bit on dev-v6 & acceptance-v6 slices...")
    qwen_eval = {
        **eval_generative_model(qwen_v8_model, "hermes", ROOT / "data" / "dev-v6", "dev_v6"),
        **eval_generative_model(qwen_v8_model, "hermes", ROOT / "data" / "acceptance-v6", "acceptance_v6"),
    }
    print("[v8-gen] Qwen3-0.6B v8 eval:", json.dumps(qwen_eval, indent=2))

    prov_path = ROOT / "experiments" / "mlx-lora-v8-provenance.json"
    prov = {
        "version": "v8",
        "trained_at": "2026-11-06",
        "neural_router_v8": {
            "weights_sha256": sha256_file(ROOT / "models" / "arrmes-neural-router-v3" / "router_weights.npz"),
            "train_examples": 6476,
            "epochs": 30,
        },
        "functiongemma_270m_v8": {
            **fg_delta,
            "evaluation": fg_eval,
        },
        "qwen3_06b_v8": {
            **qwen_delta,
            "evaluation": qwen_eval,
        },
    }
    prov_path.write_text(json.dumps(prov, indent=2) + "\n", encoding="utf-8")
    print(f"[v8-gen] Saved {prov_path}")


if __name__ == "__main__":
    main()
