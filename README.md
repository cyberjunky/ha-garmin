# ha-garmin

Python client for Garmin Connect API, designed for Home Assistant integration.

> **Note:** ha-garmin covers the subset of the Garmin Connect API that the
> [Home Assistant Garmin Connect integration](https://github.com/cyberjunky/home-assistant-garmin_connect)
> needs, with data shaped for its coordinators and sensors. For the full suite of
> Garmin Connect API calls, use [python-garminconnect](https://github.com/cyberjunky/python-garminconnect).

## Features

- **Native Garmin Connect API** integration
- **Fully async** - aiohttp for the data API (session can be injected), curl_cffi `AsyncSession` for login, MFA and token refresh
- **Robust authentication** with multiple login strategies and automatic fallbacks
- **MFA support** with automatic endpoint fallback
- **Token persistence** - save and restore sessions to avoid re-login
- **Automatic token refresh** - proactively refreshes before expiry
- **Retry with backoff** for rate limits (429) and server errors (5xx)
- **Midnight fallback** - automatically uses yesterday's data when today isn't ready yet
- **Coordinator-based fetch** - optimized data fetching for Home Assistant multi-coordinator pattern
- **Data transformations** - automatic unit conversions (seconds→minutes, grams→kg)

## Installation

```bash
pip install ha-garmin
```

Optional: install with improved browser UA generation:

```bash
pip install ha-garmin[ua]
```

## Usage

ha-garmin is fully async: every network call is awaited on the event loop.

- **Data API:** aiohttp. Pass your own `aiohttp.ClientSession` (Home Assistant passes its own); without one the client creates a session, which you release with `await client.close()` or `async with`.
- **Login, MFA and token refresh:** `curl_cffi`'s asyncio `AsyncSession` with TLS impersonation. Garmin's web application firewall only lets browser- or app-like TLS fingerprints through to SSO, which aiohttp cannot produce, so these keep their own short-lived sessions. That also keeps SSO cookies out of a shared cookie jar.

### Upgrading from 0.1.x

0.2.0 is fully async, which changes the API:

- `GarminAuth.login()`, `complete_mfa()` and `logout()` are coroutines: `await` them instead of calling them (or running them in an executor).
- `GarminClient(...)` takes an optional `session=` (`aiohttp.ClientSession`). Without it, close the client with `await client.close()` or use `async with GarminClient(...) as client:`.
- `GarminAuth.cancel_mfa()` (new) abandons a pending MFA login and closes its session without deleting the token file, unlike `logout()`.
- `load_session()` no longer refreshes an expiring token; the client refreshes it before its next request. `load_session()` / `save_session()` are still synchronous file I/O.
- `requests` is no longer a dependency; `aiohttp` is.
- Token files and stored DI tokens from 0.1.x keep working.

### Standalone script

```python
import asyncio

import aiohttp

from ha_garmin import GarminAuth, GarminClient, GarminMFARequired

TOKENS = ".garmin_tokens.json"


async def main() -> None:
    auth = GarminAuth()
    if not auth.load_session(TOKENS):  # file I/O only, no network
        try:
            await auth.login("email@example.com", "password")
        except GarminMFARequired:
            await auth.complete_mfa(input("Enter MFA code: "))
        auth.save_session(TOKENS)

    async with aiohttp.ClientSession() as session:
        # timezone: fallback zone for "today" until the Garmin account's own UTC
        # offset is learned from the first daily summary (defaults to the system zone).
        client = GarminClient(auth, timezone="Europe/Amsterdam", session=session)
        today = client.today()  # Garmin calendar date, independent of the process zone
        core_data     = await client.fetch_core_data(today)      # Steps, HR, sleep, stress
        body_data     = await client.fetch_body_data(today)      # Weight, body composition, fitness age
        activity_data = await client.fetch_activity_data(today)  # Activities, workouts, training calendar
        training_data = await client.fetch_training_data(today)  # HRV, training status
        goals_data    = await client.fetch_goals_data()          # Goals, badges
        gear_data     = await client.fetch_gear_data()           # Gear, alarms, solar, devices


asyncio.run(main())
```

`load_session` and `save_session` do blocking file I/O; inside a running event loop (e.g. Home Assistant) call them in an executor.

### Home Assistant integration

Restore the tokens the config flow stored, and pass Home Assistant's aiohttp session to the client.

```python
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ha_garmin import GarminAuth, GarminClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    auth = GarminAuth()
    auth.di_token = entry.data["token"]
    auth.di_refresh_token = entry.data["refresh_token"]
    auth.di_client_id = entry.data["client_id"]

    client = GarminClient(
        auth,
        timezone=hass.config.time_zone,
        session=async_get_clientsession(hass),
    )
    ...
```

Persist `auth.di_token` / `auth.di_refresh_token` back to the config entry after polls: the library rotates them on refresh.

### Testing locally

The scripts in `scripts/` run the library against a real account, without Home Assistant. Credentials come from `GARMIN_EMAIL` / `GARMIN_PASSWORD` or a prompt; tokens are kept in `.garmin_tokens.json` between runs.

```bash
make fetch         # fetch all data and dump it to .garmin_data_dump.json
make fetch-debug   # same, in asyncio debug mode: logs anything that blocks the event loop >100 ms
make add-data      # interactive write actions (nutrition, hydration, blood pressure, uploads, downloads)
make login-strategies  # try each login strategy on its own (no fallback) and report which work
```

## Coordinator Fetch Methods

Optimized methods that group related API calls for Home Assistant coordinators:

| Method | API Calls | Data Returned |
| ------ | --------- | ------------- |
| `fetch_core_data(latest_heart_rate=False)` | 3+ | Steps, distance, calories, HR, stress, sleep, skin temperature change, body battery, SPO2; with `latest_heart_rate=True` also the latest heart rate sample (re-fetched only after a new watch sync) |
| `fetch_body_data()` | 3 | Weight, BMI, body fat, hydration, fitness age |
| `fetch_activity_data()` | 7+ | Activities, workouts, HR zones, polylines, training calendar (scheduled workouts, with steps and targets for today's and the next one), upcoming events and the plan goal event |
| `fetch_training_data()` | 8+ | Training readiness, status, VO2 Max (running and cycling), heat/altitude acclimation, HRV, lactate, endurance/hill scores, power-to-weight |
| `fetch_goals_data(lifetime_totals=True)` | 4–5 | Goals (active/future/history), badges, user level, lifetime totals (at most hourly; `lifetimeTotalsHidden` when the profile hides them, asked again next poll; skip with `lifetime_totals=False`) |
| `fetch_gear_data()` | 6+ | Gear items with usage totals and notes (gear v2, no per-item stats calls), device alarms, solar intensity (daily avg + gain), devices, last sync, paired sensors |
| `fetch_blood_pressure_data()` | 1 | Blood pressure measurements |
| `fetch_menstrual_data()` | 2 | Menstrual cycle data |
| `fetch_nutrition_data()` | 1 | Nutrition log: consumed macros, goals, per-meal breakdown |

## Write / Action Methods

Methods for logging data back to Garmin Connect, suitable for Home Assistant services:

| Method | Description |
| ------ | ----------- |
| `add_nutrition_log(calories, carbs, protein, fat, name)` | Log a Quick Add nutrition entry (requires Connect+) |
| `set_hydration(value_in_ml)` | Log hydration intake |
| `set_blood_pressure(systolic, diastolic, pulse)` | Log a blood pressure measurement |
| `add_body_composition(weight, percent_fat, ...)` | Log weight / body composition via FIT upload |
| `create_activity(name, type, start, duration_min)` | Create a manual activity |
| `upload_activity(file_path)` | Upload a FIT / GPX / TCX file |
| `set_active_gear(activity_type, setting, gear_uuid)` | Set default gear for an activity type |
| `add_gear_to_activity(gear_uuid, activity_id)` | Associate gear with an activity |
| `create_workout(workout)` | Create a workout from Garmin workout JSON; returns it with `workoutId` |
| `update_workout(workout_id, workout)` | Replace a workout in place (keeps its id and schedules) |
| `delete_workout(workout_id)` | Delete a workout from the library |
| `schedule_workout(workout_id, date)` | Put a workout on the training calendar |
| `unschedule_workout(scheduled_workout_id)` | Remove a workout from the calendar (keeps the workout) |

### Nutrition logging

Requires a **Garmin Connect+** subscription and initial setup through the Garmin Connect app (the app creates the meal slots that the API needs).

```python
await client.add_nutrition_log(
    calories=500,
    carbs=60,       # optional, grams
    protein=30,     # optional, grams
    fat=15,         # optional, grams
    name="Lunch",   # optional label
)
```

The method automatically fetches the correct meal slot ID and time for the day. If no meal slots are configured yet, open the Garmin Connect app and set up your nutrition plan first.

## Individual API Methods

| Method | Description |
| ------ | ----------- |
| `get_user_profile()` | User profile info |
| `get_user_settings()` | Profile settings (`userData`, incl. VO2 Max running/cycling) |
| `get_daily_summary(date)` | Garmin's full daily summary for any date (steps, calories, HR, stress, Body Battery, intensity minutes, respiration, floors) |
| `get_daily_steps()` | Steps for date range |
| `get_daily_heart_rate(date)` | A day's heart rate: resting/min/max and the samples (`[UTC epoch ms, bpm]`, about every 2 minutes) |
| `get_latest_heart_rate()` | Newest heart rate sample and its UTC time (yesterday's last right after midnight); only as recent as the last watch sync |
| `get_body_composition()` | Latest weigh-in: weight, BMI, body fat |
| `get_fitness_age()` | Fitness age metrics |
| `get_hydration_data()` | Daily hydration |
| `get_activities()` | Most recent activities (newest first, no date filter) |
| `get_activity(activity_id)` | Single activity summary (includes e-bike fields) |
| `get_activity_details()` | Detailed activity with polyline |
| `get_activity_connect_iq_display_info()` | Field definitions of the Connect IQ apps used in an activity; values are in `get_activity()['connectIQMeasurements']`, joined by `fetch_activity_data()` into `lastActivity['connectIQFields']` |
| `get_activity_hr_in_timezones()` | HR time in zones |
| `download_activity(activity_id, file_format)` | Download activity file (fit/original/tcx/gpx/kml/csv) |
| `get_workouts()` | Workout library (created/saved workouts, not when scheduled) |
| `get_workout(workout_id)` | One workout's full definition: segments, steps and targets |
| `get_scheduled_workouts(year, month)` | Training calendar for a month: self-scheduled + Garmin Coach / adaptive-plan workouts |
| `get_upcoming_events(days_forward, limit)` | Upcoming races and plan goal events, with projected/predicted race times |
| `get_training_readiness()` | Training readiness score |
| `get_training_status()` | Training status |
| `get_morning_training_readiness()` | Morning readiness |
| `get_endurance_score()` | Endurance score |
| `get_hill_score()` | Hill score |
| `get_lactate_threshold()` | Lactate threshold |
| `get_power_to_weight(target_date)` | FTP / power-to-weight data |
| `get_goals()` | User goals by status |
| `get_earned_badges()` | Earned badges |
| `get_lifetime_totals()` | Lifetime distance, steps, calories and active days. Garmin answers 403 unless "Lifetime Totals" is shown in the user's Garmin Connect profile (Account Settings → Public Profile → Show in profile; the profile can stay private) |
| `get_gear_list()` | Active and retired gear with usage totals and notes (gear v2) |
| `get_gear_details(gear_uuid)` | One piece of gear, including its default activity types (gear v2) |
| `get_gear_defaults()` | Default gear settings |
| `get_devices()` | Connected devices |
| `get_sensors()` | Paired ANT+/BLE sensors and battery status |
| `get_device_alarms()` | Device alarms |
| `get_device_solar_data()` | Solar intensity data per device |
| `get_device_last_used()` | Last used device and last sync time |
| `get_device_settings()` | Device settings |
| `get_blood_pressure()` | Blood pressure data |
| `get_menstrual_data()` | Menstrual cycle data |
| `get_menstrual_calendar()` | Menstrual calendar |
| `get_nutrition_log()` | Daily nutrition log (Connect+) |

## Data Transformations

The library automatically adds computed fields for convenience:

- **Time conversions**: `sleepTimeSeconds` → `sleepTimeMinutes`
- **Activity time**: `highlyActiveSeconds` → `highlyActiveMinutes`
- **Weight**: `weight` (grams) → `weightKg`
- **Stress**: `stressQualifier` → `stressQualifierText` (capitalized)
- **Nested flattening**: HRV status, training readiness, scores
- **VO2 Max**: training status (running, else cycling), then profile settings, then recent activities
- **Training plan goal**: the plan's goal event, else the upcoming (primary) race on your calendar
- **Skin temperature**: `avgSkinTempDeviationC` is the nightly change from your baseline, not an absolute temperature
- **Sleep stress**: `sleepStress` is the overnight series as `[{time, value}]` with UTC datetimes; samples Garmin could not measure are dropped
- **Sanity checks**: a `metabolicAge` that can't be an age in years is returned as `None`

## Community

Questions, ideas or just want to chat? Join The Junkyard on Discord:

<a href="https://discord.gg/hUBQxRhJCU">
  <img src="https://raw.githubusercontent.com/cyberjunky/cyberjunky/main/assets/junkyard-discord.svg"
       alt="Join The Junkyard on Discord" width="600">
</a>

## License

MIT
