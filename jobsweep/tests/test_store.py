from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from jobsweep.models import Job
from jobsweep.store import Store, load_lane_map

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now = self.now + timedelta(**kw)


def job(source="greenhouse", sid="1", title="Political Risk Analyst", company="Beazley", **kw) -> Job:
    kw.setdefault("url", f"https://example.com/{source}/{sid}")
    kw.setdefault("location", "London, UK")
    kw.setdefault("lanes", ["political-risk"])
    kw.setdefault("groups", ["specialty-insurance"])
    kw.setdefault("score", 60.0)
    return Job(source=source, source_id=sid, title=title, company=company, **kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Store:
    s = Store(":memory:", clock=clock)
    yield s
    s.close()


def uids(rows: list[dict]) -> list[str]:
    return [r["uid"] for r in rows]


# ---- upsert / new detection ---------------------------------------------------------------------


def test_upsert_new_then_updated_preserves_first_seen(store: Store, clock: Clock) -> None:
    res = store.upsert_jobs([job(), job(sid="2", title="Sanctions Analyst", lanes=["sanctions"])])
    assert res["new"] == ["greenhouse:1", "greenhouse:2"]
    assert res["updated"] == 0
    first = store.get("greenhouse:1")
    assert first["first_seen"] == first["last_seen"] == "2026-10-01T12:00:00+00:00"
    assert first["active"] is True and first["status"] == "new" and first["duplicate_of"] is None

    clock.advance(days=2)
    res = store.upsert_jobs([job(score=80.0)])
    assert res == {"new": [], "updated": 1, "duplicates": []}
    row = store.get("greenhouse:1")
    assert row["first_seen"] == "2026-10-01T12:00:00+00:00"
    assert row["last_seen"] == "2026-10-03T12:00:00+00:00"
    assert row["score"] == 80.0
    # row dict = Job.to_dict() + store fields
    for k in ("uid", "title", "lanes", "match_reasons", "first_seen", "status", "note", "status_updated_at"):
        assert k in row


def test_upsert_accepts_dicts_and_records_runs(store: Store) -> None:
    run_id = store.start_run({"options": {"x": 1}})
    res = store.upsert_jobs([job().to_dict()], run_id=run_id)
    assert res["new"] == ["greenhouse:1"]
    store.finish_run(run_id, {"new": 1})
    runs = store.runs()
    assert runs[0]["id"] == run_id and runs[0]["meta"] == {"options": {"x": 1}}
    assert runs[0]["stats"] == {"new": 1} and runs[0]["finished_at"]


def test_reupsert_reactivates_inactive_job(store: Store, clock: Clock) -> None:
    store.upsert_jobs([job(source="adzuna", sid="9", employer_key=None)])
    clock.advance(days=40)
    assert store.expire(30) == 1
    assert store.get("adzuna:9")["active"] is False
    store.upsert_jobs([job(source="adzuna", sid="9")])
    assert store.get("adzuna:9")["active"] is True


# ---- status ---------------------------------------------------------------------------------------


def test_set_status_survives_reupsert(store: Store, clock: Clock) -> None:
    store.upsert_jobs([job()])
    row = store.set_status("greenhouse:1", "saved", note="apply by Friday")
    assert row["status"] == "saved" and row["note"] == "apply by Friday"
    assert row["status_updated_at"] == "2026-10-01T12:00:00+00:00"
    clock.advance(hours=5)
    store.upsert_jobs([job(title="Political Risk Analyst", score=90)])
    row = store.get("greenhouse:1")
    assert (row["status"], row["note"]) == ("saved", "apply by Friday")
    # note=None keeps the note, a new status replaces the status
    row = store.set_status("greenhouse:1", "applied")
    assert (row["status"], row["note"]) == ("applied", "apply by Friday")


def test_set_status_errors(store: Store) -> None:
    store.upsert_jobs([job()])
    with pytest.raises(ValueError):
        store.set_status("greenhouse:1", "maybe")
    assert store.set_status("nope:1", "saved") is None


# ---- duplicates -----------------------------------------------------------------------------------


def test_cross_source_duplicates(store: Store, clock: Clock) -> None:
    store.upsert_jobs([job(source="greenhouse", sid="1", company="Beazley")])
    clock.advance(minutes=5)
    res = store.upsert_jobs(
        [
            job(source="adzuna", sid="77", company="Beazley plc", location="London"),  # same fingerprint
            job(source="greenhouse", sid="2"),  # same source, same fingerprint -> not a duplicate
        ]
    )
    assert res["duplicates"] == ["adzuna:77"]
    assert store.get("adzuna:77")["duplicate_of"] == "greenhouse:1"
    assert store.get("greenhouse:2")["duplicate_of"] is None
    assert uids(store.query(min_score=0)) == ["greenhouse:2", "greenhouse:1"]  # same score: newest first
    assert store.count() == 2
    assert set(uids(store.query(include_duplicates=True))) == {"greenhouse:1", "greenhouse:2", "adzuna:77"}
    assert store.stats()["duplicates"] == 1

    # canonical disappears from its board -> the duplicate becomes canonical on its next sighting
    store.mark_board_complete("beazley-none", set())  # unrelated employer: no-op
    store._conn.execute("UPDATE jobs SET active = 0 WHERE uid = 'greenhouse:1'")
    store.upsert_jobs([job(source="adzuna", sid="77", company="Beazley plc", location="London")])
    assert store.get("adzuna:77")["duplicate_of"] == "greenhouse:2"  # next active same-fingerprint job
    store._conn.execute("UPDATE jobs SET active = 0 WHERE uid = 'greenhouse:2'")
    store.upsert_jobs([job(source="adzuna", sid="77", company="Beazley plc", location="London")])
    assert store.get("adzuna:77")["duplicate_of"] is None


def test_blank_fingerprint_never_duplicates(store: Store) -> None:
    store.upsert_jobs([job(source="a", sid="1", company="", title="Analyst")])
    store.upsert_jobs([job(source="b", sid="1", company="", title="Analyst")])
    assert store.get("b:1")["duplicate_of"] is None


# ---- filters --------------------------------------------------------------------------------------


@pytest.fixture
def filled(store: Store, clock: Clock) -> Store:
    store.upsert_jobs(
        [
            job(sid="pr", title="Political Risk Analyst", score=80, entry_level=True, location="New York, NY",
                posted_at="2026-09-20", closes_at="2026-10-20", description="Lloyd's political risk underwriting"),
            job(source="lever", sid="sanc", title="Senior Sanctions Manager", company="Kroll", lanes=["sanctions"],
                groups=["sanctions-fincrime"], score=55, entry_level=False, location="Washington, DC",
                posted_at="2026-09-28", description="OFAC sanctions screening"),
            job(source="usajobs", sid="cti", title="Intelligence Specialist", company="DHS",
                lanes=["threat-intelligence", "osint"], groups=["intelligence-security"], score=40,
                entry_level=None, remote=True, location="Remote"),
            job(source="lever", sid="eval", title="Program Evaluation Associate", company="Mathematica",
                lanes=["program-evaluation"], groups=["earlier-lanes"], score=70, entry_level=True),
            job(source="lever", sid="low", title="Office Manager", company="Acme", lanes=[], groups=[], score=5),
        ]
    )  # fmt: skip
    clock.advance(days=3)
    store.upsert_jobs([job(source="ashby", sid="new", title="Trust and Safety Analyst", company="OpenAI",
                           lanes=["trust-safety-policy"], groups=["trust-safety"], score=65, entry_level=True)])  # fmt: skip
    return store


def test_filter_lanes_groups(filled: Store) -> None:
    assert uids(filled.query(lanes=["osint"])) == ["usajobs:cti"]
    assert set(uids(filled.query(lanes=["osint", "sanctions"]))) == {"usajobs:cti", "lever:sanc"}
    assert uids(filled.query(groups="sanctions-fincrime")) == ["lever:sanc"]
    assert filled.count(groups=["specialty-insurance", "intelligence-security"]) == 2


def test_filter_inactive_lanes(filled: Store) -> None:
    assert "lever:eval" not in uids(filled.query(limit=0))
    assert "lever:eval" in uids(filled.query(include_inactive_lanes=True))
    # asking for an earlier lane explicitly shows it
    assert uids(filled.query(lanes=["program-evaluation"])) == ["lever:eval"]
    assert uids(filled.query(groups=["earlier-lanes"])) == ["lever:eval"]
    # mixed earlier + active lane is shown
    filled.upsert_jobs([job(sid="mix", lanes=["program-evaluation", "think-tank-research"],
                            groups=["earlier-lanes", "policy-research"])])  # fmt: skip
    assert "greenhouse:mix" in uids(filled.query(limit=None))


def test_filter_entry_score_location_remote_sources(filled: Store) -> None:
    assert set(uids(filled.query(entry_only=True))) == {
        "greenhouse:pr",
        "usajobs:cti",
        "lever:low",
        "ashby:new",
    }
    assert set(uids(filled.query(entry_only=True, include_unknown_entry=False))) == {
        "greenhouse:pr",
        "ashby:new",
    }
    assert set(uids(filled.query(min_score=50))) == {"greenhouse:pr", "lever:sanc", "ashby:new"}
    assert uids(filled.query(location="new york")) == ["greenhouse:pr"]
    assert uids(filled.query(location="remote")) == ["usajobs:cti"]
    assert uids(filled.query(remote=True)) == ["usajobs:cti"]
    assert set(uids(filled.query(sources=["lever"]))) == {"lever:sanc", "lever:low"}
    assert set(uids(filled.query(employers=None, sources="lever,usajobs"))) == {
        "lever:sanc",
        "lever:low",
        "usajobs:cti",
    }


def test_filter_q_fts_and_like(filled: Store) -> None:
    assert filled.fts
    assert uids(filled.query(q="underwriting")) == ["greenhouse:pr"]
    assert uids(filled.query(q="sanction")) == ["lever:sanc"]  # prefix match
    assert uids(filled.query(q='"political risk"')) == ["greenhouse:pr"]
    assert set(uids(filled.query(q="ofac OR lloyd"))) == {"greenhouse:pr", "lever:sanc"}
    assert uids(filled.query(q="kroll sanctions")) == ["lever:sanc"]  # company + body
    assert filled.query(q="'(*") == []  # odd punctuation never raises
    assert uids(filled.query(q="OR sanctions AND")) == ["lever:sanc"]  # dangling operators dropped
    assert len(filled.query(q="  ")) == 5  # blank q = no text filter
    filled.fts = False
    assert uids(filled.query(q="underwriting")) == ["greenhouse:pr"]
    assert uids(filled.query(q="Kroll")) == ["lever:sanc"]


def test_filter_since_and_statuses(filled: Store, clock: Clock) -> None:
    assert uids(filled.query(since="2026-10-03")) == ["ashby:new"]
    assert uids(filled.query(since=1)) == ["ashby:new"]  # days
    assert len(filled.query(since="2026-09-30T00:00:00Z")) == 5
    filled.set_status("lever:sanc", "applied")
    filled.set_status("lever:low", "dismissed")
    assert uids(filled.query(statuses=["applied"])) == ["lever:sanc"]
    assert filled.count(statuses=["new"]) == 3
    # triaged jobs stay visible after the posting closes when asked for by status
    filled._conn.execute("UPDATE jobs SET active = 0 WHERE uid = 'lever:sanc'")
    assert uids(filled.query(statuses=["applied"])) == ["lever:sanc"]
    assert "lever:sanc" not in uids(filled.query())
    assert "lever:sanc" in uids(filled.query(active_only=False))
    with pytest.raises(ValueError):
        filled.query(statuses=["bogus"])
    with pytest.raises(TypeError):
        filled.query(colour="red")


def test_sort_limit_offset(filled: Store) -> None:
    assert uids(filled.query())[:2] == ["greenhouse:pr", "ashby:new"]
    assert uids(filled.query(sort="newest"))[0] == "ashby:new"
    assert uids(filled.query(sort="posted"))[:2] == ["lever:sanc", "greenhouse:pr"]
    closing = uids(filled.query(sort="closing"))
    assert closing[0] == "greenhouse:pr"
    assert uids(filled.query(sort="company"))[0] == "lever:low"  # Acme
    page1, page2 = filled.query(limit=2), filled.query(limit=2, offset=2)
    assert len(page1) == 2 and not set(uids(page1)) & set(uids(page2))
    assert filled.count(sort="score", limit=1, offset=3) == 5  # paging args ignored


# ---- stats ----------------------------------------------------------------------------------------


def test_stats(filled: Store) -> None:
    run = filled.start_run({})
    filled.finish_run(run, {"new": 6})
    filled.set_status("greenhouse:pr", "saved")
    st = filled.stats()
    assert st["total"] == 6 and st["active"] == 6
    assert st["new_24h"] == 1 and st["new_7d"] == 6
    assert st["by_group"]["specialty-insurance"] == 1
    assert st["by_group"]["earlier-lanes"] == 1
    assert st["by_lane"]["osint"] == 1 and st["by_lane"]["threat-intelligence"] == 1
    assert st["by_source"] == {"lever": 3, "greenhouse": 1, "usajobs": 1, "ashby": 1}
    assert st["by_status"] == {"new": 5, "saved": 1}
    assert st["last_run"]["id"] == run and st["last_run"]["stats"] == {"new": 6}


# ---- lifecycle ------------------------------------------------------------------------------------


def test_mark_board_complete(store: Store) -> None:
    store.upsert_jobs([job(sid=s, title=f"Analyst {s}", employer_key="beazley") for s in "abc"])
    store.upsert_jobs([job(source="adzuna", sid="z", title="Other", employer_key="beazley")])
    n = store.mark_board_complete("beazley", {"greenhouse:a", "greenhouse:c"}, source="greenhouse")
    assert n == 1
    assert store.get("greenhouse:b")["active"] is False
    assert store.get("adzuna:z")["active"] is True  # search jobs are not touched
    # without source: sources inferred from seen uids
    assert store.mark_board_complete("beazley", {"greenhouse:a"}) == 1
    # nothing seen and no source: every non-search source of that employer
    assert store.mark_board_complete("beazley", set()) == 1
    assert store.get("adzuna:z")["active"] is True


def test_expire(store: Store, clock: Clock) -> None:
    store.upsert_jobs(
        [
            job(source="usajobs", sid="old"),
            job(source="greenhouse", sid="board", employer_key="beazley"),
            job(source="greenhouse", sid="closing", employer_key="beazley", closes_at="2026-10-05T23:59:00"),
        ]
    )
    clock.advance(days=10)
    assert store.expire(30) == 1  # closes_at passed
    assert store.get("greenhouse:closing")["active"] is False
    clock.advance(days=25)
    assert store.expire(30) == 1  # search job unseen 35 days
    assert store.get("usajobs:old")["active"] is False
    assert store.get("greenhouse:board")["active"] is True  # boards retire via mark_board_complete
    clock.advance(days=60)
    assert store.expire(30) == 1  # ...unless unseen for 90+ days
    assert store.get("greenhouse:board")["active"] is False


def test_prune(store: Store, clock: Clock) -> None:
    store.upsert_jobs([job(source="usajobs", sid="a"), job(source="usajobs", sid="b", title="Other")])
    store.set_status("usajobs:b", "applied")
    clock.advance(days=40)
    store.expire(30)
    clock.advance(days=200)
    assert store.prune(180) == 1
    assert store.get("usajobs:a") is None
    assert store.get("usajobs:b")["status"] == "applied"


# ---- snapshots ------------------------------------------------------------------------------------


def test_snapshot_round_trip_and_merge(clock: Clock) -> None:
    a = Store(":memory:", clock=clock)
    a.upsert_jobs(
        [job(sid="1"), job(sid="2", title="Sanctions Analyst", lanes=["sanctions"], description="x" * 50)]
    )
    a.set_status("greenhouse:1", "saved", "from A")
    run = a.start_run({"who": "A"})
    a.finish_run(run, {"new": 2})
    snap = a.export_snapshot()
    assert snap["version"] == 1 and snap["exported_at"] and len(snap["jobs"]) == 2 and len(snap["runs"]) == 1

    # B saw job 1 later (newer data), saw it first earlier, and has an older status
    b_clock = Clock(T0 - timedelta(days=3))
    b = Store(":memory:", clock=b_clock)
    b.upsert_jobs([job(sid="1", score=10)])
    b.set_status("greenhouse:1", "dismissed", "old")
    b_clock.advance(days=10)
    b.upsert_jobs([job(sid="1", score=99)])
    n = b.import_snapshot(snap)
    assert n == 2
    row = b.get("greenhouse:1")
    assert row["first_seen"] == (T0 - timedelta(days=3)).isoformat()  # earliest
    assert row["last_seen"] == (T0 + timedelta(days=7)).isoformat()  # latest
    assert row["score"] == 99  # data from the newer sighting
    assert (row["status"], row["note"]) == ("saved", "from A")  # newer status_updated_at wins
    assert b.get("greenhouse:2")["lanes"] == ["sanctions"]
    assert len(b.runs()) == 1
    # importing twice changes nothing
    assert b.import_snapshot(snap) == 0
    assert len(b.runs()) == 1
    # merged rows are searchable and filterable
    assert uids(b.query(lanes=["sanctions"])) == ["greenhouse:2"]
    assert uids(b.query(q="sanctions")) == ["greenhouse:2"]

    # truncated snapshot
    short = a.export_snapshot(max_description=10)
    assert len(next(r for r in short["jobs"] if r["uid"] == "greenhouse:2")["description"]) <= 12


def test_import_snapshot_status_newer_locally_wins(clock: Clock) -> None:
    a = Store(":memory:", clock=clock)
    a.upsert_jobs([job()])
    a.set_status("greenhouse:1", "saved")
    snap = a.export_snapshot()
    b = Store(":memory:", clock=Clock(T0 + timedelta(days=1)))
    b.import_snapshot(snap)
    b.set_status("greenhouse:1", "applied", "sent CV")
    b.import_snapshot(snap)
    assert b.get("greenhouse:1")["status"] == "applied"


def test_import_snapshot_rejects_garbage(store: Store) -> None:
    with pytest.raises(ValueError):
        store.import_snapshot({"foo": 1})
    with pytest.raises(ValueError):
        store.import_snapshot({"version": 99, "jobs": []})
    # rows with only a uid are accepted
    assert store.import_snapshot({"version": 1, "jobs": [{"uid": "import:x:1", "title": "T"}]}) == 1
    assert store.get("import:x:1")["source_id"] == "x:1"


# ---- infrastructure -------------------------------------------------------------------------------


def test_file_db_wal_and_threads(tmp_path) -> None:
    path = tmp_path / "sub" / "jobs.db"
    s = Store(path)
    assert path.exists()
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                s.upsert_jobs([job(source=f"src{n}", sid=str(i), title=f"Job {n}-{i}")])
                s.query(q="job", limit=5)
                s.count()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert s.count() == 80
    s.close()
    # reopening keeps data and schema
    s2 = Store(path)
    assert s2.count() == 80
    s2.close()


def test_lane_map_fallback(tmp_path) -> None:
    lane_group, inactive_lanes, inactive_groups = load_lane_map(str(tmp_path / "missing.yaml"))
    assert lane_group["political-risk"] == "specialty-insurance"
    assert "program-evaluation" in inactive_lanes and inactive_groups == {"earlier-lanes"}
    custom = tmp_path / "lanes.yaml"
    custom.write_text(
        "groups:\n  - id: g1\n    active: true\n    lanes:\n      - id: l1\n      - id: l2\n        active: false\n"
        "  - id: old\n    active: false\n    lanes:\n      - id: l3\n",
        encoding="utf-8",
    )
    lane_group, inactive_lanes, inactive_groups = load_lane_map(str(custom))
    assert lane_group == {"l1": "g1", "l2": "g1", "l3": "old"}
    assert inactive_lanes == {"l2", "l3"} and inactive_groups == {"old"}


def test_unknown_lane_uses_single_group(store: Store) -> None:
    store.upsert_jobs([job(sid="u", lanes=["brand-new-lane"], groups=["policy-research"])])
    assert uids(store.query(groups=["policy-research"])) == ["greenhouse:u"]
    assert uids(store.query(lanes=["brand-new-lane"])) == ["greenhouse:u"]
