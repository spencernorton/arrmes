#!/usr/bin/env python3
"""Reformat benchmark JSON without changing data; optionally retain originals.

Each metadata field and raw row occupies one line for reviewable Git diffs.
The index records original and compact byte hashes; JSON semantic equality is
verified before replacing any file. An optional deterministic ZIP retains the
original bytes, inputs, harnesses, source and rendered reports for the owner.
"""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
import zipfile
ROOT=Path(__file__).resolve().parents[1]

BUNDLE_README='''# Arrmes owner-private measurement evidence

This archive preserves original measurement metadata, frozen inputs, raw outputs,
historical source cuts and the harnesses that produced or rescored them. Local
paths and original private-source fingerprints are intentional. It contains no
model weights, real credentials or production media library data. Public exports
have a separate explicit privacy-transformation ledger; their hashes can differ.

For normal use, install the current reviewed public source or published release
from https://github.com/spencernorton/arrmes separately. This evidence archive is
not an installation bundle. Historical sources and helpers can retain bugs that
were fixed later; exact source cuts are retained to make old measurements auditable.

Read benchmark-overnight-v1-addendum.md and the main report under reports/. The
native-only HTTP efficiency mix, synthetic fixtures, actual Sonarr empty-library
check and neural comparisons are separate evidence. Do not combine their timings
or interpret verifier corrections as correct raw model predictions.

Preserve an untouched copy. Raw/frozen entries under data/ and experiments/ have
read-only Unix permissions in this ZIP (0444; executable launchers use 0555).
Some ZIP extractors ignore these bits. They aid preservation, while SHA-256 values
in bundle-manifest.json check actual content. They do not enforce immutability.
Use disposable copies for diagnostics and never train or tune on frozen inputs.

The archived private scripts/rescore_benchmark_observations.py intentionally
preserves historical code and MUTATES ITS INPUT RECEIPT. Prefer the current public
helper, which writes a new diagnostic copy and refuses source/existing-output
overwrites. Run that current helper on disposable copies and choose a new output
directory; do not execute historical report reproduction commands on originals.
Other retained historical scripts may also rewrite their designated outputs.

The manifest records every payload's byte count, SHA-256 and intended Unix mode.
Frozen source JSONL can be restored with scripts/restore_benchmark_source.py into
a new disposable directory. Match each receipt's exact source/model revisions,
hashes, settings and dependencies when reproducing. No benchmark or platform claim
extends beyond the hardware, task mix and limitations recorded in those receipts.
'''


def archive_mode(name):
 if name.startswith(('data/','experiments/')):
  return 0o555 if '/launchers/' in name else 0o444
 return 0o644


def digest(blob):return hashlib.sha256(blob).hexdigest()


