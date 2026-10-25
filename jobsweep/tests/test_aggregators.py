"""Keyword aggregators: adzuna, theirstack, hasdata_indeed, jobspy, apify.

Fixtures (tests/fixtures/aggregators/):
* ``jobspy_indeed.json`` — REAL ``jobspy.scrape_jobs`` rows captured live 2026-10-25 (Indeed, "geopolitical
  risk analyst", New York), 3 of 5 rows; ``apify_jobspy_items.json`` reuses two of them as JobSpy-actor items.
* ``apify_hiringcafe_items.json`` — first item is the verbatim example record from the
  blackfalcondata~hiringcafe-scraper README (public build API); the others follow its schema.
* ``adzuna_us_search.json``, ``theirstack_search.json``, ``hasdata_indeed_listing.json``,
  ``hasdata_indeed_job.json`` — built from the providers' documented response schemas (Adzuna Swagger
  model, TheirStack OpenAPI, HasData Indeed API page) because no API keys are available here.

Live smoke tests are marked ``live`` and only run with ``-m live`` (or ``JOBSWEEP_LIVE=1``). Without keys
they verify the documented endpoints exist (a bogus key must be rejected with 401).
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobsweep.config import Settings
from jobsweep.http import HTTPError
from jobsweep.models import Job, SearchQuery
from jobsweep.sources import SOURCE_PATHS, get_source_class
from jobsweep.sources.adzuna import AdzunaSource, country_for, where_for
from jobsweep.sources.apify import ApifySource, actor_id, build_input
from jobsweep.sources.base import SourceContext, SourceError, SourceUnavailable
from jobsweep.sources.hasdata_indeed import HasDataIndeedSource, canonical_url, indeed_key
from jobsweep.sources.jobspy_source import JobSpySource, scrape_kwargs
from jobsweep.sources.theirstack import TheirStackSource, build_body

FIX = Path(__file__).parent / "fixtures" / "aggregators"
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
KEYS = {
    "ADZUNA_APP_ID": "test-id",
    "ADZUNA_APP_KEY": "test-key",
    "THEIRSTACK_API_KEY": "ts-key",
    "HASDATA_API_KEY": "hd-key",
    "APIFY_TOKEN": "apify-token",
}


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def run(cls: type, fn: Callable[[Any], Awaitable[Any]], env: dict[str, str] | None = None) -> Any:
    """Build a SourceContext on a fresh AsyncClient inside the event loop and run ``fn(source)``."""

    async def main() -> Any:
        async with httpx.AsyncClient() as client:
            ctx = SourceContext(client=client, settings=Settings(env=dict(KEYS if env is None else env)))
            return await fn(cls(ctx))

    return asyncio.run(main())


def check_contract(jobs: list[Job], source: str) -> None:
    """Invariants every adapter must satisfy (ARCHITECTURE §2)."""
    assert jobs, "expected jobs"
    assert len({j.uid for j in jobs}) == len(jobs), "uids must be unique"
    for j in jobs:
        assert j.source == source
        assert j.source_id and j.title
        assert j.url.startswith("http")
        assert j.employer_key is None
        assert "<p" not in j.description and "&lt;" not in j.description and "**" not in j.description
        assert len(j.description) <= 12100
        assert j.employment_type is None or j.employment_type in EMPLOYMENT_TYPES
        assert j.remote in (True, False, None)
        assert j.posted_at is None or ISO.match(j.posted_at)
        assert j.closes_at is None or ISO.match(j.closes_at)
        assert j.salary_period in (None, "year", "month", "week", "day", "hour")
        if j.salary_min is not None or j.salary_max is not None:
            assert j.salary_currency and len(j.salary_currency) == 3
        json.dumps(j.to_dict())  # serialisable


@pytest.fixture(autouse=True)
def _live_gate(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live"):
        expr = (request.config.getoption("markexpr", "") or "").replace("not live", "")
        if "live" not in expr and os.environ.get("JOBSWEEP_LIVE") != "1":
            pytest.skip("live test: run with -m live")


# ----------------------------------------------------------------------------------------------- registry
@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("adzuna", AdzunaSource),
        ("theirstack", TheirStackSource),
        ("hasdata_indeed", HasDataIndeedSource),
        ("jobspy", JobSpySource),
        ("apify", ApifySource),
    ],
)
def test_registry_paths_match_classes(name: str, cls: type) -> None:
    assert SOURCE_PATHS[name].endswith(":" + cls.__name__)
    assert get_source_class(name) is cls
    assert cls.name == name and cls.kind == "search" and cls.label and cls.docs_url.startswith("https://")


def test_class_metadata() -> None:
    assert AdzunaSource.requires_env == ("ADZUNA_APP_ID", "ADZUNA_APP_KEY") and AdzunaSource.max_queries == 40
    assert not AdzunaSource.metered
    assert TheirStackSource.metered and TheirStackSource.max_queries == 10
    assert HasDataIndeedSource.metered and HasDataIndeedSource.max_queries == 10
    assert JobSpySource.requires_module == "jobspy" and JobSpySource.max_queries == 15
    assert not JobSpySource.metered
    assert ApifySource.metered and ApifySource.requires_env == ("APIFY_TOKEN",) and ApifySource.max_queries == 5


def test_availability() -> None:
    empty = Settings(env={})
    for cls in (AdzunaSource, TheirStackSource, HasDataIndeedSource, ApifySource):
        ok, reason = cls.availability(empty)
        assert not ok and "missing env" in reason
        assert cls.availability(Settings(env=KEYS)) == (True, "ok")
    assert JobSpySource.availability(empty) == (True, "ok")  # python-jobspy is installed in the dev env


@pytest.mark.parametrize("cls", [AdzunaSource, TheirStackSource, HasDataIndeedSource, ApifySource])
def test_missing_credentials_raise_unavailable(cls: type) -> None:
    with pytest.raises(SourceUnavailable):
        run(cls, lambda s: s.search(SearchQuery(keywords="analyst")), env={})


# ----------------------------------------------------------------------------------------------- adzuna
ADZUNA_US = re.compile(r"https://api\.adzuna\.com/v1/api/jobs/us/search/\d+")


@respx.mock
def test_adzuna_search_maps_results_and_params() -> None:
    route = respx.get(ADZUNA_US).respond(json=load("adzuna_us_search.json"))
    q = SearchQuery(keywords="political risk analyst", location="New York, NY", posted_within_days=14, limit=20)
    jobs = run(AdzunaSource, lambda s: s.search(q))
    check_contract(jobs, "adzuna")
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.url.path == "/v1/api/jobs/us/search/1"
    p = dict(req.url.params)
    assert p["app_id"] == "test-id" and p["app_key"] == "test-key"
    assert p["what"] == "political risk analyst" and p["where"] == "New York, NY"
    assert p["results_per_page"] == "20" and p["max_days_old"] == "14"
    assert p["content-type"] == "application/json" and p["sort_by"] == "date"

    by_id = {j.source_id: j for j in jobs}
    pr = by_id["4921837765"]
    assert pr.title == "Political Risk Analyst"  # <strong> highlight tags stripped
    assert pr.company == "Marsh McLennan" and pr.location == "New York City, New York"
    assert pr.url.startswith("https://www.adzuna.com/land/ad/4921837765")
    assert (pr.salary_min, pr.salary_max, pr.salary_currency, pr.salary_period) == (70000, 85000, "USD", "year")
    assert pr.employment_type == "full-time" and pr.department == "Consultancy Jobs"
    assert pr.posted_at.startswith("2026-10-21") and "&amp;" not in pr.description and "<strong>" not in pr.description
    assert pr.remote is None

    sanc = by_id["4930011122"]
    assert sanc.salary_min is None and sanc.extra["salary_estimate"] == [61234.56, 61234.56]  # predicted
    assert sanc.employment_type == "contract" and sanc.remote is True

    gsoc = by_id["4925550198"]
    assert gsoc.employment_type == "part-time" and "graduate-program" in gsoc.tags
    assert gsoc.salary_min is None and gsoc.salary_currency is None


def test_adzuna_country_and_where() -> None:
    assert country_for(SearchQuery(keywords="x")) == "us"
    assert country_for(SearchQuery(keywords="x", location="London, UK")) == "gb"
    assert country_for(SearchQuery(keywords="x", extra={"country": "UK"})) == "gb"
    assert country_for(SearchQuery(keywords="x", location="London", extra={"country": "us"})) == "us"
    with pytest.raises(SourceError):
        country_for(SearchQuery(keywords="x", extra={"country": "zz"}))
    assert where_for("Remote", "us") is None and where_for(None, "us") is None
    assert where_for("London, United Kingdom", "gb") == "London"
    assert where_for("Washington, DC", "us") == "Washington, DC"


@respx.mock
def test_adzuna_gb_remote_and_pagination() -> None:
    page = load("adzuna_us_search.json")
    full = copy.deepcopy(page)
    full["count"] = 120
    full["results"] = [dict(page["results"][0], id=str(1000 + i)) for i in range(50)]
    short = copy.deepcopy(page)
    short["count"] = 120
    short["results"] = [dict(page["results"][2], id=str(2000 + i)) for i in range(5)]
    r1 = respx.get("https://api.adzuna.com/v1/api/jobs/gb/search/1").respond(json=full)
    r2 = respx.get("https://api.adzuna.com/v1/api/jobs/gb/search/2").respond(json=short)
    q = SearchQuery(keywords="sanctions analyst", location="London", limit=80)
    jobs = run(AdzunaSource, lambda s: s.search(q))
    assert r1.called and r2.called and len(jobs) == 55
    p = dict(r1.calls[0].request.url.params)
    assert p["results_per_page"] == "50" and p["where"] == "London" and "max_days_old" not in p
    assert all(j.salary_currency in (None, "GBP") for j in jobs)
    assert jobs[0].extra["country"] == "gb"

    respx.get("https://api.adzuna.com/v1/api/jobs/us/search/1").respond(json=page)
    run(AdzunaSource, lambda s: s.search(SearchQuery(keywords="osint", location="Remote")))
    assert "where" not in dict(respx.calls.last.request.url.params)


@respx.mock
def test_adzuna_malformed_results_are_skipped() -> None:
    data = load("adzuna_us_search.json")
    data["results"] = [None, "junk", {"id": 1}, {"title": None}, *data["results"][:1], {"title": 5, "company": "x"}]
    respx.get(ADZUNA_US).respond(json=data)
    jobs = run(AdzunaSource, lambda s: s.search(SearchQuery(keywords="x")))
    assert [j.source_id for j in jobs][0] == "4921837765"
    respx.get(ADZUNA_US).respond(json={"exception": "nope"})
    assert run(AdzunaSource, lambda s: s.search(SearchQuery(keywords="x"))) == []


@respx.mock
def test_adzuna_auth_failure_raises() -> None:
    respx.get(ADZUNA_US).respond(
        401, json={"display": "Authorisation failed", "exception": "AUTH_FAIL", "doc": "https://api.adzuna.com/v1/doc"}
    )
    with pytest.raises(HTTPError) as err:
        run(AdzunaSource, lambda s: s.search(SearchQuery(keywords="x")))
    assert err.value.status == 401


# ----------------------------------------------------------------------------------------------- theirstack
TS_API = "https://api.theirstack.com/v1/jobs/search"


@respx.mock
def test_theirstack_search_body_and_mapping() -> None:
    route = respx.post(TS_API).respond(json=load("theirstack_search.json"))
    q = SearchQuery(keywords="threat intelligence analyst", location="New York, NY", posted_within_days=14, limit=50)
    jobs = run(TheirStackSource, lambda s: s.search(q))
    check_contract(jobs, "theirstack")
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer ts-key"
    body = json.loads(req.content)
    assert body["job_title_or"] == ["threat intelligence analyst"]
    assert body["job_country_code_or"] == ["US"] and body["posted_at_max_age_days"] == 14
    assert body["limit"] == 25 and body["page"] == 0  # credits: capped at 25 per query by default
    assert "workplace_types_or" not in body

    dm, bz, ant = jobs
    assert dm.source_id == "186427765" and dm.company == "Dataminr"
    assert dm.url == "https://job-boards.greenhouse.io/dataminr/jobs/6123456"  # final_url preferred
    assert dm.location == "New York, NY" and dm.remote is False  # hybrid
    assert (dm.salary_min, dm.salary_max, dm.salary_currency, dm.salary_period) == (75000, 95000, "USD", "year")
    assert dm.employment_type == "full-time" and dm.seniority == "junior" and dm.posted_at == "2026-10-22"
    assert dm.description.startswith("About the role") and "- Monitor open sources" in dm.description

    assert bz.url == "https://careers.beazley.com/jobs/4821" and bz.salary_currency == "GBP"
    assert bz.location == "London" and bz.remote is False

    assert ant.remote is True and ant.employment_type == "internship" and "internship" in ant.tags
    assert (ant.salary_min, ant.salary_period) == (45, "hour")  # published hourly figure, not annualised
    assert ant.location.count(";") == 1 and "<p>" not in ant.description


def test_theirstack_build_body_variants() -> None:
    uk = build_body(SearchQuery(keywords="political risk", location="London"))
    assert uk["job_country_code_or"] == ["GB"] and uk["posted_at_max_age_days"] == 30
    remote = build_body(SearchQuery(keywords="osint analyst", location="Remote"))
    assert remote["workplace_types_or"] == ["remote"]
    custom = build_body(
        SearchQuery(
            keywords="ignored",
            extra={
                "title_patterns": ["sanctions", "export control"],
                "description_patterns": "(?i)OFAC",
                "country_codes": "GB",
                "seniority": "junior",
            },
        )
    )
    assert custom["job_title_or"] == ["sanctions", "export control"]
    assert custom["job_description_pattern_or"] == ["(?i)OFAC"]
    assert custom["job_country_code_or"] == ["GB"] and custom["job_seniority_or"] == ["junior"]
    feed = build_body(SearchQuery(keywords="*"))
    assert "job_title_or" not in feed


@respx.mock
def test_theirstack_is_never_retried_and_pages() -> None:
    route = respx.post(TS_API).respond(500, json={"error": {"title": "boom"}})
    with pytest.raises(HTTPError):
        run(TheirStackSource, lambda s: s.search(SearchQuery(keywords="x")))
    assert route.call_count == 1  # paid call: no blind retries

    data = load("theirstack_search.json")
    first = dict(data, data=[dict(data["data"][0], id=i) for i in range(10)])
    second = dict(data, data=[dict(data["data"][1], id=100 + i) for i in range(3)])
    respx.post(TS_API).mock(side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)])
    jobs = run(TheirStackSource, lambda s: s.search(SearchQuery(keywords="x", extra={"limit": 10})))
    assert len(jobs) == 10  # stops once the requested credits are used


@respx.mock
def test_theirstack_malformed_rows() -> None:
    data = load("theirstack_search.json")
    data["data"] = [None, {"id": 1}, {"job_title": "Bad", "company_object": "?", "locations": "x"}, data["data"][0]]
    respx.post(TS_API).respond(json=data)
    jobs = run(TheirStackSource, lambda s: s.search(SearchQuery(keywords="x")))
    assert [j.title for j in jobs] == ["Bad", "Threat Intelligence Analyst"]


# ----------------------------------------------------------------------------------------------- hasdata
HD_LIST = "https://api.hasdata.com/scrape/indeed/listing"
HD_JOB = "https://api.hasdata.com/scrape/indeed/job"


@respx.mock
def test_hasdata_listing_maps_jobs() -> None:
    route = respx.get(HD_LIST).respond(json=load("hasdata_indeed_listing.json"))
    q = SearchQuery(keywords="geopolitical analyst", location="New York, NY", posted_within_days=5)
    jobs = run(HasDataIndeedSource, lambda s: s.search(q))
    check_contract(jobs, "hasdata_indeed")
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "hd-key"
    p = dict(req.url.params)
    assert p == {"keyword": "geopolitical analyst", "location": "New York, NY", "sort": "date", "datePosted": "7"}
    assert route.call_count == 1  # one paid call per query by default

    eg, cr, doi = jobs
    assert eg.source_id == "9a1c3e5f7b2d4f60" and eg.url == "https://www.indeed.com/viewjob?jk=9a1c3e5f7b2d4f60"
    assert (eg.salary_min, eg.salary_max, eg.salary_currency, eg.salary_period) == (70000, 80000, "USD", "year")
    assert eg.employment_type == "full-time" and eg.remote is False  # "Hybrid work"
    assert eg.posted_at.startswith("2026-10-22") and eg.extra["snippet"] is True
    assert cr.remote is True and cr.employment_type == "internship" and "internship" in cr.tags
    assert cr.extra["sponsored"] is True
    assert doi.salary_period == "hour" and doi.salary_min == 28.5 and doi.employment_type is None


def test_hasdata_params() -> None:
    def params(**kw: Any) -> dict:
        return HasDataIndeedSource(SourceContext(client=None, settings=Settings(env=KEYS))).params_for(  # type: ignore[arg-type]
            SearchQuery(**kw)
        )

    assert params(keywords="osint", location="Remote")["location"] == "United States"
    assert params(keywords="osint", location="Remote")["remoteType"] == "remote"
    assert params(keywords="osint", location="London")["domain"] == "uk.indeed.com"
    assert "datePosted" not in params(keywords="x", posted_within_days=30)
    assert params(keywords="x", posted_within_days=1)["datePosted"] == 1
    assert params(keywords="x", posted_within_days=14)["datePosted"] == 14
    assert params(keywords="x", extra={"domain": "ca.indeed.com", "sort": "relevance"})["sort"] == "relevance"


def test_indeed_url_helpers() -> None:
    url = "https://uk.indeed.com/m/viewjob?jk=abc123&from=serp&tk=1"
    assert indeed_key(url) == "abc123" and canonical_url(url) == "https://uk.indeed.com/viewjob?jk=abc123"
    assert indeed_key("https://example.com/job/1") is None
    assert canonical_url("https://example.com/job/1") == "https://example.com/job/1"


@respx.mock
def test_hasdata_enrich_uses_job_endpoint() -> None:
    respx.get(HD_LIST).respond(json=load("hasdata_indeed_listing.json"))
    detail = respx.get(HD_JOB).respond(json=load("hasdata_indeed_job.json"))

    async def go(src: HasDataIndeedSource) -> list[Job]:
        jobs = await src.search(SearchQuery(keywords="geopolitical analyst", location="New York, NY"))
        await src.enrich(jobs[0])
        await src.enrich(jobs[0])  # second call is a no-op
        await src.enrich(jobs[2])
        return jobs

    jobs = run(HasDataIndeedSource, go)
    assert detail.call_count == 2
    assert dict(detail.calls[0].request.url.params) == {"url": "https://www.indeed.com/viewjob?jk=9a1c3e5f7b2d4f60"}
    eg = jobs[0]
    assert eg.extra["enriched"] is True and "snippet" not in eg.extra
    assert "political risk in emerging markets" in eg.description and "- Track elections" in eg.description
    assert "<p>" not in eg.description
    assert jobs[2].employment_type == "full-time"  # filled from details.jobType


@respx.mock
def test_hasdata_alternative_shapes_and_missing_item() -> None:
    data = {
        "jobs": [
            None,
            {"title": ""},
            {
                "title": "Due Diligence Analyst",
                "company": "Kroll",
                "location": "Washington, DC",
                "salary": "$60,000 - $70,000 a year",
                "postedAt": "2026-10-01",
                "link": "https://www.indeed.com/rc/clk?jk=ff00ee11&from=vj",
                "jobKey": "ff00ee11",
                "details": "Contract",
            },
        ]
    }
    respx.get(HD_LIST).respond(json=data)
    respx.get(HD_JOB).respond(json={"requestMetadata": {"status": "ok"}, "error": "Job not found"})

    async def go(src: HasDataIndeedSource) -> list[Job]:
        jobs = await src.search(SearchQuery(keywords="due diligence"))
        await src.enrich(jobs[0])
        return jobs

    jobs = run(HasDataIndeedSource, go)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source_id == "ff00ee11" and j.url == "https://www.indeed.com/viewjob?jk=ff00ee11"
    assert (j.salary_min, j.salary_max, j.salary_period) == (60000, 70000, "year")
    assert j.employment_type == "contract" and j.posted_at == "2026-10-01"
    assert j.extra["enriched"] is False  # "not found" answer leaves the job untouched


# ----------------------------------------------------------------------------------------------- jobspy
def _frame(rows: list[dict[str, Any]]) -> Any:
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame(rows)
    frame["date_posted"] = [date.fromisoformat(d) if d else None for d in frame["date_posted"]]
    return frame


def test_jobspy_scrape_kwargs() -> None:
    kw = scrape_kwargs(SearchQuery(keywords="osint analyst", location="New York, NY", posted_within_days=7, limit=100))
    assert kw["site_name"] == ["indeed", "linkedin"] and kw["search_term"] == "osint analyst"
    assert kw["results_wanted"] == 30 and kw["hours_old"] == 168 and kw["country_indeed"] == "USA"
    assert kw["description_format"] == "markdown" and kw["verbose"] == 0 and "is_remote" not in kw
    remote = scrape_kwargs(SearchQuery(keywords="x", location="Remote", limit=5, extra={"sites": "indeed"}))
    assert remote["location"] == "United States" and remote["is_remote"] is True and remote["site_name"] == ["indeed"]
    assert remote["results_wanted"] == 5
    uk = scrape_kwargs(SearchQuery(keywords="x", location="London", extra={"distance": 10, "bogus": 1}))
    assert uk["country_indeed"] == "UK" and uk["distance"] == 10 and "bogus" not in uk
    assert scrape_kwargs(SearchQuery(keywords="x"))["location"] == "United States"


def test_jobspy_search_maps_real_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    jobspy = pytest.importorskip("jobspy")
    rows = load("jobspy_indeed.json")["rows"]
    linkedin = dict(
        rows[0],
        id="li-4312987001",
        site="linkedin",
        job_url="https://www.linkedin.com/jobs/view/4312987001",
        job_type="internship",
        job_level="internship",
        is_remote=True,
        min_amount=float("nan"),
        max_amount=float("nan"),
        interval=None,
        currency=None,
        description="Intern on the **Protective Intelligence** team \\- apply [here](https://example.com).",
        job_function="Research",
    )
    calls: list[dict[str, Any]] = []

    def fake_scrape_jobs(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _frame([*rows, linkedin])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)
    q = SearchQuery(keywords="geopolitical risk analyst", location="New York, NY", posted_within_days=30, limit=10)
    jobs = run(JobSpySource, lambda s: s.search(q))
    check_contract(jobs, "jobspy")
    assert calls and calls[0]["results_wanted"] == 10 and calls[0]["hours_old"] == 720

    con_ed = jobs[0]
    assert con_ed.source_id == "indeed-in-532540930a0e547e" and con_ed.title == "Protective Intelligence Sr Analyst"
    assert con_ed.url == "https://www.indeed.com/viewjob?jk=532540930a0e547e"
    assert con_ed.company == "Con Edison" and con_ed.location == "New York, NY, USA"
    assert con_ed.posted_at == "2026-10-23" and con_ed.employment_type == "full-time"
    assert (con_ed.salary_min, con_ed.salary_max, con_ed.salary_currency, con_ed.salary_period) == (
        105000,
        135000,
        "USD",
        "year",
    )
    assert con_ed.remote is None and "indeed" in con_ed.tags
    assert con_ed.description.startswith("Mission Statement:") and "**" not in con_ed.description
    assert con_ed.extra["job_url_direct"].startswith("https://ejcu.fa.us6.oraclecloud.com")

    li = jobs[-1]
    assert li.source_id == "linkedin-li-4312987001" and li.tags[0] == "linkedin" and "internship" in li.tags
    assert li.remote is True and li.seniority == "internship" and li.department == "Research"
    assert li.salary_min is None and li.salary_currency is None and li.salary_period is None
    assert li.description == "Intern on the Protective Intelligence team - apply here."


def test_jobspy_errors_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    jobspy = pytest.importorskip("jobspy")

    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("429 from linkedin")

    monkeypatch.setattr(jobspy, "scrape_jobs", boom)
    with pytest.raises(SourceError, match="429"):
        run(JobSpySource, lambda s: s.search(SearchQuery(keywords="x")))

    monkeypatch.setattr(jobspy, "scrape_jobs", lambda **kw: None)
    assert run(JobSpySource, lambda s: s.search(SearchQuery(keywords="x"))) == []
    bad_rows = [{"title": None}, {"title": "No URL row", "site": "google", "id": None, "date_posted": math.nan}]
    monkeypatch.setattr(jobspy, "scrape_jobs", lambda **kw: bad_rows)
    jobs = run(JobSpySource, lambda s: s.search(SearchQuery(keywords="x")))
    assert len(jobs) == 1 and jobs[0].source_id.startswith("google-") and jobs[0].posted_at is None


# ----------------------------------------------------------------------------------------------- apify
APIFY_HC = "https://api.apify.com/v2/acts/blackfalcondata~hiringcafe-scraper/run-sync-get-dataset-items"
APIFY_JS = "https://api.apify.com/v2/acts/aakash19~jobspy/run-sync-get-dataset-items"


@respx.mock
def test_apify_hiringcafe_run() -> None:
    route = respx.post(APIFY_HC).respond(json=load("apify_hiringcafe_items.json"))
    q = SearchQuery(keywords="political risk analyst", location="New York, NY", posted_within_days=14, limit=20)
    jobs = run(ApifySource, lambda s: s.search(q))
    check_contract(jobs, "apify")
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer apify-token" and "token" not in str(req.url)
    p = dict(req.url.params)
    assert p["maxItems"] == "20" and p["timeout"] == "240" and p["format"] == "json"
    body = json.loads(req.content)
    assert body["query"] == "political risk analyst" and body["country"] == "US" and body["location"] == "New York"
    assert body["maxResults"] == 20 and body["postedWithinDays"] == 14 and body["includeDetails"] is True
    assert "workplaceTypes" not in body

    assert len(jobs) == 3  # the title-less item is dropped
    sf, chubb, discord = jobs
    assert sf.source_id.startswith("hiring-cafe:2c348e57") and sf.company == "State Farm"
    assert sf.url.startswith("https://jobs.statefarm.com/jobs/43740") and sf.extra["portal_url"].startswith("https://hiring.cafe/")
    assert sf.remote is False and sf.employment_type == "full-time" and sf.seniority == "Mid Level"
    assert sf.posted_at.startswith("2026-03-16") and "<p" not in sf.description and sf.extra["ats"] == "icims"
    assert (chubb.salary_min, chubb.salary_max, chubb.salary_currency, chubb.salary_period) == (75000, 90000, "USD", "year")
    assert chubb.department == "Insurance Underwriting" and chubb.extra["min_years_experience"] == 1
    assert "contract frustration" in chubb.description and "hiring-cafe" in chubb.tags
    assert discord.remote is True and discord.employment_type == "contract" and discord.description == ""


@respx.mock
def test_apify_jobspy_actor_and_overrides() -> None:
    route = respx.post(APIFY_JS).respond(json=load("apify_jobspy_items.json"))
    q = SearchQuery(
        keywords="protective intelligence",
        location="Remote",
        posted_within_days=90,
        limit=10,
        extra={"actor": "jobspy", "actor_input": {"job_boards": ["indeed", "google"]}, "max_charge_usd": 0.5},
    )
    jobs = run(ApifySource, lambda s: s.search(q))
    check_contract(jobs, "apify")
    body = json.loads(route.calls[0].request.content)
    assert body["search_terms"] == ["protective intelligence"] and body["locations"] == ["United States"]
    assert body["job_boards"] == ["indeed", "google"] and body["days_posted"] == 60 and body["remote_only"] is True
    assert dict(route.calls[0].request.url.params)["maxTotalChargeUsd"] == "0.5"
    j = jobs[0]
    assert j.source_id == "jobspy:in-532540930a0e547e" and j.tags == ["jobspy", "indeed"]
    assert j.salary_min == 105000 and j.salary_period == "year" and j.posted_at == "2026-10-23"


def test_apify_actor_ids_and_inputs() -> None:
    assert actor_id(None) == "blackfalcondata~hiringcafe-scraper"
    assert actor_id("hiring.cafe") == "blackfalcondata~hiringcafe-scraper"
    assert actor_id("someone/job-scraper") == "someone~job-scraper"
    london = build_input("blackfalcondata~hiringcafe-scraper", SearchQuery(keywords="kyc", location="London, UK"), 5)
    assert london["country"] == "GB" and london["location"] == "London" and london["postedWithinDays"] == 0
    remote = build_input("blackfalcondata~hiringcafe-scraper", SearchQuery(keywords="osint", remote=True), 5)
    assert remote["workplaceTypes"] == ["Remote"] and "location" not in remote
    other = build_input("someone~job-scraper", SearchQuery(keywords="osint", location="DC"), 7)
    assert other == {"query": "osint", "maxResults": 7, "location": "DC"}


def test_apify_errors() -> None:
    q = SearchQuery(keywords="x")
    with respx.mock() as mock:
        mock.post(APIFY_HC).respond(408, json={"error": {"type": "run-timeout-exceeded"}})
        with pytest.raises(SourceError, match="300 s"):
            run(ApifySource, lambda s: s.search(q))
    with respx.mock() as mock:
        route = mock.post(APIFY_HC).respond(500, json={"error": {"type": "internal-server-error"}})
        with pytest.raises(HTTPError) as err:
            run(ApifySource, lambda s: s.search(q))
        assert err.value.status == 500 and "apify-token" not in str(err.value)
        assert route.call_count == 1  # paid run: never retried
    with respx.mock() as mock:
        mock.post(APIFY_HC).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(SourceError):
            run(ApifySource, lambda s: s.search(q))
    with respx.mock() as mock:
        mock.post(APIFY_HC).respond(json={"items": [{"title": "Wrapped", "url": "https://x.example/1", "id": 9}]})
        jobs = run(ApifySource, lambda s: s.search(q))
        assert [j.source_id for j in jobs] == ["hiring-cafe:9"]


# ----------------------------------------------------------------------------------------------- live smoke
def live(cls: type, query: SearchQuery, env: dict[str, str] | None = None) -> list[Job]:
    from jobsweep.http import make_client

    async def main() -> list[Job]:
        settings = Settings.load()
        if env is not None:
            settings.env = {**settings.env, **env}
        async with make_client(settings.user_agent) as client:
            return await cls(SourceContext(client=client, settings=settings)).search(query)

    return asyncio.run(main())


@pytest.mark.live
def test_live_jobspy_indeed() -> None:
    q = SearchQuery(
        keywords="intelligence analyst", location="New York, NY", limit=5, extra={"sites": ["indeed"]}
    )
    jobs = live(JobSpySource, q)
    check_contract(jobs, "jobspy")
    assert len(jobs) <= 5 and all(j.source_id.startswith("indeed-") for j in jobs)


BAD_KEYS = {
    AdzunaSource: {"ADZUNA_APP_ID": "invalid", "ADZUNA_APP_KEY": "invalid"},
    TheirStackSource: {"THEIRSTACK_API_KEY": "invalid"},
    HasDataIndeedSource: {"HASDATA_API_KEY": "invalid"},
    ApifySource: {"APIFY_TOKEN": "invalid"},
}


@pytest.mark.live
@pytest.mark.parametrize("cls", list(BAD_KEYS))
def test_live_keyed_endpoint(cls: type) -> None:
    """Real search when the key is configured, otherwise prove the documented endpoint rejects a bad key."""
    q = SearchQuery(keywords="political risk analyst", location="New York, NY", posted_within_days=30, limit=5)
    if cls.availability(Settings.load())[0]:
        jobs = live(cls, q)
        if jobs:
            check_contract(jobs, cls.name)
        return
    with pytest.raises(HTTPError) as err:
        live(cls, q, env=BAD_KEYS[cls])
    assert err.value.status in (401, 403)
