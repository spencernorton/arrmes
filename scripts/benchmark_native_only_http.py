#!/usr/bin/env python3
"""Core-only authenticated HTTP diagnostic for explicit native read mode.

This task mix measures a model-free grammar, not neural routing quality. Inputs
are frozen before the server is launched. Only loopback fixtures are reachable.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import resource
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quantiles(values):
    xs = sorted(values)
    def percentile(p):
        i = (len(xs) - 1) * p
        return xs[math.floor(i)] + (xs[math.ceil(i)] - xs[math.floor(i)]) * (i % 1)
    return {"samples": len(xs), "p50_seconds": percentile(.5), "p95_seconds": percentile(.95),
            "minimum_seconds": xs[0], "maximum_seconds": xs[-1]}


def source_hashes(source):
    return {str(p.relative_to(source)): sha(p) for p in sorted((source / "src").rglob("*.py"))}


def package_versions():
    return {d.metadata["Name"]: d.version for d in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower())}


def child(args):
    from arrmes.sandbox import NetworkIsolationGuard
    from arrmes.server import main as server_main
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        with NetworkIsolationGuard(allowed_ports={args.fixture_port}):
            server_main()
    except KeyboardInterrupt:
        pass
    finally:
        info = {"pid": os.getpid(), "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                "rss_method": "OS process ru_maxrss high-water counter, excludes separate client/fixture process",
                "packages": package_versions(),
                "optional_modules_loaded": sorted(k for k in sys.modules if k.split(".")[0] in {"mlx", "mlx_lm", "torch", "transformers", "llama_cpp", "pytest", "numpy"}),
                "python": sys.version, "executable": sys.executable}
        Path(args.child_info).write_text(json.dumps(info, indent=2) + "\n")


def fixture_prefix(path):
    """Reuse the existing fixture without importing optional pytest/tests."""
    tree = ast.parse(path.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            break
        if isinstance(node, ast.Import) and any(n.name == "pytest" for n in node.names):
            continue
        body.append(node)
    prefix = ast.Module(body=body, type_ignores=[])
    module = {"__name__": "native_only_http_fixture", "__file__": str(path)}
    exec(compile(prefix, str(path), "exec"), module)
    return module, ast.unparse(prefix) + "\n"


def cases():
    specs = [
        ("sonarr", "health", "Show Sonarr health", {"healthy": True, "server_version": "4.0.20"}),
        ("sonarr", "queue", "Show Sonarr queue", {"items.0.id": 77, "items.0.status": "queued"}),
        ("sonarr", "missing", "Show Sonarr missing", {"items.0.id": 77, "items.0.seriesId": 8}),
        ("radarr", "queue", "Show Radarr queue", {"items.0.id": 77, "items.0.title": "Fixture title"}),
        ("radarr", "library", "Show Radarr library", {"items.0.id": 1, "items.0.title": "Fixture title"}),
        ("lidarr", "library", "Show Lidarr library", {"items.0.id": 1, "items.0.artistName": "Fixture title"}),
        ("lidarr", "history", "Show Lidarr history", {"items.0.id": 77, "items.0.sourceTitle": "Fixture title"}),
        ("bazarr", "missing", "Show missing movie subtitles in Bazarr", {"items.0.radarrId": 4, "items.0.title": "Fixture title"}),
        ("bazarr", "subtitles", 'Show movie subtitles for "Sintel" in Bazarr', {"items.0.radarrId": 4, "items.0.title": "Sintel"}),
        ("qbittorrent", "activity", "Show qBittorrent activity", {"transfer.dl_info_speed": 3000, "transfer.connection_status": "connected"}),
        ("qbittorrent", "queue", "Show qBittorrent queue", {"items.0.hash": "a" * 40, "items.0.name": "Fixture torrent"}),
        ("tautulli", "activity", "Show Tautulli activity", {"items.0.session_key": "0", "items.0.title": "Fixture movie"}),
        ("tautulli", "history", "Show Tautulli history", {"items.0.row_id": 1, "items.0.full_title": "Fixture movie"}),
        ("seerr", "search", 'Look up "Fixture title" in Seerr', {"items.0.id": 66, "items.0.title": "Fixture movie"}),
    ]
    result = []
    for i, (service, operation, query, expected) in enumerate(specs, 1):
        arguments = {"service": service, "operation": operation}
        if operation == "search":
            arguments["query"] = "Fixture title"
        if service == "bazarr":
            arguments["media_type"] = "movie"
            if operation == "subtitles":
                arguments["query"] = "Sintel"
        result.append({"id": f"NO1-{i:02d}", "query": query,
                       "expected_proposal": {"name": "service_query", "arguments": arguments},
                       "expected_business_fields": {"source": "native_api", "operation": operation, "complete_listing": True, **expected}})
    return result


def at_path(value, dotted):
    for key in dotted.split("."):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def request(base, path, payload=None, token=None):
    started = time.perf_counter()
    raw = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=raw, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status, body = response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        with exc:
            status, body = exc.code, json.loads(exc.read())
    elapsed = time.perf_counter() - started
    return {"http_status": status, "response": body, "client_wall_seconds": elapsed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root")
    parser.add_argument("--source-commit")
    parser.add_argument("--output")
    parser.add_argument("--inputs")
    parser.add_argument("--work-dir")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--fixture-port", type=int)
    parser.add_argument("--child-info")
    args = parser.parse_args()
    if args.child:
        return child(args)
    source, work = Path(args.source_root).resolve(), Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    before = source_hashes(source)
    harness_before = sha(__file__)
    fixture = source / "tests/test_native_services.py"
    fixture_module, prefix = fixture_prefix(fixture)
    prefix_file = work / "fixture-prefix.py"
    prefix_file.write_text(prefix)
    tasks = cases()
    controls = [
        {"id": "missing_auth", "path": "/v1/execute", "payload": {"query": tasks[0]["query"]}, "credential": "none", "expected_http": 401},
        {"id": "invalid_auth", "path": "/v1/execute", "payload": {"query": tasks[0]["query"]}, "credential": "invalid", "expected_http": 401},
        {"id": "scope_denied", "path": "/v1/execute", "payload": {"query": tasks[0]["query"]}, "credential": "no_scopes", "expected_http": 403, "expected_terminal": "denied"},
        {"id": "write_abstention", "path": "/v1/execute", "payload": {"query": 'Pause "Fixture torrent" in qBittorrent'}, "credential": "reader", "expected_http": 200, "expected_terminal": "clarify", "expected_routing": "native_only_abstention"},
        {"id": "write_confirmation_denied", "path": "/v1/confirm", "payload": {"confirm_id": "not-issued-for-this-model-free-mode"}, "credential": "reader", "expected_http": 403, "expected_terminal": "denied"},
        {"id": "outside_grammar", "path": "/v1/execute", "payload": {"query": "Make me a sandwich"}, "credential": "reader", "expected_http": 200, "expected_terminal": "clarify", "expected_routing": "native_only_abstention"},
    ]
    frozen = {"schema_version": 1, "kind": "model-free explicit-service read efficiency diagnostic; distinct from neural task mix",
              "frozen_at": datetime.now(timezone.utc).isoformat(), "source_commit": args.source_commit,
              "source_file_sha256": before, "fixture_file_sha256": sha(fixture),
              "fixture_transform": "AST module prefix before first test_* function, removing only import pytest; fixture handlers unchanged",
              "fixture_prefix_sha256": sha(prefix_file), "harness_sha256": harness_before,
              "repetitions": args.repetitions, "warmups": "one execute and one route per task, excluded",
              "order": "fixed task order repeated; serial, fresh HTTP connections, no concurrent load",
              "tasks": tasks, "controls": controls}
    inputs = Path(args.inputs)
    if inputs.exists() or Path(args.output).exists():
        raise RuntimeError("Refusing to overwrite frozen inputs or prior results")
    inputs.parent.mkdir(parents=True, exist_ok=True)
    inputs.write_text(json.dumps(frozen, indent=2) + "\n")
    from arrmes.sandbox import NetworkIsolationGuard
    from arrmes.auth import token_digest
    token, no_scope_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    client_file = work / "clients.json"
    client_file.write_text(json.dumps({"clients": [
        {"subject": "fixture-reader", "scopes": ["media:read", "activity:read", "torrents:read"], "token_sha256": token_digest(token)},
        {"subject": "fixture-no-scopes", "scopes": [], "token_sha256": token_digest(no_scope_token)}]}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    child_info = work / "child-info.json"
    rows, warmups, control_rows, runtime_health = [], [], [], None
    process = None
    with fixture_module["native_fixture"](guard=False) as (fixture_url, calls):
        fixture_port = int(fixture_url.rsplit(":", 1)[1])
        services = work / "services.json"
        services.write_text(json.dumps({"services": {name: {"base_url": fixture_url, "api_key": "fixture-key", "session_id": "fixture-session"} for name in ("sonarr", "radarr", "lidarr", "bazarr", "qbittorrent", "tautulli", "seerr")}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("ARRMES_")}
        env.update(ARRMES_BACKEND="native-only", ARRMES_CLIENTS_FILE=str(client_file), ARRMES_SERVICES_FILE=str(services),
                   ARRMES_BIND_HOST="127.0.0.1", ARRMES_PORT=str(port), PYTHONPATH=str(source / "src"), PYTHONUNBUFFERED="1")
        launch = time.perf_counter()
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", "--fixture-port", str(fixture_port), "--child-info", str(child_info)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            if not ready:
                raise RuntimeError("Native-only server did not become ready")
            ready_line = process.stdout.readline().strip()
            launch_ready = time.perf_counter() - launch
            if "listening" not in ready_line:
                raise RuntimeError("Unexpected server startup: " + ready_line)
            baseline_rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(process.pid)], text=True).strip()) * 1024
            with NetworkIsolationGuard(allowed_ports={fixture_port, port}):
                base = f"http://127.0.0.1:{port}"
                runtime_health = request(base, "/healthz")["response"]
                assert runtime_health["backend"] == "native-only" and runtime_health["write_mode"] == "disabled" and runtime_health["neural_model_loaded"] is False
                for task in tasks:
                    warmups.append({"id": task["id"], "route": request(base, "/v1/route", {"query": task["query"]}, token),
                                    "execute": request(base, "/v1/execute", {"query": task["query"]}, token)})
                for repetition in range(args.repetitions):
                    for task in tasks:
                        route = request(base, "/v1/route", {"query": task["query"]}, token)
                        call_start = len(calls)
                        execute = request(base, "/v1/execute", {"query": task["query"]}, token)
                        observation = execute["response"].get("observation") or {}
                        data = observation.get("data") or {}
                        mismatches = []
                        for key, expected in task["expected_business_fields"].items():
                            try:
                                actual = at_path(data, key)
                            except (KeyError, IndexError, TypeError, ValueError):
                                actual = "<missing>"
                            if actual != expected:
                                mismatches.append({"field": key, "expected": expected, "actual": actual})
                        exact = route["response"].get("proposal") == task["expected_proposal"] and execute["response"].get("proposal") == task["expected_proposal"]
                        successful = execute["http_status"] == 200 and execute["response"].get("terminal_state") == "completed" and observation.get("verified_complete") is True and exact and not mismatches
                        rows.append({"id": task["id"], "repetition": repetition, "query": task["query"], "route": route, "execute": execute,
                                     "exact_proposals": exact, "business_mismatches": mismatches, "successful": successful,
                                     "upstream_calls": [{"method": c["method"], "path": c["path"], "query": c["query"]} for c in calls[call_start:]]})
                for control in controls:
                    key = control["credential"]
                    credential = {"reader": token, "no_scopes": no_scope_token, "none": None, "invalid": "invalid-fixture-credential"}[key]
                    call_start = len(calls)
                    result = request(base, control["path"], control["payload"], credential)
                    ok = result["http_status"] == control["expected_http"]
                    if "expected_terminal" in control:
                        ok &= result["response"].get("terminal_state") == control["expected_terminal"]
                    if "expected_routing" in control:
                        ok &= result["response"].get("routing_source") == control["expected_routing"]
                    control_rows.append({**control, **result, "upstream_calls": len(calls) - call_start, "successful": bool(ok and len(calls) == call_start)})
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
        diagnostics = json.loads(child_info.read_text())
        assert not diagnostics["optional_modules_loaded"]
        upstream_methods = {method: sum(c["method"] == method for c in calls) for method in {c["method"] for c in calls}}
    after = source_hashes(source)
    if before != after or harness_before != sha(__file__):
        raise RuntimeError("Source or harness changed during diagnostic")
    receipt = {"schema_version": 1, "kind": frozen["kind"], "recorded_at": datetime.now(timezone.utc).isoformat(),
               "source_commit": args.source_commit, "source_file_sha256_before": before, "source_file_sha256_after": after,
               "harness_sha256_before": harness_before, "harness_sha256_after": sha(__file__), "inputs_sha256": sha(inputs), "inputs_frozen_at": frozen["frozen_at"],
               "fixture_file_sha256": sha(fixture), "fixture_prefix_sha256": sha(prefix_file), "fixture_transform": frozen["fixture_transform"],
               "hardware": {"platform": platform.platform(), "machine": platform.machine(), "processor": subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip(), "logical_cpus": os.cpu_count(), "physical_memory_bytes": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))},
               "runtime_health": runtime_health, "child_process": diagnostics, "client_python": sys.version, "client_packages": package_versions(),
               "settings": {"backend": "native-only", "write_mode": "disabled", "model_loaded": False, "concurrency": 1, "fresh_connection_per_request": True, "client_timeout_seconds": 5, "fixture_and_client_in_separate_process_from_arrmes": True},
               "timing_boundaries": {"client_wall": "JSON encode, authenticated HTTP connection/request, response read and JSON decode; grading excluded; loopback fixture execution included for execute requests",
                                      "route_internal": "Product /v1/route latency_ms: explicit grammar plus verifier; excludes HTTP ingress/auth/serialization and has 0.001ms rounding",
                                      "execute_internal": "Product /v1/execute latency_ms: routing, executor and fixture HTTP after principal lock; excludes HTTP ingress/auth/serialization",
                                      "startup": "Fresh Python process launch to flushed listening line; downloads/install excluded; filesystem caches not flushed"},
               "summary": {"unique_tasks": len(tasks), "repetitions": args.repetitions, "samples": len(rows), "warmup_requests_excluded": len(warmups) * 2,
                           "successful_business_outcomes": sum(r["successful"] for r in rows), "client_execute_wall": quantiles([r["execute"]["client_wall_seconds"] for r in rows]),
                           "client_route_preview_wall": quantiles([r["route"]["client_wall_seconds"] for r in rows]),
                           "route_internal": quantiles([r["route"]["response"]["latency_ms"] / 1000 for r in rows]),
                           "execute_internal": quantiles([r["execute"]["response"]["latency_ms"] / 1000 for r in rows]),
                           "process_startup_to_listening_seconds": launch_ready, "process_rss_before_warmup_bytes": baseline_rss,
                           "process_peak_rss_bytes": diagnostics["peak_rss_bytes"], "safety_controls_passed": sum(c["successful"] for c in control_rows), "safety_controls": len(control_rows), "upstream_methods_including_warmups": upstream_methods},
               "warmups": warmups, "controls": control_rows, "rows": rows,
               "limitations": ["Separate explicit-service grammar task mix: not a head-to-head comparison with neural benchmarks.", "One warm process, serial loopback requests; no TLS, remote services, production data or concurrent load.", "Existing synthetic HTTP fixtures, not actual upstream service binaries or API-version integration qualification.", "Write requests abstain; a forged/nonexistent confirmation is denied. This is not successful write execution.", "Startup is a single sample; memory is the isolated Arrmes process, not Python installation size."]}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt["summary"], indent=2))
    if not all(r["successful"] for r in rows) or not all(c["successful"] for c in control_rows):
        raise SystemExit("Diagnostic failures retained in receipt")


if __name__ == "__main__":
    main()
