"""Exercise real legacy transport/cache behavior at the confirmation boundary."""
from __future__ import annotations

from copy import deepcopy
import io
import json
import urllib.error
import urllib.parse

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.live_adapter import LiveGatewayAdapter
from arrmes.planner import Principal
from arrmes.protocol import Call
from arrmes.sandbox import NetworkIsolationGuard


WRITER = Principal("fixture-writer", frozenset({"media:read", "media:manage"}))
CASES = [
    ("sonarr", Call("tv_status", {"title": "Fixture series"}),
     Call("tv_search_missing", {"title": "Fixture series"}),
     'Download missing episodes of "Fixture series"'),
    ("radarr", Call("movie_status", {"title": "Fixture movie"}),
     Call("movie_search", {"title": "Fixture movie"}),
     'Search indexers for the movie "Fixture movie"'),
    ("lidarr", Call("music_status", {"artist": "Fixture artist"}),
     Call("music_search", {"artist": "Fixture artist"}),
     'Download missing music for artist "Fixture artist"'),
]


class _Response(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Upstream:
    """A controlled transport; _request_json and its cache remain unmocked."""

    def __init__(self):
        self.catalogs = {
            "/sonarr/api/v3/series": [{"id": 7, "title": "Fixture series", "year": 2020,
                "statistics": {"episodeCount": 2, "episodeFileCount": 0}, "monitored": True}],
            "/radarr/api/v3/movie": [{"id": 7, "title": "Fixture movie", "year": 2020,
                "hasFile": False, "monitored": True, "sizeOnDisk": 0}],
            "/lidarr/api/v1/artist": [{"id": 7, "artistName": "Fixture artist", "monitored": True}],
        }
        self.calls = []
        self.unavailable = False

    def open(self, request, *, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        body = json.loads(request.data) if request.data is not None else None
        self.calls.append({"method": request.get_method(), "path": path, "body": body})
        if self.unavailable:
            raise urllib.error.URLError("controlled fixture unavailable")
        if path in self.catalogs:
            return _Response(deepcopy(self.catalogs[path]))
        if path == "/lidarr/api/v1/album":
            return _Response([])
        if request.get_method() == "POST" and path.endswith("/command"):
            return _Response({"id": 99, "status": "queued"})
        if path.endswith("/command/99"):
            return _Response({"id": 99, "status": "completed"})
        raise AssertionError("Unexpected controlled upstream request")

    def catalog_reads(self, service):
        return [call for call in self.calls if call["method"] == "GET"
                and call["path"] in self.catalogs and call["path"].startswith("/" + service + "/")]

    @property
    def posts(self):
        return [call for call in self.calls if call["method"] == "POST"]


@pytest.fixture
def gateway(monkeypatch):
    upstream = _Upstream()
    monkeypatch.setattr("arrmes.live_adapter._open_same_origin", upstream.open)
    adapter = LiveGatewayAdapter("https://gateway.fixture.invalid", gateway_api_key="fixture-key",
                                 write_mode="confirm", max_retries=0)
    # Only the fake transport may respond. Accidental live connections fail.
    with NetworkIsolationGuard():
        yield adapter, upstream


def _stage_with_warm_read_cache(adapter, upstream, case):
    service, read, write, query = case
    executor = BoundedExecutor(adapter)
    for _ in range(2):
        assert adapter.execute(read, WRITER).verified_complete
    assert len(upstream.catalog_reads(service)) == 1
    pending = executor.execute_call(write, WRITER, query=query)
    assert pending.terminal_state == "awaiting_confirmation", pending
    assert len(upstream.catalog_reads(service)) == 1  # preflight uses the warm cache
    assert not upstream.posts
    return executor, pending.plan["confirm_id"]


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("change", ["id", "title", "reused_id"])
def test_confirmation_bypasses_warm_catalog_before_changed_entity_can_mutate(gateway, case, change):
    adapter, upstream = gateway
    executor, confirmation = _stage_with_warm_read_cache(adapter, upstream, case)
    service = case[0]
    rows = next(rows for path, rows in upstream.catalogs.items() if path.startswith("/" + service + "/"))
    original = deepcopy(rows[0])
    if change == "id":
        rows[0]["id"] = 8
    else:
        rows[0]["artistName" if service == "lidarr" else "title"] = "Fixture replacement"
        if change == "reused_id":
            rows.append({**original, "id": 8})
    result = executor.confirm_write(confirmation, WRITER)
    # An already-consumed confirmation whose target vanished fails rather
    # than creating another pending write; no dispatch is permitted.
    assert result.terminal_state in {"denied", "error"}
    assert not result.observation.verified_complete and not upstream.posts
    assert len(upstream.catalog_reads(service)) == 2
    assert not any(row.get("cached") for row in result.observation.audit_trail)
    replay = executor.confirm_write(confirmation, WRITER)
    assert replay.observation.data["error"] == "confirmation_already_consumed"
    assert len(upstream.catalog_reads(service)) == 2 and not upstream.posts


@pytest.mark.parametrize("case", CASES[:2])
def test_same_library_id_and_title_with_changed_year_requires_new_confirmation(gateway, case):
    adapter, upstream = gateway
    executor, confirmation = _stage_with_warm_read_cache(adapter, upstream, case)
    service = case[0]
    next(rows for path, rows in upstream.catalogs.items() if path.startswith("/" + service + "/"))[0]["year"] = 2021
    result = executor.confirm_write(confirmation, WRITER)
    assert result.terminal_state == "denied"
    assert result.observation.data["error"] == "confirmation_entity_changed"
    assert len(upstream.catalog_reads(service)) == 2 and not upstream.posts


@pytest.mark.parametrize("case", CASES)
def test_unchanged_entity_is_freshly_read_then_mutated_exactly_once(gateway, case):
    adapter, upstream = gateway
    executor, confirmation = _stage_with_warm_read_cache(adapter, upstream, case)
    result = executor.confirm_write(confirmation, WRITER)
    # The legacy Lidarr path reports queued searches as dispatched; Sonarr
    # and Radarr additionally poll the controlled completed command.
    assert result.terminal_state == ("dispatched" if case[0] == "lidarr" else "completed"), result
    assert len(upstream.catalog_reads(case[0])) == 2
    assert len(upstream.posts) == 1
    body = upstream.posts[0]["body"]
    assert body.get("seriesId", body.get("artistId", body.get("movieIds"))) in (7, [7])
    executor.confirm_write(confirmation, WRITER)
    assert len(upstream.posts) == 1


def test_revoked_scope_blocks_confirmation_before_cache_or_transport_access(gateway):
    adapter, upstream = gateway
    executor, confirmation = _stage_with_warm_read_cache(adapter, upstream, CASES[1])
    before = len(upstream.calls)
    result = executor.confirm_write(confirmation, Principal(WRITER.subject, frozenset({"media:read"})))
    assert result.terminal_state == "denied" and result.observation.data["error"] == "permission_required"
    assert len(upstream.calls) == before and not upstream.posts


def test_unavailable_current_catalog_cannot_be_replaced_by_preflight_cache(gateway):
    adapter, upstream = gateway
    executor, confirmation = _stage_with_warm_read_cache(adapter, upstream, CASES[1])
    upstream.unavailable = True
    result = executor.confirm_write(confirmation, WRITER)
    assert result.terminal_state == "error" and not result.observation.verified_complete
    assert len(upstream.catalog_reads("radarr")) == 2 and not upstream.posts
