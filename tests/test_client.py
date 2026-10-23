"""Tests for GarminClient."""

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ha_garmin import GarminAuth, GarminClient
from ha_garmin.const import GARMIN_CONNECT_API
from ha_garmin.exceptions import GarminAPIError, GarminAuthError, GarminTLSError
from ha_garmin.models import UserProfile

_PROFILE = UserProfile(id=1, profileId=82413233, displayName="tester")


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


# Connect IQ payloads as reported for an indoor rowing session recorded
# with the FTMSRowing app (chart-only fields and other locales omitted).
_CIQ_APP = "f201a31a-79c2-4f81-b4be-28a80603132e"
_CIQ_MEASUREMENTS = [
    {"appID": _CIQ_APP, "developerFieldNumber": 10, "value": "FIT-RW-"},
    {"appID": _CIQ_APP, "developerFieldNumber": 14, "value": "491.0"},
    {"appID": _CIQ_APP, "developerFieldNumber": 16, "value": "30.0"},
    {"appID": _CIQ_APP, "developerFieldNumber": 18, "value": "17.0"},
    {"appID": _CIQ_APP, "developerFieldNumber": 11, "value": "2946.0"},
    {"appID": _CIQ_APP, "developerFieldNumber": 12, "value": "2.946000099182129"},
]
_CIQ_DISPLAY_INFO = [
    {
        "appId": _CIQ_APP,
        "name": "FTMSRowing",
        "developerDisplayName": "Jeff_Crystal",
        "fitContributionsJSON": {
            "datafields": [
                {
                    "id": 10,
                    "label-key": "machine_name_label",
                    "unit-label-key": "none_units",
                    "sort-order": 10,
                    "precision": 0,
                },
                {
                    "id": 11,
                    "label-key": "CellDistanceMeters",
                    "unit-label-key": "distance_meters_units",
                    "sort-order": 11,
                    "precision": 0,
                },
                {
                    "id": 12,
                    "label-key": "total_distance_label",
                    "unit-label-key": "distance_km_units",
                    "sort-order": 12,
                    "precision": 2,
                },
                {
                    "id": 14,
                    "label-key": "total_strokes_label",
                    "unit-label-key": "strokes_units",
                    "sort-order": 14,
                    "precision": 0,
                },
                {
                    "id": 16,
                    "label-key": "CellPkSPM",
                    "unit-label-key": "strokes_units",
                    "sort-order": 16,
                    "precision": 0,
                },
                {
                    "id": 18,
                    "label-key": "peak_power_label",
                    "unit-label-key": "power_units",
                    "sort-order": 18,
                    "precision": 0,
                },
            ],
            "strings": {
                "default": {
                    "machine_name_label": "Machine",
                    "none_units": " ",
                    "CellDistanceMeters": "Distance (Meters)",
                    "distance_meters_units": "m",
                    "total_distance_label": "Distance",
                    "distance_km_units": "Km",
                    "total_strokes_label": "Total Strokes",
                    "strokes_units": "Strokes",
                    "CellPkSPM": "Peak Stroke Rate",
                    "peak_power_label": "Peak Power",
                    "power_units": "W",
                },
                "de-DE": {"machine_name_label": "Gerät"},
            },
        },
    }
]


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

    async def test_get_upcoming_events_params(self):
        from ha_garmin.const import CALENDAR_EVENTS_URL

        client = GarminClient(_make_auth())
        payload = [{"id": 29937926, "eventName": "5K Plan"}]
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = payload
            result = await client.get_upcoming_events()

        assert result == payload
        mock_req.assert_awaited_once_with(
            "GET",
            f"{CALENDAR_EVENTS_URL}/upcoming",
            params={"numDaysForward": 365, "limit": 10},
        )

    async def test_fetch_activity_data_includes_scheduled_workouts(self):
        """fetch_activity_data surfaces workout-type calendar items only,
        across this month and next.

        Real calendarItems mix several unrelated event types under
        `itemType` (weigh-ins, naps, workouts); only scheduled sessions
        ("workout" here; "fbtAdaptiveWorkout" is covered below) are a
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
                    "trainingPlanId": 222,
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
                client, "get_upcoming_events", new_callable=AsyncMock
            ) as mock_goal,
            patch.object(
                client, "get_workout", new_callable=AsyncMock, return_value={}
            ),
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

        mock_goal.assert_awaited_once_with()
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
        get_upcoming_events, the filtering in fetch_activity_data)
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
            if url == f"{CALENDAR_EVENTS_URL}/upcoming":
                if events_params is not None:
                    events_params.append(params)
                return goal_events or []
            return []

        return fake_request

    async def test_fetch_activity_data_includes_fbt_adaptive_workout(self):
        """A Daily Suggested / adaptive session is a scheduled workout
        too.

        calendar-service reports it with itemType "fbtAdaptiveWorkout", not
        "workout". The item below is a real one, with only its date moved
        to today so the upcoming-only filter keeps it.
        """
        auth = _make_auth()
        client = GarminClient(auth)

        today = date.today()
        today_str = today.isoformat()
        fbt_item = {
            "id": 1789814567000,
            "trainingPlanId": 45489509,
            "itemType": "fbtAdaptiveWorkout",
            "title": "Basis",
            "date": "2026-09-19",
            "sportTypeKey": "running",
        }
        fbt_today = {**fbt_item, "date": today_str}

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
        assert events_params == [{"numDaysForward": 365, "limit": 10}]
        assert data["trainingPlanGoalEvent"] == {}

    async def test_fetch_activity_data_goal_event_survives_fbt_first(self):
        """The goal event's plan id comes from the first upcoming item that
        carries an atpPlanId, not only from the next/today item.

        fbtAdaptiveWorkout items have been seen without an atpPlanId. With
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
                "eventName": "Primary 10K",
                "date": "2026-11-01",
                "eventCustomization": {"isPrimaryEvent": True},
            },
            {
                "eventName": "5K Plan",
                "date": "2026-11-21",
                "completionTarget": {"value": 5.0, "unit": "kilometer"},
                "eventCustomization": {
                    "trainingPlanId": 222,
                    "trainingPlanType": "COACH_ATP",
                },
            },
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
        assert len(events_params) == 1
        assert data["trainingPlanGoalEvent"]["eventName"] == "5K Plan"
        assert data["trainingPlanGoalEvent"]["trainingPlanType"] == "COACH_ATP"

    @staticmethod
    def _event(title: str, day: date, primary: bool, race: bool = True, plan=None):
        return {
            "eventName": title,
            "date": day.isoformat(),
            "eventType": "running",
            "race": race,
            "completionTarget": {
                "value": 42195.0,
                "unit": "meter",
                "unitType": "distance",
            },
            "eventCustomization": {
                "isPrimaryEvent": primary,
                "trainingPlanId": plan,
                "projectedRaceTimeDurationSeconds": 12600 if plan else None,
            },
        }

    async def test_fetch_activity_data_goal_event_falls_back_to_primary_event(self):
        """Without a plan goal event, the primary event wins over a sooner race."""
        client = GarminClient(_make_auth())
        today = date.today()
        events = [
            self._event("Parkrun", today, primary=False),
            self._event("TCS Amsterdam Marathon", today + timedelta(days=2), True),
            self._event("Club Social", today, False, race=False),
        ]
        fake_request = self._calendar_request_router({}, goal_events=events)
        with patch.object(client, "_request", side_effect=fake_request):
            data = await client.fetch_activity_data()

        goal = data["trainingPlanGoalEvent"]
        assert goal["eventName"] == "TCS Amsterdam Marathon"
        assert goal["date"] == (today + timedelta(days=2)).isoformat()
        assert goal["targetDistance"] == 42195.0
        assert goal["targetDistanceUnit"] == "meter"
        assert goal["primaryEvent"] is True
        assert [e["eventName"] for e in data["upcomingEvents"]] == [
            "Parkrun",
            "TCS Amsterdam Marathon",
            "Club Social",
        ]
        assert data["scheduledWorkouts"] == []

    async def test_fetch_activity_data_race_fallback_soonest_without_primary(self):
        """With no primary event, the soonest race is used; non-races are not."""
        client = GarminClient(_make_auth())
        today = date.today()
        events = [
            self._event("Social", today, False, race=False),
            self._event("Later", today + timedelta(days=3), primary=False),
            self._event("Sooner", today + timedelta(days=1), primary=False),
        ]
        fake_request = self._calendar_request_router({}, goal_events=events)
        with patch.object(client, "_request", side_effect=fake_request):
            data = await client.fetch_activity_data()

        assert data["trainingPlanGoalEvent"]["eventName"] == "Sooner"
        assert data["trainingPlanGoalEvent"]["primaryEvent"] is False

    async def test_fetch_activity_data_plan_goal_event_beats_primary_event(self):
        """The plan's own goal event (with projections) takes precedence."""
        client = GarminClient(_make_auth())
        today = date.today()
        atp_workout = {
            "id": 1,
            "itemType": "workout",
            "title": "Easy Run",
            "date": today.isoformat(),
            "atpPlanId": 222,
        }
        events = [
            self._event("Some Race", today + timedelta(days=1), primary=True),
            self._event("Plan Marathon", today + timedelta(days=30), False, plan=222),
        ]
        fake_request = self._calendar_request_router(
            {(today.year, today.month): [atp_workout]}, goal_events=events
        )
        with patch.object(client, "_request", side_effect=fake_request):
            data = await client.fetch_activity_data()

        goal = data["trainingPlanGoalEvent"]
        assert goal["eventName"] == "Plan Marathon"
        assert goal["projectedRaceTimeDurationSeconds"] == 12600

    async def test_fetch_activity_data_uses_recency_not_window(self):
        """Test fetch_activity_data returns lastActivity even for old activities."""
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
                client,
                "get_activity_connect_iq_display_info",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                client, "get_activity", new_callable=AsyncMock, return_value={}
            ),
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
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
        """lastActivities must not be capped at 10.

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
                client,
                "get_activity_connect_iq_display_info",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                client, "get_activity", new_callable=AsyncMock, return_value={}
            ),
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
        ):
            mock_acts.return_value = activities
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            data = await client.fetch_activity_data()

        assert len(data["lastActivities"]) == 15

    async def test_fetch_activity_data_merges_ebike_fields(self):
        """Test fetch_activity_data merges e-bike fields from the summary endpoint."""
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
                client,
                "get_activity_connect_iq_display_info",
                new_callable=AsyncMock,
                return_value=[],
            ),
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
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
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
        """Test an empty e-bike summary is not cached negatively forever.

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
                client,
                "get_activity_connect_iq_display_info",
                new_callable=AsyncMock,
                return_value=[],
            ),
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
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
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
                client,
                "get_activity_connect_iq_display_info",
                new_callable=AsyncMock,
                return_value=[],
            ),
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
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
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
        """Overlapping callers must not let one clobber the other's result.

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

    async def test_fetch_activity_data_merges_connect_iq_fields(self):
        """Connect IQ values (summary) joined with their definitions (display info)."""
        client = GarminClient(_make_auth())
        row = {
            "activityId": 11,
            "activityName": "Indoor Rowing",
            "activityType": {"typeKey": "indoor_rowing"},
            "hasPolyline": False,
        }
        summary = {"activityId": 11, "connectIQMeasurements": _CIQ_MEASUREMENTS}

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_activity", new_callable=AsyncMock
            ) as mock_summary,
            patch.object(
                client, "get_activity_connect_iq_display_info", new_callable=AsyncMock
            ) as mock_info,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
        ):
            mock_acts.return_value = [row]
            mock_summary.return_value = summary
            mock_info.return_value = _CIQ_DISPLAY_INFO
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            first = await client.fetch_activity_data()
            second = await client.fetch_activity_data()

        # One call each per new activity, not per poll
        mock_summary.assert_awaited_once_with(11)
        mock_info.assert_awaited_once_with(11)
        fields = first["lastActivity"]["connectIQFields"]
        assert second["lastActivity"]["connectIQFields"] == fields
        assert [f["field"] for f in fields] == [
            "Machine",
            "Distance (Meters)",
            "Distance",
            "Total Strokes",
            "Peak Stroke Rate",
            "Peak Power",
        ]
        assert fields[0] == {
            "app": "FTMSRowing",
            "field": "Machine",
            "value": "FIT-RW-",
            "unit": None,
            "developerFieldNumber": 10,
        }
        assert fields[1]["value"] == 2946
        assert fields[1]["unit"] == "m"
        assert fields[2]["value"] == 2.95  # precision 2
        assert fields[2]["unit"] == "Km"
        assert fields[3]["value"] == 491
        assert fields[3]["unit"] == "Strokes"
        # Raw measurements are not leaked, and list rows stay trimmed
        assert "connectIQMeasurements" not in first["lastActivity"]
        assert "connectIQFields" not in first["lastActivities"][0]

    async def test_fetch_activity_data_omits_connect_iq_when_none(self):
        """No measurements: no attribute and the display info is never fetched."""
        client = GarminClient(_make_auth())
        walk = {
            "activityId": 12,
            "activityName": "Walk",
            "activityType": {"typeKey": "walking"},
            "hasPolyline": False,
        }

        with (
            patch.object(client, "get_activities", new_callable=AsyncMock) as mock_acts,
            patch.object(
                client, "get_activity", new_callable=AsyncMock
            ) as mock_summary,
            patch.object(
                client, "get_activity_connect_iq_display_info", new_callable=AsyncMock
            ) as mock_info,
            patch.object(
                client, "get_workouts", new_callable=AsyncMock
            ) as mock_workouts,
            patch.object(
                client, "get_activity_hr_in_timezones", new_callable=AsyncMock
            ) as mock_hr,
            patch.object(
                client, "get_scheduled_workouts", new_callable=AsyncMock
            ) as mock_calendar,
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
        ):
            mock_acts.return_value = [walk]
            mock_summary.return_value = {"activityId": 12, "connectIQMeasurements": []}
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            retry_limit = client._EBIKE_FIELDS_EMPTY_RETRY_LIMIT
            for _ in range(retry_limit + 3):
                data = await client.fetch_activity_data()

        assert "connectIQFields" not in data["lastActivity"]
        mock_info.assert_not_awaited()
        # An empty summary is retried a bounded number of polls, then cached
        assert mock_summary.await_count == retry_limit

    async def test_get_activity_connect_iq_display_info_returns_list_only(self):
        """A non-list body (404 -> {}) becomes an empty list."""
        client = GarminClient(_make_auth())
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response({}, status=404)
            assert await client.get_activity_connect_iq_display_info(5) == []

    def test_merge_connect_iq_fields_without_definitions(self):
        """Values whose app or field has no definition keep a generic name."""
        from ha_garmin.client import _merge_connect_iq_fields

        fields = _merge_connect_iq_fields(
            [{"appID": "app-x", "developerFieldNumber": 3, "value": "12.5"}], []
        )
        assert fields == [
            {
                "app": "app-x",
                "field": "field_3",
                "value": 12.5,
                "unit": None,
                "developerFieldNumber": 3,
            }
        ]

    async def test_fetch_activity_data_ignores_ebike_fields_for_non_rides(self):
        """The summary is fetched for every activity (Connect IQ), e-bike keys only for rides."""
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
                client,
                "get_activity_connect_iq_display_info",
                new_callable=AsyncMock,
                return_value=[],
            ),
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
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
        ):
            mock_acts.return_value = [run]
            mock_summary.return_value = {"activityId": 8, "eBikeBatteryRemaining": 42}
            mock_workouts.return_value = []
            mock_hr.return_value = []
            mock_calendar.return_value = {}
            data = await client.fetch_activity_data()

        mock_summary.assert_awaited_once_with(8)
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
            },
            "skinTempDataExists": True,
            "avgSkinTempDeviationC": 0.2,
            "avgSkinTempDeviationF": 0.3,
            "skinTempCalibrationDays": 19,
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
        assert data["avgSkinTempDeviationC"] == 0.2
        assert data["skinTempCalibrationDays"] == 19

    async def test_fetch_core_data_bedtime_uses_gmt_local_delta_for_offset(self):
        """bedtime/wake_time must not silently assume UTC+0.

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
        # No skin temperature sensor data in this payload.
        assert data["avgSkinTempDeviationC"] is None
        assert data["skinTempCalibrationDays"] is None

    async def test_fetch_core_data_transient_error_does_not_use_yesterday(self):
        """Test a transient 502/503 does not get papered over with yesterday's summary.

        Regression test: a transient API failure while fetching today's
        summary must not be treated the same as "today's data isn't ready
        yet", or fast-changing
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

    async def test_request_raises_tls_error_on_untrusted_certificate(self):
        """A failed certificate check means interception, not a flaky endpoint."""
        import ssl

        import requests
        from urllib3.exceptions import MaxRetryError
        from urllib3.exceptions import SSLError as Urllib3SSLError

        client = GarminClient(_make_auth(), is_cn=True)
        cert_err = ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )
        cert_err.verify_message = "self-signed certificate"
        wrapped = requests.exceptions.SSLError(
            MaxRetryError(MagicMock(), "/test", Urllib3SSLError(cert_err))
        )

        with (
            patch("asyncio.to_thread", new_callable=AsyncMock, side_effect=wrapped),
            pytest.raises(GarminTLSError) as excinfo,
        ):
            await client._request("GET", f"{GARMIN_CONNECT_API}/test")

        message = str(excinfo.value)
        assert "connectapi.garmin.cn" in message
        assert "self-signed certificate" in message
        assert not isinstance(excinfo.value, GarminAPIError)

    async def test_request_keeps_api_error_for_other_ssl_failures(self):
        """A dropped handshake is transient; only certificate failures abort."""
        import ssl

        import requests

        client = GarminClient(_make_auth())
        wrapped = requests.exceptions.SSLError(
            ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
        )

        with (
            patch("asyncio.to_thread", new_callable=AsyncMock, side_effect=wrapped),
            pytest.raises(GarminAPIError, match="Request failed"),
        ):
            await client._request("GET", f"{GARMIN_CONNECT_API}/test")

    async def test_safe_call_does_not_swallow_tls_error(self):
        """_safe_call skips a failing endpoint, but a TLS failure ends the fetch."""
        client = GarminClient(_make_auth())

        async def failing() -> None:
            raise GarminTLSError("intercepted")

        with pytest.raises(GarminTLSError):
            await client._safe_call(failing)

    async def test_get_body_composition_newest_weigh_in_from_range(self):
        """The usual case: one weight/range call, newest weigh-in wins."""
        client = GarminClient(_make_auth())
        client._request = AsyncMock(
            return_value={
                "dailyWeightSummaries": [
                    {"latestWeight": {"calendarDate": "2026-10-01", "weight": 85000.0}},
                    {
                        "latestWeight": {
                            "calendarDate": "2026-10-14",
                            "weight": 87000.0,
                            "bodyWater": 56.1,
                        }
                    },
                ],
                "previousDateWeight": {"calendarDate": "2026-09-01", "weight": 1.0},
            }
        )

        result = await client.get_body_composition(date(2026, 10, 16))

        assert result["weight"] == 87000.0
        assert result["bodyWater"] == 56.1
        client._request.assert_awaited_once()
        assert client._request.call_args.args[1].endswith("/2026-09-16/2026-10-16")

    async def test_get_body_composition_old_weigh_in_keeps_body_water(self):
        """No weigh-in in 30 days: previousDateWeight is the complete record."""
        client = GarminClient(_make_auth())
        previous = {
            "calendarDate": "2026-09-13",
            "weight": 80000.0,
            "bodyFat": 22.0,
            "bodyWater": 57.3,
            "sourceType": "INDEX_SCALE",
        }
        client._request = AsyncMock(
            return_value={"dailyWeightSummaries": [], "previousDateWeight": previous}
        )

        result = await client.get_body_composition(date(2026, 10, 15))

        assert result == previous
        client._request.assert_awaited_once()

    async def test_get_body_composition_last_resort_is_weight_latest(self):
        """No weigh-in in or before the window: weight/latest, dated from its ms."""
        from ha_garmin.const import WEIGHT_LATEST_URL

        client = GarminClient(_make_auth())
        latest = {"date": 1789286400000, "version": 7, "weight": 80000.0}

        async def fake_request(method, url, params=None):
            return latest if url == WEIGHT_LATEST_URL else {"dailyWeightSummaries": []}

        client._request = fake_request
        result = await client.get_body_composition(date(2026, 10, 15))

        assert result["weight"] == 80000.0
        assert result["calendarDate"] == "2026-09-13"
        assert result["samplePk"] == 7

    async def test_get_body_composition_empty_without_weigh_in(self):
        client = GarminClient(_make_auth())

        async def fake_request(method, url, params=None):
            return {"weight": None} if "latest" in url else {}

        client._request = fake_request
        assert await client.get_body_composition() == {}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (2177427894000, None),  # ms timestamp seen from a third-party scale
            (38, 38),
            (None, None),
        ],
    )
    async def test_get_body_composition_drops_impossible_metabolic_age(
        self, raw, expected
    ):
        """metabolicAge is only passed through when it can be an age in years."""
        auth = _make_auth()
        client = GarminClient(auth)

        payload = {
            "dailyWeightSummaries": [
                {
                    "latestWeight": {
                        "calendarDate": "2026-04-03",
                        "weight": 87000.0,
                        "metabolicAge": raw,
                    },
                },
            ],
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = _mock_response(payload)
            result = await client.get_body_composition()

        assert result["metabolicAge"] == expected
        assert result["weight"] == 87000.0

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

    @staticmethod
    def _vo2_safe_call(responses: dict, calls: list[str]):
        """Fake _safe_call answering by method name; everything else is {}."""

        async def fake(func, *args, **kwargs):
            calls.append(func.__name__)
            return responses.get(func.__name__, {})

        return fake

    @pytest.mark.parametrize(
        ("most_recent", "expected", "expected_precise"),
        [
            # Cycling-only account: generic is null.
            (
                {
                    "generic": None,
                    "cycling": {"vo2MaxValue": 52.0, "vo2MaxPreciseValue": 51.7},
                },
                52.0,
                51.7,
            ),
            # Both present: generic (running) wins.
            (
                {
                    "generic": {"vo2MaxValue": 36.0, "vo2MaxPreciseValue": 35.6},
                    "cycling": {"vo2MaxValue": 52.0, "vo2MaxPreciseValue": 51.7},
                },
                36.0,
                35.6,
            ),
        ],
    )
    async def test_fetch_training_data_vo2max_generic_then_cycling(
        self, most_recent, expected, expected_precise
    ):
        """VO2 Max comes from generic, else cycling, without extra requests."""
        client = GarminClient(_make_auth())
        calls: list[str] = []
        client._safe_call = self._vo2_safe_call(
            {"get_training_status": {"mostRecentVO2Max": most_recent}}, calls
        )

        data = await client.fetch_training_data()

        assert data["vo2MaxValue"] == expected
        assert data["vo2MaxPreciseValue"] == expected_precise
        assert "get_user_settings" not in calls
        assert "get_activities" not in calls

    @pytest.mark.parametrize(
        ("user_data", "expected"),
        [
            ({"vo2MaxRunning": 36.0, "vo2MaxCycling": None}, 36.0),
            ({"vo2MaxRunning": None, "vo2MaxCycling": 52.0}, 52.0),
        ],
    )
    async def test_fetch_training_data_vo2max_falls_back_to_user_settings(
        self, user_data, expected
    ):
        """No VO2 Max in training status: the profile's own value is used."""
        client = GarminClient(_make_auth())
        calls: list[str] = []
        client._safe_call = self._vo2_safe_call(
            {"get_user_settings": {"userData": user_data}}, calls
        )

        data = await client.fetch_training_data()

        assert data["vo2MaxValue"] == expected
        assert "get_activities" not in calls

    async def test_fetch_training_data_vo2max_activities_last_resort(self):
        """Neither training status nor profile: last activities' value."""
        client = GarminClient(_make_auth())
        calls: list[str] = []
        client._safe_call = self._vo2_safe_call(
            {
                "get_user_settings": {"userData": {"vo2MaxRunning": None}},
                "get_activities": [{"vO2MaxValue": None}, {"vO2MaxValue": 41.0}],
            },
            calls,
        )

        data = await client.fetch_training_data()

        assert data["vo2MaxValue"] == 41.0
        assert calls.index("get_user_settings") < calls.index("get_activities")

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
        client._profile_cache = _PROFILE

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
        client._profile_cache = _PROFILE

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
        client._profile_cache = _PROFILE

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
        client._profile_cache = _PROFILE

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
        client._profile_cache = _PROFILE
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
        """Garmin rejects 92+ inclusive days, so the window is clamped to 90."""
        auth = _make_auth()
        client = GarminClient(auth)

        start_date = date(2026, 1, 1)
        end_date = date(2026, 12, 31)
        expected_clamped_date = start_date + timedelta(days=89)

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
        end_date = date(2026, 8, 5)  # 90 days inclusive, the maximum

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
            ("get_gear_details", ("../../../etc/passwd",)),
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
            mock_request.return_value = {}
            # Undashed legacy uuids are dashed for gear v2.
            await client.get_gear_details("a0eebc999c0b4ef8bb6d6bb9bd380a11")
            assert mock_request.call_args[0][1].endswith(
                "/v2/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
            )


