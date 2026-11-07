#!/usr/bin/env python3
"""Data-driven chart generator wrapper delegating to `scripts/generate_v9_charts.py`."""
from __future__ import annotations

from generate_v9_charts import generate_all, load_chart_metrics

__all__ = ["generate_all", "load_chart_metrics"]

if __name__ == "__main__":
    generate_all()
