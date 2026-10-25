"""Sweep orchestrator: plan board + search targets, run them concurrently, classify, store.

Flow (ARCHITECTURE.md §6): plan -> board targets (registry.select x adapter) + search targets
(taxonomy.search_plan per source, capped by budget) -> run under ``asyncio.Semaphore(concurrency)`` ->
``employer.title_filter`` -> ``title_signal`` -> ``enrich`` promising jobs -> ``classify`` ->
``upsert_jobs`` -> ``mark_board_complete`` per successful full board -> ``expire()`` -> ``finish_run``.
One failing target never aborts the sweep.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import sources as sources_mod
from .config import Settings
from .http import make_client
from .models import Employer, Job, SearchQuery
from .sources.base import Source, SourceContext, SourceUnavailable

if TYPE_CHECKING:
    from .employers import EmployerRegistry
    from .store import Store

log = logging.getLogger("jobsweep.sweep")

#: max ``enrich`` calls per board per sweep (detail fetches are the expensive part of big boards)
MAX_ENRICH_PER_BOARD = 60
#: sources never run by sweeps / live search (``import`` only parses pushed payloads)
NON_SWEEP_SOURCES = frozenset({"import"})
#: default per-board cap the adapters apply when ``employer.max_jobs`` is unset (§3.1)
DEFAULT_MAX_JOBS = {"workday": 400, "oracle": 400}
DEFAULT_MAX_JOBS_OTHER = 1000

Progress = Callable[[dict], Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str] | None:
    """None -> None; "a,b" -> ["a","b"]; list -> flattened list of stripped strings."""
    if value is None:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    out: list[str] = []
    for v in value:
        out.extend((_as_list(v) or []) if isinstance(v, str) else [str(v)])
    return out


@dataclass
class SweepOptions:
    sources: list[str] | None = None  # None = every available, non-metered source
    lanes: list[str] | None = None  # None = all active lanes
    groups: list[str] | None = None
    employers: list[str] | None = None  # employer keys
    include_metered: bool = False
    boards: bool = True  # run board sources over the employer registry
    searches: bool = True  # run search sources over taxonomy.search_plan
    max_queries_per_source: int = 40  # further capped by Source.query_budget
    posted_within_days: int | None = 30
    concurrency: int = 8
    enrich_threshold: float = 8.0  # title_signal >= this -> call source.enrich(job)
    dry_run: bool = False  # plan only
    #: keep jobs that match no lane/group and score below profile.min_score (off: keeps the DB/state small)
    store_unmatched: bool = False
    max_enrich_per_board: int = MAX_ENRICH_PER_BOARD

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SweepOptions:
        """Build from JSON-ish input (web/MCP): unknown keys ignored, "a,b" strings split into lists."""
        data = dict(data or {})
        kw: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data or data[f.name] is None:
                continue
            v = data[f.name]
            if f.name in ("sources", "lanes", "groups", "employers"):
                v = _as_list(v) or None
            elif f.name in ("include_metered", "boards", "searches", "dry_run", "store_unmatched"):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif f.name in (
                "max_queries_per_source",
                "posted_within_days",
                "concurrency",
                "max_enrich_per_board",
            ):
                v = int(v)
            elif f.name == "enrich_threshold":
                v = float(v)
            kw[f.name] = v
        return cls(**kw)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SweepReport:
    run_id: int | None
    started_at: str
    finished_at: str
    plan: dict
    per_source: dict[str, dict] = field(default_factory=dict)  # {"targets","ok","errors","jobs",...}
    errors: list[dict] = field(default_factory=list)  # {"source","target","error"}
    new_uids: list[str] = field(default_factory=list)
    total_jobs: int = 0
    dry_run: bool = False
    expired: int = 0
    deactivated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        counts = self.plan.get("counts", {})
        lines: list[str] = []
        head = "Sweep plan (dry run)" if self.dry_run else f"Sweep run #{self.run_id}"
        lines.append(f"{head}: {self.started_at} -> {self.finished_at}")
        lines.append(
            f"Targets: {counts.get('boards', 0)} boards, {counts.get('searches', 0)} searches"
            f" across {len(self.plan.get('sources', []))} sources"
        )
        if not self.dry_run:
            ok = sum(p.get("ok", 0) for p in self.per_source.values())
            fetched = sum(p.get("jobs", 0) for p in self.per_source.values())
            lines.append(f"Results: {ok} ok, {len(self.errors)} errors")
            lines.append(
                f"Jobs: {fetched} fetched, {self.total_jobs} stored, {len(self.new_uids)} new;"
                f" {self.deactivated} closed on boards, {self.expired} expired"
            )
        if self.per_source:
            lines.append("Per source:")
            for name, p in sorted(self.per_source.items()):
                lines.append(
                    f"  {name:<16} targets {p.get('targets', 0):>4}  ok {p.get('ok', 0):>4}  "
                    f"err {p.get('errors', 0):>3}  jobs {p.get('jobs', 0):>5}  kept {p.get('kept', 0):>5}  "
                    f"new {p.get('new', 0):>4}"
                )
        elif self.dry_run:
            by_source = counts.get("by_source", {})
            if by_source:
                lines.append("Planned per source:")
                for name, n in sorted(by_source.items()):
                    lines.append(f"  {name:<16} {n:>4} targets")
        for t in self.plan.get("truncated", {}).items():
            name, info = t
            lines.append(f"  note: {name} queries capped {info['planned']} -> {info['kept']}")
        skipped = self.plan.get("skipped_sources", [])
        if skipped:
            lines.append("Skipped sources: " + "; ".join(f"{s['source']} ({s['reason']})" for s in skipped))
        skipped_emp = self.plan.get("skipped_employers", [])
        if skipped_emp:
            by_reason: dict[str, list[str]] = {}
            for s in skipped_emp:
                by_reason.setdefault(f"{s.get('ats')}: {s['reason']}", []).append(s["employer"])
            lines.append(f"Skipped employers: {len(skipped_emp)}")
            for reason, keys in sorted(by_reason.items()):
                more = f" (+{len(keys) - 8} more)" if len(keys) > 8 else ""
                lines.append(f"  {reason}: {', '.join(keys[:8])}{more}")
        if self.errors:
            lines.append("Errors:")
            for e in self.errors[:40]:
                lines.append(f"  {e['source']} {e['target']}: {e['error'][:160]}")
            if len(self.errors) > 40:
                lines.append(f"  … {len(self.errors) - 40} more")
        return "\n".join(lines)


@dataclass
class _Target:
    kind: str  # "board" | "search"
    source: str
    employer: Employer | None = None
    query: SearchQuery | None = None

    @property
    def label(self) -> str:
        if self.employer is not None:
            return self.employer.key
        return self.query.keywords if self.query else "?"


# ---- helpers --------------------------------------------------------------------------------------


def _source_status(
    settings: Settings, name: str, *, explicit: bool, include_metered: bool
) -> tuple[type[Source] | None, str | None]:
    """(class, None) when usable, else (class-or-None, reason)."""
    if name in NON_SWEEP_SOURCES:
        return None, "import-only source (use `jobsweep import`)"
    cls = sources_mod.get_source_class(name)
    if cls is None:
        if name not in sources_mod.SOURCE_PATHS:
            return None, "unknown source"
        return None, "adapter missing or failed to import"
    if cls.metered and not (explicit or include_metered):
        return cls, "metered (use --include-metered or list it in --sources)"
    try:
        ok, reason = cls.availability(settings)
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"availability check failed: {exc}"
    if not ok:
        return cls, reason or "unavailable"
    return cls, None


def _lane_ids(taxonomy: Any, lanes: list[str] | None, groups: list[str] | None) -> list[str] | None:
    out: list[str] = list(lanes or [])
    if groups:
        for g in getattr(taxonomy, "groups", None) or []:
            if getattr(g, "id", None) in groups:
                out.extend(getattr(lane, "id", "") for lane in getattr(g, "lanes", []) or [])
    out = [x for x in dict.fromkeys(out) if x]
    return out or None


def _min_score(taxonomy: Any) -> float:
    try:
        return float(taxonomy.profile.min_score)
    except Exception:  # noqa: BLE001
        return 25.0


def apply_title_filter(jobs: list[Job], employer: Employer | None) -> list[Job]:
    """Keep jobs whose title matches ``employer.title_filter`` (case-insensitive); all if unset/invalid."""
    pattern = employer.title_filter if employer else None
    if not pattern:
        return list(jobs)
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        log.warning("bad title_filter for %s (%s); keeping all titles", employer.key, exc)
        return list(jobs)
    return [j for j in jobs if rx.search(j.title or "")]


async def enrich_promising(
    src: Source,
    jobs: list[Job],
    taxonomy: Any,
    employer: Employer | None,
    *,
    threshold: float,
    cap: int = MAX_ENRICH_PER_BOARD,
) -> tuple[list[Job], int, int]:
    """Call ``src.enrich`` on the best-scoring titles (signal >= threshold, at most ``cap``).

    Returns (jobs, enriched_count, enrich_errors). Enrich failures keep the original job.
    """
    scored: list[tuple[float, int]] = []
    for i, job in enumerate(jobs):
        try:
            signal = float(taxonomy.title_signal(job.title or "", employer))
        except Exception as exc:  # noqa: BLE001
            log.debug("title_signal failed for %s: %s", job.uid, exc)
            signal = 0.0
        if signal >= threshold:
            scored.append((signal, i))
    scored.sort(key=lambda t: (-t[0], t[1]))
    picks = [i for _, i in scored[: max(cap, 0)]]
    errors = 0

    async def one(i: int) -> None:
        nonlocal errors
        try:
            res = await src.enrich(jobs[i])
            if isinstance(res, Job):
                jobs[i] = res
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.debug("enrich failed for %s: %s", jobs[i].uid, exc)

    if picks:
        await asyncio.gather(*(one(i) for i in picks))
    return jobs, len(picks) - errors, errors


def _interleave(targets: list[_Target]) -> list[_Target]:
    """Round-robin targets by source so every host gets work early (keeps per-source order)."""
    queues: dict[str, list[_Target]] = {}
    for t in targets:
        queues.setdefault(t.source, []).append(t)
    out: list[_Target] = []
    while queues:
        for name in list(queues):
            out.append(queues[name].pop(0))
            if not queues[name]:
                del queues[name]
    return out


def _classify(taxonomy: Any, job: Job, employer: Employer | None) -> Job:
    res = taxonomy.classify(job, employer)
    return res if isinstance(res, Job) else job


async def _emit(progress: Progress | None, event: dict) -> None:
    if progress is None:
        return
    try:
        res = progress(event)
        if inspect.isawaitable(res):
            await res
    except Exception:  # noqa: BLE001 - a broken UI callback must not break the sweep
        log.exception("progress callback failed")


# ---- planning -------------------------------------------------------------------------------------


def build_plan(
    settings: Settings, taxonomy: Any, registry: EmployerRegistry | None, options: SweepOptions
) -> tuple[dict, list[_Target], dict[str, type[Source]]]:
    """Resolve sources and targets without touching the network."""
    explicit = _as_list(options.sources) or []
    names = explicit or [n for n in sources_mod.SOURCE_PATHS if n not in NON_SWEEP_SOURCES]
    selected: dict[str, type[Source]] = {}
    reasons: dict[str, str] = {}
    skipped_sources: list[dict] = []
    for name in dict.fromkeys(names):
        cls, reason = _source_status(
            settings, name, explicit=bool(explicit), include_metered=options.include_metered
        )
        if reason:
            reasons[name] = reason
            skipped_sources.append({"source": name, "reason": reason})
            continue
        selected[name] = cls  # type: ignore[assignment]

    targets: list[_Target] = []
    skipped_employers: list[dict] = []
    if options.boards and registry is not None:
        employers = registry.select(keys=options.employers, groups=options.groups, lanes=options.lanes)
        for emp in employers:
            ats = emp.ats or ""
            if explicit and ats not in explicit:
                continue
            cls = selected.get(ats)
            if cls is None or "board" not in cls.kind:
                reason = reasons.get(ats) or (
                    "no board adapter" if ats in sources_mod.SOURCE_PATHS else "unknown ats"
                )
                skipped_employers.append({"employer": emp.key, "ats": ats, "reason": reason})
                continue
            targets.append(_Target("board", ats, employer=emp))
        if options.employers:
            found = {e.key for e in employers}
            for key in options.employers:
                if key not in found:
                    emp = registry.get(key)
                    reason = "not in registry" if emp is None else "not sweepable (no supported ATS board)"
                    skipped_employers.append(
                        {"employer": key, "ats": emp.ats if emp else None, "reason": reason}
                    )

    truncated: dict[str, dict] = {}
    plan_errors: list[dict] = []
    # --employers alone means "check these boards", not "run every keyword search too".
    employers_only = bool(options.employers) and not (options.lanes or options.groups or explicit)
    if options.searches and not employers_only:
        lane_ids = _lane_ids(taxonomy, options.lanes, options.groups)
        for name, cls in selected.items():
            if "search" not in cls.kind:
                continue
            try:
                queries = list(
                    taxonomy.search_plan(lane_ids, source=name, posted_within_days=options.posted_within_days)
                )
            except Exception as exc:  # noqa: BLE001
                plan_errors.append({"source": name, "target": "search_plan", "error": str(exc)[:300]})
                continue
            cap = max(int(options.max_queries_per_source), 0)
            try:
                budget = cls.query_budget(settings)
            except Exception:  # noqa: BLE001
                budget = None
            if budget is not None:
                cap = min(cap, int(budget))
            if len(queries) > cap:
                truncated[name] = {"planned": len(queries), "kept": cap}
            targets.extend(_Target("search", name, query=q) for q in queries[:cap])

    by_source: dict[str, int] = {}
    for t in targets:
        by_source[t.source] = by_source.get(t.source, 0) + 1
    plan = {
        "sources": sorted({t.source for t in targets}),
        "boards": [
            {
                "source": t.source,
                "employer": t.employer.key,
                "name": t.employer.name,
                "board": t.employer.board,
            }
            for t in targets
            if t.kind == "board" and t.employer is not None
        ],
        "searches": [
            {
                "source": t.source,
                "keywords": t.query.keywords,
                "lane": t.query.lane,
                "location": t.query.location,
                "extra": t.query.extra,
            }
            for t in targets
            if t.kind == "search" and t.query is not None
        ],
        "skipped_sources": skipped_sources,
        "skipped_employers": skipped_employers,
        "truncated": truncated,
        "errors": plan_errors,
        "counts": {
            "boards": sum(1 for t in targets if t.kind == "board"),
            "searches": sum(1 for t in targets if t.kind == "search"),
            "by_source": by_source,
        },
    }
    return plan, targets, selected


# ---- execution ------------------------------------------------------------------------------------


class _Runner:
    def __init__(self, settings, taxonomy, registry, store, options: SweepOptions, progress, run_id):
        self.settings = settings
        self.taxonomy = taxonomy
        self.registry = registry
        self.store = store
        self.options = options
        self.progress = progress
        self.run_id = run_id
        self.min_score = _min_score(taxonomy)
        self.per_source: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.new_uids: list[str] = []
        self.stored: set[str] = set()
        self.deactivated = 0
        self.done = 0
        self.total = 0
        self.instances: dict[str, Source] = {}

    def _stats(self, source: str) -> dict:
        return self.per_source.setdefault(
            source,
            {
                "targets": 0,
                "ok": 0,
                "errors": 0,
                "jobs": 0,
                "kept": 0,
                "new": 0,
                "enriched": 0,
                "filtered": 0,
            },
        )

    def _keep(self, job: Job) -> bool:
        return bool(
            self.options.store_unmatched or job.lanes or job.groups or (job.score or 0) >= self.min_score
        )

    def _employer_for(self, job: Job) -> Employer | None:
        if self.registry is None or not job.employer_key:
            return None
        try:
            return self.registry.get(job.employer_key)
        except Exception:  # noqa: BLE001
            return None

    def _save(self, jobs: list[Job]) -> list[str]:
        if not jobs or self.store is None:
            self.stored.update(j.uid for j in jobs)
            return []
        res = self.store.upsert_jobs(jobs, self.run_id)
        self.stored.update(j.uid for j in jobs)
        dups = set(res.get("duplicates") or [])
        fresh = [u for u in res.get("new", []) if u not in dups]
        self.new_uids.extend(fresh)
        return fresh

    async def run_board(self, t: _Target) -> dict:
        src, emp, st = self.instances[t.source], t.employer, self._stats(t.source)
        assert emp is not None
        raw = [j for j in (await src.fetch_board(emp) or []) if isinstance(j, Job)]
        st["jobs"] += len(raw)
        jobs = apply_title_filter(raw, emp)
        st["filtered"] += len(raw) - len(jobs)
        jobs, enriched, _ = await enrich_promising(
            src,
            jobs,
            self.taxonomy,
            emp,
            threshold=self.options.enrich_threshold,
            cap=self.options.max_enrich_per_board,
        )
        st["enriched"] += enriched
        kept = []
        for job in jobs:
            if not job.employer_key:
                job.employer_key = emp.key
            job = _classify(self.taxonomy, job, emp)
            if self._keep(job):
                kept.append(job)
        fresh = self._save(kept)
        st["kept"] += len(kept)
        st["new"] += len(fresh)
        cap = emp.max_jobs or DEFAULT_MAX_JOBS.get(t.source, DEFAULT_MAX_JOBS_OTHER)
        complete = len(raw) < cap
        if complete and self.store is not None:
            self.deactivated += self.store.mark_board_complete(emp.key, {j.uid for j in raw}, source=t.source)
        return {"jobs": len(raw), "kept": len(kept), "new": len(fresh)}

    async def run_search(self, t: _Target) -> dict:
        src, q, st = self.instances[t.source], t.query, self._stats(t.source)
        assert q is not None
        raw = [j for j in (await src.search(q) or []) if isinstance(j, Job)]
        st["jobs"] += len(raw)
        kept = []
        for job in raw:
            job = _classify(self.taxonomy, job, self._employer_for(job))
            if self._keep(job):
                kept.append(job)
        fresh = self._save(kept)
        st["kept"] += len(kept)
        st["new"] += len(fresh)
        return {"jobs": len(raw), "kept": len(kept), "new": len(fresh)}

    async def run_target(self, t: _Target, sem: asyncio.Semaphore, source_sem: asyncio.Semaphore) -> None:
        # Per-source cap first, so targets queued behind a slow/rate-limited source hold no global slot.
        async with source_sem, sem:
            st = self._stats(t.source)
            st["targets"] += 1
            t0 = time.monotonic()
            event: dict[str, Any] = {
                "event": "target_done",
                "kind": t.kind,
                "source": t.source,
                "target": t.label,
            }
            try:
                res = await (self.run_board(t) if t.kind == "board" else self.run_search(t))
                st["ok"] += 1
                event.update(ok=True, error=None, **res)
            except Exception as exc:  # noqa: BLE001 - isolate every target
                msg = (str(exc) or type(exc).__name__)[:300]
                st["errors"] += 1
                self.errors.append({"source": t.source, "target": t.label, "error": msg})
                log.info("%s %s failed: %s", t.source, t.label, msg)
                event.update(ok=False, error=msg, jobs=0, kept=0, new=0)
            self.done += 1
            event.update(done=self.done, total=self.total, elapsed_s=round(time.monotonic() - t0, 2))
            await _emit(self.progress, event)


async def run_sweep(
    settings: Settings,
    taxonomy: Any,
    registry: EmployerRegistry | None,
    store: Store | None,
    options: SweepOptions | None = None,
    progress: Progress | None = None,
) -> SweepReport:
    """Run one sweep (see module docstring). ``progress(event)`` receives plan/target_done/finished events."""
    options = options or SweepOptions()
    started = _now_iso()
    plan, targets, selected = build_plan(settings, taxonomy, registry, options)
    await _emit(
        progress,
        {"event": "plan", "counts": plan["counts"], "total": len(targets), "dry_run": options.dry_run},
    )
    if options.dry_run:
        report = SweepReport(
            run_id=None, started_at=started, finished_at=_now_iso(), plan=plan, errors=list(plan["errors"]),
            dry_run=True,
        )  # fmt: skip
        await _emit(progress, {"event": "finished", "report": report.to_dict()})
        return report

    run_id = None
    if store is not None:
        run_id = store.start_run({"options": options.to_dict(), "counts": plan["counts"]})
    runner = _Runner(settings, taxonomy, registry, store, options, progress, run_id)
    runner.errors.extend(plan["errors"])
    runner.total = len(targets)
    expired = 0
    try:
        async with make_client(settings.user_agent) as client:
            ctx = SourceContext(client=client, settings=settings)
            for name in {t.source for t in targets}:
                try:
                    runner.instances[name] = selected[name](ctx)
                except Exception as exc:  # noqa: BLE001
                    runner.errors.append(
                        {"source": name, "target": "*", "error": f"init failed: {exc}"[:300]}
                    )
            runnable = _interleave([t for t in targets if t.source in runner.instances])
            runner.total = len(runnable)
            concurrency = max(int(options.concurrency), 1)
            sem = asyncio.Semaphore(concurrency)
            per_source = max(concurrency // 2, 2)
            source_sems = {name: asyncio.Semaphore(per_source) for name in runner.instances}
            await asyncio.gather(*(runner.run_target(t, sem, source_sems[t.source]) for t in runnable))
    finally:
        if store is not None:
            try:
                expired = store.expire()
            except Exception as exc:  # noqa: BLE001
                runner.errors.append({"source": "store", "target": "expire", "error": str(exc)[:300]})
    report = SweepReport(
        run_id=run_id,
        started_at=started,
        finished_at=_now_iso(),
        plan=plan,
        per_source=runner.per_source,
        errors=runner.errors,
        new_uids=runner.new_uids,
        total_jobs=len(runner.stored),
        expired=expired,
        deactivated=runner.deactivated,
    )
    if store is not None and run_id is not None:
        store.finish_run(
            run_id,
            {
                "targets": runner.total,
                "ok": sum(p["ok"] for p in runner.per_source.values()),
                "errors": len(runner.errors),
                "new": len(runner.new_uids),
                "total_jobs": report.total_jobs,
                "expired": expired,
                "deactivated": runner.deactivated,
                "per_source": runner.per_source,
                "error_list": runner.errors[:50],
            },
        )
    await _emit(progress, {"event": "finished", "report": report.to_dict()})
    return report


def _run_coro_sync(coro_factory: Callable[[], Any]) -> Any:
    """asyncio.run, or in a worker thread when this thread already runs an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def run_sweep_sync(*args: Any, **kwargs: Any) -> SweepReport:
    """Blocking wrapper around :func:`run_sweep` (same arguments)."""
    return _run_coro_sync(lambda: run_sweep(*args, **kwargs))


