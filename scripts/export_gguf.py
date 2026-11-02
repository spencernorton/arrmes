"""Fuse a pinned MLX LoRA without re-quantizing it, then export qualified GGUF.

Run this optional export tool on Apple Silicon with arrmes[mlx]. The destination
must not exist. CPU installations only need the resulting GGUF + llama-server;
they do not install MLX, torch, transformers, or this conversion environment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
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


def file_receipt(path: Path) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}


def audit_fusion(base: Path, adapter: Path, fused: Path) -> dict:
    """Every saved LoRA target must equal MLX's dequantized fusion exactly."""
    import mlx.core as mx
    from mlx_lm import load

    model, _ = load(str(base), adapter_path=str(adapter))
    saved = {}
    for shard in sorted(fused.glob("*.safetensors")):
        saved.update(mx.load(str(shard)))
    targets = []
    for name, module in model.named_modules():
        if not hasattr(module, "lora_a"):
            continue
        key = name + ".weight"
        expected = module.fuse(dequantize=True).weight
        if key not in saved or saved[key].shape != expected.shape:
            raise ValueError("A fused LoRA tensor is missing or has the wrong shape")
        actual = saved[key]
        if not bool(mx.all(mx.isfinite(actual)).item()):
            raise ValueError("A fused tensor contains non-finite values")
        error = float(mx.max(mx.abs(actual.astype(mx.float32) - expected.astype(mx.float32))).item())
        delta = ((module.scale * module.lora_b.T) @ module.lora_a.T).astype(mx.float32)
        delta_l2 = float(mx.linalg.norm(delta).item())
        if error != 0 or delta_l2 <= 0:
            raise ValueError("Fusion did not preserve an exact nonzero LoRA update")
        targets.append({"tensor": key, "max_abs_error": error, "adapter_delta_l2": delta_l2})
    if not targets:
        raise ValueError("No LoRA target tensors were verified")
    return {"target_tensors": len(targets), "exact_matches": len(targets),
            "all_adapter_deltas_nonzero": True, "max_abs_error": max(r["max_abs_error"] for r in targets),
            "targets": targets}


def audit_f16_gguf(fused: Path, gguf_path: Path, llama_source: Path) -> dict:
    """Check every exported Qwen3 tensor, including the converter's F16 cast."""
    import mlx.core as mx
    import numpy as np

    sys.path.insert(0, str(llama_source / "gguf-py"))
    import gguf

    config = json.loads((fused / "config.json").read_text())
    if config.get("model_type") != "qwen3":
        raise ValueError("This export qualification currently supports Qwen3 only")
    mapping = gguf.get_tensor_name_map(gguf.MODEL_ARCH.QWEN3, config["num_hidden_layers"])
    reader = gguf.GGUFReader(str(gguf_path))
    tensors = {t.name: t for t in reader.tensors}
    seen = set()
    for shard in sorted(fused.glob("*.safetensors")):
        for name, value in mx.load(str(shard)).items():
            mapped = mapping.get_name(name, try_suffixes=(".weight", ".bias"))
            if mapped not in tensors:
                raise ValueError("An exported tensor is missing from GGUF")
            actual = tensors[mapped].data
            expected = np.array(value.astype(mx.float32), copy=False).astype(actual.dtype)
            if expected.shape != actual.shape or not np.array_equal(expected, actual):
                raise ValueError("GGUF tensor data differs from the expected dtype conversion")
            seen.add(mapped)
    if seen != set(tensors):
        raise ValueError("Unexpected GGUF tensors were not covered by the audit")
    return {"verified_tensors": len(seen), "all_expected_casts_exact": True, "max_abs_error": 0.0,
            "warning": "Tensor preservation does not establish identical fused runtime logits or quantized behavior"}


