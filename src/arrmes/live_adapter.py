"""Production gateway adapter connecting Arrmes to the Self-Hosted API Gateway and SandboxCluster."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from arrmes import __version__
from arrmes.adapters import AdapterObservation, SandboxAdapter, _norm
from arrmes.catalog import V2_BY_NAME, V3_BY_NAME, WRITE_EFFECT_ACTIONS, WRITE_EFFECT_ACTIONS_V3
from arrmes.services.contract import SERVICE_PERMISSIONS, validate_service_query
from arrmes.services.native import _public_row
from arrmes.planner import CatalogInvertedIndex, Principal, ResolutionRequired, prepare, resolve_title, resolve_title_candidates
from arrmes.protocol import Call, normalized, validate_call

_REDACT_QUERY_RE = re.compile(r"([?&](?:apikey|api_key|token|key)=)[^&#\s]+", re.IGNORECASE)
# Read-only POST endpoints (queries expressed as POST bodies); safe to replay.
_READ_ONLY_POST_SUFFIXES = ("/armiger/parse", "/bitagent/graphql", "/image-gen/v1/infer")
# Mutations with no verified live implementation behind the gateway. They are
# rejected before a confirmation token is minted so they are never advertised as executable.
LIVE_UNSUPPORTED_WRITES = frozenset({"torrent_pause", "torrent_resume"})
_COMMAND_TERMINAL_OK = frozenset({"completed"})
_COMMAND_IN_FLIGHT = frozenset({"queued", "started"})
_RELEASE_PARSE_RE = re.compile(
    r"^(?P<title>.+?)[.\s_-]+(?P<year>(?:19|20)\d{2})?[.\s_-]*(?P<season_ep>S\d{1,2}(?:E\d{1,2})?)?[.\s_-]*"
    r"(?P<resolution>2160p|1080p|720p|480p)?",
    re.IGNORECASE,
)


def redact_url_path(path: str) -> str:
    """Redact any query-string API keys or tokens before recording in audit trails."""
    return _REDACT_QUERY_RE.sub(r"\1REDACTED", path)


def _load_default_ges_key() -> str:
    """Load the caller's GES_API_KEY from environment or protected local key store without logging it."""
    env_key = os.environ.get("GES_API_KEY", "").strip()
    if env_key:
        return env_key
    key_file_env = os.environ.get("GES_KEY_FILE", "").strip()
    candidates = [key_file_env] if key_file_env else []
    candidates.extend([
        "~/.config/arrmes/gateway.key",
        "~/.config/opencode/ge-api.key",
    ])
    for cand in candidates:
        p = Path(cand).expanduser()
        if p.is_file():
            try:
                val = p.read_text(encoding="utf-8").strip()
                if val:
                    return val
            except OSError:
                continue
    return ""


def _parse_release_metadata(release_name: str) -> dict[str, Any]:
    """Deterministic release-name parser for Armiger release metadata."""
    clean = release_name.strip()
    res_match = re.search(r"\b(2160p|1080p|720p|480p)\b", clean, re.IGNORECASE)
    resolution = res_match.group(1).lower() if res_match else "unknown"
    src_match = re.search(r"\b(WEB-DL|WEBRip|BluRay|BDRip|Remux|HDTV)\b", clean, re.IGNORECASE)
    source = src_match.group(1) if src_match else "unknown"
    codec_match = re.search(r"\b(H\.?265|x265|HEVC|H\.?264|x264|AV1)\b", clean, re.IGNORECASE)
    codec = codec_match.group(1) if codec_match else "unknown"
    hdr_match = re.search(r"\b(DV|Dolby\.?Vision|HDR10\+?|HDR)\b", clean, re.IGNORECASE)
    hdr = hdr_match.group(1) if hdr_match else "SDR"
    grp_match = re.search(r"-([A-Za-z0-9]+)(?:\.[a-z0-9]{2,4})?$", clean)
    group = grp_match.group(1) if grp_match else "unknown"
    return {
        "release_name": clean,
        "resolution": resolution,
        "source": source,
        "video_codec": codec,
        "dynamic_range": hdr,
        "release_group": group,
        "parsed": True,
    }


def _map_qui_state(raw_state: str) -> str:
    st = (raw_state or "").lower()
    if "paused" in st or "stopped" in st:
        return "paused"
    if "stalled" in st:
        return "stalled"
    if st in {"downloading", "metadl", "forceddl", "queueddl", "allocating", "checkingdl"}:
        return "downloading"
    if st in {"uploading", "forcedup", "queuedup", "checkingup"}:
        return "seeding"
    if "up" in st:
        return "completed"
    return st or "unknown"


@dataclass
class WriteConfirmationToken:
    confirm_id: str
    subject: str
    call_name: str
    arguments: dict[str, Any]
    summary: str
    resolved_entity: dict[str, Any]
    created_at: float
    expires_at: float
    signature: str
    consumed: bool = False


def _extract_tautulli_data(resp: Any, *, expected_list_key: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate Tautulli's HTTP-200 JSON envelope and require result == 'success' plus expected list data."""
    if not isinstance(resp, dict):
        return None, {"error": "tautulli_invalid_envelope", "raw": resp}
    envelope = resp.get("response")
    if not isinstance(envelope, dict):
        return None, {"error": "tautulli_missing_response_envelope", "raw": resp}
    if envelope.get("result") != "success":
        return None, {
            "error": "tautulli_application_error",
            "result": envelope.get("result"),
            "message": envelope.get("message") or "Tautulli returned an application error",
        }
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(expected_list_key), list):
        return None, {
            "error": "tautulli_invalid_data_shape",
            "expected_list_key": expected_list_key,
            "message": envelope.get("message") or "Unexpected Tautulli data payload",
        }
    return data, {}


