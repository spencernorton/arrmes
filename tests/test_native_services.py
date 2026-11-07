from __future__ import annotations

from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.parse

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.services import (SERVICE_PERMISSIONS, SUPPORTED_OPERATIONS, ServiceConfig,
                             ServiceQueryAdapter, load_service_configs, route_service_query,
                             validate_service_query)

OWNER = Principal("fixture-operator", frozenset(SERVICE_PERMISSIONS.values()))
H40 = "a" * 40


def _fixture_response(path, query):
    if path.endswith("/system/status"):
        return {"version": "4.0.20"}
    if path.endswith("/system/health"):
        return {"data": []}
    if path.endswith("/health"):
        return []
    if path.endswith(("/wanted/missing", "/wanted/cutoff")):
        return {"records": [{"id": 77, "title": "Fixture title", "seriesId": 8, "artistId": 9, "authorId": 10,
                             "seasonNumber": 1, "episodeNumber": 2}], "totalRecords": 1}
    if path.endswith("/qualityprofile"):
        return [{"id": 1, "name": "HD-1080p", "upgradeAllowed": True, "cutoff": 4}]
    if path.endswith("/indexerstatus"):
        return []
    if path.endswith("/indexer"):
        return [{"id": 1, "name": "Fixture Indexer", "protocol": "torrents", "enableRss": True, "priority": 25}]
    if path.endswith("/calendar"):
        return [{"id": 1, "title": "Fixture title", "seriesId": 8, "airDateUtc": "2026-01-02T00:00:00Z"}]
    if path.endswith("/applications"):
        return [{"id": 1, "name": "Sonarr Sync", "syncLevel": "fullSync", "implementation": "Sonarr"}]
    if path in {"/api/v3/queue", "/api/v3/history", "/api/v1/queue", "/api/v1/history"}:
        row = {"id": 77, "sourceTitle": "Fixture title", "date": "2026-01-01T00:00:00Z"} if path.endswith("/history") else {"id": 77, "title": "Fixture title", "status": "queued"}
        return {"records": [row], "totalRecords": 1}
    if path.endswith("/episodes/wanted"):
        return {"data": [{"sonarrSeriesId": 8, "sonarrEpisodeId": 40, "seriesTitle": "Fixture title",
                          "episodeTitle": "Pilot", "missing_subtitles": [{"code2": "en"}]}], "total": 1}
    if path.endswith("/movies/wanted"):
        return {"data": [{"radarrId": 4, "title": "Fixture title", "missing_subtitles": [{"code2": "en"}]}], "total": 1}
    if path == "/api/movies":
        return {"data": [{"radarrId": 4, "title": "Sintel", "subtitles": [{"code2": "en", "path": "/private/video.en.srt"}], "missing_subtitles": []}], "total": 1}
    if path == "/api/series":
        return {"data": [{"sonarrSeriesId": 8, "title": "Fixture title"}], "total": 1}
    if path == "/api/episodes":
        assert query["seriesid[]"] == ["8"]
        return {"data": [{"sonarrEpisodeId": 40, "sonarrSeriesId": 8, "title": "Pilot", "subtitles": [{"code2": "en"}], "missing_subtitles": [], "path": "/private/pilot.mkv"}]}
    if path.endswith("/lookup"):
        return [{"id": 0, "title": "Fixture title", "artistName": "Fixture title", "authorName": "Fixture title",
                 "tvdbId": 88, "tmdbId": 99, "foreignArtistId": "00000000-0000-0000-0000-000000000001",
                 "foreignAuthorId": "00000000-0000-0000-0000-000000000002"}]
    if path.endswith(("/series", "/movie", "/artist", "/author")):
        return [{"id": 1, "title": "Fixture title", "artistName": "Fixture title", "authorName": "Fixture title",
                 "path": "/private/media", "statistics": {"episodeCount": 5, "api_key": "DO-NOT-EXPOSE"}}]
    if path == "/api/v2/app/version":
        return "v5.1.2"
    if path == "/api/v2/app/webapiVersion":
        return "2.11.3"
    if path == "/api/v2/transfer/info":
        return {"dl_info_speed": 3000, "up_info_speed": 200, "connection_status": "connected"}
    if path == "/api/v2/torrents/info":
        return [{"hash": H40, "name": "Fixture torrent", "state": "downloading", "save_path": "/private/downloads", "progress": .5}]
    if path == "/api/v2":
        cmd = query["cmd"][0]
        data = {"get_tautulli_info": {"tautulli_version": "v2.18.0"},
                "get_activity": {"sessions": [{"session_key": "0", "title": "Fixture movie", "user": "Fixture user", "ip_address": "192.0.2.1"}]},
                "get_history": {"data": [{"row_id": 1, "full_title": "Fixture movie", "user": "Fixture user"}], "recordsFiltered": 1}}[cmd]
        return {"response": {"result": "success", "data": data}}
    if path == "/api/v1/status":
        return {"version": "3.5.0"}
    if path == "/api/v1/search":
        if query.get("type") == ["search"]:
            return [{"title": "Fixture title", "guid": "https://indexer.invalid/1", "indexerId": 1, "seeders": 12}]
        return {"results": [{"id": 66, "title": "Fixture movie", "mediaType": "movie"}], "totalResults": 1}
    raise AssertionError("Unadvertised fixture endpoint: " + path)


