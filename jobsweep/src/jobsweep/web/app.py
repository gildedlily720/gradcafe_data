"""FastAPI web board (ARCHITECTURE.md §9): the JSON API plus the static UI in ``web/static``.

``create_app(settings=None, store=None, taxonomy=None, registry=None)`` wires everything; any argument left
out is loaded lazily from the environment (``Settings.load()``, ``Store(settings.db_path)``,
``Taxonomy.load()``, ``EmployerRegistry.load()``). Sweeps run in one background thread at a time; their
progress events are kept in memory for ``GET /api/sweep/status``.

Run it with ``jobsweep serve`` or ``uvicorn 'jobsweep.web.app:create_app' --factory`` (binds 127.0.0.1).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import logging
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import CATALOG_DIR, STATIC_DIR, Settings
from ..models import JOB_STATUSES, Employer, Job, Profile

log = logging.getLogger("jobsweep.web")

SORTS = ("score", "newest", "posted", "closing", "company")
#: hard cap on ``limit`` for one page of /api/jobs
MAX_LIMIT = 1000
#: description length in ``compact=1`` list responses (the detail endpoint returns the full text)
COMPACT_DESCRIPTION = 360
#: target_done events kept for the live progress log
RECENT_EVENTS = 40
#: plan lists / new uids kept in ``last_report`` (the full plan can list hundreds of targets)
REPORT_LIST_CAP = 300
IMPORT_FORMATS = ("auto", "json", "csv", "md")

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _flag(value: Any) -> bool | None:
    """Query-string boolean: "1/true/yes/on" -> True, "0/false/no/off" -> False, missing/other -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _split(values: list[str] | str | None) -> list[str]:
    """Repeated and/or comma-separated query values -> flat list."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [v.strip() for raw in values for v in str(raw).split(",") if v.strip()]


# ---- taxonomy fallback ----------------------------------------------------------------------------


class CatalogTaxonomy:
    """Read-only stand-in used when ``taxonomy.py`` cannot be loaded.

    Exposes group/lane names from ``catalog/lanes.yaml`` and the profile, so the board still renders;
    ``classify`` is a no-op and ``search_plan`` returns nothing (sweeps are refused instead).
    """

    available = False

    def __init__(self, lanes_path: str | Path | None = None, profile_path: str | Path | None = None):
        import yaml

        self.groups: list[SimpleNamespace] = []
        self.lanes: dict[str, SimpleNamespace] = {}
        try:
            doc = yaml.safe_load(Path(lanes_path or CATALOG_DIR / "lanes.yaml").read_text("utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot read lanes.yaml: %s", exc)
            doc = {}
        for g in doc.get("groups") or []:
            if not isinstance(g, dict) or not g.get("id"):
                continue
            g_active = g.get("active", True) is not False
            lanes = []
            for raw in g.get("lanes") or []:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                lane = SimpleNamespace(
                    id=raw["id"],
                    name=raw.get("name") or raw["id"],
                    group=g["id"],
                    active=g_active and raw.get("active", True) is not False,
                    queries=list(raw.get("queries") or []),
                )
                lanes.append(lane)
                self.lanes[lane.id] = lane
            self.groups.append(
                SimpleNamespace(id=g["id"], name=g.get("name") or g["id"], active=g_active, lanes=lanes)
            )
        self.profile = Profile()
        try:
            ppath = Path(profile_path) if profile_path else CATALOG_DIR / "profile.yaml"
            self.profile = Profile.from_dict(yaml.safe_load(ppath.read_text("utf-8")) or {})
        except Exception as exc:  # noqa: BLE001
            log.debug("profile unavailable: %s", exc)

    def lane(self, lane_id: str) -> SimpleNamespace | None:
        return self.lanes.get(lane_id)

    def active_lanes(self) -> list[SimpleNamespace]:
        return [lane for lane in self.lanes.values() if lane.active]

    def classify(self, job: Job, employer: Employer | None = None) -> Job:
        return job

    def title_signal(self, title: str, employer: Employer | None = None) -> float:
        return 0.0

    def search_plan(self, lane_ids=None, source=None, posted_within_days=None) -> list:  # noqa: ANN001
        return []

    def to_dict(self) -> dict:
        return {
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "active": g.active,
                    "lanes": [
                        {"id": x.id, "name": x.name, "active": x.active, "group": g.id, "queries": x.queries}
                        for x in g.lanes
                    ],
                }
                for g in self.groups
            ]
        }


# ---- shared context -------------------------------------------------------------------------------


class BoardContext:
    """Lazily created store / taxonomy / registry shared by the request handlers and the sweep thread."""

    def __init__(self, settings: Settings, store: Any = None, taxonomy: Any = None, registry: Any = None):
        self.settings = settings
        self._store = store
        self.owns_store = store is None
        self._taxonomy = taxonomy
        self._registry = registry
        self._lock = threading.RLock()

    @property
    def store(self) -> Any:
        with self._lock:
            if self._store is None:
                from ..store import Store

                self._store = Store(self.settings.db_path)
            return self._store

    @property
    def taxonomy(self) -> Any:
        with self._lock:
            if self._taxonomy is None:
                try:
                    from ..taxonomy import Taxonomy

                    self._taxonomy = Taxonomy.load(profile=self.settings.profile_path)
                except Exception as exc:  # noqa: BLE001 - keep the board usable without the classifier
                    log.warning("taxonomy unavailable (%s); lanes come from catalog/lanes.yaml", exc)
                    self._taxonomy = CatalogTaxonomy(profile_path=self.settings.profile_path)
            return self._taxonomy

    @property
    def registry(self) -> Any:
        with self._lock:
            if self._registry is None:
                from ..employers import EmployerRegistry

                try:
                    self._registry = EmployerRegistry.load()
                except Exception as exc:  # noqa: BLE001
                    log.warning("employer registry unavailable: %s", exc)
                    self._registry = EmployerRegistry([])
            return self._registry

    def min_score(self) -> float:
        try:
            return float(self.taxonomy.profile.min_score)
        except Exception:  # noqa: BLE001
            return Profile().min_score

    def close(self) -> None:
        if self.owns_store and self._store is not None:
            try:
                self._store.close()
            except Exception:  # noqa: BLE001
                pass


# ---- sweeps ---------------------------------------------------------------------------------------


def _slim_report(report: dict) -> dict:
    """Cap the long lists of a SweepReport dict so status polling stays light."""
    rep = copy.deepcopy(report)
    plan = rep.get("plan")
    if isinstance(plan, dict):
        for key in ("boards", "searches", "skipped_employers"):
            items = plan.get(key)
            if isinstance(items, list) and len(items) > REPORT_LIST_CAP:
                plan[key] = items[:REPORT_LIST_CAP]
                plan[f"{key}_truncated"] = len(items)
    new = rep.get("new_uids")
    if isinstance(new, list):
        rep["new_count"] = len(new)
        rep["new_uids"] = new[:REPORT_LIST_CAP]
    errors = rep.get("errors")
    if isinstance(errors, list) and len(errors) > REPORT_LIST_CAP:
        rep["errors"] = errors[:REPORT_LIST_CAP]
        rep["errors_truncated"] = len(errors)
    return rep


class SweepManager:
    """Runs one sweep at a time in a daemon thread and records its progress events."""

    def __init__(self, ctx: BoardContext):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.running = False
        self.progress: dict[str, Any] = {}
        self.last_report: dict | None = None

    def start(self, options: Any) -> bool:
        """Start a sweep; False when one is already running."""
        with self._lock:
            if self.running:
                return False
            self.running = True
            opts = options.to_dict() if hasattr(options, "to_dict") else dict(vars(options))
            self.progress = {
                "phase": "starting",
                "started_at": _now_iso(),
                "finished_at": None,
                "options": opts,
                "done": 0,
                "total": None,
                "counts": None,
                "ok": 0,
                "errors": 0,
                "jobs": 0,
                "new": 0,
                "recent": [],
            }
        self._thread = threading.Thread(target=self._run, args=(options,), name="jobsweep-sweep", daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the current sweep thread ends (tests, shutdown). True when nothing is running."""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return not self.running

    def on_event(self, event: dict) -> None:
        """``run_sweep`` progress callback: plan / target_done / finished events."""
        if not isinstance(event, dict):
            return
        with self._lock:
            p = self.progress
            kind = event.get("event")
            if kind == "plan":
                p["phase"] = "planned" if event.get("dry_run") else "running"
                p["total"] = event.get("total")
                p["counts"] = event.get("counts")
            elif kind == "target_done":
                p["phase"] = "running"
                p["done"] = event.get("done", p.get("done", 0) + 1)
                if event.get("total") is not None:
                    p["total"] = event.get("total")
                if event.get("ok"):
                    p["ok"] += 1
                else:
                    p["errors"] += 1
                p["jobs"] += int(event.get("jobs") or 0)
                p["new"] += int(event.get("new") or 0)
                item = {
                    k: event.get(k)
                    for k in ("kind", "source", "target", "ok", "error", "jobs", "kept", "new", "elapsed_s")
                }
                item["at"] = _now_iso()
                p["recent"] = (p.get("recent") or [])[-(RECENT_EVENTS - 1) :] + [item]
            elif kind == "finished":
                p["phase"] = "finishing"

    def _run(self, options: Any) -> None:
        report: dict | None = None
        phase = "finished"
        try:
            from .. import sweep as sweep_mod

            ctx = self.ctx
            result = sweep_mod.run_sweep(
                ctx.settings, ctx.taxonomy, ctx.registry, ctx.store, options, progress=self.on_event
            )
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            if hasattr(result, "to_dict"):
                report = result.to_dict()
                try:
                    report["summary"] = result.summary()
                except Exception:  # noqa: BLE001
                    pass
            else:
                report = dict(result or {})
            report = _slim_report(report)
        except Exception as exc:  # noqa: BLE001 - surface the failure in /api/sweep/status
            log.exception("sweep failed")
            phase = "error"
            report = {"error": str(exc) or type(exc).__name__, "finished_at": _now_iso()}
        finally:
            with self._lock:
                self.last_report = report
                self.progress["phase"] = phase
                self.progress["finished_at"] = _now_iso()
                self.running = False

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "last_report": copy.deepcopy(self.last_report),
                "progress": copy.deepcopy(self.progress),
            }


