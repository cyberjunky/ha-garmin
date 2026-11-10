"""Async HTTP transports for ha_garmin.

Two transports share one response type:

* ``ApiTransport`` runs the Garmin Connect data API on an ``aiohttp``
  session. The caller can inject its own session (Home Assistant passes one);
  otherwise the transport creates one and closes it in ``close()``.
* Login sessions talk to Garmin SSO. Garmin's web application firewall only
  lets requests through whose TLS fingerprint looks like a real browser or
  app, which ``aiohttp`` cannot produce. Those use ``curl_cffi``'s asyncio
  ``AsyncSession`` with TLS impersonation, or a short-lived ``aiohttp``
  session for the strategies that need no impersonation. They are separate
  from the injected session so SSO cookies never land in a shared cookie jar.
"""

from __future__ import annotations

import json as jsonlib
import logging
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from curl_cffi.requests import AsyncSession

from .exceptions import GarminAPIError, GarminConnectError, GarminTLSError

_LOGGER = logging.getLogger(__name__)

# (filename, content, content type) per multipart field, like requests' files=.
Files = Mapping[str, tuple[str, bytes, str]]


@dataclass(frozen=True)
class HttpResponse:
    """A fully read HTTP response, independent of the client library."""

    status_code: int
    content: bytes = b""
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    encoding: str = "utf-8"

    @property
    def ok(self) -> bool:
        """True for any status below 400, like requests."""
        return self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.content)


def cert_verification_failure(err: BaseException) -> str | None:
    """Return why certificate verification failed, or None for any other error.

    HTTP clients wrap the ssl error one or more layers deep, linked through
    ``__cause__``, ``__context__``, ``certificate_error``, ``reason`` or
    ``args`` depending on the library.
    """
    seen: set[int] = set()
    pending: list[Any] = [err]
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            # aiohttp's ClientConnectorCertificateError subclasses this but
            # has no verify_message; its certificate_error (walked below) does.
            message = getattr(current, "verify_message", None)
            if message:
                return str(message)
            if not hasattr(current, "certificate_error"):
                return "certificate verify failed"
        pending.extend(
            (
                current.__cause__,
                current.__context__,
                getattr(current, "certificate_error", None),
                getattr(current, "reason", None),
                *current.args,
            )
        )
    if "CERTIFICATE_VERIFY_FAILED" in str(err):
        return "certificate verify failed"
    return None


def transport_error(err: BaseException, url: str) -> GarminConnectError:
    """Map a transport failure (connection, DNS, timeout, TLS) to a library error."""
    _LOGGER.debug("Request to %s failed: %s", url, err)
    if (reason := cert_verification_failure(err)) is not None:
        host = urlsplit(url).hostname
        return GarminTLSError(
            f"TLS certificate for {host} could not be verified ({reason}). "
            "The connection to Garmin is being intercepted or redirected "
            "by a proxy, HTTPS inspection or DNS filter on this network; "
            f"exclude {host} from it"
        )
    return GarminAPIError(f"Request failed: {err}")


def _query(params: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Encode query params the way requests does.

    aiohttp rejects None and bool values; requests drops None and sends
    bools as "True"/"False".
    """
    if params is None:
        return None
    return {k: str(v) for k, v in params.items() if v is not None}


def _form(files: Files) -> aiohttp.FormData:
    form = aiohttp.FormData()
    for name, (filename, content, content_type) in files.items():
        form.add_field(name, content, filename=filename, content_type=content_type)
    return form


async def _aiohttp_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    data: Any = None,
    files: Files | None = None,
    timeout: float = 30,
) -> HttpResponse:
    """One aiohttp request, read completely; transport failures raise library errors."""
    body = _form(files) if files else data
    try:
        async with session.request(
            method,
            url,
            params=_query(params),
            headers=dict(headers) if headers else None,
            json=json,
            data=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            content = await resp.read()
            return HttpResponse(
                status_code=resp.status,
                content=content,
                url=str(resp.url),
                headers=dict(resp.headers),
                encoding=resp.get_encoding() if content else "utf-8",
            )
    except (aiohttp.ClientError, TimeoutError) as err:
        raise transport_error(err, url) from err


class ApiTransport:
    """Garmin Connect data API over aiohttp, on an injected or owned session."""

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        files: Files | None = None,
        timeout: float = 30,
    ) -> HttpResponse:
        return await _aiohttp_request(
            await self._get_session(),
            method,
            url,
            params=params,
            headers=headers,
            json=json,
            files=files,
            timeout=timeout,
        )

    async def close(self) -> None:
        """Close the session if this transport created it; an injected one is left open."""
        if (
            self._owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()
        self._session = None


class LoginSession:
    """A cookie-keeping session for one SSO login attempt.

    ``impersonate`` selects curl_cffi with that TLS fingerprint; None uses a
    plain aiohttp session. Either way ``get``/``post`` return an
    ``HttpResponse`` and transport failures raise library errors.
    """

    def __init__(self, impersonate: str | None = None, timeout: float = 30) -> None:
        self._impersonate = impersonate
        self._timeout = timeout
        self._cffi: AsyncSession[Any] | None = None
        self._aiohttp: aiohttp.ClientSession | None = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        timeout = self._timeout if timeout is None else timeout
        if self._impersonate is None:
            if self._aiohttp is None:
                self._aiohttp = aiohttp.ClientSession()
            return await _aiohttp_request(
                self._aiohttp,
                method,
                url,
                params=params,
                headers=headers,
                json=json,
                data=data,
                timeout=timeout,
            )
        if self._cffi is None:
            self._cffi = AsyncSession(impersonate=self._impersonate)  # type: ignore[arg-type,unused-ignore]  # stricter stubs in older curl_cffi
        try:
            r = await self._cffi.request(
                method,  # type: ignore[arg-type,unused-ignore]
                url,
                params=_query(params),
                headers=dict(headers) if headers else None,
                json=json,
                data=data,
                timeout=timeout,
            )
        except OSError as err:  # curl_cffi's RequestException is an OSError
            raise transport_error(err, url) from err
        return HttpResponse(
            status_code=r.status_code,
            content=r.content,
            url=str(r.url),
            headers=dict(r.headers),
            encoding=r.encoding or "utf-8",
        )

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("POST", url, **kwargs)

    async def close(self) -> None:
        if self._cffi is not None:
            await self._cffi.close()
            self._cffi = None
        if self._aiohttp is not None:
            await self._aiohttp.close()
            self._aiohttp = None


async def impersonated_request(
    method: str, url: str, *, impersonate: str = "chrome", **kwargs: Any
) -> HttpResponse:
    """A single curl_cffi request with TLS impersonation, in its own session."""
    session = LoginSession(impersonate=impersonate)
    try:
        return await session.request(method, url, **kwargs)
    finally:
        await session.close()
