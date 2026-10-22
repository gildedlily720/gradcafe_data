#!/usr/bin/env python3
"""Hourly job sweep across the lanes in lanes.md.

Sources with real timestamps:
  * LinkedIn guest job search (f_TPR window, per-card datetime + relative age)
  * Greenhouse boards (first_published), Lever (createdAt), Workday (postedOn text)
  * NYC Jobs open dataset kpav-sd4t (posting_date; lags about two days)

Usage:
  python3 job_sweeps/sweep.py --hours 1.25            # last 75 minutes
  python3 job_sweeps/sweep.py --since 2026-10-22T04:00:00Z
  add --update-ledger to record everything found in seen.json
Writes job_sweeps/out/sweep_<utc timestamp>.json and prints a markdown summary of NEW items.
"""
import argparse, datetime as dt, html, json, os, re, sys, time, urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "seen.json")
OUT = os.path.join(HERE, "out")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

# ---------------------------------------------------------------- lanes
# (lane, LinkedIn keywords). Quotes are passed through to LinkedIn.
LINKEDIN_QUERIES = [
    ("Political risk", '"political risk" analyst'),
    ("Political risk", '"country risk" OR "sovereign risk" analyst'),
    ("Political risk", '"geopolitical" analyst OR researcher'),
    ("Political risk", '"geoeconomics" OR "economic security" analyst'),
    ("Political risk", '"supply chain risk" OR "forced labor" analyst'),
    ("Political risk", '"expert network" OR "primary research" associate'),
    ("Intelligence and security", '"intelligence analyst"'),
    ("Intelligence and security", '"threat intelligence" analyst'),
    ("Intelligence and security", '"protective intelligence" OR "executive protection" analyst'),
    ("Intelligence and security", '"GSOC" OR "global security operations" analyst'),
    ("Intelligence and security", '"corporate security" analyst'),
    ("Intelligence and security", '"OSINT" OR "open source intelligence" analyst'),
    ("Intelligence and security", '"counterterrorism" OR "counter-terrorism" analyst'),
    ("Intelligence and security", '"travel risk" OR "crisis response" analyst'),
    ("Investigations", '"due diligence" analyst OR associate'),
    ("Investigations", '"corporate intelligence" OR "investigative research"'),
    ("Investigations", '"investigative analyst" OR "investigations analyst"'),
    ("Investigations", '"forensic" investigations analyst'),
    ("Investigations", '"fraud investigator" OR "fraud investigations" analyst'),
    ("Investigations", '"public defender" investigator'),
    ("Investigations", '"beneficial ownership" OR "asset recovery" analyst'),
    ("Sanctions and financial crime", '"sanctions" analyst'),
    ("Sanctions and financial crime", '"export controls" OR "export compliance" analyst'),
    ("Sanctions and financial crime", '"AML" analyst OR investigator'),
    ("Sanctions and financial crime", '"KYC" analyst'),
    ("Sanctions and financial crime", '"financial crime" analyst OR investigator'),
    ("Sanctions and financial crime", '"market surveillance" OR "trade surveillance" analyst'),
    ("Sanctions and financial crime", '"blockchain" investigations OR "crypto" investigations analyst'),
    ("Trust and safety", '"trust and safety" OR "trust & safety" analyst OR associate'),
    ("Trust and safety", '"content policy" OR "platform integrity"'),
    ("Trust and safety", '"misinformation" OR "disinformation" OR "influence operations" analyst'),
    ("Trust and safety", '"AI policy" OR "model policy" researcher OR analyst'),
    ("Trust and safety", '"election integrity" OR "human rights" researcher OR associate'),
    ("Policy and research", '"research associate" policy'),
    ("Policy and research", '"research assistant" policy OR "think tank"'),
    ("Policy and research", '"program associate" OR "program assistant" policy'),
    ("Policy and research", '"policy analyst"'),
    ("Policy and research", '"legislative analyst" OR "legislative" assistant'),
    ("Policy and research", '"national security" OR "foreign policy" analyst OR associate'),
    ("Policy and research", '"fact-checker" OR "fact checking" OR "research writer"'),
    ("Policy and research", '"criminal justice" OR "urban planning" research analyst'),
    ("Strategic communications", '"strategic communications" analyst OR associate'),
    ("Strategic communications", '"public affairs" OR "government affairs" associate OR analyst'),
    ("Strategic communications", '"crisis communications" OR "issues management"'),
    ("Strategic communications", '"media analysis" OR "media monitoring" OR "insights" analyst'),
    ("Specialty insurance", '"assistant underwriter" OR "underwriting assistant"'),
    ("Specialty insurance", '"underwriter" trainee OR "associate underwriter"'),
    ("Specialty insurance", '"political risk" OR "trade credit" OR "surety" underwriter'),
    ("Specialty insurance", '"transactional liability" OR "representations and warranties" OR "financial lines"'),
    ("Specialty insurance", '"fine art" OR "specie" OR "aviation" OR "marine" underwriter'),
    ("Specialty insurance", '"reinsurance" analyst OR "broker" associate'),
    ("Specialty insurance", '"kidnap" OR "political violence" OR "terrorism" OR "war risk" insurance'),
    ("Specialty insurance", '"SIU" OR "special investigations unit" OR "insurance fraud" investigator'),
    ("Government", '"District Attorney" analyst'),
    ("Government", '"Department of Investigation" OR "Inspector General" OR "CCRB"'),
    ("Government", '"Federal Reserve Bank of New York"'),
    ("Government", '"emergency management" OR "resiliency" analyst'),
    ("Government", '"Pathways" "recent graduate"'),
    ("Entry routes", '"early careers" OR "graduate program" OR "rotational program" 2027'),
    ("Entry routes", '"analyst program" OR "development program" 2027'),
    ("Entry routes", '"fellowship" research OR policy'),
]
# geoId 90000070 = New York City Metropolitan Area; 103644278 = United States
GEOS = [
    ("NYC metro", {"geoId": "90000070"}),
    ("Remote US", {"geoId": "103644278", "f_WT": "2"}),
]
DC_LANES = {"Policy and research", "Trust and safety", "Intelligence and security", "Political risk", "Government"}
DC_GEO = ("DC metro", {"geoId": "90000097"})

