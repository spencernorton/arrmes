#!/usr/bin/env python3
"""Retired shim: v8 charts were a forward to v9; both now use bench_chart.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from generate_v9_charts import generate_all
    generate_all()