class TestUploadActivityLogging:
    """upload_activity() must not log Garmin's full upload response."""

    _RESULT = {
        "detailedImportResult": {
            "uploadId": 123456789,
            "uploadUuid": {"uuid": "secret-upload-uuid"},
            "owner": 987654321,
            "fileName": "ride.fit",
            "successes": [],
            "failures": [
                {
                    "internalId": 555,
                    "messages": [{"code": 202, "content": "Duplicate Activity."}],
                }
            ],
        }
    }

    async def test_warning_logs_messages_not_body(self, tmp_path, caplog):
        fit = tmp_path / "ride.fit"
        fit.write_bytes(b"fit")
        client = GarminClient(_make_auth())
        with (
            patch("requests.post", return_value=_mock_response(self._RESULT, 400)),
            caplog.at_level("WARNING", logger="ha_garmin.client"),
        ):
            body = await client.upload_activity(str(fit))
        assert body == self._RESULT
        assert "Upload accepted with warnings: Duplicate Activity." in caplog.text
        assert "987654321" not in caplog.text
        assert "secret-upload-uuid" not in caplog.text

    async def test_failure_error_lists_failure_messages(self, tmp_path):
        fit = tmp_path / "ride.fit"
        fit.write_bytes(b"fit")
        client = GarminClient(_make_auth())
        result = {
            "detailedImportResult": {
                "failures": [
                    {"messages": [{"content": "Bad file"}, {"content": "Really bad"}]}
                ]
            }
        }
        with (
            patch("requests.post", return_value=_mock_response(result, 500)),
            pytest.raises(GarminAPIError, match="Upload failed: Bad file; Really bad"),
        ):
            await client.upload_activity(str(fit))


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
        """fetch_gear_data must surface paired ANT+/BLE sensors."""
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
            patch.object(client, "get_gear_list", return_value=[]),
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
        evening reads near 0% even on a sunny day.
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
            patch.object(client, "get_gear_list", return_value=[]),
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


