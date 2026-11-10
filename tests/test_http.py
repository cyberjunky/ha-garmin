"""Tests for the async HTTP transports."""

import asyncio
import json
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ha_garmin.exceptions import GarminAPIError, GarminTLSError
from ha_garmin.http import (
    ApiTransport,
    HttpResponse,
    LoginSession,
    cert_verification_failure,
    impersonated_request,
)

URL = "https://connectapi.garmin.com/test"


def test_http_response_properties() -> None:
    """ok/text/json behave like requests' Response."""
    resp = HttpResponse(status_code=200, content=b'{"a": 1}')
    assert resp.ok
    assert resp.text == '{"a": 1}'
    assert resp.json() == {"a": 1}
    assert HttpResponse(status_code=399).ok
    assert not HttpResponse(status_code=400).ok
    assert (
        HttpResponse(
            status_code=200, content="é".encode("latin-1"), encoding="latin-1"
        ).text
        == "é"
    )


@pytest.fixture
async def server():
    """A local aiohttp server that records requests and replies per path."""
    received: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.read()
        received.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": body,
                "content_type": request.content_type,
            }
        )
        if request.path == "/json":
            return web.json_response({"ok": True}, headers={"X-Test": "1"})
        if request.path == "/created":
            return web.json_response({"id": 1}, status=201)
        if request.path == "/slow":
            await asyncio.sleep(5)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    test_server = TestServer(app)
    await test_server.start_server()
    test_server.received = received
    yield test_server
    await test_server.close()


async def test_api_transport_request_reads_the_response(server) -> None:
    """A request returns the full body, status, url and headers."""
    transport = ApiTransport()
    url = str(server.make_url("/json"))
    resp = await transport.request("GET", url, headers={"Authorization": "Bearer t"})
    await transport.close()

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.headers["X-Test"] == "1"
    assert resp.url == url
    assert server.received[0]["headers"]["Authorization"] == "Bearer t"


async def test_api_transport_encodes_params_like_requests(server) -> None:
    """None params are dropped and bools sent as text, as requests did."""
    transport = ApiTransport()
    await transport.request(
        "GET",
        str(server.make_url("/q")),
        params={"date": "2026-01-01", "includeAll": True, "n": 3, "x": None},
    )
    await transport.close()
    assert server.received[0]["query"] == {
        "date": "2026-01-01",
        "includeAll": "True",
        "n": "3",
    }


async def test_api_transport_sends_json_and_files(server) -> None:
    """JSON bodies and multipart uploads reach the server."""
    transport = ApiTransport()
    resp = await transport.request(
        "POST", str(server.make_url("/created")), json={"a": 1}
    )
    upload = await transport.request(
        "POST",
        str(server.make_url("/upload")),
        files={"file": ("ride.fit", b"FITDATA", "application/octet-stream")},
    )
    await transport.close()

    assert resp.status_code == 201
    assert upload.status_code == 204
    first, second = server.received
    assert json.loads(first["body"]) == {"a": 1}
    assert second["content_type"] == "multipart/form-data"
    assert b'filename="ride.fit"' in second["body"]
    assert b"FITDATA" in second["body"]


async def test_api_transport_maps_connection_errors() -> None:
    """A refused connection raises GarminAPIError, not an aiohttp exception."""
    transport = ApiTransport()
    with pytest.raises(GarminAPIError, match="Request failed"):
        # Port 9 (discard) on localhost is closed in test environments.
        await transport.request("GET", "http://127.0.0.1:9/x", timeout=5)
    await transport.close()


async def test_api_transport_maps_timeouts(server) -> None:
    """A request exceeding its timeout raises GarminAPIError."""
    transport = ApiTransport()
    with pytest.raises(GarminAPIError):
        await transport.request("GET", str(server.make_url("/slow")), timeout=0.1)
    await transport.close()


