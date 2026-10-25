"""Refresh stored screening/date metadata without treating old jobs as new sightings.

Run after restoring state and before sweeping/exporting. Safe to repeat: it never touches
first_seen, last_seen, active, duplicate links, run IDs, or application status/notes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jobsweep.config import Settings
from jobsweep.date_provenance import repair_posting_date
from jobsweep.employers import EmployerRegistry
from jobsweep.models import Job
from jobsweep.store import Store
from jobsweep.taxonomy import Taxonomy


def reclassify_store(store: Store, taxonomy: Taxonomy, registry: EmployerRegistry) -> dict[str, int]:
    counts = {"scanned": 0, "changed": 0, "dates_repaired": 0}
    now = store.clock()
    # upsert_jobs intentionally refreshes sightings and activates jobs, so it must not be used here.
    # The targeted transaction refreshes source/classification columns and indexes only.
    with store._tx() as conn:
        rows = conn.execute("SELECT uid, data FROM jobs ORDER BY uid").fetchall()
        for row in rows:
            counts["scanned"] += 1
            original = json.loads(row["data"])
            job = Job.from_dict(original)
            if repair_posting_date(job):
                counts["dates_repaired"] += 1
            taxonomy.classify(job, registry.get(job.employer_key) if job.employer_key else None, now=now)
            data = {**original, **job.to_dict()}
            if data == original:
                continue
            blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
            columns = store._columns(job)
            assignments = [*(f"{name} = ?" for name in columns), "data = ?"]
            conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE uid = ?",
                [*columns.values(), blob, row["uid"]],
            )
            store._index(conn, row["uid"], data)
            counts["changed"] += 1
    return counts


def main() -> None:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--profile", type=Path, default=settings.profile_path)
    args = parser.parse_args()
    if not args.db.exists():
        print("No restored database to reclassify.")
        return
    taxonomy = Taxonomy.load(profile=args.profile)
    registry = EmployerRegistry.load()
    with Store(args.db) as store:
        print(json.dumps(reclassify_store(store, taxonomy, registry), sort_keys=True))


if __name__ == "__main__":
    main()
