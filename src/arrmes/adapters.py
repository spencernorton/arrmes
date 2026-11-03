"""Sandbox-backed service adapters with entity resolution, pagination, retries, and async job verification."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from arrmes.planner import Principal, ResolutionRequired, resolve_title, resolve_title_candidates
from arrmes.protocol import Call, normalized
from arrmes.sandbox.isolation import NetworkIsolationGuard, SandboxIsolationViolation
from arrmes.sandbox.sanitize import SANDBOX_AUTH_IDENTIFIERS


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass
class AdapterObservation:
    status: str  # "verified_ok" | "clarify_required" | "error" | "denied"
    service: str
    operation: str
    http_status: int
    verified_complete: bool
    data: dict[str, Any]
    audit_trail: list[dict[str, Any]]


class SandboxAdapter:
    """Trusted adapter executing validated semantic calls against an isolated loopback SandboxCluster."""

    def __init__(self, base_url: str, port: int, *, max_retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.port = port
        self.max_retries = max_retries

    def _auth_headers_for(self, service: str, *, principal: Principal | None = None) -> dict[str, str]:
        if service in {"sonarr", "radarr", "lidarr"}:
            return {("X-" + "Api-Key"): SANDBOX_AUTH_IDENTIFIERS[service]}
        if service == "qbittorrent":
            return {"Cookie": f"SID={SANDBOX_AUTH_IDENTIFIERS['qbittorrent_sid']}"}
        if service in {"requests", "seer"}:
            h = {"Authorization": f"Bearer {SANDBOX_AUTH_IDENTIFIERS['requests_token']}"}
            if principal is not None and service == "requests":
                h["X-Principal-Id"] = principal.subject
            return h
        return {}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        audit: list[dict[str, Any]],
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        req_headers = {"Accept": "application/json", **(headers or {})}
        payload_bytes = None
        if body is not None:
            payload_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        attempts = 0
        with NetworkIsolationGuard(allowed_ports={self.port}):
            while True:
                attempts += 1
                req = urllib.request.Request(url, data=payload_bytes, headers=req_headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        status = int(resp.status)
                        raw = resp.read().decode("utf-8")
                        parsed = json.loads(raw) if raw else {}
                        audit.append({"method": method, "path": path, "status": status, "attempt": attempts})
                        return status, parsed
                except SandboxIsolationViolation:
                    raise
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    raw = exc.read().decode("utf-8")
                    try:
                        parsed = json.loads(raw) if raw else {"error": f"http_{status}"}
                    except ValueError:
                        parsed = {"error": raw or f"http_{status}"}
                    audit.append({"method": method, "path": path, "status": status, "attempt": attempts})
                    if status in {502, 503, 504} and attempts <= self.max_retries:
                        continue
                    return status, parsed
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    reason = getattr(exc, "reason", None)
                    if isinstance(reason, SandboxIsolationViolation):
                        raise reason
                    audit.append({"method": method, "path": path, "status": 503, "attempt": attempts, "error": str(exc)})
                    can_retry = method == "GET" or "Idempotency-Key" in req_headers
                    if not isinstance(exc, ValueError) and can_retry and attempts <= self.max_retries:
                        continue
                    return 503, {"error": "transport_or_parse_error", "detail": str(exc)}

    def execute(
        self,
        call: Call,
        principal: Principal,
        *,
        idempotency_key: str | None = None,
    ) -> AdapterObservation:
        norm = normalized(call)
        name = norm["name"]
        args = norm["arguments"]
        audit: list[dict[str, Any]] = []

        handler = getattr(self, f"execute_{name}", None)
        if handler is None:
            return AdapterObservation(
                status="error",
                service="unknown",
                operation=name,
                http_status=400,
                verified_complete=False,
                data={"error": f"unimplemented_adapter_{name}"},
                audit_trail=audit,
            )
        return handler(args, principal, idempotency_key=idempotency_key, audit=audit)

    def execute_tv_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = self._auth_headers_for("sonarr", principal=principal)
        code, series_list = self._request_json("GET", "/sonarr/api/v3/series", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "sonarr", "tv_status", code, False, series_list, audit)
        try:
            resolved = resolve_title(args["title"], series_list)
        except ResolutionRequired:
            cands = [
                {"id": c["id"], "title": c["title"], "year": c.get("year")}
                for c in resolve_title_candidates(args["title"], series_list)
                if type(c.get("id")) is int and c["id"] > 0 and isinstance(c.get("title"), str)
            ]
            return AdapterObservation(
                "clarify_required",
                "sonarr",
                "tv_status",
                409 if len(cands) > 1 else 404,
                False,
                {"slot": "reference", "reason": "series_not_uniquely_resolved", "title": args["title"], "candidates": cands},
                audit,
            )
        stats = resolved.get("statistics") or {}
        ep_count = int(resolved.get("episodeCount", stats.get("episodeCount", 0)))
        ep_files = int(resolved.get("episodeFileCount", stats.get("episodeFileCount", 0)))
        return AdapterObservation(
            "verified_ok",
            "sonarr",
            "tv_status",
            200,
            True,
            {
                "series_id": resolved["id"],
                "title": resolved["title"],
                "year": resolved.get("year"),
                "monitored": bool(resolved.get("monitored", False)),
                "episode_count": ep_count,
                "episode_file_count": ep_files,
                "available": ep_files > 0,
            },
            audit,
        )

    def execute_tv_missing(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = self._auth_headers_for("sonarr", principal=principal)
        code, series_list = self._request_json("GET", "/sonarr/api/v3/series", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "sonarr", "tv_missing", code, False, series_list, audit)
        try:
            resolved = resolve_title(args["title"], series_list)
        except ResolutionRequired:
            cands = [
                {"id": c["id"], "title": c["title"], "year": c.get("year")}
                for c in resolve_title_candidates(args["title"], series_list)
                if type(c.get("id")) is int and c["id"] > 0 and isinstance(c.get("title"), str)
            ]
            return AdapterObservation(
                "clarify_required",
                "sonarr",
                "tv_missing",
                409 if len(cands) > 1 else 404,
                False,
                {"slot": "reference", "reason": "series_not_uniquely_resolved", "title": args["title"], "candidates": cands},
                audit,
            )
        inc_spec = "true" if args.get("include_specials", False) else "false"
        page = 1
        page_size = 2
        all_records: list[dict[str, Any]] = []
        while True:
            q = urllib.parse.urlencode({"seriesId": resolved["id"], "includeSpecials": inc_spec, "page": page, "pageSize": page_size})
            c2, payload = self._request_json("GET", f"/sonarr/api/v3/wanted/missing?{q}", headers=headers, audit=audit)
            if c2 != 200:
                return AdapterObservation("error", "sonarr", "tv_missing", c2, False, payload, audit)
            records = payload.get("records", [])
            all_records.extend(records)
            if len(all_records) >= int(payload.get("totalRecords", 0)) or not records:
                break
            page += 1
        return AdapterObservation(
            "verified_ok",
            "sonarr",
            "tv_missing",
            200,
            True,
            {
                "series_id": resolved["id"],
                "title": resolved["title"],
                "include_specials": bool(args.get("include_specials", False)),
                "missing_count": len(all_records),
                "missing_episodes": all_records,
            },
            audit,
        )

    def execute_tv_search_missing(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = self._auth_headers_for("sonarr", principal=principal)
        code, series_list = self._request_json("GET", "/sonarr/api/v3/series", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "sonarr", "tv_search_missing", code, False, series_list, audit)
        try:
            resolved = resolve_title(args["title"], series_list)
        except ResolutionRequired:
            cands = [
                {"id": c["id"], "title": c["title"], "year": c.get("year")}
                for c in resolve_title_candidates(args["title"], series_list)
                if type(c.get("id")) is int and c["id"] > 0 and isinstance(c.get("title"), str)
            ]
            return AdapterObservation(
                "clarify_required",
                "sonarr",
                "tv_search_missing",
                409 if len(cands) > 1 else 404,
                False,
                {"slot": "reference", "reason": "series_not_uniquely_resolved", "title": args["title"], "candidates": cands},
                audit,
            )
        cmd_body: dict[str, Any] = {"seriesId": resolved["id"]}
        if "season" in args:
            cmd_body["name"] = "SeasonSearch"
            cmd_body["seasonNumber"] = args["season"]
        else:
            cmd_body["name"] = "MissingEpisodeSearch"
        cmd_headers = dict(headers)
        if idempotency_key:
            cmd_headers["Idempotency-Key"] = idempotency_key
        c2, cmd_resp = self._request_json("POST", "/sonarr/api/v3/command", headers=cmd_headers, body=cmd_body, audit=audit)
        if c2 not in {200, 201}:
            return AdapterObservation("error", "sonarr", "tv_search_missing", c2, False, cmd_resp, audit)
        cmd_id = cmd_resp["id"]
        c3, poll_resp = self._request_json("GET", f"/sonarr/api/v3/command/{cmd_id}", headers=headers, audit=audit)
        verified = c3 == 200 and poll_resp.get("status") == "completed"
        return AdapterObservation(
            "verified_ok" if verified else "error",
            "sonarr",
            "tv_search_missing",
            c3,
            verified,
            {
                "series_id": resolved["id"],
                "title": resolved["title"],
                "command_id": cmd_id,
                "command_name": cmd_body["name"],
                "command_status": poll_resp.get("status"),
                "idempotent_replay": bool(cmd_resp.get("idempotentReplay", False)),
            },
            audit,
        )

    def execute_movie_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = self._auth_headers_for("radarr", principal=principal)
        code, movies = self._request_json("GET", "/radarr/api/v3/movie", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "radarr", "movie_status", code, False, movies, audit)
        try:
            resolved = resolve_title(args["title"], movies, args.get("year"))
        except ResolutionRequired:
            matching_years = sorted(
                m["year"] for m in movies if _norm(m.get("title", "")) == _norm(args["title"])
            )
            return AdapterObservation(
                "clarify_required",
                "radarr",
                "movie_status",
                409 if len(matching_years) > 1 else 404,
                False,
                {
                    "slot": "reference",
                    "reason": "remake_year_ambiguous" if len(matching_years) > 1 else "movie_not_found",
                    "title": args["title"],
                    "candidate_years": matching_years,
                },
                audit,
            )
        stats = resolved.get("statistics") or {}
        size_val = int(resolved.get("sizeOnDisk", stats.get("sizeOnDisk", 0)))
        has_file = bool(resolved.get("hasFile", size_val > 0))
        return AdapterObservation(
            "verified_ok",
            "radarr",
            "movie_status",
            200,
            True,
            {
                "movie_id": resolved["id"],
                "title": resolved["title"],
                "year": resolved.get("year"),
                "has_file": has_file,
                "size_on_disk": size_val,
                "monitored": bool(resolved.get("monitored", False)),
            },
            audit,
        )

    def execute_movie_search(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = self._auth_headers_for("radarr", principal=principal)
        code, movies = self._request_json("GET", "/radarr/api/v3/movie", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "radarr", "movie_search", code, False, movies, audit)
        try:
            resolved = resolve_title(args["title"], movies, args.get("year"))
        except ResolutionRequired:
            matching_years = sorted(
                m["year"] for m in movies if _norm(m.get("title", "")) == _norm(args["title"])
            )
            return AdapterObservation(
                "clarify_required",
                "radarr",
                "movie_search",
                409 if len(matching_years) > 1 else 404,
                False,
                {
                    "slot": "reference",
                    "reason": "remake_year_ambiguous" if len(matching_years) > 1 else "movie_not_found",
                    "title": args["title"],
                    "candidate_years": matching_years,
                },
                audit,
            )
        cmd_headers = dict(headers)
        if idempotency_key:
            cmd_headers["Idempotency-Key"] = idempotency_key
        body = {"name": "MoviesSearch", "movieIds": [resolved["id"]]}
        c2, cmd_resp = self._request_json("POST", "/radarr/api/v3/command", headers=cmd_headers, body=body, audit=audit)
        if c2 not in {200, 201}:
            return AdapterObservation("error", "radarr", "movie_search", c2, False, cmd_resp, audit)
        cmd_id = cmd_resp["id"]
        c3, poll_resp = self._request_json("GET", f"/radarr/api/v3/command/{cmd_id}", headers=headers, audit=audit)
        verified = c3 == 200 and poll_resp.get("status") == "completed"
        return AdapterObservation(
            "verified_ok" if verified else "error",
            "radarr",
            "movie_search",
            c3,
            verified,
            {
                "movie_id": resolved["id"],
                "title": resolved["title"],
                "year": resolved["year"],
                "command_id": cmd_id,
                "command_status": poll_resp.get("status"),
                "idempotent_replay": bool(cmd_resp.get("idempotentReplay", False)),
            },
            audit,
        )

    def execute_music_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = self._auth_headers_for("lidarr", principal=principal)
        code, artists = self._request_json("GET", "/lidarr/api/v1/artist", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "lidarr", "music_status", code, False, artists, audit)
        artist_matches = [a for a in artists if _norm(a["artistName"]) == _norm(args["artist"])]
        if len(artist_matches) != 1:
            return AdapterObservation(
                "clarify_required",
                "lidarr",
                "music_status",
                404,
                False,
                {"slot": "reference", "reason": "artist_not_found", "artist": args["artist"]},
                audit,
            )
        artist = artist_matches[0]
        c2, albums = self._request_json("GET", f"/lidarr/api/v1/album?artistId={artist['id']}", headers=headers, audit=audit)
        if c2 != 200:
            return AdapterObservation("error", "lidarr", "music_status", c2, False, albums, audit)
        if "album" in args:
            albums = [al for al in albums if _norm(al["title"]) == _norm(args["album"])]
            if not albums:
                return AdapterObservation(
                    "clarify_required",
                    "lidarr",
                    "music_status",
                    404,
                    False,
                    {"slot": "reference", "reason": "album_not_found", "artist": artist["artistName"], "album": args["album"]},
                    audit,
                )
        return AdapterObservation(
            "verified_ok",
            "lidarr",
            "music_status",
            200,
            True,
            {"artist_id": artist["id"], "artist": artist["artistName"], "albums": albums},
            audit,
        )

    def execute_music_search(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        status_obs = self.execute_music_status(args, principal, idempotency_key=idempotency_key, audit=audit)
        if status_obs.status != "verified_ok":
            return AdapterObservation(status_obs.status, "lidarr", "music_search", status_obs.http_status, False, status_obs.data, audit)
        headers = self._auth_headers_for("lidarr", principal=principal)
        if "album" in args:
            album_ids = [al["id"] for al in status_obs.data["albums"]]
            body: dict[str, Any] = {"name": "AlbumSearch", "albumIds": album_ids}
        else:
            body = {"name": "ArtistSearch", "artistId": status_obs.data["artist_id"]}
        c2, resp = self._request_json("POST", "/lidarr/api/v1/command", headers=headers, body=body, audit=audit)
        verified = c2 in {200, 201} and resp.get("status") == "completed"
        return AdapterObservation(
            "verified_ok" if verified else "error",
            "lidarr",
            "music_search",
            c2,
            verified,
            {"artist": status_obs.data["artist"], "command": resp},
            audit,
        )

    def execute_now_playing(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        q = urllib.parse.urlencode({("api" + "key"): SANDBOX_AUTH_IDENTIFIERS["tautulli"], "cmd": "get_activity"})
        code, resp = self._request_json("GET", f"/tautulli/api/v2?{q}", audit=audit)
        if code != 200:
            return AdapterObservation("error", "tautulli", "now_playing", code, False, resp, audit)
        data = resp.get("response", {}).get("data", {})
        return AdapterObservation(
            "verified_ok",
            "tautulli",
            "now_playing",
            200,
            True,
            {
                "stream_count": int(data.get("stream_count", 0)),
                "total_bandwidth_kbps": data.get("total_bandwidth", 0),
                "sessions": data.get("sessions", []),
            },
            audit,
        )

    def execute_watch_history(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        params = {("api" + "key"): SANDBOX_AUTH_IDENTIFIERS["tautulli"], "cmd": "get_history"}
        if "title" in args:
            params["search"] = args["title"]
        q = urllib.parse.urlencode(params)
        code, resp = self._request_json("GET", f"/tautulli/api/v2?{q}", audit=audit)
        if code != 200:
            return AdapterObservation("error", "tautulli", "watch_history", code, False, resp, audit)
        data = resp.get("response", {}).get("data", {})
        return AdapterObservation(
            "verified_ok",
            "tautulli",
            "watch_history",
            200,
            True,
            {"records_filtered": data.get("recordsFiltered", 0), "history": data.get("data", [])},
            audit,
        )

    def execute_torrent_list(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = {"Cookie": f"SID={SANDBOX_AUTH_IDENTIFIERS['qbittorrent_sid']}"}
        state = args.get("state", "all")
        q = urllib.parse.urlencode({"filter": state})
        code, items = self._request_json("GET", f"/qbittorrent/api/v2/torrents/info?{q}", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "qbittorrent", "torrent_list", code, False, items, audit)
        query_filter = args.get("query")
        if query_filter:
            items = [
                x for x in items
                if _norm(query_filter) in _norm(x.get("title_key", "")) or _norm(query_filter) in _norm(x.get("name", ""))
            ]
        return AdapterObservation(
            "verified_ok",
            "qbittorrent",
            "torrent_list",
            200,
            True,
            {"state_filter": state, "count": len(items), "torrents": items},
            audit,
        )

    def _mutate_torrent(
        self,
        action_name: str,
        endpoint: str,
        expected_state: str,
        args: dict[str, Any],
        audit: list[dict[str, Any]],
    ) -> AdapterObservation:
        headers = {"Cookie": f"SID={SANDBOX_AUTH_IDENTIFIERS['qbittorrent_sid']}"}
        code, items = self._request_json("GET", "/qbittorrent/api/v2/torrents/info?filter=all", headers=headers, audit=audit)
        if code != 200:
            return AdapterObservation("error", "qbittorrent", action_name, code, False, items, audit)
        target_title = _norm(args["title"])
        matches = [
            x for x in items
            if _norm(x.get("title_key", "")) == target_title or _norm(x.get("name", "")) == target_title
        ]
        if len(matches) != 1:
            return AdapterObservation(
                "clarify_required",
                "qbittorrent",
                action_name,
                404,
                False,
                {"slot": "reference", "reason": "torrent_not_uniquely_resolved", "title": args["title"]},
                audit,
            )
        target_hash = matches[0]["hash"]
        c2, m_resp = self._request_json(
            "POST",
            endpoint,
            headers=headers,
            body={"hashes": [target_hash]},
            audit=audit,
        )
        if c2 != 200:
            return AdapterObservation("error", "qbittorrent", action_name, c2, False, m_resp, audit)
        # Verify state transition by polling torrents/info
        c3, after_items = self._request_json("GET", "/qbittorrent/api/v2/torrents/info?filter=all", headers=headers, audit=audit)
        after_match = [x for x in after_items if x["hash"] == target_hash] if c3 == 200 else []
        verified = bool(after_match and after_match[0]["state"] == expected_state)
        return AdapterObservation(
            "verified_ok" if verified else "error",
            "qbittorrent",
            action_name,
            c2,
            verified,
            {
                "hash": target_hash,
                "title": matches[0]["title_key"],
                "verified_state": after_match[0]["state"] if after_match else "unknown",
            },
            audit,
        )

    def execute_torrent_pause(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        return self._mutate_torrent("torrent_pause", "/qbittorrent/api/v2/torrents/pause", "pausedDL", args, audit)

    def execute_torrent_resume(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        return self._mutate_torrent("torrent_resume", "/qbittorrent/api/v2/torrents/resume", "downloading", args, audit)

    def execute_request_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = {
            "Authorization": f"Bearer {SANDBOX_AUTH_IDENTIFIERS['requests_token']}",
            "X-Principal-Id": principal.subject,
        }
        q_params: dict[str, Any] = {"title": args["title"]}
        if args.get("year") is not None:
            q_params["year"] = int(args["year"])
        if args.get("media_type") is not None:
            q_params["media_type"] = str(args["media_type"])
        q = urllib.parse.urlencode(q_params)
        code, resp = self._request_json("GET", f"/requests/api/v1/requests?{q}", headers=headers, audit=audit)
        if code == 409 and isinstance(resp, dict) and resp.get("error") == "ambiguous_media_identity":
            return AdapterObservation(
                "clarify_required",
                "requests",
                "request_status",
                409,
                False,
                {"slot": "reference", "reason": "ambiguous_media_identity", "title": args["title"], "candidates": resp.get("candidates", [])},
                audit,
            )
        if code != 200:
            return AdapterObservation("error", "requests", "request_status", code, False, resp, audit)
        return AdapterObservation("verified_ok", "requests", "request_status", 200, True, resp, audit)

    def execute_request_media(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = {
            "Authorization": f"Bearer {SANDBOX_AUTH_IDENTIFIERS['requests_token']}",
            "X-Principal-Id": principal.subject,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        code, resp = self._request_json("POST", "/requests/api/v1/requests", headers=headers, body=args, audit=audit)
        if code not in {200, 201}:
            return AdapterObservation("error", "requests", "request_media", code, False, resp, audit)
        return AdapterObservation("verified_ok", "requests", "request_media", code, True, resp, audit)

    def execute_seer_discover(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        headers = {"Authorization": f"Bearer {SANDBOX_AUTH_IDENTIFIERS['requests_token']}"}
        q = urllib.parse.urlencode({"category": args["category"], "media_type": args["media_type"]})
        code, resp = self._request_json("GET", f"/requests/api/v1/discover?{q}", headers=headers, audit=audit)
        return AdapterObservation("verified_ok" if code == 200 else "error", "seer", "seer_discover", code, code == 200, resp, audit)

    def execute_armiger_parse(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        code, resp = self._request_json("POST", "/extensible/api/v1/armiger/parse", body={"release_name": args["release_name"]}, audit=audit)
        return AdapterObservation("verified_ok" if code == 200 else "error", "armiger", "armiger_parse", code, code == 200, resp, audit)

    def execute_bitmagnet_search(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        q = urllib.parse.urlencode({"query": args["query"]})
        code, resp = self._request_json("GET", f"/extensible/api/v1/bitmagnet/search?{q}", audit=audit)
        return AdapterObservation("verified_ok" if code == 200 else "error", "bitmagnet", "bitmagnet_search", code, code == 200, resp, audit)

    def execute_waypoint_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        code, resp = self._request_json("GET", "/extensible/api/v1/waypoint/status", audit=audit)
        return AdapterObservation("verified_ok" if code == 200 else "error", "dh-waypoint", "waypoint_status", code, code == 200, resp, audit)
