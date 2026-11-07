"""Conservative explicit-service NL routes; these are not v6 neural predictions."""
from __future__ import annotations

import re
from typing import Any

from arrmes.protocol import Call
from .contract import BITMAGNET_CONTENT_TYPES, SUPPORTED_OPERATIONS, validate_service_query

_SERVICES = re.compile(
    r"\b(sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|qbittorrent|qbt|tautulli|seerr|seer|bitmagnet)\b",
    re.I,
)
_QUOTED = re.compile(r'"([^"\n]+)"|\'([^\'\n]+)\'')
_WRITE = re.compile(
    r"\b(download|delete|remove|restart|reboot|shutdown|pause|resume|stop|start|update|upgrade|scan|sync|grab|upload|add|change|trigger|initiate|launch|set)\b",
    re.I,
)
_COMPOUND = re.compile(r"\b(and\s+then|then|and\s+also|after\s+that|followed\s+by)\b|[;\n]", re.I)
_UNSAFE = re.compile(
    r"\b(ignore|bypass|override|credentials?|passwords?|tokens?|api\s*keys?|instructions?|system\s*prompt|sudo|shell)\b",
    re.I,
)

_BASE_READ_WORDS = frozenset("""service entity please show list get tell me what which is are the a an my our in on for of
    whether currently check checks display inspect give report look up lookup catalog metadata search health healthy
    online reachable running queue waiting pending queued held sitting stuck history recent events library inventory
    missing wanted activity streams stream playing transfer stats statistics speed speeds bandwidth torrents torrent
    subtitles subtitle subs movie movies film films tv series episodes episode shows show do does have has how many
    if matching status verify items active across all artists artist albums album authors author books book scenes scene
    studios studio entries records right present overview summary configured registered available kindly today
    within from via using through by with to at into about can could would you we i us fetch retrieve pull read view
    query return provide see confirm state condition alive up responding operational stored tracked listed indexed
    logged recorded past log logs ledger listing roster contents collection titles title names name releases release
    downloads transfers sessions session play plays playback watching system daemon instance server endpoint api
    database index files tasks jobs Perform perform run execute
    readout clean its live diagnostics reported snapshot out inside event latest
    cataloged settings definitions real time metrics television existing""".lower().split())

_OP_EXTRA_WORDS: dict[str, frozenset[str]] = {
    "wanted": frozenset({"cutoff", "unmet", "upgrades", "upgrade", "below", "threshold"}),
    "calendar": frozenset({"calendar", "upcoming", "airing", "airings", "schedule", "scheduled", "releases", "release", "broadcast", "broadcasts", "timetable", "slate"}),
    "search": frozenset({"indexers", "indexer", "dht", "swarm", "torrents", "torrent"}),
    "quality_profiles": frozenset({"quality", "profile", "profiles", "quality_profiles", "tiers", "definitions", "presets"}),
    "indexers": frozenset({"indexer", "indexers", "feeds", "trackers", "configured"}),
    "applications": frozenset({"application", "applications", "app", "apps", "synced", "sync", "connected", "downstream", "targets"}),
}

_SPAN_STOPWORDS = frozenset({
    "it", "that", "this", "them", "those", "these", "one", "that one", "this one", "anything", "something", "everything",
    "movie", "movies", "film", "films", "series", "show", "shows", "tv", "episode", "episodes", "subtitle", "subtitles",
    "subs", "artist", "artists", "album", "albums", "author", "authors", "book", "books", "library", "catalog",
    "metadata", "queue", "history", "health", "missing", "wanted", "indexers", "indexer", "applications", "calendar",
    "sonarr", "radarr", "lidarr", "prowlarr", "readarr", "chaptarr", "whisparr", "bazarr", "qbittorrent", "qbt",
    "tautulli", "seerr", "seer", "bitmagnet",
})

_SPAN_AMBIGUOUS_RE = re.compile(
    r"\b(and|or|above|below|under|over|since|before|after|only|except|without|more\s+than|less\s+than|at\s+least|between|from\s+\d{4})\b",
    re.I,
)

