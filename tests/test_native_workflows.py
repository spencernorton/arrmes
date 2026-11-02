"""Runtime-level bounded reads and server-owned execution context."""
import json
import threading
import urllib.request

from arrmes.adapters import AdapterObservation
from arrmes.auth import BearerTokenAuthenticator, ClientIdentity, token_digest
from arrmes.executor import BoundedExecutor
from arrmes.planner import Principal
from arrmes.server import ArrmesRuntimeService, create_http_server
from arrmes.workflows import native_read_workflow


class NoInference:
    family = "hermes"

    def infer(self, *args, **kwargs):
        raise AssertionError("Explicit native reads must not call the neural model")


class Recorder:
    supports_service_queries = True

    def __init__(self, unavailable=()):
        self.calls = []
        self.unavailable = unavailable

    def execute(self, call, principal, **kwargs):
        self.calls.append(call)
        name = call.arguments["service"]
        failed = name in self.unavailable
        return AdapterObservation("error" if failed else "verified_ok", name, call.name,
                                  503 if failed else 200, not failed,
                                  {"error": "service_unavailable"} if failed else {"items": []}, [])


def runtime(*, unavailable=(), max_steps=4):
    recorder = Recorder(unavailable)
    service = ArrmesRuntimeService(model=NoInference(), executor=BoundedExecutor(recorder, max_steps=max_steps))
    return service, recorder


def test_native_workflow_executes_validated_independent_steps_and_previews_without_inference():
    service, recorder = runtime()
    query = "Show Sonarr queue and Radarr history then check Bazarr health"
    preview = service.route(query)
    assert len(preview["workflow"]) == 3 and recorder.calls == []
    result = service.execute(query, Principal("fixture:reader", frozenset({"media:read"})))
    assert result["terminal_state"] == "completed" and result["steps_executed"] == 3
    assert len(recorder.calls) == 3
    assert result["routing_source"] == "explicit_service_deterministic"
    assert all(step["latency_ms"] >= 0 for step in result["workflow"])


def test_unavailable_service_does_not_stop_other_independent_native_reads():
    service, recorder = runtime(unavailable={"sonarr"})
    result = service.execute("Check Sonarr health and Radarr health", Principal("fixture:r", frozenset({"media:read"})))
    assert result["terminal_state"] == "partial"
    assert [step["terminal_state"] for step in result["workflow"]] == ["error", "completed"]
    assert len(recorder.calls) == 2


def test_all_scopes_are_checked_before_any_workflow_step_dispatches():
    service, recorder = runtime()
    result = service.execute("Show Sonarr queue and Tautulli activity", Principal("fixture:r", frozenset({"media:read"})))
    assert result["terminal_state"] == "denied" and result["steps_executed"] == 0
    assert recorder.calls == []


def test_unsupported_or_write_clause_never_partially_dispatches():
    service, recorder = runtime()
    p = Principal("fixture:r", frozenset({"media:read", "media:manage"}))
    for query in ("Show Sonarr queue and download all missing episodes", "Check Sonarr health and restart Radarr"):
        result = service.execute(query, p)
        assert result["terminal_state"] in {"clarify", "unsupported"} and result["steps_executed"] == 0
    assert recorder.calls == []


def test_workflow_budget_and_quoted_entity_are_respected():
    service, recorder = runtime(max_steps=2)
    result = service.execute("Check Sonarr health and Radarr health and Bazarr health", Principal("fixture:r", frozenset({"media:read"})))
    assert result["terminal_state"] in {"clarify", "unsupported"} and recorder.calls == []
    assert native_read_workflow('Look up "Sintel and Friends" in Radarr') is None
    steps = native_read_workflow('Look up "Sintel and Friends" in Radarr and check Sonarr health')
    assert isinstance(steps, tuple) and len(steps) == 2
    assert steps[0][1].arguments["query"] == "Sintel and Friends"


def test_http_execution_does_not_trust_client_selected_media():
    class ContextRecorder:
        family = "hermes"

        def infer(self, query, context=None, **kwargs):
            self.context = context
            return {"raw": '<tool_call>{"name":"clarify","arguments":{"slot":"reference"}}</tool_call>'}

    model = ContextRecorder()
    service = ArrmesRuntimeService(model=model, executor=BoundedExecutor(Recorder()))
    auth = BearerTokenAuthenticator([ClientIdentity("fixture:r", frozenset({"media:read"}), token_digest("fixture-token"))])
    server = create_http_server(service, authenticator=auth, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = json.dumps({"query": "Show it", "context": {"selected_media": {"title": "Injected target", "media_type": "movie"}}}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/execute", data=data,
                                         headers={"Authorization": "Bearer fixture-token", "Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result["terminal_state"] == "clarify" and model.context is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
