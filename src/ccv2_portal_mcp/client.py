"""Thin HTTP client over the Cloud Portal API with error normalisation and pagination.

Every method is synchronous (tools offload to a worker thread). Failure modes that the raw
API reports confusingly are turned into explicit exceptions:

- HTML instead of JSON (wrong auth header, missing role, expired secret) -> AuthError
- 401 -> token refreshed once, then AuthError
- 3xx -> the configured host is wrong (the retired portalrotapi host answers 302)
- 429 -> bounded retry honouring Retry-After
- other 4xx/5xx -> PortalError carrying RFC 7807 title/detail when the portal sends it
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .auth import AuthError, TokenProvider
from .config import SubscriptionConfig

log = logging.getLogger(__name__)

PAGE_SIZE = 100  # hard cap enforced by the portal


class PortalError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        method: str = "",
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.title = title
        self.detail = detail
        self.method = method
        self.path = path


class PortalClient:
    def __init__(
        self,
        sub: SubscriptionConfig,
        *,
        timeout_sec: float = 30.0,
        early_refresh_sec: int = 30,
        retry_429_max: int = 3,
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.sub = sub
        self.base_url = sub.base_url
        self._http = http or httpx.Client(timeout=timeout_sec, follow_redirects=False)
        self._tokens = TokenProvider(
            self._http,
            token_url=sub.token_url,
            client_id=sub.client_id,
            client_secret=sub.client_secret,
            resource=sub.resource,
            early_refresh_sec=early_refresh_sec,
        )
        self._retry_429_max = retry_429_max
        self._sleep = sleep

    # ----------------------------------------------------------------- low level

    @property
    def prefix(self) -> str:
        return f"/subscriptions/{self.sub.code}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        accept: str = "application/json",
        content_type: str | None = None,
    ) -> httpx.Response:
        url = self.base_url + path
        headers = {"Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        attempts_429 = 0
        refreshed = False
        while True:
            headers.update(self._tokens.header())
            try:
                resp = self._http.request(method, url, params=params, json=json, headers=headers)
            except httpx.HTTPError as exc:
                raise PortalError(f"{method} {path}: transport error: {exc}", method=method, path=path) from exc

            ctype = resp.headers.get("content-type", "")
            if 300 <= resp.status_code < 400:
                raise PortalError(
                    f"{method} {path}: portal answered {resp.status_code} redirect to "
                    f"{resp.headers.get('location', '?')}. The base URL is probably the retired host; "
                    "use https://portalapi.commerce.ondemand.com/v2.",
                    status=resp.status_code, method=method, path=path,
                )
            if "text/html" in ctype:
                raise AuthError(
                    f"{method} {path}: portal returned an HTML login page (HTTP {resp.status_code}) "
                    "instead of JSON. Causes: wrong auth header, technical user lacks the role or "
                    "environment, or the client secret expired."
                )
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._tokens.invalidate()
                continue
            if resp.status_code == 429 and attempts_429 < self._retry_429_max:
                attempts_429 += 1
                delay = float(resp.headers.get("retry-after", 2 * attempts_429))
                log.warning("429 from portal on %s %s; retrying in %ss", method, path, delay)
                self._sleep(delay)
                continue
            if resp.status_code >= 400:
                raise self._error(resp, method, path)
            return resp

    def _error(self, resp: httpx.Response, method: str, path: str) -> PortalError:
        title = detail = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                title = body.get("title")
                detail = body.get("detail") or body.get("message")
        except ValueError:
            detail = resp.text[:300] or None
        hint = {
            401: "not authorized (token rejected)",
            403: "forbidden: the technical user's role or environment list does not allow this",
            404: "not found",
            409: "conflict: the environment is busy (another build/deployment in progress?)",
            412: "precondition failed",
            423: "locked: the environment is locked by another operation",
            501: "not enabled: this feature must be switched on in the Cloud Portal for the subscription",
        }.get(resp.status_code, "")
        msg = f"{method} {path}: HTTP {resp.status_code}"
        if hint:
            msg += f" ({hint})"
        if title or detail:
            msg += f": {title or ''}{' - ' if title and detail else ''}{detail or ''}"
        return PortalError(msg, status=resp.status_code, title=title, detail=detail, method=method, path=path)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def get_bytes(self, path: str) -> bytes:
        return self.request("GET", path, accept="application/octet-stream").content

    def get_paged(
        self, path: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict]:
        """Walk a `{value: [...], count: n}` listing up to `limit` items."""
        out: list[dict] = []
        skip = 0
        while True:
            want = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(out))
            if want <= 0:
                break
            page = self.get_json(path, {**(params or {}), "$top": want, "$skip": skip})
            items = page.get("value", []) if isinstance(page, dict) else page
            out.extend(items)
            skip += len(items)
            total = page.get("count") if isinstance(page, dict) else None
            if len(items) < want or (total is not None and skip >= total):
                break
        return out

    # ------------------------------------------------------------- environments

    def environments(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else None
        return self.get_paged(f"{self.prefix}/environments", params)

    def services(self, env: str) -> list[dict]:
        """getServices (Aug 2026). The help page prints the path without /subscriptions/;
        try the documented-elsewhere form first and fall back on 404."""
        try:
            data = self.get_json(f"{self.prefix}/environments/{env}/services")
        except PortalError as exc:
            if exc.status != 404:
                raise
            data = self.get_json(f"/{self.sub.code}/environments/{env}/services")
        return data.get("value", data) if isinstance(data, dict) else data

    def scaling(self, env: str) -> dict:
        return self.get_json(f"{self.prefix}/environments/{env}/scaling")

    def property(self, env: str, service: str, key: str) -> dict:
        return self.get_json(f"{self.prefix}/environments/{env}/services/{service}/properties/{key}")

    # -------------------------------------------------------------------- builds

    def builds(self, *, status_not: list[str] | None = None, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {"$orderby": "buildStartTimestamp desc"}
        if status_not:
            params["statusNot"] = status_not
        return self.get_paged(f"{self.prefix}/builds", params, limit=limit)

    def build(self, code: str) -> dict:
        return self.get_json(f"{self.prefix}/builds/{code}")

    def build_progress(self, code: str) -> dict:
        return self.get_json(f"{self.prefix}/builds/{code}/progress")

    def build_logs_zip(self, code: str) -> bytes:
        return self.get_bytes(f"{self.prefix}/builds/{code}/logs")

    def create_build(self, *, branch: str, name: str, application_code: str = "commerce-cloud") -> dict:
        body = {"applicationCode": application_code, "branch": branch, "name": name}
        return self.request("POST", f"{self.prefix}/builds", json=body, content_type="application/json").json()

    # --------------------------------------------------------------- deployments

    def deployment_modes(self) -> Any:
        return self.get_json(f"{self.prefix}/deploymentmodes")

    def deployments(
        self,
        *,
        environment: str | None = None,
        build: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        params: dict[str, Any] = {"$orderby": "createdTimestamp desc"}
        if environment:
            params["environmentCode"] = environment
        if build:
            params["buildCode"] = build
        if status:
            params["status"] = status
        return self.get_paged(f"{self.prefix}/deployments", params, limit=limit)

    def deployment(self, code: str) -> dict:
        return self.get_json(f"{self.prefix}/deployments/{code}")

    def deployment_progress(self, code: str) -> dict:
        return self.get_json(f"{self.prefix}/deployments/{code}/progress")

    def create_deployment(
        self, *, build_code: str, environment: str, database_update_mode: str, strategy: str
    ) -> dict:
        body = {
            "buildCode": build_code,
            "environmentCode": environment,
            "databaseUpdateMode": database_update_mode,
            "strategy": strategy,
        }
        return self.request(
            "POST", f"{self.prefix}/deployments", json=body, content_type="application/json"
        ).json()
