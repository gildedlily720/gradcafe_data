"""ATS detection (URL -> board, company name -> boards, careers page -> boards) plus small
normalisation helpers shared by the core ATS adapters.

Board strings follow ARCHITECTURE.md §3.1 exactly (``tenant/wdN/site`` for Workday, ``eu:site`` for EU
Lever, ``host/siteNumber`` for Oracle, ``tenant/guid`` for UltiPro, ``cid|ccId`` for ADP, ...).

``detect_from_url`` / ``find_boards`` only report sweepable ATS (the §3.1 table) unless
``include_vendors=True``, in which case vendor-only systems (iCIMS, Taleo, SuccessFactors, ...) are
reported too, with ``ats`` = the vendor name (use it for ``Employer.ats_vendor``, never ``Employer.ats``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from ..http import HTTPError, fetch_json, request
from ..models import Job
from ..text import MAX_DESCRIPTION_CHARS
from .base import SourceContext, SourceError

log = logging.getLogger("jobsweep.sources.detect")

# --------------------------------------------------------------------------------------------------
# Shared normalisation helpers (used by greenhouse/lever/ashby/workday/smartrecruiters/workable/recruitee)
# --------------------------------------------------------------------------------------------------

EMPLOYMENT_TYPES = (
    "full-time",
    "part-time",
    "contract",
    "temporary",
    "internship",
    "fellowship",
    "volunteer",
    "per-diem",
    "other",
)

_ET_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "internship",
        re.compile(
            r"\b(intern|interns|internship|trainee|traineeship|apprentice\w*|co op|coop|werkstudent)\b"
        ),
    ),
    ("fellowship", re.compile(r"\bfellow(ship)?s?\b")),
    ("volunteer", re.compile(r"\bvolunteer\w*\b")),
    ("per-diem", re.compile(r"\b(per diem|perdiem|prn)\b")),
    ("part-time", re.compile(r"\b(part time|parttime|part)\b")),
    ("contract", re.compile(r"\b(contract|contractor|freelance\w*|consultant|c2c|1099)\b")),
    ("temporary", re.compile(r"\b(temp|temporary|seasonal|fixed term|interim)\b")),
    ("full-time", re.compile(r"\b(full time|fulltime|full|permanent|regular)\b")),
]


def normalize_employment_type(value: Any) -> str | None:
    """Map any ATS employment-type label ("FullTime", "Full time", "fulltime_permanent", "Intern",
    "per-hour", "Fixed-Term", ...) to the ARCHITECTURE §2 vocabulary. None when empty."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value))  # FullTime -> Full Time
    s = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    if not s:
        return None
    if s in EMPLOYMENT_TYPES or s.replace(" ", "-") in EMPLOYMENT_TYPES:
        return s.replace(" ", "-")
    for name, pat in _ET_RULES:
        if pat.search(s):
            return name
    return "other"


_TAG_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("internship", re.compile(r"\bintern(ship)?s?\b", re.I)),
    ("fellowship", re.compile(r"\bfellow(ship)?s?\b", re.I)),
    (
        "graduate-program",
        re.compile(r"\b(graduate|graduates|grad|new grads?|early careers?|campus|university hire)\b", re.I),
    ),
    ("student", re.compile(r"\b(student|students|working student|werkstudent)\b", re.I)),
    ("reservist", re.compile(r"\breservists?\b", re.I)),
]


def structured_tags(*values: Any) -> list[str]:
    """Tags from *structured* source fields (employment type, level, department, metadata values).

    Titles and descriptions are deliberately not scanned here; the classifier handles them.
    """
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            text = re.sub(r"[_\-]+", " ", str(item))
            for tag, pat in _TAG_RULES:
                if tag not in out and pat.search(text):
                    out.append(tag)
    return out


def join_locations(parts: Iterable[Any]) -> str:
    """De-duplicate (case-insensitively, order kept) and join locations with ``"; "``."""
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        s = re.sub(r"\s+", " ", str(p)).strip(" ,;|")
        s = re.sub(r"(,\s*)+,", ",", s)  # "Sao Paulo, , Brazil" -> "Sao Paulo, Brazil"
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return "; ".join(out)


