# ha-garmin

Python client for Garmin Connect API, designed for Home Assistant integration.

## Features

- **Native Garmin Connect API** integration
- **Robust authentication** with multiple login strategies and automatic fallback
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

```python
from datetime import date
from ha_garmin import GarminClient, GarminAuth

async def main():
    auth = GarminAuth()

    # Load saved session from disk
    if not auth.load_session(".garmin_tokens.json"):
        await auth.login("email@example.com", "password")

        # Handle MFA if required
        if not auth.is_authenticated:
            mfa_code = input("Enter MFA code: ")
            await auth.complete_mfa(mfa_code)

        auth.save_session(".garmin_tokens.json")

    client = GarminClient(auth)

    today = date.today()
    core_data     = await client.fetch_core_data(today)      # Steps, HR, sleep, stress
    body_data     = await client.fetch_body_data(today)      # Weight, body composition, fitness age
    activity_data = await client.fetch_activity_data(today)  # Activities, workouts
    training_data = await client.fetch_training_data(today)  # HRV, training status
    goals_data    = await client.fetch_goals_data()          # Goals, badges
    gear_data     = await client.fetch_gear_data()           # Gear, device alarms
```

## For Home Assistant

```python
auth = GarminAuth()
auth.load_session(config_dir / "garmin_tokens.json")

client = GarminClient(auth)

core_data = await client.fetch_core_data(target_date=date.today())
body_data = await client.fetch_body_data(target_date=date.today())
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
