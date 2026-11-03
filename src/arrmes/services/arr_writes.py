"""Confirmed native indexer searches for the three existing trained v2 actions."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import re
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal, resolve_title_candidates
from arrmes.protocol import Call, InvalidCall, normalized, validate_call
from .native import ServiceQueryAdapter, _UpstreamError, _id, _norm, _rows, _text

CONFIRMATION_PREFIX = "native-arr:"
ARR_ACTIONS = {"tv_search_missing": ("sonarr", "v3", 4),
               "movie_search": ("radarr", "v3", 6),
               "music_search": ("lidarr", "v1", 3)}
_JOB_STATES = {"queued", "started", "completed", "failed", "aborted", "cancelled", "orphaned"}


class _Clarify(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate upstream JSON key")
        data[key] = value
    return data


def _label(value, limit=2048):
    return _text(value) and len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _api_integer(value, *, zero=False):
    return _id(value, zero=zero) and value <= 2_147_483_647


def _obs(name, status, data, audit, code=200, *, complete=False):
    service = ARR_ACTIONS[name][0] if name in ARR_ACTIONS else "configured-arr"
    return AdapterObservation(status, service, name, code, complete, {"source": "native_api", **data}, audit)


class NativeArrWriteAdapter:
    """Finite exact-ID commands; no global missing search, add, or monitoring write.

    Artist-only music_search is an artist-wide typed request. The NL verifier
    must establish that scope explicitly rather than treating an omitted album
    as evidence. All downloads remain dispatched; command-job evidence and media
    file completion are reported separately.
    """

    def __init__(self, native: ServiceQueryAdapter, *, write_mode: str = "confirm", confirmation_store=None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if write_mode not in {"confirm", "shadow", "disabled"}:
            raise ValueError("Native Arr writes require confirm, shadow, or disabled mode")
        from arrmes.live_adapter import WriteConfirmationStore
        self.native, self.write_mode = native, write_mode
        self.confirmation_store = confirmation_store or WriteConfirmationStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_targets = min(native.max_records, 250)

    def supports(self, name: str) -> bool:
        return name in ARR_ACTIONS and ARR_ACTIONS[name][0] in self.native.configs

    @staticmethod
    def _authorized(principal):
        return bool(principal.subject and "media:manage" in principal.scopes)

    def _now(self):
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _UpstreamError("invalid_utc_clock")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _stamp(value):
        if value is None:
            return None
        if not _label(value, 100):
            raise _UpstreamError("invalid_media_release_date")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _UpstreamError("invalid_media_release_date") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise _UpstreamError("invalid_media_release_date")
        return parsed.astimezone(timezone.utc)

    def _http(self, service, method, path, audit, *, body=None):
        """One bounded attempt with strict JSON, no redirect or retry."""
        config = self.native.configs[service]
        headers = {"Accept": "application/json", "X-Api-Key": config.api_key, "User-Agent": "arrmes-native-arr-write/1"}
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body, allow_nan=False).encode()
        request = urllib.request.Request(config.base_url.rstrip("/") + path, data=raw, headers=headers, method=method)
        recorded = False
        try:
            with self.native._opener.open(request, timeout=self.native.timeout_seconds) as response:
                code = int(response.status)
                audit.append({"method": method, "path": path, "status": code, "attempt": 1})
                recorded = True
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared = int(declared)
                    except ValueError:
                        raise _UpstreamError("invalid_upstream_content_length") from None
                    if not 0 <= declared <= self.native.max_response_bytes:
                        raise _UpstreamError("response_limit_exceeded")
                chunks, total, deadline = [], 0, time.monotonic() + self.native.timeout_seconds
                read = getattr(response, "read1", response.read)
                while True:
                    chunk = read(min(65_536, self.native.max_response_bytes + 1 - total))
                    if time.monotonic() > deadline:
                        raise _UpstreamError("upstream_response_deadline_exceeded", 504)
                    total += len(chunk)
                    if total > self.native.max_response_bytes:
                        raise _UpstreamError("response_limit_exceeded")
                    if not chunk:
                        break
                    chunks.append(chunk)
                payload = b"".join(chunks)
                if declared is not None and total != declared:
                    raise _UpstreamError("incomplete_upstream_response")
                try:
                    data = json.loads(payload.decode(), object_pairs_hook=_unique,
                                      parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("Nonfinite JSON"))) if payload else None
                except (ValueError, UnicodeError, RecursionError):
                    raise _UpstreamError("invalid_upstream_json") from None
                if method == "GET" and code != 200:
                    raise _UpstreamError("unexpected_upstream_status", 502)
                return code, data
        except urllib.error.HTTPError as exc:
            code = int(exc.code);exc.close()
            audit.append({"method": method, "path": path, "status": code, "attempt": 1})
            error = "upstream_authentication_failed" if code in {401, 403} else "upstream_redirect_refused" if 300 <= code < 400 else "upstream_http_error"
            raise _UpstreamError(error, code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if not recorded:
                audit.append({"method": method, "path": path, "status": 503, "attempt": 1})
            else:
                audit[-1]["response_read_error"] = True
            raise _UpstreamError("service_unavailable", 503) from None

    def _get(self, service, path, audit):
        return self._http(service, "GET", path, audit)[1]

    def _identity(self, service, api, major, audit):
        config = self.native.configs.get(service)
        if config is None or not config.api_key:
            raise _UpstreamError("configured_service_api_key_required", 503)
        status = self._get(service, f"/api/{api}/system/status", audit)
        if (not isinstance(status, dict) or not isinstance(status.get("appName"), str)
                or status["appName"].casefold() != service):
            raise _UpstreamError("service_identity_mismatch")
        version = status.get("version")
        if not isinstance(version, str) or re.fullmatch(rf"{major}\.\d+\.\d+(?:\.\d+)?", version) is None:
            raise _UpstreamError("unsupported_arr_service_version", 501)
        start = status.get("startTime")
        if self._stamp(start) is None:
            raise _UpstreamError("invalid_service_start_identity")
        return {"service": service, "api": api, "version": version, "start_time": start,
                "origin_sha256": _digest(config.base_url)}

    @staticmethod
    def _inventory(payload, *, parent_key=None, parent_id=None):
        # These audited native endpoints are complete unpaginated JSON arrays.
        # A paginated wrapper is not silently treated as a complete inventory.
        rows = _rows(payload)
        seen = set()
        for row in rows:
            if not _api_integer(row.get("id")) or row["id"] in seen:
                raise _UpstreamError("invalid_or_duplicate_arr_identity")
            seen.add(row["id"])
            if parent_key is not None and (not _api_integer(row.get(parent_key)) or row[parent_key] != parent_id):
                raise _UpstreamError("inconsistent_arr_parent_identity")
        return rows

    @staticmethod
    def _exact(rows, text, field):
        if any(not _label(r.get(field)) for r in rows):
            raise _UpstreamError("invalid_arr_entity_name")
        matches = [r for r in rows if _norm(r[field]) == _norm(text)]
        if len(matches) != 1:
            raise _Clarify("entity_not_uniquely_resolved")
        return matches[0]

    @staticmethod
    def _path_identity(row):
        if not _label(row.get("path"), 8192):
            raise _UpstreamError("invalid_arr_media_path_identity")
        return _digest(row["path"])

    @staticmethod
    def _year(row):
        value = row.get("year")
        if value is not None and (not _id(value, zero=True) or value and not 1800 <= value <= 2300):
            raise _UpstreamError("invalid_arr_media_year")
        return value

    def _tv(self, args, audit):
        rows = self._inventory(self._get("sonarr", "/api/v3/series", audit))
        if any(not _label(r.get("title")) for r in rows):
            raise _UpstreamError("invalid_arr_entity_name")
        matches = resolve_title_candidates(args["title"], rows)
        if len(matches) != 1:
            raise _Clarify("entity_not_uniquely_resolved")
        series = matches[0]
        if type(series.get("monitored")) is not bool:
            raise _UpstreamError("invalid_series_monitoring")
        if not series["monitored"]:
            raise _Clarify("series_not_monitored")
        episodes = self._inventory(self._get("sonarr", "/api/v3/episode?" + urllib.parse.urlencode({"seriesId": series["id"]}), audit),
                                   parent_key="seriesId", parent_id=series["id"])
        selected, undated, now = [], 0, self._now()
        for row in episodes:
            if (not _api_integer(row.get("seasonNumber"), zero=True) or not _api_integer(row.get("episodeNumber"), zero=True)
                    or type(row.get("monitored")) is not bool or type(row.get("hasFile")) is not bool):
                raise _UpstreamError("invalid_episode_search_state")
            season = row["seasonNumber"]
            if (row["hasFile"] or not row["monitored"] or ("season" in args and args["season"] != season)
                    or ("season" not in args and season == 0)):
                continue
            stamp = self._stamp(row.get("airDateUtc"))
            if stamp is None:
                undated += 1
                continue
            if stamp <= now:
                selected.append({k: row[k] for k in ("id", "seriesId", "seasonNumber", "episodeNumber", "monitored", "hasFile", "airDateUtc")})
        selected.sort(key=lambda r:r["id"])
        return {"series_id": series["id"], "title": series["title"], "year": self._year(series),
                "path_sha256": self._path_identity(series), "series_monitored": True,
                "season": args.get("season"), "undated_episodes_excluded": undated,
                "target_snapshot_sha256": _digest(selected), "target_ids": [r["id"] for r in selected],
                "command": {"name": "EpisodeSearch", "episodeIds": [r["id"] for r in selected]}}

    def _movie(self, args, audit):
        rows = self._inventory(self._get("radarr", "/api/v3/movie", audit))
        if any(not _label(r.get("title")) for r in rows):
            raise _UpstreamError("invalid_movie_title")
        matches = [r for r in rows if _norm(r["title"]) == _norm(args["title"])]
        for row in matches:self._year(row)
        matches = [r for r in matches if "year" not in args or r.get("year") == args["year"]]
        if len(matches) != 1:
            raise _Clarify("movie_not_uniquely_resolved")
        row = matches[0]
        if type(row.get("monitored")) is not bool or type(row.get("hasFile")) is not bool:
            raise _UpstreamError("invalid_movie_monitoring_or_file_state")
        return {"movie_id": row["id"], "title": row["title"], "year": self._year(row),
                "path_sha256": self._path_identity(row), "monitored": row["monitored"], "has_file": row["hasFile"],
                "target_ids": [row["id"]], "command": {"name": "MoviesSearch", "movieIds": [row["id"]]}}

    def _music(self, args, audit):
        artist = self._exact(self._inventory(self._get("lidarr", "/api/v1/artist", audit)), args["artist"], "artistName")
        if type(artist.get("monitored")) is not bool:
            raise _UpstreamError("invalid_artist_monitoring")
        if not artist["monitored"]:
            raise _Clarify("artist_not_monitored")
        albums = self._inventory(self._get("lidarr", "/api/v1/album?" + urllib.parse.urlencode({"artistId": artist["id"]}), audit),
                                 parent_key="artistId", parent_id=artist["id"])
        if any(not _label(r.get("title")) or type(r.get("monitored")) is not bool for r in albums):
            raise _UpstreamError("invalid_album_search_state")
        if "album" in args:
            albums = [self._exact(albums, args["album"], "title")]
        selected, undated, now = [], 0, self._now()
        for row in albums:
            if not row["monitored"]:
                continue
            stamp = self._stamp(row.get("releaseDate"))
            if stamp is None:
                undated += 1
                continue
            if stamp <= now:
                stats = row.get("statistics")
                if (not isinstance(stats, dict) or not _api_integer(stats.get("trackCount"), zero=True)
                        or not _api_integer(stats.get("trackFileCount"), zero=True)):
                    raise _UpstreamError("invalid_album_track_statistics")
                if not stats["trackCount"]:
                    raise _Clarify("album_track_metadata_incomplete")
                if "album" not in args and stats["trackFileCount"] >= stats["trackCount"]:
                    continue
                selected.append({**{k: row[k] for k in ("id", "artistId", "title", "monitored", "releaseDate")},
                                 "track_count":stats["trackCount"], "track_file_count":stats["trackFileCount"]})
        selected.sort(key=lambda r:r["id"])
        return {"artist_id": artist["id"], "artist": artist["artistName"], "artist_monitored": True,
                "album": args.get("album"), "path_sha256": self._path_identity(artist),
                "scope": "exact_album" if "album" in args else "explicit_artist_monitored_released_missing_albums",
                "undated_albums_excluded": undated, "target_snapshot_sha256": _digest(selected),
                "target_ids": [r["id"] for r in selected], "album_titles": [r["title"] for r in selected],
                "command": {"name": "AlbumSearch", "albumIds": [r["id"] for r in selected]}}

    def _resolve(self, call, audit):
        service, api, major = ARR_ACTIONS[call.name]
        target = self._identity(service, api, major, audit)
        target.update(getattr(self, {"tv_search_missing":"_tv", "movie_search":"_movie", "music_search":"_music"}[call.name])(call.arguments, audit))
        if (not target["target_ids"] and call.name == "music_search" and "album" not in call.arguments
                and target["undated_albums_excluded"]):
            raise _Clarify("album_release_metadata_incomplete")
        if not target["target_ids"] and not (call.name == "music_search" and "album" not in call.arguments):
            raise _Clarify("no_eligible_search_targets")
        if len(target["target_ids"]) > self.max_targets:
            raise _Clarify("search_target_budget_exceeded_narrow_scope")
        return target

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None):
        call = validate_call(call.name, call.arguments, allow_v3=False)
        if call.name not in ARR_ACTIONS:
            raise InvalidCall("Native Arr writes accept only the three trained search actions")
        audit = []
        if not self._authorized(principal):
            return _obs(call.name, "denied", {"error":"permission_required"}, audit, 403)
        if self.write_mode == "disabled":
            return _obs(call.name, "denied", {"error":"writes_disabled"}, audit, 403)
        try:
            target = self._resolve(call, audit)
            if not target["target_ids"]:
                return _obs(call.name, "verified_ok", {"changed":False,"search_dispatched":False,
                    "reason":"no_eligible_released_missing_albums","resolved_entity":target,
                    "media_download_completion_verified":False}, audit, complete=True)
            summary = f"Run {target['command']['name']} in {target['service']} for {target.get('title',target.get('artist'))!r}; exact target IDs: {target['target_ids']}. May enqueue downloads; does not change monitoring."
            if target['command']['name'] == 'AlbumSearch':
                albums = ', '.join(f"{title!r} (ID {identity})" for title, identity in zip(target['album_titles'], target['target_ids']))
                summary += f" Album targets: {albums}."
            if self.write_mode == "shadow":
                return _obs(call.name, "shadow_logged", {"summary":summary,"resolved_entity":target}, audit)
            token = self.confirmation_store.mint(principal, call, summary=summary, resolved_entity=target)
            return _obs(call.name, "confirmation_required", {"confirm_id":CONFIRMATION_PREFIX+token.confirm_id,
                "summary":summary,"expires_at":token.expires_at,"resolved_entity":target}, audit)
        except _Clarify as exc:
            return _obs(call.name, "clarify_required", {"slot":"reference","reason":str(exc)}, audit, 409)
        except _UpstreamError as exc:
            return _obs(call.name, "error", {"error":exc.code,"outcome":"not_dispatched"}, audit, exc.status)

    @staticmethod
    def _correlated(resource, target, command_id=None):
        expected = target["command"]
        if (not isinstance(resource, dict) or not _api_integer(resource.get("id"))
                or (command_id is not None and resource["id"] != command_id)
                or resource.get("name") != expected["name"] or not isinstance(resource.get("body"), dict)):
            return False
        body = resource["body"]
        if body.get("name") != expected["name"]:
            return False
        key = next(k for k in expected if k != "name")
        ids = body.get(key)
        if not isinstance(ids, list) or any(not _api_integer(i) for i in ids) or len(set(ids)) != len(ids) or sorted(ids) != expected[key]:
            return False
        scope_keys = {"episodeIds","movieIds","albumIds","seriesId","artistId","seasonNumber"}
        return set(body)&scope_keys == {key}

    def confirm_write(self, confirm_id: str, principal: Principal, *, idempotency_key: str | None = None, now: float | None = None):
        audit = []
        if not self._authorized(principal) or self.write_mode != "confirm":
            return _obs("confirm_write", "denied", {"error":"permission_required_or_writes_disabled"}, audit, 403)
        if not isinstance(confirm_id, str) or not confirm_id.startswith(CONFIRMATION_PREFIX):
            return _obs("confirm_write", "denied", {"error":"confirmation_namespace_invalid"}, audit, 403)
        token, error = self.confirmation_store.redeem(confirm_id[len(CONFIRMATION_PREFIX):], principal, now=now)
        if error or token is None:
            return _obs("confirm_write", "denied", {"error":error or "confirmation_not_found"}, audit, 403)
        if token.call_name not in ARR_ACTIONS:
            return _obs("confirm_write", "denied", {"error":"confirmation_action_mismatch"}, audit, 403)
        call = validate_call(token.call_name, token.arguments, allow_v3=False)
        confirmed = {"confirmed_call":normalized(call),"media_download_completion_verified":False}
        try:
            target = self._resolve(call, audit)
            if target != token.resolved_entity:
                return _obs(call.name, "denied", {**confirmed,"error":"confirmation_target_or_service_changed"}, audit, 409)
        except _Clarify as exc:
            return _obs(call.name, "denied", {**confirmed,"error":"confirmation_target_unavailable","reason":str(exc)}, audit, 409)
        except _UpstreamError as exc:
            return _obs(call.name, "error", {**confirmed,"error":exc.code,"outcome":"not_dispatched"}, audit, exc.status)
        service, api = target["service"], target["api"]
        try:
            code, resource = self._http(service, "POST", f"/api/{api}/command", audit, body=target["command"])
        except _UpstreamError as exc:
            rejected = exc.status in {400,401,403,404,405,409,422,429}
            return _obs(call.name, "error", {**confirmed,"error":exc.code,"outcome":"rejected" if rejected else "unknown",
                         "mutation_attempts":1,"replayed":False}, audit, exc.status)
        data = {**confirmed,"outcome":"dispatched","mutation_attempts":1,"replayed":False,
                "target_ids":target["target_ids"],"command_scope_correlated":False,
                "search_job_status":"unknown","search_job_completion_verified":False}
        if code not in {201,202}:
            return _obs(call.name, "error", {**data,"outcome":"unknown","error":"unexpected_command_acceptance_status"}, audit, code)
        if not self._correlated(resource, target):
            return _obs(call.name, "dispatched", {**data,"observation_note":"accepted_command_identity_not_correlatable"}, audit, code)
        command_id = resource["id"]
        data["command_id"] = command_id
        try:
            observed = self._get(service, f"/api/{api}/command/{command_id}", audit)
        except _UpstreamError:
            return _obs(call.name, "dispatched", {**data,"observation_note":"command_status_unavailable"}, audit, code)
        if self._correlated(observed, target, command_id) and isinstance(observed.get("status"), str) and observed["status"] in _JOB_STATES:
            data.update(command_scope_correlated=True, search_job_status=observed["status"],
                        search_job_completion_verified=observed["status"] == "completed" and observed.get("result") == "successful")
        else:
            data["observation_note"] = "command_status_identity_or_scope_mismatch"
        # Even a completed command is an indexer search job, not downloaded files.
        return _obs(call.name, "dispatched", data, audit, code)
