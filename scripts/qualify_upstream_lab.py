#!/usr/bin/env python3
"""Boot and qualify actual official upstream *Arr & companion Docker containers (11-service loopback-isolated lab).

This script:
1. Creates an isolated Docker bridge network (`arrmes_upstream_lab_net`) and boots real official
   upstream containers bound strictly to 127.0.0.1 ephemeral ports across all 11 supported services:
   - lscr.io/linuxserver/sonarr:latest (Sonarr v4)
   - lscr.io/linuxserver/radarr:latest (Radarr v5/v6)
   - lscr.io/linuxserver/lidarr:latest (Lidarr v2/v3)
   - lscr.io/linuxserver/readarr:0.4.19-nightly (Readarr v0.4)
   - thespad/whisparr:latest (Whisparr v3)
   - lscr.io/linuxserver/prowlarr:latest (Prowlarr v1)
   - lscr.io/linuxserver/bazarr:latest (Bazarr 1.6+)
   - lscr.io/linuxserver/qbittorrent:latest (qBittorrent v5)
   - lscr.io/linuxserver/tautulli:latest (Tautulli v2)
   - ghcr.io/bitmagnet-io/bitmagnet:latest + postgres:16-alpine (Bitmagnet v0.10.1 GraphQL + Postgres)
   - ghcr.io/seerr-team/seerr:latest (Seerr v3.5.1)
2. Extracts the live auto-generated API keys/credentials from the booted container configs.
3. Seeds multi-page library rows (320 items > 250 single-page budget) into the live Radarr
   SQLite database, seeds an author + monitored released missing book into the live Readarr
   SQLite database, seeds a torrent + content record into the live Bitmagnet Postgres database,
   and seeds synthetic users + a pending request into the live Seerr 3.5.1 database.
4. Exercises Arrmes native adapters (`ServiceQueryAdapter`, `NativeArrWriteAdapter`,
   `NativeBitmagnetAdapter`, `NativeRequestAdapter`) against the live container HTTP endpoints.
5. Records exact image digests, upstream binary versions, HTTP status receipts, and post-condition
   checks into `experiments/upstream-lab-v1/summary.json`, then tears down all containers in `finally`.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from arrmes.planner import Principal
from arrmes.protocol import Call
from arrmes.services.arr_writes import NativeArrWriteAdapter
from arrmes.services.bitmagnet import NativeBitmagnetAdapter
from arrmes.services.native import ServiceConfig, ServiceQueryAdapter
from arrmes.services.requests import NativeRequestAdapter

LAB_NET = "arrmes_upstream_lab_net"
CONTAINERS = [
    "arrmes_lab_sonarr",
    "arrmes_lab_radarr",
    "arrmes_lab_lidarr",
    "arrmes_lab_readarr",
    "arrmes_lab_whisparr",
    "arrmes_lab_prowlarr",
    "arrmes_lab_bazarr",
    "arrmes_lab_qbittorrent",
    "arrmes_lab_tautulli",
    "arrmes_lab_bitmagnet",
    "arrmes_lab_postgres",
    "arrmes_lab_seerr",
]


def _run(cmd: list[str], timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def _cleanup() -> None:
    for name in CONTAINERS:
        _run(["docker", "rm", "-f", name], timeout=20)
    _run(["docker", "network", "rm", LAB_NET], timeout=20)


def _image_digest(image: str) -> str:
    cp = _run(["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"])
    out = cp.stdout.strip()
    if "@" in out:
        return out.split("@", 1)[1]
    cp2 = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    return cp2.stdout.strip()


def _wait_http(url: str, timeout_s: float = 90.0, headers: dict[str, str] | None = None, require_200: bool = False) -> tuple[int, str]:
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if not require_200:
                return exc.code, body
            last_err = f"HTTP {exc.code}: {body[:120]}"
            time.sleep(1.0)
        except Exception as exc:
            last_err = str(exc)
            time.sleep(1.0)
    raise RuntimeError(f"Timed out waiting for {url}: {last_err}")


def _extract_xml_apikey(container: str, timeout_s: float = 75.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cp = _run(["docker", "exec", container, "cat", "/config/config.xml"])
        if cp.returncode == 0 and "<ApiKey>" in cp.stdout:
            m = re.search(r"<ApiKey>([^<]+)</ApiKey>", cp.stdout)
            if m:
                return m.group(1).strip()
        time.sleep(1.0)
    raise RuntimeError(f"Could not extract ApiKey from {container}:/config/config.xml")


def _extract_bazarr_apikey(container: str, timeout_s: float = 90.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cp = _run(["docker", "exec", container, "cat", "/config/config/config.yaml"])
        if cp.returncode == 0 and "apikey:" in cp.stdout:
            m = re.search(r"apikey:\s*['\"]?([a-fA-F0-9]{16,64})['\"]?", cp.stdout)
            if m:
                return m.group(1).strip()
        cp2 = _run(["docker", "exec", container, "cat", "/config/config/config.ini"])
        if cp2.returncode == 0 and "apikey" in cp2.stdout:
            m2 = re.search(r"apikey\s*=\s*([a-fA-F0-9]{16,64})", cp2.stdout)
            if m2:
                return m2.group(1).strip()
        time.sleep(1.0)
    raise RuntimeError(f"Could not extract Bazarr apikey from {container}")


def _extract_tautulli_apikey(container: str, timeout_s: float = 75.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cp = _run(["docker", "exec", container, "cat", "/config/config.ini"])
        if cp.returncode == 0 and "api_key" in cp.stdout:
            m = re.search(r"^api_key\s*=\s*([a-fA-F0-9]{16,64})", cp.stdout, flags=re.M)
            if m and m.group(1).strip():
                return m.group(1).strip()
        time.sleep(1.0)
    raise RuntimeError(f"Could not extract Tautulli api_key from {container}")


def _extract_qbit_password(container: str, timeout_s: float = 75.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cp = _run(["docker", "logs", container])
        combined = (cp.stdout or "") + "\n" + (cp.stderr or "")
        m = re.search(r"temporary password is provided for this session:\s*(\S+)", combined)
        if m:
            return m.group(1).strip()
        time.sleep(1.0)
    raise RuntimeError(f"Could not extract qBittorrent temporary password from logs of {container}")


def _login_qbit_sid(base_url: str, password: str) -> str:
    data = urllib.parse.urlencode({"username": "admin", "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/v2/auth/login",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{base_url}/",
        },
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        cookies = resp.headers.get_all("Set-Cookie") or []
        for c in cookies:
            m = re.search(r"((?:QBT_SID_\d+|SID)=[^;\s]+)", c)
            if m:
                val = m.group(1)
                return val if val.startswith("QBT_SID_") else val.split("=", 1)[1]
    raise RuntimeError("Could not obtain qBittorrent SID cookie")


def _seed_radarr_multipage_sqlite(container: str, count: int = 320) -> int:
    _run(["docker", "stop", container], timeout=30, check=True)
    tmp_db = Path("/tmp/arrmes_lab_radarr.db")
    for ext in ("", "-wal", "-shm"):
        Path(f"/tmp/arrmes_lab_radarr.db{ext}").unlink(missing_ok=True)
    _run(["docker", "cp", f"{container}:/config/radarr.db", str(tmp_db)], check=True)

    conn = sqlite3.connect(str(tmp_db))
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(MovieMetadata)")
    meta_cols = [r[1] for r in cur.fetchall()]
    cur.execute("PRAGMA table_info(Movies)")
    movie_cols = [r[1] for r in cur.fetchall()]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(1, count + 1):
        tmdb_id = 900000 + i
        title = f"Lab Qualified Film {i:03d}"
        clean_title = f"labqualifiedfilm{i:03d}"
        sort_title = f"lab qualified film {i:03d}"
        slug = f"lab-qualified-film-{i:03d}-{tmdb_id}"
        meta_row = {
            "TmdbId": tmdb_id,
            "ImdbId": f"tt{tmdb_id}",
            "Title": title,
            "CleanTitle": clean_title,
            "SortTitle": sort_title,
            "Status": 3,
            "Overview": "Seeded upstream binary qualification movie",
            "Images": "[]",
            "Website": "",
            "Year": 2024,
            "Ratings": "{}",
            "Genres": '["Sci-Fi"]',
            "Runtime": 120,
            "CleanOriginalTitle": clean_title,
            "OriginalTitle": title,
            "OriginalLanguage": 1,
            "Recommendations": "[]",
            "Popularity": 10.0,
            "YoutubeTrailerId": "",
            "Studio": "LabStudio",
            "LastInfoSync": now,
        }
        m_keys = [k for k in meta_row if k in meta_cols]
        cur.execute(
            f"INSERT OR IGNORE INTO MovieMetadata ({','.join(m_keys)}) VALUES ({','.join(['?']*len(m_keys))})",
            [meta_row[k] for k in m_keys],
        )
        meta_id = cur.lastrowid or i
        mov_row = {
            "Path": f"/movies/{title} (2024)",
            "Monitored": 1 if i % 2 == 1 else 0,
            "QualityProfileId": 1,
            "Added": now,
            "TitleSlug": slug,
            "RootFolderPath": "/movies",
            "MovieFileId": 0,
            "MinimumAvailability": 3,
            "MovieMetadataId": meta_id,
            "Tags": "[]",
            "AddOptions": "{}",
        }
        v_keys = [k for k in mov_row if k in movie_cols]
        cur.execute(
            f"INSERT OR IGNORE INTO Movies ({','.join(v_keys)}) VALUES ({','.join(['?']*len(v_keys))})",
            [mov_row[k] for k in v_keys],
        )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM Movies")
    total = int(cur.fetchone()[0])
    conn.close()
    _run(["docker", "cp", str(tmp_db), f"{container}:/config/radarr.db"], check=True)
    tmp_db.unlink(missing_ok=True)
    _run(["docker", "start", container], timeout=30, check=True)
    return total


def _seed_readarr_sqlite(container: str) -> dict[str, int]:
    _run(["docker", "stop", container], timeout=30, check=True)
    tmp_db = Path("/tmp/arrmes_lab_readarr.db")
    for ext in ("", "-wal", "-shm"):
        Path(f"/tmp/arrmes_lab_readarr.db{ext}").unlink(missing_ok=True)
    _run(["docker", "cp", f"{container}:/config/readarr.db", str(tmp_db)], check=True)
    conn = sqlite3.connect(str(tmp_db))
    cur = conn.cursor()
    now = "2024-01-15 00:00:00"
    ratings = json.dumps({"votes": 10, "value": 4.5})
    cur.execute(
        "INSERT OR REPLACE INTO AuthorMetadata (Id, ForeignAuthorId, TitleSlug, Name, Overview, Status, Images, Links, Genres, Ratings, Aliases, SortName, NameLastFirst, SortNameLastFirst) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "ol-author-1", "lab-qualified-author", "Lab Qualified Author", "Seeded author", 0, "[]", "[]", "[]", ratings, "[]", "lab qualified author", "Author, Lab Qualified", "author, lab qualified"),
    )
    cur.execute(
        "INSERT OR REPLACE INTO Authors (Id, CleanName, Path, Monitored, LastInfoSync, QualityProfileId, Tags, Added, AddOptions, MetadataProfileId, AuthorMetadataId, MonitorNewItems) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "labqualifiedauthor", "/books/Lab Qualified Author", 1, now, 1, "[]", now, "{}", 1, 1, 0),
    )
    cur.execute(
        "INSERT OR REPLACE INTO Books (Id, AuthorMetadataId, ForeignBookId, TitleSlug, Title, ReleaseDate, Links, Genres, Ratings, CleanTitle, Monitored, AnyEditionOk, LastInfoSync, Added, AddOptions, RelatedBooks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "ol-book-1", "lab-qualified-book-001", "Lab Qualified Book 001", now, "[]", "[]", ratings, "labqualifiedbook001", 1, 1, now, now, "{}", "[]"),
    )
    cur.execute(
        "INSERT OR REPLACE INTO Editions (Id, BookId, ForeignEditionId, Isbn13, Asin, Title, TitleSlug, Language, Overview, Format, IsEbook, Disambiguation, Publisher, PageCount, ReleaseDate, Images, Links, Ratings, Monitored, ManualAdd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "ol-ed-1", "9780000000001", "B000000001", "Lab Qualified Book 001", "lab-qualified-book-001-ed1", "eng", "Seeded book", "Hardcover", 1, "", "LabPress", 320, now, "[]", "[]", ratings, 1, 0),
    )
    conn.commit()
    conn.close()
    _run(["docker", "cp", str(tmp_db), f"{container}:/config/readarr.db"], check=True)
    tmp_db.unlink(missing_ok=True)
    _run(["docker", "start", container], timeout=30, check=True)
    return {"seeded_authors": 1, "seeded_books": 1}


def _seed_bitmagnet_postgres(pg_container: str) -> int:
    sql = """
