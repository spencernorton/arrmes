import pytest

from arrmes.evaluate import summarize
from arrmes.live_adapter import LiveGatewayAdapter
from arrmes.planner import Principal, _is_grounded_in_text, verify_proposal
from arrmes.protocol import Call


def _row(case_id, *, raw=None):
    row = {"id": case_id, "family_id": case_id, "category": "read", "expected": {"name": "tv_status"},
           "actual": {"name": "tv_status"}, "exact": True, "schema_valid": True, "action_correct": True, "seconds": .2}
    if raw is not None:
        row.update(raw_neural_actual={"name": "clarify"}, raw_neural_schema_valid=True, raw_neural_exact=raw)
    return row


def test_missing_raw_evidence_never_becomes_full_accuracy_or_failures():
    report = summarize([_row("observed", raw=False), _row("unobserved")])
    assert report["raw_neural_observed_cases"] == 1
    assert report["raw_neural_scored_cases"] == 1
    assert report["raw_neural_exact"] is None and report["raw_neural_exact_accuracy"] is None
    assert report["raw_neural_wilson_95_ci"] is None and report["raw_neural_cluster_bootstrap_95_ci"] is None
    assert report["raw_neural_observed_exact"] == 0 and report["raw_neural_observed_exact_accuracy"] == 0
    assert report["raw_neural_failures_by_expected_action"] is None
    assert report["raw_neural_observed_failures_by_expected_action"] == {"tv_status": 1}
    assert report["by_service"]["sonarr"]["raw_neural_exact_accuracy"] is None
    assert report["by_category"]["read"]["raw_neural_exact"] is None


def test_observed_invalid_raw_output_is_a_measured_model_failure():
    row = _row("invalid")
    row.update(raw_neural_actual=None, raw_neural_schema_valid=False, raw_neural_exact=False)
    report = summarize([row])
    assert report["exact_accuracy"] == 1
    assert report["raw_neural_observed_cases"] == 1 and report["raw_neural_exact_accuracy"] == 0


@pytest.mark.parametrize("entity,text", [("Alien", "Search Radarr for Aliens"),
                                         ("It", "Is the bitrate okay?"),
                                         ("Sintel", "Check Sin Tell"),
                                         ("Dune", "Search Dunes")])
def test_substrings_or_joined_words_are_not_entity_grounding(entity, text):
    assert not _is_grounded_in_text(entity, text)


def test_title_recovery_uses_the_complete_requested_span_before_write():
    out = verify_proposal(Call("movie_search", {"title": "Alien"}), "Search Radarr for Aliens")
    assert out.name != "movie_search" or out.arguments["title"] == "Aliens"
    assert _is_grounded_in_text("Björk", "Check Bjork in Lidarr")
    assert _is_grounded_in_text("夜之国", '查找电影 "夜之国"')


@pytest.mark.parametrize("descriptor", ["active", "paused", "downloading", "first", "newest", "current"])
def test_transfer_descriptors_do_not_become_unbound_mutation_targets(descriptor):
    call = Call("torrent_pause", {"title": descriptor})
    result = verify_proposal(call, f"Pause the {descriptor} torrent download.")
    assert result == Call("clarify", {"slot": "title"})
    quoted = verify_proposal(call, f'Pause the torrent named "{descriptor}".')
    assert quoted == call


@pytest.mark.parametrize("fallback", [(503, {"error": "unavailable"}), (200, {"episodes": []})])
def test_other_series_missing_records_never_survive_failed_fallback(fallback):
    adapter = LiveGatewayAdapter("https://gateway.test", gateway_api_key="fixture-key", write_mode="confirm")

    def request(method, path, *, audit, **kwargs):
        if path.endswith("/series"):
            return 200, [{"id": 7, "title": "Sintel"}]
        if "/wanted/missing" in path:
            return 200, {"records": [{"id": 9, "seriesId": 88, "title": "Unrelated episode"}], "totalRecords": 1}
        if "/episode?" in path:
            return fallback
        raise AssertionError("Unexpected fixture path")

    adapter._request_json = request
    obs = adapter.execute(Call("tv_missing", {"title": "Sintel"}), Principal("fixture:r", frozenset({"media:read"})))
    assert obs.status == "error" and not obs.verified_complete
    assert obs.data == {"error": "series_episode_fallback_failed"}
