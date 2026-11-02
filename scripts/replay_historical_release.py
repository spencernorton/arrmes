#!/usr/bin/env python3
"""Reproduce a historical model suite with explicit source/model provenance."""
from __future__ import annotations
import argparse,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',required=True);p.add_argument('--model',required=True);p.add_argument('--adapter',required=True);p.add_argument('--cases',default=str(ROOT/'data/sealed-acceptance-v3.jsonl'));p.add_argument('--output',required=True);args=p.parse_args()
 src=Path(args.source_root);hashes={str(f.relative_to(src)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(src.rglob('*.py'))};sys.path.insert(0,str(src.resolve()))
 from arrmes.model import LocalModel
 from arrmes.evaluate import run
 from arrmes.sandbox import NetworkIsolationGuard
 model=LocalModel(args.model,family='hermes',adapter=args.adapter,contract_version='v2')
 with NetworkIsolationGuard(allowed_ports=set()):d=run(model,args.cases,args.output)
 d['qualification_note']='Diagnostic historical-suite replay, not fresh acceptance. Suite was inspected in earlier training cycles; no model/prompt/labels changed for this replay.'
 d['source_file_sha256']=hashes;d['adapter_sha256']=hashlib.sha256(Path(args.adapter,'adapters.safetensors').read_bytes()).hexdigest()
 Path(args.output).write_text(json.dumps(d,indent=2)+'\n')
if __name__=='__main__':main()
