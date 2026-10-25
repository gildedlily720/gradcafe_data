"""Exports: static GitHub Pages board, markdown/JSON digest, CSV."""

from __future__ import annotations

import copy
import csv
import io
import json
import logging
import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from .config import STATIC_DIR, Settings
from .models import JOB_STATUSES, Profile
from .text import truncate

if TYPE_CHECKING:
    from .employers import EmployerRegistry
    from .store import Store

log = logging.getLogger("jobsweep.export")

#: description length kept in the static site's jobs.json
SITE_DESCRIPTION_CHARS = 1500

CSV_COLUMNS = [
    "uid", "title", "company", "location", "remote", "score", "entry_level", "lanes", "groups", "status",
    "note", "posted_at", "closes_at", "first_seen", "last_seen", "employment_type", "grade", "salary_min",
    "salary_max", "salary_currency", "salary_period", "source", "url",
]  # fmt: skip

_FALLBACK_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>jobsweep</title></head>
<body><h1>jobsweep</h1><p>The board UI (web/static) was not available when this site was exported.
The data is in <a href="data/jobs.json">data/jobs.json</a>.</p></body></html>
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_min_score(taxonomy: Any) -> float:
    try:
        return float(taxonomy.profile.min_score)
    except Exception:  # noqa: BLE001
        return Profile().min_score


def source_rows(settings: Settings | None = None) -> list[dict]:
    """``[{"name","label","kind","available","reason","metered","docs_url"}]`` for every registered source."""
    from . import sources as sources_mod

    settings = settings or Settings.load()
    out = []
    for name in sources_mod.SOURCE_PATHS:
        cls = sources_mod.get_source_class(name)
        if cls is None:
            out.append(
                {"name": name, "label": name, "kind": None, "available": False,
                 "reason": "adapter missing or failed to import", "metered": False, "docs_url": ""}
            )  # fmt: skip
            continue
        try:
            ok, reason = cls.availability(settings)
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"availability check failed: {exc}"
        out.append(
            {
                "name": name,
                "label": cls.label or name,
                "kind": cls.kind,
                "available": bool(ok),
                "reason": reason,
                "metered": bool(cls.metered),
                "docs_url": cls.docs_url,
            }
        )
    return out


def site_rows(store: Store, *, min_score: float, days: int | None = None) -> list[dict]:
    """Rows for the static board: active non-duplicates >= min_score (+ every triaged job), slimmed."""
    since = days if days else None
    rows = store.query(min_score=min_score, since=since, include_inactive_lanes=True, limit=None)
    by_uid = {r["uid"]: r for r in rows}
    triaged = [s for s in JOB_STATUSES if s != "new"]
    for r in store.query(statuses=triaged, active_only=False, include_inactive_lanes=True, limit=None):
        by_uid.setdefault(r["uid"], r)
    out = []
    for r in by_uid.values():
        r = dict(r)
        r["description"] = truncate(r.get("description") or "", SITE_DESCRIPTION_CHARS)
        out.append(r)
    out.sort(key=lambda r: (-(r.get("score") or 0), r.get("uid") or ""))
    return out


