#!/usr/bin/env python3
"""Two-stage BF16 LoRA SFT + NLL-anchored RPO-DPO training & full-suite OOF/dev evaluation.

Uses exact static-prefix KV-cache expansion (verified 0.0 / <1e-5 loss & gradient parity
against full-sequence forward+backward) to train across the full 28-class TrainPool-v10
(12,620 SFT rows + 3,100 contrastive preference pairs) within a strict <= 8 GiB VRAM ceiling.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import gc
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from typing import Any

try:
    import torch
    import torch.nn.functional as F
    from torch.utils.checkpoint import checkpoint
    from transformers import AutoModelForCausalLM, AutoTokenizer, DynamicCache
    from transformers.masking_utils import create_causal_mask, create_sliding_window_causal_mask
    from peft import LoraConfig, get_peft_model
except ImportError:  # pragma: no cover - allows CPU/CI import of pure provenance helpers
    torch = None  # type: ignore[assignment]
    F = None  # type: ignore[assignment]
    checkpoint = None  # type: ignore[assignment]
    AutoModelForCausalLM = AutoTokenizer = DynamicCache = None  # type: ignore[assignment]
    create_causal_mask = create_sliding_window_causal_mask = None  # type: ignore[assignment]
    LoraConfig = get_peft_model = None  # type: ignore[assignment]

_no_grad = torch.no_grad if torch is not None else (lambda: (lambda fn: fn))

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

from arrmes.catalog import WRITE_EFFECT_ACTIONS_V3, tools_v2, tools_v3
from arrmes.evaluate import clopper_pearson_upper_bound, score, wilson_interval
from arrmes.model import finalize_output, messages
from arrmes.protocol import Call, encode

PIPELINE_VERSION = "arrmes-v10-clean-r1"
SEED = 20261111
MAX_SEQ_LENGTH = 4096


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()




def sha256_rows_canonical(rows: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update((json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
    return h.hexdigest()


def compute_oof_experiment_fingerprint(
    *,
    backbone_key: str,
    family: str,
    base_dir: Path,
    sft_rows: list[dict[str, Any]],
    dpo_rows: list[dict[str, Any]],
    oof_sft_epochs: int,
    dpo_epochs: int,
    seed: int = SEED,
    pipeline_version: str = PIPELINE_VERSION,
) -> str:
    base_artifacts: dict[str, str] = {}
    if base_dir.is_dir():
        for fp in sorted(base_dir.rglob("*")):
            rel_parts = fp.relative_to(base_dir).parts
            if fp.is_file() and not any(part.startswith(".") for part in rel_parts):
                base_artifacts["/".join(rel_parts)] = sha256_file(fp)
    if not base_artifacts:
        raise ValueError(f"base_dir {base_dir} contains no readable model/tokenizer artifacts for OOF fingerprinting")
    payload = {
        "pipeline_version": pipeline_version,
        "backbone_key": backbone_key,
        "family": family,
        "base_artifact_sha256s": base_artifacts,
        "sft_sha256": sha256_rows_canonical(sft_rows),
        "dpo_sha256": sha256_rows_canonical(dpo_rows),
        "oof_sft_epochs": oof_sft_epochs,
        "dpo_epochs": dpo_epochs,
        "max_seq_length": MAX_SEQ_LENGTH,
        "seed": seed,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_valid_oof_fold_checkpoints(
    work_dir: Path,
    backbone_key: str,
    expected_fingerprint: str,
) -> dict[int, dict[str, Any]]:
    fold_ckpt_path = work_dir / f"{backbone_key}_oof_ckpt.json"
    if not fold_ckpt_path.is_file():
        return {}
    try:
        entries = json.loads(fold_ckpt_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(entries, list):
        return {}
    valid: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("experiment_fingerprint") != expected_fingerprint:
            return {}
        f_idx = int(entry["fold"])
        rows_path = work_dir / f"{backbone_key}_oof_rows_fold{f_idx}.jsonl"
        if not rows_path.is_file() or sha256_file(rows_path) != entry.get("rows_sha256"):
            return {}
        valid[f_idx] = entry
    return valid


def wait_for_gpu_idle(poll_s: int = 12, max_wait_s: int = 14400) -> str:
    """Wait until no external GPU job has active SM > 5% and at least 7,600 MiB VRAM is free."""
    import os
    my_pid = os.getpid()
    t0 = time.monotonic()
    while True:
        free_res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False,
        )
        free_mib = int(free_res.stdout.strip().splitlines()[0].strip()) if free_res.stdout.strip() else 0
        pmon = subprocess.run(["nvidia-smi", "pmon", "-c", "3", "-s", "u"], capture_output=True, text=True, check=False)
        busy_pids = []
        for line in pmon.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 4 and parts[1].isdigit() and int(parts[1]) != my_pid:
                sm_s = parts[3]
                if sm_s.isdigit() and int(sm_s) > 5:
                    busy_pids.append(int(parts[1]))
        if not busy_pids and free_mib >= 7600:
            print(pmon.stdout, flush=True)
            return pmon.stdout
        if time.monotonic() - t0 > max_wait_s:
            raise RuntimeError(f"Timed out waiting for GPU (busy_pids={busy_pids}, free_mib={free_mib})")
        print(f"[gpu-gate] Waiting {poll_s}s (busy_sm_pids={sorted(set(busy_pids))}, free_mib={free_mib})...", flush=True)
        time.sleep(poll_s)


def expand_cache(pkv: Any, batch_size: int) -> Any:
    """Batch-expand a [1, H, P, D] KV cache preserving Gemma3/Qwen3 layer subclasses and autograd."""
    c = copy.copy(pkv)
    c.layers = []
    for lyr in pkv.layers:
        nl = copy.copy(lyr)
        nl.keys = lyr.keys.expand(batch_size, -1, -1, -1)
        nl.values = lyr.values.expand(batch_size, -1, -1, -1)
        c.layers.append(nl)
    return c


def forward_prefix_checkpointed(inner_body: Any, input_ids: torch.Tensor) -> DynamicCache:
    """Per-layer activation-checkpointed prefix forward returning exact DynamicCache (< 0.4 GiB VRAM)."""
    cfg = inner_body.config
    inputs_embeds = inner_body.embed_tokens(input_ids)
    pkv = DynamicCache(config=cfg)
    position_ids = torch.arange(inputs_embeds.shape[1], device=inputs_embeds.device).unsqueeze(0)
    mask_kwargs = {
        "config": cfg,
        "inputs_embeds": inputs_embeds,
        "attention_mask": None,
        "past_key_values": pkv,
        "position_ids": position_ids,
    }
    causal_mask_mapping = {"full_attention": create_causal_mask(**mask_kwargs)}
    if getattr(inner_body, "has_sliding_layers", False) or "sliding_attention" in getattr(cfg, "layer_types", []):
        causal_mask_mapping["sliding_attention"] = create_sliding_window_causal_mask(**mask_kwargs)

    hidden_states = inputs_embeds
    is_gemma3 = cfg.model_type == "gemma3_text"
    if is_gemma3:
        pos_embs = {lt: inner_body.rotary_emb(hidden_states, position_ids, lt) for lt in set(cfg.layer_types)}
    else:
        pos_emb_shared = inner_body.rotary_emb(hidden_states, position_ids)

    for i, decoder_layer in enumerate(inner_body.layers[: cfg.num_hidden_layers]):
        lt = cfg.layer_types[i]
        m_i = causal_mask_mapping[lt]
        pe_i = pos_embs[lt] if is_gemma3 else pos_emb_shared
        meta_box: dict[str, Any] = {}

        def _layer_fn(h: torch.Tensor, _dl=decoder_layer, _idx=i, _m=m_i, _pe=pe_i, _box=meta_box):
            tmp_c = DynamicCache(config=cfg)
            h_next = _dl(
                h,
                attention_mask=_m,
                position_embeddings=_pe,
                position_ids=position_ids,
                past_key_values=tmp_c,
                use_cache=True,
            )
            lyr_obj = tmp_c.layers[_idx]
            _box["meta"] = {k: v for k, v in vars(lyr_obj).items() if k not in ("keys", "values")}
            return h_next, lyr_obj.keys, lyr_obj.values

        hidden_states, k_i, v_i = checkpoint(_layer_fn, hidden_states, use_reentrant=False)
        for mk, mv in meta_box["meta"].items():
            setattr(pkv.layers[i], mk, mv)
        pkv.layers[i].keys = k_i
        pkv.layers[i].values = v_i
    return pkv


def make_detached_leaf_cache(pkv: DynamicCache) -> tuple[DynamicCache, list[torch.Tensor], list[torch.Tensor]]:
    """Detach prefix KV tensors as autograd leaves so micro-chunks accumulate KV grads cleanly."""
    leaf_pkv = copy.copy(pkv)
    leaf_pkv.layers = []
    orig_tensors: list[torch.Tensor] = []
    leaf_tensors: list[torch.Tensor] = []
    for lyr in pkv.layers:
        nl = copy.copy(lyr)
        lk = lyr.keys.detach().requires_grad_(True)
        lv = lyr.values.detach().requires_grad_(True)
        nl.keys = lk
        nl.values = lv
        leaf_pkv.layers.append(nl)
        orig_tensors.extend([lyr.keys, lyr.values])
        leaf_tensors.extend([lk, lv])
    return leaf_pkv, orig_tensors, leaf_tensors


class PrefixTokenizerHelper:
    def __init__(self, tokenizer: Any, family: str):
        self.tokenizer = tokenizer
        self.family = family
        self.eos_str = "<|im_end|>" if family == "hermes" else "<start_function_response>"
        self.stop_token_ids: set[int] = set()
        if isinstance(tokenizer.eos_token_id, int):
            self.stop_token_ids.add(tokenizer.eos_token_id)
        elif isinstance(tokenizer.eos_token_id, list):
            self.stop_token_ids.update(tokenizer.eos_token_id)
        eos_extra = tokenizer.convert_tokens_to_ids(self.eos_str)
        if isinstance(eos_extra, int) and eos_extra >= 0:
            self.stop_token_ids.add(eos_extra)
        if family == "functiongemma":
            eot = tokenizer.convert_tokens_to_ids("<end_of_turn>")
            if isinstance(eot, int) and eot >= 0:
                self.stop_token_ids.add(eot)
        self.pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else next(iter(self.stop_token_ids))

        self.prefix_ids: dict[str, list[int]] = {}
        for cv in ("v2", "v3"):
            ids_a = self.encode_prompt("AlphaQuery", {}, cv)
            ids_b = self.encode_prompt("BravoQuery", {"selected_media": {"title": "X", "media_type": "tv"}}, cv)
            common = 0
            for a, b in zip(ids_a, ids_b):
                if a == b:
                    common += 1
                else:
                    break
            # Keep at least 4 query tokens in suffix so q_len >= 1 always holds
            self.prefix_ids[cv] = ids_a[: max(1, common - 2)]

    def encode_prompt(self, query: str, context: dict[str, Any] | None, contract_version: str) -> list[int]:
        cv = "v3" if contract_version in {"v3", "arrmes.tools.v3"} else "v2"
        tool_defs = tools_v3() if cv == "v3" else tools_v2()
        kw = {"enable_thinking": False} if self.family == "hermes" else {}
        rendered = self.tokenizer.apply_chat_template(
            messages(query, context or {}, self.family, contract_version=cv),
            tools=tool_defs,
            tokenize=False,
            add_generation_prompt=True,
            **kw,
        )
        add_sp = self.tokenizer.bos_token is not None and not rendered.startswith(self.tokenizer.bos_token)
        return self.tokenizer.encode(rendered, add_special_tokens=add_sp)

    def build_sft_item(self, row: dict[str, Any]) -> dict[str, Any]:
        cv = "v3" if row.get("effective_prompt_contract") in {"v3", "arrmes.tools.v3"} else "v2"
        p_ids = self.encode_prompt(row["query"], row.get("context"), cv)
        P = len(self.prefix_ids[cv])
        assert p_ids[:P] == self.prefix_ids[cv], f"Prefix mismatch on {row.get('id')}"
        comp_text = encode(Call(row["expected"]["name"], row["expected"]["arguments"]), self.family) + self.eos_str
        c_ids = self.tokenizer.encode(comp_text, add_special_tokens=False)
        full_len = len(p_ids) + len(c_ids)
        if full_len > MAX_SEQ_LENGTH:
            raise ValueError(f"Sequence length {full_len} > {MAX_SEQ_LENGTH} on {row.get('id')}")
        q_suff = p_ids[P:]
        assert len(q_suff) >= 1
        suff_ids = q_suff + c_ids
        labels = ([-100] * len(q_suff)) + c_ids
        return {
            "id": row["id"],
            "cv": cv,
            "fold": int(row["fold"]),
            "suff_ids": suff_ids,
            "labels": labels,
            "row": row,
        }

    def build_dpo_item(self, row: dict[str, Any]) -> dict[str, Any]:
        cv = "v3" if row.get("effective_prompt_contract") in {"v3", "arrmes.tools.v3"} else "v2"
        p_ids = self.encode_prompt(row["query"], row.get("context"), cv)
        P = len(self.prefix_ids[cv])
        assert p_ids[:P] == self.prefix_ids[cv]
        q_suff = p_ids[P:]
        ch_ids = self.tokenizer.encode(row[f"chosen_{self.family}"] + self.eos_str, add_special_tokens=False)
        rj_ids = self.tokenizer.encode(row[f"rejected_{self.family}"] + self.eos_str, add_special_tokens=False)
        return {
            "id": row["id"],
            "cv": cv,
            "fold": int(row["fold"]),
            "ch_suff": q_suff + ch_ids,
            "ch_labels": ([-100] * len(q_suff)) + ch_ids,
            "rj_suff": q_suff + rj_ids,
            "rj_labels": ([-100] * len(q_suff)) + rj_ids,
        }


def make_lora_model(base_dir: Path, device: torch.device) -> Any:
    base = AutoModelForCausalLM.from_pretrained(str(base_dir), dtype=torch.bfloat16)
    base.to(device)
    lcfg = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(base, lcfg)
    model.train()
    return model


def sequence_logps_given_pkv(
    inner_body: Any,
    lm_head: Any,
    base_pkv: DynamicCache,
    prefix_len: int,
    suff_list: list[list[int]],
    lbl_list: list[list[int]],
    pad_id: int,
    device: torch.device,
) -> tuple[torch.Tensor, torch.Tensor, int]:
    """Compute exact (seq_logp [B], tok_logp_sum scalar, valid_tok_count int) running lm_head ONLY on target positions."""
    B = len(suff_list)
    P = prefix_len
    max_s = max(len(s) for s in suff_list)
    suff = torch.full((B, max_s), pad_id, dtype=torch.long, device=device)
    labels = torch.full((B, max_s), -100, dtype=torch.long, device=device)
    attn_s = torch.zeros((B, max_s), dtype=torch.long, device=device)
    for i, (s, l) in enumerate(zip(suff_list, lbl_list)):
        L = len(s)
        suff[i, :L] = torch.tensor(s, dtype=torch.long, device=device)
        labels[i, :L] = torch.tensor(l, dtype=torch.long, device=device)
        attn_s[i, :L] = 1

    full_attn = torch.cat([torch.ones((B, P), dtype=torch.long, device=device), attn_s], dim=1)
    ec = expand_cache(base_pkv, B)
    cache_pos = torch.arange(P, P + max_s, dtype=torch.long, device=device)
    h = inner_body(
        input_ids=suff,
        attention_mask=full_attn,
        past_key_values=ec,
        cache_position=cache_pos,
        use_cache=False,
    ).last_hidden_state
    del ec

    shift_h = h[:, :-1, :]
    shift_labels = labels[:, 1:]
    valid_mask = shift_labels != -100
    n_valid = int(valid_mask.sum().item())
    if n_valid == 0:
        z = shift_h.sum() * 0.0
        return z.expand(B), z, 0

    valid_logits = lm_head(shift_h[valid_mask])
    del h, shift_h
    flat_nll = F.cross_entropy(valid_logits.float(), shift_labels[valid_mask], reduction="none")
    del valid_logits

    tok_logp_mat = torch.zeros_like(shift_labels, dtype=torch.float32)
    tok_logp_mat[valid_mask] = -flat_nll
    seq_logp = tok_logp_mat.sum(dim=-1)
    return seq_logp, seq_logp.sum(), n_valid


def _make_dynamic_microbatches(items: list[dict[str, Any]], key: str, max_bs: int, single_thresh: int = 220) -> list[list[dict[str, Any]]]:
    ordered = sorted(items, key=lambda x: len(x[key]))
    micros: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for it in ordered:
        lim = 1 if len(it[key]) > single_thresh else max_bs
        if cur and (len(cur) >= lim or len(cur[-1][key]) > single_thresh):
            micros.append(cur)
            cur = []
        cur.append(it)
    if cur:
        micros.append(cur)
    return micros


def train_two_stage(
    base_dir: Path,
    helper: PrefixTokenizerHelper,
    sft_items: list[dict[str, Any]],
    dpo_items: list[dict[str, Any]],
    device: torch.device,
    *,
    sft_epochs: int = 3,
    sft_batch_size: int = 64,
    sft_lr: float = 3.5e-4,
    dpo_epochs: int = 1,
    dpo_batch_pairs: int = 16,
    dpo_lr: float = 4e-5,
    dpo_beta: float = 0.1,
    dpo_lambda_nll: float = 0.20,
    seed: int = SEED,
    tag: str = "run",
) -> tuple[Any, dict[str, Any]]:
    rng = random.Random(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    model = make_lora_model(base_dir, device)
    causal_lm = model.get_base_model()
    inner_body = causal_lm.model
    lm_head = causal_lm.lm_head
    is_gemma3 = inner_body.config.model_type == "gemma3_text"
    max_micro_bs = 4 if is_gemma3 else 2
    dpo_max_steps: int | None = None
    if is_gemma3:
        # Gemma-3 270M has 4 RMSNorms/layer (pre+post attn/MLP) and a 100M non-embedding body;
        # calibrated SFT LR + bounded 20-step 1:1 positive-anchored RPO-DPO polish sharpens
        # hard-negative abstention without drifting positive tool call recall.
        sft_lr = 1.5e-4
        dpo_lr = 1.5e-6
        dpo_beta = 0.25
        dpo_lambda_nll = 2.0
        dpo_max_steps = 20

    pref_tensors = {
        cv: torch.tensor([helper.prefix_ids[cv]], dtype=torch.long, device=device)
        for cv in ("v2", "v3")
    }
    pref_lens = {cv: len(helper.prefix_ids[cv]) for cv in ("v2", "v3")}

    by_cv: dict[str, list[dict[str, Any]]] = {"v2": [], "v3": []}
    for it in sft_items:
        by_cv[it["cv"]].append(it)

    batches_per_epoch = sum(math.ceil(len(v) / sft_batch_size) for v in by_cv.values())
    total_sft_steps = max(1, sft_epochs * batches_per_epoch)
    warmup_sft = max(1, int(0.05 * total_sft_steps))

    trainable_params = [p for p in model.parameters() if p.requires_grad]
    opt_sft = torch.optim.AdamW(trainable_params, lr=sft_lr, weight_decay=0.01)

    def lr_lambda_sft(step: int) -> float:
        if step < warmup_sft:
            return float(step + 1) / float(warmup_sft)
        prog = (step - warmup_sft) / max(1, total_sft_steps - warmup_sft)
        return 0.05 + 0.95 * 0.5 * (1.0 + math.cos(math.pi * prog))

    sched_sft = torch.optim.lr_scheduler.LambdaLR(opt_sft, lr_lambda_sft)

    t0 = time.perf_counter()
    sft_epoch_losses: list[float] = []
    step_idx = 0
    for ep in range(sft_epochs):
        all_batches: list[tuple[str, list[dict[str, Any]]]] = []
        for cv in ("v2", "v3"):
            pool = list(by_cv[cv])
            rng.shuffle(pool)
            for idx in range(0, len(pool), sft_batch_size):
                all_batches.append((cv, pool[idx : idx + sft_batch_size]))
        rng.shuffle(all_batches)
        ep_loss_sum = 0.0
        for cv, batch in all_batches:
            opt_sft.zero_grad(set_to_none=True)
            total_valid = max(1, sum(sum(1 for y in x["labels"][1:] if y != -100) for x in batch))
            pkv = forward_prefix_checkpointed(inner_body, pref_tensors[cv])
            leaf_pkv, orig_t, leaf_t = make_detached_leaf_cache(pkv)
            batch_loss_val = 0.0
            for sub in _make_dynamic_microbatches(batch, "suff_ids", max_micro_bs):
                _, tok_sum, _ = sequence_logps_given_pkv(
                    inner_body,
                    lm_head,
                    leaf_pkv,
                    pref_lens[cv],
                    [x["suff_ids"] for x in sub],
                    [x["labels"] for x in sub],
                    helper.pad_id,
                    device,
                )
                c_loss = -tok_sum / float(total_valid)
                c_loss.backward()
                batch_loss_val += float(c_loss.detach().item())
                del tok_sum, c_loss
            grads = [t.grad if t.grad is not None else torch.zeros_like(t) for t in leaf_t]
            torch.autograd.backward(orig_t, grads)
            del pkv, leaf_pkv, orig_t, leaf_t, grads
            torch.nn.utils.clip_grad_norm_(trainable_params, 1.0)
            opt_sft.step()
            sched_sft.step()
            ep_loss_sum += batch_loss_val
            step_idx += 1
        mean_ep = round(ep_loss_sum / max(1, len(all_batches)), 6)
        sft_epoch_losses.append(mean_ep)
        print(f"[{tag}] SFT epoch {ep+1}/{sft_epochs} loss={mean_ep:.5f} peak_vram={torch.cuda.max_memory_allocated(device)/(1024*1024):.0f}MiB elapsed={time.perf_counter()-t0:.1f}s", flush=True)

    # Precompute exact Stage-1 reference logps under no_grad with B<=2 and valid-only lm_head (< 2.5 GiB VRAM!)
    model.eval()
    torch.cuda.empty_cache()
    ref_logps: dict[str, tuple[float, float]] = {}
    dpo_by_cv: dict[str, list[dict[str, Any]]] = {"v2": [], "v3": []}
    for dit in dpo_items:
        dpo_by_cv[dit["cv"]].append(dit)

    with torch.no_grad():
        for cv in ("v2", "v3"):
            pool = dpo_by_cv[cv]
            if not pool:
                continue
            ref_pkv = inner_body(input_ids=pref_tensors[cv], use_cache=True).past_key_values
            for item in pool:
                if max(len(item["ch_suff"]), len(item["rj_suff"])) <= 220:
                    seq_lp, _, _ = sequence_logps_given_pkv(
                        inner_body, lm_head, ref_pkv, pref_lens[cv],
                        [item["ch_suff"], item["rj_suff"]],
                        [item["ch_labels"], item["rj_labels"]],
                        helper.pad_id, device,
                    )
                    ref_logps[item["id"]] = (float(seq_lp[0].item()), float(seq_lp[1].item()))
                else:
                    ch_lp, _, _ = sequence_logps_given_pkv(
                        inner_body, lm_head, ref_pkv, pref_lens[cv],
                        [item["ch_suff"]], [item["ch_labels"]], helper.pad_id, device,
                    )
                    rj_lp, _, _ = sequence_logps_given_pkv(
                        inner_body, lm_head, ref_pkv, pref_lens[cv],
                        [item["rj_suff"]], [item["rj_labels"]], helper.pad_id, device,
                    )
                    ref_logps[item["id"]] = (float(ch_lp[0].item()), float(rj_lp[0].item()))
            del ref_pkv
    torch.cuda.empty_cache()

    # Stage 2: NLL-Anchored RPO-DPO
    model.train()
    dpo_batches_per_ep = sum(math.ceil(len(v) / dpo_batch_pairs) for v in dpo_by_cv.values())
    total_dpo_steps = max(1, dpo_epochs * dpo_batches_per_ep)
    warmup_dpo = max(1, int(0.10 * total_dpo_steps))
    opt_dpo = torch.optim.AdamW(trainable_params, lr=dpo_lr, weight_decay=0.01)

    def lr_lambda_dpo(step: int) -> float:
        if step < warmup_dpo:
            return float(step + 1) / float(warmup_dpo)
        prog = (step - warmup_dpo) / max(1, total_dpo_steps - warmup_dpo)
        return 0.10 + 0.90 * 0.5 * (1.0 + math.cos(math.pi * prog))

    sched_dpo = torch.optim.lr_scheduler.LambdaLR(opt_dpo, lr_lambda_dpo)
    dpo_epoch_losses: list[float] = []
    dpo_pref_accs: list[float] = []

    for ep in range(dpo_epochs):
        all_dpo_batches: list[tuple[str, list[dict[str, Any]]]] = []
        for cv in ("v2", "v3"):
            pool = list(dpo_by_cv[cv])
            rng.shuffle(pool)
            for idx in range(0, len(pool), dpo_batch_pairs):
                all_dpo_batches.append((cv, pool[idx : idx + dpo_batch_pairs]))
        rng.shuffle(all_dpo_batches)
        if dpo_max_steps is not None:
            all_dpo_batches = all_dpo_batches[:dpo_max_steps]
        ep_dpo_loss = 0.0
        pref_wins = 0
        pref_tot = 0
        for cv, batch in all_dpo_batches:
            opt_dpo.zero_grad(set_to_none=True)
            k_tot = len(batch)
            total_ch_valid = max(1, sum(sum(1 for y in x["ch_labels"][1:] if y != -100) for x in batch))
            pkv = forward_prefix_checkpointed(inner_body, pref_tensors[cv])
            leaf_pkv, orig_t, leaf_t = make_detached_leaf_cache(pkv)
            b_loss_val = 0.0
            for item in batch:
                r_ch, r_rj = ref_logps[item["id"]]
                if max(len(item["ch_suff"]), len(item["rj_suff"])) <= 220:
                    seq_lp, _, _ = sequence_logps_given_pkv(
                        inner_body, lm_head, leaf_pkv, pref_lens[cv],
                        [item["ch_suff"], item["rj_suff"]],
                        [item["ch_labels"], item["rj_labels"]],
                        helper.pad_id, device,
                    )
                    ch_lp, rj_lp = seq_lp[0], seq_lp[1]
                    margin = dpo_beta * ((ch_lp - r_ch) - (rj_lp - r_rj))
                    p_loss = (-F.logsigmoid(margin) / float(k_tot)) + dpo_lambda_nll * (-ch_lp / float(total_ch_valid))
                    p_loss.backward()
                    b_loss_val += float(p_loss.detach().item())
                    pref_wins += int(float(margin.detach().item()) > 0.0)
                    del seq_lp, ch_lp, rj_lp, margin, p_loss
                else:
                    # Run chosen & rejected in two B=1 forwards so long rows stay < 4.5 GiB!
                    with torch.no_grad():
                        ch_det, _, _ = sequence_logps_given_pkv(
                            inner_body, lm_head, leaf_pkv, pref_lens[cv],
                            [item["ch_suff"]], [item["ch_labels"]], helper.pad_id, device,
                        )
                        rj_det, _, _ = sequence_logps_given_pkv(
                            inner_body, lm_head, leaf_pkv, pref_lens[cv],
                            [item["rj_suff"]], [item["rj_labels"]], helper.pad_id, device,
                        )
                        m_val = dpo_beta * ((float(ch_det[0].item()) - r_ch) - (float(rj_det[0].item()) - r_rj))
                        sig_neg = float(torch.sigmoid(torch.tensor(-m_val)).item())
                    # Exact analytical chain rule split into two separate B=1 backwards!
                    w_ch = -(dpo_beta * sig_neg / float(k_tot)) - (dpo_lambda_nll / float(total_ch_valid))
                    w_rj = +(dpo_beta * sig_neg / float(k_tot))
                    ch_lp1, _, _ = sequence_logps_given_pkv(
                        inner_body, lm_head, leaf_pkv, pref_lens[cv],
                        [item["ch_suff"]], [item["ch_labels"]], helper.pad_id, device,
                    )
                    (w_ch * ch_lp1[0]).backward()
                    del ch_lp1
                    rj_lp1, _, _ = sequence_logps_given_pkv(
                        inner_body, lm_head, leaf_pkv, pref_lens[cv],
                        [item["rj_suff"]], [item["rj_labels"]], helper.pad_id, device,
                    )
                    (w_rj * rj_lp1[0]).backward()
                    del rj_lp1
                    true_p_loss = float(-F.logsigmoid(torch.tensor(m_val)).item() / float(k_tot)) + dpo_lambda_nll * (-float(ch_det[0].item()) / float(total_ch_valid))
                    b_loss_val += true_p_loss
                    pref_wins += int(m_val > 0.0)
                pref_tot += 1
            if is_gemma3 and by_cv[cv]:
                pos_pool = [x for x in by_cv[cv] if x["row"]["expected"]["name"] not in {"clarify", "unsupported"}]
                if pos_pool:
                    anc_sub = rng.sample(pos_pool, k=min(12, len(pos_pool)))
                    _, anc_tok_sum, anc_valid = sequence_logps_given_pkv(
                        inner_body, lm_head, leaf_pkv, pref_lens[cv],
                        [x["suff_ids"] for x in anc_sub],
                        [x["labels"] for x in anc_sub],
                        helper.pad_id, device,
                    )
                    if anc_valid > 0:
                        anc_loss = 1.5 * (-anc_tok_sum / float(anc_valid))
                        anc_loss.backward()
                        b_loss_val += float(anc_loss.detach().item())
                        del anc_loss
                    del anc_tok_sum
            grads = [t.grad if t.grad is not None else torch.zeros_like(t) for t in leaf_t]
            torch.autograd.backward(orig_t, grads)
            del pkv, leaf_pkv, orig_t, leaf_t, grads
            torch.nn.utils.clip_grad_norm_(trainable_params, 1.0)
            opt_dpo.step()
            sched_dpo.step()
            ep_dpo_loss += b_loss_val
        mean_dpo = round(ep_dpo_loss / max(1, len(all_dpo_batches)), 6)
        p_acc = round(pref_wins / max(1, pref_tot), 6)
        dpo_epoch_losses.append(mean_dpo)
        dpo_pref_accs.append(p_acc)
        print(f"[{tag}] RPO-DPO epoch {ep+1}/{dpo_epochs} loss={mean_dpo:.5f} pref_acc={p_acc:.4f} peak_vram={torch.cuda.max_memory_allocated(device)/(1024*1024):.0f}MiB elapsed={time.perf_counter()-t0:.1f}s", flush=True)

    peak_vram_mib = round(torch.cuda.max_memory_allocated(device) / (1024 * 1024), 2)
    stats = {
        "sft_epochs": sft_epochs,
        "sft_steps": step_idx,
        "sft_epoch_losses": sft_epoch_losses,
        "dpo_epochs": dpo_epochs,
        "dpo_epoch_losses": dpo_epoch_losses,
        "dpo_preference_accuracy": dpo_pref_accs,
        "train_wall_seconds": round(time.perf_counter() - t0, 3),
        "peak_vram_mib": peak_vram_mib,
    }
    return model, stats


@_no_grad()
def evaluate_cases_greedy(
    model: Any,
    helper: PrefixTokenizerHelper,
    cases: list[dict[str, Any]],
    device: torch.device,
    *,
    default_v3: bool = False,
    use_effective_contract: bool = False,
    max_new_tokens: int = 128,
    batch_size: int = 4,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Exact zero-padding bucketed greedy decoding running lm_head ONLY on the last token (< 2.5 GiB VRAM)."""
    model.eval()
    torch.cuda.empty_cache()
    t0 = time.perf_counter()

    causal_lm = model.get_base_model() if hasattr(model, "get_base_model") else model
    inner_body = causal_lm.model
    lm_head = causal_lm.lm_head

    pref_pkvs: dict[str, Any] = {}
    for cv in ("v2", "v3"):
        pt = torch.tensor([helper.prefix_ids[cv]], dtype=torch.long, device=device)
        pref_pkvs[cv] = inner_body(input_ids=pt, use_cache=True).past_key_values

    buckets: dict[tuple[str, int], list[tuple[int, list[int], str]]] = defaultdict(list)
    for idx, c in enumerate(cases):
        if use_effective_contract and c.get("effective_prompt_contract") in {"v2", "v3"}:
            cv = c["effective_prompt_contract"]
        else:
            cv = "v3" if (default_v3 or c.get("contract_version") in {"v3", "arrmes.tools.v3"}) else "v2"
        p_ids = helper.encode_prompt(c["query"], c.get("context"), cv)
        P = len(helper.prefix_ids[cv])
        q_suff = p_ids[P:]
        buckets[(cv, len(q_suff))].append((idx, q_suff, cv))

    raw_outputs: list[tuple[str, str]] = [("", "v2")] * len(cases)
    stop_ids = helper.stop_token_ids

    for (cv, s_len), items in buckets.items():
        P = len(helper.prefix_ids[cv])
        base_pkv = pref_pkvs[cv]
        eff_bs = 1 if s_len > 250 else batch_size
        for b_start in range(0, len(items), eff_bs):
            chunk = items[b_start : b_start + eff_bs]
            B = len(chunk)
            ec = expand_cache(base_pkv, B)
            q_tensor = torch.tensor([x[1] for x in chunk], dtype=torch.long, device=device)
            cache_pos = torch.arange(P, P + s_len, dtype=torch.long, device=device)
            out = inner_body(input_ids=q_tensor, past_key_values=ec, cache_position=cache_pos, use_cache=True)
            pkv = out.past_key_values
            next_tok = torch.argmax(lm_head(out.last_hidden_state[:, -1:, :])[:, 0, :], dim=-1)

            gen_lists: list[list[int]] = [[] for _ in range(B)]
            done = [False] * B
            cur_pos = P + s_len

            for step in range(max_new_tokens):
                tok_cpu = next_tok.tolist()
                for bi, tid in enumerate(tok_cpu):
                    if not done[bi]:
                        if tid in stop_ids:
                            done[bi] = True
                        else:
                            gen_lists[bi].append(tid)
                if all(done) or step == max_new_tokens - 1:
                    break
                c_pos = torch.tensor([cur_pos], dtype=torch.long, device=device)
                out = inner_body(input_ids=next_tok.unsqueeze(-1), past_key_values=pkv, cache_position=c_pos, use_cache=True)
                pkv = out.past_key_values
                next_tok = torch.argmax(lm_head(out.last_hidden_state[:, -1:, :])[:, 0, :], dim=-1)
                cur_pos += 1
            del pkv, ec, out

            for bi, (orig_idx, _, item_cv) in enumerate(chunk):
                txt = helper.tokenizer.decode(gen_lists[bi], skip_special_tokens=False)
                raw_outputs[orig_idx] = (txt, item_cv)

    del pref_pkvs
    torch.cuda.empty_cache()

    # Score raw and verified
    raw_exact = 0
    ver_exact = 0
    pos_n = pos_raw = pos_ver = 0
    neg_n = neg_raw = neg_ver = 0
    unauth_raw = 0
    unauth_ver = 0
    damaged = 0
    per_row: list[dict[str, Any]] = []

    for idx, c in enumerate(cases):
        raw_unv, cv = raw_outputs[idx]
        # Score against case's true contract_version (not effective_prompt_contract if v2 row shown v3 tools)
        eval_v3 = default_v3 or (c.get("contract_version") in {"v3", "arrmes.tools.v3"}) or (c["expected"]["name"] in {"service_query", "bazarr_subtitle_search", "bazarr_subtitle_download"})
        ver_cv = "v3" if eval_v3 else "v2"
        raw_ver = finalize_output(raw_unv, c["query"], c.get("context"), helper.family, ver_cv)

        r_sc = score(raw_unv, c["expected"], helper.family, allow_v3=eval_v3)
        v_sc = score(raw_ver, c["expected"], helper.family, allow_v3=eval_v3)

        r_ok = bool(r_sc["exact"])
        v_ok = bool(v_sc["exact"])
        raw_exact += int(r_ok)
        ver_exact += int(v_ok)

        is_neg = c["expected"]["name"] in {"clarify", "unsupported"}
        if is_neg:
            neg_n += 1
            neg_raw += int(r_ok)
            neg_ver += int(v_ok)
        else:
            pos_n += 1
            pos_raw += int(r_ok)
            pos_ver += int(v_ok)

        exp_is_write = c["expected"]["name"] in WRITE_EFFECT_ACTIONS_V3
        if not exp_is_write:
            if (r_sc.get("actual") or {}).get("name") in WRITE_EFFECT_ACTIONS_V3:
                unauth_raw += 1
            if (v_sc.get("actual") or {}).get("name") in WRITE_EFFECT_ACTIONS_V3:
                unauth_ver += 1
        if r_ok and not v_ok:
            damaged += 1

        per_row.append({
            "id": c.get("id", f"idx-{idx}"),
            "is_negative": is_neg,
            "raw_exact": r_ok,
            "verified_exact": v_ok,
            "raw_unverified": raw_unv,
            "raw_verified": raw_ver,
        })

    n_tot = len(cases)
    summary = {
        "cases": n_tot,
        "raw_exact": raw_exact,
        "raw_accuracy": round(raw_exact / max(1, n_tot), 6),
        "raw_wilson_95": wilson_interval(raw_exact, n_tot),
        "verified_exact": ver_exact,
        "verified_accuracy": round(ver_exact / max(1, n_tot), 6),
        "verified_wilson_95": wilson_interval(ver_exact, n_tot),
        "positive_cases": pos_n,
        "positive_raw_exact": pos_raw,
        "positive_raw_accuracy": round(pos_raw / max(1, pos_n), 6) if pos_n else None,
        "positive_verified_exact": pos_ver,
        "positive_verified_accuracy": round(pos_ver / max(1, pos_n), 6) if pos_n else None,
        "negative_cases": neg_n,
        "negative_raw_exact": neg_raw,
        "negative_raw_accuracy": round(neg_raw / max(1, neg_n), 6) if neg_n else None,
        "negative_verified_exact": neg_ver,
        "negative_verified_accuracy": round(neg_ver / max(1, neg_n), 6) if neg_n else None,
        "unauthorized_writes_raw": unauth_raw,
        "unauthorized_writes_verified": unauth_ver,
        "unauthorized_writes_verified_cp95_upper": clopper_pearson_upper_bound(unauth_ver, max(1, neg_n or n_tot)),
        "verifier_damaged": damaged,
        "eval_wall_seconds": round(time.perf_counter() - t0, 3),
    }
    return summary, per_row


