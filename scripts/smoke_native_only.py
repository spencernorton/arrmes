"""Reproduce model-free package-main bootstrap in a fresh, minimal environment.

This is a bounded packaging smoke test against synthetic loopback fixtures, not
a model-quality benchmark or native Windows qualification. The dependency setup
may access the package index; all application requests run under the sandbox's
Python network guard. No production registry or existing model is read.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import platform
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


FORBIDDEN_IMPORTS = ["mlx", "mlx_lm", "torch", "transformers", "numpy", "llama_cpp"]
BOOTSTRAP = """import sys,runpy,importlib.abc,json
from pathlib import Path
attempts=[]
forbidden=set(json.loads(sys.argv[3]))
class RejectOptional(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,*args):
  if fullname.split('.')[0] in forbidden:
   attempts.append(fullname)
   raise ImportError('Optional inference import forbidden: '+fullname)
sys.meta_path.insert(0,RejectOptional())
sys.path.insert(0,sys.argv[1])
from arrmes.sandbox import NetworkIsolationGuard
guard=NetworkIsolationGuard(allowed_ports=set(json.loads(sys.argv[4])))
try:
 with guard:
  runpy.run_module('arrmes.server',run_name='__main__')
finally:
 Path(sys.argv[2]).write_text(json.dumps({
  'network':guard.evidence_summary(),
  'forbidden_optional_import_attempts':attempts,
  'forbidden_modules_loaded':sorted(name for name in sys.modules if name.split('.')[0] in forbidden),
 }))
"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_checked(command: list[str], commands: list[list[str]], *, env=None) -> str:
    commands.append(command)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=120, env=env)
    if result.returncode:
        raise RuntimeError(f"Command failed: {command!r}\n{result.stdout}")
    return result.stdout


