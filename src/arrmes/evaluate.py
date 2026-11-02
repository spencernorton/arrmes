"""Frozen-fixture semantic scoring, statistical confidence bounds, and sandbox evaluation."""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import time
from typing import Any

from .catalog import V2_BY_NAME, WRITE_EFFECT_ACTIONS, contract_hash, contract_hash_v2
from .protocol import Call, InvalidCall, decode, normalized


def read_cases(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def score(raw: str, expected: dict, family: str) -> dict:
    try:
        actual = decode(raw, family)
    except InvalidCall:
        return {"schema_valid": False, "action_correct": False,
                "exact": False, "actual": None}
    target = Call(expected["name"], expected["arguments"])
    return {"schema_valid": True, "action_correct": actual.name == target.name,
            "exact": normalized(actual) == normalized(target), "actual": normalized(actual)}


def _installed_versions(names: list[str]) -> dict[str, str | None]:
    """Package versions for the receipt; optional backends that are not installed are recorded as None."""
    import importlib.metadata

    out: dict[str, str | None] = {}
    for name in names:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def percentile(values, p):
    return sorted(values)[max(0, int(len(values) * p + 0.999999) - 1)] if values else None


def wilson_interval(k: int, n: int, z: float = 1.959963984540054) -> dict[str, float | None]:
    """Two-sided 95% Wilson score interval for binomial proportion k/n."""
    if n <= 0:
        return {"lower": None, "upper": None}
    p = k / n
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)) / denom
    return {"lower": round(max(0.0, center - half), 6), "upper": round(min(1.0, center + half), 6)}


def clopper_pearson_upper_bound(k: int, n: int, alpha: float = 0.05) -> float | None:
    """Exact one-sided (1 - alpha) Clopper-Pearson upper confidence bound for rare unsafe events."""
    if n <= 0:
        return None
    if k >= n:
        return 1.0
    if k == 0:
        return round(1.0 - (alpha ** (1.0 / n)), 6)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        tail = sum(math.comb(n, i) * (mid ** i) * ((1.0 - mid) ** (n - i)) for i in range(k + 1))
        if tail > alpha:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 6)


def _row_bool_metric(row: dict[str, Any], key: str, fallback_key: str | None = None) -> bool:
    if key in row and row[key] is not None:
        return bool(row[key])
    if fallback_key is not None and fallback_key in row and row[fallback_key] is not None:
        return bool(row[fallback_key])
    return False


def cluster_bootstrap_ci(
    rows: list[dict[str, Any]],
    *,
    n_boot: int = 2000,
    seed: int = 1379,
    key: str = "exact",
    fallback_key: str | None = None,
) -> dict[str, Any]:
    """95% cluster-bootstrap confidence interval for exact accuracy accounting for template/entity family correlation."""
    if not rows:
        return {"clusters": 0, "lower": None, "upper": None}
    eff_fallback = fallback_key if fallback_key is not None else ("exact" if key == "raw_neural_exact" else None)
    clusters: dict[str, list[int]] = {}
    for r in rows:
        cid = str(r.get("family_id") or r.get("entity_family") or r.get("category") or r.get("id"))
        clusters.setdefault(cid, []).append(1 if _row_bool_metric(r, key, eff_fallback) else 0)
    cluster_keys = sorted(clusters.keys())
    m = len(cluster_keys)
    if m <= 1:
        k = sum(1 for r in rows if _row_bool_metric(r, key, eff_fallback))
        w = wilson_interval(k, len(rows))
        return {"clusters": m, "lower": w["lower"], "upper": w["upper"]}
    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_boot):
        sampled = [clusters[rng.choice(cluster_keys)] for _ in range(m)]
        num = sum(sum(c) for c in sampled)
        den = sum(len(c) for c in sampled)
        if den > 0:
            boot_means.append(num / den)
    boot_means.sort()
    lo_idx = max(0, int(0.025 * len(boot_means)))
    hi_idx = min(len(boot_means) - 1, int(0.975 * len(boot_means)))
    return {
        "clusters": m,
        "replicates": n_boot,
        "lower": round(boot_means[lo_idx], 6),
        "upper": round(boot_means[hi_idx], 6),
    }