class TestGarminCalendarZone:
    """The Garmin calendar day must not depend on the process time zone."""

    def test_fallback_zone_used_before_offset_is_learned(self):
        client = GarminClient(_make_auth(), timezone="Europe/Istanbul")
        assert client.now().utcoffset() == timedelta(hours=3)

    def test_invalid_zone_falls_back_to_system(self, caplog):
        client = GarminClient(_make_auth(), timezone="Not/AZone")
        assert "Invalid timezone" in caplog.text
        assert client.now().utcoffset() == datetime.now().astimezone().utcoffset()

    def test_learned_offset_overrides_disagreeing_zone(self):
        """A device in another zone (travel) defines the Garmin day."""
        client = GarminClient(_make_auth(), timezone="Europe/Istanbul")
        client._learn_utc_offset(
            {
                "wellnessStartTimeLocal": "2026-01-02T00:00:00.0",
                "wellnessStartTimeGmt": "2026-01-02T08:00:00.0",
            }
        )
        assert client.now().utcoffset() == timedelta(hours=-8)

    def test_agreeing_zone_is_kept_for_dst(self):
        client = GarminClient(_make_auth(), timezone="Europe/Istanbul")
        client._learn_utc_offset(
            {
                "wellnessStartTimeLocal": "2026-01-02T00:00:00.0",
                "wellnessStartTimeGmt": "2026-01-01T21:00:00.0",
            }
        )
        assert str(client.now().tzinfo) == "Europe/Istanbul"

    @pytest.mark.parametrize(
        "summary",
        [
            {},
            {"wellnessStartTimeLocal": "garbage", "wellnessStartTimeGmt": "x"},
            {
                "wellnessStartTimeLocal": "2026-01-02T00:13:00.0",
                "wellnessStartTimeGmt": "2026-01-02T00:00:00.0",
            },
            {
                "wellnessStartTimeLocal": "2026-01-03T00:00:00.0",
                "wellnessStartTimeGmt": "2026-01-02T00:00:00.0",
            },
        ],
    )
    def test_implausible_offsets_are_ignored(self, summary):
        client = GarminClient(_make_auth())
        client._learn_utc_offset(summary)
        assert client._garmin_utc_offset is None

    async def test_user_summary_learns_offset(self):
        client = GarminClient(_make_auth())
        client.get_user_profile = AsyncMock(return_value=MagicMock(display_name="me"))
        client._request = AsyncMock(
            return_value={
                "wellnessStartTimeLocal": "2026-01-02T00:00:00.0",
                "wellnessStartTimeGmt": "2026-01-01T21:00:00.0",
            }
        )
        await client._get_user_summary_raw(date(2026, 1, 2))
        assert client._garmin_utc_offset == timedelta(hours=3)

    async def test_hydration_calendar_date_follows_timestamp(self):
        client = GarminClient(_make_auth(), timezone="Europe/Istanbul")
        client._profile_cache = _PROFILE
        client._put_request = AsyncMock(return_value={})
        await client.set_hydration(250, timestamp="2026-04-17T23:30:00")
        payload = client._put_request.call_args.args[1]
        assert payload["calendarDate"] == "2026-04-17"
        assert payload["timestampLocal"] == "2026-04-17T23:30:00.000"

    async def test_blood_pressure_aware_timestamp_uses_garmin_zone(self):
        client = GarminClient(_make_auth(), timezone="Europe/Istanbul")
        client._post_request = AsyncMock(return_value={})
        await client.set_blood_pressure(120, 80, timestamp="2026-01-01T21:30:00+00:00")
        payload = client._post_request.call_args.args[1]
        assert payload["measurementTimestampLocal"] == "2026-01-02T00:30:00.000"
        assert payload["measurementTimestampGMT"] == "2026-01-01T21:30:00.000"

    async def test_nutrition_naive_timestamp_is_garmin_local(self):
        client = GarminClient(_make_auth(), timezone="Europe/Istanbul")
        client._put_request = AsyncMock(return_value={})
        client._get_nutrition_meal = AsyncMock(return_value=(1, "00:00:00"))
        await client.add_nutrition_log(calories=100, timestamp="2026-04-18T01:00:00")
        payload = client._put_request.call_args.args[1]
        assert payload["mealDate"] == "2026-04-18"
        entry = payload["quickAddItems"][0]
        assert entry["logTimestamp"] == "2026-04-17T22:00:00.000Z"
        assert entry["mealTime"] == "01:00:00"