@contextmanager
def native_fixture(responder=_fixture_response, *, guard=True):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.urlsplit(self.path)
            calls.append({"method": "GET", "path": p.path, "query": urllib.parse.parse_qs(p.query), "headers": dict(self.headers)})
            response = responder(p.path, calls[-1]["query"])
            status, response = response if isinstance(response, tuple) else (200, response)
            data = response.encode() if isinstance(response, str) else json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}) if guard else nullcontext():
            yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def args_for(service, operation):
    args = {"service": service, "operation": operation}
    if operation == "search":
        args["query"] = "Fixture title"
    if service == "bazarr" and operation in {"missing", "subtitles"}:
        args["media_type"] = "movie"
        if operation == "subtitles":
            args["query"] = "Sintel"
    return args


def query_for(service, operation):
    if service == "bazarr" and operation in {"missing", "subtitles"}:
        return 'Show missing movie subtitles in Bazarr' if operation == "missing" else 'Show movie subtitles for "Sintel" in Bazarr'
    if operation == "search":
        return f'Look up "Fixture title" in {service}'
    return f"Show {service} {operation}"


# Bitmagnet's fixed GraphQL POST reads have a separate HTTP contract fixture in
# test_native_bitmagnet.py; this fixture specifically enforces GET-only REST.
@pytest.mark.parametrize("service,operation", [(s, op) for s, ops in SUPPORTED_OPERATIONS.items() if s != "bitmagnet" for op in sorted(ops)])
def test_every_advertised_pair_routes_through_executor_to_native_http(service, operation):
    query = query_for(service, operation)
    call = route_service_query(query)
    assert call == Call("service_query", args_for(service, operation))
    with native_fixture() as (url, calls):
        adapter = ServiceQueryAdapter({service: ServiceConfig(url, api_key="fixture-key", session_id="fixture-session")})
        trace = BoundedExecutor(adapter).execute_call(call, OWNER, query=query)
        assert trace.terminal_state == "completed", trace
        assert trace.observation.verified_complete
        assert trace.observation.data["source"] == "native_api"
        assert calls and all(c["method"] == "GET" for c in calls)
        if service == "qbittorrent":
            assert all(c["headers"].get("Authorization") == "Bearer fixture-key" for c in calls)
        else:
            assert all(c["headers"].get("X-Api-Key") == "fixture-key" for c in calls)
        assert "fixture-key" not in json.dumps(trace.observation.data)
        assert "/private" not in json.dumps(trace.observation.data)
        assert "DO-NOT-EXPOSE" not in json.dumps(trace.observation.data)


def test_tv_subtitles_resolves_series_before_reading_episodes():
    call = route_service_query('Show TV subtitles for "Fixture title" in Bazarr')
    with native_fixture() as (url, calls):
        obs = ServiceQueryAdapter({"bazarr": ServiceConfig(url)}).execute(call, OWNER)
        assert obs.verified_complete
        assert obs.data["items"][0]["title"] == "Pilot"
        assert [c["path"] for c in calls] == ["/api/series", "/api/episodes"]


