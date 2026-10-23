#!/usr/bin/env python3
"""Test script to inspect ha_garmin data returned by fetch methods.

Usage:
    python test_fetch_data.py

You need to set environment variables or edit credentials below.
Tokens are saved to .garmin_tokens.json for subsequent runs.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from pprint import pprint

logging.basicConfig(level=logging.INFO)
from ha_garmin import GarminAuth, GarminAuthError, GarminClient  # noqa: E402
from ha_garmin.exceptions import GarminMFARequired  # noqa: E402

# === CREDENTIALS ===
# Set these via environment variables or edit directly
EMAIL = os.getenv("GARMIN_EMAIL", "your-email@example.com")
PASSWORD = os.getenv("GARMIN_PASSWORD", "your-password")

# Token storage file
TOKEN_FILE = Path(__file__).parent / ".garmin_tokens.json"


def json_serial(obj):
    """JSON serializer for objects not serializable by default."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def print_section(title: str, data: dict | list | None):
    """Pretty print a data section."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    if data is None:
        print("  (No data)")
    elif isinstance(data, dict):
        # Print keys and their types/values
        for key, value in sorted(data.items()):
            val_type = type(value).__name__
            if isinstance(value, (datetime, date)):
                print(f"  {key}: {value.isoformat()} ({val_type})")
            elif isinstance(value, dict):
                print(f"  {key}: {{...}} ({len(value)} keys)")
            elif isinstance(value, list):
                print(f"  {key}: [...] ({len(value)} items)")
            elif isinstance(value, str) and len(value) > 50:
                print(f"  {key}: '{value[:50]}...' ({val_type})")
            else:
                print(f"  {key}: {value} ({val_type})")
    else:
        pprint(data)


def interactive_login(auth: GarminAuth) -> None:
    """Prompt for credentials (and MFA if required) and log in."""
    email = EMAIL
    password = PASSWORD

    if email == "your-email@example.com":
        email = input("Garmin Email: ").strip()
    if password == "your-password":
        import getpass

        password = getpass.getpass("Garmin Password: ")

    print(f"Logging in as {email}...")

    try:
        auth.login(email, password)
    except GarminMFARequired:
        print("MFA required!")
        mfa_code = input("Enter MFA code: ").strip()
        auth.complete_mfa(mfa_code)

    print("Login successful!")
    auth.save_session(TOKEN_FILE)
    print(f"Saved persistent auth state to {TOKEN_FILE}")


async def main():
    """Fetch and display all data from ha_garmin."""
    # Initialize new engine
    auth = GarminAuth()

    if auth.load_session(TOKEN_FILE):
        print(f"Successfully loaded seamless JWT session from {TOKEN_FILE}")
    else:
        print("No valid session found, initiating native login...")
        try:
            interactive_login(auth)
        except Exception as e:
            print(f"Login failed: {e}")
            return

    client = GarminClient(auth)

    today = date.today()

    # === USER PROFILE ===
    print("\n" + "=" * 60)
    print("  FETCHING USER PROFILE")
    print("=" * 60)
    try:
        profile = await client.get_user_profile()
    except GarminAuthError:
        # The loaded session looked valid locally (unexpired per its own
        # stored timestamps) but Garmin rejected the refresh -- the refresh
        # token itself has expired or been revoked server-side. Fall back
        # to the same interactive login used for "no session on disk".
        print(
            "Stored session was rejected by Garmin (expired/revoked refresh "
            "token). Re-authenticating..."
        )
        TOKEN_FILE.unlink(missing_ok=True)
        try:
            interactive_login(auth)
        except Exception as e:
            print(f"Login failed: {e}")
            return
        profile = await client.get_user_profile()
    print(f"  id: {profile.id}")
    print(f"  profile_id: {profile.profile_id}")
    print(f"  display_name: {profile.display_name}")

    # === FETCH CORE DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING CORE DATA (today)")
    print("=" * 60)
    core_data = await client.fetch_core_data(today)
    print_section("Core Data", core_data)

    # === FETCH ACTIVITY DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING ACTIVITY DATA")
    print("=" * 60)
    activity_data = await client.fetch_activity_data(today)
    print_section("Activity Data", activity_data)

    # Check lastActivity specifically
    if "lastActivity" in activity_data:
        print("\n  --- Last Activity Details ---")
        last_act = activity_data["lastActivity"]
        if isinstance(last_act, dict):
            for k, v in last_act.items():
                if isinstance(v, (datetime, date)):
                    print(f"    {k}: {v.isoformat()} ({type(v).__name__})")
                elif k not in ("polyline", "hrTimeInZones"):
                    print(f"    {k}: {v}")

    # === FETCH SCHEDULED WORKOUTS DATA (training calendar / Garmin Coach) ===
    # activity_data["todayScheduledWorkout"] / ["nextScheduledWorkout"] /
    # ["scheduledWorkouts"] already have this filtered to workout-type
    # items across this month + next, via fetch_activity_data
    # (home-assistant-garmin_connect#521). Fetched again raw here too, for
    # the full unfiltered calendar (weigh-ins, naps, etc. included).
    print("\n  --- Today / Next Scheduled Workout ---")
    print(f"    today: {activity_data.get('todayScheduledWorkout')}")
    print(f"    next: {activity_data.get('nextScheduledWorkout')}")

    print("\n" + "=" * 60)
    print("  FETCHING RAW CALENDAR (training calendar, unfiltered)")
    print("=" * 60)
    scheduled_workouts_data = await client.get_scheduled_workouts(
        today.year, today.month
    )
    print_section(
        f"Raw Calendar ({today.year}-{today.month:02d})", scheduled_workouts_data
    )

    next_month = today.month + 1 if today.month < 12 else 1
    next_month_year = today.year if today.month < 12 else today.year + 1
    next_month_scheduled_workouts_data = await client.get_scheduled_workouts(
        next_month_year, next_month
    )
    print_section(
        f"Raw Calendar ({next_month_year}-{next_month:02d})",
        next_month_scheduled_workouts_data,
    )

    # === FETCH TRAINING PLAN DATA (#521's training-plan detail) ===
    # calendar-service only shows what Garmin has already committed to the
    # calendar. The plan's own goal event (target race, projected time)
    # lives at get_calendar_events_for_plan instead. The weekly day-marker
    # look-ahead ("browse to next week" in the app) needs atp-api, a
    # different gateway wanting session-cookie + CSRF auth this client
    # can't currently produce -- tried and reverted, see git history on
    # this file if picking it back up.
    print("\n" + "=" * 60)
    print("  FETCHING TRAINING PLANS")
    print("=" * 60)
    training_plans_data = await client.get_training_plans()
    print_section("Training Plans", training_plans_data)

    atp_plan_id = (activity_data.get("nextScheduledWorkout") or {}).get("atpPlanId")
    adaptive_plan_data = None
    calendar_events_data = None
    if atp_plan_id:
        print("\n" + "=" * 60)
        print(f"  FETCHING ADAPTIVE TRAINING PLAN ({atp_plan_id})")
        print("=" * 60)
        adaptive_plan_data = await client.get_adaptive_training_plan_by_id(atp_plan_id)
        print_section(f"Adaptive Training Plan ({atp_plan_id})", adaptive_plan_data)

        print("\n" + "=" * 60)
        print(f"  FETCHING CALENDAR EVENTS FOR PLAN ({atp_plan_id})")
        print("=" * 60)
        calendar_events_data = await client.get_calendar_events_for_plan(atp_plan_id)
        print_section(f"Calendar Events ({atp_plan_id})", calendar_events_data)
    else:
        print(
            "\n  (No atpPlanId on nextScheduledWorkout -- skipping adaptive plan fetches)"
        )

    # === FETCH TRAINING DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING TRAINING DATA")
    print("=" * 60)
    training_data = await client.fetch_training_data(today)
    print_section("Training Data", training_data)

    # === FETCH BODY DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING BODY DATA")
    print("=" * 60)
    body_data = await client.fetch_body_data(today)
    print_section("Body Data", body_data)

    # === FETCH GOALS DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING GOALS DATA")
    print("=" * 60)
    goals_data = await client.fetch_goals_data()
    print_section("Goals Data", goals_data)

    # === FETCH GEAR DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING GEAR DATA")
    print("=" * 60)
    gear_data = await client.fetch_gear_data(timezone="Europe/Amsterdam")
    print_section("Gear Data", gear_data)

    # === FETCH BLOOD PRESSURE DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING BLOOD PRESSURE DATA")
    print("=" * 60)
    blood_pressure_data = await client.fetch_blood_pressure_data(today)
    print_section("Blood Pressure Data", blood_pressure_data)

    # === FETCH MENSTRUAL DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING MENSTRUAL DATA")
    print("=" * 60)
    menstrual_data = await client.fetch_menstrual_data(today)
    print_section("Menstrual Data", menstrual_data)

    # === FETCH NUTRITION DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING NUTRITION DATA (requires Connect+)")
    print("=" * 60)
    nutrition_data = await client.fetch_nutrition_data(today)
    print_section("Nutrition Data", nutrition_data)

    # === FETCH SENSORS DATA ===
    print("\n" + "=" * 60)
    print("  FETCHING SENSORS DATA")
    print("=" * 60)
    sensors_data = await client.get_sensors()
    print_section("Sensors Data", sensors_data)

    # === SHOW NULL/NONE VALUES ===
    print("\n" + "=" * 60)
    print("  VALUES THAT ARE None (may need historical fetch)")
    print("=" * 60)

    all_data = {
        "core": core_data,
        "activity": activity_data,
        "training": training_data,
        "body": body_data,
        "goals": goals_data,
        "gear": gear_data,
        "blood_pressure": blood_pressure_data,
        "menstrual": menstrual_data,
        "nutrition": nutrition_data,
        "sensors": sensors_data,
        "scheduled_workouts_this_month": scheduled_workouts_data,
        "scheduled_workouts_next_month": next_month_scheduled_workouts_data,
        "training_plans": training_plans_data,
        "adaptive_training_plan": adaptive_plan_data,
        "calendar_events_for_plan": calendar_events_data,
    }

    for section, data in all_data.items():
        if isinstance(data, dict):
            none_keys = [k for k, v in data.items() if v is None]
            if none_keys:
                print(f"\n  {section.upper()}:")
                for k in sorted(none_keys):
                    print(f"    - {k}")

    # Save refreshed tokens so next run doesn't need to re-authenticate
    auth.save_session(TOKEN_FILE)

    # === SAVE FULL DATA TO JSON ===
    output_file = ".garmin_data_dump.json"
    with open(output_file, "w") as f:
        json.dump(all_data, f, indent=2, default=json_serial)
    print(f"\n\nFull data saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
