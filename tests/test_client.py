"""Tests for GarminClient."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_garmin import GarminAuth, GarminClient
from ha_garmin.exceptions import GarminAuthError


def _make_auth(di_token: str = "fake_di_token") -> GarminAuth:
    """Return an authenticated GarminAuth with a fake DI token."""
    auth = GarminAuth()
    auth.di_token = di_token
    return auth


def _mock_response(payload: object, status: int = 200) -> MagicMock:
    """Return a fake requests.Response-like object."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


class TestGarminClient:
    """Tests for GarminClient class."""

    async def test_request_without_auth(self):
        """Test request fails without authentication."""
        auth = GarminAuth()
        client = GarminClient(auth)

        with pytest.raises(GarminAuthError, match="Not authenticated"):
            await client.get_user_profile()

    async def test_get_user_profile(self):
        """Test get_user_profile parses response correctly."""
        auth = _make_auth()
        client = GarminClient(auth)

        profile_payload = {
            "id": 12345,
            "profileId": 67890,
            "displayName": "testuser",
            "profileImageUrlMedium": "https://example.com/image.jpg",
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(profile_payload)
            profile = await client.get_user_profile()

        assert profile.display_name == "testuser"
        assert profile.id == 12345
        assert profile.profile_id == 67890

    async def test_get_activities(self):
        """Test get_activities_by_date returns list and preserves fields."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = [
            {
                "activityId": 1,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2024-01-01T08:00:00",
                "startTimeGMT": "2024-01-01T07:00:00",
                "distance": 5000.0,
                "duration": 1800.0,
            }
        ]

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            end_date = date.today()
            start_date = end_date - timedelta(days=7)
            activities = await client.get_activities_by_date(start_date, end_date)

        assert len(activities) == 1
        assert activities[0]["activityName"] == "Morning Run"
        assert activities[0]["distance"] == 5000.0

    async def test_get_devices(self):
        """Test get_devices returns list."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = [
            {
                "deviceId": 123,
                "displayName": "Forerunner 955",
                "deviceTypeName": "forerunner955",
                "batteryLevel": 85,
                "batteryStatus": "GOOD",
            }
        ]

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            devices = await client.get_devices()

        assert len(devices) == 1
        assert devices[0]["displayName"] == "Forerunner 955"
        assert devices[0]["batteryLevel"] == 85

    async def test_fetch_core_data_sleep_fields(self):
        """Test fetch_core_data returns all sleep fields including nap and unmeasurable."""
        auth = _make_auth()
        client = GarminClient(auth)

        profile_payload = {
            "id": 12345,
            "profileId": 67890,
            "displayName": "testuser",
        }
        summary_payload = {
            "dailyStepGoal": 10000,
            "totalSteps": 5000,
            "totalDistanceMeters": 4000,
        }
        steps_payload = [
            {
                "totalSteps": 8000,
                "totalDistance": 6000,
                "calendarDate": "2026-01-23",
            }
        ]
        sleep_payload = {
            "dailySleepDTO": {
                "sleepTimeSeconds": 28800,
                "deepSleepSeconds": 7200,
                "lightSleepSeconds": 14400,
                "remSleepSeconds": 5400,
                "awakeSleepSeconds": 1800,
                "napTimeSeconds": 3600,
                "unmeasurableSleepSeconds": 600,
                "sleepStartTimestampLocal": 1775944808000,
                "sleepEndTimestampLocal": 1775973467000,
                "sleepNeed": {"baseline": 470},
                "sleepAlignment": {
                    "optimalSleepWindowStartMins": -80,
                    "optimalSleepWindowEndMins": 390,
                },
                "sleepScores": {"overall": {"value": 85}},
            }
        }

        responses = [
            _mock_response(
                profile_payload
            ),  # get_user_profile (1st call, cached after)
            _mock_response(summary_payload),  # _get_user_summary_raw
            _mock_response(steps_payload),  # get_daily_steps
            _mock_response(sleep_payload),  # _get_sleep_data_raw (profile cached)
        ]

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = responses
            data = await client.fetch_core_data()

        assert data["sleepScore"] == 85
        assert "sleepTimeSeconds" not in data
        assert "deepSleepSeconds" not in data
        assert "lightSleepSeconds" not in data
        assert "remSleepSeconds" not in data
        assert "awakeSleepSeconds" not in data
        assert "napTimeSeconds" not in data
        assert "unmeasurableSleepSeconds" not in data

        assert data["sleepTimeMinutes"] == 480
        assert data["deepSleepMinutes"] == 120
        assert data["lightSleepMinutes"] == 240
        assert data["remSleepMinutes"] == 90
        assert data["awakeSleepMinutes"] == 30
        assert data["napTimeMinutes"] == 60
        assert data["unmeasurableSleepMinutes"] == 10
        assert data["sleepNeed"] == 470
        assert data["bedtime"] == datetime(2026, 4, 11, 22, 0, 8, tzinfo=UTC)
        assert data["optimalBedtime"] == datetime(2026, 4, 11, 22, 40, tzinfo=UTC)
        assert data["wakeTime"] == datetime(2026, 4, 12, 5, 57, 47, tzinfo=UTC)
        assert data["optimalWakeTime"] == datetime(2026, 4, 12, 6, 30, tzinfo=UTC)

    async def test_request_returns_empty_on_204(self):
        """Test _request returns empty dict on 204 No Content."""
        auth = _make_auth()
        client = GarminClient(auth)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response({}, status=204)
            result = await client._request("GET", "https://connectapi.garmin.com/test")

        assert result == {}

    async def test_get_body_composition_uses_daily_weight_summaries(self):
        """Test get_body_composition returns latest weight from dailyWeightSummaries."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = {
            "dailyWeightSummaries": [
                {
                    "summaryDate": "2026-04-01",
                    "latestWeight": {
                        "calendarDate": "2026-04-01",
                        "weight": 85000.0,
                        "bmi": 25.0,
                    },
                },
                {
                    "summaryDate": "2026-04-03",
                    "latestWeight": {
                        "calendarDate": "2026-04-03",
                        "weight": 87000.0,
                        "bmi": 26.0,
                    },
                },
            ],
            "totalAverage": {"weight": 86000.0, "bmi": 25.5},
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            result = await client.get_body_composition()

        # Should pick the most recent by calendarDate
        assert result["weight"] == 87000.0
        assert result["bmi"] == 26.0

    async def test_get_body_composition_falls_back_to_total_average(self):
        """Test get_body_composition falls back to totalAverage when no summaries."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = {
            "dailyWeightSummaries": [],
            "totalAverage": {"weight": 86000.0, "bmi": 25.5},
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            result = await client.get_body_composition()

        assert result["weight"] == 86000.0
        assert result["bmi"] == 25.5

    async def test_add_computed_fields_burned_kilocalories_computed(self):
        """Test _add_computed_fields computes burnedKilocalories from bmr+active when null."""
        from ha_garmin.client import _add_computed_fields

        data = {
            "burnedKilocalories": None,
            "bmrKilocalories": 1400.0,
            "activeKilocalories": 200.0,
        }
        result = _add_computed_fields(data)
        assert result["burnedKilocalories"] == 1600.0

    async def test_add_computed_fields_burned_kilocalories_not_overwritten(self):
        """Test _add_computed_fields does not overwrite burnedKilocalories when present."""
        from ha_garmin.client import _add_computed_fields

        data = {
            "burnedKilocalories": 1500.0,
            "bmrKilocalories": 1400.0,
            "activeKilocalories": 200.0,
        }
        result = _add_computed_fields(data)
        assert result["burnedKilocalories"] == 1500.0

    async def test_get_power_to_weight_returns_list(self):
        """Test get_power_to_weight returns a list of sport entries."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = [
            {
                "sport": "RUNNING",
                "functionalThresholdPower": 425,
                "powerToWeight": 4.84,
                "weight": 87.87,
                "isStale": False,
            },
            {
                "sport": "CYCLING",
                "functionalThresholdPower": 242,
                "powerToWeight": 2.75,
                "weight": 87.87,
                "isStale": False,
            },
        ]

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            result = await client.get_power_to_weight()

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["sport"] == "RUNNING"
        assert result[0]["functionalThresholdPower"] == 425

    async def test_fetch_training_data_includes_power_to_weight(self):
        """Test fetch_training_data includes powerToWeight in result."""
        auth = _make_auth()
        client = GarminClient(auth)

        ptw_payload = [
            {"sport": "RUNNING", "functionalThresholdPower": 425, "powerToWeight": 4.84}
        ]

        async def mock_safe_call(func, *args, **kwargs):
            if func == client.get_power_to_weight:
                return ptw_payload
            return {}

        client._safe_call = mock_safe_call
        data = await client.fetch_training_data()

        assert "powerToWeight" in data
        assert data["powerToWeight"] == ptw_payload

    async def test_fetch_training_data_power_to_weight_yesterday_fallback(self):
        """Test fetch_training_data falls back to yesterday for powerToWeight."""
        auth = _make_auth()
        client = GarminClient(auth)

        ptw_yesterday = [
            {"sport": "RUNNING", "functionalThresholdPower": 420, "powerToWeight": 4.78}
        ]

        call_count = {"n": 0}

        async def mock_safe_call(func, *args, **kwargs):
            if func == client.get_power_to_weight:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return []  # today: empty
                return ptw_yesterday  # yesterday: has data
            return {}

        client._safe_call = mock_safe_call
        data = await client.fetch_training_data()

        assert data["powerToWeight"] == ptw_yesterday