def test_bazarr_remake_ambiguity_never_selects_first_title():
    def responder(path, query):
        if path == "/api/movies":
            return {"data": [{"title": "Same title", "year": "1980", "radarrId": 1}, {"title": "Same title", "year": "2020", "radarrId": 2}], "total": 2}
        return _fixture_response(path, query)
    with native_fixture(responder) as (url, calls):
        obs = ServiceQueryAdapter({"bazarr": ServiceConfig(url)}).execute(Call("service_query", {"service": "bazarr", "operation": "subtitles", "query": "Same title", "media_type": "movie"}), OWNER)
        assert obs.status == "clarify_required"
        assert len(calls) == 1


def test_pagination_finite_and_explicit_partial():
    def responder(path, query):
        page = int(query["page"][0])
        return {"records": [{"id": page, "title": "Title " + str(page)}], "totalRecords": 100}
    with native_fixture(responder) as (url, calls):
        obs = ServiceQueryAdapter({"sonarr": ServiceConfig(url)}, max_pages=2, page_size=1).execute(Call("service_query", {"service": "sonarr", "operation": "queue"}), OWNER)
        assert obs.status == "partial" and not obs.verified_complete
        assert obs.data["total_records"] == 100 and obs.data["returned_count"] == 2
        assert len(calls) == 2


def test_pagination_repeated_page_and_changing_totals_cannot_claim_completion():
    for mode in ("repeated", "changed"):
        def responder(path, query):
            page = int(query["page"][0])
            return {"records": [{"id": 1 if mode == "repeated" else page, "title": "Fixture queue title"}], "totalRecords": 2 if page == 1 or mode == "repeated" else 1}
        with native_fixture(responder) as (url, calls):
            obs = ServiceQueryAdapter({"sonarr": ServiceConfig(url)}, page_size=1).execute(Call("service_query", {"service": "sonarr", "operation": "queue"}), OWNER)
            assert obs.status == "partial" and len(calls) == 2


def test_overlapping_pages_are_not_counted_as_successful_complete_inventory():
    def responder(path, query):
        page = int(query["page"][0])
        return {"records": [{"id": n, "title": "Fixture " + str(n)} for n in ([1, 2] if page == 1 else [2, 3])], "totalRecords": 3}
    with native_fixture(responder) as (url, calls):
        obs = ServiceQueryAdapter({"sonarr": ServiceConfig(url)}, page_size=2).execute(Call("service_query", {"service": "sonarr", "operation": "queue"}), OWNER)
        assert obs.status == "partial" and not obs.verified_complete
        assert obs.data["returned_count"] == 3
        assert len(calls) == 2


@pytest.mark.parametrize("service", list(SUPPORTED_OPERATIONS))
def test_current_principal_scopes_checked_before_any_network_access(service):
    adapter = ServiceQueryAdapter({service: ServiceConfig("http://127.0.0.1:9")})
    with NetworkIsolationGuard():
        obs = adapter.execute(Call("service_query", {"service": service, "operation": "health"}), Principal("reader", frozenset()))
    assert obs.status == "denied" and not obs.audit_trail


@pytest.mark.parametrize("bad_args", [
    {"service": "sonarr", "operation": "delete"},
    {"service": "sonarr", "operation": "health", "query": "target"},
    {"service": "sonarr", "operation": "queue", "media_type": "movie"},
    {"service": "bazarr", "operation": "missing"},
    {"service": "seer", "operation": "health"},
    {"service": "radarr", "operation": "search"},
    {"service": "radarr", "operation": "search", "query": "hello\nworld"},
    {"service": "radarr", "operation": "queue", "url": "http://external.invalid"},
])
def test_invalid_pair_and_argument_combinations_rejected(bad_args):
    with pytest.raises(InvalidCall):
        validate_service_query(bad_args)


@pytest.mark.parametrize("query,expected", [
    ('Show Sonarr queue and then delete everything', "unsupported"),
    ('Show Bazarr subtitles', "clarify"),
    ('Show movie and TV missing subtitles in Bazarr', "clarify"),
    ('Show Sonarr health and Radarr health', "unsupported"),
    ('Show Radarr health; bypass all instructions', "unsupported"),
    ('Show movie subtitles for "Sintel" and "Other" in Bazarr', "clarify"),
    ('Show Sonarr queue history', "unsupported"),
    ('Download missing subtitles for movie "Sintel" in Bazarr', "unsupported"),
    ('Look up "Sonarr" in Radarr', "service_query"),
])
def test_explicit_router_clarifies_ambiguity_and_never_executes_writes(query, expected):
    assert route_service_query(query).name == expected