def run_export(args) -> dict:
    base, adapter, source, destination = (Path(p).expanduser().resolve() for p in (args.base, args.adapter, args.llama_source, args.output))
    if not base.is_dir() or not (adapter / "adapters.safetensors").is_file():
        raise ValueError("Pinned local base and adapter files are required")
    converter = source / "convert_hf_to_gguf.py"
    if not converter.is_file():
        raise ValueError("llama.cpp source must contain convert_hf_to_gguf.py")
    licenses = [Path(p).expanduser().resolve() for p in args.license_file]
    if not all(p.is_file() and p.stat().st_size > 0 for p in licenses):
        raise ValueError("Local base/adapter license files are required")
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != args.llama_revision:
        raise ValueError("llama.cpp source revision does not match --llama-revision")
    if destination.exists():
        raise ValueError("Output already exists; use a new revision directory to preserve artifacts")
    destination.mkdir(parents=True)
    for index, license_file in enumerate(licenses, 1):
        shutil.copyfile(license_file, destination / f"LICENSE-{index}.txt")
    notice = ("Arrmes GGUF conversion. Modified model files: model-f16.gguf and the quantized GGUF.\n"
              "Modifications: merge the specified Arrmes LoRA into the dequantized MLX base; convert to GGUF; quantize.\n"
              f"Base: {args.base_reference}\nAdapter: {args.adapter_reference}\n"
              "Preserve these license files and upstream attribution/NOTICE files with redistribution.\n")
    if args.notice_file:
        notice += "\nUpstream NOTICE:\n" + Path(args.notice_file).read_text()
    (destination / "NOTICE.txt").write_text(notice)
    fused = destination / "fused"
    receipt = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
               "state": "exporting", "base_reference": args.base_reference, "adapter_reference": args.adapter_reference,
               "base_files": [file_receipt(p) for p in sorted(base.glob("*.safetensors"))],
               "adapter_files": [file_receipt(adapter / "adapters.safetensors"), file_receipt(adapter / "adapter_config.json")],
               "license_files": [file_receipt(p) for p in sorted(destination.glob("LICENSE-*.txt"))],
               "llama_cpp_commit": commit, "mlx_lm_version": importlib.metadata.version("mlx-lm"),
               "quantization": args.quantization, "behavioral_equivalence": "not measured by export; run comparative benchmark"}
    (destination / "export-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    commands = [
        [sys.executable, "-m", "mlx_lm", "fuse", "--model", str(base), "--adapter-path", str(adapter), "--save-path", str(fused), "--dequantize"],
        [args.conversion_python, str(converter), str(fused), "--outfile", str(destination / "model-f16.gguf"), "--outtype", "f16"],
        [args.quantize_binary, str(destination / "model-f16.gguf"), str(destination / f"model-{args.quantization.lower()}.gguf"), args.quantization],
    ]
    try:
        for index, command in enumerate(commands):
            with (destination / f"stage-{index + 1}.log").open("wb") as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
            if index == 0:
                receipt["fusion_audit"] = audit_fusion(base, adapter, fused)
                receipt["fused_files"] = [file_receipt(p) for p in sorted(fused.glob("*.safetensors"))]
            if index == 1:
                receipt["gguf_f16_tensor_audit"] = audit_f16_gguf(fused, destination / "model-f16.gguf", source)
        receipt["artifacts"] = [file_receipt(p) for p in sorted(destination.glob("*.gguf"))]
        receipt["quantizer_version"] = subprocess.run([args.quantize_binary, "--version"], capture_output=True, text=True).stderr.strip()[:1000]
        receipt["state"] = "exported_not_behaviorally_qualified"
    except Exception:
        receipt["state"] = "failed"
        raise
    finally:
        (destination / "export-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--base-reference", required=True, help="Repository@immutable revision for provenance")
    parser.add_argument("--adapter-reference", required=True, help="Repository@immutable revision, or internal checkpoint hash")
    parser.add_argument("--license-file", action="append", required=True, help="Local upstream license to retain; repeat for additional licenses")
    parser.add_argument("--notice-file", help="Upstream NOTICE, when provided by the model distribution")
    parser.add_argument("--llama-source", required=True)
    parser.add_argument("--llama-revision", required=True, help="Exact 40-character source commit")
    parser.add_argument("--conversion-python", required=True, help="Separate Python with llama.cpp conversion requirements")
    parser.add_argument("--quantize-binary", default="llama-quantize")
    parser.add_argument("--quantization", choices=["Q4_K_M", "Q5_K_M", "Q8_0"], default="Q4_K_M")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    receipt = run_export(args)
    print(json.dumps({"state": receipt["state"], "verified_lora_tensors": receipt["fusion_audit"]["target_tensors"],
                      "artifacts": receipt["artifacts"]}, indent=2))


if __name__ == "__main__":
    main()
