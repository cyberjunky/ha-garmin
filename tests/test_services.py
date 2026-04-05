"""Tests for service methods."""

import pytest

from ha_garmin import GarminAuth, GarminClient


def _make_auth() -> GarminAuth:
    auth = GarminAuth()
    auth.di_token = "fake_di_token"
    return auth


class TestServiceMethods:
    """Tests for service-related client methods."""

    async def test_upload_activity_file_not_found(self):
        """Test upload_activity with non-existent file."""
        auth = _make_auth()
        client = GarminClient(auth)

        with pytest.raises(FileNotFoundError):
            await client.upload_activity("/nonexistent/file.fit")

    async def test_upload_activity_invalid_format(self, tmp_path):
        """Test upload_activity with unsupported file format."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        auth = _make_auth()
        client = GarminClient(auth)

        with pytest.raises(ValueError, match="Invalid file format"):
            await client.upload_activity(str(test_file))
