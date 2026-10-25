"""MCP server exposing jobsweep to Claude Code / Claude Desktop over stdio (ARCHITECTURE.md §10).

Start it with ``jobsweep-mcp``, ``jobsweep mcp`` or ``python -m jobsweep.mcp_server``. Only MCP protocol
messages reach stdout: stray ``print`` output is redirected to stderr and logs go to stderr.

Settings, store, taxonomy and employer registry are loaded lazily on first use and cached for the life of
the process; ``configure()`` / ``reset_state()`` let tests and embedders swap them. Modules owned by other
components (store, taxonomy, sweep, export, sources.detect, sources.manual_import) are imported lazily via
``importlib``, so a missing or broken piece disables one tool instead of the whole server.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import difflib
import importlib
import inspect
import io
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import ENV_VARS, Settings
from .models import JOB_STATUSES, Employer, Job, Profile, SearchQuery

log = logging.getLogger("jobsweep.mcp")

# ---- fixed ids (ARCHITECTURE.md §4.1) ------------------------------------------------------------

#: group id -> lane ids. Hardcoded on purpose: tool docstrings and input validation must not depend on
#: catalog/lanes.yaml loading (ids found in the loaded taxonomy are accepted too).
LANE_GROUPS: dict[str, tuple[str, ...]] = {
    "specialty-insurance": (
        "political-risk", "political-violence-terrorism", "war-risk", "kidnap-ransom", "special-risks",
        "trade-credit-surety", "transactional-liability", "financial-lines", "fine-art-specie", "intangibles",
        "aviation", "marine-cargo", "reinsurance-broking", "insurance-fraud-siu",
    ),
    "intelligence-security": (
        "strategic-intelligence", "threat-intelligence", "protective-intelligence", "travel-risk-crisis",
        "gsoc-watch", "corporate-security", "osint", "counterterrorism", "regional-research-specialist",
        "defense-intel-data",
    ),
    "investigations-dd": (
        "corporate-intelligence", "due-diligence", "beneficial-ownership", "fraud-investigations",
        "forensic-investigations", "law-firm-analyst", "litigation-case-analyst",
        "public-defense-investigation", "nonprofit-grant-dd",
    ),
    "geopolitical-risk": (
        "country-sovereign-risk", "geopolitical-analysis", "political-forecasting", "geoeconomics",
        "economic-security", "supply-chain-forced-labor", "risk-consulting",
        "primary-research-expert-networks", "investment-research-dd",
    ),
    "sanctions-fincrime": (
        "sanctions", "export-controls", "aml-kyc", "fincrime-intelligence", "illicit-finance",
        "crypto-investigations", "investigative-compliance", "market-surveillance",
    ),
    "trust-safety": (
        "trust-safety-policy", "policy-enforcement-ai-safeguards", "ai-policy-forecasting",
        "election-integrity", "disinformation", "human-rights",
    ),
    "policy-research": (
        "national-security", "foreign-policy", "think-tank-research", "international-orgs",
        "policy-legislative-analyst", "tech-regulatory-policy", "criminal-justice-policy",
        "social-policy-advocacy", "urban-planning-research", "research-writing",
    ),
    "strategic-comms": (
        "strategic-comms-advisory", "crisis-comms", "public-government-affairs", "reputation-media-analysis",
        "insights-monitoring",
    ),
    "government": (
        "district-attorney", "nyc-integrity", "nyc-mayors-office", "nys-agencies", "ny-fed",
        "emergency-management", "federal-pathways", "federal-investigator",
    ),
    "entry-routes": ("graduate-programs", "analyst-programs", "fellowships", "entry-level-analyst"),
    "earlier-lanes": (
        "program-evaluation", "philanthropy", "digital-rights", "economic-consulting", "broadcast-journalism",
        "real-estate",
    ),
}  # fmt: skip
INACTIVE_GROUPS = frozenset({"earlier-lanes"})
SORTS = ("score", "newest", "posted", "closing", "company")
#: statuses search_jobs shows by default (dismissed/rejected stay hidden unless asked for)
DEFAULT_STATUSES = tuple(s for s in JOB_STATUSES if s not in ("dismissed", "rejected"))
SNIPPET_CHARS = 300
MAX_LIMIT = 100
LIVE_TIMEOUT_S = 110.0
TOP_NEW = 15

#: source name aliases Claude is likely to use
_SOURCE_ALIASES = {
    "indeed": "hasdata_indeed",
    "hasdata": "hasdata_indeed",
    "hasdata-indeed": "hasdata_indeed",
    "nyc": "nycjobs",
    "nyc-jobs": "nycjobs",
    "nys": "nysjobs",
    "statejobsny": "nysjobs",
    "oracle_hcm": "oracle",
    "oracle-hcm": "oracle",
    "jobspy_source": "jobspy",
    "python-jobspy": "jobspy",
    "manual_import": "import",
    "imported": "import",
}

#: credentials that are optional for a source (required ones come from ``Source.requires_env``)
_OPTIONAL_ENV: dict[str, tuple[str, ...]] = {
    "usajobs": ("USAJOBS_API_KEY", "USAJOBS_EMAIL", "PIPEWORX_API_KEY"),
    "nycjobs": ("SOCRATA_APP_TOKEN",),
}

#: claude.ai connectors that pair with jobsweep (their results go into import_jobs)
CLAUDE_CONNECTORS: list[dict[str, Any]] = [
    {
        "name": "Indeed (claude.ai connector)",
        "tools": {
            "search_jobs": "search=<keywords>, location=<'New York, NY' | 'Washington, DC' | 'remote' | 'London'>, "
            "country_code=<'US' | 'GB'>, job_type=<optional: fulltime|parttime|contract|internship|temporary>",
            "get_job_details": "job_id=<Job Id from search_jobs>",
            "get_company_data": "companyName=<company> (reviews, salaries, ratings)",
        },
        "claude_code_names": [
            "mcp__Indeed__search_jobs",
            "mcp__Indeed__get_job_details",
            "mcp__Indeed__get_company_data",
        ],
        "feed": "import_jobs(text=<raw markdown output>, origin='indeed-connector')",
        "notes": "Broadest keyword coverage. Run one search per lane query x location; pass the output unchanged.",
    },
    {
        "name": "USAJOBS via Pipeworx (custom connector)",
        "tools": {"usajobs_search": "keyword, location, pay_grade_low/high, remote, results_per_page"},
        "feed": "import_jobs(text=<raw output>, origin='usajobs-connector')",
        "notes": "jobsweep's own 'usajobs' source already queries USAJOBS (directly with USAJOBS_API_KEY, else via "
        "the Pipeworx gateway); use the connector for extra federal / Pathways queries. Setup: docs/CONNECTORS.md.",
    },
    {
        "name": "TheirStack",
        "tools": {"job search": "title / company / location filters over aggregated postings"},
        "feed": "import_jobs(text=<raw output>, origin='theirstack')",
        "notes": "Credit metered. Same data as the 'theirstack' source (THEIRSTACK_API_KEY).",
    },
    {
        "name": "Apify actors",
        "tools": {"run actor": "hiring.cafe scraper, job-search / LinkedIn / Indeed actors"},
        "feed": "import_jobs(jobs=<dataset items> or text=<raw output>, origin='apify')",
        "notes": "Pay per result. Same actors as the 'apify' source (APIFY_TOKEN).",
    },
    {
        "name": "Exa (web search)",
        "tools": {"search": "find careers pages / postings on employer sites"},
        "feed": "check_employer(name_or_url=<careers page URL>) for ATS boards; import_jobs(...) for listings",
        "notes": "Good for discovering employers not in the registry.",
    },
    {
        "name": "Firecrawl (scraping)",
        "tools": {"scrape / extract": "careers pages without a supported ATS (iCIMS, Taleo, custom sites)"},
        "feed": "import_jobs(text=<markdown or JSON listing>, origin='firecrawl')",
        "notes": "Use for 'manual' employers from list_employers(manual_only=true).",
    },
    {
        "name": "Workopia / Jobs & Careers (job search connectors)",
        "tools": {"job_tool / job-search": "aggregated ATS postings with title, company, location, pay, url"},
        "feed": "import_jobs(text=<raw output>, origin='workopia' | 'jobs-and-careers')",
        "notes": "Optional extra coverage when connected.",
    },
]


def _lane_ids_doc() -> str:
    lines = []
    for gid, lanes in LANE_GROUPS.items():
        note = " [inactive: earlier interests, hidden unless asked for]" if gid in INACTIVE_GROUPS else ""
        lines.append(f"  {gid}{note}: {', '.join(lanes)}")
    return "\n".join(lines)


_DOC_VARS = {
    "{LANE_IDS}": _lane_ids_doc(),
    "{GROUP_IDS}": ", ".join(LANE_GROUPS),
    "{STATUSES}": ", ".join(JOB_STATUSES),
}


def _render_doc(doc: str | None) -> str:
    text = inspect.cleandoc(doc or "")
    for key, value in _DOC_VARS.items():
        text = text.replace(key, value)
    return text


INSTRUCTIONS = _render_doc(
    """
    jobsweep is the candidate's personal job board. The candidate is early career, targeting specialty
    insurance (political risk, K&R, war, trade credit, transactional, financial lines, marine, aviation,
    reinsurance broking, SIU), intelligence & security, investigations & due diligence, geopolitical risk,
    sanctions / export controls / financial crime, trust & safety, policy research, strategic comms, and
    NYC / NYS / federal government roles (incl. Pathways GS-5..9), plus graduate / analyst / fellowship
    routes. Preferred locations: New York, Washington DC, remote US, London.

    Jobs are swept from ~280 employer ATS boards and public job feeds, classified into lanes (grouped as
    {GROUP_IDS}), scored 0-100 for fit and stored locally with a triage status ({STATUSES}).

    Workflow: search_jobs (instant, stored board) -> get_job for details -> set_job_status to triage.
    run_sweep refreshes the board (slow, network); live_search runs one ad hoc query; check_employer looks
    at one company's live board; import_jobs stores jobs found through other connectors (e.g. the Indeed
    connector's raw output). The daily_sweep prompt describes the full daily routine.
    Always present jobs as clickable markdown links [Title](url) with company and location, keep the uid for
    follow-ups, and never invent URLs.
    """
)

mcp = FastMCP("jobsweep", instructions=INSTRUCTIONS, log_level="WARNING")


def _tool(
    *,
    title: str,
    read_only: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
    structured: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn`` as an MCP tool with its docstring rendered (lane/group/status ids filled in)."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__doc__ = _render_doc(fn.__doc__)
        mcp.tool(
            title=title,
            description=fn.__doc__,
            annotations=ToolAnnotations(
                title=title,
                readOnlyHint=read_only,
                destructiveHint=False,
                idempotentHint=idempotent or read_only,
                openWorldHint=open_world,
            ),
            structured_output=structured,
        )(fn)
        return fn

    return deco


# ---- lazily loaded state -------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings: Settings | None = None
        self.store: Any = None
        self.owns_store = False
        self.taxonomy: Any = None
        self.registry: Any = None


_STATE = _State()


def _close_owned_store() -> None:
    if _STATE.store is not None and _STATE.owns_store:
        try:
            _STATE.store.close()
        except Exception:  # noqa: BLE001
            log.debug("closing store failed", exc_info=True)
    _STATE.store, _STATE.owns_store = None, False


def configure(
    *, settings: Settings | None = None, store: Any = None, taxonomy: Any = None, registry: Any = None
) -> None:
    """Inject ready-made components (tests, embedding). Components left unset still load lazily."""
    with _STATE.lock:
        if settings is not None:
            _STATE.settings = settings
        if store is not None:
            _close_owned_store()
            _STATE.store, _STATE.owns_store = store, False
        if taxonomy is not None:
            _STATE.taxonomy = taxonomy
        if registry is not None:
            _STATE.registry = registry


def reset_state() -> None:
    """Forget cached settings/store/taxonomy/registry (closing a store we opened) and the sweep result."""
    with _STATE.lock:
        _close_owned_store()
        _STATE.settings = _STATE.taxonomy = _STATE.registry = None
    _SWEEPS.reset()


def _module(name: str, what: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - a missing component disables one tool, not the server
        raise ToolError(f"{what} is unavailable ({name} failed to import: {exc})") from exc


def get_settings() -> Settings:
    with _STATE.lock:
        if _STATE.settings is None:
            _STATE.settings = Settings.load()
        return _STATE.settings


def get_store() -> Any:
    with _STATE.lock:
        if _STATE.store is None:
            settings = get_settings()
            store_mod = _module("jobsweep.store", "the job store")
            try:
                _STATE.store = store_mod.Store(settings.db_path)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"cannot open the job store at {settings.db_path}: {exc}") from exc
            _STATE.owns_store = True
            log.info("store opened at %s", settings.db_path)
        return _STATE.store


def get_taxonomy() -> Any:
    with _STATE.lock:
        if _STATE.taxonomy is None:
            settings = get_settings()
            tax_mod = _module("jobsweep.taxonomy", "the lane taxonomy")
            try:
                _STATE.taxonomy = tax_mod.Taxonomy.load(profile=settings.profile_path)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"the lane taxonomy (catalog/lanes.yaml) failed to load: {exc}") from exc
        return _STATE.taxonomy


def _taxonomy_or_none() -> Any:
    try:
        return get_taxonomy()
    except ToolError as exc:
        log.warning("%s", exc)
        return None


def get_registry() -> Any:
    with _STATE.lock:
        if _STATE.registry is None:
            from .employers import EmployerRegistry

            try:
                _STATE.registry = EmployerRegistry.load()
            except Exception as exc:  # noqa: BLE001
                log.warning("employer registry failed to load (%s); continuing without it", exc)
                _STATE.registry = EmployerRegistry([])
        return _STATE.registry


# ---- small helpers -------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str]:
    """None / "a,b" / ["a", "b,c"] -> flat list of stripped non-empty strings."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    out: list[str] = []
    for v in value:
        out.extend(_as_list(v) if isinstance(v, str) else [str(v)])
    return out


