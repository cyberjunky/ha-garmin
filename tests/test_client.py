"""Tests for GarminClient."""

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ha_garmin import GarminAuth, GarminClient
from ha_garmin.exceptions import GarminAPIError, GarminAuthError


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

    async def test_get_activities_by_recency(self):
        """Test get_activities queries by start/limit without date filters."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = [{"activityId": 1, "activityName": "Old Ride"}]

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = payload
            activities = await client.get_activities(0, 10)

        assert activities == payload
        params = mock_req.call_args.kwargs.get("params") or mock_req.call_args[0][2]
        assert params == {"start": 0, "limit": 10}

    async def test_get_scheduled_workouts_month_is_zero_indexed(self):
        """Garmin's calendar endpoint is 0-indexed for month; the public API isn't."""
        auth = _make_auth()
        client = GarminClient(auth)

        captured = {}

        async def fake_request(method, url):
            captured["url"] = url
            return {}

        with patch.object(client, "_request", side_effect=fake_request):
            await client.get_scheduled_workouts(2026, 1)
            assert captured["url"].endswith("/year/2026/month/0")

            await client.get_scheduled_workouts(2026, 12)
            assert captured["url"].endswith("/year/2026/month/11")

    async def test_get_scheduled_workouts_rejects_invalid_month(self):
        auth = _make_auth()
        client = GarminClient(auth)
        with pytest.raises(ValueError, match="month must be between 1 and 12"):
            await client.get_scheduled_workouts(2026, 13)
        with pytest.raises(ValueError, match="month must be between 1 and 12"):
            await client.get_scheduled_workouts(2026, 0)

    async def test_get_training_plans_hits_plans_url(self):
        from ha_garmin.const import TRAINING_PLANS_URL

        auth = _make_auth()
        client = GarminClient(auth)

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"plans": []}
            result = await client.get_training_plans()

        assert result == {"plans": []}
        mock_req.assert_awaited_once_with("GET", TRAINING_PLANS_URL)

    async def test_get_adaptive_training_plan_by_id_builds_url(self):
        from ha_garmin.const import ADAPTIVE_TRAINING_PLAN_URL

        auth = _make_auth()
        client = GarminClient(auth)

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"planId": 1789148356}
            result = await client.get_adaptive_training_plan_by_id(1789148356)

        assert result == {"planId": 1789148356}
        mock_req.assert_awaited_once_with(
            "GET", f"{ADAPTIVE_TRAINING_PLAN_URL}/1789148356"
        )

    async def test_get_adaptive_training_plan_by_id_rejects_non_positive(self):
        auth = _make_auth()
        client = GarminClient(auth)
        with pytest.raises(ValueError):
            await client.get_adaptive_training_plan_by_id(0)

    async def test_get_calendar_events_for_plan_passes_training_plan_id(self):
        from ha_garmin.const import CALENDAR_EVENTS_URL

        auth = _make_auth()
        client = GarminClient(auth)

        payload = [{"id": 29937926, "eventName": "5K Plan"}]
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = payload
            result = await client.get_calendar_events_for_plan(1789148356)

        assert result == payload
        mock_req.assert_awaited_once_with(
            "GET", CALENDAR_EVENTS_URL, params={"trainingPlanId": 1789148356}
        )

    async def test_fetch_activity_data_includes_scheduled_workouts(self):
        """fetch_activity_data surfaces workout-type calendar items only,
        across this month and next (home-assistant-garmin_connect#521).

        Real calendarItems mix several unrelated event types under
        `itemType` (weigh-ins, naps, workouts); only scheduled sessions
        ("workout" here; "fbtAdaptiveWorkout" is covered below, #595) are a
        Coach / adaptive-plan session or self-scheduled workout. Past items
        and other item types must not leak through.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        today_str = today.isoformat()
        next_month = today.month + 1 if today.month < 12 else 1
        next_month_year = today.year if today.month < 12 else today.year + 1
        next_month_date = date(next_month_year, next_month, 1).isoformat()

        this_month_payload = {
            "calendarItems": [
                {"itemType": "workout", "date": yesterday, "title": "Old Run"},
                {"itemType": "weight", "date": today_str, "weight": 80000.0},
                {
                    "itemType": "workout",
                    "date": today_str,
                    "title": "Benchmark Run",
                    "sportTypeKey": "running",
                    "workoutId": 111,
                    "atpPlanId": 222,
                    "protectedWorkoutSchedule": True,
                },
            ]
        }
        next_month_payload = {
            "calendarItems": [
                {"itemType": "workout", "date": next_month_date, "title": "Long Run"},
            ]
        }

        async def fake_get_scheduled_workouts(year, month):
            if (year, month) == (today.year, today.month):
                return this_month_payload
            if (year, month) == (next_month_year, next_month):
                return next_month_payload
            raise AssertionError(f"unexpected month requested: {year}-{month}")

        goal_event_payload = [
            {
                "eventName": "5K Plan",
                "date": "2026-11-21",
                "eventType": "running",
                "completionTarget": {"value": 5.0, "unit": "kilometer"},
                "eventCustomization": {
                    "trainingPlanType": "COACH_ATP",
                    "projectedRaceTimeDurationSeconds": 1829,
                    "predictedRaceTimeDurationSeconds": 2101,
                    "enrollmentTime": "2026-09-11T12:39:16.350",
                },
            }
        ]

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client,
                "get_scheduled_workouts",
                side_effect=fake_get_scheduled_workouts,
            ) as mock_calendar,
            patch.object(
                client, "get_calendar_events_for_plan", new_callable=AsyncMock
            ) as mock_goal,
        ):
            mock_acts.return_value = []
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_goal.return_value = goal_event_payload
            data = await client.fetch_activity_data()

        assert mock_calendar.await_count == 2
        dates = [w["date"] for w in data["scheduledWorkouts"]]
        assert dates == [today_str, next_month_date]
        assert data["todayScheduledWorkout"]["title"] == "Benchmark Run"
        assert data["nextScheduledWorkout"]["title"] == "Benchmark Run"
        assert data["nextScheduledWorkout"]["atpPlanId"] == 222

        mock_goal.assert_awaited_once_with(222)
        assert data["trainingPlanGoalEvent"]["eventName"] == "5K Plan"
        assert data["trainingPlanGoalEvent"]["targetDistance"] == 5.0
        assert data["trainingPlanGoalEvent"]["targetDistanceUnit"] == "kilometer"
        assert data["trainingPlanGoalEvent"]["trainingPlanType"] == "COACH_ATP"
        assert data["trainingPlanGoalEvent"]["projectedRaceTimeDurationSeconds"] == 1829

    @staticmethod
    def _calendar_request_router(
        calendar_items_by_month: dict[tuple[int, int], list[dict]],
        goal_events: list[dict] | None = None,
        events_params: list[dict] | None = None,
    ):
        """Fake `_request` that answers only the calendar endpoints.

        Everything above the HTTP layer (get_scheduled_workouts,
        get_calendar_events_for_plan, the filtering in fetch_activity_data)
        runs for real. Other endpoints fetch_activity_data touches get [].
        """
        from ha_garmin.const import CALENDAR_EVENTS_URL, CALENDAR_URL

        month_url = re.compile(re.escape(CALENDAR_URL) + r"/(\d+)/month/(\d+)$")

        async def fake_request(method, url, params=None):
            match = month_url.match(url)
            if match:
                year, zero_based_month = int(match[1]), int(match[2])
                items = calendar_items_by_month.get((year, zero_based_month + 1), [])
                return {"calendarItems": items}
            if url == CALENDAR_EVENTS_URL:
                if events_params is not None:
                    events_params.append(params)
                return goal_events or []
            return []

        return fake_request

    async def test_fetch_activity_data_includes_fbt_adaptive_workout(self):
        """A Daily Suggested / adaptive session is a scheduled workout
        too (home-assistant-garmin_connect#595).

        calendar-service reports it with itemType "fbtAdaptiveWorkout", not
        "workout". The item below is the one quoted in the issue, with only
        its date moved to today so the upcoming-only filter keeps it.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        today = date.today()
        today_str = today.isoformat()
        issue_595_item = {
            "id": 1789814567000,
            "trainingPlanId": 45489509,
            "itemType": "fbtAdaptiveWorkout",
            "title": "Basis",
            "date": "2026-09-19",
            "sportTypeKey": "running",
        }
        fbt_today = {**issue_595_item, "date": today_str}

        events_params: list[dict] = []
        fake_request = self._calendar_request_router(
            {(today.year, today.month): [fbt_today]}, events_params=events_params
        )
        with patch.object(client, "_request", side_effect=fake_request):
            data = await client.fetch_activity_data()

        expected = {
            "id": 1789814567000,
            "trainingPlanId": 45489509,
            "title": "Basis",
            "date": today_str,
            "sportTypeKey": "running",
        }
        assert data["scheduledWorkouts"] == [expected]
        assert data["todayScheduledWorkout"] == expected
        assert data["nextScheduledWorkout"] == expected
        # No atpPlanId on the item, so there is no goal event to look up.
        assert events_params == []
        assert data["trainingPlanGoalEvent"] == {}

    async def test_fetch_activity_data_goal_event_survives_fbt_first(self):
        """The goal event's plan id comes from the first upcoming item that
        carries an atpPlanId, not only from the next/today item.

        The fbt item in the snippet quoted in #595 shows no atpPlanId. With
        such an item today and an ATP "workout" later, the next item is the fbt
        one; the ATP plan's goal event must still be fetched from the later
        item.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        today = date.today()
        today_str = today.isoformat()
        tomorrow_str = (today + timedelta(days=1)).isoformat()
        fbt_today = {
            "id": 1789814567000,
            "trainingPlanId": 45489509,
            "itemType": "fbtAdaptiveWorkout",
            "title": "Basis",
            "date": today_str,
            "sportTypeKey": "running",
        }
        atp_tomorrow = {
            "id": 1789814568000,
            "itemType": "workout",
            "title": "Benchmark Run",
            "date": tomorrow_str,
            "sportTypeKey": "running",
            "workoutId": 111,
            "atpPlanId": 222,
        }
        goal_events = [
            {
                "eventName": "5K Plan",
                "date": "2026-11-21",
                "completionTarget": {"value": 5.0, "unit": "kilometer"},
                "eventCustomization": {"trainingPlanType": "COACH_ATP"},
            }
        ]

        events_params: list[dict] = []
        fake_request = self._calendar_request_router(
            {(today.year, today.month): [atp_tomorrow, fbt_today]},
            goal_events=goal_events,
            events_params=events_params,
        )
        with patch.object(client, "_request", side_effect=fake_request):
            data = await client.fetch_activity_data()

        assert [w["title"] for w in data["scheduledWorkouts"]] == [
            "Basis",
            "Benchmark Run",
        ]
        assert data["todayScheduledWorkout"]["title"] == "Basis"
        assert data["nextScheduledWorkout"]["title"] == "Basis"
        assert events_params == [{"trainingPlanId": 222}]
        assert data["trainingPlanGoalEvent"]["eventName"] == "5K Plan"
        assert data["trainingPlanGoalEvent"]["trainingPlanType"] == "COACH_ATP"

    async def test_fetch_activity_data_uses_recency_not_window(self):
        """Test fetch_activity_data returns lastActivity even for old activities (#519)."""
        auth = _make_auth()
        client = GarminClient(auth)

        old_activity = {
            "activityId": 42,
            "activityName": "Winter Run",
            "activityType": {"typeKey": "running"},
            "startTimeGMT": "2024-01-01T07:00:00",
            "hasPolyline": False,
        }

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
        ):
            mock_acts.return_value = [old_activity]
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            data = await client.fetch_activity_data()

        mock_acts.assert_awaited_once_with(0, GarminClient._RECENT_ACTIVITIES_LIMIT)
        assert data["lastActivity"]["activityId"] == 42
        assert len(data["lastActivities"]) == 1

    async def test_fetch_activity_data_returns_more_than_ten_recent(self):
        """lastActivities must not be capped at 10 (home-assistant-garmin_connect#567).

        A consumer that derives a rolling-7-day count from `lastActivities`
        needs the pool itself to hold more than a week's worth of activities
        for an active user, or that count silently pins at the fetch limit
        forever instead of tracking real activity.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        activities = [
            {
                "activityId": i,
                "activityName": f"Activity {i}",
                "activityType": {"typeKey": "running"},
                "startTimeGMT": "2026-01-01T07:00:00",
                "hasPolyline": False,
            }
            for i in range(15)
        ]

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
        ):
            mock_acts.return_value = activities
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            data = await client.fetch_activity_data()

        assert len(data["lastActivities"]) == 15

    async def test_fetch_activity_data_merges_ebike_fields(self):
        """Test fetch_activity_data merges e-bike fields from the summary endpoint (#527)."""
        auth = _make_auth()
        client = GarminClient(auth)

        ride = {
            "activityId": 7,
            "activityName": "E-Bike Ride",
            "activityType": {"typeKey": "e_bike_fitness"},
            "hasPolyline": False,
        }
        summary = {
            "activityId": 7,
            "eBikeBatteryRemaining": 42,
            "eBikeBatteryUsage": 19,
            "eBikeMaxAssistModes": 7,
            "eBikeAssistModeInfoDTOList": None,
        }

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_activity", new_callable=AsyncMock
            ) as mock_summary,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
        ):
            mock_acts.return_value = [ride]
            mock_summary.return_value = summary
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            data = await client.fetch_activity_data()

        mock_summary.assert_awaited_once_with(7)
        assert data["lastActivity"]["eBikeBatteryRemaining"] == 42
        assert data["lastActivity"]["eBikeBatteryUsage"] == 19
        assert data["lastActivity"]["eBikeMaxAssistModes"] == 7
        assert "eBikeAssistModeInfoDTOList" not in data["lastActivity"]

    async def test_fetch_activity_data_ebike_fields_retry_after_empty_poll(self):
        """Test an empty e-bike summary is not cached negatively forever (#527).

        A new activity's summary can lag behind Garmin's backend, so the
        first poll may come back without e-bike fields even though the
        activity does have them. The next poll for the same activity must
        retry the summary call and surface the fields once they appear,
        instead of being stuck with the first, empty result.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        ride = {
            "activityId": 9,
            "activityName": "E-Bike Ride",
            "activityType": {"typeKey": "e_bike_fitness"},
            "hasPolyline": False,
        }
        empty_summary = {"activityId": 9}
        full_summary = {
            "activityId": 9,
            "eBikeBatteryRemaining": 55,
            "eBikeBatteryUsage": 12,
            "eBikeMaxAssistModes": 5,
        }

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_activity", new_callable=AsyncMock
            ) as mock_summary,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
        ):
            mock_acts.return_value = [ride]
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}

            mock_summary.return_value = empty_summary
            first_poll = await client.fetch_activity_data()

            mock_summary.return_value = full_summary
            second_poll = await client.fetch_activity_data()

        assert "eBikeBatteryRemaining" not in first_poll["lastActivity"]
        assert second_poll["lastActivity"]["eBikeBatteryRemaining"] == 55
        assert second_poll["lastActivity"]["eBikeBatteryUsage"] == 12
        assert second_poll["lastActivity"]["eBikeMaxAssistModes"] == 5
        assert mock_summary.await_count == 2

    async def test_fetch_activity_data_ebike_fields_empty_retry_is_bounded(self):
        """Test a genuinely e-bike-field-less ride stops being retried.

        A regular (non ANT+ LEV) bike ride never gets e-bike fields from
        the summary endpoint. After a few empty polls the cache should
        stop calling the summary endpoint on every single poll, so a
        normal bike isn't penalized with an extra API call forever.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        ride = {
            "activityId": 10,
            "activityName": "Regular Bike Ride",
            "activityType": {"typeKey": "cycling"},
            "hasPolyline": False,
        }

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_activity", new_callable=AsyncMock
            ) as mock_summary,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
        ):
            mock_acts.return_value = [ride]
            mock_summary.return_value = {"activityId": 10}
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}

            retry_limit = client._EBIKE_FIELDS_EMPTY_RETRY_LIMIT
            for _ in range(retry_limit + 3):
                data = await client.fetch_activity_data()

        assert "eBikeBatteryRemaining" not in data["lastActivity"]
        assert mock_summary.await_count == retry_limit

    async def test_get_ebike_fields_concurrent_calls_do_not_race(self):
        """Overlapping callers must not let one clobber the other's result (#527).

        Regression test: two callers racing on the same activity_id used to
        both see an empty cache, both fetch, and whichever wrote last (even
        an empty/failed result) won -- silently discarding a concurrent
        successful fetch. The lock must serialize them so the second caller
        observes the first's finished, cached result instead of re-fetching.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        good_summary = {
            "activityId": 1,
            "eBikeBatteryRemaining": 64,
            "eBikeBatteryUsage": 8,
            "eBikeMaxAssistModes": 7,
        }
        call_count = 0

        async def slow_success(_activity_id):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # yield control so a second caller can start
            return good_summary

        with patch.object(client, "get_activity", side_effect=slow_success):
            results = await asyncio.gather(
                client._get_ebike_fields(1),
                client._get_ebike_fields(1),
            )

        # Second caller waited for the lock and got the cached result instead
        # of re-fetching -- one call, not two.
        assert call_count == 1
        expected = {
            "eBikeBatteryRemaining": 64,
            "eBikeBatteryUsage": 8,
            "eBikeMaxAssistModes": 7,
        }
        assert results[0] == expected
        assert results[1] == expected

    async def test_fetch_activity_data_skips_summary_for_non_rides(self):
        """Test fetch_activity_data does not fetch the summary for non-ride activities."""
        auth = _make_auth()
        client = GarminClient(auth)

        run = {
            "activityId": 8,
            "activityName": "Morning Run",
            "activityType": {"typeKey": "running"},
            "hasPolyline": False,
        }

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_activity", new_callable=AsyncMock
            ) as mock_summary,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
        ):
            mock_acts.return_value = [run]
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            data = await client.fetch_activity_data()

        mock_summary.assert_not_awaited()
        assert "eBikeBatteryRemaining" not in data["lastActivity"]

    def test_is_cycling_activity(self):
        """Test _is_cycling_activity recognizes ride type keys."""
        from ha_garmin.client import _is_cycling_activity

        for type_key in (
            "cycling",
            "road_biking",
            "mountain_biking",
            "gravel_cycling",
            "e_bike_fitness",
            "e_bike_mountain",
            "virtual_ride",
            "indoor_cycling",
        ):
            assert _is_cycling_activity({"activityType": {"typeKey": type_key}})

        for type_key in ("running", "lap_swimming", "strength_training", None):
            assert not _is_cycling_activity({"activityType": {"typeKey": type_key}})
        assert not _is_cycling_activity({})

    async def test_get_body_composition_falls_back_to_weight_latest(self):
        """Test get_body_composition uses weight/latest when the 30-day window is empty."""
        auth = _make_auth()
        client = GarminClient(auth)

        latest_payload = {"weight": 89400.0, "bmi": 26.4, "bodyFat": 23.6}

        async def fake_request(method, url, params=None, **kwargs):
            if "weight/latest" in url:
                return latest_payload
            return {"dailyWeightSummaries": [], "totalAverage": {}}

        with patch.object(client, "_request", side_effect=fake_request):
            body = await client.get_body_composition(date(2026, 7, 11))

        assert body == latest_payload

    async def test_fetch_blood_pressure_uses_year_window(self):
        """Test fetch_blood_pressure_data queries a 365-day range."""
        auth = _make_auth()
        client = GarminClient(auth)

        with patch.object(
            client, "get_blood_pressure", new_callable=AsyncMock
        ) as mock_bp:
            mock_bp.return_value = {}
            await client.fetch_blood_pressure_data(date(2026, 7, 11))

        start, end = mock_bp.call_args[0]
        assert end == date(2026, 7, 11)
        assert (end - start).days == 365

    def test_trim_activity_keeps_ebike_fields(self):
        """Test _trim_activity preserves e-bike fields and drops unknown keys."""
        from ha_garmin.client import _trim_activity

        activity = {
            "activityId": 2,
            "activityType": {"typeKey": "e_bike_fitness"},
            "eBikeBatteryRemaining": 42,
            "eBikeBatteryUsage": 19,
            "eBikeMaxAssistModes": 7,
            "eBikeAssistModeInfoDTOList": None,
            "someUnknownField": "dropped",
        }

        trimmed = _trim_activity(activity)

        assert trimmed["eBikeBatteryRemaining"] == 42
        assert trimmed["eBikeBatteryUsage"] == 19
        assert trimmed["eBikeMaxAssistModes"] == 7
        assert trimmed["activityType"] == "e_bike_fitness"
        assert "eBikeAssistModeInfoDTOList" not in trimmed
        assert "someUnknownField" not in trimmed

    def test_trim_activity_no_ebike_fields_absent(self):
        """Test _trim_activity does not inject e-bike keys for non-e-bike activities."""
        from ha_garmin.client import _trim_activity

        trimmed = _trim_activity(
            {"activityId": 3, "activityType": {"typeKey": "running"}}
        )

        assert "eBikeBatteryRemaining" not in trimmed
        assert "eBikeBatteryUsage" not in trimmed
        assert "eBikeMaxAssistModes" not in trimmed

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

    async def test_get_device_solar_data(self):
        """Test get_device_solar_data unwraps deviceSolarInput."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = {
            "deviceSolarInput": {
                "deviceId": 123,
                "solarDailyDataDTOs": [
                    {
                        "solarInputReadings": [
                            {
                                "readingTimestampGmt": "2026-07-09T10:00:00.0",
                                "solarUtilization": 42.5,
                                "activityTimeGainMs": 60000,
                            }
                        ]
                    }
                ],
            }
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            solar = await client.get_device_solar_data(123)

        readings = solar["solarDailyDataDTOs"][0]["solarInputReadings"]
        assert readings[0]["solarUtilization"] == 42.5

    async def test_get_device_solar_data_not_solar(self):
        """Test get_device_solar_data returns empty dict for non-solar devices."""
        auth = _make_auth()
        client = GarminClient(auth)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response({})
            solar = await client.get_device_solar_data(123)

        assert solar == {}

    async def test_download_activity_gpx(self):
        """Test download_activity returns raw bytes for export formats."""
        auth = _make_auth()
        client = GarminClient(auth)

        gpx_bytes = b'<?xml version="1.0"?><gpx></gpx>'
        resp = MagicMock()
        resp.status_code = 200
        resp.content = gpx_bytes

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = resp
            data = await client.download_activity(12345, "gpx")

        assert data == gpx_bytes

    async def test_download_activity_fit_extracts_zip(self):
        """Test download_activity fit format extracts the file from the zip."""
        import io
        import zipfile

        auth = _make_auth()
        client = GarminClient(auth)

        fit_bytes = b"\x0e\x10fake-fit-content"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("12345.fit", fit_bytes)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = buf.getvalue()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            # First to_thread call is the HTTP request, second is zip extraction
            def dispatch(func, *args, **kwargs):
                if mock_thread.call_count == 1:
                    return resp
                return func(*args, **kwargs)

            mock_thread.side_effect = dispatch
            data = await client.download_activity(12345, "fit")

        assert data == fit_bytes

    async def test_download_activity_invalid_format(self):
        """Test download_activity rejects unknown formats."""
        auth = _make_auth()
        client = GarminClient(auth)

        with pytest.raises(ValueError, match="Invalid file format"):
            await client.download_activity(12345, "pdf")

    async def test_get_device_last_used(self):
        """Test get_device_last_used converts upload time to UTC datetime."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = {
            "userDeviceId": 3627212773,
            "lastUsedDeviceApplicationKey": "venu4",
            "lastUsedDeviceName": "Venu 4 - 45mm",
            "lastUsedDeviceUploadTime": 1783686462000,
            "imageUrl": "https://example.com/venu4.png",
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            last_used = await client.get_device_last_used()

        assert last_used["lastUsedDeviceName"] == "Venu 4 - 45mm"
        assert "lastUsedDeviceUploadTime" not in last_used
        sync_time = last_used["lastSyncTime"]
        assert sync_time == datetime.fromtimestamp(1783686462, tz=UTC)
        assert sync_time.tzinfo is not None

    def test_trim_device_keeps_essentials(self):
        """Test _trim_device keeps identity fields and drops capability flags."""
        from ha_garmin.client import _trim_device

        device = {
            "deviceId": 3627212773,
            "unitId": 3627212773,
            "displayName": "Venu 4 - 45mm",
            "productDisplayName": "Venu 4 - 45mm",
            "applicationKey": "venu4",
            "serialNumber": "8PM119893",
            "partNumber": "006-B4643-00",
            "productSku": "010-03014-00",
            "imageUrl": "https://example.com/venu4.png",
            "primary": True,
            "primaryActivityTrackerIndicator": True,
            "deviceCategories": ["FITNESS", "WELLNESS"],
            "wifi": True,
            "runningWorkoutCapable": True,
            "minGCMAndroidVersion": 10149,
            "bestInClassVideoLink": None,
        }

        trimmed = _trim_device(device)

        assert trimmed["deviceId"] == 3627212773
        assert trimmed["serialNumber"] == "8PM119893"
        assert trimmed["deviceCategories"] == ["FITNESS", "WELLNESS"]
        assert "runningWorkoutCapable" not in trimmed
        assert "minGCMAndroidVersion" not in trimmed
        assert "bestInClassVideoLink" not in trimmed

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
            "bodyBatteryActivityEventList": [
                {"eventType": "SLEEP", "timezoneOffset": 7200000}
            ],
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
                "sleepNeed": {"actual": 470},
                "nextSleepNeed": {
                    "actual": 470,
                    "recommendedBedtimeStartMins": 1360,
                    "recommendedBedtimeEndMins": 1380,
                },
                "sleepAlignment": {
                    "optimalSleepWindowStartMins": 10,
                    "optimalSleepWindowEndMins": 20,
                },
                "sleepScores": {"overall": {"value": 85}},
                "averageRespirationValue": 14.2,
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
            data = await client.fetch_core_data(date(2026, 4, 12))

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
        assert data["bedtime"] == datetime(2026, 4, 11, 20, 0, 8, tzinfo=UTC)
        assert data["optimalBedtime"] == datetime(2026, 4, 12, 20, 40, tzinfo=UTC)
        assert data["wakeTime"] == datetime(2026, 4, 12, 3, 57, 47, tzinfo=UTC)
        assert data["optimalWakeTime"] == datetime(2026, 4, 13, 4, 30, tzinfo=UTC)
        assert data["avgSleepRespirationValue"] == 14.2

    async def test_fetch_core_data_bedtime_uses_gmt_local_delta_for_offset(self):
        """bedtime/wake_time must not silently assume UTC+0 (home-assistant-garmin_connect#564).

        Real-world payloads have been seen with no explicit timezoneOffset
        field in dailySleepDTO and no SLEEP event in
        bodyBatteryActivityEventList either -- the previous fallback chain
        silently defaulted to a 0 offset in that case, storing the local
        wall-clock time mislabeled as UTC. Home Assistant's own UTC-to-local
        display conversion then shifted it by the viewer's offset *again*,
        showing bedtime/wake_time hours later than reality. The GMT/Local
        timestamp pair Garmin always sends alongside each sleep timestamp
        must be used instead of falling through to 0.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        profile_payload = {"id": 1, "profileId": 2, "displayName": "testuser"}
        summary_payload = {
            "dailyStepGoal": 10000,
            "totalSteps": 5000,
            "totalDistanceMeters": 4000,
            # Deliberately no bodyBatteryActivityEventList / timezoneOffset
            # anywhere -- the exact shape that used to default to 0.
        }
        steps_payload = []

        # Real UTC instants for a French UTC+2 (CEST) user: bedtime 22:44,
        # wake 07:06 local.
        gmt_start = datetime(2026, 8, 26, 20, 44, 0, tzinfo=UTC)
        gmt_end = datetime(2026, 8, 27, 5, 6, 0, tzinfo=UTC)
        offset = timedelta(minutes=120)

        sleep_payload = {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int(gmt_start.timestamp() * 1000),
                "sleepStartTimestampLocal": int(
                    (gmt_start + offset).timestamp() * 1000
                ),
                "sleepEndTimestampGMT": int(gmt_end.timestamp() * 1000),
                "sleepEndTimestampLocal": int((gmt_end + offset).timestamp() * 1000),
                "sleepScores": {"overall": {"value": 84}},
            }
        }

        responses = [
            _mock_response(profile_payload),
            _mock_response(summary_payload),
            _mock_response(steps_payload),
            _mock_response(sleep_payload),
        ]

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = responses
            data = await client.fetch_core_data(date(2026, 8, 27))

        # Stored as the true UTC instant, so a UTC+2 viewer's own display
        # conversion correctly lands back on 22:44 / 07:06, not 00:44 / 09:06.
        assert data["bedtime"] == gmt_start
        assert data["wakeTime"] == gmt_end

    async def test_fetch_core_data_transient_error_does_not_use_yesterday(self):
        """Test a transient 502/503 does not get papered over with yesterday's summary.

        Regression test for cyberjunky/home-assistant-garmin_connect#536: a
        transient API failure while fetching today's summary must not be
        treated the same as "today's data isn't ready yet", or fast-changing
        fields like body battery briefly flip to a stale, day-old value.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        yesterday_summary = {
            "dailyStepGoal": 10000,
            "bodyBatteryMostRecentValue": 33,
        }

        async def fake_get_summary(target_date):
            if target_date == date(2026, 4, 12):
                raise GarminAPIError("Server error 502 after 3 retries", 502)
            return yesterday_summary

        with (
            patch.object(
                client,
                "_get_user_summary_raw",
                new_callable=AsyncMock,
                side_effect=fake_get_summary,
            ) as mock_summary,
            patch.object(
                client, "get_daily_steps", new_callable=AsyncMock
            ) as mock_steps,
            patch.object(
                client, "_get_sleep_data_raw", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            mock_steps.return_value = None
            mock_sleep.return_value = None
            data = await client.fetch_core_data(date(2026, 4, 12))

        # Today's failed fetch must not be silently replaced by yesterday's
        # full summary - the stale body battery value must not leak through.
        mock_summary.assert_awaited_once_with(date(2026, 4, 12))
        assert "bodyBatteryMostRecentValue" not in data
        assert "dailyStepGoal" not in data

    async def test_fetch_core_data_midnight_fallback_still_works(self):
        """Test the legitimate "today not ready yet" fallback to yesterday still works.

        When today's endpoint responds but has no data yet (e.g. right after
        midnight, before Garmin rolls the calendar day over), falling back to
        yesterday's summary is intentional and must keep working.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        yesterday_summary = {
            "dailyStepGoal": 10000,
            "bodyBatteryMostRecentValue": 87,
        }

        async def fake_get_summary(target_date):
            if target_date == date(2026, 4, 12):
                return {}
            return yesterday_summary

        with (
            patch.object(
                client,
                "_get_user_summary_raw",
                new_callable=AsyncMock,
                side_effect=fake_get_summary,
            ) as mock_summary,
            patch.object(
                client, "get_daily_steps", new_callable=AsyncMock
            ) as mock_steps,
            patch.object(
                client, "_get_sleep_data_raw", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            mock_steps.return_value = None
            mock_sleep.return_value = None
            data = await client.fetch_core_data(date(2026, 4, 12))

        assert mock_summary.await_args_list == [
            call(date(2026, 4, 12)),
            call(date(2026, 4, 11)),
        ]
        assert data["bodyBatteryMostRecentValue"] == 87
        assert data["dailyStepGoal"] == 10000

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

    async def test_add_nutrition_log_builds_correct_payload(self):
        """Test add_nutrition_log sends PUT with mealDate+quickAddItems wrapper."""
        auth = _make_auth()
        client = GarminClient(auth)

        put_payloads = []

        async def fake_put(url, payload):
            put_payloads.append((url, payload))
            return {"mealDate": "2026-04-18"}

        async def fake_get_meal(*_a, **_kw):
            return (942305, "07:00:00")

        client._put_request = fake_put
        client._get_nutrition_meal = fake_get_meal

        result = await client.add_nutrition_log(
            calories=500,
            carbs=60,
            protein=30,
            fat=15,
            name="Lunch",
            meal_time="12:00:00",
            timestamp="2026-04-18T20:21:52.000",
        )

        assert result == {"mealDate": "2026-04-18"}
        assert len(put_payloads) == 1
        url, payload = put_payloads[0]
        assert "nutrition-service/food/logs/quickAdd" in url
        assert payload["mealDate"] == "2026-04-18"
        assert "quickAddItems" in payload
        entry = payload["quickAddItems"][0]
        assert entry["logCategory"] == "QUICK_ADD"
        assert entry["action"] == "ADD"
        assert entry["logSource"] == "GCW"
        assert entry["calories"] == "500"
        assert entry["carbs"] == "60"
        assert entry["protein"] == "30"
        assert entry["fat"] == "15"
        assert entry["name"] == "Lunch"
        assert entry["mealTime"] == "12:00:00"
        assert entry["logTimestamp"].endswith("Z")

    async def test_add_nutrition_log_empty_macros_are_empty_string(self):
        """Test add_nutrition_log sends empty string for omitted macros."""
        auth = _make_auth()
        client = GarminClient(auth)

        put_payloads = []

        async def fake_put(url, payload):
            put_payloads.append((url, payload))
            return {}

        async def fake_get_meal(*_a, **_kw):
            return (942305, "07:00:00")

        client._put_request = fake_put
        client._get_nutrition_meal = fake_get_meal

        await client.add_nutrition_log(calories=200)

        entry = put_payloads[0][1]["quickAddItems"][0]
        assert entry["calories"] == "200"
        assert entry["carbs"] == ""
        assert entry["protein"] == ""
        assert entry["fat"] == ""

    async def test_set_hydration_normalizes_timestamp_without_milliseconds(self):
        """A timestamp with no milliseconds must still be accepted and padded to .000."""
        auth = _make_auth()
        client = GarminClient(auth)

        put_payloads = []

        async def fake_put(url, payload):
            put_payloads.append((url, payload))
            return {}

        client._put_request = fake_put

        await client.set_hydration(500, timestamp="2024-01-15T08:30:00")

        payload = put_payloads[0][1]
        assert payload["timestampLocal"] == "2024-01-15T08:30:00.000"

    async def test_set_hydration_preserves_provided_milliseconds(self):
        """A timestamp that already has sub-second precision keeps millisecond precision."""
        auth = _make_auth()
        client = GarminClient(auth)

        put_payloads = []

        async def fake_put(url, payload):
            put_payloads.append((url, payload))
            return {}

        client._put_request = fake_put

        await client.set_hydration(500, timestamp="2024-01-15T08:30:00.123456")

        payload = put_payloads[0][1]
        assert payload["timestampLocal"] == "2024-01-15T08:30:00.123"

    async def test_set_hydration_defaults_timestamp_to_now(self):
        """Omitting the timestamp still produces a millisecond-precision value."""
        auth = _make_auth()
        client = GarminClient(auth)

        put_payloads = []

        async def fake_put(url, payload):
            put_payloads.append((url, payload))
            return {}

        client._put_request = fake_put

        await client.set_hydration(500)

        payload = put_payloads[0][1]
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}", payload["timestampLocal"]
        )

    async def test_set_hydration_builds_correct_payload(self):
        """set_hydration sends a PUT with calendarDate/timestampLocal/valueInML."""
        auth = _make_auth()
        client = GarminClient(auth)

        put_payloads = []

        async def fake_put(url, payload):
            put_payloads.append((url, payload))
            return {"success": True}

        client._put_request = fake_put

        result = await client.set_hydration(
            750, cdate="2026-04-18", timestamp="2026-04-18T20:21:52"
        )

        assert result == {"success": True}
        assert len(put_payloads) == 1
        url, payload = put_payloads[0]
        assert "hydration" in url.lower()
        assert payload["calendarDate"] == "2026-04-18"
        assert payload["timestampLocal"] == "2026-04-18T20:21:52.000"
        assert payload["valueInML"] == 750

    async def test_set_hydration_rejects_value_over_limit(self):
        """Values beyond +/-10000 mL must raise ValueError before any request is made."""
        auth = _make_auth()
        client = GarminClient(auth)
        client._put_request = AsyncMock()

        with pytest.raises(ValueError, match="10000"):
            await client.set_hydration(10001)

        client._put_request.assert_not_called()

    async def test_set_blood_pressure_includes_pulse_when_given(self):
        """set_blood_pressure includes pulse in the payload when provided."""
        auth = _make_auth()
        client = GarminClient(auth)

        post_payloads = []

        async def fake_post(url, payload):
            post_payloads.append((url, payload))
            return {"success": True}

        client._post_request = fake_post

        await client.set_blood_pressure(120, 80, pulse=65)

        assert len(post_payloads) == 1
        _, payload = post_payloads[0]
        assert payload["systolic"] == 120
        assert payload["diastolic"] == 80
        assert payload["pulse"] == 65

    async def test_set_blood_pressure_omits_pulse_when_not_given(self):
        """set_blood_pressure works without a pulse, matching Garmin Connect's own UI."""
        auth = _make_auth()
        client = GarminClient(auth)

        post_payloads = []

        async def fake_post(url, payload):
            post_payloads.append((url, payload))
            return {"success": True}

        client._post_request = fake_post

        result = await client.set_blood_pressure(120, 80)

        assert result == {"success": True}
        _, payload = post_payloads[0]
        assert "pulse" not in payload

    async def test_get_nutrition_log_returns_dict(self):
        """Test get_nutrition_log returns dict from API response."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = {
            "dailyNutritionContent": {"calories": 500},
            "mealDetails": [],
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            result = await client.get_nutrition_log()

        assert result == payload

    async def test_get_nutrition_log_404_returns_empty(self):
        """Test get_nutrition_log returns {} on 404 (no Connect+)."""
        auth = _make_auth()
        client = GarminClient(auth)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response({}, status=404)
            result = await client.get_nutrition_log()

        assert result == {}

    async def test_fetch_nutrition_data_full_day(self):
        """Test fetch_nutrition_data transforms a real Connect+ nutrition log."""
        auth = _make_auth()
        client = GarminClient(auth)

        log_payload = {
            "mealDate": "2026-07-06",
            "dailyNutritionGoals": {
                "calories": 1650,
                "carbs": 165.0,
                "fat": 55.0,
                "protein": 124.0,
            },
            "dailyNutritionContent": {
                "calories": 902,
                "carbs": 91.0,
                "fat": 53.0,
                "protein": 15.0,
            },
            "mealDetails": [
                {
                    "meal": {"mealName": "BREAKFAST"},
                    "mealNutritionContent": {
                        "calories": 2,
                        "carbs": 0.0,
                        "protein": 0.0,
                        "fat": 0.0,
                    },
                    "loggedFoods": [
                        {"logTimestamp": "2026-07-06T06:00:00.000Z"},
                    ],
                },
                {
                    "meal": {"mealName": "LUNCH"},
                    "mealNutritionContent": {
                        "calories": 650,
                        "carbs": 54.0,
                        "protein": 11.0,
                        "fat": 43.0,
                    },
                    "loggedFoods": [
                        {"logTimestamp": "2026-07-06T10:56:43.000Z"},
                    ],
                },
                {
                    "meal": {"mealName": "DINNER"},
                    "mealNutritionContent": {
                        "calories": 250,
                        "carbs": 37.0,
                        "protein": 4.0,
                        "fat": 10.0,
                    },
                    "loggedFoods": [
                        {"logTimestamp": "2026-07-06T17:42:31.000Z"},
                    ],
                },
                {
                    "meal": {"mealName": "SNACKS"},
                    "loggedFoods": [],
                },
            ],
        }

        with patch.object(
            client, "get_nutrition_log", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = log_payload
            data = await client.fetch_nutrition_data()

        assert data["nutritionConsumedCalories"] == 902
        assert data["nutritionConsumedProtein"] == 15.0
        assert data["nutritionConsumedFat"] == 53.0
        assert data["nutritionConsumedCarbs"] == 91.0
        assert data["nutritionCalorieGoal"] == 1650
        assert data["nutritionProteinGoal"] == 124.0
        assert data["nutritionFatGoal"] == 55.0
        assert data["nutritionCarbsGoal"] == 165.0
        assert data["nutritionRemainingCalories"] == 748
        assert data["nutritionLoggedEntries"] == 3
        assert data["nutritionLastLoggedTime"] == datetime(
            2026, 7, 6, 17, 42, 31, tzinfo=UTC
        )
        assert len(data["nutritionMeals"]) == 4
        assert data["nutritionMeals"][0] == {
            "meal": "BREAKFAST",
            "calories": 2,
            "protein": 0.0,
            "fat": 0.0,
            "carbs": 0.0,
            "entries": 1,
        }
        assert data["nutritionMeals"][1]["entries"] == 1
        assert data["nutritionMeals"][3]["entries"] == 0

    async def test_fetch_nutrition_data_empty_day(self):
        """Test fetch_nutrition_data returns zeros for an empty day."""
        auth = _make_auth()
        client = GarminClient(auth)

        log_payload = {
            "dailyNutritionGoals": {
                "calories": 2400,
                "protein": 140.0,
                "fat": 80.0,
                "carbs": 270.0,
            },
            "dailyNutritionContent": {
                "calories": 0,
                "protein": 0.0,
                "fat": 0.0,
                "carbs": 0.0,
            },
            "mealDetails": [
                {
                    "meal": {"mealName": "BREAKFAST"},
                    "mealNutritionContent": {
                        "calories": 0,
                        "protein": 0.0,
                        "fat": 0.0,
                        "carbs": 0.0,
                    },
                    "loggedFoods": [],
                },
            ],
        }

        with patch.object(
            client, "get_nutrition_log", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = log_payload
            data = await client.fetch_nutrition_data()

        assert data["nutritionConsumedCalories"] == 0
        assert data["nutritionConsumedProtein"] == 0.0
        assert data["nutritionLoggedEntries"] == 0
        assert data["nutritionLastLoggedTime"] is None

    async def test_fetch_nutrition_data_unavailable(self):
        """Test fetch_nutrition_data returns {} when nutrition is unavailable."""
        auth = _make_auth()
        client = GarminClient(auth)

        with patch.object(
            client, "get_nutrition_log", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {}
            data = await client.fetch_nutrition_data()

        assert data == {}

    async def test_get_menstrual_calendar_backwards_dates(self):
        """Test get_menstrual_calendar raises ValueError if start_date > end_date."""
        auth = _make_auth()
        client = GarminClient(auth)

        start_date = date(2026, 6, 10)
        end_date = date(2026, 6, 1)

        with pytest.raises(ValueError, match="start_date cannot be after end_date"):
            await client.get_menstrual_calendar(
                start_date=start_date, end_date=end_date
            )

    async def test_get_menstrual_calendar_clamps_end_date(self):
        """Test get_menstrual_calendar clamps end_date to a maximum of 92 days."""
        auth = _make_auth()
        client = GarminClient(auth)

        start_date = date(2026, 1, 1)
        end_date = date(2026, 12, 31)
        expected_clamped_date = start_date + timedelta(days=92)

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"status": "ok"}
            await client.get_menstrual_calendar(
                start_date=start_date, end_date=end_date
            )

            mock_request.assert_called_once()
            called_url = mock_request.call_args[0][1]

            assert expected_clamped_date.isoformat() in called_url
            assert end_date.isoformat() not in called_url

    async def test_get_menstrual_calendar_url_formatting(self):
        """Test get_menstrual_calendar formats the path URL correctly."""
        auth = _make_auth()
        client = GarminClient(auth)

        start_date = date(2026, 5, 8)
        end_date = date(2026, 8, 6)  # Exactly 90 days diff

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"status": "ok"}
            await client.get_menstrual_calendar(
                start_date=start_date, end_date=end_date
            )

            mock_request.assert_called_once()
            called_url = mock_request.call_args[0][1]

            expected_path_ending = (
                f"/calendar/{start_date.isoformat()}/{end_date.isoformat()}"
            )
            assert called_url.endswith(expected_path_ending)

    # ------------------------------------------------------------------ #
    #  Security hardening                                                  #
    # ------------------------------------------------------------------ #

    async def test_ensure_token_fresh_serializes_concurrent_refresh(self):
        """Only one concurrent caller should perform the actual token refresh."""
        import asyncio

        auth = _make_auth()
        client = GarminClient(auth)

        refresh_calls = []

        def fake_refresh() -> None:
            refresh_calls.append(len(refresh_calls))

        # First two outside-lock checks are True; once inside the lock the
        # refresh runs and the next inside-lock check is False, so the second
        # waiter skips the network request.
        expires_side = [True, True, False]

        with (
            patch.object(
                auth, "_token_expires_soon", side_effect=expires_side
            ) as mock_expires,
            patch.object(auth, "_refresh_with_lock", side_effect=fake_refresh),
        ):
            await asyncio.gather(
                client._ensure_token_fresh(), client._ensure_token_fresh()
            )

        # Two outside-lock checks + one re-check under lock = 3 calls.
        assert mock_expires.call_count == 3
        # Only the first caller performed the actual refresh.
        assert len(refresh_calls) == 1

    @pytest.mark.parametrize(
        "method,args",
        [
            ("get_activity", ("abc",)),
            ("get_activity_details", (-1,)),
            ("get_activity_hr_in_timezones", (0,)),
            ("download_activity", (-5, "fit")),
            ("get_device_solar_data", ("device",)),
            ("get_device_settings", (0,)),
        ],
    )
    async def test_integer_path_ids_rejected(self, method, args):
        """Methods that take integer IDs must reject non-positive integers."""
        auth = _make_auth()
        client = GarminClient(auth)

        with pytest.raises(ValueError, match="must be a positive integer"):
            await getattr(client, method)(*args)

    @pytest.mark.parametrize(
        "method,args",
        [
            ("get_gear_stats", ("../../../etc/passwd",)),
            ("set_active_gear", ("running", "set as default", "not-a-uuid")),
            ("add_gear_to_activity", ("bad-uuid", 12345)),
        ],
    )
    async def test_uuid_path_ids_rejected(self, method, args):
        """Methods that take gear UUIDs must reject malformed UUIDs."""
        auth = _make_auth()
        client = GarminClient(auth)

        with pytest.raises(ValueError, match="must be a valid UUID"):
            await getattr(client, method)(*args)

    async def test_uuid_path_accepts_valid_uuid(self):
        """A well-formed UUID must be accepted for gear UUID path segments."""
        auth = _make_auth()
        client = GarminClient(auth)

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"gearStats": {}}
            await client.get_gear_stats("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
            assert (
                "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11" in mock_request.call_args[0][1]
            )


class TestSecurityAuditHardening:
    """Regression tests for the python-garminconnect audit cross-check."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://connectapi.garmin.com/a/../../secret",
            "https://connectapi.garmin.com/a/%2e%2e/%2e%2e/secret",
            "https://connectapi.garmin.com/a/%2E%2E/secret",
            "/gear-service/gear/../admin",
        ],
    )
    def test_assert_safe_url_rejects_traversal(self, url):
        from ha_garmin.client import _assert_safe_url

        with pytest.raises(ValueError, match="Invalid API URL"):
            _assert_safe_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://connectapi.garmin.com/userprofile-service/socialProfile",
            # dots inside a segment are not traversal
            "https://connectapi.garmin.com/a/first..last",
        ],
    )
    def test_assert_safe_url_accepts_legitimate(self, url):
        from ha_garmin.client import _assert_safe_url

        _assert_safe_url(url)

    async def test_get_gear_defaults_validates_profile_id(self):
        auth = _make_auth()
        client = GarminClient(auth)
        with pytest.raises(ValueError, match="user_profile_id"):
            await client.get_gear_defaults("1/../../admin")

    async def test_fetch_gear_data_includes_sensors(self):
        """fetch_gear_data must surface paired ANT+/BLE sensors (home-assistant-garmin_connect#535)."""
        auth = _make_auth()
        client = GarminClient(auth)

        profile = MagicMock()
        profile.profile_id = 999

        sensor_payload = [
            {
                "deviceId": 111,
                "sensorType": "HEART_RATE",
                "batteryStatus": "good",
                "batteryLevel": 82,
            },
            {
                "deviceId": 222,
                "sensorType": "BIKE_POWER",
                "batteryStatus": "low",
                "batteryLevel": 15,
            },
        ]

        with (
            patch.object(client, "get_user_profile", return_value=profile),
            patch.object(client, "get_gear", return_value=[]),
            patch.object(client, "get_gear_defaults", return_value=[]),
            patch.object(client, "get_devices", return_value=[]),
            patch.object(client, "get_device_last_used", return_value={}),
            patch.object(client, "get_device_alarms", return_value=[]),
            patch.object(client, "get_sensors", return_value=sensor_payload),
        ):
            data = await client.fetch_gear_data()

        assert data["sensors"] == sensor_payload

    async def test_fetch_gear_data_aggregates_solar_readings(self):
        """Solar intensity must aggregate the whole day, not just the latest reading.

        The latest reading alone reflects only the moment of the last sync,
        which is way off from the day as a whole -- e.g. syncing in the
        evening reads near 0% even on a sunny day
        (home-assistant-garmin_connect#508).
        """
        auth = _make_auth()
        client = GarminClient(auth)

        profile = MagicMock()
        profile.profile_id = 999

        device = {"deviceId": 456, "productDisplayName": "Instinct 2X Solar"}

        solar_payload = {
            "solarDailyDataDTOs": [
                {
                    "solarInputReadings": [
                        {
                            "solarUtilization": 0,
                            "activityTimeGainMs": 0,
                            "readingTimestampGmt": "2026-04-12T06:00:00.0",
                        },
                        {
                            "solarUtilization": 45,
                            "activityTimeGainMs": 300000,
                            "readingTimestampGmt": "2026-04-12T12:00:00.0",
                        },
                        {
                            "solarUtilization": 80,
                            "activityTimeGainMs": 600000,
                            "readingTimestampGmt": "2026-04-12T14:00:00.0",
                        },
                        # Evening sync -- this is the only reading the old
                        # "latest" logic surfaced.
                        {
                            "solarUtilization": 2,
                            "activityTimeGainMs": 0,
                            "readingTimestampGmt": "2026-04-12T20:00:00.0",
                        },
                    ]
                }
            ]
        }

        with (
            patch.object(client, "get_user_profile", return_value=profile),
            patch.object(client, "get_gear", return_value=[]),
            patch.object(client, "get_gear_defaults", return_value=[]),
            patch.object(client, "get_devices", return_value=[device]),
            patch.object(client, "get_device_last_used", return_value={}),
            patch.object(client, "get_device_alarms", return_value=[]),
            patch.object(client, "get_sensors", return_value=[]),
            patch.object(client, "get_device_solar_data", return_value=solar_payload),
        ):
            data = await client.fetch_gear_data()

        entry = data["solarIntensity"][0]
        assert entry["solarUtilization"] == 2  # latest reading, unchanged
        assert entry["avgSolarUtilization"] == 31.8  # (0+45+80+2)/4
        assert entry["totalActivityTimeGainMinutes"] == 15  # (300000+600000)/60000

    async def test_set_active_gear_rejects_unknown_activity_type(self):
        auth = _make_auth()
        client = GarminClient(auth)
        with pytest.raises(ValueError, match="Unknown activity_type"):
            await client.set_active_gear("running/../../x", "set as default", "9" * 32)

    async def test_user_summary_quotes_display_name(self):
        """A server-supplied display name must not alter the request path."""
        auth = _make_auth()
        client = GarminClient(auth)
        profile = MagicMock()
        profile.display_name = "user/../admin"
        captured = {}

        async def fake_request(method, url, params=None):
            captured["url"] = url
            return {}

        with (
            patch.object(client, "get_user_profile", return_value=profile),
            patch.object(client, "_request", side_effect=fake_request),
        ):
            await client._get_user_summary_raw(date(2026, 1, 1))

        assert "user/../admin" not in captured["url"]
        assert "user%2F..%2Fadmin" in captured["url"]

    def test_sanitize_filename_strips_header_breakout_chars(self):
        from ha_garmin.client import _sanitize_filename

        assert _sanitize_filename('a"\r\nb\\c.fit') == "a___b_c.fit"
        assert _sanitize_filename("normal.fit") == "normal.fit"
