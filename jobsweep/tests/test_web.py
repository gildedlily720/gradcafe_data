"""Web board: FastAPI endpoints (ARCHITECTURE.md §9) and static UI sanity checks."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import jobsweep.sources as sources_mod  # noqa: E402
from jobsweep.config import STATIC_DIR, Settings  # noqa: E402
from jobsweep.employers import EmployerRegistry  # noqa: E402
from jobsweep.models import JOB_STATUSES, Employer, Job, Profile  # noqa: E402
from jobsweep.sources.base import Source  # noqa: E402
from jobsweep.web import app as web_app  # noqa: E402
from jobsweep.web.app import CatalogTaxonomy, create_app, detect_format  # noqa: E402

store_mod = pytest.importorskip("jobsweep.store")
Store = store_mod.Store

FIXTURES = Path(__file__).parent / "fixtures"
NODE = shutil.which("node") or ("/opt/node22/bin/node" if Path("/opt/node22/bin/node").exists() else None)

GROUPS = [
    ("specialty-insurance", "Specialty insurance (underwriting and broking)", ["political-risk"]),
    ("intelligence-security", "Intelligence and security", ["threat-intelligence"]),
    ("sanctions-fincrime", "Sanctions, export controls and financial crime", ["sanctions"]),
    ("government", "Government", ["federal-pathways"]),
    ("earlier-lanes", "Earlier lanes", ["program-evaluation"]),
]
LANE_GROUPS = {lane: g for g, _, lanes in GROUPS for lane in lanes}
LONG_DESC = "About the role\nWe underwrite political risk.\n\n- Prepare files\n- Analyse obligors\n\n" + (
    "word " * 300
)


class FakeTaxonomy:
    """Minimal taxonomy: names for /api/lanes, keyword classification for imports."""

    def __init__(self) -> None:
        self.profile = Profile(min_score=25)
        self.groups = [
            SimpleNamespace(id=g, name=n, active=g != "earlier-lanes",
                            lanes=[SimpleNamespace(id=x, name=x.replace("-", " ").title()) for x in lanes])
            for g, n, lanes in GROUPS
        ]  # fmt: skip
        self.classified: list[str] = []

    def to_dict(self) -> dict:
        return {
            "groups": [
                {"id": g.id, "name": g.name, "active": g.active,
                 "lanes": [{"id": x.id, "name": x.name, "active": g.active, "group": g.id, "queries": []}
                           for x in g.lanes]}
                for g in self.groups
            ]
        }  # fmt: skip

    def classify(self, job: Job, employer: Employer | None = None) -> Job:
        self.classified.append(job.uid)
        if "risk" in job.title.lower():
            job.lanes, job.groups, job.score = ["political-risk"], ["specialty-insurance"], 66.0
        if employer is not None:
            job.match_reasons.append(f"employer prior: {employer.name}")
        return job


def mk(source: str, sid: str, title: str, score: float, lanes: list[str], **kw) -> Job:
    kw.setdefault("company", "Beazley")
    kw.setdefault("location", "New York, NY")
    return Job(source=source, source_id=sid, title=title, url=f"https://example.com/{source}/{sid}", score=score,
               lanes=lanes, groups=[LANE_GROUPS[x] for x in lanes], match_reasons=[f"title: {title}"], **kw)  # fmt: skip


SAMPLE = [
    mk(
        "greenhouse",
        "1",
        "Political Risk Underwriting Assistant",
        80,
        ["political-risk"],
        entry_level=True,
        location="London, UK",
        description=LONG_DESC,
        remote=False,
    ),
    mk(
        "lever",
        "abc/def",
        "Sanctions Analyst",
        70,
        ["sanctions"],
        company="K2 Integrity",
        entry_level=True,
        remote=True,
    ),
    mk(
        "usajobs",
        "DHS-1",
        "Intelligence Research Specialist",
        60,
        ["federal-pathways"],
        company="Department of Homeland Security",
        location="Washington, DC",
        tags=["pathways"],
    ),
    mk(
        "greenhouse",
        "2",
        "Senior Director, Threat Intelligence",
        30,
        ["threat-intelligence"],
        company="Recorded Future",
        entry_level=False,
    ),
    mk("greenhouse", "3", "Program Evaluation Analyst", 50, ["program-evaluation"], company="Mathematica"),
    Job(
        source="import",
        source_id="indeed:9",
        title="Office Coordinator",
        company="Acme",
        url="https://example.com/9",
        score=10.0,
    ),
]


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "web.db", lane_groups=LANE_GROUPS, inactive_lanes={"program-evaluation"},
              inactive_groups={"earlier-lanes"})  # fmt: skip
    s.upsert_jobs([Job.from_dict(j.to_dict()) for j in SAMPLE])
    yield s
    s.close()


@pytest.fixture
def registry() -> EmployerRegistry:
    return EmployerRegistry([
        Employer(key="beazley", name="Beazley", ats="greenhouse", board="beazley", groups=["specialty-insurance"],
                 lanes=["political-risk"], verified="2026-10-25", open_jobs=40),
        Employer(key="lloyds", name="Lloyd's of London", ats="other", ats_vendor="workday-custom",
                 careers_url="https://example.com/careers", groups=["specialty-insurance"]),
    ])  # fmt: skip


@pytest.fixture
def taxonomy() -> FakeTaxonomy:
    return FakeTaxonomy()


@pytest.fixture
def app(tmp_path: Path, store, taxonomy, registry):
    settings = Settings(env={}, db_path=tmp_path / "unused.db")
    return create_app(settings, store=store, taxonomy=taxonomy, registry=registry)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def uids(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [r["uid"] for r in resp.json()["items"]]


# ---- UI files -------------------------------------------------------------------------------------


def test_index_config_and_static_files(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "jobsweep" in r.text and 'src="app.js"' in r.text and 'href="styles.css"' in r.text
    r = client.get("/config.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert 'window.JOBSWEEP_MODE = "api";' in r.text
    assert '"min_score": 25' in r.text
    for path, kind in (("/app.js", "javascript"), ("/styles.css", "css")):
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path
        assert r.headers["cache-control"] == "no-cache"
    assert client.get("/nope.js").status_code == 404


def test_unknown_api_path_is_json_404(client: TestClient) -> None:
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and "detail" in r.json()
    assert client.get("/api/stats").headers["cache-control"] == "no-store"


# ---- /api/jobs ------------------------------------------------------------------------------------


def test_jobs_default_shape_and_order(client: TestClient) -> None:
    r = client.get("/api/jobs")
    body = r.json()
    assert set(body) == {"total", "items"}
    # inactive-lane-only job (program-evaluation) is hidden by default
    assert uids(r) == ["greenhouse:1", "lever:abc/def", "usajobs:DHS-1", "greenhouse:2", "import:indeed:9"]
    assert body["total"] == 5
    row = body["items"][0]
    for key in (
        "uid",
        "title",
        "company",
        "url",
        "lanes",
        "groups",
        "score",
        "status",
        "note",
        "first_seen",
        "last_seen",
        "active",
        "duplicate_of",
        "match_reasons",
        "description",
    ):
        assert key in row
    assert row["status"] == "new" and row["description"] == LONG_DESC


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("lane=political-risk", ["greenhouse:1"]),
        ("lane=political-risk,sanctions", ["greenhouse:1", "lever:abc/def"]),
        ("lane=political-risk&lane=sanctions", ["greenhouse:1", "lever:abc/def"]),
        ("group=sanctions-fincrime", ["lever:abc/def"]),
        ("lane=program-evaluation", ["greenhouse:3"]),
        ("entry=1&min_score=25", ["greenhouse:1", "lever:abc/def", "usajobs:DHS-1"]),
        ("min_score=65", ["greenhouse:1", "lever:abc/def"]),
        ("remote=1", ["lever:abc/def"]),
        ("location=Washington", ["usajobs:DHS-1"]),
        ("q=sanctions", ["lever:abc/def"]),
        ("source=usajobs&source=import", ["usajobs:DHS-1", "import:indeed:9"]),
        ("status=new&entry=1&min_score=65&days=7", ["greenhouse:1", "lever:abc/def"]),
        ("employer=nobody", []),
        ("limit=2&offset=1", ["lever:abc/def", "usajobs:DHS-1"]),
    ],
)
def test_jobs_filters(client: TestClient, query: str, expected: list[str]) -> None:
    assert uids(client.get("/api/jobs?" + query)) == expected


def test_jobs_total_ignores_paging_and_inactive_lanes_flag(client: TestClient) -> None:
    r = client.get("/api/jobs?limit=1&include_inactive_lanes=1")
    assert r.json()["total"] == 6 and len(r.json()["items"]) == 1


def test_suitability_filters_page_totals_and_lane_counts(client: TestClient, store) -> None:
    """Screening must happen before pagination and use the same criteria in the sidebar."""
    for uid, suitability in (
        ("greenhouse:1", "excluded"),
        ("lever:abc/def", "eligible"),
        ("usajobs:DHS-1", "review"),
        ("greenhouse:2", "review"),
    ):
        row = store.get(uid)
        row["suitability"] = suitability
        store.upsert_jobs([Job.from_dict(row)])
    response = client.get("/api/jobs?suitability=eligible&limit=1")
    assert uids(response) == ["lever:abc/def"]
    assert response.json()["total"] == 1
    response = client.get("/api/jobs?suitability=review,not_assessed&limit=1&offset=1")
    assert uids(response) == ["greenhouse:2"]
    assert response.json()["total"] == 3
    assert uids(client.get("/api/jobs?suitability=eligible&suitability=review")) == [
        "lever:abc/def",
        "usajobs:DHS-1",
        "greenhouse:2",
    ]
    counts = client.get("/api/lanes?suitability=eligible").json()
    assert counts["total"] == 1
    groups = {group["id"]: group for group in counts["groups"]}
    assert groups["sanctions-fincrime"]["count"] == 1
    assert groups["specialty-insurance"]["count"] == 0
    # The UI omits suitability on filed tabs; excluded applications stay accessible.
    store.set_status("greenhouse:1", "applied")
    assert uids(client.get("/api/jobs?status=applied")) == ["greenhouse:1"]
    assert client.get("/api/jobs?suitability=qualified").status_code == 422
    assert client.get("/api/lanes?suitability=qualified").status_code == 422


def test_jobs_sorts(client: TestClient) -> None:
    companies = [r["company"] for r in client.get("/api/jobs?sort=company").json()["items"]]
    assert companies == sorted(companies, key=str.lower)
    for sort in ("score", "newest", "posted", "closing", "company"):
        assert client.get(f"/api/jobs?sort={sort}").status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "sort=random",
        "status=bogus",
        "status=new,bogus",
        "limit=0",
        "limit=5000",
        "min_score=500",
        "offset=-1",
    ],
)
def test_jobs_bad_params_are_422(client: TestClient, query: str) -> None:
    r = client.get("/api/jobs?" + query)
    assert r.status_code == 422, r.text


def test_jobs_compact_truncates_descriptions(client: TestClient) -> None:
    item = client.get("/api/jobs?lane=political-risk&compact=1").json()["items"][0]
    assert item["description_truncated"] is True
    assert len(item["description"]) <= web_app.COMPACT_DESCRIPTION + 2
    full = client.get("/api/jobs/greenhouse:1").json()
    assert full["description"] == LONG_DESC and "description_truncated" not in full


def test_status_all_includes_triaged_and_inactive(client: TestClient, store) -> None:
    store.set_status("greenhouse:2", "dismissed")
    assert "greenhouse:2" not in uids(client.get("/api/jobs?status=new"))
    assert "greenhouse:2" in uids(client.get("/api/jobs?status=dismissed&status=rejected"))
    assert "greenhouse:2" in uids(client.get("/api/jobs?status=all"))


# ---- /api/jobs/{uid} -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "uid"),
    [
        ("/api/jobs/greenhouse:1", "greenhouse:1"),
        ("/api/jobs/greenhouse%3A1", "greenhouse:1"),
        ("/api/jobs/lever:abc/def", "lever:abc/def"),
        ("/api/jobs/lever%3Aabc%2Fdef", "lever:abc/def"),
        ("/api/jobs/import:indeed:9", "import:indeed:9"),
    ],
)
def test_get_job_by_uid_with_colons_and_slashes(client: TestClient, path: str, uid: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.json()["uid"] == uid


@pytest.mark.parametrize("uid", ["greenhouse:999", "nope:missing:x", "lever:abc"])
def test_get_unknown_job_is_404(client: TestClient, uid: str) -> None:
    r = client.get("/api/jobs/" + uid)
    assert r.status_code == 404 and uid in r.json()["detail"]


# ---- status updates -------------------------------------------------------------------------------


def test_status_update_persists(client: TestClient, store) -> None:
    r = client.post("/api/jobs/greenhouse:1/status", json={"status": "saved", "note": "ask about referral"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "saved" and r.json()["note"] == "ask about referral"
    assert r.json()["status_updated_at"]
    assert client.get("/api/jobs/greenhouse:1").json()["status"] == "saved"
    assert store.get("greenhouse:1")["status"] == "saved"
    assert uids(client.get("/api/jobs?status=saved")) == ["greenhouse:1"]
    assert "greenhouse:1" not in uids(client.get("/api/jobs?status=new"))
    # status without note keeps the note; empty note clears it
    r = client.post("/api/jobs/greenhouse:1/status", json={"status": "applied"})
    assert r.json()["status"] == "applied" and r.json()["note"] == "ask about referral"
    r = client.post("/api/jobs/greenhouse:1/status", json={"status": "applied", "note": ""})
    assert r.json()["note"] == ""
    assert client.get("/api/stats").json()["by_status"]["applied"] == 1


def test_status_update_uid_with_slash(client: TestClient) -> None:
    r = client.post("/api/jobs/lever%3Aabc%2Fdef/status", json={"status": "interviewing"})
    assert (
        r.status_code == 200 and r.json()["uid"] == "lever:abc/def" and r.json()["status"] == "interviewing"
    )


@pytest.mark.parametrize("status", JOB_STATUSES)
def test_every_known_status_is_accepted(client: TestClient, status: str) -> None:
    assert client.post("/api/jobs/greenhouse:1/status", json={"status": status}).json()["status"] == status


@pytest.mark.parametrize(
    "body", [{"status": "archived"}, {"status": ""}, {}, {"status": None}, {"status": "saved", "note": 5}]
)
def test_status_update_validation(client: TestClient, store, body: dict) -> None:
    r = client.post("/api/jobs/greenhouse:1/status", json=body)
    assert r.status_code == 422, r.text
    assert store.get("greenhouse:1")["status"] == "new"


def test_status_update_unknown_uid_is_404(client: TestClient) -> None:
    r = client.post("/api/jobs/nope:missing:1/status", json={"status": "saved"})
    assert r.status_code == 404


def test_cross_origin_writes_are_refused(client: TestClient, store) -> None:
    r = client.post("/api/jobs/greenhouse:1/status", json={"status": "saved"},
                    headers={"Origin": "https://evil.example"})  # fmt: skip
    assert r.status_code == 403
    assert store.get("greenhouse:1")["status"] == "new"
    r = client.post("/api/jobs/greenhouse:1/status", json={"status": "saved"},
                    headers={"Origin": "http://testserver"})  # fmt: skip
    assert r.status_code == 200


# ---- lanes / stats / runs / sources / employers --------------------------------------------------


def test_lanes_counts_from_stats(client: TestClient) -> None:
    body = client.get("/api/lanes").json()
    groups = {g["id"]: g for g in body["groups"]}
    assert list(groups) == [g for g, _, _ in GROUPS]
    assert groups["specialty-insurance"]["count"] == 1
    assert groups["specialty-insurance"]["lanes"][0] == {
        "id": "political-risk", "name": "Political Risk", "active": True, "group": "specialty-insurance",
        "queries": [], "count": 1,
    }  # fmt: skip
    assert groups["earlier-lanes"]["active"] is False and groups["earlier-lanes"]["lanes"][0]["count"] == 1
    assert body["total"] == 6 and body["min_score"] == 25 and body["statuses"] == list(JOB_STATUSES)


def test_lanes_counts_follow_filters(client: TestClient) -> None:
    body = client.get("/api/lanes?status=new&entry=1&min_score=65&lane=sanctions").json()
    lanes = {x["id"]: x["count"] for g in body["groups"] for x in g["lanes"]}
    # the lane filter itself is ignored so every lane shows what selecting it would give
    assert lanes == {"political-risk": 1, "threat-intelligence": 0, "sanctions": 1, "federal-pathways": 0,
                     "program-evaluation": 0}  # fmt: skip
    assert body["total"] == 2
    assert client.get("/api/lanes?status=bogus").status_code == 422


@pytest.mark.parametrize(
    "query",
    ["status=new&entry=1&min_score=25", "min_score=40", "status=all&q=analyst", "remote=0&days=30",
     "status=new&include_inactive_lanes=1", "source=greenhouse"],
)  # fmt: skip
def test_lane_counts_fast_path_matches_public_count_api(
    client: TestClient, store, monkeypatch, query: str
) -> None:
    # a job in both an active and an inactive lane, plus the inactive-only one from SAMPLE
    store.upsert_jobs([mk("greenhouse", "4", "Evaluation and Sanctions Analyst", 55, ["program-evaluation", "sanctions"],
                          company="Other Org")])  # fmt: skip
    fast = client.get("/api/lanes?" + query).json()
    monkeypatch.setattr(web_app, "_grouped_counts", lambda *a, **k: None)
    slow = client.get("/api/lanes?" + query).json()
    assert fast == slow
    assert any(lane["count"] for g in fast["groups"] for lane in g["lanes"])


def test_stats_and_runs(client: TestClient, store) -> None:
    stats = client.get("/api/stats").json()
    for key in (
        "total",
        "active",
        "new_24h",
        "new_7d",
        "by_group",
        "by_lane",
        "by_source",
        "by_status",
        "last_run",
    ):
        assert key in stats
    assert stats["total"] == 6 and stats["last_run"] is None
    assert client.get("/api/runs").json() == []
    rid = store.start_run({"options": {"sources": None}})
    store.finish_run(rid, {"targets": 3, "ok": 3, "errors": 0, "new": 2, "total_jobs": 9})
    runs = client.get("/api/runs?limit=5").json()
    assert runs[0]["id"] == rid and runs[0]["stats"]["new"] == 2
    assert client.get("/api/stats").json()["last_run"]["id"] == rid
    assert client.get("/api/runs?limit=0").status_code == 422


class _OkSource(Source):
    name = "oksrc"
    kind = "search"
    label = "OK Source"
    docs_url = "https://example.com/docs"


class _KeySource(Source):
    name = "keysrc"
    kind = "board"
    metered = True
    requires_env = ("SOME_KEY",)


def test_sources(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(sources_mod, "SOURCE_PATHS", {"oksrc": "x:y", "keysrc": "x:z", "gone": "x:w"})
    monkeypatch.setattr(sources_mod, "_cache", {"oksrc": _OkSource, "keysrc": _KeySource, "gone": None})
    rows = {r["name"]: r for r in client.get("/api/sources").json()}
    assert set(rows) == {"oksrc", "keysrc", "gone"}
    for r in rows.values():
        assert set(r) >= {"name", "label", "kind", "available", "reason", "metered", "docs_url"}
    assert rows["oksrc"]["available"] is True and rows["oksrc"]["label"] == "OK Source"
    assert rows["keysrc"]["available"] is False and "SOME_KEY" in rows["keysrc"]["reason"]
    assert rows["keysrc"]["metered"] is True and rows["gone"]["available"] is False


def test_employers(client: TestClient) -> None:
    body = client.get("/api/employers").json()
    assert [e["key"] for e in body["sweepable"]] == ["beazley"]
    assert [e["key"] for e in body["manual"]] == ["lloyds"]
    assert body["manual"][0]["careers_url"] == "https://example.com/careers"
    assert body["sweepable"][0]["open_jobs"] == 40
    assert client.get("/api/employers?q=beaz").json()["manual"] == []
    assert client.get("/api/employers?group=government").json() == {"sweepable": [], "manual": []}
    assert [e["key"] for e in client.get("/api/employers?lane=political-risk").json()["sweepable"]] == [
        "beazley"
    ]
    assert client.get("/api/employers?manual_only=1").json()["sweepable"] == []


# ---- sweeps ---------------------------------------------------------------------------------------


class _FakeReport:
    def __init__(self, new: int) -> None:
        self.new = new

    def to_dict(self) -> dict:
        return {"run_id": 7, "started_at": "s", "finished_at": "f", "dry_run": False,
                "plan": {"counts": {"boards": 1, "searches": 1}, "boards": [{"employer": "x"}] * 500},
                "per_source": {}, "errors": [], "new_uids": [f"greenhouse:{i}" for i in range(self.new)],
                "total_jobs": 9}  # fmt: skip

    def summary(self) -> str:
        return f"Sweep run #7: {self.new} new"


def _install_fake_sweep(monkeypatch, *, fail: bool = False):
    sweep_mod = pytest.importorskip("jobsweep.sweep")
    release = threading.Event()
    calls: list = []

    async def fake_run_sweep(settings, taxonomy, registry, store, options=None, progress=None):
        calls.append(options)
        progress({"event": "plan", "counts": {"boards": 1, "searches": 1}, "total": 2, "dry_run": False})
        progress({"event": "target_done", "kind": "board", "source": "greenhouse", "target": "beazley", "ok": True,
                  "error": None, "jobs": 5, "kept": 3, "new": 2, "done": 1, "total": 2, "elapsed_s": 0.1})  # fmt: skip
        while not release.is_set():
            await asyncio.sleep(0.01)
        if fail:
            raise RuntimeError("network exploded")
        progress({"event": "target_done", "kind": "search", "source": "usajobs", "target": "analyst", "ok": False,
                  "error": "HTTP 500", "jobs": 0, "kept": 0, "new": 0, "done": 2, "total": 2, "elapsed_s": 0.2})  # fmt: skip
        progress({"event": "finished", "report": {}})
        return _FakeReport(new=400)

    monkeypatch.setattr(sweep_mod, "run_sweep", fake_run_sweep)
    return release, calls


def test_sweep_runs_in_background_and_rejects_concurrent(client: TestClient, app, monkeypatch) -> None:
    release, calls = _install_fake_sweep(monkeypatch)
    idle = client.get("/api/sweep/status").json()
    assert idle == {"running": False, "last_report": None, "progress": {}}
    r = client.post(
        "/api/sweep", json={"sources": "greenhouse,usajobs", "include_metered": True, "boards": True}
    )
    assert r.status_code == 202 and r.json() == {"started": True}
    try:
        for _ in range(200):
            st = client.get("/api/sweep/status").json()
            if st["progress"].get("done") == 1:
                break
            threading.Event().wait(0.01)
        assert st["running"] is True
        assert st["progress"]["total"] == 2 and st["progress"]["ok"] == 1 and st["progress"]["new"] == 2
        assert st["progress"]["recent"][0]["target"] == "beazley"
        assert st["progress"]["options"]["sources"] == ["greenhouse", "usajobs"]
        assert client.post("/api/sweep", json={}).status_code == 409
    finally:
        release.set()
    assert app.state.sweeps.wait(5)
    st = client.get("/api/sweep/status").json()
    assert st["running"] is False
    assert st["progress"]["phase"] == "finished" and st["progress"]["errors"] == 1
    rep = st["last_report"]
    assert rep["summary"] == "Sweep run #7: 400 new" and rep["new_count"] == 400
    assert len(rep["new_uids"]) == web_app.REPORT_LIST_CAP and rep["plan"]["boards_truncated"] == 500
    assert calls[0].sources == ["greenhouse", "usajobs"] and calls[0].include_metered is True
    # a new sweep can start once the previous one finished
    release.set()
    assert client.post("/api/sweep", json={"dry_run": False}).status_code == 202
    assert app.state.sweeps.wait(5)


def test_sweep_failure_is_reported(client: TestClient, app, monkeypatch) -> None:
    release, _ = _install_fake_sweep(monkeypatch, fail=True)
    release.set()
    assert client.post("/api/sweep").status_code == 202
    assert app.state.sweeps.wait(5)
    st = client.get("/api/sweep/status").json()
    assert st["running"] is False and st["progress"]["phase"] == "error"
    assert st["last_report"]["error"] == "network exploded"


def test_sweep_bad_options_are_422(client: TestClient, monkeypatch) -> None:
    _install_fake_sweep(monkeypatch)
    r = client.post("/api/sweep", json={"max_queries_per_source": "lots"})
    assert r.status_code == 422
    assert client.get("/api/sweep/status").json()["running"] is False


def test_sweep_refused_without_classifier(tmp_path: Path, store, registry) -> None:
    pytest.importorskip("jobsweep.sweep")
    app = create_app(Settings(env={}, db_path=tmp_path / "x.db"), store=store, taxonomy=CatalogTaxonomy(),
                     registry=registry)  # fmt: skip
    assert TestClient(app).post("/api/sweep", json={}).status_code == 503


# ---- import ---------------------------------------------------------------------------------------


def test_import_with_stub_parser(client: TestClient, store, taxonomy, monkeypatch) -> None:
    seen: list = []

    def fake_parse(payload, origin, fmt, settings):
        seen.append((payload, origin, fmt))
        return [
            Job(source="import", source_id=f"{origin}:1", title="Political Risk Analyst", company="Beazley plc",
                url="https://example.com/i1", tags=[origin]),
            Job(source="import", source_id=f"{origin}:2", title="Office Manager", company="Acme",
                url="https://example.com/i2"),
        ]  # fmt: skip

    monkeypatch.setattr(web_app, "_parse_import", fake_parse)
    body = {
        "origin": "indeed connector!",
        "format": "auto",
        "payload": "**Job Title:** Political Risk Analyst",
    }
    r = client.post("/api/import", json=body)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["imported"] == 2 and res["new"] == 2 and res["format"] == "md"
    assert res["origin"] == "indeed-connector-" and seen[0][1:] == ("indeed-connector-", "md")
    row = store.get("import:indeed-connector-:1")
    assert row["lanes"] == ["political-risk"] and row["employer_key"] == "beazley"
    assert "employer prior: Beazley" in row["match_reasons"]
    assert taxonomy.classified == ["import:indeed-connector-:1", "import:indeed-connector-:2"]
    again = client.post("/api/import", json=body).json()
    assert again["imported"] == 2 and again["new"] == 0
    # JSON list payloads are passed through as objects
    client.post("/api/import", json={"origin": "workopia", "payload": [{"title": "x"}]})
    assert seen[-1] == ([{"title": "x"}], "workopia", "json")


@pytest.mark.parametrize(
    "body",
    [
        {"payload": ""},
        {"payload": "   "},
        {},
        {"payload": []},
        {"payload": 5},
        {"payload": "a,b", "format": "xml"},
    ],
)
def test_import_validation(client: TestClient, body: dict) -> None:
    assert client.post("/api/import", json=body).status_code == 422


def test_import_parser_errors(client: TestClient, monkeypatch) -> None:
    def bad(*a, **k):
        raise ValueError("not JSON")

    monkeypatch.setattr(web_app, "_parse_import", bad)
    r = client.post("/api/import", json={"payload": "{oops", "format": "json"})
    assert r.status_code == 422 and "not JSON" in r.json()["detail"]

    def missing(*a, **k):
        raise ImportError("no module named manual_import")

    monkeypatch.setattr(web_app, "_parse_import", missing)
    assert client.post("/api/import", json={"payload": "x"}).status_code == 503


def test_import_real_parser_indeed_markdown(client: TestClient, store) -> None:
    pytest.importorskip("jobsweep.sources.manual_import")
    text = (FIXTURES / "import" / "indeed_connector_search.md").read_text(encoding="utf-8")
    r = client.post("/api/import", json={"origin": "indeed-connector", "format": "md", "payload": text})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] >= 3 and r.json()["new"] == r.json()["imported"]
    rows = client.get("/api/jobs?source=import&limit=100").json()["items"]
    assert any("Geopolitical" in x["title"] for x in rows)


@pytest.mark.parametrize(
    ("payload", "fmt"),
    [("[{}]", "json"), ('  {"items": []}', "json"), ("**Job Title:** x", "md"), ("# Jobs", "md"),
     ("title,company\nA,B", "csv"), ("just text", "md"), ([{"a": 1}], "json")],
)  # fmt: skip
def test_detect_format(payload, fmt: str) -> None:
    assert detect_format(payload) == fmt


# ---- digest ---------------------------------------------------------------------------------------


def test_digest(client: TestClient) -> None:
    pytest.importorskip("jobsweep.export")
    r = client.get("/api/digest?days=30")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# jobsweep digest") and "Political Risk Underwriting Assistant" in r.text
    data = client.get("/api/digest?days=30&format=json&min_score=65").json()
    assert data["total"] == 2 and data["groups"]
    assert client.get("/api/digest?format=xml").status_code == 422
    assert client.get("/api/digest?days=0").status_code == 422


# ---- app wiring -----------------------------------------------------------------------------------


def test_create_app_opens_and_closes_its_own_store(tmp_path: Path, taxonomy, registry) -> None:
    db = tmp_path / "own" / "board.db"
    app = create_app(Settings(env={}, db_path=db), taxonomy=taxonomy, registry=registry)
    with TestClient(app) as c:
        assert c.get("/api/stats").json()["total"] == 0
        assert c.get("/api/jobs").json() == {"total": 0, "items": []}
    assert db.exists()


def test_real_taxonomy_lanes(tmp_path: Path, store, registry) -> None:
    pytest.importorskip("jobsweep.taxonomy")
    app = create_app(Settings(env={}, db_path=tmp_path / "x.db"), store=store, registry=registry)
    body = TestClient(app).get("/api/lanes").json()
    ids = [g["id"] for g in body["groups"]]
    assert ids[:3] == ["specialty-insurance", "intelligence-security", "investigations-dd"]
    assert ids[-1] == "earlier-lanes" and len(ids) == 11
    assert all("count" in lane for g in body["groups"] for lane in g["lanes"])


def test_catalog_taxonomy_fallback_reads_lanes_yaml() -> None:
    tax = CatalogTaxonomy()
    data = tax.to_dict()
    if not data["groups"]:
        pytest.skip("catalog/lanes.yaml not available")
    ids = [g["id"] for g in data["groups"]]
    assert "specialty-insurance" in ids and "earlier-lanes" in ids
    earlier = next(g for g in data["groups"] if g["id"] == "earlier-lanes")
    assert earlier["active"] is False and all(lane["active"] is False for lane in earlier["lanes"])
    job = Job(source="x", source_id="1", title="t", company="c", url="u")
    assert tax.classify(job) is job and tax.search_plan() == []


# ---- static UI sanity -------------------------------------------------------------------------------


def test_app_js_parses_with_node() -> None:
    if not NODE:
        pytest.skip("node not installed")
    res = subprocess.run(
        [NODE, "--check", str(STATIC_DIR / "app.js")], capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr


def test_ui_is_self_contained_and_xss_safe() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    # relative asset paths (works under a GitHub Pages sub-path) and no CDN dependencies
    for ref in ('src="config.js"', 'src="app.js"', 'href="styles.css"'):
        assert ref in html
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "@import" not in css and "url(http" not in css
    assert not re.search(r"""fetch\(\s*["'`]https?://""", js)
    # job data is never injected as HTML (checked on the code, comments stripped)
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in code, sink
    # both modes and the API surface the contract defines
    for needle in (
        '"static"',
        '"api"',
        "data/jobs.json",
        "data/lanes.json",
        "data/stats.json",
        "data/employers.json",
        "data/sources.json",
        "api/jobs",
        "api/lanes",
        "api/sweep/status",
        "api/import",
        "localStorage",
    ):
        assert needle in js, needle
    assert "prefers-color-scheme: dark" in css