def encode(obj):
 dumps=lambda value:json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
 if not isinstance(obj,dict):return (dumps(obj)+'\n').encode()
 lines=['{'];items=list(obj.items())
 for i,(key,value) in enumerate(items):
  comma=',' if i<len(items)-1 else ''
  if key=='rows' and isinstance(value,list):
   lines.append(dumps(key)+':[')
   lines.extend(dumps(row)+(',' if j<len(value)-1 else '') for j,row in enumerate(value))
   lines.append(']'+comma)
  else:lines.append(dumps(key)+':'+dumps(value)+comma)
 lines.append('}');return ('\n'.join(lines)+'\n').encode()


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive');args=p.parse_args()
 folder=ROOT/'experiments/overnight-v1';index=folder/'receipt-encoding-index.json'
 files=sorted(f for f in folder.glob('*.json') if f!=index)
 originals={f:f.read_bytes() for f in files};records=[];archival={}
 cache=ROOT/'work/benchmarks/original-receipts';cache.mkdir(parents=True,exist_ok=True)
 previous=json.loads(index.read_text()) if index.exists() else None
 for f,blob in originals.items():
  obj=json.loads(blob);compact=encode(obj)
  if json.loads(compact)!=obj:raise RuntimeError('JSON semantic equality failed: '+str(f))
  rec={'file':str(f.relative_to(ROOT)),'original_byte_sha256':digest(blob),'compact_byte_sha256':digest(compact),'original_bytes':len(blob),'compact_bytes':len(compact),'semantic_equality_verified':True}
  if previous:
   prior=next((r for r in previous['records'] if r['file']==rec['file']),None)
   if prior and prior['compact_byte_sha256']==rec['original_byte_sha256']:
    rec['original_byte_sha256']=prior['original_byte_sha256'];rec['original_bytes']=prior['original_bytes']
  cache_file=cache/(rec['original_byte_sha256']+'.json')
  if not cache_file.exists():
   if digest(blob)!=rec['original_byte_sha256']:raise RuntimeError('Original encoding cache missing: '+str(f))
   cache_file.write_bytes(blob)
  archival[f]=cache_file.read_bytes()
  if digest(archival[f])!=rec['original_byte_sha256']:raise RuntimeError('Original cache hash mismatch')
  records.append(rec)
  f.write_bytes(compact)
 manifest={'schema_version':1,'method':'Valid JSON; one top-level metadata field or raw row per line; exact decoded semantic equality checked; input/model outputs and labels unchanged. Original indented bytes, when archived, remain owner-private and retain their paths.','records':records}
 index.write_text(json.dumps(manifest,indent=2)+'\n')
 if args.archive:
  target=Path(args.archive).resolve();target.parent.mkdir(parents=True,exist_ok=True)
  bundle={str(f.relative_to(ROOT)):blob for f,blob in archival.items()}
  for pattern in ['data/overnight-v1/*','experiments/overnight-v1/*.jsonl','scripts/*benchmark*.py','scripts/benchmark*.py','scripts/smoke*.py','scripts/replay_historical_release.py','scripts/score_benchmark_outputs.py','reports/benchmark-overnight-v1*','src/arrmes/**/*.py','tests/test_benchmark_comparison.py','tests/test_native_transfers.py','experiments/portable-v1/**/*','scripts/export*.py','scripts/*portable*.py','docs/portable-runtime.md','Dockerfile','compose*.yaml','.dockerignore','pyproject.toml']:
   for f in ROOT.glob(pattern):
    if f.is_file() and '__pycache__' not in f.parts:bundle[str(f.relative_to(ROOT))]=f.read_bytes()
  bundle[str(index.relative_to(ROOT))]=index.read_bytes()
  bundle['README.md']=BUNDLE_README.encode()
  archive_manifest={'schema_version':2,'kind':'Owner-private benchmark evidence; includes original measurement metadata paths. Public export requires separate explicit privacy rewrite. No model weights, real credentials or production media data.',
                    'preservation_policy':'Raw/frozen entries under data/ and experiments/ use read-only Unix mode 0444 (executable launchers 0555); other entries 0644. Extractor support varies. Hash verification and an untouched original remain necessary. Historical private rescore helper mutates inputs; prefer current public helper on disposable copies.',
                    'files':{name:{'sha256':digest(data),'bytes':len(data),'unix_mode':oct(archive_mode(name))} for name,data in sorted(bundle.items())}}
  bundle['bundle-manifest.json']=(json.dumps(archive_manifest,indent=2)+'\n').encode()
  with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
   for name,data in sorted(bundle.items()):
    info=zipfile.ZipInfo(name,date_time=(2026,11,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3;info.external_attr=(0o100000|archive_mode(name))<<16;z.writestr(info,data)
  print(json.dumps({'archive':str(target),'sha256':digest(target.read_bytes()),'files':len(bundle),'bytes':target.stat().st_size}))
 print(json.dumps({'receipts':len(records),'original_bytes':sum(r['original_bytes'] for r in records),'compact_bytes':sum(r['compact_bytes'] for r in records)}))

if __name__=='__main__':main()
