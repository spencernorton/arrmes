#!/usr/bin/env python3
"""Replay recorded neural proposals through native transfer policy/HTTP fixtures.

Run from a source checkout with its test extra installed. This does not run a
model; the input receipt retains the separate measured model provenance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from arrmes.executor import BoundedExecutor
from arrmes.protocol import decode
from arrmes.server import ArrmesRuntimeService
from arrmes.service_router import ConfiguredAdapterRouter
from arrmes.services import ServiceQueryAdapter
from test_native_transfers import H1, OWNER, TransferFixtureState, posts, transfer_fixture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "experiments/overnight-v1/torrent-transfer-smoke.json")
    parser.add_argument("--output", type=Path, default=ROOT / "experiments/overnight-v1/native-transfer-execution-smoke.json")
    args = parser.parse_args()
    source_bytes = args.input.read_bytes()
    source = json.loads(source_bytes)
    results = []
    for row in source["rows"]:
        call = decode(row["raw_unverified"], family="hermes")
        if call.name not in {"torrent_pause", "torrent_resume"} or call.arguments != {"title": "Fixture torrent"}:
            raise ValueError("Input model output is not an exact supported fixture transfer intent")
        class CapturedOutputModel:
            family = "hermes"
            def infer(self, query, *_args, **_kwargs):
                if query != row["query"]:
                    raise ValueError("Captured model output bound to a different query")
                return {"raw": row["raw_unverified"]}
        for version in ("v4.6.7", "v5.1.2"):
            initial = "downloading" if call.name == "torrent_pause" else "pausedDL" if version.startswith("v4") else "stoppedDL"
            state = TransferFixtureState(version=version, torrents=[{"hash": H1, "name": "Fixture torrent", "state": initial}])
            with transfer_fixture(state) as (cfg, state):
                runtime = ArrmesRuntimeService(model=CapturedOutputModel(), executor=BoundedExecutor(
                    ConfiguredAdapterRouter(ServiceQueryAdapter({"qbittorrent": cfg}))))
                start = time.perf_counter()
                proposed = runtime.execute(row["query"], OWNER)
                execute_ms = 1000 * (time.perf_counter() - start)
                before_posts = len(posts(state))
                if proposed["terminal_state"] != "awaiting_confirmation":
                    raise AssertionError("Exact model output did not request explicit confirmation")
                start = time.perf_counter()
                confirmed = runtime.confirm(proposed["plan"]["confirm_id"], OWNER)
                confirm_ms = 1000 * (time.perf_counter() - start)
                replay = runtime.confirm(proposed["plan"]["confirm_id"], OWNER)
                mutations = posts(state)
                passed = (before_posts == 0 and confirmed["terminal_state"] == "completed"
                          and confirmed["observation"]["verified_complete"]
                          and replay["terminal_state"] == "denied" and len(mutations) == 1
                          and mutations[0]["form"] == {"hashes": [H1]})
                results.append({"query": row["query"], "model_output_sha256": hashlib.sha256(row["raw_unverified"].encode()).hexdigest(),
                                "proposal": {"name": call.name, "arguments": call.arguments},
                                "fixture_application_version": version, "passed": bool(passed),
                                "confirmation_state": proposed["terminal_state"], "writes_before_confirmation": before_posts,
                                "confirmed_state": confirmed["terminal_state"], "replay_state": replay["terminal_state"],
                                "mutation_count": len(mutations), "mutation_endpoint": mutations[0]["path"] if mutations else None,
                                "exact_hash_form_verified": bool(mutations and mutations[0]["form"] == {"hashes": [H1]}),
                                "final_state": state.torrents[0]["state"], "postcondition": confirmed["observation"]["data"],
                                "replay_execute_ms": round(execute_ms, 4), "confirmation_ms": round(confirm_ms, 4)})
    paths = ["src/arrmes/" + name + ".py" for name in ("executor", "protocol", "planner", "server", "service_router")]
    paths += ["src/arrmes/services/" + name + ".py" for name in ("native", "transfers")]
    paths.append("tests/test_native_transfers.py")
    receipt = {"kind": "captured neural output replay through runtime, confirmation and native HTTP fixture; integration smoke only",
               "generated_at": datetime.now(timezone.utc).isoformat(), "model_inference_executed_by_this_command": False,
               "input_sha256": hashlib.sha256(source_bytes).hexdigest(), "model_revision": source["model_revision"],
               "latency_scope": "model-output replay plus immediate confirmation; actual inference remains in the input receipt; human decision delay excluded",
               "transport": "real loopback HTTP fixture under NetworkIsolationGuard; no upstream application binary or production service",
               "source_worktree_base": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
               "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths},
               "environment": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
               "rows": results, "passed": sum(row["passed"] for row in results), "total": len(results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Native transfer replay: {receipt['passed']}/{receipt['total']} fixtures passed; no new model inference")
    return 0 if receipt["passed"] == receipt["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
