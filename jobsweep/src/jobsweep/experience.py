"""Evidence-based experience requirements; numeric thresholds are not candidate qualification."""

from __future__ import annotations

import re

VERSION = 1
WORDS = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
        range(21),
        strict=True,
    )
)
NUMBER = r"(?:\d{1,2}(?:\.\d+)?|" + "|".join(WORDS) + r")"
YEARS = re.compile(
    r"(?<![\w.,/])(?P<n>" + NUMBER + r")(?:\s*\(\s*\d{1,2}\s*\))?\s*(?:\+|plus)?\s*"
    r"(?:(?:[-–—]|to|or)\s*(?P<end>" + NUMBER + r")(?:\s*\(\s*\d{1,2}\s*\))?\s*\+?\s*)?"
    r"(?:full[ -]+)?(?:or\s+more\s+)?-?(?P<unit>years?|yrs?|months?)\b",
    re.I,
)
EXPERIENCE = re.compile(r"\b(?:experience|employment|working|work experience|professional practice)\b", re.I)
BAD_AFTER = re.compile(
    r"^\s*['’]?\s*[- ]?\s*(?:old|of age|ago|of (?:history|service|operation|growth)|in business|anniversary|"
    r"degree|program|programme|college|university|bachelor|fellowship|term|contract|appointment|assignment|"
    r"period|commitment|rotation|scheme|course|cycle|plan|budget|grant|warranty|since|after|from|of graduation)\b",
    re.I,
)
BAD_BEFORE = re.compile(
    r"\b(?:founded|established|celebrat\w*|history|legacy|combined|within the last|within the past|"
    r"our (?:team|firm|company|founders?|experts?|professionals|people))\b",
    re.I,
)
PREFERRED = re.compile(
    r"\b(?:preferred|desirable|desired|ideally|nice[ -]to[ -]have|a plus|an advantage|bonus)\b", re.I
)
REQUIRED = re.compile(r"\b(?:required|requirements?|minimum|at least|must|mandatory|essential|need)\b", re.I)
NO_EXPERIENCE = re.compile(
    r"\bno\s+(?:(?:prior|previous|professional|work|industry)\s+)*experience\s+(?:is\s+)?(?:required|necessary|needed)\b",
    re.I,
)
SCOPES = {
    "financial industry": r"financial (?:industry|services)|finance industry|banking industry",
    "investment banking": r"investment banking",
    "private equity / capital": r"private equity|private capital|private markets|fundraising|capital raising",
    "insurance / underwriting": r"insurance|underwriting|reinsurance|claims",
    "financial crime / compliance": r"financial crim|anti.money laundering|\baml\b|\bkyc\b|sanctions|compliance",
    "client sales / relationships": r"business development|client (?:engag|relationship)|sales|prospecting|investor relations",
    "law enforcement / intelligence": r"law enforcement|military|intelligence community|police|criminal investigation",
    "software / cybersecurity": r"software|cybersecurity|cyber security|information security|penetration test|systems engineering",
    "accounting": r"accounting|auditing|bookkeeping",
}


def _number(value: str) -> float | int:
    number = WORDS[value.lower()] if value.lower() in WORDS else float(value)
    return int(number) if int(number) == number else number