class TestValidatePositiveInt:
    """IDs must not be silently truncated, and the URL gets the clean int."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(123, 123), ("123", 123), (" 123 ", 123), (123.0, 123)],
    )
    def test_accepts_integral_values(self, value, expected):
        from ha_garmin.client import _validate_positive_int

        assert _validate_positive_int(value, "x") == expected

    @pytest.mark.parametrize(
        "value", [1.9, "1.9", "abc", "", None, True, [1], 0, -5, "0", 0.0]
    )
    def test_rejects_non_integral_or_non_positive(self, value):
        from ha_garmin.client import _validate_positive_int

        with pytest.raises(ValueError, match="x must be a positive integer"):
            _validate_positive_int(value, "x")

    async def test_url_uses_cleaned_id(self):
        from ha_garmin.const import ADAPTIVE_TRAINING_PLAN_URL

        client = GarminClient(_make_auth())
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {}
            await client.get_adaptive_training_plan_by_id(1789148356.0)
        mock_req.assert_awaited_once_with(
            "GET", f"{ADAPTIVE_TRAINING_PLAN_URL}/1789148356"
        )

    async def test_fractional_id_rejected_without_request(self):
        client = GarminClient(_make_auth())
        with (
            patch.object(client, "_request", new_callable=AsyncMock) as mock_req,
            pytest.raises(ValueError),
        ):
            await client.get_adaptive_training_plan_by_id(1.9)
        mock_req.assert_not_awaited()


class TestWorkoutWrites:
    """Workout create/update/delete/schedule hit the workout-service endpoints."""

    async def test_create_workout_posts_json(self):
        from ha_garmin.const import WORKOUT_URL

        client = GarminClient(_make_auth())
        client._post_request = AsyncMock(return_value={"workoutId": 42})
        workout = {"workoutName": "Easy", "sportType": {"sportTypeKey": "running"}}
        assert await client.create_workout(workout) == {"workoutId": 42}
        client._post_request.assert_awaited_once_with(WORKOUT_URL, workout)

    async def test_create_workout_rejects_non_object(self):
        client = GarminClient(_make_auth())
        client._post_request = AsyncMock()
        with pytest.raises(ValueError, match="JSON object"):
            await client.create_workout([{"workoutName": "x"}])
        client._post_request.assert_not_awaited()

    async def test_update_workout_forces_id(self):
        from ha_garmin.const import WORKOUT_URL

        client = GarminClient(_make_auth())
        client._put_request = AsyncMock(return_value={})
        await client.update_workout("42", {"workoutId": 1, "workoutName": "New"})
        client._put_request.assert_awaited_once_with(
            f"{WORKOUT_URL}/42", {"workoutId": 42, "workoutName": "New"}
        )

    async def test_delete_workout(self):
        from ha_garmin.const import WORKOUT_URL

        client = GarminClient(_make_auth())
        client._delete_request = AsyncMock(return_value={})
        await client.delete_workout(42)
        client._delete_request.assert_awaited_once_with(f"{WORKOUT_URL}/42")

    @pytest.mark.parametrize("when", [date(2026, 3, 20), "2026-03-20"])
    async def test_schedule_workout(self, when):
        from ha_garmin.const import WORKOUT_SCHEDULE_URL

        client = GarminClient(_make_auth())
        client._post_request = AsyncMock(return_value={"workoutScheduleId": 7})
        result = await client.schedule_workout(42, when)
        assert result == {"workoutScheduleId": 7}
        client._post_request.assert_awaited_once_with(
            f"{WORKOUT_SCHEDULE_URL}/42", {"date": "2026-03-20"}
        )

    async def test_schedule_workout_rejects_bad_date(self):
        client = GarminClient(_make_auth())
        client._post_request = AsyncMock()
        with pytest.raises(ValueError):
            await client.schedule_workout(42, "20-03-2026")
        client._post_request.assert_not_awaited()

    async def test_unschedule_workout(self):
        from ha_garmin.const import WORKOUT_SCHEDULE_URL

        client = GarminClient(_make_auth())
        client._delete_request = AsyncMock(return_value={})
        await client.unschedule_workout(7)
        client._delete_request.assert_awaited_once_with(f"{WORKOUT_SCHEDULE_URL}/7")

    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.delete_workout(1.5),
            lambda c: c.update_workout(0, {}),
            lambda c: c.schedule_workout("abc", "2026-03-20"),
            lambda c: c.unschedule_workout(-1),
        ],
    )
    async def test_invalid_ids_rejected_without_request(self, call):
        client = GarminClient(_make_auth())
        client._request = AsyncMock()
        client._post_request = AsyncMock()
        client._put_request = AsyncMock()
        client._delete_request = AsyncMock()
        with pytest.raises(ValueError, match="positive integer"):
            await call(client)
        for mock in (client._post_request, client._put_request, client._delete_request):
            mock.assert_not_awaited()


class TestWebAppParity:
    """Requests match what the Garmin Connect web app sends."""

    async def test_hydration_rejects_non_numbers(self):
        client = GarminClient(_make_auth())
        client._profile_cache = _PROFILE
        client._put_request = AsyncMock(return_value={})
        for bad in (True, float("nan"), float("inf"), "250"):
            with pytest.raises(ValueError):
                await client.set_hydration(bad)
        client._put_request.assert_not_called()

    async def test_hydration_sends_user_profile_id(self):
        client = GarminClient(_make_auth(), timezone="Europe/Amsterdam")
        client._profile_cache = _PROFILE
        client._put_request = AsyncMock(return_value={})
        await client.set_hydration(250)
        payload = client._put_request.call_args.args[1]
        assert payload["userProfileId"] == _PROFILE.profile_id

    async def test_hydration_cdate_alone_logs_at_midnight(self):
        client = GarminClient(_make_auth(), timezone="Europe/Amsterdam")
        client._profile_cache = _PROFILE
        client._put_request = AsyncMock(return_value={})
        await client.set_hydration(250, cdate="2026-04-18")
        payload = client._put_request.call_args.args[1]
        assert payload["calendarDate"] == "2026-04-18"
        assert payload["timestampLocal"] == "2026-04-18T00:00:00.000"

    async def test_hydration_rejects_mismatched_cdate(self):
        client = GarminClient(_make_auth(), timezone="Europe/Amsterdam")
        client._profile_cache = _PROFILE
        client._put_request = AsyncMock(return_value={})
        with pytest.raises(ValueError, match="doesn't match cdate"):
            await client.set_hydration(
                250, cdate="2026-04-18", timestamp="2026-04-19T08:00:00"
            )

    async def test_nutrition_log_requests_goals(self):
        client = GarminClient(_make_auth())
        client._request = AsyncMock(return_value={})
        await client.get_nutrition_log(date(2026, 4, 18))
        assert client._request.call_args.kwargs["params"] == {
            "includeGoals": "true",
            "includeNutrition": "true",
        }

    async def test_menstrual_day_view_sends_today_date(self):
        client = GarminClient(_make_auth(), timezone="Europe/Amsterdam")
        client._request = AsyncMock(return_value={})
        await client.get_menstrual_data(date(2026, 4, 1))
        params = client._request.call_args.kwargs["params"]
        assert params == {"todayDate": client.today().isoformat()}

    async def test_menstrual_default_window_is_not_clamped(self):
        client = GarminClient(_make_auth(), timezone="Europe/Amsterdam")
        client._request = AsyncMock(return_value={})
        await client.get_menstrual_calendar()
        today = client.today()
        url = client._request.call_args.args[1]
        assert url.endswith(
            f"/{(today - timedelta(days=30)).isoformat()}"
            f"/{(today + timedelta(days=59)).isoformat()}"
        )

    @pytest.mark.parametrize(
        ("systolic", "diastolic", "pulse"),
        [(300, 80, None), (120, 20, None), (120, 80, 10), (120.5, 80, None)],
    )
    async def test_blood_pressure_rejects_out_of_range(
        self, systolic, diastolic, pulse
    ):
        client = GarminClient(_make_auth())
        client._post_request = AsyncMock(return_value={})
        with pytest.raises(ValueError):
            await client.set_blood_pressure(systolic, diastolic, pulse)
        client._post_request.assert_not_called()

    def test_alarms_default_to_garmin_day_zone(self):
        client = GarminClient(_make_auth(), timezone="Europe/Amsterdam")
        alarms = [{"alarmMode": "ON", "alarmTime": 420, "alarmDays": ["ONCE"]}]
        assert client._calculate_next_active_alarms(alarms, None)


class TestUploadErrors:
    """Upload failures keep Garmin's message and HTTP status."""

    _DUPLICATE = {
        "detailedImportResult": {
            "failures": [{"messages": [{"content": "Duplicate Activity."}]}]
        }
    }

    async def test_duplicate_after_403_retry_reports_message(self, tmp_path):
        fit = tmp_path / "ride.fit"
        fit.write_bytes(b"fit")
        client = GarminClient(_make_auth())
        client._auth.refresh_session = AsyncMock(return_value=True)
        responses = [_mock_response({}, 403), _mock_response(self._DUPLICATE, 409)]
        with (
            patch("requests.post", side_effect=responses),
            pytest.raises(GarminAPIError, match="Duplicate Activity") as exc,
        ):
            await client.upload_activity(str(fit))
        assert exc.value.status_code == 409

    async def test_non_text_messages_are_skipped(self, tmp_path):
        fit = tmp_path / "ride.fit"
        fit.write_bytes(b"fit")
        client = GarminClient(_make_auth())
        result = {
            "detailedImportResult": {
                "failures": [{"messages": [{"content": {"x": 1}}, {"content": "Bad"}]}]
            }
        }
        with (
            patch("requests.post", return_value=_mock_response(result, 500)),
            pytest.raises(GarminAPIError, match=r"Upload failed: Bad$"),
        ):
            await client.upload_activity(str(fit))


