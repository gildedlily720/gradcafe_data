"""Repair legacy source dates without inventing a publication timestamp."""

from __future__ import annotations

from .models import Job
from .text import iso_date


def repair_posting_date(job: Job) -> bool:
    """Repair a legacy Greenhouse job in place; return whether its date metadata changed.

    The former importer used ``updated_at`` when ``first_published`` was unavailable.
    Exact normalized timestamp equality makes an unmarked legacy date ambiguous. Clear
    it conservatively until a source refresh can recover the real publication date.
    Comparing only calendar days would incorrectly erase genuine earlier publication
    times, so no date truncation is used here. Status and observation times are untouched.
    """
    if job.source != "greenhouse":
        return False
    before = (job.posted_at, dict(job.extra))
    published = iso_date(job.extra.get("first_published"))
    if published:
        job.posted_at = published
        job.extra["posted_at_source"] = "greenhouse:first_published"
    elif job.extra.get("posted_at_source") == "greenhouse:first_published":
        # Explicit provenance from the corrected importer takes precedence over equality.
        pass
    else:
        posted = iso_date(job.posted_at)
        updated = iso_date(job.extra.get("updated_at"))
        if posted and updated and posted == updated:
            job.posted_at = None
            job.extra["posting_date_note"] = "Legacy update timestamp removed; publication date unknown"
        job.extra["posted_at_source"] = "greenhouse:first_published" if job.posted_at else "unknown"
    return before != (job.posted_at, job.extra)
