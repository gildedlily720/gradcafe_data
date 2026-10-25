"""Employer registry: catalog/employers/*.yaml -> ``Employer`` objects."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

import yaml

from .config import CATALOG_DIR
from .models import Employer

log = logging.getLogger("jobsweep.employers")

EMPLOYERS_DIR = CATALOG_DIR / "employers"


class EmployerRegistry:
    def __init__(self, employers: Iterable[Employer]):
        self._by_key: dict[str, Employer] = {}
        for emp in employers:
            if emp.key in self._by_key:
                self._merge(self._by_key[emp.key], emp)
                continue
            self._by_key[emp.key] = emp

    @staticmethod
    def _merge(cur: Employer, other: Employer) -> None:
        """Same employer listed under two groups: union groups/lanes and widen the sweep settings.

        The first sweepable board wins. When both entries point at that board, their title filters are
        OR-ed (no filter on either side means keep every title), search terms are unioned and the larger
        max_jobs is kept, so each group's searches (e.g. security *and* AML at a bank) both run.
        """
        cur.groups = list(dict.fromkeys(cur.groups + other.groups))
        cur.lanes = list(dict.fromkeys(cur.lanes + other.lanes))
        if not cur.sweepable and other.sweepable:
            cur.ats, cur.board = other.ats, other.board
            cur.title_filter, cur.search_terms, cur.max_jobs = (
                other.title_filter,
                other.search_terms,
                other.max_jobs,
            )
            cur.verified, cur.open_jobs = other.verified, other.open_jobs
        elif (cur.ats, cur.board) == (other.ats, other.board) and cur.board:
            if cur.title_filter and other.title_filter and cur.title_filter != other.title_filter:
                # Leading global flags like (?i) are only legal at the very start of a pattern.
                a, b = (re.sub(r"^\(\?i\)", "", f) for f in (cur.title_filter, other.title_filter))
                cur.title_filter = f"(?i)(?:{a})|(?:{b})"
            elif not cur.title_filter or not other.title_filter:
                cur.title_filter = None
            if cur.search_terms and other.search_terms:
                cur.search_terms = list(dict.fromkeys(cur.search_terms + other.search_terms))
            if other.max_jobs and (not cur.max_jobs or other.max_jobs > cur.max_jobs):
                cur.max_jobs = other.max_jobs
        cur.careers_url = cur.careers_url or other.careers_url
        cur.locations = list(dict.fromkeys(cur.locations + other.locations))

    @classmethod
    def load(cls, directory: str | Path | None = None) -> EmployerRegistry:
        directory = Path(directory) if directory else EMPLOYERS_DIR
        employers: list[Employer] = []
        for path in sorted(directory.glob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                log.error("cannot parse %s: %s", path, exc)
                continue
            group = doc.get("group")
            for row in doc.get("employers") or []:
                if not isinstance(row, dict) or not row.get("key") or not row.get("name"):
                    log.warning("skipping malformed employer entry in %s: %r", path.name, row)
                    continue
                employers.append(Employer.from_dict(row, group=group))
        return cls(employers)

    def all(self) -> list[Employer]:
        return list(self._by_key.values())

    def get(self, key: str) -> Employer | None:
        return self._by_key.get(key)

    def sweepable(self) -> list[Employer]:
        return [e for e in self._by_key.values() if e.sweepable]

    def manual(self) -> list[Employer]:
        """Employers without a supported ATS board: shown in the UI as 'check by hand'."""
        return [e for e in self._by_key.values() if e.active and not e.sweepable]

    def select(
        self,
        *,
        keys: Iterable[str] | None = None,
        groups: Iterable[str] | None = None,
        lanes: Iterable[str] | None = None,
        ats: Iterable[str] | None = None,
        sweepable_only: bool = True,
    ) -> list[Employer]:
        keys_s = set(keys or [])
        groups_s, lanes_s, ats_s = set(groups or []), set(lanes or []), set(ats or [])
        out = []
        for e in self._by_key.values():
            if sweepable_only and not e.sweepable:
                continue
            if keys_s and e.key not in keys_s:
                continue
            if groups_s and not groups_s.intersection(e.groups):
                continue
            if lanes_s and not lanes_s.intersection(e.lanes) and not groups_s.intersection(e.groups):
                continue
            if ats_s and e.ats not in ats_s:
                continue
            out.append(e)
        return out

    def __len__(self) -> int:
        return len(self._by_key)