def _has_raw_neural_observation(row: dict[str, Any]) -> bool:
    if "raw_neural_actual" not in row:
        return False
    if row["raw_neural_actual"] is not None:
        return True
    return row.get("raw_neural_schema_valid") is False


def summarize(rows: list[dict], *, provenance_raw_unverified: bool = False) -> dict:
    n = len(rows)
    kinds = sorted({x["category"] for x in rows})
    controls = [x for x in rows if x["expected"]["name"] in {"clarify", "unsupported"}]
    writes = [x for x in rows if x["expected"]["name"] in WRITE_EFFECT_ACTIONS]
    non_writes = [x for x in rows if x["expected"]["name"] not in WRITE_EFFECT_ACTIONS]
    unexpected_writes = [x for x in non_writes
                         if x.get("actual") and x["actual"]["name"] in WRITE_EFFECT_ACTIONS]
    raw_observed_rows = rows if provenance_raw_unverified else [x for x in rows if _has_raw_neural_observation(x)]
    raw_observed_non_writes = (
        non_writes if provenance_raw_unverified else [x for x in non_writes if _has_raw_neural_observation(x)]
    )
    raw_unexpected_writes = [
        x for x in raw_observed_non_writes
        if (
            (x.get("raw_neural_actual") and x["raw_neural_actual"]["name"] in WRITE_EFFECT_ACTIONS)
            or (
                provenance_raw_unverified
                and "raw_neural_actual" not in x
                and x.get("actual")
                and x["actual"]["name"] in WRITE_EFFECT_ACTIONS
            )
        )
    ]
    raw_non_write_complete = len(raw_observed_non_writes) == len(non_writes)
    latencies = [x["seconds"] for x in rows]
    warm_latencies = latencies[1:] if len(latencies) > 1 else latencies
    exact_k = sum(x["exact"] for x in rows)
    raw_neural_exact_k = sum(int(bool(x.get("raw_neural_exact", x["exact"]))) for x in rows)
    verifier_mutated_k = sum(int(bool(x.get("verifier_mutated", False))) for x in rows)

    by_service: dict[str, dict[str, Any]] = {}
    for x in rows:
        svc = V2_BY_NAME[x["expected"]["name"]].service
        slot = by_service.setdefault(svc, {"cases": 0, "exact": 0, "raw_neural_exact": 0, "action_correct": 0})
        slot["cases"] += 1
        slot["exact"] += int(bool(x["exact"]))
        slot["raw_neural_exact"] += int(bool(x.get("raw_neural_exact", x["exact"])))
        slot["action_correct"] += int(bool(x["action_correct"]))
    for svc, slot in by_service.items():
        slot["exact_accuracy"] = round(slot["exact"] / slot["cases"], 6) if slot["cases"] else None
        slot["raw_neural_exact_accuracy"] = round(slot["raw_neural_exact"] / slot["cases"], 6) if slot["cases"] else None

    pred_abstentions = sum(1 for x in rows if x.get("actual") and x["actual"]["name"] in {"clarify", "unsupported"})

    return {
        "cases": n,
        "schema_valid": sum(x["schema_valid"] for x in rows),
        "action_correct": sum(x["action_correct"] for x in rows),
        "exact": exact_k,
        "exact_accuracy": exact_k / n if n else None,
        "wilson_95_ci": wilson_interval(exact_k, n),
        "cluster_bootstrap_95_ci": cluster_bootstrap_ci(rows, key="exact"),
        "raw_neural_exact": raw_neural_exact_k,
        "raw_neural_exact_accuracy": round(raw_neural_exact_k / n, 6) if n else None,
        "raw_neural_wilson_95_ci": wilson_interval(raw_neural_exact_k, n),
        "raw_neural_cluster_bootstrap_95_ci": cluster_bootstrap_ci(rows, key="raw_neural_exact", fallback_key="exact"),
        "raw_neural_observed_cases": len(raw_observed_rows),
        "raw_neural_non_write_opportunities": len(raw_observed_non_writes),
        "raw_neural_unexpected_write_proposals": len(raw_unexpected_writes) if raw_non_write_complete else None,
        "raw_neural_unsafe_write_proposal_upper_bound_95": (
            clopper_pearson_upper_bound(len(raw_unexpected_writes), len(raw_observed_non_writes))
            if raw_non_write_complete
            else None
        ),
        "raw_neural_observed_unsafe_write_proposal_upper_bound_95": clopper_pearson_upper_bound(
            len(raw_unexpected_writes), len(raw_observed_non_writes)
        ),
        "verifier_mutated_count": verifier_mutated_k,
        "verifier_mutation_rate": round(verifier_mutated_k / n, 6) if n else None,
        "control_exact": sum(x["exact"] for x in controls),
        "control_cases": len(controls),
        "write_exact": sum(x["exact"] for x in writes),
        "write_cases": len(writes),
        "non_write_opportunities": len(non_writes),
        "unexpected_write_proposals": len(unexpected_writes),
        "unsafe_write_proposal_upper_bound_95": clopper_pearson_upper_bound(len(unexpected_writes), len(non_writes)),
        "coverage_rate": round((n - pred_abstentions) / n, 6) if n else None,
        "abstention_rate": round(pred_abstentions / n, 6) if n else None,
        "latency_seconds": {
            "median": statistics.median(latencies) if n else None,
            "p95": percentile(latencies, .95),
            "p99": percentile(latencies, .99),
            "first_measured": latencies[0] if n else None,
            "warm_p50": statistics.median(warm_latencies) if warm_latencies else None,
            "warm_p95": percentile(warm_latencies, .95),
            "warm_p99": percentile(warm_latencies, .99),
        },
        "by_service": dict(sorted(by_service.items())),
        "by_category": {
            k: {
                "cases": sum(x["category"] == k for x in rows),
                "exact": sum(x["category"] == k and x["exact"] for x in rows),
                "raw_neural_exact": sum(x["category"] == k and bool(x.get("raw_neural_exact", x["exact"])) for x in rows),
            }
            for k in kinds
        },
        "failures_by_expected_action": dict(Counter(x["expected"]["name"] for x in rows if not x["exact"])),
        "raw_neural_failures_by_expected_action": dict(Counter(x["expected"]["name"] for x in rows if not x.get("raw_neural_exact", x["exact"]))),
    }


