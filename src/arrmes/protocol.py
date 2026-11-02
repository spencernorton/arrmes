"""Strict, non-executing FunctionGemma and Hermes wire decoders."""

from dataclasses import dataclass
import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from .catalog import BY_NAME, V2_BY_NAME


class InvalidCall(ValueError):
    pass


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict[str, Any]


def _unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise InvalidCall("Duplicate argument")
        result[k] = v
    return result


def validate_call(name: str, arguments: dict, *, allow_v2: bool = True) -> Call:
    registry = V2_BY_NAME if allow_v2 else BY_NAME
    if not isinstance(name, str) or name not in registry:
        raise InvalidCall("Unknown action")
    if not isinstance(arguments, dict):
        raise InvalidCall("Arguments must be an object")
    action_spec = registry[name]
    errors = list(Draft202012Validator(action_spec.parameters).iter_errors(arguments))
    if errors:
        # Do not repeat potentially sensitive/injected argument values in errors.
        raise InvalidCall("Arguments do not match the action schema")
    # jsonschema permits a mathematically integral float for integer. The wire
    # contract deliberately accepts JSON integer tokens only, and never bool IDs.
    for k, v in arguments.items():
        spec = action_spec.properties.get(k, {})
        if spec.get("type") == "integer" and type(v) is not int:
            raise InvalidCall("Expected an integer token")
    return Call(name, dict(arguments))


class _GemmaArguments:
    """Parse the limited native syntax needed by our scalar action schemas.

    Strings use <escape> delimiters, not JSON quotes. Never use eval/exec or
    approximate a call by searching for a JSON substring inside arbitrary text.
    """
    def __init__(self, source: str):
        self.source = source
        self.pos = 0

    def ws(self):
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def take(self, literal: str):
        self.ws()
        if not self.source.startswith(literal, self.pos):
            raise InvalidCall("Malformed native tool call")
        self.pos += len(literal)

    def value(self):
        self.ws()
        if self.source.startswith("<escape>", self.pos):
            self.pos += len("<escape>")
            end = self.source.find("<escape>", self.pos)
            if end < 0:
                raise InvalidCall("Unterminated string")
            value = self.source[self.pos:end]
            self.pos = end + len("<escape>")
            return value
        for token, value in [("true", True), ("false", False)]:
            if self.source.startswith(token, self.pos):
                self.pos += len(token)
                return value
        match = re.match(r"-?(?:0|[1-9][0-9]*)", self.source[self.pos:])
        if match:
            # All current integer slots are bounded years or season numbers.
            # Reject oversized wire tokens before Python's int-string limit
            # can raise a ValueError outside the InvalidCall boundary.
            if len(match[0]) > 12:
                raise InvalidCall("Native integer token is too long")
            self.pos += len(match[0])
            return int(match[0])
        raise InvalidCall("Unsupported native value")

    def parse(self):
        self.take("{")
        self.ws()
        pairs = []
        if not self.source.startswith("}", self.pos):
            while True:
                self.ws()
                match = re.match(r"[A-Za-z_][A-Za-z_0-9]*", self.source[self.pos:])
                if not match:
                    raise InvalidCall("Invalid argument name")
                self.pos += len(match[0])
                self.take(":")
                pairs.append((match[0], self.value()))
                self.ws()
                if not self.source.startswith(",", self.pos):
                    break
                self.pos += 1
        self.take("}")
        self.ws()
        if self.pos != len(self.source):
            raise InvalidCall("Trailing native data")
        return _unique(pairs)


def decode(raw: str, family: str = "functiongemma") -> Call:
    if not isinstance(raw, str) or len(raw) > 16384:
        raise InvalidCall("Invalid output size")
    if family == "functiongemma":
        match = re.fullmatch(r"\s*<start_function_call>call:([A-Za-z_][A-Za-z_0-9]*)(\{.*\})<end_function_call>\s*", raw, re.S)
        if not match:
            raise InvalidCall("Expected exactly one complete native tool call")
        return validate_call(match[1], _GemmaArguments(match[2]).parse())
    if family == "hermes":
        match = re.fullmatch(r"\s*(?:<think>\s*</think>\s*)?<tool_call>\s*(\{.*\})\s*</tool_call>\s*", raw, re.S)
        if not match:
            raise InvalidCall("Expected exactly one complete Hermes tool call")
        try:
            value = json.loads(match[1], object_pairs_hook=_unique)
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidCall("Malformed tool JSON") from exc
        if not isinstance(value, dict) or set(value) != {"name", "arguments"} or not isinstance(value["name"], str):
            raise InvalidCall("Invalid call envelope")
        return validate_call(value["name"], value["arguments"])
    raise InvalidCall("Unknown protocol family")


def encode(call: Call, family: str = "functiongemma") -> str:
    call = validate_call(call.name, call.arguments)
    if family == "hermes":
        return "<tool_call>" + json.dumps({"name": call.name, "arguments": call.arguments}, ensure_ascii=False) + "</tool_call>"
    if family != "functiongemma":
        raise InvalidCall("Unknown protocol family")
    def scalar(value):
        if isinstance(value, str):
            if "<escape>" in value:
                raise InvalidCall("Reserved delimiter in string")
            return "<escape>" + value + "<escape>"
        return json.dumps(value)
    body = ",".join(k + ":" + scalar(v) for k, v in call.arguments.items())
    return "<start_function_call>call:" + call.name + "{" + body + "}<end_function_call>"


def normalized(call: Call) -> dict:
    args = dict(V2_BY_NAME[call.name].defaults)
    args.update(call.arguments)
    return {"name": call.name, "arguments": args}
