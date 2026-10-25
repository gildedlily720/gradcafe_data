"""Import jobs found elsewhere (claude.ai connectors, spreadsheets, scraper exports) as ``Job`` objects.

``ImportSource.parse(payload, origin="claude-connector", fmt=None)`` accepts (auto-detected when ``fmt`` is
None; ``fmt`` may be ``json``, ``jsonl``, ``csv``, ``tsv`` or ``md``/``markdown``/``text``):

* **JSON** — a list of job objects; a dict wrapping one (``jobs``/``items``/``results``/``data``/
  ``topMatches``/``postings``/...); a single job object; JSON lines; or a raw MCP tool result
  (``{"content": [{"type": "text", "text": ...}]}``). Real samples captured 2026-10-25 live in
  ``tests/fixtures/import/``: Workopia ``job_tool`` (``{"items": [...]}``) and Jobs & Careers ``job-search``
  (``{"topMatches": [...]}``).
* **CSV/TSV** text with a header row.
* **Markdown / text**:
  - Indeed-connector blocks of ``**Field:** value`` lines (Job Title, Job Id, Company, Location, Posted on,
    Job Type, Compensation, View Job URL), one block per job;
  - numbered lists ``N. **[Title](url)** — Company · Location · Job Type · Compensation · Posted MMM DD``
    (how Claude renders Workopia / Jobs & Careers results);
  - markdown tables with a header row.

Field names are matched case/punctuation-insensitively against alias lists (``jobTitle`` = ``job_title`` =
``Job Title``). ``N/A``-style values become None. ``source="import"``,
``source_id=f"{origin}:{id or sha1(url|title|company)[:12]}"``, ``tags=[origin]``.

This module also hosts the record-mapping helpers shared by the aggregator adapters (apify, jobspy,
hasdata_indeed, theirstack, adzuna): ``job_from_record``, ``normalize_employment_type``, ``normalize_period``,
``remote_from_value``, ``parse_date``, ``salary_from_text``, ``md_to_text``, ``clean_description``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..models import Job
from ..text import MAX_DESCRIPTION_CHARS, html_to_text, iso_date, parse_salary, relative_to_iso, truncate
from .base import Source

log = logging.getLogger("jobsweep.sources.import")

# ---------------------------------------------------------------------------------------------------------
# field aliases (normalised: lowercase, alphanumerics only)

TITLE_KEYS = ("title", "jobtitle", "position", "positiontitle", "jobname", "postingtitle", "name")
COMPANY_KEYS = (
    "company",
    "companyname",
    "employer",
    "employername",
    "organization",
    "organisation",
    "organizationname",
    "hiringorganization",
    "companydisplayname",
    "advertiser",
)
URL_KEYS = (
    "url",
    "joburl",
    "viewjoburl",
    "link",
    "joblink",
    "applyurl",
    "applylink",
    "applicationurl",
    "redirecturl",
    "jobposturl",
    "postingurl",
    "joburldirect",
    "finalurl",
    "portalurl",
    "sourceurl",
    "href",
)
ID_KEYS = ("id", "jobid", "jobkey", "key", "jk", "uid", "postingid", "requisitionid", "reqid", "externalid")
LOCATION_KEYS = (
    "location",
    "joblocation",
    "formattedlocation",
    "locationname",
    "locations",
    "shortlocation",
    "longlocation",
    "place",
    "city",
)
DESCRIPTION_KEYS = (
    "description",
    "jobdescription",
    "descriptiontext",
    "descriptionmarkdown",
    "fulldescription",
    "descriptionhtml",
    "snippet",
    "summary",
    "body",
)
BULLET_KEYS = ("highlights", "keyrequirements", "requirementssummary", "requirements", "responsibilities")
POSTED_KEYS = (
    "posted",
    "postedat",
    "dateposted",
    "postedon",
    "posteddate",
    "postingdate",
    "isodate",
    "publishedat",
    "published",
    "publicationdate",
    "datepublished",
    "created",
    "createdat",
    "datecreated",
    "listeddate",
    "pubdate",
    "date",
)
CLOSES_KEYS = (
    "closesat",
    "closingdate",
    "closedate",
    "deadline",
    "applicationdeadline",
    "applyby",
    "validthrough",
    "expiresat",
    "expirationdate",
)
SALARY_KEYS = (
    "salary",
    "compensation",
    "pay",
    "salaryrange",
    "salarystring",
    "salarytext",
    "payrange",
    "basesalary",
    "wage",
)
SALARY_MIN_KEYS = ("salarymin", "minsalary", "minamount", "salaryfrom", "minimumsalary", "payfrom", "paymin")
SALARY_MAX_KEYS = ("salarymax", "maxsalary", "maxamount", "salaryto", "maximumsalary", "payto", "paymax")
CURRENCY_KEYS = ("salarycurrency", "currency", "currencycode", "paycurrency")
PERIOD_KEYS = ("salaryperiod", "salaryfrequency", "salaryinterval", "interval", "payperiod", "salaryunit")
EMPLOYMENT_KEYS = (
    "employmenttype",
    "jobtype",
    "jobtypes",
    "contracttype",
    "contracttime",
    "commitment",
    "commitmenttypes",
    "employmentstatus",
    "employmentstatuses",
    "worktype",
    "schedule",
    "type",
)
REMOTE_KEYS = (
    "remote",
    "isremote",
    "workmode",
    "workplacetype",
    "workplace",
    "remotetype",
    "worksetting",
    "workarrangement",
    "workfromhometype",
    "locationtype",
)
SENIORITY_KEYS = ("seniority", "senioritylevel", "joblevel", "experiencelevel", "careerlevel", "level")
DEPARTMENT_KEYS = ("department", "jobcategory", "category", "jobfunction", "function", "team")

NA_VALUES = {
    "",
    "n/a",
    "na",
    "n.a.",
    "none",
    "null",
    "nil",
    "nan",
    "nat",
    "undefined",
    "-",
    "--",
    "—",
    "–",
    "not specified",
    "not available",
    "not disclosed",
    "unknown",
}
_CONTAINER_KEYS = (
    "jobs",
    "items",
    "results",
    "data",
    "topmatches",
    "jobpostings",
    "postings",
    "listings",
    "jobresults",
    "hits",
    "records",
    "rows",
    "matches",
    "jobcards",
    "elements",
    "edges",
    "nodes",
)
_WRAPPER_KEYS = ("card", "job", "node", "posting", "item", "jobposting")


# ---------------------------------------------------------------------------------------------------------
# small value helpers


def norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def clean(value: Any) -> Any:
    """Strip strings and turn N/A-style placeholders, NaN and NaT into None."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return None if s.lower() in NA_VALUES else s
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if value != value:  # pandas NaT / numpy NaN
            return None
    except (TypeError, ValueError):
        pass
    return value


