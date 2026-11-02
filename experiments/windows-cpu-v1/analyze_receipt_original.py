#!/usr/bin/env python3
"""Read-only Windows smoke receipt analysis; never dispatches or polls a run."""
import argparse
import hashlib
import json
from pathlib import Path
import re

MODEL_SHA = "c7d6f1eef1eb4cd0f93769dadca7e270f3c3d470cfb542240733859c7d02f255"
RUNTIME_COMMIT = "d81235049384534c167caea52b85a694f6103d14"
CHECKS = {"raw_activity_call_and_fixture_execution", "actual_cpu_backend_loaded", "four_native_generations_completed", "raw_schema_valid_all_examples", "verified_controls_abstain", "managed_native_process_stopped", "sandbox_stopped", "temporary_sandbox_removed"}
IDS = {"activity_read", "missing_route", "unsafe_control", "wildcard_control"}


def analyze(receipt, *, run_id, workflow_sha, source_manifest, run_metadata):
    checks = {}
    ci = receipt.get("ci", {})
    checks["actual_windows_x64_environment"] = receipt.get("hardware", {}).get("system") == "Windows" and receipt.get("hardware", {}).get("machine", "").lower() in {"amd64", "x86_64"}
    checks["ci_identity_bound"] = ci.get("GITHUB_ACTIONS") == "true" and ci.get("GITHUB_REPOSITORY") == "spencernorton/arrmes" and str(ci.get("GITHUB_RUN_ID")) == str(run_id) and ci.get("GITHUB_SHA") == workflow_sha and ci.get("RUNNER_OS") == "Windows" and ci.get("RUNNER_ARCH") == "X64"
    checks["api_run_identity_bound"] = str(run_metadata.get("id")) == str(run_id) and run_metadata.get("head_sha") == workflow_sha and run_metadata.get("event") == "workflow_dispatch" and run_metadata.get("path", "").endswith("windows-model-smoke.yml") and run_metadata.get("status") == "completed"
    checks["workflow_concluded_success"] = run_metadata.get("conclusion") == "success"
    checks["exact_model_identity"] = receipt.get("model", {}).get("sha256") == MODEL_SHA and receipt.get("model", {}).get("bytes") == 396704352
    runtime = receipt.get("runtime", {})
    checks["pinned_runtime_identity"] = runtime.get("expected_source_commit") == RUNTIME_COMMIT and RUNTIME_COMMIT[:9] in runtime.get("version_output", "") and bool(re.fullmatch(r"[a-f0-9]{64}", runtime.get("executable_sha256", "")))
    expected = {item["file"]: item["sha256"] for item in source_manifest["files"]}
    actual = receipt.get("source_file_sha256", {})
    checks["exact_installed_product_source"] = actual == expected and receipt.get("package_version") == "1.2.0"
    checks["source_manifest_self_consistent"] = receipt.get("source_manifest_sha256") == hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
    generation = receipt.get("generation", {})
    checks["cpu_generation_settings"] = generation == {"device": "cpu", "threads": 2, "context_size": 4096, "slots": 1, "max_tokens": 64, "temperature": 0, "seed": 0}
    distributions = [item.get("name", "").lower().replace("_", "-") for item in receipt.get("installed_distributions", [])]
    checks["optional_heavy_dependencies_absent"] = bool(distributions) and not any(name in {"torch", "mlx", "mlx-lm", "llama-cpp-python"} or name.startswith(("nvidia-", "cuda-", "cupy")) for name in distributions)
    rows = receipt.get("rows", [])
    checks["four_actual_native_calls"] = len(rows) == 4 and {row.get("id") for row in rows} == IDS and all(row.get("inference", {}).get("backend") == "llama-server" and row.get("inference", {}).get("device") == "cpu" for row in rows)
    checks["eight_operational_checks_passed"] = set(receipt.get("checks", {})) == CHECKS and all(value is True for value in receipt.get("checks", {}).values()) and receipt.get("all_checks_passed") is True and "failure" not in receipt
    checks["no_outbound_attempts"] = receipt.get("network_isolation", {}).get("blocked_outbound_attempts_count") == 0
    return {"schema_version": 1, "run_id": str(run_id), "workflow_sha": workflow_sha,
            "all_identity_and_operational_checks_passed": all(checks.values()), "checks": checks,
            "raw_expected_tool_matches": sum(row.get("raw_expected_tool_matches") is True for row in rows),
            "verified_expected_tool_matches": sum(row.get("verified_expected_tool_matches") is True for row in rows),
            "native_model_calls": len(rows), "actual_hardware": receipt.get("hardware"),
            "qualification": "Hosted Windows x86_64 CPU runner operational smoke only; not owned Windows hardware, broad accuracy, native NVIDIA, or all-service qualification.",
            "memory_measured_by_smoke": False, "memory": receipt.get("memory"),
            "failure": receipt.get("failure"), "api_run_conclusion": run_metadata.get("conclusion")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--run-metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+", args.run_id) or not re.fullmatch(r"[a-f0-9]{40}", args.workflow_sha):
        parser.error("run ID and workflow commit must be exact numeric/SHA identities")
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite an analysis")
    result = analyze(json.loads(args.receipt.read_text()), run_id=args.run_id, workflow_sha=args.workflow_sha,
                     source_manifest=json.loads(args.source_manifest.read_text()), run_metadata=json.loads(args.run_metadata.read_text()))
    result["inputs"] = [{"role": role, "file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for role, path in [("receipt", args.receipt), ("source_manifest", args.source_manifest), ("api_metadata", args.run_metadata), ("analysis_script", Path(__file__))]]
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["all_identity_and_operational_checks_passed"], "checks": result["checks"]}))
    raise SystemExit(0 if result["all_identity_and_operational_checks_passed"] else 1)


if __name__ == "__main__":
    main()
