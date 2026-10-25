"""Tests for the extended ATS board adapters (personio, breezy, bamboohr, paylocity, adp, ultipro,
oracle, jobvite, rippling, pinpoint, teamtailor).

Unit tests replay trimmed real responses from ``fixtures/ats_ext`` with respx (no network).
Live smoke tests (``@pytest.mark.live``) hit one real employer per adapter; run them with
``pytest tests/test_ats_extended.py -m live``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from jobsweep.config import Settings
from jobsweep.models import Employer, Job
from jobsweep.sources import SOURCE_PATHS, get_source_class
from jobsweep.sources.base import Source, SourceContext, SourceError

FIX = Path(__file__).parent / "fixtures" / "ats_ext"
UA = "jobsweep-tests/0.1 (+https://github.com/gildedlily720/gradcafe_data)"
EXTENDED = (
    "personio",
    "breezy",
    "bamboohr",
    "paylocity",
    "adp",
    "ultipro",
    "oracle",
    "jobvite",
    "rippling",
    "pinpoint",
    "teamtailor",
)


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fxj(name: str) -> Any:
    return json.loads(fx(name))


def emp(ats: str, board: str, **kw: Any) -> Employer:
    return Employer(key=f"test-{ats}", name=f"Test {ats.title()}", ats=ats, board=board, **kw)


def run(source: str, fn: Callable[[Source], Awaitable[Any]]) -> Any:
    """Build a SourceContext inside the event loop and run ``fn(adapter)``."""

    async def main() -> Any:
        async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=60) as client:
            cls = get_source_class(source)
            assert cls is not None, f"adapter {source} failed to import"
            return await fn(cls(SourceContext(client=client, settings=Settings(env={}))))

    return asyncio.run(main())


def board(source: str, employer: Employer) -> list[Job]:
    return run(source, lambda s: s.fetch_board(employer))


def enrich(source: str, job: Job) -> Job:
    return run(source, lambda s: s.enrich(job))


def qs(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


# --------------------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXTENDED)
def test_registry_paths_import(name: str) -> None:
    cls = get_source_class(name)
    assert cls is not None, SOURCE_PATHS[name]
    assert cls.name == name
    assert cls.kind == "board"
    assert cls.label
    assert cls.availability(Settings(env={})) == (True, "ok")


# --------------------------------------------------------------------------------------------------
# personio
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_personio_board() -> None:
    respx.get("https://prewave.jobs.personio.de/xml").respond(200, text=fx("personio_prewave.xml"))
    jobs = board("personio", emp("personio", "prewave"))
    assert len(jobs) == 3
    by_id = {j.source_id: j for j in jobs}
    sdr = by_id["prewave:1968836"]
    assert sdr.uid == "personio:prewave:1968836"
    assert sdr.title.startswith("B2B Sales Development Representative - DACH & US")
    assert sdr.url == "https://prewave.jobs.personio.de/job/1968836"
    assert sdr.location == "Vienna; Ireland"
    assert sdr.employment_type == "full-time"
    assert sdr.department == "Sales"
    assert sdr.seniority == "experienced"
    assert sdr.posted_at == "2025-02-14T14:05:20+00:00"
    assert sdr.company == "Test Personio" and sdr.employer_key == "test-personio"
    assert sdr.extra["years_of_experience"] == "1-2"
    assert "What we build" in sdr.description and "<" not in sdr.description
    assert by_id["prewave:2625679"].seniority == "entry-level"
    assert all(j.remote is None for j in jobs)


@respx.mock
def test_personio_com_host_and_employment_mapping() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?><workzag-jobs>
      <position><id>1</id><office>Remote</office><name>Intern, Research</name>
        <employmentType>intern</employmentType><schedule>full-time</schedule><createdAt>2026-01-02T00:00:00+00:00</createdAt></position>
      <position><id>2</id><office>Berlin</office><name>Werkstudent (m/w/d) Policy</name>
        <employmentType>working_student</employmentType><seniority>student</seniority><schedule>part-time</schedule></position>
      <position><id>3</id><office>Berlin</office><name>Trainee Analyst</name><employmentType>trainee</employmentType>
        <schedule>full-time</schedule></position>
      <position><office>Berlin</office><name>No id: skipped</name></position>
      <position><id>5</id><office>Berlin</office><name>Freelance Editor</name><employmentType>freelance</employmentType></position>
    </workzag-jobs>"""
    respx.get("https://acme.jobs.personio.com/xml").respond(200, text=xml)
    jobs = board("personio", emp("personio", "acme.jobs.personio.com"))
    got = {j.title: (j.employment_type, j.tags, j.remote) for j in jobs}
    assert got == {
        "Intern, Research": ("internship", ["internship"], True),
        "Werkstudent (m/w/d) Policy": ("part-time", ["student"], None),
        "Trainee Analyst": ("full-time", ["graduate-program"], None),
        "Freelance Editor": ("contract", [], None),
    }
    assert jobs[0].url == "https://acme.jobs.personio.com/job/1"


