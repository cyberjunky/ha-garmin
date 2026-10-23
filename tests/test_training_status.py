"""One device with nothing to report must not sink the whole training fetch."""

import pytest

from ha_garmin.client import _add_computed_fields

_LIVE = {
    "calendarDate": "2026-08-11",
    "trainingStatus": 7,
    "trainingStatusFeedbackPhrase": "PRODUCTIVE_6",
}


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
                "old": {
                    "calendarDate": "2026-08-01",
                    "trainingStatusFeedbackPhrase": "STRAINED_3",
                },
                "new": {
                    "calendarDate": "2026-08-11",
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_6",
                },
                "quiet": None,
            }
        )
    )

    assert result["trainingStatusPhrase"] == "Productive"


@pytest.mark.parametrize(
    ("phrase", "label"),
    [
        ("OVERREACHING_1", "Overreaching"),
        ("MAINTAINING_1", "Maintaining"),
        ("RECOVERY_1", "Recovery"),
        ("PRODUCTIVE_6", "Productive"),
        ("STRAINED_3", "Strained"),
        ("NO_STATUS", "No Status"),
    ],
)
def test_label_comes_from_garmins_phrase(phrase: str, label: str) -> None:
    """Phrases as Garmin sends them, seen on a real account."""
    result = _add_computed_fields(
        _payload(
            {
                "dev1": {
                    "calendarDate": "2026-08-11",
                    "trainingStatusFeedbackPhrase": phrase,
                }
            }
        )
    )

    assert result["trainingStatusPhrase"] == label


def test_the_phrase_wins_over_the_code() -> None:
    """Code 3 is Overreaching; an old code table called it Detraining."""
    result = _add_computed_fields(
        _payload(
            {
                "dev1": {
                    "calendarDate": "2026-08-11",
                    "trainingStatus": 3,
                    "trainingStatusFeedbackPhrase": "OVERREACHING_1",
                }
            }
        )
    )

    assert result["trainingStatusPhrase"] == "Overreaching"


@pytest.mark.parametrize("phrase", [None, "", "  ", 3, []])
def test_no_usable_phrase_reads_as_absent(phrase: object) -> None:
    result = _add_computed_fields(
        _payload(
            {
                "dev1": {
                    "calendarDate": "2026-08-11",
                    "trainingStatus": 3,
                    "trainingStatusFeedbackPhrase": phrase,
                }
            }
        )
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
                "watch_a": {
                    "calendarDate": "2026-08-10",
                    "trainingStatusFeedbackPhrase": "STRAINED_3",
                },
                "watch_b": None,
                "watch_c": {
                    "calendarDate": "2026-08-12",
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_6",
                },
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