def run(model, cases_path: str, output: str, limit: int | None = None):
    cases = read_cases(cases_path)
    if limit:
        cases = cases[:limit]
    rows = []
    started = time.time()
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    for i, case in enumerate(cases):
        inference = model.infer(case["query"], case.get("context"))
        result = score(inference["raw"], case["expected"], model.family)
        raw_unv = inference.get("raw_unverified", inference["raw"])
        raw_res = score(raw_unv, case["expected"], model.family)
        row_data = {
            **case,
            **inference,
            **result,
            "raw_neural_schema_valid": raw_res["schema_valid"],
            "raw_neural_action_correct": raw_res["action_correct"],
            "raw_neural_exact": raw_res["exact"],
            "raw_neural_actual": raw_res["actual"],
            "verifier_mutated": (raw_res["actual"] != result["actual"]),
        }
        rows.append(row_data)
        if (i + 1) % 20 == 0:
            print(json.dumps({"completed": i + 1, "total": len(cases),
                              "exact": sum(x["exact"] for x in rows),
                              "raw_neural_exact": sum(x["raw_neural_exact"] for x in rows)}), flush=True)
    import importlib.metadata
    c_ver = getattr(model, "contract_version", "v1")
    c_hash = contract_hash_v2() if c_ver == "v2" else contract_hash()
    pol_mod = __import__("arrmes.model", fromlist=["POLICY", "POLICY_V2"])
    pol_str = pol_mod.POLICY_V2 if c_ver == "v2" else pol_mod.POLICY
    receipt = {"schema_version": 2, "model_path": model.path, "adapter": model.adapter,
               "family": model.family, "contract_version": c_ver, "load_seconds": model.load_seconds,
               "started_unix": started, "completed_unix": time.time(),
               "host": {"platform": platform.platform(), "machine": platform.machine()},
               "packages": _installed_versions(["mlx-lm", "mlx", "transformers", "jsonschema"]),
               "contract_sha256": c_hash,
               "policy_sha256": hashlib.sha256(pol_str.encode()).hexdigest(),
               "dataset_sha256": hashlib.sha256(Path(cases_path).read_bytes()).hexdigest(),
               "method": "greedy decoding; native templates; 160 output-token limit; model remains loaded",
               "summary": summarize(rows), "rows": rows}
    target.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(receipt["summary"], indent=2), flush=True)
    return receipt
