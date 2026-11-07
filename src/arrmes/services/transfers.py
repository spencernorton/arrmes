"""Native qBittorrent transfer controls with exact-target, single-use confirmation."""
from __future__ import annotations

import re
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from arrmes.adapters import AdapterObservation
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, normalized, validate_call
from .native import ServiceQueryAdapter, _norm, _rows, _UpstreamError

CONFIRMATION_PREFIX = "native-qbt:"
TRANSFER_ACTIONS = frozenset({"torrent_pause", "torrent_resume"})
TORRENT_ACTIONS = TRANSFER_ACTIONS | {"torrent_list"}
_STOPPED = frozenset({"pausedup", "pauseddl", "stoppedup", "stoppeddl"})
_RUNNABLE = frozenset({"uploading", "downloading", "queuedup", "queueddl", "stalledup", "stalleddl",
                       "checkingup", "checkingdl", "forcedup", "forceddl", "metadl", "allocating",
                       "checkingresumedata", "moving"})
_HASH = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})")


def _obs(status: str, name: str, code: int, data: dict[str, Any], audit, *, complete: bool = False) -> AdapterObservation:
    return AdapterObservation(status, "qbittorrent", name, code, complete, data, audit)


class NativeTransferAdapter:
    """Existing v2 pause/resume names, implemented against native WebAPI v2.

    Only exact unique catalogue names resolve. Mutation uses a hash pinned into
    the confirmation store's authenticated resolved_entity, never a fresh title
    search or an unsigned user argument. There is no direct-write mode.
    """

    def __init__(self, native: ServiceQueryAdapter, *, write_mode: str = "confirm", confirmation_store=None) -> None:
        if write_mode not in {"confirm", "shadow", "disabled"}:
            raise ValueError("Native transfer mode must be confirm, shadow, or disabled")
        from arrmes.live_adapter import WriteConfirmationStore
        self.native = native
        self.write_mode = write_mode
        self.confirmation_store = confirmation_store or WriteConfirmationStore()

    @staticmethod
    def _authorized(principal: Principal) -> bool:
        return bool(principal.subject and "torrents:manage" in principal.scopes)

    def _major_version(self, audit) -> int:
        version = self.native._get("qbittorrent", "/api/v2/app/version", audit, text=True)
        api = self.native._get("qbittorrent", "/api/v2/app/webapiVersion", audit, text=True)
        match = re.fullmatch(r"v?([45])\.(\d+)(?:\.\d+){0,2}(?:[-+].*)?", version)
        if not match or not re.fullmatch(r"2\.\d+(?:\.\d+)?", api):
            raise _UpstreamError("unsupported_qbittorrent_version", 501)
        if match[1] == "4" and int(match[2]) < 1:
            raise _UpstreamError("unsupported_qbittorrent_version", 501)
        return int(match[1])

    @staticmethod
    def _at_target(name: str, state: Any) -> bool:
        if not isinstance(state, str) or state.casefold() not in _STOPPED | _RUNNABLE:
            raise _UpstreamError("unrecognized_torrent_state")
        return state.casefold() in (_STOPPED if name == "torrent_pause" else _RUNNABLE)

    def _resolve(self, call: Call, audit) -> tuple[AdapterObservation | None, dict[str, Any]]:
        # Manage permission authorizes the prerequisite inventory read for this
        # exact transfer intent. It does not expose an unfiltered read action.
        listing = self.native._qbittorrent({"service": "qbittorrent", "operation": "queue"}, audit)
        if listing.status != "verified_ok" or not listing.data.get("complete_listing"):
            return _obs("clarify_required", call.name, 409,
                        {"slot": "reference", "reason": "torrent_inventory_incomplete"}, audit), {}
        exact = [r for r in listing.data["items"] if _norm(str(r.get("name", ""))) == _norm(call.arguments["title"])]
        if len(exact) != 1:
            return _obs("clarify_required", call.name, 409,
                        {"slot": "reference", "reason": "torrent_not_uniquely_resolved"}, audit), {}
        row = exact[0]
        info_hash = row.get("hash")
        if not isinstance(info_hash, str) or not _HASH.fullmatch(info_hash):
            raise _UpstreamError("invalid_torrent_identity")
        return None, {"hash": info_hash.lower(), "name": row["name"], "api_major": self._major_version(audit),
                      "prestate": row.get("state"), "service": "qbittorrent"}

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        call = validate_call(call.name, call.arguments, allow_v3=False)
        if call.name not in TORRENT_ACTIONS:
            raise InvalidCall("Native torrent adapter accepts only list/pause/resume")
        audit: list[dict[str, Any]] = []
        if call.name == "torrent_list":
            return self._list(call, principal, audit)
        if not self._authorized(principal):
            return _obs("denied", call.name, 403, {"error": "permission_required"}, audit)
        if self.write_mode == "disabled":
            return _obs("denied", call.name, 403, {"error": "writes_disabled"}, audit)
        if "qbittorrent" not in self.native.configs:
            return _obs("error", call.name, 503, {"error": "service_not_configured"}, audit)
        try:
            failure, entity = self._resolve(call, audit)
            if failure is not None:
                return failure
            if self._at_target(call.name, entity["prestate"]):
                return _obs("verified_ok", call.name, 200, {"hash": entity["hash"], "name": entity["name"],
                            "state": entity["prestate"], "changed": False}, audit, complete=True)
            summary = f"{'Pause' if call.name == 'torrent_pause' else 'Resume'} torrent '{entity['name']}' ({entity['hash']})"
            if self.write_mode == "shadow":
                return _obs("shadow_logged", call.name, 200, {"summary": summary, "resolved_entity": entity}, audit)
            token = self.confirmation_store.mint(principal, call, summary=summary, resolved_entity=entity)
            return _obs("confirmation_required", call.name, 200,
                        {"confirm_id": CONFIRMATION_PREFIX + token.confirm_id, "summary": summary,
                         "expires_at": token.expires_at, "resolved_entity": entity}, audit)
        except _UpstreamError as exc:
            return _obs("error", call.name, exc.status, {"error": exc.code}, audit)

    def _list(self, call: Call, principal: Principal, audit) -> AdapterObservation:
        if not principal.subject or "torrents:read" not in principal.scopes:
            return _obs("denied", call.name, 403, {"error": "permission_required"}, audit)
        if "qbittorrent" not in self.native.configs:
            return _obs("error", call.name, 503, {"error": "service_not_configured"}, audit)
        try:
            major = self._major_version(audit)
            state = call.arguments.get("state", "all")
            native_filter = "stopped" if state == "paused" and major == 5 else state
            args = {"service": "qbittorrent", "operation": "queue", "torrent_filter": native_filter}
            if "query" in call.arguments:
                args["query"] = call.arguments["query"]
            obs = self.native._qbittorrent(args, audit)
            # Preserve exact upstream state as well as the v2 semantic filter.
            obs.operation = "torrent_list"
            obs.data.update(operation="torrent_list", state=state, torrents=obs.data.pop("items"), count=obs.data["returned_count"])
            return obs
        except _UpstreamError as exc:
            return _obs("error", call.name, exc.status, {"error": exc.code}, audit)

    def _read_bound(self, entity: dict[str, Any], audit) -> dict[str, Any] | None:
        rows = _rows(self.native._get("qbittorrent", "/api/v2/torrents/info?" + urllib.parse.urlencode({"hashes": entity["hash"]}), audit))
        if len(rows) != 1:
            return None
        row = rows[0]
        if (not isinstance(row.get("hash"), str) or row["hash"].lower() != entity["hash"]
                or row.get("name") != entity["name"]):
            return None
        return row

    def _post_once(self, endpoint: str, info_hash: str, audit) -> tuple[int, dict[str, Any]]:
        """A single form POST. A timeout/5xx is unknown, never replayed."""
        cfg = self.native.configs["qbittorrent"]
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                   "Referer": cfg.base_url.rstrip("/") + "/", "User-Agent": "arrmes-native-transfer/1"}
        if cfg.api_key:
            headers["Authorization"] = "Bearer " + cfg.api_key
        if cfg.session_id:
            headers["Cookie"] = cfg.session_id if cfg.session_id.startswith("QBT_SID_") else ("SID=" + cfg.session_id)
        body = urllib.parse.urlencode({"hashes": info_hash}).encode("ascii")
        req = urllib.request.Request(cfg.base_url.rstrip("/") + endpoint, data=body, headers=headers, method="POST")
        try:
            with self.native._opener.open(req, timeout=self.native.timeout_seconds) as response:
                code = int(response.status)
                audit.append({"method": "POST", "path": endpoint, "status": code, "attempt": 1})
                if not 200 <= code < 300:
                    return code, {"error": "upstream_mutation_rejected", "replayed": False}
                return code, {}
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            exc.close()
            audit.append({"method": "POST", "path": endpoint, "status": code, "attempt": 1})
            return code, {"error": "upstream_mutation_rejected", "replayed": False,
                          "outcome": "unknown" if code >= 500 or 300 <= code < 400 else "rejected"}
        except (urllib.error.URLError, TimeoutError, OSError):
            audit.append({"method": "POST", "path": endpoint, "status": 503, "attempt": 1})
            return 503, {"error": "mutation_transport_error", "outcome": "unknown", "replayed": False}

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
        if token.call_name not in TRANSFER_ACTIONS:
            return _obs("denied", "confirm_write", 403, {"error": "confirmation_action_mismatch"}, audit)
        entity, name = token.resolved_entity, token.call_name
        # This comes exclusively from the authenticated confirmation record,
        # never an upstream response object. Preserve the caller's exact signed
        # arguments so the executor can report the action it actually confirmed.
        confirmed_call = normalized(validate_call(name, token.arguments, allow_v3=False))

        def confirmed(status: str, code: int, data: dict[str, Any], *, complete: bool = False) -> AdapterObservation:
            return _obs(status, name, code, {**data, "confirmed_call": confirmed_call}, audit, complete=complete)

        if (set(entity) != {"hash", "name", "api_major", "prestate", "service"}
                or entity.get("service") != "qbittorrent" or not isinstance(entity.get("hash"), str)
                or not _HASH.fullmatch(entity["hash"]) or entity.get("api_major") not in {4, 5}
                or not isinstance(entity.get("name"), str)):
            return _obs("denied", name, 403, {"error": "confirmation_binding_invalid"}, audit)
        if "qbittorrent" not in self.native.configs:
            return _obs("error", name, 503, {"error": "service_not_configured"}, audit)
        try:
            if self._major_version(audit) != entity["api_major"]:
                return _obs("denied", name, 409, {"error": "confirmation_service_version_changed"}, audit)
            row = self._read_bound(entity, audit)
            if row is None:
                return _obs("denied", name, 409, {"error": "confirmation_target_changed"}, audit)
            if self._at_target(name, row.get("state")):
                return confirmed("verified_ok", 200, {"hash": entity["hash"], "name": entity["name"],
                                 "state": row["state"], "changed": False}, complete=True)
            method = ({"torrent_pause": "stop", "torrent_resume": "start"} if entity["api_major"] == 5
                      else {"torrent_pause": "pause", "torrent_resume": "resume"})[name]
            code, failure = self._post_once("/api/v2/torrents/" + method, entity["hash"], audit)
            if failure:
                return _obs("error", name, code, failure, audit)
            after = self._read_bound(entity, audit)
            if after is None:
                return confirmed("dispatched", code, {"hash": entity["hash"], "changed": None,
                                 "postcondition": "target_unavailable_after_dispatch"})
            complete = self._at_target(name, after.get("state"))
            return confirmed("verified_ok" if complete else "dispatched", code,
                             {"hash": entity["hash"], "name": entity["name"], "state": after["state"],
                              "changed": True, "postcondition_verified": complete}, complete=complete)
        except _UpstreamError as exc:
            return _obs("error", name, exc.status, {"error": exc.code}, audit)