def extract_experience(text: str | None) -> dict:
    """Retain each criterion; use the strongest mandatory threshold, never a preferred/subset minimum.

    Education/experience alternatives are deliberately unresolved: the smallest path cannot be
    silently treated as applicable to this candidate. Truncated text with no numeric requirement
    remains incomplete, not zero. Employer history and program duration are excluded.
    """
    text = (text or "").replace("\u00a0", " ")
    requirements = []
    section = "required"
    for raw in re.split(r"[\n;]|(?<=[.!?])\s+(?=[A-Z])", text):
        line = raw.strip(" \t•*-–")
        if not line:
            continue
        if re.match(
            r"^(?:preferred|desired|desirable|nice.to.have)\s+(?:qualifications|skills|experience|requirements)",
            line,
            re.I,
        ):
            section = "preferred"
        elif re.match(
            r"^(?:(?:required|minimum|basic|essential)\s+)?(?:qualifications|requirements)\s*[:?]?$|^the successful candidate must have",
            line,
            re.I,
        ):
            section = "required"
        elif re.match(
            r"^(?:responsibilities|benefits|compensation|about us|what you will do)\s*:?$", line, re.I
        ):
            section = "required"
        matches = list(YEARS.finditer(line))
        zero = NO_EXPERIENCE.search(line)
        if zero:
            requirements.append(
                {
                    "min_years": 0,
                    "level": "required",
                    "scope": [],
                    "evidence": line[:400],
                    "strictly_more": False,
                }
            )
        for index, match in enumerate(matches):
            # Keep qualifiers local to each numeric requirement rather than the entire paragraph.
            start = matches[index - 1].end() if index else 0
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            before = line[start : match.start()]
            after = line[match.end() : end]
            if BAD_AFTER.search(after) or BAD_BEFORE.search(before[-75:]):
                continue
            local_before = re.split(r",|\b(?:and|but)\b", before, flags=re.I)[-1]
            nearby = local_before[-60:] + match.group(0) + after[:160]
            if not EXPERIENCE.search(after[:120]) and not (
                EXPERIENCE.search(before[-60:])
                or REQUIRED.search(before[-35:])
                and re.match(r"\s+in\s+", after)
            ):
                continue
            if re.search(
                r"\b(?:our|we|firm|company|team)\b.{0,30}\b(?:has|have|with|brings?|offers?)\b", before, re.I
            ):
                continue
            if re.search(r"\b(?:combined experience|of combined|history|in business)\b", after[:80], re.I):
                continue
            years = _number(match.group("n"))
            if years > 40:
                continue
            if match.group("unit").lower().startswith("month"):
                years /= 12
            # A required clause followed by a preferred clause must retain both local levels.
            clause = re.split(r",\s*(?=(?:but|and)?\s*(?:at least|minimum|\d))", nearby)[0]
            level = (
                "preferred"
                if PREFERRED.search(clause)
                else "required"
                if REQUIRED.search(clause)
                else section
            )
            if re.search(r"\bnot (?:required|necessary)|\bno minimum\b", nearby, re.I):
                continue
            scope_text = re.split(r"[.;]|\b(?:including|of which)\b", after, maxsplit=1, flags=re.I)[0][:160]
            scope = [name for name, pattern in SCOPES.items() if re.search(pattern, scope_text, re.I)]
            requirements.append(
                {
                    "min_years": years,
                    "level": level,
                    "scope": scope,
                    "evidence": line[max(0, match.start() - 65) : min(len(line), match.end() + 230)].strip()[
                        :400
                    ],
                    "strictly_more": bool(re.search(r"(?:more than|over|greater than)\s*$", before, re.I)),
                }
            )
        # A specialist requirement can matter even without a number of years.
        if (
            not matches
            and EXPERIENCE.search(line)
            and not zero
            and not re.search(r"not required|no .*experience", line, re.I)
        ):
            scopes = [name for name, pattern in SCOPES.items() if re.search(pattern, line, re.I)]
            if scopes and (
                REQUIRED.search(line)
                or re.match(r"^(?:experience|prior experience|previous experience)\b", line, re.I)
            ):
                requirements.append(
                    {
                        "min_years": None,
                        "level": "preferred" if PREFERRED.search(line) else section,
                        "scope": scopes,
                        "evidence": line[:400],
                        "strictly_more": False,
                    }
                )
    mandatory = [r for r in requirements if r["level"] == "required" and r["min_years"] is not None]
    alternatives = bool(mandatory) and bool(
        re.search(
            r"\bor\s+(?:a\s+|an\s+)?(?:master|bachelor|ph\.?d|doctor|equivalent)|"
            r"(?:master|bachelor|ph\.?d|doctor)[^\n.]{0,120}\bor\b[^\n.]{0,100}(?:year|experience)|"
            r"(?:education|degree|college|credits|diploma)[^\n.]{0,160}(?:substitut|in lieu|instead)|"
            r"equivalent combination of[^\n.]{0,80}(?:education|experience)|"
            r"\bOR\s*\n\s*(?:\d[.)]\s*)?(?:a |an )?(?:high school|associate|bachelor|master)",
            text,
            re.I,
        )
    )
    if alternatives:
        years = None
        status = "alternatives"
    elif mandatory:
        years = max(r["min_years"] for r in mandatory)
        status = "stated"
    else:
        years = None
        status = "incomplete" if not text.strip() or text.rstrip().endswith(("…", "...")) else "unspecified"
    return {
        "years_required": years,
        "experience_requirements": requirements,
        "experience_status": status,
        "experience_version": VERSION,
    }


def experience_flags(audit: dict) -> list[dict[str, str]]:
    flags = []
    for requirement in audit["experience_requirements"]:
        if requirement["scope"] and requirement["min_years"] != 0:
            flags.append(
                {
                    "kind": "experience",
                    "requirement": ", ".join(requirement["scope"]) + " experience",
                    "level": requirement["level"],
                    "status": "review" if requirement["level"] == "required" else "info",
                    "evidence": requirement["evidence"],
                }
            )
    if audit["experience_status"] in {"alternatives", "incomplete"}:
        flags.append(
            {
                "kind": "experience",
                "requirement": "Education/experience alternatives"
                if audit["experience_status"] == "alternatives"
                else "Incomplete experience requirements",
                "level": "required",
                "status": "review",
                "evidence": "Check which education/experience pathway applies; no shortest path is assumed."
                if audit["experience_status"] == "alternatives"
                else "The available posting text is missing or truncated before a numeric experience requirement was found.",
            }
        )
    return flags
