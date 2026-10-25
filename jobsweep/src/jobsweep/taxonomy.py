"""Lane taxonomy: load ``catalog/lanes.yaml``, classify and score jobs, build keyword search plans.

Matching (ARCHITECTURE.md section 4.2):

* Plain terms are case-insensitive whole-word / whole-phrase matches. Inside a term a space or hyphen
  matches any run of spaces/hyphens, ``&`` and `` and `` are interchangeable, ``'`` is optional and the
  last word also matches its plural (``analyst`` -> ``analysts``, ``policy`` -> ``policies``).
  All-caps acronyms (``OFAC``, ``KYC``, ``AML/KYC``) match case-sensitively so that ``US``/``ADA``/``EAR``
  do not fire on ordinary words.
* ``/.../`` terms are raw Python regexes. A raw regex containing lower-case letters is compiled with
  ``re.IGNORECASE`` (lanes.yaml writes them that way and scopes case with ``(?-i:...)``); an all-caps
  raw regex such as ``/\\bGSOC\\b/`` is case-sensitive unless it starts with ``(?i)``.
* Text is normalised first (NFKC, curly quotes, unicode dashes, no-break spaces).

Speed: every job is tokenised once; an inverted index of "anchor" words picks the few candidate terms
whose words all occur in the text, and only those run their regex (single-word terms need no regex).
Classifying 20k postings with 12k-character descriptions takes a few seconds.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import CATALOG_DIR, Settings
from .models import Employer, Job, Profile, SearchQuery

try:  # Python >= 3.11
    from re import _constants as _sre_c  # type: ignore[attr-defined]
    from re import _parser as _sre_p  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Python 3.10
    import sre_constants as _sre_c  # type: ignore[no-redef]
    import sre_parse as _sre_p  # type: ignore[no-redef]

log = logging.getLogger("jobsweep.taxonomy")

LANES_PATH = CATALOG_DIR / "lanes.yaml"
PROFILE_PATH = CATALOG_DIR / "profile.yaml"

ENTRY_GROUP = "entry-routes"
FEED_SOURCES = frozenset({"nycjobs", "nysjobs"})
# Lanes without a ``usajobs`` mapping still search USAJOBS with their generic queries in these groups.
USAJOBS_FALLBACK_GROUPS = frozenset(
    {
        "government",
        "intelligence-security",
        "sanctions-fincrime",
        "policy-research",
        "geopolitical-risk",
        "investigations-dd",
        "trust-safety",
    }
)
FEED_LIMIT = 5000

# ---- scoring knobs (relevance is on a ~0-100 scale before it is squashed into score points) ----------
KEEP_THRESHOLD = 15.0  # lane kept when relevance >= this ...
DESC_ONLY_THRESHOLD = 18.0  # ... or this when the only evidence is in the description
TITLE_HIT = 40.0  # first distinct lane title term in the job title
TITLE_EXTRA = 10.0  # each further distinct title term (max 2)
PHRASE_IN_TITLE = 20.0
PHRASE_IN_DESC = 12.0
PHRASE_DECAY = (1.0, 0.6, 0.45, 0.35, 0.25, 0.2, 0.15)
PHRASE_CAP = 36.0
CONTEXT_HIT = 4.0
CONTEXT_MAX = 3
PRIOR_LANE = 16.0  # employer.lanes, title plausibly in-domain
PRIOR_GROUP = 6.0  # employer.groups, only on lanes with their own title/phrase evidence
PRIOR_ONLY_MAX = 3  # lanes kept on employer prior alone (no lane evidence)
OFF_DOMAIN_FACTOR = 0.25
DESC_ONLY_FACTOR = 0.6
INACTIVE_FACTOR = 0.6
CYBER_MIN_TERMS = 4
REL_POINTS = 60.0
REL_SCALE = 28.0
ENTRY_POINTS = {True: 20.0, None: 0.0, False: -25.0}
LOC_FULL = 12.0
LOC_PARTIAL = 7.0
LOC_UNKNOWN = 4.0
NO_DOMAIN_CAP = 15.0
MAX_LANES = 8
MAX_REASONS = 8


# =====================================================================================================
# data classes
# =====================================================================================================


@dataclass
class Lane:
    id: str
    name: str
    group: str
    active: bool = True
    title: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    usajobs: dict[str, Any] | None = None
    nycjobs: dict[str, Any] | None = None


@dataclass
class Group:
    id: str
    name: str
    active: bool = True
    lanes: list[Lane] = field(default_factory=list)


# =====================================================================================================
# text helpers
# =====================================================================================================

_UNI_MAP = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "′": "'",
    "ʼ": "'",
    "´": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "″": '"',
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "​": "",
    "­": "",
    "﻿": "",
}
_UNI_RE = re.compile("[" + "".join(_UNI_MAP) + "]")
_ASCII_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_TOKEN_TABLE = bytes((c if (48 <= c <= 57 or 97 <= c <= 122) else 32) for c in range(256))


def normalize_text(text: str | None) -> str:
    """NFKC + ASCII quotes/dashes/spaces. Case is preserved."""
    if not text:
        return ""
    if text.isascii():
        return text
    text = unicodedata.normalize("NFKC", text)
    return _UNI_RE.sub(lambda m: _UNI_MAP[m.group(0)], text)


def _tokens(low: str) -> set[bytes]:
    """Maximal ASCII alnum runs of an already lower-cased text."""
    return set(low.encode("ascii", "replace").translate(_TOKEN_TABLE).split())


def _word_forms(word: str) -> list[str]:
    """The word plus the plural forms a plain term's last word also matches."""
    out = [word]
    if len(word) >= 2 and word[-1].isalpha():
        if len(word) > 2 and word[-1] in "yY" and word[-2].lower() not in "aeiouy":
            out.append(word[:-1] + "ies")
        out += [word + "s", word + "es"]
    return out


def _canon_tokens(term: str) -> tuple[str, ...]:
    """Canonical token tuple used to merge duplicate/overlapping hits ('K & R' == 'k and r')."""
    s = term.lower().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", s)
    out = []
    for w in words:
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return tuple(out)


