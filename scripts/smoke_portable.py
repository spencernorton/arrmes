"""Hardware smoke: real native GGUF inference through the isolated executor.

This checks portability and actual request execution, not comparative model
quality. The fixed scenarios are development smoke cases and are never counted
as held-out acceptance. All service traffic is restricted to fixture loopback.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from arrmes.adapters import SandboxAdapter
from arrmes.backends import LlamaServerModel
from arrmes.executor import BoundedExecutor
from arrmes.planner import Principal
from arrmes.protocol import Call, normalized
from arrmes.sandbox import NetworkIsolationGuard, SandboxCluster


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--runtime", default="llama-server")
    parser.add_argument("--device", choices=["cpu", "cuda", "metal"], default="cpu")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    tests = [
        ("Which episodes of Severance are missing?", Call("tv_missing", {"title": "Severance"}), "completed"),
        ("List downloading torrents in qBittorrent", Call("torrent_list", {"state": "downloading"}), "completed"),
        ("Check Dune in Radarr", Call("movie_status", {"title": "Dune"}), "clarify"),
        ("Pause all torrents in qBittorrent", Call("unsupported", {"reason": "unsafe"}), "unsupported"),
    ]
    rows = []
    before = time.perf_counter()
    with LlamaServerModel(args.model, family="hermes", device=args.device, threads=args.threads,
                          executable=args.runtime, context_size=4096) as model:
        load_seconds = model.load_seconds
        model_port = urlsplit(model.endpoint).port
        with tempfile.TemporaryDirectory(prefix="arrmes-portable-smoke-") as scratch:
            with SandboxCluster(Path(scratch)) as cluster:
                executor = BoundedExecutor(SandboxAdapter(cluster.base_url, cluster.port), allow_writes=False)
                principal = Principal("owner-1", frozenset({"media:read", "torrents:read"}))
                with NetworkIsolationGuard(allowed_ports={model_port, cluster.port}) as guard:
                    for query, expected_call, expected_terminal in tests:
                        start = time.perf_counter()
                        inference = model.infer(query)
                        trace = executor.execute_raw_proposal(inference["raw"], principal, family="hermes", query=query)
                        proposal = trace.proposal
                        tool = proposal.get("name") if proposal else None
                        expected = normalized(expected_call)
                        rows.append({"query": query, "expected_call": expected, "expected_terminal": expected_terminal,
                                     "actual_tool": tool, "terminal_state": trace.terminal_state,
                                     "actual_call": proposal,
                                     "passed": proposal == expected and trace.terminal_state == expected_terminal,
                                     "inference": inference, "end_to_end_seconds": time.perf_counter() - start})
                    isolation = {"allowed_loopback_attempts": guard.allowed_attempts,
                                 "blocked_attempts": len(guard.blocked_attempts), "nonloopback_allowed": False}
                mutations = sum(r.get("method") not in {"GET", "HEAD"} for r in cluster.audit_log)
        child_rss = None
        if platform.system() == "Linux":
            for line in Path(f"/proc/{model._process.pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    child_rss = int(line.split()[1]) * 1024
    try:
        import resource
        child_maxrss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except ImportError:  # Native Windows has no resource module.
        child_maxrss = None
    native_version = subprocess.run([args.runtime, "--version"], capture_output=True, text=True)
    cpu_name = platform.processor()
    if platform.system() == "Linux":
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_name = line.split(":", 1)[1].strip()
                break
    receipt = {"schema_version": 2, "track": "hardware_development_smoke_not_quality_benchmark",
               "created_at": datetime.now(timezone.utc).isoformat(), "hardware": {"os": platform.platform(),
               "architecture": platform.machine(), "cpu": cpu_name, "python": platform.python_version()},
               "runtime_version": (native_version.stdout + native_version.stderr).strip(),
               "model": {"bytes": Path(args.model).stat().st_size, "sha256": file_sha256(args.model)},
               "settings": {"threads": args.threads, "context_size": 4096, "device": args.device,
                            "native_output": True, "temperature": 0, "seed": 0, "max_tokens": 160},
               "load_seconds": load_seconds, "total_seconds": time.perf_counter() - before,
               "native_peak_rss_bytes": child_rss,
               "child_ru_maxrss": child_maxrss, "child_ru_maxrss_unit": None if child_maxrss is None else "bytes" if platform.system() == "Darwin" else "KiB",
               "service_mutation_http_requests": mutations, "isolation": isolation,
               "passed": sum(r["passed"] for r in rows), "count": len(rows), "rows": rows}
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"passed": receipt["passed"], "count": receipt["count"], "native_peak_rss_bytes": child_rss,
                      "service_mutation_http_requests": mutations, "load_seconds": load_seconds}))
    if receipt["passed"] != receipt["count"] or mutations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
