"""Garmin Connect authentication using native DI Bearer tokens.

Flow:
1. Portal web flow with curl_cffi (multiple TLS fingerprints — safari first).
2. Exchange CAS service ticket for native DI Bearer token via diauth.garmin.com.
3. API requests use Bearer token directly against connectapi.garmin.com,
   bypassing Cloudflare TLS inspection entirely.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests

try:
    from ua_generator import generate as _generate_ua

    HAS_UA_GEN = True
except ImportError:
    HAS_UA_GEN = False

from .exceptions import GarminAPIError, GarminAuthError, GarminMFARequired
from .models import AuthResult

_LOGGER = logging.getLogger(__name__)

# Auth constants (matching Android GCM app)
PORTAL_SSO_CLIENT_ID = "GarminConnect"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

NATIVE_API_USER_AGENT = "GCM-Android-5.23"
NATIVE_X_GARMIN_USER_AGENT = (
    "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
    "Android/33; Dalvik/2.1.0"
)

DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
DI_GRANT_TYPE = (
    "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
)
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
)


def _build_basic_auth(client_id: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:".encode()).decode()


def _native_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": NATIVE_API_USER_AGENT,
        "X-Garmin-User-Agent": NATIVE_X_GARMIN_USER_AGENT,
        "X-Garmin-Paired-App-Version": "10861",
        "X-Garmin-Client-Platform": "Android",
        "X-App-Ver": "10861",
        "X-Lang": "en",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        headers.update(extra)
    return headers


def _random_browser_headers() -> dict[str, str]:
    """Generate random browser User-Agent headers; falls back to static Chrome UA."""
    if HAS_UA_GEN:
        ua = _generate_ua()
        return dict(ua.headers.get())
    return {"User-Agent": DESKTOP_USER_AGENT}


def _http_post(url: str, **kwargs: Any) -> Any:
    """POST using curl_cffi TLS impersonation."""
    return cffi_requests.post(url, impersonate="chrome", **kwargs)


class GarminAuth:
    """Authentication engine using native DI Bearer tokens."""

    def __init__(self, is_cn: bool = False) -> None:
        self._is_cn = is_cn
        domain = "garmin.cn" if is_cn else "garmin.com"
        self._sso = f"https://sso.{domain}"
        self._connect = f"https://connect.{domain}"
        self._connectapi = f"https://connectapi.{domain}"
        self._portal_service_url = f"https://connect.{domain}/app"

        # Native DI Bearer tokens
        self.di_token: str | None = None
        self.di_refresh_token: str | None = None
        self.di_client_id: str | None = None

        self._tokenstore_path: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.di_token)

    def get_api_headers(self) -> dict[str, str]:
        """Headers for API requests using DI Bearer token."""
        if not self.is_authenticated:
            raise GarminAuthError("Not authenticated")

        return _native_headers(
            {
                "Authorization": f"Bearer {self.di_token}",
                "Accept": "application/json",
            }
        )

    def get_api_base_url(self) -> str:
        """Base URL for API requests."""
        return self._connectapi

    def _token_expires_soon(self) -> bool:
        """Check if the active token will expire within 15 minutes."""
        import time as _time

        token = self.di_token
        if not token:
            return False
        try:
            parts = str(token).split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(
                    base64.urlsafe_b64decode(payload_b64.encode()).decode()
                )
                exp = payload.get("exp")
                if exp and _time.time() > (int(exp) - 900):
                    return True
        except Exception:
            _LOGGER.debug("Failed to check token expiry")
        return False

    # -- LOGIN FLOW --

    async def login(self, email: str, password: str) -> AuthResult:
        """Login via portal web flow with curl_cffi TLS impersonation."""
        impersonations = ["safari", "safari_ios", "chrome120", "edge101", "chrome"]
        last_err: Exception | None = None
        for imp in impersonations:
            try:
                _LOGGER.debug("Trying login with impersonation=%s", imp)
                sess: Any = cffi_requests.Session(impersonate=imp)  # type: ignore[arg-type]
                return self._portal_web_login(sess, email, password)
            except (GarminAuthError, GarminMFARequired):
                raise
            except Exception as e:
                _LOGGER.debug("Login cffi(%s) failed: %s", imp, e)
                last_err = e
                continue
        raise last_err or GarminAPIError("All cffi impersonations failed")

    # -- PORTAL WEB LOGIN (desktop browser flow) --

    def _portal_web_login(self, sess: Any, email: str, password: str) -> AuthResult:
        """Login via /portal/api/login — the same endpoint Garmin Connect React uses."""
        signin_url = f"{self._sso}/portal/sso/en-US/sign-in"
        browser_hdrs = _random_browser_headers()

        sess.get(
            signin_url,
            params={
                "clientId": PORTAL_SSO_CLIENT_ID,
                "service": self._portal_service_url,
            },
            headers={
                **browser_hdrs,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
        )

        login_params = {
            "clientId": PORTAL_SSO_CLIENT_ID,
            "locale": "en-US",
            "service": self._portal_service_url,
        }
        post_headers = {
            **browser_hdrs,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": self._sso,
            "Referer": (
                f"{signin_url}?clientId={PORTAL_SSO_CLIENT_ID}"
                f"&service={self._portal_service_url}"
            ),
        }

        r = sess.post(
            f"{self._sso}/portal/api/login",
            params=login_params,
            headers=post_headers,
            json={
                "username": email,
                "password": password,
                "rememberMe": True,
                "captchaToken": "",
            },
            timeout=30,
        )

        if r.status_code == 429:
            raise GarminAPIError(
                "Portal login returned 429 — Cloudflare blocking this request."
            )

        try:
            res = r.json()
        except Exception as err:
            raise GarminAPIError(
                f"Portal login failed (non-JSON): HTTP {r.status_code}"
            ) from err

        resp_type = res.get("responseStatus", {}).get("type")

        if resp_type == "MFA_REQUIRED":
            self._mfa_method = res.get("customerMfaInfo", {}).get(
                "mfaLastMethodUsed", "email"
            )
            self._mfa_session = sess
            self._mfa_login_params = login_params
            self._mfa_post_headers = post_headers
            raise GarminMFARequired("mfa_required")

        if resp_type == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._establish_session(ticket, sess=sess)
            return AuthResult(success=True)

        if resp_type == "INVALID_USERNAME_PASSWORD":
            raise GarminAuthError("401 Unauthorized (Invalid Username or Password)")

        raise GarminAPIError(f"Portal web login failed: {res}")

    # -- MFA COMPLETION --

    async def complete_mfa(self, mfa_code: str) -> AuthResult:
        """Complete MFA verification."""
        if not hasattr(self, "_mfa_session"):
            raise GarminAuthError("No pending MFA session")
        self._complete_mfa(mfa_code)
        return AuthResult(success=True)

    def _complete_mfa(self, mfa_code: str) -> None:
        """Complete MFA via portal web flow — tries both portal and mobile endpoints."""
        sess = self._mfa_session
        mfa_json: dict[str, Any] = {
            "mfaMethod": getattr(self, "_mfa_method", "email"),
            "mfaVerificationCode": mfa_code,
            "rememberMyBrowser": True,
            "reconsentList": [],
            "mfaSetup": False,
        }

        mfa_endpoints = [
            (
                f"{self._sso}/portal/api/mfa/verifyCode",
                self._mfa_login_params,
                self._mfa_post_headers,
                self._portal_service_url,
            ),
        ]

        failures: list[str] = []
        for mfa_url, params, headers, svc_url in mfa_endpoints:
            try:
                r = sess.post(
                    mfa_url, params=params, headers=headers, json=mfa_json, timeout=30
                )
            except Exception as e:
                failures.append(f"{mfa_url}: connection error {e}")
                continue

            if r.status_code == 429:
                failures.append(f"{mfa_url}: HTTP 429")
                continue

            try:
                res = r.json()
            except Exception:
                failures.append(f"{mfa_url}: HTTP {r.status_code} non-JSON")
                continue

            if res.get("error", {}).get("status-code") == "429":
                failures.append(f"{mfa_url}: 429 in JSON body")
                continue

            if res.get("responseStatus", {}).get("type") == "SUCCESSFUL":
                ticket = res["serviceTicketId"]
                self._establish_session(ticket, sess=sess, service_url=svc_url)
                return

            failures.append(f"{mfa_url}: {res}")

        raise GarminAuthError(f"MFA Verification failed: {'; '.join(failures)}")

    # -- SESSION ESTABLISHMENT --

    def _establish_session(
        self, ticket: str, sess: Any = None, service_url: str | None = None
    ) -> None:
        """Exchange a CAS service ticket for a DI Bearer token."""
        self._exchange_service_ticket(ticket, service_url=service_url)

    def _exchange_service_ticket(
        self, ticket: str, service_url: str | None = None
    ) -> None:
        """Exchange a CAS service ticket for a native DI Bearer token.

        POST to diauth.garmin.com to get a DI OAuth2 token.
        """
        svc_url = service_url or self._portal_service_url

        di_token = None
        di_refresh = None
        di_client_id = None

        for client_id in DI_CLIENT_IDS:
            r = _http_post(
                DI_TOKEN_URL,
                headers=_native_headers(
                    {
                        "Authorization": _build_basic_auth(client_id),
                        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cache-Control": "no-cache",
                    }
                ),
                data={
                    "client_id": client_id,
                    "service_ticket": ticket,
                    "grant_type": DI_GRANT_TYPE,
                    "service_url": svc_url,
                },
                timeout=30,
            )
            if r.status_code == 429:
                raise GarminAuthError("DI token exchange rate limited")
            if not r.ok:
                _LOGGER.debug(
                    "DI exchange failed for %s: %s %s",
                    client_id,
                    r.status_code,
                    r.text[:200],
                )
                continue
            try:
                data = r.json()
                di_token = data["access_token"]
                di_refresh = data.get("refresh_token")
                di_client_id = self._extract_client_id_from_jwt(di_token) or client_id
                break
            except Exception as e:
                _LOGGER.debug("DI token parse failed for %s: %s", client_id, e)
                continue

        if not di_token:
            raise GarminAuthError("DI token exchange failed for all client IDs")

        self.di_token = di_token
        self.di_refresh_token = di_refresh
        self.di_client_id = di_client_id

    def _extract_client_id_from_jwt(self, token: str) -> str | None:
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
            value = payload.get("client_id")
            return str(value) if value else None
        except Exception:
            return None

    # -- TOKEN REFRESH --

    def _refresh_di_token(self) -> None:
        """Refresh the DI Bearer token using the stored refresh token."""
        if not self.di_refresh_token or not self.di_client_id:
            raise GarminAuthError("No DI refresh token available")
        r = _http_post(
            DI_TOKEN_URL,
            headers=_native_headers(
                {
                    "Authorization": _build_basic_auth(self.di_client_id),
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-cache",
                }
            ),
            data={
                "grant_type": "refresh_token",
                "client_id": self.di_client_id,
                "refresh_token": self.di_refresh_token,
            },
            timeout=30,
        )
        if not r.ok:
            raise GarminAuthError(
                f"DI token refresh failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        self.di_token = data["access_token"]
        self.di_refresh_token = data.get("refresh_token", self.di_refresh_token)
        self.di_client_id = (
            self._extract_client_id_from_jwt(self.di_token) or self.di_client_id
        )

    async def refresh_session(self) -> bool:
        """Refresh DI Bearer token using the stored refresh token."""
        if not self.is_authenticated:
            return False

        try:
            self._refresh_di_token()
            if self._tokenstore_path:
                with contextlib.suppress(Exception):
                    self.save_session(self._tokenstore_path)
            return True
        except Exception as err:
            _LOGGER.debug("DI token refresh failed: %s", err)
        return False

    # -- SESSION PERSISTENCE --

    def save_session(self, path: str | Path) -> None:
        """Save all tokens to disk."""
        if not self.is_authenticated:
            return

        data: dict[str, Any] = {
            k: v
            for k, v in {
                "di_token": self.di_token,
                "di_refresh_token": self.di_refresh_token,
                "di_client_id": self.di_client_id,
            }.items()
            if v is not None
        }

        p = Path(path).expanduser()
        if p.is_dir() or not str(p).endswith(".json"):
            p = p / "garmin_tokens.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))

    def load_session(self, path: str | Path) -> bool:
        """Load tokens from disk."""
        p = Path(path).expanduser()
        if p.is_dir() or not str(p).endswith(".json"):
            p = p / "garmin_tokens.json"
        if not p.exists():
            return False

        try:
            data = json.loads(p.read_text())
            self._tokenstore_path = str(path)
            self.di_token = data.get("di_token")
            self.di_refresh_token = data.get("di_refresh_token")
            self.di_client_id = data.get("di_client_id")

            if not self.is_authenticated:
                return False

            # Proactively refresh if token is expiring soon
            if self.di_refresh_token and self._token_expires_soon():
                _LOGGER.debug("Token expiring soon, refreshing proactively")
                try:
                    self._refresh_di_token()
                except Exception as e:
                    _LOGGER.debug("Proactive refresh failed: %s", e)

            return True
        except Exception:
            return False