def _is_sub(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True when ``a`` is a strict contiguous sub-sequence of ``b``."""
    n = len(a)
    if not n or n >= len(b):
        return False
    return any(b[i : i + n] == a for i in range(len(b) - n + 1))


# =====================================================================================================
# term compilation
# =====================================================================================================

_BRK = "<brk>"
_WILD = "<wild>"
_PLURAL = "<plural>"


def _is_break_set(items: Any) -> bool:
    """A regex character set that only matches characters which split ASCII tokens."""
    try:
        for op, av in items:
            if op is _sre_c.LITERAL:
                if chr(av).isascii() and chr(av).isalnum():
                    return False
            elif op is _sre_c.CATEGORY:
                if av not in (_sre_c.CATEGORY_SPACE, _sre_c.CATEGORY_NOT_WORD):
                    return False
            else:
                return False
        return True
    except Exception:  # noqa: BLE001 - unknown parser shapes are treated as wildcards
        return False


def _flatten_regex(items: Any, seq: list[str]) -> None:
    for op, av in items:
        if op is _sre_c.LITERAL:
            seq.append(chr(av))
        elif op is _sre_c.AT:
            seq.append(_WILD if av is _sre_c.AT_NON_BOUNDARY else _BRK)
        elif op is _sre_c.SUBPATTERN:
            _flatten_regex(av[-1], seq)
        elif op in (_sre_c.MAX_REPEAT, _sre_c.MIN_REPEAT):
            lo, hi, item = av
            kind = _WILD
            if len(item) == 1:
                iop, iav = item[0]
                if iop is _sre_c.LITERAL and not (chr(iav).isascii() and chr(iav).isalnum()):
                    kind = _BRK
                elif iop is _sre_c.IN and _is_break_set(iav):
                    kind = _BRK
                elif iop is _sre_c.LITERAL and chr(iav) in "sS" and lo == 0 and hi == 1:
                    kind = _PLURAL
            if kind is _BRK and lo >= 1:
                seq.append(_BRK)
            elif kind is _PLURAL and seq and len(seq[-1]) == 1 and seq[-1].isalnum():
                seq.append(_PLURAL)
            else:
                seq.append(_WILD)
        elif op is _sre_c.IN:
            seq.append(_BRK if _is_break_set(av) else _WILD)
        else:  # BRANCH, ANY, ASSERT, GROUPREF, NOT_LITERAL, CATEGORY, ...
            seq.append(_WILD)


def _regex_requirements(src: str, flags: int) -> tuple[list[tuple[str, bool]], list[str]]:
    """(whole words, substrings) that every match of a raw regex must contain (lower-case)."""
    try:
        tree = _sre_p.parse(src, flags)
        seq: list[str] = []
        _flatten_regex(tree, seq)
    except Exception:  # noqa: BLE001
        return [], []
    words: list[tuple[str, bool]] = []
    subs: list[str] = []
    run, plural, left_brk, prev_brk = "", False, False, False
    for x in [*seq, _WILD]:
        if len(x) == 1 and x.isascii() and x.isalnum():
            if plural:  # letters after an optional 's' -> not a clean token
                if len(run) >= 3:
                    subs.append(run)
                run, plural, prev_brk = "", False, False
            if not run:
                left_brk = prev_brk
            run += x.lower()
            continue
        if x is _PLURAL and run:
            plural = True
            continue
        is_brk = x is _BRK or len(x) == 1  # a non-alnum literal also splits tokens
        if run:
            if left_brk and is_brk:
                words.append((run, plural))
            elif len(run) >= 3:
                subs.append(run)
        run, plural, prev_brk = "", False, is_brk
    return words, subs


def _raw_flags(src: str) -> int:
    if src.startswith("(?i)"):
        return 0  # inline flag already present
    bare = re.sub(r"\\.", "", src)
    bare = re.sub(r"\(\?[a-zA-Z-]+[:)]", "", bare)
    bare = re.sub(r"\[[^\]]*\]", "", bare)
    return re.IGNORECASE if re.search(r"[a-z]", bare) else 0


def _plain_case_sensitive(term: str) -> bool:
    letters = [c for c in term if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def _plain_pattern(term: str, case_sensitive: bool) -> str:
    s = re.sub(r"\s+", " ", term.strip())
    if not case_sensitive:
        s = s.lower()
    s = re.sub(r"\s*&\s*", "\x00", s)
    s = re.sub(r"(?i)(?<=[A-Za-z0-9.)]) and (?=[A-Za-z0-9(])", "\x00", s)
    s = re.sub(r"\s*([-/])\s*", r"\1", s)
    pieces = re.findall(r"[A-Za-z0-9]+|\x00|.", s)
    multi = sum(1 for p in pieces if p[0] in _ASCII_ALNUM) > 1
    out = []
    for i, p in enumerate(pieces):
        if p[0] in _ASCII_ALNUM:
            if i == len(pieces) - 1 and len(p) >= 2:
                forms = sorted(set(_word_forms(p)), key=len, reverse=True)
                out.append("(?:" + "|".join(re.escape(f) for f in forms) + ")")
            else:
                out.append(re.escape(p))
        elif p == "\x00":
            out.append(r"(?:\s*&\s*|\s+and\s+)")
        elif p in " -":
            out.append(r"[\s\-]+")
        elif p == "/":
            out.append(r"\s*/\s*")
        elif p == "'":
            out.append("'?")
        elif p == ".":
            out.append(r"\.?" if multi else r"\.")
        elif p == ",":
            out.append(",?")
        else:
            out.append(re.escape(p))
    tail = r"(?![A-Za-z0-9])" if case_sensitive else r"(?![a-z0-9])"
    return "".join(out) + tail


class Term:
    """One compiled lane term (plain phrase or ``/regex/``)."""

    __slots__ = (
        "idx",
        "raw",
        "regex",
        "case_sensitive",
        "rx",
        "pattern",
        "anchors",
        "subs",
        "canon",
        "token_only",
    )

    def __init__(self, raw: str, idx: int = 0):
        self.idx = idx
        self.raw = raw
        self.regex = len(raw) > 2 and raw.startswith("/") and raw.endswith("/")
        self.anchors: tuple[tuple[bytes, ...], ...] = ()
        self.subs: tuple[str, ...] = ()
        self.token_only = False
        self.rx: re.Pattern[str] | None = None
        self.pattern: str | None = None  # plain terms compile lazily (most never reach the regex stage)
        if self.regex:
            src = raw[1:-1]
            flags = _raw_flags(src)
            self.case_sensitive = not flags and not src.startswith("(?i)")
            self.rx = re.compile(src, flags)
            words, subs = _regex_requirements(src, flags)
            self.anchors = tuple(
                tuple(f.encode() for f in (_word_forms(w) if plural else [w])) for w, plural in words
            )
            self.subs = tuple(dict.fromkeys(subs))
            self.canon: tuple[str, ...] = ("re:" + src,)
            return
        self.case_sensitive = _plain_case_sensitive(raw)
        self.canon = _canon_tokens(raw)
        words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", raw)]
        joined = re.sub(r"\s+", " ", raw.strip())
        single = len(words) == 1 and re.fullmatch(r"[A-Za-z0-9]+", joined) is not None
        if single and not self.case_sensitive:
            # one plain word: the token set decides exactly, no regex needed
            self.token_only = True
            self.anchors = (tuple(f.encode() for f in _word_forms(words[0])),)
            return
        self.pattern = _plain_pattern(raw, self.case_sensitive)
        if re.search(r"(?i)\band\b|&", raw):
            words = [w for w in words if w != "and"]
        long_words = [w for w in words if len(w) >= 2]
        chosen = list(dict.fromkeys(long_words or words))
        self.anchors = tuple(tuple(f.encode() for f in dict.fromkeys(_word_forms(w))) for w in chosen)

    # -- matching ----------------------------------------------------------------------------------
    def find(self, low: str, norm: str, toks: set[bytes]) -> str | None:
        """Matched text (or the term itself) when the term occurs; prefilter included."""
        for alt in self.anchors:
            if not any(v in toks for v in alt):
                return None
        if self.subs and not all(s in low for s in self.subs):
            return None
        if self.token_only:
            return self.raw
        return self._regex_find(low, norm)

    def _regex_find(self, low: str, norm: str) -> str | None:
        rx = self.rx
        if rx is None:
            if self.pattern is None:
                return None
            rx = self.rx = re.compile(self.pattern)
        if self.regex:
            m = rx.search(norm)
            return m.group(0).strip() if m else None
        text = norm if self.case_sensitive else low
        for m in rx.finditer(text):
            s = m.start()
            if s == 0 or text[s - 1] not in _ASCII_ALNUM:
                return self.raw
        return None

    def search(self, text: str) -> str | None:
        """Convenience matcher for one text (tests, debugging)."""
        norm = normalize_text(text)
        low = norm.lower()
        return self.find(low, norm, _tokens(low))

    def search_slow(self, text: str) -> str | None:
        """Same as ``search`` without the token prefilter (tests check both agree)."""
        norm = normalize_text(text)
        low = norm.lower()
        if self.token_only:
            word = self.anchors[0][0].decode()
            rx = re.compile(_plain_pattern(word, False))
            for m in rx.finditer(low):
                if m.start() == 0 or low[m.start() - 1] not in _ASCII_ALNUM:
                    return self.raw
            return None
        return self._regex_find(low, norm)

    def __repr__(self) -> str:
        return f"Term({self.raw!r})"


def compile_term(raw: str) -> Term:
    """Compile one lanes.yaml term (exposed for tests and debugging)."""
    return Term(str(raw))


class _Text:
    __slots__ = ("norm", "low", "toks")

    def __init__(self, text: str | None):
        self.norm = normalize_text(text)
        self.low = self.norm.lower()
        self.toks = _tokens(self.low)


# =====================================================================================================
# title vocabularies (generic family, off-domain, seniority, entry level)
# =====================================================================================================

_GENERIC_RE = re.compile(
    r"\b(?:analys[ti]s?|analysis|research\w*|investigat\w*|intelligence|underwrit\w*|brok(?:er|ers|ing|erage)"
    r"|risks?|polic(?:y|ies)|compliance|program(?:me)?s?|associates?|assistants?|fellow(?:s|ship)?"
    r"|coordinator|consultant|advis[eo]r[sy]?|aide|trainee|graduate|scholar|sanctions|diligence)\b",
    re.I,
)

_OFF_DOMAIN_RE = re.compile(
    r"""\b(?:
      engineer(?:s|ing)?|developer|programmer|swe|sre|devops|dev\s*ops|mlops|data\s+scien\w*
      |machine\s+learning|deep\s+learning|computer\s+vision|full[\s-]?stack|front[\s-]?end|back[\s-]?end
      |data\s+analyst|business\s+analyst|bi\s+analyst|business\s+intelligence|financial\s+analyst|fp&a
      |financial\s+planning|pricing\s+analyst|marketing\s+analyst
      |accountant|accounting|bookkeep\w*|payroll|accounts\s+(?:payable|receivable)|tax|controller|actuar\w*
      |sales|salesperson|account\s+executive|account\s+manager|business\s+development|customer\s+success
      |customer\s+service|customer\s+support|client\s+success|inside\s+sales|sdr|bdr|retail|cashier
      |merchandis\w*|store\s+manager
      |recruit(?:er|ers|ing)|talent\s+acquisition|sourcer|human\s+resources|hr\s+(?:business\s+partner
      |generalist|manager|coordinator|assistant|specialist|partner)|people\s+(?:partner|operations)
      |nurse|nursing|rn|lpn|physician|doctor|surgeon|dentist|dental|pharmac\w*|therapist|clinician|clinical
      |medical\s+(?:assistant|director|coder|biller)|phlebotom\w*|radiolog\w*|veterinar\w*|psychiatr\w*
      |driver|mechanic|technician|electrician|plumber|carpenter|welder|machinist|hvac|janitor|custodian
      |housekeep\w*|cleaner|landscap\w*|groundskeeper|facilities|maintenance|warehouse|forklift|picker|packer
      |chef|cook|kitchen|dishwasher|bartender|barista|food\s+service
      |marketing|graphic\s+designer|designer|ux|ui|copywriter|product\s+manager|product\s+owner
      |product\s+management|product\s+marketing|scrum\s+master|it\s+support|help\s*desk|service\s+desk
      |desktop\s+support|sys(?:tems?)?\s*admin\w*|database\s+administrator|dba|network\s+administrator
      |teacher|tutor|teaching\s+assistant|receptionist|administrative\s+assistant|executive\s+assistant
      |office\s+manager|office\s+assistant|front\s+desk|mailroom|data\s+entry|security\s+guard
      |security\s+officer|fundrais\w*|donor|development\s+(?:associate|officer|coordinator|assistant)
    )\b""",
    re.I | re.X,
)

# Remove "assistant to the Director", "Office of the Chief ..." before looking for senior words.
_SENIOR_STRIP_RE = re.compile(
    r"\b(?:to|for)\s+(?:the\s+)?(?:[a-z]+\s+){0,3}?(?:managing\s+director|director|chief|president|ceo|cfo|coo"
    r"|partner|commissioner|head|vice\s+president|vp)s?\b"
    r"|\boffice\s+of\s+(?:the\s+)?(?:[a-z]+\s+){0,3}?(?:director|chief|commissioner|president|mayor)\b"
    r"|\bdirector\s+of\s+national\s+intelligence\b",
    re.I,
)
_SENIOR_RE = re.compile(
    r"\b(?:senior|sr\.?|snr|lead|principal|director|head|vp|svp|evp|avp|vice\s+president|chief|managing"
    r"|manager|executive\s+director|supervisor[y]?|superintendent|commissioner|professor|postdoc\w*"
    r"|post-doctoral|nonresident|non-resident|distinguished|visiting\s+fellow"
    r"|partner(?!\s+(?:marketing|success|programs?|relations|engagement|operations|support|enablement))"
    r"|staff\s+(?:software\s+|data\s+|security\s+|ml\s+|machine\s+learning\s+|research\s+)?"
    r"(?:engineer|scientist|researcher|designer|architect)"
    r"|(?:analyst|associate|specialist|investigator|consultant|officer|underwriter|researcher|engineer"
    r"|level)\s*(?:iii|iv|v|3|4|5))\b",
    re.I,
)
_CASE_MANAGER_RE = re.compile(r"\bcase\s+manager\b", re.I)
_LAWYER_RE = re.compile(r"\b(?:counsel|attorney|lawyer|solicitor|barrister)s?\b", re.I)
_BAR_RE = re.compile(
    r"\b(?:j\.\s?d\.|juris doctor|bar admission|admitted to (?:practice|the bar)|member (?:in good standing )?of"
    r" the [a-z ]{0,30}bar)",
    re.I,
)
_MID_RE = re.compile(
    r"\b(?:analyst|associate|specialist|investigator|consultant|officer|underwriter|researcher|level)\s*(?:ii|2)\b",
    re.I,
)
_ENTRY_RE = re.compile(
    r"\b(?:(?:analyst|specialist|investigator|associate|officer|coordinator|researcher|underwriter|level)"
    r"\s*(?:i|1)(?![\w-])|analyst|associate|assistant|coordinator|graduate|junior|jr\.?|entry[\s-]*level|entry"
    r"|early[\s-]*careers?|fellow(?:ship)?s?|trainee|rotational|apprentice(?:ship)?|aide|pathways"
    r"|recent\s+grad(?:uate)?s?|new\s+grad(?:uate)?s?|legislative\s+correspondent)\b",
    re.I,
)
_INTERN_RE = re.compile(
    r"\b(?:interns?|internships?|summer\s+(?:analyst|associate|intern)s?|co-op|placement\s+year"
    r"|industrial\s+placement|student\s+(?:trainee|assistant|worker|aide|intern))\b",
    re.I,
)
_ENTRY_TAGS = (
    "pathways",
    "recent-graduates",
    "entry-level",
    "fellowship",
    "graduate-program",
    "early-career",
    "new-grad",
)
_INTERN_TAGS = ("internship", "student")

# ---- years of experience ------------------------------------------------------------------------
_NUM_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
_NUM = r"(?:\d{1,2}|" + "|".join(_NUM_WORDS) + r")"
_YEAR_WORD_RE = re.compile(r"\b(?:years?|yrs?)\b")
_YEARS_BEFORE_RE = re.compile(
    r"(?<![\w.,/])(?P<a>" + _NUM + r")(?:\s*\(\s*\d{1,2}\s*\))?\s*(?:\+|plus)?\s*"
    r"(?:(?:-|to|or)\s*(?P<b>" + _NUM + r")(?:\s*\(\s*\d{1,2}\s*\))?\s*\+?\s*)?"
    r"(?:full[\s-]+)?(?:or\s+more\s+)?$"
)
_YEARS_AFTER_OK_RE = re.compile(
    r"^'?\s*(?:of\s+)?(?:[\w&/,()'-]+\s+){0,7}?(?:experience|work|working|employment|practice)\b"
)
_YEARS_AFTER_BAD_RE = re.compile(
    r"^'?\s*(?:-?\s*)?(?:old|of\s+age|ago|of\s+(?:history|service|operation|growth)|in\s+business|anniversary"
    r"|degree|program|programme|college|university|bachelor|fellowship|term|contract|appointment|assignment"
    r"|period|commitment|rotation|scheme|course|cycle|plan|budget|grant|warranty|since|after|from\s+(?:the\s+)?"
    r"(?:date|graduation)|of\s+(?:graduation|college|university|study|school|undergraduate|graduate))\b"
)
_YEARS_PRE_OK_RE = re.compile(
    r"\b(?:minimum|min\.?|at\s+least|require[sd]?|requirement|must\s+have|possess|need|ideally|preferred|plus"
    r"|experience)\b"
)
_YEARS_PRE_BAD_RE = re.compile(
    r"\b(?:within|founded|since|established|celebrat\w*|history|legacy|combined|in\s+business|last|past|next"
    r"|first|every|per|we've|we\s+have\s+been|our\s+(?:team|firm|company|founders?|experts?|professionals"
    r"|consultants|people|staff|organi[sz]ation))\b"
)
_MAX_YEARS = 25

# ---- federal grades -------------------------------------------------------------------------------
_GRADE_PLAN_RE = re.compile(r"\b(?:GS|GG|GL|GM)\b", re.I)

# ---- cyber postings (veto lanes that exclude cyber) ----------------------------------------------------
_CYBER_TOKENS = frozenset(
    t.encode()
    for t in (
        "siem",
        "splunk",
        "edr",
        "xdr",
        "soar",
        "malware",
        "firewall",
        "firewalls",
        "phishing",
        "crowdstrike",
        "sentinelone",
        "qradar",
        "wireshark",
        "ransomware",
        "tcp",
        "nmap",
        "metasploit",
        "cybersecurity",
        "infosec",
        "pentest",
        "pentesting",
    )
)
_CYBER_PHRASES = (
    "threat hunting",
    "penetration testing",
    "vulnerability management",
    "vulnerability scanning",
    "endpoint detection",
    "intrusion detection",
    "network security",
    "security information and event",
    "mitre att&ck",
    "log analysis",
    "network traffic",
)

# ---- locations ----------------------------------------------------------------------------------
_NYC_FULL_RE = re.compile(
    r"\b(?:nyc|new\s+york\s+city|manhattan|brooklyn|queens|bronx|staten\s+island|long\s+island\s+city"
    r"|new\s+york\s*,\s*(?:ny|new\s+york)\b)|^\s*new\s+york\s*(?:,\s*(?:usa?|united\s+states))?\s*$",
    re.I,
)
_NY_STATE_RE = re.compile(
    r"\b(?:new\s+york|ny|jersey\s+city|hoboken|newark|white\s+plains|westchester|stamford)\b", re.I
)
_DC_FULL_RE = re.compile(
    r"\b(?:washington\s*,?\s*d\.?\s?c\b|d\.c\.|dc|district\s+of\s+columbia|arlington|alexandria|bethesda|mclean"
    r"|reston|tysons|chantilly|fairfax|herndon|falls\s+church|silver\s+spring|rockville|chevy\s+chase"
    r"|college\s+park|fort\s+meade|crystal\s+city|pentagon|national\s+harbor|suitland|greenbelt"
    r"|gaithersburg|dmv|national\s+capital\s+region)\b",
    re.I,
)
_WASHINGTON_RE = re.compile(r"\bwashington\b", re.I)
_WA_STATE_RE = re.compile(
    r"\b(?:wa|washington\s+state|seattle|spokane|tacoma|bellevue|redmond|olympia|everett|kirkland)\b", re.I
)
_DC_NEAR_RE = re.compile(r"\b(?:virginia|maryland|va|md|baltimore|annapolis|richmond)\b", re.I)
_NOT_DMV_STATE_RE = re.compile(
    r",\s*(?:tx|texas|ma|massachusetts|ca|california|il|illinois|ky|kentucky|on|ontario|tn|ohio|oh)\b", re.I
)
_LONDON_RE = re.compile(r"\blondon\b", re.I)
_NOT_LONDON_RE = re.compile(
    r"\b(?:ontario|new\s+london|londonderry)\b|london\s*,\s*(?:on|ky|oh|ct|ontario|canada)\b", re.I
)
_UK_RE = re.compile(r"\b(?:uk|united\s+kingdom|england|great\s+britain|gb|scotland|wales)\b", re.I)
_REMOTE_RE = re.compile(
    r"\b(?:remote|anywhere|work\s+from\s+home|wfh|telework\w*|virtual|home[\s-]based|distributed)\b", re.I
)
_FOREIGN_RE = re.compile(
    r"\b(?:canada|india|mexico|brazil|philippines|germany|france|spain|portugal|poland|netherlands|ireland"
    r"|singapore|australia|japan|china|hong\s+kong|emea|apac|latam|europe|asia|africa|argentina|colombia"
    r"|romania|ukraine|israel|uae|dubai)\b",
    re.I,
)


def _loc_parts(location: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[;|]|\s/\s|\n", location) if p.strip()]
    return parts or ([location.strip()] if location.strip() else [])


# =====================================================================================================
# profile loading
# =====================================================================================================


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping")
    if isinstance(doc.get("profile"), dict):
        doc = doc["profile"]
    return dict(doc)


def load_profile(profile: Profile | str | Path | None = None) -> Profile:
    """Profile from ``catalog/profile.yaml`` overlaid with ``profile`` (path) or ``JOBSWEEP_PROFILE``."""
    if isinstance(profile, Profile):
        return profile
    data: dict[str, Any] = {}
    if PROFILE_PATH.exists():
        try:
            data.update(_read_yaml_dict(PROFILE_PATH))
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot read %s: %s", PROFILE_PATH, exc)
    override: Path | None = Path(profile).expanduser() if profile is not None else None
    if override is None:
        try:
            override = Settings.load().profile_path
        except Exception:  # noqa: BLE001
            override = None
    if override is not None and override.resolve() != PROFILE_PATH.resolve():
        if override.exists():
            data.update(_read_yaml_dict(override))
        else:
            log.warning("profile file %s not found; using defaults", override)
    if isinstance(data.get("preferred_locations"), str):
        data["preferred_locations"] = [data["preferred_locations"]]
    if isinstance(data.get("countries"), str):
        data["countries"] = [data["countries"]]
    return Profile.from_dict(data)


# =====================================================================================================
# taxonomy
# =====================================================================================================


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return [str(value)]


@dataclass
class _Ev:
    """Per-lane evidence gathered for one job."""

    title: list[int] = field(default_factory=list)
    phrase_title: list[int] = field(default_factory=list)
    phrase_desc: list[int] = field(default_factory=list)
    context: list[int] = field(default_factory=list)
    exclude: int | None = None


@dataclass
class _LaneResult:
    li: int
    rel: float
    own: bool  # title/phrase evidence of its own
    title_hit: bool
    prior: bool
    reason: str


class Taxonomy:
    """Lanes + candidate profile: classify/score jobs and plan keyword searches."""

    def __init__(self, groups: list[Group], profile: Profile | None = None, path: Path | None = None):
        self.groups: list[Group] = groups
        self.profile: Profile = profile or Profile()
        self.path = path
        self.lanes: dict[str, Lane] = {}
        self._group_by_id: dict[str, Group] = {g.id: g for g in groups}
        for g in groups:
            for lane in g.lanes:
                self.lanes[lane.id] = lane
        self._compile()
        self._compile_locations()

    # ---- loading --------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None, profile: Profile | str | Path | None = None) -> Taxonomy:
        p = Path(path) if path else LANES_PATH
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        doc = yaml.load(p.read_text(encoding="utf-8"), Loader=loader) or {}  # noqa: S506 - safe loader
        return cls.from_dict(doc, profile=load_profile(profile), path=p)

    @classmethod
    def from_dict(
        cls, doc: dict[str, Any], profile: Profile | str | Path | None = None, path: Path | None = None
    ) -> Taxonomy:
        if not isinstance(doc, dict):
            raise ValueError("lanes document must be a mapping with a 'groups' list")
        groups: list[Group] = []
        seen: set[str] = set()
        for g in doc.get("groups") or []:
            if not isinstance(g, dict) or not g.get("id"):
                log.warning("skipping malformed group %r", g)
                continue
            gid = str(g["id"])
            group = Group(id=gid, name=str(g.get("name") or gid), active=g.get("active", True) is not False)
            for raw in g.get("lanes") or []:
                if not isinstance(raw, dict) or not raw.get("id"):
                    log.warning("skipping malformed lane in group %s: %r", gid, raw)
                    continue
                lid = str(raw["id"])
                if lid in seen:
                    log.warning("duplicate lane id %s ignored", lid)
                    continue
                seen.add(lid)
                usaj = raw.get("usajobs")
                nycj = raw.get("nycjobs")
                group.lanes.append(
                    Lane(
                        id=lid,
                        name=str(raw.get("name") or lid),
                        group=gid,
                        active=raw.get("active", True) is not False,
                        title=_str_list(raw.get("title")),
                        phrases=_str_list(raw.get("phrases")),
                        context=_str_list(raw.get("context")),
                        exclude=_str_list(raw.get("exclude")),
                        queries=_str_list(raw.get("queries")),
                        usajobs=dict(usaj) if isinstance(usaj, dict) else None,
                        nycjobs=dict(nycj) if isinstance(nycj, dict) else None,
                    )
                )
            groups.append(group)
        prof = profile if isinstance(profile, Profile) else load_profile(profile)
        return cls(groups, prof, path)

    # ---- simple accessors -----------------------------------------------------------------------
    def lane(self, lane_id: str) -> Lane | None:
        return self.lanes.get(lane_id)

    def group(self, group_id: str) -> Group | None:
        return self._group_by_id.get(group_id)

    def is_active(self, lane_id: str) -> bool:
        lane = self.lanes.get(lane_id)
        if lane is None:
            return False
        g = self._group_by_id.get(lane.group)
        return bool(lane.active and (g is None or g.active))

    def active_lanes(self) -> list[Lane]:
        return [lane for g in self.groups if g.active for lane in g.lanes if lane.active]

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "active": g.active,
                    "lanes": [
                        {
                            "id": lane.id,
                            "name": lane.name,
                            "active": lane.active,
                            "group": lane.group,
                            "queries": list(lane.queries),
                        }
                        for lane in g.lanes
                    ],
                }
                for g in self.groups
            ]
        }

    def __repr__(self) -> str:
        return f"Taxonomy({len(self.groups)} groups, {len(self.lanes)} lanes, {len(self._terms)} terms)"

    # ---- compilation ----------------------------------------------------------------------------
    def _compile(self) -> None:
        self._lane_list: list[Lane] = list(self.lanes.values())
        self._lane_idx: dict[str, int] = {lane.id: i for i, lane in enumerate(self._lane_list)}
        self._terms: list[Term] = []
        by_raw: dict[str, int] = {}
        self._roles: list[list[tuple[int, str]]] = []
        cyber_lanes: set[int] = set()

        def term_id(raw: str) -> int | None:
            if raw in by_raw:
                return by_raw[raw]
            try:
                term = Term(raw, len(self._terms))
            except re.error as exc:
                log.warning("bad lane term %r skipped: %s", raw, exc)
                return None
            by_raw[raw] = term.idx
            self._terms.append(term)
            self._roles.append([])
            return term.idx

        for li, lane in enumerate(self._lane_list):
            for cat, terms in (("T", lane.title), ("P", lane.phrases), ("C", lane.context), ("X", lane.exclude)):
                for raw in terms:
                    tid = term_id(raw)
                    if tid is None:
                        continue
                    if (li, cat) not in self._roles[tid]:
                        self._roles[tid].append((li, cat))
                    if cat == "X" and self._terms[tid].canon in (("cyber",), ("cybersecurity",)):
                        cyber_lanes.add(li)
        self._cyber_lanes = frozenset(cyber_lanes)

        # inverted index on each term's most distinctive anchor word
        self._title_index: dict[bytes, list[int]] = {}
        self._body_index: dict[bytes, list[int]] = {}
        self._title_always: list[int] = []
        self._body_always: list[int] = []
        for term in self._terms:
            body_role = any(cat in ("P", "C") for _, cat in self._roles[term.idx])
            if term.anchors:
                key = max(term.anchors, key=lambda alt: (len(alt[0]), -term.anchors.index(alt)))
                for v in key:
                    self._title_index.setdefault(v, []).append(term.idx)
                    if body_role:
                        self._body_index.setdefault(v, []).append(term.idx)
            else:
                self._title_always.append(term.idx)
                if body_role:
                    self._body_always.append(term.idx)
        self._title_keys = frozenset(self._title_index)
        self._body_keys = frozenset(self._body_index)

    def _compile_locations(self) -> None:
        self._loc_prefs: list[tuple[str, str]] = []  # (kind, label)
        for raw in self.profile.preferred_locations or []:
            label = str(raw).strip()
            key = label.lower()
            if not key:
                continue
            if key.startswith("new york") or key in ("nyc", "ny"):
                kind = "ny"
            elif "washington" in key or key in ("dc", "d.c."):
                kind = "dc"
            elif "london" in key or key in ("uk", "united kingdom"):
                kind = "london"
            elif key in ("remote", "anywhere", "work from home"):
                kind = "remote"
            else:
                kind = "re:" + key
            self._loc_prefs.append((kind, label))

    # ---- matching -------------------------------------------------------------------------------
    def _match(self, text: _Text, title: bool) -> dict[int, str]:
        index, keys, always = (
            (self._title_index, self._title_keys, self._title_always)
            if title
            else (self._body_index, self._body_keys, self._body_always)
        )
        cands: set[int] = set(always)
        for v in text.toks & keys:
            cands.update(index[v])
        out: dict[int, str] = {}
        terms = self._terms
        for tid in cands:
            hit = terms[tid].find(text.low, text.norm, text.toks)
            if hit is not None:
                out[tid] = hit
        return out

    def _distinct(self, tids: list[int]) -> list[int]:
        """Drop hits that duplicate or are contained in another hit of the same category."""
        if len(tids) < 2:
            return list(tids)
        terms = self._terms
        kept: list[int] = []
        seen: set[tuple[str, ...]] = set()
        for t in tids:
            c = terms[t].canon
            if c in seen:
                continue
            if any(_is_sub(c, terms[o].canon) for o in tids if o != t):
                continue
            seen.add(c)
            kept.append(t)
        return kept

    # ---- title helpers --------------------------------------------------------------------------
    @staticmethod
    def _senior(title: str) -> str | None:
        stripped = _SENIOR_STRIP_RE.sub(" ", title)
        for m in _SENIOR_RE.finditer(stripped):
            word = m.group(0)
            if word.lower() == "manager" and _CASE_MANAGER_RE.search(stripped):
                continue
            return word
        return None

    def _employer_prior_ok(self, title: _Text, title_hits: dict[int, str], off: bool) -> bool:
        if off:
            return False
        if any(cat != "X" for tid in title_hits for _, cat in self._roles[tid]):
            return True
        return _GENERIC_RE.search(title.norm) is not None

    def _evidence(self, title_hits: dict[int, str], body_hits: dict[int, str]) -> dict[int, _Ev]:
        ev: dict[int, _Ev] = {}
        for tid in sorted(title_hits):
            for li, cat in self._roles[tid]:
                e = ev.get(li) or ev.setdefault(li, _Ev())
                if cat == "T":
                    e.title.append(tid)
                elif cat == "P":
                    e.phrase_title.append(tid)
                elif cat == "C":
                    e.context.append(tid)
                elif e.exclude is None:
                    e.exclude = tid
        for tid in sorted(body_hits):
            if tid in title_hits:
                continue
            for li, cat in self._roles[tid]:
                if cat == "P":
                    (ev.get(li) or ev.setdefault(li, _Ev())).phrase_desc.append(tid)
                elif cat == "C":
                    (ev.get(li) or ev.setdefault(li, _Ev())).context.append(tid)
        return ev

    # ---- lane scoring ---------------------------------------------------------------------------
    def _score_lanes(
        self,
        title: _Text,
        title_hits: dict[int, str],
        body_hits: dict[int, str],
        employer: Employer | None,
        cyber: bool,
        notes: dict[str, Any],
    ) -> list[_LaneResult]:
        off_m = _OFF_DOMAIN_RE.search(title.norm)
        off = off_m is not None
        plausible = self._employer_prior_ok(title, title_hits, off)
        notes["off_domain"] = off_m.group(0) if off_m else None
        ev = self._evidence(title_hits, body_hits)
        emp_lanes: list[int] = []
        emp_groups: set[str] = set()
        if employer is not None:
            emp_lanes = [self._lane_idx[x] for x in employer.lanes or [] if x in self._lane_idx]
            emp_groups = set(employer.groups or [])
            if plausible:
                for li in emp_lanes:
                    if self._lane_list[li].group != ENTRY_GROUP:
                        ev.setdefault(li, _Ev())
        emp_lane_set = set(emp_lanes)
        terms = self._terms
        results: list[_LaneResult] = []
        vetoed: list[tuple[int, str]] = []
        for li in sorted(ev):
            e = ev[li]
            lane = self._lane_list[li]
            has_ev = bool(e.title or e.phrase_title or e.phrase_desc)
            if e.exclude is not None:
                if has_ev:
                    vetoed.append((li, "excluded: '" + title_hits.get(e.exclude, terms[e.exclude].raw) + "'"))
                continue
            if cyber and li in self._cyber_lanes:
                if has_ev:
                    vetoed.append((li, "cyber posting"))
                continue
            t_hits = self._distinct(e.title)
            phrases = self._distinct(e.phrase_title + e.phrase_desc)
            in_title = set(e.phrase_title)
            ctx = self._distinct(e.context)
            lane_prior = plausible and li in emp_lane_set and lane.group != ENTRY_GROUP
            group_prior = plausible and not lane_prior and lane.group in emp_groups
            own = bool(t_hits or phrases)
            if not own and not lane_prior:
                continue
            rel = 0.0
            if t_hits:
                rel += TITLE_HIT + TITLE_EXTRA * min(len(t_hits) - 1, 2)
            if phrases:
                weights = sorted(
                    (PHRASE_IN_TITLE if t in in_title else PHRASE_IN_DESC for t in phrases), reverse=True
                )
                p = sum(w * PHRASE_DECAY[min(i, len(PHRASE_DECAY) - 1)] for i, w in enumerate(weights))
                rel += min(PHRASE_CAP, p)
            rel += CONTEXT_HIT * min(len(ctx), CONTEXT_MAX)
            if lane_prior:
                rel += PRIOR_LANE
            elif group_prior and own:
                rel += PRIOR_GROUP
            title_ev = bool(t_hits) or any(t in in_title for t in phrases)
            if off and not t_hits:
                rel *= OFF_DOMAIN_FACTOR
            elif not title_ev and not lane_prior and not plausible:
                rel *= DESC_ONLY_FACTOR
            threshold = KEEP_THRESHOLD if (title_ev or lane_prior) else DESC_ONLY_THRESHOLD
            if rel < threshold:
                continue
            if t_hits:
                reason = f"title: '{title_hits[t_hits[0]]}' → {lane.id}"
            elif phrases and phrases[0] in in_title:
                reason = f"title: '{title_hits[phrases[0]]}' → {lane.id}"
            elif phrases:
                reason = f"phrase: '{body_hits.get(phrases[0], terms[phrases[0]].raw)}' → {lane.id}"
            else:
                reason = f"employer prior: {employer.name if employer else '?'} → {lane.id}"
            results.append(_LaneResult(li, rel, own, bool(t_hits), lane_prior, reason))

        # prior-only lanes: only when no domain lane has title evidence; at most PRIOR_ONLY_MAX
        if any(r.title_hit and self._lane_list[r.li].group != ENTRY_GROUP for r in results):
            results = [r for r in results if r.own]
        else:
            prior_only = [r for r in results if not r.own]
            if len(prior_only) > PRIOR_ONLY_MAX:
                order = {li: i for i, li in enumerate(emp_lanes)}
                prior_only.sort(key=lambda r: (-r.rel, order.get(r.li, 99)))
                drop = {r.li for r in prior_only[PRIOR_ONLY_MAX:]}
                results = [r for r in results if r.li not in drop]
        notes["vetoed"] = vetoed
        notes["plausible"] = plausible
        notes["group_prior"] = bool(
            plausible
            and employer is not None
            and any(self._lane_list[r.li].group in emp_groups and not r.prior for r in results)
        )

        def rank(r: _LaneResult) -> tuple[int, float, int]:
            lane = self._lane_list[r.li]
            cat = 2 if lane.group == ENTRY_GROUP else (0 if self.is_active(lane.id) else 1)
            return (cat, -round(r.rel, 6), r.li)

        results.sort(key=rank)
        return results[:MAX_LANES]

    # ---- entry level ----------------------------------------------------------------------------
    def _entry(
        self, job: Job, title: _Text, body_low: str, entry_lanes: list[str]
    ) -> tuple[bool | None, int | None, list[str]]:
        prof = self.profile
        reasons: list[str] = []
        years = parse_years_required(body_low, lowered=True)
        tags = {str(t).lower() for t in job.tags or []}
        etype = (job.employment_type or "").lower()
        level = str(job.extra.get("career_level") or "") if isinstance(job.extra, dict) else ""
        seniority = " ".join(x for x in (level, job.seniority or "") if x).lower()

        intern_m = _INTERN_RE.search(title.norm)
        is_intern = bool(
            intern_m
            or etype == "internship"
            or tags.intersection(_INTERN_TAGS)
            or re.search(r"\b(?:intern(?:ship)?|student)\b", seniority)
        )
        if is_intern:
            if not prof.include_internships:
                return False, years, ["internship"]
            reasons.append("entry: internship")

        senior = self._senior(title.norm)
        if senior:
            return False, years, [f"senior: '{senior}' in title"]
        lawyer = _LAWYER_RE.search(title.norm)
        if lawyer:
            return False, years, [f"lawyer role: '{lawyer.group(0)}' in title"]
        if re.search(r"\bassociate\b", title.low) and _BAR_RE.search(body_low):
            return False, years, ["lawyer role: bar admission required"]

        grades = _federal_grades(job)
        if grades and min(grades) >= 11:
            return False, years, [f"senior: grade GS-{min(grades):02d}"]
        if years is not None and years > prof.max_years_experience:
            return False, years, [f"requires {years}+ years (max {prof.max_years_experience})"]
        if re.search(r"\b(?:director|executive|vp|vice president|c-level)\b", seniority) or re.search(
            r"(?<!non-)(?<!non )\bmanager\b", level.lower()
        ):
            return False, years, [f"senior: level '{level or job.seniority}'"]

        entry_m = None if _MID_RE.search(title.norm) else _ENTRY_RE.search(title.norm)
        if entry_m:
            reasons.append(f"entry: '{entry_m.group(0)}' in title")
        for tag in _ENTRY_TAGS:
            if tag in tags:
                reasons.append(f"entry: tag '{tag}'")
                break
        if grades and min(grades) <= 9:
            reasons.append(f"entry: grade GS-{min(grades):02d}")
        if re.search(r"\b(?:entry|junior|graduate|early)", seniority):
            reasons.append(f"entry: level '{level or job.seniority}'")
        if entry_lanes:
            reasons.append(f"entry: {entry_lanes[0]} lane")
        if years is not None:
            reasons.append(f"entry: {years} years required (max {prof.max_years_experience})")
        return (True if reasons else None), years, reasons

    # ---- location / recency -----------------------------------------------------------------------
    def _location(self, job: Job) -> tuple[float, str | None]:
        loc = normalize_text(job.location or "")
        prefs = self._loc_prefs
        remote_pref = any(kind == "remote" for kind, _ in prefs)
        best, label = 0.0, None

        def take(points: float, text: str) -> None:
            nonlocal best, label
            if points > best:
                best, label = points, text

        if job.remote is True and (self.profile.remote_ok or remote_pref):
            take(LOC_FULL - (0 if remote_pref else 1), "location: remote")
        for rank, (kind, name) in enumerate(prefs):
            full = LOC_FULL - min(rank, 2)
            for part in _loc_parts(loc):
                if kind == "ny":
                    if _NYC_FULL_RE.search(part):
                        take(full, f"location: {name}")
                    elif _NY_STATE_RE.search(part):
                        take(LOC_PARTIAL, f"location: {name} (state)")
                elif kind == "dc":
                    if _DC_FULL_RE.search(part) and not _NOT_DMV_STATE_RE.search(part):
                        take(full, f"location: {name}")
                    elif _WASHINGTON_RE.search(part) and not _WA_STATE_RE.search(part):
                        take(full, f"location: {name}")
                    elif _DC_NEAR_RE.search(part):
                        take(LOC_PARTIAL, f"location: near {name}")
                elif kind == "london":
                    if _LONDON_RE.search(part) and not _NOT_LONDON_RE.search(part):
                        take(full, f"location: {name}")
                    elif _UK_RE.search(part):
                        take(LOC_PARTIAL, f"location: {name} (UK)")
                elif kind == "remote":
                    if _REMOTE_RE.search(part):
                        if _FOREIGN_RE.search(part) and not re.search(
                            r"\b(?:us|usa|united\s+states|uk|united\s+kingdom)\b", part, re.I
                        ):
                            take(3.0, "location: remote (outside US/UK)")
                        else:
                            take(full, "location: remote")
                elif re.search(r"\b" + re.escape(kind[3:]) + r"\b", part, re.I):
                    take(full, f"location: {name}")
        if best == 0.0 and not loc.strip() and job.remote is None:
            return LOC_UNKNOWN, None
        return best, label

    @staticmethod
    def _recency(posted_at: str | None, now: datetime) -> tuple[float, int | None]:
        d = _parse_date(posted_at)
        if d is None:
            return 2.0, None
        age = max(0, (now.date() - d).days)
        if age <= 2:
            return 8.0, age
        if age <= 7:
            return 6.0, age
        if age <= 14:
            return 4.0, age
        if age <= 30:
            return 2.0, age
        return 0.0, age

    # ---- public API -----------------------------------------------------------------------------
    def classify(self, job: Job, employer: Employer | None = None, *, now: datetime | None = None) -> Job:
        """Fill the classification fields of ``job`` in place and return it."""
        now = now or datetime.now(timezone.utc)
        title = _Text(job.title or "")
        body = _Text(_body_text(job))
        title_hits = self._match(title, True)
        body_hits = self._match(body, False)
        cyber = bool(self._cyber_lanes) and _is_cyber(body)
        notes: dict[str, Any] = {}
        results = self._score_lanes(title, title_hits, body_hits, employer, cyber, notes)

        lane_ids = [self._lane_list[r.li].id for r in results]
        domain = [r for r in results if self._lane_list[r.li].group != ENTRY_GROUP]
        entry_lanes = [self._lane_list[r.li].id for r in results if self._lane_list[r.li].group == ENTRY_GROUP]

        entry_level, years, entry_reasons = self._entry(job, title, body.low, entry_lanes)
        loc_pts, loc_reason = self._location(job)
        rec_pts, age = self._recency(job.posted_at, now)

        best = 0.0
        for r in domain:
            w = 1.0 if self.is_active(self._lane_list[r.li].id) else INACTIVE_FACTOR
            best = max(best, r.rel * w)
        entry_pts = ENTRY_POINTS[entry_level]
        if best > 0:
            score = REL_POINTS * (1.0 - math.exp(-best / REL_SCALE)) + entry_pts + loc_pts + rec_pts
        else:
            score = (5.0 if entry_lanes else 0.0) + max(entry_pts, 0.0) * 0.25 + (loc_pts + rec_pts) * 0.5
            score = min(score, NO_DOMAIN_CAP)
        score = round(min(100.0, max(0.0, score)), 1)

        job.lanes = lane_ids
        job.groups = list(dict.fromkeys(self._lane_list[r.li].group for r in results))
        job.lane_scores = {self._lane_list[r.li].id: round(r.rel, 1) for r in results}
        job.score = score
        job.entry_level = entry_level
        job.years_required = years
        job.match_reasons = self._reasons(
            results, employer, notes, entry_reasons, loc_reason, age, title_hits, body_hits
        )
        return job

    def _reasons(
        self,
        results: list[_LaneResult],
        employer: Employer | None,
        notes: dict[str, Any],
        entry_reasons: list[str],
        loc_reason: str | None,
        age: int | None,
        title_hits: dict[int, str],
        body_hits: dict[int, str],
    ) -> list[str]:
        out: list[str] = []
        domain = [r for r in results if self._lane_list[r.li].group != ENTRY_GROUP]
        if not domain:
            out.append("no domain lane matched")
        for r in domain[:3]:
            out.append(r.reason)
        if employer is not None and not any(x.startswith("employer prior:") for x in out):
            prior = next((r for r in results if r.prior), None)
            if prior is not None:
                out.append(f"employer prior: {employer.name} → {self._lane_list[prior.li].id}")
            elif notes.get("group_prior"):
                g = next(
                    (
                        self._lane_list[r.li].group
                        for r in results
                        if self._lane_list[r.li].group in (employer.groups or [])
                    ),
                    None,
                )
                if g:
                    out.append(f"employer prior: {employer.name} → {g}")
        out.extend(entry_reasons[:2])
        if notes.get("off_domain"):
            out.append(f"off-domain title: '{notes['off_domain']}'")
        if loc_reason:
            out.append(loc_reason)
        if age is not None and age <= 14:
            out.append("posted today" if age == 0 else f"posted {age} day{'s' if age != 1 else ''} ago")
        for r in results:
            if self._lane_list[r.li].group == ENTRY_GROUP:
                out.append(r.reason)
        for r in domain[3:]:
            out.append(r.reason)
        for li, why in (notes.get("vetoed") or [])[:2]:
            out.append(f"{why} → not {self._lane_list[li].id}")
        return list(dict.fromkeys(out))[:MAX_REASONS]

    def title_signal(self, title: str, employer: Employer | None = None) -> float:
        """Cheap title-only relevance used before ``enrich()``; >= 8 means "worth fetching the description"."""
        t = _Text(title or "")
        hits = self._match(t, True)
        best = 0.0
        ctx: set[tuple[str, ...]] = set()
        excluded: set[int] = set()
        per_lane: dict[int, list[int]] = {}
        title_hit = False
        for tid in hits:
            for li, cat in self._roles[tid]:
                if cat == "X":
                    excluded.add(li)
                elif cat == "C":
                    ctx.add(self._terms[tid].canon)
                else:
                    per_lane.setdefault(li, [0, 0])[0 if cat == "T" else 1] += 1
        for li, (n_title, n_phrase) in per_lane.items():
            if li in excluded:
                continue
            if n_title:
                title_hit = title_hit or self._lane_list[li].group != ENTRY_GROUP
                s = 20.0 + 5.0 * min(n_title - 1, 2)
            else:
                s = 12.0
            if self._lane_list[li].group == ENTRY_GROUP:
                s *= 0.5
            best = max(best, s)
        sig = best + 3.0 * min(len(ctx), 2)
        if _GENERIC_RE.search(t.norm):
            sig += 6.0
        off = _OFF_DOMAIN_RE.search(t.norm) is not None
        if employer is not None and self._employer_prior_ok(t, hits, off):
            if any(x in self._lane_idx for x in employer.lanes or []):
                sig += 6.0
            elif any(x in self._group_by_id for x in employer.groups or []):
                sig += 3.0
        if off and not title_hit:
            sig = min(sig, 2.0)
        if self._senior(t.norm):
            sig -= 3.0
        return round(max(0.0, sig), 2)

    def search_plan(
        self,
        lane_ids: list[str] | None = None,
        source: str | None = None,
        posted_within_days: int | None = None,
    ) -> list[SearchQuery]:
        """Keyword queries for one search source, interleaved across lanes (see ARCHITECTURE 4.1)."""
        if source in FEED_SOURCES:
            return [
                SearchQuery(
                    keywords="*", extra={"feed": True}, limit=FEED_LIMIT, posted_within_days=posted_within_days
                )
            ]
        if lane_ids:
            lanes = [self.lanes[x] for x in dict.fromkeys(lane_ids) if x in self.lanes]
            unknown = [x for x in lane_ids if x not in self.lanes]
            if unknown:
                log.debug("search_plan: unknown lane ids ignored: %s", unknown)
        else:
            lanes = self.active_lanes()
        per_lane: list[list[SearchQuery]] = []
        for lane in lanes:
            if source == "usajobs":
                qs = self._usajobs_queries(lane, posted_within_days)
            else:
                qs = [
                    SearchQuery(keywords=q, lane=lane.id, posted_within_days=posted_within_days)
                    for q in lane.queries
                ]
            if qs:
                per_lane.append(qs)
        out: list[SearchQuery] = []
        seen: set[tuple[str, str]] = set()
        depth = max((len(qs) for qs in per_lane), default=0)
        for i in range(depth):
            for qs in per_lane:
                if i >= len(qs):
                    continue
                q = qs[i]
                key = (" ".join(q.keywords.lower().split()), json.dumps(q.extra, sort_keys=True, default=str))
                if key in seen:
                    continue
                seen.add(key)
                out.append(q)
        return out

    def _usajobs_queries(self, lane: Lane, posted_within_days: int | None) -> list[SearchQuery]:
        spec = lane.usajobs
        if not spec:
            if lane.group not in USAJOBS_FALLBACK_GROUPS:
                return []
            return [
                SearchQuery(keywords=q, lane=lane.id, posted_within_days=posted_within_days)
                for q in lane.queries
            ]
        extra: dict[str, Any] = {}
        series = [_pad(s, 4) for s in _str_list(spec.get("series"))]
        if series:
            extra["series"] = series
        paths = _str_list(spec.get("hiring_path"))
        if paths:
            extra["hiring_path"] = paths
        for key in ("grade_low", "grade_high"):
            if spec.get(key) not in (None, ""):
                extra[key] = _pad(spec[key], 2)
        for key, value in spec.items():
            if key not in ("keywords", "series", "hiring_path", "grade_low", "grade_high") and value not in (
                None,
                "",
                [],
            ):
                extra[key] = value
        keywords = _str_list(spec.get("keywords"))
        if not keywords:
            keywords = [""] if extra else list(lane.queries)
        return [
            SearchQuery(keywords=kw, lane=lane.id, posted_within_days=posted_within_days, extra=dict(extra))
            for kw in keywords
        ]


# =====================================================================================================
# module helpers
# =====================================================================================================


def _pad(value: Any, width: int) -> str:
    s = str(value).strip()
    return s.zfill(width) if s.isdigit() else s


def _body_text(job: Job) -> str:
    """Description plus metadata worth matching (company, department, federal grade/series)."""
    meta = [job.company or "", job.department or ""]
    grade = job.grade or ""
    if grade:
        meta.append(grade)
    for s in job.series or []:
        s = str(s).strip()
        if s.isdigit():
            meta.append(f"GS-{s.zfill(4)}")
    meta_text = "\n".join(m for m in meta if m)
    desc = job.description or ""
    return f"{desc}\n{meta_text}" if meta_text else desc


def _is_cyber(body: _Text) -> bool:
    n = len(body.toks & _CYBER_TOKENS)
    if n >= CYBER_MIN_TERMS:
        return True
    for p in _CYBER_PHRASES:
        if p in body.low:
            n += 1
            if n >= CYBER_MIN_TERMS:
                return True
    return False


def _federal_grades(job: Job) -> list[int]:
    grade = job.grade or ""
    if not grade:
        return []
    if not (_GRADE_PLAN_RE.search(grade) or job.source == "usajobs"):
        return []
    cleaned = re.sub(r"\b\d{4}\b", " ", grade)  # drop series codes like 0343
    return [n for n in (int(x) for x in re.findall(r"\d{1,2}", cleaned)) if 1 <= n <= 15]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    from .text import iso_date

    iso = iso_date(s)
    if iso:
        try:
            return date.fromisoformat(iso[:10])
        except ValueError:
            return None
    return None


def _num(word: str) -> int | None:
    word = word.lower()
    if word.isdigit():
        return int(word)
    return _NUM_WORDS.get(word)


def parse_years_required(text: str | None, *, lowered: bool = False) -> int | None:
    """Minimum years of experience asked for ('3+ years', 'at least two (2) years', '5-7 years')."""
    if not text:
        return None
    low = text if lowered else normalize_text(text).lower()
    if "year" not in low and "yr" not in low:
        return None
    found: list[int] = []
    for m in _YEAR_WORD_RE.finditer(low):
        s, e = m.start(), m.end()
        before = low[max(0, s - 48) : s]
        nm = _YEARS_BEFORE_RE.search(before)
        if not nm:
            continue
        n = _num(nm.group("a"))
        if n is None or n > _MAX_YEARS:
            continue
        after = low[e : e + 90]
        if _YEARS_AFTER_BAD_RE.match(after):
            continue
        pre = low[max(0, s - 48 - 40) : s - len(nm.group(0))]
        pre_tail = pre[-32:]
        if _YEARS_PRE_BAD_RE.search(pre_tail):
            continue
        if not (_YEARS_AFTER_OK_RE.match(after) or _YEARS_PRE_OK_RE.search(pre_tail)):
            continue
        found.append(n)
        if len(found) >= 3:
            break
    return min(found) if found else None


__all__ = [
    "ENTRY_GROUP",
    "FEED_SOURCES",
    "Group",
    "Lane",
    "Taxonomy",
    "Term",
    "compile_term",
    "load_profile",
    "normalize_text",
    "parse_years_required",
]