class TestGearV2:
    """Gear comes from gear v2; output keeps the legacy shape and uuids."""

    _V2 = [
        {
            "uuid": "a214d553-c175-4a04-9e8e-f0c812e5342b",
            "gearType": "SHOES",
            "status": "ACTIVE",
            "name": "Brooks",
            "brand": "Adrenaline GTS 21",
            "maxUsageDistanceMeters": 643738.0,
            "firstUseDate": "2021-05-14",
            "numActivitiesLinked": 49,
            "durationUsedSeconds": 110579,
            "distanceUsedMeters": 89654.6,
            "daysUsed": 45,
            "notes": "My notes",
        }
    ]
    _DEFAULTS = [
        {
            "uuid": "a214d553c1754a049e8ef0c812e5342b",
            "activityTypePk": 1,
            "defaultGear": True,
        }
    ]

    async def test_fetch_gear_data_maps_v2_without_stats_calls(self):
        client = GarminClient(_make_auth())
        client._profile_cache = _PROFILE
        with (
            patch.object(client, "get_gear_list", return_value=self._V2),
            patch.object(client, "get_gear_defaults", return_value=self._DEFAULTS),
            patch.object(client, "get_devices", return_value=[]),
            patch.object(client, "get_device_last_used", return_value={}),
            patch.object(client, "get_device_alarms", return_value=[]),
            patch.object(client, "get_sensors", return_value=[]),
        ):
            data = await client.fetch_gear_data()

        (stat,) = data["gearStats"]
        assert stat["uuid"] == stat["gearUuid"] == "a214d553c1754a049e8ef0c812e5342b"
        assert stat["gearName"] == "Brooks"
        assert stat["gearTypeName"] == "Shoes"
        assert stat["gearStatusName"] == "active"
        assert stat["totalDistance"] == 89654.6
        assert stat["totalActivities"] == 49
        assert stat["totalDurationSeconds"] == 110579
        assert stat["maximumMeters"] == 643738.0
        assert stat["customMakeModel"] == "Adrenaline GTS 21"
        assert stat["notes"] == "My notes"
        assert stat["defaultForActivity"] == ["running"]

    async def test_set_active_gear_puts_full_v2_object(self):
        client = GarminClient(_make_auth())
        details = {
            "uuid": "a214d553-c175-4a04-9e8e-f0c812e5342b",
            "name": "Brooks",
            "associatedActivityTypes": [
                {
                    "activityTypeKey": "hiking",
                    "defaultGear": True,
                    "preferredGear": False,
                }
            ],
        }
        client._request = AsyncMock(return_value=details)
        client._put_request = AsyncMock(return_value={})

        await client.set_active_gear(
            "running", "set as default", "a214d553c1754a049e8ef0c812e5342b"
        )

        url, payload = client._put_request.call_args.args
        assert url.endswith(
            "/gear-service/gear/v2/a214d553-c175-4a04-9e8e-f0c812e5342b"
        )
        assert payload["name"] == "Brooks"
        assert [a["activityTypeKey"] for a in payload["associatedActivityTypes"]] == [
            "hiking",
            "running",
        ]

    async def test_set_active_gear_unset_others(self):
        client = GarminClient(_make_auth())
        client._profile_cache = _PROFILE
        target = "a214d553c1754a049e8ef0c812e5342b"
        other = "bdcea3e8f70e40a78d50cd2fe5397798"
        defaults = [
            {"uuid": target, "activityTypePk": 1, "defaultGear": True},
            {"uuid": other, "activityTypePk": 1, "defaultGear": True},
            {"uuid": other, "activityTypePk": 4, "defaultGear": True},
        ]
        running = {
            "activityTypeKey": "running",
            "defaultGear": True,
            "preferredGear": False,
        }
        hiking = {
            "activityTypeKey": "hiking",
            "defaultGear": True,
            "preferredGear": False,
        }

        async def fake_request(method, url, params=None):
            if url.endswith("/activityTypes"):
                return defaults
            return {
                "uuid": url.rsplit("/", 1)[1],
                "associatedActivityTypes": [running, hiking],
            }

        client._request = fake_request
        client._put_request = AsyncMock(return_value={})

        await client.set_active_gear(
            "running", "set this as default, unset others", target
        )

        puts = {
            c.args[0].rsplit("/", 1)[1]: c.args[1]
            for c in client._put_request.call_args_list
        }
        assert len(puts) == 2
        other_keys = [
            a["activityTypeKey"]
            for a in puts["bdcea3e8-f70e-40a7-8d50-cd2fe5397798"][
                "associatedActivityTypes"
            ]
        ]
        assert other_keys == ["hiking"]

    async def test_set_active_gear_unknown_gear_raises(self):
        client = GarminClient(_make_auth())
        client._request = AsyncMock(return_value={})
        with pytest.raises(GarminAPIError, match="not found"):
            await client.set_active_gear("running", "unset default", "9" * 32)


