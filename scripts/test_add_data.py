#!/usr/bin/env python3
"""Test script for ha_garmin write/action methods.

Usage:
    python test_add_data.py

Tokens are loaded from .garmin_tokens.json if available (written by test_fetch_data.py).
Credentials can be set via GARMIN_EMAIL / GARMIN_PASSWORD environment variables.
"""

import asyncio
import getpass
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from ha_garmin import GarminAuth, GarminClient
from ha_garmin.exceptions import GarminAuthError

logging.basicConfig(level=logging.WARNING)
logging.getLogger("ha_garmin").setLevel(logging.DEBUG)

EMAIL = os.getenv("GARMIN_EMAIL", "your-email@example.com")
PASSWORD = os.getenv("GARMIN_PASSWORD", "your-password")
TOKEN_FILE = Path(__file__).parent / ".garmin_tokens.json"

MENU = [
    ("Nutrition quick-add", "nutrition"),
    ("Hydration log", "hydration"),
    ("Blood pressure", "blood_pressure"),
    ("Body composition", "body_composition"),
    ("Create manual activity", "create_activity"),
    ("Upload activity file", "upload_activity"),
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


async def get_client() -> GarminClient:
    auth = GarminAuth()

    if auth.load_session(TOKEN_FILE):
        print(f"Loaded session from {TOKEN_FILE}\n")
    else:
        print("No valid session found, logging in...")
        email = EMAIL
        password = PASSWORD

        if email == "your-email@example.com":
            email = input("Garmin Email: ").strip()
        if password == "your-password":
            password = getpass.getpass("Garmin Password: ")

        try:
            from ha_garmin.exceptions import GarminMFARequired

            auth.login(email, password)
        except GarminMFARequired:
            mfa_code = input("MFA code: ").strip()
            auth.complete_mfa(mfa_code)
        except Exception as e:
            print(f"Login failed: {e}")
            raise

        auth.save_session(TOKEN_FILE)
        print(f"Session saved to {TOKEN_FILE}\n")

    return GarminClient(auth)


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------


async def do_nutrition(client: GarminClient) -> None:
    """Log a Quick Add nutrition entry (Connect+ required)."""
    print("\n--- Nutrition Quick Add ---")
    calories = float(input("Calories [100]: ").strip() or "100")
    carbs = float(input("Carbs g  [10]:  ").strip() or "10")
    protein = float(input("Protein g [5]: ").strip() or "5")
    fat = float(input("Fat g    [3]:  ").strip() or "3")
    name = input("Name     [HA Test]: ").strip() or "HA Test"

    result = await client.add_nutrition_log(
        calories=calories,
        carbs=carbs,
        protein=protein,
        fat=fat,
        name=name,
    )
    _print_result(result)
    print("Check Garmin Connect > Nutrition to verify.")


async def do_hydration(client: GarminClient) -> None:
    """Log hydration intake."""
    print("\n--- Hydration Log ---")
    ml = float(input("Amount in mL [250]: ").strip() or "250")

    result = await client.set_hydration(value_in_ml=ml)
    _print_result(result)


async def do_blood_pressure(client: GarminClient) -> None:
    """Log a blood pressure measurement."""
    print("\n--- Blood Pressure ---")
    systolic = int(input("Systolic  [120]: ").strip() or "120")
    diastolic = int(input("Diastolic  [80]: ").strip() or "80")
    pulse = int(input("Pulse      [70]: ").strip() or "70")
    notes = input("Notes      []:   ").strip()

    result = await client.set_blood_pressure(
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
        notes=notes,
    )
    _print_result(result)


async def do_body_composition(client: GarminClient) -> None:
    """Log a body weight / composition measurement via FIT upload."""
    print("\n--- Body Composition ---")
    weight = float(input("Weight kg    [75.0]: ").strip() or "75.0")
    percent_fat_s = input("Body fat %   [skip]: ").strip()
    percent_fat = float(percent_fat_s) if percent_fat_s else None

    result = await client.add_body_composition(
        weight=weight,
        percent_fat=percent_fat,
    )
    _print_result(result)


async def do_create_activity(client: GarminClient) -> None:
    """Create a manual activity entry."""
    print("\n--- Create Manual Activity ---")
    name = input("Activity name  [Test run]:  ").strip() or "Test run"
    atype = input("Type key       [running]:   ").strip() or "running"
    start = input("Start datetime [now]:        ").strip()
    if not start:
        start = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000")
    duration = int(input("Duration min   [30]:        ").strip() or "30")
    distance = float(input("Distance km    [5.0]:       ").strip() or "5.0")

    result = await client.create_activity(
        activity_name=name,
        activity_type=atype,
        start_datetime=start,
        duration_min=duration,
        distance_km=distance,
    )
    _print_result(result)


async def do_upload_activity(client: GarminClient) -> None:
    """Upload a FIT / GPX / TCX activity file."""
    print("\n--- Upload Activity File ---")
    file_path = input("File path (FIT/GPX/TCX): ").strip()
    if not file_path:
        print("No path given, skipping.")
        return

    result = await client.upload_activity(file_path=file_path)
    _print_result(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_result(result: object) -> None:
    print("\nResponse:")
    print(json.dumps(result, indent=2, default=str))


HANDLERS = {
    "nutrition": do_nutrition,
    "hydration": do_hydration,
    "blood_pressure": do_blood_pressure,
    "body_composition": do_body_composition,
    "create_activity": do_create_activity,
    "upload_activity": do_upload_activity,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    print("=" * 50)
    print("  ha_garmin — write/action test script")
    print("=" * 50)

    client = await get_client()

    while True:
        print("\nSelect action:")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}. {label}")
        print("  0. Exit")

        choice = input("\n> ").strip()
        if choice == "0":
            break

        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(MENU)):
                raise ValueError
        except ValueError:
            print("Invalid choice.")
            continue

        _, key = MENU[idx]
        try:
            await HANDLERS[key](client)
        except GarminAuthError:
            print("\nSession expired — please log in again.")
            TOKEN_FILE.unlink(missing_ok=True)
            client = await get_client()
            try:
                await HANDLERS[key](client)
            except Exception as e:
                print(f"\nError after re-login: {e}")
        except Exception as e:
            print(f"\nError: {e}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
