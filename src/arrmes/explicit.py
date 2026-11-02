"""A deliberate model-free mode for explicit native read workflows."""
from __future__ import annotations

import time

from .model import DeterministicBaselineModel
from .protocol import Call, encode


class NativeOnlyModel(DeterministicBaselineModel):
    """Never infer a legacy action when the explicit native router abstains.

    This is separate from the benchmark's keyword baseline. It loads no weights
    or inference runtime and returns a clarification outside the documented
    explicit-service read grammar.
    """

    backend = "native-only"

    def __init__(self):
        started = time.perf_counter()
        super().__init__(family="hermes", contract_version="v2")
        self.path = "native-only"
        self.load_seconds = time.perf_counter() - started

    def infer(self, query, context=None, max_tokens=64):
        started = time.perf_counter()
        raw = encode(Call("clarify", {"slot": "scope"}), "hermes")
        return {"raw": raw, "raw_unverified": raw,
                "seconds": time.perf_counter() - started,
                "backend": self.backend, "prompt_tokens": None,
                "generation_tokens": None, "generation_tps": None,
                "peak_memory_gb": None, "first_token_seconds": None}
