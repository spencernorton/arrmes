"""Explicit native request phrases; unquoted requests can use the v2 model."""
from __future__ import annotations
import re

from arrmes.protocol import Call, InvalidCall, validate_call

_QUOTED = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
_SERVICE = re.compile(r"\b(?:seerr|seer)\b", re.I)
_MOVIE = re.compile(r'^(?:please\s+)?request\s+(?:the\s+)?movie\s+"(?P<title>[^"\n]+)"'
                    r'(?:\s*\((?P<year>\d{4})\))?\s+(?:in|through)\s+Seerr?[.!?]?$', re.I)
_STATUS = re.compile(r'^(?:please\s+)?(?:show|get)\s+(?:my\s+)?request\s+(?:status|progress)\s+'
                     r'(?:for|of)\s+"(?P<title>[^"\n]+)"\s+(?:in|through)\s+Seerr?[.!?]?$', re.I)


def has_native_request_intent(query: str) -> bool:
    plain = _QUOTED.sub(" ENTITY ", query) if isinstance(query, str) else ""
    return bool(_SERVICE.search(plain) and re.search(r"\brequests?\b", plain, re.I))


def route_native_request_query(query: str) -> Call | None:
    if not has_native_request_intent(query):
        return None
    plain = _QUOTED.sub(" ENTITY ", query)
    if len(query) > 2000:
        return Call("clarify", {"slot": "scope"})
    if re.search(r"\b(ignore|bypass|override|credentials?|passwords?|tokens?|sudo|shell)\b", plain, re.I):
        return Call("unsupported", {"reason": "unsafe"})
    if re.search(r"\b(and|then|after|before|unless)\b|[;\n]", plain, re.I):
        return Call("unsupported", {"reason": "complex"})
    if re.search(r"\b(approve|decline|delete|remove|cancel|restart|upgrade|update|download|ignorequota)\b", plain, re.I):
        return Call("unsupported", {"reason": "out_of_scope"})
    if re.search(r"\b(4k|uhd|seasons?|episodes?|profile|quota|on\s+behalf|as\s+(?!movie\b|tv\b)\w+)\b", plain, re.I):
        return Call("clarify", {"slot": "scope"})
    movie, status = _MOVIE.fullmatch(query.strip()), _STATUS.fullmatch(query.strip())
    try:
        if movie:
            args = {"title": movie["title"], "media_type": "movie"}
            if movie["year"]:
                args["year"] = int(movie["year"])
            return validate_call("request_media", args, allow_v3=False)
        if status:
            return validate_call("request_status", {"title": status["title"]}, allow_v3=False)
    except InvalidCall:
        return Call("clarify", {"slot": "scope"})
    # A quoted, service-qualified request outside this grammar is not handed
    # to a different media operation. Plain v2 requests keep their neural route.
    return Call("clarify", {"slot": "scope"}) if _QUOTED.search(query) else None
