"""Portable backend boundary checks; no weights or native runtime required."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading

import pytest

from arrmes.backends import InferenceError, LlamaServerModel, create_model
from arrmes.protocol import Call, decode, encode


@contextmanager
def native_fixture(content=None, *, invalid_json=False, status=200):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            self.send_response(status)
            self.end_headers()
            result = ({"prompt": "native rendered prompt"} if self.path == "/apply-template" else
                      {"content": content, "tokens_evaluated": 123, "tokens_predicted": 20,
                       "timings": {"predicted_per_second": 50}, "stopped_limit": False})
            self.wfile.write(b"not json" if invalid_json else json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_output_and_verifier_remain_separate():
    proposed = encode(Call("tv_search_missing", {"title": "Sintel"}), "hermes")
    with native_fixture(proposed) as (url, calls):
        with LlamaServerModel("fixture-model", endpoint=url) as model:
            result = model.infer("Only list missing episodes of Sintel; do not download anything")
            assert result["raw_unverified"] == proposed
            assert decode(result["raw"], "hermes").name != "tv_search_missing"
            assert result["wall_seconds"] >= result["seconds"]
            assert result["peak_memory_gb"] is None
            assert result["first_token_seconds"] is None
            assert result["device"] == "external"
    template, completion = calls
    assert template[0] == "/apply-template"
    assert template[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert template[1]["parallel_tool_calls"] is False
    assert "tools" in template[1]
    assert "principal" not in template[1] and "scopes" not in template[1]
    assert completion[0] == "/completion"
    assert completion[1]["temperature"] == 0
    assert completion[1]["cache_prompt"] is True
    assert "grammar" not in completion[1] and "json_schema" not in completion[1]


def test_native_completion_rejects_invalid_envelopes_and_bounds():
    with native_fixture(["not a string"]) as (url, _):
        with LlamaServerModel("fixture-model", endpoint=url) as model:
            with pytest.raises(InferenceError, match="invalid native completion"):
                model.infer("What is playing?")
            for n in (0, True, 4097):
                with pytest.raises(ValueError, match="max_tokens"):
                    model.infer("What is playing?", max_tokens=n)
            with pytest.raises(ValueError, match="Only selected_media"):
                model.infer("What is playing?", {"scopes": ["media:manage"]})
        with pytest.raises(InferenceError, match="closed"):
            model.infer("What is playing?")


def test_native_server_failure_is_not_a_model_prediction():
    with native_fixture("", status=503) as (url, _):
        with LlamaServerModel("fixture-model", endpoint=url) as model:
            with pytest.raises(InferenceError, match="HTTP 503"):
                model.infer("What is playing?")
    with native_fixture("", invalid_json=True) as (url, _):
        with LlamaServerModel("fixture-model", endpoint=url) as model:
            with pytest.raises(InferenceError, match="malformed JSON"):
                model.infer("What is playing?")


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://" + "fixture-user" + ":" + "fixture-password" + "@localhost:8080", "http://localhost:8080/completion", "http://localhost:8080?key=value"])
def test_endpoint_must_be_an_explicit_origin(url):
    with pytest.raises(ValueError, match="origin"):
        LlamaServerModel("fixture-model", endpoint=url)


def test_unfused_adapters_are_rejected_for_gguf():
    with pytest.raises(InferenceError, match="fused adapter"):
        create_model("example.gguf", "hermes", adapter="old-adapter")


def test_optional_mlx_fails_clearly_off_platform(monkeypatch):
    monkeypatch.setattr("arrmes.backends.platform.system", lambda: "Windows")
    monkeypatch.setattr("arrmes.backends.platform.machine", lambda: "AMD64")
    with pytest.raises(InferenceError, match="use a GGUF"):
        create_model("models/mlx", backend="mlx")


def test_missing_runtime_and_unavailable_requested_device(tmp_path, monkeypatch):
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"GGUF")
    monkeypatch.setattr("arrmes.backends.shutil.which", lambda _: None)
    with pytest.raises(InferenceError, match="executable not found"):
        LlamaServerModel(str(model_path))
    monkeypatch.setattr("arrmes.backends.subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "Available devices:\n  MTL0: Apple GPU", ""))
    with pytest.raises(InferenceError, match="cuda device is unavailable"):
        LlamaServerModel._select_device("llama-server", "cuda")
    assert LlamaServerModel._select_device("llama-server", "metal") == "MTL0"
    assert LlamaServerModel._select_device("llama-server", "cpu") is None


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="Backend must"):
        create_model("models/whatever", backend="made-up")


def test_local_model_has_no_router_assisted_overwrite():
    import inspect
    from arrmes.model import HybridRouterMLXModel, LocalModel

    sig = inspect.signature(LocalModel.__init__)
    assert "router_assisted" not in sig.parameters
    infer_src = inspect.getsource(LocalModel.infer)
    assert "_router" not in infer_src
    assert "router_assisted" not in infer_src
    assert HybridRouterMLXModel.backend == "hybrid-router-mlx"
