"""Qualify the integration instrument without crediting scripted calls as model accuracy."""
import importlib.util
import json
from pathlib import Path

import pytest

from arrmes.protocol import Call, encode

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_write_smoke", ROOT / "scripts/benchmark_native_write_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
_SCENARIOS_A = ROOT / "data/native-workflows-v1/scenarios.json"
_SCENARIOS_B = ROOT / "data/native-arr-workflows-v1/scenarios.json"
if _SCENARIOS_A.exists() and _SCENARIOS_B.exists():
    CASES = json.loads(_SCENARIOS_A.read_text(encoding="utf-8"))["cases"]
    CASES += json.loads(_SCENARIOS_B.read_text(encoding="utf-8"))["cases"]
else:
    CASES = []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_instrument_reaches_expected_fixture_without_mixing_model_and_system_scores(case):
    class Scripted:
        family = "hermes"

        def infer(self, *_args, **_kwargs):
            assert case["route"] == "neural_v2", "Explicit grammar unexpectedly invoked a model"
            raw = encode(Call(case["expected"]["name"], case["expected"]["arguments"]), "hermes")
            return {"raw_unverified": raw, "raw": raw}

    result = smoke.run_case(case, Scripted())
    assert result["system_expected"], result
    assert result["raw_exact"] is (True if case["route"] == "neural_v2" else None)
    assert result["mutations_before_confirmation"] == 0


def test_graphql_read_posts_are_not_credited_as_write_mutations():
    reads = [{"method": "POST", "payload": {"query": "query Read { version }"}}]
    writes = [{"method": "POST", "payload": {"query": "mutation Change { change }"}}]
    assert smoke.mutation_calls({"fixture": "bitmagnet"}, reads) == []
    assert smoke.mutation_calls({"fixture": "bitmagnet"}, writes) == writes
