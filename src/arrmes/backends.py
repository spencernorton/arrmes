"""Inference backends with no mandatory GPU or model-framework dependency.

The portable path delegates only inference to a native llama.cpp server. Model
output still passes through Arrmes' normal protocol and grounding verifier.
No service credential or caller permission is sent to the inference runtime.
"""

from __future__ import annotations

import atexit
import json
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .catalog import tools, tools_v2
from .model import LocalModel, finalize_output, messages


class InferenceError(RuntimeError):
    """A backend configuration, availability, or bounded response failure."""


class RouterModel(Protocol):
    path: str
    family: str
    adapter: str | None
    contract_version: str
    load_seconds: float

    def infer(self, query: str, context: dict | None = None, max_tokens: int = 160) -> dict[str, Any]: ...


def _positive_int(value: int, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer between {lower} and {upper}")
    return value


class LlamaServerModel:
    """A local GGUF model, or an explicitly configured native llama-server.

    /apply-template renders the GGUF's own template. /completion returns its
    native output without a JSON grammar or tool-choice coercion. This avoids
    mistaking a runtime's format repair for model accuracy. Requests serialize
    over one slot, bounding KV-cache use and keeping cache ownership explicit.
    """

    backend = "llama-server"

    def __init__(
        self,
        path: str,
        family: str = "hermes",
        adapter: str | None = None,
        *,
        contract_version: str = "v2",
        endpoint: str | None = None,
        device: str = "cpu",
        executable: str = "llama-server",
        context_size: int = 4096,
        threads: int | None = None,
        startup_timeout: float = 90,
        request_timeout: float = 120,
    ):
        if family not in {"functiongemma", "hermes"}:
            raise ValueError("Unknown model family")
        if contract_version not in {"v1", "v2"}:
            raise ValueError("Unknown contract version")
        if adapter is not None:
            raise InferenceError("GGUF inference requires a fused adapter; convert and qualify the fused checkpoint first")
        if device not in {"cpu", "cuda", "metal", "vulkan", "sycl"}:
            raise ValueError("Device must be cpu, cuda, metal, vulkan, or sycl")
        _positive_int(context_size, "context_size", 512, 131072)
        if threads is not None:
            _positive_int(threads, "threads", 1, 1024)
        if not 0 < startup_timeout <= 600 or not 0 < request_timeout <= 600:
            raise ValueError("Inference timeouts must be positive and at most 600 seconds")
        self.family = family
        self.adapter = None
        self.contract_version = contract_version
        self.path = str(Path(path).resolve()) if not endpoint else path
        self.device = device if endpoint is None else "external"
        self.context_size = context_size
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._log = None
        self._closed = False
        self._managed = endpoint is None
        started = time.perf_counter()
        if endpoint is not None:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                raise ValueError("Inference endpoint must be an HTTP(S) origin without credentials, path, query, or fragment")
            self.endpoint = endpoint.rstrip("/")
            self._json("GET", "/health")
        else:
            model_path = Path(path).expanduser().resolve()
            if model_path.suffix.lower() != ".gguf" or not model_path.is_file():
                raise InferenceError("llama-server requires an existing local .gguf file")
            self.path = str(model_path)
            binary = shutil.which(executable)
            if binary is None:
                raise InferenceError("llama-server executable not found; install a CPU or accelerator build and set ARRMES_LLAMA_SERVER")
            selected_device = self._select_device(binary, device)
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            self.endpoint = f"http://127.0.0.1:{port}"
            command = [binary, "--model", self.path, "--host", "127.0.0.1", "--port", str(port),
                       "--ctx-size", str(context_size), "--parallel", "1", "--jinja", "--no-webui",
                       "--n-gpu-layers", "0" if device == "cpu" else "all"]
            if selected_device:
                command += ["--device", selected_device]
            if threads is not None:
                command += ["--threads", str(threads), "--threads-batch", str(threads)]
            self._tmp = tempfile.TemporaryDirectory(prefix="arrmes-llama-")
            self._log = open(Path(self._tmp.name) / "server.log", "w+b")
            try:
                self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + startup_timeout
                while time.monotonic() < deadline:
                    if self._process.poll() is not None:
                        raise InferenceError("llama-server exited during startup; check the GGUF, context size, and selected device")
                    try:
                        self._json("GET", "/health", timeout=min(1, startup_timeout))
                        break
                    except InferenceError:
                        time.sleep(0.1)
                else:
                    raise InferenceError("llama-server startup timed out; check model size and available RAM")
            except BaseException:
                self.close()
                raise
        self.load_seconds = time.perf_counter() - started
        atexit.register(self.close)

    @staticmethod
    def _select_device(binary: str, requested: str) -> str | None:
        if requested == "cpu":
            return None
        try:
            devices = subprocess.run([binary, "--list-devices"], capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InferenceError("Cannot query llama-server accelerator support") from exc
        prefix = {"cuda": "CUDA", "metal": "MTL", "vulkan": "Vulkan", "sycl": "SYCL"}[requested]
        for line in (devices.stdout + devices.stderr).splitlines():
            name = line.strip().split(":", 1)[0]
            if name.startswith(prefix):
                return name
        raise InferenceError(f"Requested {requested} device is unavailable in this llama-server build; use cpu or install the matching build")

    def _json(self, method: str, path: str, payload: dict | None = None, *, timeout: float | None = None) -> dict:
        if self._closed:
            raise InferenceError("Inference backend is closed")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(self.endpoint + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout or self.request_timeout) as response:
                body = response.read(1048577)
            if len(body) > 1048576:
                raise InferenceError("Inference response exceeds the 1 MiB limit")
            result = json.loads(body)
        except HTTPError as exc:
            raise InferenceError(f"llama-server rejected the inference request (HTTP {exc.code})") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise InferenceError("llama-server is unavailable or the inference request timed out") from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise InferenceError("llama-server returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise InferenceError("llama-server returned an invalid response envelope")
        return result

    def render_prompt(self, query: str, context: dict | None = None) -> str:
        payload = {"messages": messages(query, context, self.family, contract_version=self.contract_version),
                   "tools": tools_v2() if self.contract_version == "v2" else tools(),
                   "parallel_tool_calls": False, "chat_template_kwargs": {"enable_thinking": False},
                   "reasoning_format": "none"}
        rendered = self._json("POST", "/apply-template", payload).get("prompt")
        if not isinstance(rendered, str) or not rendered or len(rendered) > 131072:
            raise InferenceError("llama-server returned an invalid rendered prompt")
        return rendered

    def infer(self, query: str, context: dict | None = None, max_tokens: int = 160) -> dict[str, Any]:
        _positive_int(max_tokens, "max_tokens", 1, 4096)
        started = time.perf_counter()
        with self._lock:
            prompt = self.render_prompt(query, context)
            generation_start = time.perf_counter()
            response = self._json("POST", "/completion", {"prompt": prompt, "n_predict": max_tokens,
                                  "temperature": 0, "seed": 0, "stream": False, "cache_prompt": True,
                                  "id_slot": 0, "stop": ["<start_function_response>"] if self.family == "functiongemma" else []})
            generation_seconds = time.perf_counter() - generation_start
        raw_unverified = response.get("content")
        if not isinstance(raw_unverified, str) or len(raw_unverified) > 16384:
            raise InferenceError("llama-server returned an invalid native completion")
        raw = finalize_output(raw_unverified, query, context, self.family, self.contract_version)
        native = response.get("timings", {})
        if not isinstance(native, dict):
            native = {}
        return {"raw": raw, "raw_unverified": raw_unverified, "seconds": round(generation_seconds, 6),
                "wall_seconds": round(time.perf_counter() - started, 6), "first_token_seconds": None,
                "prompt_tokens": response.get("tokens_evaluated"), "generation_tokens": response.get("tokens_predicted"),
                "generation_tps": native.get("predicted_per_second"), "peak_memory_gb": None,
                "max_tokens": max_tokens, "backend": self.backend, "device": self.device,
                "native_timings": native, "truncated": response.get("stopped_limit", False)}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        if self._log is not None:
            self._log.close()
        if self._tmp is not None:
            self._tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def create_model(path: str, family: str = "functiongemma", adapter: str | None = None, *,
                 contract_version: str = "v1", backend: str = "auto", endpoint: str | None = None,
                 device: str = "cpu", executable: str = "llama-server", context_size: int = 4096,
                 threads: int | None = None) -> RouterModel:
    """Choose a backend explicitly, or detect a local GGUF versus MLX directory."""
    if backend == "auto":
        backend = "llama-server" if endpoint or Path(path).suffix.lower() == ".gguf" else "mlx"
    if backend == "mlx":
        if endpoint is not None:
            raise ValueError("The MLX backend does not use an inference endpoint")
        if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
            raise InferenceError("MLX inference requires Apple Silicon macOS; use a GGUF with backend=llama-server on this platform")
        try:
            return LocalModel(path, family, adapter, contract_version=contract_version)
        except ImportError as exc:
            raise InferenceError("MLX backend is optional; install arrmes[mlx] on Apple Silicon or select llama-server") from exc
    if backend == "llama-server":
        return LlamaServerModel(path, family, adapter, contract_version=contract_version, endpoint=endpoint,
                                device=device, executable=executable, context_size=context_size, threads=threads)
    raise ValueError("Backend must be auto, mlx, or llama-server")
