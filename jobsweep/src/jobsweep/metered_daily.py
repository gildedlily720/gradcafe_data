"""Budgeted daily metered searches, reserved durably before any billable request.

The workflow runs ``plan``, pushes its ledger to the state branch, then runs ``run``.
Reservations are deliberately not refunded after failures: a timed-out request may
still have incurred a charge. Account reads never log/store raw responses or keys.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .models import SearchQuery

SOURCES = ("theirstack", "hasdata_indeed", "apify")
KEYS = {"theirstack": "THEIRSTACK_API_KEY", "hasdata_indeed": "HASDATA_API_KEY", "apify": "APIFY_TOKEN"}
# Reservation units: provider credits for the first two sources; US cents for Apify.
DAILY_QUERIES = {"theirstack": 1, "hasdata_indeed": 10, "apify": 1}
QUERY_UNITS = {"theirstack": 6, "hasdata_indeed": 10, "apify": 8}
MONTHLY_UNITS = {"theirstack": 200, "hasdata_indeed": 4000, "apify": 250}


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "reservations": [], "cursors": {}}
    ledger = json.loads(path.read_text())
    if ledger.get("version") != 1 or not isinstance(ledger.get("reservations"), list):
        raise ValueError("Invalid metered ledger; refusing to reset its spending history")
    for entry in ledger["reservations"]:
        if (
            entry.get("source") not in SOURCES
            or not isinstance(entry.get("day"), str)
            or not isinstance(entry.get("units"), int)
            or entry["units"] < 0
        ):
            raise ValueError("Invalid metered reservation")
    if not isinstance(ledger.get("cursors"), dict):
        raise ValueError("Invalid metered query cursors")
    return ledger


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def reservation_id(day: str, source: str, queries: list[dict]) -> str:
    return hashlib.sha256(json.dumps([day, source, queries], sort_keys=True).encode()).hexdigest()


def available_units(client: httpx.Client, settings: Settings, source: str) -> tuple[int, int]:
    """(currently affordable units, daily query cap), from read-only account APIs.

    Unknown/missing schemas fail closed. Apify additionally requires the account's
    existing hard spending limit to fit inside included plan credit: per-run actor
    event limits alone do not necessarily cap separate platform/storage charges.
    """
    key = settings.get(KEYS[source])
    if not key:
        raise ValueError("missing credential")

    def get(url: str, headers: dict) -> dict:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    if source == "theirstack":
        data = get("https://api.theirstack.com/v0/billing/credit-balance", {"Authorization": f"Bearer {key}"})
        return max(0, int(data["api_credits"])), DAILY_QUERIES[source]
    if source == "hasdata_indeed":
        data = get("https://api.hasdata.com/user/me/usage", {"x-api-key": key})["data"]
        # A 5,000-credit plan allows all 10 calls/day (3,100 in a 31-day month).
        # Smaller plans are automatically scaled down; never assume which one is paid.
        cap = min(DAILY_QUERIES[source], int(data["totalCredits"]) // (31 * 10))
        return max(0, int(data["availableCredits"])), cap
    headers = {"Authorization": f"Bearer {key}"}
    account = get("https://api.apify.com/v2/users/me", headers)["data"]
    limits = get("https://api.apify.com/v2/users/me/limits", headers)["data"]
    included = float(account["plan"]["monthlyUsageCreditsUsd"])
    hard_limit = float(limits["limits"]["maxMonthlyUsageUsd"])
    used = float(limits["current"]["monthlyUsageUsd"])
    if not all(math.isfinite(v) and v >= 0 for v in (included, hard_limit, used)):
        raise ValueError("invalid account budget")
    if hard_limit > included:
        raise ValueError("account spending limit permits overage; daily Apify paused")
    # Leave 10 cents for dataset/platform overhead, which the actor event cap excludes.
    return max(0, math.floor((min(included, hard_limit) - used - 0.10) * 100)), 1


def query_pool(taxonomy: Any, source: str, lanes: list[str] | None) -> list[SearchQuery]:
    pool = []
    for query in taxonomy.search_plan(lanes, source=source, posted_within_days=30):
        for remote in (False, True):
            # Only use adapter options whose cost is bounded by this reservation.
            extra: dict = {}
            limit = 50
            if source == "theirstack":
                limit = QUERY_UNITS[source]
                extra = {"limit": limit, "country_codes": ["US"]}
            elif source == "hasdata_indeed":
                extra = {"pages": 1}
            else:
                # Leave time to enrich descriptions used by qualification screening.
                limit = 20
                extra = {"actor": "hiringcafe", "max_charge_usd": 0.08, "timeout_secs": 240}
            pool.append(
                replace(
                    query,
                    location="Remote" if remote else "New York, NY",
                    remote=remote,
                    limit=limit,
                    extra=extra,
                )
            )
    return pool


def reserve(
    ledger: dict,
    taxonomy: Any,
    settings: Settings,
    client: httpx.Client,
    *,
    day: str,
    selected: list[str],
    lanes: list[str] | None = None,
) -> dict:
    plan: dict = {"version": 1, "day": day, "sources": {}, "skipped": {}}
    for source in selected:
        records = [r for r in ledger["reservations"] if r["source"] == source]
        if any(r["day"] == day for r in records):
            plan["skipped"][source] = "already reserved today (reruns do not spend again)"
            continue
        used = sum(r["units"] for r in records if r["day"][:7] == day[:7])
        remaining = max(0, MONTHLY_UNITS[source] - used)
        try:
            available, cap = available_units(client, settings, source)
        except Exception as exc:  # account errors must never leak headers/response bodies
            safe_reasons = {
                "missing credential",
                "invalid account budget",
                "account spending limit permits overage; daily Apify paused",
            }
            reason = str(exc) if str(exc) in safe_reasons else type(exc).__name__
            plan["skipped"][source] = f"account/budget check failed: {reason}"
            continue
        count = min(
            cap, DAILY_QUERIES[source], remaining // QUERY_UNITS[source], available // QUERY_UNITS[source]
        )
        pool = query_pool(taxonomy, source, lanes)
        count = min(count, len(pool))
        if count <= 0:
            plan["skipped"][source] = "no remaining budget or active queries"
            continue
        cursor = int(ledger["cursors"].get(source, 0))
        queries = [pool[(cursor + i) % len(pool)].to_dict() for i in range(count)]
        identity = reservation_id(day, source, queries)
        units = count * QUERY_UNITS[source]
        ledger["reservations"].append({"day": day, "source": source, "units": units, "id": identity})
        ledger["cursors"][source] = cursor + count
        plan["sources"][source] = {"id": identity, "units": units, "queries": queries}
    return plan


class PlannedTaxonomy:
    def __init__(self, taxonomy: Any, plan: dict):
        self.taxonomy, self.plan = taxonomy, plan

    def __getattr__(self, name: str) -> Any:
        return getattr(self.taxonomy, name)

    def search_plan(self, lane_ids=None, *, source=None, posted_within_days=None):
        yield from (SearchQuery(**q) for q in self.plan["sources"].get(source, {}).get("queries", []))


def run_plan(settings: Settings, plan_path: Path, ledger_path: Path) -> dict:
    from .cli import _load_registry, _load_taxonomy, _open_store
    from .sweep import SweepOptions, run_sweep_sync

    plan = json.loads(plan_path.read_text())
    ledger = read_ledger(ledger_path)
    if plan.get("day") != today() or plan.get("consumed"):
        raise ValueError("Metered plan expired or already consumed")
    for source, spec in plan["sources"].items():
        if spec["id"] != reservation_id(plan["day"], source, spec["queries"]):
            raise ValueError("Metered query changed after reservation")
        if not any(
            r["source"] == source
            and r["day"] == plan["day"]
            and r["id"] == spec["id"]
            and r["units"] == spec["units"]
            for r in ledger["reservations"]
        ):
            raise ValueError("Metered plan has no matching reservation")
    plan["consumed"] = True
    write_json(plan_path, plan)  # never replay this local plan after a timeout/crash
    if not plan["sources"]:
        return {"total_jobs": 0, "new_uids": [], "per_source": {}, "metered_budget": plan}
    store = _open_store(settings)
    try:
        report = run_sweep_sync(
            settings,
            PlannedTaxonomy(_load_taxonomy(settings), plan),
            _load_registry(settings),
            store,
            SweepOptions(sources=list(plan["sources"]), boards=False, include_metered=True, concurrency=1),
        )
    finally:
        store.close()
    result = report.to_dict()
    result["metered_budget"] = plan
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run"))
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--sources", default="")
    parser.add_argument("--lanes", default="")
    args = parser.parse_args()
    settings = Settings.load()
    if args.command == "run":
        print(json.dumps(run_plan(settings, args.plan, args.ledger), indent=2))
        return
    from .cli import _load_taxonomy

    selected = [s.strip() for s in args.sources.split(",") if s.strip()] or list(SOURCES)
    selected = [s for s in selected if s in SOURCES]
    lanes = [s.strip() for s in args.lanes.split(",") if s.strip()] or None
    ledger = read_ledger(args.ledger)
    with httpx.Client(timeout=30) as client:
        plan = reserve(
            ledger, _load_taxonomy(settings), settings, client, day=today(), selected=selected, lanes=lanes
        )
    write_json(args.ledger, ledger)
    write_json(args.plan, plan)
    for source, spec in plan["sources"].items():
        unit = "US cents" if source == "apify" else "credits"
        print(f"{source}: reserved {len(spec['queries'])} searches, at most {spec['units']} {unit}")
    for source, reason in plan["skipped"].items():
        print(f"{source}: SKIPPED — {reason}")


if __name__ == "__main__":
    main()
