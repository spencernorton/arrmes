"""Trusted application boundary, entity-agnostic proposal grounding, and pre-dispatch verification."""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import re
from typing import Any, Iterable
import unicodedata

from .catalog import (BY_NAME, CONTRACT_VERSION, CONTRACT_VERSION_V3, V3_BY_NAME,
                      contract_hash, contract_hash_v3)
from .protocol import Call, normalized, validate_call


@dataclass(frozen=True)
class Principal:
    """Only an authenticated host application may construct this object."""
    subject: str
    scopes: frozenset[str]


class ResolutionRequired(ValueError):
    pass


_DEICTIC_RE = re.compile(
    r"\b(it|its|that|this|one|ones|second|first|selected|discussed|same)\b",
    re.IGNORECASE,
)
_SCOPE_RE = re.compile(
    r"\b(fix|fixing|handle|take\s+care\s+of|deal\s+with|manage|do\s+something|"
    r"something\s+is\s+wrong|whatever\s+is\s+going\s+on|problem|issue|setup|troubleshoot|clean\s+up|"
    r"sort\s+out|do\s+the\s+thing|help\s+with\s+my\s+downloads|sonarr\s+please)\b",
    re.IGNORECASE,
)
_REQUEST_WORD_RE = re.compile(r"\b(request|requests|requested)\b", re.IGNORECASE)
_REQUEST_TRACK_RE = re.compile(
    r"\b(track|status\s+of\s+my\s+request|where\s+(?:is|has)\s+my\s+.+\s+request|"
    r"existing\s+request|request\s+status|request\s+progress|how\s+is\s+my\s+.*request|"
    r"any\s+(?:update|progress)\s+on\s+my\s+.*request|has\s+my\s+request\s+.*finished|"
    r"is\s+.+\s+(?:already\s+)?requested|did\s+my\s+request\s+.*go\s+through|"
    r"(?:is|was|has|did)\s+(?:the|my|our)\s+request)\b",
    re.IGNORECASE,
)
_INTERROGATIVE_OR_HYPOTHETICAL_WRITE_RE = re.compile(
    r"(?:"
    r"\b(?:do|does|did|would|should|must|might|can|could)\s+(?:i|we|one|anyone|somebody)\b"
    r"|\b(?:is\s+it\s+(?:necessary|needed|required|possible|safe|worthwhile)\s+to|is\s+there\s+a\s+(?:need|reason)\s+to)\b"
    r"|\b(?:what\s+happens\s+if|what\s+if|suppose\s+(?:i|we)|if\s+(?:i|we|you)\s+(?:were\s+to\s+)?(?:request|search|trigger|download|grab|pause|resume))\b"
    r"|^\s*(?:how|why|when|where|who)\s+(?:do|does|did|can|could|should|would|is|are|was|were|has|have)\b"
    r"|^\s*(?:has|had|did|is|are|was|were|does)\b"
    r"|^\s*have\s+(?:the|a|an|my|our|we|you|they|any|these|those)\b"
    r"|\b(?:has|have|had|did|is|are|was|were)\s+(?:the|a|an|my|our|that|this|any)?\s*(?:indexer\s+|missing[- ]episode\s+|season\s+|movie\s+|tv\s+|series\s+|music\s+|album\s+|artist\s+|torrent\s+)?(?:search(?:es|ing)?|request(?:s|ing)?|download(?:s|ing)?|transfer(?:s)?)\b"
    r"|\b(?:search(?:es|ing)?|request(?:s|ing)?|download(?:s|ing)?)\b[^.;?!]*?\b(?:finished|completed|done|succeeded|failed|still\s+running|in\s+progress|go\s+through|went\s+through)\b"
    r"|\b(?:did|has|have|is|was)\s+(?:radarr|sonarr|lidarr|seer|overseerr|jellyseerr|qbittorrent)\s+(?:already\s+|still\s+|currently\s+)?(?:search(?:ed|ing)?|request(?:ed|ing)?|download(?:ed|ing)?|finish(?:ed)?|complet(?:e|ed)|paus(?:e|ed)|resum(?:e|ed))\b"
    r")",
    re.IGNORECASE,
)
_AFFIRMATIVE_REQUEST_RE = re.compile(
    r"\b(request|add\s+.+\s+(?:to|through)\s+(?:my\s+)?requests?|submit\s+(?:a\s+)?(?:new\s+)?(?:movie\s+|tv\s+|series\s+|film\s+|show\s+)?request|put\s+in\s+(?:a\s+)?(?:new\s+)?(?:movie\s+|tv\s+|series\s+|film\s+|show\s+)?request)\b",
    re.IGNORECASE,
)
_NEGATED_REQUEST_RE = re.compile(
    r"\b(?:"
    r"(?:don'?t|do\s+not|never|not|without|instead\s+of|avoid|skip|refrain\s+from)\s+"
    r"(?:(?:ever|actually|automatically)\s+)?"
    r"(?:request(?:ing)?|(?:submit(?:ting)?|put(?:ting)?\s+in|plac(?:e|ing)|fil(?:e|ing)|log(?:ging)?|open(?:ing)?|creat(?:e|ing)|mak(?:e|ing)|send(?:ing)?|add(?:ing)?)\b[^.;?!]*?\b(?:requests?|seer|overseerr|jellyseerr|wishlist)\b)"
    r"|no\s+(?:new\s+)?(?:movie\s+|tv\s+|series\s+|film\s+|show\s+)?requests?\b"
    r")\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_PAUSE_RE = re.compile(r"\b(pause|halt|suspend)\b", re.IGNORECASE)
_NEGATED_PAUSE_RE = re.compile(
    r"\b(don't\s+(?:pause|halt|suspend)|do\s+not\s+(?:pause|halt|suspend)|never\s+(?:pause|halt|suspend)|without\s+(?:pausing|halting|suspending))\b",
    re.IGNORECASE,
)
_PASSIVE_PAUSE_QUERY_RE = re.compile(
    r"^\s*(?:is|are|was|were|has|have|which|what|list|show|check|display|verify|inspect)\b.*\bpaused\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_RESUME_RE = re.compile(r"\b(resume|unpause)\b", re.IGNORECASE)
_NEGATED_RESUME_RE = re.compile(
    r"\b(don't\s+(?:resume|unpause)|do\s+not\s+(?:resume|unpause)|never\s+(?:resume|unpause)|without\s+(?:resuming|unpausing))\b",
    re.IGNORECASE,
)
_PASSIVE_RESUME_QUERY_RE = re.compile(
    r"^\s*(?:is|are|was|were|has|have|did|which|what|list|show|check|display|verify|inspect)\b.*\b(?:resumed|unpaused)\b",
    re.IGNORECASE,
)
_TORRENT_READ_VERB_RE = re.compile(
    r"\b(is|are|was|were|has|have|did|which|what|list|show|check|display|verify|inspect|status)\b",
    re.IGNORECASE,
)
_TRAILING_PAREN_YEAR_RE = re.compile(r"^(.*\S)\s+\((18[7-9]\d|19\d{2}|20\d{2}|21\d{2})\)$")
_MOVIE_WORD_RE = re.compile(r"\b(movie|movies|film|films|radarr)\b", re.IGNORECASE)
_TV_WORD_RE = re.compile(r"\b(tv|series|show|shows|episodes|episode|season|sonarr)\b", re.IGNORECASE)
_MEDIA_OR_SERVICE_WORD_RE = re.compile(
    r"\b(movie|movies|film|films|radarr|tv|series|show|shows|episodes|episode|season|sonarr)\b",
    re.IGNORECASE,
)
_NEGATED_SPECIALS_RE = re.compile(
    r"\b(no\s+specials?|without\s+specials?|without\s+special\s+episodes?|excluding\s+specials?|"
    r"exclude\s+specials?|skip\s+specials?|ignore\s+specials?|not\s+including\s+specials?|"
    r"don't\s+include\s+specials?|do\s+not\s+include\s+specials?)\b",
    re.IGNORECASE,
)
_WRITE_ACTION_VERBS_PATTERN = (
    r"search(?:ing)?|indexers?|download(?:ing)?|grab(?:bing)?|fetch(?:ing)?|"
    r"trigger(?:ing)?|pull(?:ing)?|retr(?:y|ying)|dispatch(?:ing)?|initiat(?:e|ing)|"
    r"kick(?:ing)?\s+off|start(?:ing)?(?:\s+a)?|run(?:ning)?(?:\s+a)?|request(?:ing)?|"
    r"missingepisodesearch|seasonsearch|moviesearch|albumsearch|artistsearch"
)
_SEARCH_DOWNLOAD_TRIGGER_RE = re.compile(
    rf"(?:\b(?:{_WRITE_ACTION_VERBS_PATTERN})\b|missingepisodesearch|seasonsearch|moviesearch|albumsearch|artistsearch)",
    re.IGNORECASE,
)
_NEGATED_WRITE_RE = re.compile(
    rf"\b(?:"
    rf"(?:do\s+not|don't|dont|never|without|no|instead\s+of|rather\s+than|avoid|refrain\s+from)"
    rf"(?:\s+(?:ever|actually|automatically|any|a|an|the|new|more|further|or|and)){{0,3}}\s+(?:{_WRITE_ACTION_VERBS_PATTERN})"
    rf"|only\s+(?:report|parse|check|list|tell\s+me)"
    rf"|inventory\s+check\s+only|check\s+.+\s+only"
    rf")\b",
    re.IGNORECASE,
)
_SEASON_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}
_SEASON_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}
_SEASON_NUM_RE = re.compile(
    r"\bseason\s*(?:#\s*)?0*(\d{1,2})\b|\bs0*(\d{1,2})(?:\s*e\d{1,3})?\b"
    r"|\bseason\s+(" + "|".join(_SEASON_WORDS) + r")\b"
    r"|\b(" + "|".join(_SEASON_ORDINALS) + r")\s+season\b",
    re.IGNORECASE,
)
_ALL_SEASONS_RE = re.compile(
    r"\b(all\s+(?:the\s+)?seasons|every\s+season|each\s+season|entire\s+series|whole\s+(?:series|show)|full\s+series)\b",
    re.IGNORECASE,
)
_SEASON_MENTION_RE = re.compile(r"\bseasons?\b|\bs\d{1,2}\b", re.IGNORECASE)


