"""Bounded, empty-library Sonarr v4 qualification using the real ARM64 binary.

Requires one explicitly pulled image, local Docker Desktop and Python 3.12+.
It creates one 1-CPU/768-MiB Sonarr container, an internal network and temporary
synthetic configuration. The Arrmes app runs from an immutable source snapshot
in a fresh minimal venv. A loopback GET-only audit proxy invokes curl inside the
isolated container, forwarding unchanged JSON from the actual app. It is not an
API fixture or direct cross-container TCP qualification. No production services
are used, and the Sonarr container publishes no ports.

Preparation (one approximately 85-MB compressed image, no model download):
  docker pull --platform linux/arm64 lscr.io/linuxserver/sonarr@sha256:d3bf2056aa6439d54e1e673ae850b7fc70425b5b2b441414c4f480532f8fe8e0
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

_helper_spec = importlib.util.spec_from_file_location("_arrmes_native_only_smoke",
    Path(__file__).with_name("smoke_native_only.py"))
_helper = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(_helper)
BOOTSTRAP, FORBIDDEN_IMPORTS, digest = _helper.BOOTSTRAP, _helper.FORBIDDEN_IMPORTS, _helper.digest


IMAGE = "lscr.io/linuxserver/sonarr@sha256:d3bf2056aa6439d54e1e673ae850b7fc70425b5b2b441414c4f480532f8fe8e0"


def worker(root: Path, source: Path, upstream: str) -> dict:
    sys.path.insert(0, str(source / "src"))
    from arrmes.sandbox import NetworkIsolationGuard, SandboxCluster

    key = os.environ["ARRMES_SYNTHETIC_SONARR_KEY"]
    wire_audit: list[dict] = []
    child = None
    result: dict = {"checks": {}, "operations": [], "wire_audit": wire_audit}
    cleanup = {"application_stopped": False, "audit_proxy_stopped": False,
               "sandbox_stopped": False, "graceful_application_shutdown": False}
    result["cleanup"] = cleanup

    def upstream_get(path, api_key):
        # No caller-controlled method or target can reach this transport. Docker
        # exec accesses only this harness's disposable container, while curl
        # talks HTTP over its loopback interface on the internal-only network.
        if not path.startswith("/api/v3/") or "\n" in path or "\r" in path:
            raise ValueError("Unexpected upstream API path")
        command = ["docker", "exec", upstream, "curl", "--silent", "--show-error", "--max-time", "5",
            "--request", "GET", "--header", "X-Api-Key: " + api_key, "--write-out", "\n%{http_code}",
            "http://127.0.0.1:8989" + path]
        response = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if response.returncode:
            raise OSError(response.stderr.decode(errors="replace").replace(api_key, "<synthetic-api-key-redacted>"))
        body, status = response.stdout.rsplit(b"\n", 1)
        return body, int(status)

    class ReadOnlyAuditProxy(BaseHTTPRequestHandler):
        def do_GET(self):
            body, status = upstream_get(self.path, self.headers.get("X-Api-Key", ""))
            wire_audit.append({"method": "GET", "path": self.path, "status": status,
                "synthetic_api_key_header_matches": self.headers.get("X-Api-Key") == key,
                "body_sha256": digest(body), "bytes": len(body)})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def deny_write(self):
            wire_audit.append({"method": self.command, "path": self.path, "forwarded": False})
            self.send_error(405, "Only GET is permitted in the upstream qualification")

        do_POST = do_PUT = do_PATCH = do_DELETE = deny_write

        def log_message(self, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ReadOnlyAuditProxy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        app_port = available.getsockname()[1]
    origin = f"http://127.0.0.1:{app_port}"
    try:
        with SandboxCluster(root / "sandbox") as sandbox:
            allowed_ports = {proxy.server_port, app_port, sandbox.port}
            with NetworkIsolationGuard(allowed_ports=allowed_ports) as guard:
                started = time.perf_counter()
                status = None
                for _ in range(240):
                    try:
                        body, response_status = upstream_get("/api/v3/system/status", key)
                        if response_status == 200:
                            status = json.loads(body)
                            break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)
                if status is None:
                    raise AssertionError("Actual Sonarr API did not become ready within the bounded startup window")
                result["sonarr_startup_wait_seconds"] = time.perf_counter() - started
                result["upstream_status_metadata"] = {name: status[name] for name in (
                    "appName", "instanceName", "version", "packageVersion", "packageAuthor", "osName",
                    "osVersion", "architecture", "runtimeName", "runtimeVersion") if name in status}
                result["checks"]["actual_sonarr_v4_api_v3_identity"] = (
                    status.get("appName") == "Sonarr" and str(status.get("version", "")).startswith("4."))
                token = "synthetic-" + "sonarr-upstream-reader"
                clients, services = root / "clients.json", root / "services.json"
                clients.write_text(json.dumps({"clients": [{"subject": "fixture:upstream-reader",
                    "token_sha256": digest(token.encode()), "scopes": ["media:read"]}]}))
                services.write_text(json.dumps({"services": {"sonarr": {
                    "base_url": f"http://127.0.0.1:{proxy.server_port}", "api_key": key}}}))
                env = {name: value for name, value in os.environ.items()
                       if not name.startswith("ARRMES_") and name != "GES_KEY_FILE"}
                env.update({"ARRMES_BACKEND": "native-only", "ARRMES_CLIENTS_FILE": str(clients),
                            "ARRMES_SERVICES_FILE": str(services), "ARRMES_WRITE_MODE": "disabled",
                            "ARRMES_PORT": str(app_port), "PYTHONDONTWRITEBYTECODE": "1"})
                guard_file = root / "app-isolation.json"
                command = [sys.executable, "-I", "-B", "-c", BOOTSTRAP, str(source / "src"), str(guard_file),
                           json.dumps(FORBIDDEN_IMPORTS), json.dumps(sorted(allowed_ports))]
                result["application_command"] = command
                with (root / "app.log").open("w+") as log:
                    child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                    health = None
                    for _ in range(100):
                        if child.poll() is not None:
                            log.seek(0)
                            raise RuntimeError(log.read())
                        try:
                            with urllib.request.urlopen(origin + "/healthz", timeout=0.1) as response:
                                health = json.load(response)
                            break
                        except (OSError, urllib.error.URLError):
                            time.sleep(0.05)
                    if health is None:
                        raise AssertionError("Arrmes native-only startup timed out")
                    result["arrmes_health"] = health
                    result["checks"]["model_free_public_package_main_loaded"] = (
                        health["backend"] == "native-only" and health["neural_model_loaded"] is False
                        and health["write_mode"] == "disabled")

                    def execute(query: str, authenticated: bool = True):
                        headers = {"Content-Type": "application/json"}
                        if authenticated:
                            headers["Authorization"] = "Bearer " + token
                        req = urllib.request.Request(origin + "/v1/execute", headers=headers,
                            data=json.dumps({"query": query}).encode())
                        with urllib.request.urlopen(req, timeout=15) as response:
                            return json.load(response)

                    result["checks"]["unauthenticated_http_execution_denied"] = False
                    try:
                        execute("Show Sonarr queue", False)
                    except urllib.error.HTTPError as exc:
                        result["checks"]["unauthenticated_http_execution_denied"] = exc.code == 401
                    for operation in ("health", "queue", "library", "history"):
                        query = f"Show Sonarr {operation}"
                        response = execute(query)
                        data = response.get("observation", {}).get("data", {})
                        correct = (response.get("terminal_state") == "completed"
                            and response.get("routing_source") == "explicit_service_deterministic"
                            and response.get("proposal") == {"name": "service_query", "arguments": {
                                "service": "sonarr", "operation": operation}}
                            and data.get("source") == "native_api")
                        if operation == "health":
                            correct = correct and data.get("server_version") == status.get("version")
                        else:
                            correct = correct and data.get("items") == [] and data.get("total_records") == 0
                        result["checks"][f"natural_language_{operation}_read_completed"] = bool(correct)
                        result["operations"].append({"query": query, "expected_operation": operation,
                                                     "response": response, "passed": bool(correct)})
                    result["checks"]["wire_audit_only_successful_upstream_gets"] = (
                        len(wire_audit) == 5 and all(row["method"] == "GET" and row["status"] == 200
                                                    and row["synthetic_api_key_header_matches"]
                                                    for row in wire_audit))
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=5)
                    cleanup["application_stopped"] = child.poll() is not None
                    cleanup["graceful_application_shutdown"] = guard_file.is_file()
                    child = None
                    result["application_isolation"] = json.loads(guard_file.read_text())
                    result["network_isolation"] = guard.evidence_summary()
        cleanup["sandbox_stopped"] = True
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=5)
            cleanup["application_stopped"] = True
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=3)
        cleanup["audit_proxy_stopped"] = not proxy_thread.is_alive()
    result["runtime"] = {"python_executable": sys.executable, "python_version": sys.version,
        "installed_distributions": sorted(({"name": dist.metadata["Name"], "version": dist.version}
            for dist in importlib.metadata.distributions()), key=lambda row: row["name"].lower())}
    result["all_checks_passed"] = all(result["checks"].values()) and all(cleanup.values())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--upstream", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name == "nt":
        raise SystemExit("This POSIX qualification harness does not validate native Windows")
    if args.worker_root:
        result = worker(args.worker_root, args.source_root, args.upstream)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        raise SystemExit(0 if result["all_checks_passed"] else 1)

    commands: list[list[str]] = []
    key = secrets.token_hex(16)

    def command(parts, *, env=None, timeout=120):
        commands.append([str(part).replace(key, "<synthetic-api-key-redacted>") for part in parts])
        completed = subprocess.run(parts, env=env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout)
        if completed.returncode:
            raise RuntimeError(completed.stdout.replace(key, "<synthetic-api-key-redacted>"))
        return completed.stdout

    source = args.source_root.resolve()
    files = {str(path.relative_to(source)): digest(path.read_bytes()) for path in sorted((source / "src").rglob("*.py"))}
    commit = command(["git", "-C", str(source), "rev-parse", "HEAD"]).strip()
    info = json.loads(command(["docker", "info", "--format", "{{json .}}"] ))
    context = command(["docker", "context", "show"]).strip()
    if (context != "desktop-linux" or info["Architecture"] not in {"aarch64", "arm64"}
            or info["OperatingSystem"] != "Docker Desktop"):
        raise SystemExit("This bounded run requires the local Docker Desktop ARM64 context")
    image = json.loads(command(["docker", "image", "inspect", IMAGE]))[0]
    resource_sample = command(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}"])
    aggregate_cpu_percent, container_bytes = 0.0, 0.0
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    for line in resource_sample.splitlines():
        cpu, memory, *_ = line.split()
        aggregate_cpu_percent += float(cpu.rstrip("%"))
        amount, unit = re.fullmatch(r"([0-9.]+)([A-Za-z]+)", memory).groups()
        container_bytes += float(amount) * units[unit]
    if info["NCPU"] < 2 or info["MemTotal"] < 4 * 1024**3:
        raise SystemExit("Insufficient Docker Desktop resource allocation")
    if info["MemTotal"] - container_bytes < 3 * 1024**3 or aggregate_cpu_percent > (info["NCPU"] - 1) * 100:
        raise SystemExit("Insufficient sampled Docker headroom for the bounded smoke")
    label = "arrmes-sonarr-qualification-" + uuid.uuid4().hex[:10]
    network, container = label + "-network", label + "-app"
    cleanup = {"container_removed": False, "network_removed": False, "temporary_environment_removed": False}
    receipt = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "track": "real_upstream_sonarr_empty_library_native_only_read_smoke",
        "qualification": "Actual Linux ARM64 Sonarr binary on macOS Docker Desktop VM; Mac ARM64 Arrmes native-only HTTP application. No inference qualification.",
        "limits": "Empty isolated app only: health/version, queue, library and history. No seeded entities, metadata-provider search, missing-episode semantics, writes, other services or production workloads qualified.",
        "source_root": str(source), "source_commit": commit, "source_file_sha256": files,
        "source_manifest_sha256": digest(json.dumps(files, sort_keys=True).encode()),
        "script_sha256": digest(Path(__file__).read_bytes()),
        "harness_dependencies_sha256": {"scripts/smoke_native_only.py": digest(Path(__file__).with_name("smoke_native_only.py").read_bytes())},
        "invocation": [sys.executable, *sys.argv], "commands": commands,
        "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "docker": {"context": context, **{name: info[name] for name in ("ServerVersion", "Architecture", "NCPU", "MemTotal")}},
        "preflight_existing_container_usage_without_names": resource_sample.splitlines(),
        "preflight_sample": {"aggregate_cpu_percent": aggregate_cpu_percent,
            "summed_container_memory_bytes": int(container_bytes),
            "memory_note": "Summed Docker container usage is not free VM RAM; a 3-GiB margin is required for the 768-MiB test and VM overhead."},
        "image": {"reference": IMAGE, **{name: image.get(name) for name in ("Id", "RepoDigests", "Architecture", "Os", "Size")}},
        "image_cache": "Required to be present before this harness starts; retained for reproduction. The harness pulls no images.",
        "container_budget": {"cpu_limit": 1, "memory_bytes": 768 * 1024**2, "memory_swap_bytes": 768 * 1024**2,
                             "pids_limit": 256, "config_tmpfs_bytes": 128 * 1024**2, "tmp_tmpfs_bytes": 64 * 1024**2},
        "traffic": "Arrmes -> GET-only audit proxy -> docker exec curl GET -> actual Sonarr HTTP on container loopback, unchanged JSON. This is not a direct network-client qualification. Registries/keys are synthetic; Sonarr has an internal-only network and publishes no ports.",
        "cleanup": cleanup}
    created_network = created_container = False
    try:
        command(["docker", "network", "create", "--internal", "--label", label, network])
        created_network = True
        xml = ("<Config><BindAddress>*</BindAddress><Port>8989</Port><EnableSsl>False</EnableSsl>"
               "<LaunchBrowser>False</LaunchBrowser><ApiKey>" + key + "</ApiKey>"
               "<AuthenticationMethod>External</AuthenticationMethod>"
               "<AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>"
               "<AnalyticsEnabled>False</AnalyticsEnabled><UpdateMechanism>Docker</UpdateMechanism></Config>")
        container_start = time.perf_counter()
        command(["docker", "run", "--detach", "--name", container, "--label", label,
            "--network", network, "--cpus", "1", "--memory", "768m",
            "--memory-swap", "768m", "--pids-limit", "256", "--cap-drop", "NET_RAW",
            "--security-opt", "no-new-privileges:true", "--tmpfs", "/config:rw,size=128m",
            "--tmpfs", "/tmp:rw,size=64m", "--env", "PUID=10001", "--env", "PGID=10001",
            "--env", "TZ=Etc/UTC", "--entrypoint", "/bin/sh", IMAGE, "-c",
            "printf '%s' '" + xml + "' > /config/config.xml; exec /init"])
        created_container = True
        inspect = json.loads(command(["docker", "inspect", container]))[0]
        receipt["container_policy_actual"] = {name: inspect["HostConfig"][name] for name in (
            "NanoCpus", "Memory", "MemorySwap", "PidsLimit", "Tmpfs", "CapDrop", "SecurityOpt")}
        net = json.loads(command(["docker", "network", "inspect", network]))[0]
        receipt["network_internal"] = net["Internal"]
        receipt["host_port_bindings"] = inspect["HostConfig"]["PortBindings"]
        receipt["container_mount_types"] = [row["Type"] for row in inspect["Mounts"]]
        with tempfile.TemporaryDirectory(prefix="arrmes-sonarr-upstream-") as temporary:
            root = Path(temporary)
            snapshot = root / "source"
            for name, expected in files.items():
                target = snapshot / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, target)
                if digest(target.read_bytes()) != expected:
                    raise RuntimeError("Source changed during snapshot")
            command([args.python, "-m", "venv", str(root / "venv")])
            python = str(root / "venv/bin/python")
            command([python, "-m", "pip", "install", "--disable-pip-version-check", "jsonschema==4.26.0",
                "attrs==26.1.0", "jsonschema-specifications==2025.9.1", "referencing==0.37.0",
                "rpds-py==2026.9.1", "typing_extensions==4.16.0"])
            output = root / "worker.json"
            env = {**os.environ, "ARRMES_SYNTHETIC_SONARR_KEY": key}
            try:
                command([python, "-I", "-B", str(Path(__file__).resolve()), "--source-root", str(snapshot),
                    "--worker-root", str(root), "--upstream", container, "--output", str(output)], env=env)
            finally:
                # Preserve actual operation responses even when a check fails.
                if output.is_file():
                    child_receipt = json.loads(output.read_text())
                    cleanup.update(child_receipt.pop("cleanup"))
                    receipt.update(child_receipt)
        cleanup["temporary_environment_removed"] = not root.exists()
        receipt["whole_run_seconds_before_teardown"] = time.perf_counter() - container_start
        receipt["container_stats_sample"] = json.loads(command(["docker", "stats", "--no-stream", "--format", "{{json .}}", container]))
        receipt["container_state"] = {name: value for name, value in json.loads(command(["docker", "inspect", container]))[0]["State"].items()
                                      if name in {"Running", "OOMKilled", "Error", "StartedAt"}}
    except Exception as exc:
        receipt["error"] = str(exc).replace(key, "<synthetic-api-key-redacted>")
        receipt["all_checks_passed"] = False
        if created_container:
            receipt["upstream_log_tail"] = command(["docker", "logs", "--tail", "40", container]).replace(key, "<synthetic-api-key-redacted>")
    finally:
        if created_container:
            command(["docker", "rm", "--force", "--volumes", container])
        cleanup["container_removed"] = subprocess.run(["docker", "container", "inspect", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
        if created_network:
            command(["docker", "network", "rm", network])
        cleanup["network_removed"] = subprocess.run(["docker", "network", "inspect", network],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    receipt["all_checks_passed"] = bool(receipt.get("all_checks_passed")) and all(cleanup.values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"all_checks_passed": receipt["all_checks_passed"], "checks": receipt.get("checks"),
        "error": receipt.get("error"), "cleanup": cleanup, "output": str(args.output)}, indent=2))
    raise SystemExit(0 if receipt["all_checks_passed"] else 1)


if __name__ == "__main__":
    main()
