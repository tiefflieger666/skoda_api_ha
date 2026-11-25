# Škoda Public API for Home Assistant

Custom integration for [Home Assistant](https://www.home-assistant.io/) based on the official **MyŠkoda Public API**.

> **Status:** beta. In daily use on the vehicle below; other vehicles are untested. Please report problems with the [bug report form](https://github.com/tiefflieger666/skoda_api_ha/issues/new/choose).

> **Tested vehicle:** Developed and tested on **Škoda Elroq, software version 5.6.4**. Other vehicles and software versions may work but are untested.

> This is an unofficial integration. Škoda and the Škoda logo (used as the integration icon) are trademarks of ŠKODA AUTO a.s.

## Quick start
1. **Install:** HACS (Marketplace) → ⋮ → *Custom repositories* → add `https://github.com/tiefflieger666/skoda_api_ha` (type *Integration*) → download **Škoda Public API** → restart Home Assistant. Or copy `custom_components/skoda_api_ha` to `/config/custom_components/` ([details](#installation)).
2. **Get an API key:** MyŠkoda app → **Profile → Smart home** → create a key for your vehicle and copy it (it is shown only once). Škoda Connect must be active.
3. **Add the vehicle:** Settings → Devices & services → **Add integration** → *Škoda API* → enter the API key and the VIN.
4. **Optional, own charger (wallbox):** if Home Assistant has a sensor with the current power of your home charger (W or kW), select it as **Charger power sensor (optional)**, either when adding the vehicle or later under Settings → Devices & services → Škoda API → **Configure**. It confirms start and stop of charging within about 20 seconds and without extra API requests. If your charger's standby or charging power is unusual, adjust **Charger power: charging from / idle up to** (defaults 500 W and 10 W) in the same form; without it the next regular update confirms them ([details](#best-practice-charging-at-home)).
5. **Done.** The vehicle updates every 5 minutes (12 per hour) and 8 requests per hour stay free for commands. Change this under **Configure** if needed ([options](#options)).

> **⚠️ Only 20 API requests per hour per vehicle**, shared with every other app or integration using the same vehicle. Do not run a second Škoda integration for the same car.

## Contents
[Features](#features) · [Supported vehicles](#supported-vehicles) · [Requirements](#requirements) · [Installation](#installation) · [Configuration](#configuration) · [Entities](#entities) · [API quota and polling](#api-quota-and-polling) · [Commands](#commands) · [Examples](#examples) · [Languages](#languages) · [Limitations](#limitations) · [Troubleshooting](#troubleshooting) · [Removal](#removal) · [Security](#security) · [License](#license)

## Features
- **Vehicle data:** battery, range, odometer, charging (state, power, rate, time, limit, profile), climate, doors, windows, locks, lights, parking position (map) and a picture of the vehicle. See [Entities](#entities).
- **Controls:** start and stop charging, air conditioning with target temperature, charge mode, charge limit and the charging profile of the current location (reduced current, target and minimum charge, cable unlock, preferred charging times). Only the operations your vehicle supports are created.
- **Quota aware:** the API allows 20 requests per hour per vehicle. The integration plans every request from the quota headers, keeps a reserve for commands and always leaves one request unused, except for stopping charging (see [API quota and polling](#api-quota-and-polling)). A restart of Home Assistant uses no request.
- **Reliable commands:** each command is sent exactly once and shown as *pending* (hourglass icon) until a later update confirms it. Start and stop of charging can also be confirmed by your own charger power sensor, without any extra request.
- **Key management:** reauthentication, key change before expiry without an extra request, a sensor with the expiry date of the key.
- **Home Assistant standards:** UI setup only (config flow), diagnostics download without personal data, repair messages, English and German translations.

### Built-in Škoda integration
Home Assistant 2026.11 includes a built-in [Škoda](https://www.home-assistant.io/integrations/skoda/) integration on the same public API. It is read-only (no charging or climate control). This integration adds the controls, the quota planning and command confirmation. **Do not use both for the same vehicle:** they share the same 20 requests per hour.

## Supported vehicles
Vehicles that are available in the MyŠkoda app with an active **Škoda Connect** service. **Available sensors depend on the vehicle's model, powertrain, equipment, and the validity of Škoda Connect licenses.** The integration creates only the entities for data the API reports for your vehicle, and only the controls for operations it lists for your vehicle. When a Škoda Connect service expires, the related values are no longer reported (renew the service in the Škoda Connect Shop).

Developed and tested on the Škoda Elroq (electric). Other models, including combustion and plug-in hybrid vehicles, are untested; fuel values are not supported yet. Reports with the diagnostics download are welcome.

## Requirements
- Home Assistant **2026.10** or newer.
- **Škoda Connect** active in the MyŠkoda app, with the vehicle added to your account.
- A **MyŠkoda Public API key** (`X-API-Key`), created in the MyŠkoda app under **Profile → Smart home**, one per vehicle. The app shows the key only once, so copy it right away. The key expires; the integration shows the expiry date (*API key expires*) and lets you replace it (see [Replacing the API key](#replacing-the-api-key)). API documentation: <https://public.api.connect.skoda-auto.cz/docs>.
- The **VIN** of the vehicle (17 characters).

## Installation
### HACS / Marketplace
1. Open HACS (Marketplace) → ⋮ → **Custom repositories**, add `https://github.com/tiefflieger666/skoda_api_ha` with the type **Integration**.
2. Search for **Škoda Public API** and download it.
3. Restart Home Assistant.
4. Continue with [Configuration](#configuration).

### Manual
1. Download the latest release (or **Code → Download ZIP**) from GitHub.
2. Copy the folder `custom_components/skoda_api_ha` into `/config/custom_components/` of your Home Assistant (for example with the Samba share, File editor or Studio Code Server add-on). The result is `/config/custom_components/skoda_api_ha/manifest.json`.
3. Restart Home Assistant.
4. Continue with [Configuration](#configuration).

To update manually, replace the folder and restart Home Assistant. Your settings and entities stay.

## Configuration
Settings → Devices & services → Add integration → **Škoda API**.

| Field | Description |
|---|---|
| API key | The key (`X-API-Key`) from the MyŠkoda app (Profile → Smart home). |
| VIN | Vehicle identification number (17 characters). |
| Device name | What the device name is based on: **vehicle name** (default), **license plate** or **VIN**. The license plate and the VIN are private data. |
| Charger power sensor (optional) | Only if you have one: a Home Assistant sensor with the current power of your own charger (wallbox) in W. It confirms the start and stop of charging faster and without extra requests. Leave it empty if you have no such sensor; everything works without it, and commands are then confirmed by the Škoda API (next regular update). |

Checking the key uses one request from the hourly API quota of the vehicle.

### Options
| Option | Default | Description |
|---|---|---|
| Device name | vehicle name | Can be changed at any time. Only the display names change; existing `entity_id`s stay as they were created, and `unique_id`s are always based on the VIN. Saving the options does not use any API request. |
| Regular updates per hour | 12 | 0 to 20. With 12 (every 5 minutes), 8 requests per hour stay in reserve for commands and manual refreshes. 0: no regular updates (only the Refresh button and commands; a restart makes no request). **From 19 per hour there is no quota left for commands** (a repair warning says so). |
| Confirmation request after a command | off | Off: the next regular update (or the charger power sensor, if set) confirms the command, with no extra request. On: one extra request after a command. It is skipped if the reserve would run out; then the next regular update (or the charger power sensor, if set) confirms the command. |
| Charger power sensor | empty | Optional. Can be added, changed or removed at any time. |
| Charger power: charging from (W) | 500 | Only with a charger power sensor. A start of charging is confirmed when the charger delivers at least this power for 20 seconds. Choose a value your charger reliably exceeds while charging. |
| Charger power: idle up to (W) | 10 | Only with a charger power sensor. A stop of charging is confirmed when the power stays at or below this value for 20 seconds. Choose a value above your charger's standby power; it must be lower than the charging threshold. |

### Best practice: charging at home
The integration does not copy your charger power sensor into the Škoda device: it is your wallbox's entity, and a copy would only duplicate it. For automations (for example a charging log) create your own binary sensor with the built-in **Threshold** helper instead:

1. Settings → Devices & services → Helpers → Create helper → **Threshold**.
2. Entity: your charger power sensor. Upper limit: a value you choose (for example the same value as *Charger power: charging from*). Name, for example, *External charging home*.
3. The helper is on while the charger delivers more than the limit. Use it in your automations; it reacts faster than the vehicle data from the API (a few minutes delay).

For confirming commands, select the **power sensor** itself (not the binary sensor) as the optional *Charger power sensor*.

### Replacing the API key
- **Reconfigure** (before the key expires): Settings → Devices & services → Škoda API → ⋮ → Reconfigure. Enter the new key. It is saved as a candidate and tried on the **next regular update**, so the change uses no extra request. It replaces the current key only if it answers for the same vehicle and is valid longer than the current key (`X-API-Key-Expires-At`). Otherwise the current key stays in use and a repair message (Settings → System → Repairs) says why. Until then nothing changes; the data keeps coming with the current key.
- **Reauthentication** starts automatically when the API rejects the key (HTTP 401/403).

## Entities
One device per vehicle. Entity names are made of the device name and the entity name, for example "Elroq Battery" or "Elroq Charging state". The `unique_id` of every entity is `<VIN>_<entity key>` and never changes; changing the device name later only changes the display names, the `entity_id`s stay as they were created.

Entities are created only for the parts of the API answer the vehicle supports. A value the API leaves out (or reports as `UNKNOWN`) is shown as *unknown*, never as 0 or off. If an update fails, the entities become *unavailable* until the next successful update.

### Sensors
<details>
<summary>All sensors (table)</summary>

| Entity | Key | API field | Notes |
|---|---|---|---|
| Battery | `battery_level` | `charging.status.battery.stateOfChargeInPercent` | % |
| Range | `range` | `charging.status.battery.remainingCruisingRangeInMeters` | shown in km |
| Odometer | `odometer` | `odometer.mileageInKm` | km |
| Charging state | `charging_state` | `charging.status.state` | Connect cable, Charging, Conserving, Ready for charging, Discharging, Charging interrupted |
| Charge type | `charge_type` | `charging.status.chargeType` | AC, DC, Off |
| Charging power | `charging_power` | `charging.status.chargePowerInKw` | kW |
| Charging rate | `charging_rate` | `charging.status.chargingRateInKilometersPerHour` | km/h |
| Remaining charging time | `remaining_charging_time` | `charging.status.remainingTimeToFullyChargedInMinutes` | min |
| Fully charged at | `fully_charged_at` | `charging.status.fullyChargedAt` | timestamp |
| Battery care limit | `battery_care_limit` | `charging.settings.batteryCareModeTargetValueInPercent` | % |
| Maximum charging current | `max_charge_current` | `charging.settings.maxChargeCurrentAcAmpere` | A |
| Climate state | `climate_state` | `airConditioning.state` | Off, Cooling, Heating, Auxiliary heating, Ventilation, Completed |
| Climate target reached at | `climate_target_reached_at` | `airConditioning.estimatedReachOfTargetTemperatureAt` | timestamp |
| Charging profile | `charging_profile` | `chargingProfiles.currentVehiclePositionProfile.name` | profile at the current location |
| Next charging time | `next_charging_time` | `chargingProfiles.currentVehiclePositionProfile.nextChargingTime` | HH:mm, vehicle time |
| Parking address | `parking_address` | `parkingPosition.formattedAddress` | **disabled by default** (private data) |
| … captured at (6×) | `status_captured_at`, `odometer_captured_at`, `parking_position_captured_at`, `climate_captured_at`, `charging_captured_at`, `charging_profiles_captured_at` | `carCapturedTimestamp` of each part | diagnostic, **disabled by default** |

</details>

### Binary sensors
<details>
<summary>All binary sensors (table)</summary>

| Entity | Key | API field | On means |
|---|---|---|---|
| Charging cable | `plug_connected` | `charging.status.plugConnectionState` | connected |
| Charging cable lock | `plug_lock` | `charging.status.plugLockState` | unlocked |
| Battery care mode | `battery_care_mode` | `charging.settings.chargingCareMode` | activated |
| In saved location | `in_saved_location` | `charging.isVehicleInSavedLocation` | at a saved charging location |
| Vehicle lock | `locked` | `status.overall.locked` | unlocked |
| Door lock | `doors_locked` | `status.overall.doorsLocked` | unlocked (also when a door or the trunk is open) |
| Doors | `doors` | `status.overall.doors` | open |
| Windows | `windows` | `status.overall.windows` | open (side windows) |
| Sunroof | `sunroof` | `status.detail.sunroof` | open |
| Trunk | `trunk` | `status.detail.trunk` | open |
| Bonnet | `bonnet` | `status.detail.bonnet` | open |
| Lights | `lights` | `status.overall.lights` | on |
| In motion | `in_motion` | `parkingPosition.state` | driving |
| Window heating front / rear | `window_heating_front`, `window_heating_rear` | `airConditioning.windowHeating` | on |
| Climate at unlock | `climate_at_unlock` | `airConditioning.airConditioningAtUnlock` | enabled |
| Climate without external power | `climate_without_external_power` | `airConditioning.airConditioningWithoutExternalPower` | enabled |

</details>

### Parking position
`device_tracker` with the key `parking_position`. The API sends a position only while the vehicle is parked, so the tracker keeps the last parking position while driving (also across a restart) and moves when the vehicle parks again.

### Vehicle image
`image` with the key `vehicle_image`: the picture of the vehicle from `renderUrl`. The URL comes with every regular update, so it costs **no extra request** to the Škoda API; Home Assistant loads the picture from that URL (without the API key) when it is shown, and again only when the URL changes. Created only if the API sends a picture (or reports it as temporarily unavailable).

### Controls
<details>
<summary>All controls and how they behave</summary>

| Entity | Key | Command | Notes |
|---|---|---|---|
| Charging | `charging` | `startCharging`, `stopCharging` | switch: on = start, off = stop; **unavailable while the cable is unplugged** |
| Start charging, Stop charging | `start_charging`, `stop_charging` | `startCharging`, `stopCharging` | **disabled by default**, see below; buttons: *Start* only with the cable plugged in and not charging, *Stop* only while charging (or while a charging command waits) |
| Refresh | `refresh` | one `GET` | see below |
| Climate | `climate` | `startAirConditioning`, `stopAirConditioning` | off or heat/cool, 15.5–30 °C in steps of 0.5 |
| Charge mode | `charge_mode` | `setChargeMode` | only the modes the vehicle offers |
| Charge limit | `charge_limit` | `setChargingLimit` | 50–100 % in steps of 10 |
| Profile target charge, Profile minimum charge | `profile_target_soc`, `profile_min_soc` | `updateChargingProfile` | 50–100 % and 0–50 %, steps of 10 |
| Reduced current | `reduced_current` | `updateChargingProfile` | on = reduced, off = maximum |
| Unlock cable when charged | `auto_unlock_plug` | `updateChargingProfile` | |
| Charging time 1–4 | `charging_time_1` … `charging_time_4` | `updateChargingProfile` | preferred charging time on/off |
| Charging time 1–4 from / to | `charging_time_1_start` … `charging_time_4_end` | `updateChargingProfile` | start and end of the preferred charging time; a new start keeps the end and vice versa. Named *from* / *to* so they are listed in order on the device page. |

Controls are created only for the operations the vehicle lists.

**Charging switch or buttons:** the **Charging** switch is the main control: it shows whether the vehicle charges and starts or stops it. The **Start charging** and **Stop charging** buttons send the same commands, so they are **disabled by default** to keep the device page clean. Enable them if you prefer buttons (for example on a dashboard or in a script that should only ever start): Settings → Devices & services → Škoda API → device → *+N entities not shown* → select the button → ⚙ → **Enabled**. Installations from before version 0.10.0 keep the buttons enabled; disable them the same way if you do not need them. Profile controls act on the charging profile of the **current location** of the vehicle; a preferred time the profile does not have is *unknown*.

- **Pending commands:** a sent command is shown with an **hourglass icon** (`mdi:timer-sand`) on the control and in the `pending` attribute (for example `on`, `off`, `start`, `stop`, `90`, or the new temperature) until it is confirmed, rejected or times out. On the charging buttons only the pressed button shows the hourglass.
  - **Climate:** the thermostat dialog of Home Assistant does not show the entity icon, so the hourglass is not visible there. Instead, `hvac_action` always comes from the last update, while `hvac_mode` shows the **requested** mode during a pending start or stop (and during a pending temperature change). This is the split Home Assistant intends: the mode is what you want, the action (heating, cooling, off) is what the vehicle does. If the next update does not confirm the command, the mode falls back to the vehicle's state.
  - The **Charging** switch also shows the requested state at once, with the hourglass, and falls back the same way.
  - All other controls (settings) keep the value of the last update until it is confirmed; only the hourglass shows the change.
- **Climate temperature**: the API has no command that only sets the temperature; it travels with the start of the air conditioning.
  - A new temperature is sent at once with a start command (one request from the quota), after a confirmation, whether the air conditioning runs or not. **While it is off, this starts it** (the API has no other way); the message says so. Setting the temperature together with mode *off* only keeps it (no request) for the next start.
  - The confirmation: Home Assistant cannot show a confirmation dialog for an entity action, so the first change answers with a message that names the cost and the quota left (requests left until the reset and how many of them are free for commands). **Set the same temperature again within 60 seconds to send it.** If the quota does not allow a command, the message says so at once and nothing is sent (*No quota – use the official Škoda app*). An automation that changes the temperature must call the action twice.
  - A new temperature is confirmed when an update shows it running with that temperature. Without a value set in Home Assistant the vehicle's last value is sent; without either the field is left out. The kept value survives a restart. Attributes: `vehicle_target_temperature`, `ha_target_temperature` and `temperature_sync` (`in_sync`, `pending_next_start`, `sent_awaiting_vehicle`, `vehicle_differs`, `unknown`). After a stop the Elroq reports its own stored value again, so a difference while the air conditioning is off is expected.
- **Refresh** makes one update now. If the regular update is not due yet, it is an extra request from the reserve and is refused with a message if it would use the last request in reserve (golden reserve) or while the API asks to wait (HTTP 429). It stays usable when the other entities are unavailable.
- **Clear pending commands** (`clear_pending`, button in Configuration): forgets every command waiting for confirmation and removes the hourglasses. It sends no request and is always usable. Use it only if a command clearly will not be confirmed; normally the next update decides.

</details>

### When a control is unavailable (grey)
A control is greyed out (*unavailable*) when it cannot be used. The values are those of the last update; a value the API did not report (*unknown*) never blocks a control, the command checks decide instead.

| Control | Usable when |
|---|---|
| All controls (except below) | The last update succeeded and the **API access** switch is on. |
| **Charging** switch | In addition: the charging cable is plugged in. |
| **Start charging** button | In addition: the cable is plugged in and the vehicle is not charging. |
| **Stop charging** button | In addition: the vehicle is charging, or a charging command is waiting for confirmation (so a start sent by mistake can be stopped at once). |
| **Refresh** | The **API access** switch is on (also after a failed update). |
| **Clear pending commands**, **API access** | Always. |

Some limits make a control refuse a command with a message instead of greying it out:
- **One command per group** while the previous one waits (hourglass): charging, air conditioning, charge mode, charge limit, charging profile. Only *stop* may follow a pending *start*.
- **Quota:** not enough requests left until the reset (golden reserve), or the API asks to wait (HTTP 429).
- **Repetition:** the same command within 25 seconds.

### Diagnostics of the integration
<details>
<summary>Diagnostic sensors and the diagnostics download</summary>

Sensors of the category *Diagnostic*. They stay available when the API fails (that is when they help most) and use no request. Only **Quota remaining**, **Quota reset** (when the quota is refilled) and **API key expires** are enabled by default; enable the others on the device page if needed.

| Entity | Key | Notes |
|---|---|---|
| Quota limit, Quota remaining, Quota reset | `quota_limit`, `quota_remaining`, `quota_reset` | from the `RateLimit-*` headers of the last answer |
| Quota reserve | `quota_reserve` | requests kept for commands and refreshes in this window |
| Regular updates left | `regular_updates_left` | regular updates the quota still allows before the reset |
| Update interval, Effective update interval | `update_interval`, `effective_interval` | chosen and actual time between regular updates (s); unknown without regular updates |
| Next update, Last update | `next_update`, `last_update` | timestamps |
| API errors | `api_errors` | parts of the answer the API reported as unavailable; attribute `types` |
| API key expires | `api_key_expires` | `X-API-Key-Expires-At` |
| API key change | `api_key_change` | *None* or *New key waiting* (reconfigure until the next regular update) |
| Last … command (5×) | `last_command_charging`, `last_command_climate`, `last_command_charge_mode`, `last_command_charge_limit`, `last_command_profile` | Accepted, Confirmed, Not confirmed, Rejected, Uncertain; attributes `command`, `at`, `detail` |

**Download diagnostics** (Settings → Devices & services → Škoda API → ⋮ → Download diagnostics) contains the options, the quota plan, the command state and the last API answer. The API key, the new key, the VIN, the license plate, the vehicle name, the GPS position, the address, the render URL and the charger sensor are removed.

</details>

### Not available in the API
Lock and unlock, wake-up, seat heating, climate timers, service intervals, outside temperature and the software version are not part of the MyŠkoda Public API.

## API quota and polling
> **⚠️ The Škoda API allows only 20 requests per hour per vehicle (VIN). This quota is shared by every app, script or integration that uses the API for the same vehicle with any key. Requests made elsewhere (another Home Assistant, a script, a second integration) use the same 20 requests and leave fewer for this integration, which then updates less often and may refuse commands.**

<details>
<summary>How requests are planned (details)</summary>

- One regular update is exactly one request (`GET /api/v1/vehicles/{vin}`). By default there are 12 regular updates per hour (every 5 minutes); you can choose **0 to 20** in the options.
  - **0:** no regular updates. The data changes only with the Refresh button (one request each) and a restart makes no request.
  - **19 or 20:** the regular updates use (almost) the whole quota. Nothing is left for commands and manual refreshes, so they are refused (stop charging only if a request is left). A repair warning (Settings → System → Repairs) says so while this is set.
- A reserve of `max(8, limit / 3)` requests (8 with a limit of 20) is kept for commands and manual refreshes, but never more than the regular updates leave free (`20 − updates per hour`, so 5 with 15 per hour and 0 with 20).
- After every response the next update is planned again from the `RateLimit-Remaining` and `RateLimit-Reset` headers. If another client used part of the quota, the integration automatically waits longer so that the reserve stays untouched.
- After HTTP 429 no request is sent until `Retry-After` has passed (or the quota resets).
- An HTTP 429 with the problem type `vehicle-not-accepting-requests` comes from the vehicle, not from the quota: the vehicle limits how often it takes requests, or its battery is low. The command is shown as rejected (`Last ... command` sensor, detail `vehicle-not-accepting-requests`) and you can try again later. It does not start the quota wait; the request still counts against the quota.
- **Golden reserve:** a manual refresh (and later a command or a confirmation request) is only sent if enough requests remain for every regular update until the quota resets **plus one** unused request. Otherwise it is skipped without a request.
- Two regular updates are always at least 61 seconds apart; a command waits at least 5 seconds after the previous request.
- **Restart:** the last response, the time of the last request and the quota headers are stored in Home Assistant (`.storage/skoda_api_ha.<entry_id>`, without the API key). A restart of Home Assistant or a reload of the integration shows the stored data and waits for the next planned update; it does not use an extra request.
- **Adding a vehicle:** the request that checks the key is also the first update. Its answer is stored, so the setup right after adding the vehicle makes no request of its own (one request in total).
- **Reauthentication:** the new key is checked with one request. That request counts in the quota and its answer is stored, so the reload after a successful key change does not make another request. A failed check counts as one request from the reserve.
- **Reconfiguration (key change before expiry):** no extra request; the next regular update tries the new key.

</details>

### API access switch
The switch **API access** (device page, Configuration) stops **all** requests of this integration for the vehicle: no regular update, no refresh, no command, no confirmation request, also after a restart. The sensors keep the last data; the buttons, switches, numbers, selects, times and the climate become unavailable (grey). Switching it on again makes no request by itself: the next regular update follows the quota plan. Switching uses no request. Use it, for example, to leave the whole quota to another app for a while.

## Commands
<details>
<summary>Command rules (details)</summary>

Commands are sent from the control entities below. The rules for all of them:

| Command | API operation | Request |
|---|---|---|
| Start / stop charging | `startCharging`, `stopCharging` | `POST /charging/start`, `/charging/stop` |
| Start / stop air conditioning | `startAirConditioning`, `stopAirConditioning` | `POST /air-conditioning/start`, `/air-conditioning/stop` |
| Charge mode | `setChargeMode` | `PUT /charging/mode` (only modes the vehicle lists) |
| Charge limit | `setChargingLimit` | `PUT /charging/limit` (50–100 % in steps of 10) |
| Charging profile of the current location (charging current, target and minimum charge, automatic cable unlock, preferred charging times 1–4) | `updateChargingProfile` | `PUT /charging-profiles/{id}` with the whole profile, one setting changed |

- A command is sent only if the last update lists its operation (`operations`).
- **Exactly one request** per command, never repeated automatically. HTTP 202 means *accepted*, not *executed*: the command is **pending** until an update shows the result (*confirmed*), or shows the opposite or nothing after 30 minutes (*not confirmed*; with fewer updates per hour: two intervals plus 2 minutes). Charging and air conditioning are decided only by data the car captured after the command.
- The same command is sent at most once every 25 seconds, and only one command at a time.
- **No command hangs in pending:** the requested state in any update confirms a command (also data from before the command: then the vehicle was there already, for example *stop* while not charging); the opposite state decides *not confirmed* as soon as the car reports data captured after the command; charging with the cable unplugged confirms a stop and rejects a start; after 30 minutes (or two intervals plus 2 minutes) without a decision the command is *not confirmed*. The **Clear pending commands** button (no request) forgets all pending commands at once; the next update shows the real state.
- **One command per group until it is confirmed:** while a command waits for confirmation (hourglass), no new command of the same group (charging, air conditioning, charge mode, charge limit, charging profile) is sent. **Exception:** a stop may follow a pending start (or a pending temperature change) at once, so a start sent by mistake can be stopped.
- Settings changes (mode, limit, profile) are built from the last update. They need an update that is recent (15 minutes, or one interval plus 5 minutes) and newer than the previous accepted change of the same setting group, so an old answer never undoes a newer change.
- **Golden reserve:** a command is sent only if enough requests remain afterwards for every regular update until the quota resets plus one. The refusal names the requests left and the reset time. **Exception: stop charging** may use that last request; the regular updates until the reset stay covered.
- No clear answer (network error, timeout, HTTP 5xx): the command may have been executed, so it is not repeated. The next update shows the result.
- **Confirmation** without extra requests: the next regular update, or (start/stop charging) the optional **charger power sensor**: start is confirmed when the charger was idle (≤ 10 W) and then delivers ≥ 500 W for 20 seconds; stop when it was charging (≥ 500 W) and then stays ≤ 10 W for 20 seconds. Both thresholds are defaults and can be changed in the [options](#options). The sensor may report in W, kW or MW. In any other case (sensor unavailable, already charging at start) the Škoda API decides.
- **Confirmation request after a command** (option, off by default): one extra update 3 minutes after the command (the API shows a new charging state only after a few minutes). It is skipped if the command is already confirmed or the golden reserve would break.
- The pending commands survive a restart of Home Assistant.

</details>

## Examples
Notify your phone when charging stops unexpectedly (replace the entity IDs with yours):

```yaml
automation:
  - alias: "Car: charging interrupted"
    triggers:
      - trigger: state
        entity_id: sensor.elroq_charging_state
        to: charging_interrupted
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "Charging of the car was interrupted."
```

Charge at home overnight: start at 22:00 when the vehicle is parked at home with the cable plugged in, stop at 06:00.

> **⚠️ Add a notification for a refused command.** Each start and stop is one request from the quota. If not enough requests are left (golden reserve), the API asks to wait (HTTP 429) or the vehicle rejects the command, nothing is charged (or stopped) and the automation would fail silently. The example uses `continue_on_error` and then checks the `pending` attribute of the switch: it is set only when the command was accepted. Without it, notify yourself and use the official Škoda app.

<details>
<summary>Automation YAML</summary>

```yaml
automation:
  - alias: "Car: charge at home overnight"
    triggers:
      - trigger: time
        at: "22:00:00"
        id: start
      - trigger: time
        at: "06:00:00"
        id: stop
    actions:
      - choose:
          - conditions:
              - condition: trigger
                id: start
              - condition: state
                entity_id: device_tracker.elroq_parking_position
                state: home
              - condition: state
                entity_id: binary_sensor.elroq_charging_cable
                state: "on"
              - condition: not
                conditions:
                  - condition: state
                    entity_id: sensor.elroq_charging_state
                    state: charging
            sequence:
              - action: switch.turn_on
                target:
                  entity_id: switch.elroq_charging
                continue_on_error: true
              - if:
                  - condition: template
                    value_template: "{{ state_attr('switch.elroq_charging', 'pending') != 'on' }}"
                then:
                  - action: notify.mobile_app_my_phone
                    data:
                      message: "Charging could NOT be started (no quota or rejected). Use the Škoda app."
          - conditions:
              - condition: trigger
                id: stop
              - condition: state
                entity_id: sensor.elroq_charging_state
                state: charging
            sequence:
              - action: switch.turn_off
                target:
                  entity_id: switch.elroq_charging
                continue_on_error: true
              - if:
                  - condition: template
                    value_template: "{{ state_attr('switch.elroq_charging', 'pending') != 'off' }}"
                then:
                  - action: notify.mobile_app_my_phone
                    data:
                      message: "Charging could NOT be stopped (no quota or rejected). Use the Škoda app."
```

</details>

`home` is the *Home* zone of Home Assistant; the parking position is updated only while the vehicle is parked, which is exactly when this check matters. The vehicle may also stop by itself earlier when it reaches the charge limit. Stop charging may use the last request of the reserve, so a stop is refused only when the quota is really used up.

More ideas: start the air conditioning before a departure time, set the charge limit depending on tomorrow's calendar, or log charging sessions together with your charger power sensor (see [Best practice: charging at home](#best-practice-charging-at-home)). Keep the quota in mind: every command is one request.

## Languages
- English
- German
- Other languages: not yet. Contributions are welcome: copy `custom_components/skoda_api_ha/translations/en.json` to `<language>.json`, translate the values and open a pull request.

**Entity names** follow the **server language** of Home Assistant (Settings → System → General → Language), not the language of your user profile; the profile language changes only the menus and the translated states. After changing the server language, restart Home Assistant. The `entity_id`s stay as they were created (in the server language at that time); only the names change. Names you changed yourself are kept.

## Limitations
- **Tested only on the Škoda Elroq, software version 5.6.4.** Other models may report fields or operations differently. Entities and controls follow what the API reports for your vehicle.
- **20 requests per hour per vehicle**, shared with every other client of the API for the same VIN. The data is therefore up to a few minutes old (every 5 minutes by default), and the vehicle itself reports charging changes with a delay of a few minutes.
- **No lock/unlock, wake-up, horn/flash, trip history, seat heating, climate timers, service intervals or cabin temperature:** these are not part of the MyŠkoda Public API (see [Not available in the API](#not-available-in-the-api)).
- **Commands are not repeated automatically.** HTTP 202 means the vehicle accepted the command, not that it was executed; confirmation follows with a later update.
- **A new climate temperature** is sent with a start command (it also starts the air conditioning when it is off) and needs the same value twice within 60 seconds (Home Assistant cannot show a confirmation dialog for entity actions). An automation must call the action twice.
- **Charging profile controls** act on the profile of the vehicle's current location only; other profiles and their timers are not changed.
- The parking position is updated only while the vehicle is parked.
- **No warning before the key expires yet** (planned as a repair message). Check the *API key expires* sensor.

## Troubleshooting
- **Reauthentication required / HTTP 401 or 403:** the key expired, was revoked or does not cover this VIN. Enter a new key in the reauthentication dialog.
- **Commands are refused with "No quota":** not enough requests are left until the quota resets. Lower *Regular updates per hour*, stop other clients of the API for this vehicle, or use the official Škoda app.
- **Repair "no quota for commands":** you set 19 or 20 regular updates per hour; lower the value to get a reserve for commands.
- **Command rejected with `vehicle-not-accepting-requests`:** the vehicle limits how often it takes commands (or its battery is low). Try again later.
- **Data does not change:** check the *API access* switch, the *Quota remaining* and *Last update* sensors (Diagnostic) and Settings → System → Logs (filter `skoda_api_ha`).
- **Values become unknown or commands are rejected after a while:** check in the MyŠkoda app whether your Škoda Connect services are still active.
- **Lost API key:** the app shows a key only once. Create a new one (Profile → Smart home) and enter it with Reconfigure, or in the reauthentication dialog when the old key no longer works. The device and its entities stay.
- **Reporting a problem:** attach the diagnostics download (Settings → Devices & services → Škoda API → ⋮ → Download diagnostics). It contains no key, VIN, license plate, GPS position or address.

## Removal
1. Settings → Devices & services → **Škoda API** → ⋮ on the vehicle → **Delete**. This removes the device, its entities and the stored state (`.storage/skoda_api_ha.<entry_id>`). Repeat for every vehicle.
2. Remove the integration code: in HACS → Škoda Public API → ⋮ → **Remove**, or (manual installation) delete the folder `/config/custom_components/skoda_api_ha`.
3. Restart Home Assistant.
4. If you no longer use the API key, revoke it in the MyŠkoda app (Profile → Smart home).

## Development
- Tests: `pip install -r requirements_test.txt && pytest --cov` (fake data only, the Škoda API is never called).
- Lint: `pip install -r requirements_lint.txt && ruff check . && ruff format --check .`
- CI runs ruff, pytest (coverage at least 95 %) and hassfest on every pull request, and weekly with the newest Home Assistant test package.

## Security
Please report vulnerabilities privately, see [SECURITY.md](SECURITY.md).

## License
[MIT](LICENSE). This is an unofficial integration and is not affiliated with or endorsed by ŠKODA AUTO a.s.
