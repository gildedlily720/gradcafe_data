import pytest

from jobsweep.experience import experience_flags, extract_experience
from jobsweep.models import Job, Profile
from jobsweep.suitability import assess_role
from jobsweep.taxonomy import Taxonomy


@pytest.mark.parametrize(
    ("text", "minimum"),
    [
        ("2+ years of financial industry experience", 2),
        ("5 years overall experience, including 2 years of banking experience", 5),
        ("2 years of experience preferred and 5 years of experience required", 5),
        ("5 years of experience required and 2 years of experience preferred", 5),
        ("Preferred qualifications:\n5 years of financial industry experience", None),
        ("3–5 years of experience in investigations", 3),
        ("one (1) year of relevant experience", 1),
        ("6 months of work experience required", 0.5),
        ("No previous experience is required", 0),
        ("2-year work experience required", 2),
        ("A 4-year degree is required", None),
        ("For more than 20 years, our firm has advised clients", None),
        ("Our team has 30 years of combined experience", None),
        ("Bachelor's and 5 years of experience OR a Master's and 3 years of experience", None),
    ],
)
def test_required_minimum(text, minimum):
    assert extract_experience(text)["years_required"] == minimum


def test_scope_is_not_generic_years_and_preference_is_not_required():
    audit = extract_experience(
        "The successful candidate must have:\n2+ years of financial industry experience"
    )
    assert audit["experience_requirements"][0]["scope"] == ["financial industry"]
    assert experience_flags(audit)[0]["status"] == "review"
    assert (
        experience_flags(extract_experience("2 years of financial industry experience preferred"))[0][
            "status"
        ]
        == "info"
    )


def test_independent_requirements_and_strictly_more():
    audit = extract_experience("5 years of work experience, including 2 years of insurance experience")
    assert audit["years_required"] == 5
    assert [r["min_years"] for r in audit["experience_requirements"]] == [5, 2]
    assert extract_experience("More than 2 years of experience")["experience_requirements"][0][
        "strictly_more"
    ]
    assert not extract_experience("2+ years of experience")["experience_requirements"][0]["strictly_more"]


def test_incomplete_or_alternative_text_requires_review():
    for text in [
        "",
        "About our firm. We are seeking an analyst …",
        "Bachelor's and 5 years of experience or a Master's and 3 years of experience",
    ]:
        audit = extract_experience(text)
        assert audit["years_required"] is None
        assert experience_flags(audit)[-1]["status"] == "review"


def test_full_evidence_survives_truncated_state_and_resets_on_new_posting():
    tax = Taxonomy.load(profile=Profile(screen_qualifications=True))
    job = Job(
        source="test",
        source_id="1",
        title="Research Analyst",
        company="Acme",
        url="https://example.com",
        description="Required qualifications:\n2+ years of financial industry experience",
    )
    tax.classify(job)
    assert job.qualification_status == "review"
    assert job.years_required == 2
    job.description = "Company introduction …"
    tax.classify(job)
    assert job.years_required == 2
    assert job.experience_requirements[0]["scope"] == ["financial industry"]
    job.description = "No experience required."
    tax.classify(job)
    assert job.years_required == 0
    assert job.qualification_status == "no_flags"


def test_private_capital_sales_is_not_intelligence_research():
    profile = Profile(excluded_role_patterns=[r"\baccountant\b"])
    job = Job(
        source="test",
        source_id="1",
        title="ICS - Private Capital Group, Analyst",
        company="Blackstone",
        url="https://example.com",
        description="The key function of the role will be to support business development efforts across North America.",
    )
    assert assess_role(job, profile)[0] == "excluded"
    job.title = "Financial Crime Risk Analysis"
    job.description = "Investigate illicit financial flows and produce intelligence assessments."
    assert assess_role(job, profile)[0] == "eligible"