def _norm_id(value: str) -> str:
    return re.sub(r"[\s_]+", "-", value.strip().lower())


def _slug(text: str) -> str:
    base = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", base.replace("&", " and ")).strip("-") or "employer"


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _day(value: Any) -> str | None:
    return str(value)[:10] if value else None


def _snippet(text: str | None, limit: int = SNIPPET_CHARS) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _money(value: float, currency: str | None) -> str:
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get((currency or "").upper())
    text = f"{value:,.0f}" if value >= 100 or float(value).is_integer() else f"{value:,.2f}"
    if sym:
        return sym + text
    return f"{text} {currency}" if currency else text


def format_salary(row: dict[str, Any]) -> str | None:
    """'$65,000–$75,000/yr', '£40,000/yr', '$28.50/hr' or None."""
    lo, hi = _num(row.get("salary_min")), _num(row.get("salary_max"))
    if not lo and not hi:
        return None
    cur = row.get("salary_currency")
    period = {"year": "/yr", "month": "/mo", "week": "/wk", "day": "/day", "hour": "/hr"}.get(
        str(row.get("salary_period") or "").lower(), ""
    )
    if lo and hi and abs(hi - lo) > 0.5:
        return f"{_money(lo, cur)}–{_money(hi, cur)}{period}"
    return f"{_money(lo or hi or 0.0, cur)}{period}"


def job_item(row: dict[str, Any]) -> dict[str, Any]:
    """Compact, JSON-serialisable view of a stored row (or ``Job.to_dict()``) for tool results."""
    score = _num(row.get("score")) or 0.0
    uid = row.get("uid") or f"{row.get('source')}:{row.get('source_id')}"
    return {
        "uid": uid,
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "location": row.get("location") or "",
        "remote": row.get("remote"),
        "posted_at": _day(row.get("posted_at")),
        "closes_at": _day(row.get("closes_at")),
        "salary": format_salary(row),
        "lanes": list(row.get("lanes") or []),
        "score": round(score),
        "entry_level": row.get("entry_level"),
        "status": row.get("status") or "new",
        "url": row.get("url") or None,
        "first_seen": _day(row.get("first_seen")),
        "snippet": _snippet(row.get("description")),
    }


def _rank_key(row: dict[str, Any]) -> tuple:
    """Entry-compatible first, then lane matches, then score."""
    return (row.get("entry_level") is False, not row.get("lanes"), -(_num(row.get("score")) or 0.0))


def _profile_min_score() -> float:
    tax = _taxonomy_or_none()
    try:
        return float(tax.profile.min_score)
    except Exception:  # noqa: BLE001
        return Profile().min_score


