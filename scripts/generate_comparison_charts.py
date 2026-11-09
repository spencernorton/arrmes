#!/usr/bin/env python3
"""Retired: superseded by export_bench_receipt.py + shared bench_chart.py.

The v5 front-page charts were replaced by the v9 bench-receipt flow. This
shim exports the current receipt and prints the one-line render command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_bench_receipt import build  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "figures")
    args = ap.parse_args()
    receipt = ROOT / "experiments" / "bench_receipt_v9.json"
    receipt.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {receipt}\nRender with:\n  python3 ../shared-ops/scripts/bench_chart.py "
          f"--input {receipt} --output-dir {args.output_dir} --name arrmes_v9_comparison")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