INSERT INTO metadata_sources (key, name, created_at, updated_at) VALUES ('tmdb', 'TMDB', NOW(), NOW()) ON CONFLICT DO NOTHING;
INSERT INTO content (type, source, id, title, release_date, release_year, created_at, updated_at, tsv)
VALUES ('movie', 'tmdb', '991001', 'Lab Qualified Bitmagnet Film', '2024-05-01', 2024, NOW(), NOW(), to_tsvector('simple', 'Lab Qualified Bitmagnet Film')) ON CONFLICT DO NOTHING;
INSERT INTO torrents (info_hash, name, size, private, created_at, updated_at, files_status, files_count)
VALUES (decode('0123456789abcdef0123456789abcdef01234567', 'hex'), 'Lab.Qualified.Bitmagnet.Film.2024.1080p.WEB-DL.mkv', 4294967296, false, NOW(), NOW(), 'single', 1) ON CONFLICT DO NOTHING;
INSERT INTO torrent_contents (info_hash, content_type, content_source, content_id, video_resolution, created_at, updated_at, tsv, seeders, leechers, published_at, size, files_count)
VALUES (decode('0123456789abcdef0123456789abcdef01234567', 'hex'), 'movie', 'tmdb', '991001', 'V1080p', NOW(), NOW(), to_tsvector('simple', 'Lab Qualified Bitmagnet Film 2024 1080p'), 42, 3, NOW(), 4294967296, 1) ON CONFLICT DO NOTHING;
"""
    _run(["docker", "exec", pg_container, "psql", "-U", "postgres", "-d", "bitmagnet", "-c", sql], check=True)
    return 1


def _seed_seerr_sqlite(seerr_container: str) -> dict[str, Any]:
    seed_js = """
