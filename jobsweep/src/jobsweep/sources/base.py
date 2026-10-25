"""Source adapter contract.

Two kinds of adapters:

* ``kind = "board"``  — per-employer ATS boards (Greenhouse, Lever, Ashby, Workday, ...).
  Implement ``fetch_board(employer)``; return every open job on that board (the sweep applies
  ``employer.title_filter`` and classification afterwards).
* ``kind = "search"`` — keyword search APIs (USAJOBS, NYC Jobs, Adzuna, TheirStack, Indeed via HasData,
  JobSpy, Apify actors). Implement ``search(query)``.

An adapter may implement both (e.g. Apify). Adapters never classify jobs and never touch the store.
Errors: raise ``SourceError`` (or let ``jobsweep.http.HTTPError`` propagate) — the sweep records it
per target and continues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from ..config import Settings
from ..http import DEFAULT_HOST_LIMITS, HostLimiter, fetch_json, fetch_text
from ..models import Employer, Job, SearchQuery


class SourceError(RuntimeError):
    pass


class SourceUnavailable(SourceError):
    """Raised when credentials or an optional dependency are missing."""


@dataclass
class SourceContext:
    client: httpx.AsyncClient
    settings: Settings
    limiter: HostLimiter = field(default_factory=lambda: HostLimiter(4, DEFAULT_HOST_LIMITS))
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("jobsweep.sources"))


class Source:
    #: registry name, also stored in Job.source
    name: ClassVar[str] = ""
    #: "board", "search" or "board+search"
    kind: ClassVar[str] = "board"
    #: human label for UI/docs
    label: ClassVar[str] = ""
    #: env vars that must ALL be set for the adapter to work (empty = no credentials needed)
    requires_env: ClassVar[tuple[str, ...]] = ()
    #: python module that must be importable (optional dependency), e.g. "jobspy"
    requires_module: ClassVar[str | None] = None
    #: True when calls cost money/credits — the sweep only runs these when explicitly enabled
    metered: ClassVar[bool] = False
    #: docs URL for setup
    docs_url: ClassVar[str] = ""
    #: default cap on search queries per sweep (None = no cap). Override ``query_budget`` for dynamic caps.
    max_queries: ClassVar[int | None] = None

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx

    # ---- availability -------------------------------------------------------------------------
    @classmethod
    def availability(cls, settings: Settings) -> tuple[bool, str]:
        """(available, reason). Override when there is a credential-free fallback (USAJOBS -> Pipeworx)."""
        missing = [k for k in cls.requires_env if not settings.get(k)]
        if missing:
            return False, "missing env: " + ", ".join(missing)
        if cls.requires_module:
            import importlib.util

            if importlib.util.find_spec(cls.requires_module) is None:
                return False, f"optional dependency '{cls.requires_module}' not installed"
        return True, "ok"

    @classmethod
    def query_budget(cls, settings: Settings) -> int | None:
        """Max keyword queries this source may run in one sweep (free-tier quotas, politeness)."""
        return cls.max_queries

    # ---- operations ---------------------------------------------------------------------------
    async def fetch_board(self, employer: Employer) -> list[Job]:
        raise NotImplementedError(f"{self.name} does not support employer boards")

    async def search(self, query: SearchQuery) -> list[Job]:
        raise NotImplementedError(f"{self.name} does not support keyword search")

    async def enrich(self, job: Job) -> Job:
        """Fill ``job.description`` (and other detail fields) when the listing call omitted them.

        The sweep calls this only for promising jobs (title-level match), so list endpoints that lack
        descriptions (Workday, SmartRecruiters, Oracle, ...) stay cheap. Default: no-op.
        """
        return job

    # ---- helpers for subclasses ---------------------------------------------------------------
    async def get_json(self, url: str, **kw):  # noqa: ANN201
        return await fetch_json(self.ctx.client, url, limiter=self.ctx.limiter, **kw)

    async def post_json(self, url: str, payload, **kw):  # noqa: ANN001, ANN201
        return await fetch_json(
            self.ctx.client, url, method="POST", json_body=payload, limiter=self.ctx.limiter, **kw
        )

    async def get_text(self, url: str, **kw) -> str:
        return await fetch_text(self.ctx.client, url, limiter=self.ctx.limiter, **kw)