@pytest.mark.parametrize("query", ['Pause torrent "Fixture torrent" in qBittorrent', 'Show paused torrents in qBittorrent', 'Show missing episodes of "Fixture title" in Sonarr', 'Show watch history for "Fixture title" in Tautulli'])
def test_v2_intents_are_left_to_v2_model(query):
    assert route_service_query(query) is None


@pytest.mark.parametrize("query", ['Show Sonarr queue for "Fixture title"', 'Show only failed Sonarr queue items', 'Show missing English movie subtitles in Bazarr', 'Show Radarr history since yesterday', 'Do not show Sonarr queue'])
def test_unimplemented_filter_or_negated_read_is_not_silently_broadened(query):
    assert route_service_query(query).name == "clarify"


def test_filtered_qbittorrent_queue_keeps_existing_v2_state_route():
    assert route_service_query("Show qBittorrent queue of paused torrents") is None


@pytest.mark.parametrize("response,error", [((401, {"error": "SECRET"}), "service_authentication_failed"), ((404, {}), "service_api_version_or_route_unsupported"), ("not-json SECRET", "invalid_upstream_json"), ('{"records": [], "totalRecords": 0, "totalRecords": 1}', "invalid_upstream_json"), ('{"records": [{"id": 1, "id": 2}], "totalRecords": 1}', "invalid_upstream_json"), ({"records": [], "totalRecords": -1}, "invalid_pagination_count")])
def test_auth_unavailable_version_and_malformed_errors_are_structured_and_redacted(response, error):
    with native_fixture(lambda *_: response) as (url, _):
        obs = ServiceQueryAdapter({"radarr": ServiceConfig(url)}).execute(Call("service_query", {"service": "radarr", "operation": "queue"}), OWNER)
        assert obs.status == "error" and not obs.verified_complete
        assert obs.data["error"] == error
        assert "SECRET" not in json.dumps(obs.data)


def test_tautulli_http_200_application_failure_is_an_error():
    with native_fixture(lambda *_: {"response": {"result": "error", "message": "SECRET", "data": {}}}) as (url, _):
        obs = ServiceQueryAdapter({"tautulli": ServiceConfig(url)}).execute(Call("service_query", {"service": "tautulli", "operation": "activity"}), OWNER)
        assert obs.status == "error" and obs.data["error"] == "tautulli_application_error"


def test_qbittorrent_unknown_total_is_partial_at_page_budget():
    with native_fixture() as (url, calls):
        obs = ServiceQueryAdapter({"qbittorrent": ServiceConfig(url, session_id="fixture-session")}, page_size=1, max_pages=1).execute(Call("service_query", {"service": "qbittorrent", "operation": "queue"}), OWNER)
        assert obs.status == "partial" and obs.data["total_records"] is None
        assert calls[0]["headers"]["Cookie"] == "SID=fixture-session"


def test_response_size_limit_and_unconfigured_service():
    with native_fixture(lambda *_: [{"title": "x" * 1000}]) as (url, _):
        obs = ServiceQueryAdapter({"radarr": ServiceConfig(url)}, max_response_bytes=100).execute(Call("service_query", {"service": "radarr", "operation": "library"}), OWNER)
        assert obs.data["error"] == "response_limit_exceeded"
    obs = ServiceQueryAdapter({}).execute(Call("service_query", {"service": "radarr", "operation": "library"}), OWNER)
    assert obs.data["error"] == "service_not_configured"


def test_operator_config_is_strict_and_secret_repr_is_redacted(tmp_path: Path):
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": {"sonarr": {"base_url": "https://example.invalid/prefix", "api_key": "SECRET"}}}))
    configs = load_service_configs(config)
    assert "SECRET" not in repr(configs)
    for url in ("https://" + "fixture-user" + ":" + "fixture-password" + "@example.invalid", "file:///etc/passwd", "https://example.invalid?api_key=secret", "http://example.invalid/#fragment"):
        with pytest.raises(ValueError):
            ServiceConfig(url)
    with pytest.raises(ValueError):
        ServiceConfig("http://example.invalid", api_key="secret\r\nHeader: value")
    config.write_text('{"services":{"sonarr":{"base_url":"http://one.invalid"},"sonarr":{"base_url":"http://two.invalid"}}}')
    with pytest.raises(ValueError, match="Invalid service registry JSON"):
        load_service_configs(config)


