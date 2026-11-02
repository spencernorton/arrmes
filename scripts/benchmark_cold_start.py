#!/usr/bin/env python3
"""Measure process launch through first parsed route, with fresh processes."""
from __future__ import annotations
import argparse,json,subprocess,sys,time
try:
 import resource
except ImportError:
 resource=None
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',default=str(ROOT/'src'));p.add_argument('--model',required=True);p.add_argument('--adapter');p.add_argument('--backend',choices=['mlx','llama-server'],default='mlx');p.add_argument('--device',default='cpu');p.add_argument('--repetitions',type=int,default=3);p.add_argument('--output',required=True);p.add_argument('--child',action='store_true');args=p.parse_args()
 if args.child:
  sys.path.insert(0,str(Path(args.source_root).resolve()))
  from arrmes.backends import create_model
  from arrmes.sandbox import NetworkIsolationGuard
  from urllib.parse import urlparse
  start=time.perf_counter();model=create_model(args.model,family='hermes',adapter=args.adapter,contract_version='v2',backend=args.backend,device=args.device,threads=4)
  loaded=time.perf_counter()-start;allowed=set();endpoint=getattr(model,'endpoint',None)
  if endpoint:allowed.add(urlparse(endpoint).port)
  with NetworkIsolationGuard(allowed_ports=allowed):inf=model.infer('Show the current Plex streaming activity.',max_tokens=160)
  print(json.dumps({'event':'route_ready','load_seconds':loaded,'generation_seconds':inf.get('seconds'),'raw_unverified':inf.get('raw_unverified',inf['raw'])}),flush=True)
  close=getattr(model,'close',None)
  if callable(close):close()
  peak=(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)) if resource is not None else None
  child_peak=(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*(1 if sys.platform=='darwin' else 1024)) if resource is not None else None
  print(json.dumps({'event':'resources','process_peak_rss_bytes':peak,'native_child_peak_rss_bytes':child_peak if args.backend=='llama-server' else None}),flush=True);return
 rows=[]
 for i in range(args.repetitions):
  command=[sys.executable,str(Path(__file__).resolve()),'--source-root',args.source_root,'--model',args.model,'--backend',args.backend,'--device',args.device,'--output',args.output,'--child']
  if args.adapter:command+=['--adapter',args.adapter]
  start=time.perf_counter();proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  line=proc.stdout.readline();elapsed=time.perf_counter()-start
  row=json.loads(line);tail,errors=proc.communicate(timeout=120)
  if proc.returncode:raise RuntimeError(errors[-1000:])
  row.update(json.loads(tail.strip().splitlines()[-1]));row.pop('event',None);row['launch_to_first_route_seconds']=elapsed;row['repetition']=i;rows.append(row)
 output={'schema_version':1,'backend':args.backend,'model':args.model,'adapter':args.adapter,'repetitions':args.repetitions,'method':'Fresh Python+native runtime process for each repetition. Parent monotonic wall to the flushed child route_ready event includes interpreter startup, imports, local model loading, first full prompt and route verification. Teardown/exit excluded; resource high-water measurements follow teardown. OS file cache not flushed; model downloads excluded. One fixed query; this is startup, not quality evaluation.','rows':rows}
 Path(args.output).write_text(json.dumps(output,indent=2)+'\n');print(json.dumps(rows))
if __name__=='__main__':main()
