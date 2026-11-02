"""Confirmation identity and native-route integration regressions."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.live_adapter import LiveGatewayAdapter, WriteConfirmationStore
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, decode, normalized, validate_call

WRITER = Principal("fixture:writer", frozenset({"media:read", "media:manage", "requests:create"}))


def _changing_gateway():
    adapter = LiveGatewayAdapter("https://gateway.test", gateway_api_key="fixture-key", write_mode="confirm",
                                 request_owner_subject=WRITER.subject)
    state = {"id": 7, "posts": [], "reads": 0}

    def request(method, path, *, audit, headers=None, body=None, **kwargs):
        if method == "POST":
            state["posts"].append((path, copy.deepcopy(body)))
            return 201, {"id": 99, "status": "queued"}
        state["reads"] += 1
        if path.endswith("/series"):
            return 200, [{"id": state["id"], "title": "Sintel", "year": 2010}]
        if path.startswith("/request/api/search?"):
            return 200, {"results": [{"id": state["id"], "title": "Sintel", "year": 2010, "type": "movie"}]}
        if path.endswith("/command/99"):
            return 200, {"id": 99, "status": "completed"}
        raise AssertionError("Unexpected fixture request")

    adapter._request_json = request
    return adapter, state


@pytest.mark.parametrize("name,args,query", [
    ("tv_search_missing", {"title": "Sintel"}, "Download missing episodes of Sintel"),
    ("request_media", {"title": "Sintel", "media_type": "movie", "year": 2010}, "Request the movie Sintel (2010)"),
])
def test_confirmation_pins_resolved_entity_before_any_write(name, args, query):
    adapter, state = _changing_gateway()
    executor = BoundedExecutor(adapter)
    pending = executor.execute_call(Call(name, args), WRITER, query=query)
    assert pending.terminal_state == "awaiting_confirmation"
    assert state["posts"] == []
    state["id"] = 8  # A re-added library entry or changed metadata search result.
    result = executor.confirm_write(pending.plan["confirm_id"], WRITER)
    assert result.terminal_state == "denied"
    assert result.observation.data["error"] == "confirmation_entity_changed"
    assert state["posts"] == []
    replay = executor.confirm_write(pending.plan["confirm_id"], WRITER)
    assert replay.observation.data["error"] == "confirmation_already_consumed"


@pytest.mark.parametrize("field", ["arguments", "resolved_entity"])
def test_confirmation_signature_binds_arguments_and_service_identity(field):
    store = WriteConfirmationStore(ttl_seconds=10, clock=lambda: 100)
    token = store.mint(WRITER, Call("tv_search_missing", {"title": "Sintel"}),
                       summary="Search missing Sintel", resolved_entity={"series_id": 7})
    getattr(token, field)["title" if field == "arguments" else "series_id"] = "Other" if field == "arguments" else 8
    redeemed, error = store.redeem(token.confirm_id, WRITER)
    assert redeemed is None and error == "confirmation_signature_invalid"


def test_confirmation_expires_at_exact_deadline_and_detaches_input():
    entity = {"series_id": 7, "nested": {"id": 1}}
    store = WriteConfirmationStore(ttl_seconds=10, clock=lambda: 100)
    token = store.mint(WRITER, Call("tv_search_missing", {"title": "Sintel"}), summary="fixture", resolved_entity=entity)
    entity["nested"]["id"] = 2
    assert token.resolved_entity["nested"]["id"] == 1
    redeemed, error = store.redeem(token.confirm_id, WRITER, now=110)
    assert redeemed is None and error == "confirmation_expired"


def test_confirmation_binds_full_expiry_and_rejects_nonfinite_times():
    for ttl in (float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            WriteConfirmationStore(ttl_seconds=ttl)
    store = WriteConfirmationStore(clock=lambda: 100.123456)
    token = store.mint(WRITER, Call("tv_search_missing", {"title": "Sintel"}), summary="fixture")
    token.expires_at += 0.00001
    assert store.redeem(token.confirm_id, WRITER)[1] == "confirmation_signature_invalid"
    with pytest.raises(ValueError, match="finite"):
        store.redeem(token.confirm_id, WRITER, now=float("nan"))


def test_confirmation_concurrent_replay_dispatches_exactly_once():
    adapter, state = _changing_gateway()
    executor = BoundedExecutor(adapter)
    pending = executor.execute_call(Call("tv_search_missing", {"title": "Sintel"}), WRITER,
                                    query="Download missing episodes of Sintel")
    barrier = threading.Barrier(12)

    def redeem(_):
        barrier.wait()
        return executor.confirm_write(pending.plan["confirm_id"], WRITER)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(redeem, range(12)))
    assert len(state["posts"]) == 1
    assert sum(r.terminal_state == "completed" for r in results) == 1
    successful = next(r for r in results if r.terminal_state == "completed")
    assert successful.proposal == {"name": "tv_search_missing", "arguments": {"title": "Sintel"}}
    assert sum(r.observation.data.get("error") == "confirmation_already_consumed" for r in results) == 11


@pytest.mark.parametrize("name,args", [("armiger_parse", {"release_name": "Sintel.2010.1080p.mkv"}),
                                       ("waypoint_status", {})])
def test_live_support_never_substitutes_an_unrelated_service(name, args):
    adapter, state = _changing_gateway()
    obs = adapter.execute(Call(name, args), WRITER)
    assert obs.status == "unsupported" and not obs.verified_complete
    assert state["reads"] == 0 and state["posts"] == []


def test_legacy_neural_wire_contract_cannot_select_untrained_extension():
    args = {"service": "sonarr", "operation": "health"}
    assert validate_call("service_query", args).arguments == args
    raw = "<tool_call>" + json.dumps({"name": "service_query", "arguments": args}) + "</tool_call>"
    with pytest.raises(InvalidCall, match="Unknown action"):
        decode(raw, family="hermes")


def test_native_plan_and_grounding_keep_scope_and_requested_service_bound():
    from arrmes.planner import prepare, verify_proposal
    call = Call("service_query", {"service": "sonarr", "operation": "queue"})
    p = Principal("fixture:reader", frozenset({"media:read"}))
    plan = prepare(call, p)
    assert plan["service"] == "sonarr" and plan["effect"] == "read"
    assert plan["contract"] == "arrmes.tools.v3"
    assert prepare(Call("service_query", {"service": "tautulli", "operation": "activity"}), p)["status"] == "denied"
    assert normalized(verify_proposal(call, "Show Sonarr queue")) == normalized(call)
    assert verify_proposal(call, "Show Radarr queue").name == "clarify"


def test_confirmation_store_rejects_read_actions_and_unknown_arguments():
    store = WriteConfirmationStore()
    with pytest.raises(ValueError, match="write actions"):
        store.mint(WRITER, Call("now_playing", {}), summary="read")
    with pytest.raises(InvalidCall):
        store.mint(WRITER, Call("movie_search", {"title": "Sintel", "url": "http://untrusted.test"}), summary="bad")
