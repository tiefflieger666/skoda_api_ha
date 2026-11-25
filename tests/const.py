"""Fake test data. Never real keys or VINs."""

from custom_components.skoda_api_ha.api import API_BASE_URL

VIN = "TMBTEST0000000001"
OTHER_VIN = "TMBTEST0000000002"
API_KEY = "test-key-not-real"
NEW_API_KEY = "test-key-new-not-real"
VEHICLE_URL = f"{API_BASE_URL}/api/v1/vehicles/{VIN}"

QUOTA_HEADERS = {
    "RateLimit-Limit": "20",
    "RateLimit-Remaining": "11",
    "RateLimit-Reset": "1800",
    "X-API-Key-Expires-At": "2027-05-01T00:00:00Z",
}


def vehicle_body(
    vin: str = VIN,
    name: str | None = "FICTION CAR",
    plate: str | None = "XX TEST-01",
) -> dict:
    """Minimal GET /api/v1/vehicles/{vin} body (shape as in the Swagger spec)."""
    vehicle: dict = {"vin": vin, "odometer": {"mileageInKm": 1234}}
    if name is not None:
        vehicle["name"] = name
    if plate is not None:
        vehicle["licensePlate"] = plate
    return {"vehicle": vehicle, "errors": []}


ALL_OPERATIONS = (
    "startCharging",
    "stopCharging",
    "startAirConditioning",
    "stopAirConditioning",
    "setChargeMode",
    "setChargingLimit",
    "updateChargingProfile",
)


def profile(pid: int = 4) -> dict:
    """Location profile, shape as reported by the Elroq (fictional name)."""
    return {
        "id": pid,
        "name": "FICTION HOME",
        "settings": {
            "autoUnlockPlugWhenCharged": "PERMANENT",
            "maxChargingCurrent": "REDUCED",
            "minBatteryStateOfCharge": {"minimumBatteryStateOfChargeInPercent": 0},
            "targetStateOfChargeInPercent": 80,
        },
        "preferredChargingTimes": [
            {"id": 13, "enabled": False, "startTime": "14:01", "endTime": "15:59"},
            {"id": 14, "enabled": True, "startTime": "22:00", "endTime": "06:00"},
        ],
        "timers": [
            {
                "id": 10,
                "enabled": False,
                "type": "ONE_OFF",
                "oneOffDay": "MONDAY",
                "time": "04:00",
            }
        ],
    }


def command_vehicle(
    *,
    captured: str = "2026-11-23T09:59:00+00:00",
    charging_state: str = "READY_FOR_CHARGING",
    climate_state: str = "OFF",
    operations: tuple[str, ...] = ALL_OPERATIONS,
) -> dict:
    """Vehicle with the sections the commands use (Swagger shape, fake data)."""
    return {
        "vin": VIN,
        "name": "FICTION CAR",
        "operations": [{"name": op} for op in operations],
        "charging": {
            "carCapturedTimestamp": captured,
            "status": {"state": charging_state},
            "settings": {
                "availableChargeModes": ["MANUAL", "PREFERRED_CHARGING_TIMES"],
                "preferredChargeMode": "MANUAL",
                "targetStateOfChargeInPercent": 80,
            },
        },
        "airConditioning": {
            "carCapturedTimestamp": captured,
            "state": climate_state,
            "targetTemperature": {"value": 22.0, "unit": "CELSIUS"},
            "airConditioningWithoutExternalPower": False,
        },
        "chargingProfiles": {
            "profiles": [profile(1), profile(4)],
            "currentVehiclePositionProfile": {"id": 4, "name": "FICTION HOME"},
        },
    }


RENDER_URL = "https://render.example.com/fiction-car.png"


def full_vehicle() -> dict:
    """Vehicle with every section the entities read (Swagger shape, fake data)."""
    captured = "2026-11-23T09:55:00Z"
    return {
        "vin": VIN,
        "name": "FICTION CAR",
        "licensePlate": "XX TEST-01",
        "renderUrl": RENDER_URL,
        "status": {
            "overall": {
                "doorsLocked": "YES",
                "locked": "YES",
                "doors": "CLOSED",
                "windows": "CLOSED",
                "lights": "OFF",
                "reliableLockStatus": "LOCKED",
            },
            "detail": {"sunroof": "UNSUPPORTED", "trunk": "CLOSED", "bonnet": "CLOSED"},
            "carCapturedTimestamp": captured,
        },
        "odometer": {"mileageInKm": 12345, "carCapturedTimestamp": captured},
        "parkingPosition": {
            "state": "PARKED",
            "gpsCoordinates": {"latitude": 46.0, "longitude": 14.5},
            "formattedAddress": "Fiction Street 1, 1000 Nowhere",
            "carCapturedTimestamp": captured,
        },
        "airConditioning": {
            "state": "OFF",
            "targetTemperature": {"value": 21.5, "unit": "CELSIUS"},
            "estimatedReachOfTargetTemperatureAt": "2026-11-23T10:20:00Z",
            "airConditioningWithoutExternalPower": True,
            "airConditioningAtUnlock": False,
            "windowHeating": {"enabled": False, "front": "OFF", "rear": "OFF"},
            "carCapturedTimestamp": captured,
        },
        "charging": {
            "isVehicleInSavedLocation": True,
            "status": {
                "chargingRateInKilometersPerHour": 42.5,
                "chargePowerInKw": 7.2,
                "remainingTimeToFullyChargedInMinutes": 95,
                "fullyChargedAt": "2026-11-23T11:30:00Z",
                "state": "CHARGING",
                "chargeType": "AC",
                "plugConnectionState": "CONNECTED",
                "plugLockState": "LOCKED",
                "battery": {
                    "remainingCruisingRangeInMeters": 287000,
                    "stateOfChargeInPercent": 64,
                },
            },
            "settings": {
                "targetStateOfChargeInPercent": 80,
                "batteryCareModeTargetValueInPercent": 80,
                "preferredChargeMode": "MANUAL",
                "availableChargeModes": ["MANUAL", "PREFERRED_CHARGING_TIMES"],
                "chargingCareMode": "ACTIVATED",
                "autoUnlockPlugWhenCharged": "PERMANENT",
                "maxChargeCurrentAc": "REDUCED",
                "maxChargeCurrentAcAmpere": 13,
            },
            "carCapturedTimestamp": captured,
        },
        "chargingProfiles": {
            "profiles": [profile(4)],
            "currentVehiclePositionProfile": {
                "id": 4,
                "name": "FICTION HOME",
                "targetStateOfChargeInPercent": 80,
                "nextChargingTime": "22:00",
            },
            "carCapturedTimestamp": captured,
        },
        "operations": [{"name": op} for op in ALL_OPERATIONS],
    }
