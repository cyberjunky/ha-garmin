#!/usr/bin/env python3
"""Try each login strategy on its own and report which ones work.

login() stops at the first strategy that works, so a normal login only ever
exercises the first one. This runs the strategies one at a time, without
fallback, to show which of them get through from this network and account.

Usage:
    make login-strategies                       (all strategies)
    .venv/bin/python scripts/test_login_strategies.py --list
    .venv/bin/python scripts/test_login_strategies.py -s widget+cffi -s portal+cffi
    .venv/bin/python scripts/test_login_strategies.py --skip-mfa --pause 120

Credentials come from GARMIN_EMAIL / GARMIN_PASSWORD or a prompt. Nothing is
written to .garmin_tokens.json. Every attempt is a real login: Garmin may
email an MFA code per strategy, and many logins in a row can get the account
or IP rate limited for a while, hence the pause between strategies.
"""

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time

from ha_garmin import GarminAuth
from ha_garmin.exceptions import (
    GarminAuthError,
    GarminMFARequired,
    GarminRateLimitError,
)

EMAIL = os.getenv("GARMIN_EMAIL", "")
PASSWORD = os.getenv("GARMIN_PASSWORD", "")


def _discard_typeahead() -> None:
    """Drop keys pressed while waiting, so a stray Enter isn't read as the answer."""
    if sys.stdin.isatty():
        try:
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (ImportError, OSError):  # not a POSIX terminal
            pass


async def ainput(prompt: str) -> str:
    """input() off the event loop (stripped), ignoring earlier keypresses."""
    _discard_typeahead()
    return (await asyncio.to_thread(input, prompt)).strip()


async def ask_code(prompt: str) -> str:
    """Ask until a non-empty MFA code is entered."""
    while not (code := await ainput(prompt)):
        print("  (empty code, try again)")
    return code


async def agetpass(prompt: str) -> str:
    """getpass() off the event loop."""
    return await asyncio.to_thread(getpass.getpass, prompt)


async def try_strategy(
    name: str, email: str, password: str, *, is_cn: bool, skip_mfa: bool
) -> tuple[str, str]:
    """Run one strategy in a fresh GarminAuth; return (result, detail)."""
    auth = GarminAuth(is_cn=is_cn)
    try:
        await auth.login_with_strategy(name, email, password)
    except GarminMFARequired:
        if skip_mfa:
            await auth.cancel_mfa()
            return "MFA", "reached the MFA step (code not entered)"
        code = await ask_code(f"  [{name}] MFA code: ")
        try:
            await auth.complete_mfa(code)
        except GarminAuthError as err:
            return "MFA FAILED", str(err)
        return "OK (MFA)", "logged in after MFA"
    except GarminRateLimitError as err:
        return "RATE LIMITED", str(err)
    except GarminAuthError as err:
        return "AUTH ERROR", str(err)
    except Exception as err:  # report every failure kind
        return "FAILED", f"{type(err).__name__}: {err}"
    finally:
        await auth.cancel_mfa()
    return "OK", "logged in"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "-s",
        "--strategy",
        action="append",
        help="strategy to try (repeatable); default: all, in login() order",
    )
    parser.add_argument("--list", action="store_true", help="list strategies and exit")
    parser.add_argument("--cn", action="store_true", help="use Garmin Connect China")
    parser.add_argument(
        "--skip-mfa",
        action="store_true",
        help="stop at the MFA step instead of asking for a code",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=60,
        help="seconds to wait between strategies (default 60)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug log of every request"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    if args.verbose:
        logging.getLogger("ha_garmin").setLevel(logging.DEBUG)

    available = [
        name for name, _ in GarminAuth(is_cn=args.cn)._login_strategies("", "")
    ]
    if args.list:
        print("\n".join(available))
        return
    names = args.strategy or available
    unknown = [n for n in names if n not in available]
    if unknown:
        parser.error(f"unknown strategy {unknown}; choose from {available}")

    email = EMAIL or await ainput("Garmin Email: ")
    password = PASSWORD or await agetpass("Garmin Password: ")

    results: list[tuple[str, str, str, float]] = []
    for i, name in enumerate(names):
        if i:
            print(f"  waiting {args.pause:.0f}s before the next strategy...")
            await asyncio.sleep(args.pause)
        print(f"\n▶ {name}")
        started = time.monotonic()
        result, detail = await try_strategy(
            name, email, password, is_cn=args.cn, skip_mfa=args.skip_mfa
        )
        elapsed = time.monotonic() - started
        print(f"  {result} in {elapsed:.0f}s: {detail}")
        results.append((name, result, detail, elapsed))

    print("\n" + "=" * 72)
    print(f"  {'strategy':<16} {'result':<14} {'time':>5}  detail")
    print("=" * 72)
    for name, result, detail, elapsed in results:
        print(f"  {name:<16} {result:<14} {elapsed:>4.0f}s  {detail[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