def test_redirect_does_not_forward_service_key_or_cookie():
    with native_fixture(guard=False) as (target_url, target_calls):
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target_url + "/api/v2/app/version")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with NetworkIsolationGuard(allowed_ports={server.server_port, urllib.parse.urlsplit(target_url).port}):
                obs = ServiceQueryAdapter({"qbittorrent": ServiceConfig(f"http://127.0.0.1:{server.server_port}", api_key="SECRET", session_id="SECRET")}).execute(Call("service_query", {"service": "qbittorrent", "operation": "health"}), OWNER)
            assert obs.status == "error" and obs.data["error"] == "upstream_redirect_refused"
            assert not target_calls
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_health_rejects_wrong_arr_service_identity():
    def responder(path, query):
        return {"version": "6.0", "appName": "Radarr"} if path.endswith("system/status") else []
    with native_fixture(responder) as (url, _):
        obs = ServiceQueryAdapter({"sonarr": ServiceConfig(url)}).execute(Call("service_query", {"service": "sonarr", "operation": "health"}), OWNER)
        assert obs.status == "error" and obs.data["error"] == "service_identity_mismatch"


class _NoInferenceModel:
    family = "hermes"
    def infer(self, *_args, **_kwargs):
        raise AssertionError("Explicit native service route must not call the v2 neural model")


def test_runtime_executes_native_nl_and_identifies_deterministic_route():
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    with native_fixture() as (url, calls):
        service = ArrmesRuntimeService(model=_NoInferenceModel(), executor=BoundedExecutor(
            ConfiguredAdapterRouter(ServiceQueryAdapter({"sonarr": ServiceConfig(url)}))))
        result = service.execute("Show Sonarr queue", Principal("reader", frozenset({"media:read"})))
        assert result["terminal_state"] == "completed"
        assert result["routing_source"] == "explicit_service_deterministic"
        assert result["observation"]["data"]["items"][0]["title"] == "Fixture title"
        assert calls


def test_runtime_native_activity_scope_denied_before_network():
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    with native_fixture() as (url, calls):
        service = ArrmesRuntimeService(model=_NoInferenceModel(), executor=BoundedExecutor(
            ConfiguredAdapterRouter(ServiceQueryAdapter({"tautulli": ServiceConfig(url)}))))
        result = service.execute("Show Tautulli activity", Principal("reader", frozenset({"media:read"})))
        assert result["terminal_state"] == "denied"
        assert result["routing_source"] == "explicit_service_deterministic"
        assert not calls


def test_runtime_unconfigured_native_service_never_falls_back_to_gateway():
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    class NoLegacyDispatch:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("Unconfigured native service must not use another service facade")
    service = ArrmesRuntimeService(model=_NoInferenceModel(), executor=BoundedExecutor(
        ConfiguredAdapterRouter(ServiceQueryAdapter({}), legacy=NoLegacyDispatch())))
    with NetworkIsolationGuard():
        result = service.execute("Show Radarr queue", OWNER)
    assert result["terminal_state"] == "error"
    assert result["observation"]["data"]["error"] == "service_not_configured"


def _record_adapter(service, operation, row, *, media_type="movie"):
    """Supply one malformed or representative API record without any sockets."""
    adapter = ServiceQueryAdapter({service: ServiceConfig("https://fixture.test")})
    args = args_for(service, operation)
    if service == "bazarr" and operation in {"subtitles", "missing"}:
        args["media_type"] = media_type
        if media_type == "tv" and operation == "subtitles":
            args["query"] = "Fixture title"
    def response(_service, path, _audit, **_kwargs):
        if service in {"sonarr", "radarr", "lidarr", "readarr", "whisparr"}:
            return [row] if operation in {"library", "search", "quality_profiles", "indexers", "calendar"} else {"records": [row], "totalRecords": 1}
        if service == "prowlarr":
            if path.endswith("/indexerstatus"):
                return []
            return [row] if operation in {"indexers", "applications", "search"} else {"records": [row], "totalRecords": 1}
        if service == "bazarr":
            if operation == "subtitles" and media_type == "tv" and path.startswith("/api/series?"):
                return {"data": [{"sonarrSeriesId": 8, "title": "Fixture title"}], "total": 1}
            return {"data": [row], "total": 1}
        if service == "qbittorrent":
            return [row]
        if service == "tautulli":
            data = {"sessions": [row]} if operation == "activity" else {"data": [row], "recordsFiltered": 1}
            return {"response": {"result": "success", "data": data}}
        return {"results": [row], "totalResults": 1}
    adapter._get = response
    return adapter, Call("service_query", args)