def worker(root: Path, source: Path) -> dict:
    sys.path.insert(0, str(source / "src"))
    from arrmes.sandbox import NetworkIsolationGuard, SandboxCluster, SandboxIsolationViolation

    checks: dict[str, bool] = {}
    child = None
    audit: list[dict] = []

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            audit.append({"method": "GET", "path": self.path})
            if not self.path.startswith("/api/v3/queue?"):
                self.send_error(404)
                return
            payload = json.dumps({"page": 1, "pageSize": 50, "totalRecords": 1,
                                  "records": [{"id": 1, "title": "Sintel", "status": "downloading"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    cleanup = {"application_stopped": False, "fixture_stopped": False,
               "sandbox_stopped": False, "graceful_application_shutdown": False}
    result: dict = {"checks": checks, "cleanup": cleanup}
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    try:
        with SandboxCluster(root / "sandbox") as cluster:
            allowed_ports = {fixture.server_port, cluster.port, port}
            with NetworkIsolationGuard(allowed_ports=allowed_ports) as guard:
                # Reserved documentation addresses are denied before any socket
                # connection or external DNS resolution can occur.
                for address in (("192.0.2.1", 443), ("unrelated.invalid", 443)):
                    try:
                        socket.create_connection(address, timeout=0.1)
                    except SandboxIsolationViolation:
                        pass
                    else:
                        raise AssertionError("Network-isolation negative probe escaped")
                token = "synthetic-" + "native-only-smoke-token"
                clients, services = root / "clients.json", root / "services.json"
                clients.write_text(json.dumps({"clients": [{"subject": "fixture:native-only-smoke",
                    "token_sha256": digest(token.encode()), "scopes": ["media:read", "media:manage"]}]}))
                services.write_text(json.dumps({"services": {"sonarr": {
                    "base_url": f"http://127.0.0.1:{fixture.server_port}", "api_key": "synthetic-" + "fixture-only"}}}))
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith("ARRMES_") and key != "GES_KEY_FILE"}
                configured = {"ARRMES_BACKEND": "native-only", "ARRMES_CLIENTS_FILE": str(clients),
                    "ARRMES_SERVICES_FILE": str(services), "ARRMES_PORT": str(port),
                    "ARRMES_WRITE_MODE": "confirm", "ARRMES_MODEL_PATH": str(root / "not-downloaded.gguf"),
                    "ARRMES_ADAPTER_PATH": str(root / "not-downloaded-adapter"), "PYTHONDONTWRITEBYTECODE": "1"}
                env.update(configured)
                guard_path = root / "application-guard.json"
                command = [sys.executable, "-I", "-B", "-c", BOOTSTRAP, str(source / "src"), str(guard_path),
                           json.dumps(FORBIDDEN_IMPORTS), json.dumps(sorted(allowed_ports))]
                result.update({"application_command": command, "application_environment_overrides": configured,
                               "bootstrap_code_sha256": digest(BOOTSTRAP.encode())})
                with (root / "application.log").open("w+") as log:
                    child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                    health = None
                    for _ in range(200):
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
                        raise AssertionError("Application startup timed out")
                    checks["package_main_starts_without_weights_runtime_or_optional_imports"] = (
                        health["backend"] == "native-only" and health["neural_model_loaded"] is False)
                    checks["confirm_environment_forced_disabled"] = health["write_mode"] == "disabled"

                    def request(path: str, query: str, authenticated: bool = True) -> dict:
                        headers = {"Content-Type": "application/json"}
                        if authenticated:
                            headers["Authorization"] = "Bearer " + token
                        req = urllib.request.Request(origin + path,
                            data=json.dumps({"query": query}).encode(), headers=headers)
                        with urllib.request.urlopen(req, timeout=3) as response:
                            return json.load(response)

                    checks["unauthenticated_route_denied"] = False
                    try:
                        request("/v1/route", "Show Sonarr queue", False)
                    except urllib.error.HTTPError as exc:
                        checks["unauthenticated_route_denied"] = exc.code == 401
                    executed = request("/v1/execute", "Show Sonarr queue")
                    checks["actual_native_read_fixture_executes"] = (
                        executed["terminal_state"] == "completed"
                        and executed["observation"]["data"]["items"][0]["title"] == "Sintel"
                        and executed["routing_source"] == "explicit_service_deterministic")
                    refused = request("/v1/execute", "Pause the torrent Sintel")
                    checks["write_intent_abstains_without_dispatch"] = (
                        refused["terminal_state"] in {"clarify", "unsupported", "denied"}
                        and refused["steps_executed"] == 0)
                    clarified = request("/v1/route", "Tell me the weather")
                    checks["outside_grammar_clarifies"] = (
                        clarified["proposal"]["name"] == "clarify"
                        and clarified["routing_source"] == "native_only_abstention")
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=5)
                    cleanup["graceful_application_shutdown"] = guard_path.is_file()
                    cleanup["application_stopped"] = child.poll() is not None
                    child = None
                    application_guard = json.loads(guard_path.read_text())
                    if (application_guard["forbidden_optional_import_attempts"]
                            or application_guard["forbidden_modules_loaded"]):
                        checks["package_main_starts_without_weights_runtime_or_optional_imports"] = False
                    result.update({"health": health, "fixture_execution_response": executed,
                        "write_intent_response": refused, "outside_grammar_response": clarified,
                        "application_isolation": application_guard,
                        "fixture_audit": audit, "sandbox_active": True,
                        "network_isolation": guard.evidence_summary()})
        cleanup["sandbox_stopped"] = True
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=5)
            cleanup["application_stopped"] = True
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=3)
        cleanup["fixture_stopped"] = not thread.is_alive()
    result["runtime"] = {"python_executable": sys.executable, "python_version": sys.version,
        "installed_distributions": sorted(({"name": dist.metadata["Name"], "version": dist.version}
            for dist in importlib.metadata.distributions()), key=lambda item: item["name"].lower())}
    result["all_checks_passed"] = len(checks) == 6 and all(checks.values()) and all(cleanup.values())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=sys.executable, help="Python 3.12+ used to create the clean venv")
    parser.add_argument("--dependency", action="append", help="Optional exact dependency pins; repeat to reproduce a receipt")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name == "nt":
        raise SystemExit("This smoke harness uses POSIX SIGINT cleanup; it does not qualify native Windows execution")
    if args.worker_root:
        result = worker(args.worker_root, args.source_root)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        raise SystemExit(0 if result["all_checks_passed"] else 1)

    source = args.source_root.resolve()
    commands: list[list[str]] = []
    files = {str(path.relative_to(source)): digest(path.read_bytes())
             for path in sorted((source / "src").rglob("*.py"))}
    try:
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    except subprocess.CalledProcessError:
        commit = None
    receipt = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "track": "native_only_package_main_minimal_environment_smoke_not_model_quality",
        "qualification": f"Actual {platform.system()} {platform.machine()} run only; no native Windows inference or accelerator qualification",
        "hardware": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "script_sha256": digest(Path(__file__).read_bytes()), "invocation": [sys.executable, *sys.argv],
        "source_root": str(source), "source_commit": commit, "source_file_sha256": files,
        "source_manifest_sha256": digest(json.dumps(files, sort_keys=True).encode()),
        "source_execution": "Copied immutable Python source snapshot; hashes checked before execution",
        "forbidden_optional_imports": FORBIDDEN_IMPORTS,
        "dependency_setup_note": "Package-index access is used only for fresh environment setup; application and fixture requests are loopback-guarded",
        "commands": commands}
    with tempfile.TemporaryDirectory(prefix="arrmes-native-only-smoke-") as temp:
        root = Path(temp)
        snapshot = root / "source"
        for name, expected in files.items():
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, destination)
            if digest(destination.read_bytes()) != expected:
                raise RuntimeError("Source changed during the snapshot; rerun against a stable tree")
        run_checked([args.python, "-m", "venv", str(root / "venv")], commands)
        python = str(root / "venv" / "bin" / "python")
        run_checked([python, "-m", "pip", "install", "--disable-pip-version-check",
                     *(args.dependency or ["jsonschema>=4.23,<5"])], commands)
        worker_output = root / "worker-result.json"
        run_checked([python, "-I", "-B", str(Path(__file__).resolve()), "--source-root", str(snapshot),
                     "--worker-root", str(root), "--output", str(worker_output)], commands)
        receipt.update(json.loads(worker_output.read_text()))
    # Re-run the same source revision with these explicit package pins to avoid
    # silently changing a future environment when transitive dependencies move.
    packages = receipt["runtime"]["installed_distributions"]
    receipt["reproduce_dependency_arguments"] = [part for item in packages
        for part in ("--dependency", f"{item['name']}=={item['version']}")]
    receipt["cleanup"]["temporary_environment_registries_and_snapshot_removed"] = not root.exists()
    receipt["all_checks_passed"] = receipt["all_checks_passed"] and all(receipt["cleanup"].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"all_checks_passed": receipt["all_checks_passed"], "checks": receipt["checks"],
                      "cleanup": receipt["cleanup"], "output": str(args.output)}, indent=2))
    raise SystemExit(0 if receipt["all_checks_passed"] else 1)


if __name__ == "__main__":
    main()
