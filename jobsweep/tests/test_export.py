from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import jobsweep.sources as sources_mod
from jobsweep.config import STATIC_DIR, Settings
from jobsweep.employers import EmployerRegistry
from jobsweep.export import digest, export_site, source_rows, write_csv
from jobsweep.models import Employer, Job, Profile
from jobsweep.sources.base import Source
from jobsweep.store import Store

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)

GROUPS = [
    (
        "specialty-insurance",
        "Specialty insurance",
        [("political-risk", "Political risk"), ("war-risk", "War risk")],
    ),
    ("sanctions-fincrime", "Sanctions & financial crime", [("sanctions", "Sanctions")]),
    ("earlier-lanes", "Earlier lanes", [("program-evaluation", "Program evaluation")]),
]


class FakeTaxonomy:
    def __init__(self, min_score: float = 25):
        self.profile = Profile(min_score=min_score)
        self.groups = [
            SimpleNamespace(id=g, name=n, active=g != "earlier-lanes",
                            lanes=[SimpleNamespace(id=lid, name=ln) for lid, ln in lanes])
            for g, n, lanes in GROUPS
        ]  # fmt: skip

    def to_dict(self) -> dict:
        return {
            "groups": [
                {"id": g.id, "name": g.name, "active": g.active,
                 "lanes": [{"id": lane.id, "name": lane.name, "active": g.active, "group": g.id, "queries": []}
                           for lane in g.lanes]}
                for g in self.groups
            ]
        }  # fmt: skip


class OkSource(Source):
    name = "oksrc"
    kind = "search"
    label = "OK Source"
    docs_url = "https://example.com/docs"


class KeySource(Source):
    name = "keysrc"
    kind = "board"
    metered = True
    requires_env = ("SOME_KEY",)


@pytest.fixture(autouse=True)
def fake_sources(monkeypatch):
    monkeypatch.setattr(sources_mod, "SOURCE_PATHS", {"oksrc": "x:y", "keysrc": "x:z", "gone": "x:w"})
    monkeypatch.setattr(sources_mod, "_cache", {"oksrc": OkSource, "keysrc": KeySource, "gone": None})


def mk(
    sid: str, title: str, score: float, lanes: list[str], groups: list[str], source: str = "greenhouse", **kw
) -> Job:
    kw.setdefault("company", "Beazley")
    kw.setdefault("location", "New York, NY")
    return Job(source=source, source_id=sid, title=title, url=f"https://jobs/{sid}", score=score, lanes=lanes,
               groups=groups, match_reasons=[f"title: {title}"], **kw)  # fmt: skip


@pytest.fixture
def store() -> Store:
    clock = SimpleNamespace(now=T0 - timedelta(days=5))
    s = Store(":memory:", clock=lambda: clock.now)
    s.upsert_jobs([mk("old", "Political Risk Underwriter", 70, ["political-risk"], ["specialty-insurance"])])
    clock.now = T0
    s.upsert_jobs(
        [
            mk("pr", "Political Risk Analyst", 82, ["political-risk"], ["specialty-insurance"], entry_level=True,
               description="word " * 1000, posted_at="2026-10-18", closes_at="2026-11-30"),
            mk("pr-sr", "Senior Political Risk Broker [London]", 90, ["political-risk"], ["specialty-insurance"],
               entry_level=False, location="London"),
            mk("war", "War Risk Assistant", 50, ["war-risk"], ["specialty-insurance"], entry_level=None),
            mk("sanc", "Sanctions Analyst", 64, ["sanctions"], ["sanctions-fincrime"], entry_level=True,
               company="Kroll"),
            mk("low", "Receptionist", 10, [], []),
            mk("eval", "Evaluation Associate", 55, ["program-evaluation"], ["earlier-lanes"]),
            mk("saved-low", "Claims Clerk", 12, ["political-risk"], ["specialty-insurance"]),
            mk("ri", "Risk Intern", 30, ["political-risk"], ["specialty-insurance"]),
            mk("unc", "Research Assistant", 30, [], []),
        ]
    )  # fmt: skip
    s.upsert_jobs(
        [
            mk(
                "dup",
                "Political Risk Analyst",
                82,
                ["political-risk"],
                ["specialty-insurance"],
                source="adzuna",
            )
        ]
    )
    s.set_status("greenhouse:saved-low", "saved", "long shot")
    s.clock = lambda: T0 + timedelta(hours=1)
    yield s
    s.close()


