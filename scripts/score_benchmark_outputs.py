#!/usr/bin/env python3
"""Add explicitly separate full-JSON format-translation scores to raw receipts.

Does not re-run inference, alter native scores, train, repair arguments or search
for JSON substrings. A complete JSON object (optionally in the exact native wrapper) with only
name/arguments is accepted; valid serialized JSON arguments are decoded once
and validated against the same strict schema. Native wire scores remain intact.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
import re
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def canonical_json_call(raw: str):
    from arrmes.protocol import validate_call
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate JSON key')
            out[k]=v
        return out
    wrapper=re.fullmatch(r'\s*<tool_call>\s*(.*?)\s*</tool_call>\s*',raw,re.S)
    body=wrapper.group(1) if wrapper else raw
    obj=json.loads(body,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    if not isinstance(obj,dict) or set(obj)!={'name','arguments'}:
        raise ValueError('not one canonical call object')
    arguments=obj['arguments']
    if isinstance(arguments,str):
        arguments=json.loads(arguments,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    return validate_call(obj['name'],arguments)


def rescore(path: Path) -> dict:
    from arrmes.protocol import decode,encode,InvalidCall
    from arrmes.evaluate import score
    d=json.loads(path.read_text());rows=d.get('rows',[])
    if not rows or 'track_A' not in rows[0]:return d
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    for r in rows:
        raw=r['raw_unverified'];a=r['track_A'];b=r['track_B'];c=r['track_C']
        translated=False
        try:
            call=decode(raw,d['family'])
        except (InvalidCall,ValueError):
            try:
                call=canonical_json_call(raw);translated=True
            except (ValueError,TypeError,InvalidCall):
                call=None
        result=score(encode(call,d['family']),r['expected'],d['family']) if call else {'schema_valid':False,'action_correct':False,'exact':False,'actual':None}
        a['format_translation_applied']=translated
        a['translated_schema_valid']=result['schema_valid']
        a['translated_tool_correct']=result['action_correct']
        a['translated_arguments_and_entities_exact']=result['exact']
        a['translated_actual']=result['actual']
        # Correct initial harness overcounting wrong model abstentions as catches.
        raw_error=not a['arguments_and_entities_exact']
        caught=raw_error and (a['actual']!=b['actual'] or (a['actual'] is None and c['initial_terminal']=='invalid_proposal'))
        b['raw_error_caught']=caught
        b['raw_error_not_caught']=raw_error and not caught
        b['raw_error_remains_after_verification']=raw_error and not b['verified_exact']
    primary=[r for r in rows if r['repetition']==0]
    s=d['summary'];s['format_translations']=sum(r['track_A']['format_translation_applied'] for r in primary)
    s['translated_raw_schema_valid']=sum(r['track_A']['translated_schema_valid'] for r in primary)
    s['translated_raw_tool_correct']=sum(r['track_A']['translated_tool_correct'] for r in primary)
    s['translated_raw_exact']=sum(r['track_A']['translated_arguments_and_entities_exact'] for r in primary)
    s['raw_errors_caught']=sum(r['track_B']['raw_error_caught'] for r in primary)
    if d['backend']=='deterministic':s['peak_accelerator_allocated_gb']=None
    note='whole-output single JSON-call translation, with exact native wrapper and valid serialized-JSON arguments accepted; no substring extraction or argument repair; strict native and system scores unchanged; raw-error catch metric repaired to exclude preexisting incorrect abstentions; no inference or label changes'
    hist=d.setdefault('scoring_history',[])
    if not hist or hist[-1].get('method')!=note:
        hist.append({'at':datetime.now(timezone.utc).isoformat(),'input_receipt_sha256':before,'method':note})
    path.write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n')
    return d


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('receipts',nargs='+');p.add_argument('--source-root',default=str(ROOT/'src'));args=p.parse_args()
    sys.path.insert(0,str(Path(args.source_root).resolve()))
    for f in args.receipts:
        d=rescore(Path(f))
        if 'translated_raw_exact' in d.get('summary',{}):print(f,d['summary']['raw_exact'],d['summary']['translated_raw_exact'])

if __name__=='__main__':main()
