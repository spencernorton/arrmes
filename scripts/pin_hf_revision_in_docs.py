#!/usr/bin/env python3
"""Pin an immutable 40-char Hugging Face commit SHA into docs/portable-runtime.md and optionally verify live HTTP + SHA-256."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import urllib.request

ROOT_DIR = Path(__file__).resolve().parents[1]
EXPECTED_WEIGHTS_SHA256 = "d91eca610add347575ea6c29e04d1861aba8045b4970c8c72ec2b4ecf1a0be05"
EXPECTED_CONFIG_SHA256 = "89c87c0e59308c916f445f44c3768c5b9fc61d3b0858e65ef6474c47912e4dd4"
ROUTER_URL_RE = re.compile(
    r"(https://huggingface\.co/NorviTech/arrmes-neural-router-v3/resolve/)([^/\s]+)(/router_(?:weights\.npz|config\.json))"
)


def pin_router_revision_in_markdown(md_text: str, commit_sha: str) -> str:
    sha = commit_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"commit_sha must be a 40-char lowercase hex SHA, got: {commit_sha!r}")
    updated, count = ROUTER_URL_RE.subn(rf"\g<1>{sha}\g<3>", md_text)
    if count < 2:
        raise RuntimeError(f"Expected at least 2 router download URLs in markdown, replaced {count}")
    if "/resolve/main/" in updated:
        raise RuntimeError("Unpinned /resolve/main/ URL remains after replacement")
    return updated


def verify_live_router_urls(
    commit_sha: str,
    expected_weights_sha256: str = EXPECTED_WEIGHTS_SHA256,
    expected_config_sha256: str = EXPECTED_CONFIG_SHA256,
) -> dict[str, str]:
    sha = commit_sha.strip().lower()
    out: dict[str, str] = {}
    for fname in ("router_weights.npz", "router_config.json"):
        url = f"https://huggingface.co/NorviTech/arrmes-neural-router-v3/resolve/{sha}/{fname}"
        req = urllib.request.Request(url, headers={})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Unexpected HTTP {resp.status} for {url}")
            body = resp.read()
        digest = hashlib.sha256(body).hexdigest()
        out[fname] = digest
    if out["router_weights.npz"] != expected_weights_sha256:
        raise RuntimeError(
            f"Live router_weights.npz digest mismatch: {out['router_weights.npz']} != {expected_weights_sha256}"
        )
    if out["router_config.json"] != expected_config_sha256:
        raise RuntimeError(
            f"Live router_config.json digest mismatch: {out['router_config.json']} != {expected_config_sha256}"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--router-commit-sha", required=True, help="40-char Hugging Face commit OID for NorviTech/arrmes-neural-router-v3")
    ap.add_argument("--doc-path", type=Path, default=ROOT_DIR / "docs/portable-runtime.md")
    ap.add_argument("--verify-live", action="store_true")
    args = ap.parse_args()

    md_text = args.doc_path.read_text(encoding="utf-8")
    updated = pin_router_revision_in_markdown(md_text, args.router_commit_sha)
    if args.verify_live:
        digests = verify_live_router_urls(args.router_commit_sha)
        print("Verified live pinned URLs:", digests)
    args.doc_path.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
