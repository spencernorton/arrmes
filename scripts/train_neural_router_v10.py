#!/usr/bin/env python3
"""Train & temperature-calibrate ArrmesNeuralRouterV3 v10 on TrainPool-v10 (12,620 rows across 28 classes)
with 5-fold Template-Skeleton GroupKFold OOF, ECE calibration, and full acceptance-v7 + overnight-v1 verification.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import sys
import time
from typing import Any

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
for _p in (str(ROOT_DIR), str(ROOT_DIR / "src"), str(ROOT_DIR / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from arrmes.evaluate import score
from arrmes.neural_router import (
    ArrmesNeuralRouterV3,
    CLASS_TO_IDX,
    ROUTER_CLASSES,
    TOTAL_FEATURES,
    featurize_query,
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_ece(probs: np.ndarray, y_true: np.ndarray, n_bins: int = 15) -> float:
    conf = np.max(probs, axis=1)
    pred = np.argmax(probs, axis=1)
    acc = (pred == y_true).astype(np.float64)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n = len(y_true)
    for b in range(n_bins):
        lo, hi = bins[b], bins[b + 1]
        mask = (conf >= lo) & (conf <= hi if b == n_bins - 1 else conf < hi)
        if np.any(mask):
            ece += (np.sum(mask) / n) * abs(float(np.mean(acc[mask])) - float(np.mean(conf[mask])))
    return float(ece)


def fit_mlp_adamw(
    X: np.ndarray,
    y: np.ndarray,
    sample_w: np.ndarray | None = None,
    *,
    hidden_dim: int = 128,
    epochs: int = 38,
    batch_size: int = 64,
    lr: float = 4.0e-3,
    wd: float = 2.0e-4,
    seed: int = 20261111,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[float]]:
    rng = np.random.default_rng(seed)
    num_classes = len(ROUTER_CLASSES)
    n = X.shape[0]

    W1 = (rng.standard_normal((TOTAL_FEATURES, hidden_dim)) * np.sqrt(2.0 / TOTAL_FEATURES)).astype(np.float32)
    b1 = np.zeros(hidden_dim, dtype=np.float32)
    W2 = (rng.standard_normal((hidden_dim, num_classes)) * np.sqrt(2.0 / hidden_dim)).astype(np.float32)
    b2 = np.zeros(num_classes, dtype=np.float32)

    params = [W1, b1, W2, b2]
    m_state = [np.zeros_like(p) for p in params]
    v_state = [np.zeros_like(p) for p in params]
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    total_steps = epochs * ((n + batch_size - 1) // batch_size)
    step_num = 0
    epoch_losses: list[float] = []

    for _ep in range(epochs):
        perm = rng.permutation(n)
        ep_loss = 0.0
        steps = 0
        for i in range(0, n, batch_size):
            idx = perm[i : i + batch_size]
            xb, yb = X[idx], y[idx]
            bs = xb.shape[0]
            step_num += 1
            cur_lr = lr * 0.5 * (1.0 + math.cos(math.pi * min(step_num, total_steps) / max(total_steps, 1)))
            cur_lr = max(cur_lr, lr * 0.05)

            z1 = xb @ W1 + b1
            h1 = np.maximum(0.0, z1)
            logits = h1 @ W2 + b2
            logits_shift = logits - np.max(logits, axis=1, keepdims=True)
            exp_l = np.exp(logits_shift)
            probs = exp_l / np.sum(exp_l, axis=1, keepdims=True)

            wb = sample_w[idx] if sample_w is not None else np.ones(bs, dtype=np.float32)
            w_sum = max(float(np.sum(wb)), 1e-8)
            loss = float(-np.sum(wb * np.log(np.clip(probs[np.arange(bs), yb], 1e-12, 1.0))) / w_sum)
            ep_loss += loss
            steps += 1

            dlogits = probs
            dlogits[np.arange(bs), yb] -= 1.0
            dlogits *= (wb[:, None] / w_sum)

            dW2 = h1.T @ dlogits
            db2 = np.sum(dlogits, axis=0)
            dh1 = dlogits @ W2.T
            dz1 = dh1 * (z1 > 0)
            dW1 = xb.T @ dz1
            db1 = np.sum(dz1, axis=0)

            for pi, (param, grad) in enumerate(zip(params, (dW1, db1, dW2, db2))):
                if param.ndim == 2:
                    param *= (1.0 - cur_lr * wd)
                m_state[pi] = beta1 * m_state[pi] + (1.0 - beta1) * grad
                v_state[pi] = beta2 * v_state[pi] + (1.0 - beta2) * (grad * grad)
                m_hat = m_state[pi] / (1.0 - beta1 ** step_num)
                v_hat = v_state[pi] / (1.0 - beta2 ** step_num)
                param -= cur_lr * m_hat / (np.sqrt(v_hat) + eps)

        epoch_losses.append(round(ep_loss / max(steps, 1), 6))

    return W1, b1, W2, b2, epoch_losses


def softmax_temp(logits: np.ndarray, temp: float) -> np.ndarray:
    s = logits / temp
    s = s - np.max(s, axis=1, keepdims=True)
    e = np.exp(s)
    return e / np.sum(e, axis=1, keepdims=True)


def main() -> None:
    t0 = time.perf_counter()
    from build_trainpool_v10 import load_barriers
    from build_acceptance_v8 import _norm_q, normalize_skeleton_token_bag, normalize_template_skeleton, normalize_token_bag
    from arrmes.neural_router import label_from_call
    barriers = load_barriers()
    sft_v10 = [json.loads(l) for l in (ROOT_DIR / "data/train-v10/trainpool_v10_oof.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    skel_to_fold = {r["skeleton_hash"]: int(r["fold"]) for r in sft_v10}
    seen_q = {(_norm_q(r["query"]), json.dumps(r.get("context"), sort_keys=True)) for r in sft_v10}
    extra_tv9 = []
    for r in barriers["tv9_rows"]:
        q = r["query"]
        key = (_norm_q(q), json.dumps(r.get("context"), sort_keys=True))
        if key in seen_q:
            continue
        sk = normalize_template_skeleton(q, barriers["sorted_ents"])
        sb = normalize_skeleton_token_bag(q, barriers["sorted_ents"])
        sh = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()
        if sh(_norm_q(q)) in barriers["forbidden_exact_sha"] or sh(normalize_token_bag(q)) in barriers["forbidden_tbag_sha"] or sh(sk) in barriers["forbidden_skel_sha"] or sh(sb) in barriers["forbidden_sbag_sha"]:
            continue
        seen_q.add(key)
        sk_h = sh(sk)
        extra_tv9.append({
            **r,
            "label": label_from_call(r["expected"]),
            "skeleton_hash": sk_h,
            "fold": skel_to_fold.setdefault(sk_h, int(sk_h[:8], 16) % 5),
            "provenance_source": r.get("provenance_source", "train-v9"),
        })
    rows = sft_v10 + extra_tv9
    # Assert zero cross-fold skeleton leakage across all combined rows
    _by_sk = {}
    for _r in rows:
        _by_sk.setdefault(_r["skeleton_hash"], set()).add(int(_r["fold"]))
    assert all(len(fs) == 1 for fs in _by_sk.values()), "Skeleton split across folds!"
    n = len(rows)
    X = np.stack([featurize_query(r["query"], r.get("context")) for r in rows]).astype(np.float32)
    y = np.array([CLASS_TO_IDX[r["label"]] for r in rows], dtype=np.int64)
    sample_w = np.array([0.20 if str(r.get("provenance_source", "")).startswith("v10_") else 1.0 for r in rows], dtype=np.float32)
    folds = np.array([int(r["fold"]) for r in rows], dtype=np.int64)

    # 1. 5-fold Template-Skeleton GroupKFold OOF
    oof_logits = np.zeros((n, len(ROUTER_CLASSES)), dtype=np.float32)
    oof_raw_exact = np.zeros(n, dtype=np.int64)
    oof_ver_exact = np.zeros(n, dtype=np.int64)
    tmp_fold_dir = ROOT_DIR / "work" / "_tmp_router_fold"
    tmp_fold_dir.mkdir(parents=True, exist_ok=True)

    for f in range(5):
        tr_mask = folds != f
        va_mask = folds == f
        W1_f, b1_f, W2_f, b2_f, _ = fit_mlp_adamw(X[tr_mask], y[tr_mask], sample_w[tr_mask], seed=20261111 + f)
        h_va = np.maximum(0.0, X[va_mask] @ W1_f + b1_f)
        oof_logits[va_mask] = h_va @ W2_f + b2_f

        np.savez_compressed(tmp_fold_dir / "router_weights.npz", W1=W1_f, b1=b1_f, W2=W2_f, b2=b2_f, temperature=np.array([1.0], dtype=np.float32))
        rtr_v2 = ArrmesNeuralRouterV3(str(tmp_fold_dir), family="hermes", contract_version="v2")
        rtr_v3 = ArrmesNeuralRouterV3(str(tmp_fold_dir), family="hermes", contract_version="v3")
        va_indices = np.where(va_mask)[0]
        for idx_i in va_indices:
            r = rows[int(idx_i)]
            cver = r.get("contract_version", "v2")
            rtr = rtr_v3 if cver == "v3" else rtr_v2
            inf = rtr.infer(r["query"], r.get("context"))
            oof_raw_exact[idx_i] = int(score(inf["raw_unverified"], r["expected"], "hermes", allow_v3=(cver == "v3"))["exact"])
            oof_ver_exact[idx_i] = int(score(inf["raw"], r["expected"], "hermes", allow_v3=(cver == "v3"))["exact"])

    shutil.rmtree(tmp_fold_dir, ignore_errors=True)

    # 2. Temperature scaling on OOF logits
    probs_uncal = softmax_temp(oof_logits, 1.0)
    nll_before = float(-np.mean(np.log(np.clip(probs_uncal[np.arange(n), y], 1e-12, 1.0))))
    ece_before = compute_ece(probs_uncal, y)

    best_temp = 1.0
    best_nll = nll_before
    for cand_t in np.linspace(0.30, 2.50, 221):
        p_c = softmax_temp(oof_logits, float(cand_t))
        nll_c = float(-np.mean(np.log(np.clip(p_c[np.arange(n), y], 1e-12, 1.0))))
        if nll_c < best_nll:
            best_nll = nll_c
            best_temp = round(float(cand_t), 4)

    probs_cal = softmax_temp(oof_logits, best_temp)
    ece_after = compute_ece(probs_cal, y)
    oof_top1_acc = float(np.mean(np.argmax(oof_logits, axis=1) == y))

    # 3. Train full-pool ArrmesNeuralRouterV3 v10 into worktree models/arrmes-neural-router-v3 (unlink symlink safely!)
    W1, b1, W2, b2, epoch_losses = fit_mlp_adamw(X, y, sample_w, seed=20261111)
    save_dir = ROOT_DIR / "models" / "arrmes-neural-router-v3"
    if save_dir.is_symlink():
        save_dir.unlink()
    elif save_dir.exists():
        shutil.rmtree(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    weights_path = save_dir / "router_weights.npz"
    np.savez_compressed(
        weights_path,
        W1=W1,
        b1=b1,
        W2=W2,
        b2=b2,
        temperature=np.array([best_temp], dtype=np.float32),
    )
    w_sha = sha256_file(weights_path)
    w_bytes = weights_path.stat().st_size

    # 4. Evaluate full acceptance-v7 (N=2580) & overnight-v1 (N=80) & warm M4 Pro latency
    rtr_final_v2 = ArrmesNeuralRouterV3(str(save_dir), family="hermes", contract_version="v2")
    rtr_final_v3 = ArrmesNeuralRouterV3(str(save_dir), family="hermes", contract_version="v3")
    av7_pos = [json.loads(l) for l in (ROOT_DIR / "data/acceptance-v7/cases-positive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    av7_neg = [json.loads(l) for l in (ROOT_DIR / "data/acceptance-v7/cases-negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ov1 = [json.loads(l) for l in (ROOT_DIR / "data/overnight-v1/routing-heldout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    def eval_split(split_rows: list[dict[str, Any]], default_v3: bool = False) -> dict[str, Any]:
        raw_ok = 0
        ver_ok = 0
        lats: list[float] = []
        for c in split_rows:
            use_v3 = default_v3 or (c.get("contract_version") == "v3")
            rtr_cur = rtr_final_v3 if use_v3 else rtr_final_v2
            inf = rtr_cur.infer(c["query"], c.get("context"))
            lats.append(inf["seconds"] * 1000.0)
            raw_ok += int(score(inf["raw_unverified"], c["expected"], "hermes", allow_v3=use_v3)["exact"])
            ver_ok += int(score(inf["raw"], c["expected"], "hermes", allow_v3=use_v3)["exact"])
        warm = lats[5:] if len(lats) > 5 else lats
        return {
            "cases": len(split_rows),
            "raw_exact": raw_ok,
            "raw_accuracy": round(raw_ok / len(split_rows), 6),
            "verified_exact": ver_ok,
            "verified_accuracy": round(ver_ok / len(split_rows), 6),
            "warm_p50_ms": round(statistics.median(warm), 4),
            "warm_p95_ms": round(sorted(warm)[int(0.95 * len(warm))], 4),
        }

    av7_pos_res = eval_split(av7_pos)
    av7_neg_res = eval_split(av7_neg, default_v3=True)
    ov1_res = eval_split(ov1)

    cfg = {
        "architecture": "ArrmesNeuralRouterV3-MultiHeadHashedMLP",
        "version": "v10",
        "pipeline_version": "arrmes-v10-clean-r1",
        "input_dim": TOTAL_FEATURES,
        "hidden_dim": 128,
        "num_classes": len(ROUTER_CLASSES),
        "classes": list(ROUTER_CLASSES),
        "train_examples": n,
        "epochs": 38,
        "temperature": best_temp,
        "calibration": {
            "oof_folds": 5,
            "oof_top1_class_accuracy": round(oof_top1_acc, 6),
            "oof_raw_exact_accuracy": round(float(np.mean(oof_raw_exact)), 6),
            "oof_verified_exact_accuracy": round(float(np.mean(oof_ver_exact)), 6),
            "oof_nll_before": round(nll_before, 6),
            "oof_nll_after": round(best_nll, 6),
            "oof_ece_before": round(ece_before, 6),
            "oof_ece_after": round(ece_after, 6),
        },
        "dev_benchmarks": {
            "acceptance_v7_positive": av7_pos_res,
            "acceptance_v7_negative_v3_contract": av7_neg_res,
            "acceptance_v7_negative_strict_v2_contract": eval_split(av7_neg, default_v3=False),
            "overnight_v1": ov1_res,
        },
        "initial_loss": epoch_losses[0],
        "final_loss": epoch_losses[-1],
        "epoch_losses": epoch_losses,
        "weights_file": "router_weights.npz",
        "weights_bytes": w_bytes,
        "weights_mib": round(w_bytes / (1024 * 1024), 3),
        "weights_sha256": w_sha,
        "train_seconds": round(time.perf_counter() - t0, 3),
        "seed": 20261111,
    }
    (save_dir / "router_config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (ROOT_DIR / "experiments" / "arrmes-neural-router-v3-config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(cfg["calibration"], indent=2))
    print(json.dumps(cfg["dev_benchmarks"], indent=2))
    print(f"weights_mib={cfg['weights_mib']} sha256={w_sha}")


if __name__ == "__main__":
    main()
