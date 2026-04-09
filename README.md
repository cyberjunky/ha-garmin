# ha-garmin

Python client for Garmin Connect API, designed for Home Assistant integration.

## Features

- **Native Garmin Connect API** integration
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

### Standalone script

Authentication is synchronous; data fetches run in a thread pool and are awaited.

```python
import asyncio
from datetime import date
from ha_garmin import GarminAuth, GarminClient, GarminMFARequired

# Auth is synchronous
auth = GarminAuth()

if not auth.load_session(".garmin_tokens.json"):
    try:
        auth.login("email@example.com", "password")
    except GarminMFARequired:
        mfa_code = input("Enter MFA code: ")
        auth.complete_mfa(mfa_code)
    auth.save_session(".garmin_tokens.json")

client = GarminClient(auth)


async def fetch_all():
    today = date.today()
    core_data     = await client.fetch_core_data(today)      # Steps, HR, sleep, stress
    body_data     = await client.fetch_body_data(today)      # Weight, body composition, fitness age
    activity_data = await client.fetch_activity_data(today)  # Activities, workouts
    training_data = await client.fetch_training_data(today)  # HRV, training status
    goals_data    = await client.fetch_goals_data()          # Goals, badges
    gear_data     = await client.fetch_gear_data()           # Gear, device alarms


asyncio.run(fetch_all())
```

### Home Assistant integration

Set up auth during `async_setup_entry` and pass the client to your coordinators.

```python
from datetime import date, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from ha_garmin import GarminAuth, GarminClient

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    auth = GarminAuth()

    token_path = hass.config.path(".storage/garmin_tokens.json")
    if not auth.load_session(token_path):
        # Initial login must have been completed via the config flow
        raise ConfigEntryAuthFailed("No valid Garmin session")

    client = GarminClient(auth)

    coordinator = GarminCoreCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return True


class GarminCoreCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, client: GarminClient) -> None:
        super().__init__(hass, logger=_LOGGER, name="Garmin core", update_interval=timedelta(minutes=15))
        self._client = client

    async def _async_update_data(self) -> dict:
        return await self._client.fetch_core_data(date.today())
```

## Coordinator Fetch Methods

Optimized methods that group related API calls for Home Assistant coordinators:

| Method | API Calls | Data Returned |
| ------ | --------- | ------------- |
| `fetch_core_data()` | 3 | Steps, distance, calories, HR, stress, sleep, body battery, SPO2 |
| `fetch_body_data()` | 3 | Weight, BMI, body fat, hydration, fitness age |
| `fetch_activity_data()` | 4+ | Activities, workouts, HR zones, polylines |
| `fetch_training_data()` | 7 | Training readiness, status, HRV, lactate, endurance/hill scores |
| `fetch_goals_data()` | 4 | Goals (active/future/history), badges, user level |
| `fetch_gear_data()` | 4+ | Gear items, stats, device alarms |
| `fetch_blood_pressure_data()` | 1 | Blood pressure measurements |
| `fetch_menstrual_data()` | 2 | Menstrual cycle data |

## Individual API Methods

| Method | Description |
| ------ | ----------- |
| `get_user_profile()` | User profile info |
| `get_user_summary()` | Daily summary (steps, HR, stress, body battery) |
| `get_daily_steps()` | Steps for date range |
| `get_body_composition()` | Weight, BMI, body fat |
| `get_fitness_age()` | Fitness age metrics |
| `get_hydration_data()` | Daily hydration |
| `get_activities_by_date()` | Activities in date range |
| `get_activity_details()` | Detailed activity with polyline |
| `get_activity_hr_in_timezones()` | HR time in zones |
| `get_workouts()` | Scheduled workouts |
| `get_training_readiness()` | Training readiness score |
| `get_training_status()` | Training status |
| `get_morning_training_readiness()` | Morning readiness |
| `get_endurance_score()` | Endurance score |
| `get_hill_score()` | Hill score |
| `get_lactate_threshold()` | Lactate threshold |
| `get_hrv_data()` | Heart rate variability |
| `get_goals()` | User goals by status |
| `get_earned_badges()` | Earned badges |
| `get_gear()` | User gear items |
| `get_gear_stats()` | Gear statistics |
| `get_gear_defaults()` | Default gear settings |
| `get_devices()` | Connected devices |
| `get_device_alarms()` | Device alarms |
| `get_device_settings()` | Device settings |
| `get_blood_pressure()` | Blood pressure data |
| `get_menstrual_data()` | Menstrual cycle data |
| `get_menstrual_calendar()` | Menstrual calendar |

## Data Transformations

The library automatically adds computed fields for convenience:

- **Time conversions**: `sleepTimeSeconds` → `sleepTimeMinutes`
- **Activity time**: `highlyActiveSeconds` → `highlyActiveMinutes`
- **Weight**: `weight` (grams) → `weightKg`
- **Stress**: `stressQualifier` → `stressQualifierText` (capitalized)
- **Nested flattening**: HRV status, training readiness, scores

## License

MIT
