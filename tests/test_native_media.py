"""Controlled native HTTP fixtures for the six released media read actions."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

import pytest

from arrmes.executor import BoundedExecutor, SessionEntityLedger
from arrmes.planner import Principal
from arrmes.protocol import Call, encode, normalized
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.services import ServiceConfig, ServiceQueryAdapter
from arrmes.services.media import NativeMediaReadAdapter

READER = Principal("fixture-reader", frozenset({"media:read", "activity:read"}))
FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _episode(identity, **overrides):
    return {"id": identity, "seriesId": 1, "seasonNumber": 1, "episodeNumber": identity,
            "title": "Fixture episode " + str(identity), "monitored": True, "hasFile": False,
            "airDateUtc": "2020-01-01T00:00:00Z", "path": "/fixture/private/episode.mkv", **overrides}


@dataclass
class MediaFixtureState:
    series: list[dict] = field(default_factory=lambda: [{"id": 1, "title": "Fixture series", "year": 2020,
        "monitored": True, "statistics": {"episodeCount": 3, "episodeFileCount": 1}, "path": "/fixture/private/series"}])
    episodes: list[dict] = field(default_factory=lambda: [
        _episode(1), _episode(2, airDateUtc="2050-01-01T00:00:00Z"), _episode(3, airDateUtc=None),
        _episode(4, hasFile=True), _episode(5, monitored=False), _episode(6, seasonNumber=0)])
    movies: list[dict] = field(default_factory=lambda: [{"id": 2, "title": "Fixture movie", "year": 2020,
        "hasFile": True, "monitored": True, "sizeOnDisk": 123, "path": "/fixture/private/movie.mkv"}])
    artists: list[dict] = field(default_factory=lambda: [{"id": 4, "artistName": "Fixture artist", "monitored": True}])
    albums: list[dict] = field(default_factory=lambda: [
        {"id": 5, "artistId": 4, "title": "Fixture album", "statistics": {"trackCount": 10, "trackFileCount": 5}, "path": "/fixture/private/music"},
        {"id": 6, "artistId": 404, "title": "Fixture album", "statistics": {"trackCount": 8, "trackFileCount": 8}}])
    sessions: list[dict] = field(default_factory=lambda: [{"session_key": "0", "full_title": "Fixture movie",
        "title": "Fixture movie", "user": "Fixture viewer", "ip_address": "192.0.2.1"}])
    history: list[dict] = field(default_factory=lambda: [{"row_id": 1, "full_title": "Fixture series - Pilot",
        "title": "Pilot", "grandparent_title": "Fixture series", "user": "Fixture viewer", "ip_address": "192.0.2.1"}])
    calls: list[dict] = field(default_factory=list)
    faults: dict[str, tuple[int, object]] = field(default_factory=dict)
    repeat_history_page: bool = False


@contextmanager
def media_fixture(state: MediaFixtureState | None = None, **native_options):
    """Yield (configured ServiceQueryAdapter, state); never connects outside loopback."""
    state = state or MediaFixtureState()
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, code, data):
            raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            state.calls.append({"method": "GET", "path": url.path, "query": query})
            if self.headers.get("X-Api-Key") != "fixture-key":
                self._respond(401, {"error": "fixture authentication required"})
                return
            if url.path in state.faults:
                self._respond(*state.faults[url.path])
                return
            payloads = {"/api/v3/series": state.series, "/api/v3/episode": state.episodes,
                        "/api/v3/movie": state.movies, "/api/v1/artist": state.artists,
                        "/api/v1/album": state.albums}
            if url.path in payloads:
                # Album responses intentionally include a different artist, so
                # client-side entity filtering is exercised as well.
                self._respond(200, payloads[url.path])
                return
            if url.path == "/api/v2":
                command = query.get("cmd", [""])[0]
                if command == "get_activity":
                    data = {"sessions": state.sessions, "stream_count": len(state.sessions)}
                elif command == "get_history":
                    rows = state.history
                    search = query.get("search", [""])[0].casefold()
                    if search:
                        rows = [row for row in rows if any(search in str(value).casefold() for value in row.values())]
                    start = 0 if state.repeat_history_page else int(query.get("start", ["0"])[0])
                    length = int(query.get("length", ["50"])[0])
                    data = {"data": rows[start:start + length], "recordsFiltered": len(rows), "recordsTotal": len(state.history)}
                else:
                    self._respond(400, {})
                    return
                self._respond(200, {"response": {"result": "success", "data": data}})
                return
            self._respond(404, {})

        def do_POST(self):
            state.calls.append({"method": "POST", "path": self.path})
            self._respond(405, {"error": "read fixture forbids mutations"})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            config = ServiceConfig(f"http://127.0.0.1:{server.server_port}", api_key="fixture-key")
            yield ServiceQueryAdapter({service: config for service in ("sonarr", "radarr", "lidarr", "tautulli")}, **native_options), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


READ_CASES = [
    ('Is "Fixture series" in Sonarr?', Call("tv_status", {"title": "Fixture series"})),
    ('Which episodes of "Fixture series" are missing?', Call("tv_missing", {"title": "Fixture series"})),
    ('Check the 2020 movie "Fixture movie" in Radarr', Call("movie_status", {"title": "Fixture movie", "year": 2020})),
    ('Check the album "Fixture album" by "Fixture artist" in Lidarr', Call("music_status", {"artist": "Fixture artist", "album": "Fixture album"})),
    ('Who is watching Plex right now?', Call("now_playing", {})),
    ('Show Tautulli watch history for "Fixture series"', Call("watch_history", {"title": "Fixture series"})),
]


@pytest.mark.parametrize("query,call", READ_CASES)
def test_scripted_released_tool_proposals_bind_nl_and_complete_native_http_reads(query, call):
    # Selection quality is measured by the actual-model integration smoke;
    # these scripted native outputs exercise schema/grounding/executor/HTTP.
    with media_fixture() as (native, state):
        adapter = NativeMediaReadAdapter(native, clock=lambda: FIXED_NOW)
        executor = BoundedExecutor(adapter)
        trace = executor.execute_raw_proposal(encode(call, family="hermes"), READER, family="hermes", query=query)
        assert trace.terminal_state == "completed", trace
        assert trace.proposal == normalized(call)
        assert trace.observation.verified_complete
        assert trace.observation.operation == call.name
        assert trace.observation.data["source"] == "native_api"
        assert state.calls and all(row["method"] == "GET" for row in state.calls)
        rendered = json.dumps(trace.observation.data)
        assert "/fixture/private" not in rendered and "fixture-key" not in rendered and "192.0.2.1" not in rendered


def test_support_detection_and_scope_check_happen_without_network():
    adapter = NativeMediaReadAdapter(ServiceQueryAdapter({"sonarr": ServiceConfig("https://fixture.test")}))
    assert adapter.supports("tv_status") and adapter.supports("tv_missing")
    assert not adapter.supports("movie_status") and not adapter.supports("tv_search_missing")
    with NetworkIsolationGuard():
        obs = adapter.execute(Call("tv_status", {"title": "Fixture series"}), Principal("reader", frozenset()))
        assert obs.status == "denied" and not obs.audit_trail
        obs = adapter.execute(Call("movie_status", {"title": "Fixture movie"}), READER)
        assert obs.status == "error" and obs.data["error"] == "service_not_configured"


@pytest.mark.parametrize("index", range(6))
def test_native_read_permission_is_rechecked_before_any_upstream_access(index):
    with media_fixture() as (native, state):
        obs = NativeMediaReadAdapter(native).execute(READ_CASES[index][1], Principal("reader", frozenset()))
        assert obs.status == "denied" and not state.calls


def test_full_catalog_is_scanned_before_uniqueness_even_beyond_output_record_cap():
    fixture = MediaFixtureState()
    fixture.series.append({**fixture.series[0], "id": 99})
    with media_fixture(fixture, max_records=1) as (native, state):
        obs = NativeMediaReadAdapter(native).execute(Call("tv_status", {"title": "Fixture series"}), READER)
        assert obs.status == "clarify_required"
        assert len(state.calls) == 1
    fixture.series[1]["title"] = "Other series"
    with media_fixture(fixture, max_records=1) as (native, _):
        assert NativeMediaReadAdapter(native).execute(Call("tv_status", {"title": "Other series"}), READER).verified_complete


def test_catalog_response_budget_cannot_be_bypassed_by_an_early_matching_title():
    fixture = MediaFixtureState()
    fixture.series[0]["oversized"] = "x" * 1000
    with media_fixture(fixture, max_response_bytes=200) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("tv_status", {"title": "Fixture series"}), READER)
        assert obs.status == "error" and obs.data["error"] == "response_limit_exceeded"


def test_tv_missing_checks_aired_utc_monitoring_and_specials_without_global_wanted_route():
    with media_fixture() as (native, state):
        adapter = NativeMediaReadAdapter(native, clock=lambda: FIXED_NOW)
        base = adapter.execute(Call("tv_missing", {"title": "Fixture series"}), READER)
        assert base.verified_complete and base.data["missing_count"] == 1
        assert base.data["missing_scope"] == "known_aired_monitored"
        assert base.data["undated_episodes_excluded"] == 1
        assert [row["id"] for row in base.data["missing_episodes"]] == [1]
        specials = adapter.execute(Call("tv_missing", {"title": "Fixture series", "include_specials": True}), READER)
        assert [row["id"] for row in specials.data["missing_episodes"]] == [1, 6]
        assert all("wanted" not in row["path"] for row in state.calls)
        assert any(row["query"].get("seriesId") == ["1"] for row in state.calls)


@pytest.mark.parametrize("stamp", ["2020-01-01T00:00:00", "malformed", 1, ""])
def test_naive_or_malformed_episode_airdates_are_errors(stamp):
    fixture = MediaFixtureState(episodes=[_episode(1, airDateUtc=stamp)])
    with media_fixture(fixture) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("tv_missing", {"title": "Fixture series"}), READER)
        assert obs.status == "error" and not obs.verified_complete


def test_utc_offset_and_exact_airtime_boundary_are_compared_as_instants():
    fixture = MediaFixtureState(episodes=[_episode(1, airDateUtc="2026-01-01T01:00:00+01:00"),
                                          _episode(2, airDateUtc="2026-01-01T00:00:01Z")])
    with media_fixture(fixture) as (native, _):
        obs = NativeMediaReadAdapter(native, clock=lambda: FIXED_NOW).execute(Call("tv_missing", {"title": "Fixture series"}), READER)
        assert obs.verified_complete and [row["id"] for row in obs.data["missing_episodes"]] == [1]


def test_unmonitored_series_is_not_turned_into_a_monitored_missing_inventory():
    fixture = MediaFixtureState()
    fixture.series[0]["monitored"] = False
    with media_fixture(fixture) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("tv_missing", {"title": "Fixture series"}), READER)
        assert obs.verified_complete and obs.data["missing_count"] == 0 and obs.data["series_monitored"] is False


def test_missing_output_limit_is_partial_without_losing_the_full_count():
    fixture = MediaFixtureState(episodes=[_episode(1), _episode(2)])
    with media_fixture(fixture, max_records=1) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("tv_missing", {"title": "Fixture series"}), READER)
        assert obs.status == "partial" and not obs.verified_complete
        assert obs.data["missing_count"] == 2 and obs.data["returned_count"] == 1


def test_other_series_episodes_and_duplicate_episode_identity_are_rejected():
    for rows in ([_episode(1, seriesId=404)], [_episode(1), _episode(1)]):
        with media_fixture(MediaFixtureState(episodes=rows)) as (native, _):
            obs = NativeMediaReadAdapter(native).execute(Call("tv_missing", {"title": "Fixture series"}), READER)
            assert obs.status == "error" and obs.data["error"] == "inconsistent_series_episode_identity"


def test_movie_remakes_require_year_and_never_choose_a_different_year():
    fixture = MediaFixtureState()
    fixture.movies.append({**fixture.movies[0], "id": 3, "year": 1984})
    with media_fixture(fixture) as (native, _):
        adapter = NativeMediaReadAdapter(native)
        ambiguous = adapter.execute(Call("movie_status", {"title": "Fixture movie"}), READER)
        assert ambiguous.status == "clarify_required" and ambiguous.data["candidate_years"] == [1984, 2020]
        chosen = adapter.execute(Call("movie_status", {"title": "Fixture movie", "year": 2020}), READER)
        assert chosen.verified_complete and chosen.data["movie_id"] == 2
        assert adapter.execute(Call("movie_status", {"title": "Fixture movie", "year": 2019}), READER).status == "clarify_required"


def test_album_artist_filter_is_applied_before_album_uniqueness():
    with media_fixture() as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("music_status", {"artist": "Fixture artist", "album": "Fixture album"}), READER)
        assert obs.verified_complete and [row["id"] for row in obs.data["albums"]] == [5]
    fixture = MediaFixtureState()
    fixture.albums.append({**fixture.albums[0], "id": 7})
    with media_fixture(fixture) as (native, _):
        assert NativeMediaReadAdapter(native).execute(Call("music_status", {"artist": "Fixture artist", "album": "Fixture album"}), READER).status == "clarify_required"


def test_no_fuzzy_prefix_or_first_hit_entity_resolution():
    with media_fixture() as (native, _):
        adapter = NativeMediaReadAdapter(native)
        for call in (Call("tv_status", {"title": "Fixture"}), Call("movie_status", {"title": "Fixture"}),
                     Call("music_status", {"artist": "Fixture"}), Call("music_status", {"artist": "Fixture artist", "album": "Fixture"})):
            assert adapter.execute(call, READER).status == "clarify_required"


def test_watch_history_server_search_is_not_mistaken_for_exact_title_match():
    fixture = MediaFixtureState()
    fixture.history.append({"row_id": 2, "title": "Other movie", "full_title": "Other movie", "user": "Fixture series"})
    with media_fixture(fixture) as (native, state):
        obs = NativeMediaReadAdapter(native).execute(Call("watch_history", {"title": "Fixture series"}), READER)
        assert obs.verified_complete and obs.data["count"] == 1
        assert obs.data["upstream_total_records"] == 2
        assert [row["row_id"] for row in obs.data["history"]] == [1]
        assert any(row["query"].get("search") == ["Fixture series"] for row in state.calls)


def test_paginated_history_retains_partial_and_nonadvancing_page_status():
    fixture = MediaFixtureState(history=[{"row_id": i, "title": "Fixture series"} for i in range(1, 4)])
    with media_fixture(fixture, page_size=1, max_pages=2) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("watch_history", {"title": "Fixture series"}), READER)
        assert obs.status == "partial" and obs.data["total_records"] is None and obs.data["count"] == 2
    fixture.repeat_history_page = True
    with media_fixture(fixture, page_size=1) as (native, _):
        assert NativeMediaReadAdapter(native).execute(Call("watch_history", {}), READER).status == "partial"


@pytest.mark.parametrize("target", ["statistics", "hasFile"])
def test_missing_status_fields_never_become_false_zero_or_false_unavailable(target):
    fixture = MediaFixtureState()
    if target == "statistics":
        del fixture.series[0]["statistics"]
        call = Call("tv_status", {"title": "Fixture series"})
    else:
        del fixture.movies[0]["hasFile"]
        call = Call("movie_status", {"title": "Fixture movie"})
    with media_fixture(fixture) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(call, READER)
        assert obs.status == "error" and not obs.verified_complete


def test_unavailable_and_malformed_services_return_errors_without_raw_payloads():
    for fault in ((503, {"error": "do-not-leak-response"}), (200, {"not": "an array"})):
        fixture = MediaFixtureState(faults={"/api/v3/series": fault})
        with media_fixture(fixture) as (native, _):
            obs = NativeMediaReadAdapter(native).execute(Call("tv_status", {"title": "Fixture series"}), READER)
            assert obs.status == "error" and "do-not-leak-response" not in json.dumps(obs.data)


def test_native_observations_feed_the_existing_verified_per_caller_ledger():
    with media_fixture() as (native, _):
        ledger = SessionEntityLedger(READER.subject)
        ledger.advance_turn()
        executor = BoundedExecutor(NativeMediaReadAdapter(native, clock=lambda: FIXED_NOW))
        first = executor.execute_call(Call("tv_status", {"title": "Fixture series"}), READER,
                                      query='Is "Fixture series" in Sonarr?', ledger=ledger)
        assert first.terminal_state == "completed"
        assert ledger.to_model_context()["selected_media"]["title"] == "Fixture series"
        ledger.advance_turn()
        second = executor.execute_call(Call("tv_missing", {"title": "Fixture series"}), READER,
                                       query="Which episodes of that series are missing?", ledger=ledger)
        assert second.terminal_state == "completed" and second.observation.data["missing_count"] == 1


@pytest.mark.parametrize("query,call", READ_CASES)
def test_runtime_routes_existing_v2_reads_natively_without_any_legacy_gateway(query, call):
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    class ScriptedProposalModel:
        family = "hermes"
        calls = 0
        def infer(self, actual_query, *_args, **_kwargs):
            assert actual_query == query
            self.calls += 1
            return {"raw": encode(call, family="hermes")}
    class NoLegacyFallback:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("A configured native read must not call a private gateway")
    with media_fixture() as (native, state):
        model = ScriptedProposalModel()
        runtime = ArrmesRuntimeService(model=model, executor=BoundedExecutor(ConfiguredAdapterRouter(native, NoLegacyFallback())))
        result = runtime.execute(query, READER)
        assert model.calls == 1
        assert result["terminal_state"] == "completed"
        assert result["proposal"] == normalized(call)
        assert result["observation"]["data"]["source"] == "native_api"
        assert all(row["method"] == "GET" for row in state.calls)


def test_series_statistics_support_v3_top_level_and_nullable_legacy_fields():
    fixture = MediaFixtureState()
    fixture.series[0].update(episodeCount=None, episodeFileCount=None)
    with media_fixture(fixture) as (native, _):
        obs = NativeMediaReadAdapter(native).execute(Call("tv_status", {"title": "Fixture series"}), READER)
        assert obs.verified_complete and obs.data["episode_file_count"] == 1
    del fixture.series[0]["statistics"]
    fixture.series[0].update(episodeCount=3, episodeFileCount=1)
    with media_fixture(fixture) as (native, _):
        assert NativeMediaReadAdapter(native).execute(Call("tv_status", {"title": "Fixture series"}), READER).verified_complete


def test_duplicate_artist_name_clarifies_before_album_lookup():
    fixture = MediaFixtureState()
    fixture.artists.append({**fixture.artists[0], "id": 9})
    with media_fixture(fixture) as (native, state):
        obs = NativeMediaReadAdapter(native).execute(Call("music_status", {"artist": "Fixture artist", "album": "Fixture album"}), READER)
        assert obs.status == "clarify_required"
        assert [row["path"] for row in state.calls] == ["/api/v1/artist"]


def test_tautulli_application_errors_are_not_completed_history_or_activity():
    fixture = MediaFixtureState(faults={"/api/v2": (200, {"response": {"result": "error", "data": {}, "message": "do-not-leak-response"}})})
    with media_fixture(fixture) as (native, _):
        adapter = NativeMediaReadAdapter(native)
        for call in (Call("now_playing", {}), Call("watch_history", {}), Call("watch_history", {"title": "Fixture series"})):
            obs = adapter.execute(call, READER)
            assert obs.status == "error" and obs.data["error"] == "tautulli_application_error"
            assert "do-not-leak-response" not in json.dumps(obs.data)


def test_boolean_statistics_or_file_states_never_become_zero_or_unavailable():
    fixture = MediaFixtureState()
    fixture.series[0]["statistics"]["episodeCount"] = True
    fixture.movies[0]["hasFile"] = 1
    with media_fixture(fixture) as (native, _):
        adapter = NativeMediaReadAdapter(native)
        assert adapter.execute(Call("tv_status", {"title": "Fixture series"}), READER).status == "error"
        assert adapter.execute(Call("movie_status", {"title": "Fixture movie"}), READER).status == "error"
