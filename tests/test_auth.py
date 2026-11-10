"""Tests for GarminAuth."""

import asyncio
import base64
import json
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_garmin import GarminAuth, GarminAuthError
from ha_garmin.auth import (
    LOGIN_DELAY_MAX_S,
    LOGIN_DELAY_MIN_S,
    MOBILE_DELAY_MAX_S,
    MOBILE_DELAY_MIN_S,
    MOBILE_IMPERSONATIONS,
    WIDGET_DELAY_MAX_S,
    WIDGET_DELAY_MIN_S,
)
from ha_garmin.exceptions import (
    GarminAPIError,
    GarminConnectError,
    GarminConnectionError,
    GarminMFARequired,
    GarminRateLimitError,
)
from ha_garmin.http import HttpResponse, LoginSession


def _resp(
    status: int = 200,
    text: str = "",
    *,
    json_body: Any = None,
    url: str = "",
) -> HttpResponse:
    """An HttpResponse with a text or JSON body."""
    content = json.dumps(json_body) if json_body is not None else text
    return HttpResponse(status_code=status, content=content.encode(), url=url)


@contextmanager
def fake_login_sessions(
    *scripts: tuple[list[Any], list[Any]],
) -> Iterator[list[LoginSession]]:
    """Patch auth.LoginSession; the n-th session built answers with scripts[n].

    Each script is (get responses, post responses), consumed in order. The
    fakes subclass LoginSession so the isinstance() check in
    _clear_mfa_state still recognises them. Yields the sessions created.
    """
    created: list[LoginSession] = []
    pending = list(scripts)

    class FakeLoginSession(LoginSession):
        def __init__(self, impersonate: str | None = None, timeout: float = 30):
            super().__init__(impersonate, timeout)
            get, post = pending.pop(0) if pending else ([], [])
            self.get = AsyncMock(side_effect=list(get))  # type: ignore[method-assign]
            self.post = AsyncMock(side_effect=list(post))  # type: ignore[method-assign]
            self.close = AsyncMock()  # type: ignore[method-assign]
            created.append(self)

    with patch("ha_garmin.auth.LoginSession", FakeLoginSession):
        yield created


