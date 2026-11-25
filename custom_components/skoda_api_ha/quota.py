"""Quota planner for the per-VIN request quota of the MyŠkoda Public API.

No Home Assistant imports.

Rules:

* Regular GETs at the interval chosen by the user (default 12 per hour, 0
  to 20; 0 means no regular GETs).
* A reserve of ``max(8, ceil(limit / 3))`` requests is kept for commands and
  manual refreshes, but never more than the user leaves free: with more
  regular GETs per hour the reserve is ``limit - GETs per hour`` (0 at 20).
  Then the golden reserve cannot hold and commands are refused (user
  decision). Requests already spent from the reserve in the current
  window reduce what regular GETs must leave untouched.
* After every response the next GET is recomputed from ``RateLimit-Remaining``
  and ``RateLimit-Reset``, so the use of other clients of the same VIN counts.
* After 429 nothing is sent until ``Retry-After`` (plus a guard) has passed.
* Golden reserve: an extra request (command, confirmation GET, manual
  refresh) is allowed only if afterwards there are still enough requests for
  every regular GET until the reset plus one unused request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

DEFAULT_LIMIT = 20
MIN_RESERVE = 8
GOLDEN_RESERVE = 1
RESET_GUARD_SECONDS = 10
MIN_GET_SPACING_SECONDS = 61
RETRY_GUARD_SECONDS = 5
# Without Retry-After and without a usable reset: wait 15 minutes (fail closed).
DEFAULT_RETRY_SECONDS = 900
QUOTA_WINDOW = timedelta(hours=1)


def interval_seconds(polls_per_hour: int) -> int:
    """Regular GET interval in seconds for the polls per hour chosen by the user."""
    return math.ceil(3600 / max(1, polls_per_hour))


def reserve_for_limit(limit: int | None) -> int:
    """Return the requests reserved for commands and manual refreshes.

    A future larger quota is not assumed before the headers report it.
    """
    if not isinstance(limit, int) or limit < 1:
        limit = DEFAULT_LIMIT
    return max(MIN_RESERVE, math.ceil(limit / 3))


def reserve_for_polls(limit: int | None, polls_per_hour: int) -> int:
    """Reserve for the user's choice: never more than the requests left free."""
    if not isinstance(limit, int) or limit < 1:
        limit = DEFAULT_LIMIT
    return min(reserve_for_limit(limit), max(0, limit - max(0, polls_per_hour)))


def commands_possible(limit: int | None, polls_per_hour: int) -> bool:
    """Whether the reserve leaves room for a command above the golden reserve."""
    return reserve_for_polls(limit, polls_per_hour) > GOLDEN_RESERVE