def _explicit_seasons(query: str) -> set[int]:
    found: set[int] = set()
    for m in _SEASON_NUM_RE.finditer(query):
        num, s_num, word, ordinal = m.groups()
        if num is not None:
            found.add(int(num))
        elif s_num is not None:
            found.add(int(s_num))
        elif word is not None:
            found.add(_SEASON_WORDS[word.lower()])
        elif ordinal is not None:
            found.add(_SEASON_ORDINALS[ordinal.lower()])
    return found


_HISTORY_WORD_RE = re.compile(
    r"\b(history|watched\s+recently|was\s+watched|recently\s+watched)\b",
    re.IGNORECASE,
)

_COMPLEX_RE = re.compile(
    r"\b("
    r"and\s+also|and\s+simultaneously|and\s+then|then\s+(?:download|request|pause|restart)|"
    r"request\s+both\b|both\s+.+\s+and\s+.+\s+at\s+the\s+same\s+time|"
    r"(?:list|show|check)\s+(?:active\s+plex\s+)?streams?\b.*\band\s+(?:request|pause|restart)\b|"
    r"(?:check|list)\s+(?:missing\s+.+|the\s+movie\s+.+|.+\s+in\s+(?:sonarr|radarr)|tautulli)\s+and\s+(?:request|list|restart|pause)\b|"
    r"do\s+three\s+different\s+operations\b"
    r")",
    re.IGNORECASE,
)

