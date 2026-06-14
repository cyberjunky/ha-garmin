"""Tests for GarminAuth."""

import os
import stat
import sys
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
            auth.login("e@x.com", "pw")
        assert auth.di_token == "good"

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
    #  Widget MFA title detection                                         #
    # ------------------------------------------------------------------ #

    def _widget_session(self, post_title: str) -> MagicMock:
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
            text=f"<html><title>{post_title}</title></html>",
        )
        sess = MagicMock()
        sess.get.side_effect = [embed_resp, signin_resp]
        sess.post.return_value = post_resp
        return sess

    def test_widget_totp_mfa_title_raises_mfa_required(self):
        """TOTP MFA title ('Enter MFA code for login') triggers GarminMFARequired."""
        auth = GarminAuth()
        sess = self._widget_session("Enter MFA code for login")
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminMFARequired),
        ):
            auth._widget_web_login("u@x.com", "pw")

    def test_widget_email_mfa_title_raises_mfa_required(self):
        """Email MFA title ('GARMIN Authentication Application') triggers GarminMFARequired."""
        auth = GarminAuth()
        sess = self._widget_session("GARMIN Authentication Application")
        with (
            patch("ha_garmin.auth.cffi_requests.Session", return_value=sess),
            patch("time.sleep"),
            pytest.raises(GarminMFARequired),
        ):
            auth._widget_web_login("u@x.com", "pw")

    def test_widget_unrelated_title_raises_api_error(self):
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