def _jwt(exp: float) -> str:
    """An unsigned JWT whose payload carries only ``exp``."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"h.{payload.rstrip('=')}.s"


_MFA_REQUIRED = {
    "responseStatus": {"type": "MFA_REQUIRED"},
    "customerMfaInfo": {"mfaLastMethodUsed": "email"},
}
_MFA_OK = {"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "ST-1"}
_DI_OK = {"access_token": "di_new", "refresh_token": "di_refresh_new"}


class TestGarminAuth:
    """Tests for GarminAuth class."""

    async def test_init(self):
        """Test auth initialization."""
        auth = GarminAuth()
        assert auth.di_token is None
        assert not auth.is_authenticated

    async def test_is_authenticated_with_di_token(self):
        """Test is_authenticated is True when DI token is set."""
        auth = GarminAuth()
        auth.di_token = "fake_di_token"
        assert auth.is_authenticated

    async def test_get_api_headers_not_authenticated(self):
        """Test get_api_headers raises when not authenticated."""
        auth = GarminAuth()
        with pytest.raises(GarminAuthError, match="Not authenticated"):
            auth.get_api_headers()

    async def test_get_api_headers_bearer(self):
        """Test get_api_headers returns Bearer header when DI token set."""
        auth = GarminAuth()
        auth.di_token = "mytoken"
        headers = auth.get_api_headers()
        assert headers["Authorization"] == "Bearer mytoken"

    async def test_get_api_base_url(self):
        """Test get_api_base_url returns connectapi.garmin.com."""
        auth = GarminAuth()
        assert "connectapi.garmin.com" in auth.get_api_base_url()

    async def test_verify_token_true_on_200(self):
        """A 200 from socialProfile means the token is accepted."""
        auth = GarminAuth()
        auth.di_token = "tok"
        with patch(
            "ha_garmin.auth.impersonated_request",
            AsyncMock(return_value=_resp(200)),
        ) as request:
            assert await auth._verify_token() is True
        assert request.await_args.args[0] == "GET"
        assert request.await_args.args[1].endswith("/userprofile-service/socialProfile")

    @pytest.mark.parametrize("status", [401, 403])
    async def test_verify_token_false_on_auth_rejection(self, status):
        """A 401/403 means the API tier rejected the token."""
        auth = GarminAuth()
        auth.di_token = "tok"
        with patch(
            "ha_garmin.auth.impersonated_request",
            AsyncMock(return_value=_resp(status)),
        ):
            assert await auth._verify_token() is False

    async def test_verify_token_inconclusive_keeps_token(self):
        """A transient error must not reject an otherwise-working token."""
        auth = GarminAuth()
        auth.di_token = "tok"
        with patch(
            "ha_garmin.auth.impersonated_request",
            AsyncMock(side_effect=GarminAPIError("Request failed: network")),
        ):
            assert await auth._verify_token() is True

    async def test_verify_token_false_when_unauthenticated(self):
        """No token at all cannot be valid."""
        auth = GarminAuth()
        assert await auth._verify_token() is False

    @pytest.mark.parametrize(("status", "hinted"), [(401, False), (403, True)])
    async def test_rejected_strategy_error_names_status(self, status, hinted):
        """The login error carries the HTTP status; only a 403 gets the
        client/network-block hint, since a 401 really is a refused token.
        """
        from ha_garmin.models import AuthResult

        auth = GarminAuth()

        def strategy(_email, _password):
            auth.di_token = "tok"
            return AuthResult(success=True)

        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=strategy),
            patch.object(auth, "_mobile_login_requests", side_effect=strategy),
            patch.object(auth, "_widget_web_login", side_effect=strategy),
            patch.object(auth, "_portal_web_login_cffi", side_effect=strategy),
            patch.object(auth, "_portal_web_login_requests", side_effect=strategy),
            patch(
                "ha_garmin.auth.impersonated_request",
                AsyncMock(return_value=_resp(status)),
            ),
            pytest.raises(GarminAPIError) as excinfo,
        ):
            await auth.login("e@x.com", "pw")
        message = str(excinfo.value)
        assert f"token rejected by API tier (HTTP {status})" in message
        assert ("client or network" in message) is hinted

    async def test_rejected_mfa_token_error_hints_on_403(self):
        """complete_mfa() reports a 403 rejection with the same hint."""
        auth = GarminAuth()
        auth._mfa_session = MagicMock()  # an MFA login is pending

        def complete(_code):
            auth.di_token = "tok"

        with (
            patch.object(auth, "_complete_mfa", side_effect=complete),
            patch(
                "ha_garmin.auth.impersonated_request",
                AsyncMock(return_value=_resp(403)),
            ),
            pytest.raises(GarminAuthError, match=r"HTTP 403.*client or network"),
        ):
            await auth.complete_mfa("123456")

    async def test_verify_token_success_clears_last_rejection(self):
        """An accepted token must not keep an earlier rejection's status."""
        auth = GarminAuth()
        auth.di_token = "tok"
        auth._last_rejection_status = 403
        with patch(
            "ha_garmin.auth.impersonated_request",
            AsyncMock(return_value=_resp(200)),
        ):
            assert await auth._verify_token() is True
        assert auth._last_rejection_status is None
        assert auth._token_rejected_message("x") == "x"

    async def test_login_falls_through_rejected_token(self):
        """A strategy whose token the API rejects must not win the chain;
        the next strategy that validates should.
        """
        from ha_garmin.models import AuthResult

        auth = GarminAuth()

        def first_strategy(_sess_or_email=None, _password=None):
            auth.di_token = "poisoned"
            return AuthResult(success=True)

        def second_strategy(_email, _password):
            auth.di_token = "good"
            return AuthResult(success=True)

        verify_results = iter([False, True])
        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=second_strategy),
            patch.object(auth, "_mobile_login_requests", side_effect=second_strategy),
            patch.object(auth, "_widget_web_login", side_effect=first_strategy),
            patch.object(auth, "_portal_web_login_cffi", side_effect=second_strategy),
            patch.object(
                auth, "_portal_web_login_requests", side_effect=second_strategy
            ),
            patch.object(
                auth, "_verify_token", side_effect=lambda: next(verify_results)
            ),
        ):
            # CN order runs widget first → its token is rejected → fall through.
            auth._is_cn = True
            await auth.login("e@x.com", "pw")
        assert auth.di_token == "good"

    async def test_login_success_non_mfa_does_not_set_pending(self):
        """A normal non-MFA login must succeed and leave _mfa_pending False."""
        from ha_garmin.models import AuthResult

        auth = GarminAuth()

        def working_strategy(_email, _password):
            auth.di_token = "good"
            auth.di_refresh_token = "refresh"
            auth.di_client_id = "CID"
            return AuthResult(success=True)

        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=working_strategy),
            patch.object(auth, "_verify_token", return_value=True),
        ):
            result = await auth.login("u@x.com", "pw")

        assert result.success is True
        assert auth._mfa_pending is False
        assert auth.is_authenticated

    async def test_refresh_session_not_authenticated(self):
        """Test refresh_session returns False when not authenticated."""
        auth = GarminAuth()
        result = await auth.refresh_session()
        assert result is False

    async def test_save_load_session(self, tmp_path):
        """Test round-trip save and load of tokens."""
        token_file = tmp_path / "garmin_tokens.json"
        auth = GarminAuth()
        auth.di_token = "di_abc"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2"

        auth.save_session(str(token_file))

        auth2 = GarminAuth()
        loaded = auth2.load_session(str(token_file))
        assert loaded is True
        assert auth2.di_token == "di_abc"
        assert auth2.di_refresh_token == "di_refresh"
        assert auth2.is_authenticated

    async def test_load_session_missing_file(self, tmp_path):
        """Test load_session returns False for missing file."""
        auth = GarminAuth()
        result = auth.load_session(str(tmp_path / "nonexistent.json"))
        assert result is False

    async def test_load_session_empty_tokens(self, tmp_path):
        """Test load_session returns False when tokens are missing."""
        import json

        token_file = tmp_path / "garmin_tokens.json"
        token_file.write_text(json.dumps({}))
        auth = GarminAuth()
        result = auth.load_session(str(token_file))
        assert result is False

    # ------------------------------------------------------------------ #
    #  Widget MFA detection & OTP delivery                                #
    # ------------------------------------------------------------------ #

    def _widget_mfa_page(
        self, title: str, mfa_method: str = "", code_sent_to: str = ""
    ) -> str:
        """Build a widget MFA page with the inline JS vars Garmin emits."""
        vars_ = [
            'var customerGuid = "cg-123";',
            f'var mfaMethod = "{mfa_method}";',
            'var locale = "en-US";',
            'var clientId = "";',
        ]
        if code_sent_to:
            vars_.append(f'var codeSentTo = "{code_sent_to}";')
        return (
            f"<html><head><title>{title}</title></head><body>"
            f"<script>{' '.join(vars_)}</script></body></html>"
        )

    def _widget_script(
        self, post_page: str, extra_posts: list[Any] | None = None
    ) -> tuple[list[Any], list[Any]]:
        """Session script that drives _widget_web_login to the POST response."""
        embed_resp = _resp(200, "<html></html>")
        signin_resp = _resp(
            200,
            '<input name="_csrf" value="tok">',
            url="https://sso.garmin.com/sso/signin",
        )
        return [embed_resp, signin_resp], [_resp(200, post_page), *(extra_posts or [])]

    async def _run_widget(
        self, post_page: str, extra_posts: list[Any] | None = None
    ) -> tuple[GarminAuth, LoginSession, AsyncMock, BaseException]:
        """Run _widget_web_login and return what it raised."""
        auth = GarminAuth()
        sleep = AsyncMock()
        with (
            fake_login_sessions(self._widget_script(post_page, extra_posts)) as made,
            patch("ha_garmin.auth.asyncio.sleep", sleep),
            pytest.raises(GarminConnectError) as excinfo,
        ):
            await auth._widget_web_login("u@x.com", "pw")
        (sess,) = made
        return auth, sess, sleep, excinfo.value

    async def test_widget_totp_mfa_title_raises_mfa_required(self) -> None:
        """TOTP MFA title ('Enter MFA code for login') triggers GarminMFARequired."""
        auth, sess, sleep, err = await self._run_widget(
            self._widget_mfa_page("Enter MFA code for login", mfa_method="totp")
        )
        assert isinstance(err, GarminMFARequired)
        assert not any(
            "/sso/verifyMFA/mfaCode" in call.args[0]
            for call in sess.post.await_args_list
        )
        # The anti-WAF pause between the CSRF GET and the credential POST.
        sleep.assert_awaited_once()
        assert WIDGET_DELAY_MIN_S <= sleep.await_args.args[0] <= WIDGET_DELAY_MAX_S
        # MFA pending: the session stays open for complete_mfa().
        assert auth._mfa_session is sess
        assert auth._mfa_pending is True
        sess.close.assert_not_awaited()

    async def test_widget_email_mfa_title_requests_code(self) -> None:
        """Email MFA title triggers GarminMFARequired and requests a code."""
        _, sess, _, err = await self._run_widget(
            self._widget_mfa_page(
                "GARMIN Authentication Application", mfa_method="email"
            ),
            extra_posts=[_resp(200)],
        )
        assert isinstance(err, GarminMFARequired)
        assert any(
            "/sso/verifyMFA/mfaCode" in call.args[0]
            for call in sess.post.await_args_list
        )

    async def test_widget_email_mfa_already_sent_no_request(self) -> None:
        """If Garmin already sent a code, do not request another one."""
        page = self._widget_mfa_page(
            "Enter MFA code for login",
            mfa_method="email",
            code_sent_to="u@example.com",
        )
        _, sess, _, err = await self._run_widget(page)
        assert isinstance(err, GarminMFARequired)
        assert not any(
            "/sso/verifyMFA/mfaCode" in call.args[0]
            for call in sess.post.await_args_list
        )

    async def test_widget_bare_signin_title_not_mfa(self) -> None:
        """The bare signin page title falls through to portal strategies."""
        auth, sess, _, err = await self._run_widget(
            self._widget_mfa_page("GARMIN Authentication Application")
        )
        assert isinstance(err, GarminAPIError)
        assert "auth application page" in str(err)
        sess.close.assert_awaited_once()
        assert auth._mfa_pending is False

    async def test_widget_unrelated_title_raises_api_error(self) -> None:
        """An unrecognised title raises GarminAPIError, not GarminMFARequired."""
        _, sess, _, err = await self._run_widget(
            self._widget_mfa_page("Some Random Page")
        )
        assert isinstance(err, GarminAPIError)
        assert "unexpected title" in str(err)
        sess.close.assert_awaited_once()

    async def test_widget_mfa_code_request_failure_closes_session(self) -> None:
        """A failed MFA code request is not a pending MFA: the session closes."""
        auth, sess, _, err = await self._run_widget(
            self._widget_mfa_page(
                "GARMIN Authentication Application", mfa_method="email"
            ),
            extra_posts=[_resp(500)],
        )
        assert isinstance(err, GarminAPIError)
        sess.close.assert_awaited_once()
        assert auth._mfa_pending is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
    async def test_save_session_owner_only_permissions(self, tmp_path):
        """Token file/dir must be owner-only (0o600/0o700) under any umask.

        Regression guard for the world-readable token store vulnerability —
        the file holds the DI refresh token.
        """
        old_umask = os.umask(0o022)
        try:
            token_dir = tmp_path / "tokens"
            auth = GarminAuth()
            auth.di_token = "di_abc"
            auth.di_refresh_token = "di_refresh"
            auth.di_client_id = "CID"
            auth.save_session(str(token_dir))

            token_file = token_dir / ".garmin_tokens.json"
            dir_mode = stat.S_IMODE(token_dir.stat().st_mode)
            file_mode = stat.S_IMODE(token_file.stat().st_mode)
            assert file_mode == 0o600, oct(file_mode)
            assert dir_mode == 0o700, oct(dir_mode)
            assert not (file_mode & (stat.S_IRWXG | stat.S_IRWXO))
        finally:
            os.umask(old_umask)

    # ------------------------------------------------------------------ #
    #  Security hardening                                                  #
    # ------------------------------------------------------------------ #

    async def test_save_session_failure_logs_warning(self, tmp_path, caplog):
        """A failed token persistence write must be logged at WARNING."""
        auth = GarminAuth()
        auth.di_token = "di_abc"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"
        auth._tokenstore_path = str(tmp_path / "tokens")

        with (
            patch.object(auth, "_refresh_di_token") as mock_refresh,
            patch.object(auth, "save_session", side_effect=OSError("read-only fs")),
        ):
            result = await auth.refresh_session()

        # The refresh itself succeeded (we faked the network side); persistence failed.
        mock_refresh.assert_called_once()
        assert result is True
        assert "Failed to persist tokens" in caplog.text
        assert "read-only fs" in caplog.text

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
    async def test_save_session_rejects_symlinked_directory(self, tmp_path):
        """A symlinked tokenstore directory must not be followed."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        symlink_dir = tmp_path / "link"
        symlink_dir.symlink_to(real_dir)

        auth = GarminAuth()
        auth.di_token = "di_abc"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"

        # The symlinked path must be rejected before any write occurs.
        with pytest.raises(ValueError, match="Token path must not be a symlink"):
            auth.save_session(str(symlink_dir))
        assert not any(real_dir.iterdir())

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
    async def test_load_session_rejects_symlinked_file(self, tmp_path):
        """A symlinked token file must not be followed on load."""
        real_file = tmp_path / "real_tokens.json"
        symlink_file = tmp_path / "link_tokens.json"
        real_file.write_text('{"token": "x"}')
        symlink_file.symlink_to(real_file)

        auth = GarminAuth()
        result = auth.load_session(str(symlink_file))
        assert result is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
    async def test_save_session_allows_root_owned_symlinked_parent(self, tmp_path):
        """A root-owned symlink (e.g. /home -> var/home) is not attacker-plantable."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        symlink_dir = tmp_path / "link"
        symlink_dir.symlink_to(real_dir)
        real_lstat = Path.lstat

        def fake_lstat(self):
            st = real_lstat(self)
            if self == symlink_dir:
                return os.stat_result((st.st_mode, *st[1:4], 0, *st[5:]))
            return st

        auth = GarminAuth()
        auth.di_token = "di_abc"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"
        with patch.object(Path, "lstat", fake_lstat):
            auth.save_session(str(symlink_dir / "tokens.json"))
        assert (real_dir / "tokens.json").exists()

    async def test_refresh_session_holds_token_lock(self):
        """refresh_session must execute the refresh while holding the token lock."""
        auth = GarminAuth()
        auth.di_token = "di_abc"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"

        lock_held_during_refresh = []

        async def tracked_refresh_di_token() -> None:
            # refresh_session acquires the lock before calling this helper.
            lock_held_during_refresh.append(auth._token_lock.locked())

        with patch.object(
            auth, "_refresh_di_token", side_effect=tracked_refresh_di_token
        ):
            result = await auth.refresh_session()

        assert result is True
        assert lock_held_during_refresh == [True]
        assert not auth._token_lock.locked()

    async def test_refresh_if_expiring_refreshes_once_for_concurrent_callers(self):
        """Tasks that waited on the lock see the new token and skip the refresh."""
        auth = GarminAuth()
        auth.di_token = _jwt(time.time() - 60)
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"

        async def slow_refresh() -> None:
            assert auth._token_lock.locked()
            await asyncio.sleep(0)  # let the other caller queue on the lock
            auth.di_token = _jwt(time.time() + 3600)

        with patch.object(auth, "_refresh_di_token", side_effect=slow_refresh) as ref:
            await asyncio.gather(auth.refresh_if_expiring(), auth.refresh_if_expiring())
        ref.assert_awaited_once()

    async def test_refresh_if_expiring_skips_fresh_token(self):
        auth = GarminAuth()
        auth.di_token = _jwt(time.time() + 3600)
        with patch.object(auth, "_refresh_di_token") as ref:
            await auth.refresh_if_expiring()
        ref.assert_not_awaited()

    async def test_refresh_success_updates_tokens_and_persists(self, tmp_path):
        auth = GarminAuth()
        auth.di_token = "di_old"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"
        auth._tokenstore_path = str(tmp_path / "store")
        post = AsyncMock(return_value=_resp(200, json_body=_DI_OK))
        with patch("ha_garmin.auth._http_post", post):
            assert await auth.refresh_session() is True
        assert auth.di_token == "di_new"
        assert auth.di_refresh_token == "di_refresh_new"
        assert post.await_args.kwargs["data"]["refresh_token"] == "di_refresh"
        saved = json.loads((tmp_path / "store" / ".garmin_tokens.json").read_text())
        assert saved["token"] == "di_new"
        assert auth.last_refresh_error is None

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (GarminAPIError("Request failed: down"), GarminConnectionError),
            (_resp(503), GarminConnectionError),
            (_resp(500), GarminConnectionError),
            (_resp(429), GarminRateLimitError),
            (_resp(400), GarminAuthError),
            (_resp(401), GarminAuthError),
        ],
    )
    async def test_refresh_failure_classification(self, outcome, expected):
        """Network/5xx are transient, 429 is a rate limit, other 4xx refuse the token."""
        auth = GarminAuth()
        auth.di_token = "di_old"
        auth.di_refresh_token = "di_refresh"
        auth.di_client_id = "CID"
        post = AsyncMock(
            side_effect=outcome if isinstance(outcome, Exception) else None,
            return_value=outcome,
        )
        with patch("ha_garmin.auth._http_post", post):
            with pytest.raises(expected):
                await auth._refresh_di_token()
            # refresh_session swallows it but records the cause.
            assert await auth.refresh_session() is False
        assert isinstance(auth.last_refresh_exception, expected)
        assert auth.last_refresh_error
        # The stored tokens survive a failed refresh.
        assert auth.di_token == "di_old"
        assert auth.di_refresh_token == "di_refresh"

    async def test_refresh_without_refresh_token_is_auth_error(self):
        auth = GarminAuth()
        auth.di_token = "di_old"
        with pytest.raises(GarminAuthError, match="No DI refresh token"):
            await auth._refresh_di_token()

    async def test_login_rejects_interleaved_mfa(self):
        """login() must refuse to start when an MFA flow is already pending."""
        auth = GarminAuth()
        auth._mfa_pending = True
        with pytest.raises(GarminAuthError, match="MFA login already in progress"):
            await auth.login("u@x.com", "pw")

    # ------------------------------------------------------------------ #
    #  Login session lifecycle                                           #
    # ------------------------------------------------------------------ #

    async def test_failed_strategy_closes_its_session(self):
        """A strategy that fails closes the LoginSession it opened."""
        auth = GarminAuth()
        with (
            fake_login_sessions(([_resp(429)], [])) as made,
            pytest.raises(GarminRateLimitError),
        ):
            await auth._mobile_login_requests("u@x.com", "pw")
        (sess,) = made
        assert sess._impersonate is None  # plain aiohttp session
        sess.close.assert_awaited_once()
        sess.post.assert_not_awaited()

    async def test_cffi_rotation_closes_every_session(self):
        """Each impersonation gets its own session, closed when it fails."""
        auth = GarminAuth()
        scripts = [([_resp(429)], []) for _ in MOBILE_IMPERSONATIONS]
        with (
            fake_login_sessions(*scripts) as made,
            pytest.raises(GarminRateLimitError),
        ):
            await auth._mobile_login_cffi("u@x.com", "pw")
        assert [s._impersonate for s in made] == list(MOBILE_IMPERSONATIONS)
        for sess in made:
            sess.close.assert_awaited_once()

    async def test_successful_strategy_closes_its_session(self):
        auth = GarminAuth()
        script = (
            [_resp(200)],
            [_resp(200, json_body=_MFA_OK)],
        )
        with (
            fake_login_sessions(script) as made,
            patch("ha_garmin.auth.asyncio.sleep", AsyncMock()) as sleep,
            patch(
                "ha_garmin.auth._http_post",
                AsyncMock(return_value=_resp(200, json_body=_DI_OK)),
            ),
        ):
            result = await auth._mobile_login_requests("u@x.com", "pw")
        assert result.success is True
        assert auth.di_token == "di_new"
        made[0].close.assert_awaited_once()
        sleep.assert_awaited_once()
        assert MOBILE_DELAY_MIN_S <= sleep.await_args.args[0] <= MOBILE_DELAY_MAX_S

    async def test_portal_login_awaits_anti_waf_delay(self):
        """The portal flow pauses between the GET and the credential POST."""
        auth = GarminAuth()
        sleep = AsyncMock()
        script = (
            [_resp(200)],
            [
                _resp(
                    200,
                    json_body={"responseStatus": {"type": "INVALID_USERNAME_PASSWORD"}},
                )
            ],
        )
        with (
            fake_login_sessions(script) as made,
            patch("ha_garmin.auth.asyncio.sleep", sleep),
            pytest.raises(GarminAuthError, match="Invalid Username"),
        ):
            await auth._portal_web_login_requests("u@x.com", "pw")
        (made_sess,) = made
        sleep.assert_awaited_once()
        assert LOGIN_DELAY_MIN_S <= sleep.await_args.args[0] <= LOGIN_DELAY_MAX_S
        assert made_sess.get.await_count == 1
        assert made_sess.post.await_count == 1
        made_sess.close.assert_awaited_once()

    async def _login_until_mfa(
        self, auth: GarminAuth, posts: list[Any]
    ) -> list[LoginSession]:
        """Run login() so mobile+cffi stops at MFA; later posts answer verify calls."""
        script = ([_resp(200)], [_resp(200, json_body=_MFA_REQUIRED), *posts])
        with (
            fake_login_sessions(script) as made,
            patch("ha_garmin.auth.asyncio.sleep", AsyncMock()),
            pytest.raises(GarminMFARequired),
        ):
            await auth.login("u@x.com", "pw")
        return made

    async def test_mfa_required_keeps_session_open_until_complete_mfa(self):
        """MFA state (and its open session) survives from login to complete_mfa."""
        auth = GarminAuth()
        made = await self._login_until_mfa(auth, [_resp(200, json_body=_MFA_OK)])
        (sess,) = made
        assert sess._impersonate == MOBILE_IMPERSONATIONS[0]
        assert auth._mfa_session is sess
        assert auth._mfa_pending is True
        assert auth._mfa_flow == "ios"
        sess.close.assert_not_awaited()

        with (
            patch(
                "ha_garmin.auth._http_post",
                AsyncMock(return_value=_resp(200, json_body=_DI_OK)),
            ),
            patch(
                "ha_garmin.auth.impersonated_request",
                AsyncMock(return_value=_resp(200)),
            ),
        ):
            result = await auth.complete_mfa("123456")

        assert result.success is True
        assert auth.di_token == "di_new"
        verify = sess.post.await_args_list[-1]
        assert verify.args[0].endswith("/mobile/api/mfa/verifyCode")
        assert verify.kwargs["json"]["mfaVerificationCode"] == "123456"
        sess.close.assert_awaited_once()
        assert auth._mfa_session is None
        assert auth._mfa_pending is False

    async def test_complete_mfa_failure_closes_session(self):
        auth = GarminAuth()
        refused = _resp(200, json_body={"error": {"status-code": "401"}})
        (sess,) = await self._login_until_mfa(auth, [refused, refused])

        with pytest.raises(GarminAuthError, match="MFA verification failed"):
            await auth.complete_mfa("000000")

        # Both verify endpoints were tried on the same session.
        assert sess.post.await_count == 3
        sess.close.assert_awaited_once()
        assert auth._mfa_session is None
        assert auth._mfa_pending is False
        assert not auth.is_authenticated

    async def test_complete_mfa_rejected_token_closes_session(self):
        auth = GarminAuth()
        (sess,) = await self._login_until_mfa(auth, [_resp(200, json_body=_MFA_OK)])
        with (
            patch(
                "ha_garmin.auth._http_post",
                AsyncMock(return_value=_resp(200, json_body=_DI_OK)),
            ),
            patch(
                "ha_garmin.auth.impersonated_request",
                AsyncMock(return_value=_resp(401)),
            ),
            pytest.raises(GarminAuthError, match="after MFA"),
        ):
            await auth.complete_mfa("123456")
        sess.close.assert_awaited_once()
        assert not auth.is_authenticated
        assert auth._mfa_pending is False

    async def test_complete_mfa_without_pending_session(self):
        with pytest.raises(GarminAuthError, match="No pending MFA session"):
            await GarminAuth().complete_mfa("123456")

    async def test_logout_closes_pending_mfa_session(self):
        auth = GarminAuth()
        (sess,) = await self._login_until_mfa(auth, [])
        await auth.logout()
        sess.close.assert_awaited_once()
        assert auth._mfa_session is None
        assert auth._mfa_pending is False

    async def test_new_login_allowed_after_logout_of_pending_mfa(self):
        """After logout() drops a pending MFA, login() starts again."""
        auth = GarminAuth()
        await self._login_until_mfa(auth, [])
        await auth.logout()
        made = await self._login_until_mfa(auth, [])
        assert auth._mfa_session is made[0]


