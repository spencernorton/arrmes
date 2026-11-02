"""Actual model/runtime/confirmation/native-HTTP integration, not held-out acceptance.

The input scenarios and all application/fixture/harness bytes are frozen before
model loading. Raw predictions, verifier results and system outcomes stay separate.
No inference is attributed to an explicit deterministic route.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_result(result):
    """Keep raw business evidence without publishing usable confirmation tokens."""
    if isinstance(result, dict):
        return {key: public_result(value) for key, value in result.items()
                if key not in {"confirm_id", "confirmation_token", "headers"}}
    if isinstance(result, list):
        return [public_result(value) for value in result]
    return result


@contextmanager
def fixture(case):
    if case["fixture"] == "bitmagnet":
        from test_native_bitmagnet import bitmagnet_fixture, READER
        with bitmagnet_fixture() as (native, state):
            yield native, state, READER
    elif case["fixture"] == "seerr":
        from test_native_requests import request_fixture, RequestFixture, VIEWER
        state = RequestFixture()
        if case.get("fault") == "ambiguous":
            state.search.append({**state.search[0], "id": 102})
        if case.get("fault") == "unavailable":
            state.faults["/api/v1/status"] = (503, {})
        with request_fixture(state) as (adapter, state):
            yield adapter.native, state, VIEWER
    elif case["fixture"] == "bazarr":
        from test_bazarr_subtitles import bazarr_fixture, OWNER
        with bazarr_fixture() as (native, state):
            yield native, state, OWNER
    else:
        raise ValueError("Unknown integration fixture")


def mutation_calls(case, calls):
    # Bitmagnet uses fixed GraphQL POST queries with no mutation fields.
    if case["fixture"] == "bitmagnet":
        return [call for call in calls if not call.get("payload", {}).get("query", "").lstrip().startswith("query ")]
    return [call for call in calls if call["method"] != "GET"]


def run_case(case, model):
    from arrmes.executor import BoundedExecutor
    from arrmes.protocol import InvalidCall, decode, normalized
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    rows = []

    class RecordingModel:
        family = model.family
        backend = getattr(model, "backend", "mlx")

        def infer(self, *args, **kwargs):
            result = model.infer(*args, **kwargs)
            rows.append(deepcopy(result))
            return result

    with fixture(case) as (native, state, principal):
        runtime = ArrmesRuntimeService(model=RecordingModel(),
            executor=BoundedExecutor(ConfiguredAdapterRouter(native)), write_mode="confirm")
        started = time.perf_counter()
        initial = runtime.execute(case["query"], principal)
        before_confirmation = deepcopy(state.calls)
        transitions = [public_result(initial)]
        result = initial
        confirmations = 0
        replay_denied = None
        if case.get("confirm") and initial["terminal_state"] == "awaiting_confirmation":
            token = initial["plan"]["confirm_id"]
            result = runtime.confirm(token, principal)
            confirmations += 1
            transitions.append(public_result(result))
            if case.get("download_candidate") and result["terminal_state"] == "completed":
                candidates = result["observation"]["data"].get("candidates", [])
                if candidates:
                    query = case["query"].replace("Find ", "Download ").replace(" subtitles for ",
                        " subtitle candidate " + candidates[0]["candidate_id"] + " for ")
                    staged = runtime.execute(query, principal)
                    transitions.append(public_result(staged))
                    if staged["terminal_state"] == "awaiting_confirmation":
                        token = staged["plan"]["confirm_id"]
                        result = runtime.confirm(token, principal)
                        confirmations += 1
                        transitions.append(public_result(result))
            before_replay = len(mutation_calls(case, state.calls))
            replay = runtime.confirm(token, principal)
            replay_denied = replay["terminal_state"] == "denied" and len(mutation_calls(case, state.calls)) == before_replay
        elapsed = time.perf_counter() - started
        raw = None
        raw_error = None
        if rows:
            try:
                raw = normalized(decode(rows[0]["raw_unverified"], model.family))
            except InvalidCall as exc:
                raw_error = type(exc).__name__
        writes = mutation_calls(case, state.calls)
        expected_mutations = 1 if case.get("confirm") and not case.get("fault") else 0
        actual_route = initial.get("routing_source")
        route_ok = actual_route == case["route"] and len(rows) == (1 if case["route"] == "neural_v2" else 0)
        expected_proposal = initial.get("proposal") == case["expected"]
        business = result["terminal_state"] == case["terminal"]
        if case["fixture"] == "bitmagnet" and business:
            data = result["observation"]["data"]
            business = len(data.get("items", [])) == 2
        if case["fixture"] == "seerr" and case.get("confirm") and business:
            business = state.posted and writes[0].get("body") == {"mediaType": "movie", "mediaId": 101}
        if case.get("download_candidate") and business:
            data = result["observation"]["data"]
            business = data.get("job_completion_correlated") is False and result["observation"]["verified_complete"] is False
        safe = not mutation_calls(case, before_confirmation) and len(writes) == expected_mutations
        provider_searches = None
        if case["fixture"] == "bazarr":
            from test_bazarr_subtitles import provider_gets
            provider_searches = len(provider_gets(state))
            safe = safe and not any(call["path"].startswith("/api/providers/") for call in before_confirmation)
            safe = safe and provider_searches == 1
        if case.get("confirm"):
            safe = safe and replay_denied is True and confirmations == (2 if case.get("download_candidate") else 1)
        return {**case, "raw_inferences": rows, "raw_proposal": raw, "raw_decode_error": raw_error,
            "raw_exact": raw == case["expected"] if rows else None,
            "verifier_exact": expected_proposal if rows else None,
            "runtime_proposal_exact": expected_proposal, "actual_route": actual_route,
            "inference_calls": len(rows), "route_expected": route_ok,
            "business_expected": business, "confirmation_and_mutations_expected": safe,
            "system_expected": business and safe and expected_proposal and route_ok,
            "confirmations": confirmations, "replay_denied": replay_denied,
            "mutations_before_confirmation": len(mutation_calls(case, before_confirmation)),
            "http_mutations": len(writes), "http_calls": public_result(state.calls),
            "provider_searches": provider_searches,
            "transitions": transitions, "workflow_wall_seconds": elapsed,
            "timing_scope": "In-process fixture workflow, immediate simulated human confirmations; no grading, TLS or user delay"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--inputs", type=Path, default=ROOT / "data/native-workflows-v1/scenarios.json")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists():
        raise SystemExit("Refusing to overwrite integration inputs/results/source")
    output.mkdir(parents=True)
    paths = list((ROOT / "src/arrmes").rglob("*.py"))
    paths += [ROOT / "tests" / name for name in ("test_native_bitmagnet.py", "test_native_requests.py", "test_bazarr_subtitles.py")]
    paths += [Path(__file__).resolve(), args.inputs.resolve()]
    files = {path.relative_to(ROOT).as_posix(): sha(path) for path in sorted(paths)}
    for path in paths:
        target = output / "source" / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    artifacts = {label: [{"file": path.name, "bytes": path.stat().st_size, "sha256": sha(path)}
        for path in sorted(directory.iterdir()) if path.suffix in {".safetensors", ".json", ".jinja", ".txt"}]
        for label, directory in (("base", args.model), ("adapter", args.adapter))}
    freeze = {"frozen_at": datetime.now(timezone.utc).isoformat(), "source_file_sha256": files,
              "inputs_sha256": sha(args.inputs), "model_artifacts": artifacts}
    (output / "input-freeze.json").write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8")
    sys.path[:0] = [str(output / "source/src"), str(output / "source/tests")]
    from arrmes.model import LocalModel
    model = LocalModel(str(args.model), family="hermes", adapter=str(args.adapter), contract_version="v2")
    cases = json.loads(args.inputs.read_text(encoding="utf-8"))["cases"]
    results = []
    for case in cases:
        try:
            result = run_case(case, model)
        except Exception as exc:
            result = {**case, "system_expected": False, "harness_error_type": type(exc).__name__}
        results.append(result)
        print(case["id"], result.get("raw_exact"), result.get("actual_route"), result["system_expected"], flush=True)
    if any(sha(ROOT / path) != digest or sha(output / "source" / path) != digest for path, digest in files.items()):
        raise RuntimeError("Frozen source changed during execution")
    for label, directory in (("base", args.model), ("adapter", args.adapter)):
        if any(sha(directory / item["file"]) != item["sha256"] for item in artifacts[label]):
            raise RuntimeError("Model artifact changed during execution")
    neural = [row for row in results if row["route"] == "neural_v2"]
    receipt = {"schema_version": 1, "kind": "Integration smoke derived from known fixtures, not held-out accuracy acceptance",
        "recorded_at": datetime.now(timezone.utc).isoformat(), "freeze": freeze,
        "model_revision": "mlx-community/Qwen3-0.6B-4bit@73e3e38d981303bc594367cd910ea6eb48349da8",
        "adapter_revision": "NorviTech/arrmes-qwen3-0.6b@8a9bcf224e6f9fd6bf238267093c70360ee837cb",
        "hardware": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "cpu": subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()},
        "packages": {name: importlib.metadata.version(name) for name in ("mlx", "mlx-lm", "jsonschema", "pytest")},
        "generation": {"backend": "mlx", "family": "hermes", "contract": "v2", "max_tokens": 64,
            "temperature": 0, "warmup": "none; scenario order recorded", "context": "empty server-owned ledger per scenario"},
        "summary": {"cases": len(results), "system_expected": sum(row["system_expected"] for row in results),
            "neural_cases": len(neural), "neural_raw_exact": sum(row.get("raw_exact") is True for row in neural),
            "neural_verifier_exact": sum(row.get("verifier_exact") is True for row in neural),
            "explicit_cases": len(results) - len(neural)}, "rows": results}
    (output / "results.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt["summary"]))
    return 0 if all(row["system_expected"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
