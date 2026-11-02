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


@pytest.mark.parametrize("name,args,query,slot,expected", [
    ("movie_search", {"title": "Solar"}, 'Search for the movie "Solar Echo" in Radarr', "title", "Solar Echo"),
    ("request_status", {"title": "Quiet"}, 'Show my request status for "Quiet Harbor"', "title", "Quiet Harbor"),
    ("bitmagnet_search", {"query": "Winter"}, 'Search Bitmagnet for "Winter Signal"', "query", "Winter Signal"),
])
def test_unique_quoted_entity_conserves_the_complete_explicit_span(name, args, query, slot, expected):
    result = verify_proposal(Call(name, args), query)
    assert result.name == name
    assert result.arguments[slot] == expected


def test_partial_entity_with_multiple_quoted_spans_never_selects_a_target():
    result = verify_proposal(Call("movie_search", {"title": "Solar"}),
                             'Search for "Solar Echo" or "Solar Tide" in Radarr')
    assert result.name == "clarify"


@pytest.mark.parametrize("condition", ["if it is missing", "unless it is available", "only when nobody is watching"])
def test_unimplemented_write_conditions_do_not_become_unconditional_commands(condition):
    result = verify_proposal(Call("movie_search", {"title": "Solar Echo"}),
                             'Download "Solar Echo" in Radarr ' + condition)
    assert result == Call("unsupported", {"reason": "complex"})


def test_read_to_write_request_correction_rechecks_conditional_scope():
    result = verify_proposal(Call("request_status", {"title": "Solar Echo"}),
                             'Request the movie "Solar Echo" only when it is available')
    assert result.name in {"clarify", "unsupported"}


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


def _fallback_adapter(episodes):
    adapter = LiveGatewayAdapter("https://gateway.test", gateway_api_key="fixture-key", write_mode="confirm")

    def request(method, path, **kwargs):
        if path.endswith("/series"):
            return 200, [{"id": 7, "title": "Sintel"}]
        if "/wanted/missing" in path:
            return 200, {"records": [{"id": 9, "seriesId": 88}], "totalRecords": 1}
        if "/episode?" in path:
            return 200, episodes
        raise AssertionError("Unexpected fixture path")

    adapter._request_json = request
    return adapter


def test_sonarr_fallback_only_counts_aired_monitored_missing_episodes():
    base = {"seriesId": 7, "seasonNumber": 1, "hasFile": False, "monitored": True, "airDateUtc": "2000-01-01T00:00:00Z"}
    episodes = [{**base, "id": 1}, {**base, "id": 2, "airDateUtc": "2099-01-01T00:00:00Z"},
                {**base, "id": 3, "monitored": False}, {**base, "id": 4, "hasFile": True},
                {**base, "id": 5, "seasonNumber": 0}]
    adapter = _fallback_adapter(episodes)
    principal = Principal("fixture:r", frozenset({"media:read"}))
    obs = adapter.execute(Call("tv_missing", {"title": "Sintel"}), principal)
    assert obs.status == "verified_ok" and obs.data["missing_count"] == 1
    assert [row["id"] for row in obs.data["missing_episodes"]] == [1]
    specials = adapter.execute(Call("tv_missing", {"title": "Sintel", "include_specials": True}), principal)
    assert [row["id"] for row in specials.data["missing_episodes"]] == [1, 5]


@pytest.mark.parametrize("air_date", [None, "not-a-date", "2000-01-01T00:00:00"])
def test_sonarr_fallback_never_guesses_an_unknown_air_date(air_date):
    episodes = [{"seriesId": 7, "seasonNumber": 1, "hasFile": False, "monitored": True, "airDateUtc": air_date}]
    obs = _fallback_adapter(episodes).execute(Call("tv_missing", {"title": "Sintel"}), Principal("fixture:r", frozenset({"media:read"})))
    assert obs.status == "error" and not obs.verified_complete
    assert obs.data["error"] == "episode_air_date_unavailable"