class TestSecurityAuditHardening:
    """Regression tests for the python-garminconnect audit cross-check."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
    def test_token_path_rejects_symlink_two_levels_up(self, tmp_path):
        """A symlink planted above the immediate parent must also be rejected."""
        from ha_garmin.auth import token_file_path

        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "cfg").mkdir()
        (tmp_path / "cfg" / "store").symlink_to(real)

        target = tmp_path / "cfg" / "store" / "sub" / ".garminconnect"
        with pytest.raises(ValueError, match="must not be a symlink"):
            token_file_path(str(target))

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
    def test_load_session_logs_warning_on_symlink_rejection(self, tmp_path, caplog):
        """A rejected (e.g. symlinked) tokenstore path is a security event and
        must be logged, not silently swallowed."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        auth = GarminAuth()
        with caplog.at_level("WARNING", logger="ha_garmin.auth"):
            result = auth.load_session(str(link))

        assert result is False
        assert "Refusing to load tokens" in caplog.text

    def test_load_session_logs_warning_on_corrupt_file(self, tmp_path, caplog):
        """A corrupt token file must log a warning without token values."""
        store = tmp_path / "store"
        store.mkdir()
        (store / ".garmin_tokens.json").write_text("{not json")

        auth = GarminAuth()
        with caplog.at_level("WARNING", logger="ha_garmin.auth"):
            result = auth.load_session(str(store))

        assert result is False
        assert "Failed to load tokens" in caplog.text
        assert "{not json" not in caplog.text

    async def test_logout_clears_state_and_deletes_token_file(self, tmp_path):
        auth = GarminAuth()
        auth.di_token = "tok"
        auth.di_refresh_token = "refresh"
        auth.di_client_id = "CID"
        store = tmp_path / "store"
        auth._tokenstore_path = str(store)
        store.mkdir()
        token_file = store / ".garmin_tokens.json"
        token_file.write_text("{}")

        await auth.logout()

        assert auth.di_token is None
        assert auth.di_refresh_token is None
        assert auth.di_client_id is None
        assert auth._tokenstore_path is None
        assert not token_file.exists()

    async def test_login_clears_stale_auth_state_on_entry(self):
        """A failed re-login must not leave is_authenticated True with old
        tokens."""
        auth = GarminAuth()
        auth.di_token = "stale-token"

        def fail(_email, _password):
            raise GarminAPIError("boom")

        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=fail),
            patch.object(auth, "_mobile_login_requests", side_effect=fail),
            patch.object(auth, "_widget_web_login", side_effect=fail),
            patch.object(auth, "_portal_web_login_cffi", side_effect=fail),
            patch.object(auth, "_portal_web_login_requests", side_effect=fail),
            pytest.raises(GarminAPIError, match="exhausted"),
        ):
            await auth.login("u@x.com", "pw")

        assert auth.di_token is None
        assert not auth.is_authenticated

    async def test_login_failure_redacts_url_query_values(self, caplog):
        """requests embeds full URLs in exception text; query-string values
        must not reach the log or the raised error."""
        auth = GarminAuth()
        boom = Exception(
            "Max retries exceeded with url: /x?ticket=ST-CANARY-123 (Caused by ...)"
        )

        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=boom),
            patch.object(auth, "_mobile_login_requests", side_effect=boom),
            patch.object(auth, "_widget_web_login", side_effect=boom),
            patch.object(auth, "_portal_web_login_cffi", side_effect=boom),
            patch.object(auth, "_portal_web_login_requests", side_effect=boom),
            caplog.at_level("DEBUG", logger="ha_garmin.auth"),
            pytest.raises(GarminAPIError, match="exhausted") as exc_info,
        ):
            await auth.login("u@x.com", "pw")

        assert "ST-CANARY-123" not in caplog.text
        assert "ST-CANARY-123" not in str(exc_info.value)
        assert "ticket=<redacted>" in caplog.text

    async def test_failed_strategies_log_at_debug_and_summarize(self, caplog):
        """A strategy failing is expected; only the final error reports them."""
        auth = GarminAuth()
        boom = ConnectionError("down")
        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=boom),
            patch.object(auth, "_mobile_login_requests", side_effect=boom),
            patch.object(auth, "_widget_web_login", side_effect=boom),
            patch.object(auth, "_portal_web_login_cffi", side_effect=boom),
            patch.object(auth, "_portal_web_login_requests", side_effect=boom),
            caplog.at_level("WARNING", logger="ha_garmin.auth"),
            pytest.raises(GarminAPIError, match="ConnectionError; ") as exc_info,
        ):
            await auth.login("u@x.com", "pw")
        assert caplog.text == ""
        assert "mobile+cffi: ConnectionError" in str(exc_info.value)

    def test_sanitize_exception_text_redacts_query_values(self):
        from ha_garmin.auth import _sanitize_exception_text

        out = _sanitize_exception_text(Exception("url: /x?ticket=ST-1&foo=bar ok"))
        assert "ST-1" not in out
        assert "bar" not in out
        assert "ticket=<redacted>" in out
        assert "foo=<redacted>" in out


