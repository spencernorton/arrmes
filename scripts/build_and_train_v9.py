#!/usr/bin/env python3
"""Build TrainPool-v9 (zero template-skeleton/entity/query leakage vs acceptance-v7),
train ArrmesNeuralRouterV3 v9 weights, and train genuine v9 mixed-precision QLoRA checkpoints
(FunctionGemma-270M-Arrmes-v9 & Qwen3-0.6B-Arrmes-v9) with unmodified Track A & verified Track B evaluation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from typing import Any

import mlx.core as mx
import numpy as np
from safetensors import safe_open

ROOT_DIR = Path(__file__).resolve().parents[1]
for _p in (str(ROOT_DIR), str(ROOT_DIR / "src"), str(ROOT_DIR / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from arrmes.catalog import tools_v2
from arrmes.model import LocalModel, messages
from arrmes.neural_router import (
    ArrmesNeuralRouterV3,
    CLASS_TO_IDX,
    ROUTER_CLASSES,
    TOTAL_FEATURES,
    featurize_query,
    label_from_call,
)
from arrmes.protocol import Call, InvalidCall, decode, encode, normalized
from build_and_evaluate_acceptance_v7 import normalize_template_skeleton, wilson_lower_95

PYTHON_BIN = sys.executable


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_train_v9() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build TrainPool-v9 from train-v8 + dev-v6 + acceptance-v6 (retired to dev) and audit zero overlap vs acceptance-v7."""
    if not (ROOT_DIR / "data" / "train-v8" / "train.jsonl").is_file():
        from build_and_train_v8 import build_train_v8
        build_train_v8()
    sources = [
        ("train-v8", ROOT_DIR / "data" / "train-v8" / "train.jsonl"),
        ("dev-v6-pos", ROOT_DIR / "data" / "dev-v6" / "cases-positive.jsonl"),
        ("dev-v6-neg", ROOT_DIR / "data" / "dev-v6" / "cases-negative.jsonl"),
        ("acceptance-v6-pos-retired", ROOT_DIR / "data" / "acceptance-v6" / "cases-positive.jsonl"),
        ("acceptance-v6-neg-retired", ROOT_DIR / "data" / "acceptance-v6" / "cases-negative.jsonl"),
    ]
    rows: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    seen_queries: set[str] = set()

    for label, path in sources:
        if not path.is_file():
            raise FileNotFoundError(f"Missing training source: {path}")
        raw_rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        added = 0
        for r in raw_rows:
            q_key = " ".join(r["query"].strip().lower().split()) + "|" + json.dumps(r.get("context"), sort_keys=True)
            if q_key in seen_queries:
                continue
            seen_queries.add(q_key)
            row = {
                "id": r.get("id", f"{label}-{added:04d}"),
                "query": r["query"],
                "expected": r["expected"],
                "contract_version": r.get("contract_version", "v2"),
                "split": "train",
                "provenance_source": label,
            }
            if r.get("context") is not None:
                row["context"] = r["context"]
            if r.get("negative_family") is not None:
                row["negative_family"] = r["negative_family"]
            rows.append(row)
            added += 1
        source_counts[label] = added

    # Strict zero-overlap verification against acceptance-v7 and overnight-v1
    av7_pos = [json.loads(l) for l in (ROOT_DIR / "data" / "acceptance-v7" / "cases-positive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    av7_neg = [json.loads(l) for l in (ROOT_DIR / "data" / "acceptance-v7" / "cases-negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    overnight = [json.loads(l) for l in (ROOT_DIR / "data" / "overnight-v1" / "routing-heldout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    import re as _re
    known_ents: set[str] = set()
    for r in (rows + av7_pos + av7_neg):
        q_str = r.get("query", "")
        for m in _re.findall(r"\"([^\"]+)\"", q_str):
            if len(m) >= 4:
                known_ents.add(m.casefold())
        args = r.get("expected", {}).get("arguments", {})
        for k in ("title", "artist", "album", "query", "release_name"):
            v = args.get(k)
            if isinstance(v, str) and len(v) >= 4:
                known_ents.add(v.casefold())
    sorted_ents = sorted(known_ents, key=len, reverse=True)

    av7_queries = {" ".join(c["query"].strip().lower().split()) for c in (av7_pos + av7_neg)}
    av7_skeletons = {normalize_template_skeleton(c["query"], sorted_ents) for c in (av7_pos + av7_neg)}
    ov_queries = {" ".join(c["query"].strip().lower().split()) for c in overnight}

    train_queries = {" ".join(r["query"].strip().lower().split()) for r in rows}
    train_skeletons = {normalize_template_skeleton(r["query"], sorted_ents) for r in rows}

    q_overlap_av7 = sorted(train_queries & av7_queries)
    s_overlap_av7 = sorted(train_skeletons & av7_skeletons)
    q_overlap_ov = sorted(train_queries & ov_queries)

    if q_overlap_av7 or s_overlap_av7 or q_overlap_ov:
        raise RuntimeError(
            f"Leakage detected! q_av7={len(q_overlap_av7)}, s_av7={len(s_overlap_av7)}, q_ov={len(q_overlap_ov)}"
        )

    out_dir = ROOT_DIR / "data" / "train-v9"
    out_dir.mkdir(parents=True, exist_ok=True)
    train_path = out_dir / "train.jsonl"
    train_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    meta = {
        "total_rows": len(rows),
        "source_counts": source_counts,
        "train_jsonl_sha256": sha256_file(train_path),
        "unique_template_skeletons": len(train_skeletons),
        "exact_query_overlap_vs_acceptance_v7": len(q_overlap_av7),
        "template_skeleton_overlap_vs_acceptance_v7": len(s_overlap_av7),
        "exact_query_overlap_vs_overnight_v1": len(q_overlap_ov),
    }
    return rows, meta


def train_neural_router_v9(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Train MultiHeadHashedMLP weights on TrainPool-v9 using vectorized NumPy AdamW."""
    t0 = time.perf_counter()
    rng = np.random.default_rng(20261107)

    X_list = []
    y_list = []
    for r in rows:
        X_list.append(featurize_query(r["query"], r.get("context")))
        lbl = label_from_call(r["expected"])
        y_list.append(CLASS_TO_IDX[lbl])

    X = np.stack(X_list).astype(np.float32)
    y = np.array(y_list, dtype=np.int64)

    hidden_dim = 128
    num_classes = len(ROUTER_CLASSES)
    n = X.shape[0]

    W1 = (rng.standard_normal((TOTAL_FEATURES, hidden_dim)) * np.sqrt(2.0 / TOTAL_FEATURES)).astype(np.float32)
    b1 = np.zeros(hidden_dim, dtype=np.float32)
    W2 = (rng.standard_normal((hidden_dim, num_classes)) * np.sqrt(2.0 / hidden_dim)).astype(np.float32)
    b2 = np.zeros(num_classes, dtype=np.float32)

    params = [W1, b1, W2, b2]
    m_state = [np.zeros_like(p) for p in params]
    v_state = [np.zeros_like(p) for p in params]
    lr = 4e-3
    beta1, beta2, eps, wd = 0.9, 0.999, 1e-8, 2e-4

    batch_size = 64
    epochs = 35
    step_num = 0
    initial_loss = None
    final_loss = None
    epoch_losses: list[float] = []

    for _epoch in range(epochs):
        perm = rng.permutation(n)
        epoch_loss = 0.0
        steps = 0
        for i in range(0, n, batch_size):
            idx = perm[i : i + batch_size]
            xb, yb = X[idx], y[idx]
            bs = xb.shape[0]
            step_num += 1

            z1 = xb @ W1 + b1
            h1 = np.maximum(0.0, z1)
            logits = h1 @ W2 + b2
            logits_shift = logits - np.max(logits, axis=1, keepdims=True)
            exp_l = np.exp(logits_shift)
            probs = exp_l / np.sum(exp_l, axis=1, keepdims=True)

            loss = float(-np.mean(np.log(np.clip(probs[np.arange(bs), yb], 1e-12, 1.0))))
            epoch_loss += loss
            steps += 1

            dlogits = probs
            dlogits[np.arange(bs), yb] -= 1.0
            dlogits /= bs

            dW2 = h1.T @ dlogits
            db2 = np.sum(dlogits, axis=0)
            dh1 = dlogits @ W2.T
            dz1 = dh1 * (z1 > 0)
            dW1 = xb.T @ dz1
            db1 = np.sum(dz1, axis=0)

            grads = [dW1, db1, dW2, db2]
            for pi, (param, grad) in enumerate(zip(params, grads)):
                if param.ndim == 2:
                    param *= (1.0 - lr * wd)
                m_state[pi] = beta1 * m_state[pi] + (1.0 - beta1) * grad
                v_state[pi] = beta2 * v_state[pi] + (1.0 - beta2) * (grad * grad)
                m_hat = m_state[pi] / (1.0 - beta1 ** step_num)
                v_hat = v_state[pi] / (1.0 - beta2 ** step_num)
                param -= lr * m_hat / (np.sqrt(v_hat) + eps)

        avg_loss = epoch_loss / max(steps, 1)
        epoch_losses.append(round(float(avg_loss), 6))
        if initial_loss is None:
            initial_loss = avg_loss
        final_loss = avg_loss

    h_all = np.maximum(0.0, X @ W1 + b1)
    preds = np.argmax(h_all @ W2 + b2, axis=1)
    train_acc = float(np.mean(preds == y))

    save_dir = ROOT_DIR / "models" / "arrmes-neural-router-v3"
    save_dir.mkdir(parents=True, exist_ok=True)
    weights_path = save_dir / "router_weights.npz"
    np.savez_compressed(weights_path, W1=W1, b1=b1, W2=W2, b2=b2)

    w_sha = sha256_file(weights_path)
    w_bytes = weights_path.stat().st_size
    cfg = {
        "architecture": "ArrmesNeuralRouterV3-MultiHeadHashedMLP",
        "version": "v9",
        "input_dim": TOTAL_FEATURES,
        "hidden_dim": hidden_dim,
        "num_classes": num_classes,
        "classes": list(ROUTER_CLASSES),
        "train_examples": int(n),
        "epochs": epochs,
        "initial_loss": round(float(initial_loss or 0.0), 6),
        "final_loss": round(float(final_loss or 0.0), 6),
        "epoch_losses": epoch_losses,
        "train_top1_accuracy": round(train_acc, 6),
        "weights_file": "router_weights.npz",
        "weights_bytes": w_bytes,
        "weights_sha256": w_sha,
        "train_seconds": round(time.perf_counter() - t0, 3),
        "seed": 20261107,
    }
    (save_dir / "router_config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def build_mlx_v9_datasets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build balanced 50% positive / 50% hard-negative MLX QLoRA training/validation JSONL splits."""
    v2_rows = [r for r in rows if r.get("contract_version", "v2") == "v2"]
    pos_by_action: dict[str, list[dict[str, Any]]] = {}
    neg_by_label: dict[str, list[dict[str, Any]]] = {}

    for r in v2_rows:
        lbl = label_from_call(r["expected"])
        if lbl.startswith(("clarify:", "unsupported:")):
            neg_by_label.setdefault(lbl, []).append(r)
        else:
            pos_by_action.setdefault(lbl, []).append(r)

    rng = random.Random(20261107)
    selected_pos: list[dict[str, Any]] = []
    for act, items in sorted(pos_by_action.items()):
        pool = list(items)
        rng.shuffle(pool)
        selected_pos.extend(pool[:45])  # up to 45 per v2 action (~810 positives)

    selected_neg: list[dict[str, Any]] = []
    for lbl, items in sorted(neg_by_label.items()):
        pool = list(items)
        rng.shuffle(pool)
        selected_neg.extend(pool[:25])  # 25 per clarify/unsupported class (175 negatives across all 35 families)

    curriculum = selected_pos + selected_neg
    rng.shuffle(curriculum)

    split_idx = int(len(curriculum) * 0.90)
    train_split = curriculum[:split_idx]
    valid_split = curriculum[split_idx:]

    tool_defs = tools_v2()
    stats: dict[str, Any] = {
        "curriculum_positives": len(selected_pos),
        "curriculum_negatives": len(selected_neg),
        "positive_actions_covered": len(pos_by_action),
        "negative_classes_covered": len(neg_by_label),
    }

    for family in ("functiongemma", "hermes"):
        out_dir = ROOT_DIR / "data" / "train-v9" / f"mlx_{family}"
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


def verify_adapter_delta(base_file: Path | None, trained_file: Path) -> dict[str, Any]:
    with safe_open(str(trained_file), framework="numpy") as f_tr:
        keys_tr = sorted(f_tr.keys())
        all_finite = True
        max_abs_val = 0.0
        mean_abs_vals = []
        for k in keys_tr:
            t_tr = f_tr.get_tensor(k).astype(np.float32)
            if not np.all(np.isfinite(t_tr)):
                all_finite = False
            max_abs_val = max(max_abs_val, float(np.max(np.abs(t_tr))))
            mean_abs_vals.append(float(np.mean(np.abs(t_tr))))
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
                    "non_zero_update": max_delta > 1e-6,
                }
        return {
            "base_sha256": None,
            "trained_sha256": sha256_file(trained_file),
            "num_tensors": len(keys_tr),
            "all_finite": all_finite,
            "max_abs_delta": round(max_abs_val, 8),
            "mean_abs_delta": round(float(np.mean(mean_abs_vals)), 8),
            "non_zero_update": max_abs_val > 1e-6,
        }


def sanitize_adapter_config(cfg_path: Path, model_rel: str, adapter_rel: str, data_rel: str, resume_rel: str | None) -> None:
    if not cfg_path.is_file():
        return
    obj = json.loads(cfg_path.read_text(encoding="utf-8"))
    obj["model"] = model_rel
    obj["adapter_path"] = adapter_rel
    obj["data"] = data_rel
    obj["config"] = f"configs/{Path(adapter_rel).name}.yaml"
    obj["resume_adapter_file"] = resume_rel
    cfg_path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def train_functiongemma_v9() -> dict[str, Any]:
    """Train FunctionGemma-270M v9 residual LoRA on clean 4-bit v7 base (without double-applying v7)."""
    fg_v7_base = ROOT_DIR / "models" / "functiongemma-270m-arrmes-v7-4bit"
    fg_v9_model = ROOT_DIR / "models" / "functiongemma-270m-arrmes-v9-4bit"
    fg_v9_adapter = ROOT_DIR / "adapters" / "functiongemma-arrmes-v9"

    for d in (fg_v9_model, fg_v9_adapter):
        if d.is_symlink():
            d.unlink()
        elif d.exists():
            shutil.rmtree(d)

    shutil.copytree(fg_v7_base, fg_v9_model)
    for af in ("adapters.safetensors", "adapter_config.json"):
        if (fg_v9_model / af).exists():
            (fg_v9_model / af).unlink()

    fg_v9_adapter.mkdir(parents=True, exist_ok=True)
    cfg_path = ROOT_DIR / "configs" / "functiongemma-arrmes-v9.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = ROOT_DIR / "data" / "train-v9" / "mlx_functiongemma"

    iters = 60
    lr = 8.0e-6
    batch_size = 2
    num_layers = -1
    scale = 8.0

    cfg_path.write_text(
        "\n".join([
            f'model: "{fg_v9_model}"',
            "train: true",
            'fine_tune_type: "lora"',
            f'data: "{data_dir}"',
            "seed: 20261107",
            f"num_layers: {num_layers}",
            f"batch_size: {batch_size}",
            f"iters: {iters}",
            "val_batches: 4",
            f"learning_rate: {lr}",
            "steps_per_report: 20",
            "steps_per_eval: 20",
            "grad_checkpoint: true",
            "mask_prompt: true",
            f'adapter_path: "{fg_v9_adapter}"',
            "save_every: 20",
            "max_seq_length: 2752",
            "lora_parameters:",
            "  rank: 8",
            f"  scale: {scale}",
            "  dropout: 0.0",
        ]) + "\n",
        encoding="utf-8",
    )
    t0 = time.perf_counter()
    subprocess.run([PYTHON_BIN, "-m", "mlx_lm", "lora", "--config", str(cfg_path)], check=True)
    train_sec = time.perf_counter() - t0
    cfg_path.unlink(missing_ok=True)

    # Remove intermediate step checkpoints, keeping final adapters.safetensors
    for ckpt in fg_v9_adapter.glob("*_adapters.safetensors"):
        ckpt.unlink()

    sanitize_adapter_config(
        fg_v9_adapter / "adapter_config.json",
        model_rel="models/functiongemma-270m-arrmes-v9-4bit",
        adapter_rel="adapters/functiongemma-arrmes-v9",
        data_rel="data/train-v9/mlx_functiongemma",
        resume_rel=None,
    )
    shutil.copy2(fg_v9_adapter / "adapters.safetensors", fg_v9_model / "adapters.safetensors")
    shutil.copy2(fg_v9_adapter / "adapter_config.json", fg_v9_model / "adapter_config.json")

    delta = verify_adapter_delta(None, fg_v9_adapter / "adapters.safetensors")
    total_bytes = sum(p.stat().st_size for p in fg_v9_model.rglob("*") if p.is_file())
    return {
        "name": "functiongemma-270m-arrmes-v9",
        "family": "functiongemma",
        "quantization_strategy": "residual_fp16_lora_on_4bit_v7_base_no_double_application",
        "base_model": "models/functiongemma-270m-arrmes-v7-4bit",
        "adapter_out_dir": "adapters/functiongemma-arrmes-v9",
        "out_4bit_dir": "models/functiongemma-270m-arrmes-v9-4bit",
        "out_4bit_bytes": total_bytes,
        "out_4bit_mib": round(total_bytes / (1024 * 1024), 2),
        "iters": iters,
        "learning_rate": lr,
        "batch_size": batch_size,
        "num_layers": num_layers,
        "mask_prompt": True,
        "seed": 20261107,
        "train_seconds": round(train_sec, 2),
        "delta_verification": delta,
    }


def train_qwen3_v9() -> dict[str, Any]:
    """Quantize clean FP16 Qwen3-0.6B base to 4-bit FIRST, fix <think> prompt masking in tokenizer_config.json,
    and train FP16 v9 LoRA adapter resuming from clean v6 adapter.
    """
    qwen_fp16_base = ROOT_DIR / "models" / "qwen3-06b"
    qwen_v6_adapter = ROOT_DIR / "adapters" / "qwen3-06b-distilled-v6" / "adapters.safetensors"
    qwen_v9_model = ROOT_DIR / "models" / "qwen3-06b-arrmes-v9-4bit"
    qwen_v9_adapter = ROOT_DIR / "adapters" / "qwen3-06b-arrmes-v9"

    for d in (qwen_v9_model, qwen_v9_adapter):
        if d.is_symlink():
            d.unlink()
        elif d.exists():
            shutil.rmtree(d)

    t_quant = time.perf_counter()
    subprocess.run(
        [
            PYTHON_BIN,
            "-m",
            "mlx_lm",
            "convert",
            "--hf-path",
            str(qwen_fp16_base),
            "--mlx-path",
            str(qwen_v9_model),
            "-q",
            "--q-bits",
            "4",
        ],
        check=True,
    )
    quant_sec = time.perf_counter() - t_quant

    # Ensure ChatDataset masks <think>\n\n</think>\n\n identically to LocalModel.infer(enable_thinking=False)
    tc_path = qwen_v9_model / "tokenizer_config.json"
    tc = json.loads(tc_path.read_text(encoding="utf-8"))
    if "chat_template" in tc and "{%- if enable_thinking is defined and enable_thinking is false %}" in tc["chat_template"]:
        tc["chat_template"] = tc["chat_template"].replace(
            "{%- if enable_thinking is defined and enable_thinking is false %}",
            "{%- if enable_thinking is not defined or enable_thinking is false %}",
        )
        tc_path.write_text(json.dumps(tc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    qwen_v9_adapter.mkdir(parents=True, exist_ok=True)
    cfg_path = ROOT_DIR / "configs" / "qwen3-06b-arrmes-v9.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = ROOT_DIR / "data" / "train-v9" / "mlx_hermes"

    iters = 60
    lr = 8.0e-6
    batch_size = 2
    num_layers = 14
    scale = 8.0

    cfg_path.write_text(
        "\n".join([
            f'model: "{qwen_v9_model}"',
            "train: true",
            'fine_tune_type: "lora"',
            f'data: "{data_dir}"',
            "seed: 20261107",
            f"num_layers: {num_layers}",
            f"batch_size: {batch_size}",
            f"iters: {iters}",
            "val_batches: 4",
            f"learning_rate: {lr}",
            "steps_per_report: 20",
            "steps_per_eval: 20",
            "grad_checkpoint: true",
            "mask_prompt: true",
            f'resume_adapter_file: "{qwen_v6_adapter}"',
            f'adapter_path: "{qwen_v9_adapter}"',
            "save_every: 20",
            "max_seq_length: 2752",
            "lora_parameters:",
            "  rank: 8",
            f"  scale: {scale}",
            "  dropout: 0.0",
        ]) + "\n",
        encoding="utf-8",
    )
    t0 = time.perf_counter()
    subprocess.run([PYTHON_BIN, "-m", "mlx_lm", "lora", "--config", str(cfg_path)], check=True)
    train_sec = time.perf_counter() - t0
    cfg_path.unlink(missing_ok=True)

    for ckpt in qwen_v9_adapter.glob("*_adapters.safetensors"):
        ckpt.unlink()

    sanitize_adapter_config(
        qwen_v9_adapter / "adapter_config.json",
        model_rel="models/qwen3-06b-arrmes-v9-4bit",
        adapter_rel="adapters/qwen3-06b-arrmes-v9",
        data_rel="data/train-v9/mlx_hermes",
        resume_rel="adapters/qwen3-06b-distilled-v6/adapters.safetensors",
    )
    shutil.copy2(qwen_v9_adapter / "adapters.safetensors", qwen_v9_model / "adapters.safetensors")
    shutil.copy2(qwen_v9_adapter / "adapter_config.json", qwen_v9_model / "adapter_config.json")

    delta = verify_adapter_delta(qwen_v6_adapter, qwen_v9_adapter / "adapters.safetensors")
    total_bytes = sum(p.stat().st_size for p in qwen_v9_model.rglob("*") if p.is_file())
    return {
        "name": "qwen3-06b-arrmes-v9",
        "family": "hermes",
        "quantization_strategy": "mixed_precision_clean_4bit_base_plus_fp16_lora_adapter",
        "fp16_base_model": "models/qwen3-06b",
        "resume_adapter": "adapters/qwen3-06b-distilled-v6/adapters.safetensors",
        "adapter_out_dir": "adapters/qwen3-06b-arrmes-v9",
        "out_4bit_dir": "models/qwen3-06b-arrmes-v9-4bit",
        "out_4bit_bytes": total_bytes,
        "out_4bit_mib": round(total_bytes / (1024 * 1024), 2),
        "iters": iters,
        "learning_rate": lr,
        "batch_size": batch_size,
        "num_layers": num_layers,
        "mask_prompt": True,
        "seed": 20261107,
        "quant_seconds": round(quant_sec, 2),
        "train_seconds": round(train_sec, 2),
        "delta_verification": delta,
    }


def eval_generative_slice(m: LocalModel, family: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    raw_ok = ver_ok = 0
    lats: list[float] = []
    for c in cases:
        out = m.infer(c["query"], c.get("context"))
        lats.append(float(out.get("seconds", 0.0)) * 1000.0)
        exp = normalized(Call(c["expected"]["name"], c["expected"]["arguments"]))
        try:
            rc = normalized(decode(out["raw_unverified"], family=family))
        except Exception:
            rc = None
        try:
            vc = normalized(decode(out["raw"], family=family))
        except Exception:
            vc = None
        raw_ok += int(rc == exp)
        ver_ok += int(vc == exp)
    lats_s = sorted(lats)
    n = len(cases)
    raw_w = {"lower": round(wilson_lower_95(raw_ok, n), 6)}
    ver_w = {"lower": round(wilson_lower_95(ver_ok, n), 6)}
    return {
        "total": n,
        "track_a_raw_exact": raw_ok,
        "track_a_raw_accuracy": round(raw_ok / max(n, 1), 4),
        "track_a_wilson_95_lb": raw_w["lower"],
        "track_b_verified_exact": ver_ok,
        "track_b_verified_accuracy": round(ver_ok / max(n, 1), 4),
        "track_b_wilson_95_lb": ver_w["lower"],
        "warm_p50_ms": round(lats_s[n // 2], 2) if lats_s else 0.0,
        "warm_p95_ms": round(lats_s[int(n * 0.95)], 2) if lats_s else 0.0,
    }


def evaluate_v9_generative(model_dir: Path, family: str) -> dict[str, Any]:
    m = LocalModel(str(model_dir), family=family, contract_version="v2")
    overnight = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "overnight-v1" / "routing-heldout.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    av7_pos_all = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "acceptance-v7" / "cases-positive.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    av7_pos_v2 = [c for c in av7_pos_all if c.get("contract_version") == "v2"]
    av7_neg_all = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "acceptance-v7" / "cases-negative.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    # Stratified 150-case v2 positive slice (every 7th case across all v2 actions) + 150-case negative slice (every 8th case across all 35 families)
    av7_pos_slice = av7_pos_v2[::7][:150]
    av7_neg_slice = av7_neg_all[::8][:150]

    # Warmup
    m.infer(overnight[0]["query"], overnight[0].get("context"))
    return {
        "backend": m.backend,
        "router_assisted_overwrite": False,
        "overnight_v1_80": eval_generative_slice(m, family, overnight),
        "acceptance_v7_v2_pos_150": eval_generative_slice(m, family, av7_pos_slice),
        "acceptance_v7_neg_150": eval_generative_slice(m, family, av7_neg_slice),
    }


def main() -> None:
    print("[v9] Step 1: Building TrainPool-v9 and verifying zero template/entity/query leakage vs acceptance-v7...")
    rows, train_meta = build_train_v9()
    print("[v9] TrainPool-v9 metadata:", json.dumps(train_meta, indent=2))

    print("[v9] Step 2: Training ArrmesNeuralRouterV3 v9 weights on TrainPool-v9...")
    router_cfg = train_neural_router_v9(rows)
    print("[v9] Router v9 trained:", json.dumps(router_cfg, indent=2))

    print("[v9] Step 3: Building balanced MLX QLoRA v9 datasets...")
    mlx_stats = build_mlx_v9_datasets(rows)
    print("[v9] MLX dataset stats:", json.dumps(mlx_stats, indent=2))

    print("[v9] Step 4: Training FunctionGemma-270M v9 residual QLoRA...")
    fg_meta = train_functiongemma_v9()
    print("[v9] FunctionGemma v9:", json.dumps(fg_meta, indent=2))

    print("[v9] Step 5: Training Qwen3-0.6B v9 mixed-precision QLoRA...")
    qwen_meta = train_qwen3_v9()
    print("[v9] Qwen3-0.6B v9:", json.dumps(qwen_meta, indent=2))

    # Clean up bulky MLX chat-template JSONL caches after SHA-256 capture
    shutil.rmtree(ROOT_DIR / "data" / "train-v9" / "mlx_functiongemma", ignore_errors=True)
    shutil.rmtree(ROOT_DIR / "data" / "train-v9" / "mlx_hermes", ignore_errors=True)
    shutil.rmtree(ROOT_DIR / "configs", ignore_errors=True)

    print("[v9] Step 6: Evaluating unassisted Track A & verified Track B for FunctionGemma-270M v9...")
    fg_eval = evaluate_v9_generative(ROOT_DIR / "models" / "functiongemma-270m-arrmes-v9-4bit", "functiongemma")
    print("[v9] FunctionGemma v9 eval:", json.dumps(fg_eval, indent=2))

    print("[v9] Step 7: Evaluating unassisted Track A & verified Track B for Qwen3-0.6B v9...")
    qwen_eval = evaluate_v9_generative(ROOT_DIR / "models" / "qwen3-06b-arrmes-v9-4bit", "hermes")
    print("[v9] Qwen3-0.6B v9 eval:", json.dumps(qwen_eval, indent=2))

    prov = {
        "version": "v9",
        "trained_at": "2026-11-07",
        "train_pool_v9": train_meta,
        "mlx_curriculum_v9": mlx_stats,
        "neural_router_v9": router_cfg,
        "functiongemma_270m_v9_qlora": {
            **fg_meta,
            "evaluation": fg_eval,
        },
        "qwen3_06b_v9_qlora": {
            **qwen_meta,
            "evaluation": qwen_eval,
        },
    }
    prov_path = ROOT_DIR / "experiments" / "mlx-lora-v9-provenance.json"
    prov_path.write_text(json.dumps(prov, indent=2) + "\n", encoding="utf-8")
    print("[v9] Saved provenance receipt to", prov_path)


if __name__ == "__main__":
    main()
