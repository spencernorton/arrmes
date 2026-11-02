#!/usr/bin/env python3
"""Measure real authenticated HTTP execute latency against loopback fixtures."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('benchmark_comparison',ROOT/'scripts/benchmark_comparison.py');bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',default=str(ROOT/'src'))
    p.add_argument('--model',required=True);p.add_argument('--adapter');p.add_argument('--label',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    sys.path.insert(0,str(Path(args.source_root).resolve()))
    from arrmes.auth import BearerTokenAuthenticator,ClientIdentity,token_digest,KNOWN_SCOPES
    from arrmes.executor import BoundedExecutor
    from arrmes.model import LocalModel
    from arrmes.server import ArrmesRuntimeService,create_http_server
    from arrmes.sandbox import SandboxCluster,NetworkIsolationGuard
    model=LocalModel(args.model,family='hermes',adapter=args.adapter,contract_version='v2');model._mlx_queue=queue.Queue()
    token='fixture'
    auth=BearerTokenAuthenticator([ClientIdentity('owner-1',KNOWN_SCOPES,token_digest(token))])
    manifest=json.loads((ROOT/'data/overnight-v1/manifest.json').read_text())
    data=[json.loads(l) for l in (ROOT/'data/overnight-v1/routing-heldout.jsonl').read_text().splitlines()]
    ids={'ON1-001','ON1-002','ON1-006','ON1-010','ON1-011','ON1-024','ON1-025','ON1-026'}
    cases=[r for r in data if r['id'] in ids];rows=[];errors=[];warmups=[]
    with tempfile.TemporaryDirectory(prefix='arrmes-http-latency-') as tmp:
        with SandboxCluster(Path(tmp)) as cluster:
            bench.rename_fixture(cluster,manifest['entity_renaming'])
            service=ArrmesRuntimeService(model=model,executor=BoundedExecutor(bench.make_adapter(cluster)),write_mode='confirm')
            server=create_http_server(service,authenticator=auth,host='127.0.0.1',port=0)
            server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
            def send(query):
                payload=json.dumps({'query':query}).encode();req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/execute',data=payload,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
                start=time.perf_counter()
                with urllib.request.urlopen(req,timeout=30) as response:
                    body=json.loads(response.read());status=response.status
                return {'client_wall_seconds':time.perf_counter()-start,'status':status,'body':body}
            def client():
                try:
                    for q in ['Show current Plex activity.','Read DH-Waypoint health.']:
                        warmups.append(send(q))
                    for rep in range(5):
                        for case in cases:
                            cluster.reset()
                            rows.append({'id':case['id'],'repetition':rep,'query':case['query'],**send(case['query'])})
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc))
                finally:model._mlx_queue.put(None)
            with NetworkIsolationGuard(allowed_ports={cluster.port,server.server_port}) as guard:
                worker=threading.Thread(target=client,daemon=True);worker.start()
                model.serve_mlx_loop();worker.join(timeout=2)
            server.shutdown();server.server_close();server_thread.join(timeout=2)
            if errors:raise RuntimeError(';'.join(errors))
            receipt={'schema_version':1,'label':args.label,'recorded_at':datetime.now(timezone.utc).isoformat(),'source_commit':bench.git_sha(Path(args.source_root)),
                     'hardware':bench.hardware(),'packages':bench.versions(),'dataset_sha256':manifest['sha256'],
                     'method':'Actual client POST /v1/execute, SHA256 bearer authentication, model queue on main thread, per-principal ledger, executor and HTTP fixture adapter. 2warmups excluded;8fixed read tasks×5repetitions;server production64token cap. No TLS,remote network,human confirmation or concurrency latency.',
                     'summary':{'samples':len(rows),'client_wall':bench.quantiles([r['client_wall_seconds'] for r in rows]),
                                'server_processing':bench.quantiles([r['body']['latency_ms']/1000 for r in rows]),
                                'http_success':sum(r['status']==200 for r in rows),'external_attempts_blocked':len(guard.blocked_attempts)},
                     'warmups':warmups,'rows':rows}
            Path(args.output).write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n');print(json.dumps(receipt['summary']))

if __name__=='__main__':main()
