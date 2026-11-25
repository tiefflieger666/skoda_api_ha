"""Rules for a new API key (candidate).

The candidate from the reconfigure flow is tried on the next regular GET, so
the key change costs no extra request. It replaces the active key only if the
answer is for the same VIN and the key is valid longer than the active key.
No Home Assistant imports.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class CandidateOutcome(StrEnum):
    """What happens with the candidate after its trial GET."""

    ACTIVATE = "activate"
    # The answer cannot decide it yet (missing header or VIN): try again on
    # the next regular GET.
    WAIT = "wait"
    # Rejected; the value is the issue translation key suffix.
    REJECT_AUTH = "auth"
    REJECT_VIN_MISMATCH = "vin_mismatch"
    REJECT_EXPIRED = "expired"
    REJECT_SHORTER_VALIDITY = "shorter_validity"


def judge_candidate(
    *,
    vin: str,
    answered_vin: str | None,
    new_expiry: datetime | None,
    active_expiry: datetime | None,
    now: datetime,
) -> CandidateOutcome:
    """Decide on a candidate after a successful (200) trial GET.

    An unknown expiry of the active key does not block the change: the
    candidate works for this VIN and is not expired, so it is accepted
    (waiting here could leave the change stuck forever).
    """
    if answered_vin is None:
        return CandidateOutcome.WAIT
    if answered_vin != vin:
        return CandidateOutcome.REJECT_VIN_MISMATCH
    if new_expiry is None:
        return CandidateOutcome.WAIT
    if new_expiry <= now:
        return CandidateOutcome.REJECT_EXPIRED
    if active_expiry is not None and new_expiry <= active_expiry:
        # The same expiry is not an improvement either.
        return CandidateOutcome.REJECT_SHORTER_VALIDITY
    return CandidateOutcome.ACTIVATE
