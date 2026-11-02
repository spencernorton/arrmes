"""Explicit subtitle writes cannot expand the unchanged neural wire contract."""
import json
import pytest

from arrmes.live_adapter import WriteConfirmationStore
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, decode, validate_call


def subtitle_args():
    return {"media_type": "episode", "title": "Fixture series", "season": 1,
            "episode": 2, "language": "en", "forced": False,
            "hearing_impaired": False}


def test_explicit_subtitle_call_is_not_a_neural_tool_and_can_bind_confirmation():
    args = subtitle_args()
    call = validate_call("bazarr_subtitle_search", args)
    raw = "<tool_call>" + json.dumps({"name": call.name, "arguments": args}) + "</tool_call>"
    with pytest.raises(InvalidCall):
        decode(raw, "hermes")
    principal = Principal("fixture:operator", frozenset({"media:manage"}))
    store = WriteConfirmationStore(ttl_seconds=5)
    token = store.mint(principal, call, summary="Search one subtitle provider target",
                       resolved_entity={"episode_id": 10, "version": "1.6.2"}, now=100)
    args["episode"] = 99
    redeemed, error = store.redeem(token.confirm_id, principal, now=104)
    assert error is None and redeemed.arguments["episode"] == 2
    assert store.redeem(token.confirm_id, principal, now=104)[0] is None
    expired = store.mint(principal, call, summary="Expired target", now=100)
    assert store.redeem(expired.confirm_id, principal, now=105)[0] is None


@pytest.mark.parametrize("change", [
    {"media_type": "movie"}, {"language": "english"}, {"season": True},
    {"episode": 1.0}, {"provider": "untrusted"}, {"forced": "False"},
])
def test_invalid_subtitle_targets_cannot_reach_confirmation(change):
    with pytest.raises(InvalidCall):
        validate_call("bazarr_subtitle_search", {**subtitle_args(), **change})
