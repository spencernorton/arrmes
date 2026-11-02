"""No inference: freeze integrity, input overlap detection and injected wires."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("acceptance_v4_audit", ROOT / "scripts/audit_acceptance_v4.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def fresh_row():
    return deepcopy(audit.read_jsonl(audit.DATA / "cases.jsonl")[0])


def test_frozen_authored_cases_reproduce_and_validate_against_preserved_v2_contract():
    cases, summary = audit.verify_freeze()
    assert summary["cases"] == summary["unique_inputs"] == 1000
    assert summary["unique_queries"] == 965
    assert summary["template_families"] == 128 and summary["scenario_families"] == 61
    assert summary["entity_families"] == 194 and summary["context_rows"] == 48
    assert set(summary["tool_counts"]) == set(audit.V2_BY_NAME)
    assert set(summary["family_sizes"].values()) == {5, 8}
    assert summary["repeat_inference_samples"] == 0
    assert summary["wire_faults"] == {"cases": 32, "fault_families": 16,
                                      "all_expected_rejected": True, "model_generated_outputs": 0}
    assert all(row["execution_policy"]["confirmation_required"]
               for row in cases if row["expected"]["name"] in {"torrent_pause", "movie_search", "request_media"})


@pytest.mark.parametrize("mutation", ["duplicate_id", "duplicate_input", "wrong_gold", "v3_gold", "bad_context"])
def test_invalid_inputs_and_gold_are_rejected_without_backend_calls(mutation):
    row = fresh_row()
    other = deepcopy(row)
    other["id"] = "AV4-9999"
    other["query"] += " Please."
    if mutation == "duplicate_id":
        other["id"] = row["id"]
    elif mutation == "duplicate_input":
        other["query"] = row["query"]
    elif mutation == "wrong_gold":
        other["expected"]["arguments"]["title"] = []
    elif mutation == "v3_gold":
        other["expected"] = {"name": "service_query", "arguments": {"service": "sonarr", "operation": "health"}}
    else:
        other["context"] = {"private_url": "https://example.invalid/not-context"}
    with pytest.raises((ValueError, audit.InvalidCall)):
        audit.validate_cases([row, other], audit.read_json(audit.DATA / "case.schema.json"))


def test_template_masking_catches_entity_and_numeric_substitutions():
    candidate = fresh_row()
    candidate["query"] = 'Inspect the library card "Vellum Meridian" (2001).'
    candidate["expected"] = {"name": "movie_status", "arguments": {"title": "Vellum Meridian", "year": 2001}}
    candidate["entities"] = [{"kind": "title", "name": "Vellum Meridian"}]
    reference = {"query": 'Inspect the library card "Different Synthetic Name" (1998)!',
                 "expected": {"name": "movie_status", "arguments": {"title": "Different Synthetic Name", "year": 1998}}}
    report = audit.audit_overlap([candidate], [reference])
    assert report["full_query_context_overlap_count"] == 0
    assert report["entity_name_exact_overlap_count"] == 0
    assert report["normalized_template_overlap_case_ids"] == [candidate["id"]]


def test_same_utterance_with_different_selected_media_is_query_overlap_not_full_input_overlap():
    candidate = fresh_row()
    candidate.update(query="Check the selected film.", entities=[],
                     context={"selected_media": {"title": "Vellum Meridian", "media_type": "movie", "year": 2001}})
    reference = {"query": candidate["query"], "context": {
        "selected_media": {"title": "Different Synthetic Name", "media_type": "movie", "year": 1998}}}
    report = audit.audit_overlap([candidate], [reference])
    assert report["full_query_context_overlap_count"] == 0
    assert report["exact_query_overlap_count"] == 1
    assert report["normalized_template_overlap_case_count"] == 1
    reference["context"] = deepcopy(candidate["context"])
    assert audit.audit_overlap([candidate], [reference])["full_query_context_overlap_count"] == 1


def test_quoted_decoy_entities_are_audited_even_when_not_in_control_gold():
    candidate = fresh_row()
    candidate.update(query='Discuss "Vellum Meridian" later; no action selected.',
                     expected={"name": "clarify", "arguments": {"slot": "scope"}},
                     entities=[])
    reference = {"query": 'I mentioned "Vellum Meridian", without choosing an operation.',
                 "expected": {"name": "clarify", "arguments": {"slot": "scope"}}}
    report = audit.audit_overlap([candidate], [reference])
    assert report["entity_name_exact_overlaps"] == ["vellum meridian"]
    assert report["candidate_entities_occurring_in_reference_inputs"] == ["vellum meridian"]


def test_unicode_punctuation_and_whitespace_do_not_hide_template_overlap():
    left = fresh_row()
    left.update(query='CHECK  "Vellum Meridian" — year 2001!', entities=[
        {"kind": "title", "name": "Vellum Meridian"}])
    right = {"query": 'check "Another Title", year 1998.',
             "expected": {"name": "movie_status", "arguments": {"title": "Another Title", "year": 1998}}}
    assert audit.audit_overlap([left], [right])["normalized_template_overlap_case_count"] == 1


def test_workflow_calls_without_user_inputs_are_explicit_description_proxies(tmp_path):
    path = tmp_path / "workflows.jsonl"
    path.write_text(json.dumps({"description": "Check another synthetic series then search missing episodes.",
                               "calls": [{"name": "tv_status", "arguments": {"title": "Another Title"}}]}) + "\n")
    report = audit.audit_corpus(path, [fresh_row()])["overlap"]
    assert report["reference_rows"] == report["reference_workflow_description_proxy_rows"] == 1
    assert report["reference_recorded_query_rows"] == 0
    assert report["full_query_context_audit_applicable"] is False
    assert report["full_query_context_overlap_count"] is None


def test_unrecognized_or_mismatched_training_inputs_never_claim_a_completed_audit(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"unrelated":"data"}\n')
    with pytest.raises(ValueError, match="no recognized inputs"):
        audit.audit_corpus(corpus, [fresh_row()])
    corpus.write_text(json.dumps({"query": "A local-only corpus prompt.", "expected": {
        "name": "clarify", "arguments": {"slot": "scope"}}}) + "\n")
    with pytest.raises(ValueError, match="training corpus identity mismatch"):
        audit.audit_corpus(corpus, [fresh_row()], require_original=True)


def test_report_never_copies_private_corpus_text_or_file_paths(tmp_path):
    path = tmp_path / "local-corpus.jsonl"
    secret_marker = "LOCAL-ONLY-CORPUS-TEXT-MUST-NOT-BE-PUBLISHED"
    path.write_text(json.dumps({"query": secret_marker, "expected": {
        "name": "clarify", "arguments": {"slot": "scope"}}}) + "\n")
    serialized = json.dumps(audit.audit_corpus(path, [fresh_row()]))
    assert secret_marker not in serialized
    assert str(path) not in serialized


def test_audit_cli_refuses_to_overwrite_frozen_inputs(capsys):
    original = (audit.DATA / "cases.jsonl").read_bytes()
    assert audit.main(["--output", str(audit.DATA / "cases.jsonl")]) == 1
    assert (audit.DATA / "cases.jsonl").read_bytes() == original
    assert json.loads(capsys.readouterr().err)["validated"] is False


def test_injected_wires_are_not_counted_as_model_predictions():
    rows = audit.read_jsonl(audit.DATA / "wire-faults.jsonl")
    assert {row["format"] for row in rows} == {"hermes", "functiongemma"}
    assert audit.verify_wire_cases(rows)["model_generated_outputs"] == 0
    row = deepcopy(rows[0])
    row["format"] = "hermes"
    row["raw"] = '<tool_call>{"name":"now_playing","arguments":{}}</tool_call>'
    with pytest.raises(ValueError, match="unexpectedly schema-valid"):
        audit.verify_wire_cases([row])