@dataclass(frozen=True, slots=True)
class QuotaState:
    """Everything the planner needs. Persisted across restarts."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    # Start of the last regular GET attempt that reached (or tried to reach)
    # the API. Conservative: also set when the request failed.
    last_get_at: datetime | None = None
    # Start of the last request of any kind (GET, POST, PUT).
    last_request_at: datetime | None = None
    # Last 429: when it happened, its Retry-After and the reset known then.
    rate_limited_at: datetime | None = None
    retry_after: int | None = None
    # Extra requests (commands, confirmation GETs, manual refreshes) that
    # were taken from the reserve.
    extra_requests: tuple[datetime, ...] = field(default_factory=tuple)

    def window_open(self, now: datetime) -> bool:
        """Return True when the known ``remaining`` belongs to the current window."""
        return (
            self.remaining is not None
            and self.reset_at is not None
            and self.reset_at > now
        )

    def with_response(
        self,
        started_at: datetime,
        limit: int | None,
        remaining: int | None,
        reset_at: datetime | None,
    ) -> QuotaState:
        """State after a GET response with (possibly missing) quota headers."""
        return replace(
            self,
            limit=limit if limit is not None else self.limit,
            remaining=remaining if remaining is not None else self.remaining,
            reset_at=reset_at if reset_at is not None else self.reset_at,
            last_get_at=started_at,
            last_request_at=started_at,
            rate_limited_at=None,
            retry_after=None,
        )

    def with_failed_get(self, started_at: datetime) -> QuotaState:
        """State after a GET without a usable answer (network, 5xx, 401/403)."""
        return replace(self, last_get_at=started_at, last_request_at=started_at)

    def with_rate_limit(
        self,
        started_at: datetime,
        at: datetime,
        retry_after: int | None,
        reset_at: datetime | None,
    ) -> QuotaState:
        """State after 429: nothing left until the reset or ``Retry-After``."""
        return replace(
            self,
            remaining=0,
            reset_at=reset_at if reset_at is not None else self.reset_at,
            last_get_at=started_at,
            last_request_at=started_at,
            rate_limited_at=at,
            retry_after=retry_after,
        )

    def with_command_response(
        self,
        limit: int | None,
        remaining: int | None,
        reset_at: datetime | None,
    ) -> QuotaState:
        """Take the quota headers of a command answer (after the request is counted).

        The GET clock (``last_get_at``) does not move: a command is no GET.
        """
        return replace(
            self,
            limit=limit if limit is not None else self.limit,
            remaining=remaining if remaining is not None else self.remaining,
            reset_at=reset_at if reset_at is not None else self.reset_at,
        )

    def with_command_rate_limit(
        self, at: datetime, retry_after: int | None, reset_at: datetime | None
    ) -> QuotaState:
        """State after a 429 to a command: cooldown, GET clock unchanged."""
        return replace(
            self,
            remaining=0,
            reset_at=reset_at if reset_at is not None else self.reset_at,
            rate_limited_at=at,
            retry_after=retry_after,
        )

    def with_extra_request(self, at: datetime) -> QuotaState:
        """Record one request taken from the reserve (command, confirmation)."""
        window_start = at - QUOTA_WINDOW
        kept = tuple(t for t in self.extra_requests if t >= window_start)
        return replace(
            self,
            remaining=None if self.remaining is None else max(0, self.remaining - 1),
            last_request_at=at,
            extra_requests=(*kept, at),
        )

    def extra_spent(self, now: datetime) -> int:
        """Count the requests taken from the reserve in the current quota window."""
        if self.reset_at is not None and self.reset_at <= now:
            return 0
        start = (self.reset_at - QUOTA_WINDOW) if self.reset_at else now - QUOTA_WINDOW
        return sum(1 for t in self.extra_requests if start <= t <= now)

    def retry_delay(self, now: datetime) -> int:
        """Seconds until a 429 cooldown ends (0 when there is none)."""
        if self.rate_limited_at is None:
            return 0
        if self.retry_after is not None:
            deadline = self.rate_limited_at + timedelta(
                seconds=self.retry_after + RETRY_GUARD_SECONDS
            )
        elif self.reset_at is not None and self.reset_at > self.rate_limited_at:
            deadline = self.reset_at + timedelta(seconds=RETRY_GUARD_SECONDS)
        else:
            deadline = self.rate_limited_at + timedelta(seconds=DEFAULT_RETRY_SECONDS)
        return max(0, math.ceil((deadline - now).total_seconds()))

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for ``Store``."""
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": _iso(self.reset_at),
            "last_get_at": _iso(self.last_get_at),
            "last_request_at": _iso(self.last_request_at),
            "rate_limited_at": _iso(self.rate_limited_at),
            "retry_after": self.retry_after,
            "extra_requests": [t.isoformat() for t in self.extra_requests],
        }

    @classmethod
    def from_dict(cls, data: Any) -> QuotaState:
        """Read the stored form. Invalid values become unknown."""
        if not isinstance(data, dict):
            return cls()
        extra = data.get("extra_requests")
        return cls(
            limit=_int(data.get("limit")),
            remaining=_int(data.get("remaining")),
            reset_at=_dt(data.get("reset_at")),
            last_get_at=_dt(data.get("last_get_at")),
            last_request_at=_dt(data.get("last_request_at")),
            rate_limited_at=_dt(data.get("rate_limited_at")),
            retry_after=_int(data.get("retry_after")),
            extra_requests=tuple(
                t
                for t in (_dt(v) for v in (extra if isinstance(extra, list) else []))
                if t is not None
            ),
        )


@dataclass(frozen=True, slots=True)
class PollPlan:
    """When the next regular GET may happen and why."""

    delay: float
    next_get_at: datetime
    interval: int
    effective_interval: int
    reserve: int
    reserve_total: int
    extra_spent: int
    available_gets: int | None


