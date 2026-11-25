"""Quota planner (estimate, reserve, 429, golden reserve)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.skoda_api_ha.quota import (
    QuotaState,
    can_spend,
    can_spend_stop,
    estimate,
    interval_seconds,
    plan_next_get,
    regular_gets_until_reset,
    reserve_for_limit,
)

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """NOW plus seconds."""
    return NOW + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("polls", "expected"), [(12, 300), (2, 1800), (1, 3600), (7, 515), (0, 3600)]
)
def test_interval_from_polls(polls: int, expected: int) -> None:
    """The user chooses regular GETs per hour."""
    assert interval_seconds(polls) == expected


@pytest.mark.parametrize(
    ("limit", "expected"), [(None, 8), (0, 8), (20, 8), (24, 8), (25, 9), (60, 20)]
)
def test_reserve_for_limit(limit: int | None, expected: int) -> None:
    """Reserve max(8, ceil(limit / 3)); no larger quota assumed without headers."""
    assert reserve_for_limit(limit) == expected


def test_estimate_without_history_is_due_now() -> None:
    """First GET ever: due now, nominal interval."""
    assert estimate(NOW, None, 300, None, None, 8) == (0.0, 300, None)


def test_estimate_regular_interval_with_enough_quota() -> None:
    """20 limit, 12 per hour: after the GET at :00, 19 left, next at :05."""
    delay, planned, available = estimate(NOW, NOW, 300, 19, at(3600), 8)
    assert (delay, planned, available) == (300, 300, 11)


def test_estimate_other_client_used_quota() -> None:
    """Remaining comes from the headers, so other clients' use counts.

    11 left, reserve 8: only 3 GETs fit until the reset in 30 minutes, so the
    first of them waits until reset - 3 * 300 s - 10 s.
    """
    delay, planned, available = estimate(NOW, NOW, 300, 11, at(1800), 8)
    assert available == 3
    assert delay == 1800 - 900 - 10
    assert planned == 890


def test_estimate_no_gets_left_waits_for_reset() -> None:
    """Nothing above the reserve: wait until the reset plus a guard."""
    delay, _, available = estimate(NOW, NOW, 300, 8, at(1200), 8)
    assert available == 0
    assert delay == 1210


def test_estimate_avoids_reset_boundary() -> None:
    """A GET planned within 10 s before the reset moves to after it."""
    delay, _, _ = estimate(NOW, at(-295), 300, 19, at(8), 8)
    assert delay == 18


def test_estimate_stale_window_is_ignored() -> None:
    """A reset in the past says nothing about the new window."""
    delay, _, _ = estimate(NOW, at(-400), 300, 0, at(-10), 8)
    assert delay == 0


def test_estimate_keeps_minimum_spacing() -> None:
    """Never two GETs within 61 s, even with a short interval."""
    delay, _, _ = estimate(NOW, at(-10), 30, None, None, 8)
    assert delay == 51


def test_plan_spacing_after_command() -> None:
    """A command does not reset the GET clock but keeps the 61 s spacing."""
    state = QuotaState(last_get_at=at(-299), last_request_at=at(-5))
    assert plan_next_get(state, NOW, 12).delay == 56


def test_plan_extra_requests_shrink_the_reserve() -> None:
    """Commands already spent come out of the reserve, not the regular GETs."""
    base = QuotaState(limit=20, remaining=11, reset_at=at(1800), last_get_at=NOW)
    plan = plan_next_get(base, NOW, 12)
    assert (plan.reserve, plan.available_gets) == (8, 3)

    spent = base
    for seconds in (-60, -30):
        spent = spent.with_extra_request(at(seconds))
    spent = QuotaState(
        limit=20,
        remaining=11,
        reset_at=at(1800),
        last_get_at=NOW,
        extra_requests=spent.extra_requests,
    )
    plan = plan_next_get(spent, NOW, 12)
    assert (plan.extra_spent, plan.reserve, plan.available_gets) == (2, 6, 5)
    assert plan.delay == 300


def test_extra_requests_outside_window_do_not_count() -> None:
    """Only requests in the current hourly window are counted."""
    state = QuotaState(reset_at=at(600), extra_requests=(at(-3500), at(-100)))
    assert state.extra_spent(NOW) == 1
    assert QuotaState(extra_requests=(at(-100),)).extra_spent(NOW) == 1
    assert QuotaState(reset_at=at(-1), extra_requests=(at(-100),)).extra_spent(NOW) == 0
    # Old entries are dropped when a new one is recorded.
    assert QuotaState(extra_requests=(at(-4000),)).with_extra_request(
        NOW
    ).extra_requests == (NOW,)


def test_retry_after_wins_and_survives() -> None:
    """After 429 nothing is planned before Retry-After plus 5 s."""
    state = QuotaState().with_rate_limit(NOW, NOW, 900, at(600))
    assert state.remaining == 0
    assert state.retry_delay(NOW) == 905
    assert plan_next_get(state, NOW, 12).delay == 905
    # Restored from the store, the cooldown still holds.
    restored = QuotaState.from_dict(state.as_dict())
    assert restored.retry_delay(at(100)) == 805
    assert restored.retry_delay(at(1000)) == 0


def test_retry_without_retry_after_uses_reset_or_fails_closed() -> None:
    """Without Retry-After: the reset plus 5 s, else 15 minutes."""
    assert QuotaState().with_rate_limit(NOW, NOW, None, at(300)).retry_delay(NOW) == 305
    assert QuotaState().with_rate_limit(NOW, NOW, None, None).retry_delay(NOW) == 900


def test_successful_get_clears_429_and_keeps_known_headers() -> None:
    """A 200 ends the cooldown; missing headers keep the last known values."""
    state = QuotaState(limit=20).with_rate_limit(NOW, NOW, 60, at(600))
    after = state.with_response(at(70), None, 15, None)
    assert after.retry_delay(at(70)) == 0
    assert (after.limit, after.remaining, after.reset_at) == (20, 15, at(600))
    failed = after.with_failed_get(at(400))
    assert failed.last_get_at == at(400)
    assert failed.remaining == 15


def test_regular_gets_until_reset() -> None:
    """Counts the regular GETs still planned in this window."""
    assert regular_gets_until_reset(QuotaState(), NOW, 12) == 0
    state = QuotaState(remaining=19, reset_at=at(3600), last_get_at=NOW)
    assert regular_gets_until_reset(state, NOW, 12) == 11
    assert regular_gets_until_reset(state, NOW, 2) == 1
    no_history = QuotaState(remaining=6, reset_at=at(1800))
    assert regular_gets_until_reset(no_history, NOW, 12) == 6
    late = QuotaState(remaining=6, reset_at=at(200), last_get_at=NOW)
    assert regular_gets_until_reset(late, NOW, 12) == 0


def test_golden_reserve() -> None:
    """At :00 after a GET (19 left, 11 regular GETs to go) at most 7 extra.

    1 GET + 11 regular + 7 extra + 1 golden = 20.
    """
    state = QuotaState(limit=20, remaining=19, reset_at=at(3600), last_get_at=NOW)
    assert can_spend(state, NOW, 12, cost=7)
    assert not can_spend(state, NOW, 12, cost=8)
    # Remaining 13: 11 regular + 1 golden leaves room for exactly one.
    tight = QuotaState(limit=20, remaining=13, reset_at=at(3600), last_get_at=NOW)
    assert can_spend(tight, NOW, 12)
    assert not can_spend(tight, NOW, 12, cost=2)
    assert not can_spend(tight.with_extra_request(NOW), NOW, 12)


def test_can_spend_needs_a_current_window_and_no_429() -> None:
    """Unknown or stale quota and an active 429 refuse extra requests."""
    assert not can_spend(QuotaState(), NOW, 12)
    stale = QuotaState(remaining=19, reset_at=at(-1), last_get_at=at(-100))
    assert not can_spend(stale, NOW, 12)
    limited = QuotaState(remaining=19, reset_at=at(3000)).with_rate_limit(
        NOW, NOW, 60, at(3000)
    )
    assert not can_spend(limited, at(10), 12)


def test_state_round_trip_and_invalid_values() -> None:
    """Stored form is JSON-safe; damaged values become unknown."""
    state = QuotaState(
        limit=20,
        remaining=11,
        reset_at=at(1800),
        last_get_at=NOW,
        last_request_at=NOW,
        extra_requests=(at(-10),),
    )
    assert QuotaState.from_dict(state.as_dict()) == state
    assert QuotaState.from_dict(None) == QuotaState()
    damaged = QuotaState.from_dict(
        {
            "limit": True,
            "remaining": -1,
            "reset_at": "not a date",
            "last_get_at": "2026-11-23T10:00:00",  # no time zone
            "retry_after": "60",
            "extra_requests": ["bad", NOW.isoformat(), 5],
        }
    )
    assert damaged == QuotaState(extra_requests=(NOW,))
    assert QuotaState.from_dict({"extra_requests": "x"}).extra_requests == ()


def test_can_spend_stop_may_use_golden_reserve() -> None:
    """Charging STOP may take the last request; the regular GETs stay covered."""
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    base = QuotaState(
        limit=20,
        remaining=12,
        reset_at=now + timedelta(minutes=60),
        last_get_at=now - timedelta(minutes=1),
    )
    # 12 regular GETs until the reset: STOP needs 12 left after it.
    assert not can_spend(base, now, 12)
    assert not can_spend_stop(base, now, 12)
    more = QuotaState(**{**_fields(base), "remaining": 13})
    assert not can_spend(more, now, 12)
    assert can_spend_stop(more, now, 12)
    # A window that already reset does not block STOP (the new one is full).
    stale = QuotaState(**{**_fields(base), "reset_at": now - timedelta(seconds=1)})
    assert can_spend_stop(stale, now, 12)
    # Never during 429, never without any quota known.
    limited = QuotaState(**{**_fields(more), "rate_limited_at": now, "retry_after": 60})
    assert not can_spend_stop(limited, now, 12)
    assert not can_spend_stop(QuotaState(), now, 12)
    assert not can_spend_stop(QuotaState(remaining=5), now, 12)


def _fields(state: QuotaState) -> dict:
    return {name: getattr(state, name) for name in state.__slots__}
