"""Suitability is independent of keyword relevance and is opt-in for custom profiles."""

import pytest

from jobsweep.models import Employer, Job, Profile
from jobsweep.suitability import assess_location
from jobsweep.taxonomy import Taxonomy, load_profile


def make(location="New York, NY", remote=None, description="Research public records.", **kwargs):
    return Job(
        source="test",
        source_id="1",
        title=kwargs.pop("title", "Investigations Analyst"),
        company="Acme",
        url="https://example.com/job",
        location=location,
        remote=remote,
        description=description,
        **kwargs,
    )


@pytest.mark.parametrize(
    "location,remote,description,expected",
    [
        ("New York, NY", False, "", "eligible"),
        ("New York", None, "", "eligible"),
        ("New York, United States", False, "", "eligible"),
        ("Brooklyn, NY (Hybrid)", False, "", "eligible"),
        ("London; New York, NY", False, "", "eligible"),
        ("Albany, New York", False, "", "ineligible"),
        ("New York State", False, "", "ineligible"),
        ("Washington, DC", False, "", "ineligible"),
        ("London", False, "", "ineligible"),
        ("Remote", True, "", "review"),
        ("United States", True, "", "review"),
        ("Remote - US", True, "", "review"),
        ("Remote - New York", True, "", "eligible"),
        ("Remote - California", True, "", "ineligible"),
        ("Brazil", True, "", "ineligible"),
        ("United States", True, "Work remotely from anywhere in the United States.", "eligible"),
        ("Remote", True, "Candidates may work remotely in all 50 states.", "eligible"),
        ("Remote", True, "We serve clients nationwide and have offices worldwide.", "review"),
        ("United States", True, "Must reside in Texas or California.", "ineligible"),
        ("New York, NY", True, "Must reside in California.", "ineligible"),
        ("United States", True, "Must reside in the United States.", "review"),
        ("United States", True, "Eligible states: New York, Texas, California.", "eligible"),
        ("Remote", True, "This role is not available to residents of New York.", "ineligible"),
        ("New York - Remote", True, "We cannot hire residents of New York.", "ineligible"),
        ("", None, "Our company has an office in New York.", "review"),
    ],
)
def test_location_evidence(location, remote, description, expected):
    status, reasons = assess_location(make(location, remote, description), Profile(strict_nyc_location=True))
    assert status == expected
    assert reasons


def test_legacy_profile_has_no_gate():
    assert assess_location(make("London"), Profile()) == ("not_assessed", [])


@pytest.fixture(scope="module")
def strict_taxonomy():
    return Taxonomy.load()


@pytest.mark.parametrize(
    "title,employer_lanes",
    [
        ("Executive Assistant III, Strategic Communications", ["strategic-comms-advisory"]),
        ("Software Engineer, AML", ["aml-kyc"]),
        ("AML Monitoring Engineer", ["aml-kyc"]),
        ("Information Technology Analyst - AI and Emerging Technologies", ["ai-policy-forecasting"]),
        ("Workplace Experience Specialist - NYC", ["aml-kyc", "fincrime-intelligence"]),
        ("Forensic Accountant", ["corporate-intelligence"]),
        ("Quantitative Research Analyst", ["think-tank-research"]),
        ("Demand & Supply Planner, Intelligence Systems", ["defense-intel-data"]),
    ],
)
def test_employer_keywords_cannot_rescue_excluded_function(strict_taxonomy, title, employer_lanes):
    j = make(
        title=title,
        description="AML investigations, intelligence research and due diligence. 2 years experience.",
    )
    e = Employer(key="acme", name="Acme", lanes=employer_lanes)
    strict_taxonomy.classify(j, e)
    assert j.role_fit == "excluded"
    assert j.suitability == "excluded"
    assert j.score == 0


@pytest.mark.parametrize(
    "title",
    [
        "Underwriting Assistant",
        "Research Assistant",
        "Program Assistant",
        "Financial Crime Risk Analysis Analyst",
    ],
)
def test_relevant_assistant_and_analyst_roles_remain(strict_taxonomy, title):
    j = strict_taxonomy.classify(make(title=title))
    assert j.role_fit == "eligible"
    assert j.location_eligibility == "eligible"


def test_overseas_relevance_cannot_override_location(strict_taxonomy):
    j = strict_taxonomy.classify(make(location="London", description="2 years of investigations experience"))
    assert j.suitability == "excluded"
    assert j.score == 0


def test_mandatory_credential_is_review_not_qualified(strict_taxonomy):
    j = strict_taxonomy.classify(
        make(description="Required qualifications: CPA certification required. 2 years experience.")
    )
    assert j.entry_level is True
    assert j.qualification_status == "review"
    assert j.suitability == "review"
    assert j.qualification_checks


def test_banking_avp_does_not_alone_imply_senior_or_qualified(strict_taxonomy):
    j = strict_taxonomy.classify(make(title="AVP Financial Crime Risk Analysis"))
    assert j.entry_level is None
    assert j.suitability == "review"
    assert any(c["kind"] == "experience" for c in j.qualification_checks)
    j = strict_taxonomy.classify(
        make(title="AVP Financial Crime Risk Analysis", description="2 years of investigations experience.")
    )
    assert j.entry_level is True
    j = strict_taxonomy.classify(
        make(title="AVP Financial Crime Risk Analysis", description="7 years of investigations experience.")
    )
    assert j.entry_level is False


def test_custom_profile_is_not_silently_made_strict(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text('preferred_locations: ["London"]\n')
    p = load_profile(path)
    assert p.preferred_locations == ["London"]
    assert p.countries == ["US", "GB"]
    assert not p.strict_nyc_location
    assert not p.screen_qualifications
    assert not p.excluded_role_patterns


def test_screening_serializes_and_preserves_backwards_compatibility(strict_taxonomy):
    j = strict_taxonomy.classify(make())
    d = j.to_dict()
    assert Job.from_dict(d).suitability == j.suitability
    assert "location_reasons" in d and "qualification_checks" in d


def test_only_enabled_screens_affect_suitability():
    taxonomy = Taxonomy.load()
    taxonomy.profile = Profile(strict_nyc_location=True)
    j = taxonomy.classify(make(description=""))
    assert j.location_eligibility == "eligible"
    assert j.qualification_status == "not_assessed"
    assert j.suitability == "eligible"
