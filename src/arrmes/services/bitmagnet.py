"""Native Bitmagnet GraphQL reads; never accepts a caller-supplied document."""
from __future__ import annotations

import json
import re
import time
from types import MappingProxyType
from typing import Any, Literal, TypedDict
import urllib.error
import urllib.parse
import urllib.request

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, validate_call
from .contract import validate_service_query
from .native import ServiceQueryAdapter, _UpstreamError, _count, _rows, _text


ReadOperation = Literal["health", "search"]
_DOCUMENTS = MappingProxyType({
    "health": """query ArrmesBitmagnetHealth {
  version
  health { status checks { key status } }
  workers { listAll { workers { key started } } }
}""",
    "search": """query ArrmesBitmagnetSearch($input: TorrentContentSearchQueryInput!) {
  version
  torrentContent {
    search(input: $input) {
      totalCount totalCountIsEstimate hasNextPage
      items {
        id infoHash title contentType videoResolution seeders leechers
        torrent { infoHash name size filesCount }
      }
    }
  }
}""",
})
_OPERATION_NAMES = MappingProxyType({"health": "ArrmesBitmagnetHealth", "search": "ArrmesBitmagnetSearch"})
_HASH = re.compile(r"[a-fA-F0-9]{40}")
_HEALTH = frozenset({"up", "down", "inactive", "unknown"})


def _health_state(value: Any) -> bool:
    return isinstance(value, str) and value in _HEALTH


class SearchItem(TypedDict):
    id: str
    hash: str
    title: str
    name: str
    size: int
    files_count: int | None
    seeders: int | None
    leechers: int | None
    content_type: str | None
    video_resolution: str | None
    magnet_uri: str


def _observation(status: str, args: dict[str, Any], data: dict[str, Any], audit) -> AdapterObservation:
    return AdapterObservation(status, "bitmagnet", "service_query", 200, status == "verified_ok",
                              {"source": "native_api", "api_version": "unversioned GraphQL",
                               "operation": args["operation"], **data}, audit)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate upstream JSON key")
        result[key] = value
    return result


def _post_read(native: ServiceQueryAdapter, operation: ReadOperation, variables: dict[str, Any], audit) -> dict[str, Any]:
    """POST one immutable query with variables, no redirects or automatic replay.

    Bitmagnet has no built-in API authentication. An optional X-Api-Key is for
    an operator's reverse proxy, not a claimed Bitmagnet authentication scheme.
    """
    if operation not in _DOCUMENTS:
        raise InvalidCall("Only fixed Bitmagnet read operations are permitted")
    config = native.configs["bitmagnet"]
    if config.session_id:
        raise _UpstreamError("bitmagnet_session_auth_unsupported", 503)
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "arrmes-native-bitmagnet-read/1"}
    if config.api_key:
        headers["X-Api-Key"] = config.api_key
    payload = json.dumps({"operationName": _OPERATION_NAMES[operation],
                          "query": _DOCUMENTS[operation], "variables": variables}).encode("utf-8")
    request = urllib.request.Request(config.base_url.rstrip("/") + "/graphql", data=payload,
                                     headers=headers, method="POST")
    try:
        with native._opener.open(request, timeout=native.timeout_seconds) as response:
            audit.append({"method": "POST", "path": "/graphql", "status": int(response.status),
                          "read_operation": _OPERATION_NAMES[operation], "effect": "read", "attempt": 1})
            deadline = time.monotonic() + native.timeout_seconds
            chunks, length = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise _UpstreamError("upstream_read_deadline_exceeded", 504)
                chunk = response.read1(min(65_536, native.max_response_bytes + 1 - length))
                if not chunk:
                    break
                length += len(chunk)
                if length > native.max_response_bytes:
                    raise _UpstreamError("response_limit_exceeded")
                chunks.append(chunk)
            try:
                data = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_json,
                                  parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            except (UnicodeError, ValueError, RecursionError):
                raise _UpstreamError("invalid_upstream_json") from None
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exc.close()
        audit.append({"method": "POST", "path": "/graphql", "status": status,
                      "read_operation": _OPERATION_NAMES[operation], "effect": "read", "attempt": 1})
        code = ("upstream_authentication_failed" if status in {401, 403} else
                "upstream_redirect_refused" if 300 <= status < 400 else "upstream_http_error")
        raise _UpstreamError(code, status) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        audit.append({"method": "POST", "path": "/graphql", "status": 503,
                      "read_operation": _OPERATION_NAMES[operation], "effect": "read", "attempt": 1})
        raise _UpstreamError("service_unavailable", 503) from None
    if isinstance(data, dict) and "errors" in data and data["errors"] != []:
        # GraphQL can return partial data with HTTP 200. Never call it a
        # completed read or expose raw server error text/extension values.
        raise _UpstreamError("bitmagnet_graphql_error")
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise _UpstreamError("invalid_bitmagnet_graphql_response")
    return data["data"]