@pytest.fixture
def registry() -> EmployerRegistry:
    return EmployerRegistry(
        [
            Employer(
                key="beazley",
                name="Beazley",
                ats="greenhouse",
                board="beazley",
                groups=["specialty-insurance"],
            ),
            Employer(
                key="lloyds", name="Lloyd's", ats="other", ats_vendor="workday", careers_url="https://x"
            ),
        ]
    )


def test_export_site(store, registry, tmp_path) -> None:
    static = tmp_path / "static"
    (static / "img").mkdir(parents=True)
    (static / "index.html").write_text("<html>board</html>", encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")
    (static / "styles.css").write_text("body{}", encoding="utf-8")
    (static / "img" / "logo.svg").write_text("<svg/>", encoding="utf-8")
    (static / "config.js").write_text('window.JOBSWEEP_MODE = "api";', encoding="utf-8")
    out = export_site(store, FakeTaxonomy(), registry, tmp_path / "site", static_dir=static,
                      settings=Settings(env={}))  # fmt: skip
    assert (out / "index.html").read_text() == "<html>board</html>"
    assert (out / "app.js").exists() and (out / "styles.css").exists() and (out / "img" / "logo.svg").exists()
    assert (out / "config.js").read_text().strip() == 'window.JOBSWEEP_MODE = "static";'
    assert (out / ".nojekyll").exists()

    jobs = json.loads((out / "data" / "jobs.json").read_text())
    assert set(jobs) >= {"generated_at", "total", "items"}
    got = [r["uid"] for r in jobs["items"]]
    # >= min_score 25, active, non-duplicate (incl. earlier-lane job for the UI toggle) + every triaged job
    assert got == [
        "greenhouse:pr-sr", "greenhouse:pr", "greenhouse:old", "greenhouse:sanc", "greenhouse:eval",
        "greenhouse:war", "greenhouse:ri", "greenhouse:unc", "greenhouse:saved-low",
    ]  # fmt: skip
    assert jobs["total"] == len(got)
    pr = next(r for r in jobs["items"] if r["uid"] == "greenhouse:pr")
    assert len(pr["description"]) <= 1502 and pr["description"].endswith("…")
    assert pr["match_reasons"] == ["title: Political Risk Analyst"]
    for k in ("first_seen", "status", "lanes", "score", "entry_level", "url"):
        assert k in pr
    saved = next(r for r in jobs["items"] if r["uid"] == "greenhouse:saved-low")
    assert saved["status"] == "saved" and saved["note"] == "long shot"

    lanes = json.loads((out / "data" / "lanes.json").read_text())
    g = {x["id"]: x for x in lanes["groups"]}
    assert g["specialty-insurance"]["count"] == 6
    assert {lane["id"]: lane["count"] for lane in g["specialty-insurance"]["lanes"]} == {
        "political-risk": 5,
        "war-risk": 1,
    }
    stats = json.loads((out / "data" / "stats.json").read_text())
    assert stats["total"] == 10 and stats["exported"] == len(got)
    emps = json.loads((out / "data" / "employers.json").read_text())
    assert [e["key"] for e in emps["sweepable"]] == ["beazley"] and [e["key"] for e in emps["manual"]] == [
        "lloyds"
    ]
    sources = json.loads((out / "data" / "sources.json").read_text())
    assert [s["name"] for s in sources] == ["oksrc", "keysrc", "gone"]
    assert sources[0] == {
        "name": "oksrc", "label": "OK Source", "kind": "search", "available": True, "reason": "ok",
        "metered": False, "docs_url": "https://example.com/docs",
    }  # fmt: skip
    assert sources[1]["available"] is False and sources[1]["metered"] is True
    assert sources[2]["available"] is False


def test_export_site_min_score_days_and_missing_static(store, registry, tmp_path) -> None:
    out = export_site(store, FakeTaxonomy(), registry, tmp_path / "site", min_score=60, days=1,
                      static_dir=tmp_path / "nope", settings=Settings(env={}))  # fmt: skip
    assert "data/jobs.json" in (out / "index.html").read_text()  # placeholder page
    items = json.loads((out / "data" / "jobs.json").read_text())["items"]
    # days=1 drops greenhouse:old (first seen 5 days ago); saved job always included
    assert {r["uid"] for r in items} == {
        "greenhouse:pr-sr",
        "greenhouse:pr",
        "greenhouse:sanc",
        "greenhouse:saved-low",
    }


def test_export_site_with_real_static(store, registry, tmp_path) -> None:
    if not (STATIC_DIR / "index.html").exists():
        pytest.skip("web/static not written yet")
    out = export_site(store, FakeTaxonomy(), registry, tmp_path / "site", settings=Settings(env={}))
    assert (out / "index.html").exists()
    assert (out / "config.js").read_text().strip() == 'window.JOBSWEEP_MODE = "static";'


def test_digest_markdown(store) -> None:
    md = digest(store, days=1, taxonomy=FakeTaxonomy())
    lines = md.splitlines()
    assert lines[0].startswith("# jobsweep digest — ")
    assert "**6** new matches in the last 1 day (**2** entry-level)" in md
    # group order follows the taxonomy, lanes by size
    heads = [line for line in lines if line.startswith("#")]
    assert heads[1:] == [
        "## Specialty insurance (4)",
        "### Political risk (3)",
        "### War risk (1)",
        "## Sanctions & financial crime (1)",
        "### Sanctions (1)",
        "## unclassified (1)",
        "### Other (1)",
    ]
    pr_section = md.split("### Political risk (3)")[1].split("###")[0].strip().splitlines()
    # entry-level first, then unknown, then senior
    assert pr_section[0] == (
        "- [Political Risk Analyst](https://jobs/pr) — Beazley · New York, NY · score 82 · entry-level · "
        "posted 2026-10-18 · closes 2026-11-30"
    )
    assert pr_section[1].startswith("- [Risk Intern]")  # entry unknown
    assert pr_section[2].startswith("- [Senior Political Risk Broker (London)](https://jobs/pr-sr)")
    assert "Evaluation Associate" not in md  # earlier lanes hidden
    assert "adzuna" not in md  # duplicates hidden


def test_digest_filters_and_empty(store) -> None:
    md = digest(store, days=1, min_score=60, taxonomy=FakeTaxonomy())
    assert "**3** new matches" in md and "Risk Intern" not in md
    empty = digest(store, days=1, min_score=99)
    assert "No new matches in the last 1 day" in empty
    md7 = digest(store, days=7, min_score=60)  # no taxonomy: ids as headings
    assert "## specialty-insurance (3)" in md7 and "Political Risk Underwriter" in md7
    with pytest.raises(ValueError):
        digest(store, fmt="xml")


def test_digest_json(store) -> None:
    data = json.loads(digest(store, days=1, fmt="json", taxonomy=FakeTaxonomy(), limit_per_lane=2))
    assert data["total"] == 6 and data["entry_level"] == 2 and data["min_score"] == 25
    groups = {g["id"]: g for g in data["groups"]}
    assert list(groups) == ["specialty-insurance", "sanctions-fincrime", "unclassified"]
    pr = groups["specialty-insurance"]["lanes"][0]
    assert pr["id"] == "political-risk" and pr["name"] == "Political risk" and pr["count"] == 3
    assert [j["uid"] for j in pr["jobs"]] == ["greenhouse:pr", "greenhouse:ri"]
    assert set(pr["jobs"][0]) >= {"uid", "title", "company", "url", "score", "entry_level", "lanes"}


def test_write_csv(store, tmp_path) -> None:
    rows = store.query(limit=None)
    path = write_csv(rows, tmp_path / "out" / "jobs.csv")
    with open(path, newline="", encoding="utf-8") as fh:
        parsed = list(csv.DictReader(fh))
    assert len(parsed) == len(rows)
    assert parsed[0]["uid"] == rows[0]["uid"]
    sanc = next(r for r in parsed if r["uid"] == "greenhouse:sanc")
    assert sanc["lanes"] == "sanctions" and sanc["entry_level"] == "True" and sanc["company"] == "Kroll"
    buf = io.StringIO()
    write_csv([{"uid": "a:1", "lanes": ["x", "y"], "title": None}], buf)
    assert buf.getvalue().splitlines()[1].startswith("a:1,,")
    assert "x; y" in buf.getvalue()


def test_source_rows(monkeypatch) -> None:
    rows = source_rows(Settings(env={"SOME_KEY": "k"}))
    assert {r["name"]: r["available"] for r in rows} == {"oksrc": True, "keysrc": True, "gone": False}
