#!/usr/bin/env python3
"""Resolve published squash refs by exact frozen source bytes, not ancestry.

Added after the original inference runs. This changes metadata lookup only;
it does not modify runtime code, inputs, prompts, weights, grading or results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SHA256 = "02cfd1b735c1a47f7ca3c009f7beb0f78de0045cf3e509a85c61260d1dfa012c"
FROZEN_HARNESS_SHA256 = "fe1248ca4088f4bc8cac0f973c23493c5d81ea40b74b047751f343e8777f6779"
ARCHIVE_INDEX_SHA256 = "e2cd992269a54f6b89dca612de3226d9b399c214ddda143cf610f2b7ecf53bca"


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def object_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def git(args):
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(ROOT), *args], stderr=subprocess.DEVNULL)


def resolve_source(source_root, frozen_commit, published_ref=None):
    if published_ref is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}", published_ref):
        raise ValueError("Expected a simple published tag, branch or commit ref")
    path = ROOT / "experiments/acceptance-v4/runtime-source-manifests.json"
    if file_sha(path) != MANIFEST_SHA256:
        raise ValueError("Trusted frozen source manifest was changed")
    manifest = json.loads(path.read_text())
    expected = next((source for source in manifest["sources"] if source["frozen_commit"] == frozen_commit), None)
    if expected is None or object_sha(expected["files"]) != expected["source_manifest_sha256"]:
        raise ValueError("Unknown or inconsistent frozen source identity")
    actual_commit = None
    if published_ref is not None:
        actual_commit = git(["rev-parse", "--verify", published_ref + "^{commit}"]).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40}", actual_commit):
            raise ValueError("Unexpected published commit identity")
        paths = [path for path in git(["ls-tree", "-r", "--name-only", actual_commit, "src/arrmes"]).decode().splitlines() if path.endswith(".py")]
        from_git = [{"file": path, "sha256": hashlib.sha256(git(["show", actual_commit + ":" + path])).hexdigest()} for path in paths]
        if from_git != expected["files"]:
            raise ValueError("Published ref differs from frozen source bytes")
    root = Path(source_root)
    actual = [{"file": "src/" + path.relative_to(root).as_posix(), "sha256": file_sha(path)} for path in sorted((root / "arrmes").rglob("*.py"))]
    if actual != expected["files"]:
        raise ValueError("Published ref or supplied runtime differs from frozen source bytes")
    return {"commit": actual_commit, "published_ref": published_ref, "frozen_equivalent_source_commit": frozen_commit,
            "source_origin": "exact-byte source snapshot" if published_ref is None else "exact-byte equivalent Git ref",
            "files": actual, "manifest_sha256": expected["source_manifest_sha256"], "source_byte_equivalence_verified": True,
            "metadata_resolver_sha256": file_sha(__file__), "frozen_source_manifest_file_sha256": MANIFEST_SHA256,
            "original_inference_harness_sha256": FROZEN_HARNESS_SHA256,
            "qualification": "Post-inference metadata resolution by exact source records; no ancestry claim, numeric rescoring or original receipt change."}


def extract_source_snapshot(frozen_commit, destination):
    """Extract only a pinned, bounded source bundle into a NEW disjoint path."""
    requested = Path(destination).absolute()
    if any(path.is_symlink() or getattr(path, "is_junction", lambda: False)() for path in (requested, *requested.parents)):
        raise ValueError("Refusing symlinks in snapshot destination; use canonical paths")
    destination = requested.resolve()
    if destination.exists():
        raise FileExistsError("Refusing to replace an existing snapshot destination")
    index = ROOT / "experiments/acceptance-v4/runtime-source-archives.json"
    if file_sha(index) != ARCHIVE_INDEX_SHA256:
        raise ValueError("Source archive index was changed")
    record = next((item for item in json.loads(index.read_text())["archives"] if item["frozen_commit"] == frozen_commit), None)
    if record is None:
        raise ValueError("No source snapshot for frozen commit")
    archive = ROOT / "experiments/acceptance-v4" / record["file"]
    if record.get("format") != "utf8-source-records-jsonl" or not 0 < archive.stat().st_size <= 2 * 1024**2 or archive.stat().st_size != record["bytes"] or file_sha(archive) != record["sha256"]:
        raise ValueError("Frozen source archive identity mismatch")
    expected = {item["file"]: item for item in record["entries"]}
    contents = {}
    entries = [json.loads(line) for line in archive.read_text("utf-8").splitlines()]
    if len(entries) != len(expected) or len(entries) > 100 or {item.get("file") for item in entries} != set(expected):
        raise ValueError("Duplicate, missing or unexpected source archive entry")
    for item in entries:
        if set(item) != {"file", "bytes", "sha256", "text"} or not isinstance(item["text"], str):
            raise ValueError("Invalid source record")
        name = item["file"]
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name or "\\" in name or ":" in name:
            raise ValueError("Unsafe source archive member path")
        data = item["text"].encode("utf-8")
        if len(data) > 1024**2 or len(data) != expected[name]["bytes"] or hashlib.sha256(data).hexdigest() != expected[name]["sha256"] or item["bytes"] != len(data) or item["sha256"] != expected[name]["sha256"]:
            raise ValueError("Source archive member identity mismatch")
        contents[name] = data
    if sum(map(len, contents.values())) > 8 * 1024**2:
        raise ValueError("Unpacked source archive exceeds bound")
    # All archive validation precedes directory creation. Never remove or
    # replace a caller's existing path, including unrelated ancestor contents.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arrmes-v4-source-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "snapshot"
        staged.mkdir()
        for name, data in contents.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        resolve_source(staged / "src", frozen_commit)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Snapshot destination appeared while staging; preserving it")
        staged.rename(destination)
    return destination / "src"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-root")
    source.add_argument("--extract-into", help="Create a new licensed source snapshot directory without needing historical Git objects")
    parser.add_argument("--frozen-commit", required=True)
    parser.add_argument("--published-ref", help="Optional Git ref; omit to validate an exact source-only snapshot")
    args = parser.parse_args()
    if args.extract_into and args.published_ref:
        parser.error("Snapshot extraction does not require a published Git ref")
    root = extract_source_snapshot(args.frozen_commit, args.extract_into) if args.extract_into else args.source_root
    print(json.dumps(resolve_source(root, args.frozen_commit, args.published_ref), indent=2))


if __name__ == "__main__":
    main()