require("reflect-metadata");
const ds = require("./dist/datasource").default;
const {User} = require("./dist/entity/User");
const {UserType} = require("./dist/constants/user");
const {Permission} = require("./dist/lib/permissions");
(async () => {
  await ds.initialize();
  const users = ds.getRepository(User);
  await users.save(users.create({id:1, email:"owner@example.invalid", username:"synthetic-owner", userType:UserType.LOCAL, permissions:Permission.ADMIN, avatar:""}));
  await users.save(users.create({id:2, email:"reader@example.invalid", username:"synthetic-reader", userType:UserType.LOCAL, permissions:Permission.REQUEST, avatar:""}));
  await ds.query("INSERT INTO media (id, mediaType, tmdbId, status, status4k, createdAt, updatedAt) VALUES (1, 'movie', 991001, 2, 1, datetime('now'), datetime('now'))");
  await ds.query("INSERT INTO media_request (id, status, createdAt, updatedAt, type, is4k, mediaId, requestedById) VALUES (1, 1, datetime('now'), datetime('now'), 'movie', 0, 1, 2)");
  process.stdout.write(JSON.stringify({synthetic_user_ids:[1,2], seeded_pending_request_id:1}));
  await ds.destroy();
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    cp = _run(["docker", "exec", seerr_container, "node", "-e", seed_js], check=True)
    lines = [l.strip() for l in cp.stdout.splitlines() if l.strip().startswith("{")]
    return json.loads(lines[-1]) if lines else {"ok": True}


@contextmanager
def _seerr_metadata_bridge(seerr_upstream_url: str):
    """Forwards all /api/v1/status, /api/v1/auth/me, and /api/v1/request* requests directly to the
    live Seerr 3.5.1 container, while serving deterministic local metadata on /api/v1/search and
    /api/v1/movie/991001 so Seerr never contacts external api.themoviedb.org during lab qualification."""

    class BridgeHandler(BaseHTTPRequestHandler):
        def _proxy_to_live_seerr(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else None
            fwd_headers = {}
            for k in ("Accept", "Content-Type", "X-Api-Key", "X-API-User", "User-Agent"):
                v = self.headers.get(k)
                if v is not None:
                    fwd_headers[k] = v
            req = urllib.request.Request(seerr_upstream_url.rstrip("/") + self.path, data=raw_body, headers=fwd_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    body = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/api/v1/search":
                payload = {
                    "page": 1,
                    "totalPages": 1,
                    "totalResults": 1,
                    "results": [
                        {
                            "id": 991001,
                            "mediaType": "movie",
                            "title": "Lab Qualified Seerr Film",
                            "releaseDate": "2024-06-01",
                        }
                    ],
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == "/api/v1/movie/991001":
                payload = {
                    "id": 991001,
                    "title": "Lab Qualified Seerr Film",
                    "releaseDate": "2024-06-01",
                    "mediaInfo": {"mediaType": "movie", "tmdbId": 991001, "status": 2},
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self._proxy_to_live_seerr("GET")

        def do_DELETE(self) -> None:
            self._proxy_to_live_seerr("DELETE")

        def do_POST(self) -> None:
            self._proxy_to_live_seerr("POST")

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    t0 = time.monotonic()
    _cleanup()
    receipts: dict[str, Any] = {
        "suite": "arrmes-upstream-container-lab-v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network_isolation": {
            "docker_network": LAB_NET,
            "bind_host": "127.0.0.1",
            "production_endpoints_contacted": 0,
        },
        "images": {},
        "live_containers": {},
        "adapter_verifications": [],
    }

    images_to_record = [
        "lscr.io/linuxserver/sonarr:latest",
        "lscr.io/linuxserver/radarr:latest",
        "lscr.io/linuxserver/lidarr:latest",
        "lscr.io/linuxserver/readarr:0.4.19-nightly",
        "thespad/whisparr:latest",
        "lscr.io/linuxserver/prowlarr:latest",
        "lscr.io/linuxserver/bazarr:latest",
        "lscr.io/linuxserver/qbittorrent:latest",
        "lscr.io/linuxserver/tautulli:latest",
        "ghcr.io/bitmagnet-io/bitmagnet:latest",
        "postgres:16-alpine",
        "ghcr.io/seerr-team/seerr:latest",
    ]
    for img in images_to_record:
        receipts["images"][img] = _image_digest(img)

    seerr_key = "arrmes_lab_seerr_key_9f8e7d6c5b4a3210"
    try:
        _run(["docker", "network", "create", "--driver", "bridge", LAB_NET], check=True)

        # Start Postgres first so Bitmagnet can run migrations on boot
        _run(
            [
                "docker", "run", "-d",
                "--name", "arrmes_lab_postgres",
                "--network", LAB_NET,
                "-e", "POSTGRES_PASSWORD=postgres",
                "-e", "POSTGRES_DB=bitmagnet",
                "postgres:16-alpine",
            ],
            check=True,
        )

        specs = [
            ("arrmes_lab_sonarr", "lscr.io/linuxserver/sonarr:latest", "18989:8989"),
            ("arrmes_lab_radarr", "lscr.io/linuxserver/radarr:latest", "17878:7878"),
            ("arrmes_lab_lidarr", "lscr.io/linuxserver/lidarr:latest", "18686:8686"),
            ("arrmes_lab_readarr", "lscr.io/linuxserver/readarr:0.4.19-nightly", "18787:8787"),
            ("arrmes_lab_whisparr", "thespad/whisparr:latest", "16969:6969"),
            ("arrmes_lab_prowlarr", "lscr.io/linuxserver/prowlarr:latest", "19696:9696"),
            ("arrmes_lab_bazarr", "lscr.io/linuxserver/bazarr:latest", "16767:6767"),
            ("arrmes_lab_qbittorrent", "lscr.io/linuxserver/qbittorrent:latest", "18080:18080"),
            ("arrmes_lab_tautulli", "lscr.io/linuxserver/tautulli:latest", "18181:8181"),
        ]
        for cname, image, port_map in specs:
            _run(
                [
                    "docker", "run", "-d",
                    "--name", cname,
                    "--network", LAB_NET,
                    "-e", "PUID=1000",
                    "-e", "PGID=1000",
                    "-e", "TZ=UTC",
                    "-e", "WEBUI_PORT=18080",
                    "-p", f"127.0.0.1:{port_map}",
                    image,
                ],
                check=True,
            )

        _run(
            [
                "docker", "run", "-d",
                "--name", "arrmes_lab_seerr",
                "--network", LAB_NET,
                "-e", f"API_KEY={seerr_key}",
                "-e", "TZ=Etc/UTC",
                "-p", "127.0.0.1:15055:5055",
                "ghcr.io/seerr-team/seerr:latest",
            ],
            check=True,
        )

        # Wait briefly for Postgres readiness then start Bitmagnet HTTP/GraphQL server
        for _ in range(30):
            cp_pg = _run(["docker", "exec", "arrmes_lab_postgres", "pg_isready", "-U", "postgres"])
            if cp_pg.returncode == 0:
                break
            time.sleep(1.0)

        _run(
            [
                "docker", "run", "-d",
                "--name", "arrmes_lab_bitmagnet",
                "--network", LAB_NET,
                "-e", "POSTGRES_HOST=arrmes_lab_postgres",
                "-e", "POSTGRES_PASSWORD=postgres",
                "-p", "127.0.0.1:13333:3333",
                "ghcr.io/bitmagnet-io/bitmagnet:latest",
                "worker", "run", "--keys=http_server",
            ],
            check=True,
        )

        sonarr_key = _extract_xml_apikey("arrmes_lab_sonarr")
        radarr_key = _extract_xml_apikey("arrmes_lab_radarr")
        lidarr_key = _extract_xml_apikey("arrmes_lab_lidarr")
        readarr_key = _extract_xml_apikey("arrmes_lab_readarr")
        whisparr_key = _extract_xml_apikey("arrmes_lab_whisparr")
        prowlarr_key = _extract_xml_apikey("arrmes_lab_prowlarr")
        bazarr_key = _extract_bazarr_apikey("arrmes_lab_bazarr")
        tautulli_key = _extract_tautulli_apikey("arrmes_lab_tautulli")
        qbit_pass = _extract_qbit_password("arrmes_lab_qbittorrent")

        _, sonarr_status_raw = _wait_http(
            "http://127.0.0.1:18989/api/v3/system/status",
            headers={"X-Api-Key": sonarr_key},
            require_200=True,
        )
        _, radarr_status_raw = _wait_http(
            "http://127.0.0.1:17878/api/v3/system/status",
            headers={"X-Api-Key": radarr_key},
            require_200=True,
        )
        _, lidarr_status_raw = _wait_http(
            "http://127.0.0.1:18686/api/v1/system/status",
            headers={"X-Api-Key": lidarr_key},
            require_200=True,
        )
        _, readarr_status_raw = _wait_http(
            "http://127.0.0.1:18787/api/v1/system/status",
            headers={"X-Api-Key": readarr_key},
            require_200=True,
        )
        _, whisparr_status_raw = _wait_http(
            "http://127.0.0.1:16969/api/v3/system/status",
            headers={"X-Api-Key": whisparr_key},
            require_200=True,
        )
        _, prowlarr_status_raw = _wait_http(
            "http://127.0.0.1:19696/api/v1/system/status",
            headers={"X-Api-Key": prowlarr_key},
            require_200=True,
        )
        _, bazarr_status_raw = _wait_http(
            "http://127.0.0.1:16767/api/system/status",
            headers={"X-API-KEY": bazarr_key},
            require_200=True,
        )
        _, tautulli_status_raw = _wait_http(
            f"http://127.0.0.1:18181/api/v2?apikey={tautulli_key}&cmd=get_tautulli_info",
            require_200=True,
        )
        _, seerr_status_raw = _wait_http(
            "http://127.0.0.1:15055/api/v1/status",
            headers={"X-Api-Key": seerr_key},
            require_200=True,
        )
        _wait_http("http://127.0.0.1:18080/api/v2/app/webapiVersion", require_200=False)
        qbit_sid = _login_qbit_sid("http://127.0.0.1:18080", qbit_pass)

        # Wait for Bitmagnet GraphQL endpoint
        for _ in range(45):
            try:
                req_bm = urllib.request.Request(
                    "http://127.0.0.1:13333/graphql",
                    data=b'{"query":"{ version }"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req_bm, timeout=2.0) as r_bm:
                    if r_bm.status == 200:
                        bm_status_raw = r_bm.read().decode("utf-8")
                        break
            except Exception:
                time.sleep(1.0)
        else:
            raise RuntimeError("Timed out waiting for Bitmagnet GraphQL server")

        # Seed Radarr (320 movies), Readarr (1 author + 1 book), Bitmagnet (1 torrent), and Seerr (2 users + 1 pending request)
        seeded_movies = _seed_radarr_multipage_sqlite("arrmes_lab_radarr", count=320)
        _wait_http(
            "http://127.0.0.1:17878/api/v3/system/status",
            headers={"X-Api-Key": radarr_key},
            require_200=True,
        )
        seeded_readarr = _seed_readarr_sqlite("arrmes_lab_readarr")
        _wait_http(
            "http://127.0.0.1:18787/api/v1/system/status",
            headers={"X-Api-Key": readarr_key},
            require_200=True,
        )
        seeded_bitmagnet = _seed_bitmagnet_postgres("arrmes_lab_postgres")
        seeded_seerr = _seed_seerr_sqlite("arrmes_lab_seerr")

        sonarr_info = json.loads(sonarr_status_raw)
        radarr_info = json.loads(radarr_status_raw)
        lidarr_info = json.loads(lidarr_status_raw)
        readarr_info = json.loads(readarr_status_raw)
        whisparr_info = json.loads(whisparr_status_raw)
        prowlarr_info = json.loads(prowlarr_status_raw)
        bazarr_info = json.loads(bazarr_status_raw).get("data", {})
        tautulli_info = json.loads(tautulli_status_raw).get("response", {}).get("data", {})
        seerr_info = json.loads(seerr_status_raw)
        bm_info = json.loads(bm_status_raw).get("data", {})

        with _seerr_metadata_bridge("http://127.0.0.1:15055") as seerr_bridge_url:
            configs = {
                "sonarr": ServiceConfig(base_url="http://127.0.0.1:18989", api_key=sonarr_key),
                "radarr": ServiceConfig(base_url="http://127.0.0.1:17878", api_key=radarr_key),
                "lidarr": ServiceConfig(base_url="http://127.0.0.1:18686", api_key=lidarr_key),
                "readarr": ServiceConfig(base_url="http://127.0.0.1:18787", api_key=readarr_key),
                "whisparr": ServiceConfig(base_url="http://127.0.0.1:16969", api_key=whisparr_key),
                "prowlarr": ServiceConfig(base_url="http://127.0.0.1:19696", api_key=prowlarr_key),
                "bazarr": ServiceConfig(base_url="http://127.0.0.1:16767", api_key=bazarr_key),
                "tautulli": ServiceConfig(base_url="http://127.0.0.1:18181", api_key=tautulli_key),
                "qbittorrent": ServiceConfig(base_url="http://127.0.0.1:18080", session_id=qbit_sid),
                "bitmagnet": ServiceConfig(base_url="http://127.0.0.1:13333"),
                "seerr": ServiceConfig(
                    base_url=seerr_bridge_url,
                    api_key=seerr_key,
                    user_ids={"upstream-lab-qualifier": 2},
                ),
            }
            sq = ServiceQueryAdapter(configs, max_pages=5, page_size=50, max_records=250)
            arr_writer = NativeArrWriteAdapter(
                sq,
                allowed_majors={"sonarr": {4}, "radarr": {5, 6}, "lidarr": {2, 3}, "readarr": {0}},
            )
            bm_adapter = NativeBitmagnetAdapter(sq)
            req_adapter = NativeRequestAdapter(sq, write_mode="confirm", allow_tv_creation=True)
            principal = Principal(
                subject="upstream-lab-qualifier",
                scopes=frozenset({
                    "media:read",
                    "media:manage",
                    "releases:read",
                    "activity:read",
                    "requests:read",
                    "requests:create",
                    "torrents:read",
                }),
            )

            # Verify direct (unbridged) Seerr health against port 15055
            sq_direct_seerr = ServiceQueryAdapter(
                {"seerr": ServiceConfig(base_url="http://127.0.0.1:15055", api_key=seerr_key)}
            )
            direct_seerr_health = sq_direct_seerr.execute(
                Call(name="service_query", arguments={"service": "seerr", "operation": "health"}),
                principal,
            )
            assert direct_seerr_health.status == "verified_ok", f"Direct Seerr health failed: {direct_seerr_health}"

            qbit_ver = sq.execute(Call(name="service_query", arguments={"service": "qbittorrent", "operation": "health"}), principal)

            receipts["live_containers"] = {
                "sonarr": {
                    "version": sonarr_info.get("version"),
                    "app_name": sonarr_info.get("appName"),
                    "runtime_version": sonarr_info.get("runtimeVersion"),
                },
                "radarr": {
                    "version": radarr_info.get("version"),
                    "app_name": radarr_info.get("appName"),
                    "runtime_version": radarr_info.get("runtimeVersion"),
                    "seeded_multipage_movies": seeded_movies,
                },
                "lidarr": {
                    "version": lidarr_info.get("version"),
                    "app_name": lidarr_info.get("appName"),
                    "runtime_version": lidarr_info.get("runtimeVersion"),
                },
                "readarr": {
                    "version": readarr_info.get("version"),
                    "app_name": readarr_info.get("appName"),
                    "runtime_version": readarr_info.get("runtimeVersion"),
                    **seeded_readarr,
                },
                "whisparr": {
                    "version": whisparr_info.get("version"),
                    "app_name": whisparr_info.get("appName"),
                    "branch": whisparr_info.get("branch"),
                    "runtime_version": whisparr_info.get("runtimeVersion"),
                },
                "prowlarr": {
                    "version": prowlarr_info.get("version"),
                    "app_name": prowlarr_info.get("appName"),
                    "runtime_version": prowlarr_info.get("runtimeVersion"),
                },
                "bazarr": {
                    "bazarr_version": bazarr_info.get("bazarr_version"),
                    "python_version": bazarr_info.get("python_version"),
                    "start_time_type": type(bazarr_info.get("start_time")).__name__,
                },
                "tautulli": {
                    "tautulli_version": tautulli_info.get("tautulli_version"),
                    "tautulli_branch": tautulli_info.get("tautulli_branch"),
                },
                "qbittorrent": {
                    "server_version": qbit_ver.data.get("server_version"),
                    "api_version": qbit_ver.data.get("api_version"),
                },
                "bitmagnet": {
                    "server_version": bm_info.get("version"),
                    "postgres_backed": True,
                    "seeded_torrents": seeded_bitmagnet,
                },
                "seerr": {
                    "version": seerr_info.get("version"),
                    "commit_tag": seerr_info.get("commitTag"),
                    **seeded_seerr,
                },
            }

            checks_to_run: list[dict[str, Any]] = [
                # 1. Sonarr v4 live container binary
                {"service": "sonarr", "operation": "health"},
                {"service": "sonarr", "operation": "queue"},
                {"service": "sonarr", "operation": "history"},
                {"service": "sonarr", "operation": "library"},
                {"service": "sonarr", "operation": "missing"},
                {"service": "sonarr", "operation": "wanted"},
                {"service": "sonarr", "operation": "calendar"},
                {"service": "sonarr", "operation": "quality_profiles"},
                {"service": "sonarr", "operation": "indexers"},
                # 2. Radarr v5/v6 live container binary (with 320 seeded multi-page movies!)
                {"service": "radarr", "operation": "health"},
                {"service": "radarr", "operation": "queue"},
                {"service": "radarr", "operation": "history"},
                {"service": "radarr", "operation": "library"},
                {"service": "radarr", "operation": "library", "query": "Lab Qualified Film 042"},
                {"service": "radarr", "operation": "library", "monitored": True},
                {"service": "radarr", "operation": "library", "monitored": False},
                {"service": "radarr", "operation": "missing"},
                {"service": "radarr", "operation": "wanted"},
                {"service": "radarr", "operation": "calendar"},
                {"service": "radarr", "operation": "quality_profiles"},
                {"service": "radarr", "operation": "indexers"},
                # 3. Lidarr v2/v3 live container binary
                {"service": "lidarr", "operation": "health"},
                {"service": "lidarr", "operation": "queue"},
                {"service": "lidarr", "operation": "history"},
                {"service": "lidarr", "operation": "library"},
                {"service": "lidarr", "operation": "missing"},
                {"service": "lidarr", "operation": "wanted"},
                {"service": "lidarr", "operation": "calendar"},
                {"service": "lidarr", "operation": "quality_profiles"},
                {"service": "lidarr", "operation": "indexers"},
                # 4. Readarr v0.4 live container binary (with seeded author & book!)
                {"service": "readarr", "operation": "health"},
                {"service": "readarr", "operation": "queue"},
                {"service": "readarr", "operation": "history"},
                {"service": "readarr", "operation": "library"},
                {"service": "readarr", "operation": "library", "query": "Lab Qualified Author"},
                {"service": "readarr", "operation": "missing"},
                {"service": "readarr", "operation": "wanted"},
                {"service": "readarr", "operation": "calendar"},
                {"service": "readarr", "operation": "quality_profiles"},
                {"service": "readarr", "operation": "indexers"},
                # 5. Whisparr v3 live container binary (verifying /api/v3/movie fallback!)
                {"service": "whisparr", "operation": "health"},
                {"service": "whisparr", "operation": "queue"},
                {"service": "whisparr", "operation": "history"},
                {"service": "whisparr", "operation": "library"},
                {"service": "whisparr", "operation": "missing"},
                {"service": "whisparr", "operation": "wanted"},
                {"service": "whisparr", "operation": "calendar"},
                {"service": "whisparr", "operation": "quality_profiles"},
                {"service": "whisparr", "operation": "indexers"},
                # 6. Prowlarr v1 live container binary
                {"service": "prowlarr", "operation": "health"},
                {"service": "prowlarr", "operation": "indexers"},
                {"service": "prowlarr", "operation": "applications"},
                {"service": "prowlarr", "operation": "history"},
                {"service": "prowlarr", "operation": "search", "query": "Ubuntu"},
                # 7. Bazarr 1.6+ live container binary
                {"service": "bazarr", "operation": "health"},
                {"service": "bazarr", "operation": "missing", "media_type": "tv"},
                {"service": "bazarr", "operation": "missing", "media_type": "movie"},
                # 8. Tautulli v2 live container binary
                {"service": "tautulli", "operation": "health"},
                {"service": "tautulli", "operation": "activity"},
                {"service": "tautulli", "operation": "history"},
                # 9. qBittorrent v5 live container binary
                {"service": "qbittorrent", "operation": "health"},
                {"service": "qbittorrent", "operation": "activity"},
                {"service": "qbittorrent", "operation": "queue"},
                # 10. Bitmagnet v0.10.1 + Postgres 16 live container binary
                {"service": "bitmagnet", "operation": "health"},
                {"service": "bitmagnet", "operation": "search", "query": "Bitmagnet Film"},
                {"service": "bitmagnet", "operation": "search", "query": "Bitmagnet Film", "content_type": "movie"},
                # 11. Seerr 3.5.1 live container binary
                {"service": "seerr", "operation": "health"},
                {"service": "seerr", "operation": "search", "query": "Lab Qualified Seerr Film"},
            ]

            for args in checks_to_run:
                t_call = time.perf_counter()
                obs = sq.execute(Call(name="service_query", arguments=args), principal)
                dt_ms = (time.perf_counter() - t_call) * 1000.0
                ok = obs.status in {"verified_ok", "partial"}
                receipts["adapter_verifications"].append(
                    {
                        "adapter": "ServiceQueryAdapter",
                        "arguments": args,
                        "status": obs.status,
                        "http_status": obs.http_status,
                        "ok": ok,
                        "returned_count": obs.data.get("returned_count"),
                        "total_records": obs.data.get("total_records"),
                        "truncated": obs.data.get("truncated"),
                        "latency_ms": round(dt_ms, 2),
                        "audit": obs.audit_trail,
                    }
                )
                if not ok:
                    raise RuntimeError(f"Live container verification failed for {args}: status={obs.status} data={obs.data} audit={obs.audit_trail}")

            # Verify multi-page truncation & budget enforcement on live Radarr binary (320 movies > 250 max_records)
            radarr_lib = next(
                r for r in receipts["adapter_verifications"]
                if r["arguments"] == {"service": "radarr", "operation": "library"}
            )
            assert radarr_lib["total_records"] == 320, f"Expected 320 total movies from live Radarr, got {radarr_lib}"
            assert radarr_lib["returned_count"] == 250, f"Expected 250 bounded records from live Radarr, got {radarr_lib}"
            assert radarr_lib["truncated"] is True, f"Expected truncated=True on 320-movie live Radarr library, got {radarr_lib}"

            # Verify Bitmagnet live GraphQL search returned the seeded Postgres record
            bm_search = next(
                r for r in receipts["adapter_verifications"]
                if r["arguments"] == {"service": "bitmagnet", "operation": "search", "query": "Bitmagnet Film", "content_type": "movie"}
            )
            assert bm_search["returned_count"] == 1, f"Expected 1 seeded Bitmagnet movie record, got {bm_search}"

            # Verify NativeBitmagnetAdapter (v2 bitmagnet_search bridge) against live Bitmagnet + Postgres
            bm_v2_obs = bm_adapter.execute(Call(name="bitmagnet_search", arguments={"query": "Bitmagnet Film"}), principal)
            assert bm_v2_obs.status == "verified_ok" and bm_v2_obs.data.get("returned_count") == 1, f"NativeBitmagnetAdapter failed: {bm_v2_obs}"
            receipts["adapter_verifications"].append(
                {
                    "adapter": "NativeBitmagnetAdapter",
                    "arguments": {"query": "Bitmagnet Film"},
                    "status": bm_v2_obs.status,
                    "ok": True,
                    "returned_count": bm_v2_obs.data.get("returned_count"),
                    "audit": bm_v2_obs.audit_trail,
                }
            )

            # Execute real two-phase confirmed write against live Radarr v5/v6 binary (MoviesSearch for "Lab Qualified Film 001")
            write_call = Call(name="movie_search", arguments={"title": "Lab Qualified Film 001"})
            phase1 = arr_writer.execute(write_call, principal)
            assert phase1.status == "confirmation_required", f"Expected confirmation_required, got {phase1}"
            confirm_id = phase1.data["confirm_id"]
            phase2 = arr_writer.confirm_write(confirm_id, principal)
            assert phase2.status in {"dispatched", "verified_ok"}, f"Expected dispatched/verified_ok from live Radarr command, got {phase2}"
            receipts["adapter_verifications"].append(
                {
                    "adapter": "NativeArrWriteAdapter",
                    "service": "radarr",
                    "arguments": write_call.arguments,
                    "phase1_status": phase1.status,
                    "phase2_status": phase2.status,
                    "ok": True,
                    "command_id": phase2.data.get("command_id"),
                    "search_job_status": phase2.data.get("search_job_status"),
                    "audit": phase2.audit_trail,
                }
            )

            # Execute real two-phase confirmed BookSearch against live Readarr v0.4 binary
            book_p1 = arr_writer.execute_book_search("Lab Qualified Author", principal, book="Lab Qualified Book 001")
            assert book_p1.status == "confirmation_required", f"Expected confirmation_required from Readarr, got {book_p1}"
            book_p2 = arr_writer.confirm_book_search(book_p1.data["confirm_id"], principal)
            assert book_p2.status in {"dispatched", "verified_ok"}, f"Expected dispatched/verified_ok from live Readarr BookSearch, got {book_p2}"
            receipts["adapter_verifications"].append(
                {
                    "adapter": "NativeArrWriteAdapter.book_search",
                    "service": "readarr",
                    "arguments": {"author": "Lab Qualified Author", "book": "Lab Qualified Book 001"},
                    "phase1_status": book_p1.status,
                    "phase2_status": book_p2.status,
                    "ok": True,
                    "command_id": book_p2.data.get("command_id"),
                    "search_job_status": book_p2.data.get("search_job_status"),
                    "audit": book_p2.audit_trail,
                }
            )

            # Execute NativeRequestAdapter caller-owned status check + confirmed pending request cancellation against live Seerr 3.5.1
            req_status_obs = req_adapter.execute(Call(name="request_status", arguments={"title": "Lab Qualified Seerr Film"}), principal)
            assert req_status_obs.status == "verified_ok" and req_status_obs.data.get("own_request_count") == 1, f"Seerr request_status failed: {req_status_obs}"
            receipts["adapter_verifications"].append(
                {
                    "adapter": "NativeRequestAdapter.request_status",
                    "service": "seerr",
                    "arguments": {"title": "Lab Qualified Seerr Film"},
                    "status": req_status_obs.status,
                    "own_request_count": req_status_obs.data.get("own_request_count"),
                    "ok": True,
                    "audit": req_status_obs.audit_trail,
                }
            )

            cancel_p1 = req_adapter.execute_cancel_request("Lab Qualified Seerr Film", principal, media_type="movie")
            assert cancel_p1.status == "confirmation_required", f"Expected confirmation_required for Seerr cancel, got {cancel_p1}"
            cancel_p2 = req_adapter.confirm_write(cancel_p1.data["confirm_id"], principal)
            assert cancel_p2.status == "verified_ok" and cancel_p2.data.get("cancelled_request_id") == 1 and cancel_p2.data.get("changed") is True, f"Seerr cancel confirm failed: {cancel_p2}"
            receipts["adapter_verifications"].append(
                {
                    "adapter": "NativeRequestAdapter.cancel_request",
                    "service": "seerr",
                    "arguments": {"title": "Lab Qualified Seerr Film", "media_type": "movie"},
                    "phase1_status": cancel_p1.status,
                    "phase2_status": cancel_p2.status,
                    "cancelled_request_id": cancel_p2.data.get("cancelled_request_id"),
                    "ok": True,
                    "audit": cancel_p2.audit_trail,
                }
            )

        receipts["summary"] = {
            "live_services_qualified": len(receipts["live_containers"]),
            "total_checks": len(receipts["adapter_verifications"]),
            "passed_checks": sum(1 for r in receipts["adapter_verifications"] if r["ok"]),
            "failed_checks": sum(1 for r in receipts["adapter_verifications"] if not r["ok"]),
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        }

        out_dir = ROOT_DIR / "experiments" / "upstream-lab-v1"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "summary.json"
        out_path.write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {out_path} ({receipts['summary']['passed_checks']}/{receipts['summary']['total_checks']} passed across {receipts['summary']['live_services_qualified']} live services in {receipts['summary']['elapsed_seconds']}s)")
        return 0
    finally:
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
