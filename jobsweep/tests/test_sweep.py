from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

import jobsweep.sources as sources_mod
from jobsweep.config import Settings
from jobsweep.employers import EmployerRegistry
from jobsweep.models import Employer, Job, Profile, SearchQuery
from jobsweep.sources.base import Source, SourceUnavailable
from jobsweep.store import Store
from jobsweep.sweep import (
    SweepOptions,
    _run_coro_sync,
    board_snapshot,
    live_search,
    run_sweep,
    run_sweep_sync,
    verify_boards,
)

# ---- fakes ----------------------------------------------------------------------------------------

RULES = {
    "political risk": ("political-risk", "specialty-insurance"),
    "sanctions": ("sanctions", "sanctions-fincrime"),
    "intelligence": ("threat-intelligence", "intelligence-security"),
}


class FakeTaxonomy:
    """Implements the taxonomy API the sweep uses (§4.3)."""

    def __init__(self, n_queries: int = 10):
        self.profile = Profile(min_score=25)
        self.n_queries = n_queries
        self.plan_calls: list[tuple] = []
        self.groups = [
            SimpleNamespace(id=g, name=g.title(), active=True, lanes=[SimpleNamespace(id=lane, name=lane)])
            for lane, g in RULES.values()
        ]

    def title_signal(self, title: str, employer=None) -> float:
        return 10.0 if any(k in (title or "").lower() for k in RULES) else 1.0

    def classify(self, job: Job, employer=None) -> Job:
        text = f"{job.title} {job.description}".lower()
        hits = [v for k, v in RULES.items() if k in text]
        job.lanes = [lane for lane, _ in hits]
        job.groups = list(dict.fromkeys(g for _, g in hits))
        job.score = 60.0 + (10 if employer is not None else 0) if hits else 5.0
        job.entry_level = "senior" not in job.title.lower()
        job.match_reasons = [f"title: {k}" for k in RULES if k in text]
        return job

    def search_plan(self, lane_ids=None, source=None, posted_within_days=None) -> list[SearchQuery]:
        self.plan_calls.append((tuple(lane_ids) if lane_ids else None, source, posted_within_days))
        return [
            SearchQuery(
                keywords=f"{source} q{i}", lane="political-risk", posted_within_days=posted_within_days
            )
            for i in range(self.n_queries)
        ]

    def to_dict(self) -> dict:
        return {
            "groups": [
                {"id": g.id, "name": g.name, "active": True,
                 "lanes": [{"id": lane.id, "name": lane.name, "active": True, "group": g.id, "queries": []}
                           for lane in g.lanes]}
                for g in self.groups
            ]
        }  # fmt: skip


def mkjob(source: str, sid: str, title: str, company: str = "Acme", **kw) -> Job:
    return Job(
        source=source, source_id=sid, title=title, company=company, url=f"https://x/{source}/{sid}", **kw
    )


class FakeBoard(Source):
    name = "fakeboard"
    kind = "board"
    boards: dict = {}
    enrich_calls: list = []

    async def fetch_board(self, employer: Employer) -> list[Job]:
        res = self.boards[employer.board]
        if isinstance(res, Exception):
            raise res
        return copy.deepcopy(res)

    async def enrich(self, job: Job) -> Job:
        type(self).enrich_calls.append(job.uid)
        if "explode" in job.title:
            raise RuntimeError("detail page 500")
        job.description = "Desk covering sanctions exposure"
        return job


class FakeSearch(Source):
    name = "fakesearch"
    kind = "search"
    max_queries = 3

    async def search(self, query: SearchQuery) -> list[Job]:
        if query.keywords.endswith("q1"):
            raise RuntimeError("quota exceeded for q1")
        slug = query.keywords.replace(" ", "-")
        return [
            mkjob("fakesearch", slug, "Political Risk Analyst", company=f"Co {slug}"),
            mkjob("fakesearch", slug + "-x", "Barista", company=f"Cafe {slug}"),
        ]


