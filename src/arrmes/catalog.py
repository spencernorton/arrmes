"""The versioned semantic contract; never expose raw HTTP, credentials or IDs."""

from dataclasses import dataclass
import hashlib
import json
from typing import Any

CONTRACT_VERSION = "arrmes.tools.v1"
CONTRACT_VERSION_V2 = "arrmes.tools.v2"
CONTRACT_VERSION_V3 = "arrmes.tools.v3"


def text(description: str, limit: int = 200) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": limit,
            "pattern": r"\S", "description": description}


def choice(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


YEAR = {"type": "integer", "minimum": 1870, "maximum": 2200}


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    service: str
    effect: str
    permission: str | None
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    defaults: tuple[tuple[str, Any], ...] = ()

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": self.properties,
                "required": list(self.required), "additionalProperties": False}

    def tool(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


ACTIONS = (
    Action("tv_status", "Check whether a TV series is in the library and available. Read only.",
           "sonarr", "read", "media:read", {"title": text("TV series title")}, ("title",)),
    Action("tv_missing", "List missing aired TV episodes. Read only; does not search indexers or download.",
           "sonarr", "read", "media:read", {
               "title": text("TV series title"), "include_specials": {"type": "boolean"}},
           ("title",), (("include_specials", False),)),
    Action("tv_search_missing", "Start an indexer search and downloads for missing TV episodes. Requires an explicit download or search command.",
           "sonarr", "download", "media:manage", {
               "title": text("TV series title"),
               "season": {"type": "integer", "minimum": 0, "maximum": 1000}}, ("title",)),
    Action("movie_status", "Check whether a movie is in the library and available. Read only.",
           "radarr", "read", "media:read", {"title": text("Movie title"), "year": YEAR}, ("title",)),
    Action("movie_search", "Start an indexer search and download for an existing movie. Does not create a new request.",
           "radarr", "download", "media:manage", {"title": text("Movie title"), "year": YEAR}, ("title",)),
    Action("now_playing", "Show current Plex streams using Tautulli. Operator access only.",
           "tautulli", "read", "activity:read", {}),
    Action("request_status", "Check the caller's existing media request and progress by title.",
           "requests", "read", "requests:read", {"title": text("Requested movie or TV series title")}, ("title",)),
    Action("request_media", "Submit a new movie or TV request as the authenticated caller through the request system.",
           "requests", "request", "requests:create", {
               "title": text("Movie or TV series title"), "media_type": choice("movie", "tv"), "year": YEAR},
           ("title", "media_type")),
    Action("music_status", "Check a music artist or album in Lidarr. Read only.",
           "lidarr", "read", "media:read", {"artist": text("Artist name"), "album": text("Album title")}, ("artist",)),
    Action("seer_discover", "Browse trending, popular or upcoming movie or TV recommendations in Seer. Read only.",
           "seer", "read", "media:read", {"category": choice("trending", "popular", "upcoming"),
                                                     "media_type": choice("movie", "tv")}, ("category", "media_type")),
    Action("armiger_parse", "Parse one release filename into structured metadata with Armiger. Does not retrain or change the pipeline.",
           "armiger", "read", "releases:classify", {"release_name": text("Release filename", 512)}, ("release_name",)),
    Action("bitmagnet_search", "Search Bitmagnet's torrent catalogue. Does not add or download a torrent.",
           "bitmagnet", "read", "releases:read", {"query": text("Torrent search text", 256)}, ("query",)),
    Action("waypoint_status", "Read DH-Waypoint pipeline health and processing status. Does not restart or modify jobs.",
           "dh-waypoint", "read", "pipeline:read", {}),
    Action("clarify", "Ask for a missing title, media type, ambiguous reference or unclear scope before choosing an action.",
           "control", "control", None, {"slot": choice("title", "media_type", "reference", "scope")}, ("slot",)),
    Action("unsupported", "Decline unsupported, destructive, credential, infrastructure or complex multi-action requests.",
           "control", "control", None, {"reason": choice("out_of_scope", "unsafe", "complex")}, ("reason",)),
)
BY_NAME = {a.name: a for a in ACTIONS}

V2_EXTRA_ACTIONS = (
    Action("watch_history", "Inspect recent Tautulli Plex watch history, optionally filtered by title. Read only.",
           "tautulli", "read", "activity:read", {"title": text("Optional media title filter", 200)}),
    Action("music_search", "Trigger an indexer search in Lidarr for a monitored artist or album.",
           "lidarr", "download", "media:manage", {"artist": text("Artist name"), "album": text("Album title")}, ("artist",)),
    Action("torrent_list", "List torrents in qBittorrent filtered by transfer state or title query. Read only.",
           "qbittorrent", "read", "torrents:read", {
               "state": choice("all", "downloading", "seeding", "paused", "completed", "stalled"),
               "query": text("Optional torrent title filter", 200)},
           (), (("state", "all"),)),
    Action("torrent_pause", "Pause a specific torrent in qBittorrent by resolved title. Never pauses all torrents indiscriminately.",
           "qbittorrent", "write", "torrents:manage", {"title": text("Torrent release or media title to pause", 200)}, ("title",)),
    Action("torrent_resume", "Resume a specific paused or stalled torrent in qBittorrent by resolved title.",
           "qbittorrent", "write", "torrents:manage", {"title": text("Torrent release or media title to resume", 200)}, ("title",)),
)

V2_ACTIONS = ACTIONS + V2_EXTRA_ACTIONS
V2_BY_NAME = {a.name: a for a in V2_ACTIONS}
WRITE_EFFECT_ACTIONS = frozenset(a.name for a in V2_ACTIONS if a.effect in {"download", "request", "write"})

# The released neural model continues to receive precisely tools_v2(). Native
# service reads use an explicitly identified deterministic route. Keep the v2
# schema and hash unchanged so historical experiments remain reproducible.
SERVICE_QUERY_ACTION = Action(
    "service_query", "Inspect a configured native media service. Read only.",
    "configured-service", "read", None,
    {"service": text("Configured service name", 32),
     "operation": text("Supported read operation", 32),
     "query": text("Explicit title or search text", 256),
     "media_type": choice("movie", "tv")},
    ("service", "operation"),
)
V3_ACTIONS = V2_ACTIONS + (SERVICE_QUERY_ACTION,)
V3_BY_NAME = {a.name: a for a in V3_ACTIONS}

# Provider searches can refresh subtitle indexes and contact providers even
# though Bazarr exposes them as GET. Both stages are consequential operations.
_SUBTITLE_PROPERTIES = {
    "media_type": choice("movie", "episode"),
    "title": text("Exact movie or series title"),
    "language": {"type": "string", "pattern": "^[a-z]{2}$"},
    "forced": {"type": "boolean"},
    "hearing_impaired": {"type": "boolean"},
    "year": YEAR,
    "season": {"type": "integer", "minimum": 0, "maximum": 1000},
    "episode": {"type": "integer", "minimum": 1, "maximum": 10000},
}
_SUBTITLE_REQUIRED = ("media_type", "title", "language", "forced", "hearing_impaired")
BAZARR_ACTIONS = (
    Action("bazarr_subtitle_search", "Search subtitle providers for one exact movie or episode. Requires confirmation because this refreshes indexes and contacts providers.",
           "bazarr", "write", "media:manage", dict(_SUBTITLE_PROPERTIES), _SUBTITLE_REQUIRED),
    Action("bazarr_subtitle_download", "Download one caller-bound subtitle candidate returned by a confirmed provider search. Requires a new explicit confirmation.",
           "bazarr", "write", "media:manage", {
               **_SUBTITLE_PROPERTIES,
               "candidate_id": {"type": "string", "pattern": "^[a-f0-9]{24}$"},
           }, _SUBTITLE_REQUIRED + ("candidate_id",)),
)
V3_ACTIONS += BAZARR_ACTIONS
V3_BY_NAME = {a.name: a for a in V3_ACTIONS}
WRITE_EFFECT_ACTIONS_V3 = frozenset(a.name for a in V3_ACTIONS if a.effect in {"download", "request", "write"})


def tools() -> list[dict]:
    return [a.tool() for a in ACTIONS]


def tools_v2() -> list[dict]:
    return [a.tool() for a in V2_ACTIONS]


def tools_v3() -> list[dict]:
    return [a.tool() for a in V3_ACTIONS]


def manifest() -> dict:
    return {"version": CONTRACT_VERSION, "actions": [
        {"tool": a.tool(), "service": a.service, "effect": a.effect,
         "permission": a.permission, "defaults": dict(a.defaults),
         "execution": "dry_run_only"} for a in ACTIONS]}


def manifest_v2() -> dict:
    return {"version": CONTRACT_VERSION_V2, "actions": [
        {"tool": a.tool(), "service": a.service, "effect": a.effect,
         "permission": a.permission, "defaults": dict(a.defaults),
         "execution": "sandbox_verified"} for a in V2_ACTIONS]}


def contract_hash() -> str:
    return hashlib.sha256(json.dumps(manifest(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def contract_hash_v2() -> str:
    return hashlib.sha256(json.dumps(manifest_v2(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def manifest_v3() -> dict:
    from .services import SUPPORTED_OPERATIONS, SERVICE_PERMISSIONS
    return {"version": CONTRACT_VERSION_V3, "neural_contract": CONTRACT_VERSION_V2,
            "neural_contract_sha256": contract_hash_v2(),
            "explicit_write_actions": [{"tool": a.tool(), "effect": a.effect,
                                        "permission": a.permission,
                                        "routing": "explicit_bazarr_deterministic"}
                                       for a in BAZARR_ACTIONS],
            "extension": {"tool": SERVICE_QUERY_ACTION.tool(), "effect": "read",
                          "routing": "explicit_service_deterministic",
                          "services": {name: {"operations": sorted(ops),
                                              "permission": SERVICE_PERMISSIONS[name]}
                                       for name, ops in sorted(SUPPORTED_OPERATIONS.items())}}}


def contract_hash_v3() -> str:
    return hashlib.sha256(json.dumps(manifest_v3(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
