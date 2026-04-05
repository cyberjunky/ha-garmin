"""Python client for Garmin Connect API."""

from .auth import GarminAuth
from .client import GarminClient
from .exceptions import (
    GarminAuthError,
    GarminConnectError,
    GarminMFARequired,
)

__all__ = [
    "GarminAuth",
    "GarminAuthError",
    "GarminClient",
    "GarminConnectError",
    "GarminMFARequired",
]

__version__ = "0.1.0"