@respx.mock
def test_personio_invalid_xml_raises() -> None:
    respx.get("https://broken.jobs.personio.de/xml").respond(200, text="<html>not xml")
    with pytest.raises(SourceError):
        board("personio", emp("personio", "broken"))


# --------------------------------------------------------------------------------------------------
# breezy
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_breezy_board_and_malformed_rows() -> None:
    data = fxj("breezy_zantech.json") + [None, "junk", {"id": "x"}, {"name": "no id"}]
    route = respx.get("https://zantech-it.breezy.hr/json").respond(200, json=data)
    jobs = board("breezy", emp("breezy", "zantech-it"))
    assert qs(route.calls.last.request) == {"verbose": "true"}
    assert [j.source_id for j in jobs] == ["2fffd15c0bdc", "1b91df01cd77", "7fb9c877a580"]
    first, _, cloud = jobs
    assert first.url == "https://zantech-it.breezy.hr/p/2fffd15c0bdc-acquisition-management-support-iii"
    assert first.location == "Aberdeen Proving Ground, MD"
    assert first.remote is False and cloud.remote is True
    assert first.employment_type == "full-time"
    assert first.department == "DoW"
    assert first.posted_at.startswith("2025-12-22T19:50:28")
    assert first.description.startswith("Are you looking for your next challenge?")


@respx.mock
def test_breezy_enrich_from_posting_jsonld() -> None:
    respx.get("https://zantech-it.breezy.hr/p/2fffd15c0bdc-acquisition-management-support-iii").respond(
        200, text=fx("breezy_zantech_posting.html")
    )
    job = Job(
        source="breezy",
        source_id="2fffd15c0bdc",
        title="Acquisition Management Support III",
        company="Zantech",
        url="https://zantech-it.breezy.hr/p/2fffd15c0bdc-acquisition-management-support-iii",
        extra={"board": "zantech-it"},
    )
    out = enrich("breezy", job)
    assert "Zantech" in out.description and "<p>" not in out.description
    assert out.posted_at == "2025-12-22"


# --------------------------------------------------------------------------------------------------
# bamboohr
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_bamboohr_board_and_enrich() -> None:
    respx.get("https://cnas.bamboohr.com/careers/list").respond(200, json=fxj("bamboohr_cnas_list.json"))
    respx.get("https://cnas.bamboohr.com/careers/136/detail").respond(
        200, json=fxj("bamboohr_cnas_detail.json")
    )
    (job,) = board("bamboohr", emp("bamboohr", "cnas"))
    assert job.uid == "bamboohr:cnas:136"
    assert job.title == "Research Assistant/Associate"
    assert job.url == "https://cnas.bamboohr.com/careers/136"
    assert job.location == "Washington, District of Columbia"
    assert job.remote is False
    assert job.employment_type == "full-time"
    assert job.department == "Technology and National Security Program"
    assert job.description == "" and job.posted_at is None
    job = enrich("bamboohr", job)
    assert job.description.startswith("The Center for a New American Security (CNAS) seeks")
    assert job.posted_at == "2026-07-15"
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (
        55000.0,
        65000.0,
        "USD",
        "year",
    )
    assert job.location == "Washington, District of Columbia, United States"


