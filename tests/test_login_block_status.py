"""Login blocks answered with HTTP 427 must be treated like 429 rate limits."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_garmin import GarminAuth
from ha_garmin.exceptions import GarminRateLimitError
from ha_garmin.http import HttpResponse


def _session_returning(status: int) -> MagicMock:
    sess = MagicMock()
    sess.get = AsyncMock(return_value=HttpResponse(status_code=status))
    sess.post = AsyncMock(return_value=HttpResponse(status_code=status))
    sess.close = AsyncMock()
    return sess


@pytest.mark.parametrize("status", [427, 429])
async def test_mobile_login_block_raises_rate_limit(status):
    auth = GarminAuth()
    with pytest.raises(GarminRateLimitError, match=f"returned {status}"):
        await auth._do_mobile_login(_session_returning(status), "e@x.com", "pw")


@pytest.mark.parametrize("status", [427, 429])
async def test_widget_embed_block_raises_rate_limit(status):
    auth = GarminAuth()
    sess = _session_returning(status)
    with (
        patch("ha_garmin.auth.LoginSession", return_value=sess),
        pytest.raises(GarminRateLimitError, match=f"returned {status}"),
    ):
        await auth._widget_web_login("e@x.com", "pw")
    sess.post.assert_not_awaited()
    sess.close.assert_awaited_once()


async def test_mixed_427_and_429_count_as_all_strategies_blocked():
    """A widget 427 among 429s must still yield the 'all blocked' error."""
    auth = GarminAuth()

    def blocked(status):
        def run(_email, _password):
            raise GarminRateLimitError(f"returned {status}")

        return run

    with (
        patch.object(auth, "_mobile_login_cffi", side_effect=blocked(429)),
        patch.object(auth, "_mobile_login_requests", side_effect=blocked(429)),
        patch.object(auth, "_widget_web_login", side_effect=blocked(427)),
        patch.object(auth, "_portal_web_login_cffi", side_effect=blocked(429)),
        pytest.raises(GarminRateLimitError, match="rate limited or blocked"),
    ):
        await auth.login("e@x.com", "pw")
