#!/usr/bin/env python3
"""Actual published-model -> executor/runtime -> native media HTTP fixture smoke.

A frozen integration scenario set, not held-out language qualification. Native
fixture services are the only reachable network destinations during execution.
"""
from __future__ import annotations
import argparse,copy,hashlib,importlib.util,json,shutil,sys,time
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('benchmark_comparison',ROOT/'scripts/benchmark_comparison.py');bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)


def hashes(root):return {str(p.relative_to(root)):bench.sha256(p) for p in sorted(root.rglob('*.py'))}


def business_check(name,data):
    if data.get('source')!='native_api':return False
    if name=='tv_status':return data.get('series_id')==1 and data.get('title')=='Fixture series' and data.get('episode_count')==3 and data.get('episode_file_count')==1
    if name=='tv_missing':return data.get('series_id')==1 and data.get('missing_count')==1 and [r.get('id') for r in data.get('missing_episodes',[])]==[1]
    if name=='movie_status':return data.get('movie_id')==2 and data.get('year')==2020 and data.get('available') is True
    if name=='music_status':return data.get('artist_id')==4 and data.get('album_count')==1 and [r.get('id') for r in data.get('albums',[])]==[5]
    if name=='now_playing':return data.get('stream_count')==1 and len(data.get('sessions',[]))==1
    if name=='watch_history':return data.get('count')==1 and data.get('title_filter')=='Fixture series'
    return False


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',required=True);p.add_argument('--fixture-file',required=True)
    p.add_argument('--model',required=True);p.add_argument('--adapter',required=True)
    p.add_argument('--output',required=True);p.add_argument('--inputs',required=True);p.add_argument('--snapshot-dir',required=True)
    args=p.parse_args();public_src=Path(args.source_root).resolve();fixture=Path(args.fixture_file).resolve();snapshot=Path(args.snapshot_dir).resolve()
    if snapshot.exists():raise RuntimeError('Refusing to overwrite source snapshot')
    before=hashes(public_src);fixture_before=bench.sha256(fixture)
    shutil.copytree(public_src,snapshot/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (snapshot/'tests').mkdir();shutil.copy2(fixture,snapshot/'tests'/fixture.name)
    if before!=hashes(public_src) or before!=hashes(snapshot/'src') or fixture_before!=bench.sha256(fixture):raise RuntimeError('Public source changed while freezing')
    sys.path[:0]=[str(snapshot/'src'),str(snapshot/'tests')]
    from arrmes.model import LocalModel
    from arrmes.executor import BoundedExecutor
    from arrmes.server import ArrmesRuntimeService
    from arrmes.service_router import ConfiguredAdapterRouter
    from arrmes.protocol import normalized
    from arrmes.evaluate import score
    from test_native_media import READ_CASES,MediaFixtureState,media_fixture,READER
    cases=[{'id':f'NM1-{i+1}','scenario':'positive','query':q,'expected':normalized(c)} for i,(q,c) in enumerate(READ_CASES)]
    cases+=[{'id':'NM1-7','scenario':'ambiguous_series','query':cases[0]['query'],'expected':cases[0]['expected']},
            {'id':'NM1-8','scenario':'radarr_unavailable','query':cases[2]['query'],'expected':cases[2]['expected']}]
    frozen={'kind':'integration smoke, not new held-out qualification','frozen_at':datetime.now(timezone.utc).isoformat(),'cases':cases,'source_file_sha256':before,'fixture_sha256':fixture_before}
    inputs=Path(args.inputs);inputs.parent.mkdir(parents=True,exist_ok=True)
    if inputs.exists():raise RuntimeError('Refusing to overwrite integration input freeze')
    inputs.write_text(json.dumps(frozen,indent=2)+'\n')
    model_artifacts=[{'file':str(f.resolve()),'bytes':f.stat().st_size,'sha256':bench.sha256(f)} for path in [Path(args.model),Path(args.adapter)] for f in sorted(path.iterdir()) if f.suffix in {'.safetensors','.json','.jinja','.txt'}]
    harness_before=bench.sha256(Path(__file__))
    model=LocalModel(args.model,family='hermes',adapter=args.adapter,contract_version='v2');inferences=[];original=model.infer
    def recorded(*a,**kw):
        inf=original(*a,**kw);inferences.append(inf);return inf
    model.infer=recorded;started=datetime.now(timezone.utc).isoformat();rows=[]
    for case in cases:
        state=MediaFixtureState()
        if case['scenario']=='ambiguous_series':state.series.append({**state.series[0],'id':99})
        if case['scenario']=='radarr_unavailable':state.faults['/api/v3/movie']=(503,{'error':'controlled_unavailable'})
        with media_fixture(state) as (native,state):
            router=ConfiguredAdapterRouter(native);executor=BoundedExecutor(router)
            assert router.legacy is None,'Gateway unexpectedly configured'
            # This uninterrupted path uses actual model text as the proposal.
            start=time.perf_counter();probe=model.infer(case['query'],{},max_tokens=160)
            inferred_wall=time.perf_counter()-start
            trace=executor.execute_raw_proposal(probe['raw'],READER,family='hermes',query=case['query'],context={})
            neural_execution_wall=time.perf_counter()-start
            calls_before_runtime=len(inferences)
            runtime=ArrmesRuntimeService(model=model,executor=executor,write_mode='confirm')
            start=time.perf_counter();result=runtime.execute(case['query'],READER,context={});runtime_wall=time.perf_counter()-start
            runtime_inferences=inferences[calls_before_runtime:]
            raw_score=score(probe['raw_unverified'],case['expected'],'hermes');verified=score(probe['raw'],case['expected'],'hermes')
            if case['scenario']=='positive':
                neural_ok=trace.terminal_state=='completed' and trace.proposal==case['expected'] and business_check(case['expected']['name'],trace.observation.data)
                runtime_ok=result['terminal_state']=='completed' and result['proposal']==case['expected'] and business_check(case['expected']['name'],result['observation']['data'])
            elif case['scenario']=='ambiguous_series':
                neural_ok=trace.terminal_state=='clarify';runtime_ok=result['terminal_state']=='clarify'
            else:
                neural_ok=trace.terminal_state=='error';runtime_ok=result['terminal_state']=='error'
            mutations=[r for r in state.calls if r['method']!='GET']
            rows.append({**case,'raw_neural_probe':probe,'raw_exact':raw_score['exact'],'verified_exact':verified['exact'],
                         'neural_executor_terminal':trace.terminal_state,'neural_executor_proposal':trace.proposal,
                         'neural_executor_observation':trace.observation.data if trace.observation else None,
                         'neural_executor_expected_outcome':neural_ok,'runtime_terminal':result['terminal_state'],
                         'runtime_proposal':result['proposal'],'runtime_observation':result.get('observation'),
                         'runtime_routing_source':result.get('routing_source'),'runtime_inference_calls':len(runtime_inferences),
                         'runtime_raw_inferences':runtime_inferences,'runtime_expected_outcome':runtime_ok,
                         'http_get_count':len(state.calls),'http_mutations':len(mutations),
                         'http_paths':[{'method':r['method'],'path':r['path'],'query_keys':sorted(r.get('query',{}))} for r in state.calls],
                         'timing':{'inference_wall_seconds':inferred_wall,'neural_executor_wall_seconds':neural_execution_wall,'runtime_wall_seconds':runtime_wall,'grading_excluded':True}})
            assert not mutations,'Read-only integration attempted a mutation'
    after_artifacts=[{'file':r['file'],'bytes':Path(r['file']).stat().st_size,'sha256':bench.sha256(Path(r['file']))} for r in model_artifacts]
    assert before==hashes(snapshot/'src') and model_artifacts==after_artifacts and harness_before==bench.sha256(Path(__file__))
    positive=[r for r in rows if r['scenario']=='positive']
    receipt={'schema_version':1,'kind':'actual public-model native read integration smoke, not held-out qualification',
             'recorded_at':datetime.now(timezone.utc).isoformat(),'inference_started_at':started,
             'inputs_sha256':bench.sha256(inputs),'inputs_frozen_at':frozen['frozen_at'],
             'public_source_base_commit':bench.git_sha(public_src),'source_file_sha256_before':before,'source_file_sha256_after':hashes(snapshot/'src'),
             'source_snapshot':'native-media-source.jsonl','fixture_sha256':fixture_before,'fixture_source':fixture.read_text(),
             'harness_sha256_before':harness_before,'harness_sha256_after':bench.sha256(Path(__file__)),
             'model_revision':'mlx-community/Qwen3-0.6B-4bit@73e3e38d981303bc594367cd910ea6eb48349da8',
             'adapter_revision':'NorviTech/arrmes-qwen3-0.6b@8a9bcf224e6f9fd6bf238267093c70360ee837cb',
             'model_artifacts_before':model_artifacts,'model_artifacts_after':after_artifacts,
             'generation':{'backend':'mlx','device':'Apple Silicon','family':'hermes','contract':'v2','temperature':0,'thinking':False,'direct_probe_max_tokens':160,'runtime_max_tokens':64,'context':{},'warmup':'none'},
             'hardware':bench.hardware(),'packages':bench.versions(),
             'summary':{'positive_cases':6,'positive_raw_exact':sum(r['raw_exact'] for r in positive),'positive_verified_exact':sum(r['verified_exact'] for r in positive),'positive_neural_executor_success':sum(r['neural_executor_expected_outcome'] for r in positive),'positive_runtime_success':sum(r['runtime_expected_outcome'] for r in positive),'all_expected_outcomes':sum(r['runtime_expected_outcome'] for r in rows),'total_cases':len(rows),'http_mutations':sum(r['http_mutations'] for r in rows)},'rows':rows}
    out=Path(args.output);out.write_text(json.dumps(receipt,indent=2)+'\n')
    snapshot_rows=[{'path':str(f.relative_to(snapshot/'src')),'sha256':bench.sha256(f),'source':f.read_text()} for f in sorted((snapshot/'src').rglob('*.py'))]
    out.with_name('native-media-source.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n' for r in snapshot_rows))
    print(json.dumps(receipt['summary']))
    for r in rows:print(r['id'],r['raw_exact'],r['runtime_terminal'],r['runtime_routing_source'],r['runtime_inference_calls'])
if __name__=='__main__':main()
