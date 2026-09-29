"""Garmin Connect authentication using native DI Bearer tokens.

Strategy chain (each strategy is tried in order; only auth errors stop the chain):
1. Mobile iOS + curl_cffi  (TLS fingerprint rotation, no delay needed)
2. Mobile iOS + requests   (plain HTTP fallback)
3. SSO embed widget + cffi (HTML form flow, bypasses clientId rate limits)
4. Portal web + curl_cffi  (TLS fingerprint rotation, 30-45s anti-WAF delay)
5. Portal web + requests   (plain HTTP last resort)
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import random
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, cast

from curl_cffi import requests as cffi_requests

try:
    from ua_generator import generate as _generate_ua

    HAS_UA_GEN = True
except ImportError:
    HAS_UA_GEN = False

from .exceptions import (
    GarminAPIError,
    GarminAuthError,
    GarminMFARequired,
    GarminRateLimitError,
)
from .models import AuthResult

_LOGGER = logging.getLogger(__name__)

# Detect ~username expansion that would point into another user's home directory.
_OTHER_USER_HOME_RE = re.compile(r"^~[^/\\]")

_QUERY_VALUE_RE = re.compile(r"([?&][\w.-]+=)[^&\s)'\"]+")


def _sanitize_exception_text(err: Exception) -> str:
    """Render an exception for logs/messages with URL query values redacted.

    HTTP clients embed the full request URL — query string included — in
    exception text, so raw exceptions must not reach logs or callers.
    """
    return _QUERY_VALUE_RE.sub(r"\1<redacted>", f"{type(err).__name__}: {err}")


def token_file_path(path: str | Path) -> Path:
    """Return the token file represented by a directory or JSON path.

    Rejects paths that expand into another user's home directory via
    ``~username`` syntax. Bare ``~`` and ``~/...`` are allowed because they
    resolve to the current user's home.

    Also rejects symlinked tokenstore paths so a pre-planted symlink cannot
    redirect load/save to an attacker-controlled location.
    """
    path_str = str(path)
    if _OTHER_USER_HOME_RE.match(path_str):
        raise ValueError(
            f"Token path must not reference another user's home directory: {path_str!r}"
        )
    token_path = Path(path_str).expanduser()
    # Reject symlinks anywhere in the tokenstore ancestry (e.g.
    # ~/.garminconnect -> /attacker/dir). O_NOFOLLOW on the final open()
    # only covers the last component; an intermediate symlinked directory
    # would still redirect load/save into an attacker-controlled tree.
    for check_path in (token_path, *token_path.parents):
        try:
            if check_path.is_symlink():
                raise ValueError(f"Token path must not be a symlink: {path_str!r}")
        except OSError as e:
            raise ValueError(
                f"Token path cannot be checked for symlinks: {path_str!r}"
            ) from e
    if token_path.is_dir() or token_path.suffix.casefold() != ".json":
        return token_path / ".garmin_tokens.json"
    return token_path


# -- iOS mobile app constants --
IOS_SSO_CLIENT_ID = "GCM_IOS_DARK"
IOS_SERVICE_URL = "https://mobile.integration.garmin.com/gcm/ios"
IOS_LOGIN_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

# -- Android mobile app constants --
ANDROID_SSO_CLIENT_ID = "GCM_ANDROID_DARK"
ANDROID_SERVICE_URL = "https://mobile.integration.garmin.com/gcm/android"
ANDROID_LOGIN_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)

# -- Portal (fallback) constants --
PORTAL_SSO_CLIENT_ID = "GarminConnect"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_DI_TOKEN_URL_TEMPLATE = "https://diauth.{domain}/di-oauth2-service/oauth/token"
DI_GRANT_TYPE = (
    "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
)
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    "GARMIN_CONNECT_MOBILE_IOS_DI",
)

# -- Anti-WAF delay bounds (seconds) --
# Cloudflare flags rapid GET→POST sequences as bot-like.
LOGIN_DELAY_MIN_S = 10.0
LOGIN_DELAY_MAX_S = 20.0
# Widget flow uses a shorter delay (different rate-limit bucket).
WIDGET_DELAY_MIN_S = 3.0
WIDGET_DELAY_MAX_S = 8.0

# -- TLS impersonation profiles --
MOBILE_IMPERSONATIONS: tuple[str, ...] = ("safari_ios", "safari", "chrome120")
PORTAL_IMPERSONATIONS: tuple[str, ...] = (
    "safari",
    "safari_ios",
    "chrome120",
    "edge101",
    "chrome",
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


# Garmin's widget MFA page exposes these variables in inline <script> tags.
_WIDGET_MFA_VARS_RE = re.compile(
    r'var\s+(customerGuid|mfaMethod|locale|clientId|codeSentTo)\s*=\s*"([^"]*)"\s*;'
)


def _parse_widget_mfa_vars(html: str) -> dict[str, str]:
    """Extract the inline JS variables Garmin uses for widget MFA."""
    return {m.group(1): m.group(2) for m in _WIDGET_MFA_VARS_RE.finditer(html)}


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
        self._di_token_url = _DI_TOKEN_URL_TEMPLATE.format(domain=domain)

        # Native DI Bearer tokens
        self.di_token: str | None = None
        self.di_refresh_token: str | None = None
        self.di_client_id: str | None = None

        self._tokenstore_path: str | None = None
        # Serialize token refresh and state mutation so concurrent API calls can't
        # race on the same refresh token or observe half-updated state.
        self._token_lock: threading.RLock = threading.RLock()
        # True when a login strategy has raised GarminMFARequired and MFA state
        # is held on this instance. Prevents interleaving another login on the
        # same instance while MFA is pending.
        self._mfa_pending = False

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

    def _clear_auth_state(self) -> None:
        """Wipe in-memory tokens so the next strategy starts clean."""
        self.di_token = None
        self.di_refresh_token = None
        self.di_client_id = None
        self._clear_mfa_state()

    def _clear_mfa_state(self) -> None:
        """Clear pending MFA state so a new login can start."""
        self._mfa_pending = False
        for attr in (
            "_mfa_session",
            "_mfa_login_params",
            "_mfa_post_headers",
            "_mfa_service_url",
            "_mfa_flow",
            "_mfa_method",
            "_widget_last_resp",
        ):
            setattr(self, attr, None)

    def _verify_token(self) -> bool:
        """Confirm the current token is accepted by the API tier.

        A login strategy can obtain a DI token from the auth host (HTTP 200)
        that the API tier (connectapi) then rejects with 401 "Token is not
        active" — this is account/region dependent. We confirm the token works
        with one lightweight authenticated call.

        Returns True if accepted, or if the check is inconclusive. Only a
        definitive auth rejection (401/403) returns False, so a transient
        network error or 5xx never blocks an otherwise-working login.
        """
        if not self.is_authenticated:
            return False
        try:
            r = cffi_requests.get(
                f"{self._connectapi}/userprofile-service/socialProfile",
                headers=self.get_api_headers(),
                impersonate="chrome",
                timeout=15,
            )
        except Exception as e:
            _LOGGER.debug(
                "Token validation inconclusive (kept): %s",
                _sanitize_exception_text(e),
            )
            return True
        if r.status_code in (401, 403):
            _LOGGER.warning("Token rejected by API tier: HTTP %s", r.status_code)
            return False
        return True

    def _token_expires_soon(self) -> bool:
        """Check if the active token will expire within 15 minutes."""
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
                if exp and time.time() > (int(exp) - 900):
                    return True
        except Exception:
            _LOGGER.debug("Failed to check token expiry")
        return False

    # ------------------------------------------------------------------ #
    #  LOGIN — cascading strategy chain                                    #
    # ------------------------------------------------------------------ #

    def login(self, email: str, password: str) -> AuthResult:
        """Login using a cascading strategy chain.

        Tries each strategy in order.  Only GarminAuthError (bad credentials)
        and GarminMFARequired stop the chain immediately — all other failures
        (429 rate limits, transport errors, HTML challenges) fall through to
        the next strategy.
        """
        if self._mfa_pending:
            raise GarminAuthError(
                "MFA login already in progress; complete it with complete_mfa() "
                "or call logout() first"
            )
        # Start every credential login from a clean slate: a stale token from
        # a previous login/session-load must not leave is_authenticated True
        # when every strategy fails.
        self._clear_auth_state()
        # CN SSO does not support the mobile API endpoint, so web-based
        # strategies must run first.  For .com accounts the mobile strategies
        # stay at the front because they bypass Cloudflare more reliably.
        if self._is_cn:
            strategies: list[tuple[str, Any]] = [
                ("widget+cffi", lambda: self._widget_web_login(email, password)),
                ("portal+cffi", lambda: self._portal_web_login_cffi(email, password)),
                (
                    "portal+requests",
                    lambda: self._portal_web_login_requests(email, password),
                ),
                ("mobile+cffi", lambda: self._mobile_login_cffi(email, password)),
                (
                    "mobile+requests",
                    lambda: self._mobile_login_requests(email, password),
                ),
            ]
        else:
            strategies = [
                ("mobile+cffi", lambda: self._mobile_login_cffi(email, password)),
                (
                    "mobile+requests",
                    lambda: self._mobile_login_requests(email, password),
                ),
                ("widget+cffi", lambda: self._widget_web_login(email, password)),
                ("portal+cffi", lambda: self._portal_web_login_cffi(email, password)),
                (
                    "portal+requests",
                    lambda: self._portal_web_login_requests(email, password),
                ),
            ]

        last_err: Exception | None = None
        rate_limited_count = 0

        for name, run in strategies:
            try:
                _LOGGER.debug("Trying login strategy: %s", name)
                result = run()
                # A strategy may get a token the API tier rejects (account/
                # region specific). Verify before accepting; otherwise fall
                # through to the next strategy.
                if not self._verify_token():
                    _LOGGER.warning(
                        "%s obtained a token the API rejected; trying next strategy",
                        name,
                    )
                    self._clear_auth_state()
                    last_err = GarminAPIError(f"{name}: token rejected by API tier")
                    continue
                return result
            except (GarminAuthError, GarminMFARequired):
                # Bad credentials or MFA needed — stop immediately.
                raise
            except GarminRateLimitError as e:
                _LOGGER.warning(
                    "%s returned 429: %s", name, _sanitize_exception_text(e)
                )
                rate_limited_count += 1
                last_err = e
                continue
            except Exception as e:
                _LOGGER.warning("%s failed: %s", name, _sanitize_exception_text(e))
                last_err = e
                continue

        if rate_limited_count == len(strategies):
            raise GarminRateLimitError(
                "All login strategies rate limited (429). "
                "Garmin may be blocking this client or network rather than "
                "rate limiting; try again later or from another network."
            )
        raise GarminAPIError(
            "All login strategies exhausted: "
            + (_sanitize_exception_text(last_err) if last_err else "no strategies ran")
        )

    # ------------------------------------------------------------------ #
    #  STRATEGY 1 — Mobile iOS + curl_cffi (TLS fingerprint rotation)     #
    # ------------------------------------------------------------------ #

    def _mobile_login_cffi(self, email: str, password: str) -> AuthResult:
        """Mobile login with curl_cffi TLS fingerprint rotation.

        Different TLS fingerprints land in different Cloudflare rate-limit
        buckets, so rotating through them gives us multiple shots.
        """
        last_err: Exception | None = None
        for imp in MOBILE_IMPERSONATIONS:
            try:
                _LOGGER.debug("mobile+cffi trying impersonation=%s", imp)
                sess: Any = cffi_requests.Session(impersonate=cast(Any, imp))
                return self._do_mobile_login(sess, email, password)
            except (GarminAuthError, GarminMFARequired):
                raise
            except GarminRateLimitError as e:
                _LOGGER.debug(
                    "mobile+cffi(%s) 429: %s", imp, _sanitize_exception_text(e)
                )
                last_err = e
                continue
            except Exception as e:
                _LOGGER.debug(
                    "mobile+cffi(%s) failed: %s", imp, _sanitize_exception_text(e)
                )
                last_err = e
                continue

        if last_err:
            raise last_err
        raise GarminAPIError("mobile+cffi: no impersonations available")

    # ------------------------------------------------------------------ #
    #  STRATEGY 2 — Mobile iOS + plain requests                           #
    # ------------------------------------------------------------------ #

    def _mobile_login_requests(self, email: str, password: str) -> AuthResult:
        """Mobile login with plain requests (no TLS fingerprinting)."""
        import requests as stdlib_requests

        sess = stdlib_requests.Session()
        return self._do_mobile_login(sess, email, password)

    # ------------------------------------------------------------------ #
    #  Shared mobile login logic                                          #
    # ------------------------------------------------------------------ #

    def _do_mobile_login(self, sess: Any, email: str, password: str) -> AuthResult:
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
            raise GarminRateLimitError(
                "Mobile login returned 429 — rate limited or blocked by Garmin "
                "(can be triggered by the client fingerprint, not only the IP)"
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
            self._mfa_pending = True
            raise GarminMFARequired("mfa_required")

        if resp_type == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._exchange_service_ticket(ticket, service_url=IOS_SERVICE_URL)
            return AuthResult(success=True)

        if resp_type == "INVALID_USERNAME_PASSWORD":
            raise GarminAuthError("401 Unauthorized (Invalid Username or Password)")

        # Check for 429 buried inside JSON error body
        if res.get("error", {}).get("status-code") == "429":
            raise GarminRateLimitError("Mobile login: 429 in JSON body")

        _LOGGER.warning(
            "Mobile login: unrecognised response type: %r",
            resp_type,
        )
        raise GarminAPIError(
            f"Mobile login failed: unrecognised response type {resp_type!r}"
        )

    # ------------------------------------------------------------------ #
    #  STRATEGY 3 — SSO Embed Widget + curl_cffi                         #
    # ------------------------------------------------------------------ #

    def _widget_web_login(self, email: str, password: str) -> AuthResult:
        """Login via the SSO embed HTML widget.

        Uses HTML form flow which bypasses clientId-based rate limits.
        Uses curl_cffi for TLS fingerprinting.
        """
        sess: Any = cffi_requests.Session(impersonate="chrome", timeout=30)
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

        # Step 1: GET embed page to establish session cookies
        r = sess.get(sso_embed, params=embed_params)
        if r.status_code == 429:
            raise GarminRateLimitError("Widget embed GET returned 429")
        if not r.ok:
            raise GarminAPIError(f"Widget embed returned {r.status_code}")

        # Step 2: GET signin page for CSRF token
        r = sess.get(
            f"{sso_base}/signin", params=signin_params, headers={"Referer": sso_embed}
        )
        if r.status_code == 429:
            raise GarminRateLimitError("Widget signin GET returned 429")

        csrf_match = self._CSRF_RE.search(r.text)
        if not csrf_match:
            raise GarminAPIError("Widget login: missing CSRF token")

        # Anti-WAF delay between GET and POST
        delay_s = random.uniform(WIDGET_DELAY_MIN_S, WIDGET_DELAY_MAX_S)
        _LOGGER.debug("Widget login: waiting %.0fs anti-WAF delay...", delay_s)
        time.sleep(delay_s)

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

        if r.status_code == 429:
            raise GarminRateLimitError("Widget signin POST returned 429")

        title_match = self._TITLE_RE.search(r.text)
        title = title_match.group(1) if title_match else ""

        # Detect server/infrastructure errors — fall through to next strategy
        title_lower = title.lower()
        if any(
            hint in title_lower
            for hint in (
                "bad gateway",
                "service unavailable",
                "cloudflare",
                "502",
                "503",
            )
        ):
            raise GarminAPIError(f"Widget login: server error '{title}'")

        # Early credential detection — don't waste remaining strategies
        if any(
            hint in title_lower
            for hint in ("locked", "invalid", "incorrect", "account error")
        ):
            raise GarminAuthError(f"Widget authentication failed: '{title}'")

        # "Unable To Sign In" means the account is restricted from web SSO
        # (e.g. Garmin child/family accounts). Fall through to portal JSON API
        # strategies which use a different path and may succeed.
        if "unable to sign in" in title_lower or "unable to login" in title_lower:
            _LOGGER.warning(
                "Widget login: '%s' — account may be a Garmin child/family account "
                "restricted from web SSO; falling through to portal strategies.",
                title,
            )
            raise GarminAPIError(f"Widget login: account restricted '{title}'")

        # MFA challenge. The signin page itself is also titled "GARMIN
        # Authentication Application", so rely on the inline JS variables
        # Garmin emits for the MFA page (mfaMethod, customerGuid, etc.) rather
        # than the title alone. Email/SMS MFA pages may not actually send the
        # code during the credential POST, so we explicitly request delivery via
        # the same endpoint the browser's "Request a new code" link uses.
        mfa_vars = _parse_widget_mfa_vars(r.text)
        mfa_method = mfa_vars.get("mfaMethod", "").lower()
        looks_like_mfa = "mfa" in title_lower or (
            "authentication application" in title_lower and mfa_method
        )
        if looks_like_mfa:
            self._widget_request_mfa_code(sess, r.text, r.url)
            self._mfa_session = sess
            self._mfa_login_params = signin_params
            self._mfa_post_headers = {"Referer": r.url}
            self._mfa_flow = "widget"
            self._mfa_method = mfa_vars.get("mfaMethod", "")
            self._widget_last_resp = r
            self._mfa_pending = True
            raise GarminMFARequired("mfa_required")

        if title != "Success":
            # "GARMIN Authentication Application" without MFA variables is an
            # intermediate auth page (e.g. login challenge/consent). Fall
            # through to portal strategies rather than logging an "unexpected
            # title" warning.
            if title == "GARMIN Authentication Application":
                raise GarminAPIError(f"Widget login: auth application page '{title}'")
            raise GarminAPIError(f"Widget login: unexpected title '{title}'")

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

        if r.status_code == 429:
            raise GarminRateLimitError("Widget MFA verify returned 429")

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

    def _widget_request_mfa_code(self, sess: Any, page_html: str, referer: str) -> None:
        """Request Garmin to send an email/SMS MFA code for this widget session.

        The widget's own JavaScript calls ``/sso/verifyMFA/mfaCode`` when the
        user clicks "Request a new code". We use the same endpoint to ensure
        the code is actually delivered before prompting the user.
        """
        mfa_vars = _parse_widget_mfa_vars(page_html)
        mfa_method = mfa_vars.get("mfaMethod", "").lower()
        if mfa_method not in ("email", "sms"):
            # Nothing to trigger for TOTP/authenticator apps.
            return

        if mfa_vars.get("codeSentTo"):
            # Garmin already sent a code during the signin POST.
            return

        client_id = mfa_vars.get("clientId", "")
        payload = {
            "customerGuid": mfa_vars.get("customerGuid", ""),
            "mfaMethod": mfa_vars.get("mfaMethod", ""),
            "locale": mfa_vars.get("locale", ""),
        }

        _LOGGER.debug(
            "Widget MFA: explicitly requesting %s code from Garmin", mfa_method
        )
        r = sess.post(
            f"{self._sso}/sso/verifyMFA/mfaCode",
            params={"clientId": client_id},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Referer": referer,
            },
            json=payload,
            timeout=30,
        )

        if r.status_code == 429:
            raise GarminRateLimitError("Widget MFA code request returned 429")
        if not r.ok:
            raise GarminAPIError(f"Widget MFA code request returned {r.status_code}")

    # ------------------------------------------------------------------ #
    #  STRATEGY 4 — Portal web + curl_cffi (TLS fingerprint rotation)    #
    # ------------------------------------------------------------------ #

    def _portal_web_login_cffi(self, email: str, password: str) -> AuthResult:
        """Portal login with curl_cffi TLS fingerprint rotation.

        Different TLS fingerprints land in different Cloudflare rate-limit
        buckets, so rotating through them gives us multiple shots.
        """
        last_err: Exception | None = None
        for imp in PORTAL_IMPERSONATIONS:
            try:
                _LOGGER.debug("portal+cffi trying impersonation=%s", imp)
                sess: Any = cffi_requests.Session(impersonate=cast(Any, imp))
                return self._do_portal_web_login(sess, email, password)
            except (GarminAuthError, GarminMFARequired):
                raise
            except GarminRateLimitError as e:
                _LOGGER.debug(
                    "portal+cffi(%s) 429: %s", imp, _sanitize_exception_text(e)
                )
                last_err = e
                continue
            except Exception as e:
                _LOGGER.debug(
                    "portal+cffi(%s) failed: %s", imp, _sanitize_exception_text(e)
                )
                last_err = e
                continue

        if last_err:
            raise last_err
        raise GarminAPIError("portal+cffi: no impersonations available")

    # ------------------------------------------------------------------ #
    #  STRATEGY 5 — Portal web + plain requests                          #
    # ------------------------------------------------------------------ #

    def _portal_web_login_requests(self, email: str, password: str) -> AuthResult:
        """Portal login with plain requests (no TLS fingerprinting)."""
        import requests as stdlib_requests

        sess = stdlib_requests.Session()
        return self._do_portal_web_login(sess, email, password)

    # ------------------------------------------------------------------ #
    #  Shared portal login logic                                          #
    # ------------------------------------------------------------------ #

    def _do_portal_web_login(self, sess: Any, email: str, password: str) -> AuthResult:
        """Login via /portal/api/login — desktop browser flow."""
        signin_url = f"{self._sso}/portal/sso/en-US/sign-in"
        browser_hdrs = _random_browser_headers()

        # Step 1: GET the signin page to grab initial cookies
        get_resp = sess.get(
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

        if get_resp.status_code == 429:
            raise GarminRateLimitError(
                "Portal login GET returned 429 — Cloudflare blocking this request."
            )

        # Anti-WAF delay: 30-45s mimics real browser "read then type" behaviour.
        delay_s = random.uniform(LOGIN_DELAY_MIN_S, LOGIN_DELAY_MAX_S)
        _LOGGER.info(
            "Portal login: waiting %.0fs to avoid Cloudflare rate limiting...",
            delay_s,
        )
        time.sleep(delay_s)

        # Step 2: POST credentials
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
            raise GarminRateLimitError(
                "Portal login POST returned 429 — Cloudflare blocking this request."
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
            self._mfa_pending = True
            raise GarminMFARequired("mfa_required")

        if resp_type == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._exchange_service_ticket(ticket)
            return AuthResult(success=True)

        if resp_type == "INVALID_USERNAME_PASSWORD":
            raise GarminAuthError("401 Unauthorized (Invalid Username or Password)")

        # Check for 429 buried inside JSON error body
        if res.get("error", {}).get("status-code") == "429":
            raise GarminRateLimitError("Portal login: 429 in JSON body")

        _LOGGER.warning(
            "Portal login: unrecognised response type=%r",
            resp_type,
        )
        raise GarminAPIError(
            f"Portal web login failed: unrecognised response type {resp_type!r}"
        )

    # ------------------------------------------------------------------ #
    #  MFA COMPLETION — dual-endpoint fallback                            #
    # ------------------------------------------------------------------ #

    def complete_mfa(self, mfa_code: str) -> AuthResult:
        """Complete MFA verification."""
        if not hasattr(self, "_mfa_session"):
            raise GarminAuthError("No pending MFA session")
        try:
            self._complete_mfa(mfa_code)
            # The MFA session is tied to one strategy, so there's no fall-through
            # here — but still fail loudly rather than store a token the API tier
            # rejects (see _verify_token).
            if not self._verify_token():
                self._clear_auth_state()
                self._clear_mfa_state()
                raise GarminAuthError("Token rejected by API tier after MFA")
            return AuthResult(success=True)
        finally:
            # Always clear the pending MFA flag and per-attempt state so a new
            # login can start after complete_mfa() finishes (success or failure).
            self._clear_mfa_state()

    def _complete_mfa(self, mfa_code: str) -> None:
        """Complete MFA — uses the endpoint matching the login flow that triggered it.

        For portal/ios flows, tries both /portal and /mobile MFA verify endpoints
        as they may be on different rate-limit buckets.
        """
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

        # Map flow name to SSO path segment ("ios" flow uses /mobile/ endpoint)
        flow_path = "mobile" if flow == "ios" else flow

        # Try both MFA endpoints — they share SSO session cookies but may be
        # on different rate-limit buckets.
        mfa_endpoints = [
            (
                f"{self._sso}/{flow_path}/api/mfa/verifyCode",
                self._mfa_login_params,
                self._mfa_post_headers,
            ),
        ]
        # Add the other path as fallback
        if flow_path == "mobile":
            alt_endpoint = f"{self._sso}/portal/api/mfa/verifyCode"
            alt_params = {
                "clientId": PORTAL_SSO_CLIENT_ID,
                "locale": "en-US",
                "service": self._portal_service_url,
            }
        else:
            alt_endpoint = f"{self._sso}/mobile/api/mfa/verifyCode"
            alt_params = {
                "clientId": IOS_SSO_CLIENT_ID,
                "locale": "en-US",
                "service": IOS_SERVICE_URL,
            }
        mfa_endpoints.append((alt_endpoint, alt_params, self._mfa_post_headers))

        failures: list[str] = []
        rate_limited_count = 0

        for mfa_url, params, headers in mfa_endpoints:
            try:
                r = sess.post(
                    mfa_url,
                    params=params,
                    headers=headers,
                    json=mfa_json,
                    timeout=30,
                )
            except Exception as e:
                failures.append(
                    f"{mfa_url}: connection error {_sanitize_exception_text(e)}"
                )
                continue

            if r.status_code == 429:
                failures.append(f"{mfa_url}: HTTP 429")
                rate_limited_count += 1
                continue

            try:
                res = r.json()
            except Exception:
                # Non-JSON response is almost always a Cloudflare HTML challenge
                failures.append(f"{mfa_url}: HTTP {r.status_code} non-JSON")
                continue

            if res.get("error", {}).get("status-code") == "429":
                failures.append(f"{mfa_url}: 429 in JSON body")
                rate_limited_count += 1
                continue

            if res.get("responseStatus", {}).get("type") == "SUCCESSFUL":
                ticket = res["serviceTicketId"]
                svc_url = (
                    IOS_SERVICE_URL
                    if flow == "ios"
                    else getattr(self, "_mfa_service_url", self._portal_service_url)
                )
                self._exchange_service_ticket(ticket, service_url=svc_url)
                return

            # Non-success JSON response — could be auth failure. Record only
            # the error status, never the raw body (may contain PII).
            status = res.get("error", {}).get("status-code", "unexpected response")
            failures.append(f"{mfa_url}: {status}")

        # All endpoints failed
        if rate_limited_count == len(mfa_endpoints):
            raise GarminRateLimitError(
                f"MFA verification rate limited on all endpoints: {failures}"
            )
        raise GarminAuthError(f"MFA verification failed: {failures}")

    # ------------------------------------------------------------------ #
    #  DI TOKEN EXCHANGE                                                  #
    # ------------------------------------------------------------------ #

    def _exchange_service_ticket(
        self, ticket: str, service_url: str | None = None
    ) -> None:
        """Exchange a CAS ticket for a DI Bearer token via diauth."""
        svc_url = service_url or self._portal_service_url

        di_token = None
        di_refresh = None
        di_client_id = None

        for client_id in DI_CLIENT_IDS:
            r = _http_post(
                self._di_token_url,
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
                raise GarminRateLimitError("DI token exchange rate limited")
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
                _LOGGER.debug(
                    "DI token parse failed for %s: %s",
                    client_id,
                    _sanitize_exception_text(e),
                )
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

    # ------------------------------------------------------------------ #
    #  TOKEN REFRESH                                                      #
    # ------------------------------------------------------------------ #

    def _refresh_di_token(self) -> None:
        """Refresh the DI Bearer token using the stored refresh token."""
        with self._token_lock:
            if not self.di_refresh_token or not self.di_client_id:
                raise GarminAuthError("No DI refresh token available")
            refresh_token = self.di_refresh_token
            client_id = self.di_client_id
        r = _http_post(
            self._di_token_url,
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
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if r.status_code == 429:
            raise GarminRateLimitError("DI token refresh rate limited (429)")
        if not r.ok:
            raise GarminAuthError(f"DI token refresh failed: HTTP {r.status_code}")
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
            await asyncio.to_thread(self._refresh_with_lock)
            return True
        except Exception as err:
            _LOGGER.debug("DI token refresh failed: %s", _sanitize_exception_text(err))
        return False

    def _sync_refresh_if_needed(self) -> None:
        """Synchronously refresh the token if it expires soon.

        This is meant to be called from async code via asyncio.to_thread so
        the check-then-refresh sequence is atomic under the instance token lock.
        """
        with self._token_lock:
            if self._token_expires_soon():
                self._refresh_with_lock()

    def _refresh_with_lock(self) -> None:
        """Synchronous token refresh guarded by the instance token lock."""
        with self._token_lock:
            self._refresh_di_token()
            if self._tokenstore_path:
                self._persist_tokens(self._tokenstore_path)

    def _persist_tokens(self, path: str | None) -> None:
        """Persist tokens to *path*, logging a warning when the write fails.

        Previously these writes were wrapped in ``contextlib.suppress(Exception)``,
        which hid disk-full / permission-denied / read-only-filesystem errors and
        made automated setups fail silently on restart. We still swallow the error
        (login/refresh should continue) but now log it at WARNING so operators can
        diagnose the loss of persisted credentials.
        """
        if not path:
            return
        try:
            self.save_session(path)
        except Exception as err:
            _LOGGER.warning("Failed to persist tokens to %s: %s", path, err)

    # ------------------------------------------------------------------ #
    #  SESSION PERSISTENCE                                                #
    # ------------------------------------------------------------------ #

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

        p = token_file_path(path)
        # The token file holds the DI refresh token (persistent account access),
        # so write it owner-only (0o600) inside a 0o700 directory regardless of
        # the process umask — a permissive umask must not leave it world-readable
        # on a shared host.
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            p.parent.chmod(0o700)
        # Write to an unpredictable sibling temp file with O_CREAT|O_EXCL
        # (mode 0o600, and O_NOFOLLOW where available), then atomically
        # replace the target. A fixed sibling name would be guessable, and
        # without O_EXCL a pre-planted file's inode would be reused for the
        # write instead of a fresh one being created, letting whoever holds
        # a descriptor on that pre-planted inode observe the token payload
        # as it's written. Writing straight to the target with O_TRUNC has
        # the same problem, plus it isn't atomic against a concurrent reader.
        tmp = p.with_name(f".{p.name}.{secrets.token_hex(8)}.tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as token_file:
                token_file.write(json.dumps(data, indent=2))
            # Enforce 0o600 even if the file pre-existed with looser permissions.
            with contextlib.suppress(OSError):
                tmp.chmod(0o600)
            tmp.replace(p)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def logout(self) -> None:
        """Drop all in-memory auth state and delete the persisted token file.

        Clears DI tokens, MFA state, and the remembered tokenstore path,
        then removes the on-disk token file (if any) so no usable
        credentials remain on this instance or in the tokenstore.
        """
        self._clear_auth_state()
        path, self._tokenstore_path = self._tokenstore_path, None
        if path:
            try:
                token_file_path(path).unlink(missing_ok=True)
            except OSError as e:
                _LOGGER.warning("Failed to delete token file %s: %s", path, e)

    def load_session(self, path: str | Path) -> bool:
        """Load tokens from disk."""
        try:
            p = token_file_path(path)
        except ValueError as e:
            # Includes symlink rejection — a potential attack, never silent.
            _LOGGER.warning("Refusing to load tokens from %s: %s", path, e)
            return False
        if not p.exists():
            return False

        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(p, flags)
            with os.fdopen(fd, encoding="utf-8") as token_file:
                data = json.loads(token_file.read())
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
                    _LOGGER.debug(
                        "Proactive refresh failed: %s", _sanitize_exception_text(e)
                    )

            return True
        except Exception as e:
            # Never log file contents or token values — type and message only.
            _LOGGER.warning("Failed to load tokens from %s: %s", p, type(e).__name__)
            return False
