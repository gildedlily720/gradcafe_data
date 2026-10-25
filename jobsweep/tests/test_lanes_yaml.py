"""Data tests for catalog/lanes.yaml and catalog/profile.yaml (the lanes taxonomy).

These tests use a tiny stand-in matcher (whole-word / raw-regex, title + phrases, exclude veto) so they do
not depend on taxonomy.py. An extra integration test runs against the real classifier when it exists.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "jobsweep" / "catalog"
LANES_PATH = CATALOG / "lanes.yaml"
PROFILE_PATH = CATALOG / "profile.yaml"
VALIDATOR_PATH = ROOT / "scripts" / "validate_catalog.py"

TERM_LISTS = ("title", "phrases", "context", "exclude")
# (min, max) list sizes per lane; see the task brief for the lanes taxonomy.
SIZE_LIMITS = {"title": (6, 20), "phrases": (8, 30), "context": (5, 15), "queries": (3, 8)}
# Words too generic to be a title term on their own (they belong in `context`).
GENERIC_TITLE_TERMS = {
    "analyst",
    "analysts",
    "research",
    "researcher",
    "risk",
    "policy",
    "security",
    "intelligence",
    "associate",
    "assistant",
    "specialist",
    "consultant",
    "coordinator",
    "manager",
    "officer",
}

GROUP_NAMES = {
    "specialty-insurance": "Specialty insurance (underwriting and broking)",
    "intelligence-security": "Intelligence and security",
    "investigations-dd": "Investigations and due diligence",
    "geopolitical-risk": "Geopolitical and political risk",
    "sanctions-fincrime": "Sanctions, export controls and financial crime",
    "trust-safety": "Trust and safety and information integrity",
    "policy-research": "Policy and research",
    "strategic-comms": "Strategic communications",
    "government": "Government",
    "entry-routes": "Entry routes",
    "earlier-lanes": "Earlier lanes, not in the current sweeps",
}

# Exact `agency` strings seen in NYC Open Data kpav-sd4t (NYC Jobs) and the citywide payroll dataset
# (same NYCAPS names), 2026-10-25.
KNOWN_NYC_AGENCIES = {
    "BRONX DISTRICT ATTORNEY",
    "BUSINESS INTEGRITY COMMISSION",
    "CAMPAIGN FINANCE BOARD",
    "CIVILIAN COMPLAINT REVIEW BD",
    "COMMISSION ON RACIAL EQUITY",
    "CONFLICTS OF INTEREST BOARD",
    "CONSUMER AND WORKER PROTECTION",
    "DEPARTMENT OF CITY PLANNING",
    "DEPARTMENT OF CORRECTION",
    "DEPARTMENT OF FINANCE",
    "DEPARTMENT OF INVESTIGATION",
    "DEPARTMENT OF PROBATION",
    "DEPARTMENT OF TRANSPORTATION",
    "DISTRICT ATTORNEY KINGS COUNTY",
    "DISTRICT ATTORNEY QNS COUNTY",
    "DISTRICT ATTORNEY RICHMOND COU",
    "DISTRICT ATTORNEY-MANHATTAN",
    "DISTRICT ATTORNEY-SPECIAL NARC",
    "HOUSING PRESERVATION & DVLPMNT",
    "HRA/DEPT OF SOCIAL SERVICES",
    "HUMAN RIGHTS COMMISSION",
    "LAW DEPARTMENT",
    "MAYORS OFFICE OF CONTRACT SVCS",
    "OFFICE OF CRIMINAL JUSTICE",
    "OFFICE OF EMERGENCY MANAGEMENT",
    "OFFICE OF MANAGEMENT & BUDGET",
    "OFFICE OF RACIAL EQUITY",
    "OFFICE OF THE COMPTROLLER",
    "OFFICE OF THE MAYOR",
    "POLICE DEPARTMENT",
    "PUBLIC ADVOCATE",
}
KNOWN_HIRING_PATHS = {
    "public",
    "fed",
    "graduates",
    "student",
    "vet",
    "mspouse",
    "disability",
    "native",
    "peace",
}


# ---------------------------------------------------------------------------------------------------
# helpers


@lru_cache(maxsize=1)
def load_doc() -> dict[str, Any]:
    return yaml.safe_load(LANES_PATH.read_text(encoding="utf-8"))


def all_lanes() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(g, lane) for g in load_doc()["groups"] for lane in g["lanes"]]


def lane_by_id(lane_id: str) -> dict[str, Any]:
    for _, lane in all_lanes():
        if lane["id"] == lane_id:
            return lane
    raise KeyError(lane_id)


def is_regex(term: str) -> bool:
    return len(term) > 2 and term.startswith("/") and term.endswith("/")


@cache
def compile_term(term: str) -> re.Pattern[str]:
    """Case-insensitive whole-word/phrase pattern; '/.../' terms are raw regexes."""
    if is_regex(term):
        return re.compile(term[1:-1], re.IGNORECASE)
    body = r"\s+".join(re.escape(word) for word in term.split())
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.IGNORECASE)


def lanes_for_title(title: str) -> set[str]:
    """Stand-in classifier: a lane hits when a title/phrase term matches the title and no exclude does."""
    hits = set()
    for _, lane in all_lanes():
        terms = list(lane.get("title") or []) + list(lane.get("phrases") or [])
        if not any(compile_term(t).search(title) for t in terms):
            continue
        if any(compile_term(t).search(title) for t in lane.get("exclude") or []):
            continue
        hits.add(lane["id"])
    return hits


def load_validator():
    spec = importlib.util.spec_from_file_location("jobsweep_validate_catalog", VALIDATOR_PATH)
    assert spec and spec.loader, f"cannot load {VALIDATOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------------------------------
# structure


def test_ids_and_order_match_contract() -> None:
    validator = load_validator()
    found = {g["id"]: [lane["id"] for lane in g["lanes"]] for g in load_doc()["groups"]}
    assert list(found) == list(validator.LANES), "group ids/order differ from ARCHITECTURE.md 4.1"
    assert found == validator.LANES
    assert validator.check_lanes(LANES_PATH) == []


def test_document_shape_and_names() -> None:
    doc = load_doc()
    assert doc["version"] == 1
    assert {g["id"]: g["name"] for g in doc["groups"]} == GROUP_NAMES
    allowed = {
        "id",
        "name",
        "active",
        "title",
        "phrases",
        "context",
        "exclude",
        "queries",
        "usajobs",
        "nycjobs",
    }
    for _, lane in all_lanes():
        assert set(lane) <= allowed, f"{lane['id']}: unknown keys {set(lane) - allowed}"
        assert isinstance(lane["name"], str) and lane["name"].strip() == lane["name"] and lane["name"]
        assert isinstance(lane["active"], bool)
    assert lane_by_id("political-risk")["name"] == "Political risk (incl. credit and political risk)"
    assert lane_by_id("fine-art-specie")["name"] == "Fine art, specie and jewelers block"
    assert (
        lane_by_id("nyc-mayors-office")["name"]
        == "NYC Mayor's Office and City Hall policy and research roles"
    )
    assert lane_by_id("real-estate")["name"] == "Real estate (Marx Realty internship pursuit, July 2026)"


def test_earlier_lanes_inactive_everything_else_active() -> None:
    for group in load_doc()["groups"]:
        expect = group["id"] != "earlier-lanes"
        assert group["active"] is expect, group["id"]
        for lane in group["lanes"]:
            assert lane["active"] is expect, lane["id"]


def test_active_lanes_have_title_phrases_queries() -> None:
    for group, lane in all_lanes():
        if not (group["active"] and lane["active"]):
            continue
        for key in ("title", "phrases", "queries"):
            assert lane.get(key), f"{lane['id']}: empty {key}"


@pytest.mark.parametrize("key", sorted(SIZE_LIMITS))
def test_list_sizes(key: str) -> None:
    lo, hi = SIZE_LIMITS[key]
    bad = {
        lane["id"]: len(lane.get(key) or [])
        for _, lane in all_lanes()
        if not lo <= len(lane.get(key) or []) <= hi
    }
    assert not bad, f"{key} must have {lo}-{hi} entries: {bad}"


# ---------------------------------------------------------------------------------------------------
# terms


def test_every_term_compiles_and_is_well_formed() -> None:
    for _, lane in all_lanes():
        for kind in TERM_LISTS:
            terms = lane.get(kind)
            assert isinstance(terms, list), f"{lane['id']}.{kind} must be a list"
            for term in terms:
                where = f"{lane['id']}.{kind}: {term!r}"
                assert isinstance(term, str) and term, where
                assert term == term.strip() and "  " not in term, where
                compile_term(term)  # raises re.error on a bad regex
                if not is_regex(term):
                    # \b-based matchers need word characters at both ends.
                    assert re.match(r"\w", term) and re.search(r"\w$", term), where


def test_no_duplicate_terms_within_a_lane() -> None:
    for _, lane in all_lanes():
        seen: dict[str, str] = {}
        for kind in TERM_LISTS:
            for term in lane.get(kind) or []:
                key = " ".join(term.lower().split())
                assert key not in seen, f"{lane['id']}: {term!r} in {kind} duplicates {seen.get(key)}"
                seen[key] = kind
        queries = [q.lower() for q in lane["queries"]]
        assert len(queries) == len(set(queries)), f"{lane['id']}: duplicate queries"


def test_title_terms_are_not_generic() -> None:
    for _, lane in all_lanes():
        generic = [t for t in lane["title"] if t.lower() in GENERIC_TITLE_TERMS]
        assert not generic, f"{lane['id']}: generic title terms {generic} belong in context"


def test_case_sensitive_abbreviations() -> None:
    em = lane_by_id("emergency-management")
    core = compile_term(next(t for t in em["title"] if "CORE" in t))
    assert core.search("FEMA CORE Emergency Management Specialist")
    assert not core.search("Core Operations Analyst")
    dd = lane_by_id("due-diligence")
    edd = compile_term(next(t for t in dd["title"] if "EDD" in t))
    assert edd.search("EDD Analyst") and not edd.search("Edd Smith")


def test_queries_are_reasonable() -> None:
    for _, lane in all_lanes():
        for query in lane["queries"]:
            assert isinstance(query, str) and 3 <= len(query) <= 80 and query == query.strip(), lane["id"]


# ---------------------------------------------------------------------------------------------------
# source mappings


def test_usajobs_mappings() -> None:
    with_usajobs = 0
    for _, lane in all_lanes():
        mapping = lane.get("usajobs")
        if mapping is None:
            continue
        with_usajobs += 1
        where = lane["id"]
        assert set(mapping) <= {
            "keywords",
            "series",
            "hiring_path",
            "grade_low",
            "grade_high",
            "organization",
        }, where
        assert mapping.get("keywords") and all(isinstance(k, str) and k for k in mapping["keywords"]), where
        for code in mapping.get("series") or []:
            assert isinstance(code, str) and re.fullmatch(r"\d{4}", code), f"{where}: series {code!r}"
        assert set(mapping.get("hiring_path") or []) <= KNOWN_HIRING_PATHS, where
        for key in ("grade_low", "grade_high"):
            if key in mapping:
                assert isinstance(mapping[key], str) and re.fullmatch(r"\d{2}", mapping[key]), where
    assert with_usajobs >= 30

    pathways = lane_by_id("federal-pathways")["usajobs"]
    assert pathways["series"] == ["0130", "0131", "0132", "0301", "0343", "1801"]
    assert pathways["hiring_path"] == ["graduates"]
    assert (pathways["grade_low"], pathways["grade_high"]) == ("05", "09")

    em = lane_by_id("emergency-management")["usajobs"]
    assert em["organization"] == "HSCB"  # FEMA in the USAJOBS agency sub-element codelist
    assert {"Reservist", "CORE"} <= set(em["keywords"])
    assert "0089" in em["series"]

    investigator = lane_by_id("federal-investigator")["usajobs"]
    assert {"1801", "1810", "1811"} <= set(investigator["series"])
    assert "Investigator (Employee Benefits)" in investigator["keywords"]


def test_nycjobs_mappings_use_real_agency_strings() -> None:
    for _, lane in all_lanes():
        mapping = lane.get("nycjobs")
        if mapping is None:
            continue
        assert set(mapping) <= {"keywords", "agencies", "career_level"}, lane["id"]
        unknown = set(mapping.get("agencies") or []) - KNOWN_NYC_AGENCIES
        assert not unknown, f"{lane['id']}: unknown NYC agencies {unknown}"
    for lane_id in (
        "district-attorney",
        "nyc-integrity",
        "nyc-mayors-office",
        "nys-agencies",
        "emergency-management",
    ):
        assert lane_by_id(lane_id).get("nycjobs", {}).get("agencies"), lane_id
    assert "OFFICE OF EMERGENCY MANAGEMENT" in lane_by_id("emergency-management")["nycjobs"]["agencies"]
    assert "DEPARTMENT OF INVESTIGATION" in lane_by_id("nyc-integrity")["nycjobs"]["agencies"]
    assert "CIVILIAN COMPLAINT REVIEW BD" in lane_by_id("nyc-integrity")["nycjobs"]["agencies"]


# ---------------------------------------------------------------------------------------------------
# sanity table: real-world titles -> lane


SANITY_TITLES = [
    ("Political Risk Underwriter", "political-risk"),
    ("Underwriting Assistant, Credit & Political Risk", "political-risk"),
    ("Assistant Underwriter - Political Violence & Terrorism", "political-violence-terrorism"),
    ("Marine War Underwriter", "war-risk"),
    ("K&R Underwriter", "kidnap-ransom"),
    ("Crisis Management Underwriter (Kidnap & Ransom)", "kidnap-ransom"),
    ("Contingency Underwriter - Special Risks", "special-risks"),
    ("Trade Credit Underwriter", "trade-credit-surety"),
    ("Surety Underwriter Trainee", "trade-credit-surety"),
    ("Shipping Finance Analyst", "trade-credit-surety"),
    ("Analyst, Transactional Risk", "transactional-liability"),
    ("W&I Underwriting Assistant", "transactional-liability"),
    ("D&O Underwriter", "financial-lines"),
    ("Management Liability Underwriting Assistant", "financial-lines"),
    ("Fine Art & Specie Underwriter", "fine-art-specie"),
    ("Jewelers Block Underwriter", "fine-art-specie"),
    ("Intangible Assets Analyst", "intangibles"),
    ("Aviation Underwriter", "aviation"),
    ("Marine Cargo Underwriter", "marine-cargo"),
    ("P&I Claims Executive", "marine-cargo"),
    ("Reinsurance Broking Graduate", "reinsurance-broking"),
    ("Facultative Reinsurance Analyst", "reinsurance-broking"),
    ("SIU Investigator", "insurance-fraud-siu"),
    ("Special Investigations Unit Analyst", "insurance-fraud-siu"),
    ("Intelligence Analyst", "strategic-intelligence"),
    ("Threat Intelligence Response Analyst", "threat-intelligence"),
    ("Protective Intelligence Analyst", "protective-intelligence"),
    ("Executive Protection Coordinator", "protective-intelligence"),
    ("Travel Risk Analyst", "travel-risk-crisis"),
    ("GSOC Operator", "gsoc-watch"),
    ("Watch Officer", "gsoc-watch"),
    ("Physical Security Analyst", "corporate-security"),
    ("OSINT Analyst", "osint"),
    ("Counter Terrorism Analyst", "counterterrorism"),
    ("Regional Research Specialist - Middle East", "regional-research-specialist"),
    ("Middle East Analyst", "regional-research-specialist"),
    ("Geospatial Intelligence Analyst", "defense-intel-data"),
    ("Investigative Research Analyst", "corporate-intelligence"),
    ("Due Diligence Analyst", "due-diligence"),
    ("Beneficial Ownership Researcher", "beneficial-ownership"),
    ("Fraud Investigator", "fraud-investigations"),
    ("Forensic Accounting Associate", "forensic-investigations"),
    ("Analyst, White Collar & Investigations", "law-firm-analyst"),
    ("Litigation Paralegal", "litigation-case-analyst"),
    ("Mitigation Specialist", "public-defense-investigation"),
    ("Grants Compliance Associate", "nonprofit-grant-dd"),
    ("Country Risk Analyst", "country-sovereign-risk"),
    ("Geopolitical Analyst", "geopolitical-analysis"),
    ("Political Analyst", "political-forecasting"),
    ("Research Assistant, Geoeconomics Center", "geoeconomics"),
    ("Economic Security Research Associate", "economic-security"),
    ("Forced Labor Research Analyst", "supply-chain-forced-labor"),
    ("Risk Consultant", "risk-consulting"),
    ("Expert Network Associate", "primary-research-expert-networks"),
    ("Equity Research Associate", "investment-research-dd"),
    ("Sanctions Analyst", "sanctions"),
    ("Export Compliance Specialist", "export-controls"),
    ("AML Investigator", "aml-kyc"),
    ("KYC Analyst", "aml-kyc"),
    ("Transaction Monitoring Analyst", "aml-kyc"),
    ("Financial Intelligence Unit Analyst", "fincrime-intelligence"),
    ("Illicit Finance Analyst", "illicit-finance"),
    ("Blockchain Investigator", "crypto-investigations"),
    ("Compliance Investigator", "investigative-compliance"),
    ("Trade Surveillance Analyst", "market-surveillance"),
    ("Trust & Safety Analyst", "trust-safety-policy"),
    ("Content Policy Associate", "trust-safety-policy"),
    ("Safeguards Enforcement Analyst, Child Safety", "policy-enforcement-ai-safeguards"),
    ("AI Policy Research Assistant", "ai-policy-forecasting"),
    ("Election Integrity Program Associate", "election-integrity"),
    ("Disinformation Researcher", "disinformation"),
    ("Human Rights Research Assistant", "human-rights"),
    ("National Security Program Assistant", "national-security"),
    ("Foreign Policy Research Associate", "foreign-policy"),
    ("Research Associate, Americas Program", "think-tank-research"),
    ("Research Associate, Americas Program", "foreign-policy"),
    ("Associate Programme Officer", "international-orgs"),
    ("Legislative Analyst", "policy-legislative-analyst"),
    ("Technology Policy Analyst", "tech-regulatory-policy"),
    ("Criminal Justice Research Assistant", "criminal-justice-policy"),
    ("Policy and Advocacy Associate", "social-policy-advocacy"),
    ("City Planner I", "urban-planning-research"),
    ("Fact-Checker", "research-writing"),
    ("Strategic Communications Associate", "strategic-comms-advisory"),
    ("Crisis Communications Associate", "crisis-comms"),
    ("Government Affairs Associate", "public-government-affairs"),
    ("Research and Media Analyst", "reputation-media-analysis"),
    ("Media Monitoring Analyst", "insights-monitoring"),
    ("Crime Strategies Analyst", "district-attorney"),
    ("Investigative Analyst", "nyc-integrity"),
    ("Inspector General Investigator", "nyc-integrity"),
    ("Policy Analyst, Mayor's Office of Criminal Justice", "nyc-mayors-office"),
    ("Bank Examiner Trainee", "nys-agencies"),
    ("Research Analyst, Federal Reserve Bank of New York", "ny-fed"),
    ("FEMA CORE Emergency Management Specialist", "emergency-management"),
    ("Program Analyst (Pathways Recent Graduate)", "federal-pathways"),
    ("Investigator (Employee Benefits)", "federal-investigator"),
    ("Graduate Underwriter", "graduate-programs"),
    ("Analyst Development Program", "analyst-programs"),
    ("Herbert Scoville Jr. Peace Fellow", "fellowships"),
    ("Junior Analyst", "entry-level-analyst"),
    ("Analyst I, Due Diligence", "entry-level-analyst"),
    ("Investigator Level 1", "entry-level-analyst"),
    # earlier lanes are still classified
    ("Program Evaluation Associate", "program-evaluation"),
    ("Real Estate Acquisitions Analyst", "real-estate"),
]

SANITY_NEGATIVES = [
    ("SOC Analyst (Cyber Security Operations Center)", "gsoc-watch"),
    ("Security Operations Center Analyst - Cybersecurity", "gsoc-watch"),
    ("Security Operations Center Analyst - Cybersecurity", "corporate-security"),
    ("Network Operations Center Technician", "gsoc-watch"),
    ("Software Engineer, Trust & Safety", "trust-safety-policy"),
    ("Account Executive, Trust and Safety Solutions", "trust-safety-policy"),
    ("Business Intelligence Analyst", "strategic-intelligence"),
    ("Clinical Research Associate", "think-tank-research"),
    ("Child Protective Services Caseworker", "protective-intelligence"),
    ("Forensic Scientist (DNA)", "forensic-investigations"),
    ("Mortgage Credit Analyst", "trade-credit-surety"),
    ("Weather Forecaster", "political-forecasting"),
    ("Principal Investigator, Oncology", "corporate-intelligence"),
    ("Patient Advocacy Coordinator", "social-policy-advocacy"),
    ("IT Disaster Recovery Engineer", "emergency-management"),
    ("Transaction Monitoring Analyst", "insights-monitoring"),
    ("Graduate Research Assistant", "graduate-programs"),
    ("Senior Software Engineer, Defense Tech", "defense-intel-data"),
    ("Core Operations Analyst", "emergency-management"),
    ("Assistant District Attorney", "district-attorney"),
    ("Data Integrity Analyst", "trust-safety-policy"),
    ("Insurance Policy Analyst", "policy-legislative-analyst"),
]


GENERIC_TITLES = [
    "Software Engineer",
    "Account Executive",
    "Registered Nurse",
    "Data Analyst",
    "Financial Analyst",
    "Marketing Coordinator",
    "Customer Service Representative",
    "Security Guard",
    "Accountant",
    "HR Generalist",
    "Product Manager",
    "Sales Development Representative",
    "Special Assistant, First Deputy Commissioner",
    "Program Coordinator, Bureau of Queens Neighborhood Health",
    "Child Care Inspector, Bureau of Childcare",
    "Corporate Counsel, M&A",
    "Data Scientist, Safeguards",
    "Network Intelligence Analyst",
]


@pytest.mark.parametrize("title", GENERIC_TITLES)
def test_generic_titles_hit_no_domain_lane(title: str) -> None:
    modifiers = {"graduate-programs", "analyst-programs", "fellowships", "entry-level-analyst"}
    domain = lanes_for_title(title) - modifiers
    allowed = {"Network Intelligence Analyst": {"strategic-intelligence"}}.get(title, set())
    assert domain <= allowed, f"{title!r} hit {sorted(domain - allowed)}"


def test_sanity_table_covers_every_active_lane() -> None:
    covered = {lane_id for _, lane_id in SANITY_TITLES}
    active = {lane["id"] for group, lane in all_lanes() if group["active"] and lane["active"]}
    assert active <= covered, f"no sanity title for {sorted(active - covered)}"


@pytest.mark.parametrize(("title", "lane_id"), SANITY_TITLES)
def test_sanity_title_hits_lane(title: str, lane_id: str) -> None:
    assert lane_id in lanes_for_title(title)


@pytest.mark.parametrize(("title", "lane_id"), SANITY_NEGATIVES)
def test_sanity_title_does_not_hit_lane(title: str, lane_id: str) -> None:
    assert lane_id not in lanes_for_title(title)


# ---------------------------------------------------------------------------------------------------
# profile.yaml


def test_profile_yaml() -> None:
    from jobsweep.models import Profile

    data = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    names = {f.name for f in dataclasses.fields(Profile)}
    assert set(data) == names, f"profile.yaml keys differ from models.Profile: {set(data) ^ names}"
    profile = Profile.from_dict(data)
    assert profile.preferred_locations == ["New York"]
    assert profile.remote_ok is True
    assert profile.max_years_experience == 3
    assert profile.include_internships is False
    assert profile.include_unknown_seniority is True
    assert profile.min_score == 25
    assert profile.countries == ["US"]
    assert profile.strict_nyc_location
    assert profile.screen_qualifications


# ---------------------------------------------------------------------------------------------------
# integration with the real classifier (skipped until taxonomy.py exists)


def _real_taxonomy():
    if importlib.util.find_spec("jobsweep.taxonomy") is None:
        pytest.skip("jobsweep.taxonomy not written yet")
    try:
        from jobsweep.taxonomy import Taxonomy
    except ImportError as exc:  # half-written module
        pytest.skip(f"jobsweep.taxonomy not importable: {exc}")
    return Taxonomy.load(LANES_PATH, profile=PROFILE_PATH)


def test_real_taxonomy_loads_lanes() -> None:
    tax = _real_taxonomy()
    expected = [lane["id"] for _, lane in all_lanes()]
    assert set(tax.lanes) == set(expected)
    active = {lane.id for lane in tax.active_lanes()}
    assert active == {lane["id"] for group, lane in all_lanes() if group["active"] and lane["active"]}


@pytest.mark.parametrize(
    ("title", "description", "lane_id"),
    [
        (
            "Political Risk Underwriter",
            "Join our credit and political risk team at Lloyd's covering contract frustration and expropriation.",
            "political-risk",
        ),
        ("OSINT Analyst", "Conduct open source intelligence investigations using geolocation.", "osint"),
        ("Sanctions Analyst", "Screen against the SDN List and research sanctions evasion.", "sanctions"),
    ],
)
def test_real_taxonomy_classifies_clear_titles(title: str, description: str, lane_id: str) -> None:
    tax = _real_taxonomy()
    from jobsweep.models import Job

    job = Job(
        source="test",
        source_id=lane_id,
        title=title,
        company="Example Co",
        url="https://example.com",
        location="New York, NY",
        description=description,
    )
    tax.classify(job)
    assert lane_id in job.lanes