def normalize_record(rec: Mapping[str, Any]) -> dict[str, Any]:
    """``{normalised key: value}``; the first non-empty value wins when keys collide."""
    out: dict[str, Any] = {}
    for k, v in rec.items():
        nk = norm_key(k)
        if nk and (nk not in out or _empty(out[nk])):
            out[nk] = v
    return out


def _empty(value: Any) -> bool:
    value = clean(value)
    return value is None or (isinstance(value, (list, tuple, dict, set)) and not value)


def pick(nrec: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """First non-empty value among ``keys`` (already normalised) in a normalised record."""
    for k in keys:
        if k in nrec and not _empty(nrec[k]):
            v = nrec[k]
            return v.strip() if isinstance(v, str) else v
    return None


_MD_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url><[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")


def strip_md(text: str) -> str:
    """Inline markdown -> text: links keep their label, emphasis markers are dropped."""
    s = _MD_LINK.sub(lambda m: m.group("text"), text)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = s.replace("**", "")
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", s)
    return s.strip().strip("*").strip()


def text_value(value: Any, sep: str = ", ") -> str | None:
    """Scalar/list/dict -> display string (dicts: their name-ish field)."""
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, Mapping):
        n = normalize_record(value)
        return text_value(
            pick(n, ("displayname", "name", "label", "title", "text", "value", "companyname", "fullname"))
        )
    if isinstance(value, (list, tuple, set)):
        parts = [text_value(v) for v in value]
        out: list[str] = []
        for p in parts:
            if p and p not in out:
                out.append(p)
        return sep.join(out) or None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = strip_md(str(value))
    return clean(s)


def url_value(value: Any) -> str | None:
    """Pull an http(s) URL out of a plain value, ``<url>`` or a markdown link."""
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, Mapping):
        return url_value(pick(normalize_record(value), ("url", "href", "link")))
    if isinstance(value, (list, tuple)):
        for v in value:
            u = url_value(v)
            if u:
                return u
        return None
    s = str(value).strip()
    m = _MD_LINK.search(s)
    if m:
        s = m.group("url")
    s = s.strip("<>").strip()
    m = re.search(r"https?://[^\s<>\"')\]]+", s)
    return m.group(0) if m else None


def to_float(value: Any) -> float | None:
    value = clean(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    try:
        x = float(s)
    except ValueError:
        return None
    return None if math.isnan(x) else x


def short_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:12]


def slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# ---------------------------------------------------------------------------------------------------------
# normalisers

_EMPLOYMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internship", re.compile(r"\bintern(ship)?s?\b|\bco[\s_-]?op\b|\bwork[\s_-]?study\b", re.I)),
    ("fellowship", re.compile(r"\bfellow(ship)?s?\b", re.I)),
    ("per-diem", re.compile(r"per[\s_-]?diem|\bprn\b", re.I)),
    ("part-time", re.compile(r"part[\s_-]?time", re.I)),
    ("temporary", re.compile(r"\btemp(orary)?\b|seasonal|fixed[\s_-]?term|\bcasual\b", re.I)),
    ("contract", re.compile(r"\bcontract(or|ual)?\b|freelance|\bc2h\b|\b1099\b", re.I)),
    ("volunteer", re.compile(r"volunteer", re.I)),
    ("full-time", re.compile(r"full[\s_-]?time|\bpermanent\b|\bregular\b", re.I)),
    ("other", re.compile(r"^\s*other\s*$|apprentice|co[\s_-]?founder", re.I)),
)


def normalize_employment_type(value: Any) -> str | None:
    """Map source vocab (``FULL_TIME``, ``fulltime``, ``Full Time``, ``["part_time"]``...) to the contract set."""
    value = clean(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        value = list(value.values())
    if isinstance(value, (list, tuple, set)):
        for v in value:
            got = normalize_employment_type(v)
            if got:
                return got
        return None
    s = str(value)
    for name, rx in _EMPLOYMENT_PATTERNS:
        if rx.search(s):
            return name
    return None


def normalize_period(value: Any) -> str | None:
    """``YEARLY``/``annual``/``per hour``/``hourly``... -> ``year``/``hour``/``month``/``week``/``day``."""
    value = clean(value)
    if value is None:
        return None
    s = str(value).lower()
    if re.search(r"biweek|bi-week|fortnight|quarter", s):
        return None
    if re.search(r"hour|\bhr\b|/h\b", s):
        return "hour"
    if re.search(r"\bday\b|daily|per diem", s):
        return "day"
    if re.search(r"week", s):
        return "week"
    if re.search(r"month", s):
        return "month"
    if re.search(r"year|annual|annum|\byr\b|\bpa\b", s):
        return "year"
    return None


_HYBRID_OR_ONSITE = re.compile(
    r"hybrid|on[\s_-]?site|in[\s_-]?office|in[\s_-]?person|office[\s_-]?based|\bon[\s_-]?premises?\b", re.I
)
_REMOTE = re.compile(r"\bremote|work from home|\bwfh\b|telecommut|anywhere", re.I)


def remote_from_value(value: Any) -> bool | None:
    """True for remote, False for hybrid/on-site, None when unknown. A bare ``False`` flag stays None
    (most sources default it), only words saying hybrid/on-site give False."""
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return True if value else None
    if isinstance(value, (list, tuple, set)):
        found = [remote_from_value(v) for v in value]
        if True in found:
            return True
        return False if False in found else None
    s = str(value).strip()
    if _HYBRID_OR_ONSITE.search(s):
        return False
    if _REMOTE.search(s):
        return True
    if s.lower() in ("yes", "true", "y", "1", "fully_remote"):
        return True
    return None


def location_remote(location: str | None) -> bool | None:
    if not location:
        return None
    if _REMOTE.search(location):
        return False if _HYBRID_OR_ONSITE.search(location) else True
    return None


_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}


def currency_code(value: Any) -> str | None:
    value = clean(value)
    if value is None:
        return None
    s = str(value).strip()
    if s in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[s]
    s = s.upper()
    return s if re.fullmatch(r"[A-Z]{3}", s) else None


def salary_from_text(text: Any) -> tuple[float | None, float | None, str | None, str | None]:
    """``text.parse_salary`` after normalising ``US$``/``USD 60,000``/``GBP``/``EUR`` prefixes."""
    s = text_value(text)
    if not s:
        return None, None, None, None
    cur_hint = None
    m = re.search(r"\b(USD|GBP|EUR)\b", s, re.I)
    if m:
        cur_hint = m.group(1).upper()
    s = re.sub(r"\b(?:US|USD|C|CA|AU|A|NZ)\s?\$", "$", s)
    s = re.sub(r"\bUSD\s?(?=\d)", "$", s, flags=re.I)
    s = re.sub(r"\bGBP\s?(?=\d)", "£", s, flags=re.I)
    s = re.sub(r"\bEUR\s?(?=\d)", "€", s, flags=re.I)
    s = re.sub(r"(?<=\d)\s?(?:USD|GBP|EUR)\b", "", s, flags=re.I)
    # text.parse_salary stops at cents after thousands ("$85,000.00 - $150,000.00" -> 85000 only)
    s = re.sub(r"(\d{1,3}(?:,\d{3})+)\.\d{1,2}\b", r"\1", s)
    lo, hi, cur, period = parse_salary(s)
    if lo is None:
        return None, None, None, None
    tail_period = normalize_period(s)
    return lo, hi, cur or cur_hint, period or tail_period


