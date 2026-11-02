#!/usr/bin/env python3
"""Model-selected two-turn diagnostics with fingerprinted artifacts and oracle."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('benchmark_comparison',ROOT/'scripts/benchmark_comparison.py')
bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)


def source_hashes(root):
    return {str(f.relative_to(root)):bench.sha256(f) for f in sorted(root.rglob('*.py'))}


def artifact_hashes(model, adapter):
    out=[]
    for kind,value in [('model',model),('adapter',adapter)]:
        if not value:continue
        path=Path(value)
        files=[path] if path.is_file() else sorted(f for f in path.iterdir() if f.suffix in {'.json','.safetensors','.jinja','.model','.txt'})
        for f in files:
            out.append({'kind':kind,'file':str(f.resolve()),'bytes':f.stat().st_size,'sha256':bench.sha256(f)})
    return out


def freeze_oracle(workflows, cluster, principal, source_before, dataset_sha, output):
    """Freeze expected-call observations before any model load or generation."""
    from arrmes.executor import BoundedExecutor,SessionEntityLedger
    from arrmes.protocol import Call,encode
    if output.exists():
        frozen=json.loads(output.read_text())
        if frozen['dataset_sha256']!=dataset_sha or frozen['source_file_sha256']!=source_before:
            raise RuntimeError('Existing workflow oracle belongs to a different frozen input/source cut')
        return frozen
    rows=[]
    for workflow in workflows:
        cluster.reset();executor=BoundedExecutor(bench.make_adapter(cluster));ledger=SessionEntityLedger(principal.subject)
        steps=[];last=None
        for step in workflow['steps']:
            if step.get('condition')=='missing_count_positive' and (not last or (last.get('observation_data') or {}).get('missing_count',0)<=0):
                steps.append({'skipped':'oracle_condition_not_met'});break
            ledger.advance_turn();context=ledger.to_model_context()
            case={**step,'context':context}
            last=bench.perform(encode(Call(step['expected']['name'],step['expected']['arguments']),'hermes'),'hermes',case,executor,principal,cluster,ledger=ledger)
            steps.append(last)
        rows.append({'id':workflow['id'],'steps':steps})
    frozen={'schema_version':1,'kind':'expected-call controlled fixture oracle, frozen before model load/inference',
            'frozen_at':datetime.now(timezone.utc).isoformat(),'dataset_sha256':dataset_sha,
            'source_file_sha256':source_before,'business_observation_rule':bench.OBSERVATION_RULE,'rows':rows}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(frozen,indent=2)+'\n')
    return frozen


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',default=str(ROOT/'src'));p.add_argument('--model');p.add_argument('--adapter')
    p.add_argument('--backend',choices=['mlx','deterministic','llama-server'],default='mlx')
    p.add_argument('--device',default='cpu');p.add_argument('--threads',type=int,default=4)
    p.add_argument('--model-revision',required=True);p.add_argument('--adapter-revision')
    p.add_argument('--oracle',default=str(ROOT/'data/overnight-v1/workflow-oracle-v2.json'))
    p.add_argument('--label',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();src=Path(args.source_root).resolve()
    source_before=source_hashes(src)
    harness_paths=[Path(__file__).resolve(),ROOT/'scripts/benchmark_comparison.py']
    harness_before={str(f.relative_to(ROOT)):bench.sha256(f) for f in harness_paths}
    artifacts_before=artifact_hashes(args.model,args.adapter)
    sys.path.insert(0,str(src))
    from arrmes.executor import BoundedExecutor,SessionEntityLedger
    from arrmes.model import LocalModel,DeterministicBaselineModel
    from arrmes.planner import Principal
    from arrmes.protocol import Call,normalized
    from arrmes.evaluate import score
    from arrmes.sandbox import SandboxCluster,NetworkIsolationGuard
    from urllib.parse import urlparse
    dataset=ROOT/'data/overnight-v1/workflows.jsonl';manifest=json.loads(dataset.with_name('workflow-manifest.json').read_text())
    if bench.sha256(dataset)!=manifest['sha256']:raise RuntimeError('Frozen workflow inputs changed')
    workflows=[json.loads(l) for l in dataset.read_text().splitlines() if l.strip()]
    base_manifest=json.loads(dataset.with_name('manifest.json').read_text())
    scopes=frozenset({'media:read','media:manage','requests:read','requests:create','torrents:read','torrents:manage','activity:read','releases:read','releases:classify','pipeline:read'})
    principal=Principal('owner-1',scopes);load_wall=None
    with tempfile.TemporaryDirectory(prefix='arrmes-model-workflows-') as tmp:
        with SandboxCluster(Path(tmp)) as cluster:
            bench.rename_fixture(cluster,base_manifest['entity_renaming'])
            with NetworkIsolationGuard(allowed_ports={cluster.port}):
                frozen=freeze_oracle(workflows,cluster,principal,source_before,manifest['sha256'],Path(args.oracle))
            expected_outcomes={r['id']:r['steps'] for r in frozen['rows']}
            load_start=time.perf_counter()
            if args.backend=='deterministic':model=DeterministicBaselineModel(family='hermes',contract_version='v2')
            elif args.backend=='mlx':model=LocalModel(args.model,family='hermes',adapter=args.adapter,contract_version='v2')
            else:
                from arrmes.backends import create_model
                model=create_model(args.model,family='hermes',contract_version='v2',backend='llama-server',device=args.device,threads=args.threads)
            load_wall=time.perf_counter()-load_start;inference_started_at=datetime.now(timezone.utc).isoformat()
            allowed={cluster.port}
            endpoint=getattr(model,'endpoint',None)
            if endpoint:allowed.add(urlparse(endpoint).port)
            rows=[]
            try:
                with NetworkIsolationGuard(allowed_ports=allowed) as guard:
                    for wf in workflows:
                        cluster.reset();executor=BoundedExecutor(bench.make_adapter(cluster));ledger=SessionEntityLedger(principal.subject)
                        step_rows=[];last=None
                        for index,step in enumerate(wf['steps']):
                            if step.get('condition')=='missing_count_positive' and (not last or not isinstance(last.get('observation_data'),dict) or last['observation_data'].get('missing_count',0)<=0):
                                step_rows.append({'query':step['query'],'skipped':'condition_not_met_from_actual_observation','expected':step['expected'],'success':False});break
                            ledger.advance_turn();context=ledger.to_model_context()
                            start=time.perf_counter();inf=model.infer(step['query'],context,max_tokens=160)
                            route=time.perf_counter()-start
                            case={**step,'context':context}
                            outcome=bench.perform(inf['raw'],'hermes',case,executor,principal,cluster,ledger=ledger)
                            execution_wall=time.perf_counter()-start
                            grading_start=time.perf_counter();raw=inf.get('raw_unverified',inf['raw'])
                            rawscore=score(raw,step['expected'],'hermes');verified=score(inf['raw'],step['expected'],'hermes')
                            grading_wall=time.perf_counter()-grading_start
                            terminal=step.get('terminal',step['expected']['name'] if step['expected']['name'] in {'clarify','unsupported'} else 'completed')
                            semantic=outcome['proposal']==normalized(Call(step['expected']['name'],step['expected']['arguments']))
                            observation=bench.compare_observations(outcome,expected_outcomes[wf['id']][index],step['expected']['name'])
                            success=verified['exact'] and semantic and outcome['terminal']==terminal and observation['business_observation_match']
                            step_rows.append({'query':step['query'],'expected':step['expected'],'context_from_actual_ledger':context,
                                              'raw_unverified':raw,'system_output':inf['raw'],'raw_exact':rawscore['exact'],
                                              'verified_exact':verified['exact'],'outcome':outcome,'exact_executed_call':semantic,
                                              'oracle_comparison':observation,'success':success,
                                              'routing_wall_seconds':route,'end_to_end_wall_seconds':execution_wall,
                                              'grading_seconds_excluded':grading_wall})
                            last=outcome
                        rows.append({'id':wf['id'],'steps':step_rows,'complete':len(step_rows)==len(wf['steps']) and all(x['success'] for x in step_rows)})
            finally:
                close=getattr(model,'close',None)
                if callable(close):close()
            source_after=source_hashes(src);artifacts_after=artifact_hashes(args.model,args.adapter)
            harness_after={str(f.relative_to(ROOT)):bench.sha256(f) for f in harness_paths}
            if source_before!=source_after or artifacts_before!=artifacts_after or harness_before!=harness_after:
                raise RuntimeError('Source, model artifacts or harness changed during workflow evaluation')
            receipt={'schema_version':2,'label':args.label,'recorded_at':datetime.now(timezone.utc).isoformat(),
                     'kind':'fingerprinted rerun of already inspected diagnostic workflows; not new acceptance',
                     'dataset_sha256':manifest['sha256'],'source_commit':None,'source_root':str(src),
                     'source_file_sha256_before':source_before,'source_file_sha256_after':source_after,
                     'harness_sha256_before':harness_before,'harness_sha256_after':harness_after,
                     'model_artifacts_before':artifacts_before,'model_artifacts_after':artifacts_after,
                     'model_revision':args.model_revision,'adapter_revision':args.adapter_revision,
                     'backend':args.backend,'device':('apple-metal-unified-memory' if args.backend=='mlx' else args.device),
                     'generation':{'family':'hermes','contract':'v2','max_output_tokens':160,'temperature':0,'thinking':False,'repetitions':1,'context':'model native config for MLX; 4096 native server','warmup':'none; first workflow includes cold prompt'},
                     'model_load_wall_seconds':load_wall,'inference_started_at':inference_started_at,
                     'oracle_frozen_at':frozen['frozen_at'],'oracle_sha256':bench.sha256(Path(args.oracle)),
                     'business_observation_rule':bench.OBSERVATION_RULE,
                     'hardware':bench.hardware(),'packages':bench.versions(),'summary':{'workflows':len(rows),
                        'complete':sum(r['complete'] for r in rows),'turns':sum(len(r['steps']) for r in rows),
                        'raw_exact_turns':sum(x.get('raw_exact',False) for r in rows for x in r['steps']),
                        'verified_exact_turns':sum(x.get('verified_exact',False) for r in rows for x in r['steps'])},
                     'limitations':'6 previously inspected diagnostic workflows. No human/remote-network latency. Grading occurs after elapsed capture. Context derives only from actual observations, never oracle observations or expected calls.',
                     'external_attempts_blocked':len(guard.blocked_attempts),'rows':rows}
            Path(args.output).write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n');print(json.dumps(receipt['summary']))

if __name__=='__main__':main()