class TestTrainingAndGoalsAdditions:
    def test_acclimation_and_cycling_vo2_from_training_status(self):
        from ha_garmin.client import _add_computed_fields

        result = _add_computed_fields(
            {
                "trainingStatus": {
                    "mostRecentVO2Max": {
                        "generic": {"vo2MaxValue": 36.0},
                        "cycling": {"vo2MaxValue": 41.0},
                        "heatAltitudeAcclimation": {
                            "heatAcclimationPercentage": 35,
                            "heatTrend": "ACCLIMATIZING",
                            "altitudeAcclimation": 1200,
                            "altitudeTrend": None,
                            "currentAltitude": 1500,
                        },
                    }
                }
            }
        )
        assert result["vo2MaxValue"] == 36.0
        assert result["vo2MaxCyclingValue"] == 41.0
        assert result["heatAcclimationPercentage"] == 35
        assert result["heatAcclimationTrend"] == "ACCLIMATIZING"
        assert result["altitudeAcclimation"] == 1200
        assert result["currentAltitude"] == 1500

    async def test_lifetime_totals_uses_display_name(self):
        from ha_garmin.const import LIFETIME_TOTALS_URL

        client = GarminClient(_make_auth())
        client._profile_cache = _PROFILE
        client._request = AsyncMock(return_value={"totalSteps": 10428222})
        assert await client.get_lifetime_totals() == {"totalSteps": 10428222}
        client._request.assert_awaited_once_with("GET", f"{LIFETIME_TOTALS_URL}/tester")


