"""Workday career sites (CXS JSON API).

Board format ``tenant/wdN/site`` (e.g. ``rb/wd5/FRS``, ``aig/wd1/aig``). Verified live 2026-10-25:

* list: ``POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`` with
  ``{"appliedFacets": {}, "limit": 20, "offset": N, "searchText": term}``. ``limit`` > 20 -> HTTP 400.
  ``total`` is only populated on ``offset == 0`` (later pages say 0), so we page until a short page,
  ``max_jobs`` (default 400) or the first page's total.
* detail: ``GET .../wday/cxs/{tenant}/{site}{externalPath}`` -> ``jobPostingInfo`` (``jobDescription`` HTML,
  ``location``, ``additionalLocations``, ``timeType``, ``startDate`` = posting date, ``endDate`` = closing
  date, ``remoteType``, ``jobReqId``) + ``hiringOrganization``.
* public URL: ``https://{tenant}.{wdN}.myworkdayjobs.com/{site}{externalPath}`` (``externalUrl`` in the
  detail payload is sometimes truncated, so it is not used).

``source_id`` is ``"{tenant}:{requisition id}"`` (requisition ids such as ``R-0000033737`` repeat across
tenants); the id comes from ``bulletFields`` when it appears in ``externalPath``, else from the path tail.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..models import Employer, Job
from ..text import html_to_text, iso_date, relative_to_iso
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, safe_parse, structured_tags

PAGE_SIZE = 20
DEFAULT_MAX_JOBS = 400

_N_LOCATIONS = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
_ID_LIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")


def parse_board(board: str) -> tuple[str, str, str]:
    parts = [p for p in (board or "").strip().strip("/").split("/") if p]
    if len(parts) != 3 or not re.fullmatch(r"wd\d+", parts[1].lower()):
        raise SourceError(f"workday board must be 'tenant/wdN/site', got {board!r}")
    return parts[0].lower(), parts[1].lower(), parts[2]


def base_url(tenant: str, wd: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def _req_id(posting: dict, path: str) -> str:
    tail = path.rsplit("_", 1)[-1] if "_" in path else ""
    for b in posting.get("bulletFields") or []:
        b = str(b).strip()
        if b and _ID_LIKE.match(b) and any(ch.isdigit() for ch in b) and b in path:
            return b
    if tail and any(ch.isdigit() for ch in tail):
        return re.sub(r"-\d{1,2}$", "", tail) if re.search(r"\d-\d{1,2}$", tail) else tail
    return path or str(posting.get("title") or "")


def _location_from_path(path: str) -> str:
    """'/job/Remote---Georgia/Title_R-1' -> 'Remote - Georgia' (used when the list says 'N Locations')."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "job":
        seg = parts[1].replace("---", " - ").replace("--", ", ").replace("-", " ")
        return re.sub(r"\s+", " ", seg).strip()
    return ""


def _remote(remote_type: Any, *locations: str) -> bool | None:
    r = remote_from_text(remote_type)
    if r is not None:
        return r
    for loc in locations:
        if loc and remote_from_text(loc) is True:
            return True
    return None


