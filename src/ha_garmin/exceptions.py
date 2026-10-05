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


class GarminRateLimitError(GarminConnectError):
    """HTTP 429 rate limit error.

    Separate from GarminAuthError so callers can distinguish rate limits
    (which are transient and worth retrying with a different strategy)
    from genuine credential failures.
    """


class GarminAPIError(GarminConnectError):
    """API request error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize API error."""
        super().__init__(message)
        self.status_code = status_code


class GarminTLSError(GarminConnectError):
    """The TLS certificate presented for a Garmin host could not be verified.

    Garmin serves publicly trusted certificates, so this means the connection
    is being intercepted or redirected (proxy, HTTPS inspection, DNS filter).
    Deliberately not a GarminAPIError: every other endpoint on the host fails
    the same way, so it has to abort a fetch instead of being skipped per call.
    """
