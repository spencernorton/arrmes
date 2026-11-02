"""Bounded multi-step execution architecture: proposal -> policy validation -> adapter -> verified observation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from arrmes.adapters import AdapterObservation, SandboxAdapter
from arrmes.catalog import V3_BY_NAME, WRITE_EFFECT_ACTIONS
from arrmes.planner import Principal, prepare, verify_proposal
from arrmes.protocol import Call, InvalidCall, decode, normalized, validate_call
from arrmes.sandbox.isolation import SandboxIsolationViolation


@dataclass
class LedgerEntry:
    title: str
    media_type: str  # "movie" | "tv"
    year: int | None
    turn_recorded: int


@dataclass
class SessionEntityLedger:
    """Trusted application context ledger updated strictly from verified adapter outputs."""

    principal_id: str
    current_turn: int = 0
    ttl_turns: int = 3
    latest_entry: LedgerEntry | None = None

    def advance_turn(self) -> None:
        self.current_turn += 1
        if self.latest_entry is not None and (self.current_turn - self.latest_entry.turn_recorded) > self.ttl_turns:
            self.latest_entry = None

    def record_from_observation(self, obs: AdapterObservation) -> None:
        if obs.status != "verified_ok":
            return
        if obs.operation in {"tv_status", "tv_missing", "tv_search_missing"}:
            title = obs.data.get("title")
            if isinstance(title, str) and title:
                self.latest_entry = LedgerEntry(title=title, media_type="tv", year=obs.data.get("year"), turn_recorded=self.current_turn)
        elif obs.operation in {"movie_status", "movie_search"}:
            title = obs.data.get("title")
            if isinstance(title, str) and title:
                self.latest_entry = LedgerEntry(title=title, media_type="movie", year=obs.data.get("year"), turn_recorded=self.current_turn)
        elif obs.operation in {"request_status", "request_media"}:
            title = obs.data.get("title")
            mtype = obs.data.get("media_type")
            if isinstance(title, str) and title and mtype in {"movie", "tv"}:
                self.latest_entry = LedgerEntry(title=title, media_type=mtype, year=obs.data.get("year"), turn_recorded=self.current_turn)

    def to_model_context(self) -> dict[str, Any] | None:
        if self.latest_entry is None:
            return None
        if (self.current_turn - self.latest_entry.turn_recorded) > self.ttl_turns:
            return None
        sel: dict[str, Any] = {
            "title": self.latest_entry.title,
            "media_type": self.latest_entry.media_type,
        }
        if isinstance(self.latest_entry.year, int) and 1870 <= self.latest_entry.year <= 2200:
            sel["year"] = self.latest_entry.year
        return {"selected_media": sel}


@dataclass
class ExecutionTrace:
    terminal_state: str  # "completed" | "dispatched" | "partial" | "clarify" | "unsupported" | "denied" | "dry_run_planned" | "error" | "invalid_proposal"
    proposal: dict[str, Any] | None
    plan: dict[str, Any] | None
    observation: AdapterObservation | None
    steps_executed: int
    unsafe_write_blocked: bool = False
    audit_records: list[dict[str, Any]] = field(default_factory=list)


def derive_adapter_plan_state(obs: AdapterObservation) -> dict[str, Any]:
    """Derive executed and adapter_state from obs.status, obs.verified_complete, and audit_trail.

    Distinguishes:
    - "verified": obs.status == "verified_ok" and obs.verified_complete is True
    - "confirmed_execution": obs.status == "verified_ok" (unverified completion) or a mutation
      request succeeded (2xx POST/PUT/DELETE in audit_trail) before a subsequent verification step failed
    - "unknown_outcome": transport, timeout, or upstream JSON decoding failure where outcome cannot be confirmed
    - "attempted_dispatch": deterministic upstream rejection (4xx/5xx) with no confirmed state mutation
    """
    if obs.status == "verified_ok" and obs.verified_complete:
        return {"dry_run": False, "executed": True, "adapter_state": "verified"}
    if obs.status == "unsupported":
        return {"dry_run": False, "executed": False, "adapter_state": "unsupported"}
    if obs.status == "partial":
        return {"dry_run": False, "executed": True, "adapter_state": "partial"}

    mutation_succeeded = any(
        isinstance(r, dict)
        and r.get("method") in {"POST", "PUT", "DELETE"}
        and isinstance(r.get("status"), int)
        and 200 <= r["status"] < 300
        for r in (obs.audit_trail or [])
    )
    if obs.status in {"verified_ok", "dispatched"} or mutation_succeeded:
        return {"dry_run": False, "executed": True, "adapter_state": "confirmed_execution"}

    err_code = obs.data.get("error") if isinstance(obs.data, dict) else None
    if obs.http_status in {502, 503, 504} or err_code in {
        "service_unavailable",
        "invalid_upstream_json",
        "adapter_boundary_exception",
    }:
        return {"dry_run": False, "executed": False, "adapter_state": "unknown_outcome"}

    return {"dry_run": False, "executed": False, "adapter_state": "attempted_dispatch"}


def _terminal_state_for(obs: AdapterObservation) -> str:
    """Map an adapter observation to a terminal state without overstating completion."""
    if obs.status == "verified_ok" and obs.verified_complete:
        return "completed"
    if obs.status == "dispatched":
        return "dispatched"
    if obs.status == "unsupported":
        return "unsupported"
    if obs.status == "partial":
        return "partial"
    if obs.status == "denied":
        return "denied"
    return "error"


class BoundedExecutor:
    """Trusted execution loop enforcing schema, permissions, write gates, and sandbox verification."""

    def __init__(
        self,
        adapter: SandboxAdapter,
        *,
        allow_writes: bool = True,
        max_steps: int = 4,
    ) -> None:
        self.adapter = adapter
        self.allow_writes = allow_writes
        self.max_steps = max_steps

    def execute_raw_proposal(
        self,
        raw_output: str,
        principal: Principal,
        *,
        family: str = "functiongemma",
        query: str | None = None,
        context: dict[str, Any] | None = None,
        ledger: SessionEntityLedger | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionTrace:
        try:
            call = decode(raw_output, family=family)
        except InvalidCall as exc:
            return ExecutionTrace(
                terminal_state="invalid_proposal",
                proposal=None,
                plan={"error": str(exc), "executed": False, "adapter_state": "invalid_proposal"},
                observation=None,
                steps_executed=0,
            )
        return self.execute_call(
            call,
            principal,
            query=query,
            context=context,
            ledger=ledger,
            idempotency_key=idempotency_key,
        )

    def execute_call(
        self,
        call: Call,
        principal: Principal,
        *,
        query: str | None = None,
        context: dict[str, Any] | None = None,
        ledger: SessionEntityLedger | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionTrace:
        if ledger is not None and ledger.principal_id != principal.subject:
            raise PermissionError("Cross-principal ledger access forbidden")

        effective_context = context if context is not None else (ledger.to_model_context() if ledger is not None else None)
        if query is not None:
            call = verify_proposal(call, query, effective_context)

        plan = prepare(call, principal)
        norm_call = normalized(call)

        if plan["status"] == "denied":
            return ExecutionTrace(
                terminal_state="denied",
                proposal=norm_call,
                plan={**plan, "executed": False, "adapter_state": "denied"},
                observation=None,
                steps_executed=0,
                unsafe_write_blocked=call.name in WRITE_EFFECT_ACTIONS,
            )

        if call.name in {"clarify", "unsupported"}:
            return ExecutionTrace(
                terminal_state=call.name,
                proposal=norm_call,
                plan={**plan, "executed": False, "adapter_state": call.name},
                observation=None,
                steps_executed=0,
            )

        spec = V3_BY_NAME[call.name]
        if spec.effect != "read" and not self.allow_writes:
            return ExecutionTrace(
                terminal_state="dry_run_planned",
                proposal=norm_call,
                plan={**plan, "executed": False, "adapter_state": "dry_run_planned"},
                observation=None,
                steps_executed=0,
            )

        try:
            obs = self.adapter.execute(call, principal, idempotency_key=idempotency_key)
        except (SandboxIsolationViolation, PermissionError):
            raise
        except Exception as exc:
            obs = AdapterObservation(
                status="error",
                service=spec.service,
                operation=call.name,
                http_status=503,
                verified_complete=False,
                data={"error": "adapter_boundary_exception", "exception_type": type(exc).__name__},
                audit_trail=[],
            )

        if ledger is not None:
            ledger.record_from_observation(obs)

        if obs.status == "clarify_required":
            return ExecutionTrace(
                terminal_state="clarify",
                proposal=norm_call,
                plan={**plan, "dry_run": False, "executed": False, "adapter_state": "clarify_required"},
                observation=obs,
                steps_executed=1,
                audit_records=obs.audit_trail,
            )

        if obs.status == "confirmation_required":
            return ExecutionTrace(
                terminal_state="awaiting_confirmation",
                proposal=norm_call,
                plan={
                    **plan,
                    "dry_run": False,
                    "executed": False,
                    "adapter_state": "awaiting_confirmation",
                    "confirm_id": obs.data.get("confirm_id"),
                    "summary": obs.data.get("summary"),
                },
                observation=obs,
                steps_executed=1,
                audit_records=obs.audit_trail,
            )

        if obs.status == "shadow_logged":
            return ExecutionTrace(
                terminal_state="shadow_logged",
                proposal=norm_call,
                plan={
                    **plan,
                    "dry_run": True,
                    "executed": False,
                    "adapter_state": "shadow_logged",
                    "summary": obs.data.get("summary"),
                },
                observation=obs,
                steps_executed=1,
                audit_records=obs.audit_trail,
            )

        state_fields = derive_adapter_plan_state(obs)
        return ExecutionTrace(
            terminal_state=_terminal_state_for(obs),
            proposal=norm_call,
            plan={**plan, **state_fields},
            observation=obs,
            steps_executed=1,
            audit_records=obs.audit_trail,
        )

    def confirm_write(
        self,
        confirm_id: str,
        principal: Principal,
        *,
        ledger: SessionEntityLedger | None = None,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> ExecutionTrace:
        """Redeem a short-lived write confirmation token for the authenticated principal."""
        if ledger is not None and ledger.principal_id != principal.subject:
            raise PermissionError("Cross-principal ledger access forbidden")
        if not self.allow_writes:
            return ExecutionTrace(
                terminal_state="denied",
                proposal=None,
                plan={"status": "denied", "executed": False, "adapter_state": "denied", "error": "writes_disabled"},
                observation=None,
                steps_executed=0,
                unsafe_write_blocked=True,
            )
        confirm_fn = getattr(self.adapter, "confirm_write", None)
        if confirm_fn is None:
            return ExecutionTrace(
                terminal_state="error",
                proposal=None,
                plan={"error": "adapter_does_not_support_confirm_write", "executed": False, "adapter_state": "error"},
                observation=None,
                steps_executed=0,
            )
        obs = confirm_fn(confirm_id, principal, idempotency_key=idempotency_key, now=now)
        if ledger is not None:
            ledger.record_from_observation(obs)
        if obs.status == "denied":
            return ExecutionTrace(
                terminal_state="denied",
                proposal=None,
                plan={"status": "denied", "executed": False, "adapter_state": "denied", "error": obs.data.get("error")},
                observation=obs,
                steps_executed=0,
                unsafe_write_blocked=True,
                audit_records=obs.audit_trail,
            )
        state_fields = derive_adapter_plan_state(obs)
        confirmed = obs.data.get("confirmed_call")
        proposal = None
        if isinstance(confirmed, dict) and set(confirmed) == {"name", "arguments"}:
            try:
                confirmed_call = validate_call(confirmed["name"], confirmed["arguments"])
                if confirmed_call.name == obs.operation:
                    proposal = normalized(confirmed_call)
            except InvalidCall:
                pass
        return ExecutionTrace(
            terminal_state=_terminal_state_for(obs),
            proposal=proposal,
            plan=state_fields,
            observation=obs,
            steps_executed=1,
            audit_records=obs.audit_trail,
        )

    def execute_workflow(
        self,
        steps: list[Callable[[list[AdapterObservation], SessionEntityLedger], Call | None]],
        principal: Principal,
    ) -> dict[str, Any]:
        """Execute a bounded multi-step workflow where each step depends on verified prior observations."""
        ledger = SessionEntityLedger(principal_id=principal.subject)
        observations: list[AdapterObservation] = []
        traces: list[ExecutionTrace] = []

        for idx, step_fn in enumerate(steps):
            if idx >= self.max_steps:
                return {
                    "terminal_state": "budget_exhausted",
                    "steps_executed": idx,
                    "traces": traces,
                }
            ledger.advance_turn()
            next_call = step_fn(observations, ledger)
            if next_call is None:
                return {
                    "terminal_state": "completed",
                    "steps_executed": idx,
                    "traces": traces,
                }
            trace = self.execute_call(next_call, principal, ledger=ledger)
            traces.append(trace)
            if trace.observation is not None:
                observations.append(trace.observation)
            if trace.terminal_state != "completed":
                return {
                    "terminal_state": trace.terminal_state,
                    "steps_executed": idx + 1,
                    "traces": traces,
                }

        return {
            "terminal_state": "completed",
            "steps_executed": len(traces),
            "traces": traces,
        }