def remote_from_text(value: Any) -> bool | None:
    """Workplace label -> remote flag: "Remote" True; "Hybrid"/"On-site"/"Office" False; else None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    s = re.sub(r"[^a-z]+", " ", str(value).lower())
    if not s.strip():
        return None
    if re.search(r"\bhybrid\b", s):
        return False
    if re.search(r"\bremote\b|\bwork from home\b|\bwfh\b|\bdistributed\b|\banywhere\b", s):
        return True
    if re.search(r"\b(on ?site|in office|office|in person|onsite)\b", s):
        return False
    return None


_PERIODS: list[tuple[str, re.Pattern[str]]] = [
    ("hour", re.compile(r"hour|hourly|\bhr\b", re.I)),
    ("day", re.compile(r"\bday\b|daily|per diem", re.I)),
    ("week", re.compile(r"week", re.I)),
    ("month", re.compile(r"month", re.I)),
    ("year", re.compile(r"year|annual|annum|\byr\b|salary", re.I)),
]


def normalize_period(value: Any) -> str | None:
    """'1 YEAR', 'per-hour-wage', 'Annual Salary', 'month' -> year/month/week/day/hour (None if unknown)."""
    if not value:
        return None
    s = str(value)
    for name, pat in _PERIODS:
        if pat.search(s):
            return name
    return None


def to_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def plain_text(raw: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Tidy text that is already plain (Lever/Ashby ``*Plain`` fields): whitespace + truncation only."""
    if not raw:
        return ""
    text = str(raw).replace("\xa0", " ").replace("\r\n", "\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return text


def safe_parse(
    items: Any, parse: Callable[[dict], Job | None], *, source: str, board: str, logger: logging.Logger
) -> list[Job]:
    """Run ``parse`` over raw postings; a malformed posting is logged and skipped, never fatal."""
    jobs: list[Job] = []
    if not isinstance(items, list):
        return jobs
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            job = parse(item)
        except Exception as exc:  # noqa: BLE001 - one bad posting must not sink the board
            logger.warning(
                "%s %s: skipping malformed posting (%s: %s)", source, board, type(exc).__name__, exc
            )
            continue
        if job is not None:
            jobs.append(job)
    return jobs


# --------------------------------------------------------------------------------------------------
# URL -> (ats, board)
# --------------------------------------------------------------------------------------------------

#: ATS values that have an adapter (ARCHITECTURE §3.1).
SWEEPABLE_ATS = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "workable",
        "recruitee",
        "personio",
        "breezy",
        "bamboohr",
        "paylocity",
        "adp",
        "ultipro",
        "oracle",
        "jobvite",
        "rippling",
        "pinpoint",
        "teamtailor",
    }
)
#: vendor-only systems (reported with ``include_vendors=True``; ``Employer.ats_vendor`` values).
VENDOR_ATS = frozenset(
    {"icims", "taleo", "successfactors", "jazzhr", "avature", "eightfold", "dayforce", "brassring", "csod"}
)

# path segments / subdomains that are never a board name
_RESERVED = {
    "",
    "embed",
    "v0",
    "v1",
    "v2",
    "v3",
    "jobs",
    "job",
    "www",
    "api",
    "app",
    "apps",
    "j",
    "careers",
    "career",
    "apply",
    "static",
    "assets",
    "cdn",
    "help",
    "support",
    "blog",
    "login",
    "signin",
    "wday",
    "oneclick-ui",
    "sr-jobs",
    "external-referrals",
    "marketing",
    "partners",
    "marketplace",
    "resources",
    "status",
    "docs",
    "developers",
    "favicon.ico",
    "robots.txt",
    "sitemap.xml",
}

_B = r"[A-Za-z0-9_-]+"  # generic board token
_SUB = r"[a-z0-9][a-z0-9-]*"  # DNS label
_LOCALE = r"(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?"


def _first(m: re.Match[str]) -> str:
    return m.group(1)


def _lever(m: re.Match[str]) -> str:
    return ("eu:" if m.group(1) else "") + m.group(2)


def _workday(m: re.Match[str]) -> str:
    return f"{m.group(1).lower()}/{m.group(2).lower()}/{m.group(3)}"


def _workday_site(m: re.Match[str]) -> str:  # wd3.myworkdaysite.com/recruiting/{tenant}/{site}
    return f"{m.group(2).lower()}/{m.group(1).lower()}/{m.group(3)}"


def _personio(m: re.Match[str]) -> str:
    sub, tld = m.group(1).lower(), m.group(2).lower()
    return sub if tld == "de" else f"{sub}.jobs.personio.com"


def _pair(m: re.Match[str]) -> str:
    return f"{m.group(1)}/{m.group(2)}"


def _oracle(m: re.Match[str]) -> str:
    return f"{m.group(1).lower()}/{m.group(2)}"


