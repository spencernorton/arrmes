"""Conservative explicit Bazarr manual-search/download grammar, not model training."""
from __future__ import annotations

import re

from arrmes.protocol import Call, InvalidCall
from .bazarr_subtitles import validate_bazarr_subtitle_arguments

_QUOTED = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
_LANGUAGES = {"english": "en", "french": "fr", "spanish": "es", "german": "de", "italian": "it",
              "japanese": "ja", "korean": "ko", "portuguese": "pt", "chinese": "zh", "arabic": "ar",
              "hindi": "hi", "russian": "ru", "dutch": "nl", "polish": "pl", "swedish": "sv"}
_TARGET = r'(?:(?:the\s+)?movie\s+"(?P<movie>[^"\n]+)"(?:\s*\((?P<movie_year>\d{4})\))?|(?:the\s+)?(?:episode\s+)?S(?P<season>\d{1,4})E(?P<episode>\d{1,5})\s+of\s+"(?P<series>[^"\n]+)"(?:\s*\((?P<series_year>\d{4})\))?)'
_NATURAL = re.compile(r'^(?:(?:please|kindly)\s+)?(?:(?:in|within|via|using)\s+Bazarr,\s+|use\s+Bazarr\s+to\s+)?(?P<verb>find|search(?:\s+providers)?\s+for|download)\s+(?P<language>[A-Za-z]+)\s+'
    r'(?P<forced>forced|non-forced)\s+(?P<hi>HI|non-HI)\s+subtitles?'
    r'(?:\s+candidate\s+(?P<candidate>[a-f0-9]{24}))?\s+for\s+' + _TARGET + r'(?:\s+(?:in|via|within|using)\s+Bazarr)?[.!?]?$', re.I)
_MACHINE = re.compile(r'^(?:please\s+)?(?P<verb>search|download)\s+Bazarr\s+subtitles?'
    r'(?:\s+candidate\s+(?P<candidate>[a-f0-9]{24}))?\s+for\s+' + _TARGET
    + r'\s+language\s+(?P<language>[a-z]{2})\s+forced\s+(?P<forced>true|false)\s+HI\s+(?P<hi>true|false)[.!?]?$', re.I)


def route_bazarr_subtitle_query(query: str) -> Call | None:
    """Return one exact explicit action, safe abstention, or no Bazarr intent.

    Required language and both subtitle flags are never inferred from omissions.
    Missing candidate references clarify; arbitrary provider/cache IDs are not
    accepted. Compound writes and unsupported subtitle mutations never fall
    through to the released model's media-download actions.
    """
    if not isinstance(query, str) or not query.strip():
        return None
    plain = _QUOTED.sub(" ENTITY ", query)
    if not re.search(r"\bbazarr\b", plain, re.I):
        return None
    if len(query) > 2000:
        return Call("clarify", {"slot": "scope"})
    if not re.search(r"\b(find|search|download|grab|fetch|install|delete|remove|upload|replace|sync|manage|upgrade|update|change|edit|translate|repair|fix|refresh)\b", plain, re.I):
        return None
    if re.search(r"\b(ignore|bypass|override|credentials?|passwords?|tokens?|api\s*keys?|instructions?|system\s*prompt|shell|sudo)\b", plain, re.I):
        return Call("unsupported", {"reason": "unsafe"})
    if re.search(r"\b(and|then|after|before|unless|except)\b|[;\n]", plain, re.I):
        return Call("unsupported", {"reason": "complex"})
    if re.search(r"\b(delete|remove|upload|replace|sync|upgrade|update|change|edit|translate|repair|fix|refresh|never)\b|\bdon['’]t\b|\bdo\s+not\b", plain, re.I):
        return Call("unsupported", {"reason": "out_of_scope"})
    match = _NATURAL.fullmatch(query.strip())
    natural = match is not None
    match = match or _MACHINE.fullmatch(query.strip())
    if match is None:
        return Call("clarify", {"slot": "scope"})
    fields = match.groupdict()
    language = fields["language"].lower()
    language = _LANGUAGES.get(language, language)
    if re.fullmatch(r"[a-z]{2}", language) is None:
        # Keep the released control schema unchanged; language uncertainty is
        # represented by its existing scope slot.
        return Call("clarify", {"slot": "scope"})
    download = fields["verb"].lower() == "download"
    if download != bool(fields["candidate"]):
        return Call("clarify", {"slot": "reference"})
    args = {"media_type": "movie" if fields["movie"] else "episode", "title": fields["movie"] or fields["series"],
            "language": language, "forced": fields["forced"].lower() == ("forced" if natural else "true"),
            "hearing_impaired": fields["hi"].lower() == ("hi" if natural else "true")}
    if fields["movie_year"] or fields["series_year"]:
        args["year"] = int(fields["movie_year"] or fields["series_year"])
    if fields["series"]:
        args.update(season=int(fields["season"]), episode=int(fields["episode"]))
    if download:
        args["candidate_id"] = fields["candidate"].lower()
    name = "bazarr_subtitle_download" if download else "bazarr_subtitle_search"
    try:
        return Call(name, validate_bazarr_subtitle_arguments(name, args))
    except InvalidCall:
        return Call("clarify", {"slot": "scope"})
