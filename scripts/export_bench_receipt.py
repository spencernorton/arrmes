#!/usr/bin/env python3
"""Export arrmes v9 chart metrics as bench-receipt/v1 for shared-ops bench_chart.py.

Usage:
  python3 scripts/export_bench_receipt.py --output experiments/bench_receipt_v9.json
  python3 ../shared-ops/scripts/bench_chart.py --input experiments/bench_receipt_v9.json \
      --output-dir docs/figures --name arrmes_v9_comparison
Track A/B values are fractions (0..1); latency/memory stay out of v1.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_v9_charts import load_chart_metrics  # noqa: E402

METRICS = [("pos_track_a", "Positive Track A (raw)"), ("pos_track_b", "Positive Track B (verified)"),
           ("neg_track_b", "Hard-Negative Track B (verified)")]


def build() -> dict:
    panel = load_chart_metrics(ROOT)["chart1"]["panel3_acceptance_v7_models"]
    models = [{
        "name": " ".join(str(row["label"]).replace("★", "").split()),
        "metrics": {k: round(float(row[f"{k}_pct"]) / 100.0, 4) for k, _ in METRICS},
    } for row in panel]
    return {
        "schema_version": 1,
        "title": "Arrmes v9 — acceptance-v7 Track A vs Track B",
        "subtitle": "bench-receipt/v1 via scripts/export_bench_receipt.py (load_chart_metrics)",
        "metrics": [{"key": k, "label": label} for k, label in METRICS],
        "models": models,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, default=ROOT / "experiments/bench_receipt_v9.json")
    args = ap.parse_args()
    args.output.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