class TestCalendarDedup:
    async def test_item_in_both_month_views_is_listed_once(self):
        """A month view includes adjacent-month days, so both calls return it."""
        client = GarminClient(_make_auth())
        tomorrow = (client.today() + timedelta(days=1)).isoformat()
        item = {
            "id": 1810350286,
            "itemType": "workout",
            "title": "Run Workout",
            "date": tomorrow,
            "workoutId": 1731319876,
        }
        with (
            patch.object(
                client, "get_activities", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                client, "get_workouts", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                client,
                "get_scheduled_workouts",
                new_callable=AsyncMock,
                return_value={"calendarItems": [item]},
            ),
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                client, "get_workout", new_callable=AsyncMock, return_value={}
            ),
        ):
            data = await client.fetch_activity_data()

        assert [w["id"] for w in data["scheduledWorkouts"]] == [1810350286]


class TestSlowCache:
    async def test_result_is_reused_within_the_hour(self):
        client = GarminClient(_make_auth())
        fetch = AsyncMock(return_value={"totalSteps": 1})
        assert await client._cached_safe_call("k", fetch) == {"totalSteps": 1}
        assert await client._cached_safe_call("k", fetch) == {"totalSteps": 1}
        assert fetch.await_count == 1

    async def test_refetched_after_the_hour(self):
        client = GarminClient(_make_auth())
        fetch = AsyncMock(return_value=[])
        with patch(
            "ha_garmin.client.time.monotonic", side_effect=[0.0, 3601.0, 3601.0]
        ):
            await client._cached_safe_call("k", fetch)
            await client._cached_safe_call("k", fetch)
        assert fetch.await_count == 2

    async def test_failures_are_not_cached(self):
        client = GarminClient(_make_auth())
        fetch = AsyncMock(side_effect=[GarminAPIError("boom"), {"ok": True}])
        assert await client._cached_safe_call("k", fetch) is None
        assert await client._cached_safe_call("k", fetch) == {"ok": True}


