"""Benchmark integrity checks: timing distributions and confirmation authorization."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sys

from arrmes.executor import BoundedExecutor
from arrmes.planner import Principal
from arrmes.protocol import Call,encode
from arrmes.sandbox import SandboxCluster,NetworkIsolationGuard

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('benchmark_comparison',ROOT/'scripts/benchmark_comparison.py')
bench=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_frozen_case_bytes_and_distinct_queries():
    dataset=ROOT/'data/overnight-v1/routing-heldout.jsonl'
    manifest=json.loads(dataset.with_name('manifest.json').read_text())
    assert bench.sha256(dataset)==manifest['sha256']
    rows=[json.loads(l) for l in dataset.read_text().splitlines()]
    assert len(rows)==80==len({r['id'] for r in rows})
    assert len({r['query'] for r in rows})==80
    assert manifest['training_allowed'] is False


def test_latency_samples_are_not_independent_accuracy_cases():
    q=bench.quantiles([.1,.2,.3,.4,.5])
    assert q['n']==5
    assert q['p50_seconds']==.3
    assert q['p95_seconds']==.5
    assert bench.quantiles([])['p50_seconds'] is None


def test_harness_never_auto_confirms_an_incorrect_model_write(tmp_path):
    principal=Principal('owner-1',frozenset({'media:read','media:manage'}))
    with SandboxCluster(tmp_path) as cluster:
        adapter=bench.make_adapter(cluster)
        executor=BoundedExecutor(adapter)
        # Deliberately conflicting expected label tests the benchmark's
        # authorization boundary, independent of the product's verifier.
        case={'id':'wrong-write','query':'Search missing episodes of Severance in Sonarr.',
              'expected':{'name':'tv_search_missing','arguments':{'title':'Andor'}}}
        with NetworkIsolationGuard(allowed_ports={cluster.port}):
            outcome=bench.perform(encode(Call('tv_search_missing',{'title':'Severance'}),'hermes'),'hermes',
                                  case,executor,principal,cluster)
        assert outcome['initial_terminal']=='awaiting_confirmation'
        assert outcome['explicit_confirmation_applied'] is False
        assert outcome['mutations']==0
        assert outcome['preconfirmation_mutations']==0


def test_malformed_native_output_does_not_call_fixture(tmp_path):
    principal=Principal('owner-1',frozenset({'media:read','media:manage'}))
    with SandboxCluster(tmp_path) as cluster:
        executor=BoundedExecutor(bench.make_adapter(cluster))
        case={'query':'Check Severance in Sonarr.', 'expected':{'name':'tv_status','arguments':{'title':'Severance'}}}
        for raw in ['<tool_call>{"name":"tv_status","arguments":{"title":"Severance","api_key":"x"}}</tool_call>',
                    '<tool_call>{"name":"shell","arguments":{"command":"id"}}</tool_call>',
                    '<tool_call>{"name":"tv_status","arguments":{"title":"Severance","title":"Andor"}}</tool_call>',
                    'prefix <tool_call>{"name":"tv_status","arguments":{"title":"Severance"}}</tool_call>']:
            cluster.reset()
            outcome=bench.perform(raw,'hermes',case,executor,principal,cluster)
            assert outcome['terminal']=='invalid_proposal'
            assert outcome['audit']==[]


def test_format_translation_is_complete_parse_without_argument_repair():
    spec=importlib.util.spec_from_file_location('score_benchmark_outputs',ROOT/'scripts/score_benchmark_outputs.py')
    scoring=importlib.util.module_from_spec(spec);spec.loader.exec_module(scoring)
    call=scoring.canonical_json_call('<tool_call>{"name":"tv_status","arguments":"{\\"title\\":\\"Severance\\"}"}</tool_call>')
    assert call==Call('tv_status',{'title':'Severance'})
    import pytest
    for raw in ['prose {"name":"tv_status","arguments":{"title":"Severance"}}',
                '{"name":"tv_status","arguments":{"title":"Severance","title":"Andor"}}',
                '{"name":"tv_status","arguments":"not JSON"}',
                '[{"name":"tv_status","arguments":{"title":"Severance"}}]']:
        with pytest.raises((ValueError,TypeError)):
            scoring.canonical_json_call(raw)


def test_raw_archive_preserves_original_bytes_across_repeated_compaction(tmp_path, monkeypatch):
    import hashlib
    import zipfile
    spec=importlib.util.spec_from_file_location('compact_benchmark_receipts',ROOT/'scripts/compact_benchmark_receipts.py')
    compact=importlib.util.module_from_spec(spec);spec.loader.exec_module(compact)
    folder=tmp_path/'experiments/overnight-v1';folder.mkdir(parents=True)
    record={'schema_version':1,'rows':[{'raw_unverified':'prose\n<tool_call>{"name":"tv_status","arguments":{"title":"Séries"}}</tool_call>','metric':None}]}
    original=(json.dumps(record,ensure_ascii=False,indent=4)+'\n').encode()
    source=folder/'run.json';source.write_bytes(original)
    archive=tmp_path/'evidence.zip'
    monkeypatch.setattr(compact,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['compact','--archive',str(archive)])
    compact.main()
    first_zip=archive.read_bytes()
    compact.main()
    assert archive.read_bytes()==first_zip
    assert json.loads(source.read_bytes())==record
    index=json.loads((folder/'receipt-encoding-index.json').read_text())
    assert index['records'][0]['original_byte_sha256']==hashlib.sha256(original).hexdigest()
    with zipfile.ZipFile(archive) as z:
        assert z.read('experiments/overnight-v1/run.json')==original


def test_completed_terminal_with_wrong_business_data_fails_oracle():
    oracle={'terminal':'completed','observation_status':'verified_ok','observation_data':{'movie_id':42,'title':'Correct title','year':2020,'available':True}}
    wrong={'terminal':'completed','observation_status':'verified_ok','observation_data':{'movie_id':99,'title':'Correct title','year':2020,'available':True}}
    assert bench.compare_observations(wrong,oracle,'movie_status')['business_observation_match'] is False
    wrong['observation_data']['movie_id']=42
    assert bench.compare_observations(wrong,oracle,'movie_status')['business_observation_match'] is True


def test_only_declared_volatile_fields_are_omitted():
    a={'observation_status':'verified_ok','observation_data':{'series_id':42,'title':'TV','command_id':1,'command_status':'completed','confirmed_call':{'name':'x'}}}
    b={'observation_status':'verified_ok','observation_data':{'series_id':42,'title':'TV','command_id':8,'command_status':'completed'}}
    assert bench.compare_observations(a,b,'tv_search_missing')['business_observation_match'] is True
    a['observation_data']['series_id']=43
    assert bench.compare_observations(a,b,'tv_search_missing')['business_observation_match'] is False
    # A history timestamp is business content, not command-creation metadata.
    a={'observation_status':'verified_ok','observation_data':{'history':[{'id':1,'stopped_at':1}]}}
    b={'observation_status':'verified_ok','observation_data':{'history':[{'id':1,'stopped_at':2}]}}
    assert bench.compare_observations(a,b,'watch_history')['business_observation_match'] is False
