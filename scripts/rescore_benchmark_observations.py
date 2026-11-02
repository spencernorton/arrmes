#!/usr/bin/env python3
"""Diagnostic cached Track-C check against frozen pre-inference observations.

Never overwrites source receipts, raw output, expected calls or measured timings.
It preserves the legacy exact-call/terminal score and writes a diagnostic copy
with a stricter business-state score. Public privacy placeholders can affect it.
"""
from __future__ import annotations
import argparse,hashlib,importlib.util,json,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('benchmark_comparison',ROOT/'scripts/benchmark_comparison.py');bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('receipts',nargs='+');p.add_argument('--oracle',default=str(ROOT/'data/overnight-v1/fixture-oracle.json'));p.add_argument('--output-dir',default=str(ROOT/'work/observation-rescores'));args=p.parse_args()
 sys.path.insert(0,str(ROOT/'src'))
 from arrmes.protocol import Call,normalized
 oracle_path=Path(args.oracle);oracle={r['id']:r for r in json.loads(oracle_path.read_text())['rows']}
 output_dir=Path(args.output_dir).resolve()
 for name in args.receipts:
  path=Path(name);original=path.read_bytes();d=json.loads(original);rows=d.get('rows',[])
  if not rows or 'track_C' not in rows[0]:continue
  output=output_dir/path.name
  if output==path.resolve() or output.exists():raise SystemExit('Refusing to overwrite source or existing diagnostic receipt')
  for row in rows:
   c=row['track_C'];expected=normalized(Call(row['expected']['name'],row['expected']['arguments']))
   binding=c.get('proposal')==expected or (c.get('explicit_confirmation_applied') is True and row['track_B']['actual']==expected)
   legacy=row['track_B']['verified_exact'] and binding and c['terminal']=='completed' and oracle[row['id']]['terminal']=='completed'
   c['legacy_exact_call_completed_terminal']=bool(legacy)
   result=bench.compare_observations(c,oracle[row['id']],row['expected']['name'])
   c.update(result);c['successful_workflow']=bool(legacy and result['business_observation_match'])
   row['latency']['measurement_note']='Historical benchmark-instrumented fixture wall includes two expected-answer score() calls between infer and executor. Timing values unchanged. Pure product HTTP measured separately.'
  primary=[r for r in rows if r['repetition']==0];s=d['summary']
  s['legacy_exact_call_completed_terminal_count']=sum(r['track_C']['legacy_exact_call_completed_terminal'] for r in primary)
  s['completed_matching_workflows']=sum(r['track_C']['successful_workflow'] for r in primary)
  s['business_observation_matches']=sum(r['track_C']['business_observation_match'] for r in primary)
  s['warm_end_to_end']['measurement_note']='benchmark-instrumented fixture wall, including expected-answer grading; not pure product request latency'
  d['business_observation_rule']=bench.OBSERVATION_RULE
  d['business_oracle_sha256']=hashlib.sha256(oracle_path.read_bytes()).hexdigest()
  d['timing_method']='Historical benchmark-instrumented fixture wall includes grading. Values retained; actual 40-request HTTP measurement is separate.'
  method='cached diagnostic rescore against frozen business observations; explicit volatile metadata omissions only; no model inference, output, label or timing changes'
  hist=d.setdefault('scoring_history',[])
  if not any(x.get('method')==method for x in hist):hist.append({'at':datetime.now(timezone.utc).isoformat(),'input_receipt_sha256':hashlib.sha256(original).hexdigest(),'method':method})
  d['diagnostic_source_receipt_sha256']=hashlib.sha256(original).hexdigest()
  d['diagnostic_note']='New cached rescore against the selected oracle. Public fixture placeholders can change business matches; original measured receipt is unchanged.'
  output_dir.mkdir(parents=True,exist_ok=True)
  output.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
  print(output.name,s['legacy_exact_call_completed_terminal_count'],s['completed_matching_workflows'])
if __name__=='__main__':main()