class TestScheduledWorkoutSteps:
    """Today's and the next scheduled workout carry their steps and targets."""

    # Shape from a Final Surge workout synced to Garmin, plus a
    # repeat group as Garmin stores intervals.
    _WORKOUT = {
        "workoutId": 1731319876,
        "workoutName": "Easy 3 miles",
        "estimatedDistanceInMeters": 4828.02,
        "estimatedDurationInSecs": None,
        "workoutProvider": "FinalSurge",
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {"sportTypeKey": "running"},
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {"stepTypeKey": "interval"},
                        "endCondition": {"conditionTypeKey": "distance"},
                        "endConditionValue": 4828.02000001,
                        "targetType": {"workoutTargetTypeKey": "heart.rate.zone"},
                        "targetValueOne": 141.0,
                        "targetValueTwo": 159.0,
                        "zoneNumber": None,
                        "description": None,
                    },
                    {
                        "type": "RepeatGroupDTO",
                        "stepOrder": 2,
                        "numberOfIterations": 4,
                        "workoutSteps": [
                            {
                                "type": "ExecutableStepDTO",
                                "stepOrder": 3,
                                "stepType": {"stepTypeKey": "interval"},
                                "endCondition": {"conditionTypeKey": "time"},
                                "endConditionValue": 60.0,
                                "targetType": {"workoutTargetTypeKey": "pace.zone"},
                                "targetValueOne": 3.7037037,
                                "targetValueTwo": 3.3333333,
                            },
                            {
                                "type": "ExecutableStepDTO",
                                "stepOrder": 4,
                                "stepType": {"stepTypeKey": "recovery"},
                                "endCondition": {"conditionTypeKey": "lap.button"},
                                "targetType": {"workoutTargetTypeKey": "no.target"},
                            },
                        ],
                    },
                ],
            }
        ],
    }

    def test_placeholder_values_are_dropped(self):
        """Garmin's 0.0 for an untargeted or lap-button step means nothing."""
        from ha_garmin.client import _trim_workout_step

        step = {
            "stepOrder": 1,
            "stepType": {"stepTypeKey": "warmup"},
            "endCondition": {"conditionTypeKey": "lap.button"},
            "endConditionValue": 0.0,
            "targetType": {"workoutTargetTypeKey": "no.target"},
            "targetValueTwo": 0.0,
        }
        assert _trim_workout_step(step) == {
            "stepOrder": 1,
            "stepType": "warmup",
            "endCondition": "lap.button",
            "targetType": "no.target",
        }

    def test_detail_fields_flatten_steps_and_keep_repeats_nested(self):
        from ha_garmin.client import _workout_detail_fields

        assert _workout_detail_fields(self._WORKOUT) == {
            "estimatedDistanceInMeters": 4828.02,
            "workoutProvider": "FinalSurge",
            "workoutSteps": [
                {
                    "stepOrder": 1,
                    "stepType": "interval",
                    "endCondition": "distance",
                    "endConditionValue": 4828.02,
                    "targetType": "heart.rate.zone",
                    "targetValueOne": 141.0,
                    "targetValueTwo": 159.0,
                },
                {
                    "stepOrder": 2,
                    "stepType": "repeat",
                    "numberOfIterations": 4,
                    "steps": [
                        {
                            "stepOrder": 3,
                            "stepType": "interval",
                            "endCondition": "time",
                            "endConditionValue": 60.0,
                            "targetType": "pace.zone",
                            "targetValueOne": 3.7,
                            "targetValueTwo": 3.33,
                        },
                        {
                            "stepOrder": 4,
                            "stepType": "recovery",
                            "endCondition": "lap.button",
                            "targetType": "no.target",
                        },
                    ],
                },
            ],
        }

    def test_multisport_steps_carry_their_sport(self):
        from ha_garmin.client import _workout_detail_fields

        step = {"stepOrder": 1, "stepType": {"stepTypeKey": "interval"}}
        workout = {
            "workoutSegments": [
                {"sportType": {"sportTypeKey": "swimming"}, "workoutSteps": [step]},
                {"sportType": {"sportTypeKey": "cycling"}, "workoutSteps": [step]},
            ]
        }
        steps = _workout_detail_fields(workout)["workoutSteps"]
        assert [s["sportType"] for s in steps] == ["swimming", "cycling"]

    async def _fetch(self, client, items, workout):
        get_workout = AsyncMock(return_value=workout)
        with (
            patch.object(
                client, "get_activities", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                client, "get_workouts", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                client,
                "get_scheduled_workouts",
                new_callable=AsyncMock,
                return_value={"calendarItems": items},
            ),
            patch.object(
                client, "get_upcoming_events", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(client, "get_workout", get_workout),
        ):
            data = await client.fetch_activity_data()
        return data, get_workout

    async def test_today_and_next_get_steps_once_per_hour(self):
        client = GarminClient(_make_auth())
        item = {
            "id": 1810350286,
            "itemType": "workout",
            "title": "Easy 3 miles",
            "date": client.today().isoformat(),
            "workoutId": 1731319876,
        }
        data, get_workout = await self._fetch(client, [item], self._WORKOUT)

        steps = data["todayScheduledWorkout"]["workoutSteps"]
        assert steps[0]["targetValueOne"] == 141.0
        assert data["nextScheduledWorkout"]["workoutSteps"] == steps
        # The calendar list stays light.
        assert "workoutSteps" not in data["scheduledWorkouts"][0]
        # Today's and next are the same workout: one call, then cached.
        get_workout.assert_awaited_once_with(1731319876)
        _, get_workout = await self._fetch(client, [item], self._WORKOUT)
        get_workout.assert_not_awaited()

    async def test_adaptive_item_without_workout_id_is_left_alone(self):
        client = GarminClient(_make_auth())
        item = {
            "id": 1789814567000,
            "itemType": "fbtAdaptiveWorkout",
            "title": "Basis",
            "date": client.today().isoformat(),
        }
        data, get_workout = await self._fetch(client, [item], self._WORKOUT)
        get_workout.assert_not_awaited()
        assert "workoutSteps" not in data["todayScheduledWorkout"]

    async def test_update_and_delete_drop_cached_detail(self):
        client = GarminClient(_make_auth())
        client._slow_cache["workout_5"] = (0.0, {})
        with patch.object(client, "_put_request", new_callable=AsyncMock):
            await client.update_workout(5, {})
        assert "workout_5" not in client._slow_cache
        client._slow_cache["workout_5"] = (0.0, {})
        with patch.object(client, "_delete_request", new_callable=AsyncMock):
            await client.delete_workout(5)
        assert "workout_5" not in client._slow_cache


class TestTrainingStatusLabel:
    def test_phrase_wins_over_code_in_computed_fields(self):
        from ha_garmin.client import _add_computed_fields

        result = _add_computed_fields(
            {
                "trainingStatus": {
                    "mostRecentTrainingStatus": {
                        "latestTrainingStatusData": {
                            "123": {
                                "calendarDate": "2026-10-23",
                                "trainingStatus": 3,
                                "trainingStatusFeedbackPhrase": "OVERREACHING_1",
                            }
                        }
                    }
                }
            }
        )
        assert result["trainingStatusPhrase"] == "Overreaching"
