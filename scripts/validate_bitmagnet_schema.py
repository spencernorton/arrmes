#!/usr/bin/env python3
"""Validate fixed native queries against pinned upstream SDL, without a server.

Development-only dependency: graphql-core==3.2.6. This is not an Arrmes runtime
dependency, a service integration test, or a model quality measurement.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

REVISIONS = ("4d4042acdddfa1af05bfaf092a68c97b1e3698e2", "e31b30d7351c269dc801876f92ebc59d31fee545")
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True, help="Local bitmagnet-io/bitmagnet clone with both pinned revisions")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite an existing schema receipt")
    try:
        from graphql import build_schema, parse, validate, __version__
    except ImportError:
        raise SystemExit("Use a separate development environment with graphql-core==3.2.6") from None

    module = ROOT / "src/arrmes/services/bitmagnet.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "_DOCUMENTS" for target in node.targets))
    # Evaluate the literal dictionary only. Neither Arrmes nor upstream source
    # is executed by this schema checker.
    documents = ast.literal_eval(assignment.value.args[0])
    checks = []
    for revision in REVISIONS:
        paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", revision, "graphql/schema/"],
                                        cwd=args.upstream, text=True).splitlines()
        if not paths or any(not path.startswith("graphql/schema/") or not path.endswith(".graphqls") for path in paths):
            raise SystemExit("Pinned upstream schema files are missing or unexpected")
        sources = {path: subprocess.check_output(["git", "show", revision + ":" + path], cwd=args.upstream) for path in paths}
        schema = build_schema("\n".join(raw.decode("utf-8") for raw in sources.values()))
        operations = {}
        for name, document in documents.items():
            errors = [str(error) for error in validate(schema, parse(document))]
            operations[name] = {"document_sha256": hashlib.sha256(document.encode()).hexdigest(),
                                "validation_errors": errors, "valid": not errors}
        checks.append({"upstream_revision": revision,
                       "schema_files": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest()} for path, raw in sources.items()],
                       "operations": operations})
    receipt = {
        "schema_version": 1,
        "kind": "Read-only document validation against primary upstream SDL; no service process, application binary, database, production endpoint or model execution",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tool": {"name": "graphql-core", "version": __version__, "python": sys.version.split()[0]},
        "validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(receipt, indent=2) + "\n")
    success = all(operation["valid"] for check in checks for operation in check["operations"].values())
    print(json.dumps({"upstream_revisions": len(checks), "read_documents": len(documents), "all_valid": success}))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
