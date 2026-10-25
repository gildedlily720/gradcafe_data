"""Government feeds: usajobs (direct API + Pipeworx fallback), nycjobs (Socrata), nysjobs (StateJobsNY HTML).

Offline tests replay real responses captured on 2026-10-25 from tests/fixtures/gov/ (trimmed to a few
postings): Pipeworx ``usajobs_search`` results (the same SearchResult JSON as data.usajobs.gov), NYC Open
Data ``kpav-sd4t`` rows, and StateJobsNY ``vacancyTable.cfm`` / ``vacancyDetailsView.cfm`` pages.

Live smoke tests are marked ``live`` and only run with ``-m live`` (or ``JOBSWEEP_LIVE=1``). The USAJOBS one
spends one Pipeworx call (free tier ~50/day).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from jobsweep.config import Settings
from jobsweep.models import Job, SearchQuery
from jobsweep.sources import SOURCE_PATHS, get_source_class
from jobsweep.sources import nycjobs as nyc
from jobsweep.sources import nysjobs as nys
from jobsweep.sources import usajobs as usa
from jobsweep.sources.base import SourceContext, SourceError
from jobsweep.sources.nycjobs import NYCJobsSource
from jobsweep.sources.nysjobs import NYSJobsSource
from jobsweep.sources.usajobs import USAJobsSource

FIX = Path(__file__).parent / "fixtures" / "gov"
TODAY = date(2026, 10, 25)
EMPLOYMENT_TYPES = {
    "full-time",
    "part-time",
    "contract",
    "temporary",
    "internship",
    "fellowship",
    "volunteer",
    "per-diem",
    "other",
}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
USA_KEYS = {"USAJOBS_API_KEY": "KEY123", "USAJOBS_EMAIL": "me@example.org"}


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def run(cls: type, fn: Callable[[Any], Awaitable[Any]], env: dict[str, str] | None = None) -> Any:
    """Build a SourceContext on a fresh AsyncClient inside the event loop and run ``fn(source)``."""

    async def main() -> Any:
        async with httpx.AsyncClient() as client:
            ctx = SourceContext(client=client, settings=Settings(env=dict(env or {})))
            return await fn(cls(ctx))

    return asyncio.run(main())


def search(cls: type, query: SearchQuery, env: dict[str, str] | None = None) -> list[Job]:
    return run(cls, lambda s: s.search(query), env)


def qs(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True).items()}


def check_contract(jobs: list[Job], source: str) -> None:
    """Invariants every adapter must satisfy (ARCHITECTURE §2)."""
    assert jobs, "expected jobs"
    assert len({j.uid for j in jobs}) == len(jobs), "uids must be unique"
    for j in jobs:
        assert j.source == source
        assert j.source_id and j.title and j.company and j.url.startswith("https://")
        assert "<p" not in j.description and "<br" not in j.description and "&lt;" not in j.description
        assert len(j.description) <= 12100
        assert j.posted_at is None or ISO.match(j.posted_at)
        assert j.closes_at is None or ISO.match(j.closes_at)
        assert j.employment_type is None or j.employment_type in EMPLOYMENT_TYPES
        assert j.salary_period in (None, "year", "month", "week", "day", "hour")
        assert j.employer_key is None
        json.dumps(j.to_dict())  # JSON serialisable


@pytest.fixture(autouse=True)
def _live_gate(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live"):
        expr = (request.config.getoption("markexpr", "") or "").replace("not live", "")
        if "live" not in expr and os.environ.get("JOBSWEEP_LIVE") != "1":
            pytest.skip("live test: run with -m live")


@pytest.fixture
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> date:
    for mod in (usa, nyc, nys):
        monkeypatch.setattr(mod, "_today", lambda: TODAY)
    return TODAY


def test_registry_paths() -> None:
    assert SOURCE_PATHS["usajobs"] == "jobsweep.sources.usajobs:USAJobsSource"
    assert get_source_class("usajobs") is USAJobsSource
    assert get_source_class("nycjobs") is NYCJobsSource
    assert get_source_class("nysjobs") is NYSJobsSource
    for cls in (USAJobsSource, NYCJobsSource, NYSJobsSource):
        assert cls.kind == "search" and not cls.metered and cls.availability(Settings())[0]


# ================================================================================================ usajobs
def test_usajobs_availability_and_budget() -> None:
    assert USAJobsSource.availability(Settings()) == (True, "Pipeworx fallback (free, ~50 calls/day)")
    assert USAJobsSource.query_budget(Settings()) == 12
    keyed = Settings(env=dict(USA_KEYS))
    assert USAJobsSource.availability(keyed) == (True, "direct API")
    assert USAJobsSource.query_budget(keyed) == 200
    # one credential alone is not enough for the direct API
    assert USAJobsSource.query_budget(Settings(env={"USAJOBS_API_KEY": "x"})) == 12


def test_grade_helpers() -> None:
    assert usa.normalize_grade("GS-7") == "07"
    assert usa.normalize_grade(9) == "09"
    assert usa.normalize_grade("gs13") == "13"
    assert usa.normalize_grade(None) is None and usa.normalize_grade("SES") is None
    assert usa.format_grade(["GS"], "7", "9") == "GS-07/09"
    assert usa.format_grade(["GG"], "13", "13") == "GG-13"
    assert usa.format_grade(["IA"], "02", "02") == "IA-02"
    assert usa.format_grade(["GS"], None, None) == "GS"
    assert usa.format_grade([], None, None) is None


def test_usajobs_pipeworx_request_and_mapping() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(usa.PIPEWORX_URL).respond(200, json=load("usajobs_search.json"))
        q = SearchQuery(
            "intelligence analyst",
            location="Washington, DC",
            limit=10,
            extra={"grade_low": "GS-5", "grade_high": 9, "organization": "AF", "position_title": "Analyst"},
        )
        jobs = search(USAJobsSource, q)
    req = route.calls.last.request
    assert json.loads(req.content) == {
        "keyword": "intelligence analyst",
        "location": "Washington, DC",
        "position_title": "Analyst",
        "organization": "AF",
        "pay_grade_low": "05",
        "pay_grade_high": "09",
        "results_per_page": 25,
        "page": 1,
    }
    assert "_apiKey" not in str(req.url)
    check_contract(jobs, "usajobs")
    by_id = {j.source_id: j for j in jobs}
    assert list(by_id) == ["889524900", "888515400", "883860200", "889528700"]

    intel = by_id["889524900"]
    assert intel.title == "INTELLIGENCE ANALYST (TARGETING)"
    assert intel.company == "Department of the Air Force Headquarters"
    assert intel.department == "Department of the Air Force / 36 INTELLIGENCE SQ"
    assert intel.url == "https://www.usajobs.gov/job/889524900"
    assert intel.location == "Langley AFB, Virginia"
    assert intel.series == ["0132"] and intel.grade == "GG-13"
    assert (intel.salary_min, intel.salary_max, intel.salary_currency, intel.salary_period) == (
        90925.0,
        118204.0,
        "USD",
        "year",
    )
    assert intel.posted_at == "2026-10-21" and intel.closes_at == "2026-10-27"
    assert intel.employment_type == "full-time" and intel.remote is False
    assert "internal" in intel.tags and "telework" in intel.tags  # excepted-service only
    assert intel.extra["hiring_paths"] == ["fed-excepted"] and intel.extra["via"] == "pipeworx"
    assert intel.extra["security_clearance"] == "Sensitive Compartmented Information"
    for heading in ("Summary:", "Duties:", "Qualifications:"):
        assert heading in intel.description
    assert "target intelligence" in intel.description

    cost = by_id["888515400"]
    assert cost.grade == "GS-07/09" and cost.series == ["0501"]
    assert {"recent-graduates", "pathways", "public"} <= set(cost.tags)
    assert "internal" not in cost.tags
    assert cost.location.count("; ") == 3 and "Wright-Patterson AFB, Ohio" in cost.location
    assert cost.closes_at == "2027-09-30"

    cia = by_id["883860200"]
    assert cia.employment_type == "internship" and "internship" in cia.tags
    assert cia.grade == "GS-05/07" and cia.series == ["0099"]
    assert cia.company == "Central Intelligence Agency"

    remote = by_id["889528700"]
    assert remote.remote is True and "remote job" in remote.location
    assert remote.employment_type == "temporary"  # term appointment, intermittent schedule
    assert remote.grade == "GS-07/11"


def test_usajobs_pipeworx_post_filters(pinned_today: date) -> None:
    def ids(extra: dict[str, Any], days: int | None = None) -> list[str]:
        with respx.mock() as router:
            route = router.post(usa.PIPEWORX_URL).respond(200, json=load("usajobs_search.json"))
            jobs = search(USAJobsSource, SearchQuery("analyst", posted_within_days=days, extra=extra))
        body = json.loads(route.calls.last.request.content)
        # the gateway ignores these, so they are never sent; post-filtering needs a bigger page
        assert not {"job_category_code", "hiring_path", "date_posted"} & set(body)
        if extra or days:
            assert body["results_per_page"] == 150
        return [j.source_id for j in jobs]

    assert ids({"series": ["132"]}) == ["889524900"]
    assert ids({"series": "0501;0099"}) == ["888515400", "883860200"]
    assert ids({"hiring_path": ["graduates"]}) == ["888515400"]
    assert ids({"hiring_path": ["public"], "series": ["0828", "0132"]}) == ["889528700"]
    assert ids({}, days=5) == ["889524900", "889528700"]  # published on/after 2026-10-20


def test_usajobs_pipeworx_api_key_and_errors() -> None:
    env = {"PIPEWORX_API_KEY": "abc:me@example.org"}
    with respx.mock() as router:
        route = router.post(usa.PIPEWORX_URL).respond(200, json=load("usajobs_page2.json"))
        jobs = search(USAJobsSource, SearchQuery("analyst"), env)
    assert qs(route.calls.last.request) == {"_apiKey": "abc:me@example.org"}
    assert [j.source_id for j in jobs] == ["889972400", "889210900"]
    trainee, nga = jobs
    assert trainee.employment_type == "part-time" and trainee.grade == "CO-03/09"
    assert trainee.location == "Panguitch, Utah; Tooele, Utah"
    assert nga.grade == "IA-02" and nga.series == ["1361"] and nga.department == "Department of Defense"

    # gateway tool errors come back as 200 + {"error": ...} (here: upstream 400 for pay_grade "GS5")
    err = {
        "error": "tool_error",
        "message": 'USAJOBS error: 400 {"type":"https://tools.ietf.org/html/rfc9110#section-15.5.1",'
        '"title":"Bad Request","status":400}',
        "retry_hint": "Retry the same tool with arguments matching one of the examples",
    }
    with respx.mock() as router:
        router.post(usa.PIPEWORX_URL).respond(200, json=err)
        with pytest.raises(SourceError, match="USAJOBS error: 400"):
            search(USAJobsSource, SearchQuery("analyst"))


def test_usajobs_direct_headers_and_params(pinned_today: date) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get(usa.DIRECT_URL).respond(200, json=load("usajobs_search.json"))
        q = SearchQuery(
            "budget",
            location="Washington, DC",
            posted_within_days=90,
            limit=20,
            extra={
                "series": ["0501", "0132"],
                "hiring_path": ["graduates"],
                "grade_low": "05",
                "grade_high": "GS-9",
                "organization": ["AF", "DD"],
                "position_title": "Analyst",
            },
        )
        jobs = search(USAJobsSource, q, USA_KEYS)
    req = route.calls.last.request
    assert req.headers["Host"] == "data.usajobs.gov"
    assert req.headers["User-Agent"] == "me@example.org"
    assert req.headers["Authorization-Key"] == "KEY123"
    assert qs(req) == {
        "ResultsPerPage": "100",
        "Page": "1",
        "Fields": "Full",
        "SortField": "opendate",
        "SortDirection": "Desc",
        "Keyword": "budget",
        "LocationName": "Washington, DC",
        "PositionTitle": "Analyst",
        "JobCategoryCode": "0132;0501",
        "HiringPath": "graduates",
        "PayGradeLow": "05",
        "PayGradeHigh": "09",
        "Organization": "AF;DD",
        "DatePosted": "60",  # API maximum
    }
    # server-side filters are re-checked locally: only the graduates/0501 posting qualifies
    assert [j.source_id for j in jobs] == ["888515400"]
    assert jobs[0].extra["via"] == "direct"


def test_usajobs_direct_paging_and_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usa, "MAX_PER_PAGE", 4)
    pages = {"1": load("usajobs_search.json"), "2": load("usajobs_page2.json")}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[qs(request)["Page"]])

    with respx.mock() as router:
        route = router.get(usa.DIRECT_URL).mock(side_effect=respond)
        jobs = search(USAJobsSource, SearchQuery("*", location="Remote", limit=50), USA_KEYS)
    assert [qs(c.request)["Page"] for c in route.calls] == ["1", "2"]
    first = qs(route.calls[0].request)
    assert first["RemoteIndicator"] == "True" and "Keyword" not in first and "LocationName" not in first
    check_contract(jobs, "usajobs")
    assert len(jobs) == 6


def test_usajobs_fallback_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usa, "MAX_PER_PAGE", 4)
    with respx.mock() as router:
        route = router.post(usa.PIPEWORX_URL).respond(200, json=load("usajobs_search.json"))
        jobs = search(USAJobsSource, SearchQuery("analyst", limit=100))
    assert route.call_count == usa.MAX_PAGES_FALLBACK == 2  # NumberOfPages is 3, quota caps at 2
    assert [json.loads(c.request.content)["page"] for c in route.calls] == [1, 2]
    assert len(jobs) == 4  # page 2 repeated page 1: de-duplicated


def test_usajobs_malformed_items_and_tags() -> None:
    doc = load("usajobs_search.json")
    items = doc["SearchResult"]["SearchResultItems"]
    core = copy.deepcopy(items[0])
    core["MatchedObjectId"] = "999"
    desc = core["MatchedObjectDescriptor"]
    desc["PositionTitle"] = "Emergency Management Specialist (CORE)"
    desc["UserArea"]["Details"]["JobSummary"] = "Reservists deploy to disasters. Pathways Program."
    desc["PositionRemuneration"] = [{"MinimumRange": "35", "MaximumRange": "41", "RateIntervalCode": "PH"}]
    desc["UserArea"]["Details"]["HiringPath"] = ["student", "graduates"]
    bad = [
        {"MatchedObjectId": None},
        {"MatchedObjectDescriptor": "oops"},
        {
            "MatchedObjectId": "1",
            "MatchedObjectDescriptor": {
                "PositionTitle": "Analyst",
                "PositionRemuneration": [{"MinimumRange": "n/a"}],
                "JobGrade": None,
                "PositionLocation": None,
                "UserArea": None,
            },
        },
    ]
    doc["SearchResult"]["SearchResultItems"] = [*bad, core, items[1]]
    with respx.mock() as router:
        router.post(usa.PIPEWORX_URL).respond(200, json=doc)
        jobs = search(USAJobsSource, SearchQuery("analyst"))
    by_id = {j.source_id: j for j in jobs}
    assert set(by_id) == {"1", "999", "888515400"}
    minimal = by_id["1"]
    assert minimal.url == "https://www.usajobs.gov/job/1" and minimal.salary_min is None
    assert minimal.grade is None and minimal.series == [] and minimal.remote is None
    tagged = by_id["999"]
    assert {"core", "reservist", "pathways", "student", "recent-graduates"} <= set(tagged.tags)
    assert "internal" not in tagged.tags
    assert (tagged.salary_min, tagged.salary_max, tagged.salary_period) == (35.0, 41.0, "hour")


# ================================================================================================ nycjobs
def test_nycjobs_helpers() -> None:
    assert nyc.title_case("DEPT OF HEALTH/MENTAL HYGIENE") == "Dept of Health/Mental Hygiene"
    assert nyc.company_name("ADMIN FOR CHILDREN'S SVCS") == "NYC Admin for Children's Svcs"
    assert nyc.company_name("NYC HOUSING AUTHORITY") == "NYC Housing Authority"
    assert nyc.company_name("HRA/DEPT OF SOCIAL SERVICES") == "NYC HRA/Dept of Social Services"
    assert nyc.parse_post_until("19-DEC-2026") == "2026-12-19"
    assert nyc.parse_post_until("2026-12-19T00:00:00.000") == "2026-12-19"
    assert nyc.parse_post_until("soon") is None and nyc.parse_post_until(None) is None
    clause = nyc.agency_clause("Admin for Children's Svcs")
    assert "= 'ADMIN FOR CHILDREN''S SVCS'" in clause and "like '%ADMIN%CHILDRENS%SVCS%'" in clause
    assert "like '%INVESTIGATION%'" in nyc.agency_clause("DEPT OF INVESTIGATION")


def test_nycjobs_feed_paging_merge_and_mapping(monkeypatch: pytest.MonkeyPatch, pinned_today: date) -> None:
    monkeypatch.setattr(nyc, "PAGE_SIZE", 3)
    pages = {
        "0": load("nycjobs_page1.json"),
        "3": load("nycjobs_page2.json"),
        "6": load("nycjobs_page3.json"),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[qs(request)["$offset"]])

    with respx.mock() as router:
        route = router.get(nyc.API_URL).mock(side_effect=respond)
        jobs = search(NYCJobsSource, SearchQuery("*", extra={"feed": True}, limit=5000))
    assert [qs(c.request)["$offset"] for c in route.calls] == ["0", "3", "6"]
    first = qs(route.calls[0].request)
    assert first == {"$order": "posting_date DESC, job_id, :id", "$limit": "3", "$offset": "0"}
    assert "X-App-Token" not in route.calls[0].request.headers
    check_contract(jobs, "nycjobs")
    by_id = {j.source_id: j for j in jobs}
    # 8 rows -> 6 postings; 805755 expired on 2026-10-22; 791108 has no post_until and stays
    assert list(by_id) == ["806787", "804981", "806866", "804419", "791108"]

    bi = by_id["806787"]
    assert bi.uid == "nycjobs:806787" and bi.title == "Background Investigator"
    assert bi.company == "NYC Department of Investigation"
    assert bi.extra["agency"] == "DEPARTMENT OF INVESTIGATION"
    assert bi.extra["posting_types"] == ["External", "Internal"] and "internal" not in bi.tags
    assert bi.department == "Background Investigation"
    assert bi.grade == "CONFIDENTIAL INVESTIGATOR L01" and bi.series == ["31143"]
    assert bi.url == "https://cityjobs.nyc.gov/job/806787"
    assert bi.location == "New York, NY (180 Maiden Lane)"
    assert (bi.posted_at, bi.closes_at) == ("2026-10-20", "2026-12-19")
    assert (bi.salary_min, bi.salary_max, bi.salary_currency, bi.salary_period) == (
        52426.0,
        56536.0,
        "USD",
        "year",
    )
    assert bi.employment_type == "full-time" and bi.remote is False
    assert bi.seniority == "Experienced (non-manager)"
    assert "Minimum qualifications:" in bi.description and "Background Investigation Unit" in bi.description

    analyst = by_id["804981"]  # its Internal/External rows sit on different pages
    assert analyst.extra["posting_types"] == ["External", "Internal"]
    assert "entry-level" in analyst.tags and analyst.seniority == "Entry-Level"

    intern = by_id["806866"]
    assert intern.employment_type == "internship"
    assert {"student", "internship"} <= set(intern.tags)
    assert (intern.salary_min, intern.salary_max, intern.salary_period) == (17.5, 18.0, "hour")

    assert "internal" in by_id["804419"].tags  # Internal-only posting
    assert by_id["791108"].closes_at is None


def test_nycjobs_keyword_query(pinned_today: date) -> None:
    env = {"SOCRATA_APP_TOKEN": "tok"}
    with respx.mock(assert_all_called=True) as router:
        route = router.get(nyc.API_URL).respond(200, json=load("nycjobs_page1.json"))
        q = SearchQuery(
            "investigator",
            posted_within_days=30,
            limit=1,
            extra={"agencies": ["DEPT OF INVESTIGATION"], "career_level": "Entry-Level"},
        )
        jobs = search(NYCJobsSource, q, env)
    req = route.calls.last.request
    assert req.headers["X-App-Token"] == "tok"
    params = qs(req)
    assert params["$q"] == "investigator" and params["$limit"] == "1000"
    where = params["$where"]
    assert "(upper(agency) = 'DEPT OF INVESTIGATION' OR upper(agency) like '%INVESTIGATION%')" in where
    assert "career_level in ('Entry-Level')" in where
    assert "posting_date >= '2026-09-25T00:00:00'" in where
    assert [j.source_id for j in jobs] == ["806787"]  # limit applies to merged postings
    assert NYCJobsSource.availability(Settings(env=env)) == (True, "ok (Socrata app token)")


def test_nycjobs_agencies() -> None:
    with respx.mock() as router:
        route = router.get(nyc.API_URL).respond(200, json=load("nycjobs_agencies.json"))
        names = run(NYCJobsSource, lambda s: s.agencies())
        counts = run(NYCJobsSource, lambda s: s.agency_counts())
    assert names == [
        "ADMIN FOR CHILDREN'S SVCS",
        "BRONX DISTRICT ATTORNEY",
        "DEPARTMENT OF INVESTIGATION",
        "OFFICE OF THE MAYOR",
    ]
    assert counts["DEPARTMENT OF INVESTIGATION"] == 18
    params = qs(route.calls.last.request)
    assert params["$group"] == "agency" and params["$select"] == "agency, count(*) AS n"


def test_nycjobs_malformed_rows(pinned_today: date) -> None:
    rows = [
        {"job_id": "1"},  # no title
        {"job_id": None, "business_title": "X"},
        "garbage",
        {"job_id": "2", "business_title": "Analyst", "salary_range_from": "abc", "post_until": "later"},
        {"job_id": "3", "business_title": "Old", "post_until": "01-JAN-2020"},
    ]
    with respx.mock() as router:
        router.get(nyc.API_URL).respond(200, json=rows)
        jobs = search(NYCJobsSource, SearchQuery("*"))
    assert [j.source_id for j in jobs] == ["2"]
    assert jobs[0].salary_min is None and jobs[0].company == "City of New York"
    assert jobs[0].location == "New York, NY"

    with respx.mock() as router:
        router.get(nyc.API_URL).respond(200, json={"error": True, "message": "query.soql.no-such-column"})
        with pytest.raises(SourceError, match="no-such-column"):
            search(NYCJobsSource, SearchQuery("x"))


# ================================================================================================ nysjobs
def test_nysjobs_helpers() -> None:
    assert nys.company_name("Health, Department of - Veterans Home at Oxford") == (
        "NYS Department of Health",
        "Veterans Home at Oxford",
    )
    assert nys.company_name("State Comptroller, Office of the ") == (
        "NYS Office of the State Comptroller",
        None,
    )
    assert nys.company_name("Insurance Fund, State ") == ("NYS Insurance Fund", None)
    assert nys.company_name("Senate, NYS") == ("NYS Senate", None)
    assert nys.company_name("DASNY - Dormitory Authority of the State of New York")[0].startswith("DASNY")
    assert nys.company_name("SUNY Brockport") == ("SUNY Brockport", None)


def test_nysjobs_feed_listing() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get(nys.LIST_URL).respond(200, text=text("nysjobs_table.html"))
        jobs = search(NYSJobsSource, SearchQuery("*", extra={"feed": True}, limit=5000))
    assert route.call_count == 1  # feed mode never fetches detail pages
    assert str(route.calls.last.request.url) == nys.LIST_URL
    check_contract(jobs, "nysjobs")
    by_id = {j.source_id: j for j in jobs}
    assert set(by_id) == {"227557", "227573", "227015", "226128", "227297"}
    assert [j.posted_at for j in jobs] == sorted((j.posted_at for j in jobs), reverse=True)

    inv = by_id["226128"]
    assert inv.title == "Investigator" and inv.company == "NYS Office of MTA Inspector General"
    assert inv.grade == "NS" and inv.location == "New York, NY (Manhattan)"
    assert (inv.posted_at, inv.closes_at) == ("2026-10-01", "2026-11-30")
    assert inv.url == "https://statejobs.ny.gov/public/vacancyDetailsView.cfm?id=226128"
    assert inv.extra["agency"] == "MTA Inspector General, Office of" and inv.description == ""

    auditor = by_id["227573"]
    assert auditor.grade == "SG-18" and auditor.company == "NYS Office of the State Comptroller"
    assert {"trainee", "ny-helps"} <= set(auditor.tags)
    assert "Item# 04069" in auditor.title  # entities decoded

    assert "internship" in by_id["227557"].tags
    trails = by_id["227297"]
    assert trails.grade is None and trails.extra["grade_raw"] == "Hourly"
    assert trails.location == "New York State (location to be determined)"
    assert trails.company == "NYS Office of Parks, Recreation & Historic Preservation"


def test_nysjobs_keyword_search_enriches() -> None:
    with respx.mock() as router:
        listing = router.get(nys.LIST_URL).respond(200, text=text("nysjobs_table.html"))
        for vid in ("226128", "227557"):
            router.get(nys.DETAIL_URL.format(id=vid)).respond(200, text=text(f"nysjobs_detail_{vid}.html"))
        missing = router.get(url__regex=r"vacancyDetailsView\.cfm\?id=(227573|227015|227297)$").respond(404)
        q = SearchQuery("investigator", extra={"regions": ["New York City"], "internship": True})
        jobs = search(NYSJobsSource, q)
    assert qs(listing.calls.last.request) == {
        "searchResults": "Yes",
        "Keywords": "investigator",
        "region9": "9",
        "isInternship": "Yes",
    }
    assert missing.call_count == 3  # failed detail fetches keep the listing data
    by_id = {j.source_id: j for j in jobs}
    assert len(jobs) == 5 and by_id["227015"].description == ""

    inv = by_id["226128"]
    assert inv.extra["enriched"] is True
    assert inv.description.startswith("Duties and Responsibilities")
    assert "Minimum Qualifications:" in inv.description and "How to apply:" in inv.description
    assert (inv.salary_min, inv.salary_max, inv.salary_currency, inv.salary_period) == (
        70892.0,
        88615.0,
        "USD",
        "year",
    )
    assert inv.employment_type == "full-time" and inv.remote is False
    assert "telework" in inv.tags and inv.extra["jurisdictional_class"] == "Exempt Class"
    assert inv.location == "New York, NY (Manhattan)"

    intern = by_id["227557"]
    assert (intern.salary_min, intern.salary_max, intern.salary_period) == (21.63, 22.63, "hour")
    assert intern.employment_type == "internship" and intern.location == "Albany, NY"
    assert "<br" not in intern.description and "Treasury Department" in intern.description


def test_nysjobs_enrich_once_and_filters(pinned_today: date) -> None:
    with respx.mock() as router:
        router.get(nys.LIST_URL).respond(200, text=text("nysjobs_table.html"))
        recent = search(NYSJobsSource, SearchQuery("*", posted_within_days=5))
        nysif = search(NYSJobsSource, SearchQuery("*", extra={"agencies": ["insurance fund"]}))
    assert sorted(j.source_id for j in recent) == ["227557", "227573"]
    assert sorted(j.source_id for j in nysif) == ["227015", "227557"]

    job = next(j for j in nysif if j.source_id == "227557")
    with respx.mock() as router:
        detail = router.get(nys.DETAIL_URL.format(id="227557")).respond(
            200, text=text("nysjobs_detail_227557.html")
        )

        async def twice(src: NYSJobsSource) -> Job:
            await src.enrich(job)
            return await src.enrich(job)

        out = run(NYSJobsSource, twice)
    assert detail.call_count == 1 and out.extra["appointment_type"] == "Temporary"


def test_nysjobs_blocked_page() -> None:
    with respx.mock() as router:
        router.get(nys.LIST_URL).respond(200, text="<html><body>Access denied</body></html>")
        with pytest.raises(SourceError, match="vacancy table not found"):
            search(NYSJobsSource, SearchQuery("*"))
    with pytest.raises(SourceError):
        nys.apply_detail(Job("nysjobs", "1", "T", "C", "https://x"), "<html></html>")


# ================================================================================================ live
@pytest.mark.live
def test_live_usajobs_pipeworx() -> None:
    jobs = search(USAJobsSource, SearchQuery("intelligence analyst", limit=5))
    check_contract(jobs, "usajobs")
    assert all(
        j.extra["via"] == "pipeworx" and j.url.startswith("https://www.usajobs.gov/job/") for j in jobs
    )
    assert any(j.series and j.grade for j in jobs)


@pytest.mark.live
def test_live_nycjobs_feed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nyc, "PAGE_SIZE", 200)  # one page is plenty for 20 postings
    jobs = search(NYCJobsSource, SearchQuery("*", extra={"feed": True}, limit=20))
    check_contract(jobs, "nycjobs")
    assert len(jobs) == 20 and all(j.series and j.company.startswith("NYC ") for j in jobs)

    async def resolve(src: NYCJobsSource) -> httpx.Response:
        return await src.ctx.client.get(jobs[0].url, follow_redirects=True)

    resp = run(NYCJobsSource, resolve)
    assert resp.status_code == 200 and "-jid-" in str(resp.url)  # cityjobs.nyc.gov canonical page


@pytest.mark.live
def test_live_nysjobs_listing() -> None:
    async def go(src: NYSJobsSource) -> list[Job]:
        jobs = await src.search(SearchQuery("*", extra={"feed": True}, limit=5000))
        await src.enrich(jobs[0])
        return jobs

    jobs = run(NYSJobsSource, go)
    check_contract(jobs, "nysjobs")
    assert len(jobs) > 100
    assert jobs[0].extra.get("enriched") and jobs[0].description
