#!/usr/bin/env python3
"""Bounded actual Seerr 3.5.1 identity/empty-read qualification on Linux x86_64.

Requires the explicitly prepared pinned official image and jsonschema. No image
is pulled by this script. One network-none, read-only-root container has 1 CPU,
1.5 GiB RAM, no GPU/devices/host ports/bind mounts, and disposable tmpfs config.
Two synthetic users are inserted with the upstream User ORM after migration.
No external-media login, metadata query, request creation or provider is used.

The GET-only proxy forwards unchanged responses from container loopback through
Docker exec. This is actual upstream execution, not direct TCP qualification.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

IMAGE = "ghcr.io/seerr-team/seerr@sha256:2636191726a315dbd75228537f7be74b2fa7498998bb22e37b5b5c8756018fba"
INDEX_SHA256 = "d8fab675b55ad293ded98fd71efcfd46054cbad9746534ae1cc4794a88c0b3be"
CONFIG_SHA256 = "61f6fa0cf472caa95736d00fa624aea848f687b2ac899c2370a509de93e8abba"
UPSTREAM_COMMIT = "564bfb08b54caa374bddaf820abfd226e5e69151"
SOURCE_BASIS = "e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4"
MEMORY = 1536 * 1024**2

# Fixed method, host and port. Inputs and credentials travel over stdin, never
# in process arguments or receipt command logs. Node never follows redirects.
READ_JS = r"""
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', x => { input += x; if (input.length > 8192) process.exit(2); });
process.stdin.on('end', () => {
  const p = JSON.parse(input);
  if (!/^\/api\/v1\/(?:status|auth\/me|request(?:\?.*)?)$/.test(p.path) ||
      /[\r\n#]/.test(p.path)) process.exit(2);
  const headers = {};
  if (p.api_key !== null) headers['X-Api-Key'] = p.api_key;
  if (p.user_id !== null) headers['X-API-User'] = p.user_id;
  const req = require('http').request({host:'127.0.0.1', port:5055,
      path:p.path, method:'GET', headers:headers}, res => {
    const chunks = []; let size = 0;
    res.on('data', x => {size += x.length; if (size > 2097152) req.destroy(); else chunks.push(x);});
    res.on('end', () => process.stdout.write(JSON.stringify({
      status:res.statusCode, body:Buffer.concat(chunks).toString('base64')})));
  });
  req.setTimeout(5000, () => req.destroy());
  req.on('error', () => process.exit(3));
  req.end();
});
"""

# Local fixture bootstrap, not a media request or a normal setup-flow claim.
# It uses the stable application's own entity/enum implementation. Existing
# users or media requests make it refuse, so it cannot seed a live database.
SEED_JS = r"""
require('reflect-metadata');
const ds = require('./dist/datasource').default;
const {User} = require('./dist/entity/User');
const {UserType} = require('./dist/constants/user');
const {Permission} = require('./dist/lib/permissions');
(async () => {
  await ds.initialize();
  const users = ds.getRepository(User);
  if (await users.count() !== 0) throw new Error('nonempty bootstrap refused');
  const requests = await ds.query('SELECT COUNT(*) AS n FROM media_request');
  if (Number(requests[0].n) !== 0) throw new Error('nonempty bootstrap refused');
  await users.save(users.create({id:1, email:'owner@example.invalid',
    username:'synthetic-owner', userType:UserType.LOCAL, permissions:Permission.ADMIN, avatar:''}));
  await users.save(users.create({id:2, email:'reader@example.invalid',
    username:'synthetic-reader', userType:UserType.LOCAL, permissions:Permission.REQUEST, avatar:''}));
  process.stdout.write(JSON.stringify({synthetic_user_ids:[1,2], media_requests:0}));
  await ds.destroy();
})().catch(() => { process.exitCode = 1; });
"""


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def command(parts, *, input_data=None, timeout=15):
    process = subprocess.run(parts, input=input_data, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=timeout)
    if process.returncode:
        raise RuntimeError("bounded command failed")
    return process.stdout


def validate_read_path(path: str) -> None:
    if not isinstance(path, str) or len(path) > 512 or any(ord(c) < 32 for c in path):
        raise ValueError("invalid read path")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("invalid read path")
    if parsed.path in {"/api/v1/status", "/api/v1/auth/me"} and not parsed.query:
        return
    if parsed.path != "/api/v1/request":
        raise ValueError("read endpoint not allowed")
    pairs = urllib.parse.parse_qsl(parsed.query, strict_parsing=True)
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) != {"requestedBy", "take", "skip", "filter", "sort"}:
        raise ValueError("invalid owned-request query")
    if (not re.fullmatch(r"[1-9][0-9]{0,8}", query["requestedBy"])
            or query["take"] != "2" or query["skip"] != "0"
            or query["filter"] != "all" or query["sort"] != "added"):
        raise ValueError("invalid owned-request query")


class DockerReadTransport:
    def __init__(self, container: str, key: str, audit: list):
        if not re.fullmatch(r"arrmes-seerr-qualification-[a-f0-9]{12}", container):
            raise ValueError("unexpected qualification container")
        self.container, self.key, self.audit = container, key, audit

    def get(self, path, api_key, user_id):
        validate_read_path(path)
        if user_id is not None and not re.fullmatch(r"[1-9][0-9]{0,8}", str(user_id)):
            raise ValueError("invalid synthetic user header")
        payload = {"path": path, "api_key": api_key, "user_id": None if user_id is None else str(user_id)}
        envelope = json.loads(command(["docker", "exec", "-i", self.container, "node", "-e", READ_JS],
                                      input_data=json.dumps(payload).encode(), timeout=10))
        body = base64.b64decode(envelope["body"], validate=True)
        status = envelope["status"]
        if type(status) is not int or not 100 <= status <= 599 or len(body) > 2 * 1024**2:
            raise ValueError("invalid transport response")
        self.audit.append({"method": "GET", "path": path, "status": status,
                           "api_key_matches": api_key == self.key,
                           "user_header": None if user_id is None else str(user_id),
                           "body_sha256": digest(body), "bytes": len(body)})
        return body, status


@contextmanager
def read_proxy(transport: DockerReadTransport, cleanup=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                body, status = transport.get(self.path, self.headers.get("X-Api-Key"),
                                             self.headers.get("X-API-User"))
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                body, status = b'{"error":"qualification_transport_failure"}', 502
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def deny(self):
            self.send_error(405)

        do_POST = do_PUT = do_PATCH = do_DELETE = deny

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    if cleanup is not None:
        cleanup["proxy_stopped"] = False
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if cleanup is not None:
            cleanup["proxy_stopped"] = not thread.is_alive()
        if thread.is_alive():
            raise RuntimeError("audit proxy did not stop")


def resource_sample() -> dict:
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    sample = {"cpus": os.cpu_count(), "load_average": list(os.getloadavg()),
              "mem_available_bytes": int(memory["MemAvailable"].split()[0]) * 1024,
              "cpu_pressure": Path("/proc/pressure/cpu").read_text().strip(),
              "memory_pressure": Path("/proc/pressure/memory").read_text().strip()}
    if sample["cpus"] < 2 or sample["mem_available_bytes"] < 4 * 1024**3 or sample["load_average"][0] > sample["cpus"] * .75:
        raise RuntimeError("insufficient sampled host headroom")
    return sample


def _source_manifest(source):
    return {p.relative_to(source).as_posix(): digest(p.read_bytes())
            for p in sorted((source / "src").rglob("*.py"))}


def validate_image_identity(image: dict) -> None:
    # Docker's containerd image store reports the manifest as Id; the legacy
    # image store reports the config. Both still require the exact repo digest.
    manifest_digest = IMAGE.rsplit("@", 1)[1]
    labels = image.get("Config", {}).get("Labels", {})
    if (image.get("Id") not in {"sha256:" + CONFIG_SHA256, manifest_digest}
            or IMAGE not in image.get("RepoDigests", [])
            or image.get("Descriptor", {}).get("digest", manifest_digest) != manifest_digest
            or image.get("Architecture") != "amd64" or image.get("Os") != "linux"
            or labels.get("org.opencontainers.image.revision") != UPSTREAM_COMMIT
            or labels.get("org.opencontainers.image.source") != "https://github.com/seerr-team/seerr"
            or labels.get("org.opencontainers.image.version") != "v3.5.1"):
        raise RuntimeError("prepared image identity mismatch")


def probe_reads(source, transport, receipt):
    sys.path.insert(0, str(source / "src"))
    from arrmes.executor import BoundedExecutor
    from arrmes.explicit import NativeOnlyModel
    from arrmes.planner import Principal
    from arrmes.protocol import Call
    from arrmes.sandbox import NetworkIsolationGuard
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    from arrmes.services import ServiceConfig, ServiceQueryAdapter
    from arrmes.services.requests import NativeRequestAdapter

    checks = receipt["checks"]
    def read(path, key, user):
        blob, status = transport.get(path, key, user)
        return json.loads(blob), status
    status_body, status = read("/api/v1/status", transport.key, 2)
    checks["actual_upstream_status_3_5_1"] = status == 200 and status_body.get("version") == "3.5.1"
    receipt["upstream_status"] = {"http_status": status, "version": status_body.get("version")}
    identity, status = read("/api/v1/auth/me", transport.key, 2)
    created = datetime.fromisoformat(identity.get("createdAt", "").replace("Z", "+00:00"))
    checks["mapped_identity_has_actual_created_at"] = status == 200 and identity.get("id") == 2 and created.tzinfo is not None
    receipt["identity_shape"] = {"http_status": status, "id": identity.get("id"),
        "created_at_present": isinstance(identity.get("createdAt"), str), "created_at_timezone_aware": created.tzinfo is not None,
        "created_at_sha256": digest(str(identity.get("createdAt")).encode()),
        "email_present": isinstance(identity.get("email"), str), "email_value_omitted": True}
    default, status = read("/api/v1/auth/me", transport.key, None)
    checks["missing_user_header_defaults_to_administrator_one"] = status == 200 and default.get("id") == 1
    receipt["auth_probe_http_statuses"] = {}
    for label, key, user, expected_status in [
        ("unknown_user", transport.key, 999999, 403), ("bad_key", "invalid-synthetic-key", 2, 403),
        ("missing_key", None, 2, 401),
    ]:
        _, status = read("/api/v1/auth/me", key, user)
        receipt["auth_probe_http_statuses"][label] = status
        checks[label + "_identity_rejected"] = status == expected_status
    request_path = "/api/v1/request?requestedBy=2&take=2&skip=0&filter=all&sort=added"
    own, status = read(request_path, transport.key, 2)
    checks["actual_caller_owned_empty_request_page"] = (status == 200 and own.get("results") == []
        and own.get("pageInfo", {}).get("results") == 0 and own.get("pageInfo", {}).get("pageSize") == 2)
    receipt["empty_request_page"] = {"http_status": status, "pageInfo": own.get("pageInfo"),
                                     "results": own.get("results")}
    _, status = read(request_path.replace("requestedBy=2", "requestedBy=1"), transport.key, 2)
    checks["nonadmin_foreign_owner_filter_is_403_even_empty"] = status == 403
    receipt["request_auth_probe_http_statuses"] = {}
    for label, key, user, expected_status in [
        ("unknown_user", transport.key, 999999, 403), ("bad_key", "invalid-synthetic-key", 2, 403),
        ("missing_key", None, 2, 401),
    ]:
        _, status = read(request_path, key, user)
        receipt["request_auth_probe_http_statuses"][label] = status
        checks[label + "_empty_request_read_rejected"] = status == expected_status
    with read_proxy(transport, receipt["cleanup"]) as proxy:
        with NetworkIsolationGuard(allowed_ports={proxy.server_port}) as guard:
            config = ServiceConfig("http://127.0.0.1:" + str(proxy.server_port), api_key=transport.key,
                                   user_ids={"synthetic-reader": 2})
            native = ServiceQueryAdapter({"seerr": config}, page_size=2, max_pages=1, max_records=2)
            requests = NativeRequestAdapter(native, write_mode="disabled")
            principal = Principal("synthetic-reader", frozenset({"media:read", "requests:read"}))
            context, audit = requests._context(principal), []
            calls_before = len(receipt["wire_audit"])
            fingerprint = requests._identity(context, audit)
            checks["arrmes_identity_fingerprint_accepts_actual_created_at"] = bool(re.fullmatch(r"[a-f0-9]{64}", fingerprint))
            checks["arrmes_version_guard_accepts_actual_3_5_1"] = requests._version(context, audit) == "3.5.1"
            checks["arrmes_bounded_owned_request_parser_accepts_actual_empty_page"] = requests._own_requests(context, audit) == []
            checks["arrmes_request_read_helpers_always_send_mapped_user_and_key"] = all(
                row["user_header"] == "2" and row["api_key_matches"]
                for row in receipt["wire_audit"][calls_before:])
            receipt["native_request_read_audit"] = audit
            before = len(receipt["wire_audit"])
            denied = requests.execute(Call("request_status", {"title": "Unresolved synthetic title"}),
                                      Principal("synthetic-reader", frozenset()))
            checks["current_missing_scope_denied_without_upstream"] = denied.status == "denied" and len(receipt["wire_audit"]) == before
            denied = requests.execute(Call("request_status", {"title": "Unresolved synthetic title"}),
                                      Principal("unmapped-subject", frozenset({"requests:read"})))
            checks["missing_mapping_denied_without_upstream"] = denied.status == "denied" and len(receipt["wire_audit"]) == before
            invalid_config = ServiceConfig(config.base_url, api_key=transport.key,
                                            user_ids={"synthetic-reader": 999999})
            invalid_requests = NativeRequestAdapter(ServiceQueryAdapter({"seerr": invalid_config}), write_mode="disabled")
            before = len(receipt["wire_audit"])
            denied = invalid_requests.execute(Call("request_status", {"title": "Unresolved synthetic title"}), principal)
            checks["actual_unknown_identity_403_propagates_before_metadata_lookup"] = (
                denied.status == "denied" and denied.http_status == 403
                and len(receipt["wire_audit"]) == before + 1
                and receipt["wire_audit"][-1]["path"] == "/api/v1/auth/me")
            class NoInference(NativeOnlyModel):
                def infer(self, *args, **kwargs):
                    raise AssertionError("no inference is permitted")
            runtime = ArrmesRuntimeService(model=NoInference(), write_mode="disabled",
                executor=BoundedExecutor(ConfiguredAdapterRouter(native, write_mode="disabled"), allow_writes=False))
            response = runtime.execute("Show Seerr health", principal)
            receipt["native_only_health_response"] = response
            checks["explicit_natural_language_health_executes_against_actual_binary"] = (
                response.get("terminal_state") == "completed"
                and response.get("routing_source") == "explicit_service_deterministic"
                and response.get("observation", {}).get("data", {}).get("server_version") == "3.5.1")
            receipt["sdk_network_isolation"] = guard.evidence_summary()
        checks["sdk_no_inference_runtime_imported"] = not any(
            name == root or name.startswith(root + ".") for root in ("torch", "mlx", "mlx_lm", "llama_cpp", "transformers")
            for name in sys.modules)
    receipt["limits"] = {
        "actual_binary": "Linux x86_64 Seerr3.5.1 official pinned image",
        "identity": "Two synthetic users inserted with actual stable User ORM; normal external-account setup is not tested.",
        "owned_requests": "Empty request database only; no nonempty ownership/content/lifecycle qualification.",
        "request_status_end_to_end": "Not tested: title resolution requires metadata-provider requests deliberately excluded here.",
        "direct_network_client": "Not tested: GET-only Docker-exec transport forwards unchanged loopback HTTP responses.",
        "writes": "No HTTP request creation, media/account provider calls, indexer writes, or confirmation redemption.",
        "latency": "Transport/process startup overhead is not a routing or service performance benchmark."}


def run(source: Path, work_root: Path) -> dict:
    receipt = {"schema_version": 1, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "track": "actual_seerr_3_5_1_identity_empty_read_qualification",
        "all_checks_passed": False, "checks": {}, "wire_audit": [], "startup_failures": [],
        "source_basis_commit": SOURCE_BASIS, "script_sha256": digest(Path(__file__).read_bytes()),
        "source_file_sha256": _source_manifest(source),
        "host": {"system": platform.system(), "machine": platform.machine(), "kernel": platform.release()},
        "image": {"reference": IMAGE, "index_sha256": INDEX_SHA256, "config_sha256": CONFIG_SHA256,
                  "source_revision": UPSTREAM_COMMIT, "official_tag": "ghcr.io/seerr-team/seerr:v3.5.1",
                  "signature_verification": "not performed; tag/index/AMD64 manifest/config digests and official OCI labels checked separately"},
        "container_budget": {"cpu": 1, "memory_bytes": MEMORY, "swap_total_bytes": MEMORY, "pids": 128},
        "cleanup": {"container_removed": False, "proxy_stopped": True, "temporary_directory_removed": True},
        "image_cache": "Pinned image must already be prepared; retained by this harness. No images pulled or pruned.",
        "stage": "preflight"}
    container = "arrmes-seerr-qualification-" + uuid.uuid4().hex[:12]
    key = secrets.token_hex(32)
    try:
        if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise RuntimeError("Linux x86_64 is required")
        if not receipt["source_file_sha256"] or not work_root.is_dir():
            raise RuntimeError("source and approved scratch directory are required")
        receipt["preflight_resources"] = resource_sample()
        info = json.loads(command(["docker", "info", "--format",
            '{"version":"{{.ServerVersion}}","os":"{{.OSType}}","architecture":"{{.Architecture}}","cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}']))
        receipt["docker"] = info
        receipt["stage"] = "image_identity"
        image = json.loads(command(["docker", "image", "inspect", IMAGE]))[0]
        validate_image_identity(image)
        receipt["image"].update({"id": image["Id"], "size_bytes": image["Size"], "repo_digests": image["RepoDigests"]})
        receipt["checks"]["prepared_official_image_identity_matches"] = True
        receipt["stage"] = "container_start"
        command(["docker", "run", "--detach", "--name", container, "--label", "arrmes.qualification=" + container,
            "--network", "none", "--runtime", "runc", "--read-only", "--init", "--cpus", "1",
            "--memory", str(MEMORY), "--memory-swap", str(MEMORY), "--pids-limit", "128",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "1000:1000",
            "--tmpfs", "/app/config:rw,size=64m,uid=1000,gid=1000,mode=0700",
            "--tmpfs", "/tmp:rw,size=64m,uid=1000,gid=1000,mode=0700",
            "--tmpfs", "/app/.next/cache:rw,size=32m,uid=1000,gid=1000,mode=0700",
            "--env", "API_KEY=" + key, "--env", "TZ=Etc/UTC", "--env", "LOG_LEVEL=error",
            "--env", "NODE_OPTIONS=--max-old-space-size=768", "--env", "UV_THREADPOOL_SIZE=2", IMAGE], timeout=30)
        inspection = json.loads(command(["docker", "inspect", container]))[0]
        host = inspection["HostConfig"]
        actual = {k: host.get(k) for k in ("NanoCpus", "Memory", "MemorySwap", "PidsLimit", "NetworkMode",
                                         "ReadonlyRootfs", "PortBindings", "Binds", "DeviceRequests", "Devices",
                                         "CapDrop", "SecurityOpt", "Tmpfs", "Runtime")}
        receipt["container_policy_actual"] = actual
        receipt["checks"]["container_is_bounded_no_gpu_no_ports_no_mounts"] = (
            actual["NanoCpus"] == 10**9 and actual["Memory"] == actual["MemorySwap"] == MEMORY
            and actual["PidsLimit"] == 128 and actual["NetworkMode"] == "none" and actual["ReadonlyRootfs"]
            and not actual["PortBindings"] and not actual["Binds"] and not actual["DeviceRequests"]
            and not actual["Devices"] and actual["Runtime"] == "runc" and actual["CapDrop"] == ["ALL"])
        if not receipt["checks"]["container_is_bounded_no_gpu_no_ports_no_mounts"]:
            raise RuntimeError("container isolation policy differs")
        transport = DockerReadTransport(container, key, receipt["wire_audit"])
        receipt["stage"] = "startup"
        deadline = time.monotonic() + 60
        while True:
            try:
                _, status = transport.get("/api/v1/status", key, 2)
                if status == 200:
                    break
                receipt["startup_failures"].append({"http_status": status})
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                receipt["startup_failures"].append({"error_type": type(exc).__name__})
            if time.monotonic() >= deadline:
                raise RuntimeError("upstream startup deadline exceeded")
            time.sleep(.5)
        receipt["stage"] = "synthetic_user_bootstrap"
        seeded = json.loads(command(["docker", "exec", container, "node", "-e", SEED_JS], timeout=15))
        receipt["bootstrap"] = seeded
        receipt["checks"]["empty_database_seeded_only_synthetic_users"] = seeded == {"synthetic_user_ids": [1, 2], "media_requests": 0}
        receipt["stage"] = "read_probes"
        temporary = None
        try:
            with tempfile.TemporaryDirectory(prefix="arrmes-seerr-", dir=work_root) as temporary:
                receipt["cleanup"]["temporary_directory_removed"] = False
                probe_reads(source, transport, receipt)
        finally:
            receipt["cleanup"]["temporary_directory_removed"] = temporary is None or not Path(temporary).exists()
        receipt["stage"] = "resource_receipt"
        receipt["container_memory_peak_bytes"] = int(command(
            ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.peak"]).strip())
        receipt["postflight_resources"] = resource_sample()
        receipt["checks"]["application_source_unchanged"] = _source_manifest(source) == receipt["source_file_sha256"]
        receipt["checks"]["all_forwarded_requests_are_allowlisted_gets"] = all(
            item["method"] == "GET" for item in receipt["wire_audit"])
        receipt["runtime"] = {"python": platform.python_version(), "jsonschema": importlib.metadata.version("jsonschema"),
            "installed_distributions": sorted(
                [{"name": dist.metadata["Name"], "version": dist.version} for dist in importlib.metadata.distributions()],
                key=lambda item: item["name"].casefold())}
        receipt["all_checks_passed"] = all(receipt["checks"].values())
        receipt["stage"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        receipt["error"] = {"stage": receipt["stage"], "error_type": type(exc).__name__}
        receipt["all_checks_passed"] = False
    finally:
        try:
            inspected = subprocess.run(["docker", "inspect", container], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=10)
            if inspected.returncode == 0:
                item = json.loads(inspected.stdout)[0]
                if item.get("Config", {}).get("Labels", {}).get("arrmes.qualification") == container:
                    if "error" in receipt:
                        log = subprocess.run(["docker", "logs", "--tail", "40", container],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10).stdout
                        receipt["failed_upstream_log_sha256"] = digest(log.replace(key.encode(), b"<redacted-synthetic-key>"))
                        receipt["failed_upstream_log_bytes"] = len(log)
                    command(["docker", "rm", "--force", "--volumes", container])
                else:
                    raise RuntimeError("refusing cleanup of unowned container")
            check = subprocess.run(["docker", "inspect", container], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10)
            receipt["cleanup"]["container_removed"] = check.returncode != 0
        except (Exception, KeyboardInterrupt) as exc:
            receipt["cleanup_error"] = {"error_type": type(exc).__name__}
        receipt["temporary_networks_created"] = receipt["persistent_volumes_created"] = 0
        receipt["all_checks_passed"] = receipt["all_checks_passed"] and all(receipt["cleanup"].values())
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--work-root", type=Path, required=True, help="Existing approved disposable parent directory")
    parser.add_argument("--output", type=Path, required=True, help="New receipt path; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("receipt already exists")
    receipt = run(args.source_root.resolve(), args.work_root.resolve())
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"all_checks_passed": receipt["all_checks_passed"], "checks": receipt["checks"],
                      "error": receipt.get("error"), "cleanup": receipt["cleanup"]}))
    return 0 if receipt["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