GREENHOUSE_BOARDS = ["recordedfuture", "exiger", "sayari", "alethea", "anthropic", "everstreamanalytics",
                     "verainstituteofjustice", "okx", "teneo", "humanrightswatch", "discord", "reddit",
                     "pinterest", "xai", "scaleai", "coinbase", "robinhood", "stripe", "janes", "10alabs",
                     "fgsglobal", "cision"]
LEVER_BOARDS = ["palantir", "LeadBank", "everbridge", "shieldai"]
ASHBY_BOARDS = ["trm-labs", "openai", "ramp"]
WORKDAY_SITES = [  # (host, tenant, site, label)
    ("aig.wd1.myworkdayjobs.com", "aig", "aig", "AIG"),
    ("djeholdings.wd5.myworkdayjobs.com", "djeholdings", "edelman-careers-E200", "Edelman"),
]
WORKDAY_SEARCHES = ["analyst", "underwriter", "associate", "intelligence", "investigations", "policy", "early careers"]
NYC_KEYWORDS = re.compile(r"analyst|investigat|intelligence|policy|research|emergency|resilien|integrity|"
                          r"counter ?terror|fraud|compliance|communications|public affairs|planner", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|director|vice president|vp|head of|principal|manager|managing|partner|"
                    r"chief|executive director|lead)\b", re.I)
GREENHOUSE_LOC_OK = re.compile(r"new york|nyc|manhattan|brooklyn|remote|united states|washington|dc\b|jersey", re.I)

# ---------------------------------------------------------------- helpers
def get(url, **kw):
    for attempt in range(4):
        try:
            r = S.get(url, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1)); continue
            return r
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None

REL = re.compile(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", re.I)
def rel_to_dt(text, now):
    m = REL.search(text or "")
    if not m: return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"minute": dt.timedelta(minutes=n), "hour": dt.timedelta(hours=n), "day": dt.timedelta(days=n),
             "week": dt.timedelta(weeks=n), "month": dt.timedelta(days=30 * n)}[unit]
    return now - delta

CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)</li>', re.S)
TITLE = re.compile(r'base-search-card__title">\s*(.*?)\s*</h3>', re.S)
COMPANY = re.compile(r'base-search-card__subtitle">.*?<a[^>]*>\s*(.*?)\s*</a>', re.S)
COMPANY2 = re.compile(r'base-search-card__subtitle">\s*(.*?)\s*</h4>', re.S)
LOC = re.compile(r'job-search-card__location">\s*(.*?)\s*</span>', re.S)
TIME = re.compile(r'<time[^>]*datetime="([^"]+)"[^>]*>\s*(.*?)\s*</time>', re.S)

