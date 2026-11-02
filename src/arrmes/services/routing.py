"""Conservative explicit-service NL routes; these are not v6 neural predictions."""
from __future__ import annotations

import re

from arrmes.protocol import Call
from .contract import SUPPORTED_OPERATIONS, validate_service_query

_SERVICES = re.compile(r"\b(sonarr|radarr|lidarr|bazarr|qbittorrent|qbt|tautulli|seerr|seer|bitmagnet)\b", re.I)
_QUOTED = re.compile(r'"([^"\n]+)"|\'([^\'\n]+)\'')
_WRITE = re.compile(r"\b(download|delete|remove|restart|reboot|shutdown|pause|resume|stop|start|update|upgrade|scan|sync|grab|upload|add|change|set)\b", re.I)
_COMPOUND = re.compile(r"\b(and\s+then|then|and\s+also|after\s+that|followed\s+by)\b|[;\n]", re.I)
_UNSAFE = re.compile(r"\b(ignore|bypass|override|credentials?|passwords?|tokens?|api\s*keys?|instructions?|system\s*prompt|sudo|shell)\b", re.I)
_READ_WORDS = frozenset("""service entity please show list get tell me what which is are the a an my our in on for of
    whether currently check checks display inspect give report look up lookup catalog metadata search health healthy
    online reachable running queue history recent events library inventory missing wanted activity streams playing
    transfer stats statistics speed speeds bandwidth torrents torrent subtitles subtitle subs movie movies film films
    tv series episodes episode shows do does have has how many if matching""".split())


def route_service_query(query: str) -> Call | None:
    """Return a deterministic v3 read or abstention, or leave a v2 intent alone.

    Service names inside quoted entity text are ignored. Entity-bearing new
    lookup/subtitle routes require a single quoted surface span. Missing titles,
    conflicting scopes and compounds clarify/refuse instead of guessing.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        return None
    quoted = [next(v for v in m.groups() if v is not None) for m in _QUOTED.finditer(query)]
    plain = _QUOTED.sub(" <ENTITY> ", query)
    mentions = _SERVICES.findall(plain)
    services = {"qbittorrent" if s.lower() == "qbt" else "seerr" if s.lower() == "seer" else s.lower() for s in mentions}
    if not services:
        return None
    if len(services) != 1 or _COMPOUND.search(plain):
        return Call("unsupported", {"reason": "complex"})
    service = next(iter(services))
    if _UNSAFE.search(plain):
        return Call("unsupported", {"reason": "unsafe"})
    if service == "seerr" and re.search(r"\brequests?\b", plain, re.I) and not _WRITE.search(plain):
        return None
    if service == "bitmagnet" and re.search(r"\b(import|reprocess|reclassify|classify|enqueue|purge)\b", plain, re.I):
        return Call("unsupported", {"reason": "out_of_scope"})
    if _WRITE.search(plain):
        # Preserve existing v2 confirmation-gated write intents; Bazarr and
        # Seerr mutations have no implementation and must not fall through.
        return Call("unsupported", {"reason": "out_of_scope"}) if service in {"bazarr", "seerr", "bitmagnet"} else None
    operations: set[str] = set()
    if re.search(r"\b(health|healthy|online|reachable|running)\b", plain, re.I):
        operations.add("health")
    if re.search(r"\bqueue\b", plain, re.I):
        operations.add("queue")
    if re.search(r"\b(history|recent\s+events)\b", plain, re.I):
        operations.add("history")
    if re.search(r"\b(library|inventory)\b", plain, re.I):
        operations.add("library")
    if re.search(r"\b(missing|wanted)\b", plain, re.I):
        operations.add("missing")
    if re.search(r"\b(activity|streams|playing|transfer\s+(?:stats|speeds?)|bandwidth)\b", plain, re.I):
        operations.add("activity")
    if re.search(r"\b(look\s*up|lookup|metadata\s+search|catalog\s+search|search\s+(?:the\s+)?(?:catalog|metadata))\b", plain, re.I):
        operations.discard("library")
        operations.add("search")
    if service == "bitmagnet" and re.search(r"\b(search|find)\b", plain, re.I):
        operations.add("search")
    if service == "bazarr" and re.search(r"\b(subtitles?|subs)\b", plain, re.I) and "missing" not in operations:
        operations.add("subtitles")
    if service == "qbittorrent" and re.search(r"\btorrents?\b", plain, re.I) and not operations:
        # Filtered v2 torrent_list already preserves transfer-state intent.
        if re.search(r"\b(paused|stopped|downloading|seeding|completed|stalled)\b", plain, re.I):
            return None
        operations.add("queue")
    if not operations:
        return None
    if len(operations) != 1:
        return Call("unsupported", {"reason": "complex"})
    operation = next(iter(operations))
    if operation not in SUPPORTED_OPERATIONS[service]:
        return Call("unsupported", {"reason": "out_of_scope"})
    if service == "bitmagnet" and operation == "search" and not quoted:
        # Preserve the released neural search intent. The model-free mode
        # deliberately clarifies here; quoted native routes remain stricter.
        return None
    # Existing entity-specific missing queries belong to the frozen v2 route.
    if operation == "missing" and service != "bazarr" and quoted:
        return None
    if operation == "queue" and service == "qbittorrent" and re.search(r"\b(paused|stopped|downloading|seeding|completed|stalled)\b", plain, re.I):
        return None
    args = {"service": service, "operation": operation}
    if operation in {"search", "subtitles"} or (quoted and (operation == "library" or (operation == "queue" and service == "qbittorrent"))):
        if len(quoted) != 1:
            return Call("clarify", {"slot": "title" if not quoted else "reference"})
        args["query"] = quoted[0]
    elif quoted:
        # A title-filtered health/history query is not silently broadened.
        return None if service == "tautulli" and operation == "history" else Call("clarify", {"slot": "scope"})
    if service == "bazarr" and operation in {"missing", "subtitles"}:
        movie = bool(re.search(r"\b(movies?|films?)\b", plain, re.I))
        tv = bool(re.search(r"\b(tv|series|episodes?|shows)\b", plain, re.I))
        if movie == tv:
            return Call("clarify", {"slot": "media_type"})
        args["media_type"] = "movie" if movie else "tv"
    # Unimplemented filters (language, date range, transfer state, exclusions,
    # ordering, etc.) must not silently turn into a broader inventory query.
    words = set(re.findall(r"[A-Za-z0-9_]+", _SERVICES.sub(" SERVICE ", plain).lower()))
    if words - _READ_WORDS:
        filler = {"current", "now", "using", "via", "from"}
        if service == "tautulli":
            filler |= {"plex"}
        if service == "bitmagnet":
            filler |= {"find"}
        if words - _READ_WORDS - filler:
            return Call("clarify", {"slot": "scope"})
    return Call("service_query", validate_service_query(args))
