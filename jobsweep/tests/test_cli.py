from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import jobsweep.sources as sources_mod
from jobsweep import cli
from jobsweep.employers import EmployerRegistry
from jobsweep.models import Employer, Job, Profile, SearchQuery
from jobsweep.sources.base import Source
from jobsweep.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

RULES = {
    "political risk": ("political-risk", "specialty-insurance"),
    "sanctions": ("sanctions", "sanctions-fincrime"),
    "geopolitical": ("geopolitical-analysis", "geopolitical-risk"),
}


class FakeTaxonomy:
    def __init__(self) -> None:
        self.profile = Profile(min_score=25)
        self.groups = [
            SimpleNamespace(
                id=g, name=g.title(), active=True, lanes=[SimpleNamespace(id=lane, name=lane.title())]
            )
            for lane, g in RULES.values()
        ]

    def title_signal(self, title: str, employer=None) -> float:
        return 10.0 if any(k in title.lower() for k in RULES) else 0.0

    def classify(self, job: Job, employer=None) -> Job:
        text = f"{job.title} {job.description}".lower()
        hits = [v for k, v in RULES.items() if k in text]
        job.lanes = [lane for lane, _ in hits]
        job.groups = list(dict.fromkeys(g for _, g in hits))
        job.score = (60.0 if hits else 5.0) + (15.0 if employer is not None else 0.0)
        job.entry_level = "senior" not in job.title.lower()
        return job

    def search_plan(self, lane_ids=None, source=None, posted_within_days=None) -> list[SearchQuery]:
        return [SearchQuery(keywords=f"q{i}", lane="political-risk") for i in range(2)]

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
    return Job(source=source, source_id=sid, title=title, company=company, url=f"https://x/{sid}", **kw)


class FakeBoard(Source):
    name = "fakeboard"
    kind = "board"
    boards: dict = {}

    async def fetch_board(self, employer: Employer) -> list[Job]:
        res = self.boards[employer.board]
        if isinstance(res, Exception):
            raise res
        return [Job.from_dict(j.to_dict()) for j in res]


class FakeSearch(Source):
    name = "fakesearch"
    kind = "search"
    fail = False

    async def search(self, query: SearchQuery) -> list[Job]:
        if self.fail:
            raise RuntimeError("search backend down")
        return [
            mkjob(
                "fakesearch", query.keywords, f"Political Risk Analyst {query.keywords}", company="Search Co"
            )
        ]


class SecretSource(Source):
    name = "secretsrc"
    kind = "search"
    requires_env = ("FAKE_API_KEY",)
    metered = True


BOARDS = {
    "acme": [
        mkjob("fakeboard", "a1", "Political Risk Analyst", employer_key="acme"),
        mkjob("fakeboard", "a2", "Senior Sanctions Manager", employer_key="acme"),
        mkjob("fakeboard", "a3", "Chef", employer_key="acme"),
    ],
    "globex": [mkjob("fakeboard", "g1", "Geopolitical Analyst", company="Globex", employer_key="globex")],
    "broken": RuntimeError("HTTP 500 for https://boards.example/broken"),
}

YAML_A = """\
# Intelligence & security employers (hand-curated)
group: intelligence-security
employers:
  - key: acme            # keep this comment
    name: Acme
    lanes: [threat-intelligence]
    ats: fakeboard
    board: acme
    verified: null  # set by verify-employers
    notes: >
      multi line
      notes here
  - key: broken
    name: Broken Co
    ats: fakeboard
    board: broken
    open_jobs: 5
  # manual one below
  - key: manualco
    name: Manual Co
    ats: other
    ats_vendor: icims
    careers_url: https://manual.example/careers

extra_top_level: 1
"""

