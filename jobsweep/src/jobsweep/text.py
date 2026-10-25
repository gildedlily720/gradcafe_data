"""Text helpers shared by adapters: HTML to text, date and salary parsing."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

MAX_DESCRIPTION_CHARS = 12000

_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "table",
    "section",
    "article",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Convert (possibly entity-escaped) HTML to readable plain text, truncated to ``limit`` chars."""
    if not raw:
        return ""
    text = raw
    # Greenhouse double-escapes its HTML (&lt;p&gt;). Unescape until no tags are hidden.
    for _ in range(2):
        if "&lt;" in text and "<" not in text[:200]:
            text = html.unescape(text)
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:  # noqa: BLE001 - malformed HTML falls back to a regex strip
            text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return text


def iso_date(value: object) -> str | None:
    """Best-effort normalisation of timestamps (ISO strings, epoch s/ms, 'MM/DD/YYYY') to ISO-8601."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,13}", s):
        return iso_date(int(s))
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%d %B %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            dt = datetime.strptime(s.replace("Z", "+00:00") if "%z" in fmt else s, fmt)
            return (
                dt.date().isoformat()
                if fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y")
                else dt.isoformat()
            )
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


_RELATIVE = re.compile(
    r"(?:posted\s+)?(today|yesterday|(\d+)\+?\s+(day|days|hour|hours|week|weeks|month|months)\s+ago)", re.I
)


def relative_to_iso(text: str | None, now: datetime | None = None) -> str | None:
    """'Posted 3 Days Ago' / 'Posted Today' / '30+ Days Ago' -> ISO date (Workday style)."""
    if not text:
        return None
    m = _RELATIVE.search(text)
    if not m:
        return None
    now = now or datetime.now(timezone.utc)
    word = m.group(1).lower()
    if word == "today":
        return now.date().isoformat()
    if word == "yesterday":
        return (now - timedelta(days=1)).date().isoformat()
    n, unit = int(m.group(2)), m.group(3).lower()
    days = (
        n
        * {"hour": 0, "hours": 0, "day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}[unit]
    )
    return (now - timedelta(days=days)).date().isoformat()


_MONEY = re.compile(
    r"(?P<cur>[$£€])\s?(?P<a>\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s?(?P<ak>[kK])?"
    r"(?:\s*(?:-|–|—|to)\s*(?P<cur2>[$£€])?\s?(?P<b>\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s?(?P<bk>[kK])?)?"
)
_CUR = {"$": "USD", "£": "GBP", "€": "EUR"}


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None, str | None]:
    """Extract (min, max, currency, period) from free text like '$65,000 - $80,000 per year'."""
    if not text:
        return None, None, None, None
    m = _MONEY.search(text)
    if not m:
        return None, None, None, None

    def num(v: str | None, k: str | None) -> float | None:
        if not v:
            return None
        x = float(re.sub(r"[,\s]", "", v))
        return x * 1000 if k else x

    lo, hi = num(m.group("a"), m.group("ak")), num(m.group("b"), m.group("bk"))
    tail = text[m.end() : m.end() + 40].lower()
    period = None
    if re.search(r"hour|/hr|\bhr\b|hourly", tail):
        period = "hour"
    elif re.search(r"year|annum|annual|/yr|\byr\b", tail) or (lo and lo >= 20000):
        period = "year"
    elif re.search(r"month", tail):
        period = "month"
    return lo, hi or lo, _CUR.get(m.group("cur")), period


def truncate(text: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"
