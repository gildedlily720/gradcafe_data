"""ADP Workforce Now career centers (public "MDF" recruitment API).

API (verified live 2026-10-25):

* list: ``GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/
  job-requisitions?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US&$top=20&$skip=N`` -> ``{"jobRequisitions":
  [{itemID, requisitionTitle, postDate, requisitionLocations[{address{cityName, countrySubdivisionLevel1
  {codeValue}}, nameCode{shortName}}], workLevelCode{shortName}, customFieldGroup{stringFields[
  ExternalJobID, HomeDepartment], indicatorFields[InternalPostingFlag], dateFields[PostingDate]}}],
  "meta": {"totalNumber": n}}``. ``$top`` is capped at 20 by the service and ``$skip`` is **1-based**
  (``$skip=0`` returns one row fewer), so pages are requested at ``$skip=1, 21, 41, …``.
* detail: ``GET …/job-requisitions/{itemID}?cid={cid}&lang=en_US`` -> same record plus
  ``requisitionDescription`` (HTML); used by ``enrich``.
* No server-side keyword parameter works (``$search``, ``$filter``, ``keyword``, ``q``, ``searchText`` were
  all ignored), so ``employer.search_terms`` are applied client-side to titles: a posting is kept when every
  word of at least one term occurs in its title.

Board = ``cid`` GUID or ``cid|ccId`` (ccId defaults to ``19000101_000001``). Public posting URL:
``https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&ccId={ccId}
&jobId={ExternalJobID}&lang=en_US``. ``source_id`` = ``itemID``. Verified employers: Mintz Group
(``9b1dca57-4bf4-4218-bc17-ece8b0c3c406``, investigations), DAI Global (``5745ed7b-7f8d-47a9-9161-d975aa7f3314``).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, safe_parse, structured_tags

API = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
PUBLIC = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
DEFAULT_CCID = "19000101_000001"
PAGE = 20
DEFAULT_MAX_JOBS = 1000
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def parse_board(board: str) -> tuple[str, str]:
    """``cid`` / ``cid|ccId`` / careers URL -> (cid, ccId)."""
    raw = (board or "").strip()
    cid_part, _, cc = raw.partition("|")
    m = _GUID.search(cid_part)
    if not m:
        raise SourceError(f"adp: board {board!r} has no cid GUID")
    if not cc:
        mc = re.search(r"ccId=([0-9A-Za-z_]+)", raw)
        cc = mc.group(1) if mc else DEFAULT_CCID
    return m.group(0).lower(), cc.strip() or DEFAULT_CCID


def _fields(req: dict, kind: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    group = req.get("customFieldGroup") if isinstance(req, dict) else None
    rows = group.get(kind) if isinstance(group, dict) else None
    for f in rows if isinstance(rows, list) else []:
        if not isinstance(f, dict):
            continue
        name, cat = f.get("nameCode"), f.get("categoryCode")
        code = (name.get("codeValue") if isinstance(name, dict) else None) or (
            cat.get("codeValue") if isinstance(cat, dict) else None
        )
        if code:
            out[code] = next(
                (f[k] for k in ("stringValue", "indicatorValue", "dateValue", "numberValue") if k in f), None
            )
    return out


def _locations(req: dict) -> list[str]:
    out = []
    for loc in req.get("requisitionLocations") or []:
        if not isinstance(loc, dict):
            continue
        short = ((loc.get("nameCode") or {}).get("shortName") or "").strip()
        if short:
            out.append(short)
            continue
        addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
        region = (addr.get("countrySubdivisionLevel1") or {}).get("codeValue")
        country = addr.get("countryCode")
        out.append(", ".join(p for p in (addr.get("cityName"), region, country) if p))
    return out


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if not s or s.lower() in ("null", "none") else s


def title_matches(title: str, terms: list[str]) -> bool:
    low = title.lower()
    for term in terms:
        words = re.findall(r"\w+", term.lower())
        if words and all(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return True
    return False


class ADPSource(Source):
    name = "adp"
    kind = "board"
    label = "ADP Workforce Now career centers"
    docs_url = "https://developers.adp.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        cid, ccid = parse_board(employer.board or "")
        cap = employer.max_jobs or DEFAULT_MAX_JOBS
        terms = [t for t in employer.search_terms if t and t.strip()]
        params = {"cid": cid, "ccId": ccid, "lang": "en_US", "locale": "en_US", "$top": PAGE}
        seen: dict[str, dict] = {}
        skip, total = 1, None
        while True:
            data = await self.get_json(API, params={**params, "$skip": skip})
            if not isinstance(data, dict):
                raise SourceError(f"adp {cid}: unexpected payload {type(data).__name__}")
            reqs = [r for r in data.get("jobRequisitions") or [] if isinstance(r, dict)]
            total = (data.get("meta") or {}).get("totalNumber", total)
            new = 0
            for r in reqs:
                key = str(r.get("itemID") or "")
                if key and key not in seen:
                    seen[key] = r
                    new += 1
            skip += len(reqs) or PAGE
            if not reqs or not new or (isinstance(total, int) and skip > total) or len(seen) >= 5000:
                break
            if not terms and len(seen) >= cap:
                break
        jobs = safe_parse(
            list(seen.values()),
            lambda r: self._parse(r, employer, cid, ccid),
            source=self.name,
            board=cid,
            logger=self.ctx.log,
        )
        if terms:
            jobs = [j for j in jobs if title_matches(j.title, terms)]
        return jobs[:cap]

    def _parse(self, r: dict, employer: Employer, cid: str, ccid: str) -> Job | None:
        item_id, title = _clean(r.get("itemID")), (r.get("requisitionTitle") or "").strip()
        if not item_id or not title:
            return None
        strings, flags = _fields(r, "stringFields"), _fields(r, "indicatorFields")
        if flags.get("InternalPostingFlag") is True:
            return None
        ext_id = _clean(strings.get("ExternalJobID"))
        locations = _locations(r)
        level = (
            (r.get("workLevelCode") or {}).get("shortName")
            if isinstance(r.get("workLevelCode"), dict)
            else None
        )
        department = _clean(strings.get("HomeDepartment"))
        url = (
            PUBLIC + "?" + urlencode({"cid": cid, "ccId": ccid, "jobId": ext_id or item_id, "lang": "en_US"})
        )
        extra: dict[str, Any] = {"board": cid, "cc_id": ccid}
        if ext_id:
            extra["external_job_id"] = ext_id
        if _clean(r.get("clientRequisitionID")):
            extra["requisition_id"] = r["clientRequisitionID"]
        remote = True if locations and all(remote_from_text(loc) is True for loc in locations) else None
        return Job(
            source=self.name,
            source_id=item_id,
            title=title,
            company=employer.name,
            url=url,
            location=join_locations(locations),
            remote=remote,
            description=html_to_text(r.get("requisitionDescription")),
            posted_at=iso_date(r.get("postDate")) or iso_date(_fields(r, "dateFields").get("PostingDate")),
            employment_type=normalize_employment_type(level) if level else None,
            department=department,
            employer_key=employer.key,
            tags=structured_tags(level, department),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Fetch the requisition detail for ``requisitionDescription``."""
        cid = (job.extra or {}).get("board")
        if not cid or job.description:
            return job
        try:
            r = await self.get_json(
                f"{API}/{job.source_id}", params={"cid": cid, "lang": "en_US", "locale": "en_US"}
            )
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("adp enrich %s failed: %s", job.uid, exc)
            return job
        if not isinstance(r, dict):
            return job
        job.description = html_to_text(r.get("requisitionDescription")) or job.description
        level = (
            (r.get("workLevelCode") or {}).get("shortName")
            if isinstance(r.get("workLevelCode"), dict)
            else None
        )
        if level and not job.employment_type:
            job.employment_type = normalize_employment_type(level)
        if not job.department:
            job.department = _clean(_fields(r, "stringFields").get("HomeDepartment"))
        return job