async def test_di_exchange_failure_log_omits_response_body(caplog):
    """A failed DI exchange logs the status, never the token endpoint's body."""
    auth = GarminAuth()
    failed = _resp(400, "ST-CANARY-TICKET-123 detail")
    with (
        patch("ha_garmin.auth._http_post", AsyncMock(return_value=failed)),
        caplog.at_level("DEBUG", logger="ha_garmin.auth"),
        pytest.raises(GarminAuthError),
    ):
        await auth._exchange_service_ticket("ST-CANARY-TICKET-123")
    assert "HTTP 400" in caplog.text
    assert "ST-CANARY-TICKET-123" not in caplog.text


async def test_di_exchange_tries_next_client_id_and_stores_tokens():
    auth = GarminAuth()
    post = AsyncMock(side_effect=[_resp(400), _resp(200, json_body=_DI_OK)])
    with patch("ha_garmin.auth._http_post", post):
        await auth._exchange_service_ticket("ST-1", service_url="https://svc")
    assert post.await_count == 2
    first, second = (c.kwargs["data"]["client_id"] for c in post.await_args_list)
    assert first != second
    assert post.await_args.kwargs["data"]["service_url"] == "https://svc"
    assert auth.di_token == "di_new"
    assert auth.di_refresh_token == "di_refresh_new"
    assert auth.di_client_id == second