@respx.mock
def test_bamboohr_remote_and_unknown_status() -> None:
    respx.get("https://accessnow.bamboohr.com/careers/list").respond(
        200, json=fxj("bamboohr_accessnow_list.json")
    )
    jobs = board("bamboohr", emp("bamboohr", "https://accessnow.bamboohr.com/careers"))
    assert [j.title for j in jobs] == ["Incident Response Analyst", "Financial Reporting Manager"]
    assert all(j.remote is True for j in jobs)
    assert jobs[0].location == "Indonesia, India, Philippines"
    assert jobs[1].location == "San Jose, Costa Rica"
    assert jobs[0].employment_type == "full-time"
    respx.get("https://occrp.bamboohr.com/careers/list").respond(
        200,
        json={
            "meta": {"totalCount": 2},
            "result": [
                {"id": "60", "jobOpeningName": "Editor, Moldova", "employmentStatusLabel": "TBD"},
                {"id": None},
            ],
        },
    )
    (occrp,) = board("bamboohr", emp("bamboohr", "occrp"))
    assert occrp.employment_type is None and occrp.location == ""


# --------------------------------------------------------------------------------------------------
# paylocity
# --------------------------------------------------------------------------------------------------

GMF = "43d988e3-6446-4a25-8e28-28602ea60858"


@respx.mock
def test_paylocity_board_from_page_data() -> None:
    respx.get(f"https://recruiting.paylocity.com/recruiting/jobs/All/{GMF}").respond(
        200, text=fx("paylocity_gmf_list.html")
    )
    jobs = board(
        "paylocity", emp("paylocity", f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GMF}/GMF")
    )
    assert [j.source_id for j in jobs] == ["4598961", "4598922", "4580816"]
    trainee = jobs[0]
    assert trainee.title == "Trainee - Bucharest Office"
    assert trainee.url == "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4598961"
    assert trainee.location == "Bucharest, ROU"  # bogus US state dropped for foreign offices
    assert trainee.remote is False
    assert trainee.posted_at == "2026-10-20T07:08:06-05:00"
    assert trainee.extra["location_name"] == "GMF Bucharest"
    assert jobs[2].location == "Berlin, DEU"


@respx.mock
def test_paylocity_enrich_jsonld() -> None:
    respx.get("https://recruiting.paylocity.com/Recruiting/Jobs/Details/4598961").respond(
        200, text=fx("paylocity_gmf_detail.html")
    )
    job = Job(
        source="paylocity", source_id="4598961", title="Trainee - Bucharest Office", company="GMF", url="x"
    )
    job.description = "teaser"
    out = enrich("paylocity", job)
    assert "Performance Period" in out.description and len(out.description) > 200
    assert out.extra["enriched"] is True
    assert out.location  # filled from JSON-LD when the list had none


@respx.mock
def test_paylocity_no_page_data_uses_feed_and_bad_board() -> None:
    respx.get(f"https://recruiting.paylocity.com/recruiting/jobs/All/{GMF}").respond(
        200, text="<html></html>"
    )
    respx.get(f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{GMF}").respond(
        200,
        json={
            "displayName": GMF,
            "jobs": [{"jobId": 7, "title": "Program Assistant", "postedDate": "2026-10-01"}],
        },
    )
    (job,) = board("paylocity", emp("paylocity", GMF))
    assert job.source_id == "7" and job.title == "Program Assistant" and job.posted_at == "2026-10-01"
    with pytest.raises(SourceError):
        board("paylocity", emp("paylocity", "not-a-guid"))


# --------------------------------------------------------------------------------------------------
# adp
# --------------------------------------------------------------------------------------------------

MINTZ = "9b1dca57-4bf4-4218-bc17-ece8b0c3c406"
ADP_API = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
)


