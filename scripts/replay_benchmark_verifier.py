#!/usr/bin/env python3
"""Diagnostic replay of saved raw model outputs through an explicit source tree.

No inference, training, labels or model predictions change. A changed verifier
is never credited as an improved raw model prediction or fresh qualification.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',default=str(ROOT/'src'));p.add_argument('--receipt',required=True);p.add_argument('--output',required=True);args=p.parse_args()
 src=Path(args.source_root);hashes={str(f.relative_to(src)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(src.rglob('*.py'))};sys.path.insert(0,str(src.resolve()))
 from arrmes.model import finalize_output
 from arrmes.evaluate import score
 from arrmes.catalog import WRITE_EFFECT_ACTIONS
 from arrmes.sandbox import NetworkIsolationGuard
 source=Path(args.receipt);old=json.loads(source.read_text());rows=[];family=old.get('family','hermes')
 with NetworkIsolationGuard(allowed_ports=set()):
  for r in old['rows']:
   if r.get('repetition',0)!=0:continue
   raw_unverified=r.get('raw_unverified',r.get('raw'))
   raw=finalize_output(raw_unverified,r['query'],r.get('context'),family,'v2');s=score(raw,r['expected'],family)
   rawscore=score(raw_unverified,r['expected'],family);nonwrite=r['expected']['name'] not in WRITE_EFFECT_ACTIONS
   rows.append({'id':r['id'],'query':r['query'],'expected':r['expected'],'context':r.get('context'),'raw_unverified':raw_unverified,'raw_exact':rawscore['exact'],'system_output':raw,'verified_actual':s['actual'],'verified_exact':s['exact'],'unexpected_write_proposal':bool(nonwrite and s['actual'] and s['actual']['name'] in WRITE_EFFECT_ACTIONS),'nonwrite_opportunity':nonwrite})
 summary={'cases':len(rows),'raw_exact_unchanged':sum(r['raw_exact'] for r in rows),'verified_exact':sum(r['verified_exact'] for r in rows),'nonwrite_opportunities':sum(r['nonwrite_opportunity'] for r in rows),'unexpected_write_proposals':sum(r['unexpected_write_proposal'] for r in rows)}
 d={'schema_version':1,'kind':'post-result diagnostic verifier replay;no new inference/training/labels;not fresh acceptance','recorded_at':datetime.now(timezone.utc).isoformat(),'input_receipt_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'source_file_sha256':hashes,'summary':summary,'rows':rows}
 Path(args.output).write_text(json.dumps(d,indent=2)+'\n');print(json.dumps(summary))
if __name__=='__main__':main()
