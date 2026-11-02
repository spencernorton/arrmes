#!/usr/bin/env python3
"""Exercise a real typed API-client alternative on the same controlled fixtures.

This baseline is given structured operation/arguments. It is NOT a natural-
language router and is excluded from model accuracy and routing-latency charts.
Install its optional benchmark-only dependency separately: pyarr==6.9.0.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('benchmark_comparison',ROOT/'scripts/benchmark_comparison.py');bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dependency-path');p.add_argument('--output',default=str(ROOT/'experiments/overnight-v1/pyarr-reads.json'));args=p.parse_args()
    if args.dependency_path:sys.path.insert(0,str(Path(args.dependency_path).resolve()))
    sys.path.insert(0,str(ROOT/'src'))
    from pyarr import Sonarr,Radarr
    from arrmes.sandbox import SandboxCluster,NetworkIsolationGuard
    from arrmes.sandbox.sanitize import SANDBOX_AUTH_IDENTIFIERS as AUTH
    manifest=json.loads((ROOT/'data/overnight-v1/manifest.json').read_text());rows=[]
    with tempfile.TemporaryDirectory(prefix='arrmes-pyarr-read-') as tmp:
        with SandboxCluster(Path(tmp)) as cluster:
            bench.rename_fixture(cluster,manifest['entity_renaming'])
            with NetworkIsolationGuard(allowed_ports={cluster.port}) as guard:
                sonarr=Sonarr('127.0.0.1',AUTH['sonarr'],port=cluster.port,tls=False,base_path='/sonarr',api_ver='v3')
                radarr=Radarr('127.0.0.1',AUTH['radarr'],port=cluster.port,tls=False,base_path='/radarr',api_ver='v3')
                for rep in range(10):
                    for op in ['tv_status','movie_status','tv_missing']:
                        start=time.perf_counter()
                        if op=='tv_status':
                            result=[r for r in sonarr.series.get() if r['title']=='Quartz Orchard'];ok=len(result)==1
                        elif op=='movie_status':
                            result=[r for r in radarr.movie.get() if r['title']=='Velvet Meridian' and r['year']==2020];ok=len(result)==1
                        else:
                            series=[r for r in sonarr.series.get() if r['title']=='Quartz Orchard'];series_id=series[0]['id'];result=[]
                            for page in range(1,6):
                                data=sonarr.wanted.get(page=page,page_size=2,seriesId=series_id)
                                result.extend(data['records'])
                                if page*2>=data['totalRecords']:break
                            ok=bool(result) and all(r['seriesId']==series_id for r in result)
                        rows.append({'operation':op,'repetition':rep,'seconds':time.perf_counter()-start,'success':ok,'records':len(result)})
                audit=cluster.audit_log
                assert all(r['method']=='GET' for r in audit),'Read baseline attempted a mutation'
            d={'schema_version':1,'kind':'structured read-client baseline;no NLP;not scored as model accuracy',
               'package':'pyarr','version':importlib.metadata.version('pyarr'),'source':'https://github.com/totaldebug/pyarr',
               'recorded_at':datetime.now(timezone.utc).isoformat(),'hardware':bench.hardware(),
               'summary':{'requests':len(rows),'successful':sum(r['success'] for r in rows),'latency':bench.quantiles([r['seconds'] for r in rows]),'http_mutations':0,'blocked_external_attempts':len(guard.blocked_attempts)},
               'method':'Same synthetic Sonarr/Radarr fixtures and fictional entities as model trackC;gold semantic operation/title supplied directly;client performs exact lookup and bounded pagination;no model,prompt,confirmation or NL selection included.',
               'rows':rows,'http_audit':audit}
            Path(args.output).write_text(json.dumps(d,indent=2)+'\n');print(json.dumps(d['summary']))

if __name__=='__main__':main()
