# Job sweeps

Hourly sweep for new postings in the lanes listed in `lanes.md`. Each day's findings go in
`YYYY-MM-DD.md`; every posting ever surfaced goes in `seen.json` so later checks only report
genuinely new items.

## Sources used (in order of how well they date a posting)

1. Alert emails in Gmail: LinkedIn job-alert digests (about every two hours, each covers the
   user's ~15 saved searches), Glassdoor daily alerts (carry "Just posted" / "9h" ages),
   beBee alerts, LinkedIn saved-job and recommendation emails.
2. The Jobs & Careers job-search tool (US postings, relevance ranked, no dates, top 3 per page)
   run with the lane queries below, pages 1-2.
3. The Workopia search tool (`posted_within_days` filter; its relevance is weak outside
   underwriting titles; `postedDate` is usually empty).
4. Web search (extended mode) to date a specific posting or find the employer's own listing.

## What is blocked in this environment

Outbound requests to `www.linkedin.com`, `boards-api.greenhouse.io`, `api.lever.co`,
`data.cityofnewyork.us` (and, by the same policy, Workday, Ashby, USAJOBS, Glassdoor, Indeed,
cityjobs.nyc.gov, statejobs.ny.gov) are denied by the cloud environment's network policy, so
posting timestamps cannot be read directly. If those hosts are added under Allowed domains in
the environment settings (Edit environment, Network access), the hourly check should switch to
LinkedIn's guest search feed with a one-hour window as the primary source:

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=<query>&location=New%20York%2C%20United%20States&f_TPR=r3600&start=0

(`f_TPR=r3600` = past hour; each job card carries a `<time datetime="...">` element.)
Greenhouse boards (`/v1/boards/<slug>/jobs`, field `updated_at`), Lever (`createdAt`) and the
NYC Jobs dataset `kpav-sd4t` (field `posting_date`) give exact timestamps too.

## Dating heuristics while the network is blocked

- LinkedIn job IDs are assigned in sequence. `seen.json` keeps `max_linkedin_job_id_seen`;
  an ID above it was created after the last check. On Oct 21-22 the IDs grew by roughly
  1.5 million per day.
- Glassdoor alert ages ("Just posted", "9h", "1d") are relative to the email's send time.
- Anything with no dating evidence is listed as "date unverified", never as new.

## Lane queries for the Jobs & Careers tool (city New York, state NY, seniority INTERNSHIP/ENTRY/MID)

1. Political risk: political risk, country risk, sovereign risk, geopolitical risk, geopolitics,
   geoeconomics, economic security, risk consulting, strategic intelligence x analyst/associate/researcher.
2. Sanctions and financial crime: sanctions, export controls, AML, KYC, financial crime,
   illicit finance, blockchain investigations, market surveillance, enforcement x analyst/investigator/associate.
3. Investigations: due diligence, corporate intelligence, investigative research, beneficial
   ownership, forensic investigations, fraud investigations, asset recovery, white collar,
   litigation analyst, investigative analyst, public defender x analyst/investigator/associate.
4. Intelligence and security (non-cyber): threat intelligence, protective intelligence,
   executive protection, travel risk, crisis response, GSOC, corporate security, OSINT,
   counterterrorism, intelligence analyst x analyst/associate/officer; exclude SIEM,
   penetration testing, vulnerability management.
5. Specialty insurance: political risk insurance, political violence, terrorism, war risk,
   kidnap and ransom, crisis management, trade credit, surety, credit insurance, transactional
   liability, financial lines, D&O, fine art, specie, aviation, marine, cargo, reinsurance,
   SIU x underwriter/assistant underwriter/broker/trainee/analyst/investigator.
6. Trust and safety: trust and safety, content policy, platform integrity, policy enforcement,
   AI policy, election integrity, disinformation, influence operations, human rights x analyst/researcher/associate.
7. Policy and research: national security, foreign policy, international affairs, think tank,
   legislative, policy research, criminal justice, social policy, urban planning, fact-checking,
   research writing x research associate/research assistant/program associate/policy analyst/fellow/writer.
8. Strategic communications: strategic communications, crisis communications, public affairs,
   government affairs, media analysis, media monitoring, insights x analyst/associate/coordinator/assistant account executive.
9. Government: District Attorney, Department of Investigation, Inspector General, CCRB, Mayor's
   Office, DFS, DCJS, Attorney General, Comptroller, MTA, Port Authority, NYPD, Federal Reserve
   Bank of New York, Emergency Management, FEMA, City of New York x analyst/investigator/policy analyst/fellow.
10. Entry routes: graduate program, early careers, rotational program, analyst program,
    development program, trainee, fellowship, recent graduate x risk/insurance/underwriting/
    intelligence/policy/compliance/investigations/research/public affairs.

## Hourly procedure

1. Gmail: `newer_than:2h` from the alert senders (jobalerts-noreply@linkedin.com,
   jobs-noreply@linkedin.com, noreply@glassdoor.com, noreply@notification.bebee.com,
   alerts@ziprecruiter.com, indeed.com, idealist.org, welcometothejungle.com, usajobs.gov,
   myworkday.com, greenhouse.io, lever.co). Read each new thread as plain text and extract
   every posting (title, employer, location, pay, LinkedIn job ID or URL).
2. Run the ten lane queries above (pages 1-2) and the Workopia underwriting queries with
   `posted_within_days: 1`.
3. Drop anything already in `seen.json` (match on LinkedIn job ID, or on employer + title +
   location) and anything on the already-applied list below.
4. Date what is left (heuristics above). Report new items in the posting format used in the
   daily file; put undated items in a separate "date unverified" block.
5. Append to the day's file, add to `seen.json` (update `max_linkedin_job_id_seen`), commit and
   push to branch `claude/wizardly-cerf-21o03d`.
6. If nothing is new, say so in one line.

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
Analyst; Booking Holdings Analyst ERM; Democracy Forward Research Assistant; Documented
Research Fellowship; Third Bridge Associate and Research Associate Library; JPMorganChase
Security & Resiliency Field Research Analyst; Sibylline Protective Intelligence Analyst;
Control Risks Intelligence Analyst; Ergo Custom Research Analyst; Moonshot Threat Intelligence
Analyst; Recorded Future Fraud Analyst; Ontic Strategic Intelligence Analyst; Crisis24
Associate Consultant; AlertMedia Global Intelligence Analyst; HSBC AVP Financial Crime Risk
Analysis; W.W. Norton Editorial Assistant; NYT News Assistant Data and Researcher; BerlinRosen
and Civitas (Orchestra); Milltown Partners Senior Analyst; federal Pathways and ITA
applications (see tracker).
