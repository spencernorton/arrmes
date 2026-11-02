"""Boundaries of the optional actual-binary harness; these fakes are not qualification."""
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smoke_seerr_upstream", ROOT / "scripts/smoke_seerr_upstream.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.mark.parametrize("path", [
    "/api/v1/search?query=anything", "/api/v1/movie/10", "/api/v1/request/10",
    "/api/v1/auth/me?extra=1", "https://example.invalid/api/v1/status",
    "//example.invalid/api/v1/status", "/api/v1/status#anything",
    "/api/v1/status\r\nX-Test: bad", "/api/v1/request",
    "/api/v1/request?requestedBy=2&take=2&skip=0&filter=all&sort=added&requestedBy=1",
    "/api/v1/request?requestedBy=2&take=200&skip=0&filter=all&sort=added",
    "/api/v1/request?requestedBy=2&take=2&skip=1&filter=all&sort=added",
])
def test_nonallowlisted_or_unbounded_reads_never_invoke_docker(monkeypatch, path):
    def forbidden(*args, **kwargs):
        raise AssertionError("transport invoked")
    monkeypatch.setattr(smoke, "command", forbidden)
    transport = smoke.DockerReadTransport("arrmes-seerr-qualification-" + "a" * 12, "synthetic-secret", [])
    with pytest.raises(ValueError):
        transport.get(path, "synthetic-secret", 2)


@pytest.mark.parametrize("upstream_status", [200, 401, 403])
def test_transport_keeps_credentials_off_argv_and_forwards_unchanged_json(monkeypatch, upstream_status):
    calls = []
    body = b'{"id":2,"createdAt":"2026-01-01T00:00:00.000Z"}'
    def fake(parts, **kwargs):
        calls.append((parts, kwargs))
        return json.dumps({"status": upstream_status, "body": base64.b64encode(body).decode()}).encode()
    monkeypatch.setattr(smoke, "command", fake)
    audit = []
    transport = smoke.DockerReadTransport("arrmes-seerr-qualification-" + "a" * 12, "synthetic-secret", audit)
    assert transport.get("/api/v1/auth/me", "synthetic-secret", 2) == (body, upstream_status)
    assert "synthetic-secret" not in str(calls[0][0])
    assert json.loads(calls[0][1]["input_data"])["user_id"] == "2"
    assert audit[0]["body_sha256"] == smoke.digest(body)
    assert "synthetic-secret" not in json.dumps(audit)
    assert "createdAt" not in json.dumps(audit)
    assert "method:'GET'" in smoke.READ_JS and "host:'127.0.0.1'" in smoke.READ_JS


def test_proxy_rejects_writes_before_any_upstream_call():
    class Forbidden:
        calls = 0
        def get(self, *args):
            self.calls += 1
            raise AssertionError("upstream should not run")
    transport = Forbidden()
    cleanup = {}
    with smoke.read_proxy(transport, cleanup) as proxy:
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            request = urllib.request.Request("http://127.0.0.1:" + str(proxy.server_port) + "/api/v1/request",
                                             data=b"{}", method=method)
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            assert error.value.code == 405
    assert transport.calls == 0 and cleanup["proxy_stopped"] is True


def test_proxy_cleanup_runs_when_probe_fails():
    cleanup = {}
    with pytest.raises(RuntimeError, match="controlled probe failure"):
        with smoke.read_proxy(None, cleanup):
            raise RuntimeError("controlled probe failure")
    assert cleanup["proxy_stopped"] is True


def test_failed_preflight_retains_safe_receipt_without_raw_exception_text(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.platform, "system", lambda: "Linux")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(smoke, "resource_sample", lambda: (_ for _ in ()).throw(
        RuntimeError("PRIVATE-PATH-AND-HEADER-DO-NOT-EXPOSE")))
    calls = []
    def inspected(parts, **kwargs):
        calls.append(parts)
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
    monkeypatch.setattr(smoke.subprocess, "run", inspected)
    receipt = smoke.run(ROOT, tmp_path)
    assert receipt["all_checks_passed"] is False
    assert receipt["error"] == {"stage": "preflight", "error_type": "RuntimeError"}
    assert receipt["cleanup"] == {"container_removed": True, "proxy_stopped": True, "temporary_directory_removed": True}
    assert "PRIVATE-PATH-AND-HEADER" not in json.dumps(receipt)
    assert not any("rm" in parts for parts in calls)


def test_cleanup_never_removes_a_container_with_another_ownership_label(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.platform, "system", lambda: "Linux")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(smoke, "resource_sample", lambda: (_ for _ in ()).throw(RuntimeError("controlled failure")))
    calls = []
    def foreign(parts, **kwargs):
        calls.append(parts)
        payload = [{"Config": {"Labels": {"arrmes.qualification": "another-owner"}}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")
    monkeypatch.setattr(smoke.subprocess, "run", foreign)
    receipt = smoke.run(ROOT, tmp_path)
    assert receipt["all_checks_passed"] is False and receipt["cleanup"]["container_removed"] is False
    assert receipt["cleanup_error"]["error_type"] == "RuntimeError"
    assert len(calls) == 1 and calls[0][:2] == ["docker", "inspect"]


def test_output_path_cannot_overwrite_prior_failure_receipt(tmp_path):
    output = tmp_path / "receipt.json"
    output.write_text('{"failed":"original"}')
    with pytest.raises(SystemExit):
        smoke.main(["--work-root", str(tmp_path), "--output", str(output)])
    assert output.read_text() == '{"failed":"original"}'


@pytest.mark.parametrize("identity_style", ["legacy_config", "containerd_manifest"])
def test_exact_image_identity_supports_both_docker_stores_without_accepting_another_digest(identity_style):
    image = {"Id": "sha256:" + smoke.CONFIG_SHA256 if identity_style == "legacy_config" else smoke.IMAGE.rsplit("@", 1)[1],
             "RepoDigests": [smoke.IMAGE], "Architecture": "amd64", "Os": "linux", "Config": {"Labels": {
                 "org.opencontainers.image.revision": smoke.UPSTREAM_COMMIT,
                 "org.opencontainers.image.source": "https://github.com/seerr-team/seerr",
                 "org.opencontainers.image.version": "v3.5.1"}}}
    smoke.validate_image_identity(image)
    image["RepoDigests"] = ["ghcr.io/seerr-team/seerr@sha256:" + "0" * 64]
    with pytest.raises(RuntimeError, match="identity mismatch"):
        smoke.validate_image_identity(image)