class MeteredSearch(FakeSearch):
    name = "metered"
    metered = True
    max_queries = None

    async def search(self, query: SearchQuery) -> list[Job]:
        return [mkjob("metered", query.keywords.replace(" ", "-"), "Sanctions Analyst", company="Paid Co")]


class NeedsKey(FakeSearch):
    name = "needskey"
    requires_env = ("FAKE_API_KEY",)


FAKES = {"fakeboard": FakeBoard, "fakesearch": FakeSearch, "metered": MeteredSearch, "needskey": NeedsKey}


@pytest.fixture
def fake_sources(monkeypatch):
    paths = {name: f"tests.fake:{cls.__name__}" for name, cls in FAKES.items()}
    paths["missingboard"] = "jobsweep.sources.nope:Nope"
    paths["import"] = "jobsweep.sources.manual_import:ImportSource"
    monkeypatch.setattr(sources_mod, "SOURCE_PATHS", paths)
    monkeypatch.setattr(sources_mod, "_cache", {**FAKES, "missingboard": None, "import": None})
    FakeBoard.boards = {
        "acme": [
            mkjob("fakeboard", "1", "Political Risk Analyst", employer_key="acme"),
            mkjob("fakeboard", "2", "Software Engineer", employer_key="acme"),
            mkjob("fakeboard", "3", "Senior Intelligence Lead", employer_key="acme"),
        ],
        "globex": [mkjob("fakeboard", "g1", "Sanctions Associate", company="Globex", employer_key="globex")],
        "broken": RuntimeError("HTTP 404 for https://boards.example/broken"),
    }
    FakeBoard.enrich_calls = []
    return FAKES


def employer(key: str, board: str | None = None, ats: str = "fakeboard", **kw) -> Employer:
    kw.setdefault("groups", ["specialty-insurance"])
    return Employer(key=key, name=key.title(), ats=ats, board=board or key, **kw)


