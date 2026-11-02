#!/usr/bin/env python3
"""Restore a hash-checked source snapshot for an explicit benchmark replay.

Public snapshots are privacy exports and retain both measured-original and
exported source hashes. Validation here proves the exported bytes only.
"""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path,PurePosixPath
import tempfile,shutil
ROOT=Path(__file__).resolve().parents[1]


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--snapshot',default=str(ROOT/'experiments/overnight-v1/frozen-source.jsonl'));p.add_argument('--output',required=True);args=p.parse_args()
 target=Path(args.output).resolve()
 if target.exists():raise SystemExit('Refusing to overwrite an existing source directory')
 rows=[json.loads(l) for l in Path(args.snapshot).read_text().splitlines() if l.strip()];seen=set()
 for row in rows:
  path=PurePosixPath(row['path'])
  if '\\' in row['path'] or ':' in row['path'] or path.is_absolute() or '..' in path.parts or path.parts[0]!='arrmes' or path.suffix!='.py' or str(path) in seen:raise SystemExit('Invalid or duplicate source snapshot path')
  seen.add(str(path))
  if hashlib.sha256(row['source'].encode()).hexdigest()!=row['sha256']:raise SystemExit('Source snapshot SHA mismatch')
 target.parent.mkdir(parents=True,exist_ok=True)
 tmp=Path(tempfile.mkdtemp(prefix='.arrmes-source-',dir=target.parent))
 try:
  for row in rows:
   f=tmp/row['path'];f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(row['source'].encode('utf-8'))
  tmp.rename(target)
 except BaseException:
  shutil.rmtree(tmp,ignore_errors=True);raise
 exported=any('export_note' in r for r in rows)
 print(json.dumps({'source_root':str(target),'files':len(rows),'privacy_export':exported,'note':'Public privacy-exported source; hashes verify exported bytes, not original measurement bytes. See docs/receipt-export.json.' if exported else 'Exact source from original measurement cut. Set benchmark --source-root to this directory.'}))
if __name__=='__main__':main()
