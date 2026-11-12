#!/usr/bin/env python3
"""One-shot Phase 4 evaluation of merged v10 BF16 models on sealed acceptance-v8 (N=3,135)."""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import sys
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

ROOT_DIR = Path(__file__).resolve().parents[1]
for _p in (ROOT_DIR / "src", ROOT_DIR / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hashlib

EXPECTED_AV8_SHA256 = {
    "cases-positive.jsonl": "44ad97e4a7b7f41c6c68e662ad973f8c4ce1fbdb9c085ecfecc017ae2f9dd610",
    "cases-negative.jsonl": "c6d42aa6363c260f0dccf89a8ddc646bb02910b7572c7bb88d2a967b39f5eb0e",
    "bootstrap_indices_seed10010.json": "841b983ce8c8ffb760ae07e38a5274a07b92294e5ef0c3fdcdc4c5a6412f6ebc",
    "manifest.json": "9a1a21e0a9ccf6caae6a3c431aa59555bafbade1880eeea700a7dc0e0b3a6309",
}


def verify_pinned_av8_digests(av8_dir: Path) -> dict[str, str]:
    verified = {}
    for fn, exp in EXPECTED_AV8_SHA256.items():
        actual = hashlib.sha256((av8_dir / fn).read_bytes()).hexdigest()
        if actual != exp:
            raise RuntimeError(f"acceptance-v8 digest mismatch on {fn}: {actual} != {exp}")
        verified[fn] = actual
    return verified

from arrmes.evaluate import cluster_bootstrap_ci
from train_v10_gpu import PrefixTokenizerHelper, evaluate_cases_greedy, wait_for_gpu_idle


def enrich_with_cluster_ci(summary: dict, per_row: list[dict], cases: list[dict]) -> dict:
    boot_rows = []
    for c, r in zip(cases, per_row):
        cid = c.get("cluster_id") or str(c.get("id", "")).rsplit("-", 1)[0]
        boot_rows.append({
            "cluster_id": cid,
            "raw_exact": r["raw_exact"],
            "verified_exact": r["verified_exact"],
        })
    summary["raw_cluster_bootstrap_95"] = cluster_bootstrap_ci(boot_rows, n_boot=2000, seed=20261111, key="raw_exact")
    summary["verified_cluster_bootstrap_95"] = cluster_bootstrap_ci(boot_rows, n_boot=2000, seed=20261111, key="verified_exact")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", type=Path, default=ROOT_DIR)
    args = ap.parse_args()
    wdir = args.work_dir.resolve()

    wait_for_gpu_idle()
    torch.cuda.set_per_process_memory_fraction(0.45, 0)
    device = torch.device("cuda:0")

    verified_digests = verify_pinned_av8_digests(wdir / "data/acceptance-v8")
    av8_pos = [json.loads(l) for l in (wdir / "data/acceptance-v8/cases-positive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    av8_neg = [json.loads(l) for l in (wdir / "data/acceptance-v8/cases-negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    out: dict = {"verified_input_sha256": verified_digests}
    for key, fam, mdir in [
        ("qwen3_06b_v10", "hermes", wdir / "models/arrmes-qwen3-0.6b-v10-merged"),
        ("functiongemma_270m_v10", "functiongemma", wdir / "models/arrmes-functiongemma-270m-v10-merged"),
    ]:
        tok = AutoTokenizer.from_pretrained(str(mdir))
        helper = PrefixTokenizerHelper(tok, fam)
        model = AutoModelForCausalLM.from_pretrained(str(mdir), dtype=torch.bfloat16).to(device)
        model.eval()

        pos_sum, pos_rows = evaluate_cases_greedy(model, helper, av8_pos, device, default_v3=False)
        neg_sum, neg_rows = evaluate_cases_greedy(model, helper, av8_neg, device, default_v3=True)
        enrich_with_cluster_ci(pos_sum, pos_rows, av8_pos)
        enrich_with_cluster_ci(neg_sum, neg_rows, av8_neg)

        out[key] = {
            "acceptance_v8_positive_1685": pos_sum,
            "acceptance_v8_negative_1450": neg_sum,
        }
        print(f"[av8-{key}] pos_raw={pos_sum['raw_accuracy']} pos_ver={pos_sum['verified_accuracy']} | neg_raw={neg_sum['raw_accuracy']} neg_ver={neg_sum['verified_accuracy']} unauth_ver={neg_sum['unauthorized_writes_verified']}", flush=True)
        del model
        gc.collect()
        torch.cuda.empty_cache()

    out_p = wdir / "v10_av8_oneshot_gpu_receipt.json"
    out_p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print("[DONE] Wrote", out_p, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
