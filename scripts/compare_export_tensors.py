"""Compare fused safetensor values without loading MLX or torch.

Supports the F16/BF16/F32 formats in Arrmes' Qwen3 export. Reads bounded chunks
and reports every difference; metadata ordering and serialization are ignored.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct


def tensor_header(path):
    with Path(path).open("rb") as source:
        size = struct.unpack("<Q", source.read(8))[0]
        if not 0 < size <= 16 * 1024 * 1024:
            raise ValueError("Invalid bounded safetensors header length")
        metadata = json.loads(source.read(size))
    tensors = {name: value for name, value in metadata.items() if name != "__metadata__"}
    file_bytes = Path(path).stat().st_size
    for value in tensors.values():
        start, stop = value["data_offsets"]
        if not 0 <= start <= stop <= file_bytes - 8 - size:
            raise ValueError("Tensor data is outside the file")
    return 8 + size, tensors


def compare(reference, candidate):
    import numpy as np

    ref_start, ref = tensor_header(reference)
    new_start, new = tensor_header(candidate)
    if set(ref) != set(new):
        raise ValueError("Tensor names differ")
    rows = []
    with Path(reference).open("rb") as left, Path(candidate).open("rb") as right:
        for name in sorted(ref):
            a, b = ref[name], new[name]
            if a["shape"] != b["shape"] or a["dtype"] != b["dtype"]:
                raise ValueError("Tensor shape or precision differs")
            if a["dtype"] not in {"BF16", "F16", "F32"}:
                raise ValueError("Only floating Qwen3 export tensors are supported")
            dtype = {"BF16": "<u2", "F16": "<f2", "F32": "<f4"}[a["dtype"]]
            left.seek(ref_start + a["data_offsets"][0])
            right.seek(new_start + b["data_offsets"][0])
            remaining = a["data_offsets"][1] - a["data_offsets"][0]
            if remaining != b["data_offsets"][1] - b["data_offsets"][0]:
                raise ValueError("Tensor byte count differs")
            different = 0
            maximum = 0.0
            hashes = [hashlib.sha256(), hashlib.sha256()]
            while remaining:
                count = min(4 * 1024 * 1024, remaining)
                raw_a, raw_b = left.read(count), right.read(count)
                if len(raw_a) != count or len(raw_b) != count:
                    raise ValueError("Tensor data is truncated")
                hashes[0].update(raw_a)
                hashes[1].update(raw_b)
                values = [np.frombuffer(raw, dtype=dtype) for raw in (raw_a, raw_b)]
                if a["dtype"] == "BF16":
                    values = [(v.astype(np.uint32) << 16).view(np.float32) for v in values]
                else:
                    values = [v.astype(np.float32) for v in values]
                if any(not np.all(np.isfinite(v)) for v in values):
                    raise ValueError("Non-finite export values")
                different += int(np.count_nonzero(values[0] != values[1]))
                maximum = max(maximum, float(np.max(np.abs(values[0] - values[1]))))
                remaining -= count
            rows.append({"tensor": name, "dtype": a["dtype"], "shape": a["shape"],
                         "different_elements": different, "max_abs_error": maximum,
                         "reference_data_sha256": hashes[0].hexdigest(), "candidate_data_sha256": hashes[1].hexdigest()})
    return {"schema_version": 1, "tensors": len(rows), "different_tensors": sum(r["different_elements"] > 0 for r in rows),
            "different_elements": sum(r["different_elements"] for r in rows),
            "max_abs_error": max(r["max_abs_error"] for r in rows), "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = compare(args.reference, args.candidate)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}))
    if result["different_elements"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
