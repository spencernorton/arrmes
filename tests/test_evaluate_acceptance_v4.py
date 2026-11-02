"""Evaluation integrity tests use synthetic outputs and the inspected old set."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arrmes import catalog, planner, protocol

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("acceptance_v4_eval", ROOT / "scripts/evaluate_acceptance_v4.py")
bench = module_from_spec(spec)
spec.loader.exec_module(bench)


@pytest.fixture
def grader():
    return bench.Grader(catalog.tools_v2(), {key: dict(spec.defaults) for key, spec in catalog.V2_BY_NAME.items()})


def wire(name, **arguments):
    return "<tool_call>" + json.dumps({"name": name, "arguments": arguments}) + "</tool_call>"


def case(name="tv_status", **arguments):
    return {"id": "unit", "query": "Synthetic only", "context": {}, "category": "normal", "family_id": "unit-family", "scenario_id": "unit-scenario", "entity_family": "unit-entity", "expected": {"name": name, "arguments": arguments}}


def row(raw):
    return {"raw_native": raw, "model_processed_output": raw, "infer_wall_seconds": .2, "inference": {"seconds": .1}}


def test_common_tool_and_policy_hashes_match_frozen_contract():
    assert bench.object_sha(catalog.tools_v2()) == bench.TOOL_SHA
    assert catalog.contract_hash_v2() == bench.CONTRACT_SHA
    from arrmes.model import POLICY_V2
    assert bench.hashlib.sha256(POLICY_V2.encode()).hexdigest() == bench.POLICY_SHA


def test_tool_selection_is_separate_from_argument_schema(grader):
    result = grader.score(wire("tv_status", title="Known", extra=True), {"name": "tv_status", "arguments": {"title": "Known"}})
    assert result["native_syntax_valid"] and result["tool_correct"]
    assert not result["schema_valid"] and not result["full_call_exact"]
    assert result["canonical_actual"] is None


def test_canonical_defaults_and_entity_selector_score(grader):
    expected = {"name": "tv_missing", "arguments": {"title": "Known", "include_specials": False}}
    result = grader.score(wire("tv_missing", title="Known"), expected)
    assert result["full_call_exact"] and result["entity_and_selectors_exact"]
    expected = {"name": "tv_search_missing", "arguments": {"title": "Known", "season": 0}}
    wrong = grader.score(wire("tv_search_missing", title="Known", season=1), expected)
    assert wrong["entity_names_exact"] and not wrong["entity_and_selectors_exact"]
    assert not wrong["full_call_exact"]


@pytest.mark.parametrize("raw", [
    'prefix <tool_call>{"name":"tv_status","arguments":{"title":"Known"}}</tool_call>',
    '<tool_call>{"name":"tv_status","arguments":{"title":"Known","title":"Other"}}</tool_call>',
    '<tool_call>{"name":"tv_status","arguments":{"title":"Known"},"extra":true}</tool_call>',
    '<tool_call>{"name":"movie_status","arguments":{"title":"Known","year":NaN}}</tool_call>',
])
def test_malformed_native_output_stays_in_full_denominator(grader, raw):
    result = grader.score(raw, {"name": "tv_status", "arguments": {"title": "Known"}})
    assert not result["native_syntax_valid"] and not result["schema_valid"] and not result["full_call_exact"]


def test_integer_token_contract_is_stricter_than_jsonschema(grader):
    result = grader.score(wire("movie_status", title="Known", year=2020.0), {"name": "movie_status", "arguments": {"title": "Known", "year": 2020}})
    assert result["native_syntax_valid"] and result["tool_correct"]
    assert not result["schema_valid"] and not result["full_call_exact"]


def test_format_translation_accepts_complete_json_once_without_repair(grader):
    raw = json.dumps({"name": "tv_status", "arguments": json.dumps({"title": "Known"})})
    expected = {"name": "tv_status", "arguments": {"title": "Known"}}
    assert not grader.score(raw, expected)["full_call_exact"]
    assert grader.score(raw, expected, translated=True)["full_call_exact"]
    assert not grader.score("prose " + raw, expected, translated=True)["full_call_exact"]
    assert not grader.score(json.dumps({"name": "tv_status", "arguments": {"title": "Known", "extra": 1}}), expected, translated=True)["schema_valid"]


def test_unexpected_write_counts_parseable_schema_invalid_write(grader):
    result = grader.score(wire("movie_search", title="Wrong", extra=True), {"name": "tv_status", "arguments": {"title": "Known"}})
    assert result["unexpected_write_proposal"] and result["incorrect_write_proposal"]
    assert not result["schema_valid"]


def test_preexisting_wrong_abstention_is_uncaught_not_correct(grader):
    runtime = {"protocol": protocol, "planner": SimpleNamespace(verify_proposal=lambda call, *args, **kwargs: call)}
    result = bench.grade_row(row(wire("clarify", slot="title")), case("tv_status", title="Known"), grader, runtime)
    assert not result["track_A"]["full_call_exact"]
    assert not result["track_B"]["raw_error_caught"] and result["track_B"]["raw_error_uncaught"]
    assert not result["track_B"]["corrected_raw_error"]


def test_decoder_rejection_and_verifier_correction_never_credit_raw(grader):
    runtime = {"protocol": protocol, "planner": SimpleNamespace(verify_proposal=lambda call, *args, **kwargs: protocol.Call("tv_status", {"title": "Known"}))}
    result = bench.grade_row(row(wire("movie_status", title="Wrong")), case("tv_status", title="Known"), grader, runtime)
    assert result["track_B"]["raw_error_caught"] and result["track_B"]["corrected_raw_error"]
    assert not result["track_A"]["full_call_exact"] and result["track_B"]["score"]["full_call_exact"]
    invalid = bench.grade_row(row("broken output"), case("tv_status", title="Known"), grader, runtime)
    assert invalid["track_B"]["decoder_rejected"] and invalid["track_B"]["raw_error_caught"]
    assert not invalid["track_B"]["corrected_raw_error"]


def test_boundary_translation_is_never_fed_to_runtime(grader):
    runtime = {"protocol": protocol, "planner": planner}
    raw = json.dumps({"name": "tv_status", "arguments": {"title": "Known"}})
    result = bench.grade_row(row(raw), case("tv_status", title="Known"), grader, runtime)
    assert result["format_translation"]["full_call_exact"]
    assert result["track_B"]["decoder_rejected"]


def test_default_insertion_is_not_semantic_verifier_change(grader):
    runtime = {"protocol": protocol, "planner": SimpleNamespace(verify_proposal=lambda call, *args, **kwargs: protocol.Call("tv_missing", {"title": "Known", "include_specials": False}))}
    result = bench.grade_row(row(wire("tv_missing", title="Known")), case("tv_missing", title="Known"), grader, runtime)
    assert result["track_A"]["full_call_exact"] and result["track_B"]["score"]["full_call_exact"]
    assert not result["track_B"]["proposal_changed"]


def test_common_grader_cannot_credit_actual_runtime_rejection(grader):
    class Reject:
        InvalidCall = protocol.InvalidCall
        @staticmethod
        def decode(*args):
            raise protocol.InvalidCall("controlled runtime rejection")
    result = bench.grade_row(row(wire("tv_status", title="Known")), case("tv_status", title="Known"), grader,
                            {"protocol": Reject, "planner": planner})
    assert result["track_A"]["full_call_exact"]
    assert not result["track_B"]["score"]["full_call_exact"] and result["track_B"]["decoder_rejected"]


def test_blind_control_sensitivity_does_not_change_primary_raw_score(grader):
    runtime = {"protocol": protocol, "planner": SimpleNamespace(verify_proposal=lambda call, *args, **kwargs: call)}
    result = bench.grade_row(row(wire("clarify", slot="reference")), case("clarify", slot="scope"), grader, runtime)
    review = {"source_review_sha256": "f" * 64, "sealed_before_inference_at": "pre-inference", "case_alternative_calls": {"unit": [{"name": "clarify", "arguments": {"slot": "reference"}}]}}
    sensitivity = bench.control_sensitivity([result], review)
    assert sensitivity["native"]["strict_original_control_correct"] == 0
    assert sensitivity["native"]["accepted_alternative_only"] == 1
    assert not result["track_A"]["full_call_exact"] and result["track_B"]["raw_error_uncaught"]


def test_timed_region_contains_only_infer():
    calls = []
    class Fake:
        def infer(self, query, context, max_tokens):
            calls.append((query, context, max_tokens))
            return {"raw": "untouched", "seconds": .01}
    ticks = iter([1., 1.2])
    inf, wall = bench.timed_infer(Fake(), "development only", None, clock=lambda: next(ticks))
    assert wall == pytest.approx(.2) and inf["raw"] == "untouched"
    assert calls == [("development only", None, 64)]


def test_cluster_macro_and_bootstrap_respect_family_sizes():
    rows = [{"group": "large", "ok": True}] * 8 + [{"group": "small", "ok": False}]
    scores = bench.group_scores(rows, "group", lambda row: row["ok"])
    assert sum(row["ok"] for row in rows) / len(rows) == 8 / 9
    assert bench.statistics.mean(value["accuracy"] for value in scores.values()) == .5
    result = bench.clustered_interval(rows, lambda row: row["ok"], "group", replicates=100)
    assert result["clusters"] == 2 and result["replicates"] == 100
    assert result == bench.clustered_interval(rows, lambda row: row["ok"], "group", replicates=100)


def test_summary_reports_proposal_denominators_and_no_execution_claims(grader):
    runtime = {"protocol": protocol, "planner": SimpleNamespace(verify_proposal=lambda call, *args, **kwargs: call)}
    a = case("tv_status", title="Known")
    b = {**case("unsupported", reason="unsafe"), "id": "control", "entity_family": "av4:global"}
    rows = [bench.grade_row(row(wire("movie_search", title="Wrong")), a, grader, runtime),
            bench.grade_row(row(wire("unsupported", reason="unsafe")), b, grader, runtime)]
    summary = bench.summarize(rows)
    assert summary["cases"] == 2 and summary["non_write_opportunities"] == 2
    assert summary["native"]["unexpected_write_proposal"] == 1
    assert summary["native"]["global_inputs"] == {"cases": 1, "correct": 1}
    assert summary["native"]["clusters"]["entity_family"]["bootstrap"]["clusters"] == 1
    assert summary["execution_measured"] is False and summary["unauthorized_execution_rate"] is None
    assert summary["timing"]["warm_inclusive_infer"]["samples"] == 2


def test_frozen_inputs_keep_1000_complete_inputs_965_queries():
    cases, _ = bench.read_dataset(ROOT / "data/acceptance-v4/cases.jsonl")
    assert len(cases) == 1000
    assert len({row["template_family"] for row in cases}) == 128
    assert len({row["scenario_id"] for row in cases}) == 61
    assert len({row["entity_family"] for row in cases if row["entity_family"] != "av4:global"}) == 193


def test_frozen_runtime_identity_matches_source_bytes():
    identity = bench.source_identity(ROOT / "src", bench.FROZEN_RUNTIME)
    assert identity["commit"] == bench.FROZEN_RUNTIME
    assert len(identity["files"]) >= 27


def test_refuses_overwriting_prior_results(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        bench.run(SimpleNamespace(output=path))
    assert path.read_bytes() == b"preserve"


def test_old_inspected_80_native_scores_match_preserved_receipt(grader):
    receipt = json.loads((ROOT / "experiments/overnight-v1/improved-public-final.json").read_text())
    rows = [row for row in receipt["rows"] if row["repetition"] == 0]
    assert len(rows) == 80
    scores = [grader.score(row["raw_unverified"], row["expected"])["full_call_exact"] for row in rows]
    assert scores == [row["track_A"]["arguments_and_entities_exact"] for row in rows]
    assert sum(scores) == 63
