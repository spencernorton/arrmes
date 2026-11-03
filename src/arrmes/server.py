"""Production HTTP JSON service for Arrmes verified tool routing, bounded execution, and write confirmation."""
from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import os
from pathlib import Path
import threading
import time
from typing import Any

from arrmes import __version__
from arrmes.auth import AuthenticationError, BearerTokenAuthenticator
from arrmes.catalog import V2_BY_NAME, contract_hash_v2, contract_hash_v3
from arrmes.executor import BoundedExecutor, ExecutionTrace, SessionEntityLedger
from arrmes.live_adapter import LiveGatewayAdapter
from arrmes.model import DeterministicBaselineModel, LocalModel
from arrmes.backends import create_model
from arrmes.planner import Principal, prepare, verify_proposal
from arrmes.protocol import Call, InvalidCall, decode, normalized
from arrmes.service_router import ConfiguredAdapterRouter
from arrmes.services import ServiceQueryAdapter, load_service_configs, route_service_query
from arrmes.workflows import native_read_workflow
from arrmes.services.bazarr_routing import route_bazarr_subtitle_query
from arrmes.services.request_routing import has_native_request_intent, route_native_request_query
from arrmes.explicit import NativeOnlyModel

WRITE_MODES = frozenset({"confirm", "shadow", "direct", "disabled"})
DEFAULT_CONNECTION_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_REQUEST_DURATION_SECONDS = 15.0
DEFAULT_MAX_CONCURRENT_REQUESTS = 16


