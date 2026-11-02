"""Read-only GGUF metadata, tensor-index and recognizable plaintext audit.

Uses only the Python standard library. A clean result is a bounded inspection of
the serialized artifact, not proof against memorization or every secret format.
Optional exact deny values stay in an operator-local JSON file; reports identify
their labels and match hashes without printing the underlying private values.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import mmap
from pathlib import Path
import re
import struct


FORMATS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
SIGNATURES = {
    "private_key_header": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    "openai_style_key": rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}",
    "github_pat": rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "gitlab_pat": rb"glpat-[A-Za-z0-9_-]{20,}",
    "huggingface_token": rb"hf_[A-Za-z0-9]{30,}",
    "aws_access_key_id": rb"(?:AKIA|ASIA)[A-Z0-9]{16}",
    "private_home_path": rb"(?:/Users/|/home/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]{2,}",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Reader:
    def __init__(self, source):
        self.source = source

    def scalar(self, format):
        size = struct.calcsize("<" + format)
        value = self.source.read(size)
        if len(value) != size:
            raise ValueError("Truncated GGUF scalar")
        return struct.unpack("<" + format, value)[0]

    def string(self):
        length = self.scalar("Q")
        if length > 32 * 1024**2:
            raise ValueError("Unreasonable GGUF string length")
        value = self.source.read(length)
        if len(value) != length:
            raise ValueError("Truncated GGUF string")
        return value.decode("utf-8")

    def value(self, kind, depth=0):
        if depth > 4:
            raise ValueError("Excessively nested GGUF metadata")
        if kind in FORMATS:
            return self.scalar(FORMATS[kind])
        if kind == 8:
            return self.string()
        if kind == 9:
            element_kind, count = self.scalar("I"), self.scalar("Q")
            if count > 2_000_000:
                raise ValueError("Unreasonable GGUF metadata array length")
            return [self.value(element_kind, depth + 1) for _ in range(count)]
        raise ValueError(f"Unsupported GGUF metadata value type {kind}")


def read_index(path: Path):
    with path.open("rb") as source:
        if source.read(4) != b"GGUF":
            raise ValueError("Not a little-endian GGUF file")
        reader = Reader(source)
        version, tensor_count, metadata_count = reader.scalar("I"), reader.scalar("Q"), reader.scalar("Q")
        if version not in {2, 3} or tensor_count > 100_000 or metadata_count > 100_000:
            raise ValueError("Unsupported or unreasonable GGUF header")
        metadata = {}
        for _ in range(metadata_count):
            name, kind = reader.string(), reader.scalar("I")
            if name in metadata:
                raise ValueError("Duplicate GGUF metadata key")
            metadata[name] = reader.value(kind)
        metadata_end = source.tell()
        tensors, names = [], set()
        for _ in range(tensor_count):
            name, dimensions = reader.string(), reader.scalar("I")
            if name in names or not 1 <= dimensions <= 8:
                raise ValueError("Invalid or duplicate GGUF tensor index entry")
            names.add(name)
            shape = [reader.scalar("Q") for _ in range(dimensions)]
            kind, offset = reader.scalar("I"), reader.scalar("Q")
            tensors.append({"name": name, "shape": shape, "ggml_type": kind, "relative_offset": offset})
        alignment = metadata.get("general.alignment", 32)
        if not isinstance(alignment, int) or alignment <= 0 or alignment > 4096 or alignment & (alignment - 1):
            raise ValueError("Invalid GGUF alignment")
        data_offset = (source.tell() + alignment - 1) // alignment * alignment
    size = path.stat().st_size
    if data_offset > size or any(data_offset + tensor["relative_offset"] >= size for tensor in tensors):
        raise ValueError("GGUF tensor offset lies outside the artifact")
    return {"version": version, "metadata_count": metadata_count, "tensor_count": tensor_count,
            "metadata_end": metadata_end, "tensor_data_offset": data_offset,
            "metadata": metadata, "tensors": tensors}


def audit(path: Path, *, expected_sha256=None, literal_denials=None):
    index = read_index(path)
    actual_sha256 = sha256_file(path)
    patterns = dict(SIGNATURES)
    for entry in literal_denials or []:
        if not isinstance(entry, dict) or set(entry) != {"label", "value"} or not entry["value"]:
            raise ValueError("Literal deny entries require nonempty label/value pairs")
        patterns["operator_literal:" + entry["label"]] = re.escape(entry["value"].encode())
    findings = []
    with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as contents:
        for label, pattern in patterns.items():
            for number, match in enumerate(re.finditer(pattern, contents)):
                if number == 1000:
                    findings.append({"signature": label, "remaining_matches_truncated": True})
                    break
                findings.append({"signature": label, "offset": match.start(), "bytes": len(match.group()),
                    "region": "metadata" if match.start() < index["metadata_end"] else
                              "tensor_index" if match.start() < index["tensor_data_offset"] else "tensor_data",
                    "matched_bytes_sha256": hashlib.sha256(match.group()).hexdigest()})
    metadata_summary = {}
    for name, value in index["metadata"].items():
        summary_name = name
        if any(re.search(pattern, name.encode()) for pattern in patterns.values()):
            summary_name = "redacted-key-sha256:" + hashlib.sha256(name.encode()).hexdigest()
        if isinstance(value, list):
            metadata_summary[summary_name] = {"array_count": len(value), "canonical_json_sha256": hashlib.sha256(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}
        elif isinstance(value, str) and any(re.search(pattern, value.encode()) for pattern in patterns.values()):
            metadata_summary[summary_name] = {"redacted_due_to_plaintext_finding": True,
                "bytes": len(value.encode()), "sha256": hashlib.sha256(value.encode()).hexdigest()}
        else:
            metadata_summary[summary_name] = value
    report = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "artifact": {"file": path.name, "bytes": path.stat().st_size, "sha256": actual_sha256},
        "expected_sha256_matches": actual_sha256 == expected_sha256 if expected_sha256 else None,
        "gguf": {name: index[name] for name in ("version", "metadata_count", "tensor_count", "metadata_end", "tensor_data_offset")},
        "metadata_summary": metadata_summary,
        "tensor_type_counts": dict(Counter(str(t["ggml_type"]) for t in index["tensors"])),
        "tensor_index_sha256": hashlib.sha256(json.dumps(index["tensors"], sort_keys=True).encode()).hexdigest(),
        "plaintext_scan": {"scope": "Entire artifact including metadata, token vocabulary, tensor index and tensor bytes",
            "signature_labels": sorted(patterns), "findings": findings,
            "limits": "Recognizable serialized plaintext only; not a memorization, provenance or exhaustive secret audit. Vocabulary arrays are scanned but not classified as secret merely for containing security-related words."},
        "script_sha256": sha256_file(Path(__file__)),
        "release_candidate_checks_passed": not findings and (not expected_sha256 or actual_sha256 == expected_sha256)}
    return report, index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--literal-deny-file", type=Path)
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    denials = json.loads(args.literal_deny_file.read_text()) if args.literal_deny_file else []
    report, index = audit(args.model, expected_sha256=args.expected_sha256, literal_denials=denials)
    if args.literal_deny_file:
        report["operator_literal_deny_file_sha256"] = sha256_file(args.literal_deny_file)
    if args.metadata_output:
        args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_output.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["full_metadata_and_index_json_sha256"] = sha256_file(args.metadata_output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": report["artifact"], "findings": len(report["plaintext_scan"]["findings"]),
                      "release_candidate_checks_passed": report["release_candidate_checks_passed"]}))
    raise SystemExit(0 if report["release_candidate_checks_passed"] else 1)


if __name__ == "__main__":
    main()
