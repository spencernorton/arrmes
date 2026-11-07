"""Bounded native read adapters. URLs and credentials are operator configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Any, Mapping
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal, resolve_title_candidates
from arrmes.protocol import Call, InvalidCall
from .contract import SERVICE_PERMISSIONS, SUPPORTED_OPERATIONS, validate_service_query


@dataclass(frozen=True)
class ServiceConfig:
    """An API root (including any reverse-proxy prefix), never model supplied."""
    base_url: str
    api_key: str = field(default="", repr=False)
    session_id: str = field(default="", repr=False)
    user_ids: Mapping[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        p = urllib.parse.urlsplit(self.base_url)
        if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                or p.query or p.fragment or any(c.isspace() for c in self.base_url)):
            raise ValueError("Service base_url must be an HTTP(S) API root without credentials or query")
        try:
            p.port
        except ValueError as exc:
            raise ValueError("Invalid service port") from exc
        if not isinstance(self.api_key, str) or any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise ValueError("Invalid API key encoding")
        if not isinstance(self.session_id, str) or (self.session_id and not re.fullmatch(r"(?:QBT_SID_\d+=)?[A-Za-z0-9._/+=-]+", self.session_id)):
            raise ValueError("Invalid session cookie encoding")
        if (not isinstance(self.user_ids, Mapping) or len(self.user_ids) > 256
                or any(not isinstance(subject, str) or not 1 <= len(subject) <= 200
                       or subject != subject.strip() or any(ord(c) < 32 or ord(c) == 127 for c in subject)
                       or type(user_id) is not int or not 1 <= user_id <= 2_147_483_647
                       for subject, user_id in self.user_ids.items())):
            raise ValueError("Invalid native service user mapping")
        object.__setattr__(self, "user_ids", MappingProxyType(dict(self.user_ids)))


def load_service_configs(path: str | Path) -> dict[str, ServiceConfig]:
    """Read operator service roots/credentials and optional trusted Seerr user IDs."""
    p = Path(path).expanduser()
    if p.stat().st_size > 1_048_576:
        raise ValueError("Service registry is too large")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate service registry key")
            value[key] = item
        return value
    try:
        raw = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid service registry JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"services"} or not isinstance(raw["services"], dict):
        raise ValueError("Service registry must contain only a services object")
    out: dict[str, ServiceConfig] = {}
    for name, value in raw["services"].items():
        if name not in SUPPORTED_OPERATIONS or not isinstance(value, dict) or set(value) - {"base_url", "api_key", "session_id", "user_ids"}:
            raise ValueError("Invalid service registry entry")
        if name != "seerr" and "user_ids" in value:
            raise ValueError("Native user mappings are supported only for Seerr")
        if not isinstance(value.get("base_url"), str):
            raise ValueError("Service registry entry requires base_url")
        out[name] = ServiceConfig(**value)
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Refuse all redirects, including same-origin API-to-login redirects.
        # Neither service credentials nor cookies can follow an upstream URL.
        return None


class _UpstreamError(ValueError):
    def __init__(self, code: str, status: int = 502) -> None:
        self.code, self.status = code, status


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(unicodedata.normalize("NFKC", stripped).casefold().split())


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _UpstreamError("invalid_pagination_count")
    return value


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise _UpstreamError("invalid_upstream_records")
    return value


def _id(value: Any, *, zero: bool = False) -> bool:
    return type(value) is int and value >= (0 if zero else 1)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _display(row: dict[str, Any]) -> bool:
    if any(_text(row.get(key)) for key in ("title", "sourceTitle", "artistName", "authorName", "bookTitle", "full_title", "grandparent_title")):
        return True
    return any(isinstance(row.get(key), dict) and any(_text(row[key].get(field)) for field in ("title", "artistName", "authorName"))
               for key in ("series", "movie", "artist", "album", "episode", "author", "book"))


_ARR_SERVICES = frozenset({"sonarr", "radarr", "lidarr", "readarr", "whisparr"})
_ARR_DISPLAY_FIELD = {
    "sonarr": "title",
    "radarr": "title",
    "lidarr": "artistName",
    "readarr": "authorName",
    "whisparr": "title",
}
_ARR_PROVIDER_FIELD = {
    "sonarr": "tvdbId",
    "radarr": "tmdbId",
    "lidarr": "foreignArtistId",
    "readarr": "foreignAuthorId",
    "whisparr": "tvdbId",
}


def _validated_rows(service: str, operation: str, rows: list[dict[str, Any]], *, media_type: str | None = None) -> list[dict[str, Any]]:
    """Check stable identity/display fields, leaving optional upstream metadata alone.

    Lookup resources can have internal id=0 before addition to a library. Queue
    entries can have null media IDs; their own nonnegative hash-derived ID and
    download title remain meaningful. Neither case is an empty library record.
    """
    for row in rows:
        valid = False
        if service in _ARR_SERVICES:
            title = _ARR_DISPLAY_FIELD[service]
            if operation == "search":
                internal = row.get("id")
                valid_internal = internal is None or _id(internal, zero=True)
                provider_key = _ARR_PROVIDER_FIELD[service]
                provider = row.get(provider_key)
                if service == "readarr" and provider is None and isinstance(row.get("author"), dict):
                    provider = row["author"].get("foreignAuthorId")
                if service == "readarr" and provider is None:
                    provider = row.get("foreignBookId")
                if service == "whisparr" and provider is None:
                    provider = row.get("tmdbId") or row.get("foreignId")
                is_str_provider = service in {"lidarr", "readarr"}
                valid_provider = _text(provider) if is_str_provider else _id(provider)
                provider_shape = provider is None or (isinstance(provider, str) if is_str_provider else _id(provider, zero=True))
                has_text = _text(row.get(title)) or (service == "readarr" and _text(row.get("title")))
                valid = valid_internal and provider_shape and (_id(internal) or valid_provider) and has_text
            elif operation == "queue":
                valid = _id(row.get("id"), zero=True) and _display(row)
            elif operation == "history":
                valid = _id(row.get("id")) and _display(row)
            elif operation in {"missing", "wanted"} and service in {"sonarr", "whisparr"}:
                valid = (
                    (_id(row.get("id")) and _id(row.get("seriesId"))
                     and _id(row.get("seasonNumber"), zero=True) and _id(row.get("episodeNumber"), zero=True)
                     and (row.get("title") is None or isinstance(row.get("title"), str)))
                    or (service == "whisparr" and _id(row.get("id")) and _text(row.get("title")))
                )
            elif operation in {"missing", "wanted"}:
                valid = _id(row.get("id")) and _text(row.get("title"))
                if service == "lidarr":
                    valid = valid and _id(row.get("artistId"))
                elif service == "readarr":
                    valid = valid and (_id(row.get("authorId")) or (isinstance(row.get("author"), dict) and _id(row["author"].get("id"))))
            elif operation in {"quality_profiles", "indexers"}:
                valid = _id(row.get("id")) and _text(row.get("name"))
            elif operation == "calendar":
                valid = _id(row.get("id")) and (_text(row.get("title")) or _display(row))
            else:
                valid = _id(row.get("id")) and (_text(row.get(title)) or (service == "readarr" and _text(row.get("title"))))
        elif service == "prowlarr":
            if operation in {"indexers", "applications"}:
                valid = _id(row.get("id")) and _text(row.get("name"))
            elif operation == "search":
                valid = _text(row.get("title")) and (_text(row.get("guid")) or _id(row.get("indexerId"), zero=True))
            elif operation == "history":
                valid = _id(row.get("id")) and (_display(row) or _text(row.get("eventType")) or isinstance(row.get("data"), dict))
        elif service == "bazarr":
            if operation == "series_library":
                valid = _id(row.get("sonarrSeriesId")) and _text(row.get("title"))
            elif operation == "movie_library" or media_type == "movie":
                valid = _id(row.get("radarrId")) and _text(row.get("title"))
            elif operation == "missing":
                valid = (_id(row.get("sonarrSeriesId")) and _id(row.get("sonarrEpisodeId"))
                         and _text(row.get("seriesTitle")))
            else:
                valid = (_id(row.get("sonarrSeriesId")) and _id(row.get("sonarrEpisodeId"))
                         and (_text(row.get("title")) or (_id(row.get("season"), zero=True) and _id(row.get("episode"), zero=True))))
            if operation in {"missing", "subtitles"}:
                fields = ("missing_subtitles", "subtitles") if operation == "subtitles" else ("missing_subtitles",)
                valid = valid and all(key in row and (row[key] is None or isinstance(row[key], (list, dict))) for key in fields)
        elif service == "tautulli":
            if operation == "history":
                valid = _id(row.get("row_id")) and _display(row)
            else:
                key = row.get("session_key")
                session = _id(key, zero=True) or (isinstance(key, str) and key.isdecimal()) or _text(row.get("session_id"))
                valid = session and _display(row)
        elif service == "seerr":
            kind = row.get("mediaType")
            titles = ("title", "originalTitle") if kind in {"movie", "collection"} else ("name", "originalName")
            valid = _id(row.get("id")) and kind in {"movie", "tv", "person", "collection"} and any(_text(row.get(key)) for key in titles)
        if not valid:
            raise _UpstreamError(f"invalid_{service}_{operation}_record")
    return rows


_SAFE_FIELDS = frozenset({
    "id", "title", "name", "artistName", "authorName", "artistId", "authorId", "seriesId", "movieId", "albumId", "bookId",
    "year", "monitored", "hasFile", "statistics", "seasonNumber", "episodeNumber",
    "status", "trackedDownloadStatus", "trackedDownloadState", "statusMessages", "errorMessage",
    "eventType", "date", "sourceTitle", "size", "sizeleft", "timeleft", "estimatedCompletionTime",
    "protocol", "downloadClient", "quality", "releaseDate", "inCinemas", "airDateUtc", "airDate", "physicalRelease", "digitalRelease",
    "hash", "state", "progress", "dlspeed", "upspeed", "eta", "ratio", "amount_left",
    "downloaded", "uploaded", "availability", "num_seeds", "num_leechs", "priority", "category",
    "sonarrSeriesId", "sonarrEpisodeId", "radarrId", "seriesTitle", "episodeTitle", "episode_number",
    "season", "episode", "subtitles", "missing_subtitles", "audio_language",
    "friendly_name", "user", "full_title", "grandparent_title", "media_type", "started", "stopped",
    "duration", "play_duration", "player", "platform", "transcode_decision", "stream_count",
    "mediaType", "originalTitle", "originalName", "firstAirDate", "mediaInfo",
    "language", "code2", "code3", "forced", "hi", "hearing_impaired", "source", "resolution",
    "modifier", "episodeFileCount", "episodeCount", "trackFileCount", "trackCount", "albumCount", "bookFileCount", "bookCount",
    "series", "movie", "artist", "album", "author", "book", "alternateTitles",
    "tvdbId", "tmdbId", "foreignArtistId", "foreignAuthorId", "foreignBookId",
    "row_id", "rating_key", "session_key", "session_id",
    "upgradeAllowed", "cutoff", "enable", "enableRss", "enableAutomaticSearch", "enableInteractiveSearch",
    "indexerId", "indexer", "guid", "age", "seeders", "leechers", "grabs", "categories",
    "appProfileId", "syncLevel", "implementation", "implementationName", "disabledTill", "mostRecentFailure",
})


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    """Do not emit credentials, local media paths, IP addresses or raw upstream objects."""
    def safe(value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return None
        if isinstance(value, dict):
            return {key: safe(val, depth + 1) for key, val in value.items() if key in _SAFE_FIELDS}
        if isinstance(value, list):
            return [safe(item, depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return value[:2048]
        if value is None or type(value) in {bool, int} or (type(value) is float and math.isfinite(value)):
            return value
        return None
    return safe(row)


def _matches_row_query(row: dict[str, Any], needle: str) -> bool:
    for field_name in ("title", "artistName", "authorName", "name", "sourceTitle"):
        val = row.get(field_name)
        if isinstance(val, str) and needle in _norm(val):
            return True
    alts = row.get("alternateTitles")
    if isinstance(alts, list):
        for alt in alts:
            if isinstance(alt, dict) and isinstance(alt.get("title"), str) and needle in _norm(alt["title"]):
                return True
    return False


class ServiceQueryAdapter:
    """Read-only adapter over explicit native API versions, with finite response/page budgets.

    No credentials or URL comes from a tool argument. Every dispatch revalidates
    the pair and current authenticated scopes. REST reads use GET; the separate
    Bitmagnet module permits only two immutable GraphQL query documents by POST.
    There is no generic endpoint forwarding, login or application command.
    """

    def __init__(self, configs: Mapping[str, ServiceConfig], *, max_pages: int = 5,
                 page_size: int = 50, max_records: int = 250,
                 timeout_seconds: float = 8.0, max_response_bytes: int = 8_388_608) -> None:
        if (type(max_pages) is not int or not 1 <= max_pages <= 20
                or type(page_size) is not int or not 1 <= page_size <= 100
                or type(max_records) is not int or not 1 <= max_records <= 2000
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 33_554_432):
            raise ValueError("Invalid native adapter resource bounds")
        if any(name not in SUPPORTED_OPERATIONS or not isinstance(conf, ServiceConfig) for name, conf in configs.items()):
            raise ValueError("Invalid native service configuration")
        self.configs = dict(configs)
        self.max_pages, self.page_size, self.max_records = max_pages, page_size, max_records
        self.timeout_seconds, self.max_response_bytes = timeout_seconds, max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _get(self, service: str, path: str, audit: list[dict[str, Any]], *, text: bool = False) -> Any:
        cfg = self.configs[service]
        headers = {"Accept": "application/json", "User-Agent": "arrmes-native-read/1"}
        if cfg.api_key:
            if service == "qbittorrent":
                headers["Authorization"] = "Bearer " + cfg.api_key
            else:
                headers["X-Api-Key"] = cfg.api_key
        if service == "qbittorrent":
            headers["Referer"] = cfg.base_url.rstrip("/") + "/"
            if cfg.session_id:
                headers["Cookie"] = cfg.session_id if cfg.session_id.startswith("QBT_SID_") else ("SID=" + cfg.session_id)
        req = urllib.request.Request(cfg.base_url.rstrip("/") + path, method="GET", headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as resp:
                audit.append({"method": "GET", "path": path, "status": int(resp.status)})
                deadline = time.monotonic() + self.timeout_seconds
                chunks: list[bytes] = []
                size = 0
                read_chunk = getattr(resp, "read1", resp.read)
                while True:
                    chunk = read_chunk(min(65536, self.max_response_bytes + 1 - size))
                    if time.monotonic() > deadline:
                        raise _UpstreamError("service_response_deadline_exceeded", 503)
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise _UpstreamError("response_limit_exceeded")
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)
                try:
                    decoded = raw.decode("utf-8")
                    def reject_nonfinite(_value):
                        raise ValueError("Non-finite upstream JSON number")
                    def unique_keys(pairs):
                        value = {}
                        for key, item in pairs:
                            if key in value:
                                raise ValueError("Duplicate upstream JSON key")
                            value[key] = item
                        return value
                    return decoded.strip() if text else json.loads(decoded, parse_constant=reject_nonfinite,
                                                                  object_pairs_hook=unique_keys)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise _UpstreamError("invalid_upstream_json") from exc
        except urllib.error.HTTPError as exc:
            audit.append({"method": "GET", "path": path, "status": int(exc.code)})
            error = ("service_authentication_failed" if exc.code in {401, 403} else
                     "service_api_version_or_route_unsupported" if exc.code == 404 else
                     "upstream_redirect_refused" if 300 <= exc.code < 400 else "upstream_http_error")
            status = int(exc.code)
            exc.close()
            raise _UpstreamError(error, status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            audit.append({"method": "GET", "path": path, "status": 503})
            raise _UpstreamError("service_unavailable", 503) from None

    def _paged(self, getter, *, records_key: str = "records", total_key: str = "totalRecords") -> tuple[list[dict[str, Any]], int, bool]:
        rows: list[dict[str, Any]] = []
        total = 0
        fingerprints: set[str] = set()
        seen_records: set[str] = set()
        first_total: int | None = None
        changed = False
        for page in range(self.max_pages):
            payload = getter(page)
            if not isinstance(payload, dict):
                raise _UpstreamError("invalid_upstream_page")
            incoming, reported = _rows(payload.get(records_key)), _count(payload.get(total_key))
            if reported < len(incoming):
                raise _UpstreamError("inconsistent_pagination_count")
            if first_total is None:
                first_total = reported
            changed = changed or reported != first_total
            total = max(total, reported)
            fingerprint = json.dumps(incoming, sort_keys=True)
            if not incoming or fingerprint in fingerprints:
                break
            fingerprints.add(fingerprint)
            unique_rows = []
            for row in incoming:
                key = json.dumps(row, sort_keys=True)
                if key in seen_records:
                    changed = True
                    continue
                seen_records.add(key)
                unique_rows.append(row)
            rows.extend(unique_rows[:self.max_records - len(rows)])
            if len(rows) >= total or len(rows) >= self.max_records:
                break
        return rows, total, changed or len(rows) < total

    def _result(self, service: str, args: dict[str, Any], rows: list[dict[str, Any]],
                total: int, truncated: bool, audit: list[dict[str, Any]], **extra: Any) -> AdapterObservation:
        query = args.get("query")
        monitored = args.get("monitored")
        op = args["operation"]
        filtered = bool(
            (query and op in {"library", "queue", "quality_profiles", "indexers", "calendar", "applications"})
            or (monitored is not None and op in {"library", "missing", "wanted"})
        )
        scanned_count = len(rows)
        if monitored is not None and op in {"library", "missing", "wanted"}:
            rows = [r for r in rows if r.get("monitored") is monitored]
        if query and op in {"library", "queue", "quality_profiles", "indexers", "calendar", "applications"}:
            needle = _norm(query)
            rows = [r for r in rows if _matches_row_query(r, needle)]
        data = {"source": "native_api", "operation": op, "items": [_public_row(r) for r in rows],
                "returned_count": len(rows), "total_records": total, "truncated": truncated,
                "complete_listing": not truncated, **extra}
        if filtered:
            data.update(scanned_count=scanned_count, upstream_total_records=total,
                        total_records=None if truncated else len(rows))
        if query:
            data["query"] = query
        if monitored is not None:
            data["monitored"] = monitored
        return AdapterObservation("partial" if truncated else "verified_ok", service, "service_query", 200,
                                  not truncated, data, audit)

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        audit: list[dict[str, Any]] = []
        if call.name != "service_query":
            raise InvalidCall("Native adapter accepts only service_query")
        args = validate_service_query(call.arguments)
        service = args["service"]
        if not principal.subject or SERVICE_PERMISSIONS[service] not in principal.scopes:
            return AdapterObservation("denied", service, call.name, 403, False, {"error": "permission_required"}, audit)
        if service not in self.configs:
            return AdapterObservation("error", service, call.name, 503, False, {"error": "service_not_configured"}, audit)
        try:
            if service in _ARR_SERVICES:
                return self._arr(service, args, audit)
            return getattr(self, "_" + service)(args, audit)
        except _UpstreamError as exc:
            return AdapterObservation("error", service, call.name, exc.status, False, {"error": exc.code}, audit)

    def _arr(self, service: str, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        version = "v1" if service in {"lidarr", "readarr"} else "v3"
        root = "/api/" + version
        op = args["operation"]
        entity = {
            "sonarr": "series",
            "radarr": "movie",
            "lidarr": "artist",
            "readarr": "author",
            "whisparr": "series",
        }[service]
        valid_app_names = {service} | ({"chaptarr"} if service == "readarr" else set())
        if op == "health":
            status = self._get(service, root + "/system/status", audit)
            issues = _rows(self._get(service, root + "/health", audit))
            if not isinstance(status, dict) or not _text(status.get("version")):
                raise _UpstreamError("invalid_service_status")
            if any(not _text(row.get("message")) or not _text(row.get("type")) for row in issues):
                raise _UpstreamError("invalid_service_health_issue")
            if "appName" in status and str(status["appName"]).strip().casefold() not in valid_app_names:
                raise _UpstreamError("service_identity_mismatch")
            return self._result(service, args, [], 0, False, audit, api_version=version,
                                server_version=status["version"], healthy=not issues,
                                issues=[{k: r[k] for k in ("source", "type", "message") if k in r} for r in issues])
        if op in {"queue", "history", "missing", "wanted"}:
            endpoint = "/wanted/missing" if op == "missing" else "/wanted/cutoff" if op == "wanted" else "/" + op
            inc_param = {
                "sonarr": {"includeSeries": "true"},
                "whisparr": {"includeSeries": "true"},
                "radarr": {"includeMovie": "true"},
                "lidarr": {"includeArtist": "true"},
                "readarr": {"includeAuthor": "true"},
            }[service]
            if args.get("monitored") is not None and op in {"missing", "wanted"}:
                inc_param = {**inc_param, "monitored": "true" if args["monitored"] else "false"}
            rows, total, truncated = self._paged(lambda page: self._get(service, root + endpoint + "?" + urllib.parse.urlencode({
                "page": page + 1, "pageSize": self.page_size, **inc_param,
            }, quote_via=urllib.parse.quote), audit))
        elif op in {"quality_profiles", "indexers", "calendar"}:
            endpoint_map = {
                "quality_profiles": "/qualityprofile",
                "indexers": "/indexer",
                "calendar": "/calendar",
            }
            all_rows = _rows(self._get(service, root + endpoint_map[op], audit))
            _validated_rows(service, op, all_rows)
            if args.get("query"):
                rows, total, truncated = all_rows, len(all_rows), len(all_rows) > self.max_records
            else:
                rows, total = all_rows[:self.max_records], len(all_rows)
                truncated = total > len(rows)
            return self._result(service, args, rows, total, truncated, audit, api_version=version)
        else:
            suffix = ("/lookup?" + urllib.parse.urlencode({"term": args["query"]}, quote_via=urllib.parse.quote)
                      if op == "search" else "")
            try:
                all_rows = _rows(self._get(service, root + "/" + entity + suffix, audit))
            except _UpstreamError as exc:
                if service == "whisparr" and exc.status == 404:
                    all_rows = _rows(self._get(service, root + "/movie" + suffix, audit))
                else:
                    raise
            _validated_rows(service, op, all_rows)
            if op == "library" and (args.get("query") or args.get("monitored") is not None):
                # Scan the full in-budget library before applying query/monitored filter in _result
                rows, total, truncated = all_rows, len(all_rows), len(all_rows) > self.max_records
            else:
                rows, total = all_rows[:self.max_records], len(all_rows)
                truncated = total > len(rows)
            return self._result(service, args, rows, total, truncated, audit, api_version=version)
        _validated_rows(service, op, rows)
        return self._result(service, args, rows, total, truncated, audit, api_version=version)

    def _prowlarr(self, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        root = "/api/v1"
        op = args["operation"]
        if op == "health":
            status = self._get("prowlarr", root + "/system/status", audit)
            issues = _rows(self._get("prowlarr", root + "/health", audit))
            if not isinstance(status, dict) or not _text(status.get("version")):
                raise _UpstreamError("invalid_service_status")
            if any(not _text(row.get("message")) or not _text(row.get("type")) for row in issues):
                raise _UpstreamError("invalid_service_health_issue")
            if "appName" in status and str(status["appName"]).strip().casefold() != "prowlarr":
                raise _UpstreamError("service_identity_mismatch")
            return self._result("prowlarr", args, [], 0, False, audit, api_version="v1",
                                server_version=status["version"], healthy=not issues,
                                issues=[{k: r[k] for k in ("source", "type", "message") if k in r} for r in issues])
        if op == "indexers":
            all_rows = _rows(self._get("prowlarr", root + "/indexer", audit))
            _validated_rows("prowlarr", "indexers", all_rows)
            statuses = _rows(self._get("prowlarr", root + "/indexerstatus", audit))
            status_by_id: dict[int, dict[str, Any]] = {}
            for st in statuses:
                idx_id = st.get("indexerId")
                if _id(idx_id):
                    status_by_id[idx_id] = {
                        k: st[k] for k in ("disabledTill", "mostRecentFailure") if k in st
                    }
            merged = [
                {**r, **(status_by_id.get(r["id"], {}))}
                for r in all_rows
            ]
            rows, total = merged[:self.max_records], len(merged)
            return self._result("prowlarr", args, rows, total, total > len(rows), audit, api_version="v1",
                                failing_indexer_count=len(status_by_id))
        if op == "applications":
            all_rows = _rows(self._get("prowlarr", root + "/applications", audit))
            _validated_rows("prowlarr", "applications", all_rows)
            rows, total = all_rows[:self.max_records], len(all_rows)
            return self._result("prowlarr", args, rows, total, total > len(rows), audit, api_version="v1")
        if op == "history":
            rows, total, truncated = self._paged(lambda page: self._get("prowlarr", root + "/history?" + urllib.parse.urlencode({
                "page": page + 1, "pageSize": self.page_size,
            }, quote_via=urllib.parse.quote), audit))
            _validated_rows("prowlarr", "history", rows)
            return self._result("prowlarr", args, rows, total, truncated, audit, api_version="v1")
        # op == "search"
        all_rows = _rows(self._get("prowlarr", root + "/search?" + urllib.parse.urlencode({
            "query": args["query"], "type": "search",
        }, quote_via=urllib.parse.quote), audit))
        _validated_rows("prowlarr", "search", all_rows)
        rows, total = all_rows[:self.max_records], len(all_rows)
        return self._result("prowlarr", args, rows, total, total > len(rows), audit, api_version="v1")

    def _bazarr(self, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        if args["operation"] == "health":
            payload = self._get("bazarr", "/api/system/health", audit)
            if not isinstance(payload, dict):
                raise _UpstreamError("invalid_service_status")
            issues = _rows(payload.get("data"))
            if any(not _text(row.get("object")) or not _text(row.get("issue")) for row in issues):
                raise _UpstreamError("invalid_service_health_issue")
            return self._result("bazarr", args, [], 0, False, audit, api_version="unversioned /api",
                                healthy=not issues, issues=[{k: r[k] for k in ("object", "issue") if k in r} for r in issues])
        if args["operation"] == "history":
            rows, total, truncated = self._paged(lambda page: self._get("bazarr", "/api/history/series?" + urllib.parse.urlencode({
                "start": page * self.page_size, "length": self.page_size}, quote_via=urllib.parse.quote), audit),
                records_key="data", total_key="total")
            _validated_rows("bazarr", "history", rows)
            return self._result("bazarr", args, rows, total, truncated, audit, api_version="unversioned /api")
        movie = args["media_type"] == "movie"
        kind = "movies" if movie else "episodes"
        if args["operation"] == "missing":
            rows, total, truncated = self._paged(lambda page: self._get("bazarr", "/api/" + kind + "/wanted?" + urllib.parse.urlencode({
                "start": page * self.page_size, "length": self.page_size}, quote_via=urllib.parse.quote), audit),
                records_key="data", total_key="total")
        else:
            library = "movies" if movie else "series"
            entities, _, incomplete = self._paged(lambda page: self._get("bazarr", "/api/" + library + "?" + urllib.parse.urlencode({
                "start": page * self.page_size, "length": self.page_size}, quote_via=urllib.parse.quote), audit),
                records_key="data", total_key="total")
            _validated_rows("bazarr", "movie_library" if movie else "series_library", entities)
            exact = resolve_title_candidates(args["query"], entities)
            if incomplete:
                return self._result("bazarr", args, [], 0, True, audit, reason="entity_lookup_incomplete")
            if len(exact) != 1:
                return AdapterObservation("clarify_required", "bazarr", "service_query", 409, False,
                                          {"slot": "reference", "reason": "subtitle_entity_not_uniquely_resolved"}, audit)
            if movie:
                rows = exact
            else:
                series_id = exact[0].get("sonarrSeriesId")
                if type(series_id) is not int or series_id <= 0:
                    raise _UpstreamError("invalid_resolved_entity_id")
                payload = self._get("bazarr", "/api/episodes?" + urllib.parse.urlencode(
                    {"seriesid[]": series_id}, quote_via=urllib.parse.quote), audit)
                if not isinstance(payload, dict):
                    raise _UpstreamError("invalid_upstream_page")
                rows = _rows(payload.get("data"))
            total, truncated = len(rows), len(rows) > self.max_records
            rows = rows[:self.max_records]
        _validated_rows("bazarr", args["operation"], rows, media_type=args["media_type"])
        return self._result("bazarr", args, rows, total, truncated, audit, api_version="unversioned /api", media_type=args["media_type"])

    def _qbittorrent(self, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        op = args["operation"]
        if op == "health":
            version = self._get("qbittorrent", "/api/v2/app/version", audit, text=True)
            api = self._get("qbittorrent", "/api/v2/app/webapiVersion", audit, text=True)
            if not re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+].*)?", version) or not re.fullmatch(r"2\.\d+(?:\.\d+)?", api):
                raise _UpstreamError("unsupported_qbittorrent_api_version")
            return self._result("qbittorrent", args, [], 0, False, audit, api_version=api, server_version=version)
        if op == "activity":
            payload = self._get("qbittorrent", "/api/v2/transfer/info", audit)
            if not isinstance(payload, dict) or not all(type(payload.get(k)) in {int, float} and math.isfinite(payload[k]) and payload[k] >= 0 for k in ("dl_info_speed", "up_info_speed")):
                raise _UpstreamError("invalid_transfer_stats")
            return self._result("qbittorrent", args, [], 0, False, audit, api_version="v2", transfer={
                k: payload[k] for k in ("dl_info_speed", "up_info_speed", "dl_info_data", "up_info_data", "connection_status") if k in payload})
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        exhausted = False
        for page in range(self.max_pages):
            incoming = _rows(self._get("qbittorrent", "/api/v2/torrents/info?" + urllib.parse.urlencode({
                "filter": args.get("torrent_filter", "all"), "limit": self.page_size,
                "offset": page * self.page_size, "sort": "hash"}, quote_via=urllib.parse.quote), audit))
            hashes = [r.get("hash") for r in incoming]
            if any(not isinstance(h, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", h) for h in hashes):
                raise _UpstreamError("invalid_torrent_identity")
            if any(not _text(row.get("name")) or ("state" in row and not _text(row["state"])) for row in incoming):
                raise _UpstreamError("invalid_qbittorrent_queue_record")
            if any(h in seen for h in hashes):
                break
            seen.update(hashes)
            remaining = self.max_records - len(rows)
            rows.extend(incoming[:remaining])
            if len(incoming) < self.page_size and len(incoming) <= remaining:
                exhausted = True
                break
            if len(rows) >= self.max_records:
                break
        # API has no total count; never invent one at the finite page budget.
        obs = self._result("qbittorrent", args, rows, len(rows), not exhausted, audit, api_version="v2")
        obs.data["total_records"] = obs.data["returned_count"] if exhausted else None
        return obs

    def _tautulli_data(self, path: str, audit: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._get("tautulli", path, audit)
        response = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response, dict) or response.get("result") != "success" or not isinstance(response.get("data"), dict):
            raise _UpstreamError("tautulli_application_error")
        return response["data"]

    def _tautulli(self, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        op = args["operation"]
        root = "/api/v2?"
        if op == "health":
            payload = self._tautulli_data(root + "cmd=get_tautulli_info", audit)
            version = payload.get("tautulli_version")
            if not _text(version):
                raise _UpstreamError("invalid_service_status")
            return self._result("tautulli", args, [], 0, False, audit, api_version="v2", server_version=version)
        if op == "activity":
            payload = self._tautulli_data(root + "cmd=get_activity", audit)
            rows = [] if payload == {} else _rows(payload.get("sessions"))
            _validated_rows("tautulli", "activity", rows)
            return self._result("tautulli", args, rows[:self.max_records], len(rows), len(rows) > self.max_records, audit, api_version="v2")
        rows, total, truncated = self._paged(lambda page: self._tautulli_data(root + urllib.parse.urlencode({
            "cmd": "get_history", "start": page * self.page_size, "length": self.page_size,
            "order_column": "date", "order_dir": "desc"}, quote_via=urllib.parse.quote), audit),
            records_key="data", total_key="recordsFiltered")
        _validated_rows("tautulli", "history", rows)
        return self._result("tautulli", args, rows, total, truncated, audit, api_version="v2")

    def _seerr(self, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        if args["operation"] == "health":
            payload = self._get("seerr", "/api/v1/status", audit)
            if not isinstance(payload, dict) or not _text(payload.get("version")):
                raise _UpstreamError("invalid_service_status")
            return self._result("seerr", args, [], 0, False, audit, api_version="v1", server_version=payload["version"])
        rows, total, truncated = self._paged(lambda page: self._get("seerr", "/api/v1/search?" + urllib.parse.urlencode({
            "query": args["query"], "page": page + 1}, quote_via=urllib.parse.quote), audit), records_key="results", total_key="totalResults")
        _validated_rows("seerr", "search", rows)
        return self._result("seerr", args, rows, total, truncated, audit, api_version="v1")

    def _bitmagnet(self, args: dict[str, Any], audit: list[dict[str, Any]]) -> AdapterObservation:
        from .bitmagnet import execute_bitmagnet
        return execute_bitmagnet(self, args, audit)
