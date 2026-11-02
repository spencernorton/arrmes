"""Native implementations of the released, trained v2 media read actions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
import urllib.parse

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, normalized, validate_call
from .native import (ServiceQueryAdapter, _UpstreamError, _id, _norm, _public_row,
                     _rows, _text, _validated_rows)

_READS = {
    "tv_status": ("sonarr", "media:read"),
    "tv_missing": ("sonarr", "media:read"),
    "movie_status": ("radarr", "media:read"),
    "music_status": ("lidarr", "media:read"),
    "now_playing": ("tautulli", "activity:read"),
    "watch_history": ("tautulli", "activity:read"),
}


def _observation(name: str, status: str, data: dict[str, Any], audit, *, code: int = 200) -> AdapterObservation:
    return AdapterObservation(status, _READS[name][0], name, code, status == "verified_ok",
                              {"source": "native_api", **data}, audit)


def _clarify(name: str, reason: str, audit, **details) -> AdapterObservation:
    return _observation(name, "clarify_required", {"slot": "reference", "reason": reason, **details}, audit, code=409)


def _number(value: Any) -> int:
    if not _id(value, zero=True):
        raise _UpstreamError("invalid_media_statistics")
    return value


def _optional_year(row: dict[str, Any]) -> int | None:
    value = row.get("year")
    if value is None or value == 0 and type(value) is int:
        return None
    if not _id(value):
        raise _UpstreamError("invalid_media_year")
    return value


class NativeMediaReadAdapter:
    """Exact entity reads using operator-configured native API roots.

    Whole unpaginated library responses are scanned inside the byte budget
    before uniqueness is asserted. The returned-record cap never hides a second
    matching entity. No request or media-search mutation is implemented here.
    """

    def __init__(self, native: ServiceQueryAdapter, *, clock: Callable[[], datetime] | None = None) -> None:
        self.native = native
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def supports(self, name: str) -> bool:
        return name in _READS and _READS[name][0] in self.native.configs

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        call = validate_call(call.name, call.arguments, allow_v3=False)
        if call.name not in _READS:
            raise InvalidCall("Native media reads accept only released read actions")
        name, args = call.name, normalized(call)["arguments"]
        service, permission = _READS[name]
        audit: list[dict[str, Any]] = []
        if not principal.subject or permission not in principal.scopes:
            return _observation(name, "denied", {"error": "permission_required"}, audit, code=403)
        if service not in self.native.configs:
            return _observation(name, "error", {"error": "service_not_configured"}, audit, code=503)
        try:
            return getattr(self, "_" + name)(args, audit)
        except _UpstreamError as exc:
            return _observation(name, "error", {"error": exc.code}, audit, code=exc.status)

    def _catalog(self, service: str, audit) -> list[dict[str, Any]]:
        path = {"sonarr": "/api/v3/series", "radarr": "/api/v3/movie", "lidarr": "/api/v1/artist"}[service]
        rows = _rows(self.native._get(service, path, audit))
        _validated_rows(service, "library", rows)
        if len({row["id"] for row in rows}) != len(rows):
            raise _UpstreamError("duplicate_library_identity")
        return rows

    @staticmethod
    def _exact(rows: list[dict[str, Any]], title: str, key: str = "title") -> list[dict[str, Any]]:
        return [row for row in rows if _norm(row[key]) == _norm(title)]

    def _tv_status(self, args, audit) -> AdapterObservation:
        rows = self._exact(self._catalog("sonarr", audit), args["title"])
        if len(rows) != 1:
            return _clarify("tv_status", "series_not_uniquely_resolved", audit)
        row = rows[0]
        stats = row.get("statistics")
        if stats is not None and not isinstance(stats, dict):
            raise _UpstreamError("invalid_series_statistics")
        stats = stats or {}
        count = _number(stats.get("episodeCount") if stats.get("episodeCount") is not None else row.get("episodeCount"))
        files = _number(stats.get("episodeFileCount") if stats.get("episodeFileCount") is not None else row.get("episodeFileCount"))
        monitored = row.get("monitored")
        if monitored is not None and type(monitored) is not bool:
            raise _UpstreamError("invalid_series_monitoring")
        return _observation("tv_status", "verified_ok", {
            "series_id": row["id"], "title": row["title"], "year": _optional_year(row),
            "monitored": monitored, "episode_count": count, "episode_file_count": files,
            "available": files > 0}, audit)

    def _tv_missing(self, args, audit) -> AdapterObservation:
        rows = self._exact(self._catalog("sonarr", audit), args["title"])
        if len(rows) != 1:
            return _clarify("tv_missing", "series_not_uniquely_resolved", audit)
        series = rows[0]
        if type(series.get("monitored")) is not bool:
            raise _UpstreamError("invalid_series_monitoring")
        path = "/api/v3/episode?" + urllib.parse.urlencode({"seriesId": series["id"]})
        episodes = _rows(self.native._get("sonarr", path, audit))
        _validated_rows("sonarr", "missing", episodes)
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _UpstreamError("invalid_utc_clock")
        missing, undated = [], 0
        seen: set[int] = set()
        for episode in episodes:
            if episode["seriesId"] != series["id"] or episode["id"] in seen:
                raise _UpstreamError("inconsistent_series_episode_identity")
            seen.add(episode["id"])
            if type(episode.get("monitored")) is not bool or type(episode.get("hasFile")) is not bool:
                raise _UpstreamError("invalid_episode_file_or_monitoring_state")
            if (episode["hasFile"] or not series["monitored"] or not episode["monitored"]
                    or (episode["seasonNumber"] == 0 and not args["include_specials"])):
                continue
            stamp = episode.get("airDateUtc")
            if stamp is None:
                # Sonarr's aired-only wanted inventory likewise excludes
                # episodes without a scheduled UTC air date. Never call them aired.
                undated += 1
                continue
            if not _text(stamp):
                raise _UpstreamError("invalid_episode_airdate")
            try:
                aired = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                raise _UpstreamError("invalid_episode_airdate") from None
            if aired.tzinfo is None or aired.utcoffset() is None:
                raise _UpstreamError("invalid_episode_airdate")
            if aired.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
                missing.append(episode)
        truncated = len(missing) > self.native.max_records
        selected = missing[:self.native.max_records]
        return _observation("tv_missing", "partial" if truncated else "verified_ok", {
            "series_id": series["id"], "title": series["title"], "year": _optional_year(series),
            "series_monitored": series["monitored"], "include_specials": args["include_specials"],
            "missing_count": len(missing), "missing_episodes": [_public_row(row) for row in selected],
            "returned_count": len(selected), "total_records": len(missing), "truncated": truncated,
            "undated_episodes_excluded": undated, "missing_scope": "known_aired_monitored",
            "as_of_utc": now.astimezone(timezone.utc).isoformat()}, audit)

    def _movie_status(self, args, audit) -> AdapterObservation:
        title_matches = self._exact(self._catalog("radarr", audit), args["title"])
        for row in title_matches:
            _optional_year(row)
        matches = [row for row in title_matches if "year" not in args or row.get("year") == args["year"]]
        if len(matches) != 1:
            return _clarify("movie_status", "movie_not_uniquely_resolved", audit,
                            candidate_years=sorted({_optional_year(row) for row in title_matches if _optional_year(row) is not None}))
        row = matches[0]
        if type(row.get("hasFile")) is not bool:
            raise _UpstreamError("invalid_movie_file_status")
        monitored = row.get("monitored")
        if monitored is not None and type(monitored) is not bool:
            raise _UpstreamError("invalid_movie_monitoring")
        size = row.get("sizeOnDisk")
        if size is None and isinstance(row.get("statistics"), dict):
            size = row["statistics"].get("sizeOnDisk")
        if size is not None:
            size = _number(size)
        return _observation("movie_status", "verified_ok", {"movie_id": row["id"], "title": row["title"],
            "year": _optional_year(row), "has_file": row["hasFile"], "available": row["hasFile"],
            "size_on_disk": size, "monitored": monitored}, audit)

    def _music_status(self, args, audit) -> AdapterObservation:
        artists = self._exact(self._catalog("lidarr", audit), args["artist"], "artistName")
        if len(artists) != 1:
            return _clarify("music_status", "artist_not_uniquely_resolved", audit)
        artist = artists[0]
        albums = _rows(self.native._get("lidarr", "/api/v1/album?" + urllib.parse.urlencode({"artistId": artist["id"]}), audit))
        _validated_rows("lidarr", "missing", albums)
        if len({row["id"] for row in albums}) != len(albums):
            raise _UpstreamError("duplicate_album_identity")
        albums = [row for row in albums if row["artistId"] == artist["id"]]
        if "album" in args:
            albums = self._exact(albums, args["album"])
            if len(albums) != 1:
                return _clarify("music_status", "album_not_uniquely_resolved", audit)
        truncated = len(albums) > self.native.max_records
        return _observation("music_status", "partial" if truncated else "verified_ok", {
            "artist_id": artist["id"], "artist": artist["artistName"],
            "albums": [_public_row(row) for row in albums[:self.native.max_records]],
            "album_count": len(albums), "returned_count": min(len(albums), self.native.max_records),
            "truncated": truncated}, audit)

    def _now_playing(self, args, audit) -> AdapterObservation:
        result = self.native._tautulli({"operation": "activity"}, audit)
        return _observation("now_playing", result.status, {
            "sessions": result.data["items"], "stream_count": result.data["total_records"],
            "returned_count": result.data["returned_count"], "truncated": result.data["truncated"]}, audit)

    def _watch_history(self, args, audit) -> AdapterObservation:
        query = args.get("title")
        if not query:
            result = self.native._tautulli({"operation": "history"}, audit)
            return _observation("watch_history", result.status, {
                "history": result.data["items"], "count": result.data["returned_count"],
                "total_records": result.data["total_records"], "truncated": result.data["truncated"]}, audit)
        rows, total, truncated = self.native._paged(lambda page: self.native._tautulli_data(
            "/api/v2?" + urllib.parse.urlencode({"cmd": "get_history", "start": page * self.native.page_size,
                "length": self.native.page_size, "order_column": "date", "order_dir": "desc", "search": query}),
            audit), records_key="data", total_key="recordsFiltered")
        _validated_rows("tautulli", "history", rows)
        matched = [row for row in rows if any(_text(row.get(key)) and _norm(row[key]) == _norm(query)
                                             for key in ("full_title", "title", "grandparent_title"))]
        return _observation("watch_history", "partial" if truncated else "verified_ok", {
            "title_filter": query, "history": [_public_row(row) for row in matched], "count": len(matched),
            "total_records": None if truncated else len(matched), "upstream_total_records": total,
            "scanned_count": len(rows), "truncated": truncated}, audit)
