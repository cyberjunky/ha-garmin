"""Tests for GarminAuth."""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ha_garmin import GarminAuth, GarminAuthError
from ha_garmin.exceptions import GarminAPIError, GarminMFARequired


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
            "ha_garmin.auth.cffi_requests.get",
            return_value=MagicMock(status_code=200),
        ):
            assert auth._verify_token() is True

    @pytest.mark.parametrize("status", [401, 403])
    async def test_verify_token_false_on_auth_rejection(self, status):
        """A 401/403 means the API tier rejected the token."""
        auth = GarminAuth()
        auth.di_token = "tok"
        with patch(
            "ha_garmin.auth.cffi_requests.get",
            return_value=MagicMock(status_code=status),
        ):
            assert auth._verify_token() is False

    async def test_verify_token_inconclusive_keeps_token(self):
        """A transient error must not reject an otherwise-working token."""
        auth = GarminAuth()
        auth.di_token = "tok"
        with patch("ha_garmin.auth.cffi_requests.get", side_effect=OSError("network")):
            assert auth._verify_token() is True

    async def test_verify_token_false_when_unauthenticated(self):
        """No token at all cannot be valid."""
        auth = GarminAuth()
        assert auth._verify_token() is False

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
            patch(
                "ha_garmin.auth.cffi_requests.get",
                return_value=MagicMock(status_code=status),
            ),
            pytest.raises(GarminAPIError) as excinfo,
        ):
            auth.login("e@x.com", "pw")
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
                "ha_garmin.auth.cffi_requests.get",
                return_value=MagicMock(status_code=403),
            ),
            pytest.raises(GarminAuthError, match=r"HTTP 403.*client or network"),
        ):
            auth.complete_mfa("123456")

    async def test_verify_token_success_clears_last_rejection(self):
        """An accepted token must not keep an earlier rejection's status."""
        auth = GarminAuth()
        auth.di_token = "tok"
        auth._last_rejection_status = 403
        with patch(
            "ha_garmin.auth.cffi_requests.get",
            return_value=MagicMock(status_code=200),
        ):
            assert auth._verify_token() is True
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
                auth, "_verify_token", side_effect=lambda: next(verify_results)
            ),
        ):
            # CN order runs widget first → its token is rejected → fall through.
            auth._is_cn = True
            auth.login("e@x.com", "pw")
        assert auth.di_token == "good"

    def test_login_success_non_mfa_does_not_set_pending(self):
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
            result = auth.login("u@x.com", "pw")

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

    def _widget_session(self, post_title: str, mfa_method: str = "") -> MagicMock:
        """Mock cffi Session that drives _widget_web_login to the POST response."""
        embed_resp = MagicMock(status_code=200, ok=True, text="<html></html>")
        signin_resp = MagicMock(
            status_code=200,
            ok=True,
            text='<input name="_csrf" value="tok">',
            url="https://sso.garmin.com/sso/signin",
        )
        post_resp = MagicMock(
            status_code=200,
            text=self._widget_mfa_page(post_title, mfa_method),
        )
        sess = MagicMock()
        sess.get.side_effect = [embed_resp, signin_resp]
        sess.post.return_value = post_resp
        return sess

    def test_widget_totp_mfa_title_raises_mfa_required(self) -> None:
        """TOTP MFA title ('Enter MFA code for login') triggers GarminMFARequired."""
        auth = GarminAuth()
        sess = self._widget_session("Enter MFA code for login", mfa_method="totp")
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminMFARequired),
        ):
            auth._widget_web_login("u@x.com", "pw")
        assert not any(
            "/sso/verifyMFA/mfaCode" in call.args[0]
            for call in sess.post.call_args_list
        )

    def test_widget_email_mfa_title_requests_code(self) -> None:
        """Email MFA title triggers GarminMFARequired and requests a code."""
        auth = GarminAuth()
        sess = self._widget_session(
            "GARMIN Authentication Application", mfa_method="email"
        )
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminMFARequired),
        ):
            auth._widget_web_login("u@x.com", "pw")
        assert any(
            "/sso/verifyMFA/mfaCode" in call.args[0]
            for call in sess.post.call_args_list
        )

    def test_widget_email_mfa_already_sent_no_request(self) -> None:
        """If Garmin already sent a code, do not request another one."""
        auth = GarminAuth()
        page = self._widget_mfa_page(
            "Enter MFA code for login",
            mfa_method="email",
            code_sent_to="u@example.com",
        )
        embed_resp = MagicMock(status_code=200, ok=True, text="<html></html>")
        signin_resp = MagicMock(
            status_code=200,
            ok=True,
            text='<input name="_csrf" value="tok">',
            url="https://sso.garmin.com/sso/signin",
        )
        post_resp = MagicMock(status_code=200, text=page)
        sess = MagicMock()
        sess.get.side_effect = [embed_resp, signin_resp]
        sess.post.return_value = post_resp
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminMFARequired),
        ):
            auth._widget_web_login("u@x.com", "pw")
        assert not any(
            "/sso/verifyMFA/mfaCode" in call.args[0]
            for call in sess.post.call_args_list
        )

    def test_widget_bare_signin_title_not_mfa(self) -> None:
        """The bare signin page title falls through to portal strategies."""
        auth = GarminAuth()
        sess = self._widget_session("GARMIN Authentication Application")
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminAPIError, match="auth application page"),
        ):
            auth._widget_web_login("u@x.com", "pw")

    def test_widget_unrelated_title_raises_api_error(self) -> None:
        """An unrecognised title raises GarminAPIError, not GarminMFARequired."""
        auth = GarminAuth()
        sess = self._widget_session("Some Random Page")
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminAPIError, match="unexpected title"),
        ):
            auth._widget_web_login("u@x.com", "pw")

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

        def tracked_refresh_di_token() -> None:
            # _refresh_with_lock acquires the lock before calling this helper.
            assert auth._token_lock._is_owned()  # type: ignore[attr-defined]
            lock_held_during_refresh.append(True)

        with patch.object(
            auth, "_refresh_di_token", side_effect=tracked_refresh_di_token
        ):
            result = await auth.refresh_session()

        assert result is True
        assert lock_held_during_refresh == [True]

    def test_login_rejects_interleaved_mfa(self):
        """login() must refuse to start when an MFA flow is already pending."""
        auth = GarminAuth()
        auth._mfa_pending = True
        with pytest.raises(GarminAuthError, match="MFA login already in progress"):
            auth.login("u@x.com", "pw")


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

    def test_logout_clears_state_and_deletes_token_file(self, tmp_path):
        auth = GarminAuth()
        auth.di_token = "tok"
        auth.di_refresh_token = "refresh"
        auth.di_client_id = "CID"
        store = tmp_path / "store"
        auth._tokenstore_path = str(store)
        store.mkdir()
        token_file = store / ".garmin_tokens.json"
        token_file.write_text("{}")

        auth.logout()

        assert auth.di_token is None
        assert auth.di_refresh_token is None
        assert auth.di_client_id is None
        assert auth._tokenstore_path is None
        assert not token_file.exists()

    def test_login_clears_stale_auth_state_on_entry(self):
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
            pytest.raises(GarminAPIError, match="exhausted"),
        ):
            auth.login("u@x.com", "pw")

        assert auth.di_token is None
        assert not auth.is_authenticated

    def test_login_failure_redacts_url_query_values(self, caplog):
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
            caplog.at_level("DEBUG", logger="ha_garmin.auth"),
            pytest.raises(GarminAPIError, match="exhausted") as exc_info,
        ):
            auth.login("u@x.com", "pw")

        assert "ST-CANARY-123" not in caplog.text
        assert "ST-CANARY-123" not in str(exc_info.value)
        assert "ticket=<redacted>" in caplog.text

    def test_failed_strategies_log_at_debug_and_summarize(self, caplog):
        """A strategy failing is expected; only the final error reports them."""
        auth = GarminAuth()
        boom = ConnectionError("down")
        with (
            patch.object(auth, "_mobile_login_cffi", side_effect=boom),
            patch.object(auth, "_mobile_login_requests", side_effect=boom),
            patch.object(auth, "_widget_web_login", side_effect=boom),
            patch.object(auth, "_portal_web_login_cffi", side_effect=boom),
            caplog.at_level("WARNING", logger="ha_garmin.auth"),
            pytest.raises(GarminAPIError, match="ConnectionError; ") as exc_info,
        ):
            auth.login("u@x.com", "pw")
        assert caplog.text == ""
        assert "mobile+cffi: ConnectionError" in str(exc_info.value)

    def test_sanitize_exception_text_redacts_query_values(self):
        from ha_garmin.auth import _sanitize_exception_text

        out = _sanitize_exception_text(Exception("url: /x?ticket=ST-1&foo=bar ok"))
        assert "ST-1" not in out
        assert "bar" not in out
        assert "ticket=<redacted>" in out
        assert "foo=<redacted>" in out


