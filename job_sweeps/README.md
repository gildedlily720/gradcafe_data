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

## No-repost rule (added Oct 22, 11:30 am ET)
- A posting counts as new only if it was created after the previous check. LinkedIn's "listed N minutes ago" resets on every re-listing, so it is not evidence of a new posting; most "just listed" cards are re-listings of requisitions created days or weeks earlier.
- LinkedIn job IDs are assigned at creation and only increase (about 0.7M a day, roughly 20k to 40k an hour). `sweep.py` now drops any LinkedIn item whose ID is at or below the ledger's `max_linkedin_job_id_seen` (the highest ID seen at the previous check) and reports those only as a count of "re-listed older postings dropped". Override with `--id-floor N` for a custom window; for the Oct 22 since-midnight screen the floor was 4482030840, the highest ID in the Oct 21 10:08 pm ET LinkedIn digest.
- Gmail digest items follow the same rule: compare the LinkedIn ID in the link against the ledger max before reporting.
- Glassdoor "Just posted" is not enough on its own; confirm on the employer site (a new requisition number or a posted date). Board feeds (Greenhouse `first_published`, Lever `createdAt`, Ashby `publishedAt`, NYC Jobs `posting_date`) carry real creation dates; Workday "Posted Today" is accepted.
- Employer-side checks that work from here: Greenhouse boards API (`first_published`), Fiserv and Guidehouse Workday (`postedOn`), Crum & Forster iCIMS (requisition number, e.g. 2026-5178), AEI iCIMS (requisition order only; its JSON-LD `datePosted` is rendered at fetch time and unusable). Not reachable: USAJOBS search (client-rendered), Lever LeadBank (404), Liberty Mutual careers, Allianz Phenom (no US cargo hit), Brown & Brown (403), Workday roots for Lazard, Berkley, Ankura, MTA, Zurich, MUFG and PJT (406).
- Reporting: list only non-repost items as postings. Items that are not reposts but were created one to three days earlier (for example NYC Jobs rows that the open dataset publishes with a two-day lag) go in a separate "not a repost, created earlier" line so they are never mistaken for today's.
