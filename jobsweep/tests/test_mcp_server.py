"""Tests for the MCP server (jobsweep.mcp_server).

Tools are exercised through the SDK's in-memory client against a temp SQLite DB (``JOBSWEEP_DB`` + reset
module cache). The taxonomy is a small keyword fake, and the sweep / detection / export modules are
replaced by fakes in ``sys.modules``, so these tests do not depend on other components being finished.
Integration tests against the real modules skip when those modules are missing; the live test only runs with
``-m live`` (or ``JOBSWEEP_LIVE=1``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import types
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from jobsweep import mcp_server as ms
from jobsweep.config import Settings
from jobsweep.employers import EmployerRegistry
from jobsweep.models import Employer, Job, Profile

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _live_gate(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live"):
        expr = (request.config.getoption("markexpr", "") or "").replace("not live", "")
        if "live" not in expr and os.environ.get("JOBSWEEP_LIVE") != "1":
            pytest.skip("live test: run with -m live")


# ---- fakes ---------------------------------------------------------------------------------------

RULES = {
    "political risk": ("political-risk", "specialty-insurance"),
    "sanctions": ("sanctions", "sanctions-fincrime"),
    "gsoc": ("gsoc-watch", "intelligence-security"),
}


class FakeTaxonomy:
    """Keyword classifier with the Taxonomy surface the server uses."""

    def __init__(self) -> None:
        self.profile = Profile(min_score=25)
        lanes_by_group: dict[str, list[str]] = {}
        for lane, grp in RULES.values():
            lanes_by_group.setdefault(grp, []).append(lane)
        self.groups = [
            SimpleNamespace(
                id=g,
                name=g.title(),
                active=True,
                lanes=[
                    SimpleNamespace(id=lid, name=lid.title(), active=True, queries=[lid + " analyst"])
                    for lid in ls
                ],
            )
            for g, ls in lanes_by_group.items()
        ]
        self.groups.append(
            SimpleNamespace(
                id="earlier-lanes",
                name="Earlier",
                active=False,
                lanes=[SimpleNamespace(id="real-estate", name="Real estate", active=False, queries=[])],
            )
        )
        self.lanes = {lane.id: lane for g in self.groups for lane in g.lanes}

    def classify(self, job: Job, employer: Employer | None = None) -> Job:
        text = f"{job.title} {job.description}".lower()
        job.lanes, job.groups = [], []
        for term, (lane, grp) in RULES.items():
            if term in text:
                job.lanes.append(lane)
                if grp not in job.groups:
                    job.groups.append(grp)
        title = job.title.lower()
        if "senior" in title or "director" in title:
            job.entry_level = False
        elif "analyst" in title or "associate" in title:
            job.entry_level = True
        else:
            job.entry_level = None
        job.score = (60.0 if job.lanes else 5.0) + (20.0 if job.entry_level else 0.0)
        job.match_reasons = [f"fake: {lane}" for lane in job.lanes]
        return job

    def title_signal(self, title: str, employer: Employer | None = None) -> float:
        return 10.0

    def to_dict(self) -> dict:
        return {
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "active": g.active,
                    "lanes": [
                        {
                            "id": lane.id,
                            "name": lane.name,
                            "active": lane.active,
                            "group": g.id,
                            "queries": lane.queries,
                        }
                        for lane in g.lanes
                    ],
                }
                for g in self.groups
            ]
        }


def make_registry() -> EmployerRegistry:
    return EmployerRegistry(
        [
            Employer(
                key="acme-risk",
                name="Acme Risk Partners",
                groups=["geopolitical-risk"],
                lanes=["geopolitical-analysis"],
                ats="greenhouse",
                board="acmerisk",
                careers_url="https://acme.example/careers",
            ),
            Employer(
                key="manual-intel",
                name="Manual Intelligence Co",
                groups=["intelligence-security"],
                lanes=["threat-intelligence"],
                ats="other",
                ats_vendor="icims",
                careers_url="https://manual.example/careers",
            ),
        ]
    )


LONG_DESC = "Support the political risk underwriting team in the Lloyd's market. " * 10


def seed_jobs() -> list[Job]:
    return [
        Job(
            source="greenhouse",
            source_id="1",
            title="Political Risk Analyst",
            company="Beazley",
            url="https://boards.greenhouse.io/beazley/jobs/1",
            location="New York, NY",
            description=LONG_DESC,
            salary_min=65000,
            salary_max=75000,
            salary_currency="USD",
            salary_period="year",
            posted_at="2026-10-20",
        ),
        Job(
            source="lever",
            source_id="2",
            title="Senior Director, Sanctions",
            company="Acme Bank",
            url="https://jobs.lever.co/acme/2",
            location="Washington, DC",
        ),
        Job(
            source="usajobs",
            source_id="3",
            title="GSOC Operator",
            company="Department of State",
            url="https://www.usajobs.gov/job/3",
            location="Remote",
            remote=True,
        ),
        Job(source="adzuna", source_id="4", title="Barista", company="Cafe", url="https://cafe.example/4"),
        Job(
            source="nycjobs",
            source_id="5",
            title="Sanctions Associate",
            company="NYC DOI",
            url="https://cityjobs.nyc.gov/5",
            location="New York, NY",
            closes_at="2026-12-01",
        ),
    ]


@dataclass
class FakeSweepOptions:
    sources: list[str] | None = None
    lanes: list[str] | None = None
    groups: list[str] | None = None
    employers: list[str] | None = None
    include_metered: bool = False
    boards: bool = True
    searches: bool = True
    max_queries_per_source: int = 40
    posted_within_days: int | None = 30
    dry_run: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> FakeSweepOptions:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names and v is not None})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FakeReport:
    run_id: int | None = 7
    started_at: str = "2026-10-25T10:00:00+00:00"
    finished_at: str = "2026-10-25T10:05:00+00:00"
    plan: dict = field(default_factory=lambda: {"counts": {"boards": 1, "searches": 2, "by_source": {}}})
    per_source: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    new_uids: list = field(default_factory=list)
    total_jobs: int = 0
    dry_run: bool = False
    expired: int = 0
    deactivated: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return f"Sweep run #{self.run_id}: {len(self.new_uids)} new"


def install_fake(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


# ---- harness -------------------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBSWEEP_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JOBSWEEP_HOME", str(tmp_path))
    ms.reset_state()
    tax = FakeTaxonomy()
    ms.configure(taxonomy=tax, registry=make_registry())
    store = ms.get_store()
    assert Path(store.path) == tmp_path / "jobs.db"
    store.upsert_jobs([tax.classify(j) for j in seed_jobs()])
    yield SimpleNamespace(store=store, tax=tax, tmp=tmp_path)
    thread = ms._SWEEPS.thread
    if thread is not None:
        thread.join(10)
    ms.reset_state()


def call(name: str, args: dict | None = None) -> Any:
    async def go() -> Any:
        async with create_connected_server_and_client_session(ms.mcp) as client:
            return await client.call_tool(name, args or {})

    return asyncio.run(go())


def ok(res: Any) -> Any:
    assert not res.isError, res.content[0].text if res.content else res
    if res.structuredContent is not None:
        return res.structuredContent
    return json.loads(res.content[0].text)


def err(res: Any) -> str:
    assert res.isError, res
    return res.content[0].text


# ---- listing -------------------------------------------------------------------------------------


EXPECTED_TOOLS = {
    "list_lanes",
    "search_jobs",
    "get_job",
    "set_job_status",
    "run_sweep",
    "sweep_status",
    "live_search",
    "check_employer",
    "import_jobs",
    "list_sources",
    "digest",
    "list_employers",
}


def test_lists_tools_prompts_and_resources(board) -> None:
    async def go() -> tuple:
        async with create_connected_server_and_client_session(ms.mcp) as client:
            init_tools = await client.list_tools()
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            prompt = await client.get_prompt("daily_sweep", {"focus": "sanctions"})
            triage = await client.get_prompt("triage_inbox", {"limit": "5"})
            return init_tools, prompts, resources, prompt, triage

    tools, prompts, resources, prompt, triage = asyncio.run(go())
    by_name = {t.name: t for t in tools.tools}
    assert EXPECTED_TOOLS <= set(by_name)
    for tool in by_name.values():
        assert len(tool.description) > 150, tool.name
    # every fixed lane and group id is listed for Claude in the search/sweep docstrings
    for gid, lanes in ms.LANE_GROUPS.items():
        for doc in (by_name["search_jobs"].description, by_name["run_sweep"].description):
            assert gid in doc
            for lane in lanes:
                assert lane in doc
    assert "markdown link" in by_name["search_jobs"].description
    assert by_name["search_jobs"].annotations.readOnlyHint is True
    assert set(by_name["search_jobs"].inputSchema["properties"]["sort"]["enum"]) == set(ms.SORTS)

    assert {p.name for p in prompts.prompts} >= {"daily_sweep", "triage_inbox"}
    assert {str(r.uri) for r in resources.resources} >= {"jobsweep://lanes", "jobsweep://digest/today"}
    text = prompt.messages[0].content.text
    for needle in ("run_sweep", "mcp__Indeed__search_jobs", "import_jobs(text=", 'origin="indeed-connector"',
                   "country_code", "digest(days=1)", "set_job_status", "sanctions", "New York, NY"):  # fmt: skip
        assert needle in text, needle
    assert "limit=5" in triage.messages[0].content.text


# ---- search / get / status -----------------------------------------------------------------------


def test_search_jobs_defaults_and_item_shape(board) -> None:
    data = ok(call("search_jobs"))
    titles = [i["title"] for i in data["items"]]
    # senior job hidden (entry_level_only), barista below the profile threshold
    assert set(titles) == {"Political Risk Analyst", "Sanctions Associate", "GSOC Operator"}
    assert data["total"] == 3
    assert titles[-1] == "GSOC Operator"  # lower score sorts last
    item = next(i for i in data["items"] if i["uid"] == "greenhouse:1")
    assert set(item) == {
        "uid", "title", "company", "location", "remote", "posted_at", "closes_at", "salary", "lanes", "score",
        "entry_level", "status", "url", "first_seen", "snippet",
    }  # fmt: skip
    assert item["salary"] == "$65,000–$75,000/yr"
    assert item["lanes"] == ["political-risk"]
    assert item["score"] == 80
    assert item["status"] == "new"
    assert item["url"] == "https://boards.greenhouse.io/beazley/jobs/1"
    assert len(item["snippet"]) <= 300 and item["snippet"].endswith("…")
    assert item["posted_at"] == "2026-10-20"


def test_search_jobs_filters(board) -> None:
    assert ok(call("search_jobs", {"lanes": ["political-risk"]}))["total"] == 1
    # a group id passed as a lane is accepted; comma-separated strings work
    data = ok(call("search_jobs", {"lanes": "sanctions-fincrime"}))
    assert [i["uid"] for i in data["items"]] == ["nycjobs:5"]
    data = ok(call("search_jobs", {"groups": "intelligence-security,specialty-insurance"}))
    assert {i["uid"] for i in data["items"]} == {"greenhouse:1", "usajobs:3"}
    data = ok(call("search_jobs", {"entry_level_only": False, "groups": ["sanctions-fincrime"]}))
    assert data["total"] == 2
    data = ok(call("search_jobs", {"query": "barista"}))  # a text query drops the default score floor
    assert [i["uid"] for i in data["items"]] == ["adzuna:4"]
    assert ok(call("search_jobs", {"remote": True}))["total"] == 1
    assert ok(call("search_jobs", {"location": "New York"}))["total"] == 2
    assert ok(call("search_jobs", {"sources": ["usajobs"]}))["total"] == 1
    page = ok(call("search_jobs", {"limit": 1}))
    assert page["returned"] == 1 and page["next_offset"] == 1
    assert ok(call("search_jobs", {"min_score": 0, "entry_level_only": False}))["total"] == 5
    assert ok(call("search_jobs", {"sort": "closing", "limit": 1}))["items"][0]["uid"] == "nycjobs:5"
    empty = ok(call("search_jobs", {"query": "nonexistentword"}))
    assert empty["total"] == 0 and "hint" in empty


def test_search_jobs_rejects_unknown_ids(board) -> None:
    msg = err(call("search_jobs", {"lanes": ["politcal-risk"]}))
    assert "political-risk" in msg and "list_lanes" in msg
    assert "valid" in err(call("search_jobs", {"statuses": ["archived"]}))
    assert "list_sources" in err(call("search_jobs", {"sources": ["monster"]}))


def test_get_job_and_set_status(board) -> None:
    job = ok(call("get_job", {"uid": "greenhouse:1"}))
    assert job["description"].startswith("Support the political risk")
    assert job["match_reasons"] == ["fake: political-risk"]
    assert job["salary"] == "$65,000–$75,000/yr"
    assert "no stored job" in err(call("get_job", {"uid": "greenhouse:404"}))

    res = ok(call("set_job_status", {"uid": "greenhouse:1", "status": "Saved", "note": "apply by Nov 15"}))
    assert res["ok"] and res["status"] == "saved" and res["note"] == "apply by Nov 15"
    assert board.store.get("greenhouse:1")["status"] == "saved"
    assert "valid" in err(call("set_job_status", {"uid": "greenhouse:1", "status": "maybe"}))
    assert "no stored job" in err(call("set_job_status", {"uid": "x:1", "status": "saved"}))

    ok(call("set_job_status", {"uid": "usajobs:3", "status": "dismissed"}))
    assert ok(call("search_jobs"))["total"] == 2  # dismissed hidden by default
    dismissed = ok(call("search_jobs", {"statuses": ["dismissed"]}))
    assert [i["uid"] for i in dismissed["items"]] == ["usajobs:3"]
    assert ok(call("search_jobs", {"statuses": ["saved"]}))["items"][0]["uid"] == "greenhouse:1"


# ---- lanes / employers / sources -----------------------------------------------------------------


def test_list_lanes_counts(board) -> None:
    data = ok(call("list_lanes"))
    groups = {g["id"]: g for g in data["groups"]}
    assert "earlier-lanes" not in groups
    lanes = {lane["id"]: lane for g in data["groups"] for lane in g["lanes"]}
    assert lanes["political-risk"]["count"] == 1
    assert lanes["sanctions"]["count"] == 2  # active jobs, any seniority
    assert lanes["political-risk"]["queries"] == ["political-risk analyst"]
    full = ok(call("list_lanes", {"include_inactive": True, "include_queries": False}))
    assert "earlier-lanes" in {g["id"] for g in full["groups"]}
    assert all("queries" not in lane for g in full["groups"] for lane in g["lanes"])


def test_list_lanes_falls_back_to_fixed_ids(board, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise ms.ToolError("lanes.yaml broken")

    monkeypatch.setattr(ms, "get_taxonomy", broken)
    data = ok(call("list_lanes"))
    assert "warning" in data
    ids = {lane["id"] for g in data["groups"] for lane in g["lanes"]}
    assert {"kidnap-ransom", "federal-pathways", "osint"} <= ids


def test_list_employers(board) -> None:
    data = ok(call("list_employers"))
    assert data["total"] == 2 and data["sweepable"] == 1 and data["manual"] == 1
    manual = ok(call("list_employers", {"manual_only": True}))["employers"]
    assert manual == [
        {
            "key": "manual-intel",
            "name": "Manual Intelligence Co",
            "sweepable": False,
            "ats_vendor": "icims",
            "careers_url": "https://manual.example/careers",
            "groups": ["intelligence-security"],
            "lanes": ["threat-intelligence"],
        }
    ]
    geo = ok(call("list_employers", {"group": "geopolitical-risk"}))["employers"]
    assert [e["key"] for e in geo] == ["acme-risk"] and geo[0]["board"] == "acmerisk"
    assert ok(call("list_employers", {"query": "manual"}))["total"] == 1
    assert "geopolitical-risk" in err(call("list_employers", {"group": "geopolitcal-risk"}))


def test_list_sources_reports_env_without_values(board) -> None:
    ms.configure(
        settings=Settings(env={"USAJOBS_API_KEY": "secret-value-xyz", "USAJOBS_EMAIL": "me@example.com"})
    )
    data = ok(call("list_sources"))
    rows = {r["name"]: r for r in data["sources"]}
    assert {"greenhouse", "usajobs", "nycjobs", "theirstack", "import"} <= set(rows)
    if "env" in rows["usajobs"]:
        env = {e["var"]: e for e in rows["usajobs"]["env"]}
        assert env["USAJOBS_API_KEY"]["set"] is True
        assert env["PIPEWORX_API_KEY"]["set"] is False
    if rows["theirstack"].get("label"):  # adapter importable
        assert rows["theirstack"]["metered"] is True
        assert any(e["var"] == "THEIRSTACK_API_KEY" and not e["set"] for e in rows["theirstack"]["env"])
    blob = json.dumps(data)
    assert "secret-value-xyz" not in blob and "me@example.com" not in blob
    names = " ".join(c["name"] for c in data["claude_connectors"])
    for needle in ("Indeed", "USAJOBS", "TheirStack", "Apify", "Exa", "Firecrawl"):
        assert needle in names
    indeed = data["claude_connectors"][0]
    assert "mcp__Indeed__search_jobs" in indeed["claude_code_names"]
    assert set(indeed["tools"]) == {"search_jobs", "get_job_details", "get_company_data"}


# ---- import --------------------------------------------------------------------------------------


def fake_parse(payload: Any, origin: str = "claude-connector", fmt: str | None = None) -> list[Job]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    return [
        Job(
            source="import",
            source_id=f"{origin}:{rec['id']}",
            title=rec["title"],
            company=rec.get("company", ""),
            url=rec.get("url", ""),
            location=rec.get("location", ""),
            tags=[origin],
        )
        for rec in payload
    ]


IMPORT_RECORDS = [
    {"id": "a1", "title": "Sanctions Analyst", "company": "Acme Risk Partners", "url": "https://x.example/1"},
    {"id": "a2", "title": "Office Manager", "company": "Somewhere", "url": "https://x.example/2"},
]


def test_import_jobs_classifies_and_upserts(board, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ms, "_import_parser", lambda: fake_parse)
    data = ok(call("import_jobs", {"jobs": IMPORT_RECORDS, "origin": "Indeed Connector"}))
    assert data["imported"] == 2 and data["new"] == 2 and data["matched"] == 1
    assert data["origin"] == "indeed-connector"
    top = data["items"][0]
    assert top["uid"] == "import:indeed-connector:a1"
    assert top["lanes"] == ["sanctions"] and top["entry_level"] is True
    assert board.store.get("import:indeed-connector:a1")["score"] == 80
    # re-import updates instead of duplicating; text payloads go through the same parser
    again = ok(call("import_jobs", {"text": json.dumps(IMPORT_RECORDS), "origin": "indeed-connector"}))
    assert again["imported"] == 2 and again["new"] == 0 and again["updated"] == 2
    found = ok(call("search_jobs", {"sources": ["import"]}))
    assert [i["uid"] for i in found["items"]] == ["import:indeed-connector:a1"]


def test_import_jobs_errors_and_empty(board, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ms, "_import_parser", lambda: lambda payload, origin="x", fmt=None: [])
    assert "text" in err(call("import_jobs", {}))
    data = ok(call("import_jobs", {"text": "nothing useful here"}))
    assert data["imported"] == 0 and "hint" in data


def test_import_jobs_fallback_parser_when_module_missing(board, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise ImportError("no manual_import")

    monkeypatch.setattr(ms, "_import_parser", missing)
    data = ok(call("import_jobs", {"jobs": IMPORT_RECORDS, "origin": "workopia"}))
    assert data["imported"] == 2
    assert board.store.get("import:workopia:a1")["lanes"] == ["sanctions"]


def test_import_jobs_real_indeed_markdown(board) -> None:
    pytest.importorskip("jobsweep.sources.manual_import")
    text = (FIXTURES / "import" / "indeed_connector_search.md").read_text(encoding="utf-8")
    data = ok(call("import_jobs", {"text": text, "origin": "indeed-connector"}))
    assert data["imported"] == 3 and data["new"] == 3
    uids = {i["uid"] for i in data["items"]}
    assert "import:indeed-connector:JOBSEARCH_102148" in uids
    item = next(i for i in data["items"] if i["uid"].endswith("102148"))
    assert item["url"] == "https://to.indeed.com/aaskwm2qqgzy"
    assert item["salary"] == "$65,000–$75,000/yr"


# ---- sweep ---------------------------------------------------------------------------------------


def _sweep_jobs(tax: FakeTaxonomy) -> list[Job]:
    jobs = [
        Job(
            source="greenhouse", source_id="s1", title="Sanctions Analyst", company="Acme", url="https://a/1"
        ),
        Job(
            source="greenhouse",
            source_id="s2",
            title="Senior Sanctions Lead",
            company="Acme",
            url="https://a/2",
        ),
    ]
    return [tax.classify(j) for j in jobs]


def test_run_sweep_returns_report_and_top_new(board, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def run_sweep(settings, taxonomy, registry, store, options=None, progress=None):  # noqa: ANN001, ANN202
        seen["options"] = options
        progress({"event": "plan", "total": 3})
        progress({"event": "target_done", "ok": True, "jobs": 2, "new": 2, "done": 1, "total": 3})
        res = store.upsert_jobs(_sweep_jobs(taxonomy), 7)
        return FakeReport(
            new_uids=res["new"],
            total_jobs=2,
            per_source={"greenhouse": {"targets": 1, "ok": 1, "errors": 0, "jobs": 2, "kept": 2, "new": 2}},
            errors=[{"source": "usajobs", "target": "osint", "error": "HTTP 429"}],
        )

    install_fake(monkeypatch, "jobsweep.sweep", SweepOptions=FakeSweepOptions, run_sweep=run_sweep)
    data = ok(call("run_sweep", {"lanes": ["sanctions"], "employers": ["Acme Risk"], "wait_seconds": 10}))
    assert data["status"] == "finished" and data["started"] is True
    assert data["counts"]["new"] == 2 and data["counts"]["boards_planned"] == 1
    assert data["counts"]["jobs_fetched"] == 2 and data["errors_total"] == 1
    assert data["summary"].startswith("Sweep run #7")
    assert [i["uid"] for i in data["top_new"]] == ["greenhouse:s1", "greenhouse:s2"]  # entry-level first
    opts = seen["options"]
    assert opts.lanes == ["sanctions"] and opts.employers == ["acme-risk"] and opts.include_metered is False
    status = ok(call("sweep_status"))
    assert status["status"] == "finished" and status["run_id"] == 7
    assert "list_employers" in err(call("run_sweep", {"employers": ["Globex Intelligence"]}))
    assert "import_jobs" in err(call("run_sweep", {"sources": ["import"]}))


def test_run_sweep_runs_one_at_a_time(board, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    calls: list[int] = []

    def run_sweep(settings, taxonomy, registry, store, options=None, progress=None):  # noqa: ANN001, ANN202
        calls.append(1)
        gate.wait(10)
        return FakeReport(new_uids=[])

    install_fake(monkeypatch, "jobsweep.sweep", SweepOptions=FakeSweepOptions, run_sweep=run_sweep)
    try:
        first = ok(call("run_sweep", {"wait_seconds": 0}))
        assert first["status"] == "running" and first["started"] is True
        second = ok(call("run_sweep", {"wait_seconds": 0}))
        assert second["status"] == "already_running" and second["started"] is False
        assert ok(call("sweep_status"))["status"] == "running"
    finally:
        gate.set()
    done = ok(call("sweep_status", {"wait_seconds": 5}))
    assert done["status"] == "finished"
    assert len(calls) == 1


def test_run_sweep_error_is_reported(board, monkeypatch: pytest.MonkeyPatch) -> None:
    def run_sweep(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    install_fake(monkeypatch, "jobsweep.sweep", SweepOptions=FakeSweepOptions, run_sweep=run_sweep)
    data = ok(call("run_sweep", {"wait_seconds": 5}))
    assert data["status"] == "error" and "boom" in data["error"]


def test_sweep_status_idle_shows_last_stored_run(board) -> None:
    run_id = board.store.start_run({"options": {}})
    board.store.finish_run(run_id, {"new": 4, "total_jobs": 10, "errors": 1})
    data = ok(call("sweep_status"))
    assert data["status"] == "idle"
    assert data["last_run"]["id"] == run_id and data["last_run"]["new"] == 4


def test_run_sweep_dry_run_with_real_engine(board) -> None:
    sweep = pytest.importorskip("jobsweep.sweep")
    taxonomy = pytest.importorskip("jobsweep.taxonomy")
    try:
        ms.configure(taxonomy=taxonomy.Taxonomy.load())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"real taxonomy unavailable: {exc}")
    assert hasattr(sweep, "SweepOptions")
    data = ok(call("run_sweep", {"dry_run": True, "sources": ["nycjobs"], "boards": False}))
    assert data["status"] == "planned"
    assert data["plan"]["by_source"].get("nycjobs", 0) >= 1 or data["skipped_sources"]


# ---- live search / check employer ----------------------------------------------------------------


def test_live_search_stores_and_returns(board, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def live_search(settings, taxonomy, query, sources=None, store=None, *, registry=None,  # noqa: ANN001, ANN202
                          include_metered=False, errors=None):  # fmt: skip
        seen.update(query=query, sources=sources, registry=registry)
        errors.append({"source": "adzuna", "target": query.keywords, "error": "missing env: ADZUNA_APP_ID"})
        jobs = [
            taxonomy.classify(
                Job(
                    source="usajobs",
                    source_id="L1",
                    title="Sanctions Analyst",
                    company="Treasury",
                    url="https://www.usajobs.gov/job/L1",
                    location="Washington, DC",
                )  # fmt: skip
            )
        ]
        if store is not None:
            store.upsert_jobs(jobs)
        return jobs

    install_fake(monkeypatch, "jobsweep.sweep", SweepOptions=FakeSweepOptions, live_search=live_search)
    data = ok(
        call("live_search", {"keywords": "OFAC analyst", "location": "Washington, DC", "sources": "usajobs"})
    )
    assert data["total"] == 1 and data["stored"] is True
    assert data["items"][0]["uid"] == "usajobs:L1" and data["items"][0]["status"] == "new"
    assert data["errors"][0]["source"] == "adzuna"
    assert seen["query"].keywords == "OFAC analyst" and seen["query"].location == "Washington, DC"
    assert seen["sources"] == ["usajobs"] and seen["registry"] is not None
    assert ok(call("get_job", {"uid": "usajobs:L1"}))["company"] == "Treasury"
    assert "empty" in err(call("live_search", {"keywords": "  "}))


def _install_detect_and_snapshot(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    def detect_from_url(url: str, include_vendors: bool = False) -> tuple[str, str] | None:
        if "boards.greenhouse.io/" in url:
            return "greenhouse", url.rstrip("/").rsplit("/", 1)[-1]
        return None

    async def scan_careers_page(ctx, url, *, include_vendors=False, max_hops=4):  # noqa: ANN001, ANN202
        calls.append(("scan", url))
        return [("icims", "manualintel")] if "manual" in url else []

    async def probe_company(ctx, name, slugs=None, ats=None):  # noqa: ANN001, ANN202
        calls.append(("probe", name))
        if "newco" in name.lower():
            return [{"ats": "lever", "board": "newco", "open_jobs": 2, "titles": ["Political Risk Analyst"]}]
        return []

    async def board_snapshot(settings, taxonomy, employer, **kw):  # noqa: ANN001, ANN202
        calls.append(("snapshot", employer.key, employer.ats, employer.board))
        jobs = [
            Job(source=employer.ats, source_id=f"{employer.board}-1", title="Political Risk Analyst",
                company=employer.name, url=f"https://jobs.example/{employer.board}/1", location="London"),
            Job(source=employer.ats, source_id=f"{employer.board}-2", title="Office Manager",
                company=employer.name, url=f"https://jobs.example/{employer.board}/2"),
        ]  # fmt: skip
        return [taxonomy.classify(j, employer) for j in jobs]

    install_fake(
        monkeypatch,
        "jobsweep.sources.detect",
        SWEEPABLE_ATS=frozenset({"greenhouse", "lever"}),
        detect_from_url=detect_from_url,
        scan_careers_page=scan_careers_page,
        probe_company=probe_company,
    )
    install_fake(monkeypatch, "jobsweep.sweep", SweepOptions=FakeSweepOptions, board_snapshot=board_snapshot)


def test_check_employer_registry_name(board, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _install_detect_and_snapshot(monkeypatch, calls)
    data = ok(call("check_employer", {"name_or_url": "acme risk"}))
    assert data["registry"]["key"] == "acme-risk"
    assert (data["ats"], data["board"]) == ("greenhouse", "acmerisk")
    assert data["open_jobs"] == 2 and data["matching_jobs"] == 1 and data["entry_level_matches"] == 1
    assert data["top_jobs"][0]["title"] == "Political Risk Analyst"
    assert data["stored"] == 1 and "suggested_registry_entry" not in data
    assert calls == [("snapshot", "acme-risk", "greenhouse", "acmerisk")]  # no probing for a known board
    assert ok(call("get_job", {"uid": "greenhouse:acmerisk-1"}))["company"] == "Acme Risk Partners"


def test_check_employer_probes_unknown_company(board, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _install_detect_and_snapshot(monkeypatch, calls)
    data = ok(call("check_employer", {"name_or_url": "NewCo Analytics", "store_matches": False}))
    assert data["registry"] is None
    assert (data["ats"], data["board"]) == ("lever", "newco")
    assert data["candidates"][0]["via"] == "probe"
    entry = data["suggested_registry_entry"]
    assert entry["ats"] == "lever" and entry["board"] == "newco" and entry["lanes"] == ["political-risk"]
    assert "stored" not in data and board.store.get("lever:newco-1") is None


def test_check_employer_urls(board, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _install_detect_and_snapshot(monkeypatch, calls)
    data = ok(call("check_employer", {"name_or_url": "https://boards.greenhouse.io/acmerisk"}))
    assert data["kind"] == "url" and data["registry"]["key"] == "acme-risk"
    assert data["board"] == "acmerisk" and not any(c[0] == "scan" for c in calls)

    manual = ok(call("check_employer", {"name_or_url": "manual.example/careers"}))
    assert manual["ats"] is None and manual["registry"]["key"] == "manual-intel"
    assert manual["candidates"] == [
        {"ats": "icims", "board": "manualintel", "sweepable": False, "via": "url"}
    ]
    assert "check by hand at https://manual.example/careers" in manual["notes"][0]


# ---- digest / resources --------------------------------------------------------------------------


def test_digest_and_resources_with_fake_export(board, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def digest(store, days=1, min_score=None, fmt="md", *, taxonomy=None, limit_per_lane=None):  # noqa: ANN001, ANN202
        seen.update(days=days, min_score=min_score, fmt=fmt, taxonomy=taxonomy, limit=limit_per_lane)
        return f"# jobsweep digest\n\n{len(store.query(since=days, limit=None))} new"

    install_fake(monkeypatch, "jobsweep.export", digest=digest)
    res = call("digest", {"days": 7, "min_score": 40})
    assert not res.isError
    assert res.content[0].text.startswith("# jobsweep digest")
    assert seen == {"days": 7, "min_score": 40, "fmt": "md", "taxonomy": board.tax, "limit": 10}

    async def read() -> tuple:
        async with create_connected_server_and_client_session(ms.mcp) as client:
            lanes = await client.read_resource("jobsweep://lanes")
            today = await client.read_resource("jobsweep://digest/today")
            return lanes, today

    lanes, today = asyncio.run(read())
    doc = json.loads(lanes.contents[0].text)
    assert "earlier-lanes" in {g["id"] for g in doc["groups"]}
    assert today.contents[0].text.startswith("# jobsweep digest")


def test_digest_with_real_export(board) -> None:
    pytest.importorskip("jobsweep.export")
    res = call("digest", {"days": 1})
    assert not res.isError
    text = res.content[0].text
    assert "[Political Risk Analyst](https://boards.greenhouse.io/beazley/jobs/1)" in text


# ---- helpers -------------------------------------------------------------------------------------


def test_format_salary_and_find_employer() -> None:
    assert ms.format_salary({"salary_min": 65000, "salary_max": 75000, "salary_currency": "USD",
                             "salary_period": "year"}) == "$65,000–$75,000/yr"  # fmt: skip
    assert (
        ms.format_salary({"salary_min": 40000, "salary_currency": "GBP", "salary_period": "year"})
        == "£40,000/yr"
    )
    assert ms.format_salary({"salary_min": 28.5, "salary_max": 28.5, "salary_period": "hour"}) == "28.50/hr"
    assert ms.format_salary({}) is None
    reg = make_registry()
    assert ms.find_employer(reg, "acme-risk")[0].key == "acme-risk"
    assert ms.find_employer(reg, "Acme Risk Partners, Inc.")[0].key == "acme-risk"
    emp, alts = ms.find_employer(reg, "Totally Unrelated")
    assert emp is None and alts == []


# ---- stdio entry point ---------------------------------------------------------------------------


def test_stdio_entry_point_initializes_and_lists_tools(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jobsweep.mcp_server"],
        env={"JOBSWEEP_DB": str(tmp_path / "stdio.db"), "JOBSWEEP_HOME": str(tmp_path)},
        cwd=str(tmp_path),
    )

    async def go() -> tuple:
        with (tmp_path / "stderr.log").open("w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await asyncio.wait_for(session.initialize(), 60)
                    tools = await session.list_tools()
                    res = await session.call_tool("search_jobs", {"limit": 5})
                    return init, tools, res

    init, tools, res = asyncio.run(go())
    assert init.serverInfo.name == "jobsweep"
    assert "jobsweep" in (init.instructions or "")
    assert EXPECTED_TOOLS <= {t.name for t in tools.tools}
    assert not res.isError and res.structuredContent["total"] == 0
    assert (tmp_path / "stdio.db").exists()


# ---- live ----------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_check_employer_greenhouse(board) -> None:
    pytest.importorskip("jobsweep.sources.greenhouse")
    # Sayari's Greenhouse board was verified in the registry on 2026-10-25.
    data = ok(
        call("check_employer", {"name_or_url": "https://boards.greenhouse.io/sayari", "store_matches": False})
    )
    assert data["ats"] == "greenhouse" and data["board"] == "sayari", data.get("notes")
    assert data["open_jobs"] and data["open_jobs"] > 0, data.get("notes")