_UNQUOTED_PATTERNS = (
    re.compile(
        r"^(?:please\s+)?(?:look\s*up|lookup|search(?:\s+for)?|find)\s+"
        r"(?:(?:the|a|an)\s+)?(?:(?:movie|film|tv|series|show|artist|album|author|book|release)\s+)?"
        r"(?P<span>[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*(?:\s+[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*){0,6}?)\s+"
        r"(?:in|on|via|using|through|from)\s+"
        r"(?:sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|seerr|seer|bitmagnet)"
        r"(?:\s+(?:catalog|metadata|library))?[.!?]?$",
        re.I,
    ),
    re.compile(
        r"\b(?:subtitles?|subs|look\s*up|lookup|search|find|check|show|list|get)\b[^.;?!]*?"
        r"\b(?:for|matching|named|titled|called)\s+"
        r"(?:(?:the|a|an)\s+)?(?:(?:movie|film|tv|series|show|artist|album|author|book)\s+)?"
        r"(?P<span>[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*(?:\s+[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*){0,6}?)"
        r"(?:\s+(?:in|on|via|using|through|from)\s+"
        r"(?:sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|seerr|seer|bitmagnet)"
        r"(?:\s+(?:catalog|metadata|library))?)?[.!?]?$",
        re.I,
    ),
    re.compile(
        r"^(?:please\s+)?(?:search|look\s*up|lookup|find)\s+"
        r"(?:(?:in|on)\s+)?(?:sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|seerr|seer|bitmagnet)\s+"
        r"(?:for|matching)\s+"
        r"(?:(?:the|a|an)\s+)?(?:(?:movie|film|tv|series|show|artist|album|author|book)\s+)?"
        r"(?P<span>[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*(?:\s+[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*){0,6}?)[.!?]?$",
        re.I,
    ),
    re.compile(
        r"^(?:please\s+)?(?:within|in|from|on|using|via)\s+"
        r"(?:sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|seerr|seer|bitmagnet)"
        r"(?:\s+(?:catalog|metadata|library))?[,\s]+(?:please\s+)?"
        r"(?:look\s*up|lookup|search(?:\s+for)?|find)\s+"
        r"(?:(?:the|a|an)\s+)?(?:(?:movie|film|tv|series|show|artist|album|author|book|release)\s+)?"
        r"(?P<span>[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*(?:\s+[A-Za-z0-9\u00C0-\u024F][\w\u00C0-\u024F:'&.-]*){0,6}?)[.!?]?$",
        re.I,
    ),
)


