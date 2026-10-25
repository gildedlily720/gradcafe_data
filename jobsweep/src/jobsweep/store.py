"""SQLite store for jobs, sweep runs and user triage state.

* ``jobs`` holds one row per ``Job.uid``: indexed columns used for filtering plus the full
  ``Job.to_dict()`` JSON blob, and the user's ``status``/``note`` (never overwritten by sweeps).
* ``job_lanes(uid, lane, grp)`` is a normalised copy of each job's lanes/groups for fast filtering and
  stats (``lane = ''`` rows carry groups that have no matching lane).
* ``jobs_fts`` is an FTS5 index used by ``query(q=...)`` (falls back to ``LIKE`` without FTS5).
* ``runs`` records sweeps.

One connection guarded by a re-entrant lock, so the web app can call it from a threadpool.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import CATALOG_DIR
from .models import JOB_STATUSES, Job

log = logging.getLogger("jobsweep.store")

SCHEMA_VERSION = 1
SNAPSHOT_VERSION = 1
INACTIVE_GROUP = "earlier-lanes"

# Sources whose jobs are not managed by ``mark_board_complete`` (they expire by last_seen instead).
SEARCH_SOURCES = frozenset(
    {"usajobs", "nycjobs", "nysjobs", "adzuna", "theirstack", "hasdata_indeed", "jobspy", "apify", "import"}
)

# Fallback lane -> group map (ARCHITECTURE.md §4.1) used when catalog/lanes.yaml is missing.
_FALLBACK_GROUPS: dict[str, list[str]] = {
    "specialty-insurance": [
        "political-risk", "political-violence-terrorism", "war-risk", "kidnap-ransom", "special-risks",
        "trade-credit-surety", "transactional-liability", "financial-lines", "fine-art-specie", "intangibles",
        "aviation", "marine-cargo", "reinsurance-broking", "insurance-fraud-siu",
    ],
    "intelligence-security": [
        "strategic-intelligence", "threat-intelligence", "protective-intelligence", "travel-risk-crisis",
        "gsoc-watch", "corporate-security", "osint", "counterterrorism", "regional-research-specialist",
        "defense-intel-data",
    ],
    "investigations-dd": [
        "corporate-intelligence", "due-diligence", "beneficial-ownership", "fraud-investigations",
        "forensic-investigations", "law-firm-analyst", "litigation-case-analyst",
        "public-defense-investigation", "nonprofit-grant-dd",
    ],
    "geopolitical-risk": [
        "country-sovereign-risk", "geopolitical-analysis", "political-forecasting", "geoeconomics",
        "economic-security", "supply-chain-forced-labor", "risk-consulting",
        "primary-research-expert-networks", "investment-research-dd",
    ],
    "sanctions-fincrime": [
        "sanctions", "export-controls", "aml-kyc", "fincrime-intelligence", "illicit-finance",
        "crypto-investigations", "investigative-compliance", "market-surveillance",
    ],
    "trust-safety": [
        "trust-safety-policy", "policy-enforcement-ai-safeguards", "ai-policy-forecasting",
        "election-integrity", "disinformation", "human-rights",
    ],
    "policy-research": [
        "national-security", "foreign-policy", "think-tank-research", "international-orgs",
        "policy-legislative-analyst", "tech-regulatory-policy", "criminal-justice-policy",
        "social-policy-advocacy", "urban-planning-research", "research-writing",
    ],
    "strategic-comms": [
        "strategic-comms-advisory", "crisis-comms", "public-government-affairs", "reputation-media-analysis",
        "insights-monitoring",
    ],
    "government": [
        "district-attorney", "nyc-integrity", "nyc-mayors-office", "nys-agencies", "ny-fed",
        "emergency-management", "federal-pathways", "federal-investigator",
    ],
    "entry-routes": ["graduate-programs", "analyst-programs", "fellowships", "entry-level-analyst"],
    INACTIVE_GROUP: [
        "program-evaluation", "philanthropy", "digital-rights", "economic-consulting", "broadcast-journalism",
        "real-estate",
    ],
}  # fmt: skip

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER,
    url TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    closes_at TEXT,
    employer_key TEXT,
    employment_type TEXT,
    score REAL NOT NULL DEFAULT 0,
    entry_level INTEGER,
    lanes_json TEXT NOT NULL DEFAULT '[]',
    groups_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',
    note TEXT,
    status_updated_at TEXT,
    duplicate_of TEXT,
    first_run INTEGER,
    last_run INTEGER,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_active_score ON jobs(active, duplicate_of, score);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_employer ON jobs(employer_key);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS jobs_closes ON jobs(closes_at);
CREATE TABLE IF NOT EXISTS job_lanes (
    uid TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT '',
    grp TEXT NOT NULL DEFAULT '',
    pos INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uid, lane, grp)
);
CREATE INDEX IF NOT EXISTS job_lanes_lane ON job_lanes(lane);
CREATE INDEX IF NOT EXISTS job_lanes_grp ON job_lanes(grp);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    stats TEXT
);
"""

