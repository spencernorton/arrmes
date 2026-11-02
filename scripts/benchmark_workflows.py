#!/usr/bin/env python3
"""Run actual model-selected two-turn workflows with observation-derived context."""
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


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',default=str(ROOT/'src'));p.add_argument('--model');p.add_argument('--adapter')
    p.add_argument('--backend',choices=['mlx','deterministic','llama-server'],default='mlx')
    p.add_argument('--device',default='cpu');p.add_argument('--label',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();sys.path.insert(0,str(Path(args.source_root).resolve()))
    from arrmes.executor import BoundedExecutor,SessionEntityLedger
    from arrmes.model import LocalModel,DeterministicBaselineModel
    from arrmes.planner import Principal
    from arrmes.evaluate import score
    from arrmes.sandbox import SandboxCluster,NetworkIsolationGuard
    from urllib.parse import urlparse
    dataset=ROOT/'data/overnight-v1/workflows.jsonl';manifest=json.loads(dataset.with_name('workflow-manifest.json').read_text())
    if bench.sha256(dataset)!=manifest['sha256']:raise RuntimeError('Frozen workflow inputs changed')
    workflows=[json.loads(l) for l in dataset.read_text().splitlines() if l.strip()]
    base_manifest=json.loads(dataset.with_name('manifest.json').read_text())
    if args.backend=='deterministic':model=DeterministicBaselineModel(family='hermes',contract_version='v2')
    elif args.backend=='mlx':model=LocalModel(args.model,family='hermes',adapter=args.adapter,contract_version='v2')
    else:
        from arrmes.backends import create_model
        model=create_model(args.model,family='hermes',contract_version='v2',backend='llama-server',device=args.device,threads=4)
    scopes=frozenset({'media:read','media:manage','requests:read','requests:create','torrents:read','torrents:manage','activity:read','releases:read','releases:classify','pipeline:read'})
    principal=Principal('owner-1',scopes)
    with tempfile.TemporaryDirectory(prefix='arrmes-model-workflows-') as tmp:
        with SandboxCluster(Path(tmp)) as cluster:
            bench.rename_fixture(cluster,base_manifest['entity_renaming'])
            allowed={cluster.port}
            endpoint=getattr(model,'endpoint',None)
            if endpoint:allowed.add(urlparse(endpoint).port)
            rows=[]
            with NetworkIsolationGuard(allowed_ports=allowed) as guard:
                for wf in workflows:
                    cluster.reset();executor=BoundedExecutor(bench.make_adapter(cluster));ledger=SessionEntityLedger('owner-1')
                    step_rows=[];last=None
                    for step in wf['steps']:
                        if step.get('condition')=='missing_count_positive' and (not last or not isinstance(last.get('observation_data'),dict) or last['observation_data'].get('missing_count',0)<=0):
                            step_rows.append({'query':step['query'],'skipped':'condition_not_met_from_actual_observation','expected':step['expected'],'success':False});break
                        ledger.advance_turn();context=ledger.to_model_context()
                        start=time.perf_counter();inf=model.infer(step['query'],context,max_tokens=160)
                        route=time.perf_counter()-start
                        raw=inf.get('raw_unverified',inf['raw']);rawscore=score(raw,step['expected'],'hermes');verified=score(inf['raw'],step['expected'],'hermes')
                        case={**step,'context':context}
                        outcome=bench.perform(inf['raw'],'hermes',case,executor,principal,cluster,ledger=ledger)
                        terminal=step.get('terminal',step['expected']['name'] if step['expected']['name'] in {'clarify','unsupported'} else 'completed')
                        success=verified['exact'] and outcome['terminal']==terminal
                        step_rows.append({'query':step['query'],'expected':step['expected'],'context_from_actual_ledger':context,
                                          'raw_unverified':raw,'system_output':inf['raw'],'raw_exact':rawscore['exact'],
                                          'verified_exact':verified['exact'],'outcome':outcome,'success':success,
                                          'routing_wall_seconds':route,'end_to_end_wall_seconds':time.perf_counter()-start})
                        last=outcome
                    rows.append({'id':wf['id'],'steps':step_rows,'complete':len(step_rows)==len(wf['steps']) and all(x['success'] for x in step_rows)})
            receipt={'schema_version':1,'label':args.label,'recorded_at':datetime.now(timezone.utc).isoformat(),
                     'dataset_sha256':manifest['sha256'],'source_commit':bench.git_sha(Path(args.source_root)),
                     'hardware':bench.hardware(),'packages':bench.versions(),'summary':{'workflows':len(rows),
                        'complete':sum(r['complete'] for r in rows),'turns':sum(len(r['steps']) for r in rows),
                        'raw_exact_turns':sum(x.get('raw_exact',False) for r in rows for x in r['steps']),
                        'verified_exact_turns':sum(x.get('verified_exact',False) for r in rows for x in r['steps'])},
                     'limitations':'6 diagnostic workflows, one run each; not statistically powered qualification. No human/network latency. All context comes from actual prior verified adapter observations; no expected calls are used as model inputs.',
                     'external_attempts_blocked':len(guard.blocked_attempts),'rows':rows}
            Path(args.output).write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n');print(json.dumps(receipt['summary']))
    close=getattr(model,'close',None)
    if callable(close):close()

if __name__=='__main__':main()