YAML_B = """\
group: sanctions-fincrime
employers:
- key: globex
  name: Globex
  ats: fakeboard
  board: globex
  locations:
    - New York, NY
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "var" / "jobs.db"
    monkeypatch.setenv("JOBSWEEP_DB", str(db))
    monkeypatch.delenv("JOBSWEEP_PROFILE", raising=False)
    emp_dir = tmp_path / "employers"
    emp_dir.mkdir()
    (emp_dir / "intelligence-security.yaml").write_text(YAML_A, encoding="utf-8")
    (emp_dir / "sanctions-fincrime.yaml").write_text(YAML_B, encoding="utf-8")
    monkeypatch.setattr(cli, "_load_taxonomy", lambda settings: FakeTaxonomy())
    monkeypatch.setattr(cli, "_load_registry", lambda settings: EmployerRegistry.load(emp_dir))
    monkeypatch.setattr(cli, "_employers_dir", lambda: emp_dir)
    fakes = {"fakeboard": FakeBoard, "fakesearch": FakeSearch, "secretsrc": SecretSource}
    monkeypatch.setattr(sources_mod, "SOURCE_PATHS", {k: f"x:{k}" for k in fakes})
    monkeypatch.setattr(sources_mod, "_cache", dict(fakes))
    FakeBoard.boards = dict(BOARDS)
    FakeSearch.fail = False
    return SimpleNamespace(db=db, tmp=tmp_path, emp_dir=emp_dir)


def seed(db: Path) -> None:
    s = Store(db)
    tax = FakeTaxonomy()
    jobs = [
        mkjob("greenhouse", "pr", "Political Risk Analyst", company="Beazley", location="New York, NY"),
        mkjob("greenhouse", "sr", "Senior Sanctions Lead", company="Kroll", location="London"),
        mkjob("lever", "chef", "Chef"),
        mkjob("adzuna", "geo", "Geopolitical Analyst", company="RANE", description="Europe desk"),
    ]
    s.upsert_jobs([tax.classify(j) for j in jobs])
    s.close()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys, *argv: str) -> tuple[int, object]:
    code, out, _ = run(capsys, *argv)
    return code, json.loads(out)


# ---- usage ----------------------------------------------------------------------------------------


def test_usage_exit_codes(env, capsys) -> None:
    assert run(capsys)[0] == 2
    assert run(capsys, "--help")[0] == 0
    assert run(capsys, "--version")[0] == 0
    assert run(capsys, "no-such-command")[0] == 2
    assert run(capsys, "status", "greenhouse:pr", "bogus")[0] == 2
    assert run(capsys, "list", "--json", "--csv", "x.csv")[0] == 2


# ---- jobs: list / show / status -------------------------------------------------------------------


def test_status_show_list(env, capsys) -> None:
    seed(env.db)
    code, out, _ = run(capsys, "status", "greenhouse:pr", "saved", "--note", "great fit")
    assert code == 0 and "saved" in out
    assert run(capsys, "status", "nope:1", "saved")[0] == 1

    code, row = run_json(capsys, "show", "greenhouse:pr", "--json")
    assert code == 0 and row["status"] == "saved" and row["note"] == "great fit"
    code, out, _ = run(capsys, "show", "greenhouse:pr")
    assert "Political Risk Analyst — Beazley" in out and "great fit" in out
    assert run(capsys, "show", "missing:1")[0] == 1

    code, data = run_json(capsys, "list", "--json")
    assert code == 0
    assert data["total"] == 3 and {r["uid"] for r in data["items"]} == {
        "greenhouse:pr",
        "greenhouse:sr",
        "adzuna:geo",
    }
    _, data = run_json(capsys, "list", "--json", "--min-score", "0")
    assert data["total"] == 4
    _, data = run_json(capsys, "list", "--json", "--lane", "sanctions,geopolitical-analysis")
    assert {r["uid"] for r in data["items"]} == {"greenhouse:sr", "adzuna:geo"}
    _, data = run_json(capsys, "list", "--json", "--group", "specialty-insurance")
    assert [r["uid"] for r in data["items"]] == ["greenhouse:pr"]
    _, data = run_json(capsys, "list", "--json", "--entry")
    assert {r["uid"] for r in data["items"]} == {"greenhouse:pr", "adzuna:geo"}
    _, data = run_json(capsys, "list", "--json", "--q", "europe")
    assert [r["uid"] for r in data["items"]] == ["adzuna:geo"]
    _, data = run_json(
        capsys, "list", "--json", "--source", "adzuna", "--source", "lever", "--min-score", "0"
    )
    assert {r["uid"] for r in data["items"]} == {"adzuna:geo", "lever:chef"}
    _, data = run_json(capsys, "list", "--json", "--status", "saved")
    assert [r["uid"] for r in data["items"]] == ["greenhouse:pr"]
    _, data = run_json(capsys, "list", "--json", "--location", "london", "--limit", "1")
    assert [r["uid"] for r in data["items"]] == ["greenhouse:sr"]

    # dismissed jobs are hidden unless asked for
    run(capsys, "status", "adzuna:geo", "dismissed")
    _, data = run_json(capsys, "list", "--json")
    assert "adzuna:geo" not in {r["uid"] for r in data["items"]}
    _, data = run_json(capsys, "list", "--json", "--status", "all")
    assert "adzuna:geo" in {r["uid"] for r in data["items"]}

    code, out, _ = run(capsys, "list", "--sort", "newest")
    assert code == 0 and "greenhouse:pr" in out and "of 2 jobs" in out

    csv_path = env.tmp / "out.csv"
    assert run(capsys, "list", "--csv", str(csv_path), "--min-score", "0")[0] == 0
    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("uid,title") and len(lines) == 4


def test_db_flag_overrides_env(env, capsys, tmp_path) -> None:
    other = tmp_path / "other.db"
    seed(other)
    _, data = run_json(capsys, "--db", str(other), "list", "--json")
    assert data["total"] == 3
    _, data = run_json(capsys, "list", "--json")
    assert data["total"] == 0


# ---- sweep / search -------------------------------------------------------------------------------


def test_sweep_dry_run_and_run(env, capsys) -> None:
    code, rep = run_json(capsys, "sweep", "--dry-run", "--json")
    assert code == 0 and rep["dry_run"] is True
    assert {b["employer"] for b in rep["plan"]["boards"]} == {"acme", "broken", "globex"}
    assert rep["plan"]["counts"]["searches"] == 2
    assert {s["source"] for s in rep["plan"]["skipped_sources"]} == {"secretsrc"}

    code, out, err = run(capsys, "sweep", "--no-searches", "--max-queries", "1", "--days", "7")
    assert code == 0  # one failing board is not fatal
    assert "Sweep run #1" in out and "broken" in out and "HTTP 500" in out
    assert "[3/3]" in err
    store = Store(env.db)
    assert store.get("fakeboard:a1")["employer_key"] == "acme"
    assert store.get("fakeboard:a3") is None  # no lane match
    assert store.runs()[0]["stats"]["errors"] == 1
    store.close()

    assert run(capsys, "sweep", "--no-searches", "--strict", "-q")[0] == 1
    code, rep = run_json(capsys, "sweep", "--sources", "fakesearch", "--json", "-q")
    assert code == 0 and rep["per_source"]["fakesearch"]["ok"] == 2
    FakeSearch.fail = True
    assert run(capsys, "sweep", "--sources", "fakesearch", "-q")[0] == 1  # every target failed


def test_search(env, capsys) -> None:
    code, data = run_json(capsys, "search", "risk analyst", "--json", "--no-store", "--location", "New York")
    assert code == 0 and data["total"] == 1 and data["items"][0]["uid"] == "fakesearch:risk analyst"
    assert not Store(env.db).count(min_score=0)
    code, out, _ = run(capsys, "search", "risk", "--sources", "fakesearch,secretsrc")
    assert code == 0 and "1 of 1 results (stored)" in out
    assert Store(env.db).get("fakesearch:risk")["lanes"] == ["political-risk"]
    FakeSearch.fail = True
    assert run(capsys, "search", "risk")[0] == 1


# ---- import ---------------------------------------------------------------------------------------


def test_import_with_stub_parser(env, capsys, monkeypatch) -> None:
    seen = {}

    def fake_parse(payload, origin, fmt, settings=None):
        seen.update(origin=origin, fmt=fmt, payload=payload)
        return [
            mkjob("import", f"{origin}:1", "Geopolitical Analyst", company="Globex Inc"),
            {
                "source": "import",
                "source_id": f"{origin}:2",
                "title": "Barista",
                "company": "Cafe",
                "url": "u",
            },
        ]

    monkeypatch.setattr(cli, "parse_import", fake_parse)
    f = env.tmp / "results.md"
    f.write_text("**Job Title:** Geopolitical Analyst\n", encoding="utf-8")
    code, out, _ = run(capsys, "import", str(f), "--origin", "indeed-connector")
    assert code == 0 and "imported 2 jobs" in out and "2 new" in out
    assert seen["fmt"] == "md" and seen["origin"] == "indeed-connector" and "Job Title" in seen["payload"]
    row = Store(env.db).get("import:indeed-connector:1")
    assert row["employer_key"] == "globex"  # matched to the registry by company name
    assert row["score"] == 75 and row["lanes"] == ["geopolitical-analysis"]
    assert run(capsys, "import", str(env.tmp / "missing.json"))[0] == 1


def test_detect_format() -> None:
    assert cli._detect_format("a.json", "") == "json"
    assert cli._detect_format("a.csv", "") == "csv"
    assert cli._detect_format("a.md.gz", "") == "md"
    assert cli._detect_format("-", ' [{"title": 1}]') == "json"
    assert cli._detect_format("-", "**Job Title:** X") == "md"
    assert cli._detect_format("-", "title,company,url\nA,B,C") == "csv"


def test_import_indeed_fixture_integration(env, capsys) -> None:
    pytest.importorskip("jobsweep.sources.manual_import")
    sources_mod._cache.pop("import", None)
    code, out, _ = run(capsys, "import", str(FIXTURES / "import" / "indeed_connector_search.md"))
    assert code == 0, out
    rows = Store(env.db).query(min_score=0, limit=None, include_inactive_lanes=True)
    assert rows and all(r["source"] == "import" for r in rows)


# ---- export / digest / state ----------------------------------------------------------------------


def test_export_and_digest(env, capsys) -> None:
    seed(env.db)
    static = env.tmp / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    out_dir = env.tmp / "site"
    code, out, _ = run(capsys, "export", "--out", str(out_dir), "--static", str(static), "--min-score", "50")
    assert code == 0 and "exported 3 jobs" in out
    assert json.loads((out_dir / "data" / "jobs.json").read_text())["total"] == 3
    assert (out_dir / "config.js").read_text().strip() == 'window.JOBSWEEP_MODE = "static";'

    code, data = run_json(capsys, "digest", "--format", "json")
    assert code == 0 and data["total"] == 3
    code, out, _ = run(capsys, "digest", "--days", "2")
    assert code == 0 and out.startswith("# jobsweep digest") and "## Specialty-Insurance (1)" in out
    target = env.tmp / "digest.md"
    assert run(capsys, "digest", "--out", str(target))[0] == 0 and target.read_text().startswith("# jobsweep")


def test_state_export_import(env, capsys, tmp_path) -> None:
    seed(env.db)
    run(capsys, "status", "greenhouse:pr", "applied", "--note", "sent")
    snap_path = tmp_path / "state" / "snap.json.gz"
    code, out, _ = run(capsys, "state", "export", str(snap_path))
    assert code == 0 and "exported 4 jobs" in out
    snap = json.loads(gzip.decompress(snap_path.read_bytes()))
    assert snap["version"] == 1 and len(snap["jobs"]) == 4

    plain = tmp_path / "snap.json"
    assert run(capsys, "state", "export", str(plain), "--max-description", "100")[0] == 0
    assert json.loads(plain.read_text())["version"] == 1

    other = tmp_path / "fresh.db"
    code, out, _ = run(capsys, "--db", str(other), "state", "import", str(snap_path))
    assert code == 0 and "merged 4 of 4" in out
    row = Store(other).get("greenhouse:pr")
    assert (row["status"], row["note"]) == ("applied", "sent")
    assert run(capsys, "state", "import", str(tmp_path / "nope.json"))[0] == 1
    assert run(capsys, "state", "prune", "--days", "1")[0] == 0
    assert run(capsys, "state")[0] == 2


# ---- catalog commands -----------------------------------------------------------------------------


def test_doctor_hides_secret_values(env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ADZUNA_APP_KEY", "supersecret-value")
    seed(env.db)
    code, out, _ = run(capsys, "doctor")
    assert code == 0
    assert "supersecret-value" not in out
    assert "ADZUNA_APP_KEY" in out and "fakeboard" in out and "missing env: FAKE_API_KEY" in out
    assert "employers: 4 (3 sweepable, 0 verified, 1 manual)" in out and "jobs: 4" in out
    code, data = run_json(capsys, "doctor", "--json")
    assert code == 0 and data["env"]["ADZUNA_APP_KEY"] is True
    assert {s["name"]: s["available"] for s in data["sources"]} == {
        "fakeboard": True,
        "fakesearch": True,
        "secretsrc": False,
    }
    assert data["catalog"]["manual"] == 1 and data["db"]["stats"]["total"] == 4
    assert "supersecret-value" not in json.dumps(data)

    def boom(settings):
        raise ImportError("taxonomy not written yet")

    monkeypatch.setattr(cli, "_load_taxonomy", boom)
    code, out, _ = run(capsys, "doctor")
    assert code == 1 and "taxonomy: ERROR" in out


def test_lanes_and_employers(env, capsys) -> None:
    seed(env.db)
    code, data = run_json(capsys, "lanes", "--json")
    assert code == 0
    counts = {lane["id"]: lane["count"] for g in data["groups"] for lane in g["lanes"]}
    assert counts == {"political-risk": 1, "sanctions": 1, "geopolitical-analysis": 1}
    code, out, _ = run(capsys, "lanes")
    assert code == 0 and "political-risk" in out

    _, data = run_json(capsys, "employers", "--json")
    assert sorted(e["key"] for e in data) == ["acme", "broken", "globex"]
    _, data = run_json(capsys, "employers", "--json", "--manual")
    assert [e["key"] for e in data] == ["manualco"]
    _, data = run_json(capsys, "employers", "--json", "--all", "--group", "sanctions-fincrime")
    assert [e["key"] for e in data] == ["globex"]
    code, out, _ = run(capsys, "employers", "--manual")
    assert code == 0 and "icims" in out and "1 employers" in out


# ---- verify-employers -----------------------------------------------------------------------------


def test_verify_employers_write(env, capsys) -> None:
    code, out, _ = run(capsys, "verify-employers", "--write")
    assert code == 1  # the broken board
    assert "3 boards: 2 ok" in out and "HTTP 500" in out
    a = (env.emp_dir / "intelligence-security.yaml").read_text()
    b = (env.emp_dir / "sanctions-fincrime.yaml").read_text()
    today = cli.date.today().isoformat()
    assert "# Intelligence & security employers (hand-curated)" in a
    assert "  - key: acme            # keep this comment" in a
    assert f'    verified: "{today}"  # set by verify-employers' in a
    assert "  # manual one below" in a and "extra_top_level: 1" in a
    doc_a = yaml.safe_load(a)
    acme, broken = doc_a["employers"][0], doc_a["employers"][1]
    assert acme["open_jobs"] == 3 and acme["notes"].startswith("multi line")
    assert broken["open_jobs"] == 5 and "verified" not in broken  # failed board untouched
    globex = yaml.safe_load(b)["employers"][0]
    assert (
        globex["verified"] == today and globex["open_jobs"] == 1 and globex["locations"] == ["New York, NY"]
    )
    # registry still loads
    reg = EmployerRegistry.load(env.emp_dir)
    assert reg.get("acme").open_jobs == 3

    code, data = run_json(capsys, "verify-employers", "--group", "sanctions-fincrime", "--json")
    assert code == 0 and [r["key"] for r in data["results"]] == ["globex"] and data["written"] == []


def test_update_employer_yaml_unit() -> None:
    text = YAML_A
    new, n = cli.update_employer_yaml(text, "broken", {"open_jobs": 7, "verified": "2026-10-25"})
    assert n == 1
    rows = {r["key"]: r for r in yaml.safe_load(new)["employers"]}
    assert rows["broken"]["open_jobs"] == 7 and rows["broken"]["verified"] == "2026-10-25"
    assert rows["acme"].get("open_jobs") is None
    # board mismatch -> untouched; unknown key -> untouched
    assert cli.update_employer_yaml(text, "broken", {"open_jobs": 1}, board="other")[1] == 0
    assert cli.update_employer_yaml(text, "ghost", {"open_jobs": 1}) == (text, 0)
    # flow-style list items are left alone rather than corrupted
    flow = "group: x\nemployers:\n  - {key: a, name: A, ats: lever, board: a}\n"
    assert cli.update_employer_yaml(flow, "a", {"open_jobs": 1}) == (flow, 0)


# ---- detect ---------------------------------------------------------------------------------------


def test_detect_url_without_network(env, capsys) -> None:
    det = pytest.importorskip("jobsweep.sources.detect")
    if not hasattr(det, "detect_from_url"):
        pytest.skip("detect_from_url not available")
    code, data = run_json(capsys, "detect", "https://boards.greenhouse.io/dataminr", "--no-probe", "--json")
    assert code == 0
    assert {"ats": "greenhouse", "board": "dataminr"}.items() <= data["boards"][0].items()
    code, data = run_json(capsys, "detect", "Acme", "--no-probe", "--json")
    assert data["kind"] == "name" and data["registry_matches"][0]["key"] == "acme"
