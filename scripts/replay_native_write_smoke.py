"""Replay preserved model outputs through current native fixtures; never new accuracy.

This diagnostic preserves the original receipt and raw scores. It requires the
test extra for controlled HTTP fixtures, but no model or accelerator runtime.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from benchmark_native_write_smoke import ROOT, public_result, run_case, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.receipt.resolve():
        raise SystemExit("Refusing to overwrite original evidence or prior diagnostic")
    encoded = args.receipt.read_bytes()
    original = json.loads(encoded)
    files = list((ROOT / "src/arrmes").rglob("*.py"))
    files += [ROOT / "tests" / name for name in ("test_native_arr_writes.py", "test_native_bitmagnet.py",
                                                "test_native_requests.py", "test_bazarr_subtitles.py")]
    files += [Path(__file__), ROOT / "scripts/benchmark_native_write_smoke.py"]
    before = {file.relative_to(ROOT).as_posix(): sha(file) for file in sorted(files)}
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    rows = []
    for recorded in original["rows"]:
        class Cached:
            family = "hermes"
            backend = "cached_output_not_new_inference"

            def infer(self, *_args, **_kwargs):
                calls = recorded.get("raw_inferences", [])
                if len(calls) != 1:
                    raise RuntimeError("No unique recorded inference for this scenario")
                return deepcopy(calls[0])

        rows.append(run_case(recorded, Cached()))
    if encoded != args.receipt.read_bytes() or any(sha(ROOT / path) != digest for path, digest in before.items()):
        raise RuntimeError("Preserved receipt or source changed during replay")
    neural = [row for row in rows if row["route"] == "neural_v2"]
    result = {"schema_version": 1,
        "kind": "Cached-output regression replay after development; no new inference or held-out acceptance",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "original_receipt_sha256": sha(args.receipt), "original_input_freeze": original.get("freeze"),
        "source_commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "source_file_sha256": before, "summary": {"cases": len(rows),
            "system_expected": sum(row["system_expected"] for row in rows),
            "raw_exact": sum(row.get("raw_exact") is True for row in neural),
            "verifier_exact": sum(row.get("verifier_exact") is True for row in neural),
            "neural_rows": len(neural), "neural_unique_queries": len({row["query"] for row in neural})},
        "rows": public_result(rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"]))


if __name__ == "__main__":
    main()