def _adp(m: re.Match[str]) -> str | None:
    qs = parse_qs(urlsplit("https://" + m.group(0).replace("&amp;", "&")).query)
    cid = (qs.get("cid") or [""])[0]
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", cid):
        return None
    cc = (qs.get("ccId") or qs.get("ccid") or [""])[0]
    return f"{cid}|{cc}" if cc and re.fullmatch(r"[0-9_]+", cc) else cid


def _lower_first(m: re.Match[str]) -> str:
    return m.group(1).lower()


_Pattern = tuple[str, re.Pattern[str], Callable[[re.Match[str]], str | None]]


def _p(ats: str, pattern: str, build: Callable[[re.Match[str]], str | None] = _first) -> _Pattern:
    return ats, re.compile(pattern, re.I), build


# Order matters: API/embed shapes before the generic host/path shapes of the same ATS.
ATS_PATTERNS: list[_Pattern] = [
    # greenhouse
    _p("greenhouse", rf"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/({_B})"),
    _p(
        "greenhouse",
        rf"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?(?:[^\s\"'<>]*?&(?:amp;)?)?for=({_B})",
    ),
    _p("greenhouse", rf"(?<![\w.-])(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed\b)({_B})"),
    # lever
    _p("lever", rf"api\.(eu\.)?lever\.co/v0/postings/({_B})", _lever),
    _p("lever", rf"jobs\.(eu\.)?lever\.co/({_B})", _lever),
    # ashby
    _p("ashby", r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", lambda m: unquote(m.group(1))),
    _p("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", lambda m: unquote(m.group(1)).rstrip(".")),
    # workday
    _p("workday", rf"({_SUB})\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/{_SUB}/({_B})", _workday),
    _p("workday", rf"(wd\d+)\.myworkdaysite\.com/wday/cxs/({_SUB})/({_B})", _workday_site),
    _p("workday", rf"(wd\d+)\.myworkdaysite\.com/{_LOCALE}recruiting/({_SUB})/({_B})", _workday_site),
    _p("workday", rf"({_SUB})\.(wd\d+)\.myworkdayjobs\.com/{_LOCALE}({_B})", _workday),
    # smartrecruiters
    _p("smartrecruiters", rf"api\.smartrecruiters\.com/v1/companies/({_B})"),
    _p(
        "smartrecruiters",
        rf"(?:jobs|careers)\.smartrecruiters\.com/(?:(?:oneclick-ui|sr-jobs|external-referrals)/company/)?({_B})",
    ),
    # workable
    _p("workable", rf"apply\.workable\.com/api/v\d/(?:widget/)?accounts/({_B})"),
    _p("workable", rf"(?:www\.)?workable\.com/api/accounts/({_B})"),
    _p("workable", rf"apply\.workable\.com/(?!api/|j/)({_B})"),
    _p("workable", rf"(?<![\w.-])({_SUB})\.workable\.com", _lower_first),
    # recruitee
    _p("recruitee", rf"(?<![\w.-])({_SUB})\.recruitee\.com", _lower_first),
    # personio
    _p("personio", rf"(?<![\w.-])({_SUB})\.jobs\.personio\.(de|com)", _personio),
    # breezy / bamboohr / pinpoint
    _p("breezy", rf"(?<![\w.-])({_SUB})\.breezy\.hr", _lower_first),
    _p("bamboohr", rf"(?<![\w.-])({_SUB})\.bamboohr\.com", _lower_first),
    _p("pinpoint", rf"(?<![\w.-])({_SUB})\.pinpointhq\.com", _lower_first),
    # paylocity
    _p(
        "paylocity",
        r"recruiting\.paylocity\.com/recruiting/(?:jobs/(?:all|list|details)/|v2/api/feed/jobs/)([0-9a-f]{8}-[0-9a-f-]{27})",
    ),
    # adp workforce now (cid + optional ccId)
    _p("adp", r"workforcenow\.adp\.com/[^\s\"'<>]*?[?&](?:amp;)?cid=[0-9a-fA-F-]{36}[^\s\"'<>]*", _adp),
    # ultipro / ukg
    _p(
        "ultipro",
        r"recruiting2?\.ultipro\.(?:com|ca)/([A-Za-z0-9]+)/JobBoard/([0-9a-f]{8}-[0-9a-f-]{27})",
        _pair,
    ),
    # oracle recruiting cloud
    _p(
        "oracle",
        r"([a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/[A-Za-z-]+/sites/([A-Za-z0-9_]+)",
        _oracle,
    ),
    _p(
        "oracle",
        r"([a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com)/hcmRestApi/resources/[^\s\"'<>]*?siteNumber=([A-Za-z0-9_]+)",
        _oracle,
    ),
    # jobvite
    _p("jobvite", r"app\.jobvite\.com/CompanyJobs/[^\s\"'<>]*?[?&](?:amp;)?c=([A-Za-z0-9]+)"),
    _p("jobvite", rf"jobs\.jobvite\.com/(?:careers/)?({_B})"),
    # rippling
    _p("rippling", rf"api\.rippling\.com/platform/api/ats/v1/board/({_B})"),
    _p("rippling", rf"ats\.rippling\.com/{_LOCALE}({_B})"),
    # teamtailor (EU default and NA region subdomains)
    _p("teamtailor", rf"(?<![\w.-])({_SUB}(?:\.na)?)\.teamtailor\.com", _lower_first),
    # ---- vendor-only (not sweepable) ----
    _p("icims", rf"(?<![\w.-])({_SUB})\.icims\.com", _lower_first),
    _p("taleo", rf"(?<![\w.-])({_SUB})\.taleo\.net", _lower_first),
    _p(
        "successfactors",
        r"career\d*\.successfactors\.(?:com|eu)/[^\s\"'<>]*?[?&](?:amp;)?company=([A-Za-z0-9_]+)",
    ),
    _p("successfactors", rf"(?<![\w.-])({_SUB})\.jobs2web\.com", _lower_first),
    _p("jazzhr", rf"(?<![\w.-])({_SUB})\.applytojob\.com", _lower_first),
    _p("avature", rf"(?<![\w.-])({_SUB})\.avature\.net", _lower_first),
    _p("eightfold", rf"(?<![\w.-])({_SUB})\.eightfold\.ai", _lower_first),
    _p("dayforce", rf"jobs\.dayforcehcm\.com/{_LOCALE}({_B})"),
    _p(
        "brassring",
        r"sjobs\.brassring\.com/TGnewUI/Search/Home/Home\?partnerid=(\d+)&(?:amp;)?siteid=(\d+)",
        _pair,
    ),
    _p("csod", rf"(?<![\w.-])({_SUB})\.csod\.com/ux/ats/careersite", _lower_first),
]


def _valid_board(ats: str, board: str | None) -> bool:
    if not board:
        return False
    first = board.split("/")[0].removeprefix("eu:").split(".")[0].lower()
    last = board.split("/")[-1].lower()
    if ats == "workday":
        return last not in _RESERVED and first not in _RESERVED
    if ats in ("ultipro", "oracle", "brassring"):
        return True
    return first not in _RESERVED


def find_boards(text: str, include_vendors: bool = False) -> list[tuple[str, str]]:
    """All (ats, board) pairs mentioned in ``text`` (URL, HTML, script), in pattern-priority order."""
    if not text:
        return []
    if "%2F" in text or "%2f" in text:
        text = text + "\n" + unquote(text)
    found: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for ats, pat, build in ATS_PATTERNS:
        if not include_vendors and ats not in SWEEPABLE_ATS:
            continue
        for m in pat.finditer(text):
            try:
                board = build(m)
            except (IndexError, ValueError):
                continue
            if board is None or not _valid_board(ats, board):
                continue
            key = (ats, board.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            found.append((ats, board))
    return found


def detect_from_url(url: str, include_vendors: bool = False) -> tuple[str, str] | None:
    """Recognise an ATS board in a careers/job/API URL. Returns ``(ats, board)`` or None."""
    hits = find_boards((url or "").strip(), include_vendors=include_vendors)
    return hits[0] if hits else None


# --------------------------------------------------------------------------------------------------
# Company name -> boards
# --------------------------------------------------------------------------------------------------

_STOP = {
    "inc",
    "llc",
    "ltd",
    "plc",
    "the",
    "co",
    "corp",
    "corporation",
    "company",
    "group",
    "holdings",
    "lp",
    "llp",
}


def slug_variants(name: str) -> list[str]:
    """Plausible board slugs for a company name ("Control Risks Group" -> controlrisks, control-risks, ...)."""
    base = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    base = base.replace("&", " and ").replace("'", "")
    words = re.findall(r"[a-z0-9]+", base)
    core = [w for w in words if w not in _STOP] or words
    no_and = [w for w in core if w != "and"]
    cands = [
        "".join(core),
        "-".join(core),
        "".join(no_and),
        "-".join(no_and),
        "".join(words),
        "-".join(words),
        "_".join(core),
        core[0] if core else "",
        "".join(w[0] for w in no_and) if len(no_and) > 1 else "",
    ]
    out: list[str] = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


PROBE_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee")


async def _probe_one(ctx: SourceContext, ats: str, slug: str) -> dict | None:
    """Return {"ats","board","open_jobs","titles"} when the board exists, else None."""

    async def get(url: str, **kw: Any) -> Any:
        return await fetch_json(ctx.client, url, limiter=ctx.limiter, retries=1, **kw)

    try:
        if ats == "greenhouse":
            d = await get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            jobs = (d.get("jobs") or []) if isinstance(d, dict) else []
            return {
                "ats": ats,
                "board": slug,
                "open_jobs": len(jobs),
                "titles": [j.get("title") for j in jobs[:3]],
            }
        if ats == "lever":
            for board, host in ((slug, "api.lever.co"), (f"eu:{slug}", "api.eu.lever.co")):
                try:
                    d = await get(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
                except HTTPError as exc:
                    if exc.status == 404:
                        continue
                    raise
                if isinstance(d, list):
                    return {
                        "ats": ats,
                        "board": board,
                        "open_jobs": len(d),
                        "titles": [j.get("text") for j in d[:3]],
                    }
            return None
        if ats == "ashby":
            d = await get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            jobs = [j for j in (d.get("jobs") or []) if j.get("isListed", True) is not False]
            return {
                "ats": ats,
                "board": slug,
                "open_jobs": len(jobs),
                "titles": [j.get("title") for j in jobs[:3]],
            }
        if ats == "smartrecruiters":
            d = await get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 3}
            )
            total = d.get("totalFound") or 0
            if not total:  # unknown companies answer 200 with totalFound 0
                return None
            return {
                "ats": ats,
                "board": slug,
                "open_jobs": total,
                "titles": [j.get("name") for j in d.get("content") or []],
            }
        if ats == "workable":
            # Through the adapter so probes share its pacing and 429 circuit breaker (lazy: avoids a cycle).
            from .workable import EMPTY_QUERY, LIST_URL, WorkableSource

            d = await WorkableSource(ctx)._call("POST", LIST_URL.format(board=slug), dict(EMPTY_QUERY))
            results = d.get("results") or []
            total = d.get("total") or len(results)
            if not total:
                return None
            return {
                "ats": ats,
                "board": slug,
                "open_jobs": total,
                "titles": [j.get("title") for j in results[:3]],
            }
        if ats == "recruitee":
            d = await get(f"https://{slug}.recruitee.com/api/offers/")
            offers = d.get("offers") or []
            return {
                "ats": ats,
                "board": slug,
                "open_jobs": len(offers),
                "titles": [j.get("title") for j in offers[:3]],
            }
    except HTTPError as exc:
        if exc.status not in (0, 404, 410):
            log.debug("probe %s %s: %s", ats, slug, exc)
        return None
    except SourceError as exc:  # workable circuit breaker open
        log.debug("probe %s %s: %s", ats, slug, exc)
        return None
    except (ValueError, TypeError, AttributeError) as exc:
        log.debug("probe %s %s: unexpected payload (%s)", ats, slug, exc)
        return None
    return None


async def probe_company(
    ctx: SourceContext, name: str, slugs: list[str] | None = None, ats: Iterable[str] | None = None
) -> list[dict]:
    """Try slug variants of ``name`` on the public JSON APIs of greenhouse, lever (US then EU), ashby,
    smartrecruiters, workable and recruitee. Returns ``[{"ats","board","open_jobs","titles"}]`` for
    boards that exist, de-duplicated, most open jobs first."""
    candidates = [s for s in (slugs or slug_variants(name)) if s and re.fullmatch(r"[A-Za-z0-9_.-]+", s)]
    systems = [a for a in (ats or PROBE_ATS) if a in PROBE_ATS]
    fast = [a for a in systems if a != "workable"]
    results: list[dict] = []
    tasks = [_probe_one(ctx, a, s) for s in candidates for a in fast]
    for hit in await asyncio.gather(*tasks):
        if hit:
            results.append(hit)
    if "workable" in systems:  # apply.workable.com rate limits hard: probe sequentially
        for s in candidates:
            hit = await _probe_one(ctx, "workable", s)
            if hit:
                results.append(hit)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for hit in results:
        key = (hit["ats"], str(hit["board"]).lower())
        if key not in seen:
            seen.add(key)
            out.append(hit)
    out.sort(key=lambda h: -(h.get("open_jobs") or 0))
    return out


# --------------------------------------------------------------------------------------------------
# Careers page -> boards
# --------------------------------------------------------------------------------------------------

_IFRAME = re.compile(r"<iframe\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_META_REFRESH = re.compile(
    r"<meta\b[^>]*http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*content\s*=\s*[\"'][^\"']*?url\s*=\s*([^\"'>\s]+)",
    re.I,
)
_JS_REDIRECT = re.compile(
    r"(?:window\.|document\.)?location(?:\.href)?\s*=\s*[\"'](https?://[^\"']+)[\"']", re.I
)
_LINK = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_STRONG = re.compile(
    r"\bjobs?\b|jobs?[-_]|[-_]jobs?|jobsearch|openings?\b|vacanc|\bpositions?\b|requisition|\bapply\b", re.I
)
_JOBBY = re.compile(r"career|opportunit|\bjoin\b|join[-_]us|hiring|employment|work-with-us|work-for-us", re.I)

_HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


async def _fetch_page(ctx: SourceContext, url: str) -> tuple[str, str, list[str]] | None:
    """(final_url, html, redirect_chain_urls) or None on failure."""
    try:
        resp = await request(ctx.client, "GET", url, headers=_HTML_HEADERS, limiter=ctx.limiter, retries=1)
    except Exception as exc:  # noqa: BLE001 - HTTPError, invalid hop URLs, ...: scanning is best effort
        log.debug("scan %s: %s", url, exc)
        return None
    chain = [str(r.url) for r in resp.history]
    try:
        body = resp.text
    except (UnicodeDecodeError, LookupError):
        body = ""
    return str(resp.url), body, chain


def _hops(base: str, html: str, want_links: bool) -> list[str]:
    """Next URLs worth one fetch: frames/redirects first, then job-ish links (strongest first)."""
    frames: list[str] = []
    for pat in (_IFRAME, _META_REFRESH, _JS_REDIRECT):
        for m in pat.finditer(html):
            frames.append(urljoin(base, m.group(1).strip().replace("&amp;", "&")))
    strong: list[str] = []
    weak: list[str] = []
    if want_links:
        for m in _LINK.finditer(html):
            href, label = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2))
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            url = urljoin(base, href.replace("&amp;", "&"))
            if _STRONG.search(href) or _STRONG.search(label):
                strong.append(url)
            elif _JOBBY.search(href) or _JOBBY.search(label):
                weak.append(url)
    out: list[str] = []
    base_key = base.split("#")[0].rstrip("/")
    for h in frames + strong + weak:
        h = h.split("#")[0]
        if h.startswith("http") and h.rstrip("/") != base_key and h not in out:
            out.append(h)
    return out