def _write_json(path: Path, data: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    path.write_text(text + "\n", encoding="utf-8")


def _copy_static(static_dir: Path, out: Path) -> int:
    copied = 0
    if not static_dir.is_dir():
        return 0
    for src in sorted(static_dir.rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(static_dir)
        if rel.parts and rel.parts[0] == "data":  # never let a dev data/ dir shadow the export
            continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied


def lanes_with_counts(taxonomy: Any, rows: Iterable[dict]) -> dict:
    """``taxonomy.to_dict()`` with ``count`` on each group and lane, counted over ``rows``."""
    lane_n: dict[str, int] = {}
    group_n: dict[str, int] = {}
    for r in rows:
        for lane in set(r.get("lanes") or []):
            lane_n[lane] = lane_n.get(lane, 0) + 1
        for g in set(r.get("groups") or []):
            group_n[g] = group_n.get(g, 0) + 1
    data = copy.deepcopy(taxonomy.to_dict())
    for g in data.get("groups", []):
        g["count"] = group_n.get(g.get("id"), 0)
        for lane in g.get("lanes", []):
            lane["count"] = lane_n.get(lane.get("id"), 0)
    return data


def export_site(
    store: Store,
    taxonomy: Any,
    registry: EmployerRegistry,
    out_dir: str | Path,
    *,
    min_score: float | None = None,
    days: int | None = None,
    static_dir: str | Path | None = None,
    settings: Settings | None = None,
) -> Path:
    """Write the static board (UI from ``web/static`` + ``config.js`` + ``data/*.json``) to ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    static = Path(static_dir) if static_dir else STATIC_DIR
    if not _copy_static(static, out):
        log.warning("no static UI files found in %s; writing a placeholder index.html", static)
    if not (out / "index.html").exists():
        (out / "index.html").write_text(_FALLBACK_INDEX, encoding="utf-8")
    (out / "config.js").write_text('window.JOBSWEEP_MODE = "static";\n', encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    threshold = _default_min_score(taxonomy) if min_score is None else float(min_score)
    generated = _now_iso()
    rows = site_rows(store, min_score=threshold, days=days)
    _write_json(
        out / "data" / "jobs.json",
        {"generated_at": generated, "total": len(rows), "min_score": threshold, "days": days, "items": rows},
        compact=True,
    )
    lanes = lanes_with_counts(taxonomy, rows)
    lanes["generated_at"] = generated
    _write_json(out / "data" / "lanes.json", lanes)
    stats = store.stats()
    stats["generated_at"] = generated
    stats["exported"] = len(rows)
    _write_json(out / "data" / "stats.json", stats)
    _write_json(
        out / "data" / "employers.json",
        {
            "generated_at": generated,
            "sweepable": [e.to_dict() for e in registry.sweepable()],
            "manual": [e.to_dict() for e in registry.manual()],
        },
    )
    _write_json(out / "data" / "sources.json", source_rows(settings))
    return out


# ---- digest ---------------------------------------------------------------------------------------


_AUTO: Any = object()


def _try_load_taxonomy() -> Any:
    try:
        from .taxonomy import Taxonomy

        return Taxonomy.load(profile=Settings.load().profile_path)
    except Exception as exc:  # noqa: BLE001 - names are cosmetic; fall back to ids
        log.debug("taxonomy unavailable for digest: %s", exc)
        return None


def _names(taxonomy: Any) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(group id -> name, lane id -> name, group order) from the taxonomy when available."""
    group_names: dict[str, str] = {}
    lane_names: dict[str, str] = {}
    order: list[str] = []
    for g in getattr(taxonomy, "groups", None) or []:
        gid = getattr(g, "id", None)
        if not gid:
            continue
        order.append(gid)
        group_names[gid] = getattr(g, "name", gid) or gid
        for lane in getattr(g, "lanes", None) or []:
            lid = getattr(lane, "id", None)
            if lid:
                lane_names[lid] = getattr(lane, "name", lid) or lid
    return group_names, lane_names, order


def _entry_rank(row: dict) -> int:
    return {True: 0, None: 1, False: 2}.get(row.get("entry_level"), 1)


def _md_escape(text: str) -> str:
    return (text or "").replace("[", "(").replace("]", ")").replace("\n", " ").strip()


def _digest_line(r: dict) -> str:
    title = _md_escape(r.get("title") or "(untitled)")
    head = f"- [{title}]({r.get('url')})" if r.get("url") else f"- {title}"
    parts = [r.get("company") or "", r.get("location") or ""]
    parts.append(f"score {round(float(r.get('score') or 0))}")
    if r.get("entry_level") is True:
        parts.append("entry-level")
    if r.get("posted_at"):
        parts.append(f"posted {str(r['posted_at'])[:10]}")
    if r.get("closes_at"):
        parts.append(f"closes {str(r['closes_at'])[:10]}")
    return head + " — " + " · ".join(_md_escape(p) for p in parts if p)


def _compact(r: dict) -> dict:
    keys = (
        "uid", "title", "company", "location", "url", "score", "entry_level", "posted_at", "closes_at",
        "lanes", "groups", "source", "first_seen", "status", "match_reasons",
    )  # fmt: skip
    return {k: r.get(k) for k in keys}


def digest(
    store: Store,
    days: float = 1,
    min_score: float | None = None,
    fmt: str = "md",
    *,
    taxonomy: Any = _AUTO,
    limit_per_lane: int | None = None,
) -> str:
    """New active matches (first seen within ``days``) grouped by best group -> best lane.

    Entry-level jobs come first in each lane. ``fmt`` is "md" (markdown) or "json". ``taxonomy`` supplies
    group/lane names, their order and the default ``min_score`` (loaded lazily when omitted; pass
    ``None`` to use plain ids).
    """
    if fmt not in ("md", "markdown", "json"):
        raise ValueError(f"unknown digest format {fmt!r} (expected md or json)")
    if taxonomy is _AUTO:
        taxonomy = _try_load_taxonomy()
    threshold = _default_min_score(taxonomy) if min_score is None else float(min_score)
    rows = store.query(since=days, min_score=threshold, limit=None)
    group_names, lane_names, order = _names(taxonomy)
    tree: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        g = (r.get("groups") or ["unclassified"])[0]
        lane = (r.get("lanes") or [""])[0]
        tree.setdefault(g, {}).setdefault(lane, []).append(r)
    for lanes in tree.values():
        for items in lanes.values():
            items.sort(key=lambda r: (_entry_rank(r), -(r.get("score") or 0), r.get("title") or ""))

    def group_key(g: str) -> tuple:
        return (order.index(g) if g in order else len(order), -sum(len(v) for v in tree[g].values()), g)

    groups_sorted = sorted(tree, key=group_key)
    entry = sum(1 for r in rows if r.get("entry_level") is True)
    generated = _now_iso()
    span = f"{days:g} day" + ("" if float(days) == 1 else "s")

    if fmt == "json":
        payload = {
            "generated_at": generated,
            "days": days,
            "min_score": threshold,
            "total": len(rows),
            "entry_level": entry,
            "groups": [
                {
                    "id": g,
                    "name": group_names.get(g, g),
                    "count": sum(len(v) for v in tree[g].values()),
                    "lanes": [
                        {
                            "id": lane,
                            "name": lane_names.get(lane, lane or "other"),
                            "count": len(items),
                            "jobs": [_compact(r) for r in items[:limit_per_lane]],
                        }
                        for lane, items in sorted(tree[g].items(), key=lambda kv: (-len(kv[1]), kv[0]))
                    ],
                }
                for g in groups_sorted
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    lines = [f"# jobsweep digest — {generated[:10]}", ""]
    if not rows:
        lines.append(f"No new matches in the last {span} (min score {threshold:g}).")
        return "\n".join(lines) + "\n"
    lines.append(
        f"**{len(rows)}** new matches in the last {span} (**{entry}** entry-level) across "
        f"{len(tree)} groups · min score {threshold:g}"
    )
    try:
        st = store.stats()
        lines.append(f"Board: {st['active']} active jobs, {st['new_7d']} new this week.")
    except Exception:  # noqa: BLE001
        pass
    for g in groups_sorted:
        lanes = tree[g]
        lines += ["", f"## {group_names.get(g, g)} ({sum(len(v) for v in lanes.values())})"]
        for lane, items in sorted(lanes.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines += ["", f"### {lane_names.get(lane, lane or 'Other')} ({len(items)})"]
            shown = items[:limit_per_lane] if limit_per_lane else items
            lines += [_digest_line(r) for r in shown]
            if len(shown) < len(items):
                lines.append(f"- … and {len(items) - len(shown)} more")
    return "\n".join(lines) + "\n"


# ---- CSV ------------------------------------------------------------------------------------------


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if v is None:
        return ""
    return v


def write_csv(rows: Iterable[dict], path: str | Path | TextIO) -> str | Path | TextIO:
    """Write row dicts to CSV (``CSV_COLUMNS``). ``path`` may be a file path, "-" (stdout) or a text stream."""
    import sys

    def emit(fh: TextIO) -> None:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _csv_value(r.get(k)) for k in CSV_COLUMNS})

    if isinstance(path, io.TextIOBase) or hasattr(path, "write"):
        emit(path)  # type: ignore[arg-type]
        return path
    if str(path) == "-":
        emit(sys.stdout)
        return path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        emit(fh)
    return p
