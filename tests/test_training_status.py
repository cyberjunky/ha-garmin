"""One device with nothing to report must not sink the whole training fetch."""

import pytest

from ha_garmin.client import _add_computed_fields

_LIVE = {"calendarDate": "2026-08-11", "trainingStatus": 7}


def _payload(devices: dict[str, object]) -> dict[str, object]:
    return {
        "trainingStatus": {
            "mostRecentTrainingStatus": {"latestTrainingStatusData": devices}
        }
    }


def test_a_single_device_resolves() -> None:
    result = _add_computed_fields(_payload({"dev1": _LIVE}))

    assert result["trainingStatusPhrase"] == "Productive"


def test_a_null_device_does_not_take_the_others_with_it() -> None:
    """`latestTrainingStatusData` is keyed by device, and a quiet one is null."""
    result = _add_computed_fields(_payload({"dev1": None, "dev2": _LIVE}))

    assert result["trainingStatusPhrase"] == "Productive"


def test_every_device_null() -> None:
    """Nothing to read is not the same as a crash."""
    result = _add_computed_fields(_payload({"dev1": None, "dev2": None}))

    assert result["trainingStatusPhrase"] is None


def test_a_value_that_is_not_a_mapping() -> None:
    """Seen as [] rather than null on some accounts; both must fall through."""
    result = _add_computed_fields(_payload({"dev1": []}))

    assert result["trainingStatusPhrase"] is None


def test_the_most_recent_device_wins() -> None:
    """Several watches report separately; the newest date is the real status."""
    result = _add_computed_fields(
        _payload(
            {
                "old": {"calendarDate": "2026-08-01", "trainingStatus": 8},
                "new": {"calendarDate": "2026-08-11", "trainingStatus": 7},
                "quiet": None,
            }
        )
    )

    assert result["trainingStatusPhrase"] == "Productive"


@pytest.mark.parametrize(
    ("code", "phrase"),
    [
        (2, "Unproductive"),
        (3, "Detraining"),
        (4, "Maintaining"),
        (5, "Recovering"),
    ],
)
def test_the_codes_that_were_swapped(code: int, phrase: str) -> None:
    """Regression: these four had no coverage when they were wrong."""
    result = _add_computed_fields(
        _payload({"dev1": {"calendarDate": "2026-08-11", "trainingStatus": code}})
    )

    assert result["trainingStatusPhrase"] == phrase


def test_an_unknown_code_is_not_a_crash() -> None:
    """Garmin adds statuses; an unmapped one should read as absent."""
    result = _add_computed_fields(
        _payload({"dev1": {"calendarDate": "2026-08-11", "trainingStatus": 99}})
    )

    assert result["trainingStatusPhrase"] is None


def test_a_status_code_that_cannot_be_a_key() -> None:
    """`dict.get` raises on an unhashable key, so the type check earns its place."""
    result = _add_computed_fields(
        _payload({"dev1": {"calendarDate": "2026-08-11", "trainingStatus": []}})
    )

    assert result["trainingStatusPhrase"] is None


async def test_the_whole_training_fetch_survives_a_quiet_device() -> None:
    """The point of the fix: the crash took eight other metrics with it."""
    from ha_garmin import GarminAuth, GarminClient

    auth = GarminAuth()
    auth.di_token = "token"
    client = GarminClient(auth)

    status = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "watch_a": {"calendarDate": "2026-08-10", "trainingStatus": 8},
                "watch_b": None,
                "watch_c": {"calendarDate": "2026-08-12", "trainingStatus": 7},
            }
        },
        "mostRecentVO2Max": {"generic": {"vo2MaxValue": 48}},
    }

    async def _only_status(func, *args, **kwargs):
        return status if func.__name__ == "get_training_status" else {}

    client._safe_call = _only_status  # type: ignore[method-assign]

    result = await client.fetch_training_data()

    assert result["trainingStatusPhrase"] == "Productive"
    assert result["vo2MaxValue"] == 48
