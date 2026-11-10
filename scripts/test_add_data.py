#!/usr/bin/env python3
"""Test script for ha_garmin write/action methods.

Runs the async library standalone, without Home Assistant, on an injected
aiohttp session like Home Assistant's.

Usage:
    make add-data         (or: .venv/bin/python scripts/test_add_data.py)

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

import aiohttp

from ha_garmin import GarminAuth, GarminClient
from ha_garmin.exceptions import GarminAuthError, GarminMFARequired

logging.basicConfig(level=logging.WARNING)
logging.getLogger("ha_garmin").setLevel(logging.DEBUG)

EMAIL = os.getenv("GARMIN_EMAIL", "your-email@example.com")
PASSWORD = os.getenv("GARMIN_PASSWORD", "your-password")
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".garmin_tokens.json"

MENU = [
    ("Nutrition quick-add", "nutrition"),
    ("Hydration log", "hydration"),
    ("Blood pressure", "blood_pressure"),
    ("Body composition", "body_composition"),
    ("Create manual activity", "create_activity"),
    ("Upload activity file", "upload_activity"),
    ("Download activity file", "download_activity"),
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


async def ainput(prompt: str) -> str:
    """input() off the event loop (stripped), so asyncio debug mode only flags real blocking."""
    return (await asyncio.to_thread(input, prompt)).strip()


async def agetpass(prompt: str) -> str:
    """getpass() off the event loop."""
    return await asyncio.to_thread(getpass.getpass, prompt)


async def get_client(session: aiohttp.ClientSession) -> GarminClient:
    auth = GarminAuth()

    if auth.load_session(TOKEN_FILE):
        print(f"Loaded session from {TOKEN_FILE}\n")
    else:
        print("No valid session found, logging in...")
        email = EMAIL
        password = PASSWORD

        if email == "your-email@example.com":
            email = await ainput("Garmin Email: ")
        if password == "your-password":
            password = await agetpass("Garmin Password: ")

        try:
            await auth.login(email, password)
        except GarminMFARequired:
            mfa_code = await ainput("MFA code: ")
            await auth.complete_mfa(mfa_code)
        except Exception as e:
            print(f"Login failed: {e}")
            raise

        auth.save_session(TOKEN_FILE)
        print(f"Session saved to {TOKEN_FILE}\n")

    return GarminClient(auth, session=session)


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------


async def do_nutrition(client: GarminClient) -> None:
    """Log a Quick Add nutrition entry (Connect+ required)."""
    print("\n--- Nutrition Quick Add ---")
    calories = float(await ainput("Calories [100]: ") or "100")
    carbs = float(await ainput("Carbs g  [10]:  ") or "10")
    protein = float(await ainput("Protein g [5]: ") or "5")
    fat = float(await ainput("Fat g    [3]:  ") or "3")
    name = await ainput("Name     [HA Test]: ") or "HA Test"

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
    ml = float(await ainput("Amount in mL [250]: ") or "250")

    result = await client.set_hydration(value_in_ml=ml)
    _print_result(result)


async def do_blood_pressure(client: GarminClient) -> None:
    """Log a blood pressure measurement."""
    print("\n--- Blood Pressure ---")
    systolic = int(await ainput("Systolic  [120]: ") or "120")
    diastolic = int(await ainput("Diastolic  [80]: ") or "80")
    pulse = int(await ainput("Pulse      [70]: ") or "70")
    notes = await ainput("Notes      []:   ")

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
    weight = float(await ainput("Weight kg    [75.0]: ") or "75.0")
    percent_fat_s = await ainput("Body fat %   [skip]: ")
    percent_fat = float(percent_fat_s) if percent_fat_s else None

    result = await client.add_body_composition(
        weight=weight,
        percent_fat=percent_fat,
    )
    _print_result(result)


async def do_create_activity(client: GarminClient) -> None:
    """Create a manual activity entry."""
    print("\n--- Create Manual Activity ---")
    name = await ainput("Activity name  [Test run]:  ") or "Test run"
    atype = await ainput("Type key       [running]:   ") or "running"
    start = await ainput("Start datetime [now]:        ")
    if not start:
        start = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000")
    duration = int(await ainput("Duration min   [30]:        ") or "30")
    distance = float(await ainput("Distance km    [5.0]:       ") or "5.0")

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
    file_path = await ainput("File path (FIT/GPX/TCX): ")
    if not file_path:
        print("No path given, skipping.")
        return

    result = await client.upload_activity(file_path=file_path)
    _print_result(result)


DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
DOWNLOAD_FORMATS = {
    "fit": "fit",
    "original": "zip",
    "tcx": "tcx",
    "gpx": "gpx",
    "kml": "kml",
    "csv": "csv",
}


def _looks_like(fmt: str, content: bytes) -> bool:
    """Cheap check that the bytes are the format asked for."""
    if fmt == "fit":
        return len(content) > 12 and content[8:12] == b".FIT"
    if fmt == "original":
        return content[:2] == b"PK"
    if fmt in ("tcx", "gpx", "kml"):
        return content.lstrip()[:5] == b"<?xml" or content.lstrip()[:1] == b"<"
    return bool(content)


async def do_download_activity(client: GarminClient) -> None:
    """Download one of the recent activities in a chosen format."""
    print("\n--- Download Activity File ---")
    activities = await client.get_activities(limit=5)
    for i, activity in enumerate(activities, 1):
        print(
            f"  {i}. {activity.get('activityId')}  "
            f"{activity.get('startTime')}  {activity.get('activityName')}"
        )
    choice = await ainput("Number from the list, or an activity ID [1]: ") or "1"
    if choice.isdigit() and 1 <= int(choice) <= len(activities):
        activity_id = int(activities[int(choice) - 1]["activityId"])
    elif choice.isdigit():
        activity_id = int(choice)
    else:
        print("Not a number, skipping.")
        return

    fmt = (await ainput(f"Format {list(DOWNLOAD_FORMATS)} [fit]: ") or "fit").lower()
    if fmt not in DOWNLOAD_FORMATS:
        print(f"Unknown format {fmt!r}, skipping.")
        return

    content = await client.download_activity(activity_id, fmt)
    path = DOWNLOAD_DIR / f"activity_{activity_id}.{DOWNLOAD_FORMATS[fmt]}"

    def _write() -> None:
        DOWNLOAD_DIR.mkdir(exist_ok=True)
        path.write_bytes(content)

    await asyncio.to_thread(_write)
    check = "looks valid" if _looks_like(fmt, content) else "UNEXPECTED CONTENT"
    print(f"\nSaved {len(content)} bytes to {path} ({check})")


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
    "download_activity": do_download_activity,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    print("=" * 50)
    print("  ha_garmin — write/action test script")
    print("=" * 50)

    # One shared session for the data API, as Home Assistant would inject it.
    async with aiohttp.ClientSession() as session:
        await run(session)


async def run(session: aiohttp.ClientSession) -> None:
    client = await get_client(session)

    while True:
        print("\nSelect action:")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}. {label}")
        print("  0. Exit")

        choice = await ainput("\n> ")
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
            client = await get_client(session)
            try:
                await HANDLERS[key](client)
            except Exception as e:
                print(f"\nError after re-login: {e}")
        except Exception as e:
            print(f"\nError: {e}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