def _call_kwargs(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keyword arguments ``fn`` accepts (tolerates small API drift in other modules)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(vars(obj))


# ---- id resolution -------------------------------------------------------------------------------


def _known_ids() -> tuple[set[str], set[str]]:
    lanes = {lane for ids in LANE_GROUPS.values() for lane in ids}
    groups = set(LANE_GROUPS)
    tax = _taxonomy_or_none()
    if tax is not None:
        lanes.update(getattr(tax, "lanes", {}) or {})
        groups.update(g.id for g in getattr(tax, "groups", []) or [] if getattr(g, "id", None))
    return lanes, groups


def _unknown(kind: str, bad: list[str], valid: set[str], hint: str) -> ToolError:
    parts = []
    for b in bad:
        close = difflib.get_close_matches(_norm_id(b), sorted(valid), n=3, cutoff=0.55)
        parts.append(f"{b!r}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
    return ToolError(f"unknown {kind}: {'; '.join(parts)}. {hint}")


def _resolve_ids(lanes: Any, groups: Any) -> tuple[list[str] | None, list[str] | None]:
    """Validate lane/group ids; a group id given as a lane (or vice versa) is moved to the right list."""
    known_lanes, known_groups = _known_ids()
    out_l: list[str] = []
    out_g: list[str] = []
    bad: list[str] = []
    for raw in _as_list(lanes) + _as_list(groups):
        x = _norm_id(raw)
        if x in known_lanes:
            out_l.append(x)
        elif x in known_groups:
            out_g.append(x)
        else:
            bad.append(raw)
    if bad:
        raise _unknown(
            "lane/group id",
            bad,
            known_lanes | known_groups,
            f"Group ids: {', '.join(LANE_GROUPS)}. Call list_lanes for every lane id.",
        )
    return (list(dict.fromkeys(out_l)) or None), (list(dict.fromkeys(out_g)) or None)


def _resolve_sources(sources: Any, *, runnable: bool) -> list[str] | None:
    names = _as_list(sources)
    if not names:
        return None
    from . import sources as sources_mod

    known = set(sources_mod.SOURCE_PATHS)
    out: list[str] = []
    bad: list[str] = []
    for raw in names:
        x = raw.strip().lower()
        x = _SOURCE_ALIASES.get(x, x)
        if x not in known:
            x = _SOURCE_ALIASES.get(x.replace("-", "_"), x.replace("-", "_"))
        if x not in known:
            bad.append(raw)
            continue
        if runnable and x == "import":
            raise ToolError("'import' is not a fetcher: pass connector output to import_jobs instead")
        out.append(x)
    if bad:
        raise _unknown("source", bad, known, "Call list_sources for valid names and their availability.")
    return list(dict.fromkeys(out))


_CORP_WORDS = re.compile(
    r"\b(inc|incorporated|llc|llp|lp|ltd|limited|plc|corp|corporation|co|company|group|holdings|the|sa|ag|gmbh)\b"
)


def _norm_name(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s.replace("&", " and ").replace("'", ""))
    return " ".join(_CORP_WORDS.sub(" ", s).split())


def find_employer(registry: Any, text: str) -> tuple[Employer | None, list[Employer]]:
    """Registry lookup by key or (fuzzy) name. Returns (best match or None, other plausible matches)."""
    if registry is None or not (text or "").strip():
        return None, []
    raw = text.strip()
    for key in (raw, raw.lower(), _norm_id(raw), _slug(raw)):
        emp = registry.get(key)
        if emp is not None:
            return emp, []
    needle = _norm_name(raw)
    if not needle:
        return None, []
    scored: list[tuple[float, Employer]] = []
    for emp in registry.all():
        best = 0.0
        for cand in {_norm_name(emp.name), _norm_name(emp.key.replace("-", " "))}:
            if not cand:
                continue
            if cand == needle:
                s = 1.0
            elif len(needle) >= 3 and (f" {needle} " in f" {cand} " or f" {cand} " in f" {needle} "):
                s = 0.9
            else:
                s = difflib.SequenceMatcher(None, needle, cand).ratio()
            best = max(best, s)
        if best >= 0.6:
            scored.append((best, emp))
    scored.sort(key=lambda t: (-t[0], t[1].name))
    if scored and scored[0][0] >= 0.85:
        return scored[0][1], [e for _, e in scored[1:6]]
    return None, [e for _, e in scored[:5]]


def _resolve_employers(employers: Any) -> list[str] | None:
    names = _as_list(employers)
    if not names:
        return None
    registry = get_registry()
    keys: list[str] = []
    bad: list[str] = []
    for raw in names:
        emp, alts = find_employer(registry, raw)
        if emp is None:
            bad.append(raw + (f" (did you mean {', '.join(e.key for e in alts[:3])}?)" if alts else ""))
        else:
            keys.append(emp.key)
    if bad:
        raise ToolError(
            f"unknown employer: {'; '.join(bad)}. Use list_employers to find keys, or check_employer for "
            "companies that are not in the registry."
        )
    return list(dict.fromkeys(keys))


def _employer_dict(emp: Employer) -> dict[str, Any]:
    return {
        "key": emp.key,
        "name": emp.name,
        "sweepable": emp.sweepable,
        "ats": emp.ats,
        "board": emp.board,
        "ats_vendor": emp.ats_vendor,
        "careers_url": emp.careers_url,
        "groups": list(emp.groups),
        "lanes": list(emp.lanes),
        "verified": emp.verified,
        "open_jobs": emp.open_jobs,
    }


def _employer_lookup(registry: Any) -> Callable[[Job], Employer | None]:
    """Job -> registry employer by employer_key, else by normalised company name (classification priors)."""
    by_name: dict[str, Employer] = {}
    if registry is not None:
        for emp in registry.all():
            by_name.setdefault(_norm_name(emp.name), emp)

    def lookup(job: Job) -> Employer | None:
        if registry is None:
            return None
        if job.employer_key:
            emp = registry.get(job.employer_key)
            if emp is not None:
                return emp
        return by_name.get(_norm_name(job.company)) if job.company else None

    return lookup


def _classify(taxonomy: Any, jobs: list[Job], registry: Any) -> list[Job]:
    lookup = _employer_lookup(registry)
    out = []
    for job in jobs:
        emp = lookup(job)
        try:
            res = taxonomy.classify(job, emp)
            job = res if isinstance(res, Job) else job
        except Exception as exc:  # noqa: BLE001 - keep the job unclassified rather than lose it
            log.warning("classify failed for %s: %s", job.uid, exc)
        out.append(job)
    return out


# ---- background sweep ----------------------------------------------------------------------------


class _SweepRunner:
    """One sweep at a time, in a daemon thread with its own event loop (the MCP loop stays responsive)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()
        self.running = False
        self.progress: dict[str, Any] = {}
        self.result: dict[str, Any] | None = None
        self.thread: threading.Thread | None = None

    def reset(self) -> None:
        with self._lock:
            if not self.running:
                self.progress, self.result = {}, None

    def start(self, job: Callable[[Callable[[dict], None]], dict], options: dict[str, Any]) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self._done.clear()
            self.result = None
            self.progress = {
                "phase": "starting",
                "started_at": _now_iso(),
                "finished_at": None,
                "done": 0,
                "total": None,
                "ok": 0,
                "errors": 0,
                "jobs": 0,
                "new": 0,
                "options": options,
            }
        self.thread = threading.Thread(target=self._run, args=(job,), name="jobsweep-mcp-sweep", daemon=True)
        self.thread.start()
        return True

    def _run(self, job: Callable[[Callable[[dict], None]], dict]) -> None:
        result: dict[str, Any] = {"status": "error", "error": "sweep did not finish"}
        try:
            result = job(self.on_event)
        except Exception as exc:  # noqa: BLE001 - reported through sweep_status
            log.exception("sweep failed")
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}
        finally:
            with self._lock:
                self.result = result
                self.running = False
                self.progress["phase"] = result.get("status", "finished")
                self.progress["finished_at"] = _now_iso()
            self._done.set()

    def on_event(self, event: dict) -> None:
        """``sweep.run_sweep`` progress callback (plan / target_done / finished)."""
        if not isinstance(event, dict):
            return
        with self._lock:
            p = self.progress
            kind = event.get("event")
            if kind == "plan":
                p["phase"] = "running"
                p["total"] = event.get("total")
            elif kind == "target_done":
                p["phase"] = "running"
                p["done"] = event.get("done", (p.get("done") or 0) + 1)
                if event.get("total") is not None:
                    p["total"] = event.get("total")
                p["ok" if event.get("ok") else "errors"] += 1
                p["jobs"] += int(event.get("jobs") or 0)
                p["new"] += int(event.get("new") or 0)
            elif kind == "finished":
                p["phase"] = "finishing"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "progress": copy.deepcopy(self.progress),
                "result": copy.deepcopy(self.result),
            }

    async def wait(self, seconds: float, ctx: Context | None = None) -> bool:
        """Wait (without blocking the loop) up to ``seconds`` for the sweep to end; True when it has."""
        deadline = time.monotonic() + max(float(seconds or 0), 0.0)
        last: tuple | None = None
        while not self._done.is_set():
            if ctx is not None:
                p = self.progress
                key = (p.get("done"), p.get("total"))
                if key != last:
                    last = key
                    await _report_progress(ctx, p)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await anyio.sleep(min(0.5, remaining))
        return True


_SWEEPS = _SweepRunner()


async def _report_progress(ctx: Context, p: dict[str, Any]) -> None:
    try:
        done, total = p.get("done") or 0, p.get("total")
        await ctx.report_progress(done, total, f"{done}/{total or '?'} targets, {p.get('new') or 0} new jobs")
    except Exception:  # noqa: BLE001 - progress is best effort (no token / no request context)
        pass


def _progress_view(p: dict[str, Any]) -> dict[str, Any]:
    keys = ("phase", "started_at", "done", "total", "ok", "errors", "jobs", "new")
    return {k: p.get(k) for k in keys}


def _top_new(store: Any, uids: list[str], limit: int = TOP_NEW) -> list[dict[str, Any]]:
    rows = []
    for uid in uids[:5000]:
        row = store.get(uid)
        if row and row.get("active", True) and not row.get("duplicate_of"):
            rows.append(row)
    rows.sort(key=_rank_key)
    return [job_item(r) for r in rows[:limit]]


def _sweep_result(report: Any, store: Any) -> dict[str, Any]:
    d = _as_dict(report)
    per_source = d.get("per_source") or {}
    errors = list(d.get("errors") or [])
    new_uids = list(d.get("new_uids") or [])
    plan = d.get("plan") or {}
    planned = plan.get("counts") or {}
    summary = ""
    if hasattr(report, "summary"):
        try:
            summary = str(report.summary())
        except Exception:  # noqa: BLE001
            summary = ""
    if len(summary) > 2500:
        summary = summary[:2500].rsplit("\n", 1)[0] + "\n…"
    out: dict[str, Any] = {
        "status": "planned" if d.get("dry_run") else "finished",
        "run_id": d.get("run_id"),
        "started_at": d.get("started_at"),
        "finished_at": d.get("finished_at"),
        "counts": {
            "boards_planned": planned.get("boards", 0),
            "searches_planned": planned.get("searches", 0),
            "targets_ok": sum(int(p.get("ok") or 0) for p in per_source.values()),
            "targets_failed": sum(int(p.get("errors") or 0) for p in per_source.values()),
            "jobs_fetched": sum(int(p.get("jobs") or 0) for p in per_source.values()),
            "jobs_stored": d.get("total_jobs", 0),
            "new": len(new_uids),
            "closed_on_boards": d.get("deactivated", 0),
            "expired": d.get("expired", 0),
        },
        "per_source": {
            name: {k: p.get(k, 0) for k in ("targets", "ok", "errors", "jobs", "kept", "new")}
            for name, p in sorted(per_source.items())
        },
        "errors": [
            {k: str(e.get(k, ""))[:200] for k in ("source", "target", "error")}
            for e in errors[:10]
            if isinstance(e, dict)
        ],
        "errors_total": len(errors),
        "skipped_sources": list(plan.get("skipped_sources") or [])[:25],
        "summary": summary,
    }
    if d.get("dry_run"):
        out["plan"] = {
            "sources": plan.get("sources", []),
            "by_source": planned.get("by_source", {}),
            "boards_sample": [b.get("employer") for b in (plan.get("boards") or [])[:30]],
            "searches_sample": [
                {k: s.get(k) for k in ("source", "keywords", "lane")} for s in (plan.get("searches") or [])[:20]
            ],
            "truncated": plan.get("truncated", {}),
        }
    else:
        out["top_new"] = _top_new(store, new_uids) if store is not None else []
    return out


def _sweep_payload(*, started: bool | None = None) -> dict[str, Any]:
    snap = _SWEEPS.snapshot()
    if snap["running"]:
        out: dict[str, Any] = {
            "status": "running",
            "progress": _progress_view(snap["progress"]),
            "hint": "The sweep continues in the background. Call sweep_status(wait_seconds=45) to wait for "
            "the result; search_jobs already shows jobs stored so far.",
        }
    elif snap["result"] is not None:
        out = dict(snap["result"])
    else:
        out = {"status": "idle", "hint": "No sweep has run in this server session; call run_sweep."}
        try:
            runs = get_store().runs(limit=1)
        except Exception:  # noqa: BLE001
            runs = []
        if runs:
            r = runs[0]
            stats = r.get("stats") or {}
            out["last_run"] = {
                "id": r.get("id"),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "new": stats.get("new"),
                "total_jobs": stats.get("total_jobs"),
                "errors": stats.get("errors"),
            }
    if started is not None:
        out["started"] = started
    return out


# ---- tools ---------------------------------------------------------------------------------------

IdList = list[str] | str | None


@_tool(title="Search stored jobs", read_only=True)
def search_jobs(
    query: Annotated[
        str,
        Field(
            description='Full-text search over title, company, location and description. Words are '
            'prefix-matched and ANDed; "quoted phrases" and OR work. Example: "political risk" OR "credit risk".'
        ),
    ] = "",
    lanes: Annotated[IdList, Field(description="Lane ids (match any), e.g. ['kidnap-ransom', 'war-risk'].")] = None,
    groups: Annotated[
        IdList, Field(description="Group ids (match any), e.g. ['sanctions-fincrime', 'government'].")
    ] = None,
    sources: Annotated[
        IdList,
        Field(description="Source names, e.g. ['usajobs', 'nycjobs', 'greenhouse', 'import'] (see list_sources)."),
    ] = None,
    statuses: Annotated[
        IdList,
        Field(
            description="Triage statuses to include. Default: all except dismissed and rejected. "
            "['saved'] = shortlist, ['all'] = everything."
        ),
    ] = None,
    entry_level_only: Annotated[
        bool, Field(description="Hide jobs flagged too senior (unknown seniority is kept). Default true.")
    ] = True,
    location: Annotated[
        str | list[str] | None,
        Field(description="Substring of the location, e.g. 'New York', 'Washington', 'London', 'Remote'."),
    ] = None,
    remote: Annotated[bool | None, Field(description="true = remote only, false = exclude remote.")] = None,
    new_within_days: Annotated[
        float | None, Field(description="Only jobs first seen within this many days (e.g. 1, 7).")
    ] = None,
    min_score: Annotated[
        float | None,
        Field(
            description="Minimum fit score 0-100. Default: the profile threshold (about 25) when no query is "
            "given, none with a query. 0 shows everything; 50+ is a strong match."
        ),
    ] = None,
    sort: Annotated[
        Literal["score", "newest", "posted", "closing", "company"],
        Field(description="score (best first), newest (first seen), posted, closing (deadline soonest), company."),
    ] = "score",
    limit: Annotated[int, Field(description="Max items (1-100).")] = 25,
    offset: Annotated[int, Field(description="Skip this many items (paging).")] = 0,
) -> dict[str, Any]:
    """
    Search the candidate's stored job board: postings already swept from employer ATS boards, USAJOBS / NYC /
    NY State feeds, job aggregators and imported connector results, each classified into lanes and scored
    0-100 for fit (domain relevance + entry-level fit + location + recency).

    Use this FIRST for any "what jobs are there / show me X roles / anything new" request: it is instant
    and needs no network. Use run_sweep or live_search only when the board is stale or has nothing relevant.

    Defaults: active postings, entry-level-compatible (known-senior roles hidden), every triage status except
    dismissed/rejected, score >= the profile threshold unless `query` is given, best score first, 25 items.
    Returns {"total", "offset", "items": [...]} where each item has uid (for get_job / set_job_status), title,
    company, location, remote, posted_at, closes_at, salary, lanes, score, entry_level, status, url,
    first_seen and a 300-character snippet.

    Show results as clickable markdown links, e.g. "[Title](url) — Company · Location · salary · closes
    YYYY-MM-DD", grouped or ordered sensibly, and keep uids at hand for follow-ups. Never invent URLs (url can
    be null for some imported jobs).

    Lane ids by group (lane ids go in `lanes`, group ids in `groups`):
    {LANE_IDS}
    Statuses: {STATUSES}.
    """
    store = get_store()
    lane_ids, group_ids = _resolve_ids(lanes, groups)
    status_list = [s.strip().lower() for s in _as_list(statuses)]
    bad = [s for s in status_list if s != "all" and s not in JOB_STATUSES]
    if bad:
        raise ToolError(f"unknown status {bad[0]!r}; valid: {', '.join(JOB_STATUSES)} (or 'all')")
    if not status_list:
        status_list = list(DEFAULT_STATUSES)
    has_query = bool(query and query.strip())
    if min_score is None and not has_query:
        min_score = _profile_min_score()
    filters: dict[str, Any] = {
        "q": query.strip() if has_query else None,
        "lanes": lane_ids,
        "groups": group_ids,
        "sources": _resolve_sources(sources, runnable=False),
        "statuses": status_list,
        "entry_only": bool(entry_level_only),
        "include_unknown_entry": True,
        "min_score": min_score,
        "location": _as_list(location) or None,
        "remote": remote,
        "since": new_within_days if new_within_days else None,
    }
    limit = _clamp(limit, 1, MAX_LIMIT, 25)
    offset = _clamp(offset, 0, 1_000_000, 0)
    sort_key = sort if sort in SORTS else "score"
    try:
        rows = store.query(sort=sort_key, limit=limit, offset=offset, **filters)
        total = store.count(**filters)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"invalid search: {exc}") from exc
    out: dict[str, Any] = {
        "total": total,
        "offset": offset,
        "returned": len(rows),
        "items": [job_item(r) for r in rows],
    }
    if offset + len(rows) < total:
        out["next_offset"] = offset + len(rows)
    if total == 0:
        out["hint"] = (
            "Nothing stored matches. Loosen filters (min_score=0, entry_level_only=false, statuses=['all']), "
            "or fetch fresh postings with run_sweep / live_search / check_employer."
        )
    return out


@_tool(title="Get job details", read_only=True)
def get_job(
    uid: Annotated[str, Field(description="Job uid from search results, e.g. 'greenhouse:12345'.")],
) -> dict[str, Any]:
    """
    Full stored record of one job: plain-text description (up to ~12k chars), match_reasons (why it was
    classified and scored that way), lanes and lane_scores, entry_level and years_required, salary (formatted
    plus raw fields), grade/series for federal jobs, tags, posted/closing dates, source, url, and the triage
    status and note.

    Use after search_jobs / digest / run_sweep when the user wants details, a fit assessment, or help
    applying: summarise duties, requirements and deadline, and give the url as a clickable link.
    """
    key = (uid or "").strip()
    row = get_store().get(key)
    if row is None:
        raise ToolError(f"no stored job with uid {key!r}; use search_jobs to find uids")
    out = dict(row)
    out["salary"] = format_salary(row)
    out["score"] = round(_num(row.get("score")) or 0.0, 1)
    return out


@_tool(title="Set job triage status", idempotent=True)
def set_job_status(
    uid: Annotated[str, Field(description="Job uid, e.g. 'lever:abc-123'.")],
    status: Annotated[str, Field(description="One of: new, saved, applied, interviewing, offer, rejected, dismissed.")],
    note: Annotated[
        str, Field(description="Optional note stored with the job (omit to keep the existing note).")
    ] = "",
) -> dict[str, Any]:
    """
    Triage a stored job by recording the user's decision or progress. Statuses: {STATUSES}.
      saved = shortlisted / plans to apply; applied = application submitted; interviewing / offer / rejected =
      later stages; dismissed = not interested (hidden from default search_jobs results); new = reset.
    Only set applied, interviewing, offer or rejected when the user says so. `note` is free text such as
    "referral via alumni network" or "apply by Nov 15". Returns the updated job (compact) with its note.
    """
    value = (status or "").strip().lower()
    if value not in JOB_STATUSES:
        raise ToolError(f"unknown status {status!r}; valid: {', '.join(JOB_STATUSES)}")
    key = (uid or "").strip()
    try:
        row = get_store().set_status(key, value, note.strip() or None if note else None)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if row is None:
        raise ToolError(f"no stored job with uid {key!r}; use search_jobs to find uids")
    out = job_item(row)
    out.update(ok=True, note=row.get("note"), status_updated_at=row.get("status_updated_at"))
    return out


@_tool(title="List lanes", read_only=True)
def list_lanes(
    include_inactive: Annotated[
        bool, Field(description="Also list the inactive 'earlier-lanes' group (old interests).")
    ] = False,
    include_queries: Annotated[
        bool, Field(description="Include each lane's keyword queries (used for searches and the Indeed connector).")
    ] = True,
) -> dict[str, Any]:
    """
    The candidate's lane taxonomy: groups -> lanes with ids, names, the number of active stored jobs in each,
    and (include_queries) the keyword queries jobsweep searches with. Use it to map a request to lane/group
    ids for search_jobs / run_sweep, to pick queries for the Indeed connector (daily_sweep), or to show which
    areas currently have openings. Group ids: {GROUP_IDS}.
    """
    tax = _taxonomy_or_none()
    try:
        stats = get_store().stats()
    except Exception as exc:  # noqa: BLE001 - counts are a nice-to-have
        log.warning("store stats unavailable: %s", exc)
        stats = {}
    by_lane, by_group = stats.get("by_lane") or {}, stats.get("by_group") or {}
    if tax is not None:
        doc = tax.to_dict()
    else:
        doc = {
            "groups": [
                {
                    "id": gid,
                    "name": gid,
                    "active": gid not in INACTIVE_GROUPS,
                    "lanes": [{"id": lid, "name": lid, "active": True, "queries": []} for lid in lanes],
                }
                for gid, lanes in LANE_GROUPS.items()
            ]
        }
    groups = []
    for g in doc.get("groups", []):
        if not include_inactive and not g.get("active", True):
            continue
        lanes = []
        for lane in g.get("lanes", []):
            if not include_inactive and not lane.get("active", True):
                continue
            item = {"id": lane.get("id"), "name": lane.get("name"), "count": by_lane.get(lane.get("id"), 0)}
            if include_inactive:
                item["active"] = lane.get("active", True)
            if include_queries:
                item["queries"] = list(lane.get("queries") or [])
            lanes.append(item)
        entry = {"id": g.get("id"), "name": g.get("name"), "count": by_group.get(g.get("id"), 0), "lanes": lanes}
        if include_inactive:
            entry["active"] = g.get("active", True)
        groups.append(entry)
    out: dict[str, Any] = {"groups": groups, "active_jobs": stats.get("active"), "new_7d": stats.get("new_7d")}
    if tax is None:
        out["warning"] = "lanes.yaml could not be loaded; showing the fixed ids only"
    return out


@_tool(title="Run a sweep", open_world=True)
async def run_sweep(
    sources: Annotated[
        IdList,
        Field(
            description="Only these sources (e.g. ['greenhouse', 'usajobs', 'nycjobs']). Default: every "
            "available non-metered source. Naming a metered source runs it."
        ),
    ] = None,
    lanes: Annotated[IdList, Field(description="Only these lane ids (boards and searches).")] = None,
    groups: Annotated[IdList, Field(description="Only these group ids.")] = None,
    employers: Annotated[
        IdList, Field(description="Only these employer boards (registry keys or names; see list_employers).")
    ] = None,
    include_metered: Annotated[
        bool, Field(description="Also run credit-metered sources (TheirStack, HasData Indeed, Apify).")
    ] = False,
    searches: Annotated[bool, Field(description="Run keyword searches (USAJOBS, NYC Jobs, aggregators).")] = True,
    boards: Annotated[bool, Field(description="Fetch employer ATS boards.")] = True,
    posted_within_days: Annotated[
        int | None, Field(description="Search sources: only postings from the last N days (default 30).")
    ] = None,
    max_queries_per_source: Annotated[
        int | None, Field(description="Cap on keyword queries per search source (default 40).")
    ] = None,
    dry_run: Annotated[bool, Field(description="Only plan: show targets without fetching anything.")] = False,
    wait_seconds: Annotated[
        float, Field(description="How long this call waits for the sweep to finish before returning.")
    ] = 45,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Refresh the board: fetch the tracked employers' ATS boards (Greenhouse, Lever, Ashby, Workday, …) and run
    the lanes' keyword searches on the free search sources (USAJOBS, NYC Jobs, NY State, plus Adzuna / JobSpy
    when configured), then classify, score and store the postings. Network-heavy: a full sweep takes several
    minutes; narrow it with lanes / groups / sources / employers for a quick, targeted refresh.

    Metered sources only run with include_metered=true or when named in `sources`. Only one sweep runs at a
    time. The sweep keeps running in the background: this call waits up to wait_seconds and returns either
    the result (status "finished": counts, per-source stats, errors, a text summary and top_new = the 15 best
    new matches) or status "running", in which case call sweep_status(wait_seconds=45) until it finishes.
    dry_run=true previews the plan without fetching.

    Afterwards present top_new as markdown links ("[Title](url) — Company · Location"), or call
    digest(days=1) for every new match grouped by lane.

    Lane ids by group:
    {LANE_IDS}
    """
    sweep_mod = _module("jobsweep.sweep", "the sweep engine")
    lane_ids, group_ids = _resolve_ids(lanes, groups)
    opts: dict[str, Any] = {
        "sources": _resolve_sources(sources, runnable=True),
        "lanes": lane_ids,
        "groups": group_ids,
        "employers": _resolve_employers(employers),
        "include_metered": bool(include_metered),
        "searches": bool(searches),
        "boards": bool(boards),
        "dry_run": bool(dry_run),
    }
    if posted_within_days:
        opts["posted_within_days"] = _clamp(posted_within_days, 1, 365, 30)
    if max_queries_per_source is not None:
        opts["max_queries_per_source"] = _clamp(max_queries_per_source, 0, 1000, 40)
    options_cls = sweep_mod.SweepOptions
    if hasattr(options_cls, "from_dict"):
        options = options_cls.from_dict(opts)
    else:
        options = options_cls(**{k: v for k, v in opts.items() if v is not None})
    settings, taxonomy, registry, store = get_settings(), get_taxonomy(), get_registry(), get_store()

    if dry_run:
        report = await _maybe_await(sweep_mod.run_sweep(settings, taxonomy, registry, store, options))
        return _sweep_result(report, None)

    def job(on_event: Callable[[dict], None]) -> dict[str, Any]:
        res = sweep_mod.run_sweep(settings, taxonomy, registry, store, options, progress=on_event)
        if inspect.isawaitable(res):
            res = asyncio.run(res)  # type: ignore[arg-type]
        return _sweep_result(res, store)

    if not _SWEEPS.start(job, {k: v for k, v in opts.items() if v not in (None, False)}):
        out = _sweep_payload(started=False)
        out["status"] = "already_running"
        out["hint"] = "A sweep is already running; call sweep_status(wait_seconds=45) to wait for its result."
        return out
    await _SWEEPS.wait(wait_seconds, ctx)
    return _sweep_payload(started=True)


@_tool(title="Sweep status", read_only=True)
async def sweep_status(
    wait_seconds: Annotated[
        float, Field(description="Wait up to this many seconds for a running sweep to finish (0 = just check).")
    ] = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Progress or result of the background sweep started by run_sweep; waits up to wait_seconds for it to
    finish. status is "running" (with progress), "finished" (same result as run_sweep: counts, errors,
    summary, top_new), "error", or "idle" when no sweep ran in this server session (then last_run shows the
    most recent sweep recorded in the store, e.g. the scheduled GitHub Actions sweep).
    """
    if wait_seconds and _SWEEPS.running:
        await _SWEEPS.wait(min(float(wait_seconds), 600.0), ctx)
    return _sweep_payload()


@_tool(title="Live keyword search", open_world=True)
async def live_search(
    keywords: Annotated[str, Field(description="Keywords or a job title, e.g. 'sanctions analyst'.")],
    location: Annotated[
        str | None, Field(description="e.g. 'New York, NY', 'Washington, DC', 'London'. Default: anywhere.")
    ] = None,
    sources: Annotated[
        IdList,
        Field(
            description="Search sources to use (e.g. ['usajobs', 'adzuna', 'jobspy']). Default: every available "
            "non-metered search source."
        ),
    ] = None,
    limit: Annotated[int, Field(description="Max items returned (1-100).")] = 25,
    remote: Annotated[bool | None, Field(description="true = remote roles only.")] = None,
    include_metered: Annotated[
        bool, Field(description="Also use credit-metered sources when `sources` is not given.")
    ] = False,
    store_results: Annotated[
        bool, Field(description="Store results on the board (default true) so uids work with get_job.")
    ] = True,
) -> dict[str, Any]:
    """
    Ad hoc keyword search on the search sources right now (USAJOBS, NYC Jobs, Adzuna, JobSpy scraping of
    Indeed/LinkedIn/Google, … whichever list_sources reports available; metered ones only when named in
    `sources` or include_metered=true). Results are classified and scored like sweep results, stored on the
    board (so get_job / set_job_status work on their uids) and returned best first, with per-source errors.

    Use for a specific title or keyword the lane plan may not cover ("OFAC licensing analyst",
    "K&R response consultant"), or to look for a fresh posting before the next sweep. For broad refreshes
    use run_sweep; to store results from the Indeed connector use import_jobs. Show results as markdown links.
    """
    kw = (keywords or "").strip()
    if not kw:
        raise ToolError("keywords must not be empty")
    sweep_mod = _module("jobsweep.sweep", "the sweep engine")
    names = _resolve_sources(sources, runnable=True)
    settings, taxonomy, registry = get_settings(), get_taxonomy(), get_registry()
    store = get_store() if store_results else None
    limit = _clamp(limit, 1, MAX_LIMIT, 25)
    query = SearchQuery(keywords=kw, location=(location or None), remote=remote, limit=max(limit, 25))
    errors: list[dict] = []
    extra = _call_kwargs(
        sweep_mod.live_search, {"registry": registry, "include_metered": include_metered, "errors": errors}
    )
    try:
        jobs = await asyncio.wait_for(
            _maybe_await(sweep_mod.live_search(settings, taxonomy, query, names, store, **extra)),
            timeout=LIVE_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        raise ToolError(
            f"live search timed out after {LIVE_TIMEOUT_S:.0f}s; retry with fewer sources (see list_sources)"
        ) from exc
    jobs = [j for j in jobs or [] if isinstance(j, Job)]
    items = []
    for job in jobs[:limit]:
        row = store.get(job.uid) if store is not None else None
        items.append(job_item(row or job.to_dict()))
    out: dict[str, Any] = {
        "keywords": kw,
        "location": location,
        "total": len(jobs),
        "stored": store is not None,
        "sources_with_results": sorted({j.source for j in jobs}),
        "items": items,
        "errors": [{k: str(e.get(k, ""))[:200] for k in ("source", "error")} for e in errors[:10]],
    }
    if not jobs:
        out["hint"] = (
            "No results. list_sources shows which search sources are available; the Indeed connector (if "
            "connected) can run the same query, then pass its output to import_jobs."
        )
    return out


def _looks_like_url(text: str) -> bool:
    return bool(re.match(r"^(https?://|www\.)", text, re.I) or re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", text, re.I))


async def _check_employer(text: str, store_matches: bool) -> dict[str, Any]:
    det = _module("jobsweep.sources.detect", "ATS detection")
    sweep_mod = _module("jobsweep.sweep", "the sweep engine")
    from .http import make_client
    from .sources import SOURCE_PATHS
    from .sources.base import SourceContext

    settings, registry = get_settings(), get_registry()
    sweepable_ats = set(getattr(det, "SWEEPABLE_ATS", ()) or ()) or (set(SOURCE_PATHS) - {"import"})
    is_url = _looks_like_url(text)
    url = (text if re.match(r"^https?://", text, re.I) else "https://" + text) if is_url else None
    out: dict[str, Any] = {"input": text, "kind": "url" if is_url else "name", "registry": None}
    notes: list[str] = []
    emp: Employer | None = None
    candidates: list[dict[str, Any]] = []

    async with make_client(settings.user_agent) as client:
        ctx = SourceContext(client=client, settings=settings)
        if is_url:
            pairs: list[tuple[str, str]] = []
            direct = det.detect_from_url(url, include_vendors=True)
            if direct:
                pairs.append((direct[0], direct[1]))
            if not direct or direct[0] not in sweepable_ats:
                for a, b in await det.scan_careers_page(ctx, url, include_vendors=True):
                    if (a, b) not in pairs:
                        pairs.append((a, b))
            candidates = [{"ats": a, "board": b, "sweepable": a in sweepable_ats, "via": "url"} for a, b in pairs]
            for e in registry.all():
                if any(e.ats == a and (e.board or "").lower() == b.lower() for a, b in pairs) or (
                    e.careers_url and e.careers_url.rstrip("/").lower() == url.rstrip("/").lower()
                ):
                    emp = e
                    break
        else:
            emp, alts = find_employer(registry, text)
            if alts:
                out["registry_alternatives"] = [{"key": e.key, "name": e.name} for e in alts]
            if emp is not None and emp.sweepable:
                candidates.append({"ats": emp.ats, "board": emp.board, "sweepable": True, "via": "registry"})
            else:
                hits = await det.probe_company(ctx, emp.name if emp is not None else text)
                for h in hits or []:
                    candidates.append(
                        {
                            "ats": h.get("ats"),
                            "board": h.get("board"),
                            "sweepable": True,
                            "open_jobs": h.get("open_jobs"),
                            "titles": list(h.get("titles") or [])[:5],
                            "via": "probe",
                        }
                    )
    if emp is not None:
        out["registry"] = _employer_dict(emp)
    out["candidates"] = candidates[:10]
    best = next((c for c in candidates if c.get("sweepable") and c.get("ats") in sweepable_ats), None)
    out.update(ats=None, board=None, open_jobs=None, matching_jobs=0, entry_level_matches=0, top_jobs=[])
    if best is None:
        vendor = next((c["ats"] for c in candidates if not c.get("sweepable")), None)
        careers = (emp.careers_url if emp is not None else None) or url
        msg = "No supported ATS board found"
        if vendor or (emp is not None and emp.ats_vendor):
            msg += f" (uses {vendor or emp.ats_vendor}, which jobsweep cannot sweep)"
        if careers:
            msg += f"; check by hand at {careers}"
        notes.append(msg + ". A scraping connector (Firecrawl) plus import_jobs can capture its listings.")
        out["notes"] = notes
        return out

    ats, board = str(best["ats"]), str(best["board"])
    if emp is not None and emp.ats == ats and emp.board == board:
        target = emp
    elif emp is not None:
        target = Employer(
            key=emp.key, name=emp.name, groups=list(emp.groups), lanes=list(emp.lanes), ats=ats, board=board,
            careers_url=emp.careers_url,
        )  # fmt: skip
    else:
        token = board.split("/")[0].split("|")[0]
        token = token[3:] if token.startswith("eu:") else token
        name = text if not is_url else token.replace("-", " ").replace("_", " ").title()
        target = Employer(key=_slug(name), name=name, ats=ats, board=board, careers_url=url)
    out.update(ats=ats, board=board)
    taxonomy = get_taxonomy()
    try:
        jobs = await sweep_mod.board_snapshot(settings, taxonomy, target)
    except Exception as exc:  # noqa: BLE001 - SourceUnavailable / HTTP errors are reported, not raised
        notes.append(f"board fetch failed: {type(exc).__name__}: {str(exc)[:300]}")
        out["notes"] = notes
        return out
    jobs = [j for j in jobs or [] if isinstance(j, Job)]
    matching = sorted((j for j in jobs if j.lanes), key=lambda j: _rank_key(j.to_dict()))
    store = get_store() if store_matches and matching else None
    if store is not None:
        res = store.upsert_jobs(matching)
        out["stored"] = len(matching)
        out["new"] = len(res.get("new") or [])
    items = []
    for job in matching[:10]:
        row = store.get(job.uid) if store is not None else None
        items.append(job_item(row or job.to_dict()))
    out.update(
        open_jobs=len(jobs),
        matching_jobs=len(matching),
        entry_level_matches=sum(1 for j in matching if j.entry_level is True),
        top_jobs=items,
    )
    if jobs and not matching:
        out["sample_titles"] = [j.title for j in jobs[:10]]
        notes.append("Open roles exist but none match the candidate's lanes.")
    if emp is None:
        lane_counts = Counter(lane for j in matching for lane in j.lanes[:1])
        out["suggested_registry_entry"] = {
            "key": target.key,
            "name": target.name,
            "ats": ats,
            "board": board,
            "careers_url": target.careers_url,
            "lanes": [lane for lane, _ in lane_counts.most_common(3)],
            "open_jobs": len(jobs),
        }
        notes.append("Not in the employer registry: add suggested_registry_entry to catalog/employers/*.yaml.")
    elif not emp.sweepable:
        notes.append(f"Registry lists {emp.name} as not sweepable; this board ({ats}:{board}) could be added.")
    out["notes"] = notes
    return out


@_tool(title="Check an employer's board", open_world=True)
async def check_employer(
    name_or_url: Annotated[
        str,
        Field(description="Company name (e.g. 'Control Risks', 'Dataminr') or a careers / job posting URL."),
    ],
    store_matches: Annotated[
        bool, Field(description="Store lane-matching jobs on the board so they can be triaged (default true).")
    ] = True,
    timeout_seconds: Annotated[float, Field(description="Give up after this many seconds.")] = 90,
) -> dict[str, Any]:
    """
    Check one employer's live job board: identify its ATS (Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
    Workable, …) and list its open roles that fit the candidate's lanes. Use when the user asks "is X hiring?",
    "what does X have open?", or names a company that may not be tracked yet.

    A name is matched against the employer registry (fuzzy, e.g. 'control risks' -> control-risks) and, when
    the employer has no known board, probed on the public ATS APIs; a URL is recognised directly or its page is
    scanned for ATS links. Returns ats, board, open_jobs (all openings), matching_jobs, entry_level_matches,
    top_jobs (best 10 lane matches, entry-level first), registry info or suggested_registry_entry (for an
    untracked employer), candidates (boards found) and notes (e.g. "no supported ATS: check by hand at
    <careers_url>"). Show top_jobs as markdown links.
    """
    text = (name_or_url or "").strip()
    if not text:
        raise ToolError("name_or_url must not be empty")
    try:
        return await asyncio.wait_for(
            _check_employer(text, store_matches), timeout=max(float(timeout_seconds or 90), 5.0)
        )
    except asyncio.TimeoutError as exc:
        raise ToolError(f"checking {text!r} timed out after {timeout_seconds:.0f}s") from exc


def _import_parser() -> Callable[..., Any]:
    """``ImportSource.parse`` (tests replace this)."""
    from .sources.manual_import import ImportSource

    return ImportSource.parse


def _fallback_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if isinstance(payload, dict):
        for key in ("jobs", "items", "results", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    return [r for r in payload or [] if isinstance(r, dict)]


def _fallback_parse(payload: Any, origin: str = "claude-connector", fmt: str | None = None) -> list[Job]:
    """Minimal JSON importer used only when sources.manual_import is unavailable."""
    import hashlib

    jobs = []
    for rec in _fallback_records(payload):
        title = str(rec.get("title") or rec.get("job_title") or rec.get("jobTitle") or "").strip()
        company = str(rec.get("company") or rec.get("company_name") or rec.get("companyName") or "").strip()
        url = str(rec.get("url") or rec.get("job_url") or rec.get("link") or "").strip()
        if not title or not (company or url):
            continue
        rid = rec.get("id") or rec.get("job_id") or hashlib.sha1(f"{url}|{title}|{company}".encode()).hexdigest()[:12]
        jobs.append(
            Job(
                source="import",
                source_id=f"{origin}:{rid}",
                title=title,
                company=company,
                url=url,
                location=str(rec.get("location") or ""),
                description=str(rec.get("description") or "")[:12000],
                posted_at=rec.get("posted_at") or rec.get("date_posted"),
                tags=[origin],
            )
        )
    return jobs


def _parse_import(payload: Any, origin: str, fmt: str | None) -> list[Job]:
    try:
        parse = _import_parser()
    except Exception as exc:  # noqa: BLE001
        log.warning("manual_import unavailable (%s); using the minimal JSON importer", exc)
        parse = _fallback_parse
    kwargs = _call_kwargs(parse, {"origin": origin, "fmt": fmt})
    result = parse(payload, **kwargs)
    return [j if isinstance(j, Job) else Job.from_dict(j) for j in result or []]


@_tool(title="Import jobs from connectors")
def import_jobs(
    jobs: Annotated[
        list[dict[str, Any]] | dict[str, Any] | None,
        Field(
            description="Job objects with keys like title, company, url, location, description, salary, "
            "posted_at, id (aliases such as jobTitle / companyName / job_url are understood)."
        ),
    ] = None,
    text: Annotated[
        str,
        Field(
            description="Raw, unmodified output of a job connector tool (Indeed search_jobs / get_job_details "
            "markdown, numbered [Title](url) lists, markdown tables, CSV or JSON)."
        ),
    ] = "",
    origin: Annotated[
        str,
        Field(
            description="Where the jobs came from: indeed-connector, workopia, jobs-and-careers, "
            "usajobs-connector, theirstack, apify, firecrawl, manual, …"
        ),
    ] = "claude-connector",
    fmt: Annotated[
        Literal["json", "jsonl", "csv", "tsv", "md"] | None,
        Field(description="Force a parser; default auto-detects."),
    ] = None,
) -> dict[str, Any]:
    """
    Save jobs found through other Claude connectors (or pasted by the user) onto the jobsweep board, classified
    and scored like swept jobs, so they appear in search_jobs, digest and the web board and can be triaged.

    Pass `text` = the raw output of a job connector tool exactly as returned — e.g. the Indeed connector's
    search_jobs markdown (blocks of **Job Title:** … **Job Id:** … **View Job URL:** …) — and/or `jobs` = a
    list of job objects. Do not summarise or rewrite connector output first: the parser needs the original
    fields and links. `origin` names the connector; it becomes part of each uid (import:<origin>:<id>), so
    importing the same results again updates them instead of duplicating.

    Returns {"imported", "new", "updated", "matched" (jobs that fit a lane), "items": the 10 best imported jobs}.
    Mention the strongest new matches to the user as markdown links.
    """
    has_text = bool(text and text.strip())
    if not jobs and not has_text:
        raise ToolError("pass `text` (raw connector output) or `jobs` (a list of job objects)")
    origin_slug = _slug(origin or "claude-connector")
    parsed: list[Job] = []
    if has_text:
        parsed += _parse_import(text, origin_slug, fmt)
    if jobs:
        parsed += _parse_import(jobs, origin_slug, None)
    unique: dict[str, Job] = {}
    for job in parsed:
        unique.setdefault(job.uid, job)
    parsed = list(unique.values())
    if not parsed:
        return {
            "imported": 0,
            "new": 0,
            "items": [],
            "hint": "No jobs recognised. Pass the connector output unchanged in `text`, or `jobs` as objects with "
            "at least title plus company or url.",
        }
    warnings: list[str] = []
    tax = _taxonomy_or_none()
    if tax is not None:
        parsed = _classify(tax, parsed, get_registry())
    else:
        warnings.append("taxonomy unavailable: jobs stored unclassified")
    store = get_store()
    res = store.upsert_jobs(parsed)
    rows = [store.get(j.uid) or j.to_dict() for j in parsed]
    rows.sort(key=_rank_key)
    out: dict[str, Any] = {
        "imported": len(parsed),
        "new": len(res.get("new") or []),
        "updated": res.get("updated", 0),
        "duplicates_of_existing": len(res.get("duplicates") or []),
        "matched": sum(1 for r in rows if r.get("lanes")),
        "entry_level": sum(1 for r in rows if r.get("entry_level") is True),
        "origin": origin_slug,
        "items": [job_item(r) for r in rows[:10]],
    }
    if warnings:
        out["warnings"] = warnings
    return out


@_tool(title="List job sources", read_only=True)
def list_sources() -> dict[str, Any]:
    """
    Which job sources jobsweep can use right now: every adapter (ATS boards, USAJOBS, NYC Jobs, NY State,
    Adzuna, TheirStack, HasData Indeed, JobSpy, Apify, import) with available / reason, kind (board or
    search), whether it is metered (costs credits), and the environment variables that enable it (set them in
    jobsweep/.env or $JOBSWEEP_HOME/.env, then restart the MCP server; values are never shown).

    Also returns claude_connectors: the claude.ai connectors to pair with jobsweep (Indeed connector tools
    search_jobs / get_job_details / get_company_data, USAJOBS via Pipeworx, TheirStack, Apify actors, Exa,
    Firecrawl, …) and how to feed their output into import_jobs. Use when planning a sweep, when a source
    returns nothing, or when the user asks how to add coverage.
    """
    from . import sources as sources_mod

    settings = get_settings()
    rows = []
    for name in sources_mod.SOURCE_PATHS:
        cls = sources_mod.get_source_class(name)
        if cls is None:
            rows.append({"name": name, "available": False, "reason": "adapter missing or failed to import"})
            continue
        try:
            ok, reason = cls.availability(settings)
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"availability check failed: {exc}"
        row: dict[str, Any] = {
            "name": name,
            "label": cls.label or name,
            "kind": cls.kind,
            "available": bool(ok),
            "reason": reason,
            "metered": bool(cls.metered),
        }
        env_names = list(dict.fromkeys([*cls.requires_env, *_OPTIONAL_ENV.get(name, ())]))
        if env_names:
            row["env"] = [
                {
                    "var": var,
                    "set": bool(settings.get(var)),
                    "required": var in cls.requires_env,
                    "about": ENV_VARS.get(var, ""),
                }
                for var in env_names
            ]
        if cls.docs_url:
            row["docs_url"] = cls.docs_url
        rows.append(row)
    available = [r["name"] for r in rows if r.get("available")]
    return {
        "sources": rows,
        "available": available,
        "unavailable": [r["name"] for r in rows if not r.get("available")],
        "env_files": "jobsweep/.env, ./.env, $JOBSWEEP_HOME/.env or ~/.config/jobsweep/.env (see .env.example)",
        "claude_connectors": CLAUDE_CONNECTORS,
    }


@_tool(title="New-jobs digest", read_only=True, structured=False)
def digest(
    days: Annotated[float, Field(description="New matches first seen within this many days (1 = today).")] = 1,
    min_score: Annotated[
        float | None, Field(description="Minimum fit score; default the profile threshold (about 25).")
    ] = None,
    limit_per_lane: Annotated[int, Field(description="Max jobs listed per lane (0 = all).")] = 10,
) -> str:
    """
    Markdown digest of NEW matches first seen within the last `days` days, grouped by lane group -> lane,
    entry-level jobs first; each line is a [Title](url) link with company, location, score and posted /
    closing dates. Use for the daily briefing after run_sweep / import_jobs, or "what's new this week"
    (days=7). Present it to the user with the links intact (summarise the strongest matches first if long).
    """
    export = _module("jobsweep.export", "the digest exporter")
    kwargs = _call_kwargs(
        export.digest,
        {"taxonomy": _taxonomy_or_none(), "limit_per_lane": _clamp(limit_per_lane, 0, 1000, 10) or None},
    )
    return export.digest(get_store(), days=days, min_score=min_score, fmt="md", **kwargs)


@_tool(title="List tracked employers", read_only=True)
def list_employers(
    group: Annotated[str | None, Field(description="Only employers in this group id.")] = None,
    manual_only: Annotated[
        bool, Field(description="Only employers without a sweepable ATS board ('check by hand').")
    ] = False,
    lane: Annotated[str | None, Field(description="Only employers that are a prior for this lane id.")] = None,
    query: Annotated[str | None, Field(description="Substring of the employer name or key.")] = None,
    limit: Annotated[int, Field(description="Max employers returned (1-500).")] = 100,
) -> dict[str, Any]:
    """
    The employer registry jobsweep tracks: sweepable employers (ats + board, fetched on every sweep) and manual
    ones (no supported ATS: careers_url + ats_vendor, to check by hand or with a scraping connector). Filter by
    group id ({GROUP_IDS}), lane id, or name text. Use to answer "which companies are covered?", to find
    employer keys for run_sweep(employers=[...]), or to list careers pages worth visiting manually.
    """
    registry = get_registry()
    lane_ids, group_ids = _resolve_ids(lane, group)
    needle = (query or "").strip().lower()
    picked = []
    for emp in sorted(registry.all(), key=lambda e: e.name.lower()):
        if not emp.active:
            continue
        if manual_only and emp.sweepable:
            continue
        if group_ids and not set(group_ids) & set(emp.groups):
            continue
        if lane_ids and not set(lane_ids) & set(emp.lanes):
            continue
        if needle and needle not in emp.name.lower() and needle not in emp.key:
            continue
        picked.append(emp)
    limit = _clamp(limit, 1, 500, 100)
    items = []
    for emp in picked[:limit]:
        item: dict[str, Any] = {"key": emp.key, "name": emp.name, "sweepable": emp.sweepable}
        if emp.sweepable:
            item.update(ats=emp.ats, board=emp.board)
        else:
            item.update(ats_vendor=emp.ats_vendor or emp.ats, careers_url=emp.careers_url)
        item.update(groups=list(emp.groups), lanes=list(emp.lanes)[:5])
        if emp.verified:
            item["verified"] = emp.verified
        items.append(item)
    return {
        "total": len(picked),
        "sweepable": sum(1 for e in picked if e.sweepable),
        "manual": sum(1 for e in picked if not e.sweepable),
        "returned": len(items),
        "employers": items,
    }


# ---- prompts -------------------------------------------------------------------------------------

_INDEED_LOCATIONS = '"New York, NY", "Washington, DC" and "remote" (country_code "US")'


@mcp.prompt(
    title="Daily job sweep",
    description="Run the full daily routine: jobsweep sweep + Indeed/other job connectors + digest + triage.",
)
def daily_sweep(focus: str = "") -> str:
    """Step-by-step instructions for Claude's daily job sweep."""
    focus = (focus or "").strip()
    focus_line = (
        f"Today's focus: {focus}. Map it to lane/group ids with list_lanes and restrict every step to them."
        if focus
        else "No special focus: cover every active lane group."
    )
    return f"""Run my daily job sweep with the jobsweep tools and my job-search connectors.

About me: early-career candidate; targets are specialty insurance (incl. Lloyd's/London market), intelligence
& security, investigations & due diligence, geopolitical risk, sanctions / export controls / financial crime,
trust & safety, policy research, strategic comms, and NYC / NYS / federal government (incl. Pathways GS-5..9),
plus graduate / analyst / fellowship programmes. Locations: New York, Washington DC, remote (US), London.
{focus_line}

1. Refresh the board: call run_sweep() (defaults = every available non-metered source; pass lanes/groups for
   a focus). If it returns status "running", call sweep_status(wait_seconds=45) until it is "finished" or
   "error". Note any failing sources briefly; do not retry them.

2. Connector pass. Call list_lanes() and, for each active lane group (or only the focus lanes), take the
   1-2 most relevant lane queries, preferring entry-level phrasing (analyst, associate, assistant, coordinator,
   graduate, fellow). For each query call the Indeed connector's search_jobs tool (in Claude Code:
   mcp__Indeed__search_jobs) with search=<query>, location in {_INDEED_LOCATIONS}; for
   specialty-insurance, geopolitical-risk and intelligence-security also try location "London" with
   country_code "GB". Right after each call, pass its raw markdown output unchanged to
   import_jobs(text=<output>, origin="indeed-connector") (never summarise it first). Keep it to about 30-40
   connector searches; skip a location once it returns nothing relevant.
   If other job connectors are connected, use them the same way and import with an origin naming them:
   Workopia job_tool (origin "workopia"), Jobs & Careers job-search ("jobs-and-careers"), USAJOBS via
   Pipeworx ("usajobs-connector", e.g. Pathways / recent-graduates searches), TheirStack ("theirstack"),
   Apify actors ("apify"). If the Indeed connector is not available, say so and continue.

3. Call digest(days=1) for everything new since yesterday (sweep + imports).

4. Present the result: the top entry-level matches grouped by lane group -> lane, at most ~5 per lane,
   strongest first. Each job as a clickable markdown link: [Title](url) — Company · Location · salary if
   known · closes YYYY-MM-DD if known. Flag deadlines within 14 days and federal/NYC postings with fixed
   closing dates. Skip roles that are clearly senior (Senior/Lead/Director/Manager, 5+ years). Use get_job
   on borderline jobs to check requirements. Never invent URLs.

5. Triage: propose which jobs to save and which to dismiss (one short reason each). After I confirm (or
   straight away if I asked you to triage on your own), call set_job_status(uid, "saved" | "dismissed",
   note=<reason>). Never mark jobs applied/interviewing/offer/rejected unless I say so. End with a one-line
   summary: new jobs found, saved, dismissed, sources that failed."""


@mcp.prompt(
    title="Triage new jobs",
    description="Review untriaged jobs on the board and save or dismiss them with the user.",
)
def triage_inbox(focus: str = "", limit: int = 20) -> str:
    """Instructions for reviewing the 'new' jobs on the board."""
    focus = (focus or "").strip()
    focus_line = (
        f"Only look at: {focus} (map it to lane/group ids with list_lanes)." if focus else "Cover all lanes."
    )
    n = _clamp(limit, 1, 100, 20)
    return f"""Help me triage my jobsweep inbox. {focus_line}

1. Call search_jobs(statuses=["new"], entry_level_only=true, sort="score", limit={n}) (add lanes/groups for
   the focus). Also call search_jobs(statuses=["saved"], sort="closing", limit=10) to catch saved jobs with
   deadlines coming up.
2. For each new job decide: save (good fit: entry level, in my lanes, NYC / DC / remote US / London),
   dismiss (wrong seniority, off-target domain, wrong location), or unsure. Use get_job when the title alone
   is not enough (check years_required, match_reasons, deadline, clearance or citizenship requirements).
3. Show a compact list grouped by recommendation; each job as [Title](url) — Company · Location · score ·
   one-line reason. Show saved jobs closing within 14 days separately.
4. Ask me to confirm or adjust, then call set_job_status(uid, "saved" | "dismissed", note=<short reason>) for
   each decision. Only use applied / interviewing / offer / rejected when I say so.
5. Finish with counts (saved, dismissed, left as new)."""


# ---- resources -----------------------------------------------------------------------------------


@mcp.resource(
    "jobsweep://lanes",
    name="lanes",
    title="Lane taxonomy",
    description="Groups and lanes (ids, names, active job counts, search queries) as JSON.",
    mime_type="application/json",
)
def lanes_resource() -> str:
    return json.dumps(list_lanes(include_inactive=True, include_queries=True), ensure_ascii=False)


@mcp.resource(
    "jobsweep://digest/today",
    name="digest-today",
    title="Today's digest",
    description="Markdown digest of matches first seen in the last 24 hours, grouped by lane.",
    mime_type="text/markdown",
)
def digest_today_resource() -> str:
    return digest(days=1)


# ---- entry point ---------------------------------------------------------------------------------


async def _serve_stdio(protocol_out: Any) -> None:
    """``FastMCP.run_stdio_async`` with an explicit stdout, so ``sys.stdout`` can point at stderr."""
    from mcp.server.stdio import stdio_server

    stdout = anyio.wrap_file(io.TextIOWrapper(protocol_out, encoding="utf-8"))
    server = mcp._mcp_server
    async with stdio_server(stdout=stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server (stdio by default). Logs go to stderr; stdout carries only the protocol."""
    parser = argparse.ArgumentParser(prog="jobsweep-mcp", description="jobsweep MCP server for Claude")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--log-level", default=os.environ.get("JOBSWEEP_LOG_LEVEL", "WARNING"))
    args, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    level = getattr(logging, str(args.log_level).upper(), logging.WARNING)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    root.setLevel(level)
    real_stdout = sys.stdout
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is real_stdout:
            handler.setStream(sys.stderr)

    if args.transport != "stdio":
        mcp.run(transport=args.transport)
        return 0
    protocol_out = getattr(real_stdout, "buffer", None)
    if protocol_out is None or not hasattr(mcp, "_mcp_server"):
        mcp.run()
        return 0
    sys.stdout = sys.stderr  # stray print() from libraries must never corrupt the protocol stream
    try:
        anyio.run(_serve_stdio, protocol_out)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = real_stdout
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