_UNSAFE_RE = re.compile(
    r"\b("
    r"pause\s+(?:all|every)\b|delete\s+(?:all|every|the\s+entire)\b|delete\s+.+\s+from\s+(?:radarr|sonarr|lidarr|disk)\b|"
    r"remove\s+(?:all\s+torrent|.+and\s+delete)\b|erase\s+all\b|purge\s+the\b|wipe\s+(?:all|the)\b|"
    r"restart\s+(?:the|sonarr|radarr|lidarr|tautulli|qbittorrent|dh-waypoint)\b|reboot\s+the\b|"
    r"execute\s+a\s+shell\b|stop\s+(?:every|everyone's)\s+.*streams?\b|retrain\s+armiger\b|"
    r"reconfigure\s+dh-waypoint\b|disable\s+(?:authentication|validation)\b|post\s+arbitrary\s+json\b|"
    r"run\s+curl\s+on\b|export\s+the\s+server\s+credentials\b|change\s+my\s+user\s+role\s+to\s+admin\b|"
    r"api\s+keys?\b|api\s+tokens?\b|secret\s+keys?\b|x-api-key\b|"
    r"admin\s+(?:permissions?|privileges?)\b|modify\s+my\s+request-service\s+quota\b"
    r")",
    re.IGNORECASE,
)

# Pure contract/service vocabulary and deictic placeholders (zero benchmark entity names).
_DEICTIC_PLACEHOLDERS = frozenset({
    "it", "its", "that", "this", "one", "that one", "this one", "the one",
    "the second one", "the first one", "second one", "first one",
    "the second result", "the first result", "second result", "first result",
    "that series", "this series", "that show", "this show",
    "that movie", "this movie", "that film", "this film", "its movie",
    "that torrent", "this torrent", "the selected movie", "selected movie",
    "the selected series", "selected series", "the selected show", "selected show",
    "the selected film", "selected film", "the one we discussed", "one we discussed",
    "missing ones", "the missing ones", "its missing episodes", "the same one", "same one",
    "this release", "other version", "the other version", "latest one", "the latest one",
    "that album", "this album", "the selected album", "selected album", "that artist", "this artist",
})

_GENERIC_DOMAIN_PLACEHOLDERS = frozenset({
    "movie", "the movie", "a movie", "movies",
    "film", "the film", "a film", "films",
    "series", "the series", "a series", "tv series", "a tv series", "the tv series",
    "show", "the show", "a show", "tv show", "a tv show", "the tv show", "shows",
    "episode", "episodes", "missing", "missing episode", "missing episodes",
    "the missing episodes", "missing tv episodes", "tv episodes", "missing episode search",
    "torrent", "the torrent", "a torrent", "torrents", "all torrents", "active torrent", "the active torrent", "active torrents",
    "torrent transfer", "the torrent transfer", "transfer", "the transfer",
    "active torrent download", "the active torrent download", "torrent download",
    "torrent title", "media", "library", "collection", "disk", "status", "request", "requests",
    "sonarr", "radarr", "lidarr", "tautulli", "qbittorrent", "plex", "seer",
    "armiger", "bitmagnet", "waypoint", "dh waypoint", "indexers", "indexer",
    "history", "recent", "viewing history", "watch history", "request service quota",
    "live production data", "all",
    "album", "the album", "an album", "albums", "new album", "the new album", "latest album", "the latest album",
    "artist", "the artist", "an artist", "artists",
})

_MANDATORY_ENTITY_SLOTS: dict[str, str] = {
    "tv_status": "title",
    "tv_missing": "title",
    "tv_search_missing": "title",
    "movie_status": "title",
    "movie_search": "title",
    "torrent_pause": "title",
    "torrent_resume": "title",
    "request_status": "title",
    "request_media": "title",
    "music_status": "artist",
    "music_search": "artist",
    "bitmagnet_search": "query",
    "armiger_parse": "release_name",
}

_OPTIONAL_ENTITY_SLOTS: dict[str, str] = {
    "watch_history": "title",
    "torrent_list": "query",
    "music_status": "album",
    "music_search": "album",
}

# Transfer states and relative selectors describe a set, not a unique title.
# A literal torrent named "Active" remains usable when quoted or established
# by trusted context. This vocabulary comes from transfer semantics, not media.
_TRANSFER_DESCRIPTORS = frozenset({
    "active", "inactive", "paused", "stopped", "running", "downloading", "seeding",
    "completed", "stalled", "queued", "checking", "errored", "first", "second", "third",
    "last", "latest", "newest", "oldest", "selected", "current",
})
_TRANSFER_REFERENCE_WORDS = _TRANSFER_DESCRIPTORS | {"the", "a", "an", "torrent", "download", "transfer", "one"}


