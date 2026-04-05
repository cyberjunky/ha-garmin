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
from ha_garmin import GarminAuth, GarminClient

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


async def main():
    """Fetch and display all data from ha_garmin."""
    # Initialize new engine
    auth = GarminAuth()

    if auth.load_session(TOKEN_FILE):
        print(f"Successfully loaded seamless JWT session from {TOKEN_FILE}")
    else:
        print("No valid session found, initiating native login...")
        email = EMAIL
        password = PASSWORD

        if email == "your-email@example.com":
            email = input("Garmin Email: ").strip()
        if password == "your-password":
            import getpass

            password = getpass.getpass("Garmin Password: ")

        print(f"Logging in as {email}...")

        try:
            from ha_garmin.exceptions import GarminMFARequired

            auth.login(email, password)
        except GarminMFARequired:
            print("MFA required!")
            mfa_code = input("Enter MFA code: ").strip()
            auth.complete_mfa(mfa_code)
        except Exception as e:
            print(f"Login failed: {e}")
            return

        print("Seamless Login successful!")
        auth.save_session(TOKEN_FILE)
        print(f"Saved persistent auth state to {TOKEN_FILE}")

    client = GarminClient(auth)

    today = date.today()

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
    gear_data = await client.fetch_gear_data()
    print_section("Gear Data", gear_data)

    # === SHOW NULL/NONE VALUES ===
    print("\n" + "=" * 60)
    print("  VALUES THAT ARE None (may need historical fetch)")
    print("=" * 60)

    all_data = {
        "core": core_data,
        "activity": activity_data,
        "training": training_data,
        "body": body_data,
    }

    for section, data in all_data.items():
        if isinstance(data, dict):
            none_keys = [k for k, v in data.items() if v is None]
            if none_keys:
                print(f"\n  {section.upper()}:")
                for k in sorted(none_keys):
                    print(f"    - {k}")

    # === SAVE FULL DATA TO JSON ===
    output_file = ".garmin_data_dump.json"
    with open(output_file, "w") as f:
        json.dump(all_data, f, indent=2, default=json_serial)
    print(f"\n\nFull data saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