def _version(data: dict[str, Any]) -> str:
    version = data.get("version")
    if not _text(version) or len(version) > 128 or any(ord(c) < 32 for c in version):
        raise _UpstreamError("invalid_bitmagnet_version")
    return version


def _optional_count(value: Any) -> int | None:
    return None if value is None else _count(value)


def _item(row: dict[str, Any]) -> SearchItem:
    torrent = row.get("torrent")
    if (not _text(row.get("id")) or len(row["id"]) > 512 or not _text(row.get("title"))
            or not isinstance(row.get("infoHash"), str) or not _HASH.fullmatch(row["infoHash"])
            or not isinstance(torrent, dict) or not _text(torrent.get("name"))
            or not isinstance(torrent.get("infoHash"), str)
            or torrent["infoHash"].lower() != row["infoHash"].lower()):
        raise _UpstreamError("invalid_bitmagnet_search_record")
    for key in ("contentType", "videoResolution"):
        if row.get(key) is not None and (not _text(row[key]) or len(row[key]) > 80):
            raise _UpstreamError("invalid_bitmagnet_search_record")
    info_hash = row["infoHash"].lower()
    # Build a minimal URI from the validated hash. Never return upstream
    # tracker URLs, download URLs, filenames or raw torrent source objects.
    return SearchItem(id=row["id"], hash=info_hash, title=row["title"][:2048], name=torrent["name"][:2048],
                      size=_count(torrent.get("size")), files_count=_optional_count(torrent.get("filesCount")),
                      seeders=_optional_count(row.get("seeders")), leechers=_optional_count(row.get("leechers")),
                      content_type=row.get("contentType"), video_resolution=row.get("videoResolution"),
                      magnet_uri="magnet:?xt=urn:btih:" + info_hash)


def _health(native, args, audit) -> AdapterObservation:
    data = _post_read(native, "health", {}, audit)
    version = _version(data)
    health, workers = data.get("health"), data.get("workers")
    if not isinstance(health, dict) or not _health_state(health.get("status")) or not isinstance(workers, dict):
        raise _UpstreamError("invalid_bitmagnet_health")
    checks = _rows(health.get("checks"))
    listing = workers.get("listAll")
    if not isinstance(listing, dict):
        raise _UpstreamError("invalid_bitmagnet_health")
    worker_rows = _rows(listing.get("workers"))
    for group, state_key in ((checks, "status"), (worker_rows, "started")):
        if len({row.get("key") for row in group if isinstance(row.get("key"), str)}) != len(group):
            raise _UpstreamError("invalid_bitmagnet_health_identity")
        if any(not _text(row.get("key")) or len(row["key"]) > 256 or
               (not _health_state(row.get("status")) if state_key == "status" else type(row.get("started")) is not bool)
               for row in group):
            raise _UpstreamError("invalid_bitmagnet_health")
    truncated = max(len(checks), len(worker_rows)) > native.max_records
    state = health["status"]
    return _observation("partial" if truncated else "verified_ok", args, {
        "server_version": version, "health_status": state,
        "healthy": state == "up" if state in {"up", "down"} else None,
        "checks": [{"key": row["key"], "status": row["status"]} for row in checks[:native.max_records]],
        "workers": [{"key": row["key"], "started": row["started"]} for row in worker_rows[:native.max_records]],
        "check_count": len(checks), "worker_count": len(worker_rows), "truncated": truncated}, audit)


