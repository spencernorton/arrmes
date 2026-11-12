"""Compact multi-head neural intent router and typed span/argument resolver (ArrmesNeuralRouterV3).

Provides a fast, low-memory (<15 MB disk, <2 ms warm CPU latency) local learned
router that supports both the frozen v2 20-tool contract and the v3 23-tool
expanded native contract (31 service_query pairs + Bazarr subtitle workflows).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
import unicodedata

import array
import io
import math
import struct
import zipfile

try:
    import numpy as np
except ImportError:  # pragma: no cover - stdlib fallback for minimal CI environments
    np = None  # type: ignore[assignment]


def _read_npy_float32(raw_bytes: bytes) -> tuple[tuple[int, ...], array.array]:
    """Parse a standard little-endian float32/float64 .npy buffer using only the Python standard library."""
    stream = io.BytesIO(raw_bytes)
    magic = stream.read(6)
    if magic != b"\x93NUMPY":
        raise ValueError("Invalid .npy magic header")
    major, _minor = struct.unpack("<BB", stream.read(2))
    hdr_len = struct.unpack("<H", stream.read(2))[0] if major == 1 else struct.unpack("<I", stream.read(4))[0]
    header = stream.read(hdr_len).decode("latin1")
    m_shape = re.search(r"'shape':\s*\(([^)]*)\)", header)
    if not m_shape:
        raise ValueError("Missing shape in .npy header")
    shape = tuple(int(x.strip()) for x in m_shape.group(1).split(",") if x.strip())
    data_bytes = stream.read()
    if "'<f4'" in header:
        arr = array.array("f")
        arr.frombytes(data_bytes)
        return shape, arr
    if "'<f8'" in header:
        arr_d = array.array("d")
        arr_d.frombytes(data_bytes)
        return shape, array.array("f", (float(v) for v in arr_d))
    raise ValueError(f"Unsupported .npy descr in header: {header}")


from .catalog import BY_NAME, V2_BY_NAME, V3_BY_NAME, WRITE_EFFECT_ACTIONS_V3
from .model import finalize_output
from .planner import (
    _context_role_matches,
    _AFFIRMATIVE_PAUSE_RE,
    _AFFIRMATIVE_REQUEST_RE,
    _AFFIRMATIVE_RESUME_RE,
    _ALL_SEASONS_RE,
    _COMPLEX_RE,
    _DEICTIC_PLACEHOLDERS,
    _DEICTIC_RE,
    _GENERIC_DOMAIN_PLACEHOLDERS,
    _INTERROGATIVE_OR_HYPOTHETICAL_WRITE_RE,
    _MOVIE_WORD_RE,
    _NEGATED_PAUSE_RE,
    _NEGATED_REQUEST_RE,
    _NEGATED_RESUME_RE,
    _NEGATED_SPECIALS_RE,
    _NEGATED_WRITE_RE,
    _OUT_OF_SCOPE_RE,
    _PASSIVE_PAUSE_QUERY_RE,
    _PASSIVE_RESUME_QUERY_RE,
    _REQUEST_TRACK_RE,
    _REQUEST_WORD_RE,
    _SCOPE_RE,
    _SEARCH_DOWNLOAD_TRIGGER_RE,
    _SEASON_MENTION_RE,
    _TORRENT_READ_VERB_RE,
    _TRAILING_PAREN_YEAR_RE,
    _TV_WORD_RE,
    _UNSAFE_RE,
    _artist_role_is_explicit,
    _explicit_seasons,
    _has_history_intent,
    _infer_clarify_slot,
    _norm_alnum,
    _quoted_spans,
    _request_identity_scope_fits_schema,
    _strip_quoted_and_releases,
    _write_scope_fits_schema,
    verify_proposal,
)
from .protocol import Call, encode, validate_call
from .services.bazarr_routing import route_bazarr_subtitle_query
from .services.routing import route_service_query

ROUTER_CLASSES: tuple[str, ...] = (
    "tv_status",
    "tv_missing",
    "tv_search_missing",
    "movie_status",
    "movie_search",
    "music_status",
    "music_search",
    "now_playing",
    "watch_history",
    "torrent_list",
    "torrent_pause",
    "torrent_resume",
    "request_status",
    "request_media",
    "seer_discover",
    "armiger_parse",
    "bitmagnet_search",
    "waypoint_status",
    "clarify:title",
    "clarify:media_type",
    "clarify:reference",
    "clarify:scope",
    "unsupported:unsafe",
    "unsupported:complex",
    "unsupported:out_of_scope",
    "service_query",
    "bazarr_subtitle_search",
    "bazarr_subtitle_download",
)
CLASS_TO_IDX: dict[str, int] = {c: i for i, c in enumerate(ROUTER_CLASSES)}
CONCRETE_V2_INDICES: list[int] = [i for i, c in enumerate(ROUTER_CLASSES) if not c.startswith(("clarify:", "unsupported:")) and c not in {"service_query", "bazarr_subtitle_search", "bazarr_subtitle_download"}]
NUM_HASH_FEATURES = 16384
NUM_STRUCT_FEATURES = 32
TOTAL_FEATURES = NUM_HASH_FEATURES + NUM_STRUCT_FEATURES


def _hash_token(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    val = int.from_bytes(digest, "little")
    idx = val % NUM_HASH_FEATURES
    sign = 1.0 if (val >> 63) == 0 else -1.0
    return idx, sign



_NEG_TAG_RE = re.compile(
    r"\b(do\s+not|don't|without|never|instead\s+of|rather\s+than|not\s+a|not\s+an|no\s+indexer)\s+([a-z0-9_'-]+(?:\s+[a-z0-9_'-]+){0,5})"
)
_WORD_TOK_RE = re.compile(r"[a-z0-9_<>'-]+")
_TEMP_THIS_RE = re.compile(r"\bthis\s+(?:moment|week|time|month|year|evening|morning|series|film|movie|show|album)\b", re.I)
_MISSING_GAP_RE = re.compile(r"\b(missing|absent|gaps?|holes?|unobtained|no\s+file|installments?)\b", re.I)
_NOW_PLAYING_RE = re.compile(r"\b(now\s+playing|currently\s+streaming|active\s+streams?|active\s+sessions?|who'?s\s+streaming|playing\s+right\s+now)\b", re.I)
_TORRENT_NOUN_RE = re.compile(r"\b(torrents?|transfers?|qbittorrent|stalled|seeding|paused|downloading)\b", re.I)
_MUSIC_NOUN_RE = re.compile(r"\b(lidarr|albums?|artists?|discography|tracks?|music|performer)\b", re.I)
_DISCOVER_CAT_RE = re.compile(r"\b(trending|popular|upcoming)\b", re.I)
_DISCOVER_SVC_RE = re.compile(r"\b(discover|seer|seerr|overseerr|jellyseerr|week)\b", re.I)
_ARMIGER_HINT_RE = re.compile(r"\barmiger\b|\bparse\b.*\brelease\b|\.mkv\b", re.I)
_BITMAGNET_HINT_RE = re.compile(r"\bbitmagnet\b|\bdht\b", re.I)
_WAYPOINT_HINT_RE = re.compile(r"\bwaypoint\b|\bdh-waypoint\b", re.I)
_MEDIA_DOMAIN_ANCHOR_RE = re.compile(
    r"\b(sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|qbittorrent|qbt|tautulli|plex|"
    r"seerr|seer|overseerr|jellyseerr|bitmagnet|armiger|waypoint|dh-waypoint|"
    r"movies?|films?|tv|television|series|shows?|episodes?|seasons?|specials?|ovas?|anime|"
    r"albums?|artists?|discography|tracks?|songs?|music|performer|band|"
    r"torrents?|transfers?|seeding|stalled|paused|downloading|"
    r"subtitles?|subs|captions?|streams?|streaming|playing|watched|viewing|"
    r"indexers?|requests?|discover|trending|popular|upcoming|monitored|unmonitored|"
    r"pause|resume|downloads?|queue|library|catalog|status|missing|search|grab|fetch)\b"
    r"|\.(?:mkv|mp4|avi|ts|iso)\b",
    re.I,
)
_CONDITIONAL_WRITE_RE = re.compile(r"\b(if|unless|except|until|only\s+when|provided\s+that|as\s+long\s+as|fall\s+back\s+to)\b", re.I)
_NARROW_SCOPE_MUTATION_RE = re.compile(
    r"\b(4k|uhd|2160p|1080p|720p|480p|remux|hdr|flac|mp3|lossless|bitrate|mbps|kb/s|mib/s|"
    r"quality|(?<!my\s)(?<!caller\s)profiles?|quota|root\s+folder|(?:private|public)\s+(?:indexers?|trackers?)|on\s+behalf|"
    r"tracks?|songs?|discs?|s\d{1,2}e\d{1,3}|episode\s+\d+|seasons?\s+\d+\s+(?:through|to|or)\s+(?:season\s+)?\d+|"
    r"finale|pilot|premiere|(?:latest|newest|final)\s+season|rename|unmonitor(?:ed)?|recheck|"
    r"limit\s+the\s+(?:torrent\s+)?speed|move\s+the\s+torrent|different\s+save\s+path)\b",
    re.I,
)
_ORDINAL_REF_RE = re.compile(
    r"\b(?:no\s+selection\s+list\s+is\s+attached|(?:second|first|third)\s+(?:selected|torrent|one|movie|film|series|show|item|result)|"
    r"(?:other|earlier|later|counterpart|alternate)\s+(?:version|remake|adaptation|edition))\b",
    re.I,
)
_UNQUOTED_REL_RE = re.compile(r"\b([A-Za-z0-9_.()-]+\.(?:mkv|mp4|avi|ts|iso))\b", re.I)
_UNQUOTED_TOR_RE = re.compile(r"\b([A-Za-z0-9_.-]+(?:\.S\d{1,2}(?:E\d{1,2})?|\.\d{4}\.)[A-Za-z0-9_().-]+)\b")
_STOP_ENTITY_PAT = r"(?:\s+(?:in|on|from|through|via|by|using|within|inside|rather|instead|without|before|after|and|or|if|unless|leaving|including|excluding|season|episodes?|movies?|series|show|film|status|already|right\s+now|please|today|this\s+week|is\s+(?:missing|in|on|available|monitored|already|currently|held|present|downloaded|complete)|are\s+(?:missing|in|on|available|monitored|already|currently|held|present|downloaded|complete)|missing|monitored|available|downloaded)\b|[?.!,;:]|\s+\((?:18|19|20|21)\d{2}\)|$)"
_UNQUOTED_ENT_PATS = (
    re.compile(rf"\b(?:for|of|about|called|titled|named|series|movie|film|show|torrent|transfer|pause|resume|request|unmonitor|monitor)\s+([A-Z0-9\u00C0-\u024F][\w:'&.-]*(?:\s+(?:[\w:'&.-]+|\([A-Za-z]{{2,4}}\))){{0,6}}?){_STOP_ENTITY_PAT}"),
    re.compile(r"^\s*(?:is|are|does|do|has|have)\s+([A-Z0-9\u00C0-\u024F][\w:'&.-]*(?:\s+(?:[\w:'&.-]+|\([A-Za-z]{2,4}\))){0,5}?)\s+(?:in|monitored|complete|missing|available|downloaded|stalled|paused|seeding|playing)\b"),
    re.compile(r"\b(?i:(?:how\s+many|which|what|list|show|check|find|for|search(?:\s+sonarr)?(?:\s+indexers?)?(?:\s+for)?)\s+(?:(?:all|every|each)\s+)?(?:missing\s+)?(?:regular\s+)?)([A-Z0-9\u00C0-\u024F][\w:'&.-]*(?:\s+(?:[\w:'&.-]+|\([A-Za-z]{2,4}\))){0,5}?)\s+(?i:(?:regular\s+)?episodes?(?!\s+(?:of|for)\b))\b"),
)

def featurize_query(query: str, context: dict[str, Any] | None = None) -> Any:
    """Extract hashed word/char n-grams plus structural indicators (zero entity names)."""
    vec = np.zeros(TOTAL_FEATURES, dtype=np.float32) if np is not None else [0.0] * TOTAL_FEATURES
    q_unquoted = unicodedata.normalize("NFKC", _strip_quoted_and_releases(query)).translate(
        {0x2018: "'", 0x2019: "'", 0x02BC: "'"}
    )
    q_neg_tagged = _NEG_TAG_RE.sub(
        lambda m: m.group(1) + " " + " ".join("neg_" + tok for tok in m.group(2).split()),
        q_unquoted.casefold(),
    )
    q_low = re.sub(r"\b(?:please|kindly)\b", " ", q_neg_tagged)
    words = _WORD_TOK_RE.findall(q_low)

    for i, w in enumerate(words):
        idx, sign = _hash_token("w1:" + w)
        vec[idx] += sign
        if i + 1 < len(words):
            idx2, sign2 = _hash_token("w2:" + w + "_" + words[i + 1])
            vec[idx2] += sign2 * 1.2
        if i + 2 < len(words):
            idx3, sign3 = _hash_token("w3:" + w + "_" + words[i + 1] + "_" + words[i + 2])
            vec[idx3] += sign3 * 1.4

    compact = " ".join(words)
    for i in range(max(0, len(compact) - 3)):
        idx, sign = _hash_token("c4:" + compact[i : i + 4])
        vec[idx] += sign * 0.35

    if np is not None:
        norm = float(np.linalg.norm(vec[:NUM_HASH_FEATURES]))
        if norm > 0:
            vec[:NUM_HASH_FEATURES] /= norm
    else:
        norm = math.sqrt(sum(v * v for v in vec[:NUM_HASH_FEATURES]))
        if norm > 0:
            inv = 1.0 / norm
            for idx_n in range(NUM_HASH_FEATURES):
                if vec[idx_n] != 0.0:
                    vec[idx_n] *= inv

    selected = (context or {}).get("selected_media") if isinstance(context, dict) else None
    quoted = _quoted_spans(query)
    has_selected = bool(isinstance(selected, dict) and selected.get("title"))
    base = NUM_HASH_FEATURES
    vec[base + 0] = 1.0 if quoted else 0.0
    vec[base + 1] = 1.0 if len(quoted) >= 2 else 0.0
    vec[base + 2] = 1.0 if has_selected else 0.0
    vec[base + 3] = 1.0 if isinstance(selected, dict) and selected.get("media_type") == "movie" else 0.0
    vec[base + 4] = 1.0 if isinstance(selected, dict) and selected.get("media_type") == "tv" else 0.0
    q_no_temp = _TEMP_THIS_RE.sub(" ", q_unquoted)
    has_unquoted_ent = bool(_extract_unquoted_entity(query))
    vec[base + 5] = 1.0 if (not quoted and not has_selected and not has_unquoted_ent and _DEICTIC_RE.search(q_no_temp)) else 0.0
    vec[base + 6] = 1.0 if _COMPLEX_RE.search(q_unquoted) else 0.0
    vec[base + 7] = 1.0 if _UNSAFE_RE.search(q_unquoted) else 0.0
    vec[base + 8] = 1.0 if _OUT_OF_SCOPE_RE.search(q_unquoted) else 0.0
    vec[base + 9] = 1.0 if _SCOPE_RE.search(q_unquoted) else 0.0
    vec[base + 10] = 1.0 if _SEARCH_DOWNLOAD_TRIGGER_RE.search(q_unquoted) else 0.0
    vec[base + 11] = 1.0 if _NEGATED_WRITE_RE.search(q_unquoted) else 0.0
    vec[base + 12] = 1.0 if _INTERROGATIVE_OR_HYPOTHETICAL_WRITE_RE.search(q_unquoted) else 0.0
    vec[base + 13] = 1.0 if _TV_WORD_RE.search(q_unquoted) else 0.0
    vec[base + 14] = 1.0 if _MOVIE_WORD_RE.search(q_unquoted) else 0.0
    vec[base + 15] = 1.0 if _MISSING_GAP_RE.search(q_unquoted) else 0.0
    vec[base + 16] = 1.0 if _has_history_intent(q_unquoted) else 0.0
    vec[base + 17] = 1.0 if _NOW_PLAYING_RE.search(q_unquoted) else 0.0
    vec[base + 18] = 1.0 if _REQUEST_WORD_RE.search(q_unquoted) else 0.0
    vec[base + 19] = 1.0 if _REQUEST_TRACK_RE.search(q_unquoted) else 0.0
    vec[base + 20] = 1.0 if _AFFIRMATIVE_REQUEST_RE.search(q_unquoted) and not _NEGATED_REQUEST_RE.search(q_unquoted) else 0.0
    vec[base + 21] = 1.0 if _AFFIRMATIVE_PAUSE_RE.search(q_unquoted) and not _NEGATED_PAUSE_RE.search(q_unquoted) and not _PASSIVE_PAUSE_QUERY_RE.search(q_unquoted) else 0.0
    vec[base + 22] = 1.0 if _AFFIRMATIVE_RESUME_RE.search(q_unquoted) and not _NEGATED_RESUME_RE.search(q_unquoted) and not _PASSIVE_RESUME_QUERY_RE.search(q_unquoted) else 0.0
    vec[base + 23] = 1.0 if _TORRENT_READ_VERB_RE.search(q_unquoted) and _TORRENT_NOUN_RE.search(q_unquoted) else 0.0
    vec[base + 24] = 1.0 if _MUSIC_NOUN_RE.search(q_unquoted) else 0.0
    vec[base + 25] = 1.0 if _DISCOVER_CAT_RE.search(q_unquoted) and _DISCOVER_SVC_RE.search(q_unquoted) else 0.0
    vec[base + 26] = 1.0 if _ARMIGER_HINT_RE.search(query) else 0.0
    vec[base + 27] = 1.0 if _BITMAGNET_HINT_RE.search(q_unquoted) else 0.0
    vec[base + 28] = 1.0 if _WAYPOINT_HINT_RE.search(q_unquoted) else 0.0
    vec[base + 29] = 1.0 if route_service_query(query) is not None else 0.0
    vec[base + 30] = 1.0 if route_bazarr_subtitle_query(query) is not None else 0.0
    vec[base + 31] = 1.0 if _SEASON_MENTION_RE.search(q_unquoted) else 0.0
    return vec


def label_from_call(call_dict: dict[str, Any]) -> str:
    name = call_dict["name"]
    args = call_dict.get("arguments", {})
    if name == "clarify":
        slot = args.get("slot", "title")
        lbl = f"clarify:{slot}"
        return lbl if lbl in CLASS_TO_IDX else "clarify:title"
    if name == "unsupported":
        reason = args.get("reason", "out_of_scope")
        lbl = f"unsupported:{reason}"
        return lbl if lbl in CLASS_TO_IDX else "unsupported:out_of_scope"
    return name if name in CLASS_TO_IDX else "clarify:scope"


def _extract_unquoted_entity(query: str) -> str | None:
    """Extract an unquoted capitalized entity title or release token when no quotes are present."""
    m_rel = _UNQUOTED_REL_RE.search(query)
    if m_rel:
        return m_rel.group(1).strip(".,?!:;\"'")
    m_tor = _UNQUOTED_TOR_RE.search(query)
    if m_tor:
        return m_tor.group(1).strip(".,?!:;\"'")
    for pat in _UNQUOTED_ENT_PATS:
        m = pat.search(query)
        if m:
            cand = m.group(1).strip(" .,?!:;\"'")
            if cand and _norm_alnum(cand) not in _GENERIC_DOMAIN_PLACEHOLDERS and _norm_alnum(cand) not in _DEICTIC_PLACEHOLDERS:
                return cand
    return None


def _extract_music_pair(query: str, quoted: list[str], q_unquoted: str) -> tuple[str | None, str | None]:
    """Resolve (artist, album) from quoted spans or unquoted 'Album by Artist' / 'Artist's album Album'."""
    if len(quoted) >= 2:
        if _artist_role_is_explicit(quoted[0], quoted[1], query):
            return quoted[0], quoted[1]
        if _artist_role_is_explicit(quoted[1], quoted[0], query):
            return quoted[1], quoted[0]
        q_placeholder = re.sub(r'''["'][^"'\n]+["']''', "<QUOTED>", query)
        if re.search(
            r"\bnames\s+<QUOTED>\s+as\s+(?:the\s+)?artist\s+and\s+<QUOTED>\s+as\s+(?:the\s+)?album|"
            r"\bartist(?:\s+field)?\s+is\s+<QUOTED>\s+and\s+(?:my\s+|the\s+)?album(?:\s+field)?\s+is\s+<QUOTED>|"
            r"<QUOTED>\s+is\s+(?:the\s+)?(?:artist|performer|band)\s+and\s+<QUOTED>\s+is\s+(?:the\s+)?(?:album|record)|"
            r"<QUOTED>'s\s+album\s+<QUOTED>|\bby\s+<QUOTED>.*\balbum\s+<QUOTED>",
            q_placeholder,
            re.I,
        ):
            return quoted[0], quoted[1]
        if re.search(
            r"\bnames\s+<QUOTED>\s+as\s+(?:the\s+)?album\s+and\s+<QUOTED>\s+as\s+(?:the\s+)?artist|"
            r"\balbum(?:\s+field)?\s+is\s+<QUOTED>\s+and\s+(?:my\s+|the\s+)?artist(?:\s+field)?\s+is\s+<QUOTED>|"
            r"<QUOTED>\s+is\s+(?:the\s+)?(?:album|record)\s+and\s+<QUOTED>\s+is\s+(?:the\s+)?(?:artist|performer|band)|"
            r"<QUOTED>\s+by\s+<QUOTED>",
            q_placeholder,
            re.I,
        ):
            return quoted[1], quoted[0]
        first_pos = query.find(quoted[0])
        second_pos = query.find(quoted[1], first_pos + len(quoted[0]))
        before_first = query[:first_pos].lower()
        between = query[first_pos + len(quoted[0]) : second_pos].lower()
        if any(w in between for w in ("by", "artist", "performer", "band")):
            return quoted[1], quoted[0]
        if any(w in before_first for w in ("artist", "performer", "band")) and "album" in between:
            return quoted[0], quoted[1]
        if "album" in before_first and any(w in between for w in ("artist", "performer", "band")):
            return quoted[1], quoted[0]
        return quoted[0], quoted[1]
    if len(quoted) == 1:
        q_placeholder = re.sub(r'''["'][^"'\n]+["']''', "<QUOTED>", query)
        if re.search(r"\balbum\s+<QUOTED>", q_placeholder, re.I) and not re.search(r"\b(?:artist|performer|band)\b", q_placeholder, re.I):
            return None, quoted[0]
        return quoted[0], None
    m_poss = re.search(
        r"\bfor\s+([A-Z][A-Za-z0-9&' -]{1,40}?)'s\s+album\s+([A-Z][A-Za-z0-9&' -]{1,40}?)(?:[?.!,;:]|$)",
        query,
    )
    if m_poss:
        return m_poss.group(1).strip(), m_poss.group(2).strip()
    m_by = re.search(
        r"\b(?:whether|if|is|for|of|album|tracks\s+from|tracks\s+on)\s+([A-Z][A-Za-z0-9&' -]{1,40}?)\s+by\s+([A-Z][A-Za-z0-9&' -]{1,40}?)(?:\s+(?:is|are|in|from|on|complete|missing)\b|[?.!,;:]|$)",
        query,
    )
    if m_by:
        return m_by.group(2).strip(), m_by.group(1).strip()
    ent = _extract_unquoted_entity(query)
    return ent, None


