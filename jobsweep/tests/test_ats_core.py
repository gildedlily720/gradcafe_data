"""Core ATS board adapters: greenhouse, lever, ashby, workday, smartrecruiters, workable, recruitee.

Offline tests replay real API responses captured on 2026-10-25 (trimmed to a few postings) from
tests/fixtures/ats/. Workable fixtures are real captures of account ``wati-dot-i-o`` from 2026-10-23
(public repo jobverify/jobverify-backend) because apply.workable.com rate limited this egress.

Live smoke tests are marked ``live`` and only run with ``-m live`` (or ``JOBSWEEP_LIVE=1``).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobsweep.config import Settings
from jobsweep.http import HTTPError
from jobsweep.models import Employer, Job
from jobsweep.sources import SOURCE_PATHS, get_source_class
from jobsweep.sources.ashby import AshbySource, compensation
from jobsweep.sources.base import SourceContext, SourceError
from jobsweep.sources.greenhouse import GreenhouseSource
from jobsweep.sources.lever import LeverSource
from jobsweep.sources.recruitee import RecruiteeSource
from jobsweep.sources.smartrecruiters import SmartRecruitersSource
from jobsweep.sources.workable import WorkableSource
from jobsweep.sources.workday import WorkdaySource, parse_board

FIX = Path(__file__).parent / "fixtures" / "ats"
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


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def employer(ats: str, board: str, **kw: Any) -> Employer:
    return Employer(key="test-employer", name="Test Employer", ats=ats, board=board, **kw)


def run(cls: type, fn: Callable[[Any], Awaitable[Any]]) -> Any:
    """Build a SourceContext on a fresh AsyncClient inside the event loop and run ``fn(source)``."""

    async def main() -> Any:
        async with httpx.AsyncClient() as client:
            ctx = SourceContext(client=client, settings=Settings())
            return await fn(cls(ctx))

    return asyncio.run(main())


def check_contract(jobs: list[Job], source: str) -> None:
    """Invariants every adapter must satisfy (ARCHITECTURE §2)."""
    assert jobs, "expected jobs"
    assert len({j.uid for j in jobs}) == len(jobs), "uids must be unique"
    for j in jobs:
        assert j.source == source
        assert j.source_id and j.title and j.url.startswith("http")
        assert j.company == "Test Employer" and j.employer_key == "test-employer"
        assert "<p" not in j.description and "&lt;" not in j.description
        assert len(j.description) <= 12010
        assert j.employment_type is None or j.employment_type in EMPLOYMENT_TYPES
        assert j.remote in (True, False, None)
        assert j.posted_at is None or ISO.match(j.posted_at)
        assert j.closes_at is None or ISO.match(j.closes_at)
        assert j.salary_period in (None, "year", "month", "week", "day", "hour")
        json.dumps(j.to_dict())  # serialisable


@pytest.fixture(autouse=True)
def _live_gate(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live"):
        expr = (request.config.getoption("markexpr", "") or "").replace("not live", "")
        if "live" not in expr and os.environ.get("JOBSWEEP_LIVE") != "1":
            pytest.skip("live test: run with -m live")


def test_registry_paths_match_classes() -> None:
    for name in ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable", "recruitee"):
        cls = get_source_class(name)
        assert cls is not None, SOURCE_PATHS[name]
        assert cls.name == name and cls.kind == "board"


# ----------------------------------------------------------------------------------------------- greenhouse
GH = "https://boards-api.greenhouse.io/v1/boards"


def test_greenhouse_anthropic() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{GH}/anthropic/jobs").mock(
            return_value=httpx.Response(200, json=load("greenhouse_anthropic.json"))
        )
        jobs = run(GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "anthropic")))
    params = route.calls.last.request.url.params
    assert params["content"] == "true" and params["pay_transparency"] == "true"
    check_contract(jobs, "greenhouse")
    assert len(jobs) == 3
    onsite, remote, hybrid = jobs
    raw = load("greenhouse_anthropic.json")["jobs"][0]
    assert onsite.source_id == str(raw["id"]) and onsite.url == raw["absolute_url"]
    assert onsite.remote is False and remote.remote is True and hybrid.remote is False
    assert onsite.salary_min == raw["pay_input_ranges"][0]["min_cents"] / 100
    assert onsite.salary_max == raw["pay_input_ranges"][0]["max_cents"] / 100
    assert onsite.salary_currency == "USD" and onsite.salary_period == "year"
    assert onsite.description.startswith("About Anthropic")
    assert onsite.department == raw["departments"][0]["name"]
    assert onsite.location == "; ".join(p.strip() for p in raw["location"]["name"].split("|"))
    assert onsite.posted_at.startswith(raw["first_published"][:10])
    assert onsite.extra["board"] == "anthropic" and onsite.extra["requisition_id"]


def test_greenhouse_hourly_pay_and_deadline() -> None:
    with respx.mock() as router:
        router.get(f"{GH}/robinhood/jobs").mock(
            return_value=httpx.Response(200, json=load("greenhouse_robinhood.json"))
        )
        jobs = run(GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "robinhood")))
    check_contract(jobs, "greenhouse")
    hourly, yearly = jobs
    assert hourly.salary_period == "hour" and hourly.salary_max < 300
    assert hourly.closes_at and ISO.match(hourly.closes_at)
    assert yearly.salary_period == "year" and yearly.salary_min > 10000


def test_greenhouse_workplace_type_metadata() -> None:
    with respx.mock() as router:
        router.get(f"{GH}/airbnb/jobs").mock(
            return_value=httpx.Response(200, json=load("greenhouse_airbnb.json"))
        )
        jobs = run(GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "airbnb")))
    check_contract(jobs, "greenhouse")
    assert [j.remote for j in jobs] == [True, False]
    assert jobs[0].salary_min and jobs[0].salary_currency


def test_greenhouse_malformed_postings_are_skipped() -> None:
    data = load("greenhouse_anthropic.json")
    data["jobs"] += [
        {"id": None, "title": "no id"},
        {"id": 1, "title": None},
        "not-a-dict",
        {"id": 2, "title": "Broken location", "location": "NYC"},  # str instead of dict -> AttributeError
        {
            "id": 3,
            "title": "Minimal posting",
            "absolute_url": None,
            "metadata": None,
            "pay_input_ranges": None,
        },
    ]
    with respx.mock() as router:
        router.get(f"{GH}/anthropic/jobs").mock(return_value=httpx.Response(200, json=data))
        jobs = run(
            GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "anthropic", max_jobs=10))
        )
    assert [j.source_id for j in jobs][-1] == "3" and len(jobs) == 4
    assert jobs[-1].url == "https://job-boards.greenhouse.io/anthropic/jobs/3"


def test_greenhouse_unknown_board_raises_and_max_jobs() -> None:
    with respx.mock() as router:
        router.get(f"{GH}/nope/jobs").mock(
            return_value=httpx.Response(404, json={"status": 404, "error": "Job not found"})
        )
        with pytest.raises(HTTPError):
            run(GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "nope")))
        router.get(f"{GH}/anthropic/jobs").mock(
            return_value=httpx.Response(200, json=load("greenhouse_anthropic.json"))
        )
        jobs = run(GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "anthropic", max_jobs=2)))
    assert len(jobs) == 2
    with pytest.raises(SourceError):
        run(GreenhouseSource, lambda s: s.fetch_board(employer("greenhouse", "")))


# ----------------------------------------------------------------------------------------------- lever
def test_lever_palantir() -> None:
    raw = load("lever_palantir.json")
    with respx.mock() as router:
        route = router.get("https://api.lever.co/v0/postings/palantir").mock(
            return_value=httpx.Response(200, json=raw)
        )
        jobs = run(LeverSource, lambda s: s.fetch_board(employer("lever", "palantir")))
    assert route.calls.last.request.url.params["mode"] == "json"
    check_contract(jobs, "lever")
    full, intern, remote = jobs
    assert full.source_id == raw[0]["id"] and full.url == raw[0]["hostedUrl"]
    assert full.remote is False and remote.remote is True
    assert full.employment_type == "full-time" and intern.employment_type == "internship"
    assert intern.tags == ["internship"] and full.tags == []
    for lst in raw[0]["lists"]:  # lists[] headings are part of the description
        assert lst["text"].strip()[:30] in full.description
    assert "<li>" not in full.description
    assert full.posted_at.startswith("20") and full.department == raw[0]["categories"].get(
        "department", full.department
    )
    assert raw[0]["categories"]["location"] in full.location


def test_lever_salary_range_and_eu_instance() -> None:
    raw = load("lever_zoox.json")
    with respx.mock() as router:
        eu = router.get("https://api.eu.lever.co/v0/postings/zoox").mock(
            return_value=httpx.Response(200, json=raw)
        )
        jobs = run(LeverSource, lambda s: s.fetch_board(employer("lever", "eu:zoox")))
    assert eu.called
    check_contract(jobs, "lever")
    yearly, hourly = jobs
    assert (yearly.salary_min, yearly.salary_max) == (
        raw[0]["salaryRange"]["min"],
        raw[0]["salaryRange"]["max"],
    )
    assert yearly.salary_period == "year" and hourly.salary_period == "hour"
    assert yearly.salary_currency == "USD"


def test_lever_unexpected_payload() -> None:
    with respx.mock() as router:
        router.get("https://api.lever.co/v0/postings/acme").mock(
            return_value=httpx.Response(200, json={"ok": False})
        )
        with pytest.raises(SourceError):
            run(LeverSource, lambda s: s.fetch_board(employer("lever", "acme")))


# ----------------------------------------------------------------------------------------------- ashby
ASHBY = "https://api.ashbyhq.com/posting-api/job-board"


def test_ashby_openai() -> None:
    raw = load("ashby_openai.json")
    with respx.mock() as router:
        route = router.get(f"{ASHBY}/openai").mock(return_value=httpx.Response(200, json=raw))
        jobs = run(AshbySource, lambda s: s.fetch_board(employer("ashby", "openai")))
    assert route.calls.last.request.url.params["includeCompensation"] == "true"
    check_contract(jobs, "ashby")
    secondary, hourly, remote, contract = jobs
    sec_raw = raw["jobs"][0]
    assert secondary.location.split("; ")[0] == sec_raw["location"]
    assert all(s["location"] in secondary.location for s in sec_raw["secondaryLocations"])
    assert secondary.salary_period == "year" and secondary.salary_min and secondary.salary_currency == "USD"
    assert hourly.salary_period == "hour" and hourly.salary_max < 1000
    assert remote.remote is True
    assert contract.employment_type == "contract"
    assert secondary.url == sec_raw["jobUrl"] and secondary.posted_at.startswith(sec_raw["publishedAt"][:10])


def test_ashby_unlisted_skipped_and_hybrid_is_not_remote() -> None:
    raw = load("ashby_openai.json")
    raw["jobs"][1]["isListed"] = False
    raw["jobs"][0]["workplaceType"], raw["jobs"][0]["isRemote"] = (
        "Hybrid",
        True,
    )  # Ashby flags hybrid as isRemote
    with respx.mock() as router:
        router.get(f"{ASHBY}/openai").mock(return_value=httpx.Response(200, json=raw))
        jobs = run(AshbySource, lambda s: s.fetch_board(employer("ashby", "openai")))
    assert len(jobs) == 3 and jobs[0].remote is False


def test_ashby_compensation_from_tiers_only() -> None:
    comp = copy.deepcopy(load("ashby_openai.json")["jobs"][1]["compensation"])
    comp.pop("summaryComponents")
    lo, hi, cur, period = compensation(comp)
    assert lo and hi and lo <= hi and cur == "USD" and period == "hour"
    assert compensation(None) == (None, None, None, None)


# ----------------------------------------------------------------------------------------------- workday
FRS = "https://rb.wd5.myworkdayjobs.com/wday/cxs/rb/FRS"
AIG = "https://aig.wd1.myworkdayjobs.com/wday/cxs/aig/aig"


def test_workday_pages_past_unreliable_total_until_max_jobs() -> None:
    p0, p1 = load("workday_frs_analyst_p0.json"), load("workday_frs_analyst_p1.json")
    assert p1["total"] == 0  # real behaviour: total only on the first page
    bodies: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={0: p0, 20: p1}[body["offset"]])

    with respx.mock() as router:
        router.post(f"{FRS}/jobs").mock(side_effect=reply)
        emp = employer("workday", "rb/wd5/FRS", search_terms=["analyst"], max_jobs=40)
        jobs = run(WorkdaySource, lambda s: s.fetch_board(emp))
    assert [b["offset"] for b in bodies] == [0, 20]
    assert all(b["limit"] == 20 and b["searchText"] == "analyst" and b["appliedFacets"] == {} for b in bodies)
    check_contract(jobs, "workday")
    assert len(jobs) == 40
    first = p0["jobPostings"][0]
    assert jobs[0].url == "https://rb.wd5.myworkdayjobs.com/FRS" + first["externalPath"]
    assert jobs[0].source_id == f"rb:{first['bulletFields'][0]}"
    assert jobs[0].posted_at and re.fullmatch(r"\d{4}-\d{2}-\d{2}", jobs[0].posted_at)
    multi = [j for j in jobs if "locations_count" in j.extra]
    assert multi and all(j.location and "Locations" not in j.location for j in multi)


def test_workday_search_terms_dedupe_and_short_page() -> None:
    page = load("workday_aig_political_risk.json")
    texts: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["searchText"])
        return httpx.Response(200, json=page)

    with respx.mock() as router:
        router.post(f"{AIG}/jobs").mock(side_effect=reply)
        emp = employer("workday", "aig/wd1/aig", search_terms=["political risk", "Political Risk"])
        jobs = run(WorkdaySource, lambda s: s.fetch_board(emp))
    assert texts == ["political risk", "Political Risk"]  # one search per term, short page stops paging
    check_contract(jobs, "workday")
    assert len(jobs) == len(page["jobPostings"])
    assert all(j.source_id.startswith("aig:JR") for j in jobs)


def test_workday_enrich_detail() -> None:
    page, detail = load("workday_aig_political_risk.json"), load("workday_aig_detail.json")
    info = detail["jobPostingInfo"]
    path = "/" + info["externalUrl"].split("/aig/", 1)[1]  # truncated in the payload; use the posting path
    posting = next(p for p in page["jobPostings"] if p["externalPath"].startswith(path))

    async def go(src: WorkdaySource) -> Job:
        job = src._parse(posting, employer("workday", "aig/wd1/aig"), "aig/wd1/aig", "aig", "wd1", "aig")
        return await src.enrich(job)

    with respx.mock() as router:
        route = router.get(f"{AIG}{posting['externalPath']}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        job = run(WorkdaySource, go)
    assert route.called
    check_contract([job], "workday")
    assert job.description and "Political" in job.description
    assert job.employment_type == "full-time"
    assert job.posted_at == info["startDate"] and job.closes_at == info["endDate"]
    assert job.extra["job_req_id"] == info["jobReqId"] and job.extra["enriched"] is True
    assert job.url.endswith(posting["externalPath"])  # not the truncated externalUrl


def test_workday_enrich_remote_multi_location_from_url_only() -> None:
    detail = load("workday_frs_detail_remote.json")
    info = detail["jobPostingInfo"]
    url = info["externalUrl"]
    job = Job(source="workday", source_id="rb:" + info["jobReqId"], title=info["title"], company="X", url=url)
    with respx.mock() as router:
        path = url.split("/FRS", 1)[1]
        router.get(f"{FRS}{path}").mock(return_value=httpx.Response(200, json=detail))
        job = run(WorkdaySource, lambda s: s.enrich(job))
    assert job.remote is True
    assert job.location.split("; ")[0] == info["location"]
    assert len(job.location.split("; ")) == 1 + len(info["additionalLocations"])


def test_workday_enrich_failure_keeps_job_and_bad_board() -> None:
    job = Job(
        source="workday",
        source_id="rb:R-1",
        title="T",
        company="X",
        url="https://rb.wd5.myworkdayjobs.com/FRS/job/X/T_R-1",
        extra={"board": "rb/wd5/FRS", "external_path": "/job/X/T_R-1"},
    )
    with respx.mock() as router:
        router.get(f"{FRS}/job/X/T_R-1").mock(return_value=httpx.Response(404, json={"errorCode": "S21"}))
        out = run(WorkdaySource, lambda s: s.enrich(job))
    assert out is job and out.description == ""
    malformed = {
        "jobPostingInfo": {"jobDescription": "<p>Body</p>", "country": "US", "additionalLocations": "x"}
    }
    with respx.mock() as router:
        router.get(f"{FRS}/job/X/T_R-1").mock(return_value=httpx.Response(200, json=malformed))
        out = run(WorkdaySource, lambda s: s.enrich(job))
    assert out is job and out.description == "Body"  # applied up to the malformed field, no exception
    with pytest.raises(SourceError):
        parse_board("rb/FRS")
    assert parse_board("RB/WD5/FRS") == ("rb", "wd5", "FRS")


# ----------------------------------------------------------------------------------------------- smartrecruiters
SR = "https://api.smartrecruiters.com/v1/companies"


def test_smartrecruiters_search_terms_and_enrich() -> None:
    lst, detail = (
        load("smartrecruiters_securitas_intelligence.json"),
        load("smartrecruiters_securitas_detail.json"),
    )

    async def go(src: SmartRecruitersSource) -> list[Job]:
        jobs = await src.fetch_board(employer("smartrecruiters", "Securitas", search_terms=["intelligence"]))
        target = next(j for j in jobs if j.source_id == detail["id"])
        await src.enrich(target)
        return jobs

    with respx.mock() as router:
        listing = router.get(f"{SR}/Securitas/postings").mock(return_value=httpx.Response(200, json=lst))
        router.get(f"{SR}/Securitas/postings/{detail['id']}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        jobs = run(SmartRecruitersSource, go)
    params = listing.calls[0].request.url.params
    assert params["q"] == "intelligence" and params["limit"] == "100" and params["offset"] == "0"
    check_contract(jobs, "smartrecruiters")
    assert len(jobs) == len(lst["content"])
    raw0 = lst["content"][0]
    j0 = jobs[0]
    assert j0.url == f"https://jobs.smartrecruiters.com/Securitas/{raw0['id']}"
    assert j0.location.startswith(raw0["location"]["city"]) and j0.remote is False
    assert j0.employment_type == "full-time"
    enriched = next(j for j in jobs if j.source_id == detail["id"])
    assert enriched.extra["enriched"] and enriched.extra["posting_url"] == detail["postingUrl"]
    first_section = html_text_head(detail["jobAd"]["sections"]["jobDescription"]["text"])
    assert first_section in enriched.description
    na = [
        j for j, r in zip(jobs, lst["content"], strict=True) if r["experienceLevel"]["id"] == "not_applicable"
    ]
    assert all(j.seniority is None for j in na)


def html_text_head(html: str) -> str:
    from jobsweep.text import html_to_text

    return html_to_text(html)[:40]


def test_smartrecruiters_pagination_and_max_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobsweep.sources.smartrecruiters as srmod

    monkeypatch.setattr(srmod, "PAGE_SIZE", 2)
    p0, p1 = load("smartrecruiters_experian_p0.json"), load("smartrecruiters_experian_p1.json")
    offsets: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        off = request.url.params["offset"]
        offsets.append(off)
        return httpx.Response(200, json={"0": p0, "2": p1}[off])

    with respx.mock() as router:
        router.get(f"{SR}/Experian/postings").mock(side_effect=reply)
        jobs = run(
            SmartRecruitersSource,
            lambda s: s.fetch_board(employer("smartrecruiters", "Experian", max_jobs=4)),
        )
    assert offsets == ["0", "2"]
    check_contract(jobs, "smartrecruiters")
    assert len(jobs) == 4


# ----------------------------------------------------------------------------------------------- workable
WK = "https://apply.workable.com/api/v3/accounts/wati-dot-i-o/jobs"


@pytest.fixture
def fast_workable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WorkableSource, "min_interval", 0.0)


def test_workable_token_pagination_and_enrich(fast_workable: None) -> None:
    p1, last, detail = (
        load("workable_wati_p1.json"),
        load("workable_wati_last.json"),
        load("workable_wati_detail.json"),
    )
    assert p1["request"]["url"] == WK
    bodies: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=p1["response"] if "token" not in body else last["response"])

    code = detail["response"]["shortcode"]

    async def go(src: WorkableSource) -> list[Job]:
        jobs = await src.fetch_board(employer("workable", "wati-dot-i-o"))
        await src.enrich(next(j for j in jobs if j.source_id == code))
        return jobs

    with respx.mock() as router:
        router.post(WK).mock(side_effect=reply)
        router.get(detail["request"]["url"]).mock(return_value=httpx.Response(200, json=detail["response"]))
        jobs = run(WorkableSource, go)
    assert bodies[0] == p1["request"]["body"]
    assert bodies[1]["token"] == p1["response"]["nextPage"] and len(bodies) == 2
    check_contract(jobs, "workable")
    results = p1["response"]["results"] + last["response"]["results"]
    assert [j.source_id for j in jobs] == [r["shortcode"] for r in results]
    by_workplace = {r["workplace"]: j.remote for r, j in zip(results, jobs, strict=True)}
    assert by_workplace == {"hybrid": False, "remote": True, "on_site": False}
    j0 = jobs[0]
    assert j0.url == f"https://apply.workable.com/wati-dot-i-o/j/{results[0]['shortcode']}/"
    assert j0.employment_type == "full-time" and j0.department == results[0]["department"][0]
    assert j0.location.startswith(results[0]["location"]["city"])
    assert j0.extra["enriched"] and "Requirements" in j0.description and len(j0.description) > 200


def test_workable_rate_limit_circuit_breaker(fast_workable: None) -> None:
    async def go(src: WorkableSource) -> tuple[type, type]:
        errors = []
        for _ in range(2):
            try:
                await src.fetch_board(employer("workable", "wati-dot-i-o"))
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc))
        return errors[0], errors[1]

    with respx.mock() as router:
        route = router.post(WK).mock(
            return_value=httpx.Response(429, text="error code: 1015", headers={"Retry-After": "0"})
        )
        first, second = run(WorkableSource, go)
    assert first is HTTPError and second is SourceError
    assert route.call_count == 2  # one try + one retry, then the breaker short-circuits


# ----------------------------------------------------------------------------------------------- recruitee
def test_recruitee_crisisgroup() -> None:
    raw = load("recruitee_crisisgroup.json")
    with respx.mock() as router:
        router.get("https://crisisgroup.recruitee.com/api/offers/").mock(
            return_value=httpx.Response(200, json=raw)
        )
        jobs = run(RecruiteeSource, lambda s: s.fetch_board(employer("recruitee", "crisisgroup")))
    check_contract(jobs, "recruitee")
    assert len(jobs) == len(raw["offers"])
    for job, offer in zip(jobs, raw["offers"], strict=True):
        assert job.url == offer["careers_url"] and job.source_id == str(offer["id"])
        assert job.posted_at.startswith(offer["published_at"][:10])
        assert job.remote is False  # hybrid
    intern = next(j for j in jobs if j.employment_type == "internship")
    assert "internship" in intern.tags and "student" in intern.tags
    analyst = next(j for j in jobs if j.title.startswith("Analyst"))
    assert analyst.closes_at and "Washington" in analyst.location and "Requirements" in analyst.description


def test_recruitee_salary_and_part_time() -> None:
    raw = load("recruitee_vandebron.json")
    with respx.mock() as router:
        router.get("https://vandebron.recruitee.com/api/offers/").mock(
            return_value=httpx.Response(200, json=raw)
        )
        jobs = run(RecruiteeSource, lambda s: s.fetch_board(employer("recruitee", "vandebron")))
    check_contract(jobs, "recruitee")
    paid, student = jobs
    sal = raw["offers"][0]["salary"]
    assert (paid.salary_min, paid.salary_max) == (float(sal["min"]), float(sal["max"]))
    assert paid.salary_currency == "EUR" and paid.salary_period == "month"
    assert "student" in student.tags


# ----------------------------------------------------------------------------------------------- robustness
GARBAGE: list[Any] = [
    None,
    7,
    "posting",
    {},
    {"id": "x"},
    {
        # every field present but with the wrong type
        "id": 99,
        "title": "Garbage Analyst",
        "text": "Garbage Analyst",
        "name": "Garbage Analyst",
        "shortcode": "GARBAGE01",
        "externalPath": "/job/Nowhere/Garbage-Analyst_R-99",
        "location": 5,
        "locations": "bad",
        "locationsText": 3,
        "categories": "bad",
        "compensation": "bad",
        "salary": "bad",
        "salaryRange": "bad",
        "secondaryLocations": "bad",
        "metadata": "bad",
        "departments": "bad",
        "offices": "bad",
        "typeOfEmployment": "bad",
        "experienceLevel": "bad",
        "department": 7,
        "bulletFields": "bad",
        "lists": "bad",
        "pay_input_ranges": "bad",
        "postedOn": 12,
        "published_at": 12,
        "createdAt": "not a date",
    },
]


@pytest.mark.parametrize(
    ("cls", "ats", "board", "method", "url", "fixture", "key"),
    [
        (
            GreenhouseSource,
            "greenhouse",
            "anthropic",
            "GET",
            f"{GH}/anthropic/jobs",
            "greenhouse_anthropic.json",
            "jobs",
        ),
        (
            LeverSource,
            "lever",
            "palantir",
            "GET",
            "https://api.lever.co/v0/postings/palantir",
            "lever_palantir.json",
            None,
        ),
        (AshbySource, "ashby", "openai", "GET", f"{ASHBY}/openai", "ashby_openai.json", "jobs"),
        (
            WorkdaySource,
            "workday",
            "aig/wd1/aig",
            "POST",
            f"{AIG}/jobs",
            "workday_aig_political_risk.json",
            "jobPostings",
        ),
        (
            SmartRecruitersSource,
            "smartrecruiters",
            "Securitas",
            "GET",
            f"{SR}/Securitas/postings",
            "smartrecruiters_securitas_intelligence.json",
            "content",
        ),
        (
            RecruiteeSource,
            "recruitee",
            "crisisgroup",
            "GET",
            "https://crisisgroup.recruitee.com/api/offers/",
            "recruitee_crisisgroup.json",
            "offers",
        ),
    ],
)
def test_malformed_postings_never_crash_a_board(
    cls: type, ats: str, board: str, method: str, url: str, fixture: str, key: str | None
) -> None:
    data = load(fixture)
    good = len(data if key is None else data[key])
    if key is None:
        data = data + GARBAGE
    else:
        data[key] = data[key] + GARBAGE
    with respx.mock() as router:
        router.route(method=method, url=url).mock(return_value=httpx.Response(200, json=data))
        jobs = run(cls, lambda s: s.fetch_board(employer(ats, board)))
    assert len(jobs) >= good
    for j in jobs:
        assert isinstance(j.title, str) and isinstance(j.location, str) and isinstance(j.description, str)
        json.dumps(j.to_dict())


def test_workable_malformed_results(fast_workable: None) -> None:
    page = load("workable_wati_last.json")["response"]
    page["results"] = page["results"] + GARBAGE
    with respx.mock() as router:
        router.post(WK).mock(return_value=httpx.Response(200, json=page))
        jobs = run(WorkableSource, lambda s: s.fetch_board(employer("workable", "wati-dot-i-o")))
    assert len(jobs) >= 2


# ----------------------------------------------------------------------------------------------- live smoke
def live(cls: type, emp: Employer, enrich: bool = False) -> list[Job]:
    from jobsweep.http import make_client

    async def main() -> list[Job]:
        settings = Settings()
        async with make_client(settings.user_agent) as client:
            src = cls(SourceContext(client=client, settings=settings))
            jobs = await src.fetch_board(emp)
            if enrich and jobs:
                await src.enrich(jobs[0])
            return jobs

    return asyncio.run(main())


@pytest.mark.live
@pytest.mark.parametrize(
    ("cls", "ats", "board", "kw"),
    [
        (GreenhouseSource, "greenhouse", "anthropic", {}),
        (LeverSource, "lever", "palantir", {}),
        (AshbySource, "ashby", "openai", {}),
        (RecruiteeSource, "recruitee", "crisisgroup", {}),
        (SmartRecruitersSource, "smartrecruiters", "Securitas", {"search_terms": ["intelligence"]}),
        (WorkdaySource, "workday", "rb/wd5/FRS", {"max_jobs": 40}),
        (WorkdaySource, "workday", "aig/wd1/aig", {"search_terms": ["analyst"], "max_jobs": 20}),
    ],
)
def test_live_board(cls: type, ats: str, board: str, kw: dict) -> None:
    enrich = ats in ("workday", "smartrecruiters")
    jobs = live(cls, employer(ats, board, **kw), enrich=enrich)
    check_contract(jobs, ats)
    if enrich:
        assert jobs[0].description and jobs[0].extra.get("enriched")
    if ats == "workday":
        assert len(jobs) <= kw["max_jobs"]


@pytest.mark.live
def test_live_workable() -> None:
    try:
        jobs = live(WorkableSource, employer("workable", "wati-dot-i-o"), enrich=True)
    except (HTTPError, SourceError) as exc:
        if "429" in str(exc) or "rate limited" in str(exc):
            pytest.skip(f"apply.workable.com rate limited this egress: {str(exc)[:80]}")
        raise
    check_contract(jobs, "workable")
