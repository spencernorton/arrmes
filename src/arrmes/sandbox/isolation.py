"""Network and credential isolation enforcement for the Arrmes offline sandbox.

Enforces Python-level socket and urllib network isolation for the in-process
sandbox cluster and evaluation harness, denying all non-loopback connections,
unregistered loopback ports, external DNS resolution, and UDP datagrams
(including both sendto and sendmsg).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import socket
from typing import Any
import urllib.error
import urllib.request


class SandboxIsolationViolation(PermissionError):
    """Raised when sandboxed code attempts any non-loopback network connection."""


@dataclass
class BlockedAttempt:
    timestamp: str
    operation: str
    target_host: str
    target_port: int | None
    reason: str


def is_loopback_host(host: str) -> bool:
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True
    try:
        addr = ipaddress.ip_address(normalized.strip("[]"))
        return addr.is_loopback
    except ValueError:
        return False


@dataclass
class NetworkIsolationGuard:
    """Context manager that intercepts Python socket and urllib calls to forbid non-loopback traffic."""

    allowed_ports: set[int] | None = None
    blocked_attempts: list[BlockedAttempt] = field(default_factory=list)
    allowed_attempts: int = 0
    _orig_create_connection: Any = None
    _orig_socket_connect: Any = None
    _orig_socket_connect_ex: Any = None
    _orig_socket_sendto: Any = None
    _orig_socket_sendmsg: Any = None
    _orig_getaddrinfo: Any = None

    def _record_block(self, op: str, host: str, port: int | None, reason: str) -> None:
        self.blocked_attempts.append(
            BlockedAttempt(
                timestamp=datetime.now(timezone.utc).isoformat(),
                operation=op,
                target_host=str(host),
                target_port=port,
                reason=reason,
            )
        )
        raise SandboxIsolationViolation(f"Sandbox network isolation blocked {op} to {host}:{port} ({reason})")

    def _check_endpoint(self, op: str, address: Any) -> None:
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = str(address[0]), int(address[1])
        else:
            self._record_block(op, str(address), None, "non_inet_socket_disallowed")
            return
        if not is_loopback_host(host):
            self._record_block(op, host, port, "non_loopback_destination_denied")
        if self.allowed_ports is not None and port not in self.allowed_ports and port != 0:
            self._record_block(op, host, port, "loopback_port_not_in_sandbox_allowlist")
        self.allowed_attempts += 1

    def __enter__(self) -> "NetworkIsolationGuard":
        guard = self
        self._orig_create_connection = socket.create_connection
        self._orig_socket_connect = socket.socket.connect
        self._orig_socket_connect_ex = socket.socket.connect_ex
        self._orig_socket_sendto = socket.socket.sendto
        self._orig_socket_sendmsg = getattr(socket.socket, "sendmsg", None)
        self._orig_getaddrinfo = socket.getaddrinfo

        def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
            guard._check_endpoint("socket.create_connection", address)
            return guard._orig_create_connection(address, *args, **kwargs)

        def guarded_connect(sock_self: socket.socket, address: Any) -> Any:
            guard._check_endpoint("socket.connect", address)
            return guard._orig_socket_connect(sock_self, address)

        def guarded_connect_ex(sock_self: socket.socket, address: Any) -> Any:
            guard._check_endpoint("socket.connect_ex", address)
            return guard._orig_socket_connect_ex(sock_self, address)

        def guarded_sendto(sock_self: socket.socket, data: bytes, *args: Any) -> Any:
            address = args[-1] if args else None
            guard._check_endpoint("socket.sendto", address)
            return guard._orig_socket_sendto(sock_self, data, *args)

        def guarded_sendmsg(sock_self: socket.socket, buffers: Any, ancdata: Any = (), flags: int = 0, address: Any = None) -> Any:
            target_addr = address
            if target_addr is None:
                try:
                    target_addr = sock_self.getpeername()
                except OSError:
                    target_addr = None
            guard._check_endpoint("socket.sendmsg", target_addr)
            return guard._orig_socket_sendmsg(sock_self, buffers, ancdata, flags, address)

        def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            if not is_loopback_host(str(host)):
                guard._record_block("socket.getaddrinfo", str(host), int(port) if isinstance(port, int) else None, "external_dns_resolution_denied")
            return guard._orig_getaddrinfo(host, port, *args, **kwargs)

        socket.create_connection = guarded_create_connection
        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
        if self._orig_socket_sendmsg is not None:
            socket.socket.sendmsg = guarded_sendmsg  # type: ignore[method-assign]
        socket.getaddrinfo = guarded_getaddrinfo
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._orig_create_connection is not None:
            socket.create_connection = self._orig_create_connection
        if self._orig_socket_connect is not None:
            socket.socket.connect = self._orig_socket_connect  # type: ignore[method-assign]
        if self._orig_socket_connect_ex is not None:
            socket.socket.connect_ex = self._orig_socket_connect_ex  # type: ignore[method-assign]
        if self._orig_socket_sendto is not None:
            socket.socket.sendto = self._orig_socket_sendto  # type: ignore[method-assign]
        if self._orig_socket_sendmsg is not None:
            socket.socket.sendmsg = self._orig_socket_sendmsg  # type: ignore[method-assign]
        if self._orig_getaddrinfo is not None:
            socket.getaddrinfo = self._orig_getaddrinfo

    def evidence_summary(self) -> dict[str, Any]:
        payload = {
            "policy": "deny_all_non_loopback_and_unregistered_ports",
            "allowed_ports": sorted(self.allowed_ports) if self.allowed_ports is not None else "any_loopback",
            "allowed_loopback_connections": self.allowed_attempts,
            "blocked_outbound_attempts_count": len(self.blocked_attempts),
            "blocked_attempts": [
                {
                    "timestamp": b.timestamp,
                    "operation": b.operation,
                    "target_host": b.target_host,
                    "target_port": b.target_port,
                    "reason": b.reason,
                }
                for b in self.blocked_attempts
            ],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return {**payload, "evidence_sha256": digest}


def prove_network_isolation(allowed_ports: set[int] | None = None) -> dict[str, Any]:
    """Run automated probes against production/external endpoints inside the isolation guard."""
    probes = [
        ("external_remote_sonarr", ("198.51.100.73", 8989), "tcp"),
        ("external_remote_radarr", ("198.51.100.73", 7878), "tcp"),
        ("external_remote_qbit", ("198.51.100.56", 8080), "tcp"),
        ("external_hostname_dns", ("git.example.invalid", 443), "dns"),
        ("bittorrent_dht_udp_bootstrap", ("198.51.100.10", 6881), "udp"),
        ("bittorrent_local_service_discovery_multicast", ("203.0.113.143", 6771), "udp"),
        ("bittorrent_udp_sendmsg_probe", ("198.51.100.10", 6881), "sendmsg"),
    ]
    with NetworkIsolationGuard(allowed_ports=allowed_ports or set()) as guard:
        for label, (host, port), proto in probes:
            try:
                if proto == "dns":
                    socket.getaddrinfo(host, port)
                elif proto == "udp":
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        sock.sendto(b"d1:ad2:id20:abcdefghij0123456789e1:q4:ping1:t2:aa1:y1:qe", (host, port))
                    finally:
                        sock.close()
                elif proto == "sendmsg":
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        sock.sendmsg([b"d1:q4:pinge"], [], 0, (host, port))
                    finally:
                        sock.close()
                else:
                    socket.create_connection((host, port), timeout=0.1)
            except SandboxIsolationViolation:
                pass
        # Also test urllib.request to production API
        try:
            urllib.request.urlopen("http://198.51.100.73:8989/api/v3/system/status", timeout=0.1)
        except (SandboxIsolationViolation, urllib.error.URLError):
            pass
    summary = guard.evidence_summary()
    summary["all_probes_blocked"] = summary["blocked_outbound_attempts_count"] == len(probes) + 1
    return summary