def _search(native, args, audit) -> AdapterObservation:
    rows: list[SearchItem] = []
    seen: set[str] = set()
    first_version = None
    first_exact_count = None
    reported = None
    estimated = False
    reason = "page_limit"
    complete = False
    pages = 0
    for page in range(native.max_pages):
        limit = min(native.page_size, native.max_records - len(rows))
        data = _post_read(native, "search", {"input": {"queryString": args["query"],
            "limit": limit, "offset": page * native.page_size, "totalCount": True,
            "hasNextPage": True, "cached": False,
            "orderBy": [{"field": "relevance", "descending": True}, {"field": "info_hash", "descending": False}]}}, audit)
        pages += 1
        version = _version(data)
        if first_version is not None and version != first_version:
            raise _UpstreamError("bitmagnet_version_changed_during_search")
        first_version = version
        content = data.get("torrentContent")
        result = content.get("search") if isinstance(content, dict) else None
        if not isinstance(result, dict):
            raise _UpstreamError("invalid_bitmagnet_search_response")
        incoming = _rows(result.get("items"))
        if len(incoming) > limit or type(result.get("totalCountIsEstimate")) is not bool or type(result.get("hasNextPage")) is not bool:
            raise _UpstreamError("invalid_bitmagnet_search_pagination")
        reported, estimated = _count(result.get("totalCount")), result["totalCountIsEstimate"]
        parsed = [_item(row) for row in incoming]
        repeated = any(row["id"] in seen for row in parsed) or len({row["id"] for row in parsed}) != len(parsed)
        for row in parsed:
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
        if repeated or (not incoming and result["hasNextPage"]):
            reason = "nonadvancing_or_overlapping_page"
            break
        if result["hasNextPage"] and len(incoming) != limit:
            reason = "short_page_with_more_results"
            break
        if not estimated:
            if first_exact_count is None:
                first_exact_count = reported
            if reported != first_exact_count or reported < len(rows):
                reason = "search_changed_during_pagination"
                break
        if not result["hasNextPage"]:
            complete = estimated or reported == len(rows)
            reason = "exhausted" if complete else "inconsistent_final_count"
            break
        if len(rows) >= native.max_records:
            reason = "record_limit"
            break
    return _observation("verified_ok" if complete else "partial", args, {
        "query": args["query"], "server_version": first_version, "items": rows, "returned_count": len(rows),
        "total_records": len(rows) if complete else None, "reported_total_count": reported,
        "total_count_is_estimate": estimated, "truncated": not complete, "complete_listing": complete,
        "pagination_end": reason, "pages_read": pages,
        "consistency": "bounded offset scan; upstream provides no snapshot token"}, audit)


def execute_bitmagnet(native: ServiceQueryAdapter, args: dict[str, Any], audit) -> AdapterObservation:
    args = validate_service_query(args)
    if args["service"] != "bitmagnet":
        raise InvalidCall("Bitmagnet reads require the Bitmagnet service")
    if args["operation"] == "health":
        return _health(native, args, audit)
    if args["operation"] == "search":
        return _search(native, args, audit)
    raise InvalidCall("Only fixed Bitmagnet read operations are permitted")


class NativeBitmagnetAdapter:
    """Bridge the already-trained bitmagnet_search action to the native API."""

    def __init__(self, native: ServiceQueryAdapter):
        self.native = native

    def supports(self, name: str) -> bool:
        return name == "bitmagnet_search" and "bitmagnet" in self.native.configs

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        call = validate_call(call.name, call.arguments, allow_v3=False)
        if call.name != "bitmagnet_search":
            raise InvalidCall("Native Bitmagnet bridge accepts only bitmagnet_search")
        if not principal.subject or "releases:read" not in principal.scopes:
            return AdapterObservation("denied", "bitmagnet", call.name, 403, False, {"error": "permission_required"}, [])
        if "bitmagnet" not in self.native.configs:
            return AdapterObservation("error", "bitmagnet", call.name, 503, False, {"error": "service_not_configured"}, [])
        audit: list[dict[str, Any]] = []
        try:
            result = _search(self.native, {"operation": "search", "query": call.arguments["query"]}, audit)
            result.operation = call.name
            return result
        except _UpstreamError as exc:
            return AdapterObservation("error", "bitmagnet", call.name, exc.status, False, {"error": exc.code}, audit)