def linkedin(lane, keywords, geo_label, geo_params, window_s, now, max_pages=4):
    out = []
    for page in range(max_pages):
        params = {"keywords": keywords, "f_TPR": f"r{window_s}", "start": page * 25}
        params.update(geo_params)
        url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode(params)
        r = get(url)
        if r is None or r.status_code != 200 or not r.text.strip():
            break
        cards = CARD.findall(r.text)
        for jid, body in cards:
            t = TITLE.search(body); c = COMPANY.search(body) or COMPANY2.search(body)
            l = LOC.search(body); tm = TIME.search(body)
            title = html.unescape(re.sub(r"\s+", " ", t.group(1))) if t else ""
            company = html.unescape(re.sub(r"<[^>]+>|\s+", " ", c.group(1)).strip()) if c else ""
            loc = html.unescape(re.sub(r"\s+", " ", l.group(1))) if l else ""
            date_attr = tm.group(1) if tm else ""
            rel = html.unescape(re.sub(r"\s+", " ", tm.group(2))) if tm else ""
            posted = rel_to_dt(rel, now)
            out.append({"key": f"linkedin:{jid}", "source": "linkedin", "lane": lane, "query": keywords,
                        "geo": geo_label, "id": int(jid), "title": title, "employer": company, "location": loc,
                        "date": date_attr, "posted_text": rel,
                        "posted_at": posted.isoformat(timespec="minutes") if posted else "",
                        "url": f"https://www.linkedin.com/jobs/view/{jid}/"})
        if len(cards) < 25:
            break
        time.sleep(1.0)
    time.sleep(0.8)
    return out

def greenhouse(slug, since):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    if r is None or r.status_code != 200: return out
    for j in r.json().get("jobs", []):
        fp = j.get("first_published") or j.get("updated_at") or ""
        try: ts = dt.datetime.fromisoformat(fp.replace("Z", "+00:00"))
        except ValueError: continue
        loc = (j.get("location") or {}).get("name", "")
        if ts >= since and GREENHOUSE_LOC_OK.search(loc or ""):
            out.append({"key": f"greenhouse:{j['id']}", "source": f"greenhouse:{slug}", "lane": "board", "query": slug,
                        "geo": loc, "id": j["id"], "title": j.get("title", ""), "employer": slug, "location": loc,
                        "date": fp[:10], "posted_text": "first_published " + fp, "posted_at": ts.isoformat(timespec="minutes"),
                        "url": j.get("absolute_url", "")})
    return out

def lever(slug, since):
    r = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    if r is None or r.status_code != 200: return out
    for j in r.json():
        ts = dt.datetime.fromtimestamp(j.get("createdAt", 0) / 1000, tz=dt.timezone.utc)
        loc = (j.get("categories") or {}).get("location", "") or ""
        if ts >= since and GREENHOUSE_LOC_OK.search(loc):
            out.append({"key": f"lever:{j['id']}", "source": f"lever:{slug}", "lane": "board", "query": slug,
                        "geo": loc, "id": j["id"], "title": j.get("text", ""), "employer": slug, "location": loc,
                        "date": ts.date().isoformat(), "posted_text": "createdAt", "posted_at": ts.isoformat(timespec="minutes"),
                        "url": j.get("hostedUrl", "")})
    return out

def ashby(slug, since):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    if r is None or r.status_code != 200: return out
    for j in r.json().get("jobs", []):
        pa = j.get("publishedAt") or ""
        try: ts = dt.datetime.fromisoformat(pa.replace("Z", "+00:00"))
        except ValueError: continue
        loc = j.get("location", "") or ""
        if ts >= since and (j.get("isRemote") or GREENHOUSE_LOC_OK.search(loc)):
            out.append({"key": f"ashby:{j['id']}", "source": f"ashby:{slug}", "lane": "board", "query": slug,
                        "geo": loc, "id": j["id"], "title": j.get("title", ""), "employer": slug, "location": loc,
                        "date": pa[:10], "posted_text": "publishedAt " + pa, "posted_at": ts.isoformat(timespec="minutes"),
                        "url": j.get("jobUrl", "")})
    return out

