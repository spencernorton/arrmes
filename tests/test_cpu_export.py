"""Optional CPU conversion codec tests (run in the separate export environment)."""

import importlib.util
import json
from pathlib import Path

import pytest

torch = pytest.importorskip("torch", reason="CPU export has an optional torch environment")
safetensors = pytest.importorskip("safetensors.torch")
spec = importlib.util.spec_from_file_location("export_mlx_cpu", Path(__file__).parents[1] / "scripts/export_mlx_cpu_gguf.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def test_affine_layout_matches_independent_low_lane_reference():
    # Two explicit uint32 words encode 0..15, repeated across one group of64.
    packed = torch.tensor([[0x76543210, 0xFEDCBA98] * 4], dtype=torch.uint32)
    scales = torch.tensor([[0.5]], dtype=torch.bfloat16)
    biases = torch.tensor([[-3]], dtype=torch.bfloat16)
    actual = export.affine_dequantize_4bit(packed, scales, biases)
    expected = torch.tensor([[n / 2 - 3 for n in range(16)] * 4], dtype=torch.bfloat16)
    assert torch.equal(actual, expected)
    assert torch.equal(actual, export.affine_dequantize_4bit(packed, scales, biases, row_chunk=1))


def test_affine_layout_rejects_incompatible_metadata():
    with pytest.raises(ValueError, match="group64"):
        export.affine_dequantize_4bit(torch.zeros((1, 4), dtype=torch.uint32), torch.ones((1, 1)), torch.ones((1, 1)))
    with pytest.raises(ValueError, match="types/shapes"):
        export.affine_dequantize_4bit(torch.zeros((1, 8)), torch.ones((1, 1)), torch.ones((1, 1)))


def test_cpu_merge_uses_mlx_scale_and_transpose_without_rank_division(tmp_path):
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    base.mkdir()
    adapter.mkdir()
    (base / "config.json").write_text(json.dumps({"model_type": "qwen3", "quantization": {"bits": 4, "group_size": 64}}))
    (adapter / "adapter_config.json").write_text(json.dumps({"fine_tune_type": "lora", "lora_parameters": {"rank": 2, "scale": 8}}))
    prefix = "model.layers.0.self_attn.q_proj"
    safetensors.save_file({prefix + ".weight": torch.zeros((2, 8), dtype=torch.uint32),
                          prefix + ".scales": torch.ones((2, 1), dtype=torch.bfloat16),
                          prefix + ".biases": torch.full((2, 1), -3, dtype=torch.bfloat16)}, str(base / "model.safetensors"))
    safetensors.save_file({prefix + ".lora_a": torch.full((64, 2), 1 / 64),
                          prefix + ".lora_b": torch.tensor([[0.25, -0.125], [0.125, 0.25]])}, str(adapter / "adapters.safetensors"))
    output = tmp_path / "fused"
    receipt = export.merge_mlx_cpu(base, adapter, output)
    actual = safetensors.load_file(str(output / "model.safetensors"))[prefix + ".weight"]
    expected = torch.stack([torch.full((64,), -3 + 0.046875), torch.full((64,), -3 + 0.015625)]).bfloat16()
    assert torch.equal(actual, expected)
    assert receipt["adapted_matrices"] == 1
    assert receipt["base_dequantized_matrices"] == 1
    assert "quantization" not in json.loads((output / "config.json").read_text())
    assert json.loads((base / "config.json").read_text())["quantization"]["bits"] == 4