def infer_period(lo: float | None, hi: float | None) -> str | None:
    top = max(x for x in (lo, hi, 0.0) if x is not None)
    if not top:
        return None
    if top >= 15000:
        return "year"
    if top <= 300:
        return "hour"
    return None


_MONTH_DAY = re.compile(
    r"^(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?$"
)
_DAY_MONTH = re.compile(r"^(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3,9})\.?,?\s+(?P<year>\d{4})$")
_POSTED_PREFIX = re.compile(
    r"^\s*(?:date\s+)?(?:posted|published|listed|active|employer\s+active|updated)\s*(?:on|at)?\s*:?\s*",
    re.I,
)


def _month_number(name: str) -> int | None:
    for fmt in ("%b", "%B"):
        try:
            return datetime.strptime(name[:3] if fmt == "%b" else name, fmt).month
        except ValueError:
            continue
    return None


def parse_date(value: Any, now: datetime | None = None) -> str | None:
    """ISO strings, epochs, ``July 31, 2026``, ``Posted Oct 21``, ``3 days ago``, ``Just posted`` -> ISO."""
    value = clean(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return iso_date(value)
    raw = str(value).strip()
    s = _POSTED_PREFIX.sub("", raw).strip()
    got = iso_date(s)
    if got:
        return got
    now = now or datetime.now(timezone.utc)
    if re.search(r"just posted|^new$|^today$|hours? ago|minutes? ago", s, re.I):
        return now.date().isoformat()
    got = relative_to_iso(raw, now=now)
    if got:
        return got
    for rx in (_MONTH_DAY, _DAY_MONTH):
        m = rx.match(s)
        if not m:
            continue
        month = _month_number(m.group("mon"))
        if not month:
            continue
        year = int(m.group("year")) if m.group("year") else now.year
        try:
            d = date(year, month, int(m.group("day")))
        except ValueError:
            return None
        if not m.group("year") and d > now.date() + timedelta(days=2):
            d = d.replace(year=year - 1)
        return d.isoformat()
    return None


_HTML_HINT = re.compile(r"<\s*/?\s*(p|div|br|li|ul|ol|h[1-6]|span|strong|b|em|i|table|tr|td|a)\b[^>]*>", re.I)


def md_to_text(raw: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Light markdown -> plain text (keeps line structure and bullets)."""
    if not raw:
        return ""
    s = str(raw).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = _MD_LINK.sub(lambda m: m.group("text"), s)
    s = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*", "", s)
    s = re.sub(r"(?m)^[ \t]*(?:[-=_*][ \t]*){3,}$", "", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s, flags=re.S)
    s = s.replace("**", "")
    s = re.sub(r"(?m)^([ \t]*)[*+•][ \t]+", r"\1- ", s)
    s = re.sub(r"(?<![\w*\\])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", s)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", s)
    s = s.replace("```", "").replace("`", "")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return truncate(s, limit) if limit else s


def clean_description(raw: Any, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """HTML (incl. escaped) or markdown/plain text -> plain text."""
    raw = clean(raw)
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        raw = "\n".join(f"- {text_value(x)}" for x in raw if text_value(x))
    s = str(raw)
    if _HTML_HINT.search(s) or "&lt;" in s[:500]:
        return html_to_text(s, limit=limit)
    return md_to_text(s, limit=limit)


def _location_value(value: Any) -> str | None:
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, Mapping):
        n = normalize_record(value)
        name = text_value(pick(n, ("displayname", "formatted", "name", "label", "text", "fulladdress")))
        if name:
            return name
        parts = [
            text_value(pick(n, (k,)))
            for k in ("city", "addresslocality", "state", "region", "addressregion", "country")
        ]
        return ", ".join(p for p in parts if p) or None
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for v in value:
            loc = _location_value(v)
            if loc and loc not in out:
                out.append(loc)
        return "; ".join(out) or None
    return text_value(value)


# ---------------------------------------------------------------------------------------------------------
# record -> Job


def job_from_record(
    rec: Mapping[str, Any],
    *,
    source: str,
    id_prefix: str | None,
    tags: Iterable[str] = (),
    now: datetime | None = None,
) -> Job | None:
    """Map one loosely structured job dict to a ``Job`` (None when it has no title).

    ``source_id`` = ``f"{id_prefix}:{id}"`` (or ``id`` alone when ``id_prefix`` is falsy); records without an
    id get ``sha1(url|title|company)[:12]``.
    """
    n = normalize_record(rec)
    raw_title = pick(n, TITLE_KEYS)
    if isinstance(raw_title, (Mapping, list, tuple)):
        raw_title = text_value(raw_title)
    title = text_value(raw_title)
    if not title:
        return None
    url = url_value(pick(n, URL_KEYS))
    if not url and isinstance(raw_title, str):
        url = url_value(raw_title) if _MD_LINK.search(raw_title) else None
    company = text_value(pick(n, COMPANY_KEYS)) or ""
    rid = text_value(pick(n, ID_KEYS))
    ident = rid or short_hash(url, title, company)
    source_id = f"{id_prefix}:{ident}" if id_prefix else ident

    location = _location_value(pick(n, LOCATION_KEYS))
    if not location:
        parts = [text_value(n.get(k)) for k in ("state", "region", "country")]
        location = ", ".join(p for p in parts if p) or ""

    extra: dict[str, Any] = {}
    # salary: numeric fields first, then a salary dict, then free text
    lo, hi = to_float(pick(n, SALARY_MIN_KEYS)), to_float(pick(n, SALARY_MAX_KEYS))
    cur = currency_code(pick(n, CURRENCY_KEYS))
    period = normalize_period(pick(n, PERIOD_KEYS))
    sal = pick(n, SALARY_KEYS)
    if isinstance(sal, Mapping):
        sn = normalize_record(sal)
        lo = lo if lo is not None else to_float(pick(sn, ("min", "minimum", "from", "minvalue", "low")))
        hi = hi if hi is not None else to_float(pick(sn, ("max", "maximum", "to", "maxvalue", "high")))
        cur = cur or currency_code(pick(sn, ("currency", "currencycode")))
        period = period or normalize_period(pick(sn, ("type", "period", "interval", "unit", "unittext")))
        text = text_value(pick(sn, ("text", "raw", "display", "salarytext")))
        if lo is None and hi is None and text:
            lo, hi, c, p = salary_from_text(text)
            cur, period = cur or c, period or p
    elif sal is not None and lo is None and hi is None:
        lo, hi, c, p = salary_from_text(sal)
        cur, period = cur or c, period or p
        if lo is None and text_value(sal):
            extra["salary_text"] = text_value(sal)[:200]
    if lo is not None or hi is not None:
        period = period or infer_period(lo, hi)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
    else:
        cur = period = None

    raw_type = pick(n, EMPLOYMENT_KEYS)
    employment_type = normalize_employment_type(raw_type)

    remote = remote_from_value(pick(n, REMOTE_KEYS))
    if remote is None:
        remote = location_remote(location)
    if not location and remote:
        location = "Remote"

    description = clean_description(pick(n, DESCRIPTION_KEYS))
    bullets: list[str] = []
    for key in BULLET_KEYS:
        val = clean(n.get(key))
        if isinstance(val, (list, tuple)):
            bullets.extend(t for t in (text_value(v) for v in val) if t)
        elif val is not None and text_value(val):
            bullets.append(text_value(val))
    extra_lines = [b for b in dict.fromkeys(bullets) if b not in description]
    if extra_lines:
        joined = "\n".join(f"- {b}" for b in extra_lines)
        description = truncate(f"{description}\n\n{joined}".strip(), MAX_DESCRIPTION_CHARS)

    raw_posted = pick(n, POSTED_KEYS)
    posted_at = parse_date(raw_posted, now=now)
    if posted_at is None and text_value(raw_posted):
        extra["posted_text"] = text_value(raw_posted)[:80]
    closes_at = parse_date(pick(n, CLOSES_KEYS), now=now)

    seniority = text_value(pick(n, SENIORITY_KEYS))
    department = text_value(pick(n, DEPARTMENT_KEYS))

    tag_list = [t for t in dict.fromkeys(t for t in tags if t)]
    if employment_type in ("internship", "fellowship") and employment_type not in tag_list:
        tag_list.append(employment_type)
    if seniority and re.search(r"\bintern", seniority, re.I) and "internship" not in tag_list:
        tag_list.append("internship")

    return Job(
        source=source,
        source_id=source_id,
        title=title,
        company=company,
        url=url or "",
        location=location or "",
        remote=remote,
        description=description,
        posted_at=posted_at,
        closes_at=closes_at,
        salary_min=lo,
        salary_max=hi,
        salary_currency=cur,
        salary_period=period,
        employment_type=employment_type,
        department=department,
        seniority=seniority,
        tags=tag_list,
        extra=extra,
    )


# ---------------------------------------------------------------------------------------------------------
# payload -> records

_TITLE_SET = set(TITLE_KEYS)


def _looks_like_job(obj: Any) -> bool:
    if not isinstance(obj, Mapping):
        return False
    for k, v in obj.items():
        if norm_key(k) in _TITLE_SET and isinstance(v, str) and clean(v):
            return True
    return False


def _unwrap(obj: Any) -> Any:
    if isinstance(obj, Mapping) and not _looks_like_job(obj):
        for k, v in obj.items():
            if norm_key(k) in _WRAPPER_KEYS and _looks_like_job(v):
                return v
    return obj


def _records_from_obj(obj: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6 or obj is None:
        return []
    if isinstance(obj, str):
        return _records_from_text(obj, None, depth + 1)
    if isinstance(obj, (list, tuple)):
        out: list[dict[str, Any]] = []
        for item in obj:
            item = _unwrap(item)
            if _looks_like_job(item):
                out.append(dict(item))
            elif isinstance(item, (Mapping, list, tuple)):
                out.extend(_records_from_obj(item, depth + 1))
            elif isinstance(item, str) and len(item) > 20:
                out.extend(_records_from_text(item, None, depth + 1))
        return out
    if isinstance(obj, Mapping):
        # raw MCP tool result: structuredContent first, else the text blocks
        sc = obj.get("structuredContent")
        if isinstance(sc, (Mapping, list)):
            got = _records_from_obj(sc, depth + 1)
            if got:
                return got
        content = obj.get("content")
        if isinstance(content, list) and content and all(isinstance(c, Mapping) for c in content):
            texts = [c.get("text") for c in content if isinstance(c.get("text"), str)]
            if texts:
                return _records_from_text("\n\n".join(texts), None, depth + 1)
        keys = {norm_key(k): k for k in obj}
        for ck in _CONTAINER_KEYS:
            if ck in keys:
                got = _records_from_obj(obj[keys[ck]], depth + 1)
                if got:
                    return got
        if _looks_like_job(_unwrap(obj)):
            return [dict(_unwrap(obj))]
        for v in obj.values():
            if isinstance(v, (list, Mapping)):
                got = _records_from_obj(v, depth + 1)
                if got:
                    return got
    return []


def detect_format(text: str) -> str:
    """Best guess of ``json`` / ``csv`` / ``md`` for a text payload."""
    s = text.lstrip("﻿").strip()
    if s[:1] in ("[", "{"):
        try:
            json.loads(s)
            return "json"
        except ValueError:
            lines = [ln for ln in s.splitlines() if ln.strip()]
            if lines and all(ln.lstrip().startswith("{") for ln in lines):
                return "jsonl"
    if _looks_markdown(s):
        return "md"
    if _csv_delimiter(s):
        return "csv"
    return "md"


_MD_HINT = re.compile(
    r"\*\*[^*\n]{1,40}:\*\*|\*\*[^*\n]{1,40}\*\*\s*:|^\s*\d{1,3}[.)]\s+(?:\*\*|\[)|^\s*\|.*\|\s*$|^#{1,6}\s",
    re.M,
)


def _looks_markdown(s: str) -> bool:
    return bool(_MD_HINT.search(s[:5000]))


def _csv_delimiter(s: str) -> str | None:
    """Delimiter whose header row names a title column (``title``, ``Job Title``, ``position``...)."""
    first = s.splitlines()[0] if s else ""
    for delim in (",", "\t", ";", "|"):
        if first.count(delim) < 1:
            continue
        header = next(csv.reader([first], delimiter=delim))
        if len(header) >= 2 and any(norm_key(h) in _TITLE_SET for h in header):
            return delim
    return None


def _records_from_csv(text: str, delimiter: str | None = None) -> list[dict[str, Any]]:
    s = text.lstrip("﻿")
    delim = delimiter or _csv_delimiter(s) or ","
    reader = csv.DictReader(io.StringIO(s), delimiter=delim)
    rows = []
    for row in reader:
        rec = {k.strip(): v for k, v in row.items() if k}
        if any(clean(v) for v in rec.values()):
            rows.append(rec)
    return rows


def _records_from_jsonl(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.extend(_records_from_obj(json.loads(line)))
        except ValueError:
            continue
    return out


# ---- markdown -------------------------------------------------------------------------------------------

_FIELD_LINE = re.compile(
    r"^\s*(?:[-*•+]\s+|\d{1,3}[.)]\s+)?\*\*(?P<label>[^*\n]{1,48}?)(?::\*\*|\*\*\s*:)\s*(?P<value>.*?)\s*$"
)
_PLAIN_FIELD_LINE = re.compile(r"^\s*(?:[-*•+]\s+)?(?P<label>[A-Za-z][A-Za-z /_-]{1,30}):\s+(?P<value>\S.*?)\s*$")
_LIST_PREFIX = re.compile(r"^\s*(?:(?P<num>\d{1,3})[.)]|(?P<bullet>[-*•+])|(?P<head>#{1,6}))\s+(?P<body>.+?)\s*$")
_BOLD_LINE = re.compile(r"^\s*\*\*(?P<body>.+)\*\*\s*$")
_TITLE_LINK = re.compile(
    r"^\**\s*\[(?P<title>[^\]]+)\]\((?P<url><[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)\s*\**"
)
_TITLE_BOLD = re.compile(r"^\*\*(?P<title>[^*]+?)\*\*")
_SEP_LEAD = re.compile(r"^\s*(?:[—–|·•:]|-(?=\s))\s*")
_PART_SPLIT = re.compile(r"\s+[·•|]\s+|\s+[—–]\s+")
_DASH_SPLIT = re.compile(r"\s+[-—–]\s+")
_PAY_HINT = re.compile(
    r"[$£€]\s?\d|\b\d[\d,.]*\s?[kK]\b|\b(?:usd|gbp|eur)\s?\d|\bper\s+(?:year|hour|annum)|/\s?(?:hr|yr|hour|year)\b",
    re.I,
)
_POSTED_HINT = re.compile(r"^(?:posted|published|listed)\b|^(?:just posted|today|yesterday)$|\bago$", re.I)
_TITLE_FIELDS = set(TITLE_KEYS)
# labels the plain "Label: value" form may use (keeps prose like "Note: ..." out of records)
_PLAIN_LABELS = (
    set(TITLE_KEYS)
    | set(COMPANY_KEYS)
    | set(URL_KEYS)
    | set(ID_KEYS)
    | set(LOCATION_KEYS)
    | set(POSTED_KEYS)
    | set(SALARY_KEYS)
    | set(EMPLOYMENT_KEYS)
) - {"name", "type", "key", "date", "link", "place", "body"}


def _title_line(line: str) -> dict[str, Any] | None:
    """A list item / heading / bold line that starts a new job, e.g. the Workopia numbered format."""
    m = _LIST_PREFIX.match(line)
    head = False
    if m:
        body, head = m.group("body"), bool(m.group("head"))
    else:
        b = _BOLD_LINE.match(line)
        if b:
            body = line.strip()
        elif _TITLE_LINK.match(line.strip()):
            body = line.strip()
        else:
            return None
    body = body.strip()
    if head:
        body = re.sub(r"^\d{1,3}[.)]\s+", "", body)
    rec: dict[str, Any] = {}
    tm = _TITLE_LINK.match(body)
    if tm:
        rec["title"] = strip_md(tm.group("title"))
        rec["url"] = tm.group("url").strip("<>")
        rest = body[tm.end() :]
    else:
        bm = _TITLE_BOLD.match(body)
        if bm:
            title = bm.group("title").strip()
            if title.endswith(":"):  # "**Label:** value" lines are fields, not titles
                return None
            rec["title"] = strip_md(title)
            rest = body[bm.end() :]
        elif head:
            rec["title"] = strip_md(body)
            rest = ""
        else:
            # plain "Title — Company · ..." items; a bare " - " only counts in numbered lists
            parts = re.split(r"\s+[—–]\s+", body, maxsplit=1)
            if len(parts) < 2:
                parts = re.split(r"\s+[·•|]\s+", body, maxsplit=1)
            if len(parts) < 2 and m is not None and m.group("num"):
                parts = _DASH_SPLIT.split(body, maxsplit=1)
            if len(parts) < 2:
                return None
            rec["title"] = strip_md(parts[0])
            rest = " — " + parts[1]
    if not rec.get("title"):
        return None
    rest = _SEP_LEAD.sub("", rest.strip())
    if rest:
        parts = [strip_md(p) for p in _PART_SPLIT.split(rest) if strip_md(p)]
        if len(parts) == 1 and _DASH_SPLIT.search(parts[0]):
            parts = [strip_md(p) for p in _DASH_SPLIT.split(parts[0]) if strip_md(p)]
        _classify_parts(rec, parts)
    return rec


def _classify_parts(rec: dict[str, Any], parts: list[str]) -> None:
    for i, p in enumerate(parts):
        jid = re.match(r"(?i)^job\s*id\s*[:#]?\s*(\S+)$", p)
        if jid:
            rec["job id"] = jid.group(1)
        elif _POSTED_HINT.search(p):
            rec["posted"] = p
        elif _PAY_HINT.search(p):
            rec.setdefault("compensation", p)
        elif i == 0:
            rec["company"] = p
        elif len(p) <= 40 and normalize_employment_type(p) and "job type" not in rec:
            rec["job type"] = p
        elif len(p) <= 30 and "," not in p and remote_from_value(p) is not None and "work mode" not in rec:
            rec["work mode"] = p
        elif "company" not in rec:
            rec["company"] = p
        elif "location" not in rec:
            rec["location"] = p
        else:
            rec.setdefault("details", []).append(p)


def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _records_from_markdown(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    current_keys: set[str] = set()

    def flush() -> None:
        nonlocal current, current_keys
        if current:
            records.append(current)
        current, current_keys = {}, set()

    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # markdown table: header row + |---| separator
        if (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1].strip())
        ):
            flush()
            header = _split_table_row(stripped)
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = _split_table_row(lines[i])
                row = {h: c for h, c in zip(header, cells) if h}
                if any(clean(v) for v in row.values()):
                    records.append(row)
                i += 1
            continue
        i += 1
        if not stripped:
            continue
        fm = _FIELD_LINE.match(line)
        plain = None if fm else _PLAIN_FIELD_LINE.match(line)
        if plain and norm_key(plain.group("label")) not in _PLAIN_LABELS:
            plain = None
        if fm or plain:
            m = fm or plain
            assert m is not None
            label, value = m.group("label").strip(), m.group("value").strip()
            nk = norm_key(label)
            if not nk:
                continue
            if nk in current_keys or (nk in _TITLE_FIELDS and any(k in _TITLE_FIELDS for k in current_keys)):
                flush()
            current[label] = value
            current_keys.add(nk)
            continue
        tl = _title_line(line)
        if tl:
            flush()
            current = tl
            current_keys = {norm_key(k) for k in tl}
    flush()
    return records


def _records_from_text(text: str, fmt: str | None, depth: int = 0) -> list[dict[str, Any]]:
    s = text.lstrip("﻿").strip()
    if not s or depth > 6:
        return []
    fmt = (fmt or "").lower().lstrip(".") or None
    if fmt in ("markdown", "text", "txt", "mdown"):
        fmt = "md"
    if fmt == "tsv":
        return _records_from_csv(s, "\t")
    if fmt in ("json", "jsonl", "ndjson"):
        try:
            return _records_from_obj(json.loads(s), depth + 1)
        except ValueError:
            got = _records_from_jsonl(s)
            if got:
                return got
        fmt = None  # not JSON after all: fall back to detection
    if fmt == "csv":
        got = _records_from_csv(s)
        if got and any(_looks_like_job(r) for r in got):
            return got
        fmt = None
    if fmt == "md":
        got = _records_from_markdown(s)
        if got:
            return got
        fmt = None
    detected = detect_format(s)
    if detected == "json":
        return _records_from_obj(json.loads(s), depth + 1)
    if detected == "jsonl":
        return _records_from_jsonl(s)
    if detected == "csv":
        got = _records_from_csv(s)
        if any(_looks_like_job(r) for r in got):
            return got
    return _records_from_markdown(s)


# ---------------------------------------------------------------------------------------------------------


class ImportSource(Source):
    """Not a fetcher: turns connector / file output into jobs. Used by the CLI, web API and MCP import."""

    name = "import"
    kind = "import"
    label = "Imported jobs (Claude connectors, JSON / CSV / markdown)"
    docs_url = ""

    @staticmethod
    def parse(payload: Any, origin: str = "claude-connector", fmt: str | None = None) -> list[Job]:
        """Parse ``payload`` (str/bytes/list/dict) into jobs; malformed records are skipped, never fatal."""
        origin = slug(origin) or "claude-connector"
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8-sig", errors="replace")
        try:
            if isinstance(payload, str):
                records = _records_from_text(payload, fmt)
            else:
                records = _records_from_obj(payload)
        except Exception as exc:  # noqa: BLE001 - a bad payload yields no jobs instead of a crash
            log.warning("import: could not parse payload (%s): %s", fmt or "auto", exc)
            return []
        now = datetime.now(timezone.utc)
        jobs: list[Job] = []
        seen: set[str] = set()
        for rec in records:
            try:
                job = job_from_record(rec, source="import", id_prefix=origin, tags=[origin], now=now)
            except Exception as exc:  # noqa: BLE001
                log.debug("import: skipped malformed record %r: %s", rec, exc)
                continue
            if job is None or not (job.url or job.company):
                continue
            if job.source_id in seen:
                continue
            seen.add(job.source_id)
            jobs.append(job)
        return jobs

    @staticmethod
    def detect_format(text: str) -> str:
        return detect_format(text)

    @classmethod
    def parse_file(cls, path: str | Path, origin: str = "claude-connector", fmt: str | None = None) -> list[Job]:
        p = Path(path)
        if fmt is None:
            fmt = {".json": "json", ".jsonl": "jsonl", ".csv": "csv", ".tsv": "tsv", ".md": "md"}.get(
                p.suffix.lower()
            )
        return cls.parse(p.read_text(encoding="utf-8-sig", errors="replace"), origin=origin, fmt=fmt)
