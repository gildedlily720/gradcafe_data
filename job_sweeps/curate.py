#!/usr/bin/env python3
"""Curate a sweep: list NEW items with off-lane/senior flags and fetch LinkedIn details for the shortlist.
usage: python3 curate.py out/sweep_<stamp>.json [--max N] [--all]
"""
import html, json, re, sys, time, urllib.error, urllib.request
OFF = re.compile(r"\b(software|engineer(ing)?|developer|devops|nurse|nursing|RN|LPN|physician|clinical|clinic|patient|sales|marketing|account executive|accountant|accounting|bookkeep|payroll|paralegal|legal assistant|legal secretary|attorney|counsel|cyber|SOC|penetration|data scientist|machine learning|UX|product (manager|owner|design)|recruit(er|ing|ment)?|talent|human resources|HR|teacher|professor|lecturer|faculty|tenure|customer (service|success)|cashier|driver|warehouse|technician|therapist|pharmac\w*|dental|real estate|construction|electrician|mechanic|chef|cook|barista|retail|store|call center|scientist|laboratory|lab|security (guard|officer)|janitor|housekeep\w*|caregiver|physical therapy|surgical|oncology|histology|biolog\w*|chemist\w*|supply operations|QA|quality assurance|deviation|GME|residency|aerospace|electrical|full[- ]stack|front[- ]end|back[- ]end|SQL|java|python|cloud|network engineer|IT )\b", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|director|vice president|vp|head of|principal|manager|managing|partner|chief|executive director|lead|supervisor|III|IV|V)\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
def clean(s): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
def fetch(jid):
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}", headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r: return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2: time.sleep(8 * (attempt + 1)); continue
            raise
def age_hours(ago):
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)", ago or "")
    if not m: return None
    n, u = int(m.group(1)), m.group(2)
    return n / 60 if u == "minute" else n if u == "hour" else n * 24 if u == "day" else n * 168 if u == "week" else n * 720
def repost_verdict(ago, applicants):
    """Re-listed postings keep their applicant history while 'listed N ago' resets, so many applicants on a
    young listing means a repost. 'Be among the first 25 applicants' means the posting is new on LinkedIn."""
    h = age_hours(ago); m = re.search(r"(\d+)\s*applicants", applicants or "")
    if "first 25" in (applicants or ""): return "new on LinkedIn (under 25 applicants)"
    if m and h is not None:
        n = int(m.group(1))
        if h <= 3 and n >= 40: return f"REPOST ({n} applicants after {ago})"
        if h <= 1 and n >= 20: return f"REPOST ({n} applicants after {ago})"
        if h <= 24 and n >= 150: return f"REPOST ({n} applicants after {ago})"
        return f"unclear ({n} applicants after {ago})"
    return "unclear"
def detail(jid):
    b = fetch(jid)
    g = lambda p: (re.search(p, b, re.S) or [None, ""])[1]
    crit = {clean(k): clean(v) for k, v in re.findall(r"description__job-criteria-subheader[^>]*>(.*?)</h3>\s*<span[^>]*>(.*?)</span>", b, re.S)}
    desc = clean(g(r"show-more-less-html__markup[^>]*>(.*?)</div>"))
    ago = clean(g(r"posted-time-ago__text[^>]*>(.*?)</span>")); appl = clean(g(r"num-applicants__caption[^>]*>(.*?)</"))
    return {"id": jid, "title": clean(g(r"top-card-layout__title[^>]*>(.*?)</h")), "company": clean(g(r"topcard__org-name-link[^>]*>(.*?)</a>")),
            "location": clean(g(r"topcard__flavor--bullet[^>]*>(.*?)</span>")), "ago": ago, "applicants": appl, "verdict": repost_verdict(ago, appl),
            "seniority": crit.get("Seniority level"), "emp_type": crit.get("Employment type"), "salary": clean(g(r"compensation__salary[^>]*>(.*?)</div>")),
            "years": re.findall(r"[^.]{0,60}\b\d+\+?\s*(?:to|-|–)?\s*\d*\s*years?[^.]{0,50}", desc)[:3],
            "pay": re.findall(r"\$\s?[\d,]{4,}(?:\.\d+)?(?:\s*(?:-|to|–)\s*\$?\s?[\d,]{4,})?", desc)[:3],
            "clearance": re.findall(r"(TS/SCI|Top Secret|Secret clearance|security clearance|public trust|polygraph)", desc, re.I)[:2],
            "gs": re.findall(r"\bGS-?\s?\d{1,2}\b[^.]{0,40}", desc)[:3], "deadline": re.findall(r"[^.]{0,40}(?:deadline|apply by|closes? on|closing date)[^.]{0,60}", desc, re.I)[:2],
            "desc": desc[:1200]}
def main():
    path = sys.argv[1]; mx = 30; everything = "--all" in sys.argv
    if "--max" in sys.argv: mx = int(sys.argv[sys.argv.index("--max") + 1])
    d = json.load(open(path)); items = d["items"]
    new = [i for i in items if i.get("new")]
    relisted = [i for i in items if i.get("repost_by_id")]
    print(f"# {path}: {len(items)} in window, {len(new)} new to the ledger" + (f", {len(relisted)} dropped by id floor" if relisted else ""))
    short = []
    for i in sorted(new, key=lambda x: (x["lane"], x["title"])):
        off = bool(OFF.search(i["title"])); sen = bool(SENIOR.search(i["title"]))
        tag = ("OFF " if off else "") + ("SENIOR " if sen else "")
        print(f"- [{tag.strip() or 'ok'}] {i['source']}:{i['id']} | {i['lane']} | {i['title'][:80]} | {i['employer'][:40]} | {i['location'][:30]} | {i.get('posted_text') or i.get('date')}")
        if i["source"] == "linkedin" and (everything or (not off and not sen)): short.append(i)
    print(f"\n# fetching details for {min(len(short), mx)} of {len(short)} shortlisted")
    out = {}
    for i in short[:mx]:
        try: out[str(i["id"])] = detail(i["id"])
        except Exception as e: out[str(i["id"])] = {"id": i["id"], "err": f"{type(e).__name__}: {str(e)[:80]}"}
        x = out[str(i["id"])]
        print(json.dumps({k: v for k, v in x.items() if k != "desc"})[:700])
        time.sleep(1.5)
    dp = path.replace("sweep_", "details_"); json.dump(out, open(dp, "w"), indent=1); print(f"wrote {dp}")
if __name__ == "__main__": main()
