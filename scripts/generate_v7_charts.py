#!/usr/bin/env python3
"""Retired: v7 figures are frozen historical artifacts — never re-rendered.

The docs/figures/arrmes_v7_*.png files are pinned by SHA-256 in
tests/test_chart_receipt_parity.py (commit c5844ca). This shim exists only to
stop accidental regeneration. Current charts come from
scripts/export_bench_receipt.py + shared-ops bench_chart.py.
"""
import sys

if __name__ == "__main__":
    sys.exit("generate_v7_charts is retired: v7 figures are frozen (see "
             "tests/test_chart_receipt_parity.py). Use export_bench_receipt.py "
             "+ ../shared-ops/scripts/bench_chart.py for current charts.")
