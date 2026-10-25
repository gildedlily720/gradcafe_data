"""SmartRecruiters public postings API.

Verified live 2026-10-25:

* list: ``GET https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100&offset=N[&q=term]``
  -> ``{"offset","limit","totalFound","content":[...]}``. Unknown companies answer 200 with
  ``totalFound: 0`` (not 404). The list has no description.
* detail: ``GET .../companies/{board}/postings/{id}`` -> ``jobAd.sections.{companyDescription,
  jobDescription, qualifications, additionalInformation}.text`` (HTML), ``postingUrl``, ``applyUrl``.
* public URL: ``https://jobs.smartrecruiters.com/{board}/{id}``.

Honours ``employer.search_terms`` (one ``q=`` search per term, de-duplicated) and ``employer.max_jobs``
(default 1000).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, plain_text, safe_parse, structured_tags

API = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100
DEFAULT_MAX_JOBS = 1000
SECTION_ORDER = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


def _location(loc: dict) -> str:
    full = (loc.get("fullLocation") or "").strip()
    parts = (
        full.split(",") if full else [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
    )
    out: list[str] = []
    for p in parts:  # "United States, UNITED STATES, United States" -> "United States"
        p = str(p or "").strip()
        if p and p.lower() not in (o.lower() for o in out):
            out.append(p)
    return join_locations([", ".join(out)])


def _remote(loc: dict) -> bool | None:
    if loc.get("remote") is True:
        return True
    if loc.get("hybrid") is True or loc.get("remote") is False:
        return False
    return None


def _label(obj: Any) -> str | None:
    if isinstance(obj, dict):
        label = obj.get("label") or obj.get("id")
        return str(label) if label else None
    return str(obj) if obj else None


class SmartRecruitersSource(Source):
    name = "smartrecruiters"
    kind = "board"
    label = "SmartRecruiters career sites"
    docs_url = "https://developers.smartrecruiters.com/docs/posting-api"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip()
        if not board:
            raise SourceError(f"smartrecruiters: employer {employer.key} has no company identifier")
        max_jobs = employer.max_jobs or DEFAULT_MAX_JOBS
        terms = [t.strip() for t in (employer.search_terms or []) if t and t.strip()] or [""]
        postings: list[dict] = []
        seen: set[str] = set()
        for term in terms:
            offset = 0
            while len(postings) < max_jobs:
                params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": offset}
                if term:
                    params["q"] = term
                data = await self.get_json(f"{API}/{quote(board)}/postings", params=params)
                if not isinstance(data, dict):
                    raise SourceError(f"smartrecruiters {board}: unexpected payload {type(data).__name__}")
                page = data.get("content") or []
                for p in page:
                    pid = str(p.get("id") or "") if isinstance(p, dict) else ""
                    if pid and pid not in seen:
                        seen.add(pid)
                        postings.append(p)
                        if len(postings) >= max_jobs:
                            break
                offset += PAGE_SIZE
                total = data.get("totalFound") or 0
                if len(page) < PAGE_SIZE or offset >= total:
                    break
            if len(postings) >= max_jobs:
                break
        return safe_parse(
            postings,
            lambda p: self._parse(p, employer, board),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )

    def _parse(self, p: dict, employer: Employer, board: str) -> Job | None:
        pid, title = p.get("id"), (p.get("name") or "").strip()
        if not pid or not title:
            return None
        loc = p.get("location") or {}
        type_label = _label(p.get("typeOfEmployment"))
        exp = p.get("experienceLevel") or {}
        exp_label = _label(exp)
        seniority = (
            None if (exp.get("id") if isinstance(exp, dict) else None) == "not_applicable" else exp_label
        )
        extra: dict[str, Any] = {"board": board}
        for key, value in (
            ("ref_number", p.get("refNumber")),
            ("function", _label(p.get("function"))),
            ("industry", _label(p.get("industry"))),
            (
                "language",
                (p.get("language") or {}).get("code") if isinstance(p.get("language"), dict) else None,
            ),
            ("country", (loc.get("country") or "").upper() or None),
        ):
            if value:
                extra[key] = value
        return Job(
            source=self.name,
            source_id=str(pid),
            title=title,
            company=employer.name,
            url=f"https://jobs.smartrecruiters.com/{quote(board)}/{pid}",
            location=_location(loc),
            remote=_remote(loc),
            posted_at=iso_date(p.get("releasedDate")),
            employment_type=normalize_employment_type(type_label),
            department=_label(p.get("department")),
            seniority=seniority,
            employer_key=employer.key,
            tags=structured_tags(
                type_label, exp_label, (exp or {}).get("id") if isinstance(exp, dict) else None
            ),
            extra=extra,
        )

    @staticmethod
    def _board_of(job: Job) -> str | None:
        board = (job.extra or {}).get("board")
        if board:
            return str(board)
        parts = urlsplit(job.url or "")
        if parts.hostname and parts.hostname.endswith("smartrecruiters.com"):
            segs = [s for s in parts.path.split("/") if s]
            return segs[0] if segs else None
        return None

    async def enrich(self, job: Job) -> Job:
        """Fetch the posting detail for the job ad text (the list endpoint has none)."""
        board = self._board_of(job)
        if not board:
            return job
        try:
            d = await self.get_json(f"{API}/{quote(board)}/postings/{job.source_id}")
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("smartrecruiters enrich %s failed: %s", job.uid, exc)
            return job
        if not isinstance(d, dict):
            return job
        sections = ((d.get("jobAd") or {}).get("sections") or {}) if isinstance(d.get("jobAd"), dict) else {}
        chunks: list[str] = []
        names = [n for n in SECTION_ORDER if n in sections] + [n for n in sections if n not in SECTION_ORDER]
        for name in names:
            sec = sections.get(name) or {}
            body = html_to_text(sec.get("text"), limit=0) if isinstance(sec, dict) else ""
            if body:
                heading = (sec.get("title") or "").strip()
                chunks.append(f"{heading}\n{body}" if heading else body)
        if chunks:
            job.description = plain_text("\n\n".join(chunks))
        for key, field in (("posting_url", "postingUrl"), ("apply_url", "applyUrl")):
            if d.get(field):
                job.extra[key] = d[field]
        job.extra["enriched"] = True
        return job