# ---- ad hoc operations ----------------------------------------------------------------------------


async def live_search(
    settings: Settings,
    taxonomy: Any,
    query: SearchQuery,
    sources: list[str] | None = None,
    store: Store | None = None,
    *,
    registry: EmployerRegistry | None = None,
    include_metered: bool = False,
    errors: list[dict] | None = None,
) -> list[Job]:
    """Run ``query`` on the given (or every available non-metered) search source; classified, best first.

    Failures are logged and appended to ``errors`` when given; they never raise.
    """
    explicit = _as_list(sources) or []
    names = explicit or [n for n in sources_mod.SOURCE_PATHS if n not in NON_SWEEP_SOURCES]
    errs = errors if errors is not None else []
    chosen: dict[str, type[Source]] = {}
    for name in dict.fromkeys(names):
        cls, reason = _source_status(settings, name, explicit=bool(explicit), include_metered=include_metered)
        if reason is None and cls is not None and "search" not in cls.kind:
            reason = "not a search source"
        if reason:
            if explicit:
                errs.append({"source": name, "target": query.keywords, "error": reason})
            continue
        chosen[name] = cls  # type: ignore[assignment]
    if not chosen:
        return []

    async with make_client(settings.user_agent) as client:
        ctx = SourceContext(client=client, settings=settings)

        async def one(name: str, cls: type[Source]) -> list[Job]:
            try:
                return [j for j in (await cls(ctx).search(query) or []) if isinstance(j, Job)]
            except Exception as exc:  # noqa: BLE001
                errs.append(
                    {
                        "source": name,
                        "target": query.keywords,
                        "error": (str(exc) or type(exc).__name__)[:300],
                    }
                )
                return []

        batches = await asyncio.gather(*(one(n, c) for n, c in chosen.items()))

    seen: dict[str, Job] = {}
    for job in (j for batch in batches for j in batch):
        if job.uid in seen:
            continue
        emp = registry.get(job.employer_key) if registry is not None and job.employer_key else None
        try:
            job = _classify(taxonomy, job, emp)
        except Exception as exc:  # noqa: BLE001
            errs.append({"source": job.source, "target": job.uid, "error": f"classify failed: {exc}"[:300]})
        seen[job.uid] = job
    jobs = sorted(seen.values(), key=lambda j: (-(j.score or 0), j.title or ""))
    if store is not None and jobs:
        store.upsert_jobs(jobs)
    return jobs