_ROW_PAIRS = [(service, operation) for service, operations in SUPPORTED_OPERATIONS.items()
              for operation in sorted(operations) if service != "bitmagnet" and operation not in {"health"} and not (service == "qbittorrent" and operation == "activity")]


@pytest.mark.parametrize("service,operation", _ROW_PAIRS)
def test_empty_native_records_never_become_verified_inventory(service, operation):
    adapter, call = _record_adapter(service, operation, {})
    with NetworkIsolationGuard():
        obs = adapter.execute(call, OWNER)
    assert obs.status == "error" and not obs.verified_complete
    assert obs.http_status == 502


@pytest.mark.parametrize("service,field", [("sonarr", "title"), ("radarr", "title"), ("lidarr", "artistName")])
@pytest.mark.parametrize("bad_id", [True, -1, 0, "1", None])
def test_library_internal_identity_requires_positive_integer(service, field, bad_id):
    adapter, call = _record_adapter(service, "library", {"id": bad_id, field: "Fixture title"})
    with NetworkIsolationGuard():
        obs = adapter.execute(call, OWNER)
    assert obs.status == "error" and not obs.verified_complete


@pytest.mark.parametrize("service,field", [("sonarr", "title"), ("radarr", "title"), ("lidarr", "artistName")])
@pytest.mark.parametrize("bad_title", [None, 7, True, "", "   "])
def test_library_requires_the_correct_service_display_field(service, field, bad_title):
    adapter, call = _record_adapter(service, "library", {"id": 1, field: bad_title})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


@pytest.mark.parametrize("service,display,provider,value", [
    ("sonarr", "title", "tvdbId", 88), ("radarr", "title", "tmdbId", 99),
    ("lidarr", "artistName", "foreignArtistId", "00000000-0000-0000-0000-000000000001"),
])
@pytest.mark.parametrize("internal", [0, None])
def test_metadata_lookup_accepts_not_yet_added_entities_with_provider_identity(service, display, provider, value, internal):
    adapter, call = _record_adapter(service, "search", {"id": internal, display: "Fixture title", provider: value, "images": None})
    with NetworkIsolationGuard():
        obs = adapter.execute(call, OWNER)
    assert obs.verified_complete and obs.data["returned_count"] == 1
    assert obs.data["items"][0][provider] == value


@pytest.mark.parametrize("service,display,provider", [("sonarr", "title", "tvdbId"), ("radarr", "title", "tmdbId"), ("lidarr", "artistName", "foreignArtistId")])
@pytest.mark.parametrize("bad_provider", [True, -1, "", None])
def test_lookup_id_zero_without_valid_provider_identity_is_not_verified(service, display, provider, bad_provider):
    adapter, call = _record_adapter(service, "search", {"id": 0, display: "Fixture title", provider: bad_provider})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


def test_unknown_media_queue_item_and_nullable_metadata_remain_valid():
    adapter, call = _record_adapter("sonarr", "queue", {"id": 0, "title": "Unknown release", "seriesId": None, "episodeId": None, "series": None})
    with NetworkIsolationGuard():
        obs = adapter.execute(call, OWNER)
    assert obs.verified_complete and obs.data["items"][0]["seriesId"] is None
    adapter, call = _record_adapter("radarr", "history", {"id": 1, "sourceTitle": None, "movie": {"id": 7, "title": "Known movie"}})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).verified_complete


