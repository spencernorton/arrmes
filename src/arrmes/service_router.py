"""Select a configured native read adapter without exposing transport to models."""
from __future__ import annotations

from typing import Any

from .adapters import AdapterObservation
from .catalog import V3_BY_NAME
from .planner import Principal
from .protocol import Call, validate_call
from .services import ServiceQueryAdapter, NativeTransferAdapter
from .services.transfers import CONFIRMATION_PREFIX, TORRENT_ACTIONS
from .services.media import NativeMediaReadAdapter
from .services.bitmagnet import NativeBitmagnetAdapter
from .services.bazarr_subtitles import NativeBazarrSubtitleAdapter, SUBTITLE_ACTIONS
from .services.bazarr_subtitles import CONFIRMATION_PREFIX as BAZARR_CONFIRMATION_PREFIX
from .services.requests import NativeRequestAdapter, CONFIRMATION_PREFIX as REQUEST_CONFIRMATION_PREFIX


class ConfiguredAdapterRouter:
    """A native read path and an optional legacy gateway share one policy boundary.

    Native queries never fall back to a gateway endpoint for a different service.
    Legacy write confirmation remains wholly owned by its existing adapter.
    """

    supports_service_queries = True

    def __init__(self, native: ServiceQueryAdapter, legacy: Any | None = None, *, write_mode: str = "confirm"):
        self.native = native
        self.legacy = legacy
        self.transfers = NativeTransferAdapter(native, write_mode="confirm" if write_mode == "direct" else write_mode)
        self.media_reads = NativeMediaReadAdapter(native)
        self.bitmagnet_reads = NativeBitmagnetAdapter(native)
        self.subtitles = NativeBazarrSubtitleAdapter(native, write_mode="confirm" if write_mode == "direct" else write_mode)
        self.requests = NativeRequestAdapter(native, write_mode="confirm" if write_mode == "direct" else write_mode)

    def execute(self, call: Call, principal: Principal, *, idempotency_key: str | None = None) -> AdapterObservation:
        call = validate_call(call.name, call.arguments)
        if call.name in SUBTITLE_ACTIONS:
            return self.subtitles.execute(call, principal, idempotency_key=idempotency_key)
        if self.requests.supports(call.name):
            return self.requests.execute(call, principal, idempotency_key=idempotency_key)
        if call.name == "service_query":
            return self.native.execute(call, principal, idempotency_key=idempotency_key)
        if call.name in TORRENT_ACTIONS and "qbittorrent" in self.native.configs:
            return self.transfers.execute(call, principal, idempotency_key=idempotency_key)
        if self.media_reads.supports(call.name):
            return self.media_reads.execute(call, principal, idempotency_key=idempotency_key)
        if self.bitmagnet_reads.supports(call.name):
            return self.bitmagnet_reads.execute(call, principal, idempotency_key=idempotency_key)
        if self.legacy is not None:
            return self.legacy.execute(call, principal, idempotency_key=idempotency_key)
        return AdapterObservation("unsupported", V3_BY_NAME[call.name].service, call.name, 501, False,
                                  {"error": "legacy_gateway_not_configured"}, [])

    def confirm_write(self, confirm_id: str, principal: Principal, *,
                      idempotency_key: str | None = None, now: float | None = None) -> AdapterObservation:
        if isinstance(confirm_id, str) and confirm_id.startswith(BAZARR_CONFIRMATION_PREFIX):
            return self.subtitles.confirm_write(confirm_id, principal, idempotency_key=idempotency_key, now=now)
        if isinstance(confirm_id, str) and confirm_id.startswith(REQUEST_CONFIRMATION_PREFIX):
            return self.requests.confirm_write(confirm_id, principal, idempotency_key=idempotency_key, now=now)
        if isinstance(confirm_id, str) and confirm_id.startswith(CONFIRMATION_PREFIX):
            return self.transfers.confirm_write(confirm_id, principal, idempotency_key=idempotency_key, now=now)
        if self.legacy is not None:
            return self.legacy.confirm_write(confirm_id, principal, idempotency_key=idempotency_key, now=now)
        return AdapterObservation("denied", "control", "confirm_write", 403, False,
                                  {"error": "write_adapter_not_configured"}, [])