async def scan_careers_page(
    ctx: SourceContext, url: str, *, include_vendors: bool = False, max_hops: int = 4
) -> list[tuple[str, str]]:
    """Fetch a careers page and return the ATS boards it links to or embeds.

    Scans the URL itself, the redirect chain, the final URL and the HTML; then follows one extra hop:
    iframe ``src``/meta-refresh/JS redirects always, and (only when nothing was found yet) up to
    ``max_hops`` links that look like job pages.
    """
    found: list[tuple[str, str]] = []

    def add(pairs: list[tuple[str, str]]) -> None:
        for p in pairs:
            if p not in found:
                found.append(p)

    add(find_boards(url, include_vendors))
    page = await _fetch_page(ctx, url)
    if page is None:
        return found
    final_url, html, chain = page
    add(find_boards("\n".join([*chain, final_url]), include_vendors))
    add(find_boards(html, include_vendors))

    hops = _hops(final_url, html, want_links=not found)[:max_hops]
    for hop in hops:
        direct = find_boards(hop, include_vendors)
        if direct:
            add(direct)
            continue
        sub = await _fetch_page(ctx, hop)
        if sub is None:
            continue
        s_url, s_html, s_chain = sub
        add(find_boards("\n".join([*s_chain, s_url]), include_vendors))
        add(find_boards(s_html, include_vendors))
    return found


__all__ = [
    "ATS_PATTERNS",
    "EMPLOYMENT_TYPES",
    "PROBE_ATS",
    "SWEEPABLE_ATS",
    "VENDOR_ATS",
    "detect_from_url",
    "find_boards",
    "join_locations",
    "normalize_employment_type",
    "normalize_period",
    "plain_text",
    "probe_company",
    "remote_from_text",
    "safe_parse",
    "scan_careers_page",
    "slug_variants",
    "structured_tags",
    "to_float",
]
