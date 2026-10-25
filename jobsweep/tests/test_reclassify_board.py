from __future__ import annotations

import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobsweep.employers import EmployerRegistry
from jobsweep.export import site_rows
from jobsweep.models import Job
from jobsweep.store import Store
from jobsweep.taxonomy import Taxonomy

reclassify_store = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "reclassify_board.py")
)["reclassify_store"]


def test_reclassification_preserves_lifecycle_and_tracking():
    observed = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    with Store(":memory:", clock=lambda: observed) as store:
        j = Job(
            source="greenhouse",
            source_id="old",
            title="Executive Assistant, Intelligence",
            company="Acme",
            url="https://example.com/old",
            location="New York, NY",
            description="Intelligence and investigations. 2 years experience.",
            score=93,
            entry_level=True,
            lanes=["corporate-intelligence"],
            groups=["investigations-dd"],
            employer_key="acme",
            posted_at="2026-09-30T11:20:00Z",
            extra={"updated_at": "2026-09-30T11:20:00Z"},
        )
        run_id = store.start_run({"test": True})
        store.upsert_jobs([j], run_id=run_id)
        store.clock = lambda: observed + timedelta(days=1)
        store.upsert_jobs([j], run_id=run_id)
        store.set_status(j.uid, "applied", "Referral received; follow up next week")
        store.mark_board_complete("acme", [], source="greenhouse")
        before = dict(store._conn.execute("SELECT * FROM jobs WHERE uid = ?", (j.uid,)).fetchone())
        store.clock = lambda: observed + timedelta(days=10)
        taxonomy, registry = Taxonomy.load(), EmployerRegistry([])
        counts = reclassify_store(store, taxonomy, registry)
        after = dict(store._conn.execute("SELECT * FROM jobs WHERE uid = ?", (j.uid,)).fetchone())
        assert counts == {"scanned": 1, "changed": 1, "dates_repaired": 1}
        for key in (
            "first_seen",
            "last_seen",
            "active",
            "status",
            "note",
            "status_updated_at",
            "duplicate_of",
            "first_run",
            "last_run",
        ):
            assert after[key] == before[key], key
        current = store.get(j.uid)
        assert current["suitability"] == "excluded"
        assert current["score"] == 0
        assert current["posted_at"] is None
        assert current["extra"]["posted_at_source"] == "unknown"
        # Triage rows remain accessible after they fail the new relevance/location filters.
        exported = site_rows(store, min_score=25)
        assert len(exported) == 1
        assert exported[0]["status"] == "applied"
        assert exported[0]["posted_at_source"] == "unknown"
        assert exported[0]["source_updated_at"] == "2026-09-30T11:20:00Z"
        assert "extra" not in exported[0]
        assert reclassify_store(store, taxonomy, registry) == {
            "scanned": 1,
            "changed": 0,
            "dates_repaired": 0,
        }


def test_reclassification_preserves_duplicate_links():
    with Store(":memory:") as store:
        common = dict(
            title="Investigations Analyst",
            company="Acme",
            location="New York, NY",
            url="https://example.com/role",
            description="Research public records. 2 years experience.",
        )
        store.upsert_jobs([Job(source="greenhouse", source_id="1", **common)])
        store.upsert_jobs([Job(source="adzuna", source_id="2", **common)])
        before = store.get("adzuna:2")["duplicate_of"]
        assert before == "greenhouse:1"
        reclassify_store(store, Taxonomy.load(), EmployerRegistry([]))
        assert store.get("adzuna:2")["duplicate_of"] == before


def test_static_feed_retains_excluded_id_for_browser_only_tracking():
    with Store(":memory:") as store:
        j = Job(
            source="test",
            source_id="locally-saved",
            title="AML Engineer",
            company="Acme",
            url="https://example.com/role",
            location="New York, NY",
            score=0,
            suitability="excluded",
            role_fit="excluded",
        )
        store.upsert_jobs([j])
        # Its saved status exists only in the old browser, so the server still calls this 'new'.
        assert store.get(j.uid)["status"] == "new"
        rows = site_rows(store, min_score=25)
        assert rows[0]["uid"] == j.uid
        assert rows[0]["suitability"] == "excluded"


def test_static_feed_retains_tracked_inactive_duplicate():
    with Store(":memory:") as store:
        common = dict(
            title="Investigations Analyst",
            company="Acme",
            location="New York, NY",
            url="https://example.com/role",
            employer_key="acme",
            score=50,
        )
        store.upsert_jobs([Job(source="greenhouse", source_id="1", **common)])
        store.upsert_jobs([Job(source="adzuna", source_id="2", **common)])
        store.set_status("adzuna:2", "saved")
        store.mark_board_complete("acme", [], source="adzuna")
        rows = site_rows(store, min_score=25)
        tracked = next(r for r in rows if r["uid"] == "adzuna:2")
        assert tracked["duplicate_of"] == "greenhouse:1"
        assert tracked["active"] is False
        assert tracked["status"] == "saved"
