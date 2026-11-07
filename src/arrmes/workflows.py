'''Bounded independent native reads and read-to-confirm workflows. No model-generated execution programs.'''
from __future__ import annotations

import re
from typing import Any, Callable

from .adapters import AdapterObservation
from .executor import BoundedExecutor, SessionEntityLedger
from .planner import Principal
from .protocol import Call, validate_call
from .services import route_service_query

_PART = re.compile(r'''"[^"\n]*"|'[^'\n]*'|\b(?:and(?:\s+then)?|then)\b''', re.I)


def native_read_workflow(query: str, *, max_steps: int = 4) -> tuple[tuple[str, Call], ...] | Call | None:
    '''Each clause must independently name a service and one supported read.

    Quoted titles keep their conjunctions. If any clause is unclear, unsupported
    or a write, the whole request abstains before dispatch. This is deliberately
    limited to independent reads: no inferred target, conditional, or write.
    '''
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


def build_read_to_confirm_steps(
    read_call: Call,
    *,
    season: int | None = None,
) -> list[Callable[[list[AdapterObservation], SessionEntityLedger], Call | None]]:
    '''Build a deterministic 2-step read-then-confirm workflow for BoundedExecutor.

    Step 1 executes a verified read (`tv_missing`, `movie_status`, `music_status`,
    or `bazarr_subtitle_search`).
    Step 2 inspects the verified observation from Step 1 and, only when eligible
    missing targets exist, proposes the corresponding confirmation-gated write
    bound to the exact resolved entity.
    '''
    read_call = validate_call(read_call.name, read_call.arguments)
    if read_call.name not in {"tv_missing", "movie_status", "music_status", "bazarr_subtitle_search"}:
        raise ValueError(f"Unsupported read-to-confirm seed action: {read_call.name}")

    def step_read(_observations: list[AdapterObservation], _ledger: SessionEntityLedger) -> Call | None:
        return read_call

    def step_propose_write(observations: list[AdapterObservation], _ledger: SessionEntityLedger) -> Call | None:
        if not observations:
            return None
        obs = observations[-1]
        if obs.status != "verified_ok" or not obs.verified_complete:
            return None
        data = obs.data
        if read_call.name == "tv_missing":
            missing_count = data.get("missing_count", 0)
            title = data.get("title")
            if not isinstance(missing_count, int) or missing_count <= 0 or not isinstance(title, str) or not title:
                return None
            args: dict[str, Any] = {"title": title}
            if season is not None:
                episodes = data.get("missing_episodes")
                if isinstance(episodes, list) and not any(
                    isinstance(ep, dict) and ep.get("seasonNumber") == season for ep in episodes
                ):
                    return None
                args["season"] = season
            return validate_call("tv_search_missing", args)

        if read_call.name == "movie_status":
            if data.get("has_file") is not False:
                return None
            title = data.get("title")
            if not isinstance(title, str) or not title:
                return None
            args = {"title": title}
            if type(data.get("year")) is int:
                args["year"] = data["year"]
            return validate_call("movie_search", args)

        if read_call.name == "music_status":
            artist = data.get("artist")
            if not isinstance(artist, str) or not artist:
                return None
            albums = data.get("albums")
            if isinstance(albums, list) and albums:
                has_missing = any(
                    isinstance(a, dict)
                    and a.get("monitored") is not False
                    and int((a.get("statistics") or {}).get("percentOfTracks", 0) if isinstance(a.get("statistics"), dict) else 0) < 100
                    for a in albums
                )
                if not has_missing:
                    return None
            args = {"artist": artist}
            if "album" in read_call.arguments:
                args["album"] = read_call.arguments["album"]
            return validate_call("music_search", args)

        if read_call.name == "bazarr_subtitle_search":
            if int(data.get("returned_count", 0) or 0) <= 0:
                return None
            return validate_call("bazarr_subtitle_download", dict(read_call.arguments))

        return None

    return [step_read, step_propose_write]


def execute_read_to_confirm_workflow(
    executor: BoundedExecutor,
    read_call: Call,
    principal: Principal,
    *,
    season: int | None = None,
) -> dict[str, Any]:
    '''Execute a bounded read-to-confirm workflow and return the BoundedExecutor workflow result.'''
    steps = build_read_to_confirm_steps(read_call, season=season)
    return executor.execute_workflow(steps, principal)
