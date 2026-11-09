"""Smoke: adapter -> bench-receipt/v1 -> shared bench_chart PNG. Skips without the shared tool."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _tool() -> Path | None:
    for c in [ROOT.parent / "shared-ops" / "scripts" / "bench_chart.py",
              ROOT.parent / "_tmp_bench_shared-ops" / "scripts" / "bench_chart.py"]:
        if c.is_file():
            return c
    return None


def test_adapter_renders(tmp_path: Path) -> None:
    tool = _tool()
    if tool is None:
        pytest.skip("shared-ops bench_chart.py not found (sibling checkout required)")
    from export_bench_receipt import build
    spec = importlib.util.spec_from_file_location("bench_chart", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    receipt = build()
    mod._validate(receipt)
    png, pdf = mod.render(receipt, tmp_path, "smoke")
    assert png.is_file() and png.stat().st_size > 1024 and pdf.is_file()
