"""Core data model shared by every module.

Contract notes (do not change field names without updating ARCHITECTURE.md):
- ``Job`` is what every source adapter returns. Adapters fill the *source* fields only;
  the classifier (``taxonomy.Taxonomy.classify``) fills the *classification* fields.
- ``Job.uid`` (``"<source>:<source_id>"``) is the primary key in the store.
- Dates are ISO-8601 strings (``YYYY-MM-DD`` or full datetimes) or ``None``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Statuses a user can put a job in (store + web UI + MCP use the same vocabulary).
JOB_STATUSES: tuple[str, ...] = (
    "new",
    "saved",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "dismissed",
)


def _norm(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Job:
    # ---- source fields (filled by adapters) ----
    source: str  # adapter name, e.g. "greenhouse", "usajobs", "nycjobs"
    source_id: str  # stable id inside that source
    title: str
    company: str
    url: str  # public posting / apply URL
    location: str = ""  # human readable, "; "-joined when several
    remote: bool | None = None  # True remote, False on-site/hybrid, None unknown
    description: str = ""  # plain text (no HTML), truncated to ~12k chars by adapters
    posted_at: str | None = None  # ISO date/datetime
    closes_at: str | None = None  # ISO date/datetime (application deadline)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None  # ISO 4217, e.g. "USD"
    salary_period: str | None = None  # "year" | "month" | "week" | "day" | "hour"
    employment_type: str | None = None  # "full-time", "part-time", "contract", "internship", "temporary", ...
    department: str | None = None
    seniority: str | None = None  # source-provided level text if any
    grade: str | None = None  # e.g. "GS-07/09" for federal, NYC civil service level, etc.
    series: list[str] = field(default_factory=list)  # occupational series codes (federal), title codes (NYC)
    employer_key: str | None = None  # key in the employer registry when fetched from a known board
    tags: list[str] = field(
        default_factory=list
    )  # source hints: "pathways", "recent-graduates", "fellowship", ...
    extra: dict[str, Any] = field(default_factory=dict)  # small source-specific extras (JSON-serialisable)

    # ---- classification fields (filled by taxonomy.Taxonomy.classify) ----
    lanes: list[str] = field(default_factory=list)  # matched lane ids, best first
    groups: list[str] = field(default_factory=list)  # matched group ids, best first
    score: float = 0.0  # overall fit 0..100
    lane_scores: dict[str, float] = field(default_factory=dict)  # lane id -> relevance score
    entry_level: bool | None = None  # True = entry/early-career fit, False = too senior, None = unclear
    years_required: int | None = None  # minimum years of experience parsed from the text
    match_reasons: list[str] = field(default_factory=list)  # short human readable explanations

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def fingerprint(self) -> str:
        """Cross-source duplicate key: normalised company | title | first location token."""
        loc = _norm(self.location.split(";")[0].split(",")[0]) if self.location else ""
        company = re.sub(
            r"\b(inc|llc|ltd|plc|corp|corporation|co|company|group|the)\b", "", _norm(self.company)
        )
        company = re.sub(r"\s+", " ", company).strip()
        return f"{company}|{_norm(self.title)}|{loc}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["uid"] = self.uid
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Employer:
    """An organisation in the employer registry (catalog/employers/*.yaml)."""

    key: str  # unique slug, e.g. "dataminr"
    name: str
    groups: list[str] = field(default_factory=list)  # taxonomy group ids this employer is relevant to
    lanes: list[str] = field(default_factory=list)  # taxonomy lane ids this employer is a strong prior for
    ats: str | None = None  # adapter name ("greenhouse", "lever", "workday", ...) or None/"other"
    board: str | None = None  # board identifier, format depends on ats (see ARCHITECTURE.md)
    careers_url: str | None = None  # human careers page (always fill when known)
    ats_vendor: str | None = None  # when ats is None/"other": vendor name for humans ("icims", "taleo", ...)
    hq: str | None = None
    locations: list[str] = field(default_factory=list)  # offices of interest
    title_filter: str | None = None  # optional regex; when set, only titles matching it are kept (big boards)
    search_terms: list[str] = field(
        default_factory=list
    )  # server-side searches for huge boards (Workday/Oracle/...)
    max_jobs: int | None = None  # cap on jobs pulled from this board per sweep (default per adapter)
    notes: str = ""
    verified: str | None = None  # ISO date the board was last confirmed to return jobs
    open_jobs: int | None = None  # job count seen at verification time
    active: bool = True

    @property
    def sweepable(self) -> bool:
        return bool(self.active and self.ats and self.ats != "other" and self.board)

    @classmethod
    def from_dict(cls, data: dict[str, Any], group: str | None = None) -> Employer:
        names = {f.name for f in fields(cls)}
        emp = cls(**{k: v for k, v in data.items() if k in names})
        if group and group not in emp.groups:
            emp.groups = [group, *emp.groups]
        return emp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchQuery:
    """A keyword search sent to search-type sources (USAJOBS, Adzuna, NYC Jobs, Indeed, ...)."""

    keywords: str
    location: str | None = None
    remote: bool | None = None
    lane: str | None = None  # lane id that produced this query (for logging/attribution)
    posted_within_days: int | None = None
    limit: int = 50
    # Source-specific parameters. Known keys:
    #   usajobs: {"series": ["0132"], "hiring_path": ["graduates"], "grade_low": "05", "grade_high": "09",
    #             "organization": "HSCB", "position_title": "..."}
    #   nycjobs: {"agencies": ["DEPT OF INVESTIGATION"], "career_level": ["Entry-Level"]}
    #   adzuna:  {"country": "us", "category": "..."}
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Profile:
    """The candidate's preferences used for scoring (catalog/profile.yaml, overridable by the user)."""

    preferred_locations: list[str] = field(
        default_factory=lambda: ["New York", "Washington", "Remote", "London"]
    )
    remote_ok: bool = True
    max_years_experience: int = 3  # jobs requiring more than this are marked not entry-level
    include_internships: bool = False
    include_unknown_seniority: bool = True
    min_score: float = 25.0  # jobs below this are stored but hidden by default
    countries: list[str] = field(default_factory=lambda: ["US", "GB"])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceResult:
    """What a sweep records per source call."""

    source: str
    target: str  # employer key or query keywords
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    elapsed_s: float = 0.0
