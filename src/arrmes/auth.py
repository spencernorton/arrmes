"""Caller authentication for the Arrmes HTTP service.

The HTTP boundary never accepts identity or permissions from a request body.
Each caller presents ``Authorization: Bearer <token>``; the service hashes the
token and looks it up in an operator-managed client registry that stores only
SHA-256 digests. The registry entry alone determines the caller's subject and
scopes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Any, Iterable

from arrmes.catalog import V2_ACTIONS
from arrmes.planner import Principal

KNOWN_SCOPES = frozenset(a.permission for a in V2_ACTIONS if a.permission)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@-]{0,127}$")


class AuthenticationError(Exception):
    """The request did not carry a valid caller credential."""


@dataclass(frozen=True)
class ClientIdentity:
    subject: str
    scopes: frozenset[str]
    token_sha256: str


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BearerTokenAuthenticator:
    """Resolve a bearer credential to a trusted ``Principal``."""

    def __init__(self, clients: Iterable[ClientIdentity]) -> None:
        self._clients = tuple(clients)
        if not self._clients:
            raise ValueError("At least one client identity is required")
        seen_digests: set[str] = set()
        seen_subjects: set[str] = set()
        for client in self._clients:
            if not _SHA256_HEX_RE.match(client.token_sha256):
                raise ValueError(f"Client {client.subject!r}: token_sha256 must be 64 lowercase hex characters")
            if not _SUBJECT_RE.match(client.subject):
                raise ValueError(f"Client subject {client.subject!r} is not a valid identifier")
            unknown = set(client.scopes) - KNOWN_SCOPES
            if unknown:
                raise ValueError(f"Client {client.subject!r} requests unknown scopes: {sorted(unknown)}")
            if client.token_sha256 in seen_digests:
                raise ValueError("Two clients share the same token digest")
            if client.subject in seen_subjects:
                raise ValueError(f"Duplicate client subject {client.subject!r}")
            seen_digests.add(client.token_sha256)
            seen_subjects.add(client.subject)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BearerTokenAuthenticator":
        raw_clients = data.get("clients") if isinstance(data, dict) else None
        if not isinstance(raw_clients, list):
            raise ValueError("Client registry must be an object with a 'clients' list")
        clients = []
        for entry in raw_clients:
            if not isinstance(entry, dict):
                raise ValueError("Each client entry must be an object")
            scopes = entry.get("scopes")
            if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                raise ValueError("Each client needs a 'scopes' list of strings")
            clients.append(
                ClientIdentity(
                    subject=str(entry.get("subject", "")).strip(),
                    scopes=frozenset(scopes),
                    token_sha256=str(entry.get("token_sha256", "")).strip().lower(),
                )
            )
        return cls(clients)

    @classmethod
    def from_file(cls, path: str | Path) -> "BearerTokenAuthenticator":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def authenticate(self, authorization_header: str | None) -> Principal:
        if not authorization_header:
            raise AuthenticationError("missing_credentials")
        scheme, _, raw_cred = authorization_header.strip().partition(" ")
        raw_cred = raw_cred.strip()
        if scheme.lower() != "bearer" or not raw_cred:
            raise AuthenticationError("unsupported_authorization_scheme")
        presented = token_digest(raw_cred)
        match: ClientIdentity | None = None
        for client in self._clients:
            # Compare against every entry so timing does not reveal the match position.
            if hmac.compare_digest(presented, client.token_sha256):
                match = client
        if match is None:
            raise AuthenticationError("invalid_credentials")
        return Principal(subject=match.subject, scopes=match.scopes)
