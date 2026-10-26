"""Conservative location and job-function screens, independent of relevance scores.

A location is eligible only with evidence of an NYC workplace or New York-compatible remote work.
Unspecified remote geography is a review item, never inferred from an employer's headquarters.
"""

from __future__ import annotations

import re

from .models import Job, Profile

_NYC = re.compile(
    r"\b(?:nyc|new\s+york\s+city|manhattan|brooklyn|queens|bronx|staten\s+island|long\s+island\s+city)\b",
    re.I,
)
_NY = re.compile(r"\b(?:new\s+york|ny)\b", re.I)
_REMOTE = re.compile(r"\b(?:remot(?:e|ely)|work\s+from\s+home|home[ -]based|anywhere)\b", re.I)
_US = re.compile(r"\b(?:united\s+states|u\.?s\.?a?\.?|usa)\b", re.I)
_FOREIGN = re.compile(
    r"\b(?:uk|united\s+kingdom|london|england|canada|india|mexico|brazil|philippines|germany|france"
    r"|spain|portugal|poland|netherlands|ireland|singapore|australia|japan|china|hong\s+kong|emea"
    r"|apac|latam|europe|asia|africa|argentina|colombia|romania|ukraine|israel|uae|dubai)\b",
    re.I,
)
_EXCLUDE = re.compile(
    r"\b(?:except|excluding|excluded|not\s+(?:available|eligible|open)|cannot|can't|unable|not\s+hire|ineligible)\b",
    re.I,
)
_RESIDENCE = re.compile(
    r"\b(?:(?:must|required\s+to)\s+(?:be\s+)?(?:reside|residing|live|living|based|located)"
    r"|(?:only|eligible)\s+(?:in|within|states|locations)|(?:residency|residence|location)\s+(?:requirements?|restrictions?)"
    r"|(?:hiring|hire|employ|employment|available|open)\s+(?:only|restricted|limited)|following\s+states)\b",
    re.I,
)
_NATIONWIDE = re.compile(
    r"\b(?:all\s+(?:50|fifty)\s+states|(?:anywhere|any\s+state)\s+(?:in|within|across)\s+(?:the\s+)?"
    r"(?:united\s+states|u\.?s\.?a?\.?|usa)|(?:us|u\.s\.|united\s+states)[ -]wide|nationwide)\b",
    re.I,
)
_WORLDWIDE = re.compile(
    r"\b(?:worldwide|work\s+from\s+anywhere\s+in\s+the\s+world|globally\s+remote)\b", re.I
)
_OTHER_STATE = re.compile(
    r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia"
    r"|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan"
    r"|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico"
    r"|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina"
    r"|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming)\b",
    re.I,
)
_US_SCOPE_ONLY = re.compile(
    r"^(?:(?:remote|fully|work\s+from\s+home|home\s+office|united\s+states|us|usa|u\.s\.|america|national)"
    r"[\s,;:/()\-]*)+$",
    re.I,
)


def _sentences(text: str) -> list[str]:
    # Keep abbreviations and state lists intact; no broad cross-paragraph inference.
    return [s.strip() for s in re.split(r"[\n;]|(?<=[.!?])\s+(?=[A-Z])", text or "") if s.strip()]


def _nyc_location(location: str) -> bool:
    if _NYC.search(location):
        return True
    for part in re.split(r"[;|]|\s/\s", location):
        part = part.strip()
        if re.search(r"\bnew\s+york\s+state\b", part, re.I):
            continue
        if re.search(
            r"(?:^|\b(?:us|usa|united\s+states)[\s,\-]+)new\s+york(?:\s*,\s*(?:ny|new\s+york|us|usa|united\s+states)\b|\s*$|\s*[-(])",
            part,
            re.I,
        ):
            return True
        if re.search(r"\bnew\s+york\s*,\s*(?:ny|new\s+york)\b", part, re.I):
            return True
    return False