@respx.mock
def test_adp_board_paginates_with_one_based_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobsweep.sources import adp

    data = fxj("adp_mintz_list.json")
    reqs = data["jobRequisitions"]
    monkeypatch.setattr(adp, "PAGE", 2)

    def page(request: httpx.Request) -> httpx.Response:
        q = qs(request)
        assert q["cid"] == MINTZ and q["ccId"] == "19000101_000001" and q["$top"] == "2"
        start = int(q["$skip"]) - 1  # $skip is 1-based
        return httpx.Response(200, json={**data, "jobRequisitions": reqs[start : start + 2]})

    route = respx.get(ADP_API).mock(side_effect=page)
    jobs = board("adp", emp("adp", MINTZ))
    assert [qs(c.request)["$skip"] for c in route.calls] == ["1", "3"]
    assert len(jobs) == 4
    india, senior = jobs[0], jobs[1]
    assert india.uid == "adp:9207542293906_1"
    assert "jobId=1009378" in india.url and f"cid={MINTZ}" in india.url
    assert india.location == "Mumbai, Maharashtra, IN"
    assert india.posted_at == "2026-10-06T18:20:00"
    assert senior.location == "Worthing, West Sussex, GB"
    assert jobs[3].employment_type == "full-time"
    assert jobs[3].location == "Washington, DC, US"


@respx.mock
def test_adp_search_terms_filter_titles_and_internal_flag() -> None:
    data = fxj("adp_mintz_list.json")
    internal = json.loads(json.dumps(data["jobRequisitions"][0]))
    internal["itemID"] = "999_1"
    internal["requisitionTitle"] = "Internal Investigator"
    for f in internal["customFieldGroup"]["indicatorFields"]:
        if f["nameCode"]["codeValue"] == "InternalPostingFlag":
            f["indicatorValue"] = True
    data["jobRequisitions"].append(internal)
    data["meta"]["totalNumber"] = 5
    respx.get(ADP_API).respond(200, json=data)
    jobs = board(
        "adp", emp("adp", f"{MINTZ}|19000101_000001", search_terms=["investigator", "due diligence"])
    )
    assert [j.title for j in jobs] == [
        "Senior Investigator",
        "Senior Managing Investigator - Disputes",
        "Managing Investigator - Executive Due Diligence",
    ]


@respx.mock
def test_adp_enrich_description() -> None:
    iid = "9206644501083_1"
    route = respx.get(f"{ADP_API}/{iid}").respond(200, json=fxj("adp_mintz_detail.json"))
    job = Job(source="adp", source_id=iid, title="t", company="c", url="u", extra={"board": MINTZ})
    out = enrich("adp", job)
    assert qs(route.calls.last.request)["cid"] == MINTZ
    assert len(out.description) > 500 and "<" not in out.description
    assert out.employment_type == "full-time"
    assert out.department == "Exec Diligence"


def test_adp_board_parsing() -> None:
    from jobsweep.sources.adp import parse_board

    assert parse_board(MINTZ) == (MINTZ, "19000101_000001")
    assert parse_board(f"{MINTZ.upper()}|9200000_1") == (MINTZ, "9200000_1")
    url = (
        f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={MINTZ}&ccId=123_4"
    )
    assert parse_board(url) == (MINTZ, "123_4")
    with pytest.raises(SourceError):
        parse_board("nope")


# --------------------------------------------------------------------------------------------------
# ultipro
# --------------------------------------------------------------------------------------------------

IREX = "INT1065INRE/86f30819-0126-446a-84bb-cf37a5e878c1"
UP_BASE = f"https://recruiting.ultipro.com/{IREX.split('/')[0]}/JobBoard/{IREX.split('/')[1]}"