class WorkdaySource(Source):
    name = "workday"
    kind = "board"
    label = "Workday career sites"
    docs_url = "https://www.workday.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = employer.board or ""
        tenant, wd, site = parse_board(board)
        base = base_url(tenant, wd)
        list_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        max_jobs = employer.max_jobs or DEFAULT_MAX_JOBS
        terms = [t for t in (employer.search_terms or []) if t and t.strip()] or [""]
        postings: list[dict] = []
        seen_paths: set[str] = set()
        for term in terms:
            if len(postings) >= max_jobs:
                break
            offset, total = 0, None
            term_paths: set[str] = set()
            while len(postings) < max_jobs:
                data = await self.post_json(
                    list_url,
                    {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": term.strip()},
                )
                page = (data.get("jobPostings") or []) if isinstance(data, dict) else []
                if offset == 0 and isinstance(data, dict):
                    total = data.get("total") or None  # only reliable on the first page
                fresh = 0  # postings not yet seen for this term (guards against tenants ignoring offset)
                for p in page:
                    path = p.get("externalPath") if isinstance(p, dict) else None
                    if not path or path in term_paths:
                        continue
                    term_paths.add(path)
                    fresh += 1
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                    postings.append(p)
                    if len(postings) >= max_jobs:
                        break
                offset += PAGE_SIZE
                if len(page) < PAGE_SIZE or not fresh or (total and offset >= total):
                    break
        jobs = safe_parse(
            postings,
            lambda p: self._parse(p, employer, board, tenant, wd, site),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )
        unique: dict[str, Job] = {}
        for job in jobs:
            unique.setdefault(job.source_id, job)
        return list(unique.values())

    def _parse(self, p: dict, employer: Employer, board: str, tenant: str, wd: str, site: str) -> Job | None:
        path, title = p.get("externalPath") or "", (p.get("title") or "").strip()
        if not path or not title:
            return None
        loc_text = (p.get("locationsText") or "").strip()
        extra: dict[str, Any] = {"board": board, "external_path": path}
        if _N_LOCATIONS.match(loc_text):
            location = _location_from_path(path)
            extra["locations_count"] = int(re.findall(r"\d+", loc_text)[0])
        else:
            location = loc_text
        bullets = [str(b) for b in p.get("bulletFields") or [] if b]
        if bullets:
            extra["bullet_fields"] = bullets
        return Job(
            source=self.name,
            source_id=f"{tenant}:{_req_id(p, path)}",
            title=title,
            company=employer.name,
            url=f"{base_url(tenant, wd)}/{site}{path}",
            location=location,
            remote=_remote(p.get("remoteType"), location, loc_text),
            posted_at=relative_to_iso(p.get("postedOn")),
            employer_key=employer.key,
            extra=extra,
        )

    @staticmethod
    def _detail_url(job: Job) -> str | None:
        extra = job.extra or {}
        board, path = extra.get("board"), extra.get("external_path")
        if board and path:
            try:
                tenant, wd, site = parse_board(board)
            except SourceError:
                return None
            return f"{base_url(tenant, wd)}/wday/cxs/{tenant}/{site}{path}"
        parts = urlsplit(job.url or "")
        m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", parts.hostname or "")
        segs = [s for s in parts.path.split("/") if s]
        if m and len(segs) >= 2:
            if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
                segs = segs[1:]
            site, rest = segs[0], "/".join(segs[1:])
            return f"https://{parts.hostname}/wday/cxs/{m.group(1)}/{site}/{rest}"
        return None

    async def enrich(self, job: Job) -> Job:
        """Fetch the posting detail: description, all locations, time type, dates, remote type."""
        url = self._detail_url(job)
        if not url:
            return job
        try:
            data = await self.get_json(url)
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("workday enrich %s failed: %s", job.uid, exc)
            return job
        try:
            self._apply_detail(job, data)
        except Exception as exc:  # noqa: BLE001 - a malformed detail payload must not lose the job
            self.ctx.log.warning("workday enrich %s: malformed detail (%s)", job.uid, exc)
        return job

    @staticmethod
    def _apply_detail(job: Job, data: Any) -> None:
        info = data.get("jobPostingInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return
        job.description = html_to_text(info.get("jobDescription")) or job.description
        extra_locs = info.get("additionalLocations") or []
        location = join_locations(
            [info.get("location"), *(extra_locs if isinstance(extra_locs, list) else [])]
        )
        if location:
            job.location = location
        if info.get("title"):
            job.title = str(info["title"]).strip()
        time_type = info.get("timeType")
        if time_type:
            job.employment_type = normalize_employment_type(time_type)
        job.posted_at = (
            iso_date(info.get("startDate")) or job.posted_at or relative_to_iso(info.get("postedOn"))
        )
        job.closes_at = iso_date(info.get("endDate")) or job.closes_at
        remote = _remote(info.get("remoteType"), job.location)
        if remote is not None:
            job.remote = remote
        org = ((data.get("hiringOrganization") or {}).get("name") or "").strip()
        country = ((info.get("country") or {}).get("descriptor") or "").strip()
        for key, value in (
            ("job_req_id", info.get("jobReqId")),
            ("hiring_organization", org),
            ("country", country),
        ):
            if value:
                job.extra[key] = value
        job.tags = list(dict.fromkeys([*job.tags, *structured_tags(time_type)]))
        job.extra["enriched"] = True