def assess_location(job: Job, profile: Profile) -> tuple[str, list[str]]:
    if not profile.strict_nyc_location:
        return "not_assessed", []
    loc = (job.location or "").strip()
    body = job.description or ""
    remote = job.remote is True or bool(_REMOTE.search(loc))
    lines = _sentences(body)
    restrictions = [s for s in lines if _RESIDENCE.search(s) or (_EXCLUDE.search(s) and _NY.search(s))]
    for text in [loc, *restrictions]:
        if _EXCLUDE.search(text) and _NY.search(text):
            # Avoid "New York residents are eligible, except for ..." unrelated benefits sentences.
            if text == loc or re.search(
                r"remote|residen|locat|hir|employ|work|eligible|available|open", text, re.I
            ):
                return "ineligible", ["New York is explicitly excluded: " + text[:250]]
    if not remote:
        if _nyc_location(loc):
            return "eligible", ["Posting lists an NYC workplace"]
        if not loc or loc.lower() in {
            "multiple locations",
            "various locations",
            "united states",
            "usa",
            "us",
        }:
            return "review", ["Workplace location or NYC attendance eligibility is unspecified"]
        return "ineligible", ["No NYC workplace or confirmed remote arrangement: " + loc]

    # Explicit residence restrictions take priority over a generic 'US remote' label.
    for text in restrictions:
        if _EXCLUDE.search(text):
            continue  # exclusions of other states do not establish NY inclusion
        if _NY.search(text) or _NATIONWIDE.search(text):
            return "eligible", ["Remote residence includes New York: " + text[:250]]
        if _RESIDENCE.search(text) and (_OTHER_STATE.search(text) or _FOREIGN.search(text)):
            return "ineligible", ["Remote residence is restricted outside New York: " + text[:250]]
        if _RESIDENCE.search(text) and not _US_SCOPE_ONLY.fullmatch(
            re.sub(r"^.*?\b(?:reside|live|based|located)\s+(?:in|within)\s+", "", text, flags=re.I).rstrip(
                "."
            )
        ):
            return "review", ["Remote residence restriction needs checking for New York: " + text[:250]]
    if _NY.search(loc):
        return "eligible", ["Remote posting explicitly includes New York"]
    remote_terms = [loc, *(s for s in lines if _REMOTE.search(s) or _RESIDENCE.search(s))]
    if any(_NATIONWIDE.search(s) or _WORLDWIDE.search(s) for s in remote_terms):
        return "eligible", ["Posting explicitly permits nationwide or worldwide remote work"]
    if _FOREIGN.search(loc) and not _US.search(loc):
        return "ineligible", ["Remote role is advertised outside the United States: " + loc]
    if _OTHER_STATE.search(loc) and not _US.search(loc):
        return "ineligible", ["Remote role lists a different state: " + loc]
    if _US_SCOPE_ONLY.fullmatch(loc):
        return "review", ["US remote is listed; New York residence eligibility is not explicitly confirmed"]
    return "review", ["Remote work is listed, but New York residence eligibility is unconfirmed"]


def assess_role(job: Job, profile: Profile) -> tuple[str, list[str]]:
    patterns = profile.excluded_role_patterns or []
    if not patterns:
        return "not_assessed", []
    for pattern in patterns:
        match = re.search(pattern, job.title or "", re.I)
        if match:
            return "excluded", ["Excluded job function in title: " + match.group(0)]
    sales_title = re.search(
        r"\b(?:business development|sales development|client solutions|investor relations|capital raising)\b",
        job.title or "",
        re.I,
    )
    sales_function = re.search(
        r"(?:key|primary|main)\s+(?:function|purpose|focus|responsibility)[^.\n]{0,100}\b(?:business development|sales|fundraising|capital raising)\b|"
        r"seeking a business development professional|\bmaintain a high level of prospecting activity\b",
        job.description or "",
        re.I,
    )
    if sales_title or sales_function:
        evidence = (sales_title or sales_function).group(0)
        return "excluded", [
            "Client sales / business development rather than investigative research: " + evidence
        ]
    return "eligible", []
