"""Isolated Seerr HTTP fixtures; no real accounts, production APIs or model weights."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import threading
import urllib.parse

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.live_adapter import WriteConfirmationStore
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, encode, normalized
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.services import ServiceConfig, ServiceQueryAdapter
from arrmes.services.requests import NativeRequestAdapter, CONFIRMATION_PREFIX

VIEWER = Principal("fixture-viewer", frozenset({"requests:read", "requests:create"}))
MOVIE = Call("request_media", {"title": "Fixture movie", "media_type": "movie", "year": 2020})
STATUS = Call("request_status", {"title": "Fixture movie"})


def _request(identity=1, *, owner=7, tmdb=101, kind="movie", state=1, availability=2, **extra):
    return {"id": identity, "type": kind, "status": state, "is4k": False,
            "requestedBy": {"id": owner, "createdAt": "2020-01-01T00:00:00.000Z",
                            "email": "do-not-expose.invalid", "apiKey": "DO-NOT-EXPOSE"},
            "media": {"tmdbId": tmdb, "mediaType": kind, "status": availability,
                      "serviceUrl": "https://private.invalid", "serviceId": 998},
            "rootFolder": "/private/fixture/library", **extra}


@dataclass
class RequestFixture:
    search: list[dict] = field(default_factory=lambda: [{"id": 101, "mediaType": "movie", "title": "Fixture movie", "releaseDate": "2020-01-01"}])
    details: dict = field(default_factory=lambda: {("movie", 101): {"id": 101, "title": "Fixture movie", "releaseDate": "2020-01-01", "mediaInfo": None}})
    requests: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    version: str = "3.5.1"
    search_size: int = 20
    identity_override: int | None = None
    identity_sequence: list[int] = field(default_factory=list)
    identity_created_at: str = "2020-01-01T00:00:00.000Z"
    identity_created_sequence: list[object] = field(default_factory=list)
    identity_email: str = "DO-NOT-EXPOSE"
    ignore_owner_filter: bool = False
    repeated_request_page: bool = False
    faults: dict = field(default_factory=dict)
    post_status: int = 201
    post_transform: object = None
    after_transform: object = None
    after_status: int = 200
    posted: bool = False


@contextmanager
def request_fixture(state=None, *, mapping=None, mode="confirm", store=None, **native_options):
    state = state or RequestFixture()
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data, headers=None):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items(): self.send_header(key, value)
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass

        def identity(self, method):
            parsed = urllib.parse.urlsplit(self.path)
            call = {"method": method, "path": parsed.path, "query": urllib.parse.parse_qs(parsed.query), "raw_query": parsed.query, "headers": {key.lower(): value for key, value in self.headers.items()}}
            state.calls.append(call)
            if self.headers.get("X-Api-Key") != "fixture-key":
                self.respond(401, {})
                return None, call
            # Mirror upstream's unsafe admin default if the header is absent.
            return int(self.headers.get("X-API-User", "1")), call

        def do_GET(self):
            user, call = self.identity("GET")
            if user is None: return
            path, query = call["path"], call["query"]
            if path in state.faults:
                self.respond(*state.faults[path]);return
            if path == "/api/v1/auth/me":
                actual = state.identity_sequence.pop(0) if state.identity_sequence else state.identity_override or user
                created = state.identity_created_sequence.pop(0) if state.identity_created_sequence else state.identity_created_at
                self.respond(200, {"id": actual, "createdAt": created, "email": state.identity_email, "permissions": 999});return
            if path == "/api/v1/status":
                self.respond(200, {"version": state.version, "path": "/private/fixture/config"});return
            if path == "/api/v1/search":
                if "+" in call["raw_query"]:
                    # Seerr 3.5.1 rejects reserved characters, including "+" for a space.
                    self.respond(400, {"message": "Parameter 'query' must be url encoded. Its value may not contain reserved characters."});return
                page = int(query["page"][0]);start = (page - 1) * state.search_size
                self.respond(200, {"page": page, "totalPages": math.ceil(len(state.search) / state.search_size),
                    "totalResults": len(state.search), "results": deepcopy(state.search[start:start + state.search_size])});return
            if path in {f"/api/v1/{kind}/{identity}" for kind, identity in state.details}:
                kind, identity = path.split("/")[-2:]
                self.respond(200, deepcopy(state.details[(kind, int(identity))]));return
            if path == "/api/v1/request":
                assert query["requestedBy"] == [str(user)]
                rows = state.requests if state.ignore_owner_filter else [row for row in state.requests if row["requestedBy"]["id"] == user]
                take = int(query["take"][0]);skip = 0 if state.repeated_request_page else int(query["skip"][0])
                self.respond(200, {"pageInfo": {"results": len(rows), "pageSize": take, "page": int(query["skip"][0]) // take + 1,
                    "pages": math.ceil(len(rows) / take)}, "results": deepcopy(rows[skip:skip + take]),
                    "serviceErrors": {"radarr": [{"name": "DO-NOT-EXPOSE"}]}});return
            if path.startswith("/api/v1/request/"):
                row = next((row for row in state.requests if row["id"] == int(path.split("/")[-1])), None)
                payload = deepcopy(row)
                if state.after_transform: payload = state.after_transform(payload)
                self.respond(state.after_status, payload);return
            self.respond(404, {})

        def do_POST(self):
            user, call = self.identity("POST")
            if user is None: return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            call["body"] = payload
            assert call["path"] == "/api/v1/request" and set(payload) == {"mediaType", "mediaId"}
            row = _request(max([r["id"] for r in state.requests] + [0]) + 1, owner=user, tmdb=payload["mediaId"], kind=payload["mediaType"])
            if state.post_transform: row = state.post_transform(row)
            if 400 <= state.post_status < 500:
                self.respond(state.post_status, {"error": "DO-NOT-EXPOSE"});return
            state.requests.append(deepcopy(row));state.posted = True
            self.respond(state.post_status, row)

        def log_message(self, *_args): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True);thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            config = ServiceConfig(f"http://127.0.0.1:{server.server_port}", api_key="fixture-key",
                                   user_ids={VIEWER.subject: 7} if mapping is None else mapping)
            native = ServiceQueryAdapter({"seerr": config}, **native_options)
            yield NativeRequestAdapter(native, write_mode=mode, confirmation_store=store), state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def _pending(adapter):
    executor = BoundedExecutor(adapter)
    pending = executor.execute_raw_proposal(encode(MOVIE, "hermes"), VIEWER, family="hermes",
                                          query='Request the movie "Fixture movie" (2020)')
    assert pending.terminal_state == "awaiting_confirmation", pending
    return executor, pending.plan["confirm_id"]


def _posts(state): return [call for call in state.calls if call["method"] == "POST"]


def test_nl_scripted_proposal_executor_native_http_confirmation_and_postcondition():
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter)
        assert confirmation.startswith(CONFIRMATION_PREFIX) and not _posts(state)
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "completed" and result.proposal == normalized(MOVIE)
        assert result.observation.data["completion_scope"] == "request_submission_not_download"
        assert result.observation.data["request"]["request_status"] == "pending"
        assert len(_posts(state)) == 1 and _posts(state)[0]["body"] == {"mediaType": "movie", "mediaId": 101}
        assert state.calls[-1]["path"] == "/api/v1/request/1"
        assert all(call["headers"].get("x-api-user") == "7" and call["headers"].get("x-api-key") == "fixture-key" for call in state.calls)
        rendered = json.dumps(result.observation.data)
        assert "DO-NOT-EXPOSE" not in rendered and "/private" not in rendered and "fixture-key" not in rendered
        assert executor.confirm_write(confirmation, VIEWER).observation.data["error"] == "confirmation_already_consumed"
        assert len(_posts(state)) == 1


def test_request_status_returns_only_exact_target_caller_owned_records_and_no_foreign_count():
    state = RequestFixture(requests=[_request(owner=7), _request(2, owner=8), _request(3, owner=8)])
    with request_fixture(state) as (adapter, _):
        trace = BoundedExecutor(adapter).execute_raw_proposal(encode(STATUS, "hermes"), VIEWER, family="hermes",
                                                           query='What is my request status for "Fixture movie"?')
        assert trace.terminal_state == "completed"
        data = trace.observation.data
        assert data["own_request_count"] == 1 and [row["request_id"] for row in data["requests"]] == [1]
        assert data["status_scope"] == "caller_owned_requests_only" and not _posts(state)
        assert "requestedBy" not in json.dumps(data) and "serviceErrors" not in data


@pytest.mark.parametrize("call", [STATUS, MOVIE])
def test_missing_mapping_or_current_scope_never_contacts_upstream_or_uses_admin(call):
    with request_fixture(mapping={}) as (adapter, state):
        assert adapter.supports(call.name)
        obs = adapter.execute(call, VIEWER)
        assert obs.status == "denied" and obs.data["error"] == "native_request_user_mapping_required" and not state.calls
    with request_fixture() as (adapter, state):
        assert adapter.execute(call, Principal(VIEWER.subject, frozenset())).status == "denied" and not state.calls


@pytest.mark.parametrize("call", [STATUS, MOVIE])
def test_dropped_identity_header_detects_upstream_admin_default_before_other_endpoints(call):
    with request_fixture(RequestFixture(identity_override=1)) as (adapter, state):
        obs = adapter.execute(call, VIEWER)
        assert obs.status == "denied" and obs.data["error"] == "native_request_caller_identity_mismatch"
        assert len(state.calls) == 1 and state.calls[0]["path"] == "/api/v1/auth/me" and not _posts(state)


def test_identity_is_rechecked_immediately_before_post():
    state = RequestFixture(identity_sequence=[7, 7, 1])
    with request_fixture(state) as (adapter, _):
        executor, confirmation = _pending(adapter)
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "denied" and not _posts(state)
        assert result.observation.data["error"] == "native_request_caller_identity_mismatch"


def test_foreign_record_from_ignored_owner_filter_never_leaks_records_or_total_count():
    state = RequestFixture(requests=[_request(owner=7), _request(999, owner=8)], ignore_owner_filter=True)
    with request_fixture(state) as (adapter, _):
        obs = adapter.execute(STATUS, VIEWER)
        assert obs.status == "denied" and obs.data == {"source": "native_seerr", "error": "foreign_request_in_response"}
        assert not _posts(state)


def test_remakes_media_types_and_multiple_exact_results_require_clarification():
    for extra in ({"id": 102, "mediaType": "movie", "title": "Fixture movie", "releaseDate": "1984-01-01"},
                  {"id": 103, "mediaType": "tv", "name": "Fixture movie", "firstAirDate": "2020-01-01"}):
        state = RequestFixture();state.search.append(extra)
        with request_fixture(state) as (adapter, _):
            assert adapter.execute(STATUS, VIEWER).status == "clarify_required"
            # Explicit movie/type/year remains uniquely resolved.
            assert adapter.execute(MOVIE, VIEWER).status == "confirmation_required"
            assert not _posts(state)


def test_no_fuzzy_first_hit_or_wrong_year_fallback():
    with request_fixture() as (adapter, state):
        for call in (Call("request_media", {"title": "Fixture", "media_type": "movie"}),
                     Call("request_media", {"title": "Fixture movie", "media_type": "movie", "year": 2019})):
            assert adapter.execute(call, VIEWER).status == "clarify_required"
        assert not _posts(state)


def test_metadata_page_budget_cannot_hide_a_later_ambiguous_match():
    state = RequestFixture(search_size=1);state.search.append({**state.search[0], "id": 102})
    with request_fixture(state, max_pages=1) as (adapter, _):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.status == "clarify_required" and obs.data["reason"] == "metadata_lookup_incomplete"
        assert not _posts(state)


def test_request_inventory_page_cap_or_nonadvancing_results_never_mint_confirmation():
    for repeated, max_pages in ((False, 1), (True, 5)):
        state = RequestFixture(requests=[_request(1, tmdb=800), _request(2, tmdb=801)], repeated_request_page=repeated)
        with request_fixture(state, page_size=1, max_pages=max_pages) as (adapter, _):
            obs = adapter.execute(MOVIE, VIEWER)
            assert obs.status == "clarify_required" and obs.data["reason"] == "own_request_inventory_incomplete"
            assert not _posts(state)


@pytest.mark.parametrize("change", ["media_id", "year", "version"])
def test_confirm_freshly_rebinds_provider_identity_year_and_service_version(change):
    with request_fixture() as (adapter, state):
        # Omit year so the fresh year is matched then rejected by token binding.
        call = Call("request_media", {"title": "Fixture movie", "media_type": "movie"})
        pending = adapter.execute(call, VIEWER);assert pending.status == "confirmation_required"
        if change == "version": state.version = "3.5.2"
        elif change == "year":
            state.search[0]["releaseDate"] = "2022-01-01";state.details[("movie", 101)]["releaseDate"] = "2022-01-01"
        else:
            state.search[0]["id"] = 202;state.details[("movie", 202)] = {**state.details[("movie", 101)], "id": 202}
        result = adapter.confirm_write(pending.data["confirm_id"], VIEWER)
        assert result.status == "denied" and result.data["error"] == "confirmation_entity_changed" and not _posts(state)


def test_changed_user_mapping_or_revoked_scope_prevents_any_confirmation_network_access():
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter);before = len(state.calls)
        revoked = Principal(VIEWER.subject, frozenset({"requests:read"}))
        assert executor.confirm_write(confirmation, revoked).terminal_state == "denied" and len(state.calls) == before
        cfg = adapter.native.configs["seerr"]
        adapter.native.configs["seerr"] = ServiceConfig(cfg.base_url, api_key=cfg.api_key, user_ids={VIEWER.subject: 8})
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "denied" and result.observation.data["error"] == "confirmation_user_mapping_changed"
        assert len(state.calls) == before and not _posts(state)


@pytest.mark.parametrize("tamper", ["arguments", "resolved_entity"])
def test_signed_arguments_and_bound_media_cannot_be_changed(tamper):
    store = WriteConfirmationStore(clock=lambda: 100)
    with request_fixture(store=store) as (adapter, state):
        _, confirmation = _pending(adapter)
        token = store._tokens[confirmation[len(CONFIRMATION_PREFIX):]]
        getattr(token, tamper)["title"] = "Other movie"
        assert adapter.confirm_write(confirmation, VIEWER).data["error"] == "confirmation_signature_invalid"
        assert not _posts(state)


def test_expiry_subject_binding_and_single_use_fail_without_any_post():
    store = WriteConfirmationStore(clock=lambda: 100, ttl_seconds=10)
    with request_fixture(store=store) as (adapter, state):
        _, confirmation = _pending(adapter)
        other = Principal("fixture-other", VIEWER.scopes)
        assert adapter.confirm_write(confirmation, other, now=101).data["error"] == "confirmation_subject_mismatch"
        assert adapter.confirm_write(confirmation, VIEWER, now=110).data["error"] == "confirmation_expired"
        assert not _posts(state)


def test_own_existing_request_noop_and_new_duplicate_between_preview_and_confirm():
    with request_fixture(RequestFixture(requests=[_request()])) as (adapter, state):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.verified_complete and obs.data["already_requested"] and not obs.data["changed"] and not _posts(state)
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter);state.requests.append(_request())
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "completed" and result.observation.data["changed"] is False and not _posts(state)


@pytest.mark.parametrize("status", [3, 4])
def test_declined_or_failed_existing_request_never_silently_retries_or_claims_success(status):
    with request_fixture(RequestFixture(requests=[_request(state=status)])) as (adapter, state):
        assert adapter.execute(MOVIE, VIEWER).status == "clarify_required" and not _posts(state)


@pytest.mark.parametrize("fault", ["owner", "media", "type", "quality", "status", "request_id"])
def test_postcondition_identity_and_state_must_be_verified_before_completed(fault):
    def corrupt(row):
        if fault == "owner": row["requestedBy"]["id"] = 8
        elif fault == "media": row["media"]["tmdbId"] = 999
        elif fault == "type": row["type"] = "tv";row["media"]["mediaType"] = "tv"
        elif fault == "quality": row["is4k"] = True
        elif fault == "status": row["status"] = 0
        else: row["id"] = 999
        return row
    with request_fixture(RequestFixture(after_transform=corrupt)) as (adapter, state):
        executor, confirmation = _pending(adapter);result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "dispatched" and not result.observation.verified_complete
        assert result.observation.data["outcome"] == "unknown" and len(_posts(state)) == 1


@pytest.mark.parametrize("post_status,after_status", [(503, 200), (201, 503)])
def test_uncertain_mutation_or_unavailable_postcondition_is_never_retried(post_status, after_status):
    with request_fixture(RequestFixture(post_status=post_status, after_status=after_status)) as (adapter, state):
        executor, confirmation = _pending(adapter);result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state in {"error", "dispatched"} and result.observation.data["outcome"] == "unknown"
        assert result.observation.data["replayed"] is False and len(_posts(state)) == 1
        executor.confirm_write(confirmation, VIEWER);assert len(_posts(state)) == 1


@pytest.mark.parametrize("mode", ["shadow", "disabled"])
def test_shadow_and_disabled_modes_never_post(mode):
    with request_fixture(mode=mode) as (adapter, state):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.status == ("shadow_logged" if mode == "shadow" else "denied") and not _posts(state)


def test_tv_reads_work_but_tv_creation_is_explicitly_unqualified():
    state = RequestFixture(search=[{"id": 201, "mediaType": "tv", "name": "Fixture series", "firstAirDate": "2021-01-01"}],
        details={("tv", 201): {"id": 201, "name": "Fixture series", "firstAirDate": "2021-01-01", "mediaInfo": None}},
        requests=[_request(kind="tv", tmdb=201)])
    with request_fixture(state) as (adapter, _):
        obs = adapter.execute(Call("request_status", {"title": "Fixture series"}), VIEWER)
        assert obs.verified_complete and obs.data["media_type"] == "tv" and obs.data["own_request_count"] == 1
        before = len(state.calls)
        obs = adapter.execute(Call("request_media", {"title": "Fixture series", "media_type": "tv"}), VIEWER)
        assert obs.status == "unsupported" and len(state.calls) == before and not _posts(state)


@pytest.mark.parametrize("fault", [
    (401, {"error": "DO-NOT-EXPOSE"}), (503, {"error": "DO-NOT-EXPOSE"}),
    (200, b'{"id":7,"id":1}'), (200, b'not-json'), (200, {"id": True}),
    (307, {}, {"Location": "http://127.0.0.1:9/redirect"}),
])
def test_auth_unavailable_redirect_and_malformed_responses_fail_before_metadata_or_writes(fault):
    with request_fixture(RequestFixture(faults={"/api/v1/auth/me": fault})) as (adapter, state):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.status in {"error", "denied"} and len(state.calls) == 1 and not _posts(state)
        assert "DO-NOT-EXPOSE" not in json.dumps(obs.data)


def test_response_budget_and_model_arguments_cannot_inject_identity_or_overrides():
    with request_fixture(max_response_bytes=12) as (adapter, state):
        assert adapter.execute(MOVIE, VIEWER).data["error"] == "response_limit_exceeded" and not _posts(state)
    with request_fixture() as (adapter, state):
        for extra in ({"userId": 1}, {"ignoreQuota": True}, {"serverId": 1}, {"mediaId": 999}, {"url": "https://other.invalid"}):
            with pytest.raises(InvalidCall): adapter.execute(Call("request_media", {**MOVIE.arguments, **extra}), VIEWER)
        assert not state.calls


@pytest.mark.parametrize("field,value", [("mediaType", []), ("id", True), ("title", "x" * 2049),
    ("releaseDate", "2020-99-99"), ("releaseDate", {})])
def test_malformed_metadata_is_a_structured_error_not_a_guessed_target(field, value):
    state = RequestFixture();state.search[0][field] = value
    with request_fixture(state) as (adapter, _):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.status == "error" and not obs.verified_complete and not _posts(state)


@pytest.mark.parametrize("field,value", [("id", True), ("status", 0), ("status", True), ("type", {}), ("is4k", 1)])
def test_malformed_own_request_records_never_count_as_submitted(field, value):
    row = _request();row[field] = value
    with request_fixture(RequestFixture(requests=[row])) as (adapter, state):
        obs = adapter.execute(STATUS, VIEWER)
        assert obs.status == "error" and not obs.verified_complete and not _posts(state)


def test_wrong_owner_in_post_response_is_unknown_and_never_used_for_a_followup_lookup():
    def wrong_owner(row): row["requestedBy"]["id"] = 8;return row
    with request_fixture(RequestFixture(post_transform=wrong_owner)) as (adapter, state):
        executor, confirmation = _pending(adapter);result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "error" and result.observation.data["outcome"] == "unknown"
        assert not any(call["path"].startswith("/api/v1/request/") for call in state.calls)
        assert "requestedBy" not in json.dumps(result.observation.data) and len(_posts(state)) == 1


@pytest.mark.parametrize("code", [403, 409])
def test_permission_quota_or_duplicate_rejection_is_not_retried_or_hidden_as_success(code):
    with request_fixture(RequestFixture(post_status=code)) as (adapter, state):
        executor, confirmation = _pending(adapter);result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "error" and result.observation.data["outcome"] == "rejected"
        assert result.observation.data["error"] == "upstream_request_rejected" and len(_posts(state)) == 1
        assert state.requests == []
        executor.confirm_write(confirmation, VIEWER);assert len(_posts(state)) == 1


def test_changed_api_root_requires_reconfirmation_before_contacting_another_server():
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter);before = len(state.calls)
        cfg = adapter.native.configs["seerr"]
        adapter.native.configs["seerr"] = ServiceConfig(cfg.base_url + "/other", api_key=cfg.api_key, user_ids=cfg.user_ids)
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "denied" and result.observation.data["error"] == "confirmation_service_changed"
        assert len(state.calls) == before and not _posts(state)


def test_two_concurrent_confirmations_for_the_same_movie_dispatch_only_one_post():
    from concurrent.futures import ThreadPoolExecutor
    with request_fixture() as (adapter, state):
        executor, first = _pending(adapter);_, second = _pending(adapter)
        barrier = threading.Barrier(2)
        def confirm(token):
            barrier.wait()
            return executor.confirm_write(token, VIEWER)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, [first, second]))
        assert all(result.terminal_state == "completed" for result in results)
        assert len(_posts(state)) == 1
        assert sorted(result.observation.data["changed"] for result in results) == [False, True]


def test_person_and_collection_search_results_cannot_become_a_movie_request_target():
    state = RequestFixture()
    state.search.extend([{"id": 301, "mediaType": "person", "name": "Fixture movie"},
                         {"id": 302, "mediaType": "collection", "title": "Fixture movie"}])
    with request_fixture(state) as (adapter, _):
        _, confirmation = _pending(adapter)
        assert adapter.confirm_write(confirmation, VIEWER).verified_complete
        assert _posts(state)[0]["body"] == {"mediaType": "movie", "mediaId": 101}


def test_reused_numeric_user_id_is_rejected_before_reading_the_replacement_accounts_data():
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter)
        before = len(state.calls)
        state.identity_created_at = "2026-11-02T00:00:00.000Z"
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "denied"
        assert result.observation.data["error"] == "confirmation_account_identity_changed"
        assert [call["path"] for call in state.calls[before:]] == ["/api/v1/auth/me"]
        assert not _posts(state)


def test_replacement_account_on_the_final_auth_me_check_cannot_receive_the_post():
    state = RequestFixture(identity_created_sequence=["2020-01-01T00:00:00.000Z",
        "2020-01-01T00:00:00.000Z", "2026-11-02T00:00:00.000Z"])
    with request_fixture(state) as (adapter, _):
        executor, confirmation = _pending(adapter)
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "denied" and not _posts(state)
        assert result.observation.data["error"] == "confirmation_account_identity_changed"
        assert state.calls[-1]["path"] == "/api/v1/auth/me"


@pytest.mark.parametrize("created", [None, "", "not-a-date", "2020-01-01T00:00:00", True, 1])
def test_missing_or_invalid_account_creation_timestamp_fails_before_metadata(created):
    state = RequestFixture(identity_created_sequence=[created])
    with request_fixture(state) as (adapter, _):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.status == "denied" and obs.data["error"] == "native_request_account_identity_unavailable"
        assert len(state.calls) == 1 and not _posts(state)


def test_normalized_account_instant_is_stable_and_preview_exposes_only_a_fingerprint():
    state = RequestFixture(identity_email="fixture.viewer@example.invalid",
        identity_created_sequence=["2020-01-01T00:00:00.000Z", "2020-01-01T01:00:00+01:00", "2020-01-01T00:00:00Z"])
    with request_fixture(state) as (adapter, _):
        pending = adapter.execute(MOVIE, VIEWER)
        entity = pending.data["resolved_entity"]
        assert len(entity["user_identity_fingerprint"]) == 64
        assert "fixture.viewer@example.invalid" not in json.dumps(pending.data)
        assert "createdAt" not in entity and "email" not in entity
        assert adapter.confirm_write(pending.data["confirm_id"], VIEWER).verified_complete
        assert len(_posts(state)) == 1


def test_changed_email_requires_new_confirmation_without_exposing_either_value():
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter)
        state.identity_email = "replacement.viewer@example.invalid"
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "denied" and not _posts(state)
        assert "replacement.viewer@example.invalid" not in json.dumps(result.observation.data)


@pytest.mark.parametrize("version", ["1.34.0", "2.7.3", "4.0.0", "Overseerr 1.34.0", "Jellyseerr 2.7.3", "develop", "3.5.1-other invalid"])
def test_unknown_or_other_product_version_families_cannot_resolve_or_submit(version):
    with request_fixture(RequestFixture(version=version)) as (adapter, state):
        obs = adapter.execute(MOVIE, VIEWER)
        assert obs.status == "error" and obs.http_status == 501 and obs.data["error"] == "unsupported_seerr_version"
        assert [call["path"] for call in state.calls] == ["/api/v1/auth/me", "/api/v1/status"]
        assert not _posts(state)


def test_version_family_change_after_preview_cannot_mutate():
    with request_fixture() as (adapter, state):
        executor, confirmation = _pending(adapter)
        state.version = "2.7.3"
        result = executor.confirm_write(confirmation, VIEWER)
        assert result.terminal_state == "error" and result.observation.data["error"] == "unsupported_seerr_version"
        assert not _posts(state)


@pytest.mark.parametrize("version", ["v3.5.1", "3.6.0-beta.1", "3.5.1+build.1"])
def test_implemented_three_x_version_syntax_still_requires_exact_bound_version(version):
    with request_fixture(RequestFixture(version=version)) as (adapter, state):
        executor, confirmation = _pending(adapter)
        assert executor.confirm_write(confirmation, VIEWER).terminal_state == "completed"
        assert len(_posts(state)) == 1