@respx.mock
def test_ultipro_board_paginates() -> None:
    data = fxj("ultipro_irex_list.json")
    opps = data["opportunities"]

    def page(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)["opportunitySearch"]
        assert body["QueryString"] == "" and body["Top"] == 50 and body["Filters"] == []
        assert body["OrderBy"][0]["PropertyName"] == "PostedDate"
        chunk = opps[body["Skip"] : body["Skip"] + 2]  # server pretends to page by 2
        return httpx.Response(200, json={**data, "opportunities": chunk, "totalCount": 3})

    route = respx.post(f"{UP_BASE}/JobBoardView/LoadSearchResults").mock(side_effect=page)
    jobs = board("ultipro", emp("ultipro", IREX))
    assert [json.loads(c.request.content)["opportunitySearch"]["Skip"] for c in route.calls] == [0, 2]
    assert [j.title for j in jobs] == [
        "University Officer",
        "Program Coordinator, Partnerships YLAI",
        "Program Coordinator, Community Solutions Program (CSP)",
    ]
    officer, coord, _ = jobs
    assert officer.url == f"{UP_BASE}/OpportunityDetail?opportunityId=ee090dbb-8569-42b0-84ab-de56da17abc7"
    assert officer.location == "Amman, Jordan"
    assert officer.remote is None and coord.remote is False  # JobLocationType 0 = hybrid
    assert officer.employment_type == "full-time"
    assert officer.department == "Global Impact"
    assert officer.extra["requisition_id"] == "UNIVE003322"
    assert coord.location == "Washington, DC, United States"


@respx.mock
def test_ultipro_search_terms_dedupe_and_host_fallback() -> None:
    data = fxj("ultipro_irex_list.json")
    respx.post(f"{UP_BASE}/JobBoardView/LoadSearchResults").respond(404, text="not found")
    alt = f"https://recruiting2.ultipro.com/{IREX.split('/')[0]}/JobBoard/{IREX.split('/')[1]}"
    route = respx.post(f"{alt}/JobBoardView/LoadSearchResults").respond(200, json={**data, "totalCount": 3})
    jobs = board("ultipro", emp("ultipro", IREX, search_terms=["program", "officer"]))
    terms = [json.loads(c.request.content)["opportunitySearch"]["QueryString"] for c in route.calls]
    assert terms == ["program", "officer"]
    assert len(jobs) == 3 and jobs[0].url.startswith(alt)


@respx.mock
def test_ultipro_enrich_detail() -> None:
    oid = "ee090dbb-8569-42b0-84ab-de56da17abc7"
    respx.get(f"{UP_BASE}/OpportunityDetail").respond(200, text=fx("ultipro_irex_detail.html"))
    job = Job(
        source="ultipro",
        source_id=oid,
        title="University Officer",
        company="IREX",
        url="u",
        extra={"board": UP_BASE},
    )
    out = enrich("ultipro", job)
    assert out.description.startswith("Location: Amman, Jordan")
    assert out.extra["enriched"] is True


def test_ultipro_board_formats() -> None:
    from jobsweep.sources.ultipro import parse_board

    assert parse_board(IREX) == (None, "INT1065INRE", "86f30819-0126-446a-84bb-cf37a5e878c1")
    url = "https://recruiting2.ultipro.com/ABC1000XYZ/JobBoard/86f30819-0126-446a-84bb-cf37a5e878c1/?q=x"
    assert parse_board(url) == (
        "recruiting2.ultipro.com",
        "ABC1000XYZ",
        "86f30819-0126-446a-84bb-cf37a5e878c1",
    )


# --------------------------------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------------------------------

ORA = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest"


