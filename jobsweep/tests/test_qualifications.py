"""Conservative qualification flags must not turn keyword matches into eligibility claims."""

from types import SimpleNamespace

import pytest

from jobsweep.models import Job
from jobsweep.qualifications import assess_qualifications


def assess(text, title="Investigations Analyst", **profile):
    job = Job(
        source="test", source_id="1", title=title, company="Acme", url="https://example.com", description=text
    )
    return assess_qualifications(job, SimpleNamespace(**profile))


@pytest.mark.parametrize(
    "text",
    [
        "CPA required.",
        "Must hold CAMS certification.",
        "Required qualifications:\nCFE certification",
        "Juris Doctor required.",
        "Series 7 required.",
        "Private investigator license required.",
    ],
)
def test_required_unknown_credential_is_review(text):
    status, flags = assess(text)
    assert status == "review"
    assert flags[0]["kind"] == "credential"
    assert flags[0]["status"] == "review"
    assert flags[0]["evidence"]


@pytest.mark.parametrize(
    "text", ["CAMS preferred.", "Preferred qualifications:\nCFE certification", "CPA not required."]
)
def test_preferred_or_negated_credentials_are_not_a_barrier(text):
    status, flags = assess(text)
    assert status == "no_flags"
    assert all(f["status"] != "review" for f in flags)


def test_known_expanded_credential_resolves_requirement():
    status, flags = assess("CPA required.", known_credentials=["Certified Public Accountant"])
    assert status == "no_flags"
    assert flags[0]["status"] == "met"


def test_alternative_certification_only_needs_one_known():
    status, flags = assess("CPA or CAMS required.", known_credentials=["CAMS"])
    assert status == "no_flags"
    assert flags[0]["requirement"] == "CPA or CAMS"


def test_required_and_preferred_in_separate_clauses():
    status, flags = assess("CAMS preferred; CPA required.")
    assert status == "review"
    assert [(f["requirement"], f["status"]) for f in flags] == [("CAMS", "info"), ("CPA", "review")]


def test_language_requirements_and_country_mentions():
    status, flags = assess("Fluency in Spanish and English required.", languages=["English"])
    assert status == "review"
    assert {f["requirement"]: f["status"] for f in flags} == {"Spanish": "review", "English": "met"}
    assert assess("Research French and German companies.")[1] == []


def test_preferred_language_is_informational():
    status, flags = assess("Spanish fluency preferred.")
    assert status == "no_flags"
    assert flags[0]["level"] == "preferred"


def test_title_language_requirement_is_flagged_even_without_description():
    status, flags = assess("", title="Mandarin-speaking Investigations Analyst")
    assert status == "review"
    assert flags[0]["requirement"] == "Mandarin"


@pytest.mark.parametrize(
    "text",
    [
        "An active TS/SCI clearance is required.",
        "Must possess a current Top Secret clearance.",
        "Security clearance: Secret",
        "Clearance Required:\nTS/SCI",
        "Required qualifications:\nActive TS/SCI with polygraph",
    ],
)
def test_existing_clearance_needs_review(text):
    status, flags = assess(text)
    assert status == "review"
    assert flags[0]["kind"] == "clearance"


@pytest.mark.parametrize(
    "text",
    [
        "Must be able to obtain and maintain a Secret clearance.",
        "Ability to obtain Public Trust clearance required.",
        "Clearance Required:\nAbility to Obtain Public Trust",
        "Eligible to obtain TS/SCI clearance.",
    ],
)
def test_obtainable_clearance_is_not_existing_clearance_barrier(text):
    status, flags = assess(text)
    assert status == "no_flags"
    assert flags[0]["level"] == "obtainable"
    assert flags[0]["status"] == "info"


def test_known_clearance_resolves_only_exact_level():
    status, flags = assess("Active TS/SCI clearance required.", active_clearances=["TS/SCI"])
    assert status == "no_flags"
    assert flags[0]["status"] == "met"


def test_trade_secrets_not_a_clearance_requirement():
    assert assess("Investigate theft of trade secrets and confidential information.")[1] == []


def test_empty_description_is_unassessed_and_no_flags_is_not_qualified():
    assert assess("") == ("not_assessed", [])
    assert assess("Research public records and write reports.") == ("no_flags", [])


def test_clearance_alternative_obtainability_and_polygraph_progression():
    status, flags = assess("Current TS/SCI clearance or ability to obtain one required.")
    assert status == "no_flags"
    assert flags[0]["level"] == "obtainable"
    status, flags = assess("Must possess active TS/SCI clearance with ability to obtain a polygraph.")
    assert status == "review"
    assert [(f["requirement"], f["level"]) for f in flags] == [
        ("TS/SCI", "required"),
        ("Polygraph", "obtainable"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "CPA required, CAMS preferred.",
        "CPA required and CAMS preferred.",
        "CPA required but CAMS preferred.",
        "CPA required, strong research skills, CAMS preferred.",
    ],
)
def test_mixed_credential_qualifiers_are_scoped_locally(text):
    status, flags = assess(text)
    assert status == "review"
    assert [(f["requirement"], f["level"]) for f in flags] == [("CPA", "required"), ("CAMS", "preferred")]


