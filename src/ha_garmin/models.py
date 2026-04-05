"""Pydantic models for Garmin Connect API responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GarminModel(BaseModel):
    """Base model that ignores unknown fields."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AuthResult(BaseModel):
    """Result of authentication attempt."""

    success: bool
    oauth1_token: dict[str, Any] | None = None
    oauth2_token: dict[str, Any] | None = None
    display_name: str | None = None
    user_id: str | None = None


class UserProfile(GarminModel):
    """User profile information.

    Used for caching user profile data, primarily to get display_name
    which is used in API URLs.

    Note: 'id' is the social profile ID, 'profile_id' is the user profile ID.
    The gear API requires the user profile ID (profile_id), not the social ID.
    """

    id: int  # Social profile ID (e.g., 376735957)
    profile_id: int = Field(alias="profileId")  # User profile ID (e.g., 82413233)
    display_name: str = Field(alias="displayName")
    profile_image_url: str | None = Field(default=None, alias="profileImageUrlMedium")