def _board_source(settings: Settings, employer: Employer) -> type[Source]:
    if not employer.ats or employer.ats == "other" or not employer.board:
        raise SourceUnavailable(f"{employer.key} has no supported ATS board (ats={employer.ats!r})")
    cls = sources_mod.get_source_class(employer.ats)
    if cls is None or "board" not in cls.kind:
        raise SourceUnavailable(f"no board adapter for ats {employer.ats!r}")
    ok, reason = cls.availability(settings)
    if not ok:
        raise SourceUnavailable(f"{employer.ats}: {reason}")
    return cls


async def board_snapshot(
    settings: Settings,
    taxonomy: Any,
    employer: Employer,
    *,
    apply_filter: bool = True,
    enrich_threshold: float | None = None,
) -> list[Job]:
    """Fetch one employer board now (no store): title_filter, enrich promising titles, classify; best first.

    Raises ``SourceUnavailable`` when the employer has no usable adapter; fetch errors propagate.
    """
    cls = _board_source(settings, employer)
    threshold = SweepOptions().enrich_threshold if enrich_threshold is None else enrich_threshold
    async with make_client(settings.user_agent) as client:
        src = cls(SourceContext(client=client, settings=settings))
        raw = [j for j in (await src.fetch_board(employer) or []) if isinstance(j, Job)]
        jobs = apply_title_filter(raw, employer) if apply_filter else raw
        jobs, _, _ = await enrich_promising(src, jobs, taxonomy, employer, threshold=threshold)
    out = []
    for job in jobs:
        if not job.employer_key:
            job.employer_key = employer.key
        out.append(_classify(taxonomy, job, employer))
    return sorted(out, key=lambda j: (-(j.score or 0), j.title or ""))


async def verify_boards(
    settings: Settings,
    employers: Iterable[Employer],
    *,
    concurrency: int = 8,
    progress: Progress | None = None,
) -> list[dict]:
    """Fetch each board (raw, no classification). Returns ``[{"key","ats","board","count","error","sample"}]``."""
    employers = list(employers)
    results: list[dict] = [
        {"key": e.key, "ats": e.ats, "board": e.board, "count": None, "error": None, "sample": []}
        for e in employers
    ]
    sem = asyncio.Semaphore(max(concurrency, 1))
    async with make_client(settings.user_agent) as client:
        ctx = SourceContext(client=client, settings=settings)

        async def one(i: int, emp: Employer) -> None:
            async with sem:
                try:
                    cls = _board_source(settings, emp)
                    jobs = [j for j in (await cls(ctx).fetch_board(emp) or []) if isinstance(j, Job)]
                    results[i]["count"] = len(jobs)
                    results[i]["sample"] = [j.title for j in jobs[:3]]
                except Exception as exc:  # noqa: BLE001
                    results[i]["error"] = (str(exc) or type(exc).__name__)[:300]
                await _emit(progress, {"event": "target_done", **results[i]})

        await asyncio.gather(*(one(i, e) for i, e in enumerate(employers)))
    return results
