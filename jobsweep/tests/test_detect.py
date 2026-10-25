"""ATS detection: URL shapes -> (ats, board), slug variants, careers-page scanning, company probing."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobsweep.config import Settings
from jobsweep.sources.base import SourceContext
from jobsweep.sources.detect import (
    detect_from_url,
    find_boards,
    normalize_employment_type,
    normalize_period,
    probe_company,
    remote_from_text,
    scan_careers_page,
    slug_variants,
    structured_tags,
)

FIX = Path(__file__).parent / "fixtures" / "ats"
GUID = "0a1b2c3d-1111-4222-8333-444455556666"


@pytest.fixture(autouse=True)
def _live_gate(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live"):
        expr = (request.config.getoption("markexpr", "") or "").replace("not live", "")
        if "live" not in expr and os.environ.get("JOBSWEEP_LIVE") != "1":
            pytest.skip("live test: run with -m live")


def run(
    fn: Callable[[SourceContext], Awaitable[Any]],
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> Any:
    async def main() -> Any:
        async with (client_factory or httpx.AsyncClient)() as client:
            return await fn(SourceContext(client=client, settings=Settings()))

    return asyncio.run(main())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # greenhouse
        ("https://boards.greenhouse.io/dataminr", ("greenhouse", "dataminr")),
        ("https://job-boards.greenhouse.io/anthropic/jobs/4980436008", ("greenhouse", "anthropic")),
        ("https://job-boards.eu.greenhouse.io/helsing/jobs/4567?gh_src=abc", ("greenhouse", "helsing")),
        (
            "https://boards.greenhouse.io/embed/job_board?for=recordedfuture&b=https%3A%2F%2Fx",
            ("greenhouse", "recordedfuture"),
        ),
        ("https://boards.greenhouse.io/embed/job_board/js?for=airbnb", ("greenhouse", "airbnb")),
        ("https://boards.greenhouse.io/embed/job_app?token=123&for=discord", ("greenhouse", "discord")),
        ("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true", ("greenhouse", "stripe")),
        # lever
        ("https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd", ("lever", "palantir")),
        ("https://jobs.eu.lever.co/acme/1234/apply", ("lever", "eu:acme")),
        ("https://api.lever.co/v0/postings/zoox?mode=json", ("lever", "zoox")),
        ("https://api.eu.lever.co/v0/postings/acme?mode=json", ("lever", "eu:acme")),
        # ashby
        (
            "https://jobs.ashbyhq.com/openai/240d459b-696d-43eb-8497-fab3e56ecd9b/application",
            ("ashby", "openai"),
        ),
        ("https://jobs.ashbyhq.com/openai/embed?version=2", ("ashby", "openai")),
        (
            "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true",
            ("ashby", "openai"),
        ),
        # workday
        (
            "https://rb.wd5.myworkdayjobs.com/FRS?hiringCompany=7186c32a743f01353b10a1caaf01da0b",
            ("workday", "rb/wd5/FRS"),
        ),
        (
            "https://rb.wd5.myworkdayjobs.com/en-US/FRS/job/Minneapolis-MN/Economist--Analyst_R-0000033737-1",
            ("workday", "rb/wd5/FRS"),
        ),
        ("https://aig.wd1.myworkdayjobs.com/wday/cxs/aig/aig/jobs", ("workday", "aig/wd1/aig")),
        ("https://Travelers.WD5.myworkdayjobs.com/External", ("workday", "travelers/wd5/External")),
        (
            "https://wd3.myworkdaysite.com/en-US/recruiting/acme/External_Careers",
            ("workday", "acme/wd3/External_Careers"),
        ),
        # smartrecruiters
        (
            "https://jobs.smartrecruiters.com/Experian/744000157541319-analista",
            ("smartrecruiters", "Experian"),
        ),
        ("https://careers.smartrecruiters.com/Securitas", ("smartrecruiters", "Securitas")),
        (
            "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings?limit=100",
            ("smartrecruiters", "BoschGroup"),
        ),
        (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Visa/publication/3c1c0a3e-1a2b",
            ("smartrecruiters", "Visa"),
        ),
        # workable
        ("https://apply.workable.com/huggingface/j/ABC123DEF0/", ("workable", "huggingface")),
        ("https://apply.workable.com/api/v3/accounts/wati-dot-i-o/jobs", ("workable", "wati-dot-i-o")),
        ("https://apply.workable.com/api/v1/widget/accounts/acme?details=true", ("workable", "acme")),
        ("https://acme.workable.com/jobs/123", ("workable", "acme")),
        # recruitee
        ("https://crisisgroup.recruitee.com/o/analyst-us-advocacy", ("recruitee", "crisisgroup")),
        ("https://crisisgroup.recruitee.com/api/offers/", ("recruitee", "crisisgroup")),
        # ats-extended formats (§3.1)
        ("https://acme.jobs.personio.de/job/123?language=en", ("personio", "acme")),
        ("https://acme.jobs.personio.com/", ("personio", "acme.jobs.personio.com")),
        ("https://acme.breezy.hr/p/abc123-analyst", ("breezy", "acme")),
        ("https://acme.bamboohr.com/careers/12", ("bamboohr", "acme")),
        (f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}/Acme-Inc", ("paylocity", GUID)),
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            f"?cid={GUID}&ccId=19000101_000001&lang=en_US",
            ("adp", f"{GUID}|19000101_000001"),
        ),
        (
            f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={GUID}",
            ("adp", GUID),
        ),
        (
            f"https://recruiting.ultipro.com/ACM1000ACME/JobBoard/{GUID}/?q=&o=postedDateDesc",
            ("ultipro", f"ACM1000ACME/{GUID}"),
        ),
        (
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
            ("oracle", "jpmc.fa.oraclecloud.com/CX_1001"),
        ),
        (
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/requisitions",
            ("oracle", "eeho.fa.us2.oraclecloud.com/CX_45001"),
        ),
        ("https://jobs.jobvite.com/acme/job/oABC123", ("jobvite", "acme")),
        ("https://jobs.jobvite.com/careers/acme/jobs", ("jobvite", "acme")),
        ("https://ats.rippling.com/acme/jobs/5b9c1a2e", ("rippling", "acme")),
        ("https://acme.pinpointhq.com/en/postings/5b9c1a2e", ("pinpoint", "acme")),
        ("https://acme.teamtailor.com/jobs", ("teamtailor", "acme")),
        ("https://acme.na.teamtailor.com/jobs/123-analyst", ("teamtailor", "acme.na")),
    ],
)
def test_detect_from_url(url: str, expected: tuple[str, str]) -> None:
    assert detect_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/about/careers/applications/",
        "https://careers.example.com/jobs?gh_jid=12345",
        "https://www.workable.com/",
        "https://apply.workable.com/j/ABC123DEF0",  # job link without the account name
        "https://boards.greenhouse.io/embed/job_board",  # embed without for=
        "https://www.lever.co/",
        "https://app.recruitee.com/login",
        "https://www.myworkday.com/acme/d/home.htmld",
        "",
    ],
)
def test_detect_from_url_none(url: str) -> None:
    assert detect_from_url(url) is None


def test_vendor_only_systems_need_opt_in() -> None:
    url = "https://careers-acme.icims.com/jobs/1234/analyst/job"
    assert detect_from_url(url) is None
    assert detect_from_url(url, include_vendors=True) == ("icims", "careers-acme")
    assert detect_from_url("https://acme.taleo.net/careersection/2/jobsearch.ftl", include_vendors=True) == (
        "taleo",
        "acme",
    )
    assert detect_from_url("https://acme.applytojob.com/apply", include_vendors=True) == ("jazzhr", "acme")


def test_find_boards_in_html_snippet() -> None:
    html = (FIX / "careers_anthropic_jobs.html").read_text(encoding="utf-8")
    assert find_boards(html) == [("greenhouse", "anthropic")]
    mixed = (
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
        '<a href="https://jobs.lever.co/acme-eu">EU</a><a href="https://jobs.lever.co/acme-eu/123">x</a>'
    )
    assert find_boards(mixed) == [("greenhouse", "acme"), ("lever", "acme-eu")]


def test_slug_variants() -> None:
    v = slug_variants("Control Risks Group")
    assert v[:2] == ["controlrisks", "control-risks"] and "control" in v and "cr" in v
    assert "atandt" in slug_variants("AT&T") and "att" in slug_variants("AT&T")
    assert slug_variants("Société Générale")[0] == "societegenerale"
    assert slug_variants("") == []


def test_shared_normalisers() -> None:
    cases = {
        "FullTime": "full-time",
        "Full time": "full-time",
        "fulltime_permanent": "full-time",
        "permanent": "full-time",
        "PartTime": "part-time",
        "Part time": "part-time",
        "Intern": "internship",
        "Internship": "internship",
        "Contract": "contract",
        "freelance": "contract",
        "Fixed-Term": "temporary",
        "Temporary": "temporary",
        "Fellowship": "fellowship",
        "Scholarship": "other",
        "": None,
        None: None,
    }
    for raw, want in cases.items():
        assert normalize_employment_type(raw) == want, raw
    assert normalize_period("1 YEAR") == "year" and normalize_period("per-hour-wage") == "hour"
    assert normalize_period("per-day-wage") == "day" and normalize_period("per-month-salary") == "month"
    assert remote_from_text("Remote") is True and remote_from_text("Hybrid (Travel-Required)") is False
    assert remote_from_text("On-Site") is False and remote_from_text("San Francisco, CA") is None
    assert structured_tags("Internship", "student_college", "Early Careers") == [
        "internship",
        "student",
        "graduate-program",
    ]


def test_scan_careers_page_follows_one_hop() -> None:
    page = (FIX / "careers_nyfed.html").read_text(encoding="utf-8")
    openings = (FIX / "careers_nyfed_openings.html").read_text(encoding="utf-8")
    with respx.mock() as router:
        router.get("https://www.newyorkfed.org/careers").mock(return_value=httpx.Response(200, html=page))
        hop = router.get("https://www.newyorkfed.org/careers/current_openings.html").mock(
            return_value=httpx.Response(200, html=openings)
        )
        found = run(lambda ctx: scan_careers_page(ctx, "https://www.newyorkfed.org/careers"))
    assert hop.call_count == 1
    assert found == [("workday", "rb/wd5/FRS")]


def test_scan_careers_page_redirect_and_iframe() -> None:
    with respx.mock() as router:
        router.get("https://acme.example/careers").mock(
            return_value=httpx.Response(302, headers={"Location": "https://jobs.lever.co/acme"})
        )
        router.get("https://jobs.lever.co/acme").mock(
            return_value=httpx.Response(200, html="<html>Acme jobs</html>")
        )
        router.get("https://beta.example/jobs").mock(
            return_value=httpx.Response(200, html='<iframe src="/frame.html"></iframe>')
        )
        router.get("https://beta.example/frame.html").mock(
            return_value=httpx.Response(
                200, html='<script src="https://boards.greenhouse.io/embed/job_board/js?for=beta"></script>'
            )
        )
        router.get("https://down.example/").mock(return_value=httpx.Response(404))

        async def go(ctx: SourceContext) -> list:
            return [
                await scan_careers_page(ctx, "https://acme.example/careers"),
                await scan_careers_page(ctx, "https://beta.example/jobs"),
                await scan_careers_page(ctx, "https://down.example/"),
            ]

        redirected, framed, down = run(go, lambda: httpx.AsyncClient(follow_redirects=True))
    assert redirected == [("lever", "acme")]
    assert framed == [("greenhouse", "beta")]
    assert down == []


def test_probe_company_offline() -> None:
    offers = json.loads((FIX / "recruitee_crisisgroup.json").read_text(encoding="utf-8"))
    with respx.mock() as router:
        router.get("https://crisisgroup.recruitee.com/api/offers/").mock(
            return_value=httpx.Response(200, json=offers)
        )
        router.get("https://api.smartrecruiters.com/v1/companies/crisisgroup/postings").mock(
            return_value=httpx.Response(200, json={"offset": 0, "limit": 3, "totalFound": 0, "content": []})
        )
        router.route().mock(return_value=httpx.Response(404, json={"error": "Not Found"}))
        hits = run(
            lambda ctx: probe_company(
                ctx,
                "Crisis Group",
                slugs=["crisisgroup"],
                ats=["greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"],
            )
        )
        lever_hosts = {c.request.url.host for c in router.calls if "lever" in c.request.url.host}
    assert lever_hosts == {"api.lever.co", "api.eu.lever.co"}  # EU instance tried after a US 404
    assert hits == [
        {
            "ats": "recruitee",
            "board": "crisisgroup",
            "open_jobs": len(offers["offers"]),
            "titles": [o["title"] for o in offers["offers"][:3]],
        }
    ]


# ----------------------------------------------------------------------------------------------- live
def live_client() -> httpx.AsyncClient:
    from jobsweep.http import make_client

    return make_client(Settings().user_agent)


@pytest.mark.live
def test_live_scan_careers_page() -> None:
    found = run(lambda ctx: scan_careers_page(ctx, "https://www.newyorkfed.org/careers"), live_client)
    assert ("workday", "rb/wd5/FRS") in found


@pytest.mark.live
def test_live_probe_company() -> None:
    hits = run(
        lambda ctx: probe_company(ctx, "Crisis Group", ats=["greenhouse", "lever", "ashby", "recruitee"]),
        live_client,
    )
    assert any(h["ats"] == "recruitee" and h["board"] == "crisisgroup" and h["open_jobs"] >= 0 for h in hits)