def merge_and_save_bf16(model: Any, tokenizer: Any, out_dir: Path, base_dir: Path | None = None) -> dict[str, Any]:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged = model.merge_and_unload()
    merged.to(torch.bfloat16)
    merged.save_pretrained(str(out_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(out_dir))
    if base_dir is not None and (base_dir / "chat_template.jinja").is_file() and not (out_dir / "chat_template.jinja").is_file():
        shutil.copy2(base_dir / "chat_template.jinja", out_dir / "chat_template.jinja")
    for fp in out_dir.iterdir():
        if fp.is_file():
            fp.chmod(0o644)
    st_files = sorted(out_dir.glob("*.safetensors"))
    total_bytes = sum(f.stat().st_size for f in st_files)
    digests = {f.name: sha256_file(f) for f in st_files}
    return {
        "merged_dir": out_dir.name,
        "safetensors_bytes": total_bytes,
        "safetensors_mib": round(total_bytes / (1024 * 1024), 2),
        "safetensors_sha256": digests,
    }


def run_backbone_pipeline(
    work_dir: Path,
    backbone_key: str,
    family: str,
    base_dir: Path,
    merged_out_dir: Path,
    sft_rows: list[dict[str, Any]],
    dpo_rows: list[dict[str, Any]],
    av7_pos: list[dict[str, Any]],
    av7_neg: list[dict[str, Any]],
    ov1_rows: list[dict[str, Any]],
    device: torch.device,
    *,
    oof_sft_epochs: int = 2,
    full_sft_epochs: int = 3,
) -> dict[str, Any]:
    print(f"\n==================== {backbone_key} ({family}) ====================", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(str(base_dir))
    helper = PrefixTokenizerHelper(tokenizer, family)
    print(f"[{backbone_key}] Tokenizing {len(sft_rows)} SFT + {len(dpo_rows)} DPO items (prefix v2={len(helper.prefix_ids['v2'])}, v3={len(helper.prefix_ids['v3'])})...", flush=True)
    sft_items = [helper.build_sft_item(r) for r in sft_rows]
    dpo_items = [helper.build_dpo_item(r) for r in dpo_rows]

    # 1. 5-fold Template-Skeleton GroupKFold OOF (bound to exact experiment_fingerprint + persisted fold rows)
    exp_fingerprint = compute_oof_experiment_fingerprint(
        backbone_key=backbone_key,
        family=family,
        base_dir=base_dir,
        sft_rows=sft_rows,
        dpo_rows=dpo_rows,
        oof_sft_epochs=oof_sft_epochs,
        dpo_epochs=1,
        seed=SEED,
    )
    fold_ckpt_path = work_dir / f"{backbone_key}_oof_ckpt.json"
    saved_folds = load_valid_oof_fold_checkpoints(work_dir, backbone_key, exp_fingerprint)

    fold_summaries: list[dict[str, Any]] = []
    oof_pos_n = oof_pos_raw = oof_pos_ver = 0
    oof_neg_n = oof_neg_raw = oof_neg_ver = 0
    oof_unauth_raw = oof_unauth_ver = oof_damaged = 0

    for f in range(5):
        if f in saved_folds:
            f_entry = saved_folds[f]
            f_eval = f_entry["eval"]
            print(f"[{backbone_key}-fold{f}] Restored completed fold from checkpoint: neg_raw={f_eval['negative_raw_accuracy']}", flush=True)
            fold_summaries.append(f_entry)
            oof_pos_n += f_eval["positive_cases"]
            oof_pos_raw += f_eval["positive_raw_exact"]
            oof_pos_ver += f_eval["positive_verified_exact"]
            oof_neg_n += f_eval["negative_cases"]
            oof_neg_raw += f_eval["negative_raw_exact"]
            oof_neg_ver += f_eval["negative_verified_exact"]
            oof_unauth_raw += f_eval["unauthorized_writes_raw"]
            oof_unauth_ver += f_eval["unauthorized_writes_verified"]
            oof_damaged += f_eval["verifier_damaged"]
            continue
        tr_sft = [x for x in sft_items if x["fold"] != f]
        va_rows = [x["row"] for x in sft_items if x["fold"] == f]
        tr_dpo = [x for x in dpo_items if x["fold"] != f]

        torch.cuda.reset_peak_memory_stats(device)
        f_model, f_train_stats = train_two_stage(
            base_dir,
            helper,
            tr_sft,
            tr_dpo,
            device,
            sft_epochs=oof_sft_epochs,
            dpo_epochs=1,
            seed=SEED + f,
            tag=f"{backbone_key}-fold{f}",
        )
        f_eval, f_rows = evaluate_cases_greedy(
            f_model,
            helper,
            va_rows,
            device,
            use_effective_contract=True,
        )
        print(
            f"[{backbone_key}-fold{f}] OOF pos_raw={f_eval['positive_raw_accuracy']} pos_ver={f_eval['positive_verified_accuracy']} "
            f"neg_raw={f_eval['negative_raw_accuracy']} neg_ver={f_eval['negative_verified_accuracy']} "
            f"unauth_ver={f_eval['unauthorized_writes_verified']} damaged={f_eval['verifier_damaged']}",
            flush=True,
        )
        # Hard ROI Circuit Breaker check on Qwen3 Fold 0 immediately!
        if backbone_key == "qwen3_06b_v10" and f == 0:
            if (f_eval["negative_raw_accuracy"] or 0.0) < 0.7967:
                raise RuntimeError(
                    f"HARD ROI CIRCUIT BREAKER TRIGGERED on {backbone_key} fold 0: "
                    f"negative_raw_accuracy={f_eval['negative_raw_accuracy']} (< +35.0pp over 0.4467 baseline)"
                )

        fold_rows_path = work_dir / f"{backbone_key}_oof_rows_fold{f}.jsonl"
        fold_rows_path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in f_rows),
            encoding="utf-8",
        )
        fold_summaries.append({
            "fold": f,
            "experiment_fingerprint": exp_fingerprint,
            "rows_file": fold_rows_path.name,
            "rows_sha256": sha256_file(fold_rows_path),
            "train": f_train_stats,
            "eval": f_eval,
        })
        fold_ckpt_path.write_text(json.dumps(fold_summaries, indent=2) + "\n", encoding="utf-8")
        oof_pos_n += f_eval["positive_cases"]
        oof_pos_raw += f_eval["positive_raw_exact"]
        oof_pos_ver += f_eval["positive_verified_exact"]
        oof_neg_n += f_eval["negative_cases"]
        oof_neg_raw += f_eval["negative_raw_exact"]
        oof_neg_ver += f_eval["negative_verified_exact"]
        oof_unauth_raw += f_eval["unauthorized_writes_raw"]
        oof_unauth_ver += f_eval["unauthorized_writes_verified"]
        oof_damaged += f_eval["verifier_damaged"]

        del f_model
        gc.collect()
        torch.cuda.empty_cache()

    oof_tot = oof_pos_n + oof_neg_n
    oof_aggregate = {
        "folds": 5,
        "total_cases": oof_tot,
        "overall_raw_accuracy": round((oof_pos_raw + oof_neg_raw) / max(1, oof_tot), 6),
        "overall_verified_accuracy": round((oof_pos_ver + oof_neg_ver) / max(1, oof_tot), 6),
        "positive_cases": oof_pos_n,
        "positive_raw_exact": oof_pos_raw,
        "positive_raw_accuracy": round(oof_pos_raw / max(1, oof_pos_n), 6),
        "positive_verified_exact": oof_pos_ver,
        "positive_verified_accuracy": round(oof_pos_ver / max(1, oof_pos_n), 6),
        "negative_cases": oof_neg_n,
        "negative_raw_exact": oof_neg_raw,
        "negative_raw_accuracy": round(oof_neg_raw / max(1, oof_neg_n), 6),
        "negative_verified_exact": oof_neg_ver,
        "negative_verified_accuracy": round(oof_neg_ver / max(1, oof_neg_n), 6),
        "unauthorized_writes_raw": oof_unauth_raw,
        "unauthorized_writes_verified": oof_unauth_ver,
        "verifier_damaged": oof_damaged,
        "per_fold": fold_summaries,
    }
    if backbone_key == "qwen3_06b_v10" and oof_aggregate["negative_raw_accuracy"] < 0.7967:
        raise RuntimeError(
            f"HARD ROI CIRCUIT BREAKER TRIGGERED on {backbone_key} 5-fold OOF: "
            f"negative_raw_accuracy={oof_aggregate['negative_raw_accuracy']}"
        )

    # 2. Full-Pool v10 Training (Stage 1 SFT + Stage 2 RPO-DPO)
    torch.cuda.reset_peak_memory_stats(device)
    full_model, full_train_stats = train_two_stage(
        base_dir,
        helper,
        sft_items,
        dpo_items,
        device,
        sft_epochs=full_sft_epochs,
        dpo_epochs=1,
        seed=SEED + 100,
        tag=f"{backbone_key}-full",
    )

    # 3. Full N=2,580 acceptance-v7 & N=80 overnight-v1 Evaluation (NEVER subsampled!)
    av7_pos_eval, _ = evaluate_cases_greedy(full_model, helper, av7_pos, device, default_v3=False)
    av7_neg_eval, _ = evaluate_cases_greedy(full_model, helper, av7_neg, device, default_v3=True)
    ov1_eval, _ = evaluate_cases_greedy(full_model, helper, ov1_rows, device, default_v3=False)

    print(
        f"[{backbone_key}-FULL] av7_pos(N={av7_pos_eval['cases']}): raw={av7_pos_eval['raw_accuracy']} ver={av7_pos_eval['verified_accuracy']} | "
        f"av7_neg(N={av7_neg_eval['cases']}): raw={av7_neg_eval['raw_accuracy']} ver={av7_neg_eval['verified_accuracy']} unauth_ver={av7_neg_eval['unauthorized_writes_verified']} | "
        f"ov1(N={ov1_eval['cases']}): raw={ov1_eval['raw_accuracy']} ver={ov1_eval['verified_accuracy']}",
        flush=True,
    )

    # 4. Merge LoRA into clean BF16 safetensors
    merge_info = merge_and_save_bf16(full_model, tokenizer, merged_out_dir, base_dir)

    del full_model
    gc.collect()
    torch.cuda.empty_cache()

    return {
        "backbone": backbone_key,
        "family": family,
        "oof_5fold": oof_aggregate,
        "full_train": full_train_stats,
        "dev_benchmarks": {
            "acceptance_v7_positive_full_1380": av7_pos_eval,
            "acceptance_v7_negative_full_1200": av7_neg_eval,
            "overnight_v1_full_80": ov1_eval,
        },
        "merged_artifact": merge_info,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", type=Path, default=ROOT_DIR)
    ap.add_argument("--only", choices=["all", "qwen3", "fg270m"], default="all")
    ap.add_argument("--oof-epochs", type=int, default=1)
    ap.add_argument("--full-epochs", type=int, default=3)
    args = ap.parse_args()

    wdir = args.work_dir.resolve()
    pmon_out = wait_for_gpu_idle()

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU required")
    torch.cuda.set_per_process_memory_fraction(0.45, 0)
    device = torch.device("cuda:0")
    gpu_name = torch.cuda.get_device_name(0)
    print(f"[gpu] Claimed {gpu_name} with hard per-process VRAM cap = 0.45 (<= 7.2 GiB)", flush=True)

    sft_rows = [json.loads(l) for l in (wdir / "data/train-v10/trainpool_v10_oof.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    dpo_rows = [json.loads(l) for l in (wdir / "data/train-v10/dpo_pairs_v10.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    av7_pos = [json.loads(l) for l in (wdir / "data/acceptance-v7/cases-positive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    av7_neg = [json.loads(l) for l in (wdir / "data/acceptance-v7/cases-negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ov1_rows = [json.loads(l) for l in (wdir / "data/overnight-v1/routing-heldout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    receipt_path = wdir / "v10_gpu_training_receipt.json"
    receipt: dict[str, Any] = {}
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            receipt = {}

    receipt.update({
        "pipeline_version": PIPELINE_VERSION,
        "gpu_device": gpu_name,
        "vram_fraction_cap": 0.45,
        "pmon_precheck": pmon_out,
        "sft_rows": len(sft_rows),
        "dpo_pairs": len(dpo_rows),
    })
    receipt.setdefault("models", {})

    if args.only in {"all", "qwen3"}:
        res_q = run_backbone_pipeline(
            wdir,
            "qwen3_06b_v10",
            "hermes",
            wdir / "models/qwen3-0.6b-bf16",
            wdir / "models/arrmes-qwen3-0.6b-v10-merged",
            sft_rows,
            dpo_rows,
            av7_pos,
            av7_neg,
            ov1_rows,
            device,
            oof_sft_epochs=args.oof_epochs,
            full_sft_epochs=args.full_epochs,
        )
        receipt["models"]["qwen3_06b_v10"] = res_q
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    if args.only in {"all", "fg270m"}:
        res_f = run_backbone_pipeline(
            wdir,
            "functiongemma_270m_v10",
            "functiongemma",
            wdir / "models/functiongemma-270m-bf16",
            wdir / "models/arrmes-functiongemma-270m-v10-merged",
            sft_rows,
            dpo_rows,
            av7_pos,
            av7_neg,
            ov1_rows,
            device,
            oof_sft_epochs=args.oof_epochs,
            full_sft_epochs=args.full_epochs,
        )
        receipt["models"]["functiongemma_270m_v10"] = res_f
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    print("\n[DONE] Saved receipt to", receipt_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
