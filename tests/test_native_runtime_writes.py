"""Natural language must cross the runtime, policy, confirmation and native HTTP."""
from copy import deepcopy
import json
import pytest

from arrmes.adapters import AdapterObservation
from arrmes.executor import BoundedExecutor, derive_adapter_plan_state
from arrmes.planner import Principal, verify_proposal
from arrmes.protocol import Call
from arrmes.server import ArrmesRuntimeService
from arrmes.service_router import ConfiguredAdapterRouter
from arrmes.services.request_routing import route_native_request_query
from test_bazarr_subtitles import bazarr_fixture, OWNER, READER, provider_gets, posts
from test_native_requests import request_fixture, VIEWER


class NeverInfer:
    family = "hermes"
    backend = "fixture"
    def infer(self, *args, **kwargs):
        raise AssertionError("Explicit native workflow unexpectedly invoked a model")


def runtime(native, *, writes=True):
    return ArrmesRuntimeService(model=NeverInfer(),
        executor=BoundedExecutor(ConfiguredAdapterRouter(native), allow_writes=writes), write_mode="confirm")


def test_bazarr_natural_language_two_confirmations_and_uncorrelated_dispatch():
    with bazarr_fixture() as (native, state):
        service = runtime(native)
        query = 'Find English non-forced non-HI subtitles for movie "Fixture movie" (2020) in Bazarr'
        preview = service.route(query)
        assert preview["routing_source"] == "explicit_bazarr_deterministic"
        assert not state.calls
        search = service.execute(query, OWNER)
        assert search["terminal_state"] == "awaiting_confirmation"
        assert not provider_gets(state) and not posts(state)
        searched = service.confirm(search["plan"]["confirm_id"], OWNER)
        assert searched["terminal_state"] == "completed"
        candidate = searched["observation"]["data"]["candidates"][0]
        followup = query.replace("Find ", "Download ").replace(" subtitles for ",
            ' subtitle candidate ' + candidate["candidate_id"] + ' for ')
        download = service.execute(followup, OWNER)
        assert download["terminal_state"] == "awaiting_confirmation" and not posts(state)
        dispatched = service.confirm(download["plan"]["confirm_id"], OWNER)
        assert dispatched["terminal_state"] == "dispatched"
        assert dispatched["observation"]["data"]["metadata_postcondition_observed"]
        assert dispatched["observation"]["data"]["job_completion_correlated"] is False
        assert len(posts(state)) == 1
        assert service.confirm(download["plan"]["confirm_id"], OWNER)["terminal_state"] == "denied"
        assert len(posts(state)) == 1


def test_bazarr_scope_write_gate_and_argument_grounding_prevent_all_dispatch():
    with bazarr_fixture() as (native, state):
        query = 'Find English non-forced non-HI subtitles for movie "Fixture movie" (2020) in Bazarr'
        assert runtime(native).execute(query, READER)["terminal_state"] == "denied"
        assert runtime(native, writes=False).execute(query, OWNER)["terminal_state"] == "dry_run_planned"
        call = Call("bazarr_subtitle_search", {"media_type": "movie", "title": "Another movie", "year": 2020,
            "language": "en", "forced": False, "hearing_impaired": False})
        trace = runtime(native).executor.execute_call(call, OWNER, query=query)
        assert trace.terminal_state == "clarify"
        assert not state.calls


def test_native_request_explicit_route_reaches_caller_owned_controller():
    with request_fixture() as (adapter, state):
        service = runtime(adapter.native)
        query = 'Request movie "Fixture movie" (2020) in Seerr'
        staged = service.execute(query, VIEWER)
        assert staged["routing_source"] == "explicit_seerr_deterministic"
        assert staged["terminal_state"] == "awaiting_confirmation", staged
        assert not [r for r in state.calls if r["method"] == "POST"]
        result = service.confirm(staged["plan"]["confirm_id"], VIEWER)
        assert result["terminal_state"] == "completed", result
        assert len([r for r in state.calls if r["method"] == "POST"]) == 1
        assert service.confirm(staged["plan"]["confirm_id"], VIEWER)["terminal_state"] == "denied"


@pytest.mark.parametrize("query", [
    'Request 4K movie "Fixture movie" in Seerr',
    'Request movie "Fixture movie" as another user in Seerr',
    'Show request status for "Fixture movie" (2020) in Seerr',
])
def test_unrepresentable_native_request_qualifiers_clarify(query):
    call = route_native_request_query(query)
    assert call == Call("clarify", {"slot": "scope"})


def test_request_status_year_does_not_become_an_unqualified_wrong_remake():
    call = Call("request_status", {"title": "Fixture movie"})
    assert verify_proposal(call, 'What is my request status for "Fixture movie" from 2020?').name == "clarify"
    literal = Call("request_status", {"title": "Blade Runner 2049"})
    assert verify_proposal(literal, 'Show my request status for "Blade Runner 2049"') == literal


def test_title_only_request_status_does_not_drop_a_selected_remake():
    call = Call("request_status", {"title": "Fixture movie"})
    context = {"selected_media": {"title": "Fixture movie", "media_type": "movie", "year": 2020}}
    assert verify_proposal(call, 'What is its request status?', context).name == "clarify"


@pytest.mark.parametrize("query, expected", [
    ('Search for the album "One record" by "Fixture artist" in Lidarr', "clarify"),
    ('Search for music by "Fixture artist" in Lidarr', "clarify"),
    ('Search all missing albums by "Fixture artist" in Lidarr', "music_search"),
    ('Download missing music for artist "Fixture artist"', "music_search"),
])
def test_artist_only_music_write_requires_explicit_broad_scope(query, expected):
    call = Call("music_search", {"artist": "Fixture artist"})
    assert verify_proposal(call, query).name == expected


def test_explicit_unknown_write_outcome_overrides_http_500_and_read_posts_are_not_mutations():
    obs = AdapterObservation("error", "bazarr", "bazarr_subtitle_download", 500, False,
                             {"outcome": "unknown"}, [{"method": "POST", "status": 500}])
    assert derive_adapter_plan_state(obs)["adapter_state"] == "unknown_outcome"
    read = AdapterObservation("error", "bitmagnet", "search", 502, False,
                              {"error": "invalid_upstream_json"},
                              [{"method": "POST", "status": 200, "effect": "read"}])
    assert derive_adapter_plan_state(read)["adapter_state"] == "unknown_outcome"