async def test_di_exchange_rate_limit_stops():
    auth = GarminAuth()
    post = AsyncMock(return_value=_resp(429))
    with (
        patch("ha_garmin.auth._http_post", post),
        pytest.raises(GarminRateLimitError),
    ):
        await auth._exchange_service_ticket("ST-1")
    post.assert_awaited_once()


# ── Review follow-ups ─────────────────────────────────────────────────────────


def _authed() -> GarminAuth:
    auth = GarminAuth()
    auth.di_token = "old_token"
    auth.di_refresh_token = "refresh"
    auth.di_client_id = "CID"
    return auth


async def test_concurrent_401s_refresh_once() -> None:
    """Requests refused with the same token share one refresh."""
    auth = _authed()
    refreshed = HttpResponse(
        status_code=200, content=json.dumps({"access_token": "new_token"}).encode()
    )
    post = AsyncMock(return_value=refreshed)
    with patch("ha_garmin.auth._http_post", post):
        results = await asyncio.gather(
            *(auth.refresh_session(rejected_token="old_token") for _ in range(3))
        )
    assert results == [True, True, True]
    assert auth.di_token == "new_token"
    post.assert_awaited_once()


async def test_refresh_without_access_token_is_transient() -> None:
    """A 200 without a token is a Garmin glitch, not a refused refresh token."""
    auth = _authed()
    for body in (b"<html/>", b"{}"):
        reply = HttpResponse(status_code=200, content=body)
        with patch("ha_garmin.auth._http_post", AsyncMock(return_value=reply)):
            assert await auth.refresh_session() is False
        assert isinstance(auth.last_refresh_exception, GarminConnectionError)
        assert auth.di_token == "old_token"


