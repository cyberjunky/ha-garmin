"""Exceptions for ha_garmin."""


class GarminConnectError(Exception):
    """Base exception for Garmin Connect errors."""


class GarminAuthError(GarminConnectError):
    """Authentication error."""


class GarminMFARequired(GarminConnectError):
    """MFA is required to complete authentication."""

    def __init__(self, mfa_ticket: str) -> None:
        """Initialize MFA required exception."""
        super().__init__("MFA verification required")
        self.mfa_ticket = mfa_ticket


class GarminAPIError(GarminConnectError):
    """API request error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize API error."""
        super().__init__(message)
        self.status_code = status_code
