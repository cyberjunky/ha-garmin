"""Login blocks answered with HTTP 427 must be treated like 429 rate limits."""

from unittest.mock import MagicMock, patch

import pytest

from ha_garmin import GarminAuth
from ha_garmin.exceptions import GarminRateLimitError


def _session_returning(status: int) -> MagicMock:
    sess = MagicMock()
    sess.get.return_value = MagicMock(status_code=status, text="", ok=False)
    sess.post.return_value = MagicMock(status_code=status, text="", ok=False)
    return sess


@pytest.mark.parametrize("status", [427, 429])
def test_mobile_login_block_raises_rate_limit(status):
    auth = GarminAuth()
    with pytest.raises(GarminRateLimitError, match=f"returned {status}"):
        auth._do_mobile_login(_session_returning(status), "e@x.com", "pw")


@pytest.mark.parametrize("status", [427, 429])
def test_widget_embed_block_raises_rate_limit(status):
    auth = GarminAuth()
    with (
        patch(
            "ha_garmin.auth.cffi_requests.Session",
            return_value=_session_returning(status),
        ),
        pytest.raises(GarminRateLimitError, match=f"returned {status}"),
    ):
        auth._widget_web_login("e@x.com", "pw")


def test_mixed_427_and_429_count_as_all_strategies_blocked():
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
        patch.object(auth, "_portal_web_login_requests", side_effect=blocked(429)),
        pytest.raises(GarminRateLimitError, match="rate limited or blocked"),
    ):
        auth.login("e@x.com", "pw")
