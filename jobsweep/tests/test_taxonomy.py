"""Tests for jobsweep.taxonomy (lanes loading, term matching, classification/scoring, search plans)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobsweep import taxonomy as taxmod
from jobsweep.models import Employer, Job, Profile, SearchQuery
from jobsweep.taxonomy import (
    Taxonomy,
    compile_term,
    load_profile,
    normalize_text,
    parse_years_required,
)

FIX = Path(__file__).parent / "fixtures" / "taxonomy"
LANES = FIX / "lanes_min.yaml"
PROFILE = FIX / "profile_min.yaml"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

BEAZLEY = Employer(
    key="beazley",
    name="Beazley",
    lanes=["political-risk", "war-risk", "kidnap-ransom"],
    groups=["specialty-insurance"],
)
KROLL = Employer(
    key="kroll", name="Kroll", lanes=["due-diligence", "corporate-intelligence"], groups=["investigations-dd"]
)
DATAMINR = Employer(
    key="dataminr",
    name="Dataminr",
    lanes=["threat-intelligence", "gsoc-watch"],
    groups=["intelligence-security"],
)
MARKEL = Employer(key="markel", name="Markel", lanes=[], groups=["specialty-insurance"])

CYBER_DESC = (
    "Monitor SIEM alerts in Splunk, triage EDR detections and malware, investigate phishing, tune firewall "
    "rules and support threat hunting. 24/7 monitoring with incident escalation."
)
PHYSICAL_DESC = (
    "Staff our global security operations center: 24/7 monitoring of alarms and CCTV, emergency "
    "notifications to travelers, situational awareness reports and incident escalation to executives."
)
INSURER_BOILERPLATE = (
    "Beazley is a specialist insurer. Our political risk insurance, contract frustration and Joint War "
    "Committee listed areas cover is placed at Lloyd's through the London market. Kidnap and ransom insurance "
    "and K&R insurance complete the portfolio."
)


@pytest.fixture(scope="module")
def tax() -> Taxonomy:
    return Taxonomy.load(LANES, profile=PROFILE)


def make(title: str, description: str = "", company: str = "Acme", **kw) -> Job:
    return Job(
        source=kw.pop("source", "test"),
        source_id=kw.pop("source_id", "1"),
        title=title,
        company=company,
        url="https://example.com/job",
        description=description,
        **kw,
    )


def run(tax: Taxonomy, title: str, description: str = "", employer: Employer | None = None, **kw) -> Job:
    return tax.classify(make(title, description, **kw), employer, now=NOW)


# =====================================================================================================
# loading
# =====================================================================================================


def test_load_structure(tax: Taxonomy) -> None:
    assert [g.id for g in tax.groups][:3] == [
        "specialty-insurance",
        "intelligence-security",
        "investigations-dd",
    ]
    assert tax.groups[-1].id == "earlier-lanes" and tax.groups[-1].active is False
    lane = tax.lane("war-risk")
    assert lane is not None and lane.group == "specialty-insurance" and lane.active
    assert "Joint War Committee" in lane.phrases and lane.exclude == ["warehouse"]
    assert tax.lane("no-such-lane") is None
    assert tax.lane("political-risk").usajobs == {
        "keywords": ["political risk insurance"],
        "series": [1101, "1160"],
    }
    assert tax.lane("nyc-integrity").nycjobs["agencies"] == ["DEPARTMENT OF INVESTIGATION"]
    assert len(tax.lanes) == sum(len(g.lanes) for g in tax.groups)
    assert tax.profile.max_years_experience == 3


def test_active_lanes_skip_inactive_lanes_and_groups(tax: Taxonomy) -> None:
    ids = [lane.id for lane in tax.active_lanes()]
    assert "regional-research-specialist" not in ids  # lane inactive
    assert "real-estate" not in ids  # group inactive
    assert "political-risk" in ids and "graduate-programs" in ids
    assert tax.is_active("political-risk") and not tax.is_active("real-estate")


def test_profile_sources(tmp_path: Path) -> None:
    assert Taxonomy.load(LANES, profile=Profile(max_years_experience=1)).profile.max_years_experience == 1
    nested = load_profile(FIX / "profile_nested.yaml")
    assert nested.max_years_experience == 1
    assert nested.include_internships is True
    assert nested.preferred_locations == ["London"]


def test_profile_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "me.yaml"
    override.write_text("max_years_experience: 7\n", encoding="utf-8")
    monkeypatch.setattr(taxmod, "PROFILE_PATH", PROFILE)
    monkeypatch.setenv("JOBSWEEP_PROFILE", str(override))
    prof = load_profile()
    assert prof.max_years_experience == 7  # from the override
    assert prof.preferred_locations == ["New York", "Washington", "Remote", "London"]  # from the base file
    monkeypatch.setenv("JOBSWEEP_PROFILE", str(tmp_path / "missing.yaml"))
    assert load_profile().max_years_experience == 3  # missing override -> base file


def test_from_dict_tolerates_malformed_entries() -> None:
    doc = {
        "groups": [
            {
                "id": "g",
                "lanes": [
                    {"name": "no id"},
                    {"id": "a", "title": "single string", "phrases": None},
                    {"id": "a", "title": ["duplicate"]},
                    {"id": "b", "title": ["/([unclosed/", "ok term"]},
                ],
            },
            "junk",
        ]
    }
    t = Taxonomy.from_dict(doc, profile=Profile())
    assert list(t.lanes) == ["a", "b"]
    assert t.lane("a").title == ["single string"] and t.lane("a").phrases == []
    job = t.classify(make("Ok Term specialist"), now=NOW)  # the broken regex is skipped, not fatal
    assert job.lanes == ["b"]


def test_to_dict_shape(tax: Taxonomy) -> None:
    d = tax.to_dict()
    assert set(d) == {"groups"}
    for g in d["groups"]:
        assert set(g) == {"id", "name", "active", "lanes"}
        for lane in g["lanes"]:
            assert set(lane) == {"id", "name", "active", "group", "queries"}
            assert lane["group"] == g["id"]
    groups = {g["id"]: g for g in d["groups"]}
    assert groups["earlier-lanes"]["active"] is False
    lanes = {lane["id"]: lane for g in d["groups"] for lane in g["lanes"]}
    assert lanes["regional-research-specialist"]["active"] is False
    assert lanes["war-risk"]["queries"] == ["war risk underwriter", "marine war underwriting assistant"]
    json.dumps(d)


# =====================================================================================================
# term matching
# =====================================================================================================


@pytest.mark.parametrize(
    ("term", "text", "hit"),
    [
        ("trust & safety", "Trust and Safety Analyst", True),
        ("trust and safety", "Trust & Safety Analyst", True),
        ("trust and safety", "Trust&Safety", True),
        ("K&R", "K and R underwriting", True),
        ("political risk", "Political-Risk Analyst", True),
        ("political risk", "political\nrisk", True),
        ("political risk", "political risks", True),
        ("political risk", "political: risk", False),
        ("political risk", "geopolitical risk", False),
        ("policy", "foreign policies", True),
        ("Lloyd's", "Lloyd’s of London", True),
        ("Lloyd's", "Lloyds market", True),
        ("risk", "an asterisk", False),
        ("intern", "international", False),
        ("anti-money laundering", "anti money laundering", True),
        ("24/7 monitoring", "24 / 7 monitoring", True),
        ("strikes, riots and civil commotion", "Strikes, Riots & Civil Commotion", True),
        ("U.S. citizenship", "US citizenship required", True),
        ("OFAC", "ofac", False),
        ("OFAC", "OFAC screening", True),
        ("ADA", "Ada Lovelace", False),
        ("GS-0343", "GS-0343-07", True),
        ("AML/KYC", "AML / KYC analyst", True),
    ],
)
def test_plain_term_matching(term: str, text: str, hit: bool) -> None:
    assert (compile_term(term).search(text) is not None) is hit


@pytest.mark.parametrize(
    ("term", "text", "hit"),
    [
        ("/\\bK&R\\b/", "K&R Underwriter", True),
        ("/\\bK&R\\b/", "k&r", False),  # all-caps raw regex -> case-sensitive
        ("/(?i)\\bgsoc\\b/", "GSOC Operator", True),
        ("/\\bclass of 20\\d\\d\\b/", "Class of 2027 graduates", True),  # lower-case letters -> IGNORECASE
        ("/\\b(?-i:CORE)\\b/", "FEMA CORE employee", True),
        ("/\\b(?-i:CORE)\\b/", "our core values", False),
        ("/\\bGS-?0?[579]\\b/", "Analyst GS-07", True),
        ("/\\bVASPs?\\b/", "regulated VASPs", True),
        ("/\\bgraduat(?:e|ing) (?:in|by) 20\\d\\d\\b/", "graduating by 2027", True),
        ("/\\bgraduat(?:e|ing) (?:in|by) 20\\d\\d\\b/", "undergraduate in 2027", False),
    ],
)
def test_raw_regex_terms(term: str, text: str, hit: bool) -> None:
    assert (compile_term(term).search(text) is not None) is hit


CORPUS = [
    "Political-risk and war risks cover at Lloyd’s; K & R; kidnap and ransom insurance.",
    "Trust & Safety / content moderation\nharmful content, community guidelines.",
    "Skills: research, analysis. Risk management; sanctions, compliance.",
    "The GSOC monitors CCTV 24/7 with incident escalation; SOC analysts handle SIEM alerts.",
    "Graduating in May 2027? Join the Class of 2027 graduate programme (rotational program).",
    "U.S. citizenship; GS-0343-07/09; Recent Graduates Program; promotion potential to GS-09.",
    "AML/KYC analyst - know-your-customer, Bank Secrecy Act, suspicious activity reports.",
    "Enhanced due diligence, adverse media and FCPA reviews for Kroll clients.",
    "Department of Investigation: corruption investigations into fraud, waste and abuse.",
    "Office of Foreign Assets Control (OFAC) Specially Designated Nationals SDN List screening.",
]


def _variants(raw: str) -> list[str]:
    out = [raw, raw.upper(), raw.lower(), raw.title(), f"Our {raw}s team", f"x{raw}", f"{raw}x"]
    out += [raw.replace(" ", "-"), raw.replace(" ", "\n"), raw.replace(" ", ". "), raw.replace(" ", ", ")]
    out += [raw.replace("&", "and"), raw.replace(" and ", " & "), raw.replace("-", " ")]
    return out


def _check_prefilters(terms: list) -> None:
    for term in terms:
        texts = list(CORPUS)
        if not term.regex:
            texts += _variants(term.raw)
        for text in texts:
            fast, slow = term.search(text), term.search_slow(text)
            assert (fast is None) == (slow is None), (term.raw, text, fast, slow)


def test_prefilters_agree_with_plain_regex(tax: Taxonomy) -> None:
    _check_prefilters(tax._terms)


def test_normalize_text() -> None:
    assert normalize_text("Lloyd’s – “x” y") == 'Lloyd\'s - "x" y'
    assert normalize_text("certiﬁed") == "certified"
    assert normalize_text("Ｋ＆Ｒ") == "K&R"  # full-width forms -> NFKC
    assert normalize_text(None) == "" and normalize_text("plain") == "plain"


# =====================================================================================================
# classification: lanes
# =====================================================================================================


def test_political_risk_underwriter_at_insurer(tax: Taxonomy) -> None:
    job = run(
        tax,
        "Political Risk Underwriting Assistant",
        "Support the political risk insurance team at Lloyd's: contract frustration, expropriation and "
        "non-honouring cover for banks. 1-2 years of experience in the London market.",
        BEAZLEY,
        company="Beazley",
        location="London, UK",
        posted_at="2026-10-24",
    )
    assert job.lanes[0] == "political-risk"
    assert job.groups[0] == "specialty-insurance"
    assert job.entry_level is True and job.years_required == 1
    assert job.score >= 80
    assert job.match_reasons[0] == "title: 'political risk' → political-risk"
    assert "location: London" in job.match_reasons
    assert "entry-level-analyst" in job.lanes  # entry-route modifier, ranked after the domain lane
    assert job.lanes.index("entry-level-analyst") > job.lanes.index("political-risk")
    assert list(job.lane_scores) == job.lanes


def test_kidnap_and_ransom(tax: Taxonomy) -> None:
    job = run(tax, "K&R Assistant Underwriter", "Kidnap and ransom insurance, wrongful detention.", BEAZLEY)
    assert job.lanes[0] == "kidnap-ransom"
    assert job.entry_level is True


def test_war_risk(tax: Taxonomy) -> None:
    job = run(
        tax, "Marine War Underwriter", "Joint War Committee listed areas, hull and cargo.", location="London"
    )
    assert job.lanes[0] == "war-risk"
    assert job.entry_level is None  # no level vocabulary, no years


def test_gsoc_operator(tax: Taxonomy) -> None:
    job = run(tax, "GSOC Operator", PHYSICAL_DESC, location="New York, NY")
    assert job.lanes[0] == "gsoc-watch"
    assert job.match_reasons[0] == "title: 'GSOC' → gsoc-watch"


def test_protective_intelligence_analyst(tax: Taxonomy) -> None:
    job = run(
        tax,
        "Protective Intelligence Analyst",
        "Assess concerning communications and fixated persons targeting protectees.",
        location="Menlo Park, CA",
    )
    assert job.lanes[0] == "protective-intelligence"
    assert job.entry_level is True


def test_cyber_soc_analyst_is_not_gsoc_or_corporate_security(tax: Taxonomy) -> None:
    job = run(tax, "SOC Analyst", CYBER_DESC + " Physical security teams are partners.")
    assert "gsoc-watch" not in job.lanes and "corporate-security" not in job.lanes
    assert job.score <= 15


def test_cyber_description_vetoes_gsoc_even_with_gsoc_title(tax: Taxonomy) -> None:
    cyber = run(tax, "Security Operations Center Analyst", CYBER_DESC)
    physical = run(tax, "Security Operations Center Analyst", PHYSICAL_DESC)
    assert "gsoc-watch" not in cyber.lanes
    assert any(r.startswith("cyber posting") for r in cyber.match_reasons)
    assert physical.lanes[0] == "gsoc-watch"


def test_sanctions_analyst(tax: Taxonomy) -> None:
    job = run(tax, "Sanctions Analyst", "OFAC sanctions screening. 3+ years of experience in compliance.")
    assert job.lanes[0] == "sanctions"
    assert job.years_required == 3 and job.entry_level is True
    assert "entry: 3 years required (max 3)" in job.match_reasons


def test_kyc_analyst(tax: Taxonomy) -> None:
    job = run(tax, "KYC Analyst", "Perform KYC refresh and customer due diligence on banking clients.")
    assert job.lanes[0] == "aml-kyc"
    assert job.groups[0] == "sanctions-fincrime"


def test_trust_and_safety_policy(tax: Taxonomy) -> None:
    job = run(tax, "Trust & Safety Policy Associate", "Write content policies on harmful content.")
    assert job.lanes[0] == "trust-safety-policy"
    assert job.entry_level is True


def test_trust_and_safety_engineer_is_excluded(tax: Taxonomy) -> None:
    job = run(tax, "Software Engineer, Trust & Safety", "Build content moderation tooling.")
    assert "trust-safety-policy" not in job.lanes
    assert job.score <= 15


def test_nyc_doi_investigator(tax: Taxonomy) -> None:
    job = run(
        tax,
        "Investigator",
        "Conduct corruption investigations into fraud, waste and abuse in city agencies.",
        company="Department of Investigation",
        source="nycjobs",
        location="New York, NY",
        extra={"career_level": "Entry-Level"},
    )
    assert job.lanes[0] == "nyc-integrity"
    assert job.groups == ["government"]
    assert job.entry_level is True
    assert "entry: level 'Entry-Level'" in job.match_reasons
    assert "location: New York" in job.match_reasons


def test_federal_pathways_gs7_0343(tax: Taxonomy) -> None:
    job = run(
        tax,
        "Management and Program Analyst (Recent Graduate)",
        "This position is part of the Pathways Recent Graduates Program with promotion potential.",
        company="Department of State",
        source="usajobs",
        grade="GS-07/09",
        series=["0343"],
        tags=["pathways", "recent-graduates"],
        location="Washington, DC",
    )
    assert job.lanes[0] == "federal-pathways"
    assert job.entry_level is True
    assert any(r.startswith("entry:") for r in job.match_reasons)
    assert job.score >= 75


def test_federal_grade_too_senior(tax: Taxonomy) -> None:
    job = run(
        tax, "Criminal Investigator", "Federal criminal investigations.", source="usajobs", grade="GS-12/13"
    )
    assert job.lanes[0] == "federal-investigator"
    assert job.entry_level is False
    assert "senior: grade GS-12" in job.match_reasons


def test_senior_director_at_kroll_is_not_entry(tax: Taxonomy) -> None:
    desc = "Lead enhanced due diligence and integrity due diligence engagements for clients."
    senior = run(tax, "Senior Director, Due Diligence", desc, KROLL, company="Kroll", location="New York, NY")
    junior = run(tax, "Analyst, Due Diligence", desc, KROLL, company="Kroll", location="New York, NY")
    assert senior.lanes[0] == "due-diligence"
    assert senior.entry_level is False
    assert "senior: 'Senior' in title" in senior.match_reasons
    assert junior.entry_level is True
    assert junior.score - senior.score >= 40


def test_accountant_at_specialty_insurer_gets_no_domain_lane(tax: Taxonomy) -> None:
    job = run(tax, "Accountant", INSURER_BOILERPLATE, BEAZLEY, company="Beazley", location="London")
    assert job.lanes == [] and job.groups == []
    assert job.score <= 15
    assert "off-domain title: 'Accountant'" in job.match_reasons


def test_research_associate_at_think_tank(tax: Taxonomy) -> None:
    job = run(
        tax,
        "Research Associate",
        "Support scholars with policy briefs and white papers.",
        company="Brookings Institution",
        location="Washington, DC",
    )
    assert job.lanes[0] == "think-tank-research"
    assert job.entry_level is True
    assert "location: Washington" in job.match_reasons


def test_lab_research_associate_is_excluded(tax: Taxonomy) -> None:
    job = run(tax, "Research Associate, Clinical Biology", "Run laboratory assays.")
    assert "think-tank-research" not in job.lanes


def test_exclude_vetoes_lane(tax: Taxonomy) -> None:
    job = run(tax, "Political Campaign Organizer", "Political risk of the campaign.")
    assert "political-risk" not in job.lanes


def test_ampersand_and_overlapping_terms_do_not_stack(tax: Taxonomy) -> None:
    a = run(tax, "Political Risk Underwriter")
    b = run(tax, "Credit & Political Risk Underwriter")
    c = run(tax, "Credit and Political Risk Underwriter")
    assert (
        a.lane_scores["political-risk"] == b.lane_scores["political-risk"] == c.lane_scores["political-risk"]
    )


# =====================================================================================================
# employer priors and off-domain titles
# =====================================================================================================


def test_employer_prior_on_generic_title(tax: Taxonomy) -> None:
    job = run(tax, "Analyst", "Join a fast-growing team.", DATAMINR, company="Dataminr")
    assert job.lanes and set(job.lanes) <= {"threat-intelligence", "gsoc-watch"}
    assert any(r.startswith("employer prior: Dataminr") for r in job.match_reasons)
    assert job.entry_level is True


def test_prior_lanes_capped_and_dropped_when_title_matches(tax: Taxonomy) -> None:
    emp = Employer(
        key="big",
        name="Big",
        lanes=["political-risk", "war-risk", "kidnap-ransom", "insurance-fraud-siu"],
        groups=["specialty-insurance"],
    )
    generic = run(tax, "Associate", "Join our team.", emp)
    assert len(generic.lanes) == 3
    titled = run(tax, "Sanctions Analyst", "Join our team.", emp)
    assert titled.lanes == ["sanctions"]


def test_off_domain_title_blocks_prior(tax: Taxonomy) -> None:
    desc = "Dataminr delivers threat intelligence, intelligence assessments and geopolitical threats alerts."
    job = run(tax, "Software Engineer", desc, DATAMINR, company="Dataminr")
    assert job.lanes == []
    assert job.score <= 15


def test_generalist_bank_role_gets_no_prior_lanes(tax: Taxonomy) -> None:
    bank = Employer(key="bank", name="Big Bank", lanes=["aml-kyc", "sanctions"], groups=["sanctions-fincrime"])
    desc = "Execute M&A transactions. All staff comply with anti-money laundering and sanctions policies."
    assert run(tax, "Investment Banking Analyst", desc, bank).lanes == []
    assert run(tax, "KYC Analyst", desc, bank).lanes[0] == "aml-kyc"


def test_implausible_title_with_domain_description(tax: Taxonomy) -> None:
    desc = (
        "Help customers use intelligence assessments, the threat landscape and geopolitical threats data. "
        "Dataminr alerts, OSINT briefings and monitoring."
    )
    job = run(tax, "Customer Success Manager", desc, DATAMINR, company="Dataminr")
    assert job.lanes == []


def test_group_prior_needs_lane_evidence(tax: Taxonomy) -> None:
    bare = run(tax, "Analyst", "Join our underwriting team at Lloyd's.", MARKEL, company="Markel")
    assert bare.lanes == []
    with_phrases = run(
        tax,
        "Analyst",
        "Cover for Joint War Committee listed areas; hull war at Lloyd's.",
        MARKEL,
        company="Markel",
    )
    assert with_phrases.lanes[0] == "war-risk"
    assert "employer prior: Markel → specialty-insurance" in with_phrases.match_reasons


def test_description_only_evidence_needs_more_than_one_phrase(tax: Taxonomy) -> None:
    one = run(tax, "Coordinator", "Experience with FCPA is a plus.")
    two = run(tax, "Coordinator", "Enhanced due diligence and adverse media research.")
    assert "due-diligence" not in one.lanes
    assert two.lanes[0] == "due-diligence"


# =====================================================================================================
# entry level / years
# =====================================================================================================


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("3+ years of experience in sanctions compliance", 3),
        ("A minimum of 5 years of relevant work experience", 5),
        ("At least two (2) years of relevant experience required", 2),
        ("5-7 years of experience in investigations", 5),
        ("3 to 5 years' experience in the London market", 3),
        ("Experience: minimum 4 years in a compliance role", 4),
        ("one year of specialized experience equivalent to the GS-07 level", 1),
        ("Bachelor's degree and 2 years of experience; or a Master's degree and 1 year of experience", 1),
        ("Candidates must be at least 18 years old.", None),
        ("For more than 20 years, our firm has advised clients.", None),
        ("Founded 25 years ago, we have offices worldwide.", None),
        ("You graduated within the last two years.", None),
        ("A 4-year degree is required.", None),
        ("This two-year fellowship program starts in June.", None),
        ("Our team has 30 years of combined experience.", None),
        ("", None),
    ],
)
def test_parse_years_required(text: str, years: int | None) -> None:
    assert parse_years_required(text) == years


def test_years_vs_profile_max(tax: Taxonomy) -> None:
    ok = run(tax, "Investigator", "3+ years of investigative experience.")
    too_many = run(tax, "Investigator", "5 years of investigative experience required.")
    assert ok.entry_level is True and ok.years_required == 3
    assert too_many.entry_level is False and too_many.years_required == 5
    assert "requires 5+ years (max 3)" in too_many.match_reasons


@pytest.mark.parametrize(
    ("title", "entry"),
    [
        ("Senior Analyst", False),
        ("Sr. Analyst", False),
        ("Lead Investigator", False),
        ("Director of Research", False),
        ("Head of Intelligence", False),
        ("VP, Sanctions Compliance", False),
        ("Vice President, Financial Crime", False),
        ("Associate Director, Due Diligence", False),
        ("Senior Associate", False),
        ("Managing Director", False),
        ("Chief of Staff", False),
        ("Partner, Investigations", False),
        ("Manager, Threat Intelligence", False),
        ("Executive Director", False),
        ("Associate General Counsel", False),
        ("Supervisory Criminal Investigator", False),
        ("Staff Software Engineer", False),
        ("Analyst III", False),
        ("Senior Fellow", False),
        ("Assistant to the Director", True),
        ("Staff Analyst", True),
        ("Analyst I", True),
        ("Analyst", True),
        ("Junior Researcher", True),
        ("Program Assistant", True),
        ("Research Coordinator", True),
        ("Graduate Trainee", True),
        ("Associate, Investigations", True),
        ("Analyst II", None),
        ("Investigator", None),
        ("Head Start Program Specialist", None),
        ("Case Manager", None),
    ],
)
def test_title_seniority(tax: Taxonomy, title: str, entry: bool | None) -> None:
    assert run(tax, title).entry_level is entry


def test_associate_with_bar_admission_is_lawyer_role(tax: Taxonomy) -> None:
    job = run(tax, "Litigation Associate", "J.D. and admission to the New York bar required.")
    assert job.entry_level is False
    assert "lawyer role: bar admission required" in job.match_reasons


def test_internships(tax: Taxonomy) -> None:
    job = run(tax, "Summer Intern, Political Risk", "Political risk insurance internship.")
    assert job.entry_level is False and "internship" in job.match_reasons
    tagged = run(tax, "Research Assistant", tags=["internship"])
    assert tagged.entry_level is False
    keen = Taxonomy.load(LANES, profile=Profile(include_internships=True))
    job2 = keen.classify(make("Summer Intern, Political Risk"), now=NOW)
    assert job2.entry_level is True and "entry: internship" in job2.match_reasons


def test_source_seniority_levels(tax: Taxonomy) -> None:
    assert run(tax, "Investigator", extra={"career_level": "Manager"}).entry_level is False
    assert run(tax, "Investigator", extra={"career_level": "Experienced (non-manager)"}).entry_level is None
    assert run(tax, "Investigator", seniority="Entry level").entry_level is True
    assert run(tax, "Investigator", seniority="Mid-Senior level").entry_level is None


def test_entry_route_only_scores_low(tax: Taxonomy) -> None:
    job = run(tax, "Graduate Programme 2027", "A rotational program across our business.", location="London")
    assert job.lanes == ["graduate-programs"]
    assert job.entry_level is True
    assert job.score <= 15
    assert job.match_reasons[0] == "no domain lane matched"


def test_entry_route_ranks_after_domain_lane(tax: Taxonomy) -> None:
    job = run(tax, "Graduate Analyst - Political Risk")
    assert job.lanes[0] == "political-risk"
    assert "graduate-programs" in job.lanes
    assert job.groups == ["specialty-insurance", "entry-routes"]


def test_graduate_underwriter_keeps_employer_prior(tax: Taxonomy) -> None:
    job = run(tax, "Graduate Underwriter", "Join the underwriting team.", BEAZLEY, company="Beazley")
    assert job.lanes[0] in BEAZLEY.lanes


def test_inactive_lane_is_labelled_but_ranked_last(tax: Taxonomy) -> None:
    only = run(tax, "Real Estate Analyst", "Commercial real estate and REITs.")
    assert only.lanes == ["real-estate"]
    both = run(tax, "Political Risk Analyst, Real Estate", "Commercial real estate.")
    assert both.lanes[0] == "political-risk" and "real-estate" in both.lanes


# =====================================================================================================
# location, recency, score
# =====================================================================================================


@pytest.mark.parametrize(
    ("location", "remote", "reason"),
    [
        ("New York, NY", None, "location: New York"),
        ("Brooklyn, New York", None, "location: New York"),
        ("New York (Hybrid)", None, "location: New York"),
        ("Albany, NY", None, "location: New York (state)"),
        ("Washington, D.C.", None, "location: Washington"),
        ("Arlington, VA", None, "location: Washington"),
        ("Baltimore, MD", None, "location: near Washington"),
        ("Seattle, Washington", None, None),
        ("Arlington, TX", None, None),
        ("London, England", None, "location: London"),
        ("London, Ontario", None, None),
        ("Manchester, UK", None, "location: London (UK)"),
        ("Remote - United States", None, "location: remote"),
        ("Chicago, IL", True, "location: remote"),
        ("Remote (Canada)", None, "location: remote (outside US/UK)"),
        ("Chicago, IL", None, None),
        ("Berlin; New York, NY", None, "location: New York"),
    ],
)
def test_location_fit(tax: Taxonomy, location: str, remote: bool | None, reason: str | None) -> None:
    job = run(tax, "Sanctions Analyst", location=location, remote=remote)
    loc_reasons = [r for r in job.match_reasons if r.startswith("location:")]
    assert loc_reasons == ([reason] if reason else [])


def test_location_and_recency_move_the_score(tax: Taxonomy) -> None:
    ny = run(tax, "Sanctions Analyst", location="New York, NY", posted_at="2026-10-24")
    chicago = run(tax, "Sanctions Analyst", location="Chicago, IL", posted_at="2026-10-24")
    stale = run(tax, "Sanctions Analyst", location="New York, NY", posted_at="2026-08-01")
    assert ny.score - chicago.score >= 10
    assert ny.score - stale.score >= 6
    assert "posted 1 day ago" in ny.match_reasons
    when = (NOW - timedelta(hours=1)).isoformat()
    assert "posted today" in run(tax, "Sanctions Analyst", posted_at=when).match_reasons


SAMPLES = [
    ("Political Risk Underwriting Assistant", INSURER_BOILERPLATE, BEAZLEY),
    ("Accountant", INSURER_BOILERPLATE, BEAZLEY),
    ("Senior Director, Due Diligence", "Enhanced due diligence. 10+ years of experience.", KROLL),
    ("", "", None),
    ("Analyst", "", DATAMINR),
    ("GSOC Operator", PHYSICAL_DESC, None),
    ("SOC Analyst", CYBER_DESC, None),
    ("Graduate Programme", "rotational program", None),
    ("Warehouse Associate", "Joint War Committee listed areas", None),
    ("Trust & Safety Policy Associate", "harmful content " * 50, None),
    ("Chef", "", None),
]


def test_score_bounds_and_no_domain_cap(tax: Taxonomy) -> None:
    for title, desc, emp in SAMPLES:
        for loc in ("New York, NY", "", "Remote"):
            job = run(tax, title, desc, emp, location=loc, posted_at="2026-10-25")
            assert 0.0 <= job.score <= 100.0
            domain = [x for x in job.lanes if tax.lane(x).group != "entry-routes"]
            if not domain:
                assert job.score <= 15.0, (title, job.score)
            assert len(job.match_reasons) <= 8
            assert all(isinstance(r, str) and r for r in job.match_reasons)
            assert len(set(job.match_reasons)) == len(job.match_reasons)
            assert list(job.lane_scores) == job.lanes
            assert job.groups == list(dict.fromkeys(tax.lane(x).group for x in job.lanes))


def test_lane_scores_sorted_within_domain(tax: Taxonomy) -> None:
    job = run(
        tax,
        "Sanctions and KYC Analyst",
        "OFAC sanctions screening, economic sanctions, KYC refresh, money laundering.",
    )
    domain = [x for x in job.lanes if tax.is_active(x) and tax.lane(x).group != "entry-routes"]
    scores = [job.lane_scores[x] for x in domain]
    assert scores == sorted(scores, reverse=True)
    assert set(domain) >= {"sanctions", "aml-kyc"}


def test_classification_is_deterministic(tax: Taxonomy) -> None:
    other = Taxonomy.load(LANES, profile=PROFILE)
    for title, desc, emp in SAMPLES:
        a = tax.classify(make(title, desc), emp, now=NOW).to_dict()
        b = other.classify(make(title, desc), emp, now=NOW).to_dict()
        c = tax.classify(make(title, desc), emp, now=NOW).to_dict()
        assert a == b == c


def test_reclassify_resets_fields(tax: Taxonomy) -> None:
    job = run(tax, "Political Risk Analyst", "3+ years of experience")
    assert job.lanes and job.years_required == 3
    job.title, job.description = "Chef", ""
    tax.classify(job, now=NOW)
    assert job.lanes == [] and job.lane_scores == {} and job.groups == [] and job.years_required is None


# =====================================================================================================
# title_signal
# =====================================================================================================


def test_title_signal(tax: Taxonomy) -> None:
    sig = tax.title_signal
    assert sig("Political Risk Underwriter") >= 8
    assert sig("GSOC Operator") >= 8
    assert sig("Analyst") < 8
    assert sig("Analyst", DATAMINR) >= 8
    assert sig("Underwriter", MARKEL) >= 8
    assert sig("Software Engineer", DATAMINR) < 8
    assert sig("Customer Success Manager", DATAMINR) < 8
    assert sig("Executive Assistant", BEAZLEY) < 8
    assert sig("SOC Analyst") < sig("GSOC Operator")
    assert sig("Senior Political Risk Underwriter") < sig("Political Risk Underwriter")
    assert sig("") == 0.0
    assert isinstance(sig("Analyst"), float)


# =====================================================================================================
# search_plan
# =====================================================================================================


@pytest.mark.parametrize("source", ["nycjobs", "nysjobs"])
def test_search_plan_feed_sources(tax: Taxonomy, source: str) -> None:
    plan = tax.search_plan(source=source, posted_within_days=7)
    assert len(plan) == 1
    q = plan[0]
    assert isinstance(q, SearchQuery)
    assert (q.keywords, q.extra, q.limit, q.posted_within_days) == ("*", {"feed": True}, 5000, 7)
    assert tax.search_plan(["war-risk"], source=source)[0].keywords == "*"


def test_search_plan_usajobs(tax: Taxonomy) -> None:
    plan = tax.search_plan(source="usajobs", posted_within_days=14)
    by_lane: dict[str, list[SearchQuery]] = {}
    for q in plan:
        by_lane.setdefault(q.lane, []).append(q)
    pr = by_lane["political-risk"]
    assert [q.keywords for q in pr] == ["political risk insurance"]
    assert pr[0].extra == {"series": ["1101", "1160"]}
    fp = by_lane["federal-pathways"]
    assert [q.keywords for q in fp] == ["Recent Graduate", "Pathways"]
    assert fp[0].extra == {
        "series": ["0343", "0132"],
        "hiring_path": ["graduates"],
        "grade_low": "05",
        "grade_high": "09",
    }
    # no usajobs mapping: generic queries only for government-ish groups
    assert [q.keywords for q in by_lane["sanctions"]] == ["sanctions analyst", "OFAC compliance analyst"]
    assert "threat-intelligence" in by_lane and "trust-safety-policy" in by_lane
    for lane in ("war-risk", "kidnap-ransom", "crisis-comms", "graduate-programs", "real-estate"):
        assert lane not in by_lane
    assert all(q.posted_within_days == 14 for q in plan)


def test_search_plan_generic_interleaves_and_dedupes(tax: Taxonomy) -> None:
    plan = tax.search_plan(source="adzuna", posted_within_days=30)
    with_queries = [lane.id for lane in tax.active_lanes() if lane.queries]
    first = [q.lane for q in plan[: len(with_queries)]]
    assert first == with_queries  # one query per lane before any lane gets a second one
    keywords = [q.keywords for q in plan]
    assert keywords.count("political risk underwriter") == 1
    assert next(q for q in plan if q.keywords == "political risk underwriter").lane == "political-risk"
    assert "real estate analyst" not in keywords and "regional analyst middle east" not in keywords
    assert all(q.lane and q.posted_within_days == 30 and q.extra == {} for q in plan)
    assert len(plan) == len({(q.keywords.lower(), json.dumps(q.extra)) for q in plan})


def test_search_plan_explicit_lanes(tax: Taxonomy) -> None:
    plan = tax.search_plan(["real-estate", "war-risk", "no-such-lane"], source="jobspy")
    assert [(q.lane, q.keywords) for q in plan] == [
        ("real-estate", "real estate analyst"),
        ("war-risk", "war risk underwriter"),
        ("war-risk", "marine war underwriting assistant"),
    ]
    assert tax.search_plan(["no-such-lane"], source="adzuna") == []


# =====================================================================================================
# performance
# =====================================================================================================


def test_classify_speed_smoke(tax: Taxonomy) -> None:
    desc = " ".join(
        [INSURER_BOILERPLATE, PHYSICAL_DESC, CYBER_DESC, "Benefits: medical, dental, 401(k)."] * 30
    )
    desc = desc[:12000]
    jobs = [make(f"Analyst {i}", desc, source_id=str(i)) for i in range(300)]
    start = time.perf_counter()
    for job in jobs:
        tax.classify(job, now=NOW)
    assert time.perf_counter() - start < 5.0


# =====================================================================================================
# integration with the real catalog (skipped until catalog/lanes.yaml exists)
# =====================================================================================================

FIXED_IDS = {
    "specialty-insurance": "political-risk political-violence-terrorism war-risk kidnap-ransom special-risks "
    "trade-credit-surety transactional-liability financial-lines fine-art-specie intangibles aviation "
    "marine-cargo reinsurance-broking insurance-fraud-siu",
    "intelligence-security": "strategic-intelligence threat-intelligence protective-intelligence "
    "travel-risk-crisis gsoc-watch corporate-security osint counterterrorism regional-research-specialist "
    "defense-intel-data",
    "investigations-dd": "corporate-intelligence due-diligence beneficial-ownership fraud-investigations "
    "forensic-investigations law-firm-analyst litigation-case-analyst public-defense-investigation "
    "nonprofit-grant-dd",
    "geopolitical-risk": "country-sovereign-risk geopolitical-analysis political-forecasting geoeconomics "
    "economic-security supply-chain-forced-labor risk-consulting primary-research-expert-networks "
    "investment-research-dd",
    "sanctions-fincrime": "sanctions export-controls aml-kyc fincrime-intelligence illicit-finance "
    "crypto-investigations investigative-compliance market-surveillance",
    "trust-safety": "trust-safety-policy policy-enforcement-ai-safeguards ai-policy-forecasting "
    "election-integrity disinformation human-rights",
    "policy-research": "national-security foreign-policy think-tank-research international-orgs "
    "policy-legislative-analyst tech-regulatory-policy criminal-justice-policy social-policy-advocacy "
    "urban-planning-research research-writing",
    "strategic-comms": "strategic-comms-advisory crisis-comms public-government-affairs "
    "reputation-media-analysis insights-monitoring",
    "government": "district-attorney nyc-integrity nyc-mayors-office nys-agencies ny-fed emergency-management "
    "federal-pathways federal-investigator",
    "entry-routes": "graduate-programs analyst-programs fellowships entry-level-analyst",
    "earlier-lanes": "program-evaluation philanthropy digital-rights economic-consulting broadcast-journalism "
    "real-estate",
}


@pytest.fixture(scope="module")
def real_tax() -> Taxonomy:
    if not taxmod.LANES_PATH.exists():
        pytest.skip("catalog/lanes.yaml not written yet")
    return Taxonomy.load(profile=Profile())


def test_real_catalog_ids_and_shape(real_tax: Taxonomy) -> None:
    for g in real_tax.groups:
        assert g.id in FIXED_IDS, g.id
        assert {lane.id for lane in g.lanes} <= set(FIXED_IDS[g.id].split()), g.id
    assert not real_tax.group("earlier-lanes") or real_tax.group("earlier-lanes").active is False
    json.dumps(real_tax.to_dict())


def test_real_catalog_core_postings(real_tax: Taxonomy) -> None:
    def lanes(title: str, desc: str = "", emp: Employer | None = None) -> list[str]:
        return real_tax.classify(make(title, desc), emp, now=NOW).lanes

    assert "political-risk" in lanes("Political Risk Underwriter", "Political risk insurance at Lloyd's.")
    assert "sanctions" in lanes("Sanctions Analyst", "OFAC sanctions screening.")
    assert "gsoc-watch" in lanes("GSOC Operator", PHYSICAL_DESC)
    assert "aml-kyc" in lanes("KYC Analyst", "KYC refresh and customer due diligence.")
    assert "trust-safety-policy" in lanes("Trust & Safety Policy Associate", "harmful content policy")
    assert "protective-intelligence" in lanes("Protective Intelligence Analyst", "concerning communications")
    assert "kidnap-ransom" in lanes("K&R Underwriting Assistant", "kidnap and ransom insurance")
    assert "gsoc-watch" not in lanes("SOC Analyst", CYBER_DESC)
    acct = real_tax.classify(make("Accountant", INSURER_BOILERPLATE), BEAZLEY, now=NOW)
    assert acct.lanes == [] and acct.score <= 15
    assert real_tax.search_plan(source="usajobs")
    assert real_tax.search_plan(source="nycjobs")[0].extra == {"feed": True}


def test_real_catalog_prefilters_agree(real_tax: Taxonomy) -> None:
    _check_prefilters(real_tax._terms)


def test_real_profile_loads() -> None:
    if not taxmod.PROFILE_PATH.exists():
        pytest.skip("catalog/profile.yaml not written yet")
    prof = load_profile(taxmod.PROFILE_PATH)
    assert isinstance(prof, Profile) and prof.preferred_locations
