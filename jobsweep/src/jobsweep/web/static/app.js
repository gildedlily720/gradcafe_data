/* jobsweep board UI: vanilla JS (ES2020), no build step, no external dependencies.
 *
 * Two modes, picked by config.js (window.JOBSWEEP_MODE):
 *   "api"    - served by `jobsweep serve` (FastAPI): data from /api/*, statuses saved server-side.
 *   "static" - GitHub Pages snapshot: data/*.json, filtering client-side, statuses + notes in localStorage.
 * Without config.js the mode is auto-detected (api if /api/stats answers).
 *
 * Safety: job data is only ever inserted with textContent / setAttribute (never innerHTML) and links
 * from data are limited to http(s)/mailto.
 */
(() => {
  "use strict";

  // ---- constants ---------------------------------------------------------------------------------
  const PAGE = 100;
  const DAY = 86400000;
  const TRIAGE_KEY = "jobsweep:triage:v1";
  const THEME_KEY = "jobsweep:theme";
  const EXPANDED_KEY = "jobsweep:expanded:v1";
  const WIDE_QUERY = "(min-width: 1280px)";

  const STATUSES = ["new", "saved", "applied", "interviewing", "offer", "rejected", "dismissed"];
  const STATUS_LABEL = {
    new: "New", saved: "Saved", applied: "Applied", interviewing: "Interviewing", offer: "Offer",
    rejected: "Rejected", dismissed: "Dismissed",
  };
  const TABS = [
    { id: "inbox", label: "Inbox", statuses: ["new"] },
    { id: "saved", label: "Saved", statuses: ["saved"] },
    { id: "applied", label: "Applied", statuses: ["applied"] },
    { id: "interviewing", label: "Interviewing", statuses: ["interviewing", "offer"] },
    { id: "dismissed", label: "Dismissed", statuses: ["dismissed", "rejected"] },
    { id: "all", label: "All", statuses: [] },
  ];
  // Triage tabs list everything the user filed there: score / entry-level / recency filters do not apply.
  const TRIAGE_TABS = new Set(["saved", "applied", "interviewing", "dismissed"]);
  const SORTS = ["score", "newest", "posted", "closing", "company"];
  const DAYS = ["1", "3", "7", "30"];
  const VIEWS = [
    { id: "jobs", label: "Jobs" },
    { id: "employers", label: "Employers" },
    { id: "sources", label: "Sources" },
    { id: "runs", label: "Runs" },
    { id: "import", label: "Import", apiOnly: true },
  ];
  const KNOWN_GROUPS = new Set([
    "specialty-insurance", "intelligence-security", "investigations-dd", "geopolitical-risk", "sanctions-fincrime",
    "trust-safety", "policy-research", "strategic-comms", "government", "entry-routes", "earlier-lanes",
  ]);
  const TAG_LABEL = {
    pathways: "Pathways", "recent-graduates": "Recent grads", fellowship: "Fellowship",
    "graduate-program": "Graduate programme", internship: "Internship", student: "Student", reservist: "Reservist",
  };
  const SHORTCUTS = [
    [["j"], "Next job"], [["k"], "Previous job"], [["o"], "Open posting"], [["Enter"], "Open posting"],
    [["s"], "Save / unsave"], [["a"], "Mark applied"], [["d"], "Dismiss"], [["u"], "Undo status change"],
    [["n"], "Edit note"], [["/"], "Search"], [["Esc"], "Close panel"], [["?"], "This help"],
  ];
  // Claude-side connectors (shown on the Sources page). Plain text only.
  const CLAUDE_CONNECTORS = [
    {
      name: "Indeed",
      kind: "claude.ai connector",
      text: "Search Indeed from a Claude conversation. Paste the markdown it returns into Import (origin indeed-connector), or ask Claude to pass it to the jobsweep import_jobs tool.",
      how: "Indeed search_jobs -> jobsweep import_jobs(text=..., origin=\"indeed-connector\")",
    },
    {
      name: "USAJOBS via Pipeworx",
      kind: "remote MCP server",
      text: "Federal vacancies (Pathways, recent graduates, GS-5 to GS-9) with no API key, about 50 calls a day. The usajobs source falls back to the same gateway when USAJOBS_API_KEY is not set.",
      how: "gateway.pipeworx.io  tool: usajobs_search",
    },
    {
      name: "Workopia",
      kind: "claude.ai connector",
      text: "AI job search with salary and experience tags. Import the job_tool JSON result here to classify it against your lanes.",
      how: "Workopia job_tool -> Import (origin workopia)",
    },
    {
      name: "Jobs and Careers",
      kind: "claude.ai connector",
      text: "Location-aware search that returns top matches with salaries and highlights; import the JSON result.",
      how: "job-search -> Import (origin jobs-and-careers)",
    },
    {
      name: "jobsweep MCP server",
      kind: "local stdio MCP",
      text: "Lets Claude Code and Claude Desktop search this board, triage jobs, run sweeps and import connector results. Configured in the repository's .mcp.json.",
      how: "jobsweep-mcp  tools: search_jobs, get_job, set_job_status, run_sweep, live_search, import_jobs, digest",
    },
    {
      name: "TheirStack, HasData, Apify",
      kind: "metered APIs",
      text: "Credit-based aggregators. Put the key in .env, then tick Include metered when you run a sweep.",
      how: "THEIRSTACK_API_KEY  HASDATA_API_KEY  APIFY_TOKEN",
    },
  ];

  // ---- small utilities ---------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const numFmt = new Intl.NumberFormat("en-US");

  function safeUrl(u) {
    if (typeof u !== "string") return null;
    const s = u.trim();
    if (s.startsWith("#")) return s;
    if (/^(https?:|mailto:)/i.test(s)) return s;
    if (/^www\./i.test(s)) return "https://" + s;
    return null;
  }

  function append(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k === null || k === undefined || k === false || k === "") continue;
      el.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return el;
  }

  /** Tiny DOM builder. Strings become text nodes; attributes go through setAttribute. */
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = String(v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k === "style") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "href") {
          const url = safeUrl(v);
          if (url) el.setAttribute("href", url);
        } else if (k === "value") el.value = v;
        else if (k === "checked" || k === "selected" || k === "disabled") el[k] = Boolean(v);
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    return append(el, kids);
  }

  function icon(name, cls) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVGNS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }

  const sep = () => h("span", { class: "sep", "aria-hidden": "true", text: "·" });
  const fmtNum = (n) => numFmt.format(Number(n) || 0);
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const list = (s) => (s ? String(s).split(",").map((x) => x.trim()).filter(Boolean) : []);
  const shortName = (name) => String(name || "").replace(/\s*\([^)]*\)\s*/g, " ").trim();

  function debounce(fn, ms) {
    let t = null;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  const ls = {
    get(key, def) {
      try {
        const v = localStorage.getItem(key);
        return v === null ? def : JSON.parse(v);
      } catch (e) {
        return def;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
        return true;
      } catch (e) {
        return false;
      }
    },
    raw(key, value) {
      try {
        if (value === undefined) return localStorage.getItem(key);
        if (value === null) localStorage.removeItem(key);
        else localStorage.setItem(key, value);
      } catch (e) {
        return null;
      }
      return null;
    },
  };

  // ---- dates, money --------------------------------------------------------------------------------
  function parseDate(s) {
    if (!s) return null;
    const str = String(s);
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str);
    const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(str);
    return isNaN(d.getTime()) ? null : d;
  }
  const isDateOnly = (s) => /^\d{4}-\d{2}-\d{2}$/.test(String(s || ""));
  const startOfToday = () => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  };

  function fmtDate(s, withTime) {
    const d = s instanceof Date ? s : parseDate(s);
    if (!d) return "";
    const opts = { month: "short", day: "numeric" };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
    if (withTime && !isDateOnly(s)) {
      opts.hour = "numeric";
      opts.minute = "2-digit";
    }
    return d.toLocaleString("en-US", opts);
  }

  /** "3h ago", "5d ago", "in 2d", or a short date beyond ~6 weeks. */
  function relTime(s, ref) {
    const d = parseDate(s);
    if (!d) return "";
    if (isDateOnly(s)) {
      const days = Math.round((startOfToday() - d.getTime()) / DAY);
      if (days === 0) return "today";
      if (Math.abs(days) > 45) return fmtDate(s);
      return days > 0 ? days + "d ago" : "in " + -days + "d";
    }
    const diff = (ref || Date.now()) - d.getTime();
    const abs = Math.abs(diff);
    let out;
    if (abs < 3600000) out = Math.max(1, Math.round(abs / 60000)) + "m";
    else if (abs < DAY) out = Math.round(abs / 3600000) + "h";
    else if (abs < 45 * DAY) out = Math.round(abs / DAY) + "d";
    else return fmtDate(s);
    return diff >= 0 ? out + " ago" : "in " + out;
  }

  function closingInfo(s) {
    const d = parseDate(s);
    if (!d) return null;
    const end = isDateOnly(s) ? d.getTime() : new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((end - startOfToday()) / DAY);
    let label;
    if (days < 0) label = "closed";
    else if (days === 0) label = "closes today";
    else if (days === 1) label = "closes tomorrow";
    else if (days <= 45) label = "closes in " + days + "d";
    else label = "closes " + fmtDate(s);
    return { days, label, soon: days >= 0 && days <= 7, past: days < 0 };
  }

  function fmtMoney(v, cur) {
    const big = Math.abs(v) >= 10000;
    try {
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: cur || "USD",
        notation: big ? "compact" : "standard",
        maximumFractionDigits: big ? 1 : v % 1 ? 2 : 0,
        minimumFractionDigits: 0,
      }).format(v);
    } catch (e) {
      return (cur || "") + " " + Math.round(v);
    }
  }

  function fmtSalary(j) {
    const lo = j.salary_min, hi = j.salary_max;
    if (lo == null && hi == null) return "";
    const per = { year: "/yr", month: "/mo", week: "/wk", day: "/day", hour: "/hr" }[j.salary_period] || "";
    const cur = j.salary_currency || "USD";
    let s;
    if (lo != null && hi != null && Number(lo) !== Number(hi)) s = fmtMoney(+lo, cur) + "–" + fmtMoney(+hi, cur);
    else s = (lo == null ? "up to " : "") + fmtMoney(+(lo != null ? lo : hi), cur);
    return s + per;
  }

  function hashHue(str) {
    let x = 0;
    for (const c of String(str)) x = (x * 31 + c.charCodeAt(0)) % 360;
    return x;
  }

  // ---- state ---------------------------------------------------------------------------------------
  const state = {
    mode: null,
    view: "jobs",
    f: null,
    defaultMin: Number((window.JOBSWEEP_DEFAULTS || {}).min_score != null ? window.JOBSWEEP_DEFAULTS.min_score : 25),
    items: [],
    total: 0,
    loading: false,
    reqId: 0,
    sel: -1,
    selUid: null,
    detailItem: null,
    pendingJob: null,
    lanes: { groups: [] },
    laneIndex: {},
    groupIndex: {},
    inactiveLanes: new Set(),
    inactiveGroups: new Set(),
    laneTotal: 0,
    stats: null,
    generatedAt: null,
    expanded: ls.get(EXPANDED_KEY, {}) || {},
    showEmpty: {},
    undo: [],
    full: new Map(),
    cache: {},
    sweep: { running: false, timer: null, status: null },
    jobsHash: "#/jobs",
  };

  function defaultFilters() {
    return {
      q: "", tab: "inbox", lanes: [], groups: [], sources: [], employers: [], entry: true,
      min: state.defaultMin, loc: "", remote: false, days: "", sort: "score",
    };
  }
  state.f = defaultFilters();

  const tabOf = (id) => TABS.find((t) => t.id === id) || TABS[0];
  const tabStatuses = (id) => tabOf(id).statuses;
  const tabMatches = (id, status) => {
    const sts = tabStatuses(id);
    return !sts.length || sts.includes(status || "new");
  };
  const isTriageTab = (id) => TRIAGE_TABS.has(id);
  const isWide = () => window.matchMedia(WIDE_QUERY).matches;

  // ---- data layer: API mode ------------------------------------------------------------------------
  async function getJSON(url, opts) {
    const res = await fetch(url, Object.assign({ headers: { Accept: "application/json" } }, opts || {}));
    if (!res.ok) {
      let msg = res.status + " " + (res.statusText || "error");
      try {
        const body = await res.json();
        if (body && body.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch (e) {
        /* not JSON */
      }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function postJSON(url, body) {
    return getJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  }

  function apiParams(f, withLanes) {
    const p = new URLSearchParams();
    if (f.q) p.set("q", f.q);
    for (const s of tabStatuses(f.tab)) p.append("status", s);
    if (withLanes !== false) {
      f.lanes.forEach((x) => p.append("lane", x));
      f.groups.forEach((x) => p.append("group", x));
    }
    f.sources.forEach((x) => p.append("source", x));
    f.employers.forEach((x) => p.append("employer", x));
    if (!isTriageTab(f.tab)) {
      if (f.entry) p.set("entry", "1");
      if (f.min > 0) p.set("min_score", String(f.min));
      if (f.days) p.set("days", f.days);
    }
    if (f.loc) p.set("location", f.loc);
    if (f.remote) p.set("remote", "1");
    return p;
  }

  const ApiData = {
    async init() {
      const [lanes, stats] = await Promise.all([getJSON("api/lanes"), getJSON("api/stats")]);
      state.lanes = lanes;
      state.stats = stats;
      if (lanes && lanes.min_score != null && !(window.JOBSWEEP_DEFAULTS || {}).min_score) {
        state.defaultMin = Number(lanes.min_score);
      }
    },
    jobs(f, offset) {
      const p = apiParams(f);
      p.set("sort", f.sort);
      p.set("limit", String(PAGE));
      p.set("offset", String(offset));
      p.set("compact", "1");
      return getJSON("api/jobs?" + p);
    },
    job(uid) {
      return getJSON("api/jobs/" + encodeURIComponent(uid));
    },
    setStatus(uid, status, note) {
      const body = { status };
      if (note !== undefined && note !== null) body.note = note;
      return postJSON("api/jobs/" + encodeURIComponent(uid) + "/status", body);
    },
    laneCounts(f) {
      return getJSON("api/lanes?" + apiParams(f, false));
    },
    stats: () => getJSON("api/stats"),
    runs: () => getJSON("api/runs?limit=50"),
    sources: () => getJSON("api/sources"),
    employers: () => getJSON("api/employers"),
    sweep: (opts) => postJSON("api/sweep", opts),
    sweepStatus: () => getJSON("api/sweep/status"),
    importJobs: (body) => postJSON("api/import", body),
  };

  // ---- data layer: static mode (GitHub Pages snapshot) ----------------------------------------------
  const StaticData = {
    items: [],
    byUid: new Map(),
    triage: {},
    base: {},
    filtered: null,
    filteredKey: "",

    async init() {
      const opt = (p) => getJSON(p).catch(() => null);
      const [jobs, lanes, stats, employers, sources] = await Promise.all([
        getJSON("data/jobs.json"), opt("data/lanes.json"), opt("data/stats.json"),
        opt("data/employers.json"), opt("data/sources.json"),
      ]);
      this.base = { jobs, stats: stats || {}, employers, sources };
      const gen = parseDate(jobs && jobs.generated_at);
      state.generatedAt = gen ? gen.getTime() : Date.now();
      if (jobs && jobs.min_score != null) state.defaultMin = Number(jobs.min_score);
      this.triage = ls.get(TRIAGE_KEY, {}) || {};
      state.lanes = lanes && lanes.groups ? lanes : this.lanesFromItems(jobs.items || []);
      indexLanes();
      this.items = (jobs.items || []).map((it) => this.prep(it));
      this.byUid = new Map(this.items.map((it) => [it.uid, it]));
      state.stats = this.stats();
    },

    lanesFromItems(items) {
      const groups = new Map();
      for (const it of items) {
        (it.lanes || []).forEach((lane, i) => {
          const g = (it.groups || [])[i] || (it.groups || [])[0] || "other";
          if (!groups.has(g)) groups.set(g, new Set());
          groups.get(g).add(lane);
        });
      }
      return {
        groups: [...groups].map(([id, lanes]) => ({
          id, name: id, active: id !== "earlier-lanes",
          lanes: [...lanes].map((l) => ({ id: l, name: l, group: id, active: id !== "earlier-lanes" })),
        })),
      };
    },

    prep(it) {
      const t = this.triage[it.uid];
      if (t) {
        if (t.status) it.status = t.status;
        if (t.note !== undefined) it.note = t.note;
      }
      it.status = it.status || "new";
      it.lanes = it.lanes || [];
      it.groups = it.groups || [];
      const fs = parseDate(it.first_seen);
      it._first = fs ? fs.getTime() : 0;
      const laneNames = it.lanes.map((l) => (state.laneIndex[l] || {}).name || l).join(" ");
      it._hay = [it.title, it.company, it.location, laneNames, it.department, it.grade, it.description]
        .filter(Boolean).join(" \u0001 ").toLowerCase();
      it._inactiveOnly = it.lanes.length
        ? it.lanes.every((l) => state.inactiveLanes.has(l))
        : it.groups.length > 0 && it.groups.every((g) => state.inactiveGroups.has(g));
      return it;
    },

    match(it, f, opts) {
      const statuses = tabStatuses(f.tab);
      const st = it.status || "new";
      if (statuses.length && !statuses.includes(st)) return false;
      const activeOnly = !(statuses.length && !statuses.includes("new"));
      if (activeOnly && it.active === false) return false;
      if (it.duplicate_of) return false;
      if (!opts.countMode) {
        if (f.lanes.length && !it.lanes.some((l) => f.lanes.includes(l))) return false;
        if (f.groups.length && !it.groups.some((g) => f.groups.includes(g))) return false;
        const explicit = f.lanes.some((l) => state.inactiveLanes.has(l)) || f.groups.some((g) => state.inactiveGroups.has(g));
        if (!explicit && it._inactiveOnly) return false;
      }
      if (f.sources.length && !f.sources.includes(it.source)) return false;
      if (f.employers.length && !f.employers.includes(it.employer_key)) return false;
      if (!isTriageTab(f.tab)) {
        if (f.entry && it.entry_level === false) return false;
        if (f.min > 0 && (Number(it.score) || 0) < f.min) return false;
        if (f.days && it._first < state.generatedAt - Number(f.days) * DAY) return false;
      }
      if (f.loc) {
        const want = f.loc.toLowerCase();
        const loc = String(it.location || "").toLowerCase();
        if (!(loc.includes(want) || (want.includes("remote") && it.remote === true))) return false;
      }
      if (f.remote && it.remote !== true) return false;
      if (opts.tokens && opts.tokens.length && !opts.tokens.every((t) => it._hay.includes(t))) return false;
      return true;
    },

    tokens(q) {
      return String(q || "").toLowerCase().split(/\s+/).map((t) => t.replace(/^"|"$/g, "")).filter(Boolean);
    },

    sorted(f) {
      const key = JSON.stringify([f, this.items.length]);
      if (this.filtered && this.filteredKey === key) return this.filtered;
      const tokens = this.tokens(f.q);
      const out = this.items.filter((it) => this.match(it, f, { tokens }));
      const day = (s) => (s ? String(s).slice(0, 10) : "");
      const by = {
        score: (a, b) => (b.score || 0) - (a.score || 0) || b._first - a._first || (a.uid < b.uid ? -1 : 1),
        newest: (a, b) => b._first - a._first || (b.score || 0) - (a.score || 0),
        posted: (a, b) => (!a.posted_at) - (!b.posted_at) || day(b.posted_at).localeCompare(day(a.posted_at)) || (b.score || 0) - (a.score || 0),
        closing: (a, b) => (!a.closes_at) - (!b.closes_at) || day(a.closes_at).localeCompare(day(b.closes_at)) || (b.score || 0) - (a.score || 0),
        company: (a, b) => String(a.company || "").localeCompare(String(b.company || ""), "en", { sensitivity: "base" }) || (b.score || 0) - (a.score || 0),
      };
      out.sort(by[f.sort] || by.score);
      this.filtered = out;
      this.filteredKey = key;
      return out;
    },

    async jobs(f, offset) {
      const all = this.sorted(f);
      return { total: all.length, items: all.slice(offset, offset + PAGE) };
    },
    async job(uid) {
      return this.byUid.get(uid) || null;
    },
    async setStatus(uid, status, note) {
      const it = this.byUid.get(uid);
      if (!it) throw new Error("unknown job " + uid);
      it.status = status;
      if (note !== undefined && note !== null) it.note = note;
      it.status_updated_at = new Date().toISOString();
      this.triage[uid] = { status: it.status, note: it.note || "", at: it.status_updated_at };
      this.filtered = null;
      if (!ls.set(TRIAGE_KEY, this.triage)) toast("Could not save to this browser's storage", { error: true });
      return it;
    },
    async laneCounts(f) {
      const tokens = this.tokens(f.q);
      const laneN = {}, groupN = {};
      let total = 0;
      for (const it of this.items) {
        if (!this.match(it, f, { tokens, countMode: true })) continue;
        if (!it._inactiveOnly) total += 1;
        for (const l of new Set(it.lanes)) {
          if (!it._inactiveOnly || state.inactiveLanes.has(l)) laneN[l] = (laneN[l] || 0) + 1;
        }
        for (const g of new Set(it.groups)) {
          if (!it._inactiveOnly || state.inactiveGroups.has(g)) groupN[g] = (groupN[g] || 0) + 1;
        }
      }
      const out = JSON.parse(JSON.stringify({ groups: state.lanes.groups || [] }));
      for (const g of out.groups) {
        g.count = groupN[g.id] || 0;
        for (const l of g.lanes || []) l.count = laneN[l.id] || 0;
      }
      out.total = total;
      return out;
    },
    stats() {
      const s = Object.assign({}, this.base.stats || {});
      const byStatus = {};
      for (const it of this.items) byStatus[it.status] = (byStatus[it.status] || 0) + 1;
      s.by_status = byStatus;
      const bySource = {};
      for (const it of this.items) bySource[it.source] = (bySource[it.source] || 0) + 1;
      s.by_source = bySource;
      if (s.active == null) s.active = this.items.filter((it) => it.active !== false).length;
      return s;
    },
    async runs() {
      const last = (this.base.stats || {}).last_run;
      return last ? [last] : [];
    },
    async sources() {
      return this.base.sources || [];
    },
    async employers() {
      return this.base.employers || { sweepable: [], manual: [] };
    },
  };

  let data = ApiData;

  // ---- lanes index ---------------------------------------------------------------------------------
  function indexLanes() {
    state.laneIndex = {};
    state.groupIndex = {};
    state.inactiveLanes = new Set();
    state.inactiveGroups = new Set();
    for (const g of state.lanes.groups || []) {
      const gActive = g.active !== false;
      state.groupIndex[g.id] = { name: g.name || g.id, active: gActive };
      if (!gActive) state.inactiveGroups.add(g.id);
      for (const l of g.lanes || []) {
        const active = gActive && l.active !== false;
        state.laneIndex[l.id] = { name: l.name || l.id, group: g.id, active };
        if (!active) state.inactiveLanes.add(l.id);
      }
    }
  }

  function groupClass(g) {
    return g ? "g-" + String(g).replace(/[^a-z0-9-]/gi, "") : "";
  }
  function tint(el, g) {
    if (g && !KNOWN_GROUPS.has(g)) {
      el.style.setProperty("--h", String(hashHue(g)));
      el.style.setProperty("--s", "55%");
    }
    return el;
  }
  function laneChip(laneId) {
    const info = state.laneIndex[laneId] || { name: laneId, group: null };
    const gname = info.group ? (state.groupIndex[info.group] || {}).name : "";
    const el = h("span", {
      class: "chip " + groupClass(info.group),
      title: info.name + (gname ? " · " + gname : ""),
      text: shortName(info.name) || laneId,
    });
    return tint(el, info.group);
  }
  function groupChip(groupId) {
    const info = state.groupIndex[groupId] || { name: groupId };
    return tint(h("span", { class: "chip " + groupClass(groupId), title: info.name, text: shortName(info.name) }), groupId);
  }
  function swatch(groupId) {
    return tint(h("span", { class: "swatch " + groupClass(groupId), "aria-hidden": "true" }), groupId);
  }

  // ---- hash (shareable views) ----------------------------------------------------------------------
  function hashFor() {
    if (state.view !== "jobs") return "#/" + state.view;
    const f = state.f;
    const p = new URLSearchParams();
    if (f.tab !== "inbox") p.set("tab", f.tab);
    if (f.q) p.set("q", f.q);
    if (f.lanes.length) p.set("lane", f.lanes.join(","));
    if (f.groups.length) p.set("group", f.groups.join(","));
    if (f.sources.length) p.set("src", f.sources.join(","));
    if (f.employers.length) p.set("emp", f.employers.join(","));
    if (!f.entry) p.set("entry", "0");
    if (f.min !== state.defaultMin) p.set("min", String(f.min));
    if (f.loc) p.set("loc", f.loc);
    if (f.remote) p.set("remote", "1");
    if (f.days) p.set("days", f.days);
    if (f.sort !== "score") p.set("sort", f.sort);
    if (state.selUid) p.set("job", state.selUid);
    const qs = p.toString();
    return "#/jobs" + (qs ? "?" + qs : "");
  }

  function writeHash() {
    const next = hashFor();
    if (state.view === "jobs") state.jobsHash = next;
    if (location.hash !== next) history.replaceState(null, "", next);
  }

  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const qi = raw.indexOf("?");
    const path = qi >= 0 ? raw.slice(0, qi) : raw;
    const view = VIEWS.some((v) => v.id === path && (!v.apiOnly || state.mode === "api")) ? path : "jobs";
    if (view !== "jobs") return { view, f: null, job: null };
    const p = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : "");
    const f = defaultFilters();
    f.q = p.get("q") || "";
    f.tab = TABS.some((t) => t.id === p.get("tab")) ? p.get("tab") : "inbox";
    f.lanes = list(p.get("lane"));
    f.groups = list(p.get("group"));
    f.sources = list(p.get("src"));
    f.employers = list(p.get("emp"));
    f.entry = p.get("entry") !== "0";
    const min = Number(p.get("min"));
    f.min = p.has("min") && !isNaN(min) ? clamp(min, 0, 100) : state.defaultMin;
    f.loc = p.get("loc") || "";
    f.remote = p.get("remote") === "1";
    f.days = DAYS.includes(p.get("days")) ? p.get("days") : "";
    f.sort = SORTS.includes(p.get("sort")) ? p.get("sort") : "score";
    return { view, f, job: p.get("job") };
  }

  function onHashChange() {
    const parsed = parseHash();
    if (parsed.view !== state.view) showView(parsed.view);
    if (parsed.view === "jobs" && parsed.f) {
      const changed = JSON.stringify(parsed.f) !== JSON.stringify(state.f);
      if (changed) {
        state.f = parsed.f;
        state.pendingJob = parsed.job;
        syncControls();
        applyFilters({ fromHash: true });
      } else if (parsed.job && parsed.job !== state.selUid) {
        const i = indexOfUid(parsed.job);
        if (i >= 0) select(i, { open: true });
      }
    }
  }

  // ---- header -------------------------------------------------------------------------------------
  function renderViewsNav() {
    for (const id of ["views", "side-views"]) {
      const nav = $(id);
      nav.replaceChildren();
      const ul = id === "side-views" ? h("ul", { class: "tree" }) : null;
      for (const v of VIEWS) {
        if (v.apiOnly && state.mode !== "api") continue;
        const a = h("a", {
          href: v.id === "jobs" ? state.jobsHash : "#/" + v.id,
          "aria-current": state.view === v.id ? "page" : null,
          class: id === "side-views" ? "node" : null,
          text: v.label,
          dataset: { view: v.id },
        });
        if (ul) ul.append(h("li", null, a));
        else nav.append(a);
      }
      if (ul) nav.append(h("div", { class: "side-head" }, h("span", { text: "Views" })), ul);
    }
  }

  function countItems(s) {
    const bs = (s && s.by_status) || {};
    return [
      { key: "active", n: s.active, label: "active", href: "#/jobs?tab=all" },
      { key: "new_24h", n: s.new_24h, label: "new 24h", href: "#/jobs?days=1&sort=newest", accent: true },
      { key: "new_7d", n: s.new_7d, label: "new 7d", href: "#/jobs?days=7&sort=newest" },
      { key: "saved", n: bs.saved || 0, label: "saved", href: "#/jobs?tab=saved" },
      { key: "applied", n: (bs.applied || 0) + (bs.interviewing || 0) + (bs.offer || 0), label: "applied", href: "#/jobs?tab=applied" },
    ];
  }

  function renderCounts() {
    const s = state.stats || {};
    const items = countItems(s);
    const head = $("counts");
    head.replaceChildren(
      ...items.map((c) =>
        h("a", { class: "count" + (c.accent ? " accent" : ""), href: c.href, title: fmtNum(c.n) + " " + c.label },
          h("b", { text: c.n == null ? "–" : fmtNum(c.n) }), h("span", { text: c.label })),
      ),
    );
    const side = $("side-overview");
    side.replaceChildren(
      h("div", { class: "side-head" }, h("span", { text: "Overview" })),
      h("ul", { class: "tree" },
        items.map((c) => h("li", null, h("a", { class: "node", href: c.href },
          h("span", { class: "label", text: c.label }), h("span", { class: "n", text: c.n == null ? "–" : fmtNum(c.n) }))))),
    );
    renderLastSweep();
  }

  function renderLastSweep() {
    const el = $("last-sweep");
    const run = (state.stats || {}).last_run;
    el.replaceChildren();
    if (state.sweep.running) {
      el.append(h("span", { class: "dot running", "aria-hidden": "true" }), h("span", { class: "txt", text: "Sweep running…" }));
      el.title = "A sweep is running";
      return;
    }
    if (!run) {
      el.append(h("span", { class: "dot stale", "aria-hidden": "true" }), h("span", { class: "txt", text: "No sweeps yet" }));
      el.title = "No sweep has run yet";
      return;
    }
    const at = run.finished_at || run.started_at;
    const d = parseDate(at);
    const stale = d && Date.now() - d.getTime() > 36 * 3600000;
    el.append(h("span", { class: "dot" + (stale ? " stale" : ""), "aria-hidden": "true" }),
      h("span", { class: "txt", text: "Swept " + relTime(at) }));
    el.title = "Last sweep #" + run.id + " " + (run.finished_at ? "finished " : "started ") + fmtDate(at, true);
  }

  function renderBanner() {
    const b = $("banner");
    if (state.mode !== "static") {
      b.hidden = true;
      return;
    }
    const jobs = StaticData.base.jobs || {};
    const gen = jobs.generated_at;
    b.replaceChildren(
      icon("refresh", "sm"),
      h("span", null, "Snapshot generated ", h("b", { text: relTime(gen, Date.now()) || "recently" }),
        gen ? " (" + fmtDate(gen, true) + ")" : "", " · ", fmtNum(jobs.total || (jobs.items || []).length),
        " jobs with score ≥ " + (jobs.min_score != null ? jobs.min_score : state.defaultMin),
        " · your statuses and notes are kept in this browser"),
      h("button", { class: "btn sm", type: "button", onclick: exportTriage }, icon("download", "sm"), "Export my statuses"),
    );
    b.hidden = false;
  }

  function exportTriage() {
    const blob = new Blob([JSON.stringify(StaticData.triage, null, 1)], { type: "application/json" });
    const a = h("a", { download: "jobsweep-statuses.json" });
    a.href = URL.createObjectURL(blob);
    document.body.append(a);
    a.click();
    setTimeout(() => {
      URL.revokeObjectURL(a.href);
      a.remove();
    }, 500);
  }

  // ---- theme ---------------------------------------------------------------------------------------
  const THEMES = ["system", "light", "dark"];
  function applyTheme(mode) {
    const root = document.documentElement;
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
    const btn = $("theme-btn");
    const name = mode === "light" ? "sun" : mode === "dark" ? "moon" : "monitor";
    btn.replaceChildren(icon(name));
    btn.setAttribute("aria-label", "Theme: " + mode + " (click to change)");
    btn.title = "Theme: " + mode;
  }
  function currentTheme() {
    const t = ls.raw(THEME_KEY);
    return t === "light" || t === "dark" ? t : "system";
  }
  function cycleTheme() {
    const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    ls.raw(THEME_KEY, next === "system" ? null : next);
    applyTheme(next);
    toast("Theme: " + next);
  }

  // ---- sidebar ------------------------------------------------------------------------------------
  function isExpanded(gid) {
    return state.expanded[gid] !== false;
  }
  function toggleExpand(gid) {
    state.expanded[gid] = !isExpanded(gid);
    ls.set(EXPANDED_KEY, state.expanded);
    renderLaneTree();
  }

  function nodeButton(opts) {
    return h("button", {
      class: "node" + (opts.cls ? " " + opts.cls : "") + (opts.count ? "" : " zero"),
      type: "button",
      "aria-pressed": String(Boolean(opts.pressed)),
      title: opts.title || null,
      onclick: opts.onclick,
    }, opts.lead || null, h("span", { class: "label", text: opts.label }), h("span", { class: "n", text: fmtNum(opts.count || 0) }));
  }

  function laneItems(g) {
    const lanes = g.lanes || [];
    const showAll = state.showEmpty[g.id];
    const visible = lanes.filter((l) => showAll || l.count > 0 || state.f.lanes.includes(l.id));
    const hidden = lanes.length - visible.length;
    const ul = h("ul");
    for (const l of visible) {
      ul.append(h("li", null, nodeButton({
        cls: "lane-node",
        label: l.name || l.id,
        title: (l.name || l.id) + " · Ctrl/⌘-click to combine",
        count: l.count,
        pressed: state.f.lanes.includes(l.id),
        onclick: (e) => pickLane(l.id, e),
      })));
    }
    const out = [ul];
    if (hidden > 0) {
      out.push(h("button", {
        class: "link-btn more-btn", type: "button",
        onclick: () => { state.showEmpty[g.id] = true; renderLaneTree(); },
        text: "+" + hidden + (hidden === 1 ? " lane" : " lanes") + " with no matches",
      }));
    } else if (showAll && lanes.some((l) => !l.count)) {
      out.push(h("button", {
        class: "link-btn more-btn", type: "button",
        onclick: () => { state.showEmpty[g.id] = false; renderLaneTree(); },
        text: "Hide empty lanes",
      }));
    }
    return out;
  }

  function renderLaneTree() {
    const root = $("lane-tree");
    const groups = (state.lanes && state.lanes.groups) || [];
    const active = groups.filter((g) => g.active !== false);
    const earlier = groups.filter((g) => g.active === false);
    const f = state.f;
    const anyLane = f.lanes.length || f.groups.length;
    const tree = h("ul", { class: "tree" });
    tree.append(h("li", null, nodeButton({
      label: "All lanes", count: state.laneTotal, pressed: !anyLane,
      lead: icon("inbox", "sm"), onclick: () => clearLanes(),
    })));
    for (const g of active) {
      const expanded = isExpanded(g.id);
      const li = h("li");
      li.append(h("div", { class: "group-row" },
        h("button", {
          class: "caret", type: "button", "aria-expanded": String(expanded),
          "aria-label": (expanded ? "Collapse " : "Expand ") + (g.name || g.id),
          onclick: () => toggleExpand(g.id),
        }, icon("chevron")),
        nodeButton({
          label: shortName(g.name || g.id), title: (g.name || g.id) + " · Ctrl/⌘-click to combine",
          count: g.count, pressed: f.groups.includes(g.id), lead: swatch(g.id),
          onclick: (e) => pickGroup(g.id, e),
        })));
      if (expanded) append(li, laneItems(g));
      tree.append(li);
    }
    const parts = [
      h("div", { class: "side-head" }, h("span", { text: "Lanes" }),
        anyLane ? h("button", { class: "link-btn", type: "button", onclick: () => clearLanes(), text: "Clear" }) : null),
      tree,
    ];
    if (earlier.length) {
      const total = earlier.reduce((n, g) => n + (g.count || 0), 0);
      const open = earlier.some((g) => (g.lanes || []).some((l) => f.lanes.includes(l.id)) || f.groups.includes(g.id));
      const det = h("details", { class: "earlier", open: open || state.earlierOpen ? "" : null });
      det.addEventListener("toggle", () => { state.earlierOpen = det.open; });
      det.append(h("summary", { class: "node" }, icon("chevron"),
        h("span", { class: "label", text: "Earlier lanes (not in sweeps)" }), h("span", { class: "n", text: fmtNum(total) })));
      for (const g of earlier) {
        const ul = h("ul", { class: "tree" });
        for (const l of g.lanes || []) {
          ul.append(h("li", null, nodeButton({
            cls: "lane-node", label: l.name || l.id, count: l.count, pressed: f.lanes.includes(l.id),
            onclick: (e) => pickLane(l.id, e), lead: swatch(g.id),
          })));
        }
        det.append(ul);
      }
      parts.push(det);
    }
    parts.push(h("p", { class: "side-foot" }, "Click a lane to filter. Hold ", h("kbd", { text: "Ctrl" }), " or ",
      h("kbd", { text: "⌘" }), " to pick several."));
    root.replaceChildren(...parts);
  }

  function pickLane(id, e) {
    const f = state.f;
    if (e && (e.ctrlKey || e.metaKey)) {
      f.lanes = f.lanes.includes(id) ? f.lanes.filter((x) => x !== id) : f.lanes.concat(id);
    } else {
      f.lanes = f.lanes.length === 1 && f.lanes[0] === id ? [] : [id];
      closeDrawer();
    }
    f.groups = [];
    applyFilters();
  }

  function pickGroup(id, e) {
    const f = state.f;
    if (e && (e.ctrlKey || e.metaKey)) {
      f.groups = f.groups.includes(id) ? f.groups.filter((x) => x !== id) : f.groups.concat(id);
    } else {
      f.groups = f.groups.length === 1 && f.groups[0] === id ? [] : [id];
      closeDrawer();
    }
    f.lanes = [];
    applyFilters();
  }

  function clearLanes() {
    state.f.lanes = [];
    state.f.groups = [];
    closeDrawer();
    applyFilters();
  }

  function openDrawer() {
    $("sidebar").classList.add("open");
    $("scrim").hidden = false;
    $("menu-btn").setAttribute("aria-expanded", "true");
    const first = $("sidebar").querySelector("a, button");
    if (first) first.focus();
  }
  function closeDrawer() {
    if (!$("sidebar").classList.contains("open")) return;
    $("sidebar").classList.remove("open");
    $("scrim").hidden = true;
    $("menu-btn").setAttribute("aria-expanded", "false");
  }

  // ---- filter bar ---------------------------------------------------------------------------------
  function renderTabs() {
    const bs = (state.stats && state.stats.by_status) || {};
    const wrap = $("status-tabs");
    wrap.replaceChildren(
      ...TABS.map((t) => {
        const n = t.id === "inbox" || t.id === "all" ? null : t.statuses.reduce((a, s) => a + (bs[s] || 0), 0);
        return h("button", {
          class: "tab", type: "button", role: "tab", id: "tab-" + t.id,
          "aria-selected": String(state.f.tab === t.id), "aria-controls": "job-list",
          tabindex: state.f.tab === t.id ? "0" : "-1",
          onclick: () => { state.f.tab = t.id; applyFilters(); },
        }, t.label, n ? h("span", { class: "n", text: fmtNum(n) }) : null);
      }),
    );
  }

  function renderSourcesMenu() {
    const menu = $("f-sources-menu");
    const bySource = (state.stats && state.stats.by_source) || {};
    const names = [...new Set(Object.keys(bySource).concat(state.f.sources))].sort();
    menu.replaceChildren();
    if (!names.length) menu.append(h("p", { class: "muted", style: { margin: "6px" }, text: "No sources in the data yet." }));
    for (const name of names) {
      const cb = h("input", { type: "checkbox", value: name, checked: state.f.sources.includes(name) });
      cb.addEventListener("change", () => {
        const sel = new Set(state.f.sources);
        if (cb.checked) sel.add(name);
        else sel.delete(name);
        state.f.sources = [...sel];
        applyFilters();
      });
      menu.append(h("label", null, cb, h("span", { text: name }), h("span", { class: "n", text: fmtNum(bySource[name] || 0) })));
    }
    menu.append(h("div", { class: "menu-foot" },
      h("button", { class: "link-btn", type: "button", text: "All sources", onclick: () => { state.f.sources = []; applyFilters(); } }),
      h("button", { class: "link-btn", type: "button", text: "Done", onclick: () => { $("f-sources").open = false; } })));
    const lbl = $("f-sources-label");
    lbl.replaceChildren(state.f.sources.length
      ? h("span", { class: "pill-count", text: String(state.f.sources.length) })
      : document.createTextNode("All"));
  }

  function syncControls() {
    const f = state.f;
    $("search").value = f.q;
    $("f-entry").checked = f.entry;
    $("f-min").value = String(f.min);
    $("f-min-out").textContent = String(f.min);
    $("f-loc").value = f.loc;
    $("f-remote").checked = f.remote;
    $("f-days").value = f.days;
    $("f-sort").value = f.sort;
    const triage = isTriageTab(f.tab);
    for (const id of ["f-entry", "f-min", "f-days"]) {
      const ctl = $(id).closest(".control");
      ctl.style.opacity = triage ? "0.5" : "";
      ctl.title = triage ? "Not applied on triage tabs: everything you filed is listed" : ctl.dataset.title || "";
    }
    renderTabs();
    renderSourcesMenu();
    renderActiveChips();
  }

  function isDefaultFilters() {
    const d = defaultFilters();
    const f = Object.assign({}, state.f, { tab: d.tab, sort: d.sort });
    return JSON.stringify(f) === JSON.stringify(d);
  }

  function renderActiveChips() {
    const f = state.f;
    const chips = [];
    const chip = (label, onRemove, title) =>
      h("button", { class: "filter-chip", type: "button", title: title || "Remove filter", "aria-label": "Remove filter " + label, onclick: onRemove }, label, icon("x"));
    for (const id of f.groups) chips.push(chip(shortName((state.groupIndex[id] || {}).name || id), () => { f.groups = f.groups.filter((x) => x !== id); applyFilters(); }));
    for (const id of f.lanes) chips.push(chip(shortName((state.laneIndex[id] || {}).name || id), () => { f.lanes = f.lanes.filter((x) => x !== id); applyFilters(); }));
    for (const key of f.employers) chips.push(chip("Employer: " + employerName(key), () => { f.employers = f.employers.filter((x) => x !== key); applyFilters(); }));
    if (!isDefaultFilters()) {
      chips.push(h("button", {
        class: "link-btn", type: "button", text: "Reset filters",
        onclick: () => { const tab = state.f.tab; state.f = defaultFilters(); state.f.tab = tab; syncControls(); applyFilters(); },
      }));
    }
    $("active-chips").replaceChildren(...chips);
  }

  function employerName(key) {
    const emps = state.cache.employers;
    if (emps) {
      const e = (emps.sweepable || []).concat(emps.manual || []).find((x) => x.key === key);
      if (e) return e.name;
    }
    return key;
  }

  const refreshLaneCountsSoon = debounce(refreshLaneCounts, 250);
  let laneReq = 0;
  async function refreshLaneCounts() {
    const id = ++laneReq;
    try {
      const res = await data.laneCounts(state.f);
      if (id !== laneReq) return;
      state.lanes = res;
      state.laneTotal = res.total || 0;
      indexLanes();
      renderLaneTree();
    } catch (err) {
      console.warn("lane counts failed", err);
    }
  }

  async function refreshStats() {
    try {
      state.stats = await data.stats();
      renderCounts();
      renderTabs();
      renderSourcesMenu();
    } catch (err) {
      console.warn("stats failed", err);
    }
  }
  const refreshStatsSoon = debounce(refreshStats, 400);

  function applyFilters(opts) {
    opts = opts || {};
    if (!opts.fromHash) {
      state.pendingJob = null;
    }
    syncControls();
    writeHash();
    loadJobs(true);
    refreshLaneCountsSoon();
  }

  // ---- job list -----------------------------------------------------------------------------------
  const rowsEl = () => $("rows");
  const indexOfUid = (uid) => state.items.findIndex((it) => it.uid === uid);
  const rowEl = (uid) => {
    for (const el of rowsEl().children) if (el.dataset.uid === uid) return el;
    return null;
  };

  function scoreClass(s) {
    if (s >= 70) return "hi";
    if (s >= 50) return "mid";
    if (s >= 30) return "lo";
    return "min";
  }

  function isNew(it) {
    const ref = state.mode === "static" ? state.generatedAt : Date.now();
    const t = it._first || (parseDate(it.first_seen) || { getTime: () => 0 }).getTime();
    return t > 0 && ref - t < DAY;
  }

  function entryBadge(it) {
    if (it.entry_level === true) return h("span", { class: "badge entry", title: "Entry-level / early-career fit", text: "Entry" });
    if (it.entry_level === false) {
      const why = (it.match_reasons || []).find((r) => /^senior|years/i.test(r));
      return h("span", { class: "badge senior", title: why || "Probably too senior", text: "Senior" });
    }
    return null;
  }

  function tagBadges(it, max) {
    const tags = (it.tags || []).filter((t) => TAG_LABEL[t]).slice(0, max || 2);
    const out = tags.map((t) => h("span", { class: "badge tag", text: TAG_LABEL[t] }));
    if (it.grade) out.push(h("span", { class: "badge", title: "Grade", text: it.grade }));
    return out;
  }

  function laneChips(it, max) {
    const lanes = it.lanes || [];
    if (!lanes.length) return (it.groups || []).slice(0, max).map(groupChip);
    const out = lanes.slice(0, max).map(laneChip);
    if (lanes.length > max) {
      out.push(h("span", { class: "chip more", title: lanes.slice(max).map((l) => (state.laneIndex[l] || {}).name || l).join(", "), text: "+" + (lanes.length - max) }));
    }
    return out;
  }

  function statusTag(it) {
    const st = it.status || "new";
    if (st === "new") return null;
    return h("span", { class: "status-tag " + st, text: STATUS_LABEL[st] || st });
  }

  function actionButton(it, act, label, key, iconName, cls) {
    const on = act !== "note" && it.status === act;
    const hasNote = act === "note" && it.note;
    return h("button", {
      class: "act " + (cls || "") + (on || hasNote ? " on" : ""),
      type: "button",
      dataset: { act },
      title: (on ? "Undo " : "") + label + " (" + key + ")",
      "aria-label": label,
      "aria-pressed": act === "note" ? null : String(on),
      "aria-keyshortcuts": key,
    }, icon(iconName));
  }

  function renderRow(it) {
    const sc = Math.round(Number(it.score) || 0);
    const salary = fmtSalary(it);
    const posted = it.posted_at ? relTime(it.posted_at) : "";
    const close = closingInfo(it.closes_at);
    const st = it.status || "new";
    const showStatus = state.f.tab === "all" || tabStatuses(state.f.tab).length > 1;
    const li = h("li", {
      class: "row" + (st === "dismissed" || st === "rejected" ? " done" : ""),
      dataset: { uid: it.uid },
      tabindex: "-1",
      "aria-current": it.uid === state.selUid ? "true" : null,
    });
    li.append(
      h("div", { class: "score " + scoreClass(sc), title: "Match score " + sc + " / 100", "aria-label": "Score " + sc, text: String(sc) }),
      h("div", { class: "row-main" },
        h("div", { class: "row-title" },
          isNew(it) ? h("span", { class: "new-dot", title: "New in the last 24 hours", role: "img", "aria-label": "New" }) : null,
          h("a", { class: "title", href: it.url, target: "_blank", rel: "noopener noreferrer", title: it.title, text: it.title || "(untitled)" }),
          entryBadge(it),
          showStatus ? statusTag(it) : null),
        h("div", { class: "row-meta" },
          h("span", { class: "company", title: it.company, text: it.company || "Unknown employer" }),
          it.location ? [sep(), h("span", { class: "loc", title: it.location, text: it.location })] : null,
          it.remote === true ? h("span", { class: "badge remote", text: "Remote" }) : null,
          salary ? [sep(), h("span", { class: "salary", text: salary })] : null),
        h("div", { class: "row-chips" }, laneChips(it, 3), tagBadges(it, 1), h("span", { class: "badge src", title: "Source", text: it.source }))),
      h("div", { class: "row-side" },
        h("div", { class: "dates" },
          posted
            ? h("span", { title: "Posted " + fmtDate(it.posted_at), text: posted })
            : it.first_seen ? h("span", { title: "First seen " + fmtDate(it.first_seen, true), text: "seen " + relTime(it.first_seen) }) : null,
          close ? h("span", { class: "closing" + (close.soon ? " soon" : "") + (close.past ? " past" : ""), title: "Closes " + fmtDate(it.closes_at), text: close.label }) : null),
        h("div", { class: "row-actions" },
          actionButton(it, "saved", "Save", "s", "bookmark"),
          actionButton(it, "applied", "Applied", "a", "check", "applied"),
          actionButton(it, "dismissed", "Dismiss", "d", "x", "dismiss"),
          actionButton(it, "note", "Note", "n", "note"))),
    );
    return li;
  }

  function renderList() {
    const ul = rowsEl();
    ul.replaceChildren(...state.items.map(renderRow));
    renderFoot();
    renderResultCount();
  }

  function renderSkeleton() {
    rowsEl().replaceChildren(h("li", { class: "skeleton", "aria-hidden": "true" }, h("div"), h("div"), h("div"), h("div"), h("div")));
    $("list-foot").replaceChildren();
  }

  function renderResultCount() {
    const n = state.total;
    $("result-count").textContent = state.loading && !state.items.length ? "Loading…" : fmtNum(n) + (n === 1 ? " job" : " jobs");
  }

  function renderFoot() {
    const foot = $("list-foot");
    foot.replaceChildren();
    if (!state.items.length) {
      if (state.loading) return;
      const emptyDb = state.stats && !state.stats.total;
      const tab = tabOf(state.f.tab);
      foot.append(h("div", { class: "empty" },
        icon("inbox"),
        h("h3", { text: emptyDb ? "No jobs yet" : tab.id === "inbox" && isDefaultFilters() ? "Inbox zero" : "Nothing matches these filters" }),
        h("p", { text: emptyDb
          ? (state.mode === "api" ? "Run a sweep from the Runs tab, or import results from a Claude connector." : "This snapshot has no jobs.")
          : tab.id === "inbox" && isDefaultFilters()
            ? "Every new match has been triaged. New postings arrive with the next sweep."
            : "Try lowering the score threshold, turning off Entry-level, or clearing lane and source filters." }),
        emptyDb && state.mode === "api"
          ? h("a", { class: "btn primary", href: "#/runs" }, icon("play", "sm"), "Run a sweep")
          : !isDefaultFilters()
            ? h("button", { class: "btn", type: "button", onclick: () => { const tab = state.f.tab; state.f = defaultFilters(); state.f.tab = tab; applyFilters(); } }, "Reset filters")
            : null));
      return;
    }
    if (state.items.length < state.total) {
      foot.append(h("span", { text: "Showing " + fmtNum(state.items.length) + " of " + fmtNum(state.total) }),
        h("button", { class: "btn sm", type: "button", disabled: state.loading, onclick: () => loadJobs(false), text: state.loading ? "Loading…" : "Load more" }));
    } else {
      foot.append(h("span", { text: state.total > 3 ? "End of list · " + fmtNum(state.total) + " jobs" : "" }));
    }
  }

  async function loadJobs(reset) {
    const id = ++state.reqId;
    const offset = reset ? 0 : state.items.length;
    if (reset) {
      state.items = [];
      state.total = 0;
      renderSkeleton();
    }
    state.loading = true;
    renderResultCount();
    try {
      const res = await data.jobs(state.f, offset);
      if (id !== state.reqId) return;
      const rows = res.items || [];
      state.total = Number(res.total) || 0;
      if (reset) {
        state.items = rows;
        state.loading = false;
        renderList();
        restoreSelection();
      } else {
        const seen = new Set(state.items.map((x) => x.uid));
        const fresh = rows.filter((r) => !seen.has(r.uid));
        state.items.push(...fresh);
        state.loading = false;
        rowsEl().append(...fresh.map(renderRow));
        renderFoot();
        renderResultCount();
      }
      reobserve();
    } catch (err) {
      if (id !== state.reqId) return;
      state.loading = false;
      rowsEl().replaceChildren();
      $("list-foot").replaceChildren(h("div", { class: "empty" }, h("h3", { text: "Could not load jobs" }), h("p", { text: err.message || String(err) }),
        h("button", { class: "btn", type: "button", onclick: () => loadJobs(true), text: "Retry" })));
      renderResultCount();
    } finally {
      if (id === state.reqId) state.loading = false;
    }
  }

  function restoreSelection() {
    const pending = state.pendingJob;
    state.pendingJob = null;
    const want = pending || state.selUid;
    const i = want ? indexOfUid(want) : -1;
    if (i >= 0) {
      select(i, { open: Boolean(pending) && !isWide(), scroll: true });
      return;
    }
    if (pending) {
      // Shared link to a job outside the current filters: still show it.
      openUid(pending);
      return;
    }
    state.sel = -1;
    state.selUid = null;
    if (isWide() && state.items.length) select(0, { scroll: false });
    else {
      renderDetail(null);
      writeHash();
    }
  }

  // Re-arm the infinite-scroll observer: it only fires on changes, and the footer may never leave view.
  function reobserve() {
    if (!state.io) return;
    state.io.unobserve($("list-foot"));
    state.io.observe($("list-foot"));
  }

  const maybeLoadMore = () => {
    if (!state.loading && state.items.length < state.total) loadJobs(false);
  };

  function select(i, opts) {
    opts = opts || {};
    if (!state.items.length) return;
    i = clamp(i, 0, state.items.length - 1);
    const prev = rowsEl().querySelector('.row[aria-current="true"]');
    if (prev) prev.removeAttribute("aria-current");
    state.sel = i;
    const it = state.items[i];
    state.selUid = it.uid;
    const el = rowEl(it.uid);
    if (el) {
      el.setAttribute("aria-current", "true");
      if (opts.scroll !== false) el.scrollIntoView({ block: "nearest" });
    }
    showDetail(it, opts.open);
    writeHash();
    if (i >= state.items.length - 6) maybeLoadMore();
  }

  function move(delta) {
    if (!state.items.length) return;
    select(state.sel < 0 ? 0 : state.sel + delta, { open: $("detail").classList.contains("open") });
  }

  function currentItem() {
    if (state.sel >= 0 && state.items[state.sel] && state.items[state.sel].uid === state.selUid) return state.items[state.sel];
    if (state.detailItem && state.detailItem.uid === state.selUid) return state.detailItem;
    return null;
  }

  function openPosting(it) {
    it = it || currentItem();
    const url = it && safeUrl(it.url);
    if (!url) {
      toast("This job has no posting link");
      return;
    }
    window.open(url, "_blank", "noopener,noreferrer");
  }

  async function openUid(uid) {
    try {
      const it = await data.job(uid);
      if (!it) throw new Error("not found");
      state.selUid = uid;
      showDetail(it, true);
    } catch (err) {
      toast("Job " + uid + " not found", { error: true });
    }
  }

  // ---- detail pane --------------------------------------------------------------------------------
  function showDetail(it, open) {
    state.detailItem = it;
    renderDetail(it);
    const pane = $("detail");
    if (!isWide() && open) {
      pane.classList.add("open");
      const btn = pane.querySelector(".close-detail");
      if (btn) btn.focus({ preventScroll: true });
    }
    if (state.mode === "api" && it && it.description_truncated) {
      const cached = state.full.get(it.uid);
      if (cached) {
        mergeFull(it, cached);
        renderDetail(it);
      } else {
        data.job(it.uid).then((full) => {
          state.full.set(it.uid, full);
          mergeFull(it, full);
          if (state.detailItem === it) refreshDetailBody(it);
        }).catch(() => {});
      }
    }
  }

  function mergeFull(it, full) {
    it.description = full.description;
    it.description_truncated = false;
  }

  function refreshDetailBody(it) {
    const desc = $("detail").querySelector(".desc");
    if (desc) desc.replaceWith(renderDescription(it.description));
  }

  function closeDetail() {
    const pane = $("detail");
    if (!pane.classList.contains("open")) return false;
    pane.classList.remove("open");
    const el = state.selUid && rowEl(state.selUid);
    if (el) el.focus({ preventScroll: true });
    return true;
  }

  function renderDescription(text) {
    const box = h("div", { class: "desc" });
    if (!text) {
      box.append(h("p", { class: "muted", text: "No description captured for this posting. Open the posting for details." }));
      return box;
    }
    const blocks = String(text).replace(/\r\n?/g, "\n").split(/\n{2,}/);
    for (const block of blocks) {
      let para = [];
      let ul = null;
      const flush = () => {
        if (para.length) box.append(h("p", { text: para.join("\n") }));
        para = [];
      };
      for (const line of block.split("\n")) {
        const m = /^\s*(?:[-*•▪◦·‣]|\d{1,2}[.)])\s+(.*)$/.exec(line);
        if (m) {
          flush();
          if (!ul) {
            ul = h("ul");
            box.append(ul);
          }
          ul.append(h("li", { text: m[1] }));
        } else if (line.trim()) {
          ul = null;
          para.push(line.trim());
        }
      }
      flush();
    }
    return box;
  }

  function fact(label, value, cls) {
    if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return null;
    return h("div", { class: "fact" }, h("dt", { text: label }), h("dd", { class: cls || null }, value));
  }

  function renderDetail(it) {
    const pane = $("detail");
    if (!it) {
      pane.replaceChildren(h("div", { class: "detail-empty" }, h("div", null,
        icon("inbox", "lg"),
        h("p", { text: state.items.length ? "Select a job to see the details." : "No job selected." }),
        h("div", { class: "keys" },
          h("span", null, h("kbd", { text: "j" }), " ", h("kbd", { text: "k" }), " move  ·  ", h("kbd", { text: "o" }), " open posting"),
          h("span", null, h("kbd", { text: "s" }), " save  ·  ", h("kbd", { text: "a" }), " applied  ·  ", h("kbd", { text: "d" }), " dismiss  ·  ", h("kbd", { text: "?" }), " help")))));
      return;
    }
    const sc = Math.round(Number(it.score) || 0);
    const close = closingInfo(it.closes_at);
    const st = it.status || "new";
    const url = safeUrl(it.url);

    const bar = h("div", { class: "detail-bar" },
      h("button", { class: "icon-btn close-detail", type: "button", "aria-label": "Back to list", onclick: () => closeDetail() }, icon("back")),
      h("span", { class: "score " + scoreClass(sc), title: "Match score", text: String(sc) }),
      h("span", { class: "spacer" }),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Previous job (k)", title: "Previous (k)", onclick: () => move(-1) }, icon("chevron", "flip-up")),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Next job (j)", title: "Next (j)", onclick: () => move(1) }, icon("chevron", "flip-down")),
      url ? h("a", { class: "btn primary sm", href: url, target: "_blank", rel: "noopener noreferrer" }, "Open posting", icon("external", "sm")) : null);

    const seg = h("div", { class: "seg", role: "group", "aria-label": "Status" },
      STATUSES.map((s) => h("button", {
        type: "button", class: "st-" + s, "aria-pressed": String(st === s),
        onclick: () => setStatus(it.uid, s),
        text: STATUS_LABEL[s],
      })));

    const laneScores = it.lane_scores || {};
    const laneVals = (it.lanes || []).map((l) => Number(laneScores[l]) || 0);
    const maxLane = Math.max(100, ...laneVals);
    const lanes = (it.lanes || []).length
      ? h("div", { class: "lane-bars" }, (it.lanes || []).map((l) => {
        const info = state.laneIndex[l] || { group: null };
        const v = Number(laneScores[l]) || 0;
        const barFill = tint(h("span", { class: groupClass(info.group), style: { width: clamp((v / maxLane) * 100, 2, 100).toFixed(1) + "%" } }), info.group);
        return h("div", { class: "lane-bar" }, h("span", null, laneChip(l)), h("div", { class: "bar", "aria-hidden": "true" }, barFill),
          h("span", { class: "v", text: laneScores[l] != null ? String(Math.round(v)) : "" }));
      }))
      : h("p", { class: "muted", text: (it.groups || []).length ? "Groups: " + it.groups.join(", ") : "No lane matched." });

    const note = h("textarea", { id: "note", placeholder: "Contacts, deadlines, cover-letter angle…", "aria-describedby": "note-state" });
    note.value = it.note || "";
    const noteState = h("span", { id: "note-state", "aria-live": "polite" });
    const saveNote = async () => {
      const val = note.value;
      if ((it.note || "") === val) return;
      noteState.textContent = "Saving…";
      try {
        const row = await data.setStatus(it.uid, it.status || "new", val);
        it.note = row && row.note !== undefined ? row.note : val;
        noteState.textContent = "Saved";
        const el = rowEl(it.uid);
        if (el) el.replaceWith(renderRow(it));
      } catch (err) {
        noteState.textContent = "Not saved: " + (err.message || err);
      }
    };
    const saveSoon = debounce(saveNote, 900);
    note.addEventListener("input", () => { noteState.textContent = "Editing…"; saveSoon(); });
    note.addEventListener("blur", saveNote);
    note.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        note.blur();
      }
    });

    const sub = h("div", { class: "sub" },
      h("span", { class: "company", text: it.company || "Unknown employer" }),
      it.location ? [sep(), h("span", { text: it.location })] : null,
      it.department ? [sep(), h("span", { class: "muted", text: it.department })] : null);

    const badges = h("div", { class: "badges" },
      entryBadge(it),
      it.remote === true ? h("span", { class: "badge remote", text: "Remote" }) : it.remote === false ? h("span", { class: "badge", text: "On-site / hybrid" }) : null,
      it.employment_type ? h("span", { class: "badge", text: it.employment_type }) : null,
      tagBadges(it, 4),
      isNew(it) ? h("span", { class: "badge remote", text: "New" }) : null,
      it.active === false ? h("span", { class: "badge senior", text: "No longer listed" }) : null,
      h("span", { class: "badge src", text: it.source }));

    const facts = h("dl", { class: "facts" },
      fact("Salary", fmtSalary(it)),
      fact("Posted", it.posted_at ? fmtDate(it.posted_at) + " · " + relTime(it.posted_at) : null),
      fact("Closes", it.closes_at ? fmtDate(it.closes_at) + (close ? " · " + close.label : "") : null, close && close.soon ? "soon" : null),
      fact("First seen", it.first_seen ? fmtDate(it.first_seen, true) : null),
      fact("Last seen", it.last_seen ? relTime(it.last_seen) : null),
      fact("Grade", it.grade),
      fact("Series", (it.series || []).join(", ")),
      fact("Seniority", it.seniority),
      fact("Experience", it.years_required != null ? it.years_required + "+ years" : null),
      fact("Employer key", it.employer_key));

    const reasons = (it.match_reasons || []).length
      ? h("ul", { class: "reasons" }, it.match_reasons.map((r) => h("li", { text: r })))
      : h("p", { class: "muted", text: "No match reasons recorded." });

    const body = h("div", { class: "detail-body" },
      h("h2", { text: it.title || "(untitled)" }),
      sub, badges,
      h("section", null, h("h3", { text: "Status" }), seg),
      facts.childElementCount ? h("section", null, facts) : null,
      h("section", null, h("h3", { text: "Lanes" }), lanes),
      h("section", null, h("h3", { text: "Why it matched" }), reasons),
      h("section", { class: "note-box" }, h("h3", null, h("label", { for: "note", text: "Note" })), note,
        h("div", { class: "note-foot" }, h("span", { text: state.mode === "static" ? "Saved in this browser" : "Saves automatically" }), noteState)),
      h("section", null, h("h3", { text: "Description" }), renderDescription(it.description),
        it.description_truncated ? h("p", { class: "muted", text: "Loading full description…" }) : null),
      h("div", { class: "detail-meta" },
        h("span", null, "uid ", h("span", { class: "mono", text: it.uid })),
        it.duplicate_of ? h("span", null, "duplicate of ", h("span", { class: "mono", text: it.duplicate_of })) : null,
        it.status_updated_at ? h("span", { text: "status changed " + fmtDate(it.status_updated_at, true) }) : null,
        url ? h("span", null, h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url })) : null));
    pane.replaceChildren(bar, body);
    pane.scrollTop = 0;
  }

  // ---- status changes + undo ----------------------------------------------------------------------
  function findItem(uid) {
    const i = indexOfUid(uid);
    if (i >= 0) return state.items[i];
    return state.detailItem && state.detailItem.uid === uid ? state.detailItem : null;
  }

  function removeRow(uid) {
    const i = indexOfUid(uid);
    if (i < 0) return;
    const el = rowEl(uid);
    state.items.splice(i, 1);
    state.total = Math.max(0, state.total - 1);
    const wasSelected = state.selUid === uid;
    if (el) {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 170);
    }
    renderResultCount();
    if (wasSelected) {
      if (state.items.length) {
        setTimeout(() => select(Math.min(i, state.items.length - 1), { open: $("detail").classList.contains("open") }), el ? 175 : 0);
      } else {
        state.sel = -1;
        state.selUid = null;
        renderDetail(null);
        closeDetail();
        setTimeout(renderFoot, 180);
      }
    } else if (state.sel > i) {
      state.sel -= 1;
    }
    if (state.items.length < 8) maybeLoadMore();
  }

  function refreshRow(it) {
    const el = rowEl(it.uid);
    if (el) {
      const fresh = renderRow(it);
      el.replaceWith(fresh);
    }
    if (state.detailItem && state.detailItem.uid === it.uid) {
      const seg = $("detail").querySelector(".seg");
      if (seg) for (const b of seg.children) b.setAttribute("aria-pressed", String(b.classList.contains("st-" + (it.status || "new"))));
    }
  }

  async function setStatus(uid, status, opts) {
    opts = opts || {};
    const it = findItem(uid) || opts.item;
    if (!it) return;
    const prev = it.status || "new";
    if (prev === status) return;
    const index = indexOfUid(uid);
    it.status = status;
    if (!opts.undo) {
      state.undo.push({ uid, prev, index, item: it });
      if (state.undo.length > 50) state.undo.shift();
    }
    if (index >= 0 && !tabMatches(state.f.tab, status)) removeRow(uid);
    else refreshRow(it);
    if (!opts.silent) {
      const verb = status === "new" ? "Moved back to Inbox" : STATUS_LABEL[status];
      toast(verb + ": " + (it.title || uid), opts.undo ? {} : { action: "Undo", onAction: undo });
    }
    try {
      const row = await data.setStatus(uid, status);
      if (row && row.status_updated_at) it.status_updated_at = row.status_updated_at;
      if (StaticData.filtered) StaticData.filtered = null;
    } catch (err) {
      it.status = prev;
      toast("Could not update status: " + (err.message || err), { error: true });
      loadJobs(true);
    }
    refreshStatsSoon();
    refreshLaneCountsSoon();
  }

  function toggleStatus(uid, status) {
    const it = findItem(uid);
    if (!it) return;
    setStatus(uid, (it.status || "new") === status ? "new" : status);
  }

  function undo() {
    const last = state.undo.pop();
    if (!last) {
      toast("Nothing to undo");
      return;
    }
    const { uid, prev, item } = last;
    if (indexOfUid(uid) < 0 && tabMatches(state.f.tab, prev)) {
      const at = clamp(last.index < 0 ? 0 : last.index, 0, state.items.length);
      state.items.splice(at, 0, item);
      state.total += 1;
      const before = rowsEl().children[at];
      const el = renderRow(item);
      if (before) rowsEl().insertBefore(el, before);
      else rowsEl().append(el);
      renderResultCount();
      renderFoot();
      setStatus(uid, prev, { undo: true, silent: true, item }).then(() => select(at));
    } else {
      setStatus(uid, prev, { undo: true, silent: true, item });
    }
    toast("Undone: back to " + STATUS_LABEL[prev]);
  }

  // ---- toasts / help --------------------------------------------------------------------------------
  function toast(msg, opts) {
    opts = opts || {};
    const box = $("toasts");
    const el = h("div", { class: "toast" + (opts.error ? " error" : "") }, h("span", { text: msg }));
    if (opts.action) {
      el.append(h("button", {
        type: "button", text: opts.action,
        onclick: () => { el.remove(); opts.onAction(); },
      }));
    }
    box.append(el);
    while (box.children.length > 3) box.firstChild.remove();
    setTimeout(() => el.remove(), opts.error ? 7000 : 4500);
  }

  function renderHelp() {
    const body = $("help-body");
    body.replaceChildren(...SHORTCUTS.map(([keys, label]) =>
      h("div", null, h("span", { text: label }), h("span", { class: "keys" }, keys.map((k) => h("kbd", { text: k }))))));
    body.append(h("div", null, h("span", { text: "Combine lanes" }), h("span", { class: "keys" }, h("kbd", { text: "Ctrl/⌘" }), h("kbd", { text: "click" }))));
  }

  function openHelp() {
    const dlg = $("help");
    if (dlg.open) return;
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }

  // ---- views -------------------------------------------------------------------------------------
  function showView(view) {
    state.view = view;
    document.body.dataset.view = view;
    for (const v of VIEWS) {
      const sec = $("view-" + v.id);
      if (sec) sec.hidden = v.id !== view;
    }
    for (const a of document.querySelectorAll("a[data-view]")) {
      if (a.dataset.view === view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
      if (a.dataset.view === "jobs") a.setAttribute("href", state.jobsHash);
    }
    closeDrawer();
    closeDetail();
    if (view === "employers") renderEmployers();
    else if (view === "sources") renderSources();
    else if (view === "runs") renderRuns();
    else if (view === "import") renderImport();
    if (view !== "jobs" && location.hash !== "#/" + view) history.replaceState(null, "", "#/" + view);
    if (view === "jobs") writeHash();
  }

  function pageShell(id, title, lead) {
    const sec = $(id);
    const inner = h("div", { class: "page-inner" }, h("h1", { text: title }), lead ? h("p", { class: "lead", text: lead }) : null);
    sec.replaceChildren(inner);
    return inner;
  }

  function errorBox(err) {
    return h("div", { class: "notice" }, h("span", { class: "result-err", text: "Could not load: " }), err.message || String(err));
  }

  // Employers ---------------------------------------------------------------------------------------
  const empFilter = { q: "", group: "", lane: "", show: "all" };

  async function renderEmployers() {
    const inner = pageShell("view-employers", "Employers",
      "The employer registry: boards jobsweep sweeps automatically, and organisations to check by hand because their careers site is not on a supported ATS.");
    let emps = state.cache.employers;
    if (!emps) {
      inner.append(h("div", { class: "skeleton" }, h("div"), h("div"), h("div")));
      try {
        emps = state.cache.employers = await data.employers();
      } catch (err) {
        inner.lastChild.replaceWith(errorBox(err));
        return;
      }
      inner.lastChild.remove();
    }
    const groups = (state.lanes.groups || []);
    const q = h("input", { type: "search", placeholder: "Search employers", "aria-label": "Search employers", value: empFilter.q });
    const gSel = h("select", { "aria-label": "Group" }, h("option", { value: "", text: "All groups" }),
      groups.map((g) => h("option", { value: g.id, text: shortName(g.name), selected: empFilter.group === g.id })));
    const laneOpts = () => {
      const src = empFilter.group ? groups.filter((g) => g.id === empFilter.group) : groups;
      return [h("option", { value: "", text: "All lanes" })].concat(
        src.flatMap((g) => (g.lanes || []).map((l) => h("option", { value: l.id, text: shortName(l.name), selected: empFilter.lane === l.id }))));
    };
    const lSel = h("select", { "aria-label": "Lane" }, laneOpts());
    const show = h("select", { "aria-label": "Show" },
      [["all", "Boards and manual"], ["sweepable", "Sweepable boards"], ["manual", "Check by hand"]].map(([v, t]) =>
        h("option", { value: v, text: t, selected: empFilter.show === v })));
    const results = h("div");
    inner.append(h("div", { class: "toolbar" },
      h("label", { class: "control" }, icon("search", "sm"), q),
      h("label", { class: "control" }, h("span", { class: "lbl", text: "Group" }), gSel),
      h("label", { class: "control" }, h("span", { class: "lbl", text: "Lane" }), lSel),
      h("label", { class: "control" }, h("span", { class: "lbl", text: "Show" }), show)), results);

    const draw = () => {
      const tokens = empFilter.q.toLowerCase().split(/\s+/).filter(Boolean);
      const keep = (e) => {
        if (empFilter.group && !(e.groups || []).includes(empFilter.group)) return false;
        if (empFilter.lane && !(e.lanes || []).includes(empFilter.lane)) return false;
        if (tokens.length) {
          const hay = [e.key, e.name, e.hq, e.ats, e.ats_vendor, e.board, (e.locations || []).join(" ")].join(" ").toLowerCase();
          if (!tokens.every((t) => hay.includes(t))) return false;
        }
        return true;
      };
      const sweep = (emps.sweepable || []).filter(keep);
      const manual = (emps.manual || []).filter(keep);
      const parts = [];
      if (empFilter.show !== "manual") {
        parts.push(h("h2", null, "Sweepable boards", h("span", { class: "n", text: fmtNum(sweep.length) })));
        parts.push(sweep.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
          h("thead", null, h("tr", null, h("th", { text: "Employer" }), h("th", { text: "Board" }), h("th", { text: "Lanes" }),
            h("th", { class: "num", text: "Open jobs" }), h("th", { text: "Verified" }), h("th", { text: "Links" }))),
          h("tbody", null, sweep.map((e) => h("tr", null,
            h("td", null, h("div", { class: "cell-name", text: e.name }), e.hq ? h("div", { class: "cell-sub", text: e.hq }) : null),
            h("td", null, h("span", { class: "badge ats", text: e.ats }), " ", h("span", { class: "mono muted", text: e.board })),
            h("td", null, h("div", { class: "chips" }, (e.lanes || []).slice(0, 4).map(laneChip),
              (e.lanes || []).length > 4 ? h("span", { class: "chip more", text: "+" + (e.lanes.length - 4) }) : null,
              !(e.lanes || []).length ? (e.groups || []).slice(0, 2).map(groupChip) : null)),
            h("td", { class: "num", text: e.open_jobs != null ? fmtNum(e.open_jobs) : "–" }),
            h("td", { class: "muted", text: e.verified ? fmtDate(e.verified) : "unverified" }),
            h("td", null, h("a", { href: "#/jobs?tab=all&entry=0&min=0&emp=" + encodeURIComponent(e.key), text: "Jobs" }),
              e.careers_url ? [" · ", h("a", { href: e.careers_url, target: "_blank", rel: "noopener noreferrer", text: "Careers" })] : null))))))
          : h("p", { class: "muted", text: "No sweepable boards match." }));
      }
      if (empFilter.show !== "sweepable") {
        parts.push(h("h2", null, "Check by hand", h("span", { class: "n", text: fmtNum(manual.length) })));
        parts.push(manual.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
          h("thead", null, h("tr", null, h("th", { text: "Employer" }), h("th", { text: "Careers site" }), h("th", { text: "Lanes" }), h("th", { text: "Notes" }))),
          h("tbody", null, manual.map((e) => h("tr", null,
            h("td", null, h("div", { class: "cell-name", text: e.name }), e.hq ? h("div", { class: "cell-sub", text: e.hq }) : null),
            h("td", null, e.careers_url
              ? h("a", { href: e.careers_url, target: "_blank", rel: "noopener noreferrer" }, "Open careers page ", icon("external", "sm"))
              : h("span", { class: "muted", text: "no link" }),
            " ", h("span", { class: "badge ats", text: e.ats_vendor || e.ats || "other" })),
            h("td", null, h("div", { class: "chips" }, (e.lanes || []).slice(0, 4).map(laneChip))),
            h("td", { class: "cell-sub", text: e.notes || "" }))))))
          : h("p", { class: "muted", text: "Nothing to check by hand for this filter." }));
      }
      results.replaceChildren(...parts);
    };
    q.addEventListener("input", debounce(() => { empFilter.q = q.value.trim(); draw(); }, 150));
    gSel.addEventListener("change", () => { empFilter.group = gSel.value; empFilter.lane = ""; lSel.replaceChildren(...laneOpts()); draw(); });
    lSel.addEventListener("change", () => { empFilter.lane = lSel.value; draw(); });
    show.addEventListener("change", () => { empFilter.show = show.value; draw(); });
    draw();
  }

  // Sources -------------------------------------------------------------------------------------------
  function reasonNode(src) {
    if (src.available) return h("span", { class: "reason", text: src.kind === "board" ? "Ready: used for every registry employer on this ATS." : "Ready." });
    const r = String(src.reason || "unavailable");
    const m = /^missing env:\s*(.+)$/i.exec(r);
    if (m) {
      return h("span", { class: "reason" }, "Set ", m[1].split(/,\s*/).map((v, i) => [i ? ", " : "", h("code", { text: v })]), " in .env");
    }
    return h("span", { class: "reason", text: r });
  }

  async function renderSources() {
    const inner = pageShell("view-sources", "Sources",
      "Connectors jobsweep sweeps by itself, and the Claude-side connectors whose results you can import.");
    let rows = state.cache.sources;
    if (!rows) {
      try {
        rows = state.cache.sources = await data.sources();
      } catch (err) {
        inner.append(errorBox(err));
        return;
      }
    }
    const card = (s) => h("div", { class: "card src-card" },
      h("div", { class: "top" },
        h("span", { class: "status-dot " + (s.available ? "ok" : s.metered ? "warn" : ""), title: s.available ? "available" : "unavailable" }),
        h("span", { class: "name", text: s.label || s.name }),
        s.kind ? h("span", { class: "badge", text: s.kind }) : null,
        s.metered ? h("span", { class: "badge warn", text: "metered" }) : null),
      h("span", { class: "mono muted", text: s.name }),
      reasonNode(s),
      s.docs_url ? h("div", { class: "links" }, h("a", { href: s.docs_url, target: "_blank", rel: "noopener noreferrer" }, "Docs ", icon("external", "sm"))) : null);
    const boards = rows.filter((s) => s.kind === "board");
    const searches = rows.filter((s) => s.kind !== "board" && s.name !== "import");
    const other = rows.filter((s) => s.name === "import");
    const ok = rows.filter((s) => s.available).length;
    inner.append(
      h("div", { class: "stat-row" },
        h("div", { class: "stat" }, h("b", { text: fmtNum(ok) }), h("span", { text: "available" })),
        h("div", { class: "stat" }, h("b", { text: fmtNum(rows.length - ok) }), h("span", { text: "need setup" })),
        h("div", { class: "stat" }, h("b", { text: fmtNum(rows.filter((s) => s.metered).length) }), h("span", { text: "metered" }))),
      h("h2", null, "Search and feeds", h("span", { class: "n", text: fmtNum(searches.length) })),
      h("div", { class: "grid-cards" }, searches.map(card)),
      h("h2", null, "Employer boards (ATS adapters)", h("span", { class: "n", text: fmtNum(boards.length) })),
      h("div", { class: "grid-cards" }, boards.map(card)),
      other.length ? [h("h2", { text: "Import" }), h("div", { class: "grid-cards" }, other.map(card))] : null,
      h("h2", { text: "Use with Claude" }),
      h("p", { class: "lead", text: "These run inside Claude (claude.ai connectors or MCP servers). Pull results there, then import them so they are classified against your lanes and land on this board." }),
      h("div", { class: "grid-cards" }, CLAUDE_CONNECTORS.map((c) => h("div", { class: "card src-card connector" },
        h("div", { class: "top" }, h("span", { class: "name", text: c.name }), h("span", { class: "badge", text: c.kind })),
        h("p", { text: c.text }),
        h("div", { class: "how", text: c.how })))),
    );
  }

  // Runs ----------------------------------------------------------------------------------------------
  function duration(a, b) {
    const s = parseDate(a), e = parseDate(b);
    if (!s || !e) return "";
    const sec = Math.max(0, Math.round((e - s) / 1000));
    return sec < 90 ? sec + "s" : Math.round(sec / 60) + "m";
  }

  function runRow(r) {
    const st = r.stats || {};
    const per = st.per_source || {};
    const errs = st.error_list || [];
    const opts = (r.meta || {}).options || {};
    const scope = [
      (opts.sources || []).length ? "sources: " + opts.sources.join(", ") : "all sources",
      (opts.groups || []).length ? "groups: " + opts.groups.join(", ") : null,
      (opts.lanes || []).length ? "lanes: " + opts.lanes.length : null,
      opts.include_metered ? "metered" : null,
    ].filter(Boolean).join(" · ");
    const details = Object.keys(per).length || errs.length
      ? h("details", { class: "run-more" }, h("summary", { text: "Details" }),
        Object.keys(per).length ? h("table", { class: "data" },
          h("thead", null, h("tr", null, h("th", { text: "Source" }), h("th", { class: "num", text: "Targets" }), h("th", { class: "num", text: "OK" }),
            h("th", { class: "num", text: "Errors" }), h("th", { class: "num", text: "Jobs" }))),
          h("tbody", null, Object.entries(per).map(([name, p]) => h("tr", null, h("td", { class: "mono", text: name }),
            h("td", { class: "num", text: fmtNum(p.targets) }), h("td", { class: "num", text: fmtNum(p.ok) }),
            h("td", { class: "num", text: fmtNum(p.errors) }), h("td", { class: "num", text: fmtNum(p.jobs) })))))
          : null,
        errs.length ? h("ul", { class: "event-log" }, errs.slice(0, 30).map((e) => h("li", null,
          h("span", { class: "err", text: e.source }), h("span", { text: e.target }), h("span", { class: "muted", text: e.error })))) : null)
      : null;
    return h("tr", null,
      h("td", { class: "num", text: "#" + r.id }),
      h("td", null, h("div", { text: fmtDate(r.started_at, true) }), h("div", { class: "cell-sub", text: scope })),
      h("td", { text: r.finished_at ? duration(r.started_at, r.finished_at) : "running / interrupted" }),
      h("td", { class: "num", text: fmtNum(st.targets) }),
      h("td", { class: "num", text: fmtNum(st.ok) }),
      h("td", { class: "num", text: fmtNum(st.errors) }),
      h("td", { class: "num", text: fmtNum(st.new) }),
      h("td", { class: "num", text: fmtNum(st.total_jobs) }),
      h("td", null, details));
  }

  function checkbox(name, value, label, checked, extra) {
    return h("label", { class: "check" }, h("input", { type: "checkbox", name, value, checked }), h("span", { text: label }), extra || null);
  }

  async function renderRuns() {
    const inner = pageShell("view-runs", "Runs",
      state.mode === "api"
        ? "Run a sweep across employer boards and job feeds, watch it live, and review earlier runs."
        : "Sweeps for this snapshot run on a schedule in GitHub Actions; this page shows the latest one.");
    if (state.mode === "api") {
      inner.append(await sweepForm());
      inner.append(h("div", { id: "sweep-progress" }));
      refreshSweepStatus(true);
    } else {
      inner.append(h("div", { class: "notice", text: "Static snapshot: running sweeps and importing are available when you run the board locally with `jobsweep serve`." }));
    }
    const hist = h("div", null, h("div", { class: "skeleton" }, h("div"), h("div")));
    inner.append(h("h2", { text: "History" }), hist);
    try {
      const runs = await data.runs();
      hist.replaceChildren(runs.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, ["Run", "Started", "Took", "Targets", "OK", "Errors", "New", "Stored", ""].map((t, i) =>
          h("th", { class: i >= 3 && i <= 7 ? "num" : null, text: t })))),
        h("tbody", null, runs.map(runRow)))) : h("p", { class: "muted", text: "No sweeps recorded yet." }));
    } catch (err) {
      hist.replaceChildren(errorBox(err));
    }
  }

  async function sweepForm() {
    let sources = state.cache.sources;
    if (!sources) {
      try {
        sources = state.cache.sources = await data.sources();
      } catch (err) {
        sources = [];
      }
    }
    const usable = sources.filter((s) => s.name !== "import");
    const groups = (state.lanes.groups || []).filter((g) => g.active !== false);
    const form = h("form", { class: "card card-pad", "aria-label": "Run a sweep" });
    const srcList = h("div", { class: "check-list" }, usable.map((s) =>
      checkbox("source", s.name, s.label || s.name, false, !s.available ? h("span", { class: "badge off", title: s.reason || "", text: "setup" }) : s.metered ? h("span", { class: "badge warn", text: "metered" }) : null)));
    const grpList = h("div", { class: "check-list" }, groups.map((g) => checkbox("group", g.id, shortName(g.name), false, swatch(g.id))));
    const laneDet = h("details", null, h("summary", { class: "link-btn", text: "Pick individual lanes" }),
      groups.map((g) => h("div", { style: { margin: "8px 0" } }, h("div", { class: "cell-sub", text: shortName(g.name) }),
        h("div", { class: "check-list" }, (g.lanes || []).map((l) => checkbox("lane", l.id, shortName(l.name), false))))));
    const toggles = h("div", { class: "check-list" },
      checkbox("boards", "1", "Employer boards", true), checkbox("searches", "1", "Keyword searches", true),
      checkbox("include_metered", "1", "Include metered", false), checkbox("dry_run", "1", "Dry run (plan only)", false));
    const days = h("select", { name: "days" }, [["7", "7 days"], ["14", "14 days"], ["30", "30 days"], ["60", "60 days"], ["", "any time"]]
      .map(([v, t]) => h("option", { value: v, text: t, selected: v === "30" })));
    const maxQ = h("input", { type: "number", name: "max_queries", min: "1", max: "500", value: "40" });
    const submit = h("button", { class: "btn primary", type: "submit" }, icon("play", "sm"), "Run sweep");
    form.append(
      h("div", { class: "form-grid" },
        h("fieldset", { class: "field", style: { "grid-column": "1 / -1" } }, h("legend", { text: "Sources (none ticked = every available, non-metered source)" }), srcList),
        h("fieldset", { class: "field", style: { "grid-column": "1 / -1" } }, h("legend", { text: "Groups (none = all active lanes)" }), grpList, laneDet),
        h("fieldset", { class: "field", style: { "grid-column": "1 / -1" } }, h("legend", { text: "Options" }), toggles),
        h("label", { class: "field" }, h("span", { text: "Posted within" }), days),
        h("label", { class: "field" }, h("span", { text: "Max queries per source" }), maxQ)),
      h("div", { class: "form-actions" }, submit, h("span", { class: "muted", id: "sweep-form-msg", "aria-live": "polite" })));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const body = {
        sources: fd.getAll("source"),
        groups: fd.getAll("group"),
        lanes: fd.getAll("lane"),
        boards: fd.has("boards"),
        searches: fd.has("searches"),
        include_metered: fd.has("include_metered"),
        dry_run: fd.has("dry_run"),
        posted_within_days: fd.get("days") ? Number(fd.get("days")) : null,
        max_queries_per_source: Number(fd.get("max_queries")) || 40,
      };
      for (const k of ["sources", "groups", "lanes"]) if (!body[k].length) body[k] = null;
      submit.disabled = true;
      try {
        await data.sweep(body);
        toast(body.dry_run ? "Planning sweep…" : "Sweep started");
        state.sweep.running = true;
        renderLastSweep();
        refreshSweepStatus(true);
      } catch (err) {
        toast(err.status === 409 ? "A sweep is already running" : "Could not start sweep: " + err.message, { error: err.status !== 409 });
        if (err.status === 409) refreshSweepStatus(true);
      } finally {
        submit.disabled = state.sweep.running;
      }
    });
    return form;
  }

  async function refreshSweepStatus(poll) {
    if (state.mode !== "api") return;
    clearTimeout(state.sweep.timer);
    let st;
    try {
      st = await data.sweepStatus();
    } catch (err) {
      return;
    }
    const wasRunning = state.sweep.running;
    state.sweep.running = Boolean(st.running);
    state.sweep.status = st;
    renderProgress(st);
    renderLastSweep();
    if (st.running && poll !== false) {
      state.sweep.timer = setTimeout(() => refreshSweepStatus(true), 1200);
    } else if (wasRunning && !st.running) {
      const rep = st.last_report || {};
      if (rep.error) toast("Sweep failed: " + rep.error, { error: true });
      else if (rep.dry_run) toast("Dry run planned");
      else toast("Sweep finished: " + fmtNum(rep.new_count != null ? rep.new_count : (rep.new_uids || []).length) + " new jobs");
      refreshStats();
      refreshLaneCounts();
      if (state.view === "jobs") loadJobs(true);
      if (state.view === "runs") renderRuns();
    }
  }

  function renderProgress(st) {
    const box = $("sweep-progress");
    if (!box) return;
    const p = st.progress || {};
    const rep = st.last_report;
    if (!st.running && !rep) {
      box.replaceChildren();
      return;
    }
    const pct = p.total ? clamp((p.done / p.total) * 100, 0, 100) : 0;
    const parts = [
      h("h2", null, st.running ? "Sweep in progress" : "Last sweep from this session",
        h("span", { class: "n", text: p.phase ? "(" + p.phase + ")" : "" })),
      h("div", { class: "progress" + (st.running && !p.total ? " indeterminate" : ""), role: "progressbar", "aria-valuemin": "0",
        "aria-valuemax": String(p.total || 100), "aria-valuenow": String(p.done || 0) },
      h("span", { style: { width: (st.running ? pct : 100).toFixed(1) + "%" } })),
      h("div", { class: "stat-row" },
        h("div", { class: "stat" }, h("b", { text: fmtNum(p.done) + (p.total != null ? " / " + fmtNum(p.total) : "") }), h("span", { text: "targets" })),
        h("div", { class: "stat" }, h("b", { text: fmtNum(p.ok) }), h("span", { text: "ok" })),
        h("div", { class: "stat" }, h("b", { text: fmtNum(p.errors) }), h("span", { text: "errors" })),
        h("div", { class: "stat" }, h("b", { text: fmtNum(p.jobs) }), h("span", { text: "jobs fetched" })),
        h("div", { class: "stat" }, h("b", { text: fmtNum(p.new) }), h("span", { text: "new" }))),
    ];
    const recent = (p.recent || []).slice().reverse();
    if (recent.length) {
      parts.push(h("ul", { class: "event-log", "aria-label": "Recent targets" }, recent.map((e) => h("li", null,
        h("span", { class: e.ok ? "ok" : "err", text: e.ok ? "ok " : "err" }),
        h("span", { text: e.source + " " + e.target }),
        h("span", { class: "muted", text: e.ok ? fmtNum(e.jobs) + " jobs, " + fmtNum(e.new) + " new (" + e.elapsed_s + "s)" : e.error || "" })))));
    }
    if (!st.running && rep) {
      if (rep.error) parts.push(h("p", { class: "result-err", text: "Failed: " + rep.error }));
      if (rep.summary) parts.push(h("pre", { class: "summary", text: rep.summary }));
      if (!rep.error && !rep.dry_run) {
        parts.push(h("div", { class: "form-actions" }, h("a", { class: "btn", href: "#/jobs?days=1&sort=newest" }, "View new jobs")));
      }
    }
    box.replaceChildren(h("div", { class: "card card-pad", style: { "margin-top": "14px" } }, parts));
  }

  // Import --------------------------------------------------------------------------------------------
  function renderImport() {
    const inner = pageShell("view-import", "Import",
      "Paste what a Claude connector returned (Indeed connector markdown, Workopia or Jobs and Careers JSON) or any JSON or CSV list of jobs. Imported postings are classified against your lanes and added to the board.");
    if (state.mode !== "api") {
      inner.append(h("div", { class: "notice", text: "Import needs the local server (`jobsweep serve`)." }));
      return;
    }
    const origin = h("input", { type: "text", name: "origin", value: "indeed-connector", list: "origin-suggest", required: true });
    const fmt = h("select", { name: "format" }, [["auto", "Detect automatically"], ["md", "Markdown (Indeed connector)"], ["json", "JSON"], ["csv", "CSV"]]
      .map(([v, t]) => h("option", { value: v, text: t })));
    const text = h("textarea", { name: "payload", spellcheck: "false", placeholder: "**Job Title:** Geopolitical Analyst\n**Company:** …\n**Location:** New York, NY\n**View Job URL:** https://…" });
    const file = h("input", { type: "file", accept: ".json,.csv,.md,.txt,application/json,text/csv,text/markdown,text/plain", "aria-label": "Load from file" });
    file.addEventListener("change", async () => {
      const f = file.files && file.files[0];
      if (!f) return;
      text.value = await f.text();
      if (/\.json$/i.test(f.name)) fmt.value = "json";
      else if (/\.csv$/i.test(f.name)) fmt.value = "csv";
      else if (/\.(md|markdown)$/i.test(f.name)) fmt.value = "md";
    });
    const result = h("div", { "aria-live": "polite", style: { "margin-top": "12px" } });
    const submit = h("button", { class: "btn primary", type: "submit" }, icon("upload", "sm"), "Import jobs");
    const form = h("form", { class: "card card-pad" },
      h("div", { class: "form-grid" },
        h("label", { class: "field" }, h("span", { text: "Origin (where the results came from)" }), origin),
        h("label", { class: "field" }, h("span", { text: "Format" }), fmt),
        h("label", { class: "field" }, h("span", { text: "Or load a file" }), file)),
      h("datalist", { id: "origin-suggest" }, ["indeed-connector", "workopia", "jobs-and-careers", "linkedin", "manual"].map((v) => h("option", { value: v }))),
      h("label", { class: "field", style: { "margin-top": "14px" } }, h("span", { text: "Results" }), text),
      h("div", { class: "form-actions" }, submit,
        h("button", { class: "btn", type: "button", text: "Clear", onclick: () => { text.value = ""; result.replaceChildren(); text.focus(); } })),
      result);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!text.value.trim()) {
        result.replaceChildren(h("span", { class: "result-err", text: "Paste some results first." }));
        return;
      }
      submit.disabled = true;
      result.replaceChildren(h("span", { class: "muted", text: "Importing…" }));
      try {
        const res = await data.importJobs({ origin: origin.value.trim() || "manual", format: fmt.value, payload: text.value });
        result.replaceChildren(
          h("span", { class: "result-ok", text: "Imported " + fmtNum(res.imported) + " jobs, " + fmtNum(res.new) + " new." }), " ",
          res.imported ? h("a", { class: "btn sm", href: "#/jobs?tab=all&entry=0&min=0&src=import&sort=newest" }, "Show imported jobs") : null);
        refreshStats();
        refreshLaneCounts();
      } catch (err) {
        result.replaceChildren(h("span", { class: "result-err", text: "Import failed: " }), err.message || String(err));
      } finally {
        submit.disabled = false;
      }
    });
    inner.append(form);
  }

  // ---- events --------------------------------------------------------------------------------------
  function bindEvents() {
    $("theme-btn").addEventListener("click", cycleTheme);
    $("help-btn").addEventListener("click", openHelp);
    $("help-close").addEventListener("click", () => $("help").close());
    $("help").addEventListener("click", (e) => {
      if (e.target === $("help")) $("help").close();
    });
    $("menu-btn").addEventListener("click", () => ($("sidebar").classList.contains("open") ? closeDrawer() : openDrawer()));
    $("scrim").addEventListener("click", closeDrawer);
    $("filters-toggle").addEventListener("click", () => {
      const open = $("filters").classList.toggle("open");
      $("filters-toggle").setAttribute("aria-expanded", String(open));
    });

    const search = $("search");
    const onSearch = debounce(() => {
      const q = search.value.trim();
      if (q === state.f.q) return;
      state.f.q = q;
      if (state.view !== "jobs") showView("jobs");
      applyFilters();
    }, 220);
    search.addEventListener("input", onSearch);
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        onSearch();
        search.blur();
        if (state.items.length) select(0);
      }
    });

    for (const el of document.querySelectorAll("#filters .control")) el.dataset.title = el.getAttribute("title") || "";
    $("f-entry").addEventListener("change", (e) => { state.f.entry = e.target.checked; applyFilters(); });
    $("f-remote").addEventListener("change", (e) => { state.f.remote = e.target.checked; applyFilters(); });
    const minChanged = debounce(() => applyFilters(), 250);
    $("f-min").addEventListener("input", (e) => {
      state.f.min = Number(e.target.value);
      $("f-min-out").textContent = e.target.value;
      minChanged();
    });
    $("f-loc").addEventListener("input", debounce((e) => { state.f.loc = e.target.value.trim(); applyFilters(); }, 300));
    $("f-days").addEventListener("change", (e) => { state.f.days = e.target.value; applyFilters(); });
    $("f-sort").addEventListener("change", (e) => { state.f.sort = e.target.value; applyFilters(); });
    document.addEventListener("click", (e) => {
      const dd = $("f-sources");
      if (dd.open && !dd.contains(e.target)) dd.open = false;
    });

    $("rows").addEventListener("click", (e) => {
      const row = e.target.closest(".row");
      if (!row) return;
      const uid = row.dataset.uid;
      const idx = indexOfUid(uid);
      const act = e.target.closest("[data-act]");
      if (act) {
        e.preventDefault();
        if (idx >= 0 && state.selUid !== uid) select(idx, { scroll: false });
        if (act.dataset.act === "note") focusNote();
        else toggleStatus(uid, act.dataset.act);
        return;
      }
      if (e.target.closest("a")) {
        if (idx >= 0) select(idx, { scroll: false });
        return;
      }
      if (idx >= 0) select(idx, { open: true, scroll: false });
    });

    if ("IntersectionObserver" in window) {
      state.io = new IntersectionObserver((entries) => {
        if (entries.some((x) => x.isIntersecting)) maybeLoadMore();
      }, { root: $("job-list"), rootMargin: "600px" });
      state.io.observe($("list-foot"));
    }

    window.matchMedia(WIDE_QUERY).addEventListener("change", () => {
      $("detail").classList.remove("open");
      if (isWide() && state.sel < 0 && state.items.length) select(0, { scroll: false });
    });

    document.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", onHashChange);
  }

  function focusNote() {
    const it = currentItem();
    if (!it) return;
    if (!isWide()) showDetail(it, true);
    const ta = $("note");
    if (ta) {
      ta.scrollIntoView({ block: "center" });
      ta.focus({ preventScroll: true });
    }
  }

  function onKey(e) {
    if (e.defaultPrevented || e.isComposing) return;
    const t = e.target;
    const typing = t && t.closest && t.closest("input, textarea, select, [contenteditable='true']");
    if (e.key === "Escape") {
      if ($("help").open) return;
      if (typing) {
        t.blur();
        return;
      }
      if ($("f-sources").open) {
        $("f-sources").open = false;
        return;
      }
      if ($("sidebar").classList.contains("open")) {
        closeDrawer();
        return;
      }
      closeDetail();
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey || $("help").open) return;
    if (e.key === "?") {
      e.preventDefault();
      openHelp();
      return;
    }
    if (e.key === "/") {
      e.preventDefault();
      $("search").focus();
      $("search").select();
      return;
    }
    if (state.view !== "jobs") return;
    const it = currentItem();
    switch (e.key) {
      case "j":
        e.preventDefault();
        move(1);
        break;
      case "k":
        e.preventDefault();
        move(-1);
        break;
      case "o":
      case "Enter":
        if (e.key === "Enter" && t.closest && t.closest("a, button, summary")) return;
        e.preventDefault();
        openPosting(it);
        break;
      case "s":
        if (it) toggleStatus(it.uid, "saved");
        break;
      case "a":
        if (it) toggleStatus(it.uid, "applied");
        break;
      case "d":
        if (it) toggleStatus(it.uid, "dismissed");
        break;
      case "u":
        undo();
        break;
      case "n":
        e.preventDefault();
        focusNote();
        break;
      default:
        break;
    }
  }

  // ---- boot ----------------------------------------------------------------------------------------
  async function detectMode() {
    const m = window.JOBSWEEP_MODE;
    if (m === "api" || m === "static") return m;
    try {
      const r = await fetch("api/stats", { headers: { Accept: "application/json" } });
      if (r.ok && (r.headers.get("content-type") || "").includes("json")) return "api";
    } catch (e) {
      /* no server */
    }
    return "static";
  }

  async function init() {
    applyTheme(currentTheme());
    bindEvents();
    renderHelp();
    state.mode = await detectMode();
    document.body.dataset.mode = state.mode;
    data = state.mode === "static" ? StaticData : ApiData;
    renderSkeleton();
    try {
      await data.init();
    } catch (err) {
      rowsEl().replaceChildren();
      $("list-foot").replaceChildren(h("div", { class: "empty" }, h("h3", { text: "Could not load the board" }),
        h("p", { text: (state.mode === "static" ? "data/jobs.json: " : "API: ") + (err.message || String(err)) })));
      renderViewsNav();
      return;
    }
    indexLanes();
    const parsed = parseHash();
    state.f = parsed.f || defaultFilters();
    state.pendingJob = parsed.job;
    if (parsed.view === "jobs") state.jobsHash = location.hash || "#/jobs";
    renderViewsNav();
    syncControls();
    renderCounts();
    renderBanner();
    renderLaneTree();
    showView(parsed.view);
    loadJobs(true);
    refreshLaneCounts();
    if (state.mode === "api") {
      refreshSweepStatus(true);
      if (state.f.employers.length && !state.cache.employers) {
        data.employers().then((e) => { state.cache.employers = e; renderActiveChips(); }).catch(() => {});
      }
    } else {
      state.cache.employers = StaticData.base.employers || null;
    }
  }

  init();
})();
