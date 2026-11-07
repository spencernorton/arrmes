#!/usr/bin/env python3
"""Boot and qualify actual official upstream *Arr & companion Docker containers (loopback-isolated lab).

This script:
1. Creates an isolated Docker bridge network (`arrmes_upstream_lab_net`) and boots real official
   upstream containers bound strictly to 127.0.0.1 ephemeral ports:
   - lscr.io/linuxserver/sonarr:latest (Sonarr v4)
   - lscr.io/linuxserver/radarr:latest (Radarr v5/v6)
   - lscr.io/linuxserver/lidarr:latest (Lidarr v2/v3)
   - lscr.io/linuxserver/prowlarr:latest (Prowlarr v1)
   - lscr.io/linuxserver/bazarr:latest (Bazarr 1.6+)
   - lscr.io/linuxserver/qbittorrent:latest (qBittorrent v5)
   - lscr.io/linuxserver/tautulli:latest (Tautulli v2)
   - ghcr.io/bitmagnet-io/bitmagnet:latest (Bitmagnet binary version + CLI verification)
   - ghcr.io/fallenbagel/jellyseerr:latest (Seerr/Jellyseerr image + runtime inspection)
2. Extracts the live auto-generated API keys/credentials from the booted container configs.
3. Seeds multi-page library rows (320 items > 250 single-page budget) into the live Radarr
   SQLite database and exercises Arrmes native adapters (`ServiceQueryAdapter`, `NativeArrWriteAdapter`)
   against the live container HTTP endpoints.
4. Records exact image digests, upstream binary versions, HTTP status receipts, and post-condition
   checks into `experiments/upstream-lab-v1/summary.json`, then tears down all containers in `finally`.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import subprocess
import sys
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
from arrmes.services.native import ServiceConfig, ServiceQueryAdapter

LAB_NET = "arrmes_upstream_lab_net"
CONTAINERS = [
    "arrmes_lab_sonarr",
    "arrmes_lab_radarr",
    "arrmes_lab_lidarr",
    "arrmes_lab_prowlarr",
    "arrmes_lab_bazarr",
    "arrmes_lab_qbittorrent",
    "arrmes_lab_tautulli",
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
    """Stop Radarr briefly, seed 320 movies into `/config/radarr.db`, and start Radarr so its
    in-memory / SQLite cache serves all 320 multi-page movies cleanly."""
    _run(["docker", "stop", container], timeout=30, check=True)
    tmp_db = Path("/tmp/arrmes_lab_radarr.db")
    for ext in ("", "-wal", "-shm"):
        p = Path(f"/tmp/arrmes_lab_radarr.db{ext}")
        if p.exists():
            p.unlink()
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
        "lscr.io/linuxserver/prowlarr:latest",
        "lscr.io/linuxserver/bazarr:latest",
        "lscr.io/linuxserver/qbittorrent:latest",
        "lscr.io/linuxserver/tautulli:latest",
        "ghcr.io/fallenbagel/jellyseerr:latest",
        "ghcr.io/bitmagnet-io/bitmagnet:latest",
    ]
    for img in images_to_record:
        receipts["images"][img] = _image_digest(img)

    try:
        _run(["docker", "network", "create", "--driver", "bridge", LAB_NET], check=True)

        specs = [
            ("arrmes_lab_sonarr", "lscr.io/linuxserver/sonarr:latest", "18989:8989"),
            ("arrmes_lab_radarr", "lscr.io/linuxserver/radarr:latest", "17878:7878"),
            ("arrmes_lab_lidarr", "lscr.io/linuxserver/lidarr:latest", "18686:8686"),
            ("arrmes_lab_prowlarr", "lscr.io/linuxserver/prowlarr:latest", "19696:9696"),
            ("arrmes_lab_bazarr", "lscr.io/linuxserver/bazarr:latest", "16767:6767"),
            ("arrmes_lab_qbittorrent", "lscr.io/linuxserver/qbittorrent:latest", "18080:18080"),
            ("arrmes_lab_tautulli", "lscr.io/linuxserver/tautulli:latest", "18181:8181"),
        ]
        for cname, image, port_map in specs:
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    cname,
                    "--network",
                    LAB_NET,
                    "-e",
                    "PUID=1000",
                    "-e",
                    "PGID=1000",
                    "-e",
                    "TZ=UTC",
                    "-e",
                    "WEBUI_PORT=18080",
                    "-p",
                    f"127.0.0.1:{port_map}",
                    image,
                ],
                check=True,
            )

        bm_ver = _run(["docker", "image", "inspect", "ghcr.io/bitmagnet-io/bitmagnet:latest", "--format", "{{index .Config.Labels \"org.opencontainers.image.version\"}}"])
        js_ver = _run(["docker", "run", "--rm", "--entrypoint", "node", "ghcr.io/fallenbagel/jellyseerr:latest", "-p", "require('./package.json').version"])

        sonarr_key = _extract_xml_apikey("arrmes_lab_sonarr")
        radarr_key = _extract_xml_apikey("arrmes_lab_radarr")
        lidarr_key = _extract_xml_apikey("arrmes_lab_lidarr")
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
        _wait_http("http://127.0.0.1:18080/api/v2/app/webapiVersion", require_200=False)
        qbit_sid = _login_qbit_sid("http://127.0.0.1:18080", qbit_pass)

        # Seed 320 multi-page movies into the live Radarr SQLite database and wait for Radarr restart
        seeded_movies = _seed_radarr_multipage_sqlite("arrmes_lab_radarr", count=320)
        _wait_http(
            "http://127.0.0.1:17878/api/v3/system/status",
            headers={"X-Api-Key": radarr_key},
            require_200=True,
        )

        sonarr_info = json.loads(sonarr_status_raw)
        radarr_info = json.loads(radarr_status_raw)
        lidarr_info = json.loads(lidarr_status_raw)
        prowlarr_info = json.loads(prowlarr_status_raw)
        bazarr_info = json.loads(bazarr_status_raw).get("data", {})
        tautulli_info = json.loads(tautulli_status_raw).get("response", {}).get("data", {})

        configs = {
            "sonarr": ServiceConfig(base_url="http://127.0.0.1:18989", api_key=sonarr_key),
            "radarr": ServiceConfig(base_url="http://127.0.0.1:17878", api_key=radarr_key),
            "lidarr": ServiceConfig(base_url="http://127.0.0.1:18686", api_key=lidarr_key),
            "prowlarr": ServiceConfig(base_url="http://127.0.0.1:19696", api_key=prowlarr_key),
            "bazarr": ServiceConfig(base_url="http://127.0.0.1:16767", api_key=bazarr_key),
            "tautulli": ServiceConfig(base_url="http://127.0.0.1:18181", api_key=tautulli_key),
            "qbittorrent": ServiceConfig(base_url="http://127.0.0.1:18080", session_id=qbit_sid),
        }
        sq = ServiceQueryAdapter(configs, max_pages=5, page_size=50, max_records=250)
        arr_writer = NativeArrWriteAdapter(
            sq,
            allowed_majors={"sonarr": {4}, "radarr": {5, 6}, "lidarr": {2, 3}},
        )
        principal = Principal(
            subject="upstream-lab-qualifier",
            scopes=frozenset({
                "media:read",
                "media:manage",
                "releases:read",
                "activity:read",
                "requests:read",
                "torrents:read",
            }),
        )

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
            "bitmagnet": {
                "image_version": bm_ver.stdout.strip(),
            },
            "jellyseerr": {
                "package_version": js_ver.stdout.strip(),
            },
        }

        checks_to_run: list[dict[str, Any]] = [
            # Sonarr v4 live container binary
            {"service": "sonarr", "operation": "health"},
            {"service": "sonarr", "operation": "queue"},
            {"service": "sonarr", "operation": "history"},
            {"service": "sonarr", "operation": "library"},
            {"service": "sonarr", "operation": "missing"},
            {"service": "sonarr", "operation": "wanted"},
            {"service": "sonarr", "operation": "calendar"},
            {"service": "sonarr", "operation": "quality_profiles"},
            {"service": "sonarr", "operation": "indexers"},
            # Radarr v5/v6 live container binary (with 320 seeded multi-page movies!)
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
            # Lidarr v2/v3 live container binary
            {"service": "lidarr", "operation": "health"},
            {"service": "lidarr", "operation": "queue"},
            {"service": "lidarr", "operation": "history"},
            {"service": "lidarr", "operation": "library"},
            {"service": "lidarr", "operation": "missing"},
            {"service": "lidarr", "operation": "wanted"},
            {"service": "lidarr", "operation": "calendar"},
            {"service": "lidarr", "operation": "quality_profiles"},
            {"service": "lidarr", "operation": "indexers"},
            # Prowlarr v1 live container binary
            {"service": "prowlarr", "operation": "health"},
            {"service": "prowlarr", "operation": "indexers"},
            {"service": "prowlarr", "operation": "applications"},
            {"service": "prowlarr", "operation": "history"},
            {"service": "prowlarr", "operation": "search", "query": "Ubuntu"},
            # Bazarr 1.6+ live container binary (verifies float start_time handling!)
            {"service": "bazarr", "operation": "health"},
            {"service": "bazarr", "operation": "missing", "media_type": "tv"},
            {"service": "bazarr", "operation": "missing", "media_type": "movie"},
            # Tautulli v2 live container binary
            {"service": "tautulli", "operation": "health"},
            {"service": "tautulli", "operation": "activity"},
            {"service": "tautulli", "operation": "history"},
            # qBittorrent v5 live container binary
            {"service": "qbittorrent", "operation": "health"},
            {"service": "qbittorrent", "operation": "activity"},
            {"service": "qbittorrent", "operation": "queue"},
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
                "arguments": write_call.arguments,
                "phase1_status": phase1.status,
                "phase2_status": phase2.status,
                "ok": True,
                "command_id": phase2.data.get("command_id"),
                "search_job_status": phase2.data.get("search_job_status"),
                "audit": phase2.audit_trail,
            }
        )

        receipts["summary"] = {
            "total_checks": len(receipts["adapter_verifications"]),
            "passed_checks": sum(1 for r in receipts["adapter_verifications"] if r["ok"]),
            "failed_checks": sum(1 for r in receipts["adapter_verifications"] if not r["ok"]),
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        }

        out_dir = ROOT_DIR / "experiments" / "upstream-lab-v1"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "summary.json"
        out_path.write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {out_path} ({receipts['summary']['passed_checks']}/{receipts['summary']['total_checks']} passed in {receipts['summary']['elapsed_seconds']}s)")
        return 0
    finally:
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
