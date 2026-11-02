#!/usr/bin/env python3
"""Actual public-model -> runtime -> confirmed native qBittorrent fixture smoke.

This is an integration test, not held-out model qualification. No production
service or credentials are used. Requires tests/ and optional MLX installation.
"""
from __future__ import annotations
from datetime import datetime,timezone
import argparse,hashlib,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model',required=True);p.add_argument('--adapter',required=True);p.add_argument('--output',default=str(ROOT/'experiments/overnight-v1/native-transfer-neural-smoke.json'));args=p.parse_args()
 hashes={str(f.relative_to(ROOT/'src')):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((ROOT/'src').rglob('*.py'))}
 sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
 from arrmes.model import LocalModel
 from arrmes.server import ArrmesRuntimeService
 from arrmes.executor import BoundedExecutor
 from arrmes.service_router import ConfiguredAdapterRouter
 from arrmes.services import ServiceQueryAdapter
 from test_native_transfers import TransferFixtureState,transfer_fixture,OWNER,H1
 model=LocalModel(args.model,family='hermes',adapter=args.adapter,contract_version='v2')
 inferred=[];original_infer=model.infer
 def recording_infer(*a,**kw):
  r=original_infer(*a,**kw);inferred.append(r);return r
 model.infer=recording_infer
 prompts=['Pause the torrent "Fixture torrent"','Resume the torrent "Fixture torrent"','Pause "Fixture torrent" in qBittorrent','Resume "Fixture torrent" in qBittorrent']
 rows=[]
 for version in ['v4.6.7','v5.1.2']:
  for query in prompts:
   pause=query.startswith('Pause');before='downloading' if pause else ('pausedDL' if version.startswith('v4') else 'stoppedDL')
   state=TransferFixtureState(version=version,torrents=[{'hash':H1,'name':'Fixture torrent','state':before}])
   with transfer_fixture(state) as (config,state):
    service=ArrmesRuntimeService(model=model,executor=BoundedExecutor(ConfiguredAdapterRouter(ServiceQueryAdapter({'qbittorrent':config}))),write_mode='confirm')
    inference_before=len(inferred);start=time.perf_counter();initial=service.execute(query,OWNER);route_seconds=time.perf_counter()-start
    assert initial['terminal_state']=='awaiting_confirmation',initial
    assert not any(c['method']=='POST' for c in state.calls)
    cid=initial['plan']['confirm_id'];confirm_start=time.perf_counter();confirmed=service.confirm(cid,OWNER);confirm_seconds=time.perf_counter()-confirm_start
    assert confirmed['terminal_state']=='completed',confirmed
    replay=service.confirm(cid,OWNER)
    assert replay['terminal_state']=='denied',replay
    posts=[c for c in state.calls if c['method']=='POST'];assert len(posts)==1
    expected_endpoint='/api/v2/torrents/'+('pause' if pause else 'resume') if version.startswith('v4') else '/api/v2/torrents/'+('stop' if pause else 'start')
    assert posts[0]['path']==expected_endpoint
    assert posts[0]['form']=={'hashes':[H1]}
    assert len(inferred)==inference_before+1,'Runtime bypassed expected neural route'
    rows.append({'query':query,'application_version':version,'initial_terminal':initial['terminal_state'],'confirmation_terminal':confirmed['terminal_state'],'replay_terminal':replay['terminal_state'],'routing_source':initial.get('routing_source'),'raw_inference':inferred[-1],'route_and_prepare_wall_seconds':route_seconds,'confirmation_and_verification_wall_seconds':confirm_seconds,'end_to_end_excluding_human_seconds':route_seconds+confirm_seconds,'upstream_mutations':len(posts),'post_endpoint':posts[0]['path'],'post_form':posts[0]['form'],'final_state':state.torrents[0]['state'],'postcondition_verified':confirmed['observation']['data'].get('postcondition_verified')})
 d={'schema_version':1,'kind':'actual neural and native integration smoke;not heldout quality qualification','recorded_at':datetime.now(timezone.utc).isoformat(),'public_model_revision':'NorviTech/arrmes-qwen3-0.6b@8a9bcf224e6f9fd6bf238267093c70360ee837cb','source_file_sha256':hashes,'model_artifact_sha256':hashlib.sha256(Path(args.adapter,'adapters.safetensors').read_bytes()).hexdigest(),'summary':{'scenarios':len(rows),'completed':len(rows),'model_inference_calls':len(inferred),'preconfirmation_mutations':0,'mutations_per_confirmed_action':1,'replays_denied':len(rows)},'rows':rows}
 Path(args.output).write_text(json.dumps(d,indent=2)+'\n');print(json.dumps(d['summary']))
if __name__=='__main__':main()