@respx.mock
def test_oracle_search_terms_paging_and_cap() -> None:
    data = fxj("oracle_jpmc_list.json")
    reqs = data["items"][0]["requisitionList"]
    finders: list[str] = []

    def page(request: httpx.Request) -> httpx.Response:
        f = qs(request)["finder"]
        finders.append(f)
        assert f.startswith("findReqs;siteNumber=CX_1001,") and "sortBy=POSTING_DATES_DESC" in f
        offset = int(re.search(r"offset=(\d+)", f).group(1))
        item = {**data["items"][0], "requisitionList": reqs[offset : offset + 2], "TotalJobsCount": 3}
        return httpx.Response(200, json={**data, "items": [item]})

    respx.get(f"{ORA}/recruitingCEJobRequisitions").mock(side_effect=page)
    e = emp(
        "oracle",
        "jpmc.fa.oraclecloud.com/CX_1001",
        search_terms=['sanctions "screening"', "aml"],
        max_jobs=50,
    )
    jobs = board("oracle", e)
    assert ['keyword="sanctions  screening"' in f for f in finders[:2]] == [True, True]
    assert [re.search(r"offset=(\d+)", f).group(1) for f in finders] == ["0", "2", "0", "2"]
    assert 'keyword="aml"' in finders[2]
    assert len(jobs) == 3  # deduplicated across the two searches
    first = jobs[0]
    assert first.uid == "oracle:jpmc:210800911"
    assert (
        first.url
        == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210800911"
    )
    assert first.location == "Metro Manila, National Capital Region, Philippines"
    assert first.posted_at == "2026-10-21"
    assert first.department == "Payments Lifecycle"
    assert "sanctions" in first.description.lower()
    capped = board("oracle", emp("oracle", "jpmc.fa.oraclecloud.com/CX_1001", max_jobs=1))
    assert len(capped) == 1


