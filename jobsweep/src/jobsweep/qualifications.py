"""Conservative, explainable qualification screening; absence of flags is not qualification.

This deliberately records evidence instead of inferring that an early-career title means the
candidate meets every requirement. Only profile facts explicitly supplied by the user can resolve
specialist requirements. The caller should retain ``review`` jobs in a separate review queue.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Job, Profile

_REQUIRED = re.compile(r"\b(?:required|requirement|must|mandatory|essential|minimum|need to|shall)\b", re.I)
_PREFERRED = re.compile(
    r"\b(?:preferred|desirable|desired|nice[ -]to[ -]have|a plus|an advantage|bonus)\b", re.I
)
_OBTAINABLE = re.compile(
    r"\b(?:ability|able|eligible|eligibility|willing(?:ness)?)\s+(?:to|for)\s+(?:obtain|acquire|qualify|receive|a\b)|"
    r"\b(?:obtain|acquire)\b.{0,55}\b(?:after|within|upon|following)\b|"
    r"\b(?:sponsor|sponsorship|will (?:provide|support))\b",
    re.I,
)
_NEGATED = re.compile(
    r"\b(?:not|never)\s+(?:strictly\s+)?(?:required|necessary|mandatory)|"
    r"\bno\s+(?:prior\s+|active\s+|existing\s+)?(?:security\s+)?(?:clearance|certification|license|licence)\s+(?:is\s+)?required|"
    r"\bclearance\s*:\s*(?:none|n/?a|not applicable|not required)\b",
    re.I,
)
_REQUIRED_HEADING = re.compile(
    r"^(?:minimum|required|essential|basic)\s+(?:qualifications|requirements|skills|experience)|"
    r"^(?:qualifications|requirements|what you (?:bring|need)|what you'll (?:bring|need))\s*[:?]?$",
    re.I,
)
_PREFERRED_HEADING = re.compile(
    r"^(?:preferred|desired|desirable|additional)\s+(?:qualifications|skills|experience)", re.I
)
_OTHER_HEADING = re.compile(
    r"^(?:responsibilities|benefits|compensation|about (?:us|the (?:company|team|role))|what you(?:'ll| will) do|"
    r"equal opportunity|our (?:company|culture|benefits))\s*[:?]?$",
    re.I,
)
_CREDENTIALS = {
    "CPA": r"\bcpa\b|\bcertified public accountant\b",
    "CAMS": r"\bcams\b|\bcertified anti[ -]money laundering specialist\b",
    "CFCS": r"\bcfcs\b|\bcertified financial crime specialist\b",
    "CFE": r"\bcfe\b|\bcertified fraud examiner\b",
    "JD": r"\bj\.?d\.?\b|\bjuris doctor(?:ate)?\b",
    "PhD": r"\bph\.?d\.?\b|\bdoctorate\b|\bdoctoral degree\b",
    "medical degree/license": r"\b(?:doctor of medicine|doctor of osteopathic medicine|medical degree|licensed physician)\b|\bm\.?d\.?\s+(?:degree|licen[cs]e|required|preferred)\b",
    "RN": r"\brn\b|\bregistered nurse\b",
    "bar admission": r"\b(?:admi(?:tted|ssion) to (?:a |the |any |[a-z ]{0,25})?bar|bar (?:admission|membership)|licensed attorney)\b",
    "Series 7": r"\bseries[ -]7\b",
    "Series 63": r"\bseries[ -]63\b",
    "Series 66": r"\bseries[ -]66\b",
    "CFA": r"\bcfa\b|\bchartered financial analyst\b",
    "CISSP": r"\bcissp\b|\bcertified information systems security professional\b",
    "PMP": r"\bpmp\b|\bproject management professional\b",
    "private investigator license": r"\b(?:private investigator|pi)\s+(?:licen[cs]e|registration)\b",
    "security guard license": r"\b(?:security guard|guard)\s+(?:licen[cs]e|registration|card)\b",
}
_CREDENTIAL_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in _CREDENTIALS.items()]
_SPECIALIST_FIELDS = {
    "biology": r"\b(?:bio[ -]related|biolog(?:y|ical)|biomedical|microbiology|molecular biology|biochemistry|public health)\b",
    "chemistry": r"\b(?:chem(?:istry)?[ -]related|chemistry|chemical engineering|materials science)\b",
    "engineering": r"\bengineering\b",
    "computer science": r"\b(?:computer science|computer engineering|software engineering|information technology|informatics)\b",
    "accounting": r"\b(?:accounting|accountancy)\b",
    "mathematics/statistics": r"\b(?:mathematics|mathematical|math|statistics|statistical|quantitative|econometrics|operations research|data science)\b",
}
_SPECIALIST_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in _SPECIALIST_FIELDS.items()]
_DEGREE = re.compile(r"\b(?:degree|bachelor(?:'s)?|master(?:'s)?|doctorate|ph\.?d\.?)\b", re.I)
_GENERAL_DEGREE_FIELDS = re.compile(
    r"\b(?:political science|social sciences?|international relations|public policy|humanities|any (?:field|discipline))\b",
    re.I,
)


def _specialist_education(clause: str) -> list[str]:
    degree = _DEGREE.search(clause)
    if not degree:
        return []
    # Examples explain an umbrella field; they are not additional mandatory degrees.
    field_text = re.split(
        r"\(\s*(?:e\.g\.|for example|such as)", clause[degree.start() :], maxsplit=1, flags=re.I
    )[0][:240]
    if _GENERAL_DEGREE_FIELDS.search(field_text):
        if re.search(r"\bor\b|,|\bany (?:field|discipline)\b", field_text, re.I):
            return []
    return [name for name, pattern in _SPECIALIST_PATTERNS if pattern.search(field_text)]


_LANGUAGES = (
    "English",
    "Spanish",
    "French",
    "German",
    "Arabic",
    "Mandarin",
    "Cantonese",
    "Chinese",
    "Russian",
    "Portuguese",
    "Japanese",
    "Korean",
    "Italian",
    "Hebrew",
    "Farsi",
    "Persian",
    "Turkish",
    "Ukrainian",
    "Hindi",
    "Urdu",
    "Indonesian",
    "Vietnamese",
    "Polish",
    "Dutch",
)
_LANGUAGE_RE = re.compile(r"\b(" + "|".join(_LANGUAGES) + r")\b", re.I)
_LANGUAGE_SKILL = re.compile(
    r"\b(?:fluent|fluency|proficien\w*|bilingual|trilingual|native|speak\w*|reading|writing|written|"
    r"oral|verbal|command|language|linguist|translation|translate|professional working)\b",
    re.I,
)
_CLEARANCE = re.compile(
    r"\bts\s*/\s*sci\b|\btop[ -]secret(?:\s*/\s*sci)?\b|\bsecret\b|\bpublic trust\b|\bpolygraph\b", re.I
)
_GENERIC_CLEARANCE = re.compile(r"\b(?:security\s+)?clearance\b", re.I)
_ACTIVE_CLEARANCE = re.compile(r"\b(?:active|current(?:ly)?|existing|possess|hold|held|in[ -]scope)\b", re.I)


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _scoped_parts(clause: str) -> list[str]:
    """Separate criteria when each side has its own qualifier, keeping shared lists intact.

    "CPA, CAMS or CFE required" shares one requirement. "CPA required, CAMS preferred"
    has two local qualifiers; applying either to the whole sentence would change its meaning.
    """
    pieces = re.split(r",\s*|\s+(?:but|while|and)\s+", clause, flags=re.I)
    scoped = [bool(_REQUIRED.search(p) or _PREFERRED.search(p) or _OBTAINABLE.search(p)) for p in pieces]
    if sum(scoped) < 2:
        return [clause]
    out: list[str] = []
    pending: list[str] = []
    for piece, has_scope in zip(pieces, scoped, strict=True):
        pending.append(piece)
        if has_scope:
            out.append(", ".join(pending))
            pending = []
    if pending:
        # Trailing unqualified prose should not inherit an earlier preferred/required marker.
        out.append(", ".join(pending))
    return out


def _parts(text: str):
    """Yield short clauses with required/preferred section context, preserving JD abbreviations."""
    section = ""
    clearance_heading = ""
    for raw in text.splitlines():
        line = raw.strip(" \t•*-–")
        if not line:
            continue
        if re.fullmatch(r"(?:security )?clearance(?: required)?\s*:", line, re.I):
            clearance_heading = line
            continue
        if clearance_heading:
            line = clearance_heading + " " + line
            clearance_heading = ""
        if _PREFERRED_HEADING.search(line):
            section = "preferred"
        elif _REQUIRED_HEADING.search(line):
            section = "required"
        elif _OTHER_HEADING.search(line):
            section = ""
        for clause in re.split(r";|(?<=[.!?])\s+(?=[A-Z])|\s+(?:with|and)\s+(?=(?:the\s+)?ability to)", line):
            if clause.strip():
                for part in _scoped_parts(clause.strip()):
                    yield part, section


def _level(clause: str, section: str) -> str | None:
    if _NEGATED.search(clause):
        return None
    if _PREFERRED.search(clause):
        return "preferred"
    if _OBTAINABLE.search(clause):
        return "obtainable"
    if _REQUIRED.search(clause):
        return "required"
    return section or None


def assess_qualifications(job: Job, profile: Profile) -> tuple[str, list[dict[str, str]]]:
    """Return unresolved mandatory flags plus informative preferred/obtainable criteria.

    ``no_flags`` means only that this heuristic found no unresolved explicit requirements;
    it is never equivalent to "qualified". Missing source text returns ``not_assessed``.
    """
    flags: list[dict[str, str]] = []
    credentials = list(getattr(profile, "known_credentials", []) or [])
    languages = {_normal(x) for x in (getattr(profile, "languages", ["English"]) or [])}
    clearances = {_normal(x) for x in (getattr(profile, "active_clearances", []) or [])}
    if clearances.intersection({"secret", "topsecret", "tssci", "confidential", "securityclearance"}):
        clearances.update({"securityclearance", "clearance"})
    known_education = {
        _normal("degree/background in " + name)
        for name, pattern in _SPECIALIST_PATTERNS
        if any(pattern.search(credential) for credential in credentials)
    }
    known_credentials = {_normal(x) for x in credentials}
    # Resolve explicitly supplied expanded names, e.g. Certified Public Accountant, as well as CPA.
    for name, pattern in _CREDENTIAL_PATTERNS:
        if any(pattern.search(x) for x in credentials):
            known_credentials.add(_normal(name))

    def add(
        kind: str, names: list[str], level: str, evidence: str, known: set[str], either=False, mixed=False
    ):
        found = [(_normal(name) in known) for name in names]
        met = any(found) if either else all(found)
        requirement = (
            ", ".join(names) + " (mixed AND/OR requirements; check posting)"
            if mixed
            else (" or " if either else " and ").join(names)
        )
        item = {
            "kind": kind,
            "requirement": requirement,
            "level": level,
            "status": "met" if met else ("review" if level == "required" else "info"),
            "evidence": evidence[:400],
        }
        if not any(all(old[k] == item[k] for k in ("kind", "requirement", "level")) for old in flags):
            flags.append(item)

    text = job.description or ""
    # Title signals are explicit role requirements, not a complete qualification assessment.
    clauses = [(job.title or "", ""), *_parts(text)]
    for clause, section in clauses:
        level = _level(clause, section)
        credential_names = [name for name, pat in _CREDENTIAL_PATTERNS if pat.search(clause)]
        if credential_names and level:
            either = len(credential_names) > 1 and bool(re.search(r"\bor\b", clause, re.I))
            mixed = either and bool(re.search(r"\band\b", clause, re.I))
            if mixed:
                # Without a full logical parser, do not let one known alternative satisfy an AND.
                add("credential", credential_names, level, clause, known_credentials, mixed=True)
            elif either:
                add("credential", credential_names, level, clause, known_credentials, either=True)
            else:
                for name in credential_names:
                    add("credential", [name], level, clause, known_credentials)

        specialist_fields = _specialist_education(clause)
        if specialist_fields and level:
            names = ["degree/background in " + field for field in specialist_fields]
            either = len(names) > 1 and bool(re.search(r"\bor\b", clause, re.I))
            mixed = either and bool(re.search(r"\band\b", clause, re.I))
            add("education", names, level, clause, known_education, either=either and not mixed, mixed=mixed)

        language_names = list(dict.fromkeys(m.group(0).title() for m in _LANGUAGE_RE.finditer(clause)))
        bare_language_requirement = any(
            re.search(
                r"\b"
                + re.escape(name)
                + r"\s+(?:is\s+)?(?:required|preferred|essential|desirable|mandatory)\b",
                clause,
                re.I,
            )
            for name in language_names
        )
        if language_names and (_LANGUAGE_SKILL.search(clause) or bare_language_requirement):
            lang_level = level
            # "Mandarin-speaking Analyst" / "Fluent in Spanish" states a skill even without "must".
            if not lang_level and not _NEGATED.search(clause):
                if re.search(
                    r"\b(?:fluent|fluency|bilingual|trilingual|native|proficien\w*)\b|[ -]speaking\b",
                    clause,
                    re.I,
                ):
                    lang_level = "required"
            if lang_level:
                either = len(language_names) > 1 and bool(re.search(r"\bor\b", clause, re.I))
                mixed = either and bool(re.search(r"\band\b", clause, re.I))
                if mixed:
                    add("language", language_names, lang_level, clause, languages, mixed=True)
                elif either:
                    add("language", language_names, lang_level, clause, languages, either=True)
                else:
                    for name in language_names:
                        add("language", [name], lang_level, clause, languages)

        # Secret must appear in a clearance context, not a trade-secret investigation description.
        if not re.search(r"clearance|ts\s*/\s*sci|polygraph|public trust", clause, re.I):
            continue
        clearance_matches = list(_CLEARANCE.finditer(clause))
        if not clearance_matches:
            clearance_matches = list(_GENERIC_CLEARANCE.finditer(clause))
        for match in clearance_matches:
            raw = match.group(0)
            name = (
                "TS/SCI"
                if re.search(r"sci", raw, re.I)
                else ("Top Secret" if re.search(r"top", raw, re.I) else raw.title())
            )
            clr_level = level
            if _NEGATED.search(clause):
                continue
            if not clr_level and (
                _ACTIVE_CLEARANCE.search(clause) or re.search(r"clearance\s*:", clause, re.I)
            ):
                clr_level = "required"
            # Existing clearance remains mandatory even if later progression can be sponsored.
            alternative_obtainable = re.search(
                r"\bor\s+(?:the\s+)?(?:ability|be able|eligible)", clause, re.I
            )
            if (
                _ACTIVE_CLEARANCE.search(clause[: match.start() + len(raw)])
                and level == "obtainable"
                and not alternative_obtainable
            ):
                clr_level = "required"
            if clr_level:
                add("clearance", [name], clr_level, clause, clearances)

    if any(flag["status"] == "review" for flag in flags):
        return "review", flags
    if not text.strip():
        return "not_assessed", flags
    return "no_flags", flags
