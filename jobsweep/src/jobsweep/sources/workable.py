"""Workable career pages.

* list: ``POST https://apply.workable.com/api/v3/accounts/{board}/jobs`` with
  ``{"query":"","location":[],"department":[],"worktype":[],"remote":[]}``; 10 results per page,
  ``nextPage`` token sent back as ``"token"`` in the same body until it disappears.
* detail: ``GET https://apply.workable.com/api/v2/accounts/{board}/jobs/{shortcode}`` -> the list fields
  plus ``description``/``requirements``/``benefits`` (HTML).
* public URL: ``https://apply.workable.com/{board}/j/{shortcode}/``.

apply.workable.com sits behind Cloudflare rate limiting (HTTP 429 "error code: 1015", Retry-After of
hours once tripped; observed 2026-10-25 from this egress after ~50 parallel probes). The adapter
therefore spaces its calls (``min_interval`` seconds per HTTP client), retries at most once, and after a
429 fails fast for ``cooldown`` seconds instead of hammering the host. Response shapes were checked against
real captures from 2026-10-23 (account ``wati-dot-i-o``) because the live API was rate limited here.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any

from ..http import HTTPError
from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, safe_parse, structured_tags

LIST_URL = "https://apply.workable.com/api/v3/accounts/{board}/jobs"
DETAIL_URL = "https://apply.workable.com/api/v2/accounts/{board}/jobs/{shortcode}"
DEFAULT_MAX_JOBS = 1000
EMPTY_QUERY: dict[str, Any] = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}


class _Pacer:
    """Serialises Workable calls per HTTP client with a minimum spacing and a 429 circuit breaker."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.blocked_until = 0.0

    async def wait(self, interval: float) -> None:
        if time.monotonic() < self.blocked_until:
            raise SourceError("workable: rate limited (HTTP 429), cooling down")
        async with self.lock:
            delay = self.last + interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


_pacers: weakref.WeakKeyDictionary[Any, _Pacer] = weakref.WeakKeyDictionary()


def _location_text(loc: dict) -> str:
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(str(p) for p in parts if p)


def _locations(j: dict) -> str:
    locs = [loc for loc in j.get("locations") or [] if isinstance(loc, dict) and not loc.get("hidden")]
    if not locs and isinstance(j.get("location"), dict):
        locs = [j["location"]]
    return join_locations(_location_text(loc) for loc in locs)


def _remote(j: dict) -> bool | None:
    workplace = remote_from_text(str(j.get("workplace") or "").replace("_", " "))
    if workplace is not None:
        return workplace
    if j.get("remote") is True:
        return True
    if j.get("remote") is False:
        return False
    return None


def _department(value: Any) -> str | None:
    if isinstance(value, list):
        names = [str(v) for v in value if v]
        return ", ".join(names) or None
    return str(value) if value else None


class WorkableSource(Source):
    name = "workable"
    kind = "board"
    label = "Workable career pages"
    docs_url = "https://help.workable.com/"
    #: seconds between two calls to apply.workable.com on one client (tests set 0)
    min_interval: float = 1.0
    #: seconds to stop calling apply.workable.com after an HTTP 429
    cooldown: float = 300.0

    def _pacer(self) -> _Pacer:
        try:
            pacer = _pacers.get(self.ctx.client)
            if pacer is None:
                pacer = _pacers[self.ctx.client] = _Pacer()
            return pacer
        except TypeError:  # client not weak-referenceable
            return _Pacer()

    async def _call(self, method: str, url: str, payload: Any = None) -> Any:
        pacer = self._pacer()
        await pacer.wait(self.min_interval)
        try:
            if method == "POST":
                return await self.post_json(url, payload, retries=1)
            return await self.get_json(url, retries=1)
        except HTTPError as exc:
            if exc.status == 429:
                pacer.blocked_until = time.monotonic() + self.cooldown
            raise

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip()
        if not board:
            raise SourceError(f"workable: employer {employer.key} has no account name")
        max_jobs = employer.max_jobs or DEFAULT_MAX_JOBS
        url = LIST_URL.format(board=board)
        results: list[dict] = []
        seen: set[str] = set()
        token: str | None = None
        seen_tokens: set[str] = set()
        while len(results) < max_jobs:
            body = dict(EMPTY_QUERY)
            if token:
                body["token"] = token
            data = await self._call("POST", url, body)
            if not isinstance(data, dict):
                raise SourceError(f"workable {board}: unexpected payload {type(data).__name__}")
            for r in data.get("results") or []:
                code = r.get("shortcode") if isinstance(r, dict) else None
                if code and code not in seen:
                    seen.add(code)
                    results.append(r)
            token = data.get("nextPage") or None
            if not token or token in seen_tokens or not data.get("results"):
                break
            seen_tokens.add(token)
        return safe_parse(
            results[:max_jobs],
            lambda r: self._parse(r, employer, board),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )

    def _parse(self, r: dict, employer: Employer, board: str) -> Job | None:
        code, title = r.get("shortcode"), (r.get("title") or "").strip()
        if not code or not title or r.get("isInternal") is True:
            return None
        if r.get("state") not in (None, "published"):
            return None
        raw_type = r.get("type") or r.get("employmentType")
        department = _department(r.get("department"))
        extra: dict[str, Any] = {"board": board}
        for key, value in (
            ("workplace", r.get("workplace")),
            ("language", r.get("language")),
            ("code", r.get("code")),
        ):
            if value:
                extra[key] = value
        job = Job(
            source=self.name,
            source_id=str(code),
            title=title,
            company=employer.name,
            url=f"https://apply.workable.com/{board}/j/{code}/",
            location=_locations(r),
            remote=_remote(r),
            posted_at=iso_date(r.get("published")),
            employment_type=normalize_employment_type(raw_type),
            department=department,
            employer_key=employer.key,
            tags=structured_tags(raw_type, r.get("department")),
            extra=extra,
        )
        if r.get("description") or r.get("requirements"):  # detail-shaped payload
            job.description = _description(r)
        return job

    async def enrich(self, job: Job) -> Job:
        """Fetch description/requirements/benefits for one job (list pages carry none)."""
        board = (job.extra or {}).get("board")
        if not board:
            return job
        try:
            d = await self._call("GET", DETAIL_URL.format(board=board, shortcode=job.source_id))
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("workable enrich %s failed: %s", job.uid, exc)
            return job
        if not isinstance(d, dict):
            return job
        try:
            job.description = _description(d) or job.description
            if d.get("type") and not job.employment_type:
                job.employment_type = normalize_employment_type(d.get("type"))
            location = _locations(d)
            if location:
                job.location = location
            job.extra["enriched"] = True
        except Exception as exc:  # noqa: BLE001 - a malformed detail payload must not lose the job
            self.ctx.log.warning("workable enrich %s: malformed detail (%s)", job.uid, exc)
        return job


def _description(d: dict) -> str:
    chunks = []
    for key, heading in (("description", ""), ("requirements", "Requirements"), ("benefits", "Benefits")):
        html = d.get(key)
        if html:
            chunks.append(f"<h3>{heading}</h3>{html}" if heading else html)
    return html_to_text("".join(chunks))
