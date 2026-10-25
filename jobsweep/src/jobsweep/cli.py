"""Command line interface: ``jobsweep <command>`` / ``python -m jobsweep`` (ARCHITECTURE.md §8).

Exit codes: 0 ok, 1 errors, 2 usage. Heavy modules (taxonomy, web app, MCP server, ATS detection,
source adapters) are imported lazily inside the commands that need them.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import inspect
import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .config import ENV_VARS, Settings
from .models import JOB_STATUSES, Employer, Job, Profile

log = logging.getLogger("jobsweep.cli")

SORTS = ("score", "newest", "posted", "closing", "company")


# ---- loaders (tests monkeypatch these) ------------------------------------------------------------


def _load_taxonomy(settings: Settings) -> Any:
    from .taxonomy import Taxonomy

    return Taxonomy.load(profile=settings.profile_path)


def _load_registry(settings: Settings) -> Any:
    from .employers import EmployerRegistry

    return EmployerRegistry.load()


def _open_store(settings: Settings) -> Any:
    from .store import Store

    return Store(settings.db_path)


def _employers_dir() -> Path:
    from .employers import EMPLOYERS_DIR

    return EMPLOYERS_DIR


# ---- small helpers --------------------------------------------------------------------------------


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _split(values: Sequence[str] | str | None) -> list[str] | None:
    """Repeatable/comma-separated option values -> list (None when empty)."""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    out = [v.strip() for raw in values for v in str(raw).split(",") if v.strip()]
    return out or None


def _clip(value: Any, width: int) -> str:
    s = "" if value is None else str(value).replace("\n", " ")
    return s if len(s) <= width else s[: max(width - 1, 0)] + "…"


def _table(headers: list[str], rows: list[list[Any]], caps: list[int] | None = None) -> str:
    caps = caps or [60] * len(headers)
    cells = [[_clip(c, caps[i]) for i, c in enumerate(r)] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    return "\n".join(lines)


def _min_score_default(settings: Settings) -> float:
    try:
        return float(_load_taxonomy(settings).profile.min_score)
    except Exception:  # noqa: BLE001 - taxonomy optional for listing
        return Profile().min_score


def _entry_flag(value: Any) -> str:
    return {True: "Y", False: "n"}.get(value, "?")


def _employer_by_name(registry: Any) -> Callable[[Job], Employer | None]:
    """Lookup by employer_key, else by normalised company name."""

    def norm(s: str | None) -> str:
        s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
        return " ".join(re.sub(r"\b(inc|llc|ltd|plc|corp|corporation|co|company|group|the)\b", "", s).split())

    by_name: dict[str, Employer] = {}
    if registry is not None:
        for e in registry.all():
            by_name.setdefault(norm(e.name), e)

    def lookup(job: Job) -> Employer | None:
        if registry is None:
            return None
        if job.employer_key:
            emp = registry.get(job.employer_key)
            if emp:
                return emp
        return by_name.get(norm(job.company)) if job.company else None

    return lookup


def _read_text_file(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    p = Path(path)
    data = p.read_bytes()
    if p.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8-sig")


# ---- commands -------------------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    from . import sources as sources_mod

    report: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0]}
    problems = 0
    rows = []
    for name in sources_mod.SOURCE_PATHS:
        cls = sources_mod.get_source_class(name)
        if cls is None:
            rows.append(
                {
                    "name": name,
                    "kind": None,
                    "available": False,
                    "reason": "adapter missing or failed to import",
                    "metered": False,
                }
            )
            continue
        try:
            ok, reason = cls.availability(settings)
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"availability check failed: {exc}"
        rows.append(
            {
                "name": name,
                "kind": cls.kind,
                "available": bool(ok),
                "reason": reason,
                "metered": bool(cls.metered),
            }
        )
    report["sources"] = rows
    report["env"] = {k: bool(settings.get(k)) for k in ENV_VARS}

    catalog: dict[str, Any] = {}
    try:
        tax = _load_taxonomy(settings)
        groups = tax.to_dict().get("groups", [])
        lanes = [lane for g in groups for lane in g.get("lanes", [])]
        catalog.update(
            groups=len(groups),
            lanes=len(lanes),
            active_lanes=sum(
                1
                for g in groups
                for lane in g.get("lanes", [])
                if g.get("active", True) and lane.get("active", True)
            ),
            min_score=getattr(getattr(tax, "profile", None), "min_score", None),
        )
    except Exception as exc:  # noqa: BLE001
        catalog["taxonomy_error"] = str(exc)[:300]
        problems += 1
    try:
        reg = _load_registry(settings)
        sweepable = reg.sweepable()
        by_ats: dict[str, int] = {}
        for e in sweepable:
            by_ats[e.ats] = by_ats.get(e.ats, 0) + 1
        catalog.update(
            employers=len(reg),
            sweepable=len(sweepable),
            manual=len(reg.manual()),
            verified=sum(1 for e in sweepable if e.verified),
            by_ats=dict(sorted(by_ats.items(), key=lambda kv: -kv[1])),
        )
    except Exception as exc:  # noqa: BLE001
        catalog["registry_error"] = str(exc)[:300]
        problems += 1
    report["catalog"] = catalog
    db: dict[str, Any] = {"path": str(settings.db_path), "exists": Path(settings.db_path).exists()}
    try:
        store = _open_store(settings)
        try:
            st = store.stats()
            db.update(fts=getattr(store, "fts", None), stats=st)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        db["error"] = str(exc)[:300]
        problems += 1
    report["db"] = db

    if args.json:
        _print_json(report)
        return 1 if problems else 0
    _out(f"jobsweep {__version__} · Python {report['python']}")
    _out("")
    _out("Sources")
    _out(
        _table(
            ["name", "kind", "available", "metered"],
            [
                [
                    r["name"],
                    r["kind"] or "-",
                    "ok" if r["available"] else f"no: {r['reason']}",
                    "yes" if r["metered"] else "",
                ]
                for r in rows
            ],
            [16, 12, 70, 7],
        )
    )
    _out("")
    _out("Environment (values never shown)")
    for k, present in report["env"].items():
        _out(f"  {k:<20} {'set' if present else '-'}")
    _out("")
    _out("Catalog")
    if "taxonomy_error" in catalog:
        _out(f"  taxonomy: ERROR {catalog['taxonomy_error']}")
    else:
        _out(
            f"  lanes: {catalog.get('lanes')} ({catalog.get('active_lanes')} active) in {catalog.get('groups')} groups; profile min_score {catalog.get('min_score')}"
        )
    if "registry_error" in catalog:
        _out(f"  employers: ERROR {catalog['registry_error']}")
    else:
        ats = ", ".join(f"{k} {v}" for k, v in catalog.get("by_ats", {}).items())
        _out(
            f"  employers: {catalog.get('employers')} ({catalog.get('sweepable')} sweepable, "
            f"{catalog.get('verified')} verified, {catalog.get('manual')} manual)"
        )
        if ats:
            _out(f"  boards by ATS: {ats}")
    _out("")
    _out("Database")
    _out(f"  path: {db['path']}")
    if "error" in db:
        _out(f"  ERROR {db['error']}")
    else:
        st = db["stats"]
        _out(
            f"  jobs: {st['total']} ({st['active']} active, {st['new_24h']} new in 24h, {st['new_7d']} in 7d); full-text search: {'fts5' if db.get('fts') else 'LIKE'}"
        )
        last = st.get("last_run")
        if last:
            s = last.get("stats") or {}
            _out(
                f"  last run: #{last['id']} {last['started_at']} -> {last.get('finished_at') or 'unfinished'} ({s.get('new', '?')} new, {s.get('errors', '?')} errors)"
            )
        else:
            _out("  last run: never")
    return 1 if problems else 0


def cmd_lanes(args: argparse.Namespace, settings: Settings) -> int:
    tax = _load_taxonomy(settings)
    data = tax.to_dict()
    store = _open_store(settings)
    try:
        st = store.stats()
    finally:
        store.close()
    groups = []
    for g in data.get("groups", []):
        if not args.all and g.get("active") is False:
            continue
        g = dict(g)
        g["count"] = st["by_group"].get(g.get("id"), 0)
        g["lanes"] = [
            dict(lane, count=st["by_lane"].get(lane.get("id"), 0))
            for lane in g.get("lanes", [])
            if args.all or lane.get("active") is not False
        ]
        groups.append(g)
    if args.json:
        _print_json({"groups": groups})
        return 0
    for g in groups:
        flag = "" if g.get("active", True) else "  [inactive]"
        _out(f"{g['id']}  {g.get('name', '')}  ({g['count']}){flag}")
        for lane in g["lanes"]:
            lflag = "" if lane.get("active", True) else "  [inactive]"
            _out(f"    {lane['id']:<34} {_clip(lane.get('name', ''), 60):<60} {lane['count']:>5}{lflag}")
    return 0


def cmd_employers(args: argparse.Namespace, settings: Settings) -> int:
    reg = _load_registry(settings)
    if args.manual:
        emps = reg.manual()
    elif args.all:
        emps = [e for e in reg.all() if e.active]
    else:
        emps = reg.sweepable()
    if args.group:
        emps = [e for e in emps if args.group in e.groups]
    emps.sort(key=lambda e: e.key)
    if args.json:
        _print_json([e.to_dict() for e in emps])
        return 0
    if args.manual:
        _out(
            _table(
                ["key", "name", "vendor", "careers_url"],
                [[e.key, e.name, e.ats_vendor or e.ats or "", e.careers_url or ""] for e in emps],
                [28, 36, 14, 70],
            )
        )
    else:
        rows = [
            [
                e.key,
                e.name,
                e.ats or "manual",
                e.board or e.ats_vendor or "",
                e.verified or "",
                "" if e.open_jobs is None else e.open_jobs,
                ",".join(e.groups),
            ]
            for e in emps
        ]
        _out(
            _table(
                ["key", "name", "ats", "board", "verified", "open", "groups"],
                rows,
                [28, 32, 15, 36, 10, 5, 50],
            )
        )
    _out(f"\n{len(emps)} employers")
    return 0


def _progress_printer(enabled: bool) -> Callable[[dict], None] | None:
    if not enabled:
        return None

    def cb(evt: dict) -> None:
        if evt.get("event") == "plan":
            c = evt.get("counts", {})
            _err(f"plan: {c.get('boards', 0)} boards + {c.get('searches', 0)} searches")
        elif evt.get("event") == "target_done":
            if evt.get("ok"):
                status = f"{evt.get('jobs', 0)} jobs, {evt.get('kept', 0)} kept, {evt.get('new', 0)} new"
            else:
                status = "ERROR " + _clip(evt.get("error"), 140)
            _err(
                f"[{evt.get('done')}/{evt.get('total')}] {evt.get('source')} {_clip(evt.get('target'), 50)}: {status}"
            )

    return cb


def cmd_sweep(args: argparse.Namespace, settings: Settings) -> int:
    from .sweep import SweepOptions, run_sweep_sync

    opts = SweepOptions(
        sources=_split(args.sources),
        lanes=_split(args.lanes),
        groups=_split(args.groups),
        employers=_split(args.employers),
        include_metered=args.include_metered,
        boards=not args.no_boards,
        searches=not args.no_searches,
        dry_run=args.dry_run,
        store_unmatched=args.store_unmatched,
    )
    if args.max_queries is not None:
        opts.max_queries_per_source = args.max_queries
    if args.days is not None:
        opts.posted_within_days = args.days or None
    if args.concurrency:
        opts.concurrency = args.concurrency
    if args.enrich_threshold is not None:
        opts.enrich_threshold = args.enrich_threshold
    tax = _load_taxonomy(settings)
    reg = _load_registry(settings)
    store = None if args.dry_run else _open_store(settings)
    try:
        report = run_sweep_sync(
            settings, tax, reg, store, opts, _progress_printer(not args.quiet and not args.dry_run)
        )
    finally:
        if store is not None:
            store.close()
    if args.json:
        _print_json(report.to_dict())
    else:
        _out(report.summary())
        if args.dry_run and args.verbose:
            for b in report.plan.get("boards", []):
                _out(f"  board  {b['source']:<16} {b['employer']}")
            for s in report.plan.get("searches", []):
                _out(f"  search {s['source']:<16} {s['keywords']!r} lane={s.get('lane')}")
    if args.dry_run:
        return 0
    ok = sum(p.get("ok", 0) for p in report.per_source.values())
    if report.errors and (args.strict or ok == 0):
        return 1
    return 0


def _job_rows(rows: list[dict]) -> str:
    return _table(
        ["score", "E", "title", "company", "location", "lanes", "status", "uid"],
        [
            [
                f"{float(r.get('score') or 0):.0f}",
                _entry_flag(r.get("entry_level")),
                r.get("title"),
                r.get("company"),
                r.get("location"),
                ",".join((r.get("lanes") or [])[:2]),
                r.get("status") or "",
                r.get("uid"),
            ]
            for r in rows
        ],  # fmt: skip
        [5, 1, 56, 28, 26, 40, 9, 60],
    )


def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    from .models import SearchQuery
    from .sweep import _run_coro_sync, live_search

    query = SearchQuery(
        keywords=args.keywords,
        location=args.location,
        remote=True if args.remote else None,
        posted_within_days=args.days,
        limit=max(args.limit, 25),
    )
    tax = _load_taxonomy(settings)
    try:
        reg = _load_registry(settings)
    except Exception:  # noqa: BLE001
        reg = None
    store = None if args.no_store else _open_store(settings)
    errors: list[dict] = []
    try:
        jobs = _run_coro_sync(
            lambda: live_search(
                settings, tax, query, _split(args.sources), store, registry=reg,
                include_metered=args.include_metered, errors=errors,
            )
        )  # fmt: skip
    finally:
        if store is not None:
            store.close()
    shown = jobs[: args.limit]
    if args.json:
        _print_json({"total": len(jobs), "items": [j.to_dict() for j in shown], "errors": errors})
    else:
        if shown:
            _out(_job_rows([j.to_dict() for j in shown]))
        _out(f"\n{len(shown)} of {len(jobs)} results" + ("" if args.no_store else " (stored)"))
        for e in errors:
            _err(f"warning: {e['source']}: {e['error']}")
    return 1 if errors and not jobs else 0


def _list_filters(args: argparse.Namespace, settings: Settings) -> dict:
    statuses = _split(args.status)
    if statuses is None:
        statuses = [s for s in JOB_STATUSES if s != "dismissed"]
    elif "all" in statuses:
        statuses = None
    min_score = args.min_score if args.min_score is not None else _min_score_default(settings)
    return {
        "q": args.q,
        "lanes": _split(args.lane),
        "groups": _split(args.group),
        "sources": _split(args.source),
        "statuses": statuses,
        "entry_only": bool(args.entry or args.entry_strict),
        "include_unknown_entry": not args.entry_strict,
        "min_score": min_score,
        "location": args.location,
        "remote": True if args.remote else None,
        "since": args.days if args.days else None,
        "active_only": not args.inactive,
        "include_duplicates": args.duplicates,
        "include_inactive_lanes": args.inactive_lanes,
    }


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    filters = _list_filters(args, settings)
    store = _open_store(settings)
    try:
        total = store.count(**filters)
        limit = None if args.csv and args.limit == 50 else args.limit
        rows = store.query(**filters, sort=args.sort, limit=limit, offset=args.offset)
    finally:
        store.close()
    if args.csv:
        from .export import write_csv

        write_csv(rows, args.csv)
        if args.csv != "-":
            _out(f"wrote {len(rows)} rows to {args.csv}")
        return 0
    if args.json:
        _print_json({"total": total, "items": rows})
        return 0
    if rows:
        _out(_job_rows(rows))
    _out(f"\n{len(rows)} of {total} jobs (min score {filters['min_score']:g}, sort {args.sort})")
    return 0


def cmd_show(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(settings)
    try:
        row = store.get(args.uid)
    finally:
        store.close()
    if row is None:
        _err(f"not found: {args.uid}")
        return 1
    if args.json:
        _print_json(row)
        return 0
    _out(f"{row.get('title')} — {row.get('company')}")
    fields = [
        ("uid", row.get("uid")), ("url", row.get("url")), ("location", row.get("location")),
        ("remote", row.get("remote")), ("score", f"{float(row.get('score') or 0):.1f}"),
        ("entry level", row.get("entry_level")), ("years required", row.get("years_required")),
        ("lanes", ", ".join(row.get("lanes") or [])), ("groups", ", ".join(row.get("groups") or [])),
        ("posted", row.get("posted_at")), ("closes", row.get("closes_at")),
        ("type", row.get("employment_type")), ("grade", row.get("grade")),
        ("salary", " ".join(str(x) for x in (row.get("salary_min"), row.get("salary_max"), row.get("salary_currency"), row.get("salary_period")) if x)),
        ("status", row.get("status")), ("note", row.get("note")),
        ("first seen", row.get("first_seen")), ("last seen", row.get("last_seen")),
        ("active", row.get("active")), ("duplicate of", row.get("duplicate_of")),
    ]  # fmt: skip
    for k, v in fields:
        if v not in (None, "", []):
            _out(f"  {k:<15} {v}")
    if row.get("match_reasons"):
        _out("  why:")
        for reason in row["match_reasons"]:
            _out(f"    - {reason}")
    if row.get("description"):
        _out("")
        _out(row["description"])
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(settings)
    try:
        row = store.set_status(args.uid, args.status, args.note)
    finally:
        store.close()
    if row is None:
        _err(f"not found: {args.uid}")
        return 1
    _out(f"{row['uid']}: {row['status']}" + (f" — {row['note']}" if row.get("note") else ""))
    return 0


def _detect_format(path: str, text: str) -> str:
    suffix = Path(path.removesuffix(".gz")).suffix.lower()
    if suffix in (".json", ".jsonl"):
        return "json"
    if suffix == ".csv":
        return "csv"
    if suffix in (".md", ".markdown"):
        return "md"
    head = text.lstrip()[:2000]
    if head.startswith(("[", "{")):
        return "json"
    if "**" in head or head.startswith("#"):
        return "md"
    first = head.splitlines()[0] if head else ""
    return "csv" if first.count(",") >= 1 else "md"


def parse_import(payload: Any, origin: str, fmt: str | None, settings: Settings | None = None) -> list[Job]:
    """Call ``ImportSource.parse`` whatever its exact shape (static/class/instance method, fmt kwarg)."""
    from .sources.base import SourceContext
    from .sources.manual_import import ImportSource

    raw_attr = inspect.getattr_static(ImportSource, "parse")
    if isinstance(raw_attr, (staticmethod, classmethod)):
        parse = ImportSource.parse
    else:
        parse = ImportSource(SourceContext(client=None, settings=settings or Settings.load())).parse  # type: ignore[arg-type]
    params = inspect.signature(parse).parameters
    kwargs: dict[str, Any] = {}
    for name in ("fmt", "format"):
        if name in params and fmt:
            kwargs[name] = fmt
    if "origin" in params:
        result = parse(payload, origin=origin, **kwargs)
    else:
        result = parse(payload, origin, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result)  # type: ignore[arg-type]
    jobs = []
    for item in result or []:
        jobs.append(item if isinstance(item, Job) else Job.from_dict(item))
    return jobs


def import_jobs(
    jobs: Sequence[Job | dict], settings: Settings | None, store: Any, taxonomy: Any, registry: Any
) -> dict:
    """Classify (with employer priors when the company is in the registry) and upsert.

    Returns ``{"imported": n, "new": n_new, "new_uids": [...]}``.
    """
    jobs = [j if isinstance(j, Job) else Job.from_dict(j) for j in jobs]
    lookup = _employer_by_name(registry)
    for i, job in enumerate(jobs):
        emp = lookup(job)
        if emp is not None and not job.employer_key:
            job.employer_key = emp.key
        res = taxonomy.classify(job, emp)
        if isinstance(res, Job):
            jobs[i] = res
    result = store.upsert_jobs(jobs)
    return {"imported": len(jobs), "new": len(result.get("new", [])), "new_uids": result.get("new", [])}


def cmd_import(args: argparse.Namespace, settings: Settings) -> int:
    try:
        text = _read_text_file(args.file)
    except OSError as exc:
        _err(f"cannot read {args.file}: {exc}")
        return 1
    fmt = args.format or _detect_format(args.file, text)
    jobs = parse_import(text, args.origin, fmt, settings)
    if not jobs:
        _err(f"no jobs found in {args.file} (format {fmt})")
        return 1
    tax = _load_taxonomy(settings)
    try:
        reg = _load_registry(settings)
    except Exception:  # noqa: BLE001
        reg = None
    store = _open_store(settings)
    try:
        res = import_jobs(jobs, settings, store, tax, reg)
    finally:
        store.close()
    if args.json:
        _print_json(res)
    else:
        _out(f"imported {res['imported']} jobs from {args.file} ({fmt}); {res['new']} new")
    return 0


async def detect_employer(settings: Settings, text: str, *, probe: bool = True) -> dict:
    """URL -> boards found on the page; company name -> slug probes on the JSON ATS APIs."""
    from .http import make_client
    from .sources import detect as det
    from .sources.base import SourceContext

    text = text.strip()
    is_url = bool(
        re.match(r"^(https?://|www\.)", text, re.I) or re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", text, re.I)
    )
    out: dict[str, Any] = {"input": text, "kind": "url" if is_url else "name", "boards": []}
    async with make_client(settings.user_agent) as client:
        ctx = SourceContext(client=client, settings=settings)
        if is_url:
            url = text if text.lower().startswith("http") else "https://" + text
            pairs: list[tuple[str, str]] = []
            direct = det.detect_from_url(url, include_vendors=True)
            if direct:
                pairs.append(tuple(direct))  # type: ignore[arg-type]
            if probe:
                for p in await det.scan_careers_page(ctx, url, include_vendors=True):
                    if tuple(p) not in pairs:
                        pairs.append(tuple(p))  # type: ignore[arg-type]
            sweepable = set(getattr(det, "SWEEPABLE_ATS", ()) or ())
            out["boards"] = [
                {"ats": a, "board": b, "sweepable": (a in sweepable) if sweepable else None} for a, b in pairs
            ]
        elif probe:
            out["boards"] = [dict(h, sweepable=True) for h in await det.probe_company(ctx, text)]
        else:
            out["slugs"] = det.slug_variants(text)
    return out


def cmd_detect(args: argparse.Namespace, settings: Settings) -> int:
    from .sweep import _run_coro_sync

    res = _run_coro_sync(lambda: detect_employer(settings, args.target, probe=not args.no_probe))
    matches = []
    try:
        reg = _load_registry(settings)
        needle = args.target.lower()
        for e in reg.all():
            if (
                needle in (e.name or "").lower()
                or needle == e.key
                or (e.careers_url and needle in e.careers_url.lower())
            ):
                matches.append(e.to_dict())
    except Exception:  # noqa: BLE001
        pass
    res["registry_matches"] = matches
    if args.json:
        _print_json(res)
        return 0 if res["boards"] or matches else 1
    for m in matches:
        _out(f"in registry: {m['key']} ({m['name']}) ats={m['ats']} board={m['board']}")
    if not res["boards"]:
        _out(f"no ATS board found for {args.target!r}")
        return 0 if matches else 1
    for b in res["boards"]:
        extra = f" open_jobs={b['open_jobs']}" if b.get("open_jobs") is not None else ""
        titles = f"  e.g. {'; '.join(t for t in b.get('titles') or [] if t)}" if b.get("titles") else ""
        note = "" if b.get("sweepable") is not False else "  (vendor only: set ats: other, ats_vendor)"
        _out(f"{b['ats']:<16} {b['board']}{extra}{note}{titles}")
    return 0


# ---- verify-employers ------------------------------------------------------------------------------

_ITEM_RE = re.compile(r"^(?P<indent> *)-[ ]+(?P<rest>\S.*)?$")


def _employer_blocks(lines: list[str]) -> list[tuple[int, int, int]]:
    """(start, end, field_indent) for each item of the top-level ``employers:`` list."""
    start = next((i for i, line in enumerate(lines) if re.match(r"^employers:\s*(#.*)?$", line)), None)
    if start is None:
        return []
    blocks: list[tuple[int, int, int]] = []
    item_indent: int | None = None
    cur: tuple[int, int] | None = None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        m = _ITEM_RE.match(line)
        if item_indent is None:
            if not m:
                end = i
                break
            item_indent = indent
        if m and indent == item_indent:
            if cur is not None:
                blocks.append((cur[0], i, cur[1]))
            cur = (i, m.start("rest") if m.group("rest") else indent + 2)
        elif indent <= item_indent:
            end = i
            break
    if cur is not None:
        blocks.append((cur[0], end, cur[1]))
    return blocks


def _field_index(lines: list[str], block: tuple[int, int, int], name: str) -> int | None:
    s, e, fi = block
    for i in range(s, e):
        line = lines[i]
        if i != s and (len(line) - len(line.lstrip(" ")) != fi):
            continue
        if line[fi:].startswith(name + ":"):
            return i
    return None


def _field_value(lines: list[str], block: tuple[int, int, int], name: str) -> str | None:
    i = _field_index(lines, block, name)
    if i is None:
        return None
    raw = lines[i][block[2] + len(name) + 1 :].strip()
    m = re.match(r"^(\"[^\"]*\"|'[^']*'|[^#]*?)(\s+#.*)?$", raw)
    val = (m.group(1) if m else raw).strip()
    return val[1:-1] if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'" else val


def _set_field(lines: list[str], block: tuple[int, int, int], name: str, value: str) -> None:
    s, e, fi = block
    i = _field_index(lines, block, name)
    if i is not None:
        raw = lines[i][fi + len(name) + 1 :].strip()
        m = re.match(r"^(\"[^\"]*\"|'[^']*'|[^#]*?)(\s+#.*)?$", raw)
        comment = (m.group(2) or "") if m else ""
        lines[i] = f"{lines[i][:fi]}{name}: {value}{comment}"
        return
    j = e - 1
    while j > s and (not lines[j].strip() or lines[j].strip().startswith("#")):
        j -= 1
    lines.insert(j + 1, " " * fi + f"{name}: {value}")


def update_employer_yaml(
    text: str, key: str, updates: dict[str, Any], board: str | None = None
) -> tuple[str, int]:
    """Set fields (e.g. verified/open_jobs) on the employer ``key`` in one YAML file's text, line by line
    (comments and order preserved). With ``board`` only blocks whose board matches are edited.
    Returns (new_text, blocks_edited); the result is re-parsed and rejected if it no longer round-trips."""
    import yaml

    lines = text.splitlines()
    edited = 0
    for block in reversed(_employer_blocks(lines)):
        if _field_value(lines, block, "key") != key:
            continue
        if board is not None and _field_value(lines, block, "board") != board:
            continue
        for name, value in updates.items():
            rendered = f'"{value}"' if isinstance(value, str) else ("null" if value is None else str(value))
            _set_field(lines, block, name, rendered)
        edited += 1
    if not edited:
        return text, 0
    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    try:
        doc = yaml.safe_load(new_text) or {}
        rows = [r for r in doc.get("employers") or [] if isinstance(r, dict) and r.get("key") == key]
        ok = bool(rows) and all(
            str(r.get(k)) == str(v)
            for r in rows
            if board is None or str(r.get("board")) == board
            for k, v in updates.items()
        )
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        log.warning("YAML edit for %s did not round-trip; leaving file unchanged", key)
        return text, 0
    return new_text, edited


def write_verification(results: list[dict], directory: Path, today: str | None = None) -> list[Path]:
    """Write ``verified``/``open_jobs`` for successful board checks into the employer YAML files."""
    today = today or date.today().isoformat()
    files = sorted(directory.glob("*.yaml"))
    texts = {f: f.read_text(encoding="utf-8") for f in files}
    changed: set[Path] = set()
    for r in results:
        if r.get("error") or r.get("count") is None:
            continue
        updates: dict[str, Any] = {"open_jobs": int(r["count"])}
        if r["count"] > 0:
            updates["verified"] = today
        board = str(r["board"]) if r.get("board") is not None else None
        # Prefer blocks whose board matches (same org listed in several group files); else first block.
        targets = [f for f in files if update_employer_yaml(texts[f], r["key"], updates, board)[1]]
        if targets:
            for f in targets:
                texts[f], _ = update_employer_yaml(texts[f], r["key"], updates, board)
                changed.add(f)
            continue
        for f in files:
            new, n = update_employer_yaml(texts[f], r["key"], updates)
            if n:
                texts[f] = new
                changed.add(f)
                break
    for f in sorted(changed):
        f.write_text(texts[f], encoding="utf-8")
    return sorted(changed)


def cmd_verify_employers(args: argparse.Namespace, settings: Settings) -> int:
    from .sweep import _run_coro_sync, verify_boards

    reg = _load_registry(settings)
    emps = reg.select(keys=_split(args.employers), groups=[args.group] if args.group else None)
    if not emps:
        _err("no sweepable employers selected")
        return 1
    results = _run_coro_sync(lambda: verify_boards(settings, emps, concurrency=args.concurrency))
    errors = [r for r in results if r["error"]]
    written: list[Path] = []
    if args.write:
        written = write_verification(results, _employers_dir())
    if args.json:
        _print_json({"results": results, "errors": len(errors), "written": [str(p) for p in written]})
    else:
        rows = [
            [
                r["key"],
                r["ats"],
                r["board"],
                "" if r["count"] is None else r["count"],
                r["error"] or "; ".join(t for t in r["sample"] if t),
            ]
            for r in sorted(results, key=lambda r: (r["error"] is None, r["key"]))
        ]
        _out(_table(["key", "ats", "board", "jobs", "error / sample"], rows, [28, 15, 36, 5, 90]))
        zero = sum(1 for r in results if r["count"] == 0)
        _out(f"\n{len(results)} boards: {len(results) - len(errors)} ok ({zero} empty), {len(errors)} errors")
        for p in written:
            _out(f"updated {p}")
    return 1 if errors else 0


# ---- export / digest / state ----------------------------------------------------------------------


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    from .export import export_site

    tax = _load_taxonomy(settings)
    reg = _load_registry(settings)
    out = Path(args.out) if args.out else settings.site_dir
    store = _open_store(settings)
    try:
        path = export_site(
            store,
            tax,
            reg,
            out,
            min_score=args.min_score,
            days=args.days,
            static_dir=args.static,
            settings=settings,
        )
    finally:
        store.close()
    try:
        total = json.loads((path / "data" / "jobs.json").read_text(encoding="utf-8")).get("total")
    except Exception:  # noqa: BLE001
        total = "?"
    _out(f"exported {total} jobs to {path}")
    return 0


def cmd_digest(args: argparse.Namespace, settings: Settings) -> int:
    from .export import digest

    try:
        tax = _load_taxonomy(settings)
    except Exception:  # noqa: BLE001
        tax = None
    store = _open_store(settings)
    try:
        text = digest(
            store,
            days=args.days,
            min_score=args.min_score,
            fmt=args.format,
            taxonomy=tax,
            limit_per_lane=args.limit_per_lane,
        )
    finally:
        store.close()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        _out(f"wrote {args.out}")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_state(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(settings)
    try:
        if args.state_cmd == "export":
            snap = store.export_snapshot(max_description=args.max_description)
            data = json.dumps(snap, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            if args.file == "-":
                sys.stdout.write(data.decode("utf-8") + "\n")
                return 0
            p = Path(args.file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(gzip.compress(data) if p.suffix == ".gz" else data)
            _out(f"exported {len(snap['jobs'])} jobs and {len(snap['runs'])} runs to {p}")
            return 0
        if args.state_cmd == "import":
            if args.file != "-" and not Path(args.file).exists():
                _err(f"no such file: {args.file}")
                return 1
            snap = json.loads(_read_text_file(args.file))
            n = store.import_snapshot(snap)
            _out(f"merged {n} of {len(snap.get('jobs') or [])} jobs from {args.file}")
            return 0
        if args.state_cmd == "prune":
            n = store.prune(older_than_days=args.days)
            _out(f"pruned {n} inactive untriaged jobs not seen for {args.days} days")
            return 0
    finally:
        store.close()
    return 2


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    if args.db:
        os.environ["JOBSWEEP_DB"] = str(settings.db_path)
    if args.reload:
        uvicorn.run("jobsweep.web.app:create_app", factory=True, host=args.host, port=args.port, reload=True)
    else:
        from .web.app import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def cmd_mcp(args: argparse.Namespace, settings: Settings) -> int:
    if args.db:
        os.environ["JOBSWEEP_DB"] = str(settings.db_path)
    from .mcp_server import main as mcp_main

    return int(mcp_main() or 0)


# ---- parser ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobsweep", description="Lane-based job sweeps (see ARCHITECTURE.md).")
    p.add_argument("--version", action="version", version=f"jobsweep {__version__}")
    p.add_argument("--db", help="SQLite database path (default: $JOBSWEEP_DB or <project>/var/jobsweep.db)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging and tracebacks")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    sp = add("doctor", cmd_doctor, "check environment, sources, catalog and database")
    sp.add_argument("--json", action="store_true")

    sp = add("lanes", cmd_lanes, "list lane groups and lanes with job counts")
    sp.add_argument("--all", action="store_true", help="include inactive (earlier) lanes")
    sp.add_argument("--json", action="store_true")

    sp = add("employers", cmd_employers, "list the employer registry")
    sp.add_argument("--group", "-g")
    sp.add_argument("--manual", action="store_true", help="only employers without a sweepable ATS board")
    sp.add_argument("--all", action="store_true", help="sweepable and manual employers")
    sp.add_argument("--json", action="store_true")

    sp = add("sweep", cmd_sweep, "run a sweep over employer boards and search sources")
    sp.add_argument(
        "--sources",
        action="append",
        help="comma-separated source names (default: all available, non-metered)",
    )
    sp.add_argument("--lanes", action="append")
    sp.add_argument("--groups", action="append")
    sp.add_argument(
        "--employers", action="append", help="employer keys (boards only unless --lanes/--groups/--sources)"
    )
    sp.add_argument("--include-metered", action="store_true")
    sp.add_argument("--no-boards", action="store_true")
    sp.add_argument("--no-searches", action="store_true")
    sp.add_argument("--max-queries", type=int, help="max search queries per source (default 40)")
    sp.add_argument("--days", type=int, help="search postings from the last N days (0 = any; default 30)")
    sp.add_argument("--concurrency", type=int)
    sp.add_argument("--enrich-threshold", type=float)
    sp.add_argument("--store-unmatched", action="store_true", help="also store jobs matching no lane")
    sp.add_argument("--dry-run", action="store_true", help="print the plan without fetching")
    sp.add_argument("--strict", action="store_true", help="exit 1 when any target fails")
    sp.add_argument("--quiet", "-q", action="store_true", help="no per-target progress on stderr")
    sp.add_argument("--json", action="store_true")

    sp = add("search", cmd_search, "live keyword search across search sources")
    sp.add_argument("keywords")
    sp.add_argument("--location", "-l")
    sp.add_argument("--remote", action="store_true")
    sp.add_argument("--sources", action="append")
    sp.add_argument("--include-metered", action="store_true")
    sp.add_argument("--days", type=int, help="posted within N days")
    sp.add_argument("--no-store", action="store_true")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--json", action="store_true")

    sp = add("list", cmd_list, "query stored jobs")
    sp.add_argument("--q", "-q", help="full-text query")
    sp.add_argument("--lane", action="append")
    sp.add_argument("--group", action="append")
    sp.add_argument("--source", action="append")
    sp.add_argument(
        "--status", action="append", help=f"{', '.join(JOB_STATUSES)} or all (default: all but dismissed)"
    )
    sp.add_argument("--entry", action="store_true", help="entry-level (incl. unknown)")
    sp.add_argument("--entry-strict", action="store_true", help="entry-level only (excl. unknown)")
    sp.add_argument("--min-score", type=float, help="default: profile min_score")
    sp.add_argument("--days", type=float, help="first seen within N days")
    sp.add_argument("--location")
    sp.add_argument("--remote", action="store_true")
    sp.add_argument("--inactive", action="store_true", help="include closed/expired jobs")
    sp.add_argument("--duplicates", action="store_true", help="include cross-source duplicates")
    sp.add_argument("--inactive-lanes", action="store_true", help="include jobs matching only earlier lanes")
    sp.add_argument("--sort", choices=SORTS, default="score")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--offset", type=int, default=0)
    fmt = sp.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--csv", metavar="FILE", help="write CSV ('-' for stdout)")

    sp = add("show", cmd_show, "show one job")
    sp.add_argument("uid")
    sp.add_argument("--json", action="store_true")

    sp = add("status", cmd_status, "set a job's status")
    sp.add_argument("uid")
    sp.add_argument("status", choices=JOB_STATUSES)
    sp.add_argument("--note")

    sp = add("import", cmd_import, "import jobs from a JSON/CSV/markdown file (e.g. Indeed connector output)")
    sp.add_argument("file", help="file path or '-' for stdin")
    sp.add_argument("--origin", default="indeed-connector")
    sp.add_argument("--format", choices=("json", "csv", "md"))
    sp.add_argument("--json", action="store_true")

    sp = add("detect", cmd_detect, "find the ATS board for a company name or careers URL")
    sp.add_argument("target", metavar="NAME_OR_URL")
    sp.add_argument("--no-probe", action="store_true", help="URL pattern match only (no network)")
    sp.add_argument("--json", action="store_true")

    sp = add("verify-employers", cmd_verify_employers, "fetch every sweepable board and report job counts")
    sp.add_argument("--group", "-g")
    sp.add_argument("--employers", action="append")
    sp.add_argument("--write", action="store_true", help="update verified/open_jobs in the YAML files")
    sp.add_argument("--concurrency", type=int, default=8)
    sp.add_argument("--json", action="store_true")

    sp = add("export", cmd_export, "write the static board (GitHub Pages)")
    sp.add_argument("--out", help="output directory (default: $JOBSWEEP_SITE_DIR or <project>/site)")
    sp.add_argument("--min-score", type=float)
    sp.add_argument("--days", type=int, help="only jobs first seen within N days")
    sp.add_argument("--static", help="UI source directory (default: package web/static)")

    sp = add("digest", cmd_digest, "new matches grouped by lane")
    sp.add_argument("--days", type=float, default=1)
    sp.add_argument("--format", choices=("md", "json"), default="md")
    sp.add_argument("--min-score", type=float)
    sp.add_argument("--limit-per-lane", type=int)
    sp.add_argument("--out", help="write to FILE instead of stdout")

    sp = add("state", cmd_state, "export/import the database as a JSON snapshot (.gz supported)")
    ssub = sp.add_subparsers(dest="state_cmd", metavar="ACTION", required=True)
    se = ssub.add_parser("export", help="write a snapshot")
    se.add_argument("file")
    se.add_argument("--max-description", type=int, help="truncate descriptions to N chars")
    si = ssub.add_parser("import", help="merge a snapshot")
    si.add_argument("file")
    sprune = ssub.add_parser("prune", help="delete inactive untriaged jobs not seen for N days")
    sprune.add_argument("--days", type=int, default=180)

    sp = add("serve", cmd_serve, "run the web board")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--reload", action="store_true")

    add("mcp", cmd_mcp, "run the stdio MCP server (same as jobsweep-mcp)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse: --help/--version -> 0, usage errors -> 2
        return int(exc.code or 0) if not isinstance(exc.code, str) else 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    settings = Settings.load()
    if args.db:
        settings.db_path = Path(args.db).expanduser()
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            traceback.print_exc()
        _err(f"error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
