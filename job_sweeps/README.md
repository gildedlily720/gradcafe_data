# Job sweeps

Hourly sweep for new postings in the lanes listed in `lanes.md`. Each day's findings go in
`YYYY-MM-DD.md`; every posting ever surfaced goes in `seen.json` so later checks only report
genuinely new items. Raw results of each run land in `out/sweep_<utc timestamp>.json`.

## The script

    python3 job_sweeps/sweep.py --hours 1.25                 # the hourly check (75-minute window)
    python3 job_sweeps/sweep.py --since 2026-10-22T04:00:00Z # everything since midnight ET on Oct 22
    add --update-ledger to record everything found in seen.json after the items have been reported

It prints a markdown list of NEW items (not in the ledger, not a repost of a ledger item) grouped
by lane, flags titles that look senior, and writes the full window to `out/`.

Sources, all with real posting times (network access to these hosts was enabled Oct 22):

1. LinkedIn guest job search, `f_TPR=r<seconds>` window, 61 lane queries, run for the New York
   City metro (geoId 90000070), remote US (geoId 103644278 with `f_WT=2`) and, for policy,
   trust and safety, intelligence, political risk and government lanes, the DC metro
   (geoId 90000097). Each card carries a date and a relative age ("2 hours ago"); the script
   turns the age into a timestamp. Pages of 25; the script stops at the first short page.
2. Greenhouse boards (`first_published`): recordedfuture, exiger, sayari, alethea, anthropic,
   everstreamanalytics, verainstituteofjustice, okx, teneo, humanrightswatch, discord, reddit,
   pinterest, xai, scaleai, coinbase, robinhood, stripe, janes, 10alabs, fgsglobal, cision.
3. Lever (`createdAt`): palantir, LeadBank, everbridge, shieldai. Ashby (`publishedAt`): trm-labs,
   openai, ramp.
4. Workday job API (`postedOn` text such as "Posted Today"): AIG (aig/aig) and Edelman
   (djeholdings/edelman-careers-E200). Other Workday tenants exist (Chubb, Aon, WTW, Kroll,
   Moody's, Verisk, Zurich, Everest, RAND, Fitch, Gallagher, Lockton) but their site names were
   not discoverable without a browser; LinkedIn covers their postings within hours anyway.
5. NYC Jobs open dataset `kpav-sd4t` (`posting_date`, lags about two days; filtered to analyst,
   investigator, intelligence, policy, research, emergency management, integrity, fraud,
   compliance, communications and planning titles).

Not available: USAJOBS needs an API key (free registration at developer.usajobs.gov; add it as
an environment secret and the script can be extended), Workable blocks scripted access
(Control Risks), and cityjobs.nyc.gov and statejobs.ny.gov render client-side.

Alert emails in Gmail (LinkedIn digests roughly every two hours, Glassdoor daily, beBee) remain a
cross-check: the hourly run reads anything received since the previous check.

## Hourly procedure

1. `python3 job_sweeps/sweep.py --hours 1.25` (plus Gmail `newer_than:2h` from the alert senders:
   jobalerts-noreply@linkedin.com, jobs-noreply@linkedin.com, noreply@glassdoor.com,
   noreply@notification.bebee.com, alerts@ziprecruiter.com, indeed.com).
2. Curate the NEW items against `lanes.md`: drop off-lane titles, mark senior ones, skip the
   already-applied list below.
3. Report the kept items in the posting format of the daily file (title, employer and unit,
   location, posted evidence, salary, experience asked, deadline, why it fits, link), with a
   separate short block for date-unverified items. Nothing new: one line.
4. Append to the day's file (New York date), run the script again with `--update-ledger` (or
   add the curated keys by hand), commit and push to branch `claude/wizardly-cerf-21o03d`.

## Dating notes

- LinkedIn job IDs are sequential; `seen.json` keeps `max_linkedin_job_id_seen`. LinkedIn also
  re-lists postings under new IDs, so the script treats the same employer plus title as a repost.
- Glassdoor alert ages ("Just posted", "9h") are relative to the email's send time.

## Already applied (from the coaching tracker in Drive, Oct 3, 2026), do not re-report

AIG 2027 Early Careers Analyst Underwriting Political Risk (JR2603633); Arch Associate
Underwriter North; Argo Underwriter I Environmental; Tokio Marine HCC Associate Specialty
Underwriter Crisis Management; SiriusPoint Underwriting Analyst Global Programs; Gen Re Aviation
Underwriter; RLI Associate Underwriter Executive Products; QBE Next Generation Associate
Underwriter; AXA XL Associate Underwriter Political Risk & Credit; Manhattan DA Counter
Terrorism Analyst (795044), Hate Crimes Investigative Analyst and Investigative Analyst 3238;
NYS Attorney General Investigative Analyst; NYC DCWP Investigator Worker Protection (802725);
Manhattan Borough President Special Assistant (803780); Everstream Analyst Intelligence
Solutions; Deloitte Project Analyst OSINT Forensics; Hilco Analyst Global Investigations;
Kobre & Kim Analyst I; Kroll Associate Investigations Diligence & Compliance; Kharon Research
Analyst; Booking Holdings Analyst ERM; Serdika Insight (via ecruit) U.S. Policy, Political & Investment Risk Research Consultant (Oct 22); Democracy Forward Research Assistant; Documented
Research Fellowship; Third Bridge Associate and Research Associate Library; JPMorganChase
Security & Resiliency Field Research Analyst; Sibylline Protective Intelligence Analyst;
Control Risks Intelligence Analyst; Ergo Custom Research Analyst; Moonshot Threat Intelligence
Analyst; Recorded Future Fraud Analyst; Ontic Strategic Intelligence Analyst; Crisis24
Associate Consultant; AlertMedia Global Intelligence Analyst; HSBC AVP Financial Crime Risk
Analysis; W.W. Norton Editorial Assistant; NYT News Assistant Data and Researcher; BerlinRosen
and Civitas (Orchestra); Milltown Partners Senior Analyst; federal Pathways and ITA
applications (see tracker).
