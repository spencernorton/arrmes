import json

import pytest

from arrmes.auth import token_digest
from arrmes import server


def test_http_service_refuses_direct_mutations(monkeypatch):
    monkeypatch.setenv("ARRMES_WRITE_MODE", "direct")
    with pytest.raises(SystemExit, match="use confirm"):
        server.main()


def test_server_discovers_explicit_gguf_file_and_rejects_missing_explicit_paths(tmp_path, monkeypatch):
    path = tmp_path / "pinned.GGUF"
    path.write_bytes(b"fixture")
    monkeypatch.setenv("ARRMES_MODEL_PATH", str(path))
    monkeypatch.delenv("ARRMES_ADAPTER_PATH", raising=False)
    model, _ = server._autodiscover_model_and_adapter()
    assert model == str(path)
    monkeypatch.setenv("ARRMES_MODEL_PATH", str(tmp_path / "missing.gguf"))
    with pytest.raises(ValueError, match="ARRMES_MODEL_PATH"):
        server._autodiscover_model_and_adapter()
    monkeypatch.setenv("ARRMES_MODEL_PATH", str(path))
    monkeypatch.setenv("ARRMES_ADAPTER_PATH", str(tmp_path / "missing-adapter"))
    with pytest.raises(ValueError, match="ARRMES_ADAPTER_PATH"):
        server._autodiscover_model_and_adapter()


def test_server_keeps_explicit_adapter_for_backend_rejection_and_closes_managed_runtime(tmp_path, monkeypatch):
    model_path = tmp_path / "pinned.gguf"
    model_path.write_bytes(b"fixture")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps({"clients": [{"subject": "fixture:r", "token_sha256": token_digest("fixture-token"), "scopes": ["media:read"]}]}))
    services = tmp_path / "services.json"
    services.write_text('{"services": {}}')
    for key, value in {"ARRMES_MODEL_PATH": str(model_path), "ARRMES_ADAPTER_PATH": str(adapter),
                       "ARRMES_CLIENTS_FILE": str(clients), "ARRMES_SERVICES_FILE": str(services),
                       "ARRMES_BACKEND": "auto"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ARRMES_GATEWAY_URL", raising=False)
    monkeypatch.delenv("ARRMES_INFERENCE_URL", raising=False)
    seen = {}

    class Model:
        family = "hermes"
        backend = "llama-server"

        def close(self):
            seen["model_closed"] = True

    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            seen["server_closed"] = True

    def create(path, **kwargs):
        seen.update(path=path, **kwargs)
        return Model()

    monkeypatch.setattr(server, "create_model", create)
    monkeypatch.setattr(server, "create_http_server", lambda *args, **kwargs: Server())
    with pytest.raises(KeyboardInterrupt):
        server.main()
    assert seen["adapter"] == str(adapter)  # Never silently omit a caller-selected LoRA.
    assert seen["path"] == str(model_path)
    assert seen["model_closed"] and seen["server_closed"]
