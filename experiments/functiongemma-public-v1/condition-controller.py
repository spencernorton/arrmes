#!/usr/bin/env python3
"""Run a frozen diagnostic once, retaining completed rows if the harness fails.

This owner-side wrapper never changes model, prompt, parser, or harness code.
The pre-existing harness's run frame is inspected only after an exception.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time


def digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify(items: list[dict]) -> None:
    for item in items:
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size != item["bytes"]:
            raise RuntimeError("frozen_file_missing_or_size_changed")
        if digest(path) != item["sha256"]:
            raise RuntimeError("frozen_file_hash_changed")


class DiagnosticDeadline(Exception):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--condition", choices=["base", "public_v5"], required=True)
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat()
    stage = "plan_identity"
    plan = None
    receipt = None
    harness = None
    started_wall = time.perf_counter()
    fallback = Path(args.plan).parent / (args.condition + "-failure.json")
    try:
        if digest(args.plan) != args.plan_sha256:
            raise RuntimeError("frozen_plan_hash_changed")
        plan = json.loads(Path(args.plan).read_text())
        if (Path(args.plan).parent / (args.condition + ".json")).exists() or fallback.exists():
            raise RuntimeError("condition_receipt_already_exists")
        stage = "frozen_file_preflight"
        verify(plan["frozen_files"])
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        sys.path.insert(0, plan["source_root"])
        spec = importlib.util.spec_from_file_location("frozen_comparison_harness", plan["harness_path"])
        assert spec is not None and spec.loader is not None
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        condition = plan["conditions"][args.condition]
        namespace = argparse.Namespace(
            dataset=plan["dataset_path"], oracle=plan["oracle_path"],
            source_root=plan["source_root"], model=plan["base_path"],
            adapter=condition["adapter_path"], label=condition["label"],
            model_revision=condition["model_revision"], backend="mlx",
            family="functiongemma", device="cpu", threads=4,
            output=str(Path(args.plan).parent / (args.condition + ".json")),
            repetitions=1, max_tokens=160, limit=None, freeze_oracle_only=False,
        )
        def deadline(_signal, _frame):
            raise DiagnosticDeadline()
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(plan["maximum_seconds_per_condition"])
        stage = "harness_run"
        receipt = harness.run(namespace)
        signal.alarm(0)
        stage = "frozen_file_postflight"
        verify(plan["frozen_files"])
        completion = {
            "schema_version": 1, "condition": args.condition,
            "status": "completed", "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "controller_wall_seconds": time.perf_counter() - started_wall,
            "plan_sha256": args.plan_sha256,
            "receipt_sha256": digest(namespace.output),
            "all_frozen_files_unchanged": True,
        }
        (Path(args.plan).parent / (args.condition + "-completion.json")).write_text(json.dumps(completion, indent=2) + "\n")
        return 0
    except BaseException as error:
        signal.alarm(0)
        partial = {}
        cursor = error.__traceback__
        while cursor is not None:
            frame = cursor.tb_frame
            if harness is not None and frame.f_code is harness.run.__code__:
                for key in ("rows", "warmups", "source_hashes", "load_wall", "memory_receipt"):
                    if key in frame.f_locals:
                        partial[key] = frame.f_locals[key]
            cursor = cursor.tb_next
        failure = {
            "schema_version": 1, "condition": args.condition,
            "status": "failed", "all_checks_passed": False,
            "stage": stage, "error_type": type(error).__name__,
            "started_at": started,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "plan_sha256": args.plan_sha256,
            "completed_receipt_present": receipt is not None,
            "partial": partial,
            "failure_message_omitted": "Raw exception text can contain local paths or network metadata.",
            "cleanup": "Sandbox and temporary-directory context managers unwind before this record; MLX memory is released on process exit.",
        }
        fallback.write_text(json.dumps(failure, indent=2, default=str) + "\n")
        print(json.dumps({"status": "failed", "stage": stage, "error_type": type(error).__name__, "partial_rows": len(partial.get("rows", []))}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
