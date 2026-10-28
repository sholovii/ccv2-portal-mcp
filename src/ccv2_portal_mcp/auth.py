"""Technical-user authentication for the Cloud Portal API.

Since the March 2026 Kyma migration the portal accepts only OAuth2 client-credentials tokens
issued by SAP IAS for a *technical user*. Two details differ from a textbook flow and both
are easy to get wrong:

- the token request must carry a `resource` parameter (the value the portal shows when the
  technical user is created), and
- the token travels in `x-approuter-authorization: Bearer <token>`, not `Authorization`.
  With the wrong header the portal answers HTTP 200 with an HTML login page.
"""

from __future__ import annotations

import threading
import time

import httpx

AUTH_HEADER = "x-approuter-authorization"


class AuthError(RuntimeError):
    """Token could not be obtained or was rejected."""


class TokenProvider:
    """Caches one access token per subscription and refreshes it shortly before expiry."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        resource: str,
        early_refresh_sec: int = 30,
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._resource = resource
        self._early = early_refresh_sec
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.monotonic() < self._expires_at - self._early:
                return self._token
            self._token, self._expires_at = self._fetch()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def header(self) -> dict[str, str]:
        return {AUTH_HEADER: f"Bearer {self.token()}"}

    def _fetch(self) -> tuple[str, float]:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "resource": self._resource,
        }
        try:
            resp = self._http.post(
                self._token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"token request to {self._token_url} failed: {exc}") from exc
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200:
            raise AuthError(
                f"token endpoint returned {resp.status_code}: {resp.text[:300]!r}. "
                "Check client id/secret, the `resource` value, and that the secret has not expired "
                "(technical-user secrets expire; only the creator can regenerate them)."
            )
        if "json" not in ctype:
            raise AuthError(
                f"token endpoint returned {ctype or 'no content-type'} instead of JSON; "
                "the token URL is probably wrong."
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthError(f"token response has no access_token: {body}")
        expires_in = float(body.get("expires_in", 3600))
        return token, time.monotonic() + expires_in
