"""Bounded independent native reads. No model-generated execution programs."""
from __future__ import annotations

import re

from .protocol import Call, validate_call
from .services import route_service_query

_PART = re.compile(r'''"[^"\n]*"|'[^'\n]*'|\b(?:and(?:\s+then)?|then)\b''', re.I)


def native_read_workflow(query: str, *, max_steps: int = 4) -> tuple[tuple[str, Call], ...] | Call | None:
    """Each clause must independently name a service and one supported read.

    Quoted titles keep their conjunctions. If any clause is unclear, unsupported
    or a write, the whole request abstains before dispatch. This is deliberately
    limited to independent reads: no inferred target, conditional, or write.
    """
    splits = [m for m in _PART.finditer(query) if not m.group().startswith(('"', "'"))]
    if not splits:
        return None
    if len(splits) + 1 > max_steps:
        return Call("unsupported", {"reason": "complex"})
    clauses: list[str] = []
    start = 0
    for match in splits:
        clauses.append(query[start:match.start()].strip())
        start = match.end()
    clauses.append(query[start:].strip())
    steps = []
    for clause in clauses:
        call = route_service_query(clause)
        if call is None or call.name != "service_query":
            return Call("unsupported", {"reason": "complex"})
        steps.append((clause, validate_call(call.name, call.arguments)))
    return tuple(steps)
