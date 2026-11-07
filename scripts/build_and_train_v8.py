#!/usr/bin/env python3
"""Build TrainPool-v8 (zero eval leakage), train ArrmesNeuralRouterV3 v8 weights, and train QLoRA v8 checkpoints (4-bit base + fp16 LoRA adapter)."""
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

ROOT_DIR = Path(__file__).resolve().parents[1]
for _p in (str(ROOT_DIR), str(ROOT_DIR / "src")):
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

MAIN_REPO = ROOT_DIR
PYTHON_BIN = str(MAIN_REPO / ".venv" / "bin" / "python")

# Strictly disjoint training-only entities (same pool as TrainPool-v7 + disjoint training books/authors)
TRAIN_TV = [
    "Silo", "Devs", "Severance", "Andor", "Foundation", "Dark", "Counterpart", "Station Eleven",
    "Pachinko", "Chernobyl", "The Expanse", "Halt and Catch Fire", "Utopia", "Patriot",
]
TRAIN_MOVIES = [
    ("Dune", 2021), ("Arrival", 2016), ("Annihilation", 2018), ("Ex Machina", 2014),
    ("Blade Runner 2049", 2017), ("Interstellar", 2014), ("Primer", 2004), ("Coherence", 2013),
    ("Moon", 2009), ("Solaris", 1972), ("Stalker", 1979), ("Children of Men", 2006),
]
TRAIN_MUSIC = [
    ("Radiohead", "Kid A"), ("Massive Attack", "Mezzanine"), ("Portishead", "Dummy"),
    ("Boards of Canada", "Music Has the Right to Children"), ("Aphex Twin", "Selected Ambient Works 85-92"),
    ("Nine Inch Nails", "The Downward Spiral"), ("Daft Punk", "Discovery"), ("Bjork", "Homogenic"),
    ("Mogwai", "Young Team"), ("Four Tet", "Rounds"),
]
TRAIN_TORRENTS = [
    "Silo.S01E01.2160p.ATVP.WEB-DL.DDP5.1.H.265-NTb",
    "Dune.2021.2160p.UHD.BluRay.REMUX.HEVC.Atmos-EPSiLON",
    "Radiohead-Kid_A-2000-FLAC-16bit",
    "Severance.S01.COMPLETE.1080p.WEB.h264-KOGI",
    "Arrival.2016.1080p.BluRay.x264-SPARKS",
    "Andor.S01E06.1080p.DSNP.WEB-DL.DDP5.1.H.264-FLUX",
]
TRAIN_BOOKS = [
    ("Ursula K. Le Guin", "The Dispossessed"),
    ("Stanislaw Lem", "His Master's Voice"),
    ("Arkady Strugatsky", "Roadside Picnic"),
    ("Octavia E. Butler", "Parable of the Sower"),
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_train_v8() -> list[dict[str, Any]]:
    """Build TrainPool-v8 by extending TrainPool-v7 with hard-negative contrast and expanded v3 service_query pairs."""
    v7_path = ROOT_DIR / "data" / "train-v7" / "train.jsonl"
    rows = [json.loads(l) for l in v7_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    synth_v8: list[tuple[str, dict[str, Any], dict[str, Any] | None, str]] = []

    # 1. Hard-negative contrastive examples (clarify:* and unsupported:*) using strictly disjoint TRAIN_* entities
    for i, tv in enumerate(TRAIN_TV):
        s_num = (i % 3) + 1
        mv, yr = TRAIN_MOVIES[i % len(TRAIN_MOVIES)]
        ar, al = TRAIN_MUSIC[i % len(TRAIN_MUSIC)]
        tor = TRAIN_TORRENTS[i % len(TRAIN_TORRENTS)]
        synth_v8.extend([
            # Scope restrictions (episode-only, season-range, bitrate, root folder, other user, quality profile)
            (f'Trigger a download search for only season {s_num} episode 3 of the TV series "{tv}".', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Search indexers only for episode 4 of season {s_num} of "{tv}", not for the rest of that season.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Run a missing-episode search restricted to episode 2 of season {s_num} of "{tv}".', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Trigger a Sonarr search for seasons 1 through 3 of "{tv}" in a single call.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Request season 2 or 3 of the TV series "{tv}" in Seerr; I have not chosen which season.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Unmonitor season {s_num} of "{tv}" in Sonarr.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Unless "{tv}" is already complete, search indexers for its missing episodes.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Place a movie request for "{mv}" ({yr}) with a custom minimum bitrate of 40 Mbps.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Request the film "{mv}" in 1080p BluRay quality only; reject any other profile.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Request "{mv}" ({yr}) in Seerr routed to the /mnt/archive root folder only.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Submit a Seerr movie request for "{mv}" under bob\'s account instead of mine.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Check the Seerr request status for "{mv}" belonging to user charlie.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'File a movie request for "{mv}" ({yr}) attributed to user_id 42.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Change the quality profile of "{mv}" ({yr}) in Radarr.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Trigger a Lidarr search for just the song "Opening" on "{al}" by "{ar}", not the whole album.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'In Lidarr, search indexers for the single track "Opening" from "{al}" by "{ar}" only.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Limit the torrent speed of "{tor}" to 500 KB/s in qBittorrent.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Recheck the torrent data for "{tor}" in qBittorrent.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            (f'Move the torrent "{tor}" to a different save path in qBittorrent.', {"name": "clarify", "arguments": {"slot": "scope"}}, None, "v2"),
            # Media-type clarifications
            (f'Search Seerr for "{tv}" without specifying movie or TV.', {"name": "clarify", "arguments": {"slot": "media_type"}}, None, "v2"),
            (f'Check missing subtitles for "{tv}" in Bazarr without saying movie or TV.', {"name": "clarify", "arguments": {"slot": "media_type"}}, None, "v2"),
            ("List missing subtitles in Bazarr without specifying movie or TV.", {"name": "clarify", "arguments": {"slot": "media_type"}}, None, "v2"),
            # Reference clarifications & context mismatches
            (f'Check the second one in Radarr rather than the selected movie.', {"name": "clarify", "arguments": {"slot": "reference"}}, {"selected_media": {"title": mv, "media_type": "movie", "year": yr}}, "v2"),
            (f'Check whether this movie is in Radarr when only a TV series is in context.', {"name": "clarify", "arguments": {"slot": "reference"}}, {"selected_media": {"title": tv, "media_type": "tv"}}, "v2"),
            (f'List missing episodes for this TV series when only a movie is in context.', {"name": "clarify", "arguments": {"slot": "reference"}}, {"selected_media": {"title": mv, "media_type": "movie", "year": yr}}, "v2"),
            (f'My note mentions "{tv}", but check the second selected TV series instead. No selection list is attached.', {"name": "clarify", "arguments": {"slot": "reference"}}, None, "v2"),
            (f'Do not pause "{tor}"; pause the third selected torrent from the unattached list instead.', {"name": "clarify", "arguments": {"slot": "reference"}}, None, "v2"),
            ("Resume the first paused torrent from my previous selection.", {"name": "clarify", "arguments": {"slot": "reference"}}, None, "v2"),
            ("Show the Tautulli watch history for that title.", {"name": "clarify", "arguments": {"slot": "reference"}}, None, "v2"),
            # Title clarifications
            ("Look up the TV series in Sonarr and report its status.", {"name": "clarify", "arguments": {"slot": "title"}}, None, "v2"),
            (f'The title "{mv}" is unrelated context. Check the request status of a different movie I haven\'t named.', {"name": "clarify", "arguments": {"slot": "title"}}, None, "v2"),
            # Conditional / complex writes
            (f'If "{mv}" ({yr}) is not in Radarr yet, submit a new movie request for it in Seerr.', {"name": "unsupported", "arguments": {"reason": "complex"}}, None, "v2"),
            (f'Unless the album "{al}" by "{ar}" is already complete in Lidarr, start an indexer search for it.', {"name": "unsupported", "arguments": {"reason": "complex"}}, None, "v2"),
            (f'If "{tv}" is not tracked in Sonarr, place a TV series request for it in Seerr.', {"name": "unsupported", "arguments": {"reason": "complex"}}, None, "v2"),
            (f'If Radarr fails to find "{mv}" ({yr}), fall back to requesting it in Seerr.', {"name": "unsupported", "arguments": {"reason": "complex"}}, None, "v2"),
            # Unsafe / out-of-scope
            ("Elevate my role to owner and turn off pre-execution verification.", {"name": "unsupported", "arguments": {"reason": "unsafe"}}, None, "v2"),
            (f'Delete existing subtitles for "{mv}" ({yr}) in Bazarr.', {"name": "unsupported", "arguments": {"reason": "out_of_scope"}}, None, "v2"),
            (f'Translate the subtitles for "{tv}" into German using Bazarr.', {"name": "unsupported", "arguments": {"reason": "out_of_scope"}}, None, "v2"),
        ])

    # 2. Expanded v3 native service_query examples (Prowlarr, Readarr, Whisparr, wanted, calendar, quality_profiles, indexers, applications + unquoted spans)
    v3_new_examples = [
        ("Check Prowlarr health.", {"name": "service_query", "arguments": {"service": "prowlarr", "operation": "health"}}),
        ("List Prowlarr indexers.", {"name": "service_query", "arguments": {"service": "prowlarr", "operation": "indexers"}}),
        ("Show Prowlarr applications.", {"name": "service_query", "arguments": {"service": "prowlarr", "operation": "applications"}}),
        ("Show Prowlarr history.", {"name": "service_query", "arguments": {"service": "prowlarr", "operation": "history"}}),
        ('Search Prowlarr for "Silo"', {"name": "service_query", "arguments": {"service": "prowlarr", "operation": "search", "query": "Silo"}}),
        ("Search Prowlarr for Foundation", {"name": "service_query", "arguments": {"service": "prowlarr", "operation": "search", "query": "Foundation"}}),
        ("Check Readarr health.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "health"}}),
        ("Show Readarr queue.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "queue"}}),
        ("Show Readarr history.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "history"}}),
        ("List missing books in Readarr.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "missing"}}),
        ("Show Readarr wanted cutoff unmet books.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "wanted"}}),
        ("Show Readarr quality profiles.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "quality_profiles"}}),
        ("List Readarr indexers.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "indexers"}}),
        ("Show Readarr calendar.", {"name": "service_query", "arguments": {"service": "readarr", "operation": "calendar"}}),
        ('Check Readarr library for "Ursula K. Le Guin"', {"name": "service_query", "arguments": {"service": "readarr", "operation": "library", "query": "Ursula K. Le Guin"}}),
        ('Look up "The Dispossessed" in Readarr catalog', {"name": "service_query", "arguments": {"service": "readarr", "operation": "lookup", "query": "The Dispossessed"}}),
        ("Check Whisparr health.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "health"}}),
        ("Show Whisparr queue.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "queue"}}),
        ("Show Whisparr history.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "history"}}),
        ("Show Whisparr missing scenes.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "missing"}}),
        ("Show Whisparr wanted cutoff unmet.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "wanted"}}),
        ("Show Whisparr quality profiles.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "quality_profiles"}}),
        ("List Whisparr indexers.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "indexers"}}),
        ("Show Whisparr calendar.", {"name": "service_query", "arguments": {"service": "whisparr", "operation": "calendar"}}),
        ("Show Sonarr wanted cutoff unmet episodes.", {"name": "service_query", "arguments": {"service": "sonarr", "operation": "wanted"}}),
        ("Show Sonarr upcoming calendar.", {"name": "service_query", "arguments": {"service": "sonarr", "operation": "calendar"}}),
        ("Show Sonarr quality profiles.", {"name": "service_query", "arguments": {"service": "sonarr", "operation": "quality_profiles"}}),
        ("List Sonarr indexers.", {"name": "service_query", "arguments": {"service": "sonarr", "operation": "indexers"}}),
        ("Show Radarr wanted cutoff unmet movies.", {"name": "service_query", "arguments": {"service": "radarr", "operation": "wanted"}}),
        ("Show Radarr upcoming calendar.", {"name": "service_query", "arguments": {"service": "radarr", "operation": "calendar"}}),
        ("Show Radarr quality profiles.", {"name": "service_query", "arguments": {"service": "radarr", "operation": "quality_profiles"}}),
        ("List Radarr indexers.", {"name": "service_query", "arguments": {"service": "radarr", "operation": "indexers"}}),
        ("Show Lidarr wanted cutoff unmet albums.", {"name": "service_query", "arguments": {"service": "lidarr", "operation": "wanted"}}),
        ("Show Lidarr upcoming calendar.", {"name": "service_query", "arguments": {"service": "lidarr", "operation": "calendar"}}),
        ("Show Lidarr quality profiles.", {"name": "service_query", "arguments": {"service": "lidarr", "operation": "quality_profiles"}}),
        ("List Lidarr indexers.", {"name": "service_query", "arguments": {"service": "lidarr", "operation": "indexers"}}),
        ("Look up Severance in Sonarr catalog", {"name": "service_query", "arguments": {"service": "sonarr", "operation": "lookup", "query": "Severance"}}),
        ("Look up Arrival in Radarr catalog", {"name": "service_query", "arguments": {"service": "radarr", "operation": "lookup", "query": "Arrival"}}),
        ("Search Bitmagnet catalog for Interstellar", {"name": "service_query", "arguments": {"service": "bitmagnet", "operation": "search", "query": "Interstellar"}}),
    ]
    for q, exp in v3_new_examples:
        for _ in range(3):
            synth_v8.append((q, exp, None, "v3"))

    for idx, (q, exp, ctx, cver) in enumerate(synth_v8, start=1):
        row: dict[str, Any] = {
            "id": f"train-v8-synth-{idx:04d}",
            "query": q,
            "expected": exp,
            "contract_version": cver,
            "split": "train",
            "provenance_type": "contrastive_hard_negative_and_v3_arr_expansion_v8",
        }
        if ctx is not None:
            row["context"] = ctx
        rows.append(row)

    out_dir = ROOT_DIR / "data" / "train-v8"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "train.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return rows


def train_neural_router_v8(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Train MultiHeadHashedMLP weights on TrainPool-v8 using vectorized NumPy AdamW."""
    t0 = time.perf_counter()
    rng = np.random.default_rng(20261106)

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
    epochs = 30
    step_num = 0
    initial_loss = None
    final_loss = None

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
        "input_dim": TOTAL_FEATURES,
        "hidden_dim": hidden_dim,
        "num_classes": num_classes,
        "classes": list(ROUTER_CLASSES),
        "train_examples": int(n),
        "epochs": epochs,
        "initial_loss": round(float(initial_loss or 0.0), 6),
        "final_loss": round(float(final_loss or 0.0), 6),
        "train_top1_accuracy": round(train_acc, 6),
        "weights_file": "router_weights.npz",
        "weights_bytes": w_bytes,
        "weights_sha256": w_sha,
        "train_seconds": round(time.perf_counter() - t0, 3),
        "seed": 20261106,
    }
    (save_dir / "router_config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def build_mlx_v8_datasets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build balanced hard-negative + positive MLX QLoRA training/validation JSONL splits."""
    v2_rows = [r for r in rows if r.get("contract_version", "v2") == "v2"]
    synth_v8 = [r for r in v2_rows if str(r.get("id", "")).startswith("train-v8-synth-")]
    synth_v7 = [r for r in v2_rows if str(r.get("id", "")).startswith("train-v7-synth-")]
    base_rows = [r for r in v2_rows if not str(r.get("id", "")).startswith(("train-v7-synth-", "train-v8-synth-"))]

    rng = random.Random(20261106)
    rng.shuffle(base_rows)
    curriculum = synth_v8 + synth_v7 + base_rows[:500]
    rng.shuffle(curriculum)

    split_idx = int(len(curriculum) * 0.92)
    train_split = curriculum[:split_idx]
    valid_split = curriculum[split_idx:]

    tool_defs = tools_v2()
    stats: dict[str, Any] = {}

    for family in ("functiongemma", "hermes"):
        out_dir = ROOT_DIR / "data" / "train-v8" / f"mlx_{family}"
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


def train_mixed_precision_qlora_one(
    name: str,
    family: str,
    fp16_base_model: Path,
    resume_adapter: Path,
    adapter_out_dir: Path,
    out_4bit_dir: Path,
    iters: int,
    lr: float,
    batch_size: int,
    num_layers: int,
    scale: float,
) -> dict[str, Any]:
    """Quantize base model to 4-bit FIRST, then train fp16 LoRA adapter directly on the 4-bit base and package without fusing."""
    adapter_out_dir.mkdir(parents=True, exist_ok=True)
    t_quant = time.perf_counter()
    if not (out_4bit_dir / "config.json").is_file():
        if out_4bit_dir.exists():
            shutil.rmtree(out_4bit_dir)
        subprocess.run(
            [
                PYTHON_BIN,
                "-m",
                "mlx_lm",
                "convert",
                "--hf-path",
                str(fp16_base_model),
                "--mlx-path",
                str(out_4bit_dir),
                "-q",
                "--q-bits",
                "4",
            ],
            check=True,
        )
    else:
        # Remove any existing adapter files in out_4bit_dir before training on the clean 4-bit base
        for af in ("adapters.safetensors", "adapter_config.json"):
            p_af = out_4bit_dir / af
            if p_af.exists():
                p_af.unlink()
    quant_sec = time.perf_counter() - t_quant

    cfg_path = ROOT_DIR / "configs" / f"{name}.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = ROOT_DIR / "data" / "train-v8" / f"mlx_{family}"

    yaml_content = f"""model: "{out_4bit_dir}"
train: true
fine_tune_type: lora
data: "{data_dir}"
seed: 20261106
num_layers: {num_layers}
batch_size: {batch_size}
iters: {iters}
val_batches: 4
learning_rate: {lr}
steps_per_report: 25
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

    # Package mixed-precision: 4-bit base weights + fp16 LoRA adapter in out_4bit_dir (auto-loaded by LocalModel)
    shutil.copy2(new_adapter_file, out_4bit_dir / "adapters.safetensors")
    if (adapter_out_dir / "adapter_config.json").is_file():
        shutil.copy2(adapter_out_dir / "adapter_config.json", out_4bit_dir / "adapter_config.json")

    total_bytes = sum(p.stat().st_size for p in out_4bit_dir.rglob("*") if p.is_file())
    return {
        "name": name,
        "family": family,
        "quantization_strategy": "mixed_precision_4bit_base_plus_fp16_lora_adapter",
        "fp16_base_model": str(fp16_base_model),
        "resume_adapter": str(resume_adapter),
        "resume_adapter_sha256": sha256_file(resume_adapter),
        "adapter_out_dir": str(adapter_out_dir),
        "new_adapter_sha256": sha256_file(new_adapter_file),
        "out_4bit_dir": str(out_4bit_dir),
        "out_4bit_bytes": total_bytes,
        "out_4bit_mib": round(total_bytes / (1024 * 1024), 2),
        "iters": iters,
        "learning_rate": lr,
        "batch_size": batch_size,
        "num_layers": num_layers,
        "seed": 20261106,
        "train_seconds": round(train_sec, 2),
        "quant_seconds": round(quant_sec, 2),
        "delta_verification": delta_info,
    }


def eval_generative_on_slices(model_path: str, family: str) -> dict[str, Any]:
    m = LocalModel(model_path, family=family, contract_version="v2")
    overnight = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "overnight-v1" / "routing-heldout.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    pos_all = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "dev-v6" / "cases-positive.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip() and json.loads(l).get("contract_version") == "v2"
    ]
    neg_all = [
        json.loads(l)
        for l in (ROOT_DIR / "data" / "dev-v6" / "cases-negative.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    rng = random.Random(7007)
    pos_100 = rng.sample(pos_all, 100)
    neg_100 = rng.sample(neg_all, 100)

    # Warmup
    m.infer(overnight[0]["query"], overnight[0].get("context"), max_tokens=80)

    res: dict[str, Any] = {}
    for split_name, subset in [("overnight_v1_80", overnight), ("dev_v6_pos_100", pos_100), ("dev_v6_neg_100", neg_100)]:
        raw_ok = ver_ok = repaired = damaged = unauth_w = 0
        lats = []
        for c in subset:
            out = m.infer(c["query"], c.get("context"), max_tokens=80)
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
            r_match = rc == exp
            v_match = vc == exp
            raw_ok += int(r_match)
            ver_ok += int(v_match)
            if not r_match and v_match:
                repaired += 1
            if r_match and not v_match:
                damaged += 1
        lats_s = sorted(lats)
        n = len(subset)
        res[split_name] = {
            "total": n,
            "track_a_raw_exact": raw_ok,
            "track_a_raw_accuracy": round(raw_ok / n, 4),
            "track_b_verified_exact": ver_ok,
            "track_b_verified_accuracy": round(ver_ok / n, 4),
            "repaired_by_verifier": repaired,
            "damaged_by_verifier": damaged,
            "warm_p50_ms": round(lats_s[n // 2], 2),
            "warm_p95_ms": round(lats_s[int(n * 0.95)], 2),
        }
    return res


if __name__ == "__main__":
    print("[v8] Building TrainPool-v8...")
    rows = build_train_v8()
    print(f"[v8] Built TrainPool-v8 with {len(rows)} examples.")

    print("[v8] Training ArrmesNeuralRouterV3 on TrainPool-v8...")
    nr_cfg = train_neural_router_v8(rows)
    print("[v8] ArrmesNeuralRouterV3 trained:", json.dumps(nr_cfg, indent=2))

    print("[v8] Building MLX QLoRA v8 datasets...")
    ds_stats = build_mlx_v8_datasets(rows)
    print("[v8] MLX dataset stats:", json.dumps(ds_stats, indent=2))

    fg_meta = train_mixed_precision_qlora_one(
        name="functiongemma-270m-arrmes-v8",
        family="functiongemma",
        fp16_base_model=MAIN_REPO / "models" / "functiongemma",
        resume_adapter=ROOT_DIR / "adapters" / "functiongemma-arrmes-v7" / "adapters.safetensors",
        adapter_out_dir=ROOT_DIR / "adapters" / "functiongemma-arrmes-v8",
        out_4bit_dir=ROOT_DIR / "models" / "functiongemma-270m-arrmes-v8-4bit",
        iters=100,
        lr=1.5e-5,
        batch_size=2,
        num_layers=-1,
        scale=16.0,
    )
    print("[v8] FunctionGemma v8 QLoRA trained:", json.dumps(fg_meta, indent=2))

    qw_meta = train_mixed_precision_qlora_one(
        name="qwen3-06b-arrmes-v8",
        family="hermes",
        fp16_base_model=MAIN_REPO / "models" / "qwen3-06b",
        resume_adapter=ROOT_DIR / "adapters" / "qwen3-06b-arrmes-v7" / "adapters.safetensors",
        adapter_out_dir=ROOT_DIR / "adapters" / "qwen3-06b-arrmes-v8",
        out_4bit_dir=ROOT_DIR / "models" / "qwen3-06b-arrmes-v8-4bit",
        iters=100,
        lr=1.2e-5,
        batch_size=1,
        num_layers=14,
        scale=8.0,
    )
    print("[v8] Qwen3-0.6B v8 QLoRA trained:", json.dumps(qw_meta, indent=2))

    print("[v8] Evaluating FunctionGemma-270M v8 (4-bit base + fp16 LoRA)...")
    fg_eval = eval_generative_on_slices(str(ROOT_DIR / "models" / "functiongemma-270m-arrmes-v8-4bit"), "functiongemma")

    print("[v8] Evaluating Qwen3-0.6B v8 (4-bit base + fp16 LoRA)...")
    qw_eval = eval_generative_on_slices(str(ROOT_DIR / "models" / "qwen3-06b-arrmes-v8-4bit"), "hermes")

    provenance = {
        "generated_at": "2026-11-06",
        "neural_router_v8": nr_cfg,
        "datasets": ds_stats,
        "checkpoints": {
            "functiongemma_270m_arrmes_v8": fg_meta,
            "qwen3_06b_arrmes_v8": qw_meta,
        },
        "evaluations": {
            "functiongemma_270m_v8_4bit_mixed": fg_eval,
            "qwen3_06b_v8_4bit_mixed": qw_eval,
        },
    }
    out_p = ROOT_DIR / "experiments" / "mlx-lora-v8-provenance.json"
    out_p.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print("[v8] Saved provenance to", out_p)
    print(json.dumps(provenance["evaluations"], indent=2))