_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5("
    "uid UNINDEXED, title, company, location, body, tokenize='unicode61 remove_diacritics 2')"
)

_SORTS = {
    "score": "j.score DESC, j.first_seen DESC, j.uid",
    "newest": "j.first_seen DESC, j.score DESC, j.uid",
    "posted": "(julianday(j.posted_at) IS NULL), julianday(j.posted_at) DESC, j.first_seen DESC, j.uid",
    "closing": "(j.closes_at IS NULL), substr(j.closes_at, 1, 10) ASC, j.score DESC, j.uid",
    "company": "j.company COLLATE NOCASE ASC, j.score DESC, j.uid",
}

_QUERY_KEYS = {
    "q", "lanes", "groups", "sources", "statuses", "employers", "entry_only", "include_unknown_entry",
    "min_score", "location", "remote", "since", "active_only", "include_duplicates",
    "include_inactive_lanes", "suitability",
}  # fmt: skip


@functools.lru_cache(maxsize=4)
def load_lane_map(path: str | None = None) -> tuple[dict[str, str], frozenset[str], frozenset[str]]:
    """(lane -> group, inactive lane ids, inactive group ids) from lanes.yaml, or the §4.1 fallback."""
    lanes_path = Path(path) if path else CATALOG_DIR / "lanes.yaml"
    lane_group: dict[str, str] = {}
    inactive_lanes: set[str] = set()
    inactive_groups: set[str] = set()
    try:
        import yaml

        doc = yaml.safe_load(lanes_path.read_text(encoding="utf-8")) or {}
        for g in doc.get("groups") or []:
            gid = g.get("id")
            if not gid:
                continue
            g_active = g.get("active", True) is not False
            if not g_active:
                inactive_groups.add(gid)
            for lane in g.get("lanes") or []:
                lid = lane.get("id") if isinstance(lane, dict) else None
                if not lid:
                    continue
                lane_group[lid] = gid
                if not g_active or lane.get("active", True) is False:
                    inactive_lanes.add(lid)
    except Exception as exc:  # noqa: BLE001 - missing/half-written lanes.yaml -> fallback
        log.debug("lanes.yaml unavailable (%s); using built-in lane map", exc)
        lane_group = {}
    if not lane_group:
        lane_group = {lane: g for g, lanes in _FALLBACK_GROUPS.items() for lane in lanes}
        inactive_groups = {INACTIVE_GROUP}
        inactive_lanes = set(_FALLBACK_GROUPS[INACTIVE_GROUP])
    return lane_group, frozenset(inactive_lanes), frozenset(inactive_groups)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str]:
    """None/""/str/"a,b"/list -> list of non-empty strings."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    out: list[str] = []
    for v in value:
        out.extend(_as_list(v) if isinstance(v, str) else [str(v)])
    return out


def _bool_to_int(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _fingerprint_ok(fp: str) -> bool:
    parts = fp.split("|")
    return len(parts) >= 2 and bool(parts[0]) and bool(parts[1])


def _fts_query(q: str) -> str:
    """User text -> safe FTS5 MATCH expression: quoted phrases kept, words prefix-matched, AND by default."""
    parts: list[str] = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q):
        if phrase:
            toks = re.findall(r"\w+", phrase)
            if toks:
                parts.append('"' + " ".join(toks) + '"')
        elif word in ("OR", "AND", "NOT"):
            parts.append(word)
        else:
            parts.extend(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in re.findall(r"\w+", word))
    # Drop dangling operators.
    while parts and parts[0] in ("OR", "AND", "NOT"):
        parts.pop(0)
    while parts and parts[-1] in ("OR", "AND", "NOT"):
        parts.pop()
    return " ".join(parts)


class Store:
    """SQLite-backed job store. See ARCHITECTURE.md §5."""

    def __init__(
        self,
        path: str | Path,
        *,
        lane_groups: dict[str, str] | None = None,
        inactive_lanes: Iterable[str] | None = None,
        inactive_groups: Iterable[str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = str(path)
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        lm, il, ig = load_lane_map()
        self.lane_groups: dict[str, str] = dict(lane_groups) if lane_groups is not None else dict(lm)
        self.inactive_lanes: set[str] = set(inactive_lanes) if inactive_lanes is not None else set(il)
        self.inactive_groups: set[str] = set(inactive_groups) if inactive_groups is not None else set(ig)
        #: returns "now" (tz-aware); tests may replace it to simulate time passing
        self.clock: Callable[[], datetime] = clock or _utcnow
        self.fts = False
        self._init_schema()

    # ---- setup -----------------------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            c = self._conn
            c.execute("PRAGMA busy_timeout = 30000")
            if self.path != ":memory:":
                try:
                    c.execute("PRAGMA journal_mode = WAL")
                except sqlite3.DatabaseError as exc:  # e.g. network filesystems
                    log.debug("WAL unavailable: %s", exc)
            c.execute("PRAGMA synchronous = NORMAL")
            c.executescript(_SCHEMA)
            try:
                c.execute(_FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError as exc:
                log.info("FTS5 unavailable (%s); text search falls back to LIKE", exc)
            c.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _now(self) -> str:
        return _iso(self.clock())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- runs ------------------------------------------------------------------------------------
    def start_run(self, meta: dict) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs(started_at, meta) VALUES (?, ?)",
                (self._now(), json.dumps(meta or {}, default=str)),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET finished_at = ?, stats = ? WHERE id = ?",
                (self._now(), json.dumps(stats or {}, default=str), run_id),
            )

    @staticmethod
    def _run_dict(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "meta": json.loads(r["meta"] or "{}"),
            "stats": json.loads(r["stats"]) if r["stats"] else None,
        }

    def runs(self, limit: int | None = 20) -> list[dict]:
        sql = "SELECT * FROM runs ORDER BY id DESC"
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._lock:
            return [self._run_dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- writes ----------------------------------------------------------------------------------
    def _canonical_for(
        self, c: sqlite3.Connection, fp: str, source: str, uid: str, before: str | None
    ) -> str | None:
        if not _fingerprint_ok(fp):
            return None
        sql = (
            "SELECT uid FROM jobs WHERE fingerprint = ? AND active = 1 AND source != ? "
            "AND duplicate_of IS NULL AND uid != ?"
        )
        params: list[Any] = [fp, source, uid]
        if before:
            sql += " AND first_seen <= ?"
            params.append(before)
        row = c.execute(sql + " ORDER BY first_seen, uid LIMIT 1", params).fetchone()
        return row["uid"] if row else None

    def _index(self, c: sqlite3.Connection, uid: str, data: dict) -> None:
        """Refresh job_lanes and FTS rows for one job."""
        lanes = [str(x) for x in data.get("lanes") or []]
        groups = [str(x) for x in data.get("groups") or []]
        c.execute("DELETE FROM job_lanes WHERE uid = ?", (uid,))
        rows: list[tuple[str, str, str, int]] = []
        covered: set[str] = set()
        for pos, lane in enumerate(dict.fromkeys(lanes)):
            grp = self.lane_groups.get(lane) or (groups[0] if len(groups) == 1 else "")
            rows.append((uid, lane, grp, pos))
            covered.add(grp)
        for pos, grp in enumerate(dict.fromkeys(groups)):
            if grp not in covered:
                rows.append((uid, "", grp, 1000 + pos))
        c.executemany("INSERT OR IGNORE INTO job_lanes(uid, lane, grp, pos) VALUES (?, ?, ?, ?)", rows)
        if self.fts:
            body = " ".join(
                str(x)
                for x in [
                    data.get("description") or "",
                    data.get("department") or "",
                    data.get("grade") or "",
                    " ".join(data.get("tags") or []),
                    " ".join(lanes),
                    " ".join(data.get("series") or []),
                ]
                if x
            )
            c.execute("DELETE FROM jobs_fts WHERE uid = ?", (uid,))
            c.execute(
                "INSERT INTO jobs_fts(uid, title, company, location, body) VALUES (?, ?, ?, ?, ?)",
                (uid, data.get("title") or "", data.get("company") or "", data.get("location") or "", body),
            )

    @staticmethod
    def _columns(job: Job) -> dict[str, Any]:
        return {
            "source": job.source,
            "source_id": str(job.source_id),
            "title": job.title or "",
            "company": job.company or "",
            "location": job.location or "",
            "remote": _bool_to_int(job.remote),
            "url": job.url or "",
            "posted_at": job.posted_at,
            "closes_at": job.closes_at,
            "employer_key": job.employer_key,
            "employment_type": job.employment_type,
            "score": float(job.score or 0.0),
            "entry_level": _bool_to_int(job.entry_level),
            "lanes_json": json.dumps(list(job.lanes or [])),
            "groups_json": json.dumps(list(job.groups or [])),
            "fingerprint": job.fingerprint,
        }

    def upsert_jobs(self, jobs: Iterable[Job | dict], run_id: int | None = None) -> dict:
        """Insert or refresh jobs. Returns ``{"new": [uid...], "updated": n, "duplicates": [uid...]}``.

        Keeps ``first_seen`` and the user's status/note; refreshes ``last_seen`` and re-activates.
        New jobs whose fingerprint matches an active job from another source get ``duplicate_of``.
        """
        new: list[str] = []
        dups: list[str] = []
        updated = 0
        now = self._now()
        with self._tx() as c:
            for item in jobs:
                job = item if isinstance(item, Job) else Job.from_dict(item)
                uid = job.uid
                data = job.to_dict()
                blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
                cols = self._columns(job)
                row = c.execute(
                    "SELECT first_seen, duplicate_of, data FROM jobs WHERE uid = ?", (uid,)
                ).fetchone()
                if row is None:
                    dup = self._canonical_for(c, cols["fingerprint"], job.source, uid, None)
                    names = ["uid", *cols, "first_seen", "last_seen", "active", "status", "duplicate_of"]
                    names += ["first_run", "last_run", "data"]
                    values = [uid, *cols.values(), now, now, 1, "new", dup, run_id, run_id, blob]
                    c.execute(
                        f"INSERT INTO jobs({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        values,
                    )
                    self._index(c, uid, data)
                    new.append(uid)
                    if dup:
                        dups.append(uid)
                    continue
                dup = row["duplicate_of"]
                if dup:
                    still = c.execute(
                        "SELECT 1 FROM jobs WHERE uid = ? AND active = 1 AND fingerprint = ?",
                        (dup, cols["fingerprint"]),
                    ).fetchone()
                    if not still:
                        dup = self._canonical_for(c, cols["fingerprint"], job.source, uid, row["first_seen"])
                changed = row["data"] != blob
                assigns = [
                    "last_seen = ?",
                    "active = 1",
                    "duplicate_of = ?",
                    "last_run = COALESCE(?, last_run)",
                ]
                params: list[Any] = [now, dup, run_id]
                if changed:
                    assigns += [f"{k} = ?" for k in cols] + ["data = ?"]
                    params += [*cols.values(), blob]
                c.execute(f"UPDATE jobs SET {', '.join(assigns)} WHERE uid = ?", [*params, uid])
                if changed:
                    self._index(c, uid, data)
                updated += 1
        return {"new": new, "updated": updated, "duplicates": dups}

    def mark_board_complete(
        self, employer_key: str, seen_uids: Iterable[str], source: str | None = None
    ) -> int:
        """After a full board fetch: deactivate this employer's board jobs that were not seen.

        ``source`` limits it to one adapter (the sweep always passes it); without it the sources are taken
        from ``seen_uids`` (or every non-search source when nothing was seen).
        """
        seen = set(seen_uids)
        if source:
            srcs = [source]
        else:
            srcs = sorted({u.split(":", 1)[0] for u in seen})
        sql = "SELECT uid FROM jobs WHERE employer_key = ? AND active = 1"
        params: list[Any] = [employer_key]
        if srcs:
            sql += f" AND source IN ({','.join('?' * len(srcs))})"
            params += srcs
        else:
            sql += f" AND source NOT IN ({','.join('?' * len(SEARCH_SOURCES))})"
            params += sorted(SEARCH_SOURCES)
        with self._tx() as c:
            stale = [r["uid"] for r in c.execute(sql, params).fetchall() if r["uid"] not in seen]
            c.executemany("UPDATE jobs SET active = 0 WHERE uid = ?", [(u,) for u in stale])
        return len(stale)

    def expire(self, older_than_days: int = 30) -> int:
        """Deactivate search-sourced jobs not seen for ``older_than_days`` and any job past ``closes_at``.

        Board jobs are normally retired by ``mark_board_complete``; ones not seen for 3x the window (board
        removed or failing for weeks) are retired here too.
        """
        now = self.clock()
        cutoff = _iso(now - timedelta(days=older_than_days))
        board_cutoff = _iso(now - timedelta(days=max(older_than_days * 3, 90)))
        today = now.date().isoformat()
        srcs = sorted(SEARCH_SOURCES)
        marks = ",".join("?" * len(srcs))
        with self._tx() as c:
            cur = c.execute(
                "UPDATE jobs SET active = 0 WHERE active = 1 AND ("
                " (closes_at IS NOT NULL AND closes_at != '' AND substr(closes_at, 1, 10) < ?)"
                f" OR ((source IN ({marks}) OR employer_key IS NULL) AND last_seen < ?)"
                " OR last_seen < ?)",
                [today, *srcs, cutoff, board_cutoff],
            )
            return cur.rowcount

    def set_status(self, uid: str, status: str, note: str | None = None) -> dict | None:
        """Set the user's triage status (and note, when given). Raises ValueError on unknown status."""
        if status not in JOB_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {', '.join(JOB_STATUSES)}")
        with self._tx() as c:
            cur = c.execute(
                "UPDATE jobs SET status = ?, note = COALESCE(?, note), status_updated_at = ? WHERE uid = ?",
                (status, note, self._now(), uid),
            )
            if cur.rowcount == 0:
                return None
        return self.get(uid)

    def prune(self, older_than_days: int = 180) -> int:
        """Delete inactive, untriaged (status 'new') jobs not seen for ``older_than_days``."""
        cutoff = _iso(self.clock() - timedelta(days=older_than_days))
        with self._tx() as c:
            uids = [
                r["uid"]
                for r in c.execute(
                    "SELECT uid FROM jobs WHERE active = 0 AND status = 'new' AND last_seen < ?", (cutoff,)
                ).fetchall()
            ]
            for uid in uids:
                c.execute("DELETE FROM jobs WHERE uid = ?", (uid,))
                c.execute("DELETE FROM job_lanes WHERE uid = ?", (uid,))
                if self.fts:
                    c.execute("DELETE FROM jobs_fts WHERE uid = ?", (uid,))
                c.execute("UPDATE jobs SET duplicate_of = NULL WHERE duplicate_of = ?", (uid,))
        return len(uids)

    # ---- reads -----------------------------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = json.loads(r["data"])
        d["uid"] = r["uid"]
        d["first_seen"] = r["first_seen"]
        d["last_seen"] = r["last_seen"]
        d["active"] = bool(r["active"])
        d["status"] = r["status"]
        d["note"] = r["note"]
        d["status_updated_at"] = r["status_updated_at"]
        d["duplicate_of"] = r["duplicate_of"]
        return d

    def get(self, uid: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM jobs WHERE uid = ?", (uid,)).fetchone()
        return self._row(r) if r else None

    def _since_iso(self, since: Any) -> str | None:
        if since is None or since == "":
            return None
        if isinstance(since, bool):
            raise TypeError("since must be a date/datetime/ISO string or a number of days")
        if isinstance(since, (int, float)):
            return _iso(self.clock() - timedelta(days=float(since)))
        if isinstance(since, datetime):
            return _iso(since if since.tzinfo else since.replace(tzinfo=timezone.utc))
        if isinstance(since, date):
            return since.isoformat()
        s = str(since).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return _iso(self.clock() - timedelta(days=float(s)))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s
        if len(s) <= 10:
            return dt.date().isoformat()
        return _iso(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))

    def _where(
        self,
        *,
        q: str | None = None,
        lanes: Any = None,
        groups: Any = None,
        sources: Any = None,
        statuses: Any = None,
        employers: Any = None,
        suitability: Any = None,
        entry_only: bool = False,
        include_unknown_entry: bool = True,
        min_score: float | None = None,
        location: Any = None,
        remote: bool | None = None,
        since: Any = None,
        active_only: bool = True,
        include_duplicates: bool = False,
        include_inactive_lanes: bool = False,
    ) -> tuple[str, list[Any]]:
        conds: list[str] = []
        params: list[Any] = []

        def marks(values: list[Any]) -> str:
            params.extend(values)
            return ",".join("?" * len(values))

        lanes_l, groups_l = _as_list(lanes), _as_list(groups)
        statuses_l = _as_list(statuses)
        if "all" in statuses_l:
            statuses_l = []
        unknown = [s for s in statuses_l if s not in JOB_STATUSES]
        if unknown:
            raise ValueError(f"unknown status {unknown[0]!r}")
        # Asking for e.g. saved/applied jobs should still show them after the posting closes.
        if active_only and not (statuses_l and "new" not in statuses_l):
            conds.append("j.active = 1")
        if not include_duplicates:
            conds.append("j.duplicate_of IS NULL")
        if lanes_l:
            conds.append(
                f"EXISTS (SELECT 1 FROM job_lanes jl WHERE jl.uid = j.uid AND jl.lane IN ({marks(lanes_l)}))"
            )
        if groups_l:
            conds.append(
                f"EXISTS (SELECT 1 FROM job_lanes jl WHERE jl.uid = j.uid AND jl.grp IN ({marks(groups_l)}))"
            )
        explicit_inactive = bool(set(lanes_l) & self.inactive_lanes or set(groups_l) & self.inactive_groups)
        if (
            not include_inactive_lanes
            and not explicit_inactive
            and (self.inactive_lanes or self.inactive_groups)
        ):
            il = sorted(self.inactive_lanes) or [""]
            ig = sorted(self.inactive_groups) or [""]
            conds.append(
                "NOT (EXISTS (SELECT 1 FROM job_lanes jl WHERE jl.uid = j.uid) AND NOT EXISTS ("
                "SELECT 1 FROM job_lanes jl WHERE jl.uid = j.uid AND NOT ("
                f"jl.lane IN ({marks(il)}) OR (jl.lane = '' AND jl.grp IN ({marks(ig)})))))"
            )
        sources_l = _as_list(sources)
        if sources_l:
            conds.append(f"j.source IN ({marks(sources_l)})")
        if statuses_l:
            conds.append(f"j.status IN ({marks(statuses_l)})")
        employers_l = _as_list(employers)
        if employers_l:
            conds.append(f"j.employer_key IN ({marks(employers_l)})")
        suitability_l = _as_list(suitability)
        if suitability_l:
            allowed = {"eligible", "review", "excluded", "not_assessed"}
            unknown_suitability = [value for value in suitability_l if value not in allowed]
            if unknown_suitability:
                raise ValueError(f"unknown suitability {unknown_suitability[0]!r}")
            conds.append(
                "COALESCE(NULLIF(json_extract(j.data, '$.suitability'), ''), 'not_assessed') "
                f"IN ({marks(suitability_l)})"
            )
        if entry_only:
            conds.append(
                "(j.entry_level = 1 OR j.entry_level IS NULL)"
                if include_unknown_entry
                else "j.entry_level = 1"
            )
        if min_score is not None and min_score != "":
            conds.append("j.score >= ?")
            params.append(float(min_score))
        locs = [x for x in (location if isinstance(location, (list, tuple)) else [location]) if x]
        if locs:
            parts = []
            for loc in locs:
                parts.append("j.location LIKE ?")
                params.append(f"%{str(loc).strip()}%")
                if "remote" in str(loc).lower():
                    parts.append("j.remote = 1")
            conds.append("(" + " OR ".join(parts) + ")")
        if remote is not None:
            conds.append("j.remote = 1" if remote else "COALESCE(j.remote, 0) = 0")
        since_s = self._since_iso(since)
        if since_s:
            conds.append("j.first_seen >= ?")
            params.append(since_s)
        if q and str(q).strip():
            match = _fts_query(str(q)) if self.fts else ""
            if match:
                conds.append("j.uid IN (SELECT uid FROM jobs_fts WHERE jobs_fts MATCH ?)")
                params.append(match)
            else:
                for tok in re.findall(r"\w+", str(q)) or [str(q).strip()]:
                    conds.append("(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ? OR j.data LIKE ?)")
                    params.extend([f"%{tok}%"] * 4)
        return (" WHERE " + " AND ".join(conds)) if conds else "", params

    def query(
        self, *, sort: str = "score", limit: int | None = 50, offset: int = 0, **filters: Any
    ) -> list[dict]:
        """Filtered, sorted page of row dicts (see ARCHITECTURE.md §5 for the filters)."""
        bad = set(filters) - _QUERY_KEYS
        if bad:
            raise TypeError(f"unknown query filter(s): {', '.join(sorted(bad))}")
        where, params = self._where(**filters)
        sql = f"SELECT j.* FROM jobs j{where} ORDER BY {_SORTS.get(sort or 'score', _SORTS['score'])}"
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), max(int(offset or 0), 0)]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(max(int(offset), 0))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def count(self, **filters: Any) -> int:
        for k in ("sort", "limit", "offset"):
            filters.pop(k, None)
        bad = set(filters) - _QUERY_KEYS
        if bad:
            raise TypeError(f"unknown query filter(s): {', '.join(sorted(bad))}")
        where, params = self._where(**filters)
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM jobs j{where}", params).fetchone()[0])

    def stats(self) -> dict:
        """Counts over non-duplicate jobs (by_* over active ones) plus the last run."""
        now = self.clock()
        d1, d7 = _iso(now - timedelta(days=1)), _iso(now - timedelta(days=7))
        base = "duplicate_of IS NULL"
        with self._lock:
            c = self._conn

            def scalar(sql: str, *p: Any) -> int:
                return int(c.execute(sql, p).fetchone()[0] or 0)

            def grouped(sql: str, *p: Any) -> dict[str, int]:
                return {r[0]: int(r[1]) for r in c.execute(sql, p).fetchall() if r[0]}

            out: dict[str, Any] = {
                "total": scalar(f"SELECT COUNT(*) FROM jobs WHERE {base}"),
                "active": scalar(f"SELECT COUNT(*) FROM jobs WHERE {base} AND active = 1"),
                "new_24h": scalar(
                    f"SELECT COUNT(*) FROM jobs WHERE {base} AND active = 1 AND first_seen >= ?", d1
                ),
                "new_7d": scalar(
                    f"SELECT COUNT(*) FROM jobs WHERE {base} AND active = 1 AND first_seen >= ?", d7
                ),
                "entry_level": scalar(
                    f"SELECT COUNT(*) FROM jobs WHERE {base} AND active = 1 AND entry_level = 1"
                ),
                "duplicates": scalar(
                    "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL AND active = 1"
                ),
                "by_group": grouped(
                    "SELECT jl.grp, COUNT(DISTINCT j.uid) n FROM job_lanes jl JOIN jobs j ON j.uid = jl.uid "
                    "WHERE j.active = 1 AND j.duplicate_of IS NULL AND jl.grp != '' GROUP BY jl.grp ORDER BY n DESC"
                ),
                "by_lane": grouped(
                    "SELECT jl.lane, COUNT(DISTINCT j.uid) n FROM job_lanes jl JOIN jobs j ON j.uid = jl.uid "
                    "WHERE j.active = 1 AND j.duplicate_of IS NULL AND jl.lane != '' GROUP BY jl.lane ORDER BY n DESC"
                ),
                "by_source": grouped(
                    f"SELECT source, COUNT(*) n FROM jobs WHERE {base} AND active = 1 GROUP BY source ORDER BY n DESC"
                ),
                "by_status": grouped(
                    f"SELECT status, COUNT(*) n FROM jobs WHERE {base} GROUP BY status ORDER BY n DESC"
                ),
            }
            last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        out["last_run"] = self._run_dict(last) if last else None
        return out

    # ---- snapshots -------------------------------------------------------------------------------
    def export_snapshot(self, *, max_description: int | None = None, runs_limit: int = 500) -> dict:
        """Full state as JSON-able dict (all rows incl. inactive/duplicates) for persisting between CI runs."""
        with self._lock:
            rows = [self._row(r) for r in self._conn.execute("SELECT * FROM jobs ORDER BY first_seen, uid")]
        if max_description:
            for r in rows:
                desc = r.get("description") or ""
                if len(desc) > max_description:
                    r["description"] = desc[:max_description].rsplit(" ", 1)[0] + " …"
        return {
            "version": SNAPSHOT_VERSION,
            "exported_at": self._now(),
            "jobs": rows,
            "runs": list(reversed(self.runs(limit=runs_limit))),
        }

    def import_snapshot(self, snap: dict) -> int:
        """Merge a snapshot: earliest first_seen, latest last_seen (whose job data/active win), and the
        status/note with the newer ``status_updated_at``. Returns the number of rows inserted or changed."""
        if not isinstance(snap, dict) or not isinstance(snap.get("jobs"), list):
            raise ValueError("not a jobsweep snapshot (expected {'version': 1, 'jobs': [...]})")
        if int(snap.get("version") or SNAPSHOT_VERSION) != SNAPSHOT_VERSION:
            raise ValueError(f"unsupported snapshot version {snap.get('version')!r}")
        changed = 0
        now = self._now()
        with self._tx() as c:
            for raw in snap["jobs"]:
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                if (not row.get("source") or not row.get("source_id")) and ":" in str(row.get("uid") or ""):
                    row["source"], row["source_id"] = str(row["uid"]).split(":", 1)
                if not row.get("source") or row.get("source_id") in (None, ""):
                    continue
                for k in ("title", "company", "url"):
                    row[k] = row.get(k) or ""
                job = Job.from_dict(row)
                if self._merge_row(c, job, row, now):
                    changed += 1
            for run in snap.get("runs") or []:
                if not isinstance(run, dict) or not run.get("started_at"):
                    continue
                exists = c.execute("SELECT 1 FROM runs WHERE started_at = ?", (run["started_at"],)).fetchone()
                if not exists:
                    c.execute(
                        "INSERT INTO runs(started_at, finished_at, meta, stats) VALUES (?, ?, ?, ?)",
                        (
                            run["started_at"],
                            run.get("finished_at"),
                            json.dumps(run.get("meta") or {}, default=str),
                            json.dumps(run["stats"], default=str) if run.get("stats") is not None else None,
                        ),
                    )
        return changed

    def _merge_row(self, c: sqlite3.Connection, job: Job, row: dict, now: str) -> bool:
        uid = job.uid
        data = job.to_dict()
        first_seen = row.get("first_seen") or now
        last_seen = row.get("last_seen") or first_seen
        active = 1 if row.get("active", True) else 0
        status = row.get("status") if row.get("status") in JOB_STATUSES else "new"
        cur = c.execute("SELECT * FROM jobs WHERE uid = ?", (uid,)).fetchone()
        if cur is None:
            cols = self._columns(job)
            names = ["uid", *cols, "first_seen", "last_seen", "active", "status", "note", "status_updated_at"]
            names += ["duplicate_of", "data"]
            values = [
                uid, *cols.values(), first_seen, last_seen, active, status, row.get("note"),
                row.get("status_updated_at"), row.get("duplicate_of"),
                json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str),
            ]  # fmt: skip
            c.execute(f"INSERT INTO jobs({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values)
            self._index(c, uid, data)
            return True
        updates: dict[str, Any] = {}
        if first_seen < cur["first_seen"]:
            updates["first_seen"] = first_seen
        if last_seen > cur["last_seen"]:
            old = json.loads(cur["data"])
            if not data.get("description") and old.get("description"):
                data["description"] = old["description"]
            blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
            updates["last_seen"] = last_seen
            updates["active"] = active
            updates["duplicate_of"] = row.get("duplicate_of")
            if blob != cur["data"]:
                updates.update(self._columns(Job.from_dict(data)))
                updates["data"] = blob
        inc_ts, cur_ts = row.get("status_updated_at"), cur["status_updated_at"]
        if inc_ts and (not cur_ts or inc_ts > cur_ts):
            updates.update(status=status, note=row.get("note"), status_updated_at=inc_ts)
        updates = {k: v for k, v in updates.items() if cur[k] != v}
        if not updates:
            return False
        c.execute(
            f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in updates)} WHERE uid = ?",
            [*updates.values(), uid],
        )
        if "data" in updates:
            self._index(c, uid, json.loads(updates["data"]))
        return True
