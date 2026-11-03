"""Tests for LiveGatewayAdapter v3 service_query, armiger-ultra, waypoint, and Qui write confirmation."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

from arrmes.executor import BoundedExecutor
from arrmes.live_adapter import LiveGatewayAdapter
from arrmes.neural_router import ArrmesNeuralRouterV3
from arrmes.planner import Principal
from arrmes.protocol import Call
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.server import ArrmesRuntimeService

FULL_PRINCIPAL = Principal(
    "user:operator",
    frozenset({
        "media:read", "media:manage", "activity:read",
        "torrents:read", "torrents:manage", "requests:read",
        "requests:create", "releases:read", "releases:classify", "pipeline:read",
    }),
)
H40 = "b" * 40


@contextmanager
def gateway_v3_fixture():
    calls = []
    state = {"torrent_state": "downloading"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            p = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(p.query)
            calls.append({"method": "GET", "path": p.path, "query": q})
            if p.path.endswith("/system/status"):
                self._send(200, {"version": "4.0.20", "appName": "Sonarr"})
                return
            if p.path.endswith("/health"):
                self._send(200, [])
                return
            if p.path == "/sonarr/api/v3/queue":
                self._send(200, {"records": [{"id": 1, "title": "Severance S01E01", "status": "downloading"}], "totalRecords": 1})
                return
            if p.path == "/radarr/api/v3/movie":
                self._send(200, [{"id": 10, "title": "Dune", "year": 2021, "hasFile": True, "monitored": True}])
                return
            if p.path == "/tautulli/api/v2":
                cmd = q.get("cmd", [""])[0]
                if cmd == "get_server_info":
                    self._send(200, {"response": {"result": "success", "data": {"pms_version": "1.43.1", "pms_name": "MediaServer"}}})
                    return
                if cmd == "get_activity":
                    self._send(200, {"response": {"result": "success", "data": {"stream_count": "0", "sessions": []}}})
                    return
            if p.path == "/commskip/api/v1/batches":
                self._send(200, {"batches": [{"batch_id": "b-1", "status": "completed"}]})
                return
            if p.path == "/qui/api/instances/1/torrents":
                self._send(200, {
                    "total": 1,
                    "torrents": [{
                        "hash": H40,
                        "name": "Ubuntu.24.04.ISO",
                        "state": state["torrent_state"],
                        "progress": 0.5,
                        "dlspeed": 1024,
                        "upspeed": 512,
                        "size": 1000000,
                        "category": "linux",
                    }],
                })
                return
            if p.path == "/request/api/discover/trending":
                self._send(200, {"results": [{"id": 99, "title": "Dune", "type": "movie", "year": "2021"}]})
                return
            if p.path == "/request/api/search":
                self._send(200, {"results": [{"id": 99, "title": "Dune", "type": "movie", "year": "2021", "status": "available"}]})
                return
            if p.path == "/image-gen/v1/infer/job-123/wait":
                self._send(200, {
                    "job_id": "job-123",
                    "status": "completed",
                    "result": {
                        "model": "armiger-ultra",
                        "version": "3.11.0",
                        "artifact_digest": "sha256:fixture",
                        "predictions": [{"category": {"label": "tv", "prob": 0.99}, "junk": {"label": 0, "prob": 0.99}}],
                    },
                })
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self):
            p = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            calls.append({"method": "POST", "path": p.path, "body": body})
            if p.path == "/image-gen/v1/infer":
                self._send(202, {"job_id": "job-123", "status": "queued"})
                return
            if p.path == "/bitagent/graphql":
                self._send(200, {"data": {"version": "v5.8.7", "health": {"status": "up", "checks": [{"key": "dht", "status": "up"}]}}})
                return
            if p.path == "/qui/api/instances/1/torrents/bulk-action":
                state["torrent_state"] = "pausedDL" if body.get("action") == "pause" else "downloading"
                self._send(200, {"status": "ok"})
                return
            self._send(404, {"error": "not_found"})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_v3_service_queries_and_multi_step_workflow():
    with gateway_v3_fixture() as (url, _calls):
        adapter = LiveGatewayAdapter(url, gateway_api_key="fixture-ges-key", enable_gateway_v3=True)
        assert adapter.supports_service_queries is True
        service = ArrmesRuntimeService(executor=BoundedExecutor(adapter), write_mode="confirm")
        assert service.health()["contract_version"] == "arrmes.tools.v3"

        res_health = service.execute("Is Sonarr healthy?", FULL_PRINCIPAL)
        assert res_health["terminal_state"] == "completed"
        assert res_health["observation"]["data"]["healthy"] is True

        res_wf = service.execute("Show Sonarr queue and Tautulli activity", FULL_PRINCIPAL)
        assert res_wf["terminal_state"] == "completed"
        assert len(res_wf["workflow"]) == 2

        res_lib = service.execute('Show "Dune" in Radarr library', FULL_PRINCIPAL)
        assert res_lib["terminal_state"] == "completed"
        assert res_lib["observation"]["data"]["returned_count"] == 1

        res_bm = service.execute("Is Bitmagnet healthy?", FULL_PRINCIPAL)
        assert res_bm["terminal_state"] == "completed"
        assert res_bm["observation"]["data"]["healthy"] is True


class _TorrentPauseStubModel:
    family = "hermes"
    backend = "neural-router"

    def infer(self, query: str, context=None, max_tokens: int = 64):
        from arrmes.protocol import encode
        return {"raw": encode(Call("torrent_pause", {"title": "Ubuntu.24.04.ISO"}), "hermes")}


def test_unquoted_sonarr_missing_entity_extraction_and_routing():
    from arrmes.neural_router import _extract_unquoted_entity
    from arrmes.services import route_service_query

    q = "What episodes of Severance are missing in Sonarr?"
    assert _extract_unquoted_entity(q) == "Severance"
    assert route_service_query(q) is None


def test_gateway_v3_armiger_waypoint_and_confirmed_torrent_mutations():
    with gateway_v3_fixture() as (url, _calls):
        adapter = LiveGatewayAdapter(url, gateway_api_key="fixture-ges-key", enable_gateway_v3=True, write_mode="confirm")
        service = ArrmesRuntimeService(model=_TorrentPauseStubModel(), executor=BoundedExecutor(adapter), write_mode="confirm")

        obs_arm = adapter.execute(Call("armiger_parse", {"release_name": "Severance.S01E01.2160p.WEB-DL.DDP5.1.H.265-NTb.mkv"}), FULL_PRINCIPAL)
        assert obs_arm.status == "verified_ok"
        assert obs_arm.data["model"] == "armiger-ultra"
        assert obs_arm.data["classification"]["category"]["label"] == "tv"

        obs_wp = adapter.execute(Call("waypoint_status", {}), FULL_PRINCIPAL)
        assert obs_wp.status == "verified_ok"
        assert obs_wp.data["recent_batch_count"] == 1

        pause_preview = service.execute('Pause torrent "Ubuntu.24.04.ISO" in qBittorrent', FULL_PRINCIPAL)
        assert pause_preview["terminal_state"] == "awaiting_confirmation"
        cid = pause_preview["observation"]["data"]["confirm_id"]

        pause_done = service.confirm(cid, FULL_PRINCIPAL)
        assert pause_done["terminal_state"] == "completed"
        assert pause_done["observation"]["data"]["verified_state"] == "paused"
