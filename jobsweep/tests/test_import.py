"""ImportSource.parse: connector output / JSON / CSV / markdown -> Job.

Fixtures (tests/fixtures/import/):
* ``indeed_connector_search.md`` — real Indeed connector (claude.ai) ``search_jobs`` output.
* ``workopia_job_tool.json`` — real Workopia ``job_tool`` search output captured 2026-10-25 (4 of 10 items).
* ``jobs_and_careers_search.json`` — real Jobs & Careers ``job-search`` output captured 2026-10-25.
* ``workopia_rendered_list.md`` — the Workopia items rendered in the numbered-list format the connector
  tells Claude to use (``N. **[Title](url)** — Company · Location · Job Type · Compensation · Posted MMM DD``).
* ``sample_export.csv`` — synthetic spreadsheet export (header row + 3 rows).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobsweep.models import Job
from jobsweep.sources import get_source_class
from jobsweep.sources.manual_import import (
    ImportSource,
    clean_description,
    detect_format,
    job_from_record,
    md_to_text,
    normalize_employment_type,
    normalize_period,
    parse_date,
    remote_from_value,
    salary_from_text,
)

FIX = Path(__file__).parent / "fixtures" / "import"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def check(jobs: list[Job], origin: str) -> None:
    assert jobs
    assert len({j.uid for j in jobs}) == len(jobs)
    for j in jobs:
        assert j.source == "import" and j.source_id.startswith(origin + ":")
        assert j.title and (j.url or j.company)
        assert origin in j.tags
        assert j.posted_at is None or ISO.match(j.posted_at)
        assert j.salary_period in (None, "year", "month", "week", "day", "hour")
        assert "**" not in j.title and "](" not in j.title
        json.dumps(j.to_dict())
        assert Job.from_dict(j.to_dict()).uid == j.uid


# ----------------------------------------------------------------------------------------------- contract
def test_registry_and_signature() -> None:
    assert get_source_class("import") is ImportSource
    assert ImportSource.name == "import" and ImportSource.kind == "import"
    assert isinstance(inspect.getattr_static(ImportSource, "parse"), staticmethod)  # usable without a context
    params = inspect.signature(ImportSource.parse).parameters
    assert list(params)[:3] == ["payload", "origin", "fmt"]
    assert params["origin"].default == "claude-connector" and params["fmt"].default is None


# ----------------------------------------------------------------------------------------------- indeed connector
def test_indeed_connector_markdown() -> None:
    jobs = ImportSource.parse(read("indeed_connector_search.md"), origin="indeed-connector")
    check(jobs, "indeed-connector")
    assert len(jobs) == 3
    rane, marsh, am = jobs
    assert rane.uid == "import:indeed-connector:JOBSEARCH_102148"
    assert rane.title == "RANE Europe Geopolitical Analyst"
    assert rane.company == "RANE (Risk Assistance Network & Exchange)"
    assert rane.location == "New York, NY" and rane.url == "https://to.indeed.com/aaskwm2qqgzy"
    assert rane.posted_at == "2026-07-31"
    assert (rane.salary_min, rane.salary_max, rane.salary_currency, rane.salary_period) == (
        65000,
        75000,
        "USD",
        "year",
    )
    assert rane.employment_type is None  # "N/A"
    assert marsh.title == "Oliver Wyman - Senior FP&A Analyst" and marsh.employment_type == "full-time"
    assert (marsh.salary_min, marsh.salary_max) == (89000, 115000) and marsh.posted_at == "2026-05-21"
    assert am.posted_at == "2026-10-23" and am.salary_max == 155000 and am.company == "Alvarez & Marsal"
    assert rane.tags == ["indeed-connector"]


def test_indeed_markdown_explicit_formats_and_default_origin() -> None:
    text = read("indeed_connector_search.md")
    auto = ImportSource.parse(text)
    assert auto[0].source_id == "claude-connector:JOBSEARCH_102148" and auto[0].tags == ["claude-connector"]
    for fmt in ("md", "markdown", "text", "MD"):
        assert [j.uid for j in ImportSource.parse(text, fmt=fmt)] == [j.uid for j in auto]
    # a wrong explicit format falls back to detection instead of returning nothing
    assert len(ImportSource.parse(text, fmt="json")) == 3
    assert len(ImportSource.parse(text, fmt="csv")) == 3
    assert len(ImportSource.parse(text.encode("utf-8"), origin="Indeed Connector")) == 3
    assert ImportSource.parse(text, origin="Indeed Connector")[0].tags == ["indeed-connector"]


def test_indeed_markdown_wrapped_in_mcp_result() -> None:
    wrapped = {"content": [{"type": "text", "text": read("indeed_connector_search.md")}], "isError": False}
    jobs = ImportSource.parse(wrapped, origin="indeed")
    assert len(jobs) == 3 and jobs[0].source_id == "indeed:JOBSEARCH_102148"
    assert len(ImportSource.parse(json.dumps(wrapped), origin="indeed")) == 3


def test_field_blocks_without_blank_lines_and_label_variants() -> None:
    text = (
        "**Job Title:** Sanctions Analyst\n**Company:** Kharon\n**Location:** Washington, DC\n"
        "**View Job URL:** [Apply](https://to.indeed.com/aa1)\n"
        "**Job Title**: Export Controls Associate\n**Company**: Covington & Burling\n"
        "**Posted on:** Posted 3 days ago\n**Job Type:** Full-time, Contract\n"
        "**View Job URL:** <https://to.indeed.com/aa2>\n"
    )
    jobs = ImportSource.parse(text, origin="t")
    assert [j.title for j in jobs] == ["Sanctions Analyst", "Export Controls Associate"]
    assert jobs[0].url == "https://to.indeed.com/aa1" and jobs[1].url == "https://to.indeed.com/aa2"
    assert jobs[1].employment_type == "full-time" and jobs[1].posted_at and ISO.match(jobs[1].posted_at)
    # no id: sha1(url|title|company)[:12]
    expected = hashlib.sha1(b"https://to.indeed.com/aa1|Sanctions Analyst|Kharon").hexdigest()[:12]
    assert jobs[0].source_id == f"t:{expected}"


# ----------------------------------------------------------------------------------------------- json connectors
def test_workopia_json() -> None:
    raw = read("workopia_job_tool.json")
    jobs = ImportSource.parse(raw, origin="workopia")
    check(jobs, "workopia")
    assert len(jobs) == 4
    intuit, ally, temasek, key = jobs
    assert intuit.uid == "import:workopia:1e9cd4a804c387dc5ef3cf1e5afd5471"
    assert intuit.url == "https://workopia.io/jobs/1e9cd4a804c387dc5ef3cf1e5afd5471?utm=mcp-apply"
    assert intuit.remote is False and intuit.employment_type is None and intuit.posted_at is None
    assert (intuit.salary_min, intuit.salary_max, intuit.salary_currency) == (140500, 190000, "USD")
    assert "Intuit QuickBooks" in intuit.description and "3+ years of experience" in intuit.description
    assert (ally.salary_min, ally.salary_max) == (85000, 150000)  # "$85,000.00 - $150,000.00"
    assert ally.employment_type == "full-time" and ally.remote is False
    assert (temasek.salary_min, temasek.salary_max, temasek.salary_period) == (120000, 175000, "year")
    assert key.remote is True and key.location == "Brooklyn, OH, United States"
    assert ImportSource.parse(json.loads(raw), origin="workopia")[0].uid == intuit.uid  # already-parsed dict


def test_jobs_and_careers_json() -> None:
    jobs = ImportSource.parse(read("jobs_and_careers_search.json"), origin="jobs-and-careers")
    check(jobs, "jobs-and-careers")
    assert [j.company for j in jobs] == ["MSG Entertainment Holdings, LLC", "Dataminr", "City of New York"]
    msg, dataminr, nyc = jobs
    assert msg.url == "" and re.fullmatch(r"jobs-and-careers:[0-9a-f]{12}", msg.source_id)
    assert msg.employment_type == "full-time" and (msg.salary_min, msg.salary_max) == (75000, 130000)
    assert msg.salary_period == "year" and "Identify and analyze risks" in msg.description
    assert dataminr.remote is True and dataminr.salary_min is None
    assert nyc.title == "Investigative Analyst" and nyc.salary_min == 56942
    again = ImportSource.parse(read("jobs_and_careers_search.json"), origin="jobs-and-careers")
    assert [j.uid for j in again] == [j.uid for j in jobs]  # hash ids are stable


def test_json_aliases_and_containers() -> None:
    records = [
        {
            "jobTitle": "Due Diligence Analyst",
            "companyName": {"name": "Kroll"},
            "applyUrl": "https://kroll.example/jobs/77",
            "jobKey": "k-77",
            "datePosted": 1761350400000,
            "salary": {"min": 70000, "max": 82000, "currency": "USD", "type": "YEARLY"},
            "location": {"city": "New York", "state": "NY", "country": "US"},
            "remote": False,
            "employmentType": ["FULL_TIME"],
            "validThrough": "2026-11-30",
            "seniorityLevel": "Entry level",
        },
        {
            "position": "Fellow, National Security Program",
            "organization": "Center for a New American Security",
            "link": "https://cnas.example/fellow",
            "posted": "October 2, 2026",
            "compensation": "N/A",
            "work_mode": "Hybrid",
            "type": "Fellowship",
        },
        {"title": None, "company": "Nobody"},
        {
            "name": "Research Assistant",
            "employer": "Brookings",
            "url": "https://brookings.example/ra",
            "id": 12.0,
        },
        "not a job",
        42,
    ]
    for payload in (
        records,
        {"data": {"jobs": records}},
        {"results": records},
        json.dumps({"items": records}),
    ):
        jobs = ImportSource.parse(payload, origin="api")
        assert [j.title for j in jobs] == [
            "Due Diligence Analyst",
            "Fellow, National Security Program",
            "Research Assistant",
        ]
    dd, fellow, ra = ImportSource.parse(records, origin="api")
    assert dd.source_id == "api:k-77" and dd.company == "Kroll" and dd.location == "New York, NY, US"
    assert dd.posted_at.startswith("2025-10-25") and dd.closes_at == "2026-11-30"
    assert (dd.salary_min, dd.salary_max, dd.salary_period) == (70000, 82000, "year")
    assert dd.remote is None and dd.employment_type == "full-time" and dd.seniority == "Entry level"
    assert fellow.posted_at == "2026-10-02" and fellow.salary_min is None and fellow.remote is False
    assert fellow.employment_type == "fellowship" and "fellowship" in fellow.tags
    assert ra.source_id == "api:12" and ra.company == "Brookings"
    single = ImportSource.parse(
        {"title": "Policy Analyst", "company": "RAND", "url": "https://rand.example/1"}
    )
    assert len(single) == 1 and single[0].company == "RAND"


def test_jsonl_and_structured_content() -> None:
    lines = "\n".join(
        json.dumps({"title": f"Analyst {i}", "company": "Acme", "url": f"https://acme.example/{i}"})
        for i in range(3)
    )
    assert [j.title for j in ImportSource.parse(lines)] == ["Analyst 0", "Analyst 1", "Analyst 2"]
    assert len(ImportSource.parse(lines, fmt="jsonl")) == 3
    mcp = {
        "content": [{"type": "text", "text": "ignored summary"}],
        "structuredContent": {
            "jobs": [{"title": "OSINT Analyst", "company": "Dataminr", "url": "https://d.example/1"}]
        },
    }
    assert [j.title for j in ImportSource.parse(mcp)] == ["OSINT Analyst"]


def test_duplicate_ids_are_dropped() -> None:
    rec = {"title": "Analyst", "company": "Acme", "url": "https://acme.example/1", "id": "A1"}
    assert len(ImportSource.parse([rec, dict(rec), dict(rec, id="A2")])) == 2


# ----------------------------------------------------------------------------------------------- markdown lists
def test_workopia_rendered_numbered_list() -> None:
    jobs = ImportSource.parse(read("workopia_rendered_list.md"), origin="workopia")
    check(jobs, "workopia")
    assert len(jobs) == 4
    intuit, ally, temasek, key = jobs
    assert intuit.title == "Senior Analyst - Risk Data Intelligence, Fintech"
    assert intuit.url == "https://workopia.io/jobs/1e9cd4a804c387dc5ef3cf1e5afd5471?utm=mcp-apply"
    assert intuit.company == "Intuit" and intuit.location == "New York, NY, United States"
    assert intuit.remote is False and (intuit.salary_min, intuit.salary_max) == (140500, 190000)
    assert ally.employment_type == "full-time" and (ally.salary_min, ally.salary_max) == (85000, 150000)
    assert temasek.company == "Temasek Holdings Private Ltd" and temasek.salary_max == 175000
    assert key.remote is True and key.employment_type == "full-time" and key.company == "KeyCorp"
    assert key.posted_at is not None and key.posted_at.endswith("-10-21")
    # JSON and markdown renderings of the same result set do not share ids (the list has no Job Id) but
    # dedupe downstream through the fingerprint
    by_json = {j.fingerprint for j in ImportSource.parse(read("workopia_job_tool.json"), origin="workopia")}
    assert {j.fingerprint for j in jobs} == by_json


def test_other_markdown_shapes() -> None:
    text = """# Results

