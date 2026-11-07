"""Caller-owned native Seerr requests with single-use movie, TV season, and cancellation confirmation."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import re
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, normalized, validate_call
from .native import ServiceConfig, ServiceQueryAdapter, _UpstreamError, _count, _id, _norm, _rows, _text

CONFIRMATION_PREFIX = "native-seerr:"
REQUEST_ACTIONS = frozenset({"request_status", "request_media"})
_REQUEST_STATES = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
_MEDIA_STATES = {1: "unknown", 2: "pending", 3: "processing", 4: "partially_available",
                 5: "available", 6: "blocklisted", 7: "deleted"}
_SEERR_VERSION = re.compile(r"v?3\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?")
_DELETE_REQ_PATH = re.compile(r"^/api/v1/request/\d+$")


class _Clarify(ValueError):
    pass


def _obs(name, status, data, audit, *, code=200):
    return AdapterObservation(status, "seerr", name, code, status == "verified_ok",
                              {"source": "native_seerr", **data}, audit)


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate upstream JSON key")
        data[key] = value
    return data


def _year(row, kind):
    stamp = row.get("releaseDate" if kind == "movie" else "firstAirDate")
    if stamp is None or stamp == "":
        return None
    try:
        return date.fromisoformat(stamp).year
    except (ValueError, TypeError):
        raise _UpstreamError("invalid_media_release_date") from None


def _title(value):
    return _text(value) and len(value) <= 2048 and not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _valid_post_body(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if set(body) == {"mediaType", "mediaId"} and body["mediaType"] == "movie" and _id(body["mediaId"]):
        return True
    if (
        set(body) == {"mediaType", "mediaId", "seasons"}
        and body["mediaType"] == "tv"
        and _id(body["mediaId"])
        and isinstance(body["seasons"], list)
        and len(body["seasons"]) > 0
        and all(_id(s) for s in body["seasons"])
    ):
        return True
    return False


class NativeRequestAdapter:
    """Existing v2 names; trusted subjects map to verified native user IDs.

    Movie creation and TV status are enabled by default. TV/season creation and
    caller-owned pending request cancellation are supported via execute_tv_request,
    execute_cancel_request, or allow_tv_creation=True.
    """

    def __init__(
        self,
        native: ServiceQueryAdapter,
        *,
        write_mode: str = "confirm",
        confirmation_store=None,
        allow_tv_creation: bool = False,
    ) -> None:
        if write_mode not in {"confirm", "shadow", "disabled"}:
            raise ValueError("Native request mode must be confirm, shadow, or disabled")
        from arrmes.live_adapter import WriteConfirmationStore
        self.native, self.write_mode = native, write_mode
        self.allow_tv_creation = bool(allow_tv_creation)
        self.confirmation_store = confirmation_store or WriteConfirmationStore()
        # Two confirmations cannot race their duplicate preflights in this
        # adapter. Reads and confirmation previews remain independent.
        self._write_lock = threading.RLock()

    def supports(self, name: str) -> bool:
        # Configuration without a mapping must fail closed here, never fall
        # through to a gateway that might attribute requests to its owner.
        return name in REQUEST_ACTIONS and "seerr" in self.native.configs

    def _context(self, principal: Principal) -> tuple[ServiceConfig, int]:
        config = self.native.configs.get("seerr")
        if config is None:
            raise _UpstreamError("service_not_configured", 503)
        user_id = config.user_ids.get(principal.subject)
        if not _id(user_id):
            raise _UpstreamError("native_request_user_mapping_required", 403)
        if not config.api_key or config.session_id:
            raise _UpstreamError("native_request_api_key_required_without_session", 503)
        return config, user_id

    def _http(self, method: str, path: str, context: tuple[ServiceConfig, int],
              audit: list[dict[str, Any]], *, body: dict[str, Any] | None = None) -> Any:
        config, user_id = context
        if method not in {"GET", "POST", "DELETE"}:
            raise InvalidCall("Only native GET, POST, and DELETE request operations are supported")
        if method == "POST" and (path != "/api/v1/request" or not _valid_post_body(body)):
            raise InvalidCall("Only bounded movie or season-scoped TV request POST payloads are supported")
        if method == "DELETE" and (not _DELETE_REQ_PATH.fullmatch(path) or body is not None):
            raise InvalidCall("Only single caller-owned request DELETE paths are supported")
        headers = {"Accept": "application/json", "X-Api-Key": config.api_key,
                   "X-API-User": str(user_id), "User-Agent": "arrmes-native-request/1"}
        raw_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(config.base_url.rstrip("/") + path, data=raw_body,
                                         headers=headers, method=method)
        # Only the fixed API path is audited; queries can contain caller IDs
        # and user-entered search terms. Credentials never enter observations.
        safe_path = urllib.parse.urlsplit(path).path
        try:
            with self.native._opener.open(request, timeout=self.native.timeout_seconds) as response:
                code = int(response.status)
                audit.append({"method": method, "path": safe_path, "status": code, "attempt": 1})
                chunks, length = [], 0
                deadline = time.monotonic() + self.native.timeout_seconds
                while True:
                    if time.monotonic() >= deadline:
                        raise _UpstreamError("upstream_read_deadline_exceeded", 504)
                    chunk = response.read1(min(65_536, self.native.max_response_bytes + 1 - length))
                    if not chunk:
                        break
                    length += len(chunk)
                    if length > self.native.max_response_bytes:
                        raise _UpstreamError("response_limit_exceeded")
                    chunks.append(chunk)
                raw_bytes = b"".join(chunks)
                if method == "DELETE" and code in {200, 204} and not raw_bytes.strip():
                    return {}
                try:
                    data = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_unique,
                                      parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                except (UnicodeError, ValueError, RecursionError):
                    raise _UpstreamError("invalid_upstream_json") from None
                if method == "POST" and code != 201:
                    raise _UpstreamError("unexpected_upstream_status", 502)
                if method == "GET" and code != 200:
                    raise _UpstreamError("unexpected_upstream_status", 502)
                if method == "DELETE" and code not in {200, 204}:
                    raise _UpstreamError("unexpected_upstream_status", 502)
                return data
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            exc.close()
            audit.append({"method": method, "path": safe_path, "status": code, "attempt": 1})
            error = ("upstream_request_rejected" if method in {"POST", "DELETE"} and code in {400, 401, 403, 404, 405, 409, 422} else
                     "upstream_authentication_failed" if code in {401, 403} else
                     "upstream_redirect_refused" if 300 <= code < 400 else "upstream_http_error")
            raise _UpstreamError(error, code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            audit.append({"method": method, "path": safe_path, "status": 503, "attempt": 1})
            raise _UpstreamError("service_unavailable", 503) from None

    def _identity(self, context, audit):
        me = self._http("GET", "/api/v1/auth/me", context, audit)
        if not isinstance(me, dict) or not _id(me.get("id")) or me["id"] != context[1]:
            raise _UpstreamError("native_request_caller_identity_mismatch", 403)
        created = me.get("createdAt")
        if not _text(created) or len(created) > 128 or any(ord(char) < 32 for char in created):
            raise _UpstreamError("native_request_account_identity_unavailable", 403)
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            raise _UpstreamError("native_request_account_identity_unavailable", 403) from None
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise _UpstreamError("native_request_account_identity_unavailable", 403)
        # Numeric IDs may be reused after account deletion. Bind the stable
        # creation instant, plus a digest of email when supplied, without
        # exposing account fields or copying the raw /auth/me response.
        email = me.get("email")
        if email is not None and (not isinstance(email, str) or len(email) > 512 or
                                  any(ord(char) < 32 or ord(char) == 127 for char in email)):
            raise _UpstreamError("native_request_account_identity_unavailable", 403)
        identity = {"id": me["id"], "created_at": created_at.astimezone(timezone.utc).isoformat(),
                    "email": email.strip().casefold() if email is not None else None}
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()

    def _version(self, context, audit):
        status = self._http("GET", "/api/v1/status", context, audit)
        version = status.get("version") if isinstance(status, dict) else None
        if not _text(version) or len(version) > 128 or any(ord(c) < 32 for c in version):
            raise _UpstreamError("invalid_seerr_version")
        if not _SEERR_VERSION.fullmatch(version):
            raise _UpstreamError("unsupported_seerr_version", 501)
        return version

    @staticmethod
    def _extract_tv_seasons(detail: dict[str, Any]) -> list[int]:
        raw_seasons = detail.get("seasons")
        if isinstance(raw_seasons, list):
            nums: list[int] = []
            for s in raw_seasons:
                if not isinstance(s, dict):
                    raise _UpstreamError("invalid_seerr_season_record")
                sn = s.get("seasonNumber")
                if type(sn) is not int or sn < 0:
                    raise _UpstreamError("invalid_seerr_season_record")
                if sn > 0:
                    nums.append(sn)
            return sorted(set(nums))
        num_seasons = detail.get("numberOfSeasons")
        if type(num_seasons) is int and num_seasons > 0:
            return list(range(1, num_seasons + 1))
        return []

    def _resolve(self, args, context, audit):
        matches, seen, first_total, first_pages, complete = [], set(), None, None, False
        for page in range(1, self.native.max_pages + 1):
            payload = self._http("GET", "/api/v1/search?" + urllib.parse.urlencode({"query": args["title"], "page": page}, quote_via=urllib.parse.quote), context, audit)
            if not isinstance(payload, dict) or payload.get("page") != page or type(payload.get("page")) is not int:
                raise _UpstreamError("invalid_seerr_search_page")
            rows = _rows(payload.get("results"))
            total, pages = _count(payload.get("totalResults")), _count(payload.get("totalPages"))
            if first_total is None:
                first_total, first_pages = total, pages
            if total != first_total or pages != first_pages or total < len(rows):
                raise _Clarify("metadata_lookup_changed_or_incomplete")
            for row in rows:
                kind = row.get("mediaType")
                key = "title" if kind in ("movie", "collection") else "name"
                if not isinstance(kind, str) or kind not in {"movie", "tv", "person", "collection"} or not _id(row.get("id")) or not _title(row.get(key)):
                    raise _UpstreamError("invalid_seerr_search_record")
                identity = (kind, row["id"])
                if identity in seen or len(seen) >= self.native.max_records:
                    raise _Clarify("metadata_lookup_incomplete")
                seen.add(identity)
                if kind not in {"movie", "tv"}:
                    continue
                year = _year(row, kind)
                if (_norm(row[key]) == _norm(args["title"]) and
                        ("media_type" not in args or args["media_type"] == kind) and
                        ("year" not in args or args["year"] == year)):
                    matches.append({"tmdb_id": row["id"], "media_type": kind, "title": row[key], "year": year})
            if len(seen) == total and page >= pages:
                complete = True
                break
            if not rows or page >= pages:
                break
        if not complete:
            raise _Clarify("metadata_lookup_incomplete")
        if len(matches) != 1:
            raise _Clarify("media_not_uniquely_resolved")
        match = matches[0]
        detail = self._http("GET", f"/api/v1/{match['media_type']}/{match['tmdb_id']}", context, audit)
        title_key = "title" if match["media_type"] == "movie" else "name"
        if (not isinstance(detail, dict) or type(detail.get("id")) is not int or detail["id"] != match["tmdb_id"]
                or not _title(detail.get(title_key)) or _norm(detail[title_key]) != _norm(match["title"])
                or _year(detail, match["media_type"]) != match["year"]):
            raise _UpstreamError("metadata_detail_identity_mismatch")
        info = detail.get("mediaInfo")
        if info is not None and (not isinstance(info, dict) or
                (info.get("mediaType") is not None and info["mediaType"] != match["media_type"]) or
                (info.get("tmdbId") is not None and (type(info["tmdbId"]) is not int or info["tmdbId"] != match["tmdb_id"]))):
            raise _UpstreamError("metadata_detail_identity_mismatch")
        availability = info.get("status") if isinstance(info, dict) else None
        if availability is not None and (type(availability) is not int or availability not in _MEDIA_STATES):
            raise _UpstreamError("invalid_seerr_availability")
        available_seasons = self._extract_tv_seasons(detail) if match["media_type"] == "tv" else []
        return {**match, "title": detail[title_key], "_available_seasons": available_seasons}, availability

    @staticmethod
    def _owned_row(row, user_id):
        owner, media = row.get("requestedBy"), row.get("media")
        if not isinstance(owner, dict) or type(owner.get("id")) is not int or owner["id"] != user_id:
            raise _UpstreamError("foreign_request_in_response", 403)
        if (not _id(row.get("id")) or type(row.get("status")) is not int or row["status"] not in _REQUEST_STATES
                or not isinstance(row.get("type"), str) or row["type"] not in {"movie", "tv"} or type(row.get("is4k")) is not bool
                or not isinstance(media, dict) or not _id(media.get("tmdbId"))
                or media.get("mediaType") != row["type"] or type(media.get("status")) is not int
                or media["status"] not in _MEDIA_STATES):
            raise _UpstreamError("invalid_native_request_record")
        parsed = {"request_id": row["id"], "tmdb_id": media["tmdbId"], "media_type": row["type"],
                  "request_status": _REQUEST_STATES[row["status"]], "availability": _MEDIA_STATES[media["status"]],
                  "is_4k": row["is4k"]}
        if "seasons" in row and isinstance(row["seasons"], list):
            s_nums: list[int] = []
            for s in row["seasons"]:
                if isinstance(s, dict) and _id(s.get("seasonNumber")):
                    s_nums.append(s["seasonNumber"])
                elif _id(s):
                    s_nums.append(s)
                else:
                    raise _UpstreamError("invalid_native_request_record")
            parsed["seasons"] = sorted(set(s_nums))
        return parsed

    def _own_requests(self, context, audit):
        rows, seen, first_total = [], set(), None
        for page in range(self.native.max_pages):
            payload = self._http("GET", "/api/v1/request?" + urllib.parse.urlencode({"requestedBy": context[1],
                "take": self.native.page_size, "skip": page * self.native.page_size, "filter": "all", "sort": "added"}, quote_via=urllib.parse.quote), context, audit)
            info = payload.get("pageInfo") if isinstance(payload, dict) else None
            if not isinstance(info, dict):
                raise _UpstreamError("invalid_own_request_page")
            incoming = _rows(payload.get("results"))
            # Validate ownership before considering counts or returning anything.
            parsed = [self._owned_row(row, context[1]) for row in incoming]
            total = _count(info.get("results"))
            if (type(info.get("page")) is not int or info["page"] != page + 1
                    or type(info.get("pageSize")) is not int or info["pageSize"] != self.native.page_size):
                raise _Clarify("own_request_inventory_incomplete")
            if first_total is None:
                first_total = total
            if total != first_total or len(incoming) > self.native.page_size or total < len(incoming):
                raise _Clarify("own_request_inventory_changed_or_incomplete")
            for row in parsed:
                if row["request_id"] in seen or len(rows) >= self.native.max_records:
                    raise _Clarify("own_request_inventory_incomplete")
                seen.add(row["request_id"])
                rows.append(row)
            if len(rows) == total:
                return rows
            if not incoming:
                break
        raise _Clarify("own_request_inventory_incomplete")

    def _preflight(self, call, context, audit, *, expected_identity=None):
        user_identity = self._identity(context, audit)
        if expected_identity is not None and user_identity != expected_identity:
            raise _UpstreamError("confirmation_account_identity_changed", 409)
        version = self._version(context, audit)
        media_raw, availability = self._resolve(call.arguments, context, audit)
        available_seasons = media_raw.pop("_available_seasons", [])
        media = media_raw
        own = [row for row in self._own_requests(context, audit)
               if row["tmdb_id"] == media["tmdb_id"] and row["media_type"] == media["media_type"]]
        entity = {**media, "user_id": context[1], "user_identity_fingerprint": user_identity,
                  "service": "seerr", "server_version": version, "seasons": [],
                  "api_root_digest": hashlib.sha256(context[0].base_url.encode()).hexdigest()}
        return entity, availability, own, available_seasons

    @staticmethod
    def _existing(own, target_seasons: list[int] | None = None):
        standard = [row for row in own if not row["is_4k"]]
        if target_seasons:
            target_set = set(target_seasons)
            matching = []
            for row in standard:
                row_seasons = set(row.get("seasons") or [])
                if not row_seasons or target_set.issubset(row_seasons):
                    matching.append(row)
                elif row_seasons & target_set:
                    raise _Clarify("partial_season_request_overlap_requires_review")
            standard = matching
        if len(standard) > 1:
            raise _Clarify("multiple_own_requests_require_review")
        if standard and standard[0]["request_status"] in {"pending", "approved"}:
            return standard[0]
        if standard and standard[0]["request_status"] == "completed" and standard[0]["availability"] == "available":
            return standard[0]
        if standard:
            raise _Clarify("existing_request_requires_explicit_retry_or_review")
        return None

    @staticmethod
    def _permission(call, principal):
        scope = "requests:read" if call.name == "request_status" else "requests:create"
        return bool(principal.subject and scope in principal.scopes)

    def execute_tv_request(
        self,
        title: str,
        principal: Principal,
        *,
        seasons: list[int] | str = "all",
        year: int | None = None,
    ) -> AdapterObservation:
        args: dict[str, Any] = {"title": title, "media_type": "tv"}
        if year is not None:
            args["year"] = year
        call = validate_call("request_media", args, allow_v3=False)
        audit: list[dict[str, Any]] = []
        if not self._permission(call, principal):
            return _obs(call.name, "denied", {"error": "permission_required"}, audit, code=403)
        if self.write_mode == "disabled":
            return _obs(call.name, "denied", {"error": "writes_disabled"}, audit, code=403)
        if seasons != "all" and (
            not isinstance(seasons, list)
            or not seasons
            or not all(_id(s) for s in seasons)
        ):
            raise InvalidCall("seasons must be 'all' or a non-empty list of positive season numbers")
        try:
            context = self._context(principal)
            entity, _availability, own, available_seasons = self._preflight(call, context, audit)
            if not available_seasons:
                raise _Clarify("tv_seasons_unavailable_on_detail")
            if seasons == "all":
                target_seasons = list(available_seasons)
            else:
                target_seasons = sorted(set(seasons))
                if not set(target_seasons).issubset(set(available_seasons)):
                    raise _Clarify("requested_season_unavailable")
            entity["seasons"] = target_seasons
            public_media = {key: entity[key] for key in ("tmdb_id", "title", "year", "media_type", "seasons")}
            existing = self._existing(own, target_seasons=target_seasons)
            if existing:
                return _obs(call.name, "verified_ok", {**public_media, "request": existing, "changed": False,
                    "already_requested": True, "completion_scope": "request_submission_not_download"}, audit)
            season_label = ", ".join(str(s) for s in target_seasons)
            summary = (f"Submit one standard-quality TV request for '{entity['title']}' "
                       f"({entity['year'] or 'year unknown'}, seasons [{season_label}]) as Seerr user {context[1]}; "
                       f"Seerr approval/download policy applies")
            if self.write_mode == "shadow":
                return _obs(call.name, "shadow_logged", {"summary": summary, "resolved_entity": entity}, audit)
            token = self.confirmation_store.mint(principal, call, summary=summary, resolved_entity=entity)
            return _obs(call.name, "confirmation_required", {"confirm_id": CONFIRMATION_PREFIX + token.confirm_id,
                "summary": summary, "resolved_entity": entity, "expires_at": token.expires_at}, audit, code=202)
        except _Clarify as exc:
            return _obs(call.name, "clarify_required", {"slot": "reference", "reason": str(exc)}, audit, code=409)
        except _UpstreamError as exc:
            return _obs(call.name, "denied" if exc.status == 403 else "error", {"error": exc.code}, audit, code=exc.status)

    def execute_cancel_request(
        self,
        title: str,
        principal: Principal,
        *,
        media_type: str = "movie",
        request_id: int | None = None,
        year: int | None = None,
    ) -> AdapterObservation:
        args: dict[str, Any] = {"title": title, "media_type": media_type}
        if year is not None:
            args["year"] = year
        call = validate_call("request_media", args, allow_v3=False)
        audit: list[dict[str, Any]] = []
        if not self._permission(call, principal):
            return _obs("request_media", "denied", {"error": "permission_required"}, audit, code=403)
        if self.write_mode == "disabled":
            return _obs("request_media", "denied", {"error": "writes_disabled"}, audit, code=403)
        if request_id is not None and not _id(request_id):
            raise InvalidCall("request_id must be a positive integer when provided")
        try:
            context = self._context(principal)
            entity, _availability, own, _avail_seasons = self._preflight(call, context, audit)
            candidates = [r for r in own if request_id is None or r["request_id"] == request_id]
            if not candidates:
                raise _Clarify("own_request_not_found")
            if len(candidates) > 1:
                raise _Clarify("multiple_own_requests_require_review")
            target = candidates[0]
            if target["request_status"] != "pending":
                raise _Clarify("request_not_cancellable_in_current_state")
            entity["operation"] = "cancel_request"
            entity["cancel_request_id"] = target["request_id"]
            summary = (f"Cancel pending {entity['media_type']} request #{target['request_id']} for '{entity['title']}' "
                       f"({entity['year'] or 'year unknown'}) as Seerr user {context[1]}")
            if self.write_mode == "shadow":
                return _obs("request_media", "shadow_logged", {"summary": summary, "resolved_entity": entity}, audit)
            token = self.confirmation_store.mint(principal, call, summary=summary, resolved_entity=entity)
            return _obs("request_media", "confirmation_required", {"confirm_id": CONFIRMATION_PREFIX + token.confirm_id,
                "summary": summary, "resolved_entity": entity, "expires_at": token.expires_at}, audit, code=202)
        except _Clarify as exc:
            return _obs("request_media", "clarify_required", {"slot": "reference", "reason": str(exc)}, audit, code=409)
        except _UpstreamError as exc:
            return _obs("request_media", "denied" if exc.status == 403 else "error", {"error": exc.code}, audit, code=exc.status)

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        call = validate_call(call.name, call.arguments, allow_v3=False)
        if call.name not in REQUEST_ACTIONS:
            raise InvalidCall("Native requests accept only request_status and request_media")
        audit = []
        if not self._permission(call, principal):
            return _obs(call.name, "denied", {"error": "permission_required"}, audit, code=403)
        if call.name == "request_media" and self.write_mode == "disabled":
            return _obs(call.name, "denied", {"error": "writes_disabled"}, audit, code=403)
        if call.name == "request_media" and call.arguments["media_type"] != "movie":
            if self.allow_tv_creation and call.arguments["media_type"] == "tv":
                return self.execute_tv_request(
                    call.arguments["title"],
                    principal,
                    seasons="all",
                    year=call.arguments.get("year"),
                )
            return _obs(call.name, "unsupported", {"error": "native_tv_request_creation_not_qualified"}, audit, code=501)
        try:
            context = self._context(principal)
            entity, availability, own, _avail_seasons = self._preflight(call, context, audit)
            public_media = {key: entity[key] for key in ("tmdb_id", "title", "year", "media_type")}
            if call.name == "request_status":
                return _obs(call.name, "verified_ok", {**public_media, "requests": own, "own_request_count": len(own),
                    "availability": _MEDIA_STATES.get(availability), "status_scope": "caller_owned_requests_only"}, audit)
            existing = self._existing(own)
            if existing:
                return _obs(call.name, "verified_ok", {**public_media, "request": existing, "changed": False,
                    "already_requested": True, "completion_scope": "request_submission_not_download"}, audit)
            summary = (f"Submit one standard-quality movie request for '{entity['title']}' "
                       f"({entity['year'] or 'year unknown'}) as Seerr user {context[1]}; Seerr approval/download policy applies")
            if self.write_mode == "shadow":
                return _obs(call.name, "shadow_logged", {"summary": summary, "resolved_entity": entity}, audit)
            token = self.confirmation_store.mint(principal, call, summary=summary, resolved_entity=entity)
            return _obs(call.name, "confirmation_required", {"confirm_id": CONFIRMATION_PREFIX + token.confirm_id,
                "summary": summary, "resolved_entity": entity, "expires_at": token.expires_at}, audit, code=202)
        except _Clarify as exc:
            return _obs(call.name, "clarify_required", {"slot": "reference", "reason": str(exc)}, audit, code=409)
        except _UpstreamError as exc:
            return _obs(call.name, "denied" if exc.status == 403 else "error", {"error": exc.code}, audit, code=exc.status)

    def confirm_write(self, confirm_id: str, principal: Principal, *, idempotency_key: str | None = None,
                      now: float | None = None) -> AdapterObservation:
        with self._write_lock:
            return self._confirm(confirm_id, principal, now=now)

    def _confirm(self, confirm_id, principal, *, now):
        audit = []
        if self.write_mode != "confirm" or not self._permission(Call("request_media", {}), principal):
            return _obs("request_media", "denied", {"error": "permission_required_or_writes_disabled"}, audit, code=403)
        if not isinstance(confirm_id, str) or not confirm_id.startswith(CONFIRMATION_PREFIX):
            return _obs("request_media", "denied", {"error": "confirmation_namespace_invalid"}, audit, code=403)
        token, error = self.confirmation_store.redeem(confirm_id[len(CONFIRMATION_PREFIX):], principal, now=now)
        if error or token is None:
            return _obs("request_media", "denied", {"error": error or "confirmation_not_found"}, audit, code=403)
        if token.call_name != "request_media":
            return _obs("request_media", "denied", {"error": "confirmation_action_mismatch"}, audit, code=403)
        call = validate_call(token.call_name, token.arguments, allow_v3=False)
        confirmed_call = normalized(call)
        post_attempted = False
        posted = None
        try:
            context = self._context(principal)
            if token.resolved_entity.get("user_id") != context[1]:
                raise _UpstreamError("confirmation_user_mapping_changed", 403)
            if token.resolved_entity.get("api_root_digest") != hashlib.sha256(context[0].base_url.encode()).hexdigest():
                raise _UpstreamError("confirmation_service_changed", 409)
            expected_identity = token.resolved_entity.get("user_identity_fingerprint")
            if not isinstance(expected_identity, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_identity):
                raise _UpstreamError("confirmation_account_binding_missing", 409)
            entity, _availability, own, available_seasons = self._preflight(call, context, audit, expected_identity=expected_identity)
            if token.resolved_entity.get("operation") == "cancel_request":
                cancel_id = token.resolved_entity.get("cancel_request_id")
                entity_check = {**entity, "operation": "cancel_request", "cancel_request_id": cancel_id}
                if not _id(cancel_id) or entity_check != token.resolved_entity:
                    raise _UpstreamError("confirmation_entity_changed", 409)
                matching = [r for r in own if r["request_id"] == cancel_id]
                if not matching:
                    return _obs(call.name, "verified_ok", {"cancelled_request_id": cancel_id, "changed": False,
                        "already_cancelled": True, "confirmed_call": confirmed_call,
                        "completion_scope": "caller_owned_pending_request_cancelled"}, audit)
                if matching[0]["request_status"] != "pending":
                    raise _UpstreamError("request_no_longer_pending", 409)
                if self._identity(context, audit) != entity["user_identity_fingerprint"]:
                    raise _UpstreamError("confirmation_account_identity_changed", 409)
                post_attempted = True
                self._http("DELETE", f"/api/v1/request/{cancel_id}", context, audit)
                posted = {"request_id": cancel_id}
                after_own = self._own_requests(context, audit)
                if any(r["request_id"] == cancel_id for r in after_own):
                    raise _UpstreamError("cancelled_request_still_present")
                return _obs(call.name, "verified_ok", {"cancelled_request_id": cancel_id, "changed": True,
                    "confirmed_call": confirmed_call, "completion_scope": "caller_owned_pending_request_cancelled"}, audit)

            if entity["media_type"] == "tv":
                tok_seasons = token.resolved_entity.get("seasons")
                if (
                    not isinstance(tok_seasons, list)
                    or not tok_seasons
                    or not all(_id(s) for s in tok_seasons)
                    or not set(tok_seasons).issubset(set(available_seasons))
                ):
                    raise _UpstreamError("confirmation_entity_changed", 409)
                entity["seasons"] = tok_seasons
            if entity != token.resolved_entity or entity["media_type"] not in {"movie", "tv"}:
                raise _UpstreamError("confirmation_entity_changed", 409)
            existing = self._existing(own, target_seasons=entity["seasons"] if entity["media_type"] == "tv" else None)
            if existing:
                return _obs(call.name, "verified_ok", {"request": existing, "changed": False,
                    "already_requested": True, "confirmed_call": confirmed_call,
                    "completion_scope": "request_submission_not_download"}, audit)
            # Check effective upstream identity immediately before mutation too.
            if self._identity(context, audit) != entity["user_identity_fingerprint"]:
                raise _UpstreamError("confirmation_account_identity_changed", 409)
            post_attempted = True
            req_payload: dict[str, Any] = {"mediaType": entity["media_type"], "mediaId": entity["tmdb_id"]}
            if entity["media_type"] == "tv":
                req_payload["seasons"] = entity["seasons"]
            body = self._http("POST", "/api/v1/request", context, audit, body=req_payload)
            posted = self._verify_created(body, context[1], entity)
            current = self._http("GET", f"/api/v1/request/{posted['request_id']}", context, audit)
            verified = self._verify_created(current, context[1], entity)
            if verified["request_id"] != posted["request_id"]:
                raise _UpstreamError("created_request_identity_mismatch")
            return _obs(call.name, "verified_ok", {"request": verified, "changed": True,
                "confirmed_call": confirmed_call, "completion_scope": "request_submission_not_download"}, audit)
        except _Clarify as exc:
            return _obs(call.name, "denied", {"error": str(exc), "requires_new_confirmation": True}, audit, code=409)
        except _UpstreamError as exc:
            if post_attempted:
                return _obs(call.name, "dispatched" if posted is not None else "error", {
                    "error": exc.code, "outcome": "rejected" if posted is None and exc.code == "upstream_request_rejected" else "unknown",
                    "replayed": False, "confirmed_call": confirmed_call,
                    **({"request_id": posted["request_id"]} if posted is not None else {})}, audit, code=exc.status)
            return _obs(call.name, "denied" if exc.status in {403, 409} else "error", {
                "error": exc.code, "requires_new_confirmation": True}, audit, code=exc.status)

    def _verify_created(self, body, user_id, entity):
        if not isinstance(body, dict):
            raise _UpstreamError("invalid_created_request_response")
        row = self._owned_row(body, user_id)
        if row["tmdb_id"] != entity["tmdb_id"] or row["media_type"] != entity["media_type"] or row["is_4k"]:
            raise _UpstreamError("created_request_identity_mismatch")
        if entity["media_type"] == "tv" and "seasons" in row and set(row["seasons"]) != set(entity["seasons"]):
            raise _UpstreamError("created_request_identity_mismatch")
        return row
