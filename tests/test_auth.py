"""Tests for GarminAuth."""

import pytest

from ha_garmin import GarminAuth, GarminAuthError


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