### 1. Political Risk Analyst
**Company:** Marsh
**Location:** London, UK
**Link:** [Apply here](https://marsh.example/pr-1)
**Salary:** £35,000 - £40,000 per annum

- **[Geopolitical Intern](https://cfr.example/intern)** | Council on Foreign Relations | New York, NY | Internship | $20/hour
2. Trust & Safety Analyst - TikTok - Remote - Posted yesterday
- Bullet prose that is not a job
"""
    jobs = ImportSource.parse(text, origin="md")
    titles = [j.title for j in jobs]
    assert titles == ["Political Risk Analyst", "Geopolitical Intern", "Trust & Safety Analyst"]
    pr, intern, ts = jobs
    assert pr.company == "Marsh" and pr.url == "https://marsh.example/pr-1" and pr.location == "London, UK"
    assert (pr.salary_min, pr.salary_max, pr.salary_currency, pr.salary_period) == (
        35000,
        40000,
        "GBP",
        "year",
    )
    assert intern.company == "Council on Foreign Relations" and intern.location == "New York, NY"
    assert intern.employment_type == "internship" and "internship" in intern.tags
    assert (intern.salary_min, intern.salary_period) == (20, "hour")
    assert ts.company == "TikTok" and ts.remote is True and ts.location == "Remote" and ts.posted_at


def test_markdown_table() -> None:
    text = """| Title | Company | Location | Link | Posted |
|---|---|---|---|---|
| [KYC Analyst](https://bank.example/kyc) | Citi | New York, NY | | 2026-10-20 |
| Sanctions Associate | HSBC | London | https://hsbc.example/s | Oct 1, 2026 |
"""
    jobs = ImportSource.parse(text, origin="table")
    assert [(j.title, j.company, j.url) for j in jobs] == [
        ("KYC Analyst", "Citi", "https://bank.example/kyc"),
        ("Sanctions Associate", "HSBC", "https://hsbc.example/s"),
    ]
    assert jobs[1].posted_at == "2026-10-01"


def test_plain_label_blocks() -> None:
    text = "Title: Investigator\nCompany: NYC DOI\nURL: https://doi.example/1\n\nNote: this line is prose.\n"
    jobs = ImportSource.parse(text)
    assert len(jobs) == 1 and jobs[0].company == "NYC DOI" and jobs[0].url == "https://doi.example/1"


# ----------------------------------------------------------------------------------------------- csv
def test_csv_export() -> None:
    jobs = ImportSource.parse(read("sample_export.csv"), origin="sheet")
    check(jobs, "sheet")
    kh, bz, cfr = jobs
    assert kh.source_id == "sheet:KH-1" and kh.location == "Washington, DC" and kh.posted_at == "2026-10-20"
    assert (kh.salary_min, kh.salary_max, kh.salary_period) == (70000, 85000, "year")
    assert bz.salary_currency == "GBP" and (bz.salary_min, bz.salary_max) == (32000, 38000)
    assert bz.employment_type == "full-time" and bz.posted_at == "2026-10-02"
    assert re.fullmatch(r"sheet:[0-9a-f]{12}", bz.source_id)  # empty Job ID -> hash
    assert cfr.posted_at is None and cfr.salary_period == "hour" and "internship" in cfr.tags
    assert [j.uid for j in ImportSource.parse(read("sample_export.csv"), origin="sheet", fmt="csv")] == [
        j.uid for j in jobs
    ]
    tsv = "title\tcompany\turl\nAnalyst\tAcme\thttps://acme.example/1\n"
    assert ImportSource.parse(tsv, fmt="tsv")[0].company == "Acme"
    assert ImportSource.parse(tsv)[0].company == "Acme"


# ----------------------------------------------------------------------------------------------- robustness
@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "   ",
        "{not json",
        "just some words, nothing else",
        42,
        [],
        {},
        [None, 1, "x"],
        b"\xff\xfe",
        {"a": 1},
    ],
)
def test_garbage_payloads_yield_nothing(payload: object) -> None:
    assert ImportSource.parse(payload) == []


def test_one_bad_record_does_not_sink_the_rest() -> None:
    records = [
        {"title": "Good One", "company": "Acme", "url": "https://acme.example/1"},
        {"title": {"weird": object()}, "company": "Broken"},
        {"title": "Good Two", "company": "Acme", "url": "https://acme.example/2", "posted": object()},
        {"title": "No company or url"},
    ]
    assert [j.title for j in ImportSource.parse(records)] == ["Good One", "Good Two"]


def test_parse_file(tmp_path: Path) -> None:
    p = tmp_path / "jobs.json"
    p.write_text(read("workopia_job_tool.json"), encoding="utf-8")
    assert len(ImportSource.parse_file(p, origin="workopia")) == 4


# ----------------------------------------------------------------------------------------------- helpers
def test_detect_format() -> None:
    assert detect_format(read("indeed_connector_search.md")) == "md"
    assert detect_format(read("workopia_job_tool.json")) == "json"
    assert detect_format(read("sample_export.csv")) == "csv"
    assert detect_format(read("workopia_rendered_list.md")) == "md"
    assert detect_format('{"a": 1}\n{"b": 2}') == "jsonl"
    assert detect_format("hello, world") == "md"
    assert ImportSource.detect_format("title,company\nA,B") == "csv"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("FULL_TIME", "full-time"),
        ("fulltime", "full-time"),
        ("Full Time", "full-time"),
        (["part_time", "contract"], "part-time"),
        ("Contractor", "contract"),
        ("Temp to hire", "temporary"),
        ("Summer Internship", "internship"),
        ("Fellowship", "fellowship"),
        ("Per Diem", "per-diem"),
        ("Volunteer", "volunteer"),
        ("Permanent", "full-time"),
        ("Onsite", None),
        ("N/A", None),
        (None, None),
        (True, None),
    ],
)
def test_normalize_employment_type(raw: object, want: str | None) -> None:
    assert normalize_employment_type(raw) == want


def test_small_normalisers() -> None:
    assert normalize_period("YEARLY") == "year" and normalize_period("hourly") == "hour"
    assert normalize_period("per month") == "month" and normalize_period("biweek") is None
    assert remote_from_value("Remote") is True and remote_from_value("Hybrid work") is False
    assert remote_from_value("On-site") is False and remote_from_value(False) is None
    assert remote_from_value(["Full-time", "Remote"]) is True and remote_from_value("") is None
    assert salary_from_text("USD 60,000 - 70,000 per year") == (60000, 70000, "USD", "year")
    assert salary_from_text("$85,000.00 - $150,000.00") == (85000, 150000, "USD", "year")
    assert salary_from_text("US$120k to US$175k annually")[:3] == (120000, 175000, "USD")
    assert salary_from_text("Competitive") == (None, None, None, None)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("July 31, 2026", "2026-07-31"),
        ("Posted on October 23, 2026", "2026-10-23"),
        ("Posted Oct 21", "2026-10-21"),
        ("Dec 30", "2025-12-30"),  # a future month/day means last year
        ("21 Oct 2026", "2026-10-21"),
        ("3 days ago", "2026-10-22"),
        ("Posted 30+ days ago", "2026-09-25"),
        ("Just posted", "2026-10-25"),
        ("2026-10-20", "2026-10-20"),
        ("N/A", None),
        ("sometime", None),
    ],
)
def test_parse_date(raw: str, want: str | None) -> None:
    assert parse_date(raw, now=NOW) == want


def test_text_cleanup() -> None:
    md = "**Company**\n-----------\n\n#### **Requisition ID**\n\n* Track \\_sanctions\\_\n* See [docs](https://x.example)"
    assert md_to_text(md) == "Company\n\nRequisition ID\n\n- Track _sanctions_\n- See docs"
    assert clean_description("<p>Hello <b>world</b></p>") == "Hello world"
    assert clean_description(["one", "two"]) == "- one\n- two"
    assert clean_description(None) == ""


def test_job_from_record_prefix_and_none() -> None:
    assert job_from_record({"company": "x"}, source="s", id_prefix="p") is None
    j = job_from_record({"title": "[T](https://t.example)", "company": "C"}, source="s", id_prefix=None)
    assert j is not None and j.title == "T" and j.url == "https://t.example" and ":" not in j.source_id