def test_di_exchange_failure_log_omits_response_body(caplog):
    """A failed DI exchange logs the status, never the token endpoint's body."""
    auth = GarminAuth()
    failed = MagicMock(ok=False, status_code=400, text="ST-CANARY-TICKET-123 detail")
    with (
        patch("ha_garmin.auth._http_post", return_value=failed),
        caplog.at_level("DEBUG", logger="ha_garmin.auth"),
        pytest.raises(GarminAuthError),
    ):
        auth._exchange_service_ticket("ST-CANARY-TICKET-123")
    assert "HTTP 400" in caplog.text
    assert "ST-CANARY-TICKET-123" not in caplog.text


# ── Refresh hardening ─────────────────────────────────────────────────────────


def _refresh_auth() -> GarminAuth:
    auth = GarminAuth()
    auth.di_token = "old_token"
    auth.di_refresh_token = "refresh"
    auth.di_client_id = "CID"
    return auth


async def test_concurrent_401s_refresh_once() -> None:
    """Requests refused with the same token share one refresh."""
    import asyncio

    auth = _refresh_auth()
    reply = MagicMock(ok=True, status_code=200)
    reply.json.return_value = {"access_token": "new_token"}
    with patch("ha_garmin.auth._http_post", return_value=reply) as post:
        results = await asyncio.gather(
            *(auth.refresh_session(rejected_token="old_token") for _ in range(3))
        )
    assert results == [True, True, True]
    assert auth.di_token == "new_token"
    post.assert_called_once()


