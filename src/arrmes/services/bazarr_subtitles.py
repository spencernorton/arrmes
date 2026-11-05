"""Confirmed, exact-target manual subtitle search/download for Bazarr 1.6.2.

Manual provider search can refresh Bazarr's index and consume provider resources.
It is a confirmed action, separate from the read-only service_query adapter.
Download returns 204 after queuing a job. Matching metadata is a separate state
observation, not completion evidence for that candidate's job. Opaque provider
cache keys remain server-held and are never user args.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, normalized
from .native import ServiceQueryAdapter, _norm, _UpstreamError

CONFIRMATION_PREFIX = "native-bazarr:"
SUBTITLE_ACTIONS = frozenset({"bazarr_subtitle_search", "bazarr_subtitle_download"})
SUPPORTED_VERSION = "1.6.2"
_CANDIDATE = re.compile(r"[a-f0-9]{24}")


def validate_bazarr_subtitle_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Strict cross-field validation, also usable at the protocol boundary."""
    if not isinstance(name, str) or name not in SUBTITLE_ACTIONS or not isinstance(arguments, dict):
        raise InvalidCall("Unknown subtitle action")
    required = {"media_type", "title", "language", "forced", "hearing_impaired"}
    allowed = required | {"year", "season", "episode"}
    if name == "bazarr_subtitle_download":
        required.add("candidate_id")
        allowed.add("candidate_id")
    if not required <= set(arguments) or set(arguments) - allowed:
        raise InvalidCall("Invalid subtitle arguments")
    kind, title, language = (arguments.get(k) for k in ("media_type", "title", "language"))
    if kind not in ("movie", "episode"):
        raise InvalidCall("Explicit movie or episode scope required")
    if (not isinstance(title, str) or not title.strip() or title != title.strip()
            or len(title) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in title)):
        raise InvalidCall("Invalid subtitle target title")
    if not isinstance(language, str) or re.fullmatch(r"[a-z]{2}", language) is None:
        raise InvalidCall("Explicit two-letter language code required")
    if any(type(arguments[k]) is not bool for k in ("forced", "hearing_impaired")):
        raise InvalidCall("Explicit boolean subtitle flags required")
    if "year" in arguments and (type(arguments["year"]) is not int or not 1870 <= arguments["year"] <= 2200):
        raise InvalidCall("Invalid subtitle target year")
    if kind == "episode":
        if (type(arguments.get("season")) is not int or not 0 <= arguments["season"] <= 1000
                or type(arguments.get("episode")) is not int or not 1 <= arguments["episode"] <= 10000):
            raise InvalidCall("Explicit episode coordinates required")
    elif "season" in arguments or "episode" in arguments:
        raise InvalidCall("Movie action cannot contain episode coordinates")
    if name == "bazarr_subtitle_download" and (not isinstance(arguments["candidate_id"], str)
            or _CANDIDATE.fullmatch(arguments["candidate_id"]) is None):
        raise InvalidCall("Invalid subtitle candidate reference")
    return dict(arguments)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _flag(value: Any) -> bool:
    if type(value) is bool:
        return value
    if value in ("True", "False"):
        return value == "True"
    raise _UpstreamError("invalid_subtitle_boolean")


def _text(value: Any, maximum: int = 200) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _start_time(value: Any) -> bool:
    # Bazarr sets its process start time with time.time() (bazarr/init.py), so /api/system/status
    # reports epoch seconds such as 1793829434.0899186.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0
    return _text(value, 120)


def _obs(status: str, operation: str, code: int, data: dict[str, Any], audit, *, complete: bool = False):
    return AdapterObservation(status, "bazarr", operation, code, complete, data, audit)


@dataclass(frozen=True)
class _Candidate:
    candidate_id: str
    subject: str
    arguments: dict[str, Any]
    target: dict[str, Any]
    provider: str
    subtitle: str
    original_format: bool
    expires_at: float

    def fingerprint(self) -> str:
        return _digest(self.__dict__)