def _norm_alnum(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", stripped.casefold(), flags=re.UNICODE).replace("_", " ").strip()


def _strip_quoted_and_releases(query: str) -> str:
    s = re.sub(r'"[^"]+"', " <QUOTED> ", query)
    s = re.sub(r"(?:^|(?<=\s))'[^']+'(?=$|\s|[?.!,;:])", " <QUOTED> ", s)
    s = re.sub(r"\S+\.mkv\b", " <RELEASE> ", s, flags=re.IGNORECASE)
    return s


def _infer_clarify_slot(query: str, selected: dict[str, Any] | None = None) -> str:
    if selected is None and _DEICTIC_RE.search(query):
        return "reference"
    if selected is not None and re.search(r"\b(other\s+version|second\s+one|first\s+one|latest\s+one)\b", query, re.IGNORECASE):
        return "reference"
    if _SCOPE_RE.search(query):
        return "scope"
    if _REQUEST_WORD_RE.search(query) and not _MEDIA_OR_SERVICE_WORD_RE.search(query):
        return "media_type"
    return "title"


def _is_grounded_in_text(entity: str, text: str) -> bool:
    ent_norm = _norm_alnum(entity)
    txt_norm = _norm_alnum(text)
    if not ent_norm or not txt_norm:
        return False
    # Alien is not a surface mention of Aliens, and a joined sequence of
    # unrelated words is not a title. Punctuation/diacritics normalize above;
    # explicit span recovery lives in _snap_entity_to_query, never here.
    return bool(re.search(rf"(?:^|\s){re.escape(ent_norm)}(?:$|\s)", txt_norm))


def _snap_entity_to_query(entity: str, query: str) -> str | None:
    """Correct minor BPE/spelling slips or missing leading articles back to the exact surface span in query."""
    m_yr_title = re.search(
        rf"\b{re.escape(entity)}\s+([A-Z][A-Za-z0-9:'-]*(?:\s+[A-Z][A-Za-z0-9:'-]*)*)\s+(?:film|movie|series|show)\b",
        query,
    )
    if m_yr_title and re.fullmatch(r"\d{4}", entity.strip()):
        return m_yr_title.group(1).strip()

    ent_norm = _norm_alnum(entity)
    if _is_grounded_in_text(entity, query):
        if not entity.casefold().startswith("the "):
            m_the = re.search(
                rf"(?:^|(?:for|of|download|request|erase|episodes\s+of|about|check)\s+)(The\s+{re.escape(entity)})\b",
                query,
            )
            if m_the:
                return m_the.group(1)
        m_colon = re.search(rf"\b([A-Z0-9]{{2,}}:\s*{re.escape(entity)})\b", query)
        if m_colon:
            return m_colon.group(1)
        return entity

    ent_words = entity.split()
    n = len(ent_words)
    if n == 0:
        return None
    tokens = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9':.-]+", query)]
    best_span = None
    best_score = 0.0
    for length in range(max(1, n - 1), min(len(tokens), n + 1) + 1):
        for i in range(len(tokens) - length + 1):
            cand = query[tokens[i][1] : tokens[i + length - 1][2]].strip(".,?!:;\" ")
            if not cand or not cand[0].isupper():
                continue
            c_norm = _norm_alnum(cand)
            if not c_norm or c_norm in _GENERIC_DOMAIN_PLACEHOLDERS or c_norm in _DEICTIC_PLACEHOLDERS:
                continue
            ratio = difflib.SequenceMatcher(None, ent_norm, c_norm).ratio()
            c_words = c_norm.split()
            e_words = ent_norm.split()
            shared = any(
                cw == ew or (len(cw) >= 4 and len(ew) >= 4 and cw[:4] == ew[:4])
                for cw in c_words
                for ew in e_words
            )
            if ratio >= 0.74 and shared and ratio > best_score:
                best_score = ratio
                best_span = cand
    return best_span


def verify_proposal(
    call: Call,
    query: str,
    context: dict[str, Any] | None = None,
    *,
    contract_version: str = "v2",
) -> Call:
    """General, entity-agnostic grounding and pre-dispatch verification layer.

    Enforces that:
    0. Compound multi-action and destructive/privileged instructions outside quotes fail closed.
    1. Every extracted mandatory/optional entity slot (title, artist, album, query, release_name)
       is grounded in the user utterance or trusted context.selected_media (and never a generic
       service/media noun or unbound pronoun).
    2. Parenthesized years in titles ('Title (YYYY)') are normalized into title='Title', year=YYYY.
    3. Explicit context overrides ('instead') do not retain stale selected_media titles.
    4. Ungrounded years/seasons are stripped unless present in query or matched selected_media.
    5. Affirmative, non-negated, action-specific intent is required for EVERY write action
       (tv_search_missing, movie_search, music_search, request_media, torrent_pause, torrent_resume);
       absent or negated write intent downgrades to the corresponding read or clarify.
    6. Never fabricates a concrete tool call when the proposal is clarify or unsupported.
    """
    call = validate_call(call.name, call.arguments)
    name = call.name
    args = dict(call.arguments)
    selected = (context or {}).get("selected_media") if isinstance(context, dict) else None
    q_unquoted = _strip_quoted_and_releases(query)

    # 0. Compound multi-action, unsafe instruction, and vague-scope guard (ignoring quoted titles and .mkv filenames)
    if _COMPLEX_RE.search(q_unquoted):
        return validate_call("unsupported", {"reason": "complex"})
    if _UNSAFE_RE.search(q_unquoted):
        return validate_call("unsupported", {"reason": "unsafe"})
    if _SCOPE_RE.search(q_unquoted):
        return validate_call("clarify", {"slot": "scope"})
    if name in {"request_status", "request_media"}:
        # The released request schema cannot carry quality/season/other-user
        # restrictions. Do not silently discard them when choosing a provider.
        scope_text = unicodedata.normalize("NFKC", q_unquoted).casefold()
        literal = " ".join(unicodedata.normalize("NFKC", str(args.get("title", ""))).casefold().split())
        if literal:
            scope_text = re.sub(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", " ENTITY ", scope_text)
        if re.search(r"\b(4k|uhd|2160p|1080p|720p|seasons?|episodes?|quality|profile|quota|on\s+behalf)\b|\bas\s+(?!movie\b|tv\b)\w+", scope_text):
            return validate_call("clarify", {"slot": "scope"})
        if name == "request_status" and re.search(r"\b(?:18\d{2}|19\d{2}|20\d{2}|21\d{2}|2200|movies?|films?|tv|series)\b", scope_text):
            return validate_call("clarify", {"slot": "scope"})
        if name == "request_status" and isinstance(selected, dict) and _DEICTIC_RE.search(q_unquoted):
            if any(selected.get(key) is not None for key in ("year", "media_type", "season")):
                # The title-only v2 status call cannot retain a qualified
                # selected entity. Ask for an unqualified unique title rather
                # than looking up another remake after losing the qualifier.
                return validate_call("clarify", {"slot": "scope"})
    if name in {"bazarr_subtitle_search", "bazarr_subtitle_download"}:
        from .services.bazarr_routing import route_bazarr_subtitle_query
        grounded = route_bazarr_subtitle_query(query)
        if grounded is None:
            return validate_call("clarify", {"slot": "scope"})
        if grounded.name in {"clarify", "unsupported"}:
            return grounded
        if normalized(grounded) != normalized(call):
            return validate_call("clarify", {"slot": "scope"})
        return validate_call(grounded.name, grounded.arguments)
    if name == "service_query":
        from .services import route_service_query
        if contract_version in {"v1", CONTRACT_VERSION}:
            return validate_call("unsupported", {"reason": "out_of_scope"})
        grounded = route_service_query(query)
        if grounded is None:
            return validate_call("clarify", {"slot": "scope"})
        if grounded.name in {"clarify", "unsupported"}:
            return grounded
        if normalized(grounded) != normalized(call):
            return validate_call("clarify", {"slot": "scope"})
        return validate_call(grounded.name, grounded.arguments)
    if name == "unsupported" and args.get("reason") in {"complex", "unsafe"}:
        if re.search(r"\b(torrent|qbittorrent|sonarr|radarr|lidarr|tautulli|plex|seer|episode|episodes|movie|series|show)\b", q_unquoted, re.IGNORECASE):
            return validate_call("clarify", {"slot": _infer_clarify_slot(query, selected)})
        return validate_call("unsupported", {"reason": "out_of_scope"})

    # 0b. Service & Tautulli history alignment before entity checks
    if name == "now_playing" and _HISTORY_WORD_RE.search(q_unquoted):
        name = "watch_history"
    elif name == "now_playing" and re.search(r"\b(trending|popular|upcoming)\b", q_unquoted, re.IGNORECASE) and not re.search(r"\b(stream|streaming|playing|watching|plex|tautulli)\b", q_unquoted, re.IGNORECASE):
        name = "seer_discover"
        cat_m = re.search(r"\b(trending|popular|upcoming)\b", q_unquoted, re.IGNORECASE)
        args = {
            "category": cat_m.group(1).lower() if cat_m else "trending",
            "media_type": "tv" if re.search(r"\b(tv|show|shows|series)\b", q_unquoted, re.IGNORECASE) else "movie",
        }
    elif name in {"tv_status", "tv_missing", "tv_search_missing"} and re.search(r"\bradarr\b", q_unquoted, re.IGNORECASE) and not re.search(r"\bsonarr\b", q_unquoted, re.IGNORECASE):
        name = "movie_search" if name == "tv_search_missing" else "movie_status"
        args.pop("season", None)
        args.pop("include_specials", None)
    elif name == "bitmagnet_search" and re.search(r"\bradarr\b", q_unquoted, re.IGNORECASE) and not re.search(r"\bbitmagnet\b", q_unquoted, re.IGNORECASE):
        q_val = args.pop("query", None)
        name = "movie_search"
        if q_val:
            args["title"] = q_val

    # 1. Strip trailing "(YYYY)" from title if present
    if "title" in args and isinstance(args["title"], str):
        m_paren = _TRAILING_PAREN_YEAR_RE.match(args["title"].strip())
        if m_paren:
            clean_title = m_paren.group(1).strip()
            extracted_year = int(m_paren.group(2))
            args["title"] = clean_title
            if name in {"movie_status", "movie_search", "request_media"} and "year" not in args:
                args["year"] = extracted_year

    # 2. Check optional entity slots first (strip if ungrounded/placeholder)
    ungrounded_album_dropped = False
    if name in _OPTIONAL_ENTITY_SLOTS:
        opt_slot = _OPTIONAL_ENTITY_SLOTS[name]
        if opt_slot in args and isinstance(args[opt_slot], str):
            val = args[opt_slot].strip()
            val_norm = _norm_alnum(val)
            in_query = _is_grounded_in_text(val, query)
            in_selected = bool(
                selected
                and isinstance(selected.get("title"), str)
                and _is_grounded_in_text(val, selected["title"])
            )
            if (
                val_norm in _DEICTIC_PLACEHOLDERS
                or (val_norm in _GENERIC_DOMAIN_PLACEHOLDERS and not in_selected)
                or (not in_query and not in_selected)
            ):
                snapped = None if val_norm in _DEICTIC_PLACEHOLDERS else _snap_entity_to_query(val, query)
                if (
                    snapped
                    and _norm_alnum(snapped) not in _GENERIC_DOMAIN_PLACEHOLDERS
                    and _norm_alnum(snapped) not in _DEICTIC_PLACEHOLDERS
                ):
                    args[opt_slot] = snapped
                else:
                    del args[opt_slot]
                    if opt_slot == "album":
                        ungrounded_album_dropped = True

    # 3. Check mandatory entity slots
    if name in _MANDATORY_ENTITY_SLOTS:
        slot = _MANDATORY_ENTITY_SLOTS[name]
        val = str(args.get(slot, "")).strip()
        val_norm = _norm_alnum(val)
        in_query = _is_grounded_in_text(val, query)
        in_selected = bool(
            selected
            and isinstance(selected.get("title"), str)
            and _norm_alnum(val) == _norm_alnum(selected["title"])
        )
        if name in {"torrent_pause", "torrent_resume"} and not in_selected:
            words = set(val_norm.split())
            descriptive = bool(words & _TRANSFER_DESCRIPTORS) and words <= _TRANSFER_REFERENCE_WORDS
            quoted_literal = any(_norm_alnum(match[1]) == val_norm
                                 for match in re.finditer(r'''["']([^"'\n]+)["']''', query))
            if descriptive and not quoted_literal:
                return validate_call("clarify", {"slot": "title"})

        # Snap minor BPE/spelling slips or missing "The " / "PREFIX: " back to query surface text
        if not in_selected:
            snapped = _snap_entity_to_query(val, query)
            if snapped and _norm_alnum(snapped) not in _GENERIC_DOMAIN_PLACEHOLDERS and _norm_alnum(snapped) not in _DEICTIC_PLACEHOLDERS:
                val = snapped
                args[slot] = val
                val_norm = _norm_alnum(val)
                in_query = True

        # Context override guard: if query says "instead" and the model copied selected_media
        if in_selected and not in_query and re.search(r"\binstead\b", query, re.IGNORECASE):
            in_selected = False
            m_inst = re.search(
                r"^(?:check|look\s+up|search\s+for|request|show|find)\s+(?:the\s+(?:movie|series|show|film)\s+)?([A-Z][A-Za-z0-9:'-]*(?:\s+[A-Z0-9][A-Za-z0-9:'-]*)*?)(?:\s+in\s+(?:radarr|sonarr|lidarr))?\s+instead\b",
                query.strip(),
                re.IGNORECASE,
            )
            if m_inst:
                val = m_inst.group(1).strip()
                args[slot] = val
                val_norm = _norm_alnum(val)
                in_query = True
                if name == "tv_status" and (
                    re.search(r"\bradarr\b", q_unquoted, re.IGNORECASE)
                    or (selected and selected.get("media_type") == "movie" and not re.search(r"\bsonarr\b", q_unquoted, re.IGNORECASE))
                ):
                    name = "movie_status"

        if val_norm in _DEICTIC_PLACEHOLDERS:
            if selected and isinstance(selected.get("title"), str) and slot == "title":
                if re.search(r"\b(other\s+version|second\s+one|first\s+one|latest\s+one)\b", query, re.IGNORECASE):
                    return validate_call("clarify", {"slot": "reference"})
                args["title"] = selected["title"]
                in_selected = True
                in_query = False
            else:
                return validate_call("clarify", {"slot": "reference"})
        elif (val_norm in _GENERIC_DOMAIN_PLACEHOLDERS and not in_selected) or (not in_query and not in_selected):
            name = "clarify"
            args = {"slot": _infer_clarify_slot(query, selected)}

        # 4. Year grounding and parenthesized year extraction on movie/request tools
        if name in {"movie_status", "movie_search", "request_media"}:
            t_esc = re.escape(str(args.get("title", "")))
            m_q_year = re.search(rf"{t_esc}\s*\((18[7-9]\d|19\d{{2}}|20\d{{2}}|21\d{{2}})\)", query, re.IGNORECASE)
            if m_q_year and "year" not in args:
                args["year"] = int(m_q_year.group(1))

        if "year" in args:
            yr = args["year"]
            yr_in_query = bool(re.search(rf"\b{yr}\b", query))
            yr_in_selected = bool(in_selected and selected and selected.get("year") == yr)
            if not yr_in_query and not yr_in_selected:
                del args["year"]
        elif (
            in_selected
            and not in_query
            and selected
            and isinstance(selected.get("year"), int)
            and name in {"movie_status", "movie_search", "request_media"}
        ):
            args["year"] = selected["year"]

    # 5. Affirmative, non-negated write-intent verification for ALL 6 write actions
    is_non_imperative_question = bool(_INTERROGATIVE_OR_HYPOTHETICAL_WRITE_RE.search(q_unquoted))
    has_search_trigger = (
        bool(_SEARCH_DOWNLOAD_TRIGGER_RE.search(q_unquoted))
        and not bool(_NEGATED_WRITE_RE.search(q_unquoted))
        and not is_non_imperative_question
    )
    has_request_word = bool(_REQUEST_WORD_RE.search(q_unquoted))
    has_track_word = bool(_REQUEST_TRACK_RE.search(q_unquoted))
    has_negated_request = bool(_NEGATED_REQUEST_RE.search(q_unquoted))
    has_affirmative_request = (
        bool(_AFFIRMATIVE_REQUEST_RE.search(q_unquoted))
        and not has_negated_request
        and not has_track_word
        and not is_non_imperative_question
    )

    if name in {"movie_search", "movie_status"} and has_request_word and not re.search(r"\bradarr\b", q_unquoted, re.IGNORECASE):
        if has_track_word or has_negated_request:
            if has_track_word or re.search(r"\b(check|track|status|progress|existing|look\s+up|where)\b", q_unquoted, re.IGNORECASE):
                name = "request_status"
                args = {"title": args["title"]}
            else:
                return validate_call("clarify", {"slot": "scope"})
        elif has_affirmative_request:
            name = "request_media"
            if "media_type" not in args:
                if selected and _norm_alnum(str(args.get("title", ""))) == _norm_alnum(selected.get("title", "")) and selected.get("media_type") in {"movie", "tv"}:
                    args["media_type"] = selected["media_type"]
                elif _TV_WORD_RE.search(q_unquoted) and not _MOVIE_WORD_RE.search(q_unquoted):
                    args["media_type"] = "tv"
                else:
                    args["media_type"] = "movie"

    if name == "request_media":
        if has_negated_request or has_track_word:
            if has_track_word or re.search(r"\b(check|track|status|progress|existing|look\s+up|where)\b", q_unquoted, re.IGNORECASE):
                name = "request_status"
                args = {"title": args["title"]}
            else:
                return validate_call("clarify", {"slot": "scope"})
        elif not has_affirmative_request:
            if _MOVIE_WORD_RE.search(q_unquoted) or args.get("media_type") == "movie":
                name = "movie_status"
                args = {k: v for k, v in args.items() if k in {"title", "year"}}
            elif _TV_WORD_RE.search(q_unquoted) or args.get("media_type") == "tv":
                name = "tv_missing" if re.search(r"\b(missing|absent|gaps|holes)\b", q_unquoted, re.IGNORECASE) else "tv_status"
                args = {"title": args["title"]}
            else:
                return validate_call("clarify", {"slot": "scope"})
    elif name == "movie_search" and not has_search_trigger:
        name = "movie_status"
    elif name == "music_search" and not has_search_trigger:
        name = "music_status"
    elif name == "music_search" and ungrounded_album_dropped:
        return validate_call("clarify", {"slot": _infer_clarify_slot(query, selected)})
    elif name == "music_search" and "album" not in args:
        scope_text = _strip_quoted_and_releases(query)
        artist = str(args.get("artist", ""))
        if artist:
            scope_text = re.sub(r"(?<!\w)" + re.escape(artist) + r"(?!\w)", " ENTITY ", scope_text, flags=re.I)
        if not re.search(r"\b(?:all|every|entire\s+(?:artist|catalog(?:ue)?|discography)|discography|missing\s+(?:albums?|music)|artist[- ]wide)\b", scope_text, re.I):
            return validate_call("clarify", {"slot": "scope"})
    elif name == "tv_search_missing" and not has_search_trigger:
        name = "tv_missing" if re.search(r"\b(missing|absent|gaps|holes)\b", q_unquoted, re.IGNORECASE) else "tv_status"
        args.pop("season", None)
    elif name == "torrent_pause":
        is_neg_pause = bool(_NEGATED_PAUSE_RE.search(q_unquoted))
        is_passive_pause = bool(_PASSIVE_PAUSE_QUERY_RE.search(q_unquoted))
        has_aff_pause = (
            bool(_AFFIRMATIVE_PAUSE_RE.search(q_unquoted))
            and not is_neg_pause
            and not is_passive_pause
            and not is_non_imperative_question
        )
        if not has_aff_pause:
            if _TORRENT_READ_VERB_RE.search(q_unquoted):
                t_val = str(args.get("title", "")).strip()
                name = "torrent_list"
                if t_val and _norm_alnum(t_val) not in _GENERIC_DOMAIN_PLACEHOLDERS and _is_grounded_in_text(t_val, query):
                    args = {"state": "all", "query": t_val}
                elif re.search(r"\bpaused\b", q_unquoted, re.IGNORECASE):
                    args = {"state": "paused"}
                else:
                    args = {"state": "all"}
            else:
                return validate_call("clarify", {"slot": "scope"})
    elif name == "torrent_resume":
        is_neg_resume = bool(_NEGATED_RESUME_RE.search(q_unquoted))
        is_passive_resume = bool(_PASSIVE_RESUME_QUERY_RE.search(q_unquoted))
        has_aff_resume = (
            bool(_AFFIRMATIVE_RESUME_RE.search(q_unquoted))
            and not is_neg_resume
            and not is_passive_resume
            and not is_non_imperative_question
        )
        if not has_aff_resume:
            if _TORRENT_READ_VERB_RE.search(q_unquoted):
                t_val = str(args.get("title", "")).strip()
                name = "torrent_list"
                if t_val and _norm_alnum(t_val) not in _GENERIC_DOMAIN_PLACEHOLDERS and _is_grounded_in_text(t_val, query):
                    args = {"state": "all", "query": t_val}
                else:
                    args = {"state": "all"}
            else:
                return validate_call("clarify", {"slot": "scope"})

    # 6. Season grounding on tv_search_missing. A proposed season must agree with the
    # utterance. Never broaden a season-scoped write by silently dropping the season
    # when the user did restrict it: recover the single explicit season or clarify.
    if name == "tv_search_missing" and "season" in args:
        s_num = args["season"]
        explicit = _explicit_seasons(q_unquoted)
        if s_num in explicit and len(explicit) == 1:
            pass
        elif len(explicit) == 1:
            args["season"] = next(iter(explicit))
        elif len(explicit) > 1:
            return validate_call("clarify", {"slot": "scope"})
        elif _ALL_SEASONS_RE.search(q_unquoted) or not _SEASON_MENTION_RE.search(q_unquoted):
            # No season restriction in the utterance: the season was hallucinated.
            del args["season"]
        else:
            return validate_call("clarify", {"slot": "scope"})
    elif name == "tv_search_missing":
        # The model omitted a season the user asked for: do not widen to the whole series.
        explicit = _explicit_seasons(q_unquoted)
        if len(explicit) == 1:
            args["season"] = next(iter(explicit))
        elif len(explicit) > 1:
            return validate_call("clarify", {"slot": "scope"})
        elif _SEASON_MENTION_RE.search(q_unquoted) and not _ALL_SEASONS_RE.search(q_unquoted):
            # Season language that cannot be resolved ("latest season"): fail closed.
            return validate_call("clarify", {"slot": "scope"})

    # 7. include_specials grounding on tv_missing
    if name == "tv_missing" and "include_specials" in args:
        if _NEGATED_SPECIALS_RE.search(query):
            args["include_specials"] = False
        elif args["include_specials"] is True and not re.search(
            r"(special|season\s*(?:0|zero)\b|\bs00\b|\bovas?\b)", query, re.IGNORECASE
        ):
            args["include_specials"] = False

    # 8. request_media media_type grounding
    if name == "request_media":
        title_str = str(args.get("title", ""))
        used_selected = bool(
            selected
            and isinstance(selected.get("title"), str)
            and _norm_alnum(title_str) == _norm_alnum(selected["title"])
            and not _is_grounded_in_text(title_str, query)
        )
        if used_selected and selected and selected.get("media_type") in {"movie", "tv"}:
            args["media_type"] = selected["media_type"]
        else:
            q_outside = re.sub(re.escape(title_str), " ", query, flags=re.IGNORECASE) if title_str else query
            has_movie = bool(_MOVIE_WORD_RE.search(q_outside))
            has_tv = bool(_TV_WORD_RE.search(q_outside))
            # An unambiguous media-type instruction outside the title is authoritative.
            if has_movie and not has_tv:
                args["media_type"] = "movie"
            elif has_tv and not has_movie:
                args["media_type"] = "tv"
            elif not has_movie and not has_tv:
                if not (selected and _norm_alnum(title_str) == _norm_alnum(selected.get("title", ""))):
                    return validate_call("clarify", {"slot": "media_type"})

    # 9. clarify slot structural consistency (never rewrites clarify into a concrete tool call)
    if name == "clarify":
        args["slot"] = _infer_clarify_slot(query, selected)

    # 10. Contract version guard (v1 only allows 15 actions)
    if contract_version in {"v1", CONTRACT_VERSION} and name not in BY_NAME:
        return validate_call("unsupported", {"reason": "out_of_scope"})

    return validate_call(name, args)


def resolve_title(title: str, candidates: Iterable[dict], year: int | None = None) -> dict:
    """Resolve IDs from service-returned candidates, never from model output.

    Exact normalized titles only. No first-hit fuzzy match, stale selection or
    preference for the highest result. Remakes require a year or a user choice.
    """
    def key(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    matches = [x for x in candidates if isinstance(x, dict) and isinstance(x.get("title"), str)
               and key(x["title"]) == key(title) and (year is None or x.get("year") == year)]
    if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0:
        raise ResolutionRequired("A unique service identity is required")
    return dict(matches[0])


def prepare(call: Call, principal: Principal) -> dict[str, Any]:
    call = validate_call(call.name, call.arguments)
    spec = V3_BY_NAME[call.name]
    base = {"contract": CONTRACT_VERSION, "contract_sha256": contract_hash(),
            "dry_run": True, "executed": False}
    permission = spec.permission
    service = spec.service
    if call.name in {"bazarr_subtitle_search", "bazarr_subtitle_download"}:
        base.update(contract=CONTRACT_VERSION_V3, contract_sha256=contract_hash_v3())
    if call.name == "service_query":
        from .services import SERVICE_PERMISSIONS
        service = call.arguments["service"]
        permission = SERVICE_PERMISSIONS[service]
        base.update(contract=CONTRACT_VERSION_V3, contract_sha256=contract_hash_v3())
    if not principal.subject or (permission and permission not in principal.scopes):
        return {**base, "status": "denied", "reason": "permission_required"}
    if spec.service == "control":
        return {**base, "status": call.name, **call.arguments}
    return {**base, "status": "planned", "service": service,
            "operation": call.name, "arguments": normalized(call)["arguments"],
            "effect": spec.effect, "principal": principal.subject,
            "requires_entity_resolution": any(k in call.arguments for k in ("title", "artist")),
            "requires_write_authorization": spec.effect != "read",
            "adapter_state": "not_connected"}
