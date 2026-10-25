#!/usr/bin/env python3
"""Validate catalog/employers/*.yaml (and catalog/lanes.yaml when present) against the fixed ids.

Usage: validate_catalog.py [employers-file.yaml ...]   (default: every file in catalog/employers/)
Exit code 1 on errors. Warnings do not fail.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parent.parent / "src" / "jobsweep" / "catalog"

LANES: dict[str, list[str]] = {
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
        "forensic-investigations", "law-firm-analyst", "litigation-case-analyst", "public-defense-investigation",
        "nonprofit-grant-dd",
    ],
    "geopolitical-risk": [
        "country-sovereign-risk", "geopolitical-analysis", "political-forecasting", "geoeconomics",
        "economic-security", "supply-chain-forced-labor", "risk-consulting", "primary-research-expert-networks",
        "investment-research-dd",
    ],
    "sanctions-fincrime": [
        "sanctions", "export-controls", "aml-kyc", "fincrime-intelligence", "illicit-finance",
        "crypto-investigations", "investigative-compliance", "market-surveillance",
    ],
    "trust-safety": [
        "trust-safety-policy", "policy-enforcement-ai-safeguards", "ai-policy-forecasting", "election-integrity",
        "disinformation", "human-rights",
    ],
    "policy-research": [
        "national-security", "foreign-policy", "think-tank-research", "international-orgs",
        "policy-legislative-analyst", "tech-regulatory-policy", "criminal-justice-policy", "social-policy-advocacy",
        "urban-planning-research", "research-writing",
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
    "earlier-lanes": [
        "program-evaluation", "philanthropy", "digital-rights", "economic-consulting", "broadcast-journalism",
        "real-estate",
    ],
}
ALL_LANES = {lane for lanes in LANES.values() for lane in lanes}
ATS = {
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable", "recruitee", "personio", "breezy",
    "bamboohr", "paylocity", "adp", "ultipro", "oracle", "jobvite", "rippling", "pinpoint", "teamtailor", "other",
}
BOARD_RE = {
    "workday": r"^[a-z0-9-]+/wd\d+/[A-Za-z0-9_-]+$",
    "oracle": r"^[a-z0-9.-]+\.oraclecloud\.com/[A-Za-z0-9_]+$",
    "ultipro": r"^[A-Za-z0-9]+/[0-9a-fA-F-]{36}$",
    "paylocity": r"^[0-9a-fA-F-]{36}$",
}
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIELDS = {
    "key", "name", "groups", "lanes", "ats", "board", "careers_url", "ats_vendor", "hq", "locations",
    "title_filter", "search_terms", "max_jobs", "notes", "verified", "open_jobs", "active",
}


def check_employers(path: Path) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    stats = {"total": 0, "sweepable": 0, "verified": 0, "manual": 0}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"{path.name}: YAML error: {exc}"], [], stats
    group = doc.get("group")
    if group not in LANES:
        errors.append(f"{path.name}: top-level 'group' must be one of {sorted(LANES)}, got {group!r}")
    seen: set[str] = set()
    for i, row in enumerate(doc.get("employers") or []):
        where = f"{path.name}[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: not a mapping")
            continue
        stats["total"] += 1
        key = row.get("key")
        where = f"{path.name}:{key}"
        if not key or not KEY_RE.match(str(key)):
            errors.append(f"{where}: key must be lowercase-hyphen slug")
        if key in seen:
            errors.append(f"{where}: duplicate key in file")
        seen.add(key)
        if not row.get("name"):
            errors.append(f"{where}: missing name")
        unknown = set(row) - FIELDS
        if unknown:
            errors.append(f"{where}: unknown fields {sorted(unknown)}")
        for lane in row.get("lanes") or []:
            if lane not in ALL_LANES:
                errors.append(f"{where}: unknown lane {lane!r}")
        for g in row.get("groups") or []:
            if g not in LANES:
                errors.append(f"{where}: unknown group {g!r}")
        ats = row.get("ats")
        if ats is not None and ats not in ATS:
            errors.append(f"{where}: unknown ats {ats!r}")
        board = row.get("board")
        if ats and ats != "other":
            if not board:
                errors.append(f"{where}: ats {ats} needs a board")
            elif ats in BOARD_RE and not re.match(BOARD_RE[ats], str(board)):
                errors.append(f"{where}: board {board!r} does not match {ats} format {BOARD_RE[ats]}")
            stats["sweepable"] += 1
            if row.get("verified"):
                stats["verified"] += 1
            else:
                warnings.append(f"{where}: sweepable but not verified")
        else:
            stats["manual"] += 1
            if not row.get("careers_url"):
                warnings.append(f"{where}: manual employer without careers_url")
        if row.get("title_filter"):
            try:
                re.compile(row["title_filter"])
            except re.error as exc:
                errors.append(f"{where}: bad title_filter regex: {exc}")
        if not row.get("lanes"):
            warnings.append(f"{where}: no lanes")
    return errors, warnings, stats


def check_lanes(path: Path) -> list[str]:
    errors: list[str] = []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    found: dict[str, list[str]] = {}
    for g in doc.get("groups") or []:
        found[g.get("id")] = [lane.get("id") for lane in g.get("lanes") or []]
        for lane in g.get("lanes") or []:
            for kind in ("title", "phrases", "context", "exclude"):
                for term in lane.get(kind) or []:
                    if isinstance(term, str) and len(term) > 2 and term.startswith("/") and term.endswith("/"):
                        try:
                            re.compile(term[1:-1])
                        except re.error as exc:
                            errors.append(f"lanes.yaml {lane.get('id')}.{kind}: bad regex {term!r}: {exc}")
    for gid, lanes in LANES.items():
        if gid not in found:
            errors.append(f"lanes.yaml: missing group {gid}")
        elif found[gid] != lanes:
            errors.append(f"lanes.yaml: group {gid} lane ids/order differ: {found[gid]} != {lanes}")
    extra = set(found) - set(LANES)
    if extra:
        errors.append(f"lanes.yaml: unexpected groups {sorted(extra)}")
    return errors


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted((CATALOG / "employers").glob("*.yaml"))
    all_errors: list[str] = []
    for f in files:
        errors, warnings, stats = check_employers(f)
        print(f"{f.name}: {stats}")
        for w in warnings:
            print("  warn:", w)
        for e in errors:
            print("  ERROR:", e)
        all_errors += errors
    lanes = CATALOG / "lanes.yaml"
    if not argv and lanes.exists():
        errs = check_lanes(lanes)
        print(f"lanes.yaml: {'ok' if not errs else str(len(errs)) + ' errors'}")
        for e in errs:
            print("  ERROR:", e)
        all_errors += errs
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
