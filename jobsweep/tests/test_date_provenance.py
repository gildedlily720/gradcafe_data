"""Publication dates must not be fabricated from last-edit timestamps."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from jobsweep.config import Settings
from jobsweep.date_provenance import repair_posting_date
from jobsweep.models import Employer, Job
from jobsweep.sources.base import SourceContext
from jobsweep.sources.greenhouse import GreenhouseSource
from jobsweep.store import Store

GH = "https://boards-api.greenhouse.io/v1/boards/test/jobs"


def posting(**kwargs) -> Job:
    return Job(
        source="greenhouse",
        source_id="123",
        title="Analyst",
        company="Test",
        url="https://test/jobs/123",
        **kwargs,
    )


def parse(raw: dict) -> Job:
    async def main() -> Job:
        async with httpx.AsyncClient() as client:
            src = GreenhouseSource(SourceContext(client=client, settings=Settings()))
            return src._parse(raw, Employer(key="test", name="Test"), "test")

    return asyncio.run(main())


@pytest.mark.parametrize("published", [None, "not a date", ""])
def test_updated_timestamp_is_never_a_publication_date(published) -> None:
    j = parse(
        {"id": 123, "title": "Analyst", "first_published": published, "updated_at": "2026-10-25T18:00:00Z"}
    )
    assert j.posted_at is None
    assert j.extra["updated_at"] == "2026-10-25T18:00:00Z"
    assert j.extra["posted_at_source"] == "unknown"


def test_genuine_publication_date_keeps_exact_time() -> None:
    j = parse(
        {
            "id": 123,
            "title": "Analyst",
            "first_published": "2026-09-01T10:25:33-04:00",
            "updated_at": "2026-10-25T18:00:00Z",
        }
    )
    assert j.posted_at == "2026-09-01T10:25:33-04:00"
    assert j.extra["posted_at_source"] == "greenhouse:first_published"
    assert j.extra["first_published"] == "2026-09-01T10:25:33-04:00"


def test_detail_recovers_date_even_when_description_already_present() -> None:
    async def main() -> Job:
        async with httpx.AsyncClient() as client:
            src = GreenhouseSource(SourceContext(client=client, settings=Settings()))
            return await src.enrich(
                posting(
                    description="Known description", extra={"board": "test", "posted_at_source": "unknown"}
                )
            )

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{GH}/123").mock(
            return_value=httpx.Response(
                200, json={"first_published": "2026-09-01T10:25:33Z", "updated_at": "2026-10-25T18:00:00Z"}
            )
        )
        j = asyncio.run(main())
    assert j.posted_at == "2026-09-01T10:25:33+00:00"
    assert j.description == "Known description"
    assert j.extra["posted_at_source"] == "greenhouse:first_published"


def test_legacy_update_only_date_cleared_idempotently() -> None:
    j = posting(posted_at="2026-10-25T18:00:00+00:00", extra={"updated_at": "2026-10-25T18:00:00Z"})
    assert repair_posting_date(j)
    assert j.posted_at is None
    assert j.extra["updated_at"] == "2026-10-25T18:00:00Z"
    assert j.extra["posted_at_source"] == "unknown"
    assert not repair_posting_date(j)


def test_same_day_different_time_is_not_erased() -> None:
    j = posting(posted_at="2026-10-25T09:00:00+00:00", extra={"updated_at": "2026-10-25T18:00:00Z"})
    repair_posting_date(j)
    assert j.posted_at == "2026-10-25T09:00:00+00:00"
    assert j.extra["posted_at_source"] == "greenhouse:first_published"


def test_explicit_publication_provenance_survives_equal_update_time() -> None:
    j = posting(
        posted_at="2026-10-25T18:00:00+00:00",
        extra={"updated_at": "2026-10-25T18:00:00Z", "posted_at_source": "greenhouse:first_published"},
    )
    assert not repair_posting_date(j)
    assert j.posted_at is not None


def test_stored_raw_publication_restores_real_date() -> None:
    j = posting(
        posted_at="2026-10-25T18:00:00+00:00",
        extra={"updated_at": "2026-10-25T18:00:00Z", "first_published": "2026-09-01T10:00:00Z"},
    )
    assert repair_posting_date(j)
    assert j.posted_at == "2026-09-01T10:00:00+00:00"


def test_other_sources_are_untouched() -> None:
    j = posting(posted_at="2026-10-25T18:00:00+00:00", extra={"updated_at": "2026-10-25T18:00:00Z"})
    j.source = "ashby"
    assert not repair_posting_date(j)
    assert j.posted_at is not None


def test_store_posted_sort_uses_full_timestamps_and_timezones() -> None:
    dates = [
        ("older", "2026-10-24T21:00:00Z", 99),
        ("hour", "2026-10-25T20:00:00Z", 90),
        ("minute", "2026-10-25T20:59:00Z", 25),
        ("offset", "2026-10-25T16:30:00-04:00", 50),
        ("unknown", None, 100),
        ("invalid", "not a date", 100),
    ]
    with Store(":memory:") as store:
        jobs = []
        for uid, date, score in dates:
            j = posting(posted_at=date, score=score)
            j.source_id = uid
            jobs.append(j)
        store.upsert_jobs(jobs)
        rows = store.query(sort="posted", min_score=0)
        assert [row["source_id"] for row in rows[:4]] == ["minute", "offset", "hour", "older"]
        assert {row["source_id"] for row in rows[4:]} == {"unknown", "invalid"}


def test_refresh_clears_legacy_fallback_and_preserves_user_status() -> None:
    with Store(":memory:") as store:
        j = posting(posted_at="2026-10-25T18:00:00Z", extra={"updated_at": "2026-10-25T18:00:00Z"})
        store.upsert_jobs([j])
        store.set_status(j.uid, "saved", "Keep my note")
        first_seen = store.get(j.uid)["first_seen"]
        fresh = parse({"id": 123, "title": "Analyst", "updated_at": "2026-10-25T18:00:00Z"})
        store.upsert_jobs([fresh])
        row = store.get(j.uid)
        assert row["posted_at"] is None
        assert row["first_seen"] == first_seen
        assert (row["status"], row["note"]) == ("saved", "Keep my note")
