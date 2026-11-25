"""Async client for the official MyŠkoda Public API.

This module has no Home Assistant imports so that it can later move into a
separate library on PyPI. The caller passes in an ``aiohttp`` session.

Spec: https://public.api.connect.skoda-auto.cz/docs
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any

import aiohttp

API_BASE_URL = "https://public.api.connect.skoda-auto.cz"
VEHICLE_PATH = "/api/v1/vehicles/{vin}"
REQUEST_TIMEOUT = 25


class SkodaApiError(Exception):
    """Base error of the Škoda API client."""


class SkodaConnectionError(SkodaApiError):
    """Network error, timeout or server error (5xx). Temporary."""


class SkodaAuthError(SkodaApiError):
    """401: the API key is invalid or expired."""


class SkodaForbiddenError(SkodaAuthError):
    """403: the API key does not cover this vehicle."""


class SkodaRateLimitError(SkodaApiError):
    """429: the per-VIN quota is used up."""

    def __init__(self, retry_after: int | None, quota: QuotaInfo | None = None) -> None:
        """Store ``Retry-After`` (seconds, may be unknown) and the quota headers."""
        super().__init__("Rate limited")
        self.retry_after = retry_after
        self.quota = quota if quota is not None else QuotaInfo(retry_after=retry_after)


class SkodaVehicleBusyError(SkodaApiError):
    """429 ``vehicle-not-accepting-requests``: the vehicle declined, not the quota.

    The vehicle limits how often it takes requests (or its battery is low).
    The request still counts against the quota, so the ``RateLimit-*``
    headers are kept, but no quota cooldown follows.
    """

    def __init__(self, quota: QuotaInfo) -> None:
        """Store the quota headers of the answer."""
        super().__init__(VEHICLE_BUSY)
        self.quota = quota


class SkodaResponseError(SkodaApiError):
    """Any other unexpected response (4xx, invalid JSON, wrong VIN)."""


class SkodaVinMismatchError(SkodaResponseError):
    """200, but the answer is for another vehicle than the one requested."""


class SkodaCommandUncertainError(SkodaConnectionError):
    """A command got no clear answer (network, timeout, 5xx).

    The vehicle may have executed it, so it is never sent again on its own.
    """


class SkodaCommandRejectedError(SkodaResponseError):
    """A command was answered with another 4xx (problem type, if any)."""

    def __init__(self, status: int, problem: str | None) -> None:
        """Store the HTTP status and the short problem type."""
        super().__init__(f"HTTP {status}")
        self.status = status
        self.problem = problem


@dataclass(frozen=True, slots=True)
class QuotaInfo:
    """Quota and key information from the response headers."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    retry_after: int | None = None
    key_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CommandResponse:
    """Answer to an accepted command (202 = accepted, not executed)."""

    status: int
    quota: QuotaInfo = field(default_factory=QuotaInfo)


@dataclass(frozen=True, slots=True)
class VehicleResponse:
    """Body of ``GET /api/v1/vehicles/{vin}`` plus quota headers."""

    vehicle: dict[str, Any]
    errors: list[Any] = field(default_factory=list)
    quota: QuotaInfo = field(default_factory=QuotaInfo)

    @property
    def vin(self) -> str | None:
        """VIN reported by the API."""
        return _text(self.vehicle.get("vin"))

    @property
    def name(self) -> str | None:
        """Vehicle name chosen in the MyŠkoda app."""
        return _text(self.vehicle.get("name"))

    @property
    def license_plate(self) -> str | None:
        """License plate, if the owner entered one."""
        return _text(self.vehicle.get("licensePlate"))


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _parse_int(value: str | None) -> int | None:
    try:
        number = int(value) if value is not None else None
    except ValueError:
        return None
    return number if number is not None and number >= 0 else None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def parse_quota(headers: Any, now: datetime) -> QuotaInfo:
    """Read the quota headers. Missing or invalid values become ``None``.

    ``RateLimit-Reset`` is seconds until reset. ``Retry-After`` may be
    delay-seconds or an HTTP date.
    """
    reset = _parse_int(headers.get("RateLimit-Reset"))
    retry_raw = headers.get("Retry-After")
    retry_after = _parse_int(retry_raw)
    if retry_after is None and (deadline := _parse_datetime(retry_raw)):
        retry_after = max(0, math.ceil((deadline - now).total_seconds()))
    return QuotaInfo(
        limit=_parse_int(headers.get("RateLimit-Limit")),
        remaining=_parse_int(headers.get("RateLimit-Remaining")),
        reset_at=now + timedelta(seconds=reset) if reset is not None else None,
        retry_after=retry_after,
        key_expires_at=_parse_datetime(headers.get("X-API-Key-Expires-At")),
    )