def estimate(  # noqa: PLR0913, PLR0917 - all inputs of the estimate
    now: datetime,
    last: datetime | None,
    interval: int,
    remaining: int | None,
    reset_at: datetime | None,
    reserve: int,
) -> tuple[float, int, int | None]:
    """Compute when the next GET may occur at a fixed interval.

    ``RateLimit-Remaining`` already includes the cost of the previous request.
    When ``available`` GET tokens remain, to avoid making more than those
    before the reset, postpone the first one until at least
    ``reset_at - available * interval + guard``. The time until the reset is
    not divided by the available request count.

    Returns ``(delay, planned_interval, available_gets)``.
    """
    base_due = now if last is None else last + timedelta(seconds=interval)
    next_due = max(now, base_due)
    if remaining is not None and reset_at is not None and reset_at > now:
        available = max(0, remaining - reserve)
        if available == 0:
            next_due = max(next_due, reset_at + timedelta(seconds=RESET_GUARD_SECONDS))
        else:
            threshold = reset_at - timedelta(
                seconds=available * interval + RESET_GUARD_SECONDS
            )
            next_due = max(next_due, threshold)
            # Avoid a discretionary GET right on the quota rollover boundary.
            if 0 < (reset_at - next_due).total_seconds() <= RESET_GUARD_SECONDS:
                next_due = reset_at + timedelta(seconds=RESET_GUARD_SECONDS)
    if last is not None:
        next_due = max(next_due, last + timedelta(seconds=MIN_GET_SPACING_SECONDS))
    delay = max(0.0, (next_due - now).total_seconds())
    planned = (
        max(interval, math.ceil((next_due - last).total_seconds()))
        if last
        else interval
    )
    return delay, planned, None if remaining is None else max(0, remaining - reserve)


def plan_next_get(state: QuotaState, now: datetime, polls_per_hour: int) -> PollPlan:
    """Plan the next regular GET from the stored state."""
    interval = interval_seconds(polls_per_hour)
    reserve_total = reserve_for_polls(state.limit, polls_per_hour)
    spent = state.extra_spent(now)
    reserve = max(0, reserve_total - spent)
    delay, _, available = estimate(
        now, state.last_get_at, interval, state.remaining, state.reset_at, reserve
    )
    if state.last_request_at is not None:
        # A command does not reset the GET clock, but requests stay spaced.
        delay = max(
            delay,
            MIN_GET_SPACING_SECONDS - (now - state.last_request_at).total_seconds(),
        )
    delay = max(0.0, delay, float(state.retry_delay(now)))
    next_get_at = now + timedelta(seconds=delay)
    effective = (
        max(interval, math.ceil((next_get_at - state.last_get_at).total_seconds()))
        if state.last_get_at
        else interval
    )
    return PollPlan(
        delay=delay,
        next_get_at=next_get_at,
        interval=interval,
        effective_interval=effective,
        reserve=reserve,
        reserve_total=reserve_total,
        extra_spent=spent,
        available_gets=available,
    )


def regular_gets_until_reset(
    state: QuotaState, now: datetime, polls_per_hour: int
) -> int:
    """Regular GETs still planned before the quota resets."""
    if polls_per_hour <= 0 or state.reset_at is None or state.reset_at <= now:
        return 0
    interval = interval_seconds(polls_per_hour)
    next_due = (
        now
        if state.last_get_at is None
        else max(now, state.last_get_at + timedelta(seconds=interval))
    )
    if next_due >= state.reset_at:
        return 0
    return math.ceil((state.reset_at - next_due).total_seconds() / interval)


def can_spend(
    state: QuotaState, now: datetime, polls_per_hour: int, cost: int = 1
) -> bool:
    """Return True when ``cost`` extra requests keep regular GETs and golden reserve.

    Requires a 429-free, current quota window: a ``remaining`` from a window
    that has already reset says nothing about the new one.
    """
    if state.retry_delay(now) > 0 or not state.window_open(now):
        return False
    needed = regular_gets_until_reset(state, now, polls_per_hour) + GOLDEN_RESERVE
    return (state.remaining or 0) - cost >= needed


def can_spend_stop(state: QuotaState, now: datetime, polls_per_hour: int) -> bool:
    """Return True when charging STOP may be sent (design decision).

    STOP may use the golden reserve: afterwards enough requests must remain
    only for every regular GET until the reset. A window
    that has already reset does not block it (the new window starts full).
    Never during a 429 cooldown, and never without any known quota.
    """
    if state.retry_delay(now) > 0 or state.remaining is None:
        return False
    if state.reset_at is not None and state.reset_at <= now:
        return True
    if state.reset_at is None:
        return False
    return state.remaining - 1 >= regular_gets_until_reset(state, now, polls_per_hour)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
