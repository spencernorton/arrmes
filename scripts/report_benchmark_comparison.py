#!/usr/bin/env python3
"""Retired chart body: overnight-v1 comparison charts moved to bench_chart.py.

The experiments/overnight-v1 receipts are not committed, so the old standalone
HTML/PNG report cannot be regenerated from this checkout. When those receipts
are present, this shim emits a bench-receipt/v1 (raw / translated / verified
fractions) and prints the shared render command. The historical reports under
reports/ are untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "experiments" / "overnight-v1"
PRIMARY = ["released-arrmes", "qwen-base", "functiongemma-base", "granite350m",
           "granite-bf16", "qwen1.7b", "qwen4b", "deterministic"]
METRICS = [("raw", "Raw exact"), ("translated", "Translated exact"), ("verified", "Verified exact")]


def build() -> dict:
    models = []
    for key in PRIMARY:
        p = DATA / f"{key}.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))["summary"]
        n = float(s["unique_cases"])
        models.append({"name": key, "metrics": {
            "raw": s["raw_exact"] / n,
            "translated": s.get("translated_raw_exact", s["raw_exact"]) / n,
            "verified": s["verified_exact"] / n}})
    if not models:
        raise SystemExit(f"report_benchmark_comparison: no receipts under {DATA} "
                         "(overnight-v1 is not committed); nothing to chart.")
    return {"schema_version": 1, "title": "Arrmes overnight-v1 comparison",
            "metrics": [{"key": k, "label": label} for k, label in METRICS], "models": models}


def main() -> int:
    out = DATA / "bench_receipt.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}\nRender with:\n  python3 ../shared-ops/scripts/bench_chart.py "
          f"--input {out} --output-dir {ROOT / 'docs' / 'figures'} --name arrmes_overnight_v1_comparison")
    return 0


if __name__ == "__main__":
    sys.exit(main())
