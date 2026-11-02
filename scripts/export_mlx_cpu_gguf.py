"""CPU-only export of the pinned MLX affine 4-bit Qwen3 + MLX LoRA format.

This optional conversion environment needs torch (CPU build), safetensors and
llama.cpp's converter requirements. Arrmes inference does not depend on them.
Only the explicitly checked affine/group64/4-bit representation is supported.
The source model/adapter directories are never modified.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def affine_dequantize_4bit(packed, scales, biases, *, row_chunk=512):
    """MLX affine layout: eight low-first 4-bit lanes per uint32, groups of64."""
    import torch

    if packed.dtype != torch.uint32 or packed.ndim != 2 or scales.ndim != 2 or biases.shape != scales.shape:
        raise ValueError("Invalid MLX affine quantization tensor types/shapes")
    rows, packed_columns = packed.shape
    if rows != scales.shape[0] or packed_columns * 8 != scales.shape[1] * 64:
        raise ValueError("Packed width does not match affine group64 scales")
    if scales.dtype != biases.dtype or scales.dtype not in {torch.bfloat16, torch.float16, torch.float32}:
        raise ValueError("Unsupported affine scale precision")
    out = torch.empty((rows, packed_columns * 8), dtype=scales.dtype)
    shifts = torch.arange(0, 32, 4, dtype=torch.int64)
    for start in range(0, rows, row_chunk):
        stop = min(start + row_chunk, rows)
        lanes = ((packed[start:stop].to(torch.int64).unsqueeze(-1) >> shifts) & 15).reshape(stop - start, -1).float()
        scale = scales[start:stop].float().repeat_interleave(64, dim=1)
        bias = biases[start:stop].float().repeat_interleave(64, dim=1)
        out[start:stop] = (lanes * scale + bias).to(scales.dtype)
    return out


def merge_mlx_cpu(base: Path, adapter: Path, destination: Path, *, threads=2) -> dict:
    import torch
    from safetensors import safe_open
    from safetensors.torch import load_file, save_file

    torch.set_num_threads(threads)
    config = json.loads((base / "config.json").read_text())
    quantization = config.get("quantization", config.get("quantization_config"))
    if config.get("model_type") != "qwen3" or not isinstance(quantization, dict):
        raise ValueError("Only explicitly affine-quantized MLX Qwen3 is supported")
    if quantization.get("bits") != 4 or quantization.get("group_size") != 64 or quantization.get("mode", "affine") != "affine":
        raise ValueError("Only MLX affine 4-bit group64 weights are supported")
    adapter_config = json.loads((adapter / "adapter_config.json").read_text())
    params = adapter_config.get("lora_parameters", {})
    scale = params.get("scale")
    rank = params.get("rank")
    if adapter_config.get("fine_tune_type") != "lora" or type(scale) not in {int, float} or not 0 < scale <= 1024 or type(rank) is not int or not 1 <= rank <= 1024:
        raise ValueError("An explicit standard MLX LoRA rank and positive scale are required")
    adapters = load_file(str(adapter / "adapters.safetensors"), device="cpu")
    targets = {name[:-7] for name in adapters if name.endswith(".lora_a")}
    if not targets or set(adapters) != {n + suffix for n in targets for suffix in (".lora_a", ".lora_b")}:
        raise ValueError("Adapter must contain exactly paired lora_a/lora_b tensors")
    if any(not bool(torch.isfinite(value).all()) for value in adapters.values()):
        raise ValueError("Non-finite LoRA weights are unsupported")
    # Safe_open maps each source shard; the output dictionary retains only the
    # final dequantized tensors. Row-chunked unpacking avoids a full int64 model.
    weights = {}
    adapted = []
    dequantized = 0
    for shard in sorted(base.glob("*.safetensors")):
        with safe_open(str(shard), framework="pt", device="cpu") as source:
            keys = set(source.keys())
            for name in sorted(keys):
                if name.endswith((".scales", ".biases")):
                    continue
                value = source.get_tensor(name)
                if value.dtype == torch.uint32:
                    if not name.endswith(".weight"):
                        raise ValueError("Packed tensor is not a recognized weight")
                    prefix = name[:-7]
                    if prefix + ".scales" not in keys or prefix + ".biases" not in keys:
                        raise ValueError("Cross-shard or missing quantization metadata is unsupported")
                    value = affine_dequantize_4bit(value, source.get_tensor(prefix + ".scales"), source.get_tensor(prefix + ".biases"))
                    dequantized += 1
                if not bool(torch.isfinite(value).all()):
                    raise ValueError("Non-finite base weight is unsupported")
                prefix = name[:-7] if name.endswith(".weight") else ""
                if prefix in targets:
                    a, b = adapters[prefix + ".lora_a"], adapters[prefix + ".lora_b"]
                    if a.ndim != 2 or b.ndim != 2 or a.shape[1] != rank or b.shape[0] != rank or (b.shape[1], a.shape[0]) != value.shape:
                        raise ValueError("LoRA dimensions do not match the intended base weight")
                    delta = ((scale * b.T.float()) @ a.T.float()).to(value.dtype)
                    delta_norm = float(torch.linalg.vector_norm(delta.float()))
                    if delta_norm <= 0 or not bool(torch.isfinite(delta).all()):
                        raise ValueError("A LoRA target has an invalid or zero update")
                    value = value + delta
                    adapted.append({"tensor": name, "adapter_delta_l2": delta_norm})
                if name in weights:
                    raise ValueError("Duplicate base tensor across shards")
                weights[name] = value.contiguous()
    if {r["tensor"][:-7] for r in adapted} != targets:
        raise ValueError("Not every adapter target matched a base weight")
    destination.mkdir(parents=True, exist_ok=False)
    config.pop("quantization", None)
    config.pop("quantization_config", None)
    for file in base.iterdir():
        if file.is_file() and file.suffix in {".json", ".jinja", ".model"} and file.name not in {"config.json", "model.safetensors.index.json", "arrmes-provenance.json"}:
            shutil.copyfile(file, destination / file.name)
    (destination / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    save_file(weights, str(destination / "model.safetensors"), metadata={"format": "pt"})
    return {"base_dequantized_matrices": dequantized, "output_tensors": len(weights),
            "adapted_matrices": len(adapted), "targets": adapted,
            "torch_version": torch.__version__, "threads": threads,
            "arithmetic": "CPU float32 affine unpack; cast to source scale dtype; scaled MLX-layout LoRA matmul then source-dtype addition"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--base-reference", required=True)
    parser.add_argument("--adapter-reference", required=True)
    parser.add_argument("--license-file", action="append", required=True)
    parser.add_argument("--notice-file", help="Upstream NOTICE file when supplied by the model distribution")
    parser.add_argument("--llama-source", required=True)
    parser.add_argument("--llama-revision", required=True)
    parser.add_argument("--quantize-binary", default="llama-quantize")
    parser.add_argument("--quantization", choices=["Q4_K_M", "Q5_K_M", "Q8_0"], default="Q4_K_M")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.threads <= 32:
        raise ValueError("Export threads must be between1 and32")
    base, adapter, source, output = (Path(p).expanduser().resolve() for p in (args.base, args.adapter, args.llama_source, args.output))
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != args.llama_revision:
        raise ValueError("llama.cpp source revision does not match requested immutable revision")
    if output.exists():
        raise ValueError("Output exists; choose a new directory to preserve artifacts")
    licenses = [Path(p).expanduser().resolve() for p in args.license_file]
    if not all(p.is_file() and p.stat().st_size > 0 for p in licenses):
        raise ValueError("Local upstream license files are required")
    output.mkdir(parents=True)
    receipt = {"schema_version": 1, "state": "exporting", "created_at": datetime.now(timezone.utc).isoformat(),
               "base_reference": args.base_reference, "adapter_reference": args.adapter_reference,
               "base_sha256": {p.name: sha256(p) for p in sorted(base.glob("*.safetensors"))},
               "adapter_sha256": sha256(adapter / "adapters.safetensors"), "llama_cpp_commit": revision,
               "behavioral_equivalence": "not established by conversion"}
    try:
        receipt["merge"] = merge_mlx_cpu(base, adapter, output / "fused", threads=args.threads)
        for index, license_file in enumerate(licenses, 1):
            shutil.copyfile(license_file, output / f"LICENSE-{index}.txt")
        notice = f"Modified Arrmes model: CPU affine dequantization, LoRA merge, GGUF conversion and {args.quantization} quantization.\nBase: {args.base_reference}\nAdapter: {args.adapter_reference}\nRetain upstream license/attribution and applicable NOTICE text when redistributing.\n"
        if args.notice_file:
            notice += "\nUpstream NOTICE:\n" + Path(args.notice_file).read_text()
        (output / "NOTICE.txt").write_text(notice)
        commands = [
            [sys.executable, str(source / "convert_hf_to_gguf.py"), str(output / "fused"), "--outfile", str(output / "model-f16.gguf"), "--outtype", "f16"],
            [args.quantize_binary, str(output / "model-f16.gguf"), str(output / f"model-{args.quantization.lower()}.gguf"), args.quantization],
        ]
        for index, command in enumerate(commands, 1):
            with (output / f"stage-{index}.log").open("wb") as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        receipt["artifacts"] = [{"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(output.glob("*.gguf"))]
        receipt["state"] = "exported_not_behaviorally_qualified"
    except Exception:
        receipt["state"] = "failed"
        raise
    finally:
        (output / "export-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"state": receipt["state"], "adapted_matrices": receipt["merge"]["adapted_matrices"], "artifacts": receipt["artifacts"]}, indent=2))


if __name__ == "__main__":
    main()
