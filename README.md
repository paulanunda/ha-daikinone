# Daikin One for Home Assistant (official Open API)

A Home Assistant custom integration for Daikin One thermostats. It uses only
Daikin's documented [Open API](https://www.daikinone.com/openapi/documentation/index.html)
at `integrator-api.daikinskyport.com`. It never asks for the Daikin account
password and has no third-party dependencies.

This is an independent, unofficial project. "Official" describes the API it
talks to, not the integration. See the [disclaimer](#disclaimer).

Built and tested against Home Assistant 2026.10.

## Setup

### 1. Get credentials from the Daikin app

Daikin's documentation describes both steps; the menu is called *home
integration* in the thermostat's mobile app.

1. **Integrator token:** open home integration and choose *get integration
   token*. Accept the terms and enter your password in the app when asked.
2. **API key:** enable the developer menu (iOS: the app's entry in the Settings
   app; Android: tap the home integration page description five times). Then
   open home integration, *developer*, accept the terms, name your application,
   and submit.

Keep both secret. The account email is case sensitive and must be entered
exactly as registered.

### 2. Install

In HACS, add this repository as a custom repository of type *Integration*,
install it, and restart Home Assistant. Or copy `custom_components/daikinone`
into your Home Assistant `config/custom_components` directory.

### 3. Configure

Go to Settings, Devices & services, *Add integration*, and choose *Daikin One
(Open API)*. Enter the email, API key, and integrator token. They are checked
against Daikin before the entry is created.

- **Options:** the polling interval, 3 to 60 minutes (default 3).
- **Reconfigure:** replace the credentials for the same account.
- **Re-authentication:** if Daikin rejects the saved credentials, Home
  Assistant asks for a new API key and integrator token. Polling stops until
  you provide them.

## Entities

Each thermostat is one device with these entities.

| Entity | What it does |
|---|---|
| Climate | Mode (off, heat, cool, heat/cool), setpoints, indoor temperature and humidity, and current action (heating, cooling, drying, fan, idle, off). |
| Indoor temperature, Indoor humidity | Readings from the thermostat. |
| Outdoor temperature, Outdoor humidity | Outdoor readings as the thermostat reports them. |
| Heating setpoint, Cooling setpoint | Both setpoints in every mode, including the one the climate entity hides in heat, cool, and off. |
| Equipment status | What the equipment is doing: heating, cooling, dehumidifying (overcooling), fan, or idle. |
| System fan | The fan setting made at the thermostat, auto or on (read-only). |
| Online | On when the last read succeeded, off when Daikin reports the thermostat offline, unavailable when Daikin itself could not be reached. |
| Heating, Cooling | On while the equipment is heating, or cooling (including overcooling to dehumidify). |
| Fan | On while the blower is moving air: during heating, cooling, or fan-only operation. |
| Geofencing | Whether geofencing is enabled on the thermostat (read-only). |
| Fan circulation mode | Off, always on, or on a schedule. |
| Fan circulation speed | Low, medium, or high. |
| Schedule | Turns the thermostat's own schedule on or off. |

### Climate behavior worth knowing

- **Daikin "auto" is Home Assistant `heat_cool`.** It keeps the temperature
  between a low and a high setpoint. Home Assistant's own `auto` mode is not
  offered.
- **Only modes the thermostat allows are offered**, according to its mode limit.
- **Mode and setpoint in one call.** `climate.set_temperature` with `hvac_mode`
  and `temperature` sends a single request, even when the thermostat is off.
- **The two setpoints stay a minimum distance apart.** The thermostat reports
  that distance (2 °C, 3.6 °F, on a One Touch). If you set a single setpoint
  that comes too close to the hidden one, the hidden one moves, as it does on
  the thermostat itself. If you set both at once and they are too close, or a
  setpoint is outside the thermostat's limits, the call fails with an
  explanation and nothing is sent.
- **Any mode or setpoint change turns the schedule off.** That is how Daikin's
  API works. The Schedule switch shows off immediately; turn it back on to
  hand control back to the thermostat's schedule.
- **Emergency heat** appears as a preset only on thermostats that report it.
- **Temperatures** are exchanged with Daikin in Celsius, in 0.1° steps. Home
  Assistant converts for display, so a setpoint of 72 °F is sent as 22.2 °C.

### When things go wrong

- A value Daikin sends that the integration does not recognize makes that one
  entity *unknown*. Nothing else is affected.
- A thermostat that cannot be read becomes *unavailable* on its own. It never
  shows old or zero values, and the other thermostat carries on.
- A change is refused, with an explanation, when the thermostat is unavailable
  or has not reported the values needed to complete the request.
- Devices and entities are never removed automatically. A thermostat that
  disappears from the account stays, unavailable, until you delete its device
  by hand.

## Known limits of the official API

- **Polling only, every 3 minutes at most.** Changes made at the thermostat can
  take up to one polling interval to appear.
- **Changes take about 15 seconds to confirm.** Home Assistant shows a change
  at once and reads the thermostat back 15 seconds later.
- **At most 3 requests at a time**, and Daikin answers HTTP 429 when the rate
  is exceeded. The integration then waits (3 minutes, doubling up to 30, or
  whatever `Retry-After` says) before trying again.
- **Not available through this API:** equipment telemetry, power usage, air
  quality, schedule editing, humidity targets, away mode (reading or setting),
  and changing geofencing.
- **The system fan setting (`fan`: auto/on) is read-only.** The System fan
  sensor shows it; the API cannot change it.
- **No online flag.** The Online sensor is inferred from read results. If
  Daikin serves cached data for a thermostat that has dropped off Wi-Fi, it
  cannot tell.
- **Fan circulation settings apply to unitary systems only.** VRV and
  single/multi-split equipment ignore them.

`docs/daikin-open-api/contract.json` holds the documented contract the tests
check against, plus what real responses were observed to do differently.

## Disclaimer

- **Not affiliated with Daikin.** This project is not affiliated with,
  endorsed by, or supported by Daikin Comfort Technologies, its affiliates, or
  the Home Assistant project. Daikin, Daikin One, Amana, and Goodman are
  trademarks of their respective owners and are used here only to describe
  compatibility.
- **Daikin's terms apply to you.** To get an integrator token and an API key
  you accept Daikin's Integration Token Terms, B2B Terms, and Open API License
  Terms in the app. Your use of the API through this integration is subject to
  those terms and is your responsibility.
- **It controls real equipment.** Do not rely on it for freeze protection,
  safety, or looking after an unattended property. It depends on Daikin's
  cloud service, which can be unreachable, and Daikin can change or withdraw
  the API at any time.
- **Limited testing.** It has only been tested against a Daikin One Touch
  thermostat (firmware 4.0.17) on a unitary system.
- **No warranty and no guaranteed support.** This is a personal project,
  provided as is under the [MIT License](LICENSE). Report problems in this
  repository's issues, not to Daikin.

## Development

Tools are pinned with [mise](https://mise.jdx.dev).

```sh
mise install          # Python and uv
mise run install      # dependencies
mise run check        # lint, type check, tests (all offline)
mise run live-check   # read-only check against the real API
```

`mise run live-check` needs `DAIKIN_EMAIL`, `DAIKIN_API_KEY`, and
`DAIKIN_INTEGRATOR_TOKEN` in the `[env]` section of a gitignored
`mise.local.toml`. It sends only a token request and reads, masks identifiers,
and refuses to run more than once every 3 minutes.

The API client lives in `custom_components/daikinone/api` and imports nothing
from Home Assistant.
