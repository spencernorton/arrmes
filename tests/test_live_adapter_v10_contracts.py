"""Contract tests for modernized Armiger v3.13+ and DH-Waypoint v1 live_adapter paths."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

from arrmes.live_adapter import LiveGatewayAdapter
from arrmes.planner import Principal
from arrmes.protocol import Call
from arrmes.sandbox import NetworkIsolationGuard

OPERATOR = Principal(
    "user:operator",
    frozenset({"releases:classify", "pipeline:read", "media:read"}),
)


@contextmanager
def modern_services_fixture():
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            calls.append({"method": "GET", "path": parsed.path, "query": qs})
            if parsed.path == "/v1/status":
                self._send(200, {
                    "schema_version": "waypoint.status.v1",
                    "status": "online",
                    "version": "2.0.0",
                    "pipelines": [{"id": "ingest", "state": "idle"}],
                    "batches": [{"batch_id": "wp-101", "status": "completed"}],
                })
                return
            if parsed.path == "/v1/manifest":
                self._send(200, {"schema_version": "waypoint.manifest.v1", "version": "2.0.0", "capabilities": ["status"]})
                return
            if parsed.path == "/waypoint/v1/status":
                self._send(200, {
                    "schema_version": "waypoint.status.v1",
                    "status": "online",
                    "pipelines": [{"id": "gateway-pipe", "state": "healthy"}],
                    "batches": [{"batch_id": "wp-202", "status": "completed"}],
                })
                return
            if parsed.path == "/image-gen/v1/infer/job-v10/wait":
                assert qs.get("timeout_s") == ["10"], f"Expected timeout_s=10, got {qs}"
                self._send(200, {
                    "job_id": "job-v10",
                    "status": "completed",
                    "result": {
                        "schema_version": "armiger.v3.13",
                        "model": "armiger-ultra",
                        "version": "3.13.1",
                        "artifact_digest": "sha256:v3131",
                        "predictions": [{"category": {"label": "movie", "prob": 0.994}, "junk": {"label": 0, "prob": 0.998}}],
                    },
                })
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            calls.append({"method": "POST", "path": parsed.path, "body": body})
            if parsed.path == "/v1/classify":
                self._send(200, {
                    "schema_version": "armiger.classify.v1",
                    "model": "armiger-ultra",
                    "version": "3.13.1",
                    "artifact_digest": "sha256:direct3131",
                    "classification": {"category": {"label": "tv", "prob": 0.997}, "junk": {"label": 0, "prob": 0.999}},
                })
                return
            if parsed.path == "/v1/batch":
                items = body.get("items", [])
                self._send(200, {
                    "schema_version": "armiger.batch.v1",
                    "model": "armiger-ultra",
                    "version": "3.13.1",
                    "results": [{"name": it["name"], "category": {"label": "movie", "prob": 0.99}} for it in items],
                })
                return
            if parsed.path == "/image-gen/v1/infer":
                self._send(202, {"job_id": "job-v10", "status": "queued"})
                return
            self._send(404, {"error": "not_found"})

        def log_message(self, *_args) -> None:
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


def test_direct_armiger_v313_classify_batch_and_waypoint_v1_status() -> None:
    with modern_services_fixture() as (base_url, calls):
        adapter = LiveGatewayAdapter(
            "https://gateway.example.invalid",
            gateway_api_key="",
            armiger_url=base_url,
            waypoint_url=base_url,
            enable_gateway_v3=False,
        )
        obs_cls = adapter.execute(
            Call("armiger_parse", {"release_name": "Cobalt.Meridian.S01E02.2160p.WEB-DL.DDP5.1.H.265-GRP.mkv"}),
            OPERATOR,
        )
        assert obs_cls.status == "verified_ok"
        assert obs_cls.data["transport"] == "armiger_direct_v1"
        assert obs_cls.data["version"] == "3.13.1"
        assert obs_cls.data["classification"]["category"]["label"] == "tv"

        obs_batch = adapter.execute_armiger_batch(
            ["Brass.Pendulum.2021.1080p.BluRay.x264-GRP.mkv", "Velvet.Astrolabe.2022.2160p.WEB-DL-GRP.mkv"],
            OPERATOR,
        )
        assert obs_batch.status == "verified_ok"
        assert obs_batch.data["count"] == 2

        obs_wp = adapter.execute(Call("waypoint_status", {}), OPERATOR)
        assert obs_wp.status == "verified_ok"
        assert obs_wp.data["transport"] == "waypoint_direct_v1"
        assert obs_wp.data["version"] == "2.0.0"
        assert obs_wp.data["manifest"]["schema_version"] == "waypoint.manifest.v1"
        assert any(c["path"] == "/v1/classify" for c in calls)
        assert any(c["path"] == "/v1/batch" for c in calls)
        assert any(c["path"] == "/v1/status" for c in calls)


def test_gateway_armiger_wait_uses_timeout_s_and_waypoint_v1_route() -> None:
    with modern_services_fixture() as (gw_url, calls):
        tok = "fixture"
        adapter = LiveGatewayAdapter(gw_url, gateway_api_key=tok, enable_gateway_v3=True)
        obs_arm = adapter.execute(
            Call("armiger_parse", {"release_name": "Brass.Pendulum.2021.1080p.BluRay.x264-GRP.mkv"}),
            OPERATOR,
        )
        assert obs_arm.status == "verified_ok"
        assert obs_arm.data["transport"] == "gateway_infer_v1"
        assert obs_arm.data["version"] == "3.13.1"
        wait_calls = [c for c in calls if c["path"] == "/image-gen/v1/infer/job-v10/wait"]
        assert len(wait_calls) == 1
        assert wait_calls[0]["query"] == {"timeout_s": ["10"]}

        obs_wp = adapter.execute(Call("waypoint_status", {}), OPERATOR)
        assert obs_wp.status == "verified_ok"
        assert obs_wp.data["transport"] == "gateway_waypoint_v1"
        assert obs_wp.data["pipelines"][0]["id"] == "gateway-pipe"


def test_empty_and_mismatched_200_payloads_fail_closed() -> None:
    """Ensure HTTP 200 empty objects or mismatched batch lengths fail closed instead of synthesizing verified_ok."""
    class MalformedHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/v1/status":
                self._send(200, {})
                return
            if parsed.path == "/waypoint/v1/status":
                self._send(200, {"status": "online", "pipelines": [], "batches": []})
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if parsed.path == "/v1/classify":
                self._send(200, {})
                return
            if parsed.path == "/v1/batch":
                self._send(200, {"results": [{"name": "only-one"}]})
                return
            self._send(404, {"error": "not_found"})

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), MalformedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            base = f"http://127.0.0.1:{server.server_port}"
            direct = LiveGatewayAdapter(
                "https://gateway.example.invalid",
                gateway_api_key="",
                armiger_url=base,
                waypoint_url=base,
                enable_gateway_v3=False,
            )
            obs_cls = direct.execute(Call("armiger_parse", {"release_name": "Test.2024.1080p.mkv"}), OPERATOR)
            assert obs_cls.status == "error"
            assert obs_cls.data["error"] == "invalid_armiger_classify_response"

            obs_batch = direct.execute_armiger_batch(["One.mkv", "Two.mkv"], OPERATOR)
            assert obs_batch.status == "error"
            assert obs_batch.data["error"] == "invalid_armiger_batch_cardinality"

            obs_wp_empty = direct.execute(Call("waypoint_status", {}), OPERATOR)
            assert obs_wp_empty.status == "error"
            assert obs_wp_empty.data["error"] == "invalid_waypoint_status_response"

            # Direct Waypoint with valid /v1/status and 404 /v1/manifest marks manifest_status='unavailable'
            tok = "fixture"
            gw = LiveGatewayAdapter(base, gateway_api_key=tok, waypoint_url=f"{base}/waypoint", enable_gateway_v3=True)
            obs_wp_ok = gw.execute(Call("waypoint_status", {}), OPERATOR)
            assert obs_wp_ok.status == "verified_ok"
            assert obs_wp_ok.data["manifest_status"] == "unavailable"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)



def test_schema_invalid_classification_batch_and_gateway_waypoint_fail_closed() -> None:
    """Verify Codex P1-2 cases: null category, arbitrary batch dicts, gateway 200 {}, and commskip 200 {} all fail closed."""
    mode = {"commskip_valid": False, "wp_status_404": False}

    class StrictSchemaHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/waypoint/v1/status":
                if mode["wp_status_404"]:
                    self._send(404, {"error": "not_found"})
                else:
                    self._send(200, {})
                return
            if parsed.path == "/commskip/api/v1/batches":
                if mode["commskip_valid"]:
                    self._send(200, {"batches": [{"id": "b-1", "status": "done"}]})
                else:
                    self._send(200, {})
                return
            if parsed.path == "/image-gen/v1/infer/job-empty/wait":
                self._send(200, {"job_id": "job-empty", "status": "completed", "result": {"predictions": [{"category": None}]}})
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if parsed.path == "/v1/classify":
                self._send(200, {"category": None})
                return
            if parsed.path == "/v1/batch":
                self._send(200, {"results": [{"foo": 1}, {"bar": 2}]})
                return
            if parsed.path == "/image-gen/v1/infer":
                self._send(202, {"job_id": "job-empty", "status": "queued"})
                return
            self._send(404, {"error": "not_found"})

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), StrictSchemaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            base = f"http://127.0.0.1:{server.server_port}"
            direct = LiveGatewayAdapter(
                "https://gateway.example.invalid",
                gateway_api_key="",
                armiger_url=base,
                waypoint_url="",
                enable_gateway_v3=False,
            )
            # Direct /v1/classify with {"category": null} fails closed
            obs_null_cat = direct.execute(Call("armiger_parse", {"release_name": "Sample.2024.1080p.mkv"}), OPERATOR)
            assert obs_null_cat.status == "error"
            assert obs_null_cat.data["error"] == "invalid_armiger_classify_response"

            # Direct /v1/batch with arbitrary dicts of matching cardinality fails closed
            obs_bad_batch = direct.execute_armiger_batch(["One.mkv", "Two.mkv"], OPERATOR)
            assert obs_bad_batch.status == "error"
            assert obs_bad_batch.data["error"] == "invalid_armiger_batch_response"

            tok = "fixture"
            gw = LiveGatewayAdapter(base, gateway_api_key=tok, enable_gateway_v3=True)

            # Gateway /image-gen/v1/infer/.../wait with {"category": null} fails closed
            obs_gw_arm = gw.execute(Call("armiger_parse", {"release_name": "Sample.2024.1080p.mkv"}), OPERATOR)
            assert obs_gw_arm.status == "error"
            assert obs_gw_arm.data["error"] == "invalid_armiger_classify_response"

            # Gateway /waypoint/v1/status returning HTTP 200 {} fails closed WITHOUT synthesizing online or falling back
            obs_gw_wp_empty = gw.execute(Call("waypoint_status", {}), OPERATOR)
            assert obs_gw_wp_empty.status == "error"
            assert obs_gw_wp_empty.data["error"] == "invalid_waypoint_status_response"

            # Legacy /commskip fallback on 404 rejects HTTP 200 {} missing batches list, then accepts valid list
            mode["wp_status_404"] = True
            obs_cs_empty = gw.execute(Call("waypoint_status", {}), OPERATOR)
            assert obs_cs_empty.status == "error"
            assert obs_cs_empty.data["error"] == "invalid_waypoint_batches_response"

            mode["commskip_valid"] = True
            obs_cs_ok = gw.execute(Call("waypoint_status", {}), OPERATOR)
            assert obs_cs_ok.status == "verified_ok"
            assert obs_cs_ok.data["transport"] == "gateway_commskip_compat"
            assert obs_cs_ok.data["recent_batch_count"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_round3_codex_malformed_armiger_and_waypoint_payloads_fail_closed() -> None:
    """Verify Round-3 Codex findings: unrelated label/parsed substitutes, out-of-bounds/null probs, and malformed Waypoint lists fail closed."""
    state = {
        "classify_payload": {"category": None, "label": "garbage"},
        "infer_pred": {"category": {"label": "movie", "prob": -5}},
        "wp_payload": {"status": "online", "pipelines": [None, 123], "batches": [False]},
        "wp_code": 200,
        "commskip_payload": {"batches": [False]},
    }

    class Round3Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path in ("/v1/status", "/waypoint/v1/status"):
                self._send(state["wp_code"], state["wp_payload"])
                return
            if parsed.path == "/commskip/api/v1/batches":
                self._send(200, state["commskip_payload"])
                return
            if parsed.path == "/image-gen/v1/infer/job-r3/wait":
                self._send(200, {"job_id": "job-r3", "status": "completed", "result": {"predictions": [state["infer_pred"]]}})
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if parsed.path == "/v1/classify":
                self._send(200, state["classify_payload"])
                return
            if parsed.path == "/v1/batch":
                self._send(200, {"results": [state["classify_payload"]]})
                return
            if parsed.path == "/image-gen/v1/infer":
                self._send(202, {"job_id": "job-r3", "status": "queued"})
                return
            self._send(404, {"error": "not_found"})

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Round3Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            base = f"http://127.0.0.1:{server.server_port}"
            direct = LiveGatewayAdapter(
                "https://gateway.example.invalid",
                gateway_api_key="",
                armiger_url=base,
                waypoint_url=base,
                enable_gateway_v3=False,
            )
            gw = LiveGatewayAdapter(base, gateway_api_key="fixture", enable_gateway_v3=True)

            bad_armiger_payloads = [
                {"category": None, "label": "garbage"},
                {"category": {"label": "movie", "prob": -5}},
                {"category": {"label": "movie", "prob": 1.25}},
                {"category": {"label": "movie", "prob": None}},
                {"parsed": {"title": "anything"}},
                {"category": {"label": "movie", "prob": 0.9}, "junk": {"label": 9, "prob": 0.5}},
                {"category": {"label": "movie", "prob": 0.9}, "version": 12345},
                {"category": {"label": "movie", "prob": 10**400}},
                {"category": {"label": "movie", "prob": -(10**400)}},
            ]
            for bad_p in bad_armiger_payloads:
                state["classify_payload"] = bad_p
                state["infer_pred"] = bad_p
                obs_d = direct.execute(Call("armiger_parse", {"release_name": "Sample.2024.1080p.mkv"}), OPERATOR)
                assert obs_d.status == "error", f"Direct accepted malformed payload {bad_p!r}"
                assert obs_d.data["error"] == "invalid_armiger_classify_response"

                obs_b = direct.execute_armiger_batch(["Sample.2024.1080p.mkv"], OPERATOR)
                assert obs_b.status == "error", f"Batch accepted malformed payload {bad_p!r}"
                assert obs_b.data["error"] == "invalid_armiger_batch_response"

                obs_g = gw.execute(Call("armiger_parse", {"release_name": "Sample.2024.1080p.mkv"}), OPERATOR)
                assert obs_g.status == "error", f"Gateway accepted malformed payload {bad_p!r}"
                assert obs_g.data["error"] == "invalid_armiger_classify_response"

            bad_waypoint_payloads = [
                {"status": "online", "pipelines": [None, 123], "batches": [False]},
                {"status": "online", "pipelines": [{"id": "ingest", "state": None}], "batches": []},
                {"status": "online", "pipelines": [], "batches": [{"batch_id": "b1", "status": ""}]},
                {"status": "error", "pipelines": [], "batches": []},
                {"status": "garbage_state", "pipelines": [], "batches": []},
                {"status": "online", "version": 42, "pipelines": [], "batches": []},
                {"status": "online", "pipelines": [{"id": "ingest"}], "batches": [{"batch_id": "b1"}]},
                {"status": "online", "pipelines": [{"id": "ingest"}], "batches": []},
                {"status": "online", "pipelines": [], "batches": [{"batch_id": "b1"}]},
                {"status": "online", "state": "error", "pipelines": [], "batches": []},
                {"status": "online", "pipelines": [{"id": "ingest", "name": 123, "state": "idle"}], "batches": []},
                {"status": "online", "pipelines": [], "batches": [{"batch_id": "b1", "id": -1, "status": "done"}]},
            ]
            for bad_wp in bad_waypoint_payloads:
                state["wp_payload"] = bad_wp
                obs_wpd = direct.execute(Call("waypoint_status", {}), OPERATOR)
                assert obs_wpd.status == "error", f"Direct Waypoint accepted malformed payload {bad_wp!r}"
                assert obs_wpd.data["error"] == "invalid_waypoint_status_response"

                obs_wpg = gw.execute(Call("waypoint_status", {}), OPERATOR)
                assert obs_wpg.status == "error", f"Gateway Waypoint accepted malformed payload {bad_wp!r}"
                assert obs_wpg.data["error"] == "invalid_waypoint_status_response"

            # Commskip fallback also rejects non-dict and identifier-only batch items missing status/state
            state["wp_code"] = 404
            for bad_cs in ({"batches": [False]}, {"batches": [{"batch_id": "b1"}]}, {"batches": [{"id": "b1", "batch_id": None, "status": "done"}]}):
                state["commskip_payload"] = bad_cs
                obs_cs_bad = gw.execute(Call("waypoint_status", {}), OPERATOR)
                assert obs_cs_bad.status == "error", f"Commskip accepted malformed payload {bad_cs!r}"
                assert obs_cs_bad.data["error"] == "invalid_waypoint_batches_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
