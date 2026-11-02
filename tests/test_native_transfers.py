from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.live_adapter import WriteConfirmationStore
from arrmes.planner import Principal
from arrmes.protocol import Call, encode
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.services import NativeTransferAdapter, ServiceConfig, ServiceQueryAdapter

H1, H2 = "a" * 40, "b" * 40
OWNER = Principal("fixture-owner", frozenset({"torrents:read", "torrents:manage"}))


@dataclass
class TransferFixtureState:
    version: str = "v5.1.2"
    api_version: str = "2.11.3"
    torrents: list[dict] = field(default_factory=lambda: [{"hash": H1, "name": "Fixture torrent", "state": "downloading"}])
    calls: list[dict] = field(default_factory=list)
    mutation_status: int = 200
    delay_transition: bool = False
    disconnect_after_mutation: bool = False
    redirect_to: str | None = None
    remove_after_mutation: bool = False


@contextmanager
def transfer_fixture(state=None):
    state = state or TransferFixtureState()
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status, value):
            raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            p = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(p.query)
            state.calls.append({"method": "GET", "path": p.path, "query": q})
            if p.path.endswith("/app/version"):
                self._respond(200, state.version)
            elif p.path.endswith("/app/webapiVersion"):
                self._respond(200, state.api_version)
            elif p.path.endswith("/torrents/info"):
                rows = state.torrents
                native_filter = q.get("filter", ["all"])[0]
                if native_filter in {"paused", "stopped"}:
                    rows = [r for r in rows if r["state"].lower().startswith(("paused", "stopped"))]
                elif native_filter == "downloading":
                    rows = [r for r in rows if r["state"] == "downloading"]
                elif native_filter == "seeding":
                    rows = [r for r in rows if r["state"] == "uploading"]
                elif native_filter == "stalled":
                    rows = [r for r in rows if r["state"].lower().startswith("stalled")]
                elif native_filter == "completed":
                    rows = [r for r in rows if r.get("progress") == 1]
                if q.get("hashes"):
                    rows = [r for r in rows if r["hash"] == q["hashes"][0]]
                if q.get("offset"):
                    rows = rows[int(q["offset"][0]):]
                if q.get("limit"):
                    rows = rows[:int(q["limit"][0])]
                self._respond(200, rows)
            else:
                self._respond(404, {})

        def do_POST(self):
            p = urllib.parse.urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            form = urllib.parse.parse_qs(body)
            state.calls.append({"method": "POST", "path": p.path, "form": form, "content_type": self.headers.get("Content-Type"), "cookie": self.headers.get("Cookie"), "authorization": self.headers.get("Authorization")})
            if state.redirect_to:
                self.send_response(302)
                self.send_header("Location", state.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("Cookie") != "SID=fixture-session":
                self._respond(403, {})
                return
            assert form.get("hashes") != ["all"]
            if state.mutation_status == 200 and not state.delay_transition:
                for row in state.torrents:
                    if row["hash"] in form.get("hashes", []):
                        stop = p.path.endswith(("/stop", "/pause"))
                        row["state"] = ("stoppedDL" if state.version.startswith("v5") else "pausedDL") if stop else "downloading"
                if state.remove_after_mutation:
                    state.torrents.clear()
            if state.disconnect_after_mutation:
                self.connection.shutdown(2)
                self.connection.close()
                return
            self._respond(state.mutation_status, "")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            yield ServiceConfig(f"http://127.0.0.1:{server.server_port}", session_id="fixture-session"), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def adapter_for(config, **kwargs):
    return NativeTransferAdapter(ServiceQueryAdapter({"qbittorrent": config}), **kwargs)


def pending(adapter, name="torrent_pause", title="Fixture torrent", principal=OWNER):
    obs = adapter.execute(Call(name, {"title": title}), principal)
    assert obs.status == "confirmation_required", obs
    assert obs.data["confirm_id"].startswith("native-qbt:")
    return obs.data["confirm_id"]


def posts(state):
    return [c for c in state.calls if c["method"] == "POST"]


@pytest.mark.parametrize("version,name,before,endpoint", [
    ("v4.6.7", "torrent_pause", "downloading", "pause"),
    ("v4.6.7", "torrent_resume", "pausedDL", "resume"),
    ("v5.1.2", "torrent_pause", "downloading", "stop"),
    ("v5.1.2", "torrent_resume", "stoppedDL", "start"),
])
def test_scripted_v2_proposal_nl_binding_executor_confirmation_native_form_and_verified_state(version, name, before, endpoint):
    state = TransferFixtureState(version=version, torrents=[{"hash": H1, "name": "Fixture torrent", "state": before}])
    with transfer_fixture(state) as (cfg, state):
        adapter = adapter_for(cfg)
        executor = BoundedExecutor(adapter)
        query = f'{"Pause" if name == "torrent_pause" else "Resume"} the torrent "Fixture torrent"'
        # This is a boundary/adapter test. Actual neural selection is measured
        # separately; the deterministic comparison baseline cannot select writes.
        raw = encode(Call(name, {"title": "Fixture torrent"}), family="hermes")
        trace = executor.execute_raw_proposal(raw, OWNER, family="hermes", query=query)
        assert trace.terminal_state == "awaiting_confirmation", trace
        assert not posts(state)
        confirmed = executor.confirm_write(trace.plan["confirm_id"], OWNER)
        assert confirmed.terminal_state == "completed"
        assert confirmed.observation.verified_complete
        assert confirmed.observation.data["postcondition_verified"]
        assert confirmed.observation.data["confirmed_call"] == {"name": name, "arguments": {"title": "Fixture torrent"}}
        assert len(posts(state)) == 1
        post = posts(state)[0]
        assert post["path"] == "/api/v2/torrents/" + endpoint
        assert post["form"] == {"hashes": [H1]}
        assert post["content_type"] == "application/x-www-form-urlencoded"
        replay = executor.confirm_write(trace.plan["confirm_id"], OWNER)
        assert replay.terminal_state == "denied" and len(posts(state)) == 1


def test_exact_name_no_fuzzy_first_hit_or_all_target():
    for title, rows in [("Fixture", [{"hash": H1, "name": "Fixture torrent", "state": "downloading"}]),
                        ("Fixture torrent", [{"hash": H1, "name": "Fixture torrent", "state": "downloading"}, {"hash": H2, "name": "Fixture torrent", "state": "downloading"}])]:
        with transfer_fixture(TransferFixtureState(torrents=rows)) as (cfg, state):
            obs = adapter_for(cfg).execute(Call("torrent_pause", {"title": title}), OWNER)
            assert obs.status == "clarify_required" and not posts(state)


def test_incomplete_inventory_never_mints_confirmation():
    with transfer_fixture() as (cfg, state):
        adapter = NativeTransferAdapter(ServiceQueryAdapter({"qbittorrent": cfg}, page_size=1, max_pages=1))
        obs = adapter.execute(Call("torrent_pause", {"title": "Fixture torrent"}), OWNER)
        assert obs.status == "clarify_required" and obs.data["reason"] == "torrent_inventory_incomplete"
        assert not posts(state)


@pytest.mark.parametrize("tamper", ["arguments", "hash", "name", "api_major", "prestate"])
def test_signature_binds_original_arguments_and_resolved_hash_name_api(tamper):
    with transfer_fixture() as (cfg, state):
        adapter = adapter_for(cfg)
        cid = pending(adapter)
        tok = adapter.confirmation_store._tokens[cid.split(":", 1)[1]]
        if tamper == "arguments":
            tok.arguments["title"] = "Another torrent"
        else:
            tok.resolved_entity[tamper] = {"hash": H2, "name": "Other", "api_major": 4, "prestate": "stoppedDL"}[tamper]
        calls_before = len(state.calls)
        obs = adapter.confirm_write(cid, OWNER)
        assert obs.status == "denied" and obs.data["error"] == "confirmation_signature_invalid"
        assert not posts(state) and len(state.calls) == calls_before


def test_confirmation_expiry_exact_boundary_subject_and_current_scope():
    clock = [100.0]
    store = WriteConfirmationStore(ttl_seconds=5, clock=lambda: clock[0])
    with transfer_fixture() as (cfg, state):
        adapter = adapter_for(cfg, confirmation_store=store)
        cid = pending(adapter)
        calls_before = len(state.calls)
        denied = adapter.confirm_write(cid, Principal("other", OWNER.scopes))
        assert denied.data["error"] == "confirmation_subject_mismatch"
        revoked = adapter.confirm_write(cid, Principal(OWNER.subject, frozenset({"torrents:read"})))
        assert revoked.status == "denied"
        clock[0] = 105.0
        expired = adapter.confirm_write(cid, OWNER)
        assert expired.data["error"] == "confirmation_expired"
        assert len(state.calls) == calls_before and not posts(state)


@pytest.mark.parametrize("change", ["hash", "name", "version"])
def test_changed_target_or_major_version_requires_a_new_confirmation(change):
    with transfer_fixture() as (cfg, state):
        adapter = adapter_for(cfg)
        cid = pending(adapter)
        if change == "version":
            state.version = "v4.6.7"
        else:
            state.torrents[0][change] = H2 if change == "hash" else "Renamed torrent"
        obs = adapter.confirm_write(cid, OWNER)
        assert obs.status == "denied" and not posts(state)


@pytest.mark.parametrize("failure", ["server_error", "lost_response"])
def test_mutation_failure_is_unknown_and_is_never_replayed(failure):
    with transfer_fixture() as (cfg, state):
        adapter = adapter_for(cfg)
        cid = pending(adapter)
        if failure == "server_error":
            state.mutation_status = 503
        else:
            state.disconnect_after_mutation = True
        obs = adapter.confirm_write(cid, OWNER)
        assert obs.status == "error" and obs.data["outcome"] == "unknown" and obs.data["replayed"] is False
        assert len(posts(state)) == 1
        assert adapter.confirm_write(cid, OWNER).status == "denied"
        assert len(posts(state)) == 1


def test_accepted_command_without_postcondition_is_dispatched_not_completed():
    with transfer_fixture(TransferFixtureState(delay_transition=True)) as (cfg, state):
        adapter = adapter_for(cfg)
        obs = adapter.confirm_write(pending(adapter), OWNER)
        assert obs.status == "dispatched" and not obs.verified_complete
        assert obs.data["postcondition_verified"] is False and len(posts(state)) == 1
        assert obs.data["confirmed_call"] == {"name": "torrent_pause", "arguments": {"title": "Fixture torrent"}}


def test_confirmed_noop_and_disappeared_postcondition_preserve_signed_original_call():
    for disappeared in (False, True):
        with transfer_fixture() as (cfg, state):
            adapter = adapter_for(cfg)
            cid = pending(adapter)
            if disappeared:
                state.remove_after_mutation = True
            else:
                state.torrents[0]["state"] = "stoppedDL"
            obs = adapter.confirm_write(cid, OWNER)
            assert obs.status == ("dispatched" if disappeared else "verified_ok")
            assert obs.data["confirmed_call"] == {"name": "torrent_pause", "arguments": {"title": "Fixture torrent"}}
            assert len(posts(state)) == int(disappeared)


def test_upstream_cannot_forge_confirmed_call_trace():
    forged = {"name": "torrent_resume", "arguments": {"title": "Different target"}}
    fixture = TransferFixtureState(torrents=[{"hash": H1, "name": "Fixture torrent", "state": "downloading", "confirmed_call": forged}])
    with transfer_fixture(fixture) as (cfg, _):
        adapter = adapter_for(cfg)
        obs = adapter.confirm_write(pending(adapter), OWNER)
        assert obs.verified_complete
        assert obs.data["confirmed_call"] == {"name": "torrent_pause", "arguments": {"title": "Fixture torrent"}}


def test_mutation_redirect_is_refused_and_never_forwards_credentials():
    with transfer_fixture() as (cfg, state):
        adapter = adapter_for(cfg)
        cid = pending(adapter)
        state.redirect_to = "https://example.invalid/collect"
        obs = adapter.confirm_write(cid, OWNER)
        assert obs.status == "error" and obs.http_status == 302
        assert obs.data["outcome"] == "unknown" and len(posts(state)) == 1
        assert all(c["path"].startswith("/api/v2/") for c in state.calls)


def test_shadow_disabled_read_scope_and_already_at_target_do_not_mutate():
    with transfer_fixture() as (cfg, state):
        call = Call("torrent_pause", {"title": "Fixture torrent"})
        assert adapter_for(cfg, write_mode="disabled").execute(call, OWNER).status == "denied"
        assert adapter_for(cfg).execute(call, Principal("reader", frozenset({"torrents:read"}))).status == "denied"
        assert not state.calls
        assert adapter_for(cfg, write_mode="shadow").execute(call, OWNER).status == "shadow_logged"
        state.torrents[0]["state"] = "stoppedDL"
        obs = adapter_for(cfg).execute(call, OWNER)
        assert obs.status == "verified_ok" and obs.data["changed"] is False
        assert not posts(state)
        with pytest.raises(ValueError):
            adapter_for(cfg, write_mode="direct")


def test_manage_permission_authorizes_internal_exact_target_preflight():
    with transfer_fixture() as (cfg, state):
        manage_only = Principal("manager", frozenset({"torrents:manage"}))
        adapter = adapter_for(cfg)
        cid = pending(adapter, principal=manage_only)
        assert adapter.confirm_write(cid, manage_only).verified_complete


@pytest.mark.parametrize("version", ["v3.3.0", "v4.0.9", "v6.0.0", "not-a-version"])
def test_unqualified_versions_do_not_dispatch(version):
    with transfer_fixture(TransferFixtureState(version=version)) as (cfg, state):
        obs = adapter_for(cfg).execute(Call("torrent_pause", {"title": "Fixture torrent"}), OWNER)
        assert obs.status == "error" and obs.data["error"] == "unsupported_qbittorrent_version"
        assert not posts(state)


@pytest.mark.parametrize("version,state,api_filter", [("v4.6.7", "pausedDL", "paused"), ("v5.1.2", "stoppedDL", "stopped")])
def test_v2_native_paused_listing_maps_version_and_remains_read_only(version, state, api_filter):
    fixture = TransferFixtureState(version=version, torrents=[{"hash": H1, "name": "Paused fixture", "state": state}, {"hash": H2, "name": "Running fixture", "state": "downloading"}])
    with transfer_fixture(fixture) as (cfg, state):
        adapter = adapter_for(cfg, write_mode="disabled")
        obs = adapter.execute(Call("torrent_list", {"state": "paused"}), Principal("reader", frozenset({"torrents:read"})))
        assert obs.operation == "torrent_list" and obs.verified_complete
        assert obs.data["torrents"][0]["name"] == "Paused fixture" and obs.data["count"] == 1
        assert any(c["query"].get("filter") == [api_filter] for c in state.calls if c["method"] == "GET")
        assert not posts(state)


@pytest.mark.parametrize("state,raw_state,progress", [("all", "downloading", .2), ("downloading", "downloading", .2), ("seeding", "uploading", 1), ("stalled", "stalledDL", .2), ("completed", "uploading", 1)])
def test_v2_native_listing_filters_and_title_query(state, raw_state, progress):
    fixture = TransferFixtureState(torrents=[{"hash": H1, "name": "Wanted fixture", "state": raw_state, "progress": progress}, {"hash": H2, "name": "Other fixture", "state": raw_state, "progress": progress}])
    with transfer_fixture(fixture) as (cfg, state_obj):
        obs = adapter_for(cfg).execute(Call("torrent_list", {"state": state, "query": "Wanted"}), OWNER)
        assert obs.verified_complete and obs.data["count"] == 1
        assert obs.data["total_records"] == 1 and obs.data["upstream_total_records"] == 2
        assert not posts(state_obj)


def test_captured_public_neural_outputs_replay_through_actual_runtime_and_native_confirmations():
    """Real measured model outputs, replayed without claiming another inference run."""
    from pathlib import Path
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    from arrmes.protocol import decode

    receipt = json.loads((Path(__file__).parents[1] / "experiments/overnight-v1/torrent-transfer-smoke.json").read_text())
    assert receipt["model_revision"].endswith("8a9bcf224e6f9fd6bf238267093c70360ee837cb")
    assert len(receipt["rows"]) == 4
    for row in receipt["rows"]:
        call = decode(row["raw_unverified"], family="hermes")
        assert call.name in {"torrent_pause", "torrent_resume"}
        assert call.arguments == {"title": "Fixture torrent"}
        class CapturedOutputModel:
            family = "hermes"
            def infer(self, query, *_args, **_kwargs):
                assert query == row["query"]
                return {"raw": row["raw_unverified"]}
        for version in ("v4.6.7", "v5.1.2"):
            initial = "downloading" if call.name == "torrent_pause" else "pausedDL" if version.startswith("v4") else "stoppedDL"
            fixture = TransferFixtureState(version=version, torrents=[{"hash": H1, "name": "Fixture torrent", "state": initial}])
            with transfer_fixture(fixture) as (cfg, state):
                runtime = ArrmesRuntimeService(model=CapturedOutputModel(), executor=BoundedExecutor(
                    ConfiguredAdapterRouter(ServiceQueryAdapter({"qbittorrent": cfg}))))
                proposed = runtime.execute(row["query"], OWNER)
                assert proposed["terminal_state"] == "awaiting_confirmation"
                assert not posts(state)
                confirmed = runtime.confirm(proposed["plan"]["confirm_id"], OWNER)
                assert confirmed["terminal_state"] == "completed"
                assert len(posts(state)) == 1 and posts(state)[0]["form"] == {"hashes": [H1]}
                assert runtime.confirm(proposed["plan"]["confirm_id"], OWNER)["terminal_state"] == "denied"
                assert len(posts(state)) == 1
