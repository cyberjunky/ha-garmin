"""Garmin Connect authentication using native DI Bearer tokens.

Primary flow (iOS mobile app):
1. POST sso.garmin.com/mobile/api/login  (iOS Safari UA, GCM_IOS_DARK client)
2. POST diauth.garmin.com/di-oauth2-service/oauth/token  (service_ticket grant) → DI Bearer token

Fallback flow (portal web):
1. POST sso.garmin.com/portal/api/login  (desktop browser UA)
2. POST diauth.garmin.com/di-oauth2-service/oauth/token  (service_ticket grant) → DI Bearer token
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
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

# -- iOS mobile app constants --
IOS_SSO_CLIENT_ID = "GCM_IOS_DARK"
IOS_SERVICE_URL = "https://mobile.integration.garmin.com/gcm/ios"
IOS_LOGIN_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


# -- Portal (fallback) constants --
PORTAL_SSO_CLIENT_ID = "GarminConnect"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
DI_GRANT_TYPE = (
    "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
)
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    "GARMIN_CONNECT_MOBILE_IOS_DI",
)

# -- API request headers (Android native, used for actual data calls) --
NATIVE_API_USER_AGENT = "GCM-Android-5.23"
NATIVE_X_GARMIN_USER_AGENT = (
    "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
    "Android/33; Dalvik/2.1.0"
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
    """Generate random browser UA headers; falls back to static Chrome UA."""
    if HAS_UA_GEN:
        ua = _generate_ua()
        return dict(ua.headers.get())
    return {"User-Agent": DESKTOP_USER_AGENT}


def _http_post(url: str, **kwargs: Any) -> Any:
    """POST using curl_cffi TLS impersonation."""
    return cffi_requests.post(url, impersonate="chrome", **kwargs)


class GarminAuth:
    """Authentication engine using native DI Bearer tokens."""

    _CSRF_RE = re.compile(r'name="_csrf"\s+value="(.+?)"')
    _TITLE_RE = re.compile(r"<title>(.+?)</title>")

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

    def login(self, email: str, password: str) -> AuthResult:
        """Login via iOS mobile flow (primary) or portal web flow (fallback)."""
        import requests as stdlib_requests

        try:
            _LOGGER.debug("Trying iOS mobile login flow")
            sess: Any = stdlib_requests.Session()
            return self._mobile_login(sess, email, password)
        except GarminAPIError as e:
            if "429" in str(e):
                _LOGGER.warning(
                    "Mobile login returned 429. Attempting SSO widget fallback..."
                )
                try:
                    sess = cffi_requests.Session(impersonate="chrome")
                    return self._widget_web_login(sess, email, password)
                except (GarminAuthError, GarminMFARequired):
                    raise
                except Exception as we:
                    _LOGGER.warning("Widget login failed: %s", we)
                    raise
            _LOGGER.warning("iOS mobile login failed: %s — trying portal fallback", e)
        except (GarminAuthError, GarminMFARequired):
            raise
        except Exception as e:
            _LOGGER.warning("iOS mobile login failed: %s — trying portal fallback", e)

        _LOGGER.debug("Trying portal web login flow")
        for imp in ("safari", "chrome120", "edge101", "chrome"):
            try:
                sess = cffi_requests.Session(impersonate=imp)  # type: ignore[arg-type]
                return self._portal_web_login(sess, email, password)
            except (GarminAuthError, GarminMFARequired):
                raise
            except Exception as e:
                _LOGGER.warning("Portal login cffi(%s) failed: %s", imp, e)
                continue

        raise GarminAPIError("All login strategies failed (iOS mobile + portal)")

    # -- iOS MOBILE LOGIN --

    def _mobile_login(self, sess: Any, email: str, password: str) -> AuthResult:
        """Login via sso.garmin.com/mobile/api/login (iOS app flow)."""
        login_url = f"{self._sso}/mobile/api/login"
        login_params = {
            "clientId": IOS_SSO_CLIENT_ID,
            "locale": "en-US",
            "service": IOS_SERVICE_URL,
        }
        login_headers = {
            "User-Agent": IOS_LOGIN_UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self._sso,
        }

        r = sess.post(
            login_url,
            params=login_params,
            headers=login_headers,
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
                "Mobile login returned 429 — IP rate limited by Garmin"
            )

        try:
            res = r.json()
        except Exception as err:
            raise GarminAPIError(
                f"Mobile login failed (non-JSON): HTTP {r.status_code}"
            ) from err

        resp_type = res.get("responseStatus", {}).get("type")

        if resp_type == "MFA_REQUIRED":
            self._mfa_method = res.get("customerMfaInfo", {}).get(
                "mfaLastMethodUsed", "email"
            )
            self._mfa_session = sess
            self._mfa_login_params = login_params
            self._mfa_post_headers = login_headers
            self._mfa_service_url = IOS_SERVICE_URL
            self._mfa_flow = "ios"
            raise GarminMFARequired("mfa_required")

        if resp_type == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._exchange_service_ticket(ticket, service_url=IOS_SERVICE_URL)
            return AuthResult(success=True)

        if resp_type == "INVALID_USERNAME_PASSWORD":
            raise GarminAuthError("401 Unauthorized (Invalid Username or Password)")

        raise GarminAPIError(f"Mobile login failed: {res}")

    # -- WIDGET WEB LOGIN (fallback) --

    def _widget_web_login(self, sess: Any, email: str, password: str) -> AuthResult:
        """Login via the SSO embed HTML widget to bypass clientId rate limits."""
        sso_base = f"{self._sso}/sso"
        sso_embed = f"{sso_base}/embed"
        embed_params = {
            "id": "gauth-widget",
            "embedWidget": "true",
            "gauthHost": sso_base,
        }
        signin_params = {
            **embed_params,
            "gauthHost": sso_embed,
            "service": sso_embed,
            "source": sso_embed,
            "redirectAfterAccountLoginUrl": sso_embed,
            "redirectAfterAccountCreationUrl": sso_embed,
        }

        # Step 1: GET embed page
        r = sess.get(sso_embed, params=embed_params)
        if not r.ok:
            raise GarminAPIError(f"Widget embed returned {r.status_code}")

        # Step 2: GET signin page for CSRF
        r = sess.get(
            f"{sso_base}/signin", params=signin_params, headers={"Referer": sso_embed}
        )
        csrf_match = self._CSRF_RE.search(r.text)
        if not csrf_match:
            raise GarminAPIError("Widget login: missing CSRF token")

        # Step 3: POST credentials
        r = sess.post(
            f"{sso_base}/signin",
            params=signin_params,
            headers={"Referer": r.url},
            data={
                "username": email,
                "password": password,
                "embed": "true",
                "_csrf": csrf_match.group(1),
            },
            timeout=30,
        )

        title_match = self._TITLE_RE.search(r.text)
        title = title_match.group(1) if title_match else ""

        if "MFA" in title:
            # Requires MFA
            self._mfa_session = sess
            self._mfa_login_params = signin_params
            self._mfa_post_headers = {"Referer": r.url}
            self._mfa_flow = "widget"
            self._widget_last_resp = r
            raise GarminMFARequired("mfa_required")

        if title != "Success":
            raise GarminAuthError(f"Widget authentication failed: {title}")

        # Step 4: Extract service ticket
        ticket_match = re.search(r'embed\?ticket=([^"]+)"', r.text)
        if not ticket_match:
            raise GarminAPIError("Widget login: missing service ticket")

        self._exchange_service_ticket(ticket_match.group(1), service_url=sso_embed)
        return AuthResult(success=True)

    def _complete_mfa_widget(self, mfa_code: str) -> None:
        """Complete MFA for widget flow."""
        sess = getattr(self, "_mfa_session", None)
        r = getattr(self, "_widget_last_resp", None)
        if not sess or not r:
            raise GarminAuthError("Missing widget MFA context")

        csrf_match = self._CSRF_RE.search(r.text)
        if not csrf_match:
            raise GarminAuthError("Widget MFA: missing CSRF token")

        r = sess.post(
            f"{self._sso}/sso/verifyMFA/loginEnterMfaCode",
            params=getattr(self, "_mfa_login_params", {}),
            headers=getattr(self, "_mfa_post_headers", {}),
            data={
                "mfa-code": mfa_code,
                "embed": "true",
                "_csrf": csrf_match.group(1),
                "fromPage": "setupEnterMfaCode",
            },
            timeout=30,
        )

        title_match = self._TITLE_RE.search(r.text)
        title = title_match.group(1) if title_match else ""

        if title != "Success":
            raise GarminAuthError(f"Widget MFA failed: {title}")

        ticket_match = re.search(r'embed\?ticket=([^"]+)"', r.text)
        if not ticket_match:
            raise GarminAuthError("Widget MFA: missing service ticket")

        self._exchange_service_ticket(
            ticket_match.group(1), service_url=f"{self._sso}/sso/embed"
        )

    # -- PORTAL WEB LOGIN (fallback) --

    def _portal_web_login(self, sess: Any, email: str, password: str) -> AuthResult:
        """Login via /portal/api/login — desktop browser flow."""
        signin_url = f"{self._sso}/portal/sso/en-US/sign-in"
        browser_hdrs = _random_browser_headers()

        # Step 1: GET the signin page to grab initial cookies
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

        import random
        from time import sleep

        sleep(random.uniform(30, 45))  # <-- It goes right here!

        # Step 2: Build the POST parameters and submit the login
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
            self._mfa_service_url = self._portal_service_url
            self._mfa_flow = "portal"
            raise GarminMFARequired("mfa_required")

        if resp_type == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._exchange_service_ticket(ticket)
            return AuthResult(success=True)

        if resp_type == "INVALID_USERNAME_PASSWORD":
            raise GarminAuthError("401 Unauthorized (Invalid Username or Password)")

        raise GarminAPIError(f"Portal web login failed: {res}")

    # -- MFA COMPLETION --

    def complete_mfa(self, mfa_code: str) -> AuthResult:
        """Complete MFA verification."""
        if not hasattr(self, "_mfa_session"):
            raise GarminAuthError("No pending MFA session")
        self._complete_mfa(mfa_code)
        return AuthResult(success=True)

    def _complete_mfa(self, mfa_code: str) -> None:
        """Complete MFA — uses the endpoint matching the login flow that triggered it."""
        flow = getattr(self, "_mfa_flow", "portal")
        if flow == "widget":
            self._complete_mfa_widget(mfa_code)
            return

        sess = self._mfa_session

        mfa_json: dict[str, Any] = {
            "mfaMethod": getattr(self, "_mfa_method", "email"),
            "mfaVerificationCode": mfa_code,
            "rememberMyBrowser": True,
            "reconsentList": [],
            "mfaSetup": False,
        }

        if flow == "ios":
            mfa_url = f"{self._sso}/mobile/api/mfa/verifyCode"
        else:
            mfa_url = f"{self._sso}/portal/api/mfa/verifyCode"

        try:
            r = sess.post(
                mfa_url,
                params=self._mfa_login_params,
                headers=self._mfa_post_headers,
                json=mfa_json,
                timeout=30,
            )
        except Exception as e:
            raise GarminAuthError(f"MFA request failed: {e}") from e

        if r.status_code == 429:
            raise GarminAuthError("MFA verification rate limited")

        try:
            res = r.json()
        except Exception as err:
            raise GarminAuthError(
                f"MFA non-JSON response: HTTP {r.status_code}"
            ) from err

        if res.get("error", {}).get("status-code") == "429":
            raise GarminAuthError("MFA verification rate limited")

        if res.get("responseStatus", {}).get("type") == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            svc_url = IOS_SERVICE_URL if flow == "ios" else self._mfa_service_url
            self._exchange_service_ticket(ticket, service_url=svc_url)
            return

        raise GarminAuthError(f"MFA verification failed: {res}")

    # -- PORTAL TICKET EXCHANGE (fallback) --

    def _exchange_service_ticket(
        self, ticket: str, service_url: str | None = None
    ) -> None:
        """Exchange a CAS ticket for a DI Bearer token via diauth.garmin.com."""
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
        import asyncio

        if not self.is_authenticated:
            return False

        try:
            await asyncio.to_thread(self._refresh_di_token)
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
                "token": self.di_token,
                "refresh_token": self.di_refresh_token,
                "client_id": self.di_client_id,
            }.items()
            if v is not None
        }

        p = Path(path).expanduser()
        if p.is_dir() or not str(p).endswith(".json"):
            p = p / ".garmin_tokens.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))

    def load_session(self, path: str | Path) -> bool:
        """Load tokens from disk."""
        p = Path(path).expanduser()
        if p.is_dir() or not str(p).endswith(".json"):
            p = p / ".garmin_tokens.json"
        if not p.exists():
            return False

        try:
            data = json.loads(p.read_text())
            self._tokenstore_path = str(path)
            self.di_token = data.get("token")
            self.di_refresh_token = data.get("refresh_token")
            self.di_client_id = data.get("client_id")

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