class WriteConfirmationStore:
    """Short-lived, single-use confirmation token store for production write actions with bounded capacity."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        secret_key: bytes | None = None,
        clock: Callable[[], float] = time.time,
        max_pending_total: int = 256,
        max_pending_per_subject: int = 16,
        max_consumed_tombstones: int = 512,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_pending_total <= 0 or max_pending_per_subject <= 0 or max_consumed_tombstones < 0:
            raise ValueError("Confirmation store capacity bounds must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self._secret = secret_key or secrets.token_bytes(32)
        self._clock = clock
        self.max_pending_total = int(max_pending_total)
        self.max_pending_per_subject = int(max_pending_per_subject)
        self.max_consumed_tombstones = int(max_consumed_tombstones)
        self._lock = threading.Lock()
        self._tokens: dict[str, WriteConfirmationToken] = {}

    def _sign(self, confirm_id: str, subject: str, call_name: str, arguments: dict[str, Any], expires_at: float,
              resolved_entity: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "confirm_id": confirm_id,
                "subject": subject,
                "call_name": call_name,
                "arguments": arguments,
                "resolved_entity": resolved_entity,
                "expires_at": expires_at,
            },
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _prune_locked(self, ts: float, *, preserve_id: str | None = None) -> None:
        expired_ids = [
            cid
            for cid, tok in self._tokens.items()
            if cid != preserve_id and ts >= tok.expires_at
        ]
        for cid in expired_ids:
            self._tokens.pop(cid, None)

        consumed = [
            (tok.created_at, cid)
            for cid, tok in self._tokens.items()
            if tok.consumed and cid != preserve_id
        ]
        if len(consumed) > self.max_consumed_tombstones:
            consumed.sort()
            excess = len(consumed) - self.max_consumed_tombstones
            for _, cid in consumed[:excess]:
                self._tokens.pop(cid, None)

    def _evict_oldest_pending_locked(self, subject: str) -> None:
        subj_pending = [
            (tok.created_at, cid)
            for cid, tok in self._tokens.items()
            if not tok.consumed and tok.subject == subject
        ]
        if len(subj_pending) >= self.max_pending_per_subject:
            subj_pending.sort()
            to_drop = len(subj_pending) - self.max_pending_per_subject + 1
            for _, cid in subj_pending[:to_drop]:
                self._tokens.pop(cid, None)

        all_pending = [
            (tok.created_at, cid)
            for cid, tok in self._tokens.items()
            if not tok.consumed
        ]
        if len(all_pending) >= self.max_pending_total:
            all_pending.sort()
            to_drop = len(all_pending) - self.max_pending_total + 1
            for _, cid in all_pending[:to_drop]:
                self._tokens.pop(cid, None)

    def mint(
        self,
        principal: Principal,
        call: Call,
        *,
        summary: str,
        resolved_entity: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> WriteConfirmationToken:
        call = validate_call(call.name, call.arguments)
        if call.name not in WRITE_EFFECT_ACTIONS_V3:
            raise ValueError("Confirmation tokens are only valid for write actions")
        # This explicit-tool registry does not change the v2 neural decoder or
        # its frozen prompt. Every mint is schema-checked above.
        norm = normalized(call)
        ts = float(self._clock() if now is None else now)
        if not math.isfinite(ts):
            raise ValueError("Confirmation time must be finite")
        expires = ts + self.ttl_seconds
        cid = f"arrmes-confirm-{secrets.token_hex(12)}"
        entity = deepcopy(resolved_entity or {})
        arguments = deepcopy(norm["arguments"])
        sig = self._sign(cid, principal.subject, norm["name"], arguments, expires, entity)
        tok = WriteConfirmationToken(
            confirm_id=cid,
            subject=principal.subject,
            call_name=norm["name"],
            arguments=arguments,
            summary=summary,
            resolved_entity=entity,
            created_at=ts,
            expires_at=expires,
            signature=sig,
            consumed=False,
        )
        with self._lock:
            self._prune_locked(ts)
            self._evict_oldest_pending_locked(principal.subject)
            self._tokens[cid] = tok
        return tok

    def redeem(self, confirm_id: str, principal: Principal, *, now: float | None = None) -> tuple[WriteConfirmationToken | None, str | None]:
        ts = float(self._clock() if now is None else now)
        if not math.isfinite(ts):
            raise ValueError("Confirmation time must be finite")
        with self._lock:
            self._prune_locked(ts, preserve_id=confirm_id)
            tok = self._tokens.get(confirm_id)
            if tok is None:
                return None, "confirmation_not_found"
            if tok.subject != principal.subject:
                if ts >= tok.expires_at:
                    self._tokens.pop(confirm_id, None)
                return None, "confirmation_subject_mismatch"
            if tok.consumed:
                return None, "confirmation_already_consumed"
            if ts >= tok.expires_at:
                self._tokens.pop(confirm_id, None)
                return None, "confirmation_expired"
            expected_sig = self._sign(tok.confirm_id, tok.subject, tok.call_name, tok.arguments, tok.expires_at, tok.resolved_entity)
            if not hmac.compare_digest(tok.signature, expected_sig):
                return None, "confirmation_signature_invalid"
            tok.consumed = True
            self._prune_locked(ts)
            return tok, None



class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject cross-origin, scheme-changing, or non-GET redirects so credentials never leak."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != "GET":
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "redirect_not_followed_for_non_get",
                headers,
                fp,
            )
        orig = urllib.parse.urlsplit(req.full_url)
        resolved_url = urllib.parse.urljoin(req.full_url, newurl)
        target = urllib.parse.urlsplit(resolved_url)
        orig_scheme = orig.scheme.lower()
        target_scheme = target.scheme.lower()
        orig_port = orig.port or (443 if orig_scheme == "https" else 80)
        target_port = target.port or (443 if target_scheme == "https" else 80)
        if (
            target_scheme != orig_scheme
            or (target.hostname or "").lower() != (orig.hostname or "").lower()
            or target_port != orig_port
        ):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "cross_origin_redirect_blocked",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, resolved_url)


_STDLIB_URLOPEN = urllib.request.urlopen
_SAME_ORIGIN_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _open_same_origin(req: urllib.request.Request, *, timeout: float):
    if urllib.request.urlopen is not _STDLIB_URLOPEN:
        return urllib.request.urlopen(req, timeout=timeout)
    return _SAME_ORIGIN_OPENER.open(req, timeout=timeout)


def _valid_command_id(cmd_resp: Any) -> int | str | None:
    if not isinstance(cmd_resp, dict):
        return None
    cmd_id = cmd_resp.get("id")
    if isinstance(cmd_id, bool) or cmd_id is None:
        return None
    if isinstance(cmd_id, int):
        return cmd_id if cmd_id > 0 else None
    if isinstance(cmd_id, str) and cmd_id.strip():
        return cmd_id.strip()
    return None


class LiveGatewayAdapter(SandboxAdapter):
    """Production adapter connecting Arrmes to the GES API gateway (https://gateway.example.invalid) or SandboxCluster."""

    def __init__(
        self,
        gateway_base_url: str = "https://gateway.example.invalid",
        *,
        gateway_api_key: str | None = None,
        service_credentials: dict[str, str] | None = None,
        write_mode: str = "confirm",
        confirmation_store: WriteConfirmationStore | None = None,
        timeout_seconds: float = 12.0,
        max_retries: int = 2,
        qbittorrent_url: str | None = None,
        request_owner_subject: str | None = None,
        sandbox: bool = False,
        enable_gateway_v3: bool | None = None,
    ) -> None:
        if write_mode not in {"confirm", "shadow", "direct"}:
            raise ValueError("write_mode must be 'confirm', 'shadow', or 'direct'")
        parsed_url = urllib.parse.urlparse(gateway_base_url)
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
        super().__init__(gateway_base_url, port=port, max_retries=max_retries)
        self.gateway_api_key = gateway_api_key if gateway_api_key is not None else _load_default_ges_key()
        self.service_credentials = dict(service_credentials or {})
        self.write_mode = write_mode
        self.confirmation_store = confirmation_store or WriteConfirmationStore()
        self.timeout_seconds = timeout_seconds
        self.qbittorrent_url = (qbittorrent_url or os.environ.get("ARRMES_QBITTORRENT_URL", "")).rstrip("/")
        # The request service attributes a submission to the owner of the gateway key; it has no
        # verified header for acting on behalf of another user. Live request_media is therefore
        # allowed only for the one Arrmes subject that owns the gateway credential.
        self.request_owner_subject = (
            request_owner_subject if request_owner_subject is not None else os.environ.get("ARRMES_REQUEST_OWNER_SUBJECT", "")
        ).strip()
        self._active_effect: str = "read"
        # The inherited read/write transport guard uses per-execution state.
        # Serialize the entire adapter call across principals, including confirm.
        self._execution_lock = threading.RLock()
        self._confirmed_entity: dict[str, Any] | None = None
        # Sandbox semantics are selected explicitly, never inferred from a loopback URL or from
        # supplied credentials, so production identity and unsupported-action gates always apply.
        if sandbox and parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("sandbox=True requires a loopback SandboxCluster URL")
        self._is_sandbox_backend = bool(sandbox)
        self.enable_gateway_v3 = (
            os.environ.get("ARRMES_GATEWAY_V3", "").strip() == "1"
            if enable_gateway_v3 is None
            else bool(enable_gateway_v3)
        )
        self.supports_service_queries = bool(self.enable_gateway_v3 and not self._is_sandbox_backend)
        self._catalog_cache: dict[str, tuple[float, int, Any]] = {}

    @staticmethod
    def _entity_binding(name: str, args: dict[str, Any], entity: dict[str, Any]) -> dict[str, Any]:
        if name == "tv_search_missing":
            return {"series_id": entity.get("series_id"), "title": _norm(str(entity.get("title", ""))),
                    "year": entity.get("year")}
        if name == "movie_search":
            return {"movie_id": entity.get("movie_id"), "title": _norm(str(entity.get("title", ""))),
                    "year": entity.get("year")}
        if name == "music_search":
            binding = {"artist_id": entity.get("artist_id"), "artist": _norm(str(entity.get("artist", "")))}
            if "album" in args:
                binding["album_ids"] = sorted(a["id"] for a in entity.get("albums", []) if isinstance(a, dict) and type(a.get("id")) is int)
            return binding
        if name in {"torrent_pause", "torrent_resume"}:
            return {"hash": entity.get("hash")}
        if name == "request_media":
            return {"tmdb_id": entity.get("tmdb_id"), "media_type": entity.get("media_type")}
        return {}

    def _guard_confirmed_entity(self, name: str, args: dict[str, Any], entity: dict[str, Any],
                                audit: list[dict[str, Any]]) -> AdapterObservation | None:
        if self._confirmed_entity is None:
            return None
        expected = self._entity_binding(name, args, self._confirmed_entity)
        actual = self._entity_binding(name, args, entity)
        if expected != actual:
            return AdapterObservation("denied", V3_BY_NAME[name].service if name in V3_BY_NAME else "control", name, 409, False,
                                      {"error": "confirmation_entity_changed", "requires_new_confirmation": True}, audit)
        return None

    def _auth_headers_for(self, service: str, *, principal: Principal | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.gateway_api_key:
            headers["X-GES-Key"] = self.gateway_api_key
        svc_cred = self.service_credentials.get(service)
        if service in {"sonarr", "radarr", "lidarr"} and svc_cred:
            headers["X-Api-Key"] = svc_cred
        elif service == "qbittorrent" and svc_cred:
            headers["Cookie"] = f"SID={svc_cred}"
        elif service in {"requests", "seer"} and svc_cred:
            headers["Authorization"] = f"Bearer {svc_cred}"
        if principal is not None and service == "requests" and self._is_sandbox_backend:
            headers["X-Principal-Id"] = principal.subject
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        audit: list[dict[str, Any]],
        base_url_override: str | None = None,
    ) -> tuple[int, Any]:
        # Enforce read-only HTTP method invariant when executing a read-effect action
        read_only_post = method == "POST" and any(path.endswith(p) for p in _READ_ONLY_POST_SUFFIXES)
        if self._active_effect == "read" and method != "GET" and not read_only_post:
            raise PermissionError(f"Read-only action forbidden from issuing HTTP {method} to {redact_url_path(path)}")
        # Only replay requests that cannot mutate upstream state. The gateway does not
        # guarantee idempotency for mutations, so a failed mutation is reported as an
        # unknown outcome instead of being resubmitted.
        replay_safe = method == "GET" or read_only_post

        target_base = (base_url_override or self.base_url).rstrip("/")
        url = f"{target_base}{path}"
        req_headers = {
            "Accept": "application/json",
            "User-Agent": f"arrmes-router/{__version__}",
            **(headers or {}),
        }
        if self.gateway_api_key and "X-GES-Key" not in req_headers and base_url_override is None:
            req_headers["X-GES-Key"] = self.gateway_api_key

        payload_bytes = None
        if body is not None:
            payload_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        safe_path = redact_url_path(path)
        cacheable = (
            not self._is_sandbox_backend
            and method == "GET"
            and path in {
                "/sonarr/api/v3/series",
                "/radarr/api/v3/movie",
                "/lidarr/api/v1/artist",
                "/readarr/api/v1/author",
                "/whisparr/api/v3/series",
            }
        )
        # Confirmation must resolve the current upstream entity, even when the
        # preflight just populated this cache. Reusing that snapshot would hide
        # a replaced library ID or changed title/year before mutation. Successful
        # fresh reads can still refresh the ordinary read cache below.
        if cacheable and self._confirmed_entity is None:
            cached = self._catalog_cache.get(path)
            if cached is not None and (time.time() - cached[0]) < 60.0:
                audit.append({"method": method, "path": safe_path, "status": cached[1], "attempt": 0, "cached": True})
                return cached[1], cached[2]
        attempts = 0
        while True:
            attempts += 1
            req = urllib.request.Request(url, data=payload_bytes, headers=req_headers, method=method)
            try:
                with _open_same_origin(req, timeout=self.timeout_seconds) as resp:
                    status = int(resp.status)
                    raw = resp.read().decode("utf-8")
                    parsed = json.loads(raw) if raw else {}
                    if cacheable and status == 200:
                        if isinstance(parsed, list):
                            parsed = CatalogInvertedIndex(parsed)
                        self._catalog_cache[path] = (time.time(), status, parsed)
                    audit.append({"method": method, "path": safe_path, "status": status, "attempt": attempts})
                    return status, parsed
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                raw = exc.read().decode("utf-8")
                try:
                    parsed = json.loads(raw) if raw else {"error": f"http_{status}"}
                except ValueError:
                    parsed = {"error": raw or f"http_{status}"}
                audit.append({"method": method, "path": safe_path, "status": status, "attempt": attempts})
                if status in {502, 503, 504} and replay_safe and attempts <= self.max_retries:
                    continue
                if status in {502, 503, 504} and not replay_safe and isinstance(parsed, dict):
                    parsed = {**parsed, "outcome": "unknown", "replayed": False}
                return status, parsed
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                audit.append({"method": method, "path": safe_path, "status": 503, "attempt": attempts, "error": str(exc)})
                if not isinstance(exc, ValueError) and replay_safe and attempts <= self.max_retries:
                    continue
                payload: dict[str, Any] = {"error": "transport_or_parse_error", "detail": str(exc)}
                if not replay_safe:
                    payload.update({"outcome": "unknown", "replayed": False})
                return 503, payload

    @staticmethod
    def _command_observation_status(poll_ok: bool, command_status: Any) -> tuple[str, bool]:
        """Completed commands are verified; queued/started commands are dispatched, not complete."""
        if poll_ok and command_status in _COMMAND_TERMINAL_OK:
            return "verified_ok", True
        if poll_ok and command_status in _COMMAND_IN_FLIGHT:
            return "dispatched", False
        return "error", False

    # --- Live GES Gateway overrides when connected to https://gateway.example.invalid ---

    def execute_tv_missing(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_tv_missing(args, principal, idempotency_key=idempotency_key, audit=audit)
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
        page_size = 50
        all_records: list[dict[str, Any]] = []
        total_records = 0
        while page <= 5:
            q = urllib.parse.urlencode({"seriesId": resolved["id"], "includeSpecials": inc_spec, "page": page, "pageSize": page_size})
            c2, payload = self._request_json("GET", f"/sonarr/api/v3/wanted/missing?{q}", headers=headers, audit=audit)
            if c2 != 200:
                return AdapterObservation("error", "sonarr", "tv_missing", c2, False, payload, audit)
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list) or not all(isinstance(r, dict) for r in payload["records"]):
                return AdapterObservation("error", "sonarr", "tv_missing", 502, False,
                                          {"error": "malformed_missing_response"}, audit)
            records = payload["records"]
            if any(isinstance(r, dict) and "seriesId" in r and r.get("seriesId") != resolved["id"] for r in records):
                c_ep, ep_list = self._request_json("GET", f"/sonarr/api/v3/episode?seriesId={resolved['id']}", headers=headers, audit=audit)
                if c_ep == 200 and isinstance(ep_list, list):
                    inc_bool = bool(args.get("include_specials", False))
                    all_records = []
                    now_utc = datetime.now(timezone.utc)
                    for episode in ep_list:
                        if (not isinstance(episode, dict)
                                or type(episode.get("seriesId")) is not int or episode["seriesId"] != resolved["id"]
                                or type(episode.get("seasonNumber")) is not int or episode["seasonNumber"] < 0
                                or type(episode.get("hasFile")) is not bool
                                or type(episode.get("monitored")) is not bool):
                            return AdapterObservation("error", "sonarr", "tv_missing", 502, False,
                                                      {"error": "malformed_series_episode_response"}, audit)
                        if episode["hasFile"] or not episode["monitored"] or (not inc_bool and episode["seasonNumber"] == 0):
                            continue
                        air_date = episode.get("airDateUtc")
                        try:
                            aired_at = datetime.fromisoformat(air_date.replace("Z", "+00:00")) if isinstance(air_date, str) else None
                        except ValueError:
                            aired_at = None
                        if aired_at is None or aired_at.tzinfo is None:
                            return AdapterObservation("error", "sonarr", "tv_missing", 502, False,
                                                      {"error": "episode_air_date_unavailable"}, audit)
                        if aired_at <= now_utc:
                            all_records.append(episode)
                    total_records = len(all_records)
                    break
                return AdapterObservation("error", "sonarr", "tv_missing", c_ep if c_ep != 200 else 502, False,
                                          {"error": "series_episode_fallback_failed"}, audit)
            all_records.extend(records)
            total_records = int(payload.get("totalRecords", 0) or 0)
            if len(all_records) >= total_records or not records:
                break
            page += 1
        truncated = len(all_records) < total_records
        return AdapterObservation(
            "partial" if truncated else "verified_ok",
            "sonarr",
            "tv_missing",
            200,
            not truncated,
            {
                "series_id": resolved["id"],
                "title": resolved["title"],
                "include_specials": bool(args.get("include_specials", False)),
                "missing_count": total_records if truncated else len(all_records),
                "missing_episodes": all_records,
                "returned_count": len(all_records),
                "total_records": total_records,
                "truncated": truncated,
            },
            audit,
        )

    def execute_tv_search_missing(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_tv_search_missing(args, principal, idempotency_key=idempotency_key, audit=audit)
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
        changed = self._guard_confirmed_entity("tv_search_missing", args,
                                               {"series_id": resolved["id"], "title": resolved["title"], "year": resolved.get("year")}, audit)
        if changed is not None:
            return changed
        cmd_body: dict[str, Any] = {"seriesId": resolved["id"]}
        if "season" in args:
            cmd_body["name"] = "SeasonSearch"
            cmd_body["seasonNumber"] = args["season"]
        else:
            cmd_body["name"] = "MissingEpisodeSearch"
        c2, cmd_resp = self._request_json("POST", "/sonarr/api/v3/command", headers=headers, body=cmd_body, audit=audit)
        cmd_id = _valid_command_id(cmd_resp)
        if c2 not in {200, 201} or cmd_id is None:
            err_payload = (
                cmd_resp
                if c2 not in {200, 201} and isinstance(cmd_resp, dict)
                else {"error": "malformed_command_response", "outcome": "unknown", "replayed": False, "response": cmd_resp}
            )
            return AdapterObservation("error", "sonarr", "tv_search_missing", c2 if c2 not in {200, 201} else 502, False, err_payload, audit)
        c3, poll_resp = self._request_json("GET", f"/sonarr/api/v3/command/{cmd_id}", headers=headers, audit=audit)
        poll_dict = poll_resp if isinstance(poll_resp, dict) else None
        poll_status = poll_dict.get("status") if poll_dict is not None else None
        obs_status, verified = self._command_observation_status(c3 == 200 and poll_dict is not None, poll_status)
        return AdapterObservation(
            obs_status,
            "sonarr",
            "tv_search_missing",
            c3,
            verified,
            {
                "series_id": resolved["id"],
                "title": resolved["title"],
                "command_id": cmd_id,
                "command_name": cmd_body["name"],
                "command_status": poll_status,
                "idempotent_replay": bool(cmd_resp.get("idempotentReplay", False)),
            },
            audit,
        )

    def execute_movie_search(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_movie_search(args, principal, idempotency_key=idempotency_key, audit=audit)
        headers = self._auth_headers_for("radarr", principal=principal)
        code, movies = self._request_json("GET", "/radarr/api/v3/movie", headers=headers, audit=audit)
        if code != 200 or not isinstance(movies, list):
            return AdapterObservation("error", "radarr", "movie_search", code if code != 200 else 502, False, movies if isinstance(movies, dict) else {"error": "malformed_movies_response"}, audit)
        try:
            resolved = resolve_title(args["title"], movies, args.get("year"))
        except ResolutionRequired:
            matching_years = sorted(
                m["year"] for m in movies if isinstance(m, dict) and _norm(str(m.get("title", ""))) == _norm(args["title"]) and "year" in m
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
        changed = self._guard_confirmed_entity("movie_search", args,
                                               {"movie_id": resolved["id"], "title": resolved["title"], "year": resolved.get("year")}, audit)
        if changed is not None:
            return changed
        body = {"name": "MoviesSearch", "movieIds": [resolved["id"]]}
        c2, cmd_resp = self._request_json("POST", "/radarr/api/v3/command", headers=headers, body=body, audit=audit)
        cmd_id = _valid_command_id(cmd_resp)
        if c2 not in {200, 201} or cmd_id is None:
            err_payload = (
                cmd_resp
                if c2 not in {200, 201} and isinstance(cmd_resp, dict)
                else {"error": "malformed_command_response", "outcome": "unknown", "replayed": False, "response": cmd_resp}
            )
            return AdapterObservation("error", "radarr", "movie_search", c2 if c2 not in {200, 201} else 502, False, err_payload, audit)
        c3, poll_resp = self._request_json("GET", f"/radarr/api/v3/command/{cmd_id}", headers=headers, audit=audit)
        poll_dict = poll_resp if isinstance(poll_resp, dict) else None
        poll_status = poll_dict.get("status") if poll_dict is not None else None
        obs_status, verified = self._command_observation_status(c3 == 200 and poll_dict is not None, poll_status)
        return AdapterObservation(
            obs_status,
            "radarr",
            "movie_search",
            c3,
            verified,
            {
                "movie_id": resolved["id"],
                "title": resolved["title"],
                "year": resolved.get("year"),
                "command_id": cmd_id,
                "command_status": poll_status,
                "idempotent_replay": bool(cmd_resp.get("idempotentReplay", False)),
            },
            audit,
        )

    def execute_music_search(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_music_search(args, principal, idempotency_key=idempotency_key, audit=audit)
        status_obs = self.execute_music_status(args, principal, idempotency_key=idempotency_key, audit=audit)
        if status_obs.status != "verified_ok":
            return AdapterObservation(status_obs.status, "lidarr", "music_search", status_obs.http_status, False, status_obs.data, audit)
        changed = self._guard_confirmed_entity("music_search", args, status_obs.data, audit)
        if changed is not None:
            return changed
        headers = self._auth_headers_for("lidarr", principal=principal)
        if "album" in args:
            album_ids = [al["id"] for al in status_obs.data["albums"]]
            body: dict[str, Any] = {"name": "AlbumSearch", "albumIds": album_ids}
        else:
            body = {"name": "ArtistSearch", "artistId": status_obs.data["artist_id"]}
        c2, resp = self._request_json("POST", "/lidarr/api/v1/command", headers=headers, body=body, audit=audit)
        resp_dict = resp if isinstance(resp, dict) else None
        resp_status = resp_dict.get("status") if resp_dict is not None else None
        obs_status, verified = self._command_observation_status(c2 in {200, 201} and resp_dict is not None, resp_status)
        return AdapterObservation(
            obs_status,
            "lidarr",
            "music_search",
            c2,
            verified,
            {"artist": status_obs.data["artist"], "command": resp},
            audit,
        )

    def execute_now_playing(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_now_playing(args, principal, idempotency_key=idempotency_key, audit=audit)
        code, resp = self._request_json("GET", "/tautulli/api/v2?cmd=get_activity", audit=audit)
        if code != 200:
            return AdapterObservation("error", "tautulli", "now_playing", code, False, resp, audit)
        data, err = _extract_tautulli_data(resp, expected_list_key="sessions")
        if data is None:
            return AdapterObservation("error", "tautulli", "now_playing", code, False, err, audit)
        return AdapterObservation(
            "verified_ok",
            "tautulli",
            "now_playing",
            200,
            True,
            {
                "stream_count": int(data.get("stream_count", 0)),
                "total_bandwidth_kbps": data.get("total_bandwidth", 0),
                "sessions": data["sessions"],
            },
            audit,
        )

    def execute_watch_history(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_watch_history(args, principal, idempotency_key=idempotency_key, audit=audit)
        params: dict[str, Any] = {"cmd": "get_history", "length": 25}
        if "title" in args:
            params["search"] = args["title"]
        q = urllib.parse.urlencode(params)
        code, resp = self._request_json("GET", f"/tautulli/api/v2?{q}", audit=audit)
        if code != 200:
            return AdapterObservation("error", "tautulli", "watch_history", code, False, resp, audit)
        data, err = _extract_tautulli_data(resp, expected_list_key="data")
        if data is None:
            return AdapterObservation("error", "tautulli", "watch_history", code, False, err, audit)
        return AdapterObservation(
            "verified_ok",
            "tautulli",
            "watch_history",
            200,
            True,
            {"records_filtered": data.get("recordsFiltered", 0), "history": data["data"]},
            audit,
        )

    def execute_torrent_list(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_torrent_list(args, principal, idempotency_key=idempotency_key, audit=audit)
        state = args.get("state", "all")
        params: dict[str, Any] = {"limit": 100}
        if args.get("query"):
            params["search"] = args["query"]
        q = urllib.parse.urlencode(params)
        code, payload = self._request_json("GET", f"/qui/api/instances/1/torrents?{q}", audit=audit)
        if code != 200 or not isinstance(payload, dict):
            return AdapterObservation("error", "qbittorrent", "torrent_list", code, False, payload if isinstance(payload, dict) else {"error": "invalid_qui_response"}, audit)
        raw_items = payload.get("torrents") or []
        items: list[dict[str, Any]] = []
        for t in raw_items:
            if not isinstance(t, dict):
                continue
            mapped_state = _map_qui_state(str(t.get("state", "")))
            if state != "all" and mapped_state != state and str(t.get("state", "")).lower() != state.lower():
                continue
            name_val = str(t.get("name", ""))
            items.append({
                "hash": t.get("hash", ""),
                "name": name_val,
                "title_key": t.get("title_key") or name_val,
                "state": mapped_state,
                "raw_state": t.get("state", ""),
                "progress": t.get("progress", 0.0),
                "dlspeed": t.get("dlspeed", 0),
                "size": t.get("size", 0),
                "category": t.get("category", ""),
            })
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

    def _mutate_live_torrent(
        self,
        action_name: str,
        qui_action: str,
        args: dict[str, Any],
        principal: Principal,
        audit: list[dict[str, Any]],
    ) -> AdapterObservation:
        prev_effect = self._active_effect
        self._active_effect = "read"
        try:
            obs = self.execute_torrent_list({"state": "all"}, principal, idempotency_key=None, audit=audit)
        finally:
            self._active_effect = prev_effect
        if obs.status != "verified_ok":
            return AdapterObservation(obs.status, "qbittorrent", action_name, obs.http_status, False, obs.data, audit)
        target_title = _norm(args["title"])
        matches = [
            x for x in obs.data.get("torrents", [])
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
        changed = self._guard_confirmed_entity(action_name, args, matches[0], audit)
        if changed is not None:
            return changed
        target_hash = str(matches[0]["hash"])
        c2, m_resp = self._request_json(
            "POST",
            "/qui/api/instances/1/torrents/bulk-action",
            body={"action": qui_action, "hashes": [target_hash]},
            audit=audit,
        )
        if c2 not in {200, 202, 204}:
            return AdapterObservation(
                "error", "qbittorrent", action_name, c2, False,
                m_resp if isinstance(m_resp, dict) else {"error": "qui_bulk_action_failed"}, audit,
            )
        prev_effect = self._active_effect
        self._active_effect = "read"
        try:
            after_obs = self.execute_torrent_list({"state": "all"}, principal, idempotency_key=None, audit=audit)
        finally:
            self._active_effect = prev_effect
        after_match = [
            x for x in (after_obs.data.get("torrents", []) if after_obs.status == "verified_ok" else [])
            if x.get("hash") == target_hash
        ]
        after_state = after_match[0].get("state", "unknown") if after_match else "unknown"
        verified = (after_state == "paused") if qui_action == "pause" else (after_state not in {"paused", "unknown"})
        return AdapterObservation(
            "verified_ok" if verified else "dispatched",
            "qbittorrent",
            action_name,
            200 if verified else 202,
            verified,
            {
                "hash": target_hash,
                "title": matches[0].get("title_key") or matches[0].get("name"),
                "requested_action": qui_action,
                "verified_state": after_state,
            },
            audit,
        )

    def execute_torrent_pause(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_torrent_pause(args, principal, idempotency_key=idempotency_key, audit=audit)
        if not self.enable_gateway_v3:
            return AdapterObservation("unsupported", "qbittorrent", "torrent_pause", 501, False, {"error": "live_action_not_implemented"}, audit)
        return self._mutate_live_torrent("torrent_pause", "pause", args, principal, audit)

    def execute_torrent_resume(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_torrent_resume(args, principal, idempotency_key=idempotency_key, audit=audit)
        if not self.enable_gateway_v3:
            return AdapterObservation("unsupported", "qbittorrent", "torrent_resume", 501, False, {"error": "live_action_not_implemented"}, audit)
        return self._mutate_live_torrent("torrent_resume", "resume", args, principal, audit)

    def execute_request_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_request_status(args, principal, idempotency_key=idempotency_key, audit=audit)
        q = urllib.parse.urlencode({"q": args["title"]})
        code, resp = self._request_json("GET", f"/request/api/search?{q}", audit=audit)
        if code != 200 or not isinstance(resp, dict):
            return AdapterObservation("error", "requests", "request_status", code, False, resp if isinstance(resp, dict) else {}, audit)
        results = resp.get("results", [])
        target_norm = _norm(args["title"])
        exact = [r for r in results if isinstance(r, dict) and _norm(str(r.get("title", ""))) == target_norm]
        if not exact:
            return AdapterObservation(
                "clarify_required",
                "requests",
                "request_status",
                404,
                False,
                {"slot": "reference", "reason": "request_title_not_found", "title": args["title"]},
                audit,
            )
        distinct = {(r.get("type"), str(r.get("year", ""))) for r in exact}
        if len(distinct) > 1:
            return AdapterObservation(
                "clarify_required",
                "requests",
                "request_status",
                409,
                False,
                {
                    "slot": "reference",
                    "reason": "request_title_ambiguous",
                    "title": args["title"],
                    "candidates": sorted({f"{t} {y}".strip() for t, y in distinct if t}),
                },
                audit,
            )
        chosen = exact[0]
        yr_str = str(chosen.get("year", ""))
        yr_val = int(yr_str) if yr_str.isdigit() else None
        return AdapterObservation(
            "verified_ok",
            "requests",
            "request_status",
            200,
            True,
            {
                "tmdb_id": chosen.get("id"),
                "title": chosen.get("title"),
                "media_type": chosen.get("type"),
                "year": yr_val,
                "status": chosen.get("status", "none"),
                "results": results[:10],
            },
            audit,
        )

    def execute_request_media(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None,
                              audit: list[dict[str, Any]], resolve_only: bool = False) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_request_media(args, principal, idempotency_key=idempotency_key, audit=audit)
        if not self.request_owner_subject or principal.subject != self.request_owner_subject:
            # Fail closed: the gateway key would file this request as its owner, not as the caller.
            return AdapterObservation(
                "denied",
                "requests",
                "request_media",
                403,
                False,
                {"error": "request_identity_not_mapped", "subject": principal.subject},
                audit,
            )
        q = urllib.parse.urlencode({"q": args["title"]})
        code, search_resp = self._request_json("GET", f"/request/api/search?{q}", audit=audit)
        if code != 200 or not isinstance(search_resp, dict):
            return AdapterObservation("error", "requests", "request_media", code, False, search_resp if isinstance(search_resp, dict) else {}, audit)
        results = search_resp.get("results", [])
        target_norm = _norm(args["title"])
        mtype = args["media_type"]
        year_arg = args.get("year")
        title_matches = [
            r for r in results
            if isinstance(r, dict)
            and r.get("type") == mtype
            and _norm(str(r.get("title", ""))) == target_norm
        ]
        candidates = [r for r in title_matches if year_arg is None or str(r.get("year", "")) == str(year_arg)]
        if not candidates:
            # Never fall back to a different title or year for a mutation.
            return AdapterObservation(
                "clarify_required",
                "requests",
                "request_media",
                404,
                False,
                {
                    "slot": "reference",
                    "reason": "tmdb_candidate_not_found",
                    "title": args["title"],
                    "media_type": mtype,
                    "candidate_years": sorted({int(r["year"]) for r in title_matches if str(r.get("year", "")).isdigit()}),
                },
                audit,
            )
        if len(candidates) > 1:
            years = {str(r.get("year", "")) for r in candidates}
            return AdapterObservation(
                "clarify_required",
                "requests",
                "request_media",
                409,
                False,
                {
                    "slot": "reference",
                    "reason": "remake_year_ambiguous" if len(years) > 1 else "tmdb_candidate_not_unique",
                    "title": args["title"],
                    "candidate_years": sorted({int(y) for y in years if y.isdigit()}),
                },
                audit,
            )
        chosen = candidates[0]
        tmdb_id = chosen.get("id")
        if type(tmdb_id) is not int or tmdb_id <= 0:
            return AdapterObservation("error", "requests", "request_media", 502, False,
                                      {"error": "invalid_tmdb_identity"}, audit)
        yr_str = str(chosen.get("year", ""))
        yr_val = int(yr_str) if yr_str.isdigit() else year_arg
        entity = {"tmdb_id": tmdb_id, "title": chosen["title"], "media_type": mtype, "year": yr_val}
        if resolve_only:
            return AdapterObservation("verified_ok", "requests", "request_media", 200, True, entity, audit)
        changed = self._guard_confirmed_entity("request_media", args, entity, audit)
        if changed is not None:
            return changed
        if chosen.get("status") in {"available", "requested", "downloading"}:
            return AdapterObservation(
                "verified_ok",
                "requests",
                "request_media",
                200,
                True,
                {
                    "tmdb_id": tmdb_id,
                    "title": chosen["title"],
                    "media_type": mtype,
                    "year": yr_val,
                    "status": chosen["status"],
                    "already_present": True,
                },
                audit,
            )
        req_body: dict[str, Any] = {"tmdb_id": tmdb_id, "media_type": mtype}
        c2, post_resp = self._request_json("POST", "/request/api/request", body=req_body, audit=audit)
        # 409 is not a benign duplicate in this contract (e.g. `no_seer_user`); duplicates are
        # already caught by the status check above, so any conflict is reported as an error.
        if c2 in {200, 201}:
            return AdapterObservation(
                "verified_ok",
                "requests",
                "request_media",
                c2,
                True,
                {
                    "tmdb_id": tmdb_id,
                    "title": chosen["title"],
                    "media_type": mtype,
                    "year": yr_val,
                    "request_response": post_resp,
                },
                audit,
            )
        return AdapterObservation("error", "requests", "request_media", c2, False, post_resp, audit)

    def execute_seer_discover(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_seer_discover(args, principal, idempotency_key=idempotency_key, audit=audit)
        cat = args["category"]
        mtype = args["media_type"]
        if cat == "trending":
            slug = "trending"
        elif cat == "popular":
            slug = "popular-movies" if mtype == "movie" else "popular-tv"
        else:
            slug = "upcoming-movies" if mtype == "movie" else "upcoming"
        code, resp = self._request_json("GET", f"/request/api/discover/{slug}", audit=audit)
        if code != 200 or not isinstance(resp, dict):
            return AdapterObservation("error", "seer", "seer_discover", code, False, resp if isinstance(resp, dict) else {}, audit)
        # Always honour the requested media type; an empty list is the truthful answer when
        # the upstream returns only the other type.
        results = [r for r in resp.get("results", []) if isinstance(r, dict) and r.get("type") in {mtype, None}]
        return AdapterObservation(
            "verified_ok",
            "seer",
            "seer_discover",
            200,
            True,
            {"category": cat, "media_type": mtype, "slug": slug, "results": results[:20], "count": len(results),
             "source": "request_portal_discovery", "native_seerr_verified": False},
            audit,
        )

    def execute_armiger_parse(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_armiger_parse(args, principal, idempotency_key=idempotency_key, audit=audit)
        if not self.enable_gateway_v3:
            return AdapterObservation("unsupported", "armiger", "armiger_parse", 501, False,
                                      {"error": "native_armiger_integration_not_implemented"}, audit)
        rel = args["release_name"]
        parsed_meta = _parse_release_metadata(rel)
        infer_body = {
            "model": "armiger-ultra",
            "inputs": [{"name": rel, "files": [{"path": rel, "size": 0}]}],
        }
        code, submit_resp = self._request_json("POST", "/image-gen/v1/infer", body=infer_body, audit=audit)
        if code not in {200, 202} or not isinstance(submit_resp, dict):
            return AdapterObservation("error", "armiger", "armiger_parse", code, False,
                                      submit_resp if isinstance(submit_resp, dict) else {"error": "armiger_submit_failed"}, audit)
        job_id = submit_resp.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return AdapterObservation("error", "armiger", "armiger_parse", 502, False, {"error": "missing_armiger_job_id"}, audit)
        c2, wait_resp = self._request_json("GET", f"/image-gen/v1/infer/{urllib.parse.quote(job_id.strip(), safe='')}/wait?timeout=10", audit=audit)
        if c2 != 200 or not isinstance(wait_resp, dict) or wait_resp.get("status") != "completed":
            return AdapterObservation("error", "armiger", "armiger_parse", c2, False,
                                      wait_resp if isinstance(wait_resp, dict) else {"error": "armiger_wait_failed"}, audit)
        result_obj = wait_resp.get("result") if isinstance(wait_resp.get("result"), dict) else wait_resp
        preds = (
            result_obj.get("results")
            or result_obj.get("predictions")
            or wait_resp.get("results")
            or wait_resp.get("predictions")
            or []
        )
        if not isinstance(preds, list):
            preds = []
        first_pred = preds[0] if preds and isinstance(preds[0], dict) else {}
        return AdapterObservation(
            "verified_ok",
            "armiger",
            "armiger_parse",
            200,
            True,
            {
                **parsed_meta,
                "model": result_obj.get("model") or wait_resp.get("model") or "armiger-ultra",
                "version": result_obj.get("version") or wait_resp.get("version"),
                "artifact_digest": result_obj.get("artifact_digest") or wait_resp.get("artifact_digest"),
                "classification": first_pred,
            },
            audit,
        )

    def execute_bitmagnet_search(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_bitmagnet_search(args, principal, idempotency_key=idempotency_key, audit=audit)
        gql_body = {
            "query": (
                "query Search($q: String!) { torrentContent { search(input: {queryString: $q, limit: 10, "
                "hasNextPage: true, orderBy: [{field: seeders, descending: true}]}) { hasNextPage items { "
                "infoHash title contentType seeders leechers videoResolution publishedAt torrent { name size filesCount } } } } }"
            ),
            "variables": {"q": args["query"]},
        }
        code, resp = self._request_json("POST", "/bitagent/graphql", body=gql_body, audit=audit)
        if code != 200 or not isinstance(resp, dict) or "errors" in resp:
            return AdapterObservation("error", "bitmagnet", "bitmagnet_search", code, False, resp if isinstance(resp, dict) else {}, audit)
        items = resp.get("data", {}).get("torrentContent", {}).get("search", {}).get("items", [])
        return AdapterObservation(
            "verified_ok",
            "bitmagnet",
            "bitmagnet_search",
            200,
            True,
            {"query": args["query"], "count": len(items), "items": items},
            audit,
        )

    def execute_waypoint_status(self, args: dict[str, Any], principal: Principal, *, idempotency_key: str | None, audit: list[dict[str, Any]]) -> AdapterObservation:
        if self._is_sandbox_backend:
            return super().execute_waypoint_status(args, principal, idempotency_key=idempotency_key, audit=audit)
        if not self.enable_gateway_v3:
            return AdapterObservation("unsupported", "dh-waypoint", "waypoint_status", 501, False,
                                      {"error": "native_waypoint_integration_not_implemented"}, audit)
        code, resp = self._request_json("GET", "/commskip/api/v1/batches?limit=5", audit=audit)
        if code != 200 or not isinstance(resp, dict):
            return AdapterObservation("error", "dh-waypoint", "waypoint_status", code, False,
                                      resp if isinstance(resp, dict) else {"error": "waypoint_unavailable"}, audit)
        batches = resp.get("batches") if isinstance(resp.get("batches"), list) else []
        return AdapterObservation(
            "verified_ok",
            "dh-waypoint",
            "waypoint_status",
            200,
            True,
            {
                "status": "online",
                "recent_batch_count": len(batches),
                "batches": batches[:5],
            },
            audit,
        )

    def _gateway_service_result(
        self,
        service: str,
        args: dict[str, Any],
        rows: list[dict[str, Any]],
        total: int,
        truncated: bool,
        audit: list[dict[str, Any]],
        **extra: Any,
    ) -> AdapterObservation:
        op = args["operation"]
        query = args.get("query")
        filtered = bool(query and op in {"library", "queue"})
        scanned_count = len(rows)
        if filtered:
            needle = _norm(query)
            rows = [r for r in rows if needle in _norm(str(r.get("title", r.get("artistName", r.get("name", "")))))]
        max_items = 50
        capped_rows = rows[:max_items]
        if len(rows) > len(capped_rows):
            truncated = True
        data: dict[str, Any] = {
            "source": "gateway_v3",
            "operation": op,
            "items": [_public_row(r) for r in capped_rows],
            "returned_count": len(capped_rows),
            "total_records": total,
            "truncated": truncated,
            "complete_listing": not truncated,
            **extra,
        }
        if filtered:
            data.update(
                scanned_count=scanned_count,
                upstream_total_records=total,
                total_records=None if truncated else len(capped_rows),
            )
        if query:
            data["query"] = query
        return AdapterObservation(
            "partial" if truncated else "verified_ok",
            service,
            "service_query",
            200,
            not truncated,
            data,
            audit,
        )

    def execute_service_query(
        self,
        args: dict[str, Any],
        principal: Principal,
        *,
        idempotency_key: str | None,
        audit: list[dict[str, Any]],
    ) -> AdapterObservation:
        if self._is_sandbox_backend or not self.enable_gateway_v3:
            return AdapterObservation(
                "unsupported", args.get("service", "native"), "service_query", 501, False,
                {"error": "gateway_v3_service_query_disabled"}, audit,
            )
        args = validate_service_query(args)
        service = args["service"]
        op = args["operation"]
        req_perm = SERVICE_PERMISSIONS[service]
        if not principal.subject or req_perm not in principal.scopes:
            return AdapterObservation("denied", service, "service_query", 403, False, {"error": "permission_required"}, audit)

        if service in {"sonarr", "radarr", "lidarr", "readarr", "whisparr"}:
            version = "v1" if service in {"lidarr", "readarr"} else "v3"
            root = f"/{service}/api/{version}"
            entity = {"sonarr": "series", "whisparr": "series", "radarr": "movie", "lidarr": "artist", "readarr": "author"}[service]
            headers = self._auth_headers_for(service, principal=principal)
            if op == "health":
                c1, status = self._request_json("GET", f"{root}/system/status", headers=headers, audit=audit)
                if c1 != 200 or not isinstance(status, dict) or not str(status.get("version", "")).strip():
                    return AdapterObservation("error", service, "service_query", c1 if c1 != 200 else 502, False,
                                              {"error": "invalid_service_status"}, audit)
                c2, issues = self._request_json("GET", f"{root}/health", headers=headers, audit=audit)
                if c2 != 200 or not isinstance(issues, list):
                    return AdapterObservation("error", service, "service_query", c2 if c2 != 200 else 502, False,
                                              {"error": "invalid_service_health_issue"}, audit)
                issue_list = [
                    {k: r[k] for k in ("source", "type", "message") if k in r}
                    for r in issues if isinstance(r, dict)
                ]
                return self._gateway_service_result(
                    service, args, [], 0, False, audit,
                    api_version=version, server_version=status["version"],
                    healthy=not issue_list, issues=issue_list,
                )
            if op in {"queue", "history", "missing", "wanted"}:
                endpoint = "/wanted/missing" if op == "missing" else ("/wanted/cutoff" if op == "wanted" else f"/{op}")
                inc = (
                    {"includeSeries": "true"} if service in {"sonarr", "whisparr"}
                    else {"includeMovie": "true"} if service == "radarr"
                    else {"includeAuthor": "true"} if service == "readarr"
                    else {"includeArtist": "true"}
                )
                q = urllib.parse.urlencode({"page": 1, "pageSize": 50, **inc}, quote_via=urllib.parse.quote)
                code, payload = self._request_json("GET", f"{root}{endpoint}?{q}", headers=headers, audit=audit)
                if code != 200 or not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                    return AdapterObservation("error", service, "service_query", code if code != 200 else 502, False,
                                              {"error": "invalid_upstream_page"}, audit)
                rows = [r for r in payload["records"] if isinstance(r, dict)]
                if "monitored" in args and op in {"missing", "wanted"}:
                    rows = [r for r in rows if r.get("monitored") is args["monitored"]]
                total = int(payload.get("totalRecords", len(rows)))
                return self._gateway_service_result(service, args, rows, total, len(rows) < total, audit, api_version=version)
            if op in {"quality_profiles", "indexers", "calendar"}:
                endpoint = "/qualityprofile" if op == "quality_profiles" else ("/indexer" if op == "indexers" else "/calendar")
                code, rows_raw = self._request_json("GET", f"{root}{endpoint}", headers=headers, audit=audit)
                if code != 200 or not isinstance(rows_raw, list):
                    return AdapterObservation("error", service, "service_query", code if code != 200 else 502, False,
                                              {"error": "invalid_upstream_records"}, audit)
                rows = [r for r in rows_raw if isinstance(r, dict)]
                return self._gateway_service_result(service, args, rows, len(rows), len(rows) > 50, audit, api_version=version)
            if op == "search":
                q = urllib.parse.urlencode({"term": args["query"]}, quote_via=urllib.parse.quote)
                code, rows_raw = self._request_json("GET", f"{root}/{entity}/lookup?{q}", headers=headers, audit=audit)
                if code != 200 or not isinstance(rows_raw, list):
                    return AdapterObservation("error", service, "service_query", code if code != 200 else 502, False,
                                              {"error": "invalid_upstream_records"}, audit)
                rows = [r for r in rows_raw if isinstance(r, dict)]
                return self._gateway_service_result(service, args, rows, len(rows), len(rows) > 50, audit, api_version=version)
            # op == "library"
            code, rows_raw = self._request_json("GET", f"{root}/{entity}", headers=headers, audit=audit)
            if code != 200 or not isinstance(rows_raw, list):
                return AdapterObservation("error", service, "service_query", code if code != 200 else 502, False,
                                          {"error": "invalid_upstream_records"}, audit)
            rows = [r for r in rows_raw if isinstance(r, dict)]
            if "monitored" in args:
                rows = [r for r in rows if r.get("monitored") is args["monitored"]]
            return self._gateway_service_result(
                service, args, rows, len(rows), False if args.get("query") else (len(rows) > 50),
                audit, api_version=version,
            )

        if service == "prowlarr":
            root = "/prowlarr/api/v1"
            headers = self._auth_headers_for("prowlarr", principal=principal)
            if op == "health":
                c1, status = self._request_json("GET", f"{root}/system/status", headers=headers, audit=audit)
                if c1 != 200 or not isinstance(status, dict) or not str(status.get("version", "")).strip():
                    return AdapterObservation("error", "prowlarr", "service_query", c1 if c1 != 200 else 502, False,
                                              {"error": "invalid_service_status"}, audit)
                c2, issues = self._request_json("GET", f"{root}/health", headers=headers, audit=audit)
                if c2 != 200 or not isinstance(issues, list):
                    return AdapterObservation("error", "prowlarr", "service_query", c2 if c2 != 200 else 502, False,
                                              {"error": "invalid_service_health_issue"}, audit)
                issue_list = [{k: r[k] for k in ("source", "type", "message") if k in r} for r in issues if isinstance(r, dict)]
                return self._gateway_service_result(
                    "prowlarr", args, [], 0, False, audit,
                    api_version="v1", server_version=status["version"], healthy=not issue_list, issues=issue_list,
                )
            if op in {"indexers", "applications"}:
                code, rows_raw = self._request_json("GET", f"{root}/{'indexer' if op == 'indexers' else 'applications'}", headers=headers, audit=audit)
                if code != 200 or not isinstance(rows_raw, list):
                    return AdapterObservation("error", "prowlarr", "service_query", code if code != 200 else 502, False,
                                              {"error": "invalid_upstream_records"}, audit)
                rows = [r for r in rows_raw if isinstance(r, dict)]
                return self._gateway_service_result("prowlarr", args, rows, len(rows), len(rows) > 50, audit, api_version="v1")
            if op == "history":
                q = urllib.parse.urlencode({"page": 1, "pageSize": 50}, quote_via=urllib.parse.quote)
                code, payload = self._request_json("GET", f"{root}/history?{q}", headers=headers, audit=audit)
                if code != 200 or not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                    return AdapterObservation("error", "prowlarr", "service_query", code if code != 200 else 502, False,
                                              {"error": "invalid_upstream_page"}, audit)
                rows = [r for r in payload["records"] if isinstance(r, dict)]
                total = int(payload.get("totalRecords", len(rows)))
                return self._gateway_service_result("prowlarr", args, rows, total, len(rows) < total, audit, api_version="v1")
            q = urllib.parse.urlencode({"query": args["query"], "type": "search"}, quote_via=urllib.parse.quote)
            code, rows_raw = self._request_json("GET", f"{root}/search?{q}", headers=headers, audit=audit)
            if code != 200 or not isinstance(rows_raw, list):
                return AdapterObservation("error", "prowlarr", "service_query", code if code != 200 else 502, False,
                                          {"error": "invalid_upstream_records"}, audit)
            rows = [r for r in rows_raw if isinstance(r, dict)]
            return self._gateway_service_result("prowlarr", args, rows, len(rows), len(rows) > 50, audit, api_version="v1")

        if service == "tautulli":
            if op == "health":
                code, resp = self._request_json("GET", "/tautulli/api/v2?cmd=get_server_info", audit=audit)
                if code != 200 or not isinstance(resp, dict):
                    return AdapterObservation("error", "tautulli", "service_query", code if code != 200 else 502, False,
                                              {"error": "tautulli_application_error"}, audit)
                r_obj = resp.get("response") if isinstance(resp.get("response"), dict) else {}
                data = r_obj.get("data") if isinstance(r_obj.get("data"), dict) else {}
                if r_obj.get("result") != "success":
                    return AdapterObservation("error", "tautulli", "service_query", 502, False,
                                              {"error": "tautulli_application_error"}, audit)
                ver = str(data.get("pms_version") or data.get("tautulli_version") or "online")
                return self._gateway_service_result(
                    "tautulli", args, [], 0, False, audit,
                    api_version="v2", server_version=ver, pms_name=data.get("pms_name"), healthy=True,
                )
            if op == "activity":
                code, resp = self._request_json("GET", "/tautulli/api/v2?cmd=get_activity", audit=audit)
                if code != 200:
                    return AdapterObservation("error", "tautulli", "service_query", code, False, {"error": "upstream_http_error"}, audit)
                data, err = _extract_tautulli_data(resp, expected_list_key="sessions")
                if data is None:
                    return AdapterObservation("error", "tautulli", "service_query", 502, False, err, audit)
                sessions = [r for r in data["sessions"] if isinstance(r, dict)]
                return self._gateway_service_result(
                    "tautulli", args, sessions, len(sessions), len(sessions) > 50, audit,
                    api_version="v2", stream_count=data.get("stream_count", len(sessions)),
                )
            q = urllib.parse.urlencode({"cmd": "get_history", "length": 50, "order_column": "date", "order_dir": "desc"})
            code, resp = self._request_json("GET", f"/tautulli/api/v2?{q}", audit=audit)
            if code != 200:
                return AdapterObservation("error", "tautulli", "service_query", code, False, {"error": "upstream_http_error"}, audit)
            data, err = _extract_tautulli_data(resp, expected_list_key="data")
            if data is None:
                return AdapterObservation("error", "tautulli", "service_query", 502, False, err, audit)
            rows = [r for r in data["data"] if isinstance(r, dict)]
            total = int(data.get("recordsFiltered", len(rows)))
            return self._gateway_service_result("tautulli", args, rows, total, len(rows) < total, audit, api_version="v2")

        if service == "bitmagnet":
            if op == "health":
                gql_body = {"query": "{ version health { status checks { key status } } }"}
                code, resp = self._request_json("POST", "/bitagent/graphql", body=gql_body, audit=audit)
                if code != 200 or not isinstance(resp, dict) or "errors" in resp:
                    return AdapterObservation("error", "bitmagnet", "service_query", code if code != 200 else 502, False,
                                              {"error": "bitmagnet_health_failed"}, audit)
                gdata = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                health = gdata.get("health") if isinstance(gdata.get("health"), dict) else {}
                return self._gateway_service_result(
                    "bitmagnet", args, [], 0, False, audit,
                    api_version="graphql", server_version=gdata.get("version", "unknown"),
                    status=health.get("status", "unknown"), healthy=(health.get("status") == "up"),
                    checks=health.get("checks", []),
                )
            obs = self.execute_bitmagnet_search({"query": args["query"]}, principal, idempotency_key=idempotency_key, audit=audit)
            if obs.status != "verified_ok":
                return AdapterObservation(obs.status, "bitmagnet", "service_query", obs.http_status, False, obs.data, audit)
            items = obs.data.get("items", [])
            return AdapterObservation(
                "verified_ok", "bitmagnet", "service_query", 200, True,
                {
                    "source": "gateway_v3",
                    "operation": "search",
                    "query": args["query"],
                    "items": items,
                    "returned_count": len(items),
                    "total_records": len(items),
                    "truncated": False,
                    "complete_listing": True,
                },
                audit,
            )

        if service == "qbittorrent":
            params: dict[str, Any] = {"limit": 1 if op == "health" else 100}
            if args.get("query"):
                params["search"] = args["query"]
            q = urllib.parse.urlencode(params)
            code, payload = self._request_json("GET", f"/qui/api/instances/1/torrents?{q}", audit=audit)
            if code != 200 or not isinstance(payload, dict):
                return AdapterObservation("error", "qbittorrent", "service_query", code if code != 200 else 502, False,
                                          {"error": "invalid_qui_response"}, audit)
            raw_items = [t for t in (payload.get("torrents") or []) if isinstance(t, dict)]
            total = int(payload.get("total", len(raw_items)))
            if op == "health":
                return self._gateway_service_result(
                    "qbittorrent", args, [], 0, False, audit,
                    api_version="qui-v1", server_version="qui-gateway", healthy=True, total_torrents=total,
                )
            if op == "activity":
                dl_speed = sum(int(t.get("dlspeed", 0) or 0) for t in raw_items)
                up_speed = sum(int(t.get("upspeed", 0) or 0) for t in raw_items)
                return self._gateway_service_result(
                    "qbittorrent", args, [], 0, False, audit,
                    api_version="qui-v1",
                    transfer={"dl_info_speed": dl_speed, "up_info_speed": up_speed, "active_torrents": len(raw_items)},
                )
            rows = [
                {
                    "hash": t.get("hash", ""),
                    "name": str(t.get("name", "")),
                    "state": _map_qui_state(str(t.get("state", ""))),
                    "progress": t.get("progress", 0.0),
                    "dlspeed": t.get("dlspeed", 0),
                    "upspeed": t.get("upspeed", 0),
                    "size": t.get("size", 0),
                    "category": t.get("category", ""),
                }
                for t in raw_items
            ]
            return self._gateway_service_result("qbittorrent", args, rows, total, len(rows) < total, audit, api_version="qui-v1")

        if service == "seerr":
            if op == "health":
                code, resp = self._request_json("GET", "/request/api/discover/trending", audit=audit)
                if code != 200 or not isinstance(resp, dict):
                    return AdapterObservation("error", "seerr", "service_query", code if code != 200 else 502, False,
                                              {"error": "invalid_service_status"}, audit)
                return self._gateway_service_result(
                    "seerr", args, [], 0, False, audit,
                    api_version="request-portal-v1", server_version="request-portal", healthy=True,
                )
            q = urllib.parse.urlencode({"q": args["query"]})
            code, resp = self._request_json("GET", f"/request/api/search?{q}", audit=audit)
            if code != 200 or not isinstance(resp, dict) or not isinstance(resp.get("results"), list):
                return AdapterObservation("error", "seerr", "service_query", code if code != 200 else 502, False,
                                          {"error": "invalid_upstream_page"}, audit)
            rows = [
                {
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "name": r.get("title") or r.get("name"),
                    "mediaType": r.get("type") or r.get("mediaType"),
                    "year": r.get("year"),
                    "status": r.get("status"),
                }
                for r in resp["results"] if isinstance(r, dict)
            ]
            return self._gateway_service_result("seerr", args, rows, len(rows), False, audit, api_version="request-portal-v1")

        return AdapterObservation("error", service, "service_query", 503, False, {"error": "service_not_configured"}, audit)

    def _preflight_write_resolution(
        self,
        call: Call,
        principal: Principal,
        audit: list[dict[str, Any]],
    ) -> tuple[AdapterObservation | None, dict[str, Any], str]:
        """Run read-only entity resolution before minting a confirmation token or logging a shadow write."""
        name = call.name
        args = call.arguments
        prev_effect = self._active_effect
        self._active_effect = "read"
        try:
            if name == "tv_search_missing":
                obs = self.execute_tv_status({"title": args["title"]}, principal, idempotency_key=None, audit=audit)
                if obs.status != "verified_ok":
                    return AdapterObservation(obs.status, "sonarr", name, obs.http_status, False, obs.data, audit), {}, ""
                season_txt = f" (Season {args['season']})" if "season" in args else " (all seasons)"
                summary = f"Trigger Sonarr missing-episode indexer search for '{obs.data['title']}'{season_txt}"
                return None, obs.data, summary
            if name == "movie_search":
                lookup_args = {"title": args["title"]}
                if "year" in args:
                    lookup_args["year"] = args["year"]
                obs = self.execute_movie_status(lookup_args, principal, idempotency_key=None, audit=audit)
                if obs.status != "verified_ok":
                    return AdapterObservation(obs.status, "radarr", name, obs.http_status, False, obs.data, audit), {}, ""
                summary = f"Trigger Radarr indexer search for '{obs.data['title']}' ({obs.data['year']})"
                return None, obs.data, summary
            if name == "music_search":
                obs = self.execute_music_status(args, principal, idempotency_key=None, audit=audit)
                if obs.status != "verified_ok":
                    return AdapterObservation(obs.status, "lidarr", name, obs.http_status, False, obs.data, audit), {}, ""
                target_desc = f"album '{args['album']}' by {obs.data['artist']}" if "album" in args else f"artist '{obs.data['artist']}'"
                summary = f"Trigger Lidarr indexer search for {target_desc}"
                return None, obs.data, summary
            if name in {"torrent_pause", "torrent_resume"}:
                obs = self.execute_torrent_list({"state": "all"}, principal, idempotency_key=None, audit=audit)
                if obs.status != "verified_ok":
                    return AdapterObservation(obs.status, "qbittorrent", name, obs.http_status, False, obs.data, audit), {}, ""
                target_title = _norm(args["title"])
                matches = [
                    x for x in obs.data.get("torrents", [])
                    if _norm(x.get("title_key", "")) == target_title or _norm(x.get("name", "")) == target_title
                ]
                if len(matches) != 1:
                    return (
                        AdapterObservation(
                            "clarify_required",
                            "qbittorrent",
                            name,
                            404,
                            False,
                            {"slot": "reference", "reason": "torrent_not_uniquely_resolved", "title": args["title"]},
                            audit,
                        ),
                        {},
                        "",
                    )
                verb = "Pause" if name == "torrent_pause" else "Resume"
                summary = f"{verb} qBittorrent transfer '{matches[0].get('title_key') or matches[0].get('name')}' ({matches[0]['hash']})"
                return None, matches[0], summary
            if name == "request_media":
                if not self._is_sandbox_backend and (
                    not self.request_owner_subject or principal.subject != self.request_owner_subject
                ):
                    return (
                        AdapterObservation(
                            "denied", "requests", name, 403, False,
                            {"error": "request_identity_not_mapped", "subject": principal.subject}, audit,
                        ),
                        {},
                        "",
                    )
                if not self._is_sandbox_backend:
                    obs = self.execute_request_media(args, principal, idempotency_key=None, audit=audit, resolve_only=True)
                    if obs.status != "verified_ok":
                        return obs, {}, ""
                    entity = obs.data
                    yr = f" ({entity['year']})" if entity.get("year") is not None else ""
                    summary = f"Submit new {entity['media_type']} request for '{entity['title']}'{yr}"
                    return None, entity, summary
                yr = f" ({args['year']})" if "year" in args else ""
                summary = f"Submit new {args['media_type']} request for '{args['title']}'{yr}"
                return None, {"title": args["title"], "media_type": args["media_type"], "year": args.get("year")}, summary
            return None, {}, f"Execute {name}"
        finally:
            self._active_effect = prev_effect

    def execute(
        self,
        call: Call,
        principal: Principal,
        *,
        idempotency_key: str | None = None,
        bypass_write_gate: bool = False,
    ) -> AdapterObservation:
        with self._execution_lock:
            return self._execute_locked(call, principal, idempotency_key=idempotency_key,
                                        bypass_write_gate=bypass_write_gate)

    def _execute_locked(
        self, call: Call, principal: Principal, *, idempotency_key: str | None,
        bypass_write_gate: bool,
    ) -> AdapterObservation:
        norm = normalized(call)
        name = norm["name"]
        args = norm["arguments"]
        spec = V3_BY_NAME.get(name)
        service = spec.service if spec else "unknown"
        effect = spec.effect if spec else "read"
        self._active_effect = effect

        if name in LIVE_UNSUPPORTED_WRITES and not self._is_sandbox_backend and not self.enable_gateway_v3:
            return AdapterObservation(
                status="unsupported",
                service=service,
                operation=name,
                http_status=501,
                verified_complete=False,
                data={"error": "live_action_not_implemented", "action": name},
                audit_trail=[],
            )

        if effect != "read" and not bypass_write_gate and self.write_mode in {"confirm", "shadow"}:
            audit: list[dict[str, Any]] = []
            early_obs, resolved_entity, summary = self._preflight_write_resolution(Call(name, args), principal, audit)
            if early_obs is not None:
                return early_obs
            if self.write_mode == "shadow":
                audit.append({"method": "SHADOW_WRITE", "path": f"/shadow/{service}/{name}", "status": 200, "attempt": 1})
                return AdapterObservation(
                    status="shadow_logged",
                    service=service,
                    operation=name,
                    http_status=200,
                    verified_complete=False,
                    data={
                        "shadow_mode": True,
                        "action": name,
                        "arguments": args,
                        "summary": summary,
                        "resolved_entity": resolved_entity,
                    },
                    audit_trail=audit,
                )
            tok = self.confirmation_store.mint(
                principal,
                Call(name, args),
                summary=summary,
                resolved_entity=resolved_entity,
            )
            return AdapterObservation(
                status="confirmation_required",
                service=service,
                operation=name,
                http_status=202,
                verified_complete=False,
                data={
                    "confirm_id": tok.confirm_id,
                    "action": name,
                    "arguments": args,
                    "summary": tok.summary,
                    "resolved_entity": tok.resolved_entity,
                    "expires_at": round(tok.expires_at, 3),
                },
                audit_trail=audit,
            )

        return super().execute(call, principal, idempotency_key=idempotency_key)

    def confirm_write(
        self,
        confirm_id: str,
        principal: Principal,
        *,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> AdapterObservation:
        with self._execution_lock:
            return self._confirm_write_locked(confirm_id, principal, idempotency_key=idempotency_key, now=now)

    def _confirm_write_locked(
        self, confirm_id: str, principal: Principal, *,
        idempotency_key: str | None, now: float | None,
    ) -> AdapterObservation:
        tok, err = self.confirmation_store.redeem(confirm_id, principal, now=now)
        if err is not None or tok is None:
            status_code = 403 if err == "confirmation_subject_mismatch" else (409 if err == "confirmation_already_consumed" else 404)
            return AdapterObservation(
                status="denied" if status_code == 403 else "error",
                service="control",
                operation="confirm_write",
                http_status=status_code,
                verified_complete=False,
                data={"error": err or "confirmation_failed", "confirm_id": confirm_id},
                audit_trail=[],
            )
        call = Call(tok.call_name, tok.arguments)
        # Re-authorize the redeemed call against the caller's current permissions.
        if prepare(call, principal)["status"] == "denied":
            return AdapterObservation(
                status="denied",
                service=V3_BY_NAME[call.name].service if call.name in V3_BY_NAME else "control",
                operation=call.name,
                http_status=403,
                verified_complete=False,
                data={"error": "permission_required", "confirm_id": confirm_id},
                audit_trail=[],
            )
        self._confirmed_entity = tok.resolved_entity
        try:
            observation = self.execute(call, principal, idempotency_key=idempotency_key or confirm_id, bypass_write_gate=True)
            observation.data = {**observation.data, "confirmed_call": normalized(call)}
            return observation
        finally:
            self._confirmed_entity = None