def test_mixed_language_qualifiers_are_scoped_locally():
    status, flags = assess("Spanish preferred but French fluency required.")
    assert status == "review"
    assert [(f["requirement"], f["status"]) for f in flags] == [("Spanish", "info"), ("French", "review")]
    assert assess("Spanish companies preferred for research coverage.")[1] == []


def test_mixed_clearance_qualifiers_are_scoped_locally():
    status, flags = assess("Active TS/SCI required, polygraph preferred.")
    assert status == "review"
    assert [(f["requirement"], f["status"]) for f in flags] == [("TS/SCI", "review"), ("Polygraph", "info")]


def test_shared_requirement_list_stays_together():
    status, flags = assess("CPA, CAMS or CFE required.", known_credentials=["CFE"])
    assert status == "no_flags"
    assert flags[0]["requirement"] == "CPA or CAMS or CFE"


@pytest.mark.parametrize(
    "text",
    [
        "PhD required.",
        "Doctorate required.",
        "A doctoral degree is mandatory.",
        "Medical degree required.",
        "MD required.",
        "RN license required.",
        "Registered nurse required.",
    ],
)
def test_specialist_academic_and_medical_credentials_require_review(text):
    status, flags = assess(text)
    assert status == "review"
    assert flags[0]["kind"] == "credential"


def test_optional_doctorate_does_not_block_and_known_doctorate_resolves():
    assert assess("PhD preferred.")[0] == "no_flags"
    assert assess("PhD required.", known_credentials=["PhD"])[0] == "no_flags"


def test_known_english_cannot_satisfy_mixed_and_or_language_requirement():
    status, flags = assess("English and either Spanish or French fluency required.", languages=["English"])
    assert status == "review"
    assert flags[0]["status"] == "review"
    assert "mixed AND/OR" in flags[0]["requirement"]
    assert assess("English and either Spanish or French required.", languages=["English"])[0] == "review"


def test_known_cpa_cannot_satisfy_mixed_and_or_credential_requirement():
    status, flags = assess("CPA and CAMS or CFE required.", known_credentials=["CPA"])
    assert status == "review"
    assert "mixed AND/OR" in flags[0]["requirement"]


@pytest.mark.parametrize(
    "text",
    [
        "Must hold an active security clearance.",
        "Current clearance required.",
    ],
)
def test_generic_current_clearance_requires_review(text):
    status, flags = assess(text)
    assert status == "review"
    assert flags[0]["kind"] == "clearance"
    assert flags[0]["level"] == "required"


def test_generic_obtainable_clearance_is_informational():
    status, flags = assess("Must be able to obtain a security clearance.")
    assert status == "no_flags"
    assert flags[0]["level"] == "obtainable"
    assert flags[0]["status"] == "info"


def test_generic_preferred_clearance_is_informational():
    status, flags = assess("An active security clearance is preferred.")
    assert status == "no_flags"
    assert flags[0]["level"] == "preferred"


@pytest.mark.parametrize(
    "text", ["Security clearance not required.", "Security clearance: None", "No clearance required."]
)
def test_generic_negated_clearance_not_flagged(text):
    assert assess(text) == ("no_flags", [])


def test_known_named_clearance_can_resolve_generic_but_public_trust_cannot():
    assert assess("Must hold an active security clearance.", active_clearances=["Secret"])[0] == "no_flags"
    assert (
        assess("Must hold an active security clearance.", active_clearances=["Public Trust"])[0] == "review"
    )


@pytest.mark.parametrize(
    "paragraph",
    [
        "Minimum qualifications\n- Hold a degree in a bio-related field (e.g., microbiology, molecular biology, biochemistry, public health) and/or relevant professional experience in a related field.",
        "Minimum qualifications\n- Hold a degree in a chemistry-related field (e.g., chemistry, chemical engineering, materials science) and/or relevant professional experience in a related field.",
    ],
)
def test_scientific_safeguards_roles_need_specialist_background_review(paragraph):
    status, flags = assess(paragraph, title="Safeguards Enforcement Analyst")
    assert status == "review"
    assert any(f["kind"] == "education" and f["status"] == "review" for f in flags)
    assert "Hold a degree" in flags[0]["evidence"]


@pytest.mark.parametrize(
    "field",
    ["engineering", "computer science", "accounting", "mathematics", "statistics", "quantitative field"],
)
def test_specialist_degree_fields_require_review(field):
    assert assess("A bachelor's degree in " + field + " is required.")[0] == "review"


def test_preferred_specialist_degree_is_not_blocker():
    status, flags = assess("Preferred qualifications\nDegree in chemistry.")
    assert status == "no_flags"
    assert flags[0]["kind"] == "education"
    assert flags[0]["status"] == "info"


@pytest.mark.parametrize(
    "text",
    [
        "Bachelor's degree required.",
        "Degree in political science required.",
        "Degree in social science required.",
        "Bachelor's degree in political science, mathematics or a related field required.",
        "Degree in any discipline required. You will work with a chemistry team.",
    ],
)
def test_general_and_known_social_science_degrees_not_specialist_blockers(text):
    assert assess(text)[0] == "no_flags"


def test_explicit_known_specialization_can_resolve_education_requirement():
    status, flags = assess(
        "Minimum qualifications\nDegree in chemistry or relevant professional experience.",
        known_credentials=["BSc Chemistry"],
    )
    assert status == "no_flags"
    assert flags[0]["kind"] == "education"
    assert flags[0]["status"] == "met"