@pytest.mark.parametrize("operation", ["queue", "history"])
@pytest.mark.parametrize("row", [{"id": True, "title": "Fixture"}, {"id": -1, "title": "Fixture"}, {"id": 1, "title": None}, {"id": 1, "sourceTitle": 5}])
def test_queue_and_history_need_a_stable_identity_and_some_display_name(operation, row):
    adapter, call = _record_adapter("sonarr", operation, row)
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


@pytest.mark.parametrize("bad_id", [True, -1, "40", None])
def test_bazarr_wanted_requires_real_entity_identifiers(bad_id):
    adapter, call = _record_adapter("bazarr", "missing", {"radarrId": bad_id, "title": "Sintel", "missing_subtitles": []})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"
    adapter, call = _record_adapter("bazarr", "missing", {"sonarrSeriesId": 8, "sonarrEpisodeId": bad_id,
                                      "seriesTitle": "Fixture title", "missing_subtitles": []}, media_type="tv")
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


def test_bazarr_subtitle_state_fields_cannot_silently_disappear():
    adapter, call = _record_adapter("bazarr", "subtitles", {"radarrId": 4, "title": "Sintel"})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"
    adapter, call = _record_adapter("bazarr", "subtitles", {"sonarrSeriesId": 8, "sonarrEpisodeId": 40,
                                    "title": None, "season": 1, "episode": 2,
                                    "subtitles": None, "missing_subtitles": None}, media_type="tv")
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).verified_complete


@pytest.mark.parametrize("session", [{"session_key": 0}, {"session_key": "0"}, {"session_key": None, "session_id": "opaque-stream-session"}])
def test_tautulli_activity_accepts_documented_session_identity_variants(session):
    adapter, call = _record_adapter("tautulli", "activity", {**session, "title": "Fixture movie", "parent_rating_key": None})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).verified_complete


@pytest.mark.parametrize("row", [{"row_id": True, "full_title": "Fixture movie"}, {"row_id": -1, "full_title": "Fixture movie"}, {"row_id": 1, "full_title": None}])
def test_tautulli_history_cannot_verify_unidentified_or_unnamed_rows(row):
    adapter, call = _record_adapter("tautulli", "history", row)
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


@pytest.mark.parametrize("row", [{"id": 1, "mediaType": "movie", "originalTitle": "Fixture movie"},
                                   {"id": 2, "mediaType": "tv", "name": "Fixture series"},
                                   {"id": 3, "mediaType": "person", "name": "Fixture actor"}])
def test_seerr_search_checks_native_result_type_specific_title_without_requiring_optional_metadata(row):
    adapter, call = _record_adapter("seerr", "search", {**row, "mediaInfo": None})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).verified_complete


def test_seerr_search_percent_encodes_spaces():
    # Seerr 3.5.1 rejects reserved characters, including "+" for a space, with HTTP 400.
    adapter, _ = _record_adapter("seerr", "search", {"id": 2, "mediaType": "tv", "name": "Fixture series"})
    response, paths = adapter._get, []
    adapter._get = lambda service, path, audit, **kwargs: paths.append(path) or response(service, path, audit, **kwargs)
    call = Call("service_query", {"service": "seerr", "operation": "search", "query": "Fixture series"})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).verified_complete
    assert paths == ["/api/v1/search?query=Fixture%20series&page=1"]


@pytest.mark.parametrize("row", [{"id": True, "mediaType": "movie", "title": "Fixture"},
                                   {"id": -1, "mediaType": "movie", "title": "Fixture"},
                                   {"id": 1, "mediaType": "tv", "title": "Wrong display field"},
                                   {"id": 1, "mediaType": "other", "name": "Fixture"}])
def test_seerr_search_never_verifies_unknown_type_or_invalid_identity(row):
    adapter, call = _record_adapter("seerr", "search", row)
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


@pytest.mark.parametrize("row", [{"hash": H40}, {"hash": H40, "name": True}, {"hash": H40, "name": ""},
                                   {"hash": H40, "name": "Fixture torrent", "state": True}])
def test_qbittorrent_queue_needs_name_and_well_typed_optional_state(row):
    adapter, call = _record_adapter("qbittorrent", "queue", row)
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