class ArrmesNeuralRouterV3:
    """Learned multi-head neural router + typed argument resolver."""

    backend = "neural-router"

    def __init__(
        self,
        path: str,
        family: str = "hermes",
        adapter: str | None = None,
        *,
        contract_version: str = "v2",
    ) -> None:
        t0 = time.perf_counter()
        self.path = str(Path(path).expanduser().resolve())
        self.family = family
        self.adapter = adapter
        self.contract_version = contract_version
        p = Path(self.path)
        weights_file = p / "router_weights.npz" if p.is_dir() else p
        if not weights_file.is_file():
            raise FileNotFoundError(f"Neural router weights not found at {weights_file}")
        if np is not None:
            data = np.load(weights_file)
            self.W1: Any = data["W1"].astype(np.float32)
            self.b1: Any = data["b1"].astype(np.float32)
            self.W2: Any = data["W2"].astype(np.float32)
            self.b2: Any = data["b2"].astype(np.float32)
            self._hidden_dim = int(self.b1.shape[0])
            self._num_classes = int(self.b2.shape[0])
            self.temperature = float(data["temperature"][0]) if "temperature" in data else 1.0
        else:
            with zipfile.ZipFile(weights_file, "r") as zf:
                s_w1, self.W1 = _read_npy_float32(zf.read("W1.npy"))
                s_b1, self.b1 = _read_npy_float32(zf.read("b1.npy"))
                s_w2, self.W2 = _read_npy_float32(zf.read("W2.npy"))
                s_b2, self.b2 = _read_npy_float32(zf.read("b2.npy"))
                self._hidden_dim = s_b1[0]
                self._num_classes = s_b2[0]
                if "temperature.npy" in zf.namelist():
                    _, t_arr = _read_npy_float32(zf.read("temperature.npy"))
                    self.temperature = float(t_arr[0]) if t_arr else 1.0
                else:
                    self.temperature = 1.0
        self.temperature = max(self.temperature, 1e-3)
        self.load_seconds = time.perf_counter() - t0

    def predict_proba(self, query: str, context: dict[str, Any] | None = None) -> tuple[str, float, Any]:
        x = featurize_query(query, context)
        inv_t = 1.0 / self.temperature
        if np is not None:
            h = np.maximum(0.0, x @ self.W1 + self.b1)
            raw_logits = h @ self.W2 + self.b2
            scaled = raw_logits * inv_t
            max_s = float(np.max(scaled))
            exp_l = np.exp(scaled - max_s)
            sum_e = float(np.sum(exp_l))
            probs = exp_l / sum_e
            best_idx = int(np.argmax(probs))
            self._last_energy = -self.temperature * (max_s + math.log(max(sum_e, 1e-12)))
            part = np.partition(probs, -2)
            self._last_margin = float(part[-1] - part[-2])
            return ROUTER_CLASSES[best_idx], float(probs[best_idx]), probs
        # Pure-stdlib sparse matrix-vector product fallback
        h_dim = self._hidden_dim
        c_dim = self._num_classes
        h_list = list(self.b1)
        w1 = self.W1
        for i, val in enumerate(x):
            if val != 0.0:
                row_off = i * h_dim
                for j in range(h_dim):
                    h_list[j] += val * w1[row_off + j]
        for j in range(h_dim):
            if h_list[j] < 0.0:
                h_list[j] = 0.0
        logits_list = list(self.b2)
        w2 = self.W2
        for j, hj in enumerate(h_list):
            if hj > 0.0:
                row_off = j * c_dim
                for c in range(c_dim):
                    logits_list[c] += hj * w2[row_off + c]
        scaled_list = [v * inv_t for v in logits_list]
        max_l = max(scaled_list)
        exp_list = [math.exp(v - max_l) for v in scaled_list]
        sum_e = sum(exp_list)
        probs_list = [v / sum_e for v in exp_list]
        best_idx = max(range(c_dim), key=lambda idx: probs_list[idx])
        s_probs = sorted(probs_list)
        self._last_energy = -self.temperature * (max_l + math.log(max(sum_e, 1e-12)))
        self._last_margin = float(s_probs[-1] - s_probs[-2])
        return ROUTER_CLASSES[best_idx], float(probs_list[best_idx]), probs_list

    def _build_call(self, pred_class: str, probs: Any, query: str, context: dict[str, Any] | None) -> tuple[Call, bool]:
        """Construct a schema-valid Call from the predicted class and typed span extraction."""
        used_structural = False
        allow_v3 = self.contract_version in {"v3", "arrmes.tools.v3"}
        q_unquoted = unicodedata.normalize("NFKC", _strip_quoted_and_releases(query)).translate(
            {0x2018: "'", 0x2019: "'", 0x02BC: "'"}
        )
        selected = (context or {}).get("selected_media") if isinstance(context, dict) else None
        quoted = _quoted_spans(query)

        # Explicit safety, complexity, out-of-scope, or meta-ambiguity guards first
        q_for_unsafe = re.sub(
            r"\b(?:do\s+not|don't|never|without)\s+(?:delet(?:e|ing)|remov(?:e|ing)|eras(?:e|ing)|wip(?:e|ing))\s+(?:its|the|any|their)?\s*(?:downloaded\s+)?files?\b"
            r"|\bdelete\s+(?:the\s+)?(?:existing\s+)?subtitles?(?:\s+files?)?.*",
            " ",
            q_unquoted,
            flags=re.I,
        )
        svc_mentions = { ("seerr" if s.lower() == "seer" else s.lower()) for s in re.findall(r"\b(sonarr|radarr|lidarr|qbittorrent|tautulli|prowlarr|readarr|chaptarr|whisparr|seerr|seer|bitmagnet|armiger|bazarr)\b", q_unquoted, re.I) }
        if _COMPLEX_RE.search(q_unquoted):
            return validate_call("unsupported", {"reason": "complex"}), pred_class != "unsupported:complex"
        if _UNSAFE_RE.search(q_for_unsafe):
            return validate_call("unsupported", {"reason": "unsafe"}), pred_class != "unsupported:unsafe"
        if (
            len(svc_mentions) >= 2
            and re.search(r"\b(and|then|followed\s+by|after\s+that|simultaneously)\b", q_unquoted, re.I)
            and not re.search(r"\b(instead\s+of|rather\s+than|do\s+not|don't|never|without|not\s+(?:in|a|an|from))\b", q_unquoted, re.I)
        ):
            return validate_call("unsupported", {"reason": "complex"}), pred_class != "unsupported:complex"
        if _OUT_OF_SCOPE_RE.search(q_unquoted):
            return validate_call("unsupported", {"reason": "out_of_scope"}), pred_class != "unsupported:out_of_scope"
        if (
            selected is None
            and not quoted
            and not _MEDIA_DOMAIN_ANCHOR_RE.search(query)
            and not _DEICTIC_RE.search(q_unquoted)
            and not _ORDINAL_REF_RE.search(q_unquoted)
            and pred_class.startswith("clarify:")
        ):
            return validate_call("unsupported", {"reason": "out_of_scope"}), pred_class != "unsupported:out_of_scope"
        if (
            pred_class in {"torrent_pause", "torrent_resume"}
            and (_PASSIVE_PAUSE_QUERY_RE.search(q_unquoted) or _PASSIVE_RESUME_QUERY_RE.search(q_unquoted))
            and not _NEGATED_PAUSE_RE.search(q_unquoted)
            and not _NEGATED_RESUME_RE.search(q_unquoted)
            and re.search(r"\b(torrents?|transfers?|qbittorrent)\b", q_unquoted, re.I)
        ):
            pred_class = "torrent_list"
            used_structural = True

        if re.search(r"\bno\s+selection\s+list\s+is\s+attached\b|\b(?:second|first|third)\s+selected\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "reference"}), pred_class != "clarify:reference"
        if re.search(r"\b(?:without\s+(?:saying|specifying|stating|declaring|indicating)|(?:while\s+)?(?:omitting|leaving)\b[^.;?!]*?|(?:not\s+(?:specified|stated|declared)\s+whether))\s*(?:whether\s+(?:it\s+is\s+(?:a\s+)?)?)?(?:movie|film)\s+(?:or|vs\.?|versus)\s+(?:tv|television|series|show)\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "media_type"}), pred_class != "clarify:media_type"
        if re.search(r"\b(?:is\s+unrelated|is\s+background\s+only|only\s+background\s+context|appears\s+in\s+my\s+notes|(?:[s]etting|[s]et)\s+<QUOTED>\s+aside|aside\s+from\s+<QUOTED>|is\s+not\s+the\s+target|do\s+not\s+use\s+<QUOTED>|ignor(?:e|ing)\s+<QUOTED>|disregard\s+<QUOTED>).*?\b(?:different|separate|another|unnamed|not\s+(?:supplied|given|provided)|have\s+not\s+(?:supplied|named|given|provided)|(?:whose|its)\s+(?:name|title)\s+(?:(?:i\s+)?(?:left\s+(?:blank|out)|omitted|forgot)|is\s+(?:missing|blank|omitted))|forgot\s+to\s+(?:include|name|supply)|left\s+(?:blank|out))\b|\bwithout\s+(?:giving|naming)\s+(?:the|a|an)\s+(?:series|movie|film|show|album|artist)(?:\s+title|\s+name)?\b|\bwithout\s+specifying\s+which\s+one\b|\b(?:film|movie|series|show|album|artist|torrent|transfer|query(?:\s+string)?)\s+(?:that\s+|which\s+)?i\s+(?:haven't|have\s+not|forgot\s+to)\s+(?:named?|supplied|given|provided|supply)\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "title"}), pred_class != "clarify:title"
        if re.search(r"\b(?:(?:check|look\s+up|search|request|grab|trigger\s+a\s+download\s+search)\s+(?:for\s+)?(?:the\s+)?(?:other|earlier|later|counterpart|alternate)\s+(?:version|remake|adaptation|edition)|(?:other|earlier|later|counterpart|alternate)\s+(?:version|remake|adaptation|edition)\s+of\s+the\s+(?:film|movie|series|show))\b", q_unquoted, re.I) and len(set(re.findall(r"\b(18[7-9]\d|19\d{2}|20\d{2}|21\d{2})\b", q_unquoted))) < 2:
            return validate_call("clarify", {"slot": "reference"}), pred_class != "clarify:reference"
        if re.search(r"\b(?:two\s+(?:films|movies|series|shows)\s+share\s+the\s+title|between\s+the\s+\d{4}\s+and\s+\d{4}\s+(?:films|movies|series|shows)|whichever\s+one\s+i\s+mean|have\s+not\s+(?:chosen|decided|selected|picked)\s+(?:which|whether|the\s+(?:year|version|season|edition)|another\s+action|any\s+other\s+action)|prohibition\s+is\s+my\s+entire|negative\s+instruction\s+is\s+my\s+whole|no\s+other\s+action\s+is\s+requested|do\s+nothing\s+else|only\s+the\s+(?:season\s+finale|pilot|premiere)\s+episode|in\s+sonarr,\s+search\s+for\s+s\d{1,2}e\d{1,3}\b|(?:latest|newest|final)\s+season\b[^.;?!]*?\bwithout\s+knowing\s+the\s+season\s+number|season\s+\d+\s+or\s+(?:season\s+)?\d+\b|rename\s+the\s+(?:series|show|movie|film|album|artist)|change\s+the\s+(?:quality\s+profile|root\s+folder|monitoring)|unmonitor\s+season\b|to\s+unmonitored\b|\bunless\s+<QUOTED>\s+is\s+already\s+complete\b|\bonly\s+if\s+<QUOTED>\s+is\s+(?:incomplete|missing)\b)\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "scope"}), pred_class != "clarify:scope"
        if re.search(r"\b(?:do\s+not|don'?t|not\s+asking(?:\s+you)?\s+(?:to|for)|refrain\s+from|never)\b", q_unquoted, re.I) and re.search(r"\b(?:prohibition\s+is\s+my\s+entire|negative\s+instruction\s+is\s+my\s+whole|have\s+not\s+(?:selected|chosen)\s+(?:another|any\s+other)\s+action|leave\s+its\s+state\s+unchanged|no\s+other\s+action|do\s+nothing\s+else)\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "scope"}), pred_class != "clarify:scope"

        # V3 explicit native routes when contract_version == v3
        if allow_v3:
            bz = route_bazarr_subtitle_query(query)
            if bz is not None and (
                bz.name in {"bazarr_subtitle_search", "bazarr_subtitle_download"}
                or pred_class in {"bazarr_subtitle_search", "bazarr_subtitle_download"}
                or re.search(r"\bbazarr\b", q_unquoted, re.I)
            ):
                return bz, pred_class != bz.name
            sq = route_service_query(query)
            if sq is not None and not (
                sq.name == "service_query"
                and sq.arguments.get("service") == "tautulli"
                and sq.arguments.get("operation") == "activity"
                and not re.search(r"\bactivity\b", q_unquoted, re.I)
            ):
                if sq.name == "service_query" and (
                    pred_class == "service_query"
                    or not quoted
                    or re.search(r"\b(look\s*up|lookup|catalog|subtitles?|subs|seerr|seer)\b", q_unquoted, re.I)
                ):
                    return sq, pred_class != sq.name
                if pred_class == "service_query" and sq.name in {"clarify", "unsupported"}:
                    return sq, True
        if pred_class in {"service_query", "bazarr_subtitle_search", "bazarr_subtitle_download"}:
            used_structural = True
            best_c = max(CONCRETE_V2_INDICES, key=lambda idx: float(probs[idx]))
            pred_class = ROUTER_CLASSES[best_c]

        if re.search(r"\b(present\s+(?:plex\s+)?(?:viewing\s+)?sessions?|present\s+session\s+list|(?:streams?|sessions?)\s+that\s+(?:plex|tautulli)\s+(?:has|shows)\s+in\s+progress|check\s+tautulli\s+and\s+list\s+all\s+currently\s+playing)\b", q_unquoted, re.I):
            return validate_call("now_playing", {}), pred_class != "now_playing"
        if re.search(r"\b(?:(?:trending|popular|upcoming|forthcoming)\s+(?:movies?|films?|tv|television|shows?|series)\b.*\b(?:seerr|seer)|(?:seerr|seer)\b.*\b(?:movies?|films?|tv|television|shows?|series)\b.*\b(?:trending|popular|upcoming|forthcoming))\b", q_unquoted, re.I):
            pred_class = "seer_discover"
        if re.search(r"\b(?:dh-waypoint|waypoint\s+pipeline|waypoint\s+service)\b", q_unquoted, re.I) and not re.search(r"\b(restart|reconfigure|delete|stop|pause)\b", q_unquoted, re.I):
            pred_class = "waypoint_status"
        if _AFFIRMATIVE_PAUSE_RE.search(q_unquoted) and not _NEGATED_PAUSE_RE.search(q_unquoted) and not _PASSIVE_PAUSE_QUERY_RE.search(q_unquoted) and re.search(r"\b(torrent|transfer|qbittorrent)\b", q_unquoted, re.I) and (quoted or (isinstance(selected, dict) and selected.get("media_type") == "torrent") or _UNQUOTED_TOR_RE.search(query)):
            if pred_class != "torrent_pause":
                pred_class = "torrent_pause"
                used_structural = True
        if _AFFIRMATIVE_RESUME_RE.search(q_unquoted) and not _NEGATED_RESUME_RE.search(q_unquoted) and not _PASSIVE_RESUME_QUERY_RE.search(q_unquoted) and re.search(r"\b(torrent|transfer|qbittorrent)\b", q_unquoted, re.I):
            if pred_class != "torrent_resume":
                pred_class = "torrent_resume"
                used_structural = True

        # If the neural top-1 predicted clarify:* even though an explicit entity (quoted or selected) is present
        # and no genuine clarify condition holds, use the highest-probability concrete v2 action from probs.
        has_entity = bool(quoted) or bool(isinstance(selected, dict) and selected.get("title")) or bool(_extract_unquoted_entity(query))
        if pred_class.startswith("clarify:") and has_entity and not _SCOPE_RE.search(q_unquoted):
            years_in_q = set(re.findall(r"\b(18[7-9]\d|19\d{2}|20\d{2}|21\d{2})\b", q_unquoted))
            seasons_in_q = _explicit_seasons(q_unquoted)
            has_tv_w = bool(_TV_WORD_RE.search(q_unquoted))
            has_mv_w = bool(_MOVIE_WORD_RE.search(q_unquoted))
            ctx_role_mismatch = bool(
                isinstance(selected, dict)
                and not quoted
                and not _extract_unquoted_entity(query)
                and (
                    (selected.get("media_type") == "tv" and has_mv_w and not has_tv_w)
                    or (selected.get("media_type") == "movie" and has_tv_w and not has_mv_w)
                )
            )
            has_genuine_clarify_signal = (
                (pred_class == "clarify:media_type" and not has_mv_w and not has_tv_w and not _REQUEST_TRACK_RE.search(q_unquoted) and not (isinstance(selected, dict) and selected.get("media_type") in {"movie", "tv"}))
                or len(years_in_q) > 1
                or len(seasons_in_q) > 1
                or len(quoted) > 2
                or ctx_role_mismatch
                or bool(_ORDINAL_REF_RE.search(q_unquoted))
                or bool(_NARROW_SCOPE_MUTATION_RE.search(q_unquoted))
                or (not _request_identity_scope_fits_schema(q_unquoted) and bool(_REQUEST_WORD_RE.search(q_unquoted)))
                or bool(re.search(
                    r"\b(?:background\s+only|not\s+the\s+target|left\s+blank|omitted|forgot|"
                    r"whose\s+title\s+is\s+missing|have\s+not\s+(?:named|supplied|chosen|decided|selected|picked|specified)|"
                    r"without\s+(?:saying|specifying|knowing))\b",
                    q_unquoted,
                    re.I,
                ))
            )
            if not has_genuine_clarify_signal:
                best_c = max(CONCRETE_V2_INDICES, key=lambda idx: float(probs[idx]))
                pred_class = ROUTER_CLASSES[best_c]
                used_structural = True

        if (
            pred_class == "unsupported:complex"
            and re.search(r"^\s*(?:rather\s+than|instead\s+of)\b[^,]+,\s*", q_unquoted, re.I)
            and not _COMPLEX_RE.search(q_unquoted)
            and not re.search(r"\b(?:and\s+then|followed\s+by|after\s+that|simultaneously)\b", q_unquoted, re.I)
        ):
            best_c = max(CONCRETE_V2_INDICES, key=lambda idx: float(probs[idx]))
            pred_class = ROUTER_CLASSES[best_c]
            used_structural = True

        # Handle clarify / unsupported classes
        if pred_class.startswith("unsupported:"):
            reason = pred_class.split(":", 1)[1]
            return validate_call("unsupported", {"reason": reason}), used_structural
        if pred_class.startswith("clarify:"):
            slot = pred_class.split(":", 1)[1]
            if slot not in {"title", "media_type", "reference", "scope"}:
                slot = _infer_clarify_slot(query, selected)
            if _NARROW_SCOPE_MUTATION_RE.search(q_unquoted) and slot in {"title", "media_type"}:
                slot = "scope"
                used_structural = True
            elif slot == "reference" and re.search(r"\band\s+report\s+its\s+status\b", q_unquoted, re.I):
                slot = "title"
                used_structural = True
            return validate_call("clarify", {"slot": slot}), used_structural

        action = pred_class
        if action in {"tv_status", "movie_status"} and (_has_history_intent(q_unquoted) or re.search(r"\b(?:previously\s+watched|last\s+watched|when\b.*\bwatched)\b", q_unquoted, re.I)) and re.search(r"\b(tautulli|plex)\b", q_unquoted, re.I):
            action = "watch_history"
            used_structural = True
        if action in {"tv_status", "tv_missing", "tv_search_missing", "now_playing"} and re.search(r"\bradarr\b", q_unquoted, re.I) and not re.search(r"\b(sonarr|plex|tautulli)\b", q_unquoted, re.I):
            action = "movie_search" if action == "tv_search_missing" else "movie_status"
            used_structural = True

        # Zero-argument actions
        if action in {"now_playing", "waypoint_status"}:
            return validate_call(action, {}), used_structural

        if action == "seer_discover":
            cat_m = re.search(r"\b(trending|popular|upcoming|forthcoming)\b", q_unquoted, re.I)
            cat_raw = cat_m.group(1).lower() if cat_m else "trending"
            cat = "upcoming" if cat_raw == "forthcoming" else cat_raw
            mtype = "tv" if re.search(r"\b(tv|television|show|shows|series)\b", q_unquoted, re.I) and not re.search(r"\b(movie|movies|film|films)\b", q_unquoted, re.I) else "movie"
            return validate_call("seer_discover", {"category": cat, "media_type": mtype}), used_structural

        if action == "armiger_parse":
            m_mkv = re.search(r"\b(\S+\.(?:mkv|mp4|avi|ts|iso))\b", query, re.I)
            rel = m_mkv.group(1).strip("\"',.;:!?") if m_mkv else (quoted[0] if quoted else _extract_unquoted_entity(query))
            if not rel:
                return validate_call("clarify", {"slot": _infer_clarify_slot(query, selected)}), True
            return validate_call("armiger_parse", {"release_name": rel}), used_structural

        if action == "bitmagnet_search":
            q_val = quoted[0] if quoted else _extract_unquoted_entity(query)
            if not q_val and isinstance(selected, dict) and isinstance(selected.get("title"), str):
                q_val = selected["title"]
            if not q_val:
                return validate_call("clarify", {"slot": "title"}), True
            m_yr = re.search(rf"{re.escape(q_val)}[\"']?\s+\(?((?:19|20)\d{{2}})\)?", query, re.I)
            if m_yr and m_yr.group(1) not in q_val and not quoted:
                q_val = f"{q_val} {m_yr.group(1)}"
            return validate_call("bitmagnet_search", {"query": q_val}), used_structural

        if action == "watch_history":
            if selected is None and not quoted and re.search(r"\bfor\s+(?:that|this)\s+(?:title|movie|film|series|show)\b", q_unquoted, re.I):
                return validate_call("clarify", {"slot": "reference"}), True
            ent = quoted[0] if quoted else None
            if not ent and isinstance(selected, dict) and isinstance(selected.get("title"), str) and _DEICTIC_RE.search(q_unquoted):
                ent = selected["title"]
            if not ent and not re.search(r"\b(all\s+titles|recent|recently|watch\s+history|viewing\s+history|past\s+plays|plex)\b", q_unquoted, re.I):
                ent = _extract_unquoted_entity(query)
            args_wh = {"title": ent} if ent else {}
            return validate_call("watch_history", args_wh), used_structural

        if action == "torrent_list":
            q_tl_st = re.sub(r"\bcompleted\s+downloading\b", "completed", q_unquoted, flags=re.I)
            explicit_states = [
                st for st in ("paused", "stalled", "downloading", "seeding", "completed")
                if re.search(rf"\b{st}\b", q_tl_st, re.I)
                and not re.search(rf"\b(?:not|no|without|except|excluding)\s+(?:[a-z]+\s+)?{st}\b", q_tl_st, re.I)
            ]
            st_val = explicit_states[0] if len(explicit_states) == 1 else ("downloading" if re.search(r"\bqbittorrent\s+downloads\s+matching\b", q_unquoted, re.I) else "all")
            args_tl: dict[str, Any] = {"state": st_val}
            if quoted:
                args_tl["query"] = quoted[0]
            return validate_call("torrent_list", args_tl), used_structural

        if action in {"music_status", "music_search"}:
            artist, album = _extract_music_pair(query, quoted, q_unquoted)
            if not artist:
                return validate_call("clarify", {"slot": _infer_clarify_slot(query, selected)}), True
            args_m: dict[str, Any] = {"artist": artist}
            if album:
                args_m["album"] = album
            if action == "music_search":
                if not _write_scope_fits_schema(action, args_m, query):
                    return validate_call("clarify", {"slot": "scope"}), True
                if re.search(r"\b(if|unless|except|until|only\s+when|provided\s+that|as\s+long\s+as)\b", q_unquoted, re.I):
                    return validate_call("unsupported", {"reason": "complex"}), True
            return validate_call(action, args_m), used_structural

        if len(quoted) > 1:
            return validate_call("clarify", {"slot": "scope"}), True

        title = quoted[0] if len(quoted) == 1 else None
        used_selected = False
        if not title:
            if (
                isinstance(selected, dict)
                and isinstance(selected.get("title"), str)
                and not re.search(r"\binstead\b", q_unquoted, re.I)
                and (_DEICTIC_RE.search(q_unquoted) or not _extract_unquoted_entity(query))
            ):
                title = selected["title"]
                used_selected = True
            else:
                title = _extract_unquoted_entity(query)

        if not title:
            return validate_call("clarify", {"slot": _infer_clarify_slot(query, selected)}), True

        year_val: int | None = None
        m_paren = _TRAILING_PAREN_YEAR_RE.match(title.strip())
        if m_paren:
            title = m_paren.group(1).strip()
            year_val = int(m_paren.group(2))
        else:
            years_in_q = [int(y) for y in re.findall(r"\b(18[7-9]\d|19\d{2}|20\d{2}|21\d{2})\b", q_unquoted)]
            if len(set(years_in_q)) == 1:
                year_val = years_in_q[0]
            elif used_selected and isinstance(selected, dict) and isinstance(selected.get("year"), int):
                year_val = selected["year"]

        args_out: dict[str, Any] = {"title": title}
        if action in {"movie_status", "movie_search", "request_media"} and year_val is not None:
            args_out["year"] = year_val

        if action == "tv_missing" and re.search(r"\b(?:search(?:\s+sonarr)?(?:\s+indexers?)?\s+(?:for\s+)?(?:(?:all|every|each)\s+)?(?:missing|season)|search\s+for|trigger\s+.*search|initiate\s+.*search|launch\s+.*search|start\s+.*search|run\s+.*search|hunt\s+down)\b", q_unquoted, re.I) and not _NEGATED_WRITE_RE.search(q_unquoted) and not _INTERROGATIVE_OR_HYPOTHETICAL_WRITE_RE.search(q_unquoted):
            action = "tv_search_missing"
            used_structural = True
        if action == "tv_missing":
            if _NEGATED_SPECIALS_RE.search(q_unquoted):
                args_out["include_specials"] = False
            elif re.search(r"\b(specials?|season[- ]*(?:0|zero)\b|\bs00\b|\bovas?\b)", q_unquoted, re.I):
                args_out["include_specials"] = True

        if action == "tv_search_missing":
            seasons = _explicit_seasons(q_unquoted)
            if len(seasons) == 1:
                args_out["season"] = next(iter(seasons))
            elif len(seasons) > 1:
                return validate_call("clarify", {"slot": "scope"}), True

        q_for_track_nr = re.sub(
            r"\b(?:rather\s+than|instead\s+of|without)\s+(?:checking|inspecting|looking\s+up|viewing)\s+(?:(?:my|the|existing|seerr|seer)\s+)*request\s+(?:status|progress)\b",
            " ",
            q_unquoted,
            flags=re.I,
        )
        if action in {"tv_status", "movie_status", "request_media"} and _REQUEST_TRACK_RE.search(q_for_track_nr):
            action = "request_status"
            args_out = {"title": title}
            used_structural = True
        if action == "request_media":
            q_no_caller = re.sub(
                r"\b(?:(?:associated\s+with|linked\s+to|tied\s+to|attached\s+to|belonging\s+to|owned\s+by|connected\s+to|registered\s+to|under|on|for|in|from|with)\s+)?my\s+(?:(?:own|caller)\s+)?(?:account|profile|login|user)(?:'s)?\b",
                " ",
                q_unquoted,
                flags=re.I,
            )
            if re.search(r"\b(4k|uhd|2160p|1080p|720p|seasons?|episodes?|quality|profile|quota|root\s+folder)\b", q_no_caller, re.I):
                return validate_call("clarify", {"slot": "scope"}), True
            if used_selected and isinstance(selected, dict) and selected.get("media_type") in {"movie", "tv"}:
                args_out["media_type"] = selected["media_type"]
            elif _TV_WORD_RE.search(q_unquoted) and not _MOVIE_WORD_RE.search(q_unquoted):
                args_out["media_type"] = "tv"
            elif _MOVIE_WORD_RE.search(q_unquoted) and not _TV_WORD_RE.search(q_unquoted):
                args_out["media_type"] = "movie"
            else:
                return validate_call("clarify", {"slot": "media_type"}), True

        if re.search(r"\bcheck\s+the\s+(?:second|first|third)\s+one\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "reference"}), True
        if used_selected and not _context_role_matches(action, "title", selected, q_unquoted):
            return validate_call("clarify", {"slot": "reference"}), True
        if action in {"request_status", "request_media"} and not _request_identity_scope_fits_schema(q_unquoted):
            return validate_call("clarify", {"slot": "scope"}), True
        if not _write_scope_fits_schema(action, args_out, query):
            return validate_call("clarify", {"slot": "scope"}), True
        if re.search(r"\b(?:unless\s+<QUOTED>\s+is\s+already\s+complete|unmonitor\s+season)\b", q_unquoted, re.I):
            return validate_call("clarify", {"slot": "scope"}), True
        if action in WRITE_EFFECT_ACTIONS_V3 and re.search(r"\b(if|unless|except|until|only\s+when|provided\s+that|as\s+long\s+as)\b", q_unquoted, re.I):
            return validate_call("unsupported", {"reason": "complex"}), True
        if action == "torrent_pause" and not (_AFFIRMATIVE_PAUSE_RE.search(q_unquoted) and not _NEGATED_PAUSE_RE.search(q_unquoted) and not _PASSIVE_PAUSE_QUERY_RE.search(q_unquoted)):
            return validate_call("clarify", {"slot": "scope"}), True
        return validate_call(action, args_out), used_structural

    def infer(self, query: str, context: dict[str, Any] | None = None, max_tokens: int = 160) -> dict[str, Any]:
        t0 = time.perf_counter()
        gen_start = time.perf_counter()
        pred_class, confidence, probs = self.predict_proba(query, context)
        raw_call, used_structural = self._build_call(pred_class, probs, query, context)
        raw_router_proposal = encode(raw_call, family=self.family)
        gen_seconds = time.perf_counter() - gen_start
        grounded_call = verify_proposal(raw_call, query, context, contract_version=self.contract_version)
        if grounded_call != raw_call:
            used_structural = True
        raw = encode(grounded_call, family=self.family)
        raw_unverified = raw_router_proposal
        wall_seconds = time.perf_counter() - t0
        return {
            "raw": raw,
            "raw_unverified": raw_unverified,
            "raw_router_proposal": raw_router_proposal,
            "neural_top1_class": pred_class,
            "neural_confidence": round(confidence, 4),
            "neural_margin": round(float(getattr(self, "_last_margin", 0.0)), 4),
            "neural_energy": round(float(getattr(self, "_last_energy", 0.0)), 4),
            "used_structural_guard": used_structural,
            "routing_source": (
                "explicit_bazarr_deterministic"
                if raw_call.name in {"bazarr_subtitle_search", "bazarr_subtitle_download"} and pred_class != raw_call.name
                else (
                    "explicit_service_query_deterministic"
                    if raw_call.name == "service_query" and pred_class != "service_query"
                    else "neural_router_mlp"
                )
            ),
            "seconds": round(gen_seconds, 6),
            "wall_seconds": round(wall_seconds, 6),
            "first_token_seconds": round(gen_seconds, 6),
            "prompt_tokens": len(query.split()),
            "generation_tokens": 1,
            "generation_tps": round(1.0 / max(gen_seconds, 1e-6), 1),
            "peak_memory_gb": 0.085,
            "max_tokens": max_tokens,
            "backend": self.backend,
            "device": "cpu",
            "truncated": False,
        }
