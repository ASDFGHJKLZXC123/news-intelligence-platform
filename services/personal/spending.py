"""Workspace-wide, crash-safe accounting for every ordinary personal paid request.

All decisions use Decimal and a PostgreSQL transaction advisory lock shared with
settings writes. Independent sessions commit reservations and dispatch handoffs even
when the caller's article/report transaction rolls back. Uncertainty never expires.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models.core import LLMRun
from db.models.personal import PersonalProfileRevision, PersonalRun, PersonalWorkspace
from db.models.personal_spending import (
    PersonalLegacyUsage,
    PersonalPaidRequest,
    PersonalSpendingState,
)
from services.personal.settings import safe_model_route

USD_QUANTUM = Decimal("0.000000000001")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,12})?$")
_UNRESOLVED = ("reserved", "dispatching", "uncertain")
_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "deepseek"})
_PRICE_DOMAINS = {
    "openai": ("openai.com",),
    "anthropic": ("anthropic.com", "claude.com"),
    "gemini": ("ai.google.dev", "cloud.google.com"),
    "deepseek": ("deepseek.com",),
}


class PaidWorkBlocked(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.replace("_", " "))


def decimal_usd(value: Any, name: str = "USD value") -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError(
            f"{name} must be a finite non-negative decimal string with at most 12 places"
        )
    return Decimal(value)


def _bounded_int(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


@dataclass(frozen=True)
class PaidRoute:
    role: str
    provider: str
    model: str
    model_version: str
    price_revision: str
    price_source_url: str
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    max_input_tokens: int
    max_output_tokens: int
    deadline_seconds: int

    @classmethod
    def from_mapping(cls, value: Any, *, role: str) -> PaidRoute:
        keys = {
            "provider",
            "model",
            "model_version",
            "price_revision",
            "price_source_url",
            "input_usd_per_million_tokens",
            "output_usd_per_million_tokens",
            "max_input_tokens",
            "max_output_tokens",
            "deadline_seconds",
        }
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError(
                "paid route requires exact versioned prices, identity, token bounds, and deadline"
            )
        if role not in {"generation", "embedding"} or value["provider"] not in _PROVIDERS:
            raise ValueError("unsupported paid route")
        if role == "embedding" and value["provider"] != "openai":
            raise ValueError("only the configured OpenAI embedding adapter is supported")
        for field in ("model", "model_version", "price_revision"):
            if not isinstance(value[field], str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value[field]
            ):
                raise ValueError(f"paid route {field} is missing or invalid")
        source = value["price_source_url"]
        if not isinstance(source, str) or len(source) > 512:
            raise ValueError("paid route price source must identify official pricing")
        parsed = urlsplit(source)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not any(
                parsed.hostname == domain or (parsed.hostname or "").endswith("." + domain)
                for domain in _PRICE_DOMAINS[value["provider"]]
            )
        ):
            raise ValueError("paid route price source must identify official pricing")
        input_price = decimal_usd(value["input_usd_per_million_tokens"], "input price")
        output_price = decimal_usd(value["output_usd_per_million_tokens"], "output price")
        if input_price <= 0 or (role == "generation" and output_price <= 0):
            raise ValueError("paid route must have positive known token prices")
        if role == "embedding" and output_price != 0:
            raise ValueError("embedding output price must be zero")
        return cls(
            role=role,
            provider=value["provider"],
            model=value["model"],
            model_version=value["model_version"],
            price_revision=value["price_revision"],
            price_source_url=source,
            input_usd_per_million_tokens=input_price,
            output_usd_per_million_tokens=output_price,
            max_input_tokens=_bounded_int(
                value["max_input_tokens"], 1, 1_000_000, "input token limit"
            ),
            max_output_tokens=_bounded_int(
                value["max_output_tokens"],
                0 if role == "embedding" else 1,
                0 if role == "embedding" else 65536,
                "output token limit",
            ),
            deadline_seconds=_bounded_int(value["deadline_seconds"], 1, 300, "request deadline"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            key: format(value, "f") if isinstance(value, Decimal) else value
            for key, value in vars(self).items()
            if key != "role"
        }

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            (
                Decimal(input_tokens) * self.input_usd_per_million_tokens
                + Decimal(output_tokens) * self.output_usd_per_million_tokens
            )
            / Decimal(1_000_000)
        ).quantize(USD_QUANTUM, rounding=ROUND_CEILING)


def validate_model_route(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"mode", "generation", "embedding"}
        or value["mode"] != "live"
    ):
        raise ValueError(
            "paid routing requires one generation route and one embedding route, without fallback"
        )
    return {
        "mode": "live",
        **{
            role: PaidRoute.from_mapping(value[role], role=role).to_mapping()
            for role in ("generation", "embedding")
        },
    }


def acquire_workspace_spending_lock(session: Session, workspace_id: uuid.UUID) -> None:
    # Stable cross-process key; deliberately disjoint from writer/admission lock namespaces.
    key = int.from_bytes(
        hashlib.blake2b(b"personal-spending:" + workspace_id.bytes, digest_size=8).digest(),
        "big",
        signed=True,
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("spending time must include a timezone")
    return value.astimezone(dt.UTC)


def accounting_month(value: dt.datetime) -> dt.date:
    return _utc(value).date().replace(day=1)


def _next_month(month: dt.date) -> dt.datetime:
    return dt.datetime(
        month.year + (month.month == 12),
        1 if month.month == 12 else month.month + 1,
        1,
        tzinfo=dt.UTC,
    )


def import_legacy_usage(
    session: Session, workspace_id: uuid.UUID, *, now: dt.datetime | None = None
) -> None:
    """Record each legacy audit once; guarded-call audit copies are never imported twice.

    The migration imports populated legacy data. This also supports a workspace created
    after migration and metadata-created test databases. Existing unknown rows stay unknown
    until explicit receipt reconciliation; modifying an old audit alone is not evidence.
    """
    acquire_workspace_spending_lock(session, workspace_id)
    cutoff = _utc(now or dt.datetime.now(dt.UTC))
    if session.get(PersonalSpendingState, workspace_id) is None:
        session.add(PersonalSpendingState(workspace_id=workspace_id, legacy_cutover_at=cutoff))
    occurred = func.coalesce(LLMRun.started_at, LLMRun.completed_at, LLMRun.created_at)
    for row, timestamp in session.execute(
        select(LLMRun, occurred).where(
            LLMRun.created_at <= cutoff, *_unimported_legacy_conditions()
        )
    ):
        cost = established_legacy_cost(row)
        session.execute(
            insert(PersonalLegacyUsage)
            .values(
                llm_run_id=row.id,
                accounting_month=accounting_month(timestamp),
                actual_usd=cost,
                evidence={"kind": "legacy_llm_run", "cost_established": cost is not None},
            )
            .on_conflict_do_nothing(index_elements=["llm_run_id"])
        )
    session.flush()


def established_legacy_cost(row: LLMRun) -> Decimal | None:
    """Legacy zero is not proof of a free call: old timeout/missing-price paths wrote 0.

    Preserve positive recorded usage. Accept zero only with an existing audit fact
    proving cache reuse, a known pre-dispatch refusal, or the dedicated offline adapter.
    Unknown amounts require a provider receipt before paid enablement.
    """
    cost = None if row.cost_usd is None else Decimal(str(row.cost_usd))
    if cost is not None and cost > 0:
        return cost.quantize(USD_QUANTUM, rounding=ROUND_CEILING)
    refs = row.input_refs if isinstance(row.input_refs, dict) else {}
    cache_hit = refs.get("cache_hit") is True
    predispatch = (
        row.status == "failed"
        and row.error_message == "token bucket limit reached"
        and row.input_tokens == 0
        and row.output_tokens == 0
        and row.latency_ms == 0
    )
    offline = row.provider == "offline-personal-fixture" and row.model.startswith(
        "offline-personal-fixture:"
    )
    return Decimal(0) if cache_hit or predispatch or offline else None


def _unimported_legacy_conditions() -> tuple[Any, ...]:
    return (
        ~select(PersonalLegacyUsage.llm_run_id)
        .where(PersonalLegacyUsage.llm_run_id == LLMRun.id)
        .exists(),
        or_(
            LLMRun.model_params.is_(None),
            ~LLMRun.model_params.contains({"personal_paid_ledger_accounted": True}),
        ),
    )


def reconcile_legacy_usage(
    session: Session,
    workspace_id: uuid.UUID,
    llm_run_id: uuid.UUID,
    *,
    actual_usd: str,
    receipt: str,
) -> None:
    """Explicit receipt-backed correction for a retained unknown; caller owns commit."""
    amount = decimal_usd(actual_usd)
    if not receipt or len(receipt) > 512:
        raise ValueError("a provider receipt reference is required")
    acquire_workspace_spending_lock(session, workspace_id)
    row = session.get(PersonalLegacyUsage, llm_run_id)
    if row is None:
        raise ValueError("legacy usage identity is unavailable")
    if row.actual_usd is not None:
        if row.actual_usd != amount:
            raise ValueError("known legacy usage cannot be rewritten")
        return
    row.actual_usd = amount
    row.evidence = {"kind": "provider_receipt", "receipt": receipt}


def assert_legacy_usage_reconciled(
    session: Session, workspace_id: uuid.UUID, *, now: dt.datetime | None = None
) -> None:
    stamp = _utc(now or dt.datetime.now(dt.UTC))
    import_legacy_usage(session, workspace_id, now=stamp)
    if session.scalar(
        select(func.count())
        .select_from(PersonalLegacyUsage)
        .where(
            PersonalLegacyUsage.accounting_month == accounting_month(stamp),
            PersonalLegacyUsage.actual_usd.is_(None),
        )
    ):
        raise PaidWorkBlocked(
            "legacy_usage_unreconciled",
            "Prior current-month application usage needs receipt reconciliation before enabling paid work.",
        )


def _ceiling(settings: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    if settings.get("ai_enabled") is not True:
        raise PaidWorkBlocked("ai_disabled")
    try:
        month = decimal_usd(settings.get("monthly_allowance_usd"), "monthly allowance")
        run = (
            decimal_usd(settings["run_allowance_usd"], "run allowance")
            if settings.get("run_allowance_usd") is not None
            else month
        )
    except ValueError as exc:
        raise PaidWorkBlocked("configuration_missing", str(exc)) from exc
    if month <= 0 or run <= 0:
        raise PaidWorkBlocked("allowance_reached")
    return month, min(month, run)


def _obligation(row: PersonalPaidRequest) -> Decimal:
    if row.status == "reconciled":
        return row.actual_usd or Decimal(0)
    return row.reserved_usd if row.status in _UNRESOLVED else Decimal(0)


class SpendingLedger:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        ownership_token: uuid.UUID,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.workspace_id, self.run_id, self.ownership_token = workspace_id, run_id, ownership_token
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.blocked_code: str | None = None

    def _context(self, session: Session) -> tuple[PersonalRun, dict[str, Any], Decimal, Decimal]:
        # The processing control and run are locked before the allowance ledger, in
        # the transaction that durably grants reservation/dispatch permission.
        from services.personal.runs import PersonalOwnershipLost, WriterModeConflict, lock_owned_run

        try:
            run = lock_owned_run(
                session,
                self.run_id,
                self.ownership_token,
                allow_queued=False,
                dispatch=True,
                now=_utc(self.clock()),
            )
        except (PersonalOwnershipLost, WriterModeConflict) as exc:
            raise PaidWorkBlocked("stale_run_attempt") from exc
        if run.workspace_id != self.workspace_id:
            raise PaidWorkBlocked("run_unavailable")
        acquire_workspace_spending_lock(session, self.workspace_id)
        workspace = session.get(PersonalWorkspace, self.workspace_id)
        if workspace is None:
            raise PaidWorkBlocked("run_unavailable")
        frozen = session.get(PersonalProfileRevision, run.profile_revision_id)
        live = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
        if frozen is None or live is None:
            raise PaidWorkBlocked("configuration_missing")
        frozen_month, frozen_run = _ceiling(frozen.settings or {})
        live_month, live_run = _ceiling(live.settings or {})
        return run, frozen.settings or {}, min(frozen_month, live_month), min(frozen_run, live_run)

    @staticmethod
    def _require_request_budget(run: PersonalRun, seconds: float, stamp: dt.datetime) -> None:
        if (
            run.graceful_deadline_at is None
            or (run.graceful_deadline_at - stamp).total_seconds() < seconds
        ):
            raise PaidWorkBlocked("execution_deadline_insufficient")

    def _allow(
        self,
        session: Session,
        *,
        month: dt.date,
        amount: Decimal,
        month_limit: Decimal,
        run_limit: Decimal,
        excluding: uuid.UUID | None = None,
    ) -> None:
        rows = session.scalars(
            select(PersonalPaidRequest).where(
                PersonalPaidRequest.workspace_id == self.workspace_id,
            )
        ).all()
        month_used = sum(
            (
                _obligation(row)
                for row in rows
                if row.accounting_month == month and row.id != excluding
            ),
            Decimal(0),
        )
        run_used = sum(
            (_obligation(row) for row in rows if row.run_id == self.run_id and row.id != excluding),
            Decimal(0),
        )
        legacy = session.scalars(
            select(PersonalLegacyUsage).where(PersonalLegacyUsage.accounting_month == month)
        ).all()
        if any(row.actual_usd is None for row in legacy):
            raise PaidWorkBlocked("legacy_usage_unreconciled")
        month_used += sum((row.actual_usd for row in legacy), Decimal(0))
        if month_used + amount > month_limit or run_used + amount > run_limit:
            raise PaidWorkBlocked("allowance_reached")

    def reserve(
        self, route: PaidRoute, *, input_token_bound: int, output_token_bound: int
    ) -> uuid.UUID:
        _bounded_int(input_token_bound, 1, route.max_input_tokens, "request input bound")
        _bounded_int(
            output_token_bound,
            0 if route.role == "embedding" else 1,
            route.max_output_tokens,
            "request output bound",
        )
        request_id = uuid.uuid4()  # A retry is always a fresh physical request.
        try:
            with self.session_factory() as session, session.begin():
                run, settings, month_limit, run_limit = self._context(session)
                if run.scopes_frozen_at is None:
                    raise PaidWorkBlocked("scope_not_frozen")
                configured = validate_model_route(settings.get("model_route"))[route.role]
                if configured != route.to_mapping():
                    raise PaidWorkBlocked("route_changed")
                stamp = _utc(self.clock())
                self._require_request_budget(run, route.deadline_seconds, stamp)
                import_legacy_usage(session, self.workspace_id, now=stamp)
                month = accounting_month(stamp)
                amount = route.cost(input_token_bound, output_token_bound)
                self._allow(
                    session,
                    month=month,
                    amount=amount,
                    month_limit=month_limit,
                    run_limit=run_limit,
                )
                session.add(
                    PersonalPaidRequest(
                        id=request_id,
                        workspace_id=self.workspace_id,
                        run_id=self.run_id,
                        attempt=run.attempt,
                        accounting_month=month,
                        status="reserved",
                        role=route.role,
                        route=route.to_mapping(),
                        input_token_bound=input_token_bound,
                        output_token_bound=output_token_bound,
                        reserved_usd=amount,
                        reserved_at=stamp,
                        evidence={},
                    )
                )
        except PaidWorkBlocked as exc:
            self.blocked_code = exc.code
            raise
        return request_id

    def dispatch(self, request_id: uuid.UUID) -> None:
        """Persist the authoritative handoff timestamp after lock acquisition.

        If the process dies after this commit, the dispatching reservation remains
        unresolved. Only the client that successfully performs this transition may send.
        """
        try:
            with self.session_factory() as session, session.begin():
                run, _, month_limit, run_limit = self._context(session)
                row = self._request(session, request_id)
                if row.status != "reserved" or row.attempt != run.attempt:
                    raise PaidWorkBlocked("request_already_dispatched")
                stamp = _utc(self.clock())
                route = PaidRoute.from_mapping(row.route, role=row.role)
                self._require_request_budget(run, route.deadline_seconds, stamp)
                import_legacy_usage(session, self.workspace_id, now=stamp)
                month = accounting_month(stamp)
                self._allow(
                    session,
                    month=month,
                    amount=row.reserved_usd,
                    month_limit=month_limit,
                    run_limit=run_limit,
                    excluding=row.id,
                )
                row.accounting_month = month
                row.dispatch_attempt_at = stamp
                row.status = "dispatching"
            self.blocked_code = None
        except PaidWorkBlocked as exc:
            self.blocked_code = exc.code
            raise

    def _request(self, session: Session, request_id: uuid.UUID) -> PersonalPaidRequest:
        row = session.get(PersonalPaidRequest, request_id)
        if row is None or row.workspace_id != self.workspace_id or row.run_id != self.run_id:
            raise ValueError("paid request identity is unavailable")
        return row

    def cancel_before_dispatch(self, request_id: uuid.UUID) -> None:
        with self.session_factory() as session, session.begin():
            acquire_workspace_spending_lock(session, self.workspace_id)
            row = self._request(session, request_id)
            if row.status != "reserved" or row.dispatch_attempt_at is not None:
                raise ValueError("only a confirmed pre-dispatch reservation can be cancelled")
            row.status = "cancelled"
            row.evidence = {"reason": "confirmed_before_dispatch"}

    def mark_uncertain(
        self, request_id: uuid.UUID, reason: str = "transport_outcome_unknown"
    ) -> None:
        with self.session_factory() as session, session.begin():
            acquire_workspace_spending_lock(session, self.workspace_id)
            row = self._request(session, request_id)
            if row.status in {"dispatching", "uncertain"}:
                row.status = "uncertain"
                row.evidence = {**row.evidence, "reason": reason[:128]}

    def reconcile(
        self,
        request_id: uuid.UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
            _bounded_int(value, 0, 2_147_483_647, name)
        exceeded = False
        with self.session_factory() as session, session.begin():
            acquire_workspace_spending_lock(session, self.workspace_id)
            row = self._request(session, request_id)
            if row.status == "reconciled":
                if (row.input_tokens, row.output_tokens) != (input_tokens, output_tokens):
                    raise ValueError("reconciled usage cannot be rewritten")
                return
            if row.status not in {"dispatching", "uncertain"}:
                raise ValueError("usage requires a dispatched request")
            route = PaidRoute.from_mapping(row.route, role=row.role)
            row.actual_usd = route.cost(input_tokens, output_tokens)
            row.input_tokens, row.output_tokens = input_tokens, output_tokens
            row.provider_request_id = provider_request_id[:256] if provider_request_id else None
            row.reconciled_at = _utc(self.clock())
            row.status = "reconciled"
            exceeded = (
                input_tokens > row.input_token_bound or output_tokens > row.output_token_bound
            )
            row.evidence = {
                "kind": "provider_usage",
                **dict(evidence or {}),
                "token_bound_exceeded": exceeded,
            }
        if exceeded:
            self.blocked_code = "provider_usage_exceeded_bound"
            raise PaidWorkBlocked(
                self.blocked_code,
                "Provider usage exceeded its reserved maximum; actual usage has been retained.",
            )

    def confirm_non_billable(self, request_id: uuid.UUID, *, receipt: str) -> None:
        if not receipt or len(receipt) > 512:
            raise ValueError("a provider non-billable receipt reference is required")
        with self.session_factory() as session, session.begin():
            acquire_workspace_spending_lock(session, self.workspace_id)
            row = self._request(session, request_id)
            if row.status not in {"dispatching", "uncertain"}:
                raise ValueError("non-billable evidence requires an unresolved dispatch")
            row.status, row.actual_usd = "non_billable", Decimal(0)
            row.evidence = {"kind": "provider_non_billable_receipt", "receipt": receipt}
            row.reconciled_at = _utc(self.clock())


def _public_route_identity(role: str | None, route: Mapping[str, Any]) -> dict[str, Any]:
    """Project retained public identifiers only; never serialize transport or receipt data."""
    return {
        "role": role,
        **{
            key: route[key] if isinstance(route.get(key), str) else None
            for key in ("provider", "model", "model_version", "price_revision")
        },
    }


def _accounted_routes(
    rows: list[PersonalPaidRequest], legacy_audits: list[LLMRun], month: dt.date
) -> list[dict[str, Any]]:
    """Identify routes contributing to current charges or retained unresolved obligations."""
    identities = [
        {
            "source": "paid_request",
            "accounting_period": row.accounting_month.strftime("%Y-%m"),
            **_public_route_identity(row.role, row.route),
        }
        for row in rows
        if row.status in _UNRESOLVED
        or (row.accounting_month == month and row.status == "reconciled")
    ]
    # Legacy audits have provider/model, but no authoritative role, snapshot or price
    # revision. Do not infer those from today's settings or arbitrary model_params.
    identities.extend(
        {
            "source": "legacy_usage",
            "accounting_period": month.strftime("%Y-%m"),
            **_public_route_identity(None, {"provider": row.provider, "model": row.model}),
        }
        for row in legacy_audits
    )
    keys = (
        "accounting_period",
        "source",
        "role",
        "provider",
        "model",
        "model_version",
        "price_revision",
    )
    unique = {tuple(item[key] or "" for key in keys): item for item in identities}
    return [unique[key] for key in sorted(unique)]


def spending_summary(
    session: Session, workspace_id: uuid.UUID, *, now: dt.datetime | None = None
) -> dict[str, Any]:
    stamp = _utc(now or dt.datetime.now(dt.UTC))
    workspace = session.get(PersonalWorkspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace is unavailable")
    profile = (
        session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
        if workspace.active_profile_revision_id is not None
        else None
    )
    settings = profile.settings if profile else {}
    current_route = safe_model_route((settings or {}).get("model_route"))
    try:
        allowance, _ = _ceiling(settings or {})
        status = "ready"
    except PaidWorkBlocked as exc:
        allowance, status = Decimal(0), exc.code
    month = accounting_month(stamp)
    rows = session.scalars(
        select(PersonalPaidRequest).where(PersonalPaidRequest.workspace_id == workspace_id)
    ).all()
    legacy_records = session.execute(
        select(PersonalLegacyUsage, LLMRun)
        .join(LLMRun, LLMRun.id == PersonalLegacyUsage.llm_run_id)
        .where(PersonalLegacyUsage.accounting_month == month)
    ).all()
    legacy = [usage for usage, _ in legacy_records]
    # Read endpoints disclose costs awaiting the next explicit import without writing
    # cutover metadata or relying on a GET transaction to commit side effects.
    occurred = func.coalesce(LLMRun.started_at, LLMRun.completed_at, LLMRun.created_at)
    pending_legacy = session.scalars(
        select(LLMRun).where(
            *_unimported_legacy_conditions(),
            occurred >= dt.datetime.combine(month, dt.time(), dt.UTC),
            occurred < _next_month(month),
            LLMRun.created_at <= stamp,
        )
    ).all()
    finalized = sum(
        (
            row.actual_usd or Decimal(0)
            for row in rows
            if row.accounting_month == month and row.status == "reconciled"
        ),
        Decimal(0),
    )
    finalized += sum((row.actual_usd or Decimal(0) for row in legacy), Decimal(0))
    pending_costs = [established_legacy_cost(row) for row in pending_legacy]
    finalized += sum((cost for cost in pending_costs if cost is not None), Decimal(0))
    reserved = sum(
        (
            row.reserved_usd
            for row in rows
            if row.accounting_month == month and row.status in _UNRESOLVED
        ),
        Decimal(0),
    )
    unresolved_all = sum(
        (row.reserved_usd for row in rows if row.status in _UNRESOLVED), Decimal(0)
    )
    unknown_legacy = sum(row.actual_usd is None for row in legacy) + sum(
        cost is None for cost in pending_costs
    )
    if unknown_legacy:
        status = "legacy_usage_unreconciled"
    elif status == "ready" and finalized + reserved >= allowance:
        status = "allowance_reached"
    return {
        "status": status,
        "accounting_period": month.strftime("%Y-%m"),
        "accounting_timezone": "UTC",
        "monthly_allowance_usd": format(allowance, "f"),
        "finalized_usd": format(finalized, "f"),
        "reserved_usd": format(reserved, "f"),
        "unresolved_usd": format(unresolved_all, "f"),
        "unreconciled_legacy_count": unknown_legacy,
        "remaining_usd": format(max(Decimal(0), allowance - finalized - reserved), "f"),
        "next_reset_at": _next_month(month).astimezone(ZoneInfo(workspace.timezone)).isoformat(),
        "current_configuration": {
            "profile_revision_id": str(profile.id) if profile else None,
            "profile_revision": profile.revision if profile else None,
            "route_mode": current_route.get("mode"),
            "routes": [
                _public_route_identity(role, current_route[role])
                for role in ("generation", "embedding")
                if role in current_route
            ],
        },
        "accounted_routes": _accounted_routes(
            rows, [audit for _, audit in legacy_records] + pending_legacy, month
        ),
    }
