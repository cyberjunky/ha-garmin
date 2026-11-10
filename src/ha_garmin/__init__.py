"""Python client for Garmin Connect API."""

from .auth import GarminAuth
from .client import GarminClient
from .exceptions import (
    GarminAuthError,
    GarminConnectError,
    GarminConnectionError,
    GarminMFARequired,
    GarminRateLimitError,
    GarminTLSError,
)

__all__ = [
    "GarminAuth",
    "GarminAuthError",
    "GarminClient",
    "GarminConnectError",
    "GarminConnectionError",
    "GarminMFARequired",
    "GarminRateLimitError",
    "GarminTLSError",
]

__version__ = "0.1.0"