@pytest.mark.parametrize(
    "json_result",
    [ValueError("not JSON"), {}],
    ids=["non-json", "no-access-token"],
)
async def test_refresh_without_access_token_is_transient(json_result) -> None:
    """A 200 without a token is a Garmin glitch, not a refused refresh token."""
    from ha_garmin.exceptions import GarminConnectionError

    auth = _refresh_auth()
    reply = MagicMock(ok=True, status_code=200)
    if isinstance(json_result, Exception):
        reply.json.side_effect = json_result
    else:
        reply.json.return_value = json_result
    with patch("ha_garmin.auth._http_post", return_value=reply):
        assert await auth.refresh_session() is False
    assert isinstance(auth.last_refresh_exception, GarminConnectionError)
    assert auth.di_token == "old_token"


@pytest.mark.parametrize("flow", ["mobile", "portal"])
async def test_captcha_required_counts_as_blocked(flow: str) -> None:
    """CAPTCHA_REQUIRED means this client is blocked, like a 429, not an unknown reply."""
    from ha_garmin.exceptions import GarminRateLimitError

    auth = GarminAuth()
    sess = MagicMock()
    sess.get.return_value = MagicMock(status_code=200, text="<html/>")
    sess.post.return_value = MagicMock(status_code=200)
    sess.post.return_value.json.return_value = {
        "responseStatus": {"type": "CAPTCHA_REQUIRED"}
    }
    login = auth._do_mobile_login if flow == "mobile" else auth._do_portal_web_login
    with (
        patch("ha_garmin.auth.time.sleep"),
        pytest.raises(GarminRateLimitError, match="CAPTCHA"),
    ):
        login(sess, "u@x.com", "pw")


def test_no_plain_requests_portal_strategy() -> None:
    """The portal blocks plain requests sessions, so that strategy is gone."""
    assert not hasattr(GarminAuth, "_portal_web_login_requests")
