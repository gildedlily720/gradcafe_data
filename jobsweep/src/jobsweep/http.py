"""Shared async HTTP helpers: one client per sweep, polite per-host concurrency, retries.

Adapters must use ``fetch_json`` / ``fetch_text`` (never raw httpx calls) so retries, rate limits
and timeouts behave the same everywhere and tests can mock with ``respx``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("jobsweep.http")

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class HostLimiter:
    """At most ``per_host`` concurrent requests to any single host."""

    def __init__(self, per_host: int = 4, overrides: dict[str, int] | None = None):
        self.per_host = per_host
        self.overrides = overrides or {}
        self._sems: dict[str, asyncio.Semaphore] = {}

    def for_url(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).hostname or ""
        sem = self._sems.get(host)
        if sem is None:
            limit = self.per_host
            for suffix, n in self.overrides.items():
                if host == suffix or host.endswith("." + suffix):
                    limit = n
                    break
            sem = self._sems[host] = asyncio.Semaphore(limit)
        return sem


# Hosts that are rate limited hard or free-tier metered.
DEFAULT_HOST_LIMITS = {
    "gateway.pipeworx.io": 1,
    "data.usajobs.gov": 2,
    "api.adzuna.com": 1,
    "api.theirstack.com": 1,
    "api.hasdata.com": 1,
    "api.apify.com": 2,
    "myworkdayjobs.com": 3,
}


def make_client(user_agent: str, timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: Any = None,
    limiter: HostLimiter | None = None,
    retries: int = 3,
    backoff: float = 1.5,
) -> httpx.Response:
    """Send a request with retries on 429/5xx/transport errors. Raises ``HTTPError`` on final failure."""
    attempt = 0
    while True:
        attempt += 1
        try:
            if limiter is not None:
                async with limiter.for_url(url):
                    resp = await client.request(
                        method, url, params=params, headers=headers, json=json_body, data=data
                    )
            else:
                resp = await client.request(
                    method, url, params=params, headers=headers, json=json_body, data=data
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt > retries:
                raise HTTPError(0, url, f"{type(exc).__name__}: {exc}") from exc
            await asyncio.sleep(backoff**attempt + random.random())
            continue
        if resp.status_code in RETRY_STATUSES and attempt <= retries:
            wait = backoff**attempt + random.random()
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                wait = min(float(ra), 60.0)
            log.debug("retry %s %s after %.1fs (status %s)", method, url, wait, resp.status_code)
            await asyncio.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise HTTPError(resp.status_code, str(resp.url), resp.text)
        return resp


async def fetch_json(client: httpx.AsyncClient, url: str, *, method: str = "GET", **kw: Any) -> Any:
    resp = await request(client, method, url, **kw)
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPError(resp.status_code, str(resp.url), "invalid JSON: " + resp.text[:200]) from exc


async def fetch_text(client: httpx.AsyncClient, url: str, *, method: str = "GET", **kw: Any) -> str:
    resp = await request(client, method, url, **kw)
    return resp.text