async def test_mfa_cleanup_survives_a_failing_close() -> None:
    """A session that fails to close still leaves no MFA state behind."""
    auth = GarminAuth()
    session = LoginSession()
    session.close = AsyncMock(side_effect=OSError("close failed"))  # type: ignore[method-assign]
    auth._mfa_session = session
    auth._mfa_pending = True

    await auth._clear_mfa_state()

    assert auth._mfa_session is None
    assert auth._mfa_pending is False
    session.close.assert_awaited_once()


async def test_cancel_mfa_keeps_stored_tokens(tmp_path: Path) -> None:
    """cancel_mfa releases the pending session but, unlike logout, keeps the token file."""
    auth = _authed()
    token_file = tmp_path / "tokens.json"
    auth.save_session(token_file)
    assert auth.load_session(token_file)
    session = LoginSession()
    session.close = AsyncMock()  # type: ignore[method-assign]
    auth._mfa_session = session
    auth._mfa_pending = True

    await auth.cancel_mfa()

    session.close.assert_awaited_once()
    assert auth._mfa_pending is False
    assert token_file.exists()
    assert auth.di_token == "old_token"


# ── Single-strategy login ─────────────────────────────────────────────────────


def test_login_strategy_order() -> None:
    """Mobile strategies lead for .com accounts; web strategies lead for China."""
    names = [n for n, _ in GarminAuth()._login_strategies("e", "p")]
    assert names == [
        "mobile+cffi",
        "mobile+aiohttp",
        "widget+cffi",
        "portal+cffi",
        "portal+aiohttp",
    ]
    cn = [n for n, _ in GarminAuth(is_cn=True)._login_strategies("e", "p")]
    assert cn[:3] == ["widget+cffi", "portal+cffi", "portal+aiohttp"]