class _DeadlineSocketIO(io.RawIOBase):
    """Socket reader enforcing both per-read inactivity timeout and an absolute total request read deadline."""

    def __init__(self, sock: socket.socket, *, idle_timeout: float, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._idle_timeout = float(idle_timeout)
        self._deadline = float(deadline)

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0.0:
            raise socket.timeout("HTTP request total read deadline exceeded")
        self._sock.settimeout(min(self._idle_timeout, remaining))
        n = self._sock.recv_into(b)
        if n > 0 and time.monotonic() >= self._deadline:
            raise socket.timeout("HTTP request total read deadline exceeded")
        return n


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threaded server with a cap on concurrent handlers; excess connections are closed immediately."""

    daemon_threads = True

    def __init__(self, server_address, handler_cls, *, max_concurrent: int) -> None:
        self._slots = threading.BoundedSemaphore(max_concurrent)
        super().__init__(server_address, handler_cls)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
_IDENTITY_BODY_FIELDS = ("subject", "scopes", "principal")


class ArrmesRuntimeService:
    """Thread-safe runtime coordinator managing model inference, session ledgers, and bounded adapter execution."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        executor: BoundedExecutor | None = None,
        write_mode: str = "confirm",
    ) -> None:
        if write_mode not in WRITE_MODES:
            raise ValueError(f"write_mode must be one of {sorted(WRITE_MODES)}")
        self.model = model or DeterministicBaselineModel(family="hermes", contract_version="v2")
        self.write_mode = write_mode
        if executor is not None:
            self.executor = executor
        else:
            gw_url = os.environ.get("ARRMES_GATEWAY_URL", "").strip()
            services_file = os.environ.get("ARRMES_SERVICES_FILE", "").strip()
            if not gw_url and not services_file:
                raise ValueError("ARRMES_GATEWAY_URL or ARRMES_SERVICES_FILE must be set explicitly")
            adapter_mode = "confirm" if write_mode == "disabled" else write_mode
            adapter = LiveGatewayAdapter(gw_url, write_mode=adapter_mode) if gw_url else None
            if services_file:
                adapter = ConfiguredAdapterRouter(ServiceQueryAdapter(load_service_configs(services_file)), adapter,
                                                 write_mode=write_mode)
            self.executor = BoundedExecutor(adapter, allow_writes=(write_mode != "disabled"))
        self.native_queries_enabled = bool(getattr(self.executor.adapter, "supports_service_queries", False))
        self._lock = threading.Lock()
        self._ledgers: dict[str, SessionEntityLedger] = {}
        self._principal_locks: dict[str, threading.Lock] = {}
        self._latencies_ms: list[float] = []
        self._counters: dict[str, int] = {
            "total_requests": 0,
            "route_requests": 0,
            "execute_requests": 0,
            "confirm_requests": 0,
            "completed": 0,
            "dispatched": 0,
            "awaiting_confirmation": 0,
            "shadow_logged": 0,
            "clarify": 0,
            "unsupported": 0,
            "denied": 0,
            "error": 0,
        }

    def _get_ledger(self, subject: str) -> SessionEntityLedger:
        with self._lock:
            ledger = self._ledgers.get(subject)
            if ledger is None:
                ledger = SessionEntityLedger(principal_id=subject)
                self._ledgers[subject] = ledger
            return ledger

    def _principal_lock(self, subject: str) -> threading.Lock:
        """One lock per principal so a caller's turns (ledger, inference, execution, confirm) never interleave."""
        with self._lock:
            lock = self._principal_locks.get(subject)
            if lock is None:
                lock = threading.Lock()
                self._principal_locks[subject] = lock
            return lock

    def _record_metric(self, key: str, latency_ms: float | None = None) -> None:
        with self._lock:
            self._counters["total_requests"] += 1
            if key in self._counters:
                self._counters[key] += 1
            if latency_ms is not None:
                self._latencies_ms.append(round(latency_ms, 3))
                if len(self._latencies_ms) > 2000:
                    self._latencies_ms = self._latencies_ms[-1000:]

    def health(self) -> dict[str, Any]:
        """Unauthenticated liveness payload; deliberately omits paths and upstream addresses."""
        return {
            "status": "ok",
            "service": "arrmes",
            "version": __version__,
            "contract_version": "arrmes.tools.v3" if self.native_queries_enabled else "arrmes.tools.v2",
            "contract_sha256": contract_hash_v3() if self.native_queries_enabled else contract_hash_v2(),
            "neural_contract_version": "arrmes.tools.v2",
            "model_family": getattr(self.model, "family", "unknown"),
            "neural_model_loaded": not isinstance(self.model, DeterministicBaselineModel),
            "backend": getattr(self.model, "backend", "mlx" if isinstance(self.model, LocalModel) else "deterministic"),
            "write_mode": self.write_mode,
        }

    def runtime_info(self) -> dict[str, Any]:
        """Authenticated runtime detail for operators."""
        return {
            **self.health(),
            "model_path": getattr(self.model, "path", "unknown"),
            "adapter_path": getattr(self.model, "adapter", None),
            "gateway_url": getattr(self.executor.adapter, "base_url", "unknown"),
        }

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            lats = sorted(self._latencies_ms)
            c_copy = dict(self._counters)
        p50 = lats[len(lats) // 2] if lats else 0.0
        p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else 0.0
        return {
            "runtime": self.runtime_info(),
            "counters": c_copy,
            "active_ledgers": len(self._ledgers),
            "latency_ms": {"count": len(lats), "p50": p50, "p95": p95},
            "latency_scope": "Mixed in-process route/execute/confirm durations; execute starts after principal serialization. Excludes HTTP ingress and human confirmation delay.",
        }

    def _explicit_write_route(self, query: str) -> tuple[Call | None, str | None]:
        request = route_native_request_query(query)
        configured = getattr(getattr(self.executor.adapter, "native", None), "configs", {})
        if has_native_request_intent(query) and "seerr" not in configured:
            request = Call("clarify", {"slot": "scope"})
        if request is not None:
            return request, "explicit_seerr_deterministic"
        subtitle = route_bazarr_subtitle_query(query) if self.native_queries_enabled else None
        return subtitle, "explicit_bazarr_deterministic" if subtitle is not None else None

    def route(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Non-empty 'query' string is required")
        subtitle, explicit_source = self._explicit_write_route(query)
        workflow = native_read_workflow(query, max_steps=self.executor.max_steps) if self.native_queries_enabled and subtitle is None else None
        if isinstance(workflow, tuple):
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._record_metric("route_requests", elapsed_ms)
            return {"proposal": None, "workflow": [normalized(call) for _, call in workflow],
                    "verified": True, "routing_source": "explicit_service_deterministic",
                    "latency_ms": round(elapsed_ms, 3)}
        extension = subtitle if subtitle is not None else workflow if isinstance(workflow, Call) else route_service_query(query) if self.native_queries_enabled else None
        if extension is not None:
            call = verify_proposal(extension, query, context, contract_version="v3")
            source = explicit_source or "explicit_service_deterministic"
        else:
            with self._lock:
                inf = self.model.infer(query, context, max_tokens=64)
            family = getattr(self.model, "family", "hermes")
            try:
                call = decode(inf["raw"], family=family)
            except InvalidCall:
                call = verify_proposal(Call("clarify", {"slot": "title"}), query, context, contract_version="v2")
            else:
                call = verify_proposal(call, query, context, contract_version="v3" if call.name not in V2_BY_NAME else "v2")
            source = ("native_only_abstention" if isinstance(self.model, NativeOnlyModel) else
                      "deterministic_v2" if isinstance(self.model, DeterministicBaselineModel) else "neural_v2")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._record_metric("route_requests", elapsed_ms)
        return {
            "proposal": normalized(call),
            "verified": True,
            "routing_source": source,
            "latency_ms": round(elapsed_ms, 3),
        }

    def execute(
        self,
        query: str,
        principal: Principal,
        *,
        context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Non-empty 'query' string is required")
        with self._principal_lock(principal.subject):
            return self._execute_locked(query, principal, context=context, idempotency_key=idempotency_key)

    def _execute_locked(
        self,
        query: str,
        principal: Principal,
        *,
        context: dict[str, Any] | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        ledger = self._get_ledger(principal.subject)
        ledger.advance_turn()
        effective_ctx = context if context is not None else ledger.to_model_context()
        subtitle, explicit_source = self._explicit_write_route(query)
        workflow = native_read_workflow(query, max_steps=self.executor.max_steps) if self.native_queries_enabled and subtitle is None else None
        if isinstance(workflow, tuple):
            # Check every scope before the first network call. Independent
            # unavailable-service failures do not prevent other permitted reads.
            denied = next(((clause, call) for clause, call in workflow if prepare(call, principal)["status"] == "denied"), None)
            if denied is not None:
                trace = self.executor.execute_call(denied[1], principal, query=denied[0], ledger=ledger)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                with self._lock:
                    self._counters["execute_requests"] += 1
                self._record_metric("denied", elapsed_ms)
                return {**self._serialize_trace(trace, ledger, elapsed_ms), "routing_source": "explicit_service_deterministic"}
            traces = []
            serialized_steps = []
            for clause, call in workflow:
                step_start = time.perf_counter()
                trace = self.executor.execute_call(call, principal, query=clause, ledger=ledger)
                traces.append(trace)
                serialized_steps.append(self._serialize_trace(trace, ledger, (time.perf_counter() - step_start) * 1000.0))
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            terminal = "completed" if all(trace.terminal_state == "completed" for trace in traces) else "partial"
            with self._lock:
                self._counters["execute_requests"] += 1
            self._record_metric(terminal, elapsed_ms)
            return {"terminal_state": terminal, "proposal": None,
                    "workflow": serialized_steps,
                    "steps_executed": sum(trace.steps_executed for trace in traces),
                    "unsafe_write_blocked": False, "session_context": ledger.to_model_context(),
                    "routing_source": "explicit_service_deterministic", "latency_ms": round(elapsed_ms, 3)}
        extension = subtitle if subtitle is not None else workflow if isinstance(workflow, Call) else route_service_query(query) if self.native_queries_enabled else None
        if extension is not None:
            trace = self.executor.execute_call(extension, principal, query=query, context=effective_ctx,
                                               ledger=ledger, idempotency_key=idempotency_key)
            source = explicit_source or "explicit_service_deterministic"
        else:
            with self._lock:
                inf = self.model.infer(query, effective_ctx, max_tokens=64)
            family = getattr(self.model, "family", "hermes")
            trace = self.executor.execute_raw_proposal(
                inf["raw"], principal, family=family, query=query, context=effective_ctx,
                ledger=ledger, idempotency_key=idempotency_key,
            )
            source = ("native_only_abstention" if isinstance(self.model, NativeOnlyModel) else
                      "deterministic_v2" if isinstance(self.model, DeterministicBaselineModel) else "neural_v2")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._counters["execute_requests"] += 1
        self._record_metric(trace.terminal_state, elapsed_ms)
        return {**self._serialize_trace(trace, ledger, elapsed_ms), "routing_source": source}

    def confirm(
        self,
        confirm_id: str,
        principal: Principal,
        *,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        if not isinstance(confirm_id, str) or not confirm_id.strip():
            raise ValueError("Non-empty 'confirm_id' string is required")
        with self._principal_lock(principal.subject):
            ledger = self._get_ledger(principal.subject)
            trace = self.executor.confirm_write(
                confirm_id.strip(),
                principal,
                ledger=ledger,
                idempotency_key=idempotency_key,
                now=now,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                self._counters["confirm_requests"] += 1
            self._record_metric(trace.terminal_state, elapsed_ms)
            return self._serialize_trace(trace, ledger, elapsed_ms)

    @staticmethod
    def _serialize_trace(trace: ExecutionTrace, ledger: SessionEntityLedger, elapsed_ms: float) -> dict[str, Any]:
        obs_dict = asdict(trace.observation) if trace.observation is not None else None
        return {
            "terminal_state": trace.terminal_state,
            "proposal": trace.proposal,
            "plan": trace.plan,
            "observation": obs_dict,
            "steps_executed": trace.steps_executed,
            "unsafe_write_blocked": trace.unsafe_write_blocked,
            "session_context": ledger.to_model_context(),
            "latency_ms": round(elapsed_ms, 3),
        }


def create_http_server(
    service: ArrmesRuntimeService,
    *,
    authenticator: BearerTokenAuthenticator,
    host: str = "127.0.0.1",
    port: int = 8095,
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
    max_request_duration_seconds: float | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
) -> ThreadingHTTPServer:
    """Create the HTTP server. Every endpoint except ``/healthz`` requires a bearer credential.

    Each accepted connection gets both a per-recv inactivity timeout and an absolute total
    header+body read deadline (``max_request_duration_seconds``), and the number of concurrent
    handlers is bounded; slow-trickle or excess clients are disconnected immediately.
    """
    if authenticator is None:
        raise ValueError("An authenticator is required")
    if connection_timeout <= 0:
        raise ValueError("connection_timeout must be > 0")
    effective_max_duration = (
        float(max_request_duration_seconds)
        if max_request_duration_seconds is not None
        else max(float(connection_timeout) * 2.0, 0.2)
    )
    if effective_max_duration <= 0:
        raise ValueError("max_request_duration_seconds must be > 0")

    class Handler(BaseHTTPRequestHandler):
        timeout = connection_timeout

        def setup(self) -> None:
            super().setup()
            self._request_deadline = time.monotonic() + effective_max_duration
            self.rfile = io.BufferedReader(
                _DeadlineSocketIO(
                    self.connection,
                    idle_timeout=connection_timeout,
                    deadline=self._request_deadline,
                )
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authenticate(self) -> Principal | None:
            try:
                return authenticator.authenticate(self.headers.get("Authorization"))
            except AuthenticationError as exc:
                self._send_json(
                    401,
                    {"error": "unauthorized", "detail": str(exc)},
                    headers={"WWW-Authenticate": 'Bearer realm="arrmes"'},
                )
                return None

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("Request body must be between 1 and 65536 bytes")
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, service.health())
                return
            if self.path == "/v1/metrics":
                if self._authenticate() is None:
                    return
                self._send_json(200, service.metrics())
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path not in {"/v1/route", "/v1/execute", "/v1/confirm"}:
                self._send_json(404, {"error": "not_found"})
                return
            principal = self._authenticate()
            if principal is None:
                return
            try:
                body = self._read_json()
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                return
            except Exception as exc:
                self._send_json(400, {"error": "invalid_request_json", "detail": str(exc)})
                return
            supplied_identity = [k for k in _IDENTITY_BODY_FIELDS if k in body]
            if supplied_identity:
                self._send_json(
                    400,
                    {
                        "error": "identity_fields_not_accepted",
                        "detail": "Caller identity and scopes come only from the bearer credential",
                        "fields": supplied_identity,
                    },
                )
                return

            try:
                if self.path == "/v1/route":
                    res = service.route(body.get("query", ""), body.get("context"))
                    self._send_json(200, res)
                    return
                if self.path == "/v1/execute":
                    # Legacy clients may still send context, but it cannot mint
                    # trusted entity history. Execution uses only this caller's
                    # server-owned ledger; /route remains a non-executing preview.
                    res = service.execute(
                        body.get("query", ""),
                        principal,
                        idempotency_key=body.get("idempotency_key"),
                    )
                    status_code = 403 if res["terminal_state"] == "denied" else 200
                    self._send_json(status_code, res)
                    return
                res = service.confirm(
                    body.get("confirm_id", ""),
                    principal,
                    idempotency_key=body.get("idempotency_key"),
                )
                status_code = 403 if res["terminal_state"] == "denied" else (400 if res["terminal_state"] == "error" else 200)
                self._send_json(status_code, res)
            except ValueError as exc:
                self._send_json(400, {"error": "validation_error", "detail": str(exc)})
            except PermissionError as exc:
                self._send_json(403, {"error": "permission_denied", "detail": str(exc)})

    return BoundedThreadingHTTPServer((host, port), Handler, max_concurrent=max_concurrent)


def _autodiscover_model_and_adapter() -> tuple[str, str | None]:
    """Locate the pinned local Qwen3 0.6B base model and latest distilled LoRA adapter if not explicitly set."""
    repo_root = Path(__file__).resolve().parents[2]
    explicit_model = os.environ.get("ARRMES_MODEL_PATH", "").strip()
    backend_hint = os.environ.get("ARRMES_BACKEND", "auto").strip()
    if explicit_model:
        model_candidates = [explicit_model]
    elif backend_hint == "neural-router":
        model_candidates = [str(repo_root / "models" / "arrmes-neural-router-v3"), str(repo_root / "models" / "qwen3-06b")]
    else:
        model_candidates = [str(repo_root / "models" / "qwen3-06b")]
    model_path = ""
    for cand in model_candidates:
        path = Path(cand).expanduser()
        if cand and (path.is_dir() or (path.is_file() and path.suffix.lower() == ".gguf")):
            model_path = str(path)
            break
    if explicit_model and not model_path:
        raise ValueError("ARRMES_MODEL_PATH must be an existing model directory or GGUF file")

    explicit_adapter = os.environ.get("ARRMES_ADAPTER_PATH", "").strip()
    adapter_candidates = [explicit_adapter] if explicit_adapter else [
        str(repo_root / "adapters" / "qwen3-06b-distilled-v6"),
        str(repo_root / "adapters" / "qwen3-06b-distilled-v5"),
    ]
    adapter_path: str | None = None
    for cand in adapter_candidates:
        if cand and Path(cand).expanduser().is_dir():
            adapter_path = str(Path(cand).expanduser())
            break
    if explicit_adapter and not adapter_path:
        raise ValueError("ARRMES_ADAPTER_PATH must be an existing adapter directory")
    return model_path, adapter_path


def main() -> None:
    host = os.environ.get("ARRMES_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("ARRMES_PORT", "8095"))
    write_mode = os.environ.get("ARRMES_WRITE_MODE", "confirm").strip() or "confirm"
    if write_mode not in WRITE_MODES:
        raise SystemExit(f"ARRMES_WRITE_MODE must be one of {sorted(WRITE_MODES)}")
    if write_mode == "direct":
        raise SystemExit("ARRMES_WRITE_MODE=direct is unavailable for the HTTP service; use confirm, shadow, or disabled")

    clients_file = os.environ.get("ARRMES_CLIENTS_FILE", "").strip()
    if not clients_file:
        raise SystemExit("ARRMES_CLIENTS_FILE is required: the service refuses to start without caller authentication")
    authenticator = BearerTokenAuthenticator.from_file(clients_file)

    backend = os.environ.get("ARRMES_BACKEND", "auto")
    if backend not in {"auto", "mlx", "llama-server", "native-only", "neural-router"}:
        raise SystemExit("ARRMES_BACKEND must be auto, mlx, llama-server, native-only, or neural-router")
    model_path, adapter_path = ("", None) if backend == "native-only" else _autodiscover_model_and_adapter()
    family = os.environ.get("ARRMES_MODEL_FAMILY", "hermes")
    endpoint = os.environ.get("ARRMES_INFERENCE_URL", "").strip() or None
    portable = backend == "llama-server" or (backend == "auto" and (endpoint or model_path.lower().endswith(".gguf")))
    selected_adapter = (os.environ.get("ARRMES_ADAPTER_PATH", "").strip() or None) if portable else adapter_path
    if backend == "native-only":
        if not os.environ.get("ARRMES_SERVICES_FILE", "").strip():
            raise SystemExit("ARRMES_SERVICES_FILE is required for native-only mode")
        native_write_gate = os.environ.get("ARRMES_NATIVE_CONFIRMED_WRITES", "0").strip()
        if native_write_gate not in {"0", "1"}:
            raise SystemExit("ARRMES_NATIVE_CONFIRMED_WRITES must be 0 or 1")
        if native_write_gate != "1":
            write_mode = "disabled"
        mdl: Any = NativeOnlyModel()
    elif model_path or os.environ.get("ARRMES_INFERENCE_URL", "").strip():
        contract_ver = "v3" if os.environ.get("ARRMES_GATEWAY_V3", "").strip() == "1" else "v2"
        mdl: Any = create_model(
            model_path or "remote-gguf", family=family,
            adapter=selected_adapter,
            contract_version=contract_ver, backend=backend,
            endpoint=endpoint,
            device=os.environ.get("ARRMES_DEVICE", "cpu"),
            executable=os.environ.get("ARRMES_LLAMA_SERVER", "llama-server"),
            context_size=int(os.environ.get("ARRMES_CONTEXT_SIZE", "4096")),
            threads=int(os.environ["ARRMES_THREADS"]) if os.environ.get("ARRMES_THREADS") else None,
        )
        if hasattr(mdl, "_ensure_prefix_cache"):
            mdl._ensure_prefix_cache()
    elif os.environ.get("ARRMES_ALLOW_BASELINE_MODEL", "").strip() == "1":
        mdl = DeterministicBaselineModel(family=family, contract_version="v2")
    else:
        raise SystemExit(
            "No local model found (set ARRMES_MODEL_PATH). Set ARRMES_ALLOW_BASELINE_MODEL=1 only for non-production tests."
        )

    srv = None
    try:
        service = ArrmesRuntimeService(model=mdl, write_mode=write_mode)
        srv = create_http_server(service, authenticator=authenticator, host=host, port=port)
        print(
            f"Arrmes HTTP service listening on {host}:{port} "
            f"(backend={getattr(mdl, 'backend', 'mlx')}, write_mode={write_mode})", flush=True,
        )
        if hasattr(mdl, "serve_mlx_loop"):
            t = threading.Thread(target=srv.serve_forever, name="arrmes-http", daemon=True)
            t.start()
            mdl.serve_mlx_loop()
        else:
            srv.serve_forever()
    finally:
        if srv is not None:
            srv.server_close()
        if hasattr(mdl, "close"):
            mdl.close()


if __name__ == "__main__":
    main()
