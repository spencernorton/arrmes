"""Native Bitmagnet HTTP contracts, separate from the legacy BitAgent facade."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from arrmes.catalog import contract_hash_v2
from arrmes.executor import BoundedExecutor
from arrmes.explicit import NativeOnlyModel
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, encode, normalized
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.server import ArrmesRuntimeService
from arrmes.service_router import ConfiguredAdapterRouter
from arrmes.services import ServiceConfig, ServiceQueryAdapter, load_service_configs, route_service_query
from arrmes.services.bitmagnet import NativeBitmagnetAdapter, _post_read

READER = Principal("fixture-reader", frozenset({"releases:read"}))


def _row(n):
    info_hash = f"{n:040x}"
    return {"id": "content-" + str(n), "infoHash": info_hash, "title": "Fixture title " + str(n),
            "contentType": "movie", "videoResolution": None, "seeders": 3, "leechers": None,
            "torrent": {"infoHash": info_hash, "name": "Fixture.release." + str(n),
                        "size": 4096, "filesCount": 1, "files": [{"path": "/private/fixture/file"}]},
            "source": {"api_key": "DO-NOT-EXPOSE", "url": "https://private.invalid"}}


@dataclass
class BitmagnetFixture:
    rows: list[dict] = field(default_factory=lambda: [_row(1), _row(2)])
    calls: list[dict] = field(default_factory=list)
    version: str = "v0.10.0"
    health_status: str = "up"
    estimate: int | None = None
    repeat_page: bool = False
    response_hook: object = None
    http_status: int = 200
    location: str | None = None
    raw_response: bytes | None = None
    slow_body: bool = False


@contextmanager
def bitmagnet_fixture(state=None, *, prefix="", api_key="proxy-fixture-key", **native_options):
    state = state or BitmagnetFixture()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state.calls.append({"method": "POST", "path": self.path, "payload": payload,
                                "headers": dict(self.headers)})
            if self.path != prefix + "/graphql" or (api_key and self.headers.get("X-Api-Key") != api_key):
                self.send_error(401)
                return
            assert payload["query"].lstrip().startswith("query ")
            if payload["operationName"] == "ArrmesBitmagnetHealth":
                data = {"version": state.version, "health": {"status": state.health_status,
                    "checks": [{"key": "database", "status": "up", "error": "DO-NOT-EXPOSE"}]},
                    "workers": {"listAll": {"workers": [{"key": "http_server", "started": True}]}}}
            elif payload["operationName"] == "ArrmesBitmagnetSearch":
                query = payload["variables"]["input"]
                offset = 0 if state.repeat_page else query["offset"]
                limit = query["limit"]
                items = deepcopy(state.rows[offset:offset + limit])
                data = {"version": state.version, "torrentContent": {"search": {
                    "items": items, "totalCount": len(state.rows) if state.estimate is None else state.estimate,
                    "totalCountIsEstimate": state.estimate is not None, "hasNextPage": offset + len(items) < len(state.rows)}}}
            else:
                raise AssertionError("Unadvertised GraphQL operation")
            response = {"data": data}
            if state.response_hook:
                response = state.response_hook(response, payload)
            raw = state.raw_response if state.raw_response is not None else json.dumps(response).encode()
            self.send_response(state.http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if state.location:
                self.send_header("Location", state.location)
            self.end_headers()
            try:
                if state.slow_body:
                    for chunk in (raw[:1], raw[1:2], raw[2:]):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        time.sleep(.06)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            state.calls.append({"method": "GET", "path": self.path})
            self.send_error(405)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            config = ServiceConfig(f"http://127.0.0.1:{server.server_port}" + prefix, api_key=api_key)
            yield ServiceQueryAdapter({"bitmagnet": config}, **native_options), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _search_call():
    return Call("service_query", {"service": "bitmagnet", "operation": "search", "query": "Fixture title"})


@pytest.mark.parametrize("query,operation", [
    ('Search Bitmagnet for "Fixture title"', "search"),
    ('Find "Fixture title" in Bitmagnet', "search"),
    ('Look up "Fixture title" in Bitmagnet', "search"),
    ("Show Bitmagnet health", "health"),
])
def test_explicit_nl_runtime_executor_and_native_http_complete_without_model_or_gateway(query, operation):
    class NoInference(NativeOnlyModel):
        def infer(self, *_args, **_kwargs):
            raise AssertionError("The explicit native route must not invoke inference")
    with bitmagnet_fixture(page_size=1) as (native, state):
        runtime = ArrmesRuntimeService(model=NoInference(), executor=BoundedExecutor(ConfiguredAdapterRouter(native)))
        result = runtime.execute(query, READER)
        assert result["terminal_state"] == "completed", result
        assert result["routing_source"] == "explicit_service_deterministic"
        assert result["proposal"]["arguments"]["operation"] == operation
        assert result["observation"]["data"]["source"] == "native_api"
        assert state.calls and all(c["method"] == "POST" and c["path"] == "/graphql" for c in state.calls)
        assert all(c["payload"]["query"].startswith("query ArrmesBitmagnet") for c in state.calls)
        data = json.dumps(result["observation"])
        assert "/private" not in data and "DO-NOT-EXPOSE" not in data and "proxy-fixture-key" not in data


def test_existing_v2_action_uses_native_bridge_with_scripted_output_and_no_legacy_gateway():
    query = 'Search Bitmagnet for "Fixture title"'
    call = Call("bitmagnet_search", {"query": "Fixture title"})
    with bitmagnet_fixture() as (native, state):
        trace = BoundedExecutor(ConfiguredAdapterRouter(native)).execute_raw_proposal(
            encode(call, "hermes"), READER, query=query, family="hermes")
        assert trace.terminal_state == "completed" and trace.proposal == normalized(call)
        assert trace.observation.operation == "bitmagnet_search"
        assert state.calls and trace.observation.data["source"] == "native_api"


def test_unquoted_search_preserves_neural_v2_runtime_and_native_execution():
    query = "Search Bitmagnet for Fixture title"
    call = Call("bitmagnet_search", {"query": "Fixture title"})
    class RecordingModel:
        family = "hermes"
        calls = 0
        def infer(self, actual_query, *_args, **_kwargs):
            assert actual_query == query
            self.calls += 1
            return {"raw": encode(call, "hermes")}
    assert route_service_query(query) is None
    with bitmagnet_fixture() as (native, state):
        model = RecordingModel()
        runtime = ArrmesRuntimeService(model=model, executor=BoundedExecutor(ConfiguredAdapterRouter(native)))
        result = runtime.execute(query, READER)
        assert model.calls == 1 and result["terminal_state"] == "completed"
        assert result["routing_source"] == "neural_v2" and result["proposal"] == normalized(call)
        assert result["observation"]["data"]["source"] == "native_api" and state.calls


def test_unquoted_search_in_model_free_mode_abstains_without_an_upstream_call():
    with bitmagnet_fixture() as (native, state):
        runtime = ArrmesRuntimeService(model=NativeOnlyModel(), executor=BoundedExecutor(ConfiguredAdapterRouter(native)))
        result = runtime.execute("Search Bitmagnet for Fixture title", READER)
        assert result["terminal_state"] == "clarify" and not state.calls
        assert result["routing_source"] == "native_only_abstention"


def test_search_document_is_immutable_and_query_text_is_only_a_variable():
    text = 'mutation { deleteTorrents } # " injected'
    with bitmagnet_fixture() as (native, state):
        obs = NativeBitmagnetAdapter(native).execute(Call("bitmagnet_search", {"query": text}), READER)
        assert obs.verified_complete
        payload = state.calls[0]["payload"]
        assert payload["variables"]["input"]["queryString"] == text
        assert text not in payload["query"] and "deleteTorrents" not in payload["query"]
        before = len(state.calls)
        with pytest.raises(InvalidCall, match="fixed"):
            _post_read(native, "mutation", {}, [])
        assert len(state.calls) == before


@pytest.mark.parametrize("query", [
    "Search Bitmagnet", 'Search Bitmagnet for "First" or "Second"',
    'Search Bitmagnet for "Fixture title" above 10 seeders',
    'Show Bitmagnet health for "Fixture title"',
    'Delete "Fixture title" from Bitmagnet', 'Import "Fixture title" into Bitmagnet',
    'Show Bitmagnet queue', 'Search Bitmagnet for "Fixture title" then download it',
])
def test_unknown_filters_mutations_and_ambiguous_searches_never_dispatch(query):
    class NoNetwork:
        supports_service_queries = True
        def execute(self, *_args, **_kwargs):
            raise AssertionError("Unsupported request must not dispatch")
    runtime = ArrmesRuntimeService(model=NativeOnlyModel(), executor=BoundedExecutor(NoNetwork()))
    result = runtime.execute(query, READER)
    assert result["terminal_state"] in {"clarify", "unsupported"} and result["steps_executed"] == 0


@pytest.mark.parametrize("operation", ["search", "health"])
def test_scopes_and_configuration_are_checked_before_network(operation):
    with bitmagnet_fixture() as (native, state):
        call = _search_call() if operation == "search" else Call("service_query", {"service": "bitmagnet", "operation": "health"})
        obs = native.execute(call, Principal("reader", frozenset({"media:read"})))
        assert obs.status == "denied" and not state.calls
        obs = ServiceQueryAdapter({}).execute(call, READER)
        assert obs.status == "error" and obs.data["error"] == "service_not_configured"
        bridge = NativeBitmagnetAdapter(native)
        assert bridge.supports("bitmagnet_search") and not bridge.supports("movie_status")
        assert bridge.execute(Call("bitmagnet_search", {"query": "Fixture title"}), Principal("reader", frozenset())).status == "denied"
        assert not state.calls


def test_real_pagination_uses_next_page_flag_not_an_estimated_count():
    state = BitmagnetFixture(estimate=1)
    with bitmagnet_fixture(state, page_size=1) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.verified_complete and obs.data["returned_count"] == 2
        assert obs.data["total_count_is_estimate"] and obs.data["reported_total_count"] == 1
        assert obs.data["total_records"] == 2 and obs.data["pages_read"] == 2
        assert [c["payload"]["variables"]["input"]["offset"] for c in state.calls] == [0, 1]
        assert obs.data["items"][0]["magnet_uri"] == "magnet:?xt=urn:btih:" + f"{1:040x}"


@pytest.mark.parametrize("bounds,reason", [({"page_size": 1, "max_pages": 1}, "page_limit"),
                                         ({"page_size": 2, "max_records": 1}, "record_limit")])
def test_finite_page_and_record_caps_return_partial_not_a_false_complete_count(bounds, reason):
    with bitmagnet_fixture(**bounds) as (native, state):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "partial" and not obs.verified_complete
        assert obs.data["total_records"] is None and obs.data["pagination_end"] == reason
        assert len(state.calls) == 1


def test_repeated_pages_and_changed_exact_totals_cannot_claim_complete_results():
    state = BitmagnetFixture(repeat_page=True)
    with bitmagnet_fixture(state, page_size=1) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "partial" and obs.data["pagination_end"] == "nonadvancing_or_overlapping_page"
        assert obs.data["returned_count"] == 1 and len(state.calls) == 2
    state = BitmagnetFixture()
    def changed(response, payload):
        if payload["variables"]["input"]["offset"]:
            response["data"]["torrentContent"]["search"]["totalCount"] = 3
        return response
    state.response_hook = changed
    with bitmagnet_fixture(state, page_size=1) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "partial" and obs.data["pagination_end"] == "search_changed_during_pagination"


@pytest.mark.parametrize("state", [
    BitmagnetFixture(rows=[]), BitmagnetFixture(rows=[], estimate=99),
])
def test_empty_native_search_is_a_valid_complete_read_even_when_count_is_estimated(state):
    with bitmagnet_fixture(state) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.verified_complete and obs.data["items"] == [] and obs.data["total_records"] == 0


@pytest.mark.parametrize("fault", ["id", "hash", "torrent_hash", "size", "seeders", "torrent", "empty"])
def test_malformed_native_search_records_are_not_verified(fault):
    row = _row(1)
    if fault == "id": row["id"] = 7
    elif fault == "hash": row["infoHash"] = "not-a-hash"
    elif fault == "torrent_hash": row["torrent"]["infoHash"] = f"{2:040x}"
    elif fault == "size": row["torrent"]["size"] = True
    elif fault == "seeders": row["seeders"] = -1
    elif fault == "torrent": row["torrent"] = None
    else: row = {}
    with bitmagnet_fixture(BitmagnetFixture(rows=[row])) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "error" and not obs.verified_complete


@pytest.mark.parametrize("partial_data", [True, False])
def test_graphql_errors_with_http200_are_errors_and_raw_error_text_is_not_exposed(partial_data):
    state = BitmagnetFixture(response_hook=lambda response, _payload: {
        "data": response["data"] if partial_data else None,
        "errors": [{"message": "DO-NOT-EXPOSE", "extensions": {"url": "https://private.invalid"}}]})
    with bitmagnet_fixture(state) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "error" and obs.data["error"] == "bitmagnet_graphql_error"
        assert "DO-NOT-EXPOSE" not in json.dumps(obs.data)


@pytest.mark.parametrize("raw", [b'not json', b'{"data":{},"data":{}}', b'{"data":NaN}', b'[]'])
def test_invalid_json_duplicate_keys_nonfinite_and_wrong_envelopes_are_rejected(raw):
    with bitmagnet_fixture(BitmagnetFixture(raw_response=raw)) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "error" and not obs.verified_complete


@pytest.mark.parametrize("code", [401, 403, 503])
def test_authentication_or_unavailable_status_is_structured_and_never_retried(code):
    with bitmagnet_fixture(BitmagnetFixture(http_status=code)) as (native, state):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "error" and obs.http_status == code and len(state.calls) == 1
        assert obs.data["error"] == ("upstream_authentication_failed" if code in {401, 403} else "upstream_http_error")


def test_redirects_do_not_forward_proxy_credentials_or_replay_the_post():
    with bitmagnet_fixture(BitmagnetFixture(http_status=307, location="http://127.0.0.1:9/redirect")) as (native, state):
        obs = native.execute(_search_call(), READER)
        assert obs.data["error"] == "upstream_redirect_refused" and len(state.calls) == 1


def test_response_bytes_and_body_deadline_are_finite():
    with bitmagnet_fixture(max_response_bytes=32) as (native, _):
        assert native.execute(_search_call(), READER).data["error"] == "response_limit_exceeded"
    with bitmagnet_fixture(BitmagnetFixture(slow_body=True), timeout_seconds=.08) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "error" and obs.data["error"] == "upstream_read_deadline_exceeded"


@pytest.mark.parametrize("field,value", [("totalCount", True), ("totalCount", -1),
    ("totalCountIsEstimate", "false"), ("hasNextPage", None), ("hasNextPage", 1)])
def test_pagination_fields_require_their_actual_types(field, value):
    def malformed(response, _payload):
        response["data"]["torrentContent"]["search"][field] = value
        return response
    with bitmagnet_fixture(BitmagnetFixture(response_hook=malformed)) as (native, _):
        obs = native.execute(_search_call(), READER)
        assert obs.status == "error" and not obs.verified_complete


@pytest.mark.parametrize("fault", ["status", "check_status", "worker_started", "duplicate_check"])
def test_malformed_health_and_worker_fields_cannot_crash_or_report_healthy(fault):
    def malformed(response, _payload):
        data = response["data"]
        if fault == "status": data["health"]["status"] = []
        elif fault == "check_status": data["health"]["checks"][0]["status"] = {}
        elif fault == "worker_started": data["workers"]["listAll"]["workers"][0]["started"] = 1
        else: data["health"]["checks"] *= 2
        return response
    with bitmagnet_fixture(BitmagnetFixture(response_hook=malformed)) as (native, _):
        obs = native.execute(Call("service_query", {"service": "bitmagnet", "operation": "health"}), READER)
        assert obs.status == "error" and not obs.verified_complete


@pytest.mark.parametrize("health,healthy", [("up", True), ("down", False), ("inactive", None), ("unknown", None)])
def test_health_is_an_observation_not_a_claim_that_every_worker_is_running(health, healthy):
    with bitmagnet_fixture(BitmagnetFixture(health_status=health)) as (native, _):
        obs = native.execute(Call("service_query", {"service": "bitmagnet", "operation": "health"}), READER)
        assert obs.verified_complete and obs.data["healthy"] is healthy and obs.data["health_status"] == health


def test_reverse_proxy_prefix_and_optional_proxy_key_work_without_inventing_native_auth(tmp_path):
    with bitmagnet_fixture(prefix="/indexer", api_key="") as (native, state):
        config = native.configs["bitmagnet"]
        path = tmp_path / "services.json"
        path.write_text(json.dumps({"services": {"bitmagnet": {"base_url": config.base_url}}}))
        assert load_service_configs(path)["bitmagnet"] == config
        assert native.execute(_search_call(), READER).verified_complete
        assert state.calls[0]["path"] == "/indexer/graphql" and "X-Api-Key" not in state.calls[0]["headers"]
        native.configs["bitmagnet"] = ServiceConfig(config.base_url, session_id="fixture-session")
        before = len(state.calls)
        assert native.execute(_search_call(), READER).data["error"] == "bitmagnet_session_auth_unsupported"
        assert len(state.calls) == before


def test_v2_neural_tool_manifest_is_unchanged():
    # Immutable v1.2.0 model-facing manifest at source e4768ee. The v3 manifest
    # separately incorporates added native pairs and permissions.
    assert contract_hash_v2() == "e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8"