@respx.mock
def test_oracle_enrich_detail() -> None:
    route = respx.get(f"{ORA}/recruitingCEJobRequisitionDetails").respond(
        200, json=fxj("oracle_jpmc_detail.json")
    )
    job = Job(
        source="oracle",
        source_id="jpmc:210800911",
        title="t",
        company="JPMorganChase",
        url="u",
        description="short",
        extra={"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "id": "210800911"},
    )
    out = enrich("oracle", job)
    assert qs(route.calls.last.request)["finder"] == 'ById;Id="210800911",siteNumber=CX_1001'
    assert len(out.description) > 300 and out.description != "short"
    assert out.employment_type == "full-time"


def test_oracle_board_formats() -> None:
    from jobsweep.sources.oracle_hcm import parse_board

    assert parse_board("jpmc.fa.oraclecloud.com/CX_1001") == ("jpmc.fa.oraclecloud.com", "CX_1001")
    url = "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/jobsearch/job/123"
    assert parse_board(url) == ("eeho.fa.us2.oraclecloud.com", "jobsearch")
    with pytest.raises(SourceError):
        parse_board("example.com/CX")


# --------------------------------------------------------------------------------------------------
# jobvite
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_jobvite_table_template_and_enrich() -> None:
    respx.get("https://jobs.jobvite.com/brg/jobs").respond(200, text=fx("jobvite_brg_list.html"))
    respx.get("https://jobs.jobvite.com/brg/job/obQRufw6").respond(200, text=fx("jobvite_brg_detail.html"))
    jobs = board("jobvite", emp("jobvite", "brg"))
    assert [(j.source_id, j.title, j.location, j.department) for j in jobs] == [
        ("oQnAvfw2", "Billing Specialist", "London, United Kingdom", "Accounting/Finance"),
        ("obQRufw6", "Financial Services Managing Consultant", "", "Consulting Staff"),
    ]
    consultant = jobs[1]
    assert consultant.extra["locations_count"] == 2
    assert consultant.url == "https://jobs.jobvite.com/brg/job/obQRufw6"
    out = enrich("jobvite", consultant)
    assert out.posted_at == "2024-11-19"
    assert out.employment_type == "full-time"
    assert out.location.startswith("Washington, District Of Columbia, United States; New York, New York")
    assert len(out.description) > 200 and "<div" not in out.description


@respx.mock
def test_jobvite_list_template() -> None:
    respx.get("https://jobs.jobvite.com/mercycorps/jobs").respond(
        200, text=fx("jobvite_mercycorps_list.html")
    )
    jobs = board("jobvite", emp("jobvite", "https://jobs.jobvite.com/careers/mercycorps/jobs"))
    assert len(jobs) == 3
    assert jobs[0].title.startswith("Multi-Country Office (MCO) Senior Business Development")
    assert jobs[0].location == "Amman, Jordan"
    assert jobs[0].department == "Business Development and Fundraising"
    assert all(j.url.startswith("https://jobs.jobvite.com/mercycorps/job/") for j in jobs)


@respx.mock
def test_jobvite_non_jobvite_page_raises() -> None:
    respx.get("https://jobs.jobvite.com/nobody/jobs").respond(200, text="<html><body>Hello</body></html>")
    with pytest.raises(SourceError):
        board("jobvite", emp("jobvite", "nobody"))


# --------------------------------------------------------------------------------------------------
# rippling
# --------------------------------------------------------------------------------------------------

RIP = "https://api.rippling.com/platform/api/ats/v1/board/eurasia-group/jobs"


@respx.mock
def test_rippling_merges_locations_and_enriches() -> None:
    respx.get(RIP).respond(
        200, json=fxj("rippling_eurasia_list.json") + [{"uuid": "x"}, 5, {"name": "no uuid"}]
    )
    jobs = board("rippling", emp("rippling", "https://ats.rippling.com/eurasia-group/jobs"))
    assert [j.title for j in jobs] == [
        "Associate Director, Corporate Advisory & Consulting",
        "Analyst, Southeast Asia (Malaysia)",
        "Associate Director, Japan Client Services",
    ]
    ad = jobs[0]
    assert ad.source_id == "eeb2a159-658b-43af-8511-286befd066ed"
    assert ad.location == "Washington, DC; London, United Kingdom"
    assert ad.department == "Corporates"
    assert ad.url == f"https://ats.rippling.com/eurasia-group/jobs/{ad.source_id}"
    respx.get(f"{RIP}/{ad.source_id}").respond(200, json=fxj("rippling_eurasia_detail.json"))
    out = enrich("rippling", ad)
    assert out.employment_type == "full-time"
    assert (out.salary_min, out.salary_max, out.salary_currency, out.salary_period) == (
        100000.0,
        115000.0,
        "USD",
        "year",
    )
    assert out.posted_at.startswith("2026-08-03T07:24:46")
    assert out.remote is None
    assert len(out.description) > 500 and "<" not in out.description


# --------------------------------------------------------------------------------------------------
# pinpoint
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_pinpoint_board() -> None:
    data = fxj("pinpoint_kharon.json")
    data["data"].append({"id": None, "title": "broken"})
    data["data"].append({"id": 1, "title": "Weird", "location": "not a dict", "job": "nope"})
    respx.get("https://kharon.pinpointhq.com/postings.json").respond(200, json=data)
    jobs = board("pinpoint", emp("pinpoint", "kharon"))
    assert [j.source_id for j in jobs] == ["kharon:501326", "kharon:527104", "kharon:527106", "kharon:1"]
    eng = jobs[0]
    assert eng.title == "Senior Data Engineer"
    assert eng.url == "https://kharon.pinpointhq.com/en/postings/a4f89dba-09f2-4cbd-924e-1af279b92539"
    assert eng.location == "Denver, Colorado"
    assert eng.remote is False  # hybrid
    assert eng.employment_type == "full-time"
    assert eng.department == "Engineering"
    assert (eng.salary_min, eng.salary_max, eng.salary_currency, eng.salary_period) == (
        160000.0,
        200000.0,
        "USD",
        "year",
    )
    assert eng.closes_at == "2026-11-27T23:59:59-08:00"
    assert "RESPONSIBILITIES:" in eng.description and "QUALIFICATIONS:" in eng.description
    assert jobs[1].location == "Madrid"
    assert jobs[3].location == "" and jobs[3].department is None


# --------------------------------------------------------------------------------------------------
# teamtailor
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_teamtailor_rss() -> None:
    respx.get("https://sumsub.teamtailor.com/jobs.rss").respond(200, text=fx("teamtailor_sumsub.rss"))
    jobs = board("teamtailor", emp("teamtailor", "sumsub"))
    assert [j.title for j in jobs] == [
        "AI Engineer",
        "NetSuite Business Analyst",
        "Accountant & Financial Operations Specialist",
    ]
    ai = jobs[0]
    assert ai.uid == "teamtailor:c7daaeb3-cbc8-4384-9700-70b4f84d7f98"
    assert ai.url == "https://careers.sumsub.com/jobs/8590304-ai-engineer"
    assert ai.location == "Limassol, Cyprus"
    assert ai.remote is True
    assert ai.posted_at == "2026-10-22T13:29:21+02:00"
    assert ai.department == "Technical"
    assert ai.extra["role"] == "Backend Developer"
    assert ai.description.startswith("Sumsub is the first AI-powered trust infrastructure")


@respx.mock
def test_teamtailor_json_fallback_and_custom_host() -> None:
    respx.get("https://careers.acme.com/jobs.rss").respond(404, text="nope")
    respx.get("https://careers.acme.com/jobs.json").respond(200, json=fxj("teamtailor_sumsub.json"))
    jobs = board("teamtailor", emp("teamtailor", "https://careers.acme.com/jobs"))
    assert [j.source_id for j in jobs][:1] == ["c7daaeb3-cbc8-4384-9700-70b4f84d7f98"]
    assert jobs[0].location == "Limassol, EU, CY"
    assert jobs[0].posted_at == "2026-10-22T13:29:21+02:00"
    assert len(jobs[0].description) > 500

    from jobsweep.sources.teamtailor import teamtailor_host

    assert teamtailor_host("acme.na") == "acme.na.teamtailor.com"
    assert teamtailor_host("acme") == "acme.teamtailor.com"


# --------------------------------------------------------------------------------------------------
# shared behaviour
# --------------------------------------------------------------------------------------------------


@respx.mock
def test_max_jobs_and_missing_board() -> None:
    respx.get("https://kharon.pinpointhq.com/postings.json").respond(200, json=fxj("pinpoint_kharon.json"))
    assert len(board("pinpoint", emp("pinpoint", "kharon", max_jobs=2))) == 2
    for name in EXTENDED:
        with pytest.raises(SourceError):
            board(name, emp(name, ""))


# --------------------------------------------------------------------------------------------------
# live smoke tests (one real employer per adapter)
# --------------------------------------------------------------------------------------------------

LIVE_TARGETS = [
    ("personio", "prewave", {}),
    ("breezy", "zantech-it", {}),
    ("bamboohr", "cnas", {}),
    ("paylocity", GMF, {}),
    ("adp", MINTZ, {}),
    ("ultipro", IREX, {}),
    ("oracle", "jpmc.fa.oraclecloud.com/CX_1001", {"search_terms": ["sanctions"], "max_jobs": 60}),
    ("jobvite", "mercycorps", {}),
    ("rippling", "eurasia-group", {}),
    ("pinpoint", "kharon", {}),
    ("teamtailor", "sumsub", {}),
]


@pytest.fixture
def live_only(request: pytest.FixtureRequest) -> None:
    expr = request.config.getoption("markexpr", "") or ""
    if "live" not in expr.replace("not live", "") and not os.environ.get("JOBSWEEP_LIVE"):
        pytest.skip("live test: run with -m live (or JOBSWEEP_LIVE=1)")


@pytest.mark.live
@pytest.mark.parametrize(("source", "board_id", "kw"), LIVE_TARGETS, ids=[t[0] for t in LIVE_TARGETS])
def test_live_board(live_only: None, source: str, board_id: str, kw: dict[str, Any]) -> None:
    async def go(src: Source) -> tuple[list[Job], Job | None]:
        jobs = await src.fetch_board(emp(source, board_id, **kw))
        first = await src.enrich(jobs[0]) if jobs else None
        return jobs, first

    jobs, first = run(source, go)
    assert jobs, f"{source} {board_id}: no jobs returned"
    assert len({j.uid for j in jobs}) == len(jobs)
    for j in jobs:
        assert j.source == source and j.title and j.url.startswith("https://")
        assert j.employer_key == f"test-{source}"
        assert "<p>" not in j.description
    assert first is not None and first.description, f"{source}: enrich produced no description"