@pytest.fixture
def registry() -> EmployerRegistry:
    return EmployerRegistry(
        [
            employer("acme"),
            employer("globex", groups=["sanctions-fincrime"]),
            employer("initech", ats="missingboard"),
            employer("manualco", ats="other", board=None),
        ]
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(env={}, db_path=tmp_path / "db.sqlite")


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


# ---- planning -------------------------------------------------------------------------------------


def test_dry_run_plan_boards_searches_budgets(fake_sources, registry, settings) -> None:
    tax = FakeTaxonomy()
    rep = asyncio.run(run_sweep(settings, tax, registry, None, SweepOptions(dry_run=True)))
    plan = rep.plan
    assert rep.dry_run and rep.run_id is None and not rep.per_source
    assert {b["employer"] for b in plan["boards"]} == {"acme", "globex"}
    assert [e["employer"] for e in plan["skipped_employers"]] == ["initech"]
    assert plan["skipped_employers"][0]["reason"] == "adapter missing or failed to import"
    # FakeSearch.max_queries=3 caps 10 planned queries
    assert plan["counts"]["by_source"] == {"fakeboard": 2, "fakesearch": 3}
    assert plan["truncated"] == {"fakesearch": {"planned": 10, "kept": 3}}
    skipped = {s["source"]: s["reason"] for s in plan["skipped_sources"]}
    assert "metered" in skipped["metered"]
    assert skipped["needskey"] == "missing env: FAKE_API_KEY"
    assert "import" not in skipped  # import is never part of a sweep
    assert tax.plan_calls == [(None, "fakesearch", 30)]
    assert "Sweep plan (dry run)" in rep.summary()


def test_plan_max_queries_and_metered_gating(fake_sources, registry, settings) -> None:
    tax = FakeTaxonomy()
    opts = SweepOptions(dry_run=True, max_queries_per_source=2, include_metered=True, boards=False)
    plan = asyncio.run(run_sweep(settings, tax, registry, None, opts)).plan
    assert plan["counts"] == {"boards": 0, "searches": 4, "by_source": {"fakesearch": 2, "metered": 2}}

    # explicitly listed metered source runs without include_metered; unknown names are reported
    opts = SweepOptions(dry_run=True, sources=["metered", "bogus", "import"])
    plan = asyncio.run(run_sweep(settings, tax, registry, None, opts)).plan
    assert plan["counts"]["by_source"] == {"metered": 10}
    reasons = {s["source"]: s["reason"] for s in plan["skipped_sources"]}
    assert reasons["bogus"] == "unknown source" and "import-only" in reasons["import"]
    assert plan["boards"] == []  # board employers not requested by --sources


def test_plan_lanes_groups_and_employers(fake_sources, registry, settings) -> None:
    tax = FakeTaxonomy()
    opts = SweepOptions(dry_run=True, groups=["sanctions-fincrime"], posted_within_days=7)
    plan = asyncio.run(run_sweep(settings, tax, registry, None, opts)).plan
    assert [b["employer"] for b in plan["boards"]] == ["globex"]
    assert tax.plan_calls[-1] == (("sanctions",), "fakesearch", 7)

    # --employers alone: boards only, no keyword searches
    opts = SweepOptions(dry_run=True, employers=["acme", "manualco", "ghost"])
    plan = asyncio.run(run_sweep(settings, tax, registry, None, opts)).plan
    assert [b["employer"] for b in plan["boards"]] == ["acme"]
    assert plan["counts"]["searches"] == 0
    reasons = {s["employer"]: s["reason"] for s in plan["skipped_employers"]}
    assert reasons["ghost"] == "not in registry" and "not sweepable" in reasons["manualco"]


def test_options_from_dict() -> None:
    o = SweepOptions.from_dict(
        {
            "sources": "a, b",
            "lanes": ["x,y"],
            "include_metered": "true",
            "concurrency": "3",
            "nope": 1,
            "groups": None,
        }
    )
    assert o.sources == ["a", "b"] and o.lanes == ["x", "y"] and o.include_metered is True
    assert o.concurrency == 3 and o.groups is None
    assert SweepOptions.from_dict(None) == SweepOptions()


# ---- execution ------------------------------------------------------------------------------------


def test_full_sweep_stores_and_reports(fake_sources, registry, settings, store) -> None:
    events: list[dict] = []
    tax = FakeTaxonomy()
    rep = asyncio.run(run_sweep(settings, tax, registry, store, SweepOptions(), progress=events.append))
    assert rep.run_id is not None
    # acme: PR analyst + Senior Intelligence (Software Engineer has no lane -> not stored); globex: 1
    # fakesearch: q0 and q2 ok (q1 fails), each 1 matching + 1 barista dropped
    assert set(rep.new_uids) == {
        "fakeboard:1",
        "fakeboard:3",
        "fakeboard:g1",
        "fakesearch:fakesearch-q0",
        "fakesearch:fakesearch-q2",
    }
    assert rep.total_jobs == 5
    assert store.get("fakeboard:2") is None
    assert rep.per_source["fakeboard"] == {
        "targets": 2, "ok": 2, "errors": 0, "jobs": 4, "kept": 3, "new": 3, "enriched": 3, "filtered": 0,
    }  # fmt: skip
    assert rep.per_source["fakesearch"]["errors"] == 1
    assert rep.errors == [
        {"source": "fakesearch", "target": "fakesearch q1", "error": "quota exceeded for q1"}
    ]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "plan" and kinds[-1] == "finished" and kinds.count("target_done") == 5
    done = [e for e in events if e["event"] == "target_done"]
    assert max(e["done"] for e in done) == 5 and all(e["total"] == 5 for e in done)
    run = store.runs()[0]
    assert run["finished_at"] and run["stats"]["new"] == 5 and run["stats"]["errors"] == 1
    # board jobs got the employer prior (+10) and employer_key; enrich added "sanctions" to the description
    row = store.get("fakeboard:1")
    assert row["score"] == 70 and row["employer_key"] == "acme"
    assert row["lanes"] == ["political-risk", "sanctions"] and "sanctions" in row["description"]
    text = rep.summary()
    assert "Sweep run #" in text and "fakesearch q1" in text

    # second sweep: nothing new; acme drops job 3 -> deactivated by mark_board_complete
    FakeBoard.boards["acme"] = FakeBoard.boards["acme"][:2]
    rep2 = run_sweep_sync(settings, tax, registry, store, SweepOptions(searches=False))
    assert rep2.new_uids == [] and rep2.deactivated == 1
    assert store.get("fakeboard:3")["active"] is False
    assert store.get("fakeboard:1")["active"] is True


def test_error_isolation_failing_board(fake_sources, settings, store) -> None:
    reg = EmployerRegistry([employer("broken"), employer("globex")])
    rep = asyncio.run(run_sweep(settings, FakeTaxonomy(), reg, store, SweepOptions(searches=False)))
    assert rep.errors == [
        {"source": "fakeboard", "target": "broken", "error": "HTTP 404 for https://boards.example/broken"}
    ]
    assert rep.new_uids == ["fakeboard:g1"]
    assert rep.per_source["fakeboard"]["ok"] == 1 and rep.per_source["fakeboard"]["errors"] == 1


def test_title_filter_and_enrich_threshold(fake_sources, settings, store) -> None:
    FakeBoard.boards["big"] = [
        mkjob("fakeboard", "b1", "Political Risk Analyst"),
        mkjob("fakeboard", "b2", "Credit Analyst"),  # passes filter, low signal -> no enrich
        mkjob("fakeboard", "b3", "Sanctions Officer"),  # filtered out by title_filter
        mkjob("fakeboard", "b4", "Intelligence Analyst explode"),  # enrich raises -> job kept
        mkjob("fakeboard", "b5", "Warehouse Associate"),  # filtered out
    ]
    reg = EmployerRegistry([employer("bigco", board="big", title_filter=r"analyst|^risk")])
    opts = SweepOptions(searches=False, enrich_threshold=8.0)
    rep = asyncio.run(run_sweep(settings, FakeTaxonomy(), reg, store, opts))
    assert sorted(FakeBoard.enrich_calls) == ["fakeboard:b1", "fakeboard:b4"]
    st = rep.per_source["fakeboard"]
    assert st["filtered"] == 2 and st["enriched"] == 1 and st["jobs"] == 5
    # b2 enriched? no -> no lane -> dropped; b1 enriched -> also matches sanctions from description
    assert store.get("fakeboard:b2") is None and store.get("fakeboard:b3") is None
    assert store.get("fakeboard:b1")["lanes"] == ["political-risk", "sanctions"]
    assert store.get("fakeboard:b4")["lanes"] == ["threat-intelligence"]

    # enrich cap
    FakeBoard.enrich_calls = []
    opts = SweepOptions(searches=False, max_enrich_per_board=1)
    asyncio.run(run_sweep(settings, FakeTaxonomy(), reg, store, opts))
    assert len(FakeBoard.enrich_calls) == 1

    # store_unmatched keeps everything that passed the title filter
    asyncio.run(
        run_sweep(settings, FakeTaxonomy(), reg, store, SweepOptions(searches=False, store_unmatched=True))
    )
    assert store.get("fakeboard:b2") is not None


def test_incomplete_board_not_marked(fake_sources, settings, store) -> None:
    FakeBoard.boards["capped"] = [
        mkjob("fakeboard", f"c{i}", "Political Risk Analyst " + str(i)) for i in range(3)
    ]
    reg = EmployerRegistry([employer("capped", max_jobs=3)])
    asyncio.run(run_sweep(settings, FakeTaxonomy(), reg, store, SweepOptions(searches=False)))
    FakeBoard.boards["capped"] = FakeBoard.boards["capped"][:2] + [
        mkjob("fakeboard", "c9", "Political Risk 9")
    ]
    rep = asyncio.run(run_sweep(settings, FakeTaxonomy(), reg, store, SweepOptions(searches=False)))
    assert rep.deactivated == 0  # hit max_jobs: board fetch may be truncated
    assert store.get("fakeboard:c2")["active"] is True


def test_sync_wrapper_inside_running_loop(fake_sources, registry, settings) -> None:
    async def inner():
        return run_sweep_sync(settings, FakeTaxonomy(), registry, None, SweepOptions(dry_run=True))

    rep = asyncio.run(inner())
    assert rep.plan["counts"]["boards"] == 2
    assert _run_coro_sync(lambda: asyncio.sleep(0, result=5)) == 5


def test_async_progress_callback(fake_sources, registry, settings) -> None:
    seen: list[str] = []

    async def cb(evt: dict) -> None:
        seen.append(evt["event"])
        raise RuntimeError("UI broke")  # never breaks the sweep

    asyncio.run(
        run_sweep(settings, FakeTaxonomy(), registry, None, SweepOptions(searches=False), progress=cb)
    )
    assert seen[0] == "plan" and seen[-1] == "finished"


# ---- live search / snapshot / verify --------------------------------------------------------------


def test_live_search(fake_sources, registry, settings, store) -> None:
    errors: list[dict] = []
    q = SearchQuery(keywords="risk")
    jobs = asyncio.run(
        live_search(settings, FakeTaxonomy(), q, store=store, registry=registry, errors=errors)
    )
    # default: fakesearch only (metered excluded, needskey unavailable, boards ignored)
    assert [j.uid for j in jobs] == ["fakesearch:risk", "fakesearch:risk-x"]
    assert jobs[0].score > jobs[1].score and errors == []
    assert store.count(min_score=0) == 2

    errors = []
    jobs = asyncio.run(
        live_search(
            settings,
            FakeTaxonomy(),
            SearchQuery("x q1"),
            ["fakesearch", "metered", "fakeboard"],
            errors=errors,
        )
    )
    assert [j.uid for j in jobs] == ["metered:x-q1"]
    assert {e["source"] for e in errors} == {"fakesearch", "fakeboard"}


def test_board_snapshot(fake_sources, registry, settings) -> None:
    jobs = asyncio.run(board_snapshot(settings, FakeTaxonomy(), registry.get("acme")))
    assert [j.uid for j in jobs] == ["fakeboard:1", "fakeboard:3", "fakeboard:2"]
    assert all(j.employer_key == "acme" for j in jobs)
    with pytest.raises(SourceUnavailable):
        asyncio.run(board_snapshot(settings, FakeTaxonomy(), registry.get("initech")))
    with pytest.raises(SourceUnavailable):
        asyncio.run(board_snapshot(settings, FakeTaxonomy(), employer("m", ats="other")))


def test_verify_boards(fake_sources, settings) -> None:
    emps = [employer("acme"), employer("broken"), employer("initech", ats="missingboard")]
    res = asyncio.run(verify_boards(settings, emps))
    by = {r["key"]: r for r in res}
    assert by["acme"]["count"] == 3 and by["acme"]["error"] is None and len(by["acme"]["sample"]) == 3
    assert "404" in by["broken"]["error"] and by["broken"]["count"] is None
    assert "no board adapter" in by["initech"]["error"]


# ---- integration with the real taxonomy / registry (skipped until those modules exist) -------------


def test_integration_dry_run_with_real_modules(tmp_path) -> None:
    taxonomy = pytest.importorskip("jobsweep.taxonomy")
    try:
        tax = taxonomy.Taxonomy.load()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"taxonomy not loadable yet: {exc}")
    reg = EmployerRegistry.load()
    settings = Settings(env={}, db_path=tmp_path / "db.sqlite")
    rep = run_sweep_sync(settings, tax, reg, None, SweepOptions(dry_run=True))
    assert rep.dry_run and "counts" in rep.plan
    assert all(s["source"] != "import" for s in rep.plan["searches"])
