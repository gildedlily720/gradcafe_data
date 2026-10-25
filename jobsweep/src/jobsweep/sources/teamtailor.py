"""Teamtailor career sites (public RSS / JSON Feed).

Endpoints (verified live 2026-10-25 against ``sumsub.teamtailor.com``):

* ``GET https://{board}.teamtailor.com/jobs.rss`` -> RSS 2.0 with one ``<item>`` per job: ``title,
  description`` (escaped HTML, full text), ``pubDate`` (RFC 822), ``link`` (custom careers domain when set),
  ``remoteStatus`` (``none|hybrid|temporary|fully``), ``guid`` (UUID), ``tt:locations/tt:location(tt:name,
  tt:city, tt:country)``, ``tt:department``, ``tt:role`` (namespace ``https://teamtailor.com/locations``).
* ``GET https://{board}.teamtailor.com/jobs.json`` -> JSON Feed 1.1 (``items[{id (same UUID), title, url,
  date_published, content_html, _jobposting (schema.org JobPosting)}]``); used when the RSS feed fails.

Both feeds are public but can be switched off per company, so this adapter is best effort; neither is
paginated (whole board per request). Board = subdomain (``sumsub``), NA-region subdomain (``acme.na``) or
the company's own careers host (``careers.acme.com``). ``source_id`` = job UUID.
Verified employer: Sumsub (``sumsub``, KYC/AML compliance).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, safe_parse, structured_tags
from .jobvite import jsonld_locations

TT_NS = "{https://teamtailor.com/locations}"
_REMOTE = {"fully": True, "hybrid": False, "none": False}


def teamtailor_host(board: str) -> str:
    raw = re.sub(r"^https?://", "", (board or "").strip()).split("/")[0].lower()
    if re.fullmatch(r"[a-z0-9-]+(?:\.na)?", raw):
        return f"{raw}.teamtailor.com"
    return raw


def rfc822_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError):
        return iso_date(value)


def _t(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


class TeamtailorSource(Source):
    name = "teamtailor"
    kind = "board"
    label = "Teamtailor career sites (RSS)"
    docs_url = "https://docs.teamtailor.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        if not (employer.board or "").strip():
            raise SourceError(f"teamtailor: employer {employer.key} has no board")
        host = teamtailor_host(employer.board or "")
        try:
            jobs = await self._from_rss(employer, host)
        except Exception as exc:  # noqa: BLE001 - fall back to the JSON Feed
            self.ctx.log.info("teamtailor %s: RSS failed (%s); trying jobs.json", host, exc)
            jobs = await self._from_json(employer, host)
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    async def _from_rss(self, employer: Employer, host: str) -> list[Job]:
        raw = await self.get_text(
            f"https://{host}/jobs.rss", headers={"Accept": "application/rss+xml, text/xml"}
        )
        try:
            root = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as exc:
            raise SourceError(f"teamtailor {host}: invalid RSS ({exc})") from exc
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise SourceError(f"teamtailor {host}: not an RSS feed")
        items = [{"el": it} for it in channel.findall("item")]
        return safe_parse(
            items,
            lambda it: self._parse_rss(it["el"], employer, host),
            source=self.name,
            board=host,
            logger=self.ctx.log,
        )

    def _parse_rss(self, it: ET.Element, employer: Employer, host: str) -> Job | None:
        guid, title = _t(it, "guid"), _t(it, "title")
        link = _t(it, "link")
        if not title or not (guid or link):
            return None
        locations = []
        for loc in it.findall(f"{TT_NS}locations/{TT_NS}location"):
            city, country, name = _t(loc, f"{TT_NS}city"), _t(loc, f"{TT_NS}country"), _t(loc, f"{TT_NS}name")
            locations.append(", ".join(p for p in (city, country) if p) or name)
        status = _t(it, "remoteStatus").lower()
        department, role = _t(it, f"{TT_NS}department") or None, _t(it, f"{TT_NS}role")
        extra: dict[str, Any] = {"board": host}
        if role:
            extra["role"] = role
        if status:
            extra["remote_status"] = status
        return Job(
            source=self.name,
            source_id=guid or link,
            title=title,
            company=employer.name,
            url=link or f"https://{host}/jobs",
            location=join_locations(locations),
            remote=_REMOTE.get(status),
            description=html_to_text(_t(it, "description")),
            posted_at=rfc822_to_iso(_t(it, "pubDate")),
            department=department,
            employer_key=employer.key,
            tags=structured_tags(department, role),
            extra=extra,
        )

    async def _from_json(self, employer: Employer, host: str) -> list[Job]:
        data = await self.get_json(f"https://{host}/jobs.json")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceError(f"teamtailor {host}: neither jobs.rss nor jobs.json is available")
        return safe_parse(
            data["items"],
            lambda i: self._parse_json(i, employer, host),
            source=self.name,
            board=host,
            logger=self.ctx.log,
        )

    def _parse_json(self, i: dict, employer: Employer, host: str) -> Job | None:
        jid, title = i.get("id"), (i.get("title") or "").strip()
        if not jid or not title:
            return None
        posting = i.get("_jobposting") if isinstance(i.get("_jobposting"), dict) else {}
        emp = posting.get("employmentType")
        loc_type = str(posting.get("jobLocationType") or "").upper()
        return Job(
            source=self.name,
            source_id=str(jid),
            title=title,
            company=employer.name,
            url=i.get("url") or f"https://{host}/jobs",
            location=jsonld_locations(posting) if posting else "",
            remote=True if loc_type == "TELECOMMUTE" else None,
            description=html_to_text(i.get("content_html") or posting.get("description")),
            posted_at=iso_date(i.get("date_published")) or iso_date(posting.get("datePosted")),
            closes_at=iso_date(posting.get("validThrough")),
            employment_type=normalize_employment_type(emp) if emp else None,
            employer_key=employer.key,
            tags=structured_tags(emp),
            extra={"board": host},
        )
