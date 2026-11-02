"""Explicit-service reads, kept separate from the trained v2 contract.

Lazy imports let the schema registry read constants without importing adapters
and re-entering the protocol/planner modules during their initialization.
"""
from .contract import SERVICE_PERMISSIONS, SUPPORTED_OPERATIONS, validate_service_query

__all__ = ["SERVICE_PERMISSIONS", "SUPPORTED_OPERATIONS", "ServiceConfig", "ServiceQueryAdapter",
           "load_service_configs", "route_service_query", "validate_service_query", "NativeTransferAdapter"]


def __getattr__(name):
    if name == "NativeTransferAdapter":
        from .transfers import NativeTransferAdapter
        return NativeTransferAdapter
    if name == "route_service_query":
        from .routing import route_service_query
        return route_service_query
    if name in {"ServiceConfig", "ServiceQueryAdapter", "load_service_configs"}:
        from . import native
        return getattr(native, name)
    raise AttributeError(name)