def extract_unquoted_service_span(
    query: str,
    *,
    service: str | None = None,
    operation: str | None = None,
) -> tuple[str | None, float, float]:
    """Extract an unquoted entity span with confidence and margin scoring.

    Returns (span, confidence, margin). Ambiguous, multi-entity, deictic, or
    filter-heavy spans receive low confidence/margin so the caller clarifies.
    """
    if not isinstance(query, str) or not query.strip():
        return None, 0.0, 0.0
    text = query.strip()
    candidates: list[tuple[str, float]] = []
    for idx, pat in enumerate(_UNQUOTED_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        raw_span = m.group("span").strip(" .,?!:;\"'")
        # Strip trailing "in <service>" if captured greedily
        raw_span = re.sub(
            r"\s+(?:in|on|via|using|through|from)\s+"
            r"(?:sonarr|radarr|lidarr|prowlarr|readarr|chaptarr|whisparr|bazarr|seerr|seer|bitmagnet).*$",
            "",
            raw_span,
            flags=re.I,
        ).strip(" .,?!:;\"'")
        if not raw_span or len(raw_span) > 120:
            continue
        lowered = raw_span.lower()
        if lowered in _SPAN_STOPWORDS:
            continue
        words = lowered.split()
        if all(w in _SPAN_STOPWORDS for w in words):
            continue
        if _SPAN_AMBIGUOUS_RE.search(raw_span):
            return None, 0.2, 0.0
        if _WRITE.search(raw_span) or _UNSAFE.search(raw_span):
            return None, 0.1, 0.0
        # Score based on structural pattern specificity and surface capitalization/token properties
        score = 0.82 if idx == 0 else (0.80 if idx == 2 else 0.78)
        if any(w[0].isupper() or any(ch.isdigit() for ch in w) for w in raw_span.split() if w):
            score += 0.10
        if 1 <= len(words) <= 5:
            score += 0.05
        score = min(0.98, score)
        if not any(c[0].casefold() == raw_span.casefold() for c in candidates):
            candidates.append((raw_span, score))

    if not candidates:
        return None, 0.0, 0.0
    candidates.sort(key=lambda item: item[1], reverse=True)
    best_span, best_score = candidates[0]
    second_score = candidates[1][1] if len(candidates) > 1 else 0.0
    margin = round(best_score - second_score if len(candidates) > 1 else best_score - 0.35, 4)
    return best_span, round(best_score, 4), margin


def _canon_service(raw: str) -> str:
    low = raw.lower()
    if low == "qbt":
        return "qbittorrent"
    if low == "seer":
        return "seerr"
    if low == "chaptarr":
        return "readarr"
    return low


def route_service_query(query: str, *, allow_unquoted_bitmagnet: bool = False) -> Call | None:
    """Return a deterministic v3 read or abstention, or leave a v2 intent alone.

    Service names inside quoted entity text are ignored. Entity-bearing
    lookup/subtitle routes accept either a single quoted surface span or a
    single high-margin unquoted entity span. Missing titles, conflicting scopes
    and compounds clarify/refuse instead of guessing.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        return None
    quoted = [next(v for v in m.groups() if v is not None) for m in _QUOTED.finditer(query)]
    plain = _QUOTED.sub(" <ENTITY> ", query)
    mentions = _SERVICES.findall(plain)
    services = {_canon_service(s) for s in mentions}
    if not services:
        return None
    plain_no_neg_contrast = re.sub(
        r"^\s*(?:(?:please|kindly)\s+)?(?:do\s+not|don['’]?t|never|hold\s+off\s+on|refrain\s+from|skip|avoid|without)\b[^;]*;\s*",
        "",
        plain,
        flags=re.I,
    )
    if len(services) != 1 or _COMPOUND.search(plain_no_neg_contrast):
        return Call("unsupported", {"reason": "complex"})
    plain = plain_no_neg_contrast
    service = next(iter(services))
    if _UNSAFE.search(plain):
        return Call("unsupported", {"reason": "unsafe"})
    if service == "seerr" and re.search(r"\brequests?\b", plain, re.I) and not _WRITE.search(plain):
        return None
    if service == "bitmagnet" and re.search(r"\b(import|reprocess|reclassify|classify|enqueue|purge)\b", plain, re.I):
        return Call("unsupported", {"reason": "out_of_scope"})
    # Note: "sync" as a noun in "application sync" for Prowlarr is a read, not a write
    plain_for_write = (
        re.sub(r"\b(?:(?:application|app)s?\s+sync(?:\s+status)?|sync(?:ed)?\s+(?:application|app)s?)\b", " applications ", plain, flags=re.I)
        if service == "prowlarr"
        else plain
    )
    if _WRITE.search(plain_for_write):
        return (
            Call("unsupported", {"reason": "out_of_scope"})
            if service in {"bazarr", "seerr", "bitmagnet", "prowlarr", "readarr", "whisparr"}
            else None
        )

    operations: set[str] = set()
    if re.search(r"\b(health|healthy|online|reachable|running)\b", plain, re.I):
        operations.add("health")
    if re.search(r"\bqueue\b", plain, re.I):
        operations.add("queue")
    if re.search(r"\b(history|recent\s+events)\b", plain, re.I):
        operations.add("history")
    if re.search(r"\b(library|inventory)\b", plain, re.I):
        operations.add("library")
    if re.search(r"\b(quality[\s_]+profiles?)\b", plain, re.I):
        operations.add("quality_profiles")
        operations.discard("library")
    if re.search(r"\b(calendar|upcoming\s+(?:airings?|episodes?|releases?|movies?|books?|albums?)|airing\s+schedule)\b", plain, re.I):
        operations.add("calendar")
        operations.discard("library")
    if re.search(r"\b(indexers?)\b", plain, re.I):
        operations.add("indexers")
        operations.discard("health")
        operations.discard("library")
    if re.search(r"\b(applications?|app\s+sync)\b", plain, re.I) and service == "prowlarr":
        operations.add("applications")
        operations.discard("health")
    if re.search(r"\b(cutoff[\s-]*unmet|unmet\s+cutoff|wanted\s+cutoff|cutoff\s+wanted)\b", plain, re.I):
        operations.add("wanted")
        operations.discard("missing")
        operations.discard("library")
    elif re.search(r"\bmissing\b", plain, re.I):
        operations.add("missing")
        if re.search(
            r"\b(?:in|from|within|across)\s+(?:my|our|the|your|sonarr|radarr|lidarr|readarr|chaptarr|whisparr)?\s*"
            r"(?:sonarr|radarr|lidarr|readarr|chaptarr|whisparr)?\s*library\b|\bmissing\b[^.;?!]*?\b(?:inventory|library)\b",
            plain,
            re.I,
        ):
            operations.discard("library")
    elif re.search(r"\bwanted\b", plain, re.I):
        if service == "bazarr":
            operations.add("missing")
        else:
            operations.add("wanted")
            operations.discard("library")
    if re.search(r"\b(activity|streams|playing|transfer\s+(?:stats|statistics|speeds?)|bandwidth)\b", plain, re.I):
        if not ("history" in operations and re.search(r"\bactivity\s+history\b", plain, re.I)):
            operations.add("activity")
    if re.search(r"\b(look\s*up|lookup|metadata\s+search|catalog\s+search|search\s+(?:the\s+)?(?:catalog|metadata))\b", plain, re.I):
        operations.discard("library")
        operations.add("search")
    if service in {"bitmagnet", "prowlarr", "seerr"} and re.search(r"\b(search|find)\b", plain, re.I):
        operations.discard("indexers")
        operations.add("search")
    if service == "bazarr" and re.search(r"\b(subtitles?|subs)\b", plain, re.I) and "missing" not in operations and "history" not in operations:
        operations.add("subtitles")
    if service == "qbittorrent" and re.search(r"\btorrents?\b", plain, re.I) and not operations:
        return None
    if not operations:
        return None
    if len(operations) != 1:
        return Call("unsupported", {"reason": "complex"})
    operation = next(iter(operations))
    if operation not in SUPPORTED_OPERATIONS[service]:
        return Call("unsupported", {"reason": "out_of_scope"})

    # Content-type filter detection for Bitmagnet
    bm_content_type: str | None = None
    if service == "bitmagnet" and operation == "search":
        m_ct = re.search(
            r"\b(?:content[\s_-]*type|category)\s*[:=]?\s*(movie|tv_show|music|ebook|audiobook|comic|software|game|xxx)\b",
            plain,
            re.I,
        )
        if m_ct and m_ct.group(1).lower() in BITMAGNET_CONTENT_TYPES:
            bm_content_type = m_ct.group(1).lower()

    if service == "bitmagnet" and operation == "search" and not quoted and not allow_unquoted_bitmagnet and bm_content_type is None:
        # Preserve the released neural v2 bitmagnet_search path for unquoted Bitmagnet searches.
        if re.search(r"\b(?:for|about|matching|named|titled|called)\s+\S", plain, re.I):
            return None

    # Existing entity-specific missing queries on Sonarr/Radarr/Lidarr belong to the frozen v2 route.
    if operation == "missing" and service in {"sonarr", "radarr", "lidarr"} and (
        quoted
        or re.search(r"\b(?:search|find|hunt\s+down|run\s+a\s+search)\b", plain, re.I)
        or re.search(r"\b(?:for|of)\s+(?:the|a|an|this|that)\s+(?:tv\s+)?(?:show|series|movie|film|artist|album)\b", plain, re.I)
        or (service == "sonarr" and (
            re.search(r"\b(?:episodes?\s+of|missing\s+(?:(?:regular\s+)?episodes?\s+)?(?:for|of))\s+[A-Z0-9\u00C0-\u024F][\w:'&.-]*", query)
            or re.search(r"\b(?:for\s+)?(?:(?:all|every|each)\s+)?(?:missing\s+)?(?:regular\s+)?(?!Sonarr\b|sonarr\b)[A-Z0-9\u00C0-\u024F][\w:'&.-]+\s+(?:regular\s+)?episodes?\b", query)
        ))
    ):
        return None
    if operation == "queue" and service == "qbittorrent" and re.search(r"\b(paused|stopped|downloading|seeding|completed|stalled)\b", plain, re.I):
        return None

    args: dict[str, Any] = {"service": service, "operation": operation}
    if bm_content_type is not None:
        args["content_type"] = bm_content_type

    # Monitored filter on Arr library/missing/wanted
    if service in {"sonarr", "radarr", "lidarr", "readarr", "whisparr"} and operation in {"library", "missing", "wanted"}:
        has_unmon = bool(re.search(r"\bunmonitored\b", plain, re.I))
        has_mon = bool(re.search(r"\b(?<!un)monitored\b", plain, re.I))
        if has_unmon and has_mon:
            return Call("clarify", {"slot": "scope"})
        if has_unmon:
            args["monitored"] = False
        elif has_mon:
            args["monitored"] = True

    if service == "bazarr" and operation in {"missing", "subtitles"}:
        movie = bool(re.search(r"\b(movies?|films?)\b", plain, re.I))
        tv = bool(re.search(r"\b(tv|television|series|episodes?|shows)\b|\bshow\s+subtitles?\b", plain, re.I))
        if movie == tv:
            return Call("clarify", {"slot": "media_type"})
        args["media_type"] = "movie" if movie else "tv"

    unquoted_span_used: str | None = None
    if operation in {"search", "subtitles"}:
        if len(quoted) == 1:
            args["query"] = quoted[0]
        elif len(quoted) > 1:
            return Call("clarify", {"slot": "reference"})
        else:
            span, conf, margin = extract_unquoted_service_span(query, service=service, operation=operation)
            if span is not None and conf >= 0.75 and margin >= 0.25:
                args["query"] = span
                unquoted_span_used = span
            else:
                return Call("clarify", {"slot": "title"})
    elif quoted and (
        operation in {"library", "quality_profiles", "indexers", "calendar", "applications"}
        or (operation == "queue" and service == "qbittorrent")
    ):
        if len(quoted) != 1:
            return Call("clarify", {"slot": "reference"})
        args["query"] = quoted[0]
    elif quoted:
        return None if service == "tautulli" and operation == "history" else Call("clarify", {"slot": "scope"})

    # Check for unimplemented filters (language, date range, transfer state, exclusions, ordering)
    plain_clean = re.sub(
        r"\b(?:online\s+and\s+healthy|transfer\s+and\s+stream|query\s+(?:\w+)\s+and\s+report)\b",
        lambda m: m.group(0).replace(" and ", " "),
        plain,
        flags=re.I,
    )
    if unquoted_span_used is not None:
        plain_clean = re.sub(re.escape(unquoted_span_used), " <ENTITY> ", plain_clean, count=1, flags=re.I)
    words = set(re.findall(r"[A-Za-z0-9_]+", _SERVICES.sub(" SERVICE ", plain_clean).lower()))
    allowed_words = set(_BASE_READ_WORDS) | _OP_EXTRA_WORDS.get(operation, frozenset())
    if "monitored" in args:
        allowed_words |= {"monitored", "unmonitored", "only"}
    if "content_type" in args:
        allowed_words |= {"content", "type", "content_type", "category", args["content_type"]}
    if words - allowed_words:
        filler = {"current", "now", "using", "via", "from"}
        if service == "tautulli":
            filler |= {"plex"}
        if service in {"bitmagnet", "prowlarr", "seerr"}:
            filler |= {"find"}
        if words - allowed_words - filler:
            return Call("clarify", {"slot": "scope"})
    return Call("service_query", validate_service_query(args))
