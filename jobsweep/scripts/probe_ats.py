#!/usr/bin/env python3
"""Research helper: find and verify an employer's public ATS job board.

Usage:
  probe_ats.py guess "Company Name" [slug ...]     # try slug variants on greenhouse/lever/ashby/smartrecruiters/workable/recruitee
  probe_ats.py check ATS BOARD                     # e.g. check greenhouse dataminr | check workday rb/wd5/FRS
  probe_ats.py url https://company.com/careers     # fetch a careers page and report ATS links/embeds found in it

Prints one line per hit: "<ats> <board> <open_jobs> <sample titles>". Exit 0 always (research tool).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata

import httpx

UA = "jobsweep-research/0.1 (+https://github.com/gildedlily720/gradcafe_data)"
client = httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": UA})


def slug_variants(name: str) -> list[str]:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"&", " and ", base)
    words = re.findall(r"[a-z0-9]+", base)
    stop = {"inc", "llc", "ltd", "plc", "the", "co", "corp", "corporation", "company", "group", "holdings"}
    core = [w for w in words if w not in stop] or words
    cands = [
        "".join(core), "-".join(core), "".join(words), "-".join(words), core[0] if core else "",
        "".join(w[0] for w in core) if len(core) > 1 else "",
    ]
    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def check(ats: str, board: str) -> tuple[int | None, list[str]]:
    try:
        if ats == "greenhouse":
            r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
            if r.status_code != 200:
                return None, []
            jobs = r.json().get("jobs", [])
            return len(jobs), [j["title"] for j in jobs[:4]]
        if ats == "lever":
            host = "api.eu.lever.co" if board.startswith("eu:") else "api.lever.co"
            r = client.get(f"https://{host}/v0/postings/{board.removeprefix('eu:')}?mode=json")
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None, []
            jobs = r.json()
            return len(jobs), [j["text"] for j in jobs[:4]]
        if ats == "ashby":
            r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
            if r.status_code != 200:
                return None, []
            jobs = r.json().get("jobs", [])
            return len(jobs), [j["title"] for j in jobs[:4]]
        if ats == "smartrecruiters":
            r = client.get(f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=5")
            if r.status_code != 200:
                return None, []
            d = r.json()
            if not d.get("totalFound"):
                return 0 if "totalFound" in d else None, []
            return d["totalFound"], [j["name"] for j in d.get("content", [])[:4]]
        if ats == "workable":
            r = client.post(f"https://apply.workable.com/api/v3/accounts/{board}/jobs", json={"query": ""})
            if r.status_code != 200:
                return None, []
            d = r.json()
            return d.get("total", len(d.get("results", []))), [j["title"] for j in d.get("results", [])[:4]]
        if ats == "recruitee":
            r = client.get(f"https://{board}.recruitee.com/api/offers/")
            if r.status_code != 200:
                return None, []
            jobs = r.json().get("offers", [])
            return len(jobs), [j["title"] for j in jobs[:4]]
        if ats == "workday":
            tenant, wd, site = board.split("/", 2)
            r = client.post(
                f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                json={"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": ""},
            )
            if r.status_code != 200:
                return None, []
            d = r.json()
            return d.get("total"), [j["title"] for j in d.get("jobPostings", [])[:4]]
        if ats == "breezy":
            r = client.get(f"https://{board}.breezy.hr/json")
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None, []
            return len(r.json()), [j["name"] for j in r.json()[:4]]
        if ats == "bamboohr":
            r = client.get(f"https://{board}.bamboohr.com/careers/list")
            if r.status_code != 200:
                return None, []
            jobs = r.json().get("result", [])
            return len(jobs), [j.get("jobOpeningName") for j in jobs[:4]]
        if ats == "personio":
            r = client.get(f"https://{board}.jobs.personio.de/xml")
            if r.status_code != 200 or "<position" not in r.text:
                return (0 if r.status_code == 200 and "<workzag-jobs" in r.text else None), []
            return r.text.count("<position>"), re.findall(r"<name>([^<]+)</name>", r.text)[:4]
        if ats == "paylocity":
            r = client.get(f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{board}")
            if r.status_code != 200:
                return None, []
            jobs = r.json().get("jobs", [])
            return len(jobs), [j.get("title") for j in jobs[:4]]
        if ats == "pinpoint":
            r = client.get(f"https://{board}.pinpointhq.com/postings.json")
            if r.status_code != 200:
                return None, []
            jobs = r.json().get("data", [])
            return len(jobs), [j.get("title") for j in jobs[:4]]
        if ats == "rippling":
            r = client.get(f"https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs")
            if r.status_code != 200:
                return None, []
            jobs = r.json() if isinstance(r.json(), list) else r.json().get("results", [])
            return len(jobs), [j.get("name") or j.get("title") for j in jobs[:4]]
        if ats == "adp":
            cid = board.split("|")[0]
            r = client.get(
                "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions",
                params={"cid": cid, "lang": "en_US", "$top": 5},
            )
            if r.status_code != 200:
                return None, []
            d = r.json()
            reqs = d.get("jobRequisitions", [])
            total = d.get("meta", {}).get("totalNumber", len(reqs))
            return total, [j.get("requisitionTitle") for j in reqs[:4]]
        if ats == "oracle":
            host, site = board.split("/", 1)
            r = client.get(
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                params={"onlyData": "true", "expand": "requisitionList",
                        "finder": f"findReqs;siteNumber={site},limit=5"},
            )
            if r.status_code != 200:
                return None, []
            items = r.json().get("items", [])
            if not items:
                return 0, []
            return items[0].get("TotalJobsCount"), [j.get("Title") for j in items[0].get("requisitionList", [])[:4]]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        print(f"  ! {ats} {board}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return None, []


ATS_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workday", r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("breezy", r"([a-z0-9-]+)\.breezy\.hr"),
    ("bamboohr", r"([a-z0-9-]+)\.bamboohr\.com"),
    ("personio", r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("paylocity", r"recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/(?:All|List)/([0-9a-f-]{36})"),
    ("adp", r"workforcenow\.adp\.com/[^\"']*cid=([0-9a-f-]{36})"),
    ("ultipro", r"recruiting2?\.ultipro\.com/([A-Z0-9]+)/JobBoard/([0-9a-f-]{36})"),
    ("oracle", r"([a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([A-Za-z0-9_]+)"),
    ("pinpoint", r"([a-z0-9-]+)\.pinpointhq\.com"),
    ("rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("jobvite", r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)"),
    ("teamtailor", r"([a-z0-9-]+)\.teamtailor\.com"),
    ("icims", r"([a-z0-9-]+)\.icims\.com"),
    ("taleo", r"([a-z0-9-]+)\.taleo\.net"),
    ("successfactors", r"(career\d*\.successfactors\.(?:com|eu))"),
    ("jazzhr", r"([a-z0-9-]+)\.applytojob\.com"),
]


def scan_url(url: str) -> list[tuple[str, str]]:
    try:
        r = client.get(url)
    except httpx.HTTPError as exc:
        print(f"  ! fetch failed: {exc}")
        return []
    text = str(r.url) + "\n" + r.text
    found: list[tuple[str, str]] = []
    for ats, pat in ATS_PATTERNS:
        for m in re.finditer(pat, text):
            groups = [g for g in m.groups() if g]
            board = "/".join(groups) if ats in ("workday", "ultipro", "oracle") else groups[0]
            if (ats, board) not in found and board.lower() not in ("embed", "v1", "jobs", "www"):
                found.append((ats, board))
    print(f"final URL: {r.url} (HTTP {r.status_code})")
    return found


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "check" and len(argv) >= 3:
        n, titles = check(argv[1], argv[2])
        print(f"{argv[1]} {argv[2]} {n if n is not None else 'MISS'} {json.dumps(titles)}")
    elif cmd == "guess":
        name = argv[1]
        slugs = argv[2:] or slug_variants(name)
        for slug in slugs:
            for ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"):
                n, titles = check(ats, slug)
                if n is not None and (n > 0 or ats in ("greenhouse", "lever", "ashby")):
                    print(f"{ats} {slug} {n} {json.dumps(titles)}")
    elif cmd == "url":
        for ats, board in scan_url(argv[1]):
            n, titles = check(ats, board) if ats not in ("icims", "taleo", "successfactors", "jazzhr", "jobvite",
                                                        "teamtailor", "ultipro") else (None, [])
            print(f"{ats} {board} {n if n is not None else '-'} {json.dumps(titles)}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