def workday(host, tenant, site, label, since, now):
    out = []
    seen = set()
    for q in WORKDAY_SEARCHES:
        try:
            r = S.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q},
                       headers={"Accept": "application/json"}, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200: continue
        for j in r.json().get("jobPostings", []):
            po = j.get("postedOn", "")
            m = re.search(r"Posted (Today|Yesterday|(\d+)\+? Days? Ago)", po)
            if not m: continue
            days = 0 if m.group(1) == "Today" else 1 if m.group(1) == "Yesterday" else int(m.group(2))
            ts = now - dt.timedelta(days=days)
            req = (j.get("bulletFields") or [j.get("externalPath", "")])[0]
            key = f"workday:{tenant}:{req}"
            if key in seen: continue
            seen.add(key)
            if ts.date() >= since.date():
                out.append({"key": key, "source": f"workday:{label}", "lane": "board", "query": q, "geo": j.get("locationsText", ""),
                            "id": req, "title": j.get("title", ""), "employer": label, "location": j.get("locationsText", ""),
                            "date": ts.date().isoformat(), "posted_text": po, "posted_at": "",
                            "url": f"https://{host}/en-US/{site}{j.get('externalPath', '')}"})
        time.sleep(0.5)
    return out

def nyc_jobs(since):
    start = (since - dt.timedelta(days=3)).strftime("%Y-%m-%dT00:00:00")
    q = urllib.parse.quote(f"posting_date >= '{start}'")
    r = get(f"https://data.cityofnewyork.us/resource/kpav-sd4t.json?$where={q}&$limit=2000&$order=posting_date DESC")
    out = []
    if r is None or r.status_code != 200: return out
    for j in r.json():
        text = " ".join(str(j.get(k, "")) for k in ("business_title", "civil_service_title", "job_category", "agency"))
        if not NYC_KEYWORDS.search(text): continue
        out.append({"key": f"nycjobs:{j.get('job_id')}", "source": "nycjobs", "lane": "Government", "query": "kpav-sd4t",
                    "geo": j.get("work_location", ""), "id": j.get("job_id"), "title": j.get("business_title", ""),
                    "employer": j.get("agency", ""), "location": j.get("work_location", ""),
                    "date": (j.get("posting_date") or "")[:10], "posted_text": f"salary {j.get('salary_range_from')}-{j.get('salary_range_to')} {j.get('salary_frequency','')}; {j.get('career_level','')}",
                    "posted_at": "", "url": f"https://cityjobs.nyc.gov/job/{j.get('job_id')}"})
    return out

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, help="window in hours")
    ap.add_argument("--since", help="ISO timestamp (UTC) start of window")
    ap.add_argument("--update-ledger", action="store_true")
    ap.add_argument("--skip-linkedin", action="store_true")
    ap.add_argument("--skip-boards", action="store_true")
    ap.add_argument("--max-pages", type=int, default=4)
    ap.add_argument("--ledger-from", help="merge a saved out/sweep_*.json into seen.json and exit (no fetching)")
    a = ap.parse_args()
    if a.ledger_from:
        saved = json.load(open(a.ledger_from))
        ledger = json.load(open(LEDGER)) if os.path.exists(LEDGER) else {"items": [], "max_linkedin_job_id_seen": 0}
        seen = {it["key"] for it in ledger["items"]}
        added = 0
        for i in saved["items"]:
            if i["key"] in seen: continue
            ledger["items"].append({"key": i["key"], "title": i["title"], "employer": i["employer"], "location": i["location"],
                                    "pay": "", "lane": i["lane"], "first_seen": saved["run_at"][:16],
                                    "evidence": f"{i['source']} {i['posted_text'] or i['date']}", "status": "swept", "url": i["url"]})
            seen.add(i["key"]); added += 1
        mx = max([ledger.get("max_linkedin_job_id_seen", 0)] + [i["id"] for i in saved["items"] if i["source"] == "linkedin"])
        ledger["max_linkedin_job_id_seen"] = mx
        ledger["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
        json.dump(ledger, open(LEDGER, "w"), indent=1)
        print(f"ledger: +{added} from {a.ledger_from}; {len(ledger['items'])} entries; max LinkedIn id {mx}")
        return
    now = dt.datetime.now(dt.timezone.utc)
    if a.since:
        since = dt.datetime.fromisoformat(a.since.replace("Z", "+00:00"))
    else:
        since = now - dt.timedelta(hours=a.hours or 1.25)
    window_s = int((now - since).total_seconds()) + 300
    ledger = json.load(open(LEDGER)) if os.path.exists(LEDGER) else {"items": [], "max_linkedin_job_id_seen": 0}
    seen = {it["key"] for it in ledger["items"]}
    def norm(it): return re.sub(r"[^a-z0-9]+", " ", f"{it.get('employer','')}|{it.get('title','')}".lower()).strip()
    seen_norm = {norm(it) for it in ledger["items"]}
    found = {}

    def add(items):
        for it in items:
            if it["key"] not in found:
                found[it["key"]] = it

    if not a.skip_linkedin:
        for lane, kw in LINKEDIN_QUERIES:
            geos = list(GEOS) + ([DC_GEO] if lane in DC_LANES else [])
            for glabel, gparams in geos:
                add(linkedin(lane, kw, glabel, gparams, window_s, now, a.max_pages))
            print(f"  linkedin {lane}: {kw!r} -> {len(found)} total", file=sys.stderr)
    if not a.skip_boards:
        for slug in GREENHOUSE_BOARDS: add(greenhouse(slug, since))
        for slug in LEVER_BOARDS: add(lever(slug, since))
        for slug in ASHBY_BOARDS: add(ashby(slug, since))
        for host, tenant, site, label in WORKDAY_SITES: add(workday(host, tenant, site, label, since, now))
        add(nyc_jobs(since))

    # keep LinkedIn items inside the window using the relative age when we have it
    items = []
    for it in found.values():
        if it["source"] == "linkedin" and it["posted_at"]:
            if dt.datetime.fromisoformat(it["posted_at"]) < since - dt.timedelta(minutes=30):
                continue
        it["new"] = it["key"] not in seen and norm(it) not in seen_norm
        it["repost_of_seen"] = it["key"] not in seen and norm(it) in seen_norm
        it["senior"] = bool(SENIOR.search(it["title"]))
        items.append(it)
    # collapse same employer+title found under several LinkedIn IDs in this run (reposts), keep newest
    by_norm = {}
    for it in sorted(items, key=lambda x: x.get("posted_at") or "", reverse=True):
        k = (it["source"], norm(it))
        if k not in by_norm: by_norm[k] = it
    items = list(by_norm.values())
    items.sort(key=lambda x: (x["lane"], not x["new"], x["senior"], x["employer"], x["title"]))

    os.makedirs(OUT, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(OUT, f"sweep_{stamp}.json")
    json.dump({"run_at": now.isoformat(), "since": since.isoformat(), "count": len(items), "items": items}, open(path, "w"), indent=1)

    new = [i for i in items if i["new"]]
    print(f"\n# Sweep {now.strftime('%Y-%m-%d %H:%M UTC')} window since {since.strftime('%Y-%m-%d %H:%M UTC')}: {len(items)} in window, {len(new)} new\n")
    lane = None
    for i in new:
        if i["lane"] != lane:
            lane = i["lane"]; print(f"\n## {lane}")
        flag = " [senior]" if i["senior"] else ""
        print(f"- {i['title']} | {i['employer']} | {i['location']} | {i['posted_text'] or i['date']} | {i['url']}{flag}")
    print(f"\nwrote {path}")

    if a.update_ledger:
        for i in items:
            if i["key"] in seen: continue
            ledger["items"].append({"key": i["key"], "title": i["title"], "employer": i["employer"], "location": i["location"],
                                    "pay": "", "lane": i["lane"], "first_seen": now.isoformat(timespec="minutes"),
                                    "evidence": f"{i['source']} {i['posted_text'] or i['date']}", "status": "swept", "url": i["url"]})
            seen.add(i["key"])
        mx = max([ledger.get("max_linkedin_job_id_seen", 0)] + [i["id"] for i in items if i["source"] == "linkedin"])
        ledger["max_linkedin_job_id_seen"] = mx
        ledger["updated"] = now.isoformat(timespec="minutes")
        json.dump(ledger, open(LEDGER, "w"), indent=1)
        print(f"ledger updated: {len(ledger['items'])} entries, max LinkedIn id {mx}")

if __name__ == "__main__":
    main()