async def test_api_transport_maps_certificate_errors() -> None:
    """An intercepted TLS connection raises GarminTLSError naming the host."""
    cert_error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    cert_error.verify_message = "self-signed certificate"
    error = aiohttp.ClientConnectionError("TLS handshake failed")
    error.__cause__ = cert_error
    transport = ApiTransport()
    with (
        patch.object(aiohttp.ClientSession, "request", side_effect=error),
        pytest.raises(GarminTLSError, match=r"connectapi\.garmin\.com"),
    ):
        await transport.request("GET", URL)
    await transport.close()


async def test_api_transport_never_closes_an_injected_session(server) -> None:
    """Home Assistant owns the session it passes in."""
    async with aiohttp.ClientSession() as session:
        transport = ApiTransport(session)
        await transport.request("GET", str(server.make_url("/json")))
        await transport.close()
        assert not session.closed


async def test_api_transport_closes_its_own_session(server) -> None:
    """A session the transport created is closed by close()."""
    transport = ApiTransport()
    await transport.request("GET", str(server.make_url("/json")))
    session = transport._session
    assert session is not None
    await transport.close()
    assert session.closed


def test_cert_verification_failure_ignores_other_errors() -> None:
    """Only certificate problems count as TLS interception."""
    assert cert_verification_failure(ConnectionError("refused")) is None
    assert (
        cert_verification_failure(OSError("[SSL: CERTIFICATE_VERIFY_FAILED]"))
        is not None
    )


async def test_login_session_without_impersonation_uses_aiohttp(server) -> None:
    """Plain login strategies use their own aiohttp session."""
    sess = LoginSession()
    resp = await sess.get(str(server.make_url("/json")))
    assert resp.json() == {"ok": True}
    inner = sess._aiohttp
    assert inner is not None
    await sess.close()
    assert inner.closed


def _fake_cffi_session(
    response: MagicMock | None = None, error: Exception | None = None
) -> MagicMock:
    session = MagicMock()
    session.request = AsyncMock(return_value=response, side_effect=error)
    session.close = AsyncMock()
    return session


async def test_login_session_with_impersonation_uses_curl_cffi() -> None:
    """Impersonated strategies go through curl_cffi's AsyncSession."""
    cffi_response = MagicMock(
        status_code=200,
        content=b"ok",
        url="https://sso.garmin.com/a",
        headers={},
        encoding="utf-8",
    )
    fake = _fake_cffi_session(cffi_response)
    with patch("ha_garmin.http.AsyncSession", return_value=fake) as session_cls:
        sess = LoginSession(impersonate="safari")
        resp = await sess.post(
            "https://sso.garmin.com/a", data={"a": "b"}, params={"x": None}
        )
        await sess.close()

    session_cls.assert_called_once_with(impersonate="safari")
    assert resp.text == "ok"
    _, kwargs = fake.request.call_args
    assert kwargs["data"] == {"a": "b"}
    assert kwargs["params"] == {}
    fake.close.assert_awaited_once()


async def test_login_session_maps_curl_errors() -> None:
    """curl_cffi transport errors (OSError subclasses) become library errors."""
    fake = _fake_cffi_session(error=OSError("Could not resolve host"))
    with (
        patch("ha_garmin.http.AsyncSession", return_value=fake),
        pytest.raises(GarminAPIError, match="resolve host"),
    ):
        await LoginSession(impersonate="chrome").get("https://sso.garmin.com/a")


async def test_impersonated_request_closes_its_session() -> None:
    """One-off impersonated calls never leak a curl session."""
    cffi_response = MagicMock(
        status_code=204, content=b"", url="", headers={}, encoding=None
    )
    fake = _fake_cffi_session(cffi_response)
    with patch("ha_garmin.http.AsyncSession", return_value=fake):
        resp = await impersonated_request("POST", "https://diauth.garmin.com/t")
    assert resp.status_code == 204
    fake.close.assert_awaited_once()

    failing = _fake_cffi_session(error=OSError("reset"))
    with (
        patch("ha_garmin.http.AsyncSession", return_value=failing),
        pytest.raises(GarminAPIError),
    ):
        await impersonated_request("GET", "https://diauth.garmin.com/t")
    failing.close.assert_awaited_once()