# Problem type of a 429 that the vehicle, not the quota, caused (Swagger 1.1.0).
VEHICLE_BUSY = "vehicle-not-accepting-requests"


async def _problem_type(resp: aiohttp.ClientResponse) -> str | None:
    """Last part of the problem ``type`` (RFC 9457) of an error body, if any."""
    try:
        detail = await resp.json(content_type=None)
    except ValueError, aiohttp.ClientError:
        return None
    kind = detail.get("type") if isinstance(detail, dict) else None
    if not isinstance(kind, str) or not kind:
        return None
    return kind.rsplit("/", 1)[-1][:80]


class SkodaApiClient:
    """Minimal client. One method call is exactly one HTTP request."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str = API_BASE_URL,
    ) -> None:
        """Create the client. The key is only ever sent as a header."""
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        """Never include the key."""
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    def uses_key(self, api_key: str) -> bool:
        """Return True when this client sends ``api_key`` (without exposing it)."""
        return self._api_key == api_key

    def set_api_key(self, api_key: str) -> None:
        """Use another key for all following requests."""
        self._api_key = api_key

    def with_api_key(self, api_key: str) -> SkodaApiClient:
        """Return a client with the same session and another key (key trial)."""
        return SkodaApiClient(self._session, api_key, self._base_url)

    async def async_send_command(
        self,
        vin: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> CommandResponse:
        """Send exactly one command (POST or PUT). No retry, ever.

        ``path`` is below ``/api/v1/vehicles/{vin}`` (from ``commands.py``).
        A 2xx answer means the API accepted the command; whether the vehicle
        executed it shows only in a later GET.
        """
        if method not in {"POST", "PUT"} or not path.startswith("/"):
            raise ValueError("Unsupported command request")
        url = self._base_url + VEHICLE_PATH.format(vin=vin) + path
        headers = {"X-API-Key": self._api_key, "Accept": "application/json"}
        problem: str | None = None
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                quota = parse_quota(resp.headers, datetime.now(UTC))
                status = resp.status
                if HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR:
                    problem = await _problem_type(resp)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SkodaCommandUncertainError(type(err).__name__) from err

        if status == HTTPStatus.UNAUTHORIZED:
            raise SkodaAuthError("401")
        if status == HTTPStatus.FORBIDDEN:
            raise SkodaForbiddenError("403")
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            if problem == VEHICLE_BUSY:
                raise SkodaVehicleBusyError(quota)
            raise SkodaRateLimitError(quota.retry_after, quota)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise SkodaCommandUncertainError(f"HTTP {status}")
        if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            raise SkodaCommandRejectedError(status, problem)
        return CommandResponse(status=status, quota=quota)

    async def async_get_vehicle(self, vin: str) -> VehicleResponse:
        """Fetch all vehicle data with a single ``GET`` (no ``include``)."""
        url = self._base_url + VEHICLE_PATH.format(vin=vin)
        headers = {"X-API-Key": self._api_key, "Accept": "application/json"}
        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                quota = parse_quota(resp.headers, datetime.now(UTC))
                status = resp.status
                body: Any = None
                problem: str | None = None
                if status == HTTPStatus.OK:
                    body = await resp.json(content_type=None)
                elif status == HTTPStatus.TOO_MANY_REQUESTS:
                    problem = await _problem_type(resp)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SkodaConnectionError(type(err).__name__) from err
        except ValueError as err:
            raise SkodaResponseError("Invalid JSON") from err

        if status == HTTPStatus.UNAUTHORIZED:
            raise SkodaAuthError("401")
        if status == HTTPStatus.FORBIDDEN:
            raise SkodaForbiddenError("403")
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            if problem == VEHICLE_BUSY:
                raise SkodaVehicleBusyError(quota)
            raise SkodaRateLimitError(quota.retry_after, quota)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise SkodaConnectionError(f"HTTP {status}")
        if status != HTTPStatus.OK:
            raise SkodaResponseError(f"HTTP {status}")

        if not isinstance(body, dict) or not isinstance(body.get("vehicle"), dict):
            raise SkodaResponseError("Unexpected body")
        errors = body.get("errors")
        result = VehicleResponse(
            vehicle=body["vehicle"],
            errors=errors if isinstance(errors, list) else [],
            quota=quota,
        )
        if result.vin is not None and result.vin != vin:
            raise SkodaVinMismatchError("VIN mismatch")
        return result


__all__ = [
    "CommandResponse",
    "QuotaInfo",
    "SkodaApiClient",
    "SkodaApiError",
    "SkodaAuthError",
    "SkodaCommandRejectedError",
    "SkodaCommandUncertainError",
    "SkodaConnectionError",
    "SkodaForbiddenError",
    "SkodaRateLimitError",
    "SkodaResponseError",
    "SkodaVehicleBusyError",
    "SkodaVinMismatchError",
    "VehicleResponse",
]
