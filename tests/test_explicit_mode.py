import pytest

from arrmes.executor import BoundedExecutor
from arrmes.explicit import NativeOnlyModel
from arrmes.planner import Principal
from arrmes.server import ArrmesRuntimeService
from arrmes.protocol import decode
from arrmes.services import route_service_query


def test_native_only_abstains_without_loading_or_selecting_legacy_actions():
    model = NativeOnlyModel()
    for query in ("Download the missing episodes of Sintel", "Pause the torrent Sintel", "What is that one?"):
        output = model.infer(query)
        assert decode(output["raw"], "hermes").name == "clarify"
        assert output["peak_memory_gb"] is None and output["generation_tokens"] is None
    assert model.backend == "native-only"


def test_explicit_native_mode_reports_no_neural_model_and_does_not_dispatch_unknown_intents():
    class Adapter:
        supports_service_queries = True

        def execute(self, *args, **kwargs):
            raise AssertionError("Unknown requests must not dispatch")

    service = ArrmesRuntimeService(model=NativeOnlyModel(), executor=BoundedExecutor(Adapter(), allow_writes=False), write_mode="disabled")
    assert service.health()["neural_model_loaded"] is False
    assert service.health()["backend"] == "native-only"
    result = service.execute("Download Sintel", Principal("fixture:r", frozenset({"media:read"})))
    assert result["terminal_state"] in {"clarify", "unsupported"} and result["steps_executed"] == 0
    assert result["routing_source"] == "native_only_abstention"


def test_common_tautulli_filler_words_do_not_drop_filters():
    call = route_service_query("Show current Plex streams using Tautulli")
    assert call.name == "service_query" and call.arguments == {"service": "tautulli", "operation": "activity"}
    assert route_service_query("Show current Plex streams using Tautulli above 20 Mbps").name == "clarify"


def test_native_only_main_requires_explicit_native_configuration(tmp_path, monkeypatch):
    from arrmes import server
    from arrmes.auth import token_digest
    import json
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps({"clients": [{"subject": "fixture:r", "token_sha256": token_digest("fixture"), "scopes": ["media:read"]}]}))
    monkeypatch.setenv("ARRMES_CLIENTS_FILE", str(clients))
    monkeypatch.setenv("ARRMES_BACKEND", "native-only")
    monkeypatch.delenv("ARRMES_SERVICES_FILE", raising=False)
    with pytest.raises(SystemExit, match="ARRMES_SERVICES_FILE"):
        server.main()


def test_native_only_main_bootstraps_without_model_or_inference_runtime(tmp_path, monkeypatch):
    from arrmes import server
    from arrmes.auth import token_digest
    import json
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps({"clients": [{"subject": "fixture:r", "token_sha256": token_digest("fixture"), "scopes": ["media:read"]}]}))
    services = tmp_path / "services.json"
    services.write_text('{"services": {}}')
    for key, value in {"ARRMES_CLIENTS_FILE": str(clients), "ARRMES_SERVICES_FILE": str(services),
                       "ARRMES_BACKEND": "native-only", "ARRMES_WRITE_MODE": "confirm",
                       "ARRMES_MODEL_PATH": str(tmp_path / "not-downloaded.gguf")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ARRMES_GATEWAY_URL", raising=False)

    def unexpected(*args, **kwargs):
        raise AssertionError("The explicit native-only mode must not create an inference backend")

    monkeypatch.setattr(server, "create_model", unexpected)
    seen = {}

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            seen["closed"] = True

    def make_server(service, **kwargs):
        assert not service.health()["neural_model_loaded"]
        assert service.health()["backend"] == "native-only"
        assert service.write_mode == "disabled" and not service.executor.allow_writes
        return FakeServer()

    monkeypatch.setattr(server, "create_http_server", make_server)
    with pytest.raises(KeyboardInterrupt):
        server.main()
    assert seen["closed"]
