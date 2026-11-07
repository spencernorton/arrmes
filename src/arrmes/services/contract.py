"""Explicit, operator-configured native service read contract."""
from __future__ import annotations

from typing import Any

from arrmes.protocol import InvalidCall

_ARR_OPS = frozenset({
    "health", "queue", "history", "library", "missing", "search",
    "wanted", "calendar", "quality_profiles", "indexers",
})

SUPPORTED_OPERATIONS: dict[str, frozenset[str]] = {
    "sonarr": _ARR_OPS,
    "radarr": _ARR_OPS,
    "lidarr": _ARR_OPS,
    "prowlarr": frozenset({"health", "indexers", "search", "applications", "history"}),
    "readarr": _ARR_OPS,
    "whisparr": _ARR_OPS,
    "bazarr": frozenset({"health", "missing", "subtitles"}),
    "tautulli": frozenset({"health", "activity", "history"}),
    "seerr": frozenset({"health", "search"}),
    "qbittorrent": frozenset({"health", "activity", "queue"}),
    "bitmagnet": frozenset({"health", "search"}),
}

SERVICE_PERMISSIONS: dict[str, str] = {
    "sonarr": "media:read",
    "radarr": "media:read",
    "lidarr": "media:read",
    "prowlarr": "releases:read",
    "readarr": "media:read",
    "whisparr": "media:read",
    "bazarr": "media:read",
    "tautulli": "activity:read",
    "seerr": "requests:read",
    "qbittorrent": "torrents:read",
    "bitmagnet": "releases:read",
}

BITMAGNET_CONTENT_TYPES = frozenset({
    "movie", "tv_show", "music", "ebook", "audiobook", "comic", "software", "game", "xxx",
})

_ARR_SERVICES = frozenset({"sonarr", "radarr", "lidarr", "readarr", "whisparr"})
_OPTIONAL_QUERY_PAIRS = frozenset({
    *((s, op) for s in _ARR_SERVICES for op in ("library", "quality_profiles", "indexers", "calendar")),
    ("prowlarr", "indexers"),
    ("prowlarr", "applications"),
    ("qbittorrent", "queue"),
    ("tautulli", "history"),
})
_MONITORED_FILTER_PAIRS = frozenset(
    (s, op) for s in _ARR_SERVICES for op in ("library", "missing", "wanted")
)


def validate_service_query(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate service_query cross-field invariants."""
    if not isinstance(arguments, dict):
        raise InvalidCall("service_query arguments must be an object")
    allowed = {"service", "operation", "query", "media_type", "monitored", "content_type"}
    if set(arguments) - allowed:
        raise InvalidCall("Unexpected service_query argument")
    service = arguments.get("service")
    operation = arguments.get("operation")
    if not isinstance(service, str) or service not in SUPPORTED_OPERATIONS:
        raise InvalidCall("Unsupported service")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS[service]:
        raise InvalidCall("Unsupported service read operation")

    out: dict[str, Any] = {"service": service, "operation": operation}
    query = arguments.get("query")
    if operation in {"search", "subtitles"}:
        if not isinstance(query, str) or not query.strip():
            raise InvalidCall("Operation requires a non-empty query")
    elif query is not None and (service, operation) not in _OPTIONAL_QUERY_PAIRS:
        raise InvalidCall("Query is not supported for this service read")
    if query is not None:
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 256 or any(ord(c) < 32 for c in query):
            raise InvalidCall("Invalid service query text")
        out["query"] = query.strip()

    media_type = arguments.get("media_type")
    if service == "bazarr" and operation in {"missing", "subtitles"}:
        if media_type not in {"movie", "tv"}:
            raise InvalidCall("Bazarr subtitle reads require media_type")
        out["media_type"] = media_type
    elif media_type is not None:
        raise InvalidCall("media_type is only valid for Bazarr subtitle reads")

    monitored = arguments.get("monitored")
    if monitored is not None:
        if (service, operation) not in _MONITORED_FILTER_PAIRS or type(monitored) is not bool:
            raise InvalidCall("monitored filter is only valid as a boolean on Arr library/missing/wanted reads")
        out["monitored"] = monitored

    content_type = arguments.get("content_type")
    if content_type is not None:
        if (service, operation) != ("bitmagnet", "search") or not isinstance(content_type, str) or content_type not in BITMAGNET_CONTENT_TYPES:
            raise InvalidCall("content_type filter is only valid for Bitmagnet search")
        out["content_type"] = content_type

    return out