def _sweep_options(data: dict) -> Any:
    from ..sweep import SweepOptions

    if hasattr(SweepOptions, "from_dict"):
        return SweepOptions.from_dict(data)
    names = set(getattr(SweepOptions, "__dataclass_fields__", {}))
    return SweepOptions(**{k: v for k, v in data.items() if k in names})


# ---- import ---------------------------------------------------------------------------------------


def detect_format(payload: Any) -> str:
    """Guess json / csv / md for pasted text (mirrors ``jobsweep import``)."""
    if not isinstance(payload, str):
        return "json"
    head = payload.lstrip()[:2000]
    if head.startswith(("[", "{")):
        return "json"
    if "**" in head or head.startswith("#"):
        return "md"
    first = head.splitlines()[0] if head else ""
    return "csv" if first.count(",") >= 1 else "md"


def _parse_import(payload: Any, origin: str, fmt: str | None, settings: Settings) -> list[Job]:
    """Call ``ImportSource.parse`` whatever its exact shape (static/class/instance, fmt kwarg, async)."""
    from ..sources.base import SourceContext
    from ..sources.manual_import import ImportSource

    raw_attr = inspect.getattr_static(ImportSource, "parse")
    if isinstance(raw_attr, (staticmethod, classmethod)):
        parse: Callable[..., Any] = ImportSource.parse
    else:
        parse = ImportSource(SourceContext(client=None, settings=settings)).parse  # type: ignore[arg-type]
    params = inspect.signature(parse).parameters
    kwargs: dict[str, Any] = {}
    for name in ("fmt", "format"):
        if name in params and fmt:
            kwargs[name] = fmt
    result = (
        parse(payload, origin=origin, **kwargs) if "origin" in params else parse(payload, origin, **kwargs)
    )
    if inspect.isawaitable(result):
        result = asyncio.run(result)  # handlers run in the threadpool, so no loop is running here
    return [j if isinstance(j, Job) else Job.from_dict(j) for j in (result or [])]


