"""Employer registry: catalog/employers/*.yaml -> ``Employer`` objects."""

from __future__ import annotations

import logging
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
                # Same employer listed under two groups: merge groups/lanes, keep first board.
                cur = self._by_key[emp.key]
                cur.groups = list(dict.fromkeys(cur.groups + emp.groups))
                cur.lanes = list(dict.fromkeys(cur.lanes + emp.lanes))
                if not cur.board and emp.board:
                    cur.ats, cur.board = emp.ats, emp.board
                continue
            self._by_key[emp.key] = emp

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
