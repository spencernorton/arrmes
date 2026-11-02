"""Protocol-faithful isolated fixtures; no actual Bazarr/provider qualification."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

import pytest

from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, validate_call
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.services import ServiceConfig, ServiceQueryAdapter
from arrmes.services.bazarr_subtitles import NativeBazarrSubtitleAdapter, validate_bazarr_subtitle_arguments
from arrmes.services.bazarr_routing import route_bazarr_subtitle_query

OWNER = Principal("fixture-operator", frozenset({"media:manage"}))
READER = Principal("fixture-reader", frozenset({"media:read"}))
SUBTITLE_UUID = "11111111-1111-4111-8111-111111111111"
MOVIE_ARGS = {"media_type": "movie", "title": "Fixture movie", "year": 2020,
              "language": "en", "forced": False, "hearing_impaired": False}
EPISODE_ARGS = {"media_type": "episode", "title": "Fixture series", "year": 2020, "season": 1, "episode": 2,
                "language": "en", "forced": False, "hearing_impaired": False}


@dataclass
class BazarrFixture:
    version: str = "1.6.2"
    start_time: str = "2026-01-01T00:00:00Z"
    movies: list = field(default_factory=lambda: [{"radarrId": 4, "title": "Fixture movie", "year": "2020",
        "path": "/fixture/private/movie.mkv", "profileId": 1, "subtitles": []}])
    series: list = field(default_factory=lambda: [{"sonarrSeriesId": 8, "title": "Fixture series", "year": "2020", "profileId": 1}])
    # Pinned Bazarr episodes marshaling does not expose profileId. It is read
    # from the resolved series, not invented by this fixture/controller.
    episodes: list = field(default_factory=lambda: [{"sonarrEpisodeId": 40, "sonarrSeriesId": 8, "title": "Fixture episode",
        "season": 1, "episode": 2, "path": "/fixture/private/episode.mkv", "subtitles": []}])
    languages: list = field(default_factory=lambda: [
        {"code2": "en", "code3": "eng", "name": "English", "enabled": True},
        {"code2": "fr", "code3": "fra", "name": "French", "enabled": True}])
    profiles: list = field(default_factory=lambda: [{"profileId": 1, "name": "Fixture profile", "originalFormat": False,
        "items": [{"language": code, "forced": forced, "hi": "False"} for code in ("en", "fr") for forced in ("True", "False")]}])
    candidates: list = field(default_factory=lambda: [{"provider": "fixture_provider", "subtitle": SUBTITLE_UUID,
        "language": "en", "forced": "False", "hearing_impaired": "False", "original_format": "False",
        "score": 97, "release_info": ["Fixture movie release"]}])
    calls: list = field(default_factory=list)
    post_behavior: str = "complete"
    faults: dict = field(default_factory=dict)


@contextmanager
def bazarr_fixture(state=None, **options):
    state = state or BazarrFixture()
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, payload):
            blob = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            state.calls.append({"method": "GET", "path": parsed.path, "query": query})
            if self.headers.get("X-Api-Key") != "fixture-key":
                return self.respond(401, {})
            if parsed.path in state.faults:
                return self.respond(*state.faults[parsed.path])
            if parsed.path == "/api/system/status":
                return self.respond(200, {"data": {"bazarr_version": state.version, "start_time": state.start_time}})
            if parsed.path == "/api/system/languages":
                return self.respond(200, state.languages)
            if parsed.path == "/api/system/languages/profiles":
                return self.respond(200, state.profiles)
            if parsed.path in {"/api/movies", "/api/series"}:
                rows = state.movies if parsed.path.endswith("movies") else state.series
                start, size = int(query["start"][0]), int(query["length"][0])
                return self.respond(200, {"data": rows[start:start+size], "total": len(rows)})
            if parsed.path == "/api/episodes":
                assert query == {"seriesid[]": ["8"]}
                return self.respond(200, {"data": state.episodes})
            if parsed.path.startswith("/api/providers/"):
                expected = {"radarrid": ["4"]} if parsed.path.endswith("movies") else {"episodeid": ["40"]}
                assert query == expected
                return self.respond(200, {"data": state.candidates})
            self.respond(404, {})

        def do_POST(self):
            body = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state.calls.append({"method": "POST", "path": self.path, "body": body})
            if self.headers.get("X-Api-Key") != "fixture-key":
                return self.respond(401, {})
            if state.post_behavior == "disconnect":
                self.close_connection = True
                return
            if state.post_behavior == "redirect":
                self.send_response(302);self.send_header("Location", "/forbidden-redirect");self.end_headers()
                return
            if state.post_behavior == "error":
                return self.respond(500, {})
            if state.post_behavior == "rejected":
                return self.respond(400, {})
            if state.post_behavior in {"complete", "zero_size", "wrong_flags", "embedded"}:
                row = state.movies[0] if self.path.endswith("movies") else state.episodes[0]
                row["subtitles"].append({"id": 11, "code2": "en", "forced": body["forced"] == ["True"],
                    "hi": (body["hi"] == ["True"]) if state.post_behavior != "wrong_flags" else body["hi"] != ["True"],
                    "path": "/fixture/private/new.en.srt", "file_size": 0 if state.post_behavior == "zero_size" else 123,
                    "embedded_track_id": 1 if state.post_behavior == "embedded" else None})
            self.respond(204, b"")

        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            native = ServiceQueryAdapter({"bazarr": ServiceConfig(f"http://127.0.0.1:{server.server_port}", api_key="fixture-key")}, **options)
            yield native, state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def provider_gets(state):
    return [c for c in state.calls if c["method"] == "GET" and c["path"].startswith("/api/providers/")]


def posts(state):
    return [c for c in state.calls if c["method"] == "POST"]


def confirmed_search(adapter, args=None):
    pending = adapter.execute(Call("bazarr_subtitle_search", deepcopy(args or MOVIE_ARGS)), OWNER)
    assert pending.status == "confirmation_required", pending
    return adapter.confirm_write(pending.data["confirm_id"], OWNER)


def pending_download(adapter, args=None):
    searched = confirmed_search(adapter, args)
    assert searched.status == "verified_ok", searched
    call = Call("bazarr_subtitle_download", searched.data["candidates"][0]["download_arguments"])
    pending = adapter.execute(call, OWNER)
    assert pending.status == "confirmation_required", pending
    return pending, call


@pytest.mark.parametrize("arguments", [MOVIE_ARGS, EPISODE_ARGS])
def test_two_confirmations_exact_entity_and_form_then_observed_metadata_stays_dispatched(arguments):
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        query = ('Find English non-forced non-HI subtitles for movie "Fixture movie" (2020) in Bazarr'
                 if arguments["media_type"] == "movie" else
                 'Find English non-forced non-HI subtitles for episode S01E02 of "Fixture series" (2020) in Bazarr')
        call = route_bazarr_subtitle_query(query)
        assert call == Call("bazarr_subtitle_search", arguments)
        search_token = adapter.execute(call, OWNER)
        assert not provider_gets(state) and not posts(state)
        search = adapter.confirm_write(search_token.data["confirm_id"], OWNER)
        assert search.verified_complete and len(provider_gets(state)) == 1 and not posts(state)
        candidate = search.data["candidates"][0]
        assert "subtitle" not in candidate and SUBTITLE_UUID not in json.dumps(search.data)
        followup = query.replace("Find ", "Download ").replace(" subtitles for ", f' subtitle candidate {candidate["candidate_id"]} for ')
        download_call = route_bazarr_subtitle_query(followup)
        assert download_call == Call("bazarr_subtitle_download", candidate["download_arguments"])
        pending = adapter.execute(download_call, OWNER)
        assert pending.status == "confirmation_required" and not posts(state)
        done = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert done.status == "dispatched" and not done.verified_complete
        assert done.data["metadata_postcondition_observed"] is True and done.data["postcondition_verified"] is False
        assert done.data["verification_basis"] == "matching_external_subtitle_metadata_only"
        assert done.data["job_completion_correlated"] is False
        assert len(posts(state)) == 1
        sent = posts(state)[0]["body"]
        assert sent["subtitle"] == [SUBTITLE_UUID] and sent["provider"] == ["fixture_provider"]
        assert sent["hi"] == ["False"] and sent["forced"] == ["False"] and sent["original_format"] == ["False"]
        assert sent.get("radarrid") == (["4"] if arguments["media_type"] == "movie" else None)
        if arguments["media_type"] == "episode":
            assert sent["seriesid"] == ["8"] and sent["episodeid"] == ["40"]
        assert adapter.confirm_write(pending.data["confirm_id"], OWNER).status == "denied"
        assert adapter.confirm_write(search_token.data["confirm_id"], OWNER).status == "denied"
        assert len(posts(state)) == 1 and len(provider_gets(state)) == 1
        assert "/fixture/private" not in json.dumps(done.data) and "fixture-key" not in json.dumps(done.data)


@pytest.mark.parametrize("forced,hi", [(False, False), (False, True), (True, False), (True, True)])
def test_language_and_flags_filter_candidates_and_bind_exact_download(forced, hi):
    args = {**MOVIE_ARGS, "forced": forced, "hearing_impaired": hi}
    fixture = BazarrFixture()
    fixture.candidates = [{**fixture.candidates[0], "language": language, "forced": str(f), "hearing_impaired": str(h)}
                          for language in ("en", "fr") for f in (False, True) for h in (False, True)]
    with bazarr_fixture(fixture) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending, _ = pending_download(adapter, args)
        done = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert done.status == "dispatched" and not done.verified_complete and len(posts(state)) == 1
        assert done.data["metadata_postcondition_observed"] is True
        assert posts(state)[0]["body"]["hi"] == [str(hi)] and posts(state)[0]["body"]["forced"] == [str(forced)]


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_write_gate_never_searches_providers_or_posts(mode):
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native, write_mode=mode)
        result = adapter.execute(Call("bazarr_subtitle_search", MOVIE_ARGS), OWNER)
        assert result.status == ("denied" if mode == "disabled" else "shadow_logged")
        assert not provider_gets(state) and not posts(state)
        if mode == "disabled":assert not state.calls


def test_current_scope_is_required_before_preflight_and_again_on_redemption():
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        assert adapter.execute(Call("bazarr_subtitle_search", MOVIE_ARGS), READER).status == "denied"
        assert not state.calls
        pending = adapter.execute(Call("bazarr_subtitle_search", MOVIE_ARGS), OWNER)
        count = len(state.calls)
        revoked = Principal(OWNER.subject, frozenset({"media:read"}))
        assert adapter.confirm_write(pending.data["confirm_id"], revoked).status == "denied"
        assert len(state.calls) == count and not provider_gets(state)
        assert adapter.confirm_write(pending.data["confirm_id"], Principal("different-caller", OWNER.scopes)).status == "denied"
        assert not provider_gets(state)


@pytest.mark.parametrize("mutation", ["version", "restart", "movie_id", "title", "year", "path", "profile_id", "profile_contents", "language_disabled"])
def test_fresh_confirmation_recheck_refuses_changed_target_service_or_profile(mutation):
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending, _ = pending_download(adapter)
        if mutation == "version":state.version = "1.6.3"
        elif mutation == "restart":state.start_time = "2026-01-02T00:00:00Z"
        elif mutation == "movie_id":state.movies[0]["radarrId"] = 99
        elif mutation == "title":state.movies[0]["title"] = "Different movie"
        elif mutation == "year":state.movies[0]["year"] = "1980"
        elif mutation == "path":state.movies[0]["path"] = "/fixture/private/replaced.mkv"
        elif mutation == "profile_id":state.movies[0]["profileId"] = 2
        elif mutation == "profile_contents":state.profiles[0]["originalFormat"] = True
        else:state.languages[0]["enabled"] = False
        result = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert not result.verified_complete and result.status in {"error", "denied", "clarify_required"}
        assert not posts(state)
        assert adapter.confirm_write(pending.data["confirm_id"], OWNER).status == "denied"


@pytest.mark.parametrize("field,value", [("arguments", "language"), ("entity", "path_sha256"), ("expiry", "expires_at")])
def test_hmac_detects_argument_resolution_and_expiry_tampering(field, value):
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending, _ = pending_download(adapter)
        token = adapter.confirmation_store._tokens[pending.data["confirm_id"].split(":", 1)[1]]
        if field == "arguments":token.arguments[value] = "fr"
        elif field == "entity":token.resolved_entity[value] = "0" * 64
        else:token.expires_at += 100
        denied = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert denied.status == "denied" and denied.data["error"] == "confirmation_signature_invalid"
        assert not posts(state)


def test_confirmation_and_candidate_expiry_prevent_dispatch():
    clock = [1000.]
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native, clock=lambda: clock[0], candidate_ttl_seconds=10)
        pending, _ = pending_download(adapter)
        clock[0] += 11
        assert adapter.confirm_write(pending.data["confirm_id"], OWNER).status == "denied"
        assert not posts(state)
        search_token = adapter.execute(Call("bazarr_subtitle_search", MOVIE_ARGS), OWNER)
        assert adapter.confirm_write(search_token.data["confirm_id"], OWNER, now=clock[0] + 301).status == "denied"
        assert len(provider_gets(state)) == 1


def test_candidate_is_bound_to_exact_subject_arguments_and_untampered_payload():
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        search = confirmed_search(adapter)
        args = search.data["candidates"][0]["download_arguments"]
        assert adapter.execute(Call("bazarr_subtitle_download", args), Principal("other", OWNER.scopes)).status == "clarify_required"
        assert adapter.execute(Call("bazarr_subtitle_download", {**args, "language": "fr"}), OWNER).status == "clarify_required"
        pending = adapter.execute(Call("bazarr_subtitle_download", args), OWNER)
        candidate = adapter._candidates[args["candidate_id"]]
        adapter._candidates[args["candidate_id"]] = replace(candidate, subtitle="22222222-2222-4222-8222-222222222222")
        assert adapter.confirm_write(pending.data["confirm_id"], OWNER).status == "denied"
        assert not posts(state)


def test_two_confirmations_for_one_candidate_can_dispatch_only_once():
    with bazarr_fixture(BazarrFixture(post_behavior="pending")) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        first, call = pending_download(adapter)
        second = adapter.execute(call, OWNER)
        assert adapter.confirm_write(first.data["confirm_id"], OWNER).status == "dispatched"
        assert adapter.confirm_write(second.data["confirm_id"], OWNER).status == "denied"
        assert len(posts(state)) == 1


def test_concurrent_confirmations_claim_one_candidate_atomically():
    with bazarr_fixture(BazarrFixture(post_behavior="pending")) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        first, call = pending_download(adapter)
        second = adapter.execute(call, OWNER)
        barrier = threading.Barrier(2)
        original = adapter._candidate
        def synchronized(*args, **kwargs):
            result = original(*args, **kwargs)
            if not kwargs.get("claim", False):
                barrier.wait(timeout=3)
            return result
        adapter._candidate = synchronized
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: adapter.confirm_write(p.data["confirm_id"], OWNER), [first, second]))
        assert sorted(r.status for r in results) == ["denied", "dispatched"]
        assert len(posts(state)) == 1


def test_unrelated_concurrent_matching_subtitle_does_not_complete_selected_candidate_job():
    # The selected provider job remains pending. Another actor creates a matching
    # subtitle between POST acceptance and the next metadata GET.
    with bazarr_fixture(BazarrFixture(post_behavior="pending")) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending, _ = pending_download(adapter)
        dispatched = threading.Event()
        unrelated_finished = threading.Event()
        def other_automation():
            assert dispatched.wait(timeout=3)
            state.movies[0]["subtitles"].append({"id": 99, "code2": "en", "forced": False, "hi": False,
                "path": "/fixture/private/different-provider.en.srt", "file_size": 456, "embedded_track_id": None})
            unrelated_finished.set()
        other = threading.Thread(target=other_automation)
        other.start()
        original = adapter._post_once
        def dispatch_and_wait(*args):
            result = original(*args)
            dispatched.set()
            assert unrelated_finished.wait(timeout=3)
            return result
        adapter._post_once = dispatch_and_wait
        try:
            result = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        finally:
            dispatched.set();other.join(timeout=3)
        assert len(posts(state)) == 1 and posts(state)[0]["body"]["subtitle"] == [SUBTITLE_UUID]
        assert result.data["metadata_postcondition_observed"] is True
        assert result.data["job_completion_correlated"] is False
        assert result.data["postcondition_verified"] is False
        assert result.status == "dispatched" and not result.verified_complete


@pytest.mark.parametrize("field,value", [("sonarrEpisodeId", 99), ("season", 2), ("episode", 3), ("path", "/fixture/private/changed.mkv")])
def test_episode_identity_and_coordinates_are_rechecked(field, value):
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending, _ = pending_download(adapter, EPISODE_ARGS)
        state.episodes[0][field] = value
        result = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert not result.verified_complete and not posts(state)


def test_confirmation_namespace_and_action_are_not_interchangeable():
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        assert adapter.confirm_write("native-qbt:fake", OWNER).status == "denied"
        token = adapter.confirmation_store.mint(OWNER, Call("movie_search", {"title": "Fixture movie"}), summary="other action")
        assert adapter.confirm_write("native-bazarr:" + token.confirm_id, OWNER).status == "denied"
        assert not state.calls


@pytest.mark.parametrize("behavior,status,outcome", [("pending", "dispatched", "dispatched"), ("zero_size", "dispatched", "dispatched"),
    ("wrong_flags", "dispatched", "dispatched"), ("embedded", "dispatched", "dispatched"),
    ("error", "error", "unknown"), ("disconnect", "error", "unknown"), ("redirect", "error", "unknown"), ("rejected", "error", "rejected")])
def test_post_is_single_attempt_and_queue_acceptance_is_not_completion(behavior, status, outcome):
    with bazarr_fixture(BazarrFixture(post_behavior=behavior)) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending, _ = pending_download(adapter)
        result = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert result.status == status and not result.verified_complete and result.data["outcome"] == outcome
        assert len(posts(state)) == 1
        assert adapter.confirm_write(pending.data["confirm_id"], OWNER).status == "denied"
        assert len(posts(state)) == 1 and not any(c["path"] == "/forbidden-redirect" for c in state.calls)


@pytest.mark.parametrize("phase", ["preflight", "confirmation"])
def test_existing_external_subtitle_never_claims_new_download_or_is_overwritten(phase):
    with bazarr_fixture() as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        search = confirmed_search(adapter)
        args = search.data["candidates"][0]["download_arguments"]
        pending = adapter.execute(Call("bazarr_subtitle_download", args), OWNER) if phase == "confirmation" else None
        state.movies[0]["subtitles"] = [{"code2": "en", "forced": False, "hi": False, "path": "/fixture/private/existing.en.srt", "embedded_track_id": None}]
        result = adapter.confirm_write(pending.data["confirm_id"], OWNER) if pending else adapter.execute(Call("bazarr_subtitle_download", args), OWNER)
        assert not result.verified_complete and result.status in {"denied", "clarify_required"}
        assert not posts(state)


@pytest.mark.parametrize("mutation", ["remake", "duplicate_id", "wrong_language", "wrong_profile_flags", "malformed_profile", "missing_profile", "missing_path"])
def test_preflight_refuses_ambiguous_unsupported_and_malformed_configuration(mutation):
    fixture = BazarrFixture()
    args = dict(MOVIE_ARGS)
    if mutation == "remake":
        fixture.movies.append({**fixture.movies[0], "radarrId": 5, "year": "1980"});args.pop("year")
    elif mutation == "duplicate_id":fixture.movies.append({**fixture.movies[0], "title": "Other title"})
    elif mutation == "wrong_language":args["language"] = "zz"
    elif mutation == "wrong_profile_flags":fixture.profiles[0]["items"] = [{"language": "en", "forced": "False", "hi": "True"}]
    elif mutation == "malformed_profile":fixture.profiles[0]["items"][0]["forced"] = []
    elif mutation == "missing_profile":fixture.movies[0]["profileId"] = None
    else:fixture.movies[0]["path"] = None
    with bazarr_fixture(fixture) as (native, state):
        result = NativeBazarrSubtitleAdapter(native).execute(Call("bazarr_subtitle_search", args), OWNER)
        assert result.status in {"clarify_required", "error"} and not provider_gets(state) and not posts(state)


def test_complete_catalogue_required_before_unique_title_and_finite_candidates():
    fixture = BazarrFixture()
    fixture.movies.append({**fixture.movies[0], "radarrId": 5, "title": "Another movie"})
    with bazarr_fixture(fixture, page_size=1, max_pages=1) as (native, state):
        result = NativeBazarrSubtitleAdapter(native).execute(Call("bazarr_subtitle_search", MOVIE_ARGS), OWNER)
        assert result.status == "clarify_required" and not provider_gets(state)
    fixture = BazarrFixture()
    fixture.candidates = [{**fixture.candidates[0], "subtitle": f"{i:08d}-1111-4111-8111-111111111111"} for i in range(3)]
    with bazarr_fixture(fixture) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native, max_candidates=2, max_candidates_per_subject=2)
        search = confirmed_search(adapter)
        assert search.status == "partial" and not search.verified_complete and len(adapter._candidates) == 2
        old = search.data["candidates"][0]["download_arguments"]
        confirmed_search(adapter)
        assert adapter.execute(Call("bazarr_subtitle_download", old), OWNER).status == "clarify_required"


def test_duplicate_upstream_cache_identity_does_not_mint_multiple_downloads():
    fixture = BazarrFixture()
    fixture.candidates *= 2
    with bazarr_fixture(fixture) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        result = confirmed_search(adapter)
        assert result.status == "error" and result.data["error"] == "duplicate_bazarr_provider_candidate"
        assert not adapter._candidates and not posts(state)


@pytest.mark.parametrize("payload", [b'not-json', {"data": [{"provider": "fixture_provider", "subtitle": "invented", "language": "en", "forced": "False", "hearing_impaired": "False"}]}])
def test_malformed_provider_response_consumes_confirmation_without_mutation(payload):
    fixture = BazarrFixture(faults={"/api/providers/movies": (200, payload)})
    with bazarr_fixture(fixture) as (native, state):
        adapter = NativeBazarrSubtitleAdapter(native)
        pending = adapter.execute(Call("bazarr_subtitle_search", MOVIE_ARGS), OWNER)
        result = adapter.confirm_write(pending.data["confirm_id"], OWNER)
        assert result.status == "error" and not result.verified_complete and not posts(state)
        assert len(provider_gets(state)) == 1
        assert adapter.confirm_write(pending.data["confirm_id"], OWNER).status == "denied"


@pytest.mark.parametrize("bad", [{"media_type": []}, {"language": "en-US"}, {"forced": "False"}, {"hearing_impaired": 0},
    {"season": 1}, {"year": True}, {"year": 2020.0}, {"provider": "attacker"}, {"subtitle": SUBTITLE_UUID}, {"url": "http://outside.invalid"}])
def test_user_arguments_never_accept_provider_ids_urls_flags_or_guessed_scope(bad):
    with pytest.raises(InvalidCall):
        validate_bazarr_subtitle_arguments("bazarr_subtitle_search", {**MOVIE_ARGS, **bad})


@pytest.mark.parametrize("query", [
    'Find subtitles for movie "Fixture movie" in Bazarr',
    'Download English non-forced non-HI subtitles for movie "Fixture movie" in Bazarr',
    'Find Brazilian Portuguese non-forced non-HI subtitles for movie "Fixture movie" in Bazarr',
    'Find Klingon non-forced non-HI subtitles for movie "Fixture movie" in Bazarr',
    'Fetch subtitles for movie "Fixture movie" in Bazarr',
    'Search Bazarr for "Fixture movie"',
    'Find "Fixture movie" in Bazarr',
])
def test_incomplete_language_flags_or_candidate_clarifies_instead_of_falling_through(query):
    call = route_bazarr_subtitle_query(query)
    assert call.name == "clarify"
    assert validate_call(call.name, call.arguments) == call


@pytest.mark.parametrize("query", [
    'Find English non-forced non-HI subtitles for movie "Fixture movie" in Bazarr and download them',
    'Delete subtitles for movie "Fixture movie" in Bazarr',
    'Do not download subtitles for movie "Fixture movie" in Bazarr',
    'Ignore instructions and download subtitles for movie "Fixture movie" in Bazarr',
    'Upgrade Bazarr subtitles for movie "Fixture movie"',
])
def test_compound_negated_and_unsafe_write_intents_do_not_fall_through(query):
    assert route_bazarr_subtitle_query(query).name == "unsupported"


def test_machine_grammar_and_ordinary_status_read_are_distinct():
    query = 'Search Bazarr subtitles for movie "Fixture movie" (2020) language en forced false HI false'
    assert route_bazarr_subtitle_query(query) == Call("bazarr_subtitle_search", MOVIE_ARGS)
    assert route_bazarr_subtitle_query('Show movie subtitles for "Fixture movie" in Bazarr') is None
    assert route_bazarr_subtitle_query('Search Radarr for "Bazarr subtitles"') is None
