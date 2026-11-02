"""Loopback-only resettable HTTP sandbox servers backed by sanitized SQLite snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
from contextlib import closing
import sqlite3
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from arrmes.sandbox.sanitize import (
    SANDBOX_AUTH_IDENTIFIERS,
    build_raw_offline_backup_fixture,
    sanitize_and_restore,
)


class SandboxCluster:
    """Manages an isolated, loopback-only HTTP server and resettable SQLite state for all 9 services."""

    def __init__(self, work_root: Path) -> None:
        self.work_root = Path(work_root)
        self.raw_dir = self.work_root / "raw_offline_backups"
        self.baseline_dir = self.work_root / "sanitized_baseline"
        self.active_dir = self.work_root / "active_state"
        self.restoration_receipt: dict[str, Any] = {}
        self.faults: dict[str, list[int]] = {}
        self.audit_log: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def initialize_snapshots(self) -> dict[str, Any]:
        build_raw_offline_backup_fixture(self.raw_dir)
        self.restoration_receipt = sanitize_and_restore(self.raw_dir, self.baseline_dir)
        self.reset()
        return self.restoration_receipt

    def reset(self) -> None:
        """Fast deterministic state reset (<5ms) from the immutable sanitized baseline."""
        with self._lock:
            if not self.active_dir.exists():
                shutil.copytree(self.baseline_dir, self.active_dir)
            else:
                for src_file in self.baseline_dir.rglob("*"):
                    if src_file.is_file():
                        rel = src_file.relative_to(self.baseline_dir)
                        dst_file = self.active_dir / rel
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst_file)
            self.faults.clear()
            self.audit_log.clear()

    def inject_fault(self, path_prefix: str, status_code: int, count: int = 1) -> None:
        with self._lock:
            self.faults.setdefault(path_prefix, []).extend([status_code] * count)

    def _pop_fault(self, path: str) -> int | None:
        with self._lock:
            for prefix, codes in list(self.faults.items()):
                if path.startswith(prefix) and codes:
                    code = codes.pop(0)
                    if not codes:
                        del self.faults[prefix]
                    return code
        return None

    def start(self) -> "SandboxCluster":
        if not self.baseline_dir.exists():
            self.initialize_snapshots()
        cluster = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(self, code: int, payload: Any) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    return {}
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                with cluster._lock:
                    cluster.audit_log.append({"method": method, "path": path, "query": query})

                    fault_code = cluster._pop_fault(path)
                    if fault_code is not None:
                        self._send(fault_code, {"error": f"injected_fault_{fault_code}", "path": path})
                        return

                    try:
                        if path.startswith("/sonarr/"):
                            self._handle_sonarr(method, path[len("/sonarr"):], query)
                        elif path.startswith("/radarr/"):
                            self._handle_radarr(method, path[len("/radarr"):], query)
                        elif path.startswith("/lidarr/"):
                            self._handle_lidarr(method, path[len("/lidarr"):], query)
                        elif path.startswith("/tautulli/"):
                            self._handle_tautulli(method, path[len("/tautulli"):], query)
                        elif path.startswith("/qbittorrent/"):
                            self._handle_qbittorrent(method, path[len("/qbittorrent"):], query)
                        elif path.startswith("/requests/"):
                            self._handle_requests(method, path[len("/requests"):], query)
                        elif path.startswith("/extensible/"):
                            self._handle_extensible(method, path[len("/extensible"):], query)
                        else:
                            self._send(404, {"error": "unknown_sandbox_service", "path": path})
                    except Exception as exc:
                        self._send(500, {"error": "sandbox_internal_error", "detail": str(exc)})

            def _handle_sonarr(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                if self.headers.get("X-Api-Key") != SANDBOX_AUTH_IDENTIFIERS["sonarr"]:
                    self._send(401, {"error": "unauthorized_sonarr_api_key"})
                    return
                db_path = cluster.active_dir / "sonarr" / "sonarr.db"
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if method == "GET" and subpath == "/api/v3/system/status":
                        self._send(200, {"appName": "Sonarr", "version": "4.0.20.3014", "mode": "sandbox_isolated"})
                    elif method == "GET" and subpath == "/api/v3/series":
                        rows = conn.execute("SELECT * FROM Series ORDER BY Id").fetchall()
                        out = []
                        for r in rows:
                            eps = conn.execute(
                                "SELECT COUNT(*) AS total, SUM(HasFile) AS files FROM Episodes WHERE SeriesId = ? AND SeasonNumber > 0",
                                (r["Id"],),
                            ).fetchone()
                            out.append({
                                "id": r["Id"],
                                "tvdbId": r["TvdbId"],
                                "title": r["Title"],
                                "year": r["Year"],
                                "path": r["Path"],
                                "monitored": bool(r["Monitored"]),
                                "episodeCount": eps["total"] or 0,
                                "episodeFileCount": eps["files"] or 0,
                            })
                        self._send(200, out)
                    elif method == "GET" and subpath == "/api/v3/wanted/missing":
                        inc_spec = query.get("includeSpecials", ["false"])[0].lower() == "true"
                        series_id = query.get("seriesId", [None])[0]
                        page = int(query.get("page", ["1"])[0])
                        page_size = int(query.get("pageSize", ["50"])[0])
                        sql = (
                            "SELECT e.*, s.Title AS SeriesTitle FROM Episodes e "
                            "JOIN Series s ON e.SeriesId = s.Id WHERE e.HasFile = 0"
                        )
                        params: list[Any] = []
                        if not inc_spec:
                            sql += " AND e.SeasonNumber > 0"
                        if series_id is not None:
                            sql += " AND e.SeriesId = ?"
                            params.append(int(series_id))
                        sql += " ORDER BY e.Id"
                        all_rows = conn.execute(sql, params).fetchall()
                        start = (page - 1) * page_size
                        page_rows = all_rows[start : start + page_size]
                        records = [
                            {
                                "id": r["Id"],
                                "seriesId": r["SeriesId"],
                                "seriesTitle": r["SeriesTitle"],
                                "seasonNumber": r["SeasonNumber"],
                                "episodeNumber": r["EpisodeNumber"],
                                "title": r["Title"],
                                "hasFile": False,
                            }
                            for r in page_rows
                        ]
                        self._send(200, {"page": page, "pageSize": page_size, "totalRecords": len(all_rows), "records": records})
                    elif method == "POST" and subpath == "/api/v3/command":
                        body = self._read_json()
                        idem_key = self.headers.get("Idempotency-Key")
                        if idem_key:
                            existing = conn.execute("SELECT * FROM Commands WHERE IdempotencyKey = ?", (idem_key,)).fetchone()
                            if existing:
                                existing_body = json.loads(existing["Body"])
                                if existing_body != body:
                                    self._send(409, {"error": "idempotency_payload_mismatch", "idempotencyKey": idem_key})
                                    return
                                self._send(200, {"id": existing["Id"], "name": existing["Name"], "status": existing["Status"], "idempotentReplay": True})
                                return
                        series_id = body.get("seriesId")
                        if series_id is not None:
                            exists = conn.execute("SELECT 1 FROM Series WHERE Id = ?", (int(series_id),)).fetchone()
                            if not exists:
                                self._send(404, {"error": "series_not_found", "seriesId": series_id})
                                return
                        cur = conn.execute(
                            "INSERT INTO Commands (Name, Body, Status, QueuedAt, IdempotencyKey) VALUES (?, ?, 'queued', ?, ?)",
                            (body.get("name", "Unknown"), json.dumps(body, sort_keys=True), datetime.now(timezone.utc).isoformat(), idem_key),
                        )
                        cmd_id = cur.lastrowid
                        self._send(201, {"id": cmd_id, "name": body.get("name"), "status": "queued", "body": body})
                    elif method == "GET" and subpath.startswith("/api/v3/command/"):
                        cmd_id = int(subpath.rsplit("/", 1)[-1])
                        row = conn.execute("SELECT * FROM Commands WHERE Id = ?", (cmd_id,)).fetchone()
                        if not row:
                            self._send(404, {"error": "command_not_found", "id": cmd_id})
                            return
                        if row["Status"] == "queued":
                            conn.execute("UPDATE Commands SET Status = 'completed' WHERE Id = ?", (cmd_id,))
                            status = "completed"
                        else:
                            status = row["Status"]
                        self._send(200, {"id": row["Id"], "name": row["Name"], "status": status, "body": json.loads(row["Body"])})
                    else:
                        self._send(404, {"error": "sonarr_endpoint_not_found", "subpath": subpath})

            def _handle_radarr(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                if self.headers.get("X-Api-Key") != SANDBOX_AUTH_IDENTIFIERS["radarr"]:
                    self._send(401, {"error": "unauthorized_radarr_api_key"})
                    return
                db_path = cluster.active_dir / "radarr" / "radarr.db"
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if method == "GET" and subpath == "/api/v3/system/status":
                        self._send(200, {"appName": "Radarr", "version": "6.4.4.10685", "mode": "sandbox_isolated"})
                    elif method == "GET" and subpath == "/api/v3/movie":
                        rows = conn.execute("SELECT * FROM Movies ORDER BY Id").fetchall()
                        self._send(
                            200,
                            [
                                {
                                    "id": r["Id"],
                                    "tmdbId": r["TmdbId"],
                                    "title": r["Title"],
                                    "year": r["Year"],
                                    "path": r["Path"],
                                    "monitored": bool(r["Monitored"]),
                                    "hasFile": bool(r["HasFile"]),
                                    "sizeOnDisk": r["SizeOnDisk"],
                                }
                                for r in rows
                            ],
                        )
                    elif method == "POST" and subpath == "/api/v3/command":
                        body = self._read_json()
                        idem_key = self.headers.get("Idempotency-Key")
                        if idem_key:
                            existing = conn.execute("SELECT * FROM Commands WHERE IdempotencyKey = ?", (idem_key,)).fetchone()
                            if existing:
                                if json.loads(existing["Body"]) != body:
                                    self._send(409, {"error": "idempotency_payload_mismatch", "idempotencyKey": idem_key})
                                    return
                                self._send(200, {"id": existing["Id"], "name": existing["Name"], "status": existing["Status"], "idempotentReplay": True})
                                return
                        movie_ids = body.get("movieIds", [])
                        for mid in movie_ids:
                            if not conn.execute("SELECT 1 FROM Movies WHERE Id = ?", (int(mid),)).fetchone():
                                self._send(404, {"error": "movie_not_found", "movieId": mid})
                                return
                        cur = conn.execute(
                            "INSERT INTO Commands (Name, Body, Status, QueuedAt, IdempotencyKey) VALUES (?, ?, 'queued', ?, ?)",
                            (body.get("name", "MoviesSearch"), json.dumps(body, sort_keys=True), datetime.now(timezone.utc).isoformat(), idem_key),
                        )
                        self._send(201, {"id": cur.lastrowid, "name": body.get("name"), "status": "queued", "body": body})
                    elif method == "GET" and subpath.startswith("/api/v3/command/"):
                        cmd_id = int(subpath.rsplit("/", 1)[-1])
                        row = conn.execute("SELECT * FROM Commands WHERE Id = ?", (cmd_id,)).fetchone()
                        if not row:
                            self._send(404, {"error": "command_not_found", "id": cmd_id})
                            return
                        conn.execute("UPDATE Commands SET Status = 'completed' WHERE Id = ?", (cmd_id,))
                        self._send(200, {"id": row["Id"], "name": row["Name"], "status": "completed", "body": json.loads(row["Body"])})
                    else:
                        self._send(404, {"error": "radarr_endpoint_not_found", "subpath": subpath})

            def _handle_lidarr(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                if self.headers.get("X-Api-Key") != SANDBOX_AUTH_IDENTIFIERS["lidarr"]:
                    self._send(401, {"error": "unauthorized_lidarr_api_key"})
                    return
                db_path = cluster.active_dir / "lidarr" / "lidarr.db"
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if method == "GET" and subpath == "/api/v1/artist":
                        rows = conn.execute("SELECT * FROM Artists ORDER BY Id").fetchall()
                        self._send(
                            200,
                            [
                                {
                                    "id": r["Id"],
                                    "foreignArtistId": r["ForeignArtistId"],
                                    "artistName": r["ArtistName"],
                                    "path": r["Path"],
                                    "monitored": bool(r["Monitored"]),
                                }
                                for r in rows
                            ],
                        )
                    elif method == "GET" and subpath == "/api/v1/album":
                        artist_id = query.get("artistId", [None])[0]
                        sql = "SELECT * FROM Albums"
                        params: list[Any] = []
                        if artist_id is not None:
                            sql += " WHERE ArtistId = ?"
                            params.append(int(artist_id))
                        rows = conn.execute(sql + " ORDER BY Id", params).fetchall()
                        self._send(
                            200,
                            [
                                {
                                    "id": r["Id"],
                                    "artistId": r["ArtistId"],
                                    "title": r["Title"],
                                    "releaseYear": r["ReleaseYear"],
                                    "monitored": bool(r["Monitored"]),
                                    "trackCount": r["TrackCount"],
                                    "trackFileCount": r["TrackFileCount"],
                                }
                                for r in rows
                            ],
                        )
                    elif method == "POST" and subpath == "/api/v1/command":
                        body = self._read_json()
                        cur = conn.execute(
                            "INSERT INTO Commands (Name, Body, Status, QueuedAt) VALUES (?, ?, 'completed', ?)",
                            (body.get("name", "ArtistSearch"), json.dumps(body, sort_keys=True), datetime.now(timezone.utc).isoformat()),
                        )
                        self._send(201, {"id": cur.lastrowid, "name": body.get("name"), "status": "completed", "body": body})
                    else:
                        self._send(404, {"error": "lidarr_endpoint_not_found", "subpath": subpath})

            def _handle_tautulli(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                apikey = query.get("apikey", [""])[0]
                if apikey != SANDBOX_AUTH_IDENTIFIERS["tautulli"]:
                    self._send(401, {"response": {"result": "error", "message": "Invalid apikey"}})
                    return
                cmd = query.get("cmd", [""])[0]
                db_path = cluster.active_dir / "tautulli" / "tautulli.db"
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if cmd == "get_activity":
                        rows = conn.execute("SELECT * FROM active_sessions ORDER BY session_key").fetchall()
                        sessions = [dict(r) for r in rows]
                        self._send(
                            200,
                            {
                                "response": {
                                    "result": "success",
                                    "data": {
                                        "stream_count": str(len(sessions)),
                                        "total_bandwidth": sum(r["bandwidth_kbps"] for r in rows),
                                        "sessions": sessions,
                                    },
                                }
                            },
                        )
                    elif cmd == "get_history":
                        search = query.get("search", [""])[0].strip()
                        if search:
                            rows = conn.execute(
                                "SELECT * FROM session_history WHERE LOWER(title) LIKE ? ORDER BY stopped_at DESC",
                                (f"%{search.lower()}%",),
                            ).fetchall()
                        else:
                            rows = conn.execute("SELECT * FROM session_history ORDER BY stopped_at DESC").fetchall()
                        self._send(
                            200,
                            {
                                "response": {
                                    "result": "success",
                                    "data": {"recordsFiltered": len(rows), "data": [dict(r) for r in rows]},
                                }
                            },
                        )
                    else:
                        self._send(400, {"response": {"result": "error", "message": f"Unknown cmd: {cmd}"}})

            def _handle_qbittorrent(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                cookie = self.headers.get("Cookie", "")
                if f"SID={SANDBOX_AUTH_IDENTIFIERS['qbittorrent_sid']}" not in cookie:
                    self._send(403, {"error": "forbidden_missing_qbit_sid"})
                    return
                db_path = cluster.active_dir / "qbittorrent" / "qbittorrent.db"
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if method == "GET" and subpath == "/api/v2/torrents/info":
                        flt = query.get("filter", ["all"])[0]
                        rows = conn.execute("SELECT * FROM torrents ORDER BY hash").fetchall()
                        items = [dict(r) for r in rows]
                        if flt == "downloading":
                            items = [x for x in items if x["state"] == "downloading"]
                        elif flt == "seeding" or flt == "completed":
                            items = [x for x in items if x["state"] == "uploading" or x["progress"] >= 1.0]
                        elif flt == "paused":
                            items = [x for x in items if x["state"].startswith("paused")]
                        elif flt == "stalled":
                            items = [x for x in items if "stalled" in x["state"]]
                        self._send(200, items)
                    elif method == "POST" and subpath in {"/api/v2/torrents/pause", "/api/v2/torrents/resume"}:
                        body = self._read_json()
                        hashes = body.get("hashes", [])
                        if not hashes or "all" in hashes or "*" in hashes:
                            self._send(400, {"error": "wildcard_or_empty_hashes_forbidden"})
                            return
                        new_state = "pausedDL" if subpath.endswith("pause") else "downloading"
                        # Validate every requested torrent target before issuing any UPDATE (atomic batch check)
                        validated_hashes: list[str] = []
                        for h in hashes:
                            row = conn.execute("SELECT * FROM torrents WHERE hash = ?", (h,)).fetchone()
                            if not row:
                                self._send(404, {"error": "torrent_hash_not_found", "hash": h})
                                return
                            if row["protected_tag"] in {"SurgePirate", "private-vip", "cross-seed", "armiger-Protected"} and subpath.endswith("pause"):
                                self._send(403, {"error": "protected_torrent_tag_refused", "hash": h, "tag": row["protected_tag"]})
                                return
                            validated_hashes.append(h)
                        updated = []
                        for h in validated_hashes:
                            conn.execute("UPDATE torrents SET state = ? WHERE hash = ?", (new_state, h))
                            updated.append(h)
                        self._send(200, {"status": "ok", "action": subpath.rsplit("/", 1)[-1], "hashes": updated, "new_state": new_state})
                    else:
                        self._send(404, {"error": "qbit_endpoint_not_found", "subpath": subpath})

            def _handle_requests(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                if self.headers.get("Authorization") != f"Bearer {SANDBOX_AUTH_IDENTIFIERS['requests_token']}":
                    self._send(401, {"error": "unauthorized_requests_bearer"})
                    return
                principal_id = self.headers.get("X-Principal-Id", "").strip()
                db_path = cluster.active_dir / "requests" / "requests.db"
                radarr_db = cluster.active_dir / "radarr" / "radarr.db"
                sonarr_db = cluster.active_dir / "sonarr" / "sonarr.db"

                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if method == "GET" and subpath == "/api/v1/requests":
                        title_q = query.get("title", [""])[0].strip()
                        m_paren_yr = re.match(r"^(.*\S)\s+\((18[7-9]\d|19\d{2}|20\d{2}|21\d{2})\)$", title_q)
                        base_title = m_paren_yr.group(1).strip() if m_paren_yr else title_q
                        paren_year = int(m_paren_yr.group(2)) if m_paren_yr else None

                        year_param = query.get("year", [""])[0].strip()
                        explicit_year: int | None = None
                        if year_param:
                            try:
                                explicit_year = int(year_param)
                            except ValueError:
                                self._send(400, {"error": "invalid_year", "year": year_param})
                                return
                        if paren_year is not None and explicit_year is not None and paren_year != explicit_year:
                            self._send(400, {"error": "conflicting_year_qualifiers", "title": title_q, "year": explicit_year})
                            return
                        req_year = explicit_year if explicit_year is not None else paren_year

                        mtype_param = query.get("media_type", [""])[0].strip().lower()
                        if mtype_param and mtype_param not in {"movie", "tv"}:
                            self._send(400, {"error": "invalid_media_type", "media_type": mtype_param})
                            return
                        req_media_type: str | None = mtype_param or None

                        clean_q = re.sub(r"[^a-z0-9]+", "", base_title.lower())
                        clean_raw = re.sub(r"[^a-z0-9]+", "", title_q.lower())

                        req_sql = "SELECT * FROM requests WHERE clean_title IN (?, ?)"
                        req_params: list[Any] = [clean_q, clean_raw]
                        if req_media_type is not None:
                            req_sql += " AND media_type = ?"
                            req_params.append(req_media_type)
                        if req_year is not None:
                            req_sql += " AND year = ?"
                            req_params.append(req_year)
                        req_rows = conn.execute(req_sql, req_params).fetchall()

                        movie_rows: list[sqlite3.Row] = []
                        if req_media_type in {None, "movie"}:
                            with closing(sqlite3.connect(radarr_db)) as rconn, rconn:
                                rconn.row_factory = sqlite3.Row
                                msql = "SELECT Id, Title, CleanTitle, Year, HasFile FROM Movies WHERE CleanTitle = ?"
                                mparams: list[Any] = [clean_q]
                                if req_year is not None:
                                    msql += " AND Year = ?"
                                    mparams.append(req_year)
                                movie_rows = rconn.execute(msql, mparams).fetchall()

                        series_rows: list[sqlite3.Row] = []
                        if req_media_type in {None, "tv"}:
                            with closing(sqlite3.connect(sonarr_db)) as sconn, sconn:
                                sconn.row_factory = sqlite3.Row
                                ssql = (
                                    "SELECT sr.Id, sr.Title, sr.CleanTitle, sr.Year, "
                                    "SUM(CASE WHEN e.Monitored = 1 THEN 1 ELSE 0 END) AS MonitoredEps, "
                                    "SUM(CASE WHEN e.Monitored = 1 AND e.HasFile = 1 THEN 1 ELSE 0 END) AS MonitoredEpFiles, "
                                    "SUM(CASE WHEN e.HasFile = 1 THEN 1 ELSE 0 END) AS AnyEpFiles "
                                    "FROM Series sr LEFT JOIN Episodes e ON e.SeriesId = sr.Id "
                                    "WHERE sr.CleanTitle = ?"
                                )
                                sparams: list[Any] = [clean_q]
                                if req_year is not None:
                                    ssql += " AND sr.Year = ?"
                                    sparams.append(req_year)
                                ssql += " GROUP BY sr.Id"
                                series_rows = sconn.execute(ssql, sparams).fetchall()

                        identities: set[tuple[str, int | None]] = set()
                        for m in movie_rows:
                            identities.add(("movie", int(m["Year"])))
                        for s in series_rows:
                            identities.add(("tv", int(s["Year"])))
                        for r in req_rows:
                            r_mt = str(r["media_type"])
                            r_yr = int(r["year"]) if r["year"] is not None else None
                            if r_yr is None:
                                same_mt = [yr for mt, yr in identities if mt == r_mt and yr is not None]
                                if len(same_mt) == 1:
                                    r_yr = same_mt[0]
                            identities.add((r_mt, r_yr))

                        if not identities:
                            self._send(404, {"error": "request_not_found", "title": title_q})
                            return
                        if len(identities) > 1:
                            self._send(
                                409,
                                {
                                    "error": "ambiguous_media_identity",
                                    "title": title_q,
                                    "candidates": [
                                        {"media_type": mt, "year": yr}
                                        for mt, yr in sorted(identities, key=lambda x: (x[0], x[1] or 0))
                                    ],
                                },
                            )
                            return

                        resolved_type, resolved_year = next(iter(identities))
                        matched_reqs = [
                            r for r in req_rows
                            if r["media_type"] == resolved_type and (r["year"] is None or int(r["year"]) == resolved_year)
                        ]
                        row = matched_reqs[-1] if matched_reqs else None

                        if resolved_type == "movie":
                            matched_movies = [
                                m for m in movie_rows
                                if resolved_year is None or int(m["Year"]) == resolved_year
                            ]
                            disk_available = any(int(m["HasFile"]) == 1 for m in matched_movies)
                            partial_disk = False
                            canonical_title = matched_movies[0]["Title"] if matched_movies else (row["title"] if row else base_title)
                        elif resolved_type == "tv":
                            matched_series = [
                                s for s in series_rows
                                if resolved_year is None or int(s["Year"]) == resolved_year
                            ]
                            disk_available = any(
                                int(s["MonitoredEps"] or 0) > 0 and int(s["MonitoredEpFiles"] or 0) == int(s["MonitoredEps"] or 0)
                                for s in matched_series
                            )
                            partial_disk = any(int(s["AnyEpFiles"] or 0) > 0 for s in matched_series) and not disk_available
                            canonical_title = matched_series[0]["Title"] if matched_series else (row["title"] if row else base_title)
                        else:
                            disk_available = False
                            partial_disk = False
                            canonical_title = row["title"] if row else base_title

                        if not row and not disk_available and not partial_disk:
                            self._send(404, {"error": "request_not_found", "title": title_q, "media_type": resolved_type, "year": resolved_year})
                            return

                        seer_status = row["seer_status"] if row else "UNREQUESTED"
                        if disk_available:
                            effective_status = "AVAILABLE_ON_DISK"
                            authority = "disk_ownership"
                        elif row:
                            effective_status = seer_status
                            authority = "seer_request_state"
                        else:
                            effective_status = "PARTIALLY_AVAILABLE_ON_DISK"
                            authority = "disk_ownership"

                        self._send(
                            200,
                            {
                                "title": row["title"] if row else canonical_title,
                                "media_type": resolved_type,
                                "year": resolved_year,
                                "seer_status": seer_status,
                                "disk_owned": disk_available,
                                "effective_status": effective_status,
                                "reconciled_authority": authority,
                            },
                        )
                    elif method == "POST" and subpath == "/api/v1/requests":
                        if not principal_id:
                            self._send(401, {"error": "missing_principal_id"})
                            return
                        pmap = conn.execute("SELECT * FROM principal_mappings WHERE principal_id = ?", (principal_id,)).fetchone()
                        if not pmap or pmap["seer_user_id"] is None:
                            self._send(409, {"error": "no_seer_user", "principal_id": principal_id})
                            return
                        if pmap["group_name"] is None:
                            self._send(403, {"error": "not_authorized_null_group", "principal_id": principal_id})
                            return

                        body = self._read_json()
                        raw_title = str(body.get("title", "")).strip()
                        m_post_yr = re.match(r"^(.*\S)\s+\((18[7-9]\d|19\d{2}|20\d{2}|21\d{2})\)$", raw_title)
                        title = m_post_yr.group(1).strip() if m_post_yr else raw_title
                        media_type = body.get("media_type", "movie")
                        year = body.get("year")
                        if year is None and m_post_yr:
                            year = int(m_post_yr.group(2))
                        clean_t = re.sub(r"[^a-z0-9]+", "", title.lower())

                        idem_key = self.headers.get("Idempotency-Key")
                        if idem_key:
                            existing = conn.execute("SELECT * FROM requests WHERE idempotency_key = ?", (idem_key,)).fetchone()
                            if existing:
                                if existing["principal_id"] != principal_id:
                                    self._send(409, {"error": "idempotency_principal_mismatch", "idempotencyKey": idem_key})
                                    return
                                if (
                                    existing["clean_title"] != clean_t
                                    or existing["media_type"] != media_type
                                    or existing["year"] != year
                                ):
                                    self._send(409, {"error": "idempotency_payload_mismatch", "idempotencyKey": idem_key})
                                    return
                                self._send(
                                    200,
                                    {
                                        "id": existing["id"],
                                        "title": existing["title"],
                                        "principal_id": existing["principal_id"],
                                        "seer_user_id": existing["seer_user_id"],
                                        "status": existing["seer_status"],
                                        "idempotentReplay": True,
                                    },
                                )
                                return

                        if pmap["quota_remaining"] <= 0:
                            self._send(429, {"error": "quota_exceeded", "principal_id": principal_id})
                            return

                        # Disk ownership outranks Seer: check if already available on disk before filing a Seer request
                        if media_type == "movie":
                            with closing(sqlite3.connect(radarr_db)) as rconn, rconn:
                                sql = "SELECT Id, Year, HasFile FROM Movies WHERE CleanTitle = ?"
                                params: list[Any] = [clean_t]
                                if year is not None:
                                    sql += " AND Year = ?"
                                    params.append(int(year))
                                matches = rconn.execute(sql, params).fetchall()
                                if len(matches) == 1 and matches[0][2] == 1:
                                    self._send(
                                        200,
                                        {
                                            "status": "already_available_on_disk",
                                            "title": title,
                                            "year": matches[0][1],
                                            "seer_request_created": False,
                                            "authority": "radarr_disk_ownership",
                                        },
                                    )
                                    return

                        conn.execute(
                            "UPDATE principal_mappings SET quota_remaining = quota_remaining - 1 WHERE principal_id = ?",
                            (principal_id,),
                        )
                        cur = conn.execute(
                            "INSERT INTO requests (principal_id, seer_user_id, title, clean_title, media_type, year, seer_status, disk_owned, idempotency_key) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)",
                            (principal_id, pmap["seer_user_id"], title, clean_t, media_type, year, idem_key),
                        )
                        self._send(
                            201,
                            {
                                "id": cur.lastrowid,
                                "principal_id": principal_id,
                                "seer_user_id": pmap["seer_user_id"],
                                "title": title,
                                "media_type": media_type,
                                "year": year,
                                "status": "PENDING",
                                "seer_request_created": True,
                            },
                        )
                    elif method == "GET" and subpath == "/api/v1/discover":
                        cat = query.get("category", ["trending"])[0]
                        mtype = query.get("media_type", ["movie"])[0]
                        rows = conn.execute(
                            "SELECT * FROM seer_recommendations WHERE category = ? AND media_type = ? ORDER BY id",
                            (cat, mtype),
                        ).fetchall()
                        self._send(200, {"category": cat, "media_type": mtype, "results": [dict(r) for r in rows]})
                    else:
                        self._send(404, {"error": "requests_endpoint_not_found", "subpath": subpath})

            def _handle_extensible(self, method: str, subpath: str, query: dict[str, list[str]]) -> None:
                db_path = cluster.active_dir / "extensible" / "extensible.db"
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    if method == "POST" and subpath == "/api/v1/armiger/parse":
                        body = self._read_json()
                        rel = body.get("release_name", "")
                        res_match = re.search(r"\b(2160p|1080p|720p|480p)\b", rel, re.IGNORECASE)
                        yr_match = re.search(r"\b(19\d{2}|20\d{2})\b", rel)
                        ep_match = re.search(r"\bS(\d{2})E(\d{2})\b", rel, re.IGNORECASE)
                        self._send(
                            200,
                            {
                                "release_name": rel,
                                "resolution": res_match.group(1).lower() if res_match else "unknown",
                                "year": int(yr_match.group(1)) if yr_match else None,
                                "media_kind": "episode" if ep_match else "movie",
                            },
                        )
                    elif method == "GET" and subpath == "/api/v1/bitmagnet/search":
                        q = query.get("query", [""])[0].strip()
                        token = q.split()[0].lower() if q else ""
                        rows = conn.execute(
                            "SELECT * FROM bitmagnet_torrents WHERE LOWER(title) LIKE ? ORDER BY seeders DESC",
                            (f"%{token}%",),
                        ).fetchall()
                        self._send(200, {"query": q, "results": [dict(r) for r in rows]})
                    elif method == "GET" and subpath == "/api/v1/waypoint/status":
                        rows = conn.execute("SELECT * FROM waypoint_state").fetchall()
                        self._send(200, {"pipelines": [dict(r) for r in rows]})
                    else:
                        self._send(404, {"error": "extensible_endpoint_not_found", "subpath": subpath})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "SandboxCluster":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