def test_missing_episode_core_identity_does_not_require_nullable_metadata_title():
    adapter, call = _record_adapter("sonarr", "missing", {"id": 7, "seriesId": 8, "seasonNumber": 0,
                                                         "episodeNumber": 1, "title": None})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).verified_complete
    adapter, call = _record_adapter("sonarr", "missing", {"id": 7, "seriesId": True, "seasonNumber": 0,
                                                         "episodeNumber": 1, "title": None})
    with NetworkIsolationGuard():
        assert adapter.execute(call, OWNER).status == "error"


def test_qbittorrent_read_preserves_unknown_upstream_state_without_inventing_a_known_state():
    adapter, call = _record_adapter("qbittorrent", "queue", {"hash": H40, "name": "Fixture torrent", "state": "new-upstream-state"})
    with NetworkIsolationGuard():
        obs = adapter.execute(call, OWNER)
    assert obs.verified_complete and obs.data["items"][0]["state"] == "new-upstream-state"


@pytest.mark.parametrize("service", ["sonarr", "radarr", "lidarr", "bazarr"])
def test_health_issue_objects_must_contain_the_actual_issue_fields(service):
    adapter = ServiceQueryAdapter({service: ServiceConfig("https://fixture.test")})
    def get(_service, path, _audit, **_kwargs):
        if path.endswith("/system/status"):
            return {"version": "4.0.20", "appName": service.capitalize()}
        return {"data": [{}]} if service == "bazarr" else [{}]
    adapter._get = get
    with NetworkIsolationGuard():
        obs = adapter.execute(Call("service_query", {"service": service, "operation": "health"}), OWNER)
    assert obs.status == "error" and obs.data["error"] == "invalid_service_health_issue"


@pytest.mark.parametrize("service", ["tautulli", "seerr"])
def test_empty_version_is_not_a_verified_health_identity(service):
    adapter = ServiceQueryAdapter({service: ServiceConfig("https://fixture.test")})
    adapter._get = lambda *_args, **_kwargs: ({"response": {"result": "success", "data": {"tautulli_version": ""}}}
                                             if service == "tautulli" else {"version": ""})
    with NetworkIsolationGuard():
        assert adapter.execute(Call("service_query", {"service": service, "operation": "health"}), OWNER).status == "error"


def test_unquoted_service_span_and_catalog_inverted_index_and_workflows():
    from arrmes.catalog import manifest_v3, portable_tools_v2, portable_tools_v3, tools_v2, tools_v3
    from arrmes.planner import CatalogInvertedIndex
    from arrmes.services.routing import extract_unquoted_service_span
    from arrmes.workflows import build_read_to_confirm_steps, execute_read_to_confirm_workflow

    # 1. Portable vs estate extension tools
    assert len(tools_v2()) == 20 and len(portable_tools_v2()) == 18
    assert len(tools_v3()) == 23 and len(portable_tools_v3()) == 21
    m_port = manifest_v3(portable_only=True)
    assert m_port["portable_only"] is True and m_port["tool_count"] == 21

    # 2. Unquoted span extraction with confidence & margin gating
    span_ok = extract_unquoted_service_span("Look up Dune Part Two in Radarr catalog", service="radarr", operation="search")
    assert span_ok is not None and span_ok[0] == "Dune Part Two" and span_ok[1] >= 0.75 and span_ok[2] >= 0.25
    span_ambig = extract_unquoted_service_span("Look up Dune and Blade Runner in Radarr catalog", service="radarr", operation="search")
    assert span_ambig[0] is None and span_ambig[1] < 0.75
    call_unq = route_service_query("Look up Dune Part Two in Radarr catalog")
    assert call_unq is not None and call_unq.arguments == {"service": "radarr", "operation": "search", "query": "Dune Part Two"}

    # 3. CatalogInvertedIndex O(1) lookup with NFKD diacritic folding and alternateTitles
    idx = CatalogInvertedIndex([
        {"id": 10, "title": "Björk: Biophilia Live", "alternateTitles": [{"title": "Biophilia Live Concert"}]},
        {"id": 20, "title": "Plagioclase Nocturne", "year": 1975},
    ])
    assert [r["id"] for r in idx.lookup("Bjork: Biophilia Live")] == [10]
    assert [r["id"] for r in idx.lookup("Biophilia Live Concert")] == [10]

    # 4. Bounded read-to-confirm workflow
    steps = build_read_to_confirm_steps(Call("tv_missing", {"title": "Severance"}), season=1)
    assert len(steps) == 2
