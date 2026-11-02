"""Small, read-only native service contract, separate from the frozen v2 model tools."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

SUPPORTED_OPERATIONS = MappingProxyType({
    "sonarr": frozenset({"health", "queue", "history", "library", "missing", "search"}),
    "radarr": frozenset({"health", "queue", "history", "library", "missing", "search"}),
    "lidarr": frozenset({"health", "queue", "history", "library", "missing", "search"}),
    "bazarr": frozenset({"health", "missing", "subtitles"}),
    "qbittorrent": frozenset({"health", "queue", "activity"}),
    "tautulli": frozenset({"health", "activity", "history"}),
    "seerr": frozenset({"health", "search"}),
})
SERVICE_PERMISSIONS = MappingProxyType({
    "sonarr": "media:read", "radarr": "media:read", "lidarr": "media:read",
    "bazarr": "media:read", "seerr": "media:read",
    "qbittorrent": "torrents:read", "tautulli": "activity:read",
})


def validate_service_query(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate the service/operation pair and pair-specific optional arguments.

    This boundary is also used by the adapter. A permissive union of JSON tool
    enums alone cannot express these constraints safely.
    """
    from arrmes.protocol import InvalidCall

    if not isinstance(arguments, dict) or set(arguments) - {"service", "operation", "query", "media_type"}:
        raise InvalidCall("Invalid service query arguments")
    service, operation = arguments.get("service"), arguments.get("operation")
    if not isinstance(service, str) or service not in SUPPORTED_OPERATIONS:
        raise InvalidCall("Unsupported native service")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS[service]:
        raise InvalidCall("Unsupported operation for this service")
    query = arguments.get("query")
    if "query" in arguments:
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or any(ord(c) < 32 for c in query):
            raise InvalidCall("Invalid service query text")
        if operation not in {"search", "library", "subtitles", "queue"} or (operation == "queue" and service != "qbittorrent"):
            raise InvalidCall("This operation does not accept a text filter")
    if operation in {"search", "subtitles"} and query is None:
        raise InvalidCall("This operation requires query text")
    if service == "bazarr" and operation in {"missing", "subtitles"}:
        if arguments.get("media_type") not in {"movie", "tv"}:
            raise InvalidCall("Bazarr requires an explicit movie or TV scope")
    elif "media_type" in arguments:
        raise InvalidCall("This operation does not accept a media type")
    return dict(arguments)
