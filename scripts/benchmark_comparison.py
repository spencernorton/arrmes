#!/usr/bin/env python3
"""Reproducible three-track benchmark. No production endpoints or training.

Accuracy denominators are unique cases (first repetition), not repeated outputs.
All model output text is retained before decoding or verifier correction.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
try:
    import resource
except ImportError:  # Native Windows has no resource module.
    resource = None
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import threading
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def quantiles(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {'n': 0, 'p50_seconds': None, 'p95_seconds': None}
    s = sorted(xs)
    import math
    return {'n': len(s), 'p50_seconds': statistics.median(s),
            'p95_seconds': s[max(0, math.ceil(.95 * len(s)) - 1)], 'min_seconds': s[0], 'max_seconds': s[-1]}


def versions() -> dict[str, str | None]:
    out = {}
    for name in ['mlx-lm', 'mlx', 'transformers', 'jsonschema', 'huggingface-hub']:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def git_sha(path: Path) -> str | None:
    try:
        return subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.SubprocessError):
        return None


class ChildMemoryMonitor:
    """Sample native server RSS; report sampling limits explicitly."""
    def __init__(self, model):
        self.process = getattr(model, '_process', None)
        self.peak = None
        self.samples = 0
        self.stop_event = threading.Event()
        self.thread = None
        if self.process is not None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                value = int(subprocess.check_output(['ps','-o','rss=','-p',str(self.process.pid)], text=True, stderr=subprocess.DEVNULL).strip()) * 1024
                self.peak = max(self.peak or 0, value)
                self.samples += 1
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            self.stop_event.wait(.1)

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        return {'sampled_peak_native_child_rss_bytes':self.peak,'native_child_rss_samples':self.samples,
                'native_child_rss_sampling_seconds':.1 if self.process is not None else None,
                'native_child_memory_note':'RSS sampled after startup, lower bound on true peak; excludes pre-load peak. Python parent RSS reported separately.'}


def hardware() -> dict[str, Any]:
    h = {'os': platform.platform(), 'machine': platform.machine(), 'python': platform.python_version(),
         'logical_cpus': os.cpu_count()}
    if sys.platform == 'darwin':
        for k in ['machdep.cpu.brand_string','hw.memsize']:
            h[k] = subprocess.check_output(['sysctl','-n',k],text=True).strip()
    return h


def rename_fixture(cluster, mapping: dict[str,str]) -> None:
    """Rename synthetic offline entities before inference, keeping fixture shape.

    No production snapshot is loaded. IDs and state are retained solely to make
    API behavior comparable to the already tested synthetic SandboxCluster.
    """
    for path in cluster.baseline_dir.rglob('*.db'):
        with closing(sqlite3.connect(path)) as db, db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] != 'sqlite_sequence']
            for table in tables:
                columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                for col in columns:
                    for rowid,value in db.execute(f'SELECT rowid,"{col}" FROM "{table}"').fetchall():
                        if not isinstance(value,str):
                            continue
                        updated = value
                        for old,new in mapping.items():
                            for a,b in [(old,new),(old.replace(' ','.'),new.replace(' ','.')),
                                        (old.lower().replace(' ',''),new.lower().replace(' ',''))]:
                                updated = updated.replace(a,b)
                        if updated != value:
                            db.execute(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?',(updated,rowid))
                # Align the renamed fictional movie with the independently
                # authored benchmark year, without changing existing entities.
                title_col = next((c for c in columns if c.lower() == 'title'), None)
                year_col = next((c for c in columns if c.lower() == 'year'), None)
                if title_col and year_col:
                    db.execute(f'UPDATE "{table}" SET "{year_col}"=2020 WHERE "{title_col}"=?',('Velvet Meridian',))
    cluster.reset()


def make_adapter(cluster):
    from arrmes.live_adapter import LiveGatewayAdapter
    from arrmes.sandbox.sanitize import SANDBOX_AUTH_IDENTIFIERS as AUTH
    return LiveGatewayAdapter(cluster.base_url, gateway_api_key='fixture',
        service_credentials={'sonarr':AUTH['sonarr'],'radarr':AUTH['radarr'],'lidarr':AUTH['lidarr'],
                             'qbittorrent':AUTH['qbittorrent_sid'],'requests':AUTH['requests_token'],
                             'seer':AUTH['requests_token']}, write_mode='confirm', sandbox=True)


def perform(raw, model_family, case, executor, principal, cluster, *, ledger=None) -> dict[str,Any]:
    from arrmes.catalog import WRITE_EFFECT_ACTIONS
    fault=case.get('fault')
    if fault:
        cluster.inject_fault(fault['path'],fault['status'],fault['count'])
    before=len(cluster.audit_log)
    start=time.perf_counter()
    trace=executor.execute_raw_proposal(raw,principal,family=model_family,query=case['query'],context=case.get('context'),ledger=ledger)
    initial_terminal=trace.terminal_state
    before_confirmation=list(cluster.audit_log[before:])
    preconfirmation_mutations=sum(e['method'] in {'POST','PUT','PATCH','DELETE'} and not str(e['path']).endswith('/armiger/parse') for e in before_confirmation)
    confirmed=False
    if trace.terminal_state == 'awaiting_confirmation' and trace.plan and trace.plan.get('confirm_id'):
        # The benchmark operator explicitly confirms only a schema-valid write
        # that matches the frozen expected semantic call. Never authorize an
        # erroneous write merely because a model proposed one.
        from arrmes.protocol import normalized,Call
        if trace.proposal == normalized(Call(case['expected']['name'],case['expected']['arguments'])):
            trace=executor.confirm_write(trace.plan['confirm_id'],principal,ledger=ledger)
            confirmed=True
    audit=cluster.audit_log[before:]
    mutating=[e for e in audit if e['method'] in {'POST','PUT','PATCH','DELETE'} and not str(e['path']).endswith('/armiger/parse')]
    unexpected=case['expected']['name'] not in WRITE_EFFECT_ACTIONS and bool(mutating)
    obs=trace.observation
    return {'initial_terminal':initial_terminal,'terminal':trace.terminal_state,'proposal':trace.proposal,
            'confirmation_issued':initial_terminal=='awaiting_confirmation','explicit_confirmation_applied':confirmed,
            'preconfirmation_mutations':preconfirmation_mutations,'mutations':len(mutating),'unexpected_write_execution':unexpected,
            'adapter_seconds':time.perf_counter()-start,'steps':trace.steps_executed,
            'observation_status':obs.status if obs else None,'observation_data':obs.data if obs else None,
            'audit':audit}


def oracle(cases, cluster, output: Path) -> dict:
    from arrmes.executor import BoundedExecutor
    from arrmes.planner import Principal
    from arrmes.protocol import Call,encode
    scopes=frozenset({'media:read','media:manage','activity:read','requests:read','requests:create',
                     'releases:classify','releases:read','pipeline:read','torrents:read','torrents:manage'})
    rows=[]
    for case in cases:
        cluster.reset()
        executor=BoundedExecutor(make_adapter(cluster))
        principal=Principal('owner-1',frozenset(case.get('scopes',scopes)))
        outcome=perform(encode(Call(case['expected']['name'],case['expected']['arguments']),'hermes'),'hermes',case,executor,principal,cluster)
        rows.append({'id':case['id'],**outcome})
    receipt={'schema_version':1,'kind':'ground-truth fixture outcomes, expected calls only; no model inference',
             'frozen_before_inference_at':datetime.now(timezone.utc).isoformat(),'rows':rows}
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():
        previous=json.loads(output.read_text())
        a=[(r['id'],r['terminal']) for r in previous['rows']]
        b=[(r['id'],r['terminal']) for r in rows]
        if a!=b:
            raise RuntimeError('Frozen oracle terminal states changed; use new version and report reason')
        return previous
    output.write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n')
    return receipt


def run(args) -> dict:
    # Import after source-root selection, so the exact release's parser,
    # verifier, model and adapters are evaluated without monkeypatching them.
    source_hashes={str(f.relative_to(Path(args.source_root))):sha256(f) for f in sorted(Path(args.source_root).rglob('*.py'))}
    sys.path.insert(0,str(Path(args.source_root).resolve()))
    from arrmes.catalog import WRITE_EFFECT_ACTIONS,contract_hash_v2
    from arrmes.executor import BoundedExecutor
    from arrmes.model import LocalModel,DeterministicBaselineModel
    from arrmes.planner import Principal,verify_proposal
    from arrmes.protocol import Call,decode,encode,InvalidCall,normalized
    from arrmes.sandbox import SandboxCluster,NetworkIsolationGuard
    from arrmes.evaluate import score
    dataset=Path(args.dataset)
    manifest=json.loads(dataset.with_name('manifest.json').read_text())
    if sha256(dataset)!=manifest['sha256']:
        raise RuntimeError('Dataset differs from frozen manifest')
    cases=[json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    if args.limit:
        cases=cases[:args.limit]
    with tempfile.TemporaryDirectory(prefix='arrmes-comparison-') as tmp:
        with SandboxCluster(Path(tmp)) as cluster:
            rename_fixture(cluster,manifest['entity_renaming'])
            frozen_oracle=oracle(cases,cluster,Path(args.oracle))
            expected_terminal={r['id']:r['terminal'] for r in frozen_oracle['rows']}
            if args.freeze_oracle_only:
                return {'oracle':args.oracle,'cases':len(cases),'no_model_loaded':True}
            load_start=time.perf_counter()
            if args.backend=='deterministic':
                model=DeterministicBaselineModel(family=args.family,contract_version='v2')
            elif args.backend=='mlx':
                model=LocalModel(args.model,family=args.family,adapter=args.adapter,contract_version='v2')
            else:
                from arrmes.backends import create_model
                model=create_model(args.model,family=args.family,adapter=args.adapter,contract_version='v2',
                                   backend='llama-server',device=args.device,threads=args.threads)
            load_wall=time.perf_counter()-load_start
            child_memory=ChildMemoryMonitor(model)
            scopes=frozenset({'media:read','media:manage','activity:read','requests:read','requests:create',
                     'releases:classify','releases:read','pipeline:read','torrents:read','torrents:manage'})
            allowed={cluster.port}
            for value in [getattr(model,'endpoint',None),getattr(model,'base_url',None)]:
                if value:
                    port=urlparse(value).port
                    if port:allowed.add(port)
            rows=[]
            # A full timed first request is the cold-prompt sample. A second
            # distinct warmup initializes MLX's static prefix caches; neither
            # warmup contributes accuracy or warm latency denominators.
            with NetworkIsolationGuard(allowed_ports=allowed) as guard:
                warmups=[]
                for q in ['Show the current Plex streaming activity.','Give me DH-Waypoint status.']:
                    start=time.perf_counter();inf=model.infer(q,None,max_tokens=args.max_tokens)
                    warmups.append({'query':q,'wall_seconds':time.perf_counter()-start,'generation_seconds':inf.get('seconds'),'raw_unverified':inf.get('raw_unverified',inf['raw'])})
                for rep in range(args.repetitions):
                    for case in cases:
                        cluster.reset()
                        executor=BoundedExecutor(make_adapter(cluster))
                        principal=Principal('owner-1',frozenset(case.get('scopes',scopes)))
                        e2e_start=time.perf_counter();route_start=time.perf_counter()
                        inf=model.infer(case['query'],case.get('context'),max_tokens=args.max_tokens)
                        route_wall=time.perf_counter()-route_start
                        raw=inf.get('raw_unverified',inf['raw'])
                        raw_score=score(raw,case['expected'],model.family)
                        boundary_score=score(inf['raw'],case['expected'],model.family)
                        outcome=perform(inf['raw'],model.family,case,executor,principal,cluster)
                        e2e=time.perf_counter()-e2e_start
                        exp_is_write=case['expected']['name'] in WRITE_EFFECT_ACTIONS
                        actual=raw_score.get('actual')
                        verified_actual=boundary_score.get('actual')
                        raw_write=bool(actual and actual['name'] in WRITE_EFFECT_ACTIONS)
                        raw_error=not raw_score['exact']
                        caught=raw_error and (verified_actual!=actual or (actual is None and outcome['initial_terminal']=='invalid_proposal'))
                        row={'id':case['id'],'repetition':rep,'category':case['category'],'family_id':case['family_id'],
                             'query':case['query'],'context':case.get('context'),'expected':case['expected'],
                             'raw_unverified':raw,'system_output':inf['raw'],
                             'track_A':{'native_schema_valid':raw_score['schema_valid'],'tool_correct':raw_score['action_correct'],
                                        'arguments_and_entities_exact':raw_score['exact'],'actual':actual,
                                        'unexpected_write_proposal':raw_write and not exp_is_write},
                             'track_B':{'verified_exact':boundary_score['exact'],'actual':verified_actual,
                                        'verifier_changed_call':actual!=verified_actual,'raw_error_caught':caught,
                                        'raw_error_not_caught':raw_error and not caught,
                                        'raw_error_remains_after_verification':raw_error and not boundary_score['exact'],
                                        'appropriate_clarification':case['expected']['name']=='clarify' and boundary_score['exact'],
                                        'appropriate_refusal':case['expected']['name']=='unsupported' and boundary_score['exact'],
                                        'confirmation_issued':outcome['confirmation_issued'],'preconfirmation_mutations':outcome['preconfirmation_mutations'],
                                        'unauthorized_write_execution':outcome['unexpected_write_execution'] or (case.get('scopes') is not None and outcome['mutations']>0)},
                             'track_C':{**outcome,'oracle_terminal':expected_terminal[case['id']],
                                        'matches_oracle_terminal':outcome['terminal']==expected_terminal[case['id']],
                                        'successful_workflow':boundary_score['exact'] and outcome['proposal']==normalized(Call(case['expected']['name'],case['expected']['arguments'])) and outcome['terminal']=='completed' and expected_terminal[case['id']]=='completed'},
                             'latency':{'generation_seconds':inf.get('seconds'),'routing_wall_seconds':route_wall,
                                        'end_to_end_wall_seconds':e2e,'adapter_seconds':outcome['adapter_seconds'],
                                        'includes_confirmation_round_trip':False},
                             'runtime':{k:inf.get(k) for k in ['prompt_tokens','generation_tokens','generation_tps','peak_memory_gb','native_timings','backend','wall_seconds'] if k in inf}}
                        rows.append(row)
                    print(json.dumps({'model':args.label,'repetition':rep,'completed':len(rows)}),flush=True)
            memory_receipt=child_memory.stop()
            close_model=getattr(model,'close',None)
            if callable(close_model):close_model()
            memory_receipt['native_child_peak_rss_bytes']=(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*(1 if sys.platform=='darwin' else 1024) if resource is not None else None) if child_memory.process is not None else None
            memory_receipt['native_child_peak_method']='OS reaped-child high-water RSS; managed llama-server is the only large child; ps samples and sysctl helpers are smaller' if child_memory.process is not None else None
            first=[r for r in rows if r['repetition']==0]
            reads=[r for r in first if r['expected']['name'] not in WRITE_EFFECT_ACTIONS]
            controls=[r for r in first if r['expected']['name'] in {'clarify','unsupported'}]
            executable=[r for r in first if r['track_C']['oracle_terminal']=='completed']
            summary={'unique_cases':len(first),'repetitions':args.repetitions,'latency_samples':len(rows),
                     'raw_native_schema_valid':sum(r['track_A']['native_schema_valid'] for r in first),
                     'raw_tool_correct':sum(r['track_A']['tool_correct'] for r in first),
                     'raw_exact':sum(r['track_A']['arguments_and_entities_exact'] for r in first),
                     'verified_exact':sum(r['track_B']['verified_exact'] for r in first),
                     'verifier_changed_calls':sum(r['track_B']['verifier_changed_call'] for r in first),
                     'raw_errors_caught':sum(r['track_B']['raw_error_caught'] for r in first),
                     'non_write_opportunities':len(reads),'raw_unexpected_write_proposals':sum(r['track_A']['unexpected_write_proposal'] for r in reads),
                     'unsafe_write_executions':sum(r['track_C']['unexpected_write_execution'] for r in reads),
                     'unauthorized_write_executions':sum(r['track_B']['unauthorized_write_execution'] for r in first),
                     'preconfirmation_mutations':sum(r['track_B']['preconfirmation_mutations'] for r in first),
                     'oracle_executable_cases':len(executable),'completed_matching_workflows':sum(r['track_C']['successful_workflow'] for r in executable),
                     'oracle_terminal_matches':sum(r['track_C']['matches_oracle_terminal'] and r['track_B']['verified_exact'] for r in first),
                     'appropriate_controls':sum(r['track_B']['appropriate_clarification'] or r['track_B']['appropriate_refusal'] for r in controls),'control_cases':len(controls),
                     'warm_generation':quantiles([r['latency']['generation_seconds'] for r in rows if isinstance(r['latency']['generation_seconds'],(int,float))]),
                     'warm_routing_wall':quantiles([r['latency']['routing_wall_seconds'] for r in rows]),
                     'warm_end_to_end':quantiles([r['latency']['end_to_end_wall_seconds'] for r in rows]),
                     **memory_receipt,
                     'model_load_wall_seconds':load_wall,'cold_first_request_wall_seconds':warmups[0]['wall_seconds'],
                     'cold_load_plus_first_request_seconds':load_wall+warmups[0]['wall_seconds'],
                     'peak_process_rss_bytes':(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)) if resource is not None else None,
                     'peak_accelerator_allocated_gb':(max((r['runtime'].get('peak_memory_gb') or 0) for r in rows) or None) if args.backend!='deterministic' else None,
                     'vram_separate_from_unified_memory':False if sys.platform=='darwin' else None}
            paths=[Path(args.model)] if args.model else []
            if args.adapter:paths.append(Path(args.adapter))
            artifacts=[]
            for path in paths:
                for f in ([path] if path.is_file() else sorted(path.glob('*.safetensors'))+sorted(path.glob('*.json'))+sorted(path.glob('*.jinja'))):
                    artifacts.append({'file':str(f),'bytes':f.stat().st_size,'sha256':sha256(f)})
            receipt={'schema_version':1,'label':args.label,'backend':args.backend,'family':args.family,'model_revision':args.model_revision,
                     'source_root':str(Path(args.source_root).resolve()),'source_commit':git_sha(Path(args.source_root)),
                     'source_file_sha256':source_hashes,
                     'harness_sha256':sha256(Path(__file__)),
                     'dataset_sha256':manifest['sha256'],'oracle_sha256':sha256(Path(args.oracle)),
                     'recorded_at':datetime.now(timezone.utc).isoformat(),'hardware':hardware(),'packages':versions(),
                     'contract_sha256':contract_hash_v2(),'generation':{'max_output_tokens':args.max_tokens,'temperature':0,'thinking':False,
                         'tool_format_translation':'none; native Hermes or FunctionGemma parser; output repair/verifier only in track B',
                         'context_limit':'model native for MLX;4096 for native server','warmup':'2 distinct queries; full first prefill then prefix initialization; both excluded'},
                     'memory_note':'Process RSS high-water mark includes Python/runtime and model; MLX allocated peak is unified-memory allocation, not dedicated VRAM. Native llama-server child RSS must be measured separately.',
                     'network':{'allowed_loopback_ports':sorted(allowed),'external_attempts_blocked':len(guard.blocked_attempts)},
                     'artifacts':artifacts,'artifact_bytes':sum(x['bytes'] for x in artifacts),'warmups':warmups,'summary':summary,'rows':rows}
            output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n')
            close=getattr(model,'close',None)
            if callable(close):close()
            print(json.dumps(summary,indent=2),flush=True)
            return receipt


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',default=str(ROOT/'data/overnight-v1/routing-heldout.jsonl'))
    p.add_argument('--oracle',default=str(ROOT/'data/overnight-v1/fixture-oracle.json'))
    p.add_argument('--source-root',default=str(ROOT/'src'))
    p.add_argument('--model');p.add_argument('--adapter');p.add_argument('--label',required=True)
    p.add_argument('--model-revision',default='local-artifact-hashes-in-receipt')
    p.add_argument('--backend',choices=['mlx','deterministic','llama-server'],default='mlx')
    p.add_argument('--family',choices=['hermes','functiongemma'],default='hermes')
    p.add_argument('--device',default='cpu');p.add_argument('--threads',type=int,default=4)
    p.add_argument('--output',required=True);p.add_argument('--repetitions',type=int,default=3)
    p.add_argument('--max-tokens',type=int,default=160);p.add_argument('--limit',type=int)
    p.add_argument('--freeze-oracle-only',action='store_true')
    args=p.parse_args()
    if not 1<=args.repetitions<=20: p.error('repetitions must be in 1..20')
    run(args)

if __name__=='__main__':main()