async def test_login_with_strategy_runs_only_that_strategy() -> None:
    """No fallback: only the named strategy runs, and its failure is raised."""
    auth = GarminAuth()
    with (
        patch.object(
            auth, "_widget_web_login", AsyncMock(side_effect=GarminAPIError("blocked"))
        ),
        patch.object(auth, "_mobile_login_cffi", AsyncMock()) as mobile,
        pytest.raises(GarminAPIError, match="blocked"),
    ):
        await auth.login_with_strategy("widget+cffi", "e", "p")
    mobile.assert_not_awaited()


async def test_login_with_strategy_verifies_the_token() -> None:
    """A token the API tier rejects is an error, and no token is kept."""
    auth = GarminAuth()

    async def fake_login(email: str, password: str):
        auth.di_token = "tok"

    with (
        patch.object(auth, "_portal_web_login_cffi", fake_login),
        patch.object(auth, "_verify_token", AsyncMock(return_value=False)),
        pytest.raises(GarminAPIError, match="portal\\+cffi: token rejected"),
    ):
        await auth.login_with_strategy("portal+cffi", "e", "p")
    assert not auth.is_authenticated


async def test_login_with_unknown_strategy() -> None:
    """An unknown strategy name lists the valid ones."""
    with pytest.raises(ValueError, match="mobile\\+cffi"):
        await GarminAuth().login_with_strategy("carrier-pigeon", "e", "p")