def _norm_company(name: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return " ".join(re.sub(r"\b(inc|llc|ltd|plc|corp|corporation|co|company|group|the)\b", "", s).split())


def _employer_lookup(registry: Any) -> Callable[[Job], Employer | None]:
    by_name: dict[str, Employer] = {}
    try:
        for e in registry.all():
            by_name.setdefault(_norm_company(e.name), e)
    except Exception:  # noqa: BLE001
        pass

    def find(job: Job) -> Employer | None:
        if job.employer_key:
            try:
                emp = registry.get(job.employer_key)
            except Exception:  # noqa: BLE001
                emp = None
            if emp is not None:
                return emp
        return by_name.get(_norm_company(job.company)) if job.company else None

    return find


# ---- request helpers ------------------------------------------------------------------------------


def job_filters(
    q: str | None = Query(None, description="full-text search"),
    lane: Annotated[list[str] | None, Query(description="lane id(s); repeat or comma-separate")] = None,
    group: Annotated[list[str] | None, Query(description="group id(s)")] = None,
    source: Annotated[list[str] | None, Query(description="source name(s)")] = None,
    status: Annotated[list[str] | None, Query(description="status(es) or 'all'")] = None,
    employer: Annotated[list[str] | None, Query(description="employer key(s)")] = None,
    entry: str | None = Query(None, description="1 = entry-level (or unknown) only"),
    min_score: float | None = Query(None, ge=0, le=100),
    location: str | None = Query(None),
    remote: str | None = Query(None, description="1 = remote only, 0 = not remote"),
    since: str | None = Query(None, description="ISO date/datetime on first_seen"),
    days: float | None = Query(None, ge=0, description="first seen within N days"),
    include_inactive_lanes: str | None = Query(None),
    include_duplicates: str | None = Query(None),
    active: str | None = Query(None, description="0 = include inactive (closed/expired) jobs"),
) -> dict[str, Any]:
    """Query string -> ``Store.query`` keyword filters (only the ones given)."""
    f: dict[str, Any] = {}
    if q and q.strip():
        f["q"] = q.strip()
    for key, values in (("lanes", lane), ("groups", group), ("sources", source), ("employers", employer)):
        items = _split(values)
        if items:
            f[key] = items
    statuses = _split(status)
    if statuses:
        bad = [s for s in statuses if s != "all" and s not in JOB_STATUSES]
        if bad:
            raise HTTPException(
                422, f"unknown status {bad[0]!r}; expected one of {', '.join(JOB_STATUSES)} or 'all'"
            )
        f["statuses"] = statuses
    if _flag(entry):
        f["entry_only"] = True
    if min_score is not None:
        f["min_score"] = float(min_score)
    if location and location.strip():
        f["location"] = location.strip()
    rem = _flag(remote)
    if rem is not None:
        f["remote"] = rem
    if since and since.strip():
        f["since"] = since.strip()
    elif days:
        f["since"] = float(days)
    if _flag(include_inactive_lanes):
        f["include_inactive_lanes"] = True
    if _flag(include_duplicates):
        f["include_duplicates"] = True
    if _flag(active) is False:
        f["active_only"] = False
    return f


def _compact(row: dict) -> dict:
    desc = row.get("description") or ""
    if len(desc) > COMPACT_DESCRIPTION:
        row = dict(row)
        row["description"] = desc[:COMPACT_DESCRIPTION].rsplit(" ", 1)[0] + " …"
        row["description_truncated"] = True
    return row


def _source_rows(settings: Settings) -> list[dict]:
    try:
        from ..export import source_rows

        return source_rows(settings)
    except ImportError:
        pass
    from .. import sources as sources_mod

    out = []
    for name in sources_mod.SOURCE_PATHS:
        cls = sources_mod.get_source_class(name)
        if cls is None:
            out.append({"name": name, "label": name, "kind": None, "available": False,
                        "reason": "adapter missing or failed to import", "metered": False, "docs_url": ""})  # fmt: skip
            continue
        try:
            ok, reason = cls.availability(settings)
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"availability check failed: {exc}"
        out.append({"name": name, "label": cls.label or name, "kind": cls.kind, "available": bool(ok),
                    "reason": reason, "metered": bool(cls.metered), "docs_url": cls.docs_url})  # fmt: skip
    return out


def _lane_payload(ctx: BoardContext, filters: dict[str, Any]) -> dict:
    """taxonomy.to_dict() with ``count`` on every group and lane.

    Without filters the counts are the store's active totals (``stats()``); with filters each lane/group
    is counted under those filters (lane/group filters themselves are ignored), so the sidebar matches
    the current view.
    """
    store = ctx.store
    data = copy.deepcopy(ctx.taxonomy.to_dict())
    stats = store.stats()
    by_lane: dict[str, int] = stats.get("by_lane") or {}
    by_group: dict[str, int] = stats.get("by_group") or {}
    base = {k: v for k, v in filters.items() if k not in ("lanes", "groups")}
    if base:
        lane_n = {lid: store.count(lanes=[lid], **base) for lid, n in by_lane.items() if n}
        group_n = {gid: store.count(groups=[gid], **base) for gid, n in by_group.items() if n}
        data["total"] = store.count(**base)
    else:
        lane_n, group_n = by_lane, by_group
        data["total"] = stats.get("active", 0)
    for g in data.get("groups", []):
        g["count"] = int(group_n.get(g.get("id"), 0))
        for lane in g.get("lanes", []):
            lane["count"] = int(lane_n.get(lane.get("id"), 0))
    data["min_score"] = ctx.min_score()
    data["statuses"] = list(JOB_STATUSES)
    return data


def _employer_matches(e: dict, group: str | None, lane: str | None, q: str | None) -> bool:
    if group and group not in (e.get("groups") or []):
        return False
    if lane and lane not in (e.get("lanes") or []):
        return False
    if q:
        hay = " ".join(
            str(e.get(k) or "") for k in ("key", "name", "hq", "ats", "ats_vendor", "notes")
        ).lower()
        return all(tok in hay for tok in q.lower().split())
    return True


# ---- app ------------------------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None, store: Any = None, taxonomy: Any = None, registry: Any = None
) -> FastAPI:
    """Build the board app. Omitted dependencies are created lazily from ``settings``."""
    settings = settings or Settings.load()
    ctx = BoardContext(settings, store=store, taxonomy=taxonomy, registry=registry)
    sweeps = SweepManager(ctx)
    static_dir = Path(STATIC_DIR)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        yield
        sweeps.wait(timeout=0)
        ctx.close()

    app = FastAPI(
        title="jobsweep",
        version="1",
        description="Lane-based job board API (ARCHITECTURE.md §9).",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.state.sweeps = sweeps

    @app.middleware("http")
    async def guard_and_cache(request: Request, call_next):  # noqa: ANN202
        # Same-origin only for writes: a page on another site must not drive the local API.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # ---- UI ---------------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> Response:
        page = static_dir / "index.html"
        if not page.exists():
            return PlainTextResponse("jobsweep UI files are missing (web/static/index.html)", status_code=500)
        return FileResponse(page, media_type="text/html")

    @app.get("/config.js", include_in_schema=False)
    def config_js() -> Response:
        body = (
            'window.JOBSWEEP_MODE = "api";\n'
            + "window.JOBSWEEP_DEFAULTS = "
            + json.dumps({"min_score": ctx.min_score()})
            + ";\n"
        )
        return Response(body, media_type="application/javascript")

    # ---- jobs -------------------------------------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs(
        filters: Annotated[dict, Depends(job_filters)],
        sort: str = Query("score"),
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        compact: str | None = Query(None, description="1 = shorten descriptions"),
    ) -> dict:
        if sort not in SORTS:
            raise HTTPException(422, f"unknown sort {sort!r}; expected one of {', '.join(SORTS)}")
        try:
            items = ctx.store.query(sort=sort, limit=limit, offset=offset, **filters)
            total = ctx.store.count(**filters)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if _flag(compact):
            items = [_compact(r) for r in items]
        return {"total": total, "items": items}

    @app.post("/api/jobs/{uid:path}/status")
    def set_status(uid: str, body: dict) -> dict:
        status = body.get("status") if isinstance(body, dict) else None
        if not isinstance(status, str) or status not in JOB_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(JOB_STATUSES)}")
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(422, "note must be a string")
        try:
            row = ctx.store.set_status(uid, status, note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if row is None:
            raise HTTPException(404, f"no job {uid!r}")
        return row

    @app.get("/api/jobs/{uid:path}")
    def get_job(uid: str) -> dict:
        row = ctx.store.get(uid)
        if row is None:
            raise HTTPException(404, f"no job {uid!r}")
        return row

    # ---- catalog / stats --------------------------------------------------------------------------
    @app.get("/api/lanes")
    def lanes(filters: Annotated[dict, Depends(job_filters)]) -> dict:
        try:
            return _lane_payload(ctx, filters)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/stats")
    def stats() -> dict:
        return ctx.store.stats()

    @app.get("/api/runs")
    def runs(limit: int = Query(20, ge=1, le=500)) -> list:
        return ctx.store.runs(limit)

    @app.get("/api/sources")
    def sources() -> list:
        return _source_rows(ctx.settings)

    @app.get("/api/employers")
    def employers(
        group: str | None = None,
        lane: str | None = None,
        q: str | None = None,
        manual_only: str | None = None,
    ) -> dict:
        reg = ctx.registry

        def rows(items: list) -> list[dict]:
            out = [e.to_dict() for e in items]
            out = [e for e in out if _employer_matches(e, group, lane, q)]
            return sorted(out, key=lambda e: str(e.get("name") or "").lower())

        manual = rows(reg.manual())
        sweepable = [] if _flag(manual_only) else rows(reg.sweepable())
        return {"sweepable": sweepable, "manual": manual}

    # ---- sweeps -----------------------------------------------------------------------------------
    @app.post("/api/sweep")
    def start_sweep(body: dict | None = None) -> JSONResponse:
        if sweeps.running:
            raise HTTPException(409, "a sweep is already running")
        if isinstance(ctx.taxonomy, CatalogTaxonomy) and not (body or {}).get("dry_run"):
            raise HTTPException(503, "taxonomy module unavailable: cannot classify a sweep")
        try:
            options = _sweep_options(body or {})
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"bad sweep options: {exc}") from exc
        except ImportError as exc:
            raise HTTPException(503, f"sweep engine unavailable: {exc}") from exc
        if not sweeps.start(options):
            raise HTTPException(409, "a sweep is already running")
        return JSONResponse({"started": True}, status_code=202)

    @app.get("/api/sweep/status")
    def sweep_status() -> dict:
        return sweeps.status()

    # ---- import / digest --------------------------------------------------------------------------
    @app.post("/api/import")
    def import_jobs(body: dict) -> dict:
        payload = body.get("payload")
        if payload is None or (isinstance(payload, str) and not payload.strip()) or payload in ([], {}):
            raise HTTPException(422, "payload is empty")
        if not isinstance(payload, (str, list, dict)):
            raise HTTPException(422, "payload must be text, a list of jobs or a JSON object")
        origin = str(body.get("origin") or "web-import").strip() or "web-import"
        origin = re.sub(r"[^A-Za-z0-9_.-]+", "-", origin)[:60]
        fmt = str(body.get("format") or "auto").lower()
        if fmt == "markdown":
            fmt = "md"
        if fmt not in IMPORT_FORMATS:
            raise HTTPException(422, f"format must be one of {', '.join(IMPORT_FORMATS)}")
        if fmt == "auto":
            fmt = detect_format(payload)
        try:
            jobs = _parse_import(payload, origin, fmt, ctx.settings)
        except ImportError as exc:
            raise HTTPException(503, f"import parser unavailable: {exc}") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, f"could not parse payload as {fmt}: {exc}") from exc
        if not jobs:
            return {"imported": 0, "new": 0, "new_uids": [], "format": fmt, "origin": origin}
        tax, find = ctx.taxonomy, _employer_lookup(ctx.registry)
        for i, job in enumerate(jobs):
            emp = find(job)
            if emp is not None and not job.employer_key:
                job.employer_key = emp.key
            try:
                res = tax.classify(job, emp)
            except Exception as exc:  # noqa: BLE001 - store unclassified rather than lose the import
                log.warning("classify failed for %s: %s", job.uid, exc)
                res = job
            if isinstance(res, Job):
                jobs[i] = res
        result = ctx.store.upsert_jobs(jobs)
        new = list(result.get("new") or [])
        return {"imported": len(jobs), "new": len(new), "new_uids": new, "format": fmt, "origin": origin}

    @app.get("/api/digest")
    def digest(
        days: float = Query(1, gt=0),
        format: str = Query("md", pattern="^(md|markdown|json)$"),  # public query name
        min_score: float | None = Query(None, ge=0, le=100),
    ) -> Response:
        from ..export import digest as make_digest

        span: float | int = int(days) if float(days).is_integer() else days
        text = make_digest(ctx.store, days=span, min_score=min_score, fmt=format, taxonomy=ctx.taxonomy)
        if format == "json":
            return JSONResponse(json.loads(text))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/api/{rest:path}", include_in_schema=False)
    def api_not_found(rest: str) -> None:
        raise HTTPException(404, f"no API endpoint /api/{rest}")

    # Everything else (app.js, styles.css, icons) comes straight from web/static.
    app.mount("/", StaticFiles(directory=static_dir, check_dir=False), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    """``python -m jobsweep.web.app [--host 127.0.0.1] [--port 8765]``."""
    import uvicorn

    ap = argparse.ArgumentParser(prog="jobsweep-web", description="Run the jobsweep web board.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