class NativeBazarrSubtitleAdapter:
    """Two confirmed actions sharing the standard caller-bound HMAC store."""

    def __init__(self, native: ServiceQueryAdapter, *, write_mode: str = "confirm",
                 confirmation_store=None, clock: Callable[[], float] = time.time,
                 candidate_ttl_seconds: float = 300, max_candidates: int = 128,
                 max_candidates_per_subject: int = 32) -> None:
        if write_mode not in {"confirm", "shadow", "disabled"}:
            raise ValueError("Bazarr write mode must be confirm, shadow, or disabled")
        if (type(candidate_ttl_seconds) not in {int, float} or not math.isfinite(candidate_ttl_seconds)
                or not 0 < candidate_ttl_seconds <= 300 or type(max_candidates) is not int
                or not 1 <= max_candidates <= 512 or type(max_candidates_per_subject) is not int
                or not 1 <= max_candidates_per_subject <= max_candidates):
            raise ValueError("Invalid subtitle candidate bounds")
        from arrmes.live_adapter import WriteConfirmationStore
        self.native, self.write_mode = native, write_mode
        self.confirmation_store = confirmation_store or WriteConfirmationStore(clock=clock)
        self.clock, self.candidate_ttl_seconds = clock, float(candidate_ttl_seconds)
        self.max_candidates, self.max_candidates_per_subject = max_candidates, max_candidates_per_subject
        self._candidates: dict[str, _Candidate] = {}
        self._candidate_lock = threading.Lock()

    @staticmethod
    def _authorized(principal: Principal) -> bool:
        return bool(principal.subject and "media:manage" in principal.scopes)

    def _catalogue(self, endpoint: str, audit) -> list[dict[str, Any]]:
        rows, _, incomplete = self.native._paged(lambda page: self.native._get("bazarr", endpoint + "?" + urllib.parse.urlencode({
            "start": page * self.native.page_size, "length": self.native.page_size}), audit), records_key="data", total_key="total")
        if incomplete:
            raise _UpstreamError("subtitle_entity_catalogue_incomplete", 409)
        return rows

    @staticmethod
    def _unique(rows, *, title: str, year: int | None, identity: str):
        if (any(not _positive(r.get(identity)) or not _text(r.get("title"))
                or (r.get("year") is not None and (type(r["year"]) not in {str, int}
                    or re.fullmatch(r"\d{4}", str(r["year"])) is None)) for r in rows)
                or len({r[identity] for r in rows}) != len(rows)):
            raise _UpstreamError("invalid_subtitle_entity_catalogue")
        exact = [r for r in rows if _norm(r["title"]) == _norm(title)]
        if year is not None:
            exact = [r for r in exact if str(r.get("year")) == str(year)]
        if len(exact) != 1:
            raise _UpstreamError("subtitle_target_not_uniquely_resolved", 409)
        return exact[0]

    def _resolve(self, args, audit) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = self.native._get("bazarr", "/api/system/status", audit)
        status = payload.get("data") if isinstance(payload, dict) else None
        version = status.get("bazarr_version") if isinstance(status, dict) else None
        if not isinstance(version, str) or version not in {SUPPORTED_VERSION, "v" + SUPPORTED_VERSION}:
            raise _UpstreamError("unsupported_bazarr_version", 501)
        if not _start_time(status.get("start_time")):
            raise _UpstreamError("invalid_bazarr_process_identity")
        languages = self.native._get("bazarr", "/api/system/languages", audit)
        if not isinstance(languages, list) or len(languages) > 500 or any(not isinstance(r, dict) for r in languages):
            raise _UpstreamError("invalid_bazarr_languages")
        enabled = [r for r in languages if r.get("code2") == args["language"] and r.get("enabled") is True]
        if len(enabled) != 1 or not _text(enabled[0].get("name")):
            raise _UpstreamError("subtitle_language_not_uniquely_enabled", 409)
        target = {"service": "bazarr", "version": SUPPORTED_VERSION, "server_start_time": status["start_time"],
                  "origin_sha256": _digest(self.native.configs["bazarr"].base_url), "media_type": args["media_type"],
                  "language": args["language"], "language_definition_sha256": _digest(enabled[0]),
                  "forced": args["forced"], "hearing_impaired": args["hearing_impaired"]}
        if args["media_type"] == "movie":
            row = self._unique(self._catalogue("/api/movies", audit), title=args["title"], year=args.get("year"), identity="radarrId")
            target.update(movie_id=row["radarrId"], title=row["title"], year=row.get("year"))
        else:
            series = self._unique(self._catalogue("/api/series", audit), title=args["title"], year=args.get("year"), identity="sonarrSeriesId")
            payload = self.native._get("bazarr", "/api/episodes?" + urllib.parse.urlencode({"seriesid[]": series["sonarrSeriesId"]}), audit)
            rows = payload.get("data") if isinstance(payload, dict) else None
            if (not isinstance(rows, list) or len(rows) > self.native.max_records or any(not isinstance(r, dict)
                    or not _positive(r.get("sonarrEpisodeId")) or not _positive(r.get("sonarrSeriesId"))
                    or r["sonarrSeriesId"] != series["sonarrSeriesId"]
                    or type(r.get("season")) is not int or type(r.get("episode")) is not int for r in rows)
                    or len({r["sonarrEpisodeId"] for r in rows}) != len(rows)):
                raise _UpstreamError("invalid_bazarr_episode_catalogue")
            # Provider operations address the canonical episode ID, not its
            # season/episode coordinates. Validate the whole bounded response
            # before selecting a target, including rows at other positions.
            exact = [r for r in rows if r["season"] == args["season"] and r["episode"] == args["episode"]]
            if len(exact) != 1:
                raise _UpstreamError("subtitle_episode_not_uniquely_resolved", 409)
            row = dict(exact[0])
            target.update(series_id=series["sonarrSeriesId"], episode_id=row["sonarrEpisodeId"], title=series["title"],
                          year=series.get("year"), season=args["season"], episode=args["episode"])
            # Series profile changes must invalidate an episode confirmation.
            if not _positive(series.get("profileId")):
                raise _UpstreamError("subtitle_language_profile_missing", 409)
            target["series_profile_id"] = series["profileId"]
            # The pinned episodes response omits profileId from its marshalled
            # fields; the authoritative episode profile is its resolved series.
            row["profileId"] = series["profileId"]
        if not _text(row.get("path"), 8192) or not _positive(row.get("profileId")):
            raise _UpstreamError("subtitle_media_or_language_profile_missing", 409)
        target.update(path_sha256=_digest(row["path"]), profile_id=row["profileId"])
        profiles = self.native._get("bazarr", "/api/system/languages/profiles", audit)
        if (not isinstance(profiles, list) or len(profiles) > 500 or any(not isinstance(p, dict)
                or not _positive(p.get("profileId")) for p in profiles)
                or len({p["profileId"] for p in profiles}) != len(profiles)):
            raise _UpstreamError("invalid_bazarr_language_profiles")
        matching = [p for p in profiles if p["profileId"] == row["profileId"]]
        if len(matching) != 1:
            raise _UpstreamError("subtitle_language_profile_missing", 409)
        profile = matching[0]
        items = profile.get("items")
        if (not isinstance(items, list) or len(items) > 200 or any(not isinstance(i, dict)
                or not isinstance(i.get("language"), str) or re.fullmatch(r"[a-z]{2}", i["language"]) is None
                or i.get("forced") not in ("True", "False") or i.get("hi") not in ("True", "False", "Excluded") for i in items)):
            raise _UpstreamError("invalid_bazarr_language_profile_items")
        compatible = [i for i in items if i["language"] == args["language"] and i["forced"] == str(args["forced"])
                      and (i["hi"] == "False" or (i["hi"] == "True" and args["hearing_impaired"])
                           or (i["hi"] == "Excluded" and not args["hearing_impaired"]))]
        if not compatible:
            raise _UpstreamError("subtitle_language_flags_not_in_media_profile", 409)
        target["profile_sha256"] = _digest(profile)
        return target, row

    @staticmethod
    def _search_args(args):
        return {k: v for k, v in args.items() if k != "candidate_id"}

    def _prune_locked(self, now):
        if not math.isfinite(now):
            raise ValueError("Candidate time must be finite")
        for key in [k for k, v in self._candidates.items() if now >= v.expires_at]:
            self._candidates.pop(key, None)

    def _candidate(self, candidate_id, principal, args, target, *, claim=False):
        with self._candidate_lock:
            self._prune_locked(self.clock())
            item = self._candidates.get(candidate_id)
            if (item is None or item.subject != principal.subject or item.arguments != self._search_args(args)
                    or item.target != target):
                return None
            if claim:
                self._candidates.pop(candidate_id)
            return item

    @staticmethod
    def _existing_subtitles(row, args, *, require_size=False):
        rows = row.get("subtitles")
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise _UpstreamError("invalid_bazarr_subtitle_metadata")
        matches = []
        for sub in rows:
            if sub.get("code2") != args["language"]:
                continue
            if type(sub.get("forced")) is not bool or type(sub.get("hi")) is not bool:
                raise _UpstreamError("invalid_bazarr_subtitle_metadata")
            if (sub["forced"] == args["forced"] and sub["hi"] == args["hearing_impaired"]
                    and _text(sub.get("path"), 8192) and sub.get("embedded_track_id") is None):
                if require_size and not _positive(sub.get("file_size")):
                    continue
                matches.append(_digest({k: sub.get(k) for k in ("id", "path", "code2", "forced", "hi", "file_size")}))
        return set(matches)

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        args = validate_bazarr_subtitle_arguments(call.name, call.arguments)
        call = Call(call.name, args)
        audit: list[dict[str, Any]] = []
        if not self._authorized(principal):
            return _obs("denied", call.name, 403, {"error": "permission_required"}, audit)
        if self.write_mode == "disabled":
            return _obs("denied", call.name, 403, {"error": "writes_disabled"}, audit)
        if "bazarr" not in self.native.configs:
            return _obs("error", call.name, 503, {"error": "service_not_configured"}, audit)
        try:
            target, row = self._resolve(args, audit)
            if call.name == "bazarr_subtitle_download":
                candidate = self._candidate(args["candidate_id"], principal, args, target)
                if candidate is None:
                    return _obs("clarify_required", call.name, 409, {"slot": "reference", "reason": "subtitle_candidate_expired_or_mismatched"}, audit)
                if self._existing_subtitles(row, args):
                    return _obs("clarify_required", call.name, 409, {"slot": "scope", "reason": "matching_external_subtitle_already_present"}, audit)
                target.update(candidate_id=candidate.candidate_id, candidate_sha256=candidate.fingerprint())
            summary = ("Search providers (may refresh metadata)" if call.name.endswith("search") else "Download selected subtitle")
            summary += f" for {target['media_type']} '{target['title']}', language={args['language']}, forced={args['forced']}, HI={args['hearing_impaired']}"
            if target.get("year") is not None:
                summary += f", year={target['year']}"
            if args["media_type"] == "episode":
                summary += f", S{args['season']:02d}E{args['episode']:02d}"
            if call.name == "bazarr_subtitle_download":
                summary += f", provider={candidate.provider}, candidate={candidate.candidate_id}, original_format={candidate.original_format}"
            if self.write_mode == "shadow":
                return _obs("shadow_logged", call.name, 200, {"summary": summary, "resolved_entity": target}, audit)
            token = self.confirmation_store.mint(principal, call, summary=summary, resolved_entity=target)
            return _obs("confirmation_required", call.name, 200,
                        {"confirm_id": CONFIRMATION_PREFIX + token.confirm_id, "summary": summary,
                         "expires_at": token.expires_at, "resolved_entity": target}, audit)
        except _UpstreamError as exc:
            return self._failure(call.name, exc, audit)

    @staticmethod
    def _failure(operation, exc, audit):
        if exc.status == 409:
            slot = "language" if "language" in exc.code else "reference"
            return _obs("clarify_required", operation, 409, {"slot": slot, "reason": exc.code}, audit)
        return _obs("error", operation, exc.status, {"error": exc.code}, audit)

    def _search(self, args, target, principal, audit):
        kind = "movies" if args["media_type"] == "movie" else "episodes"
        query = {"radarrid": target["movie_id"]} if kind == "movies" else {"episodeid": target["episode_id"]}
        payload = self.native._get("bazarr", "/api/providers/" + kind + "?" + urllib.parse.urlencode(query), audit)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > self.native.max_records or any(not isinstance(r, dict) for r in rows):
            raise _UpstreamError("invalid_bazarr_provider_candidates")
        selected = []
        seen = set()
        for row in rows:
            if row.get("language") != args["language"]:
                continue
            if _flag(row.get("forced")) != args["forced"] or _flag(row.get("hearing_impaired")) != args["hearing_impaired"]:
                continue
            subtitle = row.get("subtitle")
            try:
                valid_uuid = isinstance(subtitle, str) and str(uuid.UUID(subtitle, version=4)) == subtitle
            except ValueError:
                valid_uuid = False
            if not valid_uuid or not _text(row.get("provider"), 100) or re.fullmatch(r"[A-Za-z0-9_-]+", row["provider"]) is None:
                raise _UpstreamError("invalid_bazarr_provider_candidate_identity")
            identity = (row["provider"], subtitle)
            if identity in seen:
                raise _UpstreamError("duplicate_bazarr_provider_candidate")
            seen.add(identity)
            selected.append((row, _flag(row.get("original_format"))))
        now = self.clock()
        if not math.isfinite(now):
            raise ValueError("Candidate time must be finite")
        public = []
        with self._candidate_lock:
            self._prune_locked(now)
            for row, original_format in selected[:self.max_candidates_per_subject]:
                subject_keys = [k for k, v in self._candidates.items() if v.subject == principal.subject]
                if len(subject_keys) >= self.max_candidates_per_subject:
                    self._candidates.pop(subject_keys[0])
                if len(self._candidates) >= self.max_candidates:
                    self._candidates.pop(next(iter(self._candidates)))
                candidate_id = secrets.token_hex(12)
                candidate = _Candidate(candidate_id, principal.subject, deepcopy(args), deepcopy(target), row["provider"],
                                       row["subtitle"], original_format, now + self.candidate_ttl_seconds)
                self._candidates[candidate_id] = candidate
                score = row.get("score")
                score = score if type(score) in {int, float} and math.isfinite(score) and 0 <= score <= 100 else None
                release_info = row.get("release_info")
                release_info = [s for s in release_info[:8] if _text(s) and "/" not in s and "\\" not in s] if isinstance(release_info, list) else []
                public.append({"candidate_id": candidate_id, "provider": row["provider"], "language": args["language"],
                               "forced": args["forced"], "hearing_impaired": args["hearing_impaired"],
                               "score": score, "release_info": release_info,
                               "original_format": original_format, "expires_at": candidate.expires_at,
                               "download_arguments": {**args, "candidate_id": candidate_id}})
        return {"source": "native_api", "candidates": public, "count": len(public),
                "truncated": len(selected) > len(public), "provider_search_performed": True,
                "metadata_refresh_possible": True, "subtitle_downloaded": False}

    def _post_once(self, args, target, candidate, audit):
        cfg = self.native.configs["bazarr"]
        kind = "movies" if args["media_type"] == "movie" else "episodes"
        fields = {"radarrid": target["movie_id"]} if kind == "movies" else {"seriesid": target["series_id"], "episodeid": target["episode_id"]}
        fields.update(hi=str(args["hearing_impaired"]), forced=str(args["forced"]), original_format=str(candidate.original_format),
                      provider=candidate.provider, subtitle=candidate.subtitle)
        endpoint = "/api/providers/" + kind
        request = urllib.request.Request(cfg.base_url.rstrip("/") + endpoint, method="POST",
                                         data=urllib.parse.urlencode(fields).encode("ascii"),
                                         headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                                                  "X-Api-Key": cfg.api_key, "User-Agent": "arrmes-bazarr-subtitles/1"})
        try:
            with self.native._opener.open(request, timeout=self.native.timeout_seconds) as response:
                code = int(response.status)
                audit.append({"method": "POST", "path": endpoint, "status": code, "attempt": 1})
                return code, "accepted" if code == 204 else "unknown"
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            exc.close()
            audit.append({"method": "POST", "path": endpoint, "status": code, "attempt": 1})
            return code, "rejected" if 400 <= code < 500 else "unknown"
        except (urllib.error.URLError, TimeoutError, OSError):
            audit.append({"method": "POST", "path": endpoint, "status": 503, "attempt": 1})
            return 503, "unknown"

    def confirm_write(self, confirm_id: str, principal: Principal, *, idempotency_key: str | None = None,
                      now: float | None = None) -> AdapterObservation:
        audit: list[dict[str, Any]] = []
        if not self._authorized(principal) or self.write_mode != "confirm":
            return _obs("denied", "confirm_write", 403, {"error": "permission_required_or_writes_disabled"}, audit)
        if not isinstance(confirm_id, str) or not confirm_id.startswith(CONFIRMATION_PREFIX):
            return _obs("denied", "confirm_write", 403, {"error": "confirmation_namespace_invalid"}, audit)
        token, error = self.confirmation_store.redeem(confirm_id[len(CONFIRMATION_PREFIX):], principal, now=now)
        if error or token is None:
            return _obs("denied", "confirm_write", 403, {"error": error or "confirmation_not_found"}, audit)
        if token.call_name not in SUBTITLE_ACTIONS:
            return _obs("denied", "confirm_write", 403, {"error": "confirmation_action_mismatch"}, audit)
        args = validate_bazarr_subtitle_arguments(token.call_name, token.arguments)
        call = Call(token.call_name, args)
        confirmed = {"confirmed_call": normalized(call)}
        if "bazarr" not in self.native.configs:
            return _obs("error", call.name, 503, {**confirmed, "error": "service_not_configured"}, audit)
        try:
            target, row = self._resolve(args, audit)
            expected = {k: v for k, v in token.resolved_entity.items() if k not in {"candidate_id", "candidate_sha256"}}
            if target != expected:
                return _obs("denied", call.name, 409, {**confirmed, "error": "confirmation_target_or_service_changed"}, audit)
            if call.name == "bazarr_subtitle_search":
                data = self._search(args, target, principal, audit)
                return _obs("partial" if data["truncated"] else "verified_ok", call.name, 200,
                            {**confirmed, **data}, audit, complete=not data["truncated"])
            candidate = self._candidate(args["candidate_id"], principal, args, target)
            if (candidate is None or token.resolved_entity.get("candidate_id") != candidate.candidate_id
                    or token.resolved_entity.get("candidate_sha256") != candidate.fingerprint()):
                return _obs("denied", call.name, 409, {**confirmed, "error": "confirmation_candidate_expired_or_changed"}, audit)
            if self._existing_subtitles(row, args):
                return _obs("denied", call.name, 409, {**confirmed, "error": "matching_external_subtitle_now_present"}, audit)
            candidate = self._candidate(args["candidate_id"], principal, args, target, claim=True)
            if candidate is None:
                return _obs("denied", call.name, 409, {**confirmed, "error": "subtitle_candidate_already_used"}, audit)
            code, outcome = self._post_once(args, target, candidate, audit)
            data = {**confirmed, "source": "native_api", "candidate_id": candidate.candidate_id,
                    "outcome": outcome, "replayed": False, "postcondition_verified": False,
                    "metadata_postcondition_observed": False, "job_completion_correlated": False}
            if outcome != "accepted":
                return _obs("error", call.name, code, {**data, "error": "subtitle_download_rejected" if outcome == "rejected" else "subtitle_download_outcome_unknown"}, audit)
            try:
                after_target, after_row = self._resolve(args, audit)
                metadata_observed = after_target == target and bool(self._existing_subtitles(after_row, args, require_size=True))
            except _UpstreamError:
                metadata_observed = False
            # This API exposes a cache UUID before dispatch and provider-native
            # subtitle IDs in history, without a job ID linking the two. Another
            # actor can create matching metadata while this candidate is queued.
            # Preserve the observation without crediting candidate completion.
            data.update(outcome="dispatched", metadata_postcondition_observed=metadata_observed,
                        verification_basis="matching_external_subtitle_metadata_only" if metadata_observed else "queue_acceptance_only")
            return _obs("dispatched", call.name, code, data, audit)
        except _UpstreamError as exc:
            return self._failure(call.name, exc, audit)
