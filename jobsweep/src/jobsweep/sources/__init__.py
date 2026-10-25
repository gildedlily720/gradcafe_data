"""Adapter registry. Modules are imported lazily so a broken optional adapter never breaks the rest."""

from __future__ import annotations

import importlib
import logging

from .base import Source, SourceContext, SourceError, SourceUnavailable

log = logging.getLogger("jobsweep.sources")

# name -> "module:ClassName". Keep in sync with ARCHITECTURE.md (section "Sources").
SOURCE_PATHS: dict[str, str] = {
    # per-employer ATS boards
    "greenhouse": "jobsweep.sources.greenhouse:GreenhouseSource",
    "lever": "jobsweep.sources.lever:LeverSource",
    "ashby": "jobsweep.sources.ashby:AshbySource",
    "workday": "jobsweep.sources.workday:WorkdaySource",
    "smartrecruiters": "jobsweep.sources.smartrecruiters:SmartRecruitersSource",
    "workable": "jobsweep.sources.workable:WorkableSource",
    "recruitee": "jobsweep.sources.recruitee:RecruiteeSource",
    "personio": "jobsweep.sources.personio:PersonioSource",
    "breezy": "jobsweep.sources.breezy:BreezySource",
    "bamboohr": "jobsweep.sources.bamboohr:BambooHRSource",
    "paylocity": "jobsweep.sources.paylocity:PaylocitySource",
    "adp": "jobsweep.sources.adp:ADPSource",
    "ultipro": "jobsweep.sources.ultipro:UltiProSource",
    "oracle": "jobsweep.sources.oracle_hcm:OracleHCMSource",
    "jobvite": "jobsweep.sources.jobvite:JobviteSource",
    "rippling": "jobsweep.sources.rippling:RipplingSource",
    "pinpoint": "jobsweep.sources.pinpoint:PinpointSource",
    "teamtailor": "jobsweep.sources.teamtailor:TeamtailorSource",
    # keyword search / feeds
    "usajobs": "jobsweep.sources.usajobs:USAJobsSource",
    "nycjobs": "jobsweep.sources.nycjobs:NYCJobsSource",
    "nysjobs": "jobsweep.sources.nysjobs:NYSJobsSource",
    "adzuna": "jobsweep.sources.adzuna:AdzunaSource",
    "theirstack": "jobsweep.sources.theirstack:TheirStackSource",
    "hasdata_indeed": "jobsweep.sources.hasdata_indeed:HasDataIndeedSource",
    "jobspy": "jobsweep.sources.jobspy_source:JobSpySource",
    "apify": "jobsweep.sources.apify:ApifySource",
    # results gathered by Claude connectors (Indeed connector, etc.) pushed in via MCP/CLI import
    "import": "jobsweep.sources.manual_import:ImportSource",
}

_cache: dict[str, type[Source] | None] = {}


def get_source_class(name: str) -> type[Source] | None:
    """Return the adapter class or None if it is unknown or fails to import."""
    if name in _cache:
        return _cache[name]
    path = SOURCE_PATHS.get(name)
    cls: type[Source] | None = None
    if path:
        mod_name, _, cls_name = path.partition(":")
        try:
            cls = getattr(importlib.import_module(mod_name), cls_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("source %s unavailable (import failed: %s)", name, exc)
            cls = None
    _cache[name] = cls
    return cls


def all_source_classes() -> dict[str, type[Source]]:
    out = {}
    for name in SOURCE_PATHS:
        cls = get_source_class(name)
        if cls is not None:
            out[name] = cls
    return out


def board_sources() -> list[str]:
    return [n for n, c in all_source_classes().items() if "board" in c.kind]


def search_sources() -> list[str]:
    return [n for n, c in all_source_classes().items() if "search" in c.kind]


__all__ = [
    "SOURCE_PATHS",
    "Source",
    "SourceContext",
    "SourceError",
    "SourceUnavailable",
    "all_source_classes",
    "board_sources",
    "get_source_class",
    "search_sources",
]
