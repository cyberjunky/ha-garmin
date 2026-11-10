"""The mobile login loads the sign-in page before posting credentials."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_garmin import GarminAuth
from ha_garmin.exceptions import GarminAPIError, GarminAuthError, GarminRateLimitError
from ha_garmin.http import HttpResponse

_INVALID = HttpResponse(
    status_code=200,
    content=json.dumps(
        {"responseStatus": {"type": "INVALID_USERNAME_PASSWORD"}}
    ).encode(),
)


def _session(get_status=200):
    sess = MagicMock()
    sess.get = AsyncMock(return_value=HttpResponse(status_code=get_status))
    sess.post = AsyncMock(return_value=_INVALID)
    return sess


async def test_sign_in_page_is_loaded_before_the_credentials_post():
    """Without the page's cookies and Referer, Garmin answers the POST with 429."""
    auth = GarminAuth()
    sess = _session()
    calls = []
    sess.get.side_effect = lambda *a, **k: (
        calls.append("GET") or HttpResponse(status_code=200)
    )
    sess.post.side_effect = lambda *a, **k: calls.append("POST") or _INVALID
    sleep = AsyncMock(side_effect=lambda _s: calls.append("SLEEP"))
    with (
        patch("ha_garmin.auth.asyncio.sleep", sleep),
        pytest.raises(GarminAuthError),
    ):
        await auth._do_mobile_login(sess, "e@x.com", "pw")

    # The pause between page load and submit is awaited, not skipped.
    assert calls == ["GET", "SLEEP", "POST"]
    sleep.assert_awaited_once()
    get_url = sess.get.call_args.args[0]
    assert get_url == "https://sso.garmin.com/mobile/sso/en_US/sign-in"
    assert sess.get.call_args.kwargs["params"]["clientId"] == "GCM_IOS_DARK"
    post_headers = sess.post.call_args.kwargs["headers"]
    assert post_headers["Referer"].startswith(get_url + "?clientId=GCM_IOS_DARK")
    assert post_headers["Accept-Language"] == "en-US,en;q=0.9"


@pytest.mark.parametrize("status", [427, 429])
async def test_blocked_sign_in_page_stops_before_posting(status):
    sess = _session(get_status=status)
    with pytest.raises(GarminRateLimitError, match=f"returned {status}"):
        await GarminAuth()._do_mobile_login(sess, "e@x.com", "pw")
    sess.post.assert_not_called()


async def test_challenged_sign_in_page_stops_before_posting():
    sess = _session(get_status=403)
    with pytest.raises(GarminAPIError, match="403"):
        await GarminAuth()._do_mobile_login(sess, "e@x.com", "pw")
    sess.post.assert_not_called()
